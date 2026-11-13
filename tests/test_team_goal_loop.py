"""TCS Prover's goal loop (Goal loop "TCS Prover"), its MCP server "tcs" and the author coordinator's team.

Offline: the Codex author or coordinator runs against the scripted RPC double of tests/test_goal_runtime.py, the
Claude coordinator against the stand-in CLI of tests/test_claude_cli.py, and the tests play the CLI's part as an MCP
client: they start the real bridge (runner/tcs_mcp.py) exactly as the CLI's settings name it and call its tools.
Team members run stand-in `codex` and `claude` programs (no model). No real CLI is started.
"""

import ast
import contextlib
import json
import os
from pathlib import Path
import re
import stat
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
from runner import claude_cli, confinement, providers, tcs_mcp, team

import test_author_coordinator as coordinator_test
import test_claude_cli as claude_test
import test_goal_runtime as goal_test

ROOT = Path(runtime.__file__).parent
# The real process start: goal-session tests replace subprocess.Popen for the author's CLI.
POPEN = subprocess.Popen
WORKFLOW = ROOT / "workflows/author_critic.yaml"
POINTER = "PROOF_FILE: proof.md"
TEXTS = {name: text for name, text in runtime.load_workflow(WORKFLOW)["prompts"].items()
         if name in runtime.HARNESS_TEXTS}

# A stand-in `codex` for a team member's `codex exec --json`: it logs its call, reports a saved session and answers
# from its prompt; like Codex 0.160, each run numbers its items from item_0 again. Markers in the prompt choose a
# failure: SLOW (works until stopped), FAIL-ONCE (a model-call failure after some work, once per run folder), NOLOGIN
# (a login the CLI refuses).
FAKE_CODEX = r'''
import json, os, sys, time, uuid
argv = sys.argv[1:]
prompt = sys.stdin.read()


def log(record):
    with open(os.environ["TCS_FAKE_CODEX_LOG"], "a") as stream:
        stream.write(json.dumps(record) + "\n")


def out(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


model = argv[argv.index("-m") + 1] if "-m" in argv else ""
fork = argv[argv.index("-") - 1] if "fork" in argv else None
log({"argv": argv, "prompt": prompt, "cwd": os.getcwd(), "fork": fork,
     "env": {key: value for key, value in os.environ.items()
             if key.startswith(("CODEX", "TCS", "OPENAI", "SSH_", "GPG_")) or key == "TMPDIR"}})
session = str(uuid.uuid4())
out({"type": "thread.started", "thread_id": session})
out({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning",
                                        "text": "thinking about " + prompt[:40]}})
if "SLOW" in prompt:
    time.sleep(120)
flag = os.path.join(os.environ["TCS_FAKE_CODEX_STATE"], "failed-once")
if "FAIL-ONCE" in prompt and not os.path.exists(flag):
    open(flag, "w").write("1")
    out({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
    sys.exit(1)
if "NOLOGIN" in prompt:
    sys.stderr.write("Error: 401 Unauthorized: not logged in\n")
    sys.exit(1)
first = next((line for line in prompt.splitlines() if line.strip()), "")
reply = f"Report of {model}" + (f" (continuing {fork})" if fork else "") + f": {first}"
out({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": reply}})
out({"type": "turn.completed", "usage": {"input_tokens": 1}})
answer = argv[argv.index("-o") + 1]
open(answer, "w").write(reply)
'''


class MCPClient:
    """The CLI's side of the "tcs" server: starts the bridge as the CLI's settings say and calls its tools."""

    def __init__(self, entry):
        environment = {**{key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "SYSTEMROOT")},
                       **entry["env"]}
        self.process = POPEN([entry["command"], *entry["args"]], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, env=environment)
        self.number, self.lock, self.answers = 0, threading.Lock(), {}
        self.arrived = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for line in self.process.stdout:
            message = json.loads(line)
            with self.arrived:
                self.answers[message.get("id")] = message
                self.arrived.notify_all()

    def send(self, message):
        with self.lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def start(self, method, params):
        with self.lock:
            self.number += 1
            number = self.number
        self.send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        return number

    def answer(self, number, timeout=30):
        with self.arrived:
            if not self.arrived.wait_for(lambda: number in self.answers, timeout):
                raise AssertionError(f"no answer to request {number}")
            return self.answers.pop(number)

    def request(self, method, params, timeout=30):
        return self.answer(self.start(method, params), timeout)

    def tool(self, name, arguments=None, timeout=30):
        message = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        result = message["result"]
        return result["content"][0]["text"], result["isError"]

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)
        self.process.stdout.close()


# What the real Codex 0.160 wrote to TCS Prover's bridge, line for line (captured from a coordinator's app-server
# against a scripted model server; only the tools/call name and the ids of its turn are the test's): its MCP client
# numbers requests from 0 and sends _meta with each.
CODEX_MCP_LINES = [
    '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":'
    '{"experimental":{"codex/auth-change":{}},"elicitation":{"form":{},"url":{}}},"clientInfo":'
    '{"name":"codex-mcp-client","title":"Codex","version":"0.160.0"}}}',
    '{"jsonrpc":"2.0","method":"notifications/initialized"}',
    '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"progressToken":0}}}',
    '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"_meta":{"callId":"act-members-1",'
    '"x-codex-turn-metadata":{"session_id":"t-1","thread_id":"t-1","reasoning_effort":"high","turn_id":"turn-1",'
    '"model":"gpt-5.5","sandbox":"seatbelt","sandbox_mode":"workspace-write","auto_review_enabled":false,'
    '"node_repl_auto_review_required":false,"node_repl_disabled":false,"turn_started_at_unix_ms":1794570369984,'
    '"codex_version":"0.160.0"},"threadId":"t-1","sessionId":"t-1","windowId":"t-1:0","itemId":"fc_1",'
    '"progressToken":1},"name":"echo","arguments":{"x":1}}}',
]


class Echo:
    """A provider of one tool for the service tests."""

    def __init__(self):
        self.release = threading.Event()

    def tools(self):
        return [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}},
                {"name": "block", "description": "Block", "inputSchema": {"type": "object"}}]

    def call(self, name, arguments):
        if name == "block":
            self.release.wait(20)
            return "released"
        if arguments.get("refuse"):
            raise team.TeamError("refused")
        return "echo " + json.dumps(arguments, sort_keys=True)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = team.Service()
        self.addCleanup(self.service.close)
        self.provider = Echo()
        self.service.add(self.provider)

    def client(self, entry=None):
        client = MCPClient(entry or self.service.codex_server())
        self.addCleanup(client.close)
        return client

    def test_the_bridge_lists_the_tools_and_forwards_calls_to_the_runner(self):
        client = self.client()
        tools = client.request("tools/list", {})["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["echo", "block"])
        self.assertEqual(client.tool("echo", {"x": 1}), ('echo {"x": 1}', False))
        self.assertEqual(client.tool("echo", {"refuse": True}), ("refused", True))
        self.assertEqual(client.tool("nothing")[1], True)
        # The CLIs start it from their settings with the token in its environment only, never in argv.
        entry = self.service.codex_server()
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], ["-I", str(team.BRIDGE)])
        self.assertNotIn(self.service.token, json.dumps(entry["args"]))
        self.assertEqual(entry["env"][team.TOKEN_ENV], self.service.token)
        self.assertEqual(entry["tool_timeout_sec"], team.TOOL_TIMEOUT_SECONDS)
        self.assertGreater(team.TOOL_TIMEOUT_SECONDS, team.WAIT_MINUTES * 60)
        self.assertEqual(entry["tools"], {"echo": {"approval_mode": "approve"}, "block": {"approval_mode": "approve"}})

    def test_claude_code_gets_an_sdk_server_that_the_runner_answers(self):
        # Claude Code 2.1.289 drops every --mcp-config server but an "sdk" one under --safe-mode; it then sends the
        # server's messages to the runner as control requests (shapes captured from that binary).
        self.assertEqual(self.service.claude_config(), {"mcpServers": {"tcs": {
            "type": "sdk", "name": "tcs", "timeout": team.TOOL_TIMEOUT_SECONDS * 1000, "alwaysLoad": True}}})
        self.assertNotIn(self.service.token, json.dumps(self.service.claude_config()))
        initialize = self.service.answer({"method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "2.1.289"}},
            "jsonrpc": "2.0", "id": 0})
        self.assertEqual(initialize["id"], 0)
        self.assertEqual(initialize["result"]["serverInfo"]["name"], "tcs")
        self.assertEqual(initialize["result"]["capabilities"], {"tools": {"listChanged": False}})
        # Claude Code shows a server's instructions to the model: the server gives none, only editable descriptions.
        self.assertNotIn("instructions", initialize["result"])
        self.assertIn(initialize["result"]["protocolVersion"], tcs_mcp.VERSIONS)
        self.assertEqual(self.service.answer({"method": "initialize", "params": {"protocolVersion": "2025-06-18"},
                                              "jsonrpc": "2.0", "id": 0})["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(self.service.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.service.answer({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                               "params": {"requestId": 2, "reason": "AbortError: remote-cancel"}}))
        listed = self.service.answer({"method": "tools/list", "jsonrpc": "2.0", "id": 1})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], ["echo", "block"])
        called = self.service.answer({"method": "tools/call", "params": {
            "name": "echo", "arguments": {"x": 1}, "_meta": {"claudecode/toolUseId": "toolu_1", "progressToken": 2}},
            "jsonrpc": "2.0", "id": 2})
        self.assertEqual(called, {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": 'echo {"x": 1}'}], "isError": False}})
        refused = self.service.answer({"method": "tools/call", "params": {"name": "echo", "arguments": {"refuse": 1}},
                                       "jsonrpc": "2.0", "id": 3})
        self.assertEqual(refused["result"], {"content": [{"type": "text", "text": "refused"}], "isError": True})
        self.assertEqual(self.service.answer({"method": "ping", "jsonrpc": "2.0", "id": 4})["result"], {})
        self.assertEqual(self.service.answer({"method": "resources/list", "jsonrpc": "2.0", "id": 5})["error"]["code"],
                         -32601)
        self.assertEqual(self.service.answer({"jsonrpc": "2.0", "id": 6})["error"]["code"], -32600)

    def test_codex_gives_every_tool_up_front_as_a_direct_tool_and_no_text_of_the_bridge_itself(self):
        entry = self.service.codex_server()
        # Codex 0.160 defers the MCP tools of a model with tool search (GPT 6.1 Sol, GPT 5.5): unlisted in the exec
        # tool's description or behind tool_search, their descriptions unseen. And a code-mode model (GPT 6.x) calls
        # them from its exec tool, whose cell yields every 30 s: a model call of Codex's wait tool each time while a
        # team_wait runs. Never deferred and never in code mode, every model gets them as direct tools.
        self.assertEqual(entry["omit_tools_from"], ["deferred", "code_mode"])
        environment = {**{key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "SYSTEMROOT")},
                       **entry["env"]}
        bridge = POPEN([entry["command"], *entry["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                       env=environment)
        try:
            answers = {}
            for line in CODEX_MCP_LINES:
                bridge.stdin.write(line + "\n")
                bridge.stdin.flush()
                if '"id"' in line:
                    answer = json.loads(bridge.stdout.readline())
                    answers[answer["id"]] = answer
        finally:
            bridge.stdin.close()
            bridge.wait(10)
            bridge.stdout.close()
        initialized = answers[0]["result"]
        self.assertEqual(initialized["protocolVersion"], "2025-06-18")
        # Codex shows a server's instructions to the model with its tools: the bridge has none of its own.
        self.assertNotIn("instructions", initialized)
        self.assertEqual([tool["name"] for tool in answers[1]["result"]["tools"]], ["echo", "block"])
        self.assertEqual(answers[2]["result"], {"content": [{"type": "text", "text": 'echo {"x": 1}'}],
                                                "isError": False})

    def test_tool_calls_and_answers_stay_utf_8_where_pipes_use_another_code_page(self):
        # Python on Windows reads and writes a piped stdin and stdout in the ANSI code page (cp1252, with
        # surrogateescape), and `python -I` ignores PYTHONUTF8 and PYTHONIOENCODING; Codex reads and writes UTF-8.
        # The bridge runs here with exactly those streams, and the test writes and reads raw UTF-8 as Codex does.
        received = []

        class Mirror:
            def tools(self):
                return [{"name": "mirror", "description": "Mirror", "inputSchema": {"type": "object"}}]

            def call(self, name, arguments):
                received.append(arguments.get("text"))
                return f"Report: {arguments.get('text')} — so x ≤ y."

        mirror = Mirror()
        self.service.add(mirror)
        self.addCleanup(self.service.remove, mirror)
        entry = self.service.codex_server()
        windows = ("import io, runpy, sys\n"
                   "sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='cp1252', errors='surrogateescape')\n"
                   "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='cp1252', errors='surrogateescape',"
                   " newline='\\r\\n')\n"
                   "runpy.run_path(sys.argv[1], run_name='__main__')\n")
        environment = {**{key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "SYSTEMROOT")},
                       **entry["env"]}
        bridge = POPEN([entry["command"], "-I", "-c", windows, entry["args"][-1]], stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE, env=environment)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(bridge.stdout), daemon=True)
        reader.start()

        def answer(number, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for line in list(lines):
                    message = json.loads(line.decode("utf-8"))  # Strict: what Codex can parse.
                    if message.get("id") == number:
                        return message
                time.sleep(0.05)
            raise AssertionError(f"no answer to request {number}")

        try:
            for number, text in enumerate(("Show that x ≤ y for all x in ℝ.", "Only a dash — here."), 1):
                request = {"jsonrpc": "2.0", "id": number, "method": "tools/call",
                           "params": {"name": "mirror", "arguments": {"text": text}}}
                bridge.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
                bridge.stdin.flush()
                result = answer(number)["result"]
                self.assertEqual(received[-1:], [text])
                self.assertEqual(result, {"content": [{"type": "text", "text": f"Report: {text} — so x ≤ y."}],
                                          "isError": False})
        finally:
            bridge.stdin.close()
            bridge.wait(10)
            reader.join(5)
            bridge.stdout.close()

    def test_a_call_without_the_token_gets_nothing(self):
        entry = self.service.codex_server()
        client = self.client({**entry, "env": {**entry["env"], team.TOKEN_ENV: "0" * 64}})
        text, error = client.tool("echo", {"x": 1})
        self.assertTrue(error)
        self.assertIn("cannot be reached", text)
        self.assertIn("error", client.request("tools/list", {}))

    def test_calls_run_side_by_side(self):
        client = self.client()
        blocked = client.start("tools/call", {"name": "block", "arguments": {}})
        # Answered while the first call still waits (it waits up to 20 s).
        self.assertEqual(client.tool("echo", {"y": 2}, timeout=5), ('echo {"y": 2}', False))
        self.provider.release.set()
        self.assertEqual(client.answer(blocked)["result"]["content"][0]["text"], "released")

    def test_a_closed_service_stops_listening_and_its_token_is_never_recorded(self):
        service = team.Service()
        service.close()
        service._thread.join(5)
        self.assertFalse(service._thread.is_alive())
        runtime.register_secret(service.token)
        self.assertNotIn(service.token, runtime.redact(f"token {service.token}"))

    def test_a_removed_provider_serves_nothing(self):
        self.service.remove(self.provider)
        self.assertEqual(self.service.names(), [])
        self.assertTrue(self.service.call("echo", {})["isError"])


class LoopDouble(coordinator_test.Coordinator):
    """The Codex RPC double; ``turns`` are callables(rpc) that play one turn each (they may call MCP tools)."""

    def __init__(self, turns, **kwargs):
        super().__init__([[] for _ in turns], **kwargs)
        self.plays = list(turns)
        self.on_empty = self.play

    def play(self, rpc):
        if self.plays:
            self.plays.pop(0)(rpc)


GOAL_PROMPTS = {**goal_test.PROMPTS, "goal": "Prove S as the first prompt says.",
                "continuation": "Keep working on S.", "report": "That reply was a report; keep going."}


class GoalLoopTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = popen.start()
        self.addCleanup(popen.stop)
        handoff = mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True)
        handoff.start()
        self.addCleanup(handoff.stop)

    def session(self, double, coordinator=False, loop="tcs", service=None, crew=None, **options):
        session = runtime.goal_session(
            double, "Prove S exactly.", prompts=GOAL_PROMPTS,
            settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"},
            features=[], coordinator=coordinator, texts=TEXTS, service=service, team=crew,
            options={"goal_cwd": str(self.directory), "goal_loop": loop, **options})
        self.addCleanup(session.close)
        return session

    @staticmethod
    def turns(double):
        return [params["input"][0]["text"] for method, params in double.rpc.calls if method == "turn/start"]

    @staticmethod
    def client(double):
        """The CLI's MCP client, started from the thread's settings as Codex starts it."""

        start = next(params for method, params in double.rpc.calls if method == "thread/start")
        return MCPClient(start["config"]["mcp_servers"]["tcs"])

    def test_tcs_runs_without_codex_goals_and_sends_the_goal_continuation_after_every_open_turn(self):
        clients = []

        def blocked(rpc):
            client = clients[0] if clients else clients.append(self.client(double)) or clients[0]
            listed = client.request("tools/list", {})["result"]["tools"]
            update, = listed
            self.assertEqual(update["name"], "update_goal")
            self.assertEqual(update["description"], TEXTS["goal_tool_update_goal"])
            self.assertEqual(update["inputSchema"]["properties"]["status"]["enum"], ["complete", "blocked", "paused"])
            self.assertEqual(update["inputSchema"]["properties"]["status"]["description"],
                             TEXTS["goal_tool_update_goal_status"])
            text, error = client.tool("update_goal", {"status": "blocked", "note": "Lemma 2 resists."})
            self.assertFalse(error)
            self.assertEqual(json.loads(text)["goal"]["status"], "active")
            rpc.messages.extend([goal_test.answer("Still blocked on Lemma 2."), goal_test.completed()])

        def done(rpc):
            text, error = clients[0].tool("update_goal", {"status": "complete"})
            self.assertEqual((json.loads(text)["goal"]["status"], error), ("complete", False))
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double = LoopDouble([lambda rpc: rpc.messages.extend([goal_test.answer("Progress."), goal_test.completed()]),
                             blocked, done], elapsed=2 * 3600 + 5 * 60)
        double.remaining = 3 * 3600 - 5 * 60
        self.addCleanup(lambda: [client.close() for client in clients])
        result = next(self.session(double, thinking_hours=5))
        self.assertEqual(result, {"outcome": "done", "output": POINTER})
        argv = self.spawn.call_args.args[0]
        self.assertIn("--disable", argv)
        self.assertEqual(argv[argv.index("--disable") + 1], "goals")
        self.assertNotIn("--enable", argv[:argv.index("--disable") + 1])
        start = next(params for method, params in double.rpc.calls if method == "thread/start")
        self.assertFalse(start["config"]["features"]["goals"])
        self.assertEqual(start["config"]["mcp_servers"]["tcs"]["enabled_tools"], ["update_goal"])
        # An author that works alone gets none of Codex's collaboration tools.
        self.assertEqual(start["config"]["agents"], {"enabled": False})
        # No goal request of Codex's goal mode.
        self.assertEqual([method for method, _ in double.rpc.calls if method.startswith("thread/goal/")], [])
        first, second, third = self.turns(double)
        self.assertEqual(first, "Prove S exactly.")
        expected = runtime.render_template(TEXTS["goal_continuation"], {
            "continuation": "Keep working on S.", "objective": "Prove S as the first prompt says.",
            "elapsed": "2 h 5 min", "limit": "5 h, about 2 h 55 min left"})
        self.assertEqual(second, expected)
        self.assertEqual(third, expected)
        self.assertTrue(second.startswith("Keep working on S.\n\nContinue working toward the active thread goal."))
        # The blocked status is recorded and never stops the work; the final status hands the reply on.
        statuses = [(event["goalStatus"], event["note"]) for event in double.events
                    if event.get("label") == "Goal status"]
        self.assertEqual(statuses, [("blocked", "Lemma 2 resists."), ("complete", "")])
        self.assertIn("Goal complete", [event.get("label") for event in double.events])

    def test_a_complete_goal_without_a_final_reply_and_a_paused_goal_continue(self):
        def call(status, reply):
            def turn(rpc):
                double.service.call("update_goal", {"status": status})
                rpc.messages.extend([*([goal_test.answer(reply)] if reply else []), goal_test.completed()])
            return turn

        double = LoopDouble([call("complete", None), call("paused", "Pausing as asked."), call("complete", POINTER)])
        service = double.service = team.Service()
        self.addCleanup(service.close)
        session = self.session(double, service=service)
        self.assertEqual(next(session)["output"], POINTER)
        self.assertEqual(len(self.turns(double)), 3)
        self.assertEqual([event["goalStatus"] for event in double.events if event.get("label") == "Goal status"],
                         ["complete", "paused", "complete"])
        # The session's tool leaves the runner's server with it.
        self.assertEqual(service.names(), ["update_goal"])
        session.close()
        self.assertEqual(service.names(), [])

    def test_only_a_status_given_in_the_turn_itself_ends_it(self):
        def done(rpc):
            double.service.call("update_goal", {"status": "complete"})
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double = LoopDouble([done, lambda rpc: rpc.messages.extend([goal_test.answer("Fixing Lemma 2."),
                                                                     goal_test.completed()]), done])
        service = double.service = team.Service()
        self.addCleanup(service.close)
        session = self.session(double, service=service)
        self.assertEqual(next(session)["output"], POINTER)
        # A late call while the critic reviews (no turn runs) belongs to no turn: the repair turn that gives no
        # status of its own is continued, never handed on.
        service.call("update_goal", {"status": "complete"})
        self.assertEqual(session.send({"solution": "Fixed", "bugs": "Gap.", "round": 1})["output"], POINTER)
        self.assertEqual(len(self.turns(double)), 3)

    def test_a_coordinators_report_is_answered_inside_the_goal_continuation(self):
        def report(rpc):
            double.service.call("update_goal", {"status": "complete"})
            rpc.messages.extend([goal_test.answer("Members 1 and 2 are on Lemma 3."), goal_test.completed()])

        def done(rpc):
            double.service.call("update_goal", {"status": "complete"})
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double = LoopDouble([report, done])
        service = double.service = team.Service()
        self.addCleanup(service.close)
        self.assertEqual(next(self.session(double, coordinator=True, service=service))["output"], POINTER)
        second = self.turns(double)[1]
        self.assertTrue(second.startswith("That reply was a report; keep going.\n\nContinue working toward"))
        self.assertIn("Coordinator report", [event.get("label") for event in double.events])

    def test_pause_and_the_time_limit_interrupt_the_turn_without_goal_requests(self):
        pause = self.directory / "pause-request.json"

        def paused(rpc):
            pause.write_text("{}")
            self.assertTrue(double.interrupted.wait(5))
            rpc.messages.append(goal_test.completed("interrupted"))

        double = LoopDouble([paused])
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(self.session(double, goal_pause_file=str(pause)))
        self.assertIsNone(caught.exception.retry)
        self.assertEqual([method for method, _ in double.rpc.calls if method.startswith("thread/goal/")], [])
        self.assertIn("turn/interrupt", [method for method, _ in double.rpc.calls])

        def expired(rpc):
            double.remaining = 0
            self.assertTrue(double.interrupted.wait(5))
            rpc.messages.append(goal_test.completed("interrupted"))

        double = LoopDouble([expired])
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(self.session(double))
        self.assertIn("time limit", str(caught.exception))
        self.assertEqual([method for method, _ in double.rpc.calls if method.startswith("thread/goal/")], [])

    def test_codex_goal_mode_stays_as_before(self):
        double = LoopDouble([lambda rpc: rpc.messages.extend(goal_test.success(POINTER))])
        self.assertEqual(next(self.session(double, loop="codex"))["output"], POINTER)
        argv = self.spawn.call_args.args[0]
        self.assertEqual(argv[argv.index("app-server") + 1:argv.index("app-server") + 3], ["--enable", "goals"])
        start = next(params for method, params in double.rpc.calls if method == "thread/start")
        self.assertTrue(start["config"]["features"]["goals"])
        self.assertNotIn("tcs", start["config"].get("mcp_servers", {}))
        self.assertIn("thread/goal/set", [method for method, _ in double.rpc.calls])
        # A caller that does not choose (a job from before) gets Codex's goal mode too.
        double = LoopDouble([lambda rpc: rpc.messages.extend(goal_test.success(POINTER))])
        session = runtime.goal_session(
            double, "Prove S exactly.", prompts=GOAL_PROMPTS,
            settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"},
            options={"goal_cwd": str(self.directory)})
        self.addCleanup(session.close)
        next(session)
        self.assertIn("thread/goal/set", [method for method, _ in double.rpc.calls])

    def test_only_an_author_with_codex_s_own_subagents_gets_codex_s_collaboration_tools(self):
        # Codex 0.160 gives a model of multi-agent version 2 its collaboration tools also without the multi_agent
        # feature (captured with the real Codex against a scripted model server): an author or coordinator that
        # starts no subagent of Codex has Codex's agents off in its thread, whoever runs its goal loop.
        settings = {"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"}
        for loop, features, subagents, agents in (
                ("tcs", [], 0, {"enabled": False}), ("codex", [], 0, {"enabled": False}),
                ("codex", ["multi_agent"], 2, {"max_concurrent_threads_per_session": 2, "max_depth": 1}),
                ("tcs", ["multi_agent"], 2, {"max_concurrent_threads_per_session": 2, "max_depth": 1})):
            with self.subTest(loop=loop, features=features):
                def complete(rpc):
                    if loop == "tcs":
                        service.call("update_goal", {"status": "complete"})
                        rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])
                    else:
                        rpc.messages.extend(goal_test.success(POINTER))

                service = team.Service()
                self.addCleanup(service.close)
                double = LoopDouble([complete])
                session = runtime.goal_session(
                    double, "Prove S exactly.", prompts=GOAL_PROMPTS, settings={**settings, "subagents": subagents},
                    features=features, texts=TEXTS, service=service,
                    options={"goal_cwd": str(self.directory), "goal_loop": loop})
                self.addCleanup(session.close)
                self.assertEqual(next(session)["output"], POINTER)
                start = next(params for method, params in double.rpc.calls if method == "thread/start")
                # Codex's own subagents keep their limits and nothing else.
                self.assertEqual(start["config"].get("agents"), agents)

    def test_a_workflow_without_the_goal_texts_says_what_is_missing(self):
        double = LoopDouble([])
        session = runtime.goal_session(
            double, "Prove S exactly.", prompts=GOAL_PROMPTS,
            settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"},
            texts={}, options={"goal_cwd": str(self.directory), "goal_loop": "tcs"})
        self.addCleanup(session.close)
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(session)
        self.assertIn("goal_continuation", str(caught.exception))
        self.assertIsNone(caught.exception.retry)


API_KEY = "sk-lab-0123456789abcdef"
MEMBER_SETTINGS = {"speed": "standard", "summary": "concise"}


def member_records(assignments, count=2):
    """The records the runner makes for ``assignments`` [(member, assignment)], run one after another by a team of
    ``count`` members of the coordinator's model (runtime.start_team()); each member run is a stand-in that reports
    what a `codex exec --json` run does: its items numbered from item_0 again. The server's and the page's tests
    read them as a job's transcript."""

    records, lock = [], threading.Lock()

    def capture(kind, stage, **fields):
        with lock:
            records.append({"kind": kind, "stage": stage, **fields})

    # runtime.member_attempt()'s own arguments: a stand-in that refused one would fail every attempt, which the team
    # retries every minute, so the next assignment of the member would be refused as busy.
    def attempt(member, prompt, conversation, started, *, directory, settings, compact_prompt, record, retried=None):
        first = prompt.splitlines()[0]
        record({"type": "thread.started", "thread_id": f"019a{member.count:04d}-0000-7000-8000-000000000000"})
        record({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": f"About {first}."}})
        record({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": f"Done: {first}"}})
        return f"Done: {first}"

    with tempfile.TemporaryDirectory() as folder, mock.patch.object(runtime, "emit", side_effect=capture), \
            mock.patch.object(runtime, "member_attempt", side_effect=attempt):
        crew = runtime.start_team(team.team_members(None, "gpt-6.1-sol", "high", count), prompts=TEXTS,
                                  directory=folder, settings=MEMBER_SETTINGS, stage="solve", node_name="author")
        try:
            for number, assignment in assignments:
                crew.call("team_assign", {"member": number, "assignment": assignment})
                crew.call("team_wait", {"timeout_minutes": 1})
        finally:
            crew.close("The test ended.")
    return records


def member_retry_records(failures, cli_retries):
    """The records of one assignment whose model call fails ``failures`` times (the endpoint answers 503) before it
    works, its CLI reporting ``cli_retries`` retries of a failed API request inside each of its runs, as Claude Code
    does (runtime.start_team(), with a stand-in for runtime.member_attempt)."""

    records, lock, prompts = [], threading.Lock(), []

    def capture(kind, stage, **fields):
        with lock:
            records.append({"kind": kind, "stage": stage, **fields})

    def attempt(member, prompt, conversation, started, *, directory, settings, compact_prompt, record, retried=None):
        prompts.append(prompt)
        for number in range(cli_retries):
            retried(f"Claude API retry: status 529, retrying in 35 s (attempt {number + 1} of 300)")
        if len(prompts) <= failures:
            raise runtime.Error("503 Service Unavailable: the model is overloaded")
        return "Lemma A holds."

    with tempfile.TemporaryDirectory() as folder, mock.patch.object(runtime, "emit", side_effect=capture), \
            mock.patch.object(runtime, "member_attempt", side_effect=attempt), \
            mock.patch.object(runtime, "MODEL_RETRY_SECONDS", 0.01):
        crew = runtime.start_team(team.team_members(None, "claude-opus-5-5", "high", 1), prompts=TEXTS,
                                  directory=folder, settings=MEMBER_SETTINGS, stage="solve", node_name="author")
        try:
            crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
            crew.call("team_wait", {"timeout_minutes": 1})
        finally:
            crew.close("The test ended.")
    return records


@unittest.skipIf(os.name == "nt", "The stand-in CLIs are POSIX scripts")
class TeamCase(unittest.TestCase):
    """Stand-in `codex` and `claude` programs on PATH, a run folder and the run's records."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("codex", FAKE_CODEX), ("claude", claude_test.FAKE_CLAUDE)):
            fake = self.bin / name
            fake.write_text(f"#!{sys.executable}\n{body}")
            fake.chmod(0o700)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        (self.root / "codex-state").mkdir()
        self.codex_log = self.root / "codex.jsonl"
        self.claude_log = self.root / "claude.jsonl"
        self.scenario = self.root / "scenario.json"
        self.scenario.write_text(json.dumps({"turns": []}))
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.root / "home"),
            "TCS_FAKE_CODEX_LOG": str(self.codex_log), "TCS_FAKE_CODEX_STATE": str(self.root / "codex-state"),
            "TCS_FAKE_CLAUDE_LOG": str(self.claude_log), "TCS_FAKE_CLAUDE_SCENARIO": str(self.scenario),
            "TCS_PROVER_API_MODELS": str(self.root / "state" / "api-models.json"),
            "TCS_PROVER_RETRIEVAL_MODE": "live", "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "SSH_AUTH_SOCK": "/agent.sock",
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        previous = Path.cwd()
        os.chdir(self.run_dir)
        self.addCleanup(os.chdir, previous)
        self.events, self.events_lock = [], threading.Lock()
        emitter = mock.patch.object(runtime, "emit", side_effect=self.capture)
        emitter.start()
        self.addCleanup(emitter.stop)
        for name, value in (("MODEL_RETRY_SECONDS", 0.05),):
            patch = mock.patch.object(runtime, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name, value in (("DISALLOWED_TOOLS", set()), ("_CONFIG_CHECKS", {}), ("_REFUSALS", {})):
            patch = mock.patch.object(claude_cli, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        private_temp = self.root / "tmp"
        private_temp.mkdir()
        temp_patch = mock.patch.object(tempfile, "tempdir", str(private_temp))
        temp_patch.start()
        self.addCleanup(temp_patch.stop)
        api = mock.patch.object(runtime, "api_codex_executable", return_value=str(self.bin / "codex"))
        api.start()
        self.addCleanup(api.stop)
        # Claude's Bash temp folders (confinement.claude_temp(), /tmp by default) go to this test's folder.
        (self.root / "ct").mkdir()
        claude_temp = mock.patch.object(confinement, "CLAUDE_TEMP_PARENT", str(self.root / "ct"))
        claude_temp.start()
        self.addCleanup(claude_temp.stop)

    def capture(self, kind, stage, **fields):
        with self.events_lock:
            self.events.append({"kind": kind, "stage": stage, **fields})

    def labelled(self, label):
        with self.events_lock:
            return [event for event in self.events if event.get("label") == label]

    def codex_calls(self):
        if not self.codex_log.exists():
            return []
        return [json.loads(line) for line in self.codex_log.read_text().splitlines()]

    def claude_starts(self):
        if not self.claude_log.exists():
            return []
        return [row for row in map(json.loads, self.claude_log.read_text().splitlines()) if row["event"] == "start"]

    def api_model(self):
        return providers.save_api_model({"format": "openai", "baseUrl": "https://lab.example.com/v1",
                                         "headerName": "Authorization", "model": "lab-model", "apiKey": API_KEY,
                                         "label": "Lab gateway"})["id"]

    def crew(self, models, coordinator="gpt-6.1-sol"):
        members = team.team_members(models, coordinator, "high", len(models) if models else 2)
        crew = runtime.start_team(members, prompts=TEXTS, directory=self.run_dir, settings=MEMBER_SETTINGS,
                                  stage="solve", node_name="author")
        self.addCleanup(crew.close, "The test ended.")
        return crew

    def collect(self, crew, count, minutes=1):
        """team_wait until ``count`` reports came (each wait returns those finished since the last)."""

        reports, deadline = [], time.monotonic() + 60
        while len(reports) < count and time.monotonic() < deadline:
            text = crew.call("team_wait", {"timeout_minutes": minutes})
            reports += [part for part in text.split("\n\n") if part.startswith("Member ")]
        self.assertEqual(len(reports), count, reports)
        return reports

    def held_member(self):
        """Members that finish only when the returned event is set (runtime.member_attempt's own arguments)."""

        release = threading.Event()

        def attempt(member, prompt, conversation, started, *, directory, settings, compact_prompt, record,
                    retried=None):
            release.wait(30)
            return "Done: " + prompt.splitlines()[0]

        patch = mock.patch.object(runtime, "member_attempt", side_effect=attempt)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(release.set)
        return release

    def finish(self, crew, release):
        """Let the members finish; then a team_wait that an ended attempt left running would have taken the reports."""

        release.set()
        deadline = time.monotonic() + 20
        while crew.running() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)


class TeamTests(TeamCase):
    def test_a_codex_coordinator_under_tcs_runs_a_team_of_a_codex_a_claude_and_an_api_member(self):
        api = self.api_model()
        self.scenario.write_text(json.dumps({"turns": [["init", {"emit": {
            "type": "assistant", "parent_tool_use_id": None, "uuid": "m1",
            "message": {"content": [{"type": "text", "text": "Lemma B holds: see team/b.md."}]}}},
            {"result": {"result": "Lemma B holds: see team/b.md."}}]]}))
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}, {"model": "claude-opus-5-5", "effort": "max"},
                          {"model": api, "effort": "medium"}])
        service = team.Service()
        self.addCleanup(service.close)
        seen = {}

        def turn(rpc):
            start = next(params for method, params in double.rpc.calls if method == "thread/start")
            client = MCPClient(start["config"]["mcp_servers"]["tcs"])
            self.addCleanup(client.close)
            seen["tools"] = client.request("tools/list", {})["result"]["tools"]
            seen["members"] = client.tool("team_members")[0]
            for number, line in ((1, "Lemma A"), (2, "Lemma B"), (3, "Lemma C")):
                text, error = client.tool("team_assign", {"member": number, "assignment": f"{line}\nProve it."})
                self.assertFalse(error, text)
            reports = []
            while len(reports) < 3:
                text, error = client.tool("team_wait", {"timeout_minutes": 1}, timeout=90)
                self.assertFalse(error, text)
                reports += [part for part in text.split("\n\n") if part.startswith("Member ")]
            seen["reports"] = reports
            client.tool("update_goal", {"status": "complete"})
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double = LoopDouble([turn])
        with mock.patch.object(runtime.subprocess, "Popen", side_effect=lambda argv, **kwargs: (
                SimpleNamespace() if "app-server" in argv else POPEN(argv, **kwargs))) as spawn, \
                mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            session = runtime.goal_session(
                double, "Coordinate.", prompts=GOAL_PROMPTS, coordinator=True, texts=TEXTS, team=crew,
                service=service, features=[],
                settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise",
                          "subagents": 3},
                options={"goal_cwd": str(self.run_dir), "goal_loop": "tcs"})
            self.addCleanup(session.close)
            self.assertEqual(next(session)["output"], POINTER)
            app_server = next(call.args[0] for call in spawn.call_args_list if "app-server" in call.args[0])
        # The coordinator has TCS Prover's tools, and none of Codex's own subagent tools: without the multi_agent
        # feature Codex 0.160 still offers a GPT 6.1 Sol thread its collaboration tools (spawn_agent, ...) unless its
        # agents are off.
        self.assertEqual([tool["name"] for tool in seen["tools"]], ["update_goal", *team.TEAM_TOOLS])
        start = next(params for method, params in double.rpc.calls if method == "thread/start")
        self.assertEqual(start["config"]["agents"], {"enabled": False})
        self.assertEqual(seen["tools"][2]["description"], TEXTS["team_tool_assign"])
        self.assertEqual(seen["tools"][2]["inputSchema"]["properties"]["assignment"]["description"],
                         TEXTS["team_tool_assign_assignment"])
        self.assertEqual(app_server[app_server.index("multi_agent") - 1], "--disable")
        self.assertEqual(seen["members"], "\n".join([
            "member 1: GPT 6.1 Sol (Codex CLI): idle", "member 2: Claude Opus 5.5 (Claude Code): idle",
            'member 3: lab-model (API connection "Lab gateway"): idle']))
        reports = "\n\n".join(seen["reports"])
        self.assertIn("Report of gpt-6.1-sol: Lemma A", reports)
        self.assertIn("Lemma B holds: see team/b.md.", reports)
        self.assertIn("Report of lab-model: Lemma C", reports)
        for number in (1, 2, 3):
            self.assertIn(f"Report file: team/member-{number}/1-report.md", reports)
            path = self.run_dir / "team" / f"member-{number}" / "1-report.md"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((path.parent.stat().st_mode & 0o777), 0o700)
        self.assertEqual((self.run_dir / "team/member-2/1-report.md").read_text(), "Lemma B holds: see team/b.md.")
        # Each member's input is the assignment, then the line that makes its final reply the report.
        prompts = sorted(call["prompt"] for call in self.codex_calls())
        self.assertEqual(prompts, [f"{line}\nProve it.\n\nYour final reply is your report to the coordinator."
                                   for line in ("Lemma A", "Lemma C")])
        for call in self.codex_calls():
            argv = call["argv"]
            self.assertEqual(argv[argv.index("-C") + 1], str(self.run_dir))
            self.assertEqual(argv[argv.index("multi_agent") - 1], "--disable")
            # One turn and no subagents: Codex's own goal tools and collaboration tools are off (`codex exec` 0.160
            # offers both otherwise).
            self.assertEqual(argv[argv.index("goals") - 1], "--disable")
            self.assertEqual(argv[argv.index("agents.enabled=false") - 1], "-c")
            self.assertLess(argv.index("agents.enabled=false"), argv.index("exec"))
            self.assertIn("--ignore-user-config", argv)
            self.assertEqual(argv[argv.index("-a") + 1], "never")
            # The author's sandbox: the confined profile on macOS, else Codex's workspace-write mode.
            self.assertTrue('default_permissions="tcs_prover_private"' in argv
                            or argv[argv.index("-s") + 1] == "workspace-write")
            self.assertNotIn("SSH_AUTH_SOCK", call["env"])
            self.assertNotIn(team.TOKEN_ENV, call["env"])
        api_call = next(call for call in self.codex_calls() if "lab-model" in call["argv"])
        self.assertEqual(api_call["env"].get("TCS_PROVER_API_TOKEN"), API_KEY)
        start, = self.claude_starts()
        tools = start["argv"][start["argv"].index("--tools") + 1].split(",")
        self.assertNotIn("Task", tools)
        self.assertIn("Write", tools)
        self.assertNotIn("--json-schema", start["argv"])
        self.assertEqual(start["cwd"], str(self.run_dir))
        self.assertTrue(any(rule.startswith("Edit(") for rule in start["settings"]["permissions"]["deny"]))
        # Records: each member's start and finish, with its number and model; its activity as a subagent's.
        started = self.labelled("Author subagent started")
        self.assertEqual(sorted((event["subagentMember"], event["subagentModel"], event["subagentTask"])
                                for event in started),
                         [(1, "GPT 6.1 Sol", "Lemma A"), (2, "Claude Opus 5.5", "Lemma B"), (3, "lab-model", "Lemma C")])
        finished = self.labelled("Author subagent finished")
        self.assertEqual(sorted(event["subagentReport"] for event in finished),
                         [f"team/member-{number}/1-report.md" for number in (1, 2, 3)])
        self.assertTrue(all(event["authorSubagent"] for event in started + finished))
        member_events = [event for event in self.events if event["kind"] == "codex_event" and "subagentMember" in event]
        self.assertTrue(member_events)
        self.assertTrue(all(event["root"] is False for event in member_events))
        self.assertEqual({event["subagentMember"] for event in member_events}, {1, 2, 3})
        self.assertNotIn(API_KEY, json.dumps(self.events))

    def test_a_claude_members_own_retries_of_a_failed_request_are_recorded(self):
        # Claude Code retries a failed API request inside the member's run (system/api_retry, as 2.1.289 reported it
        # for a key its endpoint refused, up to 300 times): each retry is a diagnostic of that member.
        self.scenario.write_text(json.dumps({"turns": [["init", {"emit": {
            "type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 300, "retry_delay_ms": 556,
            "error_status": 401, "error": "authentication_failed", "uuid": "6b2ac872-87f0-43c4-8efd-2c797872f342"}},
            {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "m1",
                      "message": {"content": [{"type": "text", "text": "Lemma A holds."}]}}},
            {"result": {"result": "Lemma A holds."}}]]}))
        crew = self.crew([{"model": "claude-opus-5-5", "effort": "high"}])
        crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
        report, = self.collect(crew, 1)
        self.assertIn("REPORT:\nLemma A holds.", report)
        retry, = [event for event in self.events if event["kind"] == "diagnostic" and "retry" in event["text"]]
        self.assertEqual(retry["text"], "Member 1 (Claude Opus 5.5): Claude API retry: status 401, retrying in 0.6 s "
                                        "(attempt 1 of 300)")
        self.assertEqual((retry["subagentMember"], retry["subagentModel"]), (1, "Claude Opus 5.5"))
        # A record of the assignment like its events and the team's own retry notes: it carries the assignment's id.
        self.assertEqual(retry["subagentId"], "member-1-1")

    def test_followup_continues_a_members_conversation_and_wrong_calls_are_refused(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}])
        with self.assertRaisesRegex(team.TeamError, "no conversation that can be continued"):
            crew.call("team_followup", {"member": 1, "message": "More."})
        for arguments, refusal in (({"member": 2, "assignment": "x"}, "There is no member 2"),
                                   ({"member": 1, "assignment": "  "}, "empty")):
            with self.assertRaisesRegex(team.TeamError, refusal):
                crew.call("team_assign", arguments)
        with self.assertRaisesRegex(team.TeamError, "timeout_minutes"):
            crew.call("team_wait", {"timeout_minutes": 31})
        self.assertIn("No member is working", crew.call("team_wait", {}))
        crew.call("team_assign", {"member": 1, "assignment": "Lemma A SLOW"})
        with self.assertRaisesRegex(team.TeamError, "working"):
            crew.call("team_assign", {"member": 1, "assignment": "Another"})
        with self.assertRaisesRegex(team.TeamError, "working"):
            crew.call("team_followup", {"member": 1, "message": "More."})
        self.assertIn('working on "Lemma A SLOW" since', crew.call("team_members", {}))
        crew.call("team_stop", {"member": 1})
        report, = self.collect(crew, 1)
        self.assertIn("stopped the assignment", report)
        self.assertIn("This assignment was stopped. The coordinator stopped it with team_stop.", report)
        failed, = self.labelled("Author subagent failed")
        self.assertEqual(failed["text"], "The coordinator stopped it with team_stop.")
        crew.call("team_assign", {"member": 1, "assignment": "Lemma B"})
        first, = self.collect(crew, 1)
        session = self.codex_calls()[-1]
        crew.call("team_followup", {"member": 1, "message": "Now Lemma C."})
        second, = self.collect(crew, 1)
        call = self.codex_calls()[-1]
        self.assertIsNotNone(call["fork"])
        self.assertEqual(call["argv"][call["argv"].index("exec") + 1], "fork")
        self.assertIn("(continuing ", second)
        self.assertEqual(call["prompt"], "Now Lemma C.\n\nYour final reply is your report to the coordinator.")
        # The stopped assignment was the member's first; each report has its own file.
        self.assertIn("team/member-1/2-report.md", first)
        self.assertIn("team/member-1/3-report.md", second)
        self.assertIsNone(session["fork"])

    def test_every_record_of_an_assignment_carries_its_id_and_two_assignments_never_share_one(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}, {"model": "gpt-6-luna", "effort": "low"}])
        crew.call("team_assign", {"member": 1, "assignment": "FAIL-ONCE Lemma A"})
        self.collect(crew, 1)
        crew.call("team_assign", {"member": 1, "assignment": "Lemma B"})
        crew.call("team_assign", {"member": 2, "assignment": "Lemma C"})
        self.collect(crew, 2)
        with self.events_lock:
            events = [dict(event) for event in self.events]
        member_events = [event for event in events if event["kind"] == "codex_event"]
        statuses = [event for event in events if event.get("authorSubagent")]
        # Its events (the retried attempt's too), its status records and its retry note: one id per assignment.
        self.assertEqual(sorted({(event["subagentMember"], event["subagentId"]) for event in member_events}),
                         [(1, "member-1-1"), (1, "member-1-2"), (2, "member-2-1")])
        self.assertEqual(sorted((event["subagentId"], event["subagentState"]) for event in statuses),
                         [("member-1-1", "finished"), ("member-1-1", "started"), ("member-1-2", "finished"),
                          ("member-1-2", "started"), ("member-2-1", "finished"), ("member-2-1", "started")])
        retry, = [event for event in events if event["kind"] == "diagnostic" and "retrying" in event["text"]]
        self.assertEqual(retry["subagentId"], "member-1-1")
        replies = {event["subagentId"]: event["event"]["item"]["text"] for event in member_events
                   if event["event"].get("item", {}).get("type") == "agent_message"}
        self.assertIn("(continuing ", replies["member-1-1"])  # The retry of the first assignment.
        self.assertIn("Lemma B", replies["member-1-2"])
        self.assertIn("Lemma C", replies["member-2-1"])
        # Each run numbers its items from item_0 again: only the assignment's id keeps them apart.
        first, second = ([event["event"]["item"]["id"] for event in member_events
                          if event["subagentId"] == identity and event["event"].get("item", {}).get("type")
                          == "agent_message"] for identity in ("member-1-1", "member-1-2"))
        self.assertEqual(first, second)

    def test_a_model_call_failure_is_retried_and_a_settings_problem_goes_to_the_coordinator(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}, {"model": "gpt-6-unknown", "effort": "high"}])
        crew.call("team_assign", {"member": 1, "assignment": "FAIL-ONCE Lemma A"})
        report, = self.collect(crew, 1)
        self.assertIn("finished the assignment", report)
        failed, retried = self.codex_calls()
        self.assertIsNone(failed["fork"])
        # The attempt worked before it failed: the retry continues its conversation with the retry note.
        self.assertIsNotNone(retried["fork"])
        self.assertEqual(retried["prompt"], runtime.AUTHOR_RETRY_NOTE)
        retry, = [event for event in self.events if event["kind"] == "diagnostic" and "retrying" in event["text"]]
        self.assertEqual(retry["subagentMember"], 1)
        crew.call("team_assign", {"member": 1, "assignment": "NOLOGIN Lemma B"})
        report, = self.collect(crew, 1)
        self.assertIn("failed the assignment", report)
        self.assertIn("authentication failed", report)
        self.assertEqual(len(self.codex_calls()), 3)  # Never retried.
        crew.call("team_assign", {"member": 2, "assignment": "Lemma C"})
        report, = self.collect(crew, 1)
        self.assertIn("Unknown model 'gpt-6-unknown'", report)
        self.assertIn("member 2: gpt-6-unknown (unknown model): idle", crew.call("team_members", {}))

    def coordinator(self, crew, turns, **options):
        service = team.Service()
        self.addCleanup(service.close)
        double = LoopDouble(turns)
        double.service = service
        spawn = mock.patch.object(runtime.subprocess, "Popen", side_effect=lambda argv, **kwargs: (
            SimpleNamespace() if "app-server" in argv else POPEN(argv, **kwargs)))
        spawn.start()
        self.addCleanup(spawn.stop)
        session = runtime.goal_session(
            double, "Coordinate.", prompts=GOAL_PROMPTS, coordinator=True, texts=TEXTS, team=crew, service=service,
            features=[], settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise",
                                   "subagents": 1},
            options={"goal_cwd": str(self.run_dir), "goal_loop": "tcs", **options})
        self.addCleanup(session.close)
        return double, session

    def wait_for_slow_member(self):
        deadline = time.monotonic() + 20
        while not any("SLOW" in call["prompt"] for call in self.codex_calls()) and time.monotonic() < deadline:
            time.sleep(0.05)

    def test_pause_stop_and_the_time_limit_stop_running_members_with_the_coordinator(self):
        for cause, label, reason in (("pause", "Author paused", "It stopped when the author paused."),
                                     ("stopped", "Author stopped", "It stopped when the author was stopped."),
                                     ("limit", None, "It stopped at the time limit.")):
            with self.subTest(cause=cause):
                self.events.clear()
                crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}])
                pause = self.run_dir / f"pause-{cause}.json"

                def turn(rpc, crew=crew, pause=pause, cause=cause):
                    crew.call("team_assign", {"member": 1, "assignment": "Lemma A SLOW"})
                    self.wait_for_slow_member()
                    if cause == "limit":
                        double.remaining = 0
                    else:
                        pause.write_text(json.dumps({"cause": cause}))
                    self.assertTrue(double.interrupted.wait(5))
                    rpc.messages.append(goal_test.completed("interrupted"))

                double, session = self.coordinator(crew, [turn], goal_pause_file=str(pause))
                began = time.monotonic()
                with self.assertRaises(runtime.WorkflowPaused) as caught:
                    next(session)
                self.assertIsNone(caught.exception.retry)
                self.assertLess(time.monotonic() - began, 60)  # The member's 120 s run was stopped.
                failed, = self.labelled("Author subagent failed")
                self.assertEqual((failed["subagentMember"], failed["text"]), (1, reason))
                self.assertIsNotNone(crew.closed)
                with self.assertRaisesRegex(team.TeamError, "The team has stopped"):
                    crew.call("team_assign", {"member": 1, "assignment": "More"})
                if label:
                    self.assertIn(label, [event.get("label") for event in double.events])

    def test_a_failed_model_call_of_the_coordinator_keeps_its_members_working(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}])

        def failing(rpc):
            crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
            rpc.messages.append(runtime.Error("stream disconnected before completion"))

        double, session = self.coordinator(crew, [failing])
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(session)
        self.assertIsNotNone(caught.exception.retry)
        self.assertIsNone(crew.closed)
        report, = self.collect(crew, 1)  # The next attempt reads the report.
        self.assertIn("Report of gpt-6.1-sol: Lemma A", report)

    def test_a_wait_of_a_failed_attempt_takes_neither_the_reports_nor_an_attention_of_the_next(self):
        # team-decisions item 5: the coordinator's model call fails while its team_wait runs. The attempt ends (Codex
        # and its bridge stop with it) and the team works on for the next attempt, which must get the report and the
        # live instructions and time checks that end its own waits.
        release = self.held_member()
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}])
        waits = {}

        def failing(rpc):
            crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
            waits["thread"] = threading.Thread(target=lambda: waits.setdefault("dead", crew.call("team_wait", {})),
                                               daemon=True)
            waits["thread"].start()
            time.sleep(0.3)
            rpc.messages.append(runtime.Error("stream disconnected before completion"))

        double, session = self.coordinator(crew, [failing])
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(session)
        self.assertIsNotNone(caught.exception.retry)
        self.assertIsNone(crew.closed)
        waits["thread"].join(5)
        self.assertEqual(waits.get("dead"), team.RELEASED_WAIT)
        # A time check of the next attempt ends that attempt's wait, not the ended one's.
        crew.attention("a time check")
        early = threading.Thread(target=lambda: waits.setdefault("next", crew.call("team_wait", {})), daemon=True)
        early.start()
        early.join(5)
        self.assertTrue(waits.get("next", "").startswith("The wait ended early: TCS Prover sent you a time check"),
                        waits.get("next"))
        self.finish(crew, release)
        self.assertIn("Done: Lemma A", crew.call("team_wait", {"timeout_minutes": 1}))

    def test_a_live_instruction_or_a_time_check_ends_a_running_wait_early(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}])
        answers = {}

        def turn(rpc):
            crew.call("team_assign", {"member": 1, "assignment": "Lemma A SLOW"})
            waiting = threading.Thread(target=lambda: answers.setdefault("wait", crew.call("team_wait", {})))
            waiting.start()
            time.sleep(0.3)
            double.command = ("steer-1", "Try Lemma B first.")
            waiting.join(20)
            double.service.call("update_goal", {"status": "complete"})
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double, session = self.coordinator(crew, [turn])
        crew_wait = time.monotonic()
        self.assertEqual(next(session)["output"], POINTER)
        self.assertLess(time.monotonic() - crew_wait, 20)
        self.assertTrue(answers["wait"].startswith("The wait ended early: TCS Prover sent you a live instruction"))
        self.assertIn("Try Lemma B first.", [params["input"][0]["text"] for method, params in double.rpc.calls
                                             if method == "turn/steer"])


class ReportBudgetTests(unittest.TestCase):
    """One team_wait answer stays within the output budget Codex 0.160 gives a tool's answer: 10,000 tokens, which
    it counts as about 4 bytes each. Captured with the real Codex: an answer of two 19,500-character reports came to
    the model cut out of its middle ("Warning: truncated output"), and more reports would lose whole headers."""

    CODEX_BUDGET_BYTES = 10_000 * 4

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()

    def finished(self, replies, directory=None):
        """team_wait's answer once every member has finished, each with its reply."""

        members = team.team_members(None, "gpt-6.1-sol", "high", len(replies))
        crew = team.Team(members, prompts=TEXTS, directory=directory or self.directory,
                         attempt=lambda member, prompt, conversation, process: replies[member.number - 1],
                         configuration=lambda error, member: None, emit=lambda kind, **fields: None,
                         render=runtime.render_template, retry_note="")
        self.addCleanup(crew.close, "The test ended.")
        for number in range(1, len(replies) + 1):
            crew.call("team_assign", {"member": number, "assignment": f"Part {number}"})
        deadline = time.monotonic() + 20
        while crew.running() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(crew.running(), 0)
        return crew.call("team_wait", {"timeout_minutes": 1})

    @staticmethod
    def blocks(answer):
        """{member number: (header lines, report text)} of one answer."""

        found = {}
        for block in answer.split("\n\nMember "):
            block = block if block.startswith("Member ") else "Member " + block
            head, _, text = block.partition("\nREPORT:\n")
            found[int(head.split()[1])] = (head, text)
        return found

    def test_report_shares_split_the_budget_and_give_unused_room_to_longer_reports(self):
        self.assertEqual(team.report_shares([13, 19_500, 30_000]), [13, 9_993, 9_994])
        self.assertEqual(team.report_shares([30_000, 30_000]), [10_000, 10_000])
        self.assertEqual(team.report_shares([5, 7]), [5, 7])
        self.assertEqual(team.report_shares([25_000]), [team.REPORT_CHARS])
        self.assertEqual(team.report_shares([]), [])

    def test_reports_that_come_back_together_share_one_answer_each_with_its_header_and_file(self):
        replies = ["Short report.", "A" * 19_500, "B" * 30_000]
        answer = self.finished(replies)
        self.assertLess(len(answer.encode("utf-8")), self.CODEX_BUDGET_BYTES)
        blocks = self.blocks(answer)
        self.assertEqual(sorted(blocks), [1, 2, 3])
        for number, share in ((2, 9_993), (3, 9_994)):
            head, text = blocks[number]
            self.assertIn(f'Member {number} (GPT 6.1 Sol) finished the assignment "Part {number}".', head)
            self.assertIn(f"Report file: team/member-{number}/1-report.md", head)
            self.assertEqual(text, replies[number - 1][:share] + f"\n[The report is cut here at {share:,} characters; "
                                   f"the whole report is in team/member-{number}/1-report.md.]")
            # The saved file keeps the whole report.
            self.assertEqual((self.directory / f"team/member-{number}/1-report.md").read_text(), replies[number - 1])
        self.assertEqual(blocks[1][1], "Short report.")

    def test_a_report_alone_keeps_its_20000_characters(self):
        answer = self.finished(["C" * 25_000])
        (head, text), = self.blocks(answer).values()
        self.assertEqual(text, "C" * 20_000 + "\n[The report is cut here at 20,000 characters; the whole report is "
                                              "in team/member-1/1-report.md.]")

    def test_a_cut_report_that_could_not_be_saved_names_no_file(self):
        (self.directory / "team").write_text("not a folder")
        answer = self.finished(["D" * 25_000])
        (head, text), = self.blocks(answer).values()
        self.assertIn("The report could not be saved", head)
        self.assertTrue(text.endswith("\n[The report is cut here at 20,000 characters; it could not be saved.]"))
        self.assertNotIn("team/member-1/1-report.md", text)


class ReportNumberingTests(unittest.TestCase):
    """Each runner of a job (after a pause and resume, a restart, a resume after the time limit) makes a team of its
    own in the same working folder: its assignments continue each member's numbering there, so an earlier runner's
    report file, which the coordinator was told about, and the rows of its assignment are never replaced."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.records = []

    def team(self, attempt):
        crew = team.Team(team.team_members(None, "gpt-6.1-sol", "high", 2), prompts=TEXTS, directory=self.directory,
                         attempt=attempt, configuration=lambda error, member: None,
                         emit=lambda kind, **fields: self.records.append({"kind": kind, **fields}),
                         render=runtime.render_template, retry_note="")
        self.addCleanup(crew.close, "The test ended.")
        return crew

    @staticmethod
    def done(member, prompt, conversation, process):
        return "Done: " + prompt.splitlines()[0]

    def test_a_later_runners_team_numbers_its_assignments_after_the_earlier_runners(self):
        first = self.team(self.done)
        first.call("team_assign", {"member": 1, "assignment": "Lemma A: the base case"})
        self.assertIn("Report file: team/member-1/1-report.md", first.call("team_wait", {"timeout_minutes": 1}))
        first.close("It stopped when the author paused.")
        second = self.team(self.done)  # The runner of the resumed job.
        second.call("team_assign", {"member": 1, "assignment": "Lemma B: the induction step"})
        self.assertIn("Report file: team/member-1/2-report.md", second.call("team_wait", {"timeout_minutes": 1}))
        folder = self.directory / "team" / "member-1"
        self.assertEqual((folder / "1-report.md").read_text(), "Done: Lemma A: the base case")
        self.assertEqual((folder / "2-report.md").read_text(), "Done: Lemma B: the induction step")
        self.assertEqual([record["subagentId"] for record in self.records if record.get("subagentState") == "started"],
                         ["member-1-1", "member-1-2"])
        # A member without an earlier assignment starts at 1.
        second.call("team_assign", {"member": 2, "assignment": "Lemma C"})
        self.assertIn("Report file: team/member-2/1-report.md", second.call("team_wait", {"timeout_minutes": 1}))

    def test_an_assignment_takes_its_number_as_it_starts(self):
        # A runner that ends before the report (killed with its members) leaves the assignment's empty report file,
        # so the next runner's first assignment takes the next number.
        release = threading.Event()

        def held(member, prompt, conversation, process):
            release.wait(20)
            return "Late report."

        first = self.team(held)
        self.addCleanup(release.set)  # Before the team closes: its member then ends at once.
        first.call("team_assign", {"member": 1, "assignment": "Lemma A"})
        reserved = self.directory / "team" / "member-1" / "1-report.md"
        self.assertEqual(reserved.read_text(), "")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(reserved.stat().st_mode), 0o600)
        second = self.team(self.done)
        second.call("team_assign", {"member": 1, "assignment": "Lemma B"})
        self.assertIn("Report file: team/member-1/2-report.md", second.call("team_wait", {"timeout_minutes": 1}))

    def test_numbering_never_reads_through_a_link(self):
        # A link in place of the member's folder (the model shares the working folder) is not followed to find
        # numbers: its target's files are not the member's reports.
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "7-report.md").write_text("Not a report of this job.")
        (self.directory / "team").mkdir()
        (self.directory / "team" / "member-1").symlink_to(outside, target_is_directory=True)
        self.assertEqual(team.highest_report(self.directory, 1), 0)
        with self.assertRaises((OSError, ValueError)):
            team.reserve_report(self.directory, 1, 1)
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["7-report.md"])


class WindowsInfo:
    """lstat information as Windows gives it for a junction: a folder with the mount-point reparse tag."""

    def __init__(self, info):
        self._info = info
        self.st_mode = stat.S_IFDIR | 0o700
        self.st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        self.st_reparse_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)

    def __getattr__(self, name):
        return getattr(self._info, name)


@unittest.skipIf(os.name == "nt", "Simulates Windows with POSIX links")
class SaveReportWindowsTests(unittest.TestCase):
    """save_report() where files cannot be opened relative to a folder without following links (Windows: no
    O_NOFOLLOW). A junction, which Path.is_symlink() does not report, is refused like any link, and a report is always
    a new file: an earlier file or link of its name is removed first, never truncated or written through."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.directory, self.outside = root / "run", root / "outside"
        self.directory.mkdir()
        self.outside.mkdir()

    def windows(self):
        real = os.lstat

        def lstat(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            return WindowsInfo(info) if stat.S_ISLNK(info.st_mode) else info

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(os, "O_NOFOLLOW", 0))
        stack.enter_context(mock.patch.object(os, "lstat", lstat))
        stack.enter_context(mock.patch.object(Path, "lstat", lambda path: lstat(path)))
        return stack

    def test_a_junction_in_place_of_the_members_folder_is_refused(self):
        (self.directory / "team").mkdir()
        (self.directory / "team" / "member-1").symlink_to(self.outside, target_is_directory=True)
        for windows in (True, False):
            with self.subTest(windows=windows), (self.windows() if windows else contextlib.ExitStack()):
                if windows:
                    self.assertFalse((self.directory / "team" / "member-1").is_symlink())  # As Windows says.
                with self.assertRaises((OSError, ValueError)):
                    team.save_report(self.directory, 1, 1, "The member's report.")
                self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_planted_hard_link_is_replaced_and_its_target_kept(self):
        kept = self.outside / "notes.md"
        for windows in (True, False):
            with self.subTest(windows=windows), (self.windows() if windows else contextlib.ExitStack()):
                kept.write_text("Keep me.")
                folder = self.directory / "team" / "member-1"
                folder.mkdir(parents=True, exist_ok=True)
                planted = folder / "2-report.md"
                if planted.exists():
                    planted.unlink()
                os.link(kept, planted)
                path = team.save_report(self.directory, 1, 2, "The member's report.")
                self.assertEqual(path.read_text(), "The member's report.")
                self.assertEqual(kept.read_text(), "Keep me.")
                self.assertEqual(os.stat(path).st_nlink, 1)


class ExecuteTeamTests(unittest.TestCase):
    """How _execute starts an author coordinator with TCS Prover's team (the session itself is a stub)."""

    STATEMENT = "Prove S."

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.workflow = runtime.load_workflow(WORKFLOW)
        self.events = []
        handoff = mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True)
        handoff.start()
        self.addCleanup(handoff.stop)

    def launch(self, options=None, claude=False, policy=None):
        calls = []

        def session(module, prompt, **kwargs):
            calls.append((prompt, kwargs))
            yield {"outcome": "done", "output": "A complete inline proof."}

        codex = mock.patch.object(runtime, "goal_session", side_effect=AssertionError("Codex") if claude else session)
        anthropic = mock.patch.object(claude_cli, "goal_session", side_effect=session if claude else AssertionError("Claude"))
        with codex, anthropic, mock.patch.object(runtime, "retrieval_policy", return_value=policy), \
                mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields:
                                  self.events.append({"kind": kind, "stage": stage, **fields})):
            runtime._execute(self.workflow, {"statement": self.STATEMENT},
                             {"workflow_stages": coordinator_test.AUTHOR_ONLY, "goal_cwd": str(self.directory),
                              "related_works": True, **(options or {})}, self.workflow["prompts"])
        (prompt, kwargs), = calls
        return prompt, kwargs

    def test_the_coordinator_gets_the_team_note_its_team_and_no_subagent_tool(self):
        models = [{"model": "gpt-6.1-sol", "effort": "high"}, {"model": "claude-opus-5-5", "effort": "max"},
                  {"model": "gpt-6-luna", "effort": "low"}]
        prompt, kwargs = self.launch({"author_team": "tcs", "goal_loop": "tcs", "author_subagent_models": models,
                                      "author_model": "gpt-5.6-luna"})
        note = runtime.render_template(TEXTS["team_note"], {"members": "\n".join([
            "- member 1: GPT 6.1 Sol (Codex CLI)", "- member 2: Claude Opus 5.5 (Claude Code)",
            "- member 3: GPT 6 Luna (Codex CLI)"])})
        works = self.workflow["prompts"]["related_works"]
        # After the related works; {original_prompt} carries it into the later prompts.
        self.assertTrue(prompt.endswith(works + "\n\n" + note), prompt[-600:])
        self.assertIsInstance(kwargs["team"], team.Team)
        self.assertIsInstance(kwargs["service"], team.Service)
        self.assertTrue(kwargs["coordinator"])  # Even a model with no Codex subagent tool coordinates its team.
        self.assertNotIn("multi_agent", kwargs["features"])
        self.assertEqual(kwargs["texts"]["goal_continuation"], TEXTS["goal_continuation"])
        self.assertEqual(kwargs["prompts"]["continuation"], self.workflow["prompts"]["coordinator_continuation"])
        # Same as the coordinator: every member runs its model.
        prompt, kwargs = self.launch({"author_team": "tcs"})
        self.assertEqual([member.model for member in kwargs["team"].members.values()], [runtime.MODEL] * 3)

    def test_a_claude_coordinator_has_no_plan_to_work_alone(self):
        # Claude Code loads TCS Prover's "sdk" server also with --bare (2.1.289), so the team stays in every mode.
        prompt, kwargs = self.launch({"author_team": "tcs", "author_model": "claude-opus-5-5"}, claude=True)
        self.assertIsInstance(kwargs["team"], team.Team)
        self.assertTrue(kwargs["coordinator"])
        self.assertNotIn("alone", kwargs)
        # A Claude coordinator with the CLI's own subagents still has one, for a CLI that offers no subagent tool.
        prompt, kwargs = self.launch({"author_team": "builtin", "author_model": "claude-opus-5-5"}, claude=True)
        self.assertIn("alone", kwargs)

    def test_jobs_from_before_and_web_cutoff_runs_keep_the_built_in_subagents(self):
        for options, policy in (({}, None), ({"author_team": "builtin"}, None),
                                ({"author_team": "tcs"}, {"isolation": {"workspace": "/run"}})):
            with self.subTest(options=options, policy=bool(policy)):
                self.events.clear()
                prompt, kwargs = self.launch(options, policy=policy)
                self.assertNotIn("team", kwargs)
                self.assertNotIn("service", kwargs)
                self.assertIn("multi_agent", kwargs["features"])
                self.assertNotIn("YOUR TEAM\nYour author subagents are TCS Prover team members", prompt)
                self.assertEqual(bool(policy), "Built-in subagents" in [event.get("label") for event in self.events])

    def test_a_wrong_number_of_member_models_stops_before_any_model_starts(self):
        with mock.patch.object(runtime, "goal_session", side_effect=AssertionError("started")), \
                mock.patch.object(runtime, "emit"):
            state = runtime._execute(self.workflow, {"statement": self.STATEMENT},
                                     {"workflow_stages": coordinator_test.AUTHOR_ONLY, "goal_cwd": str(self.directory),
                                      "author_team": "tcs", "author_subagent_models": [
                                          {"model": "gpt-6.1-sol", "effort": "high"}]},
                                     self.workflow["prompts"])
        self.assertTrue(state["paused"])


class CommandLineTests(unittest.TestCase):
    def test_the_runner_defaults_to_tcs_prover_s_loop_and_team_and_reads_a_jobs_saved_texts(self):
        with tempfile.TemporaryDirectory() as folder:
            texts = Path(folder) / "prompt-set.json"
            texts.write_text(json.dumps({"goal_continuation": "Edited {continuation}", "author": "Mine [STATEMENT]"}))
            captured = {}

            def execute(paths, state, options):
                captured.update(options)
                return {"paused": True}

            with mock.patch.object(runtime, "execute_workflows", side_effect=execute), \
                    mock.patch.object(runtime, "check_clis"), mock.patch.object(claude_cli, "sweep_stale_settings"), \
                    mock.patch.object(runtime.sys, "stdin", __import__("io").StringIO("Task")):
                runtime.main([str(WORKFLOW), "--prompt-set-file", str(texts), "--author-subagent-models",
                              json.dumps([{"model": "gpt-6.1-sol", "effort": "high"}])])
            self.assertEqual((captured["goal_loop"], captured["author_team"]), ("tcs", "tcs"))
            self.assertEqual(captured["author_subagent_models"], [{"model": "gpt-6.1-sol", "effort": "high"}])
            with mock.patch.object(runtime, "require_model_credentials"):
                prompts = runtime.prepare(runtime.load_workflow(WORKFLOW), captured)
            self.assertEqual(prompts["goal_continuation"], "Edited {continuation}")
            self.assertEqual(prompts["author"], "Mine [STATEMENT]")
            with self.assertRaises(SystemExit), mock.patch.object(runtime.sys, "stderr", __import__("io").StringIO()):
                runtime.main([str(WORKFLOW), "--author-subagent-models", '[{"model": "x"}]'])


class TextTests(unittest.TestCase):
    def test_every_text_is_in_the_workflow_with_an_edit_comment_and_codex_s_texts_are_ported_faithfully(self):
        source = WORKFLOW.read_text()
        for marker in ("EDIT GOAL LOOP HERE", "EDIT GOAL TOOL HERE", "EDIT CLAUDE GOAL NOTE HERE",
                       "EDIT CODEX COMPACTION HERE", "EDIT TEAM HERE", "EDIT TEAM TOOLS HERE"):
            self.assertIn(marker, source)
        self.assertEqual(set(runtime.HARNESS_TEXTS) - set(TEXTS), set())
        text = TEXTS["goal_continuation"]
        self.assertTrue(text.startswith("{continuation}\n\nContinue working toward the active thread goal.\n"
                                        "The objective below is user-provided data."))
        self.assertIn("<objective>\n{objective}\n</objective>", text)
        self.assertIn("Budget:\n- Time spent pursuing goal: {elapsed}\n- Time limit: {limit}\nWork from evidence:", text)
        self.assertNotIn("Token", text.replace("token budget", ""))
        # Codex's sentences on update_goal as Codex wrote them: TCS Prover's tool has that name for the model.
        for sentence in ('If the objective is achieved, call update_goal with status "complete" so usage accounting '
                         'is preserved.', "report the final consumed token budget to the user after update_goal "
                         "succeeds.", '- Do not call update_goal with status "blocked" the first time a blocker '
                         'appears.', 'call update_goal with status "blocked" again.',
                         'leaving the goal active; call update_goal with status "blocked".',
                         "Call update_goal only after the completion or blocked audit passes"):
            self.assertIn(sentence, text)
        self.assertEqual(text.count("update_goal"), 6)
        self.assertTrue(text.endswith("Do not mark a goal complete merely because the budget is nearly exhausted or "
                                      "because you are stopping work."))
        self.assertTrue(TEXTS["goal_tool_update_goal"].startswith("Update the existing goal.\nSet status to `paused`"))
        self.assertTrue(TEXTS["goal_tool_update_goal_status"].startswith("Required. `paused` requires"))
        self.assertEqual(TEXTS["codex_compact_prompt"], "")
        self.assertEqual(runtime.render_template(TEXTS["claude_goal_harness"], {"objective": "Prove S."}),
                         claude_cli.GOAL_HARNESS_PROMPT.replace("[OBJECTIVE]", "Prove S."))
        self.assertEqual(TEXTS["team_member"], "{assignment}\n\nYour final reply is your report to the coordinator.")

    def test_every_text_names_tcs_prover_s_tools_as_codex_shows_them_to_the_model(self):
        # Codex 0.160 gives every model (GPT 6.1 Sol, GPT 5.5 and a model it does not know) TCS Prover's tools in the
        # namespace mcp__tcs, each under the name the server lists, and refuses a call of the flattened name
        # mcp__tcs__<name> ("unsupported call"; captured with the real Codex against a scripted model server). So
        # the default texts and the tools' answers name each tool by that name, and never otherwise.
        crew = team.Team([], prompts=TEXTS, directory=".", attempt=None, configuration=None, emit=None, render=None,
                         retry_note="")
        listed = {tool["name"] for provider in (team.GoalTool(TEXTS, None, None, None), crew)
                  for tool in provider.tools()}
        self.assertEqual(listed, {"update_goal", "team_members", "team_assign", "team_wait", "team_followup",
                                  "team_stop"})
        named = re.compile(r"\b(?:mcp__\w+|update_goal|team_[a-z]+)\b")
        found = {}
        for name in runtime.HARNESS_TEXTS:
            for tool in named.findall(TEXTS[name]):
                self.assertIn(tool, listed, f"{name} names {tool}")
                found.setdefault(tool, name)
        # The answers of the tools: the texts of runner/team.py, its docstrings and the prompts' names aside.
        tree = ast.parse(Path(team.__file__).read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body
                      and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        answers = [node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                   and node.value not in runtime.HARNESS_TEXTS]
        self.assertTrue(any("Collect its report with team_wait." in answer for answer in answers))
        for tool in named.findall("\n".join(answers)):
            self.assertIn(tool, listed)
        # Every tool is named somewhere the coordinator or the author reads.
        self.assertEqual(set(found), listed)

    def test_edits_are_checked_as_the_runner_checks_them(self):
        from runner import prompt_catalog
        from ui.review import REVIEW_PROMPT
        entries = prompt_catalog.entries(runtime, REVIEW_PROMPT)
        names = [entry["name"] for entry in entries]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("convention", names)
        self.assertNotIn("convention_critic", names)
        self.assertEqual(prompt_catalog.problems(runtime, {entry["name"]: entry["text"] for entry in entries}), {})
        workflows = [runtime.load_workflow(WORKFLOW), runtime.load_workflow(ROOT / "workflows/clean_up.yaml")]
        for entry in entries:
            if entry["name"] == "review" or not any(item.startswith("{") for item in entry["placeholders"]):
                continue
            with self.subTest(prompt=entry["name"]):
                broken = entry["text"] + " {not_a_field}"
                self.assertIn(entry["name"], prompt_catalog.problems(runtime, {entry["name"]: broken}))
                name = prompt_catalog.workflow_name(entry["name"])
                workflow = next(item for item in workflows if name in item["prompts"])
                with mock.patch.object(runtime, "require_model_credentials"), self.assertRaises(ValueError):
                    runtime.prepare(workflow, {"author_subagents": 3, "author_mode": "coordinator",
                                               "prompt_set": {name: broken}})
        self.assertEqual(prompt_catalog.problems(runtime, {"codex_compact_prompt": ""}), {})
        self.assertIn("author", prompt_catalog.problems(runtime, {"author": "No marker"}))
        self.assertIn("nothing", prompt_catalog.problems(runtime, {"nothing": "x"}))

    def test_the_reply_turns_and_the_related_works_note_are_checked_as_the_code_fills_them_in(self):
        # The paused author's reply turns fill in the reply's {message} and nothing in a progress summary (which is
        # still a template), and the related works note is appended to the first prompt as written: the Prompts
        # dialog lists and checks exactly that, and so does a launch, so no accepted text fails in a reply turn.
        from runner import prompt_catalog
        from ui.review import REVIEW_PROMPT
        entries = {entry["name"]: entry for entry in prompt_catalog.entries(runtime, REVIEW_PROMPT)}
        names = ("summary", "coordinator_summary", "conversation", "related_works")
        self.assertEqual([entries[name]["placeholders"] for name in names], [[], [], ["{message}"], []])
        self.assertEqual([entries[name]["template"] for name in names], [True, True, True, False])
        self.assertEqual([entries[name]["template"] for name in ("critic", "critic_verify", "team_tool_wait")],
                         [True, False, False])
        workflow = runtime.load_workflow(WORKFLOW)
        for name, text in (("summary", "Summarize your progress on {original_prompt}."),
                           ("summary", "A lone { brace."), ("coordinator_summary", "Summarize round {round}."),
                           ("conversation", "Reply to {message} in round {round}.")):
            with self.subTest(name=name, text=text):
                self.assertIn(name, prompt_catalog.problems(runtime, {name: text}))
                with mock.patch.object(runtime, "require_model_credentials"), self.assertRaises(ValueError):
                    runtime.prepare(workflow, {"author_subagents": 3, "author_mode": "coordinator",
                                               "prompt_set": {name: text}})
        accepted = {"summary": "Summarize the sets {{x}} you used.", "coordinator_summary": "Summarize {{it}}.",
                    "related_works": "RELATED WORKS: see {directory}/related-works, the set {{x}} and a lone }."}
        self.assertEqual(prompt_catalog.problems(runtime, accepted), {})
        with mock.patch.object(runtime, "require_model_credentials"):
            prompts = runtime.prepare(workflow, {"author_subagents": 3, "author_mode": "coordinator",
                                                 "prompt_set": accepted})
        self.assertEqual(prompts["related_works"], accepted["related_works"])

    def test_an_accepted_summary_is_sent_in_a_reply_turn(self):
        sent, records = [], []

        def session(module, instruction, **kwargs):
            sent.append(instruction)
            return {"reply": "Here is the summary.", "threadId": "t-1"}

        for mode, name in (({"author_subagents": 0}, "summary"),
                           ({"author_subagents": 3, "author_team": "tcs"}, "coordinator_summary")):
            with self.subTest(name=name), mock.patch.object(runtime, "author_conversation", side_effect=session), \
                    mock.patch.object(runtime, "emit", side_effect=lambda *args, **fields: records.append(fields)), \
                    mock.patch.object(runtime, "require_model_credentials"):
                code = runtime.run_author_conversation(
                    WORKFLOW, {"author_model": "gpt-6.1-sol", "prompt_set": {name: "Summarize the sets {{x}}."},
                               **mode}, {"kind": "summary", "id": "c_0123456789", "text": "", "notice": ""})
                self.assertEqual(code, 0)
                self.assertEqual(sent[-1], "Summarize the sets {x}.")
                self.assertEqual((records[-1].get("conversationKind"), records[-1].get("text")),
                                 ("summary", "Here is the summary."))

    def test_a_compaction_summary_reaches_codex_only_when_it_is_written(self):
        service = team.Service()
        self.addCleanup(service.close)

        def done(rpc):
            service.call("update_goal", {"status": "complete"})
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()):
            for compact, expected in (("", None), ("Summarize the proof state.", "Summarize the proof state.")):
                double = LoopDouble([done])
                session = runtime.goal_session(
                    double, "Prove S.", prompts=GOAL_PROMPTS, texts={**TEXTS, "codex_compact_prompt": compact},
                    service=service,
                    settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"},
                    options={"goal_cwd": folder, "goal_loop": "tcs"})
                next(session)
                session.close()
                start = next(params for method, params in double.rpc.calls if method == "thread/start")
                self.assertEqual(start["config"].get("compact_prompt"), expected)


class ClaudeCoordinatorTests(TeamCase):
    """A Claude coordinator against the stand-in CLI, which models Claude Code 2.1.289's MCP behaviour as captured
    from that binary (tests/test_claude_cli.py FAKE_CLAUDE): only an "sdk" server survives --safe-mode, and its
    messages travel as control requests that goal_session answers."""

    NAMES = [f"mcp__tcs__{name}" for name in team.TEAM_TOOLS]

    def coordinator(self, crew, turns, texts=TEXTS, model="claude-opus-5-5", extra=None, **options):
        self.scenario.write_text(json.dumps({"turns": turns, "secret": claude_test.SECRET, **(extra or {})}))
        service = team.Service()
        self.addCleanup(service.close)
        session = claude_cli.goal_session(
            runtime, "Coordinate the team on S.", prompts=claude_test.PROMPTS, coordinator=True, texts=texts,
            team=crew, service=service, features=[],
            settings={"model": model, "effort": "high", "speed": "standard", "summary": "concise", "subagents": 1},
            options={"goal_cwd": str(self.run_dir), "goal_pause_file": str(self.run_dir / "pause-request.json"),
                     "author_steer_file": str(self.run_dir / "author-steer.json"), **options})
        self.addCleanup(session.close)
        return service, session

    def logged(self, event):
        return [row for row in map(json.loads, self.claude_log.read_text().splitlines()) if row["event"] == event]

    def results(self):
        return {row["name"]: row for row in self.logged("mcp_result")}

    def test_a_claude_coordinator_gets_the_team_through_tcs_prover_s_server_and_no_agent_tool(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        service, session = self.coordinator(crew, [[
            "init", "echo", {"mcp_call": {"name": "mcp__tcs__team_members"}},
            {"mcp_call": {"name": "mcp__tcs__team_assign", "arguments": {"member": 1, "assignment": "Lemma A"}}},
            {"mcp_call": {"name": "mcp__tcs__team_wait", "arguments": {"timeout_minutes": 1}}},
            {"mcp_call": {"name": "mcp__tcs__update_goal", "arguments": {"status": "complete"}}},
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]])
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            self.assertEqual(next(session)["output"], POINTER)
        start, = self.claude_starts()
        argv = start["argv"]
        tools = argv[argv.index("--tools") + 1].split(",")
        self.assertFalse({"Task", "Agent"} & set(tools))
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertEqual([tool for tool in allowed if tool.startswith("mcp__")], self.NAMES)
        self.assertNotIn("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", start["env"])
        # The isolation stays as for every Claude process: --safe-mode, which keeps only an "sdk" server.
        self.assertIn("--safe-mode", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(start["mcp_config"], {"tcs": {"type": "sdk", "name": "tcs",
                                                       "timeout": team.TOOL_TIMEOUT_SECONDS * 1000,
                                                       "alwaysLoad": True}})
        self.assertEqual(start["mcp_dropped"], [])
        self.assertEqual(start["mcp_config_mode"], "0o600")
        self.assertNotIn(service.token, json.dumps(start))
        connected, = self.logged("mcp_connected")
        self.assertEqual(connected["initialize"]["response"]["mcp_response"]["result"]["serverInfo"]["name"], "tcs")
        self.assertEqual([tool["name"] for tool in connected["tools"]], list(team.TEAM_TOOLS))
        self.assertEqual(connected["tools"][1]["description"], TEXTS["team_tool_assign"])
        results = self.results()
        self.assertEqual(results["mcp__tcs__team_members"]["content"],
                         [{"type": "text", "text": "member 1: GPT 6.1 Sol (Codex CLI): idle"}])
        self.assertIn("Report of gpt-6.1-sol: Lemma A", results["mcp__tcs__team_wait"]["content"][0]["text"])
        self.assertFalse(results["mcp__tcs__team_wait"]["is_error"])
        # A Claude coordinator ends its goal with its structured status: it has no update_goal.
        self.assertEqual(results["mcp__tcs__update_goal"]["content"],
                         "<tool_use_error>Error: No such tool available: mcp__tcs__update_goal</tool_use_error>")
        self.assertEqual(start["harness"], runtime.render_template(TEXTS["claude_goal_harness"],
                                                                  {"objective": claude_test.PROMPTS["goal"]}))
        session.close()
        self.assertIsNotNone(crew.closed)
        self.assertFalse(Path(argv[argv.index("--mcp-config") + 1]).exists())

    def test_a_coordinator_whose_cli_offers_no_team_tools_ends_the_attempt(self):
        # Claude Code starts without a server it cannot load (a dropped or failed one), so the init's tool list is
        # the check: the attempt ends, to be retried, rather than the coordinator working on without its team.
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        _, session = self.coordinator(crew, [["init", "echo", {"structured": {
            "goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]],
            extra={"mcp_unavailable": True})
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(session)
        self.assertIsNotNone(caught.exception.retry)
        self.assertIn("Claude Code did not offer the coordinator its team's tools (mcp__tcs__team_members",
                      caught.exception.reason)
        self.assertIn("TCS Prover's MCP server: failed.", caught.exception.reason)
        self.assertIsNone(crew.closed)  # A retried attempt keeps the team.

    def test_a_refused_call_is_the_tools_error_result(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        _, session = self.coordinator(crew, [[
            "init", "echo",
            {"mcp_call": {"name": "mcp__tcs__team_assign", "arguments": {"member": 9, "assignment": "x"}}},
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]])
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            self.assertEqual(next(session)["output"], POINTER)
        refused = self.results()["mcp__tcs__team_assign"]
        self.assertEqual((refused["content"], refused["is_error"]), ("There is no member 9; the members are 1 to 1.",
                                                                     True))

    def test_a_live_instruction_ends_a_running_team_wait_and_reaches_the_coordinator(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        steer = self.run_dir / "author-steer.json"
        _, session = self.coordinator(crew, [[
            "init", "echo",
            {"mcp_call": {"name": "mcp__tcs__team_assign", "arguments": {"member": 1, "assignment": "Lemma A SLOW"}}},
            # The instruction comes while the wait runs (the member works for 120 s).
            {"mcp_call": {"name": "mcp__tcs__team_wait", "arguments": {"timeout_minutes": 1}}},
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]],
            extra={"after_call": {"mcp__tcs__team_wait": {"path": str(steer), "text": json.dumps(
                {"queue": [{"id": "steer-1", "instruction": "Try Lemma B first."}]})}}})
        began = time.monotonic()
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            self.assertEqual(next(session)["output"], POINTER)
        self.assertLess(time.monotonic() - began, 60)
        wait = self.results()["mcp__tcs__team_wait"]["content"][0]["text"]
        self.assertTrue(wait.startswith("The wait ended early: TCS Prover sent you a live instruction"), wait)
        self.assertIn('member 1: GPT 6.1 Sol (Codex CLI): working on "Lemma A SLOW"', wait)
        # Claude Code queued the instruction during the call and took it in after the result.
        sent = [row["message"] for row in self.logged("input") if row["message"].get("type") == "user"]
        self.assertEqual(sent[-1]["message"]["content"][0]["text"], "Try Lemma B first.")
        accepted, = self.labelled("Author instruction accepted")
        self.assertEqual(accepted["authorSteerDelivered"], "steer-1")

    def test_a_pause_during_a_team_wait_cancels_it_and_stops_the_members(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        pause = self.run_dir / "pause-request.json"
        _, session = self.coordinator(crew, [[
            "init", "echo",
            {"mcp_call": {"name": "mcp__tcs__team_assign", "arguments": {"member": 1, "assignment": "Lemma A SLOW"}}},
            {"mcp_call": {"name": "mcp__tcs__team_wait", "arguments": {"timeout_minutes": 1}}},
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]],
            extra={"after_call": {"mcp__tcs__team_wait": {"path": str(pause), "text": "{}"}}})
        began = time.monotonic()
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(session)
        self.assertLess(time.monotonic() - began, 60)  # The member's 120 s run was stopped.
        self.assertIsNone(caught.exception.retry)
        self.assertTrue(self.results()["mcp__tcs__team_wait"]["interrupted"])
        self.assertEqual(crew.closed, "It stopped when the author paused.")
        failed, = self.labelled("Author subagent failed")
        self.assertEqual((failed["subagentMember"], failed["text"]), (1, "It stopped when the author paused."))
        self.assertIn("Author paused", [event.get("label") for event in self.events])

    def test_a_wait_of_a_failed_attempt_leaves_the_report_to_the_next_one(self):
        # team-decisions item 5, Claude Code: the CLI ends (a failed model call) while the runner answers its
        # team_wait; the attempt is retried with the team working on, and the next attempt's team_wait gets the report.
        release = self.held_member()
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        _, session = self.coordinator(crew, [[
            "init", "echo",
            {"mcp_call": {"name": "mcp__tcs__team_assign", "arguments": {"member": 1, "assignment": "Lemma A"}}},
            {"mcp_call": {"name": "mcp__tcs__team_wait", "arguments": {"timeout_minutes": 1}}},
            {"stderr": "fatal: connection reset"}, {"exit": 3}]], extra={"control_seconds": 1})
        with self.assertRaises(runtime.WorkflowPaused) as caught:
            next(session)
        self.assertIsNotNone(caught.exception.retry)
        self.assertIsNone(crew.closed)
        self.finish(crew, release)
        self.assertIn("Done: Lemma A", crew.call("team_wait", {"timeout_minutes": 1}))

    def test_a_coordinator_in_bare_mode_keeps_its_team(self):
        # An API model whose claude command drops CLAUDE_CONFIG_DIR runs with --bare, which Claude Code 2.1.289
        # gives TCS Prover's "sdk" server too: the coordinator keeps its team instead of working alone.
        model = providers.save_api_model({"format": "anthropic", "baseUrl": "https://gateway.example.com/v1",
                                          "headerName": "x-api-key", "model": "gateway-claude",
                                          "apiKey": claude_test.SECRET, "label": "Gateway"})["id"]
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator=model)
        alone = {"prompt": "Prove S alone.", "prompts": dict(claude_test.PROMPTS), "recovery": ""}
        self.scenario.write_text(json.dumps({"turns": [[
            "init", "echo",
            {"mcp_call": {"name": "mcp__tcs__team_assign", "arguments": {"member": 1, "assignment": "Lemma A"}}},
            {"mcp_call": {"name": "mcp__tcs__team_wait", "arguments": {"timeout_minutes": 1}}},
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]],
            "secret": claude_test.SECRET, "drops_config_dir": True}))
        service = team.Service()
        self.addCleanup(service.close)
        session = claude_cli.goal_session(
            runtime, "Coordinate the team on S.", prompts=claude_test.PROMPTS, coordinator=True, texts=TEXTS,
            team=crew, service=service, features=[], alone=alone,
            settings={"model": model, "effort": "high", "speed": "standard", "summary": "concise", "subagents": 1},
            options={"goal_cwd": str(self.run_dir)})
        self.addCleanup(session.close)
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            self.assertEqual(next(session)["output"], POINTER)
        start, = self.claude_starts()
        self.assertIn("--bare", start["argv"])
        sent = [row["message"] for row in self.logged("input") if row["message"].get("type") == "user"]
        self.assertEqual(sent[0]["message"]["content"][0]["text"], "Coordinate the team on S.")
        self.assertIn("Report of gpt-6.1-sol: Lemma A", self.results()["mcp__tcs__team_wait"]["content"][0]["text"])
        self.assertNotIn("No subagents with this model", [event.get("label") for event in self.events])

    def test_the_goal_note_is_the_jobs_text(self):
        _, session = self.coordinator(None, [["init", "echo", {"structured": {
            "goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]],
            texts={**TEXTS, "claude_goal_harness": "MY NOTE: {objective}"})
        self.assertEqual(next(session)["output"], POINTER)
        start, = self.claude_starts()
        self.assertEqual(start["harness"], "MY NOTE: " + claude_test.PROMPTS["goal"])
        self.assertIn(claude_cli.EMPTY_MCP_CONFIG, start["argv"])
