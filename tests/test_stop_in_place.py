"""Stop works like reaching the time limit (round 4, A2 and A3 a): the job shows Stopped and continues in
place with Continue. Also the Summary button's pause, the restart rules and the terminal. Offline: no model,
no CLI; every runner is a FakeRunner and every stop of a process tree is recorded instead of done."""

import io
import json
import math
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from ui import cli, server
from test_time_limit_conversation import LIVE_STAGES, FakeRunner, PausedAuthorCase

# Node renders the chat in chat_row_status; every runner the server would start stays a FakeRunner.
REAL_POPEN = subprocess.Popen

def chat_row_status(case, snapshot):
    """The status line the job page's chat shows under the newest item of ``snapshot`` (ui/app.js, in Node).

    The server tests replace subprocess.Popen (no runner ever starts); only Node runs here, with the real one.
    """

    if not shutil.which("node"):
        case.skipTest("Node.js is needed to render the chat")
    import test_conversation_ui as page

    job = {key: snapshot.get(key) for key in ("phase", "stage", "activeNode", "pauseCause", "conversation")}
    checks = (f"render({{...author, ...{json.dumps(job)}}});\n"
              "const row = ui.authorConversationList.children.at(-1);\n"
              "console.log(JSON.stringify(row.children.at(-1).textContent));\n")
    payload = json.dumps({"slices": page.RENDER, "setup": page.RENDER_SETUP, "checks": checks})
    with patch.object(subprocess, "Popen", REAL_POPEN):
        result = subprocess.run([shutil.which("node"), "-e", page.RUNNER, str(page.APP)], input=payload,
                                capture_output=True, text=True, timeout=30)
    case.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


AUTHOR_PAUSED = {"kind": "workflow_paused", "stage": "solve", "node": "author", "threadId": "thread-1",
                 "state": {"statement": "Exact statement", "paused": True}, "reason": ""}
STOPPED = {"kind": "status", "stage": "solve", "node": "author", "label": "Author stopped",
           "text": runtime.AUTHOR_STOPPED_TEXT}
STEP_STAGES = {"critic": "critic", "convention": "critic", "latex_editor": "final", "latex_compile": "final"}


class StopCase(PausedAuthorCase):
    """A job whose author works (its runner lives); process trees are never really stopped."""

    def setUp(self):
        super().setUp()
        self.killed, self.watched = [], []
        for change in (patch.object(server, "stop_process_tree", side_effect=self.killed.append),
                       patch.object(server.App, "_watch_author_pause", autospec=True,
                                    side_effect=lambda app, process, token: self.watched.append(token))):
            change.start()
            self.addCleanup(change.stop)
        self.process = FakeRunner(code=None)
        self.token = object()
        self.app.process, self.app.active_token, self.app.worker_token = self.process, self.token, self.token

    def reader_ends(self, *records, code=0, app=None):
        """The live runner prints ``records`` and ends; its own reader settles the job."""

        app = app or self.app
        token = app.active_token or self.token
        app._read_output(FakeRunner(records, code), token)

    def manager(self, *apps):
        manager = object.__new__(server.Server)
        manager.jobs = {app.state["runId"]: app for app in apps or (self.app,)}
        manager.jobs_lock, manager.fixed_app, manager.runs = threading.RLock(), False, self.runs
        return manager

    def restored(self, app=None):
        app = app or self.app
        app.flush_trace(force=True)
        return server.restore_saved_app(server.App(app.trace_file, self.runs))

    def pause_cause(self, app=None):
        return server.read_pause_object((app or self.app).run_dir).get("pauseCause")

    def at_step(self, node, *records):
        for record in records:
            self.app.add_trace(record)
        stage = STEP_STAGES[node]
        self.app.add_trace({"kind": "status", "stage": stage, "node": node, "label": f"Workflow step: {node}",
                            "visit": 1})
        self.app.state.update(phase="running", stage=stage, activeNode=node)

    def candidate(self, text="The complete proof."):
        import hashlib

        (self.run / runtime.SAVED_CANDIDATE_FILENAME).write_text(text, encoding="utf-8")
        return [{"kind": "author_result", "stage": "solve", "node": "author", "text": "PROOF_FILE: proof.md"},
                {"kind": "proof_handoff", "stage": "critic", "node": "critic", "manifest": {
                    "files": {"proof.md": {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}}}}]

    def accepted(self, text="The complete proof."):
        return [{"kind": "author_result", "stage": "solve", "node": "author", "text": "PROOF_FILE: proof.md"},
                {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic", "visit": 1},
                {"kind": "critic_result", "stage": "critic", "node": "critic", "round": 1,
                 "report": {"verdict": "pass", "solution": text, "bugs": "", "fixes": ""}},
                {"kind": "status", "stage": "critic", "node": "critic", "label": "Critic approved", "round": 1}]

    def stop_and_settle(self, words):
        self.app.stop()
        self.assertEqual((self.app.state["phase"], self.killed), ("stopping", [self.process]))
        record = self.app.state["trace"][-1]
        self.assertEqual((record["label"], record["stopInPlace"], record["text"]),
                         ("Stop requested", True, f"Stopping the {words} at once. Continue runs it again in this "
                                                  "same job."))
        self.reader_ends("\nStopped workflow.\n", code=130)


class AuthorStopTests(StopCase):
    def test_the_first_stop_lets_the_author_end_its_step_and_keeps_the_job_in_place(self):
        self.app.stop()
        self.assertEqual((self.app.state["phase"], self.killed, self.watched), ("pausing", [], [self.token]))
        checkpoint = self.checkpoint()
        self.assertEqual((checkpoint["status"], checkpoint["node"], checkpoint["pauseCause"]),
                         ("pausing", "author", "stopped"))
        request = server.controller_directory(self.run) / server.PAUSE_REQUEST_FILENAME
        self.assertEqual(json.loads(request.read_text(encoding="utf-8")), {"cause": "stopped"})
        self.assertEqual(runtime.pause_request_cause(request), "stopped")
        self.assertFalse((self.run / server.MANUAL_STOP_FILENAME).exists())
        record = self.app.state["trace"][-1]
        self.assertEqual((record["label"], record["stopInPlace"], record["text"]),
                         ("Stop requested", True, "Stopping the proof author at its next step: it may finish its "
                          "current step for up to 30 s. Its saved conversation is kept, and Continue picks the work "
                          "up in this same job."))
        snapshot = self.app.snapshot()
        self.assertEqual((snapshot["pauseCause"], snapshot["conversation"]["note"], snapshot["conversation"]["summary"]),
                         ("stopped", server.CHAT_NOTE_STOPPING_AUTHOR, "queue"))
        self.assertEqual(self.manager().job_list()[0]["pauseCause"], "stopped")
        # The runner stops the author at its step boundary, like a pause.
        self.reader_ends(STOPPED, AUTHOR_PAUSED)
        self.assertEqual((self.app.state["phase"], self.app.state["autoResumeAt"], self.app.state["error"]),
                         ("paused", 0, ""))
        checkpoint = self.checkpoint()
        self.assertEqual((checkpoint["status"], checkpoint["pauseCause"], checkpoint["autoResumeAt"]),
                         ("paused", "stopped", 0))
        self.assertFalse(self.app.state["manuallyStopped"])
        self.assertEqual(self.items(), [])  # Only the time limit writes a summary by itself.
        self.assertFalse(self.app.auto_resume_if_due(lambda: self.fail("resumed by itself"), now=time.time() + 10**6))
        row = self.manager().job_list()[0]
        self.assertEqual((row["pauseCause"], row["continueStoppedLabel"], row["continueStoppedDescription"]),
                         ("stopped", "Continue", server.STOPPED_CONTINUE_DESCRIPTIONS["author"]))
        conversation = self.app.snapshot()["conversation"]
        self.assertEqual((conversation["mode"], conversation["summary"]), ("paused", "now"))
        # Continue: the same job and its saved conversation; the time already used still counts.
        self.app.resume()
        argv = self.launches[-1][0]
        self.assertIn("goal_require_resume=true", argv)
        self.assertEqual(argv[argv.index("--start-node") + 1], "author")
        self.assertEqual(float(argv[argv.index("--elapsed-seconds") + 1]), checkpoint["elapsedSeconds"])
        self.assertEqual((self.app.state["phase"], self.labels()[-1]), ("running", "Resume requested"))
        self.assertFalse((server.controller_directory(self.run) / server.PAUSE_REQUEST_FILENAME).exists())

    def test_a_second_stop_stops_at_once_and_the_job_still_stays_in_place(self):
        self.app.stop()
        self.app.stop()
        self.assertEqual(self.killed, [self.process])
        self.assertEqual(self.app.state["trace"][-1]["text"], "Stopping the proof author at once. Its saved "
                                                              "conversation is kept, and Continue picks the work "
                                                              "up in this same job.")
        self.reader_ends(code=130)
        self.assertEqual((self.app.state["phase"], self.pause_cause(), self.app.state["autoResumeAt"]),
                         ("paused", "stopped", 0))

    def test_at_once_the_terminal_stops_the_author_with_one_true_record(self):
        self.app.stop(at_once=True)
        self.assertEqual((self.killed, self.watched), ([self.process], []))
        stops = [record for record in self.app.state["trace"] if record.get("label") == "Stop requested"]
        self.assertEqual([record["text"] for record in stops], ["Stopping the proof author at once. Its saved "
                                                                "conversation is kept, and Continue picks the work up "
                                                                "in this same job."])
        self.reader_ends(code=130)
        self.assertEqual((self.app.state["phase"], self.pause_cause()), ("paused", "stopped"))

    def test_a_stop_without_a_reader_settles_at_once(self):
        self.app.worker_token = None
        self.app.stop(at_once=True)
        self.assertEqual((self.app.state["phase"], self.pause_cause()), ("paused", "stopped"))
        self.assertIsNone(self.app.active_token)

    def test_a_stop_during_a_pause_or_a_summary_pause_becomes_the_stop_and_keeps_the_summary(self):
        self.app.request_author_summary()
        self.assertEqual((self.app.state["phase"], self.pause_cause()), ("pausing", "user"))
        self.assertEqual([(item["kind"], item["status"]) for item in self.items()], [("summary", "queued")])
        self.app.stop()  # While pausing: at once.
        self.assertEqual((self.killed, self.pause_cause()), ([self.process], "stopped"))
        self.reader_ends(AUTHOR_PAUSED)
        self.assertEqual((self.app.state["phase"], self.pause_cause()), ("paused", "stopped"))
        # The summary asked for is still written, now that the author is stopped.
        self.runners.append(FakeRunner([{"kind": "conversation_reply", "text": "Proved: Lemma 1."}]))
        self.assertTrue(self.app.start_conversation_if_due())
        target, args = self.spawned[-1]
        target(*args)
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["reply"]), ("answered", "Proved: Lemma 1."))

    def test_the_time_limit_running_out_during_a_stop_still_shows_stopped(self):
        self.app.stop()
        self.reader_ends({**AUTHOR_PAUSED, "reason": "Workflow time limit reached."})
        self.assertEqual(self.pause_cause(), "stopped")
        self.assertFalse([record for record in self.app.state["trace"] if record.get("label") == "Time limit reached"])

    def test_pause_on_a_stopped_job_keeps_it_stopped(self):
        self.app.stop()
        self.reader_ends(AUTHOR_PAUSED)
        self.app.pause()  # Keep paused.
        self.assertEqual((self.pause_cause(), self.app.state["autoResumeAt"]), ("stopped", 0))

    def test_a_stop_that_cannot_be_recorded_still_ends_the_runner(self):
        with patch.object(server.App, "_save_pause", side_effect=OSError("disk full")):
            self.app.stop()
        self.assertEqual((self.app.state["phase"], self.app.state["manuallyStopped"]), ("stopping", True))
        self.assertEqual(self.killed, [self.process])
        self.reader_ends(code=130)
        self.assertEqual((self.app.state["phase"], self.app.state["error"]), ("done", "Stopped."))


class StepStopTests(StopCase):
    """The critic, the convention critic and the LaTeX steps stop at once and run again with Continue."""

    def test_each_step_stops_at_once_and_continue_runs_it_again_in_this_job(self):
        runtime.save_final_input("Exact statement", "The complete proof.", directory=self.run)
        cases = (("critic", "critic", self.candidate, {}),
                 ("convention", "convention critic", self.accepted, {}),
                 ("latex_editor", "LaTeX editor", self.accepted, {}),
                 ("latex_compile", "PDF compilation", self.accepted, {"latex": True}))
        for node, words, records, options in cases:
            with self.subTest(node=node):
                self.setUp_case()
                if options.get("latex"):
                    (self.run / "latex-source.tex").write_text("\\documentclass{article}", encoding="utf-8")
                self.at_step(node, *records())
                self.stop_and_settle(words)
                self.assertEqual((self.app.state["phase"], self.app.state["activeNode"], self.app.state["error"]),
                                 ("paused", node, ""))
                checkpoint = self.checkpoint()
                self.assertEqual((checkpoint["node"], checkpoint["pauseCause"], checkpoint["autoResumeAt"]),
                                 (node, "stopped", 0))
                self.assertFalse(self.app.state["manuallyStopped"])
                snapshot = self.app.snapshot()
                self.assertEqual(snapshot["pauseCause"], "stopped")
                plan = server.Server._stopped_continuation_plan(self.app)
                self.assertEqual((plan["action"], plan["label"], plan["description"]),
                                 ("resume", "Continue", server.STOPPED_CONTINUE_DESCRIPTIONS[node]))
                # Continue: the same step with its exact saved input, in this job.
                self.app.resume()
                argv = self.launches[-1][0]
                self.assertEqual(argv[argv.index("--start-node") + 1], node)
                given = json.loads((self.run / "workflow-input.json").read_text(encoding="utf-8"))
                self.assertEqual(given.get("solution") or given.get("output"), checkpoint["state"].get("solution")
                                 or checkpoint["state"].get("output"))
                self.assertEqual(self.labels()[-1], "Resume requested")
                (self.run / "latex-source.tex").unlink(missing_ok=True)

    def setUp_case(self):
        """A fresh job for one sub-test (the run folder of the previous one is left behind)."""

        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact statement")
        self.run = self.app.run_dir
        self.app._save_controlled("checked-statement.md", "# Checked statement\n\nExact statement\n")
        runtime.save_final_input("Exact statement", "The complete proof.", directory=self.run)
        self.app.state.update(phase="running", stage="solve", activeNode="author", runId=self.run.name,
                              goalThreadId="thread-1", goalWorkspace=str(self.run.resolve()), elapsedSeconds=12,
                              workflowStages=dict(LIVE_STAGES))
        self.app._save_job_settings(self.app.state)
        self.process, self.token, self.killed[:] = FakeRunner(code=None), object(), []
        self.app.process, self.app.active_token, self.app.worker_token = self.process, self.token, self.token

    def test_a_rejection_just_before_the_stop_continues_with_the_authors_repair(self):
        self.at_step("critic", *self.candidate(),
                     {"kind": "critic_result", "stage": "critic", "node": "critic", "round": 1,
                      "report": {"verdict": "reject", "solution": "Draft.", "bugs": "Gap in Lemma 2.", "fixes": ""}})
        self.stop_and_settle("critic")
        checkpoint = self.checkpoint()
        self.assertEqual((checkpoint["node"], checkpoint["stage"], checkpoint["pauseCause"]),
                         ("author", "repair", "stopped"))
        self.assertEqual(self.app.state["activeNode"], "author")

    def test_a_stop_while_a_pause_lands_at_the_next_step_settles_as_stopped_there(self):
        # A Pause reached the step boundary: the reader moved to the critic and saved its pausing checkpoint.
        self.at_step("critic", *self.candidate())
        self.app._save_pause({"status": "pausing", "node": "critic", "stage": "critic", "pauseCause": "user",
                              "state": {"statement": "Exact statement", "solution": "The complete proof."},
                              "elapsedSeconds": 30, "autoResumeAt": 0})
        self.app.state["phase"] = "pausing"
        self.stop_and_settle("critic")
        self.assertEqual((self.app.state["phase"], self.app.state["activeNode"], self.pause_cause()),
                         ("paused", "critic", "stopped"))

    def test_a_step_with_nothing_to_rebuild_ends_as_stopped_with_todays_buttons(self):
        self.app.state.update(phase="running", stage="failure", activeNode="failure_summary")
        self.app.add_trace({"kind": "status", "stage": "failure", "node": "failure_summary", "text": "Saving."})
        (server.controller_directory(self.run) / "checked-statement.md").unlink()
        self.app.stop()
        self.reader_ends(code=130)
        self.assertEqual((self.app.state["phase"], self.app.state["error"], self.app.state["manuallyStopped"]),
                         ("done", "Stopped.", True))
        marker = json.loads((self.run / server.MANUAL_STOP_FILENAME).read_text(encoding="utf-8"))
        self.assertNotIn("inPlace", marker)

    def failing_chat_saves(self):
        """The disk refuses conversation.json (it is full); every other file is still saved."""

        real = server.App._save_controlled

        def save(app, name, *args, **kwargs):
            if name == server.CONVERSATION_FILENAME:
                raise OSError(28, "No space left on device")
            return real(app, name, *args, **kwargs)

        return patch.object(server.App, "_save_controlled", autospec=True, side_effect=save)

    def capture_answer_unsaved(self, item):
        """The author's answer to ``item`` is shown while conversation.json cannot be saved."""

        (self.app._author_workspace() / item["answerFile"]).write_text("Lemma 3.", encoding="utf-8")
        self.app.collect_chat_answers(now=100.0)
        with self.failing_chat_saves():
            self.app.collect_chat_answers(now=103.0)
        self.assertEqual(self.items()[0]["reply"], "Lemma 3.")
        record = next(record for record in reversed(self.app.state["trace"]) if record.get("kind") == "diagnostic")
        self.assertEqual(record["stage"], "conversation")
        self.assertNotIn("node", record)

    def test_a_chat_save_that_fails_while_the_critic_works_leaves_the_critic_the_active_step(self):
        item = self.app.send_chat("Which lemma are you on?")  # While the author worked.
        self.at_step("critic", *self.candidate())
        self.capture_answer_unsaved(item)
        self.assertEqual((self.app.state["phase"], self.app.state["stage"], self.app.state["activeNode"]),
                         ("running", "critic", "critic"))
        self.assertEqual(self.app.snapshot()["conversation"]["note"], server.CHAT_NOTE_CRITIC)
        # A Stop now stops the critic at once (not the author), and Continue runs the critic again.
        self.stop_and_settle("critic")
        self.assertEqual((self.app.state["phase"], self.app.state["activeNode"], self.pause_cause()),
                         ("paused", "critic", "stopped"))

    def test_a_chat_save_that_fails_while_stopped_at_the_critic_keeps_its_continue(self):
        item = self.app.send_chat("Which lemma are you on?")
        self.at_step("critic", *self.candidate())
        self.stop_and_settle("critic")
        self.capture_answer_unsaved(item)  # A late answer the author wrote just before it handed in.
        snapshot = self.app.snapshot()
        self.assertEqual((snapshot["phase"], snapshot["activeNode"]), ("paused", "critic"))
        self.assertEqual(snapshot["conversation"]["note"],
                         server.CHAT_NOTE_HELD_CRITIC.format(held="stopped", go="continue"))
        self.assertEqual(self.manager().job_list()[0]["activeNode"], "critic")
        # The author's time limit is used up: the critic step still continues (only the author waits for a higher
        # limit).
        self.app.state.update(thinkingHours=1, elapsedSeconds=4000)
        checkpoint = self.checkpoint()
        checkpoint["elapsedSeconds"] = 4000
        self.app._save_pause(checkpoint)
        self.assertEqual(self.app.resume_refusal(), "")
        plan = server.Server._stopped_continuation_plan(self.app)
        self.assertEqual((plan["action"], plan["label"], plan["description"]),
                         ("resume", "Continue", server.STOPPED_CONTINUE_DESCRIPTIONS["critic"]))

    def test_a_reply_stopped_while_the_pause_holds_the_job_at_the_critic_leaves_the_critic_there(self):
        # Pause and summarize met an author that had just handed in: the job paused at the critic with its summary
        # still queued, and the user stops that reply before the server's next check ends it.
        self.app.request_author_summary()
        self.reader_ends({"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"},
                         {"kind": "workflow_paused", "stage": "critic", "node": "critic",
                          "state": {"statement": "Exact statement", "solution": "Proof."},
                          "reason": "Paused before the next workflow step."})
        self.app.stop_author_reply()
        self.assertNotIn("node", self.app.state["trace"][-1])
        self.assertEqual((self.app.state["phase"], self.app.state["activeNode"]), ("paused", "critic"))

    def test_a_result_that_arrives_just_before_the_stop_wins(self):
        self.at_step("latex_compile", *self.accepted())
        self.app.stop()
        self.reader_ends({"kind": "final_result", "stage": "final", "node": "latex_compile",
                          "output": "\\documentclass{article}", "resultKind": "final"})
        self.assertEqual((self.app.state["phase"], self.app.state["error"], self.app.state["resultKind"]),
                         ("done", "", "final"))
        self.assertFalse(self.app.state["manuallyStopped"])


class ReviewStopTests(StopCase):
    def review_job(self):
        app = server.App(runs=self.runs, load_latest=False)
        with patch.object(runtime, "verify_model_credentials"):
            app.start_review("Prove that 1 + 1 = 2.")
        return app

    def run_review(self, app, *records):
        """The review worker the job started runs now, reading ``records`` from its reviewer."""

        if records:
            self.runners.append(FakeRunner(records))
        target, args = next(spawned for spawned in reversed(self.spawned) if spawned[0].__self__ is app)
        target(*args)

    def test_a_stopped_first_review_returns_to_its_statement_and_continues_in_place(self):
        app = self.review_job()
        app.stop()
        self.assertEqual(app.state["phase"], "stopping")
        self.run_review(app)
        self.assertEqual((app.state["phase"], app.state["error"], app.state["manuallyStopped"]),
                         ("input", server.REVIEW_STOPPED_TEXT, True))
        marker = json.loads((server.controller_directory(app.run_dir) / server.MANUAL_STOP_FILENAME).read_text())
        self.assertEqual((marker["stage"], marker["inPlace"]), ("review", True))
        plan = server.Server._stopped_continuation_plan(app)
        self.assertEqual((plan["action"], plan["label"], plan.get("inPlace")), ("review", "Continue", True))
        self.assertEqual(self.manager(app).job_list()[0]["continueStoppedLabel"], "Continue")
        # A restart restores the same, and the review never starts again by itself.
        restored = self.restored(app)
        self.assertEqual((restored.state["phase"], restored.state["error"], restored.state["manuallyStopped"],
                          restored.state.get("reviewRetryAt")), ("input", server.REVIEW_STOPPED_TEXT, True, 0))
        # A marker the author planted in its run folder is not trusted to say "in place".
        (server.controller_directory(app.run_dir) / server.MANUAL_STOP_FILENAME).unlink()
        self.assertFalse(server.saved_manual_stop(app.run_dir)["inPlace"])
        app._save_controlled(server.MANUAL_STOP_FILENAME, json.dumps(marker))
        # Continue runs the review again in this job; its marker goes.
        with patch.object(runtime, "verify_model_credentials"):
            app.retry_review()
        self.assertEqual((app.state["phase"], app.state["manuallyStopped"]), ("reviewing", False))
        for folder in (app.run_dir, server.controller_directory(app.run_dir)):
            self.assertFalse((folder / server.MANUAL_STOP_FILENAME).exists())

    def test_a_stopped_revision_returns_to_the_review_and_approving_it_retries_failures_again(self):
        app = self.review_job()
        self.run_review(app, {"kind": "request", "stage": "review", "label": "Statement review"},
                        {"kind": "review_result", "stage": "review",
                         "review": {"statement": "1 + 1 = 2 in N.", "notes": "None."}})
        self.assertEqual(app.state["phase"], "reviewed")
        with patch.object(runtime, "verify_model_credentials"):
            app.start_review("Prove that 1 + 1 = 2.", feedback="Name the semiring.", revise_existing=True)
        app.add_trace({"kind": "request", "stage": "review", "label": "Statement review"})
        app.stop()
        self.run_review(app)
        # Back at the earlier review, whose page reruns it with Retry with feedback (it has no Start workflow).
        self.assertEqual((app.state["phase"], app.state["error"], app.state["manuallyStopped"]),
                         ("reviewed", server.REVISION_STOPPED_TEXT, True))
        self.assertEqual(server.REVISION_STOPPED_TEXT,
                         "You stopped the statement review. Retry with feedback runs it again in this job.")
        restored = self.restored(app)
        self.assertEqual((restored.state["phase"], restored.state["error"], restored.state["manuallyStopped"]),
                         ("reviewed", server.REVISION_STOPPED_TEXT, True))
        # Approving the earlier review starts the proof: the stop is over, and a failed model call retries.
        with patch.object(runtime, "verify_model_credentials"):
            app.approve()
        self.assertEqual((app.state["phase"], app.state["manuallyStopped"]), ("running", False))
        self.assertFalse((server.controller_directory(app.run_dir) / server.MANUAL_STOP_FILENAME).exists())
        app.state["goalThreadId"] = "thread-9"
        self.reader_ends({"kind": "workflow_paused", "stage": "solve", "node": "author", "threadId": "thread-9",
                          "state": {"statement": "1 + 1 = 2 in N.", "paused": True},
                          "reason": "Model call failed (connection lost)."}, app=app)
        self.assertEqual((app.state["phase"], self.pause_cause(app)), ("paused", "error"))
        self.assertGreater(app.state["autoResumeAt"], 0)

    def test_after_a_continued_review_a_crash_in_the_critic_still_resumes_by_itself(self):
        app = self.review_job()
        app.stop()
        self.run_review(app)
        with patch.object(runtime, "verify_model_credentials"):
            app.retry_review()
        self.run_review(app, {"kind": "request", "stage": "review", "label": "Statement review"},
                        {"kind": "review_result", "stage": "review",
                         "review": {"statement": "1 + 1 = 2 in N.", "notes": "None."}})
        with patch.object(runtime, "verify_model_credentials"):
            app.approve()
        (app.run_dir / runtime.SAVED_CANDIDATE_FILENAME).write_text("The proof.", encoding="utf-8")
        for record in ({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"},
                       {"kind": "author_result", "stage": "solve", "node": "author", "text": "The proof."},
                       {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic",
                        "visit": 1}):
            app.add_trace(record)
        restored = self.restored(app)  # The server crashed while the critic ran.
        self.assertEqual((restored.state["phase"], restored.state["activeNode"], self.pause_cause(restored)),
                         ("paused", "critic", "interrupted"))
        self.assertGreater(restored.state["autoResumeAt"], 0)


class RestartTests(StopCase):
    def test_a_crash_while_stopping_restores_stopped_and_it_never_resumes_by_itself(self):
        self.app.stop()  # The author is still ending its step when the server crashes.
        restored = self.restored()
        self.assertEqual((restored.state["phase"], self.pause_cause(restored), restored.state["autoResumeAt"]),
                         ("paused", "stopped", 0))

    def test_a_damaged_checkpoint_after_a_stop_is_rebuilt_as_stopped(self):
        self.app.stop()
        self.reader_ends(AUTHOR_PAUSED)
        for folder in (self.run, server.controller_directory(self.run)):
            (folder / server.PAUSE_FILENAME).write_text("{", encoding="utf-8")
        restored = self.restored()
        self.assertEqual((restored.state["phase"], self.pause_cause(restored), restored.state["autoResumeAt"]),
                         ("paused", "stopped", 0))

    def test_a_crash_in_a_stopped_step_restores_it_stopped(self):
        (self.run / runtime.SAVED_CANDIDATE_FILENAME).write_text("The proof.", encoding="utf-8")
        for record in ({"kind": "author_result", "stage": "solve", "node": "author", "text": "The proof."},
                       {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic",
                        "visit": 1}):
            self.app.add_trace(record)
        self.app.state.update(stage="critic", activeNode="critic")
        self.app.stop()  # The server crashes before the reader settles the stop.
        restored = self.restored()
        self.assertEqual((restored.state["phase"], restored.state["activeNode"], self.pause_cause(restored)),
                         ("paused", "critic", "stopped"))
        self.assertEqual(restored.state["autoResumeAt"], 0)
        # Records the stopped runner wrote after the Stop never lift it.
        self.app.add_trace({"kind": "status", "stage": "critic", "node": "convention",
                            "label": "Workflow step: convention", "visit": 1})
        self.assertEqual(self.pause_cause(self.restored()), "stopped")

    def test_a_stop_of_an_earlier_version_still_ends_the_job(self):
        self.app.state.update(phase="done", error="Stopped.")
        self.app._save(server.MANUAL_STOP_FILENAME, json.dumps({"schemaVersion": 1, "stage": "solve", "node": "author",
                                                                "problemMode": "statement", "stoppedAt": ""}))
        self.app.add_trace({"kind": "status", "stage": "solve", "node": "author", "label": "Stop requested",
                            "text": "The running model and any subagents are being stopped."})
        restored = self.restored()
        self.assertEqual((restored.state["phase"], restored.state["error"], restored.state["manuallyStopped"]),
                         ("done", "Stopped.", True))
        self.assertEqual(server.Server._stopped_continuation_plan(restored)["label"], "Continue proof author")

    def continued_after_an_earlier_versions_stop(self):
        """A web-cutoff job that an earlier version stopped (its record and marker) and that Continue, after the
        update, continues in its own folder (its only continuation); its author works again."""

        self.app.add_trace({"kind": "status", "stage": "solve", "node": "author", "label": "Stop requested",
                            "text": "The running model and any subagents are being stopped."})
        self.app._save(server.MANUAL_STOP_FILENAME, json.dumps({"schemaVersion": 1, "stage": "solve", "node": "author",
                                                                "problemMode": "statement", "stoppedAt": ""}))
        self.app.process = self.app.active_token = self.app.worker_token = None
        self.app.state.update(phase="done", error="Stopped.", manuallyStopped=True, stoppedStage="solve",
                              retrievalMode="historical", retrievalPolicyPath=str(self.run / "retrieval-policy.json"))
        self.app._save_job_settings(self.app.state)
        self.assertEqual(self.restored().state["error"], "Stopped.")
        with patch.object(server.retrieval, "check_run", return_value=self.app.state["retrievalPolicyPath"]):
            self.app.continue_historical("author")
        self.assertEqual((self.app.state["phase"], self.labels()[-1]), ("running", "Historical run continued"))
        self.app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})

    def test_a_restart_after_an_earlier_versions_stop_was_continued_resumes_by_itself(self):
        self.continued_after_an_earlier_versions_stop()
        # TCS Prover closes: the author pauses for the restart, to continue by itself when it starts again.
        self.app.pause(_for_restart=True)
        self.reader_ends(AUTHOR_PAUSED)
        self.assertEqual((self.app.state["phase"], self.pause_cause()), ("paused", "restart"))
        restored = self.restored()
        self.assertEqual((restored.state["phase"], restored.state["error"], restored.state["manuallyStopped"],
                          self.pause_cause(restored)), ("paused", "", False, "restart"))
        self.assertGreater(restored.state["autoResumeAt"], 0)

    def test_a_crash_in_the_critic_after_an_earlier_versions_stop_was_continued_resumes_by_itself(self):
        self.continued_after_an_earlier_versions_stop()
        (self.run / runtime.SAVED_CANDIDATE_FILENAME).write_text("The proof.", encoding="utf-8")
        for record in ({"kind": "author_result", "stage": "solve", "node": "author", "text": "The proof."},
                       {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic",
                        "visit": 1}):
            self.app.add_trace(record)
        restored = self.restored()  # The server crashed while the critic ran.
        self.assertEqual((restored.state["phase"], restored.state["activeNode"], restored.state["manuallyStopped"],
                          self.pause_cause(restored)), ("paused", "critic", False, "interrupted"))
        self.assertGreater(restored.state["autoResumeAt"], 0)

    def test_continue_after_a_stop_at_the_critic_lets_a_later_crash_resume_by_itself(self):
        self.at_step("critic", *self.candidate())
        self.stop_and_settle("critic")
        self.assertEqual(self.pause_cause(), "stopped")
        self.app.resume()  # Continue: the critic runs again in this job.
        self.assertEqual(self.labels()[-1], "Resume requested")
        self.app.add_trace({"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic",
                            "visit": 2})
        restored = self.restored()  # The server crashed while the critic ran again.
        self.assertEqual((restored.state["phase"], restored.state["activeNode"], self.pause_cause(restored)),
                         ("paused", "critic", "interrupted"))
        self.assertGreater(restored.state["autoResumeAt"], 0)

    def test_continue_after_a_stop_of_the_author_lets_a_later_crash_in_the_critic_resume_by_itself(self):
        self.app.stop()
        self.reader_ends(STOPPED, AUTHOR_PAUSED)
        self.assertEqual(self.pause_cause(), "stopped")
        self.app.resume()  # Continue: the author works again in its saved conversation.
        self.assertEqual(self.labels()[-1], "Resume requested")
        self.at_step("critic", *self.candidate())
        restored = self.restored()  # The server crashed while the critic ran.
        self.assertEqual((restored.state["phase"], restored.state["activeNode"], self.pause_cause(restored)),
                         ("paused", "critic", "interrupted"))
        self.assertGreater(restored.state["autoResumeAt"], 0)


class HeldStopTests(StopCase):
    """A web-cutoff job you stopped that a historical check holds (a message to the stopped author found a problem):
    a check that passes releases the hold, and the job stays stopped until Continue, also when the check that
    passed ran by itself."""

    def held(self, problem):
        self.app.stop()
        self.reader_ends(STOPPED, AUTHOR_PAUSED)
        self.app.state.update(retrievalMode="historical", retrievalPolicyPath=str(self.run / "retrieval-policy.json"))
        self.app._save_job_settings(self.app.state)
        with self.app.lock:
            self.app._hold_after_failed_check(problem)
        self.launches.clear()

    def checks_pass(self):
        for change in (patch.object(server.retrieval, "check_run", return_value=self.app.state["retrievalPolicyPath"]),
                       patch.object(server.retrieval, "preflight_policy"),
                       patch.object(server.retrieval, "preflight_compiler")):
            change.start()
            self.addCleanup(change.stop)

    def assert_still_stopped(self):
        self.assertEqual(self.launches, [])
        self.assertEqual((self.app.state["phase"], self.app.state["launchHold"], self.pause_cause(),
                          self.app.state["autoResumeAt"]), ("paused", "", "stopped", 0))
        self.assertEqual(self.app.launch_due_at(time.time() + 10**6), (math.inf, 0))
        plan = server.Server._stopped_continuation_plan(self.app)
        self.assertEqual((plan["action"], plan["label"]), ("resume", "Continue"))

    def test_re_run_preflight_releases_the_hold_and_the_job_stays_stopped(self):
        self.held(server.retrieval.NeedsAttention("The Codex binary changed."))
        plan = server.Server._stopped_continuation_plan(self.app)
        self.assertEqual((plan["action"], plan["label"], plan["description"]),
                         ("preflight", "Re-run preflight", "Checks this run's policy, logins, sandboxes and compiler "
                          "again; once they pass, the hold is released. The job stays stopped until you choose "
                          "Continue."))
        self.checks_pass()
        self.app.rerun_historical_preflight()
        self.assert_still_stopped()

    def test_the_minutely_check_of_a_hold_that_could_not_finish_never_continues_it(self):
        self.held(server.retrieval.PreflightRetry("Historical run check could not finish: offline."))
        self.assertTrue(self.app.state["launchHold"].endswith(server.HISTORICAL_RETRY_SUFFIX))
        self.checks_pass()

        class Inline:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with patch.object(server.threading, "Thread", Inline):
            self.assertTrue(self.app.retry_historical_hold_if_due(time.time() + server.AUTO_RESUME_SECONDS + 1))
        self.assert_still_stopped()


class Unresponsive(FakeRunner):
    """A runner that never ends its step: every wait for it runs out."""

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("runner", timeout)


class UnresponsiveAuthorTests(PausedAuthorCase):
    """An author that does not end its step in time is closed; the activity log names the state the job then shows
    and the button that picks it up."""

    def closed_after(self, request):
        process, token = Unresponsive(code=None), object()
        self.app.process, self.app.active_token, self.app.worker_token = process, token, token
        watchers, real = [], threading.Thread

        def thread(*args, target=None, **kwargs):
            watchers.append(target)  # Run below, as if PAUSE_ACKNOWLEDGE_SECONDS had passed.
            return real(target=lambda: None)

        with patch.object(server, "stop_process_tree"), patch.object(server.threading, "Thread", side_effect=thread):
            request()
        with patch.object(server, "stop_process_tree") as stopped, \
                patch.object(server.App, "_settle_pause_without_reader"):
            watchers[0]()
        stopped.assert_called_once_with(process)
        return self.app.state["trace"][-1]

    def test_after_a_stop_it_says_the_author_did_not_stop_and_names_continue(self):
        record = self.closed_after(self.app.stop)
        self.assertEqual((record["kind"], record["text"]),
                         ("diagnostic", "The author did not stop within 45 s; closing its process. Continue reopens "
                                        "its saved conversation."))
        self.assertEqual(server.read_pause_object(self.run)["pauseCause"], "stopped")

    def test_after_a_pause_it_still_names_resume(self):
        record = self.closed_after(self.app.pause)
        self.assertEqual((record["kind"], record["text"]),
                         ("diagnostic", "The author did not acknowledge the pause; closing its process. Resume will "
                                        "reopen the saved conversation."))


class SummaryButtonTests(StopCase):
    def test_while_the_author_works_it_pauses_and_writes_the_summary_once_paused(self):
        self.assertEqual(self.app.snapshot()["conversation"]["summary"], "pause")
        self.app.request_author_summary()
        self.assertEqual((self.app.state["phase"], self.pause_cause(), self.watched), ("pausing", "user", [self.token]))
        self.assertEqual(self.app.state["trace"][-2]["text"], "Interrupting the author and preserving its saved "
                                                              "conversation; it then writes the progress summary "
                                                              "you asked for. Resume continues the work.")
        self.assertEqual([(item["kind"], item["status"]) for item in self.items()], [("summary", "queued")])
        self.assertFalse(self.app.start_conversation_if_due())  # Not while it pauses.
        self.reader_ends(AUTHOR_PAUSED)
        self.assertEqual((self.app.state["phase"], self.pause_cause()), ("paused", "user"))
        self.assertTrue(self.app.start_conversation_if_due())
        self.assertEqual(self.items()[-1]["status"], "running")
        self.assertEqual(self.app.resume_refusal(), server.CONVERSATION_BUSY)

    def test_while_pausing_it_waits_and_while_paused_it_writes_at_once(self):
        self.app.pause()
        self.assertEqual(self.app.snapshot()["conversation"]["summary"], "queue")
        self.app.request_author_summary()
        self.assertEqual(self.items()[-1]["status"], "queued")
        self.assertEqual(self.app.snapshot()["conversation"]["summaryNote"], server.CHAT_NOTE_SUMMARY_QUEUED)
        self.reader_ends(AUTHOR_PAUSED)
        self.app.stop_author_reply()
        self.assertEqual(self.app.snapshot()["conversation"]["summary"], "now")
        self.app.request_author_summary()
        self.assertEqual(self.items()[-1]["status"], "queued")

    def test_while_the_author_stops_the_summary_waits_and_never_says_paused(self):
        self.app.stop()  # The author ends its current step: the job shows Stopping.
        self.assertEqual(self.app.snapshot()["conversation"]["summary"], "queue")
        self.app.request_author_summary()
        snapshot = self.app.snapshot()
        self.assertEqual((snapshot["conversation"]["summary"], snapshot["conversation"]["summaryNote"]),
                         ("off", server.CHAT_NOTE_SUMMARY_QUEUED_STOP))
        self.assertEqual(server.CHAT_NOTE_SUMMARY_QUEUED_STOP,
                         "A progress summary is already asked for; the author writes it once it has stopped.")
        # As the chat shows it.
        self.assertEqual(chat_row_status(self, snapshot), "The author is stopping; it writes the summary once it "
                                                          "has stopped.")
        # Stopped: the summary is written at once, and Continue still picks the work up.
        self.reader_ends(STOPPED, AUTHOR_PAUSED)
        self.assertEqual((self.app.state["phase"], self.pause_cause()), ("paused", "stopped"))
        self.assertTrue(self.app.start_conversation_if_due())
        self.assertEqual(self.items()[-1]["status"], "running")

    def test_without_its_conversation_the_author_cannot_summarize_yet(self):
        self.app.state["goalThreadId"] = ""
        conversation = self.app.snapshot()["conversation"]
        self.assertEqual((conversation["summary"], conversation["summaryNote"]), ("off", server.CHAT_NOTE_NO_THREAD))
        with self.assertRaises(ValueError) as refused:
            self.app.request_author_summary()
        self.assertEqual(str(refused.exception), server.CHAT_NOTE_NO_THREAD)
        self.assertEqual(self.app.state["phase"], "running")

    def test_a_pause_that_lands_at_the_critic_says_why_there_is_no_summary(self):
        self.app.request_author_summary()
        self.reader_ends({"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"},
                         {"kind": "workflow_paused", "stage": "critic", "node": "critic",
                          "state": {"statement": "Exact statement", "solution": "Proof."},
                          "reason": "Paused before the next workflow step."})
        self.assertEqual((self.app.state["phase"], self.app.state["activeNode"]), ("paused", "critic"))
        self.assertTrue(self.app.start_conversation_if_due())
        missed = ("The author handed in its proof just before the pause, so the job paused at the critic step, "
                  "where the author cannot write a summary.")
        self.assertEqual((self.items()[-1]["status"], self.items()[-1]["error"]), ("failed", missed))
        # As the chat shows it: one lead-in, never "No reply: No summary: …".
        self.assertEqual(chat_row_status(self, self.app.snapshot()), f"No reply: {missed}")

    def test_a_summary_the_job_ended_before_says_so_and_frees_the_chat(self):
        # Pause and summarize in a job without critic or LaTeX: the author hands in just as the pause is asked
        # for, nothing follows it, and the workflow ends instead of pausing.
        self.app.state["workflowStages"] = {"review": False, "author": True, "critic": False, "writing": False}
        self.app._save_job_settings(self.app.state)
        self.app.request_author_summary()
        self.reader_ends({"kind": "author_result", "stage": "solve", "node": "author", "text": "PROOF_FILE: proof.md"},
                         {"kind": "final_result", "stage": "solve", "node": "author", "output": "The proof.",
                          "resultKind": "proof"})
        snapshot = self.app.snapshot()
        self.assertEqual(snapshot["phase"], "done")
        self.assertEqual([(item["kind"], item["status"], item["error"]) for item in snapshot["conversation"]["items"]],
                         [("summary", "failed", server.CHAT_SUMMARY_ENDED)])
        self.assertFalse(snapshot["conversation"]["busy"])
        self.assertEqual(server.CHAT_SUMMARY_ENDED, "The job ended before the author paused, so it wrote no summary.")
        saved = json.loads((server.controller_directory(self.run) / server.CONVERSATION_FILENAME).read_text())
        self.assertEqual(saved["items"][0]["status"], "failed")
        self.assertFalse(self.app.start_conversation_if_due())
        self.assertEqual(chat_row_status(self, snapshot), f"No reply: {server.CHAT_SUMMARY_ENDED}")
        # A restart keeps how it ended.
        self.assertEqual([item["status"] for item in self.restored()._conversation], ["failed"])

    def test_a_summary_queued_when_a_stop_ends_the_job_says_so(self):
        # The stop's own records cannot be written (a full disk), so it ends the job as earlier versions did, with
        # no reader left to settle it.
        self.app.request_author_summary()
        self.app.worker_token = None
        with patch.object(server.App, "_save_pause", side_effect=OSError("disk full")):
            self.app.stop()
        self.assertEqual((self.app.state["phase"], self.app.state["error"]), ("done", "Stopped."))
        self.assertEqual([(item["status"], item["error"]) for item in self.items()],
                         [("failed", server.CHAT_SUMMARY_ENDED)])
        self.assertFalse(self.app.snapshot()["conversation"]["busy"])


class TerminalTests(StopCase):
    def test_ctrl_c_stops_at_once_and_the_report_says_how_to_continue(self):
        with patch.object(server.App, "stop", autospec=True) as stopped:
            cli._stop_headless_apps([(None, self.app)])
        stopped.assert_called_once_with(self.app, at_once=True)
        self.app.stop(at_once=True)
        self.reader_ends(code=130)
        source = type("Source", (), {"name": "problem.md"})()
        stream = io.StringIO()
        self.assertTrue(cli._report_headless([(source, self.app)], stream))
        self.assertEqual(stream.getvalue().splitlines(), [
            "[problem.md] Stopped.",
            f"[problem.md] Continue the same conversation with: python3 web_ui.py --resume-research {self.run}"])
        self.app.state.update(activeNode="critic", stage="critic")
        stream = io.StringIO()
        cli._report_headless([(source, self.app)], stream)
        self.assertEqual(stream.getvalue().splitlines(), [
            "[problem.md] Stopped at the critic.",
            "[problem.md] Continue it in the web UI (python3 web_ui.py): Continue runs that step again in the same "
            "job."])
        self.assertIn("Author stopped", cli.ConciseHeadlessOutput.IMPORTANT_STATUSES)
        # A statement review stopped in place is back at its input: stopped, not "not started".
        self.app.state.update(phase="input", stage="review", activeNode="statement_reviewer", manuallyStopped=True,
                              stoppedStage="review", error=server.REVIEW_STOPPED_TEXT)
        stream = io.StringIO()
        self.assertTrue(cli._report_headless([(source, self.app)], stream))
        self.assertEqual(stream.getvalue().splitlines(), [
            "[problem.md] Stopped at the statement review.",
            "[problem.md] Continue it in the web UI (python3 web_ui.py): Continue runs the statement review again "
            "in the same job."])


if __name__ == "__main__":
    unittest.main()
