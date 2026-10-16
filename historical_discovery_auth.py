"""Preserve only refreshed discovery authentication with a bounded file CAS.

The caller supplies the digest of the exact master bytes copied into the child.
Concurrent helpers share a stable sidecar lock; a changed master is never
replaced by an older child's credentials. Invalid state returns False without
logging credentials or copying any other child files.
"""
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

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


MAX_AUTH_BYTES = 1024 * 1024
LOCK_WAIT_SECONDS = 2.0


def _identity(info):
    return info.st_dev, info.st_ino


def _owned(info):
    return (not hasattr(os, 'getuid') or info.st_uid == os.getuid()) and (
        os.name == 'nt' or not info.st_mode & 0o022)


def _unlinked(info):
    return not (getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _directory(path):
    if not path.is_absolute() or path.absolute() != path.resolve():
        raise ValueError('Invalid protected directory.')
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or not _owned(info) or not _unlinked(info):
        raise ValueError('Invalid protected directory.')
    return _identity(info)


def _file(path, descriptor):
    info, visible = os.fstat(descriptor), path.lstat()
    if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(visible.st_mode)
            or info.st_nlink != 1 or not _owned(info) or not _unlinked(visible)
            or _identity(info) != _identity(visible)):
        raise ValueError('Invalid protected file.')
    return info


def _auth_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate authentication field.')
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError('Invalid authentication number.')

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('Invalid authentication object.')
    tokens, api_key = value.get('tokens'), value.get('OPENAI_API_KEY')
    valid_tokens = (isinstance(tokens, dict) and any(
        isinstance(tokens.get(key), str) and tokens[key].strip()
        for key in ('access_token', 'refresh_token', 'id_token')))
    if not valid_tokens and not (isinstance(api_key, str) and api_key.strip()):
        raise ValueError('Authentication credentials are missing.')


def _read_auth(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = _file(path, descriptor)
        if before.st_size > MAX_AUTH_BYTES:
            raise ValueError('Authentication file is too large.')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_AUTH_BYTES + 1)
        after = _file(path, descriptor)
        if (len(raw) > MAX_AUTH_BYTES or len(raw) != after.st_size
                or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise ValueError('Authentication changed while reading.')
        _auth_json(raw)
        return raw, _identity(after)
    finally:
        os.close(descriptor)


@contextmanager
def _locked(parent):
    path = parent / '.auth-refresh.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0), 0o600)
    locked = False
    try:
        os.set_inheritable(descriptor, False)
        info = _file(path, descriptor)
        if info.st_size == 0:
            os.write(descriptor, b'\0')
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                if os.name == 'nt':
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                _file(path, descriptor)
                break
            except OSError as exc:
                if (exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
                        or time.monotonic() >= deadline):
                    raise
                time.sleep(0.01)
        yield
    finally:
        try:
            if locked:
                if os.name == 'nt':
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def preserve_refresh(master_auth_path: Path, child_auth_path: Path, original_sha256: str) -> bool:
    """Atomically retain a valid refresh if the protected master is unchanged.

    Both paths must be absolute, unlinked, controller-owned ``auth.json`` files.
    Returns False for unchanged credentials, stale children, lock contention or
    invalid state. No credential values appear in exceptions or diagnostics.
    """
    temporary = None
    try:
        if not isinstance(original_sha256, str) or not re.fullmatch('[0-9a-f]{64}', original_sha256):
            return False
        master, child = Path(master_auth_path), Path(child_auth_path)
        if master == child or any(path.name != 'auth.json' or not path.is_absolute()
                or path.absolute() != path.resolve() for path in (master, child)):
            return False
        parent_id, child_parent_id = _directory(master.parent), _directory(child.parent)
        child_raw, child_id = _read_auth(child)
        if hashlib.sha256(child_raw).hexdigest() == original_sha256:
            return False
        with _locked(master.parent):
            if _directory(master.parent) != parent_id or _directory(child.parent) != child_parent_id:
                return False
            master_raw, master_id = _read_auth(master)
            if hashlib.sha256(master_raw).hexdigest() != original_sha256:
                return False
            descriptor, name = tempfile.mkstemp(prefix='.auth-refresh-', suffix='.tmp', dir=master.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, 'wb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(child_raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Recheck after staging so neither a concurrent refresh nor a path
            # replacement can be silently overwritten during preparation.
            if _directory(master.parent) != parent_id or _directory(child.parent) != child_parent_id:
                return False
            current_raw, current_id = _read_auth(master)
            latest_child, latest_child_id = _read_auth(child)
            if (current_id != master_id or current_raw != master_raw
                    or latest_child_id != child_id or latest_child != child_raw):
                return False
            os.replace(temporary, master)
            temporary = None
            return True
    except (OSError, ValueError, TypeError, RuntimeError, UnicodeError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
