"""OpenAI-format API models through Codex's isolated custom provider; no network or model calls."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
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
from runner import confinement, providers
import test_goal_runtime as goal_test

KEY = "sk-test-codex-gateway-0123456789"


class Launched(Exception):
    pass


class ApiCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.workspace = self.root / "run"
        self.workspace.mkdir()
        environment = mock.patch.dict(os.environ, {
            providers.STORE_ENV: str(self.root / "state" / "api-models.json"),
            "TCS_PROVER_API_TOKEN": "stray-parent-token", "DEEPSEEK_API_KEY": "old-deepseek-key",
            "OPENAI_API_KEY": "parent-openai-key", "CODEX_HOME": str(self.root / "user-codex-home"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        os.environ.pop("TCS_PROVER_RETRIEVAL_MODE", None)
        previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)
        # No real Codex ever starts here; tests that need the resolver call it explicitly.
        resolver = mock.patch.object(runtime, "api_codex_executable", return_value="codex-isolated")
        self.resolver = resolver.start()
        self.addCleanup(resolver.stop)

    def model(self, header="Authorization", label="Team gateway", base="https://api.example.com/v1/", source=""):
        return providers.save_api_model({"format": "openai", "baseUrl": base, "headerName": header,
                                         "model": "gpt-gateway-x", "apiKey": KEY, "label": label, "requestSource": source})["id"]

    @staticmethod
    def overrides(arguments):
        values = [arguments[index + 1] for index, value in enumerate(arguments) if value == "-c"]
        return [value.split("=", 1) for value in values]

    def captured(self, launch):
        calls = []

        def popen(command, **kwargs):
            calls.append((command, kwargs))
            raise Launched

        with mock.patch.object(subprocess, "Popen", side_effect=popen), mock.patch.object(runtime, "codex", return_value="codex"):
            with contextlib.suppress(Launched):
                launch()
        self.assertTrue(calls, "No process was launched.")
        return calls[0]


class ProviderArgumentTests(ApiCase):
    def test_authorization_header_uses_the_bearer_environment_key(self):
        arguments = runtime.provider_arguments(self.model())
        self.assertEqual(arguments, [
            "-c", 'model_provider="tcs_api"',
            "-c", 'model_providers.tcs_api.name="TCS Prover"',
            "-c", 'model_providers.tcs_api.base_url="https://api.example.com/v1"',
            "-c", 'model_providers.tcs_api.wire_api="responses"',
            "-c", "model_providers.tcs_api.supports_websockets=false",
            "-c", "model_providers.tcs_api.requires_openai_auth=false",
            "-c", 'model_providers.tcs_api.env_key="TCS_PROVER_API_TOKEN"',
            "-c", 'model_providers.tcs_api.http_headers={"User-Agent"="TCS-Prover"}',
            # The model's shell commands never inherit the key or the placeholder.
            "-c", "shell_environment_policy.ignore_default_excludes=false",
            "-c", 'shell_environment_policy.exclude=["TCS_PROVER_API_TOKEN","OPENAI_API_KEY"]',
        ])
        self.assertNotIn("preferred_auth_method", " ".join(arguments))  # Unrecognized by Codex 0.160.
        # Codex enforces a forced login method by deleting a visible ChatGPT login.
        self.assertNotIn("forced_login_method", " ".join(arguments))
        self.assertNotIn("model_catalog_json", " ".join(arguments))
        self.assertNotIn(KEY, " ".join(arguments))

    def test_legacy_custom_headers_still_use_automatic_bearer_authentication(self):
        for header in ("authorization", "X-Api-Key", "Zephy-TCS-Prover"):
            arguments = runtime.provider_arguments(self.model(header=header))
            self.assertIn('model_providers.tcs_api.env_key="TCS_PROVER_API_TOKEN"', arguments)
            self.assertNotIn("env_http_headers", " ".join(arguments))
            self.assertIn('shell_environment_policy.exclude=["TCS_PROVER_API_TOKEN","OPENAI_API_KEY"]', arguments)

    @unittest.skipIf(tomllib is None, "tomllib is needed to parse Codex overrides")
    def test_request_source_is_quoted_and_local_display_label_is_not_transmitted(self):
        label = 'Quote " back\\slash ünïcode {braces} = sign'
        source = 'Zephy " research\\client {test} = source'
        arguments = runtime.provider_arguments(self.model(label=label, source=source))
        parsed = {}
        for key, value in self.overrides(arguments):
            parsed[key] = tomllib.loads(f"value = {value}")["value"]
        self.assertEqual(parsed["model_providers.tcs_api.name"], "TCS Prover")
        self.assertNotIn(label, " ".join(arguments))
        self.assertEqual(parsed["model_providers.tcs_api.base_url"], "https://api.example.com/v1")
        self.assertEqual(parsed["model_providers.tcs_api.http_headers"], {"User-Agent": source})
        self.assertIs(parsed["model_providers.tcs_api.supports_websockets"], False)
        self.assertIs(parsed["shell_environment_policy.ignore_default_excludes"], False)
        self.assertEqual(parsed["shell_environment_policy.exclude"], ["TCS_PROVER_API_TOKEN", "OPENAI_API_KEY"])
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            profile = runtime.private_permission_arguments(":read-only", self.model(label="Other"))
        overrides = dict((key, tomllib.loads(f"value = {value}")["value"]) for key, value in self.overrides(profile))
        self.assertEqual(set(overrides), {"default_permissions", f"permissions.{runtime.PRIVATE_PERMISSIONS}.filesystem"})
        self.assertEqual(overrides["default_permissions"], runtime.PRIVATE_PERMISSIONS)
        filesystem = overrides[f"permissions.{runtime.PRIVATE_PERMISSIONS}.filesystem"]
        self.assertEqual(filesystem[":workspace_roots"]["."], "read")
        for path in (*providers.private_paths(), *providers.user_login_files()):
            self.assertEqual(filesystem[str(path)], "deny")
        # Labels with control characters are refused by the store; quoting still escapes them.
        self.assertEqual(tomllib.loads("value = " + runtime.toml_string("tab\t del\x7f"))["value"], "tab\t del\x7f")

    def test_native_and_claude_models_define_no_custom_provider(self):
        for model in ("gpt-6.1-sol", "gpt-5.6-terra", "claude-opus-5-5"):
            self.assertEqual(runtime.provider_arguments(model), [])
        anthropic = providers.save_api_model({"format": "anthropic", "baseUrl": "https://gw.example.com",
                                              "model": "claude-x", "apiKey": KEY})["id"]
        self.assertEqual(runtime.provider_arguments(anthropic), [])

    def test_provider_ids_speed_and_schema_contract(self):
        model = self.model()
        self.assertEqual((runtime.model_provider(model), runtime.model_provider("claude-opus-5-5"),
                          runtime.model_provider("gpt-6.1-sol")), ("tcs_api", "claude", "openai"))
        self.assertEqual(runtime.effective_speed(model, "fast"), "standard")
        self.assertEqual(runtime.effective_speed("claude-opus-5-5", "fast"), "standard")
        self.assertEqual(runtime.effective_speed("gpt-6.1-sol", "fast"), "fast")
        self.assertEqual(runtime.speed_arguments("fast", model), ["--disable", "fast_mode"])
        self.assertEqual(runtime.output_schema_arguments(model, "schema.json"), [])
        self.assertEqual(runtime.output_schema_arguments("gpt-6.1-sol", "schema.json"), ["--output-schema", "schema.json"])
        schema = {"type": "object"}
        self.assertIn("OUTPUT JSON CONTRACT", runtime.structured_prompt_for_model("Task", schema, model))
        for enforced in ("gpt-6.1-sol", "claude-opus-5-5"):
            self.assertEqual(runtime.structured_prompt_for_model("Task", schema, enforced), "Task")


class EnvironmentTests(ApiCase):
    def test_the_key_reaches_only_the_child_environment_with_a_private_codex_home(self):
        model = self.model()
        environment = runtime.environment(model)
        self.assertEqual(environment["TCS_PROVER_API_TOKEN"], KEY)
        self.assertEqual(environment["OPENAI_API_KEY"], runtime.CUSTOM_PROVIDER_LOGIN_PLACEHOLDER)
        self.assertNotIn("DEEPSEEK_API_KEY", environment)
        home = Path(environment["CODEX_HOME"])
        self.assertNotEqual(home, self.root / "user-codex-home")
        self.assertEqual(home.parent.parent, providers.state_directory())
        self.assertEqual(home, runtime.api_codex_home(self.workspace))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(runtime.api_codex_home(self.workspace), home)  # Stable per workspace.
        self.assertNotEqual(runtime.api_codex_home(self.root), home)

    def test_native_models_keep_the_user_login_and_drop_stray_credentials(self):
        environment = runtime.environment("gpt-6.1-sol")
        self.assertEqual(environment["CODEX_HOME"], str(self.root / "user-codex-home"))
        for name in ("TCS_PROVER_API_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            self.assertNotIn(name, environment)
        self.assertNotIn("TCS_PROVER_API_TOKEN", runtime.environment("claude-opus-5-5"))

    def test_credentials_are_required_before_launch(self):
        model = self.model()
        runtime.require_model_credentials(model)
        store = Path(os.environ[providers.STORE_ENV])
        value = json.loads(store.read_text())
        value["models"][0]["apiKey"] = ""
        store.write_text(json.dumps(value))
        with self.assertRaisesRegex(runtime.Error, "Add an API key for Team gateway"):
            runtime.require_model_credentials(model)
        with mock.patch.object(runtime.shutil, "which", return_value=None):
            with self.assertRaisesRegex(runtime.Error, "Claude CLI is not installed"):
                runtime.verify_model_credentials("claude-haiku-5-5")
        with self.assertRaisesRegex(runtime.Error, "Unknown model"):
            runtime.verify_model_credentials("api:deleted")

    def test_historical_isolation_rejects_api_and_claude_models(self):
        for model in (self.model(), "claude-opus-5-5"):
            with self.assertRaisesRegex(runtime.Error, "Web cutoff timestamps require Codex CLI models"):
                runtime.retrieval_environment({"isolation": {}}, "structured", model)


class StructuredProcessTests(ApiCase):
    def test_structured_command_uses_the_wire_model_without_schema_enforcement(self):
        model = self.model(header="X-Gateway-Key")
        command, kwargs = self.captured(lambda: runtime.run_structured_attempt(
            "Prompt", {"type": "object"}, "critic", model, "max", "fast", "concise"))
        self.assertEqual(command[0], "codex-isolated")
        self.assertEqual(Path(self.resolver.call_args.args[0]["CODEX_HOME"]), runtime.api_codex_home(self.workspace))
        self.assertEqual(command[command.index("-m") + 1], "gpt-gateway-x")
        self.assertNotIn("--output-schema", command)
        self.assertIn(("--disable", "fast_mode"), list(zip(command, command[1:])))
        self.assertNotIn('service_tier="fast"', command)
        self.assertIn('model_provider="tcs_api"', command)
        self.assertNotIn(KEY, json.dumps(command))
        environment = kwargs["env"]
        self.assertEqual(environment["TCS_PROVER_API_TOKEN"], KEY)
        self.assertEqual(Path(environment["CODEX_HOME"]), runtime.api_codex_home(self.workspace))

    def test_structured_requests_append_the_contract_and_validate(self):
        model = self.model()
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=['Done. {"ok": true}']) as attempt, \
                mock.patch.object(runtime, "emit") as emit:
            result, _ = runtime.structured("Check.", schema, "review", model=model, effort="ultra", speed="fast")
        self.assertEqual(result, {"ok": True})
        prompt = attempt.call_args.args[0]
        self.assertIn("OUTPUT JSON CONTRACT", prompt)
        self.assertEqual(attempt.call_args.args[4], "max")
        request = next(call.kwargs for call in emit.call_args_list if call.args[0] == "request")
        self.assertEqual((request["modelProvider"], request["serviceTier"], request["reasoningEffort"]),
                         ("tcs_api", "standard", "max"))

    def test_codex_sandboxes_hide_private_state_on_macos(self):
        model = self.model()
        for platform, expected in (("darwin", True), ("linux", False), ("win32", False)):
            with self.subTest(platform=platform), mock.patch.object(runtime.sys, "platform", platform):
                command, _ = self.captured(lambda: runtime.run_structured_attempt(
                    "Prompt", {"type": "object"}, "critic", model, "max", "standard", "concise"))
                overrides = [command[index + 1] for index, value in enumerate(command) if value == "-c"]
                if expected:
                    # "-s read-only" would override the profile, so it is absent.
                    self.assertNotIn("-s", command)
                    self.assertIn(f'default_permissions="{runtime.PRIVATE_PERMISSIONS}"', overrides)
                    self.assertFalse([value for value in overrides if ".extends=" in value])
                    # A live run (without saved settings) reaches the internet.
                    self.assertIn(f'permissions.{runtime.PRIVATE_PERMISSIONS}.network={{"enabled"=true}}', overrides)
                    denied = next(value for value in overrides if value.startswith(
                        f"permissions.{runtime.PRIVATE_PERMISSIONS}.filesystem="))
                    for path in (*providers.private_paths(), *providers.user_login_files()):
                        self.assertIn(json.dumps(str(path)) + '="deny"', denied)
                    # The API model's private home holds the apply_patch helpers.
                    arg0 = os.path.join(os.path.realpath(runtime.api_codex_home(self.workspace)), "tmp", "arg0")
                    self.assertIn(json.dumps(arg0) + '="read"', denied)
                    self.assertNotIn(json.dumps(os.path.join(os.path.realpath(Path.home() / ".codex"), "tmp", "arg0")),
                                     denied)
                else:
                    self.assertIn(("-s", "read-only"), list(zip(command, command[1:])))
                    self.assertFalse([value for value in overrides if "permissions" in value])
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            native = runtime.private_permissions(":workspace", "gpt-6.1-sol")
            auditor = runtime.private_permissions(":read-only", None)
            elsewhere = runtime.private_permissions(":workspace", "gpt-6.1-sol", codex_home=self.root / "other-home")
        # No built-in profile underneath: the system runtime, the toolchains and Codex's own programs
        # are readable, /tmp and /var/tmp are closed again, and only the private temp folder
        # ($TMPDIR) and the workspace are writable. Native models keep their own login; only
        # TCS Prover's private state is hidden. Codex's project folders and the controller's
        # files in the run folder stay read-only.
        protected = {".codex": "read", ".agents": "read", ".git": "read", ".aws": "read",
                     "job-settings.json": "read", "pause.json": "read",
                     "checked-statement.md": "read", "prompts": "read", "related-works": "read"}
        common = {
            ":minimal": "read", ":slash_tmp": "deny", "/private/var/tmp": "deny", ":tmpdir": "write",
            **{str(path): "read" for path in (*confinement.toolchain_folders(), *confinement.codex_packages())},
            os.path.join(os.path.realpath(Path.home() / ".codex"), "tmp", "arg0"): "read",
            **{str(path): "deny" for path in providers.private_paths()}}
        self.assertEqual(native, {"filesystem": {**common, ":workspace_roots": {".": "write", **protected}}})
        # None is a native model (statement reviewer); read-only keeps the same reads.
        self.assertEqual(auditor, {"filesystem": {**common, ":workspace_roots": {".": "read", **protected}}})
        self.assertEqual(elsewhere["filesystem"][os.path.join(os.path.realpath(self.root / "other-home"), "tmp", "arg0")],
                         "read")

    def test_network_is_the_only_difference_of_a_live_profile(self):
        private = providers.private_paths()
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            for base in (":workspace", ":read-only"):
                live = runtime.private_permissions(base, "gpt-6.1-sol", live=True)
                offline = runtime.private_permissions(base, "gpt-6.1-sol")
                with self.subTest(base=base):
                    self.assertEqual(live, {**offline, "network": {"enabled": True}})
                    self.assertNotIn("network", offline)
                    filesystem = live["filesystem"]
                    self.assertEqual((filesystem[":slash_tmp"], filesystem["/private/var/tmp"], filesystem[":tmpdir"]),
                                     ("deny", "deny", "write"))
                    self.assertEqual(filesystem[str(private[0])], "deny")
            # The user's [sandbox_workspace_write] settings are no input: neither their network
            # access nor their writable roots and /tmp settings reach the profile.
            with self.assertRaises(TypeError):
                runtime.private_permissions(":workspace", "gpt-6.1-sol", {"network_access": True})

    @unittest.skipIf(tomllib is None, "tomllib is needed to parse Codex overrides")
    def test_profile_flags_render_nested_tables_as_toml(self):
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            expected = runtime.private_permissions(":workspace", "gpt-6.1-sol")
            arguments = runtime.private_permission_arguments(":workspace", "gpt-6.1-sol")
        overrides = dict((key, tomllib.loads(f"value = {value}")["value"]) for key, value in self.overrides(arguments))
        self.assertEqual(overrides[f"permissions.{runtime.PRIVATE_PERMISSIONS}.filesystem"], expected["filesystem"])

    @unittest.skipIf(os.name == "nt", "Uses a POSIX shell script as the stand-in Codex CLI")
    def test_records_never_carry_the_key_a_gateway_or_command_echoes(self):
        model = self.model()
        fake = self.root / "codex"
        fake.write_text(
            "#!/bin/sh\ncat > /dev/null\n"
            "printf '{\"type\":\"error\",\"message\":\"Reconnecting... 1/5 (401: Invalid API key provided: %s)\"}\\n' \"$TCS_PROVER_API_TOKEN\"\n"
            "printf '{\"type\":\"item.completed\",\"item\":{\"type\":\"command_execution\",\"aggregated_output\":\"TOKEN=%s\"}}\\n' \"$TCS_PROVER_API_TOKEN\"\n"
            "printf '{\"type\":\"turn.failed\",\"error\":{\"message\":\"401: Invalid API key provided: %s\"}}\\n' \"$TCS_PROVER_API_TOKEN\"\n"
            "exit 1\n")
        fake.chmod(0o700)
        (self.root / "workspace").mkdir()
        self.resolver.return_value = str(fake)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(runtime.Error) as failure:
            runtime._run_structured_process(
                "Prompt", {"type": "object"}, "critic", model, "max", "standard", "concise", timeout=60,
                activity_label=None, features=(), policy=None, child_environment=runtime.environment(model),
                workspace=self.root / "workspace")
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["event"]["type"] for record in records if record["kind"] == "codex_event"],
                         ["error", "item.completed", "turn.failed"])
        self.assertNotIn(KEY, output.getvalue())
        self.assertIn("TOKEN=[redacted]", output.getvalue())
        self.assertNotIn(KEY, str(failure.exception))

    @unittest.skipIf(os.name == "nt", "Uses a POSIX shell script as the stand-in Codex CLI")
    def test_failure_details_never_contain_the_key(self):
        model = self.model()
        fake = self.root / "codex"
        fake.write_text('#!/bin/sh\necho "auth failed for $TCS_PROVER_API_TOKEN" >&2\nexit 2\n')
        fake.chmod(0o700)
        (self.root / "workspace").mkdir()
        self.resolver.return_value = str(fake)
        with mock.patch.object(runtime, "emit"):
            with self.assertRaises(runtime.Error) as failure:
                runtime._run_structured_process(
                    "Prompt", {"type": "object"}, "critic", model, "max", "standard", "concise", timeout=60,
                    activity_label=None, features=(), policy=None, child_environment=runtime.environment(model),
                    workspace=self.root / "workspace")
        self.assertIn("auth failed for [redacted]", str(failure.exception))
        self.assertNotIn(KEY, str(failure.exception))


class GoalSessionTests(ApiCase):
    def test_app_server_thread_uses_the_custom_provider_and_private_home(self):
        model = self.model()

        class Double(goal_test.Runtime):
            API_PROVIDER = runtime.API_PROVIDER
            api_codex_executable = staticmethod(lambda environment: "codex-isolated")

            def RPC(self, process, record):
                rpc = super().RPC(process, record)
                call = rpc.call
                home = str(runtime.api_codex_home(goal_cwd))
                rpc.call = lambda method, params: ({**call(method, params), "codexHome": home}
                                                   if method == "initialize" else call(method, params))
                return rpc

            environment = staticmethod(runtime.environment)
            provider_arguments = staticmethod(runtime.provider_arguments)
            speed_arguments = staticmethod(runtime.speed_arguments)
            effective_speed = staticmethod(runtime.effective_speed)
            model_provider = staticmethod(runtime.model_provider)
            wire_model = staticmethod(runtime.wire_model)
            api_codex_home = staticmethod(runtime.api_codex_home)

        double = Double([goal_test.success()])
        goal_cwd = self.root / "goal-workspace"
        goal_cwd.mkdir()
        with mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()) as spawn:
            session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                           settings={**goal_test.SETTINGS, "model": model, "speed": "fast"},
                                           options={"goal_cwd": str(goal_cwd)})
            self.addCleanup(session.close)
            self.assertEqual(next(session)["outcome"], "done")
        command, kwargs = spawn.call_args.args[0], spawn.call_args.kwargs
        self.assertEqual(command[:2], ["codex-isolated", "app-server"])
        self.assertIn('model_provider="tcs_api"', command)
        self.assertIn(("--disable", "fast_mode"), list(zip(command, command[1:])))
        self.assertNotIn(KEY, json.dumps(command))
        self.assertEqual(kwargs["env"]["TCS_PROVER_API_TOKEN"], KEY)
        self.assertEqual(Path(kwargs["env"]["CODEX_HOME"]), runtime.api_codex_home(goal_cwd))
        start = next(params for method, params in double.rpc.calls if method == "thread/start")
        self.assertEqual((start["model"], start["modelProvider"]), ("gpt-gateway-x", "tcs_api"))
        self.assertFalse(start["config"]["features"]["fast_mode"])

    @unittest.skipIf(tomllib is None, "tomllib is needed to parse Codex overrides")
    def test_app_server_author_runs_in_the_confined_profile(self):
        model = self.model()
        goal_cwd = self.root / "goal-workspace"
        goal_cwd.mkdir()
        run_tmp = self.root / "controller" / "run" / "tmp"
        run_tmp.parent.mkdir(parents=True)

        class Double(goal_test.Runtime):
            API_PROVIDER = runtime.API_PROVIDER
            api_codex_executable = staticmethod(lambda environment: "codex-isolated")
            environment = staticmethod(runtime.environment)
            provider_arguments = staticmethod(runtime.provider_arguments)
            model_provider = staticmethod(runtime.model_provider)
            wire_model = staticmethod(runtime.wire_model)
            api_codex_home = staticmethod(runtime.api_codex_home)
            private_permissions = staticmethod(runtime.private_permissions)

            def RPC(self, process, record):
                rpc = super().RPC(process, record)
                call = rpc.call
                home = str(runtime.api_codex_home(goal_cwd))
                rpc.call = lambda method, params: ({**call(method, params), "codexHome": home}
                                                   if method == "initialize" else call(method, params))
                return rpc

        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), mock.patch.object(runtime.sys, "platform", platform):
                double = Double([goal_test.success()])
                with mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()) as spawn:
                    session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                                   settings={**goal_test.SETTINGS, "model": model},
                                                   options={"goal_cwd": str(goal_cwd), "run_tmp_dir": str(run_tmp)})
                    self.addCleanup(session.close)
                    self.assertEqual(next(session)["outcome"], "done")
                start = next(params for method, params in double.rpc.calls if method == "thread/start")
                argv, environment = spawn.call_args.args[0], spawn.call_args.kwargs["env"]
                # Every platform: the user's apps and plugins are off, and so is image viewing.
                self.assertEqual(start["config"]["apps"], {"_default": {"enabled": False}})
                self.assertEqual({key: start["config"]["features"][key] for key in ("apps", "plugins", "view_image")},
                                 {"apps": False, "plugins": False, "view_image": False})
                if platform == "darwin":
                    self.assertNotIn("sandbox", start)
                    self.assertEqual(start["permissions"], runtime.PRIVATE_PERMISSIONS)
                    self.assertEqual(start["config"]["default_permissions"], runtime.PRIVATE_PERMISSIONS)
                    profile = start["config"]["permissions"][runtime.PRIVATE_PERMISSIONS]
                    self.assertNotIn("extends", profile)
                    filesystem = profile["filesystem"]
                    for path in (*providers.private_paths(), *providers.user_login_files()):
                        self.assertEqual(filesystem[str(path)], "deny")
                    self.assertEqual((filesystem[":minimal"], filesystem[":slash_tmp"], filesystem["/private/var/tmp"],
                                      filesystem[":tmpdir"]), ("read", "deny", "deny", "write"))
                    # The private home of the API model holds the apply_patch helpers.
                    home = os.path.realpath(runtime.api_codex_home(goal_cwd))
                    self.assertEqual(filesystem[os.path.join(home, "tmp", "arg0")], "read")
                    # The run folder cannot create the project folders Codex trusts (.codex, ...).
                    self.assertEqual(filesystem[":workspace_roots"],
                                     {".": "write", ".codex": "read", ".agents": "read", ".git": "read", ".aws": "read",
                                      "job-settings.json": "read", "pause.json": "read",
                                      "checked-statement.md": "read", "prompts": "read", "related-works": "read"})
                    # The app-server defines the same profile, so the startup check can use it.
                    overrides = dict(self.overrides(argv))
                    self.assertEqual(overrides["default_permissions"], f'"{runtime.PRIVATE_PERMISSIONS}"')
                    self.assertEqual(tomllib.loads("value = " + overrides[
                        f"permissions.{runtime.PRIVATE_PERMISSIONS}.filesystem"])["value"], filesystem)
                    # Its private temp folder: the run's, owner-only and outside the workspace.
                    temp = os.path.realpath(run_tmp)
                    self.assertEqual((environment["TMPDIR"], environment["TMPPREFIX"], environment["GIT_CONFIG_GLOBAL"]),
                                     (temp, os.path.join(temp, "zsh"), os.devnull))
                    self.assertEqual(stat.S_IMODE(os.stat(temp).st_mode), 0o700)
                else:
                    self.assertEqual(start["sandbox"], "workspace-write")
                    self.assertNotIn("permissions", start)
                    self.assertFalse([value for value in argv if "permissions" in value])
                    self.assertNotIn("TMPPREFIX", environment)
                # The shell of the author never inherits the key either.
                self.assertIn('shell_environment_policy.exclude=["TCS_PROVER_API_TOKEN","OPENAI_API_KEY"]', argv)

    def test_native_author_takes_none_of_the_users_workspace_write_settings(self):
        extra = str(self.root / "extra-root")
        legacy = {"writable_roots": [extra], "network_access": True,
                  "exclude_tmpdir_env_var": True, "exclude_slash_tmp": False}

        class Double(goal_test.Runtime):
            Error = runtime.Error  # A failed attempt is retried as with the real runner.
            private_permissions = staticmethod(runtime.private_permissions)
            config_error = None

            def RPC(self, process, record):
                rpc = super().RPC(process, record)
                call = rpc.call

                def answer(method, params):
                    if method != "config/read":
                        return call(method, params)
                    rpc.calls.append((method, params))
                    if self.config_error:
                        raise runtime.RPCError(self.config_error)
                    return {"config": {"sandbox_workspace_write": legacy, "plugins": {"slack@openai-curated": {
                        "enabled": True}}, "mcp_servers": {"node_repl": {"command": "node_repl", "enabled": True}},
                        "apps": {"calendar": {"enabled": True}}}, "origins": {}}
                rpc.call = answer
                return rpc

        for platform, failure, mode in (("darwin", None, "live"), ("darwin", None, "disabled"),
                                        ("darwin", "config/read failed: unsupported", "live"), ("linux", None, "live"),
                                        ("linux", None, "disabled")):
            with self.subTest(platform=platform, failure=failure, mode=mode), \
                    mock.patch.object(runtime.sys, "platform", platform), \
                    mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": mode}):
                double = Double([goal_test.success()])
                double.config_error = failure
                with mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()) as spawn:
                    session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                                   settings={**goal_test.SETTINGS, "model": "gpt-6.1-sol"},
                                                   options={"goal_cwd": str(self.workspace)})
                    self.addCleanup(session.close)
                    if failure:
                        # Without the settings the user's MCP servers would load: retried, never started.
                        with self.assertRaises(runtime.WorkflowPaused) as paused:
                            next(session)
                        self.assertIn("Could not read Codex's settings", paused.exception.reason)
                        self.assertIsNotNone(paused.exception.retry)
                        self.assertFalse(any(method in {"thread/start", "turn/start"}
                                             for method, _ in double.rpc.calls))
                        continue
                    self.assertEqual(next(session)["outcome"], "done")
                start = next(params for method, params in double.rpc.calls if method == "thread/start")
                reads = [params for method, params in double.rpc.calls if method == "config/read"]
                self.assertEqual(reads, [{"cwd": str(self.workspace)}])
                # Each MCP server, plugin and app the settings name is turned off by name.
                self.assertEqual((start["config"]["mcp_servers"], start["config"]["plugins"], start["config"]["apps"]),
                                 ({"node_repl": {"enabled": False}}, {"slack@openai-curated": {"enabled": False}},
                                  {"_default": {"enabled": False}, "calendar": {"enabled": False}}))
                live = mode == "live"
                if platform != "darwin":
                    # The legacy sandbox's network follows the search mode, not network_access = true.
                    self.assertEqual(start["sandbox"], "workspace-write")
                    self.assertIn(f"sandbox_workspace_write.network_access={str(live).lower()}",
                                  spawn.call_args.args[0])
                    continue
                profile = start["config"]["permissions"][runtime.PRIVATE_PERMISSIONS]
                # A live run reaches the internet; a web cutoff of -1 does not, whatever network_access says.
                self.assertEqual(profile.get("network"), {"enabled": True} if live else None)
                self.assertNotIn(extra, profile["filesystem"])
                self.assertEqual((profile["filesystem"][":tmpdir"], profile["filesystem"][":slash_tmp"]),
                                 ("write", "deny"))
                for path in providers.private_paths():
                    self.assertEqual(profile["filesystem"][str(path)], "deny")

    def test_app_server_that_reports_another_home_is_stopped_before_any_thread(self):
        model = self.model()

        class Double(goal_test.Runtime):
            API_PROVIDER = runtime.API_PROVIDER
            environment = staticmethod(runtime.environment)
            model_provider = staticmethod(runtime.model_provider)
            api_codex_home = staticmethod(runtime.api_codex_home)
            api_codex_executable = staticmethod(lambda environment: "codex-isolated")

            def RPC(self, process, record):
                rpc = super().RPC(process, record)
                call = rpc.call
                rpc.call = lambda method, params: ({**call(method, params), "codexHome": "/Users/someone/.codex"}
                                                   if method == "initialize" else call(method, params))
                return rpc

        double = Double([goal_test.success()])
        with mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()):
            session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                           settings={**goal_test.SETTINGS, "model": model},
                                           options={"goal_cwd": str(self.workspace)})
            self.addCleanup(session.close)
            with self.assertRaises(runtime.WorkflowPaused) as paused:
                next(session)
        self.assertIn("Codex did not use the private home of this API model", paused.exception.reason)
        self.assertFalse(any(method in {"thread/start", "thread/resume", "turn/start"} for method, _ in double.rpc.calls))


@unittest.skipIf(os.name == "nt", "Uses POSIX scripts as stand-in Codex CLIs")
class IsolatedExecutableTests(unittest.TestCase):
    """A launcher that replaces CODEX_HOME must never run an API model."""

    SERVER = ("#!{python}\nimport json, os, sys\nsys.stdin.readline()\n"
              "print(json.dumps({{'id': 1, 'result': {{'codexHome': {home}}}}}), flush=True)\nsys.stdin.read()\n")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.home = self.root / "private-home"
        self.home.mkdir()
        self.environment = {**os.environ, "CODEX_HOME": str(self.home)}
        runtime._ISOLATED_CODEX.clear()
        self.addCleanup(runtime._ISOLATED_CODEX.clear)

    def stand_in(self, name, home):
        path = self.root / name / "codex"
        path.parent.mkdir()
        path.write_text(self.SERVER.format(python=sys.executable, home=home))
        path.chmod(0o700)
        return str(path)

    def test_a_launcher_that_resets_the_home_is_skipped_for_the_native_binary(self):
        launcher = self.stand_in("launcher", repr(str(self.root / "user-home")))
        native = self.stand_in("native", "os.environ['CODEX_HOME']")
        with mock.patch.object(runtime, "_codex_candidates", return_value=[launcher, native]):
            self.assertEqual(runtime.api_codex_executable(self.environment), native)
            with mock.patch.object(runtime, "_reported_codex_home", side_effect=AssertionError("probed twice")):
                self.assertEqual(runtime.api_codex_executable(self.environment), native)

    def test_the_home_probe_never_receives_the_key(self):
        probed = []

        def probe(candidate, environment):
            probed.append(dict(environment))
            return self.home

        environment = {**self.environment, "TCS_PROVER_API_TOKEN": "sk-probe-secret-0123456789",
                       "OPENAI_API_KEY": runtime.CUSTOM_PROVIDER_LOGIN_PLACEHOLDER}
        with mock.patch.object(runtime, "_codex_candidates", return_value=["/opt/launcher/codex"]), \
                mock.patch.object(runtime, "_reported_codex_home", side_effect=probe):
            self.assertEqual(runtime.api_codex_executable(environment), "/opt/launcher/codex")
        self.assertEqual(len(probed), 1)
        self.assertEqual(probed[0]["CODEX_HOME"], str(self.home))
        for name in ("TCS_PROVER_API_TOKEN", "OPENAI_API_KEY"):
            self.assertNotIn(name, probed[0])
        self.assertNotIn("sk-probe-secret-0123456789", json.dumps(probed))

    def test_no_isolating_binary_refuses_to_run(self):
        launcher = self.stand_in("launcher", repr(str(self.root / "user-home")))
        silent = self.root / "silent" / "codex"
        silent.parent.mkdir()
        silent.write_text("#!/bin/sh\nexit 3\n")
        silent.chmod(0o700)
        with mock.patch.object(runtime, "_codex_candidates", return_value=[launcher, str(silent)]):
            with self.assertRaisesRegex(runtime.Error, "need a Codex CLI that honors CODEX_HOME"):
                runtime.api_codex_executable(self.environment)

    def test_candidates_start_with_the_codex_on_path(self):
        launcher = self.stand_in("launcher", "''")
        with mock.patch.dict(os.environ, {"PATH": f"{Path(launcher).parent}{os.pathsep}/usr/bin"}):
            candidates = runtime._codex_candidates()
        self.assertEqual(candidates[0], launcher)
        self.assertEqual(len(candidates), len(set(candidates)))


class RecordRedactionTests(unittest.TestCase):
    """Every emitted record is redacted centrally, for both harnesses."""

    def setUp(self):
        saved = set(runtime._REGISTERED_SECRETS)
        self.addCleanup(lambda: (runtime._REGISTERED_SECRETS.clear(), runtime._REGISTERED_SECRETS.update(saved)))

    def emitted(self, *args, **fields):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            runtime.emit(*args, **fields)
        return output.getvalue()

    def test_every_string_field_is_redacted_but_the_record_shape_is_kept(self):
        secret = 'sk-record-"quoted"-0123456789'
        runtime.register_secret(secret)
        line = self.emitted("codex_event", "critic", event={
            "type": "item.completed", "item": {"type": "command_execution", "aggregated_output": f"key={secret}"},
            "nested": [f"a {secret} b", {"deeper": secret}]}, text=f"error body {json.dumps(secret)}",
            url=f"https://x.example/?k={server_quote(secret)}", count=3)
        record = json.loads(line)
        self.assertNotIn("record-", line)
        self.assertEqual((record["kind"], record["stage"], record["count"]), ("codex_event", "critic", 3))
        self.assertEqual(record["event"]["item"]["aggregated_output"], "key=[redacted]")
        self.assertEqual(record["event"]["nested"], ["a [redacted] b", {"deeper": "[redacted]"}])
        self.assertEqual(record["text"], 'error body "[redacted]"')
        self.assertEqual(record["url"], "https://x.example/?k=[redacted]")

    def test_inherited_credentials_are_redacted_and_short_values_left_alone(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-inherited-0123456789", "OPENAI_API_KEY": "abc"}):
            runtime.register_secret("tiny")
            line = self.emitted("status", "solve", text="sk-ant-inherited-0123456789 abc tiny latest")
        # A "secret" this short cannot be told apart from words, so records keep the text.
        self.assertEqual(json.loads(line)["text"], "[redacted] abc tiny latest")


def server_quote(value):
    from urllib.parse import quote
    return quote(value, safe="")


class CommandLineTests(ApiCase):
    def exit_code(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as stop:
                runtime.main([*arguments, "--help"])
        return stop.exception.code, errors.getvalue()

    def test_runner_flags_accept_claude_and_configured_api_ids(self):
        model = self.model()
        for value in ("claude-opus-5-5", model, "gpt-5.6-luna"):
            with self.subTest(model=value):
                self.assertEqual(self.exit_code("x.yaml", "--author-model", value, "--critic-model", value,
                                                "--writer-model", value, "--model", value)[0], 0)
        code, errors = self.exit_code("x.yaml", "--author-model", "api:unknown")
        self.assertEqual(code, 2)
        self.assertIn("Unknown model 'api:unknown'", errors)
        self.assertIn("claude-haiku-5-5", errors)
        code, errors = self.exit_code("x.yaml", "--writer-model", "deepseek-v4-pro")
        self.assertEqual(code, 2)
        self.assertIn("DeepSeek is no longer supported", errors)


if __name__ == "__main__":
    unittest.main()
