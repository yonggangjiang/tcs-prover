"""The owner's decisions for jobs, models, settings and the UI around them.

I-016, I-027, I-038, I-040, I-047 (API model fields), I-062, I-074, I-075, I-080, I-087, I-088, I-089,
I-090, I-106, I-107, I-118, I-120 and I-131. No test starts a model or a CLI: launches are replaced by
doubles, and HOME, CODEX_HOME, CLAUDE_CONFIG_DIR and the API model store point at a temporary folder.
"""

import contextlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import historical_codex_discovery as discovery
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, providers
from ui import batches, cli, review, server
import workflow_runner as runtime

import test_live_gateway as gateway_tests
import test_model_retry as retry_tests
import test_provider_ui as provider_ui_tests
import test_search_cut_runtime as cut_tests

KEY = "sk-decisions-0123456789abcdef"
WAIT = server.AUTO_RESUME_SECONDS
APP = server.UI / "app.js"
NODE = shutil.which("node")


class Launched(BaseException):
    """A launch reached Popen: the test has what it needs, and no process starts."""


class Isolated(unittest.TestCase):
    """A private API model store, CLI homes and a runs folder per test."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.store = self.root / "state" / "api-models.json"
        changes = [
            mock.patch.dict(os.environ, {"HOME": str(self.root / "home"), "CODEX_HOME": str(self.root / "home/.codex"),
                                         "CLAUDE_CONFIG_DIR": str(self.root / "home/.claude"),
                                         providers.STORE_ENV: str(self.store)}),
            mock.patch.object(server, "grant_windows_access"),
        ]
        for change in changes:
            change.start()
            self.addCleanup(change.stop)
        self.addCleanup(providers._LAST_GOOD.pop, str(self.store), None)

    def api_model(self, **values):
        return providers.save_api_model({"format": "openai", "baseUrl": "https://gateway.example.com/v1",
                                         "model": "gpt-gateway", "apiKey": KEY, "label": "Gateway", **values})

    def paused_job(self, statement="Exact statement", reason="Connection lost", now=1_000, **values):
        """An author paused by a model-call error, the way the runner reports one."""

        app = server.App(runs=self.runs, load_latest=False)
        app._new_run(statement)
        app._save_controlled("checked-statement.md", f"# Checked statement\n\n{statement}\n")
        app.state.update(phase="running", stage="solve", activeNode="author", runId=app.run_dir.name,
                         goalThreadId="saved-thread", elapsedSeconds=12, **values)
        app._save_job_settings(app.state)
        for role in ("author", "critic", "final"):
            app._save_prompt(role, app.state[f"{role}Prompt"])
        token = app.active_token = app.worker_token = object()
        process = app.process = mock.Mock(stdout=io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "solve", "node": "author",
            "state": {"statement": statement, "paused": True}, "reason": reason}) + "\n"))
        process.wait.return_value = process.poll.return_value = 1
        with mock.patch.object(server.time, "time", return_value=now):
            app._read_output(process, token)
        return app

    def queued_job(self, statement, queued_at):
        app = server.App(runs=self.runs, load_latest=False)
        app.start_direct_statement(statement, _prepare_only=True)
        app.state["queuedAt"] = queued_at
        return app

    def server(self, serve=False):
        """A real Server on port 0; with ``serve`` it answers HTTP, without its retry thread."""

        manager = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(manager.server_close)
        if serve:
            worker = threading.Thread(target=ThreadingHTTPServer.serve_forever, args=(manager, 0.01), daemon=True)
            worker.start()

            def stop():
                manager.shutdown()
                worker.join(timeout=2)
            self.addCleanup(stop)
        return manager

    @staticmethod
    def call(manager, path, body=None, raw=None):
        headers = {"Origin": manager.origin, "X-TCS-Prover-Token": manager.token}
        if body is not None:
            raw = json.dumps(body).encode()
        if raw is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(server.HOST, manager.server_port, timeout=30)
        try:
            connection.request("POST" if raw is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    @staticmethod
    def diagnostics(app):
        return [entry.get("text", "") for entry in app.state["trace"] if entry.get("kind") == "diagnostic"]


def fake_launches(case, failing=()):
    """Replace the author's launch: a statement in ``failing`` cannot start (once each)."""

    left, started = list(failing), []

    def launch(self, statement):
        if statement in left:
            left.remove(statement)
            raise OSError(11, "Resource temporarily unavailable")
        self.state["phase"] = "running"
        started.append(statement)
        return mock.Mock(poll=mock.Mock(return_value=None)), object()

    def spawn(self, target, args, token):
        self.worker_token = token

    for change in (mock.patch.object(server.App, "_launch_solver_locked", launch),
                   mock.patch.object(server.App, "_spawn_worker", spawn),
                   mock.patch.object(server, "require_model_access")):
        change.start()
        case.addCleanup(change.stop)
    return started


def run_node(case, script, *args):
    result = subprocess.run([NODE, "-e", script, str(APP), *args], capture_output=True, text=True, timeout=30)
    case.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class StatementReviewRetryTests(Isolated):
    """I-016: a failed or interrupted statement review runs again in its own job."""

    def reviewing(self, statement="Prove S.", feedback=""):
        app = server.App(runs=self.runs, load_latest=False)
        with mock.patch.object(server.App, "_spawn_worker"):
            app.start_review(statement, feedback)
        return app

    @staticmethod
    def worker(records=(), code=0):
        return mock.Mock(stdin=io.StringIO(), stdout=iter(json.dumps(record) + "\n" for record in records),
                         poll=mock.Mock(return_value=code), wait=mock.Mock(return_value=code))

    def test_a_failed_revision_keeps_its_statement_and_feedback_and_runs_again_in_place(self):
        app = self.reviewing()
        checked = {"kind": "review_result", "review": {"statement": "Checked S.", "notes": "None."}}
        with mock.patch.object(server.subprocess, "Popen", return_value=self.worker([checked])):
            app._review("Prove S.", token=app.active_token)
        self.assertEqual(app.state["phase"], "reviewed")
        with mock.patch.object(server.App, "_spawn_worker"):
            app.start_review("Checked S.", "Use n vertices.", revise_existing=True)
        with mock.patch.object(server.subprocess, "Popen", return_value=self.worker(code=1)):
            app._review("Checked S.", "Use n vertices.", token=app.active_token)
        # Back at the statement it revised, with the error and the feedback to send again.
        self.assertEqual((app.state["phase"], app.state["reviewFeedback"]), ("reviewed", "Use n vertices."))
        self.assertEqual(app.state["review"]["statement"], "Checked S.")
        self.assertTrue(app.state["error"].startswith("Review failed"))
        # Recorded: a restart restores the failure, which waits for Retry statement review.
        restored = server.restore_saved_app(server.App(app.run_dir / "transcript.jsonl", self.runs))
        self.assertEqual((restored.state["phase"], restored.state["reviewRetryAt"]), ("reviewed", 0))
        self.assertEqual(restored.state["error"], app.state["error"])
        plan = server.Server._stopped_continuation_plan(restored)
        self.assertEqual((plan["label"], plan["inPlace"]), ("Retry statement review", True))
        sent = []
        with mock.patch.object(server.App, "_spawn_worker", lambda self, target, args, token: sent.append(args)):
            restored.retry_review()
        self.assertEqual((restored.state["phase"], restored.run_dir), ("reviewing", app.run_dir))
        self.assertEqual(sent[0][:2], ("Checked S.", "Use n vertices."))

    def test_a_review_that_a_restart_interrupted_starts_again_by_itself(self):
        app = self.reviewing("Prove T.")
        app.add_trace({"kind": "request", "stage": "review", "label": "Exact review input", "text": "Prove T."})
        manager = self.server()
        restored = manager.jobs[app.run_dir.name]
        self.assertEqual(restored.state["phase"], "input")
        self.assertEqual(restored.state["error"],
                         "A restart interrupted the statement review; it starts again by itself from its saved input.")
        due = restored.state["reviewRetryAt"]
        self.assertGreater(due, 0)
        sent = []
        with mock.patch.object(server.App, "_spawn_worker", lambda self, target, args, token: sent.append(args)):
            manager._retry_paused_jobs(now=due)
        self.assertEqual(restored.state["phase"], "reviewing")
        self.assertEqual(sent[0][:2], ("Prove T.", ""))


class LaunchFailureTests(Isolated):
    """I-027: one job that cannot start stops none of the others and is tried again every minute."""

    def test_a_folder_batch_keeps_its_started_jobs_and_retries_the_one_that_failed(self):
        started = fake_launches(self, failing=["Prove B."])
        manager = self.server()
        statements = [{"name": name, "statement": f"Prove {name[0].upper()}."} for name in ("a.md", "b.md", "c.md")]
        jobs = manager.start_direct_batch({"statements": statements})
        self.assertEqual(started, ["Prove A.", "Prove C."])
        failed = next(app for app in jobs if app.state["sourceFile"] == "b.md")
        self.assertEqual((failed.state["phase"], failed.state["launchQueued"]), ("prepared", True))
        self.assertEqual(failed.state["launchError"], "Could not start: [Errno 11] Resource temporarily unavailable.")
        self.assertIn("Could not start the job for b.md", jobs.notice)
        self.assertIn("tried again every 1 minute", jobs.notice)
        # Nothing was stopped.
        self.assertFalse(any(app.state.get("manuallyStopped") for app in jobs))
        self.assertFalse(list(self.runs.glob(f"*/{server.MANUAL_STOP_FILENAME}")))
        due = failed.state["autoResumeAt"]
        manager._retry_paused_jobs(now=due - 1)
        self.assertEqual(started, ["Prove A.", "Prove C."])
        manager._retry_paused_jobs(now=due)
        self.assertEqual(started, ["Prove A.", "Prove C.", "Prove B."])
        self.assertEqual(failed.state["launchError"], "")

    def test_a_job_that_cannot_be_created_is_created_again_later(self):
        fake_launches(self)
        create = server.App.start_direct_statement
        failures = ["Prove B."]

        def flaky(app, statement, *args, **kwargs):
            if statement in failures:
                failures.remove(statement)
                app._new_run(statement)
                raise OSError(28, "No space left on device")
            return create(app, statement, *args, **kwargs)

        manager = self.server()
        with mock.patch.object(server.App, "start_direct_statement", flaky):
            jobs = manager.start_direct_batch({"statements": [{"name": "a.md", "statement": "Prove A."},
                                                              {"name": "b.md", "statement": "Prove B."}]})
        self.assertEqual([app.state["sourceFile"] for app in jobs], ["a.md"])
        self.assertIn("Could not create the job for b.md", jobs.notice)
        # The half-made folder is gone; the statement waits in memory for the next minute.
        self.assertEqual(sorted(path.name for path in self.runs.iterdir() if not path.name.startswith(".")),
                         [jobs[0].run_dir.name])
        manager._retry_paused_jobs(now=time.time() + WAIT + 1)
        names = sorted(app.state.get("sourceFile") for app in manager.jobs.values())
        self.assertEqual(names, ["a.md", "b.md"])
        self.assertEqual(manager._pending_jobs, [])

    def test_a_terminal_folder_run_keeps_its_jobs_and_retries_a_failed_start(self):
        folder = self.root / "statements"
        folder.mkdir()
        for name in ("a.md", "b.md"):
            (folder / name).write_text(f"Prove {name[0].upper()}.\n", encoding="utf-8")
        attempts = []

        def launch(app, statement):
            attempts.append(statement)
            if statement == "Prove B." and attempts.count(statement) == 1:
                raise OSError(11, "Resource temporarily unavailable")
            return mock.Mock(), object()

        def finish(app, target, args, token):
            app.state.update(phase="done", error="", resultKind="final")

        errors = io.StringIO()
        with mock.patch.object(server.App, "_launch_solver_locked", launch), \
                mock.patch.object(server.App, "_spawn_worker", finish), \
                mock.patch.object(server, "require_model_access"), \
                mock.patch.object(server, "AUTO_RESUME_SECONDS", 0.05), \
                mock.patch.object(cli, "AUTO_LAUNCH_SPACING_SECONDS", 0):
            code = cli.run_headless_markdown(folder, runs=self.runs, output_stream=io.StringIO(), error_stream=errors)
        self.assertEqual(code, 0)
        self.assertEqual(attempts, ["Prove A.", "Prove B.", "Prove B."])
        lines = errors.getvalue().splitlines()
        self.assertIn("[b.md] Could not start: [Errno 11] Resource temporarily unavailable. Trying again in 0.05 seconds.",
                      lines)
        self.assertEqual(len([line for line in lines if "Proof finished" in line]), 2)

    def test_a_terminal_folder_run_creates_a_job_again_when_its_folder_could_not_be_made(self):
        folder = self.root / "statements"
        folder.mkdir()
        for name in ("a.md", "b.md"):
            (folder / name).write_text(f"Prove {name[0].upper()}.\n", encoding="utf-8")
        create, failures = server.App.start_direct_statement, ["Prove B."]

        def flaky(app, statement, *args, **kwargs):
            if statement.strip() in failures:
                failures.remove(statement.strip())
                raise OSError(28, "No space left on device")
            return create(app, statement, *args, **kwargs)

        def finish(app, target, args, token):
            app.state.update(phase="done", error="", resultKind="final")

        errors = io.StringIO()
        with mock.patch.object(server.App, "start_direct_statement", flaky), \
                mock.patch.object(server.App, "_launch_solver_locked", lambda app, statement: (mock.Mock(), object())), \
                mock.patch.object(server.App, "_spawn_worker", finish), \
                mock.patch.object(server, "require_model_access"), \
                mock.patch.object(cli, "AUTO_RESUME_SECONDS", 0.05), \
                mock.patch.object(cli, "AUTO_LAUNCH_SPACING_SECONDS", 0):
            code = cli.run_headless_markdown(folder, runs=self.runs, output_stream=io.StringIO(), error_stream=errors)
        self.assertEqual(code, 0)
        lines = errors.getvalue().splitlines()
        self.assertIn("[b.md] Cannot start yet: [Errno 28] No space left on device. Trying again in 0.05 seconds.", lines)
        self.assertEqual(sorted(line.split("]")[0] for line in lines if "Proof finished" in line), ["[a.md", "[b.md"])

    def test_terminal_prints_never_stop_the_jobs(self):
        closed = mock.Mock(write=mock.Mock(side_effect=BrokenPipeError(32, "Broken pipe")))
        cli._say(closed, "[a.md] Proof started")  # No exception.


class TestRequestTests(Isolated):
    """I-040: the Test sends a request shaped like a run's; an endpoint that refuses those pauses with why."""

    def test_the_test_request_streams_offers_a_tool_and_asks_for_json_at_the_effort(self):
        entry = {"format": "anthropic", "baseUrl": "https://gw.example.com/v1", "model": "claude-gw",
                 "headerName": "x-api-key", "apiKey": KEY, "maxOutputTokens": 8000}
        path, headers, body = server._api_test_request(entry, "high")
        body = json.loads(body)
        self.assertEqual((path, headers["Accept"], headers["x-api-key"]), ("/v1/messages", "text/event-stream", KEY))
        self.assertEqual((body["stream"], body["max_tokens"], body["output_config"], body["tool_choice"]),
                         (True, 8000, {"effort": "high"}, {"type": "auto"}))
        self.assertEqual([tool["name"] for tool in body["tools"]], [claude_cli.STRUCTURED_OUTPUT_TOOL])
        openai = {**entry, "format": "openai", "headerName": "Authorization", "maxOutputTokens": None}
        path, headers, body = server._api_test_request(openai, "max")
        body = json.loads(body)
        self.assertEqual((path, body["stream"], body["reasoning"]["effort"]), ("/v1/responses", True, "max"))
        self.assertEqual([tool["name"] for tool in body["tools"]], ["report_result"])
        self.assertNotIn("max_output_tokens", body)
        # Without an effort: the minimal request that tells a refused shape from a refused endpoint.
        self.assertNotIn("tools", json.loads(server._api_test_request(openai)[2]))

    def test_an_invalid_request_of_an_api_model_is_a_settings_problem(self):
        model = self.api_model()["id"]
        rejected = runtime.Error("Codex review failed: unexpected status 400 Bad Request: "
                                 '{"error": {"type": "invalid_request_error", "message": "Unsupported parameter"}}')
        problem = runtime.settings_failure(rejected, model)
        self.assertIsInstance(problem, runtime.RequestRejected)
        self.assertTrue(str(problem).startswith(f"{runtime.REQUEST_REJECTED_LABEL}: Gateway refuses the requests"))
        self.assertFalse(runtime.retryable_failure(problem))
        # Built-in models, a conversation that is too long, and passing conditions stay retried.
        self.assertIsNone(runtime.settings_failure(rejected, "gpt-6.1-sol"))
        self.assertIsNone(runtime.settings_failure(runtime.Error("400 invalid_request_error: context_length_exceeded"),
                                                   model))
        self.assertIsNone(runtime.settings_failure(runtime.Error("400 invalid_request_error: overloaded, try later"),
                                                   model))


class StructuredSettingsFailureTests(retry_tests.RetryCase):
    """I-040, I-089: a structured call ends at once on a settings problem instead of retrying forever."""

    def setUp(self):
        super().setUp()
        store = mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.root / "state" / "api-models.json")})
        store.start()
        self.addCleanup(store.stop)
        self.model = providers.save_api_model({"format": "openai", "baseUrl": "https://gateway.example.com/v1",
                                               "model": "gpt-gateway", "apiKey": KEY, "label": "Gateway"})["id"]

    def attempts(self, error, model):
        calls = []

        def attempt(*args, **kwargs):
            calls.append(1)
            raise error

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            with self.assertRaises(runtime.ConfigurationError) as raised:
                runtime.structured("Review S.", retry_tests.SCHEMA, "review", model=model)
        return raised.exception, calls

    def test_an_endpoint_refusing_the_request_ends_the_call_after_one_attempt(self):
        error, calls = self.attempts(runtime.Error("unexpected status 400: invalid_request_error: bad tool"), self.model)
        self.assertIsInstance(error, runtime.RequestRejected)
        self.assertEqual((calls, self.waits()), ([1], 0))

    def test_a_model_that_does_not_exist_ends_the_call_after_one_attempt(self):
        error, calls = self.attempts(runtime.Error("The model `gpt-9` does not exist or you do not have access to it."),
                                     "gpt-6.1-sol")
        self.assertIsInstance(error, runtime.ModelUnavailable)
        self.assertTrue(str(error).startswith(f"{runtime.MODEL_UNAVAILABLE_LABEL}: "))
        self.assertEqual((calls, self.waits()), ([1], 0))


class TokenLimitTests(Isolated):
    """I-047 (API models): an optional context window and max output, passed to the CLIs."""

    def test_limits_are_optional_validated_and_can_be_cleared(self):
        model = self.api_model(contextWindow="400000", maxOutputTokens=64000)
        self.assertEqual((model["contextWindow"], model["maxOutputTokens"]), (400000, 64000))
        for values, message in (({"contextWindow": 512}, "at least 1,024"),
                                ({"contextWindow": 1000, "maxOutputTokens": 1000}, None),
                                ({"maxOutputTokens": 400000}, "smaller than the context window"),
                                ({"contextWindow": "many"}, "whole number")):
            with self.subTest(values=values), self.assertRaises(providers.ProviderError) as refused:
                providers.save_api_model({"id": model["id"], **values})
            if message:
                self.assertIn(message, str(refused.exception))
        cleared = providers.save_api_model({"id": model["id"], "contextWindow": "", "maxOutputTokens": ""})
        self.assertEqual((cleared["contextWindow"], cleared["maxOutputTokens"]), (None, None))
        self.assertNotIn("contextWindow", json.loads(self.store.read_text())["models"][0])

    def test_codex_gets_the_window_and_compacts_early_enough_for_the_output(self):
        model = self.api_model(contextWindow=400000, maxOutputTokens=64000)["id"]
        arguments = runtime.provider_arguments(model)
        self.assertEqual(arguments[:4], ["-c", "model_context_window=400000",
                                         "-c", "model_auto_compact_token_limit=268800"])
        # Codex 0.160 has no setting for the output limit itself.
        self.assertNotIn("max_output", " ".join(arguments))
        plain = self.api_model(model="gpt-plain", label="Plain")["id"]
        self.assertNotIn("model_context_window", " ".join(runtime.provider_arguments(plain)))

    def test_claude_gets_both_limits_in_its_environment(self):
        model = self.api_model(format="anthropic", baseUrl="https://gw.example.com", model="claude-gw",
                               contextWindow=200000, maxOutputTokens=32000)["id"]
        environment = claude_cli.gateway_environment(model, KEY)
        self.assertEqual((environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]),
                         ("200000", "32000"))


class RequestSizeTests(Isolated):
    """I-062: one larger limit for every request that creates a job, checked before sending, with its reason."""

    def test_an_oversized_job_request_is_refused_with_its_reason(self):
        manager = self.server(serve=True)
        status, value = self.call(manager, "/direct", raw=b"{" + b" " * (9 * 1024 * 1024) + b"}")
        self.assertEqual(status, 413)
        self.assertEqual(value["error"], "This request is 9.0 MB; the limit is 8 MB.")
        # The connection survived: the server read the body before answering, and still works.
        self.assertEqual(self.call(manager, "/settings")[0], 200)

    def test_one_statement_limit_for_every_way_to_start_a_job(self):
        long = "x" * (server.MAX_STATEMENT_CHARS + 1)
        self.assertEqual(server.MAX_STATEMENT_CHARS, server.MAX_BATCH_STATEMENT_CHARS)
        message = f"longer than {server.MAX_STATEMENT_CHARS:,} characters"
        for start in (lambda app: app.start_review(long), lambda app: app.start_direct_statement(long),
                      lambda app: app.start_latex_only(long)):
            with self.subTest(start=start), self.assertRaisesRegex(ValueError, message):
                start(server.App(runs=self.runs, load_latest=False))
        # A statement a folder batch accepts is accepted on its own too.
        with mock.patch.object(server.App, "_spawn_worker"):
            server.App(runs=self.runs, load_latest=False).start_review("x" * 150_000)

    @unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
    def test_the_browser_refuses_an_oversized_job_request_before_sending(self):
        run_node(self, r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function request('), source.indexOf('// Batch identity keeps equal run names'));
const setup = `
let requests = Promise.resolve();
const selectedBatch = () => null, state = {}, sessionToken = 't', sent = [];
const fetch = async (path) => { sent.push(path); return {ok: true, json: async () => ({ok: true})}; };
`;
const context = {assert};
Promise.resolve(vm.runInNewContext(setup + code + `
(async () => {
  const big = 'é'.repeat(4.5 * 1024 * 1024);  // 9 MB in UTF-8
  await assert.rejects(request('/direct?job=x', {statement: big}), /This request is 9\.0 MB; the limit is 8 MB/);
  await assert.rejects(request('/direct-batch', {statements: [{name: 'a', statement: big}]}), /the limit is 8 MB/);
  assert.deepEqual(sent, []);
  // Other requests are not measured, and a job request under the limit goes out.
  await request('/steer-author?job=x', {instruction: big});
  await request('/review', {statement: 'x'.repeat(3 * 1024 * 1024)});
  assert.deepEqual(sent, ['/steer-author?job=x', '/review']);
})();`, context)).catch((error) => { console.error(error); process.exitCode = 1; });
""")


class GatewayLimitTests(gateway_tests._GatewayFixture):
    """I-062: the live gateway has the same limit for every job request and says so."""

    def test_an_oversized_job_request_gets_its_reason_through_the_gateway(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=30)
        raw = b"{" + b" " * (9 * 1024 * 1024) + b"}"
        try:
            connection.request("POST", "/review", body=raw, headers={
                "Origin": self.gateway.origin, "X-TCS-Prover-Token": self.gateway.token,
                "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn("the limit is 8 MB", json.loads(response.read())["error"])
        finally:
            connection.close()
        self.assertEqual(self.current.calls, [])
        status, _ = self.request("/review", method="POST", body={"statement": "x" * 200_000})
        self.assertEqual(status, 200)


class StoreReadTests(Isolated):
    """I-074, I-080: an unreadable store is retried, never taken for removed models; bad entries block nothing."""

    def damage(self):
        self.store.write_text("{", encoding="utf-8")

    def test_a_damaged_store_is_unavailable_and_running_jobs_keep_their_last_settings(self):
        model = self.api_model()["id"]
        self.assertTrue(providers.is_known(model))
        self.damage()
        with self.assertRaisesRegex(providers.StoreUnavailable, "is damaged; fix or remove it"):
            providers.store_listing()
        # A process that read the store keeps those settings (the runner of a running job).
        self.assertTrue(providers.is_known(model))
        with self.assertRaises(providers.StoreUnavailable):
            providers.api_model(model, cached=False)
        providers._LAST_GOOD.pop(str(self.store))
        with self.assertRaises(server.TransientLaunchError):
            server.require_known_models({"author": model})
        with self.assertRaisesRegex(runtime.Error, "is damaged") as failure:
            runtime.chosen_model(model)
        self.assertTrue(runtime.retryable_failure(failure.exception))
        listing = self.server().api_model_listing()
        self.assertEqual(listing["models"], [])
        self.assertIn("is damaged", listing["storeProblem"])

    def test_an_unreadable_store_at_retry_time_is_retried_with_its_reason(self):
        model = self.api_model()["id"]
        app = self.paused_job(authorModel=model)
        due = app.state["autoResumeAt"]
        self.assertGreater(due, 0)
        saved = self.store.read_text(encoding="utf-8")
        self.damage()
        launches = []
        with mock.patch.object(server, "require_model_access"), \
                mock.patch.object(server.App, "_launch_workflow_locked",
                                  lambda self, *args, **kwargs: launches.append(1) or (mock.Mock(), object())), \
                mock.patch.object(server.App, "_spawn_worker"):
            self.assertTrue(app.auto_resume_if_due(app.resume, now=due))
            self.assertEqual(launches, [])
            self.assertEqual(app.state["waitingForSettings"], "")
            self.assertEqual(app.state["autoResumeAt"], due + WAIT)
            self.assertTrue(any("Automatic resume failed" in text and "is damaged" in text
                                for text in self.diagnostics(app)))
            self.assertIsNone(app.attention())
            self.store.write_text(saved, encoding="utf-8")  # Readable again.
            self.assertTrue(app.auto_resume_if_due(app.resume, now=due + WAIT))
        self.assertEqual(launches, [1])

    def test_one_invalid_entry_is_listed_for_repair_and_blocks_nothing(self):
        good = self.api_model()
        value = json.loads(self.store.read_text(encoding="utf-8"))
        value["models"].append({"id": "api:broken", "label": "Broken", "format": "openai", "baseUrl": "ftp://x",
                                "model": "m", "apiKey": "sk-hidden-0123456789"})
        self.store.write_text(json.dumps(value), encoding="utf-8")
        models, invalid = providers.store_listing()
        self.assertEqual([row["id"] for row in models], [good["id"]])
        [entry] = invalid
        self.assertEqual((entry["id"], entry["label"], entry["hasKey"]), ("api:broken", "Broken", True))
        self.assertIn("base URL", entry["problem"])
        self.assertNotIn("sk-hidden", json.dumps(entry))
        # Other models are still saved, tested and removed; the invalid entry stays as it was.
        other = self.api_model(model="gpt-other", label="Other")
        self.assertTrue(providers.delete_api_model(other["id"]))
        self.assertEqual(len(providers.store_listing()[1]), 1)
        # Repair: saving under its id replaces it in place.
        repaired = providers.save_api_model({"id": "api:broken", "format": "openai",
                                             "baseUrl": "https://repaired.example.com/v1", "model": "m",
                                             "apiKey": KEY, "label": "Broken"})
        self.assertEqual(repaired["id"], "api:broken")
        self.assertEqual(providers.store_listing()[1], [])
        # Remove by reference.
        value = json.loads(self.store.read_text(encoding="utf-8"))
        value["models"].append({"label": "No id"})
        self.store.write_text(json.dumps(value), encoding="utf-8")
        [entry] = providers.store_listing()[1]
        self.assertTrue(providers.delete_invalid_api_model(entry["ref"]))
        self.assertEqual(providers.store_listing()[1], [])


class RemovedModelTests(Isolated):
    """I-087: a model a waiting job needs cannot be removed; only roles Resume runs count."""

    def test_a_model_that_a_waiting_job_needs_cannot_be_removed(self):
        model = self.api_model()["id"]
        manager = self.server()
        app = self.paused_job(criticModel=model)
        manager.jobs[app.run_dir.name] = app
        with self.assertRaisesRegex(ValueError, "Gateway is used by 1 job waiting to start or resume"):
            manager.delete_api_model({"id": model})
        self.assertTrue(providers.is_known(model))
        # The reviewer of a paused job never runs again: it does not keep its model.
        app.state.update(criticModel="gpt-6.1-sol", reviewModel=model)
        manager.delete_api_model({"id": model})
        self.assertFalse(providers.is_known(model))
        # And a paused job does not wait for it.
        with mock.patch.object(server, "require_model_access"):
            app._check_launch_models_locked()


class ReplacedModelTests(Isolated):
    """I-088: a removed model's documented replacement resumes; roles that never run again are ignored."""

    def test_a_job_whose_finished_reviewer_named_deepseek_keeps_retrying_without_a_warning(self):
        directory = self.runs / "reviewed"
        directory.mkdir()
        (directory / "draft.md").write_text("# Draft problem\n\nSaved statement\n", encoding="utf-8")
        (directory / "checked-statement.md").write_text("# Checked statement\n\nSaved statement\n", encoding="utf-8")
        (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({
            "reviewModel": "deepseek-v4-pro", "authorModel": "gpt-6.1-sol", "criticModel": "gpt-6.1-sol",
            "writerModel": "gpt-6.1-sol", "thinkingHours": 24}), encoding="utf-8")
        records = [{"kind": "request", "stage": "review", "model": "deepseek-v4-pro"},
                   {"kind": "review_result", "stage": "review", "review": {"statement": "S", "notes": ""}},
                   {"kind": "request", "stage": "solve", "model": "gpt-6.1-sol"}]
        (directory / "transcript.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records),
                                                    encoding="utf-8")
        (directory / server.PAUSE_FILENAME).write_text(json.dumps({
            "status": "paused", "node": "author", "stage": "solve", "state": {"statement": "S"},
            "pauseCause": "error", "error": "Network error", "elapsedSeconds": 30, "autoResumeAt": 0}), encoding="utf-8")
        app = server.restore_saved_jobs(self.runs)["reviewed"]
        self.assertEqual((app.state["phase"], app.state["settingsWarning"]), ("paused", ""))
        self.assertFalse(app.state.get("replacedRoles"))
        self.assertGreater(app.state["autoResumeAt"], 0)


class UnavailableModelTests(Isolated):
    """I-089: a model that does not exist or cannot be used is a settings problem, not an endless retry."""

    def test_a_built_in_model_that_cannot_be_used_needs_attention_and_waits_for_resume(self):
        problem = runtime.settings_failure(runtime.Error(
            "Codex author turn failed: The model `gpt-9` does not exist or you do not have access to it."), "gpt-6.1-sol")
        self.assertIsInstance(problem, runtime.ModelUnavailable)
        self.assertIsNone(runtime.settings_failure(runtime.Error("Selected model is at capacity."), "gpt-6.1-sol"))
        app = self.paused_job(reason=str(problem))
        self.assertEqual(app.state["autoResumeAt"], 0)
        attention = app.attention()
        self.assertEqual((attention["kind"], attention["title"], attention["retrying"]),
                         ("model", "Model unavailable", False))

    def test_an_api_job_waiting_for_its_settings_resumes_once_they_change(self):
        model = self.api_model()["id"]
        problem = runtime.settings_failure(runtime.Error("400 Bad Request: invalid_request_error: bad field"), model)
        app = self.paused_job(reason=str(problem), authorModel=model)
        self.assertEqual(app.state["autoResumeAt"], 0)
        self.assertEqual(app.attention()["kind"], "request")
        resumed = []
        self.assertFalse(app.auto_resume_if_due(lambda: resumed.append(1), now=10**9))
        time.sleep(1.1)  # updatedAt has one-second resolution.
        providers.save_api_model({"id": model, "headerName": "X-Api-Key", "apiKey": KEY})
        with mock.patch.object(server, "require_model_access"):
            self.assertTrue(app.auto_resume_if_due(lambda: resumed.append(1), now=10**9 + 60))
        self.assertEqual(resumed, [1])


class AttentionTests(Isolated):
    """I-090, I-075: jobs that need you are named on the home page, while their retries go on."""

    def test_login_usage_and_key_problems_need_attention_while_retries_continue(self):
        login = self.paused_job(reason="Codex author turn failed: 401 Unauthorized: please log in again")
        attention = login.attention()
        self.assertEqual((attention["kind"], attention["title"], attention["retrying"]),
                         ("login", "Codex login needed", True))
        self.assertIn("codex login", attention["text"])
        self.assertGreater(login.state["autoResumeAt"], 0)
        running = server.App(runs=self.runs, load_latest=False)
        running.state.update(phase="running", stage="critic", modelRetry={"failure": "usage limit reached",
                                                                        "stage": "critic", "retryAt": 10**9})
        self.assertEqual(running.attention()["kind"], "usage")
        model = self.api_model()["id"]
        key = self.paused_job(reason="Model call failed: 401 invalid api key", authorModel=model)
        self.assertEqual((key.attention()["kind"], key.attention()["title"]), ("key", "API key rejected"))
        self.assertIsNone(self.paused_job(reason="Connection lost").attention())

    def test_the_home_page_lists_the_jobs_that_need_attention(self):
        login = self.paused_job(reason="Codex author turn failed: 401 Unauthorized", sourceFile="P07.md")
        manager = self.server(serve=True)
        manager.jobs = {login.run_dir.name: login}
        status, value = self.call(manager, "/batches")
        root = next(card for card in value["batches"] if card["id"] == "__root__")
        [item] = root["attention"]
        self.assertEqual((item["runId"], item["job"], item["kind"], item["title"]),
                         (login.run_dir.name, "P07.md", "login", "Codex login needed"))
        self.assertEqual(root["runningLimit"], 0)
        status, value = self.call(manager, "/jobs")
        self.assertEqual(value["jobs"][0]["needsAttention"]["kind"], "login")
        self.assertGreater(value["jobs"][0]["autoResumeAt"], 0)

    @unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
    def test_the_banner_and_the_job_cards_say_why_and_when(self):
        run_node(self, r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function request('), source.indexOf('function selectedProblemMode('))
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function localDateTime('));
// The fake DOM lives in the page's own realm, so its arrays compare with the checks' arrays.
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
const questions = [], posted = [];
const confirm = (question) => { questions.push(question); return true; }, alert = () => {};
const refresh = async () => {}, render = () => {};
const fetch = async (path) => { posted.push(path); return {ok: true, json: async () => ({runId: 'r', phase: 'reviewing'})}; };
`;
Promise.resolve(vm.runInNewContext(setup + code + `
(async () => {
  const time = (seconds) => new Date(seconds * 1000).toLocaleTimeString();
  renderJobs([
    {runId: 'a', phase: 'paused', title: 'P1', error: 'Model call failed (authentication failed).', autoResumeAt: 2e9,
     needsAttention: {kind: 'login', title: 'Codex login needed', text: 'Log in to the Codex CLI again (codex login); the job continues by itself.', retrying: true},
     checkpoints: [], canContinueStopped: true, continueStoppedLabel: 'Resume'},
    {runId: 'q', phase: 'prepared', title: 'P2', launchQueued: true, autoResumeAt: 2e9, error: 'Could not start: disk full.',
     checkpoints: [], canContinueStopped: true, continueStoppedLabel: 'Start now'},
    {runId: 'r', phase: 'input', title: 'P3', error: 'Review failed: x', checkpoints: [], canContinueStopped: true,
     continueStoppedLabel: 'Retry statement review', continueStoppedInPlace: true,
     continueStoppedDescription: 'Runs the statement review again in this job.'},
  ]);
  const [paused, queued, failed] = ui.jobsList.children;
  const texts = (card) => card.children[0].children.map((child) => child.textContent);
  assert.match(paused.className, /needs-attention/);
  assert.equal(paused.children[0].children[1].textContent, 'Paused · Codex login needed');
  assert(texts(paused).includes('Codex login needed: Log in to the Codex CLI again (codex login); the job continues by itself.'));
  assert(texts(paused).includes('Automatic retry at ' + time(2e9)));
  assert.match(queued.className, /tone-queued/);
  assert.equal(queued.children[0].children[1].textContent, 'Queued');
  assert(texts(queued).includes('Queued · starts when a running slot is free'));
  assert(texts(queued).includes('Next start attempt at ' + time(2e9)));
  assert(texts(queued).includes('Could not start: disk full.'));
  assert.match(ui.jobsCount.textContent, /1 queued/);
  assert.doesNotMatch(ui.jobsCount.textContent, /ready to start/);
  // A failed statement review runs again in its own job: no "new job" is promised.
  assert.equal(failed.children[1].children.find((button) => button.textContent === 'Retry statement review').hidden, false);
  await continueStopped({runId: 'r', phase: 'input', continueStoppedInPlace: true, continueStoppedLabel: 'Retry statement review',
    continueStoppedDescription: 'Runs the statement review again in this job.'}, 'P3');
  assert.equal(questions.at(-1), 'Retry statement review for “P3”?\\n\\nRuns the statement review again in this job.');
  assert.deepEqual(posted, ['/continue-stopped?job=r']);
  // The home page: the jobs that need you, and the queue.
  renderBatches([{id: '__root__', label: 'Runs', current: true, jobCount: 2, counts: {paused: 1, prepared: 1}, queued: 1,
    attention: [{runId: 'a', job: 'P1.md', kind: 'login', title: 'Codex login needed', text: 'Log in to the Codex CLI again.'}]}]);
  assert.equal(ui.attentionBanner.hidden, false);
  const [row] = ui.attentionList.children;
  assert.deepEqual(row.children.map((child) => child.textContent), ['Codex login needed', 'P1.md', 'Log in to the Codex CLI again.']);
  assert.equal(row.children[1].href, '/?job=a');
  const progress = ui.batchesList.children[0].children[0].children[2].children.map((child) => child.textContent);
  assert.deepEqual(progress, ['1 queued', '1 paused']);
  renderBatches([{id: '__root__', label: 'Runs', current: true, jobCount: 0, counts: {}, attention: []}]);
  assert.equal(ui.attentionBanner.hidden, true);
})();`, {assert, URLSearchParams})).catch((error) => { console.error(error); process.exitCode = 1; });
""")


class RunningLimitTests(Isolated):
    """I-107: a limit on running jobs with a queue, starts spread out, and no claim of a quota per job."""

    def test_queued_jobs_start_in_order_within_the_limit_and_spaced(self):
        started = fake_launches(self)
        manager = self.server()
        manager.save_installation_settings({"maxRunningJobs": 1})
        self.assertEqual(server.load_installation_settings(), {"maxRunningJobs": 1})
        running = server.App(runs=self.runs, load_latest=False)
        running.state.update(phase="running", runId="running")
        second, first = self.queued_job("Prove B.", 200), self.queued_job("Prove A.", 100)
        manager.jobs = {"running": running, "b": second, "a": first}
        manager._retry_paused_jobs(now=1_000)
        self.assertEqual(started, [])  # The one slot is taken.
        running.state["phase"] = "done"
        manager._retry_paused_jobs(now=1_000)
        self.assertEqual(started, ["Prove A."])  # The one that waited longest.
        manager.save_installation_settings({"maxRunningJobs": 3})
        manager._retry_paused_jobs(now=1_001)
        self.assertEqual(started, ["Prove A."])  # At most one start every AUTO_LAUNCH_SPACING_SECONDS.
        manager._retry_paused_jobs(now=1_000 + server.AUTO_LAUNCH_SPACING_SECONDS)
        self.assertEqual(started, ["Prove A.", "Prove B."])

    def test_a_folder_batch_beyond_the_limit_waits_in_the_queue(self):
        started = fake_launches(self)
        manager = self.server()
        manager.save_installation_settings({"maxRunningJobs": 2})
        jobs = manager.start_direct_batch({"statements": [{"name": f"{name}.md", "statement": f"Prove {name}."}
                                                          for name in "ABC"]})
        self.assertEqual(started, ["Prove A.", "Prove B."])
        self.assertEqual(jobs[2].state["launchQueued"], True)
        self.assertIn("1 job waits in the queue: at most 2 jobs run at once", jobs.notice)
        listed = {row["runId"]: row for row in manager.job_list()}
        self.assertTrue(listed[jobs[2].run_dir.name]["launchQueued"])
        self.assertEqual(listed[jobs[2].run_dir.name]["continueStoppedLabel"], "Start now")

    def test_the_limit_is_read_and_saved_over_http(self):
        manager = self.server(serve=True)
        self.assertEqual(self.call(manager, "/settings"), (200, {"maxRunningJobs": 0, "runningJobs": 0, "queuedJobs": 0}))
        self.assertEqual(self.call(manager, "/settings", {"maxRunningJobs": 4})[1]["maxRunningJobs"], 4)
        status, value = self.call(manager, "/settings", {"maxRunningJobs": -1})
        self.assertEqual(status, 400)
        self.assertIn("0 for no limit", value["error"])
        self.assertEqual(json.loads((self.store.parent / server.INSTALLATION_SETTINGS).read_text()), {"maxRunningJobs": 4})

    def test_no_job_claims_a_quota_of_its_own(self):
        for path in (APP, server.UI / "index.html", Path(server.ROOT) / "README.md"):
            self.assertNotIn("own quota", path.read_text(encoding="utf-8"))


class RetentionTests(Isolated):
    """I-118: login copies are deleted after each call; private per-call folders and the trash are pruned."""

    def test_deleted_jobs_stay_in_the_trash_for_30_days(self):
        manager = self.server()
        app = self.paused_job()
        app.state.update(phase="done", autoResumeAt=0)
        manager.jobs[app.run_dir.name] = app
        manager.delete_job(app.run_dir.name)
        trash = self.runs / server.TRASH_FOLDER
        self.assertTrue((trash / app.run_dir.name).is_dir())
        self.assertFalse(server.controller_directory(app.run_dir).exists())
        arrived = json.loads((trash / server.TRASH_INDEX).read_text())[app.run_dir.name]
        server.prune_trash(self.runs, now=arrived + 29 * 24 * 3600)
        self.assertTrue((trash / app.run_dir.name).is_dir())
        server.prune_trash(self.runs, now=arrived + 30 * 24 * 3600)
        self.assertFalse((trash / app.run_dir.name).exists())
        # A job moved there by an older version starts its 30 days when first seen.
        (trash / "older").mkdir()
        server.prune_trash(self.runs, now=arrived + 365 * 24 * 3600)
        self.assertTrue((trash / "older").is_dir())

    def test_old_proof_snapshots_are_pruned_beyond_the_newest(self):
        old = time.time() - 2 * 24 * 3600
        for index in range(runtime.SNAPSHOTS_KEPT + 3):
            folder = self.root / f"{runtime.SNAPSHOT_PREFIX}{index:03d}"
            folder.mkdir()
            os.utime(folder, (old + index, old + index))
        young = self.root / f"{runtime.SNAPSHOT_PREFIX}young"
        young.mkdir()
        os.utime(young, (old - 10**6, time.time()))
        with mock.patch.object(runtime.tempfile, "gettempdir", return_value=str(self.root)):
            runtime._prune_snapshots()
        left = sorted(path.name for path in self.root.glob(runtime.SNAPSHOT_PREFIX + "*"))
        self.assertEqual(len(left), runtime.SNAPSHOTS_KEPT)
        self.assertIn(young.name, left)
        self.assertNotIn(f"{runtime.SNAPSHOT_PREFIX}000", left)

    def test_discovery_deletes_its_login_copy_and_prunes_old_runs(self):
        executable = self.root / "codex"
        executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"native-test-only")
        executable.chmod(0o700)
        auth = self.root / "auth"
        auth.mkdir()
        (auth / "auth.json").write_text('{"tokens":{"access_token":"test"}}')
        state = self.root / "state-discovery"
        state.mkdir(mode=0o700)
        old = time.time() - 2 * 24 * 3600
        for index in range(discovery.DISCOVERIES_KEPT + 2):
            folder = state / f"discovery-{index:04d}"
            folder.mkdir()
            os.utime(folder, (old, old))
        with mock.patch.object(discovery.sys, "platform", "darwin"):
            settings = discovery.build_settings(executable, auth, state, "gpt-6.1-sol")
            with mock.patch.object(discovery, "_verify", side_effect=discovery.DiscoveryError()), \
                    mock.patch("retrieval.historical_discovery_auth.preserve_refresh", return_value=False):
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.preflight(settings)
        folders = list(state.glob("discovery-*"))
        self.assertEqual(len(folders), discovery.DISCOVERIES_KEPT + 1)
        self.assertEqual(list(state.glob("discovery-*/home/auth.json")), [])

    def test_a_critic_session_keeps_its_record_but_no_login_copy(self):
        fixture = cut_tests.HistoricalPolicyTests("test_critic_sessions_are_fresh_auth_only_and_keep_original_policy")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.protected_auth()
        with retrieval_runtime.critic_session(fixture.policy()) as session:
            home = retrieval_runtime.role_home(session, "critic")
            self.assertTrue((home / "auth.json").is_file())
        self.assertTrue(home.is_dir())
        self.assertFalse((home / "auth.json").exists())


class PrivateStateTests(Isolated):
    """I-120: the private state folder is checked at startup; a linked folder of this user is accepted."""

    def test_a_linked_state_folder_of_this_user_is_used_and_made_private(self):
        target = self.root / "elsewhere"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        link = self.root / "linked-state"
        link.symlink_to(target, target_is_directory=True)
        self.assertEqual(providers.private_directory(link), target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        with mock.patch.dict(os.environ, {providers.STORE_ENV: str(link / "api-models.json")}):
            self.assertEqual(providers.check_private_state(), "")
            self.assertEqual(self.api_model()["label"], "Gateway")

    def test_an_unusable_state_folder_is_named_at_startup_and_is_a_settings_problem(self):
        link = self.root / "dangling-state"
        link.symlink_to(self.root / "missing", target_is_directory=True)
        with mock.patch.dict(os.environ, {providers.STORE_ENV: str(link / "api-models.json")}):
            problem = providers.check_private_state()
            self.assertIn("symbolic link to a folder that does not exist", problem)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                manager = self.server()
            self.assertEqual(manager.state_problem, problem)
            self.assertIn(problem, errors.getvalue())
            self.assertEqual(manager.api_model_listing()["stateProblem"], problem)
            with self.assertRaisesRegex(runtime.ConfigurationError, "Cannot prepare the private Codex home"):
                runtime.api_codex_home(self.runs)

    def test_the_review_worker_reports_a_settings_problem_instead_of_crashing(self):
        errors = io.StringIO()
        with mock.patch.object(review, "review", side_effect=providers.ProviderError("Private state is unusable.")), \
                mock.patch.object(review.runtime, "configure_standard_streams"), \
                mock.patch.object(review.runtime, "check_clis"), \
                mock.patch.object(review.sys, "stdin", io.StringIO("Prove S.")), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(review.review_worker_main([]), 1)
        self.assertEqual(errors.getvalue(), "error: Private state is unusable.\n")


class SavedRunDisplayTests(Isolated):
    """I-131: nothing too large is silently replaced; controller state is read whole; the live view says so."""

    def saved_run(self, **pause):
        directory = self.runs / "saved"
        directory.mkdir()
        (directory / "draft.md").write_text("# Draft problem\n\nA theorem.\n")
        (directory / "transcript.jsonl").write_text('{"kind":"status"}\n')
        if pause:
            (directory / server.PAUSE_FILENAME).write_text(json.dumps(pause))
        return directory, batches.BatchCatalog(self.runs, current_root=self.root / "elsewhere")

    def test_files_too_large_to_show_are_named_and_controller_state_is_read_whole(self):
        directory, catalog = self.saved_run()
        big = "x" * (batches.MAX_ARTIFACT_BYTES + 1)
        (directory / "partial-output.md").write_text(big)
        (directory / "SOLUTION.md").write_text("Another file, never shown in its place.")
        (directory / "prompts").mkdir()
        (directory / "prompts/author.txt").write_text(big)
        settings = {"authorModel": "gpt-6-astra", "padding": big}
        (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(settings))
        (directory / server.PAUSE_FILENAME).write_text(json.dumps({"status": "paused", "stage": "solve",
                                                                   "node": "author", "padding": big}))
        state = catalog.state("__root__", "saved")
        self.assertEqual(state["phase"], "paused")
        self.assertEqual(state["authorModel"], "gpt-6-astra")
        self.assertTrue(state["output"].startswith(
            "partial-output.md is too large to show here (2.0 MB). See partial-output.md in the run folder"))
        self.assertTrue(state["authorPrompt"].startswith("prompts/author.txt is too large to show here"))

    def test_a_run_that_stopped_part_way_is_interrupted(self):
        self.saved_run(status="running", stage="solve", node="author")
        job = batches.BatchCatalog(self.runs, current_root=self.root / "elsewhere").jobs("__root__")[0]
        self.assertTrue(job["interrupted"])
        self.assertEqual(job["error"], "Interrupted: the run stopped part-way, while its last checkpoint says it was running.")

    def test_the_live_view_says_older_events_left_and_where_the_rest_is(self):
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run("Exact statement")
        app.state["traceVersion"] = len(app.state["trace"]) + 500
        state = app.snapshot()
        self.assertTrue(state["traceTruncated"])
        self.assertIn("transcript.jsonl", state["traceNotice"])

    @unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
    def test_the_job_page_offers_the_transcript_and_keeps_failed_feedback(self):
        run_node(self, r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const renderer = source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function localDateTime('))
  + source.slice(source.indexOf('function render(next)'), source.indexOf('async function refresh()'));
const handlers = source.slice(source.indexOf('ui.stop.onclick ='), source.indexOf('ui.home.onclick ='));
const setup = `
let state = {}, previousPhase = '', currentJob = 'job', timer, clock, reviewPending = false;
const ui = new Proxy({}, {get(target, name) {return target[name] ||= {
  classList: {toggle() {}}, setAttribute() {}, focus() {}, replaceChildren() {}
};}});
const document = {activeElement: null, body: {append() {}}, createElement() {
  return {click() {download = this.download;}, remove() {}};
}};
const show = (element, visible) => element.hidden = !visible;
let keep = Infinity;
const retainTrace = (entries) => entries.slice(-keep);
const ingest = () => {}, renderWorkflow = () => {}, renderClock = () => {};
const renderBatchNavigation = () => {}, refresh = () => {}, renderResearchAudits = () => {}, managesResearchFiles = () => true;
const restoreRetrievalSettings = () => {};
const syncPrompts = () => {}, updateModelSummary = () => {}, appendFormattedText = () => {};
const fillResearchAuditControls = () => {}, checkEdited = () => {}, restoreWorkflowStages = () => {};
const setTimeout = () => 1, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
const jobPath = (path) => path + '?job=job', sessionToken = 'secret';
let requested = '', download = '';
const act = () => {};
const fetch = async (path) => { requested = path; return {ok: true, blob: async () => 'bytes'}; };
const URL = {createObjectURL() { return 'blob:x'; }, revokeObjectURL() {}};
`;
const checks = `
(async () => {
  const running = {runId: 'job', phase: 'running', stage: 'solve', activeNode: 'author', traceFrom: 0, traceVersion: 3,
    trace: [{kind: 'status'}, {kind: 'status'}, {kind: 'status'}]};
  render(running);
  assert.equal(ui.traceNotice.hidden, true);
  assert.equal(ui.downloadTranscript.hidden, true);
  // The server's window moved on: its words, and the transcript.
  render({...running, traceTruncated: true, traceNotice: 'Showing the latest 3 of 900 events.'});
  assert.equal(ui.traceNotice.textContent, 'Showing the latest 3 of 900 events.');
  assert.equal(ui.downloadTranscript.hidden, false);
  await ui.downloadTranscript.onclick();
  assert.equal(requested, '/transcript?job=job');
  assert.equal(download, 'transcript.jsonl');
  // This page dropped older routine events itself: it says so too, and keeps saying so.
  keep = 2;
  render({...running, traceFrom: 3, traceVersion: 4, trace: [{kind: 'status'}]});
  assert.equal(state.traceTrimmed, true);
  assert.equal(ui.traceNotice.hidden, false);
  assert.match(ui.traceNotice.textContent, /complete history is in this job's transcript\\.jsonl/);
  keep = Infinity;
  render({...running, traceFrom: 4, traceVersion: 5, trace: [{kind: 'status'}]});
  assert.equal(ui.traceNotice.hidden, false);
  // A revision that failed comes back with its feedback.
  const reviewed = {runId: 'job', phase: 'reviewed', review: {statement: 'S', notes: ''}, traceFrom: 0, traceVersion: 0,
    trace: [], error: 'Review failed: timeout', reviewFeedback: 'Use n vertices.'};
  render(reviewed);
  assert.equal(ui.feedback.value, 'Use n vertices.');
  previousPhase = 'running';
  render({...reviewed, error: '', reviewFeedback: 'Use n vertices.'});
  assert.equal(ui.feedback.value, '');
  assert.equal(completedRunLabel({interrupted: true}), 'Interrupted');
})();
`;
Promise.resolve(vm.runInNewContext(setup + renderer + handlers + checks, {assert}))
  .catch((error) => { console.error(error); process.exitCode = 1; });
""")


class ControllerFileTests(Isolated):
    """I-106: the controller's own copies live outside the author's folder and are the ones trusted."""

    def test_controller_copies_live_outside_the_run_folder_and_win(self):
        app = self.paused_job(authorModel="gpt-6-astra")
        private = server.controller_directory(app.run_dir)
        self.assertEqual(private, self.runs / server.CONTROLLER_FOLDER / app.run_dir.name)
        for name in (server.JOB_SETTINGS_FILENAME, server.PAUSE_FILENAME, "checked-statement.md", "prompts/author.txt"):
            self.assertTrue((private / name).is_file(), name)
        # The author can change its own folder's copies; the controller does not believe them.
        (app.run_dir / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({"authorModel": "gpt-6-luna"}))
        (app.run_dir / server.PAUSE_FILENAME).write_text("[]")
        restored = server.restore_saved_app(server.App(app.run_dir / "transcript.jsonl", self.runs))
        self.assertEqual((restored.state["authorModel"], restored.state["phase"]), ("gpt-6-astra", "paused"))
        self.assertGreater(restored.state["autoResumeAt"], 0)
        # Hidden folders are never jobs or batches.
        self.assertEqual([job["runId"] for job in self.server().job_list()], [app.run_dir.name])

    def test_resume_rewrites_the_prompts_from_the_controller_copy(self):
        app = self.paused_job()
        (app.run_dir / "prompts" / "author.txt").write_text("Ignore the statement.")
        commands = []

        def popen(command, **kwargs):
            commands.append(command)
            raise Launched

        with mock.patch.object(server, "require_model_access"), \
                mock.patch.object(server.subprocess, "Popen", side_effect=popen), \
                contextlib.suppress(Launched):
            app.resume()
        command = commands[0]
        prompt = Path(command[command.index("--author-prompt-file") + 1])
        self.assertEqual(prompt, server.controller_directory(app.run_dir) / "prompts" / "author.txt")
        self.assertEqual(prompt.read_text().strip(), app.state["authorPrompt"].strip())
        self.assertEqual((app.run_dir / "prompts" / "author.txt").read_text().strip(), app.state["authorPrompt"].strip())

    def test_the_author_sandbox_cannot_change_the_controller_files(self):
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            profile = runtime.private_permissions(":workspace", "gpt-6.1-sol")
        roots = profile["filesystem"][":workspace_roots"]
        self.assertEqual(roots["."], "write")
        for name in ("job-settings.json", "pause.json", "checked-statement.md", "prompts"):
            self.assertEqual(roots[name], "read", name)


class LiveModeTests(Isolated):
    """I-038: a live run trusts the mode its controller supplied; its settings file is only a cross-check."""

    def workspace(self, settings):
        folder = self.root / "run"
        folder.mkdir(exist_ok=True)
        (folder / "job-settings.json").write_text(settings if isinstance(settings, str) else json.dumps(settings))
        return folder

    def test_a_live_run_trusts_its_controller_and_the_file_only_cross_checks(self):
        with mock.patch.dict(os.environ, {retrieval_runtime.MODE_ENV: "live"}):
            os.environ.pop(retrieval_runtime.POLICY_ENV, None)
            self.assertEqual(retrieval_runtime.search_mode(self.workspace("{")), "live")
            self.assertEqual(retrieval_runtime.search_mode(self.workspace({"retrievalMode": "disabled"})), "live")
            self.assertIsNone(runtime.retrieval_policy(self.workspace("{")))
            historical = self.workspace({"retrievalMode": "historical"})
            with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, "supplied for a historical run"):
                retrieval_runtime.search_mode(historical)
            with self.assertRaises(runtime.ConfigurationError):
                runtime.retrieval_policy(historical)

    def test_settings_that_cannot_be_read_now_are_retried(self):
        workspace = self.workspace({"retrievalMode": "disabled"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(retrieval_runtime.MODE_ENV, None)
            os.environ.pop(retrieval_runtime.POLICY_ENV, None)
            self.assertEqual(retrieval_runtime.search_mode(workspace), "disabled")
            with mock.patch.object(Path, "read_text", side_effect=OSError(5, "Input/output error")):
                with self.assertRaises(runtime.Error) as failure:
                    runtime.retrieval_policy(workspace)
        self.assertNotIsInstance(failure.exception, runtime.ConfigurationError)
        self.assertTrue(runtime.retryable_failure(failure.exception))



OPTIONS = [{"id": "model-a", "label": "First model"}, {"id": "model-z", "label": "Last model"}]


class ConnectionModelTests(Isolated):
    """I-047, I-080, I-087, I-040 on API connections: one saved key with its discovered models."""

    def connection(self, **values):
        return providers.save_api_connection({"format": "openai", "baseUrl": "https://gateway.example.com/v1",
                                              "apiKey": KEY, "label": "Gateway", **values}, OPTIONS)

    def test_token_limits_belong_to_the_connection_and_reach_its_models_and_the_size_check(self):
        connection = self.connection(contextWindow="400000", maxOutputTokens=64000)
        self.assertEqual((connection["contextWindow"], connection["maxOutputTokens"]), (400000, 64000))
        model = connection["models"][1]["modelId"]
        self.assertEqual(providers.api_model(model)["contextWindow"], 400000)
        # The graph's size check of a submitted proof reads the same field.
        self.assertEqual(runtime.model_context_tokens(model), 400000)
        self.assertEqual(runtime.provider_arguments(model)[:4], ["-c", "model_context_window=400000",
                                                                 "-c", "model_auto_compact_token_limit=268800"])
        # An edit without the fields keeps them; empty fields clear them; a wrong pair is refused.
        kept = providers.save_api_connection({"id": connection["id"], "apiKey": "", "label": "Renamed"}, OPTIONS)
        self.assertEqual((kept["contextWindow"], kept["maxOutputTokens"]), (400000, 64000))
        with self.assertRaisesRegex(providers.ProviderError, "smaller than the context window"):
            providers.save_api_connection({"id": connection["id"], "apiKey": "", "contextWindow": 2000,
                                           "maxOutputTokens": 2000}, OPTIONS)
        cleared = providers.save_api_connection({"id": connection["id"], "apiKey": "", "contextWindow": "",
                                                 "maxOutputTokens": ""}, OPTIONS)
        self.assertEqual((cleared["contextWindow"], cleared["maxOutputTokens"]), (None, None))
        self.assertIsNone(runtime.model_context_tokens(model))
        anthropic = providers.save_api_connection({"format": "anthropic", "baseUrl": "https://gw.example.com",
                                                   "apiKey": KEY, "label": "Claude gateway", "contextWindow": 200000,
                                                   "maxOutputTokens": 32000}, OPTIONS)
        environment = claude_cli.gateway_environment(anthropic["models"][0]["modelId"], KEY)
        self.assertEqual((environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]),
                         ("200000", "32000"))

    def test_an_invalid_connection_is_repaired_in_place_from_its_form(self):
        legacy = self.api_model()
        value = json.loads(self.store.read_text(encoding="utf-8"))
        value["models"][0]["baseUrl"] = "ftp://broken.example.com"
        self.store.write_text(json.dumps(value), encoding="utf-8")
        manager = self.server()
        listing = manager.api_model_listing()
        self.assertEqual((listing["connections"], [entry["id"] for entry in listing["invalid"]]), ([], [legacy["id"]]))
        discovered = {"ok": True, "status": 200, "message": "", "models": [{"id": "gpt-gateway", "label": "gpt-gateway"},
                                                                            {"id": "gpt-new", "label": "gpt-new"}]}
        with mock.patch.object(server, "discover_api_models", return_value=discovered):
            saved = manager.save_api_model({"id": legacy["id"], "format": "openai",
                                            "baseUrl": "https://repaired.example.com/v1", "apiKey": KEY})
        self.assertEqual((saved["connection"]["id"], saved["invalid"]), (legacy["id"], []))
        # A job that named the version-one model id finds it again.
        self.assertEqual((providers.wire_model(legacy["id"]), providers.api_model_secret(legacy["id"])),
                         ("gpt-gateway", KEY))
        self.assertEqual(len(json.loads(self.store.read_text(encoding="utf-8"))["models"]), 1)

    def test_a_connection_whose_model_a_waiting_job_needs_cannot_be_removed(self):
        connection = self.connection()
        model = connection["models"][0]["modelId"]
        manager = self.server()
        app = self.paused_job(authorModel=model)
        manager.jobs[app.run_dir.name] = app
        with self.assertRaisesRegex(ValueError, "Gateway is used by 1 job waiting to start or resume"):
            manager.delete_api_model({"id": connection["id"]})
        self.assertTrue(providers.is_known(model))
        app.state["phase"] = "done"
        self.assertEqual(manager.delete_api_model({"id": connection["id"]})["deleted"], connection["id"])
        self.assertFalse(providers.is_known(model))

    def test_a_later_step_stopped_by_its_endpoint_resumes_once_its_connection_changes(self):
        connection = self.connection()
        model = connection["models"][0]["modelId"]
        problem = runtime.settings_failure(runtime.Error("400 Bad Request: invalid_request_error: bad field"), model)
        self.assertIsInstance(problem, runtime.RequestRejected)
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run("Exact statement")
        app._save_controlled("checked-statement.md", "# Checked statement\n\nExact statement\n")
        app.state.update(phase="running", stage="critic", activeNode="critic", runId=app.run_dir.name,
                         criticModel=model)
        app._save_job_settings(app.state)
        token = app.active_token = app.worker_token = object()
        # A structured step marks a configuration error "no automatic retry" (see _execute).
        process = app.process = mock.Mock(stdout=io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "critic", "node": "critic", "safetyStop": True,
            "state": {"statement": "S", "solution": "P", "paused": True},
            "reason": str(problem) + runtime.SAFETY_STOP_HINT}) + "\n"))
        process.wait.return_value = process.poll.return_value = 1
        with mock.patch.object(server.time, "time", return_value=1_000):
            app._read_output(process, token)
        self.assertEqual((app.state["phase"], app.state["autoResumeAt"]), ("paused", 0))
        self.assertEqual(app.attention()["kind"], "request")
        resumed = []
        self.assertFalse(app.auto_resume_if_due(lambda: resumed.append(1), now=10**9))
        time.sleep(1.1)  # updatedAt has one-second resolution.
        entry = providers.validated_api_connection({}, connection["id"])
        providers.refresh_api_connection(connection["id"], entry, OPTIONS)
        with mock.patch.object(server, "require_model_access"):
            self.assertTrue(app.auto_resume_if_due(lambda: resumed.append(1), now=10**9 + 60))
        self.assertEqual(resumed, [1])
        self.assertFalse(read_checkpoint(app)["safetyStop"])

    def test_a_model_test_on_a_connection_sends_a_run_shaped_request(self):
        connection = self.connection(maxOutputTokens=8000)
        model = connection["models"][1]["modelId"]
        entry, saved = providers.validated_api_model({}, model)
        self.assertEqual((entry["model"], saved["sourceId"]), ("model-z", connection["id"]))
        path, headers, body = server._api_test_request(entry, "high")
        body = json.loads(body)
        self.assertEqual((path, headers["Accept"], headers["Authorization"]),
                         ("/v1/responses", "text/event-stream", f"Bearer {KEY}"))
        self.assertEqual((body["model"], body["stream"], body["reasoning"]["effort"], body["max_output_tokens"]),
                         ("model-z", True, "high", 8000))


def read_checkpoint(app):
    return json.loads((server.controller_directory(app.run_dir) / server.PAUSE_FILENAME).read_text(encoding="utf-8"))


class QueueIntegrationTests(Isolated):
    """I-107 with upstream's selectable stages and the historical holds; I-016 next to historical continuations."""

    def test_a_folder_batch_with_the_review_stage_queues_its_reviews_within_the_limit(self):
        manager = self.server()
        manager.installation = {"maxRunningJobs": 1}
        reviews = []

        def spawn(app, target, args, token):
            app.worker_token = token
            reviews.append(app.state.get("sourceFile"))

        stages = {"review": True, "author": True, "critic": True, "writing": True}
        with mock.patch.object(server.App, "_spawn_worker", spawn):
            started = manager.start_direct_batch({"workflowStages": stages, "statements": [
                {"name": "a.md", "statement": "Statement A"}, {"name": "b.md", "statement": "Statement B"}]})
        self.assertEqual(started.notice, "1 job waits in the queue: at most 1 job runs at once, and the next one "
                                         "starts when one finishes or pauses.")
        first, second = sorted(started, key=lambda app: app.state["sourceFile"])
        self.assertEqual((first.state["phase"], second.state["phase"]), ("reviewing", "prepared"))
        self.assertEqual((second.state["stage"], second.state["launchQueued"]), ("review", True))
        self.assertEqual(reviews, ["a.md"])
        # The queue survives a restart as a statement review.
        restored = server.restore_saved_jobs(self.runs)[second.run_dir.name]
        self.assertEqual((restored.state["phase"], restored.state["stage"], restored.state["launchQueued"]),
                         ("prepared", "review", True))
        # Once the first job leaves its slot, the queued review starts in its own folder.
        first.state["phase"], first.worker_token = "reviewed", None
        with mock.patch.object(server.App, "_spawn_worker", spawn):
            manager._retry_paused_jobs(now=time.time() + 5)
        self.assertEqual((second.state["phase"], second.state.get("launchQueued")), ("reviewing", None))
        self.assertEqual(reviews, ["a.md", "b.md"])
        self.assertEqual(json.loads((second.run_dir / server.REVIEW_INPUT_FILENAME).read_text())["statement"],
                         "Statement B")

    def test_a_running_historical_check_counts_against_the_limit(self):
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run("Exact statement")
        app.state.update(phase="prepared", launchHold=server.HISTORICAL_PENDING_HOLD, runId=app.run_dir.name)
        app._hold_retry_at = 500
        self.assertEqual(app.launch_due_at(1_000), (500, 0))
        self.assertFalse(app.occupies_slot())
        app._hold_check_running = True
        self.assertTrue(app.occupies_slot())
        self.assertEqual(app.launch_due_at(1_000)[0], float("inf"))

    def test_a_stopped_jobs_held_check_still_runs_again_every_minute(self):
        # As its hold says: the check runs again after a manual stop too (only the hold is released then),
        # never for a run that another process owns.
        manager = self.server()
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run("Exact statement")
        app.state.update(phase="done", manuallyStopped=True, runId=app.run_dir.name,
                         retrievalPolicyPath=str(self.root / "policy.json"),
                         launchHold="Historical search preflight failed: busy." + server.HISTORICAL_RETRY_SUFFIX)
        app._hold_retry_at = 500
        manager.jobs[app.run_dir.name] = app
        self.assertEqual(app.launch_due_at(1_000), (500, 0))
        checks = []
        with mock.patch.object(server.App, "retry_historical_hold_if_due",
                               lambda self, now=None: checks.append((self, now)) or False):
            manager._retry_paused_jobs(now=1_000)
        self.assertEqual(checks, [(app, 1_000)])
        app.state["runningElsewhere"] = "another process"
        self.assertEqual(app.launch_due_at(2_000)[0], float("inf"))

    def test_a_settings_check_that_cannot_save_stops_no_other_job(self):
        manager = self.server()
        broken, waiting = self.paused_job(), self.paused_job("Other statement")
        manager.jobs = {broken.run_dir.name: broken, waiting.run_dir.name: waiting}
        broken.state["autoResumeAt"] = 0
        resumed = []
        with mock.patch.object(server.App, "_settings_changed_locked", side_effect=OSError("No space left on device")), \
                mock.patch.object(server.Server, "resume_paused_job", lambda self, run_id: resumed.append(run_id)), \
                mock.patch.object(server, "require_model_access"):
            self.assertEqual(broken.launch_due_at(5_000)[0], float("inf"))
            manager._retry_paused_jobs(now=5_000)
        self.assertEqual(resumed, [waiting.run_dir.name])

    def test_a_failed_review_and_a_historical_run_continue_in_place_by_their_own_paths(self):
        manager = self.server()
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run("Exact statement")
        calls = []
        with mock.patch.object(server.App, "retry_review", lambda self: calls.append("review")), \
                mock.patch.object(server.App, "continue_historical", lambda self, action: calls.append(action)), \
                mock.patch.object(server.Server, "_check_author_workspace_idle"):
            for plan in ({"action": "review", "inPlace": True}, {"action": "author", "inPlace": True}):
                with mock.patch.object(server.Server, "_stopped_continuation_plan", return_value=plan):
                    self.assertIs(manager._continue_stopped_job_locked(app), app)
        self.assertEqual(calls, ["review", "author"])



@unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
class ConnectionManagerPageTests(unittest.TestCase):
    """I-080 and I-047 in the API connection manager: entries to repair, an unreadable store, token limits."""

    def run_app(self, checks):
        browser = provider_ui_tests.ProviderBrowserTests
        payload = json.dumps({"dom": provider_ui_tests.DOM, "slices": browser.MANAGER, "setup": browser.MANAGER_SETUP,
                              "checks": checks})
        result = subprocess.run([NODE, "-e", provider_ui_tests.RUNNER, str(APP)], input=payload,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_entries_that_cannot_be_used_are_repaired_or_removed_and_an_unreadable_store_is_named(self):
        self.run_app(r"""
advanced.open = true; advanced.listeners.toggle({target:advanced});
const broken = {id:'api:broken', ref:'invalid-1-abc', invalid:true, problem:'Enter a valid http(s) base URL.',
  label:'Broken', model:'gpt-old', format:'openai', host:'broken.example', hasKey:true};
sent[0].reply(200, {...listing([connection]), invalid:[broken], storeProblem:'', stateProblem:''});
await settle();
assert.equal($('apiModelCount').textContent, '1 saved · 1 to repair');
assert.match($('apiModelList').textContent, /Broken.*Cannot be used.*Enter a valid http\(s\) base URL\. No job can use it/);
assert.equal($('apiModelsProblem').hidden, true);
// Repair: the form, saved under the entry's id with the key typed again.
click($('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === 'repair'));
assert.equal($('apiModelFormHeading').textContent, 'Repair Broken');
assert.equal($('apiModelKey').required, true);
type('apiModelBaseUrl', 'https://repaired.example/v1'); type('apiModelKey', 'secret-api-key');
$('apiModelSaveButton').onclick();
assert.equal(sent[1].path, '/api-models');
assert.equal(sent[1].body.id, 'api:broken');
const repaired = {...connection, id:'api:broken', label:'Broken'};
sent[1].reply(200, {connection:repaired, ...listing([connection, repaired]), invalid:[]});
await settle();
assert.equal($('apiModelCount').textContent, '2 saved');
// Remove by its reference, never by a saved connection's id.
const nameless = {id:'', ref:'invalid-2-def', invalid:true, problem:'The entry has no valid id (api:<name>).',
  label:'Entry 3', model:'', format:'', host:'', hasKey:false};
advanced.listeners.toggle({target:advanced});
sent[2].reply(200, {...listing([connection]), invalid:[nameless]});
await settle();
click($('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === 'remove'
  && b.dataset.apiModel === nameless.ref));
assert.deepEqual(sent[3].body, {ref:'invalid-2-def'});
sent[3].reply(200, {deleted:nameless.ref, ...listing([connection]), invalid:[]});
await settle();
assert.equal($('apiModelCount').textContent, '1 saved');
// A store that cannot be read is named, never shown as empty.
advanced.listeners.toggle({target:advanced});
sent[4].reply(200, {...listing([]), invalid:[], storeProblem:'The API model store is damaged; fix or remove it.',
  stateProblem:''});
await settle();
assert.equal($('apiModelCount').textContent, 'Unreadable');
assert.equal($('apiModelEmpty').hidden, true);
assert.equal($('apiModelsProblem').hidden, false);
assert.match($('apiModelsProblem').textContent, /damaged/);
""")

    def test_token_limits_are_sent_with_a_connection_and_can_be_cleared(self):
        self.run_app(r"""
type('apiModelBaseUrl', connection.baseUrl); type('apiModelKey', 'secret-api-key');
$('apiModelContextWindow').value = '400000'; $('apiModelMaxOutput').value = '64000';
$('apiModelSaveButton').onclick();
assert.equal(sent[0].body.contextWindow, '400000');
assert.equal(sent[0].body.maxOutputTokens, '64000');
const limited = {...connection, contextWindow:400000, maxOutputTokens:64000};
sent[0].reply(200, {connection:limited, ...listing([limited])});
await settle();
assert.match($('apiModelList').textContent, /400,000-token context · 64,000 max output/);
// Editing shows them; emptied fields clear them.
click($('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === 'edit'));
assert.equal($('apiModelContextWindow').value, '400000');
$('apiModelContextWindow').value = ''; $('apiModelMaxOutput').value = '';
$('apiModelSaveButton').onclick();
assert.equal(sent[1].body.contextWindow, '');
assert.equal(sent[1].body.maxOutputTokens, '');
// A form without limits sends none.
sent[1].reply(200, {connection, ...listing([connection])});
await settle();
type('apiModelBaseUrl', 'https://other.example/v1'); type('apiModelKey', 'secret-api-key');
$('apiModelSaveButton').onclick();
assert.equal('contextWindow' in sent[2].body, false);
""")


if __name__ == "__main__":
    unittest.main()
