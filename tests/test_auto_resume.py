"""Error retries preserve checkpoints and respect deliberate pauses, without model calls."""

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ui import server


class AutoResumeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact statement")
        self.run = self.app.run_dir
        self.app._save("checked-statement.md", "# Checked statement\n\nExact statement\n")
        self.app.state.update(phase="running", stage="solve", activeNode="author",
                              runId=self.run.name, goalThreadId="saved-thread", elapsedSeconds=12)
        self.app._save_job_settings(self.app.state)
        self.process = Mock(stdout=io.StringIO(""))
        self.process.wait.return_value = 1
        self.process.poll.return_value = 1
        self.token = object()
        self.app.process = self.process
        self.app.active_token = self.app.worker_token = self.token

    def checkpoint(self):
        return json.loads((self.run / server.PAUSE_FILENAME).read_text(encoding="utf-8"))

    def fail(self, reason="Connection lost", now=1_000, raw=False):
        self.process.stdout = io.StringIO(
            "error: " + reason + "\n" if raw else json.dumps({
                "kind": "workflow_paused", "stage": "solve", "node": "author",
                "state": {"statement": "Exact statement", "paused": True}, "reason": reason,
            }) + "\n"
        )
        with patch.object(server.time, "time", return_value=now):
            self.app._read_output(self.process, self.token)

    def test_error_and_unexpected_exit_schedule_ten_minutes_after_pause(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                self.app.state.update(phase="running", stage="solve", activeNode="author")
                self.app.active_token = self.app.worker_token = self.token
                self.fail(raw=raw)
                checkpoint = self.checkpoint()
                self.assertEqual(self.app.state["phase"], "paused")
                self.assertEqual(checkpoint["pauseCause"], "error")
                self.assertEqual(checkpoint["autoResumeAt"], 1_600)
                restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
                self.assertEqual(restored.state["autoResumeAt"], 1_600)
                self.assertEqual(restored.state["goalThreadId"], "saved-thread")

    def test_due_retry_resumes_original_conversation_once(self):
        self.fail()
        launch = Mock(stdin=io.StringIO())
        with patch.object(server.subprocess, "Popen", return_value=launch) as popen, \
                patch.object(self.app, "_spawn_worker"):
            self.assertFalse(self.app.auto_resume_if_due(self.app.resume, now=1_599))
            popen.assert_not_called()
            self.assertTrue(self.app.auto_resume_if_due(self.app.resume, now=1_600))
            self.assertFalse(self.app.auto_resume_if_due(self.app.resume, now=1_601))
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        self.assertIn('goal_thread_id="saved-thread"', argv)
        self.assertIn('goal_require_resume=true', argv)
        self.assertEqual(popen.call_args.kwargs["cwd"], self.run)
        self.assertEqual(self.app.state["phase"], "running")
        self.assertEqual(self.app.state["autoResumeAt"], 0)
        self.assertEqual(self.checkpoint()["autoResumeAt"], 0)

    def test_failed_launch_retries_every_ten_minutes_and_survives_restart(self):
        self.fail()
        with patch.object(server.subprocess, "Popen", side_effect=OSError("offline")) as popen:
            for now, expected in ((1_600, 1), (1_601, 1), (2_199, 1), (2_200, 2)):
                self.app.auto_resume_if_due(self.app.resume, now=now)
                self.assertEqual(popen.call_count, expected)
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertEqual(self.app.state["autoResumeAt"], 2_800)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["autoResumeAt"], 2_800)

    def test_manual_and_audit_pauses_never_retry(self):
        for audit in (False, True):
            with self.subTest(audit=audit):
                self.app.state.update(phase="running", stage="solve", activeNode="author")
                self.app.active_token = self.app.worker_token = self.token
                self.app.pause(_for_audit=audit)
                self.fail(reason="Connection dropped while pausing")
                self.assertEqual(self.checkpoint()["pauseCause"], "audit" if audit else "user")
                retry = Mock()
                self.assertFalse(self.app.auto_resume_if_due(retry, now=10_000))
                retry.assert_not_called()
                self.assertEqual(self.app.state["autoResumeAt"], 0)

    def test_keep_paused_cancels_pending_retry_and_survives_restart(self):
        self.fail()
        self.app.pause()
        self.assertEqual(self.checkpoint()["pauseCause"], "user")
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["autoResumeAt"], 0)
        retry = Mock()
        restored.auto_resume_if_due(retry, now=10_000)
        retry.assert_not_called()

    def test_exhausted_budget_does_not_retry(self):
        self.fail(reason="Workflow time limit reached.")
        self.assertEqual(self.app.state["autoResumeAt"], 0)
        checkpoint = self.checkpoint()
        checkpoint.update(error="Connection lost", elapsedSeconds=168 * 3600)
        self.app._configure_auto_resume(checkpoint, now=1_000)
        self.assertEqual(checkpoint["autoResumeAt"], 0)

    def test_legacy_failure_is_scheduled_but_legacy_manual_pause_is_not(self):
        self.fail()
        for error, requested, retry in (("Connection lost", False, True), ("", False, False),
                                        ("Connection lost", True, False)):
            checkpoint = self.checkpoint()
            checkpoint.pop("pauseCause", None)
            checkpoint.pop("autoResumeAt", None)
            checkpoint["error"] = error
            if requested:
                self.app._save(server.PAUSE_REQUEST_FILENAME, "{}")
            self.app._save_pause(checkpoint)
            restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
            self.assertEqual(bool(restored.state["autoResumeAt"]), retry)

    def test_server_retries_without_browser_and_stops_with_server(self):
        self.fail(now=1)
        http = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(http.server_close)
        resumed = threading.Event()
        def resume():
            http.jobs[self.run.name].state.update(phase="running", autoResumeAt=0)
            resumed.set()
        with patch.object(http.jobs[self.run.name], "resume", side_effect=resume) as launch:
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(resumed.wait(5), "No scheduled retry without a browser")
            finally:
                http.shutdown()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(launch.call_count, 1)

    def test_workspace_conflict_defers_retry(self):
        self.fail()
        http = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(http.server_close)
        with patch.object(http, "_check_author_workspace_idle", side_effect=ValueError("Already running")), \
                patch.object(http.jobs[self.run.name], "resume") as launch:
            http._retry_paused_jobs(now=1_600)
            launch.assert_not_called()
        self.assertEqual(http.jobs[self.run.name].state["autoResumeAt"], 2_200)


if __name__ == "__main__":
    unittest.main()
