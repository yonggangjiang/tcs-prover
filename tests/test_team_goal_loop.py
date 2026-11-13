"""TCS Prover's goal loop (Goal loop "TCS Prover"), its MCP server "tcs" and the author coordinator's team.

Offline: the Codex author or coordinator runs against the scripted RPC double of tests/test_goal_runtime.py, the
Claude coordinator against the stand-in CLI of tests/test_claude_cli.py, and the tests play the CLI's part as an MCP
client: they start the real bridge (runner/tcs_mcp.py) exactly as the CLI's settings name it and call its tools.
Team members run stand-in `codex` and `claude` programs (no model). No real CLI is started.
"""

import json
import os
from pathlib import Path
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
from runner import claude_cli, confinement, providers, team

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
# from its prompt. Markers in the prompt choose a failure: SLOW (works until stopped), FAIL-ONCE (a model-call
# failure after some work, once per run folder), NOLOGIN (a login the CLI refuses).
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
out({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking about " + prompt[:40]}})
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
out({"type": "item.completed", "item": {"type": "agent_message", "text": reply}})
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
        claude = self.service.claude_config()["mcpServers"]["tcs"]
        self.assertEqual((claude["command"], claude["args"], claude["env"], claude["timeout"]),
                         (sys.executable, ["-I", str(team.BRIDGE)], self.service.environment(),
                          team.TOOL_TIMEOUT_SECONDS * 1000))

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
        self.assertEqual(client.tool("echo", {"y": 2}), ('echo {"y": 2}', False))
        self.provider.release.set()
        self.assertEqual(client.answer(blocked)["result"]["content"][0]["text"], "released")

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
        # The coordinator has TCS Prover's tools, and none of Codex's own subagent tools.
        self.assertEqual([tool["name"] for tool in seen["tools"]], ["update_goal", *team.TEAM_TOOLS])
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
        self.assertEqual(text.count("mcp__tcs__update_goal"), 6)
        self.assertNotIn(" update_goal", text.replace("mcp__tcs__update_goal", ""))
        self.assertTrue(text.endswith("Do not mark a goal complete merely because the budget is nearly exhausted or "
                                      "because you are stopping work."))
        self.assertTrue(TEXTS["goal_tool_update_goal"].startswith("Update the existing goal.\nSet status to `paused`"))
        self.assertTrue(TEXTS["goal_tool_update_goal_status"].startswith("Required. `paused` requires"))
        self.assertEqual(TEXTS["codex_compact_prompt"], "")
        self.assertEqual(runtime.render_template(TEXTS["claude_goal_harness"], {"objective": "Prove S."}),
                         claude_cli.GOAL_HARNESS_PROMPT.replace("[OBJECTIVE]", "Prove S."))
        self.assertEqual(TEXTS["team_member"], "{assignment}\n\nYour final reply is your report to the coordinator.")

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
    def coordinator(self, crew, turns, texts=TEXTS, **options):
        self.scenario.write_text(json.dumps({"turns": turns, "secret": claude_test.SECRET}))
        service = team.Service()
        self.addCleanup(service.close)
        session = claude_cli.goal_session(
            runtime, "Coordinate the team on S.", prompts=claude_test.PROMPTS, coordinator=True, texts=texts,
            team=crew, service=service, features=[],
            settings={"model": "claude-opus-5-5", "effort": "high", "speed": "standard", "summary": "concise",
                      "subagents": 1},
            options={"goal_cwd": str(self.run_dir), **options})
        self.addCleanup(session.close)
        return session

    def test_a_claude_coordinator_gets_the_team_through_tcs_prover_s_server_and_no_agent_tool(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        names = [f"mcp__tcs__{name}" for name in team.TEAM_TOOLS]
        session = self.coordinator(crew, [[{"init": {"extra_tools": names}}, "echo",
                                           {"structured": {"goal_status": "complete", "final_answer": POINTER,
                                                           "progress_note": ""}}]])
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            self.assertEqual(next(session)["output"], POINTER)
        start, = self.claude_starts()
        argv = start["argv"]
        tools = argv[argv.index("--tools") + 1].split(",")
        self.assertFalse({"Task", "Agent"} & set(tools))
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertEqual([tool for tool in allowed if tool.startswith("mcp__")], names)
        self.assertNotIn("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", start["env"])
        self.assertIn("--strict-mcp-config", argv)
        config_path = Path(argv[argv.index("--mcp-config") + 1])
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        config = json.loads(config_path.read_text())
        self.assertEqual(list(config["mcpServers"]), ["tcs"])
        # The CLI's side: its MCP client starts the bridge from that file and calls the team tools.
        client = MCPClient(config["mcpServers"]["tcs"])
        self.addCleanup(client.close)
        self.assertEqual([tool["name"] for tool in client.request("tools/list", {})["result"]["tools"]],
                         list(team.TEAM_TOOLS))
        self.assertFalse(client.tool("team_assign", {"member": 1, "assignment": "Lemma A"})[1])
        text, _ = client.tool("team_wait", {"timeout_minutes": 1}, timeout=90)
        self.assertIn("Report of gpt-6.1-sol: Lemma A", text)
        self.assertEqual(start["harness"], runtime.render_template(TEXTS["claude_goal_harness"],
                                                                  {"objective": claude_test.PROMPTS["goal"]}))
        session.close()
        self.assertIsNotNone(crew.closed)
        self.assertFalse(config_path.exists())

    def test_the_goal_note_is_the_jobs_text(self):
        session = self.coordinator(None, [["init", "echo", {"structured": {
            "goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]],
            texts={**TEXTS, "claude_goal_harness": "MY NOTE: {objective}"})
        self.assertEqual(next(session)["output"], POINTER)
        start, = self.claude_starts()
        self.assertEqual(start["harness"], "MY NOTE: " + claude_test.PROMPTS["goal"])
        self.assertIn(claude_cli.EMPTY_MCP_CONFIG, start["argv"])
