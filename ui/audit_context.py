"""Bounded retrieval hints for fresh, independent research audits.

Only file identities are carried between audits, never a previous verdict. A
successful auditor advances its baseline; failures leave that baseline unchanged.
A changed auditor or invalid checkpoint starts with a full reassessment.
"""

import hashlib
import json
from pathlib import Path


CONTEXT_MARKER = "[RESEARCH_CONTEXT]"
FOCUS_MARKER = "[AUDIT_FOCUS]"
FOCUSES = (
    "Check load-bearing claims, exact source hypotheses, and the total complexity budget.",
    "Look for a weaker sufficient interface and a structurally different route; synthesize existing lemmas.",
    "Stress-test the current bridge with small counterexamples and identify what survives each obstruction.",
)


def research_snapshot(directory):
    """Hash mathematical records in the paused workspace, without following links."""
    root = Path(directory)
    paths = [root / name for name in (
        "INITIAL_PROMPT.md", "checked-statement.md", "APPROACHES.md",
        "PROVED.md", "draft.md", "saved-candidate.md",
    )]
    approaches = root / "APPROACHES"
    if approaches.is_dir() and not approaches.is_symlink():
        paths.extend(approaches.rglob("*.md"))
    result = {}
    for path in sorted(set(paths)):
        relative = path.relative_to(root)
        if any((root / parent).is_symlink() for parent in (relative, *relative.parents)):
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        result[relative.as_posix()] = {"sha256": digest.hexdigest(), "bytes": size}
    return result


def audit_identity(model, prompt, config):
    value = {"model": model, "prompt": prompt, "context": config.get("context", {})}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def audit_prompt(prompt, snapshot, previous, identity, slot, config):
    """Render metadata hints, preserving marker-free custom prompts byte for byte."""
    if CONTEXT_MARKER not in prompt and FOCUS_MARKER not in prompt:
        return prompt
    options = config.get("context", {})
    full_every = options.get("fullEvery", 4)
    max_paths = options.get("maxPaths", 60)
    if type(full_every) is not int or full_every < 1:
        raise ValueError("Audit context.fullEvery must be a positive integer.")
    if type(max_paths) is not int or not 1 <= max_paths <= 500:
        raise ValueError("Audit context.maxPaths must be an integer from 1 to 500.")
    previous = previous if isinstance(previous, dict) else {}
    baseline = previous.get("snapshot", {})
    reviews = previous.get("reviews", 0)
    valid_snapshot = (isinstance(baseline, dict) and all(
        isinstance(name, str) and isinstance(value, dict)
        and isinstance(value.get("sha256"), str) and type(value.get("bytes")) is int
        and value["bytes"] >= 0 for name, value in baseline.items()
    ))
    compatible = (previous.get("identity") == identity and valid_snapshot
                  and type(reviews) is int and reviews > 0)
    # Persisted checkpoints may predate this format or be incomplete. Never use
    # incompatible metadata as a delta baseline, or let it prevent a fresh audit.
    if not compatible:
        baseline = {}
        reviews = 0
    changed = sorted(name for name, value in snapshot.items() if baseline.get(name) != value)
    removed = sorted(set(baseline) - set(snapshot)) if compatible else []
    task_changed = any(name in changed + removed for name in ("INITIAL_PROMPT.md", "checked-statement.md"))
    full = not compatible or task_changed or (reviews + 1) % full_every == 0
    if len(changed) + len(removed) > max_paths:
        full = True
    heading = "Full independent review" if full else "Change-focused independent review"
    lines = [heading + ".", (
        "Start with INITIAL_PROMPT.md and APPROACHES/index.md (or legacy APPROACHES.md). "
        "Read the active bridge and its complete dependency proofs in PROVED.md and node files. "
        "Use search and bounded reads to retrieve dependencies; do not concatenate the archive. "
        "These hashes indicate changed bytes, not verified mathematics. No earlier verdict is supplied."
    )]
    if full:
        lines.append("Reassess overall route coverage and load-bearing dependencies independently, including unchanged work.")
    else:
        lines.append("Prioritize changed claims and their transitive dependencies; expand to any unchanged record needed to judge them.")
    if compatible and not changed and not removed:
        lines.append("No recorded mathematical files changed. Diagnose stagnation and propose a decisive new experiment; do not repeat editorial advice.")
    names = changed + removed
    lines.append(f"Recorded files: {len(snapshot)}; changed/new: {len(changed)}; removed: {len(removed)}.")
    for name in names[:max_paths]:
        value = snapshot.get(name)
        detail = f"{value['bytes']} bytes; sha256 prefix {value['sha256'][:12]}" if value else "removed"
        lines.append(f"- {json.dumps(name, ensure_ascii=False)}: {detail}")
    if len(names) > max_paths:
        lines.append(f"{len(names) - max_paths} further paths omitted from this hint; inspect the index and directory to cover the full review.")
    lines.append("Report the files/claims actually checked and any uncovered dependency; this hint never certifies complete coverage.")
    return prompt.replace(CONTEXT_MARKER, "\n".join(lines)).replace(FOCUS_MARKER, FOCUSES[(slot - 1) % len(FOCUSES)])
