"""Private controller cache of verified capture bytes, never retrieval results.

The cache does not establish archive provenance or decide a cutoff. Callers must
validate Snapshot records before writing and again after reading, against their
own cutoff. Records use Snapshot's field names, with ``body`` encoded as canonical
base64 ASCII; all other fields are strings. No queries, snippets or model text
are accepted as extra fields.
"""
import base64
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 6 * 1024 * 1024
MAX_ENTRIES = 64
LOCK_TIMEOUT = 5.0
QUARANTINE = ".quarantine"
RECORD_FIELDS = frozenset({"url", "capture", "archive_url", "mime_type", "cdx_digest",
                         "memento_datetime", "body", "body_sha256", "content_encoding"})
ENTRY_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
TEMP_NAME = re.compile(r"\.capture-[a-z0-9_]+\.tmp\Z")


class CacheError(RuntimeError):
    """Static failure safe to expose without source contents or local paths.

    ``kind`` says how a caller may recover: "invalid" (an entry that can be
    moved aside), "busy" (a lock wait timed out) or "unavailable".
    """

    def __init__(self, message="Verified capture cache validation failed.", kind="unavailable"):
        super().__init__(message)
        self.code = "capture_cache_error"
        self.kind = kind if kind in ("invalid", "busy", "unavailable") else "unavailable"


def _canonical_path(value):
    try:
        path = Path(value)
        if not path.is_absolute() or path.absolute() != path.resolve():
            raise ValueError
        return path
    except (OSError, RuntimeError, TypeError, ValueError):
        raise CacheError("Capture cache paths must be absolute and contain no links.") from None


def _private_file(path, descriptor=None):
    info = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or (os.name != "nt" and info.st_mode & 0o077)):
        raise CacheError("Capture cache file is not private controller-owned state.", kind="invalid")
    if descriptor is not None:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise CacheError("Capture cache file changed during access.")
    return info


def _tighten_mode(path):
    """Restore 0600 on our own regular file that only became readable to others.

    A file others could write may have been forged, so it is never repaired.
    """
    if os.name == "nt":
        return
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_mode & 0o077
            and not info.st_mode & 0o022 and (not hasattr(os, "getuid") or info.st_uid == os.getuid())):
        os.chmod(path, 0o600)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class CaptureCache:
    """Share bounded, atomic verified records across broker jobs and restarts.

    ``directory`` must be outside every solver workspace, enforced by controller
    policy; this class additionally checks the supplied ``workspace_root``.
    A kernel lock serializes reads, replacement and oldest-write eviction. It is
    never deleted and is released automatically when a process dies.
    """

    def __init__(self, directory, *, workspace_root, on_quarantine=None):
        self.directory = _canonical_path(directory)
        workspace = _canonical_path(workspace_root)
        if self.directory == workspace or workspace in self.directory.parents:
            raise CacheError("Capture cache must be outside the solver workspace.")
        # Called with a reason after an entry was moved aside; never with contents.
        self.on_quarantine = on_quarantine
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._check_directory()
        except OSError:
            raise CacheError("Cannot initialize the verified capture cache.") from None

    def _check_directory(self):
        _canonical_path(self.directory)
        info = self.directory.lstat()
        if (stat.S_ISDIR(info.st_mode) and os.name != "nt" and info.st_mode & 0o077
                and (not hasattr(os, "getuid") or info.st_uid == os.getuid())):
            # A backup restore, copy or chmod loosened our own folder: tighten it
            # again instead of disabling the cache for every job.
            os.chmod(self.directory, 0o700)
            info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                or (os.name != "nt" and info.st_mode & 0o077)):
            raise CacheError("Capture cache directory is not private controller-owned state.")

    def _key(self, url):
        if not isinstance(url, str) or not 0 < len(url) <= 8192:
            raise CacheError("Capture cache source identity is invalid.")
        try:
            return hashlib.sha256(url.encode("utf-8")).hexdigest()
        except UnicodeError:
            raise CacheError("Capture cache source identity is invalid.") from None

    @contextmanager
    def _locked(self):
        descriptor = None
        locked = False
        deadline = time.monotonic() + LOCK_TIMEOUT
        try:
            self._check_directory()
            path = self.directory / ".cache.lock"
            if path.exists() or path.is_symlink():
                _tighten_mode(path)
                try:
                    _private_file(path)
                except CacheError:
                    # A linked or foreign lock name: replace it rather than
                    # disabling the cache for every job.
                    self._move_aside(path, "invalid lock")
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0), 0o600)
            os.set_inheritable(descriptor, False)
            _private_file(path, descriptor)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CacheError("Verified capture cache coordination timed out.", kind="busy")
                try:
                    if os.name == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    time.sleep(min(0.05, remaining))
            self._check_directory()
            _private_file(path, descriptor)
            yield
        except OSError:
            raise CacheError("Cannot access the verified capture cache.") from None
        finally:
            if descriptor is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _record(self, url, value):
        try:
            if (not isinstance(value, dict) or set(value) != RECORD_FIELDS
                    or any(not isinstance(item, str) for item in value.values())
                    or value["url"] != url
                    or any(len(value[key]) > limit for key, limit in {
                        "url": 8192, "capture": 14, "archive_url": 16384,
                        "mime_type": 128, "cdx_digest": 32, "memento_datetime": 256,
                        "body_sha256": 64, "content_encoding": 128,
                        "body": ((MAX_BODY_BYTES + 2) // 3) * 4}.items())):
                raise ValueError
            body = base64.b64decode(value["body"], validate=True)
            if (not 0 < len(body) <= MAX_BODY_BYTES
                    or base64.b64encode(body).decode("ascii") != value["body"]
                    or hashlib.sha256(body).hexdigest() != value["body_sha256"]):
                raise ValueError
            return dict(value)
        except (UnicodeError, TypeError, ValueError):
            raise CacheError("Verified capture cache record is invalid.", kind="invalid") from None

    def _read(self, url, key):
        path = self.directory / (key + ".json")
        try:
            _tighten_mode(path)
            info = _private_file(path)
        except FileNotFoundError:
            return None
        if info.st_size > MAX_JSON_BYTES:
            raise CacheError("Verified capture cache entry exceeds its size limit.", kind="invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        try:
            _private_file(path, descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_JSON_BYTES + 1)
        finally:
            os.close(descriptor)
        try:
            if len(data) > MAX_JSON_BYTES:
                raise ValueError
            envelope = json.loads(data, object_pairs_hook=_unique_object)
            if (not isinstance(envelope, dict)
                    or set(envelope) != {"schema_version", "url_sha256", "record"}
                    or type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1
                    or envelope["url_sha256"] != key):
                raise ValueError
            return self._record(url, envelope["record"])
        except (UnicodeError, TypeError, ValueError, RecursionError):
            raise CacheError("Verified capture cache entry is invalid.", kind="invalid") from None

    def get(self, url):
        """Return a structurally intact record, or None only for an absent URL."""
        key = self._key(url)
        with self._locked():
            return self._read(url, key)

    def _move_aside(self, path, reason):
        """Rename one invalid name into the private quarantine folder without following it.

        Moved names are kept for inspection (the 64 latest) and never read again.
        """
        folder = self.directory / QUARANTINE
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise CacheError("Verified capture cache quarantine is not a private folder.")
        if os.name != "nt" and info.st_mode & 0o077:
            os.chmod(folder, 0o700)
        label = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:64]
        os.rename(path, folder / f"{time.time_ns()}-{os.urandom(4).hex()}-{label}")
        kept = sorted(folder.iterdir(), key=lambda item: item.name)
        for old in kept[:-MAX_ENTRIES]:
            if old.is_symlink() or not old.is_dir():
                old.unlink(missing_ok=True)
        if self.on_quarantine is not None:
            try:
                self.on_quarantine(reason)
            except Exception:
                pass  # The moved file itself remains the record.

    def quarantine(self, url, reason="invalid entry"):
        """Move a URL's entry aside after it failed validation; True if one was moved."""
        path = self.directory / (self._key(url) + ".json")
        with self._locked():
            if not os.path.lexists(path):
                return False
            try:
                self._move_aside(path, reason)
            except OSError:
                raise CacheError("Verified capture cache entry could not be moved aside.") from None
            return True

    def _entries(self):
        entries = []
        for path in self.directory.iterdir():
            if path.name in (".cache.lock", QUARANTINE):
                continue
            try:
                if ENTRY_NAME.fullmatch(path.name):
                    _tighten_mode(path)
                    info = _private_file(path)
                    entries.append((info.st_mtime_ns, path.name, path))
                    continue
                if TEMP_NAME.fullmatch(path.name):
                    # With the global lock held, these can only be abandoned writes.
                    _private_file(path)
                    path.unlink()
                    continue
            except CacheError:
                pass
            # A Finder .DS_Store, a linked or loosened entry: one stray name
            # must not disable the shared cache for every job.
            self._move_aside(path, "unexpected entry")
        return sorted(entries)

    def put(self, url, record):
        """Atomically replace a verified record, retaining the 64 latest writes."""
        key = self._key(url)
        record = self._record(url, record)
        try:
            data = json.dumps({"schema_version": 1, "url_sha256": key, "record": record},
                              sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (UnicodeError, TypeError, ValueError):
            raise CacheError("Verified capture cache record cannot be serialized.") from None
        if len(data) > MAX_JSON_BYTES:
            raise CacheError("Verified capture cache entry exceeds its size limit.")
        with self._locked():
            try:
                self._read(url, key)
            except CacheError as error:
                if error.kind != "invalid":
                    raise
                # Never overwrite corrupt or linked state in place: keep it aside.
                self._move_aside(self.directory / (key + ".json"), "invalid entry")
            self._entries()  # Validate the directory and clear interrupted writes.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("wb", delete=False, dir=self.directory,
                                                 prefix=".capture-", suffix=".tmp") as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.directory / (key + ".json"))
                for _, _, path in self._entries()[:-MAX_ENTRIES]:
                    path.unlink()
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
