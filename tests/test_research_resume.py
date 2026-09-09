"""Continuation must preserve research before an author, critic, or writer runs."""

import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from research_journal import DATABASE_FILENAME, ResearchJournal
from ui import cli, server


class ResearchContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runs = Path(self.temporary.name) / "runs"
        self.source = self.runs / "old-research"
        self.source.mkdir(parents=True)
        self.statement = "Every task has the required property."
        self.legacy = json.dumps({
            "promptFingerprint": "a historically different prompt",
            "notes": "A previously failed approach. " * 3000,
        })
        (self.source / server.runtime.AUTHOR_MEMORY_FILENAME).write_text(
            self.legacy, encoding="utf-8",
        )
        (self.source / "FAILED.md").write_text(
            "# Failed approaches\n\nLocal choices exhaust a constrained resource.\n",
            encoding="utf-8",
        )
        (self.source / "STATEMENT.md").write_text(self.statement, encoding="utf-8")
        (self.source / "checked-statement.md").write_text(
            "# Checked statement\n\n" + self.statement + "\n", encoding="utf-8",
        )
        (self.source / "transcript.jsonl").write_text("public events\n", encoding="utf-8")
        (self.source / "unrelated-private.txt").write_text("Do not copy.", encoding="utf-8")
        with ResearchJournal(self.source, self.statement) as journal:
            self.event_id = journal.commit(
                "research_review", {"status": "refuted", "evidence": "Two-task counterexample"},
                {"next_step": "propose"},
            )
        self.credentials = mock.patch.object(server.runtime, "verify_model_credentials")
        self.credentials.start()
        self.addCleanup(self.credentials.stop)
        self.workers = mock.patch.object(server.App, "_spawn_worker")
        self.workers.start()
        self.addCleanup(self.workers.stop)

    def assert_restored(self, destination):
        with ResearchJournal(destination, self.statement) as journal:
            self.assertEqual(journal.get_state("next_step"), "propose")
            self.assertEqual(journal.get(self.event_id)["payload"]["evidence"], "Two-task counterexample")
        self.assertEqual(
            (destination / server.runtime.AUTHOR_MEMORY_FILENAME).read_text(encoding="utf-8"),
            self.legacy,
        )
        self.assertEqual(
            (destination / "FAILED.md").read_bytes(),
            (self.source / "FAILED.md").read_bytes(),
        )
        original = destination / "continuation-memory" / self.source.name
        self.assertEqual((original / "FAILED.md").read_bytes(), (self.source / "FAILED.md").read_bytes())
        provenance = json.loads((original / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(provenance["sourceTranscript"], str(self.source.resolve() / "transcript.jsonl"))
        self.assertFalse((destination / "unrelated-private.txt").exists())
        self.assertFalse((original / "unrelated-private.txt").exists())
        self.assertFalse((original / "transcript.jsonl").exists())
        self.assertTrue((destination / "research").is_dir())

    def test_author_restores_archive_before_launch_despite_prompt_change(self):
        app = server.App(runs=self.runs)

        def launch(statement):
            self.assertEqual(statement, self.statement)
            self.assert_restored(app.run_dir)
            return object(), object()

        with mock.patch.object(app, "_launch_solver_locked", side_effect=launch) as launched:
            app.start_direct_statement(
                self.statement, continuation_source=self.source, stopped_stage="solve",
                author_prompt="Use a new research policy. [STATEMENT]",
            )
        launched.assert_called_once()
        self.assertEqual(app.state["authorPrompt"], "Use a new research policy. [STATEMENT]")

    def test_critic_restores_archive_before_launch(self):
        app = server.App(runs=self.runs)

        def launch(statement, solution):
            self.assertEqual((statement, solution), (self.statement, "The proposed proof."))
            self.assert_restored(app.run_dir)
            return object(), object()

        with mock.patch.object(app, "_launch_critic_resume_locked", side_effect=launch) as launched:
            app.start_critic_resume(self.statement, "The proposed proof.", source_run=self.source)
        launched.assert_called_once()

    def test_final_restores_archive_before_launch(self):
        app = server.App(runs=self.runs)

        def launch(statement, solution):
            self.assert_restored(app.run_dir)
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

    def test_legacy_run_without_database_preserves_notebooks(self):
        (self.source / DATABASE_FILENAME).unlink()
        app = server.App(runs=self.runs)
        with mock.patch.object(app, "_launch_solver_locked", return_value=(object(), object())):
            app.start_direct_statement(self.statement, continuation_source=self.source)
        self.assertFalse((app.run_dir / DATABASE_FILENAME).exists())
        self.assertEqual((app.run_dir / "FAILED.md").read_bytes(), (self.source / "FAILED.md").read_bytes())
        self.assertEqual((app.run_dir / server.runtime.AUTHOR_MEMORY_FILENAME).read_text(), self.legacy)

    def test_database_copy_failure_prevents_launch(self):
        app = server.App(runs=self.runs)
        with mock.patch.object(server, "copy_research_archive", side_effect=OSError("disk full")):
            with mock.patch.object(app, "_launch_solver_locked") as launched:
                with self.assertRaisesRegex(ValueError, "Cannot restore.*disk full"):
                    app.start_direct_statement(self.statement, continuation_source=self.source)
        launched.assert_not_called()

    def test_legacy_copy_failure_prevents_launch(self):
        app = server.App(runs=self.runs)
        with mock.patch.object(server.shutil, "copyfile", side_effect=OSError("disk full")):
            with mock.patch.object(app, "_launch_critic_resume_locked") as launched:
                with self.assertRaisesRegex(OSError, "disk full"):
                    app.start_critic_resume(self.statement, "Proof.", source_run=self.source)
        launched.assert_not_called()

    def test_continuation_chain_retains_original_source_notes(self):
        first = server.App(runs=self.runs)
        with mock.patch.object(first, "_launch_solver_locked", return_value=(object(), object())):
            first.start_direct_statement(self.statement, continuation_source=self.source)
        (first.run_dir / "FAILED.md").write_text("# New notes\nThe next route failed.", encoding="utf-8")
        second = server.App(runs=self.runs)
        with mock.patch.object(second, "_launch_critic_resume_locked", return_value=(object(), object())):
            second.start_critic_resume(self.statement, "Proof.", source_run=first.run_dir)
        history = second.run_dir / "continuation-memory"
        self.assertEqual((history / self.source.name / "FAILED.md").read_bytes(), (self.source / "FAILED.md").read_bytes())
        self.assertEqual((history / first.run_dir.name / "FAILED.md").read_bytes(), (first.run_dir / "FAILED.md").read_bytes())

    def test_critic_accepts_explicit_external_run_directory(self):
        external_runs = Path(self.temporary.name) / "other-runs"
        app = server.App(runs=external_runs)
        with mock.patch.object(app, "_launch_critic_resume_locked", return_value=(object(), object())):
            app.start_critic_resume(self.statement, "Proof.", source_run=self.source)
        self.assert_restored(app.run_dir)

    def test_symlink_artifact_is_rejected_before_archive_copy(self):
        (self.source / "FAILED.md").unlink()
        try:
            (self.source / "FAILED.md").symlink_to(self.source / "unrelated-private.txt")
        except OSError:
            self.skipTest("Creating symbolic links is unavailable on this platform")
        app = server.App(runs=self.runs)
        with mock.patch.object(server, "copy_research_archive") as copied:
            with mock.patch.object(app, "_launch_solver_locked") as launched:
                with self.assertRaisesRegex(ValueError, "symbolic-link"):
                    app.start_direct_statement(self.statement, continuation_source=self.source)
        copied.assert_not_called()
        launched.assert_not_called()

    def test_source_bytes_are_unchanged(self):
        # Backup must not regenerate even missing readable exports in the source.
        next((self.source / "research").rglob("*.md")).unlink()
        before = {str(path.relative_to(self.source)): path.read_bytes()
                  for path in self.source.rglob("*") if path.is_file()}
        app = server.App(runs=self.runs)
        with mock.patch.object(app, "_launch_solver_locked", return_value=(object(), object())):
            app.start_direct_statement(self.statement, continuation_source=self.source)
        after = {str(path.relative_to(self.source)): path.read_bytes()
                 for path in self.source.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_terminal_journal_job_can_resume_without_manual_stop(self):
        app = server.App(runs=self.runs)
        app.state.update(phase="error", stage="solve", thinkingHours=3.75)
        self.assertFalse(app.state["manuallyStopped"])
        plan = server.Server._stopped_continuation_plan(app)
        self.assertEqual(plan["action"], "research")
        self.assertEqual(plan["label"], "Resume research checkpoint")
        manager = self.manager()
        with mock.patch.object(server.App, "_launch_solver_locked", return_value=(object(), object())):
            continued = manager._continue_stopped_job_locked(app)
        self.assertEqual(continued.state["thinkingHours"], 3.75)
        self.assert_restored(continued.run_dir)

    def test_running_and_successful_jobs_do_not_offer_research_restart(self):
        app = server.App(runs=self.runs)
        app.state.update(phase="running", stage="solve")
        self.assertIsNone(server.Server._stopped_continuation_plan(app))
        app.state.update(phase="done")
        (self.source / "final.tex").write_text("Verified output", encoding="utf-8")
        self.assertIsNone(server.Server._stopped_continuation_plan(app))

    def test_latex_only_journal_failure_can_resume_without_checked_statement(self):
        (self.source / "checked-statement.md").unlink()
        (self.source / "latex-input.md").write_text("The original theorem and proof", encoding="utf-8")
        app = server.App(runs=self.runs)
        app.state.update(phase="done", stage="final", problemMode="latex", manuallyStopped=False)
        plan = server.Server._stopped_continuation_plan(app)
        self.assertEqual(plan["action"], "final")
        self.assertEqual(plan["label"], "Resume formatting checkpoint")

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
