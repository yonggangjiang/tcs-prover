"""How the runner records a live job's model calls (plan v3 section 1): Codex's trace for every Codex process,
session copies before every deletion, Claude Code's stream, and the author's sessions when it stops.

Offline: a stand-in Codex CLI (a Python script) writes what Codex 0.160 writes (a trace bundle under
CODEX_ROLLOUT_TRACE_ROOT, a session file under CODEX_HOME/sessions) and deletes a session on `codex delete`;
goal sessions run against the scripted app-server double of tests/test_goal_runtime.py.
"""

import ast
import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import providers, records
from ui import model_calls

import test_author_conversation as conversation_test
import test_critic_subagents as critic_test
import test_decisions_author as author_test
import test_goal_runtime as goal_test

# The runner's own emit(), which writes each record of the transcript (tests replace it in their setUp).
REAL_EMIT = runtime.emit

THREAD = "01a1d4e2-53df-78e1-b1a3-60906592b723"
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"],
          "additionalProperties": False}

# A stand-in Codex 0.160 for `codex exec --json` and `codex delete --force <id>`: it notes its environment, writes a
# trace bundle where CODEX_ROLLOUT_TRACE_ROOT says (one request and its response) and its session file, and answers.
STAND_IN = r"""
import json, os, sys, time
argv = sys.argv[1:]
state = os.environ["TCS_FAKE_STATE"]
home = os.environ.get("CODEX_HOME")
if argv[:2] == ["delete", "--force"]:
    with open(os.path.join(state, "deleted.jsonl"), "a") as out:
        out.write(json.dumps(argv[2]) + "\n")
    for folder, _, names in os.walk(os.path.join(home, "sessions")):
        for name in names:
            if argv[2] in name:
                os.unlink(os.path.join(folder, name))
    sys.exit(0)
if "exec" not in argv:
    sys.exit(0)
sys.stdin.read()
thread = os.environ.get("TCS_FAKE_THREAD", "01a1d4e2-53df-78e1-b1a3-60906592b723")
with open(os.path.join(state, "env.jsonl"), "a") as out:
    out.write(json.dumps({key: os.environ.get(key) for key in ("CODEX_ROLLOUT_TRACE_ROOT", "TCS_PROVER_RECORD")}) + "\n")
root = os.environ.get("CODEX_ROLLOUT_TRACE_ROOT")
if root:
    bundle = os.path.join(root, "trace-00000000-0000-0000-0000-000000000000-" + thread)
    os.makedirs(os.path.join(bundle, "payloads"))
    with open(os.path.join(bundle, "manifest.json"), "w") as out:
        json.dump({"schema_version": 1, "trace_id": "t", "rollout_id": thread, "root_thread_id": thread,
                   "started_at_unix_ms": 1, "raw_event_log": "trace.jsonl", "payloads_dir": "payloads"}, out, indent=2)
    with open(os.path.join(bundle, "payloads", "1.json"), "w") as out:
        json.dump({"model": "gpt-6.1-sol", "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "REQUEST-MARKER"}]}]}, out, indent=2)
    with open(os.path.join(bundle, "payloads", "2.json"), "w") as out:
        json.dump({"response_id": "resp_1", "upstream_request_id": None, "token_usage": {"input_tokens": 7},
                   "output_items": [{"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": "ANSWER-MARKER"}]}]}, out, indent=2)
    ref = lambda n, kind: {"raw_payload_id": "raw_payload:%d" % n, "kind": {"type": kind}, "path": "payloads/%d.json" % n}
    events = [{"type": "thread_started", "thread_id": thread, "agent_path": "/root", "metadata_payload": None},
              {"type": "inference_started", "inference_call_id": "i1", "thread_id": thread, "codex_turn_id": "t1",
               "model": "gpt-6.1-sol", "provider_name": "OpenAI", "request_payload": ref(1, "inference_request")},
              {"type": "inference_completed", "inference_call_id": "i1", "response_id": "resp_1",
               "upstream_request_id": None, "response_payload": ref(2, "inference_response")}]
    with open(os.path.join(bundle, "trace.jsonl"), "w") as out:
        for number, payload in enumerate(events, 1):
            out.write(json.dumps({"schema_version": 1, "seq": number, "wall_time_unix_ms": number,
                                  "rollout_id": thread, "thread_id": thread, "codex_turn_id": "t1",
                                  "payload": payload}) + "\n")
folder = os.path.join(home, "sessions", time.strftime("%Y/%m/%d"))
os.makedirs(folder, exist_ok=True)
with open(os.path.join(folder, "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S"), thread)), "w") as out:
    out.write(json.dumps({"type": "session_meta", "payload": {"id": thread, "session_id": thread}}) + "\n")
    out.write(json.dumps({"type": "response_item", "payload": {"type": "message", "text": "SESSION-MARKER"}}) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": thread}))
print(json.dumps({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "{}"}}))
answer = json.dumps({"verdict": "pass"})
open(argv[argv.index("-o") + 1], "w").write(answer)
"""


class RecordedJob(unittest.TestCase):
    """A job with a record (records.RECORD_ENV) in a live run, in the test's own folders."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "fake-state"
        self.state.mkdir()
        self.home = self.root / "codex-home"
        self.record_folder = self.root / "store" / "records" / "run-1"
        changes = [mock.patch.dict(os.environ, {
            providers.STORE_ENV: str(self.root / "store" / "api-models.json"),
            records.RECORD_ENV: str(self.record_folder), "TCS_FAKE_STATE": str(self.state),
            "CODEX_HOME": str(self.home), "TCS_PROVER_RETRIEVAL_MODE": "live"}),
            mock.patch.object(records, "_RECORDER", None), mock.patch.object(runtime, "emit", self.emit)]
        for change in changes:
            change.start()
            self.addCleanup(change.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        self.events = []
        self.addCleanup(records.close)

    def emit(self, kind, stage, **fields):
        self.events.append({"kind": kind, "stage": stage, **fields})

    def stand_in(self):
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n" + STAND_IN)
        fake.chmod(0o700)
        patcher = mock.patch.object(runtime, "codex", lambda: str(fake))
        patcher.start()
        self.addCleanup(patcher.stop)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def lines(self, name):
        try:
            return [json.loads(line) for line in (self.state / name).read_text().splitlines()]
        except FileNotFoundError:
            return []


class EnvironmentTests(RecordedJob):
    def test_no_child_ever_learns_where_the_record_is(self):
        self.assertNotIn(records.RECORD_ENV, runtime.environment())
        self.assertNotIn(records.RECORD_ENV, runtime.environment("gpt-6.1-sol"))

    def test_a_live_codex_process_gets_its_own_trace_folder_in_the_record(self):
        environment, trace = runtime._record_codex({"PATH": "/bin"}, None, role="critic")
        self.addCleanup(trace.finish)
        root = Path(environment[records.TRACE_ROOT_ENV])
        self.assertEqual(root.parent, self.record_folder / "codex-trace")
        self.assertTrue(root.is_dir())
        self.assertEqual(json.loads((self.record_folder / "codex" / root.name / "process.json").read_text())["role"],
                         "critic")

    def test_a_web_cutoff_process_is_never_traced(self):
        environment, trace = runtime._record_codex({"PATH": "/bin"}, {"_path": "policy.json"}, role="author")
        self.assertIsNone(trace)
        self.assertEqual(environment, {"PATH": "/bin"})
        self.assertFalse((self.record_folder / "codex-trace").exists())

    def test_a_job_without_a_record_traces_nothing(self):
        with mock.patch.dict(os.environ, {records.RECORD_ENV: ""}):
            self.assertEqual(runtime._record_codex({"A": "1"}, None), ({"A": "1"}, None))


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class StructuredCallTests(RecordedJob):
    def setUp(self):
        super().setUp()
        self.stand_in()

    def test_a_structured_call_is_traced_and_its_session_copied_before_it_is_deleted(self):
        with records.labelled(role="critic", node="critic", round=2, label="Critic coordinator"):
            runtime.structured("Check.", SCHEMA, "critic", model="gpt-6.1-sol")
        root = Path(self.lines("env.jsonl")[0]["CODEX_ROLLOUT_TRACE_ROOT"])
        self.assertEqual(root.parent, self.record_folder / "codex-trace")
        self.assertIsNone(self.lines("env.jsonl")[0]["TCS_PROVER_RECORD"])
        self.assertFalse(root.exists())  # Stored, then removed.
        record = records.Record(self.record_folder)
        [call] = record.calls.read()
        self.assertEqual((call["status"], call["usage"]), ("completed", {"input_tokens": 7}))
        self.assertEqual(json.loads((self.record_folder / "codex" / call["process"] / "process.json").read_text())
                         ["round"], 2)
        # The session was copied before `codex delete` removed it.
        self.assertEqual(self.lines("deleted.jsonl"), [THREAD])
        self.assertEqual(list((self.home / "sessions").rglob("*.jsonl")), [])
        [copy] = (self.record_folder / "sources" / "codex" / THREAD).rglob("*.jsonl.gz")
        self.assertIn(b"SESSION-MARKER", records.read_source(copy))

    def test_a_session_whose_copy_fails_is_not_deleted(self):
        with mock.patch.object(records.Recorder, "keep_codex_session", lambda self, home, thread: False):
            runtime.structured("Check.", SCHEMA, "critic", model="gpt-6.1-sol")
        self.assertEqual(self.lines("deleted.jsonl"), [])
        self.assertEqual(len(list((self.home / "sessions").rglob("*.jsonl"))), 1)
        diagnostics = [event["text"] for event in self.events if event["kind"] == "diagnostic"]
        self.assertTrue(any("Could not delete the saved CLI session" in text for text in diagnostics))

    def test_without_a_record_nothing_is_traced_or_copied(self):
        with mock.patch.dict(os.environ, {records.RECORD_ENV: ""}):
            runtime.structured("Check.", SCHEMA, "critic", model="gpt-6.1-sol")
        self.assertIsNone(self.lines("env.jsonl")[0]["CODEX_ROLLOUT_TRACE_ROOT"])
        self.assertEqual(self.lines("deleted.jsonl"), [THREAD])
        self.assertFalse(self.record_folder.exists())


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class MemberTests(RecordedJob):
    def setUp(self):
        super().setUp()
        self.stand_in()
        self.directory = self.root / "run"
        self.directory.mkdir()

    def test_a_team_member_s_call_is_traced_and_its_session_copied_before_the_team_deletes_it(self):
        sessions = runtime.MemberSessions()
        conversation = runtime.MemberConversation("codex", None, sessions)
        with records.labelled(role="member", member="member-1-1", label="Member 1"):
            runtime._codex_member_attempt("Work on part 1.", "gpt-6.1-sol", "high", "concise",
                                          directory=self.directory, conversation=conversation,
                                          record=lambda event, root: None, started=lambda process: None,
                                          speed="standard", compact_prompt="")
        root = Path(self.lines("env.jsonl")[0]["CODEX_ROLLOUT_TRACE_ROOT"])
        self.assertEqual(root.parent, self.record_folder / "codex-trace")
        [call] = records.Record(self.record_folder).calls.read()
        labels = json.loads((self.record_folder / "codex" / call["process"] / "process.json").read_text())
        self.assertEqual((labels["role"], labels["member"]), ("member", "member-1-1"))
        sessions.discard()  # Team.close()
        self.assertEqual(self.lines("deleted.jsonl"), [THREAD])
        self.assertEqual(len(list((self.record_folder / "sources" / "codex" / THREAD).rglob("*.jsonl.gz"))), 1)


class WebCutoffTests(RecordedJob):
    def launch(self, policy):
        seen = []

        def popen(command, **kwargs):
            seen.append(kwargs["env"])
            raise OSError("not started")

        workspace = self.root / "snapshot"
        workspace.mkdir(exist_ok=True)
        with mock.patch.object(runtime.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(runtime, "codex", return_value="codex"), \
                mock.patch.object(runtime.retrieval_runtime, "codex_executable", return_value="codex"), \
                mock.patch.object(runtime.retrieval_runtime, "temp_arguments", return_value=[]), \
                mock.patch.object(runtime.retrieval_runtime, "remove_private_temp"), \
                mock.patch.object(runtime.retrieval_runtime, "native_search_arguments", return_value=[]), \
                self.assertRaises(OSError):
            runtime._run_structured_process(
                "Prompt", {"type": "object"}, "critic", "gpt-6.1-sol", "max", "standard", "concise", timeout=60,
                activity_label=None, features=(), policy=policy, child_environment={"PATH": "/usr/bin"},
                workspace=workspace)
        return seen[-1]

    def test_a_web_cutoff_structured_call_is_never_traced_while_a_live_one_is(self):
        self.assertNotIn(records.TRACE_ROOT_ENV, self.launch({"_path": "retrieval-policy.json"}))
        self.assertFalse((self.record_folder / "codex-trace").exists())
        self.assertIn(records.TRACE_ROOT_ENV, self.launch(None))

    def test_every_codex_launch_passes_its_own_web_cutoff_policy(self):
        # Only a team member's call passes None: web-cutoff runs have no team (the server refuses Subagent models).
        source = Path(runtime.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {}
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef):
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_record_codex":
                        calls.setdefault(function.name, []).append(ast.unparse(node.args[1]))
        self.assertEqual(calls, {"_run_structured_process": ["policy"], "_codex_member_attempt": ["None"],
                                 "goal_session": ["policy"], "author_conversation": ["policy"]})


class ReplyTurnTests(RecordedJob):
    def setUp(self):
        super().setUp()
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = popen.start()
        self.addCleanup(popen.stop)
        self.copies = []
        copy = mock.patch.object(records.Recorder, "copy_codex_thread",
                                 lambda recorder, home, thread: self.copies.append(thread))
        copy.start()
        self.addCleanup(copy.stop)
        self.directory = self.root / "run"
        self.directory.mkdir()

    def test_a_reply_turn_is_traced_and_the_thread_copied_after_it(self):
        double = conversation_test.Codex([conversation_test.reply()])
        result = runtime.author_conversation(
            double, "Summarize your progress.", settings=goal_test.SETTINGS,
            options={"goal_cwd": str(self.directory), "goal_thread_id": "thread-1"}, features=["multi_agent"],
            conversation_id="c_0123456789", label=runtime.CONVERSATION_LABELS["summary"][0])
        self.assertEqual(result["threadId"], "thread-1")
        root = Path(self.spawn.call_args.kwargs["env"][records.TRACE_ROOT_ENV])
        labels = json.loads((self.record_folder / "codex" / root.name / "process.json").read_text())
        self.assertEqual((labels["role"], labels["conversationId"]), ("summary", "c_0123456789"))
        self.assertEqual(self.copies, ["thread-1"])


class RoundLabelTests(critic_test.CriticCase):
    """Each workflow step's calls carry the round the activity log names (stand-in attempts, no model)."""

    def recorded(self):
        """The (row label, labels' round) the record gives each model call the stand-in attempts answer."""

        seen, original = [], self.attempts

        def attempts(critic):
            inner = original(critic)

            def attempt(*args, **settings):
                labels = records.current_labels()
                seen.append((model_calls._role({"process": "p"}, {"p": labels})[1], labels.get("round")))
                return inner(*args, **settings)

            return attempt

        self.attempts = attempts
        return seen

    def test_the_convention_critic_names_the_critic_round_it_checks(self):
        # The critic fixes the proof in round 1 and accepts it in round 2; the convention critic then checks round 2.
        seen = self.recorded()
        self.run_critic(critic_test.fixes, {"critic_rounds": 1})
        [result] = self.kinds("convention_result")
        self.assertEqual(result["label"], "Convention critic · round 2")
        self.assertEqual(seen, [("Critic coordinator · round 1", 1), ("Critic coordinator · round 2", 2),
                                (result["label"], 2)])

    def test_a_latex_repair_names_no_round(self):
        # Each repair comes back from the compiler, so each is a first visit: no round of its own to name.
        compiles = iter([{"status": "fail", "output": "error 1", "diagnostic": "d1"},
                         {"status": "fail", "output": "error 2", "diagnostic": "d2"}, {"status": "pass", "output": ""}])

        def model(call):
            if call["stage"] == "final":
                (call["workspace"] / "latex-source.tex").write_text(critic_test.LATEX)
                return {"summary": "Edited."}
            return critic_test.critic_answer()

        seen = self.recorded()
        with mock.patch.object(runtime, "_run_command", side_effect=lambda *args, **kwargs: next(compiles)):
            self.run_critic(model, workflows=(critic_test.AUTHOR_CRITIC, critic_test.CLEAN_UP))
        self.assertEqual([entry for entry in seen if entry[0].startswith("latex_repair")],
                         [("latex_repair", None), ("latex_repair", None)])


class MemberSessionTests(unittest.TestCase):
    def test_a_member_s_sessions_go_newest_first_so_a_fork_chain_goes_whole(self):
        # Codex refuses to delete a session that a later fork still references.
        alive = ["base", "follow-up", "retry"]

        def deleter(session):
            if alive.index(session) != len(alive) - 1:
                return False
            alive.remove(session)
            return True

        sessions = runtime.MemberSessions()
        for session in list(alive):
            sessions.add(session, deleter)
        sessions.discard()
        self.assertEqual(alive, [])

    def test_a_session_that_still_cannot_go_is_tried_twice_only(self):
        tries = []
        sessions = runtime.MemberSessions()
        sessions.add("stuck", lambda session: tries.append(session) or False)
        sessions.discard()
        self.assertEqual(tries, ["stuck", "stuck"])


class GoalSessionTests(RecordedJob):
    def setUp(self):
        super().setUp()
        self.popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.copies = []
        copy = mock.patch.object(records.Recorder, "copy_codex_thread",
                                 lambda recorder, home, thread: self.copies.append((Path(home), thread)))
        copy.start()
        self.addCleanup(copy.stop)
        self.directory = self.root / "run"
        self.directory.mkdir()

    def session(self, scripts, coordinator=False):
        double = goal_test.Runtime(scripts)
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings=goal_test.SETTINGS, options={"goal_cwd": str(self.directory)},
                                       coordinator=coordinator)
        self.addCleanup(session.close)
        return double, session

    def test_the_author_s_app_server_is_traced_and_its_thread_copied_when_it_stops(self):
        double, session = self.session([goal_test.success("PROOF_FILE: proof.md")], coordinator=True)
        self.assertEqual(next(session)["outcome"], "done")
        environment = self.spawn.call_args.kwargs["env"]
        root = Path(environment[records.TRACE_ROOT_ENV])
        self.assertEqual(root.parent, self.record_folder / "codex-trace")
        self.assertEqual(json.loads((self.record_folder / "codex" / root.name / "process.json").read_text())["role"],
                         "coordinator")
        self.assertEqual(self.copies, [])
        session.close()  # The end of the work.
        self.assertEqual([thread for _, thread in self.copies], ["thread-1"])
        self.assertFalse(root.exists())

    def test_a_model_call_failure_that_is_retried_copies_nothing_yet(self):
        double, session = self.session([[runtime.Error("stream disconnected before completion")]])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(session)
        self.assertTrue(paused.exception.retry)
        self.assertEqual(self.copies, [])


# Codex's answer to config/read, as the first real job got it at each start of an app-server, in small: the settings
# TCS Prover sets or relies on, and the user's own (projects, MCP servers with their environment and headers, plugins,
# apps, providers, shell variables, ...), with where each setting came from.
CONFIG_ANSWER = {
    "config": {
        "model": "gpt-6-astra", "model_provider": None, "model_reasoning_effort": "ultra",
        "model_reasoning_summary": None, "service_tier": "priority", "approval_policy": "never",
        "sandbox_mode": "workspace-write", "sandbox_workspace_write": {"writable_roots": [], "network_access": True},
        "default_permissions": "tcs_prover_private",
        "permissions": {"tcs_prover_private": {"filesystem": {":workspace_roots": {".": "write"}}, "network": None},
                        "users-own": {"filesystem": {"/Users/someone/clients": "write"}}},
        "web_search": "disabled", "tools": {"web_search": None, "view_image": True},
        "model_auto_compact_token_limit_scope": "body_after_prefix",
        "agents": {"max_depth": 1, "max_concurrent_threads_per_session": 3,
                   "researcher": {"config_file": "/Users/someone/agents/researcher.toml"}},
        "features": {"goals": False, "multi_agent": False, "multi_agent_v2": {"max_concurrent_threads_per_session": 1},
                     "fast_mode": False, "js_repl": True, "remote_plugin": True, "memories": False},
        "mcp_servers": {"node_repl": {"command": "node_repl", "env": {"BROWSER_TOKEN": "MCP-ENV-SECRET"}},
                        "docs": {"url": "https://docs.example",
                                 "http_headers": {"Authorization": "Bearer MCP-HEADER-SECRET"}}},
        "projects": {"/Users/someone/private-client": {"trust_level": "trusted"}},
        "plugins": {"slack@openai-curated": {"enabled": True}},
        "apps": {"calendar": {"enabled": True}},
        "model_providers": {"gateway": {"base_url": "https://gateway.example",
                                        "http_headers": {"X-Key": "PROVIDER-HEADER-SECRET"}}},
        "shell_environment_policy": {"set": {"DEPLOY_TOKEN": "SHELL-ENV-SECRET"}},
        "notify": ["/Users/someone/bin/notify"], "tui": {"theme": "dark"},
    },
    "origins": {
        "features.goals": {"name": {"type": "sessionFlags"}, "version": "sha256:1"},
        # A workflow's feature: TCS Prover's command line sets it.
        "features.js_repl": {"name": {"type": "sessionFlags"}, "version": "sha256:1"},
        "features.remote_plugin": {"name": {"type": "user", "file": "/Users/someone/.codex/config.toml"},
                                   "version": "sha256:2"},
        "projects./Users/someone/private-client.trust_level": {
            "name": {"type": "user", "file": "/Users/someone/.codex/config.toml"}, "version": "sha256:2"},
    },
    "layers": [{"name": {"type": "user", "file": "/Users/someone/.codex/config.toml"},
                "config": {"projects": {"/Users/someone/private-client": {"trust_level": "trusted"}}}}],
}
KEPT_SETTINGS = {
    "model": "gpt-6-astra", "model_provider": None, "model_reasoning_effort": "ultra", "model_reasoning_summary": None,
    "service_tier": "priority", "approval_policy": "never", "sandbox_mode": "workspace-write",
    "sandbox_workspace_write": {"writable_roots": [], "network_access": True},
    "default_permissions": "tcs_prover_private",
    "permissions": {"tcs_prover_private": {"filesystem": {":workspace_roots": {".": "write"}}, "network": None}},
    "web_search": "disabled", "tools": {"web_search": None},
    "model_auto_compact_token_limit_scope": "body_after_prefix",
    "agents": {"max_depth": 1, "max_concurrent_threads_per_session": 3},
    "features": {"goals": False, "multi_agent": False, "multi_agent_v2": {"max_concurrent_threads_per_session": 1},
                 "fast_mode": False, "js_repl": True},
    "mcp_servers": ["docs", "node_repl"],
}
LEFT_OUT = ("TCS Prover records the Codex settings the run relies on and every setting it gave this app-server on its "
            "command line: 7 other settings of the configuration, the other entries of the tables kept in part, and "
            "where each setting came from were left out.")
# What the user's configuration holds that the run does not need: none of it reaches the transcript.
PRIVATE = ("MCP-ENV-SECRET", "MCP-HEADER-SECRET", "PROVIDER-HEADER-SECRET", "SHELL-ENV-SECRET", "private-client",
           "slack@openai-curated", "researcher.toml", "users-own", "remote_plugin", "notify")


class SettingsRecordTests(unittest.TestCase):
    """Codex's answer to config/read holds the user's whole Codex configuration: the transcript keeps only the
    settings TCS Prover sets or relies on (workflow_runner.recorded_settings()), while TCS Prover acts on all of it."""

    def rpc(self, *messages):
        process = SimpleNamespace(stdout=io.StringIO("".join(json.dumps(message) + "\n" for message in messages)),
                                  stdin=io.StringIO(), stderr=None)
        recorded = []
        return runtime.RPC(process, recorded.append), recorded

    def test_the_answer_to_config_read_is_recorded_with_only_the_settings_that_matter_to_the_run(self):
        rpc, recorded = self.rpc({"id": 1, "result": CONFIG_ANSWER})
        self.assertEqual(rpc.call("config/read", {"cwd": "/work"}), CONFIG_ANSWER)  # The answer itself, whole.
        [message] = recorded
        self.assertEqual(message, {"id": 1, "result": {"config": KEPT_SETTINGS, "leftOut": LEFT_OUT}})
        for private in PRIVATE:
            self.assertNotIn(private, json.dumps(message), private)

    def test_a_check_s_answer_is_recorded_the_same_way_and_any_other_message_as_it_came(self):
        # A silent author under TCS Prover's goal loop is checked with config/read, answered later (rpc.request()).
        started = {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "turn-1"}}}
        rpc, recorded = self.rpc(started, {"id": 1, "result": CONFIG_ANSWER}, {"id": 2, "result": {"thread": {}}},
                                 {"id": 3, "error": {"code": -32603, "message": "busy"}})
        for method in ("config/read", "thread/start", "config/read"):
            rpc.request(method, {})
        self.assertEqual([rpc.read() for _ in range(4)][1], {"id": 1, "result": CONFIG_ANSWER})
        self.assertEqual(recorded, [started, {"id": 1, "result": {"config": KEPT_SETTINGS, "leftOut": LEFT_OUT}},
                                    {"id": 2, "result": {"thread": {}}},
                                    {"id": 3, "error": {"code": -32603, "message": "busy"}}])
        self.assertEqual(rpc.methods, {})

    def test_every_setting_tcs_prover_s_command_line_gave_the_app_server_is_kept(self):
        # An API connection's model: provider_arguments() gives its app-server the context window, the compaction limit,
        # TCS Prover's own provider entry and the variables its commands never inherit, which Codex 0.160 reports with
        # these origins (captured from the real app-server's config/read with those flags).
        answer = copy.deepcopy(CONFIG_ANSWER)
        flags = {"name": {"type": "sessionFlags"}, "version": "sha256:3"}
        user = {"name": {"type": "user", "file": "/Users/someone/.codex/config.toml"}, "version": "sha256:2"}
        provider = {"name": "TCS Prover", "base_url": "https://gateway.example/v1", "model_catalog_url": None,
                    "env_key": "TCS_PROVER_API_TOKEN", "experimental_bearer_token": None, "wire_api": "responses",
                    "http_headers": {"User-Agent": "TCS-Prover"}, "env_http_headers": None,
                    "requires_openai_auth": False, "supports_websockets": False,
                    "supports_standalone_web_search": False}
        answer["config"].update(
            model_provider="tcs_api", model_context_window=200000, model_auto_compact_token_limit=147200,
            suppress_unstable_features_warning=True,
            model_providers={**answer["config"]["model_providers"], "tcs_api": provider},
            shell_environment_policy={"inherit": None, "ignore_default_excludes": False,
                                      "exclude": ["TCS_PROVER_API_TOKEN", "OPENAI_API_KEY"],
                                      "set": {"DEPLOY_TOKEN": "SHELL-ENV-SECRET"}})
        answer["origins"].update({path: flags for path in (
            "model_provider", "model_context_window", "model_auto_compact_token_limit",
            "suppress_unstable_features_warning", "model_providers.tcs_api.name", "model_providers.tcs_api.base_url",
            "model_providers.tcs_api.env_key", "model_providers.tcs_api.wire_api",
            "model_providers.tcs_api.http_headers.User-Agent", "model_providers.tcs_api.requires_openai_auth",
            "model_providers.tcs_api.supports_websockets", "shell_environment_policy.ignore_default_excludes",
            "shell_environment_policy.exclude.0", "shell_environment_policy.exclude.1",
            # A definition of an MCP server is never kept, whoever set it.
            "mcp_servers.node_repl.command", "mcp_servers.node_repl.env.BROWSER_TOKEN")})
        answer["origins"].update({"shell_environment_policy.set.DEPLOY_TOKEN": user,
                                  "model_providers.gateway.base_url": user,
                                  "model_providers.gateway.http_headers.X-Key": user})
        kept = runtime.recorded_settings(answer)
        self.assertEqual(kept["config"], {
            **KEPT_SETTINGS, "model_provider": "tcs_api", "model_context_window": 200000,
            "model_auto_compact_token_limit": 147200, "suppress_unstable_features_warning": True,
            "model_providers": {"tcs_api": {key: provider[key] for key in (
                "name", "base_url", "env_key", "wire_api", "http_headers", "requires_openai_auth",
                "supports_websockets")}},
            "shell_environment_policy": {"ignore_default_excludes": False,
                                         "exclude": ["TCS_PROVER_API_TOKEN", "OPENAI_API_KEY"]}})
        self.assertEqual(kept["leftOut"], LEFT_OUT.replace("7 other settings", "5 other settings"))
        for private in PRIVATE:
            self.assertNotIn(private, json.dumps(kept), private)

    def test_the_context_window_and_the_compaction_limit_are_kept_whoever_set_them(self):
        # A built-in model's are the user's settings, or none (Codex's own for the model).
        answer = copy.deepcopy(CONFIG_ANSWER)
        answer["config"].update(model_context_window=400000, model_auto_compact_token_limit=None)
        answer["origins"]["model_context_window"] = {
            "name": {"type": "user", "file": "/Users/someone/.codex/config.toml"}, "version": "sha256:2"}
        kept = runtime.recorded_settings(answer)["config"]
        self.assertEqual((kept["model_context_window"], kept["model_auto_compact_token_limit"]), (400000, None))

    def test_tcs_prover_s_redaction_runs_over_what_is_kept(self):
        secret = "registered-key-0d6f1a9c2b7e"
        runtime.register_secret(secret)
        self.addCleanup(runtime._REGISTERED_SECRETS.discard, secret)
        answer = copy.deepcopy(CONFIG_ANSWER)
        answer["config"]["mcp_servers"][f"gateway-{secret}"] = {"command": "gateway"}
        answer["config"]["model_provider"] = f"provider-{secret}"
        kept = runtime.recorded_settings(answer)
        self.assertEqual((kept["config"]["model_provider"], kept["config"]["mcp_servers"]),
                         ("provider-[redacted]", ["docs", "gateway-[redacted]", "node_repl"]))
        self.assertNotIn(secret, json.dumps(kept))


class SettingsTranscriptTests(author_test.PipeCase):
    """The author's app-server, its real RPC class and the runner's own emit(): what reaches the transcript."""

    def test_the_transcript_keeps_only_what_matters_while_codex_still_gets_every_user_extension_turned_off(self):
        secret = "registered-key-5b1e0c7d9a3f"
        runtime.register_secret(secret)
        self.addCleanup(runtime._REGISTERED_SECRETS.discard, secret)
        answer = copy.deepcopy(CONFIG_ANSWER)
        answer["config"]["mcp_servers"][f"gateway-{secret}"] = {"command": "gateway", "env": {"KEY": secret}}

        def handler(server_double, message):
            if message["method"] == "config/read":
                server_double.reply(message, answer)
            elif message["method"] == "turn/start":
                server_double.reply(message, {"turn": {"id": "turn-1"}})
                server_double.write({"method": "turn/started", "params": {"threadId": "thread-1",
                                                                          "turn": {"id": "turn-1"}}})
                for item in author_test.completion("thread-1", "turn-1"):
                    server_double.write(item)
            else:
                author_test.answer_setup(server_double, message)

        self.handlers.append(handler)
        transcript = io.StringIO()
        with mock.patch.object(runtime, "emit", REAL_EMIT), contextlib.redirect_stdout(transcript):
            self.assertEqual(next(self.session())["outcome"], "done")
        lines = transcript.getvalue().splitlines()
        settings = [line for line in lines if '"leftOut"' in line]
        self.assertEqual(len(settings), 1)
        record = json.loads(settings[0])
        self.assertEqual((record["kind"], record["stage"], record["root"]), ("codex_event", "solve", True))
        self.assertEqual(record["event"]["result"]["config"]["mcp_servers"],
                         ["docs", "gateway-[redacted]", "node_repl"])
        self.assertLess(len(settings[0]), 3000)
        for private in (*PRIVATE, secret):
            self.assertNotIn(private, transcript.getvalue(), private)
        # TCS Prover acted on the whole answer: each of the user's MCP servers, plugins and apps is off by name.
        start = next(message["params"]["config"] for message in self.servers[0].received
                     if message["method"] == "thread/start")
        self.assertEqual((start["mcp_servers"], start["plugins"], start["apps"]),
                         ({"node_repl": {"enabled": False}, "docs": {"enabled": False},
                           f"gateway-{secret}": {"enabled": False}}, {"slack@openai-curated": {"enabled": False}},
                          {"_default": {"enabled": False}, "calendar": {"enabled": False}}))


def transcript_line(entry):
    """One transcript line as the server writes it (App.add_trace())."""

    return json.dumps(entry, ensure_ascii=False) + "\n"


def settings_line(identity, answer, stage="solve", **extra):
    """A transcript line of an app-server's answer to config/read, as a runner from before 14 Nov 2026 recorded it
    (``answer`` whole) or as the runner records it now (what recorded_settings() kept)."""

    return transcript_line({"kind": "codex_event", "stage": stage, "node": "author", **extra,
                            "event": {"id": identity, "result": answer}, "root": True,
                            "time": "2026-11-14T03:05:17.064817+00:00"})


class SettingsCopyTests(unittest.TestCase):
    """A transcript written before 14 Nov 2026 holds Codex's whole answers to config/read (the user's whole Codex
    configuration, MCP servers' environments and headers included): the record's snapshot of it and the training
    download keep of each only what the runner keeps of one now (records.recorded_transcript()); the run folder's own
    transcript.jsonl is left as it is."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        change = mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.root / "state" / "api-models.json")})
        change.start()
        self.addCleanup(change.stop)
        self.record = records.Record.create(records.record_folder("run-1"), title="A statement")
        self.transcript = self.root / "run" / "transcript.jsonl"
        self.transcript.parent.mkdir()
        # As the first real job's transcript has them: the author's start, a reply turn's start, other events.
        self.older = [
            transcript_line({"kind": "status", "stage": "solve", "label": "Workflow step: author", "time": "t"}),
            settings_line(2, CONFIG_ANSWER),
            transcript_line({"kind": "codex_event", "stage": "solve", "node": "author",
                             "event": {"id": 3, "result": {"thread": {"id": THREAD}}}, "root": True, "time": "t"}),
            # Written by another writer, compact: kept byte for byte, as is every line that is not such an answer.
            '{"kind":"diagnostic","stage":"solve","text":"Codex read its config: ∀ x, ünïcode"}\n',
            transcript_line({"kind": "codex_event", "stage": "solve", "event": {"id": 4, "error": {
                "code": -32603, "message": "config/read failed"}}, "time": "t"}),
            settings_line(2, CONFIG_ANSWER, stage="conversation", conversationId="c_760e51ded6"),
            # What the runner records now: kept as it is.
            settings_line(5, runtime.recorded_settings(CONFIG_ANSWER)),
        ]

    def kept(self, lines):
        """``lines`` as the record keeps them: each whole answer's result is what the runner records now."""

        kept = []
        for text in lines:
            entry = json.loads(text)
            result = entry.get("event", {}).get("result") if isinstance(entry.get("event"), dict) else None
            if isinstance(result, dict) and "origins" in result:
                entry["event"]["result"] = runtime.recorded_settings(result)
                text = transcript_line(entry)
            kept.append(text)
        return "".join(kept).encode("utf-8")

    def assert_private_left_out(self, data):
        for private in PRIVATE:
            self.assertNotIn(private.encode(), data, private)

    def test_a_snapshot_keeps_of_each_whole_answer_only_what_the_runner_keeps_now(self):
        self.transcript.write_text("".join(self.older), encoding="utf-8")
        records.snapshot(self.record, self.transcript)
        data = records.transcript_data(self.record)
        self.assertEqual(data, self.kept(self.older))
        self.assert_private_left_out(data)
        self.assertLess(len(data), len("".join(self.older).encode()))
        # The lines appended later (a liveness probe's answer, say) are kept the same way.
        later = [settings_line(7, CONFIG_ANSWER), transcript_line({"kind": "status", "stage": "solve", "time": "u"})]
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write("".join(later))
        records.snapshot(self.record, self.transcript)
        data = records.transcript_data(self.record)
        self.assertEqual(data, self.kept(self.older + later))
        self.assert_private_left_out(data)
        # The run folder's own transcript is left as it is.
        self.assertEqual(self.transcript.read_text(encoding="utf-8"), "".join(self.older + later))

    def test_a_snapshot_taken_before_is_copied_whole_again_with_only_those_settings(self):
        # As a snapshot of an earlier build left it: the transcript as it came, and its state without "settings".
        data = "".join(self.older).encode()
        self.transcript.write_bytes(data)
        folder = self.record.folder / "transcript"
        records.write_atomic(folder / "transcript.jsonl.gz", gzip.compress(data, mtime=0))
        records.write_atomic(folder / "transcript.json", json.dumps({
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "size": (folder / "transcript.jsonl.gz").stat().st_size}).encode())
        later = [transcript_line({"kind": "status", "stage": "solve", "time": "u"})]
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write("".join(later))
        records.snapshot(self.record, self.transcript)
        kept = records.transcript_data(self.record)
        self.assertEqual(kept, self.kept(self.older + later))
        self.assert_private_left_out((folder / "transcript.jsonl.gz").read_bytes())
        self.assert_private_left_out(gzip.decompress((folder / "transcript.jsonl.gz").read_bytes()))
        self.assertEqual(json.loads((folder / "transcript.json").read_text())["settings"], records.SNAPSHOT_SETTINGS)

    def test_the_download_of_a_snapshot_taken_before_keeps_only_those_settings(self):
        data = "".join(self.older).encode()
        folder = self.record.folder / "transcript"
        records.write_atomic(folder / "transcript.jsonl.gz", gzip.compress(data, mtime=0))
        buffer = io.BytesIO()
        model_calls.training_record("run-1", buffer)
        archive = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        kept = archive.read("transcript/transcript.jsonl")
        self.assertEqual(kept, self.kept(self.older))
        for name in archive.namelist():
            self.assert_private_left_out(archive.read(name))

    def test_only_a_whole_answer_to_config_read_is_kept_in_part(self):
        answer = {"id": 2, "result": CONFIG_ANSWER}
        self.assertTrue(records.settings_answer(answer))
        # An answer as the runner records it now, a notification, a request of Codex and an error are no such answer.
        for event in ({"id": 2, "result": runtime.recorded_settings(CONFIG_ANSWER)},
                      {"method": "config/warning", "params": {"config": {}}},
                      {"id": 9, "method": "item/tool/requestUserInput", "result": CONFIG_ANSWER},
                      {"id": 2, "error": {"code": 1, "message": "no"}}, {"id": 2, "result": {"thread": {}}}):
            self.assertFalse(records.settings_answer(event), event)
        # An answer without its origins (fields of config/read only) is one; another answer with a "config" is not.
        self.assertTrue(records.settings_answer({"id": 2, "result": {"config": {"model": "m"}}}))
        self.assertFalse(records.settings_answer({"id": 2, "result": {"config": {}, "thread": {}}}))
        # A lone surrogate (from a JSON escape), which the server writes escaped, stays escaped.
        entry = json.loads(settings_line(2, copy.deepcopy(CONFIG_ANSWER)))
        entry["event"]["result"]["config"]["model"] = "m\ud800"
        kept = records.recorded_transcript((json.dumps(entry) + "\n").encode("ascii"))
        self.assertIn(b'"model": "m\\ud800"', kept)
        self.assertEqual(set(json.loads(kept)["event"]["result"]), {"config", "leftOut"})


if __name__ == "__main__":
    unittest.main()
