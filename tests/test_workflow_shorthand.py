"""Ensure concise workflow definitions preserve the existing execution format."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_runner as runner


class WorkflowShorthandTests(unittest.TestCase):
    def load_document(self, document):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.yaml"
            path.write_text(json.dumps(document), encoding="utf-8")
            return runner.load_workflow(path)

    @staticmethod
    def response_document(response):
        return {
            "prompts": {"work": "Work."},
            "nodes": {
                "worker": {
                    "run": "structured", "prompt": "work",
                    "response": response, "next": "end",
                },
            },
        }

    def test_response_expands_to_the_exact_strict_json_schema(self):
        document = self.response_document({
            "title": "string", "accepted": "boolean", "count": "integer",
            "score": "number", "missing": "null", "verdict": ["keep", "drop"],
            "details": {"fields": {"note": "string"}},
            "checks": {
                "items": {"fields": {"focus": "string", "verdict": ["pass", "fail"]}},
                "minItems": 3, "maxItems": 3,
            },
            "tags": {"items": "string"},
            "matrix": {"items": {"items": "integer", "maxItems": 2}, "minItems": 0},
        })
        loaded = self.load_document(document)["nodes"]["worker"]

        self.assertNotIn("response", loaded)
        self.assertEqual(loaded["schema"], {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "accepted": {"type": "boolean"},
                "count": {"type": "integer"},
                "score": {"type": "number"},
                "missing": {"type": "null"},
                "verdict": {"type": "string", "enum": ["keep", "drop"]},
                "details": {
                    "type": "object", "properties": {"note": {"type": "string"}},
                    "required": ["note"], "additionalProperties": False,
                },
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "focus": {"type": "string"},
                            "verdict": {"type": "string", "enum": ["pass", "fail"]},
                        },
                        "required": ["focus", "verdict"], "additionalProperties": False,
                    },
                    "minItems": 3, "maxItems": 3,
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "matrix": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "integer"}, "maxItems": 2},
                    "minItems": 0,
                },
            },
            "required": ["title", "accepted", "count", "score", "missing", "verdict", "details", "checks", "tags", "matrix"],
            "additionalProperties": False,
        })
        self.assertEqual(loaded["next"], {"done": "end"})

    def test_shorthand_and_longhand_have_identical_requests_events_and_state(self):
        short = {
            "prompts": {
                "style": "Keep this literal LaTeX: \\frac{a}{b}.",
                "draft": "{instructions}\nDraft {source}.",
                "inspect": "Inspect {body}; visit {visit}.",
                "finish": "Format {body}.",
            },
            "nodes": {
                "draft": {
                    "run": "structured", "role": "drafter", "stage": "draft",
                    "prompt": "draft", "instructions": "style",
                    "inputs": {"source": "state.input"}, "response": {"text": "string"},
                    "after": [{"set": {"body": "result.text"}}], "next": "inspect",
                },
                "inspect": {
                    "run": "structured", "role": "inspector", "stage": "inspect",
                    "prompt": "inspect", "inputs": {"body": "state.body", "visit": "visit"},
                    "response": {"text": "string", "decision": ["adjust", "accept"]},
                    "outcome": "result.decision",
                    "before": [{"emit": {"kind": "status", "text": "Inspection {visit}"}}],
                    "after": [
                        {"set": {"body": "result.text"}},
                        {"emit": {"kind": "inspection_result", "report": "=result"}},
                    ],
                    "next": {
                        "accept": "finish",
                        "adjust": {
                            "repeat": 3, "option": "attempts", "then": "finish",
                            "after": [
                                {"set": {"accepted_at_limit": "True"}},
                                {"emit": {"kind": "status", "text": "Accepted after {limit} visits"}},
                            ],
                        },
                    },
                },
                "finish": {
                    "run": "structured", "role": "formatter", "stage": "finish",
                    "prompt": "finish", "inputs": {"body": "state.body"},
                    "response": {"text": "string"},
                    "after": [{"set": {"output": "result.text"}}], "next": "end",
                },
            },
        }
        long = copy.deepcopy(short)
        text_schema = {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        }
        for name in ("draft", "finish"):
            long["nodes"][name].pop("response")
            long["nodes"][name]["schema"] = copy.deepcopy(text_schema)
            long["nodes"][name]["next"] = {"done": short["nodes"][name]["next"]}
        long["nodes"]["draft"]["instructions"] = ["style"]
        long["nodes"]["inspect"].pop("response")
        long["nodes"]["inspect"]["schema"] = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "decision": {"type": "string", "enum": ["adjust", "accept"]},
            },
            "required": ["text", "decision"], "additionalProperties": False,
        }
        self.assertEqual(self.load_document(short), self.load_document(long))
        replies = [
            {"text": "DRAFT"}, {"text": "REPAIR 1", "decision": "adjust"},
            {"text": "REPAIR 2", "decision": "adjust"}, {"text": "FINAL"},
        ]
        results = []
        for document in (short, long):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "workflow.yaml"
                path.write_text(json.dumps(document), encoding="utf-8")
                with (
                    patch.object(runner, "structured", side_effect=[
                        (copy.deepcopy(reply), json.dumps(reply)) for reply in replies
                    ]) as requests,
                    patch.object(runner, "emit") as events,
                ):
                    state = runner.execute(path, {"input": "SOURCE Ω"}, options={
                        "attempts": 2, "inspector_model": "gpt-5.6-luna",
                    })
                results.append((state, requests.call_args_list, events.call_args_list))
        self.assertEqual(results[0], results[1])
        state, requests, events = results[0]
        self.assertEqual(state["output"], "FINAL")
        self.assertTrue(state["accepted_at_limit"])
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[1].args[0], "Inspect DRAFT; visit 1.")
        self.assertEqual(requests[2].args[0], "Inspect REPAIR 1; visit 2.")
        self.assertEqual(requests[3].args[0], "Format REPAIR 2.")
        self.assertEqual(requests[1].kwargs["model"], "gpt-5.6-luna")
        self.assertEqual(events[-1].kwargs["text"], "Accepted after 2 visits")

    def test_rejects_invalid_or_ambiguous_response_descriptions(self):
        descriptions = [
            None, 7, "object", [], ["pass", 7],
            {"type": "string"}, {"fields": []},
            {"fields": {"child": "string"}, "items": "string"},
            {"fields": {"child": "string"}, "unknown": True},
            {"items": "string", "minItems": -1},
            {"items": "string", "maxItems": 1.5},
            {"items": "string", "minItems": True},
            {"items": "string", "minItems": 2, "maxItems": 1},
            {"items": "string", "maxLength": 3},
        ]
        for description in descriptions:
            with self.subTest(description=description), self.assertRaises(ValueError):
                self.load_document(self.response_document({"value": description}))
        for response in ("string", ["text"], None):
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.load_document(self.response_document(response))

    def test_response_and_schema_cannot_both_be_supplied(self):
        document = self.response_document({"text": "string"})
        document["nodes"]["worker"]["schema"] = {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        }
        with self.assertRaises(ValueError):
            self.load_document(document)

    def test_recursive_response_alias_is_rejected_as_invalid_yaml_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recursive.yaml"
            path.write_text(
                "prompts: {work: 'Work.'}\n"
                "nodes:\n"
                "  worker:\n"
                "    run: structured\n"
                "    prompt: work\n"
                "    response: {nested: &recursive {items: *recursive}}\n"
                "    next: end\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "recursive or too deeply nested"):
                runner.load_workflow(path)

    def test_omitted_goal_lifecycle_and_stages_use_the_existing_defaults(self):
        lifecycle = [
            "goal", "initial", "memory", "anchor", "reanchor", "continuation",
            "compaction", "repair", "failure", "failure_input",
        ]
        document = {
            "prompts": {name: "Lifecycle instruction." for name in lifecycle},
            "nodes": {
                "custom_goal": {
                    "run": "goal", "role": "author", "prompt": "main",
                    "task": "state.task", "marker": "[TASK]",
                    "resume": {"solution": "state.solution", "bugs": "state.bugs", "round": "state.round"},
                    "outcome": "result.outcome", "next": {"proof": "end", "failure": "end"},
                },
            },
        }
        document["prompts"]["main"] = "Work on [TASK]."
        loaded = self.load_document(document)["nodes"]["custom_goal"]
        self.assertEqual(loaded["lifecycle"], lifecycle)
        self.assertEqual(loaded["stages"], {"initial": "solve", "resume": "repair", "failure": "failure"})
        explicit = copy.deepcopy(document)
        explicit["nodes"]["custom_goal"]["lifecycle"] = lifecycle
        explicit["nodes"]["custom_goal"]["stages"] = {"initial": "start", "resume": "revise", "failure": "stop"}
        self.assertEqual(
            self.load_document(explicit)["nodes"]["custom_goal"]["stages"],
            {"initial": "start", "resume": "revise", "failure": "stop"},
        )

    def test_bundled_workflows_preserve_the_complete_original_graphs(self):
        # Frozen after reviewing the merged main-branch audit/recovery changes.
        # Hash every prompt, schema, action, condition, and transition together.
        expected = {
            "author_critic": ("74c0824ca3b4aed0c3d89eae80ed47889944f26fd59a88b430811d12aeec5299", ["author", "critic"]),
            "clean_up": ("ac32432cbb325614f9c1b1437328491bf688884939942b6bbb8d67d9dadfeb44", ["latex_editor"]),
        }
        for name, (fingerprint, node_order) in expected.items():
            with self.subTest(workflow=name):
                workflow = runner.load_workflow(runner.WORKFLOWS / f"{name}.yaml")
                canonical = json.dumps(workflow, sort_keys=True, ensure_ascii=False)
                self.assertEqual(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), fingerprint)
                self.assertEqual(list(workflow["nodes"]), node_order)


if __name__ == "__main__":
    unittest.main()
