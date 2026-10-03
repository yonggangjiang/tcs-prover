"""Critic-only runs of both author/critic workflows; no model calls."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import workflow_runner as runtime
from test_workflow_runner import workspace

ROOT = Path(runtime.__file__).parent
WORKFLOWS = {"author_critic": ROOT / "workflows/author_critic.yaml",
             "author_critic_cheap": ROOT / "workflows/author_critic_cheap.yaml"}
CLEAN_UP = ROOT / "workflows/clean_up.yaml"
PROOF = "Every allowed case follows by the explicit argument supplied here."
LATEX = r"\documentclass{article}\begin{document}An explicit argument.\end{document}"


def verdict(workflow, kind, solution=PROOF, bugs=""):
    """A critic answer in the response format of the given workflow."""
    if workflow == "author_critic_cheap":
        return {"verdict": "reject" if kind == "reject" else "pass",
                "edits": "applied" if kind == "fix" else "none",
                "solution": solution if kind == "fix" else "", "bugs": bugs}
    return {"verdict": "reject" if kind == "reject" else "pass", "solution": solution, "bugs": bugs}


class CriticOnlyTests(unittest.TestCase):
    def run_critic_only(self, workflow, answers):
        answers, calls, events = list(answers), [], []

        def model(prompt, schema, stage, **settings):
            value = {"latex": LATEX} if "latex" in schema["properties"] else answers.pop(0)
            calls.append("final" if "latex" in schema["properties"] else "critic")
            runtime.validate_json_schema(value, schema)
            return value, json.dumps(value)

        with workspace(), patch.object(runtime, "structured", side_effect=model), \
                patch.object(runtime, "goal_session") as author, \
                patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}), \
                patch.object(runtime, "emit", side_effect=lambda *args, **kwargs: events.append((args, kwargs))):
            state = runtime.execute_workflows(
                [WORKFLOWS[workflow], CLEAN_UP],
                {"statement": "Task", "solution": PROOF, "critic_only": True},
                {"start_node": "critic", "critic_rounds": 2})
        author.assert_not_called()
        return state, calls, events

    def test_a_rejection_ends_the_run_with_the_critic_report(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                state, calls, events = self.run_critic_only(
                    workflow, [verdict(workflow, "reject", bugs="Lemma 2 fails for n = 1.")])
                self.assertEqual(calls, ["critic"])
                self.assertTrue(state["failed"])
                self.assertEqual(state["output"], "The critic rejected the proof. Unresolved bugs:\n\nLemma 2 fails for n = 1.")
                failure = [kwargs for args, kwargs in events if args[0] == "failure_result"]
                self.assertEqual([item["output"] for item in failure], [state["output"]])
                self.assertTrue(any(args[0] == "critic_result" for args, _ in events))

    def test_a_pass_still_goes_on_to_the_latex_editor(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                state, calls, _ = self.run_critic_only(workflow, [verdict(workflow, "pass")])
                self.assertEqual(calls, ["critic", "final"])
                self.assertFalse(state.get("failed"))
                self.assertEqual(state["output"], LATEX)

    def test_a_fixed_proof_is_rechecked_before_the_latex_editor(self):
        fixed = PROOF + " With the missing case."
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow):
                state, calls, _ = self.run_critic_only(
                    workflow, [verdict(workflow, "fix", solution=fixed), verdict(workflow, "pass", solution=fixed)])
                self.assertEqual(calls, ["critic", "critic", "final"])
                self.assertEqual(state["solution"], fixed)


if __name__ == "__main__":
    unittest.main()
