"""The record of a job's model calls: what was sent to the model and what came back, kept apart from the run.

Plan v3, section 1 (the owner's decisions of 13 Nov 2026): TCS Prover records every model call through the programs'
own copies, with nothing on the network changed:
- Codex's own full trace: every Codex process of a live run gets CODEX_ROLLOUT_TRACE_ROOT=<record>/codex-trace/<label>
  (codex_process()), and each trace is ingested into the record while the process runs and when it ends;
- the CLI session files: copied into <record>/sources/ (and the copy verified) before TCS Prover deletes any session,
  and the author's and coordinator's sessions incrementally when they end (keep_codex_session() and the others);
- Claude Code's output stream: every stream-json line, after TCS Prover's redaction, in <record>/claude/
  (claude_stream());
- snapshots of the job's transcript, settings and prompt set (snapshot(), by the server).

The server names a live job's record for its runner, review worker and reply turns in RECORD_ENV (web-cutoff runs get
none: their isolation is unchanged, and the server copies what they leave behind, copy_web_cutoff()). Records are
never deleted automatically and outlive a deleted job; only the owner deletes one (delete_record()).

Layout of <state>/records/<run folder name>/ (state = providers.state_directory()), owner-only:
- manifest.json: the record's run folder, title, creation time and whether it is a web-cutoff run;
- calls.jsonl: one line per model request (CallLog), written when the request ends; a request the program never
  finished (it was stopped or killed during the call) gets a line with status "interrupted";
- items/: each item's exact JSON once, by sha256, in gzip packs with an index (Objects);
- codex/<label>/process.json (who the process was) and codex/<label>/<bundle>/: each Codex trace bundle as stored:
  payloads.jsonl (where each of its payload files went), manifest.json and trace.jsonl.gz (byte copies);
- codex-trace/<label>/: Codex's raw trace while its process runs; a payload file goes once its stored copy rebuilds
  byte for byte, the rest of the bundle when the process has ended and its copy is verified (raw files of a trace
  format this code does not recognize are kept, and their calls marked "trace format not recognized");
- claude/<label>.json (who the process was), claude/<label>.jsonl while the CLI runs, claude/<label>.jsonl.gz after;
- sources/codex/<thread>/, sources/claude/<project>/: copied session files; sources/web-cutoff/: a web-cutoff run's
  author sessions and discovery folders;
- transcript/: the latest snapshot of transcript.jsonl (transcript.jsonl.gz), job-settings.json and prompts/.
Only the standard library compresses (gzip): no new dependency.
"""

import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid

from runner import providers

try:
    import fcntl
except ImportError:  # Windows.
    fcntl = None
try:
    import msvcrt
except ImportError:  # Not Windows.
    msvcrt = None

# The record folder of the job a runner, review worker or reply turn works for, set by the server for live runs only.
RECORD_ENV = "TCS_PROVER_RECORD"
# <state>/records holds one folder per job (its run folder's name).
RECORDS = "records"
# Codex 0.160's opt-in trace (codex-rs/rollout-trace): a root folder in which each root session writes one bundle
# trace-<trace id>-<thread id>/ (manifest.json, trace.jsonl, payloads/<n>.json), its spawned subagents in the same
# bundle. Bundles of manifest and event schema version 1 are understood (TRACE_SCHEMA_VERSION).
TRACE_ROOT_ENV = "CODEX_ROLLOUT_TRACE_ROOT"
TRACE_SCHEMA_VERSION = 1
SCHEMA_VERSION = 1
# How often the trace of a running Codex process is read for requests that ended (seconds).
TRACE_POLL_SECONDS = 5
# A leftover trace bundle written to this recently is left for a later start: a Codex process whose runner died may
# still be writing it (seconds).
LEFTOVER_QUIET_SECONDS = 120
# A pack of items is closed and the next one started once it holds this many bytes.
PACK_BYTES = 64 * 1024 * 1024
# Decompressed pack members kept in memory for reading (bytes).
MEMBER_CACHE_BYTES = 64 * 1024 * 1024
# The longest first line of a Codex session file read to learn its thread (its session_meta: about 25 KB).
SESSION_META_BYTES = 4 * 1024 * 1024
# Payload kinds whose JSON is stored as a skeleton plus items (each item once): a request's input, instructions and
# tools; a response's output items.
REQUEST_KINDS = ("inference_request", "compaction_request")
RESPONSE_KINDS = ("inference_response",)
# Trace events that start and end a model request: {start: (kind, request field)} and {end: (status, response field)}.
REQUEST_STARTS = {"inference_started": "request", "compaction_request_started": "compaction"}
REQUEST_ENDS = {
    "inference_completed": ("completed", "response_payload"),
    "inference_failed": ("failed", "partial_response_payload"),
    "inference_cancelled": ("cancelled", "partial_response_payload"),
    "compaction_request_completed": ("completed", "response_payload"),
    "compaction_request_failed": ("failed", None),
}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")
# The files in which Codex keeps a login (auth.json: the ChatGPT login or an API key; .credentials.json: MCP sign-ins
# when no keychain is available). A web-cutoff URL discovery call keeps a copy of the login in its private Codex home
# while it runs, and a call stopped by a signal leaves it there (retrieval/historical_codex_discovery.py): the record
# never copies one (copy_web_cutoff()).
LOGIN_FILES = ("auth.json", ".credentials.json")
# A Codex session file: rollout-<YYYY-MM-DDThh-mm-ss>-<thread id>[_<segment id>].jsonl, compressed .jsonl.zst.
_ROLLOUT = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]{36})(?:_([0-9a-f-]{36}))?\.jsonl(?:\.zst)?\Z")


class RecordError(Exception):
    """The record cannot be written or read here; the message is safe to show."""


def record_root():
    """<state>/records: every job's record."""

    return providers.state_directory() / RECORDS


def valid_name(name):
    """Whether ``name`` can be a record's folder name: a run folder's name, never a path."""

    return (isinstance(name, str) and 0 < len(name) <= 255 and name not in (".", "..") and not name.startswith(".")
            and "/" not in name and "\\" not in name and "\0" not in name and ":" not in name)


def record_folder(name):
    """The record folder of the run folder ``name`` (ValueError for anything but a plain folder name)."""

    if not valid_name(name):
        raise ValueError("Unknown record.")
    return record_root() / name


def _now_ms():
    return int(time.time() * 1000)


def _iso(ms):
    """ISO 8601 UTC with milliseconds, or None."""

    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    seconds, millis = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{millis:03d}Z"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def compact(value):
    """The bytes an item is stored and hashed as: compact JSON, UTF-8, key order kept."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def pretty(value):
    """JSON as Codex's trace writes a payload file (serde_json's pretty printer: two spaces, no final newline).

    Checked against every payload of the record audit's captures: json.loads then this gives the same bytes. Numbers
    that Python prints differently (an exponent such as 1e-7) do not round-trip; such a payload is kept whole.
    """

    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _fsync(stream):
    stream.flush()
    try:
        os.fsync(stream.fileno())
    except OSError:
        pass


def _fsync_folder(folder):
    if os.name == "nt":
        return
    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _private_folder(path):
    """Create an owner-only folder (and its parents) for the record."""

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _open_private(path, mode):
    """Open a record file owner-only; ``mode`` "ab" or "wb"."""

    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "ab" else os.O_TRUNC)
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    return os.fdopen(descriptor, mode)


def write_atomic(path, data):
    """Replace one record file with ``data`` (bytes), durably."""

    path = Path(path)
    _private_folder(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with _open_private(temporary, "wb") as stream:
            stream.write(data)
            _fsync(stream)
        os.replace(temporary, path)
        _fsync_folder(path.parent)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def read_json(path, default=None):
    try:
        with open(path, "rb") as stream:
            value = json.loads(stream.read())
    except (OSError, ValueError, UnicodeError, RecursionError):
        return default
    return value


class FileLock:
    """An exclusive lock between processes on one file (flock(2); msvcrt on Windows), with a thread lock."""

    def __init__(self, path):
        self.path = Path(path)
        self._thread = threading.RLock()
        self._descriptor = None
        self._depth = 0

    def acquire(self, blocking=True):
        if not self._thread.acquire(blocking):
            return False
        if self._depth:
            self._depth += 1
            return True
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        except OSError:
            self._thread.release()
            raise
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            elif msvcrt is not None:
                while True:
                    try:
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if not blocking:
                            raise
                        time.sleep(0.05)
        except OSError:
            os.close(descriptor)
            self._thread.release()
            if blocking:
                raise
            return False
        self._descriptor, self._depth = descriptor, 1
        return True

    def release(self):
        self._depth -= 1
        if self._depth == 0:
            descriptor, self._descriptor = self._descriptor, None
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    try:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                os.close(descriptor)
        self._thread.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class Objects:
    """Items stored once by the sha256 of their bytes (compact JSON for items, exact bytes for whole payloads).

    items/pack-<n>.gz files hold gzip members; a member is one write's objects, each framed as
    "<sha256> <length>\\n<bytes>\\n". items/index.jsonl has one line per member: {"pack", "offset", "length",
    "objects"}. A member is written and synced before its index line; only indexed objects exist, so a write cut short
    leaves unreachable bytes, never a broken item.
    """

    def __init__(self, folder):
        self.folder = Path(folder)
        self.index_path = self.folder / "index.jsonl"
        self._where = {}  # sha256 -> (pack, offset, length)
        self._index_size = 0
        self._cache = {}  # (pack, offset) -> {sha: bytes}
        self._cache_order = []
        self._cache_bytes = 0
        self._lock = threading.RLock()

    def _pack(self, number):
        return self.folder / f"pack-{number:06d}.gz"

    def refresh(self):
        """Read the index lines written since the last read (by any process)."""

        with self._lock:
            try:
                with open(self.index_path, "rb") as stream:
                    stream.seek(self._index_size)
                    data = stream.read()
            except OSError:
                return
            end = data.rfind(b"\n") + 1
            for line in data[:end].splitlines():
                try:
                    entry = json.loads(line)
                    where = (int(entry["pack"]), int(entry["offset"]), int(entry["length"]))
                    names = entry["objects"]
                except (ValueError, KeyError, TypeError):
                    continue
                for sha in names if isinstance(names, list) else ():
                    self._where.setdefault(sha, where)
            self._index_size += end

    def has(self, sha):
        with self._lock:
            return sha in self._where

    def names(self):
        """Every stored object's sha256, in the order they were written."""

        self.refresh()
        with self._lock:
            return sorted(self._where, key=lambda sha: (self._where[sha][0], self._where[sha][1]))

    def put(self, blobs):
        """Store the objects of ``blobs`` ({sha256: bytes}) not yet stored; the caller holds the record's lock."""

        self.refresh()
        with self._lock:
            fresh = {sha: data for sha, data in blobs.items() if sha not in self._where}
            if not fresh:
                return
            body = b"".join(b"%s %d\n" % (sha.encode("ascii"), len(data)) + data + b"\n"
                            for sha, data in fresh.items())
            member = gzip.compress(body, compresslevel=6, mtime=0)
            _private_folder(self.folder)
            number = max([int(path.name[5:11]) for path in self.folder.glob("pack-??????.gz")] or [1])
            pack = self._pack(number)
            try:
                if pack.stat().st_size >= PACK_BYTES:
                    number += 1
                    pack = self._pack(number)
            except OSError:
                pass
            with _open_private(pack, "ab") as stream:
                offset = stream.seek(0, os.SEEK_END)
                stream.write(member)
                _fsync(stream)
            line = json.dumps({"pack": number, "offset": offset, "length": len(member),
                               "objects": list(fresh)}, separators=(",", ":")) + "\n"
            with _open_private(self.index_path, "ab") as stream:
                stream.write(line.encode("ascii"))
                _fsync(stream)
            self.refresh()

    def get(self, sha):
        """One object's bytes; KeyError when the record does not have it."""

        with self._lock:
            if sha not in self._where:
                self.refresh()
            where = self._where[sha]
            member = self._cache.get(where[:2])
            if member is None:
                with open(self._pack(where[0]), "rb") as stream:
                    stream.seek(where[1])
                    data = gzip.decompress(stream.read(where[2]))
                member, position = {}, 0
                while position < len(data):
                    newline = data.index(b"\n", position)
                    name, size = data[position:newline].split(b" ")
                    start = newline + 1
                    member[name.decode("ascii")] = data[start:start + int(size)]
                    position = start + int(size) + 1
                self._cache[where[:2]] = member
                self._cache_order.append(where[:2])
                self._cache_bytes += len(data)
                while self._cache_bytes > MEMBER_CACHE_BYTES and len(self._cache_order) > 1:
                    old = self._cache.pop(self._cache_order.pop(0))
                    self._cache_bytes -= sum(len(value) for value in old.values())
            return member[sha]

    def item(self, sha):
        """One stored item, parsed."""

        return json.loads(self.get(sha))


class CallLog:
    """calls.jsonl: one line per model request, appended under the record's lock and synced.

    Every line has an "id" ("c" and six digits, in writing order). A line cut short by a crash is skipped by readers;
    the next write starts a new line after it.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lines = []
        self._size = 0
        self._lock = threading.RLock()

    def read(self):
        """Every complete line, parsed (the lines written since the last read are added)."""

        with self._lock:
            try:
                with open(self.path, "rb") as stream:
                    stream.seek(self._size)
                    data = stream.read()
            except OSError:
                return list(self._lines)
            end = data.rfind(b"\n") + 1
            for line in data[:end].splitlines():
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict) and isinstance(value.get("id"), str):
                    self._lines.append(value)
            self._size += end
            return list(self._lines)

    def append(self, lines):
        """Give each line its id and append them; the caller holds the record's lock. Returns the ids."""

        if not lines:
            return []
        with self._lock:
            existing = self.read()
            number = max((int(line["id"][1:]) for line in existing if re.fullmatch(r"c\d+", line["id"])), default=0)
            ids, data = [], b""
            for line in lines:
                number += 1
                identity = f"c{number:06d}"
                ids.append(identity)
                data += json.dumps({"id": identity, **line}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            _private_folder(self.path.parent)
            with _open_private(self.path, "ab") as stream:
                if stream.seek(0, os.SEEK_END):
                    with open(self.path, "rb") as check:
                        check.seek(-1, os.SEEK_END)
                        if check.read(1) != b"\n":
                            data = b"\n" + data  # After a line a crash cut short.
                stream.write(data)
                _fsync(stream)
            self.read()
            return ids


class Record:
    """One job's record folder (see the module's description)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.objects = Objects(self.folder / "items")
        self.calls = CallLog(self.folder / "calls.jsonl")
        self._lock = FileLock(self.folder / ".lock")

    @classmethod
    def create(cls, folder, **manifest):
        """Open the record at ``folder``, making it owner-only (and its manifest) when it is new."""

        folder = Path(folder)
        _private_folder(folder.parent)
        _private_folder(folder)
        record = cls(folder)
        if not (folder / "manifest.json").is_file():
            record.write_manifest({"schemaVersion": SCHEMA_VERSION, "runFolder": folder.name,
                                   "created": _iso(_now_ms()), **manifest})
        elif manifest:
            record.update_manifest(**manifest)
        return record

    def exists(self):
        return self.folder.is_dir()

    @contextlib.contextmanager
    def locked(self):
        _private_folder(self.folder)
        with self._lock:
            yield self

    def manifest(self):
        value = read_json(self.folder / "manifest.json", {})
        return value if isinstance(value, dict) else {}

    def write_manifest(self, value):
        write_atomic(self.folder / "manifest.json",
                     (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def update_manifest(self, **fields):
        with self.locked():
            value = self.manifest()
            changed = {key: item for key, item in fields.items() if value.get(key) != item}
            if changed:
                self.write_manifest({**value, **changed, "updated": _iso(_now_ms())})

    def store(self, blobs):
        """Store objects ({sha256: bytes}); the caller holds the lock."""

        self.objects.put(blobs)

    def path(self, relative):
        """A file of the record by its relative path (ValueError for one outside it)."""

        relative = str(relative).replace("\\", "/")
        parts = relative.split("/")
        if not relative or relative.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise ValueError("Not a path in the record.")
        return self.folder.joinpath(*parts)


# --- Codex traces ----------------------------------------------------------------------------------------------------

# How each kind of trace payload is stored: the top-level lists whose elements are items (stored once each), and the
# top-level values stored whole as one item (instructions and tools repeat in every request). The rest of the payload
# is its skeleton. Any other kind is kept as one object.
PAYLOAD_PARTS = {
    "inference_request": (("input",), ("instructions", "tools")),
    "compaction_request": (("input",), ("instructions", "tools")),
    "inference_response": (("output_items",), ()),
    "compaction_response": (("output_items",), ()),
    "compaction_checkpoint": (("input_history", "replacement_history"), ()),
}


def split_payload(raw, kind, previous=None):
    """How one trace payload is stored: (store, input, {sha256: bytes}); rebuild_payload() gives the bytes back.

    A payload of a kind in PAYLOAD_PARTS becomes store {"skeleton": sha, "lists": {field: [sha, ...]}, "values":
    {field: sha}}: its skeleton is the payload with those fields set to null, which keeps their place. A request's
    input list is returned apart (``input``): [sha, ...], or {"base": path, "keep": n, "add": [sha, ...]} when
    ``previous`` (the path and input of the thread's previous request) starts the same way; its store names the input
    as "input". Anything else, and JSON this cannot read, is {"object": sha} of the exact bytes.
    """

    blobs = {}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        value = None
    parts = PAYLOAD_PARTS.get(kind)
    if not isinstance(value, dict) or parts is None:
        sha = _sha(raw)
        return {"object": sha}, None, {sha: raw}
    lists, values = parts
    skeleton = dict(value)
    store, refs = {"lists": {}, "values": {}}, None
    for field in lists:
        items = value.get(field)
        if not isinstance(items, list):
            continue
        shas = []
        for item in items:
            data = compact(item)
            shas.append(_sha(data))
            blobs[shas[-1]] = data
        skeleton[field] = None
        if field == "input" and kind in REQUEST_KINDS:
            refs = shas
            if previous is not None and previous[1]:
                keep = 0
                for mine, theirs in zip(shas, previous[1]):
                    if mine != theirs:
                        break
                    keep += 1
                if keep:
                    refs = {"base": previous[0], "keep": keep, "add": shas[keep:]}
            store["lists"][field] = "input"
        else:
            store["lists"][field] = shas
    for field in values:
        whole = value.get(field)
        if isinstance(whole, (list, dict)) or isinstance(whole, str) and len(whole) > 256:
            data = compact(whole)
            sha = _sha(data)
            blobs[sha] = data
            skeleton[field] = None
            store["values"][field] = sha
    data = compact(skeleton)
    store["skeleton"] = _sha(data)
    blobs[store["skeleton"]] = data
    return store, refs, blobs


def rebuild_payload(objects, store, input_list=None):
    """The exact bytes of a stored payload; ``input_list`` is its request input as a full list of sha256."""

    if "object" in store:
        return objects.get(store["object"])
    value = json.loads(objects.get(store["skeleton"]))
    for field, refs in store.get("lists", {}).items():
        refs = input_list if refs == "input" else refs
        value[field] = [json.loads(objects.get(sha)) for sha in refs]
    for field, sha in store.get("values", {}).items():
        value[field] = json.loads(objects.get(sha))
    return pretty(value)


class StoredBundle:
    """A trace bundle as the record keeps it: codex/<label>/<bundle>/payloads.jsonl and its byte copies."""

    def __init__(self, record, label, name):
        self.record, self.label, self.name = record, label, name
        self.folder = record.folder / "codex" / label / name
        self.entries = {}  # payload path -> {"kind", "sha256", "size", "store", "thread"}
        self._size = 0
        self._full = {}

    def load(self):
        try:
            with open(self.folder / "payloads.jsonl", "rb") as stream:
                stream.seek(self._size)
                data = stream.read()
        except OSError:
            return self
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                self.entries[entry["path"]] = entry  # A later line for the same path replaces an earlier one.
        self._size += end
        return self

    def add(self, entries):
        """Append payload entries; the caller holds the record's lock."""

        _private_folder(self.folder)
        with _open_private(self.folder / "payloads.jsonl", "ab") as stream:
            stream.write(b"".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
                                  for entry in entries))
            _fsync(stream)
        self.load()

    def resolve(self, refs, depth=0):
        """The full list of item sha256 of an input stored as ``refs`` (a list, or a delta on an earlier payload)."""

        if isinstance(refs, list):
            return refs
        if not isinstance(refs, dict) or depth > 100000:
            raise ValueError("Unreadable input reference.")
        base = refs.get("base")
        if base not in self._full:
            entry = self.entries.get(base)
            if entry is None or entry.get("input") is None:
                raise ValueError(f"Missing base payload {base}.")
            self._full[base] = self.resolve(entry["input"], depth + 1)
        return self._full[base][:refs["keep"]] + list(refs["add"])

    def payload(self, path):
        """The exact bytes of one stored payload (KeyError when it is not stored)."""

        entry = self.entries.get(path)
        if entry is None:
            self.load()
            entry = self.entries[path]
        inputs = self.resolve(entry["input"]) if entry.get("input") is not None else None
        return rebuild_payload(self.record.objects, entry["store"], inputs)

    def input(self, path):
        """The full input list (item sha256) of one stored request, or None."""

        entry = self.entries.get(path) or self.load().entries.get(path)
        return self.resolve(entry["input"]) if entry and entry.get("input") is not None else None

    def copy(self, name):
        """A byte copy of the raw bundle's manifest.json or trace.jsonl, or None."""

        target = self.folder / (name + ".gz")
        try:
            return gzip.decompress(target.read_bytes())
        except (OSError, EOFError, gzip.BadGzipFile):
            return None


class TraceIngest:
    """Stores one raw trace bundle (codex-trace/<label>/trace-<id>-<thread>/) into the record as Codex writes it.

    Every event is read once, in order. Each payload an event names is stored (split_payload()), its stored copy
    rebuilt and compared with the file byte for byte (else the exact bytes are kept as one object), and only then is the
    file removed. A model request gets its line in calls.jsonl when its end event arrives; finish() gives the requests
    the process never ended status "interrupted", stores payload files no event named, keeps byte copies of the
    bundle's manifest.json and trace.jsonl and removes the raw bundle once those copies read back. A bundle whose
    manifest or events are not of TRACE_SCHEMA_VERSION is left as it is; its requests are marked "trace format not
    recognized".
    """

    def __init__(self, record, label, raw):
        self.record, self.label, self.raw = record, label, Path(raw)
        self.stored = StoredBundle(record, label, self.raw.name).load()
        self.offset, self.partial = 0, b""
        self.open = {}  # request id -> its call line so far
        self.agents = {}  # thread id -> agent path
        self.inputs = {}  # thread id -> (path, [sha]) of its latest request
        self.full_inputs = {}  # request path -> (path, [sha]) stored by this process
        self.ended = {line.get("callId") for line in record.calls.read()
                      if line.get("process") == label and line.get("bundle") == self.raw.name}
        self.recognized = None
        self.unrecognized = []  # request starts seen in a bundle of an unknown format

    def _check_manifest(self):
        manifest = read_json(self.raw / "manifest.json")
        if manifest is None:
            return None  # Not written yet (or unreadable: finish() decides).
        return isinstance(manifest, dict) and manifest.get("schema_version") == TRACE_SCHEMA_VERSION

    def step(self, final=False):
        """Store what the bundle holds now; with ``final`` (the process ended) everything, then remove it."""

        if self.recognized is None:
            self.recognized = self._check_manifest()
            if self.recognized is None and not final:
                return
            self.recognized = bool(self.recognized)
        try:
            with open(self.raw / "trace.jsonl", "rb") as stream:
                stream.seek(self.offset)
                data = stream.read()
        except OSError:
            data = b""
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        events = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                self.recognized = False
                continue
            events.append(event)
        with self.record.locked():
            calls, entries = [], []
            for event in events:
                if not self.recognized or not isinstance(event, dict) \
                        or event.get("schema_version") != TRACE_SCHEMA_VERSION:
                    self.recognized = False
                    self._note_unrecognized(event)
                    continue
                self._event(event, calls, entries)
            if entries:
                self.stored.add(entries)
                self._verify(entries)
            if final:
                self._finish(calls)
            for call in calls:
                if call.get("response") and call.get("usage") is None:
                    call["usage"] = self._usage(call["response"])
            if calls:
                self.record.calls.append(calls)
                self.ended.update(call["callId"] for call in calls)

    def _note_unrecognized(self, event):
        payload = event.get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict) and payload.get("type") in REQUEST_STARTS:
            self.unrecognized.append(event)

    def _payload(self, reference, entries, thread=None):
        """Store the payload file an event names (once); return its path or None."""

        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            return None
        path = reference["path"]
        if path in self.stored.entries or any(entry["path"] == path for entry in entries):
            return path
        kind = (reference.get("kind") or {}).get("type") if isinstance(reference.get("kind"), dict) else None
        try:
            source = self.raw.joinpath(*path.split("/"))
            if source.resolve().parent != (self.raw / "payloads").resolve():
                return None
            raw = source.read_bytes()
        except (OSError, ValueError):
            return None
        previous = self.inputs.get(thread) if kind in REQUEST_KINDS else None
        store, refs, blobs = split_payload(raw, kind, previous)
        self.record.store(blobs)
        entry = {"path": path, "kind": kind, "sha256": _sha(raw), "size": len(raw), "store": store,
                 **({"thread": thread} if thread else {}), **({"input": refs} if refs is not None else {})}
        entries.append(entry)
        if refs is not None:
            full = refs if isinstance(refs, list) else previous[1][:refs["keep"]] + refs["add"]
            self.inputs[thread] = self.full_inputs[path] = (path, full)
        return path

    def _verify(self, entries):
        """Rebuild each new payload from the record; keep the exact bytes of one that differs; remove the files."""

        fixes = []
        for entry in entries:
            source = self.raw.joinpath(*entry["path"].split("/"))
            try:
                rebuilt = self.stored.payload(entry["path"])
            except (KeyError, ValueError, OSError, EOFError, gzip.BadGzipFile):
                rebuilt = None
            if rebuilt is None or _sha(rebuilt) != entry["sha256"]:
                try:
                    raw = source.read_bytes()
                except OSError:
                    continue
                sha = _sha(raw)
                self.record.store({sha: raw})
                # Kept whole; a request keeps its whole input list for the requests stored as changes to it.
                full = self.full_inputs.get(entry["path"])
                fixes.append({**entry, "store": {"object": sha}, "exact": False,
                              **({"input": full[1]} if full else {})})
        if fixes:
            self.stored.add(fixes)
        for entry in entries:
            try:
                if _sha(self.stored.payload(entry["path"])) == entry["sha256"]:
                    self.raw.joinpath(*entry["path"].split("/")).unlink()
            except (KeyError, ValueError, OSError, EOFError, gzip.BadGzipFile):
                pass  # Kept: the record could not rebuild it, so the raw file stays.

    def _event(self, event, calls, entries):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        thread = payload.get("thread_id") or event.get("thread_id")
        for value in payload.values():
            references = value if isinstance(value, list) else [value]
            for reference in references:
                if isinstance(reference, dict) and "raw_payload_id" in reference:
                    self._payload(reference, entries, thread if kind in REQUEST_STARTS else None)
        if kind == "thread_started" and isinstance(payload.get("thread_id"), str):
            self.agents[payload["thread_id"]] = payload.get("agent_path")
        elif kind in REQUEST_STARTS:
            identity = payload.get("inference_call_id") or payload.get("compaction_request_id")
            if not isinstance(identity, str) or identity in self.ended:
                return
            request = payload.get("request_payload") or {}
            self.open[identity] = {
                "program": "codex", "kind": REQUEST_STARTS[kind], "process": self.label, "bundle": self.raw.name,
                "callId": identity, "threadId": thread, "turnId": payload.get("codex_turn_id"),
                "agentPath": self.agents.get(thread), "startedMs": event.get("wall_time_unix_ms"), "endedMs": None,
                "model": payload.get("model"), "provider": payload.get("provider_name"),
                "status": "interrupted", "error": None, "responseId": None, "upstreamRequestId": None,
                "request": request.get("path") if isinstance(request, dict) else None, "response": None,
                "usage": None}
        elif kind in REQUEST_ENDS:
            identity = payload.get("inference_call_id") or payload.get("compaction_request_id")
            call = self.open.pop(identity, None)
            if call is None:
                return
            status, field = REQUEST_ENDS[kind]
            response = payload.get(field) if field else None
            call.update(status=status, endedMs=event.get("wall_time_unix_ms"),
                        error=payload.get("error") or payload.get("reason"),
                        responseId=payload.get("response_id"), upstreamRequestId=payload.get("upstream_request_id"),
                        response=response.get("path") if isinstance(response, dict) else None)
            calls.append(call)

    def _usage(self, path):
        if not path:
            return None
        try:
            value = json.loads(self.stored.payload(path))
        except (KeyError, ValueError, OSError, EOFError, gzip.BadGzipFile):
            return None
        usage = value.get("token_usage") if isinstance(value, dict) else None
        return usage if isinstance(usage, dict) else None

    def _finish(self, calls):
        """The process ended: close the open requests, keep the bundle's files, remove the raw bundle."""

        if not self.recognized:
            for event in self.unrecognized:
                payload = event["payload"]
                identity = payload.get("inference_call_id") or payload.get("compaction_request_id")
                if isinstance(identity, str) and identity not in self.ended:
                    calls.append({"program": "codex", "kind": REQUEST_STARTS[payload["type"]],
                                  "process": self.label, "bundle": self.raw.name, "callId": identity,
                                  "threadId": payload.get("thread_id"), "startedMs": event.get("wall_time_unix_ms"),
                                  "model": payload.get("model"), "status": "trace format not recognized"})
            if not self.unrecognized and f"bundle:{self.raw.name}" not in self.ended:
                calls.append({"program": "codex", "kind": "process", "process": self.label, "bundle": self.raw.name,
                              "callId": f"bundle:{self.raw.name}", "startedMs": _modified_ms(self.raw),
                              "status": "trace format not recognized"})
            return  # The raw files stay.
        for identity, call in list(self.open.items()):
            calls.append(call)  # Never ended: "interrupted".
            self.open.pop(identity)
        entries = []
        payloads = self.raw / "payloads"
        for path in sorted(payloads.glob("*.json")) if payloads.is_dir() else ():
            relative = f"payloads/{path.name}"
            if relative not in self.stored.entries:
                # Written, but no event names it (the process ended between the two).
                self._payload({"raw_payload_id": "", "path": relative, "kind": {"type": "unreferenced"}}, entries)
        if entries:
            self.stored.add(entries)
            self._verify(entries)
        copies = {}
        for name in ("manifest.json", "trace.jsonl"):
            try:
                copies[name] = (self.raw / name).read_bytes()
            except FileNotFoundError:
                copies[name] = None
            except OSError:
                return  # Tried again at the next start.
        _private_folder(self.stored.folder)
        for name, data in copies.items():
            if data is not None:
                write_atomic(self.stored.folder / (name + ".gz"), gzip.compress(data, mtime=0))
        if any(data is not None and self.stored.copy(name) != data for name, data in copies.items()):
            return
        if any(self.raw.joinpath(*path.split("/")).exists() for path in self.stored.entries):
            return  # A payload the record could not rebuild stays with its bundle.
        shutil.rmtree(self.raw, ignore_errors=True)


def _modified_ms(path):
    try:
        return int(Path(path).stat().st_mtime * 1000)
    except OSError:
        return None


# --- Writing a job's record from its runner ----------------------------------------------------------------------------

_LABELS = threading.local()


@contextlib.contextmanager
def labelled(**labels):
    """Name the record's labels (role, node, round, ...) of the model calls this thread makes inside the block."""

    previous = getattr(_LABELS, "value", {})
    _LABELS.value = {**previous, **{key: value for key, value in labels.items() if value is not None}}
    try:
        yield
    finally:
        _LABELS.value = previous


def current_labels():
    return dict(getattr(_LABELS, "value", {}))


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "call").lower()).strip("-")[:40] or "call"


def note_problem(record, text):
    """Keep one sentence about something the record could not do (GET /jobs/<id>/model-calls shows it)."""

    try:
        with record.locked():
            with _open_private(record.folder / "problems.jsonl", "ab") as stream:
                stream.write(json.dumps({"time": _iso(_now_ms()), "text": str(text)[:2000]},
                                        ensure_ascii=False).encode() + b"\n")
                _fsync(stream)
    except OSError:
        pass


class TraceProcess:
    """The trace folder of one Codex process (codex-trace/<label>/) and its ingest (TraceIngest per bundle).

    The runner holds an exclusive lock on its .claim file while the process may run; a folder whose claim nobody
    holds is a leftover of a runner that ended without finishing it (Recorder.leftovers()).
    """

    def __init__(self, recorder, label, labels):
        self.recorder, self.record, self.label = recorder, recorder.record, label
        self.folder = self.record.folder / "codex-trace" / label
        self.claim = FileLock(self.folder / ".claim")
        self.ingests = {}
        self.lock = threading.Lock()
        self.finished = False
        with self.record.locked():
            _private_folder(self.folder)
            self.claim.acquire()
            write_atomic(self.record.folder / "codex" / label / "process.json",
                         (json.dumps({**labels, "process": label, "program": "codex", "startedMs": _now_ms()},
                                     ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def environment(self, environment):
        """``environment`` with Codex's trace root set to this process's folder."""

        return {**environment, TRACE_ROOT_ENV: str(self.folder)}

    def _bundles(self):
        try:
            return sorted(path for path in self.folder.iterdir() if path.is_dir() and path.name.startswith("trace-"))
        except OSError:
            return []

    def poll(self, final=False):
        """Store what the process's trace holds now (everything, and remove the raw files, with ``final``)."""

        with self.lock:
            if self.finished:
                return
            for bundle in self._bundles():
                ingest = self.ingests.get(bundle.name)
                if ingest is None:
                    ingest = self.ingests[bundle.name] = TraceIngest(self.record, self.label, bundle)
                try:
                    ingest.step(final=final)
                except Exception as exc:  # noqa: BLE001 - recording never stops a model call.
                    note_problem(self.record, f"The trace of Codex process {self.label} could not be stored "
                                              f"({type(exc).__name__}: {exc}); its raw files are kept.")
            if final:
                self.finished = True
                if not self._bundles():
                    self.claim.release()
                    shutil.rmtree(self.folder, ignore_errors=True)
                else:
                    self.claim.release()  # What is left is kept as it is, or stored at the next start.

    def finish(self):
        """The process ended: store its whole trace (see TraceIngest)."""

        self.recorder._forget(self)
        try:
            self.poll(final=True)
        except Exception as exc:  # noqa: BLE001
            note_problem(self.record, f"The trace of Codex process {self.label} could not be stored ({exc}).")


class ClaudeStream:
    """Claude Code's stream-json lines of one process (claude/<label>.jsonl, then .jsonl.gz) and its call lines.

    Each line is appended as the CLI wrote it, after TCS Prover's redaction. A model request is one assistant message
    (its id): its lines, the user lines sent since the previous request of the same agent (the main agent, or one
    subagent by its tool-use id) and its usage. Its call line is written when a later line of the same agent, or the
    turn's result, shows it ended; at the end of the process the open ones are written too ("interrupted" when no
    result followed them). The file is synced at each call line.
    """

    def __init__(self, recorder, label, labels):
        self.recorder, self.record, self.label = recorder, recorder.record, label
        folder = _private_folder(self.record.folder / "claude")
        self.path = folder / f"{label}.jsonl"
        self.claim = FileLock(folder / f"{label}.claim")
        self.claim.acquire()
        write_atomic(folder / f"{label}.json",
                     (json.dumps({**labels, "process": label, "program": "claude", "startedMs": _now_ms()},
                                 ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        self.stream = _open_private(self.path, "ab")
        self.count = 0
        self.lock = threading.Lock()
        self.open = {}  # message id -> call line so far
        self.order = []
        self.sent = {}  # agent -> line numbers of the user lines since its previous request
        self.session = None
        self.finished = False

    def line(self, text):
        """Append one stream-json line (already redacted)."""

        with self.lock:
            if self.finished:
                return
            data = text if text.endswith("\n") else text + "\n"
            try:
                self.stream.write(data.encode("utf-8", "replace"))
            except (OSError, ValueError):
                return
            number, self.count = self.count, self.count + 1
            try:
                message = json.loads(text)
            except (ValueError, UnicodeError):
                return
            if isinstance(message, dict):
                self._track(message, number)

    def _track(self, message, number):
        kind = message.get("type")
        if isinstance(message.get("session_id"), str):
            self.session = message["session_id"]
        agent = message.get("parent_tool_use_id")
        body = message.get("message") if isinstance(message.get("message"), dict) else {}
        ended = []
        if kind == "assistant" and isinstance(body.get("id"), str):
            identity = body["id"]
            for other in list(self.open):
                if other != identity and self.open[other]["agentPath"] == agent:
                    ended.append(other)
            call = self.open.get(identity)
            if call is None:
                call = self.open[identity] = {
                    "program": "claude", "kind": "request", "process": self.label, "callId": identity,
                    "threadId": self.session, "agentPath": agent, "startedMs": _now_ms(), "endedMs": None,
                    "model": body.get("model"), "status": "interrupted", "usage": body.get("usage"),
                    "requestId": message.get("request_id"),
                    "stream": {"file": f"claude/{self.label}.jsonl.gz", "lines": [number, number],
                               "sent": self.sent.pop(agent, [])}}
                self.order.append(identity)
            call["stream"]["lines"][1] = number
            call["endedMs"] = _now_ms()
        elif kind == "user":
            for other in list(self.open):
                if self.open[other]["agentPath"] == agent:
                    ended.append(other)
            self.sent.setdefault(agent, []).append(number)
        elif kind == "result":
            ended = list(self.open)
            for identity in ended:
                self.open[identity]["status"] = "completed"
        for identity in ended:
            self.open[identity]["status"] = "completed"
        if ended:
            self._write([self.open.pop(identity) for identity in self.order if identity in ended])
            self.order = [identity for identity in self.order if identity not in ended]

    def _write(self, calls):
        try:
            _fsync(self.stream)
            with self.record.locked():
                self.record.calls.append(calls)
        except OSError as exc:
            note_problem(self.record, f"Claude Code's stream {self.label} could not be recorded ({exc}).")

    def finish(self):
        """The CLI ended: write its open calls, compress the stream and verify the copy."""

        self.recorder._forget(self)
        with self.lock:
            if self.finished:
                return
            self.finished = True
            calls = [self.open.pop(identity) for identity in self.order if identity in self.open]
            self.order = []
            if calls:
                self._write(calls)
            try:
                self.stream.close()
            except OSError:
                pass
        if compress_stream(self.record, self.path):
            try:
                self.claim.path.unlink()  # Finished: nothing is left for a later start to store.
            except OSError:
                pass
        self.claim.release()


def compress_stream(record, path):
    """claude/<label>.jsonl -> .jsonl.gz, verified, then the plain file goes."""

    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    target = Path(str(path) + ".gz")
    try:
        write_atomic(target, gzip.compress(data, mtime=0))
        if gzip.decompress(target.read_bytes()) != data:
            return False
        Path(path).unlink()
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        note_problem(record, f"Claude Code's stream {Path(path).name} could not be compressed ({exc}).")
        return False
    return True


def stored_name(name):
    """The name a copied session file has in sources/: gzip-compressed, unless it already is (.zst, .gz)."""

    return name if name.endswith((".zst", ".gz")) else name + ".gz"


def copied_size(target):
    """The size of the source a copy in sources/ holds: its gzip trailer's (size modulo 4 GiB), else its own."""

    target = Path(target)
    try:
        if target.name.endswith(".gz"):
            with open(target, "rb") as stream:
                stream.seek(-4, os.SEEK_END)
                return int.from_bytes(stream.read(4), "little")
        return target.stat().st_size
    except OSError:
        return None


def _copy_verified(source, target):
    """Copy ``source`` to ``target`` owner-only (gzip-compressed when ``target`` ends in .gz but the source does not)
    and read the copy back; whether it gives back the source's bytes."""

    target = Path(target)
    compress = target.name.endswith(".gz") and not str(source).endswith(".gz")
    _private_folder(target.parent)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    digest = hashlib.sha256()
    try:
        with open(source, "rb") as original, _open_private(temporary, "wb") as stream:
            output = gzip.GzipFile(fileobj=stream, mode="wb", compresslevel=6, mtime=0) if compress else stream
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                digest.update(chunk)
                output.write(chunk)
            if compress:
                output.close()
            _fsync(stream)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    check = hashlib.sha256()
    try:
        with (gzip.open(target, "rb") if compress else open(target, "rb")) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                check.update(chunk)
    except (OSError, EOFError):
        return False
    return check.hexdigest() == digest.hexdigest()


def _session_meta(path):
    """The payload of a Codex session file's first record (its session_meta), or {}."""

    if str(path).endswith(".zst"):
        return {}
    try:
        with open(path, "rb") as stream:
            first = stream.readline(SESSION_META_BYTES)
        value = json.loads(first)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}
    payload = value.get("payload") if isinstance(value, dict) and value.get("type") == "session_meta" else None
    return payload if isinstance(payload, dict) else {}


_METAS = {}  # session file -> its session_meta payload (a file's first record never changes)


def codex_session_files(home, thread):
    """The session files of Codex thread ``thread`` in CODEX_HOME ``home``: its own files (each segment, plain or
    compressed) and those of the subagent threads it started, at any depth (their session_meta names the root's
    session or their parent), as [(path, path relative to sessions/)]. Subagents are looked for in the day folders
    from the thread's first one on."""

    sessions = Path(home) / "sessions"
    own, candidates = [], []
    try:
        folders = sorted(path for path in sessions.glob("*/*/*") if path.is_dir())
    except OSError:
        return []
    for folder in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            match = _ROLLOUT.match(name)
            if match:
                (own if match.group(1) == thread else candidates).append((folder / name, match.group(1)))
    if not own:
        return []
    first = min(path.parent for path, _ in own)
    family, found = {thread}, [path for path, _ in own]
    candidates = [(path, owner) for path, owner in candidates if path.parent >= first]
    changed = True
    while changed:
        changed = False
        for path, owner in candidates:
            if owner in family:
                if path not in found:
                    found.append(path)
                    changed = True
                continue
            meta = _METAS.get(path)
            if meta is None:
                meta = _METAS[path] = {key: value for key, value in _session_meta(path).items()
                                       if key in ("session_id", "parent_thread_id")}
            if meta.get("session_id") in family or meta.get("parent_thread_id") in family:
                family.add(owner)
                found.append(path)
                changed = True
    return [(path, path.relative_to(sessions).as_posix()) for path in sorted(set(found))]


def claude_session_files(config_dir, session):
    """The files of one saved Claude Code session: projects/<project>/<session>.jsonl and its folder
    projects/<project>/<session>/ (subagents/, tool-results/), as [(path, path relative to projects/)]."""

    projects = Path(config_dir) / "projects"
    found = []
    try:
        for path in projects.glob(f"*/{session}.jsonl"):
            if path.is_file() and not path.is_symlink():
                found.append(path)
        for folder in projects.glob(f"*/{session}"):
            if folder.is_dir() and not folder.is_symlink():
                for path in folder.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        found.append(path)
    except OSError:
        return []
    return [(path, path.relative_to(projects).as_posix()) for path in sorted(found)]


class Recorder:
    """The record of the job one runner process works for (RECORD_ENV); every method is safe to call from any thread
    and never raises into a model call: what it cannot do is said in the record's problems."""

    def __init__(self, folder):
        self.record = Record.create(folder)
        self.active = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._poller = None

    def _label(self, role):
        return f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{_slug(role)}-{uuid.uuid4().hex[:6]}"

    def codex_process(self, environment, **labels):
        """A Codex process about to start: (its environment with the trace root, its TraceProcess)."""

        labels = {**current_labels(), **{key: value for key, value in labels.items() if value is not None}}
        process = TraceProcess(self, self._label(labels.get("role")), labels)
        with self.lock:
            self.active.append(process)
            if self._poller is None:
                self._poller = threading.Thread(target=self._poll, daemon=True)
                self._poller.start()
        return process.environment(environment), process

    def claude_stream(self, **labels):
        """A Claude Code process about to start: its ClaudeStream."""

        labels = {**current_labels(), **{key: value for key, value in labels.items() if value is not None}}
        stream = ClaudeStream(self, self._label(labels.get("role")), labels)
        with self.lock:
            self.active.append(stream)
        return stream

    def _forget(self, process):
        with self.lock:
            if process in self.active:
                self.active.remove(process)

    def _poll(self):
        while not self._stop.wait(TRACE_POLL_SECONDS):
            with self.lock:
                processes = [process for process in self.active if isinstance(process, TraceProcess)]
            for process in processes:
                process.poll()

    def keep_codex_session(self, home, thread):
        """Copy one Codex session (each segment, and its subagents' sessions) into sources/ before it is deleted;
        whether every copy was verified."""

        return self._copy(codex_session_files(home, thread), f"sources/codex/{thread}", f"Codex session {thread}")

    def copy_codex_thread(self, home, thread):
        """Copy an author's or coordinator's Codex thread into sources/: the files that are new or changed."""

        return self._copy(codex_session_files(home, thread), f"sources/codex/{thread}", f"Codex thread {thread}",
                          changed_only=True)

    def keep_claude_session(self, config_dir, session):
        """Copy one saved Claude Code session (with subagents/ and tool-results/) into sources/ before it is
        deleted; whether every copy was verified."""

        return self._copy(claude_session_files(config_dir, session), "sources/claude", f"Claude session {session}")

    def copy_claude_session(self, config_dir, session):
        """Copy an author's or coordinator's Claude Code session into sources/: the files that are new or changed."""

        return self._copy(claude_session_files(config_dir, session), "sources/claude", f"Claude session {session}",
                          changed_only=True)

    def _copy(self, files, prefix, what, changed_only=False):
        if not files:
            note_problem(self.record, f"{what}: no session file was found to copy.")
            return False
        try:
            for source, relative in files:
                target = self.record.path(f"{prefix}/{stored_name(relative)}")
                if changed_only:
                    # The CLIs only append to a session file: one of the same size is already copied.
                    if copied_size(target) == os.stat(source).st_size % 2 ** 32:
                        continue
                if not _copy_verified(source, target):
                    note_problem(self.record, f"{what}: the copy of {relative} did not read back the same.")
                    return False
        except (OSError, ValueError) as exc:
            note_problem(self.record, f"{what} could not be copied ({exc}).")
            return False
        return True

    def leftovers(self):
        """Store what earlier runners of this job left behind (see store_leftovers())."""

        store_leftovers(self.record)

    def close(self):
        """The runner ends: finish what is still open."""

        self._stop.set()
        with self.lock:
            active = list(self.active)
        for process in active:
            process.finish()


def store_leftovers(record):
    """Store what processes of the job left behind when they ended without finishing their part of the record (a
    crash, a kill): trace folders and plain Claude streams whose claim nobody holds. A request that never ended
    gets status "interrupted"."""

    for folder in sorted((record.folder / "codex-trace").glob("*")):
        if not folder.is_dir() or not _LABEL.match(folder.name):
            continue
        claim = FileLock(folder / ".claim")
        try:
            if not claim.acquire(blocking=False):
                continue  # Its runner still works.
        except OSError:
            continue
        try:
            for bundle in sorted(path for path in folder.iterdir() if path.is_dir() and path.name.startswith("trace-")):
                if time.time() - (_modified_ms(bundle / "trace.jsonl") or 0) / 1000 < LEFTOVER_QUIET_SECONDS:
                    continue  # Perhaps still written by a Codex process whose runner died: a later start stores it.
                try:
                    TraceIngest(record, folder.name, bundle).step(final=True)
                except Exception as exc:  # noqa: BLE001
                    note_problem(record, f"A leftover trace of {folder.name} could not be stored ({exc}).")
        finally:
            claim.release()
        try:
            if not any(path.is_dir() for path in folder.iterdir()):
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
    for path in sorted((record.folder / "claude").glob("*.jsonl")):
        claim = FileLock(path.with_suffix(".claim"))
        try:
            if not claim.acquire(blocking=False):
                continue
        except OSError:
            continue
        try:
            finish_leftover_stream(record, path)
        finally:
            claim.release()


def has_leftovers(folder):
    """Whether a record folder holds raw traces or plain Claude streams (store_leftovers() has work)."""

    folder = Path(folder)
    return any(folder.glob("codex-trace/*/trace-*")) or any(folder.glob("claude/*.jsonl"))


def finish_leftover_stream(record, path):
    """A Claude stream whose runner ended without finishing it: write the calls it lacks, then compress it."""

    label = path.name[:-len(".jsonl")]
    written = {line.get("callId") for line in record.calls.read() if line.get("process") == label}
    replay = ClaudeStream.__new__(ClaudeStream)
    replay.recorder, replay.record, replay.label = None, record, label
    replay.open, replay.order, replay.sent, replay.session, replay.finished = {}, [], {}, None, False
    calls = []
    replay._write = lambda lines: calls.extend(lines)
    try:
        with open(path, "rb") as stream:
            for number, line in enumerate(stream):
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(message, dict):
                    replay._track(message, number)
    except OSError:
        return
    calls.extend(replay.open[identity] for identity in replay.order if identity in replay.open)
    calls = [call for call in calls if call["callId"] not in written]
    for call in calls:
        call["startedMs"] = call["endedMs"] = None  # Unknown after the fact.
    if calls:
        with record.locked():
            record.calls.append(calls)
    compress_stream(record, path)


_RECORDER = None
_RECORDER_LOCK = threading.Lock()
# Why this process's record could not be opened (recorder() returned None although RECORD_ENV names one).
startup_problem = ""


def recorder():
    """The record of the job this process works for (RECORD_ENV), or None when it records nothing."""

    global _RECORDER, startup_problem
    with _RECORDER_LOCK:
        if _RECORDER is None:
            folder = os.environ.get(RECORD_ENV, "").strip()
            if not folder:
                return None
            try:
                _RECORDER = Recorder(Path(folder))
                _RECORDER.leftovers()
            except (OSError, RecordError, ValueError) as exc:
                startup_problem = f"The record of this job cannot be written here ({exc}); its model calls run unrecorded."
                _RECORDER = False
        return _RECORDER or None


def close():
    """Finish the record of this process's job (see Recorder.close)."""

    if _RECORDER:
        _RECORDER.close()


# --- Snapshots and web-cutoff copies (by the server) -------------------------------------------------------------------

def snapshot(record, transcript=None, job_settings=None, prompts=None):
    """Copy the job's transcript, settings and prompt set into transcript/, so the record holds them without the run.

    The transcript (append-only) is added to transcript/transcript.jsonl.gz as one more gzip member holding the
    complete lines written since the last snapshot, after a check that the copied part is unchanged (else it is copied
    whole again); each new member is read back. job-settings.json and the prompts/ folder are copied whole.
    """

    folder = _private_folder(record.folder / "transcript")
    with record.locked():
        if transcript is not None:
            _snapshot_transcript(folder, Path(transcript))
        if job_settings is not None:
            try:
                data = Path(job_settings).read_bytes()
            except OSError:
                data = None
            if data is not None:
                write_atomic(folder / "job-settings.json", data)
        if prompts is not None and Path(prompts).is_dir():
            for source in sorted(Path(prompts).rglob("*")):
                if source.is_file() and not source.is_symlink():
                    target = folder / "prompts" / source.relative_to(prompts)
                    try:
                        data = source.read_bytes()
                        if not target.is_file() or target.read_bytes() != data:
                            write_atomic(target, data)
                    except OSError:
                        pass


def _snapshot_transcript(folder, source):
    try:
        data = source.read_bytes()
    except OSError:
        return
    data = data[:data.rfind(b"\n") + 1]
    state = read_json(folder / "transcript.json", {}) or {}
    copied = state.get("bytes") if isinstance(state.get("bytes"), int) else 0
    target = folder / "transcript.jsonl.gz"
    whole = not (target.is_file() and 0 < copied <= len(data) and _sha(data[:copied]) == state.get("sha256"))
    if whole:
        write_atomic(target, gzip.compress(data, mtime=0))
        good = gzip.decompress(target.read_bytes()) == data
    elif copied == len(data):
        return
    else:
        member = gzip.compress(data[copied:], mtime=0)
        with _open_private(target, "ab") as stream:
            offset = stream.seek(0, os.SEEK_END)
            stream.write(member)
            _fsync(stream)
        with open(target, "rb") as stream:
            stream.seek(offset)
            good = gzip.decompress(stream.read()) == data[copied:]
    if good:
        write_atomic(folder / "transcript.json", json.dumps({"bytes": len(data), "sha256": _sha(data)}).encode())
    else:
        write_atomic(folder / "transcript.json", b"{}")  # The next snapshot copies it whole.


def read_transcript(record):
    """The transcript snapshot's lines, parsed (an empty list when there is none)."""

    try:
        data = gzip.decompress((record.folder / "transcript" / "transcript.jsonl.gz").read_bytes())
    except (OSError, EOFError, gzip.BadGzipFile):
        return []
    records = []
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def copy_web_cutoff(record, policy_path):
    """Copy what a web-cutoff run leaves behind into sources/web-cutoff/: the author's session files from the run's
    bundle (<codexHome>/author/sessions) and the URL discovery folders (discovery-state/discovery-*), new or changed
    files only, never a login (LOGIN_FILES). Its isolation is untouched: nothing of this reaches its processes."""

    policy = read_json(policy_path, {})
    if not isinstance(policy, dict):
        return
    controller = Path(policy_path).parent
    home = (policy.get("isolation") or {}).get("codexHome") if isinstance(policy.get("isolation"), dict) else None
    groups = []
    if isinstance(home, str) and home:
        groups.append((Path(home) / "author" / "sessions", "sources/web-cutoff/author-sessions"))
    groups.append((controller / "discovery-state", "sources/web-cutoff/discovery"))
    for root, prefix in groups:
        if not root.is_dir():
            continue
        for source in sorted(root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(root).as_posix()
            if prefix.endswith("discovery") and not relative.startswith("discovery-"):
                continue
            if source.name.lower() in LOGIN_FILES:
                continue  # A discovery call's copy of the login.
            try:
                target = record.path(f"{prefix}/{stored_name(relative)}")
                if copied_size(target) == source.stat().st_size % 2 ** 32:
                    continue
                if not _copy_verified(source, target):
                    note_problem(record, f"The web-cutoff file {relative} did not copy the same.")
            except (OSError, ValueError) as exc:
                note_problem(record, f"The web-cutoff file {relative} could not be copied ({exc}).")


def read_source(path):
    """The bytes of a copied session file (gzip-compressed or not); None when unreadable or compressed with zstd."""

    path = Path(path)
    try:
        if path.name.endswith(".gz"):
            return gzip.decompress(path.read_bytes())
        return None if path.name.endswith(".zst") else path.read_bytes()
    except (OSError, EOFError, gzip.BadGzipFile):
        return None


def folder_size(folder):
    """The bytes of every file in ``folder``."""

    total = 0
    for directory, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total
