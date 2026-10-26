"""Fail-closed historical retrieval controls, independent of model prompts.

Only a controller-owned policy can configure the historical MCP server. Native
search follows the saved run mode and is disabled under a historical policy. An isolation status in JSON is never
accepted as proof: the exact Codex process must pass configuration and sandbox
canaries before a model turn is allowed.
"""

from datetime import datetime, timezone
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
import tomllib
import uuid

POLICY_ENV = "TCS_PROVER_RETRIEVAL_POLICY"
MODE_ENV = "TCS_PROVER_RETRIEVAL_MODE"
PROFILE = "historical"
BROKER_NAME = "historical_search"
BROKER_TOOLS = ("search", "open", "find")
DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "hooks", "memories", "skill_search",
    "skill_mcp_dependency_install", "shell_snapshot", "tool_suggest",
    "image_generation",
    "view_image", "multi_agent_v2", "standalone_web_search",
    "code_mode", "request_permissions_tool",
)
SYSTEM_ENV = ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
SYSTEM_SKILLS = ("imagegen", "openai-docs", "review-agent", "skill-creator", "skill-installer")
PROTECTED_DIRECTORIES = (".codex", ".agents", ".git", ".aws")
PROTECTED_SETTINGS = "job-settings.json"
SETTINGS_BOUNDARY_CANARIES = (".retrieval-delete-canary", ".retrieval-rename-canary")
BENCHMARK_FILES = ("input.md", "draft.md", "checked-statement.md")
BENCHMARK_DIRECTORIES = ("prompts",)
PUBLIC_EGRESS_CANARY = ("1.1.1.1", 443)
WINDOWS_ADAPTER = "codex-windows-mxc-v1"
MACOS_ADAPTER = "codex-macos-seatbelt-v1"
SETTINGS_READ_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
BROKER_MODULES = ('__init__.py', 'historical_transport.py', 'historical_rate_limit.py', 'historical_cache.py',
                  'scholarly_discovery.py', 'historical_codex_discovery.py',
                  'historical_discovery_auth.py')
# Before the broker moved into retrieval/, policies pinned it as
# <checkout>/historical_search.py beside these modules, without a package.
LEGACY_BROKER_MODULES = BROKER_MODULES[1:]
LEGACY_BROKER_SCRIPT = Path(__file__).resolve().parents[1] / 'historical_search.py'
CURRENT_BROKER_SCRIPT = Path(__file__).resolve().with_name('historical_search.py')
_CRITIC_SESSIONS = {}


class RetrievalPolicyError(RuntimeError):
    """A historical run cannot safely start or continue."""


def search_mode(workspace=None, *, policy=None):
    """Keep a launched job's mode fixed, preserving main's native-search default.

    Historical controller policies always take precedence. A fresh CLI invocation
    without saved job settings uses native live search, matching the UI default.
    """
    settings_path = Path(workspace or Path.cwd()).resolve() / "job-settings.json"
    saved = None
    if settings_path.exists() or settings_path.is_symlink():
        saved = _read_saved_settings(settings_path, "Cannot verify saved run retrieval settings.")
    if policy or os.environ.get(POLICY_ENV) or (saved or {}).get("retrievalPolicyPath"):
        return "historical"
    inherited = os.environ.get(MODE_ENV)
    mode = (saved.get("retrievalMode", "live") if saved is not None else inherited or "live")
    if mode not in ("live", "disabled") or inherited not in (None, "live", "disabled"):
        raise RetrievalPolicyError("Invalid search mode or missing historical retrieval policy; no model was started.")
    if inherited and inherited != mode:
        raise RetrievalPolicyError("Child search mode differs from the saved run settings.")
    return mode


def native_search_configuration(workspace=None, *, policy=None):
    enabled = search_mode(workspace, policy=policy) == "live"
    return {"web_search": "live" if enabled else "disabled", "tools": {"web_search": enabled}}


def native_search_arguments(workspace=None, *, policy=None):
    config = native_search_configuration(workspace, policy=policy)
    return ["-c", f'web_search="{config["web_search"]}"', "-c",
            "tools.web_search=" + str(config["tools"]["web_search"]).lower()]


def codex_executable(policy, search_path=None):
    """Resolve a real controller binary without Windows' implicit cwd search.

    Historical execution cannot trust cmd/PowerShell shims or PATH entries
    inside the model workspace. The Windows adapter requires a native binary.
    """
    workspace = Path(policy["isolation"]["workspace"]).resolve()
    macos = policy["isolation"]["adapter"] == MACOS_ADAPTER
    candidates = []
    for entry in (os.environ.get("PATH", "") if search_path is None else search_path).split(os.pathsep):
        directory = Path(entry.strip('"'))
        if not entry or not directory.is_absolute():
            continue
        candidates.append(directory / ("codex" if macos else "codex.exe"))
    if macos and search_path is None:
        # The standard shell shim resets CODEX_HOME and cannot be used for an
        # isolated run. Resolve the installed native payload directly instead.
        candidates.extend((Path.home() / ".codex/packages/standalone/current/bin/codex",
                           Path("/Applications/Codex.app/Contents/Resources/codex")))
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate == workspace or workspace in candidate.parents:
            continue
        if candidate.is_file() and (not macos or _native_macos_binary(candidate)):
            return str(candidate)
    name = "Mach-O codex binary" if macos else "codex.exe"
    raise RetrievalPolicyError(f"Historical execution requires an absolute native {name} outside the run workspace; script shims are not supported.")


def _native_macos_binary(path):
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
        return os.access(path, os.X_OK) and magic in {
            b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
        }
    except OSError:
        return False


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
            or isolation.get("adapter") not in {WINDOWS_ADAPTER, MACOS_ADAPTER}):
        raise RetrievalPolicyError("Historical retrieval requires a supported Windows MXC or macOS Seatbelt isolation adapter.")
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
    if script == LEGACY_BROKER_SCRIPT and not os.path.lexists(script):
        # Prepared before this checkout moved the broker into retrieval/: run
        # it from there. The policy file stays as written, so its recorded
        # hash still matches; the saved author config is refreshed before start.
        value["_legacyBrokerArgs0"] = arguments[0]
        script = CURRENT_BROKER_SCRIPT
        arguments = [str(script), *arguments[1:]]
        value["broker"] = broker = {**broker, "args": arguments}
    _outside(script, workspace, "broker script")
    if not script.is_file():
        raise RetrievalPolicyError("Historical broker script is missing.")
    value["_path"] = str(path)
    value["_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    value["_brokerScriptSha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    value["_brokerModuleSha256"] = {}
    # An older checkout that still has the root layout runs its own six modules.
    packaged = script.parent.name == "retrieval" or os.path.lexists(script.with_name("__init__.py"))
    for name in (BROKER_MODULES if packaged else LEGACY_BROKER_MODULES):
        module = script.with_name(name)
        _outside(module, workspace, "broker module")
        if not module.is_file() or module.is_symlink():
            raise RetrievalPolicyError("Historical broker module is missing or linked.")
        value["_brokerModuleSha256"][str(module)] = hashlib.sha256(module.read_bytes()).hexdigest()
    if ('rate_limit_directory' in broker_policy or 'solver_workspace' in broker_policy
            or broker_policy.get('provider') == 'scholarly_wayback'):
        if broker_policy.get('solver_workspace') != str(workspace):
            raise RetrievalPolicyError("Historical pacing must bind the actual solver workspace.")
        state = _absolute(broker_policy.get('rate_limit_directory'), "shared pacing directory")
        _outside(state, workspace, "shared pacing directory")
        structured = _absolute(isolation['codexHome'], 'Codex home') / 'structured' / 'workspace'
        _outside(state, structured, "shared pacing directory")
    credential_env = broker_policy.get("credential_env")
    if credential_env not in (None, "BRAVE_SEARCH_API_KEY"):
        raise RetrievalPolicyError("Historical broker credential environment is not in the audited allowlist.")
    value["_credentialEnv"] = credential_env
    if broker_policy.get('provider') == 'codex_wayback':
        from retrieval.historical_codex_discovery import validate_settings
        from retrieval.historical_transport import HistoricalSearchError
        try:
            discovery = validate_settings(broker_policy.get('discovery'), workspace=workspace)
        except (HistoricalSearchError, ValueError, OSError) as exc:
            raise RetrievalPolicyError('Historical Codex discovery configuration is invalid.') from exc
        if (Path(discovery['auth_home']) != path.parent / 'discovery-auth'
                or Path(discovery['state_root']) != path.parent / 'discovery-state'):
            raise RetrievalPolicyError('Historical discovery must use this controller\'s dedicated authentication and state directories.')
        structured = _absolute(isolation['codexHome'], 'Codex home') / 'structured' / 'workspace'
        for key in ('auth_home', 'state_root', 'codex_path'):
            _outside(Path(discovery[key]), structured, 'Codex discovery ' + key)
        value['_discoverySettings'] = discovery
    return value


def _read_saved_settings(path, error):
    """Tolerate a bounded partial write from an older running UI controller.

    New controllers replace settings atomically. Existing controllers can
    truncate the file while handling a thread-resumed notification; retry only
    incomplete JSON/UTF-8 reads. Valid but incorrect settings still fail closed.
    """
    for attempt in range(len(SETTINGS_READ_RETRY_DELAYS) + 1):
        if (not path.is_file() or path.is_symlink()
                or (hasattr(path, "is_junction") and path.is_junction())
                or path.resolve() != path.absolute()):
            raise RetrievalPolicyError("Historical isolation requires a regular protected job-settings.json file.")
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            if attempt < len(SETTINGS_READ_RETRY_DELAYS):
                time.sleep(SETTINGS_READ_RETRY_DELAYS[attempt])
                continue
            raise RetrievalPolicyError(error) from exc
        except OSError as exc:
            raise RetrievalPolicyError(error) from exc
        if not isinstance(value, dict):
            raise RetrievalPolicyError(error)
        return value


def policy_for_workspace(workspace=None):
    """Use the trusted child environment, with a saved-run fallback for CLI use."""
    workspace = Path(workspace or Path.cwd()).resolve()
    settings = {}
    settings_path = workspace / "job-settings.json"
    if settings_path.is_file() or settings_path.is_symlink():
        settings = _read_saved_settings(settings_path, "Cannot verify saved run retrieval settings.")
        if settings.get("launchHold"):
            raise RetrievalPolicyError(f"Run launch is held: {settings['launchHold']}")
    supplied, saved = os.environ.get(POLICY_ENV), settings.get("retrievalPolicyPath")
    if supplied and saved and Path(supplied).resolve() != Path(saved).resolve():
        raise RetrievalPolicyError("Child retrieval policy differs from the saved run policy.")
    if not supplied and not saved:
        if settings.get("retrievalMode") == "historical" or os.environ.get(MODE_ENV) == "historical":
            raise RetrievalPolicyError("The saved historical retrieval policy is missing; no model was started.")
        return None
    policy = load_policy(supplied or saved)
    if workspace != Path(policy["isolation"]["workspace"]).resolve():
        raise RetrievalPolicyError("Historical policy is bound to a different run workspace.")
    return policy


def configuration(policy, role):
    """Generate the exact isolated configuration; only the author gets MCP."""
    config = {
        "approval_policy": "never", "default_permissions": PROFILE,
        "web_search": "disabled", "tools": {"web_search": False},
        "project_doc_max_bytes": 0, "allow_login_shell": False,
        "features": {**{name: False for name in DISABLED_FEATURES}, "skip_host_skill_discovery": True,
                     "shell_tool": role in {"author", "critic"}, "multi_agent": True, "code_mode_host": True},
        "shell_environment_policy": {"inherit": "none", "set": {
            key: value for key, value in os.environ.items() if key.upper() in {name.upper() for name in SYSTEM_ENV}
        }},
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
    if policy["isolation"]["adapter"] == WINDOWS_ADAPTER:
        config["windows"] = {"sandbox": "mxc"}
    if role == "author":
        broker = policy["broker"]
        config["mcp_servers"][BROKER_NAME] = {
            "command": broker["command"], "args": list(broker["args"]),
            "required": True, "env_vars": [policy["_credentialEnv"]] if policy.get("_credentialEnv") else [],
            "tool_timeout_sec": 600,
            "enabled_tools": list(BROKER_TOOLS),
            "tools": {name: {"approval_mode": "approve"} for name in BROKER_TOOLS},
        }
    return config


def role_home(policy, role):
    if role == "critic":
        return _critic_paths(policy)[0]
    return Path(policy["isolation"]["codexHome"]) / ("author" if role == "author" else "structured")


def role_workspace(policy, role):
    if role == "critic":
        return _critic_paths(policy)[1]
    return Path(policy["isolation"]["workspace"]) if role == "author" else role_home(policy, role) / "workspace"


def _critic_paths(policy):
    """Only this controller's active context can select a critic snapshot."""
    from retrieval.historical_discovery_auth import _directory

    marker = policy.get("_criticSession")
    session = _CRITIC_SESSIONS.get(marker) if isinstance(marker, str) else None
    if (session is None or session["policy"] is not policy
            or not _exact_configuration(session["base"], {
                key: value for key, value in policy.items() if key != "_criticSession"})):
        raise RetrievalPolicyError("Critic execution requires an active protected snapshot session.")
    try:
        if any(_directory(path) != identity for path, identity in session["directories"].items()):
            raise ValueError("Changed session directory.")
    except (OSError, ValueError, RuntimeError) as exc:
        raise RetrievalPolicyError("Critic snapshot session directories changed.") from exc
    return session["home"], session["workspace"]


@contextmanager
def critic_session(policy):
    """Create one auth-only home and read-only snapshot workspace per attempt.

    The caller writes the selected proof/statement snapshots into role_workspace
    before preflight, and keeps this context open until the critic process ends.
    Sessions remain private on disk for audit; their capability expires on exit.
    No config, history, skills, project state or ambient credentials are copied.
    """
    from retrieval.historical_discovery_auth import _directory, _read_auth, preserve_refresh

    try:
        verified = load_policy(policy["_path"])
        if not _exact_configuration(verified, policy):
            raise ValueError("Changed controller policy.")
        base = Path(verified["isolation"]["codexHome"])
        _directory(base)
        master = None
        for role in ("structured", "author"):
            candidate_home = base / role
            if not candidate_home.exists() and not candidate_home.is_symlink():
                continue
            identity = _directory(candidate_home)
            candidate = candidate_home / "auth.json"
            if not candidate.exists() and not candidate.is_symlink():
                continue
            raw, _ = _read_auth(candidate)
            if _directory(candidate_home) != identity:
                raise ValueError("Changed authentication directory.")
            master = candidate
            master_parent_id = identity
            break
        if master is None:
            raise ValueError("Missing protected authentication.")
        sessions = base / "critic-sessions"
        sessions.mkdir(mode=0o700, exist_ok=True)
        _directory(sessions)
        if os.name != "nt" and sessions.stat().st_mode & 0o077:
            raise ValueError("Critic sessions must be private.")
        marker = uuid.uuid4().hex
        root = sessions / ("session-" + marker)
        home, workspace = root / "home", root / "workspace"
        for directory in (root, home, workspace):
            directory.mkdir(mode=0o700)
        descriptor = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        derived = copy.deepcopy(verified)
        derived["_criticSession"] = marker
        session = {"policy": derived, "base": verified, "home": home, "workspace": workspace,
                   "directories": {path: _directory(path) for path in (base, sessions, root, home, workspace)}}
    except (KeyError, TypeError, OSError, ValueError, RuntimeError, UnicodeError) as exc:
        raise RetrievalPolicyError("Cannot create a protected critic snapshot session.") from exc
    _CRITIC_SESSIONS[marker] = session
    try:
        yield derived
    finally:
        try:
            # Authentication helpers validate their current paths. Also bind
            # cleanup to the directories originally used for this session.
            _critic_paths(derived)
            if _directory(master.parent) == master_parent_id:
                preserve_refresh(master, home / "auth.json", hashlib.sha256(raw).hexdigest())
        except (RetrievalPolicyError, OSError, ValueError, RuntimeError):
            pass
        finally:
            _CRITIC_SESSIONS.pop(marker, None)


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


def _project_trust_root(policy, role):
    """Match native Codex's project identity using controller-owned Git metadata.

    Codex records trust for the common repository root of a linked worktree.
    The protected, empty .git in the model workspace is deliberately skipped.
    No Git command, config, hook, or model-controlled metadata is executed.
    """
    workspace = role_workspace(policy, role).resolve()
    for root in workspace.parents:
        marker = root / ".git"
        if marker.is_symlink():
            raise RetrievalPolicyError("Historical project trust cannot follow a linked .git marker.")
        if marker.is_dir() and (marker / "HEAD").is_file():
            return root
        if not marker.is_file():
            continue
        try:
            text = marker.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir: ") or "\n" in text:
                raise ValueError("invalid gitdir")
            gitdir = (root / text[8:]).resolve()
            _outside(gitdir, workspace, "Git metadata")
            common_file = gitdir / "commondir"
            if common_file.is_file():
                common = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve()
                _outside(common, workspace, "common Git metadata")
                if common.name != ".git" or not (common / "HEAD").is_file():
                    raise ValueError("unrecognized common Git directory")
                return common.parent
            if not (gitdir / "HEAD").is_file():
                raise ValueError("missing Git HEAD")
            return root
        except (OSError, UnicodeError, ValueError) as exc:
            raise RetrievalPolicyError("Cannot verify the native Codex project trust identity.") from exc
    return workspace


def _exact_configuration(expected, actual):
    """Compare TOML values without treating integer 1 as boolean true."""
    # Codex's raw-layer API normalizes duration integers to floating point.
    # Numeric equivalence is safe; booleans must still match their exact type.
    if type(expected) in (int, float) and type(actual) in (int, float):
        return expected == actual
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _exact_configuration(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _exact_configuration(left, right) for left, right in zip(expected, actual))
    return expected == actual


def _system_environment_policy(value):
    """Recognize a no-inherit shell environment holding only SYSTEM_ENV names."""
    names = {name.upper() for name in SYSTEM_ENV}
    return (isinstance(value, dict) and set(value) == {"inherit", "set"}
            and value["inherit"] == "none" and isinstance(value["set"], dict)
            and all(isinstance(key, str) and isinstance(item, str) and key.upper() in names
                    for key, item in value["set"].items()))


def _validate_user_configuration(policy, role, actual, label="Isolated Codex configuration", *,
                                 refresh_environment=False):
    """Accept only our exact config plus one known native bookkeeping record.

    The trust record is never an authorization to load project configuration.
    It is removed before process startup, and effective configuration layers
    are independently checked before each model turn. Before startup only, a
    controller restarted with a different PATH may also replace the saved
    system-only shell environment; secrets or inherited values still fail.
    Likewise the author's broker entry may move from the legacy root script
    to retrieval/ (load_policy() maps exactly that path, nothing else).
    """
    expected = configuration(policy, role)
    refreshed = False
    if (refresh_environment and isinstance(actual, dict)
            and _system_environment_policy(actual.get("shell_environment_policy"))
            and not _exact_configuration(expected["shell_environment_policy"],
                                         actual["shell_environment_policy"])):
        actual = {**actual, "shell_environment_policy": expected["shell_environment_policy"]}
        refreshed = True
    legacy = policy.get("_legacyBrokerArgs0")
    servers = actual.get("mcp_servers") if isinstance(actual, dict) else None
    saved = servers.get(BROKER_NAME) if isinstance(servers, dict) else None
    if refresh_environment and legacy and role == "author" and isinstance(saved, dict):
        wanted = expected["mcp_servers"][BROKER_NAME]["args"]
        if _exact_configuration([legacy, *wanted[1:]], saved.get("args")):
            actual = {**actual, "mcp_servers": {**servers, BROKER_NAME: {**saved, "args": list(wanted)}}}
            refreshed = True
    if _exact_configuration(expected, actual):
        return refreshed
    if isinstance(actual, dict) and "projects" in actual:
        remaining = {key: value for key, value in actual.items() if key != "projects"}
        if _exact_configuration(expected, remaining):
            allowed = {str(_project_trust_root(policy, role)): {"trust_level": "trusted"}}
            if _exact_configuration(allowed, actual["projects"]):
                return True
    raise RetrievalPolicyError(f"{label} has changed; re-prepare the run.")


def _verify_saved_configuration(policy, role, config, expected):
    if not config.is_file() or config.is_symlink():
        raise RetrievalPolicyError("Isolated Codex configuration must be a regular file.")
    original = config.read_bytes()
    try:
        actual = tomllib.loads(original.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RetrievalPolicyError("Isolated Codex configuration has changed; re-prepare the run.") from exc
    rewrite = _validate_user_configuration(policy, role, actual, refresh_environment=True)
    if not rewrite:
        return
    # Do not carry native auto-trust metadata or an earlier controller PATH into
    # a fresh config load. Replace atomically, preserving all sessions/auth and
    # refusing a concurrent edit.
    temporary = config.with_name(f".retrieval-config-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(expected)
        temporary.chmod(0o600)
        if config.is_symlink() or config.read_bytes() != original:
            raise RetrievalPolicyError("Isolated Codex configuration changed during verification.")
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)


def isolated_environment(policy, role, inherited):
    """Ignore user runtime overrides; shell children receive no provider secrets."""
    home = role_home(policy, role)
    home.mkdir(parents=True, exist_ok=True)
    role_workspace(policy, role).mkdir(parents=True, exist_ok=True)
    verify_protected_directories(policy, role, create=True)
    expected = config_text(policy, role)
    config = home / "config.toml"
    if config.exists() or config.is_symlink():
        _verify_saved_configuration(policy, role, config, expected)
    else:
        config.write_text(expected, encoding="utf-8")
    environment = {key: value for key, value in inherited.items() if key.upper() in {k.upper() for k in SYSTEM_ENV}}
    environment.update(CODEX_HOME=str(home), HOME=str(home), USERPROFILE=str(home),
                       RUST_LOG="error", TCS_PROVER_RETRIEVAL_POLICY=policy["_path"])
    # Independent broker discovery is optional and receives its key only in the
    # controller process. shell_environment_policy never passes it to the author.
    credential_env = policy.get("_credentialEnv")
    if role == "author" and credential_env and credential_env in inherited:
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
        saved = _read_saved_settings(settings, "Cannot verify the protected saved retrieval policy.")
        saved_policy = saved.get("retrievalPolicyPath")
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
            or hashlib.sha256(Path(policy["broker"]["args"][0]).read_bytes()).hexdigest() != policy["_brokerScriptSha256"]
            or any(not Path(path).is_file() or Path(path).is_symlink()
                   or hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
                   for path, digest in policy['_brokerModuleSha256'].items())):
        raise RetrievalPolicyError("Historical controller policy or broker changed during the run.")
    result = rpc.call("config/read", {"cwd": workspace, "includeLayers": True})
    config = result.get("config", {})
    expected = configuration(policy, role)
    if allow_runtime_overrides and (config.get("features") or {}).get("multi_agent") is False:
        # Reviewer/final stages can remove delegation; critic/author retain
        # their default subagents with the parent's immutable permissions.
        expected["features"]["multi_agent"] = False
    # Codex 0.160 serializes tools.web_search=false as null after normalizing
    # it into the disabled top-level mode. All other values remain exact.
    if config.get("web_search") == "disabled" and (config.get("tools") or {}).get("web_search", True) is None:
        expected["tools"]["web_search"] = None
    if role == "author" and not expected["mcp_servers"][BROKER_NAME]["env_vars"]:
        actual_broker = (config.get("mcp_servers") or {}).get(BROKER_NAME) or {}
        if "env_vars" not in actual_broker:
            expected["mcp_servers"][BROKER_NAME].pop("env_vars")
    _subset(expected, config)
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
        if source.get("type") == "user":
            _validate_user_configuration(policy, role, layer.get("config"), "Historical raw user config")
    if policy["isolation"]["adapter"] == WINDOWS_ADAPTER:
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
        if (set(server.get("tools") or {}) != set(BROKER_TOOLS)
                or server.get("resources") or server.get("resourceTemplates") or server.get("toolsError")):
            raise RetrievalPolicyError("Historical broker has missing or unexpected tools.")
    skills = rpc.call("skills/list", {"cwds": [workspace], "forceReload": True})
    if not isinstance(skills.get("data"), list) or not skills["data"]:
        raise RetrievalPolicyError("Historical isolation cannot verify the skills inventory.")
    for row in skills.get("data") or []:
        if row.get("errors") or any(skill.get("enabled") is not False for skill in row.get("skills", [])):
            raise RetrievalPolicyError("Historical isolation rejects enabled or unreadable skills.")
    _sandbox_canaries(rpc, policy, role)
    if policy["isolation"]["adapter"] == MACOS_ADAPTER:
        _macos_network_canaries(rpc, policy, role)


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
            # a previously unisolated workspace. Only a plain file left by an
            # interrupted controller preflight is removed first.
            _remove_stale_sentinel(sentinel)
            try:
                stream = sentinel.open("x", encoding="utf-8")
            except FileExistsError as exc:
                raise RetrievalPolicyError(f"Historical sandbox canary path {sentinel} already exists; inspect and remove it before resuming.") from exc
            created_sentinels.append(sentinel)
            with stream:
                stream.write(nonce)
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
        command = [executable, "-NoProfile", "-NonInteractive", "-Command", script]
        if policy["isolation"]["adapter"] == MACOS_ADAPTER:
            command = _macos_canary_command(
                nonce, inside, protected, settings, delete_canary, rename_canary,
                renamed_canary, outside, port,
            )
        result = rpc.call("command/exec", {
            "command": command,
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


def _remove_stale_sentinel(path):
    """Delete a canary left by a killed preflight; refuse links and other types."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise RetrievalPolicyError(f"Historical sandbox canary path {path} is not a stale controller file; inspect and remove it before resuming.")
    path.unlink()


def _macos_canary_command(nonce, inside, protected, settings, delete_canary,
                          rename_canary, renamed_canary, outside, port):
    """Use only Apple system tools, with positive controls for every probe.

    No model is involved. Opening settings for append does not write or truncate
    their contents; deletion and rename target disposable protected sentinels.
    """
    q = lambda path: shlex.quote(str(path))
    probes = {
        "insideWrite": f"printf %s {q(nonce)} > {q(inside)}",
        "insideOk": f'test "$(/bin/cat {q(inside)})" = {q(nonce)}',
        "protectedWrite": f"printf %s {q(nonce)} > {q(protected)}",
        "settingsWrite": f"/bin/sh -c {q('exec 3>> ' + q(settings))}",
        "settingsCanariesRead": (f'test "$(/bin/cat {q(delete_canary)})" = {q(nonce)} && '
                                 f'test "$(/bin/cat {q(rename_canary)})" = {q(nonce)}'),
        "settingsDelete": f"/bin/rm {q(delete_canary)}",
        "settingsRename": f"/bin/mv {q(rename_canary)} {q(renamed_canary)}",
        "outsideRead": f"/bin/cat {q(outside)}",
        "network": f"/usr/bin/nc -z -G 2 -w 2 127.0.0.1 {int(port)}",
        "publicNetwork": f"/usr/bin/nc -z -G 2 -w 2 {q(PUBLIC_EGRESS_CANARY[0])} {int(PUBLIC_EGRESS_CANARY[1])}",
    }
    lines = [f"if ( {probe} ) >/dev/null 2>&1; then {key}=true; else {key}=false; fi"
             for key, probe in probes.items()]
    template = json.dumps({"nonce": nonce, **{key: "%s" for key in probes}}, separators=(",", ":"))
    template = template.replace('"%s"', '%s') + "\n"
    lines.append("printf " + q(template) + " " + " ".join('"$' + key + '"' for key in probes))
    return ["/bin/sh", "-c", "\n".join(lines)]


def _public_network_control():
    try:
        with socket.create_connection(PUBLIC_EGRESS_CANARY, timeout=3):
            pass
    except OSError as exc:
        raise RetrievalPolicyError("Cannot verify public network isolation: controller connectivity canary failed.") from exc


@contextmanager
def _local_network_controls():
    """Observe TCP/UDP contact independently of a probe's reported exit code.

    The HTTP endpoint also acts as a harmless local proxy target. DNS replies
    are NXDOMAIN for the synthetic canary name; no query leaves this machine.
    """
    tcp, udp = socket.socket(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stopped = threading.Event()
    lock = threading.Lock()
    counts = {"tcp": 0, "udp": 0}
    threads = []
    try:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(32)
        tcp.settimeout(0.1)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(0.1)

        def accept_tcp():
            while not stopped.is_set():
                try:
                    connection, _ = tcp.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with lock:
                    counts["tcp"] += 1
                with connection:
                    connection.settimeout(0.2)
                    try:
                        request = connection.recv(4096)
                        if request.startswith(b'\x05'):
                            # Minimal SOCKS5 negotiation for the local positive
                            # control. Never connect or forward beyond here.
                            connection.sendall(b'\x05\x00')
                            connection.recv(4096)
                            connection.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                            connection.recv(4096)
                        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    except OSError:
                        pass

        def accept_udp():
            while not stopped.is_set():
                try:
                    packet, address = udp.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                with lock:
                    counts["udp"] += 1
                if len(packet) >= 12:
                    # Preserve transaction ID and question; set response,
                    # recursion-available and NXDOMAIN with no answer records.
                    reply = packet[:2] + b'\x81\x83' + packet[4:6] + b'\0' * 6 + packet[12:]
                    try:
                        udp.sendto(reply, address)
                    except OSError:
                        pass

        for target in (accept_tcp, accept_udp):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            threads.append(thread)

        def snapshot():
            with lock:
                return dict(counts)

        yield tcp.getsockname()[1], udp.getsockname()[1], snapshot
    finally:
        stopped.set()
        tcp.close()
        udp.close()
        for thread in threads:
            thread.join(timeout=1)


def _macos_network_probe_commands(port, dns_port, nonce):
    """Probe distinct network paths; none contains user or problem content."""
    q = shlex.quote
    direct = f"http://127.0.0.1:{int(port)}/"
    proxy = f"http://127.0.0.1:{int(port)}"
    curl = "/usr/bin/curl --silent --show-error --max-time 2 --output /dev/null"
    connection = f"/usr/bin/nc -z -G 2 -w 2 127.0.0.1 {int(port)}"
    ruby = f'require "socket"; s=TCPSocket.new("127.0.0.1", {int(port)}); s.close'
    return {
        "curlDirectIp": f"{curl} --noproxy '*' {q(direct)}",
        "curlLocalhost": f"{curl} --noproxy '*' {q(f'http://localhost:{int(port)}/')}",
        "curlForcedResolution": f"{curl} --noproxy '*' --resolve {q(f'canary.invalid:{int(port)}:127.0.0.1')} {q(f'http://canary.invalid:{int(port)}/')}",
        "httpProxyEnvironment": (f"/usr/bin/env http_proxy={q(proxy)} HTTP_PROXY={q(proxy)} "
                                 f"ALL_PROXY={q(proxy)} all_proxy={q(proxy)} NO_PROXY='' no_proxy='' "
                                 f"{curl} http://canary.invalid/"),
        "socksProxyEnvironment": (f"/usr/bin/env http_proxy='' HTTP_PROXY='' "
                                  f"ALL_PROXY={q('socks5h://127.0.0.1:' + str(int(port)))} "
                                  f"all_proxy={q('socks5h://127.0.0.1:' + str(int(port)))} "
                                  f"NO_PROXY='' no_proxy='' {curl} http://canary.invalid/"),
        "shellExec": f"/bin/zsh -f -c {q('exec ' + connection)}",
        "nestedShell": f"/bin/bash --noprofile --norc -c {q('/bin/sh -c ' + q(connection))}",
        "rubySocket": f"/usr/bin/ruby --disable-gems -e {q(ruby)}",
        "dnsDatagram": (f"/usr/bin/dig +notcp +nosearch @127.0.0.1 -p {int(dns_port)} "
                        f"{q(nonce + '.invalid')} A +time=1 +tries=1"),
    }


def _shell_boolean_report(nonce, probes):
    lines = [f"if ( {command} ) >/dev/null 2>&1; then {key}=true; else {key}=false; fi"
             for key, command in probes.items()]
    template = json.dumps({"nonce": nonce, **{key: "%s" for key in probes}}, separators=(",", ":"))
    template = template.replace('"%s"', '%s') + "\n"
    lines.append("printf " + shlex.quote(template) + " " + " ".join('"$' + key + '"' for key in probes))
    return ["/bin/sh", "-c", "\n".join(lines)]


def _network_observer_barrier(port, dns_port):
    """Wait for both listeners to consume preceding queued probe traffic."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as stream:
            stream.sendall(b"GET / HTTP/1.0\r\n\r\n")
            if not stream.recv(4096).startswith(b"HTTP/1.1 200"):
                raise OSError("TCP observer did not respond")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
            stream.settimeout(3)
            stream.sendto(b'\x01\x02\x01\x00' + b'\0' * 8, ("127.0.0.1", dns_port))
            response, _ = stream.recvfrom(4096)
            if not response.startswith(b'\x01\x02\x81\x83'):
                raise OSError("UDP observer did not respond")
    except OSError as exc:
        raise RetrievalPolicyError("Cannot verify macOS egress: network observer stopped responding.") from exc


def _macos_network_canaries(rpc, policy, role):
    """Verify literal IP, DNS, proxy and child-process egress without a model.

    Positive controls run the same commands outside Seatbelt first. Tool-ready
    checks inside Seatbelt prevent a missing executable from being mistaken for
    network denial. Parent-observed TCP/UDP counters also catch a connection
    that returned a nonzero exit status after sending data.
    """
    nonce = uuid.uuid4().hex
    ready = {
        "curlReady": "/usr/bin/curl --version",
        "ncReady": "/usr/bin/nc -h 2>&1 | /usr/bin/grep -q 'usage: nc'",
        "rubyReady": "/usr/bin/ruby --disable-gems -rsocket -e 'exit 0'",
        "dnsReady": "/usr/bin/dig -v",
        "shellReady": "/bin/zsh -f -c '/bin/bash --noprofile --norc -c true'",
    }
    with _local_network_controls() as (port, dns_port, contacts):
        probes = _macos_network_probe_commands(port, dns_port, nonce)
        expected_positive = {"nonce": nonce, **{name: True for name in probes}}
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
        try:
            positive = subprocess.run(_shell_boolean_report(nonce, probes),
                                      env=environment, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=15, check=False)
            if positive.returncode or json.loads(positive.stdout) != expected_positive:
                raise ValueError
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RetrievalPolicyError("Cannot verify macOS egress: network probe positive controls failed.") from exc
        _network_observer_barrier(port, dns_port)
        before = contacts()
        if not before["tcp"] or not before["udp"]:
            raise RetrievalPolicyError("Cannot verify macOS egress: local network controls were not observed.")
        result = rpc.call("command/exec", {
            "command": _shell_boolean_report(nonce, {**ready, **probes}),
            "cwd": str(role_workspace(policy, role)), "permissionProfile": PROFILE, "timeoutMs": 10000,
        })
        expected = {"nonce": nonce, **{name: True for name in ready}, **{name: False for name in probes}}
        try:
            report = json.loads(result.get("stdout", ""))
        except (ValueError, TypeError) as exc:
            raise RetrievalPolicyError("Historical macOS network canaries returned no valid evidence.") from exc
        _network_observer_barrier(port, dns_port)
        after_barrier = {name: count + 1 for name, count in before.items()}
        if result.get("exitCode") != 0 or report != expected or contacts() != after_barrier:
            raise RetrievalPolicyError("Historical macOS sandbox did not enforce IP, DNS, proxy and descendant network denial.")
        return {"networkProbes": {name: "denied" for name in probes},
                "positiveControls": True, "sandboxToolsReady": True,
                "unexpectedTcpContacts": 0, "unexpectedUdpContacts": 0}


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
    adapter = policy["isolation"]["adapter"]
    if adapter == WINDOWS_ADAPTER and os.name != "nt":
        raise RetrievalPolicyError("Historical isolation adapter requires Windows MXC; no fallback is allowed.")
    if adapter == MACOS_ADAPTER and sys.platform != "darwin":
        raise RetrievalPolicyError("Historical isolation adapter requires macOS Seatbelt; no fallback is allowed.")
    if adapter == MACOS_ADAPTER and not _native_macos_binary(Path(executable)):
        raise RetrievalPolicyError("Historical macOS execution requires a trusted native Mach-O Codex binary.")
    if adapter == MACOS_ADAPTER:
        host = Path(executable).resolve().with_name("codex-code-mode-host")
        _outside(host.resolve(), Path(policy["isolation"]["workspace"]).resolve(), "code-mode host")
        _outside(host.resolve(), role_workspace(policy, role).resolve(), "code-mode host")
        if not _native_macos_binary(host):
            raise RetrievalPolicyError("Historical macOS execution requires the trusted native codex-code-mode-host alongside Codex.")
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
