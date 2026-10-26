"""One proof author prompt is used across launch, lifecycle, and restart."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import workflow_runner as runtime
from ui import server


class AuthorPromptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.manager = object.__new__(server.Server)
        self.manager.runs = self.runs
        self.manager.jobs = {}
        self.manager.jobs_lock = threading.RLock()
        self.manager.fixed_app = False
        for change in (patch.object(runtime, "verify_model_credentials"),
                       patch.object(server.App, "_spawn_worker"),
                       patch.object(server.App, "_launch_solver_locked", return_value=(object(), object()))):
            change.start()
            self.addCleanup(change.stop)

    def test_single_author_prompt_and_independent_audits_survive_restart(self):
        app = self.manager.start_direct_job({
            "statement": "Exact statement",
            "promptOverrides": {"author": "Custom [STATEMENT]"},
            "researchAudits": {"models": ["gpt-6-astra", "none", "none"], "intervalHours": 1},
        })
        self.assertEqual(app.state["authorPrompt"], "Custom [STATEMENT]")
        self.assertEqual(app.state["researchAudits"]["models"][0], "gpt-6-astra")
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["authorPrompt"], app.state["authorPrompt"])
        self.assertEqual(restored.state["researchAudits"], app.state["researchAudits"])

    def test_every_author_lifecycle_uses_the_same_proof_prompt_family(self):
        workflow = runtime.builtin_workflow("author_critic")
        with patch.object(runtime, "require_model_credentials"):
            prompts = runtime.prepare(workflow, {})
        for name in ("author", "goal", "continuation", "compaction", "resume", "repair"):
            self.assertEqual(prompts[name], workflow["prompts"][name])
            for record in ("INITIAL_PROMPT.md", "APPROACHES", "PROVED.md"):
                self.assertNotIn(record, prompts[name])
        self.assertEqual(prompts["author"].count("[STATEMENT]"), 1)
        self.assertFalse(any(name.endswith("_simple") for name in prompts))
        with patch.object(runtime, "require_model_credentials"):
            custom = runtime.prepare(workflow, {"prompts": {"author": "Custom [STATEMENT]"}})
        self.assertEqual(custom["author"], "Custom [STATEMENT]")
        self.assertEqual(custom["resume"], prompts["resume"])

    def test_new_run_and_saved_continuation_use_the_same_default(self):
        app = self.manager.start_direct_job({"statement": "Exact statement"})
        app.trace_file.write_text("")
        self.assertEqual(app.state["authorPrompt"], server.default_prompts()["author"])
        continued = self.manager.start_saved_research_job(app.run_dir)
        self.assertEqual(continued.state["goalWorkspace"], str(app.run_dir.resolve()))
        self.assertEqual(continued.state["authorPrompt"], app.state["authorPrompt"])

    def test_saved_run_without_prompt_uses_current_author_default(self):
        source = self.manager.start_direct_job({"statement": "Exact statement"})
        (source.run_dir / "prompts/author.txt").unlink()
        (source.run_dir / runtime.SAVED_CANDIDATE_FILENAME).write_text("Candidate")
        self.assertEqual(server.saved_critic_source(source.run_dir)["author_prompt"],
                         server.default_prompts()["author"])
        continued = server.App(runs=self.runs)
        continued.start_direct_statement("Exact statement", continuation_source=source.run_dir)
        self.assertEqual(continued.state["authorPrompt"], server.default_prompts()["author"])
        critic = server.App(runs=self.runs)
        with patch.object(critic, "_launch_critic_resume_locked", return_value=(object(), object())):
            critic.start_critic_resume("Exact statement", "Candidate", source_run=source.run_dir)
        self.assertEqual(critic.state["authorPrompt"], server.default_prompts()["author"])

    def test_prepared_workspace_is_discoverable_and_starts_in_place(self):
        directory = self.runs / "prepared"
        directory.mkdir()
        statement = "Exact prepared statement"
        (directory / "checked-statement.md").write_text("# Statement sent directly to the proof author\n\n" + statement)
        (directory / "draft.md").write_text("# Draft problem\n\n" + statement)
        (directory / "INITIAL_PROMPT.md").write_text("Preserved curated instructions")
        settings = server.App._workflow_options(include_review=False)
        settings.update(preparedRun=True, skipStatementReview=True)
        (directory / "job-settings.json").write_text(json.dumps(settings))
        jobs = server.restore_saved_jobs(self.runs)
        app = jobs["prepared"]
        self.assertEqual(app.state["phase"], "prepared")
        self.assertFalse((directory / "transcript.jsonl").exists())
        self.manager.jobs.update(jobs)
        self.assertEqual(self.manager.job_list()[0]["continueStoppedLabel"], "Start prepared run")

        def launch(actual):
            self.assertEqual(actual, statement)
            self.assertEqual(app.run_dir, directory)
            self.assertEqual(app.state["goalThreadId"], "")
            self.assertFalse(app.state["goalResume"])
            app.state["phase"] = "running"
            return object(), object()

        with patch.object(app, "_launch_solver_locked", side_effect=launch):
            started = self.manager.continue_stopped_job("prepared")
        self.assertIs(started, app)
        self.assertEqual(app.state["phase"], "running")
        self.assertFalse(app.state["preparedRun"])
        self.assertEqual((directory / "INITIAL_PROMPT.md").read_text(), "Preserved curated instructions")
        self.assertEqual(list(self.runs.iterdir()), [directory])


if __name__ == "__main__":
    unittest.main()
