"""Simple and managed runs retain their mode across author lifecycle and restart."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import workflow_runner as runtime
from ui import server


class FileManagementTests(unittest.TestCase):
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

    def test_home_default_and_mode_specific_overrides(self):
        self.assertFalse(server.empty_state()["fileManagement"])
        overrides = {"author": "Managed [STATEMENT]", "author_simple": "Simple [STATEMENT]"}
        for managed, expected in ((False, "Simple"), (True, "Managed")):
            app = self.manager.start_direct_job({
                "statement": "Exact statement", "fileManagement": managed,
                "promptOverrides": overrides,
                "researchAudits": {"models": ["gpt-6-astra", "none", "none"], "intervalHours": 1},
            })
            self.assertEqual(app.state["fileManagement"], managed)
            self.assertEqual(app.state["authorPrompt"], f"{expected} [STATEMENT]")
            self.assertEqual(app.state["researchAudits"]["models"][0], "gpt-6-astra" if managed else "none")
            args = app._proof_options_locked()
            self.assertIn("file_management=" + json.dumps(managed), args)
            restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
            self.assertEqual(restored.state["fileManagement"], managed)
            self.assertEqual(restored.state["authorPrompt"], app.state["authorPrompt"])

    def test_invalid_mode_is_rejected_before_creating_a_run(self):
        for invalid in ("false", 0, None):
            with self.assertRaisesRegex(ValueError, "File management"):
                self.manager.start_direct_job({"statement": "Task", "fileManagement": invalid})
        self.assertEqual(list(self.runs.iterdir()), [])

    def test_simple_lifecycle_never_requests_managed_records(self):
        workflow = runtime.builtin_workflow("author_critic")
        for managed in (False, True):
            options = {"file_management": managed}
            with patch.object(runtime, "require_model_credentials"):
                prompts = runtime.prepare(workflow, options)
            for name in ("author", "goal", "continuation", "compaction", "resume", "repair"):
                self.assertEqual(prompts[name], workflow["prompts"][name if managed else name + "_simple"])
                if not managed:
                    self.assertNotIn("INITIAL_PROMPT.md", prompts[name])
                    self.assertNotIn("AUDITS/", prompts[name])
            self.assertEqual(prompts["critic"], workflow["prompts"]["critic"])
            self.assertEqual(prompts["author"].count("[STATEMENT]"), 1)
        with patch.object(runtime, "require_model_credentials"):
            prompts = runtime.prepare(workflow, {"file_management": False, "prompts": {"author": "Custom [STATEMENT]"}})
        self.assertEqual(prompts["author"], "Custom [STATEMENT]")
        self.assertEqual(prompts["resume"], workflow["prompts"]["resume_simple"])

    def test_fresh_simple_run_and_saved_continuation_use_simple_lifecycle(self):
        app = self.manager.start_direct_job({"statement": "Exact statement"})
        app.trace_file.write_text("")
        self.assertEqual(app.state["authorPrompt"], server.default_prompts()["author_simple"])
        saved = server.saved_research_source(app.run_dir)
        self.assertFalse(saved["settings"]["fileManagement"])
        continued = self.manager.start_saved_research_job(app.run_dir)
        self.assertFalse(continued.state["fileManagement"])
        self.assertEqual(continued.state["goalWorkspace"], str(app.run_dir.resolve()))
        self.assertIn("file_management=false", continued._proof_options_locked())

    def test_legacy_saved_run_keeps_managed_mode(self):
        directory = self.runs / "legacy"
        directory.mkdir()
        (directory / "transcript.jsonl").write_text("")
        restored = server.restore_saved_app(server.App(directory / "transcript.jsonl", self.runs))
        self.assertTrue(restored.state["fileManagement"])

    def test_simple_recovery_without_saved_prompt_uses_simple_default(self):
        source = self.manager.start_direct_job({"statement": "Exact statement"})
        (source.run_dir / "prompts/author.txt").unlink()
        (source.run_dir / runtime.SAVED_CANDIDATE_FILENAME).write_text("Candidate")
        self.assertEqual(server.saved_critic_source(source.run_dir)["author_prompt"],
                         server.default_prompts()["author_simple"])
        continued = server.App(runs=self.runs)
        continued.start_direct_statement("Exact statement", continuation_source=source.run_dir)
        self.assertFalse(continued.state["fileManagement"])
        self.assertEqual(continued.state["authorPrompt"], server.default_prompts()["author_simple"])
        critic = server.App(runs=self.runs)
        with patch.object(critic, "_launch_critic_resume_locked", return_value=(object(), object())):
            critic.start_critic_resume("Exact statement", "Candidate", source_run=source.run_dir)
        self.assertFalse(critic.state["fileManagement"])
        self.assertEqual(critic.state["authorPrompt"], server.default_prompts()["author_simple"])

    def test_existing_job_cannot_switch_file_policy_during_review_retry(self):
        with self.assertRaisesRegex(ValueError, "new job"):
            server.web_prompt_options({"fileManagement": False}, ("author",),
                                      {"fileManagement": True, "authorPrompt": "Managed [STATEMENT]"})

    def test_prepared_workspace_is_discoverable_and_starts_in_place(self):
        directory = self.runs / "prepared"
        directory.mkdir()
        statement = "Exact prepared statement"
        (directory / "checked-statement.md").write_text("# Statement sent directly to the proof author\n\n" + statement)
        (directory / "draft.md").write_text("# Draft problem\n\n" + statement)
        (directory / "INITIAL_PROMPT.md").write_text("Preserved curated instructions")
        settings = server.App._workflow_options(file_management=True, include_review=False)
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
