"""Opt-in checks of the installed CLIs against local mock endpoints (no model calls).

Run with TCS_PROVER_LIVE_CLI_TESTS=1. The real `claude` and `codex` binaries talk
only to servers on 127.0.0.1 that log every request before parsing it and never
record credential values: the test key is matched, other credentials are only
classified. These pin the provider plumbing against CLI upgrades:
- Anthropic-format API models: --bare plus a private settings file send every
  request to the configured base URL with the key only in x-api-key
  and no subscription credential;
- OpenAI-format API models: Codex sends the key only as Bearer authorization,
  runs with the private CODEX_HOME and leaves the user's Codex home alone.
"""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import workflow_runner as runtime
from runner import providers

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
            model = json.loads(text or "{}").get("model", "mock")
            for name, value in (self.anthropic(model) if self.kind == "anthropic" else self.responses(model)):
                connection.sendall(f"event: {name}\ndata: {json.dumps(value)}\n\n".encode())

    def anthropic(self, model):
        tool = {"type": "tool_use", "id": "toolu_live", "name": "StructuredOutput", "input": {}}
        yield "message_start", {"type": "message_start", "message": {
            "id": "msg_live", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}
        yield "content_block_start", {"type": "content_block_start", "index": 0, "content_block": tool}
        yield "content_block_delta", {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "input_json_delta", "partial_json": json.dumps(self.reply)}}
        yield "content_block_stop", {"type": "content_block_stop", "index": 0}
        yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                                "usage": {"output_tokens": 3}}
        yield "message_stop", {"type": "message_stop"}

    def responses(self, model):
        item = {"type": "message", "id": "msg_live", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(self.reply), "annotations": []}]}
        base = {"id": "resp_live", "object": "response", "created_at": 0, "model": model}
        yield "response.created", {"type": "response.created", "response": {**base, "status": "in_progress", "output": []}}
        yield "response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}
        yield "response.completed", {"type": "response.completed", "response": {
            **base, "status": "completed", "output": [item],
            "usage": {"input_tokens": 5, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 2,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 7}}}


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


if __name__ == "__main__":
    unittest.main()
