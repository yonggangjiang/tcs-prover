"""Read-only overview fields for a controller launched before the UI upgrade.

Never restore an App here: restoring its checkpoints could change an active run.
The original controller remains the authority for phase and all user actions.
"""

from collections import OrderedDict
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import threading


ACTIVE_PHASES = {"reviewing", "running", "stopping", "pausing"}
SUMMARY_CACHE_SIZE = 128
FINGERPRINT_BYTES = 4096
_summaries = OrderedDict()
_summaries_lock = threading.Lock()


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _json_file(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _signature(path):
    try:
        info = path.lstat()
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
    except OSError:
        return None


@lru_cache(maxsize=128)
def _statement(directory, signature):
    from .server import saved_statement
    try:
        return saved_statement(directory)
    except (OSError, ValueError):
        return ""


def _fold(line, result, acceptances):
    """Apply one transcript line to the outcome and the acceptance list."""
    try:
        record = json.loads(line)
    except ValueError:
        return result
    if not isinstance(record, dict):
        return result
    if record.get("kind") == "final_result" and isinstance(record.get("output"), str) and record["output"].strip():
        result = "final"
    elif record.get("kind") == "failure_result" and result != "final":
        result = "failure"
    if (record.get("kind") == "status"
            and record.get("label") == "Critic approved"
            and record.get("root") is not False
            and not record.get("activityLabel")):
        acceptances.append(record)
    return result


def _fingerprint(stream, offset):
    """Hash the first bytes and the bytes before offset; rewrites keep the inode."""
    stream.seek(0)
    head = stream.read(min(offset, FINGERPRINT_BYTES))
    start = max(0, offset - FINGERPRINT_BYTES)
    stream.seek(start)
    return hashlib.sha256(head + b"\n" + stream.read(offset - start)).digest()


def _transcript_summary(directory, signature):
    """Recover durable decisions, scanning only lines added since the last poll.

    A running job appends on nearly every poll, so its whole transcript must
    not be parsed again each time. Replaced or rewritten files are rescanned.
    """
    from .batches import BatchCatalog
    with _summaries_lock:
        saved = _summaries.get(directory)
        if saved is not None and saved["signature"] == signature:
            _summaries.move_to_end(directory)
            return saved["value"]
    try:
        # Reuse the catalog's pinned-directory, no-follow regular-file reader.
        # This only opens the exact controller-owned job's transcript.
        catalog = BatchCatalog(directory.parent)
        with catalog._open(directory, "transcript.jsonl") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            offset, result, acceptances = 0, "", []
            if (saved is not None and saved["identity"] == identity
                    and saved["offset"] <= info.st_size
                    and _fingerprint(stream, saved["offset"]) == saved["fingerprint"]):
                offset, result, acceptances = saved["offset"], saved["result"], list(saved["acceptances"])
            stream.seek(offset)
            tail = b""
            for line in stream:
                if not line.endswith(b"\n"):
                    tail = line
                    break
                result = _fold(line, result, acceptances)
                offset += len(line)
            entry = {"signature": signature, "identity": identity, "offset": offset,
                     "fingerprint": _fingerprint(stream, offset),
                     "result": result, "acceptances": tuple(acceptances)}
            # A record still being appended counts now but is never committed.
            if tail:
                result = _fold(tail, result, acceptances)
    except (OSError, ValueError, UnicodeError):
        return "", ()
    entry["value"] = (result, tuple(acceptances))
    with _summaries_lock:
        _summaries[directory] = entry
        _summaries.move_to_end(directory)
        while len(_summaries) > SUMMARY_CACHE_SIZE:
            _summaries.popitem(last=False)
    return entry["value"]


def _elapsed(state, directory, now):
    if state.get("phase") == "paused":
        checkpoint = _json_file(directory / "pause.json")
        try:
            elapsed = float(checkpoint["elapsedSeconds"])
            if checkpoint.get("status") in {"paused", "pausing"} and math.isfinite(elapsed):
                return max(0, elapsed)
        except (ValueError, TypeError, KeyError):
            pass
    try:
        start = _timestamp(state.get("resumedAt") or state["startedAt"])
        end = now if state.get("phase") in ACTIVE_PHASES else _timestamp(
            state.get("finishedAt") or state.get("lastActivityAt") or state["startedAt"]
        )
        base = float(state.get("elapsedSeconds", 0))
        return max(0, base) + max(0, (end - start).total_seconds()) if math.isfinite(base) else 0
    except (ValueError, TypeError, KeyError, AttributeError):
        return 0


def augment_legacy(payload, is_job_list=False, *, runs_root, status_path,
                   retrieval_loader=None, now=None):
    """Add public display metadata without changing legacy state or artifacts."""
    root = Path(runs_root).resolve()
    now = now or datetime.now(timezone.utc)
    status = _json_file(Path(status_path))
    try:
        age = (now - _timestamp(status["updatedAtUtc"])).total_seconds()
        fresh = 0 <= age <= 30
    except (ValueError, TypeError, KeyError, AttributeError):
        fresh = False
    heartbeats = {row.get("runId"): row for row in status.get("runs", []) if isinstance(row, dict)}

    def augment(source):
        state = dict(source)
        identity = state.get("runId")
        if not isinstance(identity, str) or not identity or Path(identity).name != identity or identity in {".", ".."}:
            return state
        directory = root / identity
        if directory.resolve().parent != root or directory.is_symlink():
            return state
        if retrieval_loader is None:
            from .retrieval import public_retrieval_info
            state["retrieval"] = public_retrieval_info(
                state.get("retrievalPolicyPath", ""), run_dir=directory,
                mode=state.get("retrievalMode", "live"),
                saved_cutoff=state.get("retrievalCutoffUtc", ""))
        else:
            state["retrieval"] = retrieval_loader(state.get("retrievalPolicyPath", ""))
        state["retrievalMode"] = state["retrieval"].get("mode", state.get("retrievalMode", "live"))
        state["retrievalCutoffUtc"] = state["retrieval"].get("cutoffExclusiveUtc", state.get("retrievalCutoffUtc", ""))
        phase = state.get("phase")
        state.setdefault("stageLabel", state.get("workflow", {}).get("nodes", {}).get(state.get("activeNode"), {}).get("label", ""))
        if not state["stageLabel"]:
            state["stageLabel"] = {"author": "Proof author", "critic": "Independent critic", "latex_editor": "LaTeX editor", "latex_compile": "PDF compiler", "review": "Statement reviewer"}.get(state.get("activeNode"), "")
        outcome, acceptances = ("", ())
        if not is_job_list or phase == "done" and not state.get("statementReviewOnly"):
            outcome, acceptances = _transcript_summary(directory, _signature(directory / "transcript.jsonl"))
        if phase == "done":
            state["resultKind"] = "review" if state.get("statementReviewOnly") else outcome
        else:
            state.setdefault("resultKind", "")
        if is_job_list:
            return state
        # Keep recovered milestones separate from the controller's incremental
        # trace: inserting old entries there would reorder events on polling.
        state["criticAcceptances"] = [dict(record) for record in acceptances]
        state["statement"] = _statement(directory, _signature(directory / "checked-statement.md"))
        state["activeElapsedSeconds"] = _elapsed(state, directory, now)
        state["processAlive"] = state["workerActive"] = None
        state["processHealthSource"] = "Controller heartbeat unavailable"
        heartbeat = heartbeats.get(identity, {})
        if fresh and heartbeat.get("startedAt") == state.get("startedAt") and heartbeat.get("phase") == phase:
            pid = heartbeat.get("pid")
            alive = False
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except (OSError, OverflowError):
                    pass
            state["processAlive"] = state["workerActive"] = alive
            state["processHealthSource"] = "Live controller heartbeat"
        return state

    if is_job_list:
        return {**payload, "jobs": [augment(row) for row in payload.get("jobs", [])]}
    return augment(payload)
