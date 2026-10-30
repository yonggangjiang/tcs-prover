"""The time-limit pause, its progress summary and talking to a paused author, offline (no model, no CLI)."""

import io
import json
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from ui import cli, server

WAIT = 60
LIVE_STAGES = {"review": False, "author": True, "critic": True, "writing": True}


class Stdin(io.StringIO):
    """A runner's stdin that keeps what was written after it is closed."""

    text = None

    def close(self):
        self.text = self.getvalue()
        super().close()


class FakeRunner:
    """A workflow runner that already ended: its records are read from ``lines``; it has no PID to signal."""

    def __init__(self, lines=(), code=0):
        self.stdin = Stdin()
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" if isinstance(line, dict) else line
                                          for line in lines))
        self.code, self.pid = code, None
        self.signals = []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def send_signal(self, number):
        self.signals.append(number)


class HoursTextTests(unittest.TestCase):
    def test_limits_are_written_as_the_page_writes_them(self):
        # ui/app.js limitHoursText gives the same texts (tests/test_conversation_ui.py).
        self.assertEqual([server.hours_text(value) for value in (1, 1.5, 0.25, 2, 1000, 1.004)],
                         ["1 hour", "1.5 hours", "0.25 hours", "2 hours", "1,000 hours", "1 hour"])


class PausedAuthorCase(unittest.TestCase):
    """One job whose author runs (thread-1), with every process start recorded instead of run."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact statement")
        self.run = self.app.run_dir
        self.app._save_controlled("checked-statement.md", "# Checked statement\n\nExact statement\n")
        self.app.state.update(phase="running", stage="solve", activeNode="author", runId=self.run.name,
                              goalThreadId="thread-1", goalWorkspace=str(self.run.resolve()), elapsedSeconds=12,
                              workflowStages=dict(LIVE_STAGES))
        self.app._save_job_settings(self.app.state)
        self.launches, self.runners, self.spawned = [], [], []
        for change in (patch.object(runtime, "verify_model_credentials"),
                       patch.object(server.subprocess, "Popen", side_effect=self.popen),
                       patch.object(server.App, "_spawn_worker", autospec=True, side_effect=self.spawn)):
            change.start()
            self.addCleanup(change.stop)

    def popen(self, argv, **kwargs):
        runner = self.runners.pop(0) if self.runners else FakeRunner()
        self.launches.append((argv, kwargs, runner))
        return runner

    def spawn(self, app, target, args, token):
        app.worker_token = token
        self.spawned.append((target, args))

    def checkpoint(self):
        return json.loads((server.controller_directory(self.run) / server.PAUSE_FILENAME).read_text())

    def labels(self, app=None):
        return [record.get("label") for record in (app or self.app).state["trace"]]

    def runner_ends(self, *records, now=1_000, code=0):
        """The author's runner prints ``records`` and ends; its reader settles the job."""

        token = object()
        process = FakeRunner(records, code)
        self.app.process, self.app.active_token, self.app.worker_token = process, token, token
        with patch.object(server.time, "time", return_value=now):
            self.app._read_output(process, token)

    def time_limit_pause(self, *before, hours=1, elapsed=7200):
        self.app.state["thinkingHours"] = hours
        self.app.state["elapsedSeconds"] = elapsed
        self.app.state["resumedAt"] = self.app.state["startedAt"] = server.datetime.now(server.timezone.utc).isoformat()
        self.runner_ends(*before, {"kind": "workflow_paused", "stage": "solve", "node": "author", "threadId": "thread-1",
                                   "state": {"statement": "Exact statement", "paused": True},
                                   "reason": "Workflow time limit reached."})

    def error_pause(self, reason="Connection lost"):
        self.runner_ends({"kind": "workflow_paused", "stage": "solve", "node": "author", "threadId": "thread-1",
                          "state": {"statement": "Exact statement", "paused": True}, "reason": reason})

    def reply(self, *records, code=0, now=None):
        """Start the due reply and read its runner's ``records`` to the end."""

        self.runners.append(FakeRunner(records, code))
        self.assertTrue(self.app.start_conversation_if_due(now))
        target, args = self.spawned[-1]
        target(*args)
        return self.launches[-1]

    def items(self):
        return self.app.snapshot()["conversation"]["items"]


class TimeLimitTests(PausedAuthorCase):
    def test_the_limit_gate_follows_the_phase_and_the_author_stage(self):
        allowed = {"reviewing", "reviewed", "prepared", "running", "pausing", "paused"}
        refusal = "The time limit can be changed until the job finishes, when the job has an Author stage."
        self.app.process = FakeRunner(code=None)
        for phase in (*sorted(allowed), "input", "stopping", "done"):
            with self.subTest(phase=phase):
                self.app.state.update(phase=phase, activeNode="author", stage="solve")
                if phase in allowed:
                    self.assertEqual(self.app.set_author_time_limit(5), 5)
                    self.assertEqual(json.loads((server.controller_directory(self.run)
                                                 / server.AUTHOR_LIMIT_FILENAME).read_text()), {"hours": 5})
                else:
                    with self.assertRaises(ValueError) as refused:
                        self.app.set_author_time_limit(5)
                    self.assertEqual(str(refused.exception), refusal)
        self.app.state.update(phase="paused", workflowStages={**LIVE_STAGES, "author": False})
        with self.assertRaises(ValueError) as refused:
            self.app.set_author_time_limit(5)
        self.assertEqual(str(refused.exception), refusal)
        # A running job needs its live process, as before.
        self.app.state.update(phase="running", workflowStages=dict(LIVE_STAGES))
        self.app.process = FakeRunner(code=0)
        with self.assertRaisesRegex(ValueError, "no longer running"):
            self.app.set_author_time_limit(5)

    def test_a_limit_set_while_queued_applies_at_launch_and_keeps_the_job_queued(self):
        app = server.App(runs=self.runs, load_latest=False)
        app.start_direct_statement("Prove T.", workflow_stages=dict(LIVE_STAGES), _prepare_only=True)
        self.assertEqual(app.set_author_time_limit(4), 4)
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual((restored.state["phase"], restored.state["thinkingHours"]), ("prepared", 4))
        restored.start_prepared_run()
        argv = self.launches[-1][0]
        self.assertEqual(argv[argv.index("--thinking-hours") + 1], "4.0")
        limit = Path(argv[argv.index("--author-limit-file") + 1])
        self.assertEqual(json.loads(limit.read_text()), {"hours": 4.0})

    def test_the_critic_keeps_its_active_node_and_a_lowered_limit_says_what_follows(self):
        self.app.process = FakeRunner(code=None)
        self.app.state.update(phase="running", stage="critic", activeNode="critic", elapsedSeconds=7200,
                              resumedAt=server.datetime.now(server.timezone.utc).isoformat())
        self.app.set_author_time_limit(1)
        record = self.app.state["trace"][-1]
        self.assertEqual((record["label"], record["node"], self.app.state["activeNode"]),
                         ("Total time limit set", "critic", "critic"))
        self.assertEqual(record["text"], "The total workflow time limit is now 1 hour. That is at or below the "
                                         "2 h of active work so far: the proof author stops at its next step "
                                         "and writes a progress summary; the critic and LaTeX steps still finish.")
        self.app.set_author_time_limit(3)
        self.assertEqual(self.app.state["trace"][-1]["text"], "The total workflow time limit is now 3 hours.")
        self.app.set_author_time_limit(0)
        self.assertEqual(self.app.state["trace"][-1]["text"], "The workflow has no configured time limit.")

    def test_a_paused_job_is_told_to_raise_the_limit_and_nothing_resumes_by_itself(self):
        self.time_limit_pause()
        self.app.set_author_time_limit(2)
        self.assertEqual(self.app.state["trace"][-1]["text"],
                         "The total workflow time limit is now 2 hours. Raise it above the 2 h of active work "
                         "so far, or set 0, to resume.")
        self.app.set_author_time_limit(3)
        self.assertEqual(self.app.state["trace"][-1]["text"], "The total workflow time limit is now 3 hours.")
        self.assertEqual((self.app.state["autoResumeAt"], self.checkpoint()["autoResumeAt"]), (0, 0))

    def test_a_time_limit_pause_asks_once_for_a_summary_and_starts_nothing(self):
        with patch.object(server.subprocess, "Popen", side_effect=AssertionError("launched while reading")):
            self.time_limit_pause()
        checkpoint = self.checkpoint()
        self.assertEqual((checkpoint["pauseCause"], checkpoint["autoResumeAt"], checkpoint["summaryRequested"]),
                         ("time-limit", 0, True))
        self.assertEqual((self.app.state["phase"], self.app.state["autoResumeAt"]), ("paused", 0))
        record = next(record for record in self.app.state["trace"] if record.get("label") == "Time limit reached")
        self.assertAlmostEqual(record["elapsedSeconds"], 7200, delta=5)
        self.assertEqual(record["authorLimitHours"], 1)
        self.assertEqual(record["text"], "The proof author reached the time limit of 1 hour after 2 h of active "
                                         "work and is paused.")
        items = self.items()
        self.assertEqual([(item["kind"], item["origin"], item["status"], item["dueAt"]) for item in items],
                         [("summary", "time-limit", "queued", 0)])
        self.assertIn("Progress summary queued", self.labels())
        state = self.app.snapshot()
        self.assertEqual((state["pauseCause"], state["timeLimitExhausted"]), ("time-limit", True))
        self.assertTrue(state["conversation"]["busy"])
        # The pause asked once: a restart does not ask again.
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual([item["status"] for item in restored._conversation], ["queued"])
        self.assertEqual(restored.snapshot()["pauseCause"], "time-limit")

    def test_the_critic_feedback_the_author_has_not_seen_is_saved_with_the_pause(self):
        reject = {"kind": "critic_result", "stage": "critic", "node": "critic", "round": 1,
                  "report": {"verdict": "reject", "solution": "Proof.", "bugs": "Gap."}}
        self.time_limit_pause(reject)
        self.assertIs(self.checkpoint()["criticFeedbackPending"], True)
        self.assertIs(self.app.snapshot()["criticFeedbackPending"], True)
        # Once the author's turn carries the report, nothing is pending.
        self.app.state.update(phase="running", stage="repair", activeNode="author")
        self.time_limit_pause(reject, {"kind": "request", "stage": "repair", "node": "author",
                                       "label": "Exact solve input", "text": "Repair."})
        self.assertIs(self.checkpoint()["criticFeedbackPending"], False)
        # A resumed run starts from its checkpoint's value.
        self.app.stop_author_reply()
        checkpoint = self.checkpoint()
        checkpoint.update(criticFeedbackPending=True, pauseCause="error", error="Connection lost")
        self.app._save_pause(checkpoint)
        self.app.state.update(thinkingHours=0)
        self.app.worker_token = self.app.active_token = None
        self.app.resume()
        self.assertIs(self.app._carry_critic_feedback, True)
        self.app.state.update(phase="running")
        self.error_pause()
        self.assertIs(self.checkpoint()["criticFeedbackPending"], True)
        self.assertIs(self.app._carry_critic_feedback, False)

    def test_resume_is_refused_while_a_reply_is_due_or_the_limit_is_used_up(self):
        self.time_limit_pause()
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock = self.runs, {self.run.name: self.app}, threading.RLock()
        busy = "The author is replying. Wait for the reply or stop it."
        with patch.object(manager, "_check_author_workspace_idle", side_effect=AssertionError("checked first")):
            for resume in (self.app.resume, lambda: manager.resume_paused_job(self.run.name)):
                with self.assertRaises(ValueError) as refused:
                    resume()
                self.assertEqual(str(refused.exception), busy)
            self.app.stop_author_reply()
            for resume in (self.app.resume, lambda: manager.resume_paused_job(self.run.name)):
                with self.assertRaises(ValueError) as refused:
                    resume()
                self.assertEqual(str(refused.exception), "The time limit is used up (2 h of 1 hour). Raise it, "
                                                         "or set 0 for no limit, then resume.")
        # The job list offers no Resume; the job page raises the limit first.
        self.assertIsNone(server.Server._stopped_continuation_plan(self.app))
        # Less than a minute left is still used up; more is enough.
        self.app.set_author_time_limit((7200 + 59) / 3600)
        self.assertTrue(self.app.snapshot()["timeLimitExhausted"])
        self.app.set_author_time_limit((7200 + 61) / 3600)
        self.assertFalse(self.app.snapshot()["timeLimitExhausted"])
        self.assertEqual(server.Server._stopped_continuation_plan(self.app)["action"], "resume")
        self.app.resume()
        self.assertEqual(self.app.state["phase"], "running")

    def test_a_legacy_time_limit_pause_is_a_time_limit_pause_without_a_summary(self):
        self.error_pause()
        checkpoint = self.checkpoint()
        for key in ("summaryRequested", "criticFeedbackPending"):
            checkpoint.pop(key)
        checkpoint.update(pauseCause="error", error="Workflow time limit reached.", autoResumeAt=0)
        self.app._save_pause(checkpoint)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.snapshot()["pauseCause"], "time-limit")
        self.assertEqual((restored.state["autoResumeAt"], restored._conversation), (0, []))
        # A new pause that never asked for its summary (a crash in between) asks after a restart.
        checkpoint = self.checkpoint()
        checkpoint["summaryRequested"] = False
        self.app._save_pause(checkpoint)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual([(item["kind"], item["origin"]) for item in restored._conversation], [("summary", "time-limit")])
        self.assertIs(self.checkpoint()["summaryRequested"], True)


class ConversationLaunchTests(PausedAuthorCase):
    def setUp(self):
        super().setUp()
        self.error_pause()
        self.app.state["autoResumeAt"] = 0

    def test_a_reply_runs_one_conversation_turn_and_leaves_the_pause_untouched(self):
        pause = (server.controller_directory(self.run) / server.PAUSE_FILENAME).read_bytes()
        item = self.app.ask_author("Which lemma is the obstacle?")
        argv, kwargs, runner = self.reply({"kind": "conversation_reply", "stage": "conversation", "node": "author",
                                           "label": "Author reply", "conversationKind": "message",
                                           "text": "Lemma 3.", "threadId": "thread-1", "compacted": False})
        self.assertEqual(argv[:3], [server.sys.executable, "-u", str(server.ROOT / "workflow_runner.py")])
        self.assertEqual(Path(argv[3]).name, "author_critic.yaml")
        self.assertEqual(argv[4:], [
            "--converse", "--author-model", self.app.state["authorModel"],
            "--author-effort", self.app.state["authorEffort"],
            "--reasoning-effort", self.app.state["reasoningEffort"],
            "--reasoning-summary", self.app.state["reasoningSummary"], "--speed", self.app.state["speedMode"],
            "--thinking-hours", "0",
            "--set", 'workflow_stages={"review":false,"author":true,"critic":false,"writing":false}',
            "--set", 'goal_thread_id="thread-1"', "--set", "goal_cwd=" + json.dumps(str(self.run.resolve())),
            "--set", "goal_require_resume=true",
            # The author's private temp folder, in the controller's folder of the run.
            "--set", "run_tmp_dir=" + json.dumps(str(server.controller_directory(self.run) / "tmp")),
            "--set", "goal_stderr_log=" + json.dumps(str(self.run / server.AUTHOR_STDERR_FILENAME))])
        for absent in ("--author-limit-file", "--author-steer-file", "--start-node", "--elapsed-seconds",
                       "--author-prompt-file", "--state-file"):
            self.assertNotIn(absent, argv)
        self.assertFalse(any(value.startswith("goal_pause_file") for value in argv))
        self.assertEqual(kwargs["cwd"], self.run)
        self.assertEqual(json.loads(runner.stdin.text), {"schemaVersion": 1, "id": item["id"], "kind": "message",
                                                         "text": "Which lemma is the obstacle?", "notice": ""})
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertEqual((server.controller_directory(self.run) / server.PAUSE_FILENAME).read_bytes(), pause)
        answered = self.items()[-1]
        self.assertEqual((answered["status"], answered["reply"], answered["attempts"]), ("answered", "Lemma 3.", 1))
        # Its records are the conversation's, with the item named.
        reply = next(record for record in self.app.state["trace"] if record.get("kind") == "conversation_reply")
        self.assertEqual((reply["stage"], reply["conversationId"]), ("conversation", item["id"]))
        # Only the controller keeps the conversation.
        self.assertTrue((server.controller_directory(self.run) / server.CONVERSATION_FILENAME).is_file())
        self.assertFalse((self.run / server.CONVERSATION_FILENAME).exists())
        self.assertFalse((self.run / server.PROGRESS_SUMMARY_FILENAME).exists())

    def test_a_web_cutoff_reply_keeps_no_stderr_log_and_no_summary_file(self):
        policy = str(self.run / "policy.json")
        self.app.state.update(retrievalMode="historical", retrievalPolicyPath=policy)
        with patch.object(self.app, "_check_historical_locked") as checked, \
                patch.object(server, "require_model_access"), patch.object(server, "require_known_models"), \
                patch.object(server.retrieval_runtime, "load_policy", return_value={"pinned": True}) as loaded, \
                patch.object(server.retrieval_runtime, "controller_audit") as audit:
            item = self.app.request_author_summary()
            argv, _, runner = self.reply({"kind": "conversation_reply", "conversationKind": "summary",
                                          "text": "Proved Lemma 1.", "compacted": False})
        checked.assert_called_once_with()
        # The cutoff audit records what the isolated author receives, before it is sent.
        loaded.assert_called_once_with(policy)
        audit.assert_called_once_with({"pinned": True}, "author_message_sent", conversationId=item["id"],
                                      conversationKind="summary", text="", textChars=0, notice="",
                                      textSha256=server.hashlib.sha256(b"").hexdigest())
        self.assertEqual(json.loads(runner.stdin.text)["id"], item["id"])
        self.assertFalse(any(value.startswith("goal_stderr_log") for value in argv))
        self.assertFalse((self.run / server.PROGRESS_SUMMARY_FILENAME).exists())
        self.assertFalse(self.app.snapshot()["conversation"]["readOnly"])

    def test_a_web_cutoff_message_waits_until_the_cutoff_audit_records_it(self):
        self.app.state.update(retrievalMode="historical", retrievalPolicyPath=str(self.run / "policy.json"))
        refusal = runtime.retrieval_runtime.IsolationCheckUnavailable("The log did not accept the record.")
        with patch.object(self.app, "_check_historical_locked"), \
                patch.object(server, "require_model_access"), patch.object(server, "require_known_models"), \
                patch.object(server.retrieval_runtime, "load_policy", return_value={}), \
                patch.object(server.retrieval_runtime, "controller_audit", side_effect=[refusal, None, None]) as audit:
            self.app.ask_author("Which step is hardest?")
            self.assertTrue(self.app.start_conversation_if_due(1_000))
            # Unrecorded, nothing is sent: the message waits for the usual retry.
            self.assertEqual((self.spawned, self.launches), ([], []))
            [item] = self.items()
            self.assertEqual((item["status"], item["error"]), ("queued", "Could not record the message in the "
                             "cutoff audit: The log did not accept the record. It is tried again in 1 minute."))
            self.assertFalse(self.app.start_conversation_if_due(1_030))
            # Recorded once: a launch that fails afterwards does not record it again.
            with patch.object(self.app, "_spawn_runner_locked", side_effect=OSError("no fork")):
                self.assertTrue(self.app.start_conversation_if_due(1_060))
            self.assertEqual(audit.call_count, 2)
            self.reply({"kind": "conversation_reply", "conversationKind": "message", "text": "Step 3.",
                        "compacted": False})
        self.assertEqual(audit.call_count, 2)
        self.assertEqual((audit.call_args.args[1], audit.call_args.kwargs["text"], audit.call_args.kwargs["textChars"]),
                         ("author_message_sent", "Which step is hardest?", 22))
        self.assertEqual([entry["status"] for entry in self.items()], ["answered"])

    def test_a_live_summary_is_saved_and_a_compacted_turn_re_anchors_the_next_resume(self):
        self.app.request_author_summary()
        self.reply({"kind": "conversation_reply", "conversationKind": "summary", "text": "Lemma 1 holds.",
                    "compacted": True})
        text = (self.run / server.PROGRESS_SUMMARY_FILENAME).read_text()
        self.assertTrue(text.startswith("# Progress summary (") and text.endswith(" UTC)\n\nLemma 1 holds.\n"), text)
        self.assertTrue(self.app.state["pendingReanchor"])
        self.assertTrue(json.loads((server.controller_directory(self.run)
                                    / server.JOB_SETTINGS_FILENAME).read_text())["pendingReanchor"])
        self.app.resume()
        argv = self.launches[-1][0]
        self.assertIn("goal_reanchor=true", argv)
        # The author's next turn carries the full prompt: the re-anchor is done.
        self.app.state.update(phase="running")
        self.runner_ends({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input",
                          "text": "Resume."})
        self.assertFalse(self.app.state["pendingReanchor"])

    def test_failures_end_or_retry_the_item(self):
        self.app.ask_author("First?")
        self.reply({"kind": "status", "stage": "conversation", "label": "Author reply failed", "final": True,
                    "failure": "unavailable", "text": "Codex no longer has the saved author conversation (gone)."})
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["error"]),
                         ("failed", "Codex no longer has the saved author conversation (gone)."))
        self.app.ask_author("Second?")
        self.reply("error: Invalid conversation request.\n", code=1)
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["error"]),
                         ("failed", "Invalid conversation request."))
        self.app.ask_author("Third?")
        with patch.object(server.time, "time", return_value=5_000):
            self.reply(code=-9, now=5_000)
        item = self.items()[-1]
        self.assertEqual((item["status"], item["dueAt"]), ("queued", 5_000 + WAIT))
        self.assertEqual(item["error"], "The reply was interrupted (exit code -9); it is requested again in 1 minute.")
        self.assertFalse(self.app.start_conversation_if_due(5_000 + WAIT - 1))
        self.reply({"kind": "conversation_reply", "text": "Done."}, now=5_000 + WAIT)
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["attempts"]), ("answered", 2))

    def test_a_retry_wait_is_shown_on_the_item(self):
        self.app.ask_author("Why?")
        release = threading.Event()

        def output():
            yield json.dumps({"kind": "status", "stage": "conversation", "label": runtime.RETRY_LABEL,
                              "text": "Model call failed (rate limited) — retrying in 1 min.",
                              "retryAt": 2_000.5}) + "\n"
            release.wait(5)  # The runner waits to retry.
            yield json.dumps({"kind": "conversation_reply", "text": "Because."}) + "\n"

        runner = FakeRunner()
        runner.stdout = output()
        self.runners.append(runner)
        self.assertTrue(self.app.start_conversation_if_due())
        target, args = self.spawned[-1]
        reader = threading.Thread(target=target, args=args, daemon=True)
        reader.start()
        self.addCleanup(release.set)
        for _ in range(500):
            if self.items()[-1].get("retry"):
                break
            threading.Event().wait(0.01)
        self.assertEqual(self.items()[-1]["retry"], {"text": "Model call failed (rate limited) — retrying in 1 min.",
                                                     "retryAt": 2_000.5})
        self.assertTrue(self.app.snapshot()["conversation"]["busy"])
        # The in-memory wait is never saved.
        saved = json.loads((server.controller_directory(self.run) / server.CONVERSATION_FILENAME).read_text())
        self.assertNotIn("retry", saved["items"][-1])
        release.set()
        reader.join(5)
        self.assertEqual(self.items()[-1]["status"], "answered")
        self.assertNotIn("retry", self.items()[-1])


class ConversationRuleTests(PausedAuthorCase):
    def setUp(self):
        super().setUp()
        self.error_pause()
        self.app.state["autoResumeAt"] = 0

    def test_availability_follows_the_paused_author(self):
        talk = "Talking to the author is available while the run is paused at the proof author."
        cases = (({}, ""),
                 ({"phase": "running"}, talk), ({"activeNode": "critic", "stage": "critic"}, talk),
                 ({"workflowStages": {**LIVE_STAGES, "author": False}}, talk),
                 ({"goalThreadId": ""}, "The author has not started its conversation yet."),
                 ({"sessionUnavailable": True}, "The saved author conversation is no longer available."),
                 ({"safetyStop": True}, "The author was stopped for safety; check the setup, then resume."),
                 ({"launchHold": "Check it."}, "Prepared run is held: Check it."),
                 ({"runningElsewhere": "Another process owns it."}, "Another process owns it."))
        saved = dict(self.app.state)
        for change, reason in cases:
            with self.subTest(change=change):
                self.app.state = {**saved, **change}
                conversation = self.app.snapshot()["conversation"]
                self.assertEqual((conversation["available"], conversation["reason"]), (not reason, reason))
                if reason:
                    with self.assertRaises(ValueError) as refused:
                        self.app.ask_author("Hello?")
                    self.assertEqual(str(refused.exception), reason)
        self.app.state = saved
        self.assertTrue(self.app.snapshot()["conversation"]["readOnly"])

    def test_messages_are_checked_and_one_reply_runs_at_a_time(self):
        for text, problem in (("", "Enter a message for the author."), ("   ", "Enter a message for the author."),
                              (None, "Enter a message for the author."),
                              ("a\0b", "The message cannot contain NUL characters."),
                              ("x" * 12_001, "Keep the message at or below 12,000 characters.")):
            with self.assertRaises(ValueError) as refused:
                self.app.ask_author(text)
            self.assertEqual(str(refused.exception), problem)
        item = self.app.ask_author("  " + "x" * 12_000 + "\n")
        self.assertEqual(len(item["text"]), 12_000)
        for ask in (lambda: self.app.ask_author("Again?"), self.app.request_author_summary):
            with self.assertRaises(ValueError) as refused:
                ask()
            self.assertEqual(str(refused.exception), "The author is replying. Wait for the reply or stop it.")
        self.assertEqual(self.labels()[-1], "Message for the paused author queued")

    def test_stop_reply_cancels_the_queued_or_running_item(self):
        with self.assertRaises(ValueError) as refused:
            self.app.stop_author_reply()
        self.assertEqual(str(refused.exception), "No author reply is being written.")
        queued = self.app.ask_author("One?")
        with self.assertRaises(ValueError) as refused:
            self.app.stop_author_reply("c_0000000000")
        self.assertEqual(str(refused.exception), "That reply has already finished.")
        self.app.stop_author_reply(queued["id"])
        self.assertEqual(self.items()[-1]["status"], "stopped")
        self.assertEqual(self.labels()[-1], "Author reply stopped")
        self.assertFalse(self.app.start_conversation_if_due())
        # A running reply's runner is stopped; its reader only saves.
        self.app.ask_author("Two?")
        self.runners.append(FakeRunner([{"kind": "conversation_reply", "text": "Too late."}]))
        self.app.start_conversation_if_due()
        target, args = self.spawned[-1]
        with patch.object(server, "stop_process_tree") as stop:
            self.app.stop_author_reply()
            for _ in range(100):
                if stop.called:
                    break
                threading.Event().wait(0.01)
        stop.assert_called_once_with(args[0])
        target(*args)
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["reply"]), ("stopped", ""))
        self.assertFalse(self.app.has_active_worker())
        self.assertEqual(self.app.state["phase"], "paused")

    def test_the_retry_loop_starts_the_reply_before_an_automatic_resume(self):
        self.app.state["autoResumeAt"] = 500
        checkpoint = self.checkpoint()
        checkpoint["autoResumeAt"] = 500
        self.app._save_pause(checkpoint)
        self.app.ask_author("Before you resume?")
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock = self.runs, {self.run.name: self.app}, threading.RLock()
        manager._retry_stop = threading.Event()
        self.runners.append(FakeRunner([{"kind": "conversation_reply", "text": "Yes."}]))
        with patch.object(self.app, "resume", side_effect=AssertionError("resumed first")):
            manager._retry_paused_jobs(now=1_000)
        self.assertEqual(len(self.launches), 1)
        self.assertIn("--converse", self.launches[0][0])
        self.assertTrue(self.app.has_active_worker())
        self.assertEqual(self.app.state["phase"], "paused")
        # Another job working in this workspace fails the item instead.
        target, args = self.spawned[-1]
        target(*args)
        self.app.ask_author("And now?")
        busy = server.App(runs=self.runs)
        busy.state.update(phase="running", goalWorkspace=str(self.run.resolve()))
        manager.jobs["busy"] = busy
        manager._retry_paused_jobs(now=1_000)
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["error"]),
                         ("failed", "Another job is working in this author workspace. Stop it, then ask again."))

    def test_a_launch_problem_is_retried_after_a_minute(self):
        self.app.ask_author("Hello?")
        with patch.object(server, "require_model_access",
                          side_effect=server.TransientLaunchError("Codex is updating.")):
            self.assertTrue(self.app.start_conversation_if_due(now=1_000))
        self.assertEqual((self.items()[-1]["dueAt"], self.items()[-1]["error"]), (1_000 + WAIT, "Codex is updating."))
        with patch.object(server.subprocess, "Popen", side_effect=OSError("no such file")):
            self.assertTrue(self.app.start_conversation_if_due(now=1_000 + WAIT))
        item = self.items()[-1]
        self.assertEqual((item["status"], item["dueAt"]), ("queued", 1_000 + 2 * WAIT))
        self.assertEqual(item["error"], "Could not start the reply: no such file. It is tried again in 1 minute.")
        self.assertFalse(self.app.has_active_worker())
        with patch.object(server, "require_model_access", side_effect=ValueError("Add an API key.")):
            self.assertTrue(self.app.start_conversation_if_due(now=1_000 + 2 * WAIT))
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["error"]), ("failed", "Add an API key."))

    def test_a_restart_asks_again_for_an_interrupted_reply_and_ends_unfinished_ones_elsewhere(self):
        self.app.ask_author("Interrupted?")
        self.runners.append(FakeRunner())
        self.app.start_conversation_if_due()
        self.assertEqual(self.items()[-1]["status"], "running")
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual([(item["status"], item["dueAt"]) for item in restored._conversation], [("queued", 0)])
        self.assertIn("The author's reply was interrupted by a restart; it is requested again.",
                      [record.get("text") for record in restored.state["trace"]])
        self.assertTrue(restored.conversation_busy())
        # The conversation's records say nothing about the workflow's progress.
        self.assertEqual((restored.state["phase"], restored.state["stage"], restored.state["activeNode"]),
                         ("paused", "solve", "author"))
        # A job that is not paused ends what was unfinished.
        (server.controller_directory(self.run) / server.PAUSE_FILENAME).unlink()
        (self.run / server.PAUSE_FILENAME).unlink(missing_ok=True)
        self.app._save("final.tex", "\\documentclass{article}\n")
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertNotEqual(restored.state["phase"], "paused")
        self.assertEqual([item["status"] for item in restored._conversation], ["stopped"])

    def test_delete_reset_and_clear_wait_for_a_running_reply(self):
        self.app.ask_author("Busy?")
        self.runners.append(FakeRunner())
        self.app.start_conversation_if_due()
        for action, text in ((self.app.clear_trace, "The author is replying. Stop the reply first."),
                             (self.app.reset, "The author is replying. Stop the reply first.")):
            with self.assertRaises(ValueError) as refused:
                action()
            self.assertEqual(str(refused.exception), text)
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock = self.runs, {self.run.name: self.app}, threading.RLock()
        with self.assertRaises(ValueError) as refused:
            manager.delete_job(self.run.name)
        self.assertEqual(str(refused.exception), "The author is replying. Stop the reply before deleting this job.")

    def test_the_run_stays_claimed_while_a_reply_is_due_and_a_closing_server_interrupts_it(self):
        self.app.ask_author("Claimed?")
        with patch.object(self.app._claims, "release_all") as release:
            self.app._settle_run_claim()
        release.assert_not_called()
        runner = FakeRunner(code=None)
        self.runners.append(runner)
        self.app.start_conversation_if_due()
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock = self.runs, {self.run.name: self.app}, threading.RLock()
        with patch.object(server, "stop_process_tree"):
            manager.stop_all(wait=0)
        self.assertEqual(runner.signals, [signal.SIGINT])
        self.assertEqual(self.app.state["phase"], "paused")


class TerminalTests(unittest.TestCase):
    def test_a_headless_run_prints_the_summary(self):
        stream = io.StringIO()
        output = cli.ConciseHeadlessOutput(stream, threading.Lock(), "a.md")
        for record in ({"kind": "request", "stage": "conversation", "label": "Progress summary requested"},
                       {"kind": "conversation_reply", "stage": "conversation", "label": "Progress summary",
                        "conversationKind": "summary", "text": "Lemma 1 is proved.\nLemma 2 is open."},
                       {"kind": "conversation_reply", "stage": "conversation", "conversationKind": "message",
                        "text": "Not printed."},
                       {"kind": "status", "stage": "conversation", "label": "Author reply failed", "final": True,
                        "text": "The saved conversation is gone."}):
            output.write(json.dumps(record) + "\n")
        self.assertEqual(stream.getvalue(), "[a.md] Progress summary:\nLemma 1 is proved.\nLemma 2 is open.\n"
                                            "[a.md] Progress summary failed: The saved conversation is gone.\n")


if __name__ == "__main__":
    unittest.main()
