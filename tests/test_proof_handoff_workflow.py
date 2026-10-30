"""Behavioral author-file/critic snapshot integration tests (no model calls)."""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from tests import critic_doubles
from test_workflow_runner import workspace, review_report, ROOT


class ProofHandoffWorkflowTests(unittest.TestCase):
    def test_resume_resolves_file_in_original_author_workspace(self):
        with workspace(), tempfile.TemporaryDirectory() as author:
            Path(author, "proof.md").write_text("Original workspace proof")
            node = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
            state = {"statement": "Task", "solution": "PROOF_FILE: proof.md"}
            files = runtime._prepare_node_files(node, state, 1, author)
            self.assertEqual(dict(files)["proof.md"], "Original workspace proof")

    def test_file_submission_is_captured_before_checkpoint_and_review(self):
        proof = "# Proof\n\nFor every integer n, n + 0 = n.\n"
        with workspace() as directory:
            (directory / "proof.md").write_text(proof)
            state = {"statement": "Prove the additive identity.", "solution": "PROOF_FILE: proof.md"}

            def review(prompt, schema, stage, **settings):
                # The critic reads proof.md; the proof is not repeated in its prompt.
                self.assertNotIn(proof, prompt)
                self.assertEqual((directory / "saved-candidate.md").read_text(), proof)
                files = dict(settings["input_files"])
                self.assertEqual(files["proof.md"], proof)
                self.assertEqual(files["statement.md"], state["statement"])
                manifest = json.loads(files["manifest.json"])
                self.assertEqual(manifest["source"]["proof_file"], "proof.md")
                self.assertEqual(manifest["files"]["proof.md"]["sha256"], hashlib.sha256(proof.encode()).hexdigest())
                # A later author edit cannot change the reviewed bytes.
                (directory / "proof.md").write_text("Unsubmitted replacement")
                self.assertEqual(dict(settings["input_files"])["proof.md"], proof)
                report = review_report(proof)
                return report, json.dumps(report)

            with patch.object(runtime, "structured", side_effect=critic_doubles.coordinator(review)) as model:
                result = runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"], state, {"start_node": "critic"})
            self.assertEqual([call.kwargs["request_label"] for call in model.call_args_list].count("Critic coordinator"), 1)
            self.assertEqual(result["output"], proof.strip())

    def test_rejection_returns_actual_proof_and_next_submission_is_fresh(self):
        with workspace() as directory:
            feedback, reviewed = [], []
            def author(*args, **kwargs):
                (directory / "proof.md").write_text("Candidate one")
                feedback.append((yield {"outcome": "done", "output": "PROOF_FILE: proof.md"}))
                (directory / "proof.md").write_text("Candidate two with the gap repaired")
                yield {"outcome": "done", "output": "PROOF_FILE: proof.md"}
            def review(prompt, schema, stage, **settings):
                candidate = dict(settings["input_files"])["proof.md"]
                reviewed.append(candidate)
                report = review_report(candidate, "reject" if len(reviewed) == 1 else "pass")
                return report, json.dumps(report)
            with patch.object(runtime, "goal_session", side_effect=author), patch.object(runtime, "structured", side_effect=critic_doubles.coordinator(review)):
                result = runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"], {"statement": "Task"})
            self.assertEqual(reviewed, ["Candidate one", "Candidate two with the gap repaired"])
            self.assertEqual(feedback[0]["solution"], "Candidate one")
            self.assertEqual(feedback[0]["bugs"], "A gap remains.")
            self.assertEqual(result["output"], reviewed[-1])

    def test_critic_edits_are_reviewed_without_reloading_stale_author_file(self):
        with workspace() as directory:
            (directory / "proof.md").write_text("Original candidate")
            reviewed = []
            def review(prompt, schema, stage, **settings):
                reviewed.append(dict(settings["input_files"])["proof.md"])
                report = review_report("Critic corrected candidate")
                return report, json.dumps(report)
            with patch.object(runtime, "structured", side_effect=critic_doubles.coordinator(review)):
                runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"],
                    {"statement": "Task", "solution": "PROOF_FILE: proof.md"}, {"start_node": "critic"})
            self.assertEqual(reviewed, ["Original candidate", "Critic corrected candidate"])

    def test_invalid_file_never_reaches_critic_or_overwrites_checkpoint(self):
        for reference in ("missing.md", "../outside.md", "input.md"):
            with self.subTest(reference=reference), workspace() as directory:
                (directory / "proof.md").write_text("Stale file must not be selected")
                (directory / "saved-candidate.md").write_text("Prior valid checkpoint")
                with patch.object(runtime, "structured") as model, self.assertRaises(runtime.Error):
                    runtime.criticize("Task", "PROOF_FILE: " + reference, 1)
                model.assert_not_called()
                self.assertEqual((directory / "saved-candidate.md").read_text(), "Prior valid checkpoint")

    def test_snapshot_process_gets_readable_files_and_shell_only_for_file_requests(self):
        with workspace():
            node = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
            files = runtime._prepare_node_files(node, {"statement": "Task", "solution": "Proof"}, 1)
            paths = []
            def process(*args, **kwargs):
                path = kwargs["workspace"]
                paths.append(path)
                self.assertEqual((path / "proof.md").read_text(), "Proof")
                self.assertEqual((path / "statement.md").read_text(), "Task")
                self.assertIn("shell_tool", kwargs["features"])
                self.assertEqual((path / "proof.md").stat().st_mode & 0o777, 0o400)
                return '{"ok":true}'
            with tempfile.TemporaryDirectory() as root, patch.object(runtime, "retrieval_policy", return_value=None), patch.object(runtime, "retrieval_environment", return_value={}), patch.object(runtime, "_run_structured_process", side_effect=process), patch.object(runtime.tempfile, "mkdtemp", return_value=root):
                result = runtime.run_structured_attempt("Task", {}, "critic", runtime.MODEL, runtime.EFFORT,
                    runtime.DEFAULT_SPEED, runtime.DEFAULT_REASONING_SUMMARY, input_files=files)
            self.assertEqual(result, '{"ok":true}')

    def test_structured_retries_keep_same_snapshot_and_log_file_instructions(self):
        files = (("proof.md", "Original"), ("statement.md", "Task"), ("manifest.json", "{}"))
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        with workspace(), patch.object(runtime, "retrieval_policy", return_value=None), patch.object(runtime, "require_model_credentials"), patch.object(runtime, "run_structured_attempt", side_effect=["", '{"ok":true}']) as attempt, patch.object(runtime, "emit") as emit, patch.object(runtime, "MODEL_RETRY_SECONDS", 0):
            runtime.structured("Review", schema, "critic", input_files=files)
        self.assertEqual(attempt.call_count, 2)
        for call in attempt.call_args_list:
            self.assertIs(call.kwargs["input_files"], files)
            self.assertIn("Read both files completely", call.args[0])
        inputs = [call.kwargs["text"] for call in emit.call_args_list if call.args[0] == "request"]
        self.assertEqual(len(inputs), 2)
        self.assertTrue(all("proof.md" in value for value in inputs))
