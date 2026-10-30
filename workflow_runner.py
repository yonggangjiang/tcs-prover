#!/usr/bin/env python3
"""Execute declarative YAML workflows using persistent goals and structured model calls."""

import argparse
import ast
import copy as _copy
import difflib
import errno
import functools
import hashlib
import json
import math
import os
import operator
import queue
import random
import re
import shutil
import signal
import stat
import string
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, confinement, proof_handoff, providers
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

try:
    import yaml
    from jsonschema.exceptions import SchemaError
    from jsonschema.validators import validator_for
except ImportError as exc:
    raise SystemExit("Install workflow support with: python3 -m pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parent
WORKFLOWS = ROOT / "workflows"
MARKER = "[STATEMENT]"
# Built-in models offered in menus and docs (runner/providers.py is the
# registry): Codex CLI models first, then Claude CLI models. Configured API
# models (`api:<id>`) are accepted too; they are user data, not constants.
OFFERED_MODELS = providers.offered_models()
# Codex now calls the 5.6 generation "Older". Saved runs and scripts that name
# it keep working, so it stays accepted but is no longer offered.
LEGACY_MODELS = providers.LEGACY_CODEX_MODELS
MODELS = OFFERED_MODELS + LEGACY_MODELS
MODEL, EFFORT = "gpt-6.1-sol", "max"
EFFORTS = providers.CODEX_EFFORTS
# Efforts no longer offered, still accepted from saved jobs, scripts and flags (see chosen_effort()).
RETIRED_EFFORTS = providers.RETIRED_EFFORTS
RETIRED_EFFORT_NOTE = "Ultra is no longer offered; this run uses Max."
# Efforts each built-in model lists. Codex does not validate or clamp efforts
# locally, so the UI offers only these; effective_effort() maps the rest.
MODEL_EFFORTS = {model: providers.efforts_for(model) for model in MODELS}
SPEEDS, DEFAULT_SPEED = ("standard", "fast"), "standard"
AUTHOR_MODEL = CRITIC_MODEL = WRITER_MODEL = MODEL
MAX_CRITIC_ROUNDS = 100
# Independent critics per critic round: the subagents the critic starts (see critic_count()).
MAX_CRITIC_COUNT = 10
# Subagents the proof author may start at a time (the run's Author subagents setting; 0: none).
MAX_AUTHOR_SUBAGENTS = 10
# The total time limit has no upper bound, and 0 (unlimited) is the default.
MAX_AUTHOR_HOURS = None
DEFAULT_AUTHOR_HOURS = 0
STRUCTURED_WORKSPACE = ROOT / ".codex-structured-workspace"
_GOAL_LIFECYCLE = ("goal", "continuation", "compaction", "repair", "resume")
# Lifecycle prompts a goal node may also bind: the short prompt of a reopened
# conversation, the paused author's conversation turns (--converse), the
# block of the first prompt that points to related works and the note that
# tells a resumed author why the controller refused its last submission.
_GOAL_OPTIONAL_LIFECYCLE = ("pause", "reopen", "summary", "conversation", "related_works", "handoff_refused",
                            "subagents")
_GOAL_STAGES = {"initial": "solve", "resume": "repair", "failure": "failure"}


REASONING_SUMMARIES = ("none", "concise", "detailed")

DEFAULT_REASONING_SUMMARY = "concise"

# Provider ids in events and YAML provider_options: native Codex models,
# Claude CLI subscription models, and every configured API model ("tcs_api"
# is also the isolated Codex custom provider of OpenAI-format endpoints).
OPENAI_PROVIDER = "openai"

CLAUDE_PROVIDER = "claude"

API_PROVIDER = "tcs_api"

API_TOKEN_ENV = "TCS_PROVER_API_TOKEN"

CUSTOM_PROVIDER_LOGIN_PLACEHOLDER = "tcs-prover-custom-provider"

# Credentials of removed provider configurations are never passed to a child.
OBSOLETE_CREDENTIAL_ENVIRONMENT = (
    "DEEPSEEK_API_KEY", "TCS_PROVER_DEEPSEEK_TOKEN", "OPENROUTER_API_KEY", "TCS_PROVER_OPENROUTER_TOKEN",
)

SECRET_ENVIRONMENT = ("OPENAI_API_KEY", "CODEX_API_KEY", API_TOKEN_ENV, *OBSOLETE_CREDENTIAL_ENVIRONMENT)

# Inherited credentials whose values never appear in diagnostics or records.
REDACTED_ENVIRONMENT = (*SECRET_ENVIRONMENT, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

# Records redact only secrets at least this long: a shorter "key" cannot be
# told apart from ordinary words, and rewriting those would corrupt records.
MIN_RECORD_SECRET_CHARS = 8

# The Codex permission profile that confines a run's model processes to their
# own folder and private temp folder (see private_permissions()).
PRIVATE_PERMISSIONS = "tcs_prover_private"

# Files the controller writes into the run folder and the live author may only
# read (the controller also keeps its own copies outside the run folder), and
# the related works the user supplied (see confinement.PROTECTED_ENTRIES).
CONTROLLER_FILES = confinement.CONTROLLER_FILES

# Proof snapshots in the temp folder: the newest are kept for diagnosis, older
# ones are removed once they are a day old (see run_structured_attempt()).
SNAPSHOT_PREFIX = "tcs-proof-review-"
SNAPSHOTS_KEPT = 50

HISTORICAL_CODEX_ONLY = "Web cutoff timestamps require Codex CLI models for every proof role."

# Kept for older callers; workflow model calls no longer have an attempt limit.
STRUCTURED_MAX_ATTEMPTS = 2

# Every failed model call (structured roles and the author) is retried after
# this wait, with no attempt limit. Waits do not consume the workflow budget.
MODEL_RETRY_SECONDS = 60

# Up to this many extra seconds spread the retries of many jobs and calls,
# so a provider at capacity does not receive them all in the same second.
MODEL_RETRY_JITTER_SECONDS = 5

# A retry wait checks Stop, the pause file and the time limit this often.
RETRY_POLL_SECONDS = 0.5

RETRY_LABEL = "Retrying model call"

# A structured attempt is stopped only after its CLI has written nothing for
# this long (every output line counts, subagent messages included), never
# while it works: exec-mode calls cannot be probed, and a false stop costs a
# working attempt. The stopped attempt is retried like any other failure.
STRUCTURED_STALL_SECONDS = 3600

# After a CLI has ended, its output is read this much longer; a process it
# started that still holds the output open is not waited for.
CLI_OUTPUT_GRACE_SECONDS = 5

# Each failed structured attempt's full provider messages and CLI stderr
# (keys removed) are appended to this file in the run folder; a stderr longer
# than twice STDERR_LOG_LINES keeps its first and its newest lines.
MODEL_FAILURE_LOG = "model-failures.log"
STDERR_LOG_LINES = 5000

# So many identical failures to start a CLI in a row are shown as "Codex CLI
# missing or incompatible" (the retries continue).
CLI_PROBLEM_REPEATS = 3

# The CLI check at a runner's start (`--version`, no model call) waits this long,
# and remembers each CLI's path and version in this run-folder file.
CLI_CHECK_SECONDS = 20
CLI_VERSIONS_FILENAME = "cli-versions.json"

# Removing one persisted structured session after its call never takes longer.
SESSION_CLEANUP_SECONDS = 30

# A paused or time-limited author may finish its current step this long after
# the interrupt before its CLI is stopped.
INTERRUPT_GRACE_SECONDS = 30

# Author liveness: after this long without any event while a turn runs, the
# CLI is asked a cheap question; no answer within AUTHOR_PROBE_SECONDS ends
# the attempt as a failed model call (the retry resumes the same thread).
AUTHOR_INACTIVITY_SECONDS = 30 * 60
AUTHOR_PROBE_SECONDS = 60
# A Codex turn that ended with the goal still open, followed by no new turn
# for this long, is continued with the continuation prompt.
AUTHOR_CONTINUE_SECONDS = 60
# The app-server answers every request within this long, or the attempt fails.
RPC_RESPONSE_SECONDS = 600
# This many identical refusals to reopen a saved Codex conversation (not a
# provider failure) continue the work in a new conversation.
AUTHOR_RESUME_REFUSALS = 3
# A live instruction the CLI did not accept is offered again at the next
# turn, or after this long.
STEER_RETRY_SECONDS = 60

# Provider messages for a conversation that no longer fits the model's context.
CONTEXT_WINDOW_MARKERS = (
    "context window", "context_length_exceeded", "context length", "prompt is too long",
    "maximum context", "input is too long", "too many input tokens", "exceeds the context",
)

# Harness notes that continue a resumed conversation after a failed attempt.
# The task itself is unchanged; these only say why the conversation continues.
STRUCTURED_RETRY_NOTE = (
    "RETRY AFTER A MODEL-CALL FAILURE\n"
    "The previous attempt {cause}, before it returned a final answer. The task and its "
    "inputs are unchanged. Continue from your earlier work in this conversation and return "
    "the complete final answer as one JSON object in the required format."
)
STRUCTURED_INVALID_NOTE = (
    "STRUCTURED OUTPUT RECOVERY RETRY\n"
    "Your previous final answer could not be accepted: {problem}.\n"
    "{explanation}"
    "The task and its inputs are unchanged. Return the complete corrected answer as one "
    "JSON object in the required format; do not return a placeholder."
)
SUBAGENT_RETRY_NOTE = (
    "RETRY AFTER FAILED SUBAGENT CALLS\n"
    "{failed} in your previous attempt failed before finishing{reasons}. These are model-call "
    "failures, not findings about the task, so your previous answer was set aside. The task and its "
    "inputs are unchanged. Run each failed subagent again as a fresh subagent with the same "
    "instructions and files, and keep the reports of the subagents that finished; a check you already "
    "re-ran successfully need not run again. When every check has a report, return the complete final "
    "answer as one JSON object in the required format."
)
# Added to STRUCTURED_RETRY_NOTE when the interrupted attempt also had failed subagent calls.
SUBAGENT_RERUN_NOTE = (
    "{failed} in that attempt failed before finishing{reasons}; these are model-call failures, "
    "not findings about the task. Run each failed subagent again as a fresh subagent with the same "
    "instructions and files before you answer."
)
AUTHOR_RETRY_NOTE = (
    "RETRY AFTER A MODEL-CALL FAILURE\n"
    "The previous turn ended early because of a model-call failure. Nothing about the task "
    "has changed. Continue the same work from where you left off in this conversation."
)
# A conversation turn of the paused author (run_author_conversation): the note
# that asks again for a reply a model-call failure cut off, the labels of its
# request and reply records by request kind, and the longest message or notice.
CONVERSATION_RETRY_NOTE = (
    "RETRY AFTER A MODEL-CALL FAILURE\n"
    "Your previous reply was cut off by a model-call failure. Write that reply again, in full."
)
CONVERSATION_LABELS = {"summary": ("Progress summary requested", "Progress summary"),
                       "message": ("Message to the paused author", "Author reply")}
CONVERSATION_MAX_CHARS = 12000
# A live run's conversation turn is read-only and has no subagents (Codex: a
# read-only permission profile with multi_agent off; Claude: Read and the run's
# search tools). False gives it the author's own tools instead. Web-cutoff runs
# always use the author's audited launch.
CONVERSATION_READ_ONLY = True

# Appended to the pause reason of a Codex author whose saved thread ID is
# missing. (A thread that Codex no longer has continues in a new conversation
# by itself.)
UNAVAILABLE_THREAD_HINT = (
    " A retry would lack the ID too, so automatic retry is off. Choose Continue proof author to "
    "continue the saved work in a new conversation."
)
SAFETY_STOP_HINT = " Automatic retry is off; check the setup, then choose Resume."

# Codex and Claude report a saved conversation that no longer exists like this.
MISSING_THREAD_MARKERS = (
    "not found", "not_found", "notfound", "unknown thread", "does not exist",
    "no rollout", "failed to load thread", "failed to load rollout",
    "could not find thread", "thread unavailable", "thread is unavailable",
    "no conversation found",
)

STRUCTURED_RETRY_OUTPUT_CHARS = 12000

# A resumed conversation already holds the previous answer; the note quotes
# only this much of it.
RETRY_EXCERPT_CHARS = 3000

STRUCTURED_HEARTBEAT_SECONDS = 30

# A critic result records whether the coordinator changed the submitted proof
# and the unified diff of its changes (see _proof_change). A longer diff is
# saved whole in the folder the critic_diff_dir option names (the run's
# controller folder) and the result says so; without that option it stays in
# the result. It is never shortened.
PROOF_DIFF_INLINE_CHARS = 200_000

# The saved input of a later visit of a step (node_input, workflow_paused)
# records under this state key which visit it is, and the instruction of a
# verify-only visit: {node, visit[, verify]}. A run resumed at that step
# continues the same visit, so its round and its verify instruction stay the same.
# The step's "Workflow step: <node>" status names the same visit and verify
# instruction, so a controller that kept only its transcript rebuilds it.
STEP_VISIT_KEY = "stepVisit"

# The progress of a call's subagents, from its Codex event stream (see SubagentProgress): the status labels
# the job page and its workflow rail read. Each record also has auditCount (the subagents the call may start).
SUBAGENTS_REQUESTED_LABEL = "{count} independent critic{s} requested"
SUBAGENT_STARTED_LABEL = "Independent critic {index} started"
SUBAGENT_FINISHED_LABEL = "Independent critic {index} finished"
SUBAGENTS_WAITING_LABEL = "Waiting for the independent critics"

SAVED_CANDIDATE_FILENAME = "saved-candidate.md"

FINAL_INPUT_FILENAME = "final-input.json"

AUTHOR_STEER_MAX_CHARS = 12000

EMIT_LOCK = threading.Lock()
REVIEW_MODEL = MODEL
REVIEW_EFFORT = EFFORT


class Error(RuntimeError):
    """Show a short, understandable failure."""


class FinalError(Error):
    """A failure that no retry can fix, so it is never retried automatically.

    A structured or command step pauses with it and waits for the user's
    Resume (see _execute); an author pauses with it as its reason.
    """


class ConfigurationError(FinalError):
    """The run's settings, credentials or retrieval policy do not allow this call."""


class WorkflowDefinitionError(FinalError):
    """A workflow rule cannot be evaluated on a schema-valid answer (a YAML problem, not the model's)."""


class SafetyStop(FinalError):
    """A safety check refused to continue: unexpected tools, a replaced home,
    thread or session, a missing tool runtime, or a changed retrieval policy.

    A paused author is not resumed automatically after it.
    """


class CLIUnavailable(Error):
    """The model's CLI is not on PATH, for example while it updates itself; a later attempt can find it."""


class ToolRuntimeUnavailable(Error):
    """Codex reported that its code-mode tool runtime is unavailable; the session never continues after it.

    A live run's retry starts Codex with the classic shell tool instead; a
    web-cutoff run, which needs the audited tool set, checks again every minute.
    """


class WorkflowPaused(Error):
    """Suspend execution without completing a node or entering its successor."""

    def __init__(self, message="Workflow paused.", *, reason="", thread_id=None, session_unavailable=False,
                 retry=None, safety_stop=False):
        super().__init__(message)
        self.reason = reason
        self.thread_id = thread_id
        # The saved conversation no longer exists, so an automatic retry of
        # the same resume cannot succeed.
        self.session_unavailable = session_unavailable
        # A goal session that ended on a model-call failure describes how to
        # continue it (see _goal_step): its thread, stage, prompt values, the
        # instruction of the interrupted turn and whether the model received it.
        self.retry = retry
        # A safety check stopped the author: only the user resumes it.
        self.safety_stop = safety_stop


class InvalidAnswer(Error):
    """The model answered, but its answer cannot be accepted; a retry says why in plain words.

    `problem` is the plain explanation the retry gives the model, `rule` the
    YAML expression it comes from, and `message` (a node's own error text)
    what a caller sees when no retry follows.
    """

    def __init__(self, problem, *, rule="", detail="", message=None):
        self.problem = str(problem).strip().rstrip(".") or "the answer is invalid"
        self.rule, self.detail = rule, detail
        super().__init__(message or f"The model's answer could not be accepted: {self.problem}.")


class SubagentFailure(Error):
    """Subagent calls failed during an attempt that returned an answer, so the answer rests on an incomplete review.

    `failures` maps each failed subagent to its provider message. A retry
    continues the conversation and runs the failed subagents again
    (SUBAGENT_RETRY_NOTE); the answer itself is never used.
    """

    def __init__(self, failures):
        self.failures = dict(failures)
        failed, reasons = _subagent_summary(self.failures)
        self.plain = f"{failed} failed" + (f" ({reasons})" if reasons else "")
        super().__init__(f"{self.plain}; the answer rests on an incomplete review.")


class ConversationUnavailable(Error):
    """The CLI no longer has the conversation a retry tried to resume."""


class SavedConversationGone(FinalError):
    """A paused author has no saved conversation to reopen for a conversation turn, or the CLI lost it.

    A conversation turn never starts or replaces a conversation, so no retry helps.
    """


class ConversationAttemptFailed(Error):
    """One attempt of a paused author's conversation turn failed on a model call.

    ``retry`` tells the next attempt how to continue: whether the message was
    ``delivered``, whether the model produced ``output``, the failed empty
    turn to remove first (``rewind``: a Codex turn ID, or the Claude message
    to resume at) and whether Codex needs its ``classic`` shell tool.
    """

    def __init__(self, message, *, retry=None):
        super().__init__(message)
        self.retry = {"delivered": False, "output": False, "rewind": None, "classic": False, **(retry or {})}


class ConversationRefused(ConversationAttemptFailed):
    """The CLI refused to reopen the saved author conversation (not a provider failure); ``refusal`` says how."""

    def __init__(self, refusal, *, retry=None):
        super().__init__(refusal, retry=retry)
        self.refusal = refusal


class RetryCancelled(Error):
    """A sibling request ended the workflow step while this one waited to retry."""


class RPCError(Error):
    """The Codex app-server answered a request with a JSON-RPC error; `code` and `payload` keep its details."""

    def __init__(self, message, *, code=None, payload=None):
        super().__init__(message)
        self.code, self.payload = code, payload


class HandoffRefused(Error):
    """The controller cannot hand the author's submission to the next step.

    The workflow pauses at the author as it was before the submission, and the
    author is resumed automatically, whatever the reason (a proof too large for
    the critic included); its handoff_refused prompt, when the workflow binds
    one, tells it the reason. A candidate supplied without the author stage
    cannot change, so that pause waits for the user.
    """


class ModelUnavailable(ConfigurationError):
    """The provider says the model does not exist or that this account cannot use it."""


class RequestRejected(ConfigurationError):
    """An API model's endpoint refuses the request as invalid (HTTP 400): its settings do not fit the endpoint."""


# How the job page and the controller recognize these settings problems in a
# failure message (they lead the messages of the two errors above).
MODEL_UNAVAILABLE_LABEL = "Model unavailable"
REQUEST_REJECTED_LABEL = "Request rejected by the API endpoint"

# Provider words for a model that does not exist or that this account cannot use.
_MODEL_UNAVAILABLE = re.compile(
    r"model[_ ]not[_ ]found|unrecognized[_ ]model|issue with the selected model|"
    r"\bmodel\b[^\n]{0,160}?\bdoes not exist|do(?:es)? not have access to (?:the |this )?model|"
    r"(?:is not|isn't|not) (?:available|supported) (?:for|to|on|with|when using) (?:your|this|a) "
    r"(?:account|plan|organi[sz]ation|subscription|chatgpt account|api key)", re.IGNORECASE)
# Words of a passing condition, which keep a failure retried whatever else it says.
_PASSING_CONDITION = re.compile(r"capacity|overload|high demand|temporar|\b429\b|rate ?limit|\b5\d\d\b",
                                re.IGNORECASE)
# An endpoint that refuses the request itself as malformed for it.
_REJECTED_REQUEST = re.compile(r"\b400\b[\s\S]{0,600}?invalid[_ ]request|invalid[_ ]request[\s\S]{0,600}?\b400\b",
                               re.IGNORECASE)


def settings_failure(error, model=None):
    """A failed model call that only a change of settings can fix, as the error that says so; else None.

    No retry fixes a model that does not exist or that this account cannot
    use (ModelUnavailable), an API model's endpoint that refuses the request
    as invalid (RequestRejected; a conversation that is too long is not
    that), or private TCS Prover state that cannot be used (a ProviderError).
    These end a structured call and pause an author with a message saying
    what to change; every other failure keeps being retried.
    """

    if isinstance(error, (FinalError, WorkflowPaused, RetryCancelled, InvalidAnswer, SubagentFailure,
                          providers.StoreUnavailable)):
        return None
    if isinstance(error, providers.ProviderError):
        return ConfigurationError(str(error))
    if not isinstance(error, (Error, OSError)):
        return None
    text = redact(str(error))
    if _context_window_failure(text) or _PASSING_CONDITION.search(text):
        return None
    try:
        label = providers.model_label(model) if model else "the selected model"
        api = bool(model) and providers.provider_kind(model) == providers.API
    except providers.ProviderError:
        return None  # Its settings cannot be read now: retried like any failure.
    reason = _failure_reason(text) or text
    if _MODEL_UNAVAILABLE.search(text):
        fix = ("check its API connection under Models & API (Load models lists the models its key can use)" if api
               else "check that the model exists and that your login may use it")
        return ModelUnavailable(f"{MODEL_UNAVAILABLE_LABEL}: {label} cannot be used ({reason}). "
                                f"A retry cannot fix this: {fix}.")
    if api and _REJECTED_REQUEST.search(text):
        return RequestRejected(f"{REQUEST_REJECTED_LABEL}: {label} refuses the requests this job sends as "
                               f"invalid ({reason}). A retry cannot fix this: change its API connection's settings "
                               "under Models & API (or the endpoint); a job waiting for it continues once its "
                               "settings change.")
    return None


def retryable_failure(error):
    """Whether a failed model-call attempt can succeed when it is tried again.

    Provider and transport failures (a CLI that failed, crashed or could not
    start, an error or failed-turn event, a stalled attempt, a conversation
    the CLI no longer has), unacceptable answers (InvalidAnswer) and answers
    given after failed subagent calls (SubagentFailure) are retried.
    Configuration errors, workflow-rule errors and safety refusals
    (FinalError), pauses and harness programming errors (TypeError,
    KeyError, ...) are not: a retry could never fix them.
    """

    if isinstance(error, (FinalError, WorkflowPaused, RetryCancelled)):
        return False
    return isinstance(error, (Error, OSError))


def text(value):
    """Require nonempty text."""

    value = value.strip()
    if not value:
        raise Error("The problem statement is empty.")
    return value


def critic_limit(value):
    """Require a small positive critic-round limit."""

    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
        if not isinstance(value, str) and value != parsed:
            raise ValueError
        value = parsed
    except (TypeError, ValueError, OverflowError) as exc:
        raise Error("The critic round limit must be an integer.") from exc
    if not 1 <= value <= MAX_CRITIC_ROUNDS:
        raise Error(f"Choose 1 to {MAX_CRITIC_ROUNDS} critic rounds.")
    return value


def critic_count(value):
    """Require a small positive number of independent critics (the subagents of the critic)."""

    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
        if not isinstance(value, str) and value != parsed:
            raise ValueError
        value = parsed
    except (TypeError, ValueError, OverflowError) as exc:
        raise Error("The number of independent critics must be an integer.") from exc
    if not 1 <= value <= MAX_CRITIC_COUNT:
        raise Error(f"Choose 1 to {MAX_CRITIC_COUNT} independent critics.")
    return value


def author_subagents(value):
    """Require 0 (no subagent tool) to MAX_AUTHOR_SUBAGENTS subagents for the proof author."""

    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
        if not isinstance(value, str) and value != parsed:
            raise ValueError
        value = parsed
    except (TypeError, ValueError, OverflowError) as exc:
        raise Error("The number of author subagents must be an integer.") from exc
    if not 0 <= value <= MAX_AUTHOR_SUBAGENTS:
        raise Error(f"Choose 0 to {MAX_AUTHOR_SUBAGENTS} author subagents.")
    return value


def author_hours(value):
    """Accept zero for unlimited, or any finite positive limit."""

    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise Error("The total workflow time limit must be a number of hours.") from exc
    if not math.isfinite(value) or value < 0:
        raise Error("Choose 0 for unlimited, or a positive number of hours.")
    return value


def prior_elapsed_seconds(value):
    """Require a finite nonnegative workflow runtime before the author starts."""

    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise Error("The prior workflow runtime must be a number of seconds.") from exc
    if not math.isfinite(value) or value < 0:
        raise Error("The prior workflow runtime cannot be negative or infinite.")
    return value


def controlled_author_hours(path, current):
    """Read an optional live total, ignoring incomplete or invalid updates."""

    if not path:
        return current
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        requested = author_hours(value["hours"])
    except (
        Error, OSError, UnicodeError, KeyError, TypeError,
        json.JSONDecodeError,
    ):
        return current
    return requested


def chosen_model(value):
    """Require a built-in model, an older Codex id, or a configured API model.

    An API model store that cannot be read right now is a model-call failure
    (retried), never an unknown model.
    """

    try:
        known = isinstance(value, str) and providers.is_known(value)
    except providers.StoreUnavailable as exc:
        raise Error(str(exc)) from exc
    if known:
        return value
    if providers.replacement_for_removed(value):
        raise Error("DeepSeek is no longer supported; choose another model, for example gpt-6.1-sol.")
    shown = repr(value)[:80] if isinstance(value, str) else type(value).__name__
    raise Error(
        f"Unknown model {shown}. Choose {', '.join(OFFERED_MODELS)}, or an api:<id> model "
        "configured in Advanced settings → API connections."
    )


class HelpFormatter(argparse.HelpFormatter):
    """Wrap help text without splitting model ids such as claude-sonnet-5-5."""

    def _split_lines(self, text, width):
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False)


def model_argument(value):
    """Validate a model id given on the command line."""

    try:
        return chosen_model(value)
    except Error as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def chosen_effort(value):
    """Require one supported Codex reasoning effort, or a retired one (Ultra), which runs as its replacement."""

    if value not in EFFORTS and value not in RETIRED_EFFORTS:
        raise Error("Choose a valid reasoning effort.")
    return value


def effort_argument(value):
    """Validate a reasoning effort given on the command line (the retired Ultra is accepted, not offered)."""

    try:
        return chosen_effort(value)
    except Error as exc:
        raise argparse.ArgumentTypeError(f"{exc} Choose one of: {', '.join(EFFORTS)}.") from exc


EFFORT_METAVAR = "{" + ",".join(EFFORTS) + "}"


def note_retired_efforts(efforts):
    """Say once that a retired effort (Ultra) runs as Max; ``efforts`` are the run's chosen efforts."""

    if any(effort in RETIRED_EFFORTS for effort in efforts):
        emit("status", "workflow", label="Ultra is no longer offered", text=RETIRED_EFFORT_NOTE,
             retiredEffort="ultra", replacementEffort=RETIRED_EFFORTS["ultra"])


def chosen_speed(value):
    """Require Standard or OpenAI's Fast service tier."""

    if value not in SPEEDS:
        raise Error("Choose Standard or Fast speed.")
    return value


def speed_arguments(speed, model=MODEL):
    """Return explicit Codex flags for one selected speed mode and model."""

    speed = effective_speed(model, speed)
    if speed == "fast":
        return ["-c", 'service_tier="fast"', "--enable", "fast_mode"]
    return ["--disable", "fast_mode"]


def context_cache_arguments():
    """Let each compaction window grow after its carried stable prefix."""

    return [
        "-c", 'model_auto_compact_token_limit_scope="body_after_prefix"',
    ]


# The per-job prompts a launch passes as --<role>-prompt-file, and the workflow prompt each replaces.
PROMPT_FILE_OPTIONS = {"author": "author", "critic": "critic", "convention": "convention_critic", "final": "final"}


def prompt_file(path, default):
    """Read an optional per-job prompt, or use the built-in default."""

    return Path(path).read_text(encoding="utf-8") if path else default


def codex():
    """Find the installed Codex CLI."""

    path = shutil.which("codex")
    if not path:
        raise CLIUnavailable("Codex CLI is not installed or is not on PATH.")
    return path


_ISOLATED_CODEX = {}
# Calls on several threads check a binary once, one at a time.
_ISOLATED_CODEX_LOCK = threading.Lock()

CODEX_HOME_PROBE_SECONDS = 30
# The first start in a new private home may take longer while Codex prepares it.
CODEX_HOME_FIRST_PROBE_SECONDS = 120


class _ProbeFailed(Error):
    """A CODEX_HOME probe gave no definite answer (a timeout, a failed start, no or a malformed reply)."""


def _codex_candidates():
    """The Codex on PATH first, then other PATH entries and the standard native installs."""

    names = ("codex.exe",) if os.name == "nt" else ("codex",)
    paths = [shutil.which("codex")]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and Path(entry).is_absolute():
            paths.extend(str(Path(entry) / name) for name in names)
    if os.name != "nt":
        paths += [str(Path.home() / ".codex/packages/standalone/current/bin/codex"),
                  "/Applications/Codex.app/Contents/Resources/codex"]
    found = []
    for path in paths:
        if path and os.path.isfile(path) and os.access(path, os.X_OK) and path not in found:
            found.append(path)
    return found


def _reported_codex_home(executable, environment):
    """Ask an app-server which CODEX_HOME it uses (an initialize handshake, no model request).

    Return the reported home, or None when the reply names none. A probe
    without a definite answer raises _ProbeFailed with the reason and the
    end of its error output.
    """

    try:
        first = not any(Path(environment["CODEX_HOME"]).iterdir())
    except OSError:
        first = True
    limit = CODEX_HOME_FIRST_PROBE_SECONDS if first else CODEX_HOME_PROBE_SECONDS
    try:
        process = subprocess.Popen(
            [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=environment, cwd=environment["CODEX_HOME"],
        )
    except OSError as exc:
        raise _ProbeFailed(f"it could not be started ({exc})") from exc
    reply, errors = {}, deque(maxlen=6)

    def read():
        try:
            process.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "tcs_prover_probe", "title": "TCS Prover", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }}) + "\n")
            process.stdin.flush()
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    message = None
                if not isinstance(message, dict):
                    if line.strip():  # A launcher's own output, not a protocol message.
                        emit("diagnostic", "model", text="Skipped a line of the CODEX_HOME check that is not a "
                                                         f"protocol message: {_clipped(redact(line.strip()), 300)}")
                    continue
                if message.get("id") == 1:
                    reply["answer"] = message
                    return
        except (OSError, ValueError):
            pass

    def drain():
        try:
            for line in process.stderr:
                if line.strip():
                    errors.append(_clipped(redact(line.strip()), 300))
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    drainer = threading.Thread(target=drain, daemon=True)
    reader.start()
    drainer.start()
    reader.join(limit)
    timed_out = reader.is_alive()
    stop_process(process)
    reader.join(timeout=1)
    drainer.join(timeout=1)
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    detail = f" Its error output: {' | '.join(errors)}" if errors else ""
    answer = reply.get("answer")
    if answer is None:
        raise _ProbeFailed((f"it did not answer within {_duration_text(limit)}" if timed_out
                            else f"it ended without answering (exit code {process.returncode})") + "." + detail)
    result = answer.get("result")
    home = result.get("codexHome") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not (home is None or isinstance(home, str) and home):
        raise _ProbeFailed(f"its answer was malformed: {_clipped(redact(json.dumps(answer)), 300)}." + detail)
    return Path(home).resolve() if home else None


def api_codex_executable(environment):
    """A Codex CLI verified to honor the private CODEX_HOME of an API model.

    Some installations wrap codex in a launcher that resets CODEX_HOME, which
    would hand the user's ChatGPT login and connectors to a third-party model.
    Each candidate reports its home through an initialize handshake; the first
    one that uses the private home runs the API model, and only definite
    refusals of every candidate mean refusal (SafetyStop). Only definite
    answers are remembered, per binary version (size and modification time).
    A probe without one (a timeout, a failed start, no or a malformed reply)
    fails the attempt with a retryable error, so the next retry asks again.
    """

    home = Path(environment["CODEX_HOME"]).resolve()
    # The probe needs only CODEX_HOME. A launcher may start the user's own
    # Codex configuration, which must never receive the key.
    probe = {name: value for name, value in environment.items() if name not in SECRET_ENVIRONMENT}
    candidates = _codex_candidates()
    if not candidates:
        raise CLIUnavailable("Codex CLI is not installed or is not on PATH.")
    unknown = []
    for candidate in candidates:
        try:
            info = os.stat(candidate)
            key = (candidate, str(home), info.st_size, info.st_mtime_ns)
        except OSError:
            key = (candidate, str(home))
        with _ISOLATED_CODEX_LOCK:
            if key not in _ISOLATED_CODEX:
                try:
                    _ISOLATED_CODEX[key] = _reported_codex_home(candidate, probe) == home
                except _ProbeFailed as exc:
                    unknown.append(f"the codex at {candidate}: {str(exc).rstrip('.')}")
                    continue
            isolated = _ISOLATED_CODEX[key]
        if isolated:
            return candidate
    if unknown:
        raise Error("API models through Codex need a Codex CLI that honors CODEX_HOME, and TCS Prover could "
                    f"not check {'; '.join(unknown)}. The check is repeated at the next retry.")
    raise SafetyStop(
        "API models through Codex need a Codex CLI that honors CODEX_HOME, but the codex "
        "found on PATH replaces it (for example a launcher script). Put the native Codex "
        "binary on PATH; the API model was not started."
    )


_REGISTERED_SECRETS = set()


def register_secret(value):
    """Remember a key handed to a child so every diagnostic can redact it."""

    if isinstance(value, str) and value:
        _REGISTERED_SECRETS.add(value)


def _secret_forms(minimum=1):
    """Every known secret, also as JSON and URL escapes, longest first."""

    secrets = set(_REGISTERED_SECRETS)
    secrets.update(os.environ.get(name) for name in REDACTED_ENVIRONMENT)
    forms = set()
    for secret in filter(None, secrets):
        if len(secret) >= minimum:
            forms.update((secret, json.dumps(secret)[1:-1], quote(secret, safe="")))
    return sorted(forms, key=len, reverse=True)


def redact(text):
    """Remove registered keys and inherited provider credentials from text."""

    text = str(text)
    for secret in _secret_forms():
        text = text.replace(secret, "[redacted]")
    return text


def redacted(value, secrets=None):
    """Redact every string inside a JSON-like record value (field names stay)."""

    if secrets is None:
        secrets = _secret_forms(MIN_RECORD_SECRET_CHARS)
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {key: redacted(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redacted(item, secrets) for item in value]
    return value


def api_codex_home(workspace=None):
    """A private Codex home for API models of one workspace.

    A process that talks to a third-party URL must never load the user's
    ChatGPT login (nor the ChatGPT connectors it enables), so API models never
    see the user's CODEX_HOME. The home persists per workspace so an API-model
    author thread can be resumed; it lives with the API model store, outside the
    writable run folder.
    """

    workspace = Path(workspace or Path.cwd()).resolve()
    key = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:24]
    try:
        root = providers.private_directory(providers.state_directory())
        return providers.private_directory(root / providers.CODEX_HOMES / key)
    except providers.ProviderError as exc:
        # The folder is not one TCS Prover may use: only the user can fix that.
        raise ConfigurationError(f"Cannot prepare the private Codex home for API models: {exc}") from exc
    except OSError as exc:
        # A full disk or a busy file system: tried again like any failed call.
        raise Error(f"Cannot prepare the private Codex home for API models: {exc}") from exc


def environment(model=None):
    """Use inherited provider credentials with quiet, predictable child logging."""

    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", API_TOKEN_ENV, *OBSOLETE_CREDENTIAL_ENVIRONMENT):
        env.pop(name, None)
    if (model is not None and model_provider(model) == API_PROVIDER
            and providers.harness(model) == providers.CODEX):
        key = providers.api_model_secret(model)
        if key:
            register_secret(key)
            # The custom provider reads this environment value as a Bearer
            # token. The key never appears in process arguments.
            env[API_TOKEN_ENV] = key
            # Some Codex versions require a nonempty OPENAI_API_KEY before they
            # initialize a custom provider. This public placeholder is never a
            # credential; the provider authenticates only with the token.
            env["OPENAI_API_KEY"] = CUSTOM_PROVIDER_LOGIN_PLACEHOLDER
        env["CODEX_HOME"] = str(api_codex_home())
    # Inherited debug logs could bypass the structured-event privacy filter.
    env["RUST_LOG"] = "error"
    env.pop("LOG_FORMAT", None)
    env.pop("RUST_BACKTRACE", None)
    return env


def retrieval_policy(workspace=None, features=()):
    """Read controller policy before any process or model call is allowed."""
    try:
        policy = retrieval_runtime.policy_for_workspace(workspace)
        if policy and set(features) - {"multi_agent", "shell_tool"}:
            raise retrieval_runtime.RetrievalPolicyError("Historical workflow features are limited to the audited default tool set.")
        retrieval_runtime.search_mode(workspace, policy=policy)
        return policy
    except retrieval_runtime.SettingsUnavailable as exc:
        # A momentary read error of the saved settings: retried like a failed model call.
        raise Error(str(exc)) from exc
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise ConfigurationError(str(exc)) from exc


def retrieval_environment(policy, role, model=None, inherited=None, agent_limits=None):
    """The child environment of one Codex process; a web-cutoff process's is checked first (its subagent
    limits, agent_limits(), included)."""
    try:
        if policy and model is not None and model_provider(model) != OPENAI_PROVIDER:
            raise retrieval_runtime.RetrievalPolicyError(HISTORICAL_CODEX_ONLY)
        return retrieval_runtime.preflight(
            policy, role, retrieval_runtime.codex_executable(policy) if policy else "",
            environment(model) if inherited is None else inherited,
            **({"agent_limits": agent_limits} if agent_limits else {}),
        )
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise ConfigurationError(str(exc)) from exc


def structured_workspace():
    """Use one stable, empty cwd so volatile temp paths do not split prefixes."""

    STRUCTURED_WORKSPACE.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        STRUCTURED_WORKSPACE.chmod(0o700)
    except OSError:
        pass
    return STRUCTURED_WORKSPACE


def emit_cache_usage(stage, usage, label="Cache usage"):
    """Expose cache effectiveness without changing a model request."""

    if not isinstance(usage, dict):
        return
    input_tokens = usage.get("input_tokens", usage.get("inputTokens", 0))
    cached_tokens = usage.get(
        "cached_input_tokens", usage.get("cachedInputTokens", 0),
    )
    cache_write_tokens = usage.get(
        "cache_write_input_tokens", usage.get("cacheWriteInputTokens", 0),
    )
    try:
        input_tokens = int(input_tokens or 0)
        cached_tokens = int(cached_tokens or 0)
        cache_write_tokens = int(cache_write_tokens or 0)
    except (TypeError, ValueError):
        return
    if input_tokens <= 0:
        return
    hit_rate = min(100.0, max(0.0, cached_tokens * 100.0 / input_tokens))
    emit(
        "status", stage, label=label,
        text=(
            f"{cached_tokens:,} of {input_tokens:,} input tokens cached "
            f"({hit_rate:.1f}%); {cache_write_tokens:,} cache-write tokens."
        ),
        inputTokens=input_tokens, cachedInputTokens=cached_tokens,
        cacheWriteInputTokens=cache_write_tokens, cacheHitPercent=hit_rate,
    )


def configure_standard_streams():
    """Use UTF-8 for the machine-readable CLI protocol on every platform."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def stop_process(process):
    """Stop a child and every process it started, escalating only when termination is ignored.

    The child is frozen while everything it started is killed (parents before
    children, as _stop_command_tree does), so no helper outlives it holding
    its pipes; then it gets SIGTERM and five seconds to exit before its
    remaining tree is killed. Windows ends the tree at once.
    """

    if process.poll() is not None:
        return
    if os.name == "nt":
        _stop_command_tree(process)
        process.wait()
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            # Frozen, the child starts nothing new while its descendants are listed.
            process.send_signal(signal.SIGSTOP)
            _stop_descendants(pid)
        except OSError:
            pass
        finally:
            try:
                process.send_signal(signal.SIGCONT)
            except OSError:
                pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_command_tree(process)
        process.wait()


def emit(kind, stage, **fields):
    """Write one machine-readable transcript record for the web UI.

    Every string field is redacted first: a gateway error, a command's output
    or a model message may echo a key the runner handed to a CLI.
    """

    record = json.dumps(
        {"kind": kind, "stage": stage, **redacted(fields)}, ensure_ascii=False,
    )
    with EMIT_LOCK:
        print(record, flush=True)


def public_event(value):
    """Keep observable events but remove unavailable private reasoning content."""

    if isinstance(value, list):
        return [public_event(item) for item in value]
    if not isinstance(value, dict):
        return value
    reasoning = value.get("type") == "reasoning"
    result = {
        key: public_event(item)
        for key, item in value.items()
        if key not in {"encrypted_content", "encryptedContent"}
        and not (reasoning and key == "content")
    }
    # Some providers expose raw reasoning deltas; never retain those events.
    if result.get("method") == "item/reasoning/textDelta":
        return None
    return result


class StructuredAttemptTimeout(Error):
    """The watchdog stopped one structured attempt.

    ``seconds`` is the inactivity limit of an attempt whose CLI wrote nothing
    for that long; None for an attempt that reached the workflow deadline.
    """

    def __init__(self, message, seconds=None):
        super().__init__(message)
        self.seconds = seconds


# The clock of retry waits and the workflow budget, and the wall clock of
# retry times shown to the user (tests replace all three).
_now = time.monotonic
_wall_clock = time.time


def _retry_sleep(seconds, cancel=None):
    """Sleep one slice of a retry wait; a cancelled sibling request wakes it at once."""

    if cancel is not None:
        cancel.wait(seconds)
    else:
        time.sleep(seconds)


def _duration_text(seconds):
    seconds = float(seconds)
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g} min"
    return f"{seconds:g} s"


# Who makes the model call of each built-in stage, in the words the job page uses.
_STAGE_ROLES = {"review": "statement reviewer", "solve": "proof author", "repair": "proof author",
                "conversation": "proof author", "critic": "critic", "final": "LaTeX editor"}


def _call_name(stage):
    """ "the model call of the critic" for a known stage, else "the model call"."""

    role = _STAGE_ROLES.get(stage) or (stage if stage and stage != "model" else "")
    return f"the model call of the {role}" if role else "the model call"


def _emit_budget_clock(stage, change):
    emit("status", stage, label="Workflow clock", budgetClock=change,
         text=("Every model call in progress is waiting to retry, so the workflow time budget is paused."
               if change == "paused" else "A model call is running again; the workflow time budget continues."))


class RetryClock:
    """The retry waits of one workflow run, kept out of its time budget.

    The budget pauses only while every model call in progress waits to retry
    (another request that still runs keeps it going). Each change is
    reported as budgetClock "paused"/"running" on a status event, so the
    controller leaves the same intervals out of the job's active time.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._calls = self._waiting = 0
        self._since = None
        self._excluded = 0.0

    def excluded(self):
        """Seconds spent waiting to retry so far, including a wait in progress."""

        with self._lock:
            ongoing = max(0.0, _now() - self._since) if self._since is not None else 0.0
            return self._excluded + ongoing

    def _update(self):
        paused = self._calls > 0 and self._waiting >= self._calls
        if paused and self._since is None:
            self._since = _now()
            return "paused"
        if not paused and self._since is not None:
            self._excluded += max(0.0, _now() - self._since)
            self._since = None
            return "running"
        return None

    @contextmanager
    def call(self, stage):
        """One model call in progress, attempting or waiting to retry."""

        with self._lock:
            self._calls += 1
            change = self._update()
        if change:
            _emit_budget_clock(stage, change)
        try:
            yield self
        finally:
            with self._lock:
                self._calls -= 1
                change = self._update()
            if change:
                _emit_budget_clock(stage, change)

    def wait_started(self):
        with self._lock:
            self._waiting += 1
            return self._update()

    def wait_ended(self):
        with self._lock:
            self._waiting -= 1
            return self._update()


# Model calls made without workflow options (the statement reviewer).
_DEFAULT_RETRY_CLOCK = RetryClock()


def retry_clock(options):
    """The RetryClock shared by every model call of one workflow run."""

    if options is None:
        return _DEFAULT_RETRY_CLOCK
    clock = options.get("_retry_clock")
    if not isinstance(clock, RetryClock):
        clock = options["_retry_clock"] = RetryClock()
    return clock


# Plain words for the provider failures that interrupt runs most often.
_FAILURE_PATTERNS = (
    (r"context window|context_length_exceeded|context length|prompt is too long|maximum context|"
     r"input is too long|too many input tokens|exceeds the context", "the conversation is too long for the model"),
    (r"not installed or is not on path", "the CLI is not available"),
    (r"capacity|overload|\b529\b|\b503\b|service unavailable", "model at capacity"),
    (r"usage ?limit|usagelimited|usage_limit|budgetlimited|insufficient_quota|quota", "usage limit reached"),
    (r"rate ?limit|\b429\b|too many requests", "rate limited"),
    (r"\b401\b|\b403\b|unauthori[sz]ed|authenticat|\blog ?in\b|api key|forbidden|credential", "authentication failed"),
    (r"stream disconnected|disconnect|connection|network|reset by peer|broken pipe|unreachable|\beof\b",
     "connection lost"),
    (r"empty or malformed|malformed response|incomplete response|no_events", "incomplete provider response"),
    (r"\b50[0248]\b|internal server error|bad gateway|gateway timeout|server[_ ]error", "provider server error"),
    (r"timed out|timeout", "the request timed out"),
    (r"stopped unexpectedly|exited with code|exited before|without diagnostics", "the CLI stopped unexpectedly"),
)


# Lines that never state the cause: those the runner adds to a CLI's failure
# text, and an argument parser's usage hints.
_NO_CAUSE_LINE = re.compile(r"(?:(?:Codex|Claude) exited with code -?\d+|API status \d+|Claude reported [\w-]+|"
                            r"Codex app-server stopped unexpectedly)\.|Usage: .*|For more information, try .*")


def _failure_reason(text, limit=300):
    """The provider's own message on one short line: the line that names the cause, else the newest one.

    The runner's prefix and the lines it adds itself (an exit code, an API
    status) are left out, so a cause such as a usage limit that the CLI
    reported after a generic error is the one shown. Without such a line, a
    CLI's own error line comes before its newest line.
    """

    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if lines:
        lines[0] = re.sub(r"^(?:(?:Codex|Claude) [\w-]+ failed|(?:Claude )?[Aa]uthor turn failed|"
                          r"(?:Author|Claude) reply turn failed|Author stopped|"
                          r"Could not resume the saved (?:Codex|Claude) conversation; no replacement was started\.)"
                          r"\s*:?\s*", "", lines[0]).strip()
    lines = [line for line in lines if line]
    candidates = [line for line in lines if not _NO_CAUSE_LINE.fullmatch(line)] or lines
    lowered = str(text or "").lower()
    cause = next((pattern for pattern, _ in _FAILURE_PATTERNS if re.search(pattern, lowered)), None)
    value = next((line for line in candidates if cause and re.search(cause, line.lower())), None)
    if value is None:
        value = next((line for line in reversed(candidates) if re.match(r"(?:error|fatal)\b", line, re.IGNORECASE)
                      or _CLI_PROBLEM_TEXT.search(line.lower())), candidates[-1] if candidates else "")
    return _clipped(value, limit)


def _plain_failure(text):
    """The plain words of _FAILURE_PATTERNS for a provider message, or "" for an unfamiliar one."""

    lowered = str(text or "").lower()
    return next((plain for pattern, plain in _FAILURE_PATTERNS if re.search(pattern, lowered)), "")


def _subagent_summary(failures):
    """("2 subagent calls", "model at capacity; connection lost") for {subagent: provider message}."""

    reasons = []
    for message in failures.values():
        reason = _plain_failure(message) or _failure_reason(redact(message), 160)
        if reason and reason not in reasons:
            reasons.append(reason)
    count = len(failures)
    return f"{count} subagent call{'s' if count != 1 else ''}", _clipped("; ".join(reasons), 400)


def describe_failure(error):
    """A plain description of one failed model-call attempt, and the provider's redacted reason."""

    if isinstance(error, InvalidAnswer):
        return error.problem, _failure_reason(redact(error.detail)) if error.detail else ""
    if isinstance(error, SubagentFailure):
        message = next((message for message in error.failures.values() if str(message or "").strip()), "")
        return error.plain, _failure_reason(redact(message))
    if isinstance(error, StructuredAttemptTimeout):
        # The watchdog's own words; no provider message to quote.
        return (f"no output for {_duration_text(error.seconds)}" if error.seconds
                else "the workflow time limit was reached"), ""
    text = redact(str(error) or type(error).__name__)
    reason = _failure_reason(text)
    return _plain_failure(text) or reason or type(error).__name__, reason


# How a CLI that cannot start, or that rejects how it was started, says so
# (shells, and the clap and commander argument parsers of Codex and Claude).
_CLI_PROBLEM_TEXT = re.compile(r"not installed or is not on path|command not found|exited with code 127|"
                               r"unexpected argument|unrecognized (?:option|argument|subcommand)|unknown option|"
                               r"invalid value .{0,80} for '--")


def _cli_problem(error):
    """ "Codex" or "Claude" when a failed attempt could not start that CLI, else "".

    The transports mark a CLI that ended without writing any output
    (``cli_problem``); other failures are recognized by their wording.
    """

    named = getattr(error, "cli_problem", "")
    text = str(error).lower()
    if not named and not isinstance(error, CLIUnavailable) and not _CLI_PROBLEM_TEXT.search(text):
        return ""
    if _plain_failure(text) not in ("", "the CLI is not available", "the CLI stopped unexpectedly"):
        return ""  # The CLI ran and reported a provider failure, such as a login or a usage limit.
    return named or ("Claude" if "claude" in text else "Codex")


def _retry_jitter(seconds):
    """A few random extra seconds (at most MODEL_RETRY_JITTER_SECONDS, and a twelfth of the wait)."""

    return random.uniform(0.0, max(0.0, min(float(MODEL_RETRY_JITTER_SECONDS), seconds / 12)))


def _span_text(seconds):
    """A rounded duration in words: "45 s", "6 min", "2 h 5 min", "3 days 4 h"."""

    seconds = max(0, int(round(float(seconds))))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} days {hours} h" if hours else f"{days} days"


def wait_before_retry(options, stage, attempt, error, *, cancel=None, node=None, thread_id=None,
                      activity=None, since=None, record=None):
    """Wait MODEL_RETRY_SECONDS (plus a few seconds of jitter) before attempt `attempt` of a failed model call.

    The job stays running and a status event says why and how long the call
    has been failing (`since`, a time.time() value); a CLI that failed to
    start in the same way CLI_PROBLEM_REPEATS times in a row is named as
    missing or incompatible. The wait does not consume the workflow time
    budget (RetryClock). It ends at once on Stop (KeyboardInterrupt) or when
    a sibling request cancels the step; the pause file pauses the workflow,
    and a time limit lowered meanwhile ends it. `record` holds more fields of
    its status events (a conversation turn's conversationId).
    """

    plain, reason = describe_failure(error)
    nominal = max(0.0, float(MODEL_RETRY_SECONDS))
    seconds = nominal + _retry_jitter(nominal)
    clock = retry_clock(options)
    where = {**({"node": node} if node else {}), **({"activityLabel": activity} if activity else {}),
             **(record or {})}
    label = f"{activity} · {RETRY_LABEL}" if activity else RETRY_LABEL
    invalid = isinstance(error, InvalidAnswer)
    subagents = isinstance(error, SubagentFailure)
    # Identical failures to start one CLI, each at most a few minutes after the one before.
    cli, now = _cli_problem(error), _now()
    streaks = options.setdefault("_cli_problems", {}) if options is not None else {}
    key = (stage, node or "", activity or "")
    previous = streaks.get(key)
    repeats = (previous[1] + 1 if cli and previous and previous[0] == (cli, reason)
               and now - previous[2] <= 5 * max(nominal, 60) else 1 if cli else 0)
    streaks[key] = ((cli, reason), repeats, now)
    missing = repeats >= CLI_PROBLEM_REPEATS
    if missing:
        plain = f"{cli} CLI missing or incompatible"
    details = [f"attempt {attempt}"]
    spent = max(0.0, _wall_clock() - since) if since is not None else 0.0
    if spent >= 60:
        details.append(f"retrying for {_span_text(spent)}")
    text = (f"The model's answer could not be accepted ({plain})" if invalid
            else f"The answer was set aside because {plain}" if subagents
            else plain if missing
            else f"Model call failed ({plain})") + f" — retrying in {_duration_text(nominal)} ({', '.join(details)})."
    if reason and reason != plain:
        text += f" {'Detail' if invalid else 'CLI message' if missing else 'Provider message'}: {reason}"
    said = _explanation_excerpt(getattr(error, "raw", "")) if invalid else ""
    if said:
        text += f" The model said: {said}"
    change = clock.wait_started()
    emit("status", stage, label=label, text=text, retryAttempt=attempt, retryInSeconds=nominal,
         retryAt=_wall_clock() + seconds, failure=plain, failureReason=reason,
         failureKind="invalid_answer" if invalid else "subagent" if subagents else "cli" if missing
         else "timeout" if isinstance(error, StructuredAttemptTimeout) else "model_call", **({"retryingSeconds": round(spent)} if since is not None else {}),
         **({"budgetClock": change} if change else {}), **where)
    pause_file = (options or {}).get("goal_pause_file")
    finished = False
    try:
        waited = 0.0
        while True:
            if pause_file and Path(pause_file).is_file():
                raise WorkflowPaused("Paused while waiting to retry the model call.", thread_id=thread_id)
            if cancel is not None and cancel.is_set():
                raise RetryCancelled("Another request of this step ended it while this one waited to retry.")
            if options is not None and workflow_remaining(options) <= 0:
                raise Error("Workflow time limit reached; saved work remains resumable.")
            if waited >= seconds:
                break
            step = min(RETRY_POLL_SECONDS, seconds - waited)
            _retry_sleep(step, cancel)
            waited += step
        finished = True
    finally:
        change = clock.wait_ended()
        if finished:
            emit("status", stage, label=label, text=f"Retrying {_call_name(stage)} now (attempt {attempt}).",
                 retryAttempt=attempt, **({"budgetClock": change} if change else {}), **where)
        elif change:
            _emit_budget_clock(stage, change)


def pending_author_steer(path, delivered_id=None):
    """The next controller-authored live instruction to deliver, as (id, instruction), or None.

    The controller appends every instruction to one queue file,
    {"queue": [{"id", "instruction", "createdAt"}, ...]} (an older file holding
    one {"id", "instruction"} object is a queue of one). Instructions are
    delivered one at a time and in order: ``delivered_id`` is the newest one
    the conversation accepted, so the entry after it comes next, or the first
    entry when it is no longer queued (the controller drops accepted ones).
    """

    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    entries = value["queue"] if isinstance(value, dict) and isinstance(value.get("queue"), list) else [value]
    commands = []
    for entry in entries:
        try:
            command_id = str(entry["id"]).strip()
            instruction = str(entry["instruction"]).strip()
        except (KeyError, TypeError, IndexError):
            continue
        if command_id and instruction and "\0" not in instruction and len(instruction) <= AUTHOR_STEER_MAX_CHARS:
            commands.append((command_id, instruction))
    identities = [command[0] for command in commands]
    if delivered_id in identities:
        commands = commands[identities.index(delivered_id) + 1:]
    return commands[0] if commands else None

def model_provider(model):
    """Return the provider id of one supported model (see OPENAI_PROVIDER)."""

    kind = providers.provider_kind(chosen_model(model))
    if kind == providers.API:
        return API_PROVIDER
    return CLAUDE_PROVIDER if kind == providers.CLAUDE else OPENAI_PROVIDER

def wire_model(model):
    """The model name a CLI sends to its endpoint (an API model's configured name)."""

    return providers.wire_model(chosen_model(model))

def toml_string(value):
    """Quote text as a TOML basic string for a Codex -c override."""

    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")

def codex_home_folder(environment=None):
    """The CODEX_HOME of a Codex process started with ``environment`` (None: this process's): its CODEX_HOME
    variable, else ~/.codex."""

    environment = os.environ if environment is None else environment
    user_home = Path(environment.get("HOME") or Path.home())
    configured_home = environment.get("CODEX_HOME")
    if configured_home == "~" or (configured_home or "").startswith("~/"):
        return user_home / configured_home[2:]
    return Path(configured_home).expanduser() if configured_home else user_home / ".codex"

def inherited_openai_base_url_arguments(overrides=None, environment=None):
    """Inherit only the built-in OpenAI URL while ignoring other user settings."""
    selected = dict(overrides or {})
    explicit_openai = any(
        key == "model_providers.openai" or key.startswith("model_providers.openai.")
        for key in selected
    )
    providers = selected.get("model_providers")
    explicit_openai |= isinstance(providers, dict) and "openai" in providers
    if (selected.get("model_provider", "openai") != "openai"
            or "openai_base_url" in selected or explicit_openai):
        return []
    effective = {**os.environ, **(environment or {})}
    if effective.get("OPENAI_BASE_URL", "").strip() or tomllib is None:
        return []
    home = codex_home_folder(effective)
    try:
        with (home / "config.toml").open("rb") as stream:
            value = tomllib.load(stream).get("openai_base_url")
    except (OSError, ValueError):
        return []
    if not isinstance(value, str) or not value.strip():
        return []
    quoted = json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    return ["-c", "openai_base_url=" + quoted]


def provider_arguments(model):
    """Define an isolated custom provider for an OpenAI-format API model.

    The endpoint and request source come from the API model store; the key stays in
    the child environment (environment()). Built-in models need no flags.
    """

    if model_provider(model) != API_PROVIDER or providers.harness(model) != providers.CODEX:
        return []
    entry = providers.api_model(model)
    # No forced_login_method: Codex enforces it by deleting a stored ChatGPT
    # login, which a launcher that resets CODEX_HOME would expose (verified
    # with Codex 0.160). The provider authenticates only with the variable
    # below; requires_openai_auth=false keeps any OpenAI login out of
    # its requests.
    # The context window the user set for this endpoint (Codex otherwise assumes
    # a fallback for a model it does not know), compacted early enough to leave
    # room for the max output. Codex 0.160 has no setting for the output limit.
    window, output = entry.get("contextWindow"), entry.get("maxOutputTokens")
    limits = ["-c", f"model_context_window={int(window)}",
              "-c", f"model_auto_compact_token_limit={int((window - (output or 0)) * 0.8)}"] if window else []
    return [
        *limits,
        "-c", f'model_provider="{API_PROVIDER}"',
        "-c", f'model_providers.{API_PROVIDER}.name="TCS Prover"',
        "-c", f"model_providers.{API_PROVIDER}.base_url={toml_string(entry['baseUrl'].rstrip('/'))}",
        "-c", f'model_providers.{API_PROVIDER}.wire_api="responses"',
        "-c", f"model_providers.{API_PROVIDER}.supports_websockets=false",
        "-c", f"model_providers.{API_PROVIDER}.requires_openai_auth=false",
        # Codex sends this variable as "Authorization: Bearer <key>".
        "-c", f'model_providers.{API_PROVIDER}.env_key="{API_TOKEN_ENV}"',
        "-c", f'model_providers.{API_PROVIDER}.http_headers={{"User-Agent"={toml_string(providers.request_source(entry))}}}',
        # The model's shell commands never inherit the key: by default Codex
        # 0.160 passes *KEY*/*TOKEN* variables to them (verified with a mock
        # endpoint). Both settings drop it; either alone suffices.
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        "-c", f'shell_environment_policy.exclude=["{API_TOKEN_ENV}","OPENAI_API_KEY"]',
    ]


def live_network(workspace=None, policy=None):
    """Whether the model processes of a run reach the internet: only in a live run (search mode "live").

    Network follows the search mode and nothing else, as native search does
    for the same ``workspace`` (the author's run folder; None, the current
    folder, for a structured call): a web-cutoff run, with a historical
    ``policy`` or "disabled" (training knowledge only), keeps every process
    offline, whatever the user's Codex settings say.
    """

    if policy:
        return False
    try:
        return retrieval_runtime.search_mode(workspace) == "live"
    except retrieval_runtime.SettingsUnavailable as exc:
        # A momentary read error of the saved settings: retried like a failed model call.
        raise Error(str(exc)) from exc
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise ConfigurationError(str(exc)) from exc


def legacy_network_arguments(live):
    """The network setting of Codex's legacy workspace-write mode, where private_permissions() is None.

    It follows live_network(), not the user's [sandbox_workspace_write]
    settings. The legacy read-only mode of `codex exec` has no network
    setting (Codex 0.160: no [sandbox_read_only] key, no flag): a read-only
    structured call (the statement reviewer, an independent critic, the
    final verification) stays offline there. The app-server's read-only
    mode has one, which a conversation turn sets (legacy_read_only_policy()).
    """

    return ["-c", f"sandbox_workspace_write.network_access={str(bool(live)).lower()}"]


def legacy_read_only_policy(live):
    """The sandbox policy of a conversation turn in Codex's legacy read-only mode, where private_permissions() is None.

    Codex 0.160's app-server takes it as turn/start's "sandboxPolicy" (for
    that turn and the later ones of the same app-server), and its read-only
    policy has a network switch, which follows live_network(): the paused
    author's replies and its time-limit summary reach the internet in a live
    run there too. Verified with Codex 0.160 (command/exec, and turns against
    a local mock model): networkAccess true reaches a public host and still
    writes nothing, false or no policy reaches none, and the author's next
    thread/resume ("workspace-write", in a new app-server) writes again.
    """

    return {"type": "readOnly", "networkAccess": bool(live)}


def private_permissions(base, model, *, codex_home=None, live=False):
    """The Codex permission profile that confines one model process of a run (macOS); None elsewhere.

    `base` ":workspace" lets commands write the workspace (the author, a
    structured call that edits a file), ":read-only" only read it (other
    structured calls, the paused author's conversation turns). Commands can
    read the system runtime (":minimal"), the toolchains that exist
    (confinement.TOOLCHAIN_FOLDERS), Codex's own programs and the
    apply_patch helpers of `codex_home`, and write the private temp folder
    that the process's TMPDIR names (confinement.temp_environment()). Nothing
    else: no other run, the checkout, the home folder, /tmp, /var/tmp or the
    shared $TMPDIR. TCS Prover's private state, and for API models the
    user's CLI logins, stay denied (`model` None is a native model). The
    controller's files in the workspace and Codex's project folders (.codex,
    .agents, .git, .aws) stay read-only, so a run folder cannot plant a
    .codex/config.toml whose commands Codex starts outside the sandbox.

    In a live run (``live``, see live_network()) commands of either base
    reach the internet; the user's [sandbox_workspace_write] settings never
    apply. Codex's network access also lets commands connect to localhost
    and to any Unix socket, even one in a denied folder, and they can list
    the sockets of the user's other processes (lsof): the variables that
    name the user's agents are left out (confinement.AGENT_VARIABLES), but
    their sockets stay reachable (the README says so). Verified with Codex
    0.160's macOS Seatbelt sandbox: other platforms return None and keep the
    legacy sandbox modes (legacy_network_arguments(),
    legacy_read_only_policy(), README).
    """

    if not confinement.supported():
        return None
    api = model is not None and model_provider(model) == API_PROVIDER
    paths = providers.private_paths() + (providers.user_login_files() if api else [])
    # The apply_patch helpers Codex links into $CODEX_HOME/tmp/arg0; a native model's
    # launcher may also use the default home.
    homes = [Path(codex_home)] if codex_home else []
    if not api:
        homes.append(Path.home() / ".codex")
    filesystem = {
        ":minimal": "read", ":slash_tmp": "deny", "/private/var/tmp": "deny", ":tmpdir": "write",
        **{str(path): "read" for path in (*confinement.toolchain_folders(), *confinement.codex_packages())},
        **{os.path.join(os.path.realpath(home), "tmp", "arg0"): "read" for home in homes},
        **{str(path): "deny" for path in dict.fromkeys(paths)},
        # The controller's files in the run folder stay read-only.
        ":workspace_roots": {".": "write" if base == ":workspace" else "read",
                             **{name: "read" for name in confinement.PROTECTED_ENTRIES}},
    }
    profile = {"filesystem": filesystem}
    if live:
        profile["network"] = {"enabled": True}
    return profile


def _toml_inline(value):
    """A permission-profile value as TOML: strings, booleans and nested tables."""

    if isinstance(value, dict):
        return "{" + ", ".join(f"{toml_string(key)}={_toml_inline(item)}" for key, item in value.items()) + "}"
    return str(value).lower() if isinstance(value, bool) else toml_string(value)


def private_permission_arguments(base, model, *, codex_home=None, profile=None, live=False):
    """Flags that define and select private_permissions() (or `profile`); empty where unsupported."""

    profile = private_permissions(base, model, codex_home=codex_home, live=live) if profile is None else profile
    if profile is None:
        return []
    return [
        "-c", f'default_permissions="{PRIVATE_PERMISSIONS}"',
        "-c", f"permissions.{PRIVATE_PERMISSIONS}.filesystem={_toml_inline(profile['filesystem'])}",
        *(["-c", f"permissions.{PRIVATE_PERMISSIONS}.network={_toml_inline(profile['network'])}"]
          if profile.get("network") else []),
    ]


@contextmanager
def _confined_call(writable, model, environment, policy, workspace):
    """The sandbox flags and environment of one `codex exec`, with a fresh private temp folder.

    Yields (flags, environment). On macOS the confined profile and a new
    owner-only temp folder outside the call's workspace, removed when the call
    ends; elsewhere the legacy sandbox mode. Either way the call reaches the
    internet in a live run only (live_network(), as its native search), and
    its environment names neither of the user's SSH and GPG agents
    (confinement.AGENT_VARIABLES). A web-cutoff call (``policy``) keeps its
    audited profile and the private temp folder of its verified environment
    (retrieval_runtime.preflight()), which it removes too.
    """

    if policy:
        try:
            yield retrieval_runtime.temp_arguments(policy, environment), environment
        finally:
            retrieval_runtime.remove_private_temp(policy, environment)
        return
    live = live_network()
    flags = private_permission_arguments(
        ":workspace" if writable else ":read-only", model, codex_home=(environment or {}).get("CODEX_HOME"),
        live=live)
    if not flags:
        legacy = ["-s", "workspace-write", *legacy_network_arguments(live)] if writable else ["-s", "read-only"]
        # With network access a command could use the agents, as with the confined profile.
        yield legacy, confinement.without_agents(environment if environment is not None else os.environ)
        return
    try:
        temp = confinement.fresh_folder(confinement.CALL_TEMP_PREFIX, workspace=workspace)
    except (confinement.ConfinementError, OSError) as exc:
        raise Error(f"Cannot prepare the private temp folder of this call: {exc}") from exc
    try:
        yield flags, confinement.confined_environment(environment if environment is not None else os.environ, temp)
    finally:
        shutil.rmtree(temp, ignore_errors=True)

def require_model_credentials(model):
    """Fail before launching a CLI that is missing or a key that was never saved."""

    model = chosen_model(model)
    if providers.provider_kind(model) == providers.API and not providers.api_model_secret(model):
        raise ConfigurationError(f"Add an API key for {providers.model_label(model)} in Advanced settings → API connections.")
    if providers.harness(model) == providers.CLAUDE and not shutil.which("claude"):
        # Missing for a moment while Claude Code updates itself: a model call retries it.
        raise CLIUnavailable("Claude CLI is not installed or is not on PATH; install Claude Code to use Claude models.")

def verify_model_credentials(model):
    """Check locally that the selected model has its required credential."""

    require_model_credentials(model)

def effective_effort(model, effort):
    """Map the shared effort menu to the levels each model lists.

    Codex does not clamp efforts, so use the highest one the model lists at or
    below the request (Ultra runs as Max on Luna, Claude and API models).
    """

    model = chosen_model(model)
    effort = chosen_effort(effort)
    try:
        return providers.effective_effort(model, effort)
    except providers.ProviderError as exc:
        raise Error(str(exc)) from exc

def chosen_reasoning_summary(value):
    """Require one public reasoning-summary detail level."""

    if value not in REASONING_SUMMARIES:
        raise Error("Choose Status only, Concise summaries, or Detailed summaries.")
    return value

def reasoning_summary(model, requested=DEFAULT_REASONING_SUMMARY):
    """Return the requested public summary level for one supported model."""

    chosen_model(model)
    return chosen_reasoning_summary(requested)

def effective_speed(model, speed):
    """Fast is a ChatGPT service tier; Claude and API models always run standard."""

    speed = chosen_speed(speed)
    return speed if providers.supports_fast(chosen_model(model)) else "standard"

def structured_prompt_for_model(prompt, schema_value, model):
    """Expose the JSON contract when a model lacks schema enforcement."""

    if providers.enforces_output_schema(chosen_model(model)):
        return prompt
    schema = json.dumps(schema_value, ensure_ascii=False, sort_keys=True)
    return (
        f"{prompt}\n\nOUTPUT JSON CONTRACT\n"
        "Return exactly one JSON object and no markdown fences or commentary. "
        "The object must match this JSON Schema exactly:\n"
        f"{schema}"
    )

def decoded_json_object(raw):
    """Decode the final complete JSON object from one model response."""

    value = raw.strip()
    if not value:
        raise Error("The model completed without a final structured response.")
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            value = "\n".join(lines[1:-1]).strip()
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise Error(
                "The model returned a structured value that is not an object."
            )
        return result
    except json.JSONDecodeError:
        pass

    # Prompt-only JSON contracts are not always obeyed byte-for-byte by custom
    # providers. Recover a complete final object from harmless surrounding text
    # while still validating its schema in the caller.
    decoder = json.JSONDecoder()
    objects = []
    offset = 0
    while True:
        start = value.find("{", offset)
        if start < 0:
            break
        try:
            candidate, length = decoder.raw_decode(value[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)
        offset = start + max(length, 1)
    if objects:
        return objects[-1]
    raise Error("The model returned malformed structured JSON.")


def _holds_json_object(raw):
    """Whether one model response holds a complete JSON object (see decoded_json_object)."""

    try:
        decoded_json_object(raw)
    except Error:
        return False
    return True

def validate_json_schema(value, schema, path="$"):
    """Validate model output against the same JSON Schema accepted by workflows."""
    error = next(validator_for(schema)(schema).iter_errors(value), None)
    if error is not None:
        location = path + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
        detail = "contains unsupported properties: " + error.message if error.validator == "additionalProperties" else error.message
        raise Error(f"Structured output field {location}: {detail}")
    return value


def output_schema_arguments(model, schema_path):
    """Use provider enforcement only when the selected model supports it."""

    if not providers.enforces_output_schema(chosen_model(model)):
        return []
    return ["--output-schema", str(schema_path)]

# The delegation hint of a Codex process that may start subagents (multi_agent_mode_hint_text, see
# retrieval_runtime.agent_limits()); its root agent and its subagents read it. The node's prompt states the
# same number.
SUBAGENT_HINT = ("Multi-agent delegation is allowed for this task: the root agent may start up to {count} independent "
                 "subagent{s} at a time without asking the user. Subagents do not start subagents of their own.")


# Codex 0.160's multi-agent version of each model (`codex debug models`, checked with its binary against a local
# stand-in for the model API, no model): version 2 for its built-in models (GPT 6.x, GPT 5.6 Sol and Terra),
# version 1 for models without catalog metadata (API models). GPT 5.6 Luna is listed as version 1 but gets no
# subagent tool with the multi_agent feature (only with multi_agent_v2, which TCS Prover leaves off), so its
# critic reviews alone and its author has no subagents; the run says so (note_no_subagent_tool()).
NO_SUBAGENT_TOOL_MODELS = ("gpt-5.6-luna",)


def multi_agent_version(model):
    """Codex's multi-agent version of ``model``: 2, 1, or 0 for one that gets no subagent tool; None for a model
    that runs through Claude Code."""

    model = chosen_model(model)
    if providers.harness(model) != providers.CODEX:
        return None
    if model in NO_SUBAGENT_TOOL_MODELS:
        return 0
    return 1 if providers.provider_kind(model) == providers.API else 2


def note_unobservable_subagents(stage, model, count, policy):
    """Say that a web-cutoff call shows its subagents of multi-agent version 2 only waiting (see SubagentProgress)."""

    if not count or not policy or multi_agent_version(model) != 2:
        return
    emit("status", stage, label="Independent critics not observable", subagentsObservable=False,
         text="A web-cutoff run keeps no saved session of the critic, and for this model Codex's event stream "
              "shows only that the critic waits for its independent critics: when each one starts and finishes, "
              "and whether one of their model calls failed, is not known, so the critic's answer is used as given.")


def note_no_subagent_tool(stage, model, count):
    """Say that ``model`` works without the ``count`` subagents its setting allows, when Codex gives it none."""

    if not count or multi_agent_version(model) != 0:
        return
    s = "" if count == 1 else "s"
    who = (f"the critic reviews alone, without its {count} independent critic{s}" if stage == "critic"
           else f"the author works without the {count} subagent{s} that Author subagents allows" if stage == "solve"
           else f"this step works without the {count} subagent{s} it may start")
    emit("status", stage, label="No subagents with this model", noSubagentTool=True, subagentCount=count,
         text=f"{providers.model_label(chosen_model(model))} gets no subagent tool from Codex, so {who}. "
              "Choose another model to have them.")


def subagent_limits(features, subagents):
    """The Codex settings that bound one process's subagents (retrieval_runtime.agent_limits()).

    A process without the multi_agent feature gets none: Codex 0.160 still lists its collaboration tools to
    models of multi-agent version 2 with multi_agent off, so a session limit of one thread (the root) refuses
    every spawn. With multi_agent, ``subagents`` (1 or more) is the most that may run at once, which the hint
    states too; None keeps Codex's own limits (a node that declares no number).
    """
    if "multi_agent" not in features:
        return retrieval_runtime.agent_limits(0)
    if subagents is None:
        return {}
    return retrieval_runtime.agent_limits(subagents, SUBAGENT_HINT.format(count=subagents,
                                                                           s="" if subagents == 1 else "s"))


def structured_tool_arguments(stage, features=(), policy=None, subagents=None):
    """Apply the run's search mode; keep other optional tools controlled by YAML, with their subagent limits."""
    return [
        *retrieval_runtime.native_search_arguments(policy=policy),
        "-c", "features.view_image=false",
        *[argument for feature in ("shell_tool", "multi_agent") if feature not in features for argument in ("--disable", feature)],
        *retrieval_runtime.agent_limit_arguments(subagent_limits(features, subagents)),
    ]


def structured_retry_prompt(prompt, raw, schema_value, problem=None):
    """Re-send the exact task after an unacceptable answer that a new conversation cannot see."""

    previous = (raw or "").strip()
    if not previous:
        previous = "(empty: the previous attempt produced no final message)"
    elif len(previous) > STRUCTURED_RETRY_OUTPUT_CHARS:
        half = STRUCTURED_RETRY_OUTPUT_CHARS // 2
        previous = (
            previous[:half]
            + "\n...[middle of previous output omitted]...\n"
            + previous[-half:]
        )
    schema = json.dumps(schema_value, ensure_ascii=False, sort_keys=True)
    reason = (f"The previous attempt's final answer could not be accepted: {problem}. " if problem
              else "The previous attempt did not produce one valid final JSON object. ")
    return (
        f"{prompt}\n\nSTRUCTURED OUTPUT RECOVERY RETRY\n"
        f"{reason}"
        "Do not discuss the formatting failure or return a placeholder. "
        "Re-evaluate the task using the supplied information and return the "
        "complete substantive answer as exactly one JSON object matching this "
        f"schema:\n{schema}\n\nPREVIOUS INVALID OUTPUT:\n{previous}"
    )


def _clip_middle(value, limit):
    value = str(value)
    if len(value) <= limit:
        return value
    half = max(1, limit // 2)
    return f"{value[:half]}\n...[{len(value) - 2 * half} characters omitted]...\n{value[-half:]}"


def _answer_excerpt(raw, limit=RETRY_EXCERPT_CHARS):
    """The model's previous answer, shortened for a retry note (its explanation fields stay readable)."""

    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        value = decoded_json_object(raw)
    except Error:
        value = None
    if isinstance(value, dict):
        raw = json.dumps({key: _clip_middle(item, 1200) if isinstance(item, str) else item
                          for key, item in value.items()}, ensure_ascii=False, indent=2)
    return _clip_middle(raw, limit)


def _explanation_excerpt(raw, limit=400):
    """The model's own words in an answer that was not accepted, on one short line."""

    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        value = decoded_json_object(raw)
    except Error:
        value = None
    if isinstance(value, dict):
        parts = [f"{key}: {' '.join(item.split())[:200]}" for key, item in value.items()
                 if isinstance(item, str) and item.strip()]
        text = "; ".join(parts)
    else:
        text = " ".join(raw.split())
    return _clipped(redact(text), limit)


def _context_window_failure(error):
    """Whether a provider rejected the conversation as longer than the model's context."""

    text = str(error or "").lower()
    return any(marker in text for marker in CONTEXT_WINDOW_MARKERS)


class SubagentProgress:
    """The status records that show a structured call's subagents at work (see SUBAGENTS_REQUESTED_LABEL).

    The transports report what their event streams show (StructuredConversation.subagent()):
    - Claude: each Task (or Agent) tool call starts one subagent, and its tool result ends it;
    - Codex, models with multi-agent version 1 (such as API models): a spawn_agent item names each new
      subagent thread, and the agents_states of later collab items say when one completed or failed;
    - Codex, models with multi-agent version 2 (GPT 6.x, GPT 5.6 Sol and Terra): `codex exec --json` reports
      no item for spawn_agent, only wait_agent calls (collab items "wait" naming no thread; verified with
      Codex 0.160); each subagent's start, end and failure are read from the call's saved session
      (CodexRollout). A web-cutoff call saves no session, so there only "Waiting for the independent
      critics" is observable (see note_unobservable_subagents()).
    Every attempt starts its subagents anew, and so its numbering.
    """

    def __init__(self, stage, count):
        self.stage, self.count = stage, count
        self.started, self.finished, self.waited = [], set(), False

    def _emit(self, label, **fields):
        # Root statuses of the step (no activity label), which the job page keeps for its progress line.
        emit("status", self.stage, label=label, subagentProgress=True, auditCount=self.count, **fields)

    def start_attempt(self, attempt):
        self.started, self.finished, self.waited = [], set(), False
        self._emit(SUBAGENTS_REQUESTED_LABEL.format(count=self.count, s="" if self.count == 1 else "s"),
                   attempt=attempt, text=f"The critic may start up to {self.count} independent "
                                         f"subagent{'' if self.count == 1 else 's'} at a time.")

    def subagent(self, identity, status):
        identity = str(identity)
        if status == "started" and identity not in self.started:
            self.started.append(identity)
            self._emit(SUBAGENT_STARTED_LABEL.format(index=len(self.started)), auditIndex=len(self.started))
        elif status in ("completed", "failed") and identity in self.started and identity not in self.finished:
            self.finished.add(identity)
            index = self.started.index(identity) + 1
            self._emit(SUBAGENT_FINISHED_LABEL.format(index=index), auditIndex=index,
                       **({"failed": True} if status == "failed" else {}))

    def waiting(self):
        if not self.waited:
            self.waited = True
            self._emit(SUBAGENTS_WAITING_LABEL, waiting=True)


class StructuredConversation:
    """The CLI conversations of one structured call, kept across all of its retries.

    Every attempt persists its session (Codex without --ephemeral, Claude with
    --session-id). ``base`` is the newest session that holds model work (an
    item, an assistant message or an answer). A retry forks it (`codex exec
    fork <id>`, `claude --resume <id> --fork-session`) with a short note, so
    the model keeps everything from the failed attempts while the base never
    accumulates notes: a fork that fails without model output is deleted at
    once and the next retry forks the same base again with the same note; a
    fork that produced output becomes the new base. Without a base (no attempt
    produced output yet), when the CLI no longer has it or the conversation
    outgrew the model's context, and under a historical policy (whose
    sessions are never persisted), a retry re-runs the exact saved input in
    a new conversation instead, which loses nothing.

    The transports set ``session_id``, ``confirmed`` (the CLI reported the
    session) and ``output`` (the attempt produced model output), report the
    subagents that failed or finished with subagent(), and register each
    persisted session with created(); discard() deletes them all when the
    call ends. A proof snapshot workspace is kept for every attempt, so a
    forked session finds the same files.
    """

    def __init__(self, resumable=True):
        self.resumable = resumable
        self.base = None
        # Why the attempt that produced the base failed, and the subagents that failed in it.
        self.base_failure, self.base_subagent_failures = None, {}
        # The base that the current attempt forks; None for a new conversation.
        self.fork_from = None
        self.session_id = None
        self.confirmed = False
        self.output = False
        # The subagents that failed in the current attempt: {subagent: provider message}.
        self.subagent_failures = {}
        # The call's SubagentProgress, when it may start subagents.
        self.progress = None
        # The current attempt's full provider messages and CLI stderr, keys removed
        # ({"provider": text, "stderr": text}), for the run's MODEL_FAILURE_LOG.
        self.detail = {}
        self.workspace = None
        self._sessions = []  # [session id, deleter] in creation order
        self._lock = threading.Lock()

    @property
    def resume(self):
        """Whether the current attempt continues earlier work (a fork of the base)."""

        return self.fork_from is not None

    def forget(self):
        """The saved conversation is gone or unusable: the next attempt starts a new one."""

        self.base = self.fork_from = None
        self.base_failure, self.base_subagent_failures = None, {}
        self.session_id, self.confirmed = None, False

    def start_attempt(self):
        self.session_id, self.confirmed, self.output = None, False, False
        self.subagent_failures, self.detail = {}, {}

    def subagent(self, identity, status, message=""):
        """A transport saw subagent ``identity`` start ("started"), fail ("failed", with the provider's message)
        or finish ("completed"), or the call wait for its subagents ("waiting", no identity).

        A subagent that failed and then finished (the model continued it) no
        longer counts as failed.
        """

        with self._lock:
            if status == "failed":
                self.subagent_failures[str(identity)] = str(message or "")
            elif status == "completed":
                self.subagent_failures.pop(str(identity), None)
        if self.progress is not None:
            if status == "waiting":
                self.progress.waiting()
            else:
                self.progress.subagent(identity, status)

    def created(self, session_id, deleter):
        """A transport persisted ``session_id``; ``deleter(session_id)`` removes it again (see discard)."""

        with self._lock:
            if session_id and all(entry[0] != session_id for entry in self._sessions):
                self._sessions.append([session_id, deleter])

    def finish_attempt(self, failure):
        """Keep a failed attempt's session as the new base when it holds model work, else delete it."""

        holds_work = self.output or isinstance(failure, (InvalidAnswer, SubagentFailure))
        if holds_work and self.resumable and self.confirmed and self.session_id:
            self.base = self.session_id
            self.base_failure, self.base_subagent_failures = failure, dict(self.subagent_failures)
        elif self.session_id and self.session_id != self.base:
            self._delete(self.session_id)
        if _context_window_failure(failure):
            # The conversation no longer fits: start over from the exact saved input.
            self.base, self.base_failure, self.base_subagent_failures = None, None, {}

    def request(self, prompt, schema_value, failure=None):
        """The text of the next attempt: the task, or a note when it forks the base conversation.

        A fork carries the note for the attempt that produced its base, also
        after later attempts failed without output (their forks were deleted).
        """

        self.fork_from = self.base if failure is not None and self.resumable and self.base else None
        if failure is None:
            return prompt
        if self.fork_from:
            return self._note(self.base_failure or failure)
        if isinstance(failure, InvalidAnswer):
            return structured_retry_prompt(prompt, getattr(failure, "raw", ""), schema_value, failure.problem)
        return prompt

    def _note(self, failure):
        if isinstance(failure, InvalidAnswer):
            excerpt = _answer_excerpt(getattr(failure, "raw", ""))
            return STRUCTURED_INVALID_NOTE.format(
                problem=failure.problem,
                explanation=f"Your previous answer, shortened:\n{excerpt}\n" if excerpt else "")
        if isinstance(failure, SubagentFailure):
            failed, reasons = _subagent_summary(failure.failures)
            return SUBAGENT_RETRY_NOTE.format(failed=failed, reasons=f" ({reasons})" if reasons else "")
        if isinstance(failure, StructuredAttemptTimeout) and failure.seconds:
            cause = f"was stopped after {_duration_text(failure.seconds)} without activity"
        else:
            cause = "ended early because of a model-call failure"
        note = STRUCTURED_RETRY_NOTE.format(cause=cause)
        if self.base_subagent_failures:
            failed, reasons = _subagent_summary(self.base_subagent_failures)
            note += "\n" + SUBAGENT_RERUN_NOTE.format(failed=failed, reasons=f" ({reasons})" if reasons else "")
        return note

    def _delete(self, session_id):
        with self._lock:
            entry = next((entry for entry in self._sessions if entry[0] == session_id), None)
        if entry is None:
            return True
        try:
            removed = entry[1](session_id) is not False
        except Exception:
            removed = False
        if removed:
            with self._lock:
                if entry in self._sessions:
                    self._sessions.remove(entry)
        return removed

    def discard(self):
        """Delete every session this call persisted, newest first (Codex keeps a base until its forks are gone)."""

        for _ in range(2):
            with self._lock:
                remaining = [entry[0] for entry in reversed(self._sessions)]
            if not remaining:
                return
            for session_id in remaining:
                self._delete(session_id)
        with self._lock:
            left = [entry[0] for entry in self._sessions]
            self._sessions.clear()
        if left:
            emit("diagnostic", "model", text="Could not delete the saved CLI session(s) of a finished structured "
                                            f"call: {', '.join(left)}.")


# Codex 0.160 saves each persisted session in $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl
# (checked with its binary against a local stand-in for the model API, no model). For the root agent of a model
# of multi-agent version 2, which `codex exec --json` shows only waiting, that file has what the stream lacks:
# - each subagent's start and end: event_msg item_completed records of a SubAgentActivity item, kind "started"
#   or "completed", with agent_thread_id and agent_path;
# - each subagent's last message to the root: a response_item agent_message from its agent_path to "/root",
#   "Message Type: FINAL_ANSWER ... Payload:\n<report>", whose report is "Agent errored: <provider message>
#   \n\nThis agent's turn failed. ..." when its model call failed (no SubAgentActivity "completed" follows).
# A fork's file holds only the fork's own records (it names the session it forked).
ROLLOUT_POLL_SECONDS = 2
SUBAGENT_ERRORED_PREFIX = "Agent errored:"


class CodexRollout:
    """Follows the saved session of one persisted Codex call for what its subagents do (see ROLLOUT_POLL_SECONDS).

    read() reports each new complete record to ``conversation.subagent()``;
    follow() reads every ROLLOUT_POLL_SECONDS in a thread until stop(),
    which reads once more and says whether the file was found.
    """

    def __init__(self, home, session_id, conversation):
        self.home, self.session_id, self.conversation = Path(home), session_id, conversation
        self.path, self.offset, self.partial = None, 0, b""
        self.threads = {}  # agent path -> thread id
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def _find(self, everywhere):
        sessions = self.home / "sessions"
        pattern = f"rollout-*-{self.session_id}.jsonl"
        try:
            # Its day folder is the local date it started: today's, or yesterday's just after midnight.
            for day in dict.fromkeys(time.strftime("%Y/%m/%d", time.localtime(time.time() - shift))
                                     for shift in (0, 86400)):
                found = sorted((sessions / day).glob(pattern))
                if found:
                    return found[0]
            return next(iter(sorted(sessions.glob(f"*/*/*/{pattern}"))), None) if everywhere else None
        except OSError:
            return None

    def read(self, everywhere=False):
        """Report the records written since the last read; whether the file was found."""

        with self._lock:
            if self.path is None:
                self.path = self._find(everywhere)
                if self.path is None:
                    return False
            try:
                with open(self.path, "rb") as stream:
                    stream.seek(self.offset)
                    data = stream.read()
            except OSError:
                return True
            self.offset += len(data)
            lines = (self.partial + data).split(b"\n")
            self.partial = lines.pop()
            for line in lines:
                self._record(line)
            return True

    def _record(self, line):
        try:
            record = json.loads(line)
        except ValueError:
            return
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            return
        item = payload.get("item")
        if (record.get("type") == "event_msg" and payload.get("type") == "item_completed" and isinstance(item, dict)
                and item.get("type") == "SubAgentActivity"):
            thread, path, kind = item.get("agent_thread_id"), item.get("agent_path"), item.get("kind")
            if not isinstance(thread, str) or not thread:
                return
            if isinstance(path, str):
                self.threads[path] = thread
            if kind in ("started", "completed"):
                self.conversation.subagent(thread, kind)
        elif (record.get("type") == "response_item" and payload.get("type") == "agent_message"
              and payload.get("recipient") == "/root" and isinstance(payload.get("author"), str)):
            text = "".join(part["text"] for part in payload.get("content") or ()
                           if isinstance(part, dict) and isinstance(part.get("text"), str))
            if not text.startswith("Message Type: FINAL_ANSWER"):
                return
            report = text.partition("\nPayload:\n")[2]
            identity = self.threads.get(payload["author"], payload["author"])
            if report.startswith(SUBAGENT_ERRORED_PREFIX):
                message = report[len(SUBAGENT_ERRORED_PREFIX):].split("\n\nThis agent's turn failed")[0].strip()
                self.conversation.subagent(identity, "failed", message)
            else:
                self.conversation.subagent(identity, "completed")

    def follow(self):
        def poll():
            while not self._stop.wait(ROLLOUT_POLL_SECONDS):
                self.read()

        self._thread = threading.Thread(target=poll, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return self.read(everywhere=True)


# Codex 0.160's longest wait of a root agent for its subagents (wait_agent's timeout_ms: at most 3,600,000 ms for
# both multi-agent versions). `codex exec --json` writes nothing while it waits, so while a wait is open the
# stall watchdog allows this much more silence.
CODEX_LONGEST_WAIT_SECONDS = 3600


class RequestMonitor:
    """Stall watchdog, liveness heartbeat and output readers of one structured CLI request.

    Shared by the Codex and Claude transports. start() arms the watchdog and
    the heartbeat before the prompt is written, so a CLI that never reads a
    long prompt is stopped too. Every line the CLI writes counts as activity:
    the watchdog stops the CLI only after `timeout` seconds without any
    (STRUCTURED_STALL_SECONDS by default), CODEX_LONGEST_WAIT_SECONDS more
    while its root agent waits for its subagents (waiting()), or after
    `deadline` seconds (the workflow time limit). lines() yields the CLI's
    output without waiting for a process it started that keeps the output
    open after it ended; close() always stops the CLI and everything it
    started.
    """

    def __init__(self, process, stage, model, effort, *, timeout, activity_label, cli="Codex", redact=None,
                 deadline=None):
        self.process, self.stage, self.model, self.effort = process, stage, model, effort
        self.timeout = float(STRUCTURED_STALL_SECONDS if timeout is None else timeout)
        self.deadline = None if deadline is None else float(deadline)
        self.activity_label, self.cli = activity_label, cli
        self.redact = redact or (lambda value: value)
        self.timed_out = threading.Event()
        # The watchdog stopped the CLI because it wrote nothing for `quiet_seconds` (else: the deadline).
        self.stalled = False
        self.quiet_seconds = self.timeout
        # The root agent's open waits for its subagents (their item ids).
        self._waits = set()
        # Whether the CLI wrote any output line at all (none: it failed to start).
        self.output_seen = False
        self.stderr_lines = deque(maxlen=50)
        # All of stderr for the failure log: the first lines, then the newest (see stderr_text()).
        self._stderr_head, self._stderr_omitted = [], 0
        self._stderr_tail = deque(maxlen=STDERR_LOG_LINES)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at = self._active_at = time.monotonic()
        self._progress = {"lastEventAt": self._started_at, "eventCount": 0, "contentEventCount": 0}
        self._lines = queue.Queue()
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        self._heartbeat = threading.Thread(target=self._relay_heartbeat, daemon=True)
        self._readers = (threading.Thread(target=self._read_stdout, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True))
        for reader in self._readers:
            reader.start()

    def _watch(self):
        while True:
            now = time.monotonic()
            with self._lock:
                limit = self.timeout + (CODEX_LONGEST_WAIT_SECONDS if self._waits else 0)
                quiet = limit - (now - self._active_at)
            left = math.inf if self.deadline is None else self._started_at + self.deadline - now
            if min(quiet, left) <= 0:
                if self.process.poll() is None:
                    self.stalled = left > 0
                    self.quiet_seconds = limit
                    self.timed_out.set()
                    stop_process(self.process)
                return
            if self._stop.wait(min(quiet, left)):
                return

    def _relay_heartbeat(self):
        while not self._stop.wait(STRUCTURED_HEARTBEAT_SECONDS):
            now = time.monotonic()
            with self._lock:
                quiet = now - self._progress["lastEventAt"]
                count = self._progress["eventCount"]
                content_count = self._progress["contentEventCount"]
                retrying = self._progress.get("retrying")
            elapsed = now - self._started_at
            label = self.activity_label or f"{self.stage.title()} model request"
            alive = self.process.poll() is None
            emit(
                "status", self.stage,
                label=f"{label} local request is alive",
                text=(
                    f"Verified local {self.cli} request process {self.process.pid} "
                    f"is {'alive' if alive else 'not running'} after "
                    f"{elapsed:.0f}s. The provider stream has returned "
                    f"{count} public lifecycle event"
                    f"{'s' if count != 1 else ''} and {content_count} "
                    f"content event{'s' if content_count != 1 else ''}; "
                    f"last public event {quiet:.0f}s ago. "
                    + (f"The CLI is retrying the provider request itself: {retrying}." if retrying else
                       "If content is still zero, the selected provider may be computing "
                       "or queued server-side; its API does not expose which.")
                ),
                heartbeat=True, elapsedSeconds=round(elapsed, 1),
                quietSeconds=round(quiet, 1), publicEventCount=count,
                contentEventCount=content_count, processAlive=alive,
                requestPid=self.process.pid, model=self.model,
                modelProvider=model_provider(self.model),
                reasoningEffort=self.effort,
            )

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                now = time.monotonic()
                with self._lock:
                    self._active_at = now
                if not self._stop.is_set():  # After close() nobody reads them.
                    self._lines.put((now, line))
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)
            _close_quietly(self.process.stdout)

    def _read_stderr(self):
        try:
            for stderr_line in self.process.stderr:
                value = stderr_line.strip()
                if not value:
                    continue
                value = self.redact(value)
                with self._lock:
                    self.stderr_lines.append(value)
                    if len(self._stderr_head) < STDERR_LOG_LINES:
                        self._stderr_head.append(value)
                        continue
                    if len(self._stderr_tail) == self._stderr_tail.maxlen:
                        self._stderr_omitted += 1  # Its oldest line drops out.
                    self._stderr_tail.append(value)
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(self.process.stderr)

    def lines(self):
        """The CLI's output lines, until its output ends or CLI_OUTPUT_GRACE_SECONDS after the CLI ended."""

        ended = None
        while True:
            try:
                item = self._lines.get(timeout=0.2)
            except queue.Empty:
                item = False
            if item is None:
                return
            if ended is None and self.process.poll() is not None:
                ended = time.monotonic()
            if item is False:
                if ended is not None and time.monotonic() - ended >= CLI_OUTPUT_GRACE_SECONDS:
                    return  # A process the CLI started still holds its output open.
                continue
            read_at, line = item
            if ended is not None and read_at - ended > CLI_OUTPUT_GRACE_SECONDS:
                return
            self.output_seen = True
            yield line

    def stderr_text(self):
        """All of the CLI's stderr (keys removed); a very long one keeps its first and newest lines."""

        with self._lock:
            omitted = [f"[{self._stderr_omitted} more lines omitted]"] if self._stderr_omitted else []
            return "\n".join((*self._stderr_head, *omitted, *self._stderr_tail))

    def stopped(self, stage):
        """The StructuredAttemptTimeout of an attempt the watchdog stopped, else None."""

        if not self.timed_out.is_set():
            return None
        if not self.stalled:
            return StructuredAttemptTimeout(f"The {stage} model call reached the workflow time limit.")
        return StructuredAttemptTimeout(f"The {stage} model wrote no output for {_duration_text(self.quiet_seconds)}; "
                                        "the attempt was stopped.", self.quiet_seconds)

    def waiting(self, item, open_):
        """The root agent started (``open_``) or ended its wait ``item`` for its subagents."""

        with self._lock:
            if open_:
                self._waits.add(item)
            else:
                self._waits.discard(item)

    def retrying(self, text):
        """The CLI reported that it waits to retry its provider request (``text``), or None once it moved on."""

        with self._lock:
            self._progress["retrying"] = text

    def send_input(self, prompt):
        """Write the whole prompt and end input; a CLI that already exited reports why below."""

        try:
            try:
                self.process.stdin.write(prompt)
            finally:
                self.process.stdin.close()
        except BrokenPipeError:
            pass  # The CLI exited before reading; its exit status and stderr are reported.
        except OSError as exc:
            if exc.errno != errno.EINVAL:  # Windows reports a closed pipe as EINVAL.
                raise

    def start(self):
        """Arm the watchdog and the heartbeat; call it before send_input()."""

        with self._lock:
            self._started_at = self._active_at = time.monotonic()
        self._watchdog.start()
        self._heartbeat.start()

    def observed(self, content):
        with self._lock:
            self._progress["lastEventAt"] = time.monotonic()
            self._progress["eventCount"] += 1
            if content:
                self._progress["contentEventCount"] += 1

    def close(self):
        self._stop.set()
        stop_process(self.process)
        # Each reader closes its stream when the stream ends. One that a process
        # the CLI started still holds open is left to its reader: closing it here
        # would wait for that process.
        for reader in self._readers:
            reader.join(timeout=1)


def _close_quietly(stream):
    try:
        stream.close()
    except (AttributeError, OSError, ValueError):
        pass


def _snapshot_intact(workspace, input_files):
    """Whether a kept proof snapshot still holds exactly the captured files."""

    try:
        workspace = Path(workspace)
        if workspace.is_symlink() or not workspace.is_dir():
            return False
        for name, value in input_files:
            path = workspace / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != value.encode("utf-8"):
                return False
    except OSError:
        return False
    return True


def _prune_snapshots():
    """Keep the newest SNAPSHOTS_KEPT proof snapshots, and any younger than a day.

    A call reuses its snapshot for every retry and refreshes its time, so a
    call still running keeps it; one removed anyway is written again.
    """

    retrieval_runtime.prune_folders(tempfile.gettempdir(), SNAPSHOT_PREFIX + "*", SNAPSHOTS_KEPT)


def _write_snapshot(workspace, input_files):
    """Write the captured files read-only into an empty workspace; a partial snapshot is removed."""

    try:
        for name, value in input_files:
            descriptor = os.open(workspace / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o400)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value.encode("utf-8"))
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return workspace


# The largest text a model may leave in a file it edits (see EditableFile).
MAX_EDIT_FILE_BYTES = 16 * 1024 * 1024

PROOF_FILES_NOTE = (
    "\n\n{title}:\nYour working directory contains proof.md ({proof}), statement.md (the exact task), and "
    "manifest.json (content hashes and provenance). Read both files completely; use successive ranges if tool "
    "output is truncated.{agents} Review only this snapshot. Do not resolve links into the author's workspace."
)
# Added to PROOF_FILES_NOTE for a call that may start subagents (its multi_agent feature).
PROOF_FILES_AGENTS_NOTE = " Give independent subagents these same files and working directory."
EDIT_FILE_NOTE = (
    "\n\nEDITING {name}:\n{start}Write all changes to {name} in place, in small steps, so that it holds the "
    "complete final text when you answer; never replace it with a file pointer.{unchanged}{agents} Do not copy "
    "its text into your JSON answer."
)
INLINE_EDIT_NOTE = (
    "\n\nCOMPLETE TEXT IN THE ANSWER:\nThis run cannot give you a writable {name}. Return the complete final "
    "text of {name} in the {field} field of your JSON answer, never a file pointer."
)


class EditableFile:
    """The file one structured call edits in place (a node's `edit`): its name, result field and initial text.

    The model edits a writable copy in its own workspace in small steps and
    answers with a short status, so no answer has to hold a whole proof or
    document; structured() reads the copy back into ``text`` after each
    answer. Historical (web-cutoff) runs give structured roles no writable
    workspace: there the answer carries the complete text in ``field``.
    """

    def __init__(self, name, field, initial=None, readonly=False):
        self.name, self.field, self.initial = name, field, initial
        # A read-only visit (verify) keeps the initial text whatever its answer says.
        self.readonly = readonly
        self.text = initial if readonly else None
        # The file's bytes (None: absent) when the attempt behind the conversation's base ended.
        self.base = None


def _with_field(schema, field):
    """A response schema that also requires the string `field`."""

    return {**schema, "properties": {**schema.get("properties", {}), field: {"type": "string"}},
            "required": list(dict.fromkeys([*schema.get("required", []), field]))}


def _workspace_note(input_files, edit_file, inline, schema_value, features):
    """What a structured call's working directory holds and how its edited file comes back."""

    note = ""
    writable = edit_file is not None and not inline
    if input_files is not None:
        in_bundle = writable and edit_file.name == "proof.md"
        note += PROOF_FILES_NOTE.format(title="PROOF FILES" if in_bundle else "READ-ONLY PROOF FILES",
                                        proof="a writable copy of the exact candidate" if in_bundle
                                        else "the exact candidate",
                                        agents=PROOF_FILES_AGENTS_NOTE if "multi_agent" in features else "")
        if edit_file is None and "solution" in (schema_value.get("properties") or {}):
            note += " Return the complete proof text in solution, never a file pointer."
    if edit_file is None:
        return note
    readable = edit_file.name in dict(input_files or ())
    if inline:
        note += INLINE_EDIT_NOTE.format(name=edit_file.name, field=edit_file.field)
        if not readable and edit_file.initial is not None:
            note += f"\n\nCURRENT {edit_file.name}:\n{edit_file.initial}"
        return note
    start = ("" if readable else f"Create {edit_file.name} in your working directory; it does not exist yet. "
             if edit_file.initial is None else f"Your working directory contains a writable copy of {edit_file.name}. ")
    return note + EDIT_FILE_NOTE.format(
        name=edit_file.name, start=start,
        unchanged=" Leave it unchanged if nothing needs to change." if edit_file.initial is not None else "",
        agents=" Only you edit it; subagents only read it." if "multi_agent" in features else "")


def _snapshot_ready(stage, workspace, input_files, writable=False, features=()):
    if "multi_agent" in features:
        text = ("The critic and its subagents can read statement.md, and the critic edits its own copy of proof.md."
                if writable else
                "The critic and its subagents can read proof.md and statement.md in their read-only workspace.")
    else:
        text = ("The critic can read statement.md and edits its own copy of proof.md." if writable else
                "The critic can read proof.md and statement.md in its read-only workspace.")
    emit("status", stage, label="Proof snapshot ready", text=text,
         snapshotWorkspace=str(workspace), snapshotManifest=json.loads(dict(input_files)["manifest.json"]))


def _place_edit_file(workspace, name, data):
    """Put exactly `data` (None: nothing) where an earlier attempt may have left its edits."""

    path = Path(workspace) / name
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    if data is not None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)


def _edit_file_bytes(workspace, name):
    """The bytes of a regular edited file, or None when it is missing, a link or something else."""

    try:
        descriptor = os.open(Path(workspace) / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EDIT_FILE_BYTES:
            return None
        return stream.read()


def _edit_workspace(conversation, input_files, edit_file, stage, features=()):
    """The writable workspace of one attempt: the read-only inputs and the edited file.

    A forked conversation continues with the file as the attempt behind its
    base left it; a new conversation starts from the initial text, so no
    conversation ever sees edits it did not make. Files that vanished (a
    temp-folder cleaner) are written again, and a conversation that used them
    starts over.
    """

    readonly = tuple((name, value) for name, value in input_files or () if name != edit_file.name)
    workspace = conversation.workspace
    if workspace is not None and not _snapshot_intact(workspace, readonly):
        conversation.workspace = workspace = None
        emit("diagnostic", stage, text="The working files of this call disappeared or changed; writing them again "
                                      "and starting a new conversation from the exact input.")
        resumed = conversation.resume
        conversation.forget()
        if resumed:
            raise ConversationUnavailable("The working files that the saved conversation used are gone.")
    if workspace is None:
        prefix = "tcs-proof-review-" if input_files is not None else "tcs-edit-"
        workspace = conversation.workspace = _write_snapshot(Path(tempfile.mkdtemp(prefix=prefix)), readonly)
        if input_files is not None:
            _snapshot_ready(stage, workspace, input_files, writable=True, features=features)
    _place_edit_file(workspace, edit_file.name, edit_file.base if conversation.resume else
                     None if edit_file.initial is None else edit_file.initial.encode("utf-8"))
    return workspace


def _read_edited_file(workspace, name):
    """The text a model left in its writable copy; InvalidAnswer says in plain words what is wrong with it."""

    try:
        if workspace is None or Path(workspace).is_symlink():
            raise FileNotFoundError(name)
        descriptor = os.open(Path(workspace) / name,
                             os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise InvalidAnswer(f"{name} does not exist; it must hold the complete final text") from None
    except OSError as exc:
        raise InvalidAnswer(f"{name} must be a regular file, not a link or folder", detail=str(exc)) from exc
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InvalidAnswer(f"{name} must be a regular file, not a link or folder")
        data = stream.read(MAX_EDIT_FILE_BYTES + 1)
    if len(data) > MAX_EDIT_FILE_BYTES:
        raise InvalidAnswer(f"{name} is larger than {MAX_EDIT_FILE_BYTES // (1024 * 1024)} MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidAnswer(f"{name} is not UTF-8 text", detail=str(exc)) from exc
    if "\0" in text:
        raise InvalidAnswer(f"{name} contains NUL characters")
    if not text.strip():
        raise InvalidAnswer(f"{name} is empty; it must hold the complete final text")
    return text


def run_structured_attempt(
    prompt, schema_value, stage, model, effort, speed, summary,
    timeout=None, activity_label=None, features=(), input_files=None, conversation=None, deadline=None,
    edit_file=None, subagents=None,
):
    """Run one structured CLI process (Codex or Claude) and return its final text.

    The CLI is stopped after `timeout` seconds without output (None:
    STRUCTURED_STALL_SECONDS) or after `deadline` seconds (the workflow time
    limit; None: none). With a StructuredConversation the session persists
    and, when the conversation says so, this attempt forks its base (see
    structured()). With an `edit_file` the attempt runs in the writable
    workspace that structured() prepared (see _edit_workspace). With the
    multi_agent feature, ``subagents`` is the most subagents Codex lets the
    call run at once (see subagent_limits()); Claude has no such setting.
    """

    policy = retrieval_policy(features=features)
    limits = subagent_limits(features, subagents)
    claude = providers.harness(chosen_model(model)) == providers.CLAUDE
    if claude and policy:
        raise ConfigurationError(HISTORICAL_CODEX_ONLY)
    if conversation is not None and policy:
        # Historical sessions stay ephemeral; every retry re-runs the saved input.
        conversation.resumable = False
        conversation.forget()
    if edit_file is not None and (policy or conversation is None or conversation.workspace is None):
        # Web-cutoff runs never get a writable workspace (the answer carries the text there).
        raise ConfigurationError("A writable file needs the workspace that structured() prepares.")

    def claude_request(workspace):
        # Claude reads files with its Read tool; the snapshot's shell_tool is Codex plumbing.
        return claude_cli.run_structured(
            prompt, schema_value, stage, model, effort, summary, timeout=timeout,
            activity_label=activity_label, workspace=workspace,
            features=(*features, "file_edit") if edit_file is not None else features,
            environment=environment(model), runtime=sys.modules[__name__],
            **({"conversation": conversation} if conversation is not None else {}),
            **({"deadline": deadline} if deadline is not None else {}),
        )

    if input_files is not None or edit_file is not None:
        # The tuple holds captured text, never paths to mutable author files.
        if input_files is not None and (
                not isinstance(input_files, tuple) or len(input_files) != 3
                or {name for name, _ in input_files} != {"proof.md", "statement.md", "manifest.json"}
                or not all(isinstance(value, str) for _, value in input_files)):
            raise ConfigurationError("Invalid proof snapshot bundle.")
        snapshot_features = tuple(dict.fromkeys((*features, "shell_tool")))

        def review(snapshot_policy, workspace):
            environment_value = None if claude else retrieval_environment(snapshot_policy, "critic", model,
                                                                          agent_limits=limits)
            if claude:
                return claude_request(workspace)
            return _run_structured_process(
                prompt, schema_value, stage, model, effort, speed, summary,
                timeout=timeout, activity_label=activity_label, features=snapshot_features,
                policy=snapshot_policy, child_environment=environment_value, workspace=workspace,
                conversation=conversation, deadline=deadline, writable=edit_file is not None,
                subagents=subagents,
            )

        if edit_file is not None:
            return review(None, conversation.workspace)
        if policy:
            try:
                with retrieval_runtime.critic_session(policy) as snapshot_policy:
                    workspace = _write_snapshot(retrieval_runtime.role_workspace(snapshot_policy, "critic"),
                                                input_files)
                    _snapshot_ready(stage, workspace, input_files, features=features)
                    return review(snapshot_policy, workspace)
            except retrieval_runtime.RetrievalPolicyError as exc:
                raise ConfigurationError(str(exc)) from exc
        # Keep the exact reviewed artifacts for diagnosis after the process exits.
        # Every attempt of one call reviews the same snapshot, where a forked
        # session finds the files it already read.
        workspace = conversation.workspace if conversation is not None else None
        if workspace is not None:
            try:
                os.utime(workspace)  # In use: retention keeps it (see _prune_snapshots()).
            except OSError:
                pass
        if workspace is not None and not _snapshot_intact(workspace, input_files):
            # A temp-folder cleaner (or anything else) removed or changed the
            # snapshot: write it again, and never let a conversation continue
            # from files that are no longer the captured ones.
            conversation.workspace = workspace = None
            emit("diagnostic", stage, text="The proof snapshot of this call disappeared or changed; writing the "
                                          "captured files again and starting a new conversation from the exact input.")
            if conversation.resume:
                conversation.forget()
                raise ConversationUnavailable("The proof snapshot that the saved conversation read is gone.")
            conversation.forget()
        if workspace is None:
            _prune_snapshots()
            workspace = _write_snapshot(Path(tempfile.mkdtemp(prefix=SNAPSHOT_PREFIX)), input_files)
            _snapshot_ready(stage, workspace, input_files, features=features)
            if conversation is not None:
                conversation.workspace = workspace
        return review(None, workspace)
    if claude:
        return claude_request(structured_workspace())
    child_environment = retrieval_environment(policy, "structured", model, agent_limits=limits)
    workspace = retrieval_runtime.role_workspace(policy, "structured") if policy else structured_workspace()
    return _run_structured_process(
        prompt, schema_value, stage, model, effort, speed, summary,
        timeout=timeout, activity_label=activity_label, features=features,
        policy=policy, child_environment=child_environment, workspace=workspace,
        conversation=conversation, deadline=deadline, subagents=subagents,
    )


def _codex_session_deleter(executable, environment):
    """Remove one persisted Codex session, with its subagent threads (`codex delete --force <id>`)."""

    def delete(session_id):
        try:
            result = subprocess.run(
                [executable, "delete", "--force", session_id], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", env=environment, timeout=SESSION_CLEANUP_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    return delete


def _codex_session_id(value):
    """Accept only a canonical UUID: the id reaches argv as `exec fork <id>`."""

    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _run_structured_process(
    prompt, schema_value, stage, model, effort, speed, summary,
    *, timeout, activity_label, features, policy, child_environment, workspace, conversation=None, deadline=None,
    writable=False, subagents=None,
):
    # Verified with Codex 0.160: without --ephemeral `codex exec` persists the
    # session and reports its id (thread.started); `codex exec fork <id> -`
    # accepts the same root flags (model, effort, features, -C, sandbox or
    # permission profile, config), copies the saved conversation into a new
    # session and leaves the original untouched; `codex delete --force <id>`
    # removes a session with its subagent threads. memories.generate_memories
    # =false keeps these sessions out of Codex's memory of the user's own work.
    persistent = conversation is not None and conversation.resumable and not policy
    fork = conversation.fork_from if persistent and conversation.fork_from else None
    # The call's private temp folder first: a web-cutoff call's came with its environment.
    with _confined_call(writable, model, child_environment, policy, workspace) as (sandbox, process_environment), \
            tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        schema = folder / "schema.json"
        answer = folder / "answer.json"
        schema.write_text(json.dumps(schema_value), encoding="utf-8")
        schema_arguments = output_schema_arguments(model, schema)
        try:
            if policy:
                executable = retrieval_runtime.codex_executable(policy)
            elif model_provider(model) == API_PROVIDER:
                executable = api_codex_executable(child_environment)
            else:
                executable = codex()
            tool_arguments = structured_tool_arguments(stage, features, policy, subagents)
        except retrieval_runtime.RetrievalPolicyError as exc:
            # The run's policy, its native binary or its search settings changed since this call checked them.
            raise ConfigurationError(str(exc)) from exc
        command = [
            executable,
            *(["--strict-config"] if policy else []),
            "-m", wire_model(model), "-c", f'model_reasoning_effort="{effort}"',
            *([] if policy else inherited_openai_base_url_arguments(
                overrides={"model_provider": model_provider(model)}, environment=child_environment,
            )),
            *provider_arguments(model),
            *speed_arguments(speed, model), *context_cache_arguments(),
            *(["-c", "memories.generate_memories=false"] if persistent else []),
            "-c", f'model_reasoning_summary="{summary}"',
            *tool_arguments,
            *[argument for feature in features for argument in ("--enable", feature)],
            # "-s read-only" would override the confined profile. A call that edits a
            # file writes only its own temporary workspace (see _edit_workspace).
            "-C", str(workspace), *sandbox,
            "-a", "never", "exec", *(["fork"] if fork else []),
            "--json", *([] if persistent else ["--ephemeral"]), "--skip-git-repo-check",
            *([] if policy else ["--ignore-user-config"]), *schema_arguments, "-o", str(answer),
            *([fork] if fork else []), "-",
        ]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", env=process_environment,
            )
        except OSError as exc:
            exc.cli_problem = "Codex"  # The CLI could not be started at all.
            raise
        monitor = RequestMonitor(process, stage, model, effort, timeout=timeout, deadline=deadline,
                                 activity_label=activity_label)
        provider_errors = []
        started = False
        # The saved session of a call with subagents, which alone shows those of multi-agent version 2.
        rollout, rollout_found = None, True
        try:
            monitor.start()
            monitor.send_input(prompt)
            for line in monitor.lines():
                try:
                    raw_event = json.loads(line)
                    if not isinstance(raw_event, dict):
                        raise ValueError("not an event")
                    kind = raw_event.get("type")
                    if kind == "thread.started" and persistent:
                        thread = raw_event.get("thread_id")
                        started = True
                        if _codex_session_id(thread):
                            conversation.session_id, conversation.confirmed = thread.lower(), True
                            conversation.created(thread.lower(), _codex_session_deleter(executable, child_environment))
                            if subagents and "multi_agent" in features and rollout is None:
                                rollout = CodexRollout(codex_home_folder(process_environment), thread.lower(),
                                                       conversation)
                                rollout.follow()
                    elif (persistent and isinstance(kind, str) and kind.startswith("item.")
                          and isinstance(raw_event.get("item"), dict)
                          and raw_event["item"].get("type") not in (None, "error")):
                        # The model worked in this session: a retry continues from it.
                        conversation.output = True
                    item = raw_event.get("item")
                    if (isinstance(item, dict) and item.get("type") == "collab_tool_call" and item.get("tool") == "wait"
                            and kind in ("item.started", "item.completed")):
                        # Codex writes nothing while the root agent waits for its subagents.
                        monitor.waiting(str(item.get("id")), kind == "item.started")
                    if (conversation is not None and isinstance(item, dict) and item.get("type") == "collab_tool_call"
                            and isinstance(item.get("agents_states"), dict)):
                        # Codex 0.160 reports each subagent's state ({status, message}) on the
                        # spawn_agent, send_input, wait and close_agent calls of multi-agent version 1;
                        # version 2 reports only its wait_agent calls ("wait", naming no thread), and its
                        # subagents only in the saved session (CodexRollout).
                        receivers = item.get("receiver_thread_ids")
                        if (item.get("tool") == "spawn_agent" and item.get("status") == "completed"
                                and isinstance(receivers, list)):
                            for thread in receivers:
                                conversation.subagent(thread, "started")
                        elif item.get("tool") == "wait" and kind == "item.started":
                            conversation.subagent(None, "waiting")
                        for thread, state in item["agents_states"].items():
                            status = state.get("status") if isinstance(state, dict) else None
                            if status in ("errored", "completed"):
                                conversation.subagent(thread, "failed" if status == "errored" else "completed",
                                                      state.get("message") if status == "errored" else "")
                    if kind == "error":
                        message = raw_event.get("message")
                        if isinstance(message, str) and not message.startswith(
                            "Reconnecting..."
                        ):
                            provider_errors.append(message)
                    elif kind == "turn.failed":
                        failure = raw_event.get("error")
                        message = (
                            failure.get("message")
                            if isinstance(failure, dict) else failure
                        )
                        if isinstance(message, str) and message.strip():
                            provider_errors.append(message.strip())
                    event = public_event(raw_event)
                    if event is not None:
                        # A malformed event may name its type with a number or a list.
                        event_name = str(event.get("method") or event.get("type") or "")
                        content_event = event_name.startswith("item/") or (
                            event_name.startswith("item.")
                            or event_name.startswith("item_")
                        )
                        monitor.observed(content_event)
                        emit(
                            "codex_event", stage, event=event,
                            activityLabel=activity_label or "",
                        )
                    if kind == "turn.completed":
                        emit_cache_usage(stage, raw_event.get("usage"))
                except ValueError:  # Includes json.JSONDecodeError.
                    emit(
                        "diagnostic", stage,
                        text="Codex returned a malformed event.",
                    )
            code = process.wait()
        finally:
            monitor.close()
            if rollout is not None:
                rollout_found = rollout.stop()
        if not rollout_found and multi_agent_version(model) == 2:
            emit("diagnostic", stage, activityLabel=activity_label or "",
                 text="Codex's saved session of this attempt could not be read, so whether one of its independent "
                      "critics' model calls failed is not known for this attempt.")
        # Codex writes the answer file; replace undecodable bytes so a damaged
        # answer is retried as an unusable answer, not a crash.
        output = answer.read_text(encoding="utf-8", errors="replace") if answer.exists() else ""
        if conversation is not None:
            conversation.detail = {"provider": redact("\n".join(provider_errors)),
                                   "stderr": redact(monitor.stderr_text())}
        stopped = monitor.stopped(stage)
        if stopped is not None:
            raise stopped
        # A turn that failed without a usable answer is a provider failure, also when Codex exits 0.
        if code or (provider_errors and not _holds_json_object(output)):
            detail = _clip_middle(redact("\n".join((*provider_errors, *monitor.stderr_lines)).strip()), 8000)
            if fork and not started:
                # The fork never started (Codex no longer has the saved session,
                # or this CLI cannot fork it): nothing ran.
                raise ConversationUnavailable(f"Codex could not continue the saved {stage} conversation: {detail}")
            failure = Error(
                f"Codex {stage} failed: {detail}"
                if detail else f"Codex {stage} failed without diagnostics."
            )
            if not monitor.output_seen:
                failure.cli_problem = "Codex"  # It ended without any output: it could not start.
            raise failure
        return output

def save_final_input(statement, solution, directory=None):
    """Atomically preserve the exact clean proof sent to the LaTeX editor."""

    path = Path(directory or Path.cwd()) / FINAL_INPUT_FILENAME
    payload = {
        "schemaVersion": 1,
        "statement": text(statement),
        "solution": text(solution),
    }
    _private_atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return path

def _schema_problem(value, schema):
    """Say in plain words why a structured answer does not match its response schema."""

    error = next(validator_for(schema)(schema).iter_errors(value), None)
    if error is None:
        return ""
    path = list(error.absolute_path)
    field = "".join(f"[{part}]" if isinstance(part, int) else (f".{part}" if index else str(part))
                    for index, part in enumerate(path))
    subject = f"the {field} field" if field else "the answer"
    if error.validator == "required":
        missing = re.findall(r"'([^']+)' is a required property", error.message)
        return f"{subject if field else 'the answer'} is missing the {missing[0] if missing else 'required'} field"
    if error.validator == "additionalProperties":
        return f"{subject} contains fields that are not allowed ({error.message})"
    if error.validator == "type":
        expected = error.validator_value
        expected = " or ".join(expected) if isinstance(expected, list) else expected
        return f"{subject} must be {'an' if str(expected)[:1] in 'aeiou' else 'a'} {expected}"
    if error.validator == "enum":
        return f"{subject} must be one of {', '.join(map(str, error.validator_value))}"
    if error.validator == "minItems":
        return f"{subject} must have at least {error.validator_value} items"
    if error.validator == "maxItems":
        return f"{subject} must have at most {error.validator_value} items"
    return f"{subject} is invalid ({error.message})"


def _accepted_answer(raw, schema_value, validate=None):
    """Decode, schema-check and (optionally) further check one answer; InvalidAnswer says what is wrong."""

    try:
        value = decoded_json_object(raw)
    except Error as exc:
        problem = ("no final answer was returned" if not (raw or "").strip()
                   else "the final answer was not one complete JSON object")
        raise InvalidAnswer(problem, detail=str(exc)) from exc
    try:
        validate_json_schema(value, schema_value)
    except Error as exc:
        raise InvalidAnswer(_schema_problem(value, schema_value) or str(exc), detail=str(exc)) from exc
    return validate(value, raw) if validate is not None else value


def _attempt_timeout(timeout, options):
    """One attempt's timeout: none starts after the workflow deadline, and none outlives it."""

    if options is None:
        return timeout
    remaining = workflow_remaining(options)
    if remaining <= 0:
        raise Error("Workflow time limit reached; saved work remains resumable.")
    if math.isinf(remaining):
        return timeout
    return remaining if timeout is None else min(float(timeout), remaining)


_FAILURE_LOG_LOCK = threading.Lock()


def _append_failure_log(path, text):
    """Append to the run's failure log, never through a link, a FIFO or a second hard link; whether it was written.

    The sandboxed author can write the run folder, so it could leave any of
    them where the log belongs.
    """

    import stat

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        if not nofollow and os.path.islink(path):
            return False
        with _FAILURE_LOG_LOCK:
            descriptor = os.open(path, flags, 0o600)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return False
                data = text.encode("utf-8")
                while data:
                    data = data[os.write(descriptor, data):]
            finally:
                os.close(descriptor)
    except OSError:
        return False
    return True


def structured(
    prompt, schema_value, stage, model=MODEL, effort=EFFORT,
    speed=DEFAULT_SPEED, summary=DEFAULT_REASONING_SUMMARY,
    timeout=None, attempts=None,
    request_label=None, activity_label=None, features=(), input_files=None, edit_file=None,
    validate=None, options=None, cancel=None, candidate_text=True, subagents=None,
):
    """Run one read-only structured model call, retrying every failure a retry can fix until it succeeds.

    An ``edit_file`` (EditableFile) is the one file the call may change: the
    model edits a writable copy in its workspace and its text, read back after
    each answer, is checked in the same retry loop (a missing or empty file is
    an unacceptable answer).

    This is the one retry loop of every structured role. A failed attempt
    (provider or transport error, CLI crash, non-zero exit or missing CLI, an
    attempt stopped because its CLI wrote nothing for ``timeout`` seconds
    (STRUCTURED_STALL_SECONDS by default; a working attempt is never
    stopped), an empty, unparsable or schema-invalid answer, an answer that
    ``validate`` rejects with InvalidAnswer, or any answer given after some
    of the call's subagents failed) is retried after MODEL_RETRY_SECONDS,
    with no attempt limit (``attempts`` caps it only for direct callers that
    ask). Configuration errors, workflow-rule errors, safety refusals and
    harness errors are raised at once (see retryable_failure). A retry
    continues the call's conversation with a short note, or re-runs the
    exact saved input when it cannot (see StructuredConversation). With
    workflow ``options`` no attempt starts after the workflow deadline or
    outlives it, the waits do not consume the time budget, the pause file
    is honoured, and each failed attempt's full provider messages and CLI
    stderr are appended to the ``failure_log`` file; Stop interrupts a wait
    at once, and ``cancel`` ends one for a sibling request (an
    attempt that fails once ``cancel`` is set ends the call with
    RetryCancelled, without a log entry, a diagnostic or a wait). The CLI
    sessions the call persisted are deleted when it ends. The first request
    record carries the reviewed proof (candidateText) unless
    ``candidate_text`` is False, and the diagnostics of a call with an
    activity label name it. With the multi_agent feature, ``subagents`` is
    the most subagents the call may run at once (subagent_limits()); its
    attempts report their subagents' progress (SubagentProgress).
    """

    policy = retrieval_policy(features=features)
    model = chosen_model(model)
    effort = effective_effort(model, effort)
    speed = effective_speed(model, speed)
    summary = reasoning_summary(model, summary)
    try:
        require_model_credentials(model)
    except CLIUnavailable:
        pass  # A CLI updating itself is back soon: every attempt looks for it again.
    # Web-cutoff runs give structured roles no writable workspace: the answer carries the edited text.
    inline = edit_file is not None and policy is not None
    if inline:
        schema_value = _with_field(schema_value, edit_file.field)
    prompt += _workspace_note(input_files, edit_file, inline, schema_value, features)
    prompt = structured_prompt_for_model(prompt, schema_value, model)
    if attempts is not None:
        try:
            if isinstance(attempts, bool):
                raise ValueError
            attempts = int(attempts)
        except (TypeError, ValueError) as exc:
            raise Error("Structured attempts must be a positive integer.") from exc
        if attempts < 1:
            raise Error("Structured attempts must be a positive integer.")
    conversation = StructuredConversation(resumable=policy is None)
    activity = activity_label or request_label
    if subagents and "multi_agent" in features:
        conversation.progress = SubagentProgress(stage, subagents)
        note_no_subagent_tool(stage, model, subagents)
        note_unobservable_subagents(stage, model, subagents, policy)
    logged = {}  # request text -> the attempt that logged it in full
    failure, attempt, failing_since = None, 0, None
    stall = STRUCTURED_STALL_SECONDS if timeout is None else float(timeout)
    failure_log = (options or {}).get("failure_log")
    entry = {}  # The newest full entry of this call in the failure log: its body and attempt.

    def log_failure(exc):
        """Append the attempt's full failure to the run's failure log; the log's name, or "" if nothing was written."""

        if not failure_log:
            return ""
        detail = conversation.detail
        parts = [f"Provider messages:\n{detail['provider']}" if detail.get("provider") else "",
                 f"CLI stderr:\n{detail['stderr']}" if detail.get("stderr") else ""]
        if isinstance(exc, SubagentFailure):
            parts.append("Subagent errors:\n" + "\n".join(
                f"{name}: {redact(message)}" for name, message in exc.failures.items()))
        if isinstance(exc, InvalidAnswer) and not any(parts):
            return ""  # The diagnostic already says everything.
        body = "\n".join(part for part in (redact(str(exc) or type(exc).__name__), *parts) if part)
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(_wall_clock()))
        header = f"{when} · {_call_name(stage)}{f' ({activity})' if activity else ''} · attempt {attempt}"
        same = entry.get("body") == body
        text = (f"{header} failed the same way as attempt {entry['attempt']}.\n\n" if same
                else f"{header} failed:\n{body}\n\n")
        if not _append_failure_log(failure_log, text):
            return ""
        if not same:
            entry.update(body=body, attempt=attempt)
        return Path(failure_log).name

    def request(text):
        retry = attempt > 1
        repeated = logged.get(text)
        emit(
            "request", stage,
            label=(request_label or f"Exact {stage} input") if not retry
            else (f"{request_label} · retry" if request_label else f"Exact {stage} retry"),
            text=(f"(Identical to the exact input of attempt {repeated}.)" if repeated else text),
            attempt=attempt, model=model, modelProvider=model_provider(model),
            reasoningEffort=effort, reasoningSummary=summary,
            serviceTier=speed, responseSchema=schema_value,
            **({"conversation": "continued" if conversation.resume else "new"} if retry else {}),
            **({"sameInputAsAttempt": repeated} if repeated else {}),
            # The reviewed proof is a file, not part of the request text.
            **({"candidateText": dict(input_files)["proof.md"]}
               if input_files is not None and candidate_text and not retry else {}),
        )
        logged.setdefault(text, attempt)

    def attempt_once(text):
        request(text)
        conversation.start_attempt()
        if conversation.progress is not None:
            conversation.progress.start_attempt(attempt)
        if edit_file is not None and not inline:
            edit_file.text = None
            _edit_workspace(conversation, input_files, edit_file, stage, features)
        return run_structured_attempt(
            text, schema_value, stage, model, effort, speed, summary,
            timeout=stall, activity_label=activity, features=features,
            **({"input_files": input_files} if input_files is not None else {}),
            **({"edit_file": edit_file} if edit_file is not None and not inline else {}),
            **({"subagents": subagents} if subagents is not None else {}),
            conversation=conversation, deadline=attempt_deadline,
        )

    def run(text):
        nonlocal attempt
        try:
            return attempt_once(text)
        except ConversationUnavailable as exc:
            if cancel is not None and cancel.is_set():
                raise RetryCancelled("Another request of this step ended it before this attempt started.") from exc
            # Nothing ran: re-run the exact saved input in a new conversation now.
            attempt += 1
            emit("status", stage, label=f"{activity} · {RETRY_LABEL}" if activity else RETRY_LABEL,
                 retryAttempt=attempt, **({"activityLabel": activity} if activity else {}),
                 text="The saved conversation of this model call is no longer available; re-running "
                      f"its exact saved input in a new conversation now (attempt {attempt}).",
                 detail=_clipped(redact(str(exc)), 1000))
            conversation.forget()
            return attempt_once(conversation.request(prompt, schema_value, failure))

    with retry_clock(options).call(stage):
        try:
            while True:
                attempt += 1
                if cancel is not None and cancel.is_set():
                    raise RetryCancelled("Another request of this step ended it before this attempt started.")
                # No attempt starts after the workflow deadline, and none outlives it.
                attempt_deadline = _attempt_timeout(None, options)
                raw = ""
                try:
                    raw = run(conversation.request(prompt, schema_value, failure))
                    if conversation.subagent_failures:
                        # An answer after failed subagent calls rests on an
                        # incomplete review: never use it, run them again.
                        raise SubagentFailure(conversation.subagent_failures)
                    value = _accepted_answer(raw, schema_value)
                    if edit_file is not None and not inline:
                        edit_file.text = _read_edited_file(conversation.workspace, edit_file.name)
                    return (validate(value, raw) if validate is not None else value), raw
                except Exception as exc:
                    settings = settings_failure(exc, model)
                    if settings is not None:
                        raise settings from exc
                    if not retryable_failure(exc):
                        raise  # A retry cannot fix it (see retryable_failure).
                    if cancel is not None and cancel.is_set():
                        # The step ended meanwhile (Stop ended this attempt, or another request ended the
                        # step): nothing is retried, so the failure is not logged or announced as one.
                        raise RetryCancelled("Another request of this step ended it while this one ran.") from exc
                    failure = exc
                    if isinstance(exc, InvalidAnswer):
                        exc.raw = raw
                    conversation.finish_attempt(exc)
                    if (edit_file is not None and not inline and conversation.base
                            and conversation.base == conversation.session_id):
                        # A retry continues this attempt's conversation, and so its file.
                        edit_file.base = _edit_file_bytes(conversation.workspace, edit_file.name)
                    failing_since = failing_since or _wall_clock()
                    logged_to = log_failure(exc)
                    full = f" Full details: {logged_to} in the run folder." if logged_to else ""
                    # A call with an activity label (such as the critic coordinator) is named.
                    where = f"{activity}, attempt {attempt}" if activity else f"attempt {attempt}"
                    named = {"activityLabel": activity} if activity else {}
                    if isinstance(exc, InvalidAnswer):
                        emit("diagnostic", stage, rawResponse=(raw or "")[:STRUCTURED_RETRY_OUTPUT_CHARS],
                             attempt=attempt, **named,
                             text=(f"The answer to {_call_name(stage)} ({where}) could not be accepted: "
                                   f"{exc.problem}." + (f" Rule: {exc.rule}." if exc.rule else "")
                                   + (f" {_clip_middle(redact(exc.detail).strip(), 1600)}" if exc.detail else "")
                                   + full))
                    elif isinstance(exc, SubagentFailure):
                        messages = "; ".join(dict.fromkeys(
                            _failure_reason(redact(message), 400) for message in exc.failures.values()
                            if str(message or "").strip()))
                        emit("diagnostic", stage, rawResponse=(raw or "")[:STRUCTURED_RETRY_OUTPUT_CHARS],
                             attempt=attempt, **named,
                             text=(f"The answer to {_call_name(stage)} ({where}) was set aside because "
                                   f"{exc.plain}; the retry runs the failed subagents again."
                                   + (f" Subagent errors: {messages}" if messages else "") + full))
                    else:
                        emit("diagnostic", stage, attempt=attempt, **named, text=(
                            f"{_call_name(stage)[0].upper()}{_call_name(stage)[1:]} ({where}) failed: "
                            f"{_clip_middle(redact(str(exc) or type(exc).__name__).strip(), 2000)}{full}"))
                    if attempts is not None and attempt >= attempts:
                        raise
                wait_before_retry(options, stage, attempt + 1, failure, cancel=cancel, activity=activity,
                                  since=failing_since)
        finally:
            # The persisted CLI sessions served only this call.
            conversation.discard()


class RPC:
    """Send the few JSON-RPC messages needed by Goal mode.

    Requests the app-server sends to this unattended client (approvals, user
    input, MCP elicitations, ...) are answered at once, with a decline or a
    JSON-RPC error, so the turn continues. ``pending_call`` names the call
    waiting for its answer, which the goal session's watcher bounds;
    ``notice`` (set by the session) receives one diagnostic sentence per
    answered request kind and per error reply, and ``stderr_log`` receives the
    whole redacted stderr of the app-server.
    """

    skipped = 0  # Output lines that were not protocol messages (see wire()).

    # The answers an unattended client gives; other requests get a JSON-RPC error.
    DECLINED = {
        "item/commandExecution/requestApproval": {"decision": "decline"},
        "item/fileChange/requestApproval": {"decision": "decline"},
        "execCommandApproval": {"decision": "denied"},
        "applyPatchApproval": {"decision": "denied"},
        "mcpServer/elicitation/request": {"action": "decline"},
    }

    def __init__(self, process, record=None, stderr_log=None):
        self.process, self.record = process, record
        self.number, self.waiting = 0, deque()
        self.write_lock = threading.Lock()
        self.stderr_tail = deque(maxlen=12)
        self.stderr_reader = None
        self.pending_call = None
        self.notice = None
        self.answered = set()
        if getattr(process, "stderr", None) is not None:
            def drain_errors():
                log = _open_private_log(stderr_log) if stderr_log else None
                try:
                    if log is not None:
                        log.write(f"--- Codex app-server started {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
                        log.flush()
                    for line in process.stderr:
                        # Startup diagnostics must not publish environment credentials
                        # or an API model's key.
                        value = redact(line.strip())
                        if value:
                            self.stderr_tail.append(value[:800])
                            if log is not None:
                                log.write(value + "\n")
                                log.flush()
                except (OSError, ValueError):
                    pass
                finally:
                    if log is not None:
                        try:
                            log.close()
                        except OSError:
                            pass
            self.stderr_reader = threading.Thread(target=drain_errors, daemon=True)
            self.stderr_reader.start()


    def send(self, message):
        with self.write_lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def answer(self, message):
        """Answer one request of the app-server: this client is unattended and asks nobody."""

        method = str(message.get("method"))
        result = self.DECLINED.get(method)
        reply = {"id": message["id"]}
        if result is not None:
            reply["result"] = result
        else:
            reply["error"] = {"code": -32601, "message": f"TCS Prover runs unattended and cannot answer {method}."}
        try:
            self.send(reply)
        except (OSError, ValueError):
            pass  # The app-server is gone; reading reports it.
        if self.notice is not None and method not in self.answered:
            self.answered.add(method)
            self.notice(f"Codex asked for {method}; TCS Prover runs unattended, so it "
                        f"{'declined' if result is not None else 'answered that it cannot reply'}, "
                        "and the turn continues.")

    def wire(self):
        """Read one new message from Codex.

        A line that is not a JSON object (for example a launcher's own
        output) is skipped with a diagnostic. The end of the output, also
        after a partial line of a crashing app-server, reports its stderr tail.
        """

        while True:
            line = self.process.stdout.readline()
            try:
                message = json.loads(line) if line.strip() else None
            except ValueError:
                message = None
            if isinstance(message, dict):
                break
            if not line.endswith("\n"):
                if self.stderr_reader:
                    self.stderr_reader.join(timeout=0.2)
                detail = "\n".join(self.stderr_tail)
                raise Error("Codex app-server stopped unexpectedly." + ("\n" + detail if detail else ""))
            if line.strip():
                self.skipped += 1
                if self.skipped <= 3:
                    emit("diagnostic", "model", text="Skipped a line of Codex app-server output that is not a "
                         "protocol message" + (" (later ones are skipped silently)" if self.skipped == 3 else "")
                         + f": {_clipped(redact(line.strip()), 300)}")
        if self.record:
            self.record(message)
        # This is a runtime warning, not model-authored text. Continuing after
        # it can strand goal completion and repeatedly spend requests while
        # every functions.exec call fails, including historical retrieval.
        if (message.get("method") == "warning"
                and "code mode is unavailable" in str((message.get("params") or {}).get("message", "")).lower()):
            raise ToolRuntimeUnavailable("Required model tool runtime is unavailable: " + message["params"]["message"])
        if "id" in message and "method" in message:
            self.answer(message)
        return message

    def read(self):
        return self.waiting.popleft() if self.waiting else self.wire()

    def request(self, method, params, before_send=None):
        """Send one numbered request and return its identifier.

        ``before_send(identifier)`` runs under the write lock before the
        request is written, so an answer can never arrive before its caller
        has recorded the request.
        """

        with self.write_lock:
            self.number += 1
            request = self.number
            if before_send is not None:
                before_send(request)
            self.process.stdin.write(json.dumps({
                "id": request, "method": method, "params": params,
            }) + "\n")
            self.process.stdin.flush()
            return request

    def call(self, method, params):
        request = self.request(method, params)
        self.pending_call = (method, time.monotonic())
        try:
            while True:
                message = self.wire()
                if message.get("id") == request and "method" not in message:
                    if "error" in message:
                        error = message["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        if self.notice is not None:
                            self.notice(f"Codex answered {method} with JSON-RPC error {code}: "
                                        f"{_clipped(json.dumps(error, ensure_ascii=False), 600)}")
                        raise RPCError(f"{method} failed: {_provider_message(error) or 'no reason given'}",
                                       code=code, payload=error)
                    return message.get("result", {})
                self.waiting.append(message)
        finally:
            self.pending_call = None


def _open_private_log(path):
    """Open an owner-only append log for one writer; None when a link, FIFO or other non-file is in the way."""

    try:
        if not getattr(os, "O_NOFOLLOW", 0) and os.path.islink(path):
            return None
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), 0o600)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        # A second hard link could name a file outside the run folder.
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a", encoding="utf-8")
    except OSError:
        pass
    os.close(descriptor)
    return None


def _author_sandbox(runtime, options, *, directory, policy, model, read_only=False):
    """The confined profile of an author's app-server: {"base", "temp"}, or None where Codex keeps its legacy sandbox.

    ``temp`` is the run's private temp folder, which the controller names
    with the ``run_tmp_dir`` option (runs/.controller/<run>/tmp); the runner
    creates it owner-only when missing and refuses a link. Without the
    option (tests, direct CLI use) each runner process uses a fresh folder.
    A folder that cannot be prepared fails the attempt, which is retried: the
    author never starts without its sandbox.
    """

    permissions = None if policy else getattr(runtime, "private_permissions", None)
    base = ":read-only" if read_only else ":workspace"
    if permissions is None or permissions(base, model) is None:
        return None
    try:
        temp = (confinement.private_folder(options["run_tmp_dir"], directory) if options.get("run_tmp_dir")
                else confinement.runner_folder(directory))
    except (confinement.ConfinementError, OSError, TypeError, ValueError) as exc:
        raise runtime.Error(f"Cannot prepare the run's private temp folder, so the author does not start: {exc}") from exc
    return {"base": base, "temp": temp}


def author_confinement_check(rpc, *, sandbox, directory, emit):
    """Before the author's first turn, prove through its app-server that its profile confines it (no model).

    See confinement.startup_check(). A check that cannot run or does not
    hold fails the attempt with a retryable Error: the author is never
    started without its sandbox, and the run tries again after the usual wait.
    """

    writable = sandbox["base"] == ":workspace"
    try:
        confinement.startup_check(lambda params: rpc.call("command/exec", params), workspace=directory,
                                  temp=sandbox["temp"], writable=writable, profile=PRIVATE_PERMISSIONS)
    except confinement.ConfinementError as exc:
        raise Error(f"The sandbox check before the author's turn failed: {exc}. The author is not started "
                    "without its sandbox.") from exc
    except (Error, OSError) as exc:
        if isinstance(exc, FinalError):
            raise
        raise Error(f"The sandbox check before the author's turn could not run ({exc}). The author is not started "
                    "without its sandbox.") from exc
    emit("status", label="Sandbox checked",
         text=f"A check command in the author's sandbox could {'read and write' if writable else 'read'} the run's "
              f"folder and write its private temp folder {sandbox['temp']}, and could not read outside them.")


def _user_extensions_off(config):
    """Thread settings that turn off the MCP servers, plugins and apps of Codex's effective settings `config`.

    They run outside the sandbox. Each is turned off by name (verified with
    Codex 0.160: an empty mcp_servers table leaves them on).
    """

    def names(key):
        table = config.get(key)
        return [name for name in table if isinstance(name, str) and name] if isinstance(table, dict) else []

    off = {"apps": {"_default": {"enabled": False},
                    **{name: {"enabled": False} for name in names("apps") if name != "_default"}}}
    for key in ("mcp_servers", "plugins"):
        if names(key):
            off[key] = {name: {"enabled": False} for name in names(key)}
    return off


def _author_app_server(runtime, *, directory, policy, role, model, settings, features, classic, sandbox=None):
    """The command line of an author's Codex app-server: (argv, child environment, whether it runs an API model).

    With a ``sandbox`` (_author_sandbox()) the app-server runs with the
    private temp folder's environment and defines the confined profile, so
    the startup check can run a command in it (author_confinement_check());
    in a live run that profile reaches the internet (live_network()), as
    Codex's legacy sandbox does elsewhere (legacy_network_arguments()).
    Either way the environment lacks the variables that name the user's SSH
    and GPG agents (with network access the author could connect to them). A
    web-cutoff author gets the private temp folder of its verified
    environment instead (retrieval_runtime.preflight()); the caller removes
    it when the process ends (retrieval_runtime.remove_private_temp()). Its
    subagent limits (subagent_limits(), from ``features`` and
    settings["subagents"]) are session flags, which a web-cutoff run's
    checks verify exactly.
    """

    limits = subagent_limits(features, settings.get("subagents"))
    child_environment = retrieval_environment(policy, role, model, inherited=runtime.environment(model),
                                              agent_limits=limits)
    try:
        api_model = (child_environment is not None and hasattr(runtime, "api_codex_home")
                     and runtime.model_provider(model) == getattr(runtime, "API_PROVIDER", None))
        if api_model:
            # An API-model thread lives in the private home of its workspace,
            # so a later run can resume it (never the user's CODEX_HOME).
            child_environment["CODEX_HOME"] = str(runtime.api_codex_home(directory))
        live = live_network(directory, policy)
        if sandbox is not None:
            child_environment = confinement.confined_environment(child_environment, sandbox["temp"])
            temp_environment = confinement.temp_environment(sandbox["temp"])
            confined = [*private_permission_arguments(sandbox["base"], model, profile=runtime.private_permissions(
                sandbox["base"], model, codex_home=child_environment.get("CODEX_HOME"), live=live)),
                # Also for a user's shell_environment_policy that passes on only some variables (inherit = "core").
                *[part for name, value in temp_environment.items()
                  for part in ("-c", f"shell_environment_policy.set.{name}={toml_string(value)}")]]
        elif policy:
            confined = retrieval_runtime.temp_arguments(policy, child_environment)
        else:
            child_environment = confinement.without_agents(
                child_environment if child_environment is not None else os.environ)
            confined = legacy_network_arguments(live)
        argv = [
            retrieval_runtime.codex_executable(policy) if policy
            else runtime.api_codex_executable(child_environment) if api_model else runtime.codex(),
            *(["--strict-config"] if policy else []), "app-server", "--enable", "goals",
            *retrieval_runtime.native_search_arguments(directory, policy=policy),
            *([] if "multi_agent" in features else ["--disable", "multi_agent"]),
            *runtime.provider_arguments(model), *runtime.speed_arguments(settings["speed"], model),
            *runtime.context_cache_arguments(), *confined, *retrieval_runtime.agent_limit_arguments(limits),
            *[argument for feature in features for argument in ("--enable", feature)],
            *(["--disable", "code_mode", "--enable", "shell_tool"] if classic else []),
        ]
    except BaseException:
        # No process will use this web-cutoff author's private temp folder.
        retrieval_runtime.remove_private_temp(policy, child_environment)
        raise
    return argv, child_environment, api_model


def _initialize_author_rpc(runtime, rpc, *, policy, role, child_environment, api_model, agent_limits=None):
    """The handshake of an author's app-server; a web-cutoff run verifies its isolation (with the subagent
    limits of its session flags, ``agent_limits``) before any thread."""

    initialized = rpc.call("initialize", {
        "clientInfo": {"name": "tcs_prover", "title": "TCS Prover", "version": "1"},
        "capabilities": {"experimentalApi": True},
    })
    if api_model and Path(str((initialized or {}).get("codexHome") or "")).resolve() != Path(
            child_environment["CODEX_HOME"]).resolve():
        raise SafetyStop("Codex did not use the private home of this API model; refusing to continue.")
    rpc.send({"method": "initialized", "params": {}})
    if policy:
        retrieval_runtime.verify_rpc(rpc, policy, role, child_environment, allow_runtime_overrides=True,
                                     **({"agent_limits": agent_limits} if agent_limits else {}))


def _author_thread_options(runtime, rpc, *, directory, policy, role, model, settings, summary, features, classic,
                           emit, read_only=False, environment=None):
    """The options with which an author starts or resumes its thread.

    ``environment`` is the environment of the author's app-server
    (_author_app_server()). A web-cutoff run uses its historical profile,
    with the TMPDIR and TMPPREFIX of that environment's private temp folder
    in its shell environment (retrieval_runtime.temp_overrides()), as the
    app-server's session flags have them: a thread's
    shell_environment_policy replaces the app-server's, so without them the
    thread's commands got neither (verified with Codex 0.160 at thread/start
    and at thread/resume in a new app-server).
    Otherwise the author works in the confined profile
    (private_permissions() for the environment's CODEX_HOME, where Codex
    supports one; the legacy workspace-write sandbox elsewhere, whose network
    setting the app-server's flags carry), or, with ``read_only`` (a
    conversation turn), in its read-only form (elsewhere the legacy
    read-only sandbox, whose network setting the turn carries,
    legacy_read_only_policy()); either form reaches the internet in a live
    run (live_network()). The user's MCP servers, plugins and apps, read
    from Codex's effective settings, and view_image are turned off; settings
    that cannot be read fail the attempt, which is retried. The thread has
    the subagent limits of its app-server (subagent_limits()).
    """

    limits = subagent_limits(features, settings.get("subagents"))

    thread_options = {
        "model": getattr(runtime, "wire_model", lambda value: value)(model),
        "cwd": str(directory), "runtimeWorkspaceRoots": [str(directory)],
        "sandbox": "workspace-write", "approvalPolicy": "never",
        "config": {"model_reasoning_effort": settings["effort"], "model_reasoning_summary": summary,
            **retrieval_runtime.native_search_configuration(directory, policy=policy),
            "features": {"goals": True, "multi_agent": False, "fast_mode": settings["speed"] == "fast",
                         **{feature: True for feature in features},
                         **({"code_mode": False, "shell_tool": True} if classic else {})}},
    }
    if hasattr(runtime, "model_provider"):
        thread_options["modelProvider"] = runtime.model_provider(model)
    if policy:
        thread_options.pop("sandbox")
        thread_options["permissions"] = retrieval_runtime.PROFILE
        locked = retrieval_runtime.configuration(policy, role)
        locked_features = locked.pop("features")
        # The commands' private temp folder, as verify_rpc() found it in the app-server's settings.
        temp_variables = retrieval_runtime.temp_overrides(policy, environment).get(
            "shell_environment_policy", {}).get("set")
        if temp_variables:
            locked["shell_environment_policy"]["set"].update(temp_variables)
        thread_options["config"].update(locked)
        thread_options["config"]["features"].update(locked_features)
        # The Author subagents setting decides, as for the app-server (--disable multi_agent for none): the
        # generated table turns multi_agent on for every role.
        thread_options["config"]["features"]["multi_agent"] = "multi_agent" in features
        thread_options["config"] = retrieval_runtime._merged(thread_options["config"], limits)
        return thread_options
    try:
        effective = rpc.call("config/read", {"cwd": str(directory)})
    except runtime.Error as exc:
        if isinstance(exc, (SafetyStop, ToolRuntimeUnavailable)):
            raise
        raise runtime.Error(f"Could not read Codex's settings, which the author needs in order to turn off your MCP "
                            f"servers, plugins and apps; trying again: {exc}") from exc
    effective = effective.get("config") if isinstance(effective, dict) else None
    effective = effective if isinstance(effective, dict) else {}
    # The user's MCP servers, plugins and apps run outside the sandbox: a live author gets none of them.
    thread_options["config"].update(_user_extensions_off(effective))
    thread_options["config"]["features"].update(apps=False, plugins=False, view_image=False)
    # The confined profile where Codex supports one, selected exactly as the historical profile is; none of
    # the user's [sandbox_workspace_write] settings carries over: network follows the run's search mode.
    permissions = getattr(runtime, "private_permissions", None)
    base = ":read-only" if read_only else ":workspace"
    private = permissions(base, model, codex_home=(environment or {}).get("CODEX_HOME"),
                          live=live_network(directory)) if permissions else None
    if private is not None:
        thread_options.pop("sandbox")
        thread_options["permissions"] = PRIVATE_PERMISSIONS
        thread_options["config"].update(default_permissions=PRIVATE_PERMISSIONS,
                                        permissions={PRIVATE_PERMISSIONS: private})
    elif read_only:
        thread_options["sandbox"] = "read-only"
    thread_options["config"] = retrieval_runtime._merged(thread_options["config"], limits)
    return thread_options


def asks_user(item):
    """Whether an agent message is a question to the user, never an answer of its turn.

    Codex's request_user_input_async tool delivers it at once ("delivery":
    "async") with its "questions", in the phase "final_answer"; the model
    keeps working, and an answer reaches it as a live instruction.
    """

    return isinstance(item, dict) and (item.get("delivery") == "async" or bool(item.get("questions")))


def _root_message(message, thread):
    """Whether an app-server message belongs to the root thread `thread`, not to a subagent's thread."""

    params = message.get("params") or {}
    owners = [params.get("threadId")]
    for key in ("goal", "turn", "item"):
        value = params.get(key)
        if isinstance(value, dict):
            owners.append(value.get("threadId"))
    if isinstance(params.get("thread"), dict):
        owners.append(params["thread"].get("id"))
    owners = [owner for owner in owners if owner is not None]
    scoped = message.get("method") in {
        "turn/started", "turn/completed", "item/completed",
        "thread/goal/updated", "thread/tokenUsage/updated",
    }
    return (bool(owners) or not scoped) and all(owner == thread for owner in owners)


def goal_session(runtime, prompt, *, prompts, settings, options, features=(),
                 node_name="author", stages=None, initial_instruction="", handoff_note=""):
    """Run one goal-enabled thread with lifecycle instructions supplied by YAML.

    Yield its final answer only after goal completion; accept follow-up feedback
    through ``send``. The model manages its own work in the supplied workspace.

    One session is one attempt: a model-call failure ends it in WorkflowPaused
    whose ``retry`` describes how to continue (_goal_step resumes the same
    thread after MODEL_RETRY_SECONDS). The ``goal_retry`` option carries that
    description into the continuing session: its stage, prompt values, first
    instruction, an undelivered compaction re-anchor and, after a turn that
    failed before the model produced anything, that turn's id (``rewind``):
    the session reverts it (thread/revert, verified with Codex 0.160) before
    sending its instruction again, so failed attempts add nothing to the
    thread. Failures a retry cannot fix carry no ``retry``: a missing thread
    id (session_unavailable) and safety refusals (safety_stop), which the
    controller never resumes by itself.

    A saved thread Codex no longer has (or refuses to reopen
    AUTHOR_RESUME_REFUSALS times in a row) and a conversation that outgrew
    the model's context continue in a new conversation of the same run,
    announced as "Conversation replaced": its first instruction is the resume
    prompt plus any open critic feedback. A thread that is reopened receives
    the short "reopen" prompt (when the workflow defines one) instead of the
    full resume prompt, unless the ``goal_reanchor`` option says that a
    conversation turn of the paused author compacted its context; critic
    feedback it already received (``author_repair_delivered``) is not sent
    again. ``handoff_note``, the controller's note on a submission it could not
    hand to the critic, follows any critic feedback in the first instruction,
    also in a new conversation, until the author submits again.

    A watcher keeps the session live: a request Codex leaves unanswered for
    RPC_RESPONSE_SECONDS, or a running turn with no event for
    AUTHOR_INACTIVITY_SECONDS followed by an unanswered check, ends the
    attempt as a failed model call; a turn that ends with the goal still open
    and no new turn within AUTHOR_CONTINUE_SECONDS is continued.
    """
    directory = os.path.abspath(os.fspath(options.get("goal_cwd") or os.getcwd()))
    policy = retrieval_policy(directory, features)
    retrieval_role = "author" if node_name == "author" else "structured"
    if policy and retrieval_role != "author":
        raise Error("Historical goal sessions are restricted to the configured author node.")
    stages = stages or {"initial": "solve", "resume": "repair", "failure": "failure"}
    continued = options.get("goal_retry") if isinstance(options.get("goal_retry"), dict) else {}
    stage = continued.get("stage") or stages["initial"]
    values = dict(original_prompt=prompt, directory=directory,
                  solution="", bugs="", round=0, revision_number=0, instruction=initial_instruction)
    values.update(continued.get("values") or {})

    def render(name):
        return runtime.render_template(prompts[name], values)

    def emit(kind, **fields):
        runtime.emit(kind, stage, node=node_name, **fields)

    settings = dict(settings)
    model = settings["model"]
    settings["effort"] = runtime.effective_effort(model, settings["effort"])
    settings["speed"] = runtime.effective_speed(model, settings["speed"])
    summary = settings["summary"] = runtime.reasoning_summary(model, settings["summary"])
    grace = getattr(runtime, "INTERRUPT_GRACE_SECONDS", INTERRUPT_GRACE_SECONDS)
    inactivity = getattr(runtime, "AUTHOR_INACTIVITY_SECONDS", AUTHOR_INACTIVITY_SECONDS)
    probe_limit = getattr(runtime, "AUTHOR_PROBE_SECONDS", AUTHOR_PROBE_SECONDS)
    continue_after = getattr(runtime, "AUTHOR_CONTINUE_SECONDS", AUTHOR_CONTINUE_SECONDS)
    response_limit = getattr(runtime, "RPC_RESPONSE_SECONDS", RPC_RESPONSE_SECONDS)
    steer_retry = getattr(runtime, "STEER_RETRY_SECONDS", STEER_RETRY_SECONDS)
    # A live run whose code-mode tool runtime was unavailable uses the classic shell tool.
    classic = bool(continued.get("classic_shell")) and not policy
    thread = None
    rpc = process = child_environment = None
    stop = threading.Event()
    expired = threading.Event()
    pausing = threading.Event()
    pause_file = options.get("goal_pause_file")
    # "turn_id" is the latest turn's id and "instruction_turn" the turn that
    # carried our latest instruction; "output" says whether the model produced
    # anything in the latest turn and "steered" whether a live instruction or
    # re-anchor joined it. Only an empty, unsteered turn of our own instruction
    # is ever reverted. "idle_since" is when the latest turn ended while the
    # goal stayed open.
    state = {"turn": None, "active": True, "steer": options.get("author_steer_delivered"),
             "instruction": None, "delivered": False, "turn_id": None, "instruction_turn": None,
             "output": False, "steered": False, "rewound": False, "delivered_once": False,
             "idle_since": None}
    attempt = continued.get("attempt") if isinstance(continued.get("attempt"), int) else None
    pending_steers = {}
    # Live instructions Codex did not accept: offered again at the next turn or later.
    held, told = {}, set()
    pending_compaction = continued.get("pending_compaction") or None
    seen_compactions = set()
    goal = {}
    pause_reason = ""
    last_usage = {}
    # The time of the newest message from Codex, and a cheap request that checks it still answers.
    activity = {"last": time.monotonic()}
    probe = {"at": None}
    stalled = {"reason": None}
    # Failures that a retry of the same resume cannot fix.
    final = {"reason": None, "refusal": None}

    def is_root(message):
        return _root_message(message, thread)

    def record(message):
        activity["last"] = time.monotonic()
        event = runtime.public_event(message)
        if event is not None:
            emit("codex_event", event=event, root=is_root(event))

    def pending_repair():
        """The critic feedback this conversation must receive once: a repair of this session, else the recovery."""

        return render("repair") if stage == stages["resume"] else str(values.get("instruction") or "")

    def replacement_instruction():
        """The first instruction of a new conversation that continues one Codex cannot continue."""

        repair = pending_repair()
        return render("resume") + ("\n\n" + repair if repair else "") + ("\n\n" + handoff_note if handoff_note else "")

    def received(repair):
        """Whether the reopened thread already received this critic feedback (the controller recorded it)."""

        delivered = options.get("author_repair_delivered")
        return (isinstance(delivered, dict) and delivered.get("thread") == thread
                and delivered.get("digest") == _repair_digest(repair))

    def hold(identity, why):
        held[identity] = (state["turn"], time.monotonic() + steer_retry)
        if identity not in told:
            told.add(identity)
            emit("diagnostic", text=f"{why}; it is offered again when the next turn starts or in "
                                    f"{_span_text(steer_retry)}.", threadId=thread)

    def held_back(identity):
        turn, until = held.get(identity, (None, None))
        if until is None:
            return False
        if state["turn"] != turn or time.monotonic() >= until:
            held.pop(identity, None)
            return False
        return True

    def steer(instruction, identity, compaction=False):
        entry = (identity, instruction, compaction)
        sent = []

        def register(request):
            # Recorded before the request is written: Codex may answer at once.
            sent.append(request)
            pending_steers[request] = entry

        try:
            rpc.request("turn/steer", {
                "threadId": thread, "expectedTurnId": state["turn"],
                "input": [{"type": "text", "text": instruction}],
            }, register)
        except BaseException:
            for request in sent:
                pending_steers.pop(request, None)
            raise
        if compaction:
            state["steered"] = True
        # The controller recognizes a restored instruction by its command id.
        emit("request", label="Author context re-anchor after compaction" if compaction
             else "Live author instruction sent", text=instruction, threadId=thread,
             **({} if compaction else {"steerId": identity}))

    def check(kind):
        probe["at"] = time.monotonic()  # Before sending: the answer may arrive at once.
        try:
            rpc.request("thread/goal/get", {"threadId": thread})
        except (runtime.Error, OSError) as exc:
            return f"Codex stopped responding: a check could not be sent ({exc})."
        if kind == "quiet":
            emit("diagnostic", text=f"No author event for {_span_text(inactivity)}; checking that Codex still "
                                    "answers.", threadId=thread)
        return None

    def liveness(now):
        """Why this attempt must end because Codex stopped answering, or None; asks a cheap question first."""

        call = getattr(rpc, "pending_call", None)
        if call and now - call[1] >= response_limit:
            return f"Codex stopped responding: no answer to {call[0]} within {_span_text(response_limit)}."
        if probe["at"] is not None:
            if activity["last"] >= probe["at"]:
                probe["at"] = None  # Codex answered, or worked: the attempt continues.
            elif now - probe["at"] >= probe_limit:
                return (f"Codex stopped responding: no event for {_span_text(now - activity['last'])} and no "
                        f"answer to a check within {_span_text(probe_limit)}.")
            return None
        if state["turn"] and now - activity["last"] >= inactivity:
            return check("quiet")
        if not state["turn"] and state["idle_since"] is not None and now - state["idle_since"] >= continue_after:
            # Its answer wakes the session, which then continues the open goal.
            return check("idle")
        return None

    def watch():
        pause_started = None
        interrupted_turn = None
        while not stop.wait(0.1):
            if pause_file and Path(pause_file).is_file() and state["active"]:
                if pause_started is None:
                    pause_started = time.monotonic()
                    pausing.set()
                if thread and state["turn"] and state["turn"] != interrupted_turn:
                    interrupted_turn = state["turn"]
                    try:
                        rpc.request("thread/goal/set", {"threadId": thread, "status": "paused"})
                        rpc.request("turn/interrupt", {"threadId": thread, "turnId": interrupted_turn})
                        emit("status", label="Author stopping", threadId=thread,
                             text=f"Pause requested: the author may finish its current step for up to "
                                  f"{_span_text(grace)} before it is stopped.")
                    except (runtime.Error, OSError) as exc:
                        emit("diagnostic", text=f"Could not interrupt the author: {exc}")
                if time.monotonic() - pause_started >= grace:
                    emit("diagnostic", text="Codex did not acknowledge the pause; keeping its saved session and the last saved work.")
                    runtime.stop_process(process)
                    return
                continue
            if runtime.workflow_remaining(options) <= 0:
                expired.set()
                if not state["active"]:
                    return
                try:
                    if thread:
                        rpc.request("thread/goal/set", {"threadId": thread, "status": "paused"})
                        if state["turn"]:
                            rpc.request("turn/interrupt", {"threadId": thread, "turnId": state["turn"]})
                            emit("status", label="Author stopping", threadId=thread,
                                 text=f"The workflow time limit was reached: the author may finish its current "
                                      f"step for up to {_span_text(grace)} before it is stopped.")
                except (runtime.Error, OSError):
                    pass
                if not stop.wait(grace):
                    runtime.stop_process(process)
                return
            if not state["active"]:
                continue
            stall = liveness(time.monotonic())
            if stall:
                stalled["reason"] = stall
                emit("diagnostic", text=stall, threadId=thread)
                runtime.stop_process(process)
                return
            if state["turn"]:
                command = runtime.pending_author_steer(options.get("author_steer_file"), state["steer"])
                if (command and not held_back(command[0])
                        and not any(item[0] == command[0] for item in list(pending_steers.values()))):
                    try:
                        steer(command[1], command[0])
                    except (runtime.Error, OSError) as exc:
                        hold(command[0], f"Could not send the live instruction ({exc})")

    def start_turn(instruction):
        # A retry re-sends this instruction unless the thread received it.
        state.update(instruction=instruction, delivered=False, instruction_turn=None, output=False, steered=False,
                     idle_since=None)
        if policy:
            retrieval_runtime.verify_rpc(rpc, policy, retrieval_role, child_environment, allow_runtime_overrides=True,
                                         agent_limits=subagent_limits(features, settings.get("subagents")))
        if pause_file and Path(pause_file).is_file():
            pausing.set()
            raise runtime.Error("Pause requested.")
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        rpc.call("thread/goal/set", {**goal, "status": "paused"})
        emit("request", label="Exact solve input", text=instruction, threadId=thread,
             model=model, reasoningEffort=settings["effort"], reasoningSummary=summary,
             **({"attempt": attempt} if attempt and not state["delivered_once"] else {}))
        state["active"] = True
        result = rpc.call("turn/start", {"threadId": thread, "cwd": str(directory),
            "input": [{"type": "text", "text": instruction}], "summary": summary})
        state["delivered"] = state["delivered_once"] = True
        state["turn"] = (result.get("turn") or {}).get("id")
        state["turn_id"] = state["instruction_turn"] = state["turn"]
        repair = pending_repair()
        if repair and repair in instruction:
            # The controller sends this feedback to the same thread only once.
            emit("status", label="Critic feedback delivered", threadId=thread, repairDelivered=_repair_digest(repair),
                 text="The author's conversation has received the critic's feedback.")
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        if not pausing.is_set() and not (pause_file and Path(pause_file).is_file()):
            rpc.call("thread/goal/set", {"threadId": thread, "status": "active"})

    def collect(item, answers):
        # A question to the user is in the phase "final_answer" too, but the model keeps working after it.
        if (item.get("type") in {"agentMessage", "agent_message"}
                and item.get("phase") in {None, "final_answer"} and not asks_user(item)
                and isinstance(item.get("text"), str) and item["text"].strip()):
            answers.append(item["text"])

    try:
        if pause_file and Path(pause_file).is_file():
            pausing.set()
            raise runtime.Error("Pause requested before starting the model.")
        if runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        if options.get("goal_require_resume") and not options.get("goal_thread_id"):
            final["reason"] = "missing thread id"
            raise runtime.Error("The saved Codex thread ID is missing; cannot resume the same conversation."
                                + UNAVAILABLE_THREAD_HINT)
        sandbox = _author_sandbox(runtime, options, directory=directory, policy=policy, model=model)
        argv, child_environment, api_model = _author_app_server(
            runtime, directory=directory, policy=policy, role=retrieval_role, model=model, settings=settings,
            features=features, classic=classic, sandbox=sandbox)
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(directory),
            env=child_environment)
        # A web-cutoff author can read its run folder: it gets no new controller output there.
        log = None if policy else options.get("goal_stderr_log")
        rpc = runtime.RPC(process, record, **({"stderr_log": log} if log else {}))
        rpc.notice = lambda text: emit("diagnostic", text=text, threadId=thread)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        _initialize_author_rpc(runtime, rpc, policy=policy, role=retrieval_role, child_environment=child_environment,
                               api_model=api_model, agent_limits=subagent_limits(features, settings.get("subagents")))
        thread_options = _author_thread_options(
            runtime, rpc, directory=directory, policy=policy, role=retrieval_role, model=model, settings=settings,
            summary=summary, features=features, classic=classic, emit=emit, environment=child_environment)
        if sandbox is not None and hasattr(runtime, "author_confinement_check"):
            runtime.author_confinement_check(rpc, sandbox=sandbox, directory=directory, emit=emit)
        if classic:
            emit("status", label="Classic shell tool",
                 text="Codex's code-mode tool runtime was unavailable, so this attempt uses the classic shell tool.")
        resumed = False
        replaced = ""
        if options.get("goal_thread_id"):
            try:
                result = rpc.call("thread/resume", {**thread_options, "threadId": options["goal_thread_id"],
                                                    "excludeTurns": True})
                resumed = True
            except runtime.Error as exc:
                if isinstance(exc, (SafetyStop, ToolRuntimeUnavailable)):
                    raise  # Never mistaken for a missing conversation.
                detail = _failure_reason(redact(str(exc)), 300)
                gone = any(word in str(exc).lower() for word in MISSING_THREAD_MARKERS)
                if not gone and isinstance(exc, RPCError) and not _plain_failure(str(exc)):
                    # Codex answered with a refusal that is not a provider failure.
                    previous = continued.get("refusal") if isinstance(continued.get("refusal"), dict) else {}
                    count = previous.get("count", 0) + 1 if previous.get("text") == detail else 1
                    final["refusal"] = {"text": detail, "count": count}
                    gone = count >= getattr(runtime, "AUTHOR_RESUME_REFUSALS", AUTHOR_RESUME_REFUSALS)
                if not gone:
                    if options.get("goal_require_resume"):
                        raise runtime.Error(f"Could not resume the saved Codex conversation; no replacement was started. {exc}") from exc
                    raise
                replaced = (f"Codex refused to reopen the saved conversation {final['refusal']['count']} times "
                            f"({detail})" if final["refusal"] else f"Codex no longer has the saved conversation ({detail})")
                final["refusal"] = None
        if not resumed:
            result = rpc.call("thread/start", {**thread_options, "ephemeral": False})
        thread = result.get("thread") if isinstance(result, dict) else None
        thread = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread, str) or not thread:
            thread = None
            raise runtime.Error(f"Codex answered {'thread/resume' if resumed else 'thread/start'} without a "
                                "thread ID; the next attempt asks again.")
        if resumed and thread != options["goal_thread_id"]:
            raise SafetyStop("Codex returned a different thread ID; refusing to continue a replacement conversation.")
        rewind = continued.get("rewind") if resumed and isinstance(continued.get("rewind"), dict) else {}
        if isinstance(rewind.get("turn"), str) and rewind["turn"]:
            # The interrupted turn failed before the model produced anything:
            # remove it, so its instruction is sent again instead of piling up.
            try:
                rpc.call("thread/revert", {"threadId": thread, "beforeTurnId": rewind["turn"]})
                state["rewound"] = True
                emit("status", label="Failed turn removed", threadId=thread,
                     text="The interrupted turn ended before the model produced anything; it was removed from the "
                          "conversation and its instruction is sent again.")
            except runtime.Error as exc:
                if isinstance(exc, (SafetyStop, ToolRuntimeUnavailable)):
                    raise
                emit("diagnostic", text=f"Could not remove the failed turn ({exc}); sending its instruction again.",
                     threadId=thread)
        saved_goal = (rpc.call("thread/goal/get", {"threadId": thread}).get("goal") or {}) if resumed else {}
        goal = {"threadId": thread, "objective": saved_goal.get("objective") or render("goal")}
        emit("status", label="Goal resumed" if resumed else "Goal started", threadId=thread,
             text=f"Thread {thread}")
        if replaced:
            emit("status", label="Conversation replaced", threadId=thread, text=_replacement_notice(replaced))
            instruction = replacement_instruction()
        elif continued.get("instruction"):
            # A retry: the interrupted turn's instruction, or the retry note.
            instruction = continued["instruction"]
        else:
            # A thread that is reopened holds the task already: the short reopen prompt, unless a
            # conversation turn compacted its context (goal_reanchor), which the full resume prompt restores.
            instruction = (render("reopen") if resumed and "reopen" in prompts and not options.get("goal_reanchor")
                           else render("resume") if options.get("goal_resume") or options.get("goal_thread_id")
                           else prompt)
            if initial_instruction and not (resumed and received(initial_instruction)):
                instruction += "\n\n" + initial_instruction
            if handoff_note:
                instruction += "\n\n" + handoff_note
        start_turn(instruction)
        status, running, answers = None, True, []
        while True:
            if expired.is_set() or runtime.workflow_remaining(options) <= 0:
                expired.set()
                if not running:
                    raise runtime.Error("Workflow time limit reached.")
                # The watcher interrupts the turn; it may end within the grace.
            message = rpc.read()
            params = message.get("params", {})
            if not is_root(message):
                continue
            request = message.get("id")
            if request in pending_steers and "method" not in message:
                identity, instruction, compaction = pending_steers.pop(request)
                if "error" in message:
                    reason = _provider_message(message["error"]) or "no reason given"
                    if compaction:
                        pending_compaction = instruction
                        emit("diagnostic", text=f"Codex did not accept the context re-anchor ({reason}); it is sent "
                                                "again when the next turn starts.")
                    else:
                        hold(identity, f"Codex did not accept the live instruction ({reason})")
                elif not compaction:
                    state["steer"] = identity
                    state["steered"] = True
                    emit("status", label="Author instruction accepted", authorSteerDelivered=identity,
                         text="The current conversation has received the live instruction.", threadId=thread)
            method, turn = message.get("method"), params.get("turn") or {}
            if method in {"item/started", "item/completed"}:
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") not in {None, "userMessage", "user_message"}:
                    state["output"] = True  # The model worked in this turn.
            if method == "turn/started":
                running, answers = True, []
                state["turn"], state["idle_since"] = turn.get("id"), None
                if state["turn"] and state["turn"] != state["turn_id"]:
                    if state["delivered"] and state["instruction_turn"] is None:
                        # Our instruction's turn, whose id turn/start did not report.
                        state["instruction_turn"] = state["turn"]
                    else:
                        # A turn Codex started by itself (the goal continues): its own output.
                        state.update(output=False, steered=False)
                    state["turn_id"] = state["turn"]
                if pending_compaction and state["turn"] and not pausing.is_set():
                    steer(pending_compaction, "retry", True)
                    pending_compaction = None
            elif method == "item/completed":
                item = params.get("item") or {}
                collect(item, answers)
                if item.get("type") == "contextCompaction" and not pausing.is_set():
                    key = (params.get("turnId"), item.get("id"))
                    if key not in seen_compactions:
                        seen_compactions.add(key)
                        state["turn"] = params.get("turnId") or state["turn"]
                        pending_compaction = render("compaction")
                        if state["turn"]:
                            steer(pending_compaction, key, True)
                            pending_compaction = None
            elif method == "thread/goal/updated":
                status = (params.get("goal") or {}).get("status")
            elif method == "thread/tokenUsage/updated":
                last_usage = (params.get("tokenUsage") or {}).get("last") or {}
            elif method == "turn/completed":
                running, state["turn"] = False, None
                if last_usage and hasattr(runtime, "emit_cache_usage"):
                    runtime.emit_cache_usage(stage, last_usage, label="Author cache usage")
                    last_usage = {}
                for item in turn.get("items", []):
                    collect(item, answers)
                if pausing.is_set() or (pause_file and Path(pause_file).is_file()):
                    pausing.set()
                    raise runtime.Error("Author paused; its current turn was interrupted."
                                        if turn.get("status") == "interrupted"
                                        else "Author paused at the current turn boundary.")
                if expired.is_set():
                    raise runtime.Error("Workflow time limit reached.")
                if turn.get("status") in {"failed", "interrupted"}:
                    _note_raw_error(emit, turn.get("error"))
                    raise runtime.Error(f"Author turn {turn['status']}: {_provider_message(turn.get('error')) or 'no complete result'}")
                state["idle_since"] = time.monotonic()
            elif method == "error":
                if not params.get("willRetry", False):
                    _note_raw_error(emit, params.get("error"))
                    raise runtime.Error(_provider_message(params.get("error")) or str(params))
                # Codex retries this request itself; say so, as the wait can be long.
                emit("status", label="Codex is reconnecting", text=_reconnect_text(params.get("error")),
                     threadId=thread)
            if not running and pause_file and Path(pause_file).is_file():
                pausing.set()
                raise runtime.Error("Author paused at the current turn boundary.")
            if status in {"usageLimited", "budgetLimited"}:
                raise runtime.Error(f"Author stopped: {status}.")
            if not running and status == "complete" and answers:
                state["active"] = False
                solution = answers[-1]
                emit("status", label="Goal complete", threadId=thread, text=f"Thread {thread}")
                emit("author_result", label="Author solution", text=solution, threadId=thread)
                rejection = yield {"outcome": "done", "output": solution}
                if rejection is None:
                    return
                handoff_note = ""  # This submission answered it.
                values.update(rejection)
                values["revision_number"] += 1
                stage = stages["resume"]
                start_turn(render("repair"))
                status, running, answers = None, True, []
            elif not running and status in {"blocked", "complete"}:
                start_turn(render("continuation"))
                status, running, answers = None, True, []
            elif (not running and state["idle_since"] is not None
                    and time.monotonic() - state["idle_since"] >= continue_after):
                emit("status", label="Author continued", threadId=thread,
                     text=f"The turn ended with the goal still open and Codex started no new turn within "
                          f"{_span_text(continue_after)}; the continuation instruction was sent.")
                start_turn(render("continuation"))
                status, running, answers = None, True, []
    except Exception as exc:
        # A stalled attempt reads as its stall, not as the stopped process it caused.
        failure = runtime.Error(stalled["reason"]) if stalled["reason"] else exc
        if pausing.is_set() or (pause_file and Path(pause_file).is_file()):
            pausing.set()
            emit("status", label="Author paused", text=str(failure), threadId=thread)
        else:
            pausing.set()
            # A model this account cannot use, or an endpoint that refuses the request: no retry helps.
            failure = settings_failure(failure, model) or failure
            limited = expired.is_set() or runtime.workflow_remaining(options) <= 0
            safety = isinstance(failure, (SafetyStop, retrieval_runtime.RetrievalPolicyError))
            pause_reason = "Workflow time limit reached." if expired.is_set() else (str(failure) or type(failure).__name__)
            tools = isinstance(failure, ToolRuntimeUnavailable) and not limited
            if tools:
                pause_reason += (" Web-cutoff runs need it, so this needs attention (for example a Codex update that "
                                 "has not finished); it is checked again every minute." if policy
                                 else " It is checked again every minute." if classic
                                 else " The next attempt starts Codex with the classic shell tool instead.")
            if safety and not limited:
                final["safety"] = True
                pause_reason = f"Stopped for safety: {pause_reason}{SAFETY_STOP_HINT}"
            emit("diagnostic", text=pause_reason, threadId=thread, **({"attempt": attempt} if attempt else {}))
            if tools and policy:
                emit("status", label="Author needs attention", text=pause_reason, threadId=thread, needsAttention=True)
            if limited or final["reason"] or safety or not retryable_failure(failure):
                emit("status", label="Author stopped for safety" if safety and not limited else "Author paused",
                     text=pause_reason, threadId=thread)
            else:
                # A model-call failure: the controller resumes this thread after a wait.
                reverted = (state["delivered"] and not state["output"] and not state["steered"]
                            and state["turn_id"] and state["turn_id"] == state["instruction_turn"])
                final["retry"] = {"thread": thread, "stage": stage, "values": dict(values),
                                  "instruction": state["instruction"], "delivered": state["delivered"],
                                  "output": state["output"], "rewound": state["rewound"],
                                  "rewind": {"turn": state["turn_id"]} if reverted else None,
                                  "steer": state["steer"], "pending_compaction": pending_compaction,
                                  "refusal": final["refusal"], "classic_shell": classic or (tools and not policy)}
                if thread and _context_window_failure(failure):
                    # The same conversation would fail again: continue in a new one.
                    final["retry"].update(
                        replace=f"The conversation no longer fits the model's context ({_failure_reason(redact(str(failure)), 300)})",
                        instruction=replacement_instruction(), delivered=False, output=False, rewound=False,
                        rewind=None, pending_compaction=None)
    finally:
        stop.set()
        if rpc and thread and state["active"]:
            def pause():
                try:
                    rpc.call("thread/goal/set", {"threadId": thread, "status": "paused"})
                except (runtime.Error, OSError, ValueError):
                    pass
            cleanup = threading.Thread(target=pause, daemon=True)
            cleanup.start()
            cleanup.join(timeout=1)
        if process:
            runtime.stop_process(process)
        # A web-cutoff author's private temp folder ends with its app-server.
        retrieval_runtime.remove_private_temp(policy, child_environment)
    if pausing.is_set():
        raise WorkflowPaused(pause_reason or "Workflow paused.", reason=pause_reason, thread_id=thread,
                             retry=final.get("retry"), session_unavailable=bool(final["reason"]),
                             safety_stop=bool(final.get("safety")))


def author_conversation(runtime, instruction, *, settings, options, features=(), conversation_id, label,
                        attempt=1, rewind=None, classic=False):
    """One conversation turn of the paused Codex author in its saved thread; return its reply.

    It reopens the saved thread (it never starts or replaces one), sets its
    goal paused (never active, and without an objective) and sends
    ``instruction`` as exactly one turn, read until that turn ends; it never
    steers. The workflow's time budget, checkpoint and state are untouched.
    A live run's turn is read-only and has no subagents
    (CONVERSATION_READ_ONLY); a web-cutoff run's uses the author's audited
    launch and profile, verified at the handshake and again before the turn.
    ``rewind`` names a failed empty turn to remove first (thread/revert);
    ``classic`` starts a live run's Codex with the classic shell tool. Every
    record has stage "conversation", node "author" and the conversationId.

    Return {"reply", "threadId", "compacted"}. A missing or gone saved thread
    raises SavedConversationGone, and safety and settings refusals are
    FinalErrors too. A refusal to reopen the thread raises
    ConversationRefused, and any other failed attempt
    ConversationAttemptFailed, whose ``retry`` says how the next attempt
    continues (see run_author_conversation).
    """

    directory = os.path.abspath(os.fspath(options.get("goal_cwd") or os.getcwd()))

    def emit(kind, **fields):
        runtime.emit(kind, "conversation", node="author", conversationId=conversation_id, **fields)

    saved = options.get("goal_thread_id")
    saved = str(saved).strip() if saved is not None else ""
    if not saved:
        raise SavedConversationGone("The author has not started its conversation yet.")
    policy = retrieval_policy(directory, features)
    restricted = not policy and getattr(runtime, "CONVERSATION_READ_ONLY", True)
    launch_features = () if restricted else tuple(features)
    classic = bool(classic) and not policy
    settings = dict(settings)
    model = settings["model"]
    settings["effort"] = runtime.effective_effort(model, settings["effort"])
    settings["speed"] = runtime.effective_speed(model, settings["speed"])
    summary = settings["summary"] = runtime.reasoning_summary(model, settings["summary"])
    inactivity = getattr(runtime, "AUTHOR_INACTIVITY_SECONDS", AUTHOR_INACTIVITY_SECONDS)
    probe_limit = getattr(runtime, "AUTHOR_PROBE_SECONDS", AUTHOR_PROBE_SECONDS)
    response_limit = getattr(runtime, "RPC_RESPONSE_SECONDS", RPC_RESPONSE_SECONDS)
    thread = rpc = process = child_environment = None
    stop = threading.Event()
    # "running" while the turn runs; "turn" is its id, "delivered" whether Codex
    # accepted it and "output" whether the model produced anything in it.
    state = {"running": False, "turn": None, "delivered": False, "output": False}
    activity = {"last": time.monotonic()}
    probe = {"at": None}
    stalled = {"reason": None}

    def record(message):
        activity["last"] = time.monotonic()
        event = runtime.public_event(message)
        if event is not None:
            emit("codex_event", event=event, root=_root_message(event, thread))

    def stall(reason):
        stalled["reason"] = reason
        emit("diagnostic", text=reason, threadId=thread)
        runtime.stop_process(process)

    def watch():
        while not stop.wait(1):
            now = time.monotonic()
            call = getattr(rpc, "pending_call", None)
            if call and now - call[1] >= response_limit:
                return stall(f"Codex stopped responding: no answer to {call[0]} within {_span_text(response_limit)}.")
            if probe["at"] is not None:
                if activity["last"] >= probe["at"]:
                    probe["at"] = None  # Codex answered, or worked: the turn continues.
                elif now - probe["at"] >= probe_limit:
                    return stall(f"Codex stopped responding: no event for {_span_text(now - activity['last'])} and "
                                 f"no answer to a check within {_span_text(probe_limit)}.")
                continue
            if state["running"] and now - activity["last"] >= inactivity:
                probe["at"] = now  # Before sending: the answer may arrive at once.
                try:
                    rpc.request("thread/goal/get", {"threadId": thread})
                except (runtime.Error, OSError) as exc:
                    return stall(f"Codex stopped responding: a check could not be sent ({exc}).")
                emit("diagnostic", text=f"No author event for {_span_text(inactivity)}; checking that Codex still "
                                        "answers.", threadId=thread)

    def collect(item, texts, answers, questions):
        if (item.get("type") in {"agentMessage", "agent_message"}
                and isinstance(item.get("text"), str) and item["text"].strip()):
            # A question to the user is no reply; only a turn with no other text shows it.
            if asks_user(item):
                questions.append(item["text"])
                return
            texts.append(item["text"])
            if item.get("phase") in {None, "final_answer"}:
                answers.append(item["text"])

    try:
        sandbox = _author_sandbox(runtime, options, directory=directory, policy=policy, model=model,
                                  read_only=restricted)
        argv, child_environment, api_model = _author_app_server(
            runtime, directory=directory, policy=policy, role="author", model=model, settings=settings,
            features=launch_features, classic=classic, sandbox=sandbox)
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(directory),
            env=child_environment)
        # A web-cutoff author can read its run folder: it gets no new controller output there.
        log = None if policy else options.get("goal_stderr_log")
        rpc = runtime.RPC(process, record, **({"stderr_log": log} if log else {}))
        if hasattr(rpc, "notice"):
            rpc.notice = lambda text: emit("diagnostic", text=text, threadId=thread)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        _initialize_author_rpc(runtime, rpc, policy=policy, role="author", child_environment=child_environment,
                               api_model=api_model,
                               agent_limits=subagent_limits(launch_features, settings.get("subagents")))
        thread_options = _author_thread_options(
            runtime, rpc, directory=directory, policy=policy, role="author", model=model, settings=settings,
            summary=summary, features=launch_features, classic=classic, emit=emit, read_only=restricted,
            environment=child_environment)
        # Codex's legacy read-only sandbox (no confined profile) gets its network setting with the turn.
        turn_sandbox = ({"sandboxPolicy": legacy_read_only_policy(live_network(directory))}
                        if thread_options.get("sandbox") == "read-only" else {})
        if sandbox is not None and hasattr(runtime, "author_confinement_check"):
            runtime.author_confinement_check(rpc, sandbox=sandbox, directory=directory, emit=emit)
        try:
            result = rpc.call("thread/resume", {**thread_options, "threadId": saved, "excludeTurns": True})
        except runtime.Error as exc:
            if isinstance(exc, (SafetyStop, ToolRuntimeUnavailable)):
                raise  # Never mistaken for a missing conversation.
            detail = _failure_reason(redact(str(exc)), 300)
            if any(word in str(exc).lower() for word in MISSING_THREAD_MARKERS):
                raise SavedConversationGone(f"Codex no longer has the saved author conversation ({detail}).") from exc
            if isinstance(exc, RPCError) and not _plain_failure(str(exc)):
                # Codex answered with a refusal that is not a provider failure.
                raise ConversationRefused(detail) from exc
            raise
        reopened = result.get("thread") if isinstance(result, dict) else None
        reopened = reopened.get("id") if isinstance(reopened, dict) else None
        if not isinstance(reopened, str) or not reopened:
            raise runtime.Error("Codex answered thread/resume without a thread ID; the next attempt asks again.")
        if reopened != saved:
            raise SafetyStop("Codex returned a different thread ID; refusing to continue a replacement conversation.")
        thread = reopened
        emit("status", label="Conversation reopened", threadId=thread, text=f"Thread {thread}")
        if isinstance(rewind, str) and rewind:
            # The failed attempt's turn ended before the model produced anything: remove it, so the
            # message is sent again instead of piling up.
            try:
                rpc.call("thread/revert", {"threadId": thread, "beforeTurnId": rewind})
                emit("status", label="Failed turn removed", threadId=thread,
                     text="The failed reply turn ended before the model produced anything; it was removed and the "
                          "message is sent again.")
            except runtime.Error as exc:
                if isinstance(exc, (SafetyStop, ToolRuntimeUnavailable)):
                    raise
                emit("diagnostic", text=f"Could not remove the failed turn ({exc}); sending the message again.",
                     threadId=thread)
        try:
            # The goal stays paused: Codex answers this one turn and never continues the proof by itself.
            rpc.call("thread/goal/set", {"threadId": thread, "status": "paused"})
        except (runtime.Error, OSError) as exc:
            if isinstance(exc, (SafetyStop, ToolRuntimeUnavailable)):
                raise
            emit("diagnostic", text=f"Could not confirm that the goal is paused: {exc}", threadId=thread)
        if policy:
            retrieval_runtime.verify_rpc(rpc, policy, "author", child_environment, allow_runtime_overrides=True,
                                         agent_limits=subagent_limits(launch_features, settings.get("subagents")))
        emit("request", label=label, text=instruction, threadId=thread, model=model,
             reasoningEffort=settings["effort"], reasoningSummary=summary,
             **({"attempt": attempt} if attempt > 1 else {}))
        result = rpc.call("turn/start", {"threadId": thread, "cwd": str(directory),
                                         "input": [{"type": "text", "text": instruction}], "summary": summary,
                                         **turn_sandbox})
        state["delivered"] = state["running"] = True
        state["turn"] = (result.get("turn") or {}).get("id") if isinstance(result, dict) else None
        texts, answers, questions, compacted = [], [], [], False
        while True:
            message = rpc.read()
            if not _root_message(message, thread):
                continue
            method, params = message.get("method"), message.get("params") or {}
            if method in {"item/started", "item/completed"}:
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") not in {None, "userMessage", "user_message"}:
                    state["output"] = True  # The model worked in this turn.
            if method == "turn/started":
                state["turn"] = state["turn"] or (params.get("turn") or {}).get("id")
            elif method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                collect(item, texts, answers, questions)
                # The reply needs no re-anchor; the next Resume sends the full resume prompt (goal_reanchor).
                compacted = compacted or item.get("type") == "contextCompaction"
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if state["turn"] and turn.get("id") and turn["id"] != state["turn"]:
                    continue  # Not this turn.
                state["running"] = False
                for item in turn.get("items") or []:
                    if isinstance(item, dict):
                        collect(item, texts, answers, questions)
                if turn.get("status") in {"failed", "interrupted"}:
                    _note_raw_error(emit, turn.get("error"))
                    raise runtime.Error(f"Author reply turn {turn['status']}: "
                                        f"{_provider_message(turn.get('error')) or 'no complete result'}")
                break
            elif method == "error":
                if not params.get("willRetry", False):
                    _note_raw_error(emit, params.get("error"))
                    raise runtime.Error(_provider_message(params.get("error")) or str(params))
                # Codex retries this request itself; say so, as the wait can be long.
                emit("status", label="Codex is reconnecting", text=_reconnect_text(params.get("error")),
                     threadId=thread)
        reply = answers[-1] if answers else texts[-1] if texts else questions[-1] if questions else ""
        if not reply.strip():
            raise runtime.Error("The author's turn ended without a reply.")
        return {"reply": reply, "threadId": thread, "compacted": compacted}
    except (FinalError, retrieval_runtime.RetrievalPolicyError, ConversationAttemptFailed):
        raise
    except (runtime.Error, Error, OSError) as exc:
        # A stalled attempt reads as its stall, not as the stopped process it caused.
        message = stalled["reason"] or str(exc) or type(exc).__name__
        raise ConversationAttemptFailed(message, retry={
            "delivered": state["delivered"], "output": state["output"],
            "rewind": state["turn"] if state["delivered"] and not state["output"] else None,
            "classic": isinstance(exc, ToolRuntimeUnavailable)}) from exc
    finally:
        stop.set()
        if rpc is not None and thread:
            def pause():
                try:
                    rpc.call("thread/goal/set", {"threadId": thread, "status": "paused"})
                except (runtime.Error, OSError, ValueError):
                    pass
            cleanup = threading.Thread(target=pause, daemon=True)
            cleanup.start()
            cleanup.join(timeout=1)
        if process is not None:
            runtime.stop_process(process)
        # A web-cutoff reply's private temp folder ends with its app-server.
        retrieval_runtime.remove_private_temp(policy, child_environment)


def _replacement_notice(reason):
    """The "Conversation replaced" status: why, and what the new conversation keeps."""

    return (f"{str(reason).rstrip('.')}. The author continues in a new conversation in this run, starting from the "
            "resume prompt, its working directory, any open critic feedback and any note on why its last submission "
            "was refused; the critic rounds and the time budget are kept.")


def _repair_digest(text):
    """The fingerprint by which a goal session recognizes critic feedback its thread already received."""

    return _sha256(normalized_text(text))


def _note_raw_error(emit, error):
    """Keep the raw payload of a provider error that has no message of its own as a diagnostic."""

    if isinstance(error, dict) and not (isinstance(error.get("message"), str) and error["message"].strip()):
        emit("diagnostic", text=f"Codex reported the failure as: {_clipped(json.dumps(error, ensure_ascii=False), 600)}")


def _reconnect_text(error):
    """"Codex is reconnecting (2/5)…" with the provider's message, for an error Codex retries itself."""

    message = _provider_message(error)
    count = re.search(r"(\d+)\s*/\s*(\d+)", message)
    detail = re.sub(r"^\s*reconnecting\W*(?:\d+\s*/\s*\d+)?\W*", "", message, flags=re.IGNORECASE).strip(" ()")
    text = f"Codex is reconnecting ({count.group(1)}/{count.group(2)})…" if count else "Codex is retrying the request…"
    return text + (f" Provider message: {_clipped(detail, 300)}" if detail else "") + (
        " TCS Prover's own retry follows only if Codex gives up.")


def _provider_message(error):
    """The readable message of a provider error that Codex reports as text or as an object.

    An object without a message is described by its codexErrorInfo, for
    example "usage limit exceeded" or "response stream disconnected (HTTP 503)".
    """

    if isinstance(error, dict):
        message = error.get("message")
        text = (message.strip() if isinstance(message, str) and message.strip()
                else _error_info_text(error.get("codexErrorInfo", error.get("codex_error_info")))
                or json.dumps(error, ensure_ascii=False))
        details = error.get("additionalDetails") or error.get("additional_details")
        if isinstance(details, str) and details.strip() and details.strip() not in text:
            text += f" ({details.strip()})"
        return text
    return str(error).strip() if error else ""


def _error_info_text(info):
    """Plain words for a codexErrorInfo: "usageLimitExceeded", or {"responseStreamDisconnected": {...}}."""

    if isinstance(info, dict) and len(info) == 1:
        (name, value), = info.items()
        words = _error_info_text(name)
        status = value.get("httpStatusCode") if isinstance(value, dict) else None
        return words + (f" (HTTP {status})" if words and isinstance(status, int) and not isinstance(status, bool) else "")
    if isinstance(info, str) and info.strip():
        return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", info.strip()).replace("_", " ").lower()
    return ""


def _sha256(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def normalized_text(value):
    """Normalize line endings and trailing/outer blank space for text comparison."""

    lines = str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _clipped(value, limit=1600):
    """Shorten diagnostic text while retaining a fingerprint of omitted text."""

    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    suffix = f" ... [truncated; sha256={_sha256(value)[:16]}]"
    return value[:max(0, limit - len(suffix))].rstrip() + suffix


def _sync_file(descriptor):
    """Flush one file to the disk itself: F_FULLFSYNC on macOS, where fsync() stops at the drive's cache."""

    try:
        import fcntl
        fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)
    except (ImportError, AttributeError, OSError):
        os.fsync(descriptor)


def _sync_folder(folder):
    """Make a rename into `folder` durable where the platform can sync a folder."""

    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _private_atomic_write(path, content, *, staging_directory=None):
    """Atomically replace one private run-local UTF-8 state file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = path.parent
    if staging_directory is not None:
        staging = Path(staging_directory).resolve()
        workspace = path.parent.resolve()
        if staging == workspace or workspace in staging.parents:
            raise Error("Protected state must be staged outside the writable run workspace.")
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", delete=False,
            dir=staging, prefix=f".{path.name}.", suffix=".tmp",
        ) as stream:
            temporary = Path(stream.name)
            # Set the mode through the descriptor: a path in the author's
            # workspace could be swapped for a link to an outside file.
            try:
                os.fchmod(stream.fileno(), 0o600)
            except (AttributeError, OSError):
                pass  # mkstemp already created the file owner-only.
            stream.write(content)
            # A power loss must not leave an empty checkpoint in place of the previous one.
            stream.flush()
            _sync_file(stream.fileno())
        temporary.replace(path)
        _sync_folder(path.parent)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _strip(value):
    if not isinstance(value, str):
        raise ValueError("strip() requires a string")
    return value.strip()


class _EmptyText(ValueError):
    """text() met an empty string: the answer left a required text empty (the rule is not met)."""


def _text(value):
    # Workflow expressions report ValueError, which evaluate() wraps; direct
    # Python callers keep text() and its statement-specific Error.
    if not isinstance(value, str):
        raise ValueError("text() requires nonempty text")
    if not value.strip():
        raise _EmptyText("text() requires nonempty text")
    return value.strip()


def _get(mapping, key, default=None):
    if not isinstance(mapping, dict):
        raise ValueError("get() requires a dictionary")
    return mapping.get(key, default)


class _RuleProblem(ValueError):
    """A rule function found the answer unacceptable; its message says why, for the model's retry."""


# What JSON escapes leave of a LaTeX command whose backslash was not doubled:
# a backspace (\begin), a form feed (\frac), a lone carriage return (\right),
# or a tab or line break before the rest of \textbf or \newline.
_LATEX_ESCAPE_DAMAGE = re.compile(
    r"[\x08\x0c]|\r(?!\n)"
    r"|\t(?=(?:ext(?:bf|it|rm|tt|sf|sc|sl|up|md|normal|width|height|style|color)|heta|imes|ilde|riangle|itle"
    r"|ableofcontents|oday|hanks|hispagestyle|frac|herefore|iny|op|au|o)(?![A-Za-z])|ext\{)"
    r"|\n(?=(?:ew(?:line|command|theorem|page|environment|block|counter|length)|oindent|abla|otin|onumber"
    r"|olimits|ormalsize|atural|obreak|opagebreak|ocite|oexpand|umberwithin)(?![A-Za-z]))")
_LATEX_ESCAPES = {"\x08": ("a backspace", "b"), "\x0c": ("a form feed", "f"), "\r": ("a carriage return", "r"),
                  "\t": ("a tab", "t"), "\n": ("a line break", "n")}
# A comment standing in for omitted text: "% ... rest unchanged ...".
_LATEX_ELISION = re.compile(r"\b(?:unchanged|omitted|elided|as before|same as (?:before|above)|rest of|remainder"
                            r"|truncated|abbreviated|for brevity|snip)\b", re.IGNORECASE)
_THEOREM_ENVIRONMENTS = {"theorem", "lemma", "proposition", "corollary", "claim", "definition", "remark", "example",
                         "conjecture", "fact", "observation", "proof", "assumption", "notation", "problem",
                         "question", "exercise", "construction", "hypothesis", "property", "algorithm"}


def _latex_elisions(text):
    return [comment.strip() for comment in re.findall(r"(?<!\\)%(.*)", text)
            if re.search(r"\.\.\.|…|[\[(]", comment) and _LATEX_ELISION.search(comment)]


def _latex_intact(text, source=""):
    """Whether a LaTeX answer keeps its commands; _RuleProblem says what is wrong.

    It rejects characters that JSON escapes made of commands (I-029) and
    comments in place of text (I-051) that `source`, the answer's input, does
    not have already.
    """

    if not isinstance(text, str) or not isinstance(source, str):
        raise ValueError("latex_intact() requires text")
    examples = []
    for match in _LATEX_ESCAPE_DAMAGE.finditer(text):
        what, letter = _LATEX_ESCAPES[match.group()]
        rest = re.match(r"[A-Za-z]*", text[match.end():]).group()
        if match.group() + rest not in source:
            examples.append(f'{what} before "{rest}" where \\{letter}{rest} was meant' if rest else what)
        if len(examples) == 3:
            break
    if examples:
        raise _RuleProblem(f"the latex field has characters that JSON escapes made of LaTeX commands "
                           f"({'; '.join(examples)}): write each LaTeX backslash as two backslashes in the JSON string")
    elided = [comment for comment in _latex_elisions(text) if comment not in _latex_elisions(source)]
    if elided:
        raise _RuleProblem(f'the latex field has a comment in place of text ("% {_clipped(elided[0], 120)}"): '
                           "return the complete document")
    return True


def _latex_kept(text, reference=""):
    """latex_intact() against `reference`, the editor's version, which must also keep its content.

    A repair may lose at most a tenth of the editor's text (and 500
    characters), of its theorem-like environments and of its labels (I-051).
    Commented-out text still counts as kept.
    """

    _latex_intact(text, reference)
    if not reference.strip():
        return True
    lost = []
    size, kept = len(re.sub(r"\s+", "", reference)), len(re.sub(r"\s+", "", text))
    if size - kept > max(0.1 * size, 500):
        lost.append(f"it keeps {round(100 * kept / size)}% of the text")
    names = _THEOREM_ENVIRONMENTS | set(re.findall(r"\\newtheorem\*?\{([^}]+)\}", reference))
    environments = [len([name for name in re.findall(r"\\begin\{([^}*]+)\*?\}", value) if name in names])
                    for value in (reference, text)]
    if environments[0] - environments[1] > max(1, environments[0] // 10):
        lost.append(f"{environments[0] - environments[1]} of its {environments[0]} theorem-like environments are missing")
    labels = set(re.findall(r"\\label\{([^}]*)\}", reference))
    missing = sorted(labels - set(re.findall(r"\\label\{([^}]*)\}", text)))
    if len(missing) > max(1, len(labels) // 10):
        lost.append(f"{len(missing)} of its {len(labels)} labels are missing, such as {', '.join(missing[:3])}")
    if lost:
        raise _RuleProblem(f"the latex field drops content of the editor's version ({'; '.join(lost)}): keep all of "
                           "the document's content and fix only the compilation errors")
    return True


_EXPRESSION_FUNCTIONS = {
    "len": len, "str": str, "bool": bool, "int": int, "float": float,
    "all": all, "any": any, "strip": _strip, "text": _text,
    "is_string": lambda value: isinstance(value, str),
    "is_bool": lambda value: isinstance(value, bool),
    "is_list": lambda value: isinstance(value, list),
    "is_dict": lambda value: isinstance(value, dict),
    "get": _get, "normalized_text": normalized_text, "json": lambda value: json.dumps(value, ensure_ascii=False, indent=2),
    "latex_intact": _latex_intact, "latex_kept": _latex_kept,
}
_EXPRESSION_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
}
_EXPRESSION_COMPARE = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
}


def _check_expression_node(node):
    """Accept only syntax that the interpreter below explicitly implements."""

    if isinstance(node, ast.Constant):
        if node.value is not None and type(node.value) not in {str, int, float, bool}:
            raise ValueError("only text, numbers, booleans, and None are constants")
        children = []
    elif isinstance(node, ast.Name):
        children = []
    elif isinstance(node, ast.Attribute):
        children = [node.value]
    elif isinstance(node, ast.Subscript):
        children = [node.value, node.slice]
    elif isinstance(node, (ast.List, ast.Tuple)):
        children = node.elts
    elif isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise ValueError("dictionary unpacking is unsupported")
        children = [*node.keys, *node.values]
    elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        children = node.values
    elif isinstance(node, ast.Compare) and all(
        type(operation) in _EXPRESSION_COMPARE for operation in node.ops
    ):
        children = [node.left, *node.comparators]
    elif isinstance(node, ast.IfExp):
        children = [node.test, node.body, node.orelse]
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.UAdd, ast.USub)):
        children = [node.operand]
    elif isinstance(node, ast.BinOp) and type(node.op) in _EXPRESSION_BINARY:
        children = [node.left, node.right]
    elif isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id not in _EXPRESSION_FUNCTIONS
            or node.keywords
        ):
            raise ValueError("calls require a permitted pure function and positional arguments")
        children = node.args
    elif isinstance(node, (ast.ListComp, ast.GeneratorExp)):
        if len(node.generators) != 1:
            raise ValueError("comprehensions support one generator")
        generator = node.generators[0]
        if generator.is_async or not isinstance(generator.target, ast.Name):
            raise ValueError("comprehensions require one simple variable")
        children = [generator.iter, *generator.ifs, node.elt]
    else:
        raise ValueError(f"unsupported syntax: {type(node).__name__}")
    for child in children:
        _check_expression_node(child)


@functools.lru_cache(maxsize=512)
def _parse_expression(expression):
    try:
        parsed = ast.parse(expression, mode="eval")
        _check_expression_node(parsed.body)
        return parsed.body
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError(f"Invalid workflow expression {expression!r}: {exc}") from exc


def check_expression(expression):
    """Validate allowed expression syntax without resolving context names.

    Return the parsed expression body for callers that need to inspect names.
    No Python bytecode is compiled or evaluated.
    """

    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("A workflow expression must be a nonempty string")
    return _parse_expression(expression)


def _expression_field(value, key):
    if not isinstance(value, dict):
        raise ValueError(f"Field {key!r} requires a dictionary")
    if key not in value:
        raise ValueError(f"Missing expression key: {key!r}")
    return value[key]


def _evaluate_expression_node(node, context):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise ValueError(f"Unknown expression name: {node.id!r}")
        return context[node.id]
    if isinstance(node, ast.Attribute):
        return _expression_field(_evaluate_expression_node(node.value, context), node.attr)
    if isinstance(node, ast.Subscript):
        value = _evaluate_expression_node(node.value, context)
        key = _evaluate_expression_node(node.slice, context)
        if isinstance(value, dict):
            return _expression_field(value, key)
        if not isinstance(value, (list, tuple, str)):
            raise ValueError("Subscripts require a dictionary, list, tuple, or string")
        return value[key]
    if isinstance(node, (ast.List, ast.Tuple)):
        result = [_evaluate_expression_node(item, context) for item in node.elts]
        return tuple(result) if isinstance(node, ast.Tuple) else result
    if isinstance(node, ast.Dict):
        return {
            _evaluate_expression_node(key, context): _evaluate_expression_node(value, context)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.BoolOp):
        for item in node.values:
            value = _evaluate_expression_node(item, context)
            if isinstance(node.op, ast.And) and not value:
                return value
            if isinstance(node.op, ast.Or) and value:
                return value
        return value
    if isinstance(node, ast.Compare):
        left = _evaluate_expression_node(node.left, context)
        for operation, right_node in zip(node.ops, node.comparators):
            right = _evaluate_expression_node(right_node, context)
            if not _EXPRESSION_COMPARE[type(operation)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        selected = node.body if _evaluate_expression_node(node.test, context) else node.orelse
        return _evaluate_expression_node(selected, context)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_expression_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not value
        return +value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        return _EXPRESSION_BINARY[type(node.op)](
            _evaluate_expression_node(node.left, context),
            _evaluate_expression_node(node.right, context),
        )
    if isinstance(node, ast.Call):
        return _EXPRESSION_FUNCTIONS[node.func.id](
            *(_evaluate_expression_node(argument, context) for argument in node.args)
        )
    if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
        generator = node.generators[0]
        iterable = _evaluate_expression_node(generator.iter, context)

        def values():
            for item in iterable:
                local = {**context, generator.target.id: item}
                if all(_evaluate_expression_node(condition, local) for condition in generator.ifs):
                    yield _evaluate_expression_node(node.elt, local)

        return list(values()) if isinstance(node, ast.ListComp) else values()
    # check_expression rejects everything else before evaluation starts.
    raise ValueError(f"Unsupported expression node: {type(node).__name__}")


def evaluate(expression, context):
    """Evaluate a checked, data-only expression against a dictionary context."""

    node = check_expression(expression)
    if not isinstance(context, dict):
        raise ValueError("Expression context must be a dictionary")
    try:
        return _evaluate_expression_node(node, context)
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, RecursionError) as exc:
        raise ValueError(f"Cannot evaluate workflow expression {expression!r}: {exc}") from exc


def _result_field(node):
    """The answer field X of `result.X` (or `result['X']`), else None."""

    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "result":
        return node.attr
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "result"
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
        return node.slice.value
    return None


def _nonempty_field(node):
    """X when `node` holds exactly when answer field X is not empty (strip, bool, len, != '')."""

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1:
        if node.func.id in {"bool", "strip", "text", "len"}:
            return _nonempty_field(node.args[0])
        return None
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        right = node.comparators[0]
        if isinstance(right, ast.Constant):
            if isinstance(node.ops[0], ast.NotEq) and right.value in {"", 0, None}:
                return _nonempty_field(node.left)
            if isinstance(node.ops[0], ast.Gt) and right.value == 0:
                return _nonempty_field(node.left)
            if isinstance(node.ops[0], ast.GtE) and right.value == 1:
                return _nonempty_field(node.left)
        return None
    return _result_field(node)


def _changed_field(node):
    """(X, True) for `normalized_text(result.X) != normalized_text(state.X)`, (X, False) for `==`, else None."""

    if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq))):
        return None
    sides = [node.left, node.comparators[0]]
    if not all(isinstance(side, ast.Call) and isinstance(side.func, ast.Name) and side.func.id == "normalized_text"
               and len(side.args) == 1 and isinstance(side.args[0], ast.Attribute)
               and isinstance(side.args[0].value, ast.Name) for side in sides):
        return None
    answer, saved = sides[0].args[0], sides[1].args[0]
    if answer.value.id != "result" or saved.value.id != "state" or answer.attr != saved.attr:
        return None
    return answer.attr, isinstance(node.ops[0], ast.NotEq)


def _requirement_phrase(node, schema):
    if (isinstance(node, ast.IfExp) and isinstance(node.orelse, ast.Constant) and node.orelse.value is True
            and _changed_field(node.test)):
        # "the fixes field must not be empty when you changed the solution text": a rule only for an answer
        # that changed (or kept) the text it reviewed (_accept_node_answer names an edited file instead).
        when = _requirement_phrase(node.body, schema)
        field, differs = _changed_field(node.test)
        return (f"{when} when you changed the {field} text" if differs
                else f"{when} when you left the {field} text unchanged") if when else None
    field = _nonempty_field(node)
    if field:
        return f"the {field} field must not be empty"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        field = _nonempty_field(node.operand)
        return f"the {field} field must be empty" if field else None
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        parts = [_requirement_phrase(value, schema) for value in node.values]
        return " and ".join(parts) if all(parts) else None
    if (isinstance(node, ast.Compare) and len(node.ops) == 1 and _result_field(node.left)
            and isinstance(node.comparators[0], ast.Constant)):
        field, value = _result_field(node.left), node.comparators[0].value
        if isinstance(node.ops[0], ast.Eq):
            return f"the {field} field must be {value}"
        if isinstance(node.ops[0], ast.NotEq):
            return f"the {field} field must not be {value!r}" if value != "" else f"the {field} field must not be empty"
        return None
    if isinstance(node, ast.IfExp):
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                and _result_field(test.left) and isinstance(test.comparators[0], ast.Constant)):
            return None
        selector, value = _result_field(test.left), test.comparators[0].value
        when, otherwise = _requirement_phrase(node.body, schema), _requirement_phrase(node.orelse, schema)
        if not when or not otherwise:
            return None
        choices = ((schema or {}).get("properties", {}).get(selector) or {}).get("enum")
        others = [choice for choice in choices or () if choice != value]
        condition = f"when {selector} is {others[0]}" if len(others) == 1 else "otherwise"
        same = re.match(r"the (\w+) field must (be empty|not be empty)$", when), re.match(
            r"the (\w+) field must (be empty|not be empty)$", otherwise)
        if all(same) and same[0][1] == same[1][1]:
            # "the bugs field must be empty when verdict is pass and non-empty when verdict is reject"
            words = {"be empty": "empty", "not be empty": "non-empty"}
            return (f"the {same[0][1]} field must be {words[same[0][2]]} when {selector} is {value} "
                    f"and {words[same[1][2]]} {condition}")
        return f"{when} when {selector} is {value}, and {otherwise} {condition}"
    return None


def _branch_guidance(node, schema, answer):
    """For a rule that picks an emptiness requirement by a selector field, what the answer must change.

    The critic's rule makes bugs empty for verdict pass and non-empty for
    reject. The guidance names the answer's own choice and keeps it: a
    reject without bugs is asked for its bugs (never nudged to pass), and a
    pass that lists bugs is asked to decide which of the two it means.
    """

    if not (isinstance(node, ast.IfExp) and isinstance(answer, dict)):
        return ""
    test = node.test
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and _result_field(test.left) and isinstance(test.comparators[0], ast.Constant)):
        return ""
    selector, value = _result_field(test.left), test.comparators[0].value
    branch = node.body if answer.get(selector) == value else node.orelse
    phrase = _requirement_phrase(branch, schema) or ""
    match = re.match(r"the (\w+) field must (be empty|not be empty)$", phrase)
    if not match or not isinstance(answer.get(selector), str):
        return ""
    field, chosen = match[1], answer[selector]
    if match[2] == "not be empty":
        return (f" (your answer has {selector} {chosen} and an empty {field} field: keep {selector} {chosen} "
                f"and fill in the {field} field)")
    choices = ((schema or {}).get("properties", {}).get(selector) or {}).get("enum") or ()
    others = [choice for choice in choices if choice != chosen]
    alternative = f"; if what you wrote in {field} still applies, the {selector} should be {others[0]}" \
        if len(others) == 1 else ""
    return (f" (your answer has {selector} {chosen} and a non-empty {field} field: with {selector} {chosen} "
            f"the {field} field must be empty{alternative})")


def describe_requirement(expression, schema=None, answer=None):
    """Say in plain words what one `require` rule demands of a model answer.

    `bool(strip(result.solution))` is "the solution field must not be empty";
    a rule it cannot phrase is quoted. With the rejected ``answer``, a rule
    that depends on another field also says what that answer chose and how
    to correct it (see _branch_guidance).
    """

    try:
        node = check_expression(expression)
        phrase = _requirement_phrase(node, schema)
        guidance = _branch_guidance(node, schema, answer) if phrase else ""
    except (ValueError, RecursionError):
        phrase, guidance = None, ""
    return (phrase + guidance) if phrase else f"the answer must satisfy the rule {expression}"


def _template_parts(template, roots=None):
    """Validate template syntax and, when known, the available input names."""

    if not isinstance(template, str):
        raise ValueError("A prompt template must be text")
    parts = list(string.Formatter().parse(template))
    for _, field, specification, conversion in parts:
        if field is None:
            continue
        if conversion is not None or specification:
            raise ValueError("Prompt fields cannot use conversions or format specifications")
        names = field.split(".")
        if not all(name.isidentifier() for name in names):
            raise ValueError(f"Invalid prompt field: {field!r}")
        if roots is not None and names[0] not in roots:
            raise ValueError(f"Unknown prompt input: {names[0]!r}")
    return parts


def render_template(template, values):
    """Replace simple fields once, treating inserted prompt text literally.

    Dot-separated fields traverse dictionaries only. Escaped {{ and }} retain
    the ordinary Python-template meaning, but conversions and format specs are
    deliberately unavailable.
    """

    if not isinstance(template, str) or not isinstance(values, dict):
        raise ValueError("A prompt template must be text with dictionary values")
    try:
        pieces = []
        for literal, field, _, _ in _template_parts(template, values):
            pieces.append(literal)
            if field is None:
                continue
            value = values
            for name in field.split("."):
                value = _expression_field(value, name)
            pieces.append(str(value))
        return "".join(pieces)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f"Cannot render workflow prompt: {exc}") from exc


def _check_actions(actions):
    if not isinstance(actions, list):
        raise ValueError("Node actions must be a list.")
    for action in actions:
        if not isinstance(action, dict) or set(action) - {"when", "set", "merge", "emit", "write", "command"}:
            raise ValueError("Unknown workflow action.")
        if not (set(action) - {"when"}):
            raise ValueError("A guarded action needs an operation.")
        if "when" in action:
            check_expression(action["when"])
        if "merge" in action:
            check_expression(action["merge"])
        if "command" in action:
            config = action["command"]
            if not isinstance(config, dict) or not {"argv", "timeout", "result"} <= config.keys() or set(config) - {"argv", "cwd", "env", "timeout", "produces", "log", "result", "requires"}:
                raise ValueError("A command needs argv, timeout, and a result state key.")
            if not isinstance(config["argv"], list) or not config["argv"] or not all(isinstance(arg, str) and arg for arg in config["argv"]):
                raise ValueError("Command argv must be a nonempty list of strings.")
            if not isinstance(config.get("requires", []), list) or not all(isinstance(name, str) and name for name in config.get("requires", [])):
                raise ValueError("Command requires must be a list of executable names.")
            if type(config["timeout"]) not in {int, float} or not math.isfinite(config["timeout"]) or config["timeout"] <= 0:
                raise ValueError("Command timeout must be positive and finite.")
            if not all(isinstance(config.get(key, "."), str) and config.get(key, ".") for key in ("cwd", "log", "result")):
                raise ValueError("Command paths and result must be nonempty strings.")
            if not isinstance(config.get("env", {}), dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in config.get("env", {}).items()):
                raise ValueError("Command env must map strings to strings.")
            outputs = config.get("produces", [])
            if not isinstance(outputs, list) or not all(isinstance(path, str) and path and not Path(path).is_absolute() and ".." not in Path(path).parts for path in outputs):
                raise ValueError("Command produces must name relative output files.")
            for value in [*config["argv"], config.get("cwd", "."), *config.get("env", {}).values()]:
                _template_parts(value, {"state", "result", "raw", "visit", "outcome", "limit"})
        if "set" in action:
            if not isinstance(action["set"], dict) or not all(isinstance(key, str) for key in action["set"]):
                raise ValueError("State assignments must be a named mapping.")
            for expression in action["set"].values():
                check_expression(expression)
        if "emit" in action:
            if not isinstance(action["emit"], dict) or not isinstance(action["emit"].get("kind"), str):
                raise ValueError("An emitted event needs a kind.")
            for value in action["emit"].values():
                if isinstance(value, str) and value.startswith("="):
                    check_expression(value[1:])
                elif isinstance(value, str):
                    _template_parts(value, {"state", "result", "raw", "visit", "outcome", "limit"})
        if "write" in action:
            entry = action["write"]
            if not isinstance(entry, dict) or set(entry) != {"path", "text"} or not isinstance(entry["path"], str):
                raise ValueError("A write action needs path and text.")
            check_expression(entry["text"])


def _response_fields(fields):
    """Expand a concise field map into an ordinary, closed JSON object schema."""

    if not isinstance(fields, dict) or not all(isinstance(name, str) for name in fields):
        raise ValueError("Response fields must be a named mapping.")
    return {
        "type": "object",
        "properties": {name: _response_type(value) for name, value in fields.items()},
        "required": list(fields),
        "additionalProperties": False,
    }


def _response_type(description):
    if isinstance(description, str) and description in {"string", "boolean", "integer", "number", "null"}:
        return {"type": description}
    if isinstance(description, list) and description and all(isinstance(value, str) for value in description):
        return {"type": "string", "enum": description}
    if isinstance(description, dict):
        if set(description) == {"fields"}:
            return _response_fields(description["fields"])
        if "items" in description and not set(description) - {"items", "minItems", "maxItems"}:
            schema = {"type": "array"}
            for bound in ("minItems", "maxItems"):
                if bound in description:
                    value = description[bound]
                    if type(value) is not int or value < 0:
                        raise ValueError(f"Response {bound} must be a nonnegative integer.")
                    schema[bound] = value
            if schema.get("minItems", 0) > schema.get("maxItems", float("inf")):
                raise ValueError("Response minItems cannot exceed maxItems.")
            schema["items"] = _response_type(description["items"])
            return schema
    raise ValueError(f"Invalid response type: {description!r}. Use a scalar type, string enum, fields, or items.")


def _check_request_options(node):
    if "attempts" in node and (type(node["attempts"]) is not int or node["attempts"] < 1):
        raise ValueError("Request attempts must be a positive integer.")
    overrides = node.get("provider_options", {})
    if not isinstance(overrides, dict):
        raise ValueError("Provider options must be a mapping.")
    for provider, settings in overrides.items():
        if not isinstance(provider, str) or not isinstance(settings, dict) or set(settings) - {"effort", "timeout"}:
            raise ValueError("Provider options allow effort and timeout.")
        if "effort" in settings:
            chosen_effort(settings["effort"])
        if "timeout" in settings and (type(settings["timeout"]) not in {int, float} or not math.isfinite(settings["timeout"]) or settings["timeout"] <= 0):
            raise ValueError("Request timeout must be a positive finite number.")


def _check_subagents(config, name):
    """Validate a node's subagent count: a number, or the option that sets it with its default and minimum.

    Every count is 0 (no subagents) to MAX_CRITIC_COUNT; see subagent_count().
    """

    if isinstance(config, int) and not isinstance(config, bool):
        config = {"default": config}
    if (not isinstance(config, dict) or set(config) - {"option", "default", "minimum"} or "default" not in config
            or ("option" in config and (not isinstance(config["option"], str) or not config["option"].isidentifier()))):
        raise ValueError(f"Node {name} subagents need a number, or an option name with its default.")
    minimum = config.get("minimum", 0)
    for value in (config["default"], minimum):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_CRITIC_COUNT:
            raise ValueError(f"Node {name} subagents must be whole numbers from 0 to {MAX_CRITIC_COUNT}.")
    if config["default"] < minimum:
        raise ValueError(f"Node {name} subagents default to fewer than their minimum.")


def _expand_node(node):
    """Desugar authoring shortcuts; execution uses the original node format."""

    bindings = node.get("inputs")
    if isinstance(bindings, dict) and "proof_handoff" in bindings:
        # A reserved constant input keeps deployed YAML readable by older
        # controllers while new workers normalize it before prompt handling.
        if "proof_handoff" in node:
            raise ValueError("Choose top-level or input proof_handoff, not both.")
        expression = bindings["proof_handoff"]
        try:
            if node.get("run") != "structured" or not isinstance(expression, str):
                raise ValueError("Invalid compatibility binding.")
            parsed = ast.parse(expression.strip(), mode="eval").body
            if not isinstance(parsed, ast.Dict) or len(parsed.keys) != 2:
                raise ValueError("Expected one constant mapping.")
            handoff = ast.literal_eval(parsed)
            if (set(handoff) != {"candidate", "statement"}
                    or not all(isinstance(value, str) and value.isidentifier() for value in handoff.values())
                    or handoff["candidate"] == handoff["statement"]):
                raise ValueError("Expected distinct state field names.")
        except (ValueError, TypeError, SyntaxError, RecursionError) as exc:
            raise ValueError("Proof handoff input must be a constant mapping of distinct candidate and statement state fields.") from exc
        node["proof_handoff"] = handoff
        node["inputs"] = {key: value for key, value in bindings.items() if key != "proof_handoff"}

    if "response" in node:
        if "schema" in node:
            raise ValueError("Choose response or schema, not both.")
        try:
            node["schema"] = _response_fields(node.pop("response"))
        except RecursionError as exc:
            raise ValueError("Response definition is recursive or too deeply nested.") from exc
    if isinstance(node.get("instructions"), str):
        node["instructions"] = [node["instructions"]]
    if isinstance(node.get("next"), str):
        node["next"] = {"done": node["next"]}
    if node["run"] == "goal":
        node.setdefault("lifecycle", list(_GOAL_LIFECYCLE))
        node.setdefault("stages", dict(_GOAL_STAGES))



def load_workflow(path):
    """Read a data-only graph; node names and outcomes carry no built-in meaning."""

    try:
        workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        raise ValueError(f"Cannot read workflow {path}{where}: {exc}") from exc
    if not isinstance(workflow, dict) or set(workflow) != {"prompts", "nodes"}:
        raise ValueError("A workflow must contain only prompts and nodes.")
    prompts, nodes = workflow["prompts"], workflow["nodes"]
    if not isinstance(prompts, dict) or not all(
        isinstance(name, str) and isinstance(value, str) and value.strip()
        for name, value in prompts.items()
    ):
        raise ValueError("Workflow prompts must be named nonempty strings.")
    if not isinstance(nodes, dict) or not nodes or not all(
        isinstance(name, str) and name and name != "end" for name in nodes
    ):
        raise ValueError("A workflow needs named nodes; 'end' is reserved.")
    targets = set(nodes) | {"end"}
    shared = {"run", "role", "stage", "model", "effort", "prompt", "outcome", "next", "before", "after",
              "workflow_stage", "skip"}
    specific = {
        "structured": {"instructions", "inputs", "schema", "features", "require", "error", "subagents", "attempts", "provider_options", "request_label", "activity_label", "normalize", "proof_handoff", "edit"},
        "goal": {"task", "marker", "lifecycle", "resume", "stages", "recovery", "features", "subagents"},
        "command": {"command", "require", "error"},
    }
    for name, node in nodes.items():
        if not isinstance(node, dict) or not {"run", "next"} <= node.keys():
            raise ValueError(f"Node {name} needs run and next.")
        _expand_node(node)
        kind = node["run"]
        if not isinstance(kind, str) or kind not in specific or set(node) - (shared | specific[kind]):
            raise ValueError(f"Unsupported operation or fields in node {name}.")
        if "workflow_stage" in node or "skip" in node:
            if (node.get("workflow_stage") not in WORKFLOW_STAGES
                    or not isinstance(node.get("skip"), str) or node["skip"] not in targets):
                raise ValueError(f"Node {name} needs a valid workflow_stage and skip target.")
        reference = node.get("prompt")
        references = [] if kind == "command" else [reference]
        if isinstance(reference, dict):
            if set(reference) != {"when", "then", "else"} or kind != "structured":
                raise ValueError(f"Invalid prompt selection in node {name}.")
            check_expression(reference["when"])
            references = [reference["then"], reference["else"]]
        instructions = node.get("instructions", [])
        if not isinstance(instructions, list):
            raise ValueError(f"Node {name} instructions must be prompt names.")
        references += instructions
        for reference in references:
            if not isinstance(reference, str) or reference not in prompts:
                raise ValueError(f"Unknown prompt {reference!r} in node {name}.")
        for field in ("role", "stage", "model", "effort"):
            if field in node and (not isinstance(node[field], str) or not node[field]):
                raise ValueError(f"Node {name} needs a nonempty {field}.")
        if "outcome" in node:
            check_expression(node["outcome"])
        if "subagents" in node:
            _check_subagents(node["subagents"], name)
        _check_actions(node.get("before", []))
        _check_actions(node.get("after", []))
        if kind == "structured":
            _check_request_options(node)
            if "proof_handoff" in node:
                handoff = node["proof_handoff"]
                if (not isinstance(handoff, dict) or set(handoff) != {"candidate", "statement"}
                        or not all(isinstance(value, str) and value.isidentifier() for value in handoff.values())
                        or handoff["candidate"] == handoff["statement"]):
                    raise ValueError("Proof handoff must name distinct candidate and statement state fields.")
            normalized = node.get("normalize", {})
            if not isinstance(normalized, dict) or not all(isinstance(key, str) for key in normalized):
                raise ValueError("Result normalization must map field names to expressions.")
            for expression in normalized.values():
                check_expression(expression)
            if not isinstance(node.get("schema"), dict):
                raise ValueError(f"Structured node {name} needs a response schema.")
            try:
                json.dumps(node["schema"])
                validator_for(node["schema"]).check_schema(node["schema"])
            except (TypeError, ValueError, SchemaError) as exc:
                raise ValueError(f"Invalid JSON schema in node {name}.") from exc
            if "edit" in node:
                # The file the model edits in place; its text becomes result.<field> (see EditableFile).
                edit = node["edit"]
                if (not isinstance(edit, dict) or not {"file", "field"} <= set(edit)
                        or set(edit) - {"file", "field", "input"} or not isinstance(edit["file"], str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", edit["file"])
                        or not isinstance(edit["field"], str) or not edit["field"].isidentifier()
                        or edit["field"] in (node["schema"].get("properties") or {})
                        or ("input" in edit and edit["input"] not in (node.get("inputs") or {}))):
                    raise ValueError(f"Node {name} edit needs a plain file name, a result field outside its "
                                     "response, and optionally one of its inputs as the initial text.")
            for field in ("require", "features"):
                if not isinstance(node.get(field, []), list) or not all(isinstance(item, str) for item in node.get(field, [])):
                    raise ValueError(f"Node {name} {field} must be a list of strings.")
            for expression in node.get("require", []):
                check_expression(expression)
            bindings = node.get("inputs", {})
        elif kind == "command":
            _check_actions([{"command": node.get("command")}])
            if not isinstance(node.get("require", []), list):
                raise ValueError(f"Node {name} require must be a list of expressions.")
            for expression in node.get("require", []):
                check_expression(expression)
            bindings = {}
        else:
            if not isinstance(node.get("features", []), list) or not all(
                isinstance(feature, str) and feature for feature in node.get("features", [])
            ):
                raise ValueError(f"Goal node {name} features must be a list of nonempty strings.")
            for required in ("task", "marker", "lifecycle", "resume"):
                if required not in node:
                    raise ValueError(f"Goal node {name} needs {required}.")
            check_expression(node["task"])
            if not isinstance(node["marker"], str) or not node["marker"]:
                raise ValueError(f"Goal node {name} needs a nonempty marker.")
            lifecycle = node["lifecycle"]
            if isinstance(lifecycle, list) and all(isinstance(key, str) for key in lifecycle):
                lifecycle = {key: key for key in lifecycle}
            required = set(_GOAL_LIFECYCLE)
            if not isinstance(lifecycle, dict) or not required <= set(lifecycle) or set(lifecycle) - required - set(_GOAL_OPTIONAL_LIFECYCLE) or not all(
                isinstance(ref, str) and ref in prompts for ref in lifecycle.values()
            ):
                raise ValueError(f"Goal node {name} must bind its lifecycle prompts.")
            stages = node["stages"]
            if not isinstance(stages, dict) or set(stages) != {"initial", "resume", "failure"} or not all(isinstance(stage, str) and stage for stage in stages.values()):
                raise ValueError(f"Invalid goal stages in node {name}.")
            if "recovery" in node:
                recovery = node["recovery"]
                if not isinstance(recovery, dict) or set(recovery) != {"when", "prompt"} or not isinstance(recovery["prompt"], str) or recovery["prompt"] not in prompts:
                    raise ValueError("Goal recovery needs when and a named prompt.")
                check_expression(recovery["when"])
            bindings = node["resume"]
            if not isinstance(bindings, dict):
                raise ValueError(f"Goal node {name} resume must be a mapping of input names to expressions.")
        if not isinstance(bindings, dict) or not all(isinstance(key, str) for key in bindings):
            raise ValueError(f"Invalid input bindings in node {name}.")
        for expression in bindings.values():
            check_expression(expression)
        branches = node["next"]
        if not isinstance(branches, dict) or not branches or not all(isinstance(key, str) for key in branches):
            raise ValueError(f"Node {name} needs named outcome transitions.")
        for branch in branches.values():
            target = branch
            if isinstance(branch, dict):
                if "repeat" in branch:
                    if (set(branch) - {"repeat", "option", "then", "after", "verify"}
                            or not {"then", "verify"} & set(branch)):
                        raise ValueError(f"Invalid repeat branch in node {name}.")
                    if type(branch["repeat"]) is not int or branch["repeat"] < 1:
                        raise ValueError(f"Invalid repeat limit in node {name}.")
                    if "option" in branch and not isinstance(branch["option"], str):
                        raise ValueError(f"Invalid repeat option in node {name}.")
                    # At the limit, one verify-only visit with this extra instruction (see _execute).
                    if "verify" in branch and (kind != "structured" or not isinstance(branch["verify"], str)
                                               or branch["verify"] not in prompts):
                        raise ValueError(f"Invalid verify prompt in node {name}.")
                    target = branch.get("then", name)
                else:
                    if set(branch) - {"to", "after"} or "to" not in branch:
                        raise ValueError(f"Invalid transition in node {name}.")
                    target = branch["to"]
                _check_actions(branch.get("after", []))
            if not isinstance(target, str) or target not in targets:
                raise ValueError(f"Unknown transition target {target!r} in node {name}.")
    return workflow


# The last version of each built-in workflow that loaded, by path. An edit that
# does not load falls back to it, with a warning (workflow_warnings()); the copy
# of the bundled files beside the private state also serves a fresh start.
_LAST_GOOD_WORKFLOWS = {}
_WORKFLOW_WARNINGS = {}


def _last_good_copy(path):
    """The saved last-good copy of a bundled workflow file, or None for any other folder."""

    path = Path(path)
    if path.parent.resolve() != (ROOT / "workflows").resolve():
        return None
    return providers.state_directory() / providers.LAST_GOOD_WORKFLOWS / path.name


def read_editable_yaml(path, parse):
    """Parse an editable built-in YAML file, falling back to its last version that parsed.

    Return (value, path of the text that was used).
    """

    path = Path(path)
    key = str(path.resolve())
    saved = _last_good_copy(path)
    try:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Cannot read workflow {path}: {exc}") from exc
        value = parse(path)
    except ValueError as exc:
        remembered = _LAST_GOOD_WORKFLOWS.get(key)
        if remembered is None and saved is not None and saved.is_file():
            try:
                remembered = (parse(saved), saved.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                remembered = None
        if remembered is None:
            raise
        _WORKFLOW_WARNINGS[path.name] = (f"{exc} TCS Prover uses the last version of {path.name} that could be "
                                         "read until the file is fixed.")
        return _copy.deepcopy(remembered[0]), saved if saved is not None and saved.is_file() else None
    _WORKFLOW_WARNINGS.pop(path.name, None)
    if _LAST_GOOD_WORKFLOWS.get(key, (None, None))[1] != text:
        _LAST_GOOD_WORKFLOWS[key] = (_copy.deepcopy(value), text)
        if saved is not None:
            try:
                if not saved.is_file() or saved.read_text(encoding="utf-8") != text:
                    _private_atomic_write(saved, text)
            except (OSError, UnicodeError, Error):
                pass  # The copy only helps a later start; this load succeeded.
    return value, path


def workflow_warnings():
    """Why a built-in workflow file currently runs from its last readable version, if it does."""

    return " ".join(_WORKFLOW_WARNINGS[name] for name in sorted(_WORKFLOW_WARNINGS))


def builtin_workflow(name):
    """Read current UI/convenience defaults without caching editable YAML."""

    return read_editable_yaml(WORKFLOWS / f"{name}.yaml", load_workflow)[0]


def builtin_workflow_path(name):
    """The file a launch of a built-in workflow reads: the live one, or its last readable copy."""

    used = read_editable_yaml(WORKFLOWS / f"{name}.yaml", load_workflow)[1]
    if used is None:
        raise ValueError(_WORKFLOW_WARNINGS.get(f"{name}.yaml") or f"Cannot read workflow {name}.yaml.")
    return used


def __getattr__(name):
    # These public defaults are used by the UI and existing Python callers.
    # The generic execution path never needs to open either bundled YAML file.
    if name == "DEFAULT_CRITIC_ROUNDS":
        return builtin_workflow("author_critic")["nodes"]["critic"]["next"]["fixed"]["repeat"]
    if name == "DEFAULT_CRITIC_COUNT":
        # The critic's subagents: its node's number, or its option's default (see subagent_count).
        return subagent_count(builtin_workflow("author_critic")["nodes"]["critic"], {})
    if name == "DEFAULT_AUTHOR_SUBAGENTS":
        return subagent_count(builtin_workflow("author_critic")["nodes"]["author"], {})
    if name == "FINAL_PROMPT":
        return builtin_workflow("clean_up")["prompts"]["final"]
    raise AttributeError(name)


# The run options that set a node's number of subagents, with the check that names their setting.
SUBAGENT_OPTIONS = {"critic_count": critic_count, "author_subagents": author_subagents}


def subagent_count(node, options):
    """How many subagents a node's model may start at once: its `subagents` number, or the run option that sets
    it (else its default); None for a node that declares none.

    A known option is checked as its setting is (critic_count(), author_subagents()); every count lies
    between the node's minimum and MAX_CRITIC_COUNT, else Error before any model starts.
    """

    config = node.get("subagents")
    if config is None:
        return None
    if not isinstance(config, dict):
        config = {"default": config}
    option = config.get("option")
    value = options.get(option, config["default"]) if option else config["default"]
    if option in SUBAGENT_OPTIONS:
        value = SUBAGENT_OPTIONS[option](value)
    minimum = config.get("minimum", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_CRITIC_COUNT:
        raise Error(f"Choose {minimum} to {MAX_CRITIC_COUNT} subagents.")
    return value


def node_features(node, options):
    """A node's features and its number of subagents (subagent_count()).

    A node that declares subagents gets the multi_agent feature exactly when that number is 1 or more: the
    run's setting decides, not the node's feature list.
    """

    features = list(node.get("features", []))
    count = subagent_count(node, options)
    if count is not None:
        features = [feature for feature in features if feature != "multi_agent"] + (["multi_agent"] if count else [])
    return features, count


def _settings(node, options):
    role = node.get("role", "")
    return {
        "model": chosen_model(options.get(f"{role}_model") or node.get("model") or options.get("model", MODEL)),
        "effort": chosen_effort(options.get(f"{role}_effort") or node.get("effort") or options.get("effort", EFFORT)),
        "speed": chosen_speed(options.get("speed", DEFAULT_SPEED)),
        "summary": chosen_reasoning_summary(options.get("summary", DEFAULT_REASONING_SUMMARY)),
    }


WORKFLOW_STAGES = ("review", "author", "critic", "writing")


def normalize_workflow_stages(value=None):
    """The four application stages are enabled unless explicitly deselected."""

    if value is None:
        return dict.fromkeys(WORKFLOW_STAGES, True)
    if not isinstance(value, dict) or any(
        name not in WORKFLOW_STAGES or type(enabled) is not bool
        for name, enabled in value.items()
    ):
        raise ValueError("Workflow stages must map review, author, critic and writing to enabled or disabled.")
    stages = {**dict.fromkeys(WORKFLOW_STAGES, True), **value}
    if not any(stages.values()):
        raise ValueError("Enable at least one workflow stage.")
    return stages


def _stage_enabled(node, options):
    name = node.get("workflow_stage")
    return name is None or normalize_workflow_stages(options.get("workflow_stages"))[name]


def _selected_entry(nodes, current, options):
    """Follow declared skip edges without executing disabled nodes or their actions."""

    skipped = set()
    while current != "end" and not _stage_enabled(nodes[current], options):
        if current in skipped:
            raise ValueError("Disabled workflow stages form a cycle without an enabled step.")
        skipped.add(current)
        current = nodes[current]["skip"]
    return current


def _node_prompt_names(node):
    reference = node.get("prompt")
    names = {reference} if isinstance(reference, str) else {
        reference[key] for key in ("then", "else")
    } if isinstance(reference, dict) else set()
    instructions = node.get("instructions", [])
    names.update([instructions] if isinstance(instructions, str) else instructions)
    lifecycle = node.get("lifecycle", {})
    names.update(lifecycle if isinstance(lifecycle, list) else lifecycle.values())
    if node.get("recovery"):
        names.add(node["recovery"]["prompt"])
    branches = node.get("next")
    if isinstance(branches, dict):
        # The instruction of a repeat branch's verify-only visit (see _execute).
        names.update(branch["verify"] for branch in branches.values()
                     if isinstance(branch, dict) and isinstance(branch.get("verify"), str))
    return names


def prepare(workflow, options):
    """Resolve prompt overrides and validate every node before starting any model."""

    normalize_workflow_stages(options.get("workflow_stages"))
    enabled = [node for node in workflow["nodes"].values() if _stage_enabled(node, options)]
    active_prompts = set().union(*(_node_prompt_names(node) for node in enabled))
    inactive_prompts = set().union(*(_node_prompt_names(node) for node in workflow["nodes"].values())) - active_prompts
    prompts = dict(workflow["prompts"])
    prompts.update({name: value for name, value in options.get("prompts", {}).items()
                    if name in prompts and name not in inactive_prompts})
    if not all(isinstance(value, str) and value.strip() for value in prompts.values()):
        raise ValueError("Prompt overrides must be nonempty strings.")
    for node in enabled:
        if node["run"] == "command":
            continue
        settings = _settings(node, options)
        require_model_credentials(settings["model"])
        # The number of subagents the run's options give.
        count = subagent_count(node, options)
        if node["run"] == "structured":
            if count is not None:
                # The instructions of a node with subagents are templates: {count} is that number.
                for name in node.get("instructions", []):
                    _template_parts(prompts[name], {"count"})
            reference = node["prompt"]
            references = [reference["then"], reference["else"]] if isinstance(reference, dict) else [reference]
            for reference in references:
                _template_parts(prompts[reference], set(node.get("inputs", {})) | {"instructions"})
        else:
            if options.get("author_input") is None and prompts[node["prompt"]].count(node["marker"]) != 1:
                raise ValueError(f"Goal prompt must contain exactly one {node['marker']}.")
            fields = {"original_prompt", "directory", "solution", "bugs", "round", "revision_number", "instruction"} | set(node["resume"])
            if "recovery" in node:
                _template_parts(prompts[node["recovery"]["prompt"]], fields)
            lifecycle = node["lifecycle"]
            if isinstance(lifecycle, list):
                lifecycle = {key: key for key in lifecycle}
            for key, reference in lifecycle.items():
                # Only the paused author's conversation prompt quotes the user's message, the note on a
                # refused submission its reason, and the subagents note their number (the controller
                # renders these with nothing else).
                _template_parts(prompts[reference], {"reason"} if key == "handoff_refused"
                                else {"author_subagents"} if key == "subagents"
                                else fields | {"message"} if key == "conversation" else fields)
    return prompts


def _instructions(node, prompts, values=None):
    """A node's instructions, in order; with ``values`` (a node with subagents: {count}) each is rendered."""

    instructions = ""
    for name in node.get("instructions", []):
        part = text(prompts[name] if values is None else render_template(prompts[name], values))
        if part not in instructions:
            instructions = instructions + "\n\n" + part if instructions else part
    return instructions


def _request_prompt(node, prompts, context):
    values = {key: evaluate(expression, context) for key, expression in node.get("inputs", {}).items()}
    values["instructions"] = _instructions(node, prompts, {"count": context["count"]} if "count" in context else None)
    reference = node["prompt"]
    if isinstance(reference, dict):
        reference = reference["then"] if evaluate(reference["when"], context) else reference["else"]
    return render_template(prompts[reference], values)


def _structured_options(node, options):
    settings = _settings(node, options)
    provider = model_provider(settings["model"])
    settings.update(node.get("provider_options", {}).get(provider, {}))
    settings["effort"] = effective_effort(settings["model"], settings["effort"])
    if "attempts" in node:
        settings["attempts"] = node["attempts"]
        if not options.get("_attempts_noted"):
            # Said once per workflow: the key is accepted for older workflows but has no effect.
            options["_attempts_noted"] = True
            emit("diagnostic", node.get("stage", "model"),
                 text="This workflow sets attempts on a structured request. Workflow model calls ignore it: "
                      "every failed attempt is retried each minute until the call succeeds.")
    return settings


def workflow_remaining(options):
    """One elapsed-time budget across proof search, verification, and final editing.

    Waits before a model-call retry are not elapsed work (see RetryClock).
    The limit stops only the author: steps after a submission (options from
    _after_submission) always finish, so an approved proof is never cut off.
    Their time still counts toward the budget of later author rounds.
    """
    started = options.setdefault("_workflow_started", _now())
    if options.get("_after_submission"):
        return math.inf
    hours = controlled_author_hours(options.get("author_limit_file"),
                                   options.get("thinking_hours", DEFAULT_AUTHOR_HOURS))
    if hours == 0:
        return math.inf
    return max(0.0, hours * 3600 - options.get("elapsed_seconds", 0)
               - (_now() - started - retry_clock(options).excluded()))


def _after_submission(options):
    """The options of a step after the author's submission: the same run, without a deadline."""

    return {**options, "_after_submission": True}


def _bounded_request_settings(settings, options):
    """Settings of one workflow model call, bounded by the shared deadline.

    No attempt starts after the deadline and none outlives it: structured()
    recomputes the remaining budget before each attempt. There is no internal
    attempt budget (a node's legacy `attempts` is ignored); every failure is
    retried after MODEL_RETRY_SECONDS, and those waits do not consume the budget.
    A node's `timeout` (provider_options) is its inactivity limit: without one
    an attempt is stopped only after STRUCTURED_STALL_SECONDS without output,
    also when the budget is unlimited, and never while it works.
    """
    remaining = workflow_remaining(options)
    if remaining <= 0:
        raise Error("Workflow time limit reached; saved work remains resumable.")
    settings = {key: value for key, value in settings.items() if key != "attempts"}
    return {**settings, "options": options}


def _accept_node_answer(node, context, input_files, report, raw, edited=None):
    """Apply a node's normalize, schema, proof-pointer and require checks to one answer.

    Runs inside structured()'s retry loop, so an unacceptable answer is
    retried with a plain explanation (InvalidAnswer) instead of failing the run.
    The text of a node's edited file (``edited``, an EditableFile) becomes
    the result field the node's `edit` names, so its rules and actions read
    it like an answer field.
    """
    report = dict(report)
    for key, expression in node.get("normalize", {}).items():
        try:
            report[key] = evaluate(expression, {**context, "result": report, "raw": raw})
        except ValueError as exc:
            # The rule cannot handle a schema-valid answer: a workflow problem,
            # which no retry of the model call can fix.
            raise WorkflowDefinitionError(str(exc)) from exc
    edit, schema = node.get("edit"), node["schema"]
    from_file = (bool(edit) and edited is not None and edited.text is not None
                 and (edited.readonly or edit["field"] not in report))
    if edit:
        if from_file:
            report[edit["field"]] = edited.text
            schema = _with_field(schema, edit["field"])
        else:
            # The text came with the answer: a web-cutoff answer, which structured() already
            # checked against the full schema, or a stand-in for structured(). Only it is required.
            schema = {**_with_field(schema, edit["field"]), "required": [edit["field"]]}
    try:
        validate_json_schema(report, schema)
    except Error as exc:
        if node.get("normalize"):
            # structured() already checked the answer against this schema:
            # the node's own normalize broke it.
            raise WorkflowDefinitionError(str(exc)) from exc
        raise InvalidAnswer(_schema_problem(report, schema) or str(exc), detail=str(exc)) from exc
    # A returned pointer would be resolved against the author's stale file or
    # accepted literally; stop before any action replaces the saved candidate.
    handoff = node.get("proof_handoff")
    returned = report.get(handoff["candidate"]) if handoff and input_files is not None else None
    captured = dict(input_files)["proof.md"] if isinstance(returned, str) else ""
    subject = edit["file"] if from_file else f"the {handoff['candidate']} field" if handoff else ""
    if isinstance(returned, str) and not normalized_text(returned):
        # No proof at all (for example a critic whose subagents all failed).
        raise InvalidAnswer(f"{subject} must not be empty",
                            message="The critic returned no proof text; the captured candidate was kept.")
    if isinstance(returned, str) and normalized_text(returned) != normalized_text(captured):
        # Marker lines kept verbatim from the captured proof are text of an
        # edited proof; only what the critic wrote itself can be a pointer.
        # This includes the near-miss forms a pointer may take ("PROOF_FILE proof.md").
        marker = re.compile(r"^[ \t]*`?PROOF_FILE[ \t]*:")
        inherited = {line.strip() for line in captured.splitlines()
                     if marker.match(line) or proof_handoff.MARKER_LINE.match(line)}
        remainder = "\n".join(line for line in returned.splitlines() if line.strip() not in inherited)
        try:
            pointer = (not normalized_text(remainder)
                       or proof_handoff.extract_proof_reference(remainder) is not None)
        except proof_handoff.ProofHandoffError:
            pointer = True
        if pointer:
            pointer = ("The critic returned a file pointer instead of the complete proof text; "
                       "the captured candidate was kept.")
            raise InvalidAnswer(f"{subject} must contain the complete proof text, not a file pointer",
                                detail=pointer, message=pointer)
    for condition in node.get("require", []):
        try:
            satisfied = evaluate(condition, {**context, "result": report, "raw": raw})
        except ValueError as exc:
            if isinstance(exc.__cause__, _RuleProblem):
                # The rule's function says what is wrong with the answer (latex_intact); for an
                # edited file it names the file the model edits, not the field its text fills.
                problem = str(exc.__cause__)
                if from_file:
                    problem = problem.replace(f"the {edit['field']} field", edit["file"])
                raise InvalidAnswer(problem, rule=condition, detail=node.get("error", ""),
                                    message=node.get("error") or f"Invalid model response: {problem}.") from exc
            if not isinstance(exc.__cause__, _EmptyText):
                # The rule cannot be evaluated on a schema-valid answer (for
                # example it names a field the schema lacks): a workflow
                # problem, which no retry of the model call can fix.
                raise WorkflowDefinitionError(node.get("error", str(exc))) from exc
            satisfied = False  # text() of an empty field: the answer does not meet the rule.
        if not satisfied:
            problem = describe_requirement(condition, node["schema"], report)
            if from_file:
                # The model edits a file, not the field its text fills.
                problem = problem.replace(f"the {edit['field']} text", edit["file"])
            raise InvalidAnswer(problem, rule=condition, detail=node.get("error", ""),
                                message=node.get("error") or f"Invalid model response: {problem}.")
    return report


def model_context_tokens(model):
    """The context window of `model` in tokens when its settings record one (API models), else None."""

    value = (providers.api_model(model) or {}).get("contextWindow") if providers.is_api_id(model) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _over_byte_limit(size, limit=proof_handoff.MAX_PROOF_BYTES):
    """The one wording of a proof over the handoff byte limit, inline or as a file."""

    return f"the proof is {size:,} bytes, more than the {limit:,} bytes ({limit / 1048576:g} MiB) the critic accepts"


def candidate_size_problem(candidate, model=None):
    """Why a submitted proof is too large to hand to the critic `model`, in plain words, or "".

    The critic edits its copy in small steps, so no output limit applies to the
    proof; it must still be able to read the whole proof at once.
    """

    size = len(candidate.encode("utf-8"))
    if size > proof_handoff.MAX_PROOF_BYTES:
        return _over_byte_limit(size)
    window = model_context_tokens(model) if model else None
    tokens = -(-size // 4)  # About four bytes of English or LaTeX per token.
    if window and tokens > window:
        return (f"the proof is about {tokens:,} tokens ({size:,} bytes), more than the {window:,}-token context "
                f"window of {providers.model_label(model)}")
    return ""


def _prepare_node_files(node, state, visit, workspace=None, resolve_reference=True, model=None):
    """Resolve an explicitly submitted artifact before checkpoints and prompts.

    Only a node's first visit receives a submission; a repeated visit reviews
    the previous answer as text, never the author's possibly stale file. A
    submission that cannot be handed over raises HandoffRefused; with the
    critic's `model` its size is checked too.
    """
    handoff = node.get("proof_handoff")
    if handoff is None:
        return None
    candidate_key, statement_key = handoff["candidate"], handoff["statement"]
    candidate, statement = state.get(candidate_key), state.get(statement_key)
    if not isinstance(candidate, str) or not isinstance(statement, str) or not statement.strip():
        raise Error("Proof handoff needs a candidate and the exact problem statement.")
    captured = state.get("proof_handoff") if isinstance(state.get("proof_handoff"), dict) else {}
    if (((captured.get("files") or {}).get("proof.md") or {}).get("sha256")
            == hashlib.sha256(candidate.encode("utf-8")).hexdigest()):
        # A step resumed from its checkpoint reviews the very text it captured before.
        resolve_reference = False
    try:
        snapshot = (proof_handoff.resolve_author_proof(candidate, workspace or Path.cwd())
                    if resolve_reference else None)
    except proof_handoff.ProofTooLarge as exc:
        raise HandoffRefused("Cannot hand the submitted proof to the critic: "
                             f"{_over_byte_limit(exc.size_bytes, exc.limit)}.") from exc
    except proof_handoff.ProofHandoffError as exc:
        raise HandoffRefused(f"Cannot hand the submitted proof to the critic: {exc}") from exc
    if snapshot is not None:
        candidate = snapshot.text
    if not candidate.strip():
        raise HandoffRefused("Cannot hand an empty proof to the critic.")
    problem = candidate_size_problem(candidate, model) if resolve_reference else ""
    if problem:
        raise HandoffRefused(f"Cannot hand the submitted proof to the critic: {problem}.")
    source = {"kind": "file", **snapshot.receipt()} if snapshot else {"kind": "inline"}
    files = (("proof.md", candidate), ("statement.md", statement))
    manifest = {
        "schemaVersion": 1, "visit": visit, "source": source,
        "files": {name: {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                         "sizeBytes": len(value.encode("utf-8"))} for name, value in files},
    }
    # Repair feedback, saved candidates and exact-proof comparisons all use the
    # captured content. A subsequent review captures any critic/author edits anew.
    state[candidate_key] = candidate
    state["proof_handoff"] = manifest
    emit("proof_handoff", node.get("stage", "model"), label="Submitted proof captured",
         text="Captured the exact candidate and statement for an isolated read-only review.",
         manifest=manifest, round=visit)
    return (*files, ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"))


def _model_call(node, prompts, state, options, visit, input_files=None, verify=None):
    """Run a configured request.

    A node with `subagents` (see subagent_count()) runs one call that may
    start that many subagents at once: its instructions read the number as
    {count}, the CLI's own limits hold it (subagent_limits()), and the call
    reports their progress (SubagentProgress). A node's `edit` file is
    edited in place (see EditableFile); with a `proof_handoff`, the result
    also records whether the proof changed and the diff (see _proof_change).
    A `verify` visit (the prompt a repeat
    branch names) adds that instruction to the node's own instructions only
    and keeps the file read-only, so the visit can only confirm or reject its
    text.
    """
    if verify:
        node = {**node, "instructions": [*node.get("instructions", []), verify]}
    context = {"state": state, "visit": visit}
    features, count = node_features(node, options)
    if count is not None:
        context["count"] = count
    prompt = _request_prompt(node, prompts, context)
    settings = _structured_options(node, options)
    settings.update({key: node[key] for key in ("request_label", "activity_label") if key in node})
    edit, edited = node.get("edit"), None
    if edit:
        initial = (str(evaluate(node["inputs"][edit["input"]], context)) if "input" in edit
                   else dict(input_files or ()).get(edit["file"]))
        edited = EditableFile(edit["file"], edit["field"], initial, readonly=bool(verify))

    accepted = {}

    def accept(report, raw):
        accepted["report"] = _accept_node_answer(node, context, input_files, report, raw, edited)
        return accepted["report"]

    # The node's checks run inside the shared retry loop: an unacceptable
    # answer is retried with a plain explanation, never failing the run.
    report, raw = structured(prompt, node["schema"], node.get("stage", "model"), features=features,
                             validate=accept, **({"input_files": input_files} if input_files is not None else {}),
                             **({"edit_file": edited} if edited is not None and not edited.readonly else {}),
                             **({"subagents": count} if count else {}),
                             **_bounded_request_settings(settings, options))
    if accepted.get("report") is not report:
        # A replacement structured() (such as a test double) skipped the checks.
        report = _accept_node_answer(node, context, input_files, report, raw, edited)
    if edited is not None and node.get("proof_handoff") and isinstance(report.get(edit["field"]), str):
        report = {**report, **_proof_change(edited.initial, report[edit["field"]], options, visit,
                                            node.get("stage", "model"))}
    return report, raw


def _proof_change(before, after, options, visit, stage):
    """Whether a reviewing call changed the submitted proof, and the unified diff of its changes.

    The texts are compared as the critic's outcome compares them
    (normalized_text): spacing at line ends and blank lines around the proof
    are no change. The diff joins the result as `proofDiff`. A diff longer
    than PROOF_DIFF_INLINE_CHARS is saved whole in the folder the
    critic_diff_dir option names, and the result names that file and says why
    (`proofDiffFile`, `proofDiffNote`); without the option, or when the file
    cannot be written, the whole diff stays in the result.
    """

    before, after = normalized_text(before or ""), normalized_text(after)
    if before == after:
        return {"proofChanged": False, "proofDiff": ""}
    diff = "\n".join(difflib.unified_diff(before.split("\n"), after.split("\n"), "submitted proof",
                                          "coordinator's proof", lineterm=""))
    change = {"proofChanged": True, "proofDiff": diff, "proofDiffLines": diff.count("\n") + 1}
    folder = options.get("critic_diff_dir")
    if len(diff) <= PROOF_DIFF_INLINE_CHARS or not folder:
        return change
    name = f"round-{visit}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(_wall_clock()))}-{uuid.uuid4().hex[:8]}.diff"
    try:
        _private_atomic_write(Path(folder) / name, diff + "\n")
    except (OSError, UnicodeError, Error) as exc:
        emit("diagnostic", stage, text=f"Could not save the {len(diff):,}-character diff of the coordinator's "
                                       f"changes to a file ({exc}); the whole diff stays in the critic result.")
        return change
    return {**change, "proofDiff": "", "proofDiffFile": name, "proofDiffChars": len(diff),
            "proofDiffNote": (f"The coordinator's changes are {change['proofDiffLines']:,} diff lines "
                              f"({len(diff):,} characters), too long to show here. The whole diff is saved with "
                              "this run's private controller files and can be downloaded.")}


def _command_descendants(root):
    """List the descendants of one POSIX process, parents before children."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children = {}
    for line in listing.splitlines():
        try:
            pid, parent = (int(value) for value in line.split())
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
    found, pending = [], [root]
    while pending:
        for pid in children.get(pending.pop(0), []):
            if pid != root and pid not in found:
                found.append(pid)
                pending.append(pid)
    return found


def _stop_descendants(root):
    """Kill every descendant of a running POSIX process, which itself keeps running (see stop_process).

    As in _stop_command_tree, parents are frozen before their children, and
    the tree is listed again until nothing new appears, before anything dies.
    """
    frozen, pending = [], _command_descendants(root)
    while pending and len(frozen) < 1000:
        for pid in pending:
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:
                pass
        frozen.extend(pending)
        pending = [pid for pid in _command_descendants(root) if pid not in frozen]
    for pid in frozen:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _stop_command_tree(process):
    """Kill a command and every process it started.

    Commands stay in the runner's process group so that stopping the job still
    reaches them; a timeout therefore has to stop the command's own tree.
    """
    if process.poll() is not None:
        return  # Never signal a reaped process ID; its children were reparented anyway.
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        # Stop parents before children so nothing forks or reaps while the
        # tree is listed, then list again to catch children started meanwhile.
        frozen, pending = [], [process.pid]
        while pending and len(frozen) < 1000:
            for pid in pending:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except OSError:
                    pass
            frozen.extend(pending)
            pending = [pid for pid in _command_descendants(process.pid) if pid not in frozen]
        for pid in frozen:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    process.kill()


# A command's output is read as it arrives and only its head and its tail
# (COMMAND_OUTPUT_KEEP characters each) are kept for its log; a command whose
# output, or a file it is watched for (a TeX build's own log), grows past
# COMMAND_OUTPUT_LIMIT is stopped like one that timed out.
COMMAND_OUTPUT_KEEP = 1_000_000
COMMAND_OUTPUT_LIMIT = 256 * 1024 * 1024
# Of a longer output, the next step (a LaTeX repair) reads every TeX error and the end.
COMMAND_OUTPUT_CHARS = 24000
# A command that cannot run yet (missing tools, a start failure, an isolated
# compiler that is not ready) is run again every MODEL_RETRY_SECONDS.
COMMAND_RETRY_LABEL = "Retrying command"
# A timed-out run is repeated with each longer limit before its node sees the timeout.
COMMAND_TIMEOUT_RETRIES = (900, 1800)
# The same failure (or the same input) this often ends a command loop that makes no progress.
COMMAND_STALL_REPEATS = 3
COMMAND_HISTORY = 20


class _CommandOutput:
    """A command's output as it arrives: the head and the tail are kept, the middle only counted."""

    def __init__(self, keep=COMMAND_OUTPUT_KEEP):
        self.keep, self.size, self.omitted = keep, 0, 0
        self.head, self.tail, self.tail_size = [], deque(), 0
        self.lock = threading.Lock()

    def read(self, descriptor, encoding, errors):
        try:
            with open(descriptor, encoding=encoding, errors=errors) as stream:
                for line in iter(lambda: stream.readline(65536), ""):
                    self.add(line)
        except (OSError, ValueError):
            pass  # The pipe closed under the reader; what arrived is kept.

    def add(self, text):
        with self.lock:
            room = self.keep - self.size
            self.size += len(text)
            if room > 0:
                self.head.append(text[:room])
                text = text[room:]
            if text:
                self.tail.append(text)
                self.tail_size += len(text)
                while self.tail_size - len(self.tail[0]) >= self.keep:
                    self.tail_size -= len(self.tail[0])
                    self.omitted += len(self.tail.popleft())

    def text(self):
        with self.lock:
            tail = "".join(self.tail)
            cut = max(0, len(tail) - self.keep)
            omitted = self.omitted + cut
            return "".join(self.head) + (f"\n[{omitted} characters omitted]\n" if omitted else "") + tail[cut:]


def _file_size(path):
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def _run_bounded(argv, *, timeout, watch=(), **kwargs):
    """Run one command; on timeout or interruption, stop everything it started.

    Its output is decoded and read as it arrives (_CommandOutput). A command
    whose output, or one of the `watch` files, grows past COMMAND_OUTPUT_LIMIT
    is stopped as well: TimeoutExpired with `runaway` saying why.
    """
    encoding, errors = kwargs.pop("encoding", None) or "utf-8", kwargs.pop("errors", None) or "replace"
    kwargs.pop("text", None)
    with subprocess.Popen(argv, **kwargs) as process:
        output, reader = _CommandOutput(), None
        deadline = time.monotonic() + timeout
        try:
            if process.stdout is not None:
                # The reader owns a duplicate, so closing the pipe never waits for a read.
                reader = threading.Thread(target=output.read, args=(os.dup(process.stdout.fileno()), encoding, errors),
                                          daemon=True)
                reader.start()
            while reader is not None and reader.is_alive():
                reader.join(min(1.0, max(0.0, deadline - time.monotonic())))
                limit = f"more than {COMMAND_OUTPUT_LIMIT // 2 ** 20} MB"
                runaway = (f"printed {limit} of output" if output.size > COMMAND_OUTPUT_LIMIT
                           else next((f"wrote {limit} to {Path(path).name}" for path in watch
                                      if _file_size(path) > COMMAND_OUTPUT_LIMIT), ""))
                if runaway or (reader.is_alive() and time.monotonic() >= deadline):
                    expired = subprocess.TimeoutExpired(argv, timeout)
                    expired.runaway = runaway
                    raise expired
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(argv, timeout) from None
        except BaseException as exc:
            _stop_command_tree(process)
            if reader is not None:
                reader.join(5)  # A detached process may still hold the pipe.
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.stdout = output.text()
            raise
        return subprocess.CompletedProcess(argv, process.returncode, output.text())


# Files a TeX build reads back from its previous run (latexmk's records too).
# A failed attempt can leave entries that a corrected source no longer defines,
# such as lineno or biblatex commands in final.aux, and TeX then stops at them
# in every later compile ("./final.aux:2: Undefined control sequence").
TEX_AUXILIARY_SUFFIXES = (".aux", ".bbl", ".bcf", ".blg", ".fdb_latexmk", ".fls", ".idx", ".ilg", ".ind",
                          ".lof", ".log", ".lot", ".nav", ".out", ".run.xml", ".snm", ".toc", ".vrb", ".xdv")


def _tex_auxiliary_files(argv, directory):
    """The auxiliary files of a TeX command's -jobname, only ever inside its own folder."""
    names = [value.split("=", 1)[1] for value in argv[1:] if value.startswith(("-jobname=", "--jobname="))]
    jobname = names[-1] if names else ""
    if not jobname or jobname in {".", ".."} or Path(jobname).name != jobname:
        return []
    return [directory / f"{jobname}{suffix}" for suffix in TEX_AUXILIARY_SUFFIXES]


# Where TeX installs its programs, for a server whose PATH lacks them (one
# started from Finder or launchd); the newest TeX Live year comes first.
TEX_TOOL_FOLDERS = ("/Library/TeX/texbin", "/usr/local/texlive/*/bin/*", "~/Library/TinyTeX/bin/*",
                    "~/.TinyTeX/bin/*", "/opt/homebrew/bin", "/usr/local/bin", "/usr/texbin")
# A TeX error line: "./latex-source.tex:12: Undefined control sequence." (with
# -file-line-error) or "! LaTeX Error: File `x.sty' not found.".
_TEX_ERROR = re.compile(r"!\s|(?:\.{0,2}/)?[^\s:]+\.(?:tex|aux|sty|cls|bbl|toc|lof|lot|out|def|cfg|clo|fd|ltx|ind"
                        r"|nav|snm|vrb):\d+: ")
# A citation or reference that the last TeX pass left undefined.
_TEX_UNDEFINED = re.compile(r"Warning: (Citation|Reference) [`']([^'\n]+)' on page \S+ undefined")


def _find_tool(name):
    """A command's executable on PATH, else in one of TEX_TOOL_FOLDERS, else None."""

    found = shutil.which(name)
    if found or os.sep in name:
        return found
    folders = []
    for pattern in TEX_TOOL_FOLDERS:
        pattern = os.path.expanduser(pattern)
        base, wildcard, rest = pattern.partition("/*")
        try:
            folders += sorted((str(path) for path in Path(base).glob("*" + rest)), reverse=True) if wildcard else [base]
        except OSError:
            continue
    return shutil.which(name, path=os.pathsep.join(folders))


def _tex_errors(output, limit=100):
    """Every TeX error of one compile in order, each with the context lines TeX prints after it."""

    lines, blocks, index = output.splitlines(), [], 0
    while index < len(lines) and len(blocks) < limit:
        if not _TEX_ERROR.match(lines[index]):
            index += 1
            continue
        block = [lines[index][:400]]
        index += 1
        while (index < len(lines) and len(block) < 7 and lines[index] and not _TEX_ERROR.match(lines[index])
               and not lines[index].startswith("Latexmk:")):
            block.append(lines[index][:400])
            index += 1
        blocks.append("\n".join(block))
    return blocks


def _tex_unresolved(log, keep=16 * 1024 * 1024):
    """(names, found): the citations and references the last TeX pass left undefined, from the end of its log."""

    try:
        if log.is_symlink() or not log.is_file():
            return [], False
        descriptor = os.open(log, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            handle.seek(max(0, handle.seek(0, os.SEEK_END) - keep))
            text = handle.read(keep).decode("utf-8", errors="replace")
    except OSError:
        return [], False
    text = re.sub(r"(?m)^(.{79})\n", r"\1", text)  # TeX breaks log lines at 79 characters.
    names = list(dict.fromkeys(f"{kind.lower()} {name}" for kind, name in _TEX_UNDEFINED.findall(text)))
    return names, bool(names or re.search(r"There were undefined (?:references|citations)", text))


def _command_excerpt(output, errors=(), limit=COMMAND_OUTPUT_CHARS):
    """What the next step reads of an output: all of it when short, else every error and the end."""

    if len(output) <= limit:
        return output
    tail = output[-(limit // 2 if errors else limit - 60):]
    shown, size = [], 0
    for block in errors:
        if block in tail:
            continue
        if size + len(block) > limit // 2 - 60:
            shown.append("[More errors omitted]")
            break
        shown.append(block)
        size += len(block) + 2
    before = output[:len(output) - len(tail)]
    # The kept head and tail of a long output already mark the middle they omit.
    omitted = len(before) + sum(int(count) - len(marker) for marker, count
                                in re.findall(r"(\n\[(\d+) characters omitted\]\n)", before))
    return "".join(f"{block}\n\n" for block in shown) + f"[{omitted} earlier characters omitted]\n{tail}"


def _command_inputs(argv, directory):
    """A digest of the files its command line names in the command's folder (a TeX build's source)."""

    digest = hashlib.sha256()
    for value in argv[1:]:
        path = directory / value
        try:
            if not value.startswith("-") and Path(value).name == value and path.is_file() and not path.is_symlink():
                digest.update(value.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
        except (OSError, ValueError):
            continue
    return digest.hexdigest()


def _failure_signature(result, errors):
    """A failed run's report without line numbers: the same one again means a repair made no progress."""

    if result.get("timed_out"):
        return "did not finish"
    if result.get("unresolved"):
        return "undefined " + ", ".join(result["unresolved"])
    if errors:
        lines = errors[0].splitlines()
        context = next((line for line in lines[1:] if re.match(r"l\.\d+ ", line)), "")
        return re.sub(r"(\.\w+:)\d+:", r"\1:", lines[0]) + " " + re.sub(r"^l\.\d+", "l.", context)
    return str(result.get("diagnostic", ""))


def _command_result(config, argv, directory, result, attempt=None):
    """Complete a command result: its TeX errors and unresolved references, signature, excerpt and logs."""

    result = {"diagnostic": "", "returncode": None, **result}
    output = result.get("output") or ""
    auxiliary = _tex_auxiliary_files(argv, directory)
    errors = _tex_errors(output) if auxiliary else []
    if result["status"] == "fail" and errors and not result.get("timed_out"):
        # "Command exited with code 12: ./latex-source.tex:4: Undefined control sequence."
        result["diagnostic"] = (f"{result['diagnostic'].rstrip('.')}: {errors[0].splitlines()[0]}"
                                + (f" ({len(errors)} errors in all.)" if len(errors) > 1 else ""))
    log = next((path for path in auxiliary if path.suffix == ".log"), None)
    names, unresolved = _tex_unresolved(log) if result["status"] == "pass" and log is not None else ([], False)
    if unresolved:
        # The PDF stays: should repairs not resolve them, it is still the best result.
        counts = [f"{count} {kind}{'s' if count != 1 else ''}" for kind, count in (
            ("citation", sum(name.startswith("citation ") for name in names)),
            ("reference", sum(name.startswith("reference ") for name in names))) if count]
        result.update(status="fail", unresolved=names or ["references"], diagnostic=(
            (f"The document compiled, but LaTeX left {' and '.join(counts)} undefined: {', '.join(names[:20])}."
             if names else "The document compiled, but LaTeX reports undefined references.")
            + " Give the references in an inline thebibliography environment (BibTeX and .bib files are not"
              " available in every run), and give every \\ref and \\cite a \\label or \\bibitem in the document."))
    if result["status"] == "fail":
        result["signature"] = _failure_signature(result, errors)
        result["input_sha256"] = _command_inputs(argv, directory)
    if config.get("log"):
        logs = [config["log"]]
        if attempt is not None and result["status"] != "unavailable":
            log_path = Path(config["log"])
            logs.append(str(log_path.with_name(f"{log_path.stem}-{attempt}{log_path.suffix}")))
        for name in logs:
            try:
                _private_atomic_write(directory / name, result["diagnostic"] + "\n\n" + output)
            except OSError as exc:
                # The run continues without this log.
                result.setdefault("log_error", f"The command log {name} could not be written: {exc}")
    result["output"] = _command_excerpt(output, errors)
    return result


def _run_command(config, context, attempt=None):
    """Run a bounded argv command and check only its declared output files.

    The status is pass, fail (with timed_out when it did not finish, and
    runaway when it was stopped for printing without end), unavailable
    (nothing ran: its tools are missing or, with transient, could not start;
    under a web cutoff, the isolated compiler is not ready) or uncompiled (the
    isolated compiler refuses the source). A TeX build (one with -jobname) also
    fails when its log names unresolved citations or references (unresolved);
    its outputs are then kept. output holds every TeX error and the end of a
    long output; the log holds its head and tail and, given `attempt`, has a
    copy for that attempt (latex-compile-3.log).
    """
    policy = retrieval_policy()
    argv = [render_template(value, context) for value in config["argv"]]
    directory = (Path.cwd() / render_template(config.get("cwd", "."), context)).resolve()
    outputs = [directory / name for name in config.get("produces", [])]
    result = {"returncode": None, "output": ""}
    if policy is not None:
        from retrieval import isolated_compiler
        try:
            result = isolated_compiler.compile_command(policy, config, argv, directory)
        except isolated_compiler.CompilerNotReady as exc:
            # Checked again before the next attempt; nothing ran (I-069).
            result.update(status="unavailable", diagnostic=f"The isolated compiler is not ready: {exc}")
        except isolated_compiler.CompilerRefusal as exc:
            # Fail closed: no output of this or an earlier attempt is published (I-061).
            for path in outputs:
                path.unlink(missing_ok=True)
            result.update(status="uncompiled", diagnostic=f"The isolated compiler refused to compile: {exc}")
        except isolated_compiler.CompilerIsolationError as exc:
            # An unsupported command, platform or timeout: no retry can change it.
            raise ConfigurationError(str(exc)) from exc
        return _command_result(config, argv, directory, result, attempt)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in outputs:
        path.unlink(missing_ok=True)
    tools = {name: _find_tool(name) for name in (argv[0], *config.get("requires", []))}
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        result.update(status="unavailable", diagnostic=f"Command unavailable: {', '.join(missing)} "
                                                       "(not on PATH or in the standard TeX folders).")
    else:
        environment = {**os.environ, **{key: render_template(value, context) for key, value in config.get("env", {}).items()}}
        # Tools found outside PATH bring their folders: latexmk starts pdflatex by name.
        known = environment.get("PATH", "").split(os.pathsep)
        extra = [folder for folder in dict.fromkeys(str(Path(path).parent) for path in tools.values())
                 if folder not in known]
        if extra:
            environment["PATH"] = os.pathsep.join(filter(None, [environment.get("PATH", ""), *extra]))
        # Every attempt compiles from a clean slate, as the isolated compiler
        # does in its fresh folder; sources may still use files of this folder.
        auxiliary = _tex_auxiliary_files(argv, directory)
        for path in auxiliary:
            path.unlink(missing_ok=True)
        try:
            completed = _run_bounded([tools[argv[0]], *argv[1:]], cwd=directory, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", timeout=config["timeout"],
                watch=[path for path in auxiliary if path.suffix == ".log"],
                **({"umask": 0o077} if os.name != "nt" else {}))
            code, output = completed.returncode, completed.stdout or ""
            produced = all(path.is_file() and path.stat().st_size > 0 for path in outputs)
            diagnostic = f"Command exited with code {code}."
            if code == 0 and not produced:
                diagnostic += " A declared output file is missing or empty."
            result.update(status="pass" if code == 0 and produced else "fail", diagnostic=diagnostic,
                          returncode=code, output=output)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            runaway = getattr(exc, "runaway", "")
            result.update(status="fail", timed_out=True, output=output, diagnostic=(
                f"Command {runaway} and was stopped." if runaway else f"Command exceeded {config['timeout']:g} seconds."))
            if runaway:
                result["runaway"] = True
        except OSError as exc:
            # Only a missing, inaccessible or unrunnable tool means it is not installed (I-022).
            result.update(status="unavailable", diagnostic=f"Command could not run: {exc}")
            if exc.errno not in (errno.ENOENT, errno.EACCES, errno.ENOEXEC):
                result["transient"] = True
    if result["status"] != "pass":
        for path in outputs:
            path.unlink(missing_ok=True)
    return _command_result(config, argv, directory, result, attempt)


def _wait_before_command_retry(options, stage, node, attempt, result, since):
    """Wait MODEL_RETRY_SECONDS before running a command that could not run; no model is called.

    The job stays running and a status event says why. The wait does not
    consume the time budget (RetryClock); Stop ends it at once and the pause
    file pauses the workflow.
    """

    nominal = max(0.0, float(MODEL_RETRY_SECONDS))
    seconds = nominal + _retry_jitter(nominal)
    clock = retry_clock(options)
    details = [f"attempt {attempt}"]
    spent = max(0.0, _wall_clock() - since)
    if spent >= 60:
        details.append(f"waiting for {_span_text(spent)}")
    change = clock.wait_started()
    emit("status", stage, label=COMMAND_RETRY_LABEL, node=node, retryAttempt=attempt, retryInSeconds=nominal,
         retryAt=_wall_clock() + seconds, failure=str(result.get("diagnostic", "")), failureKind="command",
         text=f"{result.get('diagnostic', '')} Trying again in {_duration_text(nominal)} ({', '.join(details)}).",
         retryingSeconds=round(spent), **({"budgetClock": change} if change else {}))
    pause_file = (options or {}).get("goal_pause_file")
    finished = False
    try:
        waited = 0.0
        while waited < seconds:
            if pause_file and Path(pause_file).is_file():
                raise WorkflowPaused("Paused while waiting to run the command again.")
            step = min(RETRY_POLL_SECONDS, seconds - waited)
            _retry_sleep(step)
            waited += step
        finished = True
    finally:
        change = clock.wait_ended()
        if finished:
            emit("status", stage, label=COMMAND_RETRY_LABEL, node=node, retryAttempt=attempt + 1,
                 text=f"Running the command again now (attempt {attempt + 1}).",
                 **({"budgetClock": change} if change else {}))
        elif change:
            _emit_budget_clock(stage, change)


def _command_step(config, state, options, visit, name, stage, transitions):
    """Run a command node until it has an outcome for the graph; no model is called meanwhile.

    A command that cannot run (status unavailable) runs again every
    MODEL_RETRY_SECONDS until it can, without a limit (see
    _wait_before_command_retry). A timed-out run is repeated with each longer
    limit of COMMAND_TIMEOUT_RETRIES first. The result counts the node's runs
    (attempt) and keeps its recent failures (history): once the same failure
    or the same input has failed COMMAND_STALL_REPEATS times, a TeX build
    that only left references unresolved passes with its PDF, and a node with
    an uncompiled transition takes it instead of repeating the loop.
    """

    previous = state.get(config["result"]) if isinstance(state.get(config["result"]), dict) else {}
    attempt = previous.get("attempt") if type(previous.get("attempt")) is int else 0
    # Only the failures of an unfinished loop count; one that ended leaves none to a later loop.
    history = ([] if previous.get("status") in {"pass", "uncompiled"}
               else [entry for entry in previous.get("history") or [] if isinstance(entry, dict)])
    longer = [limit for limit in COMMAND_TIMEOUT_RETRIES if limit > config["timeout"]]
    checks, since, announced, limits = 0, None, None, []
    with retry_clock(options).call(stage):
        while True:
            if attempt and announced != attempt:
                announced = attempt
                emit("status", stage, label=f"Workflow step: {name}", node=name, visit=visit, attempt=attempt + 1,
                     text=f"Attempt {attempt + 1} of this step.")
            try:
                result = _run_command(config, {"state": state, "visit": visit}, attempt=attempt + 1)
            except OSError as exc:
                # A full disk or a vanished folder passes like a missing tool.
                result = {"status": "unavailable", "transient": True, "returncode": None, "output": "",
                          "diagnostic": f"Command could not run: {exc}"}
            if result.get("log_error"):
                emit("diagnostic", stage, node=name, text=result["log_error"])
            if result.get("status") == "unavailable":
                checks += 1
                since = since or _wall_clock()
                _wait_before_command_retry(options, stage, name, checks, result, since)
                continue
            attempt += 1
            checks, since = 0, None
            if result.get("timed_out") and not result.get("runaway") and longer and workflow_remaining(options) > 0:
                limits.append(config["timeout"])
                config = {**config, "timeout": min(longer.pop(0), workflow_remaining(options))}
                emit("status", stage, label=f"Workflow step: {name}", node=name, visit=visit, attempt=attempt + 1,
                     text=f"Attempt {attempt} did not finish within {_span_text(limits[-1])}; attempt {attempt + 1} "
                          f"may run {_span_text(config['timeout'])}.")
                announced = attempt
                continue
            break
    result = {**result, "attempt": attempt}
    if result.get("timed_out") and limits:
        result["diagnostic"] = (f"{result.get('diagnostic', '')} Earlier attempts stopped after "
                                f"{' and '.join(map(_span_text, limits))}.")
    if result.get("status") == "fail":
        signature, source = result.get("signature") or result.get("diagnostic", ""), result.get("input_sha256")
        repeats = 1 + max(sum(entry.get("signature") == signature for entry in history),
                          sum(bool(source) and entry.get("input_sha256") == source for entry in history))
        history = [*history, {"signature": signature, "input_sha256": source}][-COMMAND_HISTORY:]
        if repeats >= COMMAND_STALL_REPEATS and result.get("unresolved"):
            emit("diagnostic", stage, node=name, label="References left unresolved",
                 text=f"{repeats} attempts left the same references undefined, so the PDF is used as it is. "
                      f"{result.get('diagnostic', '')}")
            result["status"] = "pass"
        elif repeats >= COMMAND_STALL_REPEATS and "uncompiled" in transitions:
            log = Path(config["log"]) if config.get("log") else None
            result.update(status="uncompiled", diagnostic=(
                f"{repeats} attempts failed the same way, so the step stopped repeating. Last attempt: "
                f"{result.get('diagnostic', '')}"
                + (f" The output of each attempt is in {log.stem}-N{log.suffix}." if log else "")))
    result["history"] = history
    return result


STORAGE_RETRY_LABEL = "Retrying file write"


def _storage_retry(operation, options, stage, what):
    """Run one local file operation; an OS error (a full disk, an I/O error) is retried without a limit.

    The step keeps its in-memory state and waits MODEL_RETRY_SECONDS between
    attempts, outside the time budget. Stop ends a wait at once and the pause
    file pauses the workflow at this step.
    """

    attempt, since = 1, None
    clock = retry_clock(options)
    pause_file = (options or {}).get("goal_pause_file")
    with clock.call(stage):
        while True:
            try:
                return operation()
            except OSError as exc:
                attempt += 1
                since = since or _wall_clock()
                spent = max(0.0, _wall_clock() - since)
                cause = ("disk full" if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
                         else exc.strerror or str(exc) or type(exc).__name__)
                nominal = max(0.0, float(MODEL_RETRY_SECONDS))
                seconds = nominal + _retry_jitter(nominal)
                change = clock.wait_started()
                emit("status", stage, label=STORAGE_RETRY_LABEL, failure=cause, failureKind="storage",
                     retryAttempt=attempt, retryInSeconds=nominal, retryAt=_wall_clock() + seconds,
                     text=f"Could not save {what} ({cause}) — retrying in {_duration_text(nominal)} (attempt {attempt}"
                          + (f", retrying for {_span_text(spent)}" if spent >= 60 else "") + ").",
                     **({"budgetClock": change} if change else {}))
                finished = False
                try:
                    waited = 0.0
                    while waited < seconds:
                        if pause_file and Path(pause_file).is_file():
                            raise WorkflowPaused("Paused while waiting to retry a file write.")
                        step = min(RETRY_POLL_SECONDS, seconds - waited)
                        _retry_sleep(step)
                        waited += step
                    finished = True
                finally:
                    change = clock.wait_ended()
                    if finished:
                        emit("status", stage, label=STORAGE_RETRY_LABEL, retryAttempt=attempt,
                             text=f"Saving {what} again now (attempt {attempt}).",
                             **({"budgetClock": change} if change else {}))
                    elif change:
                        _emit_budget_clock(stage, change)


def _apply_actions(actions, context, stage, revision, options=None):
    state = context["state"]
    for action in actions:
        if "when" in action and not evaluate(action["when"], context):
            continue
        if "merge" in action:
            values = evaluate(action["merge"], context)
            if not isinstance(values, dict):
                raise ValueError("Merged state must be a dictionary.")
            state.update(values)
        if "set" in action:
            state.update({key: evaluate(expression, context) for key, expression in action["set"].items()})
        if "write" in action:
            path, content = Path.cwd() / action["write"]["path"], evaluate(action["write"]["text"], context)
            _storage_retry(lambda: _private_atomic_write(path, content), options, stage, action["write"]["path"])
        if "command" in action:
            config = action["command"]
            state[config["result"]] = _run_command(config, context)
            if state[config["result"]].get("log_error"):
                emit("diagnostic", stage, text=state[config["result"]]["log_error"])
        if "emit" in action:
            fields = {}
            for key, value in action["emit"].items():
                fields[key] = (evaluate(value[1:], context) if value.startswith("=") else render_template(value, context)) if isinstance(value, str) else value
            emit(fields.pop("kind"), fields.pop("stage", stage), **fields)


def _complete_node(node, state, result, raw, visit, revision, options=None):
    context = {"state": state, "result": result, "raw": raw, "visit": visit,
               "stages": normalize_workflow_stages((options or {}).get("workflow_stages"))}
    try:
        for condition in node.get("require", []):
            if not evaluate(condition, context):
                raise ValueError(f"Unmet response requirement: {describe_requirement(condition, node.get('schema'))} "
                                 f"(rule: {condition})")
        outcome = evaluate(node.get("outcome", "'done'"), context)
    except ValueError as exc:
        raise Error(node.get("error", str(exc))) from exc
    if not isinstance(outcome, str) or outcome not in node["next"]:
        raise Error(f"Node returned an unknown outcome: {outcome!r}.")
    context["outcome"] = outcome
    _apply_actions(node.get("after", []), context, node.get("stage", "model"), revision, options)
    state["outcome"] = outcome
    return context


def _goal_step(sessions, name, launch, initial_instruction, options, step, stage):
    """Advance one goal session; retry every model-call failure without a limit.

    A failed attempt of a real session ends in WorkflowPaused carrying retry
    details. A user's Pause, the time limit, a conversation that no longer
    exists, a safety stop and other failures no retry can fix carry none and
    pause as before. Otherwise the controller waits MODEL_RETRY_SECONDS
    (outside the time budget; Stop and the pause file end the wait) and
    continues in a new session that resumes the same thread:
    - a turn the model never received is sent again;
    - a turn the model received but answered with nothing at all is removed
      from the thread (``rewind``) and sent again, so outages add nothing;
    - after a turn in which the model worked, AUTHOR_RETRY_NOTE asks it to
      continue.
    Whatever a later attempt fails to learn (for example its thread, when it
    fails before resuming) keeps its last known value, so no attempt ever
    starts a new conversation or drops an undelivered instruction. Without
    any thread yet, the same first attempt runs again. The one exception is a
    conversation that cannot continue (``replace``, for example one that no
    longer fits the model's context): the retries continue in a new
    conversation, announced as "Conversation replaced", whose first
    instruction the failed session prepared.
    """

    attempt = 1
    # The latest known way to continue; a session that fails early reports less.
    known = {"thread": options.get("goal_thread_id"), "stage": stage, "values": None, "steer": None,
             "pending_compaction": None, "anchor": None, "instruction": None, "rewind": None,
             "replace": None, "refusal": None, "classic_shell": False}
    failing_since = None
    with retry_clock(options).call(stage):
        while True:
            try:
                return step(sessions[name])
            except WorkflowPaused as exc:
                retry = exc.retry
                if not isinstance(retry, dict):
                    raise
                attempt += 1
                if failing_since is None or retry.get("output"):
                    failing_since = _wall_clock()
                for key in ("thread", "stage", "values", "steer", "pending_compaction", "anchor"):
                    if retry.get(key):
                        known[key] = retry[key]
                # Consecutive identical refusals to reopen the thread; any other outcome resets the count.
                known["refusal"] = retry.get("refusal")
                known["classic_shell"] = known["classic_shell"] or bool(retry.get("classic_shell"))
                if retry.get("instruction"):
                    if retry.get("delivered"):
                        # The rewind (if any) removes this very turn; else the
                        # model worked in it and continues after a note.
                        rewind = retry.get("rewind") if isinstance(retry.get("rewind"), dict) else None
                        known["instruction"] = retry["instruction"] if rewind else AUTHOR_RETRY_NOTE
                        known["rewind"] = rewind
                    else:
                        known["instruction"] = retry["instruction"]
                        if retry.get("rewound"):
                            known["rewind"] = None
                if retry.get("rewind_failed") and known["rewind"]:
                    # The CLI could not reopen the conversation before the failed
                    # turn: continue after it instead, so the retries progress.
                    known["rewind"] = None
                    emit("diagnostic", known["stage"], node=name,
                         text="The CLI could not reopen the conversation before the failed turn; the next retry "
                              "continues after it.")
                if retry.get("replace"):
                    known.update(thread=None, rewind=None, anchor=None, pending_compaction=None,
                                 replace=retry["replace"], instruction=retry.get("instruction") or known["instruction"])
                    emit("status", known["stage"], node=name, label="Conversation replaced",
                         text=_replacement_notice(retry["replace"]))
                elif retry.get("thread"):
                    known["replace"] = None
                thread = known["thread"]
                wait_before_retry(options, known["stage"], attempt, Error(exc.reason or str(exc)),
                                  node=name, thread_id=thread, since=failing_since)
                session_options = dict(options)
                if known["steer"]:
                    session_options["author_steer_delivered"] = known["steer"]
                rewind = known["rewind"] or {}
                continued = {"stage": known["stage"], "values": known["values"] or {},
                             "instruction": known["instruction"], "rewind": known["rewind"],
                             "pending_compaction": known["pending_compaction"], "anchor": known["anchor"],
                             "attempt": attempt, "refusal": known["refusal"], "classic_shell": known["classic_shell"]}
                if rewind.get("fresh") or known["replace"]:
                    # The conversation's first turn produced nothing (a new
                    # conversation with the same first instruction loses
                    # nothing), or the conversation cannot continue.
                    for key in ("goal_thread_id", "goal_resume", "goal_require_resume"):
                        session_options.pop(key, None)
                    known["thread"] = None
                    session_options["goal_retry"] = {**continued, "rewind": None}
                    sessions[name] = launch(options=session_options, initial_instruction="")
                elif thread:
                    session_options.update(goal_thread_id=thread, goal_resume=True, goal_require_resume=True,
                                           goal_retry=continued)
                    # Without a recorded instruction the session rebuilds the same first one.
                    sessions[name] = launch(options=session_options,
                                            initial_instruction="" if known["instruction"] else initial_instruction)
                else:
                    sessions[name] = launch(options=session_options, initial_instruction=initial_instruction)
                step = next


def _branch_targets(node):
    """The nodes that one node's outcomes lead to."""

    return {branch if isinstance(branch, str) else branch.get("to", branch.get("then"))
            for branch in node["next"].values()}


def _step_visit(state, node, visit, verify):
    """Mark `state` as the input of this visit of step `node`; return it.

    A later visit and a verify-only visit record which visit they are (see
    STEP_VISIT_KEY), so a run resumed at `node` from this input continues
    that visit; the input of a first visit records nothing.
    """
    if visit > 1 or verify:
        state[STEP_VISIT_KEY] = {"node": node, "visit": visit, **({"verify": verify} if verify else {})}
    return state


def _resumed_visit(node, name, saved):
    """How many visits of step `name` precede its resumed visit, and that visit's verify instruction.

    Both come from the record its saved input carries (see _step_visit).
    Anything else, such as no record, a record of another step or a verify
    prompt that none of the step's repeat branches names, resumes the step
    as its first visit.
    """
    if not isinstance(saved, dict) or saved.get("node") != name:
        return 0, None
    visit, verify = saved.get("visit"), saved.get("verify")
    verifies = {branch["verify"] for branch in node["next"].values() if isinstance(branch, dict) and "verify" in branch}
    if type(visit) is not int or visit < 1 or (verify is not None and verify not in verifies):
        return 0, None
    return visit - 1, verify


def _execute(workflow, state, options, prompts):
    nodes = workflow["nodes"]
    # The visit that the saved input of a resumed step records; no workflow expression reads it.
    saved_visit = state.pop(STEP_VISIT_KEY, None)
    current, visits, revision = options.get("start_node", next(iter(nodes))), 0, 0
    if current not in nodes:
        raise ValueError(f"Unknown workflow entry node: {current!r}.")
    current = _selected_entry(nodes, current, options)
    if current == "end":
        _capture_unreviewed_proof(workflow, state, options)
        return state
    sessions, revisions, launchers = {}, {}, {}
    options.setdefault("_workflow_started", _now())
    retry_clock(options)
    # The time limit stops only the author: later steps always finish (see workflow_remaining). They
    # share these options, so a note said once per workflow (see _structured_options) stays said once.
    later_options = _after_submission(options)
    # The previous step; the goal node whose result it handed on, with the state before that result;
    # the exact input of the current step; and the instruction of a verify-only visit.
    previous, submitted, entry, verify = None, None, None, None
    if "start_node" in options:
        # A resumed step continues as the visit it was: same round, verify instruction and critic checkpoint.
        visits, verify = _resumed_visit(nodes[current], current, saved_visit)
    try:
        while current != "end":
            node = nodes[current]
            goal = node["run"] == "goal"
            node_options = options if goal else later_options
            entry = None
            visits += 1
            if options.get("goal_pause_file") and Path(options["goal_pause_file"]).is_file():
                raise WorkflowPaused("Paused before the next workflow step.")
            if goal and workflow_remaining(options) <= 0:
                raise Error("Workflow time limit reached.")
            # The step record names its visit, and the instruction of a verify-only visit: the transcript
            # keeps it (unlike node_input), so a restart continues an interrupted visit as itself.
            emit("status", node.get("stage", current), label=f"Workflow step: {current}",
                 node=current, visit=visits, **({"verify": verify} if verify else {}))
            if (node.get("workflow_stage") == "writing" and node["run"] == "structured"
                    and "solution" in node.get("inputs", {})):
                candidate = evaluate(node["inputs"]["solution"], {"state": state, "visit": visits})
                statement = state.get("statement")
                if isinstance(statement, str) and statement.strip() and isinstance(candidate, str) and candidate.strip():
                    save_final_input(statement, candidate)
                    emit("final_input", node.get("stage", current), node=current,
                         label="Exact writing input saved")
            # Only the first visit after the author (or at the start) receives a submission.
            submission = visits == 1 and (previous is None or nodes[previous]["run"] == "goal")
            critic_model = _settings(node, options)["model"] if "proof_handoff" in node else None
            input_files = _prepare_node_files(node, state, visits, options.get("goal_cwd"),
                                              resolve_reference=submission, model=critic_model)
            if not goal:
                # A runner that ends during this step is resumed from this exact input, as this visit.
                entry = _step_visit(_copy.deepcopy(state), current, visits, verify)
                emit("node_input", node.get("stage", current), node=current, visit=visits, state=entry)
            _apply_actions(node.get("before", []), {"state": state, "visit": visits}, node.get("stage", current), revision,
                           node_options)
            if node["run"] == "structured":
                result, raw = _model_call(node, prompts, state, node_options, visits, input_files=input_files,
                                          verify=verify)
            elif node["run"] == "command":
                config = dict(node["command"])
                # The time limit stops only the author (I-044): node_options has no deadline, so a
                # compilation is never cut short and a timed-out run is still repeated with longer limits.
                result = _command_step(config, state, node_options, visits, current, node.get("stage", current),
                                       node["next"])
                state[config["result"]] = result
                raw = result["output"]
            else:
                submitted = (current, _copy.deepcopy(state))
                context = {"state": state, "visit": visits}
                revision = revisions.get(current, 0)
                if current not in sessions:
                    task = text(evaluate(node["task"], context))
                    prompt = options.get("author_input")
                    if prompt is None:
                        prompt = prompts[node["prompt"]].replace(node["marker"], task, 1)
                    lifecycle = node["lifecycle"]
                    if isinstance(lifecycle, list):
                        lifecycle = {key: key for key in lifecycle}
                    if options.get("related_works") and "related_works" in lifecycle:
                        # The run has related works: the first prompt points to them, and
                        # {original_prompt} carries the pointer into the later prompts.
                        prompt += "\n\n" + prompts[lifecycle["related_works"]]
                    features, count = node_features(node, options)
                    if count and "subagents" in lifecycle:
                        # The author may start subagents: the first prompt says how many, like the related
                        # works, and {original_prompt} carries it into the later prompts.
                        prompt += "\n\n" + render_template(prompts[lifecycle["subagents"]], {"author_subagents": count})
                    session_prompts = {key: prompts[ref] for key, ref in lifecycle.items() if key != "subagents"}
                    directory = options.get("goal_cwd") or str(Path.cwd())
                    recovery = node.get("recovery")
                    instruction = ""
                    if recovery and evaluate(recovery["when"], context):
                        feedback = {key: evaluate(expression, context) for key, expression in node["resume"].items()}
                        instruction = render_template(prompts[recovery["prompt"]], {
                            "original_prompt": prompt, "directory": str(directory),
                            "instruction": "", "revision_number": 1, **feedback,
                        })
                    settings = _settings(node, options)
                    if count is not None:
                        # The most subagents at once (Codex's own limits, see subagent_limits()).
                        settings["subagents"] = count
                        note_no_subagent_tool(node.get("stage", "solve"), settings["model"], count)
                    # Claude models (and Anthropic-format API models) run the
                    # same goal protocol through the Claude CLI.
                    session_function = (claude_cli.goal_session
                                        if providers.harness(settings["model"]) == providers.CLAUDE
                                        else goal_session)
                    launch = functools.partial(
                        session_function, sys.modules[__name__], prompt, prompts=session_prompts,
                        settings=settings, features=features,
                        stages=node.get("stages"), node_name=current,
                    )
                    launchers[current] = (launch, instruction)
                    refused = state.get("handoff_refused")
                    if "handoff_refused" in lifecycle and isinstance(refused, str) and refused.strip():
                        # The controller refused the author's last submission: every attempt until the author
                        # submits again says why, after any critic feedback (launchers keeps the launch
                        # without the note for the revisions after that submission).
                        launch = functools.partial(launch, handoff_note=render_template(
                            prompts[lifecycle["handoff_refused"]], {"reason": refused}))
                    sessions[current] = launch(options=options, initial_instruction=instruction)
                    result = _goal_step(sessions, current, launch, instruction, options, next,
                                        node.get("stage", current))
                else:
                    revision += 1
                    revisions[current] = revision
                    feedback = {key: evaluate(expression, context) for key, expression in node["resume"].items()}
                    launch, instruction = launchers[current]
                    result = _goal_step(sessions, current, launch, instruction, options,
                                        lambda session: session.send(feedback), node.get("stage", current))
                raw = ""
                # The author submitted again: the note on its refused submission is never sent again.
                state.pop("handoff_refused", None)
            context = _complete_node(node, state, result, raw, visits, revision, node_options)
            branch = node["next"][context["outcome"]]
            target = branch
            if isinstance(branch, dict):
                if "repeat" in branch:
                    limit = options.get(branch.get("option"), branch["repeat"])
                    if visits < limit:
                        target = current
                    elif branch.get("verify") and not verify:
                        # One more visit that can only confirm or reject the latest edits (see _model_call).
                        target, verify = current, branch["verify"]
                    elif "then" in branch:
                        target = branch["then"]
                        context["limit"] = limit
                        _apply_actions(branch.get("after", []), context, node.get("stage", current), revision,
                                       node_options)
                    else:
                        raise WorkflowDefinitionError(f"The verify-only visit of {current} returned "
                                                      f"{context['outcome']!r} again.")
                else:
                    target = branch["to"]
                    _apply_actions(branch.get("after", []), context, node.get("stage", current), revision,
                                   node_options)
            if target != current:
                visits, verify = 0, None
            previous, current = current, _selected_entry(nodes, target, options)
        if not state.get("failed"):
            # Without the critic the author's submission is handed on here, so a
            # refused one pauses at the author like a refused critic handoff (I-004).
            _capture_unreviewed_proof(workflow, state, options)
        return state
    except Exception as exc:
        explain, retry_off = True, False
        if isinstance(exc, HandoffRefused):
            # The author's submission cannot be handed on: the author continues, as it was before submitting.
            if submitted is not None and submitted[0] == previous:
                current = submitted[0]
                state.clear()
                state.update(submitted[1])
            else:
                author = next((name for name, item in nodes.items() if item["run"] == "goal"
                               and current in _branch_targets(item)), None)
                if author is not None and _stage_enabled(nodes[author], options):
                    current = author
                else:
                    # A supplied candidate (no author stage) stays as it is: no retry can change it.
                    retry_off = True
            node, reason = nodes[current], str(exc)
            if not retry_off and "handoff_refused" in node["lifecycle"]:
                # Saved with the checkpoint: the resumed author is told why, until it submits again.
                state["handoff_refused"] = reason
        elif isinstance(exc, WorkflowPaused) or node["run"] == "goal":
            explain = False  # The goal session or the pause request has said why.
            reason = exc.reason if isinstance(exc, WorkflowPaused) else (str(exc) or type(exc).__name__)
        else:
            # A later step pauses and resumes there automatically, except after a
            # configuration or safety error, which waits for the user's Resume.
            reason = str(exc) or type(exc).__name__
            retry_off = isinstance(exc, (FinalError, retrieval_runtime.RetrievalPolicyError))
            if isinstance(exc, (SafetyStop, retrieval_runtime.RetrievalPolicyError)):
                reason = f"Stopped for safety: {reason}"
        if retry_off:
            reason += SAFETY_STOP_HINT
        if explain:
            emit("diagnostic", node.get("stage", current), node=current, text=reason)
        if node["run"] != "goal" and entry is not None:
            # The step resumes from its exact input.
            state.clear()
            state.update(entry)
        elif node["run"] != "goal":
            # It paused before capturing its input (such as before the next step): it resumes as this visit.
            _step_visit(state, current, visits, verify)
        state["paused"] = True
        state.pop("failed", None)
        thread_id = getattr(exc, "thread_id", None) or options.get("goal_thread_id")
        emit("workflow_paused", node.get("stage", current), node=current, state=state,
             reason=reason, threadId=thread_id,
             **({"sessionUnavailable": True} if getattr(exc, "session_unavailable", False) else {}),
             # No automatic retry can fix it (see App._configure_auto_resume).
             **({"safetyStop": True} if getattr(exc, "safety_stop", False) or retry_off else {}))
        return state
    finally:
        for session in sessions.values():
            session.close()


def execute(path, state=None, *, options=None):
    """Execute any graph in the documented format; no node-name dispatch."""

    return execute_workflows([path], {} if state is None else state, options)


def _capture_unreviewed_proof(workflow, state, options):
    """A skipped critic must still pass complete author text to later stages.

    A submission that cannot be read raises HandoffRefused, which pauses the
    run at the author as it was before submitting (see _execute).
    """

    stages = normalize_workflow_stages(options.get("workflow_stages"))
    if stages["critic"] or not any(node.get("workflow_stage") == "author" and _stage_enabled(node, options)
                                    for node in workflow["nodes"].values()):
        return
    for node in workflow["nodes"].values():
        handoff = node.get("proof_handoff")
        if not handoff or _stage_enabled(node, options):
            continue
        candidate = state.get(handoff["candidate"])
        if not isinstance(candidate, str) or not candidate.strip():
            raise HandoffRefused("The proof author returned no complete proof.")
        try:
            snapshot = proof_handoff.resolve_author_proof(candidate, options.get("goal_cwd") or Path.cwd())
        except proof_handoff.ProofHandoffError as exc:
            raise HandoffRefused(f"Cannot read the author's submitted proof: {exc}") from exc
        if snapshot is not None:
            candidate = snapshot.text
        state[handoff["candidate"]] = state["output"] = candidate
        # The critic and the convention critic share the submission: it is captured once.
        return


def _emit_selected_proof_result(prepared, state, options):
    stages = normalize_workflow_stages(options.get("workflow_stages"))
    proof_stages = {node.get("workflow_stage") for workflow, _, _ in prepared
                    for node in workflow["nodes"].values() if _stage_enabled(node, options)} & {"author", "critic"}
    if (stages["writing"] or not proof_stages or state.get("failed") or state.get("paused")):
        return
    output = state.get("output") or state.get("solution", "")
    if not isinstance(output, str) or not output.strip():
        raise Error("The selected proof stages returned no complete output.")
    state["output"] = output
    state["resultKind"] = "proof"
    emit("final_result", "critic" if "critic" in proof_stages else "solve",
         label="Proof result", resultKind="proof", text=output, output=output)


def _missing_command_tools(workflows, options=None):
    """The executables that the workflows' selected command nodes need and cannot find (see _find_tool)."""

    missing = []
    for workflow in workflows:
        for node in workflow["nodes"].values():
            if node["run"] == "command" and _stage_enabled(node, options or {}):
                for name in (node["command"]["argv"][0], *node["command"].get("requires", [])):
                    if "{" not in name and name not in missing and _find_tool(name) is None:
                        missing.append(name)
    return missing


def execute_workflows(paths, state, options=None):
    """Validate once, then feed state through the graphs until one reports failure.

    A start node is looked up in every graph: the chain starts in the first
    graph that contains it (a LaTeX checkpoint resumes in clean_up.yaml), and
    the graphs before it are skipped.
    """

    options = {} if options is None else dict(options)
    normalize_workflow_stages(options.get("workflow_stages"))
    if "input_solution" in options:
        candidate = options["input_solution"]
        if not isinstance(candidate, str) or not candidate.strip() or "\0" in candidate:
            raise ValueError("The supplied candidate must be nonempty text without NUL characters.")
        state["solution"] = candidate
    options.setdefault("_workflow_started", _now())
    # Every graph of the chain shares one budget, retry waits included.
    retry_clock(options)
    workflows = [load_workflow(path) for path in paths]
    first = 0
    if "start_node" in options:
        first = next((index for index, workflow in enumerate(workflows)
                      if options["start_node"] in workflow["nodes"]), None)
        if first is None:
            raise ValueError(f"Unknown workflow entry node: {options['start_node']!r}.")
    prepared = []
    for index, workflow in enumerate(workflows[first:], first):
        local_options = dict(options)
        if index != first:
            local_options.pop("start_node", None)
        prepared.append((workflow, prepare(workflow, local_options), local_options))
    # Missing LaTeX tools are reported at launch, not only once the proof is done
    # (historical runs check their fixed toolchain when they are prepared); a
    # run without its writing stage needs none.
    missing = (_missing_command_tools((workflow for workflow, _, _ in prepared), options)
               if retrieval_policy() is None else [])
    if missing:
        emit("diagnostic", "workflow", label="Compiler tools not found",
             text=f"{', '.join(missing)} not found on PATH or in the standard TeX folders. The run continues; the "
                  "step that needs them checks again every minute until they are installed.")
    # A state saved by workflow_paused still carries its control flags; they
    # describe the earlier run and must not end this one after its first graph.
    state.pop("paused", None)
    state.pop("failed", None)
    state.pop("revisionRequired", None)
    for workflow, prompts, local_options in prepared:
        _execute(workflow, state, local_options, prompts)
        if state.get("failed") or state.get("paused"):
            break
    _emit_selected_proof_result(prepared, state, options)
    return state


def _builtin_node(workflow_name, node_name, state, options, visit=1, prompts=None):
    """Compatibility convenience calls share the same generic node interpreter."""

    workflow = builtin_workflow(workflow_name)
    resolved = {**workflow["prompts"], **(prompts or {}), **options.get("prompts", {})}
    node = workflow["nodes"][node_name]
    input_files = _prepare_node_files(node, state, visit, options.get("goal_cwd"))
    _apply_actions(node.get("before", []), {"state": state, "visit": visit}, node.get("stage", "model"), 0)
    result, raw = _model_call(node, resolved, state, options, visit, input_files=input_files)
    _complete_node(node, state, result, raw, visit, 0, options)
    return result


def criticize(statement, solution, round_number, model=CRITIC_MODEL, effort=EFFORT, instructions=None, speed=DEFAULT_SPEED, prompts=None, summary=DEFAULT_REASONING_SUMMARY):
    options = {"model": model, "effort": effort, "speed": speed, "summary": summary, "prompts": {"critic": instructions} if instructions is not None else {}}
    return _builtin_node("author_critic", "critic", {"statement": statement, "solution": solution}, options, round_number, prompts)


_CONVERSATION_ID = re.compile(r"c_[0-9a-fA-F]{10}")


def conversation_request(source):
    """The stdin request of `--converse`, checked; any other input raises Error("Invalid conversation request.").

    {"schemaVersion": 1, "id": "c_" and 10 hex digits, "kind": "summary" or
    "message", "text": the message (1 to CONVERSATION_MAX_CHARS characters
    after strip; empty for a summary), "notice": text put in front of it (at
    most CONVERSATION_MAX_CHARS characters, often empty)}; no NUL anywhere.
    """

    try:
        request = json.loads(source)
    except (ValueError, RecursionError):
        request = None
    if not isinstance(request, dict):
        raise Error("Invalid conversation request.")
    version, identity, kind, message, notice = (request.get(key) for key in (
        "schemaVersion", "id", "kind", "text", "notice"))
    if not (type(version) is int and version == 1
            and isinstance(identity, str) and _CONVERSATION_ID.fullmatch(identity)
            and isinstance(kind, str) and kind in CONVERSATION_LABELS
            and isinstance(message, str) and isinstance(notice, str)
            and "\0" not in message and "\0" not in notice and len(notice) <= CONVERSATION_MAX_CHARS
            and (0 < len(message.strip()) <= CONVERSATION_MAX_CHARS if kind == "message" else not message.strip())):
        raise Error("Invalid conversation request.")
    return {"schemaVersion": 1, "id": identity, "kind": kind, "text": message.strip(), "notice": notice.strip()}


def run_author_conversation(workflow_path, options, request):
    """One conversation turn of a paused proof author (`--converse`); return the exit code 0.

    ``request`` comes from conversation_request(): a progress summary (the
    YAML ``summary`` prompt) or a reply to the user's message (``conversation``),
    led by its notice. Only the author node is prepared, so only its model and
    credentials are checked, and the turn runs outside the time budget: the
    time limit, live-instruction and pause files are dropped. The turn runs in
    the author's saved conversation (author_conversation(), or
    claude_cli.author_conversation() for Claude models) and every model-call
    failure is retried after MODEL_RETRY_SECONDS without a limit, in the same
    conversation: a message the model never received is sent again, one it
    received and answered with nothing is removed and sent again, and a reply
    that was cut off is asked for again (CONVERSATION_RETRY_NOTE). The waits
    never enter the workflow's retry clock, so no "Workflow clock" record
    appears. The turn ends with one record: conversation_reply {label,
    conversationKind, text, threadId, compacted}, or, for a failure no retry
    can fix (a conversation that is gone, or that Codex or Claude refused to
    reopen AUTHOR_RESUME_REFUSALS times in a row; a safety stop; a settings or
    workflow problem), the status "Author reply failed" {final, failure:
    "unavailable", "safety" or "configuration", text}. Every record has stage
    "conversation", node "author" and the request's conversationId.
    """

    kind, conversation_id = request["kind"], request["id"]
    request_label, reply_label = CONVERSATION_LABELS[kind]
    options = {**options, "workflow_stages": {"review": False, "author": True, "critic": False, "writing": False},
               "thinking_hours": 0}
    for name in ("author_limit_file", "author_steer_file", "goal_pause_file"):
        options.pop(name, None)
    record = {"node": "author", "conversationId": conversation_id}

    def failed(text, failure):
        emit("status", "conversation", label="Author reply failed", final=True, failure=failure, text=text,
             **record)
        return 0

    def prepared():
        """The turn's instruction, settings, session function and features: only the author is checked."""

        workflow = load_workflow(workflow_path)
        node = next((node for node in workflow["nodes"].values() if node["run"] == "goal"), None)
        if node is None:
            raise WorkflowDefinitionError("The workflow has no goal node for the proof author.")
        prompts = prepare(workflow, options)
        lifecycle = node["lifecycle"]
        if isinstance(lifecycle, list):
            lifecycle = {key: key for key in lifecycle}
        key = "summary" if kind == "summary" else "conversation"
        if key not in lifecycle:
            raise WorkflowDefinitionError(f"The workflow's author node binds no {key} prompt.")
        try:
            body = render_template(prompts[lifecycle[key]], {"message": request["text"]} if key == "conversation"
                                   else {})
        except ValueError as exc:
            raise WorkflowDefinitionError(str(exc)) from exc
        instruction = f"{request['notice']}\n\n{body}" if request["notice"] else body
        settings = _settings(node, options)
        try:
            claude = providers.harness(settings["model"]) == providers.CLAUDE
        except providers.StoreUnavailable as exc:
            raise Error(str(exc)) from exc  # Read again at the next attempt.
        return {"instruction": instruction, "settings": settings, "features": list(node.get("features", [])),
                "session": claude_cli.author_conversation if claude else author_conversation,
                "cli": "Claude" if claude else "Codex"}

    # The prepared turn; the instruction of the next attempt (the turn's own, or the retry note),
    # the failed turn it removes first, and the refusal to reopen the conversation in a row.
    turn, current, rewind, classic, refusal = None, None, None, False, (None, 0)
    attempt, since = 1, None
    while True:
        try:
            if turn is None:
                turn = prepared()
            result = turn["session"](
                sys.modules[__name__], current or turn["instruction"], settings=turn["settings"], options=options,
                features=turn["features"], conversation_id=conversation_id, label=request_label, attempt=attempt,
                rewind=rewind, classic=classic)
        except (FinalError, retrieval_runtime.RetrievalPolicyError) as exc:
            failure = ("unavailable" if isinstance(exc, SavedConversationGone)
                       else "safety" if isinstance(exc, (SafetyStop, retrieval_runtime.RetrievalPolicyError))
                       else "configuration")
            return failed(str(exc) or type(exc).__name__, failure)
        except (Error, OSError) as exc:
            # A model this account cannot use, or an endpoint that refuses the request: no retry helps.
            unusable = settings_failure(exc, turn["settings"]["model"] if turn else None)
            if unusable is not None:
                return failed(str(unusable), "configuration")
            if isinstance(exc, ConversationRefused):
                count = refusal[1] + 1 if refusal[0] == exc.refusal else 1
                refusal = (exc.refusal, count)
                if count >= AUTHOR_RESUME_REFUSALS:
                    return failed(f"{turn['cli']} refused to reopen the saved author conversation {count} times: "
                                  f"{exc.refusal}", "unavailable")
            else:
                refusal = (None, 0)  # Only identical refusals in a row count.
            retry = exc.retry if isinstance(exc, ConversationAttemptFailed) else {}
            if since is None or retry.get("output"):
                since = _wall_clock()
            if not retry.get("delivered"):
                rewind = None  # The model never received it: the same instruction again.
            elif not retry.get("output"):
                rewind = retry.get("rewind")  # Received but answered with nothing: removed, then sent again.
            else:
                current, rewind = CONVERSATION_RETRY_NOTE, None  # A reply was cut off: asked for again.
            classic = classic or bool(retry.get("classic"))
            attempt += 1
            wait_before_retry(options, "conversation", attempt, exc, node="author",
                              thread_id=options.get("goal_thread_id"), since=since,
                              record={"conversationId": conversation_id})
        else:
            emit("conversation_reply", "conversation", label=reply_label, conversationKind=kind,
                 text=result["reply"], threadId=result["threadId"], compacted=bool(result.get("compacted")),
                 **record)
            return 0


def main(argv=None):
    configure_standard_streams()
    # A runner killed before its cleanup may have left a key-bearing Claude
    # settings folder behind.
    claude_cli.sweep_stale_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=HelpFormatter)
    parser.add_argument("workflows", nargs="+", help="YAML files, executed in order")
    models = (f"{', '.join(OFFERED_MODELS)}, or an api:<id> model from Advanced settings → API connections "
              "(the older GPT-5.6 ids are still accepted)")
    parser.add_argument("--model", type=model_argument, default=MODEL, metavar="MODEL",
                        help=f"fallback model: {models}")
    for role in ("author", "critic", "writer"):
        parser.add_argument(f"--{role}-model", type=model_argument, metavar="MODEL",
                            help=f"{role} model; same choices as --model")
        parser.add_argument(f"--{role}-effort", type=effort_argument, metavar=EFFORT_METAVAR)
    parser.add_argument("--reasoning-effort", dest="effort", type=effort_argument, metavar=EFFORT_METAVAR,
                        default=EFFORT)
    parser.add_argument("--speed", choices=SPEEDS, default=DEFAULT_SPEED)
    parser.add_argument("--critic-rounds", type=critic_limit)
    parser.add_argument("--critic-count", type=critic_count,
                        help=f"independent critics per critic round: the subagents the critic starts "
                             f"(1 to {MAX_CRITIC_COUNT})")
    parser.add_argument("--author-subagents", type=author_subagents,
                        help=f"subagents the proof author may start at a time (0 to {MAX_AUTHOR_SUBAGENTS}; "
                             "0, the default, gives it no subagent tool)")
    parser.add_argument("--thinking-hours", type=author_hours, default=DEFAULT_AUTHOR_HOURS)
    parser.add_argument("--elapsed-seconds", type=prior_elapsed_seconds, default=0)
    parser.add_argument("--author-limit-file")
    parser.add_argument("--author-steer-file")
    parser.add_argument("--reasoning-summary", dest="summary", choices=REASONING_SUMMARIES, default=DEFAULT_REASONING_SUMMARY)
    parser.add_argument("--state-file", help="Read initial workflow state from a JSON object instead of stdin")
    parser.add_argument("--start-node", help="Enter the first workflow at this named node")
    parser.add_argument("--set", dest="settings", action="append", default=[], metavar="NAME=VALUE", help="override any named option; JSON values are accepted")
    for role in PROMPT_FILE_OPTIONS:
        parser.add_argument(f"--{role}-prompt-file")
    parser.add_argument("--converse", action="store_true",
                        help="one conversation turn of the paused author of one workflow: a progress summary or a "
                             "reply to the user's message; the JSON request is read from stdin")
    try:
        args = parser.parse_args(argv)
        options = {key: value for key, value in vars(args).items() if value is not None}
        converse = options.pop("converse", False)
        options["prompts"] = {}
        for role, name in PROMPT_FILE_OPTIONS.items():
            path = options.pop(f"{role}_prompt_file", None)
            if path:
                options["prompts"][name] = prompt_file(path, None)
        for setting in options.pop("settings"):
            key, separator, value = setting.partition("=")
            if not separator or not key:
                raise Error("Use --set NAME=VALUE.")
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
            options[key] = value
        for name in SUBAGENT_OPTIONS:
            if name in options:
                options[name] = SUBAGENT_OPTIONS[name](options[name])  # --set critic_count=N too.
        # A saved job, script or flag may still say Ultra: it runs as Max, and the log says so once.
        note_retired_efforts([options.get(name) for name in ("effort", "author_effort", "critic_effort",
                                                              "writer_effort")])
        if converse:
            if len(args.workflows) != 1:
                raise Error("--converse takes exactly one workflow.")
            return run_author_conversation(args.workflows[0], options, conversation_request(sys.stdin.read()))
        if args.state_file:
            state = json.loads(Path(args.state_file).read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise Error("Workflow state must be a JSON object.")
        else:
            source = text(sys.stdin.read())
            if "\0" in source:
                raise Error("Workflow input cannot contain NUL characters.")
            state = {"input": source, "statement": source, "source": source}
        # The runner works in the run folder: failed model calls are logged there.
        options.setdefault("failure_log", failure_log_path())
        check_clis(_workflow_models(args.workflows, options))
        state = execute_workflows(args.workflows, state, options)
        if state.get("paused"):
            return 0
        if state.get("failed"):
            return 1
        emit("workflow_result", "workflow", output=state.get("output", ""))
        return 0
    except KeyboardInterrupt:
        print("\nStopped workflow.", file=sys.stderr)
        return 130
    except (Error, ValueError, OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"error: {redact(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a harness bug still ends with one plain error line.
        print(f"error: {_unexpected_failure(exc)}", file=sys.stderr)
        return 1


def _unexpected_failure(exc):
    """One line naming an unexpected exception and where it was raised; its traceback becomes one diagnostic."""

    import traceback

    emit("diagnostic", "workflow", text="The workflow runner failed unexpectedly:\n"
                                        + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" in {frames[-1].name} ({Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    message = " ".join(redact(exc).split())
    return (f"The workflow runner failed unexpectedly ({type(exc).__name__}{where})"
            + (f": {message}" if message else "."))


def failure_log_path(directory=None):
    """The MODEL_FAILURE_LOG of the run folder `directory` (the working directory by default)."""

    return str(Path(directory or Path.cwd()) / MODEL_FAILURE_LOG)


def _workflow_models(paths, options):
    """Every model that the nodes of these workflows would use; an invalid workflow is reported later."""

    models = []
    for path in paths:
        try:
            nodes = load_workflow(path)["nodes"].values()
            for node in nodes:
                if node["run"] != "command":
                    models.append(_settings(node, options)["model"])
        except (Error, ValueError, KeyError, TypeError, OSError):
            continue
    return models


def check_clis(models, directory=None):
    """Name the path and version of each CLI these models need before any model call, and any that cannot run.

    It runs `codex --version` or `claude --version` (no model call) and
    remembers the answers in CLI_VERSIONS_FILENAME, so a CLI that changed
    since the job last started is named too. Nothing waits: model calls
    retry every minute until the CLI works. Web-cutoff runs check their
    pinned binary in their own preflight instead. Where model processes
    cannot be confined to their run (not macOS), a diagnostic says so.
    """

    directory = Path(directory or Path.cwd())
    try:
        if retrieval_runtime.policy_for_workspace(directory):
            return
    except (retrieval_runtime.RetrievalPolicyError, OSError, ValueError):
        return  # The run's own checks report a broken policy.
    if models and not confinement.supported():
        # Said once per runner; the run continues with the legacy sandboxes (see README).
        emit("diagnostic", "workflow", text=confinement.UNSUPPORTED_NOTICE)
    names = []
    for model in models:
        try:
            name = "Claude" if providers.harness(chosen_model(model)) == providers.CLAUDE else "Codex"
        except (Error, ValueError, providers.ProviderError):
            continue
        if name not in names:
            names.append(name)
    found, problems = {}, []
    for name in names:
        try:
            path = claude_cli.executable(sys.modules[__name__]) if name == "Claude" else codex()
            result = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    env=environment(), timeout=CLI_CHECK_SECONDS)
        except (Error, OSError, subprocess.SubprocessError) as exc:
            problems.append(f"{name} CLI missing or incompatible: {exc}")
            continue
        version = _clipped(" ".join(result.stdout.split()), 200)
        if result.returncode or not version:
            problems.append(f"{name} CLI missing or incompatible: `{Path(path).name} --version` exited with code "
                            f"{result.returncode}" + (f" ({version})." if version else "."))
            continue
        found[name] = {"path": path, "version": version}
    record = directory / CLI_VERSIONS_FILENAME
    try:
        previous = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        previous = {}
    previous = previous if isinstance(previous, dict) else {}
    if found:
        emit("status", "workflow", label="CLI check", cliVersions=found,
             text=" ".join(f"{name}: {item['version']} ({item['path']})." for name, item in found.items()))
    for problem in problems:
        emit("diagnostic", "workflow", text=f"{problem} Model calls retry every minute until it works.")
    for name, item in found.items():
        before = previous.get(name)
        if isinstance(before, dict) and before != item:
            emit("diagnostic", "workflow",
                 text=f"The {name} CLI changed since this job last started: {before.get('version')} "
                      f"({before.get('path')}) is now {item['version']} ({item['path']}).")
    if any(previous.get(name) != item for name, item in found.items()):
        try:
            _private_atomic_write(record, json.dumps({**previous, **found}, indent=2, sort_keys=True) + "\n")
        except (OSError, Error):
            pass


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # SIGTERM unwinds like Ctrl-C, so cleanup still removes private settings.
    signal.signal(signal.SIGTERM, _interrupt)
    raise SystemExit(main())
