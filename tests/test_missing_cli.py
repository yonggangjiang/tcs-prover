"""A program that is not installed never stops a start: the model call that needs it says so and is retried.

API models run through a program on this computer too (an OpenAI-format connection through the Codex
CLI, an Anthropic-format one through Claude Code), so someone who uses only API models may have
neither installed. Nothing checks for the programs before a start, resume, continue, folder run,
terminal run or reply; the call that needs a missing one names the role, the model and why that model
needs the program, and the existing loops retry it every minute, in the same conversation.

Offline and in-process. Neither program can be found: shutil.which finds neither, and no native
Codex install is offered (_codex_candidates is empty). Nothing can start a CLI: subprocess.Popen and
App._spawn_worker are replaced, and the goal sessions and author conversation turns a test does not
run raise AuthorStarted; a test that runs runner code meets a Popen that raises ProcessStarted. API
connections use fake keys and https://*.invalid base URLs, and every server listens on port 0.
"""

import contextlib
import functools
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, providers
from ui import cli, ownership, review, server

import test_model_retry as retry_test
import test_provider_ui as provider_ui

ROOT = Path(runtime.__file__).parent
WORKFLOWS = ROOT / "workflows"
STATEMENT = "Prove that 1 + 1 = 2."
ANTHROPIC_KEY = "sk-ant-missing-cli-0000000000000000"
OPENAI_KEY = "sk-missing-cli-openai-0000000000"
NO_REVIEW = {"review": False, "author": True, "critic": True, "writing": True}
WITH_REVIEW = {"review": True, "author": True, "critic": True, "writing": True}
# A saved author conversation (a Claude session ID is a UUID).
THREAD = "8b0c2f4e-6f1a-4c55-9a52-3f1d2b7e9c10"
CONVERSATION_ID = "c_0123456789"
# The real author entry points of both programs, for the tests that run them (every other test replaces them).
REAL_CLAUDE_GOAL_SESSION = claude_cli.goal_session
REAL_CLAUDE_CONVERSATION = claude_cli.author_conversation
REAL_CODEX_GOAL_SESSION = runtime.goal_session
REAL_CODEX_CONVERSATION = runtime.author_conversation
NODE = shutil.which("node")


class AuthorStarted(BaseException):
    """A goal session or author conversation turn was reached that this test does not run."""


class ProcessStarted(BaseException):
    """Runner code reached subprocess.Popen: without a CLI to find, that would be a regression."""


class StopRun(BaseException):
    """Ends runner code after the retries a test wanted to see (no runner code catches it)."""


class FakeProcess:
    """What the replaced Popen returns: no runner, reviewer or CLI ever runs."""

    def __init__(self, argv, kwargs):
        self.argv, self.kwargs = [str(part) for part in argv], kwargs
        self.pid = None  # ownership.note_runner ignores a pid that is not an int.
        self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(""), io.StringIO("")
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.returncode = 0
        return "", ""

    def kill(self):
        self.returncode = -9

    terminate = kill

    def send_signal(self, signal):
        pass

    @property
    def program(self):
        return Path(self.argv[2]).name if len(self.argv) > 2 and self.argv[1] == "-u" else self.argv[0]


class Lines:
    """A runner's output that yields the given lines, then ends its reader mid-run (StopRun)."""

    def __init__(self, lines):
        self.lines = list(lines)

    def __iter__(self):
        return self

    def __next__(self):
        if self.lines:
            return self.lines.pop(0)
        raise StopRun

    def close(self):
        pass


class NoCliCase(unittest.TestCase):
    """A private API store with one Anthropic-format and one OpenAI-format connection, and no CLI anywhere."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.store = self.root / "state" / "api-models.json"
        which = shutil.which
        for change in (
                mock.patch.dict(os.environ, {
                    "HOME": str(self.root / "home"), "CODEX_HOME": str(self.root / "home/.codex"),
                    "CLAUDE_CONFIG_DIR": str(self.root / "home/.claude"), providers.STORE_ENV: str(self.store),
                    "TCS_PROVER_RETRIEVAL_MODE": "live"}),
                # Neither program can be found, on PATH or at a native install.
                mock.patch.object(shutil, "which", side_effect=lambda name, *args, **kwargs: (
                    None if Path(str(name)).stem in {"codex", "claude"} else which(name, *args, **kwargs))),
                mock.patch.object(runtime, "_codex_candidates", return_value=[]),
                mock.patch.object(server, "grant_windows_access"),
                # Only a test that runs an author entry point itself (behind a Popen that fails) reaches one.
                mock.patch.object(runtime, "goal_session", side_effect=AuthorStarted("a Codex goal session")),
                mock.patch.object(claude_cli, "goal_session", side_effect=AuthorStarted("a Claude goal session")),
                mock.patch.object(runtime, "author_conversation", side_effect=AuthorStarted("a Codex reply turn")),
                mock.patch.object(claude_cli, "author_conversation", side_effect=AuthorStarted("a Claude reply turn")),
        ):
            change.start()
            self.addCleanup(change.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        self.addCleanup(providers._LAST_GOOD.pop, str(self.store), None)
        self.assertIsNone(shutil.which("codex"))
        self.assertIsNone(shutil.which("claude"))
        anthropic = providers.save_api_connection(
            {"format": "anthropic", "baseUrl": "https://anthropic-gw.invalid", "apiKey": ANTHROPIC_KEY,
             "label": "Anthropic gateway"}, [{"id": "claude-gw-1", "label": "Claude GW 1"}])
        openai = providers.save_api_connection(
            {"format": "openai", "baseUrl": "https://openai-gw.invalid/v1", "apiKey": OPENAI_KEY,
             "label": "OpenAI gateway"}, [{"id": "gpt-gw-1", "label": "GPT GW 1"}])
        self.anthropic, self.anthropic_connection = anthropic["models"][0]["modelId"], anthropic["id"]
        self.openai, self.openai_connection = openai["models"][0]["modelId"], openai["id"]
        # The four kinds of model a role can use, and the program each runs through.
        self.models = {"Anthropic-format API": self.anthropic, "OpenAI-format API": self.openai,
                       "built-in Claude": "claude-opus-5-5", "built-in Codex": "gpt-6.1-sol"}
        self.programs = {self.anthropic: "Claude", self.openai: "Codex", "claude-opus-5-5": "Claude",
                         "gpt-6.1-sol": "Codex"}

    def capture(self):
        """Record what runner code emits in self.events."""

        self.events = []
        emitter = mock.patch.object(runtime, "emit", side_effect=lambda kind, stage=None, **fields: (
            self.events.append({"kind": kind, "stage": stage, **fields})))
        emitter.start()
        self.addCleanup(emitter.stop)

    def records(self, kind, label=None):
        return [event for event in self.events if event["kind"] == kind
                and (label is None or event.get("label") == label)]

    def notices(self, stage=None):
        """The retry notices (one per failed attempt), optionally of one stage."""

        return [event for event in self.records("status") if str(event.get("label")).endswith(runtime.RETRY_LABEL)
                and "retryAt" in event and (stage is None or event["stage"] == stage)]

    @contextlib.contextmanager
    def retrying(self, waits=2):
        """Runner code here: retry waits take no time, and the wait after ``waits`` retries ends it (StopRun).

        Yields the error of each wait. Reaching Popen (a CLI start) raises ProcessStarted.
        """

        errors, original = [], runtime.wait_before_retry

        def wait(options, stage, attempt, error, **kwargs):
            errors.append(error)
            if len(errors) > waits:
                raise StopRun
            return original(options, stage, attempt, error, **kwargs)

        clock = retry_test.FakeClock()
        with mock.patch.object(runtime, "wait_before_retry", wait), \
                mock.patch.object(runtime.subprocess, "Popen", side_effect=ProcessStarted("a process was started")), \
                mock.patch.object(runtime, "_now", clock.now), mock.patch.object(runtime, "_wall_clock", clock.wall), \
                mock.patch.object(runtime, "_retry_sleep", clock.sleep), \
                mock.patch.object(runtime, "_retry_jitter", lambda seconds: 0.0):
            yield errors


class ServerCase(NoCliCase):
    """A real Server on port 0 over an empty runs folder; what it launches is recorded, never run."""

    def setUp(self):
        super().setUp()
        self.launches, self.workers = [], []

        def popen(argv, *args, **kwargs):
            process = FakeProcess(argv, kwargs)
            self.launches.append(process)
            return process

        def spawn(app, target, args, token):  # A plain function, so App binds itself to it.
            with app.lock:
                app.worker_token = token
            self.workers.append((app, getattr(target, "__name__", str(target)), args))

        for change in (mock.patch.object(server.subprocess, "Popen", side_effect=popen),
                       mock.patch.object(server.App, "_spawn_worker", spawn),
                       mock.patch.object(server, "stop_process_tree"),
                       mock.patch.object(cli, "stop_process_tree")):
            change.start()
            self.addCleanup(change.stop)
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(self.http.server_close)
        self.assertNotEqual(self.http.server_port, 8765)
        # HTTP only: each test drives the retry loop of Server.serve_forever itself (_retry_paused_jobs).
        worker = threading.Thread(target=ThreadingHTTPServer.serve_forever, args=(self.http, 0.01), daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            worker.join(timeout=2)
        self.addCleanup(stop)

    def call(self, path, body=None):
        headers = {"Origin": self.http.origin, "X-TCS-Prover-Token": self.http.token}
        raw = None
        if body is not None:
            raw, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=30)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        for key in (ANTHROPIC_KEY, OPENAI_KEY):
            self.assertNotIn(key.encode(), data)  # No answer carries a key.
        return response.status, json.loads(data)

    @staticmethod
    def body(model, stages=NO_REVIEW, **more):
        return {"content": STATEMENT, "reviewModel": model, "authorModel": model, "criticModel": model,
                "writerModel": model, "workflowStages": stages, "thinkingHours": 1, **more}

    def programs_since(self, before):
        return [process.program for process in self.launches[before:]]

    def assert_started(self, app, phase="running"):
        """The job runs (or reviews) with nothing held back, and nothing asks the user to start it."""

        self.assertEqual(app.state["phase"], phase)
        self.assertEqual(app.state.get("launchError") or "", "")
        self.assertEqual(app.state.get("launchHold") or "", "")

    def start_job(self, model, stages=NO_REVIEW):
        before = len(self.launches)
        status, reply = self.call("/start-workflow", self.body(model, stages))
        self.assertEqual(status, 200, reply)
        self.assertEqual(self.programs_since(before), ["workflow_runner.py"])
        return self.http.jobs[reply["runId"]]

    def settle(self, app, records=(), returncode=0):
        """Feed the job's (fake) runner output to its reader, as the reader thread would."""

        with app.lock:
            token = app.active_token or app.worker_token or object()
            app.active_token = app.worker_token = token
            process = app.process if isinstance(app.process, FakeProcess) else FakeProcess(["runner"], {})
            app.process = process
        process.stdout = io.StringIO("".join(json.dumps(record) + "\n" for record in records))
        process.returncode = returncode
        app._read_output(process, token)

    def error_pause(self, app, reason="Model call failed (connection lost)."):
        """The author paused by a model-call error after its conversation started."""

        with app.lock:
            app.state["goalThreadId"] = THREAD
        self.settle(app, [{"kind": "workflow_paused", "stage": "solve", "node": "author",
                           "state": {"statement": STATEMENT, "paused": True}, "reason": reason}])
        self.assertEqual(app.state["phase"], "paused")

    def stop_job(self, app):
        """Stop works like the time limit: the author ends its step and the job stays stopped in place."""

        status, reply = self.call(f"/stop?job={app.state['runId']}", {})
        self.assertEqual(status, 200, reply)
        if app.state["phase"] == "pausing":
            self.settle(app, [{"kind": "workflow_paused", "stage": "solve", "node": "author",
                               "state": {"statement": STATEMENT, "paused": True}, "reason": ""}])
        elif app.state["phase"] == "stopping":
            self.settle(app, returncode=130)
        self.assertEqual((app.state["phase"], app.snapshot()["pauseCause"]), ("paused", "stopped"))

    def tick(self, now=None):
        """One pass of the server's retry loop, free of the spacing between automatic starts."""

        self.http._last_auto_launch = -1e18
        self.http._retry_paused_jobs(now=time.time() + 1 if now is None else now)


class EveryStartWithNeitherProgram(ServerCase):
    """Every start goes ahead for every kind of model while neither program is installed."""

    def test_new_problems_start(self):
        for kind, model in self.models.items():
            for path, stages in (("/start-workflow", NO_REVIEW), ("/start-workflow", WITH_REVIEW), ("/direct", None),
                                 ("/review", None), ("/finalize", None)):
                reviewing = path == "/review" or stages is WITH_REVIEW
                with self.subTest(kind=kind, path=path, review=reviewing):
                    body = self.body(model, stages)
                    if stages is None:  # The older routes take the statement and their own stages.
                        body.pop("workflowStages")
                        body["statement"] = body["content"]
                    before, workers = len(self.launches), len(self.workers)
                    status, reply = self.call(path, body)
                    self.assertEqual(status, 200, reply)
                    app = self.http.jobs[reply["runId"]]
                    self.assert_started(app, "reviewing" if reviewing else "running")
                    if reviewing:  # The statement reviewer's worker starts it.
                        self.assertEqual([name for _, name, _ in self.workers[workers:]], ["_review"])
                        self.assertEqual(self.programs_since(before), [])
                    else:
                        self.assertEqual(self.programs_since(before), ["workflow_runner.py"])
                        self.assertEqual(app.state["stage"], "final" if path == "/finalize" else "solve")

    def test_folder_runs_start_every_job(self):
        for kind, model in self.models.items():
            for stages in (NO_REVIEW, WITH_REVIEW):
                with self.subTest(kind=kind, review=stages["review"]):
                    body = {**self.body(model, stages), "statements": [
                        {"name": "one.md", "statement": "Prove one."}, {"name": "two.md", "statement": "Prove two."}]}
                    body.pop("content")
                    before = len(self.launches)
                    status, reply = self.call("/direct-batch", body)
                    self.assertEqual(status, 200, reply)
                    self.assertEqual((len(reply["startedJobs"]), reply["queuedJobs"], reply["notice"]), (2, [], ""))
                    for run_id in reply["startedJobs"]:
                        self.assert_started(self.http.jobs[run_id], "reviewing" if stages["review"] else "running")
                    self.assertEqual(self.programs_since(before), [] if stages["review"] else ["workflow_runner.py"] * 2)

    def test_resume_and_the_automatic_resume_start_again(self):
        for kind, model in self.models.items():
            with self.subTest(kind=kind, action="Resume"):
                app = self.start_job(model)
                self.error_pause(app)
                before = len(self.launches)
                status, reply = self.call(f"/resume?job={app.state['runId']}", {})
                self.assertEqual(status, 200, reply)
                self.assert_started(app)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])
            with self.subTest(kind=kind, action="automatic resume"):
                self.error_pause(app)
                trace = len(app.state["trace"])
                before = len(self.launches)
                self.tick(now=app.state["autoResumeAt"] + 1)
                self.assert_started(app)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])
                texts = [str(entry.get("text") or "") for entry in app.state["trace"][trace:]]
                self.assertFalse([text for text in texts if text.startswith(("Automatic resume failed",
                                                                             "Automatic resume waits"))], texts)
                self.assertEqual(app.state.get("waitingForSettings") or "", "")

    def test_continue_after_stop_and_continue_at_a_saved_candidate_start(self):
        for kind, model in self.models.items():
            with self.subTest(kind=kind, action="Continue"):
                app = self.start_job(model)
                self.stop_job(app)
                before = len(self.launches)
                status, reply = self.call(f"/continue-stopped?job={app.state['runId']}", {})
                self.assertEqual(status, 200, reply)
                self.assert_started(self.http.jobs[reply["runId"]])
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])
            with self.subTest(kind=kind, action="Continue at critic"):
                source = self.start_job(model)
                self.stop_job(source)
                candidate = source.run_dir / runtime.SAVED_CANDIDATE_FILENAME
                candidate.write_text("1 + 1 = 2 by definition.\n", encoding="utf-8")
                before = len(self.launches)
                status, reply = self.call(f"/resume-checkpoint?job={source.state['runId']}", {"checkpoint": "candidate"})
                self.assertEqual(status, 200, reply)
                self.assert_started(self.http.jobs[reply["runId"]])
                self.assertEqual(self.http.jobs[reply["runId"]].state["stage"], "critic")
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])

    def test_approve_starts_the_proof_after_the_statement_review(self):
        for kind, model in self.models.items():
            with self.subTest(kind=kind):
                status, reply = self.call("/start-workflow", self.body(model, WITH_REVIEW))
                self.assertEqual(status, 200, reply)
                app = self.http.jobs[reply["runId"]]
                with app.lock:  # The review finished: the checked statement waits for approval.
                    app.worker_token = None
                    app.state.update(phase="reviewed", review={"statement": STATEMENT, "notes": ""})
                before = len(self.launches)
                status, reply = self.call(f"/approve?job={app.state['runId']}", {})
                self.assertEqual(status, 200, reply)
                self.assert_started(app)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])

    def test_a_queued_job_starts_when_its_slot_is_free(self):
        self.http.installation = {**self.http.installation, "maxRunningJobs": 1}
        for kind, model in self.models.items():
            with self.subTest(kind=kind):
                body = {**self.body(model), "statements": [
                    {"name": "one.md", "statement": "Prove one."}, {"name": "two.md", "statement": "Prove two."}]}
                body.pop("content")
                status, reply = self.call("/direct-batch", body)
                self.assertEqual(status, 200, reply)
                first, queued = (self.http.jobs[run_id] for run_id in reply["startedJobs"])
                self.assertEqual(reply["queuedJobs"], [queued.state["runId"]])
                self.assertEqual(queued.state["phase"], "prepared")
                self.stop_job(first)  # Its running slot is free again.
                before = len(self.launches)
                self.tick()
                self.assert_started(queued)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])
                self.stop_job(queued)

    def test_replies_the_summary_button_and_the_time_limit_summary_start(self):
        for kind, model in self.models.items():
            for label, path, body in (("reply", "/ask-author", {"text": "How far are you?"}),
                                      ("Summary button", "/author-summary", {})):
                with self.subTest(kind=kind, request=label):
                    app = self.start_job(model)
                    self.error_pause(app)
                    status, reply = self.call(f"{path}?job={app.state['runId']}", body)
                    self.assertEqual(status, 200, reply)
                    before = len(self.launches)
                    self.tick(now=time.time() + 1)
                    item = app._conversation[-1]
                    self.assertEqual((item["status"], item["error"]), ("running", ""))
                    self.assertEqual(self.programs_since(before), ["workflow_runner.py"])
                    self.assertIn("--converse", self.launches[-1].argv)
            with self.subTest(kind=kind, request="summary at the time limit"):
                app = self.start_job(model)
                with app.lock:
                    app.state["goalThreadId"] = THREAD
                self.settle(app, [{"kind": "workflow_paused", "stage": "solve", "node": "author",
                                   "state": {"statement": STATEMENT, "paused": True},
                                   "reason": f"{server.WORKFLOW_TIME_LIMIT_ERROR}; saved work remains resumable."}])
                before = len(self.launches)
                self.tick(now=time.time() + 1)
                item = app._conversation[-1]
                self.assertEqual((item["kind"], item["origin"], item["status"], item["error"]),
                                 ("summary", "time-limit", "running", ""))
                self.assertIn("--converse", self.launches[-1].argv)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])

    def test_a_statement_review_a_restart_interrupted_starts_again(self):
        for kind, model in self.models.items():
            with self.subTest(kind=kind):
                status, reply = self.call("/start-workflow", self.body(model, WITH_REVIEW))
                self.assertEqual(status, 200, reply)
                app = self.http.jobs[reply["runId"]]
                with app.lock:  # A restart interrupted the review; it starts again by itself.
                    app.worker_token = None
                    app.state.update(phase="input", reviewRetryAt=1)
                workers = len(self.workers)
                self.tick()
                self.assert_started(app, "reviewing")
                self.assertEqual([name for _, name, _ in self.workers[workers:]], ["_review"])
                self.assertEqual(app.state.get("reviewRetryAt") or 0, 0)
                self.assertNotIn("could not start again", app.state.get("error") or "")


class TerminalWithNeitherProgram(ServerCase):
    """A terminal file run and --resume-author start and exit 0."""

    def run_cli(self, argv):
        errors, output = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "argv", ["web_ui.py", *map(str, argv)]), \
                mock.patch.object(cli.run_headless_markdown, "__defaults__", (self.runs, None, None, False)), \
                mock.patch.object(cli, "_handle_signals", return_value=None), \
                mock.patch.object(cli, "_restore_signals"), mock.patch.object(ownership, "ROLE", ownership.ROLE), \
                mock.patch.object(cli.sys, "stderr", errors), mock.patch.object(cli.sys, "stdout", output), \
                mock.patch.object(runtime, "configure_standard_streams"):
            code = cli.main()
        return code, errors.getvalue()

    def test_a_file_run_starts_and_exits_zero(self):
        statement = self.root / "one.md"
        statement.write_text("Prove one.\n", encoding="utf-8")

        def finish(app, target, args, token):
            # The runner's reader: the (fake) runner returns a proof and exits 0.
            with app.lock:
                app.worker_token = token
            self.workers.append((app, target.__name__, args))
            process = args[0]
            process.stdout = io.StringIO(json.dumps({"kind": "final_result", "stage": "final",
                                                     "output": "\\documentclass{article}\n"}) + "\n")
            process.returncode = 0
            target(*args)

        for kind, model in self.models.items():
            with self.subTest(kind=kind), mock.patch.object(server.App, "_spawn_worker", finish):
                before = len(self.launches)
                code, errors = self.run_cli([statement, "--author-model", model, "--critic-model", model,
                                             "--writer-model", model])
                self.assertEqual(code, 0, errors)
                self.assertNotIn("Cannot start", errors)
                self.assertIn("[one.md] Proof finished in", errors)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])

    def test_resume_author_starts_and_exits_zero(self):
        for kind, model in self.models.items():
            with self.subTest(kind=kind):
                app = self.start_job(model)
                self.stop_job(app)
                made = []

                def factory(address, runs=None, single_instance=False):
                    made.append(server.Server((server.HOST, 0), runs=self.runs))
                    return made[-1]

                before = len(self.launches)
                with mock.patch.object(cli, "Server", factory), \
                        mock.patch.object(server.Server, "serve_forever", side_effect=KeyboardInterrupt), \
                        mock.patch.object(server.Server, "stop_all"):
                    code, errors = self.run_cli(["--resume-author", app.run_dir, "--no-browser"])
                for manager in made:
                    manager.server_close()
                self.assertEqual(code, 0, errors)
                self.assertNotIn("Cannot resume saved job", errors)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])


class SavedSettingsAreStillChecked(ServerCase):
    """What the user saved is still checked: a model without its key, or a removed model.

    New problem refuses it with today's words; a waiting job keeps waiting for its settings.
    """

    def set_key(self, connection_id, key=""):
        """Change a saved connection's key in the store itself (the form never saves one without a key)."""

        value = json.loads(self.store.read_text(encoding="utf-8"))
        for entry in value["models"]:
            if entry["id"] == connection_id:
                entry["apiKey"] = key
        self.store.write_text(json.dumps(value), encoding="utf-8")

    def test_a_model_without_its_key_or_a_removed_model_is_refused_at_new_problem(self):
        status, reply = self.call("/start-workflow", self.body(self.models["built-in Codex"],
                                                               authorModel="api:gone"))
        self.assertEqual((status, reply["error"]), (400, (
            "The API model api:gone chosen for the proof author is not configured. Add it under Advanced settings → "
            "API connections, or choose another model.")))
        self.set_key(self.openai_connection)
        status, reply = self.call("/start-workflow", self.body(self.openai))
        self.assertEqual((status, reply["error"]),
                         (400, "Add an API key for OpenAI gateway in Advanced settings → API connections."))
        self.assertEqual(self.launches, [])
        self.assertEqual(self.workers, [])

    def test_an_automatic_resume_still_waits_for_the_settings(self):
        for problem in ("key", "removed"):
            with self.subTest(problem=problem):
                app = self.start_job(self.anthropic)
                self.error_pause(app)
                if problem == "key":
                    self.set_key(self.anthropic_connection)
                else:
                    self.assertTrue(providers.delete_api_model(self.anthropic_connection))
                before = len(self.launches)
                self.tick(now=app.state["autoResumeAt"] + 1)
                self.assertEqual(app.state["phase"], "paused")
                self.assertEqual(self.programs_since(before), [])
                self.assertIn("Automatic resume waits for the model settings:", app.state["trace"][-1]["text"])
                self.assertEqual(app.attention()["kind"], "settings")
                # Restored: the next try starts the job again.
                if problem == "key":
                    self.set_key(self.anthropic_connection, ANTHROPIC_KEY)
                else:
                    providers.save_api_connection(
                        {"format": "anthropic", "baseUrl": "https://anthropic-gw.invalid", "apiKey": ANTHROPIC_KEY,
                         "label": "Anthropic gateway"}, [{"id": "claude-gw-1", "label": "Claude GW 1"}])
                self.tick(now=app.state["autoResumeAt"] + 1)
                self.assert_started(app)
                self.assertEqual(self.programs_since(before), ["workflow_runner.py"])


class RunnerWithNeitherProgram(NoCliCase):
    """The runner and the statement reviewer start; each model call names what is missing and retries."""

    def setUp(self):
        super().setUp()
        self.capture()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        previous = Path.cwd()
        os.chdir(self.run_dir)
        self.addCleanup(os.chdir, previous)

    def main(self, arguments, stdin=""):
        errors = io.StringIO()
        with mock.patch.object(runtime.sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(runtime.sys, "stderr", errors), \
                mock.patch.object(runtime, "configure_standard_streams"):
            try:
                code = runtime.main([str(argument) for argument in arguments])
            except StopRun:
                code = "still retrying"
        return code, errors.getvalue()

    def assert_retried(self, stage, text, errors, count=2):
        """Each failed attempt of ``stage`` says ``text`` and is retried with it, every minute."""

        notices = self.notices(stage)
        self.assertEqual(len(notices), count)
        for attempt, notice in enumerate(notices, start=2):
            self.assertEqual(notice["failure"], "the CLI is not available")
            self.assertEqual(notice["retryAttempt"], attempt)
            self.assertEqual(notice["retryInSeconds"], 60)
            self.assertTrue(notice["text"].endswith(f" CLI message: {text}"), notice["text"])
        self.assertEqual([str(error) for error in errors], [text] * (count + 1))

    def startup_notices(self):
        """The start-up notice's diagnostics, without the one that says run confinement is unsupported here.

        That one is added where model processes cannot be confined to their run (not macOS).
        """

        return [event["text"] for event in self.records("diagnostic") if event["stage"] == "workflow"
                and event["text"] != runtime.confinement.UNSUPPORTED_NOTICE]

    def run_author(self, model):
        """main() with every role on ``model``; the real author of its program starts until its third wait."""

        self.events.clear()
        with mock.patch.object(claude_cli, "goal_session", REAL_CLAUDE_GOAL_SESSION), \
                mock.patch.object(runtime, "goal_session", REAL_CODEX_GOAL_SESSION), self.retrying() as errors:
            code, stderr = self.main([WORKFLOWS / "author_critic.yaml", WORKFLOWS / "clean_up.yaml",
                                      "--author-model", model, "--critic-model", model, "--writer-model", model,
                                      "--set", f"workflow_stages={json.dumps(NO_REVIEW)}"], stdin=STATEMENT)
        return code, stderr, errors

    def run_critic(self, model):
        """main() at the critic alone, on ``model``, until its third wait."""

        self.events.clear()
        (self.run_dir / "state.json").write_text(json.dumps({"statement": STATEMENT,
                                                             "solution": "1 + 1 = 2 by definition."}))
        stages = {"review": False, "author": False, "critic": True, "writing": False}
        with self.retrying() as errors:
            code, stderr = self.main([WORKFLOWS / "author_critic.yaml", "--critic-model", model,
                                      "--set", f"workflow_stages={json.dumps(stages)}",
                                      "--state-file", self.run_dir / "state.json"])
        return code, stderr, errors

    def run_summary(self, model):
        """A --converse summary turn of the paused author on ``model`` (its real reply turn) until its third wait."""

        self.events.clear()
        request = {"schemaVersion": 1, "id": CONVERSATION_ID, "kind": "summary", "text": "", "notice": ""}
        stages = {"review": False, "author": True, "critic": False, "writing": False}
        with mock.patch.object(claude_cli, "author_conversation", REAL_CLAUDE_CONVERSATION), \
                mock.patch.object(runtime, "author_conversation", REAL_CODEX_CONVERSATION), \
                self.retrying() as errors:
            code, stderr = self.main(["--converse", WORKFLOWS / "author_critic.yaml", "--author-model", model,
                                      "--set", f"workflow_stages={json.dumps(stages)}",
                                      "--set", f"goal_thread_id={THREAD}", "--set", f"goal_cwd={self.run_dir}",
                                      "--set", "goal_require_resume=true"], stdin=json.dumps(request))
        return code, stderr, errors

    def test_every_role_on_an_anthropic_format_model_starts_and_its_author_retries(self):
        model = self.anthropic
        code, stderr, errors = self.run_author(model)
        self.assertEqual((code, stderr), ("still retrying", ""))  # Never exit code 1.
        text = runtime.missing_cli_text("Claude", model, "solve")
        self.assertEqual(text, (
            "Claude Code is not installed or is not on PATH. The proof author's model claude-gw-1 (API connection "
            "“Anthropic gateway”) is Anthropic-format, and TCS Prover runs Anthropic-format models through Claude "
            "Code (no Claude login needed). Install Claude Code; the job retries every minute and continues by "
            "itself."))
        # The start-up notice names the program once, for the first role that needs it.
        self.assertEqual(self.startup_notices(), [text])
        self.assertTrue([event for event in self.records("diagnostic") if event["stage"] == "solve"
                         and event["text"] == text])
        self.assert_retried("solve", text, errors)
        self.assertFalse(self.records("status", "Author paused"))

    def test_every_role_on_an_openai_format_model_starts_and_its_author_retries(self):
        # The Codex author of an API model finds Codex as an API model's calls do (api_codex_executable).
        model = self.openai
        code, stderr, errors = self.run_author(model)
        self.assertEqual((code, stderr), ("still retrying", ""))  # Never exit code 1.
        text = runtime.missing_cli_text("Codex", model, "solve")
        self.assertEqual(text, (
            "The Codex CLI is not installed or is not on PATH. The proof author's model gpt-gw-1 (API connection "
            "“OpenAI gateway”) is OpenAI-format, and TCS Prover runs OpenAI-format models through the Codex CLI (no "
            "ChatGPT login needed). Install the Codex CLI; the job retries every minute and continues by itself."))
        self.assertEqual(self.startup_notices(), [text])
        self.assert_retried("solve", text, errors)
        self.assertFalse(self.records("status", "Author paused"))

    def test_a_built_in_models_author_retries_with_its_own_message(self):
        for model in ("claude-opus-5-5", "gpt-6.1-sol"):
            with self.subTest(model=model):
                code, stderr, errors = self.run_author(model)
                self.assertEqual((code, stderr), ("still retrying", ""))
                text = runtime.missing_cli_text(self.programs[model], model, "solve")
                self.assertIn(" with your ", text)  # The login the built-in model needs, unlike an API model.
                self.assertEqual(self.startup_notices(), [text])
                self.assert_retried("solve", text, errors)
                self.assertFalse(self.records("status", "Author paused"))

    def test_a_critic_call_on_an_anthropic_format_model_retries(self):
        code, stderr, errors = self.run_critic(self.anthropic)
        self.assertEqual((code, stderr), ("still retrying", ""))
        text = runtime.missing_cli_text("Claude", self.anthropic, "critic")
        self.assertEqual(text, (
            "Claude Code is not installed or is not on PATH. The critic's model claude-gw-1 (API connection "
            "“Anthropic gateway”) is Anthropic-format, and TCS Prover runs Anthropic-format models through Claude "
            "Code (no Claude login needed). Install Claude Code; the job retries every minute and continues by "
            "itself."))
        self.assert_retried("critic", text, errors)
        self.assertTrue(all(isinstance(error, runtime.CLIUnavailable) and error.cli_problem == "Claude"
                            for error in errors))

    def test_a_critic_call_on_a_built_in_model_retries_with_its_own_message(self):
        for model in ("claude-opus-5-5", "gpt-6.1-sol"):
            with self.subTest(model=model):
                code, stderr, errors = self.run_critic(model)
                self.assertEqual((code, stderr), ("still retrying", ""))
                cli_name = self.programs[model]
                self.assert_retried("critic", runtime.missing_cli_text(cli_name, model, "critic"), errors)
                self.assertTrue(all(isinstance(error, runtime.CLIUnavailable) and error.cli_problem == cli_name
                                    for error in errors))

    def test_a_summary_turn_of_the_paused_author_retries(self):
        code, stderr, errors = self.run_summary(self.anthropic)
        self.assertEqual((code, stderr), ("still retrying", ""))
        text = runtime.missing_cli_text("Claude", self.anthropic, "conversation")
        # A reply never moves the job on (it stays paused, or at its time limit): only the reply is retried.
        self.assertEqual(text, (
            "Claude Code is not installed or is not on PATH. The proof author's model claude-gw-1 (API connection "
            "“Anthropic gateway”) is Anthropic-format, and TCS Prover runs Anthropic-format models through Claude "
            "Code (no Claude login needed). Install Claude Code; the author's reply is retried every minute and "
            "arrives by itself."))
        self.assert_retried("conversation", text, errors)
        self.assertFalse(self.records("status", "Author reply failed"))

    def test_a_summary_turn_on_each_other_kind_of_model_retries_with_its_own_message(self):
        for kind, model in self.models.items():
            if model == self.anthropic:
                continue
            with self.subTest(kind=kind):
                code, stderr, errors = self.run_summary(model)
                self.assertEqual((code, stderr), ("still retrying", ""))
                text = runtime.missing_cli_text(self.programs[model], model, "conversation")
                self.assertTrue(text.endswith("; the author's reply is retried every minute and arrives by itself."),
                                text)
                self.assertNotIn("the job", text)
                self.assert_retried("conversation", text, errors)
                self.assertFalse(self.records("status", "Author reply failed"))

    def test_the_statement_reviewer_on_an_openai_format_model_retries(self):
        errors_stream = io.StringIO()
        with self.retrying() as errors, mock.patch.object(review.sys, "stdin", io.StringIO(f"{STATEMENT}\0")), \
                mock.patch.object(review.sys, "stderr", errors_stream), \
                mock.patch.object(runtime, "configure_standard_streams"):
            with self.assertRaises(StopRun):
                review.review_worker_main(["--review-model", self.openai])
        text = runtime.missing_cli_text("Codex", self.openai, "review")
        self.assertEqual(text, (
            "The Codex CLI is not installed or is not on PATH. The statement reviewer's model gpt-gw-1 (API connection "
            "“OpenAI gateway”) is OpenAI-format, and TCS Prover runs OpenAI-format models through the Codex CLI (no "
            "ChatGPT login needed). Install the Codex CLI; the job retries every minute and continues by itself."))
        self.assertEqual(self.startup_notices(), [text])
        self.assert_retried("review", text, errors)
        self.assertEqual(errors_stream.getvalue(), "")


class MissingProgramMessageTests(NoCliCase):
    """What a call says when its program is missing, and how the retry logic reads it."""

    def test_the_four_kinds_of_model_get_their_own_words(self):
        self.assertEqual(runtime.missing_cli_text("Claude", self.anthropic, "critic"), (
            "Claude Code is not installed or is not on PATH. The critic's model claude-gw-1 (API connection “Anthropic "
            "gateway”) is Anthropic-format, and TCS Prover runs Anthropic-format models through Claude Code (no Claude "
            "login needed). Install Claude Code; the job retries every minute and continues by itself."))
        self.assertEqual(runtime.missing_cli_text("Codex", self.openai, "critic"), (
            "The Codex CLI is not installed or is not on PATH. The critic's model gpt-gw-1 (API connection “OpenAI "
            "gateway”) is OpenAI-format, and TCS Prover runs OpenAI-format models through the Codex CLI (no ChatGPT "
            "login needed). Install the Codex CLI; the job retries every minute and continues by itself."))
        self.assertEqual(runtime.missing_cli_text("Codex", "gpt-6.1-sol", "solve"), (
            "The Codex CLI is not installed or is not on PATH. The proof author's model GPT 6.1 Sol runs through the "
            "Codex CLI with your ChatGPT login. Install it and run codex login; the job retries every minute and "
            "continues by itself."))
        self.assertEqual(runtime.missing_cli_text("Claude", "claude-opus-5-5", "review"), (
            "Claude Code is not installed or is not on PATH. The statement reviewer's model Claude Opus 5.5 runs "
            "through Claude Code with your Claude login. Install it and log in (run claude, then /login); the job "
            "retries every minute and continues by itself."))
        # A paused author's reply (a message, Summary, or the summary at the time limit) never moves its job on.
        self.assertEqual(runtime.missing_cli_text("Codex", "gpt-6.1-sol", "conversation"), (
            "The Codex CLI is not installed or is not on PATH. The proof author's model GPT 6.1 Sol runs through the "
            "Codex CLI with your ChatGPT login. Install it and run codex login; the author's reply is retried every "
            "minute and arrives by itself."))

    def test_every_role_and_kind_is_named_and_read_as_a_missing_program(self):
        names = {self.anthropic: ("claude-gw-1", "“Anthropic gateway”", "Anthropic-format"),
                 self.openai: ("gpt-gw-1", "“OpenAI gateway”", "OpenAI-format"),
                 "claude-opus-5-5": ("Claude Opus 5.5", "your Claude login"),
                 "gpt-6.1-sol": ("GPT 6.1 Sol", "your ChatGPT login")}
        for kind, model in self.models.items():
            cli_name = self.programs[model]
            program = runtime.CLI_PROGRAMS[cli_name]
            for stage, role in (*runtime._STAGE_ROLES.items(), (None, None)):
                with self.subTest(kind=kind, stage=stage):
                    error = runtime.missing_cli(cli_name, model, stage)
                    text = str(error)
                    self.assertEqual(text, runtime.missing_cli_text(cli_name, model, stage))
                    self.assertTrue(text.startswith(f"{program[0].upper()}{program[1:]} is not installed or is not "
                                                    "on PATH."), text)
                    self.assertIn(f"The {role}'s model " if role else "This call's model ", text)
                    for name in names[model]:
                        self.assertIn(name, text)
                    self.assertIn(f"through {program}", text)
                    self.assertTrue(text.endswith(
                        "; the author's reply is retried every minute and arrives by itself." if stage == "conversation"
                        else "; the job retries every minute and continues by itself."), text)
                    for key in (ANTHROPIC_KEY, OPENAI_KEY):
                        self.assertNotIn(key, text)
                    self.assertEqual(runtime._plain_failure(text), "the CLI is not available")
                    self.assertEqual(runtime._cli_problem(error), cli_name)
                    self.assertEqual(runtime._cli_problem(runtime.Error(text)), cli_name)  # The author's plain Error.
                    self.assertIsNone(runtime.settings_failure(error, model))
                    self.assertTrue(runtime.retryable_failure(error))
                    self.assertIsInstance(error, runtime.CLIUnavailable)

    def test_settings_that_cannot_be_read_give_the_short_form(self):
        unreadable = providers.StoreUnavailable("Cannot read the API model store (Permission denied).")
        with mock.patch.object(providers, "_entries", side_effect=unreadable):
            self.assertEqual(runtime.missing_cli_text("Claude", self.anthropic, "critic"), (
                "Claude Code is not installed or is not on PATH. Install Claude Code; the job retries every minute and "
                "continues by itself."))
        for model in ("api:gone", None, 7):
            with self.subTest(model=model):
                self.assertEqual(runtime.missing_cli_text("Codex", model, "solve"), (
                    "The Codex CLI is not installed or is not on PATH. Install the Codex CLI; the job retries every "
                    "minute and continues by itself."))
                self.assertEqual(runtime.missing_cli_text("Codex", model, "conversation"), (
                    "The Codex CLI is not installed or is not on PATH. Install the Codex CLI; the author's reply is "
                    "retried every minute and arrives by itself."))

    def test_model_and_connection_names_never_change_how_the_message_is_read(self):
        # A connection named like a provider failure: still a missing program, retried in the same conversation.
        tricky = providers.save_api_connection(
            {"format": "anthropic", "baseUrl": "https://tricky.invalid", "apiKey": ANTHROPIC_KEY,
             "label": "Codex-compatible 1M context window (model_not_found 400 invalid_request)"},
            [{"id": "claude-codex-mirror", "label": "x"}])["models"][0]["modelId"]
        error = runtime.missing_cli("Claude", tricky, "solve")
        self.assertIsNone(runtime.settings_failure(error, tricky))
        self.assertFalse(runtime._context_window_failure(error))
        self.assertEqual(runtime._plain_failure(str(error)), "the CLI is not available")
        self.assertEqual(runtime._cli_problem(runtime.Error(str(error))), "Claude")
        # A reply turn wraps it (ConversationAttemptFailed): still no settings problem, though its words alone
        # would read as one.
        preview = providers.save_api_connection(
            {"format": "openai", "baseUrl": "https://preview.invalid/v1", "apiKey": OPENAI_KEY,
             "label": "Preview (not available for your account yet)"}, [{"id": "gpt-preview", "label": "x"}])
        preview = preview["models"][0]["modelId"]
        error = runtime.missing_cli("Codex", preview, "conversation")
        self.assertIsInstance(runtime.settings_failure(runtime.Error(str(error)), preview), runtime.ModelUnavailable)
        self.assertIsNone(runtime.settings_failure(error, preview))
        try:
            raise runtime.ConversationAttemptFailed(str(error), retry={}) from error
        except runtime.ConversationAttemptFailed as wrapped:
            self.assertIsNone(runtime.settings_failure(wrapped, preview))
            self.assertEqual(runtime._cli_problem(wrapped), "Codex")
        # The older texts keep naming their own program, also an earlier version's "Claude CLI".
        for text, name in (("Codex CLI is not installed or is not on PATH.", "Codex"),
                           ("Claude Code is not installed or is not on PATH.", "Claude"),
                           ("Claude CLI is not installed or is not on PATH.", "Claude"),
                           ("Codex review failed: Claude Code is not installed or is not on PATH.", "Claude")):
            self.assertEqual(runtime._cli_problem(runtime.Error(text)), name, text)

    def test_a_claude_model_on_an_openai_format_connection_names_the_codex_cli(self):
        # An OpenAI-compatible gateway may serve a model called claude-…: the program is the one named before
        # "is not installed", never the other one that the model or its connection is named after.
        model = providers.save_api_connection(
            {"format": "openai", "baseUrl": "https://litellm.invalid/v1", "apiKey": OPENAI_KEY,
             "label": "Claude gateway (LiteLLM)"},
            [{"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"}])["models"][0]["modelId"]
        text = runtime.missing_cli_text("Codex", model, "solve")
        self.assertTrue(text.startswith("The Codex CLI is not installed or is not on PATH. The proof author's model "
                                        "claude-sonnet-4-5 (API connection “Claude gateway (LiteLLM)”)"), text)
        self.assertEqual(runtime._cli_problem(runtime.missing_cli("Codex", model, "solve")), "Codex")
        self.assertEqual(runtime._cli_problem(runtime.Error(text)), "Codex")  # The author's plain Error.
        # The same failure three times in a row names the Codex CLI, and so does Needs your attention.
        self.capture()
        retry_test.patch_clock(self, retry_test.FakeClock())
        options = {}
        for attempt in (2, 3, 4):
            runtime.wait_before_retry(options, "solve", attempt, runtime.Error(text), node="author")
        notices = self.notices("solve")
        self.assertEqual([notice["failure"] for notice in notices], [
            "the CLI is not available", "the CLI is not available", "Codex CLI missing or incompatible"])
        app = server.App(runs=self.runs, load_latest=False)
        attention = app.attention({**app.state, "phase": "running", "stage": "solve", "authorModel": model,
                                   "modelRetry": {"stage": "solve", "retryAt": 2e9, "failure": notices[-1]["failure"],
                                                  "text": notices[-1]["text"]}})
        self.assertEqual((attention["kind"], attention["title"]), ("cli", "Codex CLI not installed or not working"))

    def test_the_lookups_fail_with_the_message_and_keep_their_own_signatures(self):
        with self.assertRaises(runtime.CLIUnavailable) as raised:
            runtime.located_cli(runtime.codex, "Codex", "gpt-6.1-sol", "final")
        self.assertEqual(str(raised.exception), runtime.missing_cli_text("Codex", "gpt-6.1-sol", "final"))
        self.assertEqual(raised.exception.cli_problem, "Codex")
        with self.assertRaises(runtime.CLIUnavailable) as raised:
            claude_cli.located(runtime, self.anthropic, "review")
        self.assertEqual(str(raised.exception), runtime.missing_cli_text("Claude", self.anthropic, "review"))
        # A runtime without missing_cli (a test double) gets the lookup's own error.
        double = type("Runtime", (), {"Error": runtime.Error, "CLIUnavailable": runtime.CLIUnavailable})
        with self.assertRaisesRegex(runtime.CLIUnavailable, r"^Claude Code is not installed or is not on PATH\.$"):
            claude_cli.located(double, self.anthropic, "review")
        self.assertEqual(runtime.located_cli(lambda: "/opt/codex", "Codex", "gpt-6.1-sol"), "/opt/codex")
        # The lookups themselves are unchanged (tests replace them with stand-ins).
        with self.assertRaisesRegex(runtime.CLIUnavailable, r"^Codex CLI is not installed or is not on PATH\.$"):
            runtime.codex()
        with self.assertRaisesRegex(runtime.CLIUnavailable, r"^Claude Code is not installed or is not on PATH\.$"):
            claude_cli.executable(runtime)
        with self.assertRaisesRegex(runtime.CLIUnavailable, r"^Codex CLI is not installed or is not on PATH\.$"):
            runtime.api_codex_executable({"CODEX_HOME": str(self.root)})


class RetryNoticeTests(NoCliCase):
    """The retry notice quotes the CLI's message whole and calls it so."""

    def setUp(self):
        super().setUp()
        self.capture()
        retry_test.patch_clock(self, retry_test.FakeClock())

    def test_a_structured_call_says_cli_message_from_its_first_retry(self):
        missing = [runtime.missing_cli("Codex", self.openai, "final") for _ in range(3)]
        failures = [*missing, runtime.Error("Codex final failed: Selected model is at capacity."), retry_test.VALID]
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=failures):
            result, _ = runtime.structured("Review S.", retry_test.SCHEMA, "final", model=self.openai, options={})
        self.assertEqual(result["notes"], "ok")
        text = runtime.missing_cli_text("Codex", self.openai, "final")
        notices = self.notices()
        self.assertEqual([notice["text"] for notice in notices], [
            f"Model call failed (the CLI is not available) — retrying in 1 min (attempt 2). CLI message: {text}",
            f"Model call failed (the CLI is not available) — retrying in 1 min (attempt 3, retrying for 1 min). "
            f"CLI message: {text}",
            # The same failure three times in a row: the Overview lists the job (see AttentionTests).
            f"Codex CLI missing or incompatible — retrying in 1 min (attempt 4, retrying for 2 min). CLI message: {text}",
            "Model call failed (model at capacity) — retrying in 1 min (attempt 5, retrying for 3 min). Provider "
            "message: Selected model is at capacity."])
        self.assertEqual([notice["failureKind"] for notice in notices], ["model_call", "model_call", "cli", "model_call"])

    def test_the_author_quotes_a_message_longer_than_300_characters_whole(self):
        launch = functools.partial(REAL_CLAUDE_GOAL_SESSION, runtime, "Prove S exactly.",
                                   prompts=retry_test.CodexAuthorTests.PROMPTS,
                                   settings={"model": self.anthropic, "effort": "high", "speed": "standard",
                                             "summary": "concise"}, features=[], stages=None, node_name="author")
        options = {"goal_cwd": str(self.root)}
        with self.retrying(waits=3) as errors, self.assertRaises(StopRun):
            runtime._goal_step({"author": launch(options=options, initial_instruction="")}, "author", launch, "",
                               options, next, "solve")
        text = runtime.missing_cli_text("Claude", self.anthropic, "solve")
        self.assertGreater(len(text), 300)
        self.assertEqual([type(error) for error in errors], [runtime.Error] * 4)  # The author's plain errors.
        notices = self.notices("solve")
        self.assertEqual(len(notices), 3)
        for notice in notices:
            self.assertTrue(notice["text"].endswith(f" CLI message: {text}"), notice["text"])
            self.assertEqual(notice["failureReason"], text)
        self.assertEqual([notice["failure"] for notice in notices],
                         ["the CLI is not available", "the CLI is not available", "Claude Code missing or incompatible"])

    def test_a_cli_message_of_several_lines_is_quoted_by_its_error_line(self):
        # Only the runner's own one-line text about a missing CLI is quoted whole (docs/retries.md, Diagnostics).
        error = runtime.Error("Codex critic failed: Codex exited with code 127.\n"
                              "/bin/sh: line 1: node: command not found\n"
                              "hint: the codex launcher needs Node.js 22 or later")
        error.cli_problem = "Codex"
        runtime.wait_before_retry({}, "critic", 2, error)
        notice, = self.notices()
        self.assertEqual(notice["text"], "Model call failed (the CLI stopped unexpectedly) — retrying in 1 min "
                                         "(attempt 2). CLI message: /bin/sh: line 1: node: command not found")


class InstallingLaterTests(retry_test.RetryCase):
    """Once the program is installed, the next attempt runs, in the same conversation."""

    # The scripted app-server's helpers (test_model_retry.CodexAuthorTests), without its tests.
    PROMPTS = retry_test.CodexAuthorTests.PROMPTS
    launcher = retry_test.CodexAuthorTests.launcher
    run_goal = retry_test.CodexAuthorTests.run_goal
    names = retry_test.CodexAuthorTests.names
    sent = retry_test.CodexAuthorTests.sent

    def test_a_structured_call_answers_once_its_program_is_there(self):
        missing = [runtime.missing_cli("Claude", "claude-opus-5-5", "critic") for _ in range(2)]
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=[*missing, retry_test.VALID]) as attempt:
            result, raw = runtime.structured("Review S.", retry_test.SCHEMA, "critic", model="claude-opus-5-5")
        self.assertEqual((result, raw), ({"statement": "S", "notes": "ok"}, retry_test.VALID))
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(self.waits(), 2 * 60)
        self.assertEqual([event["failure"] for event in self.notices()], ["the CLI is not available"] * 2)

    def test_an_author_resumes_the_same_thread_after_an_attempt_without_its_program(self):
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.servers, self.scripts, self.threads, self.failures = [], [], {}, {}
        self.turn_count, self.codex_home, self.replacement, self.safety_reads = 0, None, None, []
        lookups = []

        def codex():
            lookups.append(len(lookups) + 1)
            if len(lookups) == 2:  # Removed (or updating itself) at the second attempt; installed again after it.
                raise runtime.CLIUnavailable("Codex CLI is not installed or is not on PATH.")
            return "codex-test"

        for name, value in (("RPC", functools.partial(retry_test.AppServer, self)), ("codex", codex)):
            patcher = mock.patch.object(runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=mock.Mock(
            poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0, kill=lambda: None))
        popen.start()
        self.addCleanup(popen.stop)
        environment = mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live",
                                                    "CODEX_HOME": str(self.root / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        self.scripts = [{"work": "Lemma 1 holds.", "error": retry_test.FAILURES[5]}, {"answer": "Full proof"}]
        result = self.run_goal()
        self.assertEqual(result, {"outcome": "done", "output": "Full proof"})
        self.assertEqual(lookups, [1, 2, 3])
        self.assertEqual(len(self.servers), 2)  # The attempt without its program started no app-server.
        third = self.servers[1]
        self.assertIn("thread/resume", self.names(third))
        self.assertNotIn("thread/start", self.names(third))
        self.assertEqual([params["threadId"] for name, params in third.calls if name == "thread/resume"], ["thread-1"])
        self.assertEqual(self.sent(third), [runtime.AUTHOR_RETRY_NOTE])
        self.assertEqual(list(self.threads), ["thread-1"])
        notices = self.notices()
        self.assertEqual([notice["failure"] for notice in notices], ["connection lost", "the CLI is not available"])
        self.assertTrue(notices[1]["text"].endswith(
            " CLI message: " + runtime.missing_cli_text("Codex", "gpt-6.1-sol", "solve")), notices[1]["text"])


class StartupNoticeTests(NoCliCase):
    """The start-up notice names only the programs of the selected stages, found as the calls find them."""

    def setUp(self):
        super().setUp()
        self.capture()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        supported = mock.patch.object(runtime.confinement, "supported", return_value=True)
        supported.start()
        self.addCleanup(supported.stop)

    def test_writing_off_with_anthropic_format_models_names_claude_code_only(self):
        options = {"model": runtime.MODEL, "author_model": self.anthropic, "critic_model": self.anthropic,
                   "workflow_stages": {"review": False, "author": True, "critic": True, "writing": False}}
        models = runtime._workflow_models([WORKFLOWS / "author_critic.yaml", WORKFLOWS / "clean_up.yaml"], options)
        # The LaTeX editor's nodes (Writing is off) would fall back to the default Codex model.
        self.assertEqual(models, [("solve", self.anthropic), ("critic", self.anthropic), ("critic", self.anthropic)])
        with mock.patch.object(runtime.subprocess, "run", side_effect=AssertionError("ran a CLI")):
            runtime.check_clis(models, directory=self.run_dir)
        self.assertEqual([(event["kind"], event["text"]) for event in self.events],
                         [("diagnostic", runtime.missing_cli_text("Claude", self.anthropic, "solve"))])
        self.assertNotIn("Codex", json.dumps(self.events))

    def test_codex_found_only_at_its_standalone_install_is_checked_and_named(self):
        standalone = str(self.root / "home/.codex/packages/standalone/current/bin/codex")
        answer = subprocess.CompletedProcess([standalone, "--version"], 0, stdout="codex-cli 0.0.0-test\n")
        with mock.patch.object(runtime, "_codex_candidates", return_value=[standalone]), \
                mock.patch.object(runtime.subprocess, "run", return_value=answer) as run:
            runtime.check_clis([("critic", self.openai)], directory=self.run_dir)
        self.assertEqual(run.call_args.args[0], [standalone, "--version"])
        status, = self.records("status", "CLI check")
        self.assertEqual(status["cliVersions"], {"Codex": {"path": standalone, "version": "codex-cli 0.0.0-test"}})
        self.assertEqual(self.records("diagnostic"), [])
        # A built-in Codex model's calls look on PATH only: there it is missing, and the notice says so.
        self.events.clear()
        with mock.patch.object(runtime, "_codex_candidates", return_value=[standalone]), \
                mock.patch.object(runtime.subprocess, "run", side_effect=AssertionError("ran a CLI")):
            runtime.check_clis([("critic", self.openai), ("solve", "gpt-6.1-sol")], directory=self.run_dir)
        self.assertEqual([event["text"] for event in self.events],
                         [runtime.missing_cli_text("Codex", "gpt-6.1-sol", "solve")])

    def test_a_cli_that_fails_its_version_check_keeps_its_words(self):
        answer = subprocess.CompletedProcess(["claude", "--version"], 127, stdout="")
        with mock.patch.object(claude_cli, "executable", return_value="/opt/claude"), \
                mock.patch.object(runtime.subprocess, "run", return_value=answer):
            runtime.check_clis([("review", "claude-opus-5-5")], directory=self.run_dir)
        self.assertEqual([event["text"] for event in self.records("diagnostic")], [
            "Claude Code missing or incompatible: `claude --version` exited with code 127. Model calls retry every "
            "minute until it works."])


class AttentionTests(ServerCase):
    """A job whose program stays missing is listed under Needs your attention while it retries."""

    def test_only_a_named_missing_program_needs_attention_while_the_job_runs(self):
        app = server.App(runs=self.runs, load_latest=False)
        retry = {"stage": "critic", "retryAt": 2e9, "text": "Model call failed (the CLI is not available) — retrying."}
        state = {**app.state, "phase": "running", "stage": "critic", "criticModel": self.anthropic}
        self.assertIsNone(app.attention({**state, "modelRetry": {**retry, "failure": "the CLI is not available"}}))
        for failure, phase, title in (("Claude Code missing or incompatible", "running",
                                       "Claude Code not installed or not working"),
                                      ("Codex CLI missing or incompatible", "reviewing",
                                       "Codex CLI not installed or not working"),
                                      # An earlier version's words, from a saved job or a runner started before
                                      # an update.
                                      ("Claude CLI missing or incompatible", "running",
                                       "Claude Code not installed or not working")):
            with self.subTest(failure=failure):
                text = f"{failure} — retrying in 1 min (attempt 4). CLI message: …"
                self.assertEqual(app.attention({**state, "phase": phase,
                                                "modelRetry": {**retry, "failure": failure, "text": text}}),
                                 {"kind": "cli", "title": title, "retrying": True, "text": text})
        self.assertIsNone(app.attention({**state, "phase": "paused", "error": "Model call failed.", "modelRetry": {
            **retry, "failure": "Claude Code missing or incompatible"}}))
        # Only these words name a program: not another program's, and not a failure that merely ends alike.
        for failure in ("Claude Code CLI missing or incompatible", "Codex Code missing or incompatible",
                        "Gemini CLI missing or incompatible", "Claude Code missing or incompatible: x"):
            with self.subTest(failure=failure):
                self.assertIsNone(app.attention({**state, "modelRetry": {**retry, "failure": failure}}))

    def test_a_paused_authors_reply_that_waits_for_its_program_needs_attention(self):
        app = server.App(runs=self.runs, load_latest=False)
        state = {**app.state, "phase": "paused", "stage": "solve", "authorModel": self.openai,
                 "error": "Paused by request."}
        item = {"id": CONVERSATION_ID, "kind": "message", "status": "running", "retry": {
            "text": "Model call failed (the CLI is not available) — retrying in 1 min (attempt 3).", "retryAt": 2e9,
            "failure": "the CLI is not available"}}
        app._conversation = [item]
        self.assertIsNone(app.attention(state))  # Not yet three identical failures (a CLI updating itself).
        text = "Codex CLI missing or incompatible — retrying in 1 min (attempt 4). CLI message: …"
        item["retry"] = {"text": text, "retryAt": 2e9, "failure": "Codex CLI missing or incompatible"}
        self.assertEqual(app.attention(state), {"kind": "cli", "title": "Codex CLI not installed or not working",
                                                "retrying": True, "text": text})
        # Only while the reply is being written, and only for a paused job.
        for status in ("queued", "answered", "failed", "stopped"):
            with self.subTest(status=status):
                item["status"] = status
                self.assertIsNone(app.attention(state))
        item["status"] = "running"
        self.assertIsNone(app.attention({**state, "phase": "done"}))

    def test_a_time_limit_summary_that_waits_for_its_program_lists_the_job_on_the_overview(self):
        # The summary the server asks for by itself at the time limit: nobody is on the job page to see its line.
        app = self.start_job(self.anthropic)
        with app.lock:
            app.state["goalThreadId"] = THREAD
        self.settle(app, [{"kind": "workflow_paused", "stage": "solve", "node": "author",
                           "state": {"statement": STATEMENT, "paused": True},
                           "reason": f"{server.WORKFLOW_TIME_LIMIT_ERROR}; saved work remains resumable."}])
        self.tick(now=time.time() + 1)
        item = app._conversation[-1]
        self.assertEqual((item["kind"], item["origin"], item["status"]), ("summary", "time-limit", "running"))
        # What its runner prints while Claude Code stays missing (wait_before_retry, three times).
        self.capture()
        retry_test.patch_clock(self, retry_test.FakeClock())
        options, text = {}, runtime.missing_cli_text("Claude", self.anthropic, "conversation")
        for attempt in (2, 3, 4):
            runtime.wait_before_retry(options, "conversation", attempt, runtime.ConversationAttemptFailed(
                text, retry={}), node="author", record={"conversationId": item["id"]})
        records = [event for event in self.events if event["kind"] == "status"]
        last = max(index for index, record in enumerate(records) if record.get("failureKind") == "cli")
        with app.lock:
            token = app.active_token
        process = app.process
        process.stdout = Lines(json.dumps(record) + "\n" for record in records[:last + 1])
        with self.assertRaises(StopRun):  # The reply's runner still runs: its reader is stopped mid-run.
            app._read_conversation_output(process, token, item["id"])
        status, snapshot = self.call(f"/state?job={app.state['runId']}")
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot["phase"], "paused")
        notice = f"Claude Code missing or incompatible — retrying in 1 min (attempt 4). CLI message: {text}"
        self.assertEqual(snapshot["needsAttention"], {"kind": "cli", "title": "Claude Code not installed or not working",
                                                      "retrying": True, "text": notice})
        self.assertEqual(snapshot["conversation"]["items"][-1]["retry"]["text"], notice)  # The reply's own line.
        status, batches = self.call("/batches")
        root = next(card for card in batches["batches"] if card["id"] == "__root__")
        self.assertEqual([(item["runId"], item["kind"]) for item in root["attention"]], [(app.state["runId"], "cli")])
        # Once the program is there and the summary is written, nothing waits any more.
        process.stdout = io.StringIO(json.dumps({
            "kind": "conversation_reply", "stage": "conversation", "label": "Progress summary",
            "conversationKind": "summary", "text": "Lemma 1 is proved.", "threadId": THREAD, "compacted": False}) + "\n")
        process.returncode = 0
        app._read_conversation_output(process, token, item["id"])
        self.assertEqual(item["status"], "answered")
        status, snapshot = self.call(f"/state?job={app.state['runId']}")
        self.assertIsNone(snapshot["needsAttention"])

    def test_three_identical_failures_list_the_job_on_the_overview(self):
        app = self.start_job(self.anthropic)
        # What the runner prints while the author's program stays missing (wait_before_retry, three times).
        self.capture()
        retry_test.patch_clock(self, retry_test.FakeClock())
        options = {}
        for attempt in (2, 3, 4):
            runtime.wait_before_retry(options, "solve", attempt, runtime.Error(
                runtime.missing_cli_text("Claude", self.anthropic, "solve")), node="author")
        records = [event for event in self.events if event["kind"] == "status"]
        last = max(index for index, record in enumerate(records) if record.get("failureKind") == "cli")
        records = records[:last + 1]  # The runner waits to retry the fourth attempt.
        with app.lock:
            token = app.active_token
        process = app.process
        process.stdout = Lines(json.dumps(record) + "\n" for record in records)
        with self.assertRaises(StopRun):  # The runner still runs: its reader is stopped mid-run.
            app._read_output(process, token)
        status, snapshot = self.call(f"/state?job={app.state['runId']}")
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot["phase"], "running")
        attention = snapshot["needsAttention"]
        self.assertEqual((attention["kind"], attention["title"], attention["retrying"]),
                         ("cli", "Claude Code not installed or not working", True))
        self.assertTrue(attention["text"].startswith("Claude Code missing or incompatible — retrying in 1 min "
                                                     "(attempt 4). CLI message: Claude Code is not installed"))
        status, batches = self.call("/batches")
        root = next(card for card in batches["batches"] if card["id"] == "__root__")
        self.assertEqual([(item["runId"], item["kind"]) for item in root["attention"]], [(app.state["runId"], "cli")])


class WebCutoffStartTests(ServerCase):
    """The one start that still needs an installed program: a web-cutoff run checks the native Codex binary."""

    @unittest.skipUnless(sys.platform == "darwin", "A web cutoff timestamp needs macOS")
    def test_a_new_web_cutoff_job_is_refused_without_the_native_codex_binary(self):
        body = self.body(self.models["built-in Codex"], retrievalMode="historical",
                         retrievalCutoffUtc="2025-01-01T00:00:00Z")
        # No native Codex binary anywhere (on PATH, at the standalone install or in Codex.app).
        with mock.patch.object(runtime.retrieval_runtime, "_native_macos_binary", return_value=False):
            status, reply = self.call("/start-workflow", body)
        self.assertEqual((status, reply["error"]), (400, (
            "Historical execution requires an absolute native Mach-O codex binary outside the run workspace; script "
            "shims are not supported.")))
        self.assertEqual((self.launches, self.workers), ([], []))


class ReadmeTests(unittest.TestCase):
    """The README and its guides say what a missing program stops, and no longer what the change made untrue."""

    def test_the_readme_names_the_web_cutoff_exception_and_drops_the_old_claims(self):
        def read(path):
            return " ".join(path.read_text(encoding="utf-8").split())

        # The README's former sections: model providers in docs/models.md, retries in docs/retries.md.
        readme, models = read(ROOT / "README.md"), read(ROOT / "docs" / "models.md")
        retries = read(ROOT / "docs" / "retries.md")
        text = " ".join(read(path) for path in (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))))
        # Only a web cutoff timestamp (a historical policy) needs the native Codex binary and Codex CLI models,
        # and with Writing & polishing MacTeX for its isolated compiler; a cutoff of -1 does not.
        self.assertIn("A missing program never stops a start (a run with a web cutoff timestamp is the exception: its "
                      "checks before launch need the native Codex binary, and with Writing & polishing on MacTeX; a "
                      "job that fails them is held until you install what is missing and choose **Re-run "
                      "preflight**).", models)
        self.assertIn("API model, has its saved key; a run with a web cutoff timestamp also needs Codex CLI models for "
                      "its proof roles and the native Codex binary, and with **Writing & polishing** on also MacTeX "
                      "in `/usr/local/texlive` and macOS's `sandbox-exec` for its isolated LaTeX compiler", models)
        self.assertIn("Nothing here stops a start: a job whose program or LaTeX is missing says so, tries again every "
                      "minute, and continues by itself once you install it. Runs with a web cutoff timestamp are the "
                      "exception: they need the native Codex CLI, and with **Writing & polishing** on also MacTeX (in "
                      "`/usr/local/texlive`), before they start.", readme)
        self.assertIn("The message of a CLI that could not start is labelled *CLI message*, as no provider was reached, "
                      "and the runner's own message about a CLI that is not installed is quoted whole.", retries)
        self.assertIn("(a CLI that is not installed is no such setting: the resume goes ahead, and the model call that "
                      "needs it says so and is retried every minute)", retries)
        for claim in ("installed programs are never checked there", "a CLI that is only missing for a moment",
                      "a CLI's own message is quoted whole"):
            self.assertNotIn(claim, text)


@unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
class PageTests(unittest.TestCase):
    """The page names each API model's program, and lists a job whose program is missing."""

    def test_each_api_model_chip_names_its_program_without_checking_it(self):
        rows = [{"id": "api:claude-gw", "label": "Claude GW", "format": "anthropic", "host": "anthropic-gw.invalid"},
                {"id": "api:gpt-gw", "label": "GPT GW", "format": "openai", "host": "openai-gw.invalid"}]
        provider_ui.ProviderBrowserTests.run_app(
            self, provider_ui.ProviderBrowserTests.PROVIDERS, provider_ui.ProviderBrowserTests.providers_setup(None, rows),
            r"""
for (const [model, text] of [
  ['api:claude-gw', 'API · Anthropic format via Claude Code · anthropic-gw.invalid'],
  ['api:gpt-gw', 'API · OpenAI format via Codex CLI · openai-gw.invalid'],
  ['claude-opus-5-5', 'Claude Code'], ['gpt-6.1-sol', 'Codex CLI'],
]) {
  for (const role of ['review', 'author', 'critic', 'writer']) {
    const select = ui[role + 'Model'];
    if (!select.options.some((option) => option.value === model)) select.add(new Option(model, model));
    select.value = model;
  }
  renderModelChoices();
  for (const role of ['review', 'author', 'critic', 'writer']) {
    assert.equal(chip(role).textContent, text, `${role} ${model}`);
    assert.equal(chip(role).dataset.state, '', `${role} ${model}`);  // Never a warning: nothing is checked.
  }
}
""")

    def test_the_overview_and_the_job_card_list_a_missing_program(self):
        item = {"kind": "cli", "title": "Claude Code not installed or not working", "retrying": True,
                "text": "Claude Code missing or incompatible — retrying in 1 min (attempt 4). CLI message: Claude Code "
                        "is not installed or is not on PATH."}
        # A saved job keeps the words an earlier version wrote, which the page shows as they are.
        earlier = {**item, "text": "Claude CLI missing or incompatible — retrying in 1 min (attempt 4). CLI message: "
                                   "Claude CLI is not installed or is not on PATH."}
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const item = JSON.parse(process.argv[2]);
const code = source.slice(source.indexOf('function request('), source.indexOf('function selectedProblemMode('))
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function localDateTime('));
const setup = `
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.hidden = false; this.textContent = '';
    this.classList = {toggle() {}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this[key] = value; }
  remove() {}
}
const ui = new Proxy({}, {get: (target, key) => target[key] ||= new Element()});
let currentJob = '', currentBatch = '', composingProblem = false, batchCatalog = [], state = {phase: 'input'};
let requests = Promise.resolve(), previousPhase = '', timer, clock, jobsTimer;
const sessionToken = 't', timelineRows = new Map(), detailRows = new Map();
const location = {pathname: '/'}, history = {urls: [], pushState(a, b, url) { this.urls.push(url); }};
const clearTimeout = () => {}, clearInterval = () => {}, setTimeout = () => 1;
const document = {createElement: (tag) => new Element(tag)};
const window = {open() {}};
const localDateTime = (value) => value;
const confirm = () => true, alert = () => {};
const refresh = async () => {}, render = () => {};
const fetch = async () => ({ok: true, json: async () => ({})});
`;
vm.runInNewContext(setup + code + `
renderJobs([{runId: 'a', phase: 'running', title: 'P1', stageLabel: 'Proof author', error: '', retryAt: 2e9,
             needsAttention: item, checkpoints: [], canContinueStopped: false}]);
const [card] = ui.jobsList.children;
const texts = card.children[0].children.map((child) => child.textContent);
assert.equal(card.children[0].children[1].textContent, 'Running · Proof author · ' + item.title);
assert(texts.includes(item.title + ': ' + item.text), texts.join(' | '));
const line = card.children[0].children.find((child) => child.textContent === item.title + ': ' + item.text);
assert.equal(line.className, 'job-attention cli');
renderBatches([{id: '__root__', label: 'Runs', current: true, jobCount: 1, counts: {running: 1},
                attention: [{...item, runId: 'a', job: 'P1.md'}]}]);
assert.equal(ui.attentionBanner.hidden, false);
const [row] = ui.attentionList.children;
assert.equal(row.className, 'attention-item cli');
assert.deepEqual(row.children.map((child) => child.textContent), [item.title, 'P1.md', item.text]);
`, {assert, URLSearchParams, item});
"""
        for shown in (item, earlier):
            with self.subTest(text=shown["text"]):
                result = subprocess.run([NODE, "-e", script, str(provider_ui.APP), json.dumps(shown)],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_connection_form_and_the_notification_help_say_what_to_install(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const formats = source.slice(source.indexOf('const apiFormats = {'), source.indexOf('const roleNames = {'));
const alerts = source.slice(source.indexOf('function attentionAlertsOn('),
                            source.indexOf('// One notification per job and reason'));
console.log(JSON.stringify(vm.runInNewContext(formats + alerts + `
const Notification = {permission: 'default'}, localStorage = {getItem: () => null};
const jobsUi = {alerts: {}, alertsHelp: {}};
syncAttentionAlerts();
({openai: apiFormats.openai.help, anthropic: apiFormats.anthropic.help, alerts: jobsUi.alertsHelp.textContent})`)));
"""
        result = subprocess.run([NODE, "-e", script, str(provider_ui.APP)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        texts = json.loads(result.stdout)
        openai = ("Runs through the Codex CLI with the Responses API. The Codex CLI must be installed on this "
                  "computer, but needs no login. Models load automatically when you save.")
        self.assertEqual(texts["openai"], openai)
        self.assertEqual(texts["anthropic"], (
            "Runs through Claude Code in a private configuration folder, with subagents and web tools; a claude "
            "launcher that drops CLAUDE_CONFIG_DIR limits it to bare mode. Claude Code must be installed on this "
            "computer, but needs no login. Models load automatically when you save."))
        self.assertEqual(texts["alerts"], (
            "While TCS Prover is open in a tab, a notification says when a job first needs you: a CLI login, a usage "
            "limit, a rejected API key, a program to install, or a model or setting to fix. The Overview lists these "
            "jobs either way."))
        # The form's first text, before any format is chosen, is the OpenAI one.
        self.assertEqual(" ".join(provider_ui.page().texts["apiModelFormatHelp"].split()), openai)


if __name__ == "__main__":
    unittest.main()
