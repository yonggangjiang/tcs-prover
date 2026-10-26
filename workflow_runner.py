#!/usr/bin/env python3
"""Execute declarative YAML workflows using persistent goals and structured model calls."""

import argparse
import ast
import errno
import functools
import hashlib
import json
import math
import os
import operator
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, proof_handoff, providers
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Efforts each built-in model lists. Codex does not validate or clamp efforts
# locally, so the UI offers only these; effective_effort() maps the rest.
MODEL_EFFORTS = {model: providers.efforts_for(model) for model in MODELS}
SPEEDS, DEFAULT_SPEED = ("standard", "fast"), "standard"
AUTHOR_MODEL = CRITIC_MODEL = WRITER_MODEL = MODEL
MAX_CRITIC_ROUNDS = 100
MAX_AUTHOR_HOURS = 168
DEFAULT_AUTHOR_HOURS = MAX_AUTHOR_HOURS
STRUCTURED_WORKSPACE = ROOT / ".codex-structured-workspace"
_GOAL_LIFECYCLE = ("goal", "continuation", "compaction", "repair", "resume")
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

# The Codex permission profile that hides TCS Prover's private state from
# sandboxed commands (see private_permissions()).
PRIVATE_PERMISSIONS = "tcs_prover_private"

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

# One structured attempt's timeout. Each consecutive timed-out attempt of the
# same call doubles the next one (up to the cap and the remaining budget), so
# an answer that needs longer still arrives.
STRUCTURED_TIMEOUT_SECONDS = 900
MAX_ATTEMPT_TIMEOUT_SECONDS = 24 * 3600

# Removing one persisted structured session after its call never takes longer.
SESSION_CLEANUP_SECONDS = 30

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

# Appended to the reason of an author pause that no automatic retry can fix.
UNAVAILABLE_THREAD_HINT = (
    " Codex no longer has this conversation, so automatic retry is off. Choose Continue proof "
    "author to continue the saved work in a new conversation."
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

CRITIC_AUDIT_TIMEOUT_SECONDS = 1800

CRITIC_COORDINATOR_TIMEOUT_SECONDS = 1800

CRITIC_AUDIT_CHECKPOINT_FILENAME = "critic-audits.json"

CRITIC_AUDIT_RECOVERY_DISABLED_FILENAME = "fresh-critic-audits"

CRITIC_AUDIT_CHECKPOINT_SCHEMA_VERSION = 1

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

    A structured call ends the run with it (as before automatic retries); an
    author pauses with it as its reason.
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


class RetryCancelled(Error):
    """A sibling request ended the workflow step while this one waited to retry."""


def retryable_failure(error):
    """Whether a failed model-call attempt can succeed when it is tried again.

    Provider and transport failures (a CLI that failed, crashed or could not
    start, an error or failed-turn event, a timed-out attempt, a conversation
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


def author_hours(value):
    """Accept zero for unlimited, or a finite limit of at most one week."""

    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise Error("The total workflow time limit must be a number of hours.") from exc
    if not math.isfinite(value) or not 0 <= value <= MAX_AUTHOR_HOURS:
        raise Error(f"Choose 0 for unlimited, or at most {MAX_AUTHOR_HOURS} hours.")
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
    """Require a built-in model, an older Codex id, or a configured API model."""

    if isinstance(value, str) and providers.is_known(value):
        return value
    if providers.replacement_for_removed(value):
        raise Error("DeepSeek is no longer supported; choose another model, for example gpt-6.1-sol.")
    shown = repr(value)[:80] if isinstance(value, str) else type(value).__name__
    raise Error(
        f"Unknown model {shown}. Choose {', '.join(OFFERED_MODELS)}, or an api:<id> model "
        "configured in Advanced settings → API models."
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
    """Require one supported Codex reasoning effort."""

    if value not in EFFORTS:
        raise Error("Choose a valid reasoning effort.")
    return value


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

CODEX_HOME_PROBE_SECONDS = 30


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
    """Ask an app-server which CODEX_HOME it uses (an initialize handshake, no model request)."""

    try:
        process = subprocess.Popen(
            [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            env=environment, cwd=environment["CODEX_HOME"],
        )
    except OSError:
        return None
    reply = {}

    def read():
        try:
            process.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "tcs_prover_probe", "title": "TCS Prover", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }}) + "\n")
            process.stdin.flush()
            for line in process.stdout:
                message = json.loads(line)
                if message.get("id") == 1:
                    reply.update(message.get("result") or {})
                    return
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(CODEX_HOME_PROBE_SECONDS)
    stop_process(process)
    reader.join(timeout=1)
    for stream in (process.stdin, process.stdout):
        try:
            stream.close()
        except OSError:
            pass
    home = reply.get("codexHome")
    return Path(home).resolve() if isinstance(home, str) and home else None


def api_codex_executable(environment):
    """A Codex CLI verified to honor the private CODEX_HOME of an API model.

    Some installations wrap codex in a launcher that resets CODEX_HOME, which
    would hand the user's ChatGPT login and connectors to a third-party model.
    Each candidate reports its home through an initialize handshake; the first
    one that uses the private home runs the API model, and none means refusal.
    """

    home = Path(environment["CODEX_HOME"]).resolve()
    # The probe needs only CODEX_HOME. A launcher may start the user's own
    # Codex configuration, which must never receive the key.
    probe = {name: value for name, value in environment.items() if name not in SECRET_ENVIRONMENT}
    for candidate in _codex_candidates():
        key = (candidate, str(home))
        if key not in _ISOLATED_CODEX:
            _ISOLATED_CODEX[key] = _reported_codex_home(candidate, probe) == home
        if _ISOLATED_CODEX[key]:
            return candidate
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
    except (OSError, providers.ProviderError) as exc:
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
            # The custom provider reads this environment value (as a Bearer
            # token or in the configured header). The key never appears in
            # process arguments.
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
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise ConfigurationError(str(exc)) from exc


def retrieval_environment(policy, role, model=None, inherited=None):
    try:
        if policy and model is not None and model_provider(model) != OPENAI_PROVIDER:
            raise retrieval_runtime.RetrievalPolicyError(HISTORICAL_CODEX_ONLY)
        return retrieval_runtime.preflight(
            policy, role, retrieval_runtime.codex_executable(policy) if policy else "",
            environment(model) if inherited is None else inherited,
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
    """Stop a child promptly, escalating only when termination is ignored."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
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
    """One bounded structured request stopped waiting for its provider."""

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
                "critic": "critic", "final": "LaTeX editor"}


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
    (a parallel request that still runs keeps it going). Each change is
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


def _failure_reason(text, limit=300):
    """The provider's own message, without the runner's prefix, on one short line."""

    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    value = lines[0] if lines else ""
    value = re.sub(r"^(?:(?:Codex|Claude) [\w-]+ failed|(?:Claude )?[Aa]uthor turn failed|Author stopped|"
                   r"Could not resume the saved (?:Codex|Claude) conversation; no replacement was started\.)\s*:?\s*",
                   "", value).strip()
    if not value and len(lines) > 1:
        value = lines[1]
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
    text = redact(str(error) or type(error).__name__)
    reason = _failure_reason(text)
    if isinstance(error, StructuredAttemptTimeout) and error.seconds:
        return f"no final answer within {_duration_text(error.seconds)}", reason
    return _plain_failure(text) or reason or type(error).__name__, reason


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
                      activity=None, since=None, timeouts=0, next_timeout=None):
    """Wait MODEL_RETRY_SECONDS (plus a few seconds of jitter) before attempt `attempt` of a failed model call.

    The job stays running and a status event says why, how long the call has
    been failing (`since`, a time.time() value) and, after timed-out
    attempts, how long the next attempt may run. The wait does not consume
    the workflow time budget (RetryClock). It ends at once on Stop
    (KeyboardInterrupt) or when a sibling request cancels the step; the pause
    file pauses the workflow, and a time limit lowered meanwhile ends it.
    """

    plain, reason = describe_failure(error)
    nominal = max(0.0, float(MODEL_RETRY_SECONDS))
    seconds = nominal + _retry_jitter(nominal)
    clock = retry_clock(options)
    where = {**({"node": node} if node else {}), **({"activityLabel": activity} if activity else {})}
    label = f"{activity} · {RETRY_LABEL}" if activity else RETRY_LABEL
    invalid = isinstance(error, InvalidAnswer)
    subagents = isinstance(error, SubagentFailure)
    details = [f"attempt {attempt}"]
    spent = max(0.0, _wall_clock() - since) if since is not None else 0.0
    if spent >= 60:
        details.append(f"retrying for {_span_text(spent)}")
    if timeouts and next_timeout:
        details.append((f"{timeouts} timeouts in a row; " if timeouts > 1 else "")
                       + f"the next attempt may run {_span_text(next_timeout)}")
    text = (f"The model's answer could not be accepted ({plain})" if invalid
            else f"The answer was set aside because {plain}" if subagents
            else f"Model call failed ({plain})") + f" — retrying in {_duration_text(nominal)} ({', '.join(details)})."
    if reason and reason != plain:
        text += f" {'Detail' if invalid else 'Provider message'}: {reason}"
    said = _explanation_excerpt(getattr(error, "raw", "")) if invalid else ""
    if said:
        text += f" The model said: {said}"
    change = clock.wait_started()
    emit("status", stage, label=label, text=text, retryAttempt=attempt, retryInSeconds=nominal,
         retryAt=_wall_clock() + seconds, failure=plain, failureReason=reason,
         failureKind="invalid_answer" if invalid else "subagent" if subagents
         else "timeout" if isinstance(error, StructuredAttemptTimeout) else "model_call", **({"retryingSeconds": round(spent)} if since is not None else {}),
         **({"consecutiveTimeouts": timeouts, "nextAttemptTimeout": next_timeout} if timeouts and next_timeout else {}),
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
    """Read one new controller-authored live instruction, if available."""

    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        command_id = str(value["id"]).strip()
        instruction = str(value["instruction"]).strip()
    except (OSError, UnicodeError, KeyError, TypeError, ValueError,
            json.JSONDecodeError):
        return None
    if (
        not command_id or command_id == delivered_id or not instruction
        or "\0" in instruction or len(instruction) > AUTHOR_STEER_MAX_CHARS
    ):
        return None
    return command_id, instruction

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
    user_home = Path(effective.get("HOME") or Path.home())
    configured_home = effective.get("CODEX_HOME")
    if configured_home == "~" or (configured_home or "").startswith("~/"):
        home = user_home / configured_home[2:]
    else:
        home = Path(configured_home).expanduser() if configured_home else user_home / ".codex"
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

    The endpoint and header come from the API model store; the key stays in
    the child environment (environment()). Built-in models need no flags.
    """

    if model_provider(model) != API_PROVIDER or providers.harness(model) != providers.CODEX:
        return []
    entry = providers.api_model(model)
    header = entry["headerName"]
    if not providers.HEADER_NAME.match(header):
        raise ConfigurationError("The API model's header name is invalid.")
    authentication = (
        # Codex sends this variable as "Authorization: Bearer <key>".
        f'model_providers.{API_PROVIDER}.env_key="{API_TOKEN_ENV}"'
        if header.lower() == "authorization" else
        f'model_providers.{API_PROVIDER}.env_http_headers={{{toml_string(header)}="{API_TOKEN_ENV}"}}'
    )
    # No forced_login_method: Codex enforces it by deleting a stored ChatGPT
    # login, which a launcher that resets CODEX_HOME would expose (verified
    # with Codex 0.160). The provider authenticates only with the variable or
    # header below; requires_openai_auth=false keeps any OpenAI login out of
    # its requests.
    return [
        "-c", f'model_provider="{API_PROVIDER}"',
        "-c", f"model_providers.{API_PROVIDER}.name={toml_string(entry['label'])}",
        "-c", f"model_providers.{API_PROVIDER}.base_url={toml_string(entry['baseUrl'].rstrip('/'))}",
        "-c", f'model_providers.{API_PROVIDER}.wire_api="responses"',
        "-c", f"model_providers.{API_PROVIDER}.supports_websockets=false",
        "-c", f"model_providers.{API_PROVIDER}.requires_openai_auth=false",
        "-c", authentication,
        # The model's shell commands never inherit the key: by default Codex
        # 0.160 passes *KEY*/*TOKEN* variables to them (verified with a mock
        # endpoint). Both settings drop it; either alone suffices.
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        "-c", f'shell_environment_policy.exclude=["{API_TOKEN_ENV}","OPENAI_API_KEY"]',
    ]


def private_permissions(base, model, workspace_write=None):
    """A Codex permission profile: the built-in `base` without TCS Prover's private state.

    Codex's read-only and workspace sandboxes let commands read every file the
    user can, including the API model store. This profile keeps the built-in
    sandbox and denies the private paths, and for API models also the user's
    CLI logins (`model` None is a native model, as research auditors use).
    Verified with Codex 0.160's macOS Seatbelt sandbox: other
    platforms return None and keep the legacy sandbox modes (see README).

    A derived ":workspace" profile keeps neither the read-only .codex, .agents,
    .git and .aws folders of the workspace (a run folder under /tmp or $TMPDIR
    could then plant a .codex/config.toml whose commands Codex starts outside
    the sandbox on resume) nor the user's [sandbox_workspace_write] settings,
    which the legacy workspace-write sandbox applied. Both are restored here;
    `workspace_write` is that table as Codex's config/read reports it.
    """

    if sys.platform != "darwin":
        return None
    paths = providers.private_paths()
    if model is not None and model_provider(model) == API_PROVIDER:
        paths += providers.user_login_files()
    denied = list(dict.fromkeys(paths))
    profile = {"extends": base, "filesystem": {str(path): "deny" for path in denied}}
    if base != ":workspace":
        return profile
    filesystem = profile["filesystem"]
    filesystem[":workspace_roots"] = {
        ".": "write", **{name: "read" for name in retrieval_runtime.PROTECTED_DIRECTORIES}}
    legacy = workspace_write if isinstance(workspace_write, dict) else {}
    for root in legacy.get("writable_roots") or ():
        resolved = Path(os.path.realpath(root)) if isinstance(root, str) and os.path.isabs(root) else None
        # A writable root never reopens private state (a deny entry stays more specific).
        if resolved and not any(resolved == path or path in resolved.parents for path in denied):
            filesystem.setdefault(root, "write")
    if legacy.get("exclude_slash_tmp") is True:
        filesystem[":slash_tmp"] = "read"
    if legacy.get("exclude_tmpdir_env_var") is True:
        filesystem[":tmpdir"] = "read"
    if legacy.get("network_access") is True:
        profile["network"] = {"enabled": True}
    return profile


def _toml_inline(value):
    """A permission-profile value as TOML: strings, booleans and nested tables."""

    if isinstance(value, dict):
        return "{" + ", ".join(f"{toml_string(key)}={_toml_inline(item)}" for key, item in value.items()) + "}"
    return str(value).lower() if isinstance(value, bool) else toml_string(value)


def private_permission_arguments(base, model):
    """Flags selecting private_permissions() for `codex exec`; empty where unsupported."""

    profile = private_permissions(base, model)
    if profile is None:
        return []
    return [
        "-c", f'default_permissions="{PRIVATE_PERMISSIONS}"',
        "-c", f"permissions.{PRIVATE_PERMISSIONS}.extends={toml_string(base)}",
        "-c", f"permissions.{PRIVATE_PERMISSIONS}.filesystem={_toml_inline(profile['filesystem'])}",
    ]

def require_model_credentials(model):
    """Fail before launching a CLI that is missing or a key that was never saved."""

    model = chosen_model(model)
    if providers.provider_kind(model) == providers.API and not providers.api_model_secret(model):
        raise ConfigurationError(f"Add an API key for {providers.model_label(model)} in Advanced settings → API models.")
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

def structured_tool_arguments(stage, features=(), policy=None):
    """Apply the run's search mode; keep other optional tools controlled by YAML."""
    return [
        *retrieval_runtime.native_search_arguments(policy=policy),
        "-c", "features.view_image=false",
        *[argument for feature in ("shell_tool", "multi_agent") if feature not in features for argument in ("--disable", feature)],
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
        self.subagent_failures = {}

    def subagent(self, identity, status, message=""):
        """A transport saw subagent ``identity`` fail ("failed", with the provider's message) or finish ("completed").

        A subagent that failed and then finished (the model continued it) no
        longer counts as failed.
        """

        with self._lock:
            if status == "failed":
                self.subagent_failures[str(identity)] = str(message or "")
            elif status == "completed":
                self.subagent_failures.pop(str(identity), None)

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
            cause = f"was stopped after {_duration_text(failure.seconds)}"
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


class RequestMonitor:
    """Watchdog, liveness heartbeat and stderr tail of one structured CLI request.

    Shared by the Codex and Claude transports: start() arms the timeout and the
    heartbeat after the prompt was written; close() always stops the process.
    """

    def __init__(self, process, stage, model, effort, *, timeout, activity_label, cli="Codex", redact=None):
        self.process, self.stage, self.model, self.effort = process, stage, model, effort
        self.timeout = None if timeout is None else float(timeout)
        self.activity_label, self.cli = activity_label, cli
        self.redact = redact or (lambda value: value)
        self.timed_out = threading.Event()
        self.stderr_lines = deque(maxlen=50)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._progress = {"lastEventAt": self._started_at, "eventCount": 0, "contentEventCount": 0}
        self._watchdog = (threading.Timer(self.timeout, self._enforce_timeout)
                          if self.timeout is not None else None)
        if self._watchdog is not None:
            self._watchdog.daemon = True
        self._heartbeat = threading.Thread(target=self._relay_heartbeat, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    def _enforce_timeout(self):
        if self.process.poll() is None:
            self.timed_out.set()
            stop_process(self.process)

    def _relay_heartbeat(self):
        while not self._stop.wait(STRUCTURED_HEARTBEAT_SECONDS):
            now = time.monotonic()
            with self._lock:
                quiet = now - self._progress["lastEventAt"]
                count = self._progress["eventCount"]
                content_count = self._progress["contentEventCount"]
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
                    f"last public event {quiet:.0f}s ago. If content is "
                    "still zero, the selected provider may be computing "
                    "or queued server-side; its API does not expose which."
                ),
                heartbeat=True, elapsedSeconds=round(elapsed, 1),
                quietSeconds=round(quiet, 1), publicEventCount=count,
                contentEventCount=content_count, processAlive=alive,
                requestPid=self.process.pid, model=self.model,
                modelProvider=model_provider(self.model),
                reasoningEffort=self.effort,
            )

    def _read_stderr(self):
        for stderr_line in self.process.stderr:
            value = stderr_line.strip()
            if value:
                self.stderr_lines.append(self.redact(value))

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
        if self._watchdog is not None:
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
        if self._watchdog is not None:
            self._watchdog.cancel()
        stop_process(self.process)
        self._stderr_reader.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (AttributeError, OSError):
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


def run_structured_attempt(
    prompt, schema_value, stage, model, effort, speed, summary,
    timeout=None, activity_label=None, features=(), input_files=None, conversation=None,
):
    """Run one structured CLI process (Codex or Claude) and return its final text.

    With a StructuredConversation the session persists and, when the
    conversation says so, this attempt forks its base (see structured()).
    """

    policy = retrieval_policy(features=features)
    claude = providers.harness(chosen_model(model)) == providers.CLAUDE
    if claude and policy:
        raise ConfigurationError(HISTORICAL_CODEX_ONLY)
    if conversation is not None and policy:
        # Historical sessions stay ephemeral; every retry re-runs the saved input.
        conversation.resumable = False
        conversation.forget()

    def claude_request(workspace):
        # Claude reads files with its Read tool; the snapshot's shell_tool is Codex plumbing.
        return claude_cli.run_structured(
            prompt, schema_value, stage, model, effort, summary, timeout=timeout,
            activity_label=activity_label, features=features, workspace=workspace,
            environment=environment(model), runtime=sys.modules[__name__],
            **({"conversation": conversation} if conversation is not None else {}),
        )

    if input_files is not None:
        # The tuple holds captured text, never paths to mutable author files.
        if (not isinstance(input_files, tuple) or len(input_files) != 3
                or {name for name, _ in input_files} != {"proof.md", "statement.md", "manifest.json"}
                or not all(isinstance(value, str) for _, value in input_files)):
            raise ConfigurationError("Invalid proof snapshot bundle.")
        snapshot_features = tuple(dict.fromkeys((*features, "shell_tool")))

        def snapshot_ready(workspace):
            emit("status", stage, label="Proof snapshot ready",
                 text="The critic and its subagents can read proof.md and statement.md in their read-only workspace.",
                 snapshotWorkspace=str(workspace), snapshotManifest=json.loads(dict(input_files)["manifest.json"]))

        def review(snapshot_policy, workspace):
            environment_value = None if claude else retrieval_environment(snapshot_policy, "critic", model)
            if claude:
                return claude_request(workspace)
            return _run_structured_process(
                prompt, schema_value, stage, model, effort, speed, summary,
                timeout=timeout, activity_label=activity_label, features=snapshot_features,
                policy=snapshot_policy, child_environment=environment_value, workspace=workspace,
                conversation=conversation,
            )

        if policy:
            try:
                with retrieval_runtime.critic_session(policy) as snapshot_policy:
                    workspace = _write_snapshot(retrieval_runtime.role_workspace(snapshot_policy, "critic"),
                                                input_files)
                    snapshot_ready(workspace)
                    return review(snapshot_policy, workspace)
            except retrieval_runtime.RetrievalPolicyError as exc:
                raise ConfigurationError(str(exc)) from exc
        # Keep the exact reviewed artifacts for diagnosis after the process exits.
        # Every attempt of one call reviews the same snapshot, where a forked
        # session finds the files it already read.
        workspace = conversation.workspace if conversation is not None else None
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
            workspace = _write_snapshot(Path(tempfile.mkdtemp(prefix="tcs-proof-review-")), input_files)
            snapshot_ready(workspace)
            if conversation is not None:
                conversation.workspace = workspace
        return review(None, workspace)
    if claude:
        return claude_request(structured_workspace())
    child_environment = retrieval_environment(policy, "structured", model)
    workspace = retrieval_runtime.role_workspace(policy, "structured") if policy else structured_workspace()
    return _run_structured_process(
        prompt, schema_value, stage, model, effort, speed, summary,
        timeout=timeout, activity_label=activity_label, features=features,
        policy=policy, child_environment=child_environment, workspace=workspace,
        conversation=conversation,
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
    *, timeout, activity_label, features, policy, child_environment, workspace, conversation=None,
):
    timeout = None if timeout is None else float(timeout)
    # Verified with Codex 0.160: without --ephemeral `codex exec` persists the
    # session and reports its id (thread.started); `codex exec fork <id> -`
    # accepts the same root flags (model, effort, features, -C, sandbox or
    # permission profile, config), copies the saved conversation into a new
    # session and leaves the original untouched; `codex delete --force <id>`
    # removes a session with its subagent threads. memories.generate_memories
    # =false keeps these sessions out of Codex's memory of the user's own work.
    persistent = conversation is not None and conversation.resumable and not policy
    fork = conversation.fork_from if persistent and conversation.fork_from else None
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        schema = folder / "schema.json"
        answer = folder / "answer.json"
        schema.write_text(json.dumps(schema_value), encoding="utf-8")
        schema_arguments = output_schema_arguments(model, schema)
        if policy:
            executable = retrieval_runtime.codex_executable(policy)
        elif model_provider(model) == API_PROVIDER:
            executable = api_codex_executable(child_environment)
        else:
            executable = codex()
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
            *structured_tool_arguments(stage, features, policy),
            *[argument for feature in features for argument in ("--enable", feature)],
            # "-s read-only" would override the profile that hides private state.
            "-C", str(workspace),
            *([] if policy else private_permission_arguments(":read-only", model) or ["-s", "read-only"]),
            "-a", "never", "exec", *(["fork"] if fork else []),
            "--json", *([] if persistent else ["--ephemeral"]), "--skip-git-repo-check",
            *([] if policy else ["--ignore-user-config"]), *schema_arguments, "-o", str(answer),
            *([fork] if fork else []), "-",
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=child_environment,
        )
        monitor = RequestMonitor(process, stage, model, effort, timeout=timeout,
                                 activity_label=activity_label)
        provider_errors = deque(maxlen=10)
        started = False
        try:
            monitor.send_input(prompt)
            monitor.start()
            for line in process.stdout:
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
                    elif (persistent and isinstance(kind, str) and kind.startswith("item.")
                          and isinstance(raw_event.get("item"), dict)
                          and raw_event["item"].get("type") not in {None, "error"}):
                        # The model worked in this session: a retry continues from it.
                        conversation.output = True
                    item = raw_event.get("item")
                    if (conversation is not None and isinstance(item, dict) and item.get("type") == "collab_tool_call"
                            and isinstance(item.get("agents_states"), dict)):
                        # Codex 0.160 reports each subagent's state ({status, message})
                        # on the spawn_agent, send_input, wait and close_agent calls.
                        for thread, state in item["agents_states"].items():
                            status = state.get("status") if isinstance(state, dict) else None
                            if status in {"errored", "completed"}:
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
                        event_name = event.get("method") or event.get("type") or ""
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
        if monitor.timed_out.is_set():
            raise StructuredAttemptTimeout(
                f"The {stage} model did not respond within {monitor.timeout:g} seconds.", monitor.timeout,
            )
        if code:
            detail = "\n".join((*provider_errors, *monitor.stderr_lines)).strip()
            detail = _clipped(redact(detail), 8000)
            if fork and not started:
                # The fork never started (Codex no longer has the saved session,
                # or this CLI cannot fork it): nothing ran.
                raise ConversationUnavailable(f"Codex could not continue the saved {stage} conversation: {detail}")
            raise Error(
                f"Codex {stage} failed: {detail}"
                if detail else f"Codex {stage} failed without diagnostics."
            )
        # Codex writes the answer file; replace undecodable bytes so a damaged
        # answer is retried as an unusable answer, not a crash.
        return answer.read_text(encoding="utf-8", errors="replace") if answer.exists() else ""

def critic_audit_assignment_sha256(statement, solution, model, effort, instructions, config=None, identity=None):
    """Fingerprint the exact request inputs, model, settings, and parallel items."""
    config = _parallel_definition(config)
    identity = ({"statement": text(statement), "solution": text(solution), "instructions": text(instructions)} if identity is None else identity)
    assignment = {**identity, "model": chosen_model(model), "reasoningEffort": effective_effort(model, effort), config.get("checkpoint", {}).get("item_key", "items"): config["items"]}
    return _sha256(json.dumps(assignment, ensure_ascii=False, sort_keys=True))

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

def load_critic_audit_checkpoint(
    statement, solution, model, effort, instructions, directory=None, config=None, identity=None,
):
    """Restore only completed audits for this exact proof and configuration."""

    config = _parallel_definition(config)
    stage = config.get("stage", "critic")

    path = Path(directory or Path.cwd()) / config["checkpoint"]["file"]
    empty = [None] * len(config["items"])
    expected = critic_audit_assignment_sha256(
        statement, solution, model, effort, instructions, config=config, identity=identity,
    )
    candidates = [path]
    # A user may resume the original proof job instead of the latest failed
    # critic job. Exact fingerprints make it safe to recover paid audits from
    # sibling run folders without relying on the selected source folder.
    runs_directory = path.parent.parent
    recovery_disabled = (
        path.parent / config["checkpoint"].get("disabled", "fresh-critic-audits")
    ).is_file()
    if (
        not recovery_disabled
        and runs_directory.name == "runs"
        and runs_directory.is_dir()
    ):
        siblings = sorted(
            (
                item for item in runs_directory.glob(
                    f"*/{config['checkpoint']['file']}"
                )
                if item != path
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        candidates.extend(siblings)

    restored = list(empty)
    primary_problem = None
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
            reports = value.get("reports") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict)
                or value.get("schemaVersion")
                != CRITIC_AUDIT_CHECKPOINT_SCHEMA_VERSION
                or value.get("assignmentSha256") != expected
                or not isinstance(reports, list)
                or len(reports) != len(config["items"])
            ):
                raise ValueError("checkpoint belongs to another proof or setting")
            added = 0
            for index, report in enumerate(reports):
                if report is None or restored[index] is not None:
                    continue
                validate_json_schema(report, config["schema"])
                report = dict(report)
                if config.get("item_field"):
                    report[config["item_field"]] = config["items"][index]
                restored[index] = report
                added += 1
            if added and candidate != path:
                emit(
                    "status", stage,
                    label="Prior exact-proof audits found",
                    text=(
                        f"Recovered {added} completed independent audit"
                        f"{'s' if added != 1 else ''} from prior job "
                        f"{candidate.parent.name}."
                    ),
                    checkpoint=True, checkpointSource=candidate.parent.name,
                    recoveredAuditCount=added,
                )
            if all(report is not None for report in restored):
                break
        except (
            OSError, UnicodeError, json.JSONDecodeError, ValueError, Error,
        ) as exc:
            if candidate == path:
                primary_problem = exc
    if primary_problem is not None and not any(restored):
        emit(
            "diagnostic", stage,
            text=(
                "Ignored incompatible critic audit checkpoint: "
                f"{primary_problem}"
            ),
        )
    return restored

def save_critic_audit_checkpoint(
    reports, statement, solution, model, effort, instructions, directory=None, config=None, identity=None,
):
    """Atomically preserve every completed independent auditor result."""

    config = _parallel_definition(config)
    stage = config.get("stage", "critic")

    value = {
        "schemaVersion": CRITIC_AUDIT_CHECKPOINT_SCHEMA_VERSION,
        "assignmentSha256": critic_audit_assignment_sha256(
            statement, solution, model, effort, instructions, config=config, identity=identity,
        ),
        "model": chosen_model(model),
        "reasoningEffort": effective_effort(model, effort),
        "reports": reports,
    }
    _private_atomic_write(
        Path(directory or Path.cwd()) / config["checkpoint"]["file"],
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


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


def _grown_timeout(timeout, timeouts):
    """The call's timeout doubled once for each consecutive timed-out attempt (capped)."""

    if timeout is None:
        return None
    return min(float(timeout) * 2 ** min(int(timeouts), 20), max(float(timeout), MAX_ATTEMPT_TIMEOUT_SECONDS))


def structured(
    prompt, schema_value, stage, model=MODEL, effort=EFFORT,
    speed=DEFAULT_SPEED, summary=DEFAULT_REASONING_SUMMARY,
    timeout=None, attempts=None,
    request_label=None, activity_label=None, features=(), input_files=None,
    validate=None, options=None, cancel=None,
):
    """Run one read-only structured model call, retrying every failure a retry can fix until it succeeds.

    This is the one retry loop of every structured role. A failed attempt
    (provider or transport error, CLI crash, non-zero exit or missing CLI, a
    timed-out attempt, an empty, unparsable or schema-invalid answer, an
    answer that ``validate`` rejects with InvalidAnswer, or any answer given
    after some of the call's subagents failed) is retried after
    MODEL_RETRY_SECONDS, with no attempt limit (``attempts`` caps it only for
    direct callers that ask). Configuration errors, workflow-rule errors,
    safety refusals and harness errors are raised at once (see
    retryable_failure). A retry continues the call's conversation with a
    short note, or re-runs the exact saved input when it cannot (see
    StructuredConversation); after consecutive timeouts each attempt may run
    twice as long as the one before. With workflow ``options`` no attempt
    starts after the workflow deadline, the waits do not consume the time
    budget, and the pause file is honoured; Stop interrupts a wait at once,
    and ``cancel`` ends one for a sibling parallel request. The CLI sessions
    the call persisted are deleted when it ends.
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
    if input_files is not None:
        prompt += (
            "\n\nREAD-ONLY PROOF FILES:\n"
            "Your working directory contains proof.md (the exact candidate), "
            "statement.md (the exact task), and manifest.json (content hashes and provenance). "
            "Read both files completely; use successive ranges if tool output is truncated. "
            "Give independent subagents these same files and working directory. "
            "Review only this snapshot. Do not resolve links into the author's workspace. "
            "Return the complete proof text in solution, never a file pointer."
        )
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
    logged = {}  # request text -> the attempt that logged it in full
    failure, attempt, timeouts, failing_since = None, 0, 0, None

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
        )
        logged.setdefault(text, attempt)

    def attempt_once(text):
        request(text)
        conversation.start_attempt()
        return run_structured_attempt(
            text, schema_value, stage, model, effort, speed, summary,
            timeout=attempt_timeout, activity_label=activity, features=features,
            **({"input_files": input_files} if input_files is not None else {}),
            conversation=conversation,
        )

    def run(text):
        nonlocal attempt
        try:
            return attempt_once(text)
        except ConversationUnavailable as exc:
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
                attempt_timeout = _attempt_timeout(_grown_timeout(timeout, timeouts), options)
                raw = ""
                try:
                    raw = run(conversation.request(prompt, schema_value, failure))
                    if conversation.subagent_failures:
                        # An answer after failed subagent calls rests on an
                        # incomplete review: never use it, run them again.
                        raise SubagentFailure(conversation.subagent_failures)
                    return _accepted_answer(raw, schema_value, validate), raw
                except Exception as exc:
                    if not retryable_failure(exc):
                        raise  # A retry cannot fix it (see retryable_failure).
                    failure = exc
                    if isinstance(exc, InvalidAnswer):
                        exc.raw = raw
                    conversation.finish_attempt(exc)
                    timeouts = timeouts + 1 if isinstance(exc, StructuredAttemptTimeout) else 0
                    failing_since = failing_since or _wall_clock()
                    if isinstance(exc, InvalidAnswer):
                        emit("diagnostic", stage, rawResponse=(raw or "")[:STRUCTURED_RETRY_OUTPUT_CHARS],
                             attempt=attempt,
                             text=(f"The answer to {_call_name(stage)} (attempt {attempt}) could not be accepted: "
                                   f"{exc.problem}." + (f" Rule: {exc.rule}." if exc.rule else "")
                                   + (f" {_clipped(redact(exc.detail), 1600)}" if exc.detail else "")))
                    elif isinstance(exc, SubagentFailure):
                        messages = "; ".join(dict.fromkeys(
                            _failure_reason(redact(message), 400) for message in exc.failures.values()
                            if str(message or "").strip()))
                        emit("diagnostic", stage, rawResponse=(raw or "")[:STRUCTURED_RETRY_OUTPUT_CHARS],
                             attempt=attempt,
                             text=(f"The answer to {_call_name(stage)} (attempt {attempt}) was set aside because "
                                   f"{exc.plain}; the retry runs the failed subagents again."
                                   + (f" Subagent errors: {messages}" if messages else "")))
                    else:
                        emit("diagnostic", stage, attempt=attempt, text=(
                            f"{_call_name(stage)[0].upper()}{_call_name(stage)[1:]} (attempt {attempt}) failed: "
                            f"{_clipped(redact(str(exc) or type(exc).__name__), 2000)}"))
                    if attempts is not None and attempt >= attempts:
                        raise
                wait_before_retry(options, stage, attempt + 1, failure, cancel=cancel, activity=activity,
                                  since=failing_since, timeouts=timeouts,
                                  next_timeout=_grown_timeout(timeout, timeouts) if timeouts else None)
        finally:
            # The persisted CLI sessions served only this call.
            conversation.discard()


class RPC:
    """Send the few JSON-RPC messages needed by Goal mode."""

    def __init__(self, process, record=None):
        self.process, self.record = process, record
        self.number, self.waiting = 0, deque()
        self.write_lock = threading.Lock()
        self.stderr_tail = deque(maxlen=12)
        self.stderr_reader = None
        if getattr(process, "stderr", None) is not None:
            def drain_errors():
                for line in process.stderr:
                    # Startup diagnostics must not publish environment credentials
                    # or an API model's key.
                    value = redact(line.strip())
                    if value:
                        self.stderr_tail.append(value[:800])
            self.stderr_reader = threading.Thread(target=drain_errors, daemon=True)
            self.stderr_reader.start()


    def send(self, message):
        with self.write_lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def wire(self):
        """Read one new message from Codex."""

        line = self.process.stdout.readline()
        if not line:
            if self.stderr_reader:
                self.stderr_reader.join(timeout=0.2)
            detail = "\n".join(self.stderr_tail)
            raise Error("Codex app-server stopped unexpectedly." + ("\n" + detail if detail else ""))
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise Error(f"Codex app-server sent a malformed message: {_clipped(redact(line.strip()), 300)}") from exc
        if not isinstance(message, dict):
            raise Error("Codex app-server sent a malformed message.")
        if self.record:
            self.record(message)
        # This is a runtime warning, not model-authored text. Continuing after
        # it can strand goal completion and repeatedly spend requests while
        # every functions.exec call fails, including historical retrieval.
        if (message.get("method") == "warning"
                and "code mode is unavailable" in str((message.get("params") or {}).get("message", "")).lower()):
            raise SafetyStop("Required model tool runtime is unavailable: " + message["params"]["message"])
        # This small noninteractive client cannot answer server-side questions.
        if "id" in message and "method" in message:
            raise SafetyStop(f"Interactive Codex request is unsupported: {message['method']}.")
        return message

    def read(self):
        return self.waiting.popleft() if self.waiting else self.wire()

    def request(self, method, params):
        """Send one numbered request and return its identifier."""

        with self.write_lock:
            self.number += 1
            request = self.number
            self.process.stdin.write(json.dumps({
                "id": request, "method": method, "params": params,
            }) + "\n")
            self.process.stdin.flush()
            return request

    def call(self, method, params):
        request = self.request(method, params)
        while True:
            message = self.wire()
            if message.get("id") == request and "method" not in message:
                if "error" in message:
                    raise Error(f"{method} failed: {message['error']}")
                return message.get("result", {})
            self.waiting.append(message)


def goal_session(runtime, prompt, *, prompts, settings, options, features=(),
                 node_name="author", stages=None, initial_instruction=""):
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
    thread. Failures a retry cannot fix carry no ``retry``: a thread Codex
    no longer has or a missing thread id (session_unavailable), and safety
    refusals (safety_stop), which the controller never resumes by itself.
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
    thread = None
    rpc = process = None
    stop = threading.Event()
    expired = threading.Event()
    pausing = threading.Event()
    pause_file = options.get("goal_pause_file")
    # "turn_id" is the latest turn's id and "instruction_turn" the turn that
    # carried our latest instruction; "output" says whether the model produced
    # anything in the latest turn and "steered" whether a live instruction or
    # re-anchor joined it. Only an empty, unsteered turn of our own instruction
    # is ever reverted.
    state = {"turn": None, "active": True, "steer": options.get("author_steer_delivered"),
             "instruction": None, "delivered": False, "turn_id": None, "instruction_turn": None,
             "output": False, "steered": False, "rewound": False, "delivered_once": False}
    attempt = continued.get("attempt") if isinstance(continued.get("attempt"), int) else None
    pending_steers = {}
    pending_compaction = continued.get("pending_compaction") or None
    seen_compactions = set()
    goal = {}
    pause_reason = ""
    last_usage = {}
    # Failures that a retry of the same resume cannot fix.
    final = {"reason": None}

    def is_root(message):
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

    def record(message):
        event = runtime.public_event(message)
        if event is not None:
            emit("codex_event", event=event, root=is_root(event))

    def steer(instruction, identity, compaction=False):
        request = rpc.request("turn/steer", {
            "threadId": thread, "expectedTurnId": state["turn"],
            "input": [{"type": "text", "text": instruction}],
        })
        pending_steers[request] = (identity, instruction, compaction)
        if compaction:
            state["steered"] = True
        # The controller recognizes a restored instruction by its command id.
        emit("request", label="Author context re-anchor after compaction" if compaction
             else "Live author instruction sent", text=instruction, threadId=thread,
             **({} if compaction else {"steerId": identity}))

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
                    except (runtime.Error, OSError) as exc:
                        emit("diagnostic", text=f"Could not interrupt the author: {exc}")
                if time.monotonic() - pause_started >= getattr(runtime, "INTERRUPT_GRACE_SECONDS", 5):
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
                except (runtime.Error, OSError):
                    pass
                if not stop.wait(getattr(runtime, "INTERRUPT_GRACE_SECONDS", 5)):
                    runtime.stop_process(process)
                return
            if state["active"] and state["turn"]:
                command = runtime.pending_author_steer(options.get("author_steer_file"), state["steer"])
                if command and not any(item[0] == command[0] for item in list(pending_steers.values())):
                    try:
                        steer(command[1], command[0])
                    except (runtime.Error, OSError) as exc:
                        emit("diagnostic", text=f"Could not send live instruction: {exc}")

    def start_turn(instruction):
        # A retry re-sends this instruction unless the thread received it.
        state.update(instruction=instruction, delivered=False, instruction_turn=None, output=False, steered=False)
        if policy:
            retrieval_runtime.verify_rpc(rpc, policy, retrieval_role, child_environment, allow_runtime_overrides=True)
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
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        if not pausing.is_set() and not (pause_file and Path(pause_file).is_file()):
            rpc.call("thread/goal/set", {"threadId": thread, "status": "active"})

    def collect(item, answers):
        if (item.get("type") in {"agentMessage", "agent_message"}
                and item.get("phase") in {None, "final_answer"}
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
        child_environment = retrieval_environment(policy, retrieval_role, model, inherited=runtime.environment(model))
        api_model = (child_environment is not None and hasattr(runtime, "api_codex_home")
                     and runtime.model_provider(model) == getattr(runtime, "API_PROVIDER", None))
        if api_model:
            # An API-model thread lives in the private home of its workspace,
            # so a later run can resume it (never the user's CODEX_HOME).
            child_environment["CODEX_HOME"] = str(runtime.api_codex_home(directory))
        process = subprocess.Popen([
            retrieval_runtime.codex_executable(policy) if policy
            else runtime.api_codex_executable(child_environment) if api_model else runtime.codex(),
            *(["--strict-config"] if policy else []), "app-server", "--enable", "goals",
            *retrieval_runtime.native_search_arguments(directory, policy=policy),
            *([] if "multi_agent" in features else ["--disable", "multi_agent"]),
            *runtime.provider_arguments(model), *runtime.speed_arguments(settings["speed"], model),
            *runtime.context_cache_arguments(),
            *[argument for feature in features for argument in ("--enable", feature)],
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(directory),
            env=child_environment)
        rpc = runtime.RPC(process, record)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        initialized = rpc.call("initialize", {
            "clientInfo": {"name": "tcs_prover", "title": "TCS Prover", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        if api_model and Path(str((initialized or {}).get("codexHome") or "")).resolve() != Path(
                child_environment["CODEX_HOME"]).resolve():
            raise SafetyStop("Codex did not use the private home of this API model; refusing to continue.")
        rpc.send({"method": "initialized", "params": {}})
        if policy:
            retrieval_runtime.verify_rpc(rpc, policy, retrieval_role, child_environment, allow_runtime_overrides=True)
        thread_options = {
            "model": getattr(runtime, "wire_model", lambda value: value)(model),
            "cwd": str(directory), "runtimeWorkspaceRoots": [str(directory)],
            "sandbox": "workspace-write", "approvalPolicy": "never",
            "config": {"model_reasoning_effort": settings["effort"], "model_reasoning_summary": summary,
                **retrieval_runtime.native_search_configuration(directory, policy=policy),
                "features": {"goals": True, "multi_agent": False, "fast_mode": settings["speed"] == "fast",
                             **{feature: True for feature in features}}},
        }
        if hasattr(runtime, "model_provider"):
            thread_options["modelProvider"] = runtime.model_provider(model)
        if policy:
            thread_options.pop("sandbox")
            thread_options["permissions"] = retrieval_runtime.PROFILE
            locked = retrieval_runtime.configuration(policy, retrieval_role)
            locked_features = locked.pop("features")
            thread_options["config"].update(locked)
            thread_options["config"]["features"].update(locked_features)
        else:
            # The workspace-write sandbox, minus TCS Prover's private state,
            # selected exactly as the historical profile is.
            permissions = getattr(runtime, "private_permissions", None)
            private = permissions(":workspace", model) if permissions else None
            if private is not None:
                # A profile ignores [sandbox_workspace_write] (network access,
                # extra writable roots, /tmp exclusions), which the legacy
                # sandbox applied: carry the effective settings over.
                try:
                    effective = rpc.call("config/read", {"cwd": str(directory)})
                except runtime.Error as exc:
                    effective = {}
                    emit("diagnostic", text=f"Could not read Codex's [sandbox_workspace_write] settings; "
                                            f"the author sandbox runs without them: {exc}")
                effective = effective.get("config") if isinstance(effective, dict) else None
                private = permissions(":workspace", model, effective.get("sandbox_workspace_write")
                                      if isinstance(effective, dict) else None)
                thread_options.pop("sandbox")
                thread_options["permissions"] = PRIVATE_PERMISSIONS
                thread_options["config"].update(default_permissions=PRIVATE_PERMISSIONS,
                                                permissions={PRIVATE_PERMISSIONS: private})
        resumed = False
        if options.get("goal_thread_id"):
            try:
                result = rpc.call("thread/resume", {**thread_options, "threadId": options["goal_thread_id"],
                                                    "excludeTurns": True})
                resumed = True
            except runtime.Error as exc:
                gone = any(word in str(exc).lower() for word in MISSING_THREAD_MARKERS)
                if options.get("goal_require_resume"):
                    if gone:
                        # Retrying the same resume cannot bring the thread back:
                        # the job waits for the user's choice instead.
                        final["reason"] = "thread unavailable"
                    raise runtime.Error(f"Could not resume the saved Codex conversation; no replacement was started. {exc}"
                                        + (UNAVAILABLE_THREAD_HINT if gone else "")) from exc
                if not gone:
                    raise
                emit("diagnostic", text=f"Saved thread unavailable; starting a new session with the supplied resume instructions: {exc}")
        if not resumed:
            result = rpc.call("thread/start", {**thread_options, "ephemeral": False})
        thread = result["thread"]["id"]
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
                emit("diagnostic", text=f"Could not remove the failed turn ({exc}); sending its instruction again.",
                     threadId=thread)
        saved_goal = (rpc.call("thread/goal/get", {"threadId": thread}).get("goal") or {}) if resumed else {}
        goal = {"threadId": thread, "objective": saved_goal.get("objective") or render("goal")}
        emit("status", label="Goal resumed" if resumed else "Goal started", threadId=thread,
             text=f"Thread {thread}")
        if continued.get("instruction"):
            # A retry: the interrupted turn's instruction, or the retry note.
            instruction = continued["instruction"]
        else:
            instruction = render("resume") if options.get("goal_resume") or options.get("goal_thread_id") else prompt
            if initial_instruction:
                instruction += "\n\n" + initial_instruction
        start_turn(instruction)
        status, running, answers = None, True, []
        while True:
            if expired.is_set() or runtime.workflow_remaining(options) <= 0:
                raise runtime.Error("Workflow time limit reached.")
            message = rpc.read()
            params = message.get("params", {})
            if not is_root(message):
                continue
            request = message.get("id")
            if request in pending_steers and "method" not in message:
                identity, instruction, compaction = pending_steers.pop(request)
                if "error" in message:
                    if compaction:
                        pending_compaction = instruction
                    emit("diagnostic", text=f"Instruction raced with turn completion: {message['error']}")
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
                state["turn"] = turn.get("id")
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
                    raise runtime.Error("Author paused at the current turn boundary.")
                if turn.get("status") in {"failed", "interrupted"}:
                    raise runtime.Error(f"Author turn {turn['status']}: {_provider_message(turn.get('error')) or 'no complete result'}")
            elif method == "error" and not params.get("willRetry", False):
                raise runtime.Error(_provider_message(params.get("error")) or str(params))
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
                values.update(rejection)
                values["revision_number"] += 1
                stage = stages["resume"]
                start_turn(render("repair"))
                status, running, answers = None, True, []
            elif not running and status in {"blocked", "complete"}:
                start_turn(render("continuation"))
                status, running, answers = None, True, []
    except Exception as exc:
        if pausing.is_set() or (pause_file and Path(pause_file).is_file()):
            pausing.set()
            emit("status", label="Author paused", text=str(exc), threadId=thread)
        else:
            pausing.set()
            limited = expired.is_set() or runtime.workflow_remaining(options) <= 0
            safety = isinstance(exc, (SafetyStop, retrieval_runtime.RetrievalPolicyError))
            pause_reason = "Workflow time limit reached." if expired.is_set() else (str(exc) or type(exc).__name__)
            if safety and not limited:
                final["safety"] = True
                pause_reason = f"Stopped for safety: {pause_reason}{SAFETY_STOP_HINT}"
            emit("diagnostic", text=pause_reason, threadId=thread, **({"attempt": attempt} if attempt else {}))
            if limited or final["reason"] or safety or not retryable_failure(exc):
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
                                  "steer": state["steer"], "pending_compaction": pending_compaction}
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
    if pausing.is_set():
        raise WorkflowPaused(pause_reason or "Workflow paused.", reason=pause_reason, thread_id=thread,
                             retry=final.get("retry"), session_unavailable=bool(final["reason"]),
                             safety_stop=bool(final.get("safety")))


def _provider_message(error):
    """The readable message of a provider error that Codex reports as text or as an object."""

    if isinstance(error, dict):
        message = error.get("message")
        text = message.strip() if isinstance(message, str) and message.strip() else json.dumps(error, ensure_ascii=False)
        details = error.get("additionalDetails") or error.get("additional_details")
        if isinstance(details, str) and details.strip() and details.strip() not in text:
            text += f" ({details.strip()})"
        return text
    return str(error).strip() if error else ""


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
        temporary.replace(path)
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


_EXPRESSION_FUNCTIONS = {
    "len": len, "str": str, "bool": bool, "int": int, "float": float,
    "all": all, "any": any, "strip": _strip, "text": _text,
    "is_string": lambda value: isinstance(value, str),
    "is_bool": lambda value: isinstance(value, bool),
    "is_list": lambda value: isinstance(value, list),
    "is_dict": lambda value: isinstance(value, dict),
    "get": _get, "normalized_text": normalized_text, "json": lambda value: json.dumps(value, ensure_ascii=False, indent=2),
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


def _requirement_phrase(node, schema):
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


def _check_parallel(config, prompts):
    allowed = {"run", "items", "prompt", "instructions", "inputs", "schema", "features", "output", "item_field", "checkpoint", "attempts", "provider_options", "role", "stage", "model", "effort"}
    if not isinstance(config, dict) or config.get("run") != "structured" or set(config) - allowed or not isinstance(config.get("items"), list) or not config["items"]:
        raise ValueError("Parallel requests need a nonempty items list and request definition.")
    if not isinstance(config.get("schema"), dict):
        raise ValueError("Parallel requests need response or schema.")
    try:
        validator_for(config["schema"]).check_schema(config["schema"])
        json.dumps(config["items"])
    except (TypeError, ValueError, SchemaError) as exc:
        raise ValueError("Invalid parallel request schema or items.") from exc
    if not isinstance(config.get("prompt"), str) or config["prompt"] not in prompts:
        raise ValueError("Parallel requests need a named prompt.")
    instructions = config.get("instructions", [])
    if not isinstance(instructions, list) or not all(isinstance(name, str) and name in prompts for name in instructions):
        raise ValueError("Parallel request instructions must name existing prompts.")
    inputs = config.get("inputs", {})
    if not isinstance(inputs, dict) or not all(isinstance(key, str) for key in inputs):
        raise ValueError("Parallel inputs must be a named mapping.")
    for expression in inputs.values():
        check_expression(expression)
    for key in ("output", "item_field", "role", "stage"):
        if key in config and (not isinstance(config[key], str) or not config[key]):
            raise ValueError(f"Parallel {key} must be a nonempty string.")
    if not isinstance(config.get("features", []), list) or not all(isinstance(feature, str) for feature in config.get("features", [])):
        raise ValueError("Parallel features must be strings.")
    if "model" in config:
        chosen_model(config["model"])
    if "effort" in config:
        chosen_effort(config["effort"])
    _check_request_options(config)
    if "checkpoint" in config:
        checkpoint = config["checkpoint"]
        if not isinstance(checkpoint, dict) or not {"file", "identity"} <= checkpoint.keys() or set(checkpoint) - {"file", "disabled", "identity", "item_key"}:
            raise ValueError("A parallel checkpoint needs file and identity.")
        for key in ("file", "disabled", "item_key"):
            if key in checkpoint and (not isinstance(checkpoint[key], str) or not checkpoint[key]):
                raise ValueError("Checkpoint names must be nonempty strings.")
        if not isinstance(checkpoint["identity"], dict) or not all(isinstance(key, str) for key in checkpoint["identity"]):
            raise ValueError("Checkpoint identity must be a named mapping.")
        for expression in checkpoint["identity"].values():
            check_expression(expression)


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

    if "parallel" in node:
        parallel = node["parallel"]
        if not isinstance(parallel, dict):
            raise ValueError("Parallel requests need a mapping.")
        parallel.setdefault("run", "structured")
        _expand_node(parallel)


def load_workflow(path):
    """Read a data-only graph; node names and outcomes carry no built-in meaning."""

    try:
        workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot read workflow {path}: {exc}") from exc
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
    shared = {"run", "role", "stage", "model", "effort", "prompt", "outcome", "next", "before", "after"}
    specific = {
        "structured": {"instructions", "inputs", "schema", "features", "require", "error", "parallel", "attempts", "provider_options", "request_label", "activity_label", "normalize", "proof_handoff"},
        "goal": {"task", "marker", "lifecycle", "resume", "stages", "recovery", "features"},
        "command": {"command", "require", "error"},
    }
    for name, node in nodes.items():
        if not isinstance(node, dict) or not {"run", "next"} <= node.keys():
            raise ValueError(f"Node {name} needs run and next.")
        _expand_node(node)
        kind = node["run"]
        if not isinstance(kind, str) or kind not in specific or set(node) - (shared | specific[kind]):
            raise ValueError(f"Unsupported operation or fields in node {name}.")
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
            if "parallel" in node:
                _check_parallel(node["parallel"], prompts)
            if not isinstance(node.get("schema"), dict):
                raise ValueError(f"Structured node {name} needs a response schema.")
            try:
                json.dumps(node["schema"])
                validator_for(node["schema"]).check_schema(node["schema"])
            except (TypeError, ValueError, SchemaError) as exc:
                raise ValueError(f"Invalid JSON schema in node {name}.") from exc
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
            if not isinstance(lifecycle, dict) or not required <= set(lifecycle) or set(lifecycle) - required - {"pause"} or not all(
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
                    if set(branch) - {"repeat", "option", "then", "after"} or "then" not in branch:
                        raise ValueError(f"Invalid repeat branch in node {name}.")
                    if type(branch["repeat"]) is not int or branch["repeat"] < 1:
                        raise ValueError(f"Invalid repeat limit in node {name}.")
                    if "option" in branch and not isinstance(branch["option"], str):
                        raise ValueError(f"Invalid repeat option in node {name}.")
                    target = branch["then"]
                else:
                    if set(branch) - {"to", "after"} or "to" not in branch:
                        raise ValueError(f"Invalid transition in node {name}.")
                    target = branch["to"]
                _check_actions(branch.get("after", []))
            if not isinstance(target, str) or target not in targets:
                raise ValueError(f"Unknown transition target {target!r} in node {name}.")
    return workflow


def builtin_workflow(name):
    """Read current UI/convenience defaults without caching editable YAML."""

    return load_workflow(WORKFLOWS / f"{name}.yaml")


def __getattr__(name):
    # These public defaults are used by the UI and existing Python callers.
    # The generic execution path never needs to open either bundled YAML file.
    if name == "DEFAULT_CRITIC_ROUNDS":
        return builtin_workflow("author_critic")["nodes"]["critic"]["next"]["fixed"]["repeat"]
    if name == "FINAL_PROMPT":
        return builtin_workflow("clean_up")["prompts"]["final"]
    if name in {"CRITIC_AUDIT_FOCI", "CRITIC_CHECK_SCHEMA"}:
        config = _parallel_definition()
        return tuple(config["items"]) if name == "CRITIC_AUDIT_FOCI" else config["schema"]
    raise AttributeError(name)


def _settings(node, options):
    role = node.get("role", "")
    return {
        "model": chosen_model(options.get(f"{role}_model") or node.get("model") or options.get("model", MODEL)),
        "effort": chosen_effort(options.get(f"{role}_effort") or node.get("effort") or options.get("effort", EFFORT)),
        "speed": chosen_speed(options.get("speed", DEFAULT_SPEED)),
        "summary": chosen_reasoning_summary(options.get("summary", DEFAULT_REASONING_SUMMARY)),
    }


def prepare(workflow, options):
    """Resolve prompt overrides and validate every node before starting any model."""

    prompts = dict(workflow["prompts"])
    # Built-in author prompts have a simple variant for every lifecycle event.
    # Other workflows without these variants retain their original behavior.
    if options.get("file_management") is False:
        prompts.update({name: prompts[f"{name}_simple"] for name in tuple(prompts)
                        if f"{name}_simple" in prompts})
    prompts.update(options.get("prompts", {}))
    if not all(isinstance(value, str) and value.strip() for value in prompts.values()):
        raise ValueError("Prompt overrides must be nonempty strings.")
    for node in workflow["nodes"].values():
        if node["run"] == "command":
            continue
        settings = _settings(node, options)
        require_model_credentials(settings["model"])
        if node["run"] == "structured":
            if "parallel" in node:
                config = {key: node[key] for key in ("role", "stage", "model", "effort") if key in node}
                config.update(node["parallel"])
                require_model_credentials(_settings(config, options)["model"])
                _template_parts(prompts[config["prompt"]], set(config.get("inputs", {})) | {"instructions"})
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
            for reference in lifecycle.values():
                _template_parts(prompts[reference], fields)
    return prompts


def _parallel_definition(config=None):
    return builtin_workflow("author_critic")["nodes"]["critic"]["parallel"] if config is None else config


def _instructions(node, prompts):
    instructions = ""
    for name in node.get("instructions", []):
        part = text(prompts[name])
        if part not in instructions:
            instructions = instructions + "\n\n" + part if instructions else part
    return instructions


def _request_prompt(node, prompts, context):
    values = {key: evaluate(expression, context) for key, expression in node.get("inputs", {}).items()}
    values["instructions"] = _instructions(node, prompts)
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
    return settings


def workflow_remaining(options):
    """One elapsed-time budget across research, audits, and final editing.

    Waits before a model-call retry are not elapsed work (see RetryClock).
    """
    started = options.setdefault("_workflow_started", _now())
    hours = controlled_author_hours(options.get("author_limit_file"),
                                   options.get("thinking_hours", DEFAULT_AUTHOR_HOURS))
    if hours == 0:
        return math.inf
    return max(0.0, hours * 3600 - options.get("elapsed_seconds", 0)
               - (_now() - started - retry_clock(options).excluded()))


def _bounded_request_settings(settings, options):
    """Settings of one workflow model call, bounded by the shared deadline.

    No attempt starts after the deadline and none outlives it: structured()
    recomputes the remaining budget before each attempt. There is no internal
    attempt budget (a node's legacy `attempts` is ignored); every failure is
    retried after MODEL_RETRY_SECONDS, and those waits do not consume the budget.
    """
    remaining = workflow_remaining(options)
    if remaining <= 0:
        raise Error("Workflow time limit reached; saved work remains resumable.")
    settings = {key: value for key, value in settings.items() if key != "attempts"}
    # Every attempt has a timeout, even with an unlimited budget, so a hung
    # request is noticed; consecutive timeouts double it (see structured()).
    timeout = settings.get("timeout") or STRUCTURED_TIMEOUT_SECONDS
    return {**settings, "timeout": timeout if math.isinf(remaining) else min(timeout, remaining),
            "options": options}


def _parallel_requests(config, prompts, state, options, visit, input_files=None):
    """Run independent structured requests, restoring exact-input checkpoints."""
    settings = _structured_options(config, options)
    model, effort = settings["model"], settings["effort"]
    stage = config.get("stage", "model")
    context = {"state": state, "visit": visit, "instructions": _instructions(config, prompts)}
    checkpoint = config.get("checkpoint")
    identity = {key: evaluate(value, context) for key, value in checkpoint["identity"].items()} if checkpoint else None
    reports = load_critic_audit_checkpoint("", "", model, effort, "", config=config, identity=identity) if checkpoint else [None] * len(config["items"])
    restored = sum(report is not None for report in reports)
    for index, report in enumerate(reports):
        if report is not None:
            emit("status", stage, label=f"Independent audit {index + 1} restored", text="Loaded this completed audit from the exact-proof checkpoint; no new provider request will be charged for it.", node=stage, auditIndex=index + 1, auditVerdict=report.get("verdict"), checkpoint=True)
    emit("status", stage, label="Three independent audits started" if len(reports) == 3 else "Independent requests started", text=f"The controller restored {restored} completed audits and is launching {len(reports) - restored} explicit parallel model requests.", node=stage, auditCount=len(reports), restoredAuditCount=restored, launchedAuditCount=len(reports)-restored, reasoningEffort=effort, timeoutSeconds=settings.get("timeout"))
    # Each request retries its own failures without a limit; a request that
    # ends the step (pause, time limit, Stop) cancels the waits of the others.
    cancel = threading.Event()

    def run_one(index, item):
        prompt = _request_prompt(config, prompts, {**context, "item": item})
        report, _ = structured(prompt, config["schema"], stage, features=config.get("features", []), request_label=f"Independent critic audit {index + 1}", activity_label=f"Independent audit {index + 1}", cancel=cancel, **({"input_files": input_files} if input_files is not None else {}), **_bounded_request_settings(settings, options))
        report = dict(report)
        if config.get("item_field"):
            report[config["item_field"]] = item
        return report
    failures = []
    missing = [(index, item) for index, item in enumerate(config["items"]) if reports[index] is None]
    with ThreadPoolExecutor(max_workers=max(1, len(missing))) as pool:
        futures = {pool.submit(run_one, index, item): index for index, item in missing}
        try:
            for future in as_completed(futures):
                index = futures[future]
                try:
                    reports[index] = future.result()
                    if checkpoint:
                        save_critic_audit_checkpoint(reports, "", "", model, effort, "", config=config, identity=identity)
                    emit("status", stage, label=f"Independent audit {index + 1} complete", text=reports[index].get("report", ""), node=stage, auditIndex=index + 1, auditVerdict=reports[index].get("verdict"))
                except Exception as exc:
                    cancel.set()
                    if not isinstance(exc, RetryCancelled):
                        failures.append((index, exc))
                        emit("diagnostic", stage, text=f"Independent audit {index + 1} failed: {exc}", node=stage, auditIndex=index + 1)
        finally:
            # Stop (KeyboardInterrupt) or any failure: no sibling keeps waiting to retry.
            cancel.set()
    if failures:
        failures.sort(key=lambda failure: failure[0])
        for _, exc in failures:
            if isinstance(exc, WorkflowPaused):
                raise exc
        detail = "; ".join(f"audit {index + 1}: {exc}" for index, exc in failures)
        raise Error("The controller could not complete all independent audits. " + detail)
    return reports


def _accept_node_answer(node, context, input_files, report, raw):
    """Apply a node's normalize, schema, proof-pointer and require checks to one answer.

    Runs inside structured()'s retry loop, so an unacceptable answer is
    retried with a plain explanation (InvalidAnswer) instead of failing the run.
    """
    report = dict(report)
    parallel = node.get("parallel")
    if parallel and parallel.get("output"):
        report[parallel["output"]] = context["parallel"]
    for key, expression in node.get("normalize", {}).items():
        try:
            report[key] = evaluate(expression, {**context, "result": report, "raw": raw})
        except ValueError as exc:
            # The rule cannot handle a schema-valid answer: a workflow problem,
            # which no retry of the model call can fix.
            raise WorkflowDefinitionError(str(exc)) from exc
    try:
        validate_json_schema(report, node["schema"])
    except Error as exc:
        if node.get("normalize") or (parallel and parallel.get("output")):
            # structured() already checked the answer against this schema:
            # the node's own normalize or parallel output broke it.
            raise WorkflowDefinitionError(str(exc)) from exc
        raise InvalidAnswer(_schema_problem(report, node["schema"]) or str(exc), detail=str(exc)) from exc
    # A returned pointer would be resolved against the author's stale file or
    # accepted literally; stop before any action replaces the saved candidate.
    handoff = node.get("proof_handoff")
    returned = report.get(handoff["candidate"]) if handoff and input_files is not None else None
    captured = dict(input_files)["proof.md"] if isinstance(returned, str) else ""
    if isinstance(returned, str) and not normalized_text(returned):
        # No proof at all (for example a critic whose subagents all failed).
        raise InvalidAnswer(f"the {handoff['candidate']} field must not be empty",
                            message="The critic returned no proof text; the captured candidate was kept.")
    if isinstance(returned, str) and normalized_text(returned) != normalized_text(captured):
        # Marker lines kept verbatim from the captured proof are text of an
        # edited proof; only what the critic wrote itself can be a pointer.
        marker = re.compile(r"^[ \t]*`?PROOF_FILE[ \t]*:")
        inherited = {line.strip() for line in captured.splitlines() if marker.match(line)}
        remainder = "\n".join(line for line in returned.splitlines() if line.strip() not in inherited)
        try:
            pointer = (not normalized_text(remainder)
                       or proof_handoff.extract_proof_reference(remainder) is not None)
        except proof_handoff.ProofHandoffError:
            pointer = True
        if pointer:
            pointer = ("The critic returned a file pointer instead of the complete proof text; "
                       "the captured candidate was kept.")
            raise InvalidAnswer(
                f"the {handoff['candidate']} field must contain the complete proof text, not a file pointer",
                detail=pointer, message=pointer)
    for condition in node.get("require", []):
        try:
            satisfied = evaluate(condition, {**context, "result": report, "raw": raw})
        except ValueError as exc:
            if not isinstance(exc.__cause__, _EmptyText):
                # The rule cannot be evaluated on a schema-valid answer (for
                # example it names a field the schema lacks): a workflow
                # problem, which no retry of the model call can fix.
                raise WorkflowDefinitionError(node.get("error", str(exc))) from exc
            satisfied = False  # text() of an empty field: the answer does not meet the rule.
        if not satisfied:
            problem = describe_requirement(condition, node["schema"], report)
            raise InvalidAnswer(problem, rule=condition, detail=node.get("error", ""),
                                message=node.get("error") or f"Invalid model response: {problem}.")
    return report


def _prepare_node_files(node, state, visit, workspace=None, resolve_reference=True):
    """Resolve an explicitly submitted artifact before checkpoints and prompts.

    Only a node's first visit receives a submission; a repeated visit reviews
    the previous answer as text, never the author's possibly stale file.
    """
    handoff = node.get("proof_handoff")
    if handoff is None:
        return None
    candidate_key, statement_key = handoff["candidate"], handoff["statement"]
    candidate, statement = state.get(candidate_key), state.get(statement_key)
    if not isinstance(candidate, str) or not isinstance(statement, str) or not statement.strip():
        raise Error("Proof handoff needs a candidate and the exact problem statement.")
    try:
        snapshot = (proof_handoff.resolve_author_proof(candidate, workspace or Path.cwd())
                    if resolve_reference else None)
    except proof_handoff.ProofHandoffError as exc:
        raise Error(f"Cannot hand the submitted proof to the critic: {exc}") from exc
    if snapshot is not None:
        candidate = snapshot.text
    if not candidate.strip():
        raise Error("Cannot hand an empty proof to the critic.")
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


def _model_call(node, prompts, state, options, visit, input_files=None):
    """Run a configured request, optionally collecting independent inputs first."""
    context = {"state": state, "visit": visit}
    parallel = node.get("parallel")
    if parallel:
        config = {key: node[key] for key in ("role", "stage", "model", "effort") if key in node}
        config.update(parallel)
        context["parallel"] = options.get("parallel_results")
        if context["parallel"] is None:
            context["parallel"] = _parallel_requests(config, prompts, state, options, visit, input_files=input_files)
    prompt = _request_prompt(node, prompts, context)
    settings = _structured_options(node, options)
    settings.update({key: node[key] for key in ("request_label", "activity_label") if key in node})

    accepted = {}

    def accept(report, raw):
        accepted["report"] = _accept_node_answer(node, context, input_files, report, raw)
        return accepted["report"]

    # The node's checks run inside the shared retry loop: an unacceptable
    # answer is retried with a plain explanation, never failing the run.
    report, raw = structured(prompt, node["schema"], node.get("stage", "model"), features=node.get("features", []),
                             validate=accept, **({"input_files": input_files} if input_files is not None else {}),
                             **_bounded_request_settings(settings, options))
    if accepted.get("report") is not report:
        # A replacement structured() (such as a test double) skipped the checks.
        report = _accept_node_answer(node, context, input_files, report, raw)
    return report, raw


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


def _run_bounded(argv, *, timeout, **kwargs):
    """Run one command; on timeout or interruption, stop everything it started."""
    with subprocess.Popen(argv, **kwargs) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException as exc:
            _stop_command_tree(process)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired as late:
                output = late.stdout  # A detached process still holds the pipe.
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.stdout = output
            raise
        return subprocess.CompletedProcess(argv, process.returncode, output)


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


def _run_command(config, context):
    """Run a bounded argv command and check only its declared output files."""
    policy = retrieval_policy()
    argv = [render_template(value, context) for value in config["argv"]]
    directory = (Path.cwd() / render_template(config.get("cwd", "."), context)).resolve()
    if policy is not None:
        from retrieval import isolated_compiler
        try:
            return isolated_compiler.compile_command(policy, config, argv, directory)
        except isolated_compiler.CompilerIsolationError as exc:
            raise Error(str(exc)) from exc
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs = [directory / name for name in config.get("produces", [])]
    for path in outputs:
        path.unlink(missing_ok=True)
    engine = shutil.which(argv[0])
    missing = [name for name in config.get("requires", []) if shutil.which(name) is None]
    code, output = None, ""
    if engine is None or missing:
        status, diagnostic = "unavailable", f"Command unavailable: {', '.join(([argv[0]] if engine is None else []) + missing)}."
    else:
        environment = {**os.environ, **{key: render_template(value, context) for key, value in config.get("env", {}).items()}}
        # Every attempt compiles from a clean slate, as the isolated compiler
        # does in its fresh folder; sources may still use files of this folder.
        for path in _tex_auxiliary_files(argv, directory):
            path.unlink(missing_ok=True)
        try:
            result = _run_bounded([engine, *argv[1:]], cwd=directory, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", timeout=config["timeout"],
                **({"umask": 0o077} if os.name != "nt" else {}))
            code, output = result.returncode, result.stdout or ""
            produced = all(path.is_file() and path.stat().st_size > 0 for path in outputs)
            status = "pass" if code == 0 and produced else "fail"
            diagnostic = f"Command exited with code {code}."
            if code == 0 and not produced:
                diagnostic += " A declared output file is missing or empty."
        except subprocess.TimeoutExpired as exc:
            status, diagnostic = "fail", f"Command exceeded {config['timeout']:g} seconds."
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
        except OSError as exc:
            status, diagnostic = "unavailable", f"Command could not run: {exc}"
    if status != "pass":
        for path in outputs:
            path.unlink(missing_ok=True)
    if config.get("log"):
        _private_atomic_write(directory / config["log"], diagnostic + "\n\n" + output)
    return {"status": status, "diagnostic": diagnostic, "returncode": code,
            "output": output[-24000:]}


def _apply_actions(actions, context, stage, revision):
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
            _private_atomic_write(Path.cwd() / action["write"]["path"], evaluate(action["write"]["text"], context))
        if "command" in action:
            config = action["command"]
            state[config["result"]] = _run_command(config, context)
        if "emit" in action:
            fields = {}
            for key, value in action["emit"].items():
                fields[key] = (evaluate(value[1:], context) if value.startswith("=") else render_template(value, context)) if isinstance(value, str) else value
            emit(fields.pop("kind"), fields.pop("stage", stage), **fields)


def _complete_node(node, state, result, raw, visit, revision):
    context = {"state": state, "result": result, "raw": raw, "visit": visit}
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
    _apply_actions(node.get("after", []), context, node.get("stage", "model"), revision)
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
    any thread yet, the same first attempt runs again.
    """

    attempt = 1
    # The latest known way to continue; a session that fails early reports less.
    known = {"thread": options.get("goal_thread_id"), "stage": stage, "values": None, "steer": None,
             "pending_compaction": None, "anchor": None, "instruction": None, "rewind": None}
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
                             "attempt": attempt}
                if rewind.get("fresh"):
                    # The conversation's first turn produced nothing: a new
                    # conversation with the same first instruction loses nothing.
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


def _execute(workflow, state, options, prompts):
    nodes = workflow["nodes"]
    current, visits, revision = options.get("start_node", next(iter(nodes))), 0, 0
    if current not in nodes:
        raise ValueError(f"Unknown workflow entry node: {current!r}.")
    sessions, revisions, launchers = {}, {}, {}
    options.setdefault("_workflow_started", _now())
    retry_clock(options)
    try:
        while current != "end":
            node = nodes[current]
            if options.get("goal_pause_file") and Path(options["goal_pause_file"]).is_file():
                raise WorkflowPaused("Paused before the next workflow step.")
            if workflow_remaining(options) <= 0:
                raise Error("Workflow time limit reached.")
            visits += 1
            emit("status", node.get("stage", current), label=f"Workflow step: {current}",
                 node=current, visit=visits)
            input_files = _prepare_node_files(node, state, visits, options.get("goal_cwd"),
                                              resolve_reference=visits == 1)
            _apply_actions(node.get("before", []), {"state": state, "visit": visits}, node.get("stage", current), revision)
            if node["run"] == "structured":
                result, raw = _model_call(node, prompts, state, options, visits, input_files=input_files)
            elif node["run"] == "command":
                config = dict(node["command"])
                config["timeout"] = min(config["timeout"], workflow_remaining(options))
                result = _run_command(config, {"state": state, "visit": visits})
                state[config["result"]] = result
                raw = result["output"]
            else:
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
                    session_prompts = {key: prompts[ref] for key, ref in lifecycle.items()}
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
                    # Claude models (and Anthropic-format API models) run the
                    # same goal protocol through the Claude CLI.
                    session_function = (claude_cli.goal_session
                                        if providers.harness(settings["model"]) == providers.CLAUDE
                                        else goal_session)
                    launch = functools.partial(
                        session_function, sys.modules[__name__], prompt, prompts=session_prompts,
                        settings=settings, features=node.get("features", []),
                        stages=node.get("stages"), node_name=current,
                    )
                    launchers[current] = (launch, instruction)
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
            context = _complete_node(node, state, result, raw, visits, revision)
            branch = node["next"][context["outcome"]]
            target = branch
            if isinstance(branch, dict):
                if "repeat" in branch:
                    limit = options.get(branch.get("option"), branch["repeat"])
                    target = branch["then"] if visits >= limit else current
                    if visits >= limit:
                        context["limit"] = limit
                        _apply_actions(branch.get("after", []), context, node.get("stage", current), revision)
                else:
                    target = branch["to"]
                    _apply_actions(branch.get("after", []), context, node.get("stage", current), revision)
            if target != current:
                visits = 0
            current = target
        return state
    except Exception as exc:
        if not isinstance(exc, WorkflowPaused) and node["run"] != "goal":
            raise
        state["paused"] = True
        state.pop("failed", None)
        reason = exc.reason if isinstance(exc, WorkflowPaused) else (str(exc) or type(exc).__name__)
        thread_id = getattr(exc, "thread_id", None) or options.get("goal_thread_id")
        emit("workflow_paused", node.get("stage", current), node=current, state=state,
             reason=reason, threadId=thread_id,
             **({"sessionUnavailable": True} if getattr(exc, "session_unavailable", False) else {}),
             **({"safetyStop": True} if getattr(exc, "safety_stop", False) else {}))
        return state
    finally:
        for session in sessions.values():
            session.close()


def execute(path, state=None, *, options=None):
    """Execute any graph in the documented format; no node-name dispatch."""

    workflow = load_workflow(path)
    options = {} if options is None else options
    state = {} if state is None else state
    prompts = prepare(workflow, options)
    state.pop("paused", None)
    state.pop("failed", None)
    return _execute(workflow, state, options, prompts)


def execute_workflows(paths, state, options=None):
    """Validate once, then feed state through the graphs until one reports failure."""

    options = {} if options is None else dict(options)
    options.setdefault("_workflow_started", _now())
    # Every graph of the chain shares one budget, retry waits included.
    retry_clock(options)
    prepared = []
    for index, path in enumerate(paths):
        workflow = load_workflow(path)
        local_options = dict(options)
        if index:
            local_options.pop("start_node", None)
        elif "start_node" in local_options and local_options["start_node"] not in workflow["nodes"]:
            raise ValueError(f"Unknown workflow entry node: {local_options['start_node']!r}.")
        prepared.append((workflow, prepare(workflow, local_options), local_options))
    # A state saved by workflow_paused still carries its control flags; they
    # describe the earlier run and must not end this one after its first graph.
    state.pop("paused", None)
    state.pop("failed", None)
    for workflow, prompts, local_options in prepared:
        _execute(workflow, state, local_options, prompts)
        if state.get("failed") or state.get("paused"):
            break
    return state


def _builtin_node(workflow_name, node_name, state, options, visit=1, prompts=None):
    """Compatibility convenience calls share the same generic node interpreter."""

    workflow = builtin_workflow(workflow_name)
    resolved = {**workflow["prompts"], **(prompts or {}), **options.get("prompts", {})}
    node = workflow["nodes"][node_name]
    input_files = _prepare_node_files(node, state, visit, options.get("goal_cwd"))
    _apply_actions(node.get("before", []), {"state": state, "visit": visit}, node.get("stage", "model"), 0)
    result, raw = _model_call(node, resolved, state, options, visit, input_files=input_files)
    _complete_node(node, state, result, raw, visit, 0)
    return result


def criticize(statement, solution, round_number, model=CRITIC_MODEL, effort=EFFORT, instructions=None, speed=DEFAULT_SPEED, prompts=None, summary=DEFAULT_REASONING_SUMMARY):
    options = {"model": model, "effort": effort, "speed": speed, "summary": summary, "prompts": {"critic": instructions} if instructions is not None else {}}
    return _builtin_node("author_critic", "critic", {"statement": statement, "solution": solution}, options, round_number, prompts)


def main(argv=None):
    configure_standard_streams()
    # A runner killed before its cleanup may have left a key-bearing Claude
    # settings folder behind.
    claude_cli.sweep_stale_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=HelpFormatter)
    parser.add_argument("workflows", nargs="+", help="YAML files, executed in order")
    models = (f"{', '.join(OFFERED_MODELS)}, or an api:<id> model from Advanced settings → API models "
              "(the older GPT-5.6 ids are still accepted)")
    parser.add_argument("--model", type=model_argument, default=MODEL, metavar="MODEL",
                        help=f"fallback model: {models}")
    for role in ("author", "critic", "writer"):
        parser.add_argument(f"--{role}-model", type=model_argument, metavar="MODEL",
                            help=f"{role} model; same choices as --model")
        parser.add_argument(f"--{role}-effort", choices=EFFORTS)
    parser.add_argument("--reasoning-effort", dest="effort", choices=EFFORTS, default=EFFORT)
    parser.add_argument("--speed", choices=SPEEDS, default=DEFAULT_SPEED)
    parser.add_argument("--critic-rounds", type=critic_limit)
    parser.add_argument("--thinking-hours", type=author_hours, default=DEFAULT_AUTHOR_HOURS)
    parser.add_argument("--elapsed-seconds", type=prior_elapsed_seconds, default=0)
    parser.add_argument("--author-limit-file")
    parser.add_argument("--author-steer-file")
    parser.add_argument("--reasoning-summary", dest="summary", choices=REASONING_SUMMARIES, default=DEFAULT_REASONING_SUMMARY)
    parser.add_argument("--state-file", help="Read initial workflow state from a JSON object instead of stdin")
    parser.add_argument("--start-node", help="Enter the first workflow at this named node")
    parser.add_argument("--set", dest="settings", action="append", default=[], metavar="NAME=VALUE", help="override any named option; JSON values are accepted")
    for role in ("author", "critic", "final"):
        parser.add_argument(f"--{role}-prompt-file")
    try:
        args = parser.parse_args(argv)
        options = {key: value for key, value in vars(args).items() if value is not None}
        options["prompts"] = {}
        for role in ("author", "critic", "final"):
            path = options.pop(f"{role}_prompt_file", None)
            if path:
                options["prompts"][role] = prompt_file(path, None)
        for setting in options.pop("settings"):
            key, separator, value = setting.partition("=")
            if not separator or not key:
                raise Error("Use --set NAME=VALUE.")
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
            options[key] = value
        if args.state_file:
            state = json.loads(Path(args.state_file).read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise Error("Workflow state must be a JSON object.")
        else:
            source = text(sys.stdin.read())
            if "\0" in source:
                raise Error("Workflow input cannot contain NUL characters.")
            state = {"input": source, "statement": source, "source": source}
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


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # SIGTERM unwinds like Ctrl-C, so cleanup still removes private settings.
    signal.signal(signal.SIGTERM, _interrupt)
    raise SystemExit(main())
