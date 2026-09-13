"""Live audit settings and lifecycle integration without model requests."""

import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ui import audits, server


class AuditIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact task")
        self.app.state.update(phase="paused", activeNode="author", stage="solve",
                              runId=self.app.run_dir.name, goalWorkspace=str(self.app.run_dir),
                              fileManagement=True)
        self.app._save("checked-statement.md", "# Checked statement\n\nExact task\n")
        self.app.add_trace({"kind": "status", "stage": "solve", "node": "author"})
        self.options = {"intervalHours": 2, "models": ["gpt-6-astra", "none", "claude-opus"]}

    def test_scheduler_reads_and_rotates_in_actual_author_workspace(self):
        workspace = self.runs / "author-workspace"
        workspace.mkdir()
        (workspace / "PROVED.md").write_text("Author's saved proof")
        (workspace / "AUDITS").mkdir()
        (workspace / "AUDITS/previous.md").write_text("Previous advice")
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state.update(phase="running", researchAudits=self.options,
                              goalWorkspace=str(workspace))
        with patch.object(threading.Thread, "start"):
            self.app._start_research_audits(token)
        engine = self.app.research_audits
        self.addCleanup(engine.close)
        self.assertEqual(engine.run_dir, workspace)
        engine.preflight = lambda model: True
        engine.before_batch = Mock(return_value=True)
        engine.after_batch = Mock()
        calls = []

        def provider(model, prompt, cwd, cancelled):
            calls.append(cwd)
            self.assertEqual(cwd, workspace.resolve())
            self.assertEqual((cwd / "PROVED.md").read_text(), "Author's saved proof")
            self.assertEqual((cwd / "audit_history/previous.md").read_text(), "Previous advice")
            self.assertFalse((cwd / "AUDITS/previous.md").exists())
            return {"text": "New advice", "model": model["model"]}

        engine.provider = provider
        engine.update(self.options, active=True, start_now=True)
        engine.thread.join(timeout=3)
        self.assertFalse(engine.thread.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(list((workspace / "AUDITS").glob("*.md"))), 2)
        self.assertFalse((self.app.run_dir / "AUDITS").exists())
        engine.after_batch.assert_called_once_with(engine)

    def test_simple_runs_reject_audits_and_never_start_scheduler(self):
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state.update(phase="running", fileManagement=False, researchAudits=self.options)
        with patch.object(audits, "ResearchAudits") as constructor:
            self.app._start_research_audits(token)
        constructor.assert_not_called()
        for action in (lambda: self.app.set_research_audits(self.options),
                       self.app.start_research_audit_now):
            with self.assertRaisesRegex(ValueError, "require file management"):
                action()

    def test_live_settings_persist_without_restart_or_note_changes(self):
        self.app._save("INITIAL_PROMPT.md", "Original instructions")
        self.app._save("author-steer.json", '{"instruction":"User instruction"}')
        with patch.object(server.subprocess, "Popen", side_effect=AssertionError("Unexpected restart")):
            self.app.set_research_audits(self.options)
        self.app.state["researchAuditProgress"] = {"elapsedSeconds": 150, "lastStartedSeconds": 0}
        self.app._save_job_settings(self.app.state)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["researchAudits"], self.options)
        self.assertEqual(restored.state["researchAuditProgress"]["elapsedSeconds"], 150)
        self.assertEqual((self.app.run_dir / "INITIAL_PROMPT.md").read_text(), "Original instructions")
        self.assertEqual((self.app.run_dir / "author-steer.json").read_text(), '{"instruction":"User instruction"}')
        self.assertFalse((self.app.run_dir / "audit.md").exists())

    def test_enabling_while_paused_starts_a_fresh_interval(self):
        self.app.state["researchAuditProgress"] = {"elapsedSeconds": 36000, "lastStartedSeconds": 0}
        self.app.set_research_audits(self.options)
        engine = audits.ResearchAudits(self.app.run_dir, progress=self.app.state["researchAuditProgress"])
        self.addCleanup(engine.close)
        engine.update(self.options, active=True)
        self.assertIsNone(engine.thread)

    def test_audit_constructor_failure_does_not_clear_author_worker(self):
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state.update(phase="running", researchAudits=self.options)
        with patch.object(audits, "ResearchAudits", side_effect=ValueError("Invalid audit config")):
            self.app._start_research_audits(token)
        self.assertIs(self.app.worker_token, token)
        self.assertIs(self.app.active_token, token)
        self.assertEqual(self.app.state["phase"], "running")
        self.assertIn("Invalid audit config", self.app.state["researchAuditStatus"]["lastError"])

    def test_initial_none_has_no_audit_engine_or_counter(self):
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state["phase"] = "running"
        with patch.object(audits, "ResearchAudits") as constructor:
            self.app._start_research_audits(token)
        constructor.assert_not_called()
        self.assertIsNone(self.app.research_audits)
        self.assertEqual(self.app.state["researchAuditProgress"], {})
        self.assertFalse(self.app.state["auditHoldingAuthor"])
        self.assertIs(self.app.worker_token, token)

    def test_recovered_model_warning_is_saved_immediately_with_live_activity(self):
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state.update(phase="running", researchAudits=self.options)
        with patch.object(threading.Thread, "start"):
            self.app._start_research_audits(token)
        engine = self.app.research_audits
        self.addCleanup(engine.close)
        engine.update(self.options)
        engine._warning(1, "gpt-6-astra", "Usage limit")
        engine._warning(3, "claude-opus", "Not logged in")
        engine._activity(3, "claude-opus", threading.Event(), "working", "Read: PROVED.md")
        saved = json.loads((self.app.run_dir / "job-settings.json").read_text())
        self.assertEqual([row["slot"] for row in saved["researchAuditProgress"]["warnings"]], [1])
        self.assertEqual([row["slot"] for row in self.app.state["researchAuditStatus"]["warnings"]], [1])
        event = self.app.state["trace"][-1]
        self.assertEqual(event["kind"], "research_audit")
        self.assertEqual(event["text"], "Read: PROVED.md")
        self.assertTrue(event["recovered"])
        self.assertTrue(event["time"])
        self.assertFalse((self.app.run_dir / "AUDITS").exists())

    def test_audit_outcome_stays_visible_after_author_activity_and_reload(self):
        outcome = {"kind": "research_audit", "status": "completed", "slot": 1,
                   "model": "gpt-6-astra", "report": "AUDITS/report.md", "text": "Report saved"}
        with patch.object(server, "TRACE_LIMIT", 3):
            self.app.add_trace(outcome)
            for _ in range(5):
                self.app.add_trace({"kind": "research_audit", "status": "working", "text": "Reading"})
            self.assertNotIn("completed", [row.get("status") for row in self.app.state["trace"]])
            reloaded = server.App(self.app.trace_file, self.runs)
            for app in (self.app, reloaded):
                reports = [row for row in app.retained_trace() if row.get("status") == "completed"]
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0]["report"], "AUDITS/report.md")

    def test_audit_prompt_persists_exactly_in_transcript_and_survives_reload(self):
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state.update(phase="running", researchAudits=self.options)
        with patch.object(threading.Thread, "start"):
            self.app._start_research_audits(token)
        engine = self.app.research_audits
        self.addCleanup(engine.close)
        prompt = "Edited instructions.\n\nKeep \\lambda and 中文.  \n"
        event = {"kind": "request", "stage": "audit", "status": "prompt", "slot": 1,
                 "model": "gpt-6-astra", "label": "Audit-1 — GPT Astra (Ultra) — Prompt to model",
                 "text": prompt}
        with patch.object(server, "TRACE_LIMIT", 3):
            engine.on_event(event)
            for _ in range(5):
                self.app.add_trace({"kind": "research_audit", "status": "working", "text": "Reading"})
            saved = [json.loads(line) for line in self.app.trace_file.read_text().splitlines()]
            self.assertEqual(next(row for row in saved if row.get("kind") == "request")["text"], prompt)
            reloaded = server.App(self.app.trace_file, self.runs)
            records = [row for row in reloaded.retained_trace() if row.get("kind") == "request"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["text"], prompt)
            self.assertEqual(records[0]["label"], event["label"])

    def manual_scheduler(self):
        self.app.worker_token = self.app.active_token = object()
        self.app.state.update(phase="running", researchAudits=self.options,
                              elapsedSeconds=1234, thinkingHours=12)
        release = threading.Event()
        engine = audits.ResearchAudits(
            self.app.run_dir, clock=lambda: 0,
            progress={"elapsedSeconds": 300, "lastStartedSeconds": 100},
            preflight=lambda model: release.wait(5), provider=Mock(),
            before_batch=Mock(return_value=False), after_batch=Mock(),
        )
        self.app.research_audits = engine
        def cleanup():
            release.set()
            engine.close()
            if engine.thread is not None:
                engine.thread.join(timeout=2)
        self.addCleanup(cleanup)
        return engine

    def test_manual_start_persists_clock_immediately_and_preserves_author_settings(self):
        engine = self.manual_scheduler()
        self.app._save("INITIAL_PROMPT.md", "Author instructions")
        with patch.object(self.app, "_start_research_audits") as start_monitor:
            self.app.start_research_audit_now()
        start_monitor.assert_not_called()
        saved = json.loads((self.app.run_dir / "job-settings.json").read_text())
        self.assertEqual(saved["researchAuditProgress"]["elapsedSeconds"], 300)
        self.assertEqual(saved["researchAuditProgress"]["lastStartedSeconds"], 300)
        self.assertEqual(saved["researchAudits"], self.options)
        self.assertEqual((saved["elapsedSeconds"], saved["thinkingHours"]), (1234, 12))
        self.assertTrue(self.app.state["researchAuditStatus"]["batchActive"])
        self.assertEqual((self.app.run_dir / "INITIAL_PROMPT.md").read_text(), "Author instructions")
        self.assertFalse(self.app.state["auditHoldingAuthor"])
        engine.before_batch.assert_not_called()  # Preflight has not completed yet.

    def test_manual_start_rejects_none_and_non_running_author_states(self):
        self.app.worker_token = object()
        self.app.state.update(phase="running")
        with patch.object(self.app, "_start_research_audits") as start_monitor:
            with self.assertRaisesRegex(ValueError, "at least one auditor"):
                self.app.start_research_audit_now()
        start_monitor.assert_not_called()
        self.assertEqual(self.app.state["researchAuditProgress"], {})
        self.manual_scheduler()
        for change in ({"phase": "paused"}, {"phase": "pausing"}, {"phase": "stopping"},
                       {"stage": "critic"}, {"activeNode": "critic"}, {"manuallyStopped": True},
                       {"auditHoldingAuthor": True}):
            original = dict(self.app.state)
            with self.subTest(change=change):
                self.app.state.update(change)
                with self.assertRaisesRegex(ValueError, "while the author is running"):
                    self.app.start_research_audit_now()
                self.app.state = original
        self.app.worker_token = None
        with self.assertRaises(ValueError):
            self.app.start_research_audit_now()
        self.assertIsNone(self.app.research_audits.thread)
        self.assertEqual(self.app.research_audits.checkpoint()["lastStartedSeconds"], 100)

    def test_model_warnings_restore_while_paused_and_follow_live_slot_selection(self):
        warnings = [{"slot": slot, "model": model, "message": f"Warning: Audit-{slot} — {model} is unavailable"}
                    for slot, model in ((1, "gpt-6-astra"), (3, "claude-opus"))]
        self.app.state.update(researchAudits=self.options,
                              researchAuditProgress={"elapsedSeconds": 7200, "lastStartedSeconds": 7200,
                                                     "warnings": warnings})
        self.app._save_job_settings(self.app.state)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["researchAuditStatus"]["warnings"], warnings)
        self.assertIn("gpt-6-astra", restored.state["researchAuditStatus"]["lastError"])
        self.assertIn("claude-opus", restored.state["researchAuditStatus"]["lastError"])
        self.assertEqual(restored.state["researchAuditStatus"]["runningSlots"], [])
        restored.state.update(phase="paused", activeNode="author", stage="solve")
        restored.set_research_audits({"intervalHours": 2, "models": ["none", "none", "claude-opus"]})
        self.assertEqual(restored.state["researchAuditStatus"]["warnings"], warnings[1:])
        restored.set_research_audits({"intervalHours": 2, "models": ["none", "none", "kimi-code"]})
        self.assertEqual(restored.state["researchAuditStatus"]["warnings"], [])
        self.assertEqual(restored.state["researchAuditStatus"]["lastError"], "")
        self.assertIsNone(restored.research_audits)

    def test_audit_update_failure_does_not_interrupt_author(self):
        engine = Mock()
        engine.closed = False
        engine.update.side_effect = RuntimeError("Audit unavailable")
        self.app.research_audits = engine
        self.app.state["phase"] = "running"
        self.app._update_research_audits()
        engine.close.assert_called_once()
        self.assertEqual(self.app.state["phase"], "running")
        self.assertIn("Audit unavailable", self.app.state["researchAuditStatus"]["lastError"])

    def test_all_unavailable_auditors_warn_without_pausing_author(self):
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state.update(phase="running", researchAudits=self.options)
        provider = Mock()
        before = Mock(wraps=self.app._pause_for_research_audit)
        after = Mock(wraps=self.app._resume_after_research_audit)
        engine = audits.ResearchAudits(
            self.app.run_dir, on_event=self.app.add_trace, clock=lambda: 0,
            progress={"elapsedSeconds": 7200, "lastStartedSeconds": 0},
            provider=provider, preflight=Mock(side_effect=RuntimeError("CLI not installed")),
            before_batch=before, after_batch=after,
        )
        self.app.research_audits = engine
        self.addCleanup(engine.close)
        self.app._update_research_audits()
        engine.thread.join(timeout=2)
        self.assertFalse(engine.thread.is_alive())
        self.app._update_research_audits()
        before.assert_not_called()
        after.assert_not_called()
        provider.assert_not_called()
        self.assertIs(self.app.worker_token, token)
        self.assertIs(self.app.active_token, token)
        self.assertEqual(self.app.state["phase"], "running")
        self.assertEqual(self.app.state["error"], "")
        self.assertFalse(self.app.state["auditHoldingAuthor"])
        self.assertFalse((self.app.run_dir / server.PAUSE_FILENAME).exists())
        self.assertFalse((self.app.run_dir / "AUDITS").exists())
        warnings = [record for record in self.app.state["trace"] if record.get("status") == "warning"]
        self.assertEqual([record["slot"] for record in warnings], [1, 3])
        self.assertIn("CLI not installed", self.app.state["researchAuditStatus"]["lastError"])

    def test_reset_detaches_audits_and_preserves_saved_run_settings(self):
        self.app.state.update(researchAudits=self.options, authorThreadId="saved-author-thread")
        self.app._save_job_settings(self.app.state)
        settings_path = self.app.run_dir / "job-settings.json"
        saved = settings_path.read_bytes()
        engine = self.app.research_audits = Mock()
        self.app._audit_token = object()
        self.app.reset()
        engine.close.assert_called_once()
        self.assertIsNone(self.app.research_audits)
        self.assertIsNone(self.app._audit_token)
        self.assertEqual(settings_path.read_bytes(), saved)

    def pause_for_audit(self):
        self.app.state.update(phase="running", goalThreadId="saved-author-thread", researchAudits=self.options)
        engine = self.app.research_audits = Mock()
        engine.closed = False
        engine.checkpoint.return_value = {"elapsedSeconds": 7200, "lastStartedSeconds": 7200}
        engine.status.return_value = {"runningSlots": [], "lastError": ""}
        process = self.app.process = Mock(stdout=io.StringIO(""))
        process.wait.return_value = 0
        token = self.app.worker_token = self.app.active_token = self.app._audit_token = object()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.app._pause_for_research_audit(engine)))
        worker.start()
        deadline = time.monotonic() + 2
        while self.app.state["phase"] != "pausing" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.app.state["auditHoldingAuthor"])
        self.assertEqual(result, [])  # Auditors must wait for the author reader, not just the Pause request.
        checkpoint = json.loads((self.app.run_dir / server.PAUSE_FILENAME).read_text())
        process.stdout = io.StringIO(json.dumps({"kind": "workflow_paused", "stage": "solve",
                                               "node": "author", "state": checkpoint["state"]}) + "\n")
        self.app._read_output(process, token)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertTrue(engine.update.call_args.kwargs["paused_for_audit"])
        return engine

    def test_audit_pause_drains_author_then_resumes_same_thread_folder_and_monitor(self):
        engine = self.pause_for_audit()
        with self.assertRaises(ValueError):
            self.app.resume()
        run = self.app.run_dir
        for role in ("author", "critic", "final"):
            self.app._save(f"prompts/{role}.txt", self.app.state[f"{role}Prompt"])
        self.app._save("AUDITS/20260912-audit-1.md", "Completed advice")
        pending = '{"id":"pending-user-instruction", "instruction":"Keep my assumptions"}'
        self.app._save(server.AUTHOR_STEER_FILENAME, pending)
        def launch(command, **kwargs):
            values = dict(command[i + 1].split("=", 1) for i, value in enumerate(command) if value == "--set")
            self.assertEqual(json.loads(values["goal_thread_id"]), "saved-author-thread")
            self.assertTrue(json.loads(values["goal_require_resume"]))
            self.assertEqual(kwargs["cwd"], run)
            self.assertTrue((run / "AUDITS/20260912-audit-1.md").is_file())
            return Mock(stdin=io.StringIO())
        def spawn(target, args, token):
            self.app.worker_token = token
            self.app._start_research_audits(token)
        with patch.object(server.subprocess, "Popen", side_effect=launch), patch.object(self.app, "_spawn_worker", side_effect=spawn):
            self.app._resume_after_research_audit(engine)
        self.assertEqual(self.app.state["phase"], "running")
        self.assertFalse(self.app.state["auditHoldingAuthor"])
        self.assertIs(self.app.research_audits, engine)
        self.assertIs(self.app._audit_token, self.app.worker_token)
        self.assertEqual((run / server.AUTHOR_STEER_FILENAME).read_text(), pending)
        self.assertEqual(len(list(self.runs.iterdir())), 1)

    def test_manual_pause_during_audit_prevents_automatic_resume(self):
        engine = self.pause_for_audit()
        self.app.pause()
        with patch.object(self.app, "resume") as resume:
            self.app._resume_after_research_audit(engine)
        resume.assert_not_called()
        self.assertFalse(self.app.state["auditHoldingAuthor"])
        self.assertFalse(engine.update.call_args.kwargs["paused_for_audit"])
        self.assertEqual(self.app.state["phase"], "paused")

    def test_stop_during_audit_prevents_automatic_resume(self):
        engine = self.pause_for_audit()
        with patch.object(server, "stop_process_tree"), patch.object(self.app, "resume") as resume:
            self.app.stop()
            self.app._resume_after_research_audit(engine)
        resume.assert_not_called()
        self.assertTrue(self.app.state["manuallyStopped"])
        self.assertEqual(self.app.state["phase"], "done")

    def test_failed_automatic_resume_leaves_author_paused(self):
        engine = self.pause_for_audit()
        with patch.object(self.app, "resume", side_effect=OSError("Connection unavailable")):
            self.app._resume_after_research_audit(engine)
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertFalse(self.app.state["auditHoldingAuthor"])
        self.assertIn("Connection unavailable", self.app.state["error"])

    def test_scheduler_failure_waits_for_audit_cleanup_then_resumes_author(self):
        engine = self.pause_for_audit()
        engine.update.side_effect = RuntimeError("Audit scheduler failed")
        engine.close.side_effect = lambda: setattr(engine, "closed", True)
        with patch.object(self.app, "resume") as resume:
            self.app._update_research_audits()
            resume.assert_not_called()
            self.assertIs(self.app._audit_pause_engine, engine)
            self.assertEqual(self.app.state["phase"], "paused")
            self.app._resume_after_research_audit(engine)
        resume.assert_called_once()
        self.assertIsNone(self.app.research_audits)
        self.assertFalse(self.app.state["auditHoldingAuthor"])
        self.assertIn("Audit scheduler failed", self.app.state["researchAuditStatus"]["lastError"])

    def test_author_completion_during_pause_skips_audit_and_releases_checkpoint(self):
        engine = self.app.research_audits = Mock()
        engine.closed = False
        self.app.state.update(phase="running", goalThreadId="saved-author-thread")
        def settle_at_critic(**kwargs):
            self.app.state.update(phase="paused", activeNode="critic", stage="critic")
            self.app.worker_token = None
        with patch.object(self.app, "pause", side_effect=settle_at_critic):
            self.assertFalse(self.app._pause_for_research_audit(engine))
        with patch.object(self.app, "resume") as resume:
            self.app._resume_after_research_audit(engine)
        resume.assert_called_once()
        self.assertFalse(self.app.state["auditHoldingAuthor"])

    def test_critic_handoff_cancels_audits_without_changing_critic(self):
        token = self.app.worker_token = self.app.active_token = object()
        self.app.state["phase"] = "running"
        engine = self.app.research_audits = Mock()
        engine.closed = False
        engine.checkpoint.return_value = {"elapsedSeconds": 30, "lastStartedSeconds": 0}
        engine.status.return_value = {"runningSlots": [], "lastError": ""}
        process = self.app.process = Mock(stdout=io.StringIO(json.dumps(
            {"kind": "status", "stage": "critic", "node": "critic", "label": "Critic started"}) + "\n"))
        process.wait.return_value = 0
        self.app._read_output(process, token)
        self.assertTrue(engine.update.call_args_list)
        self.assertTrue(all(call.kwargs["active"] is False for call in engine.update.call_args_list))
        self.assertEqual(self.app.state["activeNode"], "critic")

    def test_lowercase_index_and_audit_are_readable_in_both_file_readers(self):
        run = self.app.run_dir.resolve()
        (run / "APPROACHES").mkdir()
        (run / "APPROACHES/index.md").write_text("DAG index")
        (run / "APPROACHES/A001.md").write_text("First attempt")
        (run / "audit.md").write_text("Independent advice")
        (run / "AUDITS").mkdir()
        (run / "AUDITS/20260912-audit-1.md").write_text("New independent advice")
        (run / "AUDITS/linked.md").symlink_to(run / "PROVED.md")
        (run / "audit_history").mkdir()
        (run / "audit_history/older.md").write_text("Archived advice")
        (run / "audit_history/linked.md").symlink_to(run / "PROVED.md")
        for portable in (False, True):
            for name, expected in (("APPROACHES", "DAG index"), ("audit.md", "Independent advice"),
                                   ("AUDITS", "New independent advice"),
                                   ("AUDITS/20260912-audit-1.md", "New independent advice"),
                                   ("audit_history", "Archived advice"),
                                   ("audit_history/older.md", "Archived advice")):
                result = (self.app._memory_file_portable(run, name, "", {"name": name, "status": "missing"}, name == "APPROACHES")
                          if portable else self.app.memory_file(name))
                self.assertEqual(result["content"], expected)
                if name == "APPROACHES":
                    self.assertEqual(result["files"], ["APPROACHES/index.md", "APPROACHES/A001.md"])
                elif name.startswith("AUDITS"):
                    self.assertEqual(result["files"], ["AUDITS/20260912-audit-1.md"])
                else:
                    self.assertEqual(result["files"], ["audit_history/older.md", "audit.md"])

    def test_history_reader_handles_empty_archive_and_rejects_symlinks(self):
        run = self.app.run_dir.resolve()
        for portable in (False, True):
            def read(name):
                return (self.app._memory_file_portable(run, name, "", {"name": name, "status": "missing"}, False)
                        if portable else self.app.memory_file(name))
            self.assertEqual(read("audit_history")["status"], "missing")
            (run / "audit_history").symlink_to(run.parent)
            self.assertEqual(read("audit_history")["status"], "unavailable")
            self.assertEqual(read("audit_history/anything.md")["status"], "unavailable")
            (run / "audit_history").unlink()
        for name in ("audit_history/../PROVED.md", "audit_history/subfolder/file.md"):
            with self.assertRaises(ValueError):
                self.app.memory_file(name)

    def test_http_live_settings_require_auth_and_validate_choices(self):
        http = server.Server((server.HOST, 0), runs=self.runs)
        http.jobs[self.app.state["runId"]] = self.app
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        url = http.origin + "/set-research-audits?job=" + self.app.state["runId"]
        def request(data, authenticated=True):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["X-TCS-Prover-Token"] = http.token
            return urlopen(Request(url, data=json.dumps(data).encode(), headers=headers))
        with self.assertRaises(HTTPError) as error:
            request(self.options, False)
        self.assertEqual(error.exception.code, 403)
        with request(self.options) as response:
            self.assertEqual(json.load(response)["researchAudits"], self.options)
        for invalid in ({**self.options, "intervalHours": 0}, {**self.options, "models": ["none"]}):
            with self.assertRaises(HTTPError) as error:
                request(invalid)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.app.state["researchAudits"], self.options)

    def test_http_manual_audit_requires_auth_uses_saved_job_settings_and_rejects_duplicates(self):
        engine = self.manual_scheduler()
        http = server.Server((server.HOST, 0), runs=self.runs)
        http.jobs[self.app.state["runId"]] = self.app
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        def request(authenticated=True, job=None):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["X-TCS-Prover-Token"] = http.token
            url = http.origin + "/start-research-audit?job=" + (job or self.app.state["runId"])
            return urlopen(Request(url, data=b'{"models":["none","none","none"]}', headers=headers))
        with self.assertRaises(HTTPError) as error:
            request(False)
        self.assertEqual(error.exception.code, 403)
        self.assertIsNone(engine.thread)
        with self.assertRaises(HTTPError) as error:
            request(job="missing-job")
        self.assertEqual(error.exception.code, 400)
        self.assertIsNone(engine.thread)
        with request() as response:
            state = json.load(response)
        self.assertEqual(state["researchAudits"], self.options)
        self.assertTrue(state["researchAuditStatus"]["batchActive"])
        self.assertEqual(state["researchAuditProgress"]["lastStartedSeconds"], 300)
        saved = (self.app.run_dir / "job-settings.json").read_bytes()
        with self.assertRaises(HTTPError) as error:
            request()
        self.assertEqual(error.exception.code, 400)
        self.assertIn("already in progress", json.load(error.exception)["error"])
        self.assertEqual((self.app.run_dir / "job-settings.json").read_bytes(), saved)
        self.assertEqual(engine.checkpoint()["lastStartedSeconds"], 300)


if __name__ == "__main__":
    unittest.main()
