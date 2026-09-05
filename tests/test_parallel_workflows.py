"""Exercise generic parallel requests and resumable work without model calls."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_runner as runner


class ParallelWorkflowTests(unittest.TestCase):
    @staticmethod
    def document():
        return {
            "prompts": {
                "instructions": "Score only the requested region.",
                "regional": "{instructions}\nRegion {region}; source {source}.",
                "combine": "Summarize the completed regional reports: {reports}",
            },
            "nodes": {
                "summarize_regions": {
                    "run": "structured", "role": "analyst", "stage": "regional_summary",
                    "prompt": "combine", "inputs": {"reports": "json(parallel)"},
                    "response": {
                        "summary": "string",
                        "reviews": {"items": {"fields": {"region": "string", "score": "integer"}}},
                    },
                    "parallel": {
                        "items": ["north", "south"], "prompt": "regional",
                        "instructions": "instructions",
                        "inputs": {"region": "item", "source": "state.source"},
                        "response": {"region": "string", "score": "integer"},
                        "output": "reviews", "item_field": "region",
                        "checkpoint": {
                            "file": "regional-checkpoint.json", "disabled": "fresh-regions",
                            "identity": {"source": "state.source", "instructions": "instructions"},
                            "item_key": "regions",
                        },
                    },
                    "require": ["len(result.reviews) == 2"],
                    "after": [{"set": {"output": "result.summary", "reviews": "result.reviews"}}],
                    "next": "end",
                },
            },
        }

    def test_parallel_requests_run_concurrently_and_reuse_only_matching_checkpoints(self):
        barrier = threading.Barrier(2)
        audit_calls, coordinator_prompts = [], []

        def model(prompt, schema, stage, **options):
            if options.get("request_label"):
                index = int(options["request_label"].rsplit(" ", 1)[1])
                audit_calls.append((index, prompt))
                # Both requests must start before either response can complete.
                barrier.wait(timeout=2)
                result = {"region": "model-supplied label", "score": index}
            else:
                coordinator_prompts.append(prompt)
                result = {
                    "summary": "REGIONAL SUMMARY",
                    "reviews": [{"region": "fabricated", "score": 99}] * 2,
                }
            return result, json.dumps(result)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "custom.yaml"
            path.write_text(json.dumps(self.document()), encoding="utf-8")
            with (
                patch.object(runner.Path, "cwd", return_value=workspace),
                patch.object(runner, "structured", side_effect=model),
                patch.object(runner, "emit"),
            ):
                first = runner.execute(path, {"source": "ORIGINAL"})
                restored = runner.execute(path, {"source": "ORIGINAL"})
                replacement = runner.execute(path, {"source": "CHANGED"})
            saved = json.loads((workspace / "regional-checkpoint.json").read_text())

        expected = [{"region": "north", "score": 1}, {"region": "south", "score": 2}]
        for state in (first, restored, replacement):
            self.assertEqual(state["output"], "REGIONAL SUMMARY")
            self.assertEqual(state["reviews"], expected)
        self.assertEqual(len(audit_calls), 4)
        self.assertEqual(len(coordinator_prompts), 3)
        self.assertTrue(all("fabricated" not in prompt for prompt in coordinator_prompts))
        self.assertEqual(saved["reports"], expected)
        self.assertEqual(sum("source ORIGINAL" in prompt for _, prompt in audit_calls), 2)
        self.assertEqual(sum("source CHANGED" in prompt for _, prompt in audit_calls), 2)

    def test_failed_parallel_request_preserves_completed_work_and_resumes_only_the_missing_item(self):
        audit_calls, coordinator_calls = [], []

        def model(prompt, schema, stage, **options):
            if options.get("request_label"):
                index = int(options["request_label"].rsplit(" ", 1)[1])
                audit_calls.append(index)
                if index == 2 and audit_calls.count(2) == 1:
                    raise runner.Error("Temporary provider failure")
                result = {"region": "supplied", "score": index}
            else:
                coordinator_calls.append(prompt)
                result = {"summary": "RECOVERED", "reviews": [{"region": "x", "score": 0}] * 2}
            return result, json.dumps(result)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "custom.yaml"
            path.write_text(json.dumps(self.document()), encoding="utf-8")
            with (
                patch.object(runner.Path, "cwd", return_value=workspace),
                patch.object(runner, "structured", side_effect=model),
                patch.object(runner, "emit"),
            ):
                with self.assertRaisesRegex(runner.Error, "Temporary provider failure"):
                    runner.execute(path, {"source": "UNCHANGED"})
                self.assertFalse(coordinator_calls)
                saved = json.loads((workspace / "regional-checkpoint.json").read_text())
                self.assertEqual(saved["reports"], [{"region": "north", "score": 1}, None])
                result = runner.execute(path, {"source": "UNCHANGED"})
        self.assertEqual(result["output"], "RECOVERED")
        self.assertEqual(audit_calls.count(1), 1)
        self.assertEqual(audit_calls.count(2), 2)
        self.assertEqual(len(coordinator_calls), 1)

    def test_parallel_requests_inherit_parent_model_and_effort_with_explicit_child_override(self):
        for child_model in (None, "gpt-5.6-terra"):
            with self.subTest(child_model=child_model), tempfile.TemporaryDirectory() as directory:
                document = self.document()
                node = document["nodes"]["summarize_regions"]
                node.update(model="gpt-5.6-luna", effort="low")
                if child_model:
                    node["parallel"]["model"] = child_model
                workspace = Path(directory)
                path = workspace / "settings.yaml"
                path.write_text(json.dumps(document), encoding="utf-8")

                def model(prompt, schema, stage, **options):
                    result = (
                        {"region": "placeholder", "score": 1}
                        if options.get("request_label")
                        else {"summary": "DONE", "reviews": [{"region": "x", "score": 0}] * 2}
                    )
                    return result, json.dumps(result)

                with (
                    patch.object(runner.Path, "cwd", return_value=workspace),
                    patch.object(runner, "structured", side_effect=model) as requests,
                    patch.object(runner, "emit"),
                ):
                    runner.execute(path, {"source": "INPUT"})
                self.assertEqual(requests.call_count, 3)
                for call in requests.call_args_list:
                    expected = "gpt-5.6-luna"
                    if call.kwargs.get("request_label") and child_model:
                        expected = child_model
                    self.assertEqual(call.kwargs["model"], expected)
                    self.assertEqual(call.kwargs["effort"], "low")

    def test_invalid_parallel_configuration_fails_before_any_model_request(self):
        invalid = (
            {"items": []}, {"prompt": "missing"}, {"attempts": True}, {"run": "unknown"},
            {"item_field": ""}, {"checkpoint": {"file": "checkpoint.json"}},
            {"response": {"score": "unknown"}},
        )
        for change in invalid:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                document = copy.deepcopy(self.document())
                document["nodes"]["summarize_regions"]["parallel"].update(change)
                path = Path(directory) / "invalid.yaml"
                path.write_text(json.dumps(document), encoding="utf-8")
                with patch.object(runner, "structured") as requests:
                    with self.assertRaises(ValueError):
                        runner.execute(path, {"source": "INPUT"})
                    requests.assert_not_called()


if __name__ == "__main__":
    unittest.main()
