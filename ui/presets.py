"""Saved New problem settings: named sets of the form's settings, one of which may be the default.

The New problem form saves and loads them (Save these settings… and Load saved
settings… under Advanced settings → Saved settings), and every fresh form opens with
the default; a rerun or a job sent back to the form keeps its own settings. Terminal
runs never read them.

They live in <state>/new-problem-presets.json (runner.providers.PRESETS, beside the
API model store): an owner-only file in an owner-only folder that no model sandbox can
write, with the rest of the private state (providers.private_paths()), so that a model
can never change the settings the next problem opens with. On macOS no model sandbox
can read it either, and Claude Code's sandbox cannot anywhere; Codex's legacy
sandboxes on Linux and Windows can read it (docs/sandbox.md). It holds no keys.

A preset holds what the form sends with a job (proofJobSettings() in ui/app.js)
without its prompts, plus a description of each API model it names, without key or
address, kept only to name that model once it is gone. Never a statement, related
works, prompts, an API key or a URL. Its settings are checked as a job's are, except
whether the models and the web cutoff can run now: Start checks that.

File: {"version": 1, "defaultId": "p-…" or "", "presets": [{"id", "name", "createdAt",
"updatedAt", "settings", "apiModels"}]}. A file that cannot be read, is damaged or
comes from a newer version lists nothing, says why, and is never overwritten. One
entry that cannot be used is listed as such (it can only be deleted) and every write
keeps it as it was; so is every entry a write does not change.
"""

from collections import namedtuple
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import secrets
import threading
import unicodedata

import workflow_runner as runtime
from retrieval import historical_search
from runner import providers

try:
    import fcntl
except ImportError:  # Windows: writes are serialized within this process only.
    fcntl = None

VERSION = 1
PRESET_ID = re.compile(r"p-[0-9a-f]{12}\Z")
MAX_NAME_CHARS = providers.MAX_LABEL_CHARS
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
ROLES = ("review", "author", "critic", "writer")
# The stage each role works in (ui.server.STAGE_ROLES the other way round).
STAGES = {"review": "review", "author": "author", "critic": "critic", "writer": "writing"}
# How warnings name a role ("Critic model not available: …"), as the form's stages do.
ROLE_WORDS = {"review": "Reviewer", "author": "Author", "critic": "Critic", "writer": "Writer"}
# What a preset keeps: the form's settings in its order (proofJobSettings() without promptOverrides).
SETTINGS = ("workflowStages", "reviewModel", "reviewEffort", "authorModel", "authorEffort", "authorSubagents",
            "criticModel", "criticEffort", "criticCount", "criticRounds", "writerModel", "writerEffort",
            "thinkingHours", "speedMode", "reasoningSummary", "retrievalMode", "retrievalCutoffUtc")
# What a preset keeps of each API model it names (from its catalog row): no key, no address.
API_FIELDS = ("label", "model", "format", "sourceId", "sourceLabel")
BUILTIN_NAME = "Built-in settings"
GONE = "Those saved settings no longer exist. Open Load saved settings… again to see the current list."
_LOCK = threading.Lock()

# One entry of the file: as read (item), its checked form (preset, None when it cannot be used),
# why it cannot be used, and the id the page names it by (an unusable entry's reference).
Row = namedtuple("Row", "item preset problem ref")


class PresetError(ValueError):
    """Saved settings cannot be read, saved or changed as asked; the message is safe to show."""


class PresetsUnavailable(PresetError):
    """The saved settings file exists but cannot be used now: it lists nothing and is never written."""


def _jobs():
    """The checks and defaults of job settings (ui.server, which imports this module)."""

    from . import server
    return server


def path():
    """The saved settings file, in the private state folder beside the API model store."""

    return providers.state_directory() / providers.PRESETS


def builtin_settings():
    """TCS Prover's own settings: what a fresh New problem form opens with while no default is saved."""

    server = _jobs()
    return {
        "workflowStages": server.normalize_workflow_stages(),
        "reviewModel": server.DEFAULT_REVIEW_MODEL, "reviewEffort": server.DEFAULT_REVIEW_EFFORT,
        "authorModel": server.DEFAULT_AUTHOR_MODEL, "authorEffort": server.DEFAULT_REASONING_EFFORT,
        "authorSubagents": server.DEFAULT_AUTHOR_SUBAGENTS,
        "criticModel": server.DEFAULT_CRITIC_MODEL, "criticEffort": server.DEFAULT_REASONING_EFFORT,
        "criticCount": server.DEFAULT_CRITIC_COUNT, "criticRounds": server.DEFAULT_CRITIC_ROUNDS,
        "writerModel": server.DEFAULT_WRITER_MODEL, "writerEffort": server.DEFAULT_REASONING_EFFORT,
        "thinkingHours": server.DEFAULT_THINKING_HOURS, "speedMode": server.DEFAULT_SPEED,
        "reasoningSummary": server.DEFAULT_REASONING_SUMMARY, "retrievalMode": "live", "retrievalCutoffUtc": "",
    }


def _model(value, role, stored):
    """A role's model: a built-in or older id, or any well-formed api: id (configured or not).

    A stored preset may also name a model TCS Prover no longer supports (DeepSeek): it loads
    as its replacement, with a note.
    """

    if isinstance(value, str) and value:
        if providers.is_api_id(value):
            return value
        # Not an api: id, so this reads no connection list.
        if not value.startswith(providers.API_ID_PREFIX) and providers.lookup(value) is not None:
            return value
        if stored and providers.replacement_for_removed(value):
            return value
    shown = repr(value)[:80] if isinstance(value, str) and value else "(none)"
    raise PresetError(f"Unknown model {shown} for the {_jobs().ROLE_LABELS[role]}.")


def _effort(value, role):
    """A reasoning effort the menus offer; Ultra, no longer offered, is kept as Max."""

    if isinstance(value, str):
        value = runtime.RETIRED_EFFORTS.get(value, value)
    if value not in runtime.EFFORTS:
        raise PresetError(f"Choose a valid reasoning effort for the {_jobs().ROLE_LABELS[role]}.")
    return value


def _hours(value):
    """The author's time limit in hours: 0 for unlimited, or any positive number."""

    try:
        if isinstance(value, bool):
            raise ValueError
        hours = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PresetError("The time limit must be a number of hours: 0 for unlimited.") from None
    if not math.isfinite(hours) or hours < 0:
        raise PresetError("Choose 0 for an unlimited time limit, or a positive number of hours.")
    return int(hours) if hours.is_integer() else hours


def _cutoff(mode, cutoff):
    """(retrievalMode, retrievalCutoffUtc) as the form sends them: off, -1 (no web search) or a UTC time.

    Whether a historical cutoff can run now (in the past, on this computer, with these
    models and stages) is checked at Start, as for any job.
    """

    if mode not in ("live", "disabled", "historical"):
        raise PresetError("Choose unrestricted web search or a web search cutoff.")
    if not isinstance(cutoff, str):
        raise PresetError("The web search cutoff must be text.")
    cutoff = cutoff.strip()
    if mode == "live":
        if cutoff:
            raise PresetError("Clear the cutoff when unrestricted web search is selected.")
        return "live", ""
    if mode == "disabled" or cutoff == "-1":
        if cutoff not in ("", "-1"):
            raise PresetError("Use -1 to disable web-search tools, or choose a historical UTC cutoff.")
        return "disabled", "-1"
    try:
        historical_search.parse_cutoff(cutoff)
    except historical_search.HistoricalSearchError:
        raise PresetError("Enter -1 to disable web-search tools, or an exact UTC cutoff such as "
                          "2026-10-02T14:13:12Z.") from None
    return "historical", cutoff


def valid_settings(values, *, stored=False):
    """The settings a preset keeps from ``values``, checked as the form's.

    Unknown keys are dropped and missing ones take the built-in settings, so a preset
    saved before a setting existed keeps loading. Models are kept for every role, also
    for stages that are off. As at Start, a setting that only a stage that is off uses
    (its role's model and reasoning, the author's subagents and time limit, the critics
    and their rounds) is never refused: it is kept when it is valid, else it takes the
    built-in value, which Start runs a stage that is off with. ``stored``: read from the
    file (see _model()).
    """

    if not isinstance(values, dict):
        raise PresetError("The settings must be an object.")
    server = _jobs()
    settings = builtin_settings()
    try:
        if "workflowStages" in values:
            settings["workflowStages"] = server.normalize_workflow_stages(values["workflowStages"])
    except ValueError as exc:
        raise PresetError(str(exc)) from None
    stages = settings["workflowStages"]
    checks = []  # (key, its stage, its check), in the order a problem is reported
    for role in ROLES:
        checks += [(f"{role}Model", STAGES[role], lambda value, role=role: _model(value, role, stored)),
                   (f"{role}Effort", STAGES[role], lambda value, role=role: _effort(value, role))]
    checks += [("authorSubagents", "author", server.valid_author_subagents),
               ("criticCount", "critic", server.valid_critic_count), ("criticRounds", "critic", runtime.critic_limit),
               ("thinkingHours", "author", _hours)]
    for key, stage, check in checks:
        if key not in values:
            continue
        try:
            settings[key] = check(values[key])
        except (ValueError, runtime.Error) as exc:  # PresetError is a ValueError
            if stages[stage]:
                raise PresetError(str(exc)) from None
    try:
        if "speedMode" in values:
            if values["speedMode"] not in server.SPEEDS:
                raise PresetError("Choose Standard or Fast speed.")
            settings["speedMode"] = values["speedMode"]
        if "reasoningSummary" in values:
            if values["reasoningSummary"] not in server.REASONING_SUMMARIES:
                raise PresetError("Choose Status only, Concise summaries, or Detailed summaries.")
            settings["reasoningSummary"] = values["reasoningSummary"]
        settings["retrievalMode"], settings["retrievalCutoffUtc"] = _cutoff(
            values.get("retrievalMode", "live"), values.get("retrievalCutoffUtc", ""))
    except PresetError:
        raise
    except (ValueError, runtime.Error) as exc:
        raise PresetError(str(exc)) from None
    return settings


def _name(value):
    """A preset's name: 1 to MAX_NAME_CHARS characters, without control characters."""

    try:
        return providers._clean_text(value, "name", MAX_NAME_CHARS)
    except providers.ProviderError as exc:
        raise PresetError(str(exc)) from None


def _stored_api_models(value):
    """The API model descriptions of a stored preset that are well formed (display text only)."""

    if not isinstance(value, dict):
        return {}
    return {model: {key: entry[key] for key in API_FIELDS} for model, entry in value.items()
            if providers.is_api_id(model) and isinstance(entry, dict) and entry.get("format") in providers.FORMATS
            and all(isinstance(entry.get(key), str) and len(entry[key]) <= MAX_NAME_CHARS for key in API_FIELDS)}


def _stored(item):
    """Check one entry read from the file; refuse anything a save could not have written."""

    if not isinstance(item, dict):
        raise PresetError("This entry is not an object.")
    if not isinstance(item.get("id"), str) or not PRESET_ID.match(item["id"]):
        raise PresetError("This entry has no valid id.")
    try:
        name = _name(item.get("name"))
        settings = valid_settings(item.get("settings"), stored=True)
    except PresetError as exc:
        raise PresetError(f"These settings cannot be used: {exc}") from None
    return {"id": item["id"], "name": name, "settings": settings, "apiModels": _stored_api_models(item.get("apiModels")),
            **{key: item[key] if isinstance(item.get(key), str) else "" for key in ("createdAt", "updatedAt")}}


def _reference(index, item):
    """How the page names an entry that cannot be used: its position and a digest of its content."""

    digest = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    return f"invalid-{index}-{digest}"


def _load():
    """The file read strictly: {"rows": [Row…], "defaultId": as written}; nothing when it does not exist.

    Raises PresetsUnavailable when it exists but cannot be used: a symbolic link, unreadable,
    damaged, or written by a newer version.
    """

    file = path()
    try:
        if file.is_symlink():
            raise PresetsUnavailable(f"The saved settings file {file} is a symbolic link; "
                                     "replace it with the file itself.")
        raw = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"rows": [], "defaultId": ""}
    except (OSError, UnicodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise PresetsUnavailable(f"Cannot read the saved settings file {file} ({reason}).") from None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("presets"), list):
            raise ValueError
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError
    except (ValueError, RecursionError):
        raise PresetsUnavailable(f"The saved settings file {file} is damaged; fix or remove it.") from None
    if version > VERSION:
        raise PresetsUnavailable(f"The saved settings file {file} comes from a newer version of TCS Prover; "
                                 "update TCS Prover to use it.")
    rows, seen = [], set()
    for index, item in enumerate(value["presets"]):
        try:
            preset = _stored(item)
            if preset["id"] in seen:
                raise PresetError("Other saved settings have the same id.")
        except PresetError as exc:
            rows.append(Row(item, None, str(exc), _reference(index, item)))
            continue
        seen.add(preset["id"])
        rows.append(Row(item, preset, "", preset["id"]))
    default = value.get("defaultId")
    return {"rows": rows, "defaultId": default if isinstance(default, str) else ""}


def _effective_default(data):
    """The default's id when it names saved settings that can be used, else ""."""

    default = data["defaultId"]
    return default if default and any(row.preset and row.preset["id"] == default for row in data["rows"]) else ""


def _write(rows, default_id):
    """Replace the file atomically (owner-only); entries a change did not touch are written as they were read."""

    file = path()
    try:
        folder = providers.private_directory(file.parent)
    except providers.ProviderError as exc:
        raise PresetError(str(exc)) from None
    if file.is_symlink():
        raise PresetsUnavailable(f"The saved settings file {file} is a symbolic link; replace it with the file itself.")
    payload = {"version": VERSION, "defaultId": default_id, "presets": [row.item for row in rows]}
    try:
        runtime._private_atomic_write(folder / providers.PRESETS,
                                      json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise PresetError(f"Cannot save the settings ({exc.strerror or type(exc).__name__}).") from None


@contextmanager
def _locked():
    """Serialize changes: within this process, and on POSIX also with other TCS Prover processes
    sharing this state folder (a lock on its own owner-only file), so no save is lost."""

    with _LOCK:
        try:
            folder = providers.private_directory(providers.state_directory())
        except providers.ProviderError as exc:
            raise PresetError(str(exc)) from None
        if fcntl is None:
            yield
            return
        try:
            descriptor = os.open(folder / providers.PRESETS_LOCK, os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600)
        except OSError as exc:
            raise PresetError(f"Cannot lock the saved settings ({exc.strerror or type(exc).__name__}).") from None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


def _resolver():
    """Look up model ids for one answer, each once: (catalog row or None, whether that could be told).

    A connection list that cannot be read now tells nothing: no API model is called missing then.
    """

    seen = {}

    def resolve(model):
        if model not in seen:
            try:
                seen[model] = (providers.lookup(model), True)
            except providers.StoreUnavailable:
                seen[model] = (None, False)
        return seen[model]
    return resolve


def _api_models(settings, previous, resolve):
    """What a save keeps of each API model the settings name; a model gone already keeps its earlier record."""

    records = {}
    for role in ROLES:
        model = settings[f"{role}Model"]
        if not providers.is_api_id(model) or model in records:
            continue
        row, _ = resolve(model)
        if row:
            records[model] = {"label": row["label"], "model": row["wireModel"], "format": row["format"],
                              "sourceId": row["sourceId"], "sourceLabel": row["sourceLabel"]}
        elif model in previous:
            records[model] = previous[model]
    return records


def _labels(settings, api_models, resolve):
    """How the page names each model the settings use: its menu label, or as the preset recorded it."""

    labels = {}
    for role in ROLES:
        model = settings[f"{role}Model"]
        if model not in labels:
            row, _ = resolve(model)
            saved = api_models.get(model)
            labels[model] = row["label"] if row else saved["label"] if saved else model
    return labels


def _unavailable(settings, api_models, resolve):
    """The roles whose model is gone: an API model whose connection or model was removed, or DeepSeek.

    Loading such a preset puts the default model in that role, with a note; the preset itself
    is never changed, so it works again once the connection is added back.
    """

    missing = []
    for role in ROLES:
        model = settings[f"{role}Model"]
        if providers.replacement_for_removed(model):
            label = "DeepSeek, which is no longer supported"
        elif providers.is_api_id(model):
            row, told = resolve(model)
            if row or not told:
                continue
            saved = api_models.get(model)
            label = f"{saved['sourceLabel']} · {saved['label']}" if saved else model
        else:
            continue
        missing.append({"role": role, "model": model, "label": label,
                        "text": f"{ROLE_WORDS[role]} model not available: {label}"})
    return missing


def _public(row, default, resolve):
    """One entry as the page lists it."""

    if row.preset is None:
        item = row.item if isinstance(row.item, dict) else {}
        name = item.get("name") if isinstance(item.get("name"), str) else ""
        name = "".join(character for character in name if unicodedata.category(character) != "Cc").strip()
        return {"id": row.ref, "name": name[:MAX_NAME_CHARS] or "Unreadable saved settings", "unusable": True,
                "problem": row.problem}
    preset = row.preset
    return {"id": preset["id"], "name": preset["name"], "isDefault": preset["id"] == default,
            "createdAt": preset["createdAt"], "updatedAt": preset["updatedAt"], "settings": preset["settings"],
            "labels": _labels(preset["settings"], preset["apiModels"], resolve),
            "unavailable": _unavailable(preset["settings"], preset["apiModels"], resolve)}


def listing():
    """The saved settings as the page shows them: {presets, defaultId, problem, builtIn}.

    It never writes. A file that cannot be used lists nothing and says why (problem);
    new problems then open with the built-in settings.
    """

    try:
        data, problem = _load(), ""
    except PresetsUnavailable as exc:
        data = {"rows": [], "defaultId": ""}
        problem = f"{exc} Until then new problems open with the built-in settings, and settings cannot be saved."
    default, resolve = _effective_default(data), _resolver()
    builtin = builtin_settings()
    return {"presets": [_public(row, default, resolve) for row in data["rows"]], "defaultId": default,
            "problem": problem,
            "builtIn": {"name": BUILTIN_NAME, "settings": builtin, "labels": _labels(builtin, {}, resolve)}}


def _usable(rows, identity):
    return next((row for row in rows if row.preset and row.preset["id"] == identity), None)


def name_key(name):
    """A name as names are compared, ignoring case: lower case, as the page compares them (toLowerCase()).

    Not casefold(): it would also take "STRASSE" for "Straße", which the page does not, and then
    the page could never offer to replace those settings.
    """

    return name.lower()


def _taken(rows, name, keep=None):
    """Raise when other usable saved settings have this name (ignoring case, see name_key())."""

    clash = next((row for row in rows if row.preset and row is not keep
                  and name_key(row.preset["name"]) == name_key(name)), None)
    if clash:
        raise PresetError(f"Saved settings called “{clash.preset['name']}” already exist. "
                          "Choose another name, or replace them.")


def _fallback_name(rows):
    """A name for a save without one, "Saved settings N" (the page proposes a name of its own)."""

    names = {name_key(row.preset["name"]) for row in rows if row.preset}
    number = sum(1 for row in rows if row.preset) + 1
    while f"saved settings {number}" in names:
        number += 1
    return f"Saved settings {number}"


def _new_id(rows):
    """A fresh "p-<12 hex>" id that no entry of the file has."""

    taken = {row.ref for row in rows} | {row.item.get("id") for row in rows if isinstance(row.item, dict)}
    while True:
        identity = f"p-{secrets.token_hex(6)}"
        if identity not in taken:
            return identity


def save(name, settings, *, make_default=False, replace_id=""):
    """Save the form's settings under a name and return their id.

    A blank name gets "Saved settings N". A name already in use (ignoring case) is refused
    unless ``replace_id`` names those settings, which are then replaced under their id.
    ``make_default`` makes them the default; otherwise the default stays as it was.
    """

    if not isinstance(make_default, bool):
        raise PresetError("Say whether these settings become the default.")
    if name is not None and not isinstance(name, str):
        raise PresetError("The name must be text.")
    settings = valid_settings(settings)
    with _locked():
        data = _load()
        rows = data["rows"]
        target = _usable(rows, replace_id) if replace_id else None
        if replace_id and target is None:
            raise PresetError(GONE)
        name = _name(name) if name and name.strip() else _fallback_name(rows)
        _taken(rows, name, keep=target)
        previous = target.preset if target else {}
        identity = previous["id"] if target else _new_id(rows)
        now = providers._now()
        item = {"id": identity, "name": name, "createdAt": previous.get("createdAt") or now, "updatedAt": now,
                "settings": settings, "apiModels": _api_models(settings, previous.get("apiModels", {}), _resolver())}
        row = Row(item, _stored(item), "", identity)
        _write([row if entry is target else entry for entry in rows] if target else [*rows, row],
               identity if make_default else data["defaultId"])
    return identity


def rename(identity, name):
    """Give saved settings another name (one no other saved settings have, ignoring case)."""

    with _locked():
        data = _load()
        target = _usable(data["rows"], identity)
        if target is None:
            raise PresetError(GONE)
        name = _name(name)
        _taken(data["rows"], name, keep=target)
        item = {**target.item, "name": name, "updatedAt": providers._now()}
        _write([Row(item, {**target.preset, "name": name}, "", target.ref) if row is target else row
                for row in data["rows"]], data["defaultId"])


def delete(identity):
    """Delete saved settings (also an entry that cannot be used); deleting the default leaves none.

    Deleting the usable entry the default names clears the default. Otherwise the default stays
    while an entry with its id is left: a copy of the default with the same id (listed as
    unusable) goes without taking the default from the usable one, and a default whose own entry
    cannot be used now (say, saved by a newer TCS Prover) stays until that entry is deleted.
    """

    with _locked():
        data = _load()
        target = next((row for row in data["rows"] if isinstance(identity, str) and row.ref == identity), None)
        if target is None:
            raise PresetError(GONE)
        rows = [row for row in data["rows"] if row is not target]
        default = data["defaultId"]
        if (target.preset and target.preset["id"] == default) or not any(
                isinstance(row.item, dict) and row.item.get("id") == default for row in rows):
            default = ""
        _write(rows, default)


def set_default(identity):
    """Make saved settings the default, or with "" the built-in settings (no saved default)."""

    if not isinstance(identity, str):
        raise PresetError(GONE)
    with _locked():
        data = _load()
        if identity and _usable(data["rows"], identity) is None:
            raise PresetError(GONE)
        if identity != data["defaultId"]:
            _write(data["rows"], identity)


def _save_request(body):
    identity = save(body.get("name"), body.get("settings"), make_default=body.get("makeDefault", False),
                    replace_id=body.get("replaceId") or "")
    return {"savedId": identity, **listing()}


def _rename_request(body):
    rename(body.get("id"), body.get("name"))
    return listing()


def _delete_request(body):
    delete(body.get("id"))
    return listing()


def _default_request(body):
    if "id" not in body:
        raise PresetError("Choose saved settings to make the default, or the built-in settings.")
    set_default(body["id"])
    return listing()


# The POST routes (ui.server and ui.live_gateway route them; GET /presets is listing()).
ACTIONS = {"/presets": _save_request, "/presets/rename": _rename_request, "/presets/delete": _delete_request,
           "/presets/default": _default_request}
