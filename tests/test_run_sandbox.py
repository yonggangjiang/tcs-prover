"""The macOS run-folder sandbox: profile contents and where it is applied; no model calls."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import workflow_runner as module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_goal_runtime import PROMPT, PROMPTS, SETTINGS, Runtime, success  # noqa: E402
from ui import audits  # noqa: E402

try:
    import tomllib
except ImportError:  # Python 3.9
    tomllib = None


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class SandboxProfileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(os.path.realpath(folder.name))
        self.home = base / "home"
        self.run = self.home / "repo" / "runs" / "2026-10-04_run"
        self.run.mkdir(parents=True)
        (self.run.parent / "other-run").mkdir()
        self.conda = executable(self.home / "opt" / "conda" / "bin" / "python3")
        self.release = executable(self.home / ".codex" / "releases" / "1.0" / "bin" / "codex")
        self.search_path = os.pathsep.join([str(self.conda.parent), "/usr/bin", "/bin"])
        for change in (patch.object(module.Path, "home", return_value=self.home),
                       patch.object(module, "codex", return_value=str(self.release)),
                       patch.object(module, "ROOT", self.home / "repo"),
                       patch.dict(os.environ, {module.SANDBOX_READ_ENV: ""})):
            change.start()
            self.addCleanup(change.stop)

    def filesystem(self, **kwargs):
        return module.run_sandbox_permissions(self.run, search_path=self.search_path, **kwargs)

    def test_only_the_run_folder_is_writable_and_nothing_above_it_is_readable(self):
        profile = self.filesystem()
        filesystem = profile["filesystem"]
        self.assertEqual(filesystem[":project_roots"], "write")
        self.assertEqual(filesystem[":minimal"], "read")
        self.assertEqual([path for path, access in filesystem.items() if access == "write"], [":project_roots"])
        for root in ("/usr", "/bin", "/System"):
            self.assertEqual(filesystem[root], "read")
        # Toolchains in the home folder and the Codex release stay usable.
        self.assertEqual(filesystem[str(self.conda.parent.parent)], "read")
        self.assertEqual(filesystem[str(self.release.parent.parent)], "read")
        readable = [Path(path) for path, access in filesystem.items() if access == "read" and path.startswith("/")]
        for protected in (self.home, self.home / "repo", self.run.parent, self.run):
            self.assertFalse(any(protected == root or root in protected.parents for root in readable), protected)
        # Shared scratch folders are closed so parallel runs cannot meet there.
        self.assertEqual(filesystem["/private/tmp*"], "deny")
        self.assertEqual(filesystem["/private/tmp/**"], "deny")
        self.assertTrue(profile["network"]["enabled"])

    def test_extra_read_folders_cannot_reopen_the_home_folder_or_the_runs(self):
        extra = os.pathsep.join([str(self.home), str(self.home / "repo"), str(self.run.parent),
                                 str(self.home / "opt"), "relative/path"])
        with patch.dict(os.environ, {module.SANDBOX_READ_ENV: extra}):
            filesystem = self.filesystem()["filesystem"]
        self.assertEqual(filesystem[str(self.home / "opt")], "read")
        for path in (self.home, self.home / "repo", self.run.parent):
            self.assertNotIn(str(path), filesystem)

    def test_read_only_calls_cannot_write_or_reach_the_network(self):
        profile = self.filesystem(writable=False)
        self.assertEqual(profile["filesystem"][":project_roots"], "read")
        self.assertNotIn("write", profile["filesystem"].values())
        self.assertFalse(profile["network"]["enabled"])

    def test_a_run_folder_inside_tmp_keeps_its_own_folder_readable(self):
        with patch.object(module, "_inside", wraps=module._inside):
            filesystem = module.run_sandbox_permissions("/private/tmp/runs/x", search_path="")["filesystem"]
        self.assertNotIn("/private/tmp*", filesystem)
        self.assertIn("/private/var/tmp*", filesystem)

    def test_arguments_are_valid_toml_and_send_scratch_files_to_the_run_folder(self):
        arguments = module.run_sandbox_arguments(self.run, search_path=self.search_path)
        values = arguments[1::2]
        self.assertEqual(arguments[0::2], ["-c"] * len(values))
        self.assertEqual(values[0], f'default_permissions="{module.RUN_SANDBOX_PROFILE}"')
        scratch = self.run / module.SANDBOX_SCRATCH_DIRNAME
        self.assertTrue(scratch.is_dir())
        self.assertIn(f'"TMPDIR" = "{scratch}/"', values[-1])
        self.assertIn(f'"TMPPREFIX" = "{scratch}/zsh"', values[-1])
        if tomllib:
            for value in values:
                key, _, text = value.partition("=")
                parsed = tomllib.loads(f"value = {text}")["value"]
                if key.endswith(".filesystem"):
                    self.assertEqual(parsed, self.filesystem()["filesystem"])
        read_only = module.run_sandbox_arguments(self.run, writable=False, search_path=self.search_path)
        self.assertFalse(any(value.startswith("shell_environment_policy") for value in read_only[1::2]))

    def test_enabled_only_on_macos_unless_turned_off(self):
        with patch.object(module.sys, "platform", "darwin"), patch.dict(os.environ, {module.SANDBOX_ENV: ""}):
            os.environ.pop(module.SANDBOX_ENV)
            self.assertTrue(module.run_sandbox_enabled())
        with patch.object(module.sys, "platform", "darwin"), patch.dict(os.environ, {module.SANDBOX_ENV: "0"}):
            self.assertFalse(module.run_sandbox_enabled())
        with patch.object(module.sys, "platform", "linux"), patch.dict(os.environ, {module.SANDBOX_ENV: "1"}):
            self.assertFalse(module.run_sandbox_enabled())

    def test_login_path_falls_back_to_the_inherited_path(self):
        module.login_shell_path.cache_clear()
        self.addCleanup(module.login_shell_path.cache_clear)
        with patch.object(module.subprocess, "run", side_effect=OSError("no shell")):
            self.assertEqual(module.login_shell_path("/usr/bin:/bin"), "/usr/bin:/bin")


class SandboxedRuntime(Runtime):
    """The goal-session double, with the sandbox switched on."""

    run_sandbox_enabled = staticmethod(lambda: True)
    RUN_SANDBOX_PROFILE = module.RUN_SANDBOX_PROFILE
    environment = staticmethod(lambda model: {"PATH": "/inherited"})
    login_shell_path = staticmethod(lambda path: f"/login:{path}")

    def run_sandbox_arguments(self, directory, writable=True, search_path=None):
        self.sandbox_call = (directory, writable, search_path)
        return ["-c", "default_permissions=\"tcs_prover_run\""]


class SandboxApplicationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()

    def test_the_author_thread_uses_the_named_profile_and_the_login_path(self):
        runtime = SandboxedRuntime([success()])
        with patch.object(module.subprocess, "Popen", return_value=object()) as popen:
            session = module.goal_session(runtime, PROMPT, prompts=PROMPTS, settings=SETTINGS,
                                          options={"goal_cwd": str(self.directory)})
            self.addCleanup(session.close)
            self.assertEqual(next(session)["output"], "Full proof")
        start = next(params for method, params in runtime.rpc.calls if method == "thread/start")
        self.assertEqual(start["permissions"], module.RUN_SANDBOX_PROFILE)
        self.assertNotIn("sandbox", start)
        self.assertEqual(start["runtimeWorkspaceRoots"], [str(self.directory)])
        argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertIn('default_permissions="tcs_prover_run"', argv)
        self.assertEqual(kwargs["env"]["PATH"], "/login:/inherited")
        self.assertEqual((Path(runtime.sandbox_call[0]), *runtime.sandbox_call[1:]), (self.directory, True, "/login:/inherited"))

    def test_without_the_sandbox_the_author_keeps_workspace_write(self):
        runtime = Runtime([success()])
        with patch.object(module.subprocess, "Popen", return_value=object()):
            session = module.goal_session(runtime, PROMPT, prompts=PROMPTS, settings=SETTINGS,
                                          options={"goal_cwd": str(self.directory)})
            self.addCleanup(session.close)
            next(session)
        start = next(params for method, params in runtime.rpc.calls if method == "thread/start")
        self.assertEqual(start["sandbox"], "workspace-write")
        self.assertNotIn("permissions", start)

    def capture_structured_command(self):
        seen = []

        def refuse(command, **kwargs):
            seen.append(command)
            raise OSError("captured")

        with patch.object(module, "run_sandbox_enabled", return_value=True), \
                patch.object(module, "codex", return_value="/usr/bin/true"), \
                patch.object(module.subprocess, "Popen", side_effect=refuse):
            with self.assertRaises(OSError):
                module.run_structured_attempt("Review.", {"type": "object", "properties": {}}, "critic",
                                              "gpt-6-astra", "max", "standard", "concise")
        return seen[0]

    def test_critic_writer_and_reviewer_calls_are_read_only_in_their_empty_workspace(self):
        command = self.capture_structured_command()
        self.assertNotIn("-s", command)
        self.assertIn(f'default_permissions="{module.RUN_SANDBOX_PROFILE}"', command)
        filesystem = next(value for value in command if value.startswith(f"permissions.{module.RUN_SANDBOX_PROFILE}.filesystem="))
        self.assertIn('":project_roots" = "read"', filesystem)
        self.assertNotIn('"write"', filesystem)
        self.assertEqual(command[command.index("-C") + 1], str(module.structured_workspace()))

    def test_codex_auditors_are_read_only_in_their_workspace(self):
        seen = []

        def refuse(command, **kwargs):
            seen.append(command)
            raise OSError("captured")

        model = {"provider": "codex", "model": "gpt-6-astra", "effort": "max"}
        with patch.object(module, "run_sandbox_enabled", return_value=True), \
                patch.object(audits, "resolve_executable", return_value="/usr/bin/true"), \
                patch.object(audits.subprocess, "Popen", side_effect=refuse):
            try:
                audits.run_auditor(model, "Audit.", self.directory, threading.Event())
            except OSError:
                pass
        command = seen[0]
        self.assertNotIn("read-only", command)
        filesystem = next(value for value in command if value.startswith(f"permissions.{module.RUN_SANDBOX_PROFILE}.filesystem="))
        self.assertIn('":project_roots" = "read"', filesystem)
        self.assertEqual(command[command.index("-C") + 1], str(self.directory))


if __name__ == "__main__":
    unittest.main()
