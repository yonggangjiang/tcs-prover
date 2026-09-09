"""Operational critic/final recovery is independent of author memory."""

import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from ui import cli, server


class OperationalContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runs = Path(self.temporary.name) / "runs"
        self.source = self.runs / "interrupted"
        self.source.mkdir(parents=True)
        self.statement = "Every task has the required property."
        (self.source / "checked-statement.md").write_text(
            "# Checked statement\n\n" + self.statement + "\n", encoding="utf-8",
        )
        (self.source / "transcript.jsonl").write_text("", encoding="utf-8")
        (self.source / "APPROACHES.md").write_text("Author-managed notes.", encoding="utf-8")
        credentials = mock.patch.object(server.runtime, "verify_model_credentials")
        credentials.start()
        self.addCleanup(credentials.stop)
        workers = mock.patch.object(server.App, "_spawn_worker")
        workers.start()
        self.addCleanup(workers.stop)

    def assert_no_memory_copy(self, destination):
        self.assertFalse((destination / "APPROACHES.md").exists())
        self.assertFalse((destination / "continuation-memory").exists())
        self.assertEqual((self.source / "APPROACHES.md").read_text(), "Author-managed notes.")

    def test_critic_preserves_operational_input_before_launch(self):
        app = server.App(runs=self.runs)

        def launch(statement, solution):
            self.assertEqual((statement, solution), (self.statement, "The proposed proof."))
            self.assert_no_memory_copy(app.run_dir)
            self.assertEqual((app.run_dir / server.runtime.SAVED_CANDIDATE_FILENAME).read_text(), solution + "\n")
            self.assertEqual((app.run_dir / server.runtime.CRITIC_AUDIT_CHECKPOINT_FILENAME).read_text(), '{"saved": "audit"}')
            self.assertEqual(app.state["goalWorkspace"], str(self.source.resolve()))
            return object(), object()

        with mock.patch.object(app, "_launch_critic_resume_locked", side_effect=launch) as launched:
            app.start_critic_resume(self.statement, "The proposed proof.", source_run=self.source,
                                    audit_checkpoint='{"saved": "audit"}')
        launched.assert_called_once()


    def test_final_preserves_operational_input_before_launch(self):
        app = server.App(runs=self.runs)

        def launch(statement, solution):
            self.assert_no_memory_copy(app.run_dir)
            saved = json.loads((app.run_dir / server.runtime.FINAL_INPUT_FILENAME).read_text())
            self.assertEqual(saved["statement"], self.statement)
            self.assertEqual(saved["solution"], "Reviewed proof.")
            return object(), object()

        with mock.patch.object(app, "_launch_saved_final_locked", side_effect=launch) as launched:
            app.start_final_resume(self.statement, "Reviewed proof.", continuation_source=self.source)
        launched.assert_called_once()


    def test_final_resume_preserves_saved_verifier_model_settings(self):
        app = server.App(runs=self.runs)
        with mock.patch.object(app, "_launch_saved_final_locked", return_value=(object(), object())):
            app.start_final_resume(
                self.statement, "Reviewed proof.", continuation_source=self.source,
                critic_model="gpt-5.6-sol", critic_effort="high",
            )
        options = app._final_options()
        self.assertEqual(options[options.index("--critic-model") + 1], "gpt-5.6-sol")
        self.assertEqual(options[options.index("--critic-effort") + 1], "high")


    def test_critic_accepts_explicit_external_run_directory(self):
        external_runs = Path(self.temporary.name) / "other-runs"
        app = server.App(runs=external_runs)
        with mock.patch.object(app, "_launch_critic_resume_locked", return_value=(object(), object())):
            app.start_critic_resume(self.statement, "Proof.", source_run=self.source)
        self.assert_no_memory_copy(app.run_dir)


    def test_running_and_successful_jobs_do_not_offer_research_restart(self):
        app = server.App(runs=self.runs)
        app.state.update(phase="running", stage="solve")
        self.assertIsNone(server.Server._stopped_continuation_plan(app))
        app.state.update(phase="done")
        (self.source / "final.tex").write_text("Verified output", encoding="utf-8")
        self.assertIsNone(server.Server._stopped_continuation_plan(app))


    def manager(self):
        manager = object.__new__(server.Server)
        manager.runs = self.runs
        manager.jobs = {}
        manager.jobs_lock = threading.RLock()
        return manager


    def test_saved_research_uses_saved_budget_and_explicit_overrides(self):
        (self.source / server.JOB_SETTINGS_FILENAME).write_text(
            json.dumps({"thinkingHours": 4.25, "criticRounds": 7}), encoding="utf-8",
        )
        manager = self.manager()
        with mock.patch.object(server.App, "_launch_solver_locked", return_value=(object(), object())):
            default = manager.start_saved_research_job(self.source)
            default.state["phase"] = "done"
            changed = manager.start_saved_research_job(self.source, {"thinkingHours": 2.5})
        self.assertEqual(default.state["thinkingHours"], 4.25)
        self.assertEqual(default.state["criticRounds"], 7)
        self.assertEqual(changed.state["thinkingHours"], 2.5)
        self.assertEqual(changed.state["criticRounds"], 7)


    def test_saved_research_cannot_duplicate_an_active_source_job(self):
        manager = self.manager()
        source_app = server.App(runs=self.runs)
        source_app.state["phase"] = "running"
        manager.jobs[self.source.name] = source_app
        with mock.patch.object(server.App, "_launch_solver_locked") as launched:
            with self.assertRaisesRegex(ValueError, "Stop the source"):
                manager.start_saved_research_job(self.source)
        launched.assert_not_called()

    def test_continuations_sharing_a_workspace_cannot_run_together(self):
        manager = self.manager()
        active = server.App(runs=self.runs)
        active._new_run(self.statement)
        active.state.update(phase="running", goalWorkspace=str(self.source.resolve()))
        manager.jobs[active.run_dir.name] = active
        with mock.patch.object(server.App, "_launch_solver_locked") as launched:
            with self.assertRaisesRegex(ValueError, "source workspace's active job"):
                manager.start_saved_research_job(self.source)
        launched.assert_not_called()

    def test_cannot_delete_workspace_used_by_active_continuation(self):
        manager = self.manager()
        source_app = server.App(runs=self.runs)
        source_app.state["phase"] = "done"
        manager.jobs[self.source.name] = source_app
        active = server.App(runs=self.runs)
        active._new_run(self.statement)
        active.state.update(phase="running", goalWorkspace=str(self.source.resolve()))
        manager.jobs[active.run_dir.name] = active
        with self.assertRaisesRegex(ValueError, "Stop the continuation"):
            manager.delete_job(self.source.name)
        self.assertTrue(self.source.is_dir())


    def test_cli_research_resume_preserves_unspecified_settings(self):
        manager = mock.Mock()
        manager.start_saved_research_job.return_value.state = {"runId": "continued"}
        manager.serve_forever.side_effect = KeyboardInterrupt
        arguments = ["tcs-prover", "--resume-research", str(self.source), "--no-browser"]
        with mock.patch.object(cli.sys, "argv", arguments):
            with mock.patch.object(cli, "Server", return_value=manager):
                with mock.patch.object(cli.sys, "stdout", io.StringIO()):
                    self.assertEqual(cli.main(), 0)
        manager.start_saved_research_job.assert_called_once_with(self.source.resolve(), {})
        manager.start_saved_critic_job.assert_not_called()


    def test_cli_research_resume_only_applies_explicit_setting_changes(self):
        manager = mock.Mock()
        manager.start_saved_research_job.return_value.state = {"runId": "continued"}
        manager.serve_forever.side_effect = KeyboardInterrupt
        arguments = [
            "tcs-prover", "--resume-research", str(self.source), "--no-browser",
            "--thinking-hours=5", "--criticRounds", "9",
        ]
        with mock.patch.object(cli.sys, "argv", arguments):
            with mock.patch.object(cli, "Server", return_value=manager):
                with mock.patch.object(cli.sys, "stdout", io.StringIO()):
                    self.assertEqual(cli.main(), 0)
        manager.start_saved_research_job.assert_called_once_with(
            self.source.resolve(), {"thinkingHours": 5.0, "criticRounds": 9},
        )


    def test_final_failure_report_remains_visible_without_output_field(self):
        app = server.App(runs=self.runs)
        app._new_run(self.statement)
        process, token = mock.Mock(), object()
        process.stdout = iter([json.dumps({
            "kind": "failure_result", "stage": "final",
            "label": "Final document needs repair",
            "summary": "An argument was omitted in formatting.",
        }) + "\n"])
        process.wait.return_value = 1
        app.process, app.active_token = process, token
        app.state.update(phase="running", stage="final")
        app._read_output(process, token)
        self.assertEqual(app.state["output"], "An argument was omitted in formatting.")
        self.assertEqual(
            (app.run_dir / "failure-summary.md").read_text(),
            "An argument was omitted in formatting.",
        )
        self.assertIn("Workflow incomplete", app.state["error"])


    def test_final_verifier_context_survives_node_less_requests(self):
        app = server.App(runs=self.runs)
        app._new_run(self.statement)
        process, token = mock.Mock(), object()
        records = [
            {"kind": "status", "stage": "final", "node": "final_verifier"},
            {"kind": "request", "stage": "final", "text": "Check the formatted proof"},
            {"kind": "failure_result", "stage": "final", "summary": "A missing argument"},
        ]
        process.stdout = iter(json.dumps(record) + "\n" for record in records)
        process.wait.return_value = 1
        app.process, app.active_token = process, token
        app.state.update(phase="running", stage="final", activeNode="latex_editor")
        app._read_output(process, token)
        self.assertEqual(app.state["activeNode"], "final_verifier")
        self.assertEqual(server.event_node({"stage": "final"}), "latex_editor")


if __name__ == "__main__":
    unittest.main()
