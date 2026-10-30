"""Model registry and the private store of user-configured API models.

Every model runs through one of two proven agent harnesses: the Codex CLI or
the Claude CLI. API models reuse those harnesses pointed at a user-specified
endpoint: OpenAI-format endpoints run through Codex with an isolated custom
provider, Anthropic-format endpoints through Claude. This module is the single
source of truth for accepted model ids, their harness and their efforts.

API keys live only in the private store file. Nothing here returns a key except
api_model_secret(); every other view is safe to send to the browser or to log.
"""

from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import unicodedata
from urllib.parse import urlsplit


CODEX, CLAUDE, API = "codex", "claude", "api"
OPENAI_FORMAT, ANTHROPIC_FORMAT = "openai", "anthropic"
FORMATS = (OPENAI_FORMAT, ANTHROPIC_FORMAT)
PROVIDER_LABELS = {CODEX: "Codex CLI", CLAUDE: "Claude CLI", API: "API"}

CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Efforts no longer offered, and what they run as. Ultra was Max plus Codex's proactive
# task delegation; subagents now follow the run's own settings (Author subagents,
# Independent critics). A saved job, script or flag that says ultra runs as max.
RETIRED_EFFORTS = {"ultra": "max"}
# `claude --effort` levels; API models in either format offer the same five.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
API_EFFORTS = {OPENAI_FORMAT: ("low", "medium", "high", "xhigh", "max"), ANTHROPIC_FORMAT: CLAUDE_EFFORTS}
DEFAULT_EFFORT = "max"

# Menu order; the first Codex entry stays the default model.
BUILTIN_MODELS = (
    {"id": "gpt-6.1-sol", "label": "GPT 6.1 Sol", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-6-astra", "label": "GPT 6 Astra", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-6-sol", "label": "GPT 6 Sol", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-6-luna", "label": "GPT 6 Luna", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "provider": CLAUDE, "efforts": CLAUDE_EFFORTS},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "provider": CLAUDE, "efforts": CLAUDE_EFFORTS},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "provider": CLAUDE, "efforts": CLAUDE_EFFORTS},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5", "provider": CLAUDE, "efforts": CLAUDE_EFFORTS},
)
# Codex calls the 5.6 generation "Older": accepted for saved runs and scripts,
# but no longer offered.
LEGACY_CODEX_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
_LEGACY_MODELS = (
    {"id": "gpt-5.6-sol", "label": "GPT 5.6 Sol (older)", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-5.6-terra", "label": "GPT 5.6 Terra (older)", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-5.6-luna", "label": "GPT 5.6 Luna (older)", "provider": CODEX, "efforts": CODEX_EFFORTS},
)
REMOVED_MODEL_REPLACEMENTS = {"deepseek-v4-pro": "gpt-6.1-sol", "deepseek/deepseek-v4-pro": "gpt-6.1-sol"}
REMOVED_MODEL_WARNING = "DeepSeek is no longer supported; this role now uses GPT 6.1 Sol."

API_ID_PREFIX = "api:"
API_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
DEFAULT_HEADERS = {OPENAI_FORMAT: "Authorization", ANTHROPIC_FORMAT: "x-api-key"}
DEFAULT_REQUEST_SOURCE = "TCS-Prover"
# Plain HTTP would expose the key on the network; only loopback may use it.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Host names every URL parser reads alike (see _base_url()).
HOST_NAME = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?\Z")
MAX_BASE_URL_CHARS = 2048
MAX_MODEL_CHARS = 200
MAX_LABEL_CHARS = 200
MAX_REQUEST_SOURCE_CHARS = 200
MAX_KEY_CHARS = 4096
MAX_TEST_MESSAGE_CHARS = 300
# Optional token limits of an API connection's models, passed to their CLI (the
# CLIs assume default windows for models they do not know).
MAX_CONTEXT_WINDOW = 10_000_000
MIN_CONTEXT_WINDOW = 1024

ROOT = Path(__file__).resolve().parents[1]
STORE_ENV = "TCS_PROVER_API_MODELS"
DEFAULT_STORE = ROOT / ".tcs-prover" / "api-models.json"
STORE_VERSION = 2
# Private folders beside the store: atomic-write staging, the Codex homes of
# OpenAI-format API models, the Claude --settings files that carry a key, the
# Claude configuration folders of Anthropic-format API models and the private
# copies of Claude author conversations.
STORE_STAGING = ".api-models-staging"
CODEX_HOMES = "codex-homes"
CLAUDE_SETTINGS = "claude-settings"
CLAUDE_HOMES = "claude-homes"
CLAUDE_TRANSCRIPTS = "claude-transcripts"
# The browser session tokens of the web UI (ui.ownership.session_token).
SESSIONS = "sessions"
# Who holds each run and runs-folder lock (ui.ownership), the last readable copy
# of each bundled workflow (workflow_runner.read_editable_yaml) and this
# installation's settings (ui.server.save_installation_settings).
RUN_OWNERS = "run-owners"
SERVERS = "servers"
LAST_GOOD_WORKFLOWS = "last-good-workflows"
INSTALLATION_SETTINGS = "settings.json"
_STORE_LOCK = threading.RLock()

_BUILTIN = {row["id"]: row for row in (*BUILTIN_MODELS, *_LEGACY_MODELS)}
# The last store this process read successfully, per store path: a read that
# fails for a moment keeps the settings a running job already uses.
_LAST_GOOD = {}


class ProviderError(ValueError):
    """A model id or an API model setting is invalid; the message is safe to show."""


class StoreUnavailable(ProviderError):
    """The API model store exists but cannot be read or parsed right now.

    It never means "no models": a check that meets it waits and reads again.
    """


def offered_models():
    """Built-in ids in menu order (older Codex ids are accepted, not offered)."""

    return tuple(row["id"] for row in BUILTIN_MODELS)


def store_path():
    """The private API model store, overridable for tests and isolated installs.

    A relative override is resolved against this checkout, not the working
    directory: the server, the runner and the review worker each run in a
    different folder and must all read the same store.
    """

    configured = os.environ.get(STORE_ENV, "").strip()
    if not configured:
        return DEFAULT_STORE
    path = Path(configured).expanduser()
    return path if path.is_absolute() else ROOT / path


def state_directory():
    """Private controller state that lives next to the API model store."""

    return store_path().parent


def private_paths():
    """Private state that no model sandbox may read or write, as resolved paths.

    The key store and its staging folder, the private Codex and Claude homes,
    the Claude settings folders, the copies of Claude conversations, the
    browser session tokens, the run and server lock records, the last
    readable copies of the bundled workflows (a launch may run from one) and
    the installation settings. These entries rather than the whole state
    folder: an overridden store path may share its folder with run folders.
    """

    state = state_directory()
    return [Path(os.path.realpath(path)) for path in (
        store_path(), state / STORE_STAGING, state / CODEX_HOMES, state / CLAUDE_SETTINGS,
        state / CLAUDE_HOMES, state / CLAUDE_TRANSCRIPTS, state / SESSIONS, state / RUN_OWNERS,
        state / SERVERS, state / LAST_GOOD_WORKFLOWS, state / INSTALLATION_SETTINGS)]


def user_login_files():
    """This user's Codex and Claude CLI login files.

    Sandboxes of models served by a third-party endpoint may not read them,
    so a subscription login can never be echoed to that endpoint.
    """

    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return [Path(os.path.realpath(path)) for path in (codex / "auth.json", claude / ".credentials.json")]


def private_directory(path):
    """Create an owner-only directory (and missing parents) for controller state.

    A symbolic link is used only when it names a folder this user owns and
    that is owner-only (0700, set here when possible); the resolved folder is
    returned. Anything else is refused with a message that says what to fix.
    """

    path = Path(path)
    if not path.is_symlink():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        return path
    try:
        target = path.resolve(strict=True)
        info = target.stat()
    except (OSError, RuntimeError):
        raise ProviderError(f"Private TCS Prover state {path} is a symbolic link to a folder that does not exist; "
                            "create that folder or remove the link.") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ProviderError(f"Private TCS Prover state {path} links to {target}, which is not a folder.")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ProviderError(f"Private TCS Prover state {path} links to {target}, which belongs to another user; "
                            "link it to a folder you own, or remove the link.")
    if os.name != "nt":
        try:
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.chmod(target, 0o700)
                info = target.stat()
        except OSError:
            pass
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ProviderError(f"Private TCS Prover state {path} links to {target}, which others can open; "
                                "make it owner-only (chmod 700).")
    return target


def _clean_text(value, label, limit, *, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProviderError(f"The {label} must be text.")
    value = value.strip()
    if required and not value:
        raise ProviderError(f"Enter the {label}.")
    if len(value) > limit:
        raise ProviderError(f"The {label} is longer than {limit} characters.")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ProviderError(f"The {label} cannot contain control characters.")
    return value


def _base_url(value):
    value = _clean_text(value, "base URL", MAX_BASE_URL_CHARS)
    if any(character.isspace() for character in value):
        raise ProviderError("The base URL cannot contain spaces.")
    # Python's urlsplit (validation, the browser's host, the Test request) and
    # the WHATWG URL parsers of the Claude and Codex CLIs disagree about a
    # backslash, a percent-encoded or non-ASCII host and short or octal IPv4
    # forms, so the host shown and tested could differ from the host that
    # receives the key. Only spellings every parser reads alike are accepted.
    if "\\" in value:
        raise ProviderError("The base URL cannot contain backslashes.")
    if not value.isascii():
        raise ProviderError("Enter the base URL in plain ASCII; write an international domain name in its xn-- form.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ProviderError("Enter a valid http(s) base URL.") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise ProviderError("Enter an http(s) base URL with a host, for example https://api.openai.com/v1.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ProviderError("Remove credentials from the base URL; enter the key in the API key field.")
    if parts.fragment or "#" in value:
        raise ProviderError("Remove the #fragment from the base URL.")
    if parts.query or "?" in value:
        raise ProviderError("Remove the query string from the base URL.")
    _check_host(parts.netloc, host)
    if any(segment.lower().replace("%2e", ".") in {".", ".."} for segment in parts.path.split("/")):
        raise ProviderError("Remove the '.' and '..' segments from the base URL path.")
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ProviderError("Use https for remote endpoints; plain http is allowed only for localhost.")
    if port == 0:
        raise ProviderError("Enter a valid http(s) base URL.")
    return value.rstrip("/"), host


def _check_host(netloc, host):
    """Accept a DNS name, a dotted-decimal IPv4 address or a bracketed IPv6 address."""

    if "%" in netloc:
        # WHATWG parsers decode a percent-encoded host (and reject zone ids).
        raise ProviderError("The base URL host cannot contain percent-encoding.")
    if netloc.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ProviderError("Enter a valid IPv6 address in brackets, for example http://[::1]:8000/v1.") from None
        return
    if not HOST_NAME.match(host):
        raise ProviderError("The base URL host may contain only letters, digits, hyphens, underscores and dots.")
    labels = host.split(".")
    last = labels[-1] or (labels[-2] if len(labels) > 1 else "")
    if last.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last):
        # WHATWG parsers read every host ending in a number as IPv4, including
        # octal, hexadecimal and short forms such as 127.1.
        if host.endswith(".") or len(labels) != 4 or any(
                not label.isdigit() or str(int(label)) != label or int(label) > 255 for label in labels):
            raise ProviderError("Write an IPv4 address as four decimal numbers, for example 127.0.0.1.")


def _request_source(value):
    value = _clean_text(value, "request source", MAX_REQUEST_SOURCE_CHARS, required=False)
    if not value.isascii():
        raise ProviderError("The request source must use printable ASCII characters.")
    return value


def request_source(entry):
    """The non-secret source identifier sent as User-Agent on API requests."""

    return entry.get("requestSource") or DEFAULT_REQUEST_SOURCE


def _api_key(value):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProviderError("The API key must be text.")
    value = value.strip()
    if value[:7].lower() == "bearer ":
        value = value[7:].strip()
    if not value or value.lower() == "bearer":
        raise ProviderError("Enter the API key.")
    if len(value) > MAX_KEY_CHARS:
        raise ProviderError(f"The API key is longer than {MAX_KEY_CHARS} characters.")
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in value):
        raise ProviderError("The API key cannot contain spaces or control characters.")
    return value


def _model_name(value):
    value = _clean_text(value, "model name", MAX_MODEL_CHARS)
    if value.startswith("-"):
        # The name reaches the CLI as the value of --model / -m.
        raise ProviderError("The model name cannot start with a hyphen.")
    return value


def _token_limit(value, label):
    """An optional token count: None when blank, else a whole number in range."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_CONTEXT_WINDOW:
        raise ProviderError(f"Enter the {label} as a whole number of tokens, or leave it empty.")
    return value


def _token_limits(values, existing):
    """The optional context window and max output of a connection's models (an absent field keeps the saved one)."""

    window = _token_limit(values.get("contextWindow", existing.get("contextWindow")), "context window")
    output = _token_limit(values.get("maxOutputTokens", existing.get("maxOutputTokens")), "max output")
    if window is not None and window < MIN_CONTEXT_WINDOW:
        raise ProviderError(f"The context window must be at least {MIN_CONTEXT_WINDOW:,} tokens.")
    if window is not None and output is not None and output >= window:
        raise ProviderError("The max output must be smaller than the context window.")
    return {**({"contextWindow": window} if window else {}), **({"maxOutputTokens": output} if output else {})}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_api_id(model_id):
    return (isinstance(model_id, str) and model_id.startswith(API_ID_PREFIX)
            and bool(API_SLUG.match(model_id[len(API_ID_PREFIX):])))


def _slug(text):
    slug = re.sub(r"[^a-z0-9._-]+", "-", str(text).lower()).strip("-._")
    return (slug or "model")[:48].rstrip("-._") or "model"


def _validated_connection(values, existing=None, *, require_key=True):
    """Validate connection fields without needing a model from the endpoint yet."""

    if not isinstance(values, dict):
        raise ProviderError("API model settings must be an object.")
    existing = existing or {}
    api_format = values.get("format", existing.get("format"))
    if api_format not in FORMATS:
        raise ProviderError("Choose the OpenAI or Anthropic API format.")
    base_url, _ = _base_url(values.get("baseUrl", existing.get("baseUrl")))
    # Authentication is fixed by the protocol. Normalize older custom header
    # settings while retaining their saved id and key at the same destination.
    header = DEFAULT_HEADERS[api_format]
    source = _request_source(values.get("requestSource", existing.get("requestSource")))
    supplied_key = values.get("apiKey")
    blank = supplied_key is None or (isinstance(supplied_key, str) and not supplied_key.strip())
    if existing and blank:
        # An empty field keeps the saved key, but only for the destination it
        # was saved for: otherwise anyone who can call the controller could
        # send a key they never saw to a host and header of their choice.
        if api_format != existing.get("format") or base_url != existing.get("baseUrl"):
            raise ProviderError("Enter the API key again: a saved key is sent only to the format and base URL "
                                "it was saved with.")
        key = existing.get("apiKey", "")
    elif blank and not require_key:
        key = ""
    else:
        key = _api_key(supplied_key)
    return {"format": api_format, "baseUrl": base_url, "headerName": header, "requestSource": source, "apiKey": key,
            **_token_limits(values, existing)}


def _validated(values, existing=None, *, require_key=True):
    """Return a complete stored entry from submitted fields (and a saved entry)."""

    connection = _validated_connection(values, existing, require_key=require_key)
    existing = existing or {}
    model = _model_name(values.get("model", existing.get("model")))
    label = _clean_text(values.get("label", existing.get("label")), "label", MAX_LABEL_CHARS, required=False)
    host = (urlsplit(connection["baseUrl"]).hostname or "").lower()
    return {**connection, "label": label or f"{model} · {host}", "model": model}


def _stored(entry):
    """Validate one entry read from disk; reject anything a save could not produce."""

    if not isinstance(entry, dict):
        raise ProviderError("The entry is not an object.")
    if not is_api_id(entry.get("id")):
        raise ProviderError("The entry has no valid id (api:<name>).")
    result = _validated(entry, require_key=False)
    result["id"] = entry["id"]
    # Version-one entries represented one model per key. Preserve their exact
    # selected id while exposing that key as an independent connection source.
    result["legacyModelId"] = entry.get("legacyModelId", True) is not False
    result["availableModels"] = _available_models(entry.get("availableModels", [
        {"id": result["model"], "label": result["model"]}]))
    for field in ("createdAt", "updatedAt"):
        result[field] = entry.get(field) if isinstance(entry.get(field), str) else ""
    test = entry.get("lastTest")
    old_header = entry.get("headerName", result["headerName"])
    same_auth = isinstance(old_header, str) and old_header.lower() == result["headerName"].lower()
    # A historical custom-header test describes a different authentication
    # scheme; it cannot vouch for the automatic one used after migration.
    result["lastTest"] = _last_test(test) if same_auth and isinstance(test, dict) else None
    return result


def _available_models(values):
    """Validate a provider's model choices before they enter the private store."""

    if not isinstance(values, list) or not values or len(values) > 5000:
        raise ProviderError("The API key must provide at least one available model (at most 5000).")
    models = {}
    for item in values:
        if not isinstance(item, dict):
            raise ProviderError("The available API models must contain model IDs.")
        identity = _model_name(item.get("id"))
        label = _clean_text(item.get("label", identity), "model label", MAX_LABEL_CHARS, required=False) or identity
        models.setdefault(identity, {"id": identity, "label": label})
    return list(models.values())


def api_model_id(connection_id, wire_model):
    """Stable role-selection id for one wire model under a saved API key."""

    digest = hashlib.sha256((connection_id + "\0" + wire_model).encode("utf-8")).hexdigest()[:16]
    return API_ID_PREFIX + connection_id[len(API_ID_PREFIX):][:40] + "--m-" + digest


def _connection_models(entry):
    return [{**row, "modelId": entry["id"] if entry.get("legacyModelId", True) and row["id"] == entry["model"]
             else api_model_id(entry["id"], row["id"])} for row in entry["availableModels"]]


def _resolve_api_model(entries, model_id):
    for entry in entries:
        if entry.get("legacyModelId", True) and entry["id"] == model_id:
            return {**entry, "sourceId": entry["id"], "id": model_id}
        for row in _connection_models(entry):
            if row["modelId"] == model_id:
                return {**entry, "sourceId": entry["id"], "id": model_id, "model": row["id"]}
    return None


def _last_test(value):
    message = value.get("message")
    latency = value.get("latencyMs")
    status = value.get("status")
    return {
        "ok": value.get("ok") is True,
        "at": value.get("at") if isinstance(value.get("at"), str) else _now(),
        "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
        "message": (message if isinstance(message, str) else "")[:MAX_TEST_MESSAGE_CHARS],
        "latencyMs": latency if isinstance(latency, (int, float)) and not isinstance(latency, bool) else None,
    }


def _load_store():
    """Read the store strictly: one [stored item, valid entry or None, problem] row per entry.

    An absent store holds no connections. A store that exists but cannot be
    read or parsed raises StoreUnavailable: it never reads as empty, so a read
    error is never taken for a removed model. Invalid entries stay in the
    rows (see invalid_api_models()) and every write keeps them unchanged, so
    one bad entry blocks nothing else.
    """

    path = store_path()
    try:
        if path.is_symlink():
            raise StoreUnavailable(f"The API model store {path} is a symbolic link; replace it with the file itself.")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise StoreUnavailable(f"Cannot read the API model store {path} ({reason}).") from None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("models"), list):
            raise ValueError
    except (ValueError, RecursionError):
        # The JSON error text could quote stored content, including a key.
        raise StoreUnavailable(f"The API model store {path} is damaged; fix or remove it.") from None
    rows, seen = [], set()
    for item in value["models"]:
        try:
            entry = _stored(item)
            if entry["id"] in seen:
                raise ProviderError("Another entry has the same id.")
        except ProviderError as exc:
            rows.append([item, None, str(exc)])
            continue
        seen.add(entry["id"])
        rows.append([item, entry, ""])
    if os.name != "nt":
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass
    return rows


def _entries(cached=True):
    """The valid entries (connections) of the store.

    A store that cannot be read right now gives this process's last good
    read, so a running job keeps its settings through a momentary error
    (``cached=False`` reads strictly and raises StoreUnavailable instead).
    """

    key = str(store_path())
    with _STORE_LOCK:
        try:
            entries = [entry for _, entry, _ in _load_store() if entry is not None]
        except StoreUnavailable:
            if cached and key in _LAST_GOOD:
                return _LAST_GOOD[key]
            raise
        _LAST_GOOD[key] = entries
        return entries


def _row_id(row):
    """The id a store row names (also an invalid entry's, when it has one)."""

    item, entry, _ = row
    return entry["id"] if entry is not None else item.get("id") if isinstance(item, dict) else None


def _write_store(rows):
    """Atomically replace the store with an owner-only file; invalid entries are written back unchanged."""

    entries = [entry if entry is not None else item for item, entry, _ in rows]
    path = store_path()
    private_directory(path.parent)
    if path.is_symlink():
        raise ProviderError("The API model store cannot be a symbolic link.")
    payload = json.dumps({"version": STORE_VERSION, "models": entries}, ensure_ascii=False, indent=2) + "\n"
    # The staging folder, like the store, is hidden from every model sandbox.
    staging = private_directory(path.parent / STORE_STAGING)
    descriptor, temporary = tempfile.mkstemp(dir=staging, prefix=".api-models.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            try:
                os.fchmod(stream.fileno(), 0o600)
            except (AttributeError, OSError):
                pass  # mkstemp already created the file owner-only.
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def public_api_model(entry):
    """Browser-safe view of one stored entry: never the key itself."""

    key = entry.get("apiKey") or ""
    return {
        "id": entry["id"], "label": entry["label"], "format": entry["format"],
        "baseUrl": entry["baseUrl"], "host": (urlsplit(entry["baseUrl"]).hostname or "").lower(),
        "headerName": entry["headerName"], "requestSource": entry["requestSource"],
        "model": entry["model"], "hasKey": bool(key),
        "keyHint": ("…" + key[-4:]) if len(key) >= 12 else ("…" if key else ""),
        "lastTest": entry.get("lastTest"), "createdAt": entry.get("createdAt", ""),
        "updatedAt": entry.get("updatedAt", ""),
        "sourceId": entry.get("sourceId", entry["id"]), "sourceLabel": entry["label"],
        "contextWindow": entry.get("contextWindow"), "maxOutputTokens": entry.get("maxOutputTokens"),
    }


def public_api_connection(entry):
    """A saved API key's source and discovered model choices, never its secret."""

    return {**public_api_model(entry), "models": _connection_models(entry)}


def _invalid_reference(index, item):
    """A stable name for one invalid entry: its position and a digest of everything but its key."""

    content = {key: value for key, value in item.items() if key != "apiKey"} if isinstance(item, dict) else item
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    return f"invalid-{index}-{digest}"


def _public_invalid(index, item, problem):
    """Browser-safe view of an entry that cannot be used: what is readable of it, never its key or URL."""

    item = item if isinstance(item, dict) else {}
    text = {name: item[name].strip()[:80] for name in ("label", "model")
            if isinstance(item.get(name), str) and item[name].strip()}
    try:
        host = (urlsplit(item["baseUrl"]).hostname or "").lower() if isinstance(item.get("baseUrl"), str) else ""
    except ValueError:
        host = ""
    identity = item.get("id") if is_api_id(item.get("id")) else ""
    return {"id": identity, "ref": _invalid_reference(index, item), "invalid": True, "problem": problem,
            "label": text.get("label") or identity or f"Entry {index + 1}", "model": text.get("model", ""),
            "format": item.get("format") if item.get("format") in FORMATS else "", "host": host[:200],
            "hasKey": isinstance(item.get("apiKey"), str) and bool(item["apiKey"].strip())}


def load_api_models():
    """Every valid configured API model, without keys (StoreUnavailable when the store cannot be read)."""

    return [public_api_model(entry) for entry in _entries()]


def load_api_connections():
    """Every valid saved API connection with its model choices, without keys (StoreUnavailable as above)."""

    return [public_api_connection(entry) for entry in _entries()]


def invalid_api_models():
    """The store entries that cannot be used, with why, so that they can be repaired or removed."""

    return store_listing()[1]


def store_listing():
    """The store as the settings page shows it, read strictly: (connections without keys, invalid entries)."""

    with _STORE_LOCK:
        rows = _load_store()
        _LAST_GOOD[str(store_path())] = [entry for _, entry, _ in rows if entry is not None]
    return ([public_api_connection(entry) for _, entry, _ in rows if entry is not None],
            [_public_invalid(index, item, problem) for index, (item, entry, problem) in enumerate(rows)
             if entry is None])


def check_private_state():
    """'' when the private state folders can be created and written, else what to fix (checked at startup)."""

    try:
        state = private_directory(state_directory())
        for name in (CODEX_HOMES, CLAUDE_SETTINGS, CLAUDE_HOMES, CLAUDE_TRANSCRIPTS, SESSIONS):
            if (state / name).exists() or (state / name).is_symlink():
                private_directory(state / name)
        descriptor, probe = tempfile.mkstemp(dir=state, prefix=".probe-")
        os.close(descriptor)
        os.unlink(probe)
    except ProviderError as exc:
        return str(exc)
    except OSError as exc:
        return (f"The private TCS Prover state folder {state_directory()} cannot be used "
                f"({exc.strerror or type(exc).__name__}).")
    return ""


def api_connection(connection_id, cached=True):
    """One saved API connection without its key, or None (StoreUnavailable when the store cannot be read)."""

    entry = next((entry for entry in _entries(cached) if entry["id"] == connection_id), None)
    return public_api_connection(entry) if entry else None


def api_model(model_id, cached=True):
    """One configured API model without its key, or None when the store has no such model.

    A store that cannot be read raises StoreUnavailable (see _entries()).
    """

    if not is_api_id(model_id):
        return None
    entry = _resolve_api_model(_entries(cached), model_id)
    return public_api_model(entry) if entry else None


def api_model_secret(model_id):
    """The saved key of one configured API model ('' when absent)."""

    if not is_api_id(model_id):
        return ""
    entry = _resolve_api_model(_entries(), model_id)
    return entry.get("apiKey", "") if entry else ""


def save_api_model(values, reserved=()):
    """Create or update one API model and return its browser-safe view.

    Without an id a new `api:<slug>` id is chosen, never one in `reserved`;
    an unknown explicit id is created as given. On update an empty or absent
    apiKey keeps the saved key while the format and base URL stay the same.
    """

    if not isinstance(values, dict):
        raise ProviderError("API model settings must be an object.")
    requested = values.get("id")
    if requested not in (None, "") and not is_api_id(requested):
        raise ProviderError("API model ids look like api:<name> (lowercase letters, digits, '.', '_' or '-').")
    with _STORE_LOCK:
        rows = _load_store()
        entries = [entry for _, entry, _ in rows if entry is not None]
        existing = next((entry for entry in entries if requested and entry["id"] == requested), None)
        entry = _validated(values, existing)
        entry.update(legacyModelId=True, availableModels=[{"id": entry["model"], "label": entry["model"]}])
        now = _now()
        if existing:
            # The Test sends the max output too, so a changed one needs a new test.
            changed = any(existing.get(key) != entry.get(key)
                          for key in ("format", "baseUrl", "requestSource", "model", "apiKey", "maxOutputTokens"))
            entry.update(id=existing["id"], createdAt=existing.get("createdAt") or now, updatedAt=now,
                         lastTest=None if changed else existing.get("lastTest"))
        else:
            identity = requested
            if not identity:
                taken = {item["id"] for item in entries} | set(reserved)
                taken.update(row["modelId"] for item in entries for row in _connection_models(item))
                taken.update(_row_id(row) for row in rows if row[1] is None)
                base = new_api_id(values.get("label"), entry["model"])
                identity, number = base, 2
                while identity in taken:
                    identity, number = f"{base}-{number}", number + 1
            entry.update(id=identity, createdAt=now, updatedAt=now, lastTest=None)
        _write_store(_with_entry(rows, entry))
        return public_api_model(entry)


def _with_entry(rows, entry):
    """The store rows with ``entry`` saved over the entry with its id, else added.

    Without a valid entry of that id, the invalid entries naming it are
    replaced: saving under an invalid entry's id repairs it in place.
    """

    valid = [index for index, row in enumerate(rows) if row[1] is not None and row[1]["id"] == entry["id"]]
    invalid = [index for index, row in enumerate(rows) if row[1] is None and _row_id(row) == entry["id"]]
    if valid:
        return [[entry, entry, ""] if index == valid[0] else row for index, row in enumerate(rows)]
    if invalid:
        return [[entry, entry, ""] if index == invalid[0] else row for index, row in enumerate(rows)
                if index == invalid[0] or index not in invalid]
    return [*rows, [entry, entry, ""]]


def save_api_connection(values, discovered_models, reserved=(), *, legacy_model=None, expected=None):
    """Save one API key and its catalog without asking the user to select a model."""

    if not isinstance(values, dict):
        raise ProviderError("API connection settings must be an object.")
    requested = values.get("id") or None
    if requested and not is_api_id(requested):
        raise ProviderError("Choose a saved API connection to edit.")
    choices = _available_models(discovered_models)
    legacy_model = _model_name(legacy_model) if legacy_model is not None else None
    with _STORE_LOCK:
        rows = _load_store()
        entries = [entry for _, entry, _ in rows if entry is not None]
        existing = next((entry for entry in entries if entry["id"] == requested), None)
        broken = next((row[0] for row in rows if row[1] is None and requested and _row_id(row) == requested), None)
        if (existing is None and broken is not None and legacy_model is None
                and broken.get("legacyModelId", True) is not False):
            # Repairing a version-one entry keeps the model id that jobs recorded for it.
            try:
                legacy_model = _model_name(broken.get("model"))
            except ProviderError:
                legacy_model = None
        if expected is not None and (existing is None or any(
                existing.get(key) != expected.get(key)
                for key in ("format", "baseUrl", "requestSource", "apiKey", "label", "updatedAt"))):
            raise ProviderError("The API connection changed while loading models. Save it again with the current settings.")
        connection = _validated_connection(values, existing)
        label = _clean_text(values.get("label", (existing or {}).get("label")), "label", MAX_LABEL_CHARS,
                            required=False) or (urlsplit(connection["baseUrl"]).hostname or "API key")
        now = _now()
        if existing:
            identity = existing["id"]
        else:
            taken = {entry["id"] for entry in entries} | set(reserved)
            taken.update(row["modelId"] for entry in entries for row in _connection_models(entry))
            if not requested:
                # A requested id that only an invalid entry has repairs that entry.
                taken.update(_row_id(row) for row in rows if row[1] is None)
            base = requested or API_ID_PREFIX + _slug(label)
            identity, number = base, 2
            while identity in taken:
                if requested:
                    raise ProviderError("That API connection id is already in use.")
                identity, number = f"{base}-{number}", number + 1
        entry = {**connection, "id": identity, "label": label, "availableModels": choices,
                 "legacyModelId": existing.get("legacyModelId", True) if existing else legacy_model is not None,
                 "model": existing["model"] if existing else legacy_model or choices[0]["id"],
                 "createdAt": (existing or {}).get("createdAt") or now, "updatedAt": now,
                 "lastTest": None}
        _write_store(_with_entry(rows, entry))
        return public_api_connection(entry)


def refresh_api_connection(connection_id, expected, discovered_models):
    """Publish a fetched catalog only if its saved connection remained unchanged."""

    choices = _available_models(discovered_models)
    with _STORE_LOCK:
        rows = _load_store()
        entry = next((entry for _, entry, _ in rows if entry is not None and entry["id"] == connection_id), None)
        fields = ("format", "baseUrl", "requestSource", "apiKey")
        if entry is None or any(entry[key] != expected[key] for key in fields):
            raise ProviderError("The API connection changed while loading models. Load its models again.")
        entry["availableModels"] = choices
        entry["updatedAt"] = _now()
        _write_store(rows)
        return public_api_connection(entry)


def new_api_id(label, model):
    """The id a new API model saved without one gets, unless a saved model has it (then -2, -3...)."""

    return API_ID_PREFIX + _slug(label or model)


def validated_api_model(values, model_id=None):
    """Check settings exactly as save_api_model() would, without saving them.

    With the id of a saved model, absent fields come from that model, and an
    empty apiKey too while the destination (format and base URL) is unchanged.
    Returns (entry, saved): the complete settings and the saved
    entry (None when unsaved). Both include the key: keep them on the server.
    """

    if model_id not in (None, "") and not is_api_id(model_id):
        raise ProviderError("API model ids look like api:<name> (lowercase letters, digits, '.', '_' or '-').")
    saved = _resolve_api_model(_entries(cached=False), model_id) if model_id else None
    return _validated(values, saved), saved


def validated_api_connection(values, model_id=None):
    """Validate discovery settings, reusing a saved key only for its saved destination.

    No model is required before fetching the provider's available model list.
    The result contains the key and must stay on the server.
    """

    if model_id not in (None, "") and not is_api_id(model_id):
        raise ProviderError("API model ids look like api:<name> (lowercase letters, digits, '.', '_' or '-').")
    saved = next((entry for entry in _entries(cached=False) if model_id and entry["id"] == model_id), None)
    if model_id and saved is None:
        raise ProviderError("That API model is not configured.")
    return _validated_connection(values, saved)


def delete_api_model(model_id):
    """Remove one API connection (also an invalid entry with that id); return whether it existed."""

    with _STORE_LOCK:
        rows = _load_store()
        remaining = [row for row in rows if _row_id(row) != model_id]
        if len(remaining) == len(rows):
            return False
        _write_store(remaining)
        return True


def delete_invalid_api_model(reference):
    """Remove one invalid entry named by its reference (see invalid_api_models()); return whether it existed."""

    with _STORE_LOCK:
        rows = _load_store()
        remaining = [row for index, row in enumerate(rows)
                     if row[1] is not None or _invalid_reference(index, row[0]) != reference]
        if len(remaining) == len(rows):
            return False
        _write_store(remaining)
        return True


def record_api_model_test(model_id, result):
    """Save the outcome of an endpoint test on a saved model (no key involved)."""

    if not isinstance(result, dict):
        raise ProviderError("A test result must be an object.")
    with _STORE_LOCK:
        rows = _load_store()
        entries = [entry for _, entry, _ in rows if entry is not None]
        resolved = _resolve_api_model(entries, model_id)
        entry = next((entry for entry in entries if entry["id"] == (resolved or {}).get("sourceId", model_id)), None)
        if entry is None:
            raise ProviderError("Unknown API model.")
        entry["lastTest"] = _last_test({**result, "at": result.get("at") or _now()})
        _write_store(rows)
        return public_api_model(entry)


def _builtin_row(row):
    return {
        "id": row["id"], "label": row["label"], "provider": row["provider"],
        "providerLabel": PROVIDER_LABELS[row["provider"]], "efforts": list(row["efforts"]),
        "defaultEffort": DEFAULT_EFFORT, "supportsFast": row["provider"] == CODEX,
        "sourceId": row["provider"], "sourceLabel": PROVIDER_LABELS[row["provider"]],
        **({"legacy": True} if row["id"] in LEGACY_CODEX_MODELS else {}),
    }


def _api_row(model, choice=None):
    choice = choice or {"id": model["model"], "label": model["model"], "modelId": model["id"]}
    return {
        "id": choice["modelId"], "label": choice["label"], "provider": API, "providerLabel": PROVIDER_LABELS[API],
        "sourceId": model.get("sourceId", model["id"]), "sourceLabel": model["label"], "wireModel": choice["id"],
        "efforts": list(API_EFFORTS[model["format"]]), "defaultEffort": DEFAULT_EFFORT,
        "supportsFast": False, "format": model["format"], "host": model["host"], "hasKey": model["hasKey"],
    }


def catalog(include_legacy=False):
    """Menu rows: built-ins first, then configured API models. No secrets.

    While the store cannot be read (and this process never read it), the
    menus show the built-in models only; the API connection list says why.
    """

    rows = [_builtin_row(row) for row in BUILTIN_MODELS]
    if include_legacy:
        rows += [_builtin_row(row) for row in _LEGACY_MODELS]
    try:
        entries = _entries()
    except StoreUnavailable:
        entries = []
    for entry in entries:
        model = public_api_model(entry)
        choices = _connection_models(entry)
        rows += [_api_row(model, choice) for choice in choices]
        if include_legacy and entry.get("legacyModelId") and not any(choice["modelId"] == entry["id"] for choice in choices):
            rows.append({**_api_row(model), "legacy": True})
    return rows


def source_catalog():
    """Every selectable CLI or saved API-key source, without credentials."""

    try:
        connections = load_api_connections()
    except StoreUnavailable:
        connections = []  # As in catalog(): the built-in sources only.
    return [{"id": CODEX, "label": PROVIDER_LABELS[CODEX], "provider": CODEX},
            {"id": CLAUDE, "label": PROVIDER_LABELS[CLAUDE], "provider": CLAUDE},
            *[{"id": entry["id"], "label": entry["label"], "provider": API,
               "format": entry["format"], "host": entry["host"], "hasKey": entry["hasKey"], "keyHint": entry["keyHint"]}
              for entry in connections]]


def lookup(model_id):
    """The catalog row of any accepted id (including older Codex ids), or None.

    For an api:<id> model a store that cannot be read raises StoreUnavailable.
    """

    if isinstance(model_id, str) and model_id in _BUILTIN:
        return _builtin_row(_BUILTIN[model_id])
    if not is_api_id(model_id):
        return None
    entry = _resolve_api_model(_entries(), model_id)
    if entry is None:
        return None
    choice = next((row for row in entry["availableModels"] if row["id"] == entry["model"]),
                  {"id": entry["model"], "label": entry["model"]})
    return _api_row(public_api_model(entry), {**choice, "modelId": model_id})


def is_known(model_id):
    return lookup(model_id) is not None


def _required(model_id):
    if isinstance(model_id, str) and model_id in _BUILTIN:
        return _BUILTIN[model_id], None
    model = api_model(model_id)
    if model is None:
        raise ProviderError(f"Unknown model {str(model_id)[:80]!r}.")
    return None, model


def provider_kind(model_id):
    """'codex' or 'claude' for built-in models, 'api' for configured endpoints."""

    builtin, _ = _required(model_id)
    return builtin["provider"] if builtin else API


def api_format(model_id):
    """The wire format of an API model (None for built-in models)."""

    _, model = _required(model_id)
    return model["format"] if model else None


def harness(model_id):
    """The CLI that runs a model: OpenAI-format endpoints use Codex, Anthropic-format Claude."""

    builtin, model = _required(model_id)
    if builtin:
        return builtin["provider"]
    return CODEX if model["format"] == OPENAI_FORMAT else CLAUDE


def wire_model(model_id):
    """The model name the CLI sends to its endpoint."""

    builtin, model = _required(model_id)
    return builtin["id"] if builtin else model["model"]


def model_label(model_id):
    builtin, model = _required(model_id)
    return builtin["label"] if builtin else model["label"]


def efforts_for(model_id):
    builtin, model = _required(model_id)
    return tuple(builtin["efforts"]) if builtin else API_EFFORTS[model["format"]]


def effective_effort(model_id, effort):
    """Clamp a shared-menu effort to the highest level the model lists at or below it.

    A retired effort runs as its replacement first (Ultra as Max, see RETIRED_EFFORTS).
    """

    effort = RETIRED_EFFORTS.get(effort, effort)
    if effort not in CODEX_EFFORTS:
        raise ProviderError("Choose a valid reasoning effort.")
    supported = efforts_for(model_id)
    if effort in supported:
        return effort
    lower = [value for value in CODEX_EFFORTS[:CODEX_EFFORTS.index(effort)] if value in supported]
    return lower[-1] if lower else supported[0]


def supports_fast(model_id):
    """Fast is a ChatGPT service tier: only native Codex models offer it."""

    return provider_kind(model_id) == CODEX


def enforces_output_schema(model_id):
    """Whether the harness enforces a JSON schema for structured roles.

    Codex uses --output-schema and Claude --json-schema. OpenAI-format API models
    get a prompt-level JSON contract instead, with lenient decoding and
    validation in workflow_runner.structured(), which retries an invalid
    answer every MODEL_RETRY_SECONDS with no attempt limit.
    """

    return not (provider_kind(model_id) == API and api_format(model_id) == OPENAI_FORMAT)


def replacement_for_removed(model_id):
    """The supported replacement for a model whose provider was removed, else None."""

    if not isinstance(model_id, str):
        return None
    return REMOVED_MODEL_REPLACEMENTS.get(model_id.strip().lower())
