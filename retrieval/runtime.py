"""Fail-closed historical retrieval controls, independent of model prompts.

Only a controller-owned policy can configure the historical MCP server. Native
search follows the saved run mode and is disabled under a historical policy. An isolation status in JSON is never
accepted as proof: the exact Codex process must pass configuration and sandbox
canaries before a model turn is allowed.

A check that could not finish (no connectivity for the controls, a slow or
exited app-server, unreadable probe output) raises IsolationCheckUnavailable,
which callers retry; only a proven violation raises RetrievalPolicyError.
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
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
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
# The folder of the user's related works in the author workspace (ui/related_works.FOLDER).
RELATED_WORKS_FOLDER = "related-works"
SETTINGS_BOUNDARY_CANARIES = (".retrieval-delete-canary", ".retrieval-rename-canary")
BENCHMARK_FILES = ("input.md", "draft.md", "checked-statement.md")
BENCHMARK_DIRECTORIES = ("prompts",)
# Codex's ":minimal" also grants /tmp and /var/tmp (verified with Codex 0.160), where other runs keep
# files and live authors download papers: the macOS profile denies both again. (Codex reports
# "none" as "deny", so the exact value is "deny".)
SHARED_TEMP_DENIED = {":slash_tmp": "deny", "/private/var/tmp": "deny"}
# Instead, each macOS process of a run gets a private temp folder of its own (private_temp()):
# owner-only, made fresh in codex-home/tmp/ of the controller's folder (outside every workspace) and
# removed when the process ends. It is the process's TMPDIR, which ":tmpdir" lets commands write,
# and its commands' TMPDIR and TMPPREFIX (where zsh, which runs the model's commands, writes
# heredocs), set for that process alone by session flags (temp_overrides()) and, since a thread's
# shell environment replaces them, in the author's thread settings too.
PRIVATE_TEMP_GRANT = {":tmpdir": "write"}
PRIVATE_TEMP_FOLDER = "tmp"
PRIVATE_TEMP_NAME = re.compile(r"(?:author|structured|critic)-(\d+)-[A-Za-z0-9_]{8}\Z")
PUBLIC_EGRESS_CANARY = ("1.1.1.1", 443)
WINDOWS_ADAPTER = "codex-windows-mxc-v1"
MACOS_ADAPTER = "codex-macos-seatbelt-v1"
SETTINGS_READ_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
BUNDLED_SKILLS_FILE = ".bundled-skills.json"
SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
LOGIN_HOME_ENV = "TCS_PROVER_UI_AUTH_HOME"
BROKER_SERVER_KEYS = frozenset({"command", "args", "required", "env_vars", "tool_timeout_sec", "enabled_tools",
                                "tools", "startup_timeout_sec"})
# Critic session folders kept for audit (each attempt makes one); older ones
# beyond this count are removed once they are a day old.
CRITIC_SESSIONS_KEPT = 50
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


class NeedsAttention(RetrievalPolicyError):
    """A fixed problem that no retry solves and no isolation check found.

    For example a moved or missing policy, a pinned discovery binary with no
    verified replacement, or an isolated config holding settings TCS Prover
    never writes. The controller holds the run and names the problem instead
    of retrying it every minute.
    """


class IsolationCheckUnavailable(OSError):
    """An isolation check could not finish, so it proved nothing either way.

    No connectivity for the positive controls, a slow or exited app-server,
    unreadable probe output, or a native binary missing during an update. Like
    any OSError it is retried; a proven violation is a RetrievalPolicyError.
    """


class SettingsUnavailable(RetrievalPolicyError):
    """The saved job settings cannot be read right now (an I/O error or an unfinished write).

    Nothing starts without them; unlike a setting that is wrong, a later
    attempt may read them, so the caller retries.
    """


def _live_mode():
    """The live search mode the controller gave this process, or None (historical runs and direct CLI use)."""

    mode = os.environ.get(MODE_ENV)
    return mode if mode in ("live", "disabled") and not os.environ.get(POLICY_ENV) else None


def _cross_check(settings_path):
    """Saved settings of a live run, read only to confirm it is not historical ({} when unreadable)."""

    try:
        return _read_saved_settings(settings_path, "Cannot verify saved run retrieval settings.")
    except RetrievalPolicyError:
        return {}


def search_mode(workspace=None, *, policy=None):
    """Keep a launched job's mode fixed, preserving main's native-search default.

    Historical controller policies always take precedence. A live run trusts
    the mode its controller supplied and reads job-settings.json only as a
    cross-check that the run is not historical: an unreadable file does not
    stop it. A fresh CLI invocation without saved job settings uses native
    live search, matching the UI default.
    """
    settings_path = Path(workspace or Path.cwd()).resolve() / "job-settings.json"
    saved = None
    live = None if policy else _live_mode()
    if settings_path.exists() or settings_path.is_symlink():
        saved = (_cross_check(settings_path) if live
                 else _read_saved_settings(settings_path, "Cannot verify saved run retrieval settings."))
    if live:
        if (saved or {}).get("retrievalPolicyPath") or (saved or {}).get("retrievalMode") == "historical":
            raise RetrievalPolicyError("A live search mode was supplied for a historical run; no model was started.")
        return live
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
    # Also seen while Codex updates itself: a later attempt finds it again.
    raise IsolationCheckUnavailable(f"Historical execution requires an absolute native {name} outside the run workspace; script shims are not supported.")


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
    except FileNotFoundError as exc:
        raise NeedsAttention(f"The historical retrieval policy {path} is missing; restore its controller folder "
                             "or move the checkout back, then resume the run.") from exc
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
        missing = broker["command"] if not Path(broker["command"]).is_file() else broker["policyFile"]
        raise NeedsAttention(f"Historical broker executable or policy file is missing: {missing}. Restore it, "
                             "then resume the run.")
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
        raise NeedsAttention(f"Historical broker script is missing: {script}. Restore the checkout, then resume the run.")
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
        from retrieval.historical_codex_discovery import DiscoveryError, validate_settings
        from retrieval.historical_transport import HistoricalSearchError
        try:
            try:
                validate_settings(broker_policy.get('discovery'), workspace=workspace)
            except DiscoveryError as exc:
                if exc.code != 'discovery_binary':
                    raise
                # Only discovery runs this binary: other roles continue, and the
                # author's preflight re-pins it or holds the run (preflight()).
                value['_discoveryProblem'] = str(exc)
                validate_settings(broker_policy.get('discovery'), workspace=workspace, require_binary=False)
            discovery = dict(broker_policy['discovery'])
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
            raise SettingsUnavailable(error) from exc
        except OSError as exc:
            raise SettingsUnavailable(error) from exc
        if not isinstance(value, dict):
            raise RetrievalPolicyError(error)
        return value


def policy_for_workspace(workspace=None):
    """Use the trusted child environment, with a saved-run fallback for CLI use."""
    workspace = Path(workspace or Path.cwd()).resolve()
    settings = {}
    settings_path = workspace / "job-settings.json"
    if settings_path.is_file() or settings_path.is_symlink():
        # A live run trusts its controller: an unreadable file is only a skipped cross-check.
        settings = (_cross_check(settings_path) if _live_mode()
                    else _read_saved_settings(settings_path, "Cannot verify saved run retrieval settings."))
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
                # The user's related works: read-only inputs (a folder run hard-links one copy into every job).
                RELATED_WORKS_FOLDER: "read",
                PROTECTED_SETTINGS: "read",
                **{name: "read" for name in SETTINGS_BOUNDARY_CANARIES},
                **{name: "read" for name in (*BENCHMARK_FILES, *BENCHMARK_DIRECTORIES)},
            }, **({**SHARED_TEMP_DENIED, **PRIVATE_TEMP_GRANT}
                  if policy["isolation"]["adapter"] == MACOS_ADAPTER else {})},
            "network": {"enabled": False},
        }},
        "mcp_servers": {},
        "skills": {"config": [{"path": str(role_home(policy, role) / "skills/.system" / name / "SKILL.md"),
                               "enabled": False} for name in bundled_skills(policy)]},
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


def bundled_skills(policy):
    """System skills to turn off: the known ones and those a Codex update added.

    Codex installs its bundled skills into each home when it starts; a name not
    yet listed is recorded by verify_rpc() and disabled from the next start.
    """
    names = set(SYSTEM_SKILLS)
    path = Path(policy["isolation"]["codexHome"]) / BUNDLED_SKILLS_FILE
    try:
        if not path.is_symlink() and path.is_file() and path.stat().st_size <= 64 * 1024:
            recorded = json.loads(path.read_text(encoding="utf-8")).get("names")
            if isinstance(recorded, list):
                names.update(name for name in recorded if isinstance(name, str) and SKILL_NAME.fullmatch(name))
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return sorted(names)


def _record_bundled_skills(policy, names):
    home = Path(policy["isolation"]["codexHome"])
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = home / f".{BUNDLED_SKILLS_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        recorded = (set(bundled_skills(policy)) | set(names)) - set(SYSTEM_SKILLS)
        temporary.write_text(json.dumps({"schemaVersion": 1, "names": sorted(recorded)}) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(home / BUNDLED_SKILLS_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def controller_audit(policy, event, **fields):
    """Append one controller record (a migration, a re-pin) to the run's broker audit log.

    Raises IsolationCheckUnavailable when the log refuses it, so the change it
    records waits for a later attempt instead of happening unrecorded.
    """
    try:
        provider = json.loads(Path(policy["broker"]["policyFile"]).read_text(encoding="utf-8-sig")).get("provider")
        cutoff = datetime.fromisoformat(policy["cutoffExclusiveUtc"].replace("Z", "+00:00"))
        record = {"event": event, "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "cutoff": cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "provider": provider, "source": "controller", **fields}
        descriptor = os.open(policy["broker"]["auditLog"], os.O_WRONLY | os.O_APPEND | os.O_CREAT
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, UnicodeError, ValueError, KeyError, AttributeError) as exc:
        raise IsolationCheckUnavailable("The historical audit log did not accept a controller record; "
                                        "checking again.") from exc


def _login_source():
    """The user's own Codex login, read exactly as the launch preflight reads it."""
    home = os.environ.get(LOGIN_HOME_ENV) or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home).expanduser().resolve() / "auth.json"


def _login(path):
    """(stamp, bytes) of a valid login, or None; never follows links."""
    from retrieval.historical_discovery_auth import _read_auth
    try:
        raw, _ = _read_auth(path)
    except (OSError, ValueError, UnicodeError):
        return None
    try:
        refreshed = json.loads(raw).get("last_refresh")
        stamp = datetime.fromisoformat(refreshed.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        stamp = path.stat().st_mtime
    return stamp, raw


def sync_login(policy, targets, source=None):
    """Give isolated homes the newest valid Codex login before a process starts.

    Candidates are the user's current login (checked as at launch) and each
    isolated copy; the newest by Codex's ``last_refresh`` wins, so a login the
    user renewed, or a refresh one role made, reaches the other roles on their
    next attempt. The user's own file is only read, never written.
    """
    homes = [Path(policy["isolation"]["codexHome"]) / role for role in ("author", "structured")]
    if policy.get("_discoverySettings"):
        homes.append(Path(policy["_discoverySettings"]["auth_home"]))
    candidates = [Path(source) if source else _login_source(), *(home / "auth.json" for home in homes)]
    logins = {path: _login(path) for path in candidates if os.path.lexists(path)}
    valid = [(login[0], str(path), login[1]) for path, login in logins.items() if login is not None]
    if not valid:
        return
    stamp, _, raw = max(valid)
    for target in targets:
        target = Path(target) / "auth.json"
        current = logins.get(target)
        if target.is_symlink() or (current is not None and (current[1] == raw or current[0] >= stamp)):
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(f".auth-sync-{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


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


def prune_folders(parent, pattern, keep, minimum_age=24 * 3600):
    """Remove the folders matching ``pattern`` beyond the newest ``keep`` that are older than ``minimum_age`` seconds."""

    try:
        folders = [path for path in Path(parent).glob(pattern) if path.is_dir() and not path.is_symlink()]
        folders.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    cutoff = time.time() - minimum_age
    for path in folders[keep:]:
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


@contextmanager
def critic_session(policy):
    """Create one auth-only home and read-only snapshot workspace per attempt.

    The caller writes the selected proof/statement snapshots into role_workspace
    before preflight, and keeps this context open until the critic process ends.
    Sessions remain private on disk for audit (the newest CRITIC_SESSIONS_KEPT,
    and any younger than a day); their capability expires on exit, and the copy
    of the login is deleted as soon as its refresh is preserved.
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
        if (base / "structured").is_dir() and not (base / "structured").is_symlink():
            # The newest login (the user's renewed one, or another role's refresh) first.
            try:
                sync_login(verified, [base / "structured"])
            except OSError:
                pass  # The existing copy is checked below as before.
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
        prune_folders(sessions, "session-*", CRITIC_SESSIONS_KEPT)
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
            # The session keeps its record for audit, but no copy of the login.
            try:
                (home / "auth.json").unlink(missing_ok=True)
            except OSError:
                pass


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
    skills = actual.get("skills") if isinstance(actual, dict) else None
    if skills != expected["skills"] and _disabled_system_skills(policy, role, skills):
        if refresh_environment:
            # Codex bundled a new skill since this config was written: turn it off too.
            actual = {**actual, "skills": expected["skills"]}
            refreshed = True
        else:
            # This process started before a newly bundled skill was recorded.
            expected["skills"] = skills
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
            if _exact_configuration(_trust_record(policy, role), actual["projects"]):
                return True
    raise RetrievalPolicyError(f"{label} has changed; re-prepare the run.")


def _trust_record(policy, role):
    """The [projects] table Codex itself writes into a role's config: trust in the run's project.

    Codex 0.160 writes it at an author's thread/start; the next start
    removes it (_verify_saved_configuration()).
    """
    return {str(_project_trust_root(policy, role)): {"trust_level": "trusted"}}


def _disabled_system_skills(policy, role, value):
    """A skills table that only turns off bundled system skills of this role's home."""
    try:
        entries = value["config"]
        if set(value) != {"config"} or not isinstance(entries, list):
            return False
        root, names = role_home(policy, role) / "skills/.system", set()
        for entry in entries:
            path = Path(entry["path"])
            if (set(entry) != {"path", "enabled"} or entry["enabled"] is not False or path.name != "SKILL.md"
                    or path.parent.parent != root or not SKILL_NAME.fullmatch(path.parent.name)):
                return False
            names.add(path.parent.name)
        return names >= set(SYSTEM_SKILLS)
    except (KeyError, TypeError, AttributeError, RetrievalPolicyError):
        return False


_MISSING = object()


def _config_differences(policy, role, actual):
    """Key paths where a saved config differs from ours, and those a migration may not touch.

    A controller or Codex update adds settings, retires disabled features,
    changes the bundled-skill list or the broker's timeouts. Any other change
    (an enabled feature, network, approval, a foreign table) is not migrated.
    A shell environment of system variables only (a PATH of an earlier
    controller start) is replaced exactly, and Codex's own trust record of
    the run's project (_trust_record()) is removed, as at every start.
    """
    changed, blocking = [], []
    saved_shell = actual.get("shell_environment_policy") if isinstance(actual, dict) else None
    refreshable = _system_environment_policy(saved_shell)
    try:
        trusted = isinstance(actual, dict) and _exact_configuration(_trust_record(policy, role), actual.get("projects"))
    except RetrievalPolicyError:
        trusted = False  # No verifiable project identity: the table is not Codex's record.

    def walk(want, have, path):
        if isinstance(want, dict) and isinstance(have, dict):
            for key in sorted(set(want) | set(have), key=str):
                walk(want.get(key, _MISSING), have.get(key, _MISSING), (*path, str(key)))
            return
        if want is not _MISSING and have is not _MISSING and _exact_configuration(want, have):
            return
        name = ".".join(path)
        changed.append(name)
        # The shell environment has its own exact refresh (system variables only).
        shell = path[:1] == ("shell_environment_policy",)
        added = have is _MISSING and not shell
        retired = want is _MISSING and len(path) == 2 and path[0] == "features" and have is False
        tuned = (path[:1] == ("skills",)
                 or (path[:2] == ("mcp_servers", BROKER_NAME) and len(path) == 3
                     and path[2] in ("tool_timeout_sec", "startup_timeout_sec")))
        if not (added or retired or tuned or (shell and refreshable) or (trusted and path == ("projects",))):
            blocking.append(name)

    walk(configuration(policy, role), actual if isinstance(actual, dict) else {"": actual}, ())
    return changed, blocking


def _verify_saved_configuration(policy, role, config, expected):
    if not config.is_file() or config.is_symlink():
        raise RetrievalPolicyError("Isolated Codex configuration must be a regular file.")
    original = config.read_bytes()
    try:
        actual = tomllib.loads(original.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise NeedsAttention(f"Isolated Codex configuration has changed and cannot be read: {config}. "
                             "Inspect it, then resume the run.") from exc
    try:
        rewrite = _validate_user_configuration(policy, role, actual, refresh_environment=True)
    except RetrievalPolicyError:
        changed, blocking = _config_differences(policy, role, actual)
        if blocking:
            raise NeedsAttention(f"Isolated Codex configuration has changed in ways TCS Prover does not write "
                                 f"({', '.join(blocking[:6])}); inspect {config} and remove them, then resume the "
                                 "run.") from None
        # Written by an earlier TCS Prover or for an earlier Codex: migrate it,
        # recorded before the change is made.
        controller_audit(policy, "isolated_config_migrated", role=role, changed_keys=changed[:64],
                         previous_sha256=hashlib.sha256(original).hexdigest(),
                         sha256=hashlib.sha256(expected.encode("utf-8")).hexdigest())
        rewrite = True
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


def private_temp(policy, role):
    """A fresh private temp folder for one macOS process of ``role`` (None for other adapters).

    Owner-only, never a link, made in codex-home/tmp/ of the controller's
    folder (outside every workspace) and named <role>-<PID>-<8 characters>:
    first the folders of processes that ended are removed. The caller
    removes it when its process ends (remove_private_temp()). A folder that
    cannot be made is checked again; a parent that is a link or another
    user's is not a controller folder.
    """
    if policy["isolation"]["adapter"] != MACOS_ADAPTER:
        return None
    parent = Path(policy["isolation"]["codexHome"]) / PRIVATE_TEMP_FOLDER
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RetrievalPolicyError(f"Historical private temp folders need a controller folder {parent}; inspect "
                                       "and remove it, then resume the run.")
        if stat.S_IMODE(info.st_mode) != 0o700:
            parent.chmod(0o700)
        for entry in parent.iterdir():
            match = PRIVATE_TEMP_NAME.match(entry.name)
            if not match or int(match.group(1)) == os.getpid():
                continue
            try:
                entry_info = entry.lstat()
                if stat.S_ISDIR(entry_info.st_mode) and entry_info.st_uid == os.getuid():
                    os.kill(int(match.group(1)), 0)
            except ProcessLookupError:
                shutil.rmtree(entry, ignore_errors=True)  # Its process ended without removing it.
            except OSError:
                pass
        temp = Path(os.path.realpath(tempfile.mkdtemp(prefix=f"{role}-{os.getpid()}-", dir=parent)))
    except OSError as exc:
        raise IsolationCheckUnavailable(f"Cannot prepare a private temp folder ({exc}); checking again.") from exc
    try:
        _outside(temp, role_workspace(policy, role).resolve(), "private temp folder")
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return temp


def remove_private_temp(policy, environment):
    """Remove the private temp folder (private_temp()) named by the TMPDIR of a process that ended.

    Nothing else: a live run (no ``policy``) or a path outside codex-home/tmp/
    of this run's controller folder is left alone.
    """
    temp = environment.get("TMPDIR") if policy and isinstance(environment, dict) else None
    home = (policy.get("isolation") or {}).get("codexHome") if temp else None
    if not home:
        return
    path = Path(temp)
    parent = Path(os.path.realpath(Path(home) / PRIVATE_TEMP_FOLDER))
    if path.parent == parent and PRIVATE_TEMP_NAME.match(path.name) and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)


def _verified_private_temp(policy, role, environment):
    """The private temp folder that the TMPDIR of a macOS process's ``environment`` names (None for other adapters).

    It must be one that private_temp() made for this ``role`` in this
    process: TMPDIR is its real path in codex-home/tmp/ of the controller's
    folder, named <role>-<this PID>-<8 characters>, outside the role's
    workspace (anything else, such as no folder or the folder that holds
    every process's, is a violation), and on disk a folder of this user
    that is not a link (a violation too: the grant would follow a link) and
    is owner-only. Its commands cannot remove, rename or replace it, but
    they can change its mode (verified with Codex 0.160): a folder that is
    gone, is no folder or is not owner-only is checked again, and the next
    process gets a new one.
    """
    if policy["isolation"]["adapter"] != MACOS_ADAPTER:
        return None
    temp = environment.get("TMPDIR") if isinstance(environment, dict) else None
    parent = os.path.realpath(Path(policy["isolation"]["codexHome"]) / PRIVATE_TEMP_FOLDER)
    match = PRIVATE_TEMP_NAME.match(os.path.basename(temp)) if isinstance(temp, str) else None
    if (not match or os.path.dirname(temp) != parent or not os.path.basename(temp).startswith(f"{role}-")
            or int(match.group(1)) != os.getpid()):
        raise RetrievalPolicyError("Historical isolation requires the process's own private temp folder.")
    path = Path(temp)
    _outside(path, role_workspace(policy, role).resolve(), "private temp folder")
    try:
        info = path.lstat()
    except OSError as exc:
        raise IsolationCheckUnavailable(f"The process's private temp folder {path} is gone ({exc}); checking "
                                        "again.") from exc
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise RetrievalPolicyError(f"Historical isolation requires the process's own private temp folder; {path} is "
                                   "a link or another user's.")
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise IsolationCheckUnavailable(f"The process's private temp folder {path} is no longer an owner-only "
                                        "folder; checking again.")
    return path


# Codex 0.160's own subagent limits, verified with its binary, `codex debug prompt-input` and `codex exec`
# against a local stand-in for the model API (no model was started):
# - features.multi_agent_v2.max_concurrent_threads_per_session: how many agent threads of one session may be
#   active at once, the root included and subagents of subagents too (models of multi-agent version 2: GPT 6.x,
#   GPT 5.6 Sol and Terra). 1 refuses every spawn ("collab spawn failed: agent thread limit reached"); a finished
#   subagent frees its slot. Codex lists its collaboration tools to these models even with multi_agent off.
# - agents.max_concurrent_threads_per_session (also read as agents.max_threads): how many subagents may be
#   active at once (models of version 1, such as API models). agents.max_depth = 1 gives their subagents no
#   subagent tool; version 2 ignores max_depth. Turning multi_agent off removes the version 1 tools.
# - features.multi_agent_v2.multi_agent_mode_hint_text replaces the delegation hint (the <multi_agent_mode>
#   developer message) of the root agent and of its subagents.
def agent_limits(count, hint=""):
    """The settings that let one Codex process run at most ``count`` subagents at a time (0: none at all).

    None leaves Codex's own defaults (a workflow node that declares no count).
    """
    if count is None:
        return {}
    multi_agent_v2 = {"max_concurrent_threads_per_session": count + 1}
    if count and hint:
        multi_agent_v2["multi_agent_mode_hint_text"] = hint
    limits = {"features": {"multi_agent_v2": multi_agent_v2}}
    if count:
        limits["agents"] = {"max_concurrent_threads_per_session": count, "max_depth": 1}
    return limits


def agent_limit_arguments(limits):
    """The `-c` flags that apply agent_limits() to one Codex process."""

    def flags(value, prefix):
        for key, item in value.items():
            if isinstance(item, dict):
                yield from flags(item, f"{prefix}{key}.")
            else:
                yield from ("-c", f"{prefix}{key}={_toml_value(item)}")

    return list(flags(limits or {}, ""))


def _merged(base, extra):
    """``base`` with the nested tables of ``extra`` merged in (both unchanged)."""
    result = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        result[key] = (_merged(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict)
                       else copy.deepcopy(value))
    return result


def _check_agent_limits(config, limits):
    """Require exactly the audited subagent limits (agent_limits()) in Codex's effective settings, or none."""
    expected_v2 = ((limits or {}).get("features") or {}).get("multi_agent_v2")
    actual_v2 = (config.get("features") or {}).get("multi_agent_v2")
    if expected_v2 is None:
        if actual_v2 is not False:
            raise RetrievalPolicyError("Historical isolation mismatch: config.features.multi_agent_v2.")
    elif not _exact_configuration({"enabled": False, **expected_v2}, actual_v2):
        # The generated config turns the feature off; the session flags add exactly these limits.
        raise RetrievalPolicyError("Historical isolation rejects unexpected subagent limits.")
    actual_agents = config.get("agents")
    if isinstance(actual_agents, dict):
        actual_agents = {key: value for key, value in actual_agents.items() if value is not None}
    if not _exact_configuration((limits or {}).get("agents") or {}, actual_agents or {}):
        raise RetrievalPolicyError("Historical isolation rejects inherited agents.")


def temp_overrides(policy, environment):
    """The session settings that give a macOS process's commands its private temp folder ({} without one).

    TMPDIR, and TMPPREFIX for zsh's heredocs, as the TMPDIR of the
    process's own environment names it (isolated_environment()).
    """
    temp = environment.get("TMPDIR") if isinstance(environment, dict) else None
    if not temp or (policy.get("isolation") or {}).get("adapter") != MACOS_ADAPTER:
        return {}
    return {"shell_environment_policy": {"set": {"TMPDIR": temp, "TMPPREFIX": os.path.join(temp, "zsh")}}}


def temp_arguments(policy, environment):
    """The `-c` flags that apply temp_overrides() to one Codex process."""
    values = temp_overrides(policy, environment).get("shell_environment_policy", {}).get("set", {})
    return [part for name, value in values.items()
            for part in ("-c", f"shell_environment_policy.set.{name}={_toml_value(value)}")]


def isolated_environment(policy, role, inherited, temp=None):
    """Ignore user runtime overrides; shell children receive no provider secrets.

    With a private ``temp`` folder (private_temp()) the process's TMPDIR and
    TMPPREFIX name it: Codex grants it as ":tmpdir" (PRIVATE_TEMP_GRANT).
    """
    home = role_home(policy, role)
    home.mkdir(parents=True, exist_ok=True)
    role_workspace(policy, role).mkdir(parents=True, exist_ok=True)
    verify_protected_directories(policy, role, create=True)
    if role in ("author", "structured"):
        # A renewed or refreshed login reaches this role's next process; the
        # author also carries discovery's login for the broker it starts.
        sync_login(policy, [home, *([Path(policy["_discoverySettings"]["auth_home"])]
                                    if role == "author" and policy.get("_discoverySettings") else [])])
    expected = config_text(policy, role)
    config = home / "config.toml"
    if config.exists() or config.is_symlink():
        _verify_saved_configuration(policy, role, config, expected)
    else:
        config.write_text(expected, encoding="utf-8")
    environment = {key: value for key, value in inherited.items() if key.upper() in {k.upper() for k in SYSTEM_ENV}}
    environment.update(CODEX_HOME=str(home), HOME=str(home), USERPROFILE=str(home),
                       RUST_LOG="error", TCS_PROVER_RETRIEVAL_POLICY=policy["_path"])
    if temp is not None:
        environment.update(TMPDIR=str(temp), TMPPREFIX=os.path.join(str(temp), "zsh"))
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


def verify_rpc(rpc, policy, role, environment, allow_runtime_overrides=False, agent_limits=None):
    """No model calls: inspect effective config and exercise the actual sandbox.

    On macOS the process must have its own private temp folder: its
    ``environment`` names it (_verified_private_temp()) and its session
    flags are exactly temp_overrides() (``allow_runtime_overrides``, the
    author's own app-server, also has its search and feature flags there),
    plus exactly the subagent limits ``agent_limits`` (agent_limits()), whose
    values are checked exactly in the effective settings too.
    """
    workspace = str(role_workspace(policy, role))
    temp = _verified_private_temp(policy, role, environment)
    overrides = _merged(temp_overrides(policy, environment), agent_limits)
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
    if isinstance(config, dict) and _disabled_system_skills(policy, role, config.get("skills")):
        # This process started before a newly bundled skill was recorded.
        expected["skills"] = config["skills"]
    if allow_runtime_overrides and (config.get("features") or {}).get("multi_agent") is False:
        # Reviewer, critic and final stages remove delegation; the author retains
        # its default subagents with the parent's immutable permissions.
        expected["features"]["multi_agent"] = False
    # Codex 0.160 serializes tools.web_search=false as null after normalizing
    # it into the disabled top-level mode. All other values remain exact.
    if config.get("web_search") == "disabled" and (config.get("tools") or {}).get("web_search", True) is None:
        expected["tools"]["web_search"] = None
    if role == "author" and not expected["mcp_servers"][BROKER_NAME]["env_vars"]:
        actual_broker = (config.get("mcp_servers") or {}).get(BROKER_NAME) or {}
        if "env_vars" not in actual_broker:
            expected["mcp_servers"][BROKER_NAME].pop("env_vars")
    # The commands' private temp folder, added by this process's session flags.
    expected["shell_environment_policy"]["set"].update(overrides.get("shell_environment_policy", {}).get("set", {}))
    # The subagent limits are checked exactly below (_check_agent_limits).
    expected["features"].pop("multi_agent_v2")
    _subset(expected, config)
    _check_agent_limits(config, agent_limits)
    for key in ("sandbox_mode", "sandbox_workspace_write", "instructions", "developer_instructions",
                "model_instructions_file", "experimental_compact_prompt_file", "compact_prompt",
                "hooks", "plugins", "marketplaces", "orchestrator", "notify",
                "model_catalog_json", "js_repl_node_module_dirs"):
        if config.get(key):
            raise RetrievalPolicyError(f"Historical isolation rejects inherited {key}.")
    actual_servers = config.get("mcp_servers") or {}
    if set(actual_servers) != ({BROKER_NAME} if role == "author" else set()):
        raise RetrievalPolicyError("Historical isolation rejects inherited MCP servers.")
    profile = config["permissions"][PROFILE]
    # Only the generated entries (their values were compared above), e.g. the /tmp and /var/tmp denials.
    allowed = set(configuration(policy, role)["permissions"][PROFILE]["filesystem"]) | {"glob_scan_max_depth"}
    if set(profile.get("filesystem", {})) - allowed:
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
                and not (source.get("type") == "sessionFlags"
                         and (allow_runtime_overrides or _exact_configuration(overrides, layer["config"])))):
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
        raise IsolationCheckUnavailable("Historical isolation cannot verify the MCP inventory; checking again.")
    names = {row.get("name") for row in servers.get("data") or []}
    if servers.get("nextCursor") or names != ({BROKER_NAME} if role == "author" else set()):
        raise RetrievalPolicyError("Historical isolation found missing or unexpected live MCP servers.")
    for server in servers.get("data") or []:
        if (server.get("toolsError") and not server.get("tools") and not server.get("resources")
                and not server.get("resourceTemplates")):
            # The broker itself failed to start: nothing extra is exposed.
            raise IsolationCheckUnavailable("The historical search broker did not start; checking again.")
        if (set(server.get("tools") or {}) != set(BROKER_TOOLS)
                or server.get("resources") or server.get("resourceTemplates") or server.get("toolsError")):
            raise RetrievalPolicyError("Historical broker has missing or unexpected tools.")
    skills = rpc.call("skills/list", {"cwds": [workspace], "forceReload": True})
    if not isinstance(skills.get("data"), list) or not skills["data"]:
        raise IsolationCheckUnavailable("Historical isolation cannot verify the skills inventory; checking again.")
    added = set()
    for row in skills.get("data") or []:
        if row.get("errors"):
            raise RetrievalPolicyError("Historical isolation rejects enabled or unreadable skills.")
        for skill in row.get("skills", []):
            if skill.get("enabled") is False:
                continue
            name = _new_bundled_skill(policy, role, skill)
            if name is None:
                raise RetrievalPolicyError("Historical isolation rejects enabled or unreadable skills.")
            added.add(name)
    if added:
        # A Codex update bundled another system skill: record it so the next
        # start turns it off, and start no model turn with it on.
        _record_bundled_skills(policy, added)
        try:
            controller_audit(policy, "bundled_skill_disabled", role=role, names=sorted(added))
        except IsolationCheckUnavailable:
            pass
        raise IsolationCheckUnavailable(f"Codex added the bundled skill {', '.join(sorted(added))}; it is turned "
                                        "off before the next start.")
    _sandbox_canaries(rpc, policy, role, temp)
    if policy["isolation"]["adapter"] == MACOS_ADAPTER:
        _macos_network_canaries(rpc, policy, role)


def _new_bundled_skill(policy, role, skill):
    """The name of an enabled skill Codex bundled into this home and we do not yet disable."""
    try:
        path = Path(skill["path"])
        root = role_home(policy, role) / "skills/.system"
        if (path.is_absolute() and path.name == "SKILL.md" and path.parent.parent == root
                and SKILL_NAME.fullmatch(path.parent.name) and path.parent.name not in bundled_skills(policy)):
            return path.parent.name
    except (KeyError, TypeError, ValueError, RetrievalPolicyError):
        pass
    return None


def _canary_verdict(report, expected, label, violation):
    """Raise for a proven violation; IsolationCheckUnavailable when evidence is missing.

    A denial that did not hold (a write, read or connection that should have
    failed) is a violation. Malformed output, or a positive control that failed
    (the sandbox blocked even allowed work), proves nothing either way.
    """
    if not isinstance(report, dict) or set(report) != set(expected) or report.get("nonce") != expected["nonce"]:
        raise IsolationCheckUnavailable(f"{label} returned no valid evidence; checking again.")
    if any(report[key] is not False for key, value in expected.items() if value is False):
        raise RetrievalPolicyError(violation)
    if report != expected:
        raise IsolationCheckUnavailable(f"{label} could not complete its positive controls; checking again.")


def _sandbox_canaries(rpc, policy, role, temp=None):
    """Prove allowed I/O works, external reads fail, and local TCP egress fails.

    On macOS also that /tmp and /var/tmp can be neither read nor written
    (SHARED_TEMP_DENIED): the controller puts a file in each, which the
    command must not read, and the command must not create one there. With
    the process's private ``temp`` folder (private_temp()), that the command
    can write a file through its TMPDIR and read it there, that its
    TMPPREFIX is in it, and that it cannot read a controller file in
    codex-home/tmp/, the folder that holds the folders of other processes
    (so a TMPDIR naming that folder, or one that holds it, fails too).
    """
    nonce = uuid.uuid4().hex
    macos = policy["isolation"]["adapter"] == MACOS_ADAPTER
    shared = (("slashTmp", Path("/private/tmp")), ("varTmp", Path("/private/var/tmp"))) if macos else ()
    shared_temp = [(name, folder / f".retrieval-tmp-canary-{nonce}", folder / f".retrieval-tmp-write-{nonce}")
                   for name, folder in shared]
    private = ((Path(temp), Path(temp) / f".retrieval-temp-canary-{nonce}",
                Path(os.path.realpath(Path(policy["isolation"]["codexHome"]) / PRIVATE_TEMP_FOLDER))
                / f".retrieval-temp-neighbor-{nonce}") if macos and temp is not None else None)
    created_shared = []
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
        for readable in (*(readable for _, readable, _ in shared_temp), *([private[2]] if private else [])):
            try:
                descriptor = os.open(readable, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                     0o600)
            except OSError as exc:
                raise IsolationCheckUnavailable(f"Cannot prepare the {readable.parent} canary; checking again.") from exc
            created_shared.append(readable)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(nonce)
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
                renamed_canary, outside, port, shared_temp, private,
            )
        result = rpc.call("command/exec", {
            "command": command,
            "cwd": str(role_workspace(policy, role)), "permissionProfile": PROFILE, "timeoutMs": 10000,
        })
        try:
            report = json.loads(result.get("stdout", ""))
        except (TypeError, ValueError) as exc:
            raise IsolationCheckUnavailable("Historical sandbox canary returned no valid evidence; checking again.") from exc
        try:
            _public_network_control()
            offline = None
        except IsolationCheckUnavailable as exc:
            offline = exc
        expected = {"nonce": nonce, "insideOk": True, "insideWrite": role == "author", "protectedWrite": False, "settingsWrite": False, "settingsDelete": False, "settingsRename": False, "settingsCanariesRead": True, "outsideRead": False, "network": False, "publicNetwork": False,
                    **{name + access: False for name, _, _ in shared_temp for access in ("Read", "Write")},
                    **({"privateTempWrite": True, "privateTempNeighborRead": False} if private else {})}
        # A denial that did not hold is a violation whatever the connectivity.
        _canary_verdict(report, expected, "Historical sandbox canary",
                        "Historical sandbox did not enforce workspace I/O, outside-read denial (including /tmp and "
                        "/var/tmp and other private temp folders) and TCP denial.")
        if offline is not None:
            raise offline
        if result.get("exitCode") != 0:
            raise IsolationCheckUnavailable("Historical sandbox canary exited abnormally; checking again.")
    finally:
        listener.close()
        outside.unlink(missing_ok=True)
        inside.unlink(missing_ok=True)
        protected.unlink(missing_ok=True)
        for sentinel in created_sentinels:
            sentinel.unlink(missing_ok=True)
        renamed_canary.unlink(missing_ok=True)
        for path in (*created_shared, *(written for _, _, written in shared_temp), *([private[1]] if private else [])):
            # Only this check's own files: never a link someone else put there.
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)


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
                          rename_canary, renamed_canary, outside, port, shared_temp=(), private=None):
    """Use only Apple system tools, with positive controls for every probe.

    No model is involved. Opening settings for append does not write or truncate
    their contents; deletion and rename target disposable protected sentinels.
    ``shared_temp`` holds (name, a controller file to read, a file to create)
    for /tmp and /var/tmp, probed as <name>Read and <name>Write. ``private``
    holds (the private temp folder, a file to create there through $TMPDIR,
    a controller file in codex-home/tmp/), probed as privateTempWrite and
    privateTempNeighborRead.
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
    for name, readable, written in shared_temp:
        probes[name + "Read"] = f"/bin/cat {q(readable)}"
        probes[name + "Write"] = f"printf %s {q(nonce)} > {q(written)}"
    if private:
        temp, written, neighbor = private
        probes["privateTempWrite"] = (f'printf %s {q(nonce)} > "$TMPDIR"/{q(written.name)} && '
                                      f'test "$(/bin/cat {q(written)})" = {q(nonce)} && '
                                      f'test "$TMPPREFIX" = {q(temp / "zsh")}')
        probes["privateTempNeighborRead"] = f"/bin/cat {q(neighbor)}"
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
        # Offline, a VPN switch or a captive portal: denial cannot be shown now.
        raise IsolationCheckUnavailable("Cannot verify public network isolation: controller connectivity canary "
                                        "failed; checking again.") from exc


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
        raise IsolationCheckUnavailable("Cannot verify macOS egress: network observer stopped responding; "
                                        "checking again.") from exc


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
            raise IsolationCheckUnavailable("Cannot verify macOS egress: network probe positive controls failed; "
                                            "checking again.") from exc
        _network_observer_barrier(port, dns_port)
        before = contacts()
        if not before["tcp"] or not before["udp"]:
            raise IsolationCheckUnavailable("Cannot verify macOS egress: local network controls were not observed; "
                                            "checking again.")
        result = rpc.call("command/exec", {
            "command": _shell_boolean_report(nonce, {**ready, **probes}),
            "cwd": str(role_workspace(policy, role)), "permissionProfile": PROFILE, "timeoutMs": 10000,
        })
        expected = {"nonce": nonce, **{name: True for name in ready}, **{name: False for name in probes}}
        try:
            report = json.loads(result.get("stdout", ""))
        except (ValueError, TypeError) as exc:
            raise IsolationCheckUnavailable("Historical macOS network canaries returned no valid evidence; "
                                            "checking again.") from exc
        _network_observer_barrier(port, dns_port)
        after_barrier = {name: count + 1 for name, count in before.items()}
        if contacts() != after_barrier:
            raise RetrievalPolicyError("Historical macOS sandbox did not enforce IP, DNS, proxy and descendant network denial.")
        _canary_verdict(report, expected, "Historical macOS network canaries",
                        "Historical macOS sandbox did not enforce IP, DNS, proxy and descendant network denial.")
        if result.get("exitCode") != 0:
            raise IsolationCheckUnavailable("Historical macOS network canaries exited abnormally; checking again.")
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
                raise IsolationCheckUnavailable("Historical isolation preflight timed out; checking again.") from exc
            if line is None:
                raise IsolationCheckUnavailable("Historical isolation preflight process exited; checking again.")
            try:
                message = json.loads(line)
            except ValueError:
                continue  # Not a protocol message (for example a launcher's own output line).
            if not isinstance(message, dict) or message.get("id") != self.number:
                continue
            if "error" in message:
                raise IsolationCheckUnavailable(f"Historical isolation {method} failed: {message['error']}")
            return message.get("result", {})


def _ensure_discovery_binary(policy, executable):
    """Re-pin discovery to the installed Codex once the pinned bytes are gone.

    A no-model preflight of the installed binary comes first and the audit log
    records both hashes. If it fails, the hard link kept at preparation still
    serves; without one the run needs attention, naming the binary and hash.
    """
    from retrieval import historical_codex_discovery as discovery
    settings, workspace = policy["_discoverySettings"], policy["isolation"]["workspace"]
    if not discovery.needs_repin(settings, workspace):
        return
    record = discovery._repin_record(settings)
    if record is not None and discovery._usable(record["codex_path"], record["codex_sha256"], workspace):
        installed = discovery._hash(Path(executable).resolve())
        if installed == record["codex_sha256"]:
            return
    try:
        record = discovery.repin(settings, executable, workspace)
    except (discovery.DiscoveryError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if policy.get("_discoveryProblem"):
            raise NeedsAttention(policy["_discoveryProblem"] + f" The installed Codex ({executable}) did not pass "
                                 "the no-model discovery check.") from exc
        return
    try:
        controller_audit(policy, "codex_discovery_repinned", previous_sha256=settings["codex_sha256"],
                         codex_sha256=record["codex_sha256"])
    except IsolationCheckUnavailable:
        # Never leave a re-pin unrecorded: undo it and try again next time.
        (Path(settings["auth_home"]).parent / discovery.REPIN_RECORD).unlink(missing_ok=True)
        raise
    policy.pop("_discoveryProblem", None)


def preflight(policy, role, executable, inherited, agent_limits=None):
    """Return the isolated child env only after real host enforcement passes.

    On macOS the environment names the private temp folder made for the
    process that uses it (private_temp()), which the check proves; that
    process's argv adds temp_arguments(), and its caller removes the folder
    when it ends (remove_private_temp()). A failed check removes it here.
    The process's subagent limits (``agent_limits``, see agent_limits()) are
    passed to the checked app-server too and verified exactly.
    """
    if policy is None:
        return inherited
    adapter = policy["isolation"]["adapter"]
    if adapter == WINDOWS_ADAPTER and os.name != "nt":
        raise RetrievalPolicyError("Historical isolation adapter requires Windows MXC; no fallback is allowed.")
    if adapter == MACOS_ADAPTER and sys.platform != "darwin":
        raise RetrievalPolicyError("Historical isolation adapter requires macOS Seatbelt; no fallback is allowed.")
    if adapter == MACOS_ADAPTER and not _native_macos_binary(Path(executable)):
        # Missing while Codex updates itself; a later attempt finds it again.
        raise IsolationCheckUnavailable("Historical macOS execution requires a trusted native Mach-O Codex binary.")
    if adapter == MACOS_ADAPTER:
        host = Path(executable).resolve().with_name("codex-code-mode-host")
        _outside(host.resolve(), Path(policy["isolation"]["workspace"]).resolve(), "code-mode host")
        _outside(host.resolve(), role_workspace(policy, role).resolve(), "code-mode host")
        if not _native_macos_binary(host):
            raise IsolationCheckUnavailable("Historical macOS execution requires the trusted native codex-code-mode-host alongside Codex.")
    if role == "author" and policy.get("_discoverySettings"):
        _ensure_discovery_binary(policy, executable)
    temp = private_temp(policy, role)
    try:
        environment = isolated_environment(policy, role, inherited, temp=temp)
        process = subprocess.Popen(
            [executable, "--strict-config", "app-server", *temp_arguments(policy, environment),
             *agent_limit_arguments(agent_limits)],
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
            verify_rpc(rpc, policy, role, environment, agent_limits=agent_limits)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except BaseException:
        if temp is not None:
            remove_private_temp(policy, {"TMPDIR": str(temp)})
        raise
    return environment
