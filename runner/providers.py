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
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
from urllib.parse import urlsplit


CODEX, CLAUDE, API = "codex", "claude", "api"
OPENAI_FORMAT, ANTHROPIC_FORMAT = "openai", "anthropic"
FORMATS = (OPENAI_FORMAT, ANTHROPIC_FORMAT)
PROVIDER_LABELS = {CODEX: "Codex CLI", CLAUDE: "Claude CLI", API: "API"}

CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
# `claude --effort` levels; API models in either format offer the same five.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
API_EFFORTS = {OPENAI_FORMAT: ("low", "medium", "high", "xhigh", "max"), ANTHROPIC_FORMAT: CLAUDE_EFFORTS}
DEFAULT_EFFORT = "max"

# Menu order; the first Codex entry stays the default model.
BUILTIN_MODELS = (
    {"id": "gpt-6.1-sol", "label": "GPT 6.1 Sol", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-6-astra", "label": "GPT 6 Astra", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-6-sol", "label": "GPT 6 Sol", "provider": CODEX, "efforts": CODEX_EFFORTS},
    {"id": "gpt-6-luna", "label": "GPT 6 Luna", "provider": CODEX, "efforts": CODEX_EFFORTS[:-1]},
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
    {"id": "gpt-5.6-luna", "label": "GPT 5.6 Luna (older)", "provider": CODEX, "efforts": CODEX_EFFORTS[:-1]},
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

ROOT = Path(__file__).resolve().parents[1]
STORE_ENV = "TCS_PROVER_API_MODELS"
DEFAULT_STORE = ROOT / ".tcs-prover" / "api-models.json"
STORE_VERSION = 1
# Private folders beside the store: atomic-write staging, the Codex homes of
# OpenAI-format API models, and the Claude --settings files that carry a key.
STORE_STAGING = ".api-models-staging"
CODEX_HOMES = "codex-homes"
CLAUDE_SETTINGS = "claude-settings"
_STORE_LOCK = threading.RLock()

_BUILTIN = {row["id"]: row for row in (*BUILTIN_MODELS, *_LEGACY_MODELS)}


class ProviderError(ValueError):
    """A model id or an API model setting is invalid; the message is safe to show."""


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

    The key store and its staging folder, the private Codex homes and the
    Claude settings folders. These entries rather than the whole state
    folder: an overridden store path may share its folder with run folders.
    """

    state = state_directory()
    return [Path(os.path.realpath(path)) for path in (
        store_path(), state / STORE_STAGING, state / CODEX_HOMES, state / CLAUDE_SETTINGS)]


def user_login_files():
    """This user's Codex and Claude CLI login files.

    Sandboxes of models served by a third-party endpoint may not read them,
    so a subscription login can never be echoed to that endpoint.
    """

    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return [Path(os.path.realpath(path)) for path in (codex / "auth.json", claude / ".credentials.json")]


def private_directory(path):
    """Create an owner-only directory (and missing parents) for controller state."""

    path = Path(path)
    if path.is_symlink():
        raise ProviderError("Private TCS Prover state cannot be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


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
    return {"format": api_format, "baseUrl": base_url, "headerName": header, "requestSource": source, "apiKey": key}


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

    if not isinstance(entry, dict) or not is_api_id(entry.get("id")):
        raise ProviderError("Invalid API model entry.")
    result = _validated(entry, require_key=False)
    result["id"] = entry["id"]
    for field in ("createdAt", "updatedAt"):
        result[field] = entry.get(field) if isinstance(entry.get(field), str) else ""
    test = entry.get("lastTest")
    old_header = entry.get("headerName", result["headerName"])
    same_auth = isinstance(old_header, str) and old_header.lower() == result["headerName"].lower()
    # A historical custom-header test describes a different authentication
    # scheme; it cannot vouch for the automatic one used after migration.
    result["lastTest"] = _last_test(test) if same_auth and isinstance(test, dict) else None
    return result


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


def _read_store(strict=False):
    """Read the entries; lenient readers skip damage, writers refuse to overwrite it."""

    path = store_path()
    try:
        if path.is_symlink():
            raise ProviderError("The API model store cannot be a symbolic link.")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except ProviderError:
        if strict:
            raise
        return []
    except (OSError, UnicodeError) as exc:
        if strict:
            raise ProviderError(f"Cannot read the API model store {path}.") from exc
        return []
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("models"), list):
            raise ValueError
    except ValueError:
        if strict:
            # The JSON error text could quote stored content, including a key.
            raise ProviderError(f"The API model store {path} is damaged; fix or remove it.") from None
        return []
    entries, seen = [], set()
    for entry in value["models"]:
        try:
            entry = _stored(entry)
            if entry["id"] in seen:
                raise ProviderError("Duplicate API model id.")
        except ProviderError:
            if strict:
                raise ProviderError(f"The API model store {path} has an invalid entry; fix or remove it.") from None
            continue
        seen.add(entry["id"])
        entries.append(entry)
    if os.name != "nt":
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass
    return entries


def _write_store(entries):
    """Atomically replace the store with an owner-only file."""

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
    }


def load_api_models():
    """Every valid configured API model, without keys."""

    with _STORE_LOCK:
        return [public_api_model(entry) for entry in _read_store()]


def api_model(model_id):
    """One configured API model without its key, or None."""

    if not is_api_id(model_id):
        return None
    with _STORE_LOCK:
        entry = next((entry for entry in _read_store() if entry["id"] == model_id), None)
    return public_api_model(entry) if entry else None


def api_model_secret(model_id):
    """The saved key of one configured API model ('' when absent)."""

    if not is_api_id(model_id):
        return ""
    with _STORE_LOCK:
        entry = next((entry for entry in _read_store() if entry["id"] == model_id), None)
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
        entries = _read_store(strict=True)
        existing = next((entry for entry in entries if requested and entry["id"] == requested), None)
        entry = _validated(values, existing)
        now = _now()
        if existing:
            changed = any(existing[key] != entry[key]
                          for key in ("format", "baseUrl", "requestSource", "model", "apiKey"))
            entry.update(id=existing["id"], createdAt=existing.get("createdAt") or now, updatedAt=now,
                         lastTest=None if changed else existing.get("lastTest"))
            entries = [entry if item["id"] == existing["id"] else item for item in entries]
        else:
            identity = requested
            if not identity:
                taken = {item["id"] for item in entries} | set(reserved)
                base = new_api_id(values.get("label"), entry["model"])
                identity, number = base, 2
                while identity in taken:
                    identity, number = f"{base}-{number}", number + 1
            entry.update(id=identity, createdAt=now, updatedAt=now, lastTest=None)
            entries.append(entry)
        _write_store(entries)
        return public_api_model(entry)


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
    with _STORE_LOCK:
        saved = next((entry for entry in _read_store() if model_id and entry["id"] == model_id), None)
    return _validated(values, saved), saved


def validated_api_connection(values, model_id=None):
    """Validate discovery settings, reusing a saved key only for its saved destination.

    No model is required before fetching the provider's available model list.
    The result contains the key and must stay on the server.
    """

    if model_id not in (None, "") and not is_api_id(model_id):
        raise ProviderError("API model ids look like api:<name> (lowercase letters, digits, '.', '_' or '-').")
    with _STORE_LOCK:
        saved = next((entry for entry in _read_store() if model_id and entry["id"] == model_id), None)
    if model_id and saved is None:
        raise ProviderError("That API model is not configured.")
    return _validated_connection(values, saved)


def delete_api_model(model_id):
    """Remove one API model; return whether it existed."""

    with _STORE_LOCK:
        entries = _read_store(strict=True)
        remaining = [entry for entry in entries if entry["id"] != model_id]
        if len(remaining) == len(entries):
            return False
        _write_store(remaining)
        return True


def record_api_model_test(model_id, result):
    """Save the outcome of an endpoint test on a saved model (no key involved)."""

    if not isinstance(result, dict):
        raise ProviderError("A test result must be an object.")
    with _STORE_LOCK:
        entries = _read_store(strict=True)
        entry = next((entry for entry in entries if entry["id"] == model_id), None)
        if entry is None:
            raise ProviderError("Unknown API model.")
        entry["lastTest"] = _last_test({**result, "at": result.get("at") or _now()})
        _write_store(entries)
        return public_api_model(entry)


def _builtin_row(row):
    return {
        "id": row["id"], "label": row["label"], "provider": row["provider"],
        "providerLabel": PROVIDER_LABELS[row["provider"]], "efforts": list(row["efforts"]),
        "defaultEffort": DEFAULT_EFFORT, "supportsFast": row["provider"] == CODEX,
        **({"legacy": True} if row["id"] in LEGACY_CODEX_MODELS else {}),
    }


def _api_row(model):
    return {
        "id": model["id"], "label": model["label"], "provider": API, "providerLabel": PROVIDER_LABELS[API],
        "efforts": list(API_EFFORTS[model["format"]]), "defaultEffort": DEFAULT_EFFORT,
        "supportsFast": False, "format": model["format"], "host": model["host"], "hasKey": model["hasKey"],
    }


def catalog(include_legacy=False):
    """Menu rows: built-ins first, then configured API models. No secrets."""

    rows = [_builtin_row(row) for row in BUILTIN_MODELS]
    if include_legacy:
        rows += [_builtin_row(row) for row in _LEGACY_MODELS]
    return rows + [_api_row(model) for model in load_api_models()]


def lookup(model_id):
    """The catalog row of any accepted id (including older Codex ids), or None."""

    if isinstance(model_id, str) and model_id in _BUILTIN:
        return _builtin_row(_BUILTIN[model_id])
    model = api_model(model_id)
    return _api_row(model) if model else None


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

    Ultra therefore runs as Max on Luna, Claude and API models.
    """

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
