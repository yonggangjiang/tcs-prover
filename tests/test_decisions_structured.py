"""The owner's decisions on the structured-call transport.

I-042: an attempt is stopped only after its CLI wrote nothing for a while, never
while it works; the workflow deadline still bounds it. I-102: the watchdog and
heartbeat are armed before a long prompt is written. I-109: a timeout or close
stops the CLI's whole process tree, and a process left holding the output never
holds up the call. I-125: each failed attempt is logged in full in the run
folder. I-128: a failed turn that exits 0 is a provider failure. I-039 / I-036:
malformed events and output lines are skipped. I-100: a CLI that is missing or
incompatible is named. I-007: a workflow's `attempts` is ignored, said once.

Offline: stand-in Codex and Claude CLIs (found through a patched lookup or a
PATH of their own) and scripted app-server output; no model call is made and no
real CLI or login can be reached.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli

import test_claude_cli as claude_test
import test_model_retry as retry_test

SCHEMA = retry_test.SCHEMA
VALID = retry_test.VALID

# A stand-in `codex`: `exec` (also `exec fork <id> -`) plays the next scripted
# behaviour, `delete --force <id>` succeeds and `--version` prints the version
# file. A behaviour may stream reasoning items, leave a child process holding
# the CLI's output, stay silent, never read its prompt, or end a failed turn.
CODEX = r'''
import json, os, subprocess, sys, time, uuid
STATE = os.environ["TCS_STAND_IN_STATE"]
argv = sys.argv[1:]


def log(record):
    with open(os.path.join(STATE, "log.jsonl"), "a") as stream:
        stream.write(json.dumps(record) + "\n")


if argv[:1] == ["--version"]:
    log({"version": True})
    path = os.path.join(STATE, "version")
    print(open(path).read().strip() if os.path.exists(path) else "codex-cli 0.160.0")
    sys.exit(int(os.environ.get("TCS_STAND_IN_VERSION_EXIT", "0")))
if "exec" not in argv:
    sys.exit(0)
script = json.load(open(os.path.join(STATE, "script.json")))
pointer = os.path.join(STATE, "next")
position = int(open(pointer).read()) if os.path.exists(pointer) else 0
open(pointer, "w").write(str(position + 1))
behaviour = script[min(position, len(script) - 1)]
if "ignore_input" in behaviour:
    log({"argv": argv, "stdin": None})
    time.sleep(behaviour["ignore_input"])
    sys.exit(0)
log({"argv": argv, "stdin": sys.stdin.read()})


def event(value):
    sys.stdout.write((value if isinstance(value, str) else json.dumps(value)) + "\n")
    sys.stdout.flush()


for line in behaviour.get("stderr", []):
    sys.stderr.write(line + "\n")
sys.stderr.flush()
if behaviour.get("silent"):
    sys.exit(behaviour.get("exit", 1))
for line in behaviour.get("lines", []):
    event(line)
event({"type": "thread.started", "thread_id": str(uuid.uuid4())})
event({"type": "turn.started"})
for index in range(behaviour.get("stream", 0)):
    event({"type": "item.completed", "item": {"id": "r%d" % index, "type": "reasoning", "text": "Checking."}})
    time.sleep(behaviour.get("every", 0))
if "child" in behaviour:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%d)" % behaviour["child"]])
    open(os.path.join(STATE, "child.pid"), "w").write(str(child.pid))
if "hang" in behaviour:
    time.sleep(behaviour["hang"])
for message in behaviour.get("errors", []):
    event({"type": "error", "message": message})
if "turn_failed" in behaviour:
    event({"type": "turn.failed", "error": {"message": behaviour["turn_failed"]}})
if "answer" in behaviour:
    open(argv[argv.index("-o") + 1], "w").write(behaviour["answer"])
    event({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": behaviour["answer"]}})
    event({"type": "turn.completed", "usage": {"input_tokens": 3, "cached_input_tokens": 0}})
sys.exit(behaviour.get("exit", 0))
'''


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gone(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class AuthorStarted(BaseException):
    """A workflow test reached the author, which would start a real CLI session."""


@unittest.skipIf(os.name == "nt", "The stand-in CLIs are POSIX scripts")
class StandInCase(retry_test.RetryCase):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir()
        self.fake = self.root / "codex"
        self.fake.write_text(f"#!{sys.executable}\n{CODEX}")
        self.fake.chmod(0o700)
        environment = mock.patch.dict(os.environ, {"TCS_STAND_IN_STATE": str(self.state),
                                                    "CODEX_HOME": str(self.root / "codex-home"),
                                                    "TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        for name, value in (("codex", mock.Mock(return_value=str(self.fake))),
                            ("goal_session", mock.Mock(side_effect=AuthorStarted))):
            patcher = mock.patch.object(runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        author = mock.patch.object(claude_cli, "goal_session", side_effect=AuthorStarted)
        author.start()
        self.addCleanup(author.stop)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.addCleanup(self.kill_children)

    def script(self, *behaviours):
        (self.state / "script.json").write_text(json.dumps(list(behaviours)))

    def execs(self):
        path = self.state / "log.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if "argv" in row]

    def child(self):
        return int((self.state / "child.pid").read_text())

    def kill_children(self):
        path = self.state / "child.pid"
        if path.exists():
            kill_quietly(int(path.read_text()))

    def diagnostics(self):
        return [event for event in self.events if event["kind"] == "diagnostic"]


class StallTests(StandInCase):
    """I-042 and I-102: no wall-clock cap; only silence, or the workflow deadline, stops an attempt."""

    def test_an_attempt_that_keeps_writing_is_never_stopped(self):
        # Five seconds of work with an event every 0.25 s, against an inactivity limit of 1.5 s.
        self.script({"stream": 20, "every": 0.25, "answer": VALID})
        started = time.monotonic()
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", timeout=1.5,
                                       options={"thinking_hours": 0})
        self.assertEqual(result["notes"], "ok")
        self.assertGreater(time.monotonic() - started, 4)
        self.assertEqual(len(self.execs()), 1)
        self.assertEqual(self.notices(), [])

    def test_a_silent_attempt_is_stopped_after_its_inactivity_limit_also_without_a_time_limit(self):
        self.script({"stream": 1, "hang": 30}, {"answer": VALID})
        started = time.monotonic()
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", timeout=1,
                                       options={"thinking_hours": 0})
        self.assertEqual(result["notes"], "ok")
        self.assertLess(time.monotonic() - started, 20)
        execs = self.execs()
        self.assertEqual(len(execs), 2)
        self.assertEqual(execs[1]["stdin"], runtime.STRUCTURED_RETRY_NOTE.format(
            cause="was stopped after 1 s without activity"))
        self.assertIn("The critic model wrote no output for 1 s; the attempt was stopped.",
                      self.diagnostics()[0]["text"])
        # The watchdog's own words are not quoted as a provider message.
        self.assertEqual(self.notices()[0]["text"],
                         "Model call failed (no output for 1 s) — retrying in 1 min (attempt 2).")

    def test_the_workflow_deadline_still_bounds_an_attempt_that_works(self):
        self.script({"stream": 60, "every": 0.1, "answer": VALID})
        started = time.monotonic()
        with self.assertRaisesRegex(runtime.Error, "Workflow time limit reached"):
            runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol",
                               options={"thinking_hours": 1, "elapsed_seconds": 3600 - 1.5})
        self.assertLess(time.monotonic() - started, 5)  # Stopped long before its six seconds of work.
        self.assertIn("The critic model call reached the workflow time limit.", self.diagnostics()[0]["text"])

    def test_a_cli_that_never_reads_a_long_prompt_is_stopped_and_reported_alive_meanwhile(self):
        self.script({"ignore_input": 30})
        started = time.monotonic()
        with mock.patch.object(runtime, "STRUCTURED_HEARTBEAT_SECONDS", 0.2):
            with self.assertRaises(runtime.StructuredAttemptTimeout):
                runtime.structured("x" * 300_000, SCHEMA, "critic", model="gpt-6.1-sol", timeout=1, attempts=1)
        self.assertLess(time.monotonic() - started, 15)
        heartbeats = [event for event in self.events if event.get("heartbeat")]
        self.assertGreaterEqual(len(heartbeats), 2)

    def test_a_timeout_stops_the_processes_the_cli_started(self):
        self.script({"stream": 1, "child": 120, "hang": 120})
        started = time.monotonic()
        with self.assertRaises(runtime.StructuredAttemptTimeout):
            runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", timeout=1, attempts=1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(gone(self.child()))

    def test_a_process_left_holding_the_output_never_holds_up_the_answer(self):
        self.script({"stream": 1, "child": 120, "answer": VALID})
        started = time.monotonic()
        with mock.patch.object(runtime, "CLI_OUTPUT_GRACE_SECONDS", 0.5):
            result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol")
        self.assertEqual(result["notes"], "ok")
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(self.notices(), [])


@unittest.skipIf(os.name == "nt", "The stand-in CLIs are POSIX scripts")
class ClaudeStallTests(claude_test.FakeClaudeCase):
    def test_subagent_messages_count_as_activity(self):
        subagent = {"emit": {"type": "assistant", "parent_tool_use_id": "toolu_task", "uuid": "s1",
                             "message": {"content": [{"type": "text", "text": "Checking lemma 2."}]}}}
        self.script([["init", *([subagent, {"sleep": 0.3}] * 10), {"structured": {"statement": "S", "notes": "ok"}}]])
        started = time.monotonic()
        result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", timeout=1.2,
                                       features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        self.assertGreater(time.monotonic() - started, 2.5)
        self.assertEqual(len(self.records("start")), 1)

    def test_a_cli_that_ends_before_its_init_message_is_named_after_three_identical_failures(self):
        refusal = [{"stderr": "error: unknown option '--safe-mode'"}, {"exit": 1}]
        self.script([refusal, refusal, refusal, ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5", options={})
        self.assertEqual(result["notes"], "ok")
        notices = [event for event in self.events if event.get("label") == runtime.RETRY_LABEL and "retryAt" in event]
        self.assertEqual(len(notices), 3)
        self.assertTrue(notices[1]["text"].startswith("Model call failed"), notices[1]["text"])
        self.assertTrue(notices[2]["text"].startswith("Claude CLI missing or incompatible — retrying in"),
                        notices[2]["text"])
        self.assertIn("CLI message: error: unknown option '--safe-mode'", notices[2]["text"])
        self.assertEqual(notices[2]["failureKind"], "cli")

    def test_a_failed_attempt_is_logged_with_its_provider_message_and_stderr(self):
        log = self.root / "model-failures.log"
        self.script([["init", {"stderr": "gateway: upstream reset"},
                      {"result": {"is_error": True, "result": "API Error: 529 Overloaded", "api_error_status": 529}},
                      {"exit": 1}],
                     ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5", options={"failure_log": str(log)})
        text = log.read_text()
        self.assertIn("Provider messages:\nAPI Error: 529 Overloaded", text)
        self.assertIn("CLI stderr:\ngateway: upstream reset", text)


class FailureDetailTests(StandInCase):
    """I-125 and I-128: the whole cause is kept, and a failed turn is a provider failure."""

    ERRORS = ["exceeded retry limit, last status: 429 Too Many Requests",
              *[f"detail {index}: {'x' * 900}" for index in range(10)],
              "You've hit your usage limit. Try again in 3 hours."]
    STDERR = [f"stderr line {index}" for index in range(61)]

    def test_every_failed_attempt_is_logged_in_full_and_named_in_its_diagnostic(self):
        log = self.root / "model-failures.log"
        failing = {"errors": self.ERRORS, "stderr": self.STDERR, "exit": 1}
        self.script(failing, failing, {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol",
                                       options={"failure_log": str(log)})
        self.assertEqual(result["notes"], "ok")
        text = log.read_text()
        for line in (*self.ERRORS, *self.STDERR):
            self.assertIn(line, text)
        self.assertIn("· the model call of the statement reviewer · attempt 1 failed:\nCodex review failed:", text)
        self.assertIn("attempt 2 failed the same way as attempt 1.", text)
        self.assertEqual(text.count("stderr line 60"), 2)  # The full entry once; its repetition is one line.
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        diagnostic = self.diagnostics()[0]["text"]
        self.assertTrue(diagnostic.endswith("Full details: model-failures.log in the run folder."), diagnostic)
        # Clipped in the middle: the newest stderr line survives in the card.
        self.assertIn("stderr line 60", diagnostic)
        notice = self.notices()[0]
        self.assertEqual(notice["failure"], "usage limit reached")
        self.assertIn("Provider message: You've hit your usage limit. Try again in 3 hours.", notice["text"])

    def test_the_log_is_never_written_through_a_link(self):
        outside = self.root / "outside.txt"
        outside.write_text("unchanged\n")
        failing = {"errors": ["stream disconnected before completion"], "exit": 1}
        for link in (os.symlink, os.link):
            with self.subTest(link=link.__name__):
                log = self.root / f"{link.__name__}-failures.log"
                link(outside, log)
                (self.state / "next").unlink(missing_ok=True)
                self.script(failing, {"answer": VALID})
                runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol",
                                   options={"failure_log": str(log)})
                self.assertEqual(outside.read_text(), "unchanged\n")
                self.assertNotIn("Full details", self.diagnostics()[-1]["text"])

    def test_a_failed_turn_that_exits_0_is_retried_as_a_provider_failure(self):
        message = "stream disconnected before completion: transport error"
        self.script({"stream": 1, "errors": [message], "turn_failed": message},
                    {"errors": [message], "turn_failed": message},
                    {"errors": [message], "turn_failed": message},
                    {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol")
        self.assertEqual(result["notes"], "ok")
        notices = self.notices()
        self.assertEqual({notice["failure"] for notice in notices}, {"connection lost"})
        self.assertEqual({notice["failureKind"] for notice in notices}, {"model_call"})
        execs = self.execs()
        note = runtime.STRUCTURED_RETRY_NOTE.format(cause="ended early because of a model-call failure")
        # The first attempt worked: the retries fork it, each with the same single note.
        self.assertEqual([entry["stdin"] for entry in execs], ["Review S.", note, note, note])
        self.assertTrue(all("fork" in entry["argv"] for entry in execs[1:]))
        self.assertFalse(any("RECOVERY RETRY" in entry["stdin"] for entry in execs))
        self.assertIn(f"Codex review failed: {message}", self.diagnostics()[0]["text"])

    def test_a_context_overflow_reported_with_exit_0_starts_over_from_the_exact_input(self):
        self.script({"stream": 1, "turn_failed": "context_length_exceeded: input exceeds the context window"},
                    {"answer": VALID})
        runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol")
        execs = self.execs()
        self.assertEqual(execs[1]["stdin"], "Review S.")
        self.assertNotIn("fork", execs[1]["argv"])

    def test_an_answer_written_despite_an_error_event_is_accepted(self):
        self.script({"errors": ["Model metadata not found; using defaults."], "answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol")
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(len(self.execs()), 1)

    def test_the_retry_notice_quotes_the_line_that_names_the_cause(self):
        reason = runtime._failure_reason
        self.assertEqual(reason("Codex review failed: exceeded retry limit, last status: 429 Too Many Requests\n"
                                "You've hit your usage limit. Try again in 3 hours."),
                         "You've hit your usage limit. Try again in 3 hours.")
        self.assertEqual(reason("Claude review failed: API Error: 529 Overloaded\nAPI status 529.\n"
                                "Claude exited with code 1."), "API Error: 529 Overloaded")
        # Lines the runner adds are never the provider's message; else the newest line is.
        self.assertEqual(reason("Claude review failed: Something odd\nClaude exited with code 1."), "Something odd")
        self.assertEqual(reason("Codex app-server stopped unexpectedly.\nError: Operation not permitted"),
                         "Error: Operation not permitted")
        self.assertEqual(reason("Codex critic failed: first line\nsecond line\nnewest line"), "newest line")
        # A CLI's own error line comes before its usage hints.
        self.assertEqual(reason("Codex app-server stopped unexpectedly.\nerror: unexpected argument '--enable' found"
                                "\n\nUsage: codex app-server [OPTIONS]\n\nFor more information, try '--help'."),
                         "error: unexpected argument '--enable' found")
        self.assertEqual(reason(f"Codex review failed: {retry_test.CAPACITY}"), retry_test.CAPACITY)

    def test_claude_failure_details_keep_their_newest_text(self):
        detail = claude_cli.failure_detail({"is_error": True, "result": "A" * 9000 + " the real cause"}, [],
                                           "", runtime)
        self.assertTrue(detail.endswith("the real cause"), detail[-40:])
        self.assertLess(len(detail), 8200)


class MalformedOutputTests(StandInCase):
    """I-039 and I-036: lines that are not events are skipped; only real failures end a call."""

    def test_events_with_odd_types_are_skipped_and_the_answer_accepted(self):
        lines = ['{"type": 5}', '{"type": ["item.completed"]}', '{"type": "item.completed", "item": {"type": ["x"]}}',
                 '{"method": {"a": 1}}', '"text"', "[1, 2]", "null", "not json",
                 '{"type": "item.completed", "item": {"type": "collab_tool_call", '
                 '"agents_states": {"t": {"status": ["x"]}}}}']
        self.script({"lines": lines, "answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol")
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(len(self.execs()), 1)
        malformed = [event for event in self.diagnostics() if event["text"] == "Codex returned a malformed event."]
        self.assertEqual(len(malformed), 4)

    def test_a_policy_change_at_the_repeated_check_is_a_configuration_error(self):
        refusal = retrieval_runtime.RetrievalPolicyError("Child search mode differs from the saved run settings.")
        with mock.patch.object(runtime, "structured_tool_arguments", side_effect=refusal):
            with self.assertRaisesRegex(runtime.ConfigurationError, "differs from the saved run settings"):
                runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol")
        self.assertEqual(self.execs(), [])
        self.assertEqual(self.clock.sleeps, [])

    def rpc(self, stdout, stderr=""):
        process = SimpleNamespace(stdout=io.StringIO(stdout), stdin=io.StringIO(), stderr=io.StringIO(stderr))
        return runtime.RPC(process)

    def test_the_app_server_reader_skips_lines_that_are_not_messages(self):
        message = {"method": "thread/started", "params": {"thread": {"id": "t"}}}
        rpc = self.rpc("Welcome to the codex launcher\n\n123\n\"text\"\n" + json.dumps(message) + "\n")
        self.assertEqual(rpc.wire(), message)
        skipped = [event["text"] for event in self.diagnostics()]
        self.assertEqual(len(skipped), 3)  # The blank line is skipped silently.
        self.assertIn("Welcome to the codex launcher", skipped[0])
        # A complete last message without a line break is still read.
        self.assertEqual(self.rpc(json.dumps(message)).wire(), message)

    def test_output_that_ends_in_a_partial_line_reports_the_stderr_tail(self):
        rpc = self.rpc('{"id": 1, "res', "fatal: the app-server crashed\n")
        with self.assertRaises(runtime.Error) as raised:
            rpc.wire()
        self.assertEqual(str(raised.exception), "Codex app-server stopped unexpectedly.\nfatal: the app-server crashed")
        self.assertTrue(runtime.retryable_failure(raised.exception))

    def test_the_codex_home_check_skips_a_launcher_banner(self):
        home = self.root / "private-home"
        home.mkdir()
        server = self.root / "app-server"
        server.write_text(f"#!{sys.executable}\nimport json, sys\nsys.stdin.readline()\n"
                          "print('Codex launcher 1.0'); print('42')\n"
                          f"print(json.dumps({{'id': 1, 'result': {{'codexHome': {str(home)!r}}}}}))\n"
                          "sys.stdout.flush(); sys.stdin.read()\n")
        server.chmod(0o700)
        reported = runtime._reported_codex_home(str(server), {**os.environ, "CODEX_HOME": str(home)})
        self.assertEqual(reported, home.resolve())
        self.assertEqual(len(self.diagnostics()), 2)

    def test_the_historical_preflight_reader_skips_lines_that_are_not_messages(self):
        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(
            'launcher banner\n7\n[1]\n{"id": 1, "result": {"codexHome": "/h"}}\n'))
        self.assertEqual(retrieval_runtime._ProbeRPC(process).call("initialize", {}), {"codexHome": "/h"})


class CLIProblemTests(StandInCase):
    """I-100: a CLI that cannot start is named, while the retries go on."""

    def run_failures(self, make, count=4, options=None, between=None):
        failures = iter(range(count))

        def attempt(*args, **kwargs):
            index = next(failures, None)
            if index is None:
                return VALID
            if between:
                between(index)
            raise make()

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            return runtime.structured("Review S.", SCHEMA, "review", options={} if options is None else options)

    @staticmethod
    def launch_failure():
        failure = runtime.Error("Codex review failed: error: unexpected argument '--ignore-user-config' found")
        failure.cli_problem = "Codex"
        return failure

    def test_three_identical_failures_to_start_name_the_cli(self):
        self.run_failures(self.launch_failure)
        notices = self.notices()
        self.assertEqual(len(notices), 4)
        for notice in notices[:2]:
            self.assertTrue(notice["text"].startswith("Model call failed ("), notice["text"])
            self.assertEqual(notice["failureKind"], "model_call")
        for notice in notices[2:]:
            self.assertTrue(notice["text"].startswith("Codex CLI missing or incompatible — retrying in 1 min"),
                            notice["text"])
            self.assertIn("CLI message: error: unexpected argument '--ignore-user-config' found", notice["text"])
            self.assertEqual((notice["failure"], notice["failureKind"]), ("Codex CLI missing or incompatible", "cli"))

    def test_a_missing_cli_is_named_too(self):
        self.run_failures(lambda: runtime.CLIUnavailable("Claude CLI is not installed or is not on PATH."))
        self.assertEqual(self.notices()[2]["failure"], "Claude CLI missing or incompatible")

    def test_a_cli_that_ran_and_reported_a_provider_failure_is_never_called_incompatible(self):
        def login():
            failure = runtime.Error("Codex review failed: 401 Unauthorized: please run codex login")
            failure.cli_problem = "Codex"
            return failure

        self.run_failures(login)
        self.assertEqual({notice["failure"] for notice in self.notices()}, {"authentication failed"})

    def test_failures_far_apart_are_not_a_streak(self):
        def later(index):
            if index == 2:
                self.clock.offset += 3600  # An hour passed (for example the call succeeded meanwhile).

        self.run_failures(self.launch_failure, count=5, between=later)
        failures = [notice["failure"] for notice in self.notices()]
        self.assertNotEqual(failures[2], "Codex CLI missing or incompatible")
        self.assertEqual(failures[4], "Codex CLI missing or incompatible")

    def test_the_codex_transport_marks_a_cli_that_ended_without_output(self):
        refusal = {"silent": True, "stderr": ["error: unexpected argument '--ignore-user-config' found"], "exit": 2}
        self.script(refusal, refusal, refusal, {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol", options={})
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(self.notices()[2]["failure"], "Codex CLI missing or incompatible")

    def test_a_cli_that_cannot_be_launched_is_marked(self):
        with mock.patch.object(runtime, "codex", return_value=str(self.root / "missing" / "codex")):
            with self.assertRaises(OSError) as raised:
                runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol", attempts=1)
        self.assertEqual(raised.exception.cli_problem, "Codex")
        self.assertEqual(runtime._cli_problem(raised.exception), "Codex")


@unittest.skipIf(os.name == "nt", "The stand-in CLIs are POSIX scripts")
class CLICheckTests(unittest.TestCase):
    """I-100: each runner start names the CLIs' paths and versions; nothing waits."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.bin, self.run_dir, self.state = self.root / "bin", self.root / "run", self.root / "state"
        for directory in (self.bin, self.run_dir, self.state):
            directory.mkdir()
        (self.bin / "codex").write_text(f"#!{sys.executable}\n{CODEX}")
        (self.bin / "claude").write_text(f"#!{sys.executable}\nprint('2.1.289 (Claude Code)')\n")
        for name in ("codex", "claude"):
            (self.bin / name).chmod(0o700)
        environment = mock.patch.dict(os.environ, {"PATH": str(self.bin), "TCS_STAND_IN_STATE": str(self.state),
                                                    "TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        self.events = []
        emitter = mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: self.events.append(
            {"kind": kind, "stage": stage, **fields}))
        emitter.start()
        self.addCleanup(emitter.stop)

    def check(self, *models):
        self.events.clear()
        runtime.check_clis(models or ("gpt-6.1-sol", "claude-opus-5-5"), directory=self.run_dir)
        return self.events

    def test_paths_and_versions_are_named_and_remembered(self):
        events = self.check()
        status = next(event for event in events if event.get("label") == "CLI check")
        self.assertEqual(status["cliVersions"], {
            "Codex": {"path": str(self.bin / "codex"), "version": "codex-cli 0.160.0"},
            "Claude": {"path": str(self.bin / "claude"), "version": "2.1.289 (Claude Code)"}})
        self.assertIn("Codex: codex-cli 0.160.0", status["text"])
        self.assertEqual([event for event in events if event["kind"] == "diagnostic"], [])
        saved = json.loads((self.run_dir / runtime.CLI_VERSIONS_FILENAME).read_text())
        self.assertEqual(saved, status["cliVersions"])
        # A Codex update between two starts of the job is named.
        (self.state / "version").write_text("codex-cli 0.161.0")
        changed = [event["text"] for event in self.check() if event["kind"] == "diagnostic"]
        self.assertEqual(changed, [f"The Codex CLI changed since this job last started: codex-cli 0.160.0 "
                                   f"({self.bin / 'codex'}) is now codex-cli 0.161.0 ({self.bin / 'codex'})."])
        self.assertEqual(json.loads((self.run_dir / runtime.CLI_VERSIONS_FILENAME).read_text())["Codex"]["version"],
                         "codex-cli 0.161.0")
        self.assertEqual([event for event in self.check() if event["kind"] == "diagnostic"], [])

    def test_only_the_clis_of_the_models_are_checked(self):
        events = self.check("gpt-6.1-sol")
        status = next(event for event in events if event.get("label") == "CLI check")
        self.assertEqual(set(status["cliVersions"]), {"Codex"})

    def test_a_missing_or_broken_cli_is_named_and_nothing_waits(self):
        os.environ["PATH"] = str(self.root / "state")  # No CLI here.
        texts = [event["text"] for event in self.check() if event["kind"] == "diagnostic"]
        self.assertEqual(texts, [
            "Codex CLI missing or incompatible: Codex CLI is not installed or is not on PATH. "
            "Model calls retry every minute until it works.",
            "Claude CLI missing or incompatible: Claude CLI is not installed or is not on PATH. "
            "Model calls retry every minute until it works."])
        os.environ.update(PATH=str(self.bin), TCS_STAND_IN_VERSION_EXIT="127")
        texts = [event["text"] for event in self.check("gpt-6.1-sol") if event["kind"] == "diagnostic"]
        self.assertEqual(texts, ["Codex CLI missing or incompatible: `codex --version` exited with code 127 "
                                 "(codex-cli 0.160.0). Model calls retry every minute until it works."])

    def test_web_cutoff_runs_check_their_own_binary(self):
        os.environ["TCS_PROVER_RETRIEVAL_POLICY"] = str(self.root / "policy.json")
        self.assertEqual(self.check(), [])
        self.assertFalse((self.state / "log.jsonl").exists())  # No CLI was started.


@unittest.skipIf(os.name == "nt", "Uses POSIX signals")
class ProcessTreeTests(unittest.TestCase):
    """I-109: stopping a CLI stops what it started; its own SIGTERM still runs."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def parent(self, code):
        pidfile = self.root / "child.pid"
        process = subprocess.Popen([sys.executable, "-c", code, str(pidfile), str(self.root / "terminated")],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pidfile.read_text())
        self.addCleanup(kill_quietly, child)
        return process, child

    SPAWN = ("import signal, subprocess, sys, time\n"
             "def done(*_):\n    open(sys.argv[2], 'w').write('yes'); sys.exit(0)\n"
             "signal.signal(signal.SIGTERM, done)\n"
             "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
             "open(sys.argv[1], 'w').write(str(child.pid))\n")

    def test_stopping_a_cli_stops_its_children_and_lets_it_exit_on_sigterm(self):
        process, child = self.parent(self.SPAWN + "time.sleep(120)\n")
        runtime.stop_process(process)
        self.assertIsNotNone(process.poll())
        self.assertTrue(gone(child))
        self.assertEqual((self.root / "terminated").read_text(), "yes")

    def test_closing_a_session_never_waits_for_a_process_that_holds_its_output(self):
        session = claude_cli.SessionProcess([sys.executable, "-c", self.SPAWN.replace(
            "sys.argv[1]", repr(str(self.root / "child.pid"))) + "print('ready', flush=True)\n"],
            str(self.root), dict(os.environ), lambda text: text)
        deadline = time.monotonic() + 10
        while not (self.root / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int((self.root / "child.pid").read_text())
        self.addCleanup(kill_quietly, child)
        session.process.wait(timeout=10)  # The CLI ended; its child still holds the output.
        closer = threading.Thread(target=session.close, args=(runtime, 0.1), daemon=True)
        closer.start()
        closer.join(15)
        self.assertFalse(closer.is_alive())


class IgnoredAttemptsTests(retry_test.RetryCase):
    """I-007: a workflow's `attempts` is accepted, ignored, and said once."""

    def setUp(self):
        super().setUp()
        for target in (runtime, claude_cli):
            patcher = mock.patch.object(target, "goal_session", side_effect=AuthorStarted)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_attempts_is_ignored_and_said_once(self):
        workflow = self.root / "check.yaml"
        workflow.write_text(runtime.yaml.safe_dump({
            "nodes": {"check": {"run": "structured", "prompt": "check", "attempts": 2,
                                "inputs": {"content": "state.input"},
                                "response": {"statement": "string", "notes": "string"},
                                "after": [{"set": {"output": "result.statement"}}], "next": "end"}},
            "prompts": {"check": "Check {content}"}}))
        outcomes = [runtime.Error(f"Codex review failed: {retry_test.CAPACITY}")] * 3 + [VALID]
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=outcomes) as attempt:
            state = runtime.execute_workflows([workflow], {"input": "S"}, {})
        self.assertEqual(state["output"], "S")
        self.assertEqual(attempt.call_count, 4)  # More than the two attempts the workflow names.
        said = [event for event in self.events if event["kind"] == "diagnostic" and "attempts" in event["text"]]
        self.assertEqual([event["text"] for event in said], [
            "This workflow sets attempts on a structured request. Workflow model calls ignore it: every failed "
            "attempt is retried each minute until the call succeeds."])
        options = {}
        for _ in range(2):
            self.assertNotIn("attempts", runtime._bounded_request_settings(
                runtime._structured_options({"attempts": 3, "stage": "critic"}, options), options))
        self.assertEqual(len([event for event in self.events if "sets attempts" in str(event.get("text"))]), 2)


class MainTests(unittest.TestCase):
    """I-039, I-100 and I-125 in the runner's entry point."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        (self.root / "state.json").write_text('{"statement": "Task", "solution": "Proof."}')
        for target in (runtime, claude_cli):
            patcher = mock.patch.object(target, "goal_session", side_effect=AuthorStarted)
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self):
        arguments = [str(retry_test.ROOT / "workflows/clean_up.yaml"), "--state-file", str(self.root / "state.json")]
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            code = runtime.main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def test_an_unexpected_exception_ends_with_one_error_line(self):
        with mock.patch.object(runtime, "check_clis"), mock.patch.object(
                runtime, "execute_workflows", side_effect=AttributeError("'int' object has no attribute 'startswith'")):
            code, output, errors = self.main()
        self.assertEqual(code, 1)
        lines = errors.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("error: The workflow runner failed unexpectedly (AttributeError in "),
                        lines[0])
        self.assertTrue(lines[0].endswith(": 'int' object has no attribute 'startswith'"), lines[0])
        records = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "diagnostic")
        self.assertIn("Traceback (most recent call last)", records[0]["text"])

    def test_the_runner_logs_failures_in_its_run_folder_and_checks_its_clis_first(self):
        calls = []
        with mock.patch.object(runtime, "check_clis", side_effect=lambda models: calls.append(("check", models))), \
                mock.patch.object(runtime, "execute_workflows", side_effect=lambda paths, state, options: (
                    calls.append(("run", options["failure_log"])) or {"output": "done"})):
            code, _, _ = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("check", [runtime.MODEL, runtime.MODEL]),
                                 ("run", str(self.root / runtime.MODEL_FAILURE_LOG))])


if __name__ == "__main__":
    unittest.main()
