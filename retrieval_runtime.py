"""Fail-closed historical retrieval controls, independent of model prompts.

Only a controller-owned policy can configure the historical MCP server. Native
search is always disabled on this branch. An isolation status in JSON is never
accepted as proof: the exact Codex process must pass configuration and sandbox
canaries before a model turn is allowed.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import threading
import uuid

POLICY_ENV = "TCS_PROVER_RETRIEVAL_POLICY"
PROFILE = "historical"
BROKER_NAME = "historical_search"
DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "hooks", "memories", "skill_search",
    "skill_mcp_dependency_install", "shell_snapshot", "tool_suggest",
    "image_generation",
)
SYSTEM_ENV = ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
SYSTEM_SKILLS = ("imagegen", "openai-docs", "review-agent", "skill-creator", "skill-installer")
PROTECTED_DIRECTORIES = (".codex", ".agents", ".git", ".aws")
PROTECTED_SETTINGS = "job-settings.json"
SETTINGS_BOUNDARY_CANARIES = (".retrieval-delete-canary", ".retrieval-rename-canary")
BENCHMARK_FILES = ("input.md", "draft.md", "checked-statement.md")
BENCHMARK_DIRECTORIES = ("prompts",)
PUBLIC_EGRESS_CANARY = ("1.1.1.1", 443)


class RetrievalPolicyError(RuntimeError):
    """A historical run cannot safely start or continue."""


def native_search_arguments():
    return ["-c", 'web_search="disabled"', "-c", "tools.web_search=false"]


def codex_executable(policy, search_path=None):
    """Resolve a real controller binary without Windows' implicit cwd search.

    Historical execution cannot trust cmd/PowerShell shims or PATH entries
    inside the model workspace. The Windows adapter requires a native binary.
    """
    workspace = Path(policy["isolation"]["workspace"]).resolve()
    for entry in (os.environ.get("PATH", "") if search_path is None else search_path).split(os.pathsep):
        directory = Path(entry.strip('"'))
        if not entry or not directory.is_absolute():
            continue
        candidate = (directory / "codex.exe").resolve()
        if candidate == workspace or workspace in candidate.parents:
            continue
        if candidate.is_file():
            return str(candidate)
    raise RetrievalPolicyError("Historical execution requires an absolute native codex.exe on a trusted PATH outside the run workspace.")


def _absolute(value, label):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise RetrievalPolicyError(f"Historical retrieval requires an absolute {label}.")
    return Path(value).resolve()


def _outside(path, workspace, label):
    if path == workspace or workspace in path.parents:
        raise RetrievalPolicyError(f"Historical {label} must be outside the writable run workspace.")


def load_policy(path):
    """Validate immutable policy input without executing anything or networking."""
    path = _absolute(os.fspath(path), "policy path")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RetrievalPolicyError(f"Cannot read historical retrieval policy: {exc}") from exc
    required = {"schemaVersion", "mode", "runId", "cutoffExclusiveUtc", "broker", "isolation"}
    if (not isinstance(value, dict) or not required.issubset(value)
            or set(value) - required - {"metadata"}
            or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or value.get("mode") != "historical"):
        raise RetrievalPolicyError("Historical retrieval policy has an invalid schema or mode.")
    if not isinstance(value["runId"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value["runId"]):
        raise RetrievalPolicyError("Historical retrieval policy has an invalid runId.")
    try:
        cutoff = datetime.fromisoformat(value["cutoffExclusiveUtc"].replace("Z", "+00:00"))
        if cutoff.tzinfo is None or cutoff.utcoffset() != timezone.utc.utcoffset(cutoff):
            raise ValueError("not UTC")
    except (AttributeError, TypeError, ValueError) as exc:
        raise RetrievalPolicyError("Historical cutoffExclusiveUtc must be an explicit UTC timestamp.") from exc
    broker, isolation = value["broker"], value["isolation"]
    if not isinstance(broker, dict) or set(broker) != {"command", "args", "policyFile", "auditLog"}:
        raise RetrievalPolicyError("Historical broker requires command, args, policyFile and auditLog.")
    if not isinstance(broker["args"], list) or not all(isinstance(arg, str) for arg in broker["args"]):
        raise RetrievalPolicyError("Historical broker args must be an array of strings.")
    if (not isinstance(isolation, dict)
            or set(isolation) != {"status", "adapter", "codexHome", "workspace"}
            or not isinstance(isolation.get("status"), str)
            or isolation.get("adapter") != "codex-windows-mxc-v1"):
        raise RetrievalPolicyError("Historical retrieval requires the codex-windows-mxc-v1 isolation adapter.")
    workspace = _absolute(isolation["workspace"], "isolation workspace")
    for candidate, label in ((path, "policy"),
                             (_absolute(isolation["codexHome"], "isolated Codex home"), "Codex home"),
                             (_absolute(broker["command"], "broker executable"), "broker executable"),
                             (_absolute(broker["policyFile"], "broker policy path"), "broker policy"),
                             (_absolute(broker["auditLog"], "broker audit log"), "broker audit log")):
        _outside(candidate, workspace, label)
    if not Path(broker["command"]).is_file() or not Path(broker["policyFile"]).is_file():
        raise RetrievalPolicyError("Historical broker executable or policy file is missing.")
    try:
        broker_policy = json.loads(Path(broker["policyFile"]).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RetrievalPolicyError("Cannot read historical broker policy.") from exc
    if not isinstance(broker_policy, dict):
        raise RetrievalPolicyError("Historical broker policy must be an object.")
    arguments = broker["args"]
    policy_hash = hashlib.sha256(Path(broker["policyFile"]).read_bytes()).hexdigest()
    if (len(arguments) != 9 or arguments[1:] != [
            "--cutoff", value["cutoffExclusiveUtc"], "--policy-file", broker["policyFile"],
            "--policy-sha256", policy_hash, "--audit-log", broker["auditLog"]]):
        raise RetrievalPolicyError("Historical broker arguments must bind the exact cutoff, policy hash and audit log.")
    script = _absolute(arguments[0], "broker script")
    _outside(script, workspace, "broker script")
    if not script.is_file():
        raise RetrievalPolicyError("Historical broker script is missing.")
    value["_path"] = str(path)
    value["_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    value["_brokerScriptSha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    credential_env = broker_policy.get("credential_env")
    if credential_env not in (None, "BRAVE_SEARCH_API_KEY"):
        raise RetrievalPolicyError("Historical broker credential environment is not in the audited allowlist.")
    value["_credentialEnv"] = credential_env
    return value


def policy_for_workspace(workspace=None):
    """Use the trusted child environment, with a saved-run fallback for CLI use."""
    workspace = Path(workspace or Path.cwd()).resolve()
    settings = {}
    settings_path = workspace / "job-settings.json"
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict):
                raise ValueError("expected object")
        except (OSError, UnicodeError, ValueError) as exc:
            raise RetrievalPolicyError(f"Cannot verify saved run retrieval settings: {exc}") from exc
        if settings.get("launchHold"):
            raise RetrievalPolicyError(f"Run launch is held: {settings['launchHold']}")
    supplied, saved = os.environ.get(POLICY_ENV), settings.get("retrievalPolicyPath")
    if supplied and saved and Path(supplied).resolve() != Path(saved).resolve():
        raise RetrievalPolicyError("Child retrieval policy differs from the saved run policy.")
    if not supplied and not saved:
        return None
    policy = load_policy(supplied or saved)
    if workspace != Path(policy["isolation"]["workspace"]).resolve():
        raise RetrievalPolicyError("Historical policy is bound to a different run workspace.")
    return policy


def configuration(policy, role):
    """Generate the exact isolated configuration; only the author gets MCP."""
    config = {
        "approval_policy": "never", "default_permissions": PROFILE,
        "web_search": "disabled", "tools": {"web_search": False, "view_image": False},
        "project_doc_max_bytes": 0, "allow_login_shell": False,
        "features": {**{name: False for name in DISABLED_FEATURES}, "skip_host_skill_discovery": True},
        "shell_environment_policy": {"inherit": "none", "set": {
            key: value for key, value in os.environ.items() if key.upper() in {name.upper() for name in SYSTEM_ENV}
        }},
        "windows": {"sandbox": "mxc"},
        "permissions": {PROFILE: {
            "filesystem": {":minimal": "read", ":workspace_roots": {
                ".": "write" if role == "author" else "read",
                **{name: "read" for name in PROTECTED_DIRECTORIES},
                PROTECTED_SETTINGS: "read",
                **{name: "read" for name in SETTINGS_BOUNDARY_CANARIES},
                **{name: "read" for name in (*BENCHMARK_FILES, *BENCHMARK_DIRECTORIES)},
            }},
            "network": {"enabled": False},
        }},
        "mcp_servers": {},
        "skills": {"config": [{"path": str(role_home(policy, role) / "skills/.system" / name / "SKILL.md"),
                               "enabled": False} for name in SYSTEM_SKILLS]},
    }
    if role == "author":
        broker = policy["broker"]
        config["mcp_servers"][BROKER_NAME] = {
            "command": broker["command"], "args": list(broker["args"]),
            "required": True, "env_vars": [policy["_credentialEnv"]] if policy.get("_credentialEnv") else [],
        }
    return config


def role_home(policy, role):
    return Path(policy["isolation"]["codexHome"]) / ("author" if role == "author" else "structured")


def role_workspace(policy, role):
    return Path(policy["isolation"]["workspace"]) if role == "author" else role_home(policy, role) / "workspace"


def _toml_value(value):
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(key) + " = " + _toml_value(item) for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _toml_table(value, prefix=()):
    lines = []
    if prefix:
        lines.append("[" + ".".join(json.dumps(part) for part in prefix) + "]")
    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(f"{json.dumps(key)} = {_toml_value(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            lines.extend(_toml_table(item, (*prefix, key)))
    return lines


def config_text(policy, role):
    return "\n".join(_toml_table(configuration(policy, role))) + "\n"


def isolated_environment(policy, role, inherited):
    """Ignore user runtime overrides; shell children receive no provider secrets."""
    home = role_home(policy, role)
    home.mkdir(parents=True, exist_ok=True)
    role_workspace(policy, role).mkdir(parents=True, exist_ok=True)
    verify_protected_directories(policy, role, create=True)
    expected = config_text(policy, role)
    config = home / "config.toml"
    if config.exists() and config.read_text(encoding="utf-8") != expected:
        raise RetrievalPolicyError("Isolated Codex configuration has changed; re-prepare the run.")
    if not config.exists():
        config.write_text(expected, encoding="utf-8")
    environment = {key: value for key, value in inherited.items() if key.upper() in {k.upper() for k in SYSTEM_ENV}}
    environment.update(CODEX_HOME=str(home), HOME=str(home), USERPROFILE=str(home),
                       RUST_LOG="error", TCS_PROVER_RETRIEVAL_POLICY=policy["_path"])
    # Independent broker discovery is optional and receives its key only in the
    # controller process. shell_environment_policy never passes it to the author.
    credential_env = policy.get("_credentialEnv")
    if credential_env and credential_env in inherited:
        environment[credential_env] = inherited[credential_env]
    return environment


def verify_protected_directories(policy, role, create=False):
    """Keep project configuration, skills, rules and credentials out of the run."""
    for name in PROTECTED_DIRECTORIES:
        path = role_workspace(policy, role) / name
        if create:
            path.mkdir(exist_ok=True)
        if (not path.is_dir() or path.is_symlink()
                or (hasattr(path, "is_junction") and path.is_junction())
                or path.resolve() != path.absolute()
                or any(path.iterdir())):
            raise RetrievalPolicyError(f"Historical isolation requires an empty protected {name} directory.")
    settings = role_workspace(policy, role) / PROTECTED_SETTINGS
    if create and role != "author" and not settings.exists():
        # Structured sessions use an empty private workspace; this harmless
        # controller-created file exercises the same denied-write boundary.
        settings.write_text("{}\n", encoding="utf-8")
    if (not settings.is_file() or settings.is_symlink()
            or (hasattr(settings, "is_junction") and settings.is_junction())
            or settings.resolve() != settings.absolute()):
        raise RetrievalPolicyError("Historical isolation requires a regular protected job-settings.json file.")
    if role == "author":
        try:
            saved = json.loads(settings.read_text(encoding="utf-8-sig"))
            saved_policy = saved.get("retrievalPolicyPath") if isinstance(saved, dict) else None
        except (OSError, UnicodeError, ValueError) as exc:
            raise RetrievalPolicyError("Cannot verify the protected saved retrieval policy.") from exc
        if not saved_policy or Path(saved_policy).resolve() != Path(policy["_path"]).resolve():
            raise RetrievalPolicyError("Protected job-settings.json must retain this run's retrieval policy.")
    for name in (*BENCHMARK_FILES, *BENCHMARK_DIRECTORIES):
        path = role_workspace(policy, role) / name
        if not path.exists() and not path.is_symlink():
            continue
        expected_type = path.is_dir() if name in BENCHMARK_DIRECTORIES else path.is_file()
        if (not expected_type or path.is_symlink()
                or (hasattr(path, "is_junction") and path.is_junction())
                or path.resolve() != path.absolute()):
            raise RetrievalPolicyError(f"Historical benchmark {name} must be a regular protected path.")
        if path.is_dir() and any(child.is_symlink()
                or (hasattr(child, "is_junction") and child.is_junction())
                or child.resolve() != child.absolute() for child in path.rglob("*")):
            raise RetrievalPolicyError("Historical benchmark prompts cannot contain links or junctions.")


def _subset(expected, actual, label="config"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise RetrievalPolicyError(f"Historical isolation mismatch: {label}.")
        for key, value in expected.items():
            _subset(value, actual.get(key), f"{label}.{key}")
    elif expected != actual:
        raise RetrievalPolicyError(f"Historical isolation mismatch: {label}.")


def verify_rpc(rpc, policy, role, environment, allow_runtime_overrides=False):
    """No model calls: inspect effective config and exercise the actual sandbox."""
    workspace = str(role_workspace(policy, role))
    verify_protected_directories(policy, role)
    if (hashlib.sha256(Path(policy["_path"]).read_bytes()).hexdigest() != policy["_sha256"]
            or hashlib.sha256(Path(policy["broker"]["args"][0]).read_bytes()).hexdigest() != policy["_brokerScriptSha256"]):
        raise RetrievalPolicyError("Historical controller policy or broker changed during the run.")
    result = rpc.call("config/read", {"cwd": workspace, "includeLayers": True})
    config = result.get("config", {})
    _subset(configuration(policy, role), config)
    for key in ("sandbox_mode", "sandbox_workspace_write", "instructions", "developer_instructions",
                "model_instructions_file", "experimental_compact_prompt_file", "compact_prompt",
                "hooks", "plugins", "marketplaces", "agents", "orchestrator", "notify",
                "model_catalog_json", "js_repl_node_module_dirs"):
        if config.get(key):
            raise RetrievalPolicyError(f"Historical isolation rejects inherited {key}.")
    actual_servers = config.get("mcp_servers") or {}
    if set(actual_servers) != ({BROKER_NAME} if role == "author" else set()):
        raise RetrievalPolicyError("Historical isolation rejects inherited MCP servers.")
    profile = config["permissions"][PROFILE]
    if set(profile.get("filesystem", {})) - {":minimal", ":workspace_roots", "glob_scan_max_depth"}:
        raise RetrievalPolicyError("Historical isolation rejects additional filesystem grants.")
    if profile.get("extends") or profile.get("workspace_roots"):
        raise RetrievalPolicyError("Historical isolation rejects inherited permission profiles.")
    if profile["filesystem"][":workspace_roots"] != configuration(policy, role)["permissions"][PROFILE]["filesystem"][":workspace_roots"]:
        raise RetrievalPolicyError("Historical isolation rejects additional workspace grants.")
    if any(value for key, value in profile["network"].items() if key != "enabled"):
        raise RetrievalPolicyError("Historical isolation rejects additional network grants.")
    for layer in result.get("layers") or []:
        source = layer.get("name") or {}
        if (source.get("type") != "user" and layer.get("config")
                and not (allow_runtime_overrides and source.get("type") == "sessionFlags")):
            raise RetrievalPolicyError("Historical isolation rejects nonempty inherited configuration layers.")
        if source.get("type") == "user" and Path(source.get("file", "")).resolve() != Path(environment["CODEX_HOME"]) / "config.toml":
            raise RetrievalPolicyError("Historical isolation loaded a different Codex home.")
    readiness = rpc.call("windowsSandbox/readiness", {})
    if readiness.get("status") != "ready":
        raise RetrievalPolicyError(f"Historical isolation is unavailable: Windows sandbox {readiness.get('status', 'unknown')}.")
    servers = rpc.call("mcpServerStatus/list", {})
    if not isinstance(servers.get("data"), list):
        raise RetrievalPolicyError("Historical isolation cannot verify the MCP inventory.")
    names = {row.get("name") for row in servers.get("data") or []}
    if servers.get("nextCursor") or names != ({BROKER_NAME} if role == "author" else set()):
        raise RetrievalPolicyError("Historical isolation found missing or unexpected live MCP servers.")
    for server in servers.get("data") or []:
        if (set(server.get("tools") or {}) != {"search", "open"}
                or server.get("resources") or server.get("resourceTemplates") or server.get("toolsError")):
            raise RetrievalPolicyError("Historical broker has missing or unexpected tools.")
    skills = rpc.call("skills/list", {"cwds": [workspace], "forceReload": True})
    if not isinstance(skills.get("data"), list) or not skills["data"]:
        raise RetrievalPolicyError("Historical isolation cannot verify the skills inventory.")
    for row in skills.get("data") or []:
        if row.get("errors") or any(skill.get("enabled") is not False for skill in row.get("skills", [])):
            raise RetrievalPolicyError("Historical isolation rejects enabled or unreadable skills.")
    _sandbox_canaries(rpc, policy, role)


def _sandbox_canaries(rpc, policy, role):
    """Prove allowed I/O works, external reads fail, and local TCP egress fails."""
    nonce = uuid.uuid4().hex
    outside = Path(policy["_path"]).parent / f".read-canary-{nonce}"
    inside = role_workspace(policy, role) / f".isolation-canary-{nonce}"
    protected = role_workspace(policy, role) / ".codex" / f".isolation-canary-{nonce}"
    settings = role_workspace(policy, role) / PROTECTED_SETTINGS
    delete_canary, rename_canary = [role_workspace(policy, role) / name for name in SETTINGS_BOUNDARY_CANARIES]
    renamed_canary = role_workspace(policy, role) / f".retrieval-renamed-{nonce}"
    created_sentinels = []
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    listener = socket.socket()
    try:
        outside.write_text(nonce, encoding="utf-8")
        inside.write_text(nonce, encoding="utf-8")
        for sentinel in (delete_canary, rename_canary):
            # Never overwrite an existing path, including a symlink planted in
            # a previously unisolated workspace.
            with sentinel.open("x", encoding="utf-8") as stream:
                stream.write(nonce)
            created_sentinels.append(sentinel)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        # A TCP handshake sends no prompt/query or HTTP content. Both parent
        # positive controls must succeed so offline networking cannot be mistaken
        # for enforced public egress denial inside the sandbox.
        _public_network_control()
        script = (
            "$ErrorActionPreference='Stop'; $insideOk=$false; $insideWrite=$false; $protectedWrite=$false; $settingsWrite=$false; $settingsDelete=$false; $settingsRename=$false; $settingsCanariesRead=$false; $outsideRead=$false; $network=$false; $publicNetwork=$false; "
            f"try {{ [IO.File]::WriteAllText({quote(inside)},'{nonce}'); $insideWrite=$true }} catch {{ }}; "
            f"try {{ $insideOk=([IO.File]::ReadAllText({quote(inside)}) -eq '{nonce}') }} catch {{ }}; "
            f"try {{ [IO.File]::WriteAllText({quote(protected)},'{nonce}'); $protectedWrite=$true }} catch {{ }}; "
            # Open-only probe: never truncate, write, rename or delete settings.
            "$settingsHandle=$null; try { "
            f"$settingsHandle=[IO.File]::Open({quote(settings)},[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite); "
            "$settingsWrite=$true } catch { } finally { if ($null -ne $settingsHandle) { $settingsHandle.Dispose() } }; "
            f"try {{ $settingsCanariesRead=([IO.File]::ReadAllText({quote(delete_canary)}) -eq '{nonce}' -and "
            f"[IO.File]::ReadAllText({quote(rename_canary)}) -eq '{nonce}') }} catch {{ }}; "
            f"try {{ [IO.File]::Delete({quote(delete_canary)}); $settingsDelete=(-not [IO.File]::Exists({quote(delete_canary)})) }} catch {{ }}; "
            f"try {{ [IO.File]::Move({quote(rename_canary)},{quote(renamed_canary)}); $settingsRename=$true }} catch {{ }}; "
            f"try {{ $null=[IO.File]::ReadAllText({quote(outside)}); $outsideRead=$true }} catch {{ }}; "
            "$client=New-Object Net.Sockets.TcpClient; try { "
            f"$task=$client.ConnectAsync('127.0.0.1',{port}); "
            "if ($task.Wait(2000) -and $client.Connected) { $network=$true } "
            "} catch { } finally { $client.Dispose() }; "
            "$client=New-Object Net.Sockets.TcpClient; try { "
            f"$task=$client.ConnectAsync('{PUBLIC_EGRESS_CANARY[0]}',{PUBLIC_EGRESS_CANARY[1]}); "
            "if ($task.Wait(2000) -and $client.Connected) { $publicNetwork=$true } "
            "} catch { } finally { $client.Dispose() }; "
            f"@{{nonce='{nonce}';insideOk=$insideOk;insideWrite=$insideWrite;protectedWrite=$protectedWrite;settingsWrite=$settingsWrite;settingsDelete=$settingsDelete;settingsRename=$settingsRename;settingsCanariesRead=$settingsCanariesRead;outsideRead=$outsideRead;network=$network;publicNetwork=$publicNetwork}} "
            "| ConvertTo-Json -Compress"
        )
        executable = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        result = rpc.call("command/exec", {
            "command": [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            "cwd": str(role_workspace(policy, role)), "permissionProfile": PROFILE, "timeoutMs": 10000,
        })
        try:
            report = json.loads(result.get("stdout", ""))
        except (TypeError, ValueError) as exc:
            raise RetrievalPolicyError("Historical sandbox canary returned no valid evidence.") from exc
        _public_network_control()
        if result.get("exitCode") != 0 or report != {"nonce": nonce, "insideOk": True, "insideWrite": role == "author", "protectedWrite": False, "settingsWrite": False, "settingsDelete": False, "settingsRename": False, "settingsCanariesRead": True, "outsideRead": False, "network": False, "publicNetwork": False}:
            raise RetrievalPolicyError("Historical sandbox did not enforce workspace I/O, outside-read denial and TCP denial.")
    finally:
        listener.close()
        outside.unlink(missing_ok=True)
        inside.unlink(missing_ok=True)
        protected.unlink(missing_ok=True)
        for sentinel in created_sentinels:
            sentinel.unlink(missing_ok=True)
        renamed_canary.unlink(missing_ok=True)


def _public_network_control():
    try:
        with socket.create_connection(PUBLIC_EGRESS_CANARY, timeout=3):
            pass
    except OSError as exc:
        raise RetrievalPolicyError("Cannot verify public network isolation: controller connectivity canary failed.") from exc


class _ProbeRPC:
    """Bounded JSON-RPC transport for preflight only, never starts a model turn."""
    def __init__(self, process):
        self.process, self.number, self.messages = process, 0, queue.Queue()
        def read():
            for line in process.stdout:
                self.messages.put(line)
            self.messages.put(None)
        threading.Thread(target=read, daemon=True).start()

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.number += 1
        self.send({"id": self.number, "method": method, "params": params})
        while True:
            try:
                line = self.messages.get(timeout=20)
            except queue.Empty as exc:
                raise RetrievalPolicyError("Historical isolation preflight timed out.") from exc
            if line is None:
                raise RetrievalPolicyError("Historical isolation preflight process exited.")
            message = json.loads(line)
            if message.get("id") != self.number:
                continue
            if "error" in message:
                raise RetrievalPolicyError(f"Historical isolation {method} failed: {message['error']}")
            return message.get("result", {})


def preflight(policy, role, executable, inherited):
    """Return the isolated child env only after real host enforcement passes."""
    if policy is None:
        return inherited
    if role == "auditor":
        raise RetrievalPolicyError("Optional research audits are disabled for historical runs.")
    if os.name != "nt":
        raise RetrievalPolicyError("Historical isolation adapter requires Windows MXC; no fallback is allowed.")
    environment = isolated_environment(policy, role, inherited)
    process = subprocess.Popen(
        [executable, "--strict-config", "app-server"],
        cwd=str(role_workspace(policy, role)), env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        rpc = _ProbeRPC(process)
        initialized = rpc.call("initialize", {
            "clientInfo": {"name": "tcs_retrieval_preflight", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        if Path(initialized.get("codexHome", "")).resolve() != Path(environment["CODEX_HOME"]).resolve():
            raise RetrievalPolicyError("Historical preflight loaded a different Codex home.")
        rpc.send({"method": "initialized", "params": {}})
        verify_rpc(rpc, policy, role, environment)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return environment
