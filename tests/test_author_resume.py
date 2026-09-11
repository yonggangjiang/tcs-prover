"""Author recovery reuses a workspace; only the LLM manages its notebooks."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ui import cli, server


class AuthorNotebookContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runs = Path(temporary.name) / "runs"
        self.source = self.runs / "interrupted-author"
        self.source.mkdir(parents=True)
        self.statement = "Establish the requested property."
        self.prompt = "My original custom author policy.\n\nSTATEMENT:\n[STATEMENT]"
        self.contents = {
            "INITIAL_PROMPT.md": self.prompt.replace("[STATEMENT]", self.statement),
            "APPROACHES.md": "# Approaches\n\nA001 failed: local choices exhaust a constrained resource.\n",
            "PROVED.md": "# Checked results\n\nThe two-task obstruction has been verified.\n",
        }
        for name, value in self.contents.items():
            (self.source / name).write_text(value, encoding="utf-8")
        (self.source / "checked-statement.md").write_text(
            "# Checked statement\n\n" + self.statement + "\n", encoding="utf-8",
        )
        (self.source / "prompts").mkdir()
        (self.source / "prompts" / "author.txt").write_text(self.prompt + "\n", encoding="utf-8")
        (self.source / server.JOB_SETTINGS_FILENAME).write_text(
            json.dumps({"thinkingHours": 2.75, "criticRounds": 3}), encoding="utf-8",
        )
        records = [
            {"kind": "status", "stage": "solve", "label": "Goal started", "threadId": "root-one"},
            {"kind": "status", "stage": "repair", "label": "Goal resumed", "threadId": "root-two"},
            {"kind": "status", "stage": "repair", "root": False, "threadId": "subagent"},
            {"kind": "status", "stage": "critic", "threadId": "critic-thread"},
        ]
        (self.source / "transcript.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
        )
        credentials = mock.patch.object(server.runtime, "verify_model_credentials")
        credentials.start()
        self.addCleanup(credentials.stop)
        workers = mock.patch.object(server.App, "_spawn_worker")
        workers.start()
        self.addCleanup(workers.stop)

    def test_workspace_and_original_prompt_restore_without_notebook_access(self):
        source_before = {str(path.relative_to(self.source)): path.read_bytes()
                         for path in self.source.rglob("*") if path.is_file()}
        app = server.App(runs=self.runs)

        def launch(statement):
            self.assertEqual(statement, self.statement)
            self.assertEqual(app.state["authorPrompt"], self.prompt)
            for name in self.contents:
                self.assertFalse((app.run_dir / name).exists())
            self.assertFalse((app.run_dir / "continuation-memory").exists())
            options = app._proof_options_locked()
            setting = options[options.index("--set") + 1]
            self.assertEqual(setting.split("=", 1)[0], "goal_thread_id")
            self.assertEqual(json.loads(setting.split("=", 1)[1]), "root-two")
            settings = dict(options[index + 1].split("=", 1)
                            for index, argument in enumerate(options) if argument == "--set")
            self.assertEqual(json.loads(settings["goal_cwd"]), str(self.source.resolve()))
            self.assertTrue(json.loads(settings["goal_resume"]))
            return object(), object()

        original_read, original_write = Path.read_text, Path.write_text
        def guarded_read(path, *args, **kwargs):
            self.assertNotIn(path.name, self.contents, "UI must not read LLM memory")
            return original_read(path, *args, **kwargs)
        def guarded_write(path, *args, **kwargs):
            self.assertNotIn(path.name, self.contents, "UI must not write LLM memory")
            return original_write(path, *args, **kwargs)
        with mock.patch.object(app, "_launch_solver_locked", side_effect=launch):
            with mock.patch.object(Path, "read_text", guarded_read), mock.patch.object(Path, "write_text", guarded_write):
                app.start_direct_statement(self.statement, continuation_source=self.source, stopped_stage="solve")
        self.assertEqual(
            {str(path.relative_to(self.source)): path.read_bytes()
             for path in self.source.rglob("*") if path.is_file()}, source_before,
        )

    def test_network_quota_time_and_manual_stops_all_offer_author_continuation(self):
        for reason, manual in (("Network connection lost", False), ("Quota exhausted", False),
                               ("Workflow time limit reached", False), ("Stopped", True)):
            with self.subTest(reason=reason):
                app = server.App(runs=self.runs)
                app.state.update(phase="done", stage="solve", error=reason, manuallyStopped=manual)
                plan = server.Server._stopped_continuation_plan(app)
                self.assertEqual(plan["action"], "author")
                self.assertEqual(plan["label"], "Continue proof author")

    def test_quota_failure_during_critic_resume_repair_keeps_critic_recovery(self):
        for name in self.contents:
            (self.source / name).unlink()
        (self.source / server.runtime.SAVED_CANDIDATE_FILENAME).write_text(
            "The complete proof sent to the critic.", encoding="utf-8",
        )
        app = server.App(runs=self.runs)
        app.state.update(phase="error", stage="repair", problemMode="critic-resume",
                         error="Quota exhausted", manuallyStopped=False)
        plan = server.Server._stopped_continuation_plan(app)
        self.assertEqual(plan["action"], "critic")
        self.assertEqual(plan["label"], "Continue from critic")
        (self.source / server.runtime.SAVED_CANDIDATE_FILENAME).unlink()
        self.assertIsNone(server.Server._stopped_continuation_plan(app))

    def test_new_root_session_is_saved_in_existing_job_settings(self):
        app = server.App(runs=self.runs)
        app._new_run(self.statement)
        app._save("checked-statement.md", "# Checked statement\n\n" + self.statement + "\n")
        process, token = mock.Mock(), object()
        records = [
            {"kind": "status", "stage": "solve", "label": "Goal resumed", "threadId": "resumed-root"},
            {"kind": "status", "stage": "solve", "root": False, "threadId": "wrong-subagent"},
            {"kind": "diagnostic", "stage": "solve", "text": "error: Network disconnected"},
        ]
        process.stdout = iter(json.dumps(record) + "\n" for record in records)
        process.wait.return_value = 1
        app.process, app.active_token = process, token
        app.state.update(phase="running", stage="solve", activeNode="author")
        app._read_output(process, token)
        settings = json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(settings["goalThreadId"], "resumed-root")
        self.assertEqual(server.saved_goal_thread_id(app.run_dir), "resumed-root")
        self.assertEqual(app.state["phase"], "paused")
        checkpoint = json.loads((app.run_dir / server.PAUSE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["state"]["statement"], self.statement)
        self.assertEqual(checkpoint["goalThreadId"], "resumed-root")
        self.assertFalse((app.run_dir / "author-session.json").exists())

    def test_saved_author_source_loads_from_ui_settings(self):
        source = server.saved_research_source(self.source)
        self.assertEqual(source["statement"], self.statement)
        self.assertEqual(source["settings"]["authorPrompt"], self.prompt + "\n")
        self.assertEqual(source["settings"]["thinkingHours"], 2.75)
        self.assertEqual(server.saved_goal_thread_id(self.source), "root-two")

    def test_missing_thread_id_still_reopens_workspace(self):
        (self.source / "transcript.jsonl").write_text("", encoding="utf-8")
        app = server.App(runs=self.runs)
        with mock.patch.object(app, "_launch_solver_locked", return_value=(object(), object())):
            app.start_direct_statement(self.statement, continuation_source=self.source)
        self.assertEqual(app.state["goalThreadId"], "")
        options = app._proof_options_locked()
        settings = dict(options[index + 1].split("=", 1)
                        for index, argument in enumerate(options) if argument == "--set")
        self.assertNotIn("goal_thread_id", settings)
        self.assertEqual(json.loads(settings["goal_cwd"]), str(self.source.resolve()))
        self.assertTrue(json.loads(settings["goal_resume"]))
        for name in self.contents:
            self.assertFalse((app.run_dir / name).exists())

    def test_continuation_chain_keeps_original_workspace(self):
        first = server.App(runs=self.runs)
        with mock.patch.object(first, "_launch_solver_locked", return_value=(object(), object())):
            first.start_direct_statement(self.statement, continuation_source=self.source)
        second = server.App(runs=self.runs)
        with mock.patch.object(second, "_launch_solver_locked", return_value=(object(), object())):
            second.start_direct_statement(self.statement, continuation_source=first.run_dir)
        self.assertEqual(second.state["goalWorkspace"], str(self.source.resolve()))
        settings = json.loads((second.run_dir / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(settings["goalWorkspace"], str(self.source.resolve()))

    def test_interruption_before_memory_files_exist_can_continue(self):
        for name in self.contents:
            (self.source / name).unlink()
        (self.source / "transcript.jsonl").write_text("", encoding="utf-8")
        app = server.App(runs=self.runs)
        app.state.update(phase="error", stage="solve", error="Network unavailable")
        self.assertEqual(server.Server._stopped_continuation_plan(app)["action"], "author")

    def test_continuation_rejects_changed_author_template(self):
        app = server.App(runs=self.runs)
        with mock.patch.object(app, "_launch_solver_locked") as launch:
            with self.assertRaisesRegex(ValueError, "original author prompt"):
                app.start_direct_statement(self.statement, continuation_source=self.source,
                                           author_prompt="A different policy. [STATEMENT]")
        launch.assert_not_called()

    def test_resume_author_cli_alias_preserves_saved_defaults(self):
        manager = mock.Mock()
        manager.start_saved_research_job.return_value.state = {"runId": "continued"}
        manager.serve_forever.side_effect = KeyboardInterrupt
        args = ["tcs-prover", "--resume-author", str(self.source), "--no-browser"]
        with mock.patch.object(cli.sys, "argv", args):
            with mock.patch.object(cli, "Server", return_value=manager):
                with mock.patch.object(cli.sys, "stdout", io.StringIO()):
                    self.assertEqual(cli.main(), 0)
        manager.start_saved_research_job.assert_called_once_with(self.source.resolve(), {})


if __name__ == "__main__":
    unittest.main()
