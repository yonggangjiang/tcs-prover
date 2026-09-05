"""Test the generic workflow entry point used to continue saved critic jobs."""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import workflow_runner as runner


@contextmanager
def working_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class SavedWorkflowResumeTests(unittest.TestCase):
    @staticmethod
    def report(solution, *, bugs="", fixed=False):
        return {
            "checks": [
                {"focus": f"audit {index}", "verdict": "fail" if bugs else "pass", "report": "Checked."}
                for index in range(3)
            ],
            "verdict": "reject" if bugs else "pass", "fixed": fixed,
            "solution": solution, "bugs": bugs,
            "memory_update": {
                "approach_family": "saved approach", "approach_result": "audited",
                "blocked_routes": [], "unresolved_obligations": [bugs] if bugs else [],
            },
        }

    def run_saved_critic(self, reports, author_revision=None, custom_repair=False):
        pending = deque(copy.deepcopy(reports))
        critic_inputs, final_inputs, closed = [], [], []

        def model(prompt, schema, stage, **options):
            if stage == "critic":
                critic_inputs.append(prompt)
                report = pending.popleft()
            elif stage == "final":
                final_inputs.append(prompt)
                report = {"latex": "FINAL LATEX"}
            else:
                raise AssertionError(f"Unexpected model stage: {stage}")
            return report, json.dumps(report)

        def goal_session(*args, **kwargs):
            if author_revision is None:
                raise AssertionError("A clean critic approval must skip the author.")
            try:
                yield {"outcome": "proof", "solution": author_revision}
            finally:
                closed.append(True)

        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            saved_state = workspace / "saved-state.json"
            saved_state.write_text(json.dumps({
                "statement": "EXACT STATEMENT", "solution": "SAVED CANDIDATE",
            }), encoding="utf-8")
            author_workflow = runner.WORKFLOWS / "author_critic.yaml"
            if custom_repair:
                document = copy.deepcopy(runner.builtin_workflow("author_critic"))
                document["prompts"]["my_repair"] = document["prompts"].pop("repair")
                author = document["nodes"]["author"]
                author["lifecycle"] = {key: key for key in author["lifecycle"]}
                author["lifecycle"]["repair"] = "my_repair"
                author_workflow = workspace / "custom-author.yaml"
                author_workflow.write_text(json.dumps(document), encoding="utf-8")
            with (
                working_directory(workspace),
                patch.object(runner.Path, "cwd", return_value=workspace),
                patch.object(runner, "configure_standard_streams"),
                patch.object(sys, "stdin", io.StringIO("")),
                patch.object(sys, "stdout", output),
                patch.object(sys, "stderr", errors),
                patch.object(runner, "structured", side_effect=model),
                patch.object(
                    runner, "_parallel_requests",
                    side_effect=lambda *args, **kwargs: copy.deepcopy(pending[0]["checks"]),
                ) as audits,
                patch.object(runner, "author_session", side_effect=goal_session) as author,
            ):
                code = runner.main([
                    str(author_workflow),
                    str(runner.WORKFLOWS / "clean_up.yaml"),
                    "--state-file", str(saved_state), "--start-node", "critic",
                    "--critic-rounds", "2",
                ])
            self.assertEqual(code, 0, errors.getvalue())
            final_input = json.loads((workspace / runner.FINAL_INPUT_FILENAME).read_text())
        self.assertFalse(pending, "Every scripted critic round must be consumed.")
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return code, critic_inputs, final_inputs, author.call_args_list, audits.call_count, events, final_input, closed

    def test_saved_clean_candidate_skips_author_and_runs_cleanup_once(self):
        code, critics, finals, authors, audits, events, saved, closed = self.run_saved_critic([
            self.report("APPROVED SAVED PROOF"),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(authors, [])
        self.assertEqual(closed, [])
        self.assertEqual(audits, 1)
        self.assertEqual(len(critics), 1)
        self.assertIn("CANDIDATE SOLUTION:\nSAVED CANDIDATE", critics[0])
        self.assertEqual(finals, [
            runner.FINAL_PROMPT + "\n\nSTATEMENT:\nEXACT STATEMENT"
            "\n\nLATEST SOLUTION:\nAPPROVED SAVED PROOF",
        ])
        self.assertEqual(saved["solution"], "APPROVED SAVED PROOF")
        self.assertTrue(any(event.get("kind") == "final_result" and event.get("output") == "FINAL LATEX" for event in events))

    def test_saved_rejection_recovers_exact_feedback_and_accepts_the_final_allowed_repair(self):
        code, critics, finals, authors, audits, events, saved, closed = self.run_saved_critic([
            self.report("SAFE PARTIAL FIX", bugs="EXACT MISSING LEMMA"),
            self.report("FIRST CRITIC REPAIR", fixed=True),
            self.report("SECOND CRITIC REPAIR", fixed=True),
        ], author_revision="AUTHOR REVISION", custom_repair=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(authors), 1)
        self.assertEqual(closed, [True])
        recovered_prompt, statement = authors[0].args[:2]
        self.assertEqual(statement, "EXACT STATEMENT")
        self.assertIn(runner.make_prompt(statement), recovered_prompt)
        self.assertIn("SAFE PARTIAL FIX", recovered_prompt)
        self.assertIn("EXACT MISSING LEMMA", recovered_prompt)
        self.assertEqual(audits, 3)
        self.assertEqual(len(critics), 3)
        for prompt, solution in zip(critics, ["SAVED CANDIDATE", "AUTHOR REVISION", "FIRST CRITIC REPAIR"]):
            self.assertIn(f"CANDIDATE SOLUTION:\n{solution}", prompt)
        self.assertEqual([event["label"] for event in events if event.get("kind") == "critic_result"], [
            "Critic round 1", "Critic round 1", "Critic round 2",
        ])
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0].endswith("LATEST SOLUTION:\nSECOND CRITIC REPAIR"))
        self.assertEqual(saved["statement"], statement)
        self.assertEqual(saved["solution"], "SECOND CRITIC REPAIR")
        self.assertTrue(any(event.get("label") == "Critic round limit accepted" for event in events))


if __name__ == "__main__":
    unittest.main()
