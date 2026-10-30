"""Owner locks: one process owns a run while it may start its runner; one server serves a runs folder.

A lock is an exclusive flock(2) on the run (or runs) folder itself, so it adds
no file that the author's sandbox could remove or that a run listing shows.
Its descriptor is passed to the runner the owner starts: a runner that outlives
its controller (a crash or a kill) keeps the run locked until it exits, and a
restarted server shows the job as running elsewhere until then. Locks are
shared by everything in one process. Who holds a lock (PID, start time, role,
and a server's port) is recorded beside the private controller state. Where
flock(2) is unavailable (Windows) every lock is a no-op.
"""

import errno
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import threading

from runner import providers

try:
    import fcntl
except ImportError:  # Windows: no flock(2).
    fcntl = None

OWNER_FOLDER = providers.RUN_OWNERS
SERVER_FOLDER = providers.SERVERS
SESSION_FOLDER = providers.SESSIONS
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
# What this process is, in the words other processes show ("web server", "terminal run").
ROLE = "TCS Prover process"
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
TOKEN = re.compile(r"[A-Za-z0-9_-]{20,200}\Z")

_registry_lock = threading.Lock()
_held = {}  # folder -> [descriptor, number of holders in this process]


class OwnedElsewhere(ValueError):
    """Another process (or a runner it left behind) owns this run; the message is safe to show."""

    def __init__(self, message, holder=None):
        super().__init__(message)
        self.holder = holder or {}


class AlreadyRunning(Exception):
    """Another TCS Prover server already serves this runs folder."""

    def __init__(self, holder, url=""):
        super().__init__(f"TCS Prover is already running at {url or 'another port'}.")
        self.holder, self.url = holder, url


def _key(folder):
    return str(Path(folder).resolve())


def _record_path(kind, key, suffix=".json"):
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return providers.state_directory() / kind / f"{name}{suffix}"


def _locked_descriptor(folder):
    """Open `folder` and lock it without waiting; None while another process holds it."""

    descriptor = os.open(folder, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}:
            return None
        raise
    return descriptor


def _write_text(path, text):
    """Replace one small owner-only file atomically; a failure is ignored (it only describes a lock)."""

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_record(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def pid_alive(pid):
    """True, False, or None when it cannot be told."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def describe(holder):
    """Why a run cannot be used here, in plain words."""

    pid, runner = holder.get("pid"), holder.get("runnerPid")
    if pid_alive(pid) is False:
        which = f"A runner (PID {runner})" if isinstance(runner, int) and not isinstance(runner, bool) else "A runner"
        return (f"{which} started by an earlier TCS Prover process (PID {pid}) is still working on this job. "
                "This server takes it over once that runner ends.")
    role = holder.get("role") if isinstance(holder.get("role"), str) and holder.get("role") else ROLE
    since = holder.get("startedAt") if isinstance(holder.get("startedAt"), str) else ""
    detail = (f" (PID {pid}" + (f", started {since}" if since else "") + ")") if isinstance(pid, int) else ""
    return f"This job is running in another {role}{detail}. This server takes it over once that process ends."


def claim(run_dir):
    """Own `run_dir` in this process; return the lock descriptor to pass to its runner.

    Every caller in one process shares the lock. Raise OwnedElsewhere while
    another process holds it. Return None where locks are unavailable, or when
    the folder cannot be opened: a lock must never stop the job itself.
    """

    if fcntl is None:
        return None
    key = _key(run_dir)
    with _registry_lock:
        entry = _held.get(key)
        if entry is not None:
            entry[1] += 1
            return entry[0]
        try:
            descriptor = _locked_descriptor(key)
        except OSError:
            return None
        if descriptor is None:
            holder = read_record(_record_path(OWNER_FOLDER, key))
            raise OwnedElsewhere(describe(holder), holder)
        _held[key] = [descriptor, 1]
    _write_text(_record_path(OWNER_FOLDER, key), json.dumps(
        {"pid": os.getpid(), "startedAt": STARTED_AT, "role": ROLE, "runDir": key}, sort_keys=True) + "\n")
    return descriptor


def note_runner(run_dir, pid):
    """Record the runner that inherited this process's lock of `run_dir`, for the owner record."""

    if fcntl is None or not isinstance(pid, int) or isinstance(pid, bool):
        return
    key = _key(run_dir)
    with _registry_lock:
        if key not in _held:
            return
    path = _record_path(OWNER_FOLDER, key)
    record = read_record(path)
    if record.get("pid") == os.getpid():
        _write_text(path, json.dumps({**record, "runnerPid": pid}, sort_keys=True) + "\n")


def release(run_dir):
    """Give up one hold of `run_dir`; the lock ends with the last one (and with its runner)."""

    _release(_key(run_dir))


def _release(key):
    if fcntl is None:
        return
    with _registry_lock:
        entry = _held.get(key)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        del _held[key]
        try:
            path = _record_path(OWNER_FOLDER, key)
            if read_record(path).get("pid") == os.getpid():
                path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            os.close(entry[0])


def held_elsewhere(run_dir):
    """The owner record of another process that holds `run_dir`, or None when it is free or ours."""

    try:
        claim(run_dir)
    except OwnedElsewhere as exc:
        return exc.holder or {"pid": None}
    release(run_dir)
    return None


class Claims:
    """The owner locks one job holds, released together (also when the job object is discarded)."""

    def __init__(self):
        self._keys = set()
        self._lock = threading.Lock()

    def claim(self, run_dir):
        key = _key(run_dir)
        with self._lock:
            if key in self._keys:
                with _registry_lock:
                    entry = _held.get(key)
                if entry is not None:
                    return entry[0]
                self._keys.discard(key)
            descriptor = claim(key)
            if descriptor is not None:
                self._keys.add(key)
            return descriptor

    def held(self):
        with self._lock:
            return bool(self._keys)

    def release_all(self):
        with self._lock:
            keys, self._keys = self._keys, set()
        for key in keys:
            _release(key)


class Instance:
    """The one server of a runs folder: an exclusive lock on the folder, with its PID, start time and port."""

    def __init__(self, runs):
        self.key = _key(runs)
        self.path = _record_path(SERVER_FOLDER, self.key)
        self.descriptor = None

    def acquire(self):
        """Hold the lock, or return False while another server holds it."""

        if fcntl is None:
            return True
        descriptor = _locked_descriptor(self.key)
        if descriptor is None:
            return False
        self.descriptor = descriptor
        return True

    def holder(self):
        return read_record(self.path)

    def publish(self, **record):
        if self.descriptor is not None:
            _write_text(self.path, json.dumps(
                {"pid": os.getpid(), "startedAt": STARTED_AT, "runs": self.key, **record}, sort_keys=True) + "\n")

    def release(self):
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            try:
                if self.holder().get("pid") == os.getpid():
                    self.path.unlink(missing_ok=True)
            except OSError:
                pass
            os.close(descriptor)


def session_token(runs):
    """The browser session token of one runs folder, kept owner-only so open tabs survive restarts.

    A token file that cannot be read or written gives a token for this launch only.
    """

    path = _record_path(SESSION_FOLDER, _key(runs), ".token")
    try:
        value = path.read_text(encoding="utf-8").strip()
        if TOKEN.fullmatch(value) and not path.is_symlink():
            return value
    except (OSError, UnicodeError):
        pass
    token = secrets.token_urlsafe(32)
    _write_text(path, token + "\n")
    return token
