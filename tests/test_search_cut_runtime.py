"""Offline search isolation regression tests; never invoke a real model or CLI."""

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import Mock, patch

import retrieval_runtime as retrieval
import workflow_runner as runtime
from ui import audits
from test_goal_runtime import Runtime, PROMPTS, PROMPT, SETTINGS, success


class HistoricalPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "run"
        self.workspace.mkdir()
        self.control = self.root / "control"
        self.control.mkdir()
        self.broker_script = self.control / "historical_search.py"
        self.broker_script.write_text("# unused mock broker", encoding="utf-8")
        self.broker_policy = self.control / "broker.json"
        self.broker_policy.write_text('{"schema_version":1,"credential_env":"BRAVE_SEARCH_API_KEY"}', encoding="utf-8")
        self.path = self.control / "retrieval-policy.json"
        self.value = {
            "schemaVersion": 1, "mode": "historical", "runId": "P01",
            "cutoffExclusiveUtc": "2026-10-02T12:00:00Z",
            "broker": {"command": sys.executable, "policyFile": str(self.broker_policy),
                       "auditLog": str(self.control / "search-audit.jsonl"), "args": []},
            "isolation": {"status": "blocked", "adapter": "codex-windows-mxc-v1",
                          "codexHome": str(self.control / "codex-home"), "workspace": str(self.workspace)},
        }
        self.value["broker"]["args"] = [
            str(self.broker_script), "--cutoff", self.value["cutoffExclusiveUtc"],
            "--policy-file", str(self.broker_policy), "--policy-sha256",
            hashlib.sha256(self.broker_policy.read_bytes()).hexdigest(),
            "--audit-log", self.value["broker"]["auditLog"],
        ]
        self.save()
        (self.workspace / "job-settings.json").write_text(json.dumps({"retrievalPolicyPath": str(self.path)}), encoding="utf-8")
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(retrieval.POLICY_ENV, None)
        self.trusted_bin = self.control / "trusted-bin"
        self.trusted_bin.mkdir()
        (self.trusted_bin / "codex.exe").write_bytes(b"mock executable; never started")
        os.environ["PATH"] = str(self.trusted_bin) + os.pathsep + os.environ.get("PATH", "")

    def save(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")

    def policy(self):
        return retrieval.load_policy(self.path)

    def configured_rpc(self, role="author"):
        policy = self.policy()
        retrieval.role_workspace(policy, role).mkdir(parents=True, exist_ok=True)
        retrieval.verify_protected_directories(policy, role, create=True)
        config = retrieval.configuration(policy, role)
        messages = {
            "config/read": {"config": config, "layers": []},
            "windowsSandbox/readiness": {"status": "ready"},
            "mcpServerStatus/list": {"data": [{"name": retrieval.BROKER_NAME,
                "tools": {"search": {}, "open": {}}}] if role == "author" else []},
            "skills/list": {"data": [{"skills": [{"name": "imagegen", "enabled": False}], "errors": []}]},
        }
        rpc = Mock()
        rpc.call.side_effect = lambda method, params: messages[method]
        return policy, messages, rpc, {"CODEX_HOME": str(retrieval.role_home(policy, role))}

    def test_configuration_serializes_valid_toml_and_broker_is_author_only(self):
        policy = self.policy()
        for role in ("author", "structured"):
            with self.subTest(role=role):
                config = tomllib.loads(retrieval.config_text(policy, role))
                self.assertEqual(config, retrieval.configuration(policy, role))
                self.assertEqual(config["web_search"], "disabled")
                self.assertFalse(config["tools"]["web_search"])
                self.assertEqual(set(config["mcp_servers"]), {"historical_search"} if role == "author" else set())
                self.assertFalse(config["permissions"]["historical"]["network"]["enabled"])
                self.assertEqual(config["permissions"]["historical"]["filesystem"][":workspace_roots"]["job-settings.json"], "read")
                for name in ("input.md", "draft.md", "checked-statement.md", "prompts"):
                    self.assertEqual(config["permissions"]["historical"]["filesystem"][":workspace_roots"][name], "read")
                self.assertNotIn("sandbox_mode", config)
                self.assertTrue(all(not row["enabled"] for row in config["skills"]["config"]))

    def test_invalid_policy_and_argument_binding_fail_before_subprocess(self):
        changes = (
            lambda value: value.pop("cutoffExclusiveUtc"),
            lambda value: value.update(mode="live"),
            lambda value: value.update(schemaVersion=True),
            lambda value: value.update(cutoffExclusiveUtc="2026-10-02T12:00:00"),
            lambda value: value["broker"].update(command="python"),
            lambda value: value["broker"]["args"].__setitem__(2, "2026-10-14T00:00:00Z"),
            lambda value: value["broker"]["args"].__setitem__(6, "0" * 64),
            lambda value: value["isolation"].update(codexHome=str(self.workspace / "home")),
        )
        original = copy.deepcopy(self.value)
        for change in changes:
            self.value = copy.deepcopy(original)
            change(self.value)
            self.save()
            with self.subTest(change=change), patch.object(runtime.subprocess, "Popen") as process:
                with self.assertRaises(retrieval.RetrievalPolicyError):
                    self.policy()
                process.assert_not_called()

    def test_model_cannot_modify_broker_script_inside_workspace(self):
        script = self.workspace / "bad.py"
        script.write_text("# writable broker")
        self.value["broker"]["args"][0] = str(script)
        self.save()
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "outside the writable"):
            self.policy()

    def test_saved_run_fallback_hold_and_conflicting_env_fail_closed(self):
        settings = self.workspace / "job-settings.json"
        settings.write_text(json.dumps({"retrievalPolicyPath": str(self.path)}))
        self.assertEqual(retrieval.policy_for_workspace(self.workspace)["runId"], "P01")
        settings.write_text(json.dumps({"retrievalPolicyPath": str(self.path), "launchHold": "Awaiting review"}))
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "Awaiting review"):
            retrieval.policy_for_workspace(self.workspace)
        settings.write_text(json.dumps({"retrievalPolicyPath": str(self.path)}))
        with patch.dict(os.environ, {retrieval.POLICY_ENV: str(self.control / "other.json")}):
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "differs"):
                retrieval.policy_for_workspace(self.workspace)

    def test_isolated_home_never_inherits_credentials_or_overrides(self):
        policy = self.policy()
        inherited = {"PATH": "safe-path", "SystemRoot": "safe-system", "CODEX_HOME": "unsafe",
                     "OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret", "HTTP_PROXY": "unsafe",
                     "BRAVE_SEARCH_API_KEY": "broker-only", "PYTHONPATH": "unsafe"}
        environment = retrieval.isolated_environment(policy, "author", inherited)
        self.assertEqual(environment["CODEX_HOME"], str(retrieval.role_home(policy, "author")))
        self.assertEqual(environment["BRAVE_SEARCH_API_KEY"], "broker-only")
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "HTTP_PROXY", "PYTHONPATH"):
            self.assertNotIn(key, environment)
        shell = retrieval.configuration(policy, "author")["shell_environment_policy"]
        self.assertEqual(shell["inherit"], "none")
        self.assertNotIn("BRAVE_SEARCH_API_KEY", shell["set"])
        (Path(environment["CODEX_HOME"]) / "config.toml").write_text('web_search="live"')
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "configuration has changed"):
            retrieval.isolated_environment(policy, "author", inherited)

    def test_verified_status_does_not_bypass_real_sandbox_failure(self):
        self.value["isolation"]["status"] = "verified"
        self.save()
        policy, messages, rpc, environment = self.configured_rpc()
        with patch.object(retrieval, "_sandbox_canaries", side_effect=RuntimeError("native MXC unavailable")) as canary:
            with self.assertRaisesRegex(RuntimeError, "native MXC unavailable"):
                retrieval.verify_rpc(rpc, policy, "author", environment)
        canary.assert_called_once()

    def test_not_ready_inherited_tools_or_skills_are_rejected(self):
        changes = (
            lambda messages: messages["windowsSandbox/readiness"].update(status="updateRequired"),
            lambda messages: messages["config/read"]["config"].update(sandbox_mode="workspace-write"),
            lambda messages: messages["config/read"]["config"]["features"].update(apps=True),
            lambda messages: messages["config/read"]["config"]["mcp_servers"].update(browser={"command": "browser"}),
            lambda messages: messages["mcpServerStatus/list"]["data"].append({"name": "live_search", "tools": {}}),
            lambda messages: messages["mcpServerStatus/list"]["data"][0]["tools"].update(fetch_url={}),
            lambda messages: messages["skills/list"]["data"][0]["skills"][0].update(enabled=True),
            lambda messages: messages["config/read"]["config"]["permissions"]["historical"]["filesystem"].update({":root": "read"}),
        )
        for change in changes:
            policy, messages, rpc, environment = self.configured_rpc()
            change(messages)
            with self.subTest(change=change), patch.object(retrieval, "_sandbox_canaries") as canary:
                with self.assertRaises(retrieval.RetrievalPolicyError):
                    retrieval.verify_rpc(rpc, policy, "author", environment)
                canary.assert_not_called()

    def test_controller_policy_changes_rejected_before_rpc(self):
        policy, _, rpc, environment = self.configured_rpc()
        self.path.write_text(self.path.read_text() + "\n")
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "changed during"):
            retrieval.verify_rpc(rpc, policy, "author", environment)
        rpc.call.assert_not_called()

    def test_canary_requires_positive_execution_and_each_denial(self):
        policy = self.policy()
        nonce = "testnonce"
        correct = {"nonce": nonce, "insideOk": True, "insideWrite": True, "protectedWrite": False, "settingsWrite": False, "settingsDelete": False, "settingsRename": False, "settingsCanariesRead": True, "outsideRead": False, "network": False, "publicNetwork": False}
        reports = [correct, {**correct, "outsideRead": True}, {**correct, "network": True},
                   {**correct, "insideOk": False}, {**correct, "insideWrite": False},
                   {**correct, "protectedWrite": True}, {**correct, "settingsWrite": True},
                   {**correct, "settingsDelete": True}, {**correct, "settingsRename": True},
                   {**correct, "settingsCanariesRead": False},
                   {**correct, "publicNetwork": True}]
        settings_before = (self.workspace / "job-settings.json").read_bytes()
        for report in reports:
            rpc = Mock()
            rpc.call.return_value = {"exitCode": 0, "stdout": json.dumps(report)}
            with self.subTest(report=report), patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), patch.object(retrieval, "_public_network_control") as public_control:
                if report == correct:
                    retrieval._sandbox_canaries(rpc, policy, "author")
                else:
                    with self.assertRaises(retrieval.RetrievalPolicyError):
                        retrieval._sandbox_canaries(rpc, policy, "author")
                self.assertEqual(public_control.call_count, 2)
            self.assertEqual(rpc.call.call_args.args[0], "command/exec")
            self.assertEqual(rpc.call.call_args.args[1]["permissionProfile"], "historical")
            script = rpc.call.call_args.args[1]["command"][-1]
            self.assertIn("[IO.FileMode]::Open,[IO.FileAccess]::Write", script)
            self.assertEqual((self.workspace / "job-settings.json").read_bytes(), settings_before)
            self.assertFalse(list(self.control.glob(".read-canary-*")))
            self.assertFalse(list(self.workspace.glob(".isolation-canary-*")))

    def test_default_native_search_is_disabled_at_actual_structured_process(self):
        with patch.object(runtime, "codex", return_value="codex-mock"), patch.object(runtime.subprocess, "Popen", side_effect=RuntimeError("stop before subprocess")) as process:
            with self.assertRaisesRegex(RuntimeError, "stop before"):
                runtime.run_structured_attempt("Task", {}, "critic", runtime.MODEL, "max", "fast", "concise")
        arguments = process.call_args.args[0]
        self.assertIn('web_search="disabled"', arguments)
        self.assertIn("tools.web_search=false", arguments)

    def test_historical_invalid_policy_never_spawns_author_structured_or_auditor(self):
        self.value.pop("cutoffExclusiveUtc")
        self.save()
        with patch.dict(os.environ, {retrieval.POLICY_ENV: str(self.path)}), patch.object(runtime.subprocess, "Popen") as process:
            with self.assertRaises(runtime.Error):
                runtime.run_structured_attempt("Task", {}, "critic", runtime.MODEL, "max", "fast", "concise")
            session = runtime.goal_session(Runtime([success()]), PROMPT, prompts=PROMPTS, settings=SETTINGS,
                                           options={"goal_cwd": str(self.workspace)})
            with self.assertRaises(runtime.Error):
                next(session)
            for provider in ("codex", "claude", "kimi"):
                with self.assertRaises(runtime.Error):
                    audits.run_auditor({"provider": provider}, "Task", self.workspace, threading.Event())
            process.assert_not_called()

    def test_historical_structured_attempts_use_isolated_no_broker_configuration(self):
        policy = self.policy()
        child_env = {"CODEX_HOME": str(retrieval.role_home(policy, "structured"))}
        with patch.object(runtime, "retrieval_policy", return_value=policy), patch.object(runtime, "codex", return_value="codex-mock"), patch.object(retrieval, "preflight", return_value=child_env) as preflight, patch.object(runtime.subprocess, "Popen", side_effect=RuntimeError("mock process boundary")) as process:
            for attempt in range(2):
                with self.assertRaisesRegex(RuntimeError, "mock process boundary"):
                    runtime.run_structured_attempt("Task", {}, "critic", runtime.MODEL, "max", "fast", "concise")
                arguments = process.call_args.args[0]
                self.assertIn("--strict-config", arguments)
                self.assertIn('web_search="disabled"', arguments)
                for unsafe_argument in ("-s", "-P", "--ignore-user-config"):
                    self.assertNotIn(unsafe_argument, arguments)
                self.assertEqual(process.call_args.kwargs["env"], child_env)
                self.assertEqual(preflight.call_args.args[1], "structured")
            self.assertEqual(preflight.call_count, 2)
        self.assertEqual(retrieval.configuration(policy, "structured")["mcp_servers"], {})

    def test_failed_isolation_prevents_every_model_launch(self):
        policy = self.policy()
        with patch.object(runtime, "retrieval_policy", return_value=policy), patch.object(runtime, "codex", return_value="codex-mock"), patch.object(retrieval, "preflight", side_effect=retrieval.RetrievalPolicyError("native MXC unavailable")), patch.object(runtime.subprocess, "Popen") as process:
            with self.assertRaisesRegex(runtime.Error, "native MXC unavailable"):
                runtime.run_structured_attempt("Task", {}, "critic", runtime.MODEL, "max", "fast", "concise")
            for options in ({}, {"goal_thread_id": "thread-1"}):
                session = runtime.goal_session(Runtime([success()]), PROMPT, prompts=PROMPTS,
                    settings={**SETTINGS, "model": runtime.MODEL}, options={"goal_cwd": str(self.workspace), **options})
                with self.assertRaisesRegex(runtime.WorkflowPaused, "native MXC unavailable"):
                    next(session)
            process.assert_not_called()

    def test_historical_start_resume_and_repairs_keep_author_only_policy(self):
        for resume in (False, True):
            instance = Runtime([success("proof"), success("repaired")])
            options = {"goal_cwd": str(self.workspace)}
            if resume:
                options["goal_thread_id"] = "thread-1"
            child_env = {"CODEX_HOME": str(self.control / "codex-home/author")}
            with self.subTest(resume=resume), patch.dict(os.environ, {retrieval.POLICY_ENV: str(self.path)}), patch.object(retrieval, "preflight", return_value=child_env), patch.object(retrieval, "verify_rpc") as verified, patch.object(runtime.subprocess, "Popen", return_value=Mock()) as process:
                session = runtime.goal_session(instance, PROMPT, prompts=PROMPTS,
                                               settings={**SETTINGS, "model": runtime.MODEL}, options=options)
                self.assertEqual(next(session)["outcome"], "done")
                self.assertEqual(session.send({"solution": "proof", "bugs": "repair", "round": 1})["outcome"], "done")
                self.assertEqual(verified.call_count, 3)  # process, first turn, repair turn
                session.close()
                command = process.call_args.args[0]
                self.assertIn("--strict-config", command)
                self.assertIn('web_search="disabled"', command)
                self.assertEqual(process.call_args.kwargs["env"], child_env)
                params = next(params for method, params in instance.rpc.calls if method in {"thread/start", "thread/resume"})
                self.assertNotIn("sandbox", params)
                self.assertEqual(params["permissions"], "historical")
                self.assertEqual(set(params["config"]["mcp_servers"]), {"historical_search"})
                self.assertFalse(params["config"]["tools"]["web_search"])

    def test_native_start_resume_defaults_disable_search_without_changing_features(self):
        native_workspace = self.root / "native-run"
        native_workspace.mkdir()
        for resume in (False, True):
            instance = Runtime([success()])
            options = {"goal_cwd": str(native_workspace), **({"goal_thread_id": "thread-1"} if resume else {})}
            with patch.object(runtime.subprocess, "Popen", return_value=Mock()) as process:
                session = runtime.goal_session(instance, PROMPT, prompts=PROMPTS, settings=SETTINGS,
                                               options=options, features=["multi_agent"])
                next(session)
                session.close()
            self.assertIn('web_search="disabled"', process.call_args.args[0])
            params = next(params for method, params in instance.rpc.calls if method in {"thread/start", "thread/resume"})
            self.assertEqual(params["config"]["web_search"], "disabled")
            self.assertFalse(params["config"]["tools"]["web_search"])
            self.assertTrue(params["config"]["features"]["multi_agent"])

    def test_saved_policy_is_required_and_nonlink_before_sandbox_checks(self):
        policy = self.policy()
        retrieval.verify_protected_directories(policy, "author", create=True)
        settings = self.workspace / "job-settings.json"
        original = settings.read_bytes()
        settings.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "retain this run's retrieval policy"):
            retrieval.verify_protected_directories(policy, "author")
        settings.write_bytes(original)
        original_is_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == settings or original_is_symlink(path)):
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "regular protected"):
                retrieval.verify_protected_directories(policy, "author")

    def test_historical_codex_resolution_ignores_workspace_cwd_and_script_shims(self):
        policy = self.policy()
        for name in ("codex.exe", "codex.cmd", "codex.ps1"):
            (self.workspace / name).write_text("untrusted executable")
        path = os.pathsep.join((str(self.workspace), ".", str(self.trusted_bin)))
        self.assertEqual(retrieval.codex_executable(policy, path), str(self.trusted_bin / "codex.exe"))
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "native codex.exe"):
            retrieval.codex_executable(policy, os.pathsep.join((str(self.workspace), ".")))
        (self.trusted_bin / "codex.exe").unlink()
        (self.trusted_bin / "codex.cmd").write_text("unverified wrapper")
        with self.assertRaises(retrieval.RetrievalPolicyError):
            retrieval.codex_executable(policy, str(self.trusted_bin))

    def test_historical_external_commands_block_before_lookup_or_output_changes(self):
        source = self.workspace / "latex-source.tex"
        source.write_text("untrusted TeX")
        output = self.workspace / "final.pdf"
        output.write_bytes(b"existing output")
        config = {"argv": ["latexmk", "latex-source.tex"], "requires": ["pdflatex"],
                  "timeout": 120, "produces": ["final.pdf"], "result": "compilation"}
        with patch.object(runtime, "retrieval_policy", return_value=self.policy()), patch.object(runtime.shutil, "which") as lookup, patch.object(runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(runtime.Error, "isolated compiler/command adapter"):
                runtime._run_command(config, {"state": {}})
            with self.assertRaises(runtime.Error):
                runtime._apply_actions([{"command": config}], {"state": {}}, "final", 0)
            lookup.assert_not_called()
            run.assert_not_called()
        self.assertEqual(source.read_text(), "untrusted TeX")
        self.assertEqual(output.read_bytes(), b"existing output")

    def test_credential_free_broker_receives_no_brave_secret(self):
        self.broker_policy.write_text('{"schema_version":1,"provider":"duckduckgo_wayback"}')
        self.value["broker"]["args"][6] = hashlib.sha256(self.broker_policy.read_bytes()).hexdigest()
        self.save()
        policy = self.policy()
        self.assertEqual(retrieval.configuration(policy, "author")["mcp_servers"]["historical_search"]["env_vars"], [])
        environment = retrieval.isolated_environment(policy, "author", {"BRAVE_SEARCH_API_KEY": "not-needed"})
        self.assertNotIn("BRAVE_SEARCH_API_KEY", environment)


if __name__ == "__main__":
    unittest.main()
