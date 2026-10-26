"""Stage combinations make requests only for the selected proof operations."""

import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as runtime


ROOT = Path(runtime.__file__).parent
PATHS = [ROOT / "workflows/author_critic.yaml", ROOT / "workflows/clean_up.yaml"]
PROOF = "A complete proof from the author."
SUPPLIED = "An existing complete candidate supplied by the user."
LATEX = r"\documentclass{article}\begin{document}A proof.\end{document}"


class WorkflowStageSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.calls = []

    def author(self, *_args, **_kwargs):
        self.calls.append("author")
        yield {"outcome": "done", "output": PROOF}

    def structured(self, _prompt, _schema, stage, **_kwargs):
        self.calls.append(stage)
        report = ({"latex": LATEX} if stage == "final" else
                  {"verdict": "pass", "solution": self.candidate, "bugs": ""})
        return report, json.dumps(report)

    def execute(self, stages=None, *, initial=None, model=None, **settings):
        self.candidate = PROOF if stages is None or stages.get("author", True) else SUPPLIED
        options = {**settings, **({"workflow_stages": stages} if stages is not None else {})}
        with patch.object(runtime, "goal_session", side_effect=self.author) as author, \
                patch.object(runtime, "structured", side_effect=model or self.structured) as structured, \
                patch.object(runtime, "require_model_credentials") as credentials, \
                patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}) as compiler, \
                patch.object(runtime, "emit") as events:
            state = runtime.execute_workflows(
                PATHS, {"statement": "The exact statement.", "solution": SUPPLIED, **(initial or {})}, options)
        return state, author, structured, credentials, compiler, events

    def test_every_stage_combination_skips_disabled_requests_credentials_and_outputs(self):
        for review, author, critic, writing in itertools.product((False, True), repeat=4):
            if not any((review, author, critic, writing)):
                continue
            stages = dict(review=review, author=author, critic=critic, writing=writing)
            with self.subTest(stages=stages):
                self.calls.clear()
                for path in self.directory.iterdir():
                    if path.is_file():
                        path.unlink()
                models = {f"{name}_model": actual if enabled else "not-a-configured-model"
                          for name, enabled, actual in (
                              ("author", author, "gpt-6.1-sol"),
                              ("critic", critic, "gpt-6-astra"),
                              ("writer", writing, "gpt-6-sol"))}
                prompts = {name: "" for name, enabled in (("author", author), ("critic", critic), ("final", writing))
                           if not enabled}
                state, _, _, credentials, compiler, events = self.execute(stages, prompts=prompts, **models)
                expected = (["author"] if author else []) + (["critic"] if critic else []) + (["final"] if writing else [])
                self.assertEqual(self.calls, expected)
                expected_models = {value for name, value in models.items() if value != "not-a-configured-model"}
                self.assertEqual({call.args[0] for call in credentials.call_args_list}, expected_models)
                self.assertEqual(compiler.call_count, int(writing))
                self.assertEqual((self.directory / "final.tex").exists(), writing)
                self.assertEqual((self.directory / runtime.FINAL_INPUT_FILENAME).exists(), writing)
                self.assertFalse(state.get("failed"))
                self.assertFalse(any(call.args[0] == "review_result" for call in events.call_args_list))
                if writing:
                    self.assertEqual(state["output"], LATEX)
                elif author or critic:
                    self.assertEqual(state["output"], PROOF if author else SUPPLIED)
                    terminal = [call.kwargs for call in events.call_args_list if call.args[0] == "final_result"]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0]["resultKind"], "proof")

    def test_author_then_writing_preserves_the_author_candidate_without_critic_calls(self):
        state, author, model, _, compiler, events = self.execute({"critic": False})
        author.assert_called_once()
        self.assertEqual([call.args[2] for call in model.call_args_list], ["final"])
        self.assertIn(PROOF, model.call_args.args[0])
        compiler.assert_called_once()
        self.assertEqual(state["output"], LATEX)
        self.assertFalse(any(call.kwargs.get("label") == "Critic approved" for call in events.call_args_list))
        saved = json.loads((self.directory / runtime.FINAL_INPUT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual((saved["statement"], saved["solution"]), ("The exact statement.", PROOF))

    def test_writing_without_author_or_critic_saves_exact_input_before_request(self):
        def model(prompt, _schema, stage, **_kwargs):
            self.assertEqual(stage, "final")
            self.assertIn("EXACT STATEMENT (when supplied):\nThe exact statement.", prompt)
            self.assertIn(SUPPLIED, prompt)
            saved = json.loads((self.directory / runtime.FINAL_INPUT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual((saved["statement"], saved["solution"]), ("The exact statement.", SUPPLIED))
            raise runtime.Error("Stopped while writing")

        with self.assertRaisesRegex(runtime.Error, "Stopped while writing"):
            self.execute({"author": False, "critic": False}, model=model)

    def test_disabled_critic_ignores_invalid_instruction_and_input_prompt_overrides(self):
        state, author, model, _, compiler, _ = self.execute(
            {"critic": False, "writing": False}, prompts={"critic": "", "critic_input": None})
        author.assert_called_once()
        model.assert_not_called()
        compiler.assert_not_called()
        self.assertEqual(state["output"], PROOF)
        self.assertEqual(runtime._node_prompt_names({"instructions": "critic"}), {"critic"})

    def test_critic_without_author_rejects_once_and_never_enters_writing(self):
        report = {"verdict": "reject", "solution": SUPPLIED + " Safe repairs.", "bugs": "Step 3 remains unproved."}
        state, author, model, _, compiler, events = self.execute(
            {"author": False}, model=lambda *_args, **_kwargs: (report, json.dumps(report)))
        author.assert_not_called()
        model.assert_called_once()
        compiler.assert_not_called()
        self.assertTrue(state["failed"])
        self.assertTrue(state["revisionRequired"])
        self.assertEqual(state["output"], report["solution"])
        self.assertFalse((self.directory / "final.tex").exists())
        self.assertFalse(any(call.kwargs.get("label") == "Critic approved" for call in events.call_args_list))
        terminal = [call.kwargs for call in events.call_args_list if call.args[0] == "failure_result"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual((terminal[0]["resultKind"], terminal[0]["bugs"]), ("needs-revision", report["bugs"]))

    def test_critic_without_author_rechecks_edited_passes_and_stops_at_the_round_limit(self):
        count = 0

        def model(_prompt, _schema, stage, **_kwargs):
            nonlocal count
            self.assertEqual(stage, "critic")
            count += 1
            report = {"verdict": "pass", "solution": SUPPLIED + " Repair." * count, "bugs": ""}
            return report, json.dumps(report)

        state, author, model_calls, _, compiler, _ = self.execute(
            {"author": False, "writing": False}, model=model, critic_rounds=3)
        author.assert_not_called()
        compiler.assert_not_called()
        self.assertEqual(model_calls.call_count, 3)
        self.assertEqual(state["output"], SUPPLIED + " Repair." * 3)
        self.assertFalse(state.get("failed"))

    def test_input_solution_option_supplies_the_critic_candidate(self):
        supplied = "A different complete user proof."
        seen = []

        def model(prompt, _schema, _stage, **_kwargs):
            seen.append(prompt)
            report = {"verdict": "pass", "solution": supplied, "bugs": ""}
            return report, json.dumps(report)

        state, author, _, _, compiler, _ = self.execute(
            {"author": False, "writing": False}, input_solution=supplied, model=model)
        self.assertIn(supplied, seen[0])
        self.assertEqual(state["output"], supplied)
        author.assert_not_called()
        compiler.assert_not_called()

    def test_a_file_submission_is_captured_before_skipping_the_critic(self):
        complete = "The complete author proof loaded from its submission file."

        def file_author(*_args, **_kwargs):
            yield {"outcome": "done", "output": "PROOF_FILE: proof.md"}

        with patch.object(runtime.proof_handoff, "resolve_author_proof") as capture:
            capture.return_value.text = complete
            self.author = file_author
            state, _, model, _, _, _ = self.execute({"critic": False})
        capture.assert_called_once_with("PROOF_FILE: proof.md", Path.cwd())
        self.assertIn(complete, model.call_args.args[0])
        self.assertEqual(state["solution"], complete)

    def test_unannotated_generic_workflows_keep_their_existing_execution(self):
        source = self.directory / "custom.yaml"
        source.write_text("""nodes:
  custom:
    run: structured
    role: author
    prompt: task
    response: {value: string}
    after:
      - set: {output: result.value}
    next: end
prompts:
  task: A custom task.
""")
        with patch.object(runtime, "require_model_credentials"), patch.object(runtime, "structured", return_value=(
                {"value": "Custom output"}, '{"value":"Custom output"}')) as model, patch.object(runtime, "emit") as events:
            state = runtime.execute(source, {}, options={"workflow_stages": {"author": False, "critic": False}})
        model.assert_called_once()
        self.assertEqual(state["output"], "Custom output")
        self.assertFalse(any(call.args[0] == "final_result" for call in events.call_args_list))

    def test_all_stages_remain_enabled_by_default(self):
        state, author, model, _, compiler, _ = self.execute()
        self.assertEqual(self.calls, ["author", "critic", "final"])
        self.assertEqual(state["output"], LATEX)
        author.assert_called_once()
        self.assertEqual(model.call_count, 2)
        compiler.assert_called_once()

    def test_invalid_selections_fail_before_credentials_or_requests(self):
        invalid = [dict.fromkeys(runtime.WORKFLOW_STAGES, False), {"author": "false"}, {"unknown": True}, []]
        for stages in invalid:
            with self.subTest(stages=stages), patch.object(runtime, "require_model_credentials") as credentials, \
                    patch.object(runtime, "goal_session") as author, patch.object(runtime, "structured") as model:
                with self.assertRaises(ValueError):
                    runtime.execute_workflows(PATHS, {"statement": "S"}, {"workflow_stages": stages})
                credentials.assert_not_called()
                author.assert_not_called()
                model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
