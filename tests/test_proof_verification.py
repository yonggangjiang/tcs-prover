"""A changed or unresolved proof must never cross the publication boundary."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as runtime


class ProofVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.events = []
        emitter = patch.object(runtime, "emit", side_effect=lambda *args, **fields: self.events.append((args, fields)))
        emitter.start()
        self.addCleanup(emitter.stop)

    def run_critic(self, candidate, replies, **options):
        def model(prompt, schema, stage, **settings):
            result = next(replies)
            runtime.validate_json_schema(result, schema)
            return result, json.dumps(result)

        with patch.object(runtime, "structured", side_effect=model) as calls:
            state = runtime.execute_workflows(
                [runtime.WORKFLOWS / "author_critic.yaml"],
                {"statement": "Exact statement", "solution": candidate},
                {"start_node": "critic", **options},
            )
        return state, calls

    def test_whitespace_changes_are_reviewed_as_part_of_exact_candidate(self):
        original = "First line\n  indented second line\n"
        changed = "First line\nindented second line"
        report = {"verdict": "pass", "solution": changed, "bugs": ""}
        state, calls = self.run_critic(original, iter([report, report]), critic_rounds=2)
        self.assertEqual(calls.call_count, 2)
        self.assertTrue(state["proof_verified"])
        self.assertEqual(state["output"], changed)
        self.assertEqual((self.directory / "saved-candidate.md").read_text(), changed)

    def test_exact_accepted_text_is_not_stripped_after_verification(self):
        original = "  Complete argument.\n\n"
        report = {"verdict": "pass", "solution": original, "bugs": ""}
        state, calls = self.run_critic(original, iter([report]))
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(state["output"], original)
        self.assertTrue(state["proof_verified"])

    def test_edit_at_last_round_never_calls_writer_when_author_cannot_finish(self):
        report = {"verdict": "pass", "solution": "Revised argument", "bugs": ""}

        def author(*args, **settings):
            self.assertIn("has not received an unchanged acceptance", settings["initial_instruction"])
            yield {"outcome": "failure", "output": "Missing bridge"}

        with patch.object(runtime, "structured", return_value=(report, json.dumps(report))) as calls, \
                patch.object(runtime, "goal_session", side_effect=author), \
                patch.object(runtime, "_run_command") as compiler:
            state = runtime.execute_workflows(
                [runtime.WORKFLOWS / "author_critic.yaml", runtime.WORKFLOWS / "clean_up.yaml"],
                {"statement": "Task", "solution": "Original", "proof_verified": True, "output": "Old output"},
                {"start_node": "critic", "critic_rounds": 1},
            )
        self.assertEqual(calls.call_count, 1)
        compiler.assert_not_called()
        self.assertTrue(state["failed"])
        self.assertFalse(state["proof_verified"])
        self.assertEqual(state["output"], "")
        self.assertFalse(any(fields.get("label") == "Critic approved" for _, fields in self.events))

    def test_writer_math_objection_stops_before_compilation(self):
        report = {"verdict": "unresolved", "latex": "% Candidate has a gap", "bugs": "Lemma 2 assumes independent adaptive samples."}
        with patch.object(runtime, "structured", return_value=(report, json.dumps(report))) as calls, \
                patch.object(runtime, "_run_command") as compiler:
            state = runtime.execute_workflows(
                [runtime.WORKFLOWS / "clean_up.yaml"],
                {"solution": "Candidate", "proof_verified": True, "output": "Earlier output"},
            )
        self.assertEqual(calls.call_count, 1)
        compiler.assert_not_called()
        self.assertTrue(state["failed"])
        self.assertFalse(state["proof_verified"])
        self.assertEqual(state["output"], "")
        self.assertEqual(state["editing_report"]["bugs"], report["bugs"])
        self.assertEqual((self.directory / "latex-source.tex").read_text(), report["latex"])
        self.assertFalse((self.directory / "final.tex").exists())
        self.assertFalse(any(args[0] == "final_result" for args, _ in self.events))

    def test_writer_cannot_claim_preserved_with_nonempty_objections(self):
        report = {"verdict": "preserved", "latex": "Document", "bugs": "A proof gap remains."}
        with patch.object(runtime, "structured", return_value=(report, json.dumps(report))), \
                patch.object(runtime, "_run_command") as compiler:
            with self.assertRaises(runtime.Error):
                runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"solution": "Candidate"})
        compiler.assert_not_called()

    def test_python_wrappers_report_author_or_editor_failure_instead_of_empty_success(self):
        wrappers = (
            lambda: runtime.run_goal("Author instructions", "Statement"),
            lambda: runtime.audit_candidate("Statement", "Candidate"),
            lambda: runtime.finalize("Statement", "Candidate"),
            lambda: runtime.polish("Candidate"),
        )
        failures = (
            {"failure_reason": "Author could not prove the bridge.",
             "editing_report": {"bugs": "An older editorial objection."}},
            {"editing_report": {"bugs": "The final proof has a missing dependency."}},
        )
        for failure in failures:
            expected = failure.get("failure_reason") or failure["editing_report"]["bugs"]
            def fail(paths, state, options):
                state.update(failure, failed=True, output="")
                return state
            for wrapper in wrappers:
                with self.subTest(expected=expected, wrapper=wrapper), \
                        patch.object(runtime, "execute_workflows", side_effect=fail):
                    with self.assertRaises(runtime.Error) as caught:
                        wrapper()
                    self.assertEqual(str(caught.exception), expected)

    def test_empty_failure_uses_explanatory_fallback(self):
        self.assertEqual(runtime.workflow_failure_message({"failed": True, "output": ""}),
                         "Workflow stopped without a verified result.")


if __name__ == "__main__":
    unittest.main()
