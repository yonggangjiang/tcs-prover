"""Offline search isolation regression tests; never invoke a real model or CLI."""

import copy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import Mock, patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import runtime as retrieval
import workflow_runner as runtime
from test_goal_runtime import Runtime, PROMPTS, PROMPT, SETTINGS, answer, completed, success


class HistoricalPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "run"
        self.workspace.mkdir()
        self.control = self.root / "control"
        self.control.mkdir()
        self.broker_script = self.control / "historical_search.py"
        self.broker_script.write_text("# unused mock broker", encoding="utf-8")
        for name in retrieval.BROKER_MODULES:
            (self.control / name).write_text('# unused mock dependency', encoding='utf-8')
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

    def test_imported_broker_module_changes_abort_before_model_turn(self):
        policy, _, rpc, environment = self.configured_rpc()
        (self.control / retrieval.BROKER_MODULES[0]).write_text('changed dependency')
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, 'broker changed'):
            retrieval.verify_rpc(rpc, policy, 'author', environment)

    def test_scholarly_pacing_cannot_use_solver_writable_state(self):
        broker = {'schema_version': 1, 'provider': 'scholarly_wayback',
                  'solver_workspace': str(self.workspace),
                  'rate_limit_directory': str(self.workspace / 'unsafe-state')}
        self.broker_policy.write_text(json.dumps(broker))
        self.value['broker']['args'][6] = hashlib.sha256(self.broker_policy.read_bytes()).hexdigest()
        self.save()
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, 'outside'):
            self.policy()

    def policy(self):
        return retrieval.load_policy(self.path)

    def protected_auth(self, role="structured", token="protected-login"):
        home = Path(self.value["isolation"]["codexHome"]) / role
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        auth = home / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
        auth.chmod(0o600)
        return auth

    def test_critic_sessions_are_fresh_auth_only_and_keep_original_policy(self):
        policy = self.policy()
        original = copy.deepcopy(policy)
        auth = self.protected_auth()
        (auth.parent / "config.toml").write_text('web_search="live"')
        (auth.parent / "history.jsonl").write_text("old session state")
        (auth.parent / "skills").mkdir()
        with retrieval.critic_session(policy) as first:
            first_home = retrieval.role_home(first, "critic")
            first_workspace = retrieval.role_workspace(first, "critic")
            self.assertEqual(set(path.name for path in first_home.iterdir()), {"auth.json"})
            self.assertEqual((first_home / "auth.json").read_bytes(), auth.read_bytes())
            self.assertEqual(list(first_workspace.iterdir()), [])
            self.assertEqual(first_home.parent, first_workspace.parent)
            self.assertNotEqual(first_home, retrieval.role_home(policy, "structured"))
            if os.name != "nt":
                self.assertEqual(first_home.stat().st_mode & 0o777, 0o700)
                self.assertEqual(first_workspace.stat().st_mode & 0o777, 0o700)
                self.assertEqual((first_home / "auth.json").stat().st_mode & 0o777, 0o600)
            (first_workspace / "proof.md").write_text("proof snapshot")
            with retrieval.critic_session(policy) as second:
                self.assertNotEqual(first_home, retrieval.role_home(second, "critic"))
                self.assertEqual(list(retrieval.role_workspace(second, "critic").iterdir()), [])
        self.assertEqual(policy, original)
        self.assertTrue(first_home.is_dir())
        self.assertEqual((first_workspace / "proof.md").read_text(), "proof snapshot")
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "active protected"):
            retrieval.role_workspace(first, "critic")

    def test_parallel_critics_get_one_independent_session_each(self):
        # The independent critics of one round run at once, each attempt in its own session.
        policy = self.policy()
        auth = self.protected_auth()
        count, entered, sessions, problems = 5, threading.Barrier(5), [], []
        lock = threading.Lock()

        def critic(number):
            try:
                with retrieval.critic_session(policy) as session:
                    home = retrieval.role_home(session, "critic")
                    workspace = retrieval.role_workspace(session, "critic")
                    (workspace / "proof.md").write_text(f"snapshot {number}")
                    entered.wait(10)  # Every session is open at the same time.
                    with lock:
                        sessions.append((home, workspace, (home / "auth.json").read_bytes()))
                    self.assertEqual((workspace / "proof.md").read_text(), f"snapshot {number}")
                    self.assertEqual(retrieval.role_home(session, "critic"), home)
            except BaseException as exc:  # Reported below: an assertion in a thread would be lost.
                problems.append(exc)

        threads = [threading.Thread(target=critic, args=(number,)) for number in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(problems, [])
        self.assertEqual(len({home for home, _, _ in sessions}), count)
        self.assertEqual(len({workspace for _, workspace, _ in sessions}), count)
        self.assertEqual({copied for _, _, copied in sessions}, {auth.read_bytes()})
        # Each session gave up its copy of the login when it ended; the master is untouched.
        self.assertFalse(any((home / "auth.json").exists() for home, _, _ in sessions))
        self.assertEqual(json.loads(auth.read_text()), {"tokens": {"access_token": "protected-login"}})

    def test_critic_auth_prefers_structured_and_falls_back_only_when_missing(self):
        structured = self.protected_auth("structured", "structured-token")
        author = self.protected_auth("author", "author-token")
        os.utime(author, (1, 1))  # Older than the structured copy.
        with retrieval.critic_session(self.policy()) as critic:
            self.assertEqual((retrieval.role_home(critic, "critic") / "auth.json").read_bytes(),
                             structured.read_bytes())
        structured.unlink()
        with retrieval.critic_session(self.policy()) as critic:
            self.assertEqual((retrieval.role_home(critic, "critic") / "auth.json").read_bytes(),
                             author.read_bytes())
        # An invalid structured copy is replaced from a valid login (I-081), never used as is.
        structured.write_text('{"tokens": {}}')
        structured.chmod(0o600)
        with retrieval.critic_session(self.policy()) as critic:
            self.assertEqual((retrieval.role_home(critic, "critic") / "auth.json").read_bytes(),
                             author.read_bytes())
        self.assertEqual(structured.read_bytes(), author.read_bytes())

    def test_critic_rejects_missing_linked_and_oversized_auth(self):
        self.protected_auth("author")
        structured = self.protected_auth()
        for kind in ("symlink", "hardlink", "oversized", "duplicate", "missing"):
            with self.subTest(kind=kind):
                structured.unlink(missing_ok=True)
                author = self.protected_auth("author")
                if kind == "symlink":
                    structured.symlink_to(author)
                elif kind == "hardlink":
                    os.link(author, structured)
                elif kind == "oversized":
                    structured.write_bytes(b"x" * (1024 * 1024 + 1))
                elif kind == "duplicate":
                    structured.write_text('{"OPENAI_API_KEY":"one","OPENAI_API_KEY":"two"}')
                else:
                    author.unlink()
                if kind in ("oversized", "duplicate"):
                    # Never used as is: replaced from the valid login first (I-081).
                    with retrieval.critic_session(self.policy()) as critic:
                        copied = (retrieval.role_home(critic, "critic") / "auth.json").read_bytes()
                    self.assertEqual(copied, author.read_bytes())
                    self.assertEqual(structured.stat().st_nlink, 1)
                    continue
                with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "protected critic"):
                    with retrieval.critic_session(self.policy()):
                        self.fail("Invalid auth must fail closed.")

    def test_critic_session_rejects_changed_base_and_forged_capability(self):
        self.protected_auth()
        policy = self.policy()
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "active protected"):
            retrieval.role_home(policy, "critic")
        forged = {**policy, "_criticSession": str(self.workspace)}
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "active protected"):
            retrieval.role_workspace(forged, "critic")
        with retrieval.critic_session(policy) as critic:
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "active protected"):
                retrieval.role_home(copy.deepcopy(critic), "critic")
            critic["cutoffExclusiveUtc"] = "2100-01-01T00:00:00Z"
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "active protected"):
                retrieval.role_workspace(critic, "critic")
        self.path.write_text(self.path.read_text() + "\n")
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "protected critic"):
            with retrieval.critic_session(policy):
                self.fail("Changed policy must fail before session creation.")

    def test_critic_rejects_linked_session_parent_and_replaced_workspace(self):
        auth = self.protected_auth()
        sessions = auth.parent.parent / "critic-sessions"
        sessions.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "protected critic"):
            with retrieval.critic_session(self.policy()):
                self.fail("Linked session parent must fail.")
        sessions.unlink()
        with retrieval.critic_session(self.policy()) as critic:
            workspace = retrieval.role_workspace(critic, "critic")
            workspace.rmdir()
            workspace.symlink_to(self.workspace, target_is_directory=True)
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "directories changed"):
                retrieval.role_workspace(critic, "critic")

    def test_critic_shell_reads_only_snapshot_without_web_mcp_or_provider_environment(self):
        self.protected_auth()
        policy = self.policy()
        with retrieval.critic_session(policy) as critic:
            config = retrieval.configuration(critic, "critic")
            self.assertEqual(tomllib.loads(retrieval.config_text(critic, "critic")), config)
            self.assertTrue(config["features"]["shell_tool"])
            self.assertTrue(config["features"]["multi_agent"])
            self.assertEqual(config["permissions"]["historical"]["filesystem"][":workspace_roots"]["."], "read")
            self.assertFalse(config["permissions"]["historical"]["network"]["enabled"])
            self.assertEqual(config["web_search"], "disabled")
            self.assertFalse(config["tools"]["web_search"])
            self.assertEqual(config["mcp_servers"], {})
            self.assertTrue(all(not row["enabled"] for row in config["skills"]["config"]))
            env = retrieval.isolated_environment(critic, "critic", {
                "PATH": "safe", "BRAVE_SEARCH_API_KEY": "secret", "OPENAI_API_KEY": "secret",
                "CODEX_API_KEY": "secret", "HTTP_PROXY": "secret", "PYTHONPATH": "secret"})
            self.assertEqual(env["PATH"], "safe")
            for key in ("BRAVE_SEARCH_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "HTTP_PROXY", "PYTHONPATH"):
                self.assertNotIn(key, env)
        self.assertFalse(retrieval.configuration(policy, "structured")["features"]["shell_tool"])
        self.assertTrue(retrieval.configuration(policy, "author")["features"]["shell_tool"])

    def test_critic_preserves_auth_refresh_on_exit_and_never_copies_other_state(self):
        master = self.protected_auth()
        with retrieval.critic_session(self.policy()) as critic:
            home = retrieval.role_home(critic, "critic")
            refreshed = b'{"tokens":{"access_token":"refreshed-token"}}'
            (home / "auth.json").write_bytes(refreshed)
            (home / "history.jsonl").write_text("private critic history")
        self.assertEqual(master.read_bytes(), refreshed)
        self.assertFalse((master.parent / "history.jsonl").exists())
        with self.assertRaisesRegex(RuntimeError, "critic failed"):
            with retrieval.critic_session(self.policy()) as critic:
                home = retrieval.role_home(critic, "critic")
                refreshed = b'{"tokens":{"access_token":"refresh-before-failure"}}'
                (home / "auth.json").write_bytes(refreshed)
                raise RuntimeError("critic failed")
        self.assertEqual(master.read_bytes(), refreshed)
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "active protected"):
            retrieval.role_home(critic, "critic")

    def test_critic_stale_refresh_does_not_overwrite_newer_master(self):
        master = self.protected_auth()
        with retrieval.critic_session(self.policy()) as critic:
            (retrieval.role_home(critic, "critic") / "auth.json").write_text(
                '{"tokens":{"access_token":"stale-child"}}')
            newer = b'{"tokens":{"access_token":"newer-master"}}'
            master.write_bytes(newer)
        self.assertEqual(master.read_bytes(), newer)

    def test_critic_refresh_rejects_replaced_session_or_master_directory(self):
        for replaced in ("session", "master"):
            with self.subTest(replaced=replaced):
                master = self.protected_auth()
                original = master.read_bytes()
                with retrieval.critic_session(self.policy()) as critic:
                    home = retrieval.role_home(critic, "critic")
                    replacement = home if replaced == "session" else master.parent
                    replacement.rename(replacement.with_name(replacement.name + "-saved"))
                    replacement.mkdir(mode=0o700)
                    master.write_bytes(original)
                    (home / "auth.json").write_text('{"tokens":{"access_token":"replacement-token"}}')
                    (home / "auth.json").chmod(0o600)
                self.assertEqual(master.read_bytes(), original)

    def configured_rpc(self, role="author"):
        policy = self.policy()
        retrieval.role_workspace(policy, role).mkdir(parents=True, exist_ok=True)
        retrieval.verify_protected_directories(policy, role, create=True)
        config = retrieval.configuration(policy, role)
        environment = {"CODEX_HOME": str(retrieval.role_home(policy, role))}
        layers = []
        if policy["isolation"]["adapter"] == retrieval.MACOS_ADAPTER:
            # The process's private temp folder (retrieval.private_temp()), set by its session flags.
            temp = retrieval.private_temp(policy, role)
            self.addCleanup(shutil.rmtree, temp, True)
            environment["TMPDIR"] = str(temp)
            overrides = retrieval.temp_overrides(policy, environment)
            config["shell_environment_policy"]["set"].update(overrides["shell_environment_policy"]["set"])
            layers.append({"name": {"type": "sessionFlags"}, "config": overrides})
        messages = {
            "config/read": {"config": config, "layers": layers},
            "windowsSandbox/readiness": {"status": "ready"},
            "mcpServerStatus/list": {"data": [{"name": retrieval.BROKER_NAME,
                "tools": {name: {} for name in retrieval.BROKER_TOOLS}}] if role == "author" else []},
            "skills/list": {"data": [{"skills": [{"name": "imagegen", "enabled": False}], "errors": []}]},
        }
        rpc = Mock()
        rpc.call.side_effect = lambda method, params: messages[method]
        return policy, messages, rpc, environment

    def test_the_subagent_limits_of_a_session_are_audited_exactly(self):
        # The limits are session flags on top of the generated `features.multi_agent_v2 = false`.
        limits = retrieval.agent_limits(3, "Up to 3 subagents.")
        for role, overrides in (("structured", False), ("author", True)):
            with self.subTest(role=role), patch.object(retrieval, "_sandbox_canaries"), \
                    patch.object(retrieval, "_macos_network_canaries"):
                policy, messages, rpc, environment = self.configured_rpc(role)
                read = messages["config/read"]
                read["config"]["features"]["multi_agent_v2"] = {"enabled": False,
                                                                 **limits["features"]["multi_agent_v2"]}
                read["config"]["agents"] = {"enabled": None, "max_depth": 1, "default_subagent_model": None,
                                            "max_concurrent_threads_per_session": 3}
                read["layers"].insert(0, {"name": {"type": "sessionFlags"}, "config": json.loads(json.dumps(limits))})
                retrieval.verify_rpc(rpc, policy, role, environment, allow_runtime_overrides=overrides,
                                     agent_limits=limits)
                # Without the limits it was started with, the same settings are refused.
                with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "multi_agent_v2|inherited"):
                    retrieval.verify_rpc(rpc, policy, role, environment, allow_runtime_overrides=overrides)
                for change in (lambda config: config["agents"].update(max_depth=2),
                               lambda config: config["features"]["multi_agent_v2"].update(
                                   max_concurrent_threads_per_session=11)):
                    changed = json.loads(json.dumps(read))
                    change(changed["config"])
                    messages["config/read"] = changed
                    with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "subagent limits|inherited agents"):
                        retrieval.verify_rpc(rpc, policy, role, environment, allow_runtime_overrides=overrides,
                                             agent_limits=limits)
                messages["config/read"] = read
        # A structured process's session flags must be exactly its limits (and its temp folder's).
        with patch.object(retrieval, "_sandbox_canaries"), patch.object(retrieval, "_macos_network_canaries"):
            policy, messages, rpc, environment = self.configured_rpc("structured")
            read = messages["config/read"]
            read["config"]["features"]["multi_agent_v2"] = {"enabled": False, "max_concurrent_threads_per_session": 1}
            read["layers"].insert(0, {"name": {"type": "sessionFlags"}, "config": {
                "features": {"multi_agent_v2": {"max_concurrent_threads_per_session": 1}},
                "model_reasoning_effort": "max"}})
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "inherited configuration"):
                retrieval.verify_rpc(rpc, policy, "structured", environment, agent_limits=retrieval.agent_limits(0))
            read["layers"][0]["config"].pop("model_reasoning_effort")
            retrieval.verify_rpc(rpc, policy, "structured", environment, agent_limits=retrieval.agent_limits(0))

    def test_a_process_checked_without_subagent_limits_must_have_the_feature_off(self):
        # The launch preflight (ui/retrieval.py) and the First Proof launcher check their processes without limits:
        # the generated `features.multi_agent_v2 = false` must then be exactly what Codex reports.
        retrieval._check_agent_limits({"features": {"multi_agent_v2": False}}, None)
        loosened = ({"enabled": True}, {"enabled": False, "max_concurrent_threads_per_session": 50}, {}, None, True)
        for value in loosened:
            with self.subTest(value=value), self.assertRaisesRegex(retrieval.RetrievalPolicyError, "multi_agent_v2"):
                retrieval._check_agent_limits({"features": {"multi_agent_v2": value}}, None)
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "multi_agent_v2"):
            retrieval._check_agent_limits({"features": {}}, None)
        for role, overrides in (("structured", False), ("author", True)):
            with self.subTest(role=role), patch.object(retrieval, "_sandbox_canaries"), \
                    patch.object(retrieval, "_macos_network_canaries"):
                policy, messages, rpc, environment = self.configured_rpc(role)
                retrieval.verify_rpc(rpc, policy, role, environment, allow_runtime_overrides=overrides)
                for value in loosened[:2]:
                    messages["config/read"]["config"]["features"]["multi_agent_v2"] = value
                    with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "multi_agent_v2"):
                        retrieval.verify_rpc(rpc, policy, role, environment, allow_runtime_overrides=overrides)

    def test_the_launch_preflight_refuses_a_process_whose_subagent_feature_is_on(self):
        # As ui/retrieval.py calls it: preflight() without agent limits, here on macOS with stand-in processes.
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        executable = str(self.trusted_bin / "codex")
        started, checked, real_verify = [], [], retrieval.verify_rpc
        for loosened in (False, {"enabled": True}, {"enabled": False, "max_concurrent_threads_per_session": 50}):
            with self.subTest(loosened=loosened), patch.object(retrieval, "_sandbox_canaries"), \
                    patch.object(retrieval, "_macos_network_canaries"):
                policy, messages, configured, configured_environment = self.configured_rpc("structured")
                messages["config/read"]["config"]["features"]["multi_agent_v2"] = loosened

                def verify(rpc, policy, role, environment, agent_limits=None):
                    # Codex's answers for the checked process, with the limits preflight() passes on.
                    checked.append(agent_limits)
                    return real_verify(configured, policy, role, configured_environment, agent_limits=agent_limits)

                def probe(process):
                    rpc = Mock()
                    rpc.call.side_effect = lambda method, params: {"codexHome": started[-1][1]["CODEX_HOME"]}
                    return rpc

                with patch.object(retrieval.sys, "platform", "darwin"), \
                        patch.object(retrieval, "_native_macos_binary", return_value=True), \
                        patch.object(retrieval.subprocess, "Popen",
                                     side_effect=lambda argv, **kwargs: started.append((argv, kwargs["env"])) or Mock()), \
                        patch.object(retrieval, "_ProbeRPC", side_effect=probe), \
                        patch.object(retrieval, "verify_rpc", side_effect=verify):
                    if loosened is False:
                        retrieval.remove_private_temp(policy, retrieval.preflight(policy, "structured", executable,
                                                                                  {"PATH": "/usr/bin"}))
                    else:
                        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "multi_agent_v2"):
                            retrieval.preflight(policy, "structured", executable, {"PATH": "/usr/bin"})
        self.assertEqual(checked, [None, None, None])
        # No agent limits reach the checked app-server either.
        self.assertFalse([argument for argv, _ in started for argument in argv if "agent" in argument])

    def test_a_web_cutoff_authors_thread_keeps_the_subagent_limits_of_its_app_server(self):
        # A thread's config replaces the app-server's features table (generated with multi_agent_v2 = false), so
        # the thread must carry the limits itself; Codex 0.160 keeps the feature off for this merged table. The
        # generated table turns multi_agent on for every role: the setting decides, as for the app-server.
        policy = self.policy()

        class Author:
            Error = runtime.Error
            wire_model = staticmethod(lambda model: model)

        hint = runtime.SUBAGENT_HINT.format(count=2, s="s")
        for features, count, expected_v2, expected_agents in (
                (["multi_agent"], 2, {"max_concurrent_threads_per_session": 3, "multi_agent_mode_hint_text": hint},
                 {"max_concurrent_threads_per_session": 2, "max_depth": 1}),
                ([], 0, {"max_concurrent_threads_per_session": 1}, None)):
            with self.subTest(count=count):
                options = runtime._author_thread_options(
                    Author, None, directory=str(self.workspace), policy=policy, role="author", model="gpt-6.1-sol",
                    settings={"effort": "max", "speed": "standard", "subagents": count}, summary="concise",
                    features=features, classic=False, emit=lambda *args, **kwargs: None, environment={})
                config = options["config"]
                self.assertEqual(options["permissions"], retrieval.PROFILE)
                self.assertEqual(config["features"]["multi_agent_v2"], expected_v2)
                self.assertEqual(config.get("agents"), expected_agents)
                self.assertEqual(config["features"]["multi_agent"], bool(count))
                # Exactly what the app-server was started with and verify_rpc() checked.
                limits = runtime.subagent_limits(features, count)
                self.assertEqual(config["features"]["multi_agent_v2"], limits["features"]["multi_agent_v2"])
                self.assertEqual(config.get("agents"), limits.get("agents"))

    def test_configuration_serializes_valid_toml_and_broker_is_author_only(self):
        policy = self.policy()
        for role in ("author", "structured"):
            with self.subTest(role=role):
                config = tomllib.loads(retrieval.config_text(policy, role))
                self.assertEqual(config, retrieval.configuration(policy, role))
                self.assertEqual(config["web_search"], "disabled")
                self.assertFalse(config["tools"]["web_search"])
                self.assertTrue(config["features"]["code_mode_host"])
                self.assertEqual(set(config["mcp_servers"]), {"historical_search"} if role == "author" else set())
                self.assertEqual(config["approval_policy"], "never")
                if role == "author":
                    broker = config["mcp_servers"]["historical_search"]
                    self.assertEqual(broker["enabled_tools"], ["search", "open", "find"])
                    self.assertEqual(broker["tools"], {name: {"approval_mode": "approve"} for name in ("search", "open", "find")})
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
                     "BRAVE_SEARCH_API_KEY": "broker-only", "PYTHONPATH": "unsafe",
                     "http_proxy": "unsafe", "HTTPS_PROXY": "unsafe", "https_proxy": "unsafe",
                     "ALL_PROXY": "unsafe", "all_proxy": "unsafe", "NO_PROXY": "unsafe",
                     "DYLD_INSERT_LIBRARIES": "unsafe", "LD_PRELOAD": "unsafe"}
        environment = retrieval.isolated_environment(policy, "author", inherited)
        self.assertEqual(environment["CODEX_HOME"], str(retrieval.role_home(policy, "author")))
        self.assertEqual(environment["BRAVE_SEARCH_API_KEY"], "broker-only")
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "HTTP_PROXY", "PYTHONPATH", "http_proxy",
                    "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY",
                    "DYLD_INSERT_LIBRARIES", "LD_PRELOAD"):
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
            lambda messages: messages["mcpServerStatus/list"]["data"][0]["tools"].pop("find"),
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
        # A failed positive control proves nothing either way: it is checked again (I-005).
        unproven = ({**correct, "insideOk": False}, {**correct, "insideWrite": False},
                    {**correct, "settingsCanariesRead": False})
        settings_before = (self.workspace / "job-settings.json").read_bytes()
        for report in reports:
            rpc = Mock()
            rpc.call.return_value = {"exitCode": 0, "stdout": json.dumps(report)}
            with self.subTest(report=report), patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), patch.object(retrieval, "_public_network_control") as public_control:
                if report == correct:
                    retrieval._sandbox_canaries(rpc, policy, "author")
                elif report in unproven:
                    with self.assertRaises(retrieval.IsolationCheckUnavailable):
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

    def test_default_native_search_is_live_at_actual_structured_process(self):
        with patch.object(runtime, "codex", return_value="codex-mock"), patch.object(runtime.subprocess, "Popen", side_effect=RuntimeError("stop before subprocess")) as process:
            with self.assertRaisesRegex(RuntimeError, "stop before"):
                runtime.run_structured_attempt("Task", {}, "critic", runtime.MODEL, "max", "fast", "concise")
        arguments = process.call_args.args[0]
        self.assertIn('web_search="live"', arguments)
        self.assertIn("tools.web_search=true", arguments)

    def test_historical_invalid_policy_never_spawns_author_or_structured(self):
        self.value.pop("cutoffExclusiveUtc")
        self.save()
        with patch.dict(os.environ, {retrieval.POLICY_ENV: str(self.path)}), patch.object(runtime.subprocess, "Popen") as process:
            with self.assertRaises(runtime.Error):
                runtime.run_structured_attempt("Task", {}, "critic", runtime.MODEL, "max", "fast", "concise")
            session = runtime.goal_session(Runtime([success()]), PROMPT, prompts=PROMPTS, settings=SETTINGS,
                                           options={"goal_cwd": str(self.workspace)})
            with self.assertRaises(runtime.Error):
                next(session)
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

    def test_web_cutoff_processes_run_with_their_own_private_temp_folder_and_remove_it(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        made, limits = [], []

        def preflight(policy, role, executable, inherited, agent_limits=None):
            # The verified environment names a folder made for this one process (retrieval.private_temp()).
            limits.append(agent_limits)
            temp = retrieval.private_temp(policy, role)
            made.append(temp)
            return {"CODEX_HOME": str(retrieval.role_home(policy, role)), "TMPDIR": str(temp),
                    "TMPPREFIX": str(temp / "zsh")}

        def flags(temp):
            return [f"shell_environment_policy.set.TMPDIR={json.dumps(str(temp))}",
                    f"shell_environment_policy.set.TMPPREFIX={json.dumps(str(temp / 'zsh'))}"]

        with patch.object(runtime, "retrieval_policy", return_value=policy), \
                patch.object(retrieval, "codex_executable", return_value="codex-mock"), \
                patch.object(retrieval, "preflight", side_effect=preflight), \
                patch.object(runtime.subprocess, "Popen", side_effect=RuntimeError("mock process boundary")) as process:
            with self.assertRaisesRegex(RuntimeError, "mock process boundary"):
                runtime.run_structured_attempt("Task", {}, "final", runtime.MODEL, "max", "fast", "concise")
        command, environment = process.call_args.args[0], process.call_args.kwargs["env"]
        self.assertEqual(environment["TMPDIR"], str(made[-1]))
        for flag in flags(made[-1]):
            self.assertIn(flag, command)
        self.assertLess(command.index(flags(made[-1])[0]), command.index("exec"))
        self.assertFalse(made[-1].exists())  # Removed when the call ended.
        # The writer has no subagents: its check and its process both have the session limit of one thread.
        self.assertEqual(limits[-1], retrieval.agent_limits(0))
        self.assertIn("features.multi_agent_v2.max_concurrent_threads_per_session=1", command)

        def thread_shell(instance):
            """The shell environment the author's thread/start or thread/resume gives its commands."""
            [params] = [params for method, params in instance.rpc.calls if method in {"thread/start", "thread/resume"}]
            return params["config"]["shell_environment_policy"]

        def expected_shell(temp):
            # The audited one, and the folder the app-server's session flags name: a thread's table replaces those.
            shell = retrieval.configuration(policy, "author")["shell_environment_policy"]
            return {**shell, "set": {**shell["set"], "TMPDIR": str(temp), "TMPPREFIX": str(temp / "zsh")}}

        for resume in (False, True):
            options = {"goal_cwd": str(self.workspace), **({"goal_thread_id": "thread-1"} if resume else {})}
            with self.subTest(resume=resume), patch.dict(os.environ, {retrieval.POLICY_ENV: str(self.path)}), \
                    patch.object(retrieval, "codex_executable", return_value="codex-mock"), \
                    patch.object(retrieval, "preflight", side_effect=preflight), \
                    patch.object(retrieval, "verify_rpc"), \
                    patch.object(runtime.subprocess, "Popen", return_value=Mock()) as process:
                instance = Runtime([success("proof")])
                session = runtime.goal_session(instance, PROMPT, prompts=PROMPTS,
                                               settings={**SETTINGS, "model": runtime.MODEL}, options=options)
                self.assertEqual(next(session)["outcome"], "done")
                temp = made[-1]
                self.assertTrue(temp.is_dir())  # The author's for as long as its app-server runs.
                self.assertEqual(process.call_args.kwargs["env"]["TMPDIR"], str(temp))
                command = process.call_args.args[0]
                for flag in flags(temp):
                    self.assertIn(flag, command)
                self.assertEqual(thread_shell(instance), expected_shell(temp))
                session.close()
                self.assertFalse(temp.exists())
        # A paused author's reply too, in a new app-server with a new folder.
        with patch.dict(os.environ, {retrieval.POLICY_ENV: str(self.path)}), \
                patch.object(retrieval, "codex_executable", return_value="codex-mock"), \
                patch.object(retrieval, "preflight", side_effect=preflight), \
                patch.object(retrieval, "verify_rpc"), \
                patch.object(runtime.subprocess, "Popen", return_value=Mock()) as process:
            instance = Runtime([[answer("Lemma 2 is open."), completed()]])
            result = runtime.author_conversation(
                instance, "Summarize.", settings={**SETTINGS, "model": runtime.MODEL},
                options={"goal_cwd": str(self.workspace), "goal_thread_id": "thread-1"}, features=[],
                conversation_id="c" * 32, label="Progress summary requested")
        self.assertEqual(result["reply"], "Lemma 2 is open.")
        for flag in flags(made[-1]):
            self.assertIn(flag, process.call_args.args[0])
        self.assertEqual(thread_shell(instance), expected_shell(made[-1]))
        self.assertNotEqual(made[-1], temp)
        self.assertFalse(made[-1].exists())
        # An environment without a private temp folder (another adapter's) leaves the audited shell environment.
        options = runtime._author_thread_options(
            Runtime([]), None, directory=str(self.workspace), policy=policy, role="author", model=runtime.MODEL,
            settings={**SETTINGS, "speed": "standard"}, summary="concise", features=(), classic=False, emit=None,
            environment={"CODEX_HOME": str(retrieval.role_home(policy, "author"))})
        self.assertEqual(options["config"]["shell_environment_policy"],
                         retrieval.configuration(policy, "author")["shell_environment_policy"])
        # An app-server that cannot be started leaves no folder behind either.
        with patch.dict(os.environ, {retrieval.POLICY_ENV: str(self.path)}), \
                patch.object(retrieval, "codex_executable", return_value="codex-mock"), \
                patch.object(retrieval, "preflight", side_effect=preflight), \
                patch.object(runtime.subprocess, "Popen", side_effect=OSError("cannot start")):
            session = runtime.goal_session(Runtime([success("proof")]), PROMPT, prompts=PROMPTS,
                                           settings={**SETTINGS, "model": runtime.MODEL},
                                           options={"goal_cwd": str(self.workspace)})
            with self.assertRaises(runtime.WorkflowPaused):
                next(session)
        self.assertFalse(made[-1].exists())
        self.assertEqual(list((Path(policy["isolation"]["codexHome"]) / "tmp").iterdir()), [])

    def test_native_start_resume_defaults_enable_live_search_without_changing_features(self):
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
            self.assertIn('web_search="live"', process.call_args.args[0])
            params = next(params for method, params in instance.rpc.calls if method in {"thread/start", "thread/resume"})
            self.assertEqual(params["config"]["web_search"], "live")
            self.assertTrue(params["config"]["tools"]["web_search"])
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

    def test_saved_settings_retries_partial_controller_write_before_resume(self):
        policy = self.policy()
        retrieval.verify_protected_directories(policy, "author", create=True)
        settings = self.workspace / "job-settings.json"
        complete = settings.read_text()
        original_read = Path.read_text
        reads = iter(("", '{"retrievalPolicyPath":', complete))

        def read(path, *args, **kwargs):
            return next(reads) if path == settings else original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", read), patch.object(retrieval.time, "sleep") as sleep:
            retrieval.verify_protected_directories(policy, "author")
            self.assertEqual(sleep.call_count, 2)
        self.assertEqual(settings.read_text(), complete)
        reads = iter(("", complete))
        with patch.object(Path, "read_text", read), patch.object(retrieval.time, "sleep") as sleep:
            self.assertEqual(retrieval.policy_for_workspace(self.workspace)["runId"], "P01")
            sleep.assert_called_once()

    def test_saved_settings_retry_is_bounded_and_invalid_policy_never_retries(self):
        policy = self.policy()
        retrieval.verify_protected_directories(policy, "author", create=True)
        settings = self.workspace / "job-settings.json"
        settings.write_text("")
        with patch.object(retrieval.time, "sleep") as sleep:
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "Cannot verify the protected"):
                retrieval.verify_protected_directories(policy, "author")
            self.assertEqual(sleep.call_count, len(retrieval.SETTINGS_READ_RETRY_DELAYS))
        for contents in ('{}', '[]', json.dumps({"retrievalPolicyPath": str(self.control / "other.json")})):
            settings.write_text(contents)
            with self.subTest(contents=contents), patch.object(retrieval.time, "sleep") as sleep:
                with self.assertRaises(retrieval.RetrievalPolicyError):
                    retrieval.verify_protected_directories(policy, "author")
                sleep.assert_not_called()

    def test_settings_retry_rechecks_nonlink_boundary_on_every_attempt(self):
        policy = self.policy()
        retrieval.verify_protected_directories(policy, "author", create=True)
        settings = self.workspace / "job-settings.json"
        original_is_symlink = Path.is_symlink
        changed = False

        def delay(_):
            nonlocal changed
            changed = True

        settings.write_text("")
        with patch.object(Path, "is_symlink", lambda path: (path == settings and changed) or original_is_symlink(path)), patch.object(retrieval.time, "sleep", side_effect=delay) as sleep:
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "regular protected"):
                retrieval.verify_protected_directories(policy, "author")
            sleep.assert_called_once()

    def test_historical_codex_resolution_ignores_workspace_cwd_and_script_shims(self):
        policy = self.policy()
        for name in ("codex.exe", "codex.cmd", "codex.ps1"):
            (self.workspace / name).write_text("untrusted executable")
        path = os.pathsep.join((str(self.workspace), ".", str(self.trusted_bin)))
        self.assertEqual(retrieval.codex_executable(policy, path), str(self.trusted_bin / "codex.exe"))
        # No usable binary (also while Codex updates itself): checked again later.
        with self.assertRaisesRegex(retrieval.IsolationCheckUnavailable, "native codex.exe"):
            retrieval.codex_executable(policy, os.pathsep.join((str(self.workspace), ".")))
        (self.trusted_bin / "codex.exe").unlink()
        (self.trusted_bin / "codex.cmd").write_text("unverified wrapper")
        with self.assertRaises(retrieval.IsolationCheckUnavailable):
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

    def test_macos_adapter_preserves_critic_delegation_and_disables_image_bypass(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        for role in ("author", "structured"):
            config = retrieval.configuration(policy, role)
            self.assertNotIn("windows", config)
            self.assertNotIn("view_image", config["tools"])
            self.assertFalse(config["features"]["view_image"])
            self.assertTrue(config["features"]["multi_agent"])
            self.assertEqual(config["features"]["shell_tool"], role == "author")
            self.assertEqual(set(config["mcp_servers"]), {retrieval.BROKER_NAME} if role == "author" else set())

    def test_macos_resolver_rejects_shims_and_workspace_native_binaries(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        for directory in (self.workspace, self.trusted_bin):
            candidate = directory / "codex"
            candidate.write_bytes(b"\xcf\xfa\xed\xfe" + b"not executed")
            candidate.chmod(0o755)
        search_path = os.pathsep.join((str(self.workspace), ".", str(self.trusted_bin)))
        self.assertEqual(retrieval.codex_executable(policy, search_path), str(self.trusted_bin / "codex"))
        (self.trusted_bin / "codex").write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(retrieval.IsolationCheckUnavailable, "Mach-O"):
            retrieval.codex_executable(policy, search_path)

    def test_macos_effective_config_accepts_cli_normalization_and_checks_raw_layer(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.broker_policy.write_text('{"schema_version":1,"provider":"duckduckgo_wayback"}')
        self.value["broker"]["args"][6] = hashlib.sha256(self.broker_policy.read_bytes()).hexdigest()
        self.save()
        policy, messages, rpc, environment = self.configured_rpc()
        raw = retrieval.configuration(policy, "author")
        messages["config/read"]["layers"] = [{"name": {"type": "user", "file": str(retrieval.role_home(policy, "author") / "config.toml")}, "config": raw}]
        effective = messages["config/read"]["config"]
        effective["tools"]["web_search"] = None
        effective["mcp_servers"][retrieval.BROKER_NAME].pop("env_vars")
        with patch.object(retrieval, "_sandbox_canaries") as canaries, patch.object(retrieval, "_macos_network_canaries") as network:
            retrieval.verify_rpc(rpc, policy, "author", environment)
            canaries.assert_called_once()
            network.assert_called_once()
        self.assertNotIn("windowsSandbox/readiness", [call.args[0] for call in rpc.call.call_args_list])
        raw["features"]["view_image"] = True
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "raw user config"):
            retrieval.verify_rpc(rpc, policy, "author", environment)

    def test_macos_canary_invokes_native_shell_and_checks_every_boundary(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        nonce = "macnonce"
        report = {"nonce": nonce, "insideOk": True, "insideWrite": True,
                  "protectedWrite": False, "settingsWrite": False,
                  "settingsDelete": False, "settingsRename": False,
                  "settingsCanariesRead": True, "outsideRead": False,
                  "network": False, "publicNetwork": False,
                  "slashTmpRead": False, "slashTmpWrite": False, "varTmpRead": False, "varTmpWrite": False}
        rpc = Mock()
        rpc.call.return_value = {"exitCode": 0, "stdout": json.dumps(report)}
        settings = (self.workspace / "job-settings.json").read_bytes()
        with patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), patch.object(retrieval, "_public_network_control"):
            retrieval._sandbox_canaries(rpc, policy, "author")
        command = rpc.call.call_args.args[1]["command"]
        self.assertEqual(command[:2], ["/bin/sh", "-c"])
        for command_part in ("exec 3>>", "/bin/rm", "/bin/mv", "/usr/bin/nc", "127.0.0.1", "1.1.1.1",
                             f"/bin/cat /private/tmp/.retrieval-tmp-canary-{nonce}",
                             f"> /private/tmp/.retrieval-tmp-write-{nonce}",
                             f"/bin/cat /private/var/tmp/.retrieval-tmp-canary-{nonce}",
                             f"> /private/var/tmp/.retrieval-tmp-write-{nonce}"):
            self.assertIn(command_part, command[-1])
        self.assertEqual((self.workspace / "job-settings.json").read_bytes(), settings)

    def test_macos_canary_proves_the_private_temp_folder_and_closes_its_neighbours(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        temp = retrieval.private_temp(policy, "author")
        self.addCleanup(shutil.rmtree, temp, True)
        nonce = "tempnonce"
        correct = {"nonce": nonce, "insideOk": True, "insideWrite": True, "protectedWrite": False,
                   "settingsWrite": False, "settingsDelete": False, "settingsRename": False,
                   "settingsCanariesRead": True, "outsideRead": False, "network": False, "publicNetwork": False,
                   "slashTmpRead": False, "slashTmpWrite": False, "varTmpRead": False, "varTmpWrite": False,
                   "privateTempWrite": True, "privateTempNeighborRead": False}
        written, neighbor = temp / f".retrieval-temp-canary-{nonce}", temp.parent / f".retrieval-temp-neighbor-{nonce}"
        seen = []

        def answer(report):
            def call(method, params):
                # The controller's file in codex-home/tmp/, next to the folder, exists while the command runs.
                seen.append((neighbor.read_text() if neighbor.exists() else None, params["command"][-1]))
                if "privateTempWrite" in report:
                    written.write_text(nonce)
                return {"exitCode": 0, "stdout": json.dumps(report)}
            return call

        for change, outcome in (({}, None), ({"privateTempNeighborRead": True}, retrieval.RetrievalPolicyError),
                                ({"privateTempWrite": False}, retrieval.IsolationCheckUnavailable)):
            with self.subTest(change=change):
                rpc = Mock()
                rpc.call.side_effect = answer({**correct, **change})
                with patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), \
                        patch.object(retrieval, "_public_network_control"):
                    if outcome is None:
                        retrieval._sandbox_canaries(rpc, policy, "author", temp)
                    else:
                        with self.assertRaises(outcome) as caught:
                            retrieval._sandbox_canaries(rpc, policy, "author", temp)
                        if outcome is retrieval.RetrievalPolicyError:
                            self.assertNotIsInstance(caught.exception, retrieval.IsolationCheckUnavailable)
                text, script = seen[-1]
                self.assertEqual(text, nonce)
                # Written through $TMPDIR, read back where it must be; TMPPREFIX there too.
                for part in (f'printf %s {nonce} > "$TMPDIR"/{written.name}', f"/bin/cat {written})\" = {nonce}",
                             f'test "$TMPPREFIX" = {temp / "zsh"}', f"/bin/cat {neighbor}"):
                    self.assertIn(part, script)
                self.assertFalse(written.exists())
                self.assertFalse(neighbor.exists())
        # Without the private temp folder (the Windows adapter, a direct call) nothing about it is probed.
        rpc = Mock()
        rpc.call.side_effect = answer({key: value for key, value in correct.items() if "privateTemp" not in key})
        with patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), \
                patch.object(retrieval, "_public_network_control"):
            retrieval._sandbox_canaries(rpc, policy, "author")
        self.assertEqual(seen[-1][0], None)
        self.assertNotIn("TMPDIR", seen[-1][1])
        self.assertEqual(list(temp.iterdir()), [])

        # Given the folder that holds every process's temp folder (as verify_rpc() would without its folder
        # check), the controller's file is still in that folder: inside the grant, so the command reads it.
        def neighbor_read(method, params):
            seen.append((neighbor.read_text() if neighbor.exists() else None, params["command"][-1]))
            return {"exitCode": 0, "stdout": json.dumps({**correct, "privateTempNeighborRead": True})}

        rpc = Mock()
        rpc.call.side_effect = neighbor_read
        with patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), \
                patch.object(retrieval, "_public_network_control"), \
                self.assertRaisesRegex(retrieval.RetrievalPolicyError, "private temp folders"):
            retrieval._sandbox_canaries(rpc, policy, "author", temp.parent)
        self.assertEqual(seen[-1][0], nonce)
        self.assertIn(f"/bin/cat {neighbor}", seen[-1][1])
        self.assertFalse(neighbor.exists())

    def test_private_temp_folders_are_fresh_owner_only_outside_the_workspace_and_swept(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        parent = Path(policy["isolation"]["codexHome"]) / "tmp"
        first, second = retrieval.private_temp(policy, "author"), retrieval.private_temp(policy, "structured")
        for folder, role in ((first, "author"), (second, "structured")):
            self.assertEqual(folder.parent, parent.resolve())
            self.assertRegex(folder.name, rf"^{role}-{os.getpid()}-\w{{8}}$")
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            self.assertFalse(folder.is_relative_to(self.workspace))
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        environment = retrieval.isolated_environment(policy, "author", {"PATH": "/usr/bin"}, temp=first)
        self.assertEqual((environment["TMPDIR"], environment["TMPPREFIX"]), (str(first), str(first / "zsh")))
        self.assertNotIn("TMPDIR", retrieval.isolated_environment(policy, "author", {"TMPDIR": "/var/folders/x"}))
        # The folders of processes that ended go at the next start; nothing else does.
        dead, living = parent / "critic-999999-abcdefgh", parent / f"author-{os.getppid()}-abcdefgh"
        other = [parent / "notes", parent / "author-12-short"]
        for path in (dead, living, *other):
            path.mkdir()
        third = retrieval.private_temp(policy, "author")
        self.assertFalse(dead.exists())
        self.assertTrue(all(path.exists() for path in (first, second, living, *other)))
        # Removed when its process ends: only a folder of this kind in this run's codex-home/tmp/.
        retrieval.remove_private_temp(policy, {"TMPDIR": str(first)})
        self.assertFalse(first.exists())
        for environment in ({"TMPDIR": str(self.workspace)}, {"TMPDIR": str(parent / "notes")}, {}, None):
            retrieval.remove_private_temp(policy, environment)
        retrieval.remove_private_temp(None, {"TMPDIR": str(second)})
        # A policy without its isolation settings (a test double) names no folder of its own either.
        retrieval.remove_private_temp({"_path": str(self.path)}, {"TMPDIR": str(second)})
        self.assertEqual(retrieval.temp_overrides({"_path": str(self.path)}, {"TMPDIR": str(second)}), {})
        self.assertTrue(all(path.exists() for path in (self.workspace, parent / "notes", second, third)))
        link = parent / f"author-{os.getpid()}-linkfold"
        link.symlink_to(self.workspace)
        retrieval.remove_private_temp(policy, {"TMPDIR": str(link)})
        self.assertTrue(link.is_symlink() and self.workspace.is_dir())
        link.unlink()
        # A critic's folder is outside its snapshot too.
        self.protected_auth()
        with retrieval.critic_session(policy) as critic:
            folder = retrieval.private_temp(critic, "critic")
            self.assertTrue(folder.name.startswith(f"critic-{os.getpid()}-"))
            self.assertFalse(folder.is_relative_to(retrieval.role_workspace(critic, "critic")))
        # A parent that is a link is not the controller's; a folder that cannot be made is checked again.
        shutil.rmtree(parent)
        parent.symlink_to(self.workspace)
        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "inspect"):
            retrieval.private_temp(policy, "author")
        parent.unlink()
        with patch.object(retrieval.tempfile, "mkdtemp", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(retrieval.IsolationCheckUnavailable, "disk full"):
            retrieval.private_temp(policy, "author")
        self.assertEqual([path for path in self.workspace.iterdir() if path.name.startswith(("author-", "critic-"))], [])

    def test_macos_preflight_checks_a_fresh_private_temp_folder_that_failures_remove(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        executable = str(self.trusted_bin / "codex")
        started, checked = [], []

        def verify(rpc, policy, role, environment, agent_limits=None):
            checked.append(Path(environment["TMPDIR"]).is_dir())

        def probe(process):
            rpc = Mock()
            rpc.call.side_effect = lambda method, params: {"codexHome": started[-1][1]["CODEX_HOME"]}
            return rpc

        with patch.object(retrieval.sys, "platform", "darwin"), \
                patch.object(retrieval, "_native_macos_binary", return_value=True), \
                patch.object(retrieval.subprocess, "Popen",
                             side_effect=lambda argv, **kwargs: started.append((argv, kwargs["env"])) or Mock()), \
                patch.object(retrieval, "_ProbeRPC", side_effect=probe):
            with patch.object(retrieval, "verify_rpc", side_effect=verify):
                environment = retrieval.preflight(policy, "structured", executable, {"PATH": "/usr/bin"})
            temp = Path(environment["TMPDIR"])
            argv, process_environment = started[-1]
            self.assertEqual(argv, [executable, "--strict-config", "app-server",
                                    *retrieval.temp_arguments(policy, environment)])
            self.assertEqual(process_environment, environment)
            self.assertEqual((temp.parent, checked), ((Path(policy["isolation"]["codexHome"]) / "tmp").resolve(),
                                                      [True]))
            self.assertEqual(environment["TMPPREFIX"], str(temp / "zsh"))
            self.assertTrue(temp.is_dir())  # The process that runs next uses it, then removes it.
            retrieval.remove_private_temp(policy, environment)
            self.assertFalse(temp.exists())
            with patch.object(retrieval, "verify_rpc", side_effect=retrieval.RetrievalPolicyError("violation")), \
                    self.assertRaisesRegex(retrieval.RetrievalPolicyError, "violation"):
                retrieval.preflight(policy, "structured", executable, {"PATH": "/usr/bin"})
            self.assertFalse(Path(started[-1][1]["TMPDIR"]).exists())
        self.assertEqual(list((Path(policy["isolation"]["codexHome"]) / "tmp").iterdir()), [])

    def test_macos_canary_proves_tmp_and_var_tmp_closed_and_cleans_up(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        nonce = "tmpnonce"
        correct = {"nonce": nonce, "insideOk": True, "insideWrite": True, "protectedWrite": False,
                   "settingsWrite": False, "settingsDelete": False, "settingsRename": False,
                   "settingsCanariesRead": True, "outsideRead": False, "network": False, "publicNetwork": False,
                   "slashTmpRead": False, "slashTmpWrite": False, "varTmpRead": False, "varTmpWrite": False}
        canaries = [Path("/private/tmp") / f".retrieval-tmp-canary-{nonce}",
                    Path("/private/var/tmp") / f".retrieval-tmp-canary-{nonce}"]
        written = [Path("/private/tmp") / f".retrieval-tmp-write-{nonce}",
                   Path("/private/var/tmp") / f".retrieval-tmp-write-{nonce}"]
        seen = []

        def answer(report, write=False):
            def call(method, params):
                # The controller's files exist while the command runs; a write that held leaves one behind.
                seen.append([path.read_text() for path in canaries])
                if write:
                    for path in written:
                        path.write_text("x")
                return {"exitCode": 0, "stdout": json.dumps(report)}
            return call

        for change, outcome in (({}, None), ({"slashTmpRead": True}, retrieval.RetrievalPolicyError),
                                ({"varTmpWrite": True}, retrieval.RetrievalPolicyError),
                                ({"slashTmpWrite": True}, retrieval.RetrievalPolicyError)):
            with self.subTest(change=change):
                rpc = Mock()
                rpc.call.side_effect = answer({**correct, **change}, write=bool(change))
                with patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), \
                        patch.object(retrieval, "_public_network_control"):
                    if outcome is None:
                        retrieval._sandbox_canaries(rpc, policy, "author")
                    else:
                        with self.assertRaisesRegex(outcome, "/tmp and /var/tmp"):
                            retrieval._sandbox_canaries(rpc, policy, "author")
                self.assertEqual(seen[-1], [nonce, nonce])
                for path in (*canaries, *written):
                    self.assertFalse(path.exists(), path)
        # A report without the /tmp probes proves nothing: it is checked again.
        rpc = Mock()
        rpc.call.side_effect = answer({key: value for key, value in correct.items() if "Tmp" not in key})
        with patch.object(retrieval.uuid, "uuid4", return_value=Mock(hex=nonce)), \
                patch.object(retrieval, "_public_network_control"), \
                self.assertRaises(retrieval.IsolationCheckUnavailable):
            retrieval._sandbox_canaries(rpc, policy, "author")

    def test_macos_profile_denies_tmp_and_var_tmp_and_verification_accepts_only_that(self):
        for adapter, expected in ((retrieval.WINDOWS_ADAPTER, {}),
                                  (retrieval.MACOS_ADAPTER, {":slash_tmp": "deny", "/private/var/tmp": "deny",
                                                             ":tmpdir": "write"})):
            self.value["isolation"]["adapter"] = adapter
            self.save()
            policy = self.policy()
            for role in ("author", "structured"):
                with self.subTest(adapter=adapter, role=role):
                    filesystem = retrieval.configuration(policy, role)["permissions"]["historical"]["filesystem"]
                    self.assertEqual({key: value for key, value in filesystem.items()
                                      if key not in (":minimal", ":workspace_roots")}, expected)
                    written = tomllib.loads(retrieval.config_text(policy, role))
                    self.assertEqual(written["permissions"]["historical"]["filesystem"], filesystem)
        self.broker_policy.write_text('{"schema_version":1,"provider":"duckduckgo_wayback"}')
        self.value["broker"]["args"][6] = hashlib.sha256(self.broker_policy.read_bytes()).hexdigest()
        self.save()
        for change, error in (({}, None), ({":slash_tmp": "read"}, "mismatch"), ({"/Users": "read"}, "additional"),
                              ({"/private/var/tmp": None}, "mismatch"), ({":tmpdir": "read"}, "mismatch"),
                              ({":tmpdir": None}, "mismatch"), ({"/private/tmp": "write"}, "additional")):
            with self.subTest(change=change):
                policy, messages, rpc, environment = self.configured_rpc()
                filesystem = messages["config/read"]["config"]["permissions"]["historical"]["filesystem"]
                for key, value in change.items():
                    if value is None:
                        filesystem.pop(key)
                    else:
                        filesystem[key] = value
                with patch.object(retrieval, "_sandbox_canaries"), patch.object(retrieval, "_macos_network_canaries"):
                    if error is None:
                        retrieval.verify_rpc(rpc, policy, "author", environment)
                    else:
                        with self.assertRaisesRegex(retrieval.RetrievalPolicyError, error):
                            retrieval.verify_rpc(rpc, policy, "author", environment)

    def test_verification_accepts_exactly_the_process_private_temp_flags(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.broker_policy.write_text('{"schema_version":1,"provider":"duckduckgo_wayback"}')
        self.value["broker"]["args"][6] = hashlib.sha256(self.broker_policy.read_bytes()).hexdigest()
        self.save()
        policy, messages, rpc, environment = self.configured_rpc()
        temp = Path(environment["TMPDIR"])
        self.assertEqual(retrieval.temp_overrides(policy, environment), {"shell_environment_policy": {"set": {
            "TMPDIR": str(temp), "TMPPREFIX": str(temp / "zsh")}}})
        self.assertEqual(retrieval.temp_arguments(policy, environment), [
            "-c", f"shell_environment_policy.set.TMPDIR={json.dumps(str(temp))}",
            "-c", f"shell_environment_policy.set.TMPPREFIX={json.dumps(str(temp / 'zsh'))}"])
        with patch.object(retrieval, "_sandbox_canaries") as canaries, patch.object(retrieval, "_macos_network_canaries"):
            retrieval.verify_rpc(rpc, policy, "author", environment)
        # The canaries probe this process's own folder.
        self.assertEqual(canaries.call_args.args[3], temp)

        def other_folder(read, environment):
            # Commands that would write their temp files elsewhere.
            for table in (read["config"], read["layers"][0]["config"]):
                table["shell_environment_policy"]["set"].update(TMPDIR="/Users/someone", TMPPREFIX="/tmp/zsh")

        def more_flags(read, environment):
            read["layers"][0]["config"]["sandbox_workspace_write"] = {"network_access": True}

        def inherited_variable(read, environment):
            read["layers"][0]["config"]["shell_environment_policy"]["set"]["OPENAI_API_KEY"] = "secret"

        def effective_only(read, environment):
            read["config"]["shell_environment_policy"]["set"]["TMPPREFIX"] = "/tmp/zsh"

        for change, error in ((other_folder, "mismatch"), (effective_only, "mismatch"),
                              (more_flags, "inherited configuration"), (inherited_variable, "inherited configuration"),
                              (lambda read, environment: environment.pop("TMPDIR"), "private temp folder")):
            with self.subTest(change=change), patch.object(retrieval, "_sandbox_canaries") as canaries, \
                    patch.object(retrieval, "_macos_network_canaries"):
                policy, messages, rpc, environment = self.configured_rpc()
                change(messages["config/read"], environment)
                with self.assertRaisesRegex(retrieval.RetrievalPolicyError, error):
                    retrieval.verify_rpc(rpc, policy, "author", environment)
                canaries.assert_not_called()
        # The author's own app-server also has its search and feature flags; its temp entries stay exact.
        policy, messages, rpc, environment = self.configured_rpc()
        messages["config/read"]["layers"][0]["config"]["features"] = {"multi_agent": True}
        with patch.object(retrieval, "_sandbox_canaries"), patch.object(retrieval, "_macos_network_canaries"):
            retrieval.verify_rpc(rpc, policy, "author", environment, allow_runtime_overrides=True)
            messages["config/read"]["config"]["shell_environment_policy"]["set"]["TMPDIR"] = "/private/tmp"
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "mismatch"):
                retrieval.verify_rpc(rpc, policy, "author", environment, allow_runtime_overrides=True)
        # Windows keeps TEMP and TMP from the system environment: no private temp folder or flags.
        self.value["isolation"]["adapter"] = retrieval.WINDOWS_ADAPTER
        self.save()
        policy = self.policy()
        self.assertIsNone(retrieval.private_temp(policy, "author"))
        self.assertEqual(retrieval.temp_arguments(policy, {"TMPDIR": str(temp)}), [])

    def test_verification_requires_the_fresh_private_temp_folder_of_this_process_and_role(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.broker_policy.write_text('{"schema_version":1,"provider":"duckduckgo_wayback"}')
        self.value["broker"]["args"][6] = hashlib.sha256(self.broker_policy.read_bytes()).hexdigest()
        self.save()
        policy = self.policy()
        own = retrieval.private_temp(policy, "author")
        self.addCleanup(shutil.rmtree, own, True)
        parent = own.parent

        def made(path, mode=0o700, link=None):
            if link is not None:
                path.symlink_to(link)
                self.addCleanup(path.unlink)
            else:
                path.mkdir()
                path.chmod(mode)
                self.addCleanup(shutil.rmtree, path, True)
            return path

        gone = retrieval.private_temp(policy, "author")
        shutil.rmtree(gone)
        not_folder = parent / f"author-{os.getpid()}-afile123"
        not_folder.write_text("x")
        self.addCleanup(not_folder.unlink)
        alias = made(self.root / "alias", link=parent)
        cases = (
            # Not this process's own folder: a violation, found before any request to Codex.
            (parent, retrieval.RetrievalPolicyError),
            (retrieval.private_temp(policy, "structured"), retrieval.RetrievalPolicyError),
            (made(parent / "author-1-abcdefgh"), retrieval.RetrievalPolicyError),
            (made(self.control / f"author-{os.getpid()}-abcdefgh"), retrieval.RetrievalPolicyError),
            (alias / own.name, retrieval.RetrievalPolicyError),
            (made(parent / f"author-{os.getpid()}-linklink", link=self.workspace), retrieval.RetrievalPolicyError),
            # Its own folder no longer owner-only (its commands can chmod it), gone or no folder: checked again.
            (gone, retrieval.IsolationCheckUnavailable),
            (made(parent / f"author-{os.getpid()}-modemode", mode=0o755), retrieval.IsolationCheckUnavailable),
            (not_folder, retrieval.IsolationCheckUnavailable),
        )
        for temp, error in cases:
            with self.subTest(temp=temp):
                policy, messages, rpc, environment = self.configured_rpc()
                # The process's environment, session flags and settings all name ``temp``.
                environment["TMPDIR"] = str(temp)
                overrides = retrieval.temp_overrides(policy, environment)
                messages["config/read"]["config"]["shell_environment_policy"]["set"].update(
                    overrides["shell_environment_policy"]["set"])
                messages["config/read"]["layers"] = [{"name": {"type": "sessionFlags"}, "config": overrides}]
                with patch.object(retrieval, "_sandbox_canaries") as canaries, \
                        patch.object(retrieval, "_macos_network_canaries"):
                    with self.assertRaisesRegex(error, "private temp folder") as caught:
                        retrieval.verify_rpc(rpc, policy, "author", environment)
                    canaries.assert_not_called()
                rpc.call.assert_not_called()
                if error is retrieval.RetrievalPolicyError:
                    self.assertNotIsInstance(caught.exception, retrieval.IsolationCheckUnavailable)
        # Its own fresh folder passes, and the canaries probe exactly that folder.
        policy, messages, rpc, environment = self.configured_rpc()
        with patch.object(retrieval, "_sandbox_canaries") as canaries, patch.object(retrieval, "_macos_network_canaries"):
            retrieval.verify_rpc(rpc, policy, "author", environment)
        self.assertEqual(canaries.call_args.args[3], Path(environment["TMPDIR"]))
        self.assertTrue(Path(environment["TMPDIR"]).name.startswith(f"author-{os.getpid()}-"))

    def test_macos_preflight_rejects_wrong_host_and_script_before_process(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        with patch.object(retrieval.sys, "platform", "linux"), patch.object(retrieval.subprocess, "Popen") as process:
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "macOS Seatbelt"):
                retrieval.preflight(policy, "author", "/bin/sh", {})
            process.assert_not_called()
        with patch.object(retrieval.sys, "platform", "darwin"), patch.object(retrieval.subprocess, "Popen") as process:
            with self.assertRaisesRegex(retrieval.IsolationCheckUnavailable, "Mach-O"):
                retrieval.preflight(policy, "author", str(self.trusted_bin / "codex.exe"), {})
            process.assert_not_called()

    def test_macos_extended_network_probes_require_tools_controls_and_each_denial(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        nonce = 'networknonce'
        probes = retrieval._macos_network_probe_commands(1234, 1235, nonce)
        ready = {name: True for name in ('curlReady', 'ncReady', 'rubyReady', 'dnsReady', 'shellReady')}
        correct = {'nonce': nonce, **ready, **{name: False for name in probes}}
        reports = [correct] + [{**correct, name: not correct[name]} for name in (*ready, *probes)]
        for report in reports:
            rpc = Mock()
            rpc.call.return_value = {'exitCode': 0, 'stdout': json.dumps(report)}
            positive = Mock(returncode=0, stdout=json.dumps({'nonce': nonce, **{name: True for name in probes}}))
            observed = Mock(side_effect=[{'tcp': 9, 'udp': 2}, {'tcp': 10, 'udp': 3}])
            with self.subTest(report=report), patch.object(retrieval.uuid, 'uuid4', return_value=Mock(hex=nonce)), \
                    patch.object(retrieval, '_local_network_controls', return_value=nullcontext((1234, 1235, observed))), \
                    patch.object(retrieval, '_network_observer_barrier'), \
                    patch.object(retrieval.subprocess, 'run', return_value=positive):
                if report == correct:
                    evidence = retrieval._macos_network_canaries(rpc, policy, 'author')
                    self.assertTrue(evidence['positiveControls'])
                    self.assertEqual(set(evidence['networkProbes'].values()), {'denied'})
                elif any(report[name] is False for name in ready):
                    # A probe tool missing in the sandbox proves nothing: checked again.
                    with self.assertRaises(retrieval.IsolationCheckUnavailable):
                        retrieval._macos_network_canaries(rpc, policy, 'author')
                else:
                    with self.assertRaisesRegex(retrieval.RetrievalPolicyError, 'network denial'):
                        retrieval._macos_network_canaries(rpc, policy, 'author')
            request = rpc.call.call_args.args[1]
            self.assertEqual(request['permissionProfile'], 'historical')
            self.assertEqual(request['timeoutMs'], 10000)
            self.assertIn('--disable-gems', request['command'][-1])
            self.assertIn('--resolve', request['command'][-1])
            self.assertIn('socks5h://127.0.0.1:', request['command'][-1])

    def test_macos_observed_contact_fails_even_if_command_reports_denied(self):
        nonce = 'networknonce'
        probes = retrieval._macos_network_probe_commands(1234, 1235, nonce)
        report = {'nonce': nonce, **{name: True for name in ('curlReady', 'ncReady', 'rubyReady', 'dnsReady', 'shellReady')},
                  **{name: False for name in probes}}
        rpc = Mock()
        rpc.call.return_value = {'exitCode': 0, 'stdout': json.dumps(report)}
        positive = Mock(returncode=0, stdout=json.dumps({'nonce': nonce, **{name: True for name in probes}}))
        for kind in ('tcp', 'udp'):
            before = {'tcp': 8, 'udp': 1}
            observed = Mock(side_effect=[before, {name: value + (2 if name == kind else 1) for name, value in before.items()}])
            with self.subTest(kind=kind), patch.object(retrieval.uuid, 'uuid4', return_value=Mock(hex=nonce)), \
                    patch.object(retrieval, '_local_network_controls', return_value=nullcontext((1234, 1235, observed))), \
                    patch.object(retrieval, '_network_observer_barrier'), \
                    patch.object(retrieval.subprocess, 'run', return_value=positive):
                with self.assertRaisesRegex(retrieval.RetrievalPolicyError, 'network denial'):
                    retrieval._macos_network_canaries(rpc, self.policy(), 'author')

    def test_macos_network_positive_failure_stops_before_sandbox_probe(self):
        for result in (Mock(returncode=1, stdout=''), Mock(returncode=0, stdout='{}')):
            rpc = Mock()
            with patch.object(retrieval, '_local_network_controls', return_value=nullcontext((1234, 1235, lambda: {'tcp': 0, 'udp': 0}))), \
                    patch.object(retrieval.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(retrieval.IsolationCheckUnavailable, 'positive controls failed'):
                    retrieval._macos_network_canaries(rpc, self.policy(), 'author')
            rpc.call.assert_not_called()

    def test_native_startup_trust_bookkeeping_is_removed_before_resume(self):
        policy = self.policy()
        environment = retrieval.isolated_environment(policy, "author", {})
        home = Path(environment["CODEX_HOME"])
        saved_thread = home / "sessions" / "saved-thread.jsonl"
        saved_thread.parent.mkdir()
        saved_thread.write_text('{"thread_id":"same-author-thread"}\n')
        auth = home / "auth.json"
        auth.write_text('{"test":"controller-owned-auth-must-not-change"}')
        config = home / "config.toml"
        # Native Codex appends this bookkeeping record when a thread starts.
        persisted = tomllib.loads(config.read_text())
        persisted["projects"] = {str(self.workspace): {"trust_level": "trusted"}}
        config.write_text("\n".join(retrieval._toml_table(persisted)) + "\n")
        thread_before, auth_before = saved_thread.read_bytes(), auth.read_bytes()
        self.assertEqual(retrieval.isolated_environment(policy, "author", {}), environment)
        self.assertEqual(config.read_text(), retrieval.config_text(policy, "author"))
        self.assertEqual(saved_thread.read_bytes(), thread_before)
        self.assertEqual(auth.read_bytes(), auth_before)

    def test_native_trust_identity_uses_common_worktree_root_not_other_projects(self):
        policy = self.policy()
        repository = self.root / "main-repository"
        common = repository / ".git"
        metadata = common / "worktrees" / "isolated"
        metadata.mkdir(parents=True)
        (common / "HEAD").write_text("ref: refs/heads/main\n")
        (metadata / "commondir").write_text("../..\n")
        (self.root / ".git").write_text(f"gitdir: {metadata}\n")
        retrieval.verify_protected_directories(policy, "author", create=True)
        self.assertEqual(retrieval._project_trust_root(policy, "author"), repository)
        actual = retrieval.configuration(policy, "author")
        actual["projects"] = {str(repository): {"trust_level": "trusted"}}
        self.assertTrue(retrieval._validate_user_configuration(policy, "author", actual))
        actual["projects"] = {str(self.root): {"trust_level": "trusted"}}
        with self.assertRaises(retrieval.RetrievalPolicyError):
            retrieval._validate_user_configuration(policy, "author", actual)

    def test_resume_rejects_all_other_config_edits_and_never_migrates_disabled_host(self):
        policy = self.policy()
        environment = retrieval.isolated_environment(policy, "author", {})
        config = Path(environment["CODEX_HOME"]) / "config.toml"
        changes = (
            lambda value: value["features"].update(code_mode_host=False),
            lambda value: value["features"].update(code_mode_host=1),
            lambda value: value["features"].update(view_image=True),
            lambda value: value.update(model_provider="other"),
            lambda value: value["projects"].update({str(self.root): {"trust_level": "trusted"}}),
            lambda value: value["projects"][str(self.workspace)].update(model="other"),
            lambda value: value["permissions"]["historical"]["network"].update(enabled=True),
        )
        for change in changes:
            actual = retrieval.configuration(policy, "author")
            actual["projects"] = {str(self.workspace): {"trust_level": "trusted"}}
            change(actual)
            config.write_text("\n".join(retrieval._toml_table(actual)))
            before = config.read_bytes()
            with self.subTest(change=change), self.assertRaisesRegex(retrieval.RetrievalPolicyError, "configuration has changed"):
                retrieval.isolated_environment(policy, "author", {})
            self.assertEqual(config.read_bytes(), before)

    def test_allowed_native_trust_never_allows_inherited_project_layers(self):
        policy, messages, rpc, environment = self.configured_rpc()
        raw = copy.deepcopy(messages["config/read"]["config"])
        raw["mcp_servers"][retrieval.BROKER_NAME]["tool_timeout_sec"] = 600.0
        raw["projects"] = {str(self.workspace): {"trust_level": "trusted"}}
        layers = [{"name": {"type": "user", "file": str(retrieval.role_home(policy, "author") / "config.toml")}, "config": raw}]
        messages["config/read"]["layers"] = layers
        with patch.object(retrieval, "_sandbox_canaries") as canaries:
            retrieval.verify_rpc(rpc, policy, "author", environment)
            canaries.assert_called_once()
        layers.append({"name": {"type": "project", "dotCodexFolder": str(self.root / ".codex")},
                       "config": {"developer_instructions": "inherited data"}})
        with patch.object(retrieval, "_sandbox_canaries") as canaries:
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, "inherited configuration layers"):
                retrieval.verify_rpc(rpc, policy, "author", environment)
            canaries.assert_not_called()

    def test_macos_preflight_requires_native_code_mode_host_before_process(self):
        self.value["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        self.save()
        policy = self.policy()
        executable = self.trusted_bin / "codex"
        executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"not executed")
        executable.chmod(0o755)
        with patch.object(retrieval.sys, "platform", "darwin"), patch.object(retrieval.subprocess, "Popen") as process:
            with self.assertRaisesRegex(retrieval.IsolationCheckUnavailable, "codex-code-mode-host"):
                retrieval.preflight(policy, "author", str(executable), {})
            process.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "native macOS code-mode host probe")
    def test_native_code_mode_host_runs_js_without_filesystem_network_or_undeclared_tools(self):
        policy = self.policy()
        policy["isolation"]["adapter"] = retrieval.MACOS_ADAPTER
        try:
            host = Path(retrieval.codex_executable(policy)).with_name("codex-code-mode-host")
        except (retrieval.RetrievalPolicyError, retrieval.IsolationCheckUnavailable):
            self.skipTest("native Codex is not installed")
        if not host.is_file():
            self.skipTest("native code-mode host is not installed")
        # Exercise the real V8 host without a model, credentials, or enabled
        # nested tools. IPC v1 uses length-prefixed JSON on private stdio pipes.
        process = subprocess.Popen([str(host)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={"PATH": "/usr/bin:/bin"})
        messages = queue.Queue()

        def receive():
            try:
                while header := process.stdout.read(4):
                    length = struct.unpack("<I", header)[0]
                    if length > 1024 * 1024:
                        raise ValueError("unexpectedly large host response")
                    messages.put(json.loads(process.stdout.read(length)))
            except Exception as exc:
                messages.put(exc)
            finally:
                messages.put(None)

        def send(message):
            encoded = json.dumps(message).encode()
            process.stdin.write(struct.pack("<I", len(encoded)) + encoded)
            process.stdin.flush()

        def next_message():
            message = messages.get(timeout=5)
            self.assertIsInstance(message, dict)
            return message

        threading.Thread(target=receive, daemon=True).start()
        try:
            send({"type": "connection/hello", "supportedVersions": [1],
                  "requiredCapabilities": [], "optionalCapabilities": []})
            self.assertEqual(next_message()["type"], "connection/ready")
            send({"type": "operation/request", "id": 1,
                  "request": {"method": "session/open", "sessionId": "isolation-canary"}})
            self.assertEqual(next_message()["result"]["value"]["type"], "session/ready")
            source = """
const globals = {};
for (const name of ['require', 'process', 'fetch', 'XMLHttpRequest', 'WebSocket', 'Deno', 'Bun']) {
    globals[name] = typeof globalThis[name];
}
const imports = {};
for (const module of ['node:fs', 'node:net', 'file:///etc/passwd', 'https://example.invalid/']) {
    try { await import(module); imports[module] = true; } catch { imports[module] = false; }
}
let undeclaredTool = false;
try { await tools.unconfigured_tool({}); undeclaredTool = true; } catch {}
text({globals, imports, tools: Object.keys(tools), undeclaredTool});
"""
            send({"type": "operation/request", "id": 2, "request": {
                "method": "session/execute", "sessionId": "isolation-canary",
                "request": {"tool_call_id": "canary", "enabled_tools": [], "source": source}}})
            self.assertEqual(next_message()["result"]["value"]["type"], "execution/started")
            response = next_message()
            self.assertEqual(response["type"], "execute/initialResponse")
            result = response["result"]["value"]["Result"]
            self.assertIsNone(result["error_text"])
            report = json.loads(result["content_items"][0]["text"])
            self.assertTrue(all(value == "undefined" for value in report["globals"].values()))
            self.assertTrue(all(value is False for value in report["imports"].values()))
            self.assertEqual(report["tools"], [])
            self.assertFalse(report["undeclaredTool"])
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
