"""Manage proof jobs and serve the local browser interface."""

import decimal
import errno
import getpass
import hashlib
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
import weakref
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, providers
from . import ownership, related_works, retrieval
from .review import REVIEW_PROMPT


# Files and model settings shared by the UI and its worker processes.
ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
RUNS = ROOT / "runs"
WORKFLOWS = ROOT / "workflows"
HOST, PORT = "127.0.0.1", 8765
DEFAULT_CRITIC_ROUNDS = runtime.DEFAULT_CRITIC_ROUNDS
MAX_CRITIC_ROUNDS = runtime.MAX_CRITIC_ROUNDS
# Independent critics per critic round (workflow_runner.py --critic-count): the subagents the critic starts; the
# runner's default (from author_critic.yaml) and limit, or 3 and 10 when the runner does not define them.
try:
    DEFAULT_CRITIC_COUNT = runtime.DEFAULT_CRITIC_COUNT
except Exception:  # noqa: BLE001 - an edited workflow file must never keep the server from starting.
    DEFAULT_CRITIC_COUNT = 3
MAX_CRITIC_COUNT = getattr(runtime, "MAX_CRITIC_COUNT", 10)
# Subagents the proof author may start at a time (workflow_runner.py --author-subagents); 0 gives it no subagent tool.
try:
    DEFAULT_AUTHOR_SUBAGENTS = runtime.DEFAULT_AUTHOR_SUBAGENTS
except Exception:  # noqa: BLE001 - an edited workflow file must never keep the server from starting.
    DEFAULT_AUTHOR_SUBAGENTS = 0
MAX_AUTHOR_SUBAGENTS = getattr(runtime, "MAX_AUTHOR_SUBAGENTS", 10)
# Long diffs of the critic coordinator's changes, in the run's controller folder (see App.critic_diff).
CRITIC_DIFFS_FOLDER = "critic-diffs"
CRITIC_DIFF_NAME = re.compile(r"round-[0-9]+-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.diff")
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
# A closing server waits this long for paused authors and interrupted runners before stopping them.
SHUTDOWN_WAIT_SECONDS = 15
# Stop interrupts a POSIX runner group this long before killing it, so the
# runner's cleanup deletes any key-bearing Claude settings file it holds.
STOP_GRACE_SECONDS = 3
# A paused author may finish its current step for the runner's interrupt grace;
# only a runner that has not ended this long after a pause is closed.
PAUSE_ACKNOWLEDGE_SECONDS = runtime.INTERRUPT_GRACE_SECONDS + 15
WINDOWS_EVERYONE_SID = "*S-1-1-0"
AUTHOR_LIMIT_FILENAME = "author-limit.json"
AUTHOR_STEER_FILENAME = "author-steer.json"
# The whole stderr of the author's Codex app-server (redacted), across attempts.
AUTHOR_STDERR_FILENAME = "codex-app-server.log"
# Earlier live instructions a continued author reads when they do not fit in one instruction.
INSTRUCTION_HISTORY_FILENAME = "USER_INSTRUCTIONS.md"
PAUSE_FILENAME = "pause.json"
PAUSE_REQUEST_FILENAME = "pause-request.json"
JOB_SETTINGS_FILENAME = "job-settings.json"
MANUAL_STOP_FILENAME = "manual-stop.json"
CONTINUATION_SOURCE_FILENAME = "continuation-source.json"
REVIEW_INPUT_FILENAME = "review-input.json"
MAX_REQUEST_BYTES = 100_000
# Every request that creates or starts a job (one statement, a folder batch,
# LaTeX to polish, an edited statement to approve) may be this large, and so
# may each statement in it, so a statement accepted in a folder is accepted
# on its own too.
MAX_DOCUMENT_REQUEST_BYTES = 8 * 1024 * 1024
JOB_REQUEST_PATHS = {"/review", "/direct", "/direct-batch", "/start-workflow", "/finalize", "/approve", "/solve",
                     "/uploads/note"}
MAX_BATCH_STATEMENTS = 200
MAX_BATCH_STATEMENT_CHARS = 2_000_000
MAX_STATEMENT_CHARS = MAX_BATCH_STATEMENT_CHARS
# The body of a refused request is read and dropped up to this size, so the
# browser receives the reason instead of a reset connection.
MAX_DISCARDED_REQUEST_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILE_CHARS = 255
# An error-paused author is resumed automatically after the same wait as
# every other model-call retry (workflow_runner.MODEL_RETRY_SECONDS).
AUTO_RESUME_SECONDS = runtime.MODEL_RETRY_SECONDS
# The server starts jobs by itself (automatic resumes, queued folder-batch jobs)
# at most this often, so jobs that came due together reach the provider spread
# out instead of in the same second.
AUTO_LAUNCH_SPACING_SECONDS = 2
# Installation settings beside the API model store (the running-jobs limit).
INSTALLATION_SETTINGS = providers.INSTALLATION_SETTINGS
MAX_RUNNING_JOBS = 1000
# How the runner's own messages begin when the workflow budget runs out
# ("Workflow time limit reached." or "...; saved work remains resumable.").
# A provider or CLI error that merely mentions a time limit is still retried.
WORKFLOW_TIME_LIMIT_ERROR = "Workflow time limit reached"
# The phases in which a job with an Author stage takes a new time limit (it applies at the
# author's next step, or at launch or Resume).
TIME_LIMIT_PHASES = {"reviewing", "reviewed", "prepared", "running", "pausing", "paused"}
# Resume needs at least this much of the time limit left, so a resumed author never pauses at once.
RESUME_MARGIN_SECONDS = 60
# Talking to a paused author: one turn in its saved conversation (workflow_runner.py --converse).
CONVERSATION_FILENAME = "conversation.json"
PROGRESS_SUMMARY_FILENAME = "progress-summary.md"
MAX_CONVERSATION_CHARS = runtime.CONVERSATION_MAX_CHARS  # The runner refuses a longer request.
CONVERSATION_ID = re.compile(r"c_[0-9a-f]{10}\Z")
CONVERSATION_KINDS = ("summary", "message")
CONVERSATION_STATUSES = ("queued", "running", "answered", "failed", "stopped")
CONVERSATION_BUSY = "The author is replying. Wait for the reply or stop it."
CONVERSATION_STAGES = {"review": False, "author": True, "critic": False, "writing": False}
# Related works can be added while the job can still reach its author.
RELATED_WORKS_PHASES = {"reviewing", "reviewed", "prepared", "running", "pausing", "paused"}
# The transcript stage of controller records about related works. Like conversation records,
# a restart skips them: they say nothing about where the workflow is.
RELATED_WORKS_STAGE = "related-works"
# The author's questions to the user (Codex's request_user_input_async). The author does not wait for
# an answer: once the turn in which it asked has ended and this long has passed since it asked, it no
# longer waits for one, and the question closes. Controller records about a question have their own
# transcript stage, which a restart skips like the conversation's.
AUTHOR_QUESTION_WAIT_SECONDS = 600
AUTHOR_QUESTION_STAGE = "author-question"
AUTHOR_QUESTION_CLOSED = "The author's question is no longer open."
# Pauses that a restart of TCS Prover causes ("restart") or finds ("interrupted"): the author continues
# its conversation by itself after them, so its open question stays open.
RESTART_PAUSE_CAUSES = {"restart", "interrupted"}
# A folder batch ("Run a folder…") shares one related-works store among its jobs.
FOLDER_BATCH_ID = re.compile(r"b_[0-9a-f]{10}\Z")
# Staged uploads older than a day are removed at most this often (and when the server starts).
UPLOAD_PRUNE_SECONDS = 3600
ROLES = ("review", "author", "critic", "writer")
# The role whose model makes the calls of each transcript stage.
TRACE_STAGE_ROLES = {"review": "review", "solve": "author", "repair": "author", "critic": "critic", "final": "writer"}
ROLE_LABELS = {
    "review": "statement reviewer", "author": "proof author",
    "critic": "critic", "writer": "LaTeX editor",
}
# Jobs whose model processes are running or about to run.
ACTIVE_PHASES = {"reviewing", "running", "stopping", "pausing"}
# What a job takes from its saved files when its output reader failed (App._settle_from_saved_run).
SETTLED_FIELDS = ("phase", "stage", "activeNode", "error", "autoResumeAt", "output", "resultKind", "finishedAt",
                  "elapsedSeconds", "resumedAt", "sessionUnavailable", "safetyStop", "manuallyStopped",
                  "stoppedStage", "finalInputReady", "goalThreadId", "revisionRequired", "criticBugs")
# Holds that "Re-run preflight" may release once a historical job's checks pass.
HISTORICAL_PENDING_HOLD = "Historical batch preflight is pending; every run must pass before launch."
HISTORICAL_RETRY_SUFFIX = " The check runs again every minute."
HISTORICAL_FIX_ADVICE = " Choose Re-run preflight after fixing it."
HISTORICAL_RECHECK_HOLDS = ("Historical batch preflight", "Historical batch launch failed",
                            "Historical search preflight failed", "Historical compiler preflight failed",
                            "Historical run check", "Needs attention:")


def recheckable_hold(text):
    return isinstance(text, str) and text.startswith(HISTORICAL_RECHECK_HOLDS)


def historical_checks(stages):
    """What a historical launch check covers: the compiler only when the run writes LaTeX."""
    return "policy, logins, sandboxes and compiler" if stages["writing"] else "policy, logins and sandboxes"


# Idle jobs that launch again with their saved models (Resume, Approve, Start).
WAITING_PHASES = {"paused", "reviewed", "prepared"}
# The API model Test button sends a request shaped like the ones runs send
# (streamed, with one tool and a JSON answer, at the configured effort) and
# follows no redirect; a minimal one follows only when that one is refused.
API_TEST_TIMEOUT_SECONDS = 30
API_TEST_PROMPT = "Reply with OK."
API_TEST_MAX_TOKENS = 16
API_TEST_MAX_RESPONSE_BYTES = 256 * 1024
API_TEST_STRUCTURED_PROMPT = (
    'This is a connection test. Answer with exactly this JSON object and nothing else: {"ok": true}'
)
API_TEST_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                   "additionalProperties": False}
# Real runs ask for long answers; an endpoint that caps the output lower fails them.
API_TEST_RUN_OUTPUT_TOKENS = 32000
API_MODELS_MAX_RESPONSE_BYTES = 1024 * 1024
API_MODELS_MAX_PAGES = 100
API_MODELS_MAX_COUNT = 5000
ANTHROPIC_VERSION = "2023-06-01"


def load_installation_settings():
    """This installation's settings: how many jobs may run at once (0: no limit); defaults when unreadable."""

    try:
        value = json.loads((providers.state_directory() / INSTALLATION_SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    limit = value.get("maxRunningJobs") if isinstance(value, dict) else None
    valid = isinstance(limit, int) and not isinstance(limit, bool) and 0 <= limit <= MAX_RUNNING_JOBS
    return {"maxRunningJobs": limit if valid else 0}


def save_installation_settings(values):
    """Validate and save this installation's settings (owner-only, beside the API model store)."""

    limit = values.get("maxRunningJobs") if isinstance(values, dict) else None
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit.strip())
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_RUNNING_JOBS:
        raise ValueError(f"Enter how many jobs may run at once: 0 for no limit, or at most {MAX_RUNNING_JOBS}.")
    folder = providers.private_directory(providers.state_directory())
    runtime._private_atomic_write(folder / INSTALLATION_SETTINGS, json.dumps({"maxRunningJobs": limit}) + "\n")
    return {"maxRunningJobs": limit}


class BatchStart(list):
    """The jobs a folder batch created, with a note about any that wait or could not start yet."""

    notice = ""
    # The folder batch's ID, which names its shared related works (see Server.add_batch_related_works).
    folder_batch = ""


def settings_problem(text):
    """Whether a failure says the model cannot be used or the endpoint refuses the requests (see workflow_runner)."""

    text = str(text or "")
    return runtime.MODEL_UNAVAILABLE_LABEL in text or runtime.REQUEST_REJECTED_LABEL in text


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
        checkpoint = json.loads(controlled_path(run_dir, PAUSE_FILENAME).read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"The saved pause checkpoint ({PAUSE_FILENAME}) is damaged: {exc}") from exc
    if not complete_pause_checkpoint(checkpoint):
        raise ValueError(f"The saved pause checkpoint ({PAUSE_FILENAME}) is damaged: it lacks the saved "
                         "workflow state, node, stage or elapsed time")
    return checkpoint


def finite_seconds(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def saved_seconds(value, default):
    """A saved non-negative number of seconds, or `default` for anything else."""

    return float(value) if finite_seconds(value) else default


def complete_pause_checkpoint(checkpoint):
    """Whether a checkpoint holds everything Resume needs: workflow state, node, stage and elapsed time."""

    return (isinstance(checkpoint, dict) and isinstance(checkpoint.get("state"), dict)
            and all(isinstance(checkpoint.get(key), str) and checkpoint[key] for key in ("node", "stage"))
            and finite_seconds(checkpoint.get("elapsedSeconds")))


# What a damaged checkpoint still decides when it is rebuilt from the transcript.
DAMAGED_CHECKPOINT_FIELDS = {
    "pauseCause": lambda value: value in {"error", "user", "interrupted", "restart", "time-limit"},
    "autoResumeAt": finite_seconds,
    "error": lambda value: isinstance(value, str),
    "elapsedSeconds": finite_seconds,
    "goalThreadId": lambda value: isinstance(value, str) and "\0" not in value,
    "sessionUnavailable": lambda value: isinstance(value, bool),
    "safetyStop": lambda value: isinstance(value, bool),
    "criticFeedbackPending": lambda value: isinstance(value, bool),
    "summaryRequested": lambda value: isinstance(value, bool),
}


def read_pause_object(run_dir):
    """Read pause.json for display and routing; a missing or damaged file reads as {}.

    Only Resume needs a valid checkpoint, and it checks one with load_pause_checkpoint.
    """

    try:
        checkpoint = json.loads(controlled_path(run_dir, PAUSE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return checkpoint if isinstance(checkpoint, dict) else {}


def span_text(seconds):
    """A duration in words ("45 s", "2 h 5 min"), as the runner writes it."""

    return runtime._span_text(seconds)


def hours_text(hours):
    """A time limit as the page writes it: "1 hour", "1.5 hours", "1,000 hours" (at most two decimals).

    A limit under 0.01 hours keeps two significant digits ("0.003 hours"), so it never reads as 0.
    """

    value = float(hours)
    if 0 < value < 0.01:
        return f"{decimal.Decimal(f'{value:.2g}'):f} hours"
    value = round(value, 2)
    number = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{number} hour{'' if value == 1 else 's'}"


# The author time limit's history, as the job's Run settings list it (state["timeLimitHistory"]): every
# change of the limit ("set"), every pause at it ("reached"), the progress summary the author wrote at
# that pause ("summary") and the Resume after it ("resumed"). Built from the transcript records, so a
# restart rebuilds it, and saved with the job settings for saved batches, which read only recent records.
TIME_LIMIT_EVENTS = ("set", "reached", "summary", "resumed")


def follow_time_limit(history, record):
    """Add the time-limit event one transcript record states to ``history``; return whether it did."""

    if not isinstance(record, dict) or not isinstance(record.get("time"), str):
        return False
    kind, label, hours = record.get("kind"), record.get("label"), record.get("authorLimitHours")
    event = None
    if kind == "status" and label == "Total time limit set" and finite_seconds(hours):
        event = {"event": "set", "hours": float(hours)}
    elif kind == "status" and label == "Time limit reached" and finite_seconds(hours):
        event = {"event": "reached", "hours": float(hours),
                 "elapsedSeconds": saved_seconds(record.get("elapsedSeconds"), 0.0)}
    else:
        # Since the latest pause at the limit: its summary (the first one written), then its Resume.
        pause = next((index for index in range(len(history) - 1, -1, -1)
                      if history[index]["event"] == "reached"), None)
        since = {item["event"] for item in history[pause + 1:]} if pause is not None else set()
        if pause is None or "resumed" in since:
            return False
        if (kind == "conversation_reply" and record.get("conversationKind") == "summary"
                and "summary" not in since):
            event = {"event": "summary"}
        elif kind == "status" and label == "Resume requested":
            # The limit it resumes with: the latest one set, else the one it paused at.
            limit = next(item["hours"] for item in reversed(history) if "hours" in item)
            event = {"event": "resumed", "hours": limit}
    if event is None:
        return False
    history.append({**event, "time": record["time"]})
    return True


# The critic's progress records (workflow_runner.SubagentProgress and its SUBAGENT*_LABEL texts): one
# "requested" per attempt, then what its event stream shows of each of its independent critics (its
# subagents), or only that it waits for them.
CRITICS_REQUESTED = re.compile(r"(\d+) independent critics? requested")
CRITIC_STARTED_OR_FINISHED = re.compile(r"Independent critic (\d+) (started|finished)")
CRITICS_WAITING = runtime.SUBAGENTS_WAITING_LABEL


def _critic_progress(count, requested=False):
    """A critic round's progress before any of its critics showed up (see follow_critic_progress)."""

    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    return {"count": max(0, count), "requested": requested, "started": [], "finished": [], "failed": [],
            "waiting": False}


def follow_critic_progress(progress, record, critic_count=None):
    """The progress of the critic round that runs, after one more transcript record (None outside one).

    {"count": its independent critics, "requested": whether the critic's current attempt has asked for
    them, "started" and "finished": the 1-based numbers of those its event stream showed starting and
    ending, "failed": those among the finished that failed, "waiting": whether it waited for them (all that
    Codex shows of a web-cutoff call of multi-agent version 2)}. Every attempt starts its critics anew, and
    so their numbering; a failed attempt's critics are gone while it waits to retry. The job page names the step from it ("Critic 2 of 3 working"); the records themselves
    leave the page's window of recent events.
    """

    if not isinstance(record, dict) or record.get("root") is False or record.get("stage") == "conversation":
        return progress
    kind, label = record.get("kind"), record.get("label")
    label = label if isinstance(label, str) else ""
    if kind == "status" and label.startswith("Workflow step: "):
        return _critic_progress(critic_count) if record.get("node") == "critic" else None
    if progress is None:
        return None
    if kind == "critic_result":
        return None
    if (kind == "status" and record.get("stage") == "critic" and label.endswith(runtime.RETRY_LABEL)
            and finite_seconds(record.get("retryAt"))):
        # The critic's failed call waits to retry: its critics ended with it, and the retry starts them again.
        return {**progress, "started": [], "finished": [], "failed": [], "waiting": False}
    if kind != "status" or record.get("subagentProgress") is not True:
        return progress
    # A new dict for every change: a snapshot taken earlier keeps the one it copied.
    if CRITICS_REQUESTED.fullmatch(label):
        count = record.get("auditCount")
        known = isinstance(count, int) and not isinstance(count, bool)
        return _critic_progress(count if known else progress["count"], True)
    if label == CRITICS_WAITING:
        return progress if progress["waiting"] else {**progress, "waiting": True}
    event, index = CRITIC_STARTED_OR_FINISHED.fullmatch(label), record.get("auditIndex")
    if event is None or not isinstance(index, int) or isinstance(index, bool) or index < 1:
        return progress
    if event.group(2) == "started":
        return progress if index in progress["started"] else {**progress, "started": sorted([*progress["started"], index])}
    if index in progress["finished"]:
        return progress
    return {**progress, "started": sorted({*progress["started"], index}),
            "finished": sorted([*progress["finished"], index]),
            "failed": sorted([*progress["failed"], index]) if record.get("failed") is True else progress["failed"]}


def saved_time_limit_history(value):
    """The valid events of a saved time-limit history, oldest first ([] when it is damaged)."""

    if not isinstance(value, list):
        return []
    return [{key: item[key] for key in ("event", "time", "hours", "elapsedSeconds") if key in item}
            for item in value
            if isinstance(item, dict) and item.get("event") in TIME_LIMIT_EVENTS and isinstance(item.get("time"), str)
            and all(finite_seconds(item[key]) for key in ("hours", "elapsedSeconds") if key in item)
            and (item["event"] == "summary" or "hours" in item)]


def staged_uploads(runs):
    """The staging area of related-work uploads: runs/.uploads, owner-only, kept a day."""

    return related_works.UploadStage(Path(runs) / related_works.STAGING_FOLDER)


def folder_batch_store(runs, batch_id):
    """The related-works store of one folder batch: runs/.controller/.batches/<batch ID>."""

    if not isinstance(batch_id, str) or not FOLDER_BATCH_ID.match(batch_id):
        raise ValueError("That folder batch is not available in this TCS Prover session.")
    return related_works.BatchStore(related_works.batch_directory(Path(runs) / CONTROLLER_FOLDER, batch_id))


def related_works_tokens(value):
    """Validate the upload tokens a request attaches: a list of 1 to MAX_ITEMS strings."""

    if (not isinstance(value, list) or not 1 <= len(value) <= related_works.MAX_ITEMS
            or not all(isinstance(token, str) and token for token in value)):
        raise ValueError(f"Send the related works as a list of 1 to {related_works.MAX_ITEMS} upload tokens.")
    return value


def related_works_text(entries, *, batch=False):
    """The text of a "Related works added" record."""

    listed = "; ".join(related_works.describe_entry(entry) for entry in entries)
    where = "the folder batch's related works, linked into this job's" if batch else "the author's"
    return f"Added {listed} to {where} related-works folder."


def related_works_record(entries, *, batch=""):
    """A "Related works added" transcript record (a restart skips its stage)."""

    return {"kind": "status", "stage": RELATED_WORKS_STAGE, "label": "Related works added",
            "relatedWorks": [{"name": entry["name"], "originalName": entry.get("originalName", ""),
                              "sizeText": related_works.size_text(entry.get("size", 0)),
                              **({"batch": batch} if batch else {})} for entry in entries],
            "text": related_works_text(entries, batch=bool(batch)),
            **({"folderBatch": batch} if batch else {})}


def valid_conversation_item(item):
    """Whether one saved conversation item can be shown and continued."""

    return (isinstance(item, dict) and isinstance(item.get("id"), str) and bool(CONVERSATION_ID.match(item["id"]))
            and item.get("kind") in CONVERSATION_KINDS and item.get("status") in CONVERSATION_STATUSES
            and all(isinstance(item.get(key), str) for key in ("text", "reply", "error", "createdAt"))
            and finite_seconds(item.get("dueAt", 0)))


def controller_note(record):
    """Whether a transcript record is a controller note that says nothing about the workflow's progress.

    The paused author's replies (stage "conversation") and related-works
    records are written while a job waits or is paused; a restart skips them.
    """

    return isinstance(record, dict) and record.get("stage") in {"conversation", RELATED_WORKS_STAGE,
                                                                 AUTHOR_QUESTION_STAGE}


def saved_conversation_items(value):
    """The valid items of a saved conversation.json value, oldest first (none when it is damaged)."""

    items = value.get("items") if isinstance(value, dict) and value.get("schemaVersion") == 1 else None
    if not isinstance(items, list):
        return []
    loaded = []
    for item in items:
        if not valid_conversation_item(item):
            continue
        notice = item.get("notice") if isinstance(item.get("notice"), list) else []
        loaded.append({
            **{key: item[key] for key in ("id", "kind", "text", "status", "createdAt", "reply", "error")},
            "origin": "time-limit" if item.get("origin") == "time-limit" else "user",
            "dueAt": float(item.get("dueAt") or 0),
            "startedAt": item.get("startedAt") if isinstance(item.get("startedAt"), str) else "",
            "finishedAt": item.get("finishedAt") if isinstance(item.get("finishedAt"), str) else "",
            "attempts": item["attempts"] if type(item.get("attempts")) is int else 0,
            "notice": [name for name in notice if isinstance(name, str)],
            "compacted": item.get("compacted") is True})
    return loaded


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


def question_entries(value):
    """The valid [{"title", "options"}] of a question call, in order; options [] for free text only."""

    entries = []
    for entry in value if isinstance(value, list) else []:
        title = entry.get("title") if isinstance(entry, dict) else None
        if not isinstance(title, str) or not title.strip() or "\0" in title:
            continue
        options = entry.get("options") if isinstance(entry.get("options"), list) else []
        entries.append({"title": title.strip(), "options": [option.strip() for option in options
                                                            if isinstance(option, str) and option.strip()
                                                            and "\0" not in option]})
    return entries


def author_question(record):
    """{"id", "turnId", "questions"} of a question the running author asks the user, else None.

    Codex's request_user_input_async tool delivers it as an agent message
    with "delivery": "async" and its "questions", once when it starts and
    once when it completes. Only the root author's own records count: never a
    subagent's, a parallel call's or the paused author's conversation.
    """

    event = record.get("event") if record.get("kind") == "codex_event" else None
    if (not isinstance(event, dict) or record.get("root") is False or record.get("activityLabel")
            or record.get("stage") not in {"solve", "repair"} or record.get("node") not in {None, "author"}
            or event.get("method") not in {"item/started", "item/completed"}):
        return None
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    item = params.get("item")
    if (not isinstance(item, dict) or item.get("type") not in {"agentMessage", "agent_message"}
            or item.get("delivery") != "async" or not isinstance(item.get("id"), str) or not item["id"]):
        return None
    questions = question_entries(item.get("questions"))
    text = item.get("text")
    if not questions and isinstance(text, str) and text.strip() and "\0" not in text:
        questions = [{"title": text.strip(), "options": []}]
    turn = params.get("turnId")
    return {"id": item["id"], "turnId": turn if isinstance(turn, str) else "",
            "questions": questions} if questions else None


def saved_author_question(value):
    """The open question saved with a job's settings, or None when there is none or it is damaged."""

    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        return None
    questions = question_entries(value.get("questions"))
    asked, ended = value.get("askedAt"), value.get("turnEndedAt")
    if not questions or not finite_seconds(asked) or not (ended is None or finite_seconds(ended)):
        return None
    turn = value.get("turnId")
    return {"id": value["id"], "askedAt": float(asked), "turnId": turn if isinstance(turn, str) else "",
            "questions": questions, "turnEndedAt": None if ended is None else float(ended)}


def author_answer_text(questions, answers):
    """The live instruction that answers a question call; ``answers`` is [{"index", "answer"}].

    Empty answers are left out. One question: Answer to your question
    "<title>": <answer>. Several: "Answers to your questions:", then one
    line "<title>": <answer> per answered question, in the questions' order.
    """

    if not isinstance(answers, list):
        raise ValueError("Send the answers as a list, one per answered question.")
    given = {}
    for entry in answers:
        index = entry.get("index") if isinstance(entry, dict) else None
        answer = entry.get("answer") if isinstance(entry, dict) else None
        if (type(index) is not int or not 0 <= index < len(questions) or index in given
                or not isinstance(answer, str)):
            raise ValueError("Send each answer once, with the number of the question it answers.")
        if answer.strip():
            given[index] = answer.strip()
    if not given:
        raise ValueError("Choose an option or write an answer first.")
    if len(questions) == 1:
        return f'Answer to your question "{questions[0]["title"]}": {given[0]}'
    return "\n".join(["Answers to your questions:", *(f'"{questions[index]["title"]}": {given[index]}'
                                                       for index in sorted(given))])


def delivered_repair(record):
    """{"thread", "digest"} of critic feedback a root author status says its conversation received, else None."""

    digest, thread = record.get("repairDelivered"), goal_thread_from_record(record)
    if record.get("kind") == "status" and thread and isinstance(digest, str) and digest:
        return {"thread": thread, "digest": digest}
    return None


def isolated_process_options():
    """Put each Codex wrapper in a group that can be stopped as one unit."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class SleepWatch:
    """Machine sleep: wall time that passed while the awake clock (time.monotonic) stood still.

    The runner's budget clock is time.monotonic, which stops while the machine
    sleeps; the controller leaves the same sleep out of a job's active time.
    """

    MARGIN_SECONDS = 2.0

    def __init__(self):
        self._wall, self._awake = time.time(), time.monotonic()

    def slept(self):
        """Seconds asleep since the previous call; 0 below a small margin."""

        wall, awake = time.time(), time.monotonic()
        gap = (wall - self._wall) - (awake - self._awake)
        self._wall, self._awake = wall, awake
        return gap if gap > self.MARGIN_SECONDS else 0.0


def reap_runner(process):
    """Wait for a runner, then kill what it left behind in its process group.

    A descendant that outlived the runner (a double-forked compiler, say) is
    still in the group, and while it exists no other process gets its ID.
    """

    code = process.wait()
    pid = getattr(process, "pid", None)
    if os.name != "nt" and isinstance(pid, int) and not isinstance(pid, bool):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass  # Nothing was left.
    return code


def raise_open_file_limit():
    """Allow many parallel jobs: each holds pipes and owner locks, and macOS starts processes with 256."""

    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = 8192 if hard == resource.RLIM_INFINITY else min(8192, hard)
        if soft != resource.RLIM_INFINITY and soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (ImportError, ValueError, OSError):
        pass


def runner_lock_options(descriptors):
    """Hand a run's owner locks to its runner, so they outlive a crash of this process."""

    return {"pass_fds": tuple(descriptors)} if descriptors and os.name != "nt" else {}


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


def append_run_log(path, text, durable=False):
    """Append to a private run log, replacing anything planted in its place.

    A link, FIFO, or hard link is unlinked; a folder, or a log that can no
    longer be opened, is moved aside so no record is lost. Return whether the
    entry had to be replaced. ``durable`` also flushes the log to disk.
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
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
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
        "critic": author_critic["critic"],
        # The convention critic's prompt (its own step after the critic accepts); a template with {statement}.
        "convention": author_critic["convention_critic"],
        "final": runtime.builtin_workflow("clean_up")["prompts"]["final"],
    }
    _last_default_prompts = prompts
    return dict(prompts)  # Callers adjust their copy.


# Default prompts of earlier versions that no longer fit the workflow, by role. Every job saves its prompts, so
# a job created with one of these unchanged defaults gets the current default when it is resumed or continued
# (see current_prompt); a prompt the user edited stays as it is.
RETIRED_DEFAULT_PROMPTS = {
    # The coordinator of the controller-run parallel critics, which were replaced by the critic's own subagents:
    # it expected critic reports in its input, which no longer come.
    "critic": ((
        "Act as a rigorous proof critic. The controller runs several fresh, independent critics on the exact statement\n"
        "and candidate proof in parallel, each with a different auditing focus, and gives you their complete reports. The\n"
        "critics aggressively look for bugs and identify exactly which definitions and omitted arguments must be expanded\n"
        "to make the proof complete and rigorous.\n"
        "\n"
        "Examine their objections and decide which are valid. Try to fix every valid bug yourself by editing proof.md,\n"
        "defining missing terms and fully expanding omitted arguments. Reject only for a critical error that a critic\n"
        "found and that you confirm but cannot rigorously fix: then keep the safe fixes in proof.md and return\n"
        "verdict=reject with every unresolved bug and its exact location in bugs. Otherwise return verdict=pass and an\n"
        "empty bugs string. If no edits are needed, leave proof.md unchanged. Return only the requested JSON."),),
}


def current_prompt(role, text):
    """A saved prompt of ``role``, or the current default when it is a retired default (RETIRED_DEFAULT_PROMPTS)."""

    if isinstance(text, str) and text.strip() in {value.strip() for value in RETIRED_DEFAULT_PROMPTS.get(role, ())}:
        return default_prompts()[role]
    return text


def web_prompt_options(body, roles, source=None):
    """Accept only deliberate per-job edits; old browser defaults are ignored."""

    overrides = body.get("promptOverrides", {})
    selected = normalize_workflow_stages(body.get("workflowStages")) if "workflowStages" in body else (
        saved_workflow_stages(source) if source else normalize_workflow_stages())
    if not isinstance(overrides, dict) or any(
        name not in PROMPT_ROLES
        or (selected[ROLE_STAGES[PROMPT_ROLES[name]]] and not isinstance(value, str))
        for name, value in overrides.items()
    ):
        raise ValueError("Prompt overrides must map role names to prompt text.")
    return {
        f"{name}_prompt": overrides.get(name, (source or {}).get(f"{name}Prompt"))
        for name in roles
    }


TRACE_LIMIT = 1500
PINNED_KINDS = {
    "request", "review_result", "critic_result", "author_result",
    "final_result", "failure_result", "partial_result", "diagnostic", "error", "convention_result",
}
# Pinned milestones kept in memory: every result and approval, plus the first
# PINNED_HEAD and the latest of the others, PINNED_LIMIT in all. transcript.jsonl keeps every record.
PINNED_LIMIT, PINNED_HEAD, PINNED_SLACK = 2000, 200, 500
LASTING_KINDS = {"review_result", "critic_result", "author_result", "final_result", "failure_result",
                 "partial_result", "convention_result"}


def convention_verdict(record):
    """Whether one transcript record is a verdict of the convention critic (a root convention_result)."""

    return isinstance(record, dict) and record.get("kind") == "convention_result" and record.get("root") is not False


def convention_warning(record):
    """The loophole warning one convention critic record raises, or None.

    Only a verdict of loophole with its explanation and suggested statement
    warns (a faithful verdict shows only in the critic review history). The
    job keeps its latest verdict's warning: a later faithful verdict (on a
    later accepted proof) closes an earlier warning. The warning keeps its
    round and the record's time, which identify it: the user ignores this
    very warning, and a later loophole verdict shows again.
    """

    if not convention_verdict(record):
        return None
    fields = {key: record.get(key) for key in ("intended", "assessment", "loophole", "suggestedStatement")}
    if (record.get("verdict") != "loophole" or not all(isinstance(value, str) for value in fields.values())
            or not fields["loophole"].strip() or not fields["suggestedStatement"].strip()):
        return None
    number = record.get("round")
    return {"round": number if type(number) is int and number > 0 else None,
            "time": record.get("time") if isinstance(record.get("time"), str) else "",
            **fields, "dismissed": False}


def saved_convention_warning(value):
    """A loophole warning as job-settings.json saved it (convention_warning()), or None when it is not one."""

    fields = ("time", "intended", "assessment", "loophole", "suggestedStatement")
    if (not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in fields)
            or not value["loophole"].strip() or not value["suggestedStatement"].strip()):
        return None
    number = value.get("round")
    return {"round": number if type(number) is int and number > 0 else None,
            **{key: value[key] for key in fields}, "dismissed": value.get("dismissed") is True}


def same_convention_warning(saved, warning):
    """Whether a saved warning (job-settings.json) is this warning, so its dismissal still applies."""

    return (isinstance(saved, dict) and isinstance(warning, dict) and saved.get("dismissed") is True
            and saved.get("time") == warning["time"] and saved.get("round") == warning["round"])


def bounded_pinned(pinned):
    """Drop the oldest pinned milestones beyond the bound, never a result or an approval."""

    others = [index for index, record in enumerate(pinned)
              if record.get("kind") not in LASTING_KINDS and record.get("label") != "Critic approved"]
    if len(others) <= PINNED_LIMIT:
        return pinned
    dropped = set(others[PINNED_HEAD:len(others) - (PINNED_LIMIT - PINNED_HEAD)])
    return [record for index, record in enumerate(pinned) if index not in dropped]


def without_completed_deltas(records):
    """Leave out streamed fragments whose completed item is in the same records; it replaces them on the page."""

    def key(record, delta):
        event = record.get("event") if record.get("kind") == "codex_event" else None
        params = event.get("params") if isinstance(event, dict) else None
        if not isinstance(params, dict):
            return None
        if delta:
            kind = {"item/agentMessage/delta": "agent",
                    "item/reasoning/summaryTextDelta": "reasoning"}.get(event.get("method"))
            identity = params.get("itemId")
        else:
            item = params.get("item")
            kind = ({"agentMessage": "agent", "agent_message": "agent", "reasoning": "reasoning"}.get(item.get("type"))
                    if event.get("method") == "item/completed" and isinstance(item, dict) else None)
            identity = item.get("id") if kind else None
        return (str(record.get("activityLabel") or ""), kind, identity) if kind and isinstance(identity, str) else None

    completed = {key(record, False) for record in records} - {None}
    return [record for record in records if key(record, True) not in completed] if completed else records


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
    if kind == "status" and record.get("root") is not False and record.get("subagentProgress") is True:
        # A round's critics (the critic's subagents) requested, started or finished: a few records per
        # attempt, which the job page's progress line reads while the critic's own events fill the window.
        return True
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
        "review_reasoning_effort": DEFAULT_REVIEW_EFFORT,
        "revision_reasoning_effort": DEFAULT_REVIEW_EFFORT,
        "model_summary": "6.1 Sol/Max review · 6.1 Sol/Max author, critic, writer",
        "critic_rounds": {
            "default": DEFAULT_CRITIC_ROUNDS,
            "minimum": 1,
            "maximum": MAX_CRITIC_ROUNDS,
        },
        "critic_count": {
            "default": DEFAULT_CRITIC_COUNT,
            "minimum": 1,
            "maximum": MAX_CRITIC_COUNT,
        },
        "author_subagents": {
            "default": DEFAULT_AUTHOR_SUBAGENTS,
            "minimum": 0,
            "maximum": MAX_AUTHOR_SUBAGENTS,
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
            "description": "One critic starts independent critics as its subagents, weighs their reports and fixes "
                           "the valid issues. Unfixable issues return to the author.",
        },
        "convention": {
            "label": "Convention critic", "short_label": "Convention",
            "stage": "critic",
            "description": "Checks once, after the critic accepts the proof and before writing, whether the proof "
                           "does what a mathematician would want or exploits a loophole in the statement. It only "
                           "warns; the run continues either way.",
        },
        "latex_editor": {
            "label": "LaTeX editor", "short_label": "Polish", "stage": "final",
            "description": "Polishes the passing proof into a complete LaTeX document.",
        },
        "latex_compile": {
            "label": "Compile PDF", "short_label": "Compile", "stage": "final",
            "description": "Compiles LaTeX, fixes compilation errors, and repeats until the source and PDF are ready, "
                           "or until three attempts fail the same way; the source is then kept without a PDF.",
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
            "prompt_change": "Send the latest candidate to the critic, which starts fresh independent critics.",
        },
        {
            "from": "author", "to": "failure_summary",
            "label": "Preserve author work", "when": "the author is interrupted or its time budget expires",
            "prompt_change": "Resume the same author workspace and saved session.",
        },
        {
            "from": "critic", "to": "critic",
            "label": "Recheck repair",
            "when": "the critic fixes all bugs; at the round limit the recheck cannot edit",
            "prompt_change": "Send the repaired proof to the critic and new independent critics.",
        },
        {
            "from": "critic", "to": "author",
            "label": "Return bugs", "when": "critic rejects",
            "prompt_change": "Send unresolved bugs back to the proof author.",
        },
        {
            "from": "critic", "to": "convention",
            "label": "Check conventions", "when": "critic accepts the proof",
            "prompt_change": "Send the accepted proof and the statement to the convention critic.",
        },
        {
            "from": "convention", "to": "latex_editor",
            "label": "Format reviewed proof", "when": "the convention critic has answered, whatever its verdict",
            "prompt_change": "Send the independently reviewed solution to the LaTeX editor.",
        },
        {
            "from": "critic", "to": "failure_summary",
            "label": "Save verification checkpoint", "when": "the critic is interrupted",
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

WORKFLOW_STAGES = ("review", "author", "critic", "writing")
STAGE_ROLES = {"review": "review", "author": "author", "critic": "critic", "writing": "writer"}
ROLE_STAGES = {role: stage for stage, role in STAGE_ROLES.items()}
PROMPT_ROLES = {"review": "review", "author": "author", "critic": "critic", "convention": "critic", "final": "writer"}


def normalize_workflow_stages(value=None):
    """Validate the four ordered stage switches; omitted switches default on."""

    if value is None:
        return dict.fromkeys(WORKFLOW_STAGES, True)
    if (not isinstance(value, dict) or set(value) - set(WORKFLOW_STAGES)
            or any(not isinstance(enabled, bool) for enabled in value.values())):
        raise ValueError("Workflow stages must map review, author, critic and writing to enabled or disabled.")
    result = {stage: value.get(stage, True) for stage in WORKFLOW_STAGES}
    if not any(result.values()):
        raise ValueError("Select at least one workflow stage.")
    return result


def valid_author_subagents(value):
    """Require a whole number of author subagents, 0 (no subagent tool) to MAX_AUTHOR_SUBAGENTS."""

    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
        if not isinstance(value, str) and value != parsed:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("The number of author subagents must be an integer.") from exc
    if not 0 <= parsed <= MAX_AUTHOR_SUBAGENTS:
        raise ValueError(f"Choose 0 to {MAX_AUTHOR_SUBAGENTS} author subagents.")
    return parsed


def valid_critic_count(value):
    """Require a whole number of independent critics per critic round, 1 to MAX_CRITIC_COUNT."""

    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
        if not isinstance(value, str) and value != parsed:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("The number of independent critics must be an integer.") from exc
    if not 1 <= parsed <= MAX_CRITIC_COUNT:
        raise ValueError(f"Choose 1 to {MAX_CRITIC_COUNT} independent critics.")
    return parsed


def saved_workflow_stages(state):
    """Read explicit stage choices or recover the equivalent older workflow."""

    if "workflowStages" in state:
        return normalize_workflow_stages(state["workflowStages"])
    if state.get("problemMode") in {"latex", "final-resume"}:
        return {"review": False, "author": False, "critic": False, "writing": True}
    if state.get("statementReviewOnly"):
        return {"review": True, "author": False, "critic": False, "writing": False}
    return {"review": not state.get("skipStatementReview", False) and state.get("problemMode") not in {
        "algorithmic", "critic-resume"}, "author": True, "critic": True, "writing": True}


def workflow_graph(stages):
    """Only selected model stages and their meaningful transitions appear in the UI."""

    stages = normalize_workflow_stages(stages)
    if all(stages.values()):
        return PUBLIC_GRAPH
    enabled = []
    if stages["review"]:
        enabled.append("statement_reviewer")
    if stages["author"]:
        enabled.append("author")
    if stages["critic"]:
        enabled.extend(("critic", "convention"))
    if stages["writing"]:
        enabled.extend(("latex_editor", "latex_compile", "latex_repair"))
    nodes = {name: PUBLIC_GRAPH["nodes"][name] for name in enabled}
    if stages["author"] or stages["critic"]:
        nodes["failure_summary"] = PUBLIC_GRAPH["nodes"]["failure_summary"]
    edges = [edge for edge in PUBLIC_GRAPH["edges"] if edge["from"] in nodes and edge["to"] in nodes]
    sequence = [name for name in ("statement_reviewer", "author", "critic", "convention", "latex_editor")
                if name in nodes]
    for previous, following in zip(sequence, sequence[1:]):
        if not any(edge["from"] == previous and edge["to"] == following for edge in edges):
            edges.append({"from": previous, "to": following,
                          "label": "Approve statement" if previous == "statement_reviewer" else "Continue"})
    return {"settings": PUBLIC_GRAPH["settings"], "nodes": nodes, "edges": edges}


def checkpoint_roles(node, stages=None):
    """The model roles the steps after a paused step can use, which a relaunch checks.

    After the critic only the LaTeX writer remains. The critic's own step can
    reject to the author or pass to the writer, and the runner checks every
    model it is given when it starts, so it needs all three. Only the roles of
    the job's selected stages count (see saved_workflow_stages): a LaTeX-only
    job, for example, needs only the writer.
    """

    selected = normalize_workflow_stages(stages)
    roles = ("writer",) if node in LATEX_GRAPH["nodes"] else ("author", "critic", "writer")
    return tuple(role for role in roles if selected[ROLE_STAGES[role]])


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


# The controller's own folder of each run, beside the runs (hidden, so never a run or batch).
CONTROLLER_FOLDER = ".controller"


def controller_directory(run_dir):
    """The controller's folder for one run: runs/.controller/<run>, outside every model sandbox.

    The author can write its run folder, so the files that steer a run live
    here (author-limit.json, pause-request.json), and the controller keeps its
    own copies of the prompts, the checked statement, the job settings and the
    pause checkpoint, which it trusts over the run folder's. Those stay in the
    run folder too, for display and older tools.
    """

    run_dir = Path(run_dir)
    return run_dir.parent / CONTROLLER_FOLDER / run_dir.name


# The author's private temp folder in the controller's folder of its run, outside every workspace
# (the runner creates it owner-only; see runner/confinement.py).
RUN_TMP_FOLDER = "tmp"


TRASH_FOLDER = ".trash"
TRASH_KEPT_DAYS = 30
TRASH_INDEX = ".deleted-at.json"


def prune_trash(runs, now=None):
    """Remove the deleted jobs that have been in runs/.trash for TRASH_KEPT_DAYS days.

    The trash records when each job arrived; one without a record (deleted
    by an older version, or just now) starts its time when it is first seen.
    """

    trash, now = Path(runs) / TRASH_FOLDER, time.time() if now is None else now
    try:
        if trash.is_symlink() or not trash.is_dir():
            return
        try:
            recorded = json.loads((trash / TRASH_INDEX).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = {}
        recorded = recorded if isinstance(recorded, dict) else {}
        kept = {}
        for path in trash.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            arrived = recorded.get(path.name)
            if not isinstance(arrived, (int, float)) or isinstance(arrived, bool) or not math.isfinite(arrived):
                arrived = now
            if now - arrived >= TRASH_KEPT_DAYS * 24 * 3600:
                shutil.rmtree(path, ignore_errors=True)
            else:
                kept[path.name] = arrived
        if kept != recorded:
            runtime._private_atomic_write(trash / TRASH_INDEX, json.dumps(kept, sort_keys=True) + "\n")
    except (OSError, UnicodeError, ValueError):
        pass  # The trash is tidied again at the next deletion or start.


def controlled_path(run_dir, name):
    """The controller's copy of a run file when it has one (older runs have none), else the run folder's."""

    private = controller_directory(run_dir) / name
    try:
        if private.is_file() and not private.is_symlink():
            return private
    except OSError:
        pass
    return Path(run_dir) / name


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
        "workflowStages": normalize_workflow_stages(),
        "originalContent": "",
        "preparedRun": False,
        "launchHold": "",
        # Set while another process owns the run; nothing here may change it.
        "runningElsewhere": "",
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
        # The running author's newest open question to the user ({"id", "askedAt", "turnId", "questions",
        # "turnEndedAt"}), or None; App._author_question_locked closes it.
        "authorQuestion": None,
        # Why the job is paused ("error", "user", "time-limit", ...), from its checkpoint.
        "pauseCause": "",
        # The critic rejected the latest proof and the paused author has not received its report.
        "criticFeedbackPending": False,
        # The latest loophole warning of the convention critic (see convention_warning), or None.
        "conventionWarning": None,
        # A conversation turn compacted the author's context: the next Resume sends the full resume prompt.
        "pendingReanchor": False,
        # Related works added while paused; the author is told with the next message or at Resume.
        "relatedWorksPending": [],
        # The folder batch ("Run a folder…") whose related works this job shares, or "".
        "folderBatch": "",
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
        "authorPrompt": prompts["author"],
        "criticPrompt": prompts["critic"],
        "conventionPrompt": prompts["convention"],
        "finalPrompt": prompts["final"],
        "criticRounds": DEFAULT_CRITIC_ROUNDS,
        "criticCount": DEFAULT_CRITIC_COUNT,
        # Subagents the proof author may start at a time (0: no subagent tool).
        "authorSubagents": DEFAULT_AUTHOR_SUBAGENTS,
        # False for a saved job that never recorded its number of critics (older jobs): the job page
        # does not credit it with independent critics it may never have run.
        "criticCountRecorded": True,
        # False for a saved job from before the Author subagents setting, when the critic ran its independent
        # critics itself and the author always had the subagent tool: the job page does not describe it with
        # the newer settings.
        "authorSubagentsRecorded": True,
        "thinkingHours": DEFAULT_THINKING_HOURS,
        # The author time limit's changes and pauses at it (see follow_time_limit).
        "timeLimitHistory": [],
        # While a critic round runs: its independent critics, those finished, and whether the
        # coordinator was asked (see follow_critic_progress).
        "criticProgress": None,
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
    return {"model_catalog": catalog, "model_efforts": efforts, "model_sources": providers.source_catalog()}


def model_choice_error(model, role):
    """Explain why one role's model id cannot run."""

    label = ROLE_LABELS[role]
    if providers.replacement_for_removed(model):
        return (f"DeepSeek is no longer supported; choose another model for the {label}, "
                f"for example {runtime.MODEL}.")
    if providers.is_api_id(model):
        return (f"The API model {model} chosen for the {label} is not configured. Add it under "
                "Advanced settings → API connections, or choose another model.")
    shown = repr(model)[:80] if isinstance(model, str) and model else "(none)"
    return (f"Unknown model {shown} for the {label}. Choose {', '.join(OFFERED_MODELS)}, "
            "or an api:<id> model configured under Advanced settings → API connections.")


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
               f"model {saved['model']})")
    else:
        how = f"with the display name “{model[len(providers.API_ID_PREFIX):]}”"
    return (f"The API model {model} chosen for the {ROLE_LABELS[role]} is not configured. Add it again "
            f"under New problem → Advanced settings → API connections {how} to continue this job.")


class TransientLaunchError(ValueError):
    """A model cannot start right now for a reason that passes by itself (its CLI is updating)."""


def require_known_models(models):
    """Reject an unknown, removed or unconfigured model for any role in ``models``.

    An API model store that cannot be read right now is a TransientLaunchError,
    never an unconfigured model.
    """

    for role, model in models.items():
        try:
            known = isinstance(model, str) and providers.is_known(model)
        except providers.StoreUnavailable as exc:
            raise TransientLaunchError(str(exc)) from exc
        if not known:
            raise ValueError(model_choice_error(model, role))


def require_model_access(models):
    """Check locally that each model can start: a saved API key, or the Claude CLI."""

    try:
        for model in dict.fromkeys(models):
            runtime.verify_model_credentials(model)
    except runtime.CLIUnavailable as exc:
        raise TransientLaunchError(str(exc)) from exc
    except runtime.Error as exc:
        if isinstance(exc.__cause__, providers.StoreUnavailable):
            raise TransientLaunchError(str(exc)) from exc  # Unreadable for a moment, not removed.
        raise ValueError(str(exc)) from exc


API_SNAPSHOT_FIELDS = ("label", "format", "baseUrl", "headerName", "model", "requestSource", "sourceId", "sourceLabel")
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
    snapshots = {}
    for model, entry in list(value.items())[:len(ROLES)]:
        if (not providers.is_api_id(model) or not isinstance(entry, dict)
                or entry.get("format") not in providers.FORMATS or not all(
                    isinstance(entry.get(key), str) and len(entry[key]) <= providers.MAX_BASE_URL_CHARS
                    for key in ("label", "format", "baseUrl", "model"))):
            continue
        try:
            source = providers._request_source(entry.get("requestSource"))
        except providers.ProviderError:
            continue
        snapshots[model] = {**{key: entry[key] for key in ("label", "format", "baseUrl", "model")},
                            "headerName": providers.DEFAULT_HEADERS[entry["format"]], "requestSource": source,
                            "sourceId": entry.get("sourceId") if providers.is_api_id(entry.get("sourceId")) else model,
                            "sourceLabel": entry.get("sourceLabel") if isinstance(entry.get("sourceLabel"), str)
                            and len(entry["sourceLabel"]) <= providers.MAX_LABEL_CHARS else entry["label"]}
    return snapshots


def _api_connection_headers(entry):
    """Automatic protocol authentication and source for tests and model discovery."""

    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": providers.request_source(entry)}
    if entry["format"] == providers.ANTHROPIC_FORMAT:
        headers["anthropic-version"] = ANTHROPIC_VERSION
    header, key = providers.DEFAULT_HEADERS[entry["format"]], entry["apiKey"]
    headers[header] = f"Bearer {key}" if header.lower() == "authorization" else key
    return headers


def _api_endpoint_path(entry, endpoint):
    path = urlsplit(entry["baseUrl"]).path.rstrip("/")
    if entry["format"] == providers.ANTHROPIC_FORMAT:
        if path.lower().endswith("/v1"):
            path = path[:-3]
        path += "/v1"
    return path + "/" + endpoint


def _api_test_request(entry, effort=None):
    """Path, headers and body of a Test request.

    With an ``effort`` the request is shaped like the ones runs send: it
    streams its answer (server-sent events), offers one tool, asks for a JSON
    answer (Codex's prompt contract for OpenAI-format models, the structured
    output tool of the Claude CLI for Anthropic-format ones) at that effort,
    with the model's max output (Anthropic: what runs ask for when none is
    set). Without one it is the minimal request ("Reply with OK.", 16 tokens).
    """

    headers = _api_connection_headers(entry)
    if effort:
        headers["Accept"] = "text/event-stream"
    output = entry.get("maxOutputTokens")
    if entry["format"] == providers.OPENAI_FORMAT:
        # Codex sends Responses requests to <base URL>/responses.
        path = _api_endpoint_path(entry, "responses")
        body = {"model": entry["model"], "input": API_TEST_PROMPT,
                "max_output_tokens": API_TEST_MAX_TOKENS}
        if effort:
            body = {"model": entry["model"], "stream": True,
                    "input": [{"role": "user", "content": API_TEST_STRUCTURED_PROMPT}],
                    "reasoning": {"effort": effort, "summary": DEFAULT_REASONING_SUMMARY},
                    "tools": [{"type": "function", "name": "report_result", "strict": True,
                               "description": "Report the result of the connection test.",
                               "parameters": API_TEST_SCHEMA}],
                    "tool_choice": "auto", **({"max_output_tokens": output} if output else {})}
    else:
        # The Claude CLI takes the base URL without /v1 and adds /v1/messages.
        path = _api_endpoint_path(entry, "messages")
        body = {"model": entry["model"], "max_tokens": API_TEST_MAX_TOKENS,
                "messages": [{"role": "user", "content": API_TEST_PROMPT}]}
        if effort:
            body = {"model": entry["model"], "max_tokens": output or API_TEST_RUN_OUTPUT_TOKENS, "stream": True,
                    "messages": [{"role": "user", "content": API_TEST_STRUCTURED_PROMPT
                                  + " Give it by calling the StructuredOutput tool."}],
                    "tools": [{"name": claude_cli.STRUCTURED_OUTPUT_TOOL,
                               "description": "Return the final answer as JSON.", "input_schema": API_TEST_SCHEMA}],
                    "tool_choice": {"type": "auto"}, "output_config": {"effort": effort}}
    return path, headers, json.dumps(body).encode("utf-8")


def _api_test_stream(text):
    """What a streamed (server-sent events) reply says: (reply text, tool called, model, error message)."""

    reply, partial, tool, model, error = [], [], "", "", ""
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if kind in {"error", "response.failed"}:
            failure = event.get("error") or (event.get("response") or {}).get("error") or event
            error = _api_test_error({"error": failure} if isinstance(failure, dict) else event, str(failure))
        elif kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
            reply.append(event["delta"])
        elif kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            if event["item"].get("type") == "function_call":
                tool = str(event["item"].get("name") or "a tool")
                if isinstance(event["item"].get("arguments"), str):
                    partial.append(event["item"]["arguments"])
        elif kind in {"response.created", "response.completed"} and isinstance(event.get("response"), dict):
            model = event["response"].get("model") or model
        elif kind == "message_start" and isinstance(event.get("message"), dict):
            model = event["message"].get("model") or model
        elif kind == "content_block_start" and isinstance(event.get("content_block"), dict):
            if event["content_block"].get("type") == "tool_use":
                tool = str(event["content_block"].get("name") or "a tool")
        elif kind == "content_block_delta" and isinstance(event.get("delta"), dict):
            delta = event["delta"]
            if isinstance(delta.get("text"), str):
                reply.append(delta["text"])
            if isinstance(delta.get("partial_json"), str):
                partial.append(delta["partial_json"])
    return "".join(reply) or "".join(partial), tool, model if isinstance(model, str) else "", error


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


def _api_test_exchange(entry, effort, timeout):
    """Send one Test request (see _api_test_request); (status or None, Location, body text, content type, failure)."""

    parts = urlsplit(entry["baseUrl"])
    deadline, status, location, kind, chunks = time.monotonic() + timeout, None, "", "", []
    connection = (
        http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout,
                                    context=ssl.create_default_context())
        if parts.scheme == "https" else
        http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    )
    try:
        path, headers, body = _api_test_request(entry, effort)
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
        kind = (response.getheader("Content-Type") or "").lower()
        size = 0
        while size < API_TEST_MAX_RESPONSE_BYTES:
            bound()
            chunk = response.read1(min(16384, API_TEST_MAX_RESPONSE_BYTES - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except Exception as exc:  # Network, TLS, protocol or encoding failure.
        if status is None:
            return None, "", "", "", _api_test_failure(exc, timeout)
        # A body cut short still leaves a usable status line and what arrived.
    finally:
        connection.close()
    return status, location, b"".join(chunks).decode("utf-8", "replace"), kind, ""


def _api_test_outcome(status, location, text, kind, streamed):
    """(ok, model or "", message) of one Test exchange."""

    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if 300 <= status < 400:
        return False, "", (f"The endpoint redirected to {location or 'another URL'}; redirects are not "
                           "followed. Enter the final API base URL.")
    if not 200 <= status < 300:
        detail = _api_test_error(value, text).strip()
        return False, "", detail or f"HTTP {status} without an error message."
    if not streamed:
        reply = _api_test_reply(value)
        reported = value.get("model") if isinstance(value, dict) else None
        return True, reported if isinstance(reported, str) else "", (
            f"The model replied: {reply}" if reply.strip() else "The endpoint accepted the request.")
    if "text/event-stream" not in kind and not text.lstrip().startswith(("event:", "data:")):
        return False, "", ("The endpoint answered without streaming (server-sent events), which the CLIs need "
                           "for every request.")
    reply, tool, model, error = _api_test_stream(text)
    if error:
        return False, model, error
    if tool:
        return True, model, f"The model answered through the {tool} tool: {reply.strip() or '(no arguments)'}"
    try:
        answered = json.loads(reply.strip().strip("`").removeprefix("json").strip()) == {"ok": True}
    except ValueError:
        answered = False
    if not reply.strip():
        return True, model, "The endpoint streamed an answer without text."
    return True, model, (f"The model replied with the requested JSON: {reply.strip()}" if answered
                         else f"The model replied, but not with the requested JSON: {reply.strip()}")


def probe_api_endpoint(entry, timeout=None, effort=None):
    """Test an API model's endpoint with a request like the ones runs send, and describe the result.

    The request streams its answer, offers one tool and asks for a JSON
    answer at ``effort`` (the menus' default when None), with the model's
    output limit (see _api_test_request). When the endpoint refuses it, one
    minimal request tells a request the endpoint does not accept (a settings
    problem) from an endpoint that refuses everything. ``entry`` holds
    validated settings including the key. Each exchange has one overall
    deadline, follows no redirect, and the key travels only in the
    protocol's standard authentication header; no returned text contains it.
    """

    timeout = API_TEST_TIMEOUT_SECONDS if timeout is None else timeout
    effort = providers.API_EFFORTS[entry["format"]][-1] if effort not in providers.API_EFFORTS[entry["format"]] else effort
    result = {"ok": False, "status": None, "latencyMs": None, "message": "", "model": entry["model"],
              "effort": effort, "testedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    started = time.monotonic()
    status, location, text, kind, failure = _api_test_exchange(entry, effort, timeout)
    result.update(status=status, latencyMs=round((time.monotonic() - started) * 1000))
    if status is None:
        result["message"] = _api_test_text(failure, entry["apiKey"])
        return result
    ok, model, message = _api_test_outcome(status, location, text, kind, True)
    if not ok and 400 <= status < 500 and status not in {401, 403, 404}:
        basic = _api_test_exchange(entry, None, timeout)
        if basic[0] is not None and 200 <= basic[0] < 300:
            message = (f"The endpoint answers a minimal request but refuses the requests runs send (streamed, with "
                       f"a tool and a JSON answer at {effort} effort): {message}")
    elif ok and "not with the requested JSON" in message:
        message += " Runs ask again until the answer is valid JSON."
    result.update(ok=ok, model=model.strip() or entry["model"], message=message)
    result["model"] = _api_test_text(result["model"], entry["apiKey"])
    result["message"] = _api_test_text(result["message"], entry["apiKey"])
    return result


def _api_models_page(entry, path, deadline):
    """Read a bounded models page without following provider-supplied URLs or redirects."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("API model discovery deadline")
    parts = urlsplit(entry["baseUrl"])
    connection = (http.client.HTTPSConnection(parts.hostname, parts.port, timeout=remaining,
                                            context=ssl.create_default_context())
                  if parts.scheme == "https" else
                  http.client.HTTPConnection(parts.hostname, parts.port, timeout=remaining))
    try:
        connection.request("GET", path, headers=_api_connection_headers(entry))
        channel = connection.sock

        def bound():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("API model discovery deadline")
            if channel.fileno() != -1:
                channel.settimeout(left)

        bound()
        response = connection.getresponse()
        chunks, size = [], 0
        while size <= API_MODELS_MAX_RESPONSE_BYTES:
            bound()
            chunk = response.read(min(16384, API_MODELS_MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > API_MODELS_MAX_RESPONSE_BYTES:
            raise providers.ProviderError("The model list response is too large.")
        return response.status, response.getheader("Location") or "", b"".join(chunks)
    finally:
        connection.close()


def discover_api_models(entry, timeout=None):
    """Fetch model options using the connection's key, with an overall deadline.

    Anthropic pages use the returned last_id as a cursor on the original path;
    remote pagination links are never followed. Incomplete lists are rejected so
    a failed later page cannot silently hide valid choices.
    """

    timeout = API_TEST_TIMEOUT_SECONDS if timeout is None else timeout
    started = time.monotonic()
    deadline = started + timeout
    result = {"ok": False, "status": None, "latencyMs": None, "message": "", "models": [],
              "testedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path, cursor, seen_cursors, models = _api_endpoint_path(entry, "models"), None, set(), {}
    try:
        for _ in range(API_MODELS_MAX_PAGES):
            page_path = path + ("?" + urlencode({"after_id": cursor}) if cursor is not None else "")
            status, location, raw = _api_models_page(entry, page_path, deadline)
            result["status"] = status
            text = raw.decode("utf-8", "replace")
            try:
                value = json.loads(text)
            except ValueError:
                value = None
            if 300 <= status < 400:
                result["message"] = (f"The endpoint redirected to {location or 'another URL'}; redirects are not "
                                     "followed. Enter the final API base URL.")
                break
            if not 200 <= status < 300:
                detail = _api_test_error(value, text).strip() or f"HTTP {status} without an error message."
                result["message"] = detail
                break
            if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                result["message"] = "The endpoint did not return a valid model list (expected data with model IDs)."
                break
            for item in value["data"]:
                if not isinstance(item, dict):
                    continue
                try:
                    identity = providers._model_name(item.get("id"))
                except providers.ProviderError:
                    continue
                if _api_test_text(identity, entry["apiKey"]) != identity:
                    continue
                label = item.get("display_name")
                label = label if isinstance(label, str) and label.strip() else identity
                models.setdefault(identity, {"id": identity, "label": _api_test_text(label, entry["apiKey"])})
            if len(models) > API_MODELS_MAX_COUNT:
                result["message"] = "The endpoint returned too many models; narrow the API key's model access."
                break
            if not value.get("has_more"):
                result["models"] = sorted(models.values(), key=lambda item: item["id"].casefold())
                result["ok"] = bool(models)
                result["message"] = (f"Found {len(models)} model{'s' if len(models) != 1 else ''}." if models else
                                     "No models are available for this API key.")
                break
            if entry["format"] != providers.ANTHROPIC_FORMAT:
                result["message"] = "The endpoint returned a paginated model list in an unsupported format."
                break
            cursor = value.get("last_id")
            if (not isinstance(cursor, str) or not cursor or len(cursor) > providers.MAX_MODEL_CHARS
                    or cursor in seen_cursors):
                result["message"] = "The endpoint returned an invalid model list pagination cursor."
                break
            seen_cursors.add(cursor)
        else:
            result["message"] = "The endpoint returned too many model list pages."
    except providers.ProviderError as exc:
        result["message"] = str(exc)
    except Exception as exc:
        result["message"] = _api_test_failure(exc, timeout)
    result["latencyMs"] = round((time.monotonic() - started) * 1000)
    result["message"] = _api_test_text(result["message"], entry["apiKey"])
    return result


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
        self._checkpoint_cache = []
        self._checkpoint_cache_signature = None
        # Start of the runner's current retry wait, which is not active time.
        self._retry_wait_started = None
        # The model calls now waiting to retry, by activity (each labelled call waits separately), and
        # each one's latest failed attempt with the time its failures began.
        self._model_retries, self._retry_starts = {}, {}
        # The owner locks of this run while it may start a runner here (see ownership).
        self._claims = ownership.Claims()
        weakref.finalize(self, self._claims.release_all)
        self._worker_thread = None
        # A pause checkpoint that could not be written yet (for example on a full disk).
        self._unsaved_checkpoint = None
        # Transcript lines kept after a failed write, and when to try writing them again.
        self._unsaved_trace, self._trace_retry_at = [], 0.0
        # The summaries, messages and replies of the paused author (conversation.json), oldest first.
        self._conversation = []
        # Whether the author resumed by the last Resume had not yet received the critic's latest report.
        self._carry_critic_feedback = False
        # The author's questions that closed; a late copy of one (its "completed" event) never reopens it.
        self._closed_questions = set()

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

    def _controller_directory(self):
        """Create (owner-only) and return this run's controller folder."""

        folder = controller_directory(self.run_dir)
        for path in (folder.parent, folder):
            path.mkdir(exist_ok=True, mode=0o700)
        return folder

    def _save_controlled(self, name, content, *, run_copy=True):
        """Save a file the controller relies on: its own copy and, unless ``run_copy`` is False, the run folder's."""

        path = self._controller_directory() / name
        runtime._private_atomic_write(path, content)
        if run_copy:
            self._save(name, content)
        return path

    def _save_prompt(self, role, text):
        """Save one role prompt; return the controller's copy, which the runner reads."""

        return self._save_controlled(f"prompts/{role}.txt", text + "\n")

    def _save_job_settings(self, options):
        """Persist restart-safe workflow settings without prompt duplication."""

        keys = (
            "reviewModel", "authorModel", "criticModel", "writerModel",
            "reasoningEffort", "reviewEffort", "authorEffort",
            "criticEffort", "writerEffort", "criticRounds", "criticCount", "authorSubagents",
            "thinkingHours", "speedMode", "reasoningSummary",
            "problemMode", "skipStatementReview", "statementReviewOnly", "preparedRun",
            "workflowStages", "originalContent",
            "resultKind", "revisionRequired",
            "launchHold", "retrievalPolicyPath", "retrievalMode", "retrievalCutoffUtc",
            "sourceFile", "apiModels", "replacedRoles", "launchQueued", "queuedAt", "launchError",
            "goalThreadId", "goalWorkspace", "goalResume", "authorSteerDelivered", "elapsedSeconds", "resumedAt",
            "authorRepairDelivered", "pendingReanchor", "relatedWorksPending", "folderBatch", "authorQuestion",
            "timeLimitHistory", "conventionWarning",
        )
        # The solver rechecks historical settings while UI events persist
        # thread/steering state. Readers must never observe a truncated JSON.
        # Stage outside the writable workspace so a same-UID sandboxed author
        # cannot alter temporary contents before the controller installs them.
        payload = json.dumps(
            {key: options[key] for key in keys if key in options},
            ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n"
        # The controller's copy first: restoring trusts it, and the run
        # folder's copy is only what the author may read.
        for folder in (self._controller_directory(), self.run_dir):
            runtime._private_atomic_write(
                folder / JOB_SETTINGS_FILENAME, payload, staging_directory=self.run_dir.parent,
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
        source_settings_path = controlled_path(source, JOB_SETTINGS_FILENAME)
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
            self._worker_thread = (token, worker)
        except Exception:
            with self.lock:
                if self.worker_token is token:
                    self.worker_token = None
            raise

        return worker

    def has_active_worker(self):
        """Return whether a model reader can still mutate this run."""

        with self.lock:
            return self.worker_token is not None

    def _claim_run_locked(self):
        """Own this run, and the workspace of the author it continues, before a runner starts.

        Raise OwnedElsewhere while another process owns either. Return the lock
        descriptors to hand to the runner.
        """

        folders = [self.run_dir]
        workspace = self.state.get("goalWorkspace")
        if workspace:
            try:
                workspace = Path(workspace).expanduser().resolve()
                if workspace != self.run_dir.resolve() and workspace.is_dir():
                    folders.append(workspace)
            except OSError:
                pass
        return [descriptor for descriptor in map(self._claims.claim, folders) if descriptor is not None]

    def _settle_run_claim(self):
        """Give up the owner locks once nothing here can start this run's runner by itself."""

        with self.lock:
            problem = self.state.get("settingsProblem")
            if (self.worker_token is not None or self.state["phase"] in ACTIVE_PHASES
                    or (self.state["phase"] == "paused" and (self.state.get("autoResumeAt")
                                                             or (isinstance(problem, dict) and problem.get("models"))))
                    # A reply the server starts by itself.
                    or self._conversation_busy_locked()):
                return
            self._claims.release_all()

    def _check_owned_here(self):
        if self.state.get("runningElsewhere"):
            raise ValueError(self.state["runningElsewhere"])

    def _reader_gone_locked(self):
        """Whether the current reader thread ended without settling its job."""

        reader = self._worker_thread
        return (self.worker_token is not None and reader is not None
                and reader[0] is self.worker_token and not reader[1].is_alive())

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
        self._settle_run_claim()

    def _write_author_limit(self, hours):
        """Atomically publish the total workflow limit to the active solver (from the controller folder)."""

        return self._save_controlled(AUTHOR_LIMIT_FILENAME, json.dumps({"hours": hours}), run_copy=False)

    def _write_author_steer(self, instruction):
        """Atomically append one live instruction to the active author's queue.

        The author receives queued instructions one at a time and in order
        (runtime.pending_author_steer); the ones it already accepted are
        dropped here. The newest entry is repeated at the top level, which is
        the whole file an older runner reads.
        """

        command_id = secrets.token_hex(12)
        entry = {"id": command_id, "instruction": instruction, "createdAt": datetime.now(timezone.utc).isoformat()}
        queue = [item for item in self._author_steer_queue()
                 if isinstance(item, dict) and isinstance(item.get("id"), str)]
        identities = [item["id"] for item in queue]
        delivered = self.state.get("authorSteerDelivered")
        if delivered in identities:
            queue = queue[identities.index(delivered) + 1:]
        self._save(AUTHOR_STEER_FILENAME, json.dumps({**entry, "queue": [*queue, entry]}, ensure_ascii=False))
        return command_id

    def _author_steer_queue(self):
        """The queued live instructions; the author can write its run folder, so a link or FIFO reads as none."""

        try:
            descriptor = open_run_log(self.run_dir / AUTHOR_STEER_FILENAME, os.O_RDONLY, private=False)
            if descriptor is None:
                return []
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                value = json.loads(stream.read())
        except (OSError, UnicodeError, ValueError, RecursionError):
            return []
        if isinstance(value, dict) and isinstance(value.get("queue"), list):
            return value["queue"]
        return [value] if isinstance(value, dict) else []

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
                                if len(pinned) > PINNED_LIMIT + PINNED_SLACK:
                                    pinned = bounded_pinned(pinned)
                    except (ValueError, RecursionError):
                        continue
            return without_completed_deltas(list(records)), total, bounded_pinned(pinned)
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
        """Timestamp, retain, and persist one transcript record; return it as it was saved."""

        # Put the receipt time last so a child cannot replace the audit timestamp.
        entry = {**record, "time": datetime.now(timezone.utc).isoformat()}
        with self.lock:
            # Secure the private log before writing its first sensitive byte.
            if not self.trace_file:
                raise OSError("No problem run is active.")
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            try:
                line.encode("utf-8")
            except UnicodeEncodeError:
                line = json.dumps(entry) + "\n"  # A lone surrogate from a JSON escape stays escaped.
            replaced, notice = self._persist_trace(line, durable=entry.get("kind") != "codex_event")
            for added in (entry, notice) if notice else (entry,):
                self.state["trace"].append(added)
                self.state["trace"][:] = self.state["trace"][-TRACE_LIMIT:]
                if important_record(added):
                    self.pinned.append(added)
                    if len(self.pinned) > PINNED_LIMIT + PINNED_SLACK:
                        self.pinned = bounded_pinned(self.pinned)
                self.state["traceVersion"] += 1
            self.state["lastActivityAt"] = entry["time"]
            if isinstance(record.get("node"), str) and record["node"]:
                self.state["activeNode"] = record["node"]
            if isinstance(record.get("round"), int):
                self.state["round"] = record["round"]
            self.state["criticProgress"] = follow_critic_progress(self.state.get("criticProgress"), entry,
                                                                  self.state.get("criticCount"))
            history = self.state.setdefault("timeLimitHistory", [])
            if follow_time_limit(history, entry) and self.run_dir:
                # Saved batches read only recent records: the job settings keep the whole history.
                try:
                    self._save_job_settings(self.state)
                except (OSError, ValueError, runtime.Error):
                    pass  # The transcript has the record; a restart rebuilds the history from it.
            if replaced and record.get("label") != "Transcript replaced":
                self.add_trace({
                    "kind": "diagnostic", "stage": self.state["stage"], "label": "Transcript replaced",
                    "text": "transcript.jsonl in the run folder had been replaced by a link "
                            "or another unusable entry. It was not followed; a new private "
                            "transcript continues from this record.",
                })
        return entry

    def _persist_trace(self, line, durable=False):
        """Append one transcript line; a failed write keeps it, and later lines, for a retry every minute.

        Losing the disk for a while (full, too many open files, an I/O error)
        must not stop a working runner: the job keeps every record in memory
        and writes them in order once the transcript can be written again.
        Return (whether the log was replaced, a notice record for a first failure).
        """

        if self._unsaved_trace:
            self._unsaved_trace.append(line)
            self.flush_trace()
            return False, None
        try:
            # The run folder was made private once, when it was created.
            self.trace_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            return append_run_log(self.trace_file, line, durable=durable), None
        except OSError as exc:
            notice = {"kind": "diagnostic", "stage": self.state["stage"],
                      "text": f"Saving the transcript failed ({exc}). The job keeps working; its events are kept, "
                              f"and saving them is retried in {retry_interval_text()}.",
                      "time": datetime.now(timezone.utc).isoformat()}
            self._unsaved_trace = [line, json.dumps(notice, ensure_ascii=False) + "\n"]
            self._trace_retry_at = time.monotonic() + AUTO_RESUME_SECONDS
            return False, notice

    def flush_trace(self, force=False):
        """Write transcript lines kept after a failed write, at most once a minute; True when none are left."""

        with self.lock:
            if not self._unsaved_trace:
                return True
            if not force and time.monotonic() < self._trace_retry_at:
                return False
            try:
                append_run_log(self.trace_file, "".join(self._unsaved_trace), durable=True)
            except OSError:
                self._trace_retry_at = time.monotonic() + AUTO_RESUME_SECONDS
                return False
            self._unsaved_trace = []
            return True

    @staticmethod
    def parse_line(line, stage):
        """Turn either tagged JSON or a diagnostic line into one record."""

        try:
            record = json.loads(line)
            if isinstance(record, dict):
                return record
        except (ValueError, RecursionError):
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

    def snapshot(self, after=None):
        """Copy state, optionally returning only newly appended records."""

        with self.lock:
            # First: closing a question adds a record, which this copy must include.
            question = self._author_question_locked()
            state = dict(self.state)
            state["authorQuestion"] = (self._public_question(question)
                                       if question and self._author_running_locked() else None)
            run_dir = self.run_dir
            state["needsAttention"] = self.attention(state)
            state["workerActive"] = self.has_active_worker()
            state["processAlive"] = bool(self.process is not None and self.process.poll() is None)
            state["activeElapsedSeconds"] = self._elapsed_seconds()
            paused = state["phase"] in {"paused", "pausing"}
            checkpoint = read_pause_object(run_dir) if paused and run_dir else {}
            state["pauseCause"] = str(checkpoint.get("pauseCause") or "") if paused else ""
            state["criticFeedbackPending"] = paused and checkpoint.get("criticFeedbackPending") is True
            state["timeLimitExhausted"] = self._time_limit_exhausted_locked(state["activeElapsedSeconds"])
            state["relatedWorksPending"] = list(state.get("relatedWorksPending") or [])
            historical = bool(state.get("retrievalPolicyPath") or state.get("retrievalMode") == "historical")
            talk_problem = self._conversation_problem_locked()
            state["conversation"] = {"available": not talk_problem, "reason": talk_problem,
                                     "busy": self._conversation_busy_locked(),
                                     # Live runs answer read-only; a web-cutoff author keeps its audited launch.
                                     "readOnly": not historical and getattr(runtime, "CONVERSATION_READ_ONLY", True),
                                     "items": self._conversation_copies_locked()}
            works_problem = self.related_works_problem()
            workspace = self._author_workspace() if run_dir else None
            folder_batch = state.get("folderBatch") or ""
            try:
                state["statement"] = saved_statement(self.run_dir) if self.run_dir else ""
            except ValueError:
                state["statement"] = ""
            graph = state["workflow"]
            # A typo in a live YAML edit must not break job pages or report a
            # completed action as failed. Launches still read the YAML strictly.
            try:
                prompts = default_prompts()
                signature = checkpoint_artifact_signature(self.run_dir)
                if signature != self._checkpoint_cache_signature:
                    self._checkpoint_cache = (
                        saved_run_checkpoints(self.run_dir) if self.run_dir else []
                    )
                    self._checkpoint_cache_signature = signature
                # Set when an edit does not load and the last version that did is used.
                state["workflowWarning"] = runtime.workflow_warnings()
            except (ValueError, KeyError) as exc:
                prompts = dict(_last_default_prompts or graph["settings"].get("prompts", {}))
                state["workflowWarning"] = f"The workflow YAML cannot be read: {exc}"
            state["workflow"] = {
                **graph, "settings": {**graph["settings"], "prompts": prompts, **model_menu_settings()},
            }
            state["checkpoints"] = [dict(item, resumable=False) if not saved_workflow_stages(state)["critic"]
                                    else item for item in self._checkpoint_cache]
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
            # The approved proof and the working LaTeX source are results too,
            # while the LaTeX stage runs or waits for TeX (I-013, I-068). A run
            # without its writing stage returns the proof itself as its result.
            state["canDownloadProof"] = bool(self.run_dir and state.get("finalInputReady")
                                             and state.get("problemMode") != "latex"
                                             and saved_workflow_stages(state)["writing"])
            state["canDownloadSource"] = bool(
                not state["canDownloadTex"] and self.run_dir
                and (self.run_dir / "latex-source.tex").is_file()
                and not (self.run_dir / "latex-source.tex").is_symlink()
            )
            trace, version = self.state["trace"], self.state["traceVersion"]
            first = version - len(trace)
            if isinstance(after, int) and first <= after <= version:
                state["trace"], state["traceFrom"] = list(trace[after - first:]), after
            else:
                state["trace"], state["traceFrom"] = self.retained_trace(), first
            if first > 0:
                # Older routine events left the live window: say so, and where the rest is.
                state["traceTruncated"] = True
                state["traceNotice"] = (f"Showing the latest {len(trace):,} of {version:,} events; every prompt, "
                                        "result and error stays listed. The complete history is in this job's "
                                        "transcript.jsonl.")
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
        # Read from the manifests without the job lock (listing never raises).
        items = related_works.listing(workspace, controller_directory(workspace)) if workspace else []
        batch = None
        if folder_batch:
            try:
                shared = folder_batch_store(self.runs, folder_batch).listing()
                batch = {"id": folder_batch, "count": len(shared), "items": shared}
            except ValueError:
                batch = None
        state["relatedWorks"] = {"available": not works_problem, "reason": works_problem, "count": len(items),
                                 "items": items, "batch": batch}
        return state

    @staticmethod
    def _workflow_options(
        critic_rounds=DEFAULT_CRITIC_ROUNDS,
        critic_count=DEFAULT_CRITIC_COUNT,
        author_subagents=DEFAULT_AUTHOR_SUBAGENTS,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        author_effort=None, critic_effort=None, writer_effort=None,
        author_prompt=None, critic_prompt=None, final_prompt=None, convention_prompt=None,
        review_model=DEFAULT_REVIEW_MODEL, review_effort=None,
        review_prompt=None, include_review=True,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        retrieval_mode="live", retrieval_cutoff_utc="",
        workflow_stages=None,
    ):
        """Normalize and validate settings shared by both input modes."""

        stages = normalize_workflow_stages(workflow_stages) if workflow_stages is not None else {
            "review": include_review, "author": True, "critic": True, "writing": True}
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
            "critic": critic_prompt, "convention": convention_prompt, "final": final_prompt,
        }
        defaults = default_prompts()
        prompts = {
            name: str(
                defaults[name] if value is None else value
            ).strip()
            for name, value in supplied_prompts.items()
        }
        all_models = {"review": review_model, "author": author_model, "critic": critic_model, "writer": writer_model}
        role_models = {role: all_models[role] for stage, role in STAGE_ROLES.items() if stages[stage]}
        # Built-in, older Codex, or configured API ids; the store is read now.
        require_known_models(role_models)
        all_efforts = {"review": review_effort, "author": author_effort, "critic": critic_effort, "writer": writer_effort}
        efforts = [all_efforts[role] for role in role_models]
        if any(effort not in EFFORTS and effort not in runtime.RETIRED_EFFORTS for effort in efforts):
            raise ValueError("Choose a valid reasoning effort for every role.")
        selected_models = list(role_models.values())
        defaults_models = {"review": DEFAULT_REVIEW_MODEL, "author": DEFAULT_AUTHOR_MODEL,
                           "critic": DEFAULT_CRITIC_MODEL, "writer": DEFAULT_WRITER_MODEL}
        for role in all_models:
            if role in role_models:
                # A retired effort (Ultra, from a script or a saved job) is kept as given: the runner runs it as
                # Max and says so once in the activity log.
                if all_efforts[role] not in runtime.RETIRED_EFFORTS:
                    all_efforts[role] = runtime.effective_effort(all_models[role], all_efforts[role])
            else:
                all_models[role] = defaults_models[role]
                all_efforts[role] = DEFAULT_REVIEW_EFFORT if role == "review" else DEFAULT_REASONING_EFFORT
        review_model, author_model, critic_model, writer_model = (all_models[role] for role in ROLES)
        review_effort, author_effort, critic_effort, writer_effort = (all_efforts[role] for role in ROLES)
        if speed_mode not in SPEEDS:
            raise ValueError("Choose Standard or Fast speed.")
        if reasoning_summary not in REASONING_SUMMARIES:
            raise ValueError(
                "Choose Status only, Concise summaries, or Detailed summaries."
            )
        required_prompts = [prompts[name] for name, role in PROMPT_ROLES.items() if role in role_models]
        for name, role in PROMPT_ROLES.items():
            if role not in role_models:
                prompts[name] = defaults[name]
        if any(not prompt for prompt in required_prompts):
            raise ValueError("Every role prompt must contain instructions.")
        if stages["author"] and prompts["author"].count(runtime.MARKER) != 1:
            raise ValueError(
                f"The author prompt must contain exactly one {runtime.MARKER}."
            )
        if stages["critic"]:
            try:
                runtime._template_parts(prompts["critic"], {"count"})
            except ValueError as exc:
                raise ValueError(f"The critic prompt cannot be used ({exc}). Write {{count}} where the number of "
                                 "independent critics goes, and double any other brace: {{ and }}.") from exc
            try:
                runtime._template_parts(prompts["convention"], {"statement", "instructions"})
            except ValueError as exc:
                raise ValueError(f"The convention critic prompt cannot be used ({exc}). Write {{statement}} where "
                                 "the statement goes, and double any other brace: {{ and }}.") from exc
        try:
            critic_rounds = runtime.critic_limit(critic_rounds) if stages["critic"] else DEFAULT_CRITIC_ROUNDS
        except runtime.Error as exc:
            raise ValueError(str(exc)) from exc
        critic_count = valid_critic_count(critic_count) if stages["critic"] else DEFAULT_CRITIC_COUNT
        author_subagents = valid_author_subagents(author_subagents) if stages["author"] else DEFAULT_AUTHOR_SUBAGENTS
        try:
            if workflow_stages is not None and not stages["author"]:
                thinking_hours = DEFAULT_THINKING_HOURS
            thinking_hours = float(thinking_hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("The total workflow time limit must be a number.") from exc
        if not math.isfinite(thinking_hours) or thinking_hours < 0:
            raise ValueError("Choose 0 for unlimited, or a positive number of hours.")
        # Web cutoff timestamps need Codex models: say so before any CLI check.
        search_options = retrieval.validate_options(
            retrieval_mode, retrieval_cutoff_utc, models=selected_models)
        if search_options["retrievalMode"] == "historical" and (not stages["author"] or stages["review"]):
            raise ValueError("Historical search requires Proof author enabled and Statement review disabled.")
        require_model_access(selected_models)
        api_models = api_model_snapshot(selected_models)
        return {
            **search_options,
            **({"apiModels": api_models} if api_models else {}),
            "workflowStages": stages,
            "reviewModel": review_model,
            "authorModel": author_model,
            "criticModel": critic_model,
            "writerModel": writer_model,
            "reasoningEffort": author_effort,
            "reviewEffort": (
                review_effort
            ),
            "authorEffort": author_effort,
            "criticEffort": critic_effort,
            "writerEffort": writer_effort,
            "reviewPrompt": (
                prompts["review"]
            ),
            "authorPrompt": prompts["author"],
            "criticPrompt": prompts["critic"],
            "conventionPrompt": prompts["convention"],
            "finalPrompt": prompts["final"],
            "criticRounds": critic_rounds,
            "criticCount": critic_count,
            "authorSubagents": author_subagents,
            "thinkingHours": thinking_hours,
            "speedMode": speed_mode,
            "reasoningSummary": reasoning_summary,
        }

    def start_review(
        self, statement, feedback="", critic_rounds=DEFAULT_CRITIC_ROUNDS,
        critic_count=DEFAULT_CRITIC_COUNT,
        author_subagents=DEFAULT_AUTHOR_SUBAGENTS,
        review_model=DEFAULT_REVIEW_MODEL,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        review_effort=None, author_effort=None,
        critic_effort=None, writer_effort=None,
        review_prompt=None, author_prompt=None,
        critic_prompt=None, final_prompt=None, convention_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        review_only=False,
        continuation_source="", stopped_stage="",
        retrieval_mode="live", retrieval_cutoff_utc="",
        revise_existing=False,
        workflow_stages=None, original_content=None, source_file=None,
        _prepare_only=False,
        related_works=None, folder_batch=None,
    ):
        """Start the review and return immediately so the page can poll.

        ``related_works`` (upload tokens) and ``folder_batch`` are installed in
        a new run as in start_direct_statement; a "Retry statement review" in a
        new run copies its source's related works.
        ``revise_existing`` means the request names this job: it may only retry
        a reviewed or failed review in place, or start its queued review, never
        start another run. ``_prepare_only`` (a folder batch) saves the job as a
        queued one, whose review the server starts when a running slot is free.
        """

        self._check_input_replacement()

        statement = str(statement).strip()
        feedback = str(feedback or "").strip()
        if "\0" in statement or "\0" in feedback:
            raise ValueError("Statement review input cannot contain NUL characters.")
        if len(statement) > MAX_STATEMENT_CHARS:
            raise ValueError(f"The problem statement is longer than {MAX_STATEMENT_CHARS:,} characters.")
        if not isinstance(review_only, bool):
            raise ValueError("Statement review only must be enabled or disabled.")
        stages = normalize_workflow_stages(workflow_stages) if workflow_stages is not None else (
            {"review": True, "author": False, "critic": False, "writing": False} if review_only else
            normalize_workflow_stages())
        if not stages["review"]:
            raise ValueError("Enable statement review before starting a review request.")
        tokens = self._new_related_works_tokens(related_works, stages)
        review_only = not any(stages[name] for name in ("author", "critic", "writing"))
        if review_only:
            # Proof-stage settings are irrelevant to this terminal workflow.
            author_model = DEFAULT_AUTHOR_MODEL
            critic_model = DEFAULT_CRITIC_MODEL
            writer_model = DEFAULT_WRITER_MODEL
            author_effort = DEFAULT_REASONING_EFFORT
            critic_effort = DEFAULT_REASONING_EFFORT
            writer_effort = DEFAULT_REASONING_EFFORT
            author_prompt = critic_prompt = final_prompt = convention_prompt = None
            critic_rounds = DEFAULT_CRITIC_ROUNDS
            critic_count = DEFAULT_CRITIC_COUNT
            author_subagents = DEFAULT_AUTHOR_SUBAGENTS
            thinking_hours = DEFAULT_THINKING_HOURS
        options = self._workflow_options(
            critic_rounds=critic_rounds,
            critic_count=critic_count,
            author_subagents=author_subagents,
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
            convention_prompt=convention_prompt,
            final_prompt=final_prompt,
            review_model=review_model,
            review_effort=review_effort,
            review_prompt=review_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
            workflow_stages=stages,
        )
        options["originalContent"] = str(original_content if original_content is not None else (
            self.state.get("originalContent") or statement if revise_existing else statement))
        options["sourceFile"] = source_file_name(source_file)
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
            # selected job's problem folder; one job keeps one run id. So does
            # the start of a queued review.
            failed_retry = revise_existing and (self.state["phase"] == "input" or (
                self.state["phase"] == "prepared" and self._queued_review()))
            retry = self.run_dir is not None and (
                self.state["phase"] == "reviewed" or failed_retry
            )
            if revise_existing and not retry:
                raise ValueError("Start a new problem from the home screen.")
            if retry and tokens:
                raise ValueError("Add related works on the job page.")
            if failed_retry:
                self._save("draft.md", f"# Draft problem\n\n{statement}\n")
            works = ([], "")
            if not retry:
                self._new_run(statement)
                if not self.fixed_trace:
                    self.pinned = []
                self._prepare_continuation(
                    continuation_source, stopped_stage,
                )
                # A continuation stays in its source's folder batch, and takes its related works.
                folder_batch = folder_batch or (saved_folder_batch(continuation_source) if continuation_source
                                                else "")
                works = self._install_new_related_works_locked(tokens, folder_batch, self.run_dir, "",
                                                               copy_from=continuation_source)
            else:
                folder_batch = self.state.get("folderBatch") or ""
            if folder_batch:
                options["folderBatch"] = folder_batch
            self._save(
                REVIEW_INPUT_FILENAME,
                json.dumps({
                    "schemaVersion": 1,
                    "statement": statement,
                    "feedback": feedback,
                }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            prompt_names = [name for name, role in PROMPT_ROLES.items() if stages[ROLE_STAGES[role]]]
            for name in prompt_names:
                self._save_prompt(name, options[f"{name}Prompt"])
            self._save_job_settings({
                **options,
                "problemMode": "statement",
                "skipStatementReview": False,
                "statementReviewOnly": review_only,
            })
            trace = self.state["trace"] if retry or self.fixed_trace else []
            version = self.state["traceVersion"] if retry or self.fixed_trace else 0
            previous_review = self.state.get("review") if retry and feedback else None
            self.state = {
                **empty_state(trace, version),
                "phase": "reviewing",
                "draft": statement,
                "review": previous_review,
                "reviewStatement": statement,
                "reviewFeedback": feedback,
                "reviewInputRecorded": True,
                "statementReviewOnly": review_only,
                **options,
                "workflow": workflow_graph(stages),
                "activeNode": "statement_reviewer",
                "stage": "review",
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            self._record_new_related_works_locked(*works)
            if _prepare_only:
                self.state.update(phase="prepared", preparedRun=True, launchQueued=True, queuedAt=time.time())
                self._save_job_settings(self.state)
                return
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

    def _review(self, statement, feedback="", review_model=DEFAULT_REVIEW_MODEL,
                reasoning_effort=DEFAULT_REVIEW_EFFORT, token=None):
        """Run one review; an unexpected failure still settles the job (its reviewer is stopped)."""

        try:
            self._review_records(statement, feedback, review_model, reasoning_effort, token)
        except Exception as exc:
            with self.lock:
                if self.active_token not in (None, token) or self.worker_token not in (None, token):
                    return
                self.process = self.active_token = self.worker_token = None
                self._end_retry_wait_locked()
                self._model_retries, self._retry_starts = {}, {}
                self.state["modelRetry"] = None
                if self.state["phase"] == "stopping":
                    self._finish_stopped_locked(None, "The reviewer was stopped before it produced a checked statement.")
                elif self.state["phase"] == "reviewing":
                    # As in _review_records: a failed revision returns to the statement it revised.
                    problem = f"Review failed: {exc or type(exc).__name__}"
                    revision = bool(self.state.get("review")) and bool(self.state.get("reviewFeedback"))
                    self.state.update(phase="reviewed" if revision else "input", error=problem)
                    try:
                        self.add_trace({"kind": "status", "stage": "review", "node": "statement_reviewer",
                                        "label": "Statement review failed", "text": problem})
                    except OSError:
                        pass
        self._settle_run_claim()

    def _review_records(
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
            with self.lock:
                locks = self._claim_run_locked()
            process = subprocess.Popen(
                [
                    sys.executable, "-u", str(ROOT / "web_ui.py"), "--review-worker",
                    "--review-model", review_model,
                    "--review-effort", reasoning_effort,
                    "--reasoning-summary", self.state["reasoningSummary"],
                    "--speed", self.state["speedMode"],
                    "--review-prompt-file",
                    str(controlled_path(self.run_dir, "prompts/review.txt")),
                    "--thinking-hours", str(self.state["thinkingHours"]),
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=self.run_dir,
                env=self._retrieval_child_environment(),
                **isolated_process_options(), **runner_lock_options(locks),
            )
            ownership.note_runner(self.run_dir, process.pid)
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
            code = reap_runner(process)
            if code or not isinstance(report, dict) or not all(
                isinstance(report.get(key), str) for key in ("statement", "notes")
            ):
                problem = (
                    f"Review failed: {failure_detail}"
                    if failure_detail
                    else "Review failed. See the transcript for details."
                )
        except (OSError, TypeError, AttributeError, ValueError) as exc:
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
            self._model_retries, self._retry_starts = {}, {}
            self.state["modelRetry"] = None
            stopped = self.state["phase"] == "stopping"
            if valid:
                self._clear_manual_stop()
                self._save_controlled(
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
                # A failed revision returns to the statement it revised (its feedback is
                # saved for Retry); a first review returns to the draft.
                revision = bool(self.state.get("review")) and bool(self.state.get("reviewFeedback"))
                self.state.update(phase="reviewed" if revision else "input", error=problem)
                try:
                    # Recorded, so a restart restores the failure (not a vanished or interrupted review).
                    self.add_trace({"kind": "status", "stage": "review", "node": "statement_reviewer",
                                    "label": "Statement review failed", "text": problem})
                except OSError:
                    pass
            if self.active_token is token:
                self.active_token = None
            if self.worker_token is token:
                self.worker_token = None
            if valid:
                self._save_job_settings(self.state)
        self._settle_run_claim()

    def retry_review(self):
        """Run the statement review again in this job from its saved input (review-input.json), feedback included."""

        with self.lock:
            state = dict(self.state)
        self.start_review(
            statement=state.get("reviewStatement") or state.get("draft", ""), feedback=state.get("reviewFeedback", ""),
            critic_rounds=state["criticRounds"], critic_count=state["criticCount"],
            author_subagents=state.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS),
            thinking_hours=state["thinkingHours"],
            review_model=state["reviewModel"], author_model=state["authorModel"],
            critic_model=state["criticModel"], writer_model=state["writerModel"],
            reasoning_effort=state["reasoningEffort"], review_effort=state["reviewEffort"],
            author_effort=state["authorEffort"], critic_effort=state["criticEffort"],
            writer_effort=state["writerEffort"], review_prompt=state["reviewPrompt"],
            author_prompt=state["authorPrompt"], critic_prompt=state["criticPrompt"],
            convention_prompt=state.get("conventionPrompt"),
            final_prompt=state["finalPrompt"], speed_mode=state["speedMode"],
            reasoning_summary=state["reasoningSummary"], workflow_stages=saved_workflow_stages(state),
            original_content=state.get("originalContent"), source_file=state.get("sourceFile"),
            retrieval_mode=state.get("retrievalMode", "live"),
            retrieval_cutoff_utc=state.get("retrievalCutoffUtc", ""), revise_existing=True,
        )

    def retry_review_if_due(self, now=None):
        """Start a statement review that a restart interrupted, once its time has come; return whether it tried."""

        now = time.time() if now is None else now
        with self.lock:
            due = self.state.get("reviewRetryAt") or 0
            if (not due or now < due or self.state["phase"] not in {"input", "reviewed"}
                    or self.worker_token is not None):
                return False
            self.state["reviewRetryAt"] = 0
        try:
            self.retry_review()
        except (OSError, ValueError, RuntimeError) as exc:
            with self.lock:
                self.state.update(reviewRetryAt=now + AUTO_RESUME_SECONDS,
                                  error=f"The statement review could not start again: {str(exc).rstrip('.')}. "
                                        f"Trying again in {retry_interval_text()}.")
        return True

    def _retrieval_child_environment(self):
        """Bind each worker, including independent reviewers, to its saved mode."""
        child_environment = os.environ.copy()
        # Never let one parallel job inherit another job's retrieval policy.
        child_environment.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        child_environment["TCS_PROVER_RETRIEVAL_MODE"] = self.state.get("retrievalMode", "live")
        if self.state.get("retrievalPolicyPath"):
            child_environment["TCS_PROVER_RETRIEVAL_POLICY"] = self.state["retrievalPolicyPath"]
        return child_environment

    def _spawn_runner_locked(self, workflows, stdin_text, options):
        """Start one workflow runner for this job and own it: (process, token).

        The runner works in the run folder, in its own process group, with this
        run's owner locks and retrieval environment, and reads ``stdin_text``.
        Any failure leaves no runner and no token behind.
        """

        self._check_launch_hold()
        # An edit that does not load runs the last version that did (with a warning).
        paths = [runtime.builtin_workflow_path(Path(name).stem) for name in workflows]
        locks = self._claim_run_locked()
        child_environment = self._retrieval_child_environment()
        token = self.active_token = object()
        try:
            process = subprocess.Popen(
                [
                    sys.executable, "-u", str(ROOT / "workflow_runner.py"),
                    *map(str, paths), *options,
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=self.run_dir,
                env=child_environment,
                **isolated_process_options(), **runner_lock_options(locks),
            )
        except BaseException:
            if self.active_token is token:
                self.active_token = None
            raise
        self.worker_token = token
        self.process = process
        ownership.note_runner(self.run_dir, process.pid)
        try:
            process.stdin.write(stdin_text)
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
        return process, token

    def _launch_workflow_locked(self, workflows, source, options, stage, node, state=None):
        """Launch YAML graphs in this job, preserving worker ownership on errors."""

        self._check_launch_hold()
        if state is not None:
            self._save("workflow-input.json", json.dumps(state, ensure_ascii=False))
            options = [*options, "--state-file", str(self.run_dir / "workflow-input.json")]
        process, token = self._spawn_runner_locked(workflows, source, options)
        self.state.update(
            phase="running", stage=stage, activeNode=node, round=0, output="", resultKind="",
        )
        if runtime.workflow_warnings():
            self.add_trace({"kind": "diagnostic", "stage": stage, "text": runtime.workflow_warnings()})
        return process, token

    def _proof_options_locked(self):
        """Resolve the same proof settings for new and recovered candidates."""

        stages = saved_workflow_stages(self.state)
        author_limit_file = self._write_author_limit(self.state["thinkingHours"]) if stages["author"] else None
        author_steer_file = self.run_dir / AUTHOR_STEER_FILENAME
        if not self.state.get("goalResume"):
            author_steer_file.unlink(missing_ok=True)
        # From now on the job runs (and saves) this number of critics, and its author this number of subagents.
        self.state["criticCountRecorded"] = self.state["authorSubagentsRecorded"] = True
        options = [
            "--critic-rounds", str(self.state["criticRounds"]),
            "--critic-count", str(self.state["criticCount"]),
            "--author-subagents", str(self.state.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS)),
            "--thinking-hours", str(self.state["thinkingHours"]),
            "--reasoning-effort", self.state["reasoningEffort"],
            "--reasoning-summary", self.state["reasoningSummary"],
            "--speed", self.state["speedMode"],
        ]
        if stages["author"]:
            options.extend(["--author-limit-file", str(author_limit_file), "--author-steer-file", str(author_steer_file)])
        options.extend(["--set", "workflow_stages=" + json.dumps(stages)])
        for role in ("author", "critic", "writer"):
            if not stages[ROLE_STAGES[role]]:
                continue
            options.extend([
                f"--{role}-model", self.state[f"{role}Model"],
                f"--{role}-effort", self.state[f"{role}Effort"],
            ])
        # Rewritten from the controller's copy at every launch: the author can
        # change the run folder's prompts, never the ones the runner reads.
        for role in ("author", "critic", "convention", "final"):
            if not stages[ROLE_STAGES[PROMPT_ROLES[role]]]:
                continue
            options.extend([
                f"--{role}-prompt-file", str(self._save_prompt(role, self.state[f"{role}Prompt"])),
            ])
        if self.state.get("goalThreadId"):
            options.extend(["--set", "goal_thread_id=" + json.dumps(self.state["goalThreadId"])])
        if self.state.get("goalWorkspace"):
            options.extend(["--set", "goal_cwd=" + json.dumps(self.state["goalWorkspace"])])
        if self.state.get("goalResume"):
            options.extend(["--set", "goal_resume=true"])
            if self.state.get("authorSteerDelivered"):
                options.extend(["--set", "author_steer_delivered=" + json.dumps(self.state["authorSteerDelivered"])])
            if isinstance(self.state.get("authorRepairDelivered"), dict):
                # Critic feedback the saved conversation already received is not sent again.
                options.extend(["--set", "author_repair_delivered=" + json.dumps(self.state["authorRepairDelivered"])])
        options.extend(["--set", "goal_pause_file=" + json.dumps(str(self._pause_request_path()))])
        options.extend(["--set", "goal_stderr_log=" + json.dumps(str(self.run_dir / AUTHOR_STDERR_FILENAME))])
        # A diff of the coordinator's changes too long for its critic result is saved here (see critic_diff).
        options.extend(["--set", "critic_diff_dir=" + json.dumps(
            str((self._controller_directory() / CRITIC_DIFFS_FOLDER).absolute()))])
        options.extend(["--set", "run_tmp_dir=" + json.dumps(str(self._controller_directory() / RUN_TMP_FOLDER))])
        if stages["author"]:
            # The author's first prompt points to its related works (web-cutoff runs too: they are local inputs).
            workspace = self._author_workspace()
            if related_works.count(workspace, controller_directory(workspace)):
                options.extend(["--set", "related_works=true"])
        return options

    def _launch_solver_locked(self, statement):
        """Start the author/critic graph followed by final cleanup."""

        stages = saved_workflow_stages(self.state)
        stage, node = ("solve", "author") if stages["author"] else (
            ("critic", "critic") if stages["critic"] else ("final", "latex_editor"))
        initial = None if stages["author"] else {
            "statement": statement, "solution": self.state.get("originalContent") or self.state.get("draft") or statement}
        return self._launch_workflow_locked(
            ["author_critic.yaml", "clean_up.yaml"], statement,
            [*self._proof_options_locked(), "--elapsed-seconds", str(self._elapsed_seconds())],
            stage, node, state=initial,
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
        retrying = isinstance(label, str) and label.endswith(
            (runtime.RETRY_LABEL, runtime.COMMAND_RETRY_LABEL, runtime.STORAGE_RETRY_LABEL))
        if change not in {"paused", "running"} and not retrying:
            return
        with self.lock:
            now = datetime.now(timezone.utc)
            if change == "paused" and self._retry_wait_started is None:
                self._retry_wait_started = now
            elif change == "running":
                self._end_retry_wait_locked(now)
            if retrying:
                # Labelled requests wait and retry separately: one that retries
                # now must not hide another that still waits.
                key = str(record.get("activityLabel") or record.get("node") or record.get("stage") or "")
                retry_at = record.get("retryAt")
                waits = getattr(self, "_model_retries", None)
                if waits is None:
                    waits = self._model_retries = {}
                starts = getattr(self, "_retry_starts", None)
                if starts is None:
                    starts = self._retry_starts = {}
                waits.pop(key, None)
                if isinstance(retry_at, (int, float)) and not isinstance(retry_at, bool) and math.isfinite(retry_at):
                    attempt = record.get("retryAttempt") if isinstance(record.get("retryAttempt"), int) else None
                    first = starts.get(key)
                    if finite_seconds(record.get("retryingSeconds")):
                        # When this call first failed: the runner counts the seconds it has been retrying.
                        since = (now - timedelta(seconds=float(record["retryingSeconds"]))).isoformat()
                    elif first and attempt is not None and first[0] is not None and first[0] < attempt:
                        # A wait without that count (a file write): a later attempt keeps the first failure's time.
                        since = first[1]
                    else:
                        since = now.isoformat()
                    starts[key] = (attempt, since)
                    waits[key] = {
                        "text": str(record.get("text") or ""), "retryAt": float(retry_at), "since": since,
                        "attempt": attempt,
                        "stage": record.get("stage") if isinstance(record.get("stage"), str) else "",
                        "failure": record.get("failure") if isinstance(record.get("failure"), str) else "",
                        # What waits: a model call, a command that could not run (TeX), or a file write.
                        "kind": ("command" if label.endswith(runtime.COMMAND_RETRY_LABEL)
                                 else "storage" if label.endswith(runtime.STORAGE_RETRY_LABEL) else "model"),
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

    def exclude_sleep(self, seconds):
        """Leave machine sleep out of the active time of a working job, as the runner's own clock does."""

        with self.lock:
            if seconds <= 0 or self.state["phase"] not in ACTIVE_PHASES:
                return
            if self._retry_wait_started is not None:
                # The wait is excluded separately; only its awake part may count as the wait.
                self._retry_wait_started += timedelta(seconds=seconds)
            segment = self.state.get("resumedAt") or self.state.get("startedAt")
            try:
                start = datetime.fromisoformat(segment)
            except (TypeError, ValueError):
                return
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            self.state["resumedAt"] = (start + timedelta(seconds=seconds)).isoformat()
            if self.run_dir is not None:
                try:
                    self._save_job_settings(self.state)
                except (OSError, UnicodeError, TypeError, ValueError):
                    pass  # The in-memory segment still excludes the sleep.

    def _save_pause(self, checkpoint):
        """Atomically store the author's controller checkpoint."""

        self._save_controlled(PAUSE_FILENAME, json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n")

    def _pause_request_path(self):
        """Where Pause asks the runner to pause: the controller folder, which the author cannot write."""

        return controller_directory(self.run_dir) / PAUSE_REQUEST_FILENAME

    def _pause_requested(self):
        return self._pause_request_path().exists() or (self.run_dir / PAUSE_REQUEST_FILENAME).exists()

    def _clear_pause_request(self):
        for path in (self._pause_request_path(), self.run_dir / PAUSE_REQUEST_FILENAME):
            path.unlink(missing_ok=True)

    def _configure_auto_resume(self, checkpoint, now=None):
        """Persist the retry deadline and distinguish errors from deliberate pauses."""

        now = time.time() if now is None else now
        if not checkpoint.get("pauseCause"):
            # Legacy checkpoints: deliberate pauses left a pause-request file;
            # failed runs recorded an error instead.
            checkpoint["pauseCause"] = (
                "error" if checkpoint.get("error")
                and not self._pause_requested() else "user"
            )
        # The time limit stops only the author: steps after its submission always finish. It
        # counts as used up with the margin Resume uses (_time_limit_exhausted_locked), so an
        # automatic resume is never scheduled that Resume would refuse.
        hours = saved_seconds(self.state.get("thinkingHours"), 0.0)
        exhausted = checkpoint.get("node", "author") == "author" and (
            str(checkpoint.get("error", "")).startswith(WORKFLOW_TIME_LIMIT_ERROR)
            or (hours > 0 and hours * 3600 - saved_seconds(checkpoint.get("elapsedSeconds"), 0.0)
                < RESUME_MARGIN_SECONDS)
        )
        if exhausted and checkpoint["pauseCause"] in {"error", "interrupted", "restart"}:
            # Whatever paused the author (an error, a restart, an older version's
            # record), its limit is used up: a time-limit pause, which waits for
            # the user and asks the author for its progress summary. A pause this
            # version recorded says so in summaryRequested; restart and interrupted
            # pauses exist only in this version; an older error pause gets none.
            if checkpoint["pauseCause"] in {"interrupted", "restart"}:
                checkpoint.setdefault("summaryRequested", False)
            checkpoint["pauseCause"] = "time-limit"
        if (checkpoint.get("settingsProblem") is None and checkpoint["pauseCause"] == "error"
                and settings_problem(checkpoint.get("error"))):
            # A model this account cannot use, or an endpoint that refuses the
            # request: a retry cannot help. It resumes once an API model it uses
            # is changed under Models & API (see _settings_changed_locked), or on Resume.
            checkpoint["settingsProblem"] = {"models": self._api_settings_versions()}
        # A Codex author whose saved thread ID is missing cannot be resumed by
        # a retry, an author stopped by a safety check waits for the user, and
        # a held run waits until its hold is released (never "failing" every
        # minute). A runner that ended with its server ('interrupted') or was
        # paused by a closing server ('restart') continues like an error pause.
        retry = (checkpoint["pauseCause"] in {"error", "interrupted", "restart"} and not exhausted
                 and checkpoint.get("sessionUnavailable") is not True
                 and checkpoint.get("safetyStop") is not True
                 and not self.state.get("launchHold")
                 and not checkpoint.get("settingsProblem"))
        self.state["settingsProblem"] = checkpoint.get("settingsProblem") or None
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
            if not deadline and self._settings_changed_locked(now):
                deadline = now
            if self.state["phase"] != "paused" or not deadline or now < deadline or self.state.get("manuallyStopped"):
                return False
            reply = next((item for item in self._conversation if item["status"] in {"queued", "running"}), None)
            if reply is not None:
                # A reply of the paused author goes first, but one that has already
                # failed never holds the run's own retry: it is stopped with a visible
                # reason, and the run resumes once its runner is gone.
                if not self._reply_failed_before(reply):
                    return False
                self._stop_failing_reply_locked(reply)
                return True
            if self.has_active_worker():
                return False
            try:
                if self._unsaved_checkpoint is not None:
                    self._save_pause(self._unsaved_checkpoint)
                    self._unsaved_checkpoint = None
                checkpoint = load_pause_checkpoint(self.run_dir)
            except (OSError, ValueError) as exc:
                # A missing or damaged checkpoint stops neither this job's
                # retries nor anyone else's: report it, then look again later.
                self.defer_auto_resume(exc, now)
                return True
            self._configure_auto_resume(checkpoint, now)
            if not self.state["autoResumeAt"]:
                self._save_pause(checkpoint)
                # A pause whose limit turned out to be used up records it and asks for its summary.
                self._time_limit_reached_locked(checkpoint)
                self._settle_run_claim()
                return False
            # Reserve the next attempt before launching. A failed launch, or a
            # server restart during it, must not cause retries every polling tick.
            checkpoint["autoResumeAt"] = now + AUTO_RESUME_SECONDS
            self.state["autoResumeAt"] = checkpoint["autoResumeAt"]
            self._save_pause(checkpoint)
            self.add_trace({"kind": "status", "stage": self.state["stage"],
                            "label": "Automatic resume", "text": "Retrying after an error pause."})
            try:
                # Only the models of the selected steps that remain (see checkpoint_roles).
                self._check_launch_models_locked(
                    checkpoint_roles(checkpoint["node"], saved_workflow_stages(self.state)))
            except TransientLaunchError:
                pass  # For example a CLI updating itself: resume() reports it and the next try is reserved.
            except ValueError as exc:
                # A model that is not configured (removed, or without its key):
                # checked again at every retry, resumed once it is configured again.
                self._wait_for_settings_locked(str(exc))
                return True
            self.state["waitingForSettings"] = ""
            try:
                resume()
            except (OSError, ValueError, RuntimeError) as exc:
                # A failed historical check holds the job and names its cause itself (no retry).
                if not self.state.get("launchHold"):
                    self.add_trace({"kind": "diagnostic", "stage": self.state["stage"],
                                    "text": f"Automatic resume failed: {str(exc).rstrip('.')}. Retrying in {retry_interval_text()}."})
            return True

    def _wait_for_settings_locked(self, reason):
        """Show why an automatic resume waits for the model settings (once per reason); retries go on."""

        if self.state.get("waitingForSettings") != reason:
            self.state["waitingForSettings"] = reason
            self.add_trace({"kind": "diagnostic", "stage": self.state["stage"],
                            "text": f"Automatic resume waits for the model settings: {reason} The job checks "
                                    f"again every {retry_interval_text()} and continues by itself once they are fixed."})

    def _api_settings_versions(self):
        """{api model id: when its settings last changed ('' when absent)} for the roles Resume runs."""

        versions = {}
        for role in ("author", "critic", "writer"):
            model = self.state.get(f"{role}Model")
            if providers.is_api_id(model) and model not in versions:
                try:
                    entry = providers.api_model(model, cached=False)
                except providers.StoreUnavailable:
                    return {}
                versions[model] = (entry or {}).get("updatedAt") or ""
        return versions

    def _settings_changed_locked(self, now):
        """Whether an API model that a settings-problem pause waits for has been changed since (checked twice a minute)."""

        problem = self.state.get("settingsProblem")
        if (self.state["phase"] != "paused" or not isinstance(problem, dict) or not problem.get("models")
                or now - getattr(self, "_settings_checked_at", 0.0) < AUTO_RESUME_SECONDS / 2):
            return False
        self._settings_checked_at = now
        current = self._api_settings_versions()
        if not current or all(current.get(model) == version for model, version in problem["models"].items()):
            return False
        checkpoint = read_pause_object(self.run_dir)
        if checkpoint.get("pauseCause") != "error":
            return False  # Keep paused: only Resume continues.
        # A later step that stopped on this problem was marked "no automatic retry" for it alone.
        cleared = {} if str(checkpoint.get("error") or "").startswith("Stopped for safety") else {"safetyStop": False}
        checkpoint.update(settingsProblem=False, autoResumeAt=now, **cleared)
        self._save_pause(checkpoint)
        self.state.update(settingsProblem=None, autoResumeAt=now, **cleared)
        self.add_trace({"kind": "status", "stage": self.state["stage"], "label": "Settings changed",
                        "text": "The API model settings this job waited for have changed; resuming."})
        return True

    def attention(self, state=None):
        """Why this job needs you, as {kind, title, text, retrying}, or None.

        Only causes you must fix: a login, a usage limit, a rejected API key, a
        model your account cannot use, an endpoint that refuses the requests, a
        removed model, a Codex author whose saved thread ID is missing, or a
        safety stop. Retries of the causes that can pass by themselves go on
        meanwhile.
        """

        state = self.state if state is None else state
        phase, error = state.get("phase"), str(state.get("error") or "")
        retry = state.get("modelRetry") if isinstance(state.get("modelRetry"), dict) else None
        if retry and phase in {"reviewing", "running"}:
            cause, role = retry.get("failure") or "", TRACE_STAGE_ROLES.get(retry.get("stage"), "author")
        elif phase == "paused":
            cause, role = runtime._plain_failure(error), TRACE_STAGE_ROLES.get(state.get("stage"), "author")
        else:
            cause, role = "", ""
        if state.get("waitingForSettings") and phase == "paused":
            return {"kind": "settings", "title": "Model not configured", "retrying": True,
                    "text": state["waitingForSettings"]}
        hold = str(state.get("launchHold") or "")
        if hold.startswith("Needs attention:") and phase in {"paused", "prepared", "done"}:
            # A web-cutoff check that no retry fixes holds the job (see _historical_hold_text).
            return {"kind": "historical", "title": "Web cutoff check needs attention", "retrying": False,
                    "text": hold.removeprefix("Needs attention:").strip()}
        for label, kind, title in ((runtime.MODEL_UNAVAILABLE_LABEL, "model", "Model unavailable"),
                                   (runtime.REQUEST_REJECTED_LABEL, "request", "Endpoint refuses the requests")):
            if label in error and phase in {"paused", "done", "input", "reviewed"}:
                return {"kind": kind, "title": title, "text": error, "retrying": False}
        if phase == "paused" and state.get("sessionUnavailable"):
            return {"kind": "conversation", "title": "Saved conversation ID missing", "text": error, "retrying": False}
        if phase == "paused" and state.get("safetyStop"):
            return {"kind": "safety", "title": "Stopped for safety", "text": error, "retrying": False}
        if cause not in {"authentication failed", "usage limit reached"}:
            return None
        model = state.get(f"{role}Model") or ""
        try:
            api = providers.provider_kind(model) == providers.API
            name = providers.model_label(model) if api else "Claude" if providers.harness(model) == providers.CLAUDE else "Codex"
        except (providers.ProviderError, TypeError):
            api, name = False, "The CLI"
        retrying = bool(retry) or bool(state.get("autoResumeAt"))
        if cause == "usage limit reached":
            return {"kind": "usage", "title": "Usage limit reached", "retrying": retrying,
                    "text": f"{name} reports a usage limit. The job retries every {retry_interval_text()} and "
                            "continues by itself when the limit resets."}
        if api:
            return {"kind": "key", "title": "API key rejected", "retrying": retrying,
                    "text": f"The endpoint of {name} rejected its key. Update the key under Models & API; "
                            "the job continues by itself."}
        return {"kind": "login", "title": f"{name} login needed", "retrying": retrying,
                "text": f"Log in to the {name} CLI again ({'codex login' if name == 'Codex' else 'claude, then /login'}); "
                        "the job continues by itself."}

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

    def pause(self, *, _for_restart=False):
        """Interrupt the author while retaining its native session for resume.

        ``_for_restart`` is the pause of a closing server: its cause, 'restart',
        resumes the author automatically when TCS Prover starts again.
        """

        with self.lock:
            self._check_owned_here()
            if self.state["phase"] in {"pausing", "paused"}:
                # A damaged checkpoint is left for Resume to report, but its
                # automatic retry stops.
                checkpoint = read_pause_object(self.run_dir)
                if checkpoint:
                    # A time-limit pause keeps its cause (it never resumes by itself anyway).
                    checkpoint.update(pauseCause="time-limit" if checkpoint.get("pauseCause") == "time-limit"
                                      else "user", autoResumeAt=0)
                    self._save_pause(checkpoint)
                self.state["autoResumeAt"] = 0
            if self.state["phase"] in {"pausing", "paused"}:
                self._settle_run_claim()
                return
            if (self.state["phase"] != "running" or self.state["activeNode"] != "author"
                    or self.state["stage"] not in {"solve", "repair"}):
                raise ValueError("Pause is available while the proof author is working.")
            checkpoint_state = self._author_checkpoint_state()
            cause = "restart" if _for_restart else "user"
            self._save_pause({"status": "pausing", "node": "author", "state": checkpoint_state,
                              "pauseCause": cause, "autoResumeAt": 0,
                              "elapsedSeconds": self._elapsed_seconds(), "stage": self.state["stage"],
                              "goalThreadId": self.state.get("goalThreadId", "")})
            self._save_controlled(PAUSE_REQUEST_FILENAME, "{}\n", run_copy=False)
            self.state.update(phase="pausing", error="", autoResumeAt=0)
            self._save_job_settings(self.state)
            self.add_trace({"kind": "status", "stage": self.state["stage"], "node": "author",
                            "label": "Paused for a restart" if _for_restart else "Pause requested",
                            "text": ("TCS Prover is closing: preserving the author conversation, which resumes "
                                     "automatically when TCS Prover starts again." if _for_restart
                                     else "Interrupting the author and preserving its saved conversation for Resume.")})
            process, token = self.process, self.active_token

        def finish_if_unresponsive():
            # Also covers workers started before pause support was installed.
            if process is None:
                return
            try:
                process.wait(timeout=PAUSE_ACKNOWLEDGE_SECONDS)
            except subprocess.TimeoutExpired:
                with self.lock:
                    if self.active_token is not token or self.state["phase"] != "pausing":
                        return
                    self.add_trace({"kind": "diagnostic", "stage": self.state["stage"],
                                    "text": "The author did not acknowledge the pause; closing its process. Resume will reopen the saved conversation."})
                stop_process_tree(process)
            self._settle_pause_without_reader(token)

        threading.Thread(target=finish_if_unresponsive, daemon=True).start()

    def _settle_pause_without_reader(self, token):
        """Keep a requested pause whose reader ended without settling the job."""

        time.sleep(1)  # A live reader settles the job itself right after the runner exits.
        with self.lock:
            if self.active_token is not token or self.state["phase"] != "pausing" or not self._reader_gone_locked():
                return
            self.process = self.active_token = self.worker_token = None
            checkpoint = read_pause_object(self.run_dir)
            try:
                if checkpoint.get("node") and checkpoint.get("stage"):
                    checkpoint["status"] = "paused"
                    self._configure_auto_resume(checkpoint)
                    self._save_pause(checkpoint)
            except (OSError, UnicodeError, TypeError, ValueError):
                pass
            self.state.update(phase="paused", finishedAt="")
            try:
                if checkpoint.get("node") and checkpoint.get("stage"):
                    self._time_limit_reached_locked(checkpoint)
            except (OSError, UnicodeError, TypeError, ValueError):
                pass
            self._settle_run_claim()

    def _check_launch_hold(self):
        """A held run must fail before credentials, workers or elapsed clocks."""
        if self.state.get("launchHold"):
            raise ValueError("Prepared run is held: " + str(self.state["launchHold"]))

    def _historical_hold_text(self, exc):
        """The hold shown after a failed historical check: its cause, then what happens next.

        A failed batch can quote a job's own hold, which already starts with
        "Needs attention:" or ends with the advice or the retry note; each is
        given once.
        """
        text = str(exc).strip()
        if isinstance(exc, retrieval.NeedsAttention):
            text = text if text.startswith("Needs attention:") else f"Needs attention: {text}"
        elif not text.startswith("Historical"):
            text = f"Historical search preflight failed: {text}"
        ending = HISTORICAL_RETRY_SUFFIX if isinstance(exc, retrieval.PreflightRetry) else HISTORICAL_FIX_ADVICE
        return text if text.endswith(ending) else text + ending

    def _hold_after_failed_check(self, exc):
        """Hold this job with the cause; a check that could not finish runs again every minute."""
        self.state["launchHold"] = self._historical_hold_text(exc)
        self.state["autoResumeAt"] = 0
        self._hold_retry_at = time.time() + AUTO_RESUME_SECONDS if isinstance(exc, retrieval.PreflightRetry) else 0
        self._save_job_settings(self.state)
        if self.state["phase"] == "paused" and self.run_dir and controlled_path(self.run_dir, PAUSE_FILENAME).exists():
            try:
                checkpoint = load_pause_checkpoint(self.run_dir)
                checkpoint["autoResumeAt"] = 0
                self._save_pause(checkpoint)
            except (OSError, ValueError):
                pass
        self.add_trace({"kind": "diagnostic", "stage": self.state.get("stage") or "solve",
                        "text": self.state["launchHold"]})
        # A held job starts its runner only after a passing check: keep no owner lock meanwhile.
        self._settle_run_claim()

    def _check_historical_locked(self):
        """Recheck a historical run's policy, configs and discovery before a process starts.

        A moved bundle is adopted and saved (retrieval.check_run). A problem no
        retry fixes holds the job with its message and no automatic retry.
        """
        if not self.state.get("retrievalPolicyPath"):
            return
        try:
            path = retrieval.check_run(self.state["retrievalPolicyPath"], self.run_dir)
        except retrieval.NeedsAttention as exc:
            self._hold_after_failed_check(exc)
            raise ValueError(self.state["launchHold"]) from exc
        self._adopt_policy_path_locked(path)

    def _adopt_policy_path_locked(self, path):
        """Use the policy path a check returned, with this run's own folder as the author's workspace.

        A moved checkout moves both: the policy then names the new paths, and the
        saved workspace follows the run folder.
        """
        workspace = str(self.run_dir.resolve())
        if path == self.state["retrievalPolicyPath"] and self.state.get("goalWorkspace") == workspace:
            return
        moved = path != self.state["retrievalPolicyPath"]
        self.state.update(retrievalPolicyPath=path, goalWorkspace=workspace)
        self._save_job_settings(self.state)
        if moved:
            self.add_trace({"kind": "status", "stage": self.state.get("stage") or "solve",
                            "label": "Historical policy moved",
                            "text": "This run's controller folder moved with the checkout; its policy now names "
                                    "the new paths (recorded in the search audit log). The cutoff is unchanged."})

    def rerun_historical_preflight(self):
        """Check a held historical job again; once every check passes, release its hold.

        A prepared job then starts and a paused one resumes; a stopped one waits
        for its own continuation. The checks can take a minute, so they run
        without the job lock: the job list, this job's page and the server's
        once-a-second loop never wait for them.
        """
        with self.lock:
            if (not self.state.get("retrievalPolicyPath") or not recheckable_hold(self.state.get("launchHold"))
                    or self.has_active_worker() or self.state["phase"] in ACTIVE_PHASES):
                raise ValueError("This job has no historical check to run again.")
            if getattr(self, "_hold_check_running", False):
                raise ValueError("This job's historical checks are already running.")
            self._hold_check_running = True
            hold, policy_path = self.state["launchHold"], self.state["retrievalPolicyPath"]
            stages = saved_workflow_stages(self.state)
        try:
            failure = None
            try:
                path = retrieval.check_run(policy_path, self.run_dir)
                retrieval.preflight_policy(path)
                if stages["writing"]:
                    retrieval.preflight_compiler(path)
            except ValueError as exc:
                failure = exc
            with self.lock:
                if (self.state.get("launchHold") != hold or self.state.get("retrievalPolicyPath") != policy_path
                        or self.has_active_worker() or self.state["phase"] in ACTIVE_PHASES
                        or not self.run_dir.is_dir()):
                    raise ValueError("This job changed while its historical checks ran; check it again.")
                if failure is not None:
                    self._hold_after_failed_check(failure)
                    raise ValueError(self.state["launchHold"]) from failure
                self.state["launchHold"] = ""
                self._hold_retry_at = 0
                self._adopt_policy_path_locked(path)
                self._save_job_settings(self.state)
                self.add_trace({"kind": "status", "stage": self.state.get("stage") or "solve",
                                "label": "Historical checks passed",
                                "text": f"The {historical_checks(stages)} passed their checks; the hold is released."})
                phase = self.state["phase"]
                try:
                    if phase == "prepared":
                        self.start_prepared_run()
                    elif phase == "paused" and not self.resume_refusal():
                        # A reply being written, or a used-up time limit, keeps the job paused for the user.
                        self.resume()
                except (OSError, ValueError, RuntimeError) as exc:
                    if not self.state.get("launchHold"):
                        # Hold it again with the cause: one that can pass by itself (a CLI
                        # updating, a full disk) is checked again every minute.
                        passing = isinstance(exc, (OSError, RuntimeError, TransientLaunchError,
                                                   retrieval.PreflightRetry))
                        self._hold_after_failed_check((retrieval.PreflightRetry if passing else ValueError)(
                            f"Historical run check passed, but the run did not start: {str(exc).rstrip('.')}."))
                    raise
        finally:
            with self.lock:
                self._hold_check_running = False
        return self

    def retry_historical_hold_if_due(self, now=None):
        """Run a historical check that could not finish again, every minute.

        The check runs in its own thread: it can take a minute, and the retry
        loop serves every job.
        """
        now = time.time() if now is None else now
        with self.lock:
            due = getattr(self, "_hold_retry_at", 0)
            if (not due or now < due or getattr(self, "_hold_check_running", False) or self.has_active_worker()
                    or self.state["phase"] in ACTIVE_PHASES or not recheckable_hold(self.state.get("launchHold"))):
                return False
            # Reserve the next attempt first: a failure must not retry every tick.
            self._hold_retry_at = now + AUTO_RESUME_SECONDS

        def check():
            try:
                self.rerun_historical_preflight()
            except (OSError, ValueError, RuntimeError):
                pass  # The hold names the cause; a check that could not finish stays scheduled.

        threading.Thread(target=check, daemon=True).start()
        return True

    def _check_launch_models_locked(self, roles=("author", "critic", "writer")):
        """Recheck saved role models before a launch from an idle job.

        API models, their keys and the Claude CLI can change while a job waits,
        so fail with a clear message instead of a runner error.
        """

        stages = saved_workflow_stages(self.state)
        models = {role: self.state[f"{role}Model"] for role in roles if stages[ROLE_STAGES[role]]}
        for role, model in models.items():
            if not providers.is_api_id(model):
                continue
            # Read strictly: a store that cannot be read now is not a removed model.
            try:
                configured = providers.api_model(model, cached=False) is not None
            except providers.StoreUnavailable as exc:
                raise TransientLaunchError(str(exc)) from exc
            if not configured:
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
            self._check_historical_locked()
            self._check_launch_models_locked()
            for role in ("author", "critic", "convention", "final"):
                if not saved_workflow_stages(self.state)[ROLE_STAGES[PROMPT_ROLES[role]]]:
                    continue
                self._save_prompt(role, self.state[f"{role}Prompt"])
            previous = dict(self.state)
            queued = bool(self.state.get("launchQueued"))
            now = datetime.now(timezone.utc).isoformat()
            self.state.update(preparedRun=False, goalResume=False, goalThreadId="",
                              goalWorkspace=str(self.run_dir.resolve()), authorSteerDelivered="",
                              elapsedSeconds=0, startedAt=now, resumedAt=now, finishedAt="",
                              error="", launchQueued=False, launchError="", autoResumeAt=0)
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
                self._settle_run_claim()
                self._save_job_settings(self.state)
                raise
            self.add_trace({"kind": "status", "stage": "solve", "node": "author",
                            "label": "Queued job started" if queued else "Prepared research started",
                            "text": "Started the author of this queued job." if queued
                            else "Started a fresh author session in the prepared workspace."})

    def occupies_slot(self):
        """Whether this job counts against the running-jobs limit (a model process runs or is about to)."""

        with self.lock:
            # A historical check that is running starts or resumes its job once it passes.
            return (self.state["phase"] in ACTIVE_PHASES or self.worker_token is not None
                    or getattr(self, "_hold_check_running", False))

    def _queued_review(self):
        """Whether this prepared job's start is its statement review (a folder batch with the review stage)."""

        return bool(self.state.get("preparedRun") and self.state.get("reviewInputRecorded")
                    and not self.state.get("skipStatementReview") and not self.state.get("review")
                    and saved_workflow_stages(self.state)["review"])

    def start_queued(self):
        """Start this queued (or prepared) job now: its statement review when it has one to run, else its author."""

        with self.lock:
            review = self.state["phase"] == "prepared" and self._queued_review()
        if review:
            self.retry_review()
        else:
            self.start_prepared_run()

    def queue_launch(self, problem=None, now=None):
        """Wait as a queued job, which the server starts when a running slot is free.

        After a start that failed (``problem``), the next try comes AUTO_RESUME_SECONDS later.
        """

        now = time.time() if now is None else now
        with self.lock:
            # A failed historical check holds the job instead, with its cause and advice (no retry).
            held = bool(self.state.get("launchHold"))
            error = (f"Could not start: {str(problem).rstrip('.') or type(problem).__name__}."
                     if problem and not held else "")
            self.state.update(phase="prepared", preparedRun=True, launchQueued=True, launchError=error, error=error,
                              queuedAt=self.state.get("queuedAt") or now,
                              autoResumeAt=now + AUTO_RESUME_SECONDS if error else 0)
            review = self._queued_review()
            self.state.update(stage="review" if review else "solve",
                              activeNode="statement_reviewer" if review else "author")
            try:
                self._save_job_settings(self.state)
            except (OSError, ValueError):
                pass  # The queue is in memory as well; the next start saves the settings again.

    def leave_queue(self):
        """Take a queued job out of the queue: it stays prepared and starts only when asked."""

        with self.lock:
            if self.state["phase"] != "prepared" or not self.state.get("launchQueued"):
                return False
            self.state.update(launchQueued=False, autoResumeAt=0, launchError="", error="")
            self._save_job_settings(self.state)
            return True

    def launch_due_at(self, now):
        """(When the server may start this job by itself, its queue order), or (inf, 0) when it may not.

        A paused job is due at its automatic retry (or when the settings it
        waited for change); a queued job from when it was queued, or at the
        next try after a failed start; a held historical job when its check
        runs again.
        """

        with self.lock:
            if self.worker_token is not None or self.state.get("runningElsewhere"):
                return math.inf, 0
            # A held job's check runs again even after a manual stop (a stopped job then
            # only gets its hold released, see rerun_historical_preflight).
            check = getattr(self, "_hold_retry_at", 0) if recheckable_hold(self.state.get("launchHold")) else 0
            if check and not getattr(self, "_hold_check_running", False):
                return check, 0
            if self.state.get("manuallyStopped"):
                return math.inf, 0
            if self.state["phase"] == "paused":
                if not self.state.get("autoResumeAt"):
                    try:
                        self._settings_changed_locked(now)
                    except OSError:
                        pass  # Saved at its next check (a full disk); the other jobs' starts go on.
                return self.state.get("autoResumeAt") or math.inf, 0
            if self.state["phase"] == "prepared" and self.state.get("launchQueued") and not self.state.get("launchHold"):
                queued = self.state.get("queuedAt") or 0
                return max(self.state.get("autoResumeAt") or 0, queued), queued
            if self.state["phase"] in {"input", "reviewed"} and self.state.get("reviewRetryAt"):
                return self.state["reviewRetryAt"], 0
            return math.inf, 0

    def start_queued_if_due(self, now=None):
        """Start this queued job when its next try has come; return whether a start was tried."""

        now = time.time() if now is None else now
        with self.lock:
            if (self.state["phase"] != "prepared" or not self.state.get("launchQueued") or self.state.get("launchHold")
                    or self.worker_token is not None or now < (self.state.get("autoResumeAt") or 0)):
                return False
            try:
                if not self._queued_review():
                    try:
                        saved_statement(self.run_dir)
                    except ValueError:
                        # A start that failed early (a full disk) may not have saved it yet.
                        self._save_controlled("checked-statement.md",
                                              "# Statement sent directly to the proof author\n\n"
                                              f"{self.state['draft']}\n\n# Statement review\n\nSkipped by the user.\n")
                self.start_queued()
            except Exception as exc:
                self.queue_launch(exc, now)
            return True

    def discard_unstarted_run(self):
        """Remove the folders of a job that was never registered or started (nothing in them is model work)."""

        if self.run_dir is not None and not self.fixed_trace and direct_run_directory(self.run_dir, self.runs):
            shutil.rmtree(controller_directory(self.run_dir), ignore_errors=True)
            shutil.rmtree(self.run_dir, ignore_errors=True)

    def resume(self):
        """Reopen this run with a new CLI process and the saved goal/thread."""

        with self.lock:
            refusal = self.resume_refusal()
            if refusal:
                raise ValueError(refusal)
        self._check_launch_hold()

        with self.lock:
            if self.state["phase"] != "paused" or self.has_active_worker():
                raise ValueError("Wait until the author is paused before resuming.")
            # Again under the lock that launches: a reply may have been queued while it was released.
            refusal = self.resume_refusal()
            if refusal:
                raise ValueError(refusal)
            self._check_historical_locked()
            if self._unsaved_checkpoint is not None:
                self._save_pause(self._unsaved_checkpoint)
                self._unsaved_checkpoint = None
            try:
                checkpoint = load_pause_checkpoint(self.run_dir)
            except (OSError, ValueError):
                # Without a usable checkpoint, a model problem is still reported first.
                self._check_launch_models_locked()
                raise
            self._check_launch_models_locked(checkpoint_roles(checkpoint["node"], saved_workflow_stages(self.state)))
            workflow_state = dict(checkpoint["state"])
            workflow_state.pop("paused", None)
            workflow_state.pop("failed", None)
            previous = dict(self.state)
            if checkpoint.get("goalThreadId"):
                self.state["goalThreadId"] = checkpoint["goalThreadId"]
            self.state.update(goalResume=True, elapsedSeconds=checkpoint["elapsedSeconds"],
                              resumedAt=datetime.now(timezone.utc).isoformat(), finishedAt="", error="", autoResumeAt=0,
                              sessionUnavailable=False, safetyStop=False, settingsProblem=None, waitingForSettings="")
            self._clear_pause_request()
            process = None
            try:
                self._save_pause({**checkpoint, "status": "running", "error": "",
                                  "pauseCause": "", "autoResumeAt": 0, "sessionUnavailable": False,
                                  "safetyStop": False, "settingsProblem": None})
                self._save_job_settings(self.state)
                # A LaTeX step runs its own graph, as every other final-stage launch does.
                final = checkpoint["node"] in runtime.builtin_workflow("clean_up")["nodes"]
                if final and checkpoint["node"] != "latex_editor" and isinstance(workflow_state.get("output"), str):
                    # Repairs keep the content of the saved source (latex_kept), as _launch_compile_locked does.
                    workflow_state.setdefault("edited_latex", workflow_state["output"])
                resume_options = (["--set", "goal_require_resume=true"]
                                  if self.state.get("goalThreadId") and not final else [])
                if self.state.get("pendingReanchor") and not final:
                    # A conversation turn compacted the author's context: send the full resume prompt.
                    resume_options.extend(["--set", "goal_reanchor=true"])
                process, token = self._launch_workflow_locked(
                    ["clean_up.yaml"] if final else ["author_critic.yaml", "clean_up.yaml"], "",
                    [*(self._final_options() if final else [
                        *self._proof_options_locked(), *resume_options,
                        "--elapsed-seconds", str(checkpoint["elapsedSeconds"])]),
                     "--start-node", checkpoint["node"]],
                    checkpoint["stage"], checkpoint["node"], state=workflow_state,
                )
                # The new reader starts from whether the author still has to receive the critic's report.
                self._carry_critic_feedback = checkpoint.get("criticFeedbackPending") is True
                self._spawn_worker(self._read_output, (process, token), token)
            except (OSError, ValueError, RuntimeError):
                self._carry_critic_feedback = False
                if process is not None:
                    stop_process_tree(process)
                self.state = previous
                self.process = self.active_token = self.worker_token = None
                self._settle_run_claim()
                self._save_pause(checkpoint)
                self._save_job_settings(self.state)
                raise
            self.add_trace({"kind": "status", "stage": checkpoint["stage"], "node": checkpoint["node"],
                            "label": "Resume requested",
                            "text": "Reopening the saved author conversation in this same run folder."
                            if checkpoint["node"] == "author" else
                            "Continuing at the saved step with its exact input in this same run folder."})
            # Related works added while paused reach the author once, as a live instruction.
            pending = list(self.state.get("relatedWorksPending") or [])
            if pending:
                try:
                    self._queue_related_works_notice_locked(pending)
                    self.state["relatedWorksPending"] = []
                    self._save_job_settings(self.state)
                except (OSError, UnicodeError, TypeError, ValueError) as exc:
                    self.add_trace({"kind": "diagnostic", "stage": RELATED_WORKS_STAGE,
                                    "text": f"The author could not be told about the related works added while it "
                                            f"was paused ({exc}); it is told when the run resumes next."})

    def continue_historical(self, action):
        """Continue a stopped historical run in its own folder under its fixed policy.

        Every new-run continuation is impossible here: the policy binds this
        workspace and cutoff. ``action`` is "author" (resuming the saved author
        conversation when it still exists, else a new one with the YAML resume
        prompt), "critic" (the saved proof) or "final" (the saved final input).
        Like a continuation in a new run, the time budget starts again.
        """
        with self.lock:
            if self.has_active_worker() or self.state["phase"] in ACTIVE_PHASES:
                raise ValueError("A model is already working on this job.")
            self._check_launch_hold()
            self._check_historical_locked()
            self._check_launch_models_locked()
            previous = dict(self.state)
            now = datetime.now(timezone.utc).isoformat()
            self._clear_manual_stop()
            self.state.update(elapsedSeconds=0, resumedAt=now, finishedAt="", error="", autoResumeAt=0,
                              sessionUnavailable=False, safetyStop=False, settingsProblem=None,
                              waitingForSettings="")
            if action == "author":
                thread = self.state.get("goalThreadId") or saved_goal_thread_id(self.run_dir)
                self.state.update(goalThreadId="" if previous.get("sessionUnavailable") else thread,
                                  goalResume=True, goalWorkspace=str(self.run_dir.resolve()))
            self._clear_pause_request()
            # A pause checkpoint belongs to the conversation being replaced; the
            # continuation starts its workflow, and its time budget, afresh.
            pause = controlled_path(self.run_dir, PAUSE_FILENAME)
            paused = pause.read_bytes() if pause.is_file() else None
            for path in (controller_directory(self.run_dir) / PAUSE_FILENAME, self.run_dir / PAUSE_FILENAME):
                path.unlink(missing_ok=True)
            process = None
            try:
                self._save_job_settings(self.state)
                if action == "author":
                    process, token = self._launch_solver_locked(saved_statement(self.run_dir))
                elif action == "critic":
                    source = saved_critic_source(self.run_dir)
                    process, token = self._launch_critic_resume_locked(source["statement"], source["solution"])
                elif action == "final":
                    final_input = saved_final_input(self.run_dir)
                    process, token = self._launch_saved_final_locked(final_input["statement"], final_input["solution"])
                else:
                    raise ValueError("This job has no saved stage available to continue.")
                self._spawn_worker(self._read_output, (process, token), token)
            except (OSError, ValueError, RuntimeError):
                if process is not None:
                    stop_process_tree(process)
                self.state = previous
                self.process = self.active_token = self.worker_token = None
                self._settle_run_claim()
                self._save_job_settings(self.state)
                if paused is not None:
                    self._save_controlled(PAUSE_FILENAME, paused.decode("utf-8"))
                raise
            # A pause checkpoint restore could not save belongs to the replaced conversation too.
            self._unsaved_checkpoint = None
            self.add_trace({"kind": "status", "stage": self.state["stage"], "node": self.state["activeNode"],
                            "label": "Historical run continued",
                            "text": "Continuing in this same run folder under the saved web cutoff."})

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

    def approved_proof(self):
        """Read the proof handed to the LaTeX stage (critic-approved when the critic runs), for a download."""

        with self.lock:
            if not self.snapshot()["canDownloadProof"]:
                raise ValueError("No approved proof is saved for this job.")
            return (saved_final_input(self.run_dir)["solution"] + "\n").encode("utf-8")

    def critic_diff(self, name):
        """Read one saved diff of the critic coordinator's changes, too long for its critic result."""

        name = str(name or "")
        if not CRITIC_DIFF_NAME.fullmatch(name):
            raise ValueError("Unknown saved changes.")
        with self.lock:
            if not self.run_dir:
                raise ValueError("Unknown saved changes.")
            path = controller_directory(self.run_dir) / CRITIC_DIFFS_FOLDER / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("These saved changes are no longer available.")
        return path.read_bytes()

    def latex_source(self):
        """Read the working LaTeX source, for an authenticated download before final.tex exists."""

        with self.lock:
            if not self.snapshot()["canDownloadSource"]:
                raise ValueError("No LaTeX source is saved for this job.")
            return (self.run_dir / "latex-source.tex").read_bytes()

    def _final_options(self):
        return [
            "--writer-model", self.state["writerModel"],
            "--reasoning-effort", self.state["reasoningEffort"],
            "--writer-effort", self.state["writerEffort"],
            "--reasoning-summary", self.state["reasoningSummary"],
            "--speed", self.state["speedMode"],
            "--final-prompt-file", str(self._save_prompt("final", self.state["finalPrompt"])),
            "--set", "workflow_stages=" + json.dumps({"review": False, "author": False, "critic": False, "writing": True}),
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

    def _launch_compile_locked(self, state):
        """Compile saved LaTeX source again; compiler errors go to the repair step, not the editor."""

        return self._launch_workflow_locked(
            ["clean_up.yaml"], "", [*self._final_options(), "--start-node", "latex_compile"],
            # Without an editor call, repairs keep the content of the saved source (latex_kept).
            "final", "latex_compile", state={"edited_latex": state["output"], **state},
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
        critic_count=DEFAULT_CRITIC_COUNT,
        author_subagents=DEFAULT_AUTHOR_SUBAGENTS,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        author_effort=None, critic_effort=None, writer_effort=None,
        author_prompt=None, critic_prompt=None, final_prompt=None, convention_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        continuation_source="", stopped_stage="",
        allow_external_source=False,
        source_file=None,
        retrieval_mode="live", retrieval_cutoff_utc="",
        _prepare_only=False,
        workflow_stages=None, original_content=None,
        related_works=None, folder_batch=None,
    ):
        """Send a statement directly to the proof author without review.

        ``related_works`` are upload tokens installed in the new run before
        anything starts (the caller discards them once the job is registered);
        ``folder_batch`` is the folder batch whose related works the job shares.
        """

        self._check_input_replacement()

        source_file = source_file_name(source_file)
        statement = str(statement).strip()
        if not statement:
            raise ValueError("Enter a problem statement.")
        if "\0" in statement:
            raise ValueError("The problem statement cannot contain NUL characters.")
        if len(statement) > MAX_STATEMENT_CHARS:
            raise ValueError(f"The problem statement is longer than {MAX_STATEMENT_CHARS:,} characters.")
        stages = normalize_workflow_stages(workflow_stages) if workflow_stages is not None else {
            "review": False, "author": True, "critic": True, "writing": True}
        if stages["review"]:
            raise ValueError("Start the selected statement review before later workflow stages.")
        tokens = self._new_related_works_tokens(related_works, stages)
        if continuation_source:
            saved_prompt = controlled_path(continuation_source, "prompts/author.txt")
            if saved_prompt.is_file():
                original_template = read_utf8(saved_prompt, "saved author prompt")
                if author_prompt is not None and str(author_prompt).strip() != original_template.strip():
                    raise ValueError("A continued author must keep its original author prompt; start a new job to change it.")
                author_prompt = original_template
        options = self._workflow_options(
            critic_rounds=critic_rounds,
            critic_count=critic_count,
            author_subagents=author_subagents,
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
            convention_prompt=convention_prompt,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            include_review=False,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
            workflow_stages=stages,
        )
        options["sourceFile"] = source_file
        options["originalContent"] = str(original_content if original_content is not None else statement)
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
            for name in ("author", "critic", "convention", "final"):
                if not stages[ROLE_STAGES[PROMPT_ROLES[name]]]:
                    continue
                self._save_prompt(name, options[f"{name}Prompt"])
            if options["retrievalMode"] == "historical":
                self._save("input.md", statement + "\n")
                options["retrievalPolicyPath"] = retrieval.prepare_policy(
                    self.run_dir, options["retrievalCutoffUtc"])
                options["goalWorkspace"] = str(self.run_dir.resolve())
            # A continuation stays in its source's folder batch.
            folder_batch = folder_batch or (saved_folder_batch(continuation_source) if continuation_source else "")
            if folder_batch:
                options["folderBatch"] = folder_batch
            # Related works are in the author's workspace before anything runs (a web-cutoff run audits them).
            works = self._install_new_related_works_locked(
                tokens, folder_batch, options.get("goalWorkspace") or self.run_dir,
                options.get("retrievalPolicyPath", ""))
            self._save_job_settings({
                **options,
                "problemMode": "statement",
                "skipStatementReview": True,
                "statementReviewOnly": False,
            })
            self._save_controlled(
                "checked-statement.md",
                "# Statement sent directly to the proof author\n\n"
                f"{statement}\n\n# Statement review\n\nSkipped by the user.\n",
            )
            failed_check = None
            if options["retrievalMode"] == "historical" and not _prepare_only:
                try:
                    retrieval.preflight_policy(options["retrievalPolicyPath"])
                    if stages["writing"]:
                        retrieval.preflight_compiler(options["retrievalPolicyPath"])
                except ValueError as exc:
                    # Keep the prepared job, held with the real cause, instead of
                    # an orphaned folder; a check that could not finish runs again.
                    failed_check = exc
            trace = self.state["trace"] if self.fixed_trace else []
            version = self.state["traceVersion"] if self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "problemMode": "statement",
                "skipStatementReview": True,
                "draft": statement,
                **options,
                "workflow": workflow_graph(stages),
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            self._record_new_related_works_locked(*works)
            if _prepare_only or failed_check is not None:
                # A folder-batch job: historical ones wait for their batch's
                # sandbox checks, live ones in the queue the server starts.
                historical = options["retrievalMode"] == "historical"
                self.state.update(phase="prepared", preparedRun=True,
                                  stage="solve", activeNode="author", startedAt="",
                                  launchHold=HISTORICAL_PENDING_HOLD if historical else "",
                                  launchQueued=not historical, queuedAt=0 if historical else time.time())
                if failed_check is not None:
                    self._hold_after_failed_check(failed_check)
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
        latex_source="",
    ):
        """Polish one combined theorem-and-proof input without earlier stages.

        ``latex_source`` (a saved latex-source.tex) is compiled at once instead.
        """

        self._check_input_replacement()

        source = str(source).strip()
        if not source:
            raise ValueError("Enter the theorem and proof.")
        if "\0" in source:
            raise ValueError("The theorem and proof cannot contain NUL characters.")
        if len(source) > MAX_STATEMENT_CHARS:
            raise ValueError(f"The theorem and proof are longer than {MAX_STATEMENT_CHARS:,} characters.")
        options = self._workflow_options(
            writer_model=writer_model,
            reasoning_effort=reasoning_effort,
            writer_effort=writer_effort,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            include_review=False,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
            workflow_stages={"review": False, "author": False, "critic": False, "writing": True},
        )
        options["originalContent"] = source
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
            self._save_prompt("final", options["finalPrompt"])
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
            if latex_source:
                self._save("latex-source.tex", latex_source)
                process, token = self._launch_compile_locked(
                    {"input": source, "statement": source, "source": source, "output": latex_source})
            else:
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
        latex_source="",
    ):
        """Retry normal finalization with its exact statement/proof contract.

        ``latex_source`` (a saved latex-source.tex) is compiled at once instead.
        """

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
            workflow_stages={"review": False, "author": False, "critic": False, "writing": True},
        )
        options["originalContent"] = solution
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
            self._save_prompt("final", options["finalPrompt"])
            self._save_job_settings({
                **options,
                "problemMode": "final-resume",
                "skipStatementReview": True,
                "statementReviewOnly": False,
            })
            self._save_controlled(
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
            if latex_source:
                self._save("latex-source.tex", latex_source)
                process, token = self._launch_compile_locked(
                    {"statement": statement, "solution": solution, "output": latex_source})
            else:
                process, token = self._launch_saved_final_locked(
                    statement, solution,
                )
        self._spawn_worker(self._read_output, (process, token), token)

    def start_critic_resume(
        self, statement, solution, source_run="",
        critic_rounds=DEFAULT_CRITIC_ROUNDS,
        critic_count=DEFAULT_CRITIC_COUNT,
        author_subagents=DEFAULT_AUTHOR_SUBAGENTS,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        author_effort=None, critic_effort=None, writer_effort=None,
        author_prompt=None, critic_prompt=None, final_prompt=None, convention_prompt=None,
        speed_mode=DEFAULT_SPEED,
        reasoning_summary=DEFAULT_REASONING_SUMMARY,
        retrieval_mode="live", retrieval_cutoff_utc="",
        workflow_stages=None, original_content=None,
    ):
        """Create a new job that audits one complete saved candidate proof; its critic starts afresh."""

        self._check_input_replacement()

        statement, solution = str(statement).strip(), str(solution).strip()
        if not statement or not solution:
            raise ValueError("A saved statement and complete proof are required.")
        if "\0" in statement or "\0" in solution:
            raise ValueError("Saved critic inputs cannot contain NUL characters.")

        options = self._workflow_options(
            critic_rounds=critic_rounds,
            critic_count=critic_count,
            author_subagents=author_subagents,
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
            convention_prompt=convention_prompt,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            include_review=False,
            retrieval_mode=retrieval_mode, retrieval_cutoff_utc=retrieval_cutoff_utc,
            workflow_stages=workflow_stages,
        )
        options["originalContent"] = original_content if original_content is not None else solution
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
                # It shares its source's workspace, and so its folder batch's related works.
                if saved_folder_batch(source_run):
                    options["folderBatch"] = saved_folder_batch(source_run)
            for name in ("author", "critic", "convention", "final"):
                if not options["workflowStages"][ROLE_STAGES[PROMPT_ROLES[name]]]:
                    continue
                self._save_prompt(name, options[f"{name}Prompt"])
            self._save_job_settings({
                **options,
                "problemMode": "critic-resume",
                "skipStatementReview": True,
                "statementReviewOnly": False,
            })
            self._save_controlled("checked-statement.md", f"# Checked statement\n\n{statement}\n")
            self._save(runtime.SAVED_CANDIDATE_FILENAME, solution + "\n")
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
                "workflow": workflow_graph(options["workflowStages"]),
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
                self._save_controlled(
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
                self._settle_run_claim()
                raise
        self._spawn_worker(self._read_output, (process, token), token)

    def _read_output(self, process, token=None):
        """Read one runner to its end; nothing it prints, and no failed write, leaves the job unsettled."""

        try:
            self._read_records(process, token)
        except Exception as exc:
            self._read_after_failure(process, token, exc)
        self._settle_run_claim()

    def _read_after_failure(self, process, token, failure):
        """Keep saving a runner's output after handling it failed, then settle as a restart would."""

        def save(record):
            try:
                self.add_trace(record)
            except Exception:
                pass  # The runner must still be drained; the transcript keeps what it can.

        save({"kind": "diagnostic", "stage": self.state["stage"] or "solve",
              "text": f"A runner event could not be handled ({type(failure).__name__}: {failure}). The job "
                      "keeps running; its remaining events are saved, and it settles from its saved files "
                      "when the runner ends."})
        try:
            for line in process.stdout:
                record = self.parse_line(line, self.state["stage"] or "solve")
                if record.get("kind") != "node_input":  # A step's input stays in memory only (see _read_records).
                    save(record)
        except Exception:
            try:
                stop_process_tree(process)  # Its output can no longer be read.
            except Exception:
                pass
        try:
            reap_runner(process)
        except Exception:
            pass
        self._settle_from_saved_run(process, token)

    def _settle_from_saved_run(self, process, token):
        """Settle a job whose reader failed with the outcome a restart would restore from its files."""

        self.flush_trace(force=True)  # The outcome is read from the transcript.
        conversation = None
        try:
            settled = restore_saved_app(App(self.trace_file, self.runs))
            restored, conversation, problem = settled.state, settled._conversation, None
        except Exception as exc:
            restored, problem = None, exc
        with self.lock:
            if self.active_token not in (None, token) or self.worker_token not in (None, token):
                return  # A newer run of this job owns it now.
            if self.process is process:
                self.process = None
            self.active_token = self.worker_token = None
            self._end_retry_wait_locked()
            self._model_retries, self._retry_starts = {}, {}
            self.state["modelRetry"] = None
            if restored is not None:
                self.state.update({key: restored[key] for key in SETTLED_FIELDS if key in restored})
                # The settled pause may have asked for its progress summary (see _restore_conversation).
                self._conversation = conversation
            else:
                self.state.update(phase="done", finishedAt=datetime.now(timezone.utc).isoformat(),
                                  error=f"The job's output reader failed ({str(problem) or type(problem).__name__}). "
                                        "Continue from a saved checkpoint.")

    def _read_records(self, process, token=None):
        """Store tagged solver events and build the visible final answer."""

        problem, answers, order, final, failed = "", {}, [], False, False
        latest_critic_solution = ""
        last_work_node = self.state["activeNode"]
        # Whether the critic rejected the latest proof and the author has not received that report yet;
        # a resumed run starts from its checkpoint's value.
        with self.lock:
            critic_feedback, self._carry_critic_feedback = self._carry_critic_feedback, False
        # The exact input of the step after the author that runs now: a runner
        # that ends during that step pauses there (see the end of this method).
        # A resumed step starts from its checkpoint until the runner reports it.
        resumed = read_pause_object(self.run_dir) if self.run_dir else {}
        step_input = (resumed if resumed.get("status") == "running" and isinstance(resumed.get("state"), dict)
                      and resumed.get("node") == last_work_node != "author" else None)
        # Only the first repair turn after an author submission starts a new
        # answer; live instructions and continuation turns extend it.
        repair_pending = False
        # Whether the author submitted during this runner (see the end of this method).
        submitted = False
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
                if record.get("kind") == "node_input":
                    # Kept in memory only, for this runner: it repeats the whole workflow state.
                    if isinstance(record.get("node"), str) and isinstance(record.get("state"), dict):
                        step_input = record
                    continue
                entry = self.add_trace(record)
                self._follow_model_retry(record)
                self._follow_author_question(record)
                if convention_verdict(entry):
                    # The latest verdict decides: a new loophole verdict shows again, whatever the user did
                    # with an earlier one, and a faithful verdict on a later accepted proof closes it.
                    with self.lock:
                        self.state["conventionWarning"] = convention_warning(entry)
                        self._save_job_settings(self.state)
                if (record.get("kind") == "critic_result" and record.get("root") is not False
                        and isinstance(record.get("report"), dict) and record["report"].get("verdict") == "reject"):
                    critic_feedback = True
                if (record.get("kind") == "request" and record.get("label") == "Exact solve input"
                        and record.get("stage") in {"solve", "repair"} and record.get("root") is not False):
                    # The author's turn carries the critic's report, and the full prompt a re-anchor needed.
                    critic_feedback = False
                    with self.lock:
                        if self.state.get("pendingReanchor"):
                            self.state["pendingReanchor"] = False
                            self._save_job_settings(self.state)
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
                repair = delivered_repair(record)
                if repair:
                    with self.lock:
                        self.state["authorRepairDelivered"] = repair
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
                        self.state["stage"] = record_stage
                        self.state["activeNode"] = event_node(record, self.state["activeNode"])
                # A critic round is one visit of the critic step, whose proof handoff and result carry
                # its number; its requests (the critics, the coordinator, their retries) are not rounds.
                if isinstance(record.get("round"), int):
                    with self.lock:
                        self.state["round"] = record["round"]
                if record.get("kind") == "author_result":
                    repair_pending = submitted = True
                if record.get("kind") == "final_input":
                    with self.lock:
                        self.state["finalInputReady"] = True
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
                            # A closing server's pause ('restart') resumes by itself at the next start.
                            if cause not in {"user", "error", "restart"}:
                                cause = "error" if reason else "user"
                            if (cause in {"error", "restart"} and record["node"] == "author"
                                    and str(reason).startswith(WORKFLOW_TIME_LIMIT_ERROR)):
                                # Reaching the time limit is the user's choice: no automatic resume,
                                # also when a closing server was pausing the author at that moment.
                                cause = "time-limit"
                            paused_stage = record["stage"]
                            if (record["node"] == "author"
                                    and record["state"].get("report", {}).get("verdict") == "reject"):
                                paused_stage = "repair"
                            # The saved thread ID is missing: retrying the same
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
                                              "safetyStop": safety, "criticFeedbackPending": critic_feedback,
                                              # A pause of this version: its summary is still to be asked
                                              # for, even if the reader never reaches its exit (P2).
                                              "summaryRequested": False})
                            self.state.update(phase="pausing", error=reason, sessionUnavailable=unavailable,
                                              safetyStop=safety)
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
                        proof = record.get("resultKind") == "proof" or not saved_workflow_stages(self.state)["writing"]
                        self.state["resultKind"] = "proof" if proof else "final"
                        final = True
                        self._save("proof-output.md" if proof else "final.tex", self.state["output"])
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
                        revision = record.get("resultKind") == "needs-revision"
                        self.state["resultKind"] = "needs-revision" if revision else "failure"
                        self.state["revisionRequired"] = revision
                        self.state["criticBugs"] = record.get("bugs", "") if revision else ""
                        self.state["output"] = (
                            record.get("output") or record.get("summary")
                            or record.get("text", "")
                        )
                        if revision and record.get("bugs"):
                            bugs = record["bugs"]
                            feedback = "\n".join(map(str, bugs)) if isinstance(bugs, list) else str(bugs)
                            self.state["output"] += "\n\n## Critic feedback\n\n" + feedback
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
                    # A question to the user is shown in its own box, never as part of the answer.
                    and not runtime.asks_user(item)
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
            code = reap_runner(process)
        except Exception as exc:
            # A record or a write that cannot be handled never stops a working runner.
            return self._read_after_failure(process, token, exc)
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
            self._model_retries, self._retry_starts = {}, {}
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
            # A runner that ended during a later step (killed, out of memory) resumes that step from its input.
            step_interrupted = (not stopped and not paused and not final and bool(problem or code)
                                and step_input is not None and last_work_node == step_input["node"] != "author")
            if (paused or author_interrupted or step_interrupted) and (not final or failed):
                if step_interrupted:
                    label = PUBLIC_GRAPH["nodes"].get(step_input["node"], {}).get("label", step_input["node"])
                    error = problem or f"The runner stopped unexpectedly during the {label} step (exit code {code})."
                    checkpoint.update(state=step_input["state"], node=step_input["node"],
                                      stage=str(step_input.get("stage") or self.state["stage"] or "final"))
                else:
                    error = checkpoint.get("error", "") if paused else (
                        problem or (self.state["output"] if failed else "")
                        or f"The author exited before completing the task (code {code}).")
                if author_interrupted and not paused:
                    workflow_state = {**checkpoint.get("state", {}), **self._author_checkpoint_state()}
                    if submitted:
                        # The state Resume saved may say why an earlier submission was refused: the runner
                        # dropped that reason when the author submitted again, so the author is not told it again.
                        workflow_state.pop("handoff_refused", None)
                    checkpoint.update(state=workflow_state, node="author",
                                      stage="repair" if workflow_state.get("report") else "solve")
                # Every new pause says whether its automatic progress summary was asked for (a legacy one does not).
                checkpoint.update(status="paused", elapsedSeconds=self._elapsed_seconds(),
                                  goalThreadId=self.state.get("goalThreadId", ""), error=error,
                                  criticFeedbackPending=critic_feedback, summaryRequested=False)
                if not paused:
                    checkpoint.update(pauseCause="error", autoResumeAt=0)
                self._configure_auto_resume(checkpoint)
                self._save_pause(checkpoint)
                self.state.update(phase="paused", error=error, activeNode=checkpoint["node"], stage=checkpoint["stage"], finishedAt="")
                self._time_limit_reached_locked(checkpoint)
            elif final:
                # A complete terminal record is durable and wins even when Stop
                # killed the child before it closed stdout cleanly.
                self._clear_manual_stop()
                self.state["error"] = (
                    ("Proof needs revision. See the critic feedback." if self.state.get("revisionRequired") else
                     "Workflow incomplete. See the saved checkpoint and failure report.")
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
            self._save_job_settings(self.state)
            # The author stopped running with this runner: its open question closes (unless a restart paused it).
            self._author_question_locked()


    def clear_trace(self):
        """Clear the saved transcript only when no request is active."""

        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Wait for the current model request to finish.")
            if self.has_active_worker():
                raise ValueError("The author is replying. Stop the reply first." if self._conversation_running_locked()
                                 else "Wait for the current model request to finish.")
            self.state["trace"] = []
            self.state["traceVersion"] = 0
            self.pinned = []
            if self.trace_file:
                self.trace_file.unlink(missing_ok=True)

    def stop(self):
        """Immediately stop the running CLI and all of its descendants."""

        with self.lock:
            self._check_owned_here()
            if self.state["phase"] == "stopping" or (
                self.state["phase"] == "done"
                and self.state["error"] == "Stopped."
            ):
                return
            if self.state["phase"] not in {"reviewing", "running", "pausing"}:
                raise ValueError("No model request is running in this job.")
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
            process, self.state["phase"] = self.process, "stopping"
            # A full disk or a planted entry must not leave the runner working after Stop.
            try:
                self._clear_pause_request()
                for path in (controller_directory(self.run_dir) / PAUSE_FILENAME, self.run_dir / PAUSE_FILENAME):
                    path.unlink(missing_ok=True)
                self._save_job_settings(self.state)
            except (OSError, UnicodeError, TypeError, ValueError):
                pass
        stop_process_tree(process)
        with self.lock:
            # Real jobs stay non-continuable until their reader has drained all
            # buffered output. Synthetic/no-worker callers, and jobs whose reader
            # ended without settling, settle here.
            if self.state["phase"] == "stopping" and self._reader_gone_locked():
                self.worker_token = None
            if self.state["phase"] == "stopping" and self.worker_token is None:
                self._finish_stopped_locked(
                    self.active_token,
                    "The model was stopped before it produced an answer.",
                )

    def set_author_time_limit(self, hours):
        """Replace the total-workflow time limit of a job with an Author stage, until it finishes.

        A running author reads it again within a moment; during the critic or
        LaTeX steps it applies at the author's next step; before launch, at
        launch; while pausing or paused, at Resume. It never schedules an
        automatic resume: a paused job continues only when the user resumes it.
        """

        try:
            hours = float(hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("The total time limit must be a number of hours.") from exc
        if not math.isfinite(hours) or hours < 0:
            raise ValueError("Set 0 for unlimited, or a positive number of hours.")
        with self.lock:
            self._check_owned_here()
            phase = self.state["phase"]
            if not (saved_workflow_stages(self.state)["author"] and phase in TIME_LIMIT_PHASES):
                raise ValueError("The time limit can be changed until the job finishes, when the job has an "
                                 "Author stage.")
            if phase == "running" and (self.process is None or self.process.poll() is not None):
                raise ValueError("The proof author is no longer running.")
            hours = round(hours, 10)
            self._write_author_limit(hours)
            self.state["thinkingHours"] = hours
            self._save_job_settings(self.state)
            elapsed = self._elapsed_seconds()
            text = ("The workflow has no configured time limit." if hours == 0
                    else f"The total workflow time limit is now {hours_text(hours)}.")
            if phase in {"running", "pausing"} and 0 < hours * 3600 <= elapsed:
                text += (f" That is at or below the {span_text(elapsed)} of active work so far: the proof author "
                         "stops at its next step and writes a progress summary; the critic and LaTeX steps still "
                         "finish.")
            elif phase == "paused" and 0 < hours * 3600 < elapsed + RESUME_MARGIN_SECONDS:
                text += f" Raise it above the {span_text(elapsed)} of active work so far, or set 0, to resume."
            # The critic or LaTeX step that runs now stays the active node.
            self.add_trace({
                "kind": "status", "stage": self.state["stage"], "node": self.state["activeNode"] or "author",
                "label": "Total time limit set", "text": text, "authorLimitHours": hours,
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
            if not self._author_running_locked():
                raise ValueError(
                    "Live instructions can only be sent while the proof author "
                    "is running."
                )
            if self.process is None or self.process.poll() is not None:
                raise ValueError("The proof author is no longer running.")
            return self._queue_live_instruction_locked(instruction)

    def _author_running_locked(self):
        return (self.state["phase"] == "running" and self.state["activeNode"] == "author"
                and self.state["stage"] in {"solve", "repair"})

    def _queue_live_instruction_locked(self, instruction, **fields):
        """Queue a checked instruction for the running author and record it as the user's live instruction."""

        command_id = self._write_author_steer(instruction)
        self.add_trace({
            "kind": "status", "stage": self.state["stage"],
            "node": "author", "label": "Live instruction queued",
            "text": instruction, "steerId": command_id, **fields,
        })
        return command_id

    # --- The author's questions ------------------------------------------------------------------------------------

    def _follow_author_question(self, record, now=None):
        """Keep the running author's newest question to the user, and note when the turn that asked it ends.

        A question replaces the open one, which closes with a note in the
        activity log, unless it is that one again (each is delivered when it
        starts and when it completes) or one that already closed. Its turn ends
        at that turn's turn/completed, at the start of another turn, or when a
        failed model call waits to retry.
        """

        if not isinstance(record, dict):
            return
        now = time.time() if now is None else now
        with self.lock:
            asked, current = author_question(record), self.state.get("authorQuestion")
            if asked and asked["id"] not in self._closed_questions:
                if current and current["id"] == asked["id"]:
                    current["questions"] = asked["questions"]
                else:
                    if current:
                        self._close_author_question_locked("The author's question closed: the author asked a "
                                                           "newer question.")
                    self.state["authorQuestion"] = {**asked, "askedAt": now, "turnEndedAt": None}
                    self._save_question_locked()
            elif current and current["turnEndedAt"] is None and self._ends_question_turn(record, current):
                current["turnEndedAt"] = now
                self._save_question_locked()
            self._author_question_locked(now)

    @staticmethod
    def _ends_question_turn(record, question):
        """Whether a root author record shows that the turn in which ``question`` was asked has ended."""

        if record.get("stage") not in {"solve", "repair"} or record.get("root") is False or record.get("activityLabel"):
            return False
        label = record.get("label")
        if record.get("kind") == "status" and isinstance(label, str) and label.endswith(runtime.RETRY_LABEL):
            return True
        event = record.get("event") if record.get("kind") == "codex_event" else None
        params = event.get("params") if isinstance(event, dict) and isinstance(event.get("params"), dict) else {}
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        method = event.get("method") if isinstance(event, dict) else None
        return ((method == "turn/completed" and (not question["turnId"] or turn.get("id") == question["turnId"]))
                or (method == "turn/started" and bool(turn.get("id")) and turn.get("id") != question["turnId"]))

    def _author_question_locked(self, now=None):
        """The author's open question after the closing rules, or None; a question that closes stays closed.

        It closes when the author stage stops running (the proof goes to the
        critic, a pause by the user, at the time limit or after a failure, a
        stop, the end of the job, any other step) and when the author no
        longer waits for it: its turn has ended and AUTHOR_QUESTION_WAIT_SECONDS
        have passed since it asked. A pause for a restart keeps it, without
        showing it, until the author continues. The open question is shown
        while the author runs (see snapshot).
        """

        question = self.state.get("authorQuestion")
        if not isinstance(question, dict) or self.state.get("runningElsewhere"):
            return None
        now = time.time() if now is None else now
        if not self._author_running_locked():
            restarting = (self.state["phase"] in {"pausing", "paused"} and self.state["activeNode"] == "author"
                          and self.run_dir is not None
                          and read_pause_object(self.run_dir).get("pauseCause") in RESTART_PAUSE_CAUSES)
            if restarting and question["turnEndedAt"] is None:
                question["turnEndedAt"] = now  # The turn ends with the author's stage.
                self._save_question_locked()
            if not restarting:
                self._close_author_question_locked("The author's question closed: the proof author is no longer "
                                                   "running.")
                return None
        if question["turnEndedAt"] is not None and now >= question["askedAt"] + AUTHOR_QUESTION_WAIT_SECONDS:
            self._close_author_question_locked(
                f"The author's question closed: the step in which it asked has ended and "
                f"{span_text(AUTHOR_QUESTION_WAIT_SECONDS)} have passed since it asked, so the author no longer "
                "waits for an answer.")
            return None
        return question

    def _close_author_question_locked(self, note="", label="Author question closed"):
        """Close the open question for good and say why in the activity log (an answer says it itself)."""

        question = self.state.get("authorQuestion")
        if not question:
            return
        self._closed_questions.add(question["id"])
        self.state["authorQuestion"] = None
        self._save_question_locked()
        if note:
            try:
                # Its own stage: the record says nothing about where the workflow is.
                self.add_trace({"kind": "status", "stage": AUTHOR_QUESTION_STAGE, "label": label, "text": note,
                                "questionId": question["id"]})
            except (OSError, UnicodeError, TypeError, ValueError):
                pass

    def _save_question_locked(self):
        # A failed write leaves the change in memory; the next settings save writes it.
        if self.run_dir is not None:
            try:
                self._save_job_settings(self.state)
            except (OSError, UnicodeError, TypeError, ValueError):
                pass

    def close_stale_author_question(self, now=None):
        """Apply the closing rules now, so a question closes on time even when no page asks (server tick)."""

        with self.lock:
            self._author_question_locked(now)

    def _open_question_locked(self, question_id, now=None):
        question = self._author_question_locked(now)
        if not question or not isinstance(question_id, str) or question["id"] != question_id:
            raise ValueError(AUTHOR_QUESTION_CLOSED)
        return question

    def answer_author_question(self, question_id, answers, now=None):
        """Send the answers to the author's open question as one live instruction, then close the question.

        ``answers`` is [{"index": i, "answer": text}], i indexing the question's
        questions; the empty ones are left out (see author_answer_text).
        """

        with self.lock:
            self._check_owned_here()
            question = self._open_question_locked(question_id, now)
            if not self._author_running_locked():
                raise ValueError("Answers can only be sent while the proof author is running.")
            if self.process is None or self.process.poll() is not None:
                raise ValueError("The proof author is no longer running.")
            instruction = author_answer_text(question["questions"], answers)
            if "\0" in instruction:
                raise ValueError("The answer cannot contain NUL characters.")
            if len(instruction) > runtime.AUTHOR_STEER_MAX_CHARS:
                raise ValueError(f"The answer is too long to send: with the question it answers it has "
                                 f"{len(instruction):,} characters, and a live instruction holds at most "
                                 f"{runtime.AUTHOR_STEER_MAX_CHARS:,}. Shorten it.")
            command_id = self._queue_live_instruction_locked(instruction, questionId=question["id"])
            self._close_author_question_locked()
            return command_id

    def dismiss_author_question(self, question_id, now=None):
        """Close the author's open question without sending anything; the author keeps working."""

        with self.lock:
            self._check_owned_here()
            self._open_question_locked(question_id, now)
            self._close_author_question_locked("Nothing was sent to the author; it keeps working without an answer.",
                                               label="Author question dismissed")

    @staticmethod
    def _public_question(question):
        """The open question as the job page shows it."""

        return {"id": question["id"], "askedAt": question["askedAt"], "turnId": question["turnId"],
                "questions": [{"title": entry["title"], "options": list(entry["options"])}
                              for entry in question["questions"]],
                "turnEnded": question["turnEndedAt"] is not None,
                "closesAt": question["askedAt"] + AUTHOR_QUESTION_WAIT_SECONDS}

    def dismiss_convention_warning(self, identity=None):
        """Ignore the convention critic's loophole warning; a later round's new warning shows again.

        ``identity`` is the time of the warning the page showed: a newer one
        that replaced it meanwhile is not ignored by that click.
        """

        with self.lock:
            self._check_owned_here()
            warning = self.state.get("conventionWarning")
            if not isinstance(warning, dict) or warning.get("dismissed"):
                raise ValueError("There is no loophole warning to ignore.")
            if identity is not None and identity != warning.get("time"):
                raise ValueError("A newer loophole warning replaced the one you ignored; it is shown now.")
            self.state["conventionWarning"] = {**warning, "dismissed": True}
            self._save_job_settings(self.state)

    # --- Time limit ----------------------------------------------------------------------------------------------

    def _time_limit_exhausted_locked(self, elapsed=None):
        """Whether a paused author's time limit is used up: less than RESUME_MARGIN_SECONDS of it is left."""

        state = self.state
        try:
            hours = float(state.get("thinkingHours") or 0)
            author = saved_workflow_stages(state)["author"]
        except (TypeError, ValueError):
            return False
        if not (state["phase"] == "paused" and author and state.get("activeNode") == "author" and hours > 0):
            return False
        elapsed = self._elapsed_seconds() if elapsed is None else elapsed
        return hours * 3600 - elapsed < RESUME_MARGIN_SECONDS

    def resume_refusal(self):
        """Why Resume must wait, or "": a reply being written, or a time limit that is used up."""

        with self.lock:
            if self._conversation_busy_locked():
                return CONVERSATION_BUSY
            elapsed = self._elapsed_seconds()
            if self._time_limit_exhausted_locked(elapsed):
                return (f"The time limit is used up ({span_text(elapsed)} of {hours_text(self.state['thinkingHours'])}). "
                        "Raise it, or set 0 for no limit, then resume.")
            return ""

    def _time_limit_reached_locked(self, checkpoint):
        """Record a new time-limit pause and ask the author for its progress summary (once per pause).

        Nothing starts here: the server's retry loop (or the terminal) starts the reply.
        """

        if checkpoint.get("pauseCause") != "time-limit" or checkpoint.get("summaryRequested") is True:
            return
        hours = float(self.state.get("thinkingHours") or 0)
        elapsed = saved_seconds(checkpoint.get("elapsedSeconds"), 0.0)
        self.add_trace({"kind": "status", "stage": checkpoint.get("stage") or "solve", "node": "author",
                        "label": "Time limit reached", "elapsedSeconds": elapsed, "authorLimitHours": hours,
                        "text": f"The proof author reached the time limit of {hours_text(hours)} after "
                                f"{span_text(elapsed)} of active work and is paused."})
        if not self._conversation_problem_locked() and not self._conversation_busy_locked():
            self._queue_conversation_locked("summary", "", "time-limit")
        checkpoint["summaryRequested"] = True
        self._save_pause(checkpoint)

    # --- Talking to a paused author --------------------------------------------------------------------------------

    def _conversation_busy_locked(self):
        return any(item["status"] in {"queued", "running"} for item in self._conversation)

    def _conversation_running_locked(self):
        return any(item["status"] == "running" for item in self._conversation)

    def conversation_busy(self):
        """Whether a summary or reply of the paused author is queued or being written."""

        with self.lock:
            return self._conversation_busy_locked()

    def _conversation_copies_locked(self):
        return [{**item, "notice": list(item.get("notice") or []),
                 **({"retry": dict(item["retry"])} if isinstance(item.get("retry"), dict) else {})}
                for item in self._conversation]

    def _conversation_item_locked(self, identity):
        return next((item for item in self._conversation if item["id"] == identity), None)

    def _conversation_problem_locked(self):
        """Why the paused author cannot be asked now, or "" when it can."""

        state = self.state
        try:
            author = saved_workflow_stages(state)["author"]
        except ValueError:
            author = False
        if not (state["phase"] == "paused" and author and state.get("activeNode") == "author"
                and state.get("stage") in {"solve", "repair"}):
            return "Talking to the author is available while the run is paused at the proof author."
        if not state.get("goalThreadId"):
            return "The author has not started its conversation yet."
        if state.get("sessionUnavailable"):
            return "The saved author conversation is no longer available."
        if state.get("safetyStop"):
            return "The author was stopped for safety; check the setup, then resume."
        if state.get("launchHold"):
            return f"Prepared run is held: {state['launchHold']}"
        if state.get("runningElsewhere"):
            return state["runningElsewhere"]
        return ""

    def _save_conversation_locked(self):
        """Save the conversation in the controller folder only (the author cannot change it)."""

        if self.run_dir is None:
            return
        items = [{key: value for key, value in item.items() if key != "retry"} for item in self._conversation]
        try:
            self._save_controlled(CONVERSATION_FILENAME, json.dumps({"schemaVersion": 1, "items": items},
                                                                    ensure_ascii=False, indent=2) + "\n",
                                  run_copy=False)
        except (OSError, UnicodeError, ValueError) as exc:
            try:
                self.add_trace({"kind": "diagnostic", "stage": "conversation", "node": "author",
                                "text": f"The author conversation could not be saved ({exc}); it is kept here and "
                                        "saved with its next change."})
            except OSError:
                pass

    def _load_conversation(self):
        """The saved conversation items (valid ones, oldest first); none when the file is missing or damaged."""

        try:
            value = json.loads((controller_directory(self.run_dir) / CONVERSATION_FILENAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError, TypeError):
            return []
        return saved_conversation_items(value)

    def _queue_conversation_locked(self, kind, text, origin):
        """Queue one summary or message for the paused author; the retry loop starts it."""

        item = {"id": "c_" + secrets.token_hex(5), "kind": kind, "origin": origin, "text": text, "status": "queued",
                "createdAt": datetime.now(timezone.utc).isoformat(), "dueAt": 0, "startedAt": "", "finishedAt": "",
                "attempts": 0, "reply": "", "error": "", "notice": [], "compacted": False}
        self._conversation.append(item)
        self._save_conversation_locked()
        summary = kind == "summary"
        self.add_trace({"kind": "status", "stage": "conversation", "node": "author", "conversationId": item["id"],
                        "label": "Progress summary queued" if summary else "Message for the paused author queued",
                        "text": "The author is asked for a progress summary." if summary else text})
        return item

    def _check_can_talk_locked(self):
        self._check_owned_here()
        problem = self._conversation_problem_locked()
        if problem:
            raise ValueError(problem)
        if self._conversation_busy_locked():
            raise ValueError(CONVERSATION_BUSY)

    def ask_author(self, text):
        """Queue one message for the paused author, which answers in its saved conversation."""

        if not isinstance(text, str) or not text.strip():
            raise ValueError("Enter a message for the author.")
        text = text.strip()
        if "\0" in text:
            raise ValueError("The message cannot contain NUL characters.")
        if len(text) > MAX_CONVERSATION_CHARS:
            raise ValueError(f"Keep the message at or below {MAX_CONVERSATION_CHARS:,} characters.")
        with self.lock:
            self._check_can_talk_locked()
            return self._queue_conversation_locked("message", text, "user")

    def request_author_summary(self):
        """Ask the paused author for a progress summary."""

        with self.lock:
            self._check_can_talk_locked()
            return self._queue_conversation_locked("summary", "", "user")

    def stop_author_reply(self, identity=None):
        """Cancel the queued or running reply (``identity``: the one the page shows); the job stays paused."""

        identity = identity or None
        with self.lock:
            self._check_owned_here()
            item = next((item for item in self._conversation if item["status"] in {"queued", "running"}), None)
            if item is None:
                raise ValueError("No author reply is being written.")
            if identity is not None and identity != item["id"]:
                raise ValueError("That reply has already finished.")
            running = item["status"] == "running"
            item.update(status="stopped", finishedAt=datetime.now(timezone.utc).isoformat())
            item.pop("retry", None)
            self._save_conversation_locked()
            self.add_trace({"kind": "status", "stage": "conversation", "node": "author", "conversationId": item["id"],
                            "label": "Author reply stopped", "text": "The reply was stopped; the job stays paused."})
            process = None
            if running:
                # Its reader sees that it no longer owns the job, and only saves.
                process, self.process, self.active_token = self.process, None, None
        if process is not None:
            threading.Thread(target=stop_process_tree, args=(process,), daemon=True).start()
        else:
            self._settle_run_claim()
        return item

    @staticmethod
    def _reply_failed_before(item):
        """Whether a queued or running reply already failed once (it waits for, or is in, a retry)."""

        return (item.get("attempts") or 0) >= 1 and bool(item.get("error") or item.get("retry"))

    def _stop_failing_reply_locked(self, item):
        """Stop a reply that keeps failing so that the run's own automatic retry can go on."""

        running = item["status"] == "running"
        item.update(status="stopped", finishedAt=datetime.now(timezone.utc).isoformat(),
                    error="Stopped so that the run could resume by itself; ask again when it pauses.")
        item.pop("retry", None)
        self._save_conversation_locked()
        self.add_trace({"kind": "status", "stage": "conversation", "node": "author", "conversationId": item["id"],
                        "label": "Author reply stopped",
                        "text": "The reply kept failing and was stopped so that the run could resume by itself; "
                                "ask again when it pauses."})
        if running:
            # Its reader sees that it no longer owns the job, and only saves.
            process, self.process, self.active_token = self.process, None, None
            threading.Thread(target=stop_process_tree, args=(process,), daemon=True).start()

    def _finish_conversation_locked(self, item, status, error=""):
        item.update(status=status, error=error, finishedAt=datetime.now(timezone.utc).isoformat())
        item.pop("retry", None)
        self._save_conversation_locked()
        self._settle_run_claim()  # Nothing more may start this run's runner by itself.

    def _delay_conversation_locked(self, item, now, error):
        """Try a queued reply again after the usual wait (a CLI updating, a launch that failed)."""

        item.update(dueAt=now + AUTO_RESUME_SECONDS, error=error)
        self._save_conversation_locked()
        return True

    def defer_conversation(self, problem, now=None):
        """Report a due reply whose start failed unexpectedly; it is tried again after the usual wait."""

        now = time.time() if now is None else now
        with self.lock:
            item = next((item for item in self._conversation if item["status"] == "queued"), None)
            if item is None:
                return
            self._delay_conversation_locked(item, now, f"Could not start the reply: {str(problem).rstrip('.')}. "
                                                       f"It is tried again in {retry_interval_text()}.")
            try:
                self.add_trace({"kind": "diagnostic", "stage": "conversation", "node": "author",
                                "conversationId": item["id"],
                                "text": f"The author's reply could not start ({problem}). Trying again in "
                                        f"{retry_interval_text()}."})
            except OSError:
                pass

    def _conversation_options_locked(self):
        """The runner options of one conversation turn: no time limit, steering, pause file or prompt files."""

        historical = bool(self.state.get("retrievalPolicyPath") or self.state.get("retrievalMode") == "historical")
        options = [
            "--converse",
            "--author-model", self.state["authorModel"], "--author-effort", self.state["authorEffort"],
            "--reasoning-effort", self.state["reasoningEffort"],
            "--reasoning-summary", self.state["reasoningSummary"], "--speed", self.state["speedMode"],
            "--thinking-hours", "0",
            "--set", "workflow_stages=" + json.dumps(CONVERSATION_STAGES, separators=(",", ":")),
            "--set", "goal_thread_id=" + json.dumps(self.state["goalThreadId"]),
            "--set", "goal_cwd=" + json.dumps(self.state.get("goalWorkspace") or str(self.run_dir)),
            "--set", "goal_require_resume=true",
            "--set", "run_tmp_dir=" + json.dumps(str(self._controller_directory() / RUN_TMP_FOLDER)),
        ]
        if not historical:
            options.extend(["--set", "goal_stderr_log=" + json.dumps(str(self.run_dir / AUTHOR_STDERR_FILENAME))])
        return options

    def start_conversation_if_due(self, now=None, check=None):
        """Start the first queued reply of a paused author once it is due; return whether anything changed.

        ``check()`` raises ValueError while another job works in this author's
        workspace. A reply runs outside the time budget, in its own runner
        (workflow_runner.py --converse), and never changes the phase or the
        pause checkpoint.
        """

        now = time.time() if now is None else now
        with self.lock:
            if self.state["phase"] != "paused" or self.has_active_worker() or self.state.get("runningElsewhere"):
                return False
            item = next((item for item in self._conversation if item["status"] == "queued"), None)
            if item is None or (item.get("dueAt") or 0) > now:
                return False
            resume_due = 0 < (self.state.get("autoResumeAt") or 0) <= now
            if resume_due and self._reply_failed_before(item):
                return False  # auto_resume_if_due stops it and resumes the run; no relaunch first.
            problem = self._conversation_problem_locked()
            if not problem and check is not None:
                try:
                    check()
                except ValueError:
                    problem = "Another job is working in this author workspace. Stop it, then ask again."
            if problem:
                self._finish_conversation_locked(item, "failed", problem)
                return True
            try:
                self._check_launch_models_locked(roles=("author",))
            except TransientLaunchError as exc:
                return self._delay_conversation_locked(item, now, str(exc))
            except ValueError as exc:
                self._finish_conversation_locked(item, "failed", str(exc))
                return True
            try:
                self._check_historical_locked()
            except retrieval.PreflightRetry as exc:
                return self._delay_conversation_locked(item, now, str(exc))
            except ValueError as exc:
                self._finish_conversation_locked(item, "failed", str(exc))
                return True
            # Related works added while paused are announced in front of this turn, exactly once.
            pending = list(self.state.get("relatedWorksPending") or [])
            request = {"schemaVersion": 1, "id": item["id"], "kind": item["kind"],
                       "text": item["text"] if item["kind"] == "message" else "",
                       "notice": related_works.notice_text(pending) if pending else ""}
            policy_path = self.state.get("retrievalPolicyPath")
            if policy_path and not item.get("audited"):
                # A web-cutoff author receives nothing unrecorded: the cutoff audit lists what the user sent.
                try:
                    retrieval_runtime.controller_audit(
                        retrieval_runtime.load_policy(policy_path), "author_message_sent",
                        conversationId=item["id"], conversationKind=item["kind"], text=request["text"],
                        textSha256=hashlib.sha256(request["text"].encode("utf-8")).hexdigest(),
                        textChars=len(request["text"]), notice=request["notice"])
                except Exception as exc:  # noqa: BLE001 - any audit failure keeps the message unsent.
                    return self._delay_conversation_locked(
                        item, now, f"Could not record the message in the cutoff audit: {str(exc).rstrip('.')}. "
                                   f"It is tried again in {retry_interval_text()}.")
                item["audited"] = True
            try:
                process, token = self._spawn_runner_locked(["author_critic.yaml"], json.dumps(request),
                                                           self._conversation_options_locked())
            except (OSError, ValueError, RuntimeError) as exc:
                return self._delay_conversation_locked(
                    item, now, f"Could not start the reply: {str(exc).rstrip('.') or type(exc).__name__}. "
                               f"It is tried again in {retry_interval_text()}.")
            item.update(status="running", startedAt=datetime.now(timezone.utc).isoformat(), error="",
                        attempts=int(item.get("attempts") or 0) + 1, notice=pending)
            item.pop("retry", None)
            self._save_conversation_locked()
            try:
                self._spawn_worker(self._read_conversation_output, (process, token, item["id"]), token)
            except Exception as exc:
                if self.process is process:
                    self.process = None
                if self.active_token is token:
                    self.active_token = None
                threading.Thread(target=stop_process_tree, args=(process,), daemon=True).start()
                item.update(status="queued", notice=[])
                return self._delay_conversation_locked(
                    item, now, f"Could not start the reply: {exc}. It is tried again in {retry_interval_text()}.")
            return True

    def _read_conversation_output(self, process, token, item_id):
        """Keep one conversation runner's records, then settle its item: answered, failed or queued again."""

        reply, compacted, failure, problem, code = None, False, "", "", None
        try:
            for line in process.stdout:
                if self.output_stream is not None:
                    try:
                        self.output_stream.write(line)
                        self.output_stream.flush()
                    except (OSError, UnicodeError):
                        self.output_stream = None
                try:
                    record = self.parse_line(line, "conversation")
                    # Every record of a reply belongs to the conversation (a restart skips them).
                    record["stage"] = "conversation"
                    record.setdefault("node", "author")
                    record["conversationId"] = item_id
                    self.add_trace(record)
                    kind, label = record.get("kind"), record.get("label")
                    if kind == "status" and isinstance(label, str) and label.endswith(runtime.RETRY_LABEL):
                        retry_at = record.get("retryAt")
                        with self.lock:
                            item = self._conversation_item_locked(item_id)
                            if item is not None and item["status"] == "running":
                                if finite_seconds(retry_at):
                                    item["retry"] = {"text": str(record.get("text") or ""), "retryAt": float(retry_at)}
                                else:
                                    item.pop("retry", None)
                    elif kind == "request":
                        with self.lock:
                            item = self._conversation_item_locked(item_id)
                            if item is not None:
                                item.pop("retry", None)  # The next attempt has started.
                    elif kind == "conversation_reply" and isinstance(record.get("text"), str) and record["text"].strip():
                        reply, compacted = record["text"], record.get("compacted") is True
                    elif kind == "status" and label == "Author reply failed" and record.get("final") is True:
                        failure = str(record.get("text") or "").strip() or "The author's reply failed."
                    elif kind == "diagnostic" and str(record.get("text") or "").startswith("error: "):
                        problem = str(record["text"])[7:].strip()
                except Exception as exc:  # One record that cannot be handled never stops the reply.
                    try:
                        self.add_trace({"kind": "diagnostic", "stage": "conversation", "node": "author",
                                        "conversationId": item_id,
                                        "text": f"A reply event could not be handled ({type(exc).__name__}: {exc})."})
                    except Exception:
                        pass
            code = reap_runner(process)
        except Exception:
            # Its output can no longer be read: end it; the item is asked again.
            try:
                stop_process_tree(process)
                code = process.poll()
            except Exception:
                pass
        with self.lock:
            if self.process is process:
                self.process = None
            cancelled = self.active_token is not token
            if self.active_token is token:
                self.active_token = None
            if self.worker_token is token:
                self.worker_token = None
            item = self._conversation_item_locked(item_id)
            if not cancelled and item is not None and item["status"] == "running":
                item.pop("retry", None)
                if reply is not None:
                    item.update(status="answered", reply=reply, compacted=compacted, error="",
                                finishedAt=datetime.now(timezone.utc).isoformat())
                    self._conversation_answered_locked(item)
                elif failure or problem:
                    self._finish_conversation_locked(item, "failed", failure or problem)
                else:
                    item.update(status="queued", dueAt=time.time() + AUTO_RESUME_SECONDS,
                                error=f"The reply was interrupted (exit code {code}); it is requested again in "
                                      f"{retry_interval_text()}.")
            self._save_conversation_locked()
        self._settle_run_claim()

    def _conversation_answered_locked(self, item):
        """Keep what an answered turn changed: the newest summary, a compacted context, delivered notices."""

        historical = bool(self.state.get("retrievalPolicyPath") or self.state.get("retrievalMode") == "historical")
        if item["kind"] == "summary" and not historical:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            try:
                self._save(PROGRESS_SUMMARY_FILENAME, f"# Progress summary ({stamp})\n\n{item['reply']}\n")
            except (OSError, UnicodeError, ValueError) as exc:
                self.add_trace({"kind": "diagnostic", "stage": "conversation", "node": "author",
                                "conversationId": item["id"],
                                "text": f"{PROGRESS_SUMMARY_FILENAME} could not be saved ({exc}); the summary is "
                                        "shown on the job page."})
        if item.get("compacted"):
            # The next Resume sends the full resume prompt instead of the short reopen one.
            self.state["pendingReanchor"] = True
        delivered = set(item.get("notice") or [])
        if delivered:
            self.state["relatedWorksPending"] = [name for name in self.state.get("relatedWorksPending") or []
                                                 if name not in delivered]
        try:
            self._save_job_settings(self.state)
        except (OSError, UnicodeError, TypeError, ValueError):
            pass  # Kept in memory; saved with the next change.

    def _restore_conversation(self, checkpoint, *, owned=True):
        """Load the saved conversation after a restart (see restore_saved_app).

        A paused job asks again for a reply the restart interrupted, and for the
        automatic summary of a time-limit pause that never asked for it; any
        other job's unfinished items end as stopped.
        """

        self._conversation = self._load_conversation()
        if not owned:
            return  # Another process owns the run: nothing in it is written.
        paused = self.state["phase"] == "paused"
        changed = False
        for item in self._conversation:
            if item["status"] == "running" and paused:
                item.update(status="queued", dueAt=0)
                changed = True
                try:
                    self.add_trace({"kind": "diagnostic", "stage": "conversation", "node": "author",
                                    "conversationId": item["id"],
                                    "text": "The author's reply was interrupted by a restart; it is requested again."})
                except OSError:
                    pass
            elif item["status"] in {"queued", "running"} and not paused:
                item.update(status="stopped", finishedAt=item.get("finishedAt") or datetime.now(timezone.utc).isoformat())
                changed = True
        if changed:
            self._save_conversation_locked()
        # Absent means a pause saved before automatic summaries existed: it gets none.
        if (paused and checkpoint.get("pauseCause") == "time-limit" and "summaryRequested" in checkpoint
                and checkpoint.get("summaryRequested") is not True):
            try:
                # Records "Time limit reached" too, which a pause cut short by a restart never got.
                self._time_limit_reached_locked(checkpoint)
            except OSError:
                if not self._conversation_problem_locked() and not self._conversation_busy_locked():
                    self._queue_conversation_locked("summary", "", "time-limit")
                checkpoint["summaryRequested"] = True

    # --- Related works -------------------------------------------------------------------------------------------

    def _author_workspace(self):
        """The author's workspace, which holds related-works/: its saved workspace, else the run folder."""

        return Path(self.state.get("goalWorkspace") or self.run_dir).resolve()

    def related_works_problem(self):
        """Why related works cannot be added to this job now, or "" when they can."""

        with self.lock:
            state = self.state
            try:
                author = saved_workflow_stages(state)["author"]
            except ValueError:
                author = False
            if not author:
                return "Related works are read by the proof author; this job has no Author stage."
            if state.get("runningElsewhere"):
                return state["runningElsewhere"]
            if state.get("readOnly"):
                return str(state.get("readOnlyReason") or "This saved job is read-only.")
            if self.run_dir is None or state["phase"] not in RELATED_WORKS_PHASES:
                return "This job has finished; continue it to add related works."
            return ""

    @staticmethod
    def _related_works_audit(policy_path):
        """record(entries) for an install into a web-cutoff run: its audit log lists what the user supplied.

        Raising undoes the install, so nothing is added unrecorded.
        """

        if not policy_path:
            return None

        def record(entries):
            policy = retrieval_runtime.load_policy(policy_path)
            retrieval_runtime.controller_audit(policy, "related_works_added",
                                               relatedWorks=[related_works.audit_fields(entry) for entry in entries])

        return record

    def _queue_related_works_notice_locked(self, names):
        """Tell the author about new related works through its live-instruction queue."""

        text = related_works.notice_text(names)
        steer_id = self._write_author_steer(text)
        self.add_trace({"kind": "status", "stage": self.state.get("stage") or "solve",
                        "node": self.state.get("activeNode") or "author", "label": "Live instruction queued",
                        "text": text, "steerId": steer_id, "relatedWorks": list(names)})

    def _announce_related_works_locked(self, names):
        """The notice rule: running or pausing, a live instruction now; paused, with the next message or at
        Resume; before the first launch, nothing (the first prompt points to the folder)."""

        if not names:
            return ""
        phase = self.state["phase"]
        if phase in {"running", "pausing"}:
            try:
                self._queue_related_works_notice_locked(names)
                return "instruction"
            except (OSError, UnicodeError, TypeError, ValueError) as exc:
                self.add_trace({"kind": "diagnostic", "stage": RELATED_WORKS_STAGE,
                                "text": f"The author could not be told about the new related works now ({exc}); "
                                        "it is told with the next message to the paused author or when the run "
                                        "resumes."})
        elif phase != "paused":
            return ""
        self.state["relatedWorksPending"] = [*(self.state.get("relatedWorksPending") or []),
                                             *(name for name in names
                                               if name not in (self.state.get("relatedWorksPending") or []))]
        self._save_job_settings(self.state)
        return "pending"

    def add_related_works(self, tokens, stage=None):
        """Add staged uploads to this job's related works and tell its author per the notice rule."""

        tokens = related_works_tokens(tokens)
        stage = stage or staged_uploads(self.runs)
        with self.lock:
            self._check_owned_here()
            problem = self.related_works_problem()
            if problem:
                raise ValueError(problem)
            workspace = self._author_workspace()
        with related_works.workspace_lock(workspace), self.lock:
            problem = self.related_works_problem()
            if problem:
                raise ValueError(problem)
            entries = related_works.install(workspace, controller_directory(workspace), tokens, stage,
                                            run_id=self.run_dir.name,
                                            record=self._related_works_audit(self.state.get("retrievalPolicyPath")))
            if entries:
                self.add_trace(related_works_record(entries))
                self._announce_related_works_locked([entry["name"] for entry in entries])
        return entries

    def link_batch_related_works(self, store, names, workspace_run=None):
        """Link new items of this job's folder batch into its workspace; {"entries", "diagnostic"}.

        ``workspace_run`` names the run recorded as adding them (the job that owns a shared workspace).
        """

        with self.lock:
            workspace = self._author_workspace()
            policy = self.state.get("retrievalPolicyPath")
            run_id = workspace_run or self.run_dir.name
        return store.link_into(workspace, controller_directory(workspace), run_id=run_id, names=names,
                               record=self._related_works_audit(policy))

    def batch_related_works_linked(self, batch, entries, diagnostic):
        """Record items of the folder batch linked into this job's workspace, and tell its author."""

        with self.lock:
            if entries:
                self.add_trace(related_works_record(entries, batch=batch))
            if diagnostic:
                self.add_trace({"kind": "diagnostic", "stage": RELATED_WORKS_STAGE, "folderBatch": batch,
                                "text": diagnostic})
            return self._announce_related_works_locked([entry["name"] for entry in entries])

    def _install_new_related_works_locked(self, tokens, folder_batch, workspace, policy_path, copy_from=""):
        """Put a new job's related works in its workspace before anything runs.

        Returns [(entries, batch ID or ""), ...] and a diagnostic, for the
        records added once the job's state exists (_record_new_related_works_locked).
        """

        workspace = Path(workspace).resolve()
        manifest = controller_directory(workspace)
        record = self._related_works_audit(policy_path)
        store = folder_batch_store(self.runs, folder_batch) if folder_batch else None
        added, diagnostic = [], ""
        if copy_from:
            # "Retry statement review": the new run gets the source's related works (batch ones linked again).
            source = Path(copy_from).resolve()
            saved = saved_goal_workspace(source)
            source_workspace = Path(saved).resolve() if saved else source
            copied = []

            def keep(entries):
                if record is not None:
                    record(entries)
                copied.extend(entries)

            related_works.copy_into(source_workspace, controller_directory(source_workspace), workspace, manifest,
                                    run_id=self.run_dir.name, batch=store, record=keep)
            added.append((copied, ""))
        if tokens:
            added.append((related_works.install(workspace, manifest, tokens, staged_uploads(self.runs),
                                                run_id=self.run_dir.name, keep_staged=True, record=record), ""))
        if store is not None:
            outcome = store.link_into(workspace, manifest, run_id=self.run_dir.name, record=record)
            added.append((outcome["entries"], folder_batch))
            diagnostic = outcome["diagnostic"]
        return [(entries, batch) for entries, batch in added if entries], diagnostic

    @staticmethod
    def _new_related_works_tokens(value, stages):
        """The upload tokens a new job attaches ([] for none); only a job with an Author stage takes them."""

        if value is None or value == []:
            return []
        tokens = related_works_tokens(value)
        if not stages["author"]:
            raise ValueError("Related works are read by the proof author; turn on the Author stage or remove them.")
        return tokens

    def _record_new_related_works_locked(self, added, diagnostic):
        for entries, batch in added:
            self.add_trace(related_works_record(entries, batch=batch))
        if diagnostic:
            self.add_trace({"kind": "diagnostic", "stage": RELATED_WORKS_STAGE, "text": diagnostic})

    def reset(self):
        """Return to the first screen when no child is active."""

        self._check_input_replacement()

        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Stop the current work first.")
            if self.has_active_worker():
                raise ValueError("The author is replying. Stop the reply first." if self._conversation_running_locked()
                                 else "Wait for the current model request to finish.")
            queued = [item for item in self._conversation if item["status"] == "queued"]
            for item in queued:
                self._finish_conversation_locked(item, "stopped")
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
    """Return all deduplicated user steering of a stopped author run (and the runs it continued), oldest first."""

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
    return instructions


# Model-facing: the restored block that points a continued author to its instruction history.
RESTORED_INSTRUCTIONS_NOTE = (
    f"Your earlier live instructions from the user are saved, oldest first, in {INSTRUCTION_HISTORY_FILENAME} "
    "in your working directory. Read that file and keep following them; the newest ones are repeated below."
)


def instruction_history(instructions):
    """The text of INSTRUCTION_HISTORY_FILENAME: every earlier live instruction, oldest first."""

    parts = ["# Earlier live instructions", "The user's live instructions to the proof author, oldest first."]
    for number, instruction in enumerate(instructions, start=1):
        parts += [f"## {number}", instruction]
    return "\n\n".join(parts) + "\n"


def saved_goal_thread_id(run_dir):
    """Recover the author session ID from existing settings or public statuses."""

    try:
        settings = json.loads(controlled_path(run_dir, JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
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


def saved_job_setting(run_dir, key):
    """One value of a run's saved job settings (the controller's copy), or None."""

    try:
        settings = json.loads(controlled_path(run_dir, JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return settings.get(key) if isinstance(settings, dict) else None


def saved_goal_workspace(run_dir):
    """The author workspace a run saved, or ""."""

    value = saved_job_setting(run_dir, "goalWorkspace")
    return value if isinstance(value, str) and value and "\0" not in value else ""


def saved_folder_batch(run_dir):
    """The folder batch whose related works a run shares, or ""."""

    value = saved_job_setting(run_dir, "folderBatch")
    return value if isinstance(value, str) and FOLDER_BATCH_ID.match(value) else ""


def saved_goal_options(run_dir):
    """Recover generic session/workspace settings without reading author files."""

    run_dir = Path(run_dir).resolve()
    try:
        settings = json.loads(controlled_path(run_dir, JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        settings = {}
    workspace = settings.get("goalWorkspace") if isinstance(settings, dict) else None
    workspace = Path(workspace).expanduser().resolve() if isinstance(workspace, str) and workspace else run_dir
    if not workspace.is_dir():
        raise ValueError(f"The saved author workspace no longer exists: {workspace}")
    return {"goalWorkspace": str(workspace), "goalResume": True,
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


def checkpoint_artifact_signature(run_dir):
    """Return a cheap cache key for files that define durable checkpoints."""

    if not run_dir:
        return ()
    run_dir = Path(run_dir)
    signature = []
    for name in ("checked-statement.md", "SOLUTION.md", runtime.SAVED_CANDIDATE_FILENAME):
        try:
            stat = (run_dir / name).stat()
            signature.append((name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((name, 0, 0))
    return tuple(signature)


def saved_run_checkpoints(run_dir):
    """Describe every durable, resumable critic checkpoint in one run.

    The saved proof candidate is the only one; every listed continuation is a
    new run, whose critic starts afresh.
    """

    run_dir = Path(run_dir)
    statement_ready = bool(
        controlled_path(run_dir, "checked-statement.md").is_file()
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
    return checkpoints


def saved_setting_problem(key, value):
    """Why one saved job setting cannot be used, or "" when it can; the default replaces it."""

    if key == "thinkingHours":
        try:
            valid = finite_seconds(value) and runtime.author_hours(value) == value
        except runtime.Error:
            valid = False
    elif key in {"elapsedSeconds", "queuedAt"}:
        valid = finite_seconds(value)
    elif key == "criticRounds":
        try:
            valid = runtime.critic_limit(value) == value
        except runtime.Error:
            valid = False
    elif key == "criticCount":
        try:
            # Saved as a whole number: a launch passes it to the runner as text.
            valid = isinstance(value, int) and valid_critic_count(value) == value
        except ValueError:
            valid = False
    elif key == "authorSubagents":
        try:
            valid = isinstance(value, int) and valid_author_subagents(value) == value
        except ValueError:
            valid = False
    elif key.endswith(("Model", "Effort")) or key in {
            "speedMode", "reasoningSummary", "problemMode", "goalThreadId", "goalWorkspace",
            "authorSteerDelivered", "resumedAt", "launchError"}:
        valid = isinstance(value, str)
    else:
        return ""
    return "" if valid else f"The saved {key} setting in {JOB_SETTINGS_FILENAME} is invalid; the default is used."


def restore_record(record):
    """The part of one transcript record that restoring needs after its single pass, or None."""

    kind = record.get("kind")
    if kind == "critic_result":
        return {key: record.get(key) for key in ("kind", "stage", "round", "report", "time")}
    if kind == "author_result":
        return {key: record.get(key) for key in ("kind", "stage", "text", "time")}
    if kind == "proof_handoff":
        return {"kind": kind, "manifest": record.get("manifest")}
    if kind == "status" and record.get("label") in {"Workflow step: critic", "Resume requested"}:
        # Which critic round a restart interrupted (see interrupted_critic_visit).
        return {key: record.get(key) for key in ("kind", "stage", "label", "node", "visit", "verify")}
    if kind == "request":
        return {"kind": kind, "stage": record.get("stage")}
    if kind in {"final_result", "failure_result", "review_result"}:
        return {"kind": kind}
    if kind == "status" and record.get("label") in {"Critic approved", "Stop requested", "Statement review failed"}:
        return {key: record.get(key) for key in ("kind", "stage", "label", "node", "time", "text")}
    if kind == "convention_result":
        # The convention critic answered: its step is over (see interrupted_stage_checkpoint).
        return {key: record.get(key) for key in ("kind", "stage", "round", "verdict", "time")}
    return None


# Steps that start again from their exact saved input when a restart interrupts them.
INTERRUPTIBLE_NODES = {"critic", "convention", "latex_editor", "latex_compile", "latex_repair"}


def interrupted_critic_candidate(run_dir, records):
    """The exact proof an interrupted critic round was reviewing.

    The saved candidate file when the round's capture record proves it is that
    proof; otherwise the critic's own last repair (in a later round), else the
    author's last submission, which the critic resolves again.
    """

    path = Path(run_dir) / runtime.SAVED_CANDIDATE_FILENAME
    try:
        saved = path.read_text(encoding="utf-8") if path.is_file() and not path.is_symlink() else ""
    except (OSError, UnicodeError):
        saved = ""
    author = critic = capture = None
    for index, record in enumerate(records):
        if record.get("kind") == "author_result":
            author, capture = (index, record), None
        elif record.get("kind") == "critic_result":
            critic = (index, record)
        elif record.get("kind") == "proof_handoff":
            capture = record.get("manifest")
    files = capture.get("files") if isinstance(capture, dict) else None
    proof = files.get("proof.md") if isinstance(files, dict) else None
    if saved and isinstance(proof, dict) and proof.get("sha256") == hashlib.sha256(saved.encode("utf-8")).hexdigest():
        return saved
    if critic is not None and (author is None or critic[0] > author[0]):
        report = critic[1].get("report")
        repaired = report.get("solution") if isinstance(report, dict) else None
        return repaired.strip() if isinstance(repaired, str) and repaired.strip() else saved
    if author is not None and isinstance(author[1].get("text"), str) and author[1]["text"].strip():
        return author[1]["text"]
    return saved


def interrupted_critic_visit(records, running=None):
    """Which critic round a restart interrupted, as the runner's stepVisit record; None for a first round.

    A step record names its visit and the instruction of a final read-only
    check (see runtime.STEP_VISIT_KEY); the step's input record never reaches
    the transcript. A round resumed before its runner reported the step
    continues the visit that its Resume passed on: ``running`` is the state
    of the running pause checkpoint of a Resume at the critic.
    """

    for record in reversed(records):
        if record.get("kind") in {"author_result", "critic_result"}:
            return None
        if record.get("kind") != "status":
            continue
        if record.get("label") == "Workflow step: critic":
            visit, verify = record.get("visit"), record.get("verify")
            verify = verify if isinstance(verify, str) and verify else None
            if type(visit) is not int or visit < 1 or (visit == 1 and not verify):
                return None
            return {"node": "critic", "visit": visit, **({"verify": verify} if verify else {})}
        if record.get("label") == "Resume requested":
            saved = (running or {}).get(runtime.STEP_VISIT_KEY)
            return dict(saved) if record.get("node") == "critic" and isinstance(saved, dict) else None
    return None


def interrupted_stage_checkpoint(run_dir, node, state, records, running=None):
    """Pause checkpoint fields that repeat an interrupted critic or LaTeX step from its exact saved input.

    ``running`` is the state that a Resume at the critic passed to the runner (see interrupted_critic_visit).
    """

    try:
        if node in {"critic", "convention"}:
            author = critic = approved = convention = None
            for index, record in enumerate(records):
                if record.get("kind") == "author_result":
                    author = index
                elif record.get("kind") == "critic_result":
                    critic = (index, record)
                elif record.get("kind") == "status" and record.get("label") == "Critic approved":
                    approved = index
                elif record.get("kind") == "convention_result":
                    convention = index
            latest = max((index for index in (author, critic[0] if critic else None, approved, convention)
                          if index is not None), default=None)
            if latest is not None and latest == convention:
                # The convention critic answered just before the runner ended: the LaTeX editor is next,
                # when the writing stage is selected.
                if not state["workflowStages"]["writing"]:
                    return None
                return interrupted_stage_checkpoint(run_dir, "latex_editor", state, records)
            if latest is not None and latest == approved:
                # The critic approved the proof just before the runner ended, or the convention critic was
                # interrupted: it checks the accepted proof again.
                report = critic[1].get("report") if critic is not None else None
                accepted = report.get("solution") if isinstance(report, dict) else None
                if not isinstance(accepted, str) or not accepted.strip():
                    accepted = interrupted_critic_candidate(run_dir, records)
                if not accepted.strip():
                    return None
                return {"node": "convention", "stage": "critic",
                        "state": {"statement": saved_statement(run_dir), "solution": accepted.strip(),
                                  "output": accepted.strip(), "round": critic[1].get("round") if critic else None},
                        "error": "The convention critic was interrupted when its process ended; it checks the "
                                 "accepted proof again automatically."}
            report = critic[1].get("report") if critic is not None and latest == critic[0] else None
            if isinstance(report, dict) and report.get("verdict") == "reject":
                # The critic rejected the proof just before the runner ended: the author repairs it,
                # when the author stage is selected.
                if not state["workflowStages"]["author"]:
                    return None
                return {"node": "author", "stage": "repair",
                        "state": {"statement": saved_statement(run_dir), "report": report,
                                  "solution": report.get("solution", ""), "round": critic[1].get("round")},
                        "error": "The previous author process ended without a final answer; "
                                 "its saved conversation continues automatically."}
            candidate = interrupted_critic_candidate(run_dir, records)
            if not candidate.strip():
                return None
            resumed = {"statement": saved_statement(run_dir), "solution": candidate}
            # The interrupted round keeps its number (and a final check stays read-only), so the
            # reports its critics saved are reused.
            visit = interrupted_critic_visit(records, running)
            if visit:
                resumed[runtime.STEP_VISIT_KEY] = visit
            return {"node": "critic", "stage": "critic", "state": resumed,
                    "error": "The critic was interrupted when its process ended; it continues automatically "
                             "with the same saved proof."}
        if state["problemMode"] == "latex":
            source = read_utf8(run_dir / "latex-input.md", "saved LaTeX input").strip()
            base = {"input": source, "statement": source, "source": source}
        else:
            base = saved_final_input(run_dir, records=records)
        latex = run_dir / "latex-source.tex"
        if node != "latex_editor" and latex.is_file() and not latex.is_symlink():
            compiled = read_utf8(latex, "saved LaTeX source")
            # Repairs keep the content of the saved source (latex_kept), as of the editor's version.
            return {"node": "latex_compile", "stage": "final",
                    "state": {**base, "output": compiled, "edited_latex": compiled},
                    "error": "The LaTeX step was interrupted when its process ended; the saved LaTeX source "
                             "is compiled again automatically."}
        return {"node": "latex_editor", "stage": "final", "state": base,
                "error": "The LaTeX editor was interrupted when its process ended; it starts again "
                         "automatically from the saved final input."}
    except ValueError:
        return None


def restore_saved_app(app, *, owned=True):
    """Reconstruct one idle historical job for a newly started Web UI.

    ``owned`` False restores a run that another process owns: nothing in it
    is written and no automatic resume is scheduled.
    """

    run_dir = app.run_dir
    if not run_dir:
        return app
    # One streaming pass over the transcript; only the records needed after it are kept.
    relevant, first_time, last_time, any_records = [], "", "", False
    state = empty_state(app.state["trace"], app.state["traceVersion"])
    state.pop("workflowStages", None)  # Older jobs derive stages from their saved workflow mode.
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

    settings_path = controlled_path(run_dir, JOB_SETTINGS_FILENAME)
    persisted_settings = set()
    # The loophole warning as last saved: whether the user ignored it (the transcript holds the warning itself).
    saved_warning = None
    # Roles whose saved model was removed (DeepSeek) and now runs as its replacement.
    replaced_roles, settings_warnings = set(), []
    settings_unreadable = False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings_unreadable = not isinstance(settings, dict)
        if isinstance(settings, dict):
            try:
                state["sourceFile"] = source_file_name(settings.get("sourceFile"))
            except ValueError:
                pass
            api_models = saved_api_models(settings.get("apiModels"))
            if api_models:
                state["apiModels"] = api_models
            # Roles a restart already moved to their replacement keep their warning.
            if isinstance(settings.get("replacedRoles"), list):
                replaced_roles.update(role for role in settings["replacedRoles"] if role in ROLES)
            for key in (
                "reviewModel", "authorModel", "criticModel", "writerModel",
                "reasoningEffort", "reviewEffort", "authorEffort",
                "criticEffort", "writerEffort", "criticRounds", "criticCount", "authorSubagents",
                "thinkingHours", "speedMode", "reasoningSummary",
                "problemMode", "skipStatementReview", "statementReviewOnly", "preparedRun",
                "workflowStages", "originalContent",
                "launchHold", "retrievalPolicyPath", "retrievalMode", "retrievalCutoffUtc",
                "goalThreadId", "goalWorkspace", "goalResume", "authorSteerDelivered", "elapsedSeconds", "resumedAt",
                "authorRepairDelivered",
                "launchQueued", "queuedAt", "launchError",
            ):
                if key in settings:
                    if key == "workflowStages":
                        try:
                            state[key] = normalize_workflow_stages(settings[key])
                            persisted_settings.add(key)
                        except ValueError:
                            state["launchHold"] = "Invalid saved workflow stages; repair before launch."
                            settings_warnings.append("Saved workflow stages are invalid; this run is held.")
                        continue
                    if key == "originalContent" and not isinstance(settings[key], str):
                        continue
                    if key in {"launchHold", "retrievalPolicyPath", "retrievalMode", "retrievalCutoffUtc"} and not isinstance(settings[key], str):
                        state["launchHold"] = "Invalid saved historical launch controls; repair before launch."
                        continue
                    problem = saved_setting_problem(key, settings[key])
                    if problem:
                        settings_warnings.append(problem)
                        continue
                    if key in {
                        "skipStatementReview", "statementReviewOnly", "preparedRun", "launchQueued",
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
                    if key.endswith("Model"):
                        state[key] = restored_model(settings[key])
                        if state[key] != settings[key]:
                            replaced_roles.add(key[:-len("Model")])
                        continue
                    state[key] = settings[key]
            # A value of the wrong type (the run folder's copy is author-writable) keeps the default.
            if isinstance(settings.get("pendingReanchor"), bool):
                state["pendingReanchor"] = settings["pendingReanchor"]
            if isinstance(settings.get("relatedWorksPending"), list):
                state["relatedWorksPending"] = [name for name in settings["relatedWorksPending"]
                                                if isinstance(name, str) and name and "\0" not in name]
            if isinstance(settings.get("folderBatch"), str) and FOLDER_BATCH_ID.match(settings["folderBatch"]):
                state["folderBatch"] = settings["folderBatch"]
            # Kept below only when the author continues after this restart.
            state["authorQuestion"] = saved_author_question(settings.get("authorQuestion"))
            saved_warning = settings.get("conventionWarning")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError, RecursionError):
        settings_unreadable = True
    if settings_unreadable:
        settings_warnings.append(f"The saved settings ({JOB_SETTINGS_FILENAME}) cannot be read; "
                                 "this job shows and uses the default settings.")
        if (run_dir / "input.md").is_file() and not state.get("retrievalPolicyPath"):
            # Its web-cutoff policy may be among the lost settings: never run it as a live search.
            state["launchHold"] = (f"The saved settings ({JOB_SETTINGS_FILENAME}) of this run cannot be read, so "
                                   "its web-cutoff policy is unknown. Restore them before launch.")
    for name in ("review", "author", "critic", "convention", "final"):
        prompt_path = controlled_path(run_dir, f"prompts/{name}.txt")
        try:
            # A retired default gives way to the current one (see current_prompt).
            state[f"{name}Prompt"] = current_prompt(name, prompt_path.read_text(
                encoding="utf-8"
            ).strip())
        except (OSError, UnicodeError):
            pass

    direct_statement = False
    try:
        direct_statement = controlled_path(run_dir, "checked-statement.md").read_text(
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

    state["workflowStages"] = saved_workflow_stages(state)
    state["skipStatementReview"] = not state["workflowStages"]["review"]
    state["statementReviewOnly"] = state["workflowStages"]["review"] and not any(
        state["workflowStages"][name] for name in ("author", "critic", "writing"))
    state["criticCountRecorded"] = "criticCount" in persisted_settings
    state["authorSubagentsRecorded"] = "authorSubagents" in persisted_settings
    state["originalContent"] = state.get("originalContent") or state.get("draft", "")
    state["workflow"] = workflow_graph(state["workflowStages"])

    transcript_speed = None
    observed_role_settings = set()
    post_review_request = False
    last_work_node = ""
    author_finished = False
    # Whether the author submitted after the last Resume, which saved the state an
    # interrupted runner leaves in its checkpoint (see the rebuilt checkpoint below).
    submitted_since_resume = False
    # Whether the runner reported a fatal error after its last step began.
    ended_with_error = False
    # The transcript holds every record of the time limit (the job settings keep a copy for saved batches).
    state["timeLimitHistory"] = []
    for record in _iter_run_records(run_dir):
        follow_time_limit(state["timeLimitHistory"], record)
        if controller_note(record):
            continue
        if not any_records and record.get("label") != "Total time limit set":
            # A limit set while the job waited to start does not mean it started.
            first_time, any_records = record.get("time"), True
        last_time = record.get("time")
        kept = restore_record(record)
        if kept is not None:
            relevant.append(kept)
        if convention_verdict(record):
            # The latest verdict's warning (None after a faithful one); an ignored one stays ignored
            # (see same_convention_warning).
            warning = convention_warning(record)
            if warning is not None:
                warning["dismissed"] = same_convention_warning(saved_warning, warning)
            state["conventionWarning"] = warning
        if record.get("kind") == "request" or str(record.get("label", "")).startswith("Workflow step: "):
            ended_with_error = False
        goal_thread = goal_thread_from_record(record)
        if goal_thread:
            state["goalThreadId"] = goal_thread
        delivered = record.get("authorSteerDelivered")
        if (record.get("kind") == "status" and record.get("stage") in {"solve", "repair"}
                and record.get("root") is not False and isinstance(delivered, str) and delivered):
            state["authorSteerDelivered"] = delivered
        if delivered_repair(record):
            state["authorRepairDelivered"] = delivered_repair(record)
        stage = record.get("stage")
        if stage:
            state["stage"] = stage
            state["activeNode"] = event_node(record, state["activeNode"])
            if stage in {"solve", "repair", "critic", "final"}:
                last_work_node = state["activeNode"]
        if record.get("kind") == "request" and stage in {"solve", "repair"}:
            author_finished = False
        if (record.get("kind") == "status" and record.get("node") == "author"
                and record.get("label") in {"Resume requested", "Workflow step: author"}):
            # The author works again, also before its first request (such as while it retries starting).
            author_finished = False
        if record.get("kind") == "author_result":
            author_finished = submitted_since_resume = True
        elif record.get("kind") == "status" and record.get("label") == "Resume requested":
            submitted_since_resume = False
        if record.get("kind") == "failure_result":
            state["resultKind"] = "needs-revision" if record.get("resultKind") == "needs-revision" else "failure"
            state["revisionRequired"] = state["resultKind"] == "needs-revision"
            state["criticBugs"] = record.get("bugs", "")
            state["output"] = record.get("output") or record.get("summary") or record.get("text", "")
        elif (record.get("kind") in {"final_result", "workflow_result"}
                and isinstance(record.get("output"), str) and record["output"].strip()):
            if record.get("kind") != "workflow_result" or state["resultKind"] not in {"needs-revision", "failure"}:
                state["resultKind"] = "proof" if (record.get("resultKind") == "proof"
                    or not state["workflowStages"]["writing"]) else "final"
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
            ended_with_error = True

    if state["problemMode"] == "algorithmic":
        state["problemMode"] = "statement"
        state["skipStatementReview"] = True
        state["workflow"] = DIRECT_GRAPH
    if transcript_speed is not None:
        state["speedMode"] = transcript_speed

    required_roles = {role for stage, role in STAGE_ROLES.items() if state["workflowStages"][stage]}
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
    # Only roles this job still runs are worth a warning: once proof work has
    # started, the statement reviewer never runs again.
    required_roles -= {"review"} if post_review_request else set()
    replaced_roles &= required_roles
    if replaced_roles:
        settings_warnings.insert(0, removed_model_warning(replaced_roles))
        state["replacedRoles"] = sorted(replaced_roles)

    def unconfigured(model):
        try:
            return providers.is_api_id(model) and not providers.is_known(model)
        except providers.StoreUnavailable:
            return False  # Unreadable now is not removed; Resume reads the store again.

    # An API model removed since the job ran: retried every minute until it is added again.
    unconfigured_roles = [role for role in ROLES if role in required_roles and unconfigured(state.get(f"{role}Model"))]
    settings_warnings.extend(unconfigured_job_model_error(state[f"{role}Model"], role, state.get("apiModels"))
                             for role in unconfigured_roles)

    state["startedAt"] = (
        first_time if isinstance(first_time, str) and first_time
        else _artifact_time(run_dir)
    )
    state["lastActivityAt"] = last_time if isinstance(last_time, str) else ""
    state["finishedAt"] = state["lastActivityAt"]
    state["phase"] = "done"
    for filename in ("final.tex", "proof-output.md", "failure-summary.md", "partial-output.md"):
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
                elif filename == "proof-output.md":
                    state["resultKind"] = "proof"
                elif filename == "failure-summary.md":
                    state["resultKind"] = "needs-revision" if state.get("revisionRequired") else "failure"
                break
            except (OSError, UnicodeError):
                pass
    if state["resultKind"] in {"final", "proof"}:
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
    # The latest statement review never finished: it failed (recorded), or a
    # restart interrupted it. A failed one waits for Retry statement review;
    # an interrupted one starts again by itself from review-input.json.
    attempts = [index for index, record in enumerate(relevant)
                if record.get("stage") == "review" and (record.get("kind") == "request"
                                                        or record.get("label") == "Statement review failed")]
    results = [index for index, record in enumerate(relevant) if record.get("kind") == "review_result"]
    if (state["problemMode"] == "statement" and not state["skipStatementReview"] and not post_review_request
            and state["reviewInputRecorded"] and attempts and attempts[-1] > max(results, default=-1)):
        failure = relevant[attempts[-1]] if relevant[attempts[-1]].get("kind") == "status" else None
        state.update(phase="reviewed" if state["review"] and state["reviewFeedback"] else "input",
                     stage="review", activeNode="statement_reviewer", finishedAt="", resultKind="", output="",
                     error=str(failure.get("text") or "The statement review failed.") if failure else
                     "A restart interrupted the statement review; it starts again by itself from its saved input.",
                     reviewRetryAt=0 if failure else time.time())
    if state["problemMode"] != "latex" and state["workflowStages"]["writing"]:
        try:
            saved_final_input(run_dir, records=relevant)
            state["finalInputReady"] = True
        except ValueError:
            pass
    manual_stop = saved_manual_stop(run_dir, relevant)
    if manual_stop is not None:
        stopped_stage = manual_stop["stage"]
        # The LaTeX step it stopped at (the PDF compiles in the same stage as the editor writes).
        latex_node = manual_stop.get("node") if stopped_stage == "final" else None
        state.update(
            phase="done",
            error="Stopped.",
            manuallyStopped=True,
            stoppedStage=stopped_stage,
            stage=stopped_stage,
            activeNode=latex_node if latex_node in {"latex_editor", "latex_compile", "latex_repair"} else {
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
    damaged = {}
    if checkpoint.get("status") in {"paused", "pausing"} and not complete_pause_checkpoint(checkpoint):
        # Resume would refuse this checkpoint: rebuild it from the transcript below,
        # keeping the cause, retry time, reason and elapsed time it still records.
        damaged = {key: checkpoint[key] for key, valid in DAMAGED_CHECKPOINT_FIELDS.items()
                   if key in checkpoint and valid(checkpoint[key])}
        checkpoint = {}
        settings_warnings.append(f"The saved pause checkpoint ({PAUSE_FILENAME}) was damaged and has been "
                                 "rebuilt from the transcript.")
    prior = checkpoint.get("state") if isinstance(checkpoint.get("state"), dict) else {}
    interrupted = (manual_stop is None and state["resultKind"] not in {"final", "proof", "review"}
                   and checkpoint.get("status") not in {"paused", "pausing"})
    rebuilt = None
    if interrupted and last_work_node == "author" and not author_finished:
        # A server/process crash leaves the last resume checkpoint marked running.
        # There is no worker attached to a restored job, and partial text is not a proof.
        statement = prior.get("statement")
        if not statement:
            try:
                statement = saved_statement(run_dir)
            except ValueError:
                statement = state["draft"]
        if statement:
            recovered_state = prior or {"statement": statement}
            if submitted_since_resume:
                # The state Resume saved may say why an earlier submission was refused: the runner
                # dropped that reason when the author submitted again, so the author is not told it again.
                recovered_state = {key: value for key, value in recovered_state.items() if key != "handoff_refused"}
            # The unfinished author was repairing the latest rejection, even when
            # its new goal session logged as 'solve' or audit records followed it.
            rejection = next((record for record in reversed(relevant)
                              if record.get("kind") == "critic_result"), {})
            report = rejection.get("report")
            if isinstance(report, dict) and report.get("verdict") == "reject":
                recovered_state = {**recovered_state, "report": report,
                                   "solution": report.get("solution", ""),
                                   "round": rejection.get("round", state["round"])}
            rebuilt = {"node": "author", "state": recovered_state,
                       "stage": "repair" if recovered_state.get("report") else "solve",
                       "error": "The previous author process ended without a final answer; "
                                "its saved conversation continues automatically."}
    elif (interrupted and not ended_with_error and last_work_node in INTERRUPTIBLE_NODES
          and state["resultKind"] not in {"failure", "needs-revision"}):
        # The critic or LaTeX step continues from its exact saved input in this run folder.
        running = prior if checkpoint.get("status") == "running" and checkpoint.get("node") == "critic" else None
        rebuilt = interrupted_stage_checkpoint(run_dir, last_work_node, state, relevant, running)
    if rebuilt:
        elapsed = saved_seconds(checkpoint.get("elapsedSeconds"), saved_seconds(state.get("elapsedSeconds"), 0.0))
        try:
            segment_start = datetime.fromisoformat(state.get("resumedAt") or state["startedAt"])
            segment_end = datetime.fromisoformat(state["lastActivityAt"])
            elapsed += max(0, (segment_end - segment_start).total_seconds())
        except (TypeError, ValueError):
            pass
        checkpoint = {
            **checkpoint, "status": "paused", **rebuilt,
            "goalThreadId": checkpoint.get("goalThreadId") or state["goalThreadId"],
            "elapsedSeconds": elapsed, "pauseCause": "interrupted", "autoResumeAt": 0, **damaged,
        }
    elif damaged:
        state["error"] = (f"The saved pause checkpoint ({PAUSE_FILENAME}) is damaged and could not be rebuilt "
                          "from the transcript. Continue from a saved checkpoint.")
    if (checkpoint.get("status") in {"paused", "pausing"}
            and state["resultKind"] not in {"final", "proof", "review"} and manual_stop is None):
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
    elif state["resultKind"] == "needs-revision":
        state["error"] = state["error"] or "Proof needs revision. See the critic feedback."
    if state.get("retrievalMode") == "historical" and not state.get("retrievalPolicyPath"):
        state["launchHold"] = "Historical search policy is missing; restore the controller policy before launch."
    if state["preparedRun"] and not any_records and not state["output"]:
        # A queued folder-batch job starts with its statement review when its stages include it.
        review = state["reviewInputRecorded"] and not state["skipStatementReview"] and state["workflowStages"]["review"]
        state.update(phase="prepared", stage="review" if review else "solve",
                     activeNode="statement_reviewer" if review else "author", startedAt="",
                     resumedAt="", elapsedSeconds=0, finishedAt="", error="")
        if state.get("launchQueued"):
            # A queued job starts when a running slot is free, as before the restart.
            state["error"] = state.get("launchError") if isinstance(state.get("launchError"), str) else ""
    state["checkpoints"] = saved_run_checkpoints(run_dir)
    app._checkpoint_cache = list(state["checkpoints"])
    app._checkpoint_cache_signature = checkpoint_artifact_signature(run_dir)
    app.state = state
    if state["phase"] == "paused" and owned:
        pause_path = controlled_path(run_dir, PAUSE_FILENAME)
        if checkpoint.get("pauseCause") in {"interrupted", "restart"}:
            # A runner that ended with its server continues about a minute after this start.
            checkpoint["autoResumeAt"] = max(saved_seconds(checkpoint.get("autoResumeAt"), 0.0),
                                             time.time() + AUTO_RESUME_SECONDS)
        try:
            changed = pause_path.stat().st_mtime
        except OSError:
            changed = time.time()
        app._configure_auto_resume(checkpoint, changed)
        # Before the checkpoint is saved: it records that a time-limit pause asked for its summary.
        app._restore_conversation(checkpoint)
        # Only a role the remaining steps use is worth a check before the job retries by itself; a
        # replaced model resumes as its documented replacement, with the warning shown.
        roles = set(checkpoint_roles(checkpoint.get("node"), state["workflowStages"]))
        unconfigured_roles = [role for role in unconfigured_roles if role in roles]
        if unconfigured_roles and checkpoint.get("autoResumeAt"):
            settings_warnings.append(f"The job checks again every {retry_interval_text()} and continues by itself "
                                     "once the model is configured.")
        try:
            app._save_pause(checkpoint)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            # The job stays listed and paused; the next resume saves the checkpoint first.
            app._unsaved_checkpoint = checkpoint
            settings_warnings.append(f"The pause checkpoint could not be saved ({exc}); it is saved again "
                                     "before the next resume.")
    else:
        app._restore_conversation(checkpoint, owned=owned)
    question = state.get("authorQuestion")
    if question and (state["phase"] == "paused" and state["activeNode"] == "author"
                     and checkpoint.get("pauseCause") in RESTART_PAUSE_CAUSES):
        # The author continues its conversation after this restart, and its question shows again then;
        # the turn in which it asked ended with the previous server.
        if question["turnEndedAt"] is None:
            question["turnEndedAt"] = time.time()
    else:
        # Answered, dismissed, replaced or expired questions were saved as closed; any other open one
        # closed when its author stopped working.
        state["authorQuestion"] = None
    state["settingsWarning"] = " ".join(settings_warnings)
    if owned and state.get("retrievalPolicyPath") and (
            (state["phase"] == "prepared" and state.get("launchHold") == HISTORICAL_PENDING_HOLD)
            or str(state.get("launchHold", "")).endswith(HISTORICAL_RETRY_SUFFIX)):
        # A check interrupted by the restart, or one that could not finish:
        # run it again now instead of holding the job forever.
        app._hold_retry_at = time.time()
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
        app = restore_one_run(transcript, runs)
        jobs[app.state["runId"]] = app
    return jobs


def restore_one_run(transcript, runs):
    """Restore one saved run: owned here, shown as running elsewhere, or shown as damaged.

    A run that another process still owns (a live terminal run, or a runner a
    crashed server left behind) is restored without changing anything in it;
    Server._adopt_released_runs takes it over once it is free.
    """

    app = None
    try:
        app = App(transcript, runs)
        try:
            app._claims.claim(app.run_dir)
        except ownership.OwnedElsewhere as exc:
            app = restore_saved_app(app, owned=False)
            app.state.update(phase="running", runningElsewhere=str(exc), autoResumeAt=0, error="", finishedAt="")
            return app
        restore_saved_app(app)
        app._settle_run_claim()
        return app
    except (OSError, UnicodeError, TypeError, AttributeError, KeyError,
            ValueError, RecursionError, MemoryError) as exc:
        # One damaged run folder must not keep the Web UI from starting, nor disappear from it.
        if app is not None:
            app._claims.release_all()
        print(f"Restored damaged saved run {transcript.parent.name} with a warning: {exc}",
              file=sys.stderr, flush=True)
        return damaged_saved_app(transcript.parent, runs, exc)


def damaged_saved_app(run_dir, runs, problem):
    """A visible job for a run that cannot be restored; its files stay as they are."""

    app = App(runs=runs, load_latest=False)
    app.run_dir, app.trace_file, app.fixed_trace = Path(run_dir), Path(run_dir) / "transcript.jsonl", False
    detail = str(problem) or type(problem).__name__
    app.state.update(runId=app.run_dir.name, phase="done", startedAt=_artifact_time(app.run_dir),
                     error=f"This saved run could not be restored ({detail}). Its files in "
                           f"{app.run_dir.name} are unchanged.")
    try:
        draft = (app.run_dir / "draft.md").read_text(encoding="utf-8").strip()
        app.state["draft"] = draft.removeprefix("# Draft problem").lstrip()
    except (OSError, UnicodeError):
        pass
    try:
        app.state["checkpoints"] = saved_run_checkpoints(app.run_dir)
        app._checkpoint_cache = list(app.state["checkpoints"])
        app._checkpoint_cache_signature = checkpoint_artifact_signature(app.run_dir)
    except (OSError, UnicodeError, TypeError, AttributeError, KeyError, ValueError, RecursionError, MemoryError):
        pass
    return app


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
    checked_path = controlled_path(run_dir, "checked-statement.md")
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
    records = _iter_run_records(run_dir) if records is None else records
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
    for name in ("author", "critic", "convention", "final"):
        prompt_path = controlled_path(run_dir, f"prompts/{name}.txt")
        prompts[name] = current_prompt(name, (
            read_utf8(prompt_path, f"saved {name} prompt")
            if prompt_path.exists() else defaults[name]
        ))
    return {
        "run_dir": run_dir,
        "statement": statement,
        "solution": solution,
        "author_prompt": prompts["author"],
        "critic_prompt": prompts["critic"],
        "convention_prompt": prompts["convention"],
        "final_prompt": prompts["final"],
    }


def saved_research_source(path):
    """Read checkpoint launch settings without opening a historical transcript."""

    requested = Path(path).expanduser().absolute()
    source = requested if requested.is_dir() else requested.parent
    source = validated_continuation_source(source, source.parent)
    settings = {}
    settings_path = controlled_path(source, JOB_SETTINGS_FILENAME)
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
    for role in ("author", "critic", "convention", "final"):
        path = controlled_path(source, f"prompts/{role}.txt")
        settings[f"{role}Prompt"] = current_prompt(role, (
            read_utf8(path, f"saved {role} prompt") if path.exists()
            else defaults[role]
        ))
    return {"run_dir": source, "statement": saved_statement(source), "settings": settings,
            "replacedRoles": replaced}


class Server(ThreadingHTTPServer):
    """Keep independent jobs alive while browser tabs come and go."""

    # On Windows SO_REUSEADDR lets a second server bind a port that is already
    # listening, so two servers would own the same jobs. Without it a second
    # launch fails with "address in use"; POSIX keeps quick restarts.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, runs=RUNS, *, single_instance=False):
        """``single_instance`` is the launcher's server: the one owner of its runs
        folder (AlreadyRunning names the server that already is), on another port
        when the usual one is taken, with a session token that survives restarts.
        """

        self.runs = Path(runs)
        from .batches import BatchCatalog
        # The catalog refuses a symlinked root itself; a linked runs folder
        # (for example on another disk) still works for jobs and restores.
        root = self.runs.resolve()
        self.batch_catalog = BatchCatalog(root, current_root=root)
        self.jobs, self.jobs_lock = {}, threading.RLock()
        self._retry_stop = threading.Event()
        self._sleep = SleepWatch()
        self.installation = load_installation_settings()
        # Folder-batch statements whose job could not be created yet, retried every minute.
        self._pending_jobs = []
        self.instance = None
        if single_instance:
            prepare_runs_directory(self.runs)
            self.instance = ownership.Instance(self.runs)
            if not self.instance.acquire():
                holder = self.instance.holder()
                port = holder.get("port")
                url = (f"http://{HOST}:{port}/#{ownership.session_token(self.runs)}"
                       if isinstance(port, int) and not isinstance(port, bool) else "")
                raise ownership.AlreadyRunning(holder, url)
        try:
            try:
                super().__init__(address, Handler)
            except OSError as exc:
                if not (single_instance and exc.errno == errno.EADDRINUSE and address[1]):
                    raise
                # Another program holds the usual port; this folder's own server is not running.
                super().__init__((address[0], 0), Handler)
            # Key-bearing Claude settings left by runners that were killed.
            claude_cli.sweep_stale_settings()
            # Claude requests and API models need the private state folder: say at once what to fix.
            self.state_problem = providers.check_private_state()
            if self.state_problem:
                print(f"Warning: {self.state_problem} Claude models and API models cannot start until it is fixed.",
                      file=sys.stderr, flush=True)
            self.jobs.update(restore_saved_jobs(self.runs))
            prune_trash(self.runs)
            # Related-work uploads wait here until a job takes them (pruned after a day).
            self.upload_stage = staged_uploads(self.runs)
            self._prune_uploads()
        except BaseException:
            if self.instance is not None:
                self.instance.release()
            raise
        # Only the unguessable launch URL can create this server's session. The
        # launcher's server keeps it, so tabs left open still work after a restart.
        self.token = ownership.session_token(self.runs) if single_instance else secrets.token_urlsafe(32)
        self.origin = f"http://{HOST}:{self.server_port}"
        if self.instance is not None:
            self.instance.publish(port=self.server_port)

    def _uploads(self):
        """The staging area of related-work uploads (runs/.uploads)."""

        stage = getattr(self, "upload_stage", None)
        if stage is None:
            stage = self.upload_stage = staged_uploads(self.runs)
        return stage

    def _prune_uploads(self, now=None):
        """Remove staged uploads older than a day: when the server starts, then at most once an hour."""

        self._uploads_pruned_at = time.monotonic()
        try:
            self._uploads().prune(now)
        except OSError:
            pass  # Tried again within the hour.
        # A crash during a batch link or copy leaves its temporary file beside the target.
        controller = Path(self.runs) / CONTROLLER_FOLDER
        try:
            folders = [*controller.iterdir(), *(controller / related_works.BATCH_FOLDER).iterdir()] \
                if (controller / related_works.BATCH_FOLDER).is_dir() else list(controller.iterdir())
        except OSError:
            folders = []
        for folder in folders:
            if folder.is_dir() and not folder.is_symlink():
                related_works.UploadStage.prune_leftovers(folder, now)

    def running_jobs(self):
        """How many jobs count against the running-jobs limit now."""

        with self.jobs_lock:
            apps = list(self.jobs.values())
        return sum(app.occupies_slot() for app in apps)

    def free_slots(self):
        """How many more jobs the server may start by itself now (inf without a limit)."""

        limit = getattr(self, "installation", {}).get("maxRunningJobs", 0)
        return math.inf if not limit else max(0, limit - self.running_jobs())

    def _retry_paused_jobs(self, now=None):
        """Start what is due: automatic retries of error pauses, queued jobs, statement
        reviews a restart interrupted, and the checks of held historical jobs.

        Jobs that waited longest go first, within the running-jobs limit and at
        most one start every AUTO_LAUNCH_SPACING_SECONDS, so jobs that came due
        together reach the provider spread out.
        """

        now = time.time() if now is None else now
        self._create_pending_jobs(now)
        if time.monotonic() - getattr(self, "_uploads_pruned_at", -math.inf) >= UPLOAD_PRUNE_SECONDS:
            self._prune_uploads()
        with self.jobs_lock:
            # Replies of paused authors first: they are exempt from the launch spacing and the
            # running-jobs limit, but occupy a slot while they run.
            for run_id, app in list(self.jobs.items()):
                if self._retry_stop.is_set():
                    return
                try:
                    app.start_conversation_if_due(
                        now, check=lambda app=app: self._check_author_workspace_idle(app.run_dir, exclude=app))
                except Exception as exc:
                    # Reported on that job, which tries again after the usual wait.
                    app.defer_conversation(exc, now)
            free = self.free_slots()
            ready = now - getattr(self, "_last_auto_launch", -math.inf) >= AUTO_LAUNCH_SPACING_SECONDS
            for run_id, app in sorted(self.jobs.items(), key=lambda item: item[1].launch_due_at(now)):
                if self._retry_stop.is_set():
                    return
                if app.launch_due_at(now)[0] > now:
                    break
                if not ready or free <= 0:
                    continue  # Still due at the next tick.
                checking = False
                try:
                    if app.state["phase"] == "paused":
                        app.auto_resume_if_due(lambda: self.resume_paused_job(run_id), now)
                    elif app.state["phase"] == "prepared":
                        app.start_queued_if_due(now)
                    else:
                        app.retry_review_if_due(now)
                    # A passing check starts or resumes its job (see rerun_historical_preflight).
                    checking = app.retry_historical_hold_if_due(now)
                except Exception as exc:
                    # Nothing one job raises may stop the other jobs' retries:
                    # report it on that job, which tries again after the usual wait.
                    app.defer_auto_resume(exc, now)
                if checking or app.occupies_slot():
                    free, ready, self._last_auto_launch = free - 1, False, now

    def _create_pending_jobs(self, now):
        """Create the folder-batch jobs whose creation failed (for example on a full disk), every minute."""

        for item in list(getattr(self, "_pending_jobs", [])):
            if now < item["retryAt"]:
                continue
            app = App(runs=self.runs, load_latest=False)
            batch = item.get("folderBatch") or None
            try:
                if "body" in item:
                    self.start_workflow(item["body"], app=app, _prepare_only=True, _folder_batch=batch)
                else:
                    app.start_direct_statement(item["statement"], source_file=item["name"], _prepare_only=True,
                                               folder_batch=batch, **item["options"])
            except Exception as exc:
                app.discard_unstarted_run()
                item.update(retryAt=now + AUTO_RESUME_SECONDS, error=str(exc))
                continue
            self._pending_jobs.remove(item)
            with self.jobs_lock:
                self.jobs[app.state["runId"]] = app

    def installation_settings(self):
        """The running-jobs limit, with how many jobs run and wait in the queue now."""

        with self.jobs_lock:
            apps = list(self.jobs.values())
        return {**getattr(self, "installation", load_installation_settings()),
                "runningJobs": sum(app.occupies_slot() for app in apps),
                "queuedJobs": sum(app.state.get("phase") == "prepared" and bool(app.state.get("launchQueued"))
                                  and not app.state.get("launchHold") for app in apps)}

    def save_installation_settings(self, body):
        self.installation = save_installation_settings(body)
        return self.installation_settings()

    def _tick_jobs(self):
        """Keep machine sleep out of working jobs' active time; save transcript lines a failed write kept;
        close the author questions whose author no longer waits for them."""

        slept = self._sleep.slept()
        with self.jobs_lock:
            apps = list(self.jobs.values())
        for app in apps:
            if slept:
                app.exclude_sleep(slept)
            app.flush_trace()
            app.close_stale_author_question()

    def _adopt_released_runs(self):
        """Take over each job another process owned, once that process has let it go."""

        with self.jobs_lock:
            waiting = [(run_id, app) for run_id, app in self.jobs.items() if app.state.get("runningElsewhere")]
        for run_id, app in waiting:
            if self._retry_stop.is_set():
                return
            holder = ownership.held_elsewhere(app.run_dir)
            if holder is not None:
                with app.lock:
                    app.state["runningElsewhere"] = ownership.describe(holder)
                continue
            adopted = restore_one_run(app.trace_file, self.runs)
            with self.jobs_lock:
                if self.jobs.get(run_id) is app:
                    self.jobs[run_id] = adopted
                    continue
            adopted._claims.release_all()

    def serve_forever(self, poll_interval=0.5):
        # No browser tab or polling request is needed to retry a paused job.
        def retry_loop():
            while not self._retry_stop.wait(1):
                try:
                    self._tick_jobs()
                    self._adopt_released_runs()
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
        # The owner locks end with this server; runners still alive keep their own copy.
        with self.jobs_lock:
            apps = list(self.jobs.values())
        for app in apps:
            app._claims.release_all()
        if getattr(self, "instance", None) is not None:
            self.instance.release()

    def get_job(self, run_id):
        """Return one exact job."""

        with self.jobs_lock:
            app = self.jobs.get(run_id)
        if not app:
            raise ValueError("That job is not available in this TCS Prover session.")
        return app

    def start_job(self, body, run_id=""):
        """Create a new job, or revise the selected existing job."""

        if body.get("relatedWorks"):
            raise ValueError("Related works are attached with Start workflow.")
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
            critic_count=body.get("criticCount", DEFAULT_CRITIC_COUNT),
            author_subagents=body.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS),
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
            **web_prompt_options(body, ("review", "author", "critic", "convention", "final"),
                                 app.state if run_id else None),
            speed_mode=body.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=body.get(
                "reasoningSummary", DEFAULT_REASONING_SUMMARY
            ),
            **review_only_option,
            retrieval_mode=body.get("retrievalMode", app.state.get("retrievalMode", "live") if run_id else "live"),
            retrieval_cutoff_utc=body.get("retrievalCutoffUtc", app.state.get("retrievalCutoffUtc", "") if run_id else ""),
            # A named job is revised in its own folder, never re-registered.
            revise_existing=bool(run_id),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def start_workflow(self, body, run_id="", *, app=None, _prepare_only=False, _folder_batch=None):
        """Run the selected ordered stages on one input, preserving review approval.

        A folder batch passes its new job's ``app``, ``_prepare_only`` and its
        ``_folder_batch``: the job then waits in the queue the server starts
        (see start_direct_batch). ``relatedWorks`` (upload tokens) are checked
        before anything is created, installed in the new run before anything
        starts, and discarded once the job is registered.
        """

        created = app is None and not run_id
        app = app or (self.get_job(run_id) if run_id else App(runs=self.runs, load_latest=False))
        stages = normalize_workflow_stages(body.get("workflowStages", app.state.get("workflowStages") if run_id else None))
        content = body.get("content", body.get("statement", ""))
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Enter the statement or existing work to process.")
        if "\0" in content:
            raise ValueError("Workflow input cannot contain NUL characters.")
        tokens = []
        if "relatedWorks" in body:
            tokens = related_works_tokens(body["relatedWorks"])
            if run_id:
                raise ValueError("Add related works on the job page.")
            if not stages["author"]:
                raise ValueError("Related works are read by the proof author; turn on the Author stage or remove "
                                 "them.")
            self._uploads().peek(tokens)
        effective_body = {**body, "workflowStages": stages}
        if run_id:
            defaults = {key: app.state[key] for key in (
                "authorModel", "criticModel", "writerModel", "reasoningEffort", "authorEffort", "criticEffort", "writerEffort",
                "criticRounds", "criticCount", "authorSubagents", "thinkingHours", "speedMode", "reasoningSummary", "retrievalMode",
                "retrievalCutoffUtc")}
            effective_body = {**defaults, **effective_body}
        options = self._direct_options(effective_body)
        options.update(web_prompt_options(effective_body, ("author", "critic", "convention", "final"), app.state if run_id else None))
        options["workflow_stages"] = stages
        options["original_content"] = app.state.get("originalContent") or content if run_id else content
        options["source_file"] = body.get("sourceFile", app.state.get("sourceFile") if run_id else None)
        if tokens:
            options["related_works"] = tokens
        if _folder_batch:
            options["folder_batch"] = _folder_batch
        try:
            if stages["review"]:
                legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
                review_prompts = web_prompt_options(effective_body, ("review",), app.state if run_id else None)
                app.start_review(content, feedback=body.get("feedback", ""),
                                 review_model=body.get("reviewModel", app.state["reviewModel"] if run_id else DEFAULT_REVIEW_MODEL),
                                 review_effort=body.get("reviewEffort", legacy_effort),
                                 **review_prompts, **options, revise_existing=bool(run_id), _prepare_only=_prepare_only)
            else:
                if run_id:
                    raise ValueError("Start a workflow without statement review from the home screen.")
                app.start_direct_statement(content, **options, _prepare_only=_prepare_only)
        except BaseException:
            if created and tokens and app.process is None and not app.has_active_worker():
                # Nothing ran: remove the new run folder; the uploads stay valid for another try.
                app.discard_unstarted_run()
            raise
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        for token in tokens:
            self._uploads().discard(token)
        return app


    @staticmethod
    def _direct_options(body):
        """Translate one /direct (or folder batch) body into new-job author settings."""

        legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
        return {
            "retrieval_mode": body.get("retrievalMode", "live"),
            "retrieval_cutoff_utc": body.get("retrievalCutoffUtc", ""),
            "critic_rounds": body.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
            "critic_count": body.get("criticCount", DEFAULT_CRITIC_COUNT),
            "author_subagents": body.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS),
            "thinking_hours": body.get("thinkingHours", DEFAULT_THINKING_HOURS),
            "author_model": body.get("authorModel", DEFAULT_AUTHOR_MODEL),
            "critic_model": body.get("criticModel", DEFAULT_CRITIC_MODEL),
            "writer_model": body.get("writerModel", DEFAULT_WRITER_MODEL),
            "reasoning_effort": legacy_effort,
            "author_effort": body.get("authorEffort", legacy_effort),
            "critic_effort": body.get("criticEffort", legacy_effort),
            "writer_effort": body.get("writerEffort", legacy_effort),
            **web_prompt_options(body, ("author", "critic", "convention", "final")),
            "speed_mode": body.get("speedMode", DEFAULT_SPEED),
            "reasoning_summary": body.get("reasoningSummary", DEFAULT_REASONING_SUMMARY),
            **({"workflow_stages": normalize_workflow_stages(body["workflowStages"])} if "workflowStages" in body else {}),
        }

    def start_direct_job(self, body, run_id=""):
        """Create a statement job that starts directly with the author."""

        if run_id:
            raise ValueError("Start a direct proof from the home screen.")
        if body.get("relatedWorks"):
            raise ValueError("Related works are attached with Start workflow.")
        app = App(runs=self.runs, load_latest=False)
        app.start_direct_statement(
            statement=body.get("statement", ""), **self._direct_options(body),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def start_direct_batch(self, body, run_id=""):
        """Start one proof job per statement, all sharing one set of settings.

        Every statement and the shared settings are validated before any job
        starts. Each job is an ordinary /direct job (statement review skipped,
        unless the selected stages include it) in its own run folder. The jobs
        start at once up to the running-jobs limit and wait in a queue after
        that. A job that cannot be created or started never stops the others:
        it is tried again every minute, and the returned BatchStart says so.
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
        # The batch's related works go to its own store, linked into every job it creates.
        tokens = related_works_tokens(body["relatedWorks"]) if "relatedWorks" in body else []
        options = self._direct_options(
            {key: value for key, value in body.items() if key not in {"statements", "relatedWorks"}}
        )
        # The jobs share these settings: reject them once, before any job starts.
        validation_options = dict(options)
        if "workflowStages" in body:
            validation_options.update(review_model=body.get("reviewModel", DEFAULT_REVIEW_MODEL),
                                      review_effort=body.get("reviewEffort", DEFAULT_REVIEW_EFFORT),
                                      **web_prompt_options(body, ("review",)))
        normalized = App._workflow_options(include_review=False, **validation_options)
        options.update(retrieval_mode=normalized["retrievalMode"], retrieval_cutoff_utc=normalized["retrievalCutoffUtc"])
        historical = normalized["retrievalMode"] == "historical"
        if historical and len(statements) > 10:
            raise ValueError("Historical search supports at most 10 statements per launch so every sandbox can be checked before work starts.")
        review = "workflowStages" in body and normalized["workflowStages"]["review"]
        if review and historical:
            raise ValueError("Historical search requires the statement review stage to be disabled.")
        if tokens and not normalized["workflowStages"]["author"]:
            raise ValueError("Related works are read by the proof author; turn on the Author stage or remove them.")
        if tokens:
            self._uploads().peek(tokens)
        shared = {key: value for key, value in body.items() if key not in {"statements", "relatedWorks"}}
        # ``current`` indexes the statement being handled; None means a step
        # shared by the whole historical batch.
        started, current, notes = BatchStart(), 0, []
        # Every folder batch has a related-works store, so related works can be added to it later.
        batch_id = related_works.new_batch_id()
        store = folder_batch_store(self.runs, batch_id)
        if tokens:
            try:
                store.install(tokens, self._uploads(), keep_staged=True)
            except BaseException:
                shutil.rmtree(store.root, ignore_errors=True)
                raise
        started.folder_batch = batch_id

        def label(index):
            return statements[index][0] or f"statement {index + 1}"

        def settle_uploads():
            # The store keeps its own copy once a job uses it; otherwise the uploads stay valid.
            if started or any(item.get("folderBatch") == batch_id for item in getattr(self, "_pending_jobs", [])):
                for token in tokens:
                    self._uploads().discard(token)
            elif tokens:
                shutil.rmtree(store.root, ignore_errors=True)

        try:
            for current, (name, statement) in enumerate(statements):
                app = App(runs=self.runs, load_latest=False)
                pending = ({"body": {**shared, "content": statement, "sourceFile": name}} if review
                           else {"options": dict(options)})
                try:
                    if review:
                        self.start_workflow(pending["body"], app=app, _prepare_only=True, _folder_batch=batch_id)
                    else:
                        app.start_direct_statement(statement, source_file=name, _prepare_only=True,
                                                   folder_batch=batch_id, **options)
                except Exception as exc:
                    if historical:
                        raise
                    # Nothing started: drop the half-made folder, and create it again every minute.
                    app.discard_unstarted_run()
                    self._pending_jobs = getattr(self, "_pending_jobs", [])
                    self._pending_jobs.append({"name": name, "statement": statement, **pending,
                                               "folderBatch": batch_id,
                                               "retryAt": time.time() + AUTO_RESUME_SECONDS, "error": str(exc)})
                    notes.append(f"Could not create the job for {label(current)} "
                                 f"({str(exc).rstrip('.') or type(exc).__name__}); TCS Prover tries again every "
                                 f"{retry_interval_text()} while it runs.")
                    continue
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
                if normalized["workflowStages"]["writing"]:
                    retrieval.preflight_compiler(started[0].state["retrievalPolicyPath"])
                for app in started:
                    with app.lock:
                        # Every sandbox passed: the jobs join the queue.
                        app.state.update(launchHold="", launchQueued=True, queuedAt=time.time())
                        app._save_job_settings(app.state)
        except Exception as exc:
            # Only a historical batch gets here: no model has started, and the
            # prepared jobs stay held.
            held = 0
            retrying = isinstance(exc, retrieval.PreflightRetry)
            for app in started:
                try:
                    # Each held job names the cause and starts once its own checks
                    # pass: automatically for a check that could not finish.
                    cause = (retrieval.PreflightRetry if retrying else retrieval.NeedsAttention
                             if isinstance(exc, retrieval.NeedsAttention) else ValueError)
                    with app.lock:
                        app._hold_after_failed_check(cause(
                            f"Historical batch launch failed: {str(exc).rstrip('.') or type(exc).__name__}."))
                    held += 1
                except (OSError, ValueError):
                    pass
            target = "the historical batch" if current is None else f"the job for {label(current)}"
            detail = str(exc).rstrip(".") or type(exc).__name__
            message = f"Could not start {target}: {detail}."
            if held:
                message += (f" The {held} prepared job{'s are' if held != 1 else ' is'} held"
                            + ("; each starts once its checks pass, which run again every minute."
                               if retrying else "; choose Re-run preflight on each after fixing the cause."))
            raise ValueError(message) from exc
        finally:
            settle_uploads()
        # Start what the running-jobs limit allows; the others wait in the queue.
        free, queued = self.free_slots(), 0
        for app in started:
            if free <= 0:
                queued += 1
                continue
            app.start_queued_if_due()
            if app.occupies_slot():
                free -= 1
            elif app.state.get("launchHold"):
                notes.append(f"The job for {app.state.get('sourceFile') or app.state['runId']} is held: "
                             f"{app.state['launchHold']}")
            elif app.state.get("launchError"):
                notes.append(f"Could not start the job for {app.state.get('sourceFile') or app.state['runId']} "
                             f"({app.state['launchError'].removeprefix('Could not start: ').rstrip('.')}); it is "
                             f"tried again every {retry_interval_text()}.")
        if queued:
            limit = getattr(self, "installation", {}).get("maxRunningJobs", 0)
            notes.insert(0, f"{queued} job{'s wait' if queued != 1 else ' waits'} in the queue: at most {limit} "
                            f"job{'s run' if limit != 1 else ' runs'} at once, and the next one starts when one "
                            "finishes or pauses.")
        started.notice = " ".join(notes)
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

    def start_saved_critic_job(self, source_run, settings=None):
        """Create a browser-visible job from one saved complete proof."""

        settings = {**saved_research_source(source_run)["settings"], **(settings or {})}
        stages = {**saved_workflow_stages(settings), "review": False}
        if not stages["critic"]:
            raise ValueError("The critic stage is disabled for this saved workflow.")
        source = saved_critic_source(source_run)
        with self.jobs_lock:
            self._check_author_workspace_idle(source["run_dir"])
            historical = self._historical_source_job(source["run_dir"])
        if historical is not None:
            # A new run cannot take over its policy; its saved settings stay.
            historical.continue_historical("critic")
            return historical
        app = App(runs=self.runs, load_latest=False)
        app.start_critic_resume(
            statement=source["statement"],
            solution=source["solution"],
            source_run=source["run_dir"],
            critic_rounds=settings.get(
                "criticRounds", DEFAULT_CRITIC_ROUNDS
            ),
            critic_count=settings.get("criticCount", DEFAULT_CRITIC_COUNT),
            author_subagents=settings.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS),
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
            convention_prompt=settings.get("conventionPrompt", source["convention_prompt"]),
            final_prompt=settings.get("finalPrompt", source["final_prompt"]),
            speed_mode=settings.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=settings.get(
                "reasoningSummary", DEFAULT_REASONING_SUMMARY
            ),
            workflow_stages=stages, original_content=settings.get("originalContent"),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        return app

    def start_saved_research_job(self, source_run, settings=None):
        """Continue saved author work with its original prompt and a fresh budget."""

        source = saved_research_source(source_run)
        with self.jobs_lock:
            self._check_author_workspace_idle(source["run_dir"])
            historical = self._historical_source_job(source["run_dir"])
        if historical is not None:
            historical.continue_historical("author")
            return historical
        options = {**source["settings"], **(settings or {})}
        app = App(runs=self.runs, load_latest=False)
        app.start_direct_statement(
            source["statement"],
            critic_rounds=options.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
            critic_count=options.get("criticCount", DEFAULT_CRITIC_COUNT),
            author_subagents=options.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS),
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
            convention_prompt=options.get("conventionPrompt"),
            final_prompt=options.get("finalPrompt"),
            speed_mode=options.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=options.get("reasoningSummary", DEFAULT_REASONING_SUMMARY),
            continuation_source=source["run_dir"], stopped_stage="solve",
            allow_external_source=True,
            workflow_stages={**saved_workflow_stages(options), "review": False},
            original_content=options.get("originalContent"),
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

    # --- Related works of a folder batch -------------------------------------------------------------------------

    def _folder_batch_id(self, batch_id="", run_id=""):
        """The folder batch a batch route names: by its ID, or through one of its jobs."""

        if batch_id and batch_id != "__root__":
            if not FOLDER_BATCH_ID.match(batch_id):
                raise ValueError("That folder batch is not available in this TCS Prover session.")
            return batch_id
        if run_id:
            app = self.get_job(run_id)
            with app.lock:
                batch = app.state.get("folderBatch")
            if not batch:
                raise ValueError("This job was not started from a folder, so it has no batch related works.")
            return batch
        raise ValueError("Name the folder batch.")

    def _folder_batch_members(self, batch_id):
        """(the batch's jobs in this session, whether some of its jobs wait to be created)."""

        with self.jobs_lock:
            jobs = [app for app in self.jobs.values() if app.state.get("folderBatch") == batch_id]
        waiting = any(item.get("folderBatch") == batch_id for item in getattr(self, "_pending_jobs", []))
        return jobs, waiting

    @staticmethod
    def _folder_batch_problem(jobs, waiting):
        """Why nothing can be added to a folder batch now, or "" (some job's author can still read it)."""

        problems = [app.related_works_problem() for app in jobs]
        if waiting or any(not problem for problem in problems):
            return ""
        if not jobs:
            return "That folder batch is not available in this TCS Prover session."
        elsewhere = next((app.state["runningElsewhere"] for app in jobs if app.state.get("runningElsewhere")), "")
        if elsewhere:
            return elsewhere
        if not any(saved_workflow_stages(app.state)["author"] for app in jobs):
            return "Related works are read by the proof author; this batch's jobs have no Author stage."
        return "Every job of this batch has finished; continue one to add related works."

    def batch_related_works(self, batch_id="", run_id=""):
        """The related works a folder batch shares: {id, available, reason, count, items, runIds}."""

        batch_id = self._folder_batch_id(batch_id, run_id)
        jobs, waiting = self._folder_batch_members(batch_id)
        if not jobs and not waiting:
            raise ValueError("That folder batch is not available in this TCS Prover session.")
        problem = self._folder_batch_problem(jobs, waiting)
        items = folder_batch_store(self.runs, batch_id).listing()
        return {"id": batch_id, "available": not problem, "reason": problem, "count": len(items), "items": items,
                "runIds": sorted(app.state["runId"] for app in jobs)}

    def add_batch_related_works(self, tokens, batch_id="", run_id=""):
        """Add uploads to a folder batch's store and link them into every job of the batch.

        Each workspace is linked once (continuations share their source's), so
        each job's author is told once, by the usual notice rule. A job whose
        folder cannot take them is reported; the others still get them. Jobs of
        the batch created later link everything the store holds.
        """

        tokens = related_works_tokens(tokens)
        batch_id = self._folder_batch_id(batch_id, run_id)
        jobs, waiting = self._folder_batch_members(batch_id)
        problem = self._folder_batch_problem(jobs, waiting)
        if problem:
            raise ValueError(problem)
        store = folder_batch_store(self.runs, batch_id)
        names = [entry["name"] for entry in store.install(tokens, self._uploads())]
        results, notes, groups = [], [], {}
        for app in jobs:
            with app.lock:
                if app.state.get("runningElsewhere"):
                    skipped = app.state["runningElsewhere"]
                elif app.run_dir is None:
                    skipped = "This job has no run folder."
                elif not saved_workflow_stages(app.state)["author"]:
                    skipped = "Related works are read by the proof author; this job has no Author stage."
                else:
                    skipped = ""
                workspace = "" if skipped else str(app._author_workspace())
            if skipped:
                results.append({"runId": app.state["runId"], "added": [], "notice": "", "problem": skipped})
                # The page shows only the notice: a job that did not get them is named there.
                notes.append(f"{app.state.get('sourceFile') or app.state['runId']}: {skipped.rstrip('.')}")
                continue
            groups.setdefault(workspace, []).append(app)
        for workspace, apps in groups.items():
            # The job whose run folder is the workspace is recorded as adding them.
            owner = next((app for app in apps if app.run_dir.resolve() == Path(workspace)), apps[0])
            try:
                # Everything the store holds that this workspace lacks (link_into never links an item
                # twice), so a job that missed an earlier addition gets it now too.
                outcome = owner.link_batch_related_works(store, None, owner.run_dir.name)
            except (OSError, ValueError) as exc:
                for app in apps:
                    results.append({"runId": app.state["runId"], "added": [], "notice": "", "problem": str(exc)})
                    try:
                        app.add_trace({"kind": "diagnostic", "stage": RELATED_WORKS_STAGE, "folderBatch": batch_id,
                                       "text": f"The folder batch's new related works could not be linked into this "
                                               f"job's related-works folder: {exc}"})
                    except OSError:
                        pass
                notes.append(f"{', '.join(app.state.get('sourceFile') or app.state['runId'] for app in apps)}: "
                             f"{str(exc).rstrip('.')}")
                continue
            added = [entry["name"] for entry in outcome["entries"]]
            for app in apps:
                try:
                    notice = app.batch_related_works_linked(batch_id, outcome["entries"], outcome["diagnostic"])
                except (OSError, ValueError) as exc:
                    # The files are in its folder; only its record or notice failed. The others go on.
                    results.append({"runId": app.state["runId"], "added": added, "notice": "", "problem": str(exc)})
                    notes.append(f"{app.state.get('sourceFile') or app.state['runId']}: the related works are in its "
                                 f"folder, but its author could not be told ({str(exc).rstrip('.')})")
                    continue
                results.append({"runId": app.state["runId"], "added": added, "notice": notice, "problem": ""})
        listing = self.batch_related_works(batch_id)
        return {**listing, "added": names, "results": results,
                "notice": (f"Could not add them to every job: {'; '.join(notes)}." if notes else "")}

    def _historical_source_job(self, source_run):
        """The registered job of a historical source run, which continues in place."""
        source_run = Path(source_run).resolve()
        for app in self.jobs.values():
            if (app.run_dir is not None and app.run_dir.resolve() == source_run
                    and (app.state.get("retrievalPolicyPath") or app.state.get("retrievalMode") == "historical")):
                return app
        return None

    def _check_author_workspace_idle(self, source_run, exclude=None):
        """Keep two author processes from updating one shared workspace (``exclude``: the job asking)."""

        try:
            workspace = saved_goal_options(source_run)["goalWorkspace"]
        except ValueError:
            # A web-cutoff run works in its own folder, which a moved checkout moved
            # with it; its check adopts the new paths before anything starts.
            if self._historical_source_job(source_run) is None:
                raise
            workspace = str(Path(source_run).resolve())
        holder = ownership.held_elsewhere(workspace)
        if holder is not None:
            raise ownership.OwnedElsewhere(ownership.describe(holder), holder)
        for previous in self.jobs.values():
            if previous is exclude:
                continue
            if not previous.run_dir or not (
                previous.has_active_worker()
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
            with app.lock:
                refusal = app.resume_refusal()
            if refusal:
                raise ValueError(refusal)
            self._check_author_workspace_idle(app.run_dir)
            app.resume()
        return app

    def resume_critic_job(self, run_id):
        """Resume an idle job from its saved proof using its role settings."""

        source_app = self.get_job(run_id)
        source_state = source_app.snapshot()
        if source_state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
            raise ValueError(
                "Stop this job before starting another critic from its proof."
            )
        settings = {
            key: source_state.get(key) for key in (
                "criticRounds", "criticCount", "authorSubagents", "thinkingHours", "authorModel",
                "criticModel", "writerModel", "reasoningEffort",
                "authorEffort", "criticEffort", "writerEffort",
                "speedMode", "reasoningSummary", "authorPrompt",
                "workflowStages", "originalContent",
            )
        }
        return self.start_saved_critic_job(source_app.run_dir, settings)

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
        return self.resume_critic_job(run_id)

    @staticmethod
    def _stopped_continuation_plan(source_app, state=None):
        """Describe a durable restart for manual stops, exhaustion, or errors."""

        state = source_app.snapshot() if state is None else state
        if (
            state.get("phase") in {"reviewing", "running", "stopping", "pausing"}
            or source_app.has_active_worker()
        ):
            return None
        historical = bool(state.get("retrievalPolicyPath") or state.get("retrievalMode") == "historical")
        # Also after a finished result: a held run's critic can run again once the hold is released.
        if historical and state.get("retrievalPolicyPath") and recheckable_hold(state.get("launchHold")):
            return {"action": "preflight", "label": "Re-run preflight",
                    "description": f"Checks this run's {historical_checks(saved_workflow_stages(state))} again; "
                                   "once they pass, the hold is released and a prepared or paused run continues."}
        if (state.get("resultKind") in {"final", "proof", "review"}
                and not state.get("manuallyStopped") and not state.get("error")):
            return None
        if state.get("phase") == "paused" and state.get("timeLimitExhausted"):
            # Resume would be refused: the job page raises the limit first.
            return None
        if state.get("phase") == "paused" and not (
                state.get("sessionUnavailable") and state.get("stage") in {"solve", "repair"}):
            # A job stopped at its time limit (and since given more) continues; a paused one resumes.
            return {"action": "resume", "label": "Continue" if state.get("pauseCause") == "time-limit" else "Resume",
                    "description": "Continue in the same run folder using the saved author session and notebooks."}
        if state.get("phase") == "prepared":
            if state.get("launchHold"):
                return None
            if state.get("launchQueued"):
                return {"action": "prepared", "label": "Start now",
                        "description": "Start this queued job now, ahead of the queue and the running-jobs limit."}
            return {"action": "prepared", "label": "Start prepared run",
                    "description": "Start a fresh author session in this prepared research workspace."}
        if historical:
            # Its policy binds this workspace and cutoff: continue in place.
            return Server._historical_continuation_plan(source_app, state)
        stage = state.get("stoppedStage") or state.get("stage")
        # A saved LaTeX source is compiled again instead of paying for another editor request.
        latex = source_app.run_dir / "latex-source.tex" if source_app.run_dir else None
        recompile = {
            "action": "final",
            "label": "Retry LaTeX compilation",
            "description": (
                "Compiles the saved LaTeX source again in a new run, without another "
                "LaTeX-editor request; compiler errors go to the LaTeX repair step."
            ),
        } if latex is not None and latex.is_file() and not latex.is_symlink() else None
        if stage == "final" and state.get("error") and state.get("finalInputReady"):
            return recompile or {
                "action": "final", "label": "Retry LaTeX editor",
                "description": "Retry only the writing stage from its exact saved statement and existing work."}
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
        if (state.get("phase") in {"input", "reviewed"} and state.get("error") and state.get("reviewInputRecorded")
                and state.get("reviewStatement", "").strip()):
            # A failed or interrupted review runs again in this job, feedback included.
            return {"action": "review", "label": "Retry statement review", "inPlace": True,
                    "description": "Runs the statement review again in this job from its saved input, with the "
                                   "same feedback, settings and prompt."}
        # The stage-specific retries are for a stage that ended without its result:
        # stopped by the user, failed, or interrupted where it could not continue by itself.
        if not (state.get("manuallyStopped")
                or (state.get("error") and state.get("resultKind") not in {"final", "proof", "review", "needs-revision"})):
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
                    "Restores the saved proof for fresh independent critics and "
                    "their coordinator in a new run with a renewed time budget."
                ),
            }
        if stage == "final":
            if state.get("problemMode") == "latex":
                ready = (source_app.run_dir / "latex-input.md").is_file()
            else:
                ready = bool(state.get("finalInputReady"))
            if ready and recompile:
                return recompile
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
    def _historical_continuation_plan(source_app, state):
        """In-place continuations of a stopped historical run (see App.continue_historical)."""
        run_dir = source_app.run_dir
        if run_dir is None or state.get("launchHold"):
            return None
        # Only stages this run's workflow selected continue.
        stages = saved_workflow_stages(state)
        stage = state.get("stoppedStage") or state.get("stage")
        budget = " The time budget starts again."
        if stage in {"solve", "repair", "failure"} and stages["author"] and not (run_dir / "final.tex").is_file():
            try:
                saved_statement(run_dir)
            except ValueError:
                return None
            gone = state.get("phase") == "paused" and state.get("sessionUnavailable")
            return {"action": "author", "inPlace": True, "label": "Continue proof author", "description": (
                "The saved author conversation cannot be reopened (its thread ID is missing): starts a new "
                "author conversation in this same run folder, under the saved web cutoff, with the YAML resume "
                "prompt." if gone else
                "Continues the author in this same run folder under the saved web cutoff, resuming the saved "
                "author conversation when it still exists.") + budget}
        candidate = (run_dir / runtime.SAVED_CANDIDATE_FILENAME).is_file() or (run_dir / "SOLUTION.md").is_file()
        if stage == "final" and stages["writing"] and state.get("finalInputReady"):
            return {"action": "final", "inPlace": True, "label": "Retry LaTeX editor",
                    "description": "Runs the LaTeX stage again from the exact saved final input in this same run "
                                   "folder under the saved web cutoff." + budget}
        if stage in {"critic", "final"} and stages["critic"] and candidate:
            return {"action": "critic", "inPlace": True, "label": "Continue critic",
                    "description": "Runs the critic again on the saved proof in this same run folder under the "
                                   "saved web cutoff." + budget}
        return None

    @staticmethod
    def _queue_saved_author_instructions(source_app, target_app):
        """Replay prior user steering into a newly created author process.

        Every earlier instruction is queued as one block when they fit in one
        live instruction. Otherwise the whole history is written to
        INSTRUCTION_HISTORY_FILENAME in the author's workspace, and the block
        points the author to it before repeating the newest instructions that
        fit (the oldest are left to the file).
        """

        if not saved_workflow_stages(target_app.state)["author"]:
            return
        source_directory = source_app.run_dir if isinstance(source_app, App) else source_app
        instructions = saved_author_instructions(source_directory)
        if not instructions:
            return
        combined = (
            "\n\n".join(instructions)
        )
        stage = target_app.state.get("stage") or "solve"
        note = ""
        if len(combined) > runtime.AUTHOR_STEER_MAX_CHARS:
            workspace = Path(target_app.state.get("goalWorkspace") or target_app.run_dir)
            try:
                runtime._private_atomic_write(workspace / INSTRUCTION_HISTORY_FILENAME,
                                              instruction_history(instructions),
                                              staging_directory=target_app.run_dir.parent)
                lead = RESTORED_INSTRUCTIONS_NOTE
            except (OSError, UnicodeError, ValueError, runtime.Error) as exc:
                lead = ""
                target_app.add_trace({"kind": "diagnostic", "stage": stage, "node": "author",
                                      "text": f"The earlier live instructions could not be saved to "
                                              f"{INSTRUCTION_HISTORY_FILENAME}: {exc}"})
            kept, size = [], len(lead)
            for instruction in reversed(instructions):
                additional = len(instruction) + (2 if lead or kept else 0)
                if size + additional > runtime.AUTHOR_STEER_MAX_CHARS:
                    break  # The oldest are left out first.
                kept.insert(0, instruction)
                size += additional
            combined = "\n\n".join([lead, *kept] if lead else kept)
            left = len(instructions) - len(kept)
            note = (f"{left} older instruction{'s are' if left != 1 else ' is'} only in "
                    f"{INSTRUCTION_HISTORY_FILENAME}, which the author is asked to read." if lead
                    else f"{left} older instruction{'s were' if left != 1 else ' was'} left out because "
                         "they do not fit in one live instruction.")
        command_id = target_app._write_author_steer(combined)
        target_app.add_trace({
            "kind": "status",
            "stage": stage,
            "node": "author",
            "label": "Restored live instructions queued",
            "text": combined,
            "restoredInstructions": instructions,
            "steerId": command_id,
        })
        if note:
            target_app.add_trace({"kind": "status", "stage": stage, "node": "author",
                                  "label": "Earlier live instructions shortened", "text": note})

    def continue_stopped_job(self, run_id):
        """Continue a saved stage with new logs and the existing author workspace."""

        # Match delete_job's jobs -> app lock order while registering the new job.
        with self.jobs_lock:
            source_app = self.get_job(run_id)
            with source_app.lock:
                plan = self._stopped_continuation_plan(source_app)
                if not plan or plan["action"] != "preflight":
                    return self._continue_stopped_job_locked(source_app)
        # Its checks can take a minute: never hold the job list meanwhile.
        return source_app.rerun_historical_preflight()

    def _continue_stopped_job_locked(self, source_app):
        """Create a continuation while holding the source job lock."""

        validated_continuation_source(source_app.run_dir, self.runs)
        source_state = source_app.snapshot()
        plan = self._stopped_continuation_plan(source_app, source_state)
        if plan is None:
            raise ValueError(
                "This job has no saved stage available to continue."
            )
        if plan.get("inPlace") and plan["action"] == "review":
            # A failed or interrupted statement review runs again in its own job.
            source_app.retry_review()
            return source_app
        if plan.get("inPlace"):
            self._check_author_workspace_idle(source_app.run_dir)
            source_app.continue_historical(plan["action"])
            return source_app
        if plan["action"] == "resume":
            return self.resume_paused_job(source_state["runId"])
        if plan["action"] == "prepared":
            self._check_author_workspace_idle(source_app.run_dir)
            source_app.start_queued()
            return source_app
        if plan["action"] == "critic":
            continued = self.resume_critic_job(source_state["runId"])
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
                critic_count=source_state["criticCount"],
                author_subagents=source_state.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS),
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
                convention_prompt=source_state.get("conventionPrompt"),
                final_prompt=source_state["finalPrompt"],
                review_only=source_state["statementReviewOnly"],
                workflow_stages=source_state["workflowStages"],
                original_content=source_state.get("originalContent"),
                **common,
            )
        elif plan["action"] == "author":
            self._check_author_workspace_idle(source_run)
            author_options = {
                "critic_rounds": source_state["criticRounds"],
                "critic_count": source_state["criticCount"],
                "author_subagents": source_state.get("authorSubagents", DEFAULT_AUTHOR_SUBAGENTS),
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
                "convention_prompt": source_state.get("conventionPrompt"),
                "final_prompt": source_state["finalPrompt"],
                "workflow_stages": {**source_state["workflowStages"], "review": False},
                "original_content": source_state.get("originalContent"),
                **common,
            }
            app.start_direct_statement(
                statement=saved_statement(source_run),
                **author_options,
            )
            self._queue_saved_author_instructions(source_app, app)
        else:
            # A saved LaTeX source is compiled again instead of paying for another editor request.
            latex = source_run / "latex-source.tex"
            common["latex_source"] = (read_utf8(latex, "saved LaTeX source")
                                      if latex.is_file() and not latex.is_symlink() else "")
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
                    "settingsWarning", "runningElsewhere",
                    "launchHold", "retrievalPolicyPath", "retrieval", "retrievalMode", "retrievalCutoffUtc",
                    "resultKind", "stage", "workerActive", "processAlive", "activeElapsedSeconds",
                    "workflowStages",
                )
            }
            job["sourceFile"] = state.get("sourceFile") or ""
            # Why a paused job paused: the lists show a stop at the time limit apart from a pause.
            job["pauseCause"] = (state.get("pauseCause") or "") if state["phase"] == "paused" else ""
            # The folder batch whose related works the job shares ("" for a job started alone).
            job["folderBatch"] = state.get("folderBatch") or ""
            # Whether the paused author is writing a summary or reply (Resume waits for it).
            job["conversationBusy"] = bool(state.get("conversation", {}).get("busy"))
            # Whether the running author has an open question (the job page shows it), by the same rules.
            job["authorQuestionOpen"] = bool(state.get("authorQuestion"))
            # Why the job needs you (it keeps retrying meanwhile when it can), and when it tries next.
            job["needsAttention"] = state.get("needsAttention")
            # The convention critic's loophole warning, until the user ignores it.
            warning = state.get("conventionWarning")
            job["loopholeWarning"] = isinstance(warning, dict) and not warning.get("dismissed")
            job["autoResumeAt"] = state.get("autoResumeAt") or 0
            retry = state.get("modelRetry") if isinstance(state.get("modelRetry"), dict) else {}
            job["retryAt"] = retry.get("retryAt") or 0
            job["launchQueued"] = (bool(state.get("launchQueued")) and state["phase"] == "prepared"
                                   and not state.get("launchHold"))
            job["stageLabel"] = state["workflow"]["nodes"].get(state["activeNode"], {}).get("label", "")
            job["title"] = (
                state["problemDescription"].strip().split("\n", 1)[0]
                if state["problemMode"] == "algorithmic"
                else state["draft"].strip().split("\n", 1)[0]
            )
            run_dir = app.run_dir
            checkpoints = state.get("checkpoints", [])
            historical = bool(state.get("retrievalPolicyPath") or state.get("retrievalMode") == "historical")
            if historical and state.get("launchHold"):
                # A held run continues only after Re-run preflight releases it.
                # Copy: the snapshot shares these dicts with the job's cache.
                checkpoints = [{**checkpoint, "resumable": False} for checkpoint in checkpoints]
            job["checkpoints"] = checkpoints
            # A historical run's critic continues in its own folder (start_saved_critic_job).
            job["canResumeCritic"] = bool(
                not (historical and state.get("launchHold"))
                and saved_workflow_stages(state)["critic"]
                and state["phase"] not in {"reviewing", "running", "stopping", "pausing", "paused"}
                and run_dir
                and (
                    (run_dir / "SOLUTION.md").is_file()
                    or (run_dir / runtime.SAVED_CANDIDATE_FILENAME).is_file()
                )
                and controlled_path(run_dir, "checked-statement.md").is_file()
            )
            continuation = self._stopped_continuation_plan(app, state)
            job["canContinueStopped"] = continuation is not None
            job["continueStoppedLabel"] = (
                continuation["label"] if continuation else ""
            )
            job["continueStoppedDescription"] = (
                continuation["description"] if continuation else ""
            )
            job["continueStoppedInPlace"] = bool(continuation and continuation.get("inPlace"))
            jobs.append(job)
        return sorted(jobs, key=lambda job: job["startedAt"], reverse=True)

    def delete_job(self, run_id):
        """Remove an idle job and move its private folder to local trash."""

        with self.jobs_lock:
            app = self.jobs.get(run_id)
            if not app:
                raise ValueError("That job is not available in this TCS Prover session.")
            with app.lock:
                if app._conversation_running_locked():
                    raise ValueError("The author is replying. Stop the reply before deleting this job.")
                if (
                    app.state["phase"] in {"reviewing", "running", "stopping", "pausing"}
                    or app.worker_token is not None
                ):
                    raise ValueError("Stop this job before deleting it.")
                folder = app.run_dir
                if not folder or folder.resolve().parent != self.runs.resolve():
                    raise ValueError("The job folder is outside the runs directory.")
                for other in self.jobs.values():
                    if other is app or not (
                        other.has_active_worker()
                        or other.state["phase"] in {"reviewing", "running", "stopping", "pausing"}
                    ):
                        continue
                    workspace = other.state.get("goalWorkspace")
                    if workspace and Path(workspace).resolve() == folder.resolve():
                        raise ValueError("Stop the continuation using this author workspace before deleting it.")
                trash = self.runs / TRASH_FOLDER
                trash.mkdir(parents=True, exist_ok=True, mode=0o700)
                trash.chmod(0o700)
                target, number = trash / folder.name, 2
                while target.exists():
                    target = trash / f"{folder.name}-{number}"
                    number += 1
                # Own the run while it moves, so no runner of another process still uses it.
                app._claims.claim(folder)
                try:
                    folder.rename(target)
                finally:
                    app._claims.release_all()
                # The controller's copies go with it (the trash keeps the run folder's).
                shutil.rmtree(controller_directory(folder), ignore_errors=True)
                del self.jobs[run_id]
        # The trash keeps each deleted job for TRASH_KEPT_DAYS days.
        prune_trash(self.runs)
        return {"deleted": run_id}

    def api_model_listing(self):
        """Saved key connections (never their keys), entries that cannot be used, and the role menus.

        A store that cannot be read is reported (storeProblem), never shown as
        empty; jobs keep the settings their runner read last meanwhile.
        """

        try:
            connections, invalid = providers.store_listing()
            problem = ""
        except providers.StoreUnavailable as exc:
            connections, invalid = [], []
            problem = (f"{exc} Saved connections cannot be listed or changed until it can be read; running jobs "
                       f"keep the settings they read last, and waiting jobs check again every {retry_interval_text()}.")
        return {"connections": connections, "models": connections, "invalid": invalid, "storeProblem": problem,
                "stateProblem": getattr(self, "state_problem", ""), "formats": API_MODEL_FORMATS,
                **model_menu_settings()}

    def _api_model_jobs(self, model_id):
        """Run ids of the jobs naming one model: (running, waiting to start or resume)."""

        active, waiting = [], []
        with self.jobs_lock:
            apps = list(self.jobs.values())
        for app in apps:
            with app.lock:
                busy = app.state["phase"] in ACTIVE_PHASES or app.worker_token is not None
                # A paused or prepared job never runs its statement reviewer again (unless that is queued).
                roles = (ROLES if busy or app.state["phase"] == "reviewed" or app._queued_review()
                         else ("author", "critic", "writer"))
                choices = [app.state.get(f"{role}Model") for role in roles]
                snapshots = saved_api_models(app.state.get("apiModels"))
                if not any(choice == model_id or
                           (providers.api_model(choice) or snapshots.get(choice) or {}).get("sourceId") == model_id
                           for choice in choices):
                    continue
                if busy:
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
        while the format and base URL stay the same.

        A new model with exactly the settings a job recorded for a removed API
        model gets that model's id back, whatever its display name, so the job
        can continue. A new model never takes a removed id that jobs recorded
        with another format: the format chooses the CLI of their sessions.
        """

        if not body.get("model"):
            return self.save_api_connection(body)
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
                        settings[key] == entry[key] for key in ("format", "baseUrl", "model")))
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

    def save_api_connection(self, body):
        """Discover and save one key once; role menus select its models later."""

        requested = body.get("id") or None
        saved = providers.api_connection(requested) if requested else None
        expected = ({**providers.validated_api_connection({}, requested),
                     "label": saved["label"], "updatedAt": saved["updatedAt"]} if saved else None)
        with self.jobs_lock:
            if (saved and body.get("format") in providers.FORMATS and body["format"] != saved["format"]
                    and any(self._api_model_jobs(requested))):
                raise ValueError("The API format decides which CLI runs these models, so it cannot change while "
                                 "jobs use this connection. Add a new API connection for the other format.")
        entry = providers.validated_api_connection(body, requested if saved else None)
        discovery = discover_api_models(entry)
        if not discovery["ok"]:
            raise ValueError(discovery["message"] or "No models are available for this API key.")
        with self.jobs_lock:
            # Recheck after the network request: jobs can start while it loads.
            current = providers.api_connection(requested) if requested else None
            if (current and entry["format"] != current["format"] and any(self._api_model_jobs(requested))):
                raise ValueError("The API format cannot change while jobs use this connection.")
            removed = self._removed_api_models()
            reserved, candidates = set(), []
            offered = {row["id"] for row in discovery["models"]}
            for model_id, (formats, snapshot) in removed.items():
                source_id = (snapshot or {}).get("sourceId", model_id)
                if formats - {entry["format"]}:
                    reserved.add(source_id)
                elif (snapshot and snapshot["format"] == entry["format"] and snapshot["baseUrl"] == entry["baseUrl"]
                      and snapshot["model"] in offered and providers.api_connection(source_id) is None):
                    candidates.append((source_id, snapshot, model_id == source_id))
            values = {**body, **entry}
            legacy_model = None
            if not requested and candidates:
                identity, snapshot, legacy = sorted(candidates, key=lambda item: item[0])[0]
                values.update(id=identity, label=body.get("label") or snapshot.get("sourceLabel") or snapshot["label"])
                legacy_model = snapshot["model"] if legacy else None
            if values.get("id") in reserved:
                raise ValueError("Saved jobs still use this API connection with another format. Choose their saved format.")
            connection = providers.save_api_connection(values, discovery["models"], reserved=reserved,
                                                       legacy_model=legacy_model, expected=expected)
        return {"connection": connection, "model": connection, **self.api_model_listing()}

    def delete_api_model(self, body):
        """Remove one API connection unless a job that runs or waits to start or resume uses one of its models.

        An entry of the store that cannot be used is removed by its ``ref``
        (see providers.invalid_api_models()) or its id.
        """

        reference = body.get("ref")
        if isinstance(reference, str) and reference.startswith("invalid-"):
            with self.jobs_lock:
                if not providers.delete_invalid_api_model(reference):
                    raise ValueError("That entry has changed since the list was read; open the list again.")
            return {"deleted": reference, "warning": "", **self.api_model_listing()}
        model_id = body.get("id")
        if not providers.is_api_id(model_id):
            raise ValueError("Choose a configured API model to remove.")
        with self.jobs_lock:
            saved = providers.api_connection(model_id, cached=False)
            if saved is None:
                if any(entry["id"] == model_id for entry in providers.invalid_api_models()):
                    providers.delete_api_model(model_id)
                    return {"deleted": model_id, "warning": "", **self.api_model_listing()}
                raise ValueError("That API model is not configured.")
            active, waiting = self._api_model_jobs(model_id)
            if active:
                raise ValueError(
                    f"{saved['label']} is in use by a running job. Stop that job before removing "
                    "this API model."
                )
            if waiting:
                # A waiting job cannot change its model: it would wait until the model is added again.
                count = len(waiting)
                raise ValueError(
                    f"{saved['label']} is used by {count} job{'s' if count != 1 else ''} waiting to start or "
                    f"resume, which cannot continue without it. Edit the connection instead, or delete "
                    f"{'that job' if count == 1 else 'those jobs'} first."
                )
            providers.delete_api_model(model_id)
        return {"deleted": model_id, "warning": "", **self.api_model_listing()}

    def discover_api_models(self, body):
        """List the models a new or saved API key can access, before choosing one."""

        requested = body.get("id") or None
        fields = {key: body[key] for key in ("format", "baseUrl", "requestSource", "apiKey") if key in body}
        entry = providers.validated_api_connection(fields, requested)
        result = discover_api_models(entry)
        if requested and result["ok"]:
            current = providers.validated_api_connection({}, requested)
            if all(current[key] == entry[key] for key in ("format", "baseUrl", "requestSource", "apiKey")):
                result["connection"] = providers.refresh_api_connection(requested, entry, result["models"])
                listing = self.api_model_listing()
                listing.pop("models", None)  # models here remains the discovery result's wire choices.
                result.update(listing)
        return result

    def probe_api_model(self, body):
        """Test a saved API model ({id}) or unsaved form fields with one request.

        Form fields with the id of a saved model fill gaps from that model; an
        empty key is filled in only while the format and base URL
        are the saved ones, so the saved key never goes to another destination.
        Only a test of exactly the saved settings is recorded as its last test.
        """

        requested = body.get("id") or None
        if not body.get("model"):
            fields = {key: body[key] for key in ("format", "baseUrl", "requestSource", "apiKey") if key in body}
            entry = providers.validated_api_connection(fields, requested)
            result = discover_api_models(entry)
            result["recorded"] = False
            if requested:
                current = providers.validated_api_connection({}, requested)
                if all(current[key] == entry[key] for key in ("format", "baseUrl", "requestSource", "apiKey")):
                    connection = providers.record_api_model_test(requested, {
                        "ok": result["ok"], "status": result["status"], "message": result["message"],
                        "latencyMs": result["latencyMs"], "at": result["testedAt"]})
                    result.update(recorded=True, connection=providers.api_connection(connection["id"]), apiModel=connection)
            return result
        fields = {key: body[key] for key in ("format", "baseUrl", "requestSource", "model", "apiKey", "label",
                                             "contextWindow", "maxOutputTokens") if key in body}
        if requested and not fields and providers.api_model(requested) is None:
            raise ValueError("That API model is not configured.")
        entry, saved = providers.validated_api_model(fields, requested)
        result = probe_api_endpoint(entry, effort=body.get("effort"))
        # The Test asks for the max output too, so a different one is a different test.
        request_fields = ("format", "baseUrl", "requestSource", "model", "apiKey", "maxOutputTokens")
        result["recorded"] = False
        if saved is not None and all(saved.get(key) == entry.get(key) for key in request_fields):
            try:
                _, current = providers.validated_api_model({}, requested)
                if current is not None and all(current.get(key) == entry.get(key) for key in request_fields):
                    result["apiModel"] = providers.record_api_model_test(requested, {
                        "ok": result["ok"], "status": result["status"], "message": result["message"],
                        "latencyMs": result["latencyMs"], "at": result["testedAt"],
                    })
                    result["recorded"] = True
            except providers.ProviderError:
                pass  # Removed or changed during the test: nothing to record.
        return result

    def stop_all(self, wait=SHUTDOWN_WAIT_SECONDS):
        """Pause running authors and interrupt every other live child when the server closes.

        An author pauses with the cause 'restart', which the next start resumes
        automatically. Review, critic and LaTeX runners are interrupted; the next
        start continues them from their saved input. Whatever still runs after
        `wait` seconds is stopped, so no runner keeps working unobserved.
        """

        retry_stop = getattr(self, "_retry_stop", None)
        if isinstance(retry_stop, threading.Event):
            retry_stop.set()  # No automatic resume starts while the server closes.
        with self.jobs_lock:
            apps = list({id(app): app for app in self.jobs.values()}.values())
        live = []
        for app in apps:
            with app.lock:
                process = app.process
                author = (app.state["phase"] == "running" and app.state["activeNode"] == "author"
                          and app.state["stage"] in {"solve", "repair"})
            if process is None:
                continue
            live.append((app, process))
            # One job that cannot be stopped must not leave the others running.
            try:
                if author:
                    try:
                        app.pause(_for_restart=True)
                        continue
                    except (OSError, UnicodeError, TypeError, ValueError):
                        pass  # Interrupted instead: the next start continues it as an interrupted author.
                if os.name == "nt":
                    # Windows Popen cannot send SIGINT, and runners in their own
                    # process group never see the console's Ctrl-C.
                    stop_process_tree(process)
                else:
                    # SIGINT keeps the runner's graceful goal-pause cleanup.
                    process.send_signal(signal.SIGINT)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and any(
                process.poll() is None or app.has_active_worker() for app, process in live):
            time.sleep(0.1)
        remaining = [process for _, process in live if process.poll() is None]
        workers = [threading.Thread(target=stop_process_tree, args=(process,), daemon=True) for process in remaining]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()


class CountingReader:
    """A request body stream that counts what it has read (the rest is discarded after a refusal)."""

    def __init__(self, stream):
        self.stream, self.count = stream, 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.count += len(data)
        return data


class Handler(BaseHTTPRequestHandler):
    """Serve the local UI and its job-specific JSON endpoints."""

    def log_message(self, *_):
        pass

    def receive_upload(self, query):
        """POST /uploads?name=: stage one related-work file sent as its raw bytes (at most 64 MiB)."""

        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self.send({"error": "Send the file with its Content-Length, without transfer encoding."},
                             status=400, headers={"Connection": "close"})
        length = self.headers.get("Content-Length")
        if length is None or not length.strip().isdigit():
            self.close_connection = True
            return self.send({"error": "Send the file's size in its Content-Length."}, status=411,
                             headers={"Connection": "close"})
        size = int(length)
        if self.headers.get_content_type() != "application/octet-stream":
            self.discard_body(size)
            return self.send({"error": "Send the file's bytes as application/octet-stream."}, status=400,
                             headers={"Connection": "close"})
        reader = CountingReader(self.rfile)
        try:
            upload = self.server._uploads().receive((query.get("name") or [""])[0], reader, size)
        except related_works.RelatedWorksError as exc:
            # Every check comes before the bytes are read: drop the rest, so the browser gets this reason.
            if reader.count < size:
                self.discard_body(size - reader.count)
            return self.send({"error": str(exc)}, status=exc.status, headers={"Connection": "close"})
        return self.send({"upload": upload})

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

    def discard_body(self, size):
        """Read and drop a refused body (up to MAX_DISCARDED_REQUEST_BYTES; a sender that stalls is left)."""

        self.close_connection = True
        left = min(max(size, 0), MAX_DISCARDED_REQUEST_BYTES)
        try:
            self.connection.settimeout(1)
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

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
        if request.path == "/batch-related-works":
            # Here ``batch`` names a folder batch ("Run a folder…"), or ``job`` one of its jobs.
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                return self.send(self.server.batch_related_works(batch or "", run_id))
            except ValueError as exc:
                return self.send({"error": str(exc)}, status=400)
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
                    from .batches import batch_counts

                    root.update(jobCount=len(jobs), counts=batch_counts(jobs), current=True, readOnly=False)
                    # The home page's banner: jobs that need you, and the running-jobs limit.
                    root["attention"] = [{**row["needsAttention"], "runId": row["runId"],
                                          "job": row.get("sourceFile") or row["title"] or row["runId"]}
                                         for row in jobs if row.get("needsAttention")]
                    root["runningLimit"] = getattr(self.server, "installation", {}).get("maxRunningJobs", 0)
                    root["queued"] = sum(1 for row in jobs if row.get("launchQueued"))
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
        if request.path == "/download-critic-diff":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                name = (query.get("file") or [""])[0]
                body = self.server.get_job(run_id).critic_diff(name)
                return self.send(body, "text/x-diff; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="critic-changes-{name}"'})
            except (ValueError, OSError) as exc:
                return self.send({"error": str(exc)}, status=400)
        if request.path in {"/download-proof", "/download-source"}:
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                app = self.server.get_job(run_id)
                proof = request.path == "/download-proof"
                return self.send(app.approved_proof() if proof else app.latex_source(),
                                 "text/markdown; charset=utf-8" if proof else "application/x-tex; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename="
                                          + ('"approved-proof.md"' if proof else '"latex-source.tex"')})
            except (ValueError, OSError) as exc:
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
        if request.path == "/settings":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            return self.send(self.server.installation_settings())
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
            # Only the batch related-works route names a folder batch; elsewhere ``batch`` is a saved batch.
            folder_batch = batch if request.path == "/batch-related-works/add" and batch != "__root__" else None
            if batch is not None and batch != "__root__" and folder_batch is None:
                return self.send({"error": "Saved batches are read-only."}, status=403)
            if request.path == "/uploads":
                return self.receive_upload(query)
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a JSON request.")
            size = int(self.headers.get("Content-Length", "0"))
            limit = MAX_DOCUMENT_REQUEST_BYTES if request.path in JOB_REQUEST_PATHS else MAX_REQUEST_BYTES
            if not 0 <= size <= limit:
                # Read the body first, so the browser receives this reason, not a reset connection.
                self.discard_body(size)
                return self.send({"error": f"This request is {size / 1048576:.1f} MB; the limit is "
                                           f"{limit / 1048576:g} MB."}, status=413, headers={"Connection": "close"})
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            # API models belong to this installation, not to one job; no
            # response ever contains a key.
            api_model_actions = {
                "/api-models": self.server.save_api_model,
                "/api-models/delete": self.server.delete_api_model,
                "/api-models/test": self.server.probe_api_model,
                "/api-models/discover": self.server.discover_api_models,
                "/settings": self.server.save_installation_settings,
            }
            if request.path in api_model_actions:
                return self.send(api_model_actions[request.path](body))
            # Staged uploads belong to no job until one takes them.
            if request.path == "/uploads/note":
                return self.send({"upload": self.server._uploads().note(body.get("title"), body.get("text"))})
            if request.path == "/uploads/discard":
                return self.send({"discarded": self.server._uploads().discard(body.get("token"))})
            if request.path == "/batch-related-works/add":
                return self.send(self.server.add_batch_related_works(body.get("uploads"), folder_batch or "",
                                                                     run_id))
            if request.path == "/delete-job":
                return self.send(self.server.delete_job(run_id))
            if request.path == "/direct-batch":
                started = self.server.start_direct_batch(body, run_id)
                return self.send({
                    "batchId": "__root__",
                    # The folder batch's ID, which names its shared related works.
                    "folderBatch": getattr(started, "folder_batch", ""),
                    "startedJobs": [app.state["runId"] for app in started],
                    "queuedJobs": [app.state["runId"] for app in started if app.state.get("phase") == "prepared"],
                    "notice": getattr(started, "notice", ""),
                    "jobs": self.server.job_list(),
                })
            if request.path == "/resume":
                app = self.server.resume_paused_job(run_id)
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
            elif request.path == "/start-workflow":
                app = self.server.start_workflow(body, run_id)
            elif request.path == "/direct":
                app = self.server.start_direct_job(body, run_id)
            elif request.path == "/finalize":
                app = self.server.start_latex_job(body, run_id)
            else:
                app = self.server.get_job(run_id)
            if request.path in {"/approve", "/solve"}:
                app.approve(body.get("statement"))
            elif request.path == "/set-author-time-limit":
                app.set_author_time_limit(body.get("hours"))
            elif request.path == "/steer-author":
                app.steer_author(body.get("instruction"))
            elif request.path == "/answer-author-question":
                app.answer_author_question(body.get("questionId"), body.get("answers"))
            elif request.path == "/dismiss-author-question":
                app.dismiss_author_question(body.get("questionId"))
            elif request.path == "/ask-author":
                app.ask_author(body.get("text"))
            elif request.path == "/author-summary":
                app.request_author_summary()
            elif request.path == "/stop-author-reply":
                app.stop_author_reply(body.get("id"))
            elif request.path == "/related-works/add":
                app.add_related_works(body.get("uploads"), self.server._uploads())
            elif request.path == "/dismiss-convention-warning":
                app.dismiss_convention_warning(body.get("time"))
            elif request.path == "/stop":
                app.stop()
            elif request.path == "/pause":
                app.pause()
            elif request.path == "/reset":
                app.reset()
            elif request.path == "/clear-trace":
                app.clear_trace()
            elif request.path not in {
                "/review", "/direct", "/finalize", "/start-workflow",
                "/resume-critic", "/resume-checkpoint", "/continue-stopped", "/resume",
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
