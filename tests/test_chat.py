"""Talk to the author: chat messages to the working author, their delivery and answers, the author's
questions in the chat, and the web-cutoff record of what the user sends. Offline: no model, no CLI."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from ui import server
from test_time_limit_conversation import LIVE_STAGES, FakeRunner, PausedAuthorCase

# The chat framing exactly as the owner approved it (round 4, decision A4).
APPROVED_CHAT = ("[MESSAGE FROM THE USER]\n{message}\n\nIf this message asks you a question or asks for information, "
                 "answer it for the user: write your answer, addressed to the user, to the file {answer_file}, then "
                 "continue your proof work. If it only gives an instruction, follow it; no answer file is needed.")


def serve(case, app):
    """A real local Server on a free port (never 8765) that holds ``app``; stopped at the end of the test."""

    http_server = server.Server((server.HOST, 0), runs=case.runs)
    worker = threading.Thread(target=http_server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()

    def stop():
        http_server.shutdown()
        http_server.server_close()
        worker.join(timeout=2)

    case.addCleanup(stop)
    with http_server.jobs_lock:
        http_server.jobs[app.state["runId"]] = app
    return http_server


def call(http_server, path, body=None, *, raw=False, token=True):
    """One request with the session token; (status, JSON body), or (status, headers, bytes) with ``raw``."""

    headers = {"Origin": http_server.origin, **({"X-TCS-Prover-Token": http_server.token} if token else {})}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    connection = http.client.HTTPConnection(server.HOST, http_server.server_port, timeout=20)
    try:
        connection.request("POST" if body is not None else "GET", path, body=data, headers=headers)
        response = connection.getresponse()
        content = response.read()
        if raw:
            return response.status, dict(response.getheaders()), content
        return response.status, json.loads(content)
    finally:
        connection.close()


class ChatCase(PausedAuthorCase):
    """A job whose author works (its runner lives), with every process start recorded instead of run."""

    def setUp(self):
        super().setUp()
        self.token = object()
        self.app.process = FakeRunner(code=None)
        self.app.active_token = self.app.worker_token = self.token

    def queue(self):
        return json.loads((self.run / server.AUTHOR_STEER_FILENAME).read_text(encoding="utf-8"))["queue"]

    def saved_items(self):
        path = server.controller_directory(self.run) / server.CONVERSATION_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))["items"]

    def answer_path(self, item):
        return self.run / item["answerFile"]

    def write_answer(self, item, text, mode="w"):
        path = self.answer_path(item)
        path.parent.mkdir(exist_ok=True)
        with open(path, mode + "b") as stream:
            stream.write(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def collect(self, *times):
        for now in times:
            self.app.collect_chat_answers(now=now)
        return self.app._conversation_item_locked

    def item(self, identity):
        return next(item for item in self.app.snapshot()["conversation"]["items"] if item["id"] == identity)


class SendTests(ChatCase):
    def test_a_message_to_the_working_author_is_a_live_instruction_with_the_chat_framing(self):
        conversation = self.app.snapshot()["conversation"]
        self.assertEqual((conversation["mode"], conversation["note"], conversation["summary"]),
                         ("author", server.CHAT_NOTE_AUTHOR, "pause"))
        item = self.app.send_chat("  Which lemma is the main obstacle?\n")
        identity = item["id"]
        self.assertRegex(identity, r"\Ac_[0-9a-f]{10}\Z")
        self.assertEqual((item["kind"], item["origin"], item["status"], item["text"]),
                         ("instruction", "user", "queued", "Which lemma is the main obstacle?"))
        self.assertEqual(item["answerFile"], f"chat/answer-{identity}.md")
        # The steer queue holds the framed text from the YAML, with the answer file relative to the author's folder.
        framed = APPROVED_CHAT.format(message="Which lemma is the main obstacle?", answer_file=item["answerFile"])
        self.assertEqual(self.queue()[-1]["instruction"], framed)
        self.assertEqual(self.queue()[-1]["id"], item["steerId"])
        self.assertEqual(server.chat_template(), APPROVED_CHAT)
        # The chat lives in the controller folder only, where the author cannot change it.
        self.assertEqual([saved["id"] for saved in self.saved_items()], [identity])
        self.assertFalse((self.run / server.CONVERSATION_FILENAME).exists())
        record = next(record for record in self.app.state["trace"] if record.get("label") == "Live instruction queued")
        self.assertEqual((record["text"], record["steerId"], record["conversationId"], record["chatText"]),
                         (framed, item["steerId"], identity, "Which lemma is the main obstacle?"))
        # The answer folder exists, owner-only, before the author reads the message.
        info = (self.run / server.CHAT_FOLDER).lstat()
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(self.item(identity)["status"], "queued")

    def test_a_planted_chat_entry_is_never_followed(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.run / server.CHAT_FOLDER).symlink_to(outside, target_is_directory=True)
        item = self.app.send_chat("Status?")
        self.assertTrue((self.run / server.CHAT_FOLDER).is_symlink())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.queue()[-1]["id"], item["steerId"])  # The message still goes.
        # An answer written through the link is never read.
        (outside / f"answer-{item['id']}.md").write_text("Planted.", encoding="utf-8")
        self.app.collect_chat_answers(now=1)
        self.app.collect_chat_answers(now=10)
        self.assertEqual(self.item(item["id"])["reply"], "")

    def test_a_full_length_message_fits_the_queue_and_is_delivered(self):
        text = "x" * runtime.AUTHOR_STEER_MAX_CHARS
        item = self.app.send_chat(text)
        framed = self.queue()[-1]["instruction"]
        self.assertEqual(len(framed), runtime.AUTHOR_STEER_MAX_CHARS + 294)
        self.assertEqual(runtime.pending_author_steer(self.run / server.AUTHOR_STEER_FILENAME, None),
                         (item["steerId"], framed))

    def test_messages_are_checked_before_anything_is_written(self):
        for text, problem in (("", "Enter a message for the author."), ("  ", "Enter a message for the author."),
                              (None, "Enter a message for the author."),
                              ("a\0b", "The message cannot contain NUL characters."),
                              ("x" * 12_001, "Keep the message at or below 12,000 characters.")):
            with self.subTest(text=str(text)[:10]):
                with self.assertRaises(ValueError) as refused:
                    self.app.send_chat(text)
                self.assertEqual(str(refused.exception), problem)
        self.assertFalse((self.run / server.AUTHOR_STEER_FILENAME).exists())
        self.assertEqual(self.app._conversation, [])

    def test_send_is_off_with_its_reason_wherever_the_author_does_not_work(self):
        cases = (
            ({"phase": "reviewing", "stage": "review", "activeNode": "statement_reviewer"}, None, server.CHAT_NOTE_REVIEWING),
            ({"phase": "running", "stage": "critic", "activeNode": "critic"}, None, server.CHAT_NOTE_CRITIC),
            ({"phase": "running", "stage": "critic", "activeNode": "convention"}, None, server.CHAT_NOTE_CONVENTION),
            ({"phase": "running", "stage": "final", "activeNode": "latex_editor"}, None, server.CHAT_NOTE_LATEX),
            ({"phase": "running", "stage": "final", "activeNode": "latex_compile"}, None, server.CHAT_NOTE_LATEX),
            ({"phase": "pausing"}, "user", server.CHAT_NOTE_PAUSING),
            ({"phase": "pausing"}, "stopped", server.CHAT_NOTE_STOPPING_AUTHOR),
            ({"phase": "stopping", "stage": "critic", "activeNode": "critic"}, None, server.CHAT_NOTE_STOPPING),
            ({"phase": "paused", "stage": "critic", "activeNode": "critic"}, "error",
             "The job is paused at the critic step. If the critic sends the proof back after you resume, you can "
             "write to the author then."),
            ({"phase": "paused", "stage": "critic", "activeNode": "critic"}, "stopped",
             "The job is stopped at the critic step. If the critic sends the proof back after you continue, you can "
             "write to the author then."),
            ({"phase": "paused", "stage": "final", "activeNode": "latex_editor"}, "stopped",
             "The job is stopped at the LaTeX editor step. The proof author does not work again in this run."),
            ({"phase": "reviewed", "stage": "review", "activeNode": "statement_reviewer"}, None, server.CHAT_NOTE_REVIEWED),
            ({"phase": "prepared"}, None, server.CHAT_NOTE_PREPARED),
            ({"phase": "done"}, None, server.CHAT_NOTE_DONE),
            ({"workflowStages": {**LIVE_STAGES, "author": False}}, None, server.CHAT_NOTE_NO_AUTHOR),
            ({"runningElsewhere": "A terminal run owns this job."}, None, "A terminal run owns this job."),
        )
        saved = dict(self.app.state)
        for change, cause, note in cases:
            with self.subTest(change=change, cause=cause):
                self.app.state = {**saved, **change}
                if cause:
                    self.app._save_pause({"status": self.app.state["phase"], "node": self.app.state["activeNode"],
                                          "stage": self.app.state["stage"], "state": {"statement": "S"},
                                          "elapsedSeconds": 1, "pauseCause": cause})
                conversation = self.app.snapshot()["conversation"]
                self.assertEqual((conversation["mode"], conversation["note"]), ("off", note))
                with self.assertRaises(ValueError) as refused:
                    self.app.send_chat("Hello?")
                self.assertEqual(str(refused.exception), note)
                server.controller_directory(self.run).joinpath(server.PAUSE_FILENAME).unlink(missing_ok=True)
                (self.run / server.PAUSE_FILENAME).unlink(missing_ok=True)
        self.assertFalse((self.run / server.AUTHOR_STEER_FILENAME).exists())
        self.assertEqual(self.app._conversation, [])
        # A runner that has not started (or already ended) is not the working author.
        self.app.state = dict(saved)
        self.app.process = None
        self.assertEqual(self.app.snapshot()["conversation"]["note"], server.CHAT_NOTE_STARTING)

    def test_paused_at_the_author_a_message_is_a_reply_turn_and_one_runs_at_a_time(self):
        self.error_pause()
        self.app.state["autoResumeAt"] = 0
        conversation = self.app.snapshot()["conversation"]
        self.assertEqual((conversation["mode"], conversation["summary"]), ("paused", "now"))
        self.assertEqual(conversation["note"], server.CHAT_NOTE_PAUSED)
        item = self.app.send_chat("Which step fails?")
        self.assertEqual((item["kind"], item["status"]), ("message", "queued"))
        conversation = self.app.snapshot()["conversation"]
        self.assertEqual((conversation["mode"], conversation["busy"]), ("busy", True))
        with self.assertRaises(ValueError) as refused:
            self.app.send_chat("And then?")
        self.assertEqual(str(refused.exception), server.CONVERSATION_BUSY)
        self.assertFalse((self.run / server.AUTHOR_STEER_FILENAME).exists())

    def test_the_chat_route_sends_and_its_reply_says_what_send_does(self):
        http_server = serve(self, self.app)
        status, state = call(http_server, f"/chat?job={self.run.name}", {"text": "Route check."})
        self.assertEqual(status, 200, state)
        item = state["conversation"]["items"][-1]
        self.assertEqual((item["kind"], item["text"], state["conversation"]["mode"]),
                         ("instruction", "Route check.", "author"))
        self.app.state.update(stage="critic", activeNode="critic")
        status, refused = call(http_server, f"/chat?job={self.run.name}", {"text": "Now?"})
        self.assertEqual((status, refused["error"]), (400, server.CHAT_NOTE_CRITIC))


class ChatPromptTests(ChatCase):
    def test_a_chat_prompt_that_cannot_be_used_sends_nothing(self):
        real = runtime.builtin_workflow("author_critic")

        def edited(prompt=None, lifecycle=None):
            workflow = json.loads(json.dumps(real))
            if lifecycle is not None:
                workflow["nodes"]["author"]["lifecycle"] = lifecycle
            if prompt is not None:
                workflow["prompts"]["chat"] = prompt
            return workflow

        cases = (
            (edited(lifecycle=[key for key in real["nodes"]["author"]["lifecycle"] if key != "chat"]),
             "its proof author has no chat prompt"),
            (edited(prompt="Message: {answer_file}"), "its chat prompt does not name {message}"),
            (edited(prompt="Message: {message}"), "its chat prompt does not name {answer_file}"),
        )
        for workflow, reason in cases:
            with self.subTest(reason=reason), patch.object(runtime, "builtin_workflow", return_value=workflow):
                with self.assertRaises(ValueError) as refused:
                    self.app.send_chat("Hello?")
                self.assertEqual(str(refused.exception), f"The chat prompt of workflows/author_critic.yaml cannot be "
                                                         f"used ({reason}); the message was not sent.")
        self.assertFalse((self.run / server.AUTHOR_STEER_FILENAME).exists())
        self.assertEqual(self.app._conversation, [])

    def test_a_framed_message_over_the_queue_limit_is_refused_not_cut(self):
        with patch.object(runtime, "AUTHOR_STEER_QUEUE_MAX_CHARS", 300):
            with self.assertRaises(ValueError) as refused:
                self.app.send_chat("x" * 10)
        self.assertEqual(str(refused.exception), "With its chat framing this message has 304 characters; a live "
                                                 "instruction holds at most 300. Shorten it.")
        self.assertFalse((self.run / server.AUTHOR_STEER_FILENAME).exists())


class DeliveryTests(ChatCase):
    def test_the_author_taking_it_in_marks_it_delivered_also_after_a_restart(self):
        first = self.app.send_chat("Try induction.")
        second = self.app.send_chat("Why that bound?")
        self.runner_ends({"kind": "status", "stage": "solve", "node": "author", "label": "Author instruction accepted",
                          "authorSteerDelivered": first["steerId"], "text": "Accepted."},
                         {"kind": "workflow_paused", "stage": "solve", "node": "author",
                          "state": {"statement": "Exact statement", "paused": True}, "reason": "Connection lost"})
        delivered = self.item(first["id"])
        self.assertEqual(delivered["status"], "delivered")
        self.assertTrue(delivered["deliveredAt"])
        self.assertEqual((self.item(second["id"])["status"], self.item(second["id"])["deliveredAt"]), ("queued", ""))
        # A restart rebuilds the delivery from the transcript, even where the saved chat missed it.
        path = server.controller_directory(self.run) / server.CONVERSATION_FILENAME
        saved = json.loads(path.read_text(encoding="utf-8"))
        for item in saved["items"]:
            item.update(status="queued", deliveredAt="")
        path.write_text(json.dumps(saved), encoding="utf-8")
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        items = {item["id"]: item for item in restored._conversation}
        self.assertEqual(items[first["id"]]["status"], "delivered")
        self.assertEqual(items[second["id"]]["status"], "queued")

    def test_queued_chat_messages_never_make_the_author_busy(self):
        self.app.send_chat("Keep going.")
        self.error_pause()
        self.app.state["autoResumeAt"] = 0
        self.assertFalse(self.app.conversation_busy())
        self.assertEqual(self.app.resume_refusal(), "")
        self.assertFalse(self.app.start_conversation_if_due(now=time.time() + 600))
        with self.assertRaisesRegex(ValueError, "No author reply is being written."):
            self.app.stop_author_reply()
        # Resetting a job stops only replies: a chat message keeps how far it got (it has no "stopped").
        self.app.state.update(phase="done")
        self.app.reset()
        self.assertEqual([item["status"] for item in self.saved_items()], ["queued"])
        self.assertEqual([item["status"] for item in server.saved_conversation_items(
            {"schemaVersion": 1, "items": self.saved_items()})], ["queued"])


class AnswerTests(ChatCase):
    def test_an_answer_is_read_once_it_has_settled_and_again_when_it_changes(self):
        item = self.app.send_chat("What is proved so far?")
        self.write_answer(item, "Lemma 1 holds.\r\nLemma 2 is open.\x07\x1b")
        self.app.collect_chat_answers(now=100)
        self.app.collect_chat_answers(now=101.5)
        self.assertEqual(self.item(item["id"])["reply"], "")  # Not settled for 2 s yet.
        self.app.collect_chat_answers(now=102.1)
        answered = self.item(item["id"])
        self.assertEqual((answered["status"], answered["reply"]), ("answered", "Lemma 1 holds.\nLemma 2 is open."))
        self.assertTrue(answered["finishedAt"] and answered["deliveredAt"])
        self.assertEqual((answered["answerShortened"], answered["answerUpdatedAt"]), (False, ""))
        self.assertEqual(answered["answerPath"], str(self.answer_path(item).resolve().parent.parent / item["answerFile"]))
        record = self.app.state["trace"][-1]
        self.assertEqual((record["label"], record["conversationId"], record["text"], record["stage"]),
                         ("Author answered", item["id"], "Lemma 1 holds.\nLemma 2 is open.", "conversation"))
        self.assertNotIn("node", record)
        # An edit is shown too, once it has settled.
        time.sleep(0.01)
        self.write_answer(item, "Lemma 1 holds. Lemma 2 holds too.")
        self.app.collect_chat_answers(now=200)
        self.app.collect_chat_answers(now=202.5)
        updated = self.item(item["id"])
        self.assertEqual(updated["reply"], "Lemma 1 holds. Lemma 2 holds too.")
        self.assertTrue(updated["answerUpdatedAt"])
        self.assertEqual(self.app.state["trace"][-1]["label"], "Author answer updated")
        # A deleted answer file keeps the answer that was shown.
        self.answer_path(item).unlink()
        self.app.collect_chat_answers(now=300)
        self.assertEqual(self.item(item["id"])["reply"], "Lemma 1 holds. Lemma 2 holds too.")

    def test_an_empty_or_unsafe_answer_is_never_read(self):
        empty, linked, hard, fifo = (self.app.send_chat(f"Question {number}?") for number in range(4))
        self.write_answer(empty, "  \n\x00 ")
        outside = Path(tempfile.mkdtemp()) / "secret.md"
        outside.write_text("Outside the workspace.", encoding="utf-8")
        self.addCleanup(outside.unlink)
        self.answer_path(linked).symlink_to(outside)
        os.link(outside, self.answer_path(hard))
        os.mkfifo(self.answer_path(fifo))
        for now in (1, 5, 10):
            self.app.collect_chat_answers(now=now)  # A FIFO never blocks the tick.
        for item in (empty, linked, hard, fifo):
            self.assertEqual((self.item(item["id"])["status"], self.item(item["id"])["reply"]), ("queued", ""))

    def test_a_long_answer_is_shortened_says_so_and_downloads_whole(self):
        item = self.app.send_chat("Write everything down.")
        text = "ab" * (2_500_000)  # 5 MB
        self.write_answer(item, text)
        self.app.collect_chat_answers(now=1)
        self.app.collect_chat_answers(now=4)
        answered = self.item(item["id"])
        self.assertEqual(len(answered["reply"]), server.MAX_CHAT_ANSWER_CHARS)
        self.assertEqual((answered["answerShortened"], answered["answerBytes"]), (True, 5_000_000))
        stream, size = self.app.open_chat_answer(item["id"])
        with stream:
            self.assertEqual((size, len(stream.read())), (5_000_000, 5_000_000))
        for identity, problem in (("c_0000000000", "No answer to that message is saved."),
                                  ("../x", "No answer to that message is saved.")):
            with self.assertRaisesRegex(ValueError, problem):
                self.app.open_chat_answer(identity)
        # A link left where the answer was is refused.
        self.answer_path(item).unlink()
        self.answer_path(item).symlink_to(self.run / "draft.md")
        with self.assertRaisesRegex(ValueError, "The answer file is no longer available."):
            self.app.open_chat_answer(item["id"])

    def test_a_huge_answer_is_read_only_as_far_as_the_chat_shows_it(self):
        # The author writes the file: one of 2 GB must not fill the controller's memory and stop every job.
        item = self.app.send_chat("Write everything down.")
        path = self.write_answer(item, "Start. ")
        with open(path, "r+b") as stream:
            stream.truncate(2 * 1024 ** 3)  # Sparse: it takes no disk space.
        read, real = [0], os.read

        def counting(descriptor, size):
            data = real(descriptor, size)
            read[0] += len(data)
            return data

        self.app.collect_chat_answers(now=1)
        with patch.object(server.os, "read", side_effect=counting):
            self.app.collect_chat_answers(now=4)
        answered = self.item(item["id"])
        self.assertEqual((answered["status"], answered["answerShortened"], answered["answerBytes"]),
                         ("answered", True, 2 * 1024 ** 3))
        self.assertTrue(answered["reply"].startswith("Start."))
        self.assertLessEqual(read[0], server.CHAT_ANSWER_READ_BYTES + 1)

    def test_an_answer_while_the_critic_runs_leaves_the_critic_the_active_step(self):
        item = self.app.send_chat("Summarize your approach.")
        self.app.state.update(stage="critic", activeNode="critic")
        self.write_answer(item, "Induction on n.")
        self.app.collect_chat_answers(now=1)
        self.app.collect_chat_answers(now=3)
        self.assertEqual(self.item(item["id"])["reply"], "Induction on n.")
        self.assertEqual(self.app.state["activeNode"], "critic")

    def test_after_the_job_ends_answers_are_read_once_at_once(self):
        item = self.app.send_chat("Last question?")
        self.write_answer(item, "Here is my last answer.")
        self.app.state["phase"] = "done"
        self.app.collect_chat_answers(now=1)
        self.assertEqual(self.item(item["id"])["reply"], "")  # Finished jobs are not watched...
        self.app._chat_final_pass = True
        self.app.collect_chat_answers(now=1)  # ...except once, right after they ended.
        self.assertEqual(self.item(item["id"])["reply"], "Here is my last answer.")
        self.assertFalse(self.app._chat_final_pass)

    def test_an_answer_that_could_not_be_read_once_is_read_at_the_next_check(self):
        opened, failed = self.app.send_chat("Which lemma are you on?"), self.app.send_chat("And the bound?")
        self.write_answer(opened, "Lemma 3.")
        self.write_answer(failed, "n log n.")
        self.collect(100)  # First sighting: both wait to settle.
        real, opens = server._chat_answer_descriptor, []

        def descriptor(workspace, identity):
            # The read's open of the first answer fails once (too many open files, an I/O error: the helper
            # returns None); its signature probe just before it worked.
            opens.append(identity)
            if identity == opened["id"] and opens.count(identity) == 2:
                return None
            return real(workspace, identity)

        def read(descriptor, size, real_read=os.read):
            if identity_reading[0] == failed["id"]:
                identity_reading[0] = ""
                raise OSError(5, "Input/output error")  # The second answer's read fails once.
            return real_read(descriptor, size)

        identity_reading = [""]
        real_answer = server.read_chat_answer

        def read_answer(workspace, identity, signature=None):
            identity_reading[0] = identity
            return real_answer(workspace, identity, signature)

        with patch.object(server, "_chat_answer_descriptor", side_effect=descriptor), \
                patch.object(server, "read_chat_answer", side_effect=read_answer), \
                patch.object(server.os, "read", side_effect=read):
            self.collect(103)
        self.assertEqual(opens.count(opened["id"]), 2)
        self.assertEqual([self.item(identity)["reply"] for identity in (opened["id"], failed["id"])], ["", ""])
        self.collect(104)  # The next check reads both: one failure never hides an answer.
        self.assertEqual([(self.item(identity)["status"], self.item(identity)["reply"])
                          for identity in (opened["id"], failed["id"])],
                         [("answered", "Lemma 3."), ("answered", "n log n.")])
        # Only a file read whole that holds no text waits for its next change.
        empty = self.app.send_chat("Anything else?")
        self.write_answer(empty, " \n")
        self.collect(200, 203)
        self.assertTrue(self.app._chat_seen[empty["id"]][2])
        with patch.object(server, "read_chat_answer", side_effect=AssertionError("read again unchanged")):
            self.collect(210)
        self.write_answer(empty, "Nothing else.")
        self.collect(300, 303)
        self.assertEqual(self.item(empty["id"])["reply"], "Nothing else.")

    def test_the_pass_after_the_job_ended_reads_every_answer_not_shown_yet(self):
        marked, failing = self.app.send_chat("First?"), self.app.send_chat("Second?")
        self.write_answer(marked, "First answer.")
        self.write_answer(failing, "Second answer.")
        workspace = self.app._author_workspace()
        # The final pass of a job that still works would read an answer before it settled: it waits as usual.
        self.app._chat_final_pass = True
        self.collect(1)
        self.assertEqual(self.item(marked["id"])["reply"], "")
        # Marked as read whole with no text at its present signature: while the job works only a change is read,
        # but the pass after it ended reads it again.
        self.app._chat_seen[marked["id"]] = (server.chat_answer_signature(workspace, marked["id"]), 1, True)
        self.app._chat_seen.pop(failing["id"])
        self.app.state["phase"] = "done"
        self.app._chat_final_pass = True
        real = server.read_chat_answer
        with patch.object(server, "read_chat_answer",
                          side_effect=lambda folder, identity, signature=None: None if identity == failing["id"]
                          else real(folder, identity, signature)):
            self.collect(5)
        self.assertEqual(self.item(marked["id"])["reply"], "First answer.")
        # The read that failed in that pass is tried again at the next check, until it is read.
        self.assertEqual(self.item(failing["id"])["reply"], "")
        self.assertTrue(self.app._chat_final_pass)
        self.collect(6)
        self.assertEqual(self.item(failing["id"])["reply"], "Second answer.")
        self.assertFalse(self.app._chat_final_pass)
        # Then the finished job is no longer watched.
        self.write_answer(failing, "Changed much later.")
        self.collect(20, 30)
        self.assertEqual(self.item(failing["id"])["reply"], "Second answer.")

    def test_a_restart_never_captures_an_unchanged_answer_again(self):
        item = self.app.send_chat("Again?")
        self.write_answer(item, "Once.")
        self.collect(1, 4)
        records = [record for record in self.app.state["trace"] if record.get("label") == "Author answered"]
        self.assertEqual(len(records), 1)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        restored.state.update(phase="running", stage="solve", activeNode="author")
        before = restored.state["traceVersion"]
        restored.collect_chat_answers(now=10)
        restored.collect_chat_answers(now=20)
        self.assertEqual(restored.state["traceVersion"], before)
        self.assertEqual(restored._conversation_item_locked(item["id"])["reply"], "Once.")

    def test_the_download_route_streams_the_whole_answer_as_an_attachment(self):
        item = self.app.send_chat("Details?")
        self.write_answer(item, "All the details.")
        self.collect(1, 4)
        http_server = serve(self, self.app)
        path = f"/chat-answer?job={self.run.name}&id={item['id']}"
        status, headers, body = call(http_server, path, raw=True)
        self.assertEqual((status, body), (200, b"All the details."))
        self.assertEqual(headers["Content-Disposition"], f'attachment; filename="answer-{item["id"]}.md"')
        self.assertEqual((headers["Content-Type"], headers["Cache-Control"]), ("text/markdown; charset=utf-8", "no-store"))
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        status, refused = call(http_server, f"/chat-answer?job={self.run.name}&id=c_0000000000")
        self.assertEqual((status, refused["error"]), (400, "No answer to that message is saved."))
        self.assertEqual(call(http_server, path, token=False)[0], 403)
        # The gateway forwards it to the job's controller with the same two parameters.
        from ui import live_gateway

        self.assertEqual(live_gateway.GET_ROUTES["/chat-answer"], {"job", "id"})
        self.assertIn("/chat", live_gateway.UPDATED_CONTROL_ROUTES)


class WebCutoffTests(ChatCase):
    """A web-cutoff author receives nothing the run's audit log did not record first."""

    def setUp(self):
        super().setUp()
        self.app.state["retrievalPolicyPath"] = str(self.run.parent / "policy.json")
        self.audits = []
        for change in (patch.object(retrieval_runtime, "load_policy", return_value={"policy": True}),
                       patch.object(retrieval_runtime, "controller_audit",
                                    side_effect=lambda policy, kind, **fields: self.audits.append((kind, fields)))):
            change.start()
            self.addCleanup(change.stop)

    def test_every_message_and_instruction_is_recorded_before_it_is_queued(self):
        item = self.app.send_chat("What remains?")
        kind, fields = self.audits[-1]
        self.assertEqual(kind, "author_message_sent")
        self.assertEqual(fields, {"conversationKind": "chat", "conversationId": item["id"], "text": "What remains?",
                                  "textSha256": hashlib.sha256(b"What remains?").hexdigest(), "textChars": 13,
                                  "notice": ""})
        self.app.steer_author("Try the probabilistic method.")
        self.assertEqual((self.audits[-1][1]["conversationKind"], self.audits[-1][1]["text"]),
                         ("instruction", "Try the probabilistic method."))
        self.assertEqual(len(self.queue()), 2)

    def test_an_answer_to_the_authors_question_is_recorded_too(self):
        self.app._follow_author_question(json.loads(json.dumps(QUESTION)), now=1_000)
        self.app.answer_author_question("call_1", [{"index": 0, "answer": "L1"}], now=1_001)
        kind, fields = self.audits[-1]
        question = next(item for item in self.app._conversation if item["kind"] == "question")
        self.assertEqual((kind, fields["conversationKind"], fields["conversationId"]),
                         ("author_message_sent", "answer", question["id"]))
        self.assertEqual(fields["text"], 'Answer to your question "Which norm?": L1')

    def test_a_record_the_log_refuses_sends_nothing(self):
        with patch.object(retrieval_runtime, "controller_audit", side_effect=OSError("disk full")):
            with self.assertRaises(ValueError) as refused:
                self.app.send_chat("What remains?")
        self.assertEqual(str(refused.exception), "The message was not sent: the web-cutoff audit log did not accept "
                                                 "it (disk full). Try again in a moment.")
        self.assertFalse((self.run / server.AUTHOR_STEER_FILENAME).exists())
        self.assertEqual(self.app._conversation, [])


QUESTION = {"kind": "codex_event", "stage": "solve", "node": "author", "event": {
    "method": "item/started", "params": {"turnId": "turn-1", "item": {
        "type": "agentMessage", "id": "call_1", "delivery": "async",
        "questions": [{"title": "Which norm?", "options": ["L1", "L2"]}]}}}}


class QuestionTests(ChatCase):
    def ask(self, record=QUESTION, now=1_000):
        self.app._follow_author_question(json.loads(json.dumps(record)), now=now)
        return next((item for item in self.app._conversation if item["kind"] == "question"), None)

    def test_a_question_is_listed_as_the_authors_with_its_options(self):
        item = self.ask()
        self.assertEqual((item["origin"], item["status"], item["questionId"], item["text"]),
                         ("author", "open", "call_1", ""))
        self.assertEqual(item["questions"], [{"title": "Which norm?", "options": ["L1", "L2"]}])
        changed = json.loads(json.dumps(QUESTION))
        changed["event"]["method"] = "item/completed"
        changed["event"]["params"]["item"]["questions"][0]["options"] = ["L1", "L2", "Sup"]
        self.ask(changed)
        questions = [item for item in self.app._conversation if item["kind"] == "question"]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["questions"][0]["options"], ["L1", "L2", "Sup"])
        saved = [item for item in self.saved_items() if item["kind"] == "question"]
        self.assertEqual(saved[0]["questions"][0]["options"], ["L1", "L2", "Sup"])

    def test_an_answer_goes_as_the_same_live_instruction_as_before_and_the_item_keeps_it(self):
        item = self.ask()
        steer = self.app.answer_author_question("call_1", [{"index": 0, "answer": "L2"}], now=1_001)
        expected = server.author_answer_text(item["questions"], [{"index": 0, "answer": "L2"}])
        self.assertEqual(self.queue()[-1]["instruction"], expected)
        self.assertEqual((item["status"], item["reply"], item["steerId"]), ("answered", expected, steer))
        self.assertIsNone(self.app.state["authorQuestion"])
        record = self.app.state["trace"][-1]
        self.assertEqual((record["label"], record["questionId"], record["conversationId"]),
                         ("Live instruction queued", "call_1", item["id"]))

    def test_dismissed_and_closed_questions_say_how_they_ended(self):
        item = self.ask()
        self.app.dismiss_author_question("call_1", now=1_001)
        self.assertEqual((item["status"], item["error"]),
                         ("dismissed", "Nothing was sent to the author; it keeps working without an answer."))
        newer = json.loads(json.dumps(QUESTION))
        newer["event"]["params"]["item"]["id"] = "call_2"
        self.ask(newer)
        second = next(entry for entry in self.app._conversation if entry.get("questionId") == "call_2")
        self.app.state.update(stage="critic", activeNode="critic")  # The author handed in its proof.
        self.app.snapshot()
        self.assertEqual((second["status"], second["error"]),
                         ("closed", "The author's question closed: the proof author is no longer running."))

    def test_a_restart_closes_the_listed_question_unless_a_restart_pause_keeps_it(self):
        self.ask()
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        item = next(entry for entry in restored._conversation if entry["kind"] == "question")
        self.assertEqual((item["status"], item["error"]),
                         ("closed", "The author's question closed: the proof author is no longer running."))
        # A closing server paused the author: the question waits, open, until the author continues.
        self.ask()
        path = server.controller_directory(self.run) / server.CONVERSATION_FILENAME
        saved = json.loads(path.read_text(encoding="utf-8"))
        for entry in saved["items"]:
            entry.update(status="open", error="")
        path.write_text(json.dumps(saved), encoding="utf-8")
        self.app._save_job_settings({**self.app.state, "authorQuestion": {
            "id": "call_1", "askedAt": time.time(), "turnId": "turn-1", "turnEndedAt": None,
            "questions": [{"title": "Which norm?", "options": ["L1", "L2"]}]}})
        self.app._save_pause({"status": "paused", "node": "author", "stage": "solve", "state": {"statement": "S"},
                              "elapsedSeconds": 5, "pauseCause": "restart", "goalThreadId": "thread-1"})
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["authorQuestion"]["id"], "call_1")
        item = next(entry for entry in restored._conversation if entry["kind"] == "question")
        self.assertEqual(item["status"], "open")


class InstructionHistoryTests(ChatCase):
    def test_a_continued_author_gets_the_users_words_without_the_chat_framing(self):
        item = self.app.send_chat("Use the potential function.")
        framed = self.queue()[-1]["instruction"]
        self.app.steer_author("Then check the base case.")
        self.app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Live author instruction sent",
                            "text": framed, "steerId": item["steerId"]})
        self.app.flush_trace(force=True)
        self.assertEqual(server.saved_author_instructions(self.run),
                         ["Use the potential function.", "Then check the base case."])


if __name__ == "__main__":
    unittest.main()
