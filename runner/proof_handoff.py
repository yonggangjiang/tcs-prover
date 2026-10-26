"""Resolve an author's explicit proof file into a verified, immutable value.

This module neither guesses filenames nor writes files. The controller can copy
the returned bytes into a separate critic workspace. Secure descriptor traversal
is required; platforms without it reject file handoffs instead of weakening the
workspace boundary. Inline proofs continue to work on every platform.
"""

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat


MAX_PROOF_BYTES = 8 * 1024 * 1024
PROOF_SUFFIXES = {".md", ".txt", ".tex"}
PROTECTED_COMPONENTS = {
    "prompts", "codex-home", "discovery-auth", "discovery-state",
    "input.md", "draft.md", "checked-statement.md", "job-settings.json",
    "initial_prompt.md", "agents.md", "skill.md", "config.toml", "auth.json",
    "transcript.jsonl", "saved-candidate.md", "final-input.json",
    "latex-input.md", "final.tex", "failure-summary.md", "partial-output.md",
    "fresh-critic-audits", "config", "credentials",
}


class ProofHandoffError(ValueError):
    """An explicit proof reference could not be safely resolved."""


@dataclass(frozen=True)
class ProofSnapshot:
    relative_path: str
    text: str
    sha256: str
    size_bytes: int

    @property
    def content(self):
        """The exact original UTF-8 bytes, without newline normalization."""
        return self.text.encode("utf-8")

    def receipt(self):
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


def extract_proof_reference(final_message, *, code_span=True):
    """Return an explicit local file reference, or None for an inline proof.

    Prefer ``PROOF_FILE: proof.md`` or an exact ``{"proof_file":"proof.md"}``
    object. Compatibility accepts a short message whose opening sentence says
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
    if (code_span and len(message) <= 1500
            and not re.search(r'^\s*PROOF_FILE\s*:|"proof_file"\s*:|\[[^\]\n]+\]\(', unwrapped, re.M)):
        unwrapped = re.sub(r"(?m)^([ \t]*)`(PROOF_FILE[ \t]*:[^`\n]*)`\.?[ \t\r]*$", r"\1\2", unwrapped)
    marker_lines = re.findall(r"^\s*PROOF_FILE\s*:(.*)$", unwrapped, re.M)
    if marker_lines:
        if re.search(r'"proof_file"\s*:|\[[^\]\n]+\]\(', unwrapped):
            raise ProofHandoffError("Use one proof reference format; mixed pointers are ambiguous.")
        references = []
        for raw in marker_lines:
            reference = _undecorated_marker_value(raw.strip())
            if reference.startswith('"'):
                try:
                    reference = json.loads(reference)
                except ValueError as exc:
                    raise ProofHandoffError("Invalid quoted PROOF_FILE path.") from exc
            if not isinstance(reference, str) or not reference.strip():
                raise ProofHandoffError("PROOF_FILE needs a nonempty path.")
            references.append(reference)
        if len(set(references)) != 1:
            raise ProofHandoffError("The author returned conflicting proof file references.")
        return references[0]
    if re.match(r"^PROOF_FILE\b", unwrapped):
        raise ProofHandoffError("Use PROOF_FILE: followed by a local proof path.")

    # Existing author messages use these two opening sentences, with optional
    # short summaries afterward. The opening, not the mere presence of a link,
    # must explicitly designate the proof artifact.
    if len(message) > 1500:
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
            raise ProofHandoffError("The proof file exceeds the handoff size limit.")
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
                raise ProofHandoffError("The proof file exceeds the handoff size limit.")
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
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ProofHandoffError("The proof handoff size limit must be a positive integer.")
    reference = extract_proof_reference(final_message, code_span=False)
    if reference is not None:
        return _snapshot(reference, workspace, max_bytes)
    try:
        reference = extract_proof_reference(final_message)
        return None if reference is None else _snapshot(reference, workspace, max_bytes)
    except ProofHandoffError:
        return None


def _snapshot(reference, workspace, max_bytes):
    _require_secure_access()
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
        raise ProofHandoffError("The referenced proof file could not be safely read.") from exc
    return ProofSnapshot(Path(*parts).as_posix(), text, hashlib.sha256(data).hexdigest(), len(data))
