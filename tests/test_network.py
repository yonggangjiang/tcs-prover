"""Internet access of every model process in a live run, and of none in a web-cutoff run; no model calls.

Network follows the run's search mode and nothing else (workflow_runner.live_network()): in a live
run the sandbox of every Codex process allows it (the author, its conversation turns and the
time-limit summary, the statement reviewer, the critics, the coordinator, the final verification,
the LaTeX writer and its repairs), as Claude's Bash sandbox does (claude_cli.sandbox_settings());
with a web cutoff (-1, or a timestamp) none does, whatever the user's Codex settings say. The statement
reviewer and Claude's roles other than the author have no shell: they use the web only through
their search tools. Elsewhere than macOS Codex keeps its legacy sandbox modes, whose network
settings follow the same rule except for `codex exec`'s read-only mode, which has none. No model
process, on any platform, is told where the user's SSH and GPG agents listen. Popen, the Codex
app-server and the Claude CLI are stand-ins. The live counterparts, which run these profiles in
Codex's sandbox, are in tests/test_provider_cli_live.py (TCS_PROVER_LIVE_CLI_TESTS=1).
"""

import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, confinement
import test_claude_cli as claude_test
import test_goal_runtime as goal_test

MODEL = "gpt-6.1-sol"
NETWORK = f"permissions.{runtime.PRIVATE_PERMISSIONS}.network"
# Where the user's SSH and GPG agents listen, as a login shell would say.
AGENTS = {name: f"/private/tmp/agent-{name.lower()}" for name in confinement.AGENT_VARIABLES}
# The probes of the author's startup check: the filesystem only, never the network.
CHECK_PROBES = {"workspaceRead", "workspaceWrite", "tempWrite", "outsideRead", "checkoutRead", "slashTmpWrite",
                "parentList"}


def overrides(command):
    """The -c settings of a command line, by key."""

    return dict(value.split("=", 1) for flag, value in zip(command, command[1:]) if flag == "-c")


class Folders(unittest.TestCase):
    """A run folder, the current folder, with the search mode of each test."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.workspace = self.root / "runs" / "run"
        self.workspace.mkdir(parents=True)
        environment = mock.patch.dict(os.environ, {retrieval_runtime.MODE_ENV: "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(retrieval_runtime.POLICY_ENV, None)
        previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)

    def mode(self, mode):
        return mock.patch.dict(os.environ, {retrieval_runtime.MODE_ENV: mode})


class LiveNetworkTests(Folders):
    def test_network_follows_the_search_mode_and_nothing_else(self):
        for mode, expected in (("live", True), ("disabled", False)):
            with self.subTest(mode=mode), self.mode(mode):
                self.assertIs(runtime.live_network(), expected)
                self.assertIs(runtime.live_network(self.workspace), expected)
        # A historical (timestamp) policy: never, whatever mode the environment names.
        self.assertFalse(runtime.live_network(self.workspace, {"mode": "historical"}))
        # Saved settings of a run started without the controller's mode, as native search reads them.
        os.environ.pop(retrieval_runtime.MODE_ENV)
        self.assertTrue(runtime.live_network(self.workspace))  # No saved settings: main's live default.
        for saved, expected in (({"retrievalMode": "disabled"}, False), ({"retrievalMode": "live"}, True)):
            with self.subTest(saved=saved):
                (self.workspace / "job-settings.json").write_text(json.dumps(saved))
                self.assertIs(runtime.live_network(self.workspace), expected)

    def test_an_unknown_mode_and_unreadable_settings_start_nothing(self):
        with self.mode("everything"), self.assertRaises(runtime.ConfigurationError):
            runtime.live_network(self.workspace)
        unavailable = retrieval_runtime.SettingsUnavailable("Cannot verify saved run retrieval settings.")
        with mock.patch.object(retrieval_runtime, "search_mode", side_effect=unavailable), \
                self.assertRaises(runtime.Error) as raised:
            runtime.live_network(self.workspace)
        # Retried like a failed model call, not a settings problem.
        self.assertNotIsInstance(raised.exception, runtime.ConfigurationError)


class CodexProfileTests(Folders):
    def profile(self, base, live):
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            return runtime.private_permissions(base, MODEL, live=live)

    def test_every_base_reaches_the_internet_in_a_live_run_and_none_otherwise(self):
        for base in (":workspace", ":read-only"):
            with self.subTest(base=base):
                live, offline = self.profile(base, True), self.profile(base, False)
                self.assertEqual(live["network"], {"enabled": True})
                self.assertNotIn("network", offline)
                self.assertEqual(live["filesystem"], offline["filesystem"])

    @unittest.skipIf(tomllib is None, "tomllib is needed to parse Codex overrides")
    def test_the_flags_define_the_network_table_of_a_live_profile_only(self):
        for base in (":workspace", ":read-only"):
            for live in (True, False):
                with self.subTest(base=base, live=live), mock.patch.object(runtime.sys, "platform", "darwin"):
                    flags = overrides(runtime.private_permission_arguments(base, MODEL, live=live))
                    self.assertEqual(NETWORK in flags, live)
                    if live:
                        self.assertEqual(tomllib.loads(f"v = {flags[NETWORK]}")["v"], {"enabled": True})

    def test_the_legacy_sandbox_gets_the_network_setting_of_the_search_mode(self):
        self.assertEqual(runtime.legacy_network_arguments(True), ["-c", "sandbox_workspace_write.network_access=true"])
        self.assertEqual(runtime.legacy_network_arguments(False), ["-c", "sandbox_workspace_write.network_access=false"])
        # The app-server's read-only policy, which a conversation turn sets (Codex 0.160's SandboxPolicy).
        self.assertEqual(runtime.legacy_read_only_policy(True), {"type": "readOnly", "networkAccess": True})
        self.assertEqual(runtime.legacy_read_only_policy(False), {"type": "readOnly", "networkAccess": False})


class StructuredCallTests(Folders):
    """Every structured role runs through _run_structured_process: read-only calls and calls that edit a file."""

    def launch(self, platform, writable, child_environment):
        """The command line and environment of one call."""

        seen = []

        def popen(command, **kwargs):
            seen.append((command, kwargs.get("env")))
            raise OSError("not started")

        with mock.patch.object(runtime.sys, "platform", platform), \
                mock.patch.object(runtime.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(runtime, "codex", return_value="codex"), self.assertRaises(OSError):
            runtime._run_structured_process(
                "Prompt", {"type": "object"}, "critic", MODEL, "max", "standard", "concise", timeout=60,
                activity_label=None, features=(), policy=None, child_environment=child_environment,
                workspace=self.workspace, writable=writable)
        return seen[-1]

    def command(self, platform, writable):
        return self.launch(platform, writable, runtime.environment(MODEL))[0]

    def test_on_macos_every_call_reaches_the_internet_in_a_live_run_only(self):
        for mode in ("live", "disabled"):
            for writable in (False, True):
                with self.subTest(mode=mode, writable=writable), self.mode(mode):
                    command = self.command("darwin", writable)
                    settings = overrides(command)
                    self.assertEqual(settings.get(NETWORK), '{"enabled"=true}' if mode == "live" else None)
                    # Native search follows the same mode.
                    self.assertEqual(settings["web_search"], f'"{mode}"')
                    self.assertNotIn("sandbox_workspace_write.network_access", settings)

    def test_elsewhere_a_call_that_edits_a_file_gets_the_legacy_network_setting(self):
        for mode in ("live", "disabled"):
            for writable in (False, True):
                with self.subTest(mode=mode, writable=writable), self.mode(mode):
                    command = self.command("linux", writable)
                    pairs = list(zip(command, command[1:]))
                    settings = overrides(command)
                    self.assertNotIn(NETWORK, settings)
                    if writable:
                        self.assertIn(("-s", "workspace-write"), pairs)
                        self.assertEqual(settings["sandbox_workspace_write.network_access"],
                                         "true" if mode == "live" else "false")
                    else:
                        # `codex exec`'s legacy read-only mode has no network setting: such a call stays offline.
                        self.assertIn(("-s", "read-only"), pairs)
                        self.assertNotIn("sandbox_workspace_write.network_access", settings)

    def test_no_call_is_told_where_the_users_agents_listen_on_any_platform(self):
        """With network a command could use the agents' sockets: the legacy sandboxes drop the variables too."""

        for platform in ("darwin", "linux", "win32"):
            for mode in ("live", "disabled"):
                for writable in (False, True):
                    # The runner's environment, and none (the call inherits the runner's).
                    for inherited in (True, False):
                        with self.subTest(platform=platform, mode=mode, writable=writable, inherited=inherited), \
                                self.mode(mode), mock.patch.dict(os.environ, AGENTS):
                            command, environment = self.launch(
                                platform, writable, None if inherited else runtime.environment(MODEL))
                            self.assertIsNotNone(environment)
                            self.assertFalse(set(AGENTS) & set(environment))
                            self.assertEqual(environment["PATH"], os.environ["PATH"])
                            if platform != "darwin" and writable:
                                self.assertEqual(overrides(command)["sandbox_workspace_write.network_access"],
                                                 "true" if mode == "live" else "false")

    def test_each_kind_of_role_call_reaches_the_internet_in_a_live_run(self):
        """The reviewer (no files), a critic (a read-only proof snapshot), the coordinator and the writer (an edit)."""

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                  "additionalProperties": False}
        bundle = (("proof.md", "Proof."), ("statement.md", "Claim."), ("manifest.json", "{}\n"))
        calls = lambda: {  # noqa: E731 - each call edits a file of its own
            "statement reviewer": {},
            "critic": {"input_files": bundle},
            "coordinator": {"input_files": bundle, "edit_file": runtime.EditableFile("proof.md", "proof", "Proof.")},
            "LaTeX writer": {"edit_file": runtime.EditableFile("latex-source.tex", "latex", "Text.")},
        }
        for mode in ("live", "disabled"):
            for role, keywords in calls().items():
                seen = []

                def popen(command, **kwargs):
                    seen.append(command)
                    raise OSError("not started")

                with self.subTest(mode=mode, role=role), self.mode(mode), \
                        mock.patch.object(runtime.sys, "platform", "darwin"), \
                        mock.patch.object(runtime.subprocess, "Popen", side_effect=popen), \
                        mock.patch.object(runtime, "codex", return_value="codex"), \
                        mock.patch.object(runtime, "emit"), self.assertRaises(Exception):
                    runtime.structured("Check.", schema, "critic", model=MODEL, attempts=1, **keywords)
                with self.subTest(mode=mode, role=role):
                    self.assertTrue(seen)
                    settings = overrides(seen[-1])
                    self.assertEqual(NETWORK in settings, mode == "live")
                    roots = settings[f"permissions.{runtime.PRIVATE_PERMISSIONS}.filesystem"]
                    self.assertIn('"."="write"' if "edit_file" in keywords else '"."="read"', roots)

    def test_a_web_cutoff_call_keeps_its_audited_profile(self):
        with mock.patch.object(retrieval_runtime, "temp_arguments", return_value=["-c", "audited=true"]), \
                mock.patch.object(retrieval_runtime, "remove_private_temp"), \
                mock.patch.object(runtime, "live_network", side_effect=AssertionError("asked")), \
                mock.patch.object(runtime.sys, "platform", "darwin"):
            with runtime._confined_call(True, MODEL, {"PATH": "/usr/bin"}, {"mode": "historical"},
                                        self.workspace) as (flags, _):
                self.assertEqual(flags, ["-c", "audited=true"])


class AuthorDouble(goal_test.Runtime):
    """A Codex author whose app-server answers its settings read and its startup check."""

    Error = runtime.Error  # Failed attempts are retried as with the real runner.
    environment = staticmethod(lambda model: {"PATH": "/usr/bin"})
    private_permissions = staticmethod(runtime.private_permissions)
    author_confinement_check = staticmethod(runtime.author_confinement_check)
    settings = {}

    def RPC(self, process, record):
        rpc = super().RPC(process, record)
        call = rpc.call

        def answer(method, params):
            if method == "config/read":
                rpc.calls.append((method, params))
                return {"config": self.settings}
            if method == "command/exec":
                rpc.calls.append((method, params))
                return goal_test.sandbox_check(params, getattr(self, "sandbox_writable", True))
            return call(method, params)

        rpc.call = answer
        return rpc


class CodexAuthorTests(Folders):
    def setUp(self):
        super().setUp()
        self.run_tmp = self.root / "runs" / ".controller" / "run" / "tmp"
        self.run_tmp.parent.mkdir(parents=True)
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = popen.start()
        self.addCleanup(popen.stop)

    def goal(self, double):
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings={**goal_test.SETTINGS, "model": MODEL},
                                       options={"goal_cwd": str(self.workspace), "run_tmp_dir": str(self.run_tmp)})
        self.addCleanup(session.close)
        self.assertEqual(next(session)["outcome"], "done")
        return next(params for method, params in double.rpc.calls if method == "thread/start")

    def converse(self, double, label):
        double.sandbox_writable = False
        runtime.author_conversation(
            double, "Summarize.", settings={**goal_test.SETTINGS, "model": MODEL},
            options={"goal_cwd": str(self.workspace), "goal_thread_id": "thread-1", "run_tmp_dir": str(self.run_tmp)},
            features=["multi_agent"], conversation_id="c" * 32, label=label)
        return next(params for method, params in double.rpc.calls if method == "thread/resume")

    @staticmethod
    def turn(double):
        return next(params for method, params in double.rpc.calls if method == "turn/start")

    def test_a_live_author_reaches_the_internet_whatever_the_users_codex_settings_say(self):
        for network_access in (False, True, None):
            double = AuthorDouble([goal_test.success()])
            double.settings = {} if network_access is None else {
                "sandbox_workspace_write": {"network_access": network_access, "writable_roots": ["/elsewhere"]}}
            with self.subTest(network_access=network_access), mock.patch.object(runtime.sys, "platform", "darwin"):
                start = self.goal(double)
                profile = start["config"]["permissions"][runtime.PRIVATE_PERMISSIONS]
                self.assertEqual(profile["network"], {"enabled": True})
                self.assertNotIn("/elsewhere", profile["filesystem"])
                # The app-server defines the same profile, network included.
                self.assertEqual(overrides(self.spawn.call_args.args[0])[NETWORK], '{"enabled"=true}')
                self.assertEqual((start["config"]["web_search"], start["config"]["tools"]["web_search"]), ("live", True))
                # Its MCP servers, plugins and apps stay off, and its agents stay unnamed.
                self.assertEqual((start["config"]["features"]["apps"], start["config"]["features"]["plugins"]),
                                 (False, False))
                self.assertFalse({"SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO"}
                                 & set(self.spawn.call_args.kwargs["env"]))

    def test_with_a_web_cutoff_of_minus_one_the_author_stays_offline_even_with_network_access_on(self):
        double = AuthorDouble([goal_test.success()])
        double.settings = {"sandbox_workspace_write": {"network_access": True}}
        with self.mode("disabled"), mock.patch.object(runtime.sys, "platform", "darwin"):
            start = self.goal(double)
        self.assertNotIn("network", start["config"]["permissions"][runtime.PRIVATE_PERMISSIONS])
        self.assertNotIn(NETWORK, overrides(self.spawn.call_args.args[0]))
        self.assertEqual(start["config"]["web_search"], "disabled")

    def test_the_startup_check_never_needs_the_internet(self):
        double = AuthorDouble([goal_test.success()])
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            self.goal(double)
        check = next(params for method, params in double.rpc.calls if method == "command/exec")
        probes = set(re.findall(r"then (\w+)=true", check["command"][-1]))
        self.assertTrue(probes <= CHECK_PROBES, probes - CHECK_PROBES)
        self.assertIn("Sandbox checked", [event.get("label") for event in double.events])

    def test_conversation_turns_and_the_time_limit_summary_follow_the_search_mode(self):
        labels = (runtime.CONVERSATION_LABELS["summary"][0], runtime.CONVERSATION_LABELS["message"][0])
        for mode in ("live", "disabled"):
            for label in labels:
                double = AuthorDouble([[goal_test.answer("Lemma 2 is open."), goal_test.completed()]])
                with self.subTest(mode=mode, label=label), self.mode(mode), \
                        mock.patch.object(runtime.sys, "platform", "darwin"):
                    resumed = self.converse(double, label)
                    profile = resumed["config"]["permissions"][runtime.PRIVATE_PERMISSIONS]
                    self.assertEqual(profile["filesystem"][":workspace_roots"]["."], "read")
                    self.assertEqual(profile.get("network"), {"enabled": True} if mode == "live" else None)
                    self.assertEqual(NETWORK in overrides(self.spawn.call_args.args[0]), mode == "live")
                    # The profile carries the network: the turn sets no sandbox policy that would replace it.
                    self.assertNotIn("sandboxPolicy", self.turn(double))

    def test_on_other_platforms_the_legacy_sandbox_follows_the_search_mode(self):
        labels = (runtime.CONVERSATION_LABELS["summary"][0], runtime.CONVERSATION_LABELS["message"][0])
        for platform in ("linux", "win32"):
            for mode in ("live", "disabled"):
                with self.subTest(platform=platform, mode=mode), self.mode(mode), \
                        mock.patch.object(runtime.sys, "platform", platform):
                    double = AuthorDouble([goal_test.success()])
                    double.settings = {"sandbox_workspace_write": {"network_access": mode != "live"}}
                    start = self.goal(double)
                    self.assertEqual(start["sandbox"], "workspace-write")
                    self.assertNotIn("permissions", start)
                    self.assertNotIn("sandboxPolicy", self.turn(double))
                    self.assertEqual(overrides(self.spawn.call_args.args[0])["sandbox_workspace_write.network_access"],
                                     "true" if mode == "live" else "false")
                    # The app-server's read-only mode gets its network setting with each conversation turn,
                    # the paused author's replies and its time-limit summary alike.
                    for label in labels:
                        double = AuthorDouble([[goal_test.answer("Open."), goal_test.completed()]])
                        resumed = self.converse(double, label)
                        self.assertEqual(resumed["sandbox"], "read-only")
                        self.assertNotIn("permissions", resumed)
                        self.assertEqual(self.turn(double)["sandboxPolicy"],
                                         {"type": "readOnly", "networkAccess": mode == "live"})

    def test_on_no_platform_is_the_author_told_where_the_users_agents_listen(self):
        for platform in ("darwin", "linux", "win32"):
            for mode in ("live", "disabled"):
                with self.subTest(platform=platform, mode=mode), self.mode(mode), \
                        mock.patch.object(runtime.sys, "platform", platform):
                    double = AuthorDouble([goal_test.success()])
                    double.environment = lambda model: {"PATH": "/usr/bin", **AGENTS}
                    self.goal(double)
                    environment = self.spawn.call_args.kwargs["env"]
                    self.assertFalse(set(AGENTS) & set(environment))
                    self.assertEqual(environment["PATH"], "/usr/bin")
                    double = AuthorDouble([[goal_test.answer("Open."), goal_test.completed()]])
                    double.environment = lambda model: {"PATH": "/usr/bin", **AGENTS}
                    self.converse(double, "Message to the paused author")
                    self.assertFalse(set(AGENTS) & set(self.spawn.call_args.kwargs["env"]))

    def test_a_web_cutoff_author_keeps_its_audited_launch(self):
        policy = {"mode": "historical"}
        with mock.patch.object(runtime, "retrieval_environment", return_value={"PATH": "/usr/bin"}), \
                mock.patch.object(retrieval_runtime, "temp_arguments", return_value=["-c", "audited=true"]), \
                mock.patch.object(retrieval_runtime, "codex_executable", return_value="codex-audited"), \
                mock.patch.object(retrieval_runtime, "native_search_arguments", return_value=[]), \
                mock.patch.object(runtime.sys, "platform", "darwin"):
            argv, _, _ = runtime._author_app_server(
                AuthorDouble([]), directory=str(self.workspace), policy=policy, role="author", model=MODEL,
                settings={"speed": "standard"}, features=(), classic=False)
        settings = overrides(argv)
        self.assertEqual(settings["audited"], "true")
        self.assertNotIn(NETWORK, settings)
        self.assertNotIn("sandbox_workspace_write.network_access", settings)
        self.assertFalse(runtime.live_network(str(self.workspace), policy))


class ClaudeSandboxTests(unittest.TestCase):
    def test_a_live_bash_allows_every_host_and_any_other_none(self):
        self.assertEqual(claude_cli.SANDBOX_ALL_HOSTS, ("*",))
        self.assertEqual(claude_cli.sandbox_settings([], live=True)["network"],
                         {"allowedDomains": ["*"], "strictAllowlist": True})
        self.assertEqual(claude_cli.sandbox_settings([])["network"], {"allowedDomains": [], "strictAllowlist": True})
        # Nothing else about the sandbox changes: no unsandboxed escape, no Unix sockets, no local binding.
        settings = claude_cli.sandbox_settings([], live=True)
        self.assertEqual((settings["enabled"], settings["failIfUnavailable"], settings["allowUnsandboxedCommands"]),
                         (True, True, False))
        self.assertEqual(set(settings["network"]), {"allowedDomains", "strictAllowlist"})


def claude_reply(text="The summary."):
    return ["init", "echo",
            {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": str(uuid.uuid4()),
                      "message": {"content": [{"type": "text", "text": "Working notes."}]}}},
            {"result": {"result": text}}]


class ClaudeRoleTests(claude_test.FakeClaudeCase):
    """Claude's roles against the stand-in CLI: Bash's network follows the search mode, like its web tools."""

    def sandbox(self):
        start = self.records("start")[-1]
        argv = start["argv"]
        return argv[argv.index("--tools") + 1].split(","), (start.get("settings") or {}).get("sandbox")

    def test_a_structured_call_with_a_shell(self):
        for mode, hosts in (("live", ["*"]), ("disabled", [])):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": mode}), \
                    mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
                self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
                runtime.structured("Review.", claude_test.SCHEMA, "review", model="claude-haiku-5-5",
                                   features=("shell_tool",))
                tools, sandbox = self.sandbox()
                self.assertEqual(sandbox["network"], {"allowedDomains": hosts, "strictAllowlist": True})
                self.assertEqual({"WebSearch", "WebFetch"} <= set(tools), mode == "live")

    def test_the_author(self):
        for mode, hosts in (("live", ["*"]), ("disabled", [])):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": mode}), \
                    mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
                self.script([["init", "echo", claude_test.complete()]])
                session = self.goal()
                self.assertEqual(next(session)["outcome"], "done")
                session.close()
                tools, sandbox = self.sandbox()
                self.assertIn("Bash", tools)
                self.assertEqual(sandbox["network"], {"allowedDomains": hosts, "strictAllowlist": True})

    def test_a_conversation_turn(self):
        for mode, hosts in (("live", ["*"]), ("disabled", [])):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": mode}), \
                    mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
                for read_only in (True, False):
                    self.script([claude_reply()])
                    with mock.patch.object(runtime, "CONVERSATION_READ_ONLY", read_only):
                        claude_cli.author_conversation(
                            runtime, "Summarize.", settings={"model": "claude-opus-5-5", "effort": "max",
                                                             "speed": "standard", "summary": "concise"},
                            options={"goal_cwd": str(self.run_dir), "goal_thread_id": str(uuid.uuid4())},
                            features=["multi_agent"], conversation_id="c" * 32,
                            label=runtime.CONVERSATION_LABELS["summary"][0])
                    tools, sandbox = self.sandbox()
                    # A read-only turn has no shell, only the web tools of a live run.
                    self.assertEqual({"WebSearch", "WebFetch"} <= set(tools), mode == "live")
                    if read_only:
                        self.assertIsNone(sandbox)
                    else:
                        self.assertEqual(sandbox["network"], {"allowedDomains": hosts, "strictAllowlist": True})


if __name__ == "__main__":
    unittest.main()
