"""Claude CLI transport with a scripted stand-in `claude` first on PATH; no model calls."""

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

import workflow_runner as runtime
from runner import claude_cli, providers

SECRET = "sk-ant-test-0123456789abcdefSECRET"
PROMPTS = {
    "goal": "Prove the exact statement.",
    "continuation": "Continue the same goal in this session.",
    "compaction": "Context was compacted; re-read your records in {directory}.",
    "repair": "Revision {revision_number}; critic round {round}: {bugs}\nCandidate: {solution}",
    "resume": "Resume in {directory}.\nOriginal assignment:\n{original_prompt}",
}
SCHEMA = {"type": "object", "properties": {"statement": {"type": "string"}, "notes": {"type": "string"}},
          "required": ["statement", "notes"], "additionalProperties": False}

# The stand-in records its argv, selected environment, settings file and every
# stdin line, then replays the scripted stream-json output of each turn. Like
# Claude Code it keeps a transcript per persisted session (user and assistant
# messages linked by parentUuid) under $CLAUDE_CONFIG_DIR/projects, copies it
# for --fork-session and branches it at --resume-session-at.
FAKE_CLAUDE = r'''
import json, os, select, sys, time, uuid

LOG = os.environ["TCS_FAKE_CLAUDE_LOG"]
SCENARIO = json.load(open(os.environ["TCS_FAKE_CLAUDE_SCENARIO"]))
argv = sys.argv[1:]


def option(name):
    return argv[argv.index(name) + 1] if name in argv else None


def log(record):
    with open(LOG, "a") as stream:
        stream.write(json.dumps(record) + "\n")


secret = SCENARIO.get("secret") or "\0"
settings_path = option("--settings")
record = {"event": "start", "argv": argv, "cwd": os.getcwd(), "settings_path": settings_path,
          "env": {key: value for key, value in os.environ.items()
                  if key.startswith(("CLAUDE", "ANTHROPIC", "TCS_PROVER", "OPENAI", "CODEX"))},
          "secret_in_env": any(secret in value for value in os.environ.values()),
          "secret_in_argv": any(secret in value for value in argv)}
if settings_path:
    record["settings"] = json.load(open(settings_path))
    record["settings_mode"] = oct(os.stat(settings_path).st_mode & 0o777)
    record["settings_dir_mode"] = oct(os.stat(os.path.dirname(settings_path)).st_mode & 0o777)
harness_path = option("--append-system-prompt-file")
if harness_path:
    record["harness"] = open(harness_path, encoding="utf-8").read()
    record["harness_path"] = harness_path
    record["harness_mode"] = oct(os.stat(harness_path).st_mode & 0o777)
log(record)
session = option("--session-id") or option("--resume")
tools = [tool for tool in (option("--tools") or "").split(",") if tool]
current = {"message": None}
stream_input = option("--input-format") == "stream-json"
persist = "--no-session-persistence" not in argv and bool(session)
project = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude"),
                       "projects", "-fake-project")
branch = {"last": None}


def transcript(identity):
    return os.path.join(project, identity + ".jsonl")


def remember(kind, identity, error=False, text=None):
    if not persist:
        return
    record = {"type": kind, "uuid": identity, "parentUuid": branch["last"], "sessionId": session}
    if text is not None:
        record["text"] = text
    if error:
        record["isApiErrorMessage"] = True
    with open(transcript(session), "a") as stream:
        stream.write(json.dumps(record) + "\n")
    branch["last"] = identity


def write(message):
    message.setdefault("session_id", session)
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(**fields):
    value = {"type": "result", "subtype": "success", "is_error": False, "result": "", "num_turns": 1,
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 5,
                       "output_tokens": 7}}
    value.update(fields)
    write(value)


def read_line(timeout):
    if not select.select([sys.stdin], [], [], timeout)[0]:
        return None
    line = sys.stdin.readline()
    if not line:
        return ""
    log({"event": "input", "message": json.loads(line)})
    return json.loads(line)


def echo(message):
    content = (message.get("message") or {}).get("content") or [{}]
    remember("user", message.get("uuid") or str(uuid.uuid4()), text=content[0].get("text"))
    write({"type": "user", "message": message["message"], "uuid": message.get("uuid"),
           "isReplay": True, "parent_tool_use_id": None})


def run(directives):
    for directive in directives:
        if directive == "init" or (isinstance(directive, dict) and "init" in directive):
            details = directive["init"] if isinstance(directive, dict) else {}
            write({"type": "system", "subtype": "init", "model": option("--model"),
                   "tools": tools + (["StructuredOutput"] if "--json-schema" in argv else [])
                   + details.get("extra_tools", []),
                   **({"session_id": details["session_id"]} if "session_id" in details else {})})
        elif directive == "echo":
            echo(current["message"])
        elif "emit" in directive:
            emitted = dict(directive["emit"])
            if emitted.get("type") == "assistant" and emitted.get("parent_tool_use_id") is None:
                remember("assistant", str(emitted.get("uuid") or uuid.uuid4()),
                         bool(emitted.get("is_api_error_message") or emitted.get("error")))
            write(emitted)
        elif "line" in directive:
            sys.stdout.write(directive["line"] + "\n")
            sys.stdout.flush()
        elif "stderr" in directive:
            sys.stderr.write(directive["stderr"] + "\n")
            sys.stderr.flush()
        elif "sleep" in directive:
            time.sleep(directive["sleep"])
        elif "touch" in directive:
            open(directive["touch"], "w").write("{}")
        elif "write" in directive:
            open(directive["write"]["path"], "w").write(directive["write"]["text"])
        elif "consume" in directive:
            message = read_line(directive["consume"])
            if message and message.get("type") == "user":
                echo(message)
            else:
                log({"event": "consume_missed"})
        elif "wait_interrupt" in directive:
            deadline = time.time() + directive["wait_interrupt"]
            while time.time() < deadline:
                message = read_line(max(0.05, deadline - time.time()))
                if message == "":
                    return
                if message and message.get("type") == "control_request":
                    write({"type": "control_response", "response": {
                        "subtype": "success", "request_id": message["request_id"], "response": {"still_queued": []}}})
                    write({"type": "user", "message": {"role": "user", "content": [
                        {"type": "text", "text": "[Request interrupted by user]"}]}})
                    result(subtype="error_during_execution", is_error=True, result=None,
                           terminal_reason="aborted_tools")
                    return
        elif "hang" in directive:
            time.sleep(directive["hang"])
        elif "structured" in directive:
            answered = str(uuid.uuid4())
            remember("assistant", answered)
            write({"type": "assistant", "parent_tool_use_id": None, "uuid": answered,
                   "message": {"content": [{"type": "tool_use", "id": "toolu_structured",
                                            "name": "StructuredOutput", "input": directive["structured"]}]}})
            write({"type": "user", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_structured",
                 "content": "Structured output provided successfully"}]}})
            result(structured_output=directive["structured"], result=json.dumps(directive["structured"]),
                   **directive.get("result_fields", {}))
        elif "result" in directive:
            result(**directive["result"])
        elif "exit" in directive:
            sys.exit(directive["exit"])
        elif "lifecycle" in directive:
            write({"type": "command_lifecycle", "command_uuid": current["message"].get("uuid"),
                   "state": directive["lifecycle"]})
        elif "api_error" in directive:
            remember("assistant", str(uuid.uuid4()), True)
            write({"type": "assistant", "parent_tool_use_id": None, "uuid": "api-error", "error": "invalid_request",
                   "is_api_error_message": True,
                   "message": {"content": [{"type": "text", "text": directive["api_error"]}]}})


TURNS = SCENARIO.get("turns", [])
POINTER = os.environ["TCS_FAKE_CLAUDE_SCENARIO"] + ".next"


def next_turn(default):
    """Turns are consumed across invocations (retries and relaunches)."""
    index = int(open(POINTER).read()) if os.path.exists(POINTER) else 0
    open(POINTER, "w").write(str(index + 1))
    return TURNS[index] if index < len(TURNS) else default


for line in SCENARIO.get("noise", []):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
if option("--resume") and (option("--resume") in SCENARIO.get("gone_sessions", []) or SCENARIO.get("all_resumes_gone")):
    # Like Claude Code: a missing conversation ends the CLI before it reads any input.
    result(subtype="error_during_execution", is_error=True, result=None, num_turns=0, terminal_reason=None,
           errors=["No conversation found with session ID: " + option("--resume")])
    sys.stderr.write("No conversation found with session ID: " + option("--resume") + "\n")
    sys.stderr.flush()
    sys.exit(1)
if persist:
    os.makedirs(project, exist_ok=True)
    source = option("--resume")
    if "--fork-session" in argv and source and os.path.exists(transcript(source)) \
            and not os.path.exists(transcript(session)):
        with open(transcript(source)) as stream, open(transcript(session), "w") as copy:
            for line in stream:
                record = json.loads(line)
                record["sessionId"] = session
                copy.write(json.dumps(record) + "\n")
    if os.path.exists(transcript(session)):
        for line in open(transcript(session)):
            branch["last"] = json.loads(line)["uuid"]
    if option("--resume-session-at"):
        branch["last"] = option("--resume-session-at")
if not stream_input:
    stdin_text = sys.stdin.read()
    log({"event": "stdin", "text": stdin_text})
    remember("user", str(uuid.uuid4()), text=stdin_text)
    current["message"] = None
    run(next_turn([]))
    sys.exit(SCENARIO.get("exit_code", 0))
while True:
    message = read_line(60)
    if message in ("", None):
        sys.exit(0)
    if message.get("type") == "control_request":
        write({"type": "control_response", "response": {"subtype": "success", "request_id": message["request_id"],
                                                        "response": {"still_queued": []}}})
        continue
    if message.get("type") != "user":
        continue
    if option("--resume") in SCENARIO.get("missing_sessions", []):
        result(subtype="error_during_execution", is_error=True, result=None, num_turns=0, terminal_reason=None)
        sys.stderr.write("No conversation found with session ID: " + session + "\n")
        sys.stderr.flush()
        sys.exit(1)
    current["message"] = message
    run(next_turn(["init", "echo", {"structured": {
        "goal_status": "in_progress", "final_answer": "", "progress_note": "default"}}]))
'''


def complete(answer="PROOF_FILE: proof.md"):
    return {"structured": {"goal_status": "complete", "final_answer": answer, "progress_note": ""}}


def in_progress(note="Still working."):
    return {"structured": {"goal_status": "in_progress", "final_answer": "", "progress_note": note}}


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class FakeClaudeCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        fake.chmod(0o700)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.log = self.root / "claude.jsonl"
        self.scenario = self.root / "scenario.json"
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "TCS_FAKE_CLAUDE_LOG": str(self.log), "TCS_FAKE_CLAUDE_SCENARIO": str(self.scenario),
            "TCS_PROVER_API_MODELS": str(self.root / "state" / "api-models.json"),
            "TCS_PROVER_RETRIEVAL_MODE": "live",
            # Inherited host settings that must never reach a Claude child.
            "ANTHROPIC_API_KEY": "inherited-anthropic-key", "ANTHROPIC_BASE_URL": "https://inherited.example",
            "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            # API models drop CLAUDE_CONFIG_DIR and use $HOME/.claude: never the real one.
            "HOME": str(self.root / "home"),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        previous = Path.cwd()
        os.chdir(self.run_dir)
        self.addCleanup(os.chdir, previous)
        self.events = []
        emitter = mock.patch.object(runtime, "emit", side_effect=self.capture)
        emitter.start()
        self.addCleanup(emitter.stop)
        # Retries happen at once here; tests/test_model_retry.py checks the one-minute spacing.
        waits = mock.patch.object(runtime, "MODEL_RETRY_SECONDS", 0)
        waits.start()
        self.addCleanup(waits.stop)
        # Private settings directories are created here, so checks see only this test.
        private_temp = self.root / "tmp"
        private_temp.mkdir()
        temp_patch = mock.patch.object(tempfile, "tempdir", str(private_temp))
        temp_patch.start()
        self.addCleanup(temp_patch.stop)
        self.temp_before = set(private_temp.glob("tcs-claude-*"))

    def capture(self, kind, stage, **fields):
        self.events.append({"kind": kind, "stage": stage, **fields})

    def script(self, turns, **extra):
        self.scenario.write_text(json.dumps({"turns": turns, "secret": SECRET, **extra}))
        Path(f"{self.scenario}.next").unlink(missing_ok=True)

    def records(self, event=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if event is None or row["event"] == event]

    def labels(self, kind=None):
        return [event.get("label") for event in self.events if kind is None or event["kind"] == kind]

    def inputs(self, kind="user"):
        return [row["message"] for row in self.records("input") if row["message"].get("type") == kind]

    def texts(self):
        return [message["message"]["content"][0]["text"] for message in self.inputs()]

    def api_model(self, header="x-api-key", base="https://gateway.example.com/v1"):
        return providers.save_api_model({"format": "anthropic", "baseUrl": base, "headerName": header,
                                         "model": "gateway-claude", "apiKey": SECRET, "label": "Gateway"})["id"]

    def assertPrivateFilesRemoved(self):
        settings_parent = providers.state_directory() / providers.CLAUDE_SETTINGS
        for start in self.records("start"):
            if start.get("settings_path"):
                self.assertFalse(Path(start["settings_path"]).exists())
                self.assertFalse(Path(start["settings_path"]).parent.exists())
                # Every settings folder lives in the private state folder, named after its owner.
                self.assertEqual(Path(start["settings_path"]).parent.parent.resolve(), settings_parent.resolve())
                self.assertTrue(Path(start["settings_path"]).parent.name.startswith(f"run-{os.getpid()}-"))
        self.assertEqual(list(settings_parent.iterdir()) if settings_parent.exists() else [], [])
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("tcs-claude-*")) - self.temp_before, set())

    def assertNoSecretLeaks(self):
        self.assertNotIn(SECRET, json.dumps(self.events))
        for start in self.records("start"):
            self.assertFalse(start["secret_in_argv"])
            self.assertFalse(start["secret_in_env"])

    def goal(self, model="claude-opus-5-5", features=("multi_agent",), **options):
        session = claude_cli.goal_session(
            runtime, "Prove S exactly.", prompts=PROMPTS,
            settings={"model": model, "effort": "ultra", "speed": "fast", "summary": "concise"},
            options={"goal_cwd": str(self.run_dir), "goal_pause_file": str(self.run_dir / "pause-request.json"),
                     "author_steer_file": str(self.run_dir / "author-steer.json"), **options},
            features=list(features),
        )
        self.addCleanup(session.close)
        return session


class StructuredTests(FakeClaudeCase):
    def test_structured_success_uses_the_isolated_flag_set_and_returns_the_schema_output(self):
        self.script([[{"line": "[claude-code:unrecognized_model] {\"model\":\"claude-opus-5-5\"}"}, "init",
                      {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {"content": [
                          {"type": "thinking", "thinking": "Summarized: checking the statement."},
                          {"type": "text", "text": "Checking."},
                          {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x.md"}}]}}},
                      {"emit": {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                          {"type": "tool_result", "tool_use_id": "t1", "content": "text"}]}}},
                      {"structured": {"statement": "S", "notes": "ok"}}]])
        result, raw = runtime.structured("Review this.", SCHEMA, "review", model="claude-opus-5-5", effort="ultra")
        self.assertEqual(result, {"statement": "S", "notes": "ok"})
        self.assertEqual(json.loads(raw), result)
        start = self.records("start")[0]
        argv = start["argv"]
        for flag in ("-p", "--verbose", "--strict-mcp-config", "--safe-mode", "--disable-slash-commands",
                     "--no-chrome", "--restricted"):
            self.assertIn(flag, argv)
        # The session persists so that a retry can resume it; its flag comes first.
        self.assertNotIn("--no-session-persistence", argv)
        self.assertEqual(argv[0], "--session-id")
        self.assertTrue(claude_cli._session_id(argv[1]))
        # A Windows .cmd launcher would cut an argument at its first line break.
        self.assertEqual([value for value in argv if "\n" in value or "\r" in value], [])
        option = lambda name: argv[argv.index(name) + 1]
        self.assertEqual(option("--output-format"), "stream-json")
        self.assertEqual(option("--model"), "claude-opus-5-5")
        self.assertEqual(option("--effort"), "max")  # Claude lists no Ultra.
        self.assertEqual(option("--mcp-config"), '{"mcpServers":{}}')
        self.assertEqual((option("--permission-mode"), option("--permission-prompts")), ("dontAsk", "none"))
        self.assertEqual(option("--tools"), "Read,WebSearch,WebFetch")
        self.assertEqual(option("--allowedTools"), "Read,WebSearch,WebFetch")
        self.assertEqual(json.loads(option("--json-schema")), SCHEMA)
        # Concise summaries (the default) ask for Anthropic's summarized thinking.
        self.assertEqual(option("--thinking-display"), "summarized")
        self.assertNotIn("--bare", argv)
        self.assertNotIn("--settings", argv)  # No Bash and no gateway: nothing to configure.
        self.assertEqual(Path(start["cwd"]), runtime.STRUCTURED_WORKSPACE.resolve())
        self.assertEqual(self.records("stdin")[0]["text"], "Review this.")
        environment = start["env"]
        self.assertEqual(environment["CLAUDE_CODE_EFFORT_LEVEL"], "max")
        self.assertEqual(environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "1")
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.root / "claude-config"))
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                     "CLAUDE_CODE_USE_BEDROCK", "OPENAI_API_KEY", "TCS_PROVER_API_TOKEN"):
            self.assertNotIn(name, environment)
        events = [event["event"] for event in self.events if event["kind"] == "codex_event"]
        self.assertEqual([event["type"] for event in events],
                         ["turn.started", "item.completed", "item.completed", "item.started", "item.completed",
                          "item.completed", "turn.completed"])
        reasoning = events[1]["item"]
        self.assertEqual((reasoning["type"], reasoning["text"]), ("reasoning", "Summarized: checking the statement."))
        self.assertEqual(events[3]["item"]["type"], "dynamicToolCall")
        self.assertEqual(events[4]["item"]["status"], "completed")
        self.assertEqual(json.loads(events[5]["item"]["text"]), {"statement": "S", "notes": "ok"})
        self.assertTrue(all(event.get("provider") == "claude" for event in self.events if event["kind"] == "codex_event"))
        usage = next(event for event in self.events if event.get("label") == "Cache usage")
        self.assertEqual((usage["inputTokens"], usage["cachedInputTokens"], usage["cacheWriteInputTokens"]), (45, 30, 5))
        request = next(event for event in self.events if event["kind"] == "request")
        self.assertEqual((request["modelProvider"], request["serviceTier"]), ("claude", "standard"))

    def test_shell_tool_feature_offers_sandboxed_bash_through_a_private_settings_file(self):
        self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            runtime.structured("Review.", SCHEMA, "review", model="claude-haiku-5-5", features=("shell_tool", "multi_agent"))
        start = self.records("start")[0]
        argv = start["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Task,Bash,WebSearch,WebFetch")
        self.assertNotIn("Write", argv[argv.index("--allowedTools") + 1])
        sandbox = start["settings"]["sandbox"]
        self.assertEqual((sandbox["enabled"], sandbox["failIfUnavailable"], sandbox["allowUnsandboxedCommands"]),
                         (True, True, False))
        self.assertEqual(sandbox["network"], {"allowedDomains": [], "strictAllowlist": True})
        # The store, its staging folder, the private Codex homes and every
        # settings folder (other runs' too) are hidden from Bash.
        hidden = [str(path) for path in providers.private_paths()]
        self.assertEqual(sandbox["filesystem"], {"denyRead": hidden, "denyWrite": hidden})
        state = providers.state_directory().resolve()
        self.assertEqual(hidden, [str(providers.store_path().resolve()), str(state / ".api-models-staging"),
                                  str(state / "codex-homes"), str(state / "claude-settings")])
        self.assertIn(str(Path(start["settings_path"]).parent.parent.resolve()), hidden)
        self.assertEqual((start["settings_mode"], start["settings_dir_mode"]), ("0o600", "0o700"))
        self.assertNotIn("env", start["settings"])
        self.assertPrivateFilesRemoved()

    def test_unavailable_sandbox_never_offers_bash(self):
        self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=False):
            runtime.structured("Review.", SCHEMA, "review", model="claude-haiku-5-5", features=("shell_tool",))
        argv = self.records("start")[0]["argv"]
        self.assertNotIn("Bash", argv[argv.index("--tools") + 1])

    def test_structured_error_reports_a_sanitized_failure(self):
        self.script([["init", {"stderr": "⚠ Permission mode forced to default — CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set"},
                      {"stderr": f"gateway rejected key {SECRET}"},
                      {"result": {"is_error": True, "result": f"Invalid API key {SECRET}", "api_error_status": 401}}]],
                    exit_code=1)
        model = self.api_model()
        with self.assertRaises(runtime.Error) as failure:
            runtime.structured("Review.", SCHEMA, "review", model=model, attempts=1)
        message = str(failure.exception)
        self.assertTrue(message.startswith("Claude review failed: Invalid API key [redacted]"), message)
        self.assertIn("API status 401.", message)
        self.assertNotIn(SECRET, message)
        self.assertNotIn("Permission mode forced", message)
        failed = [event["event"] for event in self.events if event["kind"] == "codex_event"
                  and event["event"]["type"] == "turn.failed"]
        self.assertNotIn(SECRET, json.dumps(failed))
        self.assertNoSecretLeaks()
        self.assertPrivateFilesRemoved()

    def test_nonzero_exit_fails_even_after_a_result(self):
        self.script([["init", {"structured": {"statement": "S", "notes": ""}}, {"stderr": "shutdown failed"}]], exit_code=2)
        with self.assertRaises(runtime.Error) as failure:
            runtime.structured("Review.", SCHEMA, "review", model="claude-haiku-5-5", attempts=1)
        self.assertIn("Claude review failed: shutdown failed", str(failure.exception))
        self.assertIn("Claude exited with code 2.", str(failure.exception))

    def test_missing_structured_output_returns_text_for_the_shared_retry(self):
        self.script([["init", {"result": {"result": "I think the statement is fine."}}],
                     ["init", {"structured": {"statement": "S", "notes": "retried"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "review", model="claude-sonnet-5-5")
        self.assertEqual(result["notes"], "retried")
        retry = [row["text"] for row in self.records("stdin")][1]
        self.assertIn("STRUCTURED OUTPUT RECOVERY RETRY", retry)
        self.assertIn("I think the statement is fine.", retry)

    def test_timeout_stops_the_request(self):
        self.script([["init", {"hang": 30}]])
        started = time.monotonic()
        with self.assertRaisesRegex(runtime.Error, "did not respond within 1 seconds"):
            runtime.structured("Review.", SCHEMA, "review", model="claude-haiku-5-5", timeout=1, attempts=1)
        self.assertLess(time.monotonic() - started, 20)

    def test_unexpected_product_tools_fail_closed(self):
        self.script([[{"init": {"extra_tools": ["Artifact"]}}, {"structured": {"statement": "S", "notes": ""}}]])
        with self.assertRaisesRegex(runtime.Error, r"unexpected tools \(Artifact\)"):
            runtime.structured("Review.", SCHEMA, "review", model="claude-haiku-5-5", attempts=1)

    def test_critic_snapshot_runs_in_its_read_only_workspace_without_shell(self):
        self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
        manifest = json.dumps({"schemaVersion": 1})
        files = (("proof.md", "Proof"), ("statement.md", "Statement"), ("manifest.json", manifest))
        runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", features=("multi_agent",), input_files=files)
        start = self.records("start")[0]
        self.assertEqual(sorted(path.name for path in Path(start["cwd"]).iterdir()), ["manifest.json", "proof.md", "statement.md"])
        argv = start["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Task,WebSearch,WebFetch")
        self.assertIn("READ-ONLY PROOF FILES", self.records("stdin")[0]["text"])
        shutil.rmtree(start["cwd"])

    def test_disabled_search_offers_no_web_tools(self):
        self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
        with mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "disabled"}):
            runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
        argv = self.records("start")[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")

    def test_historical_runs_reject_claude_before_any_launch(self):
        with mock.patch.object(runtime, "retrieval_policy", return_value={"isolation": {}}):
            with self.assertRaisesRegex(runtime.Error, "Web cutoff timestamps require Codex CLI models"):
                runtime.run_structured_attempt("Review.", SCHEMA, "review", "claude-opus-5-5", "max", "standard", "concise")
        self.assertEqual(self.records(), [])

    def test_anthropic_api_model_uses_bare_mode_and_a_private_gateway_settings_file(self):
        for header, variable in (("x-api-key", "ANTHROPIC_API_KEY"), ("Authorization", "ANTHROPIC_AUTH_TOKEN"),
                                 ("X-Gateway-Key", "ANTHROPIC_CUSTOM_HEADERS")):
            with self.subTest(header=header):
                self.log.unlink(missing_ok=True)
                self.events.clear()
                for model in providers.load_api_models():
                    providers.delete_api_model(model["id"])
                model = self.api_model(header)
                self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
                with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
                    runtime.structured("Review.", SCHEMA, "review", model=model, features=("shell_tool",))
                start = self.records("start")[0]
                argv = start["argv"]
                self.assertIn("--bare", argv)
                self.assertEqual(argv[argv.index("--model") + 1], "gateway-claude")
                self.assertEqual(argv[argv.index("--tools") + 1], "Read,Bash")  # --bare offers no web tools.
                self.assertIn("Claude API mode tools", self.labels("status"))
                self.assertEqual((start["settings_mode"], start["settings_dir_mode"]), ("0o600", "0o700"))
                gateway = start["settings"]["env"]
                self.assertEqual(gateway["ANTHROPIC_BASE_URL"], "https://gateway.example.com")
                expected = f"{header}: {SECRET}" if variable == "ANTHROPIC_CUSTOM_HEADERS" else SECRET
                self.assertEqual(gateway[variable], expected)
                for other in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS"} - {variable}:
                    if other == "ANTHROPIC_API_KEY" and variable == "ANTHROPIC_CUSTOM_HEADERS":
                        self.assertEqual(gateway[other], claude_cli.API_KEY_PLACEHOLDER)
                    else:
                        self.assertNotIn(other, gateway)
                for alias in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                              "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
                    self.assertEqual(gateway[alias], "gateway-claude")
                self.assertEqual(gateway["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
                self.assertTrue(start["settings"]["sandbox"]["enabled"])
                # API mode keeps no Claude login location at all.
                self.assertNotIn("CLAUDE_CONFIG_DIR", start["env"])
                self.assertNoSecretLeaks()
                self.assertPrivateFilesRemoved()

    def test_reasoning_summaries_follow_the_job_setting_and_api_thinking_is_never_shown(self):
        thinking = {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {"content": [
            {"type": "thinking", "thinking": "Raw gateway reasoning."}]}}}
        api = self.api_model()
        for model, summary in (("claude-opus-5-5", "none"), (api, "concise"), (api, "detailed")):
            with self.subTest(model=model, summary=summary):
                self.log.unlink(missing_ok=True)
                self.events.clear()
                self.script([["init", thinking, {"structured": {"statement": "S", "notes": ""}}]])
                runtime.structured("Review.", SCHEMA, "review", model=model, summary=summary)
                self.assertNotIn("--thinking-display", self.records("start")[0]["argv"])
                reasoning = [event["event"]["item"] for event in self.events if event["kind"] == "codex_event"
                             and event["event"].get("item", {}).get("type") == "reasoning"]
                self.assertEqual([item["text"] for item in reasoning], [""])
                self.assertNotIn("Raw gateway reasoning.", json.dumps(self.events))

    def test_api_errors_become_redacted_error_events_not_agent_messages(self):
        model = self.api_model()
        self.script([["init", {"api_error": f"API Error: 400 request rejected for credential {SECRET}"},
                      {"result": {"is_error": True, "result": f"API Error: 400 request rejected for credential {SECRET}",
                                  "api_error_status": 400}}]], exit_code=1)
        with self.assertRaises(runtime.Error) as failure:
            runtime.structured("Review.", SCHEMA, "review", model=model, attempts=1)
        self.assertIn("request rejected for credential [redacted]", str(failure.exception))
        events = [event["event"] for event in self.events if event["kind"] == "codex_event"]
        errors = [event for event in events if event["type"] == "error"]
        self.assertEqual([event["message"] for event in errors],
                         ["API Error: 400 request rejected for credential [redacted]"])
        self.assertFalse([event for event in events if event.get("item", {}).get("type") == "agent_message"])
        self.assertNoSecretLeaks()

    def test_short_keys_stay_out_of_event_text_but_not_out_of_diagnostics(self):
        registered = mock.patch.object(runtime, "_REGISTERED_SECRETS", set())
        registered.start()
        self.addCleanup(registered.stop)
        model = providers.save_api_model({"format": "anthropic", "baseUrl": "http://127.0.0.1:4000",
                                          "headerName": "x-api-key", "model": "local-claude", "apiKey": "x"})["id"]
        self.script([["init", {"structured": {"statement": "For every x in the graph, x is reachable.", "notes": ""}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "review", model=model)
        self.assertEqual(result["statement"], "For every x in the graph, x is reachable.")
        messages = [event["event"]["item"]["text"] for event in self.events if event["kind"] == "codex_event"
                    and event["event"].get("item", {}).get("type") == "agent_message"]
        self.assertEqual([json.loads(text)["statement"] for text in messages], [result["statement"]])
        # Errors raised for the user still remove a short key everywhere, as on the Codex path.
        self.events.clear()
        self.script([["init", {"result": {"is_error": True, "result": "gateway rejected key x", "api_error_status": 401}}]],
                    exit_code=1)
        with self.assertRaises(runtime.Error) as failure:
            runtime.structured("Review.", SCHEMA, "review", model=model, attempts=1)
        self.assertIn("gateway rejected key [redacted]", str(failure.exception))
        failed = [event["event"]["error"]["message"] for event in self.events if event["kind"] == "codex_event"
                  and event["event"]["type"] == "turn.failed"]
        self.assertEqual(failed, ["gateway rejected key x\nAPI status 401."])

    def test_api_model_shells_cannot_read_the_cli_logins(self):
        model = self.api_model()
        self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            runtime.structured("Review.", SCHEMA, "review", model=model, features=("shell_tool",))
        hidden = self.records("start")[0]["settings"]["sandbox"]["filesystem"]["denyRead"]
        for path in (*providers.private_paths(), *providers.user_login_files()):
            self.assertIn(str(path), hidden)
        self.assertTrue(str(providers.user_login_files()[0]).endswith("auth.json"))

    def test_api_model_without_a_saved_key_never_launches(self):
        model = providers.save_api_model({"format": "anthropic", "baseUrl": "https://gateway.example.com",
                                          "model": "m", "apiKey": "temporary-key-1234"})["id"]
        store = json.loads(Path(os.environ["TCS_PROVER_API_MODELS"]).read_text())
        store["models"][0]["apiKey"] = ""
        Path(os.environ["TCS_PROVER_API_MODELS"]).write_text(json.dumps(store))
        with self.assertRaisesRegex(runtime.Error, "Add an API key for"):
            runtime.structured("Review.", SCHEMA, "review", model=model)
        self.assertEqual(self.records(), [])


class GoalTests(FakeClaudeCase):
    def test_goal_complete_on_the_first_turn(self):
        self.script([[{"line": "[claude-code:unrecognized_model] {}"}, "init", "echo",
                      {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {"content": [
                          {"type": "text", "text": "Writing the proof."},
                          {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "proof.md", "content": "x"}},
                          {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls"}},
                          {"type": "tool_use", "id": "s1", "name": "Task", "input": {"description": "Check lemma"}}]}}},
                      {"emit": {"type": "assistant", "parent_tool_use_id": "s1", "uuid": "a2", "message": {"content": [
                          {"type": "text", "text": "Subagent note."}]}}},
                      {"emit": {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                          {"type": "tool_result", "tool_use_id": "w1", "content": "ok"},
                          {"type": "tool_result", "tool_use_id": "b1", "content": "denied", "is_error": True},
                          {"type": "tool_result", "tool_use_id": "s1", "content": "done"}]}}},
                      complete()]])
        session = self.goal()
        self.assertEqual(next(session), {"outcome": "done", "output": "PROOF_FILE: proof.md"})
        start = self.records("start")[0]
        argv = start["argv"]
        option = lambda name: argv[argv.index(name) + 1]
        self.assertEqual((option("--input-format"), option("--output-format")), ("stream-json", "stream-json"))
        self.assertIn("--replay-user-messages", argv)
        self.assertNotIn("--no-session-persistence", argv)
        thread = option("--session-id")
        self.assertEqual(json.loads(option("--json-schema")), claude_cli.GOAL_TURN_SCHEMA)
        # The harness note travels in an owner-only file: no argument spans lines
        # (a Windows .cmd launcher would cut it), and the session flag comes first.
        self.assertEqual(start["harness"], claude_cli.GOAL_HARNESS_PROMPT.replace("[OBJECTIVE]", "Prove the exact statement."))
        self.assertEqual(start["harness_mode"], "0o600")
        self.assertEqual(Path(start["harness_path"]).parent.parent.resolve(),
                         (providers.state_directory() / providers.CLAUDE_SETTINGS).resolve())
        self.assertNotIn("--append-system-prompt", argv)
        self.assertEqual([value for value in argv if "\n" in value or "\r" in value], [])
        self.assertLess(argv.index("--session-id"), argv.index("--json-schema"))
        self.assertEqual(Path(start["cwd"]), self.run_dir)
        tools = option("--tools").split(",")
        self.assertEqual(tools[:2], ["Task", "Bash"] if claude_cli.sandbox_supported() else ["Task", "Read"])
        self.assertTrue({"Read", "Write", "Edit", "TodoWrite", "WebSearch", "WebFetch"} <= set(tools))
        for product in ("Artifact", "Workflow", "CronCreate", "Monitor", "Skill", "ScheduleWakeup",
                        "PushNotification", "RemoteTrigger", "DesignSync", "EnterWorktree"):
            self.assertNotIn(product, argv)
        self.assertIn("Edit(./**)", option("--allowedTools").split(","))
        self.assertNotIn("Write", option("--allowedTools").split(","))
        self.assertEqual(start["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "max")
        self.assertEqual(self.texts(), ["Prove S exactly."])
        statuses = [(event["label"], event.get("threadId")) for event in self.events if event["kind"] == "status"]
        self.assertIn(("Goal started", thread), statuses)
        self.assertIn(("Goal complete", thread), statuses)
        self.assertEqual(self.labels("request"), ["Exact solve input"])
        request = next(event for event in self.events if event["kind"] == "request")
        self.assertEqual((request["threadId"], request["model"], request["reasoningEffort"], request["reasoningSummary"]),
                         (thread, "claude-opus-5-5", "max", "concise"))
        result = next(event for event in self.events if event["kind"] == "author_result")
        self.assertEqual((result["label"], result["text"], result["threadId"]), ("Author solution", "PROOF_FILE: proof.md", thread))
        records = [event for event in self.events if event["kind"] == "codex_event"]
        methods = [record["event"].get("method") for record in records]
        self.assertEqual(methods[0], "turn/started")
        self.assertIn("turn/completed", methods)
        items = {record["event"]["params"]["item"]["type"]: record for record in records
                 if record["event"].get("method") == "item/started"}
        self.assertEqual(items["fileChange"]["event"]["params"]["item"]["name"], "proof.md")
        self.assertEqual(items["commandExecution"]["event"]["params"]["item"]["command"], "ls")
        self.assertEqual(items["collabAgentToolCall"]["event"]["params"]["item"]["tool"], "Check lemma")
        completed = [record["event"]["params"]["item"] for record in records if record["event"].get("method") == "item/completed"]
        self.assertIn("failed", [item.get("status") for item in completed if item["type"] == "commandExecution"])
        subagent = [record for record in records if record["event"].get("method") == "item/completed"
                    and record["event"]["params"]["item"].get("text") == "Subagent note."]
        self.assertEqual([record["root"] for record in subagent], [False])
        final = [item for item in completed if item["type"] == "agentMessage" and item["text"] == "PROOF_FILE: proof.md"]
        self.assertEqual(len(final), 1)
        goal = [record["event"]["params"]["goal"]["status"] for record in records if record["event"].get("method") == "thread/goal/updated"]
        self.assertEqual(goal, ["complete"])
        self.assertEqual(self.events[-1]["kind"], "author_result")
        session.close()
        self.assertPrivateFilesRemoved()

    def test_in_progress_turn_is_continued_until_complete(self):
        self.script([["init", "echo", in_progress("Outlined the approach.")], ["init", "echo", complete("Inline proof")]])
        session = self.goal()
        self.assertEqual(next(session)["output"], "Inline proof")
        self.assertEqual(self.texts(), ["Prove S exactly.", PROMPTS["continuation"]])
        self.assertEqual(self.labels("request"), ["Exact solve input", "Exact solve input"])
        progress = next(event for event in self.events if event.get("label") == "Author progress")
        self.assertEqual((progress["text"], progress["goalStatus"]), ("Outlined the approach.", "in_progress"))
        self.assertEqual(self.labels("status").count("Goal started"), 1)

    def test_rejection_starts_the_yaml_repair_turn_and_none_ends(self):
        self.script([["init", "echo", complete("First proof")], ["init", "echo", complete("Repaired proof")]])
        session = self.goal()
        self.assertEqual(next(session)["output"], "First proof")
        repaired = session.send({"solution": "First proof", "bugs": "Lemma 2 fails.", "round": 1})
        self.assertEqual(repaired, {"outcome": "done", "output": "Repaired proof"})
        self.assertEqual(self.texts()[1], "Revision 1; critic round 1: Lemma 2 fails.\nCandidate: First proof")
        repair = [event for event in self.events if event.get("label") == "Exact solve input"][1]
        self.assertEqual(repair["stage"], "repair")
        self.assertEqual([event["stage"] for event in self.events if event.get("label") == "Author solution"], ["solve", "repair"])
        with self.assertRaises(StopIteration):
            session.send(None)
        self.assertPrivateFilesRemoved()

    def test_compaction_queues_the_yaml_re_anchor(self):
        self.script([["init", "echo",
                      {"emit": {"type": "system", "subtype": "compact_boundary", "uuid": "cb-1",
                                "compact_metadata": {"trigger": "auto", "pre_tokens": 190000}}},
                      {"consume": 5}, complete()]])
        session = self.goal()
        next(session)
        anchors = [event for event in self.events if event.get("label") == "Author context re-anchor after compaction"]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["text"], PROMPTS["compaction"].format(directory=self.run_dir))
        self.assertNotIn("steerId", anchors[0])
        self.assertEqual(self.texts()[1], anchors[0]["text"])
        compactions = [event for event in self.events if event["kind"] == "codex_event"
                       and event["event"].get("params", {}).get("item", {}).get("type") == "contextCompaction"]
        self.assertEqual(len(compactions), 1)
        self.assertNotIn("consume_missed", [row["event"] for row in self.records()])

    def test_live_instruction_is_written_and_acknowledged_by_its_echo(self):
        steer = self.run_dir / "author-steer.json"
        self.script([["init", "echo",
                      {"write": {"path": str(steer), "text": json.dumps({"id": "steer-7", "instruction": "Try induction."})}},
                      {"consume": 5}, in_progress()],
                     ["init", "echo", complete()]])
        session = self.goal()
        next(session)
        sent = [event for event in self.events if event.get("label") == "Live author instruction sent"]
        self.assertEqual([(event["steerId"], event["text"]) for event in sent], [("steer-7", "Try induction.")])
        accepted = [event for event in self.events if event.get("label") == "Author instruction accepted"]
        self.assertEqual([event["authorSteerDelivered"] for event in accepted], ["steer-7"])
        self.assertEqual(self.texts(), ["Prove S exactly.", "Try induction.", PROMPTS["continuation"]])
        # A delivered instruction is never sent twice.
        self.assertEqual(self.texts().count("Try induction."), 1)

    def test_pause_interrupts_the_running_turn_and_keeps_the_session(self):
        pause = self.run_dir / "pause-request.json"
        self.script([["init", "echo", {"touch": str(pause)}, {"wait_interrupt": 10}]])
        session = self.goal()
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(session)
        thread = self.records("start")[0]["argv"][self.records("start")[0]["argv"].index("--session-id") + 1]
        self.assertEqual((paused.exception.reason, paused.exception.thread_id), ("", thread))
        self.assertEqual([message["request"]["subtype"] for message in self.inputs("control_request")][:1], ["interrupt"])
        status = [event for event in self.events if event.get("label") == "Author paused"]
        self.assertEqual(status[0]["text"], "Author paused at the current turn boundary.")
        turn = [event["event"] for event in self.events if event["kind"] == "codex_event"
                and event["event"].get("method") == "turn/completed"]
        self.assertEqual(turn[-1]["params"]["turn"]["status"], "interrupted")
        self.assertPrivateFilesRemoved()

    def test_pause_without_acknowledgement_stops_the_cli_after_the_grace_period(self):
        pause = self.run_dir / "pause-request.json"
        self.script([["init", "echo", {"touch": str(pause)}, {"hang": 30}]])
        with mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 0.5, create=True):
            session = self.goal()
            started = time.monotonic()
            with self.assertRaises(runtime.WorkflowPaused):
                next(session)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("Claude did not acknowledge the pause; keeping its saved session and the last saved work.",
                      [event.get("text") for event in self.events if event["kind"] == "diagnostic"])

    def test_resume_uses_the_saved_session_and_announces_it_after_init(self):
        thread = "123e4567-e89b-42d3-a456-426614174000"
        self.script([["init", "echo", complete()]])
        session = self.goal(goal_thread_id=thread, goal_resume=True)
        next(session)
        argv = self.records("start")[0]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], thread)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(self.texts(), [PROMPTS["resume"].format(directory=self.run_dir, original_prompt="Prove S exactly.")])
        self.assertIn("Goal resumed", self.labels("status"))
        self.assertNotIn("Goal started", self.labels("status"))
        resumed = next(event for event in self.events if event.get("label") == "Goal resumed")
        self.assertEqual(resumed["threadId"], thread)

    def test_missing_saved_session_starts_a_new_one_unless_resume_is_required(self):
        thread = "123e4567-e89b-42d3-a456-426614174000"
        self.script([["init", "echo", complete()]], missing_sessions=[thread])
        session = self.goal(goal_thread_id=thread)
        self.assertEqual(next(session)["outcome"], "done")
        starts = self.records("start")
        self.assertEqual(len(starts), 2)
        self.assertIn("--resume", starts[0]["argv"])
        self.assertIn("--session-id", starts[1]["argv"])
        diagnostic = next(event["text"] for event in self.events if event["kind"] == "diagnostic")
        self.assertTrue(diagnostic.startswith("Saved session unavailable; starting a new session"), diagnostic)
        self.assertIn("No conversation found", diagnostic)
        self.assertIn("Goal started", self.labels("status"))

        self.log.unlink()
        self.events.clear()
        self.script([], missing_sessions=[thread])
        required = self.goal(goal_thread_id=thread, goal_require_resume=True)
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(required)
        self.assertIn("Could not resume the saved Claude conversation; no replacement was started.", paused.exception.reason)
        self.assertEqual(len(self.records("start")), 1)

    def test_required_resume_without_a_saved_session_fails_before_launch(self):
        self.script([])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.goal(goal_require_resume=True))
        self.assertIn("saved Claude session ID is missing", paused.exception.reason)
        self.assertEqual(self.records(), [])

    def test_time_limit_interrupts_and_pauses_with_its_reason(self):
        deadline = time.monotonic() + 1.5
        self.script([["init", "echo", {"wait_interrupt": 20}]])
        with mock.patch.object(runtime, "workflow_remaining", side_effect=lambda options: max(0.0, deadline - time.monotonic())):
            with self.assertRaises(runtime.WorkflowPaused) as paused:
                next(self.goal())
        self.assertEqual(paused.exception.reason, "Workflow time limit reached.")
        self.assertIn("interrupt", [message["request"]["subtype"] for message in self.inputs("control_request")])
        # The abandoned turn is closed as interrupted, as Codex records its last turn.
        self.assertEqual(self.turns(), [("started", 1), ("interrupted", 1)])

    def turns(self):
        """Turn lifecycle as (status, turn number); every started turn must end."""

        numbers, rows = {}, []
        for event in self.events:
            method = event.get("event", {}).get("method") if event["kind"] == "codex_event" else None
            if method in {"turn/started", "turn/completed"}:
                turn = event["event"]["params"]["turn"]
                number = numbers.setdefault(turn["id"], len(numbers) + 1)
                rows.append(("started" if method == "turn/started" else turn["status"], number))
        return rows

    def test_api_error_message_never_becomes_part_of_the_author_answer(self):
        model = self.api_model()
        self.script([["init", "echo", {"api_error": f"Failed to authenticate. API Error: 403 credential {SECRET}"},
                      {"result": {"is_error": True, "result": f"Failed to authenticate. API Error: 403 credential {SECRET}",
                                  "api_error_status": 403}}]])
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            with self.assertRaises(runtime.WorkflowPaused) as paused:
                next(self.goal(model=model))
        self.assertIn("API Error: 403 credential [redacted]", paused.exception.reason)
        events = [event["event"] for event in self.events if event["kind"] == "codex_event"]
        self.assertEqual([event["params"]["error"]["message"] for event in events if event.get("method") == "error"],
                         ["Failed to authenticate. API Error: 403 credential [redacted]"])
        self.assertFalse([event for event in events if event.get("params", {}).get("item", {}).get("type") == "agentMessage"])
        self.assertEqual(self.turns(), [("started", 1), ("failed", 1)])
        self.assertNoSecretLeaks()

    def test_cli_that_exits_before_reading_input_falls_back_to_a_new_session(self):
        thread = "123e4567-e89b-42d3-a456-426614174000"
        self.script([["init", "echo", complete()]], gone_sessions=[thread])
        send = claude_cli.SessionProcess.send

        def slow_writer(process, message):
            # A slow transcript reader delays the first write until the CLI has exited.
            if message.get("type") == "user" and "--resume" in process.process.args:
                process.process.wait(timeout=10)
            return send(process, message)

        with mock.patch.object(claude_cli.SessionProcess, "send", slow_writer):
            session = self.goal(goal_thread_id=thread, goal_resume=True)
            self.assertEqual(next(session)["outcome"], "done")
        starts = self.records("start")
        self.assertEqual(["--resume" in start["argv"] for start in starts], [True, False])
        diagnostic = next(event["text"] for event in self.events if event["kind"] == "diagnostic")
        self.assertTrue(diagnostic.startswith("Saved session unavailable; starting a new session"), diagnostic)
        # The attempt Claude never confirmed published no prompt and no turn;
        # the new session's status comes first, as with Codex.
        labels = [event.get("label") for event in self.events if event["kind"] in {"status", "request"}]
        self.assertEqual(labels[:2], ["Goal started", "Exact solve input"])
        self.assertEqual(labels.count("Exact solve input"), 1)
        self.assertEqual(self.turns(), [("started", 1), ("completed", 1)])

    def test_a_failed_relaunch_between_turns_resends_the_repair_prompt(self):
        self.script([["init", "echo", complete("First proof"), {"exit": 0}],
                     ["init", "echo", complete("Repaired proof")]], all_resumes_gone=True)
        session = self.goal()
        self.assertEqual(next(session)["output"], "First proof")
        repaired = session.send({"solution": "First proof", "bugs": "Lemma 2 fails.", "round": 1})
        self.assertEqual(repaired["output"], "Repaired proof")
        argvs = [start["argv"] for start in self.records("start")]
        self.assertEqual(["--resume" in argv for argv in argvs], [False, True, False])
        repair = "Revision 1; critic round 1: Lemma 2 fails.\nCandidate: First proof"
        self.assertEqual(self.texts()[-1], repair)
        self.assertIn("The Claude CLI exited between turns; resuming its saved session.",
                      [event.get("text") for event in self.events if event["kind"] == "diagnostic"])

    def test_a_cli_that_ends_as_the_next_turn_is_written_is_resumed_with_that_turn(self):
        self.script([["init", "echo", complete("First proof"), {"exit": 0}],
                     ["init", "echo", complete("Repaired proof")]])
        send, written = claude_cli.SessionProcess.send, set()

        def late_writer(process, message):
            # The first CLI has already ended when the repair turn is written: a broken pipe.
            if message.get("type") == "user" and process in written:
                process.process.wait(timeout=10)
            written.add(process)
            return send(process, message)

        with mock.patch.object(claude_cli.SessionProcess, "send", late_writer):
            session = self.goal()
            next(session)
            repaired = session.send({"solution": "First proof", "bugs": "Lemma 2 fails.", "round": 1})
        self.assertEqual(repaired["output"], "Repaired proof")
        self.assertEqual(["--resume" in start["argv"] for start in self.records("start")], [False, True])
        self.assertEqual(self.texts()[-1], "Revision 1; critic round 1: Lemma 2 fails.\nCandidate: First proof")
        self.assertEqual(self.turns(), [("started", 1), ("completed", 1), ("started", 2), ("completed", 2)])

    def test_a_cli_that_ends_without_reading_the_next_turn_is_resumed_with_that_turn(self):
        # The repair message reaches the pipe, but the CLI ends without reading it.
        self.script([["init", "echo", complete("First proof"), {"sleep": 0.8}, {"exit": 0}],
                     ["init", "echo", complete("Repaired proof")]])
        session = self.goal()
        next(session)
        repaired = session.send({"solution": "First proof", "bugs": "Lemma 2 fails.", "round": 1})
        self.assertEqual(repaired["output"], "Repaired proof")
        self.assertEqual(["--resume" in start["argv"] for start in self.records("start")], [False, True])
        self.assertIn("The Claude CLI exited between turns; resuming its saved session.",
                      [event.get("text") for event in self.events if event["kind"] == "diagnostic"])
        # The unread turn is closed as interrupted; its resent copy runs.
        self.assertEqual(self.turns(), [("started", 1), ("completed", 1), ("started", 2), ("interrupted", 2),
                                        ("started", 3), ("completed", 3)])

    def test_required_resume_of_a_gone_conversation_pauses_without_automatic_retry(self):
        thread = "123e4567-e89b-42d3-a456-426614174000"
        self.script([], gone_sessions=[thread])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.goal(goal_thread_id=thread, goal_resume=True, goal_require_resume=True))
        self.assertTrue(paused.exception.session_unavailable)
        self.assertIn("No conversation found with session ID", paused.exception.reason)
        self.assertIn("cleanupPeriodDays", paused.exception.reason)
        self.assertIn("Choose Continue proof author", paused.exception.reason)
        self.assertNotIn("Exact solve input", self.labels("request"))

    def test_a_live_instruction_that_misses_its_turn_is_never_sent_during_review(self):
        released = threading.Event()

        def capture(kind, stage, **fields):
            self.capture(kind, stage, **fields)
            if kind == "codex_event" and fields["event"].get("method") == "turn/completed":
                released.set()

        def late_instruction(path, delivered):
            # The watcher saw the turn running, then reads the instruction just after it ends.
            released.wait(10)
            return "steer-9", "Too late."

        self.script([["init", "echo", {"sleep": 0.3}, complete()]])
        with mock.patch.object(runtime, "emit", side_effect=capture), \
                mock.patch.object(runtime, "pending_author_steer", side_effect=late_instruction):
            session = self.goal()
            self.assertEqual(next(session)["outcome"], "done")
            time.sleep(0.8)  # Suspended while the critic reviews the answer.
        self.assertNotIn("Too late.", self.texts())
        self.assertNotIn("Live author instruction sent", self.labels("request"))

    def test_lifecycle_messages_track_a_turn_that_queued_input_started(self):
        steer = self.run_dir / "author-steer.json"
        self.script([["init", "echo", {"write": {"path": str(steer), "text": json.dumps(
                          {"id": "steer-3", "instruction": "Try induction."})}}, {"sleep": 0.6}, in_progress()],
                     # The instruction arrived after the first turn: it starts the next turn, which
                     # only its lifecycle message reports here.
                     [{"lifecycle": "started"}, complete("After the instruction")]])
        session = self.goal()
        self.assertEqual(next(session)["output"], "After the instruction")
        self.assertEqual(self.texts(), ["Prove S exactly.", "Try induction."])  # No continuation in between.
        accepted = [event["authorSteerDelivered"] for event in self.events if event.get("label") == "Author instruction accepted"]
        self.assertEqual(accepted, ["steer-3"])
        self.assertEqual(self.turns(), [("started", 1), ("completed", 1), ("started", 2), ("completed", 2)])

    def test_reasoning_summaries_of_the_author_use_summarized_thinking(self):
        thinking = {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "t1", "message": {"content": [
            {"type": "thinking", "thinking": "Summarized: trying induction."}]}}}
        self.script([["init", "echo", thinking, complete()]])
        next(self.goal())
        argv = self.records("start")[0]["argv"]
        self.assertEqual(argv[argv.index("--thinking-display") + 1], "summarized")
        items = [event["event"]["params"]["item"] for event in self.events if event["kind"] == "codex_event"
                 and event["event"].get("params", {}).get("item", {}).get("type") == "reasoning"]
        self.assertEqual([item["text"] for item in items], ["Summarized: trying induction."])

    def test_usage_limit_error_pauses_with_its_reason_for_the_automatic_retry(self):
        self.script([["init", "echo", {"result": {"is_error": True, "result": "Claude AI usage limit reached|1792700000",
                                                   "api_error_status": 429}}]])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.goal())
        self.assertTrue(paused.exception.reason.startswith("Claude author turn failed: Claude AI usage limit reached"))
        self.assertIn("API status 429.", paused.exception.reason)
        self.assertIn(paused.exception.reason, [event.get("text") for event in self.events if event["kind"] == "diagnostic"])

    def test_failure_before_init_reports_no_thread_for_a_later_resume(self):
        self.script([[{"stderr": "Not logged in · Please run /login"}, {"exit": 1}]])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.goal())
        self.assertIsNone(paused.exception.thread_id)
        self.assertIn("Not logged in", paused.exception.reason)
        self.assertNotIn("Goal started", self.labels("status"))
        statuses = [event for event in self.events if event["kind"] == "status"]
        self.assertTrue(all(event.get("threadId") is None for event in statuses), statuses)

    def test_cli_exit_mid_turn_pauses_with_the_stderr_detail(self):
        self.script([["init", "echo", {"stderr": "fatal: crashed"}, {"exit": 3}]])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.goal())
        self.assertIn("Claude CLI stopped unexpectedly.", paused.exception.reason)
        self.assertIn("fatal: crashed", paused.exception.reason)

    def test_pause_before_start_launches_nothing(self):
        (self.run_dir / "pause-request.json").write_text("{}")
        self.script([])
        with self.assertRaises(runtime.WorkflowPaused):
            next(self.goal())
        self.assertEqual(self.records(), [])

    def test_unexpected_tools_stop_the_author(self):
        self.script([[{"init": {"extra_tools": ["RemoteTrigger"]}}, "echo", complete()]])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.goal())
        self.assertIn("unexpected tools (RemoteTrigger)", paused.exception.reason)

    def test_anthropic_api_author_runs_bare_with_the_key_only_in_the_private_settings(self):
        model = self.api_model("Authorization")
        self.script([["init", "echo", complete()]])
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            session = self.goal(model=model)
            next(session)
        start = self.records("start")[0]
        argv = start["argv"]
        self.assertIn("--bare", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Edit")
        self.assertEqual(start["settings"]["env"]["ANTHROPIC_AUTH_TOKEN"], SECRET)
        self.assertEqual(start["settings_mode"], "0o600")
        self.assertIn("Claude API mode tools", self.labels("status"))
        session.close()
        self.assertNoSecretLeaks()
        self.assertPrivateFilesRemoved()

    def short_key_model(self, key):
        # Registered keys are process-wide: keep this test's short key out of later tests.
        registered = mock.patch.object(runtime, "_REGISTERED_SECRETS", set())
        registered.start()
        self.addCleanup(registered.stop)
        return providers.save_api_model({"format": "anthropic", "baseUrl": "http://127.0.0.1:4000",
                                         "headerName": "x-api-key", "model": "local-claude", "apiKey": key,
                                         "label": "Keyless gateway"})["id"]

    def test_short_keys_never_rewrite_the_event_text_of_the_author(self):
        message = "Let x be a vertex on the shortest path; the greatest one is fixed."
        answer = "Proof. Let x be the greatest element; then x is fixed."
        for key, model, environment in (("x", None, {}), (None, "claude-opus-5-5", {"OPENAI_API_KEY": "x"}),
                                        ("test", None, {})):
            with self.subTest(key=key, environment=environment), mock.patch.dict(os.environ, environment):
                self.events.clear()
                self.script([["init", "echo",
                              {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {
                                  "content": [{"type": "text", "text": message},
                                              {"type": "tool_use", "id": "b1", "name": "Bash",
                                               "input": {"command": "python -m pytest tests -x"}}]}}},
                              complete(answer)]])
                with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
                    session = self.goal(model=model or self.short_key_model(key))
                    self.assertEqual(next(session)["output"], answer)
                    session.close()
                items = [event["event"]["params"]["item"] for event in self.events if event["kind"] == "codex_event"
                         and event["event"].get("method") in {"item/started", "item/completed"}]
                self.assertIn(message, [item.get("text") for item in items])
                self.assertIn(answer, [item.get("text") for item in items])
                self.assertIn("python -m pytest tests -x", [item.get("command") for item in items])
                self.assertNotIn("[redacted]", json.dumps(self.events))

    def test_long_keys_are_redacted_before_event_text_is_clipped(self):
        model = self.api_model()
        command = "a" * 1995 + SECRET + " && echo done"
        self.script([["init", "echo",
                      {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {
                          "content": [{"type": "text", "text": f"Using {SECRET} here."},
                                      {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": command}}]}}},
                      complete()]])
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            session = self.goal(model=model)
            next(session)
            session.close()
        items = [event["event"]["params"]["item"] for event in self.events if event["kind"] == "codex_event"
                 and event["event"].get("method") == "item/started"]
        clipped = next(item["command"] for item in items if item["type"] == "commandExecution")
        self.assertEqual(clipped, ("a" * 1995 + "[redacted]")[:1999] + "…")
        self.assertNotIn(SECRET[:5], clipped)
        self.assertIn("Using [redacted] here.", json.dumps(self.events))
        self.assertNoSecretLeaks()

    def test_historical_runs_reject_the_claude_author(self):
        with mock.patch.object(runtime, "retrieval_policy", return_value={"isolation": {}}):
            with self.assertRaisesRegex(runtime.Error, "Web cutoff timestamps require Codex CLI models"):
                next(self.goal())

    def test_workflow_dispatches_claude_authors_to_the_claude_session(self):
        definition = {"prompts": {**PROMPTS, "assignment": "Do [TASK]."}, "nodes": {
            "worker": {"run": "goal", "role": "author", "prompt": "assignment", "task": "state.input", "marker": "[TASK]",
                       "resume": {}, "outcome": "result.outcome", "after": [{"merge": "result"}],
                       "next": {"done": "end", "failure": "end"}}}}
        path = self.root / "custom.yaml"
        path.write_text(runtime.yaml.safe_dump(definition))
        workflow = runtime.load_workflow(path)
        calls = []

        def fake_session(*args, **kwargs):
            calls.append(kwargs["settings"]["model"])
            yield {"outcome": "done", "output": "Complete"}

        with mock.patch.object(claude_cli, "goal_session", side_effect=fake_session), \
                mock.patch.object(runtime, "goal_session", side_effect=AssertionError("Codex session started")):
            state = runtime._execute(workflow, {"input": "x"}, {"author_model": "claude-sonnet-5-5"}, workflow["prompts"])
        self.assertEqual((state["output"], calls), ("Complete", ["claude-sonnet-5-5"]))


class TranslatorTests(unittest.TestCase):
    def test_tool_kinds_and_styles_match_the_ui_renderer(self):
        for style, names in (("app", ("commandExecution", "fileChange", "webSearch", "collabAgentToolCall", "dynamicToolCall")),
                             ("exec", ("command_execution", "file_change", "web_search", "collab_agent_tool_call", "dynamicToolCall"))):
            translator = claude_cli.EventTranslator(style)
            blocks = [{"type": "tool_use", "id": f"t{index}", "name": name, "input": data}
                      for index, (name, data) in enumerate((("Bash", {"command": "make"}), ("NotebookEdit", {"notebook_path": "n.ipynb"}),
                                                            ("WebFetch", {"url": "https://example.com"}), ("Task", {"description": "Audit"}),
                                                            ("TodoWrite", {})))]
            events = translator.message({"type": "assistant", "parent_tool_use_id": None, "uuid": "u",
                                         "message": {"content": blocks}})
            items = [(event.get("params") or event)["item"] for event, _ in events]
            self.assertEqual(tuple(item["type"] for item in items), names)
            self.assertEqual(items[1]["name"], "n.ipynb")
            self.assertEqual(items[2]["tool"], "WebFetch")
            self.assertEqual(items[4]["tool"], "TodoWrite")

    def test_cache_usage_counts_cached_input_inside_the_total(self):
        self.assertEqual(claude_cli.cache_usage({"input_tokens": 3, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 7}),
                         {"input_tokens": 100, "cached_input_tokens": 90, "cache_write_input_tokens": 7})
        self.assertIsNone(claude_cli.cache_usage(None))

    def test_windows_batch_launchers_give_way_to_the_native_claude_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            npm, other = root / "npm", root / "native"
            native = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            native.parent.mkdir(parents=True)
            other.mkdir()
            for path in (npm / "claude.CMD", npm / "claude.bat", native, other / "claude.exe"):
                path.write_text("")
            # npm's shim runs the package's own claude.exe, which cmd.exe never parses.
            self.assertEqual(claude_cli.native_windows_executable(str(npm / "claude.CMD"), search_path=""), str(native))
            native.unlink()
            # Otherwise a claude.exe elsewhere on PATH; a native binary is kept as it is.
            self.assertEqual(claude_cli.native_windows_executable(str(npm / "claude.bat"), search_path=f"{npm}{os.pathsep}\"{other}\""),
                             str(other / "claude.exe"))
            self.assertEqual(claude_cli.native_windows_executable(str(other / "claude.exe"), search_path=""), str(other / "claude.exe"))
            # Without one the launcher stays, and a line break it would cut is refused.
            self.assertEqual(claude_cli.native_windows_executable(str(npm / "claude.CMD"), search_path=""), str(npm / "claude.CMD"))
        with mock.patch.object(claude_cli.os, "name", "nt"):
            with self.assertRaisesRegex(runtime.Error, "cannot pass this request's arguments"):
                claude_cli.batch_safe([r"C:\npm\claude.cmd", "--append-system-prompt", "one\ntwo"], runtime)
            command = [r"C:\npm\claude.cmd", "--model", "claude-opus-5-5"]
            self.assertEqual(claude_cli.batch_safe(command, runtime), command)
            native_command = [r"C:\bin\claude.exe", "--x", "a\nb"]
            self.assertEqual(claude_cli.batch_safe(native_command, runtime), native_command)

    def test_session_ids_must_be_canonical_uuids(self):
        self.assertTrue(claude_cli._session_id("123e4567-e89b-42d3-a456-426614174000"))
        for value in ("--dangerously-skip-permissions", "thread-1", "", "123E4567E89B42D3A456426614174000"):
            self.assertFalse(claude_cli._session_id(value), value)

    def test_every_translated_text_is_redacted(self):
        translator = claude_cli.EventTranslator("app", scrub=lambda text: text.replace("KEY-123456789", "[redacted]"))
        blocks = [{"type": "text", "text": "echo KEY-123456789"},
                  {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "echo KEY-123456789"}},
                  {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": "KEY-123456789.md"}},
                  {"type": "tool_use", "id": "s", "name": "WebSearch", "input": {"query": "KEY-123456789"}},
                  {"type": "tool_use", "id": "t", "name": "Task", "input": {"description": "KEY-123456789"}}]
        events = translator.message({"type": "assistant", "parent_tool_use_id": None, "uuid": "u",
                                     "message": {"content": blocks}})
        events.append((translator.turn_completed("turn-1", "failed", error="denied KEY-123456789"), True))
        self.assertEqual(len(events), 6)
        self.assertNotIn("KEY-123456789", json.dumps(events))
        self.assertEqual(json.dumps(events).count("[redacted]"), 6)

    def test_child_environment_keeps_only_the_subscription_location(self):
        base = {"PATH": "/bin", "ANTHROPIC_BASE_URL": "x", "CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "t",
                "CLAUDE_CONFIG_DIR": "/c", "CLAUDE_CODE_OAUTH_TOKEN": "oauth"}
        subscription = claude_cli.child_environment(base, "high", api=False)
        self.assertEqual({key for key in subscription if key.startswith(("CLAUDE", "ANTHROPIC"))},
                         {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL",
                          "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"})
        api = claude_cli.child_environment(base, "high", api=True)
        self.assertNotIn("CLAUDE_CONFIG_DIR", api)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", api)


class PrivateSettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        patcher = mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.root / "state" / "api-models.json")})
        patcher.start()
        self.addCleanup(patcher.stop)

    @unittest.skipIf(os.name == "nt", "POSIX process liveness")
    def test_sweep_removes_only_the_folders_of_ended_processes(self):
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        live = claude_cli.PrivateSettings(lambda directory: {"env": {"ANTHROPIC_API_KEY": "live-key-0123456789"}})
        self.addCleanup(live.close)
        parent = live.directory.parent
        self.assertEqual(parent, (self.root / "state" / "claude-settings"))
        if os.name != "nt":
            self.assertEqual(oct(parent.stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct(parent.parent.stat().st_mode & 0o777), "0o700")
        stale = parent / f"run-{ended.pid}-abc123"
        stale.mkdir()
        (stale / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "left-behind"}}')
        other = parent / f"run-{os.getppid()}-def456"  # A live process of another runner.
        other.mkdir()
        unknown = parent / "notes"
        unknown.mkdir()
        claude_cli.sweep_stale_settings()
        self.assertEqual(sorted(path.name for path in parent.iterdir()),
                         sorted([live.directory.name, other.name, unknown.name]))
        live.close()
        self.assertFalse(live.directory.exists())

    def test_windows_liveness_opens_the_process_without_signalling_it(self):
        class Kernel32:
            def __init__(self, handle, wait=0x102):
                self.handle, self.wait, self.closed = handle, wait, []

            def OpenProcess(self, access, inherit, pid):
                self.request = (access, inherit, pid)
                return self.handle

            def WaitForSingleObject(self, handle, milliseconds):
                return self.wait if (handle, milliseconds) == (self.handle, 0) else None

            def CloseHandle(self, handle):
                self.closed.append(handle)

        running = Kernel32(42)
        self.assertTrue(claude_cli._windows_process_alive(1234, running, lambda: 0))
        self.assertEqual((running.request, running.closed), ((0x00101000, False, 1234), [42]))
        ended = Kernel32(43, wait=0)  # WAIT_OBJECT_0: the process has exited.
        self.assertFalse(claude_cli._windows_process_alive(1234, ended, lambda: 0))
        self.assertEqual(ended.closed, [43])
        # No handle: access denied means another user's process runs; anything else means it is gone.
        self.assertTrue(claude_cli._windows_process_alive(1234, Kernel32(None), lambda: 5))
        self.assertFalse(claude_cli._windows_process_alive(1234, Kernel32(None), lambda: 87))

    def test_a_pause_for_a_gone_conversation_is_recorded_without_retry(self):
        definition = {"prompts": {**PROMPTS, "assignment": "Do [TASK]."}, "nodes": {
            "worker": {"run": "goal", "role": "author", "prompt": "assignment", "task": "state.input", "marker": "[TASK]",
                       "resume": {}, "outcome": "result.outcome", "after": [{"merge": "result"}],
                       "next": {"done": "end", "failure": "end"}}}}
        path = self.root / "custom.yaml"
        path.write_text(runtime.yaml.safe_dump(definition))
        workflow = runtime.load_workflow(path)

        def gone(*args, **kwargs):
            raise runtime.WorkflowPaused("gone", reason="Could not resume the saved Claude conversation.",
                                         thread_id=None, session_unavailable=True)
            yield  # pragma: no cover

        records = []
        with mock.patch.object(claude_cli, "goal_session", side_effect=gone), \
                mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: records.append((kind, fields))):
            state = runtime._execute(workflow, {"input": "x"}, {"author_model": "claude-sonnet-5-5"}, workflow["prompts"])
        self.assertTrue(state["paused"])
        paused = [fields for kind, fields in records if kind == "workflow_paused"]
        self.assertEqual([fields.get("sessionUnavailable") for fields in paused], [True])


if __name__ == "__main__":
    unittest.main()
