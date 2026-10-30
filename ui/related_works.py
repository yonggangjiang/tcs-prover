"""Related works: papers, drafts and notes that the user gives the proof author.

An upload arrives as raw bytes, one file per request, in a private staging
area (runs/.uploads); a PDF's text is extracted there in a child process.
Installing staged items puts them in related-works/ of the author workspace,
beside a controller-written INDEX.md, and records them in a manifest in the
controller folder, outside the workspace. Related works are only ever added:
each new name is claimed with link(2) or an exclusive create, so an installed
file is never overwritten, and a failed install removes what it placed.

A folder batch keeps one physical copy of its related works in its own store
(BatchStore, under the controller folder), hard-linked into the related-works/
folder of every job of the batch; each job's INDEX.md lists the batch's items
and the job's own. Every job's link reaches that one copy, so a store file
whose bytes no longer match what was added is not given to later jobs.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
import errno
from functools import partial
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]

FOLDER = "related-works"
INDEX_NAME = "INDEX.md"
MANIFEST_NAME = "related-works.json"
STAGING_FOLDER = ".uploads"
# Folder batches keep their stores in <controller folder>/.batches/<batch ID>.
BATCH_FOLDER = ".batches"

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ITEMS = 200
MAX_NOTE_CHARS = 2_000_000
MAX_TITLE_CHARS = 200
MAX_ORIGINAL_NAME_CHARS = 255
MAX_SAVED_NAME_CHARS = 120
MAX_EXTENSION_CHARS = 16
STAGED_KEEP_SECONDS = 24 * 3600
PDF_TIMEOUT_SECONDS = 120
PDF_MAX_PAGES = 2000
PDF_MAX_CHARS = 5_000_000
PDF_STREAM_WORK_BYTES = 256 * 1024 * 1024
# Not in the specification (to confirm with the owner): pypdf keeps every decoded form on its
# object, so a small hostile PDF can take many gigabytes. The child's peak memory is capped, and
# only a few children run at once; an upload over the cap is refused with _memory_problem().
PDF_MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
PDF_MAX_PARALLEL = 2
# The extraction child's exit status when its memory watcher ends it.
PDF_MEMORY_EXIT = 3
NOTICE_NAMES_MAX_CHARS = 10_000
BLOCK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
# A batch's files are hard links shared by all its jobs: read-only, so one author does not change
# them in place for the others by accident. The owner of a file can undo this with chmod, so every
# later link checks the file's bytes again (_unchanged).
BATCH_FILE_MODE = 0o400
MAX_NAME_ATTEMPTS = 10_000
MAX_CHECKED_FILES = 100_000

RELATED_WORKS_NOTICE = (
    "RELATED WORKS ADDED:\n"
    "The user added {names} to the related-works folder of your working directory; related-works/INDEX.md lists "
    "every related work. Read what is relevant."
)

PDF_MEDIA_TYPE = "application/pdf"
KINDS = ("file", "note")
CONTENT_NAME, TEXT_NAME, ITEM_NAME = "content", "text", "item.json"
TOKEN = re.compile(r"[A-Za-z0-9_-]{24}\Z")
BATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SAVED_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RESERVED_STEMS = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                            *(f"LPT{n}" for n in range(1, 10))})
LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"

EXPIRED = "This upload has expired; add the file again."
CUT_SHORT = "The upload ended before the whole file arrived; add the file again."
NOT_PLAIN = "The related-works folder of this run is not a plain folder; related works cannot be added."
BATCH_NOT_PLAIN = "The related-works folder of this batch is not a plain folder; related works cannot be added."
DAMAGED = "The related-works record of this job is damaged; related works cannot be added."
FAILED_PREFIX = "Could not add the related works: "
PROBLEM_UNAVAILABLE = "Text extraction is unavailable: it needs pypdf>=6.20.1,<7."
PROBLEM_ENCRYPTED = "The PDF is encrypted."
PROBLEM_NO_TEXT = "The PDF has no text layer (no OCR)."
PROBLEM_FAILED = "Text extraction failed: the PDF could not be read."

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Opening a FIFO an author planted must not wait for a writer; fstat then refuses it.
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
BINARY = getattr(os, "O_BINARY", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# POSIX pins each folder once by its descriptor and works relative to it; elsewhere
# (Windows) a folder is lstat-checked by path before every step.
DIR_FD = bool(NOFOLLOW and DIRECTORY) and os.link in os.supports_follow_symlinks and all(
    function in os.supports_dir_fd
    for function in (os.open, os.link, os.rename, os.unlink, os.mkdir, os.rmdir, os.stat))
# The file system cannot hard-link here (another device, no hard links, too many links): copy instead.
LINK_UNSUPPORTED = frozenset(code for code in (
    errno.EXDEV, errno.EPERM, errno.EMLINK, getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None),
    getattr(errno, "ENOSYS", None)) if code is not None)
# A Windows reparse point whose tag has this bit names another file (a symlink or junction).
NAME_SURROGATE_TAG = 0x20000000


class RelatedWorksError(ValueError):
    """A refusal shown to the user; ``status`` is the HTTP status (413 for a file over the size limit, else 400)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _Changed(Exception):
    """A source file cannot be read, or no longer matches what was recorded for it."""


def _pages_problem():
    return f"The PDF has more than {PDF_MAX_PAGES:,} pages."


def _chars_problem():
    return f"The PDF text is longer than {PDF_MAX_CHARS:,} characters."


def _timeout_problem():
    return f"Text extraction took longer than {_duration(PDF_TIMEOUT_SECONDS)}."


def _memory_problem():
    return f"Text extraction needed more than {PDF_MAX_MEMORY_BYTES / 1024 ** 3:g} GB of memory."


def _duration(seconds):
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} second{'s' if seconds != 1 else ''}"


def size_text(size):
    """A byte count as "{n} B", "{n} KB" or "{n} MB" (binary units, one decimal)."""

    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1048576:.1f} MB"


def _clause(problem):
    """A problem sentence as a clause: first letter lower-cased, without the final period."""

    problem = problem[:1].lower() + problem[1:]
    return problem[:-1] if problem.endswith(".") else problem


def _pages_text(pages):
    return f"{pages} page{'s' if pages != 1 else ''}"


def text_status(item):
    """What the author gets besides a staged item or installed entry: "" unless it is a PDF."""

    if item.get("kind") != "file" or item.get("mediaType") != PDF_MEDIA_TYPE:
        return ""
    if isinstance(item.get("text"), dict):
        pages = item["text"].get("pages")
    else:
        pages = item.get("pages") if item.get("textName") else 0
    if pages:
        return f"text extracted ({_pages_text(pages)})"
    return "no extracted text: " + _clause(item.get("textProblem") or PROBLEM_FAILED)


def _well_formed(text):
    """Lone surrogates (possible in JSON text) cannot be saved as UTF-8; they become U+FFFD."""

    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _one_line(text):
    return re.sub(r"[\x00-\x1f\x7f\x85\u2028\u2029]", " ", text)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _count(value, minimum=0):
    return type(value) is int and value >= minimum


def _sha256(value):
    return isinstance(value, str) and bool(SHA256.match(value))


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# --- Names -------------------------------------------------------------------------------------------------------


def _split_extension(name):
    """(stem, extension): the extension is the last dotted suffix of at most 16 characters, dot included."""

    index = name.rfind(".")
    if index > 0 and len(name) - index <= MAX_EXTENSION_CHARS:
        return name[:index], name[index:]
    return name, ""


def _fit(name, suffix=""):
    """``name`` with ``suffix`` before its extension, shortened to MAX_SAVED_NAME_CHARS (the extension is kept).

    A shortened stem loses the "." and "-" the cut leaves at its end, as the whole name did before.
    """

    stem, extension = _split_extension(name)
    room = MAX_SAVED_NAME_CHARS - len(extension) - len(suffix)
    if len(stem) > room:
        stem = stem[:room].rstrip(".-") or stem[:1]
    return stem + suffix + extension


def _numbered(name, number):
    return name if number == 1 else _fit(name, f"-{number}")


def safe_name(raw):
    """The saved name for an uploaded file's name (the steps of the specification, §3.2, in order).

    At most 120 portable characters of [A-Za-z0-9._-]. The original name is
    kept separately for display. Uniqueness in a folder is decided at
    install, which numbers a taken name ("-2", "-3", ...).
    """

    base = re.split(r"[/\\]", raw)[-1] if isinstance(raw, str) else ""
    base = unicodedata.normalize("NFC", base)
    name = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9._-]", "-", base)).strip(".-")
    if name.split(".", 1)[0].upper() in RESERVED_STEMS or name.casefold() == INDEX_NAME.casefold():
        name = "file-" + name
    return _fit(name) or "file"


def _valid_name(value):
    return (isinstance(value, str) and 0 < len(value) <= MAX_SAVED_NAME_CHARS + len(".txt")
            and bool(SAVED_NAME.match(value)) and not value.endswith((".", "-"))
            and value.casefold() != INDEX_NAME.casefold())


# --- Folders -----------------------------------------------------------------------------------------------------


def _linked(info):
    """Whether lstat information describes a symlink, junction or other Windows link."""

    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        and getattr(info, "st_reparse_tag", 0) & NAME_SURROGATE_TAG)


def _identity(info):
    return info.st_dev, info.st_ino


def _make_folders(path):
    """Create ``path`` and its missing parents, each owner-only."""

    missing, current = [], Path(path)
    while not os.path.lexists(current) and current != current.parent:
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass


class _Folder:
    """One folder, pinned by its descriptor (POSIX), or by its path and identity checked before every step (Windows).

    Every operation names a single entry of the folder, so no path is ever
    looked up through a link the author planted.
    """

    def __init__(self, path, descriptor, identity, follow=False):
        self.path, self.fd, self.identity, self.follow = Path(path), descriptor, identity, follow
        self.created = self.shared = False

    @classmethod
    def at(cls, path, *, create=False):
        """The folder at a caller-resolved ``path`` (links in it are followed); ``create`` makes it, owner-only."""

        if create:
            _make_folders(path)
        if DIR_FD:
            descriptor = os.open(path, os.O_RDONLY | DIRECTORY | CLOEXEC)
            return cls(path, descriptor, _identity(os.fstat(descriptor)))
        info = os.stat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Not a folder", str(path))
        return cls(path, None, _identity(info), follow=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _target(self, name):
        info = os.stat(self.path) if self.follow else os.lstat(self.path)
        if (not self.follow and _linked(info)) or not stat.S_ISDIR(info.st_mode) or _identity(info) != self.identity:
            raise OSError(errno.ENOTDIR, "The folder changed while related works were being saved", str(self.path))
        return str(self.path / name)

    def child(self, name, *, create=False, problem=None):
        """The plain subfolder ``name``, never a link (else ``problem``); ``create`` makes it owner-only."""

        created = False
        try:
            info = self.lstat(name)
        except FileNotFoundError:
            if not create:
                raise
            try:
                self.mkdir(name)
                created = True
            except FileExistsError:
                pass
            info = self.lstat(name)
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            if problem:
                raise RelatedWorksError(problem)
            raise NotADirectoryError(errno.ENOTDIR, "Not a plain folder", str(self.path / name))
        if self.fd is None:
            folder = _Folder(self.path / name, None, _identity(info))
        else:
            try:
                descriptor = os.open(name, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=self.fd)
            except OSError as exc:
                if problem and exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RelatedWorksError(problem) from None
                raise
            folder = _Folder(self.path / name, descriptor, _identity(info))
            if _identity(os.fstat(descriptor)) != folder.identity:
                folder.close()
                if problem:
                    raise RelatedWorksError(problem)
                raise NotADirectoryError(errno.ENOTDIR, "The folder changed while it was opened", str(folder.path))
        folder.created = created
        if create and os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700:
            try:
                if folder.fd is not None:
                    os.fchmod(folder.fd, 0o700)
                else:
                    os.chmod(folder._target(""), 0o700)
            except BaseException:
                folder.close()
                raise
        return folder

    def names(self):
        return os.listdir(self.fd if self.fd is not None else self._target(""))

    def device(self):
        return (os.fstat(self.fd) if self.fd is not None else os.stat(self._target(""))).st_dev

    def lstat(self, name):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return os.lstat(self._target(name))

    def mkdir(self, name):
        if self.fd is not None:
            os.mkdir(name, 0o700, dir_fd=self.fd)
        else:
            os.mkdir(self._target(name), 0o700)

    def open_read(self, name):
        """Open the entry ``name`` for reading: never through a link, and never waiting on a FIFO or device."""

        if self.fd is not None:
            return os.open(name, os.O_RDONLY | NOFOLLOW | NONBLOCK | BINARY | CLOEXEC, dir_fd=self.fd)
        target = self._target(name)
        if _linked(os.lstat(target)):
            raise OSError(errno.ELOOP, "Refusing to follow a link", target)
        return os.open(target, os.O_RDONLY | NONBLOCK | BINARY)

    def create(self, name):
        """A new owner-only file opened for writing; FileExistsError when the name is taken (it is never replaced)."""

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | BINARY | CLOEXEC
        if self.fd is not None:
            return os.open(name, flags, 0o600, dir_fd=self.fd)
        return os.open(self._target(name), flags, 0o600)

    def link(self, source, source_name, name):
        """Hard-link source/source_name as ``name``; FileExistsError when the name is taken (it is never replaced)."""

        if self.fd is not None and source.fd is not None:
            os.link(source_name, name, src_dir_fd=source.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            return
        origin = source._target(source_name)
        if _linked(os.lstat(origin)):
            raise OSError(errno.ELOOP, "Refusing to link a link", origin)
        os.link(origin, self._target(name))

    def replace(self, source, source_name, name):
        """Atomically put source/source_name in place of ``name``: the one operation that replaces a name."""

        if self.fd is not None and source.fd is not None:
            os.rename(source_name, name, src_dir_fd=source.fd, dst_dir_fd=self.fd)
        else:
            os.replace(source._target(source_name), self._target(name))

    def unlink(self, name):
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            os.unlink(self._target(name))

    def rmdir(self, name):
        if self.fd is not None:
            os.rmdir(name, dir_fd=self.fd)
        else:
            os.rmdir(self._target(name))

    def remove(self, name):
        """Remove the entry ``name``: a plain folder with everything in it, or anything else by itself."""

        info = self.lstat(name)
        if stat.S_ISDIR(info.st_mode) and not _linked(info):
            if self.fd is not None:
                shutil.rmtree(name, dir_fd=self.fd)
            else:
                shutil.rmtree(self._target(name))
        else:
            self.unlink(name)

    def sync(self):
        if self.fd is not None:
            try:
                os.fsync(self.fd)
            except OSError:
                pass


def _open_path(path, *, create=False, problem=None):
    """The folder at ``path``, whose last component must be a plain folder (its parents may be links)."""

    path = Path(path)
    with _Folder.at(path.parent, create=create) as parent:
        return parent.child(path.name, create=create, problem=problem)


def _unlink_if(folder, name, identity):
    """Remove ``name`` if it is still the file this call placed there."""

    try:
        if identity is None or _identity(folder.lstat(name)) == identity:
            folder.unlink(name)
    except OSError:
        pass


def _write_all(descriptor, data):
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def _replace_file(folder, name, data, staging):
    """Atomically replace folder/name with ``data``, written first in ``staging``, outside the author's workspace.

    The new bytes are complete and on disk before they take the name. A
    staging folder on another device falls back to a temporary file beside it.
    """

    for place in ((staging, folder) if staging is not folder else (folder,)):
        temporary, moved = f".tmp-{secrets.token_hex(8)}", False
        descriptor = place.create(temporary)
        try:
            try:
                _write_all(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                folder.replace(place, temporary, name)
            except OSError as exc:
                if exc.errno == errno.EXDEV and place is not folder:
                    continue
                raise
            moved = True
            folder.sync()
            return
        finally:
            if not moved:
                try:
                    place.unlink(temporary)
                except OSError:
                    pass


_locks_guard = threading.Lock()
_locks = {}


@contextmanager
def workspace_lock(path):
    """Serialize the related-works changes of one workspace (or batch store) in this process; re-entrant.

    Every function here that writes takes it for the folder it writes, and
    only that one, so a caller may hold it around a check and the install
    that follows without any risk of deadlock. An install also takes the
    staging area's lock while it places each upload, always after the
    workspace's; callers never need to take that one.
    """

    key = os.path.normcase(os.path.realpath(path))
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        yield


# --- Staged uploads ----------------------------------------------------------------------------------------------


class StagedItem:
    """One staged upload: its token, its folder (runs/.uploads/<token>) and its record (item.json)."""

    def __init__(self, token, item, path):
        self.token, self.item, self.path = token, item, Path(path)

    def public(self):
        return _public(self.item)


def _public(item):
    return {"token": item["token"], "kind": item["kind"], "name": item["name"], "originalName": item["originalName"],
            "title": item["title"], "size": item["size"], "sizeText": size_text(item["size"]),
            "sha256": item["sha256"], "mediaType": item["mediaType"],
            "text": dict(item["text"]) if item["text"] else None, "textProblem": item["textProblem"],
            "textStatus": text_status(item)}


def _valid_item(item, token):
    text = item.get("text") if isinstance(item, dict) else None
    return (isinstance(item, dict) and item.get("schemaVersion") == 1 and item.get("token") == token
            and item.get("kind") in KINDS and _valid_name(item.get("name"))
            and all(isinstance(item.get(key), str) for key in ("originalName", "title", "mediaType", "textProblem"))
            and _count(item.get("size"), 1) and _sha256(item.get("sha256"))
            and (text is None or (isinstance(text, dict) and _count(text.get("pages"), 1)
                                  and _count(text.get("chars"), 1) and _count(item.get("textSize"), 1)
                                  and _sha256(item.get("textSha256")))))


def _extraction_command(content, text):
    return [sys.executable, "-m", "ui.related_works", "--extract-pdf", str(content), str(text)]


_extraction_slots = threading.BoundedSemaphore(PDF_MAX_PARALLEL)


def _run_extraction(content, text):
    """Extract a PDF's text in a child process, killed after PDF_TIMEOUT_SECONDS; return its report.

    At most PDF_MAX_PARALLEL children run at once (each may use up to
    PDF_MAX_MEMORY_BYTES); further uploads wait for a free slot.
    """

    try:
        with _extraction_slots:
            completed = subprocess.run(_extraction_command(content, text), cwd=ROOT, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       timeout=PDF_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"problem": _timeout_problem()}
    except OSError:
        return {"problem": PROBLEM_FAILED}
    if completed.returncode == PDF_MEMORY_EXIT:
        return {"problem": _memory_problem()}
    lines = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        report = json.loads(lines[-1]) if lines and completed.returncode == 0 else None
    except ValueError:
        report = None
    if (isinstance(report, dict) and set(report) == {"problem"} and isinstance(report["problem"], str)
            and 0 < len(report["problem"]) <= 500 and _one_line(report["problem"]) == report["problem"]):
        return report
    if isinstance(report, dict) and set(report) == {"pages", "chars"} and _count(report["pages"], 1) \
            and _count(report["chars"], 1):
        return report
    return {"problem": PROBLEM_FAILED}


class UploadStage:
    """Uploads waiting to be attached to a job, each in runs/.uploads/<token>: owner-only, kept 24 hours.

    A token folder holds ``content`` (the bytes), ``text`` (a PDF's extracted
    text) and ``item.json`` (the record). An install hard-links the files out
    of it, then discards it.
    """

    def __init__(self, root):
        self.root = Path(os.path.abspath(root))

    def _folder(self, create=False):
        return _open_path(self.root, create=create)

    def receive(self, original_name, stream, length):
        """Stage one file of ``length`` bytes read from ``stream``; return its public description.

        Every check comes before anything is written. The bytes are read in
        1 MiB blocks; a stream that ends early leaves nothing behind.
        """

        original = original_name if isinstance(original_name, str) else ""
        if not original.strip():
            raise RelatedWorksError("Give the file a name.")
        if len(original) > MAX_ORIGINAL_NAME_CHARS:
            raise RelatedWorksError(f"The file name is longer than {MAX_ORIGINAL_NAME_CHARS} characters.")
        if not _count(length):
            raise RelatedWorksError("The upload must give the size of the file.")
        if length == 0:
            raise RelatedWorksError("The file is empty.")
        if length > MAX_FILE_BYTES:
            raise RelatedWorksError(f"This file is {length / 1048576:.1f} MB; the limit is "
                                    f"{MAX_FILE_BYTES // 1048576} MB per file.", 413)
        original = _well_formed(original)

        def write(folder):
            digest, head, remaining = hashlib.sha256(), b"", length
            descriptor = folder.create(CONTENT_NAME)
            try:
                while remaining:
                    try:
                        block = stream.read(min(BLOCK_BYTES, remaining))
                    except (OSError, ValueError) as exc:
                        raise RelatedWorksError(CUT_SHORT) from exc
                    if not block:
                        raise RelatedWorksError(CUT_SHORT)
                    block = bytes(block[:remaining])
                    remaining -= len(block)
                    digest.update(block)
                    if len(head) < 1024:
                        head = (head + block)[:1024]
                    _write_all(descriptor, block)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            pdf = b"%PDF-" in head
            text, problem, text_size, text_sha256 = self._extract(folder) if pdf else (None, "", 0, "")
            return {"kind": "file", "name": safe_name(original), "originalName": original, "title": "",
                    "size": length, "sha256": digest.hexdigest(), "mediaType": PDF_MEDIA_TYPE if pdf else "",
                    "text": text, "textProblem": problem, "textSize": text_size, "textSha256": text_sha256}

        return self._stage(write)

    def note(self, title, text):
        """Stage a note; its file is "# {title}\\n\\n{text}\\n", or "{text}\\n" without a title.

        The text is saved as given, indentation included, less its trailing
        newlines; the character limit applies to the text as given.
        """

        if not isinstance(text, str) or not text.strip():
            raise RelatedWorksError("Enter the note's text.")
        title = "" if title is None else title
        if not isinstance(title, str):
            raise RelatedWorksError(f"Keep the note title to one line of at most {MAX_TITLE_CHARS} characters.")
        if "\0" in text or "\0" in title:
            raise RelatedWorksError("The note cannot contain NUL characters.")
        if len(text) > MAX_NOTE_CHARS:
            raise RelatedWorksError(f"Keep a note at or below {MAX_NOTE_CHARS:,} characters.")
        text, title = _well_formed(text.rstrip("\n")), _well_formed(title.strip())
        if len(title) > MAX_TITLE_CHARS or any(mark in title for mark in LINE_BREAKS):
            raise RelatedWorksError(f"Keep the note title to one line of at most {MAX_TITLE_CHARS} characters.")
        content = (f"# {title}\n\n{text}\n" if title else f"{text}\n").encode("utf-8")
        name = safe_name(re.sub(r"[/\\]", "-", title) + ".md") if title else "note.md"

        def write(folder):
            descriptor = folder.create(CONTENT_NAME)
            try:
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return {"kind": "note", "name": name, "originalName": "", "title": title, "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(), "mediaType": "", "text": None, "textProblem": "",
                    "textSize": 0, "textSha256": ""}

        return self._stage(write)

    def _stage(self, write):
        """Make a token folder, let ``write`` fill it, then record the item; a failure leaves nothing behind."""

        try:
            root = self._folder(create=True)
        except (OSError, RelatedWorksError) as exc:
            raise RelatedWorksError(f"Could not save the upload: {exc}") from exc
        with root:
            token = secrets.token_urlsafe(18)
            try:
                root.mkdir(token)
            except OSError as exc:
                raise RelatedWorksError(f"Could not save the upload: {exc}") from exc
            try:
                with root.child(token) as folder:
                    item = {"schemaVersion": 1, "token": token, **write(folder), "createdAt": time.time()}
                    descriptor = folder.create(ITEM_NAME)
                    try:
                        _write_all(descriptor, _json_bytes(item))
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            except BaseException as exc:
                try:
                    root.remove(token)
                except OSError:
                    pass
                if isinstance(exc, OSError):
                    raise RelatedWorksError(f"Could not save the upload: {exc}") from exc
                raise
        return _public(item)

    def _extract(self, folder):
        """(text, problem, text size, text sha256) for the PDF just written in ``folder``; the PDF is always kept."""

        report = _run_extraction(folder.path / CONTENT_NAME, folder.path / TEXT_NAME)
        if "problem" not in report:
            try:
                descriptor = folder.open_read(TEXT_NAME)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4 * PDF_MAX_CHARS:
                        raise OSError(errno.EINVAL, "Not a text copy")
                    digest = hashlib.sha256()
                    while block := os.read(descriptor, BLOCK_BYTES):
                        digest.update(block)
                finally:
                    os.close(descriptor)
                return {"pages": report["pages"], "chars": report["chars"]}, "", info.st_size, digest.hexdigest()
            except OSError:
                report = {"problem": PROBLEM_FAILED}
        try:
            folder.unlink(TEXT_NAME)
        except OSError:
            pass
        return None, report["problem"], 0, ""

    def get(self, token):
        """The staged item of ``token``; an unknown, malformed or incomplete token raises the expiry message."""

        if not isinstance(token, str) or not TOKEN.match(token):
            raise RelatedWorksError(EXPIRED)
        try:
            with self._folder() as root, root.child(token) as folder:
                descriptor = folder.open_read(ITEM_NAME)
                with os.fdopen(descriptor, "rb") as stream:
                    item = json.loads(stream.read(1024 * 1024).decode("utf-8"))
                if not _valid_item(item, token):
                    raise ValueError("Damaged upload record.")
                content = folder.lstat(CONTENT_NAME)
                if not stat.S_ISREG(content.st_mode) or content.st_size != item["size"]:
                    raise ValueError("Damaged upload.")
                if item["text"]:
                    text = folder.lstat(TEXT_NAME)
                    if not stat.S_ISREG(text.st_mode) or text.st_size != item["textSize"]:
                        raise ValueError("Damaged text copy.")
        except (OSError, ValueError, RecursionError):
            raise RelatedWorksError(EXPIRED) from None
        return StagedItem(token, item, folder.path)

    def peek(self, tokens):
        """Check that every token names a staged upload (else the expiry message); return their items."""

        return [self.get(token) for token in _token_list(tokens)]

    def discard(self, token):
        """Remove one staged upload; whether there was one."""

        if not isinstance(token, str) or not TOKEN.match(token):
            return False
        try:
            with self._folder() as root:
                root.remove(token)
            return True
        except OSError:
            return False

    @staticmethod
    def prune_leftovers(folder, now=None):
        """Remove the `.tmp-*` files a crashed copy or INDEX.md write left directly in ``folder``; return how many.

        Batch links and copies stage beside their target (a job's controller
        folder or a batch store), which prune() does not sweep. Only regular
        files older than STAGED_KEEP_SECONDS go; a link is never followed.
        """

        now = time.time() if now is None else now
        removed = 0
        try:
            names = os.listdir(folder)
        except OSError:
            return 0
        for name in names:
            if not name.startswith(".tmp-"):
                continue
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode) and now - info.st_mtime > STAGED_KEEP_SECONDS:
                    os.unlink(path)
                    removed += 1
            except OSError:
                continue
        return removed

    def prune(self, now=None):
        """Remove staged uploads, and leftover temporary files, older than STAGED_KEEP_SECONDS; return how many.

        An upload's age is that of its item.json; a folder still being written
        has none and goes by its own time.
        """

        now = time.time() if now is None else now
        removed = 0
        try:
            root = self._folder()
        except OSError:
            return 0
        with root:
            for name in root.names():
                if not (TOKEN.match(name) or name.startswith(".tmp-")):
                    continue
                try:
                    info = root.lstat(name)
                    if stat.S_ISDIR(info.st_mode) and not _linked(info):
                        with root.child(name) as folder:
                            try:
                                info = folder.lstat(ITEM_NAME)
                            except FileNotFoundError:
                                pass
                    if now - info.st_mtime > STAGED_KEEP_SECONDS:
                        root.remove(name)
                        removed += 1
                except OSError:
                    continue
        return removed


def _token_list(tokens):
    if not isinstance(tokens, (list, tuple)) or not all(isinstance(token, str) for token in tokens):
        raise RelatedWorksError("Send the uploads as a list of upload tokens.")
    if len(set(tokens)) != len(tokens):
        raise RelatedWorksError("Each upload can be added only once.")
    return list(tokens)


# --- The manifest, INDEX.md and installs -------------------------------------------------------------------------


ENTRY_KEYS = ("name", "originalName", "kind", "title", "size", "sha256", "mediaType", "textName", "pages", "chars",
              "textProblem", "textSize", "textSha256", "addedAt", "addedIn")
TEXT_KEYS = ("originalName", "title", "mediaType", "textProblem", "textSha256")


def _valid_entry(entry):
    return (isinstance(entry, dict) and _valid_name(entry.get("name")) and entry.get("kind") in KINDS
            and _count(entry.get("size")) and _sha256(entry.get("sha256"))
            and all(isinstance(entry.get(key, ""), str)
                    for key in (*TEXT_KEYS, "addedAt", "addedIn", "batch", "batchName"))
            and all(_count(entry.get(key, 0)) for key in ("pages", "chars", "textSize"))
            and (entry.get("textName", "") == ""
                 or (_valid_name(entry["textName"]) and _sha256(entry.get("textSha256")))))


def _read_manifest(manifest_dir, strict=True):
    """(raw bytes or None, entries) of the manifest in ``manifest_dir``; a missing one is empty.

    A damaged manifest raises RelatedWorksError when ``strict`` (before any
    change); otherwise its valid entries are returned (for display).
    """

    try:
        with _open_path(manifest_dir) as folder:
            descriptor = folder.open_read(MANIFEST_NAME)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
                    raise ValueError("Not a manifest.")
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
        value = json.loads(raw.decode("utf-8"))
        items = value.get("items") if isinstance(value, dict) and value.get("schemaVersion") == 1 else None
        if not isinstance(items, list):
            raise ValueError("Not a manifest.")
        entries = [entry for entry in items if _valid_entry(entry)]
        if strict and len(entries) != len(items):
            raise ValueError("Damaged manifest entry.")
        return raw, entries
    except FileNotFoundError:
        return None, []
    except (OSError, ValueError, UnicodeError, RecursionError, RelatedWorksError) as exc:
        if strict:
            raise RelatedWorksError(DAMAGED) from exc
        return None, []


def _write_manifest(manifest_dir, entries):
    with _open_path(manifest_dir, create=True) as folder:
        _replace_file(folder, MANIFEST_NAME, _json_bytes({"schemaVersion": 1, "items": entries}), folder)


def _restore_manifest(manifest_dir, raw):
    with _open_path(manifest_dir, create=True) as folder:
        if raw is None:
            folder.unlink(MANIFEST_NAME)
        else:
            _replace_file(folder, MANIFEST_NAME, raw, folder)


def _index_line(number, entry):
    name, size = entry["name"], size_text(entry["size"])
    if entry["kind"] == "note":
        title = _one_line(entry.get("title") or "")
        detail = f'note "{title}", {size}.' if title else f"note, {size}."
    elif entry.get("mediaType") == PDF_MEDIA_TYPE:
        if entry.get("textName"):
            # The specification's text exactly (§6.7): "1 pages" too.
            detail = f"PDF, {size}, {entry.get('pages') or 0} pages; text in {entry['textName']}."
        else:
            detail = f"PDF, {size}; no extracted text ({_clause(entry.get('textProblem') or PROBLEM_FAILED)})."
    else:
        detail = f"{size}."
    original = entry.get("originalName") or ""
    if entry["kind"] == "file" and original and original != name:
        detail += f' Uploaded as "{_one_line(original)}".'
    return f"{number}. {name}: {detail}\n"


def index_text(entries):
    """The INDEX.md the author reads: one line per related work, oldest first."""

    return ("# Related works\n\nFiles the user supplied for this problem, oldest first.\n\n"
            + "".join(_index_line(number, entry) for number, entry in enumerate(entries, 1)))


def _write_index(folder, entries, staging):
    """Replace INDEX.md, a name only the controller writes: a folder planted there is moved aside, never deleted."""

    try:
        try:
            info = folder.lstat(INDEX_NAME)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode) and not _linked(info):
            folder.replace(folder, INDEX_NAME, f".{INDEX_NAME}.{secrets.token_hex(4)}.replaced")
        with _open_path(staging, create=True) as place:
            _replace_file(folder, INDEX_NAME, index_text(entries).encode("utf-8"), place)
    except OSError as exc:
        raise OSError(f"{INDEX_NAME} could not be written ({_clause(exc.strerror or str(exc))})") from exc


def _check_room(existing, new):
    if existing + new > MAX_ITEMS:
        raise RelatedWorksError(f"A job can hold at most {MAX_ITEMS} related works.")


def check_room(manifest_dir, extra):
    """Refuse, before anything is written, ``extra`` more items for a workspace or store that cannot hold them."""

    _check_room(len(_read_manifest(manifest_dir)[1]), extra)


def _template(entry):
    """The recorded facts of an item, without the names and time of one placement."""

    return {key: entry.get(key, "" if key in TEXT_KEYS else 0)
            for key in ENTRY_KEYS if key not in ("name", "textName", "addedAt", "addedIn")}


def _files(entry):
    """[(name, size, sha256)] of an entry's files in its folder: the item, then its text copy."""

    files = [(entry["name"], entry["size"], entry.get("sha256", ""))]
    if entry.get("textName"):
        files.append((entry["textName"], entry.get("textSize", 0), entry.get("textSha256", "")))
    return files


class _Plan:
    """One item to place: the folder its files come from, how (linked or copied), and its manifest facts.

    ``files`` is [(name in the source folder, size, sha256)]: the item, then
    its text copy. ``batch`` marks a batch store's files, which every job
    that links them shares. An ``optional`` item whose source changed is
    skipped, and ``skipped`` then says why.
    """

    def __init__(self, base, opener, files, template, *, link, batch=False, optional=False, extra=None):
        self.base, self.opener, self.files, self.template = base, opener, files, template
        self.link, self.batch, self.optional, self.extra = link, batch, optional, extra or {}
        self.skipped = ""


_checked_guard = threading.Lock()
_checked = {}


def _file_state(info, sha256):
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, sha256


def _remember(info, sha256):
    with _checked_guard:
        if len(_checked) >= MAX_CHECKED_FILES:
            _checked.clear()
        _checked[_identity(info)] = _file_state(info, sha256)


def _digest(descriptor):
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, BLOCK_BYTES):
        digest.update(block)
    return digest.hexdigest()


def _unchanged(descriptor, info, sha256):
    """Whether an opened batch file still has ``sha256``.

    Every job's hard link reaches the store's file, and its owner can undo
    the read-only mode, so its bytes are hashed again whenever its size or
    times differ from the last check. The change time also moves with every
    new link, so a link this process makes is remembered as checked (_place).
    """

    with _checked_guard:
        if _checked.get(_identity(info)) == _file_state(info, sha256):
            return True
    if _digest(descriptor) != sha256:
        return False
    _remember(info, sha256)
    return True


def _write_copy(descriptor, folder, name, size, sha256):
    """Copy an opened source into the new file ``name``; its bytes must match the recorded size and sha256."""

    target = folder.create(name)
    identity = None
    try:
        identity = _identity(os.fstat(target))
        digest, total = hashlib.sha256(), 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while block := os.read(descriptor, BLOCK_BYTES):
            total += len(block)
            if total > size:
                raise _Changed(f"{name} is larger than when it was added")
            digest.update(block)
            _write_all(target, block)
        if total != size or (sha256 and digest.hexdigest() != sha256):
            raise _Changed(f"{name} changed after it was added")
        os.fsync(target)
    except BaseException:
        os.close(target)
        target = None
        _unlink_if(folder, name, identity)
        raise
    finally:
        if target is not None:
            os.close(target)
    return identity


def _copy(descriptor, folder, name, size, sha256, staging=None):
    """Copy an opened source to the new file ``name``, never showing a partial file under that name.

    The bytes go to a hidden temporary file in ``staging``, outside the
    workspace, when it is on the folder's device (else beside the name).
    They then take the name with link(2), which never replaces anything,
    and the temporary name is removed: a crash leaves at most a hidden file,
    which prune() removes from the staging area. A folder that cannot hold
    hard links gets the copy written at its name directly, created exclusively.
    """

    try:
        folder.lstat(name)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, "The name is taken", name)
    with ExitStack() as stack:
        place = folder
        if staging is not None:
            try:
                outside = stack.enter_context(_open_path(staging, create=True))
            except OSError:
                outside = None
            if outside is not None and outside.device() == folder.device():
                place = outside
        temporary = f".tmp-{secrets.token_hex(8)}"
        identity = _write_copy(descriptor, place, temporary, size, sha256)
        try:
            try:
                folder.link(place, temporary, name)
            except OSError as exc:
                if exc.errno not in LINK_UNSUPPORTED:
                    raise
                return _write_copy(descriptor, folder, name, size, sha256)
            placed = folder.lstat(name)
            if _identity(placed) != identity:
                _unlink_if(folder, name, _identity(placed))
                raise OSError(errno.EIO, f"The new file {name} does not name the copy")
            return identity
        finally:
            _unlink_if(place, temporary, identity)


def _place(source, source_name, size, sha256, folder, name, *, link, batch=False, staging=None):
    """Put source/source_name at folder/name without replacing anything; return (identity, copy reason).

    FileExistsError means the name is taken. A hard link shares the source's
    inode; where the file system cannot link here, the bytes are copied and
    the reason is returned (``staging`` holds the copy until it is complete).
    _Changed means the source cannot be used. A ``batch`` store's file is
    linked only while it is read-only and its bytes still match ``sha256``;
    one made writable is copied (the copy checks them).
    """

    try:
        descriptor = source.open_read(source_name)
    except OSError as exc:
        raise _Changed(f"{source_name} could not be read ({exc.strerror or exc})") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise _Changed(f"{source_name} is not the file that was added")
        reason = ""
        if link and batch and os.name != "nt" and stat.S_IMODE(info.st_mode) != BATCH_FILE_MODE:
            link, reason = False, "a shared file is no longer read-only"
        if link and batch and not _unchanged(descriptor, info, sha256):
            raise _Changed(f"{source_name} changed after it was added")
        if link:
            try:
                folder.link(source, source_name, name)
            except OSError as exc:
                if exc.errno not in LINK_UNSUPPORTED:
                    raise
                reason = _clause(exc.strerror or str(exc))
            else:
                placed = folder.lstat(name)
                if not stat.S_ISREG(placed.st_mode) or _identity(placed) != _identity(info):
                    _unlink_if(folder, name, _identity(placed))
                    raise OSError(errno.EIO, f"The new link {name} does not name the saved file")
                if batch and (placed.st_size, placed.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
                    _remember(placed, sha256)
                return _identity(placed), ""
        return _copy(descriptor, folder, name, size, sha256, staging), reason
    finally:
        os.close(descriptor)


def _install_item(folder, taken, plan, placed, staging):
    """Place one item's files under the first free numbering of its name; (name, text name, copy reason) or None."""

    with plan.opener() as source:
        link = plan.link and not source.shared
        for number in range(1, MAX_NAME_ATTEMPTS + 1):
            name = _numbered(plan.base, number)
            names = [name, name + ".txt"][:len(plan.files)]
            if any(candidate.casefold() in taken for candidate in names):
                continue
            done, reason = [], ""
            try:
                for (source_name, size, sha256), target in zip(plan.files, names):
                    identity, why = _place(source, source_name, size, sha256, folder, target, link=link,
                                           batch=plan.batch, staging=staging)
                    done.append((target, identity))
                    reason = reason or why
            except FileExistsError:
                for target, identity in done:
                    _unlink_if(folder, target, identity)
                taken.update(candidate.casefold() for candidate in names)
                continue
            except _Changed as exc:
                for target, identity in done:
                    _unlink_if(folder, target, identity)
                if plan.optional:
                    plan.skipped = str(exc)
                    return None
                raise
            except BaseException:
                placed.extend(done)
                raise
            placed.extend(done)
            taken.update(candidate.casefold() for candidate in names)
            return name, names[1] if len(names) > 1 else "", reason
    raise OSError(errno.EEXIST, f"No free name for {plan.base} in the related-works folder")


def _remove_empty(top):
    try:
        top.rmdir(FOLDER)
    except OSError:
        pass


def _add(workspace, manifest_dir, old, plans, *, run_id, staging, problem=NOT_PLAIN, prepare=None, record=None):
    """Place every plan's files in workspace/related-works, then write INDEX.md and the manifest: all or nothing.

    Returns [(entry, copy reason)] for the items added. ``prepare(folder,
    entries)`` runs once the files are placed, before INDEX.md and the
    manifest, so the manifest only ever lists finished files; ``record``
    runs last. A failure removes the files this call placed, restores
    INDEX.md and the manifest, and raises "Could not add the related works: ...".
    """

    raw, entries = old
    if not plans:
        return []
    try:
        top = _Folder.at(workspace)
    except OSError as exc:
        raise RelatedWorksError(FAILED_PREFIX + str(exc)) from exc
    with top:
        try:
            folder = top.child(FOLDER, create=True, problem=problem)
        except OSError as exc:
            raise RelatedWorksError(FAILED_PREFIX + str(exc)) from exc
        with folder:
            placed, added, index_changed, manifest_changed = [], [], False, False
            try:
                taken = {INDEX_NAME.casefold()} | {name.casefold() for entry in entries
                                                   for name in (entry["name"], entry.get("textName")) if name}
                now, run_id = _now(), "" if run_id is None else str(run_id)
                for plan in plans:
                    outcome = _install_item(folder, taken, plan, placed, staging)
                    if outcome is not None:
                        name, text_name, reason = outcome
                        entry = {**plan.template, "name": name, "textName": text_name, "addedAt": now,
                                 "addedIn": run_id, **plan.extra}
                        added.append(({key: entry[key] for key in (*ENTRY_KEYS, *plan.extra)}, reason))
                if not added:
                    if folder.created:
                        _remove_empty(top)
                    return []
                new = [entry for entry, _ in added]
                if prepare is not None:
                    prepare(folder, new)
                index_changed = True
                _write_index(folder, entries + new, staging)
                manifest_changed = True
                _write_manifest(manifest_dir, entries + new)
                if record is not None:
                    record([dict(entry) for entry in new])
                folder.sync()
                return added
            except BaseException as exc:
                # Undo in the order readers trust: the manifest, then INDEX.md, then the files.
                if manifest_changed:
                    try:
                        _restore_manifest(manifest_dir, raw)
                    except (OSError, RelatedWorksError):
                        pass
                if index_changed:
                    try:
                        if entries:
                            _write_index(folder, entries, staging)
                        else:
                            folder.unlink(INDEX_NAME)
                    except (OSError, RelatedWorksError):
                        pass
                for name, identity in reversed(placed):
                    _unlink_if(folder, name, identity)
                if folder.created:
                    _remove_empty(top)
                if isinstance(exc, RelatedWorksError) and str(exc).startswith(FAILED_PREFIX):
                    raise
                if isinstance(exc, Exception):
                    raise RelatedWorksError(FAILED_PREFIX + str(exc)) from exc
                raise


def _install(workspace, manifest_dir, tokens, stage, *, run_id, keep_staged, record, problem, prepare=None):
    tokens = _token_list(tokens)
    if not tokens:
        return []
    with workspace_lock(workspace):
        items = stage.peek(tokens)
        old = _read_manifest(manifest_dir)
        _check_room(len(old[1]), len(items))
        try:
            root = stage._folder()
        except OSError:
            raise RelatedWorksError(EXPIRED) from None
        with root:
            plans = []
            for staged in items:
                item, text = staged.item, staged.item["text"] or {}
                files = [(CONTENT_NAME, item["size"], item["sha256"])]
                if item["text"]:
                    files.append((TEXT_NAME, item["textSize"], item["textSha256"]))
                template = _template({**item, "pages": text.get("pages", 0), "chars": text.get("chars", 0)})
                plans.append(_Plan(item["name"], partial(_staged_source, root, staged.token), files, template,
                                   link=True))
            added = _add(workspace, manifest_dir, old, plans, run_id=run_id, staging=stage.root, problem=problem,
                         prepare=prepare, record=record)
    if not keep_staged:
        for token in tokens:
            stage.discard(token)
    return [entry for entry, _ in added]


def install(workspace, manifest_dir, tokens, stage, *, run_id, keep_staged=False, record=None):
    """Install staged uploads into workspace/related-works; return their new manifest entries, in order.

    ``manifest_dir`` keeps the manifest: the controller folder of the
    workspace, outside it. Every check comes before anything is written: the
    tokens, the manifest, the 200-item limit and the folder. A later failure
    removes what this call placed, restores INDEX.md and the manifest, keeps
    the staged uploads and raises "Could not add the related works: ...".
    ``record(entries)``, when given, runs once everything is in place (for a
    historical run's audit log); if it raises, the install is undone. On
    success the staged uploads are discarded, unless ``keep_staged`` (a job
    being created discards them once it exists).

    Each entry has name, originalName, kind, title, size, sha256, mediaType,
    textName, pages, chars, textProblem, textSize, textSha256 (of the text
    copy), addedAt (UTC) and addedIn (``run_id``).
    """

    return _install(workspace, manifest_dir, tokens, stage, run_id=run_id, keep_staged=keep_staged, record=record,
                    problem=NOT_PLAIN)


@contextmanager
def _staged_source(root, token):
    """The token folder of a staged upload, marked shared when its file already has a second link.

    Such an upload is installed elsewhere already (a job being created keeps
    its uploads until it exists): it is copied, so two jobs never share an
    upload's inode. The staging area's lock is held from that check until
    the files are placed, so two installs of one upload cannot both see it
    unshared. (Callers hold a workspace or store lock first, never after.)
    """

    with workspace_lock(root.path):
        try:
            folder = root.child(token)
        except OSError:
            raise RelatedWorksError(EXPIRED) from None
        with folder:
            try:
                folder.shared = folder.lstat(CONTENT_NAME).st_nlink > 1
            except OSError:
                pass
            yield folder


def listing(workspace, manifest_dir):
    """The related works of a workspace (or batch store) for display, oldest first; never raises.

    ``batch`` is the ID of the batch an item is shared with, or "".
    """

    return [{"name": entry["name"], "originalName": entry.get("originalName", ""), "kind": entry["kind"],
             "title": entry.get("title", ""), "sizeText": size_text(entry["size"]), "textStatus": text_status(entry),
             "addedAt": entry.get("addedAt", ""), "batch": entry.get("batch", "")}
            for entry in _read_manifest(manifest_dir, strict=False)[1]]


def count(workspace, manifest_dir):
    """How many related works a workspace (or batch store) holds; never raises."""

    return len(_read_manifest(manifest_dir, strict=False)[1])


def notice_text(names):
    """The live instruction that tells the author about newly added related works ("" for none)."""

    names = [str(name) for name in names]
    if not names:
        return ""
    joined = ", ".join(names)
    if len(joined) > NOTICE_NAMES_MAX_CHARS:
        joined = f"{len(names)} files"
    return RELATED_WORKS_NOTICE.format(names=joined)


def audit_fields(entry):
    """What a historical run's audit log records for one added related work."""

    return {key: entry.get(key, "") for key in ("name", "originalName", "kind", "size", "sha256", "textName",
                                                "textSize", "textSha256", "addedAt", "addedIn", "batch")}


def describe_entry(entry):
    """One display line for an added related work, such as 'paper.pdf (PDF, 1.2 MB, text extracted (12 pages))'."""

    size = size_text(entry.get("size", 0))
    if entry.get("kind") == "note":
        title = _one_line(entry.get("title") or "")
        parts = [f'note "{title}"' if title else "note", size]
    elif entry.get("mediaType") == PDF_MEDIA_TYPE:
        parts = ["PDF", size, text_status(entry)]
    else:
        parts = [size]
    original = entry.get("originalName") or ""
    if entry.get("kind") == "file" and original and original != entry.get("name"):
        parts.append(f'uploaded as "{_one_line(original)}"')
    return f"{entry.get('name', '')} ({', '.join(parts)})"


def copy_into(source_workspace, source_manifest_dir, target_workspace, target_manifest_dir, *, run_id, batch=None,
              record=None):
    """Copy one run's related works into another run's workspace (a "Retry statement review"); return how many.

    Only regular files named in the source manifest are copied, never links,
    and only while their bytes still match the manifest; any other item is
    skipped. Items of ``batch`` (a BatchStore) are hard-linked from the batch
    store instead, so the new job keeps sharing them, under the same checks
    as BatchStore.link_into: a store file whose bytes changed is skipped.
    """

    entries = _read_manifest(source_manifest_dir, strict=False)[1]
    if not entries:
        return 0
    shared = {} if batch is None else {entry["name"]: entry for entry in _read_manifest(batch.root, strict=False)[1]}

    def linked(entry):
        return batch is not None and entry.get("batch") == batch.id and entry.get("batchName") in shared

    with workspace_lock(target_workspace), ExitStack() as stack:
        old = _read_manifest(target_manifest_dir)
        # A batch item the new job has already (linked when it was created) is not added twice.
        have = {(entry.get("batch"), entry.get("batchName")) for entry in old[1] if entry.get("batch")}
        entries = [entry for entry in entries if (entry.get("batch"), entry.get("batchName")) not in have]
        try:
            source = stack.enter_context(_open_path(Path(source_workspace) / FOLDER))
        except (OSError, RelatedWorksError):
            source = None
        store = None
        if any(linked(entry) for entry in entries):
            store = stack.enter_context(_open_path(batch.root / FOLDER, problem=BATCH_NOT_PLAIN))
        plans = []
        for entry in entries:
            if linked(entry):
                original = shared[entry["batchName"]]
                plans.append(_Plan(entry["name"], lambda: nullcontext(store), _files(original), _template(original),
                                   link=True, batch=True, optional=True,
                                   extra={"batch": batch.id, "batchName": original["name"]}))
            elif source is not None:
                plans.append(_Plan(entry["name"], lambda: nullcontext(source), _files(entry), _template(entry),
                                   link=False, optional=True,
                                   extra={key: entry[key] for key in ("batch", "batchName") if entry.get(key)}))
        _check_room(len(old[1]), len(plans))
        added = _add(target_workspace, target_manifest_dir, old, plans, run_id=run_id, staging=target_manifest_dir,
                     record=record)
    return len(added)


# --- Folder batches ----------------------------------------------------------------------------------------------


def new_batch_id():
    return "b_" + secrets.token_hex(5)


def batch_directory(controller_root, batch_id):
    """The store of one folder batch: <controller folder>/.batches/<batch ID>, outside every job's workspace."""

    if not isinstance(batch_id, str) or not BATCH_ID.match(batch_id):
        raise ValueError("Invalid batch ID.")
    return Path(controller_root) / BATCH_FOLDER / batch_id


def _listed(names):
    listed = ", ".join(names)
    return listed if len(listed) <= NOTICE_NAMES_MAX_CHARS else f"{len(names)} files"


class BatchStore:
    """The related works of one folder batch: one physical copy, hard-linked into every job of the batch.

    The copy lives in <root>/related-works, with its own INDEX.md, and the
    manifest in <root>/related-works.json. The root's name is the batch ID;
    each job entry linked from the store records it as "batch", and the
    item's name in the store as "batchName".
    """

    def __init__(self, root):
        self.root = Path(root)
        self.id = self.root.name
        if not BATCH_ID.match(self.id):
            raise ValueError("Invalid batch ID.")

    def install(self, tokens, stage, *, run_id=None, keep_staged=False):
        """Install staged uploads into the batch store; return its new entries (see install()).

        The new files are made read-only before the store's manifest lists
        them, so link_into never sees an item that is then undone.
        """

        stage.peek(tokens)
        try:
            _open_path(self.root, create=True).close()
        except (OSError, RelatedWorksError) as exc:
            raise RelatedWorksError(FAILED_PREFIX + str(exc)) from exc
        return _install(self.root, self.root, tokens, stage, run_id=self.id if run_id is None else run_id,
                        keep_staged=keep_staged, record=None, problem=BATCH_NOT_PLAIN,
                        prepare=None if os.name == "nt" else self._read_only)

    @staticmethod
    def _read_only(folder, entries):
        """Make the new shared files read-only, as part of the install (a failure undoes it)."""

        for name in (name for entry in entries for name in (entry["name"], entry["textName"]) if name):
            descriptor = folder.open_read(name)
            try:
                os.fchmod(descriptor, BATCH_FILE_MODE)
            finally:
                os.close(descriptor)

    def listing(self):
        return listing(self.root, self.root)

    def count(self):
        return count(self.root, self.root)

    def link_into(self, workspace, manifest_dir, *, run_id, names=None, record=None):
        """Hard-link the batch's related works that one job lacks (or only ``names``) into its related-works/.

        Returns {"entries": the job's new entries, named as in that job's
        folder (for its notice), "copied": the names copied instead of linked,
        "skipped": the batch's names left out because the store's file no
        longer matches what was added, "diagnostic": "" or sentences on both}.
        Every job's link reaches the store's file and an author can make it
        writable again, so each file is checked first: one whose bytes changed
        is skipped, and one that is no longer read-only is copied. A job never
        gets one batch item twice, so repeating a call adds nothing. The job's
        INDEX.md lists the batch's items and its own. ``record`` and failures
        are as for install(); the batch store itself is never changed.
        """

        shared = _read_manifest(self.root)[1]
        if names is not None:
            names = list(names)
            known = {entry["name"] for entry in shared}
            unknown = [name for name in names if name not in known]
            if unknown:
                raise RelatedWorksError(f"The batch has no related work named {unknown[0]}.")
        with workspace_lock(workspace):
            old = _read_manifest(manifest_dir)
            have = {(entry.get("batch"), entry.get("batchName")) for entry in old[1]}
            wanted = [entry for entry in shared if (names is None or entry["name"] in names)
                      and (self.id, entry["name"]) not in have]
            if not wanted:
                return {"entries": [], "copied": [], "skipped": [], "diagnostic": ""}
            _check_room(len(old[1]), len(wanted))
            with _open_path(self.root / FOLDER, problem=BATCH_NOT_PLAIN) as store:
                plans = [_Plan(entry["name"], lambda: nullcontext(store), _files(entry), _template(entry), link=True,
                               batch=True, optional=True, extra={"batch": self.id, "batchName": entry["name"]})
                         for entry in wanted]
                added = _add(workspace, manifest_dir, old, plans, run_id=run_id, staging=manifest_dir, record=record)
        copied = [entry["name"] for entry, reason in added if reason]
        skipped = [plan.base for plan in plans if plan.skipped]
        sentences = []
        if copied:
            reasons = "; ".join(dict.fromkeys(reason for _, reason in added if reason))
            sentences.append(f"The batch's related works could not be hard-linked into this job's related-works "
                             f"folder ({reasons}), so this job has its own copy of {_listed(copied)}, which uses "
                             f"extra disk space.")
        if skipped:
            one = len(skipped) == 1
            sentences.append(f"The batch's {'copy' if one else 'copies'} of {_listed(skipped)} no longer "
                             f"{'matches' if one else 'match'} what was added, so this job did not get "
                             f"{'it' if one else 'them'}.")
        return {"entries": [entry for entry, _ in added], "copied": copied, "skipped": skipped,
                "diagnostic": " ".join(sentences)}


# --- PDF text (child process) ------------------------------------------------------------------------------------


class _SkippedForms(logging.Handler):
    """Notice pypdf leaving out a form XObject's text in this thread: too many forms, or one it could not decode.

    pypdf only logs a warning then (a form over a decoding limit included),
    and the text would be incomplete.
    """

    MESSAGES = ("Exceeded %(limit)d form XObject", "Impossible to decode XFormObject")

    def __init__(self):
        super().__init__(logging.WARNING)
        self.thread, self.skipped = threading.get_ident(), False

    def emit(self, record):
        if record.thread == self.thread and str(record.msg).startswith(self.MESSAGES):
            self.skipped = True


def extract_pdf_text(pdf_path, text_path):
    """Write the text of one PDF to the new file ``text_path``; return {"pages", "chars"} or {"problem"}.

    pypdf runs with the hardened limits of the historical broker's
    extract_pdf, sized for uploads, and a strict reader. Pages are written
    as "[Page {n}]\\n{text}", joined by blank lines.
    """

    from retrieval import historical_search

    if not historical_search.pdf_supported():
        return {"problem": PROBLEM_UNAVAILABLE}
    from pypdf import PdfReader, apply_configuration

    descriptor = os.open(pdf_path, os.O_RDONLY | NOFOLLOW | NONBLOCK | BINARY | CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return {"problem": PROBLEM_FAILED}
        body = stream.read(MAX_FILE_BYTES + 1)
    if len(body) > MAX_FILE_BYTES:
        return {"problem": PROBLEM_FAILED}
    watch = _SkippedForms()
    logging.getLogger("pypdf").addHandler(watch)
    try:
        with apply_configuration(maximum_declared_stream_length=MAX_FILE_BYTES,
                                 array_based_stream_maximum_output_length=MAX_FILE_BYTES,
                                 jbig2_maximum_output_length=MAX_FILE_BYTES,
                                 lzw_maximum_output_length=MAX_FILE_BYTES,
                                 run_length_maximum_output_length=MAX_FILE_BYTES,
                                 zlib_maximum_output_length=MAX_FILE_BYTES,
                                 brotli_maximum_output_length=MAX_FILE_BYTES,
                                 zlib_maximum_recovery_input_length=100000,
                                 image_maximum_buffer_size=MAX_FILE_BYTES,
                                 stream_decoding_work_maximum_length=PDF_STREAM_WORK_BYTES,
                                 stream_filters_maximum_length=4,
                                 page_tree_maximum_entries=10000,
                                 page_tree_maximum_depth=50,
                                 xform_maximum_invocations_per_extraction=100,
                                 jbig2dec_binary=None, disable_legacy_handling=True):
            reader = PdfReader(io.BytesIO(body), strict=True)
            if reader.is_encrypted:
                return {"problem": PROBLEM_ENCRYPTED}
            pages = len(reader.pages)
            if pages > PDF_MAX_PAGES:
                return {"problem": _pages_problem()}
            parts, chars, stream_bytes, found = [], 0, 0, False
            for number, page in enumerate(reader.pages, 1):
                contents = page.get_contents()
                stream_bytes += len(contents.get_data()) if contents is not None else 0
                if stream_bytes > PDF_STREAM_WORK_BYTES:
                    return {"problem": PROBLEM_FAILED}
                text = historical_search.well_formed_text(page.extract_text() or "").strip()
                if watch.skipped:
                    return {"problem": PROBLEM_FAILED}
                found = found or bool(text)
                part = f"[Page {number}]\n{text}"
                chars += len(part) + (2 if parts else 0)
                if chars > PDF_MAX_CHARS:
                    return {"problem": _chars_problem()}
                parts.append(part)
            if not found:
                return {"problem": PROBLEM_NO_TEXT}
    except Exception:
        return {"problem": PROBLEM_FAILED}
    finally:
        logging.getLogger("pypdf").removeHandler(watch)
    text = "\n\n".join(parts)
    descriptor = os.open(text_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | BINARY | CLOEXEC, 0o600)
    try:
        _write_all(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {"pages": pages, "chars": len(text)}


def _peak_memory():
    """This process's peak resident memory in bytes, or None where it cannot be read (Windows)."""

    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _watch_memory(limit, interval=0.1):
    """End this process once its peak memory passes ``limit``; the parent reads that exit as _memory_problem().

    macOS does not enforce RLIMIT_AS, so a thread samples the peak instead.
    """

    if _peak_memory() is None:
        return

    def watch():
        while _peak_memory() <= limit:
            time.sleep(interval)
        os._exit(PDF_MEMORY_EXIT)

    threading.Thread(target=watch, name="pdf-memory-watch", daemon=True).start()


def extract_main(argv=None, *, memory_limit=None):
    """Extract one PDF's text and print the report as one JSON line; ``memory_limit`` caps the peak memory."""

    parser = argparse.ArgumentParser(prog="python -m ui.related_works",
                                     description="Extract the text of one related-work PDF (run by the server).")
    parser.add_argument("--extract-pdf", nargs=2, metavar=("PDF", "TEXT"), required=True)
    arguments = parser.parse_args(argv)
    if memory_limit:
        _watch_memory(memory_limit)
    try:
        report = extract_pdf_text(*arguments.extract_pdf)
    except Exception:
        report = {"problem": PROBLEM_FAILED}
    # The watching thread samples; this last look also catches a peak reached just before the end.
    if memory_limit and (_peak_memory() or 0) > memory_limit:
        report = {"problem": _memory_problem()}
    print(json.dumps(report), flush=True)
    return 0


def child_main(argv=None):
    """The extraction child (python -m ui.related_works): extract_main with its peak memory capped."""

    return extract_main(argv, memory_limit=PDF_MAX_MEMORY_BYTES)


if __name__ == "__main__":
    sys.exit(child_main())
