"""Error retries preserve checkpoints and respect deliberate pauses, without model calls."""

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from ui import server

# Error pauses are retried every minute, like every other model call.
WAIT = 60


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

    def test_the_retry_interval_is_one_minute(self):
        self.assertEqual(server.AUTO_RESUME_SECONDS, WAIT)
        self.assertEqual(server.AUTO_RESUME_SECONDS, server.runtime.MODEL_RETRY_SECONDS)
        self.assertEqual(server.retry_interval_text(), "1 minute")

    def test_error_and_unexpected_exit_schedule_one_minute_after_pause(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                self.app.state.update(phase="running", stage="solve", activeNode="author")
                self.app.active_token = self.app.worker_token = self.token
                self.fail(raw=raw)
                checkpoint = self.checkpoint()
                self.assertEqual(self.app.state["phase"], "paused")
                self.assertEqual(checkpoint["pauseCause"], "error")
                self.assertEqual(checkpoint["autoResumeAt"], 1_000 + WAIT)
                restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
                self.assertEqual(restored.state["autoResumeAt"], 1_000 + WAIT)
                self.assertEqual(restored.state["goalThreadId"], "saved-thread")

    def test_every_model_failure_cause_is_retried_after_one_minute(self):
        causes = ("Author turn failed: Selected model is at capacity. Please try a different model.",
                  "Claude author turn failed: API Error: 529 Overloaded.", "Author stopped: usageLimited.",
                  "exceeded retry limit, last status: 429 Too Many Requests",
                  "stream disconnected before completion: stream closed before response.completed",
                  "Codex app-server stopped unexpectedly.", "Claude CLI stopped unexpectedly.",
                  "Please run codex login: 401 Unauthorized",
                  "Could not resume the saved Codex conversation; no replacement was started. network unavailable")
        for reason in causes:
            with self.subTest(reason=reason):
                self.app.state.update(phase="running", stage="solve", activeNode="author", autoResumeAt=0)
                self.app.active_token = self.app.worker_token = self.token
                self.fail(reason=reason, now=5_000)
                self.assertEqual(self.checkpoint()["pauseCause"], "error")
                self.assertEqual(self.app.state["autoResumeAt"], 5_000 + WAIT)
                retry = Mock()
                self.assertFalse(self.app.auto_resume_if_due(retry, now=5_000 + WAIT - 1))
                self.assertTrue(self.app.auto_resume_if_due(retry, now=5_000 + WAIT))
                retry.assert_called_once_with()
                self.assertEqual(self.app.state["autoResumeAt"], 5_000 + 2 * WAIT)

    def test_user_pause_and_stop_are_never_retried(self):
        self.fail(now=1_000)
        self.app.pause()  # Keep paused.
        retry = Mock()
        self.assertFalse(self.app.auto_resume_if_due(retry, now=10**9))
        self.app.state.update(phase="running", stage="solve", activeNode="author")
        self.app.active_token = self.app.worker_token = self.token
        with patch.object(server, "stop_process_tree"):
            self.app.stop()
        self.assertEqual(self.app.state["autoResumeAt"], 0)
        self.assertFalse(self.app.auto_resume_if_due(retry, now=10**9))
        retry.assert_not_called()

    def test_due_retry_resumes_original_conversation_once(self):
        self.fail()
        launch = Mock(stdin=io.StringIO())
        with patch.object(server.subprocess, "Popen", return_value=launch) as popen, \
                patch.object(self.app, "_spawn_worker"):
            self.assertFalse(self.app.auto_resume_if_due(self.app.resume, now=1_000 + WAIT - 1))
            popen.assert_not_called()
            self.assertTrue(self.app.auto_resume_if_due(self.app.resume, now=1_000 + WAIT))
            self.assertFalse(self.app.auto_resume_if_due(self.app.resume, now=1_000 + WAIT + 1))
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        self.assertIn('goal_thread_id="saved-thread"', argv)
        self.assertIn('goal_require_resume=true', argv)
        # The resumed author keeps the run's private temp folder.
        self.assertIn("run_tmp_dir=" + json.dumps(str(server.controller_directory(self.run) / "tmp")), argv)
        self.assertEqual(popen.call_args.kwargs["cwd"], self.run)
        self.assertEqual(self.app.state["phase"], "running")
        self.assertEqual(self.app.state["autoResumeAt"], 0)
        self.assertEqual(self.checkpoint()["autoResumeAt"], 0)

    def test_failed_launch_retries_every_minute_and_survives_restart(self):
        self.fail()
        first = 1_000 + WAIT
        with patch.object(server.subprocess, "Popen", side_effect=OSError("offline")) as popen:
            for now, expected in ((first, 1), (first + 1, 1), (first + WAIT - 1, 1), (first + WAIT, 2)):
                self.app.auto_resume_if_due(self.app.resume, now=now)
                self.assertEqual(popen.call_count, expected)
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertEqual(self.app.state["autoResumeAt"], first + 2 * WAIT)
        trace = [entry.get("text", "") for entry in self.app.state["trace"]]
        self.assertTrue(any(text.endswith("Retrying in 1 minute.") for text in trace), trace)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["autoResumeAt"], first + 2 * WAIT)

    def test_manual_pauses_never_retry(self):
        for reason in ("Connection dropped while pausing", ""):
            with self.subTest(reason=reason):
                self.app.state.update(phase="running", stage="solve", activeNode="author")
                self.app.active_token = self.app.worker_token = self.token
                self.app.pause()
                self.fail(reason=reason)
                self.assertEqual(self.checkpoint()["pauseCause"], "user")
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
        self.app.state["thinkingHours"] = 168  # A finite limit; the default is unlimited.
        self.fail(reason="Workflow time limit reached.")
        self.assertEqual(self.app.state["autoResumeAt"], 0)
        checkpoint = self.checkpoint()
        checkpoint.update(error="Connection lost", elapsedSeconds=168 * 3600)
        self.app._configure_auto_resume(checkpoint, now=1_000)
        self.assertEqual(checkpoint["autoResumeAt"], 0)

    def test_a_time_limit_pause_has_its_own_cause_and_waits_for_the_user(self):
        self.app.state["thinkingHours"] = 1
        self.fail(reason="Workflow time limit reached.")
        checkpoint = self.checkpoint()
        self.assertEqual((checkpoint["pauseCause"], checkpoint["autoResumeAt"]), ("time-limit", 0))
        never = Mock(side_effect=AssertionError("resumed by itself"))
        self.assertFalse(self.app.auto_resume_if_due(never, now=10**9))
        # Raising the limit does not resume it either; Resume is the user's.
        self.app.set_author_time_limit(5)
        self.assertFalse(self.app.auto_resume_if_due(never, now=10**9))
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual((restored.state["autoResumeAt"], restored.snapshot()["pauseCause"]), (0, "time-limit"))
        restored.auto_resume_if_due(never, now=10**9)
        # Keep paused keeps the cause.
        self.app.pause()
        self.assertEqual(self.checkpoint()["pauseCause"], "time-limit")

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
            http._retry_paused_jobs(now=1_000 + WAIT)
            launch.assert_not_called()
        self.assertEqual(http.jobs[self.run.name].state["autoResumeAt"], 1_000 + 2 * WAIT)

    def test_a_conversation_the_cli_no_longer_has_is_not_retried(self):
        self.process.stdout = io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "solve", "node": "author",
            "state": {"statement": "Exact statement", "paused": True}, "sessionUnavailable": True,
            "reason": "Could not resume the saved Claude conversation; no replacement was started.",
        }) + "\n")
        with patch.object(server.time, "time", return_value=1_000):
            self.app._read_output(self.process, self.token)
        checkpoint = self.checkpoint()
        self.assertEqual((self.app.state["phase"], checkpoint["pauseCause"]), ("paused", "error"))
        self.assertEqual((checkpoint["autoResumeAt"], checkpoint["sessionUnavailable"]), (0, True))
        never = Mock(side_effect=AssertionError("retried"))
        self.assertFalse(self.app.auto_resume_if_due(never, now=10**9))
        # The saved work continues in a new conversation instead of Resume.
        plan = server.Server._stopped_continuation_plan(self.app)
        self.assertEqual((plan["action"], plan["label"]), ("author", "Continue proof author"))
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["autoResumeAt"], 0)
        self.assertEqual(server.Server._stopped_continuation_plan(restored)["action"], "author")
        # An ordinary error pause still offers Resume.
        restored.state["sessionUnavailable"] = False
        self.assertEqual(server.Server._stopped_continuation_plan(restored)["action"], "resume")

    def test_an_author_stopped_for_safety_is_never_resumed_automatically(self):
        self.process.stdout = io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "solve", "node": "author",
            "state": {"statement": "Exact statement", "paused": True}, "safetyStop": True,
            "reason": "Stopped for safety: Codex returned a different thread ID; refusing to continue a replacement "
                      "conversation. Automatic retry is off; check the setup, then choose Resume.",
        }) + "\n")
        with patch.object(server.time, "time", return_value=1_000):
            self.app._read_output(self.process, self.token)
        checkpoint = self.checkpoint()
        self.assertEqual((self.app.state["phase"], checkpoint["pauseCause"]), ("paused", "error"))
        self.assertEqual((checkpoint["autoResumeAt"], checkpoint["safetyStop"], self.app.state["safetyStop"]),
                         (0, True, True))
        never = Mock(side_effect=AssertionError("retried"))
        self.assertFalse(self.app.auto_resume_if_due(never, now=10**9))
        # Resume stays the user's choice, and survives a restart without retries.
        self.assertEqual(server.Server._stopped_continuation_plan(self.app)["action"], "resume")
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual((restored.state["autoResumeAt"], restored.state["safetyStop"]), (0, True))

    def test_a_cli_that_is_updating_does_not_stop_automatic_resume(self):
        self.fail()
        updating = server.TransientLaunchError(
            "Claude CLI is not installed or is not on PATH; install Claude Code to use Claude models.")
        with patch.object(server, "require_model_access", side_effect=updating), \
                patch.object(server.subprocess, "Popen") as popen:
            self.assertTrue(self.app.auto_resume_if_due(self.app.resume, now=1_000 + WAIT))
        popen.assert_not_called()
        # The next try stays scheduled one minute later.
        self.assertEqual(self.app.state["autoResumeAt"], 1_000 + 2 * WAIT)
        trace = [entry.get("text", "") for entry in self.app.state["trace"]]
        self.assertTrue(any(text.startswith("Automatic resume failed: Claude CLI is not installed")
                            and text.endswith("Retrying in 1 minute.") for text in trace), trace)
        self.assertFalse(any(text.startswith("Automatic resume stopped") for text in trace))
        # A missing API key waits for the settings: checked again every minute, said once, never launched.
        self.app.state["autoResumeAt"] = 1_000 + 2 * WAIT
        with patch.object(server, "require_model_access", side_effect=ValueError("Add an API key for Gateway.")), \
                patch.object(server.subprocess, "Popen") as popen:
            for tick in (2, 3):
                self.assertTrue(self.app.auto_resume_if_due(self.app.resume, now=1_000 + tick * WAIT))
        popen.assert_not_called()
        self.assertEqual(self.app.state["autoResumeAt"], 1_000 + 4 * WAIT)
        waits = [entry["text"] for entry in self.app.state["trace"]
                 if entry.get("text", "").startswith("Automatic resume waits for the model settings")]
        self.assertEqual(waits, ["Automatic resume waits for the model settings: Add an API key for Gateway. The job "
                                 "checks again every 1 minute and continues by itself once they are fixed."])
        self.assertEqual(self.app.attention()["kind"], "settings")

    def test_runtime_reports_a_missing_cli_as_transient_and_a_missing_key_as_settings(self):
        with patch.object(server.runtime, "verify_model_credentials",
                          side_effect=server.runtime.CLIUnavailable("Claude CLI is not installed.")):
            with self.assertRaises(server.TransientLaunchError):
                server.require_model_access(["claude-opus-5-5"])
        with patch.object(server.runtime, "verify_model_credentials",
                          side_effect=server.runtime.ConfigurationError("Add an API key.")):
            with self.assertRaises(ValueError) as raised:
                server.require_model_access(["api:gateway"])
        self.assertNotIsInstance(raised.exception, server.TransientLaunchError)


if __name__ == "__main__":
    unittest.main()
