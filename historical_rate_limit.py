"""Controller-owned, cross-process pacing for historical retrieval providers.

Each active request holds an OS lane lock through its entire body read. Wayback
has four lanes; arXiv remains strictly single-connection. A separate short lock
coordinates request starts and cooldowns without serializing slow body reads.
Locks never expire while a request runs, and the kernel releases them on crash.
Persistent state contains only timestamps, never queries or credentials.
"""

from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
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


class RateLimitError(RuntimeError):
    """A sanitized coordination failure safe to pass to the broker boundary."""

    def __init__(self, message, code="rate_limit_state"):
        super().__init__(message)
        self.code = code


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


class SharedHostLimiter:
    """Share provider pacing across processes using an explicit policy directory.

    ``workspace_root`` is the solver workspace forbidden from owning this state.
    The controller must also keep the directory outside every other solver's
    workspace. ``deadline`` is an absolute ``time.monotonic()`` value. Callers
    must bound their HTTP operation to that deadline while holding ``slot``.
    Clock injection is solely for deterministic tests, never a broker option.
    """

    def __init__(self, state_directory, *, workspace_root, clock=time.monotonic,
                 wall_clock=time.time, sleep=time.sleep):
        self.directory = _absolute_canonical(state_directory)
        self.workspace = _absolute_canonical(workspace_root)
        if self.directory == self.workspace or self.workspace in self.directory.parents:
            raise RateLimitError("Rate-limit state must be outside the solver workspace.", "rate_limit_policy")
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
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
        if ((hasattr(os, "getuid") and info.st_uid != os.getuid())
                or (os.name != "nt" and info.st_mode & 0o022)):
            raise RateLimitError("Rate-limit directory is not controller-owned.")

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
                            if os.name == "nt":
                                os.lseek(descriptor, 0, os.SEEK_SET)
                                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                            else:
                                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                        if os.name == "nt":
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _load(self, bucket):
        path = self.directory / (bucket + ".json")
        try:
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
        except (OSError, UnicodeError, ValueError) as exc:
            raise RateLimitError("Shared rate-limit state is invalid.") from exc
        finally:
            os.close(descriptor)

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

    @contextmanager
    def slot(self, host, deadline):
        """Pace one request and hold its concurrency lane through the body read."""
        bucket, interval = self._bucket(host)
        active = getattr(self._local, "active", None)
        if active is None:
            active = self._local.active = set()
        if bucket in active:
            raise RateLimitError("Nested requests to the same provider are not allowed.", "rate_limit_policy")
        lanes = tuple(f"{bucket}-lane-{index}" for index in range(HOST_CONCURRENCY.get(bucket, 1)))
        with self._lock_any(lanes, deadline):
            while True:
                # Other active requests can record Retry-After even while this
                # admission waits. Reload state under the mutex on every pass.
                with self._locked(bucket, deadline):
                    state = self._load(bucket)
                    remaining = self._wait_seconds(state)
                    if remaining <= 0:
                        self._deadline(deadline)
                        now = self.clock()
                        self._save(bucket, {"version": 1, "next_wall": self.wall_clock() + interval,
                                           "next_monotonic": now + interval, "written_monotonic": now})
                        break
                self._pause(remaining, deadline)
            self._deadline(deadline)
            active.add(bucket)
            try:
                yield
            finally:
                active.remove(bucket)

    def defer(self, host, seconds, *, deadline=None):
        """Persist a complete Retry-After cooldown; never shorten its duration."""
        bucket, _ = self._bucket(host)
        if not _finite_number(seconds) or seconds < 0:
            raise RateLimitError("Provider cooldown duration is invalid.", "rate_limit_policy")

        def persist():
            state = self._load(bucket)
            delay = max(seconds, self._wait_seconds(state))
            now, wall = self.clock(), self.wall_clock()
            if not math.isfinite(now + delay) or not math.isfinite(wall + delay):
                raise RateLimitError("Provider cooldown duration is invalid.", "rate_limit_policy")
            self._save(bucket, {"version": 1, "next_wall": wall + delay,
                               "next_monotonic": now + delay, "written_monotonic": now})

        # A slot owns a concurrency lane, not this timing mutex. Always acquire
        # the latter so parallel requests cannot lose a cooldown update.
        with self._locked(bucket, self.clock() + 5.0 if deadline is None else deadline):
            persist()
