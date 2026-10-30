"""The owner's decisions for the author package: author goal sessions (Codex app-server and Claude CLI).

I-094 liveness, I-024 missing tool runtime, I-025 requests of the app-server, I-037 and I-114 live
instructions, I-055 interrupt grace, I-056 and I-099 prompts, I-126 readable failures, I-021 the
CODEX_HOME probe, I-047 and I-071 replaced conversations, I-057 and I-058 the instruction queue and
its history. Offline: scripted app-servers (some on real pipes, with the real RPC class), the
stand-in claude and patched clocks; no model call is made and no real CLI can start.
"""

import functools
import io
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli
from ui import server

import test_claude_cli as claude_test
import test_goal_runtime as goal_test
from test_model_retry import FAILURES, AppServer, FakeClock, RetryCase, patch_clock

ROOT = Path(runtime.__file__).parent
SETTINGS = {"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"}
PROMPTS = {"goal": "Prove S.", "continuation": "Continue.", "compaction": "Re-read {directory}.",
           "repair": "Repair round {round}: {bugs}", "resume": "Resume in {directory}.\n{original_prompt}",
           "reopen": "Continue the same goal in this conversation."}


def wait_until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class PipeServer:
    """A Codex app-server stand-in on real pipes; the runner's own RPC class talks to it.

    ``handler(server, message)`` answers each message the runner writes. Stopping the
    "process" (stop_process: terminate, then wait) closes its output, as a killed
    app-server does, so the runner reads the end of the stream.
    """

    def __init__(self, handler):
        requests_in, requests_out = os.pipe()
        replies_in, replies_out = os.pipe()
        self.stdin = os.fdopen(requests_out, "w", encoding="utf-8")
        self.stdout = os.fdopen(replies_in, "r", encoding="utf-8")
        self.stderr = None
        self._requests = os.fdopen(requests_in, "r", encoding="utf-8")
        self._replies = os.fdopen(replies_out, "w", encoding="utf-8")
        self.handler, self.received, self.answers = handler, [], []
        self.lock, self.killed = threading.Lock(), threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            for line in self._requests:
                message = json.loads(line)
                if "method" not in message:
                    self.answers.append(message)  # The runner's answer to a request of ours.
                    continue
                self.received.append(message)
                self.handler(self, message)
        except (OSError, ValueError):
            pass

    def write(self, message):
        with self.lock:
            if not self.killed.is_set():
                self._replies.write(json.dumps(message) + "\n")
                self._replies.flush()

    def reply(self, message, result=None):
        self.write({"id": message["id"], "result": {} if result is None else result})

    def methods(self):
        return [message["method"] for message in self.received]

    # The process interface stop_process uses.
    def poll(self):
        return 0 if self.killed.is_set() else None

    def terminate(self):
        with self.lock:
            if not self.killed.is_set():
                self.killed.set()
                self._replies.close()

    kill = terminate

    def wait(self, timeout=None):
        return 0

    def close(self):
        self.terminate()
        for stream in (self.stdin, self.stdout, self._requests):
            try:
                stream.close()
            except OSError:
                pass


def answer_setup(server_double, message, thread="thread-1"):
    """The answers every attempt needs before its first turn; True when `message` was one of them."""

    method = message["method"]
    if "id" not in message:
        return True  # "initialized"
    results = {"initialize": {}, "config/read": {"config": {}}, "thread/start": {"thread": {"id": thread}},
               "thread/goal/set": {}, "thread/goal/get": {"goal": {"objective": "Prove S.", "status": "active"}}}
    if method == "command/exec":
        server_double.reply(message, goal_test.sandbox_check(message["params"]))  # A sandbox that holds (macOS).
        return True
    if method == "thread/resume":
        server_double.reply(message, {"thread": {"id": message["params"]["threadId"]}})
        return True
    if method in results:
        server_double.reply(message, results[method])
        return True
    return False


def completion(thread, turn, answer="Full proof"):
    return [{"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {
                "type": "agentMessage", "id": f"{turn}-answer", "text": answer, "phase": "final_answer"}}},
            {"method": "thread/goal/updated", "params": {"threadId": thread, "goal": {
                "threadId": thread, "status": "complete"}}},
            {"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": turn, "status": "completed", "items": []}}}]


class PipeCase(unittest.TestCase):
    """A real goal session and RPC against PipeServer; waits are shortened by patching the limits."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.events, self.servers = [], []
        self.handlers = []
        patches = [
            mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: self.events.append(
                {"kind": kind, "stage": stage, **fields})),
            mock.patch.object(runtime, "codex", return_value="codex-test"),
            mock.patch.object(runtime.subprocess, "Popen", side_effect=self.spawn),
            mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live",
                                         "CODEX_HOME": str(self.root / "codex-home")}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        # A regression that waits forever fails instead: the stand-ins end after this long.
        guard = threading.Timer(20, lambda: [double.terminate() for double in list(self.servers)])
        guard.daemon = True
        guard.start()
        self.addCleanup(guard.cancel)

    def spawn(self, command, **kwargs):
        handler = self.handlers.pop(0)
        double = PipeServer(handler)
        double.command = command
        self.servers.append(double)
        self.addCleanup(double.close)
        return double

    def limits(self, **values):
        for name, value in values.items():
            patcher = mock.patch.object(runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self, options=None, initial_instruction="", prompts=PROMPTS):
        session = runtime.goal_session(runtime, "Prove S exactly.", prompts=prompts, settings=SETTINGS,
                                       options={"goal_cwd": str(self.directory), **(options or {})},
                                       initial_instruction=initial_instruction)
        self.addCleanup(session.close)
        return session

    def labelled(self, label):
        return [event for event in self.events if event.get("label") == label]

    def diagnostics(self):
        return [event["text"] for event in self.events if event["kind"] == "diagnostic"]


class LivenessTests(PipeCase):
    """I-094: a silent author, an unanswered request or an idle open goal never blocks the run."""

    def test_a_silent_turn_whose_check_goes_unanswered_ends_the_attempt_for_the_same_thread_retry(self):
        self.limits(AUTHOR_INACTIVITY_SECONDS=0.3, AUTHOR_PROBE_SECONDS=0.3)

        def handler(server_double, message):
            if message["method"] == "turn/start":
                server_double.reply(message, {"turn": {"id": "turn-1"}})
                server_double.write({"method": "turn/started", "params": {"threadId": "thread-1",
                                                                          "turn": {"id": "turn-1"}}})
            elif message["method"] == "thread/goal/get" and "turn/start" in server_double.methods():
                pass  # The hung app-server never answers the check.
            else:
                answer_setup(server_double, message)

        self.handlers.append(handler)
        started = time.monotonic()
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            next(self.session())
        self.assertLess(time.monotonic() - started, 10)
        paused = raised.exception
        self.assertTrue(paused.reason.startswith("Codex stopped responding: no event for"), paused.reason)
        self.assertIn("no answer to a check within", paused.reason)
        # A model-call failure: the controller resumes the same thread, and the empty turn is reverted first.
        self.assertEqual(paused.retry["thread"], "thread-1")
        self.assertEqual(paused.retry["rewind"], {"turn": "turn-1"})
        self.assertTrue(any(text.startswith("No author event for") for text in self.diagnostics()))
        self.assertTrue(self.servers[0].killed.is_set())

    def test_a_quiet_turn_that_still_answers_its_checks_is_never_stopped(self):
        self.limits(AUTHOR_INACTIVITY_SECONDS=0.2, AUTHOR_PROBE_SECONDS=1)
        checks = []

        def handler(server_double, message):
            if message["method"] == "turn/start":
                server_double.reply(message, {"turn": {"id": "turn-1"}})
                server_double.write({"method": "turn/started", "params": {"threadId": "thread-1",
                                                                          "turn": {"id": "turn-1"}}})
            elif message["method"] == "thread/goal/get" and "turn/start" in server_double.methods():
                checks.append(message)
                server_double.reply(message, {"goal": {"status": "active"}})
                if len(checks) == 3:  # A long silent step, then the answer.
                    for item in completion("thread-1", "turn-1"):
                        server_double.write(item)
            else:
                answer_setup(server_double, message)

        self.handlers.append(handler)
        self.assertEqual(next(self.session()), {"outcome": "done", "output": "Full proof"})
        self.assertEqual(len(checks), 3)
        self.assertFalse(self.servers[0].killed.is_set())

    def test_an_unanswered_request_ends_the_attempt_as_a_failed_model_call(self):
        self.limits(RPC_RESPONSE_SECONDS=0.3)

        def handler(server_double, message):
            if message["method"] != "thread/start":  # thread/start is never answered.
                answer_setup(server_double, message)

        self.handlers.append(handler)
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            next(self.session())
        self.assertTrue(raised.exception.reason.startswith("Codex stopped responding: no answer to thread/start within"))
        self.assertIsNotNone(raised.exception.retry)
        self.assertTrue(runtime.retryable_failure(runtime.Error(raised.exception.reason)))

    def test_a_turn_that_ends_with_the_goal_open_and_no_new_turn_is_continued(self):
        self.limits(AUTHOR_CONTINUE_SECONDS=0.3)
        turns = []

        def handler(server_double, message):
            if message["method"] == "turn/start":
                turns.append(message["params"]["input"][0]["text"])
                turn = f"turn-{len(turns)}"
                server_double.reply(message, {"turn": {"id": turn}})
                server_double.write({"method": "turn/started", "params": {"threadId": "thread-1", "turn": {"id": turn}}})
                if len(turns) == 1:
                    # The goal stays active, and Codex starts no turn of its own.
                    server_double.write({"method": "thread/goal/updated", "params": {"threadId": "thread-1", "goal": {
                        "threadId": "thread-1", "status": "active"}}})
                    server_double.write({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
                        "id": turn, "status": "completed", "items": []}}})
                else:
                    for item in completion("thread-1", turn):
                        server_double.write(item)
            else:
                answer_setup(server_double, message)

        self.handlers.append(handler)
        self.assertEqual(next(self.session())["output"], "Full proof")
        self.assertEqual(turns, ["Prove S exactly.", PROMPTS["continuation"]])
        continued = self.labelled("Author continued")
        self.assertEqual(len(continued), 1)
        self.assertIn("started no new turn within", continued[0]["text"])

    def test_codex_continuing_the_goal_by_itself_gets_no_second_continuation(self):
        self.limits(AUTHOR_CONTINUE_SECONDS=0.2)
        turns = []

        def handler(server_double, message):
            if message["method"] == "turn/start":
                turns.append(message)
                server_double.reply(message, {"turn": {"id": "turn-1"}})
                server_double.write({"method": "turn/started", "params": {"threadId": "thread-1",
                                                                          "turn": {"id": "turn-1"}}})
                server_double.write({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
                    "id": "turn-1", "status": "completed", "items": []}}})
                server_double.write({"method": "turn/started", "params": {"threadId": "thread-1",
                                                                          "turn": {"id": "turn-2"}}})
                threading.Timer(0.6, lambda: [server_double.write(item)
                                              for item in completion("thread-1", "turn-2")]).start()
            else:
                answer_setup(server_double, message)

        self.handlers.append(handler)
        self.assertEqual(next(self.session())["output"], "Full proof")
        self.assertEqual(len(turns), 1)
        self.assertEqual(self.labelled("Author continued"), [])

    def test_the_limits_are_generous(self):
        self.assertEqual(runtime.AUTHOR_INACTIVITY_SECONDS, 30 * 60)
        self.assertEqual(runtime.AUTHOR_PROBE_SECONDS, 60)
        self.assertEqual(runtime.AUTHOR_CONTINUE_SECONDS, 60)
        self.assertGreaterEqual(runtime.RPC_RESPONSE_SECONDS, 5 * 60)


class ServerRequestTests(PipeCase):
    """I-025: a request of the app-server is answered and the turn continues."""

    def test_requests_are_declined_or_refused_with_one_diagnostic_each_and_the_turn_goes_on(self):
        def handler(server_double, message):
            if message["method"] == "turn/start":
                server_double.reply(message, {"turn": {"id": "turn-1"}})
                server_double.write({"method": "turn/started", "params": {"threadId": "thread-1",
                                                                          "turn": {"id": "turn-1"}}})
                for number, method in enumerate(("item/tool/requestUserInput", "mcpServer/elicitation/request",
                                                 "item/tool/requestUserInput"), start=900):
                    server_double.write({"id": number, "method": method, "params": {"threadId": "thread-1"}})
                threading.Thread(target=lambda: wait_until(lambda: len(server_double.answers) == 3)
                                 and [server_double.write(item) for item in completion("thread-1", "turn-1")],
                                 daemon=True).start()
            else:
                answer_setup(server_double, message)

        self.handlers.append(handler)
        self.assertEqual(next(self.session())["output"], "Full proof")
        answers = {answer["id"]: answer for answer in self.servers[0].answers}
        self.assertEqual(answers[900]["error"]["code"], -32601)
        self.assertEqual(answers[901], {"id": 901, "result": {"action": "decline"}})
        self.assertEqual(answers[902]["error"]["code"], -32601)
        notes = [text for text in self.diagnostics() if text.startswith("Codex asked for")]
        self.assertEqual(len(notes), 2)  # Once per kind of request.
        self.assertEqual(self.labelled("Author stopped for safety"), [])


class ToolHostServer(AppServer):
    """The scripted app-server of test_model_retry whose tool runtime warning ends the attempts listed in tool_reads."""

    def call(self, method, params):
        if method == "thread/start" and self.index in self.test.no_thread:
            self.calls.append((method, params))
            return {}
        return super().call(method, params)

    def read(self):
        if self.index in self.test.tool_reads:
            raise runtime.ToolRuntimeUnavailable("Required model tool runtime is unavailable: Code Mode is "
                                                 "unavailable because code-mode host is disabled.")
        return super().read()


class CodexRetryCase(RetryCase):
    """The real Codex goal session and _goal_step against the scripted app-server, as CodexAuthorTests."""

    def setUp(self):
        super().setUp()
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.servers, self.scripts, self.threads, self.failures = [], [], {}, {}
        self.turn_count, self.codex_home, self.replacement, self.safety_reads = 0, None, None, []
        self.tool_reads, self.no_thread = [], []
        self.popen = mock.Mock(return_value=SimpleNamespace(
            poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0, kill=lambda: None))
        patches = [mock.patch.object(runtime, "RPC", functools.partial(ToolHostServer, self)),
                   mock.patch.object(runtime, "codex", lambda: "codex-test"),
                   mock.patch.object(runtime.subprocess, "Popen", self.popen),
                   mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live",
                                                "CODEX_HOME": str(self.root / "codex-home")})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)

    def launcher(self, prompts=PROMPTS):
        return functools.partial(runtime.goal_session, runtime, "Prove S exactly.", prompts=prompts,
                                 settings=SETTINGS, features=[], stages=None, node_name="author")

    def run_goal(self, options=None, step=next, stage="solve", sessions=None, launch=None):
        launch = launch or self.launcher()
        options = {"goal_cwd": str(self.directory), **(options or {})}
        sessions = sessions or {"author": launch(options=options, initial_instruction="")}
        return runtime._goal_step(sessions, "author", launch, "", options, step, stage)

    def names(self, server_double):
        return [name for name, _ in server_double.calls]

    def sent(self, server_double):
        return [params["input"][0]["text"] for name, params in server_double.calls if name == "turn/start"]

    def commands(self):
        return [call.args[0] for call in self.popen.call_args_list]

    def labelled(self, label):
        return [event for event in self.events if event.get("label") == label]


class ReplacedConversationTests(CodexRetryCase):
    """I-047 and I-071: a conversation that cannot continue goes on in a new one of the same run."""

    def test_a_conversation_too_long_for_the_model_continues_in_a_new_one_with_the_open_feedback(self):
        overflow = "context_length_exceeded: Your input exceeds the context window of this model."
        self.scripts = [{"answer": "First proof"}, {"work": "w", "error": overflow}, {"answer": "Repaired proof"}]
        launch = self.launcher()
        options = {"goal_cwd": str(self.directory)}
        sessions = {"author": launch(options=options, initial_instruction="")}
        self.assertEqual(next(sessions["author"])["output"], "First proof")
        feedback = {"bugs": "Step 2 divides by zero.", "round": 1, "solution": "First proof"}
        result = runtime._goal_step(sessions, "author", launch, "", options,
                                    lambda session: session.send(feedback), "repair")
        self.assertEqual(result["output"], "Repaired proof")
        second = self.servers[1]
        self.assertIn("thread/start", self.names(second))
        self.assertNotIn("thread/resume", self.names(second))
        # The resume prompt and the critic's open feedback: the rejection and its round are kept.
        self.assertEqual(self.sent(second), [f"Resume in {self.directory}.\nProve S exactly.\n\n"
                                             "Repair round 1: Step 2 divides by zero."])
        self.assertEqual(list(self.threads), ["thread-1", "thread-2"])
        replaced = self.labelled("Conversation replaced")
        self.assertEqual(len(replaced), 1)
        self.assertIn("no longer fits the model's context", replaced[0]["text"])
        self.assertIn("the critic rounds and the time budget are kept", replaced[0]["text"])
        self.assertEqual(self.notices()[0]["failure"], "the conversation is too long for the model")
        self.assertEqual(self.waits(), 60)

    def test_a_conversation_too_long_for_the_model_keeps_the_refusal_note_until_the_next_submission(self):
        # As in _execute: the attempts before the next submission carry the note, the later ones do not.
        overflow = "context_length_exceeded: Your input exceeds the context window of this model."
        note = "Your last submission was not handed to the critic: proof.md: file not found."
        self.scripts = [{"work": "w", "error": overflow}, {"answer": "First proof"},
                        {"work": "w", "error": overflow}, {"answer": "Repaired proof"}]
        plain = self.launcher()
        told = functools.partial(plain, handoff_note=note)
        options = {"goal_cwd": str(self.directory)}
        sessions = {"author": told(options=options, initial_instruction="")}
        self.assertEqual(runtime._goal_step(sessions, "author", told, "", options, next, "solve")["output"],
                         "First proof")
        feedback = {"bugs": "Step 2 divides by zero.", "round": 1, "solution": "First proof"}
        result = runtime._goal_step(sessions, "author", plain, "", options,
                                    lambda session: session.send(feedback), "repair")
        self.assertEqual(result["output"], "Repaired proof")
        resume, repair = f"Resume in {self.directory}.\nProve S exactly.", "Repair round 1: Step 2 divides by zero."
        self.assertEqual([self.sent(server_double) for server_double in self.servers],
                         [["Prove S exactly.\n\n" + note], [resume + "\n\n" + note, repair],
                          [resume + "\n\n" + repair]])
        self.assertEqual(len(self.labelled("Conversation replaced")), 2)

    def test_three_identical_refusals_to_reopen_the_thread_continue_in_a_new_conversation(self):
        self.scripts = [{"work": "w", "error": FAILURES[5]}, {"answer": "Full proof"}]
        refusal = runtime.RPCError("thread/resume failed: rollout format 7 is not supported", code=-32600)
        for index in (2, 3, 4):
            self.failures[(index, "thread/resume")] = refusal
        self.assertEqual(self.run_goal()["output"], "Full proof")
        self.assertEqual(len(self.servers), 4)
        self.assertNotIn("thread/start", self.names(self.servers[2]))
        self.assertIn("thread/start", self.names(self.servers[3]))
        replaced = self.labelled("Conversation replaced")
        self.assertEqual(len(replaced), 1)
        self.assertIn("refused to reopen the saved conversation 3 times", replaced[0]["text"])
        self.assertEqual(self.sent(self.servers[3]), [f"Resume in {self.directory}.\nProve S exactly."])
        self.assertEqual(self.waits(), 3 * 60)

    def test_refusals_that_read_as_provider_failures_never_replace_the_conversation(self):
        self.scripts = [{"work": "w", "error": FAILURES[5]}, {"answer": "Full proof"}]
        for index in (2, 3, 4, 5):
            self.failures[(index, "thread/resume")] = runtime.RPCError(
                "thread/resume failed: error sending request: connection refused", code=-32603)
        self.assertEqual(self.run_goal()["output"], "Full proof")
        self.assertEqual(len(self.servers), 6)
        self.assertEqual(list(self.threads), ["thread-1"])
        self.assertEqual(self.labelled("Conversation replaced"), [])
        self.assertEqual(self.sent(self.servers[5]), [runtime.AUTHOR_RETRY_NOTE])

    def test_a_reply_without_a_thread_is_a_plain_failure_that_is_retried(self):
        self.no_thread = [1]
        self.scripts = [{"answer": "Full proof"}]
        self.assertEqual(self.run_goal()["output"], "Full proof")
        notice = self.notices()[0]
        self.assertIn("Codex answered thread/start without a thread ID", notice["text"])
        self.assertEqual(len(self.servers), 2)


class ToolRuntimeTests(CodexRetryCase):
    """I-024: a missing code-mode tool runtime never continues the session, but it is retried."""

    def test_a_live_run_continues_the_same_thread_with_the_classic_shell_tool(self):
        self.tool_reads = [1]
        self.scripts = [{}, {"answer": "Full proof"}]
        self.assertEqual(self.run_goal()["output"], "Full proof")
        first, second = self.commands()
        self.assertNotIn("code_mode", first)
        self.assertEqual(second[-4:], ["--disable", "code_mode", "--enable", "shell_tool"])
        resumed = next(params for name, params in self.servers[1].calls if name == "thread/resume")
        self.assertEqual(resumed["threadId"], "thread-1")
        self.assertFalse(resumed["config"]["features"]["code_mode"])
        self.assertTrue(resumed["config"]["features"]["shell_tool"])
        self.assertEqual(len(self.labelled("Classic shell tool")), 1)
        self.assertEqual(self.labelled("Author stopped for safety"), [])
        self.assertIn("classic shell tool", self.notices()[0]["text"])

    def test_the_warning_while_reopening_the_thread_is_never_read_as_a_lost_conversation(self):
        self.scripts = [{"work": "w", "error": FAILURES[5]}, {"answer": "Full proof"}]
        self.failures[(2, "thread/resume")] = runtime.ToolRuntimeUnavailable(
            "Required model tool runtime is unavailable: Code mode is unavailable.")
        self.assertEqual(self.run_goal()["output"], "Full proof")
        diagnostics = [event["text"] for event in self.events if event["kind"] == "diagnostic"]
        self.assertTrue(any(text.startswith("Required model tool runtime is unavailable") for text in diagnostics))
        self.assertFalse(any("Could not resume the saved Codex conversation" in text for text in diagnostics))
        self.assertIn("code_mode", self.commands()[2])
        self.assertEqual(list(self.threads), ["thread-1"])

    def test_the_sandbox_settings_read_never_swallows_the_warning(self):
        self.failures[(1, "config/read")] = runtime.ToolRuntimeUnavailable(
            "Required model tool runtime is unavailable: Code mode is unavailable.")
        self.scripts = [{"answer": "Full proof"}]
        with mock.patch.object(runtime, "private_permissions",
                               lambda base, model, codex_home=None, live=False: {"filesystem": {":minimal": "read"}}):
            self.assertEqual(self.run_goal()["output"], "Full proof")
        self.assertNotIn("turn/start", self.names(self.servers[0]))
        self.assertEqual(len(self.servers), 2)

    def test_a_web_cutoff_run_needs_attention_and_checks_again_every_minute(self):
        self.tool_reads = [1, 2]
        self.scripts = [{}, {}, {"answer": "Full proof"}]
        patches = [mock.patch.object(runtime, "retrieval_policy", return_value={"policy": "test"}),
                   mock.patch.object(runtime, "retrieval_environment", return_value={"CODEX_HOME": str(self.root)}),
                   mock.patch.object(runtime.retrieval_runtime, "codex_executable", return_value="codex-test"),
                   mock.patch.object(runtime.retrieval_runtime, "native_search_arguments", return_value=[]),
                   mock.patch.object(runtime.retrieval_runtime, "native_search_configuration", return_value={}),
                   mock.patch.object(runtime.retrieval_runtime, "verify_rpc"),
                   mock.patch.object(runtime.retrieval_runtime, "configuration",
                                     side_effect=lambda policy, role: {"features": {}})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(self.run_goal()["output"], "Full proof")
        self.assertFalse(any("code_mode" in command for command in self.commands()))
        attention = self.labelled("Author needs attention")
        self.assertEqual(len(attention), 2)
        self.assertTrue(attention[0]["needsAttention"])
        self.assertIn("checked again every minute", attention[0]["text"])
        self.assertEqual(self.waits(), 2 * 60)


class ReopenTests(unittest.TestCase):
    """I-099: a reopened thread gets a short prompt, and critic feedback reaches it once."""

    PROMPTS = {**goal_test.PROMPTS, "reopen": "Continue the same goal in this conversation."}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        popen.start()
        self.addCleanup(popen.stop)

    def session(self, scripts=None, prompts=None, initial_instruction="", **options):
        double = goal_test.Runtime(scripts if scripts is not None else [goal_test.success()])
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=prompts or self.PROMPTS,
                                       settings=goal_test.SETTINGS, initial_instruction=initial_instruction,
                                       options={"goal_cwd": str(self.directory), **options})
        self.addCleanup(session.close)
        return double, session

    @staticmethod
    def inputs(double):
        return [params["input"][0]["text"] for method, params in double.rpc.calls if method == "turn/start"]

    def test_a_reopened_thread_gets_the_short_reopen_prompt(self):
        double, session = self.session(goal_thread_id="thread-1", goal_resume=True, goal_require_resume=True)
        self.assertEqual(next(session)["outcome"], "done")
        self.assertEqual(self.inputs(double), [self.PROMPTS["reopen"]])

    def test_a_new_conversation_and_a_workflow_without_reopen_keep_the_full_resume_prompt(self):
        resume = goal_test.PROMPTS["resume"].format(directory=self.directory, original_prompt=goal_test.PROMPT)
        double, session = self.session(goal_resume=True)
        next(session)
        self.assertEqual(self.inputs(double), [resume])
        double, session = self.session(prompts=goal_test.PROMPTS, goal_thread_id="thread-1", goal_resume=True)
        next(session)
        self.assertEqual(self.inputs(double), [resume])

    def test_critic_feedback_the_thread_already_received_is_not_sent_again(self):
        repair = "Revision 1; critic round 2: Gap in step 3\nCandidate: Proof"
        received = {"thread": "thread-1", "digest": runtime._repair_digest(repair)}
        double, session = self.session(initial_instruction=repair, goal_thread_id="thread-1", goal_resume=True,
                                       author_repair_delivered=received)
        next(session)
        self.assertEqual(self.inputs(double), [self.PROMPTS["reopen"]])
        # Feedback recorded for another thread, or other feedback, is sent with the reopen prompt.
        for other in ({**received, "thread": "thread-0"}, {**received, "digest": "0" * 64}):
            double, session = self.session(initial_instruction=repair, goal_thread_id="thread-1",
                                           goal_resume=True, author_repair_delivered=other)
            next(session)
            self.assertEqual(self.inputs(double), [self.PROMPTS["reopen"] + "\n\n" + repair])
            marks = [event for event in double.events if event.get("label") == "Critic feedback delivered"]
            self.assertEqual([(mark["repairDelivered"], mark["threadId"]) for mark in marks],
                             [(runtime._repair_digest(repair), "thread-1")])

    def test_a_repair_turn_records_the_feedback_it_delivered(self):
        double, session = self.session([goal_test.success(), goal_test.success("Repaired")])
        next(session)
        self.assertEqual(session.send({"solution": "Safe fixes", "bugs": "Gap", "round": 3})["output"], "Repaired")
        repair = "Revision 1; critic round 3: Gap\nCandidate: Safe fixes"
        marks = [event for event in double.events if event.get("label") == "Critic feedback delivered"]
        self.assertEqual([mark["repairDelivered"] for mark in marks], [runtime._repair_digest(repair)])

    def test_the_built_in_workflow_defines_a_short_reopen_prompt(self):
        workflow = runtime.builtin_workflow("author_critic")
        self.assertIn("reopen", workflow["nodes"]["author"]["lifecycle"])
        with mock.patch.object(runtime, "require_model_credentials"):
            prompts = runtime.prepare(workflow, {})
        self.assertEqual(prompts["reopen"], workflow["prompts"]["reopen"])
        # The reopened conversation already holds the original prompt; only the resume prompt repeats it.
        for name in ("INITIAL_PROMPT.md", "AUDITS/", "{original_prompt}"):
            self.assertNotIn(name, prompts["reopen"])
        self.assertIn("{original_prompt}", prompts["resume"])
        values = {"original_prompt": prompts["author"], "directory": str(self.directory), "solution": "",
                  "bugs": "", "round": 0, "revision_number": 0, "instruction": ""}
        self.assertLess(len(prompts["reopen"]), len(runtime.render_template(prompts["resume"], values)) // 2)

    def test_only_known_lifecycle_prompts_are_accepted(self):
        definition = yaml.safe_load((ROOT / "workflows/author_critic.yaml").read_text())
        path = self.directory / "custom.yaml"
        definition["nodes"]["author"]["lifecycle"] = [*runtime._GOAL_LIFECYCLE, "reopen"]
        path.write_text(yaml.safe_dump(definition))
        runtime.load_workflow(path)
        definition["nodes"]["author"]["lifecycle"] = [*runtime._GOAL_LIFECYCLE, "unknown"]
        definition["prompts"]["unknown"] = "Text."
        path.write_text(yaml.safe_dump(definition))
        with self.assertRaisesRegex(ValueError, "must bind its lifecycle prompts"):
            runtime.load_workflow(path)


class CompactionPromptTests(unittest.TestCase):
    """I-056: the re-anchor after compaction restates the original prompt."""

    def test_compaction_restates_the_original_prompt(self):
        workflow = runtime.builtin_workflow("author_critic")
        with mock.patch.object(runtime, "require_model_credentials"):
            prompts = runtime.prepare(workflow, {})
        self.assertEqual(prompts["compaction"], workflow["prompts"]["compaction"])
        self.assertIn("{original_prompt}", prompts["compaction"])
        rendered = runtime.render_template(prompts["compaction"], {"original_prompt": "Prove that P = NP."})
        self.assertTrue(rendered.startswith("Continue the same proof task using the conversation summary. "
                                            "Preserve the exact assumptions and requested conclusion."))
        self.assertTrue(rendered.endswith("ORIGINAL AUTHOR PROMPT:\nProve that P = NP."))
        self.assertNotIn("INITIAL_PROMPT.md", prompts["compaction"])


class RepairOnceServerTests(unittest.TestCase):
    """I-099: the controller remembers which critic feedback the saved conversation received."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact statement")
        self.app._save("checked-statement.md", "# Checked statement\n\nExact statement\n")
        self.app.state.update(phase="running", stage="repair", activeNode="author", goalResume=True,
                              goalThreadId="thread-7", startedAt="2026-10-24T10:00:00+00:00")

    def read(self, records):
        token = object()
        process = mock.Mock(stdout=io.StringIO("".join(json.dumps(record) + "\n" for record in records)))
        process.wait.return_value = 0
        process.poll.return_value = 0
        self.app.process, self.app.active_token, self.app.worker_token = process, token, token
        self.app._read_output(process, token)

    def test_delivered_feedback_is_saved_restored_and_passed_to_the_resumed_runner(self):
        delivered = {"kind": "status", "stage": "repair", "node": "author", "label": "Critic feedback delivered",
                     "threadId": "thread-7", "repairDelivered": "a" * 64, "text": "Received."}
        subagent = {**delivered, "root": False, "repairDelivered": "b" * 64}
        self.read([delivered, subagent])
        expected = {"thread": "thread-7", "digest": "a" * 64}
        self.assertEqual(self.app.state["authorRepairDelivered"], expected)
        settings = json.loads((self.app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(settings["authorRepairDelivered"], expected)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["authorRepairDelivered"], expected)
        self.app.state["goalResume"] = True
        options = self.app._proof_options_locked()
        self.assertIn("author_repair_delivered=" + json.dumps(expected), options)
        self.assertIn("goal_stderr_log=" + json.dumps(str(self.app.run_dir / server.AUTHOR_STDERR_FILENAME)), options)


class InterruptGraceTests(unittest.TestCase):
    """I-055: a paused or time-limited author may finish its step for 30 s, and says it is stopping."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        popen.start()
        self.addCleanup(popen.stop)

    def test_the_grace_is_thirty_seconds_and_the_server_waits_longer_before_closing_the_runner(self):
        self.assertEqual(runtime.INTERRUPT_GRACE_SECONDS, 30)
        self.assertGreaterEqual(server.PAUSE_ACKNOWLEDGE_SECONDS, runtime.INTERRUPT_GRACE_SECONDS + 5)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        app = server.App(runs=Path(folder.name))
        app._new_run("Exact statement")
        app._save("checked-statement.md", "# Checked statement\n\nExact statement\n")
        app.state.update(phase="running", stage="solve", activeNode="author",
                         startedAt="2026-10-24T10:00:00+00:00")
        process = mock.Mock()
        process.wait.return_value = 0
        app.process, app.active_token, app.worker_token = process, object(), object()
        app.pause()
        self.assertTrue(wait_until(lambda: process.wait.called))
        self.assertEqual(process.wait.call_args.kwargs["timeout"], server.PAUSE_ACKNOWLEDGE_SECONDS)

    def test_at_the_time_limit_a_running_turn_ends_before_the_author_is_stopped(self):
        double = goal_test.Runtime([[]])
        double.INTERRUPT_GRACE_SECONDS = 5

        def deadline(rpc):
            double.remaining = 0
            self.assertTrue(double.interrupted.wait(2))
            rpc.messages.extend([goal_test.answer("A finished step", "commentary"),
                                 goal_test.completed("interrupted")])

        double.on_empty = deadline
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings=goal_test.SETTINGS, options={"goal_cwd": str(self.directory)})
        self.addCleanup(session.close)
        with self.assertRaisesRegex(runtime.WorkflowPaused, "Workflow time limit reached."):
            next(session)
        methods = [event["event"]["method"] for event in double.events if event["kind"] == "codex_event"]
        self.assertEqual(methods[-2:], ["item/completed", "turn/completed"])
        stopping = [event for event in double.events if event.get("label") == "Author stopping"]
        self.assertEqual(len(stopping), 1)
        self.assertIn("time limit", stopping[0]["text"])
        self.assertEqual(len(double.stopped), 1)  # Only the session's own cleanup stopped it.

    def test_a_pause_says_the_author_is_still_stopping(self):
        pause = self.directory / "pause-request.json"
        double = goal_test.Runtime([[]])

        def pause_after_start(rpc):
            pause.write_text("{}")
            self.assertTrue(double.interrupted.wait(2))
            rpc.messages.extend([goal_test.goal("paused"), goal_test.completed("interrupted")])

        double.on_empty = pause_after_start
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings=goal_test.SETTINGS,
                                       options={"goal_cwd": str(self.directory), "goal_pause_file": str(pause)})
        self.addCleanup(session.close)
        with self.assertRaises(runtime.WorkflowPaused):
            next(session)
        self.assertTrue(wait_until(lambda: any(event.get("label") == "Author stopping" for event in double.events)))
        stopping = next(event for event in double.events if event.get("label") == "Author stopping")
        self.assertIn("Pause requested", stopping["text"])


class HandshakeRPC:
    """An app-server double that answers turn/steer at once and lets the session handle the answer
    before request() returns: the order in which an early acknowledgement used to be missed (I-037)."""

    def __init__(self, test, record):
        self.test, self.record = test, record
        self.calls, self.number = [], 0
        self.inbox, self.handled = queue.Queue(), threading.Event()

    def send(self, message):
        self.calls.append((message["method"], message.get("params")))

    def request(self, method, params, before_send=None):
        self.number += 1
        identity = self.number
        self.calls.append((method, params))
        if before_send is not None:
            before_send(identity)
        if method == "turn/steer":
            self.handled.clear()
            self.inbox.put(self.test.steer_answer(identity, params))
            self.test.assertTrue(self.handled.wait(5))
            self.test.after_steer(self)
        return identity

    def call(self, method, params):
        self.number += 1
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            self.inbox.put(goal_test.event("turn/started", turn={"id": "turn-1"}))
            return {"turn": {"id": "turn-1"}}
        return {}

    def read(self):
        self.handled.set()  # The previous message has been handled.
        message = self.inbox.get(timeout=10)
        self.record(message)
        return message

    def steers(self):
        return [params for method, params in self.calls if method == "turn/steer"]


class LiveInstructionTests(unittest.TestCase):
    """I-037, I-114 and I-057: live instructions are acknowledged, backed off after a refusal, and queued."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.steer_file = self.directory / "author-steer.json"
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        popen.start()
        self.addCleanup(popen.stop)
        self.double = goal_test.Runtime([])
        self.double.pending_author_steer = runtime.pending_author_steer
        self.double.RPC = lambda process, record: setattr(self, "rpc", HandshakeRPC(self, record)) or self.rpc
        self.answers = []

    def queue_file(self, *entries):
        self.steer_file.write_text(json.dumps({"queue": [{"id": identity, "instruction": text}
                                                         for identity, text in entries]}))

    def steer_answer(self, identity, params):
        reject = self.answers.pop(0) if self.answers else False
        return {"id": identity, **({"error": {"code": -32600, "message": "cannot steer a compact turn"}}
                                   if reject else {"result": {}})}

    def session(self):
        session = runtime.goal_session(self.double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings=goal_test.SETTINGS,
                                       options={"goal_cwd": str(self.directory),
                                                "author_steer_file": str(self.steer_file)})
        self.addCleanup(session.close)
        return session

    def accepted(self):
        return [event["authorSteerDelivered"] for event in self.double.events
                if event.get("label") == "Author instruction accepted"]

    def test_an_answer_that_arrives_before_the_request_returns_is_never_missed(self):
        self.queue_file(("steer-1", "Try the invariant."))
        self.after_steer = lambda rpc: [rpc.inbox.put(item) for item in goal_test.success()]
        self.assertEqual(next(self.session())["outcome"], "done")
        self.assertEqual(self.accepted(), ["steer-1"])
        self.assertEqual(len(self.rpc.steers()), 1)

    def test_queued_instructions_are_delivered_one_at_a_time_in_order(self):
        self.queue_file(("steer-1", "First."), ("steer-2", "Second."), ("steer-3", "Third."))
        self.after_steer = lambda rpc: (len(rpc.steers()) == 3
                                        and [rpc.inbox.put(item) for item in goal_test.success()])
        self.assertEqual(next(self.session())["outcome"], "done")
        self.assertEqual([params["input"][0]["text"] for params in self.rpc.steers()], ["First.", "Second.", "Third."])
        self.assertEqual(self.accepted(), ["steer-1", "steer-2", "steer-3"])

    def test_a_refused_instruction_waits_for_the_next_turn_and_is_reported_once(self):
        self.queue_file(("steer-1", "Try the invariant."))
        self.answers = [True]  # Codex refuses the first attempt.
        turns = {"next": False}

        def after_steer(rpc):
            if len(rpc.steers()) == 1:
                # The refusal holds the instruction back for the rest of this turn; the next turn gets it.
                def next_turn():
                    rpc.inbox.put(goal_test.completed())
                    rpc.inbox.put(goal_test.event("turn/started", turn={"id": "turn-2"}))
                    turns["next"] = True
                threading.Timer(0.6, next_turn).start()
            else:
                rpc.inbox.put(goal_test.answer("Full proof"))
                rpc.inbox.put(goal_test.goal("complete"))
                rpc.inbox.put(goal_test.event("turn/completed", turn={"id": "turn-2", "status": "completed",
                                                                      "items": []}))

        self.after_steer = after_steer
        self.assertEqual(next(self.session())["outcome"], "done")
        self.assertTrue(turns["next"])
        self.assertEqual([params["expectedTurnId"] for params in self.rpc.steers()], ["turn-1", "turn-2"])
        refused = [event["text"] for event in self.double.events if event["kind"] == "diagnostic"
                   and "did not accept the live instruction" in event["text"]]
        self.assertEqual(len(refused), 1)
        self.assertIn("cannot steer a compact turn", refused[0])
        self.assertIn("offered again when the next turn starts", refused[0])
        self.assertEqual(self.accepted(), ["steer-1"])

    def test_the_queue_reader_delivers_in_order_and_accepts_the_older_single_instruction_file(self):
        self.assertIsNone(runtime.pending_author_steer(str(self.steer_file)))
        self.queue_file(("a", "First."), ("b", "Second."), ("c", "Third."))
        path = str(self.steer_file)
        self.assertEqual(runtime.pending_author_steer(path), ("a", "First."))
        self.assertEqual(runtime.pending_author_steer(path, "a"), ("b", "Second."))
        self.assertEqual(runtime.pending_author_steer(path, "b"), ("c", "Third."))
        self.assertIsNone(runtime.pending_author_steer(path, "c"))
        # An instruction no longer queued was dropped as accepted: the queue starts over.
        self.assertEqual(runtime.pending_author_steer(path, "gone"), ("a", "First."))
        self.steer_file.write_text(json.dumps({"queue": ["junk", {"id": "x"}, {"id": "y", "instruction": "Y."}]}))
        self.assertEqual(runtime.pending_author_steer(path), ("y", "Y."))
        self.steer_file.write_text(json.dumps({"id": "old", "instruction": "Old format."}))
        self.assertEqual(runtime.pending_author_steer(path), ("old", "Old format."))
        self.assertIsNone(runtime.pending_author_steer(path, "old"))


class SteerQueueServerTests(unittest.TestCase):
    """I-057: the controller appends to the queue instead of overwriting an undelivered instruction."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = server.App(runs=Path(folder.name))
        self.app._new_run("Exact statement")
        self.path = self.app.run_dir / server.AUTHOR_STEER_FILENAME

    def test_instructions_are_appended_and_accepted_ones_dropped(self):
        first = self.app._write_author_steer("First.")
        second = self.app._write_author_steer("Second.")
        value = json.loads(self.path.read_text())
        self.assertEqual([item["id"] for item in value["queue"]], [first, second])
        # The newest entry is repeated at the top level for an older runner.
        self.assertEqual((value["id"], value["instruction"]), (second, "Second."))
        self.assertEqual(runtime.pending_author_steer(str(self.path)), (first, "First."))
        self.app.state["authorSteerDelivered"] = first
        third = self.app._write_author_steer("Third.")
        value = json.loads(self.path.read_text())
        self.assertEqual([item["id"] for item in value["queue"]], [second, third])
        self.assertEqual(runtime.pending_author_steer(str(self.path), first), (second, "Second."))

    def test_a_planted_link_in_place_of_the_queue_is_never_read_or_followed(self):
        outside = self.app.run_dir.parent / "outside.json"
        outside.write_text(json.dumps({"queue": [{"id": "planted", "instruction": "Planted."}]}))
        self.path.symlink_to(outside)
        command = self.app._write_author_steer("Mine.")
        value = json.loads(self.path.read_text())
        self.assertFalse(self.path.is_symlink())
        self.assertEqual([item["id"] for item in value["queue"]], [command])
        self.assertIn("planted", outside.read_text())

    def test_the_text_box_counts_characters_instead_of_cutting_them(self):
        html = (ROOT / "ui/index.html").read_text()
        box = html[html.index('<textarea id="authorSteerInstruction"'):]
        box = box[:box.index("</textarea>")]
        self.assertNotIn("maxlength", box)
        self.assertIn(f'data-max-chars="{runtime.AUTHOR_STEER_MAX_CHARS}"', box)
        self.assertIn('id="authorSteerCount"', html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_the_counter_and_the_send_button_respect_the_limit(self):
        from test_audit_fixes_frontend import RUNNER
        setup = r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const listeners = {};
ui.authorSteerInstruction.addEventListener = (type, handler) => { listeners[type] = handler; };
ui.authorSteerInstruction.dataset.maxChars = '20';
const over = [];
ui.authorSteerCount.classList.toggle = (name, on) => over.push([name, on]);
const show = (element, visible) => { element.hidden = !visible; };
const acted = [];
const act = (path, body) => acted.push({path, body});
"""
        checks = r"""
ui.authorSteerInstruction.value = '  Short.  ';
listeners.input();
assert.equal(ui.authorSteerCount.textContent, '6 of 20 characters');
assert.deepEqual(over.at(-1), ['over', false]);
ui.authorSteerInstruction.value = 'x'.repeat(25);
listeners.input();
assert.match(ui.authorSteerCount.textContent, /^25 of 20 characters — too long to send/);
assert.deepEqual(over.at(-1), ['over', true]);
ui.sendAuthorSteer.onclick();
assert.equal(acted.length, 0);
assert.match(ui.notice.textContent, /at or below 20 characters \(it has 25\)/);
assert.equal(ui.authorSteerInstruction.focused, true);
ui.authorSteerInstruction.value = 'Use the lemma.';
ui.sendAuthorSteer.onclick();
assert.deepEqual(acted, [{path: '/steer-author', body: {instruction: 'Use the lemma.'}}]);
ui.authorSteerInstruction.value = '';
listeners.input();
assert.equal(ui.authorSteerCount.textContent, '');
"""
        payload = json.dumps({"slices": [["ui.sendAuthorSteer.onclick =", "ui.stop.onclick ="]],
                              "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(ROOT / "ui/app.js")], input=payload,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class InstructionHistoryTests(unittest.TestCase):
    """I-058: a continued author gets every earlier instruction; the oldest go to a file first."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.source = self.runs / "source"
        self.source.mkdir()
        self.target = server.App(runs=self.runs)
        self.target._new_run("Exact statement")
        self.workspace = self.runs / "workspace"
        self.workspace.mkdir()
        self.target.state["goalWorkspace"] = str(self.workspace)

    def write(self, instructions):
        records = [{"kind": "status", "stage": "solve", "node": "author", "label": "Live instruction queued",
                    "text": text, "steerId": f"steer-{index}"} for index, text in enumerate(instructions)]
        (self.source / "transcript.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))

    def queued(self):
        return json.loads((self.target.run_dir / server.AUTHOR_STEER_FILENAME).read_text())["instruction"]

    def trace(self, label):
        return [record for record in self.target.state["trace"] if record.get("label") == label]

    def test_every_instruction_is_kept_beyond_the_size_of_one_instruction(self):
        instructions = [f"Instruction {index}: " + "x" * 5000 for index in range(4)]
        self.write(instructions)
        self.assertEqual(server.saved_author_instructions(self.source), instructions)

    def test_a_long_history_goes_to_a_file_and_the_oldest_are_left_out_of_the_block_first(self):
        old, middle, new = "Oldest small instruction.", "M" * 11960, "Newest small instruction."
        self.write([old, middle, new])
        server.Server._queue_saved_author_instructions(self.source, self.target)
        history = (self.workspace / server.INSTRUCTION_HISTORY_FILENAME).read_text()
        self.assertLess(history.index(old), history.index(middle))
        self.assertLess(history.index(middle), history.index(new))
        block = self.queued()
        self.assertTrue(block.startswith(server.RESTORED_INSTRUCTIONS_NOTE))
        self.assertTrue(block.endswith(new))
        self.assertNotIn(old, block)  # Never kept while a newer, larger one is dropped.
        self.assertLessEqual(len(block), runtime.AUTHOR_STEER_MAX_CHARS)
        restored = self.trace("Restored live instructions queued")
        self.assertEqual(restored[0]["restoredInstructions"], [old, middle, new])
        shortened = self.trace("Earlier live instructions shortened")
        self.assertEqual(len(shortened), 1)
        self.assertIn(f"2 older instructions are only in {server.INSTRUCTION_HISTORY_FILENAME}", shortened[0]["text"])
        # The next continuation still finds every instruction, never the pointer block.
        (self.target.run_dir / "transcript.jsonl").write_text("".join(
            json.dumps(record) + "\n" for record in [*restored, {
                "kind": "request", "stage": "solve", "node": "author", "label": "Live author instruction sent",
                "text": block, "steerId": restored[0]["steerId"]}]))
        self.assertEqual(server.saved_author_instructions(self.target.run_dir), [old, middle, new])

    def test_a_history_that_fits_is_queued_as_before_without_a_file(self):
        self.write(["First.", "Second."])
        server.Server._queue_saved_author_instructions(self.source, self.target)
        self.assertEqual(self.queued(), "First.\n\nSecond.")
        self.assertFalse((self.workspace / server.INSTRUCTION_HISTORY_FILENAME).exists())
        self.assertEqual(self.trace("Earlier live instructions shortened"), [])

    def queue_without_history_file(self):
        original = server.runtime._private_atomic_write

        def write(path, *args, **kwargs):
            if Path(path).name == server.INSTRUCTION_HISTORY_FILENAME:
                raise OSError("disk full")
            return original(path, *args, **kwargs)

        with mock.patch.object(server.runtime, "_private_atomic_write", side_effect=write):
            server.Server._queue_saved_author_instructions(self.source, self.target)

    def test_a_history_file_that_cannot_be_written_is_reported_and_the_newest_are_still_queued(self):
        self.write(["A" * 7000, "B" * 7000])
        self.queue_without_history_file()
        self.assertEqual(self.queued(), "B" * 7000)
        diagnostics = [record["text"] for record in self.target.state["trace"] if record.get("kind") == "diagnostic"]
        self.assertTrue(any("disk full" in text for text in diagnostics))
        self.assertIn("1 older instruction was left out", self.trace("Earlier live instructions shortened")[0]["text"])

    def test_the_newest_instruction_is_kept_even_at_the_size_limit(self):
        newest = "N" * runtime.AUTHOR_STEER_MAX_CHARS
        self.write(["Older.", newest])
        self.queue_without_history_file()
        self.assertEqual(self.queued(), newest)


class ReadableFailureTests(unittest.TestCase):
    """I-126: plain reasons, the raw details kept as diagnostics, the whole stderr saved, reconnects shown."""

    def rpc(self, *lines, stderr=None, **kwargs):
        process = SimpleNamespace(stdout=io.StringIO("".join(line + "\n" for line in lines)), stdin=io.StringIO(),
                                  stderr=stderr)
        return runtime.RPC(process, **kwargs)

    def test_an_error_reply_reads_as_its_message_and_keeps_its_code_as_a_diagnostic(self):
        rpc = self.rpc(json.dumps({"id": 1, "error": {"code": -32600, "message": "no rollout found for thread id 0199"}}))
        notes = []
        rpc.notice = notes.append
        with self.assertRaises(runtime.RPCError) as raised:
            rpc.call("thread/resume", {})
        self.assertEqual(str(raised.exception), "thread/resume failed: no rollout found for thread id 0199")
        self.assertEqual(raised.exception.code, -32600)
        self.assertEqual(len(notes), 1)
        self.assertIn("JSON-RPC error -32600", notes[0])
        self.assertIsNone(rpc.pending_call)

    def test_error_objects_without_a_message_are_described_in_words(self):
        message = runtime._provider_message
        self.assertEqual(message({"codexErrorInfo": "usageLimitExceeded"}), "usage limit exceeded")
        self.assertEqual(message({"codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 503}}}),
                         "response stream disconnected (HTTP 503)")
        self.assertEqual(message({"codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
                                  "additionalDetails": "stream closed"}), "response stream disconnected (stream closed)")
        self.assertEqual(runtime._plain_failure(message({"codexErrorInfo": "usageLimitExceeded"})), "usage limit reached")
        self.assertEqual(runtime._plain_failure(message({"codexErrorInfo": "contextWindowExceeded"})),
                         "the conversation is too long for the model")
        self.assertEqual(message({"message": "Overloaded", "codexErrorInfo": "serverOverloaded"}), "Overloaded")

    def test_the_whole_app_server_stderr_is_saved_redacted_and_a_link_is_never_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "codex-app-server.log"
            lines = "".join(f"line {index}\n" for index in range(30)) + "key sk-stderr-secret-0123456789\n"
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-stderr-secret-0123456789"}):
                rpc = self.rpc(stderr=io.StringIO(lines), stderr_log=str(log))
                rpc.stderr_reader.join(5)
            saved = log.read_text()
            self.assertTrue(saved.startswith("--- Codex app-server started "))
            self.assertIn("line 0\n", saved)
            self.assertIn("line 29\n", saved)
            self.assertIn("key [redacted]", saved)
            self.assertNotIn("sk-stderr-secret", saved)
            self.assertEqual(len(rpc.stderr_tail), 12)
            self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
            outside = Path(folder) / "outside.txt"
            outside.write_text("untouched\n")
            linked = Path(folder) / "linked.log"
            linked.symlink_to(outside)
            rpc = self.rpc(stderr=io.StringIO("line\n"), stderr_log=str(linked))
            rpc.stderr_reader.join(5)
            self.assertEqual(outside.read_text(), "untouched\n")

    def test_codex_reconnect_notices_are_shown_as_status_lines(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()):
            reconnect = goal_test.event("error", error={"message": "Reconnecting... 2/5",
                                                        "additionalDetails": "stream disconnected before completion"},
                                        willRetry=True)
            double = goal_test.Runtime([[reconnect, *goal_test.success()]])
            session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                           settings=goal_test.SETTINGS, options={"goal_cwd": folder})
            self.assertEqual(next(session)["outcome"], "done")
            session.close()
        notices = [event for event in double.events if event.get("label") == "Codex is reconnecting"]
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]["text"].startswith("Codex is reconnecting (2/5)…"), notices[0]["text"])
        self.assertIn("stream disconnected before completion", notices[0]["text"])

    def test_a_turn_failure_without_a_message_keeps_the_raw_payload_as_a_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()):
            failed = goal_test.event("turn/completed", turn={"id": "turn-1", "status": "failed",
                                                             "error": {"codexErrorInfo": "usageLimitExceeded"}})
            double = goal_test.Runtime([[failed]])
            session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                           settings=goal_test.SETTINGS, options={"goal_cwd": folder})
            with self.assertRaises(runtime.WorkflowPaused) as raised:
                next(session)
            session.close()
        self.assertEqual(raised.exception.reason, "Author turn failed: usage limit exceeded")
        self.assertTrue(any(event["kind"] == "diagnostic" and "usageLimitExceeded" in event["text"]
                            for event in double.events))

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_a_failed_turn_shows_its_reason_in_the_activity_log(self):
        from test_audit_fixes_frontend import RUNNER
        setup = r"""
const state = {reasoningSummary: 'concise'};
const criticReviewModel = () => ({byResult: new Map()});
const collectCriticReviewCycles = () => [];
const criticRoundOutcomeLabel = () => '';
"""
        checks = r"""
const failed = describe({kind: 'codex_event', time: 't', event: {method: 'turn/completed',
  params: {turn: {id: 'turn-1', status: 'failed', error: {message: 'stream disconnected before completion'}}}}});
assert.equal(failed.text, 'failed: stream disconnected before completion');
const claude = describe({kind: 'codex_event', time: 't', event: {method: 'turn/completed',
  params: {turn: {id: 'turn-2', status: 'failed', error: 'API Error: 529 Overloaded'}}}});
assert.equal(claude.text, 'failed: API Error: 529 Overloaded');
const done = describe({kind: 'codex_event', time: 't', event: {method: 'turn/completed',
  params: {turn: {id: 'turn-3', status: 'completed'}}}});
assert.equal(done.text, 'completed');
"""
        payload = json.dumps({"slices": [["function describe(entry)", "function cardIcon("]],
                              "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(ROOT / "ui/app.js")], input=payload,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CodexHomeProbeTests(unittest.TestCase):
    """I-021: only definite CODEX_HOME answers are remembered; other outcomes are retried accurately."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.home = self.root / "private-home"
        self.home.mkdir()
        (self.home / "config.toml").write_text("")  # Not a first use: the short limit applies.
        self.environment = {**os.environ, "CODEX_HOME": str(self.home)}
        runtime._ISOLATED_CODEX.clear()
        self.addCleanup(runtime._ISOLATED_CODEX.clear)

    def stand_in(self, body):
        path = self.root / "bin" / "codex"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}")
        path.chmod(0o700)
        return str(path)

    ANSWER = ("sys.stdin.readline()\n"
              "print(json.dumps({'id': 1, 'result': {'codexHome': os.environ['CODEX_HOME']}}), flush=True)\n"
              "sys.stdin.read()\n")

    def test_a_slow_probe_is_retried_with_an_accurate_message_and_never_remembered(self):
        ready = self.root / "ready"
        codex = self.stand_in(f"if not os.path.exists({str(ready)!r}):\n    time.sleep(5)\n" + self.ANSWER)
        with mock.patch.object(runtime, "_codex_candidates", return_value=[codex]), \
                mock.patch.object(runtime, "CODEX_HOME_PROBE_SECONDS", 0.5):
            with self.assertRaises(runtime.Error) as raised:
                runtime.api_codex_executable(self.environment)
            self.assertNotIsInstance(raised.exception, runtime.SafetyStop)
            self.assertTrue(runtime.retryable_failure(raised.exception))
            self.assertIn("did not answer within", str(raised.exception))
            self.assertIn("The check is repeated at the next retry.", str(raised.exception))
            ready.write_text("")
            self.assertEqual(runtime.api_codex_executable(self.environment), codex)

    def test_notices_before_the_reply_are_skipped(self):
        codex = self.stand_in("print('starting up', flush=True)\nprint(json.dumps([1, 2]), flush=True)\n"
                              "print(json.dumps({'method': 'notice'}), flush=True)\n" + self.ANSWER)
        with mock.patch.object(runtime, "_codex_candidates", return_value=[codex]):
            self.assertEqual(runtime.api_codex_executable(self.environment), codex)

    def test_a_probe_that_ends_without_answering_reports_its_error_output(self):
        codex = self.stand_in("sys.stderr.write('dyld: missing library libcodex.dylib\\n')\nsys.exit(3)\n")
        with mock.patch.object(runtime, "_codex_candidates", return_value=[codex]):
            with self.assertRaises(runtime.Error) as raised:
                runtime.api_codex_executable(self.environment)
        self.assertNotIsInstance(raised.exception, runtime.SafetyStop)
        self.assertIn("ended without answering (exit code 3)", str(raised.exception))
        self.assertIn("dyld: missing library libcodex.dylib", str(raised.exception))
        self.assertEqual(runtime._ISOLATED_CODEX, {})

    def test_a_definite_refusal_stays_a_safety_stop_until_the_binary_changes(self):
        codex = self.stand_in("sys.stdin.readline()\nprint(json.dumps({'id': 1, 'result': {'codexHome': '/elsewhere'}}),"
                              " flush=True)\nsys.stdin.read()\n")
        with mock.patch.object(runtime, "_codex_candidates", return_value=[codex]):
            with self.assertRaises(runtime.SafetyStop):
                runtime.api_codex_executable(self.environment)
            with mock.patch.object(runtime, "_reported_codex_home", side_effect=AssertionError("asked twice")), \
                    self.assertRaises(runtime.SafetyStop):
                runtime.api_codex_executable(self.environment)
            # An updated binary is asked again.
            time.sleep(0.01)
            self.stand_in(self.ANSWER)
            os.utime(codex, ns=(time.time_ns(), time.time_ns() + 10_000_000))
            self.assertEqual(runtime.api_codex_executable(self.environment), codex)

    def test_no_codex_at_all_is_a_missing_cli(self):
        with mock.patch.object(runtime, "_codex_candidates", return_value=[]):
            with self.assertRaises(runtime.CLIUnavailable):
                runtime.api_codex_executable(self.environment)

    def test_a_first_use_of_a_private_home_gets_a_longer_limit(self):
        (self.home / "config.toml").unlink()
        codex = self.stand_in("time.sleep(1)\n" + self.ANSWER)
        with mock.patch.object(runtime, "_codex_candidates", return_value=[codex]), \
                mock.patch.object(runtime, "CODEX_HOME_PROBE_SECONDS", 0.3), \
                mock.patch.object(runtime, "CODEX_HOME_FIRST_PROBE_SECONDS", 10):
            self.assertEqual(runtime.api_codex_executable(self.environment), codex)


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class ClaudeAuthorDecisionTests(claude_test.FakeClaudeCase):
    """The Claude author: reopen prompt, feedback once, a replaced conversation and a quiet-CLI check."""

    THREAD = "123e4567-e89b-42d3-a456-426614174000"
    PROMPTS = {**claude_test.PROMPTS, "reopen": "Continue the same goal in this conversation."}

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        patch_clock(self, self.clock)

    def launcher(self, prompts=None):
        return functools.partial(claude_cli.goal_session, runtime, "Prove S exactly.", prompts=prompts or self.PROMPTS,
                                 settings={"model": "claude-opus-5-5", "effort": "max", "speed": "standard",
                                           "summary": "concise"},
                                 features=[], stages=None, node_name="author")

    def start(self, initial_instruction="", **options):
        session = self.launcher()(options={"goal_cwd": str(self.run_dir), **options},
                                  initial_instruction=initial_instruction)
        self.addCleanup(session.close)
        return session

    def resume_text(self):
        return self.PROMPTS["resume"].format(directory=self.run_dir, original_prompt="Prove S exactly.")

    def test_a_resumed_session_gets_the_reopen_prompt_and_a_new_one_the_full_resume_prompt(self):
        self.script([["init", "echo", claude_test.complete()]])
        self.assertEqual(next(self.start(goal_thread_id=self.THREAD, goal_resume=True))["outcome"], "done")
        self.assertEqual(self.texts(), [self.PROMPTS["reopen"]])
        self.log.unlink()
        self.script([["init", "echo", claude_test.complete()]], missing_sessions=[self.THREAD])
        self.assertEqual(next(self.start(goal_thread_id=self.THREAD, goal_resume=True))["outcome"], "done")
        # The gone session read the reopen prompt; the new session gets the full resume prompt.
        self.assertEqual(self.texts(), [self.PROMPTS["reopen"], self.resume_text()])

    def test_critic_feedback_is_delivered_once_and_recorded(self):
        repair = "Revision 1; critic round 2: Gap\nCandidate: Proof"
        self.script([["init", "echo", claude_test.complete()]])
        self.assertEqual(next(self.start(repair, goal_thread_id=self.THREAD, goal_resume=True))["outcome"], "done")
        self.assertEqual(self.texts(), [self.PROMPTS["reopen"] + "\n\n" + repair])
        marks = [event for event in self.events if event.get("label") == "Critic feedback delivered"]
        self.assertEqual([(mark["repairDelivered"], mark["threadId"]) for mark in marks],
                         [(runtime._repair_digest(repair), self.THREAD)])
        self.log.unlink()
        self.script([["init", "echo", claude_test.complete()]])
        received = {"thread": self.THREAD, "digest": runtime._repair_digest(repair)}
        self.assertEqual(next(self.start(repair, goal_thread_id=self.THREAD, goal_resume=True,
                                         author_repair_delivered=received))["outcome"], "done")
        self.assertEqual(self.texts(), [self.PROMPTS["reopen"]])

    def test_a_conversation_too_long_for_the_model_continues_in_a_new_session(self):
        work = {"emit": {"type": "assistant", "parent_tool_use_id": None,
                         "uuid": "0c6f7f5e-58f6-4b8e-9f37-0d9b3b0c2a11",
                         "message": {"content": [{"type": "text", "text": "Lemma 1 holds."}]}}}
        overflow = ["init", "echo", work, {"result": {
            "is_error": True, "result": "API Error: 400 context_length_exceeded: the input is too long",
            "api_error_status": 400}}]
        self.script([overflow, ["init", "echo", claude_test.complete("Done.")]])
        launch = self.launcher()
        options = {"goal_cwd": str(self.run_dir)}
        sessions = {"author": launch(options=options, initial_instruction="")}
        self.addCleanup(lambda: sessions["author"].close())
        self.assertEqual(runtime._goal_step(sessions, "author", launch, "", options, next, "solve")["output"], "Done.")
        starts = [row["argv"] for row in self.records("start")]
        self.assertEqual([argv[0] for argv in starts], ["--session-id", "--session-id"])
        self.assertNotEqual(starts[0][1], starts[1][1])
        self.assertEqual(self.texts(), ["Prove S exactly.", self.resume_text()])
        replaced = [event for event in self.events if event.get("label") == "Conversation replaced"]
        self.assertEqual(len(replaced), 1)
        self.assertIn("no longer fits the model's context", replaced[0]["text"])

    def test_a_conversation_too_long_for_the_model_keeps_the_refusal_note_until_the_next_submission(self):
        # As in _execute: the attempts before the next submission carry the note, the later ones do not.
        note = "Your last submission was not handed to the critic: proof.md: file not found."

        def overflow(identity):
            work = {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": identity,
                             "message": {"content": [{"type": "text", "text": "Lemma 1 holds."}]}}}
            return ["init", "echo", work, {"result": {
                "is_error": True, "result": "API Error: 400 context_length_exceeded: the input is too long",
                "api_error_status": 400}}]

        self.script([overflow("0c6f7f5e-58f6-4b8e-9f37-0d9b3b0c2a11"), ["init", "echo", claude_test.complete("First")],
                     overflow("5d0f4a2e-3b7c-4c1d-9e8f-6a5b4c3d2e1f"), ["init", "echo", claude_test.complete("Done.")]])
        plain = self.launcher()
        told = functools.partial(plain, handoff_note=note)
        options = {"goal_cwd": str(self.run_dir)}
        sessions = {"author": told(options=options, initial_instruction="")}
        self.addCleanup(lambda: sessions["author"].close())
        self.assertEqual(runtime._goal_step(sessions, "author", told, "", options, next, "solve")["output"], "First")
        feedback = {"solution": "First", "bugs": "Lemma 2 fails.", "round": 1}
        self.assertEqual(runtime._goal_step(sessions, "author", plain, "", options,
                                            lambda session: session.send(feedback), "repair")["output"], "Done.")
        repair = "Revision 1; critic round 1: Lemma 2 fails.\nCandidate: First"
        self.assertEqual(self.texts(), ["Prove S exactly.\n\n" + note, self.resume_text() + "\n\n" + note, repair,
                                        self.resume_text() + "\n\n" + repair])
        self.assertEqual(self.labels("status").count("Conversation replaced"), 2)

    def test_a_quiet_cli_that_does_not_answer_a_check_ends_the_attempt(self):
        # Hung long past the check; a CLI that ends by itself would read as a different failure.
        self.script([["init", "echo", {"hang": 8}, {"exit": 1}]])
        with mock.patch.object(runtime, "AUTHOR_INACTIVITY_SECONDS", 0.5), \
                mock.patch.object(runtime, "AUTHOR_PROBE_SECONDS", 0.5), \
                mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 0.5):
            with self.assertRaises(runtime.WorkflowPaused) as raised:
                next(self.start())
        self.assertTrue(raised.exception.reason.startswith("The Claude CLI stopped responding: no event for"),
                        raised.exception.reason)
        self.assertIsNotNone(raised.exception.retry)
        controls = [message for message in self.inputs("control_request")
                    if message["request"]["subtype"] == "mcp_status"]
        self.assertEqual(len(controls), 0)  # The hung CLI never read its input.
        self.assertTrue(any(event["kind"] == "diagnostic" and event["text"].startswith("No author event for")
                            for event in self.events))


if __name__ == "__main__":
    unittest.main()
