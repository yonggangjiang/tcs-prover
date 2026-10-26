"""Manage proof jobs and serve the local browser interface."""

import errno
import getpass
import http.client
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import workflow_runner as runtime
from runner import claude_cli, providers
from . import audits, retrieval
from .review import REVIEW_PROMPT


# Files and model settings shared by the UI and its worker processes.
ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
RUNS = ROOT / "runs"
WORKFLOWS = ROOT / "workflows"
HOST, PORT = "127.0.0.1", 8765
DEFAULT_CRITIC_ROUNDS = runtime.DEFAULT_CRITIC_ROUNDS
MAX_CRITIC_ROUNDS = runtime.MAX_CRITIC_ROUNDS
MAX_THINKING_HOURS = runtime.MAX_AUTHOR_HOURS
DEFAULT_THINKING_HOURS = runtime.DEFAULT_AUTHOR_HOURS
MODELS = runtime.MODELS
OFFERED_MODELS = runtime.OFFERED_MODELS
EFFORTS = runtime.EFFORTS
SPEEDS, DEFAULT_SPEED = runtime.SPEEDS, runtime.DEFAULT_SPEED
DEFAULT_REVIEW_MODEL = runtime.REVIEW_MODEL
DEFAULT_AUTHOR_MODEL = runtime.AUTHOR_MODEL
DEFAULT_CRITIC_MODEL = runtime.CRITIC_MODEL
DEFAULT_WRITER_MODEL = runtime.WRITER_MODEL
DEFAULT_REASONING_EFFORT = runtime.EFFORT
DEFAULT_REVIEW_EFFORT = runtime.REVIEW_EFFORT
REASONING_SUMMARIES = runtime.REASONING_SUMMARIES
DEFAULT_REASONING_SUMMARY = runtime.DEFAULT_REASONING_SUMMARY
STOP_TIMEOUT_SECONDS = 2
# Stop interrupts a POSIX runner group this long before killing it, so the
# runner's cleanup deletes any key-bearing Claude settings file it holds.
STOP_GRACE_SECONDS = 3
WINDOWS_EVERYONE_SID = "*S-1-1-0"
AUTHOR_LIMIT_FILENAME = "author-limit.json"
AUTHOR_STEER_FILENAME = "author-steer.json"
PAUSE_FILENAME = "pause.json"
PAUSE_REQUEST_FILENAME = "pause-request.json"
JOB_SETTINGS_FILENAME = "job-settings.json"
MANUAL_STOP_FILENAME = "manual-stop.json"
CONTINUATION_SOURCE_FILENAME = "continuation-source.json"
REVIEW_INPUT_FILENAME = "review-input.json"
RESEARCH_MEMORY_FILES = {"INITIAL_PROMPT.md", "APPROACHES.md", "PROVED.md", "audit.md"}
APPROACH_MEMORY_FILE = re.compile(r"APPROACHES/(?:index|INDEX|A[0-9]{3,}(?:-[A-Za-z0-9_-]+)?)\.md\Z")
AUDIT_MEMORY_FILE = re.compile(r"(?:AUDITS|audit_history)/[A-Za-z0-9][A-Za-z0-9_.-]*\.md\Z")
MAX_REQUEST_BYTES = 100_000
MAX_DOCUMENT_REQUEST_BYTES = 8 * 1024 * 1024
MAX_BATCH_STATEMENTS = 200
MAX_BATCH_STATEMENT_CHARS = 100_000
MAX_SOURCE_FILE_CHARS = 255
# An error-paused author is resumed automatically after the same wait as
# every other model-call retry (workflow_runner.MODEL_RETRY_SECONDS).
AUTO_RESUME_SECONDS = runtime.MODEL_RETRY_SECONDS
# How the runner's own messages begin when the workflow budget runs out
# ("Workflow time limit reached." or "...; saved work remains resumable.").
# A provider or CLI error that merely mentions a time limit is still retried.
WORKFLOW_TIME_LIMIT_ERROR = "Workflow time limit reached"
ROLES = ("review", "author", "critic", "writer")
ROLE_LABELS = {
    "review": "statement reviewer", "author": "proof author",
    "critic": "critic", "writer": "LaTeX editor",
}
# Jobs whose model processes are running or about to run.
ACTIVE_PHASES = {"reviewing", "running", "stopping", "pausing"}
# Idle jobs that launch again with their saved models (Resume, Approve, Start).
WAITING_PHASES = {"paused", "reviewed", "prepared"}
# The API model Test button sends one tiny request and follows no redirect.
API_TEST_TIMEOUT_SECONDS = 30
API_TEST_PROMPT = "Reply with OK."
API_TEST_MAX_TOKENS = 16
API_TEST_MAX_RESPONSE_BYTES = 64 * 1024
ANTHROPIC_VERSION = "2023-06-01"


def retry_interval_text(seconds=None):
    """The automatic retry interval in words ("1 minute")."""

    seconds = AUTO_RESUME_SECONDS if seconds is None else seconds
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} seconds"


def load_pause_checkpoint(run_dir):
    """Read pause.json for Resume; a damaged one raises ValueError before anything uses it.

    The run folder is the author's writable workspace, so the file can be
    truncated, replaced or edited. Resume needs an object with the saved
    workflow state, node, stage and elapsed time.
    """

    try:
        checkpoint = json.loads((Path(run_dir) / PAUSE_FILENAME).read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"The saved pause checkpoint ({PAUSE_FILENAME}) is damaged: {exc}") from exc
    elapsed = checkpoint.get("elapsedSeconds") if isinstance(checkpoint, dict) else None
    if not (isinstance(checkpoint, dict) and isinstance(checkpoint.get("state"), dict)
            and all(isinstance(checkpoint.get(key), str) and checkpoint[key] for key in ("node", "stage"))
            and isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
            and math.isfinite(elapsed) and elapsed >= 0):
        raise ValueError(f"The saved pause checkpoint ({PAUSE_FILENAME}) is damaged: it lacks the saved "
                         "workflow state, node, stage or elapsed time")
    return checkpoint


def read_pause_object(run_dir):
    """Read pause.json for display and routing; a missing or damaged file reads as {}.

    Only Resume needs a valid checkpoint, and it checks one with load_pause_checkpoint.
    """

    try:
        checkpoint = json.loads((Path(run_dir) / PAUSE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return checkpoint if isinstance(checkpoint, dict) else {}


def goal_thread_from_record(record):
    """Accept only an explicit root author status, never a subagent thread."""

    identity = record.get("threadId")
    if (
        record.get("kind") in {"status", "workflow_paused"} and record.get("stage") in {"solve", "repair"}
        and record.get("root") is not False
        and isinstance(identity, str) and identity.strip() and "\0" not in identity
    ):
        return identity.strip()
    return ""


def critic_uses_parallel_audits():
    """Whether the built-in critic supports legacy controller audit checkpoints."""

    return bool(runtime.builtin_workflow("author_critic")["nodes"]["critic"].get("parallel"))


def isolated_process_options():
    """Put each Codex wrapper in a group that can be stopped as one unit."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process_tree(process):
    """Stop one Codex wrapper and every process it launched.

    On POSIX the whole group is interrupted first (Ctrl-C), so the runner
    unwinds its cleanup and deletes private settings files, then killed after
    STOP_GRACE_SECONDS. Windows ends the tree at once, without that cleanup.
    Either way the private settings a killed runner left behind are removed
    once it has ended (claude_cli.sweep_stale_settings()).
    """

    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # Popen.terminate() only kills the Python wrapper on Windows. taskkill /T
        # also stops its Codex app-server, MCP helpers, and delegated agents.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=STOP_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=STOP_GRACE_SECONDS)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:
            # Also stops children that outlived an interrupted runner.
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # This fallback is only for a failed platform tree-stop operation.
        process.kill()
        process.wait()
    claude_cli.sweep_stale_settings()


def grant_windows_access(path, identity, permissions):
    """Add one explicit Windows ACL entry and surface configuration failures."""

    result = subprocess.run(
        ["icacls", str(path), "/grant:r", f"{identity}:{permissions}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or "icacls failed").strip()
        raise OSError(f"Cannot prepare the runs directory for Codex: {detail}")


def current_windows_account():
    """Return the account name accepted by icacls for the server process."""

    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def prepare_private_directory(path):
    """Create a private directory that still works with a restricted token."""

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if os.name == "nt":
        # OWNER RIGHTS alone is insufficient for Windows restricted-token
        # access checks; retain a concrete allow entry for the signed-in user.
        grant_windows_access(
            path, current_windows_account(), "(OI)(CI)(F)"
        )


def prepare_runs_directory(path):
    """Keep run contents private while allowing sandbox traversal on Windows."""

    prepare_private_directory(path)
    if os.name != "nt":
        return
    # Each unelevated Codex session receives access to its selected run folder,
    # but Windows must first let that sandbox identity traverse the parent.
    # RX applies only to ``runs`` itself (no OI/CI), so other run contents keep
    # their protected per-directory ACLs.
    grant_windows_access(path, WINDOWS_EVERYONE_SID, "(RX)")


# The sandboxed author can write its run folder. Never follow a link, or
# block on a FIFO, that it leaves where the controller keeps a run log.
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
BINARY = getattr(os, "O_BINARY", 0)
# Open errors caused by the entry itself: a link (ELOOP, or EMLINK on BSD), a
# FIFO or socket, a folder, or a log whose permissions were taken away.
# Resource errors such as EMFILE are reported, never treated as tampering.
UNUSABLE_LOG_ERRORS = {
    errno.ELOOP, errno.EMLINK, errno.ENXIO, errno.EOPNOTSUPP,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP), errno.EISDIR, errno.EACCES, errno.EPERM,
}
# A Windows reparse point whose tag has this bit names another file: a
# symlink, a junction, or a similar link, which ``lstat`` does not follow.
# Other reparse points, such as cloud-sync placeholders and deduplicated
# files, open as the ordinary files they are and are used in place.
NAME_SURROGATE_TAG = 0x20000000


def linked_entry(info):
    """Whether ``lstat`` information describes a symlink, junction, or other Windows link."""

    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        and getattr(info, "st_reparse_tag", 0) & NAME_SURROGATE_TAG
    )


def open_run_log(path, flags, *, private=True):
    """Open a run log only if it is one regular file; return None otherwise.

    ``private`` restores owner-only access through the open descriptor.
    """

    if not NOFOLLOW:
        # Windows has no O_NOFOLLOW: refuse symlinks and junctions first.
        try:
            if linked_entry(os.lstat(path)):
                return None
        except FileNotFoundError:
            pass
    try:
        descriptor = os.open(path, flags | NOFOLLOW | NONBLOCK | BINARY, 0o600)
    except OSError as exc:
        if exc.errno in UNUSABLE_LOG_ERRORS:
            return None
        raise
    try:
        info = os.fstat(descriptor)
        # A second hard link could name a file outside the run folder.
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if private and stat.S_IMODE(info.st_mode) != 0o600 and hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    return None


def append_run_log(path, text):
    """Append to a private run log, replacing anything planted in its place.

    A link, FIFO, or hard link is unlinked; a folder, or a log that can no
    longer be opened, is moved aside so no record is lost. Return whether the
    entry had to be replaced.
    """

    path = Path(path)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    descriptor, replaced = open_run_log(path, flags), False
    for _ in range(3):
        if descriptor is not None:
            break
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            info = None
        if info is not None:
            keep = stat.S_ISDIR(info.st_mode) or (
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not linked_entry(info))
            if not keep:
                try:
                    os.unlink(path)
                except OSError:
                    keep = True  # Windows removes a directory link only as a folder.
            if keep:
                os.replace(path, path.with_name(f".{path.name}.{secrets.token_hex(4)}.replaced"))
        try:
            descriptor = os.open(path, flags | os.O_EXCL | NOFOLLOW | BINARY, 0o600)
        except FileExistsError:
            continue
        replaced = True
    if descriptor is None:
        raise OSError(f"Cannot replace the linked run log {path}.")
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(text)
    return replaced


def source_file_name(value=None):
    """Validate the optional name of the file a job's input came from (display only)."""

    if value is None:
        return ""
    if not isinstance(value, str) or "\0" in value or len(value) > MAX_SOURCE_FILE_CHARS:
        raise ValueError(
            f"A source file name must be text of at most {MAX_SOURCE_FILE_CHARS} characters."
        )
    return value.strip()


# The last prompts read successfully; job pages show them while a live YAML
# edit cannot be parsed. Launches always read the YAML again.
_last_default_prompts = {}


def default_prompts():
    """Read workflow prompts at use time, including edits made while serving."""

    global _last_default_prompts
    author_critic = runtime.builtin_workflow("author_critic")["prompts"]
    prompts = {
        "review": REVIEW_PROMPT,
        "author": author_critic["author"],
        "author_simple": author_critic["author_simple"],
        "critic": author_critic["critic"],
        "final": runtime.builtin_workflow("clean_up")["prompts"]["final"],
    }
    _last_default_prompts = prompts
    return dict(prompts)  # Callers adjust their copy.


def web_prompt_options(body, roles, source=None):
    """Accept only deliberate per-job edits; old browser defaults are ignored."""

    overrides = body.get("promptOverrides", {})
    if not isinstance(overrides, dict) or any(
        name not in {"review", "author", "author_simple", "critic", "final"}
        or not isinstance(value, str) for name, value in overrides.items()
    ):
        raise ValueError("Prompt overrides must map role names to prompt text.")
    managed = body.get("fileManagement", (source or {}).get("fileManagement", False))
    if source and managed != source.get("fileManagement", True):
        raise ValueError("Start a new job to change research file management.")
    return {
        f"{name}_prompt": overrides.get(
            "author_simple" if name == "author" and not managed else name,
            (source or {}).get(f"{name}Prompt"))
        for name in roles
    }


TRACE_LIMIT = 1500
PINNED_KINDS = {
    "request", "review_result", "critic_result", "author_result",
    "final_result", "failure_result", "partial_result", "diagnostic", "error",
}


def readable_record(record):
    """Accept a parsed transcript line whose routing fields, when set, are text."""

    # The author can write its run folder, transcript included; one odd value
    # must not stop a restart or a new job.
    return isinstance(record, dict) and all(
        isinstance(record.get(key), (str, type(None)))
        for key in ("kind", "stage", "node", "label")
    )


def retry_record(record):
    """Whether a request or diagnostic belongs to the third or a later attempt of one model call."""

    attempt = record.get("attempt")
    return isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 2


def important_record(record):
    """Recognize prompts and other milestones that must stay visible."""

    kind = record.get("kind")
    if not isinstance(kind, str):
        return False
    if kind in {"request", "diagnostic"} and retry_record(record):
        # A call retried every minute through a long outage would pin
        # thousands of identical records: its first retry stays pinned,
        # later ones remain in the recent window and the transcript.
        return False
    if kind in PINNED_KINDS:
        return True
    if (kind == "status"
            and record.get("label") == "Critic approved"
            and record.get("root") is not False
            and not record.get("activityLabel")):
        # A PASS can either trigger another review or accept the proof. Keep
        # the controller's exact acceptance reason after routine events age out.
        return True
    if kind == "research_audit":
        return record.get("status") != "working" or bool(record.get("recovered"))
    if kind != "codex_event" or record.get("root") is False:
        return False
    event = record.get("event")
    if not isinstance(event, dict):
        return False
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    item = params.get("item") or event.get("item")
    if not isinstance(item, dict):
        return False
    return (
        (event.get("method") or event.get("type")) in (
            "item/completed", "item.completed"
        )
        and item.get("type") in ("agentMessage", "agent_message")
    )

# UI labels describe the persistent author and independent verification.
PUBLIC_GRAPH = {
    "settings": {
        "model": DEFAULT_AUTHOR_MODEL,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "reasoning_efforts": list(EFFORTS),
        "reasoning_summaries": list(REASONING_SUMMARIES),
        "reasoning_summary": DEFAULT_REASONING_SUMMARY,
        "speeds": list(SPEEDS),
        "speed": DEFAULT_SPEED,
        "review_model": DEFAULT_REVIEW_MODEL,
        "review_models": list(OFFERED_MODELS),
        "models": list(OFFERED_MODELS),
        "model_efforts": {
            model: list(efforts) for model, efforts in runtime.MODEL_EFFORTS.items()
        },
        "research_audits": {**audits.default_settings(), "choices": audits.model_choices()},
        "review_reasoning_effort": DEFAULT_REVIEW_EFFORT,
        "revision_reasoning_effort": DEFAULT_REVIEW_EFFORT,
        "model_summary": "6.1 Sol/Max review · 6.1 Sol/Max author, critic, writer",
        "critic_rounds": {
            "default": DEFAULT_CRITIC_ROUNDS,
            "minimum": 1,
            "maximum": MAX_CRITIC_ROUNDS,
        },
        "thinking_hours": {
            "default": DEFAULT_THINKING_HOURS,
            "minimum": 0,
            "zeroMeansUnlimited": True,
            "maximum": MAX_THINKING_HOURS,
        },
    },
    "nodes": {
        "statement_reviewer": {
            "label": "Statement reviewer", "short_label": "Review",
            "stage": "review",
            "description": "Quickly makes the draft rigorous and checks obvious failures.",
        },
        "author": {
            "label": "Proof author", "short_label": "Author", "stage": "solve",
            "stages": ["solve", "repair"],
            "description": "The author explores approaches and works toward a complete, rigorous proof.",
        },
        "failure_summary": {
            "label": "Saved progress", "short_label": "Saved",
            "stage": "failure",
            "description": "The author's workspace and completed verification work remain available when a job stops.",
        },
        "critic": {
            "label": "Independent critic", "short_label": "Critic",
            "stage": "critic",
            "description": "One critic call uses fresh independent subagents, checks the proof, and fixes issues. Unfixable issues return to the author.",
        },
        "latex_editor": {
            "label": "LaTeX editor", "short_label": "Polish", "stage": "final",
            "description": "Polishes the passing proof into a complete LaTeX document.",
        },
        "latex_compile": {
            "label": "Compile PDF", "short_label": "Compile", "stage": "final",
            "description": "Compiles LaTeX, fixes compilation errors, and repeats until the source and PDF are ready.",
        },
        "latex_repair": {
            "label": "Fix LaTeX errors", "short_label": "Fix LaTeX", "stage": "final",
            "description": "Makes minor source changes using the compiler errors, then recompiles.",
        },
    },
    "edges": [
        {
            "from": "statement_reviewer", "to": "author",
            "label": "Approve statement", "when": "user approves",
            "prompt_change": "Insert the approved statement into the author prompt.",
        },
        {
            "from": "author", "to": "critic",
            "label": "Audit candidate", "when": "author writes or revises the proof",
            "prompt_change": "Send the latest candidate to a fresh critic.",
        },
        {
            "from": "author", "to": "failure_summary",
            "label": "Preserve author work", "when": "the author is interrupted or its time budget expires",
            "prompt_change": "Resume the same author workspace and saved session.",
        },
        {
            "from": "critic", "to": "critic",
            "label": "Recheck repair", "when": "critic fixes all bugs and the round limit is not reached",
            "prompt_change": "Send the repaired solution to a fresh critic call with new independent subagents.",
        },
        {
            "from": "critic", "to": "author",
            "label": "Return bugs", "when": "critic rejects",
            "prompt_change": "Send unresolved bugs back to the proof author.",
        },
        {
            "from": "critic", "to": "latex_editor",
            "label": "Format reviewed proof", "when": "critic passes without edits or passes with edits at the round limit",
            "prompt_change": "Send the independently reviewed solution to the LaTeX editor.",
        },
        {
            "from": "critic", "to": "failure_summary",
            "label": "Save verification checkpoint", "when": "the critic is interrupted or the time budget is reached",
            "prompt_change": "Keep the latest saved proof for continuation at the critic.",
        },
        {"from": "latex_editor", "to": "latex_compile", "label": "Compile LaTeX"},
        {"from": "latex_compile", "to": "latex_repair", "label": "Compilation failed"},
        {"from": "latex_repair", "to": "latex_compile", "label": "Retry compilation"},
    ],
}

DIRECT_GRAPH = {
    "settings": PUBLIC_GRAPH["settings"],
    "nodes": {
        name: node for name, node in PUBLIC_GRAPH["nodes"].items()
        if name != "statement_reviewer"
    },
    "edges": [
        edge for edge in PUBLIC_GRAPH["edges"]
        if edge["from"] != "statement_reviewer"
    ],
}

LATEX_GRAPH = {
    "settings": PUBLIC_GRAPH["settings"],
    "nodes": {
        "latex_editor": {
            **PUBLIC_GRAPH["nodes"]["latex_editor"],
            "description": "Polishes the supplied writing into a complete LaTeX document.",
        },
        "latex_compile": PUBLIC_GRAPH["nodes"]["latex_compile"],
        "latex_repair": PUBLIC_GRAPH["nodes"]["latex_repair"],
    },
    "edges": [edge for edge in PUBLIC_GRAPH["edges"] if edge["from"].startswith("latex_")],
}

REVIEW_ONLY_GRAPH = {
    "settings": PUBLIC_GRAPH["settings"],
    "nodes": {
        "statement_reviewer": {
            **PUBLIC_GRAPH["nodes"]["statement_reviewer"],
            "description": "Produces and saves a checked statement, then stops.",
        },
    },
    "edges": [],
}


def event_node(record, current=""):
    """Keep explicit node context across node-less events in a shared stage."""

    if record.get("node"):
        return record["node"]
    stage = record.get("stage")
    nodes = PUBLIC_GRAPH["nodes"]
    active = nodes.get(current, {})
    if stage in active.get("stages", [active.get("stage")]):
        return current
    return next((name for name, node in nodes.items()
                 if stage in node.get("stages", [node["stage"]])), current)


def direct_run_directory(path, runs):
    """Return a real direct child of ``runs`` without following a run symlink."""

    candidate, parent = Path(path), Path(runs).resolve()
    try:
        return (
            candidate.is_dir()
            and not candidate.is_symlink()
            and candidate.resolve().parent == parent
        )
    except OSError:
        return False


def validated_continuation_source(path, runs):
    """Resolve one run while refusing symlink escapes through its artifacts."""

    candidate = Path(path)
    if not direct_run_directory(candidate, runs):
        raise ValueError("The stopped source job is outside the runs directory.")
    return candidate.resolve()


def empty_state(trace=None, trace_version=0):
    """Return the complete, intentionally small UI state."""

    prompts = default_prompts()
    return {
        "phase": "input",
        "problemMode": "statement",
        "skipStatementReview": False,
        "sourceFile": "",
        "autoResumeAt": 0,
        # The runner's current wait before retrying a failed model call.
        "modelRetry": None,
        "statementReviewOnly": False,
        "fileManagement": False,
        "preparedRun": False,
        "launchHold": "",
        "retrievalPolicyPath": "",
        "retrievalMode": "live",
        "retrievalCutoffUtc": "",
        "retrieval": retrieval.public_retrieval_info(""),
        "retrievalCapabilities": retrieval.capabilities(),
        "draft": "",
        "reviewStatement": "",
        "reviewFeedback": "",
        "reviewInputRecorded": False,
        "finalInputReady": False,
        "modelOfComputation": "",
        "problemDescription": "",
        "goal": "",
        "goalThreadId": "",
        "goalWorkspace": "",
        "goalResume": False,
        "authorSteerDelivered": "",
        "latexInput": "",
        "review": None,
        "reviewModel": DEFAULT_REVIEW_MODEL,
        "authorModel": DEFAULT_AUTHOR_MODEL,
        "criticModel": DEFAULT_CRITIC_MODEL,
        "writerModel": DEFAULT_WRITER_MODEL,
        "reviewEffort": DEFAULT_REVIEW_EFFORT,
        "authorEffort": DEFAULT_REASONING_EFFORT,
        "criticEffort": DEFAULT_REASONING_EFFORT,
        "writerEffort": DEFAULT_REASONING_EFFORT,
        # Kept for old clients; new clients send one effort per role.
        "reasoningEffort": DEFAULT_REASONING_EFFORT,
        "reasoningSummary": DEFAULT_REASONING_SUMMARY,
        "speedMode": DEFAULT_SPEED,
        "reviewPrompt": prompts["review"],
        "authorPrompt": prompts["author_simple"],
        "criticPrompt": prompts["critic"],
        "finalPrompt": prompts["final"],
        "criticRounds": DEFAULT_CRITIC_ROUNDS,
        "thinkingHours": DEFAULT_THINKING_HOURS,
        "researchAudits": audits.default_settings(),
        "researchAuditProgress": {},
        "researchAuditStatus": {"runningSlots": [], "lastError": "", "warnings": []},
        "auditHoldingAuthor": False,
        "stage": "",
        "activeNode": "",
        "round": 0,
        "startedAt": "",
        "finishedAt": "",
        "lastActivityAt": "",
        "output": "",
        "resultKind": "",
        "error": "",
        "manuallyStopped": False,
        "stoppedStage": "",
        "settingsWarning": "",
        "runId": "",
        "checkpoints": [],
        "workflow": {**PUBLIC_GRAPH, "settings": {
            **PUBLIC_GRAPH["settings"], "prompts": prompts, **model_menu_settings(),
        }},
        "trace": trace if trace is not None else [],
        "traceVersion": trace_version,
    }


def restored_model(value):
    """Map discontinued saved model routes to their supported replacement."""

    return providers.replacement_for_removed(value) or value


def removed_model_warning(roles):
    """Name the saved roles whose removed model now runs as its replacement."""

    roles = set(roles)
    names = [ROLE_LABELS[role] for role in ROLES if role in roles]
    if not names:
        return ""
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{listed[0].upper()}{listed[1:]}: {providers.REMOVED_MODEL_WARNING}"


def model_menu_settings():
    """Role menu data, read per request: configured API models change while serving.

    The catalog holds no secrets. Efforts also cover the older ids that saved
    jobs may still name, although the menus no longer offer them.
    """

    catalog = providers.catalog()
    efforts = {model: list(levels) for model, levels in runtime.MODEL_EFFORTS.items()}
    efforts.update((row["id"], list(row["efforts"])) for row in catalog)
    return {"model_catalog": catalog, "model_efforts": efforts}


def model_choice_error(model, role):
    """Explain why one role's model id cannot run."""

    label = ROLE_LABELS[role]
    if providers.replacement_for_removed(model):
        return (f"DeepSeek is no longer supported; choose another model for the {label}, "
                f"for example {runtime.MODEL}.")
    if providers.is_api_id(model):
        return (f"The API model {model} chosen for the {label} is not configured. Add it under "
                "Advanced settings → API models, or choose another model.")
    shown = repr(model)[:80] if isinstance(model, str) and model else "(none)"
    return (f"Unknown model {shown} for the {label}. Choose {', '.join(OFFERED_MODELS)}, "
            "or an api:<id> model configured under Advanced settings → API models.")


def unconfigured_job_model_error(model, role, snapshot):
    """Why a saved job cannot launch its removed API model, and how to restore it.

    A job cannot change its models, so the only remedy is adding the model
    again: with the settings the job recorded (any display name gets this id
    back, see Server.save_api_model) or, without such a record, the display
    name that gives this id.
    """

    saved = saved_api_models(snapshot).get(model)
    if saved:
        kind = "Anthropic" if saved["format"] == providers.ANTHROPIC_FORMAT else "OpenAI"
        how = (f"with the same settings ({saved['label']} · {kind} format · {saved['baseUrl']} · "
               f"header {saved['headerName']} · model {saved['model']})")
    else:
        how = f"with the display name “{model[len(providers.API_ID_PREFIX):]}”"
    return (f"The API model {model} chosen for the {ROLE_LABELS[role]} is not configured. Add it again "
            f"under New problem → Advanced settings → API models {how} to continue this job.")


def require_known_models(models):
    """Reject an unknown, removed or unconfigured model for any role in ``models``."""

    for role, model in models.items():
        if not isinstance(model, str) or not providers.is_known(model):
            raise ValueError(model_choice_error(model, role))


class TransientLaunchError(ValueError):
    """A model cannot start right now for a reason that passes by itself (its CLI is updating)."""


def require_model_access(models):
    """Check locally that each model can start: a saved API key, or the Claude CLI."""

    try:
        for model in dict.fromkeys(models):
            runtime.verify_model_credentials(model)
    except runtime.CLIUnavailable as exc:
        raise TransientLaunchError(str(exc)) from exc
    except runtime.Error as exc:
        raise ValueError(str(exc)) from exc


API_SNAPSHOT_FIELDS = ("label", "format", "baseUrl", "headerName", "model")
# The add/edit form's choices; which CLI runs each format is fixed in providers.
API_MODEL_FORMATS = [
    {"id": providers.OPENAI_FORMAT, "label": "OpenAI-compatible (Responses API)", "cli": "Codex CLI",
     "headerName": providers.DEFAULT_HEADERS[providers.OPENAI_FORMAT]},
    {"id": providers.ANTHROPIC_FORMAT, "label": "Anthropic (Messages API)", "cli": "Claude CLI",
     "headerName": providers.DEFAULT_HEADERS[providers.ANTHROPIC_FORMAT]},
]


def api_model_snapshot(models):
    """Non-secret settings of the configured API models a job uses, kept for history.

    The runner always reads the private store itself; this copy is display data.
    """

    snapshot = {}
    for model in dict.fromkeys(models):
        entry = providers.api_model(model)
        if entry is not None:
            snapshot[model] = {key: entry[key] for key in API_SNAPSHOT_FIELDS}
    return snapshot


def saved_api_models(value):
    """Keep only a well-formed saved snapshot: the author can write its run folder."""

    if not isinstance(value, dict):
        return {}
    return {
        model: {key: entry[key] for key in API_SNAPSHOT_FIELDS}
        for model, entry in list(value.items())[:len(ROLES)]
        if providers.is_api_id(model) and isinstance(entry, dict) and all(
            isinstance(entry.get(key), str) and len(entry[key]) <= providers.MAX_BASE_URL_CHARS
            for key in API_SNAPSHOT_FIELDS)
    }


def _api_test_request(entry):
    """Path, headers and body of the one minimal request a Test sends."""

    path = urlsplit(entry["baseUrl"]).path.rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "TCS-Prover-API-model-test"}
    if entry["format"] == providers.OPENAI_FORMAT:
        # Codex sends Responses requests to <base URL>/responses.
        path += "/responses"
        body = {"model": entry["model"], "input": API_TEST_PROMPT,
                "max_output_tokens": API_TEST_MAX_TOKENS}
    else:
        # The Claude CLI takes the base URL without /v1 and adds /v1/messages.
        if path.lower().endswith("/v1"):
            path = path[:-3]
        path += "/v1/messages"
        body = {"model": entry["model"], "max_tokens": API_TEST_MAX_TOKENS,
                "messages": [{"role": "user", "content": API_TEST_PROMPT}]}
        headers["anthropic-version"] = ANTHROPIC_VERSION
    header, key = entry["headerName"], entry["apiKey"]
    headers = {name: value for name, value in headers.items() if name.lower() != header.lower()}
    headers[header] = f"Bearer {key}" if header.lower() == "authorization" else key
    return path, headers, json.dumps(body).encode("utf-8")


def _api_test_text(text, key):
    """One line of endpoint text without the key or any other known secret, clipped."""

    text = " ".join(str(text).split())
    # Remove the key before clipping, or a clip could leave part of it behind.
    for secret in sorted({key, json.dumps(key)[1:-1], quote(key, safe="")} - {""}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = runtime.redact(text)
    limit = providers.MAX_TEST_MESSAGE_CHARS
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _api_test_reply(value):
    """The reply text of a Responses or Messages result, if any."""

    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("output_text"), str) and value["output_text"].strip():
        return value["output_text"]
    # Responses: output[].content[].text; Messages: content[].text.
    items = value.get("output") if isinstance(value.get("output"), list) else []
    blocks = [block for item in items if isinstance(item, dict) and isinstance(item.get("content"), list)
              for block in item["content"]]
    if isinstance(value.get("content"), list):
        blocks += value["content"]
    return " ".join(block["text"] for block in blocks
                    if isinstance(block, dict) and isinstance(block.get("text"), str))


def _api_test_error(value, text):
    """The provider's own error message (OpenAI and Anthropic shapes), else the body."""

    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str) and error["message"].strip():
            return error["message"]
        for candidate in (error, value.get("message"), value.get("detail")):
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    return text


def _api_test_failure(exc, timeout):
    """Describe a transport failure by its class; its text may quote request data."""

    name = type(exc).__name__
    if isinstance(exc, TimeoutError):
        return f"No response within {timeout:g} seconds ({name})."
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"The TLS certificate could not be verified ({name})."
    if isinstance(exc, ssl.SSLError):
        return f"The TLS connection failed ({name})."
    if isinstance(exc, socket.gaierror):
        return f"The host name could not be resolved ({name})."
    if isinstance(exc, ConnectionRefusedError):
        return f"The connection was refused ({name})."
    if isinstance(exc, http.client.HTTPException):
        return f"The endpoint did not return a valid HTTP response ({name})."
    if isinstance(exc, UnicodeError):
        return f"The API key or header cannot be sent in an HTTP header ({name})."
    return f"The request failed ({name})."


def probe_api_endpoint(entry, timeout=None):
    """Send ONE minimal model request to an API model's endpoint and describe the result.

    ``entry`` holds validated settings including the key. The exchange has one
    overall deadline, follows no redirect, and the key travels only in the
    configured header; no returned text contains it.
    """

    timeout = API_TEST_TIMEOUT_SECONDS if timeout is None else timeout
    parts = urlsplit(entry["baseUrl"])
    result = {"ok": False, "status": None, "latencyMs": None, "message": "", "model": entry["model"],
              "testedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    started = time.monotonic()
    deadline, status, location, raw = started + timeout, None, "", b""
    connection = (
        http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout,
                                    context=ssl.create_default_context())
        if parts.scheme == "https" else
        http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    )
    try:
        path, headers, body = _api_test_request(entry)
        connection.request("POST", path or "/", body=body, headers=headers)
        # The response keeps reading through this socket after getresponse().
        channel = connection.sock

        def bound():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("API model test deadline")
            # http.client closes the socket itself once it has read the whole body of a
            # response that ends the connection (HTTP/1.0 or "Connection: close"); the
            # next read then returns b"", so there is nothing left to bound.
            if channel.fileno() != -1:
                channel.settimeout(left)

        bound()
        response = connection.getresponse()
        status, location = response.status, response.getheader("Location") or ""
        chunks, size = [], 0
        while size < API_TEST_MAX_RESPONSE_BYTES:
            bound()
            chunk = response.read(min(16384, API_TEST_MAX_RESPONSE_BYTES - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b"".join(chunks)
    except Exception as exc:  # Network, TLS, protocol or encoding failure.
        if status is None:
            result.update(latencyMs=round((time.monotonic() - started) * 1000),
                          message=_api_test_text(_api_test_failure(exc, timeout), entry["apiKey"]))
            return result
        # A body cut short still leaves a usable status line.
    finally:
        connection.close()
    result.update(status=status, latencyMs=round((time.monotonic() - started) * 1000))
    text = raw.decode("utf-8", "replace")
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if 200 <= status < 300:
        reply = _api_test_reply(value)
        reported = value.get("model") if isinstance(value, dict) else None
        result.update(ok=True, model=reported if isinstance(reported, str) and reported.strip() else entry["model"],
                      message=f"The model replied: {reply}" if reply.strip() else "The endpoint accepted the request.")
    elif 300 <= status < 400:
        result["message"] = (f"The endpoint redirected to {location or 'another URL'}; redirects are not "
                             "followed. Enter the final API base URL.")
    else:
        detail = _api_test_error(value, text).strip()
        result["message"] = detail or f"HTTP {status} without an error message."
    result["model"] = _api_test_text(result["model"], entry["apiKey"])
    result["message"] = _api_test_text(result["message"], entry["apiKey"])
    return result


def approach_metadata(name, content):
    """Extract graph labels from one node's own header and summary sections."""

    identity = Path(name).name.split("-", 1)[0].removesuffix(".md")
    fallback = Path(name).stem[len(identity):].lstrip("-").replace("-", " ") or identity
    # Metadata belongs before the first section, not in examples or later prose.
    header = re.split(r"(?m)^\s*##\s+", content, maxsplit=1)[0]
    title = re.search(r"(?m)^#\s+(.+?)\s*$", header)
    title = re.sub(r"^" + re.escape(identity) + r"\b[\s:—–-]*", "", title[1]) if title else fallback
    fields = {}
    for line in header.splitlines():
        match = re.match(r"^\s*(Parents?|Children|Status)\s*:\s*(.*?)\s*$", line.replace("**", ""), re.I)
        if match:
            key = match[1].lower()
            fields["parent" if key == "parents" else key] = match[2]
    parents = list(dict.fromkeys(re.findall(r"\bA\d{3,}\b", fields.get("parent", ""))))
    status = fields.get("status", "").strip().rstrip(".").upper()
    if status == "PARKED":  # Older research notes used this name.
        status = "BLOCKED"
    if status not in {"ACTIVE", "BLOCKED", "CLOSED", "RESOLVED"}:
        status = "UNSPECIFIED"
    sections = re.split(r"(?m)^##\s+(.+?)\s*$", content)
    summaries = {sections[i].strip().lower(): sections[i + 1].strip()
                 for i in range(1, len(sections) - 1, 2)}
    preferred = "obstacles" if status == "BLOCKED" else "conclusions" if status in {"CLOSED", "RESOLVED"} else "context and objective"
    summary = summaries.get(preferred) or summaries.get("context and objective") or ""
    summary = re.split(r"\n\s*\n", summary, maxsplit=1)[0].strip()
    if len(summary) > 1000:
        summary = summary[:997].rstrip() + "…"
    return {"id": identity, "file": name, "title": title or fallback,
            "parents": parents, "status": status, "result": summary,
            "hasParents": "parent" in fields}


class App:
    """Own one review-and-solve workflow."""

    def __init__(self, trace_file=None, runs=RUNS, output_stream=None, *, load_latest=True):
        # Tests may supply one fixed log; the real app uses one folder per run.
        self.runs, self.fixed_trace = Path(runs), trace_file is not None
        self.output_stream = output_stream
        # A new job creates its own run, so it need not parse another job's log.
        self.trace_file = Path(trace_file) if trace_file else (
            self._latest_trace() if load_latest else None
        )
        self.run_dir = self.trace_file.parent if self.trace_file else None
        trace, total, self.pinned = self._load_trace()
        self.state = empty_state(trace, total)
        self.state["runId"] = self.run_dir.name if self.run_dir else ""
        self.process = None
        self.active_token = None
        self.worker_token = None
        self.lock = threading.RLock()
        self.research_audits = None
        self._audit_token = None
        self._audit_pause_engine = None
        self._checkpoint_cache = []
        self._checkpoint_cache_signature = None
        self._approach_cache = {}
        # Start of the runner's current retry wait, which is not active time.
        self._retry_wait_started = None
        # The model calls now waiting to retry, by activity (parallel audits wait separately).
        self._model_retries = {}

    def _latest_trace(self):
        """Find the most recently changed run log, if one exists."""

        if not self.runs.exists():
            return None
        logs = (
            path for path in self.runs.glob("*/transcript.jsonl")
            if not path.is_symlink()
            and direct_run_directory(path.parent, self.runs)
        )

        def changed(path):
            try:
                return path.stat().st_mtime
            except OSError:  # Deleted concurrently.
                return -math.inf

        return max(logs, key=changed, default=None)

    def _new_run(self, statement, slug_source=None):
        """Create a private, readable folder name for one problem."""

        if self.research_audits is not None:
            self.research_audits.close()
            self.research_audits = None
            self._audit_token = None
        self._audit_pause_engine = None
        self.state["auditHoldingAuthor"] = False
        if self.fixed_trace:
            self.run_dir = self.trace_file.parent
        else:
            slug_text = statement if slug_source is None else slug_source
            slug = re.sub(r"[^a-z0-9]+", "-", slug_text.lower()).strip("-")
            slug = slug[:48].rstrip("-") or "problem"
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            prepare_runs_directory(self.runs)
            # Jobs started together (a folder batch, a terminal folder run, or
            # concurrent requests) can share a second and a slug. mkdir is the
            # atomic claim, so a name taken by another thread is skipped.
            number = 1
            while True:
                name = f"{stamp}_{slug}" if number == 1 else f"{stamp}_{slug}-{number}"
                try:
                    (self.runs / name).mkdir(mode=0o700)
                except FileExistsError:
                    number += 1
                    continue
                break
            self.run_dir = self.runs / name
            self.trace_file = self.run_dir / "transcript.jsonl"
        prepare_private_directory(self.run_dir)
        self._save("draft.md", f"# Draft problem\n\n{statement}\n")

    def _save(self, name, content):
        """Save one private, human-readable run artifact."""

        # The sandboxed author can write this folder. Never write through a
        # subfolder it replaced with a link, and replace the file itself (a
        # planted link is renamed over, never followed).
        folder = self.run_dir
        for part in Path(name).parts[:-1]:
            folder = folder / part
            try:
                info = folder.lstat()
            except FileNotFoundError:
                break
            if linked_entry(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError(f"Cannot save {name}: {part} is not a private run folder.")
        runtime._private_atomic_write(
            self.run_dir / name, content, staging_directory=self.run_dir.parent,
        )

    def _save_job_settings(self, options):
        """Persist restart-safe workflow settings without prompt duplication."""

        keys = (
            "reviewModel", "authorModel", "criticModel", "writerModel",
            "reasoningEffort", "reviewEffort", "authorEffort",
            "criticEffort", "writerEffort", "criticRounds",
            "thinkingHours", "speedMode", "reasoningSummary",
            "researchAudits", "researchAuditProgress",
            "problemMode", "skipStatementReview", "statementReviewOnly", "fileManagement", "preparedRun",
            "launchHold", "retrievalPolicyPath", "retrievalMode", "retrievalCutoffUtc",
            "sourceFile", "apiModels",
            "goalThreadId", "goalWorkspace", "goalResume", "authorSteerDelivered", "elapsedSeconds", "resumedAt",
        )
        # The solver rechecks historical settings while UI events persist
        # thread/steering state. Readers must never observe a truncated JSON.
        # Stage outside the writable workspace so a same-UID sandboxed author
        # cannot alter temporary contents before the controller installs them.
        runtime._private_atomic_write(
            self.run_dir / JOB_SETTINGS_FILENAME,
            json.dumps(
                {key: options[key] for key in keys if key in options},
                ensure_ascii=False, indent=2, sort_keys=True,
            ) + "\n",
            staging_directory=self.run_dir.parent,
        )

    def _prepare_continuation(
        self, source_run="", stopped_stage="", allow_external_source=False,
    ):
        """Record a continuation source without inspecting LLM-managed files."""

        if not source_run:
            return
        requested_source = Path(source_run).expanduser().absolute()
        source = validated_continuation_source(
            requested_source,
            requested_source.parent if allow_external_source else self.runs,
        )
        if source == self.run_dir.resolve():
            raise ValueError("A continuation must use a new run directory.")
        source_settings_path = source / JOB_SETTINGS_FILENAME
        if source_settings_path.exists():
            source_settings = json.loads(source_settings_path.read_text(encoding="utf-8"))
            if source_settings.get("retrievalPolicyPath") or source_settings.get("launchHold"):
                raise ValueError(
                    "Historical runs cannot be cloned into an unrestricted continuation. "
                    "Resume the same prepared workspace or prepare a new historical run with its policy."
                )
        self._save(
            CONTINUATION_SOURCE_FILENAME,
            json.dumps({
                "sourceRun": str(source),
                "sourceTranscript": str(source / "transcript.jsonl"),
                "stoppedStage": str(stopped_stage),
                "continuedAt": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _clear_manual_stop(self):
        """Let a completed terminal result win a concurrent stop request."""

        if self.run_dir:
            (self.run_dir / MANUAL_STOP_FILENAME).unlink(missing_ok=True)
        self.state.update(manuallyStopped=False, stoppedStage="")

    def _spawn_worker(self, target, args, token):
        """Track one reader until all buffered output and artifacts settle."""

        with self.lock:
            self.worker_token = token
        try:
            worker = threading.Thread(target=target, args=args, daemon=True)
            worker.start()
        except Exception:
            with self.lock:
                if self.worker_token is token:
                    self.worker_token = None
            raise
        self._start_research_audits(token)
        return worker

    def _start_research_audits(self, token, *, paused=False):
        """Monitor optional audits separately from the workflow reader."""

        with self.lock:
            if self.state.get("retrievalPolicyPath"):
                # This experiment retains the default of no scheduled auditors.
                # Do not start a second runner outside its role-specific policy.
                return
            if self.research_audits is not None and self._audit_pause_engine is self.research_audits:
                # Automatic resume keeps the same audit clock and monitor.
                self._audit_token = token
                return
            paused = paused and self.state["phase"] == "paused" and self.worker_token is None
            if (not self.state.get("fileManagement", True)
                    or (not paused and (token is None or self.worker_token is not token or self._audit_token is token))
                    or all(model == "none" for model in self.state["researchAudits"]["models"])):
                return
            workspace = self.state.get("goalWorkspace") or self.run_dir

            def record(event):
                with self.lock:
                    if self.research_audits is engine:
                        self.add_trace(event)
                        self.state["researchAuditProgress"] = engine.checkpoint()
                        self.state["researchAuditStatus"] = engine.status()
                        if event.get("status") in {"warning", "completed", "retrying"} or event.get("recovered"):
                            self._save_job_settings(self.state)

            try:
                engine = audits.ResearchAudits(workspace, on_event=record,
                                               progress=self.state["researchAuditProgress"],
                                               before_batch=self._pause_for_research_audit,
                                               after_batch=self._resume_after_research_audit)
            except Exception as exc:
                self.state["researchAuditStatus"] = {"runningSlots": [], "lastError": str(exc)}
                return
            if self.research_audits is not None:
                self.research_audits.close()
            self.research_audits = engine
            self._audit_token = token

        def monitor():
            checkpoint_at = 0
            tick = threading.Event()
            try:
                while True:
                    with self.lock:
                        if (engine.closed or self.research_audits is not engine
                                or (self.worker_token is not self._audit_token
                                    and self._audit_pause_engine is not engine)):
                            break
                        self._update_research_audits()
                        elapsed = self.state["researchAuditProgress"].get("elapsedSeconds", 0)
                        if elapsed - checkpoint_at >= 60:
                            self._save_job_settings(self.state)
                            checkpoint_at = elapsed
                    tick.wait(1)
            except (OSError, ValueError) as exc:
                record({"kind": "research_audit", "stage": "audit", "status": "error", "text": str(exc)})
            finally:
                engine.close()
                with self.lock:
                    if self.research_audits is engine:
                        self.state["researchAuditProgress"] = engine.checkpoint()
                        status = engine.status()
                        if (self.state["researchAuditStatus"].get("lastError")
                                and not self.state["researchAuditStatus"].get("warnings")):
                            status["lastError"] = self.state["researchAuditStatus"]["lastError"]
                        self.state["researchAuditStatus"] = status
                        # An audit batch releases its own pause after its cancelled
                        # workers finish, even when this monitor has failed.
                        if self._audit_pause_engine is not engine:
                            self.research_audits = None
                            self._audit_token = None
                            self.state["auditHoldingAuthor"] = False
                        if self.run_dir:
                            self._save_job_settings(self.state)

        try:
            threading.Thread(target=monitor, daemon=True).start()
            return engine
        except Exception as exc:
            engine.close()
            with self.lock:
                self.research_audits = None
                self._audit_token = None
                self.state["researchAuditStatus"] = {"runningSlots": [], "lastError": str(exc)}

    def _update_research_audits(self):
        """Tick author time; keep audits alive during their own temporary pause."""

        if self.research_audits is not None:
            if self.research_audits.closed:
                return
            active = (self.state["phase"] == "running" and self.state["activeNode"] == "author"
                      and self.state["stage"] in {"solve", "repair"})
            try:
                self.research_audits.update(
                    self.state["researchAudits"], active=active,
                    paused_for_audit=self._audit_pause_engine is self.research_audits,
                )
                self.state["researchAuditProgress"] = self.research_audits.checkpoint()
                self.state["researchAuditStatus"] = self.research_audits.status()
            except Exception as exc:
                self.research_audits.close()
                if self._audit_pause_engine is not self.research_audits:
                    self.research_audits = None
                    self._audit_token = None
                    self.state["auditHoldingAuthor"] = False
                self.state["researchAuditStatus"] = {"runningSlots": [], "lastError": str(exc)}

    def _pause_for_research_audit(self, engine):
        """Wait for the author reader to settle before any audit reads its files."""

        with self.lock:
            if (self.research_audits is not engine or self.state["phase"] not in {"running", "paused"}
                    or self.state["activeNode"] != "author" or self.state["stage"] not in {"solve", "repair"}):
                return False
            if self.state["phase"] == "paused":
                return self.worker_token is None and self._audit_pause_engine is engine
            self._audit_pause_engine = engine
            self.state["auditHoldingAuthor"] = True
            self.pause(_for_audit=True)
        tick = threading.Event()
        while True:
            with self.lock:
                if self.research_audits is not engine or self._audit_pause_engine is not engine:
                    return False
                if self.state["phase"] == "paused" and self.worker_token is None:
                    if self.state["activeNode"] != "author" or self.state["stage"] not in {"solve", "repair"}:
                        return False
                    if not self.state.get("goalThreadId"):
                        self.state["error"] = "No saved author conversation is available. Resume manually before auditing."
                    return not bool(self.state["error"])
                if self.state["phase"] != "pausing":
                    return False
            tick.wait(0.1)

    def _resume_after_research_audit(self, engine):
        """Resume only our own pause, after all audit reports have been published."""

        with self.lock:
            if self.research_audits is not engine or self._audit_pause_engine is not engine:
                return
            self.state["auditHoldingAuthor"] = False
            if engine.closed:
                self.state["researchAuditProgress"] = engine.checkpoint()
                self.research_audits = None
                self._audit_token = self._audit_pause_engine = None
            try:
                if (self.state["phase"] == "paused" and self.worker_token is None
                        and not self.state["error"]):
                    self.resume()
            except Exception as exc:
                self.state["error"] = f"Audits finished, but the author could not resume: {exc}"
                self.add_trace({"kind": "research_audit", "stage": "audit", "status": "error",
                                "text": self.state["error"]})
            finally:
                self._audit_pause_engine = None
                self._save_job_settings(self.state)

    def set_research_audits(self, settings):
        """Apply the three optional auditor choices without restarting research."""

        settings = audits.normalize_settings(settings)
        with self.lock:
            if not self.state.get("fileManagement", True):
                raise ValueError("Research audits require file management for this run.")
            if (self.state["phase"] not in {"running", "paused"}
                    or self.state["activeNode"] != "author"
                    or self.state["stage"] not in {"solve", "repair"}):
                raise ValueError("Research audits can be changed while the author is running or paused.")
            if (self.research_audits is None
                    and all(model == "none" for model in self.state["researchAudits"]["models"])):
                self.state["researchAuditProgress"] = {}
            self.state["researchAudits"] = settings
            self._update_research_audits()
            if self.research_audits is None:
                warnings = audits.selected_warnings(self.state["researchAuditProgress"].get("warnings"), settings)
                self.state["researchAuditProgress"]["warnings"] = warnings
                self.state["researchAuditStatus"] = {
                    "runningSlots": [], "warnings": warnings,
                    "lastError": "\n".join(item["message"] for item in warnings),
                }
            self._save_job_settings(self.state)
            self._start_research_audits(self.worker_token)

    def start_research_audit_now(self):
        """Run the saved auditors now and restart their interval clock."""

        self._check_launch_hold()

        with self.lock:
            if not self.state.get("fileManagement", True):
                raise ValueError("Research audits require file management for this run.")
            paused = self.state["phase"] == "paused"
            if (self.state["phase"] not in {"running", "paused"} or self.state["activeNode"] != "author"
                    or self.state["stage"] not in {"solve", "repair"}
                    or (self.worker_token is not None if paused else self.worker_token is None)
                    or self.state["manuallyStopped"] or self._audit_pause_engine is not None
                    or self.state["auditHoldingAuthor"]):
                raise ValueError("Start an audit while the author is running or fully paused.")
            settings = self.state["researchAudits"]
            if all(model == "none" for model in settings["models"]):
                raise ValueError("Select and save at least one auditor first.")
            if self.research_audits is not None and self.research_audits.status().get("batchActive"):
                raise ValueError("A research audit batch is already in progress.")
            if paused or self.research_audits is None:
                if self._start_research_audits(self.worker_token, paused=paused) is None:
                    raise ValueError("The research audit scheduler is unavailable.")
            if self.research_audits is None or self.research_audits.closed:
                raise ValueError("The research audit scheduler is unavailable.")
            previous_error = self.state["error"]
            if paused:
                # Reserve the paused workspace during preflight as well as the audit.
                self._audit_pause_engine = self.research_audits
                self.state.update(auditHoldingAuthor=True, error="")
            try:
                self.research_audits.update(settings, active=not paused, paused_for_audit=paused, start_now=True)
            except Exception:
                if paused:
                    self._audit_pause_engine = None
                    self.state.update(auditHoldingAuthor=False, error=previous_error)
                raise
            self.state["researchAuditProgress"] = self.research_audits.checkpoint()
            self.state["researchAuditStatus"] = self.research_audits.status()
            self._save_job_settings(self.state)

    def has_active_worker(self):
        """Return whether a model reader can still mutate this run."""

        with self.lock:
            return self.worker_token is not None

    def _finish_stopped_locked(self, token, message):
        """Publish a stopped job only after its matching reader has settled."""

        if self.worker_token is token:
            self.worker_token = None
        if self.active_token is token:
            self.active_token = None
        if self.state["phase"] == "stopping":
            self.process = None
            self.state.update(
                phase="done", error="Stopped.",
                finishedAt=datetime.now(timezone.utc).isoformat(),
            )
            self.state["output"] = self.state["output"] or message

    def _write_author_limit(self, hours):
        """Atomically publish the total workflow limit to the active solver."""

        self._save(AUTHOR_LIMIT_FILENAME, json.dumps({"hours": hours}))
        return self.run_dir / AUTHOR_LIMIT_FILENAME

    def _write_author_steer(self, instruction):
        """Atomically publish one live instruction to the active author."""

        command_id = secrets.token_hex(12)
        self._save(AUTHOR_STEER_FILENAME, json.dumps({
            "id": command_id,
            "instruction": instruction,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False))
        return command_id

    def _load_trace(self):
        """Restore the append-only local transcript after a restart."""

        if not self.trace_file:
            return [], 0, []
        try:
            descriptor = open_run_log(self.trace_file, os.O_RDONLY)
            if descriptor is None:
                return [], 0, []
            records, pinned, total = deque(maxlen=TRACE_LIMIT), [], 0
            with os.fdopen(descriptor, encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    total += 1
                    try:
                        if line.strip():
                            record = json.loads(line)
                            if not readable_record(record):
                                continue
                            records.append(record)
                            if important_record(record):
                                pinned.append(record)
                    except (ValueError, RecursionError):
                        continue
            return list(records), total, pinned
        except OSError:
            return [], 0, []

    def retained_trace(self):
        """Combine pinned milestones with the bounded recent-event window."""

        recent = self.state["trace"]
        recent_ids = {id(entry) for entry in recent}
        return [
            *(entry for entry in self.pinned if id(entry) not in recent_ids),
            *recent,
        ]

    def add_trace(self, record):
        """Timestamp, retain, and persist one transcript record."""

        # Put the receipt time last so a child cannot replace the audit timestamp.
        entry = {**record, "time": datetime.now(timezone.utc).isoformat()}
        with self.lock:
            # Secure the private log before writing its first sensitive byte.
            if not self.trace_file:
                raise OSError("No problem run is active.")
            # The run folder was made private once, when it was created.
            self.trace_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            replaced = append_run_log(
                self.trace_file, json.dumps(entry, ensure_ascii=False) + "\n",
            )
            self.state["trace"].append(entry)
            self.state["trace"][:] = self.state["trace"][-TRACE_LIMIT:]
            if important_record(entry):
                self.pinned.append(entry)
            self.state["traceVersion"] += 1
            self.state["lastActivityAt"] = entry["time"]
            if record.get("node"):
                self.state["activeNode"] = record["node"]
            if isinstance(record.get("round"), int):
                self.state["round"] = record["round"]
            if replaced and record.get("label") != "Transcript replaced":
                self.add_trace({
                    "kind": "diagnostic", "stage": self.state["stage"], "label": "Transcript replaced",
                    "text": "transcript.jsonl in the run folder had been replaced by a link "
                            "or another unusable entry. It was not followed; a new private "
                            "transcript continues from this record.",
                })

    @staticmethod
    def parse_line(line, stage):
        """Turn either tagged JSON or a diagnostic line into one record."""

        try:
            record = json.loads(line)
            if isinstance(record, dict):
                return record
        except json.JSONDecodeError:
            pass
        return {"kind": "diagnostic", "stage": stage, "text": line.rstrip()}

    @staticmethod
    def failure_text(record):
        """Extract one safe user-facing failure from a tagged child record."""

        if not isinstance(record, dict):
            return ""
        if record.get("kind") == "diagnostic":
            value = record.get("text", "")
            if isinstance(value, str) and value.startswith("error: "):
                return value[7:].strip()
            return ""
        event = record.get("event")
        if not isinstance(event, dict):
            return ""
        value = event.get("message") if event.get("type") == "error" else ""
        if event.get("type") == "turn.failed":
            failure = event.get("error")
            value = (
                failure.get("message", "")
                if isinstance(failure, dict) else failure
            )
        return value.strip() if isinstance(value, str) else ""

    def memory_file(self, name, version="", *, include_files=True, listing_only=False):
        """Display one author-owned file on demand, without modifying it."""

        approach = name in {"APPROACHES", "APPROACHES.md"} or bool(APPROACH_MEMORY_FILE.fullmatch(name))
        audit = name in {"AUDITS", "audit_history", "audit.md"} or bool(AUDIT_MEMORY_FILE.fullmatch(name))
        audit_folder = "audit_history" if name in {"audit_history", "audit.md"} or name.startswith("audit_history/") else "AUDITS"
        if name not in RESEARCH_MEMORY_FILES and not approach and not audit:
            raise ValueError("Unknown research memory file.")
        with self.lock:
            directory = self.state.get("goalWorkspace") or self.run_dir
        result = {"name": name, "status": "missing"}
        if not directory:
            return result
        directory = Path(directory).expanduser().resolve()
        result["workspace"] = directory.name
        try:
            if os.name == "nt":
                return self._memory_file_portable(directory, name, version, result, approach,
                                                  include_files=include_files, listing_only=listing_only)
            # Pin directories while reading so a concurrent rename or symlink swap
            # cannot redirect the viewer outside the selected author's workspace.
            with ExitStack() as opened:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                parent = os.open(directory, flags)
                opened.callback(os.close, parent)
                filename = name
                if approach:
                    result["files"] = []
                    try:
                        folder = os.open("APPROACHES", flags, dir_fd=parent)
                    except FileNotFoundError:
                        if name not in {"APPROACHES", "APPROACHES.md"}:
                            return result
                        filename = "APPROACHES.md"
                        result["name"] = filename
                        if listing_only:
                            try:
                                if stat.S_ISREG(os.stat(filename, dir_fd=parent, follow_symlinks=False).st_mode):
                                    result["files"] = [filename]
                            except FileNotFoundError:
                                pass
                    else:
                        opened.callback(os.close, folder)
                        if not include_files and APPROACH_MEMORY_FILE.fullmatch(name):
                            # Read a known node without rescanning the whole directory.
                            parent, filename = folder, name.split("/")[1]
                        else:
                            self._list_approach_files(folder, parent, result, listing_only)
                            index = next((Path(item).name for item in result["files"]
                                          if Path(item).name in {"index.md", "INDEX.md"}), "INDEX.md")
                            if name != "APPROACHES.md":
                                parent = folder
                                filename = index if name == "APPROACHES" else name.split("/")[1]
                                result["name"] = f"APPROACHES/{filename}"
                    if listing_only:
                        return {**result, "status": "ready"}
                elif audit:
                    result["files"] = []
                    try:
                        folder = os.open(audit_folder, flags, dir_fd=parent)
                    except FileNotFoundError:
                        folder = None
                    else:
                        opened.callback(os.close, folder)
                        entries = []
                        for entry in os.listdir(folder):
                            if not AUDIT_MEMORY_FILE.fullmatch(f"{audit_folder}/{entry}"):
                                continue
                            try:
                                if stat.S_ISREG(os.stat(entry, dir_fd=folder, follow_symlinks=False).st_mode):
                                    entries.append(f"{audit_folder}/{entry}")
                            except FileNotFoundError:
                                continue
                        result["files"] = sorted(entries, reverse=True)
                    try:
                        if audit_folder == "audit_history" and stat.S_ISREG(os.stat("audit.md", dir_fd=parent, follow_symlinks=False).st_mode):
                            result["files"].append("audit.md")
                    except FileNotFoundError:
                        pass
                    if name in {"AUDITS", "audit_history"}:
                        if not result["files"]:
                            return result
                        name = result["files"][0]
                    result["name"] = name
                    filename = name
                    if name.startswith(("AUDITS/", "audit_history/")):
                        if folder is None:
                            return result
                        parent, filename = folder, name.split("/")[1]
                descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        return {**result, "status": "unavailable"}
                    current_version = f"{directory}:{result['name']}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"
                    result.update(status="ready", version=current_version,
                                  modifiedAt=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat())
                    if current_version == version:
                        return {**result, "unchanged": True}
                    return {**result, "content": source.read()}
        except FileNotFoundError:
            return {**result, "status": "missing"}
        except (OSError, UnicodeError):
            return {**result, "status": "unavailable"}

    @staticmethod
    def _list_approach_files(folder, parent, result, existing_only):
        names = os.listdir(folder)
        index = "index.md" if "index.md" in names else "INDEX.md"
        entries = []
        for entry in names:
            relative = f"APPROACHES/{entry}"
            if not APPROACH_MEMORY_FILE.fullmatch(relative):
                continue
            try:
                if stat.S_ISREG(os.stat(entry, dir_fd=folder, follow_symlinks=False).st_mode):
                    entries.append(relative)
            except FileNotFoundError:
                continue
        index_path = f"APPROACHES/{index}"
        result["files"] = ([index_path] if not existing_only or index_path in entries else []) + sorted(
            entry for entry in entries if Path(entry).name not in {"index.md", "INDEX.md"})
        try:
            if stat.S_ISREG(os.stat("APPROACHES.md", dir_fd=parent, follow_symlinks=False).st_mode):
                result["files"].append("APPROACHES.md")
        except FileNotFoundError:
            pass

    def approach_graph(self):
        """Read node metadata independently of the optional index; cache unchanged files."""

        with self.lock:
            listing = self.memory_file("APPROACHES", listing_only=True)
            files = listing.get("files", [])
            cache, nodes = {}, []
            for name in files:
                if not re.fullmatch(r"APPROACHES/A\d{3,}(?:-[A-Za-z0-9_-]+)?\.md", name):
                    continue
                previous = self._approach_cache.get(name, {})
                document = self.memory_file(name, previous.get("version", ""), include_files=False)
                if document.get("unchanged"):
                    cached = previous
                else:
                    node = approach_metadata(name, document.get("content", ""))
                    node["readStatus"] = document["status"]
                    # Failed reads must be retried even if the file's stat is unchanged.
                    cached = {"version": document.get("version", "") if document["status"] == "ready" else "", "node": node}
                cache[name] = cached
                nodes.append(cached["node"])
            self._approach_cache = cache
            return {"nodes": nodes, "files": files, "status": listing["status"],
                    "indexFile": next((name for name in files if Path(name).name in {"index.md", "INDEX.md"}), "")}

    @staticmethod
    def _memory_file_portable(directory, name, version, result, approach, *, include_files=True, listing_only=False):
        """Keep the read-only viewer available where directory descriptors are unsupported."""

        def linked(path):
            return path.is_symlink() or getattr(path, "is_junction", lambda: False)()

        path = directory / name
        if approach:
            folder = directory / "APPROACHES"
            result["files"] = []
            if linked(folder):
                return {**result, "status": "unavailable"}
            if folder.exists():
                if not folder.is_dir():
                    return {**result, "status": "unavailable"}
                entries = list(folder.iterdir()) if include_files else []
                index = "index.md" if any(entry.name == "index.md" for entry in entries) else "INDEX.md"
                index_path = folder / index
                result["files"] = ([f"APPROACHES/{index}"] if not listing_only or (not linked(index_path) and index_path.is_file()) else []) + sorted(
                    f"APPROACHES/{entry.name}" for entry in entries
                    if entry.name not in {"INDEX.md", "index.md"} and APPROACH_MEMORY_FILE.fullmatch(f"APPROACHES/{entry.name}")
                    and not linked(entry) and entry.is_file()
                )
                legacy = directory / "APPROACHES.md"
                if not linked(legacy) and legacy.is_file():
                    result["files"].append("APPROACHES.md")
                name = f"APPROACHES/{index}" if name == "APPROACHES" else name
            elif name == "APPROACHES":
                name = "APPROACHES.md"
                if listing_only and not linked(directory / name) and (directory / name).is_file():
                    result["files"] = [name]
            result["name"] = name
            path = directory / name
            if listing_only:
                return {**result, "status": "ready"}
        elif name in {"AUDITS", "audit_history", "audit.md"} or AUDIT_MEMORY_FILE.fullmatch(name):
            audit_folder = "audit_history" if name in {"audit_history", "audit.md"} or name.startswith("audit_history/") else "AUDITS"
            folder = directory / audit_folder
            result["files"] = []
            if linked(folder) or (folder.exists() and not folder.is_dir()):
                return {**result, "status": "unavailable"}
            if folder.exists():
                result["files"] = sorted((f"{audit_folder}/{entry.name}" for entry in folder.iterdir()
                                          if AUDIT_MEMORY_FILE.fullmatch(f"{audit_folder}/{entry.name}")
                                          and not linked(entry) and entry.is_file()), reverse=True)
            legacy = directory / "audit.md"
            if audit_folder == "audit_history" and not linked(legacy) and legacy.is_file():
                result["files"].append("audit.md")
            if name in {"AUDITS", "audit_history"}:
                if not result["files"]:
                    return result
                name = result["files"][0]
            result["name"] = name
            path = directory / name
        if linked(path) or path.resolve() != path:
            return {**result, "status": "unavailable"}
        if not path.is_file():
            return result
        info = path.stat()
        current_version = f"{directory}:{name}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"
        result.update(status="ready", version=current_version,
                      modifiedAt=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat())
        if current_version == version:
            return {**result, "unchanged": True}
        content = path.read_text(encoding="utf-8")
        if linked(path) or path.resolve() != path:
            return {**result, "status": "unavailable"}
        return {**result, "content": content}

    def snapshot(self, after=None):
        """Copy state, optionally returning only newly appended records."""

        with self.lock:
            state = dict(self.state)
            run_dir = self.run_dir
            state["workerActive"] = self.has_active_worker()
            state["processAlive"] = bool(self.process is not None and self.process.poll() is None)
            state["activeElapsedSeconds"] = self._elapsed_seconds()
            try:
                state["statement"] = saved_statement(self.run_dir) if self.run_dir else ""
            except ValueError:
                state["statement"] = ""
            graph = state["workflow"]
            # A typo in a live YAML edit must not break job pages or report a
            # completed action as failed. Launches still read the YAML strictly.
            try:
                prompts = default_prompts()
                include_transcript = state["phase"] not in {
                    "reviewing", "running", "stopping", "pausing",
                }
                signature = checkpoint_artifact_signature(
                    self.run_dir, include_transcript=include_transcript,
                )
                if signature != self._checkpoint_cache_signature:
                    self._checkpoint_cache = (
                        saved_run_checkpoints(self.run_dir) if self.run_dir else []
                    )
                    self._checkpoint_cache_signature = signature
                state["workflowWarning"] = ""
            except (ValueError, KeyError) as exc:
                prompts = dict(_last_default_prompts or graph["settings"].get("prompts", {}))
                state["workflowWarning"] = f"The workflow YAML cannot be read: {exc}"
            state["workflow"] = {
                **graph, "settings": {**graph["settings"], "prompts": prompts, **model_menu_settings()},
            }
            state["checkpoints"] = list(self._checkpoint_cache)
            state["canDownloadTex"] = bool(
                state["phase"] == "done" and not self.has_active_worker()
                and self.run_dir and (self.run_dir / "final.tex").is_file()
                and not (self.run_dir / "final.tex").is_symlink()
            )
            state["canDownloadPdf"] = bool(
                state["canDownloadTex"] and (self.run_dir / "final.pdf").is_file()
                and not (self.run_dir / "final.pdf").is_symlink()
                and (self.run_dir / "final.pdf").stat().st_size > 0
            )
            trace, version = self.state["trace"], self.state["traceVersion"]
            first = version - len(trace)
            if isinstance(after, int) and first <= after <= version:
                state["trace"], state["traceFrom"] = list(trace[after - first:]), after
            else:
                state["trace"], state["traceFrom"] = self.retained_trace(), first
        # Policy metadata can hash the pinned Codex binary: never hold the job
        # lock (and its transcript reader) meanwhile. A policy that no longer
        # loads still shows the cutoff saved with the job.
        state["retrieval"] = retrieval.public_retrieval_info(
            state.get("retrievalPolicyPath"), run_dir=run_dir,
            mode=state.get("retrievalMode", "live"),
            saved_cutoff=state.get("retrievalCutoffUtc", ""))
        state["retrievalCapabilities"] = retrieval.capabilities()
        if state["retrieval"]["mode"] == "historical":
            state["retrievalMode"] = "historical"
            state["retrievalCutoffUtc"] = state["retrieval"]["cutoffExclusiveUtc"]
        return state

    @staticmethod
    def _workflow_options(
        critic_rounds=DEFAULT_CRITIC_ROUNDS,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        author_effort=None, critic_effort=None, writer_effort=None,
        author_prompt=None, critic_prompt=None, final_prompt=None,
        review_model=DEFAULT_REVIEW_MODEL, review_effort=None,
        review_prompt=None, include_review=True,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        research_audits=None,
        file_management=True,
        retrieval_mode="live", retrieval_cutoff_utc="",
    ):
        """Normalize and validate settings shared by both input modes."""

        if not isinstance(file_management, bool):
            raise ValueError("File management must be enabled or disabled.")
        review_model = str(review_model or "")
        author_model = str(author_model or "")
        critic_model = str(critic_model or "")
        writer_model = str(writer_model or "")
        reasoning_effort = str(reasoning_effort or "")
        review_effort = str(review_effort or DEFAULT_REVIEW_EFFORT)
        author_effort = str(author_effort or reasoning_effort)
        critic_effort = str(critic_effort or reasoning_effort)
        writer_effort = str(writer_effort or reasoning_effort)
        speed_mode = str(speed_mode or "")
        reasoning_summary = str(reasoning_summary or "")
        supplied_prompts = {
            "review": review_prompt, "author": author_prompt,
            "critic": critic_prompt, "final": final_prompt,
        }
        defaults = default_prompts()
        if not file_management:
            defaults["author"] = defaults["author_simple"]
        prompts = {
            name: str(
                defaults[name] if value is None else value
            ).strip()
            for name, value in supplied_prompts.items()
        }
        role_models = {"author": author_model, "critic": critic_model, "writer": writer_model}
        if include_review:
            role_models = {"review": review_model, **role_models}
        # Built-in, older Codex, or configured API ids; the store is read now.
        require_known_models(role_models)
        efforts = [author_effort, critic_effort, writer_effort]
        if include_review:
            efforts.append(review_effort)
        if any(effort not in EFFORTS for effort in efforts):
            raise ValueError("Choose a valid reasoning effort for every role.")
        selected_models = list(role_models.values())
        if include_review:
            review_effort = runtime.effective_effort(review_model, review_effort)
        author_effort = runtime.effective_effort(author_model, author_effort)
        critic_effort = runtime.effective_effort(critic_model, critic_effort)
        writer_effort = runtime.effective_effort(writer_model, writer_effort)
        if speed_mode not in SPEEDS:
            raise ValueError("Choose Standard or Fast speed.")
        if reasoning_summary not in REASONING_SUMMARIES:
            raise ValueError(
                "Choose Status only, Concise summaries, or Detailed summaries."
            )
        required_prompts = [prompts[name] for name in ("author", "critic", "final")]
        if include_review:
            required_prompts.append(prompts["review"])
        if any(not prompt for prompt in required_prompts):
            raise ValueError("Every role prompt must contain instructions.")
        if prompts["author"].count(runtime.MARKER) != 1:
            raise ValueError(
                f"The author prompt must contain exactly one {runtime.MARKER}."
            )
        try:
            critic_rounds = runtime.critic_limit(critic_rounds)
        except runtime.Error as exc:
            raise ValueError(str(exc)) from exc
        try:
            thinking_hours = float(thinking_hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("The total workflow time limit must be a number.") from exc
        if not math.isfinite(thinking_hours) or not 0 <= thinking_hours <= MAX_THINKING_HOURS:
            raise ValueError(
                f"Choose 0 for unlimited, or at most {MAX_THINKING_HOURS} hours."
            )
        audit_options = audits.normalize_settings(research_audits) if file_management else {
            **audits.default_settings(), "models": ["none", "none", "none"]}
        # Web cutoff timestamps need Codex models: say so before any CLI check.
        search_options = retrieval.validate_options(
            retrieval_mode, retrieval_cutoff_utc, models=selected_models,
            research_audits=audit_options)
        require_model_access(selected_models)
        api_models = api_model_snapshot(selected_models)
        return {
            **search_options,
            **({"apiModels": api_models} if api_models else {}),
            "reviewModel": review_model if include_review else DEFAULT_REVIEW_MODEL,
            "authorModel": author_model,
            "criticModel": critic_model,
            "writerModel": writer_model,
            "reasoningEffort": author_effort,
            "reviewEffort": (
                review_effort if include_review else DEFAULT_REVIEW_EFFORT
            ),
            "authorEffort": author_effort,
            "criticEffort": critic_effort,
            "writerEffort": writer_effort,
            "reviewPrompt": (
                prompts["review"] if include_review else defaults["review"]
            ),
            "authorPrompt": prompts["author"],
            "criticPrompt": prompts["critic"],
            "finalPrompt": prompts["final"],
            "criticRounds": critic_rounds,
            "thinkingHours": thinking_hours,
            "speedMode": speed_mode,
            "reasoningSummary": reasoning_summary,
            "fileManagement": file_management,
            "researchAudits": audit_options,
        }

    def start_review(
        self, statement, feedback="", critic_rounds=DEFAULT_CRITIC_ROUNDS,
        review_model=DEFAULT_REVIEW_MODEL,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        review_effort=None, author_effort=None,
        critic_effort=None, writer_effort=None,
        review_prompt=None, author_prompt=None,
        critic_prompt=None, final_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        review_only=False,
        continuation_source="", stopped_stage="",
        research_audits=None,
        file_management=True,
        retrieval_mode="live", retrieval_cutoff_utc="",
        revise_existing=False,
    ):
        """Start the review and return immediately so the page can poll.

        ``revise_existing`` means the request names this job: it may only retry
        a reviewed or failed review in place, never start another run.
        """

        self._check_input_replacement()

        statement = str(statement).strip()
        feedback = str(feedback or "").strip()
        if "\0" in statement or "\0" in feedback:
            raise ValueError("Statement review input cannot contain NUL characters.")
        if not isinstance(review_only, bool):
            raise ValueError("Statement review only must be enabled or disabled.")
        if review_only:
            # Proof-stage settings are irrelevant to this terminal workflow.
            author_model = DEFAULT_AUTHOR_MODEL
            critic_model = DEFAULT_CRITIC_MODEL
            writer_model = DEFAULT_WRITER_MODEL
            author_effort = DEFAULT_REASONING_EFFORT
            critic_effort = DEFAULT_REASONING_EFFORT
            writer_effort = DEFAULT_REASONING_EFFORT
            author_prompt = critic_prompt = final_prompt = None
            critic_rounds = DEFAULT_CRITIC_ROUNDS
            thinking_hours = DEFAULT_THINKING_HOURS
        options = self._workflow_options(
            critic_rounds=critic_rounds,
            thinking_hours=thinking_hours,
            author_model=author_model,
            critic_model=critic_model,
            writer_model=writer_model,
            reasoning_effort=reasoning_effort,
            author_effort=author_effort,
            critic_effort=critic_effort,
            writer_effort=writer_effort,
            author_prompt=author_prompt,
            critic_prompt=critic_prompt,
            final_prompt=final_prompt,
            review_model=review_model,
            review_effort=review_effort,
            review_prompt=review_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            research_audits=research_audits if not review_only else None,
            file_management=file_management,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
        )
        if options["retrievalMode"] == "historical":
            raise ValueError("Historical search currently requires Skip statement review. Start a direct proof.")
        if continuation_source:
            options.update(retrieval.continuation_options(continuation_source))
        elif self.run_dir and self.state["phase"] == "reviewed":
            saved_search = retrieval.continuation_options(self.run_dir)
            if any(options[key] != value for key, value in saved_search.items()):
                raise ValueError("A statement revision keeps this run's search settings. Start a new job to change them.")
        if not statement:
            raise ValueError("Enter a problem statement.")
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("A model is already working on this job.")
            # Feedback retries, and retries of a failed review, stay in the
            # selected job's problem folder; one job keeps one run id.
            failed_retry = revise_existing and self.state["phase"] == "input"
            retry = self.run_dir is not None and (
                self.state["phase"] == "reviewed" or failed_retry
            )
            if revise_existing and not retry:
                raise ValueError("Start a new problem from the home screen.")
            if failed_retry:
                self._save("draft.md", f"# Draft problem\n\n{statement}\n")
            if not retry:
                self._new_run(statement)
                if not self.fixed_trace:
                    self.pinned = []
                self._prepare_continuation(
                    continuation_source, stopped_stage,
                )
            self._save(
                REVIEW_INPUT_FILENAME,
                json.dumps({
                    "schemaVersion": 1,
                    "statement": statement,
                    "feedback": feedback,
                }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            prompt_names = ("review",) if review_only else (
                "review", "author", "critic", "final"
            )
            for name in prompt_names:
                self._save(f"prompts/{name}.txt", options[f"{name}Prompt"] + "\n")
            self._save_job_settings({
                **options,
                "problemMode": "statement",
                "skipStatementReview": False,
                "statementReviewOnly": review_only,
            })
            trace = self.state["trace"] if retry or self.fixed_trace else []
            version = self.state["traceVersion"] if retry or self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "phase": "reviewing",
                "draft": statement,
                "reviewStatement": statement,
                "reviewFeedback": feedback,
                "reviewInputRecorded": True,
                "statementReviewOnly": review_only,
                **options,
                "workflow": REVIEW_ONLY_GRAPH if review_only else PUBLIC_GRAPH,
                "activeNode": "statement_reviewer",
                "stage": "review",
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            token = self.active_token = object()
            self.worker_token = token
        self._spawn_worker(
            self._review,
            (
                statement, feedback, options["reviewModel"],
                options["reviewEffort"], token,
            ),
            token,
        )

    def _review(
        self, statement, feedback="", review_model=DEFAULT_REVIEW_MODEL,
        reasoning_effort=DEFAULT_REVIEW_EFFORT, token=None,
    ):
        """Stream the review transcript and keep its final structured result."""

        process, report, problem = None, None, ""
        failure_detail, visible = "", []
        # A stop click can arrive before this background thread starts.
        with self.lock:
            if self.active_token is not token:
                if self.worker_token is token:
                    self.worker_token = None
                return
            if self.state["phase"] == "stopping":
                self._finish_stopped_locked(
                    token,
                    "The reviewer was stopped before it produced a checked statement.",
                )
                return
        try:
            process = subprocess.Popen(
                [
                    sys.executable, "-u", str(ROOT / "web_ui.py"), "--review-worker",
                    "--review-model", review_model,
                    "--review-effort", reasoning_effort,
                    "--reasoning-summary", self.state["reasoningSummary"],
                    "--speed", self.state["speedMode"],
                    "--review-prompt-file",
                    str(self.run_dir / "prompts/review.txt"),
                    "--thinking-hours", str(self.state["thinkingHours"]),
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=self.run_dir,
                env=self._retrieval_child_environment(),
                **isolated_process_options(),
            )
            with self.lock:
                stop_now = (
                    self.active_token is not token
                    or self.state["phase"] == "stopping"
                )
                if not stop_now:
                    self.process = process
            if stop_now:
                stop_process_tree(process)
                with self.lock:
                    self._finish_stopped_locked(
                        token,
                        "The reviewer was stopped before it produced a checked statement.",
                    )
                return
            process.stdin.write(statement + ("\0" + feedback if feedback else ""))
            process.stdin.close()
            for line in process.stdout:
                record = self.parse_line(line, "review")
                self.add_trace(record)
                self._follow_model_retry(record)
                failure = self.failure_text(record)
                if failure and not failure.startswith("Reconnecting..."):
                    # Prefer the precise provider error over the controller's
                    # later generic nonzero-exit diagnostic.
                    if record.get("kind") == "codex_event" or not failure_detail:
                        failure_detail = failure
                if record.get("kind") == "review_result":
                    report = record.get("review")
                event = record.get("event") or {}
                if not isinstance(event, dict):
                    continue
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    continue
                if event.get("type") == "item.completed" and item.get("type") in {
                    "reasoning", "agent_message"
                }:
                    summary = item.get("summary") or []
                    summary = summary if isinstance(summary, list) else [summary]
                    value = item.get("text") or "\n".join(map(str, summary))
                    if value:
                        value = str(value)
                        visible.append(
                            ("Reasoning summary:\n" if item["type"] == "reasoning" else "")
                            + value
                        )
                        with self.lock:
                            self.state["output"] = "\n\n".join(visible)
            code = process.wait()
            if code or not isinstance(report, dict) or not all(
                isinstance(report.get(key), str) for key in ("statement", "notes")
            ):
                problem = (
                    f"Review failed: {failure_detail}"
                    if failure_detail
                    else "Review failed. See the transcript for details."
                )
        except (OSError, TypeError, AttributeError) as exc:
            problem = str(exc)
        finally:
            # Reap a child even if a quick stop breaks its stdin or transcript.
            try:
                if process and process.poll() is None:
                    stop_process_tree(process)
            except OSError:
                pass
        with self.lock:
            valid = isinstance(report, dict) and all(
                isinstance(report.get(key), str) for key in ("statement", "notes")
            )
            if self.active_token is not token and not (
                valid and self.state.get("manuallyStopped")
            ):
                if self.worker_token is token:
                    self.worker_token = None
                return
            if self.process is process:
                self.process = None
            # A reviewer that ended while waiting to retry: that wait was not active time.
            self._end_retry_wait_locked()
            self._model_retries = {}
            self.state["modelRetry"] = None
            stopped = self.state["phase"] == "stopping"
            if valid:
                self._clear_manual_stop()
                self._save(
                    "checked-statement.md",
                    f"# Checked statement\n\n{report['statement']}\n\n"
                    f"# Reviewer notes\n\n{report['notes'] or 'None.'}\n",
                )
                # A completed review wins a stop/exit race. Review-only jobs
                # finish here and cannot enter the proof-author pipeline.
                review_only = self.state.get("statementReviewOnly", False)
                self.state.update(
                    phase="done" if review_only else "reviewed",
                    resultKind="review" if review_only else "",
                    review=report,
                    error="",
                    output=report["statement"] if review_only else "",
                    finishedAt=(
                        datetime.now(timezone.utc).isoformat()
                        if review_only else ""
                    ),
                )
            elif stopped:
                self._finish_stopped_locked(
                    token,
                    "The reviewer was stopped before it produced a checked statement.",
                )
            elif problem:
                self.state["phase"], self.state["error"] = "input", problem
            if self.active_token is token:
                self.active_token = None
            if self.worker_token is token:
                self.worker_token = None

    def _retrieval_child_environment(self):
        """Bind each worker, including independent reviewers, to its saved mode."""
        child_environment = os.environ.copy()
        # Never let one parallel job inherit another job's retrieval policy.
        child_environment.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        child_environment["TCS_PROVER_RETRIEVAL_MODE"] = self.state.get("retrievalMode", "live")
        if self.state.get("retrievalPolicyPath"):
            child_environment["TCS_PROVER_RETRIEVAL_POLICY"] = self.state["retrievalPolicyPath"]
        return child_environment

    def _launch_workflow_locked(self, workflows, source, options, stage, node, state=None):
        """Launch YAML graphs in this job, preserving worker ownership on errors."""

        self._check_launch_hold()
        child_environment = self._retrieval_child_environment()
        token = self.active_token = object()
        if state is not None:
            self._save("workflow-input.json", json.dumps(state, ensure_ascii=False))
            options = [*options, "--state-file", str(self.run_dir / "workflow-input.json")]
        process = subprocess.Popen(
            [
                sys.executable, "-u", str(ROOT / "workflow_runner.py"),
                *(str(WORKFLOWS / name) for name in workflows), *options,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, cwd=self.run_dir,
            env=child_environment,
            **isolated_process_options(),
        )
        self.worker_token = token
        self.process = process
        try:
            process.stdin.write(source)
            process.stdin.close()
        except (OSError, TypeError, ValueError):
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                if process.poll() is None:
                    stop_process_tree(process)
            except OSError:
                pass
            if self.process is process:
                self.process = None
            if self.active_token is token:
                self.active_token = None
            if self.worker_token is token:
                self.worker_token = None
            raise
        self.state.update(
            phase="running", stage=stage, activeNode=node, round=0, output="", resultKind="",
        )
        return process, token

    def _proof_options_locked(self):
        """Resolve the same proof settings for new and recovered candidates."""

        author_limit_file = self._write_author_limit(self.state["thinkingHours"])
        author_steer_file = self.run_dir / AUTHOR_STEER_FILENAME
        if not self.state.get("goalResume"):
            author_steer_file.unlink(missing_ok=True)
        options = [
            "--critic-rounds", str(self.state["criticRounds"]),
            "--thinking-hours", str(self.state["thinkingHours"]),
            "--reasoning-effort", self.state["reasoningEffort"],
            "--reasoning-summary", self.state["reasoningSummary"],
            "--speed", self.state["speedMode"],
            "--author-limit-file", str(author_limit_file),
            "--author-steer-file", str(author_steer_file),
            "--set", "file_management=" + json.dumps(self.state.get("fileManagement", True)),
        ]
        for role in ("author", "critic", "writer"):
            options.extend([
                f"--{role}-model", self.state[f"{role}Model"],
                f"--{role}-effort", self.state[f"{role}Effort"],
            ])
        for role in ("author", "critic", "final"):
            options.extend([
                f"--{role}-prompt-file", str(self.run_dir / f"prompts/{role}.txt"),
            ])
        if self.state.get("goalThreadId"):
            options.extend(["--set", "goal_thread_id=" + json.dumps(self.state["goalThreadId"])])
        if self.state.get("goalWorkspace"):
            options.extend(["--set", "goal_cwd=" + json.dumps(self.state["goalWorkspace"])])
        if self.state.get("goalResume"):
            options.extend(["--set", "goal_resume=true"])
            if self.state.get("authorSteerDelivered"):
                options.extend(["--set", "author_steer_delivered=" + json.dumps(self.state["authorSteerDelivered"])])
        options.extend(["--set", "goal_pause_file=" + json.dumps(str(self.run_dir / PAUSE_REQUEST_FILENAME))])
        return options

    def _launch_solver_locked(self, statement):
        """Start the author/critic graph followed by final cleanup."""

        return self._launch_workflow_locked(
            ["author_critic.yaml", "clean_up.yaml"], statement,
            [*self._proof_options_locked(), "--elapsed-seconds", str(self._elapsed_seconds())],
            "solve", "author",
        )

    def _elapsed_seconds(self):
        """Count active time across pauses without charging the paused interval."""

        if self.state["phase"] == "paused" and self.run_dir:
            checkpoint = read_pause_object(self.run_dir)
            try:
                if checkpoint.get("status") in {"paused", "pausing"}:
                    return float(checkpoint["elapsedSeconds"])
            except (ValueError, KeyError, TypeError):
                pass
        try:
            started_at = datetime.fromisoformat(self.state.get("resumedAt") or self.state["startedAt"])
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            end = datetime.now(timezone.utc)
            if self.state["phase"] not in {"reviewing", "running", "stopping", "pausing"}:
                end = datetime.fromisoformat(self.state.get("finishedAt") or self.state.get("lastActivityAt") or self.state["startedAt"])
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
            elapsed_seconds = max(0.0, (end - started_at).total_seconds())
            waiting = getattr(self, "_retry_wait_started", None)
            if waiting is not None and self.state["phase"] in ACTIVE_PHASES:
                # A model call waiting to retry does not consume the budget.
                elapsed_seconds = max(0.0, elapsed_seconds - max(0.0, (end - waiting).total_seconds()))
        except (KeyError, TypeError, ValueError):
            elapsed_seconds = 0.0
        return float(self.state.get("elapsedSeconds", 0)) + elapsed_seconds

    def _follow_model_retry(self, record):
        """Mirror the runner's retry waits: shown on the job page, never charged as active time.

        The runner reports budgetClock "paused" when every model call in
        progress waits to retry and "running" when one runs again; each
        finished wait moves the start of the active segment forward.
        """

        if not isinstance(record, dict) or record.get("kind") != "status":
            return
        change = record.get("budgetClock")
        label = record.get("label")
        retrying = isinstance(label, str) and label.endswith(runtime.RETRY_LABEL)
        if change not in {"paused", "running"} and not retrying:
            return
        with self.lock:
            now = datetime.now(timezone.utc)
            if change == "paused" and self._retry_wait_started is None:
                self._retry_wait_started = now
            elif change == "running":
                self._end_retry_wait_locked(now)
            if retrying:
                # Parallel requests wait and retry separately: one that retries
                # now must not hide another that still waits.
                key = str(record.get("activityLabel") or record.get("node") or record.get("stage") or "")
                retry_at = record.get("retryAt")
                waits = getattr(self, "_model_retries", None)
                if waits is None:
                    waits = self._model_retries = {}
                waits.pop(key, None)
                if isinstance(retry_at, (int, float)) and not isinstance(retry_at, bool) and math.isfinite(retry_at):
                    waits[key] = {
                        "text": str(record.get("text") or ""), "retryAt": float(retry_at),
                        "attempt": record.get("retryAttempt") if isinstance(record.get("retryAttempt"), int) else None,
                        "stage": record.get("stage") if isinstance(record.get("stage"), str) else "",
                    }
                # The latest wait that has not ended yet, if any.
                self.state["modelRetry"] = next(reversed(waits.values()), None)

    def _end_retry_wait_locked(self, now=None):
        """Leave a finished retry wait out of the current segment's active time."""

        started, self._retry_wait_started = self._retry_wait_started, None
        if started is None:
            return
        waited = max(0.0, ((now or datetime.now(timezone.utc)) - started).total_seconds())
        segment = self.state.get("resumedAt") or self.state.get("startedAt")
        if not waited or not segment:
            return
        try:
            start = datetime.fromisoformat(segment)
        except (TypeError, ValueError):
            return
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self.state["resumedAt"] = (start + timedelta(seconds=waited)).isoformat()
        if self.run_dir is not None:
            try:
                self._save_job_settings(self.state)
            except (OSError, UnicodeError, TypeError, ValueError):
                pass  # The in-memory segment still excludes the wait.

    def _save_pause(self, checkpoint):
        """Atomically store controller state; research notebooks belong to the LLM."""

        self._save(PAUSE_FILENAME, json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n")

    def _configure_auto_resume(self, checkpoint, now=None):
        """Persist the retry deadline and distinguish errors from deliberate pauses."""

        now = time.time() if now is None else now
        if not checkpoint.get("pauseCause"):
            # Legacy checkpoints: deliberate pauses left a pause-request file;
            # failed runs recorded an error instead.
            checkpoint["pauseCause"] = (
                "error" if checkpoint.get("error")
                and not (self.run_dir / PAUSE_REQUEST_FILENAME).exists() else "user"
            )
        exhausted = (
            str(checkpoint.get("error", "")).startswith(WORKFLOW_TIME_LIMIT_ERROR)
            or (float(self.state["thinkingHours"]) > 0 and
                float(checkpoint.get("elapsedSeconds", 0))
                >= float(self.state["thinkingHours"]) * 3600)
        )
        # A conversation the CLI no longer has cannot be resumed by a retry, and
        # an author stopped by a safety check waits for the user.
        retry = (checkpoint["pauseCause"] == "error" and not exhausted
                 and checkpoint.get("sessionUnavailable") is not True
                 and checkpoint.get("safetyStop") is not True)
        deadline = checkpoint.get("autoResumeAt", 0)
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= 0:
            deadline = now + AUTO_RESUME_SECONDS
        checkpoint["autoResumeAt"] = deadline if retry else 0
        self.state["autoResumeAt"] = checkpoint["autoResumeAt"]

    def auto_resume_if_due(self, resume, now=None):
        """Retry one failed pause under the same lock as manual pause/resume."""

        now = time.time() if now is None else now
        with self.lock:
            deadline = self.state.get("autoResumeAt", 0)
            if (self.state["phase"] != "paused" or not deadline or now < deadline
                    or self.has_active_worker() or self.state.get("auditHoldingAuthor")
                    or self.state.get("manuallyStopped")):
                return False
            try:
                checkpoint = load_pause_checkpoint(self.run_dir)
            except (OSError, ValueError) as exc:
                # A missing or damaged checkpoint stops neither this job's
                # retries nor anyone else's: report it, then look again later.
                self.defer_auto_resume(exc, now)
                return True
            self._configure_auto_resume(checkpoint, now)
            if not self.state["autoResumeAt"]:
                self._save_pause(checkpoint)
                return False
            # Reserve the next attempt before launching. A failed launch, or a
            # server restart during it, must not cause retries every polling tick.
            checkpoint["autoResumeAt"] = now + AUTO_RESUME_SECONDS
            self.state["autoResumeAt"] = checkpoint["autoResumeAt"]
            self._save_pause(checkpoint)
            self.add_trace({"kind": "status", "stage": self.state["stage"],
                            "label": "Automatic resume", "text": "Retrying after an error pause."})
            try:
                self._check_launch_models_locked()
            except TransientLaunchError:
                pass  # For example a CLI updating itself: resume() reports it and the next try is reserved.
            except ValueError as exc:
                # A model that is not configured (or has no key) is a settings
                # error, not a transient one: wait for an explicit Resume.
                checkpoint["autoResumeAt"] = self.state["autoResumeAt"] = 0
                self._save_pause(checkpoint)
                self.add_trace({"kind": "diagnostic", "stage": self.state["stage"],
                                "text": f"Automatic resume stopped: {exc} Choose Resume after fixing the settings."})
                return True
            try:
                resume()
            except (OSError, ValueError, RuntimeError) as exc:
                self.add_trace({"kind": "diagnostic", "stage": self.state["stage"],
                                "text": f"Automatic resume failed: {exc}. Retrying in {retry_interval_text()}."})
            return True

    def defer_auto_resume(self, problem, now=None):
        """Report a due automatic resume that could not run; the next try follows after the usual wait."""

        now = time.time() if now is None else now
        with self.lock:
            if self.state["phase"] != "paused" or not self.state.get("autoResumeAt"):
                return
            # Reserve first: the report itself can fail, for example on a full disk.
            self.state["autoResumeAt"] = now + AUTO_RESUME_SECONDS
            try:
                self.add_trace({"kind": "diagnostic", "stage": self.state["stage"],
                                "text": f"Automatic resume failed: {problem}. Retrying in {retry_interval_text()}."})
            except OSError:
                pass

    def _author_checkpoint_state(self):
        """Recover controller input if the author exits before emitting its state."""

        checkpoint_state = {"statement": saved_statement(self.run_dir)}
        report_record = next((record for record in reversed(self.retained_trace())
                              if record.get("kind") == "critic_result"), None)
        if report_record and report_record.get("report", {}).get("verdict") == "reject":
            checkpoint_state.update(report=report_record["report"],
                                    solution=report_record["report"]["solution"],
                                    round=report_record.get("round", self.state["round"]))
        return checkpoint_state

    def pause(self, *, _for_audit=False):
        """Interrupt the author while retaining its native session for resume."""

        with self.lock:
            if not _for_audit and self.state["phase"] in {"pausing", "paused"}:
                # Audit cancellation is also valid before a checkpoint exists. A damaged
                # checkpoint is left for Resume to report, but its automatic retry stops.
                checkpoint = read_pause_object(self.run_dir)
                if checkpoint:
                    checkpoint.update(pauseCause="user", autoResumeAt=0)
                    self._save_pause(checkpoint)
                self.state["autoResumeAt"] = 0
            if not _for_audit and self._audit_pause_engine is not None:
                # A human pause cancels the audits and their automatic resume.
                self._audit_pause_engine = None
                self.state["auditHoldingAuthor"] = False
                self._update_research_audits()
                if self.state["phase"] in {"pausing", "paused"}:
                    return
            if self.state["phase"] in {"pausing", "paused"}:
                return
            if (self.state["phase"] != "running" or self.state["activeNode"] != "author"
                    or self.state["stage"] not in {"solve", "repair"}):
                raise ValueError("Pause is available while the proof author is working.")
            checkpoint_state = self._author_checkpoint_state()
            self._save_pause({"status": "pausing", "node": "author", "state": checkpoint_state,
                              "pauseCause": "audit" if _for_audit else "user", "autoResumeAt": 0,
                              "elapsedSeconds": self._elapsed_seconds(), "stage": self.state["stage"],
                              "goalThreadId": self.state.get("goalThreadId", "")})
            self._save(PAUSE_REQUEST_FILENAME, "{}\n")
            self.state.update(phase="pausing", error="", autoResumeAt=0)
            self._update_research_audits()
            self._save_job_settings(self.state)
            self.add_trace({"kind": "status", "stage": self.state["stage"], "node": "author",
                            "label": "Paused for research audits" if _for_audit else "Pause requested",
                            "text": ("Preserving the author conversation; it will resume after the audit reports are saved."
                                     if _for_audit else "Interrupting the author and preserving its saved conversation for Resume.")})
            process, token = self.process, self.active_token

        def finish_if_unresponsive():
            # Also covers workers started before pause support was installed.
            if process is None:
                return
            try:
                process.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                pass
            with self.lock:
                if self.active_token is not token or self.state["phase"] != "pausing":
                    return
                self.add_trace({"kind": "diagnostic", "stage": self.state["stage"],
                                "text": "The author did not acknowledge the pause; closing its process. Resume will reopen the saved conversation."})
            stop_process_tree(process)

        threading.Thread(target=finish_if_unresponsive, daemon=True).start()

    def _check_launch_hold(self):
        """A held run must fail before credentials, workers or elapsed clocks."""
        if self.state.get("launchHold"):
            raise ValueError("Prepared run is held: " + str(self.state["launchHold"]))

    def _check_launch_models_locked(self, roles=("author", "critic", "writer")):
        """Recheck saved role models before a launch from an idle job.

        API models, their keys and the Claude CLI can change while a job waits,
        so fail with a clear message instead of a runner error.
        """

        models = {role: self.state[f"{role}Model"] for role in roles}
        for role, model in models.items():
            if providers.is_api_id(model) and not providers.is_known(model):
                raise ValueError(unconfigured_job_model_error(model, role, self.state.get("apiModels")))
        require_known_models(models)
        if (self.state.get("retrievalMode") == "historical" or self.state.get("retrievalPolicyPath")) and any(
                providers.provider_kind(model) != providers.CODEX for model in models.values()):
            raise ValueError(runtime.HISTORICAL_CODEX_ONLY)
        require_model_access(models.values())
        snapshot = api_model_snapshot(models.values())
        if snapshot:
            self.state["apiModels"] = {**saved_api_models(self.state.get("apiModels")), **snapshot}

    def _check_input_replacement(self):
        """A reused UI job must not erase its historical policy or hold."""
        self._check_launch_hold()
        if self.state.get("retrievalPolicyPath"):
            raise ValueError("Historical run inputs and policy are fixed. Create a separate new job instead.")

    def start_prepared_run(self):
        """Start a curated workspace in place, with no inherited author session."""

        with self.lock:
            self._check_launch_hold()
            if self.state["phase"] != "prepared" or self.has_active_worker():
                raise ValueError("This workspace is no longer waiting to start.")
            statement = saved_statement(self.run_dir)
            self._check_launch_models_locked()
            for role in ("author", "critic", "final"):
                self._save(f"prompts/{role}.txt", self.state[f"{role}Prompt"] + "\n")
            previous = dict(self.state)
            now = datetime.now(timezone.utc).isoformat()
            self.state.update(preparedRun=False, goalResume=False, goalThreadId="",
                              goalWorkspace=str(self.run_dir.resolve()), authorSteerDelivered="",
                              elapsedSeconds=0, startedAt=now, resumedAt=now, finishedAt="",
                              researchAuditProgress={}, error="")
            process = None
            try:
                self._save_job_settings(self.state)
                process, token = self._launch_solver_locked(statement)
                self._spawn_worker(self._read_output, (process, token), token)
            except (OSError, ValueError, RuntimeError):
                if process is not None:
                    stop_process_tree(process)
                self.state = previous
                self.process = self.active_token = self.worker_token = None
                self._save_job_settings(self.state)
                raise
            self.add_trace({"kind": "status", "stage": "solve", "node": "author",
                            "label": "Prepared research started",
                            "text": "Started a fresh author session in the prepared workspace."})

    def resume(self):
        """Reopen this run with a new CLI process and the saved goal/thread."""

        self._check_launch_hold()

        with self.lock:
            if self.state.get("auditHoldingAuthor"):
                raise ValueError("Wait for the audits to finish, or choose Pause to cancel them before resuming.")
            if self.state["phase"] != "paused" or self.has_active_worker():
                raise ValueError("Wait until the author is paused before resuming.")
            self._check_launch_models_locked()
            checkpoint = load_pause_checkpoint(self.run_dir)
            workflow_state = dict(checkpoint["state"])
            workflow_state.pop("paused", None)
            workflow_state.pop("failed", None)
            previous = dict(self.state)
            if checkpoint.get("goalThreadId"):
                self.state["goalThreadId"] = checkpoint["goalThreadId"]
            self.state.update(goalResume=True, elapsedSeconds=checkpoint["elapsedSeconds"],
                              resumedAt=datetime.now(timezone.utc).isoformat(), finishedAt="", error="", autoResumeAt=0,
                              sessionUnavailable=False, safetyStop=False)
            (self.run_dir / PAUSE_REQUEST_FILENAME).unlink(missing_ok=True)
            process = None
            try:
                self._save_pause({**checkpoint, "status": "running", "error": "",
                                  "pauseCause": "", "autoResumeAt": 0, "sessionUnavailable": False,
                                  "safetyStop": False})
                self._save_job_settings(self.state)
                resume_options = (["--set", "goal_require_resume=true"]
                                  if self.state.get("goalThreadId") else [])
                process, token = self._launch_workflow_locked(
                    ["author_critic.yaml", "clean_up.yaml"], "",
                    [*self._proof_options_locked(), *resume_options,
                     "--elapsed-seconds", str(checkpoint["elapsedSeconds"]),
                     "--start-node", checkpoint["node"]],
                    checkpoint["stage"], checkpoint["node"], state=workflow_state,
                )
                self._spawn_worker(self._read_output, (process, token), token)
            except (OSError, ValueError, RuntimeError):
                if process is not None:
                    stop_process_tree(process)
                self.state = previous
                self.process = self.active_token = self.worker_token = None
                self._save_pause(checkpoint)
                self._save_job_settings(self.state)
                raise
            self.add_trace({"kind": "status", "stage": checkpoint["stage"], "node": checkpoint["node"],
                            "label": "Resume requested", "text": "Reopening the saved author conversation in this same run folder."})

    def final_tex(self):
        """Read the finished LaTeX artifact for an authenticated download."""

        with self.lock:
            if not self.snapshot()["canDownloadTex"]:
                raise ValueError("A completed final.tex is not available for this job.")
            return (self.run_dir / "final.tex").read_bytes()

    def final_pdf(self):
        """Read the compiled PDF for an authenticated download."""

        with self.lock:
            if not self.snapshot()["canDownloadPdf"]:
                raise ValueError("A completed final.pdf is not available for this job.")
            return (self.run_dir / "final.pdf").read_bytes()

    def _final_options(self):
        return [
            "--writer-model", self.state["writerModel"],
            "--critic-model", self.state["criticModel"],
            "--reasoning-effort", self.state["reasoningEffort"],
            "--writer-effort", self.state["writerEffort"],
            "--critic-effort", self.state["criticEffort"],
            "--reasoning-summary", self.state["reasoningSummary"],
            "--speed", self.state["speedMode"],
            "--final-prompt-file", str(self.run_dir / "prompts/final.txt"),
        ]

    def _launch_final_locked(self, source):
        """Run cleanup on a combined theorem and proof."""

        return self._launch_workflow_locked(
            ["clean_up.yaml"], source, self._final_options(), "final", "latex_editor",
        )

    def _launch_saved_final_locked(self, statement, solution):
        """Run cleanup with the exact saved statement and accepted candidate."""

        return self._launch_workflow_locked(
            ["clean_up.yaml"], "", self._final_options(), "final", "latex_editor",
            state={"statement": statement, "solution": solution},
        )

    def _launch_critic_resume_locked(self, statement, solution):
        """Resume the same graph at its critic with the saved candidate."""

        return self._launch_workflow_locked(
            ["author_critic.yaml", "clean_up.yaml"], "",
            [*self._proof_options_locked(), "--start-node", "critic"],
            "critic", "critic", state={"statement": statement, "solution": solution},
        )


    def start_direct_statement(
        self, statement,
        critic_rounds=DEFAULT_CRITIC_ROUNDS,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        author_effort=None, critic_effort=None, writer_effort=None,
        author_prompt=None, critic_prompt=None, final_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        continuation_source="", stopped_stage="",
        allow_external_source=False,
        research_audits=None,
        file_management=True,
        source_file=None,
        retrieval_mode="live", retrieval_cutoff_utc="",
        _prepare_only=False,
    ):
        """Send a statement directly to the proof author without review."""

        self._check_input_replacement()

        source_file = source_file_name(source_file)
        statement = str(statement).strip()
        if not statement:
            raise ValueError("Enter a problem statement.")
        if "\0" in statement:
            raise ValueError("The problem statement cannot contain NUL characters.")
        if continuation_source:
            file_management = saved_file_management(continuation_source)
            saved_prompt = Path(continuation_source) / "prompts" / "author.txt"
            if saved_prompt.is_file():
                original_template = read_utf8(saved_prompt, "saved author prompt")
                if author_prompt is not None and str(author_prompt).strip() != original_template.strip():
                    raise ValueError("A continued author must keep its original author prompt; start a new job to change it.")
                author_prompt = original_template
        options = self._workflow_options(
            critic_rounds=critic_rounds,
            thinking_hours=thinking_hours,
            author_model=author_model,
            critic_model=critic_model,
            writer_model=writer_model,
            reasoning_effort=reasoning_effort,
            author_effort=author_effort,
            critic_effort=critic_effort,
            writer_effort=writer_effort,
            author_prompt=author_prompt,
            critic_prompt=critic_prompt,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            include_review=False,
            research_audits=research_audits,
            file_management=file_management,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
        )
        options["sourceFile"] = source_file
        if options["retrievalMode"] == "historical" and continuation_source:
            raise ValueError("Historical search cannot be added to an existing conversation. Start a new direct proof.")
        if continuation_source:
            options.update(retrieval.continuation_options(continuation_source))
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("A model is already working on this job.")
            self._new_run(statement)
            if not self.fixed_trace:
                self.pinned = []
            self._prepare_continuation(
                continuation_source, stopped_stage,
                allow_external_source=allow_external_source,
            )
            if continuation_source:
                options.update(saved_goal_options(continuation_source))
            for name in ("author", "critic", "final"):
                self._save(f"prompts/{name}.txt", options[f"{name}Prompt"] + "\n")
            if options["retrievalMode"] == "historical":
                self._save("input.md", statement + "\n")
                options["retrievalPolicyPath"] = retrieval.prepare_policy(
                    self.run_dir, options["retrievalCutoffUtc"])
                options["goalWorkspace"] = str(self.run_dir.resolve())
            self._save_job_settings({
                **options,
                "problemMode": "statement",
                "skipStatementReview": True,
                "statementReviewOnly": False,
            })
            self._save(
                "checked-statement.md",
                "# Statement sent directly to the proof author\n\n"
                f"{statement}\n\n# Statement review\n\nSkipped by the user.\n",
            )
            if options["retrievalMode"] == "historical" and not _prepare_only:
                retrieval.preflight_policy(options["retrievalPolicyPath"])
                retrieval.preflight_compiler(options["retrievalPolicyPath"])
            trace = self.state["trace"] if self.fixed_trace else []
            version = self.state["traceVersion"] if self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "problemMode": "statement",
                "skipStatementReview": True,
                "draft": statement,
                **options,
                "workflow": DIRECT_GRAPH,
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            if _prepare_only:
                self.state.update(phase="prepared", preparedRun=True,
                                  stage="solve", activeNode="author", startedAt="",
                                  launchHold="Historical batch preflight is pending; every run must pass before launch.")
                self._save_job_settings(self.state)
                return
            process, token = self._launch_solver_locked(statement)
        self._spawn_worker(self._read_output, (process, token), token)

    def start_latex_only(
        self, source,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        writer_effort=None, final_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        continuation_source="", stopped_stage="",
        retrieval_mode="live", retrieval_cutoff_utc="",
    ):
        """Polish one combined theorem-and-proof input without earlier stages."""

        self._check_input_replacement()

        source = str(source).strip()
        if not source:
            raise ValueError("Enter the theorem and proof.")
        if "\0" in source:
            raise ValueError("The theorem and proof cannot contain NUL characters.")
        options = self._workflow_options(
            writer_model=writer_model,
            reasoning_effort=reasoning_effort,
            writer_effort=writer_effort,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            include_review=False,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
        )
        if options["retrievalMode"] == "historical":
            raise ValueError("Historical search is available only for a new direct proof author.")
        if continuation_source:
            options.update(retrieval.continuation_options(continuation_source))
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("A model is already working on this job.")
            self._new_run(source)
            if not self.fixed_trace:
                self.pinned = []
            self._prepare_continuation(
                continuation_source, stopped_stage,
            )
            self._save("prompts/final.txt", options["finalPrompt"] + "\n")
            self._save_job_settings({
                **options,
                "problemMode": "latex",
                "skipStatementReview": False,
                "statementReviewOnly": False,
            })
            self._save("latex-input.md", source + "\n")
            trace = self.state["trace"] if self.fixed_trace else []
            version = self.state["traceVersion"] if self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "problemMode": "latex",
                "draft": source,
                "latexInput": source,
                **options,
                "workflow": LATEX_GRAPH,
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            process, token = self._launch_final_locked(source)
        self._spawn_worker(self._read_output, (process, token), token)

    def start_final_resume(
        self, statement, solution,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        writer_effort=None, final_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        continuation_source="", stopped_stage="final",
        critic_model=DEFAULT_CRITIC_MODEL, critic_effort=None,
        retrieval_mode="live", retrieval_cutoff_utc="",
    ):
        """Retry normal finalization with its exact statement/proof contract."""

        self._check_input_replacement()

        statement, solution = str(statement).strip(), str(solution).strip()
        if not statement or not solution:
            raise ValueError("A saved statement and clean proof are required.")
        if "\0" in statement or "\0" in solution:
            raise ValueError("Saved final input cannot contain NUL characters.")
        options = self._workflow_options(
            writer_model=writer_model,
            critic_model=critic_model,
            critic_effort=critic_effort,
            reasoning_effort=reasoning_effort,
            writer_effort=writer_effort,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            include_review=False,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
        )
        if options["retrievalMode"] == "historical":
            raise ValueError("Historical search cannot be added to a saved finalization. Resume the original run.")
        if continuation_source:
            options.update(retrieval.continuation_options(continuation_source))
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("A model is already working on this job.")
            source_label = (
                Path(continuation_source).name
                if continuation_source else "saved-final"
            )
            self._new_run(statement, f"final-resume-{source_label}")
            if not self.fixed_trace:
                self.pinned = []
            self._prepare_continuation(
                continuation_source, stopped_stage,
            )
            self._save("prompts/final.txt", options["finalPrompt"] + "\n")
            self._save_job_settings({
                **options,
                "problemMode": "final-resume",
                "skipStatementReview": True,
                "statementReviewOnly": False,
            })
            self._save(
                "checked-statement.md", f"# Checked statement\n\n{statement}\n",
            )
            runtime.save_final_input(statement, solution, directory=self.run_dir)
            trace = self.state["trace"] if self.fixed_trace else []
            version = self.state["traceVersion"] if self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "problemMode": "final-resume",
                "skipStatementReview": True,
                "finalInputReady": True,
                "draft": statement,
                "sourceRun": str(continuation_source),
                **options,
                "workflow": LATEX_GRAPH,
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            process, token = self._launch_saved_final_locked(
                statement, solution,
            )
        self._spawn_worker(self._read_output, (process, token), token)

    def start_critic_resume(
        self, statement, solution, source_run="",
        critic_rounds=DEFAULT_CRITIC_ROUNDS,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        author_effort=None, critic_effort=None, writer_effort=None,
        author_prompt=None, critic_prompt=None, final_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        audit_checkpoint="",
        recover_audit_checkpoint=True,
        retrieval_mode="live", retrieval_cutoff_utc="",
    ):
        """Create a new job that audits one complete saved candidate proof."""

        self._check_input_replacement()

        statement, solution = str(statement).strip(), str(solution).strip()
        if not statement or not solution:
            raise ValueError("A saved statement and complete proof are required.")
        if "\0" in statement or "\0" in solution:
            raise ValueError("Saved critic inputs cannot contain NUL characters.")
        file_management = saved_file_management(source_run) if source_run else True
        options = self._workflow_options(
            critic_rounds=critic_rounds,
            thinking_hours=thinking_hours,
            author_model=author_model,
            critic_model=critic_model,
            writer_model=writer_model,
            reasoning_effort=reasoning_effort,
            author_effort=author_effort,
            critic_effort=critic_effort,
            writer_effort=writer_effort,
            author_prompt=author_prompt,
            critic_prompt=critic_prompt,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            include_review=False,
            file_management=file_management,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
        )
        if options["retrievalMode"] == "historical":
            raise ValueError("Historical search cannot be added to a saved critic review. Resume the original run.")
        if source_run:
            options.update(retrieval.continuation_options(source_run))
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("A model is already working on this job.")
            source_label = Path(source_run).name if source_run else "saved-proof"
            self._new_run(statement, f"critic-resume-{source_label}")
            if not self.fixed_trace:
                self.pinned = []
            self._prepare_continuation(
                source_run, "critic", allow_external_source=True,
            )
            if source_run:
                options.update(saved_goal_options(source_run))
            for name in ("author", "critic", "final"):
                self._save(f"prompts/{name}.txt", options[f"{name}Prompt"] + "\n")
            self._save_job_settings({
                **options,
                "problemMode": "critic-resume",
                "skipStatementReview": True,
                "statementReviewOnly": False,
            })
            self._save("checked-statement.md", f"# Checked statement\n\n{statement}\n")
            self._save(runtime.SAVED_CANDIDATE_FILENAME, solution + "\n")
            if audit_checkpoint and critic_uses_parallel_audits():
                self._save(
                    runtime.CRITIC_AUDIT_CHECKPOINT_FILENAME,
                    str(audit_checkpoint),
                )
            if not recover_audit_checkpoint and critic_uses_parallel_audits():
                self._save(
                    runtime.CRITIC_AUDIT_RECOVERY_DISABLED_FILENAME,
                    (
                        "This continuation starts from the saved proof and "
                        "intentionally runs fresh independent audits.\n"
                    ),
                )
            self._save(
                "resume-source.json",
                json.dumps({"sourceRun": str(source_run)}, indent=2) + "\n",
            )
            trace = self.state["trace"] if self.fixed_trace else []
            version = self.state["traceVersion"] if self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "problemMode": "critic-resume",
                "draft": statement,
                "sourceRun": str(source_run),
                **options,
                "workflow": PUBLIC_GRAPH,
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            process, token = self._launch_critic_resume_locked(
                statement, solution
            )
        self._spawn_worker(self._read_output, (process, token), token)

    def approve(self, edited_statement=None):
        """Solve the reviewed statement, including any direct author edit."""

        self._check_launch_hold()

        with self.lock:
            if self.state.get("statementReviewOnly", False):
                raise ValueError(
                    "This job was configured for statement review only."
                )
            if self.state["phase"] != "reviewed":
                raise ValueError("There is no reviewed statement to approve.")
            self._check_launch_models_locked()
            statement = self.state["review"]["statement"] if edited_statement is None else (
                str(edited_statement).strip()
            )
            if not statement:
                raise ValueError("The approved statement cannot be empty.")
            if statement != self.state["review"]["statement"]:
                self.state["review"] = {**self.state["review"], "statement": statement}
                self._save(
                    "checked-statement.md",
                    f"# Checked statement\n\n{statement}\n\n"
                    f"# Reviewer notes\n\n"
                    f"{self.state['review']['notes'] or 'None.'}\n\n"
                    "# Author action\n\nEdited and directly approved by the author.\n",
                )
            # Charge the review, not the wait for approval: start a new active
            # segment, saved before launch as resume() and prepared runs do.
            previous = {key: self.state[key] for key in ("elapsedSeconds", "resumedAt")
                        if key in self.state}
            self.state.update(elapsedSeconds=self._elapsed_seconds(),
                              resumedAt=datetime.now(timezone.utc).isoformat())
            try:
                self._save_job_settings(self.state)
                process, token = self._launch_solver_locked(statement)
            except (OSError, ValueError, RuntimeError):
                for key in ("elapsedSeconds", "resumedAt"):
                    self.state.pop(key, None)
                self.state.update(previous)
                raise
        self._spawn_worker(self._read_output, (process, token), token)

    def _read_output(self, process, token=None):
        """Store tagged solver events and build the visible final answer."""

        problem, answers, order, final, failed = "", {}, [], False, False
        latest_critic_solution = ""
        last_work_node = self.state["activeNode"]
        # Only the first repair turn after an author submission starts a new
        # answer; live instructions and continuation turns extend it.
        repair_pending = False
        try:
            for line in process.stdout:
                if self.output_stream is not None:
                    try:
                        self.output_stream.write(line)
                        self.output_stream.flush()
                    except (OSError, UnicodeError):
                        # Losing terminal output must not terminate a proof run.
                        self.output_stream = None
                # Untagged errors belong to the most recently active stage.
                record = self.parse_line(line, self.state["stage"] or "solve")
                self.add_trace(record)
                self._follow_model_retry(record)
                goal_thread = goal_thread_from_record(record)
                if goal_thread:
                    with self.lock:
                        self.state["goalThreadId"] = goal_thread
                        if not self.state.get("goalWorkspace"):
                            self.state["goalWorkspace"] = str(self.run_dir.resolve())
                        self._save_job_settings(self.state)
                delivered = record.get("authorSteerDelivered")
                if (record.get("kind") == "status" and record.get("stage") in {"solve", "repair"}
                        and record.get("root") is not False and isinstance(delivered, str) and delivered):
                    with self.lock:
                        self.state["authorSteerDelivered"] = delivered
                        self._save_job_settings(self.state)
                if (
                    record.get("kind") == "diagnostic"
                    and record.get("text", "").startswith("error: ")
                ):
                    problem = record["text"][7:]
                record_stage, record_node = record.get("stage"), record.get("node")
                if record_node and record_node != "failure_summary":
                    last_work_node = record_node
                elif record_stage in {"solve", "repair", "critic", "final"}:
                    last_work_node = event_node(record, last_work_node)
                stages = {
                    stage
                    for name, item in PUBLIC_GRAPH["nodes"].items()
                    for stage in item.get("stages", [item["stage"]])
                }
                if record_stage in stages:
                    with self.lock:
                        previous_stage = (self.state["stage"], self.state["activeNode"])
                        self.state["stage"] = record_stage
                        self.state["activeNode"] = event_node(record, self.state["activeNode"])
                        if previous_stage != (self.state["stage"], self.state["activeNode"]):
                            self._update_research_audits()
                if isinstance(record.get("round"), int):
                    with self.lock:
                        self.state["round"] = record["round"]
                elif (
                    record.get("kind") == "request"
                    and record_stage == "critic"
                    # A retry of the same critic call is not another round.
                    and record.get("attempt") in (None, 1)
                ):
                    with self.lock:
                        self.state["round"] += 1
                if record.get("kind") == "author_result":
                    repair_pending = True
                # A repair replaces the prior answer; the final proof replaces all.
                if (
                    record.get("kind") == "request"
                    and record_stage == "repair"
                    and record.get("label") == "Exact solve input"
                    and repair_pending
                ):
                    repair_pending = False
                    answers.clear()
                    order.clear()
                    with self.lock:
                        self.state["output"] = ""
                if record.get("kind") == "critic_result":
                    report = record.get("report")
                    latest_critic_solution = (
                        report["solution"] if isinstance(report, dict)
                        and report.get("verdict") == "pass"
                        and isinstance(report.get("solution"), str)
                        else ""
                    )
                if record.get("kind") == "workflow_paused":
                    with self.lock:
                        if self.state["phase"] != "stopping" and not self.state.get("manuallyStopped"):
                            checkpoint = read_pause_object(self.run_dir)
                            reason = record.get("reason", "")
                            cause = checkpoint.get("pauseCause") if self.state["phase"] == "pausing" else "error"
                            if cause == "audit" and reason:
                                # The author failed before it saw the audit pause (an
                                # acknowledged pause carries no reason). No audit runs and
                                # nothing resumes it: an error pause, retried as usual.
                                cause = "error"
                            paused_stage = record["stage"]
                            if (record["node"] == "author"
                                    and record["state"].get("report", {}).get("verdict") == "reject"):
                                paused_stage = "repair"
                            # The saved conversation is gone: retrying the same
                            # resume cannot help, so only an explicit choice continues.
                            unavailable = record.get("sessionUnavailable") is True
                            # A safety check stopped the author: only the user resumes it.
                            safety = record.get("safetyStop") is True
                            self._save_pause({**checkpoint, "status": "pausing", "state": record["state"],
                                              "pauseCause": cause or "error", "autoResumeAt": 0,
                                              "node": record["node"], "error": reason,
                                              "elapsedSeconds": self._elapsed_seconds(),
                                              "goalThreadId": self.state.get("goalThreadId", ""),
                                              "stage": paused_stage, "sessionUnavailable": unavailable,
                                              "safetyStop": safety})
                            self.state.update(phase="pausing", error=reason, sessionUnavailable=unavailable,
                                              safetyStop=safety)
                            self._update_research_audits()
                            self._save_job_settings(self.state)
                if (
                    record.get("kind") == "status"
                    and record.get("label") == "Critic approved"
                    and latest_critic_solution.strip()
                ):
                    try:
                        runtime.save_final_input(
                            saved_statement(self.run_dir), latest_critic_solution,
                            directory=self.run_dir,
                        )
                        with self.lock:
                            self.state["finalInputReady"] = True
                    except (OSError, UnicodeError, TypeError, ValueError):
                        # The explicit approval in the transcript remains recoverable.
                        pass
                if record.get("kind") == "final_result":
                    if not isinstance(record.get("output"), str) or not record["output"].strip():
                        problem = "The workflow returned an empty final answer. Continue from a saved checkpoint."
                        continue
                    with self.lock:
                        self.state["output"] = record.get("output", "")
                        self.state["resultKind"] = "final"
                        final = True
                        self._save("final.tex", self.state["output"])
                        if self.state.get("manuallyStopped"):
                            self._clear_manual_stop()
                            self.state.update(
                                phase="done", error="",
                                finishedAt=datetime.now(timezone.utc).isoformat(),
                            )
                    continue
                if record.get("kind") == "failure_result":
                    failed = True
                    with self.lock:
                        self.state["resultKind"] = "failure"
                        self.state["output"] = (
                            record.get("output") or record.get("summary")
                            or record.get("text", "")
                        )
                        final = True
                        self._save("failure-summary.md", self.state["output"])
                        if self.state.get("manuallyStopped"):
                            self._clear_manual_stop()
                            self.state.update(
                                phase="done", error="",
                                finishedAt=datetime.now(timezone.utc).isoformat(),
                            )
                    continue
                if record.get("kind") == "partial_result":
                    with self.lock:
                        self.state["output"] = record.get("output", "")
                    continue
                event = record.get("event", {})
                if record.get("kind") != "codex_event" or not record.get("root", True):
                    continue
                params, method = event.get("params", {}), event.get("method")
                item, append = params.get("item", {}), False
                if method == "item/agentMessage/delta":
                    item_id = params.get("itemId", "")
                    answer, append = params.get("delta", ""), True
                elif (
                    method == "item/completed"
                    and item.get("type") in {"agentMessage", "agent_message"}
                ):
                    item_id, answer = item.get("id", ""), item.get("text", "")
                else:
                    continue
                activity_label = record.get("activityLabel", "")
                if activity_label:
                    item_id = f"{activity_label}:{item_id}"
                if item_id not in answers:
                    order.append(item_id)
                    answers[item_id] = ""
                answers[item_id] = answers[item_id] + answer if append else answer
                with self.lock:
                    self.state["output"] = "".join(answers[key] for key in order)
            code = process.wait()
        except OSError as exc:
            problem = str(exc)
            stop_process_tree(process)
            code = process.poll()
        with self.lock:
            terminal_stop_race = (
                final
                and self.state.get("manuallyStopped")
                and self.state["phase"] == "stopping"
            )
            if self.active_token is not token and not terminal_stop_race:
                if self.worker_token is token:
                    self.worker_token = None
                return
            stopped = self.state["phase"] == "stopping"
            paused = self.state["phase"] == "pausing"
            # A runner that ended while waiting to retry: that wait was not active time.
            self._end_retry_wait_locked()
            self._model_retries = {}
            self.state["modelRetry"] = None
            if self.process is process:
                self.process = None
            if self.active_token is token:
                self.active_token = None
            if self.worker_token is token:
                self.worker_token = None
            self.state["phase"] = "done"
            self.state["finishedAt"] = datetime.now(timezone.utc).isoformat()
            checkpoint = read_pause_object(self.run_dir)
            author_interrupted = (not stopped and last_work_node == "author" and (not final or failed))
            if (paused or author_interrupted) and (not final or failed):
                error = checkpoint.get("error", "") if paused else (
                    problem or (self.state["output"] if failed else "")
                    or f"The author exited before completing the task (code {code}).")
                if not paused:
                    workflow_state = {**checkpoint.get("state", {}), **self._author_checkpoint_state()}
                    checkpoint.update(state=workflow_state, node="author",
                                      stage="repair" if workflow_state.get("report") else "solve")
                checkpoint.update(status="paused", elapsedSeconds=self._elapsed_seconds(),
                                  goalThreadId=self.state.get("goalThreadId", ""), error=error)
                if not paused:
                    checkpoint.update(pauseCause="error", autoResumeAt=0)
                self._configure_auto_resume(checkpoint)
                self._save_pause(checkpoint)
                self.state.update(phase="paused", error=error, activeNode=checkpoint["node"], stage=checkpoint["stage"], finishedAt="")
            elif final:
                # A complete terminal record is durable and wins even when Stop
                # killed the child before it closed stdout cleanly.
                self._clear_manual_stop()
                self.state["error"] = (
                    "Workflow incomplete. See the saved checkpoint and failure report."
                    if failed else ""
                )
            elif stopped:
                self.state["error"] = "Stopped."
                self.state["output"] = self.state["output"] or (
                    "The model was stopped before it produced an answer."
                )
            elif problem or code:
                self.state["error"] = problem or f"The solver exited with code {code}."
            elif not final:
                self.state["error"] = "The workflow exited without a final answer. Continue from a saved checkpoint."
            if self.state["output"] and not final:
                self._save("partial-output.md", self.state["output"])
            self._update_research_audits()
            if self.research_audits is not None:
                self._save_job_settings(self.state)

    def clear_trace(self):
        """Clear the saved transcript only when no request is active."""

        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Wait for the current model request to finish.")
            self.state["trace"] = []
            self.state["traceVersion"] = 0
            self.pinned = []
            if self.trace_file:
                self.trace_file.unlink(missing_ok=True)

    def stop(self):
        """Immediately stop the running CLI and all of its descendants."""

        with self.lock:
            if self.state["phase"] == "stopping" or (
                self.state["phase"] == "done"
                and self.state["error"] == "Stopped."
            ):
                return
            if self.state["phase"] not in {"reviewing", "running", "pausing"} and not self.state.get("auditHoldingAuthor"):
                raise ValueError("No model request is running in this job.")
            self._audit_pause_engine = None
            self.state["auditHoldingAuthor"] = False
            self.add_trace({
                "kind": "status", "stage": self.state["stage"],
                "node": self.state["activeNode"],
                "label": "Stop requested",
                "text": "The running model and any subagents are being stopped.",
            })
            stopped_at = self.state.get("lastActivityAt", "")
            try:
                self._save(
                    MANUAL_STOP_FILENAME,
                    json.dumps({
                        "schemaVersion": 1,
                        "stage": self.state["stage"],
                        "node": self.state["activeNode"],
                        "problemMode": self.state["problemMode"],
                        "stoppedAt": stopped_at,
                    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
            except (OSError, UnicodeError, TypeError, ValueError):
                # The transcript entry remains a restart-safe legacy fallback.
                pass
            self.state.update(
                manuallyStopped=True,
                autoResumeAt=0,
                stoppedStage=self.state["stage"],
            )
            (self.run_dir / PAUSE_REQUEST_FILENAME).unlink(missing_ok=True)
            (self.run_dir / PAUSE_FILENAME).unlink(missing_ok=True)
            process, self.state["phase"] = self.process, "stopping"
            self._update_research_audits()
            self._save_job_settings(self.state)
        stop_process_tree(process)
        with self.lock:
            # Real jobs stay non-continuable until their reader has drained all
            # buffered output. Synthetic/no-worker callers can settle here.
            if self.state["phase"] == "stopping" and self.worker_token is None:
                self._finish_stopped_locked(
                    self.active_token,
                    "The model was stopped before it produced an answer.",
                )

    def set_author_time_limit(self, hours):
        """Replace a running or paused author's total-workflow deadline."""

        try:
            hours = float(hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("The total time limit must be a number of hours.") from exc
        if not math.isfinite(hours) or not 0 <= hours <= MAX_THINKING_HOURS:
            raise ValueError(
                f"Set 0 for unlimited, or a total time limit at most "
                f"{MAX_THINKING_HOURS} hours."
            )
        with self.lock:
            if not (
                self.state["phase"] in {"running", "paused"}
                and self.state["activeNode"] == "author"
                and self.state["stage"] in {"solve", "repair"}
            ):
                raise ValueError(
                    "The total time limit can only be changed while the proof "
                    "author is running or paused."
                )
            if self.state["phase"] == "running" and (self.process is None or self.process.poll() is not None):
                raise ValueError("The proof author is no longer running.")
            hours = round(hours, 10)
            self._write_author_limit(hours)
            self.state["thinkingHours"] = hours
            self._save_job_settings(self.state)
            self.add_trace({
                "kind": "status", "stage": self.state["stage"], "node": "author",
                "label": "Total time limit set",
                "text": "The workflow has no configured time limit." if hours == 0 else f"The total workflow time limit is now {hours:g} hours.",
                "authorLimitHours": hours,
            })
            return hours

    def steer_author(self, instruction):
        """Queue one live instruction for the currently running author turn."""

        instruction = str(instruction or "").strip()
        if not instruction:
            raise ValueError("Enter an instruction for the proof author.")
        if "\0" in instruction:
            raise ValueError("The live instruction cannot contain NUL characters.")
        if len(instruction) > runtime.AUTHOR_STEER_MAX_CHARS:
            raise ValueError(
                f"Keep the live instruction at or below "
                f"{runtime.AUTHOR_STEER_MAX_CHARS} characters."
            )
        with self.lock:
            if not (
                self.state["phase"] == "running"
                and self.state["activeNode"] == "author"
                and self.state["stage"] in {"solve", "repair"}
            ):
                raise ValueError(
                    "Live instructions can only be sent while the proof author "
                    "is running."
                )
            if self.process is None or self.process.poll() is not None:
                raise ValueError("The proof author is no longer running.")
            command_id = self._write_author_steer(instruction)
            self.add_trace({
                "kind": "status", "stage": self.state["stage"],
                "node": "author", "label": "Live instruction queued",
                "text": instruction, "steerId": command_id,
            })
            return command_id

    def reset(self):
        """Return to the first screen when no child is active."""

        self._check_input_replacement()

        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Stop the current work first.")
            if self.research_audits is not None:
                self.research_audits.close()
                self.research_audits = None
                self._audit_token = None
            self._audit_pause_engine = None
            trace, version = self.state["trace"], self.state["traceVersion"]
            self.state = empty_state(trace, version)
            self.state["runId"] = self.run_dir.name if self.run_dir else ""


def _artifact_time(path):
    """Return one artifact modification time as an ISO UTC timestamp."""

    try:
        return datetime.fromtimestamp(
            Path(path).stat().st_mtime, timezone.utc,
        ).isoformat()
    except OSError:
        return ""


def _run_records(run_dir):
    """Read valid records from one private append-only run transcript."""

    return list(_iter_run_records(run_dir))


def _iter_run_records(run_dir):
    """Stream public records so recovery need not retain a large transcript."""

    path = Path(run_dir) / "transcript.jsonl"
    try:
        # Never follow a link or open a FIFO left where the log belongs.
        descriptor = open_run_log(path, os.O_RDONLY, private=False)
        if descriptor is None:
            return
        with os.fdopen(descriptor, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if readable_record(value):
                    yield value
    except OSError:
        pass


def _joins_instructions(value, instructions):
    """Whether ``value`` is the blank-line join of consecutive saved instructions."""

    for start, first in enumerate(instructions):
        joined = first
        for item in instructions[start + 1:]:
            if not value.startswith(joined + "\n\n"):
                break
            joined += "\n\n" + item
            if joined == value:
                return True
    return False


def saved_author_instructions(run_dir):
    """Return bounded, deduplicated user steering from a stopped author run."""

    labels = {
        "Live instruction queued", "Live author instruction sent",
        "Restored live instructions queued",
    }
    records = [record for record in _iter_run_records(run_dir)
               if record.get("label") in labels]
    restored = [record for record in records
                if record.get("label") == "Restored live instructions queued"]
    restored_ids = {
        str(record.get("steerId") or "").strip() for record in restored
        if str(record.get("steerId") or "").strip()
    }
    # Older runners logged the delivered block without its steerId; recognize
    # that echo by its text.
    restored_texts = {
        text.strip() for text in (record.get("text") for record in restored)
        if isinstance(text, str) and text.strip()
    }
    instructions, seen = [], set()
    for record in records:
        if record.get("label") == "Live author instruction sent":
            text = record.get("text")
            if (str(record.get("steerId") or "").strip() in restored_ids
                    or (isinstance(text, str) and text.strip() in restored_texts)):
                continue
        values = record.get("restoredInstructions")
        values = values if isinstance(values, list) else [record.get("text")]
        for value in values:
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                continue
            key = value.strip()
            # Earlier continuations may have saved such a block as one more
            # instruction; it only repeats instructions already collected.
            if key in seen or _joins_instructions(key, instructions):
                continue
            seen.add(key)
            instructions.append(key)

    selected, size = [], 0
    for instruction in reversed(instructions):
        additional = len(instruction) + (2 if selected else 0)
        if size + additional > runtime.AUTHOR_STEER_MAX_CHARS:
            continue
        selected.append(instruction)
        size += additional
    return list(reversed(selected))


def saved_goal_thread_id(run_dir):
    """Recover the author session ID from existing settings or public statuses."""

    try:
        settings = json.loads((Path(run_dir) / JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        settings = {}
    if isinstance(settings, dict):
        identity = settings.get("goalThreadId")
        if isinstance(identity, str) and identity.strip() and "\0" not in identity:
            return identity.strip()
    identity = ""
    for record in _iter_run_records(run_dir):
        identity = goal_thread_from_record(record) or identity
    return identity


def saved_file_management(run_dir):
    """Old runs are managed; only an explicit saved false selects simple mode."""
    try:
        settings = json.loads((Path(run_dir) / JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return True
    return not isinstance(settings, dict) or settings.get("fileManagement") is not False


def saved_goal_options(run_dir):
    """Recover generic session/workspace settings without reading author files."""

    run_dir = Path(run_dir).resolve()
    try:
        settings = json.loads((run_dir / JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        settings = {}
    workspace = settings.get("goalWorkspace") if isinstance(settings, dict) else None
    workspace = Path(workspace).expanduser().resolve() if isinstance(workspace, str) and workspace else run_dir
    if not workspace.is_dir():
        raise ValueError(f"The saved author workspace no longer exists: {workspace}")
    return {"goalWorkspace": str(workspace), "goalResume": True,
            "fileManagement": not isinstance(settings, dict) or settings.get("fileManagement") is not False,
            "goalThreadId": saved_goal_thread_id(run_dir)}


def saved_manual_stop(run_dir, records=None):
    """Return one durable manual-stop marker, including legacy transcripts."""

    run_dir = Path(run_dir)
    records = _run_records(run_dir) if records is None else records
    marker = None
    try:
        value = json.loads(
            (run_dir / MANUAL_STOP_FILENAME).read_text(encoding="utf-8")
        )
        if (
            isinstance(value, dict)
            and value.get("schemaVersion") == 1
            and value.get("stage") in {
                "review", "solve", "repair", "critic", "final", "failure",
            }
        ):
            marker = dict(value)
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass

    legacy = None
    for index, record in enumerate(records):
        if (
            record.get("kind") == "status"
            and record.get("label") == "Stop requested"
            and record.get("stage") in {
                "review", "solve", "repair", "critic", "final", "failure",
            }
        ):
            legacy = {
                "schemaVersion": 0,
                "stage": record["stage"],
                "node": record.get("node", ""),
                "stoppedAt": record.get("time", ""),
            }
    result = marker or legacy
    if result is None:
        return None

    # A result for the latest request wins a legacy stop/result race. Review retries
    # reuse one run, so a result from an earlier review must not hide a later
    # stopped feedback request. Apply the same generation rule to durable markers:
    # Stop can be written just after a complete result but before the reader sees
    # EOF, and that completed generation must not be offered for continuation.
    stage = result["stage"]
    latest_request = max(
        (
            index for index, record in enumerate(records)
            if record.get("kind") == "request"
            and record.get("stage") == stage
        ),
        default=-1,
    )
    terminal_kinds = (
        {"review_result"}
        if stage == "review" else {"final_result", "failure_result"}
    )
    latest_result = max(
        (
            index for index, record in enumerate(records)
            if record.get("kind") in terminal_kinds
        ),
        default=-1,
    )
    if latest_result >= 0 and latest_result > latest_request:
        return None
    return result


def checkpoint_artifact_signature(run_dir, include_transcript=True):
    """Return a cheap cache key for files that define durable checkpoints."""

    if not run_dir:
        return ()
    run_dir = Path(run_dir)
    signature = []
    names = [
        "checked-statement.md",
        "SOLUTION.md", runtime.SAVED_CANDIDATE_FILENAME,
    ]
    if critic_uses_parallel_audits():
        names.append(runtime.CRITIC_AUDIT_CHECKPOINT_FILENAME)
    # The live transcript changes on every heartbeat. Candidate and audit files
    # cover checkpoints that can appear while a job is active; scan the full
    # transcript once the job becomes idle to discover coordinator failures.
    if include_transcript:
        names.append("transcript.jsonl")
    for name in names:
        try:
            stat = (run_dir / name).stat()
            signature.append((name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((name, 0, 0))
    return tuple(signature)


def valid_saved_audit_reports(run_dir, source):
    """Return only audit reports the runner can actually restore."""

    if not critic_uses_parallel_audits():
        return []
    run_dir = Path(run_dir)
    path = run_dir / runtime.CRITIC_AUDIT_CHECKPOINT_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        reports = value.get("reports") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion")
            != runtime.CRITIC_AUDIT_CHECKPOINT_SCHEMA_VERSION
            or not isinstance(reports, list)
            or len(reports) != len(runtime.CRITIC_AUDIT_FOCI)
        ):
            return []
        settings = {}
        try:
            loaded = json.loads(
                (run_dir / JOB_SETTINGS_FILENAME).read_text(encoding="utf-8")
            )
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        model = restored_model(settings.get(
            "criticModel", value.get("model", DEFAULT_CRITIC_MODEL),
        ))
        effort = settings.get(
            "criticEffort",
            settings.get(
                "reasoningEffort",
                value.get("reasoningEffort", DEFAULT_REASONING_EFFORT),
            ),
        )
        model = runtime.chosen_model(model)
        effort = runtime.chosen_effort(effort)
        audit_effort = runtime.effective_effort(model, effort)
        instructions = runtime.text(source["critic_prompt"])
        if runtime.CRITIC_MEMORY_PROMPT not in instructions:
            instructions = f"{instructions}\n\n{runtime.CRITIC_MEMORY_PROMPT}"
        expected = runtime.critic_audit_assignment_sha256(
            source["statement"], source["solution"], model, audit_effort,
            instructions,
        )
        if value.get("assignmentSha256") != expected:
            return []
        for report in reports:
            if report is not None:
                runtime.validate_json_schema(
                    report, runtime.CRITIC_CHECK_SCHEMA,
                )
        return reports
    except (
        OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError,
        runtime.Error,
    ):
        return []


def saved_run_checkpoints(run_dir):
    """Describe every durable, resumable critic checkpoint in one run."""

    run_dir = Path(run_dir)
    statement_ready = bool(
        (run_dir / "checked-statement.md").is_file()
    )
    candidate_path = next(
        (
            path for path in (
                run_dir / runtime.SAVED_CANDIDATE_FILENAME,
                run_dir / "SOLUTION.md",
            )
            if path.is_file()
        ),
        None,
    )
    source = None
    if statement_ready and candidate_path is not None:
        try:
            source = saved_critic_source(run_dir)
        except ValueError:
            pass
    checkpoints = []
    if source is not None:
        checkpoints.append({
            "id": "candidate",
            "label": "Saved proof candidate",
            "stage": "critic",
            "status": "ready",
            "completedAt": _artifact_time(candidate_path),
            "description": (
                "Continue at independent critic review without rerunning the "
                "proof author."
            ),
            "resumable": True,
            "resumeLabel": "Continue at critic",
        })

    if not critic_uses_parallel_audits():
        return checkpoints
    audit_path = run_dir / runtime.CRITIC_AUDIT_CHECKPOINT_FILENAME
    reports = valid_saved_audit_reports(run_dir, source) if source else []
    completed_audits = sum(report is not None for report in reports)
    audit_verdicts = [
        report["verdict"] for report in reports if report is not None
    ]
    if source is not None and completed_audits:
        failures = audit_verdicts.count("fail")
        checkpoints.append({
            "id": "independent-audits",
            "label": f"Independent audits {completed_audits}/3",
            "stage": "critic",
            "status": "complete" if completed_audits == 3 else "partial",
            "completedAt": _artifact_time(audit_path),
            "description": (
                f"{completed_audits} audit"
                f"{'s are' if completed_audits != 1 else ' is'} saved"
                + (f"; {failures} reported proof issues." if failures else ".")
                + (
                    " Continue with only the missing audits."
                    if completed_audits < 3
                    else " Continue directly to coordinator adjudication."
                )
            ),
            "resumable": True,
            "resumeLabel": (
                "Run missing audits" if completed_audits < 3
                else "Continue coordinator"
            ),
            "completedAuditCount": completed_audits,
            "failedAuditCount": failures,
        })

    coordinator_request, coordinator_result, coordinator_error = None, None, None
    for record in _run_records(run_dir):
        if (
            record.get("kind") == "request"
            and record.get("label") == "Critic coordinator adjudication"
        ):
            coordinator_request = record
            coordinator_result = coordinator_error = None
        elif coordinator_request is not None and record.get("kind") == "critic_result":
            coordinator_result = record
        elif (
            coordinator_request is not None
            and record.get("kind") == "diagnostic"
            and str(record.get("text", "")).startswith("error: ")
        ):
            coordinator_error = record
    if (
        source is not None
        and completed_audits == 3 and coordinator_request is not None
        and coordinator_result is None
    ):
        stopped_at = coordinator_error or coordinator_request
        checkpoints.append({
            "id": "critic-coordinator",
            "label": "Critic coordinator",
            "stage": "critic",
            "status": "failed" if coordinator_error else "interrupted",
            "completedAt": stopped_at.get("time", ""),
            "description": (
                "All three independent audits are saved. Continue the "
                "coordinator without rerunning them."
            ),
            "resumable": True,
            "resumeLabel": "Retry coordinator",
        })
    return checkpoints


def restore_saved_app(app):
    """Reconstruct one idle historical job for a newly started Web UI."""

    run_dir = app.run_dir
    if not run_dir:
        return app
    records = _run_records(run_dir)
    state = empty_state(app.state["trace"], app.state["traceVersion"])
    # Historical jobs predate the opt-in switch and used managed author records.
    state.update(fileManagement=True, authorPrompt=default_prompts()["author"])
    state["runId"] = run_dir.name
    draft_path = run_dir / "draft.md"
    try:
        draft = draft_path.read_text(encoding="utf-8").strip()
        if draft.startswith("# Draft problem"):
            draft = draft[len("# Draft problem"):].lstrip()
        state["draft"] = draft
        state["reviewStatement"] = draft
    except (OSError, UnicodeError):
        pass
    try:
        review_input = json.loads(
            (run_dir / REVIEW_INPUT_FILENAME).read_text(encoding="utf-8")
        )
        if (
            not isinstance(review_input, dict)
            or review_input.get("schemaVersion") != 1
            or not isinstance(review_input.get("statement"), str)
            or not review_input["statement"].strip()
            or not isinstance(review_input.get("feedback"), str)
        ):
            raise ValueError("invalid saved review input")
        state["reviewStatement"] = review_input["statement"].strip()
        state["reviewFeedback"] = review_input["feedback"].strip()
        state["reviewInputRecorded"] = True
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        pass

    settings_path = run_dir / JOB_SETTINGS_FILENAME
    persisted_settings = set()
    # Roles whose saved model was removed (DeepSeek) and now runs as its replacement.
    replaced_roles, settings_warnings = set(), []
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(settings, dict):
            try:
                state["sourceFile"] = source_file_name(settings.get("sourceFile"))
            except ValueError:
                pass
            api_models = saved_api_models(settings.get("apiModels"))
            if api_models:
                state["apiModels"] = api_models
            for key in (
                "reviewModel", "authorModel", "criticModel", "writerModel",
                "reasoningEffort", "reviewEffort", "authorEffort",
                "criticEffort", "writerEffort", "criticRounds",
                "thinkingHours", "speedMode", "reasoningSummary",
                "researchAudits", "researchAuditProgress",
                "problemMode", "skipStatementReview", "statementReviewOnly", "fileManagement", "preparedRun",
                "launchHold", "retrievalPolicyPath", "retrievalMode", "retrievalCutoffUtc",
                "goalThreadId", "goalWorkspace", "goalResume", "authorSteerDelivered", "elapsedSeconds", "resumedAt",
            ):
                if key in settings:
                    if key in {"launchHold", "retrievalPolicyPath", "retrievalMode", "retrievalCutoffUtc"} and not isinstance(settings[key], str):
                        state["launchHold"] = "Invalid saved historical launch controls; repair before launch."
                        continue
                    if key in {
                        "skipStatementReview", "statementReviewOnly", "fileManagement", "preparedRun",
                    } and not isinstance(settings[key], bool):
                        continue
                    if (
                        key == "problemMode"
                        and settings[key] not in {
                            "statement", "algorithmic", "latex",
                            "critic-resume", "final-resume",
                        }
                    ):
                        continue
                    persisted_settings.add(key)
                    if key == "researchAudits":
                        try:
                            state[key] = audits.normalize_settings(settings[key])
                        except ValueError:
                            settings_warnings.append("Saved research audit settings are invalid; audits are disabled.")
                        continue
                    if key == "researchAuditProgress" and not isinstance(settings[key], dict):
                        continue
                    if key.endswith("Model"):
                        state[key] = restored_model(settings[key])
                        if state[key] != settings[key]:
                            replaced_roles.add(key[:-len("Model")])
                        continue
                    state[key] = settings[key]
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if not state["fileManagement"]:
        state["authorPrompt"] = default_prompts()["author_simple"]
        state["researchAudits"] = {**audits.default_settings(), "models": ["none", "none", "none"]}
    warnings = audits.selected_warnings(state["researchAuditProgress"].get("warnings"), state["researchAudits"])
    state["researchAuditStatus"].update(warnings=warnings, lastError="\n".join(item["message"] for item in warnings))
    for name in ("review", "author", "critic", "final"):
        prompt_path = run_dir / "prompts" / f"{name}.txt"
        try:
            state[f"{name}Prompt"] = prompt_path.read_text(
                encoding="utf-8"
            ).strip()
        except (OSError, UnicodeError):
            pass

    direct_statement = False
    try:
        direct_statement = (run_dir / "checked-statement.md").read_text(
            encoding="utf-8"
        ).lstrip().startswith("# Statement sent directly to the proof author")
    except (OSError, UnicodeError):
        pass
    if "skipStatementReview" not in persisted_settings and direct_statement:
        state["skipStatementReview"] = True
    proof_prompts = [
        run_dir / "prompts" / f"{name}.txt"
        for name in ("author", "critic", "final")
    ]
    review_only_artifacts = (
        (run_dir / "prompts" / "review.txt").is_file()
        and not any(path.is_file() for path in proof_prompts)
    )
    if (
        "statementReviewOnly" not in persisted_settings
        and review_only_artifacts
    ):
        state["statementReviewOnly"] = True

    if (run_dir / "resume-source.json").is_file():
        state["problemMode"] = "critic-resume"
        state["skipStatementReview"] = True
    elif (run_dir / "algorithmic-input.json").is_file():
        state["problemMode"] = "statement"
        state["skipStatementReview"] = True
        state["workflow"] = DIRECT_GRAPH
        try:
            fields = json.loads(
                (run_dir / "algorithmic-input.json").read_text(encoding="utf-8")
            )
            if isinstance(fields, dict):
                for key in ("modelOfComputation", "problemDescription", "goal"):
                    if isinstance(fields.get(key), str):
                        state[key] = fields[key]
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    elif (run_dir / "latex-input.md").is_file():
        state["problemMode"] = "latex"
        state["workflow"] = LATEX_GRAPH
    elif (
        state["problemMode"] == "final-resume"
        and (run_dir / runtime.FINAL_INPUT_FILENAME).is_file()
    ):
        state["workflow"] = LATEX_GRAPH
    elif state["statementReviewOnly"]:
        state["workflow"] = REVIEW_ONLY_GRAPH
    elif state["skipStatementReview"]:
        state["workflow"] = DIRECT_GRAPH

    transcript_speed = None
    observed_role_settings = set()
    post_review_request = False
    last_work_node = ""
    author_finished = False
    for record in records:
        goal_thread = goal_thread_from_record(record)
        if goal_thread:
            state["goalThreadId"] = goal_thread
        delivered = record.get("authorSteerDelivered")
        if (record.get("kind") == "status" and record.get("stage") in {"solve", "repair"}
                and record.get("root") is not False and isinstance(delivered, str) and delivered):
            state["authorSteerDelivered"] = delivered
        stage = record.get("stage")
        if stage:
            state["stage"] = stage
            state["activeNode"] = event_node(record, state["activeNode"])
            if stage in {"solve", "repair", "critic", "final"}:
                last_work_node = state["activeNode"]
        if record.get("kind") == "request" and stage in {"solve", "repair"}:
            author_finished = False
        if record.get("kind") == "author_result":
            author_finished = True
        if record.get("kind") == "failure_result":
            state["resultKind"] = "failure"
            state["output"] = record.get("output") or record.get("summary") or record.get("text", "")
        elif (record.get("kind") in {"final_result", "workflow_result"}
                and isinstance(record.get("output"), str) and record["output"].strip()):
            state["resultKind"] = "final"
            state["output"] = record["output"]
        if isinstance(record.get("round"), int):
            state["round"] = record["round"]
        if record.get("kind") == "request":
            model, effort = record.get("model"), record.get("reasoningEffort")
            label = str(record.get("label", ""))
            role = {
                "review": "review", "solve": "author", "repair": "author",
                "critic": "critic", "final": (
                    "critic" if state["activeNode"] == "final_verifier" else "writer"
                ),
            }.get(stage)
            if (
                role and isinstance(model, str) and model
                and isinstance(effort, str) and effort
            ):
                observed_role_settings.add(role)
            if stage in {"solve", "repair", "critic", "final"}:
                post_review_request = True
            model_key = f"{role}Model" if role else ""
            effort_key = f"{role}Effort" if role else ""
            if (
                role and isinstance(model, str)
                and model_key not in persisted_settings
            ):
                state[f"{role}Model"] = restored_model(model)
                if state[f"{role}Model"] != model:
                    replaced_roles.add(role)
                else:
                    replaced_roles.discard(role)
            if (
                role and isinstance(effort, str)
                and effort_key not in persisted_settings
                and not (
                    role == "critic"
                    and label.startswith("Independent critic audit ")
                )
            ):
                state[f"{role}Effort"] = effort
            if (
                "reasoningSummary" not in persisted_settings
                and isinstance(record.get("reasoningSummary"), str)
            ):
                state["reasoningSummary"] = record["reasoningSummary"]
            if (
                "speedMode" not in persisted_settings
                and record.get("serviceTier") in SPEEDS
            ):
                tier = record["serviceTier"]
                if transcript_speed is None or tier == "fast":
                    transcript_speed = tier
        if record.get("kind") == "review_result" and isinstance(
            record.get("review"), dict
        ):
            state["review"] = record["review"]
        if (
            record.get("kind") == "diagnostic"
            and str(record.get("text", "")).startswith("error: ")
        ):
            state["error"] = str(record["text"])[7:]

    if state["problemMode"] == "algorithmic":
        state["problemMode"] = "statement"
        state["skipStatementReview"] = True
        state["workflow"] = DIRECT_GRAPH
    if transcript_speed is not None:
        state["speedMode"] = transcript_speed

    if state["statementReviewOnly"]:
        required_roles = {"review"}
    elif state["problemMode"] in {"latex", "final-resume"}:
        required_roles = {"writer"}
    elif state["problemMode"] in {"algorithmic", "critic-resume"}:
        required_roles = {"author", "critic", "writer"}
    else:
        required_roles = {"author", "critic", "writer"}
        if not state["skipStatementReview"]:
            required_roles.add("review")
    unknown_roles = sorted(
        role for role in required_roles
        if f"{role}Model" not in persisted_settings
        and role not in observed_role_settings
    )
    if unknown_roles:
        names = ", ".join(ROLE_LABELS[role] for role in unknown_roles)
        settings_warnings.append(
            f"This older run did not record the {names} model settings. "
            "Those roles will use the current defaults if you continue."
        )
    # Only roles this job still runs are worth a warning.
    replaced_roles &= required_roles
    if replaced_roles:
        settings_warnings.insert(0, removed_model_warning(replaced_roles))
    # An API model removed since the job ran: a settings error, never retried.
    unconfigured_roles = [
        role for role in ROLES if role in required_roles
        and providers.is_api_id(state.get(f"{role}Model")) and not providers.is_known(state[f"{role}Model"])
    ]
    settings_warnings.extend(unconfigured_job_model_error(state[f"{role}Model"], role, state.get("apiModels"))
                             for role in unconfigured_roles)

    first_time = records[0].get("time") if records else ""
    last_time = records[-1].get("time") if records else ""
    state["startedAt"] = (
        first_time if isinstance(first_time, str) and first_time
        else _artifact_time(run_dir)
    )
    state["lastActivityAt"] = last_time if isinstance(last_time, str) else ""
    state["finishedAt"] = state["lastActivityAt"]
    state["phase"] = "done"
    for filename in ("final.tex", "failure-summary.md", "partial-output.md"):
        if state["resultKind"] == "final" and filename != "final.tex":
            continue
        path = run_dir / filename
        if path.is_file():
            try:
                output = path.read_text(encoding="utf-8")
                if not output.strip():
                    continue
                state["output"] = output
                if filename == "final.tex" and state["output"].strip():
                    state["resultKind"] = "final"
                elif filename == "failure-summary.md":
                    state["resultKind"] = "failure"
                break
            except (OSError, UnicodeError):
                pass
    if state["resultKind"] == "final":
        # A completed answer supersedes errors that a later resume recovered from.
        state["error"] = ""
    if state["statementReviewOnly"] and state["review"]:
        state["output"] = state["review"].get("statement", "")
        state["resultKind"] = "review"
    elif (
        state["problemMode"] == "statement"
        and not state["skipStatementReview"]
        and state["review"]
        and not post_review_request
    ):
        state.update(
            phase="reviewed", stage="review",
            activeNode="statement_reviewer", finishedAt="", error="",
        )
    if state["problemMode"] != "latex":
        try:
            saved_final_input(run_dir, records=records)
            state["finalInputReady"] = True
        except ValueError:
            pass
    manual_stop = saved_manual_stop(run_dir, records)
    if manual_stop is not None:
        stopped_stage = manual_stop["stage"]
        state.update(
            phase="done",
            error="Stopped.",
            manuallyStopped=True,
            stoppedStage=stopped_stage,
            stage=stopped_stage,
            activeNode={
                "review": "statement_reviewer",
                "solve": "author",
                "repair": "author",
                "critic": "critic",
                "final": "latex_editor",
                "failure": "failure_summary",
            }[stopped_stage],
            finishedAt=(
                manual_stop.get("stoppedAt") or state["lastActivityAt"]
            ),
        )
        if not state["output"]:
            state["output"] = (
                "This job was manually stopped. Continue it from the home page."
            )
    checkpoint = read_pause_object(run_dir)
    if (manual_stop is None and state["resultKind"] != "final"
            and last_work_node == "author" and not author_finished
            and checkpoint.get("status") not in {"paused", "pausing"}):
        # A server/process crash leaves the last resume checkpoint marked running.
        # There is no worker attached to a restored job, and partial text is not a proof.
        statement = checkpoint.get("state", {}).get("statement")
        if not statement:
            try:
                statement = saved_statement(run_dir)
            except ValueError:
                statement = state["draft"]
        if statement:
            elapsed = checkpoint.get("elapsedSeconds", state.get("elapsedSeconds", 0))
            try:
                segment_start = datetime.fromisoformat(state.get("resumedAt") or state["startedAt"])
                segment_end = datetime.fromisoformat(state["lastActivityAt"])
                elapsed += max(0, (segment_end - segment_start).total_seconds())
            except (TypeError, ValueError):
                pass
            recovered_state = checkpoint.get("state") or {"statement": statement}
            # The unfinished author was repairing the latest rejection, even when
            # its new goal session logged as 'solve' or audit records followed it.
            rejection = next((record for record in reversed(records)
                              if record.get("kind") == "critic_result"), {})
            report = rejection.get("report")
            if isinstance(report, dict) and report.get("verdict") == "reject":
                recovered_state = {**recovered_state, "report": report,
                                   "solution": report.get("solution", ""),
                                   "round": rejection.get("round", state["round"])}
            checkpoint = {
                **checkpoint, "status": "paused", "node": "author",
                "state": recovered_state,
                "stage": "repair" if recovered_state.get("report") else "solve",
                "goalThreadId": checkpoint.get("goalThreadId") or state["goalThreadId"],
                "elapsedSeconds": elapsed,
                "error": "The previous author process ended without a final answer. Resume continues its saved conversation.",
                "pauseCause": "interrupted", "autoResumeAt": 0,
            }
    if (checkpoint.get("status") in {"paused", "pausing"}
            and state["resultKind"] != "final" and manual_stop is None):
        state.update(phase="paused", stage=checkpoint["stage"], activeNode=checkpoint["node"],
                     error=checkpoint.get("error", ""), finishedAt="",
                     elapsedSeconds=checkpoint.get("elapsedSeconds", state.get("elapsedSeconds", 0)),
                     resumedAt=state["lastActivityAt"],
                     sessionUnavailable=checkpoint.get("sessionUnavailable") is True,
                     safetyStop=checkpoint.get("safetyStop") is True)
        if checkpoint.get("goalThreadId"):
            state["goalThreadId"] = checkpoint["goalThreadId"]
    elif state["phase"] == "done" and manual_stop is None and not state["resultKind"]:
        state["error"] = state["error"] or "This run ended without a final answer. Continue from a saved checkpoint."
    elif state["resultKind"] == "failure":
        state["error"] = state["error"] or "Workflow incomplete. See the saved checkpoint and failure report."
    if state.get("retrievalMode") == "historical" and not state.get("retrievalPolicyPath"):
        state["launchHold"] = "Historical search policy is missing; restore the controller policy before launch."
    if state["preparedRun"] and not records and not state["output"]:
        state.update(phase="prepared", stage="solve", activeNode="author", startedAt="",
                     resumedAt="", elapsedSeconds=0, finishedAt="", error="")
    state["checkpoints"] = saved_run_checkpoints(run_dir)
    app._checkpoint_cache = list(state["checkpoints"])
    app._checkpoint_cache_signature = checkpoint_artifact_signature(run_dir)
    app.state = state
    if state["phase"] == "paused":
        pause_path = run_dir / PAUSE_FILENAME
        app._configure_auto_resume(checkpoint, pause_path.stat().st_mtime if pause_path.exists() else time.time())
        if replaced_roles and checkpoint.get("autoResumeAt"):
            # A replaced model is never started unattended: Resume is explicit.
            checkpoint["autoResumeAt"] = state["autoResumeAt"] = 0
            settings_warnings.insert(1, "Automatic retry is off; choose Resume to continue with the replacement.")
        if unconfigured_roles and checkpoint.get("autoResumeAt"):
            checkpoint["autoResumeAt"] = state["autoResumeAt"] = 0
            settings_warnings.append("Automatic retry is off; choose Resume once the model is configured.")
        app._save_pause(checkpoint)
    state["settingsWarning"] = " ".join(settings_warnings)
    app.fixed_trace = False
    return app


def restore_saved_jobs(runs):
    """Load every historical run as an idle job after a Web UI restart."""

    jobs = {}
    # A broken shared workflow file is not a damaged run: report it before
    # skipping individual runs could hide every job.
    default_prompts()
    candidates = set(Path(runs).glob("*/transcript.jsonl"))
    for settings_path in Path(runs).glob("*/job-settings.json"):
        if settings_path.is_symlink():
            continue
        try:
            if json.loads(settings_path.read_text(encoding="utf-8")).get("preparedRun") is True:
                candidates.add(settings_path.parent / "transcript.jsonl")
        except (OSError, UnicodeError, ValueError, AttributeError):
            continue
    for transcript in sorted(candidates):
        if transcript.is_symlink() or not direct_run_directory(
            transcript.parent, runs
        ):
            continue
        try:
            app = restore_saved_app(App(transcript, runs))
        except (OSError, UnicodeError, TypeError, AttributeError, KeyError,
                ValueError, RecursionError) as exc:
            # One damaged run folder must not keep the Web UI from starting.
            print(f"Skipped damaged saved run {transcript.parent.name}: {exc}",
                  file=sys.stderr, flush=True)
            continue
        jobs[app.state["runId"]] = app
    return jobs


def read_utf8(path, purpose):
    """Read one required UTF-8 text file with a useful CLI error."""

    source = Path(path).expanduser().resolve()
    try:
        value = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read {purpose} {source}: {exc}") from exc
    if not value.strip():
        raise ValueError(f"The {purpose} is empty: {source}")
    if "\0" in value:
        raise ValueError(f"The {purpose} contains a NUL character: {source}")
    return value


def saved_statement(path):
    """Load the exact author assignment from one saved run."""

    source = Path(path).expanduser().resolve()
    run_dir = source if source.is_dir() else source.parent
    if not run_dir.is_dir():
        raise ValueError(f"The saved run does not exist: {run_dir}")
    checked_path = run_dir / "checked-statement.md"
    if checked_path.exists():
        checked = read_utf8(checked_path, "saved checked statement")
        reviewed_prefix = "# Checked statement"
        direct_prefix = "# Statement sent directly to the proof author"
        checked = checked.lstrip()
        if checked.startswith(reviewed_prefix):
            statement = checked[len(reviewed_prefix):].lstrip("\r\n")
            statement = statement.split("\n# Reviewer notes", 1)[0].strip()
        elif checked.startswith(direct_prefix):
            statement = checked[len(direct_prefix):].lstrip("\r\n")
            footer = "\n# Statement review\n\nSkipped by the user."
            if footer in statement:
                statement = statement.rsplit(footer, 1)[0].strip()
        else:
            raise ValueError(
                f"Cannot locate the checked statement in {checked_path}."
            )
    else:
        raise ValueError(f"No saved checked statement exists in {run_dir}.")
    if not statement:
        raise ValueError("The saved checked statement is empty.")
    return statement


def saved_final_input(path, records=None):
    """Load the exact clean statement/proof pair sent to the final editor."""

    source = Path(path).expanduser().resolve()
    run_dir = source if source.is_dir() else source.parent
    final_input_path = run_dir / runtime.FINAL_INPUT_FILENAME
    try:
        value = json.loads(final_input_path.read_text(encoding="utf-8"))
        if (
            isinstance(value, dict)
            and value.get("schemaVersion") == 1
            and isinstance(value.get("statement"), str)
            and value["statement"].strip()
            and isinstance(value.get("solution"), str)
            and value["solution"].strip()
        ):
            return {
                "statement": value["statement"].strip(),
                "solution": value["solution"].strip(),
            }
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass

    # The workflow approves either an unchanged pass or an edited pass at its limit.
    records = _run_records(run_dir) if records is None else records
    latest_solution, approved_solution = "", ""
    for record in records:
        if record.get("kind") == "critic_result":
            report = record.get("report")
            latest_solution = (
                report["solution"] if isinstance(report, dict)
                and report.get("verdict") == "pass"
                and isinstance(report.get("solution"), str)
                else ""
            )
        elif record.get("kind") == "status" and record.get("label") == "Critic approved":
            approved_solution = latest_solution
    if approved_solution.strip():
        return {"statement": saved_statement(run_dir), "solution": approved_solution.strip()}
    raise ValueError(
        "This stopped final-editor job has no exact saved final input. "
        "Continue it from its latest critic checkpoint instead."
    )


def saved_critic_source(path):
    """Load the exact checked statement and complete proof from one saved run."""

    source = Path(path).expanduser().resolve()
    run_dir = source if source.is_dir() else source.parent
    if not run_dir.is_dir():
        raise ValueError(f"The saved run does not exist: {run_dir}")
    solution_path = run_dir / runtime.SAVED_CANDIDATE_FILENAME
    if not solution_path.exists():
        solution_path = run_dir / "SOLUTION.md"
    solution = read_utf8(solution_path, "saved complete proof")
    statement = saved_statement(run_dir)
    defaults = default_prompts()
    prompts = {}
    if not saved_file_management(run_dir):
        defaults["author"] = defaults["author_simple"]
    for name in ("author", "critic", "final"):
        prompt_path = run_dir / "prompts" / f"{name}.txt"
        prompts[name] = (
            read_utf8(prompt_path, f"saved {name} prompt")
            if prompt_path.exists() else defaults[name]
        )
    checkpoint_path = run_dir / runtime.CRITIC_AUDIT_CHECKPOINT_FILENAME
    try:
        audit_checkpoint = (
            read_utf8(checkpoint_path, "saved critic audit checkpoint")
            if critic_uses_parallel_audits() and checkpoint_path.exists() else ""
        )
    except ValueError:
        # A corrupt optional audit must not hide the earlier valid candidate.
        audit_checkpoint = ""
    return {
        "run_dir": run_dir,
        "statement": statement,
        "solution": solution,
        "author_prompt": prompts["author"],
        "critic_prompt": prompts["critic"],
        "final_prompt": prompts["final"],
        "audit_checkpoint": audit_checkpoint,
    }


def saved_research_source(path):
    """Read checkpoint launch settings without opening a historical transcript."""

    requested = Path(path).expanduser().absolute()
    source = requested if requested.is_dir() else requested.parent
    source = validated_continuation_source(source, source.parent)
    settings = {}
    settings_path = source / JOB_SETTINGS_FILENAME
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read saved author settings: {exc}") from exc
        if not isinstance(settings, dict):
            raise ValueError("Saved author settings must be an object.")
    replaced = []
    for role in ("author", "critic", "writer"):
        if f"{role}Model" in settings:
            saved = settings[f"{role}Model"]
            settings[f"{role}Model"] = restored_model(saved)
            if settings[f"{role}Model"] != saved:
                replaced.append(role)
    defaults = default_prompts()
    if settings.get("fileManagement") is False:
        defaults["author"] = defaults["author_simple"]
    for role in ("author", "critic", "final"):
        path = source / "prompts" / f"{role}.txt"
        settings[f"{role}Prompt"] = (
            read_utf8(path, f"saved {role} prompt") if path.exists()
            else defaults[role]
        )
    return {"run_dir": source, "statement": saved_statement(source), "settings": settings,
            "replacedRoles": replaced}


class Server(ThreadingHTTPServer):
    """Keep independent jobs alive while browser tabs come and go."""

    # On Windows SO_REUSEADDR lets a second server bind a port that is already
    # listening, so two servers would own the same jobs. Without it a second
    # launch fails with "address in use"; POSIX keeps quick restarts.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, runs=RUNS):
        self.runs = Path(runs)
        from .batches import BatchCatalog
        # The catalog refuses a symlinked root itself; a linked runs folder
        # (for example on another disk) still works for jobs and restores.
        root = self.runs.resolve()
        self.batch_catalog = BatchCatalog(root, current_root=root)
        self.jobs, self.jobs_lock = {}, threading.RLock()
        self._retry_stop = threading.Event()
        super().__init__(address, Handler)
        # Key-bearing Claude settings left by runners that were killed.
        claude_cli.sweep_stale_settings()
        self.jobs.update(restore_saved_jobs(self.runs))
        # Only the unguessable launch URL can create this server's session.
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://{HOST}:{self.server_port}"

    def _retry_paused_jobs(self, now=None):
        with self.jobs_lock:
            for run_id, app in list(self.jobs.items()):
                if self._retry_stop.is_set():
                    return
                try:
                    app.auto_resume_if_due(lambda: self.resume_paused_job(run_id), now)
                except Exception as exc:
                    # Nothing one job raises may stop the other jobs' retries:
                    # report it on that job, which tries again after the usual wait.
                    app.defer_auto_resume(exc, now)

    def serve_forever(self, poll_interval=0.5):
        # No browser tab or polling request is needed to retry a paused job.
        def retry_loop():
            while not self._retry_stop.wait(1):
                try:
                    self._retry_paused_jobs()
                except Exception:
                    # Every paused job depends on this one thread: keep it alive.
                    traceback.print_exc()

        worker = threading.Thread(target=retry_loop, daemon=True)
        worker.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            self._retry_stop.set()
            worker.join(timeout=5)

    def shutdown(self):
        self._retry_stop.set()
        super().shutdown()

    def server_close(self):
        self._retry_stop.set()
        super().server_close()

    def get_job(self, run_id):
        """Return one exact job."""

        with self.jobs_lock:
            app = self.jobs.get(run_id)
        if not app:
            raise ValueError("That job is not available in this TCS Prover session.")
        return app

    def start_job(self, body, run_id=""):
        """Create a new job, or revise the selected existing job."""

        app = self.get_job(run_id) if run_id else App(runs=self.runs, load_latest=False)
        legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
        review_only_option = (
            {"review_only": body.get("statementReviewOnly")}
            if "statementReviewOnly" in body else {}
        )
        app.start_review(
            statement=body.get("statement", ""),
            feedback=body.get("feedback", ""),
            critic_rounds=body.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
            review_model=body.get("reviewModel", DEFAULT_REVIEW_MODEL),
            thinking_hours=body.get("thinkingHours", DEFAULT_THINKING_HOURS),
            author_model=body.get("authorModel", DEFAULT_AUTHOR_MODEL),
            critic_model=body.get("criticModel", DEFAULT_CRITIC_MODEL),
            writer_model=body.get("writerModel", DEFAULT_WRITER_MODEL),
            reasoning_effort=legacy_effort,
            review_effort=body.get("reviewEffort", DEFAULT_REVIEW_EFFORT),
            author_effort=body.get("authorEffort", legacy_effort),
            critic_effort=body.get("criticEffort", legacy_effort),
            writer_effort=body.get("writerEffort", legacy_effort),
            **web_prompt_options(body, ("review", "author", "critic", "final"),
                                 app.state if run_id else None),
            speed_mode=body.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=body.get(
                "reasoningSummary", DEFAULT_REASONING_SUMMARY
            ),
            **review_only_option,
            research_audits=body.get("researchAudits", app.state.get("researchAudits") if run_id else None),
            file_management=body.get("fileManagement", app.state.get("fileManagement", True) if run_id else False),
            retrieval_mode=body.get("retrievalMode", app.state.get("retrievalMode", "live") if run_id else "live"),
            retrieval_cutoff_utc=body.get("retrievalCutoffUtc", app.state.get("retrievalCutoffUtc", "") if run_id else ""),
            # A named job is revised in its own folder, never re-registered.
            revise_existing=bool(run_id),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app


    @staticmethod
    def _direct_options(body):
        """Translate one /direct (or folder batch) body into new-job author settings."""

        legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
        return {
            "retrieval_mode": body.get("retrievalMode", "live"),
            "retrieval_cutoff_utc": body.get("retrievalCutoffUtc", ""),
            "critic_rounds": body.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
            "thinking_hours": body.get("thinkingHours", DEFAULT_THINKING_HOURS),
            "author_model": body.get("authorModel", DEFAULT_AUTHOR_MODEL),
            "critic_model": body.get("criticModel", DEFAULT_CRITIC_MODEL),
            "writer_model": body.get("writerModel", DEFAULT_WRITER_MODEL),
            "reasoning_effort": legacy_effort,
            "author_effort": body.get("authorEffort", legacy_effort),
            "critic_effort": body.get("criticEffort", legacy_effort),
            "writer_effort": body.get("writerEffort", legacy_effort),
            **web_prompt_options(body, ("author", "critic", "final")),
            "research_audits": body.get("researchAudits"),
            "file_management": body.get("fileManagement", False),
            "speed_mode": body.get("speedMode", DEFAULT_SPEED),
            "reasoning_summary": body.get("reasoningSummary", DEFAULT_REASONING_SUMMARY),
        }

    def start_direct_job(self, body, run_id=""):
        """Create a statement job that starts directly with the author."""

        if run_id:
            raise ValueError("Start a direct proof from the home screen.")
        app = App(runs=self.runs, load_latest=False)
        app.start_direct_statement(
            statement=body.get("statement", ""), **self._direct_options(body),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def start_direct_batch(self, body, run_id=""):
        """Start one direct proof job per statement, all sharing one set of settings.

        Every statement and the shared settings are validated before any job
        starts. Each job is an ordinary /direct job (statement review skipped)
        in its own run folder, and the jobs run in parallel.
        """

        if run_id:
            raise ValueError("Start a folder of statements from the home screen.")
        items = body.get("statements")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_STATEMENTS:
            raise ValueError(
                f"Send between 1 and {MAX_BATCH_STATEMENTS} statements in one folder batch."
            )
        statements = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ValueError(f"Statement {index} must have a name and a statement.")
            try:
                name = source_file_name(item.get("name"))
            except ValueError as exc:
                raise ValueError(f"Statement {index}: {exc}") from exc
            label = name or f"Statement {index}"
            statement = item.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError(f"{label} has no statement.")
            if "\0" in statement:
                raise ValueError(f"{label} contains a NUL character.")
            if len(statement) > MAX_BATCH_STATEMENT_CHARS:
                raise ValueError(
                    f"{label} is longer than {MAX_BATCH_STATEMENT_CHARS:,} characters."
                )
            statements.append((name, statement.strip()))
        options = self._direct_options(
            {key: value for key, value in body.items() if key != "statements"}
        )
        # The jobs share these settings: reject them once, before any job starts.
        normalized = App._workflow_options(include_review=False, **options)
        options.update(retrieval_mode=normalized["retrievalMode"], retrieval_cutoff_utc=normalized["retrievalCutoffUtc"])
        historical = normalized["retrievalMode"] == "historical"
        if historical and len(statements) > 10:
            raise ValueError("Historical search supports at most 10 statements per launch so every sandbox can be checked before work starts.")
        # ``current`` indexes the statement being handled; None means a step
        # shared by the whole historical batch.
        started, current = [], 0
        try:
            for current, (name, statement) in enumerate(statements):
                app = App(runs=self.runs, load_latest=False)
                app.start_direct_statement(statement, source_file=name, _prepare_only=historical, **options)
                started.append(app)
                with self.jobs_lock:
                    self.jobs[app.state["runId"]] = app
            if historical:
                # No model has started. Check every role boundary, with bounded
                # transport timeouts and only two concurrent native probes.
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    checks = [pool.submit(retrieval.preflight_policy, app.state["retrievalPolicyPath"])
                              for app in started]
                    try:
                        for current, check in enumerate(checks):
                            check.result()
                    finally:
                        for check in checks:
                            check.cancel()  # Like map(): skip checks not yet started.
                current = None
                retrieval.preflight_compiler(started[0].state["retrievalPolicyPath"])
                for current, app in enumerate(started):
                    with app.lock:
                        app.state["launchHold"] = ""
                        app._save_job_settings(app.state)
                        app.start_prepared_run()
        except Exception as exc:
            # A launch failed after validation (for example, the disk is full). Stop
            # the jobs this request started so a retry cannot duplicate them.
            stopped = held = 0
            for app in started:
                prepared = app.state.get("phase") == "prepared"
                held, stopped = held + prepared, stopped + (not prepared)
                try:
                    if prepared:
                        app.state["launchHold"] = "Historical batch launch failed. Create a new batch."
                        app._save_job_settings(app.state)
                    else:
                        app.stop()
                except (OSError, ValueError):
                    pass
            target = "the historical batch" if current is None else (
                "the job for " + (statements[current][0] or f"statement {current + 1}")
            )
            detail = str(exc).rstrip(".") or type(exc).__name__
            message = f"Could not start {target}: {detail}."
            if stopped:
                message += f" The {stopped} job{'s' if stopped != 1 else ''} it had already started were stopped."
            if held:
                message += (f" The {held} prepared job{'s are' if held != 1 else ' is'} "
                            "held and will not start.")
            raise ValueError(message) from exc
        return started

    def start_latex_job(self, body, run_id=""):
        """Create a job that runs only the final LaTeX editor."""

        if run_id:
            raise ValueError("Start LaTeX-only editing from the home screen.")
        app = App(runs=self.runs, load_latest=False)
        legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
        app.start_latex_only(
            source=body.get("content", ""),
            retrieval_mode=body.get("retrievalMode", "live"),
            retrieval_cutoff_utc=body.get("retrievalCutoffUtc", ""),
            writer_model=body.get("writerModel", DEFAULT_WRITER_MODEL),
            reasoning_effort=legacy_effort,
            writer_effort=body.get("writerEffort", legacy_effort),
            **web_prompt_options(body, ("final",)),
            speed_mode=body.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=body.get(
                "reasoningSummary", DEFAULT_REASONING_SUMMARY
            ),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def start_saved_critic_job(
        self, source_run, settings=None, include_audit_checkpoint=True,
    ):
        """Create a browser-visible job from one saved complete proof."""

        settings = dict(settings or {})
        source = saved_critic_source(source_run)
        with self.jobs_lock:
            self._check_author_workspace_idle(source["run_dir"])
        app = App(runs=self.runs, load_latest=False)
        app.start_critic_resume(
            statement=source["statement"],
            solution=source["solution"],
            source_run=source["run_dir"],
            critic_rounds=settings.get(
                "criticRounds", DEFAULT_CRITIC_ROUNDS
            ),
            thinking_hours=settings.get(
                "thinkingHours", DEFAULT_THINKING_HOURS
            ),
            author_model=settings.get("authorModel", DEFAULT_AUTHOR_MODEL),
            critic_model=settings.get("criticModel", DEFAULT_CRITIC_MODEL),
            writer_model=settings.get("writerModel", DEFAULT_WRITER_MODEL),
            reasoning_effort=settings.get(
                "reasoningEffort", DEFAULT_REASONING_EFFORT
            ),
            author_effort=settings.get("authorEffort"),
            critic_effort=settings.get("criticEffort"),
            writer_effort=settings.get("writerEffort"),
            author_prompt=settings.get(
                "authorPrompt", source["author_prompt"]
            ),
            critic_prompt=settings.get(
                "criticPrompt", source["critic_prompt"]
            ),
            final_prompt=settings.get("finalPrompt", source["final_prompt"]),
            speed_mode=settings.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=settings.get(
                "reasoningSummary", DEFAULT_REASONING_SUMMARY
            ),
            audit_checkpoint=(
                source["audit_checkpoint"] if include_audit_checkpoint else ""
            ),
            recover_audit_checkpoint=include_audit_checkpoint,
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def start_saved_research_job(self, source_run, settings=None):
        """Continue saved author work with its original prompt and a fresh budget."""

        source = saved_research_source(source_run)
        with self.jobs_lock:
            self._check_author_workspace_idle(source["run_dir"])
        options = {**source["settings"], **(settings or {})}
        app = App(runs=self.runs, load_latest=False)
        app.start_direct_statement(
            source["statement"],
            critic_rounds=options.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
            thinking_hours=options.get("thinkingHours", DEFAULT_THINKING_HOURS),
            author_model=options.get("authorModel", DEFAULT_AUTHOR_MODEL),
            critic_model=options.get("criticModel", DEFAULT_CRITIC_MODEL),
            writer_model=options.get("writerModel", DEFAULT_WRITER_MODEL),
            reasoning_effort=options.get("reasoningEffort", DEFAULT_REASONING_EFFORT),
            author_effort=options.get("authorEffort"),
            critic_effort=options.get("criticEffort"),
            writer_effort=options.get("writerEffort"),
            author_prompt=options.get("authorPrompt"),
            critic_prompt=options.get("criticPrompt"),
            final_prompt=options.get("finalPrompt"),
            speed_mode=options.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=options.get("reasoningSummary", DEFAULT_REASONING_SUMMARY),
            continuation_source=source["run_dir"], stopped_stage="solve",
            allow_external_source=True,
            research_audits=options.get("researchAudits"),
            file_management=options.get("fileManagement", True),
        )
        warning = removed_model_warning(
            role for role in source.get("replacedRoles", ()) if f"{role}Model" not in (settings or {}))
        if warning:
            with app.lock:
                app.state["settingsWarning"] = warning
                app.add_trace({"kind": "status", "stage": "solve", "node": "author",
                               "label": "Saved model replaced", "text": warning})
        self._queue_saved_author_instructions(source["run_dir"], app)
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def _check_author_workspace_idle(self, source_run):
        """Keep two author processes from updating one shared workspace."""

        workspace = saved_goal_options(source_run)["goalWorkspace"]
        for previous in self.jobs.values():
            if not previous.run_dir or not (
                previous.has_active_worker()
                or previous.state.get("auditHoldingAuthor")
                or previous.state["phase"] in {"reviewing", "running", "stopping", "pausing"}
            ):
                continue
            previous_workspace = previous.state.get("goalWorkspace") or str(previous.run_dir.resolve())
            if str(Path(previous_workspace).resolve()) == workspace:
                raise ValueError("Stop the source workspace's active job before continuing its author.")

    def resume_paused_job(self, run_id):
        """Resume in place, preventing concurrent authors in the same workspace."""

        with self.jobs_lock:
            app = self.get_job(run_id)
            self._check_author_workspace_idle(app.run_dir)
            app.resume()
        return app

    def start_research_audit_job(self, run_id):
        """Reserve a paused workspace before launching its auditors."""

        with self.jobs_lock:
            app = self.get_job(run_id)
            if app.state["phase"] == "paused":
                self._check_author_workspace_idle(app.run_dir)
            app.start_research_audit_now()
        return app

    def resume_critic_job(self, run_id, include_audit_checkpoint=True):
        """Resume an idle job from its saved proof using its role settings."""

        source_app = self.get_job(run_id)
        source_state = source_app.snapshot()
        if source_state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
            raise ValueError(
                "Stop this job before starting another critic from its proof."
            )
        settings = {
            key: source_state.get(key) for key in (
                "criticRounds", "thinkingHours", "authorModel",
                "criticModel", "writerModel", "reasoningEffort",
                "authorEffort", "criticEffort", "writerEffort",
                "speedMode", "reasoningSummary", "authorPrompt",
            )
        }
        return self.start_saved_critic_job(
            source_app.run_dir, settings,
            include_audit_checkpoint=include_audit_checkpoint,
        )

    def resume_checkpoint_job(self, run_id, checkpoint_id):
        """Continue one explicit durable checkpoint in a new browser job."""

        source_app = self.get_job(run_id)
        source_state = source_app.snapshot()
        if source_state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
            raise ValueError("Stop this job before continuing a checkpoint.")
        checkpoint = next(
            (
                item for item in source_state.get("checkpoints", [])
                if item.get("id") == checkpoint_id
            ),
            None,
        )
        if not checkpoint or not checkpoint.get("resumable"):
            raise ValueError("That checkpoint is not available for continuation.")
        return self.resume_critic_job(
            run_id,
            include_audit_checkpoint=checkpoint_id != "candidate",
        )

    @staticmethod
    def _stopped_continuation_plan(source_app, state=None):
        """Describe a durable restart for manual stops, exhaustion, or errors."""

        state = source_app.snapshot() if state is None else state
        if (
            state.get("phase") in {"reviewing", "running", "stopping", "pausing"}
            or source_app.has_active_worker()
        ):
            return None
        if state.get("phase") == "paused" and not (
                state.get("sessionUnavailable") and state.get("stage") in {"solve", "repair"}
                and not state.get("retrievalPolicyPath") and state.get("retrievalMode") != "historical"):
            return {"action": "resume", "label": "Resume", "description": "Continue in the same run folder using the saved author session and notebooks."}
        if state.get("phase") == "prepared":
            if state.get("launchHold"):
                return None
            return {"action": "prepared", "label": "Start prepared run",
                    "description": "Start a fresh author session in this prepared research workspace."}
        if state.get("retrievalPolicyPath") or state.get("retrievalMode") == "historical":
            # Every other continuation creates a new run, which a historical
            # run's fixed workspace and cutoff do not allow.
            return None
        stage = state.get("stoppedStage") or state.get("stage")
        if (
            source_app.run_dir and stage in {"solve", "repair", "failure"}
            and not (source_app.run_dir / "final.tex").is_file()
            and state.get("problemMode") != "critic-resume"
        ):
            try:
                saved_statement(source_app.run_dir)
            except ValueError:
                pass
            else:
                return {
                    "action": "author",
                    "label": "Continue proof author",
                    "description": (
                        "Reopens the same author workspace and resumes the saved "
                        "LLM session when available. Otherwise the author continues "
                        "in a new session using the YAML recovery prompt. The new run renews "
                        "the saved time budget."
                    ),
                }
        if (
            stage in {"solve", "repair", "failure"}
            and state.get("problemMode") == "critic-resume"
        ):
            if not state.get("checkpoints"):
                return None
            return {
                "action": "critic",
                "label": "Continue from critic",
                "description": (
                    "Restores the saved critic checkpoint so any required "
                    "author repair receives its exact recovery assignment."
                ),
            }
        if not state.get("manuallyStopped"):
            return None
        if stage == "review" and state.get("reviewStatement", "").strip():
            legacy_input_warning = (
                " This older run did not separately save its exact review "
                "feedback; continuation uses the saved draft with no feedback."
                if not state.get("reviewInputRecorded") else ""
            )
            return {
                "action": "review",
                "label": "Retry statement review",
                "description": (
                    "Starts a new statement-review request with the saved draft, "
                    "role settings, and prompt." + legacy_input_warning
                ),
            }
        if stage in {"solve", "repair", "failure"}:
            author_ready = False
            if state.get("problemMode") == "algorithmic":
                author_ready = all(
                    str(state.get(key, "")).strip()
                    for key in (
                        "modelOfComputation", "problemDescription", "goal",
                    )
                )
            else:
                try:
                    saved_statement(source_app.run_dir)
                    author_ready = True
                except ValueError:
                    pass
            if author_ready:
                return {
                    "action": "author",
                    "label": "Continue proof author",
                    "description": (
                        "Continues the author in the same workspace, resuming the "
                        "saved LLM session when available. "
                        "A new run renews the configured time budget. "
                        "Previously sent live instructions are queued again."
                    ),
                }
        if stage == "critic" and state.get("checkpoints"):
            return {
                "action": "critic",
                "label": "Continue critic",
                "description": (
                    "Restores the saved proof for a fresh critic call and "
                    "independent subagents in a new run with a renewed time budget."
                ),
            }
        if stage == "final":
            if state.get("problemMode") == "latex":
                ready = (source_app.run_dir / "latex-input.md").is_file()
            else:
                ready = bool(state.get("finalInputReady"))
            if ready:
                return {
                    "action": "final",
                    "label": "Retry LaTeX editor",
                    "description": (
                        "Starts only a new LaTeX-editor request from the exact "
                        "saved final input."
                    ),
                }
            if state.get("checkpoints"):
                return {
                    "action": "critic",
                    "label": "Continue from critic",
                    "description": (
                        "No exact final input was saved by this older job; "
                        "continues from its latest safe critic checkpoint."
                    ),
                }
        return None

    @staticmethod
    def _queue_saved_author_instructions(source_app, target_app):
        """Replay prior user steering into a newly created author process."""

        source_directory = source_app.run_dir if isinstance(source_app, App) else source_app
        instructions = saved_author_instructions(source_directory)
        if not instructions:
            return
        combined = (
            "\n\n".join(instructions)
        )
        command_id = target_app._write_author_steer(combined)
        target_app.add_trace({
            "kind": "status",
            "stage": target_app.state.get("stage") or "solve",
            "node": "author",
            "label": "Restored live instructions queued",
            "text": combined,
            "restoredInstructions": instructions,
            "steerId": command_id,
        })

    def continue_stopped_job(self, run_id):
        """Continue a saved stage with new logs and the existing author workspace."""

        # Match delete_job's jobs -> app lock order while registering the new job.
        with self.jobs_lock:
            source_app = self.get_job(run_id)
            with source_app.lock:
                return self._continue_stopped_job_locked(source_app)

    def _continue_stopped_job_locked(self, source_app):
        """Create a continuation while holding the source job lock."""

        validated_continuation_source(source_app.run_dir, self.runs)
        source_state = source_app.snapshot()
        plan = self._stopped_continuation_plan(source_app, source_state)
        if plan is None:
            raise ValueError(
                "This job has no saved stage available to continue."
            )
        if plan["action"] == "resume":
            return self.resume_paused_job(source_state["runId"])
        if plan["action"] == "prepared":
            self._check_author_workspace_idle(source_app.run_dir)
            source_app.start_prepared_run()
            return source_app
        if plan["action"] == "critic":
            continued = self.resume_critic_job(
                source_state["runId"], include_audit_checkpoint=True,
            )
            if isinstance(continued, App):
                self._queue_saved_author_instructions(source_app, continued)
            return continued

        app = App(runs=self.runs, load_latest=False)
        source_run = source_app.run_dir
        common = {
            "speed_mode": source_state["speedMode"],
            "reasoning_summary": source_state["reasoningSummary"],
            "continuation_source": source_run,
            "stopped_stage": source_state.get("stoppedStage") or source_state.get("stage", ""),
        }
        if plan["action"] == "review":
            app.start_review(
                statement=source_state["reviewStatement"],
                feedback=source_state["reviewFeedback"],
                critic_rounds=source_state["criticRounds"],
                thinking_hours=source_state["thinkingHours"],
                review_model=source_state["reviewModel"],
                author_model=source_state["authorModel"],
                critic_model=source_state["criticModel"],
                writer_model=source_state["writerModel"],
                reasoning_effort=source_state["reasoningEffort"],
                review_effort=source_state["reviewEffort"],
                author_effort=source_state["authorEffort"],
                critic_effort=source_state["criticEffort"],
                writer_effort=source_state["writerEffort"],
                review_prompt=source_state["reviewPrompt"],
                author_prompt=source_state["authorPrompt"],
                critic_prompt=source_state["criticPrompt"],
                final_prompt=source_state["finalPrompt"],
                review_only=source_state["statementReviewOnly"],
                file_management=source_state.get("fileManagement", True),
                research_audits=source_state.get("researchAudits"),
                **common,
            )
        elif plan["action"] == "author":
            self._check_author_workspace_idle(source_run)
            author_options = {
                "critic_rounds": source_state["criticRounds"],
                "thinking_hours": source_state["thinkingHours"],
                "author_model": source_state["authorModel"],
                "critic_model": source_state["criticModel"],
                "writer_model": source_state["writerModel"],
                "reasoning_effort": source_state["reasoningEffort"],
                "author_effort": source_state["authorEffort"],
                "critic_effort": source_state["criticEffort"],
                "writer_effort": source_state["writerEffort"],
                "author_prompt": source_state["authorPrompt"],
                "critic_prompt": source_state["criticPrompt"],
                "final_prompt": source_state["finalPrompt"],
                "file_management": source_state.get("fileManagement", True),
                "research_audits": source_state.get("researchAudits"),
                **common,
            }
            app.start_direct_statement(
                statement=saved_statement(source_run),
                **author_options,
            )
            self._queue_saved_author_instructions(source_app, app)
        else:
            if source_state["problemMode"] == "latex":
                final_source = read_utf8(
                    source_run / "latex-input.md", "saved LaTeX input",
                )
            else:
                final_input = saved_final_input(source_run)
                final_source = (
                    f"STATEMENT:\n{final_input['statement']}\n\n"
                    f"PROOF:\n{final_input['solution']}"
                )
            if source_state["problemMode"] == "latex":
                app.start_latex_only(
                    source=final_source,
                    writer_model=source_state["writerModel"],
                    reasoning_effort=source_state["reasoningEffort"],
                    writer_effort=source_state["writerEffort"],
                    final_prompt=source_state["finalPrompt"],
                    **common,
                )
            else:
                app.start_final_resume(
                    statement=final_input["statement"],
                    solution=final_input["solution"],
                    writer_model=source_state["writerModel"],
                    critic_model=source_state["criticModel"],
                    critic_effort=source_state["criticEffort"],
                    reasoning_effort=source_state["reasoningEffort"],
                    writer_effort=source_state["writerEffort"],
                    final_prompt=source_state["finalPrompt"],
                    **common,
                )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def job_list(self):
        """Return only the fields needed by the home-page job switcher."""

        with self.jobs_lock:
            apps = list(self.jobs.values())
        jobs = []
        for app in apps:
            state = app.snapshot()
            job = {
                key: state[key] for key in (
                    "runId", "phase", "draft", "activeNode", "startedAt",
                    "finishedAt", "lastActivityAt", "error", "problemMode",
                    "manuallyStopped", "stoppedStage",
                    "settingsWarning",
                    "launchHold", "retrievalPolicyPath", "retrieval", "retrievalMode", "retrievalCutoffUtc",
                    "resultKind", "stage", "workerActive", "processAlive", "activeElapsedSeconds",
                )
            }
            job["sourceFile"] = state.get("sourceFile") or ""
            # A paused job whose auditors still hold its workspace is busy.
            job["auditHoldingAuthor"] = bool(state.get("auditHoldingAuthor"))
            job["stageLabel"] = state["workflow"]["nodes"].get(state["activeNode"], {}).get("label", "")
            job["title"] = (
                state["problemDescription"].strip().split("\n", 1)[0]
                if state["problemMode"] == "algorithmic"
                else state["draft"].strip().split("\n", 1)[0]
            )
            run_dir = app.run_dir
            checkpoints = state.get("checkpoints", [])
            historical = bool(state.get("retrievalPolicyPath") or state.get("retrievalMode") == "historical")
            if historical:
                # Critic continuations start new runs, which historical runs refuse.
                # Copy: the snapshot shares these dicts with the job's cache.
                checkpoints = [{**checkpoint, "resumable": False} for checkpoint in checkpoints]
            job["checkpoints"] = checkpoints
            job["canResumeCritic"] = bool(
                not historical
                and state["phase"] not in {"reviewing", "running", "stopping", "pausing", "paused"}
                and run_dir
                and (
                    (run_dir / "SOLUTION.md").is_file()
                    or (run_dir / runtime.SAVED_CANDIDATE_FILENAME).is_file()
                )
                and (run_dir / "checked-statement.md").is_file()
            )
            continuation = self._stopped_continuation_plan(app, state)
            job["canContinueStopped"] = continuation is not None
            job["continueStoppedLabel"] = (
                continuation["label"] if continuation else ""
            )
            job["continueStoppedDescription"] = (
                continuation["description"] if continuation else ""
            )
            jobs.append(job)
        return sorted(jobs, key=lambda job: job["startedAt"], reverse=True)

    def delete_job(self, run_id):
        """Remove an idle job and move its private folder to local trash."""

        with self.jobs_lock:
            app = self.jobs.get(run_id)
            if not app:
                raise ValueError("That job is not available in this TCS Prover session.")
            with app.lock:
                engine = app.research_audits
                if (
                    app.state["phase"] in {"reviewing", "running", "stopping", "pausing"}
                    or app.worker_token is not None
                    # Auditors of a paused author still read and write its folder.
                    or app.state.get("auditHoldingAuthor") or app._audit_pause_engine is not None
                    or (engine is not None and engine.status().get("batchActive"))
                ):
                    raise ValueError("Stop this job before deleting it.")
                folder = app.run_dir
                if not folder or folder.resolve().parent != self.runs.resolve():
                    raise ValueError("The job folder is outside the runs directory.")
                for other in self.jobs.values():
                    if other is app or not (
                        other.has_active_worker() or other.state.get("auditHoldingAuthor")
                        or other.state["phase"] in {"reviewing", "running", "stopping", "pausing"}
                    ):
                        continue
                    workspace = other.state.get("goalWorkspace")
                    if workspace and Path(workspace).resolve() == folder.resolve():
                        raise ValueError("Stop the continuation using this author workspace before deleting it.")
                trash = self.runs / ".trash"
                trash.mkdir(parents=True, exist_ok=True, mode=0o700)
                trash.chmod(0o700)
                target, number = trash / folder.name, 2
                while target.exists():
                    target = trash / f"{folder.name}-{number}"
                    number += 1
                if engine is not None:
                    # An idle scheduler stays attached after Stop; detach it so no
                    # later audit event recreates the deleted folder.
                    engine.close()
                    app.research_audits = app._audit_token = None
                folder.rename(target)
                del self.jobs[run_id]
        return {"deleted": run_id}

    def api_model_listing(self):
        """Saved API models (never their keys) and the role menus that include them."""

        return {"models": providers.load_api_models(), "formats": API_MODEL_FORMATS, **model_menu_settings()}

    def _api_model_jobs(self, model_id):
        """Run ids of the jobs naming one model: (running, waiting to start or resume)."""

        active, waiting = [], []
        with self.jobs_lock:
            apps = list(self.jobs.values())
        for app in apps:
            with app.lock:
                if not any(app.state.get(f"{role}Model") == model_id for role in ROLES):
                    continue
                if (app.state["phase"] in ACTIVE_PHASES or app.worker_token is not None
                        or app.state.get("auditHoldingAuthor")):
                    active.append(app.state.get("runId", ""))
                elif app.state["phase"] in WAITING_PHASES:
                    waiting.append(app.state.get("runId", ""))
        return active, waiting

    def _removed_api_models(self):
        """Removed API models that saved jobs still name: {id: (formats, settings)}.

        Formats come from every job naming the id; settings from a job's own
        key-free snapshot, when it recorded one.
        """

        with self.jobs_lock:
            apps = list(self.jobs.values())
        removed = {}
        for app in apps:
            with app.lock:
                snapshot = saved_api_models(app.state.get("apiModels"))
                for role in ROLES:
                    model = app.state.get(f"{role}Model")
                    if not providers.is_api_id(model) or providers.is_known(model):
                        continue
                    formats, settings = removed.setdefault(model, (set(), {}))
                    if model in snapshot:
                        formats.add(snapshot[model]["format"])
                        settings.setdefault("value", snapshot[model])
        return {model: (formats, settings.get("value")) for model, (formats, settings) in removed.items()}

    def save_api_model(self, body):
        """Create or update one API model; an empty apiKey keeps the saved key
        while the format, base URL and header name stay the same.

        A new model with exactly the settings a job recorded for a removed API
        model gets that model's id back, whatever its display name, so the job
        can continue. A new model never takes a removed id that jobs recorded
        with another format: the format chooses the CLI of their sessions.
        """

        requested = body.get("id")
        reserved = set()
        with self.jobs_lock:
            saved = providers.api_model(requested) if requested else None
            if (saved is not None and body.get("format") in providers.FORMATS
                    and body["format"] != saved["format"] and any(self._api_model_jobs(requested))):
                # The format chooses the CLI, and a saved author session belongs to one CLI.
                raise ValueError(
                    "The API format decides which CLI runs this model, so it cannot change while "
                    "jobs use it. Add a new API model for the other format."
                )
            if saved is None:
                entry, _ = providers.validated_api_model(body)
                removed = self._removed_api_models()
                if not requested:
                    same = sorted(model for model, (_, settings) in removed.items() if settings and all(
                        settings[key] == entry[key] for key in ("format", "baseUrl", "model"))
                        and settings["headerName"].lower() == entry["headerName"].lower())
                    if same:
                        settings = removed[same[0]][1]
                        body = {**body, "id": same[0], "label": body.get("label") or settings["label"]}
                reserved = {model for model, (formats, _) in removed.items() if formats - {entry["format"]}}
                if body.get("id") in reserved:
                    raise ValueError(
                        f"Jobs still use {body['id']} as an API model of another format, which decides the CLI "
                        "of their sessions. Choose the format they recorded."
                    )
            model = providers.save_api_model(body, reserved=reserved)
        return {"model": model, **self.api_model_listing()}

    def delete_api_model(self, body):
        """Remove one API model unless a running job uses it."""

        model_id = body.get("id")
        if not providers.is_api_id(model_id):
            raise ValueError("Choose a configured API model to remove.")
        with self.jobs_lock:
            saved = providers.api_model(model_id)
            if saved is None:
                raise ValueError("That API model is not configured.")
            active, waiting = self._api_model_jobs(model_id)
            if active:
                raise ValueError(
                    f"{saved['label']} is in use by a running job. Stop that job before removing "
                    "this API model."
                )
            providers.delete_api_model(model_id)
        warning = ""
        if waiting:
            count = len(waiting)
            warning = (
                f"{count} job{'s' if count != 1 else ''} waiting to start or resume still "
                f"use{'s' if count == 1 else ''} {saved['label']}; {'it' if count == 1 else 'they'} "
                "cannot continue until you add it again with the same format, base URL, header "
                "name and model ID."
            )
        return {"deleted": model_id, "warning": warning, **self.api_model_listing()}

    def probe_api_model(self, body):
        """Test a saved API model ({id}) or unsaved form fields with one request.

        Form fields with the id of a saved model fill gaps from that model; an
        empty key is filled in only while the format, base URL and header name
        are the saved ones, so the saved key never goes to another destination.
        Only a test of exactly the saved settings is recorded as its last test.
        """

        requested = body.get("id") or None
        fields = {key: body[key] for key in ("format", "baseUrl", "headerName", "model", "apiKey", "label")
                  if key in body}
        if requested and not fields and providers.api_model(requested) is None:
            raise ValueError("That API model is not configured.")
        entry, saved = providers.validated_api_model(fields, requested)
        result = probe_api_endpoint(entry)
        request_fields = ("format", "baseUrl", "headerName", "model", "apiKey")
        result["recorded"] = False
        if saved is not None and all(saved[key] == entry[key] for key in request_fields):
            try:
                _, current = providers.validated_api_model({}, requested)
                if current is not None and all(current[key] == entry[key] for key in request_fields):
                    result["apiModel"] = providers.record_api_model_test(requested, {
                        "ok": result["ok"], "status": result["status"], "message": result["message"],
                        "latencyMs": result["latencyMs"], "at": result["testedAt"],
                    })
                    result["recorded"] = True
            except providers.ProviderError:
                pass  # Removed or changed during the test: nothing to record.
        return result

    def stop_all(self):
        """Interrupt every live child when the local server itself closes."""

        with self.jobs_lock:
            apps = list({id(app): app for app in self.jobs.values()}.values())
        for app in apps:
            with app.lock:
                app._audit_pause_engine = None
                app.state["auditHoldingAuthor"] = False
                if app.research_audits is not None:
                    app.research_audits.close()
                process = app.process
            if process is None:
                continue
            # One job that cannot be stopped must not leave the others running.
            try:
                if os.name == "nt":
                    # Windows Popen cannot send SIGINT, and runners in their own
                    # process group never see the console's Ctrl-C.
                    stop_process_tree(process)
                else:
                    # SIGINT keeps the runner's graceful goal-pause cleanup.
                    process.send_signal(signal.SIGINT)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass


class Handler(BaseHTTPRequestHandler):
    """Serve the local UI and its job-specific JSON endpoints."""

    def log_message(self, *_):
        pass

    def send(self, body, content_type="application/json", status=200, headers=None):
        """Send one complete response."""

        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        """Accept only this exact local origin and its secret request header."""

        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return False
        token = self.headers.get("X-TCS-Prover-Token", "")
        return bool(token) and secrets.compare_digest(token, self.server.token)

    def do_GET(self):
        """Return state or one allow-listed static file."""

        request = urlsplit(self.path)
        query = parse_qs(request.query, keep_blank_values=True)
        run_id = (query.get("job") or [""])[0]
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            return self.send({"error": "Untrusted local host."}, status=403)
        if any(len(values) != 1 for values in query.values()):
            return self.send({"error": "Duplicate request parameters are not allowed."}, status=400)
        batch = (query.get("batch") or [None])[0]
        if request.path == "/batches" or (batch is not None and request.path not in {"/", "/app.js", "/styles.css"}):
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            if request.path == "/batches":
                cards = self.server.batch_catalog.list_batches()
                root = next((card for card in cards if card["id"] == "__root__"), None)
                jobs = self.server.job_list()
                if root is None and jobs:
                    root = {"id": "__root__", "name": "Runs", "folder": "."}
                    cards.insert(0, root)
                if root is not None:
                    counts = {}
                    for row in jobs:
                        phase = row.get("phase") or "unknown"
                        counts[phase] = counts.get(phase, 0) + 1
                    root.update(jobCount=len(jobs), counts=counts, current=True, readOnly=False)
                return self.send({"batches": cards, "currentBatch": "__root__"})
            if batch != "__root__":
                try:
                    body, kind, headers = self.server.batch_catalog.response(
                        request.path, batch, run_id,
                        {key: values[0] for key, values in query.items()})
                    if isinstance(body, Path):
                        with self.server.batch_catalog.open_artifact(batch, run_id, body.name) as stream:
                            self.send_response(200)
                            self.send_header("Content-Type", kind)
                            self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                            self.send_header("Cache-Control", "no-store")
                            for name, value in headers.items():
                                self.send_header(name, value)
                            self.end_headers()
                            shutil.copyfileobj(stream, self.wfile)
                        return
                    return self.send(body, kind, headers=headers)
                except (ValueError, OSError):
                    return self.send({"error": "Unknown batch, job, or saved artifact."}, status=404)
        if request.path in {"/download-tex", "/download-pdf"}:
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                app = self.server.get_job(run_id)
                pdf = request.path == "/download-pdf"
                return self.send(app.final_pdf() if pdf else app.final_tex(),
                                 "application/pdf" if pdf else "application/x-tex; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="final.{"pdf" if pdf else "tex"}"'})
            except (ValueError, OSError) as exc:
                return self.send({"error": str(exc)}, status=400)
        if request.path == "/approach-graph":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                return self.send(self.server.get_job(run_id).approach_graph())
            except ValueError as exc:
                return self.send({"error": str(exc)}, status=400)
        if request.path == "/memory":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                name = (query.get("file") or [""])[0]
                version = (query.get("version") or [""])[0]
                return self.send(self.server.get_job(run_id).memory_file(name, version))
            except ValueError as exc:
                return self.send({"error": str(exc)}, status=400)
        if request.path == "/state":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                if not run_id:
                    state = empty_state()
                    state["traceFrom"] = 0
                    return self.send(state)
                values = query.get("after")
                after = int(values[0]) if values else None
                return self.send({**self.server.get_job(run_id).snapshot(after), "batchId": "__root__"})
            except ValueError:
                return self.send({"error": "Invalid job or transcript position."}, status=400)
        if request.path == "/jobs":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            return self.send({"jobs": [{**row, "batchId": "__root__"} for row in self.server.job_list()]})
        if request.path == "/api-models":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            return self.send(self.server.api_model_listing())
        if request.path == "/transcript":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                path = self.server.get_job(run_id).trace_file
            except ValueError as exc:
                return self.send({"error": str(exc)}, status=404)
            if not path or not path.exists():
                return self.send(b"", "application/x-ndjson")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)
            return
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            # Linked from index.html, so the browser's own icon request finds it.
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        if request.path not in files or (request.query and request.path != "/"):
            return self.send({"error": "Not found."}, status=404)
        name, kind = files[request.path]
        self.send((UI / name).read_bytes(), kind)

    def do_POST(self):
        """Call one workflow action and return its new state."""

        try:
            request = urlsplit(self.path)
            query = parse_qs(request.query, keep_blank_values=True)
            run_id = (query.get("job") or [""])[0]
            if not self.authorized():
                return self.send({"error": "Unauthorized local request."}, status=403)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError("Duplicate request parameters are not allowed.")
            batch = (query.get("batch") or [None])[0]
            if batch is not None and batch != "__root__":
                return self.send({"error": "Saved batches are read-only."}, status=403)
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a JSON request.")
            size = int(self.headers.get("Content-Length", "0"))
            limit = MAX_DOCUMENT_REQUEST_BYTES if request.path == "/direct-batch" else MAX_REQUEST_BYTES
            if not 0 <= size <= limit:
                raise ValueError("Request is too large.")
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            # API models belong to this installation, not to one job; no
            # response ever contains a key.
            api_model_actions = {
                "/api-models": self.server.save_api_model,
                "/api-models/delete": self.server.delete_api_model,
                "/api-models/test": self.server.probe_api_model,
            }
            if request.path in api_model_actions:
                return self.send(api_model_actions[request.path](body))
            if request.path == "/delete-job":
                return self.send(self.server.delete_job(run_id))
            if request.path == "/direct-batch":
                started = self.server.start_direct_batch(body, run_id)
                return self.send({
                    "batchId": "__root__",
                    "startedJobs": [app.state["runId"] for app in started],
                    "jobs": self.server.job_list(),
                })
            if request.path == "/resume":
                app = self.server.resume_paused_job(run_id)
            elif request.path == "/start-research-audit":
                app = self.server.start_research_audit_job(run_id)
            elif request.path == "/resume-critic":
                app = self.server.resume_critic_job(run_id)
            elif request.path == "/resume-checkpoint":
                app = self.server.resume_checkpoint_job(
                    run_id, str(body.get("checkpoint", "")),
                )
            elif request.path == "/continue-stopped":
                app = self.server.continue_stopped_job(run_id)
            elif request.path == "/review":
                app = self.server.start_job(body, run_id)
            elif request.path == "/direct":
                app = self.server.start_direct_job(body, run_id)
            elif request.path == "/finalize":
                app = self.server.start_latex_job(body, run_id)
            else:
                app = self.server.get_job(run_id)
            if request.path == "/approve":
                app.approve(body.get("statement"))
            elif request.path == "/set-author-time-limit":
                app.set_author_time_limit(body.get("hours"))
            elif request.path == "/set-research-audits":
                app.set_research_audits(body)
            elif request.path == "/steer-author":
                app.steer_author(body.get("instruction"))
            elif request.path == "/stop":
                app.stop()
            elif request.path == "/pause":
                app.pause()
            elif request.path == "/reset":
                app.reset()
            elif request.path == "/clear-trace":
                app.clear_trace()
            elif request.path not in {
                "/review", "/direct", "/finalize",
                "/resume-critic", "/resume-checkpoint", "/continue-stopped", "/resume", "/start-research-audit",
            }:
                return self.send({"error": "Not found."}, status=404)
            self.send({**app.snapshot(), "batchId": "__root__"})
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            # Includes runtime.Error and RetrievalPolicyError: their messages
            # are written for the user and reach only this authorized client.
            self.send({"error": str(exc)}, status=400)
        except Exception as exc:
            # Answer rather than drop the connection ("Failed to fetch").
            traceback.print_exc()
            self.send({"error": f"Unexpected server error: {exc}"}, status=500)
