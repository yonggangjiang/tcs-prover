"""The author's questions to the user (Codex's request_user_input_async) and their reply box.

The server keeps the running author's newest question as the job's open question and closes it by
its rules: an accepted answer, Dismiss, a newer question, the author stage no longer running, and
the author no longer waiting (its turn ended and AUTHOR_QUESTION_WAIT_SECONDS passed since it
asked, on an injected clock). A restart keeps an open question for the author it pauses and never
brings back a closed one. Send queues one live instruction with the exact answer text; the routes
refuse plainly. The question never counts as the turn's final answer or as part of the live answer.
The job page's box (an option cleared by a second click, the hint for several questions, the notice
when it closes while an answer is written), its Send and Dismiss, the activity card and the jobs list
tag are checked on slices of ui/app.js, as the other UI tests do.

Offline: Popen, both goal_session functions and both author_conversation functions are stubbed
(the runner tests drive goal_session and the Codex conversation against the scripted RPC double);
no model or CLI can start.
"""

import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, providers
from transcript import view_transcript
from ui import live_gateway, server

import test_author_conversation as conversation_test
import test_conversation_ui as page_test
import test_goal_runtime as goal_test

ROOT = Path(__file__).resolve().parents[1]
WAIT = server.AUTHOR_QUESTION_WAIT_SECONDS
ASKED = 1_793_244_172.0  # The injected clock: when the author asks.

# The two questions of a real run (transcript lines 20736 and 2757 of the owner's 28 October run),
# as Codex's app-server reports them.
OPTIONS_TITLE = "May I use independent subagents to search for a complete algorithm and audit the numerical constants?"
OPTIONS = ["Use independent subagents", "Continue without subagents"]
FREE_TITLE = ("Is there an intended algorithm or reference for the 10^11-operation ceiling? I have proved the "
              "basic reductions, but the explicit runtime bound is still missing; a hint about the intended method "
              "would help.")


def question_item(ident="call_fd40601c9a4a4172860ff3471c595419", questions=None):
    questions = questions if questions is not None else [{"title": OPTIONS_TITLE, "options": list(OPTIONS)}]
    lines = [entry["title"] + "".join(f"\n- {option}" for option in entry.get("options") or [])
             for entry in questions]
    return {"type": "agentMessage", "id": ident, "text": "\n".join(lines), "phase": "final_answer",
            "memoryCitation": None, "delivery": "async", "questions": questions}


def question_record(method="item/completed", turn="turn-1", root=True, stage="solve", **item):
    return {"kind": "codex_event", "stage": stage, "node": "author", "root": root,
            "event": {"method": method, "params": {"item": question_item(**item), "threadId": "thread-1",
                                                   "turnId": turn}}}


def turn_record(method, turn="turn-1"):
    return {"kind": "codex_event", "stage": "solve", "node": "author", "root": True,
            "event": {"method": method, "params": {"threadId": "thread-1", "turn": {"id": turn}}}}


def message_record(text, ident="msg_1", phase="final_answer"):
    return {"kind": "codex_event", "stage": "solve", "node": "author", "root": True,
            "event": {"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {
                "type": "agentMessage", "id": ident, "text": text, "phase": phase}}}}


class Alive:
    """The running author's runner: alive until ``ended``."""

    pid = None

    def __init__(self):
        self.ended = False

    def poll(self):
        return 0 if self.ended else None


class NoModels(unittest.TestCase):
    """Every way to a model or CLI fails the test instead of starting one."""

    # A test of the runner's own goal session or conversation drives those against the scripted double.
    STUBBED = ((runtime, "goal_session"), (claude_cli, "goal_session"), (runtime, "author_conversation"),
               (claude_cli, "author_conversation"), (subprocess, "Popen"))

    def setUp(self):
        for target, name in self.STUBBED:
            patcher = mock.patch.object(target, name, side_effect=AssertionError(f"{name} must not run here"))
            patcher.start()
            self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            providers.STORE_ENV: str(self.home / "state" / "api-models.json")})
        environment.start()
        self.addCleanup(environment.stop)
        self.runs = self.home / "runs"
        self.runs.mkdir()

    def author_job(self):
        """A job whose proof author is running in this server."""

        app = server.App(runs=self.runs, load_latest=False)
        app._new_run("Prove S.")
        app._save("checked-statement.md", "# Checked statement\n\nProve S.\n")
        app.state.update(phase="running", stage="solve", activeNode="author", runId=app.run_dir.name,
                         goalThreadId="thread-1", startedAt=server.datetime.now(server.timezone.utc).isoformat())
        app.process = Alive()
        app._save_job_settings(app.state)
        return app

    @staticmethod
    def follow(app, *records, now=ASKED):
        for record in records:
            app.add_trace(record)
            app._follow_author_question(record, now=now)

    @staticmethod
    def saved(app):
        path = server.controller_directory(app.run_dir) / server.JOB_SETTINGS_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def steers(app):
        path = app.run_dir / server.AUTHOR_STEER_FILENAME
        return [item["instruction"] for item in json.loads(path.read_text())["queue"]] if path.exists() else []

    @staticmethod
    def shown(app, now):
        with app.lock:
            question = app._author_question_locked(now)
            return question if question and app._author_running_locked() else None

    @staticmethod
    def labels(app):
        return [record.get("label") for record in app.state["trace"] if record.get("stage") == "author-question"]


class DetectionTests(NoModels):
    def test_a_root_question_is_kept_with_its_id_time_turn_and_options(self):
        app = self.author_job()
        self.follow(app, question_record("item/started"))
        self.follow(app, question_record("item/completed"), now=ASKED + 1)  # The same call again: not newer.
        question = app.state["authorQuestion"]
        self.assertEqual(question, {"id": "call_fd40601c9a4a4172860ff3471c595419", "turnId": "turn-1",
                                    "askedAt": ASKED, "turnEndedAt": None,
                                    "questions": [{"title": OPTIONS_TITLE, "options": OPTIONS}]})
        self.assertEqual(self.saved(app)["authorQuestion"], question)
        # The page and the jobs list read it from the snapshot.
        with mock.patch.object(server.time, "time", return_value=ASKED + 5):
            public = app.snapshot()["authorQuestion"]
        self.assertEqual(public, {"id": question["id"], "askedAt": ASKED, "turnId": "turn-1",
                                  "questions": [{"title": OPTIONS_TITLE, "options": OPTIONS}],
                                  "turnEnded": False, "closesAt": ASKED + WAIT})

    def test_a_question_without_options_is_free_text(self):
        app = self.author_job()
        self.follow(app, question_record(ident="call_9b3c", questions=[{"title": FREE_TITLE, "options": None}]))
        self.assertEqual(app.state["authorQuestion"]["questions"], [{"title": FREE_TITLE, "options": []}])

    def test_subagents_parallel_calls_conversations_and_ordinary_messages_ask_nothing(self):
        app = self.author_job()
        records = [question_record(root=False), {**question_record(), "activityLabel": "Critic 1 of 3"},
                   question_record(stage="conversation"), question_record(stage="critic"),
                   {**question_record(), "node": "critic"}, message_record("PROOF_FILE: proof.md"),
                   message_record("Working on Lemma 2.", phase="commentary")]
        for record in records:
            with self.subTest(record=record):
                self.assertIsNone(server.author_question(record))
        self.follow(app, *records)
        self.assertIsNone(app.state["authorQuestion"])

    def test_damaged_items_ask_nothing(self):
        for item in ({"delivery": None}, {"id": ""}, {"questions": [{"title": "  "}], "text": ""},
                     {"questions": "Which?", "text": ""}):
            record = question_record()
            record["event"]["params"]["item"].update(item)
            with self.subTest(item=item):
                self.assertIsNone(server.author_question(record))
        # Without usable questions, the message text is the one question.
        record = question_record()
        record["event"]["params"]["item"].update(questions=None, text="Which norm?")
        self.assertEqual(server.author_question(record)["questions"], [{"title": "Which norm?", "options": []}])


class ClosingTests(NoModels):
    def asked(self, app, **item):
        self.follow(app, question_record(**item))
        return app.state["authorQuestion"]["id"]

    def test_an_accepted_answer_closes_it(self):
        app = self.author_job()
        identity = self.asked(app)
        app.answer_author_question(identity, [{"index": 0, "answer": OPTIONS[1]}], now=ASKED + 30)
        self.assertIsNone(app.state["authorQuestion"])
        self.assertIsNone(self.saved(app)["authorQuestion"])
        self.assertIsNone(app.snapshot()["authorQuestion"])

    def test_dismiss_closes_it_without_sending_anything(self):
        app = self.author_job()
        identity = self.asked(app)
        app.dismiss_author_question(identity, now=ASKED + 30)
        self.assertEqual((app.state["authorQuestion"], self.steers(app)), (None, []))
        record = app.state["trace"][-1]
        self.assertEqual((record["label"], record["text"], record["questionId"], record["stage"]),
                         ("Author question dismissed",
                          "Nothing was sent to the author; it keeps working without an answer.",
                          identity, "author-question"))
        # The record says nothing about where the workflow is.
        self.assertEqual((app.state["stage"], app.state["activeNode"]), ("solve", "author"))
        self.assertTrue(server.controller_note(record))

    def test_a_newer_question_replaces_it_and_the_older_never_comes_back(self):
        app = self.author_job()
        older = self.asked(app)
        self.follow(app, question_record(ident="call_newer", turn="turn-2"), now=ASKED + 60)
        self.assertEqual((app.state["authorQuestion"]["id"], app.state["authorQuestion"]["askedAt"]),
                         ("call_newer", ASKED + 60))
        self.assertEqual(self.saved(app)["authorQuestion"]["id"], "call_newer")
        # The activity log says why the older one closed.
        notes = [record for record in app.state["trace"] if record.get("stage") == "author-question"]
        self.assertEqual([(record["label"], record["text"], record["questionId"]) for record in notes],
                         [("Author question closed", "The author's question closed: the author asked a newer "
                           "question.", older)])
        self.assertTrue(server.controller_note(notes[0]))
        self.follow(app, question_record(ident=older), now=ASKED + 61)
        self.assertEqual(app.state["authorQuestion"]["id"], "call_newer")
        # A dismissed question's late "completed" copy does not reopen it either.
        app.dismiss_author_question("call_newer", now=ASKED + 62)
        self.follow(app, question_record(ident="call_newer", turn="turn-2"), now=ASKED + 63)
        self.assertIsNone(app.state["authorQuestion"])
        self.assertEqual(self.labels(app), ["Author question closed", "Author question dismissed"])

    def test_it_closes_when_the_author_stage_stops_running(self):
        cases = {
            "the proof goes to the critic": lambda app: self.follow(
                app, {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"}),
            "a pause by the user": lambda app: app.pause(),
            "a pause at the time limit": lambda app: (
                app._save_pause({"status": "paused", "node": "author", "stage": "solve", "state": {},
                                 "elapsedSeconds": 10, "pauseCause": "time-limit"}),
                app.state.update(phase="paused")),
            "a pause after a failure": lambda app: (
                app._save_pause({"status": "paused", "node": "author", "stage": "solve", "state": {},
                                 "elapsedSeconds": 10, "pauseCause": "error"}),
                app.state.update(phase="paused")),
            "a stop": lambda app: app.state.update(phase="stopping"),
            "the end of the job": lambda app: app.state.update(phase="done", resultKind="proof"),
            "a failure": lambda app: app.state.update(phase="done", resultKind="failure", error="Workflow incomplete."),
            "another step": lambda app: app.state.update(stage="final", activeNode="latex_editor"),
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                app = self.author_job()
                identity = self.asked(app)
                with mock.patch.object(server.threading, "Thread"):  # pause() waits for the runner otherwise.
                    change(app)
                self.assertIsNone(self.shown(app, ASKED + 1))
                self.assertIsNone(app.state["authorQuestion"])
                self.assertIsNone(self.saved(app)["authorQuestion"])
                self.assertEqual(self.labels(app), ["Author question closed"])
                # Never again, also when the author works again.
                app.state.update(phase="running", stage="repair", activeNode="author")
                self.follow(app, question_record(ident=identity), now=ASKED + 2)
                self.assertIsNone(app.state["authorQuestion"])

    def test_the_runner_ending_closes_it(self):
        app = self.author_job()
        self.asked(app)
        process = page_runner([{"kind": "final_result", "stage": "solve", "node": "author", "output": "Proof",
                                "resultKind": "proof"}])
        token = app.active_token = app.worker_token = object()
        app.process = process
        app._read_output(process, token)
        self.assertEqual(app.state["phase"], "done")
        self.assertIsNone(app.state["authorQuestion"])
        self.assertIsNone(self.saved(app)["authorQuestion"])

    def test_it_closes_once_its_turn_ended_and_ten_minutes_passed_whichever_is_later(self):
        # The turn ends early: the question stays open until ten minutes after it was asked.
        app = self.author_job()
        self.asked(app)
        self.follow(app, turn_record("turn/completed"), now=ASKED + 60)
        self.assertEqual(app.state["authorQuestion"]["turnEndedAt"], ASKED + 60)
        self.assertEqual(self.saved(app)["authorQuestion"]["turnEndedAt"], ASKED + 60)
        self.assertIsNotNone(self.shown(app, ASKED + WAIT - 1))
        self.assertTrue(app._public_question(app.state["authorQuestion"])["turnEnded"])
        app.close_stale_author_question(now=ASKED + WAIT)  # The server's tick: no page needs to ask.
        self.assertIsNone(app.state["authorQuestion"])
        self.assertEqual(self.labels(app), ["Author question closed"])
        self.assertEqual(app.state["trace"][-1]["text"],
                         "The author's question closed: the step in which it asked has ended and 10 min have passed "
                         "since it asked, so the author no longer waits for an answer.")
        # The turn runs long: the question stays open past ten minutes and closes when the turn ends.
        app = self.author_job()
        self.asked(app)
        self.follow(app, turn_record("turn/completed", turn="another-turn"), now=ASKED + WAIT + 60)
        self.assertIsNotNone(self.shown(app, ASKED + 2 * WAIT))
        self.follow(app, turn_record("turn/completed"), now=ASKED + 2 * WAIT + 1)
        self.assertIsNone(app.state["authorQuestion"])

    def test_a_subagents_turn_never_ends_the_asking_turn(self):
        retry = {"kind": "status", "stage": "solve", "node": "author", "label": runtime.RETRY_LABEL,
                 "text": "Model call failed (network) — retrying in 1 minute (attempt 2).", "retryAt": ASKED + 120}
        subagent = [{**turn_record("turn/started", turn="sub-turn"), "root": False},
                    {**turn_record("turn/completed", turn="sub-turn"), "root": False}, {**retry, "root": False}]
        for asked_turn in ("turn-1", ""):  # Without its turn's id, any root turn/completed would end it.
            with self.subTest(turn=asked_turn or "no turn id"):
                app = self.author_job()
                self.asked(app, turn=asked_turn)
                for record in subagent:
                    self.assertFalse(app._ends_question_turn(record, app.state["authorQuestion"]))
                self.follow(app, *subagent, now=ASKED + 30)
                self.assertIsNone(app.state["authorQuestion"]["turnEndedAt"])
                self.assertIsNotNone(self.shown(app, ASKED + 2 * WAIT))
                # The author's own next turn does end it.
                self.follow(app, turn_record("turn/started", turn="turn-2"), now=ASKED + 40)
                self.assertEqual(app.state["authorQuestion"]["turnEndedAt"], ASKED + 40)

    def test_a_new_turn_or_a_retry_wait_ends_the_asking_turn(self):
        retry = {"kind": "status", "stage": "solve", "node": "author", "label": runtime.RETRY_LABEL,
                 "text": "Model call failed (network) — retrying in 1 minute (attempt 2).", "retryAt": ASKED + 120}
        for ending in (turn_record("turn/started", turn="turn-2"), retry):
            with self.subTest(ending=ending.get("label") or "turn/started"):
                app = self.author_job()
                self.asked(app)
                # The asking turn starting again (or a parallel critic's retry) ends nothing.
                self.follow(app, turn_record("turn/started"), {**retry, "activityLabel": "Critic 1 of 3"},
                            now=ASKED + 30)
                self.assertIsNone(app.state["authorQuestion"]["turnEndedAt"])
                self.follow(app, ending, now=ASKED + 40)
                self.assertEqual(app.state["authorQuestion"]["turnEndedAt"], ASKED + 40)
                self.assertIsNone(self.shown(app, ASKED + WAIT))


def page_runner(lines, between=None):
    """A runner whose stdout yields ``lines``; ``between(index)`` runs before line ``index`` is read."""

    def output():
        for index, line in enumerate(lines):
            if between is not None:
                between(index)
            yield json.dumps(line) + "\n"

    process = mock.Mock(pid=None, stdout=output())
    process.poll.return_value = process.wait.return_value = 0
    return process


class RestartTests(NoModels):
    def restored(self, app):
        restored = server.restore_one_run(app.trace_file, app.runs)
        self.addCleanup(restored._claims.release_all)
        return restored

    def test_a_restart_pause_keeps_the_question_and_shows_it_when_the_author_continues(self):
        app = self.author_job()
        self.follow(app, question_record(), now=time.time())
        identity = app.state["authorQuestion"]["id"]
        with mock.patch.object(server.threading, "Thread"):
            app.pause(_for_restart=True)
        self.assertEqual(app.state["phase"], "pausing")
        # Kept, not shown: no answer can be sent while the author is paused.
        self.assertIsNone(app.snapshot()["authorQuestion"])
        self.assertEqual(app.state["authorQuestion"]["id"], identity)
        paused = {"kind": "workflow_paused", "stage": "solve", "node": "author", "state": {"statement": "Prove S."},
                  "reason": "", "threadId": "thread-1"}
        process = page_runner([paused])
        token = app.active_token = app.worker_token = object()
        app.process = process
        app._read_output(process, token)
        self.assertEqual(app.state["phase"], "paused")
        self.assertEqual(self.saved(app)["authorQuestion"]["id"], identity)
        app._claims.release_all()
        restored = self.restored(app)
        self.assertEqual(restored.state["phase"], "paused")
        self.assertEqual(restored.state["authorQuestion"]["id"], identity)
        self.assertIsNotNone(restored.state["authorQuestion"]["turnEndedAt"])  # Its turn ended with the server.
        self.assertIsNone(restored.snapshot()["authorQuestion"])
        # The automatic resume: the author runs again and the question shows, within its ten minutes.
        restored.state.update(phase="running", stage="solve", activeNode="author")
        restored.process = Alive()
        self.assertEqual(restored.snapshot()["authorQuestion"]["id"], identity)
        restored.close_stale_author_question(now=restored.state["authorQuestion"]["askedAt"] + WAIT)
        self.assertIsNone(restored.snapshot()["authorQuestion"])

    def test_a_closed_question_never_reappears_after_a_restart(self):
        for close in ("answer", "dismiss", "pause", "expire"):
            with self.subTest(close=close):
                app = self.author_job()
                self.follow(app, question_record(), now=time.time())
                identity = app.state["authorQuestion"]["id"]
                if close == "answer":
                    app.answer_author_question(identity, [{"index": 0, "answer": OPTIONS[0]}])
                elif close == "dismiss":
                    app.dismiss_author_question(identity)
                elif close == "pause":
                    with mock.patch.object(server.threading, "Thread"):
                        app.pause()
                    app.close_stale_author_question()
                else:
                    self.follow(app, turn_record("turn/completed"), now=time.time())
                    app.close_stale_author_question(now=time.time() + WAIT)
                self.assertIsNone(self.saved(app)["authorQuestion"])
                app._claims.release_all()
                restored = self.restored(app)
                restored.state.update(phase="running", stage="solve", activeNode="author")
                restored.process = Alive()
                self.assertIsNone(restored.state["authorQuestion"])
                self.assertIsNone(restored.snapshot()["authorQuestion"])

    def test_a_server_that_crashed_keeps_the_question_of_its_author_like_a_restart(self):
        app = self.author_job()
        self.follow(app, question_record(), now=time.time())
        identity = app.state["authorQuestion"]["id"]
        # No pause: the next start finds the author interrupted and continues it by itself.
        app._claims.release_all()
        restored = self.restored(app)
        self.assertEqual((restored.state["phase"], restored.state["activeNode"]), ("paused", "author"))
        self.assertEqual(server.read_pause_object(restored.run_dir)["pauseCause"], "interrupted")
        self.assertEqual(restored.state["authorQuestion"]["id"], identity)
        self.assertIsNone(restored.snapshot()["authorQuestion"])

    def test_the_open_question_of_an_author_that_had_stopped_is_not_restored(self):
        app = self.author_job()
        self.follow(app, question_record(), now=time.time())
        # The server crashed after the proof went to the critic, before the question's closing was saved.
        app.add_trace({"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"})
        self.assertIsNotNone(self.saved(app)["authorQuestion"])
        app._claims.release_all()
        restored = self.restored(app)
        self.assertEqual(restored.state["activeNode"], "critic")
        self.assertIsNone(restored.state["authorQuestion"])
        # Nor when the author works again after the critic.
        restored.state.update(phase="running", stage="repair", activeNode="author")
        restored.process = Alive()
        self.assertIsNone(restored.snapshot()["authorQuestion"])

    def test_a_question_saved_open_before_a_user_or_error_pause_is_not_restored(self):
        for cause in ("user", "error", "time-limit"):
            with self.subTest(cause=cause):
                app = self.author_job()
                self.follow(app, question_record(), now=time.time())
                # The server crashed after it saved the pause, before the next evaluation closed the question.
                app._save_pause({"status": "paused", "node": "author", "stage": "solve",
                                 "state": {"statement": "Prove S."}, "elapsedSeconds": 10, "pauseCause": cause})
                app.state.update(phase="paused")
                app._save_job_settings(app.state)
                self.assertIsNotNone(self.saved(app)["authorQuestion"])
                app._claims.release_all()
                restored = self.restored(app)
                self.assertEqual((restored.state["phase"], restored.state["activeNode"]), ("paused", "author"))
                self.assertEqual(server.read_pause_object(restored.run_dir)["pauseCause"], cause)
                self.assertIsNone(restored.state["authorQuestion"])
                # Nor when the author works again after it.
                restored.state.update(phase="running", stage="solve", activeNode="author")
                restored.process = Alive()
                self.assertIsNone(restored.snapshot()["authorQuestion"])

    def test_a_damaged_saved_question_is_ignored(self):
        for value in ({"id": "call_1"}, {"id": "", "askedAt": 1, "questions": [{"title": "Q"}]},
                      {"id": "call_1", "askedAt": "soon", "questions": [{"title": "Q"}]},
                      {"id": "call_1", "askedAt": 1, "turnEndedAt": True, "questions": [{"title": "Q"}]}, "call_1"):
            with self.subTest(value=value):
                self.assertIsNone(server.saved_author_question(value))
        self.assertEqual(server.saved_author_question({"id": "call_1", "askedAt": 1, "turnId": 5,
                                                       "questions": [{"title": " Q ", "options": ["A", 3, ""]}]}),
                         {"id": "call_1", "askedAt": 1.0, "turnId": "", "turnEndedAt": None,
                          "questions": [{"title": "Q", "options": ["A"]}]})


class AnswerTests(NoModels):
    def setUp(self):
        super().setUp()
        self.app = self.author_job()

    def ask(self, questions=None):
        self.follow(self.app, question_record(questions=questions), now=time.time())
        return self.app.state["authorQuestion"]["id"]

    def test_one_question_is_answered_in_one_live_instruction(self):
        identity = self.ask()
        command = self.app.answer_author_question(identity, [{"index": 0, "answer": "  Continue without subagents "}])
        text = f'Answer to your question "{OPTIONS_TITLE}": Continue without subagents'
        self.assertEqual(self.steers(self.app), [text])
        record = self.app.state["trace"][-1]
        # Recorded as the user's live instruction: the activity log shows it as one, and a continued job keeps it.
        self.assertEqual((record["label"], record["text"], record["steerId"], record["questionId"],
                          record["node"], record["stage"]),
                         ("Live instruction queued", text, command, identity, "author", "solve"))
        self.assertEqual(server.saved_author_instructions(self.app.run_dir), [text])

    def test_several_questions_send_only_the_answered_ones_in_order(self):
        identity = self.ask([{"title": "Which norm?", "options": ["L1", "L2"]}, {"title": "Any reference?"},
                             {"title": "May I use subagents?", "options": ["Yes", "No"]}])
        self.app.answer_author_question(identity, [{"index": 2, "answer": "No"}, {"index": 1, "answer": " "},
                                                   {"index": 0, "answer": "L2, as in the statement."}])
        self.assertEqual(self.steers(self.app), [
            'Answers to your questions:\n"Which norm?": L2, as in the statement.\n"May I use subagents?": No'])

    def test_refusals_are_plain_and_change_nothing(self):
        identity = self.ask()
        long_answer = "x" * runtime.AUTHOR_STEER_MAX_CHARS
        # The answer alone fits; with the question's text it does not.
        long_text = f'Answer to your question "{OPTIONS_TITLE}": {long_answer}'
        once = "Send each answer once, with the number of the question it answers."
        cases = [
            ("call_other", [{"index": 0, "answer": "Yes"}], "The author's question is no longer open."),
            (None, [{"index": 0, "answer": "Yes"}], "The author's question is no longer open."),
            (identity, [{"index": 0, "answer": "  "}], "Choose an option or write an answer first."),
            (identity, [], "Choose an option or write an answer first."),
            (identity, [{"index": 0, "answer": "Yes\0"}], "The answer cannot contain NUL characters."),
            (identity, [{"index": 0, "answer": long_answer}],
             f"The answer is too long to send: with the question it answers it has {len(long_text):,} characters, "
             f"and a live instruction holds at most {runtime.AUTHOR_STEER_MAX_CHARS:,}. Shorten it."),
            (identity, [{"index": 1, "answer": "Yes"}], once),
            (identity, [{"index": 0, "answer": "A"}, {"index": 0, "answer": "B"}], once),
            (identity, [{"index": True, "answer": "A"}], once),
            (identity, "Yes", "Send the answers as a list, one per answered question."),
        ]
        for question, answers, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as refused:
                    self.app.answer_author_question(question, answers)
                self.assertEqual(str(refused.exception), message)
                self.assertEqual(self.steers(self.app), [])
                self.assertEqual(self.app.state["authorQuestion"]["id"], identity)
        # The author's runner has ended though the job still says it runs.
        self.app.process.ended = True
        with self.assertRaisesRegex(ValueError, "^The proof author is no longer running.$"):
            self.app.answer_author_question(identity, [{"index": 0, "answer": "Yes"}])
        # Paused for a restart: kept for later, but nothing can be sent now.
        self.app.process.ended = False
        self.app._save_pause({"status": "paused", "node": "author", "stage": "solve", "state": {},
                              "elapsedSeconds": 1, "pauseCause": "restart"})
        self.app.state.update(phase="paused")
        with self.assertRaisesRegex(ValueError, "^Answers can only be sent while the proof author is running.$"):
            self.app.answer_author_question(identity, [{"index": 0, "answer": "Yes"}])
        # Once the author no longer runs, the question has closed.
        self.app.state.update(phase="done")
        for action in (lambda: self.app.answer_author_question(identity, [{"index": 0, "answer": "Yes"}]),
                       lambda: self.app.dismiss_author_question(identity)):
            with self.assertRaisesRegex(ValueError, "^The author's question is no longer open.$"):
                action()
        self.assertEqual(self.steers(self.app), [])

    def test_a_job_another_process_owns_shows_and_takes_no_question(self):
        identity = self.ask()
        self.app.state["runningElsewhere"] = "This job is running in another terminal run (PID 7)."
        self.assertIsNone(self.app.snapshot()["authorQuestion"])
        with self.assertRaisesRegex(ValueError, "another terminal run"):
            self.app.answer_author_question(identity, [{"index": 0, "answer": "Yes"}])
        self.assertEqual(self.app.state["authorQuestion"]["id"], identity)


class RouteTests(NoModels):
    """The routes on a real local Server with its session token, and the gateway's list of them."""

    def setUp(self):
        super().setUp()
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)
        self.app = self.author_job()
        self.run_id = self.app.state["runId"]
        with self.http.jobs_lock:
            self.http.jobs[self.run_id] = self.app
        self.follow(self.app, question_record(), now=time.time())
        self.identity = self.app.state["authorQuestion"]["id"]

    def call(self, path, body=None):
        headers = {"Origin": self.http.origin, "X-TCS-Prover-Token": self.http.token}
        raw = None
        if body is not None:
            raw, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=20)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_the_state_and_the_jobs_list_show_the_open_question(self):
        status, state = self.call(f"/state?job={self.run_id}")
        self.assertEqual((status, state["authorQuestion"]["id"], state["authorQuestion"]["questions"]),
                         (200, self.identity, [{"title": OPTIONS_TITLE, "options": OPTIONS}]))
        status, listing = self.call("/jobs")
        self.assertEqual(status, 200)
        self.assertEqual([job["authorQuestionOpen"] for job in listing["jobs"] if job["runId"] == self.run_id], [True])

    def test_answer_queues_the_instruction_and_both_agree_it_closed(self):
        status, state = self.call(f"/answer-author-question?job={self.run_id}",
                                  {"questionId": self.identity, "answers": [{"index": 0, "answer": OPTIONS[0]}]})
        self.assertEqual(status, 200)
        self.assertIsNone(state["authorQuestion"])
        self.assertEqual(self.steers(self.app), [f'Answer to your question "{OPTIONS_TITLE}": {OPTIONS[0]}'])
        self.assertEqual([job["authorQuestionOpen"] for job in self.call("/jobs")[1]["jobs"]], [False])
        # Sent twice (a double click): refused plainly, nothing sent again.
        status, refused = self.call(f"/answer-author-question?job={self.run_id}",
                                    {"questionId": self.identity, "answers": [{"index": 0, "answer": OPTIONS[0]}]})
        self.assertEqual((status, refused), (400, {"error": "The author's question is no longer open."}))
        self.assertEqual(len(self.steers(self.app)), 1)

    def test_dismiss_closes_it_and_refusals_are_plain(self):
        status, refused = self.call(f"/answer-author-question?job={self.run_id}",
                                    {"questionId": self.identity, "answers": [{"index": 0, "answer": ""}]})
        self.assertEqual((status, refused), (400, {"error": "Choose an option or write an answer first."}))
        status, state = self.call(f"/dismiss-author-question?job={self.run_id}", {"questionId": self.identity})
        self.assertEqual((status, state["authorQuestion"]), (200, None))
        self.assertEqual(state["trace"][-1]["label"], "Author question dismissed")
        self.assertEqual(self.steers(self.app), [])
        status, refused = self.call(f"/dismiss-author-question?job={self.run_id}", {"questionId": self.identity})
        self.assertEqual((status, refused), (400, {"error": "The author's question is no longer open."}))

    def test_the_server_tick_closes_an_expired_question_with_no_page_open(self):
        # Asked ten minutes and a bit ago; its turn ended soon after. No page asks for the job's state.
        asked = time.time() - WAIT - 60
        app = self.author_job()
        with self.http.jobs_lock:
            self.http.jobs[app.state["runId"]] = app
        self.follow(app, question_record(ident="call_tick"), now=asked)
        self.follow(app, turn_record("turn/completed"), now=asked + 30)
        self.assertEqual(app.state["authorQuestion"]["id"], "call_tick")
        with mock.patch.object(server.App, "snapshot", side_effect=AssertionError("no page asks")):
            self.http._tick_jobs()
        self.assertIsNone(app.state["authorQuestion"])
        self.assertIsNone(self.saved(app)["authorQuestion"])
        self.assertEqual(self.labels(app), ["Author question closed"])
        # The open question of a job still within its ten minutes stays.
        self.assertEqual(self.app.state["authorQuestion"]["id"], self.identity)

    def test_the_gateway_sends_both_routes_to_the_updated_controller_only(self):
        for route in ("/answer-author-question", "/dismiss-author-question"):
            self.assertIn(route, live_gateway.UPDATED_CONTROL_ROUTES)
            self.assertIn(route, live_gateway.CONTROL_ROUTES)


class NeverAFinalAnswerTests(NoModels):
    """The question is in the phase "final_answer", but it is never the turn's answer or a submission."""

    STUBBED = ((claude_cli, "goal_session"), (claude_cli, "author_conversation"))

    def goal(self, scripts):
        double = goal_test.Runtime(scripts)
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings=goal_test.SETTINGS, options={"goal_cwd": str(self.home)})
        self.addCleanup(session.close)
        return double, session

    def converse(self, double):
        return runtime.author_conversation(
            double, "Summarize your progress.", settings=goal_test.SETTINGS,
            options={"goal_cwd": str(self.home), "goal_thread_id": "thread-1"}, features=["multi_agent"],
            conversation_id=conversation_test.CONVERSATION_ID, label="Progress summary requested")

    def setUp(self):
        super().setUp()
        # No CLI can start: the scripted double answers every request of the runner's own session.
        popen = mock.patch.object(subprocess, "Popen", return_value=mock.Mock())
        popen.start()
        self.addCleanup(popen.stop)
        environment = mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)

    @staticmethod
    def event(item):
        return goal_test.event("item/completed", item=item, turnId="turn-1")

    def test_asks_user_recognizes_only_question_deliveries(self):
        self.assertTrue(runtime.asks_user(question_item()))
        self.assertTrue(runtime.asks_user({"type": "agentMessage", "delivery": "async", "text": "Which?"}))
        for item in ({"type": "agentMessage", "phase": "final_answer", "text": "PROOF_FILE: proof.md"},
                     {"type": "agentMessage", "phase": "final_answer", "delivery": None, "questions": None},
                     None, "text"):
            self.assertFalse(runtime.asks_user(item))

    def test_a_question_after_the_proof_is_not_the_submission(self):
        _, session = self.goal([[goal_test.answer("PROOF_FILE: proof.md"), self.event(question_item()),
                                 goal_test.goal("complete"), goal_test.completed()]])
        self.assertEqual(next(session), {"outcome": "done", "output": "PROOF_FILE: proof.md"})

    def test_a_turn_with_only_a_question_continues_the_goal(self):
        double, session = self.goal([[self.event(question_item()), goal_test.goal("complete"),
                                      goal_test.completed(items=[question_item()])],
                                     goal_test.success("Full proof")])
        self.assertEqual(next(session)["output"], "Full proof")
        inputs = [params["input"][0]["text"] for method, params in double.rpc.calls if method == "turn/start"]
        self.assertEqual(inputs, [goal_test.PROMPT, goal_test.PROMPTS["continuation"]])
        # The question was logged for the page, as every root event is.
        self.assertTrue(any(event.get("event", {}).get("params", {}).get("item", {}).get("delivery") == "async"
                            and event["root"] for event in double.events if event["kind"] == "codex_event"))

    def test_a_paused_authors_reply_is_its_answer_not_its_question(self):
        double = conversation_test.Codex([[self.event(question_item()), goal_test.answer("Lemma 2 is open."),
                                           self.event(question_item(ident="call_2")), goal_test.completed()]])
        self.assertEqual(self.converse(double)["reply"], "Lemma 2 is open.")
        # Commentary is the reply of a turn without a final answer, also when a question comes after it.
        double = conversation_test.Codex([[goal_test.answer("Lemma 2 is still open.", phase="commentary"),
                                           self.event(question_item()), goal_test.completed()]])
        self.assertEqual(self.converse(double)["reply"], "Lemma 2 is still open.")
        # With nothing else to show, the question is the last text of the turn, as commentary would be.
        double = conversation_test.Codex([[self.event(question_item()), goal_test.completed()]])
        self.assertEqual(self.converse(double)["reply"], question_item()["text"])

    def test_the_live_answer_text_and_the_partial_output_leave_it_out(self):
        app = self.author_job()
        lines = [message_record("Lemma 1 holds. ", ident="msg_1", phase="commentary"),
                 question_record(), message_record("Lemma 2 is open.", ident="msg_2"),
                 {"kind": "workflow_paused", "stage": "solve", "node": "author", "state": {"statement": "Prove S."},
                  "reason": "Connection lost", "threadId": "thread-1"}]
        process = page_runner(lines)
        token = app.active_token = app.worker_token = object()
        app.process = process
        app._read_output(process, token)
        self.assertEqual(app.state["output"], "Lemma 1 holds. Lemma 2 is open.")
        self.assertEqual((app.run_dir / "partial-output.md").read_text(encoding="utf-8"),
                         "Lemma 1 holds. Lemma 2 is open.")
        # The question stays in the activity log, where it is pinned like every root message.
        self.assertTrue(any(record.get("event", {}).get("params", {}).get("item", {}).get("id")
                            == question_item()["id"] for record in app.pinned))

    def test_the_transcript_reader_names_it_a_question(self):
        record = {**question_record(), "time": "2026-10-29T03:22:52+00:00"}
        self.assertEqual(view_transcript.ui_entry(record, 0)["label"], "Question to the user")
        output = io.StringIO()
        viewer = view_transcript.TranscriptViewer(output)
        viewer.consume(record)
        viewer.finish()
        self.assertIn("QUESTION TO THE USER", output.getvalue())
        self.assertNotIn("FINAL_ANSWER", output.getvalue())


# The box, its handlers, the activity card and the jobs list tag, on slices of ui/app.js.
QUESTION = {"id": "call_fd40601c9a4a4172860ff3471c595419", "askedAt": ASKED, "closesAt": ASKED + WAIT,
            "turnEnded": False, "turnId": "turn-1", "questions": [{"title": OPTIONS_TITLE, "options": OPTIONS}]}
HANDLERS = [["// The author's question: Send queues", "// In-page links"]]


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class QuestionPageTests(unittest.TestCase):
    def run_app(self, slices, setup, checks):
        payload = json.dumps({"slices": slices, "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", page_test.RUNNER, str(page_test.APP)], input=payload,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_markers_occur_once_and_the_box_text_is_in_the_page(self):
        source = page_test.APP.read_text(encoding="utf-8")
        for marker in {marker for pair in HANDLERS for marker in pair}:
            self.assertEqual(source.count(marker), 1, marker)
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        for text in ('id="authorQuestion"', 'id="authorQuestionHeading">The proof author asks<',
                     ">The author keeps working while it waits; answering is optional.<",
                     ">Click a selected option again to leave that question unanswered.<",
                     'id="sendAuthorAnswerButton"', ">Send</button>", 'id="dismissAuthorQuestionButton"',
                     ">Dismiss</button>"):
            self.assertEqual(page.count(text), 1, text)
        # At the top of the job view: right after the job's header.
        self.assertLess(page.index("</header>"), page.index('id="authorQuestion"'))
        self.assertLess(page.index('id="authorQuestion"'), page.index('id="reviewPanel"'))
        # The hint is hidden until several questions are shown.
        self.assertIn('<p id="authorQuestionSkipHint" class="help author-question-note" hidden>', page)

    def test_the_box_shows_the_options_first_chosen_and_a_field_that_overrides_them(self):
        self.run_app(page_test.RENDER, page_test.RENDER_SETUP, r"""
const question = %s;
const running = {...author, phase: 'running', authorQuestion: question};
render(running);
assert.equal(ui.authorQuestion.hidden, false);
assert.match(ui.authorQuestionMeta.textContent, /^Asked at [^.]+\. Unanswered, it closes once the author has finished the step in which it asked, at [^.]+ at the earliest\.$/);
const rows = ui.authorQuestionList.children;
assert.equal(rows.length, 1);
const [title, group, label, field] = rows[0].children;
assert.equal(title.textContent, question.questions[0].title);
assert.equal(group.getAttribute('role'), 'group');
assert.deepEqual(group.children.map(button => [button.textContent, button.getAttribute('aria-pressed')]),
  [['Use independent subagents', 'true'], ['Continue without subagents', 'false']]);
assert.equal(label.textContent, 'Or write your own answer');
assert.equal(label.htmlFor, 'authorAnswer0');
assert.equal(field.tagName, 'TEXTAREA');
assert.deepEqual(authorQuestionAnswers(), [{index: 0, answer: 'Use independent subagents'}]);
// One question: nothing can be left unanswered, so no hint.
assert.equal(ui.authorQuestionSkipHint.hidden, true);
group.children[1].onclick();
assert.deepEqual(group.children.map(button => button.getAttribute('aria-pressed')), ['false', 'true']);
assert.deepEqual(authorQuestionAnswers(), [{index: 0, answer: 'Continue without subagents'}]);
// A click on the chosen option clears it: nothing is chosen, and nothing would be sent.
group.children[1].onclick();
assert.deepEqual(group.children.map(button => button.getAttribute('aria-pressed')), ['false', 'false']);
assert.deepEqual(authorQuestionAnswers(), []);
group.children[1].onclick();
assert.deepEqual(group.children.map(button => button.getAttribute('aria-pressed')), ['false', 'true']);
field.value = '  Use two subagents only. ';
assert.deepEqual(authorQuestionAnswers(), [{index: 0, answer: 'Use two subagents only.'}]);
// A poll with the same question keeps the rows, the choice and the text being written.
render({...running});
assert.equal(ui.authorQuestionList.children[0], rows[0]);
assert.equal(field.value, '  Use two subagents only. ');
// Once its turn ended, the closing time is stated.
render({...running, authorQuestion: {...question, turnEnded: true}});
assert.match(ui.authorQuestionMeta.textContent, /The author has finished the step in which it asked; unanswered, this question closes at [^.]+\.$/);
// The server closed it (an answer, Dismiss, a newer question, the author stopped, or time): the box goes.
render({...running, authorQuestion: null});
assert.equal(ui.authorQuestion.hidden, true);
// A saved batch or a job another process runs shows no box.
render({...running, readOnly: true});
assert.equal(ui.authorQuestion.hidden, true);
render({...running, runningElsewhere: 'Running in another process.'});
assert.equal(ui.authorQuestion.hidden, true);
""" % json.dumps(QUESTION))

    def test_free_text_and_several_questions_each_get_their_own_field(self):
        self.run_app(page_test.RENDER, page_test.RENDER_SETUP, r"""
const question = {...%s, id: 'call_2', questions: [{title: 'Any reference?', options: []},
  {title: 'Which norm?', options: ['L1', 'L2']}]};
render({...author, phase: 'running', authorQuestion: question});
const [free, choice] = ui.authorQuestionList.children;
assert.deepEqual(free.children.map(child => child.tagName), ['P', 'LABEL', 'TEXTAREA']);
assert.equal(free.children[1].textContent, 'Your answer');
assert.equal(choice.children[2].textContent, 'Or write your own answer');
// The free-text question without an answer is left out; the other sends its preselected option.
assert.deepEqual(authorQuestionAnswers(), [{index: 1, answer: 'L1'}]);
free.children[2].value = 'Lovász, 1979.';
assert.deepEqual(authorQuestionAnswers(), [{index: 0, answer: 'Lovász, 1979.'}, {index: 1, answer: 'L1'}]);
// Several questions: the hint says how to leave one out, and only the free-text answer is sent then.
assert.equal(ui.authorQuestionSkipHint.hidden, false);
const [first, second] = choice.children[1].children;
first.onclick();
assert.deepEqual([first.getAttribute('aria-pressed'), second.getAttribute('aria-pressed')], ['false', 'false']);
assert.deepEqual(authorQuestionAnswers(), [{index: 0, answer: 'Lovász, 1979.'}]);
// Several questions without options: there is nothing to clear, so no hint.
render({...author, phase: 'running', authorQuestion: {...question, id: 'call_4',
  questions: [{title: 'Any reference?', options: []}, {title: 'Which bound?', options: []}]}});
assert.equal(ui.authorQuestionSkipHint.hidden, true);
""" % json.dumps(QUESTION))

    def test_send_sends_only_the_answered_questions_of_a_mixed_call(self):
        self.run_app([*page_test.RENDER, *HANDLERS], page_test.RENDER_SETUP, r"""
const question = {...%s, id: 'call_mixed', questions: [{title: 'May I use subagents?', options: ['Yes', 'No']},
  {title: 'Any reference?', options: []}, {title: 'Which norm?', options: ['L1', 'L2']}]};
render({...author, phase: 'running', authorQuestion: question});
const [subagents, reference, norm] = ui.authorQuestionList.children;
assert.equal(ui.authorQuestionSkipHint.hidden, false);
// Only the free-text question is answered: both preselected options are cleared first.
subagents.children[1].children[0].onclick();
norm.children[1].children[0].onclick();
reference.children[2].value = 'Lovász, 1979.';
await ui.sendAuthorAnswer.onclick();
assert.deepEqual(acted, [{path: '/answer-author-question',
  body: {questionId: 'call_mixed', answers: [{index: 1, answer: 'Lovász, 1979.'}]}}]);
""" % json.dumps(QUESTION))

    def test_send_is_disabled_while_its_request_is_pending(self):
        self.run_app([*page_test.RENDER, *HANDLERS], page_test.RENDER_SETUP, r"""
const question = %s;
render({...author, phase: 'running', authorQuestion: question});
let during;
afterAct = () => { during = ui.sendAuthorAnswer.disabled; };
const sending = ui.sendAuthorAnswer.onclick();
assert.equal(ui.sendAuthorAnswer.disabled, true);
await sending;
assert.equal(during, true);
assert.equal(ui.sendAuthorAnswer.disabled, false);
// A refused request enables it again too.
actResult = false;
await ui.sendAuthorAnswer.onclick();
assert.equal(ui.sendAuthorAnswer.disabled, false);
""" % json.dumps(QUESTION))

    def test_a_question_that_closes_while_an_answer_is_written_says_so(self):
        self.run_app([*page_test.RENDER, *HANDLERS], page_test.RENDER_SETUP, r"""
const LOST = "The author's question closed before your answer was sent; you can still send it as a live instruction.";
const question = %s;
const running = {...author, phase: 'running', authorQuestion: question};
const field = () => ui.authorQuestionList.children[0].querySelector('textarea');
ui.notice.textContent = ''; ui.notice.hidden = true;
// Closed with nothing written: the box just goes.
render(running);
render({...running, authorQuestion: null});
assert.equal(ui.authorQuestion.hidden, true);
assert.deepEqual([ui.notice.textContent, ui.notice.hidden], ['', true]);
// Closed while an answer is written: the notice says so, once.
render({...running, authorQuestion: {...question, id: 'call_a'}});
field().value = 'Use two subagents only.';
render({...running, authorQuestion: null});
assert.equal(ui.authorQuestion.hidden, true);
assert.deepEqual([ui.notice.textContent, ui.notice.hidden], [LOST, false]);
ui.notice.hidden = true;
render({...running, authorQuestion: null});
assert.equal(ui.notice.hidden, true);
// A newer question replaces it while an answer is written: the same notice, and the new rows are empty.
render({...running, authorQuestion: {...question, id: 'call_b'}});
field().value = 'L2.';
render({...running, authorQuestion: {...question, id: 'call_c'}});
assert.deepEqual([ui.notice.textContent, ui.notice.hidden], [LOST, false]);
assert.equal(field().value, '');
// Another job's page is no closing.
ui.notice.textContent = ''; ui.notice.hidden = true;
field().value = 'Draft.';
render({...running, runId: 'r2', authorQuestion: null});
assert.deepEqual([ui.notice.textContent, ui.notice.hidden], ['', true]);
// This page's own accepted Send or Dismiss closes it without the notice.
for (const [button, id] of [[ui.sendAuthorAnswer, 'call_d'], [ui.dismissAuthorQuestion, 'call_e']]) {
  render({...running, authorQuestion: {...question, id}});
  // Back on this job, the other job's draft says nothing.
  assert.deepEqual([ui.notice.textContent, ui.notice.hidden], ['', true]);
  field().value = 'Answer.';
  afterAct = () => render({...running, authorQuestion: null});
  await button.onclick();
  assert.equal(acted.at(-1).body.questionId, id);
  assert.deepEqual([ui.notice.textContent, ui.notice.hidden], ['', true]);
}
// Refused because it closed meanwhile: the next state says so.
afterAct = () => {};
actResult = false;
render({...running, authorQuestion: {...question, id: 'call_f'}});
field().value = 'Answer.';
await ui.sendAuthorAnswer.onclick();
render({...running, authorQuestion: null});
assert.deepEqual([ui.notice.textContent, ui.notice.hidden], [LOST, false]);
""" % json.dumps(QUESTION))

    def test_a_question_closed_because_the_author_stopped_offers_no_live_instruction(self):
        self.run_app([*page_test.RENDER, *HANDLERS], page_test.RENDER_SETUP, r"""
const ENDED = "The author's question closed when the author stopped working; your answer was not sent.";
const question = %s;
const running = {...author, phase: 'running', authorQuestion: question};
const field = () => ui.authorQuestionList.children[0].querySelector('textarea');
// The author handed its proof to the critic, was paused, or the job ended, while an answer was written:
// no live instruction can reach it any more.
for (const [id, after] of [['call_critic', {phase: 'running', stage: 'critic', activeNode: 'critic'}],
                           ['call_paused', {phase: 'paused', pauseCause: 'user'}],
                           ['call_done', {phase: 'done', activeNode: 'author'}]]) {
  ui.notice.textContent = ''; ui.notice.hidden = true;
  render({...running, authorQuestion: {...question, id}});
  field().value = 'Use two subagents only.';
  render({...running, ...after, authorQuestion: null});
  assert.equal(ui.authorQuestion.hidden, true);
  assert.deepEqual([ui.notice.textContent, ui.notice.hidden], [ENDED, false]);
  assert.equal(ui.authorSteerControl.hidden, true);
}
""" % json.dumps(QUESTION))

    def test_a_send_meeting_another_question_than_its_rows_is_ignored(self):
        self.run_app([*page_test.RENDER, *HANDLERS], page_test.RENDER_SETUP, r"""
const question = %s;
render({...author, phase: 'running', authorQuestion: question});
// The state changed to a newer question, and its rows are not built yet.
state = {...state, authorQuestion: {...question, id: 'call_newer'}};
await ui.sendAuthorAnswer.onclick();
await ui.dismissAuthorQuestion.onclick();
assert.deepEqual(acted, []);
// Once its rows are shown, Send answers the newer question.
render({...author, phase: 'running', authorQuestion: {...question, id: 'call_newer'}});
await ui.sendAuthorAnswer.onclick();
assert.deepEqual(acted.map(entry => entry.body.questionId), ['call_newer']);
""" % json.dumps(QUESTION))

    def test_send_and_dismiss(self):
        self.run_app([*page_test.RENDER, *HANDLERS], page_test.RENDER_SETUP, r"""
const question = %s;
render({...author, phase: 'running', authorQuestion: question});
ui.authorQuestionList.children[0].children[1].children[1].onclick();
await ui.sendAuthorAnswer.onclick();
assert.deepEqual(acted, [{path: '/answer-author-question',
  body: {questionId: question.id, answers: [{index: 0, answer: 'Continue without subagents'}]}}]);
assert.equal(ui.sendAuthorAnswer.disabled, false);
await ui.dismissAuthorQuestion.onclick();
assert.deepEqual(acted.at(-1), {path: '/dismiss-author-question', body: {questionId: question.id}});
// A free-text question left empty is not sent; the notice says what to do.
acted.length = 0;
render({...author, phase: 'running', authorQuestion: {...question, id: 'call_3',
  questions: [{title: 'Any reference?', options: []}]}});
await ui.sendAuthorAnswer.onclick();
assert.deepEqual(acted, []);
assert.equal(ui.notice.textContent, 'Choose an option or write an answer first.');
assert.equal(ui.notice.hidden, false);
""" % json.dumps(QUESTION))

    def test_the_activity_log_names_the_question(self):
        self.run_app([["function describe(entry)", "function cardIcon("]], "let state = {};", r"""
const item = %s;
const card = describe({kind: 'codex_event', time: 't', stage: 'solve', node: 'author',
  event: {method: 'item/completed', params: {item}}});
assert.equal(card.label, 'Author question');
assert.equal(card.text, item.text);
const answer = describe({kind: 'codex_event', time: 't', stage: 'solve', node: 'author',
  event: {method: 'item/completed', params: {item: {type: 'agentMessage', id: 'm1', text: 'Proof.'}}}});
assert.equal(answer.label, 'Author');
// The sent answer reads like every live instruction.
const sent = describe({kind: 'status', time: 't', stage: 'solve', node: 'author', label: 'Live instruction queued',
  text: 'Answer to your question "Q": A', steerId: 's', questionId: item.id});
assert.deepEqual([sent.type, sent.label, sent.text], ['status', 'Live instruction queued', 'Answer to your question "Q": A']);
""" % json.dumps(question_item()))

    def test_the_jobs_list_tags_a_job_with_an_open_question(self):
        import test_audit_fixes_frontend as frontend
        self.run_app(frontend.BATCHES, frontend.BATCHES_SETUP, r"""
currentBatch = '__root__'; batchCatalog = [{id: '__root__', label: 'Runs', current: true}];
renderJobs([{runId: 'r1', title: 'P1', phase: 'running', stageLabel: 'Proof author', authorQuestionOpen: true},
            {runId: 'r2', title: 'P2', phase: 'running', stageLabel: 'Proof author', authorQuestionOpen: false}]);
const tags = card => card.children[0].children.filter(child => child.className === 'badge job-question');
const [asking, quiet] = ui.jobsList.children;
assert.deepEqual(tags(asking).map(tag => tag.textContent), ['Question']);
assert.deepEqual(tags(quiet), []);
""")


if __name__ == "__main__":
    unittest.main()
