"""Opt-in checks of the installed CLIs against local mock endpoints (no model calls).

Run with TCS_PROVER_LIVE_CLI_TESTS=1. The real `claude` and `codex` binaries talk
only to servers on 127.0.0.1 that log every request before parsing it and never
record credential values: the test key is matched, other credentials are only
classified. These pin the provider plumbing against CLI upgrades:
- Anthropic-format API models: a private configuration folder (or --bare when
  the `claude` launcher drops CLAUDE_CONFIG_DIR) plus a private settings file
  send every request to the configured base URL with the key only in x-api-key
  and no subscription credential; the check of that folder gets an answer and
  makes no model request;
- OpenAI-format API models: Codex sends the key only as Bearer authorization,
  runs with the private CODEX_HOME and leaves the user's Codex home alone;
- Codex's legacy sandbox modes (Linux and Windows; Seatbelt enforces them on
  macOS too): the network setting the runner sends reaches the internet in a
  live run only, a conversation turn's read-only one included, with a mock
  model that runs one command per turn.

ConfinementLiveTests makes no model request at all: `codex sandbox` runs a
table of probes in the confined profile of each role (runner/confinement.py),
and an app-server without any thread or turn runs the author's startup check
and shows what the author's commands learn about the user's agents. Its probes
also open a TCP connection to a public host (example.com): every profile of a
live run reaches the internet, and none of a web-cutoff run does. Its
web-cutoff tests (WebCutoffTempLiveTests) do the same with the generated
configuration of the web-cutoff author and critic (retrieval/runtime.py) and
their private temp folders, run the real preflight of each role, and start and
resume a thread of the web-cutoff author without a turn, with a dead-end model
provider, to show what its commands get.
"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval
from runner import claude_cli, confinement, providers

LIVE = os.environ.get("TCS_PROVER_LIVE_CLI_TESTS") == "1"
KEY = "tcs-live-test-key-0123456789"
CREDENTIAL = re.compile(r"(sk-ant-[\w-]+|sk-[\w-]{8,}|eyJ[\w-]{10,}\.[\w-]+)")


class MockEndpoint:
    """A tiny HTTP/1.1 server replying in the Anthropic Messages or OpenAI Responses stream format."""

    def __init__(self, kind, reply):
        self.kind, self.reply, self.requests = kind, reply, []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.server.close()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    @staticmethod
    def classify(value):
        if value in (KEY, f"Bearer {KEY}"):
            return value.replace(KEY, "<KEY>")
        return "<CREDENTIAL>" if CREDENTIAL.search(value) or KEY in value else "<other>"

    def handle(self, connection):
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
            while len(body) < int(headers.get("content-length", 0)):
                body += connection.recv(65536)
            text = body.decode("utf-8", "replace")
            self.requests.append({
                "line": lines[0], "body_has_key": KEY in text, "body_has_credential": bool(CREDENTIAL.search(text)),
                "headers": {name: self.classify(value) if name in {"authorization", "x-api-key", "x-live-key", "cookie"}
                            or KEY in value or CREDENTIAL.search(value) else value for name, value in headers.items()},
            })
            if not lines[0].startswith("POST"):
                connection.sendall(b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
                return
            connection.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n")
            request = json.loads(text or "{}")
            model = request.get("model", "mock")
            for name, value in (self.anthropic(model, request) if self.kind == "anthropic"
                                else self.responses(model, request)):
                connection.sendall(f"event: {name}\ndata: {json.dumps(value)}\n\n".encode())

    def anthropic(self, model, request=None, tool=None):
        tool = tool or {"type": "tool_use", "id": "toolu_live", "name": "StructuredOutput", "input": self.reply}
        yield "message_start", {"type": "message_start", "message": {
            "id": "msg_live", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}
        yield "content_block_start", {"type": "content_block_start", "index": 0,
                                      "content_block": {**tool, "input": {}}}
        yield "content_block_delta", {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool["input"])}}
        yield "content_block_stop", {"type": "content_block_stop", "index": 0}
        yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                                "usage": {"output_tokens": 3}}
        yield "message_stop", {"type": "message_stop"}

    def responses(self, model, request=None, item=None):
        item = item or {"type": "message", "id": "msg_live", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(self.reply), "annotations": []}]}
        base = {"id": "resp_live", "object": "response", "created_at": 0, "model": model}
        yield "response.created", {"type": "response.created", "response": {**base, "status": "in_progress", "output": []}}
        yield "response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}
        yield "response.completed", {"type": "response.completed", "response": {
            **base, "status": "completed", "output": [item],
            "usage": {"input_tokens": 5, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 2,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 7}}}


class ShellEndpoint(MockEndpoint):
    """An Anthropic mock that first calls Bash with ``command``, records its output, then answers ``reply``."""

    def __init__(self, command, reply):
        super().__init__("anthropic", reply)
        self.command, self.outputs = command, []

    def anthropic(self, model, request=None, tool=None):
        # Every message counts: Claude Code may append a system message after a tool result.
        results = [block for message in (request or {}).get("messages") or []
                   if isinstance(message.get("content"), list) for block in message["content"]
                   if isinstance(block, dict) and block.get("type") == "tool_result"
                   and block.get("tool_use_id") == "toolu_shell"]
        if not results:
            return super().anthropic(model, request, {"type": "tool_use", "id": "toolu_shell", "name": "Bash",
                                                      "input": {"command": self.command, "description": "Probe"}})
        content = results[-1].get("content")
        self.outputs.append("\n".join(part.get("text", "") for part in content) if isinstance(content, list)
                            else str(content))
        return super().anthropic(model, request)


class CodexShellEndpoint(MockEndpoint):
    """An OpenAI Responses mock that begins every turn with one exec_command call of ``command``, then answers."""

    def __init__(self, command, reply):
        super().__init__("openai", reply)
        self.command, self.outputs = command, []

    def responses(self, model, request=None, item=None):
        items = (request or {}).get("input") or []
        last = items[-1] if items and isinstance(items[-1], dict) else {}
        if last.get("type") == "function_call_output":
            output = last.get("output")
            self.outputs.append(output if isinstance(output, str) else json.dumps(output))
            return super().responses(model, request)
        return super().responses(model, request, {
            "type": "function_call", "id": "fc_live", "call_id": f"call_live_{os.urandom(4).hex()}",
            "name": "exec_command", "arguments": json.dumps({"cmd": self.command}), "status": "completed"})


@unittest.skipUnless(LIVE, "set TCS_PROVER_LIVE_CLI_TESTS=1 to run the installed CLIs against local mocks")
class LiveCliTests(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / "run").mkdir()
        environment = {name: value for name, value in os.environ.items() if not name.startswith(("ANTHROPIC_", "CLAUDE"))}
        environment.update({providers.STORE_ENV: str(self.root / "state" / "api-models.json"),
                            "TCS_PROVER_RETRIEVAL_MODE": "disabled"})
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        previous = Path.cwd()
        os.chdir(self.root / "run")
        self.addCleanup(os.chdir, previous)
        emitter = mock.patch.object(runtime, "emit")
        self.emit = emitter.start()
        self.addCleanup(emitter.stop)
        (self.root / "tmp").mkdir()
        temp = mock.patch.object(tempfile, "tempdir", str(self.root / "tmp"))
        temp.start()
        self.addCleanup(temp.stop)

    def endpoint(self, kind):
        endpoint = MockEndpoint(kind, {"ok": True})
        self.addCleanup(endpoint.close)
        return endpoint

    def assertNoLeaks(self, endpoint):
        posts = [request for request in endpoint.requests if request["line"].startswith("POST")]
        self.assertTrue(posts)
        for request in endpoint.requests:
            self.assertFalse(request["body_has_key"] or request["body_has_credential"], request["line"])
            self.assertNotIn("<CREDENTIAL>", request["headers"].values(), request)
        self.assertNotIn(KEY, json.dumps([call.kwargs for call in self.emit.call_args_list]))
        return posts

    @unittest.skipUnless(shutil.which("claude"), "Claude CLI is not installed")
    def test_anthropic_gateway_receives_every_request_with_the_key_only_in_its_header(self):
        for header in ("x-api-key", "Authorization", "X-Live-Key"):
            with self.subTest(header=header):
                endpoint = self.endpoint("anthropic")
                model = providers.save_api_model({"format": "anthropic", "baseUrl": f"http://127.0.0.1:{endpoint.port}/v1",
                                                  "headerName": header, "requestSource": "Zephy-TCS-Prover",
                                                  "model": "live-gateway-model", "apiKey": KEY})["id"]
                result, _ = runtime.structured("Return ok.", self.SCHEMA, "review", model=model, effort="low")
                self.assertEqual(result, {"ok": True})
                for request in self.assertNoLeaks(endpoint):
                    self.assertTrue(request["line"].startswith("POST /v1/messages"), request["line"])
                    self.assertEqual(request["headers"]["x-api-key"], "<KEY>")
                    self.assertEqual(request["headers"]["user-agent"], "Zephy-TCS-Prover")
                    self.assertNotIn("authorization", request["headers"])
                    self.assertNotIn("x-live-key", request["headers"])
                # The key-bearing settings folder lived in the private state folder and is gone.
                settings = providers.state_directory() / providers.CLAUDE_SETTINGS
                self.assertEqual(list(settings.iterdir()) if settings.exists() else [], [])
                self.assertEqual(list(Path(tempfile.gettempdir()).glob("tcs-claude-*")), [])

    @unittest.skipUnless(shutil.which("claude") and claude_cli.sandbox_supported(), "Claude's Bash sandbox is needed")
    def test_claude_bash_reaches_the_internet_in_a_live_run_only(self):
        # Exactly the probe the owner asked for; no request reaches a model (the endpoint is the mock).
        command = "curl -sS -o /dev/null -w '%{http_code}' https://example.com"
        home = self.root / "home"
        home.mkdir()
        for mode in ("live", "disabled"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": mode,
                                                                        "HOME": str(home)}):
                endpoint = ShellEndpoint(command, {"ok": True})
                self.addCleanup(endpoint.close)
                model = providers.save_api_model({"format": "anthropic",
                                                  "baseUrl": f"http://127.0.0.1:{endpoint.port}/v1",
                                                  "headerName": "x-api-key", "model": "live-gateway-model",
                                                  "apiKey": KEY})["id"]
                result, _ = runtime.structured("Return ok.", self.SCHEMA, "review", model=model, effort="low",
                                               features=("shell_tool",), attempts=1)
                self.assertEqual(result, {"ok": True})
                print(f"\nClaude Bash, search mode {mode}: {endpoint.outputs[-1]!r}")
                lines = endpoint.outputs[-1].strip().splitlines()
                if mode == "live":
                    self.assertEqual(lines, ["200"])
                else:
                    # 000: the sandbox's proxy refuses the host (CONNECT tunnel failed, response 403).
                    self.assertIn("000", lines)
                    self.assertIn("host is not on the allow list", endpoint.outputs[-1])
                self.assertNoLeaks(endpoint)

    @unittest.skipUnless(sys.platform == "darwin" and runtime._codex_candidates(), "Codex's macOS sandbox is needed")
    def test_a_legacy_read_only_conversation_turn_reaches_the_internet_in_a_live_run_only(self):
        """Elsewhere than macOS a conversation turn runs in Codex's legacy read-only sandbox; its turn sets the network.

        Codex enforces the same legacy modes with Seatbelt on macOS, so the settings the runner sends there
        (legacy_network_arguments(), legacy_read_only_policy()) run here: the author's thread in the
        workspace-write mode, then, in a new app-server, the thread reopened read-only for one conversation
        turn, then the author's thread again. The model is the mock: it calls exec_command once per turn.
        """

        command = ("/usr/bin/nc -z -G 5 example.com 443 >/dev/null 2>&1 && echo net=yes || echo net=no; "
                   "(echo x > written.txt) 2>/dev/null && echo write=yes || echo write=no")
        endpoint = CodexShellEndpoint(command, {"ok": True})
        self.addCleanup(endpoint.close)
        home = self.root / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text(
            f'model_provider = "live_mock"\n[model_providers.live_mock]\nname = "live mock"\n'
            f'base_url = "http://127.0.0.1:{endpoint.port}/v1"\nenv_key = "TCS_LIVE_TEST_KEY"\n'
            'wire_api = "responses"\nrequest_max_retries = 0\nstream_max_retries = 0\n', encoding="utf-8")
        environment = {name: value for name, value in os.environ.items() if name in ("PATH", "USER", "LANG")}
        environment.update(HOME=str(self.root), CODEX_HOME=str(home), TMPDIR=str(self.root / "tmp"))
        runtime._ISOLATED_CODEX.clear()
        # The native binary that honors CODEX_HOME (an initialize handshake, no model).
        codex = runtime.api_codex_executable(environment)
        workspace = self.root / "run"

        def stop(process):
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

        def app_server(live):
            process = subprocess.Popen(
                [codex, "app-server", *runtime.legacy_network_arguments(live)], cwd=str(workspace),
                env={**environment, "TCS_LIVE_TEST_KEY": KEY}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
            self.addCleanup(stop, process)
            rpc = retrieval._ProbeRPC(process)
            rpc.call("initialize", {"clientInfo": {"name": "tcs_prover_live_test", "title": "TCS Prover", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
            rpc.send({"method": "initialized", "params": {}})
            return process, rpc

        def turn(process, rpc, thread, **sandbox):
            seen = len(endpoint.outputs)
            rpc.call("turn/start", {"threadId": thread, "cwd": str(workspace),
                                    "input": [{"type": "text", "text": "Probe."}], **sandbox})
            # The turn ends after the mock's second answer, so its notification follows turn/start's answer.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                line = rpc.messages.get(timeout=max(0.1, deadline - time.monotonic()))
                if line is None or '"turn/completed"' in line:
                    break
            stop(process)
            (workspace / "written.txt").unlink(missing_ok=True)
            self.assertEqual(len(endpoint.outputs), seen + 1, endpoint.outputs)
            return " ".join(re.findall(r"\b(?:net|write)=(?:yes|no)\b", endpoint.outputs[-1]))

        options = {"cwd": str(workspace), "approvalPolicy": "never", "model": "live-gateway-model",
                   "modelProvider": "live_mock"}
        rows = {}
        for live in (True, False):
            process, rpc = app_server(live)
            thread = rpc.call("thread/start", {**options, "sandbox": "workspace-write"})["thread"]["id"]
            rows[live, "author"] = turn(process, rpc, thread)
            process, rpc = app_server(live)
            rpc.call("thread/resume", {**options, "sandbox": "read-only", "threadId": thread, "excludeTurns": True})
            rows[live, "conversation turn"] = turn(process, rpc, thread,
                                                   sandboxPolicy=runtime.legacy_read_only_policy(live))
            process, rpc = app_server(live)
            rpc.call("thread/resume", {**options, "sandbox": "workspace-write", "threadId": thread,
                                       "excludeTurns": True})
            rows[live, "author again"] = turn(process, rpc, thread)
        for (live, role), row in rows.items():
            print(f"\nlegacy sandbox, {'live' if live else '-1'} run, {role}: {row}")
        self.assertEqual(rows, {
            (True, "author"): "net=yes write=yes", (True, "conversation turn"): "net=yes write=no",
            (True, "author again"): "net=yes write=yes",
            (False, "author"): "net=no write=yes", (False, "conversation turn"): "net=no write=no",
            (False, "author again"): "net=no write=yes",
        })
        self.assertNoLeaks(endpoint)

    @unittest.skipUnless(shutil.which("claude"), "Claude CLI is not installed")
    def test_the_private_configuration_check_answers_without_a_model_request(self):
        endpoint = self.endpoint("anthropic")
        model = providers.save_api_model({"format": "anthropic", "baseUrl": f"http://127.0.0.1:{endpoint.port}/v1",
                                          "headerName": "x-api-key", "model": "live-gateway-model", "apiKey": KEY})["id"]
        folder = self.root / "run"
        home = claude_cli.claude_home(folder, runtime)
        environment = claude_cli.child_environment(runtime.environment(model), "low", api=True, config=home)
        answer = claude_cli.keeps_config_directory(claude_cli.executable(runtime), home, environment, model, "low",
                                                   KEY, folder, runtime)
        # True: the launcher passes CLAUDE_CONFIG_DIR on; False: it drops it (API models then run with --bare).
        self.assertIsNotNone(answer, "the CLI did not answer its initialize request")
        self.assertEqual([request for request in endpoint.requests if request["line"].startswith("POST")], [])
        for request in endpoint.requests:
            self.assertFalse(request["body_has_key"] or request["body_has_credential"], request["line"])
            self.assertNotIn("<CREDENTIAL>", request["headers"].values(), request)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_openai_gateway_uses_bearer_auth_request_source_and_a_private_codex_home(self):
        user_home = Path.home() / ".codex"
        sessions_before = set(user_home.glob("sessions/**/*.jsonl"))
        runtime._ISOLATED_CODEX.clear()
        for header in ("Authorization", "X-Live-Key"):
            with self.subTest(header=header):
                endpoint = self.endpoint("openai")
                model = providers.save_api_model({"format": "openai", "baseUrl": f"http://127.0.0.1:{endpoint.port}/v1",
                                                  "headerName": header, "requestSource": "Zephy-TCS-Prover",
                                                  "model": "live-gateway-model", "apiKey": KEY})["id"]
                result, _ = runtime.structured("Return ok.", self.SCHEMA, "review", model=model, effort="low")
                self.assertEqual(result, {"ok": True})
                for request in self.assertNoLeaks(endpoint):
                    self.assertEqual(request["headers"]["authorization"], "Bearer <KEY>")
                    self.assertEqual(request["headers"]["user-agent"], "Zephy-TCS-Prover")
                    self.assertNotIn("chatgpt-account-id", request["headers"])
                    self.assertNotIn("x-live-key", request["headers"])
        # The user's own Codex may be busy; only a rollout of this test would be a leak.
        added = set(user_home.glob("sessions/**/*.jsonl")) - sessions_before
        self.assertEqual([path for path in added if "live-gateway-model" in path.read_text(errors="replace")], [])

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("codex"), "macOS permission profiles need Codex CLI")
    def test_author_profile_keeps_codex_project_folders_read_only_in_a_temp_run_folder(self):
        # A run folder under $TMPDIR is where a derived profile used to let the
        # author plant .codex/config.toml (Codex starts its commands unsandboxed).
        home = self.root / "codex-home"
        home.mkdir()
        store = providers.store_path()
        store.parent.mkdir(parents=True)
        store.write_text("{}", encoding="utf-8")
        workspace = Path(tempfile.gettempdir()) / "author-run"
        workspace.mkdir()
        environment = {"HOME": os.environ["HOME"], "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "CODEX_HOME": str(home), "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        runtime._ISOLATED_CODEX.clear()
        executable = runtime.api_codex_executable(environment)
        profile = runtime.private_permission_arguments(":workspace", "gpt-6.1-sol")
        script = ("for d in .codex .agents .git .aws; do mkdir $d 2>/dev/null && echo made-$d; done; "
                  "echo ok > normal.txt; mkdir -p sub && echo ok > sub/nested.txt; "
                  f"cat {json.dumps(str(store))} >/dev/null 2>&1 && echo read-store; true")
        result = subprocess.run([executable, "sandbox", *profile, "-P", runtime.PRIVATE_PERMISSIONS,
                                 "-C", str(workspace), "--", "/bin/sh", "-c", script],
                                capture_output=True, text=True, env=environment, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.split(), [])
        self.assertEqual([path.name for path in workspace.iterdir() if path.name.startswith(".")], [])
        self.assertEqual((workspace / "sub" / "nested.txt").read_text(), "ok\n")
        self.assertEqual((workspace / "normal.txt").read_text(), "ok\n")


# A public host the probes connect to (port 443): a live run's profiles reach it, a web-cutoff run's do not.
PUBLIC_TCP = "/usr/bin/nc -z -G 5 example.com 443"
PUBLIC_HTTPS = "/usr/bin/curl -sS -o /dev/null --max-time 10 https://example.com"
# A provider no request can reach: nothing here may ever talk to a model. Without plugins an
# app-server does not clone Codex's plugin marketplace either (a git process that would outlive it).
DEAD_END_CONFIG = """model_provider = "dead_end"
model = "no-model"

[model_providers.dead_end]
name = "dead end"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"

[features]
plugins = false
"""
# The same dead end as session flags, for an app-server whose own settings are the audited ones.
DEAD_END_FLAGS = ["-c", 'model_providers.dead_end={name="dead end", base_url="http://127.0.0.1:9/v1", '
                        'wire_api="responses"}']
# A stand-in for the historical search broker: an MCP server that lists the broker's three tools and
# answers nothing else, without any network, so that the web-cutoff author's checks find what they expect.
STUB_BROKER = r'''
import json
import sys

TOOLS = [{"name": name, "description": "stand-in", "inputSchema": {"type": "object", "properties": {}}}
         for name in ("search", "open", "find")]
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if not isinstance(message, dict) or message.get("id") is None:
        continue
    method = message.get("method")
    results = {"tools/list": {"tools": TOOLS}, "resources/list": {"resources": []},
               "resources/templates/list": {"resourceTemplates": []}}
    if method == "initialize":
        result = {"protocolVersion": (message.get("params") or {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "stand-in broker", "version": "1"}}
    elif method in results:
        result = results[method]
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "no"}}),
              flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''


@unittest.skipUnless(LIVE and sys.platform == "darwin", "set TCS_PROVER_LIVE_CLI_TESTS=1 on macOS to run Codex's sandbox")
class ConfinementLiveTests(unittest.TestCase):
    """The generated confined profiles in the installed Codex's sandbox (no model).

    Each role's flags (workflow_runner.private_permission_arguments(), of a
    live run unless a test says otherwise) and environment
    (confinement.temp_environment()) run a probe table through `codex sandbox`
    with a scratch CODEX_HOME; the author's startup check runs through an
    app-server that never starts a thread. The probes connect to example.com
    and to a listener on 127.0.0.1, never to a model provider.
    """

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.home = self.root / "codex-home"
        self.home.mkdir()
        (self.home / "config.toml").write_text(DEAD_END_CONFIG, encoding="utf-8")
        runs = self.root / "runs"
        self.run, self.sibling = runs / "run-A", runs / "run-B"
        self.temp = runs / ".controller" / "run-A" / "tmp"
        for path in (self.run / "related-works", self.run / "prompts", self.run / ".codex", self.sibling,
                     self.temp.parent):
            path.mkdir(parents=True)
        self.temp = confinement.private_folder(self.temp, self.run)
        (self.run / "own.txt").write_text("own")
        (self.run / "job-settings.json").write_text("{}")
        (self.run / "related-works" / "paper.txt").write_text("paper")
        (self.sibling / "other.txt").write_text("other")
        (self.temp.parent / "controller.txt").write_text("controller")
        (self.run / "link-out").symlink_to(self.sibling / "other.txt")
        store = providers.store_path()
        store.parent.mkdir(parents=True, exist_ok=True)
        if not store.exists():
            store.write_text("{}", encoding="utf-8")
        self.store = store
        nonce = os.urandom(6).hex()
        self.shared = {name: Path(folder) / f"tcs-live-{name}-{nonce}" for name, folder in (
            ("slash_tmp", "/private/tmp"), ("var_tmp", "/private/var/tmp"), ("tmpdir", tempfile.gettempdir()))}
        for path in self.shared.values():
            path.write_text("shared")
            self.addCleanup(path.unlink, missing_ok=True)
            self.addCleanup(lambda written=path.with_name(path.name + "-w"): written.unlink(missing_ok=True))
        environment = {name: value for name, value in os.environ.items() if name in ("HOME", "PATH", "USER", "LANG")}
        environment["CODEX_HOME"] = str(self.home)
        runtime._ISOLATED_CODEX.clear()
        # The native binary that honors CODEX_HOME (an initialize handshake, no model).
        self.codex = runtime.api_codex_executable(environment)
        self.environment = {**environment, **confinement.temp_environment(self.temp)}

    def sandbox(self, base, workspace, probes, live=True):
        flags = runtime.private_permission_arguments(base, "gpt-6.1-sol", codex_home=self.home, live=live)
        self.assertTrue(flags)
        lines = [f"if ( {command} ) >/dev/null 2>&1; then echo {key}=yes; else echo {key}=no; fi"
                 for key, (command, _) in probes.items()]
        result = subprocess.run([self.codex, "sandbox", *flags, "-P", runtime.PRIVATE_PERMISSIONS, "-C", str(workspace),
                                 "--", "/bin/sh", "-c", "\n".join(lines)],
                                capture_output=True, text=True, env=self.environment, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        seen = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.endswith(("=yes", "=no")))
        table = {key: (seen.get(key), "yes" if expected else "no") for key, (_, expected) in probes.items()}
        print(f"\n{base} in {workspace.name} ({'live run' if live else 'web cutoff -1'}):")
        for key, (actual, expected) in table.items():
            print(f"  {key:28} {actual:4} {'ok' if actual == expected else 'EXPECTED ' + expected}")
        self.assertEqual({key: actual for key, (actual, _) in table.items()},
                         {key: expected for key, (_, expected) in table.items()})

    def common_probes(self, writable):
        q = shlex.quote
        run, sibling, temp = self.run, self.sibling, self.temp
        return {
            "public_tcp": (PUBLIC_TCP, True),
            "own_read": (f"cat {q(str(run / 'own.txt'))}", True),
            "own_write": (f"echo x > {q(str(run / 'written.txt'))}", writable),
            "related_works_read": (f"cat {q(str(run / 'related-works' / 'paper.txt'))}", True),
            "related_works_write": (f"echo x > {q(str(run / 'related-works' / 'w.txt'))}", False),
            "codex_folder_write": (f"echo x > {q(str(run / '.codex' / 'config.toml'))}", False),
            "agents_folder_create": (f"mkdir {q(str(run / '.agents'))}", False),
            "job_settings_write": (f"echo x >> {q(str(run / 'job-settings.json'))}", False),
            "pause_file_create": (f"echo x > {q(str(run / 'pause.json'))}", False),
            "prompts_remove": (f"rmdir {q(str(run / 'prompts'))}", False),
            "sibling_run_read": (f"cat {q(str(sibling / 'other.txt'))}", False),
            "sibling_run_write": (f"echo x > {q(str(sibling / 'w.txt'))}", False),
            "runs_folder_list": (f"ls {q(str(run.parent))}", False),
            "controller_read": (f"cat {q(str(temp.parent / 'controller.txt'))}", False),
            "home_list": ('ls "$HOME"', False),
            "checkout_read": (f"cat {q(str(Path(runtime.__file__).resolve().with_name('README.md')))}", False),
            "private_state_read": (f"cat {q(str(self.store))}", False),
            "slash_tmp_read": (f"cat {q(str(self.shared['slash_tmp']))}", False),
            "slash_tmp_write": (f"echo x > {q(str(self.shared['slash_tmp']) + '-w')}", False),
            "var_tmp_read": (f"cat {q(str(self.shared['var_tmp']))}", False),
            "var_tmp_write": (f"echo x > {q(str(self.shared['var_tmp']) + '-w')}", False),
            "shared_tmpdir_read": (f"cat {q(str(self.shared['tmpdir']))}", False),
            "shared_tmpdir_write": (f"echo x > {q(str(self.shared['tmpdir']) + '-w')}", False),
            "private_tmp_write": (f'echo x > "$TMPDIR/w.txt" && test -s {q(str(temp / "w.txt"))}', True),
            "tmpdir_is_private": (f'test "$TMPDIR" = {q(str(temp))}', True),
            "symlink_escape": (f"cat {q(str(run / 'link-out'))}", False),
            "new_symlink_escape": (f"ln -s {q(str(sibling / 'other.txt'))} \"$TMPDIR/l\" && cat \"$TMPDIR/l\"", False),
            "hardlink_escape": (f"ln {q(str(sibling / 'other.txt'))} \"$TMPDIR/h\"", False),
        }

    def test_the_author_reads_and_writes_only_its_folder_and_temp_folder_and_its_tools_work(self):
        q = shlex.quote
        probes = self.common_probes(writable=True)
        tex = ("mkdir -p tex && cd tex && printf '%s\\n' '\\documentclass{article}' '\\begin{document}' "
               "'Hello sandbox' '\\end{document}' > t.tex && latexmk -pdf -interaction=nonstopmode t.tex "
               "&& pdftotext t.pdf - | grep -q 'Hello sandbox'")
        patch = "*** Begin Patch\n*** Add File: patched.txt\n+patched\n*** End Patch\n"
        heredoc = "cat <<EOT > heredoc.txt\nheredoc-ok\nEOT\n"
        tools = {
            "python3": ("python3 -c 'import tempfile; tempfile.NamedTemporaryFile()'", "python3"),
            "pdftotext": ("pdftotext -v", "pdftotext"),
            "latexmk_pdf": (f"cd {q(str(self.run))} && {tex}", "latexmk"),
            "git": (f"cd {q(str(self.run))} && git init -q repo && cd repo && echo x > f && git add f "
                    "&& git -c user.email=a@b -c user.name=n commit -qm m", "git"),
            "rg": (f"rg -q own {q(str(self.run / 'own.txt'))}", None),
            "apply_patch": (f"cd {q(str(self.run))} && apply_patch {q(patch)} && test \"$(cat patched.txt)\" = patched",
                            None),
            "zsh_heredoc": (f"cd {q(str(self.run))} && printf %s {q(heredoc)} > h.zsh && /bin/zsh h.zsh "
                            "&& test \"$(cat heredoc.txt)\" = heredoc-ok", None),
        }
        for key, (command, program) in tools.items():
            if program is None or shutil.which(program, path=self.environment.get("PATH")):
                probes[key] = (command, True)
        self.sandbox(":workspace", self.run, probes)

    def test_a_conversation_turn_only_reads_its_folder(self):
        self.sandbox(":read-only", self.run, self.common_probes(writable=False))

    def test_structured_calls_read_or_edit_only_their_call_folder(self):
        q = shlex.quote
        for writable in (False, True):
            snapshot = confinement.fresh_folder("tcs-live-call-", self.root)
            (snapshot / "proof.md").write_text("proof")
            (snapshot / "proof.md").chmod(0o600 if writable else 0o400)
            probes = {
                "snapshot_read": (f"cat {q(str(snapshot / 'proof.md'))}", True),
                "snapshot_edit": (f"echo x >> {q(str(snapshot / 'proof.md'))}", writable),
                "run_folder_read": (f"cat {q(str(self.run / 'own.txt'))}", False),
                "sibling_run_read": (f"cat {q(str(self.sibling / 'other.txt'))}", False),
                "shared_tmpdir_read": (f"cat {q(str(self.shared['tmpdir']))}", False),
                "slash_tmp_write": (f"echo x > {q(str(self.shared['slash_tmp']) + '-w')}", False),
                "private_tmp_write": ('echo x > "$TMPDIR/s.txt"', True),
                "public_tcp": (PUBLIC_TCP, True),
            }
            with self.subTest(writable=writable):
                self.sandbox(":workspace" if writable else ":read-only", snapshot, probes)

    def test_only_a_live_run_reaches_the_network_and_then_local_services_too(self):
        # A service on this Mac, as the README warns a live run's commands can reach.
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        local = f"/usr/bin/nc -z -G 3 127.0.0.1 {server.getsockname()[1]}"
        snapshot = confinement.fresh_folder("tcs-live-call-", self.root)
        for live in (True, False):
            probes = {"public_tcp": (PUBLIC_TCP, live), "public_https": (PUBLIC_HTTPS, live),
                      "localhost_tcp": (local, live)}
            for base, workspace in ((":workspace", self.run), (":read-only", self.run), (":read-only", snapshot)):
                with self.subTest(live=live, base=base, workspace=workspace.name):
                    self.sandbox(base, workspace, probes, live=live)

    def app_server(self, argv, environment):
        """An app-server (initialize only: no thread, no turn, no model) and its RPC client."""

        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", bufsize=1, cwd=str(self.run), env=environment)

        def stop():
            runtime.stop_process(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()

        self.addCleanup(stop)
        rpc = runtime.RPC(process, lambda message: None)
        rpc.call("initialize", {"clientInfo": {"name": "tcs_prover_live_test", "title": "TCS Prover", "version": "1"},
                                "capabilities": {"experimentalApi": True}})
        rpc.send({"method": "initialized", "params": {}})
        return rpc

    def test_the_authors_commands_are_not_told_where_the_agents_listen(self):
        # A socket outside the sandbox, as the user's SSH agent listens in a folder of /tmp.
        folder = self.root / "agent-folder"
        folder.mkdir()
        agent_socket = folder / "agent.sock"
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(agent_socket))
        server.listen(4)
        agents = {"SSH_AUTH_SOCK": str(agent_socket), "SSH_AGENT_PID": str(os.getpid()),
                  "GPG_AGENT_INFO": f"{agent_socket}:0:1"}
        with mock.patch.object(runtime, "codex", return_value=self.codex), \
                mock.patch.object(runtime, "environment", return_value={**self.environment, **agents}):
            sandbox = runtime._author_sandbox(runtime, {"run_tmp_dir": str(self.temp)}, directory=str(self.run),
                                              policy=None, model="gpt-6.1-sol")
            argv, environment, _ = runtime._author_app_server(
                runtime, directory=str(self.run), policy=None, role="author", model="gpt-6.1-sol",
                settings={"speed": "standard"}, features=("multi_agent",), classic=False, sandbox=sandbox)
        self.assertFalse(set(agents) & set(environment))
        # A live run's author (the run folder's saved settings name no other mode) reaches the network.
        self.assertIn(f'permissions.{runtime.PRIVATE_PERMISSIONS}.network={{"enabled"=true}}', argv)
        rpc = self.app_server(argv, {**environment, "CODEX_HOME": str(self.home)})
        q = shlex.quote
        connect = ("/usr/bin/python3 -c \"import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(3); "
                   f"s.connect(sys.argv[1])\" {q(str(agent_socket))}")
        probes = {
            "variables": ('test -n "$SSH_AUTH_SOCK$SSH_AGENT_PID$GPG_AGENT_INFO"', False),
            "launchctl_getenv": ('test -n "$(/bin/launchctl getenv SSH_AUTH_SOCK)"', False),
            "socket_folder_list": (f"/bin/ls {q(str(folder))}", False),
            # What the README says remains: a socket whose path a command finds, with lsof for one.
            "lsof_lists_the_socket": (f"/usr/sbin/lsof -U | /usr/bin/grep -qF {q(str(agent_socket))}", True),
            "socket_connect_by_path": (connect, True),
            "public_tcp": (PUBLIC_TCP, True),
        }
        script = "\n".join(f"if ( {command} ) >/dev/null 2>&1; then echo {key}=yes; else echo {key}=no; fi"
                           for key, (command, _) in probes.items())
        result = rpc.call("command/exec", {"command": ["/bin/sh", "-c", script], "cwd": str(self.run),
                                           "permissionProfile": runtime.PRIVATE_PERMISSIONS, "timeoutMs": 20000})
        seen = dict(line.split("=", 1) for line in (result.get("stdout") or "").splitlines() if "=" in line)
        print("\nlive author (with network access), agents in the runner's environment:")
        for key, (_, expected) in probes.items():
            print(f"  {key:28} {seen.get(key)}")
        self.assertEqual(seen, {key: "yes" if expected else "no" for key, (_, expected) in probes.items()})

    def test_the_startup_check_passes_in_the_profile_and_fails_in_a_wider_one(self):
        def app_server(flags):
            process = subprocess.Popen([self.codex, "app-server", *flags], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                       bufsize=1, cwd=str(self.run), env=self.environment)

            def stop():
                runtime.stop_process(process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    with contextlib.suppress(OSError):
                        stream.close()

            self.addCleanup(stop)
            rpc = runtime.RPC(process, lambda message: None)
            rpc.call("initialize", {"clientInfo": {"name": "tcs_prover_live_test", "title": "TCS Prover", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
            rpc.send({"method": "initialized", "params": {}})
            return rpc

        events = []
        emit = lambda kind, **fields: events.append((kind, fields))
        # The check of a live run's profile (with network) and of a web-cutoff run's (without): it never needs it.
        for live in (True, False):
            flags = runtime.private_permission_arguments(":workspace", "gpt-6.1-sol", codex_home=self.home, live=live)
            runtime.author_confinement_check(app_server(flags), sandbox={"base": ":workspace", "temp": self.temp},
                                             directory=str(self.run), emit=emit)
            print(f"\nstartup check ({'live run' if live else 'web cutoff -1'}):", events[-1][1]["text"])
            self.assertEqual(events[-1][1]["label"], "Sandbox checked")
            read_only = runtime.private_permission_arguments(":read-only", "gpt-6.1-sol", codex_home=self.home,
                                                             live=live)
            runtime.author_confinement_check(app_server(read_only), sandbox={"base": ":read-only", "temp": self.temp},
                                             directory=str(self.run), emit=emit)
        # A profile that also lets commands read the runs folder must not pass.
        wider = runtime.private_permissions(":workspace", "gpt-6.1-sol", codex_home=self.home)
        wider["filesystem"][str(self.run.parent.parent)] = "read"
        with self.assertRaisesRegex(runtime.Error, "did not hold") as failed:
            runtime.author_confinement_check(app_server(runtime.private_permission_arguments(
                ":workspace", "gpt-6.1-sol", profile=wider)), sandbox={"base": ":workspace", "temp": self.temp},
                directory=str(self.run), emit=emit)
        print("wider profile:", failed.exception)
        self.assertEqual(sorted(path.name for path in self.run.iterdir()),
                         [".codex", "job-settings.json", "link-out", "own.txt", "prompts", "related-works"])


@unittest.skipUnless(LIVE and sys.platform == "darwin", "set TCS_PROVER_LIVE_CLI_TESTS=1 on macOS to run Codex's sandbox")
class WebCutoffTempLiveTests(unittest.TestCase):
    """The web-cutoff author's and critic's private temp folders in the installed Codex's sandbox (no model).

    The generated configuration (retrieval.config_text()) goes into a scratch
    Codex home; each role gets the private temp folder, environment and
    session flags that preflight() gives its process. `codex sandbox` runs a
    probe table in zsh, the shell Codex runs a model's commands in.
    preflight() itself runs the real checks (verify_rpc()) with a stand-in
    broker (STUB_BROKER); only the controller's own handshake with 1.1.1.1
    is left out. The author's app-server, built as goal_session() builds it
    but with a dead-end model provider, starts and resumes a thread without
    a turn to show what that thread's commands get.
    """

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.control, self.run = self.root / "control", self.root / "run"
        for path in (self.control, self.run):
            path.mkdir()
        broker, policy_file = self.control / "stub_broker.py", self.control / "retrieval-policy.json"
        broker.write_text(STUB_BROKER, encoding="utf-8")
        policy_file.write_text('{"live-test": true}\n', encoding="utf-8")
        (self.control / "broker-policy.json").write_text('{"provider": "stand-in"}\n', encoding="utf-8")
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()  # noqa: E731
        # What load_policy() makes of a prepared policy, with the stand-in broker.
        self.policy = {"schemaVersion": 1, "mode": "historical", "runId": "live",
                       "cutoffExclusiveUtc": "2024-01-01T00:00:00Z",
                       "isolation": {"adapter": retrieval.MACOS_ADAPTER, "workspace": str(self.run),
                                     "codexHome": str(self.control / "codex-home")},
                       "broker": {"command": sys.executable, "args": [str(broker)],
                                  "policyFile": str(self.control / "broker-policy.json"),
                                  "auditLog": str(self.control / "search-audit.jsonl")},
                       "_path": str(policy_file), "_sha256": digest(policy_file),
                       "_brokerScriptSha256": digest(broker), "_brokerModuleSha256": {}}
        self.temps = self.control / "codex-home" / retrieval.PRIVATE_TEMP_FOLDER
        (self.run / "job-settings.json").write_text(json.dumps({"retrievalPolicyPath": self.policy["_path"]}))
        (self.control / "controller.txt").write_text("controller")
        # A critic session's home and snapshot, as critic_session() makes them.
        session = self.control / "codex-home" / "critic-sessions" / "session-live"
        critic = mock.patch.object(retrieval, "_critic_paths", return_value=(session / "home", session / "workspace"))
        critic.start()
        self.addCleanup(critic.stop)
        # No Codex login is copied into the isolated homes (isolated_environment() would copy the user's).
        (self.root / "no-login").mkdir()
        login = mock.patch.dict(os.environ, {retrieval.LOGIN_HOME_ENV: str(self.root / "no-login")})
        login.start()
        self.addCleanup(login.stop)
        nonce = os.urandom(6).hex()
        self.shared = {name: Path(folder) / f"tcs-live-webcut-{name}-{nonce}"
                       for name, folder in (("slash_tmp", "/private/tmp"), ("var_tmp", "/private/var/tmp"))}
        for path in self.shared.values():
            path.write_text("shared")
            self.addCleanup(path.unlink, missing_ok=True)
            self.addCleanup(lambda written=path.with_name(path.name + "-w"): written.unlink(missing_ok=True))
        environment = {name: value for name, value in os.environ.items() if name in ("HOME", "PATH", "USER", "LANG")}
        probe_home = self.root / "probe-home"
        probe_home.mkdir()
        (probe_home / "config.toml").write_text(DEAD_END_CONFIG, encoding="utf-8")
        runtime._ISOLATED_CODEX.clear()
        # The native binary that honors CODEX_HOME (an initialize handshake, no model).
        self.codex = runtime.api_codex_executable({**environment, "CODEX_HOME": str(probe_home)})

    def process(self, role):
        """The private temp folder and environment of one web-cutoff process of ``role`` (as preflight() makes them)."""

        retrieval.role_workspace(self.policy, role).mkdir(parents=True, exist_ok=True)
        temp = retrieval.private_temp(self.policy, role)
        environment = retrieval.isolated_environment(self.policy, role, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                                                     temp=temp)
        self.addCleanup(retrieval.remove_private_temp, self.policy, environment)
        self.assertFalse((retrieval.role_home(self.policy, role) / "auth.json").exists())
        # And no request could reach a model provider: its endpoint is a dead end.
        return temp, {**environment, "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}

    def test_heredocs_and_temp_folders_of_the_author_and_the_critic(self):
        q = shlex.quote
        heredoc = "cat <<'EOT'\nheredoc-ok\nEOT"
        for role, writable in (("author", True), ("critic", False)):
            temp, environment = self.process(role)
            probes = {
                "zsh_heredoc": (heredoc, True),
                # macOS's bash 3.2 ignores TMPDIR for heredocs: /var/tmp, /tmp, then the current folder.
                "sh_heredoc": (f"/bin/sh -c {q(heredoc)}", writable),
                "bash_heredoc": (f"/bin/bash -c {q(heredoc)}", writable),
                "bash_heredoc_in_tmpdir": (f'cd "$TMPDIR" && /bin/bash -c {q(heredoc)}', True),
                "tmpdir_is_private": (f'test "$TMPDIR" = {q(str(temp))} && test "$TMPPREFIX" = {q(str(temp / "zsh"))}',
                                      True),
                "private_tmp_write": ('echo x > "$TMPDIR/w" && test -s "$TMPDIR/w"', True),
                "mktemp_in_tmpdir": ('f=$(mktemp -p "$TMPDIR") && test -f "$f"', True),
                "temp_parent_list": (f"ls {q(str(temp.parent))}", False),
                "slash_tmp_read": (f"cat {q(str(self.shared['slash_tmp']))}", False),
                "slash_tmp_write": (f"echo x > {q(str(self.shared['slash_tmp']) + '-w')}", False),
                "var_tmp_read": (f"cat {q(str(self.shared['var_tmp']))}", False),
                "var_tmp_write": (f"echo x > {q(str(self.shared['var_tmp']) + '-w')}", False),
                "controller_read": (f"cat {q(str(self.control / 'controller.txt'))}", False),
                "own_write": (f"echo x > {q(str(retrieval.role_workspace(self.policy, role) / 'w.txt'))}", writable),
                # No software folder is granted: the shell has the system's basic commands only.
                "python3": ("/usr/bin/python3 -c pass", False),
                # And no network, as in every web-cutoff process.
                "public_tcp": (PUBLIC_TCP, False),
                "public_https": (PUBLIC_HTTPS, False),
            }
            # Each probe on lines of its own, so that a heredoc's terminator stands alone.
            script = "\n".join(f"if (\n{command}\n) >/dev/null 2>&1; then echo {key}=yes; else echo {key}=no; fi"
                               for key, (command, _) in probes.items())
            result = subprocess.run([self.codex, "sandbox", *retrieval.temp_arguments(self.policy, environment),
                                     "-P", retrieval.PROFILE, "-C", str(retrieval.role_workspace(self.policy, role)),
                                     "--", "/bin/zsh", "-c", script],
                                    capture_output=True, text=True, env=environment, timeout=600)
            with self.subTest(role=role):
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                seen = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.endswith(("=yes", "=no")))
                print(f"\nweb-cutoff {role} in {retrieval.role_workspace(self.policy, role).name}, temp {temp.name}:")
                for key, (_, expected) in probes.items():
                    actual = seen.get(key)
                    print(f"  {key:24} {actual:4} {'ok' if actual == ('yes' if expected else 'no') else 'EXPECTED ' + ('yes' if expected else 'no')}")
                self.assertEqual(seen, {key: "yes" if expected else "no" for key, (_, expected) in probes.items()})

    def preflight(self, role, agent_limits=None):
        """retrieval.preflight() as a run calls it before each process of ``role`` (its environment, removed after),
        with the subagent limits of that process (runtime.retrieval_environment())."""

        for attempt in range(2):
            try:
                with mock.patch.object(retrieval, "_public_network_control"):
                    environment = retrieval.preflight(self.policy, role, self.codex,
                                                      {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                                                      **({"agent_limits": agent_limits} if agent_limits else {}))
            except retrieval.IsolationCheckUnavailable as exc:
                # A skill that this Codex bundles and the generated list does not name yet is off from the next start.
                if attempt or "bundled skill" not in str(exc):
                    raise
                continue
            self.addCleanup(retrieval.remove_private_temp, self.policy, environment)
            self.assertFalse((retrieval.role_home(self.policy, role) / "auth.json").exists())
            return environment

    def stopped_with_the_test(self, process):
        def stop():
            runtime.stop_process(process)
            for stream in (process.stdin, process.stdout):
                with contextlib.suppress(OSError):
                    stream.close()

        self.addCleanup(stop)
        return stop

    def test_the_sandbox_checks_pass_with_the_private_temp_folder_and_find_a_wider_grant(self):
        temps = Path(os.path.realpath(self.temps))
        for role in ("author", "critic", "structured"):
            with self.subTest(role=role):
                # Every check of the run's preflight: the folder that verify_rpc() finds in TMPDIR, and its canaries.
                environment = self.preflight(role)
                temp = Path(environment["TMPDIR"])
                self.assertEqual(temp.parent, temps)
                self.assertTrue(temp.name.startswith(f"{role}-{os.getpid()}-"))
                print(f"\nweb-cutoff {role}: preflight passes with its private temp folder {temp.name}")
                # A process given the folder that holds every process's temp folder is stopped by the folder check,
                with mock.patch.object(retrieval, "private_temp", return_value=temps), \
                        self.assertRaisesRegex(retrieval.RetrievalPolicyError, "own private temp folder") as caught:
                    self.preflight(role)
                self.assertNotIsInstance(caught.exception, retrieval.IsolationCheckUnavailable)
                print(f"web-cutoff {role}, TMPDIR {temps.name}/: {caught.exception}")
                # and without it by the sandbox checks: their controller file in that folder is then readable.
                with mock.patch.object(retrieval, "private_temp", return_value=temps), \
                        mock.patch.object(retrieval, "_verified_private_temp",
                                          side_effect=lambda policy, role, environment: Path(environment["TMPDIR"])), \
                        self.assertRaisesRegex(retrieval.RetrievalPolicyError, "private temp folders") as caught:
                    self.preflight(role)
                self.assertNotIsInstance(caught.exception, retrieval.IsolationCheckUnavailable)
                print(f"web-cutoff {role}, TMPDIR {temps.name}/ without the folder check: {caught.exception}")
                # Neither failed check removed that folder or left a file of its own in it.
                self.assertEqual(sorted(path.name for path in temps.iterdir()), [temp.name])
                retrieval.remove_private_temp(self.policy, environment)

    def test_the_authors_thread_and_its_replies_give_their_commands_the_private_temp_folder(self):
        """A thread's shell environment replaces the app-server's: its own settings name the folder too."""

        model, settings = "gpt-6.1-sol", {"speed": "standard", "effort": "low"}
        config = retrieval.role_home(self.policy, "author") / "config.toml"
        # An author without subagents gets a session of one thread, which its checks verify (subagent_limits()).
        limits = runtime.subagent_limits(("shell_tool",), settings.get("subagents"))

        def author():
            # As goal_session() and author_conversation() start the author: preflight, app-server, handshake check.
            environment = self.preflight("author", limits)
            with mock.patch.object(retrieval, "codex_executable", return_value=self.codex), \
                    mock.patch.object(runtime, "retrieval_environment", return_value=environment):
                argv, child, _ = runtime._author_app_server(
                    runtime, directory=str(self.run), policy=self.policy, role="author", model=model,
                    settings=settings, features=("shell_tool",), classic=False)
            process = subprocess.Popen([*argv, *DEAD_END_FLAGS], cwd=str(self.run), env=child, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                       bufsize=1)
            stop = self.stopped_with_the_test(process)
            rpc = retrieval._ProbeRPC(process)
            with mock.patch.object(retrieval, "_public_network_control"):
                runtime._initialize_author_rpc(runtime, rpc, policy=self.policy, role="author",
                                               child_environment=child, api_model=False, agent_limits=limits)
            options = runtime._author_thread_options(
                runtime, rpc, directory=str(self.run), policy=self.policy, role="author", model=model,
                settings=settings, summary="auto", features=("shell_tool",), classic=False, emit=None,
                environment=child)
            # No request can reach a model provider, not even the connection Codex opens when a thread starts.
            options["modelProvider"] = "dead_end"
            return rpc, options, Path(child["TMPDIR"]), stop

        def commands_get(rpc, thread):
            # `env` as a command of the thread (no turn): TMPDIR and TMPPREFIX as its commands get them.
            output = self.run / f"thread-env-{os.urandom(4).hex()}.txt"
            rpc.call("thread/shellCommand", {"threadId": thread, "command": f"/usr/bin/env > {shlex.quote(str(output))}",
                                             "timeoutMs": 10000})
            for _ in range(100):
                if output.exists() and output.stat().st_size:
                    break
                time.sleep(0.1)
            time.sleep(0.5)
            lines = output.read_text().splitlines() if output.exists() else []
            output.unlink(missing_ok=True)
            return {name: value for name, _, value in (line.partition("=") for line in lines)
                    if name in ("TMPDIR", "TMPPREFIX")}

        rpc, options, temp, stop = author()
        thread = rpc.call("thread/start", {**options, "ephemeral": False})["thread"]["id"]
        seen = commands_get(rpc, thread)
        print(f"\nweb-cutoff author, thread/start: {seen}")
        self.assertEqual(seen, {"TMPDIR": str(temp), "TMPPREFIX": str(temp / "zsh")})
        stop()
        # Codex records its trust in the run's project (if it does); without ":tmpdir" as well, as an earlier
        # TCS Prover wrote the config, the reply's start migrates it with an audit entry instead of holding the run.
        trusted = "[projects." in config.read_text()
        config.write_text(config.read_text().replace('":tmpdir" = "write"\n', ""))
        rpc, options, second, stop = author()
        migrated = [json.loads(line) for line in Path(self.policy["broker"]["auditLog"]).read_text().splitlines()
                    if json.loads(line)["event"] == "isolated_config_migrated"]
        print(f"web-cutoff author, Codex's trust record kept: {trusted}; migration: {migrated[-1]['changed_keys']}")
        self.assertIn("permissions.historical.filesystem.:tmpdir", migrated[-1]["changed_keys"])
        self.assertEqual("projects" in migrated[-1]["changed_keys"], trusted)
        self.assertEqual(config.read_text(), retrieval.config_text(self.policy, "author"))
        # A reply resumes the thread in a new app-server with a new folder.
        rpc.call("thread/resume", {**options, "threadId": thread, "excludeTurns": True})
        seen = commands_get(rpc, thread)
        print(f"web-cutoff author, thread/resume in a new app-server: {seen}")
        self.assertNotEqual(second, temp)
        self.assertEqual(seen, {"TMPDIR": str(second), "TMPPREFIX": str(second / "zsh")})
        stop()

    def test_a_web_cutoff_author_with_subagents_passes_its_checks_with_exactly_its_limits(self):
        """The Author subagents setting in a web-cutoff run: its limits are session flags that the checks verify."""

        model, settings = "gpt-6.1-sol", {"speed": "standard", "effort": "low", "subagents": 2}
        features = ("shell_tool", "multi_agent")
        limits = runtime.subagent_limits(features, settings["subagents"])
        environment = self.preflight("author", limits)
        with mock.patch.object(retrieval, "codex_executable", return_value=self.codex), \
                mock.patch.object(runtime, "retrieval_environment", return_value=environment):
            argv, child, _ = runtime._author_app_server(
                runtime, directory=str(self.run), policy=self.policy, role="author", model=model,
                settings=settings, features=features, classic=False)
        print(f"\nweb-cutoff author with 2 subagents: {[part for part in argv if 'agent' in part]}")

        def handshake(expected):
            process = subprocess.Popen([*argv, *DEAD_END_FLAGS], cwd=str(self.run), env=child,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, encoding="utf-8", bufsize=1)
            stop = self.stopped_with_the_test(process)
            try:
                with mock.patch.object(retrieval, "_public_network_control"):
                    runtime._initialize_author_rpc(runtime, retrieval._ProbeRPC(process), policy=self.policy,
                                                   role="author", child_environment=child, api_model=False,
                                                   agent_limits=expected)
            finally:
                stop()

        handshake(limits)
        print("web-cutoff author with 2 subagents: its checks pass with exactly its limits")
        # Any other limit is refused: the audit compares the session's settings with exactly these.
        for other in (runtime.subagent_limits(features, 3), None):
            with self.subTest(other=other), self.assertRaisesRegex(retrieval.RetrievalPolicyError, "Historical isolation (mismatch|rejects)"):
                handshake(other)
        print("web-cutoff author with 2 subagents: other limits are refused")


if __name__ == "__main__":
    unittest.main()
