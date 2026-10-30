"""Resolve an author's explicit proof file into a verified, immutable value.

This module neither guesses filenames nor writes files. The controller can copy
the returned bytes into a separate critic workspace. Secure descriptor traversal
is required; platforms without it reject file handoffs instead of weakening the
workspace boundary. Inline proofs continue to work on every platform.
"""

from contextlib import ExitStack
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat


MAX_PROOF_BYTES = 8 * 1024 * 1024
PROOF_SUFFIXES = {".md", ".txt", ".tex"}
# A plain PROOF_FILE line is a pointer only in a message this short (without
# its marker lines); in a longer one it is a leftover beside an inline proof.
POINTER_MAX_CHARS = 1500
# PROOF_FILE (in any case, possibly in Markdown emphasis) and a colon, or a
# space before the path. A line written as an inline-code span is not one.
_MARKER = r"^[ \t]*(?:\*\*|__|\*|_)?PROOF_FILE(?:\*\*|__|\*|_)?(?:[ \t]*:(?:\*\*|__|\*|_)?|(?=[ \t]+\S))"
MARKER_LINE = re.compile(_MARKER + r"(.*)$", re.I | re.M)
_MARKER_WHOLE_LINE = re.compile(_MARKER + r"[^\n]*(?:\n|\Z)", re.I | re.M)
_LINK = r"\[[^\]\n]+\]\("
PROTECTED_COMPONENTS = {
    "prompts", "codex-home", "discovery-auth", "discovery-state",
    "input.md", "draft.md", "checked-statement.md", "job-settings.json",
    "initial_prompt.md", "agents.md", "skill.md", "config.toml", "auth.json",
    "transcript.jsonl", "saved-candidate.md", "final-input.json",
    "latex-input.md", "final.tex", "failure-summary.md", "partial-output.md",
    "fresh-critic-audits", "config", "credentials",
    # The user's related works and the paused author's progress summary are never the proof.
    "related-works", "progress-summary.md",
}


class ProofHandoffError(ValueError):
    """An explicit proof reference could not be safely resolved."""


class ProofTooLarge(ProofHandoffError):
    """A proof file over the handoff size limit; ``size_bytes`` says how large it is (at least)."""

    def __init__(self, size_bytes, limit):
        super().__init__("The proof file exceeds the handoff size limit.")
        self.size_bytes, self.limit = size_bytes, limit


@dataclass(frozen=True)
class ProofSnapshot:
    relative_path: str
    text: str
    sha256: str
    size_bytes: int
    # An inline proof whose leftover PROOF_FILE lines were removed: no file.
    removed_marker_lines: int = 0

    @property
    def content(self):
        """The exact original UTF-8 bytes, without newline normalization."""
        return self.text.encode("utf-8")

    def receipt(self):
        if self.removed_marker_lines:
            return {"kind": "inline", "removed_marker_lines": self.removed_marker_lines,
                    "sha256": self.sha256, "size_bytes": self.size_bytes}
        return {"proof_file": self.relative_path, "sha256": self.sha256,
                "size_bytes": self.size_bytes}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProofHandoffError("Proof reference JSON has duplicate keys.")
        result[key] = value
    return result


def _undecorated_marker_value(reference):
    # A sentence period right after a name, or inline-code backticks around a
    # path, leave a final name without a proof suffix, so such values are always
    # rejected; removing them never changes an acceptable path, and every path
    # check still runs on the result. A "." or ".." component is not a period.
    stem = reference[:-1]
    if (reference.endswith(".") and not stem.endswith(("/", "."))
            and (stem.endswith(("`", '"')) or Path(stem).suffix.lower() in PROOF_SUFFIXES)):
        reference = stem
    if len(reference) >= 2 and reference[0] == reference[-1] == "`" and "`" not in reference[1:-1]:
        reference = reference[1:-1]
    return reference


def _marker_path(value):
    """The path a PROOF_FILE line names: a quoted or inline-code path, or the
    first path with a proof suffix ("proof.md (complete)" names proof.md)."""
    value = re.sub(r"^\*{1,2}(?=\S)", "", value.strip())  # A bold or italic path.
    if value.startswith('"'):
        try:
            reference, _ = json.JSONDecoder().raw_decode(value)
        except ValueError as exc:
            raise ProofHandoffError("Invalid quoted PROOF_FILE path.") from exc
    elif value.startswith("`") and "`" in value[1:]:
        reference = value[1:value.index("`", 1)]
    else:
        # A suffix counts only before the end, a space, or punctuation that
        # ends the value or a word, so "proof.md?x=1" stays one (refused) path.
        named = re.match(r"(.*?\.(?:md|txt|tex))(?=$|\s|[.,;:!?)\]*_]+(?:\s|$))", value, re.I)
        reference = named.group(1) if named else _undecorated_marker_value(value)
    if not isinstance(reference, str) or not reference.strip():
        raise ProofHandoffError("PROOF_FILE needs a nonempty path.")
    return reference


def _marker_word(value):
    """The first word of a PROOF_FILE value that is a proof path ("see `proof.md`" names proof.md).

    Only a fallback: the value as written wins whenever it names a readable
    file, so a path with spaces keeps working. A single word is never
    reread, and a word must already be a plain path, so removing punctuation
    cannot turn a refused path ("proof.md/..") into another one.
    """
    words = value.split()
    for word in words if len(words) > 1 else ():
        word = word.strip("`'\"*([<").rstrip(".,;:!?").strip("`'\"*)]>").rstrip(".,;:!?")
        if word and Path(word).suffix.lower() in PROOF_SUFFIXES and _comparable(word) == word:
            return word
    return None


def _marker_snapshot(value, workspace, max_bytes):
    """Snapshot the file a PROOF_FILE line names; failing that, its first word that is a proof path."""
    reference = _marker_path(value)
    try:
        return _snapshot(reference, workspace, max_bytes)
    except ProofHandoffError as error:
        alternative = _marker_word(value)
        if alternative is None or _comparable(alternative) == _comparable(reference):
            raise
        try:
            return _snapshot(alternative, workspace, max_bytes)
        except ProofHandoffError:
            raise error from error.__cause__  # The reason names the path as written.


def _comparable(reference):
    # "proof.md", "./proof.md" and "proof.md/." name one file; ".." is kept.
    return PurePosixPath(reference).as_posix()


def _without_markers(text):
    return _MARKER_WHOLE_LINE.sub("", text)


def extract_proof_reference(final_message, *, code_span=True):
    """Return an explicit local file reference, or None for an inline proof.

    Prefer ``PROOF_FILE: proof.md`` or an exact ``{"proof_file":"proof.md"}``
    object. A marker line is a pointer only in a pointer-sized message; beside
    a complete inline proof it is a leftover that resolve_author_proof()
    removes. Near-misses whose intent is clear are accepted: ``PROOF_FILE
    proof.md``, Markdown emphasis around the marker, words after the path,
    equivalent paths on several marker lines, and links after the first blank
    line. Compatibility accepts a short message whose opening sentence says
    that the complete proof is in a Markdown link, followed by a short synopsis.
    An incidental file link within an inline argument is never a handoff.
    With ``code_span``, a pointer-sized message may also write its marker line
    as one inline-code span (see resolve_author_proof()).
    """
    if not isinstance(final_message, str):
        raise ProofHandoffError("The author response must be text.")
    message = final_message.strip()
    fenced = re.fullmatch(r"```(?:json|text)?\s*\n(.*?)\n```", message, re.S)
    unwrapped = fenced.group(1).strip() if fenced else message
    if unwrapped.startswith("{") and re.search(r'"proof_file"\s*:', unwrapped):
        try:
            value = json.loads(unwrapped, object_pairs_hook=_json_object)
        except (ValueError, TypeError) as exc:
            raise ProofHandoffError("Invalid proof reference JSON.") from exc
        if (not isinstance(value, dict) or set(value) != {"proof_file"}
                or not isinstance(value["proof_file"], str) or not value["proof_file"].strip()):
            raise ProofHandoffError("Proof reference JSON must contain only a nonempty proof_file string.")
        return value["proof_file"]

    # A marker line written as one inline-code span is still that marker when
    # it is the only pointer of a pointer-sized message. Beside a plain marker,
    # a JSON key or a link, or inside a complete proof, it stays text, so those
    # messages resolve exactly as they did before.
    if (code_span and len(message) <= POINTER_MAX_CHARS and not MARKER_LINE.search(unwrapped)
            and not re.search(r'"proof_file"\s*:|' + _LINK, unwrapped)):
        unwrapped = re.sub(r"(?m)^([ \t]*)`(PROOF_FILE[ \t]*:[^`\n]*)`\.?[ \t\r]*$", r"\1\2", unwrapped)
    marker_lines = MARKER_LINE.findall(unwrapped)
    if marker_lines:
        if len(_without_markers(unwrapped).strip()) > POINTER_MAX_CHARS:
            return None  # A complete inline proof with a leftover marker line.
        # Links after the first blank line belong to a synopsis, not the pointer.
        head = re.split(r"\n[ \t\r]*\n", unwrapped, maxsplit=1)[0]
        if re.search(r'"proof_file"\s*:', unwrapped) or re.search(_LINK, head):
            raise ProofHandoffError("Use one proof reference format; mixed pointers are ambiguous. Give PROOF_FILE "
                                    "without a Markdown link before the first blank line and without a proof_file key.")
        references = [_marker_path(raw) for raw in marker_lines]
        if len({_comparable(reference) for reference in references}) != 1:
            names = ", ".join(dict.fromkeys(references))
            raise ProofHandoffError(f"The author returned conflicting proof file references ({names}).")
        return references[0]
    if re.match(r"^PROOF_FILE\b", unwrapped):
        raise ProofHandoffError("Use PROOF_FILE: followed by a local proof path.")

    # Existing author messages use these two opening sentences, with optional
    # short summaries afterward. The opening, not the mere presence of a link,
    # must explicitly designate the proof artifact.
    if len(message) > POINTER_MAX_CHARS:
        return None
    link = r"\[(?P<label>[^\]\n]+)\]\(\s*(?P<path><[^>\n]+>|[^()\n]+?)\s*\)"
    opening = re.match(
        r"^(?:(?:The\s+)?(?:complete\s+)?(?:audited\s+)?proof\s+"
        r"(?:is\s+(?:saved\s+)?(?:in|at)|has\s+been\s+saved\s+(?:in|at))\s+"
        r"|(?:See|Read)\s+|(?:The\s+)?)" + link, message, re.I)
    if not opening:
        return None
    if re.search(r'"proof_file"\s*:', message):
        raise ProofHandoffError("Use one proof reference format; mixed pointers are ambiguous.")
    label = opening.group("label")
    if not re.search(r"\bproof\b", label, re.I):
        return None
    tail = message[opening.end():]
    first_tail = tail.split("\n\n", 1)[0].strip()
    synopsis = tail.split("\n\n", 1)[1] if "\n\n" in tail else ""
    if re.search(r"(?im)^\s*(?:#{1,6}\s*|\*\*)?(?:proof|theorem|lemma)\b", synopsis):
        return None
    reference = opening.group("path").strip()
    reference = reference[1:-1] if reference.startswith("<") else reference
    # "The [proof](...) is saved ..." is also used by older author prompts.
    if message[:opening.start("label") - 1].strip().lower() == "the":
        saved = re.fullmatch(r"is\s+(?:saved|available)\s+in\s+`([^`\n]+)`[.!]?", first_tail, re.I)
        if not saved:
            return None
        if saved.group(1) not in (reference, Path(reference).name):
            raise ProofHandoffError("The linked proof and named saved file disagree.")
    elif first_tail not in ("", ".", "!"):
        return None
    links = re.findall(r"\[[^\]\n]+\]\([^\n]*?\)", message)
    if len(links) != 1:
        raise ProofHandoffError("The proof pointer contains multiple links; use PROOF_FILE explicitly.")
    return reference


def _relative_parts(reference, workspace):
    if (not reference or reference != reference.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in reference)
            or any(char in reference for char in ("\\", ":", "?", "#", "%"))
            or PureWindowsPath(reference).drive):
        raise ProofHandoffError("The proof path contains unsupported or ambiguous characters.")
    supplied = Path(reference)
    if ".." in supplied.parts:
        raise ProofHandoffError("The proof path must not traverse parent directories.")
    if supplied.is_absolute():
        # Absolute compatibility paths must be lexically inside the supplied
        # workspace. Do not resolve the candidate through symlinks to decide.
        try:
            supplied = supplied.relative_to(workspace)
        except ValueError as exc:
            raise ProofHandoffError("The proof file must be inside this author's workspace.") from exc
    parts = supplied.parts
    if not parts or any(part.startswith(".") or part.lower() in PROTECTED_COMPONENTS for part in parts):
        raise ProofHandoffError("The proof path is a protected or hidden workspace path.")
    if supplied.suffix.lower() not in PROOF_SUFFIXES:
        raise ProofHandoffError("The proof file must be Markdown, plain text, or LaTeX.")
    return parts


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(info):
    # The controller keeps writing beside the author's files, so folder times,
    # sizes, link counts and mode bits change harmlessly. Each component is
    # opened no-follow from a held parent descriptor, so a swapped or replaced
    # folder still changes its device, inode or file type.
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid)


def _require_secure_access():
    if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "geteuid")
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd):
        raise ProofHandoffError("Secure proof file handoff requires descriptor-based no-follow file access.")


def _secure_read(workspace, parts, max_bytes, expected_root):
    _require_secure_access()
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    with ExitStack() as stack:
        def open_at(name, parent=None, directory=False):
            fd = os.open(name, flags | (os.O_DIRECTORY if directory else getattr(os, "O_NONBLOCK", 0)),
                         dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        root_before = os.stat(workspace, follow_symlinks=False)
        if _directory_identity(root_before) != expected_root:
            raise ProofHandoffError("The author workspace changed before proof handoff.")
        parent = open_at(workspace, directory=True)
        if (not stat.S_ISDIR(root_before.st_mode)
                or _directory_identity(root_before) != _directory_identity(os.fstat(parent))):
            raise ProofHandoffError("The author workspace changed during proof handoff.")
        traversed = []
        for part in parts[:-1]:
            before = os.stat(part, dir_fd=parent, follow_symlinks=False)
            child = open_at(part, parent, directory=True)
            if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.geteuid()
                    or _directory_identity(before) != _directory_identity(os.fstat(child))):
                raise ProofHandoffError("A proof directory changed during handoff.")
            traversed.append((parent, part, before))
            parent = child
        before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ProofHandoffError("The proof must be a regular file with no symbolic or hard links.")
        if before.st_uid != os.geteuid():
            raise ProofHandoffError("The proof must belong to the controller's local user.")
        if before.st_size > max_bytes:
            raise ProofTooLarge(before.st_size, max_bytes)
        fd = open_at(parts[-1], parent)
        if _identity(before) != _identity(os.fstat(fd)):
            raise ProofHandoffError("The proof file changed before it could be read.")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ProofTooLarge(size, max_bytes)
        if (_identity(before) != _identity(os.fstat(fd))
                or _identity(before) != _identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False))):
            raise ProofHandoffError("The proof file changed while it was read.")
        for directory, name, original in traversed:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if _directory_identity(original) != _directory_identity(current):
                raise ProofHandoffError("A proof directory changed while the file was read.")
        if _directory_identity(root_before) != _directory_identity(os.stat(workspace, follow_symlinks=False)):
            raise ProofHandoffError("The author workspace changed while the proof was read.")
        return b"".join(chunks)


def resolve_author_proof(final_message, workspace, *, max_bytes=MAX_PROOF_BYTES):
    """Snapshot an explicit proof reference, returning None for inline output.

    A pointer written only as an inline-code marker line is honored when its
    file resolves; otherwise the message is reviewed as text, as it was before
    such pointers were recognized, instead of refusing the handoff.

    A complete inline proof that also has plain PROOF_FILE lines is reviewed
    as text without those lines (an inline snapshot whose receipt says so).
    When a named file is readable and is not the same proof, the handoff is
    refused, so that the author says which one it submits.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ProofHandoffError("The proof handoff size limit must be a positive integer.")
    reference = extract_proof_reference(final_message, code_span=False)
    if reference is not None:
        markers = MARKER_LINE.findall(final_message)
        if markers and _marker_path(markers[0]) == reference:
            return _marker_snapshot(markers[0], workspace, max_bytes)
        return _snapshot(reference, workspace, max_bytes)
    inline = _inline_proof(final_message, workspace, max_bytes)
    if inline is not None:
        return inline
    try:
        reference = extract_proof_reference(final_message)
        return None if reference is None else _snapshot(reference, workspace, max_bytes)
    except ProofHandoffError:
        return None


def _proof_lines(text):
    """The lines that carry a proof: blank lines, Markdown headings and spacing do not count."""
    lines = (" ".join(line.split()) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n" + "\n".join(line for line in lines if line and not re.match(r"#{1,6}(?:\s|$)", line)) + "\n"


def _inline_proof(final_message, workspace, max_bytes):
    """A complete inline proof with leftover PROOF_FILE lines, as text without them."""
    markers = MARKER_LINE.findall(final_message)
    text = _without_markers(final_message)
    if not markers or len(text.strip()) <= POINTER_MAX_CHARS:
        return None
    for value in dict.fromkeys(markers):
        try:
            named = _marker_snapshot(value, workspace, max_bytes)
        except ProofHandoffError:
            continue  # No readable file: only the inline proof was submitted.
        # A title or spacing of its own does not make the file another proof.
        if _proof_lines(named.text) not in _proof_lines(text):
            raise ProofHandoffError(
                f"The response contains a complete proof and also names {named.relative_path} as the proof "
                "file, and the two differ. Submit one of them: the complete proof without the PROOF_FILE line, "
                "or only the PROOF_FILE line.")
    data = text.encode("utf-8")
    return ProofSnapshot("", text, hashlib.sha256(data).hexdigest(), len(data), removed_marker_lines=len(markers))


def _read_problem(error, parts):
    """Why a proof file could not be read, in plain words and without its content."""
    if isinstance(error, UnicodeDecodeError):
        return f"not UTF-8 text at byte {error.start}"
    if isinstance(error, UnicodeError):
        return "not UTF-8 text"
    if isinstance(error, FileNotFoundError):
        missing = error.filename
        if parts and missing in parts[:-1]:
            return f"folder {missing} not found"
        return "file not found" if parts and missing in (None, parts[-1]) else "author workspace not found"
    if isinstance(error, PermissionError):
        return "permission denied"
    if isinstance(error, NotADirectoryError):
        return "a folder on its path is not a folder"
    if isinstance(error, OSError) and error.errno == errno.ELOOP:
        return "a symbolic link on its path"
    if isinstance(error, OSError) and error.strerror:
        return error.strerror[:1].lower() + error.strerror[1:]
    if isinstance(error, RuntimeError):
        return "its path could not be resolved"
    return type(error).__name__


def _snapshot(reference, workspace, max_bytes):
    _require_secure_access()
    parts = ()
    try:
        root = Path(workspace)
        if not root.is_absolute():
            raise ProofHandoffError("The controller must supply an absolute author workspace.")
        original_root = root.lstat()
        if not stat.S_ISDIR(original_root.st_mode):
            raise ProofHandoffError("The author workspace must be a regular directory, not a symbolic link.")
        if original_root.st_uid != os.geteuid():
            raise ProofHandoffError("The author workspace must belong to the controller's local user.")
        expected_root = _directory_identity(original_root)
        canonical_root = root.resolve(strict=True)
        if Path(reference).is_absolute() and not Path(reference).is_relative_to(root):
            parts = _relative_parts(reference, canonical_root)
        else:
            parts = _relative_parts(reference, root)
        data = _secure_read(canonical_root, parts, max_bytes, expected_root)
        if _directory_identity(root.lstat()) != expected_root:
            raise ProofHandoffError("The original author workspace changed during proof handoff.")
        text = data.decode("utf-8", errors="strict")
        if not text.strip() or "\x00" in text:
            raise ProofHandoffError("The proof file must contain nonempty UTF-8 text.")
    except (OSError, UnicodeError, RuntimeError) as exc:
        # The name and the cause tell the author what to fix; the content is never shown.
        name = Path(*parts).as_posix() if parts else reference
        raise ProofHandoffError(f"The referenced proof file could not be safely read "
                                f"({name}: {_read_problem(exc, parts)}).") from exc
    return ProofSnapshot(Path(*parts).as_posix(), text, hashlib.sha256(data).hexdigest(), len(data))
