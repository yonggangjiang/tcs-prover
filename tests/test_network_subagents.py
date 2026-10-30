"""Internet access (tests/test_network.py) together with the subagent limits (tests/test_critic_subagents.py).

Every Codex process of a live run reaches the internet and keeps the subagent limits of its role: the
critic with its independent critics as subagents (the coordinator, which edits proof.md, and its read-only
final verification), the calls without subagents (the statement reviewer, the convention critic, the LaTeX
writer), the author with its Author subagents setting, and the paused author's conversation turns, which
never have subagents. A web-cutoff process keeps its audited profile, with only the audited limits added,
and never asks for the network. Popen and the Codex app-server are stand-ins; no model call is made.
"""

import json
from types import SimpleNamespace
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
import test_goal_runtime as goal_test
import test_network as network

MODEL, NETWORK, overrides = network.MODEL, network.NETWORK, network.overrides
LEGACY_NETWORK = "sandbox_workspace_write.network_access"


def agent_flags(command):
    """The subagent settings of a command line, as -c values in order."""

    return [value for flag, value in zip(command, command[1:])
            if flag == "-c" and value.startswith(("features.multi_agent_v2.", "agents."))]


def limit_flags(count):
    """The flags of subagent_limits(): a session of one thread for none, else Codex's limits and the hint."""

    if not count:
        return ["features.multi_agent_v2.max_concurrent_threads_per_session=1"]
    hint = runtime.SUBAGENT_HINT.format(count=count, s="" if count == 1 else "s")
    return [f"features.multi_agent_v2.max_concurrent_threads_per_session={count + 1}",
            f"features.multi_agent_v2.multi_agent_mode_hint_text={json.dumps(hint)}",
            f"agents.max_concurrent_threads_per_session={count}", "agents.max_depth=1"]


def pairs(command):
    return [command[index:index + 2] for index in range(len(command) - 1)]


class StructuredCallTests(network.Folders):
    def launch(self, platform, *, writable, features, subagents, policy=None):
        seen = []

        def popen(command, **kwargs):
            seen.append(command)
            raise OSError("not started")

        with mock.patch.object(runtime.sys, "platform", platform), \
                mock.patch.object(runtime.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(runtime, "codex", return_value="codex"), self.assertRaises(OSError):
            runtime._run_structured_process(
                "Prompt", {"type": "object"}, "critic", MODEL, "max", "standard", "concise", timeout=60,
                activity_label=None, features=features, policy=policy, child_environment=runtime.environment(MODEL),
                workspace=self.workspace, writable=writable, subagents=subagents)
        return seen[-1]

    def test_the_critic_reaches_the_internet_with_its_subagents_limited(self):
        # The coordinator edits proof.md; its final verification at the round limit only reads it.
        for platform in ("darwin", "linux"):
            for mode in ("live", "disabled"):
                for writable in (True, False):
                    with self.subTest(platform=platform, mode=mode, writable=writable), self.mode(mode):
                        command = self.launch(platform, writable=writable, features=("multi_agent", "shell_tool"),
                                              subagents=3)
                        settings = overrides(command)
                        self.assertEqual(agent_flags(command), limit_flags(3))
                        self.assertIn(["--enable", "multi_agent"], pairs(command))
                        self.assertNotIn(["--disable", "multi_agent"], pairs(command))
                        if platform == "darwin":
                            self.assertEqual(settings.get(NETWORK), '{"enabled"=true}' if mode == "live" else None)
                            self.assertNotIn(LEGACY_NETWORK, settings)
                        elif writable:
                            self.assertIn(["-s", "workspace-write"], pairs(command))
                            self.assertEqual(settings[LEGACY_NETWORK], "true" if mode == "live" else "false")
                        else:
                            # `codex exec`'s legacy read-only mode has no network setting (README).
                            self.assertIn(["-s", "read-only"], pairs(command))
                            self.assertNotIn(LEGACY_NETWORK, settings)

    def test_a_call_without_subagents_reaches_the_internet_in_a_session_of_one_thread(self):
        # The statement reviewer, the convention critic (read-only) and the LaTeX writer (an edit).
        for mode in ("live", "disabled"):
            for writable in (False, True):
                with self.subTest(mode=mode, writable=writable), self.mode(mode):
                    command = self.launch("darwin", writable=writable, features=(), subagents=None)
                    self.assertEqual(agent_flags(command), limit_flags(0))
                    self.assertIn(["--disable", "multi_agent"], pairs(command))
                    self.assertEqual(overrides(command).get(NETWORK), '{"enabled"=true}' if mode == "live" else None)

    def test_a_web_cutoff_critic_keeps_its_audited_profile_with_only_the_audited_limits(self):
        with mock.patch.object(retrieval_runtime, "temp_arguments", return_value=["-c", "audited=true"]), \
                mock.patch.object(retrieval_runtime, "remove_private_temp"), \
                mock.patch.object(retrieval_runtime, "codex_executable", return_value="codex-audited"), \
                mock.patch.object(retrieval_runtime, "native_search_arguments", return_value=[]), \
                mock.patch.object(runtime, "live_network", side_effect=AssertionError("asked for the network")):
            command = self.launch("darwin", writable=False, features=("multi_agent", "shell_tool"), subagents=2,
                                  policy={"mode": "historical"})
        settings = overrides(command)
        self.assertEqual(command[:2], ["codex-audited", "--strict-config"])
        self.assertEqual(settings["audited"], "true")
        self.assertEqual(agent_flags(command), limit_flags(2))
        self.assertFalse({NETWORK, LEGACY_NETWORK} & set(settings))
        self.assertNotIn("-s", command)
        self.assertFalse([value for value in settings if value.startswith(f"permissions.{runtime.PRIVATE_PERMISSIONS}")])


class AuthorTests(network.Folders):
    """The author's app-server and thread: the network of the search mode and the Author subagents setting."""

    def setUp(self):
        super().setUp()
        self.run_tmp = self.root / "runs" / ".controller" / "run" / "tmp"
        self.run_tmp.parent.mkdir(parents=True)
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = popen.start()
        self.addCleanup(popen.stop)

    converse = network.CodexAuthorTests.converse
    turn = staticmethod(network.CodexAuthorTests.turn)

    def goal_with(self, double, count):
        session = runtime.goal_session(
            double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
            settings={**goal_test.SETTINGS, "model": MODEL, "subagents": count},
            options={"goal_cwd": str(self.workspace), "run_tmp_dir": str(self.run_tmp)},
            features=["multi_agent"] if count else [])
        self.addCleanup(session.close)
        self.assertEqual(next(session)["outcome"], "done")
        return next(params for method, params in double.rpc.calls if method == "thread/start")

    def test_the_author_reaches_the_internet_with_its_subagents_limited(self):
        for platform in ("darwin", "linux"):
            for mode in ("live", "disabled"):
                for count in (0, 2):
                    double = network.AuthorDouble([goal_test.success()])
                    with self.subTest(platform=platform, mode=mode, count=count), self.mode(mode), \
                            mock.patch.object(runtime.sys, "platform", platform):
                        start = self.goal_with(double, count)
                        argv = self.spawn.call_args.args[0]
                        settings = overrides(argv)
                        self.assertEqual(agent_flags(argv), limit_flags(count))
                        config = start["config"]
                        self.assertEqual(config["features"]["multi_agent"], bool(count))
                        self.assertEqual(config["features"]["multi_agent_v2"]["max_concurrent_threads_per_session"],
                                         count + 1)
                        self.assertEqual(config.get("agents"),
                                         {"max_concurrent_threads_per_session": count, "max_depth": 1} if count else None)
                        if platform == "darwin":
                            profile = config["permissions"][runtime.PRIVATE_PERMISSIONS]
                            self.assertEqual(profile.get("network"), {"enabled": True} if mode == "live" else None)
                            self.assertEqual(settings.get(NETWORK), '{"enabled"=true}' if mode == "live" else None)
                        else:
                            self.assertEqual(start["sandbox"], "workspace-write")
                            self.assertEqual(settings[LEGACY_NETWORK], "true" if mode == "live" else "false")

    def test_a_conversation_turn_reaches_the_internet_and_never_has_subagents(self):
        label = runtime.CONVERSATION_LABELS["summary"][0]
        for platform in ("darwin", "linux"):
            for mode in ("live", "disabled"):
                double = network.AuthorDouble([[goal_test.answer("Lemma 2 is open."), goal_test.completed()]])
                with self.subTest(platform=platform, mode=mode), self.mode(mode), \
                        mock.patch.object(runtime.sys, "platform", platform):
                    # The author itself had subagents: its conversation turns get none.
                    resumed = self.converse(double, label)
                    argv = self.spawn.call_args.args[0]
                    self.assertEqual(agent_flags(argv), limit_flags(0))
                    self.assertIn(["--disable", "multi_agent"], pairs(argv))
                    self.assertFalse(resumed["config"]["features"]["multi_agent"])
                    self.assertNotIn("agents", resumed["config"])
                    if platform == "darwin":
                        profile = resumed["config"]["permissions"][runtime.PRIVATE_PERMISSIONS]
                        self.assertEqual(profile.get("network"), {"enabled": True} if mode == "live" else None)
                        self.assertNotIn("sandboxPolicy", self.turn(double))
                    else:
                        self.assertEqual(resumed["sandbox"], "read-only")
                        self.assertEqual(self.turn(double)["sandboxPolicy"],
                                         {"type": "readOnly", "networkAccess": mode == "live"})

    def test_a_web_cutoff_author_keeps_its_audited_launch_with_only_the_audited_limits(self):
        policy = {"mode": "historical"}
        environments = []

        def environment(policy, role, model=None, inherited=None, agent_limits=None):
            environments.append(agent_limits)
            return {"PATH": "/usr/bin"}

        with mock.patch.object(runtime, "retrieval_environment", side_effect=environment), \
                mock.patch.object(retrieval_runtime, "temp_arguments", return_value=["-c", "audited=true"]), \
                mock.patch.object(retrieval_runtime, "codex_executable", return_value="codex-audited"), \
                mock.patch.object(retrieval_runtime, "native_search_arguments", return_value=[]), \
                mock.patch.object(runtime.sys, "platform", "darwin"):
            argv, _, _ = runtime._author_app_server(
                network.AuthorDouble([]), directory=str(self.workspace), policy=policy, role="author", model=MODEL,
                settings={"speed": "standard", "subagents": 2}, features=["multi_agent"], classic=False)
        settings = overrides(argv)
        self.assertEqual(settings["audited"], "true")
        self.assertEqual(agent_flags(argv), limit_flags(2))
        # The verified environment (retrieval_runtime.preflight()) checks exactly these limits.
        self.assertEqual(environments, [runtime.subagent_limits(["multi_agent"], 2)])
        self.assertFalse({NETWORK, LEGACY_NETWORK} & set(settings))

