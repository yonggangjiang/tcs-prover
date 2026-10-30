"""Per-run read/write confinement of model processes (runner/confinement.py); no model calls.

The live counterpart, which runs these profiles in `codex sandbox`, is
tests/test_provider_cli_live.py ConfinementLiveTests (TCS_PROVER_LIVE_CLI_TESTS=1).
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, confinement, providers
from ui import server
import test_goal_runtime as goal_test

PROTECTED = {".codex": "read", ".agents": "read", ".git": "read", ".aws": "read", "job-settings.json": "read",
             "pause.json": "read", "checked-statement.md": "read", "prompts": "read", "related-works": "read"}
# Where the user's SSH and GPG agents listen: no confined process gets these.
AGENTS = {"SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.x/Listeners", "SSH_AGENT_PID": "4242",
          "GPG_AGENT_INFO": "/Users/u/.gnupg/S.gpg-agent:0:1"}


# What the startup check's command prints in a sandbox that holds (with single probes changed).
passing_report = goal_test.sandbox_check


class Folders(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.workspace = self.root / "runs" / "run"
        self.workspace.mkdir(parents=True)


class PrivateTempFolderTests(Folders):
    def test_the_run_folder_is_created_owner_only_outside_the_workspace_and_kept(self):
        path = self.root / "runs" / ".controller" / "run" / "tmp"
        path.parent.mkdir(parents=True)
        folder = confinement.private_folder(path, self.workspace)
        self.assertEqual(folder, path)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        (folder / "paper.pdf").write_text("kept")
        folder.chmod(0o755)
        # Every later start keeps its files and makes it owner-only again.
        self.assertEqual(confinement.private_folder(path, self.workspace), path)
        self.assertEqual((path / "paper.pdf").read_text(), "kept")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_a_link_a_file_a_relative_path_and_a_folder_overlapping_the_workspace_are_refused(self):
        target = self.root / "elsewhere"
        target.mkdir()
        link = self.root / "link-tmp"
        link.symlink_to(target)
        plain = self.root / "file-tmp"
        plain.write_text("x")
        for path, reason in ((link, "link"), (plain, "file"), (Path("relative/tmp"), "absolute"),
                             (self.workspace / "tmp", "outside"), (self.workspace, "outside"),
                             (self.root / "runs", "outside")):
            with self.subTest(path=path), self.assertRaisesRegex(confinement.ConfinementError, reason):
                confinement.private_folder(path, self.workspace)
        self.assertEqual(list(target.iterdir()), [])
        # Nothing is created in the workspace before the refusal.
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_a_missing_controller_folder_is_made_again_owner_only(self):
        path = self.root / "runs" / ".controller" / "run" / "tmp"
        self.assertEqual(confinement.private_folder(path, self.workspace), path)
        for folder in (path, path.parent, path.parent.parent):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700, folder)

    def test_another_users_folder_is_refused(self):
        path = self.root / "tmp"
        path.mkdir()
        with mock.patch.object(confinement.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(confinement.ConfinementError, "another user"):
                confinement.private_folder(path, self.workspace)

    def test_fresh_and_runner_folders(self):
        fresh = confinement.fresh_folder("tcs-call-tmp-", self.root, self.workspace)
        self.assertEqual((fresh.parent, stat.S_IMODE(fresh.stat().st_mode)), (self.root, 0o700))
        self.assertRegex(fresh.name, rf"^tcs-call-tmp-{os.getpid()}-\w{{8}}$")  # Swept if this process is killed.
        with self.assertRaisesRegex(confinement.ConfinementError, "outside"):
            confinement.fresh_folder("tcs-call-tmp-", self.workspace, self.workspace)
        self.assertEqual([path.name for path in self.workspace.iterdir()], [])
        with mock.patch.object(confinement, "_RUNNER_FOLDERS", {}), mock.patch.object(confinement.atexit,
                                                                                      "register") as cleanup:
            first = confinement.runner_folder(self.workspace)
            self.addCleanup(shutil.rmtree, first, True)
            self.assertEqual(confinement.runner_folder(self.workspace), first)
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
            self.assertFalse(first.is_relative_to(self.workspace))
            cleanup.assert_called_once_with(shutil.rmtree, first, True)

    def test_the_environment_points_temp_files_and_zsh_heredocs_there_and_drops_global_git_config(self):
        self.assertEqual(confinement.temp_environment(self.root / "t"), {
            "TMPDIR": str(self.root / "t"), "TMPPREFIX": str(self.root / "t" / "zsh"), "GIT_CONFIG_GLOBAL": os.devnull})

    def test_a_confined_environment_does_not_name_the_users_agents(self):
        base = {"PATH": "/usr/bin", "TMPDIR": "/var/folders/x/T/", "HOME": "/Users/u", **AGENTS}
        self.assertEqual(confinement.without_agents(base), {"PATH": "/usr/bin", "TMPDIR": "/var/folders/x/T/",
                                                            "HOME": "/Users/u"})
        self.assertEqual(confinement.confined_environment(base, self.root / "t"), {
            "PATH": "/usr/bin", "HOME": "/Users/u", **confinement.temp_environment(self.root / "t")})
        self.assertEqual(set(base) & set(AGENTS), set(AGENTS))  # The runner's own environment is unchanged.

    @unittest.skipUnless(os.path.isdir("/tmp"), "needs /tmp")
    def test_claude_folders_are_short_and_owner_only(self):
        folder = confinement.claude_temp(self.workspace)
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertTrue(str(folder).startswith(f"/tmp/tcs-prover-{os.getpid()}-"))
        # Claude Code keeps Bash's TMPDIR (<folder>/claude-<uid>) only up to 44 bytes.
        self.assertLessEqual(len(os.path.join(str(folder), f"claude-{os.getuid()}").encode()), 44)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)

    @unittest.skipUnless(os.path.isdir("/tmp"), "needs /tmp")
    def test_folders_of_processes_that_ended_are_swept_and_nothing_else(self):
        temp = self.root / "temp"
        temp.mkdir()
        made = lambda prefix, parent: Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        with mock.patch.object(confinement.tempfile, "tempdir", str(temp)):
            mine = confinement.fresh_folder(confinement.CALL_TEMP_PREFIX)
            claude = confinement.claude_temp()
            self.addCleanup(shutil.rmtree, claude, True)
            dead = [made("tcs-run-tmp-999999-", temp), made("tcs-call-tmp-999999-", temp),
                    made("tcs-prover-999999-", "/tmp")]
            living = [made("tcs-run-tmp-999998-", temp), made("tcs-prover-999998-", "/tmp")]
            other = [made("tcs-prover-notes-", "/tmp"), made("tcs-run-tmp-", temp), made("tcs-proof-review-", temp)]
            for path in (*dead, *living, *other):
                self.addCleanup(shutil.rmtree, path, True)
            with mock.patch.object(confinement.sys, "platform", "darwin"):
                confinement.sweep_temps(lambda pid: pid != 999999)
        self.assertEqual([path for path in dead if path.exists()], [])
        self.assertTrue(all(path.exists() for path in (*living, *other, mine, Path(claude))))
        self.assertTrue(mine.name.startswith(f"tcs-call-tmp-{os.getpid()}-"))

    def test_toolchains_are_only_those_that_exist(self):
        present, home = self.root / "present", self.root / "home"
        present.mkdir()
        (home / "tool").mkdir(parents=True)
        with mock.patch.object(confinement, "TOOLCHAIN_FOLDERS", (str(present), str(self.root / "missing"),
                                                                  "~/tool", "~/missing")), \
                mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(confinement.toolchain_folders(), [present, home / "tool"])
            self.assertEqual(confinement.toolchain_folders(home_only=True), [home / "tool"])


class CodexProfileTests(Folders):
    """The generated profile of every Codex role (macOS)."""

    def profile(self, base, model="gpt-6.1-sol", codex_home=None, live=False):
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            return runtime.private_permissions(base, model, codex_home=codex_home, live=live)

    def test_author_conversation_and_structured_profiles(self):
        home = self.root / "codex-home"
        common = {
            ":minimal": "read", ":slash_tmp": "deny", "/private/var/tmp": "deny", ":tmpdir": "write",
            **{str(path): "read" for path in (*confinement.toolchain_folders(), *confinement.codex_packages())},
            os.path.join(os.path.realpath(home), "tmp", "arg0"): "read",
            os.path.join(os.path.realpath(Path.home() / ".codex"), "tmp", "arg0"): "read",
            **{str(path): "deny" for path in providers.private_paths()}}
        author = self.profile(":workspace", codex_home=home, live=True)
        writable = self.profile(":workspace", codex_home=home)
        read_only = self.profile(":read-only", codex_home=home, live=True)
        offline = self.profile(":read-only", codex_home=home)
        # In a live run every role reaches the internet: the author (or a structured call that edits
        # a file), and a read-only call (statement reviewer, critics, verify visits) or a conversation turn.
        self.assertEqual(author, {"filesystem": {**common, ":workspace_roots": {".": "write", **PROTECTED}},
                                  "network": {"enabled": True}})
        self.assertEqual(read_only, {"filesystem": {**common, ":workspace_roots": {".": "read", **PROTECTED}},
                                     "network": {"enabled": True}})
        # Otherwise (a web cutoff of -1) none does.
        self.assertEqual(writable, {"filesystem": {**common, ":workspace_roots": {".": "write", **PROTECTED}}})
        self.assertEqual(offline, {"filesystem": {**common, ":workspace_roots": {".": "read", **PROTECTED}}})
        # API models: their own home, and the CLI logins denied too.
        api = providers.save_api_model({"format": "openai", "baseUrl": "https://api.example.com/v1",
                                        "model": "m", "apiKey": "sk-test-0123456789abcdef"})["id"]
        profile = self.profile(":read-only", api, codex_home=home)["filesystem"]
        for path in providers.user_login_files():
            self.assertEqual(profile[str(path)], "deny")
        self.assertNotIn(os.path.join(os.path.realpath(Path.home() / ".codex"), "tmp", "arg0"), profile)
        with mock.patch.object(runtime.sys, "platform", "linux"):
            self.assertIsNone(runtime.private_permissions(":workspace", "gpt-6.1-sol"))
            self.assertEqual(runtime.private_permission_arguments(":workspace", "gpt-6.1-sol"), [])

    @unittest.skipIf(tomllib is None, "tomllib is needed to parse Codex overrides")
    def test_flags_define_the_profile_without_extends_and_network_only_when_set(self):
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            plain = runtime.private_permission_arguments(":read-only", "gpt-6.1-sol")
            networked = runtime.private_permission_arguments(":workspace", "gpt-6.1-sol", profile=self.profile(
                ":workspace", live=True))
        name = runtime.PRIVATE_PERMISSIONS
        parsed = lambda flags: {value.split("=", 1)[0]: tomllib.loads("v = " + value.split("=", 1)[1])["v"]
                                for flag, value in zip(flags, flags[1:]) if flag == "-c"}
        self.assertEqual(set(parsed(plain)), {"default_permissions", f"permissions.{name}.filesystem"})
        self.assertEqual(parsed(plain)[f"permissions.{name}.filesystem"], self.profile(":read-only")["filesystem"])
        self.assertEqual(parsed(networked)[f"permissions.{name}.network"], {"enabled": True})

    def test_a_structured_call_runs_confined_with_a_fresh_private_temp_folder_removed_afterwards(self):
        seen = []

        def popen(command, **kwargs):
            environment = kwargs["env"]
            seen.append((command, environment, Path(environment.get("TMPDIR", "")).is_dir()))
            raise OSError("not started")

        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        for platform in ("darwin", "linux"):
            for writable in (False, True):
                with self.subTest(platform=platform, writable=writable), \
                        mock.patch.object(runtime.sys, "platform", platform), \
                        mock.patch.object(runtime.subprocess, "Popen", side_effect=popen), \
                        mock.patch.object(runtime, "codex", return_value="codex"), \
                        mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live"}), \
                        self.assertRaises(OSError):
                    os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
                    with mock.patch.dict(os.environ, AGENTS):
                        runtime._run_structured_process(
                            "Prompt", {"type": "object"}, "critic", "gpt-6.1-sol", "max", "standard", "concise",
                            timeout=60, activity_label=None, features=(), policy=None,
                            child_environment=runtime.environment("gpt-6.1-sol"), workspace=snapshot,
                            writable=writable)
                command, environment, existed = seen[-1]
                # On every platform: with network access a command could use the agents.
                self.assertFalse(set(AGENTS) & set(environment))
                if platform == "linux":
                    self.assertIn(("-s", "workspace-write" if writable else "read-only"), list(zip(command, command[1:])))
                    self.assertEqual((environment.get("TMPDIR"), environment.get("TMPPREFIX")),
                                     (os.environ.get("TMPDIR"), os.environ.get("TMPPREFIX")))
                    continue
                self.assertNotIn("-s", command)
                self.assertIn(f'default_permissions="{runtime.PRIVATE_PERMISSIONS}"', command)
                profile = next(value for value in command if value.startswith(
                    f"permissions.{runtime.PRIVATE_PERMISSIONS}.filesystem="))
                self.assertIn('"."="write"' if writable else '"."="read"', profile)
                temp = Path(environment["TMPDIR"])
                self.assertTrue(existed)
                self.assertFalse(temp.exists())  # Removed when the call ended.
                self.assertFalse(temp.is_relative_to(snapshot))
                self.assertEqual((environment["TMPPREFIX"], environment["GIT_CONFIG_GLOBAL"]),
                                 (str(temp / "zsh"), os.devnull))
                self.assertIn("--ignore-user-config", command)
                self.assertIn("features.view_image=false", command)


class AuthorSandboxTests(Folders):
    class Double(goal_test.Runtime):
        Error = runtime.Error  # Failed attempts are retried as with the real runner.
        # The runner's environment names the user's agents.
        environment = staticmethod(lambda model: {"PATH": "/usr/bin", **AGENTS})
        private_permissions = staticmethod(runtime.private_permissions)
        author_confinement_check = staticmethod(runtime.author_confinement_check)
        check_writable = True
        command_exec = None
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
                    if self.command_exec is not None:
                        return self.command_exec(params)
                    return passing_report(params, self.check_writable)
                return call(method, params)
            rpc.call = answer
            return rpc

    def setUp(self):
        super().setUp()
        self.run_tmp = self.root / "runs" / ".controller" / "run" / "tmp"
        self.run_tmp.parent.mkdir(parents=True)
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = popen.start()
        self.addCleanup(popen.stop)
        platform = mock.patch.object(runtime.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)

    def session(self, double, **options):
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings={**goal_test.SETTINGS, "model": "gpt-6.1-sol"},
                                       options={"goal_cwd": str(self.workspace), "run_tmp_dir": str(self.run_tmp),
                                                **options})
        self.addCleanup(session.close)
        return session

    def methods(self, double):
        return [method for method, _ in double.rpc.calls]

    def test_the_check_runs_in_the_confined_profile_before_the_first_turn(self):
        double = self.Double([goal_test.success()])
        double.settings = {"mcp_servers": {"node_repl": {"command": "node_repl"}}, "plugins": {"slack@x": {}}}
        self.assertEqual(next(self.session(double))["outcome"], "done")
        methods = self.methods(double)
        self.assertLess(methods.index("command/exec"), methods.index("thread/start"))
        check = next(params for method, params in double.rpc.calls if method == "command/exec")
        self.assertEqual((check["permissionProfile"], check["cwd"]), (runtime.PRIVATE_PERMISSIONS,
                                                                      str(self.workspace)))
        self.assertEqual(check["command"][:2], ["/bin/sh", "-c"])
        # The profile the check names is the one the app-server defines.
        argv = self.spawn.call_args.args[0]
        self.assertIn(f'default_permissions="{runtime.PRIVATE_PERMISSIONS}"', argv)
        self.assertEqual(self.spawn.call_args.kwargs["env"]["TMPDIR"], str(self.run_tmp))
        # With network access the author could use the user's SSH or GPG agent: it is not told where they are.
        self.assertFalse(set(AGENTS) & set(self.spawn.call_args.kwargs["env"]))
        # Commands get the temp folder's environment whatever the user's shell_environment_policy passes on.
        for name, value in confinement.temp_environment(self.run_tmp).items():
            self.assertIn(f"shell_environment_policy.set.{name}={json.dumps(value)}", argv)
        start = next(params for method, params in double.rpc.calls if method == "thread/start")
        self.assertEqual((start["config"]["mcp_servers"], start["config"]["plugins"]),
                         ({"node_repl": {"enabled": False}}, {"slack@x": {"enabled": False}}))
        self.assertIn("Sandbox checked", [event.get("label") for event in double.events])
        # The check leaves nothing behind.
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.run_tmp.parent.iterdir()), ["tmp"])
        self.assertEqual(list(self.run_tmp.iterdir()), [])

    def test_a_check_that_fails_or_cannot_run_never_starts_the_author_and_is_retried(self):
        broken = runtime.RPCError("command/exec failed: invalid permission profile")
        for name, behaviour in (
                ("outside readable", lambda params: passing_report(params, outsideRead=True)),
                ("temp blocked", lambda params: passing_report(params, tempWrite=False)),
                ("no report", lambda params: {"exitCode": 1, "stdout": "", "stderr": "sandbox-exec: failed"}),
                ("refused", mock.Mock(side_effect=broken))):
            with self.subTest(name):
                double = self.Double([goal_test.success()])
                double.command_exec = behaviour
                with self.assertRaises(runtime.WorkflowPaused) as paused:
                    next(self.session(double))
                self.assertIn("The sandbox check before the author's turn", paused.exception.reason)
                self.assertIn("not started without its sandbox", paused.exception.reason)
                self.assertIsNotNone(paused.exception.retry)  # Retried every minute.
                self.assertFalse(paused.exception.safety_stop)
                self.assertFalse({"thread/start", "thread/resume", "turn/start"} & set(self.methods(double)))
                self.assertEqual(list(self.workspace.iterdir()), [])
        double = self.Double([goal_test.success()])
        double.command_exec = lambda params: passing_report(params, outsideRead=True)
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.session(double))
        self.assertIn("the sandbox did not hold: the check command could read the controller's folder",
                      paused.exception.reason)

    def test_a_private_temp_folder_that_cannot_be_used_is_retried_and_never_replaced(self):
        target = self.root / "elsewhere"
        target.mkdir()
        self.run_tmp.symlink_to(target)
        double = self.Double([goal_test.success()])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.session(double))
        self.assertIn("Cannot prepare the run's private temp folder", paused.exception.reason)
        self.assertIsNotNone(paused.exception.retry)
        self.spawn.assert_not_called()

    def test_without_the_controllers_folder_a_runner_uses_a_fresh_one(self):
        double = self.Double([goal_test.success()])
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings={**goal_test.SETTINGS, "model": "gpt-6.1-sol"},
                                       options={"goal_cwd": str(self.workspace)})
        self.addCleanup(session.close)
        self.assertEqual(next(session)["outcome"], "done")
        temp = Path(self.spawn.call_args.kwargs["env"]["TMPDIR"])
        self.assertEqual(temp, confinement.runner_folder(self.workspace))
        self.assertFalse(temp.is_relative_to(self.workspace))

    def test_a_conversation_turn_checks_the_read_only_profile(self):
        double = self.Double([[goal_test.answer("Lemma 2 is open."), goal_test.completed()]])
        double.check_writable = False
        result = runtime.author_conversation(
            double, "Summarize.", settings={**goal_test.SETTINGS, "model": "gpt-6.1-sol"},
            options={"goal_cwd": str(self.workspace), "goal_thread_id": "thread-1", "run_tmp_dir": str(self.run_tmp)},
            features=["multi_agent"], conversation_id="c" * 32, label="Progress summary requested")
        self.assertEqual(result["reply"], "Lemma 2 is open.")
        self.assertFalse(set(AGENTS) & set(self.spawn.call_args.kwargs["env"]))
        methods = self.methods(double)
        self.assertLess(methods.index("command/exec"), methods.index("thread/resume"))
        resumed = next(params for method, params in double.rpc.calls if method == "thread/resume")
        profile = resumed["config"]["permissions"][runtime.PRIVATE_PERMISSIONS]
        self.assertEqual(profile["filesystem"][":workspace_roots"]["."], "read")
        # A live run's turn (this test's search mode) reaches the internet too.
        self.assertEqual(profile["network"], {"enabled": True})
        self.assertFalse(resumed["config"]["features"]["view_image"])
        # A check that proves the workspace writable fails the read-only turn: retried, never started.
        double = self.Double([[goal_test.answer("Lemma 2 is open."), goal_test.completed()]])
        double.check_writable = True
        with self.assertRaises(runtime.ConversationAttemptFailed) as failed:
            runtime.author_conversation(
                double, "Summarize.", settings={**goal_test.SETTINGS, "model": "gpt-6.1-sol"},
                options={"goal_cwd": str(self.workspace), "goal_thread_id": "thread-1",
                         "run_tmp_dir": str(self.run_tmp)},
                features=["multi_agent"], conversation_id="c" * 32, label="Progress summary requested")
        self.assertIn("could write the run's folder", str(failed.exception))
        self.assertNotIn("thread/resume", self.methods(double))


class StartupCheckTests(Folders):
    def setUp(self):
        super().setUp()
        self.temp = confinement.fresh_folder("tcs-check-", self.root, self.workspace)

    def check(self, execute, writable=True):
        return confinement.startup_check(execute, workspace=self.workspace, temp=self.temp, writable=writable,
                                         profile="profile")

    def test_a_sandbox_that_holds_passes_and_every_probe_is_named(self):
        requests = []

        def execute(params):
            requests.append(params)
            # The controller's canaries exist while the command runs.
            names = sorted(path.name for path in (*self.workspace.iterdir(), *self.temp.parent.iterdir()))
            self.assertTrue(any(name.startswith(".tcs-confinement-") for name in names))
            return passing_report(params)

        report = self.check(execute)
        self.assertEqual(set(report) - {"nonce"}, {"workspaceRead", "workspaceWrite", "tempWrite", "outsideRead",
                                                   "checkoutRead", "slashTmpWrite", "parentList"})
        [params] = requests
        self.assertEqual((params["cwd"], params["permissionProfile"], params["timeoutMs"]),
                         (str(self.workspace), "profile", confinement.CHECK_TIMEOUT_MS))
        script = params["command"][-1]
        for part in (str(self.workspace), str(self.temp), "/private/tmp/", str(Path(confinement.__file__).resolve()),
                     f"/bin/ls {self.workspace.parent}"):
            self.assertIn(part, script)
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), sorted(["runs", self.temp.name]))

    def test_violations_blocked_work_and_missing_reports_fail(self):
        def changed(key, value):
            return lambda params: passing_report(params, **{key: value})

        for key in ("outsideRead", "checkoutRead", "slashTmpWrite", "parentList"):
            with self.subTest(key=key), self.assertRaisesRegex(confinement.ConfinementError, "did not hold"):
                self.check(changed(key, True))
        with self.assertRaisesRegex(confinement.ConfinementError, "write the run's folder"):
            self.check(changed("workspaceWrite", True), writable=False)
        for key in ("workspaceRead", "tempWrite", "workspaceWrite"):
            with self.subTest(key=key), self.assertRaisesRegex(confinement.ConfinementError, "blocked allowed work"):
                self.check(changed(key, False))
        for result in ({"stdout": "", "stderr": "Operation not permitted"}, None, {"stdout": "{}"},
                       {"stdout": json.dumps({"nonce": "other"})}):
            with self.subTest(result=result), self.assertRaisesRegex(confinement.ConfinementError, "no valid report"):
                self.check(lambda params: result)
        self.assertEqual(list(self.workspace.iterdir()), [])

    @unittest.skipUnless(os.path.exists("/bin/sh"), "needs /bin/sh")
    def test_without_a_sandbox_the_command_itself_reports_the_violations(self):
        # Run unsandboxed, the real command can read outside: the check must say so.
        def execute(params):
            result = subprocess.run(params["command"], cwd=params["cwd"], capture_output=True, text=True, timeout=30)
            return {"exitCode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

        with self.assertRaisesRegex(confinement.ConfinementError, "read the controller's folder") as caught:
            self.check(execute)
        self.assertIn("read the TCS Prover checkout", str(caught.exception))
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(list(self.temp.iterdir()), [])
        self.assertFalse(list(Path("/private/tmp").glob(".tcs-confinement-*")))


class ExtensionTests(unittest.TestCase):
    def test_every_mcp_server_plugin_and_app_is_turned_off_by_name(self):
        config = {"mcp_servers": {"node_repl": {"command": "x", "enabled": True}, "off": {"enabled": False}},
                  "plugins": {"slack@openai-curated": {"enabled": True}}, "apps": {"_default": {}, "calendar": {}}}
        self.assertEqual(runtime._user_extensions_off(config), {
            "mcp_servers": {"node_repl": {"enabled": False}, "off": {"enabled": False}},
            "plugins": {"slack@openai-curated": {"enabled": False}},
            "apps": {"_default": {"enabled": False}, "calendar": {"enabled": False}}})
        self.assertEqual(runtime._user_extensions_off({"mcp_servers": None}), {"apps": {"_default": {"enabled": False}}})


class UnsupportedPlatformTests(Folders):
    def test_each_runner_says_once_that_confinement_is_macos_only(self):
        for platform, expected in (("linux", 1), ("win32", 1), ("darwin", 0)):
            events = []
            with self.subTest(platform=platform), mock.patch.object(runtime.sys, "platform", platform), \
                    mock.patch.object(runtime, "emit", side_effect=lambda *args, **fields: events.append(fields)), \
                    mock.patch.object(runtime.subprocess, "run", side_effect=OSError("no CLI")), \
                    mock.patch.object(runtime, "codex", return_value="codex"), \
                    mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live"}):
                os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
                runtime.check_clis(["gpt-6.1-sol", "gpt-6.1-sol"], directory=self.workspace)
            notices = [event for event in events if event.get("text") == confinement.UNSUPPORTED_NOTICE]
            self.assertEqual(len(notices), expected)
        self.assertEqual(confinement.UNSUPPORTED_NOTICE, "Run confinement is enforced on macOS only; this run's "
                                                         "model can read files outside its folder.")


class ClaudeBashTests(Folders):
    def test_bash_reads_only_its_folder_its_temp_folder_and_the_system(self):
        temp = self.root / "t"
        temp.mkdir()
        hidden = [Path("/state/a"), Path("/state/b")]
        with mock.patch.object(confinement, "claude_versions", return_value=[Path("/versions")]), \
                mock.patch.object(confinement, "toolchain_folders", return_value=[Path("/home/u/.elan")]):
            settings = claude_cli.sandbox_settings(hidden, [self.workspace / "related-works"],
                                                   workspace=self.workspace, temp=temp)
        # The checkout and the runs folder too, wherever they are outside the home folder.
        outside = [path for path in (os.path.realpath(providers.ROOT), str(self.workspace.parent))
                   if not Path(path).is_relative_to(os.path.realpath(Path.home()))]
        self.assertEqual(settings["filesystem"], {
            "denyRead": [os.path.realpath(Path.home()), "/Volumes", "/private/tmp", "/private/var/tmp",
                         "/private/var/folders", *outside, "/state/a", "/state/b"],
            "allowRead": [str(self.workspace), str(temp), "/versions", "/home/u/.elan"],
            "allowWrite": [str(temp)],
            "denyWrite": ["/state/a", "/state/b", str(self.workspace / "related-works"), "/tmp/claude",
                          "/private/tmp/claude"]})
        self.assertEqual((settings["enabled"], settings["failIfUnavailable"], settings["allowUnsandboxedCommands"],
                          settings["network"]), (True, True, False, {"allowedDomains": [], "strictAllowlist": True}))
        # Without a private temp folder (other platforms) Bash keeps the plain deny lists.
        self.assertEqual(claude_cli.sandbox_settings(hidden)["filesystem"],
                         {"denyRead": ["/state/a", "/state/b"], "denyWrite": ["/state/a", "/state/b"]})

    def test_a_checkout_and_runs_outside_the_home_folder_are_denied_too(self):
        temp = self.root / "t"
        temp.mkdir()
        home, checkout = self.root / "home", self.root / "checkout"
        for path, inside in ((home / "runs" / "run", False), (self.root / "runs" / "run", True)):
            with self.subTest(inside_home=not inside), mock.patch.dict(os.environ, {"HOME": str(home)}), \
                    mock.patch.object(providers, "ROOT", checkout):
                denied = claude_cli.sandbox_settings([], workspace=path, temp=temp)["filesystem"]["denyRead"]
            self.assertEqual(denied[5:], [str(checkout), str(path.parent)] if inside else [str(checkout)])
        # A workspace directly under / never denies the whole disk.
        with mock.patch.object(providers, "ROOT", checkout):
            denied = claude_cli.sandbox_settings([], workspace=Path("/run-at-root"), temp=temp)["filesystem"]["denyRead"]
        self.assertNotIn("/", denied)

    def test_the_environment_names_the_temp_folder_and_drops_global_git_config_and_the_agents(self):
        environment = claude_cli.child_environment({"CLAUDE_CODE_TMPDIR": "/inherited", **AGENTS}, "max", api=False,
                                                   temp="/tmp/tcs-prover-1-abcdefgh")
        self.assertEqual((environment["CLAUDE_CODE_TMPDIR"], environment["GIT_CONFIG_GLOBAL"]),
                         ("/tmp/tcs-prover-1-abcdefgh", os.devnull))
        self.assertFalse(set(AGENTS) & set(environment))
        plain = claude_cli.child_environment({"CLAUDE_CODE_TMPDIR": "/inherited", **AGENTS}, "max", api=False)
        self.assertNotIn("CLAUDE_CODE_TMPDIR", plain)
        self.assertNotIn("GIT_CONFIG_GLOBAL", plain)
        # Nor any other Claude CLI (roles without Bash, other platforms).
        self.assertFalse(set(AGENTS) & set(plain))

    def test_the_run_folders_protected_entries_are_read_only_for_the_file_tools_and_bash(self):
        settings, paths = claude_cli.protected_settings(self.workspace)
        # The same entries the Codex profile keeps read-only.
        self.assertEqual([path.name for path in paths], list(PROTECTED))
        self.assertEqual(paths, [self.workspace / name for name in PROTECTED])
        # Named by the run folder's real path (//<path>/<name>): entries of these names deeper in it stay writable.
        run = f"/{self.workspace}"
        self.assertEqual(settings, {"permissions": {"deny": [
            f"Edit({run}/.codex)", f"Edit({run}/.codex/**)", f"Edit({run}/.agents)", f"Edit({run}/.agents/**)",
            f"Edit({run}/.git)", f"Edit({run}/.git/**)", f"Edit({run}/.aws)", f"Edit({run}/.aws/**)",
            f"Edit({run}/job-settings.json)", f"Edit({run}/pause.json)", f"Edit({run}/checked-statement.md)",
            f"Edit({run}/prompts)", f"Edit({run}/prompts/**)", f"Edit({run}/related-works)",
            f"Edit({run}/related-works/**)"]}})
        # Through a link too: the rules name the real path, the Bash sandbox the folder as given.
        link = self.root / "linked-run"
        link.symlink_to(self.workspace)
        linked, linked_paths = claude_cli.protected_settings(link)
        self.assertEqual(linked, settings)
        self.assertEqual(linked_paths, [link / name for name in PROTECTED])
        # A real path that a rule cannot spell as it is keeps the relative rules, which also match deeper.
        for name in ("run (copy)", "run[1]", "run*", "réunion", "run+1"):
            with self.subTest(name=name):
                odd = self.root / "runs" / name
                odd.mkdir()
                self.assertEqual(claude_cli.protected_settings(odd)[0], {"permissions": {"deny": [
                    "Edit(./.codex)", "Edit(./.codex/**)", "Edit(./.agents)", "Edit(./.agents/**)", "Edit(./.git)",
                    "Edit(./.git/**)", "Edit(./.aws)", "Edit(./.aws/**)", "Edit(./job-settings.json)",
                    "Edit(./pause.json)", "Edit(./checked-statement.md)", "Edit(./prompts)", "Edit(./prompts/**)",
                    "Edit(./related-works)", "Edit(./related-works/**)"]}})
        spaced = self.root / "runs" / "my run~2.v@x"
        spaced.mkdir()
        self.assertEqual(claude_cli.protected_settings(spaced)[0]["permissions"]["deny"][0], f"Edit(/{spaced}/.codex)")
        temp = self.root / "t"
        temp.mkdir()
        sandbox = claude_cli.sandbox_settings([], paths, workspace=self.workspace, temp=temp)["filesystem"]
        self.assertEqual(sandbox["denyWrite"], [*(str(path) for path in paths), *confinement.CLAUDE_DENIED_WRITES])
        # The workspace itself stays readable and writable: the proof, notes and new folders.
        self.assertIn(str(self.workspace), sandbox["allowRead"])
        self.assertNotIn(str(self.workspace), sandbox["denyWrite"])
        _, allowed = claude_cli.tool_permissions("author", (), live_search=False, bash=True, bare=False)
        self.assertIn("Edit(./**)", allowed)

    def test_a_temp_folder_only_for_bash_on_macos_removed_with_the_settings(self):
        runtime_module = SimpleNamespace(Error=runtime.Error)
        with mock.patch.object(confinement.sys, "platform", "darwin"):
            self.assertIsNone(claude_cli.bash_temp(["Read", "Edit"], self.workspace, runtime_module))
            temp = claude_cli.bash_temp(["Read", "Bash"], self.workspace, runtime_module)
        with mock.patch.object(confinement.sys, "platform", "linux"):
            self.assertIsNone(claude_cli.bash_temp(["Bash"], self.workspace, runtime_module))
        self.assertTrue(os.path.isdir(temp))
        with mock.patch.object(claude_cli, "settings_parent", return_value=self.root):
            settings = claude_cli.PrivateSettings(lambda folder: {"sandbox": {}}, temp=temp)
            settings.close()
        self.assertFalse(os.path.exists(temp))
        with mock.patch.object(confinement, "claude_temp", side_effect=OSError("disk full")), \
                mock.patch.object(confinement.sys, "platform", "darwin"):
            with self.assertRaisesRegex(runtime.Error, "private temp folder of Claude's Bash"):
                claude_cli.bash_temp(["Bash"], self.workspace, runtime_module)


class RunTempServerTests(unittest.TestCase):
    """The server names the author's private temp folder at every launch, resume, continuation and reply."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact statement")
        self.app._save("checked-statement.md", "# Checked statement\n\nExact statement\n")
        self.expected = "run_tmp_dir=" + json.dumps(str(server.controller_directory(self.app.run_dir) / "tmp"))

    def test_launches_continuations_and_replies_pass_the_controllers_tmp_folder(self):
        launched = []
        with mock.patch.object(self.app, "_launch_workflow_locked",
                               side_effect=lambda *args, **kwargs: launched.append(args) or (None, None)):
            self.app._launch_solver_locked("Exact statement")
        self.assertIn(self.expected, launched[0][2])
        # A continuation works in the source run's folder; its temp folder stays its own, outside it.
        source = self.runs / "source-run"
        source.mkdir()
        self.app.state.update(goalWorkspace=str(source.resolve()), goalResume=True, goalThreadId="thread-1")
        options = self.app._proof_options_locked()
        self.assertIn(self.expected, options)
        self.assertIn("goal_cwd=" + json.dumps(str(source.resolve())), options)
        self.app.state.update(phase="paused")
        self.assertIn("run_tmp_dir=" + json.dumps(str(server.controller_directory(self.app.run_dir) / "tmp")),
                      self.app._conversation_options_locked())
        # The folder itself is the runner's to create.
        self.assertFalse((server.controller_directory(self.app.run_dir) / "tmp").exists())


if __name__ == "__main__":
    unittest.main()
