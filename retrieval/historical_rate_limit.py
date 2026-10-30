"""Controller-owned, cross-process pacing for historical retrieval providers.

Each active request holds an OS lane lock through its entire body read. Wayback
has four lanes; arXiv remains strictly single-connection. A separate short lock
coordinates request starts and cooldowns without serializing slow body reads.
Waiting requests are admitted in arrival order across all processes: each holds
a locked ticket file, and only the oldest live ticket may take a lane and start.
Locks never expire while a request runs, and the kernel releases them on crash.
Persistent state contains only timestamps, never queries or credentials.
"""

from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


# The two arXiv API hostnames share a single connection and pacing budget.
HOST_LIMITS = {
    "arxiv.org": ("arxiv", 3.0),
    "export.arxiv.org": ("arxiv", 3.0),
    "web.archive.org": ("wayback", 3.0),
    "api.crossref.org": ("crossref", 1.0),
    "api.search.brave.com": ("brave", 1.0),
    "lite.duckduckgo.com": ("duckduckgo", 1.0),
}
HOST_CONCURRENCY = {"wayback": 4}
POLL_SECONDS = 0.05
MAX_SLEEP_SECONDS = 0.25
MAX_STATE_BYTES = 4096
# A provider's Retry-After is honoured up to this long; a larger value (or one
# an older version stored) would otherwise stall every job for its whole span.
MAX_COOLDOWN_SECONDS = 600.0
QUARANTINE = ".quarantine"


class RateLimitError(RuntimeError):
    """A sanitized coordination failure safe to pass to the broker boundary.

    ``retry_at`` (wall-clock seconds) is set when a provider cooldown outlasts
    the caller's deadline, so the caller can say when to try again.
    """

    def __init__(self, message, code="rate_limit_state", retry_at=None):
        super().__init__(message)
        self.code = code
        self.retry_at = retry_at


def _absolute_canonical(value):
    try:
        path = Path(value)
        valid = path.is_absolute() and path.absolute() == path.resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RateLimitError("Rate-limit paths must be absolute and contain no links.", "rate_limit_policy") from exc
    if not valid:
        raise RateLimitError("Rate-limit paths must be absolute and contain no links.", "rate_limit_policy")
    return path


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _regular_private_file(path, descriptor):
    info = os.fstat(descriptor)
    visible = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or path.is_symlink()
            or getattr(visible, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or (info.st_dev, info.st_ino) != (visible.st_dev, visible.st_ino)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or (os.name != "nt" and info.st_mode & 0o022)):
        raise RateLimitError("Shared rate-limit state is not a regular controller-owned file.")


def _owned_regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            and (not hasattr(os, "getuid") or info.st_uid == os.getuid()))


def _bucket_interval(bucket):
    return max(interval for name, interval in HOST_LIMITS.values() if name == bucket)


def _lock(descriptor):
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor):
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class _Ticket:
    """This request's place in a provider's arrival-order queue."""

    def __init__(self, path, descriptor):
        self.path, self.descriptor = path, descriptor

    def release(self):
        if self.descriptor is None:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass  # A stale ticket is recognized by its released lock.
        finally:
            try:
                _unlock(self.descriptor)
            except OSError:
                pass
            os.close(self.descriptor)
            self.descriptor = None


class SharedHostLimiter:
    """Share provider pacing across processes using an explicit policy directory.

    ``workspace_root`` is the solver workspace forbidden from owning this state.
    The controller must also keep the directory outside every other solver's
    workspace. ``deadline`` is an absolute ``time.monotonic()`` value. Callers
    must bound their HTTP operation to that deadline while holding ``slot``.
    Clock injection is solely for deterministic tests, never a broker option.
    Our own loosened folder or lock modes are tightened again; a linked, foreign
    or corrupt state name is moved aside into ``.quarantine`` (``on_quarantine``
    receives the reason) and pacing restarts conservatively from one interval.
    """

    def __init__(self, state_directory, *, workspace_root, clock=time.monotonic,
                 wall_clock=time.time, sleep=time.sleep, on_quarantine=None):
        self.directory = _absolute_canonical(state_directory)
        self.workspace = _absolute_canonical(workspace_root)
        if self.directory == self.workspace or self.workspace in self.directory.parents:
            raise RateLimitError("Rate-limit state must be outside the solver workspace.", "rate_limit_policy")
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.on_quarantine = on_quarantine
        self._local = threading.local()
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._check_directory()
        except OSError as exc:
            raise RateLimitError("Cannot initialize the shared rate-limit directory.") from exc

    def _check_directory(self):
        if _absolute_canonical(self.directory) != self.directory or not self.directory.is_dir():
            raise RateLimitError("Shared rate-limit directory has changed.")
        info = self.directory.stat()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RateLimitError("Rate-limit directory is not controller-owned.")
        if os.name != "nt" and info.st_mode & 0o022:
            # A backup restore or chmod loosened our own folder: tighten it
            # instead of stopping every job's retrieval.
            try:
                os.chmod(self.directory, 0o700)
            except OSError as exc:
                raise RateLimitError("Rate-limit directory is not controller-owned.") from exc
            if self.directory.stat().st_mode & 0o022:
                raise RateLimitError("Rate-limit directory is not controller-owned.")

    def _move_aside(self, path, reason):
        """Rename one invalid state name into the private quarantine folder, never following it."""
        folder = self.directory / QUARANTINE
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise RateLimitError("Shared rate-limit quarantine is not a private folder.")
        os.rename(path, folder / f"{time.time_ns()}-{os.urandom(4).hex()}-{path.name[:64]}")
        for old in sorted(folder.iterdir(), key=lambda item: item.name)[:-64]:
            if old.is_symlink() or not old.is_dir():
                old.unlink(missing_ok=True)
        if self.on_quarantine is not None:
            try:
                self.on_quarantine(reason)
            except Exception:
                pass  # The moved name itself remains the record.

    def _prepare_file(self, path, timing=False):
        """Repair our own lock name before opening it; move anything else aside.

        Timing state that others could have written is never trusted: it is
        moved aside too. Returns True when the name was moved aside.
        """
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if not _owned_regular(info) or (timing and os.name != "nt" and info.st_mode & 0o022):
            self._move_aside(path, "invalid shared pacing state")
            return True
        if os.name != "nt" and info.st_mode & 0o022:
            os.chmod(path, 0o600)
        return False

    def _bucket(self, host):
        if not isinstance(host, str) or host.lower().rstrip(".") not in HOST_LIMITS:
            raise RateLimitError("Rate-limit host is not approved.", "rate_limit_policy")
        return HOST_LIMITS[host.lower().rstrip(".")]

    def _deadline(self, deadline):
        if (not _finite_number(deadline)
                or deadline <= self.clock()):
            raise RateLimitError("Historical retrieval deadline was exhausted while pacing requests.", "deadline_exceeded")

    def _pause(self, seconds, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            self._deadline(deadline)
        self.sleep(min(seconds, remaining, MAX_SLEEP_SECONDS))
        self._deadline(deadline)

    @contextmanager
    def _locked(self, bucket, deadline):
        """Hold one short timing-state mutex, never across provider I/O."""
        with self._lock_any((bucket,), deadline):
            yield

    @contextmanager
    def _lock_any(self, names, deadline):
        """Hold one available kernel lock until exit, bounded by a deadline."""
        self._deadline(deadline)
        self._check_directory()
        descriptors = []
        locked = None
        try:
            try:
                for name in names:
                    path = self.directory / (name + ".lock")
                    self._prepare_file(path)
                    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                                         | getattr(os, "O_NONBLOCK", 0), 0o600)
                    descriptors.append((path, descriptor))
                    os.set_inheritable(descriptor, False)
                    _regular_private_file(path, descriptor)
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                while locked is None:
                    self._deadline(deadline)
                    for path, descriptor in descriptors:
                        try:
                            _lock(descriptor)
                        except OSError as exc:
                            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                                raise
                            continue
                        locked = descriptor
                        _regular_private_file(path, descriptor)
                        break
                    if locked is None:
                        self._pause(POLL_SECONDS, deadline)
            except OSError as exc:
                raise RateLimitError("Cannot coordinate shared historical requests.") from exc
            yield
        finally:
            for _, descriptor in descriptors:
                try:
                    if locked == descriptor:
                        _unlock(descriptor)
                finally:
                    os.close(descriptor)

    def _fresh_state(self, bucket):
        """Restart pacing one full interval from now, as after any recorded start; caller holds the mutex."""
        now, interval = self.clock(), _bucket_interval(bucket)
        state = {"version": 1, "next_wall": self.wall_clock() + interval,
                 "next_monotonic": now + interval, "written_monotonic": now}
        self._save(bucket, state)
        return state

    def _load(self, bucket):
        path = self.directory / (bucket + ".json")
        try:
            if self._prepare_file(path, timing=True):
                return self._fresh_state(bucket)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return {"version": 1, "next_wall": 0.0, "next_monotonic": 0.0, "written_monotonic": 0.0}
        except OSError as exc:
            raise RateLimitError("Cannot read shared rate-limit state.") from exc
        try:
            _regular_private_file(path, descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_STATE_BYTES + 1)
            value = json.loads(data)
            fields = {"next_wall", "next_monotonic", "written_monotonic"}
            if (len(data) > MAX_STATE_BYTES or not isinstance(value, dict)
                    or set(value) != fields | {"version"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or any(not _finite_number(value[key])
                           or value[key] < 0 for key in fields)
                    or value["next_monotonic"] < value["written_monotonic"]):
                raise ValueError("invalid state")
            return value
        except (OSError, UnicodeError, ValueError, RateLimitError):
            pass
        finally:
            os.close(descriptor)
        # Corrupt timing must not disable retrieval for every job.
        try:
            self._move_aside(path, "invalid shared pacing state")
        except OSError as exc:
            raise RateLimitError("Shared rate-limit state is invalid.") from exc
        return self._fresh_state(bucket)

    def _save(self, bucket, value):
        self._check_directory()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                    dir=self.directory, prefix="." + bucket + "-", suffix=".tmp") as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.directory / (bucket + ".json"))
        except (OSError, ValueError) as exc:
            raise RateLimitError("Cannot persist shared rate-limit state.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _wait_seconds(self, state):
        now = self.clock()
        remaining = max(0.0, state["next_wall"] - self.wall_clock())
        # Monotonic time is shared across local processes. If it reset after a
        # reboot, retain the wall-clock cooldown instead of a stale uptime.
        if now >= state["written_monotonic"]:
            remaining = max(remaining, state["next_monotonic"] - now)
        return remaining

    def _enqueue(self, bucket, deadline):
        """Take a locked ticket and wait until no older live ticket remains.

        A ticket appears under its final name only once locked, so a waiting
        process never mistakes a new ticket for a stale one. A crashed owner's
        lock is released by the kernel and its ticket is then removed.
        """
        try:
            name = f"{bucket}-{time.monotonic_ns():020d}-{os.getpid()}-{os.urandom(4).hex()}.ticket"
            path = self.directory / name
            temporary = self.directory / ("." + name + ".new")
            descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise RateLimitError("Cannot coordinate shared historical requests.") from exc
        ticket = _Ticket(path, descriptor)
        try:
            os.set_inheritable(descriptor, False)
            os.write(descriptor, b"\0")
            _lock(descriptor)
            os.rename(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            ticket.release()
            raise RateLimitError("Cannot coordinate shared historical requests.") from exc
        try:
            while not self._first(bucket, name):
                self._pause(POLL_SECONDS, deadline)
        except BaseException:
            ticket.release()
            raise
        return ticket

    def _first(self, bucket, name):
        pattern = re.compile(re.escape(bucket) + r"-[0-9]{20}-[0-9]+-[0-9a-f]{8}\.ticket\Z")
        try:
            older = sorted(entry.name for entry in os.scandir(self.directory)
                           if pattern.fullmatch(entry.name) and entry.name < name)
        except OSError as exc:
            raise RateLimitError("Cannot coordinate shared historical requests.") from exc
        for other in older:
            path = self.directory / other
            try:
                descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            except FileNotFoundError:
                continue
            except OSError:
                return False  # Unreadable: wait rather than overtake it.
            try:
                try:
                    _lock(descriptor)
                except OSError:
                    return False  # An earlier request is still waiting.
                try:
                    path.unlink(missing_ok=True)  # Its owner exited without removing it.
                finally:
                    _unlock(descriptor)
            finally:
                os.close(descriptor)
        return True

    def _cooldown_error(self, remaining):
        retry_at = self.wall_clock() + remaining
        return RateLimitError("The archive provider asked for a pause that outlasts this request's deadline.",
                              "provider_cooldown", retry_at=retry_at)

    def _peek_cooldown(self, bucket, interval, deadline):
        """Fail at once when a provider cooldown already outlasts the deadline."""
        with self._locked(bucket, deadline):
            remaining = min(self._wait_seconds(self._load(bucket)), MAX_COOLDOWN_SECONDS + interval)
        if remaining > interval and self.clock() + remaining >= deadline:
            raise self._cooldown_error(remaining)

    @contextmanager
    def slot(self, host, deadline):
        """Pace one request and hold its concurrency lane through the body read.

        Requests start in arrival order. A recorded provider cooldown that ends
        after ``deadline`` fails at once with code ``provider_cooldown`` and the
        wall-clock ``retry_at`` instead of sleeping until the deadline.
        """
        bucket, interval = self._bucket(host)
        active = getattr(self._local, "active", None)
        if active is None:
            active = self._local.active = set()
        if bucket in active:
            raise RateLimitError("Nested requests to the same provider are not allowed.", "rate_limit_policy")
        self._deadline(deadline)
        self._peek_cooldown(bucket, interval, deadline)
        lanes = tuple(f"{bucket}-lane-{index}" for index in range(HOST_CONCURRENCY.get(bucket, 1)))
        ticket = self._enqueue(bucket, deadline)
        try:
            with self._lock_any(lanes, deadline):
                try:
                    while True:
                        # Other active requests can record Retry-After even while this
                        # admission waits. Reload state under the mutex on every pass.
                        with self._locked(bucket, deadline):
                            state = self._load(bucket)
                            remaining = self._wait_seconds(state)
                            if remaining > MAX_COOLDOWN_SECONDS + interval:
                                # An older or forged state holds a longer pause than
                                # any provider signal is honoured for: shorten it.
                                now = self.clock()
                                remaining = MAX_COOLDOWN_SECONDS + interval
                                self._save(bucket, {"version": 1, "next_wall": self.wall_clock() + remaining,
                                                   "next_monotonic": now + remaining, "written_monotonic": now})
                            if remaining <= 0:
                                self._deadline(deadline)
                                now = self.clock()
                                self._save(bucket, {"version": 1, "next_wall": self.wall_clock() + interval,
                                                   "next_monotonic": now + interval, "written_monotonic": now})
                                break
                            if remaining > interval and self.clock() + remaining >= deadline:
                                raise self._cooldown_error(remaining)
                        self._pause(remaining, deadline)
                    self._deadline(deadline)
                finally:
                    # Started (or failed): the next waiting request may proceed.
                    ticket.release()
                active.add(bucket)
                try:
                    yield
                finally:
                    active.remove(bucket)
        finally:
            ticket.release()

    def defer(self, host, seconds, *, deadline=None):
        """Persist a provider's Retry-After cooldown, up to MAX_COOLDOWN_SECONDS; never shorten an existing one."""
        bucket, _ = self._bucket(host)
        if not _finite_number(seconds) or seconds < 0:
            raise RateLimitError("Provider cooldown duration is invalid.", "rate_limit_policy")
        seconds = min(seconds, MAX_COOLDOWN_SECONDS)

        def persist():
            state = self._load(bucket)
            delay = max(seconds, min(self._wait_seconds(state), MAX_COOLDOWN_SECONDS))
            now, wall = self.clock(), self.wall_clock()
            if not math.isfinite(now + delay) or not math.isfinite(wall + delay):
                raise RateLimitError("Provider cooldown duration is invalid.", "rate_limit_policy")
            self._save(bucket, {"version": 1, "next_wall": wall + delay,
                               "next_monotonic": now + delay, "written_monotonic": now})

        # A slot owns a concurrency lane, not this timing mutex. Always acquire
        # the latter so parallel requests cannot lose a cooldown update.
        with self._locked(bucket, self.clock() + 5.0 if deadline is None else deadline):
            persist()
