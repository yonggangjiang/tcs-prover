#!/usr/bin/env python3
"""Execute declarative YAML workflows using persistent goals and structured model calls."""

import argparse
import ast
import functools
import hashlib
import json
import math
import os
import operator
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yaml
    from jsonschema.exceptions import SchemaError
    from jsonschema.validators import validator_for
except ImportError as exc:
    raise SystemExit("Install workflow support with: python3 -m pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parent
WORKFLOWS = ROOT / "workflows"
MARKER = "[STATEMENT]"
MODELS = ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "deepseek-v4-pro")
MODEL, EFFORT = "gpt-6-astra", "ultra"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
SPEEDS, DEFAULT_SPEED = ("standard", "fast"), "fast"
SERVICE_TIER = DEFAULT_SPEED
AUTHOR_MODEL = CRITIC_MODEL = WRITER_MODEL = MODEL
MAX_CRITIC_ROUNDS = 100
MAX_AUTHOR_HOURS = 168
DEFAULT_AUTHOR_HOURS = MAX_AUTHOR_HOURS
STRUCTURED_WORKSPACE = ROOT / ".codex-structured-workspace"
_GOAL_LIFECYCLE = (
    "goal", "initial", "memory", "anchor", "reanchor", "continuation",
    "compaction", "repair", "failure", "failure_input",
)
_GOAL_STAGES = {"initial": "solve", "resume": "repair", "failure": "failure"}


DEEPSEEK_MODEL_CATALOG = ROOT / "deepseek-models.json"

DEEPSEEK_MODEL = "deepseek-v4-pro"

REASONING_SUMMARIES = ("none", "concise", "detailed")

DEFAULT_REASONING_SUMMARY = "concise"

OPENAI_PROVIDER = "openai"

DEEPSEEK_PROVIDER = "deepseek"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"

DEEPSEEK_TOKEN_ENV = "TCS_PROVER_DEEPSEEK_TOKEN"

CUSTOM_PROVIDER_LOGIN_PLACEHOLDER = "tcs-prover-custom-provider"

STRUCTURED_MAX_ATTEMPTS = 2

STRUCTURED_RETRY_OUTPUT_CHARS = 12000

STRUCTURED_ATTEMPT_TIMEOUT_SECONDS = {
    "review": 300,
    "critic": 900,
    "final": 900,
}

STRUCTURED_HEARTBEAT_SECONDS = 30

CRITIC_AUDIT_TIMEOUT_SECONDS = 1800

CRITIC_COORDINATOR_TIMEOUT_SECONDS = 1800

CRITIC_AUDIT_CHECKPOINT_FILENAME = "critic-audits.json"

CRITIC_AUDIT_RECOVERY_DISABLED_FILENAME = "fresh-critic-audits"

CRITIC_AUDIT_CHECKPOINT_SCHEMA_VERSION = 1

SAVED_CANDIDATE_FILENAME = "saved-candidate.md"

FINAL_INPUT_FILENAME = "final-input.json"

AUTHOR_STEER_POLL_SECONDS = 0.25

AUTHOR_STEER_MAX_CHARS = 12000

EMIT_LOCK = threading.Lock()
REVIEW_MODEL = MODEL
REVIEW_MODELS = MODELS
REVIEW_EFFORT = EFFORT


class Error(RuntimeError):
    """Show a short, understandable failure."""


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
    """Require a positive author-time limit of at most one week."""

    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise Error("The total workflow time limit must be a number of hours.") from exc
    if not 0 < value <= MAX_AUTHOR_HOURS:
        raise Error(f"Choose more than 0 and at most {MAX_AUTHOR_HOURS} hours.")
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
    """Require one supported base model."""

    if value not in MODELS:
        raise Error("Choose Astra, Sol, Terra, Luna, or DeepSeek V4 Pro.")
    return value


def chosen_effort(value):
    """Require one supported Codex reasoning effort."""

    if value not in EFFORTS:
        raise Error("Choose a valid reasoning effort.")
    return value


def chosen_speed(value):
    """Require Standard or OpenAI's 1.5x Fast mode."""

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
        raise Error("Codex CLI is not installed or is not on PATH.")
    return path


def environment(model=None):
    """Use inherited provider credentials with quiet, predictable child logging."""

    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    normalized_deepseek_key = deepseek_key()
    env.pop(DEEPSEEK_KEY_ENV, None)
    env.pop(DEEPSEEK_TOKEN_ENV, None)
    # Do not leak credentials left behind by obsolete provider configurations.
    env.pop("OPENROUTER_API_KEY", None)
    env.pop("TCS_PROVER_OPENROUTER_TOKEN", None)
    provider = model_provider(model) if model is not None else OPENAI_PROVIDER
    if provider == DEEPSEEK_PROVIDER:
        if normalized_deepseek_key:
            # The custom provider reads this environment value and adds the
            # Bearer scheme. The credential never appears in process arguments.
            env[DEEPSEEK_TOKEN_ENV] = normalized_deepseek_key
            # Forced API login requires a nonempty OPENAI_API_KEY before it
            # initializes any custom provider. Use a public placeholder here;
            # provider authentication still comes exclusively from the token.
            env["OPENAI_API_KEY"] = CUSTOM_PROVIDER_LOGIN_PLACEHOLDER
    # Inherited debug logs could bypass the structured-event privacy filter.
    env["RUST_LOG"] = "error"
    env.pop("LOG_FORMAT", None)
    env.pop("RUST_BACKTRACE", None)
    return env


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
    """Write one machine-readable transcript record for the web UI."""

    record = json.dumps(
        {"kind": kind, "stage": stage, **fields}, ensure_ascii=False,
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
    """Return the Codex provider id for one supported model."""

    model = chosen_model(model)
    if model == DEEPSEEK_MODEL:
        return DEEPSEEK_PROVIDER
    return OPENAI_PROVIDER

def is_deepseek_model(model):
    """Return whether the official DeepSeek V4 Pro model is selected."""

    return chosen_model(model) == DEEPSEEK_MODEL

def provider_arguments(model):
    """Define an isolated custom provider without editing user configuration."""

    provider = model_provider(model)
    if provider == OPENAI_PROVIDER:
        return []
    return [
        # Keep ChatGPT login state from overriding custom-provider API auth.
        "-c", (
            "model_catalog_json="
            f"{json.dumps(str(DEEPSEEK_MODEL_CATALOG))}"
        ),
        "-c", 'preferred_auth_method="apikey"',
        "-c", 'forced_login_method="api"',
        "-c", f'model_provider="{provider}"',
        "-c", f'model_providers.{provider}.name="DeepSeek"',
        "-c", (
            f'model_providers.{provider}.base_url="{DEEPSEEK_BASE_URL}"'
        ),
        "-c", (
            f'model_providers.{provider}.env_key="{DEEPSEEK_TOKEN_ENV}"'
        ),
        "-c", f'model_providers.{provider}.wire_api="responses"',
        "-c", (
            f'model_providers.{provider}.supports_websockets=false'
        ),
    ]

def require_model_credentials(model):
    """Fail before launching Codex when a third-party credential is missing."""

    if model_provider(model) == DEEPSEEK_PROVIDER and not deepseek_key():
        raise Error(
            f"Set {DEEPSEEK_KEY_ENV} before using DeepSeek V4 Pro through "
            "the official API."
        )

def normalized_key(environment_name):
    """Return a normalized provider key without exposing it in diagnostics."""

    value = os.environ.get(environment_name, "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value

def deepseek_key():
    """Return the official DeepSeek API key, if configured."""

    return normalized_key(DEEPSEEK_KEY_ENV)

def verify_model_credentials(model):
    """Check locally that the selected model has its required credential."""

    require_model_credentials(model)

def effective_effort(model, effort):
    """Map the shared effort menu to capabilities exposed by each provider."""

    model = chosen_model(model)
    effort = chosen_effort(effort)
    if not is_deepseek_model(model):
        return effort
    # The official API/catalog names V4 Pro's maximum effort `max`.
    if effort in {"low", "medium", "high"}:
        return "high"
    return "max"

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
    """Fast service tier is OpenAI-specific; custom-provider calls stay standard."""

    speed = chosen_speed(speed)
    return speed if model_provider(model) == OPENAI_PROVIDER else "standard"

def structured_prompt_for_model(prompt, schema_value, model):
    """Expose the JSON contract when a model lacks schema enforcement."""

    if not is_deepseek_model(model):
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

    if is_deepseek_model(model):
        return []
    return ["--output-schema", str(schema_path)]

def structured_tool_arguments(stage, features=()):
    """Keep structured calls tool-free unless the YAML explicitly enables a feature."""
    return [
        "-c", 'web_search="disabled"',
        "-c", "tools.web_search=false",
        "-c", "tools.view_image=false",
        *[argument for feature in ("shell_tool", "multi_agent") if feature not in features for argument in ("--disable", feature)],
    ]


def structured_retry_prompt(prompt, raw, schema_value):
    """Request one clean recovery after an empty or invalid final response."""

    previous = raw.strip()
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
    return (
        f"{prompt}\n\nSTRUCTURED OUTPUT RECOVERY RETRY\n"
        "The previous attempt did not produce one valid final JSON object. "
        "Do not call tools, search the web, discuss the formatting failure, or "
        "return a placeholder. Re-evaluate the task as needed and return the "
        "complete substantive answer as exactly one JSON object matching this "
        f"schema:\n{schema}\n\nPREVIOUS INVALID OUTPUT:\n{previous}"
    )

def run_structured_attempt(
    prompt, schema_value, stage, model, effort, speed, summary,
    timeout=None, activity_label=None, features=(),
):
    """Run one structured Codex process and return its last-message text."""

    timeout = None if timeout is None else float(timeout)
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        workspace = structured_workspace()
        schema = folder / "schema.json"
        answer = folder / "answer.json"
        schema.write_text(json.dumps(schema_value), encoding="utf-8")
        schema_arguments = output_schema_arguments(model, schema)
        command = [
            codex(), "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            *provider_arguments(model),
            *speed_arguments(speed, model), *context_cache_arguments(),
            "-c", f'model_reasoning_summary="{summary}"',
            *structured_tool_arguments(stage, features),
            *[argument for feature in features for argument in ("--enable", feature)],
            "-C", str(workspace), "-s", "read-only", "-a", "never", "exec",
            "--json", "--ephemeral", "--skip-git-repo-check",
            "--ignore-user-config", *schema_arguments, "-o", str(answer), "-",
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=environment(model),
        )
        timed_out = threading.Event()
        heartbeat_stop = threading.Event()
        progress_lock = threading.Lock()
        stderr_lines = deque(maxlen=50)
        provider_errors = deque(maxlen=10)
        started_at = time.monotonic()
        progress = {
            "lastEventAt": started_at,
            "eventCount": 0,
            "contentEventCount": 0,
        }

        def enforce_timeout():
            if process.poll() is None:
                timed_out.set()
                stop_process(process)

        watchdog = (
            threading.Timer(timeout, enforce_timeout)
            if timeout is not None else None
        )
        if watchdog is not None:
            watchdog.daemon = True

        def relay_heartbeat():
            while not heartbeat_stop.wait(STRUCTURED_HEARTBEAT_SECONDS):
                now = time.monotonic()
                with progress_lock:
                    quiet = now - progress["lastEventAt"]
                    count = progress["eventCount"]
                    content_count = progress["contentEventCount"]
                elapsed = now - started_at
                label = activity_label or f"{stage.title()} model request"
                alive = process.poll() is None
                emit(
                    "status", stage,
                    label=f"{label} local request is alive",
                    text=(
                        f"Verified local Codex request process {process.pid} "
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
                    requestPid=process.pid, model=model,
                    modelProvider=model_provider(model),
                    reasoningEffort=effort,
                )

        heartbeat = threading.Thread(target=relay_heartbeat, daemon=True)

        def read_stderr():
            for stderr_line in process.stderr:
                value = stderr_line.strip()
                if value:
                    stderr_lines.append(value)

        stderr_reader = threading.Thread(target=read_stderr, daemon=True)
        stderr_reader.start()
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            if watchdog is not None:
                watchdog.start()
            heartbeat.start()
            for line in process.stdout:
                try:
                    raw_event = json.loads(line)
                    if raw_event.get("type") == "error":
                        message = raw_event.get("message")
                        if isinstance(message, str) and not message.startswith(
                            "Reconnecting..."
                        ):
                            provider_errors.append(message)
                    elif raw_event.get("type") == "turn.failed":
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
                        with progress_lock:
                            progress["lastEventAt"] = time.monotonic()
                            progress["eventCount"] += 1
                            if content_event:
                                progress["contentEventCount"] += 1
                        emit(
                            "codex_event", stage, event=event,
                            activityLabel=activity_label or "",
                        )
                    if raw_event.get("type") == "turn.completed":
                        emit_cache_usage(stage, raw_event.get("usage"))
                except json.JSONDecodeError:
                    emit(
                        "diagnostic", stage,
                        text="Codex returned a malformed event.",
                    )
            code = process.wait()
        finally:
            heartbeat_stop.set()
            if watchdog is not None:
                watchdog.cancel()
            stop_process(process)
            stderr_reader.join(timeout=1)
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except (AttributeError, OSError):
                    pass
        if timed_out.is_set():
            raise StructuredAttemptTimeout(
                f"The {stage} model did not respond within {timeout:g} seconds."
            )
        if code:
            detail = "\n".join((*provider_errors, *stderr_lines)).strip()
            secret = deepseek_key()
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
            detail = _clipped(detail, 8000)
            raise Error(
                f"Codex {stage} failed: {detail}"
                if detail else f"Codex {stage} failed without diagnostics."
            )
        return answer.read_text(encoding="utf-8") if answer.exists() else ""

def critic_audit_assignment_sha256(statement, solution, model, effort, instructions, config=None, identity=None):
    """Fingerprint the exact request inputs, model, settings, and parallel items."""
    config = _parallel_definition(config)
    identity = ({"statement": text(statement), "solution": text(solution), "instructions": text(instructions)} if identity is None else identity)
    assignment = {**identity, "model": chosen_model(model), "reasoningEffort": effective_effort(model, effort), config.get("checkpoint", {}).get("item_key", "items"): config["items"]}
    return _sha256(json.dumps(assignment, ensure_ascii=False, sort_keys=True))

def critic_audit_checkpoint_path(directory=None):
    """Return the private run-local independent-audit checkpoint path."""

    return Path(directory or Path.cwd()) / CRITIC_AUDIT_CHECKPOINT_FILENAME

def save_critic_candidate(solution, directory=None):
    """Atomically preserve the exact candidate about to receive an audit."""

    path = Path(directory or Path.cwd()) / SAVED_CANDIDATE_FILENAME
    _private_atomic_write(path, text(solution) + "\n")
    return path

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


def structured(
    prompt, schema_value, stage, model=MODEL, effort=EFFORT,
    speed=DEFAULT_SPEED, summary=DEFAULT_REASONING_SUMMARY,
    timeout=None, attempts=STRUCTURED_MAX_ATTEMPTS,
    request_label=None, activity_label=None, features=(),
):
    """Run one read-only structured Codex call and relay its visible events."""

    model = chosen_model(model)
    effort = effective_effort(model, effort)
    speed = effective_speed(model, speed)
    summary = reasoning_summary(model, summary)
    require_model_credentials(model)
    prompt = structured_prompt_for_model(prompt, schema_value, model)
    if timeout is None and is_deepseek_model(model):
        timeout = STRUCTURED_ATTEMPT_TIMEOUT_SECONDS.get(stage, 900)
    try:
        attempts = int(attempts)
    except (TypeError, ValueError) as exc:
        raise Error("Structured attempts must be a positive integer.") from exc
    if attempts < 1:
        raise Error("Structured attempts must be a positive integer.")
    raw, attempt_effort = "", effort
    for attempt in range(attempts):
        attempt_prompt = (
            prompt if attempt == 0
            else structured_retry_prompt(prompt, raw, schema_value)
        )
        emit(
            "request", stage,
            label=(
                (request_label or f"Exact {stage} input") if attempt == 0
                else f"Exact {stage} structured-output retry"
            ),
            text=attempt_prompt, attempt=attempt + 1,
            model=model, modelProvider=model_provider(model),
            reasoningEffort=attempt_effort, reasoningSummary=summary,
            serviceTier=speed, responseSchema=schema_value,
        )
        try:
            raw = run_structured_attempt(
                attempt_prompt, schema_value, stage, model, attempt_effort,
                speed, summary, timeout=timeout,
                activity_label=activity_label or request_label, features=features,
            )
        except StructuredAttemptTimeout as exc:
            emit(
                "diagnostic", stage,
                text=f"Structured output attempt {attempt + 1} timed out: {exc}",
            )
            if attempt + 1 >= attempts:
                suffix = (
                    " The structured stage timed out twice."
                    if attempts > 1 else ""
                )
                raise Error(f"{exc}{suffix}") from exc
            if stage in {"review", "critic"} and is_deepseek_model(model):
                attempt_effort = effective_effort(model, "medium")
            emit(
                "status", stage, label="Retrying timed-out model request",
                text=(
                    "Retrying once"
                    + (
                        f" at {attempt_effort} reasoning effort."
                        if stage in {"review", "critic"}
                        and is_deepseek_model(model)
                        else "."
                    )
                ),
            )
            continue
        try:
            result = validate_json_schema(
                decoded_json_object(raw), schema_value,
            )
            return result, raw
        except Error as exc:
            emit(
                "diagnostic", stage,
                text=(
                    f"Structured output attempt {attempt + 1} was invalid: "
                    f"{exc}"
                ),
                rawResponse=raw[:STRUCTURED_RETRY_OUTPUT_CHARS],
            )
            if attempt + 1 >= attempts:
                suffix = (
                    " The structured stage was retried once and failed again."
                    if attempts > 1 else ""
                )
                raise Error(f"{exc}{suffix}") from exc
            emit(
                "status", stage, label="Retrying structured output",
                text="Retrying once without search or local tools.",
            )
    raise Error(f"Codex {stage} did not return structured output.")


def make_prompt(statement, template=None):
    """Insert the approved statement into the single template marker."""

    template = (
        builtin_workflow("author_critic")["prompts"]["author"]
        if template is None else text(template)
    )
    if template.count(MARKER) != 1:
        raise Error(f"The author prompt must contain exactly one {MARKER}.")
    return template.replace(MARKER, text(statement), 1)


class RPC:
    """Send the few JSON-RPC messages needed by Goal mode."""

    def __init__(self, process, record=None):
        self.process, self.record = process, record
        self.number, self.waiting = 0, deque()
        self.write_lock = threading.Lock()

    def send(self, message):
        with self.write_lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def wire(self):
        """Read one new message from Codex."""

        line = self.process.stdout.readline()
        if not line:
            raise Error("Codex app-server stopped unexpectedly.")
        message = json.loads(line)
        if self.record:
            self.record(message)
        # This small noninteractive client cannot answer server-side questions.
        if "id" in message and "method" in message:
            raise Error(f"Interactive Codex request is unsupported: {message['method']}.")
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

    def close(self):
        stop_process(self.process)


AUTHOR_ANCHOR_FILENAME = "author-anchor.md"


AUTHOR_MEMORY_FILENAME = "author-memory.json"


AUTHOR_MEMORY_SCHEMA_VERSION = 1


AUTHOR_MEMORY_MAX_BYTES = 64 * 1024


AUTHOR_MEMORY_PROMPT_MAX_BYTES = 24 * 1024


AUTHOR_MEMORY_LIMITS = {
    "attempts": 24,
    "approaches": 24,
    "blockedRoutes": 24,
    "criticFeedback": 24,
    "unresolvedObligations": 16,
    "candidateFingerprints": 128,
}


def _sha256(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def normalized_candidate(value):
    """Normalize only presentation-level whitespace for duplicate fingerprints."""

    lines = str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _clipped(value, limit=1600):
    """Keep ledger text bounded while retaining a fingerprint of omitted text."""

    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    suffix = f" ... [truncated; sha256={_sha256(value)[:16]}]"
    return value[:max(0, limit - len(suffix))].rstrip() + suffix


def _clipped_utf8(value, limit=384):
    """Clip model-visible history to an exact UTF-8 byte budget."""

    value = str(value or "").strip()
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = f" ... [truncated; sha256={_sha256(value)[:16]}]"
    remaining = max(0, limit - len(suffix.encode("utf-8")))
    prefix = encoded[:remaining].decode("utf-8", errors="ignore").rstrip()
    return prefix + suffix


def _private_atomic_write(path, content):
    """Atomically replace one private run-local UTF-8 state file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def author_anchor(original_prompt, statement, prompts=None):
    """Return the immutable run-local author contract."""

    original_prompt = str(original_prompt)
    statement = str(statement)
    text(original_prompt)
    text(statement)
    prompts = builtin_workflow("author_critic")["prompts"] if prompts is None else prompts
    return prompts["anchor"].format(
        original_prompt=original_prompt, statement=statement,
    )


def reanchored_author_input(
    original_prompt, statement, memory_snapshot, instruction, prompts=None,
):
    """Build a self-contained author continuation without starting a new thread."""

    original_prompt = str(original_prompt)
    statement = str(statement)
    text(original_prompt)
    text(statement)
    prompts = builtin_workflow("author_critic")["prompts"] if prompts is None else prompts
    return prompts["reanchor"].format(
        original_prompt=original_prompt, statement=statement,
        memory_snapshot=str(memory_snapshot), instruction=str(instruction).strip(),
    )


class AuthorMemory:
    """Bounded, deterministic author history stored inside one private run."""

    def __init__(self, directory, original_prompt, statement, prompts=None):
        self.prompts = prompts
        self.directory = Path(directory).resolve()
        self.anchor_path = self.directory / AUTHOR_ANCHOR_FILENAME
        self.memory_path = self.directory / AUTHOR_MEMORY_FILENAME
        self.original_prompt = str(original_prompt)
        self.statement = str(statement)
        text(self.original_prompt)
        text(self.statement)
        self.data = self._new_data()
        self._load_existing()
        self.save()

    def _new_data(self):
        return {
            "schemaVersion": AUTHOR_MEMORY_SCHEMA_VERSION,
            "problem": {
                "statementSha256": _sha256(self.statement),
                "authorPromptSha256": _sha256(self.original_prompt),
            },
            "sequence": 0,
            "currentAttemptId": None,
            "attempts": [],
            "approaches": [],
            "blockedRoutes": [],
            "criticFeedback": [],
            "unresolvedObligations": [],
            "candidateFingerprints": [],
            "rollup": {
                "attemptsDropped": 0,
                "approachesDropped": 0,
                "blockedRoutesDropped": 0,
                "criticFeedbackDropped": 0,
                "unresolvedObligationsDropped": 0,
                "candidateFingerprintsDropped": 0,
                "archiveDigest": _sha256(""),
            },
        }

    def _load_existing(self):
        """Load only controller-created memory for this exact assignment."""

        if not self.memory_path.exists():
            return
        try:
            serialized = self.memory_path.read_text(encoding="utf-8")
            if len(serialized.encode("utf-8")) > AUTHOR_MEMORY_MAX_BYTES:
                raise ValueError("file exceeds the author-memory size limit")
            candidate = json.loads(serialized)
            problem = candidate.get("problem") if isinstance(candidate, dict) else None
            list_keys = tuple(AUTHOR_MEMORY_LIMITS)
            compatible = (
                isinstance(candidate, dict)
                and candidate.get("schemaVersion") == AUTHOR_MEMORY_SCHEMA_VERSION
                and isinstance(problem, dict)
                and problem.get("statementSha256") == _sha256(self.statement)
                and problem.get("authorPromptSha256") == _sha256(self.original_prompt)
                and isinstance(candidate.get("sequence"), int)
                and candidate["sequence"] >= 0
                and (
                    candidate.get("currentAttemptId") is None
                    or isinstance(candidate.get("currentAttemptId"), str)
                )
                and all(
                    isinstance(candidate.get(key), list)
                    and all(isinstance(item, dict) for item in candidate[key])
                    for key in list_keys
                )
                and isinstance(candidate.get("rollup"), dict)
                and all(
                    isinstance(candidate["rollup"].get(key), int)
                    for key in self.data["rollup"]
                    if key.endswith("Dropped")
                )
                and isinstance(
                    candidate["rollup"].get("archiveDigest"), str
                )
                and len(candidate["rollup"]["archiveDigest"]) == 64
                and all(
                    isinstance(item.get("id"), str)
                    for item in candidate["attempts"]
                )
                and all(
                    isinstance(item.get("normalizedSha256"), str)
                    and isinstance(item.get("firstAttemptId"), str)
                    and isinstance(item.get("lastAttemptId"), str)
                    and isinstance(item.get("occurrences"), int)
                    for item in candidate["candidateFingerprints"]
                )
                and all(
                    isinstance(item.get("fingerprint"), str)
                    and isinstance(item.get("occurrences"), int)
                    for item in candidate["blockedRoutes"]
                )
                and all(
                    isinstance(item.get("fingerprint"), str)
                    for item in candidate["unresolvedObligations"]
                )
            )
            if not compatible:
                raise ValueError("incompatible schema or assignment hashes")
            defaults = self.data["rollup"]
            candidate["rollup"] = {
                key: candidate["rollup"].get(key, value)
                for key, value in defaults.items()
            }
            self.data = candidate
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            emit(
                "diagnostic", "solve",
                text=f"Ignored unusable durable author memory: {exc}",
            )

    def _attempt(self, attempt_id=None):
        attempt_id = attempt_id or self.data.get("currentAttemptId")
        return next(
            (
                item for item in self.data["attempts"]
                if item.get("id") == attempt_id
            ),
            None,
        )

    def _fold_dropped(self, key, item):
        rollup_key = f"{key}Dropped"
        if rollup_key in self.data["rollup"]:
            self.data["rollup"][rollup_key] += 1
        previous = self.data["rollup"]["archiveDigest"]
        encoded = json.dumps(item, sort_keys=True, ensure_ascii=False)
        self.data["rollup"]["archiveDigest"] = _sha256(previous + encoded)

    def _bound(self):
        for key, limit in AUTHOR_MEMORY_LIMITS.items():
            values = self.data[key]
            while len(values) > limit:
                self._fold_dropped(key, values.pop(0))

        # Individual text is clipped before insertion. This final hard bound
        # drops whole historical records rather than ever writing partial JSON.
        eviction_order = (
            "approaches", "blockedRoutes", "criticFeedback", "attempts",
            "candidateFingerprints",
        )
        while len(self.serialized().encode("utf-8")) > AUTHOR_MEMORY_MAX_BYTES:
            dropped = False
            for key in eviction_order:
                values = self.data[key]
                if not values:
                    continue
                if key == "attempts" and len(values) == 1:
                    continue
                self._fold_dropped(key, values.pop(0))
                dropped = True
                break
            if not dropped:
                break
        size = len(self.serialized().encode("utf-8"))
        if size > AUTHOR_MEMORY_MAX_BYTES:
            raise ValueError(
                f"author memory is {size} bytes; limit is "
                f"{AUTHOR_MEMORY_MAX_BYTES} bytes"
            )

    def serialized(self):
        return json.dumps(
            self.data, ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n"

    def save(self):
        """Persist canonical files, but never fail the proof over diagnostics."""

        try:
            self._bound()
            _private_atomic_write(
                self.anchor_path,
                author_anchor(self.original_prompt, self.statement, self.prompts),
            )
            _private_atomic_write(self.memory_path, self.serialized())
            return True
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            emit(
                "diagnostic", "solve",
                text=f"Could not persist durable author memory: {exc}",
            )
            return False

    def record_candidate(
        self, solution, source, revision=0, critic_round=0,
        status="awaiting_critic", persist=True,
    ):
        solution = text(solution)
        exact = _sha256(solution)
        normalized = _sha256(normalized_candidate(solution))
        seen = next(
            (
                item for item in self.data["candidateFingerprints"]
                if item["normalizedSha256"] == normalized
            ),
            None,
        )
        self.data["sequence"] += 1
        attempt_id = f"a{self.data['sequence']:06d}"
        duplicate_of = seen["firstAttemptId"] if seen else None
        if seen:
            seen["lastAttemptId"] = attempt_id
            seen["occurrences"] += 1
        else:
            self.data["candidateFingerprints"].append({
                "normalizedSha256": normalized,
                "firstAttemptId": attempt_id,
                "lastAttemptId": attempt_id,
                "occurrences": 1,
            })
        previous = self._attempt()
        if previous and previous.get("status") not in {"approved", "rejected"}:
            previous["status"] = "superseded"
        self.data["attempts"].append({
            "id": attempt_id,
            "source": _clipped(source, 160),
            "revision": int(revision),
            "criticRound": int(critic_round),
            "status": _clipped(status, 80),
            "candidate": {
                "sha256": exact,
                "normalizedSha256": normalized,
                "characters": len(solution),
            },
            "duplicateOf": duplicate_of,
            "approachFamily": "",
            "approachResult": "",
        })
        self.data["currentAttemptId"] = attempt_id
        if persist:
            self.save()
        return attempt_id

    def preserve_latest_candidate(
        self, solution, source, status, revision=0, critic_round=0,
    ):
        """Persist final author output without duplicating the current body."""

        solution = text(solution)
        normalized = _sha256(normalized_candidate(solution))
        current = self._attempt()
        candidate = current.get("candidate", {}) if current else {}
        if candidate.get("normalizedSha256") == normalized:
            current["status"] = _clipped(status, 80)
            self.save()
            return current["id"]
        return self.record_candidate(
            solution, source, revision=revision, critic_round=critic_round,
            status=status,
        )

    @staticmethod
    def _memory_update(report):
        value = report.get("memory_update")
        if not isinstance(value, dict):
            return {
                "approach_family": "", "approach_result": "",
                "blocked_routes": [], "unresolved_obligations": [],
            }
        blocked = value.get("blocked_routes")
        obligations = value.get("unresolved_obligations")
        return {
            "approach_family": _clipped(value.get("approach_family"), 400),
            "approach_result": _clipped(value.get("approach_result"), 1200),
            "blocked_routes": blocked if isinstance(blocked, list) else [],
            "unresolved_obligations": (
                obligations if isinstance(obligations, list) else []
            ),
        }

    def record_critic_report(
        self, report, critic_round, attempt_id=None, result_attempt_id=None,
    ):
        attempt = self._attempt(attempt_id)
        if attempt is None:
            return
        result_attempt = (
            self._attempt(result_attempt_id) if result_attempt_id else attempt
        ) or attempt
        update = self._memory_update(report)
        attempt["approachFamily"] = update["approach_family"]
        attempt["approachResult"] = update["approach_result"]
        if update["approach_family"] or update["approach_result"]:
            self.data["approaches"].append({
                "attemptId": attempt["id"],
                "family": update["approach_family"],
                "result": update["approach_result"],
                "verdict": str(report.get("verdict") or ""),
                "resultAttemptId": result_attempt["id"],
            })

        for blocked in update["blocked_routes"][:6]:
            if not isinstance(blocked, dict):
                continue
            route = _clipped(blocked.get("route"), 500)
            reason = _clipped(blocked.get("reason"), 1200)
            reopen = _clipped(blocked.get("reopen_condition"), 800)
            if not route and not reason:
                continue
            fingerprint = _sha256(route.lower() + "\n" + reason.lower())
            existing = next((
                item for item in self.data["blockedRoutes"]
                if item["fingerprint"] == fingerprint
            ), None)
            if existing:
                existing["occurrences"] += 1
                existing["lastAttemptId"] = attempt["id"]
                continue
            self.data["blockedRoutes"].append({
                "fingerprint": fingerprint,
                "route": route,
                "reason": reason,
                "reopenCondition": reopen,
                "firstAttemptId": attempt["id"],
                "lastAttemptId": attempt["id"],
                "occurrences": 1,
            })

        bugs = str(report.get("bugs") or "").strip()
        if bugs:
            self.data["criticFeedback"].append({
                "attemptId": result_attempt["id"],
                "auditedAttemptId": attempt["id"],
                "resultAttemptId": result_attempt["id"],
                "criticRound": int(critic_round),
                "bugs": _clipped(bugs, 4000),
                "fullTextSha256": _sha256(bugs),
            })

        verdict = report.get("verdict")
        fixed = bool(report.get("fixed"))
        if verdict == "pass" and not fixed:
            attempt["status"] = "approved"
            self.data["unresolvedObligations"] = []
        else:
            attempt["status"] = "critic_fixed" if fixed else "rejected"
            obligations = [
                _clipped(item, 700)
                for item in update["unresolved_obligations"][:12]
                if str(item or "").strip()
            ]
            if verdict == "reject" and not obligations and bugs:
                obligations = [_clipped(bugs, 700)]
            # The critic is required to report every bug still present in its
            # result candidate, so its latest list supersedes older live bugs.
            self.data["unresolvedObligations"] = []
            state = "awaiting_verification" if fixed else "needs_author"
            for obligation in obligations:
                fingerprint = _sha256(obligation.lower())
                existing = next((
                    item for item in self.data["unresolvedObligations"]
                    if item["fingerprint"] == fingerprint
                ), None)
                if existing:
                    existing["state"] = state
                    existing["lastAttemptId"] = attempt["id"]
                else:
                    self.data["unresolvedObligations"].append({
                        "fingerprint": fingerprint,
                        "text": obligation,
                        "state": state,
                        "auditedAttemptId": attempt["id"],
                        "firstAttemptId": result_attempt["id"],
                        "lastAttemptId": result_attempt["id"],
                    })
        self.save()

    def mark_current(self, status):
        attempt = self._attempt()
        if attempt:
            attempt["status"] = _clipped(status, 80)
        if status == "approved":
            self.data["unresolvedObligations"] = []
        elif status == "awaiting_critic":
            for item in self.data["unresolvedObligations"]:
                item["state"] = "awaiting_verification"
        self.save()

    def snapshot(self):
        """Render a concise model-visible view, independently capped at 24 KiB."""

        view = {
            "currentAttemptId": self.data["currentAttemptId"],
            "recentAttempts": self.data["attempts"][-10:],
            "approaches": self.data["approaches"][-12:],
            "blockedRoutes": self.data["blockedRoutes"][-12:],
            "unresolvedObligations": [
                {
                    "fingerprint": _clipped_utf8(
                        item.get("fingerprint"), 80,
                    ),
                    "text": _clipped_utf8(item.get("text"), 384),
                    "state": _clipped_utf8(item.get("state"), 48),
                    "firstAttemptId": _clipped_utf8(
                        item.get("firstAttemptId"), 80,
                    ),
                    "lastAttemptId": _clipped_utf8(
                        item.get("lastAttemptId"), 80,
                    ),
                }
                for item in self.data["unresolvedObligations"]
            ],
            "recentCriticFeedback": self.data["criticFeedback"][-8:],
            "repeatedCandidates": [
                item for item in self.data["candidateFingerprints"]
                if item["occurrences"] > 1
            ][-16:],
            "rollup": self.data["rollup"],
        }
        eviction_order = (
            "approaches", "blockedRoutes", "recentCriticFeedback",
            "recentAttempts", "repeatedCandidates",
        )
        while True:
            rendered = json.dumps(
                view, ensure_ascii=False, indent=2, sort_keys=True,
            )
            if len(rendered.encode("utf-8")) <= AUTHOR_MEMORY_PROMPT_MAX_BYTES:
                return rendered
            for key in eviction_order:
                if view[key]:
                    view[key].pop(0)
                    break
            else:
                return rendered


AUTHOR_LIMIT_POLL_SECONDS = 0.25


INTERRUPT_GRACE_SECONDS = 30


SUMMARY_GRACE_SECONDS = 300


def repair_prompt(
    statement, solution, bugs, revision_number, critic_round=None,
    include_statement=True, prompts=None,
):
    """Give unresolved critic bugs back to the original author thread."""

    critic_label = (
        f"critic round {critic_round}" if critic_round is not None
        else "the current candidate"
    )
    statement_block = (
        f"\n\nSTATEMENT:\n{text(statement)}" if include_statement else ""
    )
    prompts = builtin_workflow("author_critic")["prompts"] if prompts is None else prompts
    return prompts["repair"].format(
        critic_label=critic_label, revision_number=revision_number,
        statement_block=statement_block, solution=text(solution), bugs=text(bugs),
    ).strip()


def author_session(
    prompt, statement, thinking_hours=DEFAULT_AUTHOR_HOURS,
    author_model=AUTHOR_MODEL, effort=EFFORT, author_effort=None,
    speed=DEFAULT_SPEED, author_limit_file=None, elapsed_seconds=0,
    prompts=None, stages=None, node_name="author", memory_directory=None,
    summary=DEFAULT_REASONING_SUMMARY, author_steer_file=None,
):
    """Keep one author thread alive, yielding candidates and accepting rejections.

    The YAML runner owns transitions. Closing the generator stops the deadline
    watcher and app-server, including when an audit fails or the graph ends.
    """

    stages = stages or _GOAL_STAGES
    initial_stage, resume_stage, failure_stage = (stages[key] for key in ("initial", "resume", "failure"))
    prompts = builtin_workflow("author_critic")["prompts"] if prompts is None else prompts
    thinking_hours = controlled_author_hours(
        author_limit_file, author_hours(thinking_hours)
    )
    author_model = chosen_model(author_model)
    author_effort = effective_effort(author_model, author_effort or effort)
    speed = effective_speed(author_model, speed)
    author_summary = reasoning_summary(author_model, summary)
    require_model_credentials(author_model)
    elapsed_seconds = prior_elapsed_seconds(elapsed_seconds)
    original_prompt = str(prompt)
    statement = str(statement)
    text(original_prompt)
    text(statement)
    memory = AuthorMemory(memory_directory or Path.cwd(), original_prompt, statement, prompts=prompts)
    emit(
        "status", initial_stage, label="Durable author memory ready",
        text=(
            f"{AUTHOR_ANCHOR_FILENAME} and {AUTHOR_MEMORY_FILENAME} are stored "
            "inside this private run workspace."
        ),
    )
    initial_author_input = prompts["initial"].format(
        original_prompt=original_prompt, memory_instructions=prompts["memory"],
        memory_snapshot=memory.snapshot(),
    )
    emit(
        "request", initial_stage, label="Exact solve input", text=initial_author_input,
        model=author_model, modelProvider=model_provider(author_model), reasoningEffort=author_effort, reasoningSummary=author_summary,
        serviceTier=speed,
    )
    emit(
        "request", initial_stage, label="Goal continuation instruction", text=prompts["goal"],
        model=author_model, modelProvider=model_provider(author_model), reasoningEffort=author_effort, reasoningSummary=author_summary,
        serviceTier=speed,
    )
    command = [
        codex(), "app-server", "--enable", "goals", "--enable", "multi_agent",
        *provider_arguments(author_model),
        *speed_arguments(speed, author_model), *context_cache_arguments(),
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="replace", bufsize=1, env=environment(author_model),
    )

    # Keep the latest complete answer from the original author thread.
    answers, stage = [], initial_stage
    thread, stop_timer = None, None
    author_active = threading.Event()
    current_turn = {"id": None}
    compaction_steers, seen_compactions = {}, set()
    last_author_cache_usage = {}

    # Every app-server response and notification passes through one filter.
    def record(message):
        nonlocal last_author_cache_usage
        response_id = message.get("id")
        if response_id in compaction_steers and "method" not in message:
            context = compaction_steers.pop(response_id)
            if "error" in message:
                emit(
                    "diagnostic", stage,
                    text=(
                        "Author context re-anchor raced with turn completion; "
                        f"the next author input will re-anchor again. {message['error']}"
                    ),
                    compactionId=context,
                )
        message = public_event(message)
        if message is None:
            return
        params = message.get("params", {})
        event_thread = params.get("threadId")
        root = thread is None or event_thread in {None, thread}
        emit(
            "codex_event", stage, event=message, root=root,
        )
        item = params.get("item") or {}
        if root and message.get("method") == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last = usage.get("last") if isinstance(usage, dict) else None
            if isinstance(last, dict):
                last_author_cache_usage = dict(last)
        if (
            root and message.get("method") == "item/completed"
            and item.get("type") in {"agentMessage", "agent_message"}
            and item.get("text")
        ):
            answers.append(item["text"])
        if (
            root and author_active.is_set() and stage in {initial_stage, resume_stage}
            and message.get("method") == "item/completed"
            and item.get("type") == "contextCompaction"
        ):
            turn_id = params.get("turnId")
            compaction_id = item.get("id") or f"{turn_id}:contextCompaction"
            compaction_key = (turn_id, compaction_id)
            if turn_id and compaction_key not in seen_compactions:
                seen_compactions.add(compaction_key)
                memory.save()
                anchored = reanchored_author_input(
                    original_prompt, statement, memory.snapshot(),
                    prompts["compaction"],
                    prompts=prompts,
                )
                emit(
                    "request", stage,
                    label="Author context re-anchor after compaction",
                    text=anchored, model=author_model, modelProvider=model_provider(author_model),
                    reasoningEffort=author_effort, serviceTier=speed,
                    reasoningSummary=author_summary, node=node_name,
                    compactionId=compaction_id,
                )
                try:
                    request_id = rpc.request("turn/steer", {
                        "threadId": thread,
                        "expectedTurnId": turn_id,
                        "input": [{"type": "text", "text": anchored}],
                    })
                    compaction_steers[request_id] = compaction_id
                except (Error, OSError) as exc:
                    emit(
                        "diagnostic", stage,
                        text=f"Could not re-anchor after compaction: {exc}",
                        compactionId=compaction_id,
                    )

    rpc = RPC(process, record)
    try:
        rpc.call("initialize", {
            "clientInfo": {
                "name": "tcs_prover", "title": "TCS Prover", "version": "1"
            }
        })
        rpc.send({"method": "initialized", "params": {}})
        started = rpc.call("thread/start", {
            "model": author_model, "modelProvider": model_provider(author_model),
            # The web app starts this process inside its private problem folder.
            "cwd": str(memory.directory), "ephemeral": False,
            "sandbox": "workspace-write", "approvalPolicy": "never",
            "config": {
                "model_reasoning_effort": author_effort,
                "model_reasoning_summary": author_summary,
                **({"service_tier": "fast"} if speed == "fast" else {}),
                "features": {"fast_mode": speed == "fast"},
            },
        })
        thread = started["thread"]["id"]

        # Paused avoids an empty automatic turn before the real prompt starts.
        goal = {"threadId": thread, "objective": prompts["goal"]}
        rpc.call("thread/goal/set", {**goal, "status": "paused"})
        author_active.set()
        started_turn = rpc.call("turn/start", {
            "threadId": thread,
            "input": [{"type": "text", "text": initial_author_input}],
            "summary": author_summary,
        })
        rpc.call("thread/goal/set", {**goal, "status": "active"})
        current_turn["id"] = (started_turn.get("turn") or {}).get("id")
        timed_out, stop_timer = threading.Event(), threading.Event()
        deadline_lock = threading.Lock()
        # The web UI passes the runtime already shown by its elapsed clock, so
        # review and approval time count toward the same total. Direct/headless
        # callers leave this at zero and start counting here.
        deadline_started = time.monotonic() - elapsed_seconds
        deadline_state = {
            "hours": thinking_hours,
            "at": deadline_started + thinking_hours * 3600,
        }

        def refresh_deadline():
            """Apply the latest total workflow limit from the web control file."""

            hours = controlled_author_hours(
                author_limit_file, deadline_state["hours"]
            )
            if hours != deadline_state["hours"]:
                deadline_state["hours"] = hours
                deadline_state["at"] = deadline_started + hours * 3600

        def deadline_expired():
            """Refresh the live limit and report whether total time is spent."""

            with deadline_lock:
                refresh_deadline()
                expired = time.monotonic() >= deadline_state["at"]
                if expired:
                    timed_out.set()
                return expired

        # At the deadline, interrupt an author but allow a critic to finish.
        def enforce_deadline():
            # Short waits let a live limit change take effect promptly. They also
            # avoid one Windows wait whose timeout stops counting during sleep.
            while True:
                with deadline_lock:
                    refresh_deadline()
                    remaining = deadline_state["at"] - time.monotonic()
                    if remaining <= 0:
                        timed_out.set()
                        if author_active.is_set():
                            try:
                                rpc.request("thread/goal/set", {
                                    **goal, "status": "paused",
                                })
                            except (Error, OSError):
                                pass
                            turn_id = current_turn["id"]
                            if turn_id:
                                try:
                                    rpc.request("turn/interrupt", {
                                        "threadId": thread, "turnId": turn_id,
                                    })
                                except (Error, OSError):
                                    pass
                        break
                if stop_timer.wait(min(AUTHOR_LIMIT_POLL_SECONDS, remaining)):
                    return
            # A critic is allowed to run past the deadline. Only a stuck author
            # interruption may force-stop the persistent app server.
            if (
                author_active.is_set()
                and not stop_timer.wait(INTERRUPT_GRACE_SECONDS)
                and process.poll() is None
            ):
                process.terminate()

        timer = threading.Thread(target=enforce_deadline, daemon=True)
        timer.start()

        def relay_author_steers():
            """Forward live UI instructions into the active author turn."""

            delivered_id = None
            while not stop_timer.wait(AUTHOR_STEER_POLL_SECONDS):
                command = pending_author_steer(
                    author_steer_file, delivered_id
                )
                turn_id = current_turn["id"]
                if not command or not author_active.is_set() or not turn_id:
                    continue
                command_id, instruction = command
                try:
                    request_id = rpc.request("turn/steer", {
                        "threadId": thread,
                        "expectedTurnId": turn_id,
                        "input": [{"type": "text", "text": instruction}],
                    })
                    delivered_id = command_id
                    emit(
                        "request", stage, label="Live author instruction sent",
                        text=instruction, model=author_model,
                        modelProvider=model_provider(author_model),
                        reasoningEffort=author_effort,
                        reasoningSummary=author_summary,
                        serviceTier=speed, node=node_name,
                        steerId=command_id, requestId=request_id,
                    )
                except (Error, OSError) as exc:
                    emit(
                        "diagnostic", stage,
                        text=f"Could not steer the active author: {exc}",
                        node=node_name, steerId=command_id,
                    )

        threading.Thread(target=relay_author_steers, daemon=True).start()
        emit(
            "status", initial_stage, label="Goal started",
            text=(
                f"Thread {thread}; total workflow limit {thinking_hours:g} "
                f"hours; {elapsed_seconds:g} seconds already elapsed."
            ),
            threadId=thread,
        )

        # Resume any terminal author attempt that ended without a solution.
        status, running, last_author_output = None, True, ""
        while True:
            try:
                message = rpc.read()
            except Error:
                if timed_out.is_set():
                    break
                raise
            params = message.get("params", {})
            if params.get("threadId") not in {None, thread}:
                continue
            method = message.get("method")
            if method == "turn/started":
                running = True
                turn_id = (params.get("turn") or {}).get("id")
                current_turn["id"] = turn_id
                # Stop an automatic turn that raced with the deadline pause.
                if timed_out.is_set() and turn_id:
                    rpc.request("turn/interrupt", {
                        "threadId": thread, "turnId": turn_id,
                    })
            elif method == "turn/completed":
                turn_status = (params.get("turn") or {}).get("status")
                running = False
                current_turn["id"] = None
                if last_author_cache_usage:
                    emit_cache_usage(
                        stage, last_author_cache_usage,
                        label="Author cache usage",
                    )
                    last_author_cache_usage = {}
                if turn_status in {"failed", "interrupted"} and not timed_out.is_set():
                    status = "turnFailed"
            elif method == "thread/goal/updated":
                value = params.get("goal", {})
                if value.get("threadId") == thread and value.get("status") in {
                    "complete", "blocked", "usageLimited", "budgetLimited"
                }:
                    status = value["status"]

            if timed_out.is_set() and not running:
                break
            if status == "complete" and not running and answers:
                break
            if (
                status == "blocked"
                or (status == "complete" and not answers)
            ) and not running:
                with deadline_lock:
                    refresh_deadline()
                    if time.monotonic() >= deadline_state["at"]:
                        timed_out.set()
                        break
                if answers:
                    last_author_output = answers[-1]
                    memory.record_candidate(
                        last_author_output, "author_partial",
                        status="incomplete",
                    )
                    answers.clear()
                memory.save()
                continuation = reanchored_author_input(
                    original_prompt, statement, memory.snapshot(),
                    prompts["continuation"],
                    prompts=prompts,
                )
                emit(
                    "request", initial_stage, label="Author continuation",
                    text=continuation, model=author_model, modelProvider=model_provider(author_model),
                    reasoningEffort=author_effort,
                    serviceTier=speed,
                    reasoningSummary=author_summary,
                )
                # Explicit turns start while Goal mode is paused.
                rpc.call("thread/goal/set", {**goal, "status": "paused"})
                if timed_out.is_set():
                    break
                resumed = rpc.call("turn/start", {
                    "threadId": thread,
                    "input": [{"type": "text", "text": continuation}],
                    "summary": author_summary,
                })
                current_turn["id"] = (resumed.get("turn") or {}).get("id")
                # The deadline and activation serialize through this lock.
                with deadline_lock:
                    if timed_out.is_set():
                        if current_turn["id"]:
                            rpc.request("turn/interrupt", {
                                "threadId": thread,
                                "turnId": current_turn["id"],
                            })
                    else:
                        rpc.request("thread/goal/set", {
                            **goal, "status": "active",
                        })
                status, running = None, True
            elif status in {
                "turnFailed", "usageLimited", "budgetLimited"
            } and not running:
                break

        # Close the polling race where an author answer arrives just after the
        # deadline but before the timer thread observes it.
        deadline_expired()
        author_active.clear()
        current_turn["id"] = None

        def summarize_failure(reason, previous=""):
            """Ask the persistent author thread to preserve unfinished work."""

            nonlocal stage, last_author_cache_usage
            stop_timer.set()
            author_active.clear()
            current_turn["id"] = None
            try:
                rpc.call("thread/goal/set", {**goal, "status": "paused"})
            except (Error, OSError):
                pass
            previous = previous or (
                answers[-1] if answers else last_author_output
            )
            stage, answers[:] = failure_stage, []
            memory.save()
            summary_prompt = prompts["failure_input"].format(
                instructions=prompts["failure"], reason=reason,
                memory_snapshot=memory.snapshot(),
            )
            emit(
                "request", failure_stage, label="Failure summary request",
                text=summary_prompt, model=author_model, modelProvider=model_provider(author_model),
                reasoningEffort=author_effort,
                serviceTier=speed,
                reasoningSummary=author_summary,
            )
            summary_stop = threading.Event()
            summary_expired = threading.Event()
            try:
                summary_turn = rpc.call("turn/start", {
                    "threadId": thread,
                    "input": [{"type": "text", "text": summary_prompt}],
                    "summary": author_summary,
                })
                summary_id = (summary_turn.get("turn") or {}).get("id")

                # Even the final summary has a small, fixed grace period.
                def limit_summary():
                    if summary_stop.wait(SUMMARY_GRACE_SECONDS):
                        return
                    summary_expired.set()
                    try:
                        if summary_id:
                            rpc.request("turn/interrupt", {
                                "threadId": thread, "turnId": summary_id,
                            })
                    except (Error, OSError):
                        pass
                    # A frozen server must not defeat the bounded summary.
                    if not summary_stop.wait(5) and process.poll() is None:
                        process.terminate()

                threading.Thread(target=limit_summary, daemon=True).start()
                while True:
                    message = rpc.read()
                    params = message.get("params", {})
                    if params.get("threadId") not in {None, thread}:
                        continue
                    turn = params.get("turn") or {}
                    if (
                        message.get("method") == "turn/completed"
                        and (not summary_id or turn.get("id") == summary_id)
                    ):
                        if last_author_cache_usage:
                            emit_cache_usage(
                                failure_stage, last_author_cache_usage,
                                label="Failure-summary cache usage",
                            )
                            last_author_cache_usage = {}
                        break
                    if summary_expired.is_set():
                        break
            except (Error, OSError, StopIteration):
                pass
            finally:
                summary_stop.set()
            summary = answers[-1] if answers else (
                f"{reason}\n\nLatest author output:\n{previous[:4000]}"
                if previous else f"{reason}\n\nNo complete solution was produced."
            )
            emit(
                "failure_result", failure_stage, label="Workflow failure summary",
                text=summary, output=summary,
            )
            return summary

        if timed_out.is_set() or status in {
            "turnFailed", "usageLimited", "budgetLimited"
        }:
            with deadline_lock:
                final_author_hours = deadline_state["hours"]
            reason = (
                f"The {final_author_hours:g}-hour total workflow limit was reached "
                "while the proof author was running."
                if timed_out.is_set()
                else f"The author stopped because its state became {status}."
            )
            latest = answers[-1] if answers else last_author_output
            if latest:
                memory.preserve_latest_candidate(
                    latest,
                    "author_timeout" if timed_out.is_set() else "author_failure",
                    "timed_out" if timed_out.is_set() else "failed",
                )
            summary = summarize_failure(reason, previous=latest)
            yield {"outcome": "failure", "output": summary}
            return

        emit(
            "status", initial_stage, label="Goal complete",
            text=f"Thread {thread}", threadId=thread,
        )

        solution = answers[-1]
        memory.record_candidate(
            solution, "initial_author", status="awaiting_critic",
        )
        revision_number = 0
        while True:
            rejection = yield {"outcome": "proof", "solution": solution, "memory": memory}
            # Audit safe fixes are the starting point for author repair.
            solution = rejection["solution"]
            round_number = rejection["round"]
            # Only unresolved bugs return to the persistent author thread.
            if deadline_expired():
                with deadline_lock:
                    final_author_hours = deadline_state["hours"]
                summary = summarize_failure(
                    f"The {final_author_hours:g}-hour total workflow limit "
                    "was reached while the critic was running. The rejected "
                    "candidate cannot be returned to the proof author.",
                    previous=solution,
                )
                yield {"outcome": "failure", "output": summary}
                return
            revision_number += 1
            repair = repair_prompt(
                statement, solution, rejection["bugs"], revision_number,
                critic_round=round_number, include_statement=False,
                prompts=prompts,
            )
            memory.save()
            instruction = reanchored_author_input(
                original_prompt, statement, memory.snapshot(), repair,
                prompts=prompts,
            )
            stage, answers[:] = resume_stage, []
            emit(
                "request", resume_stage,
                label=f"Proof author revision {revision_number}",
                text=instruction, model=author_model, modelProvider=model_provider(author_model),
                reasoningEffort=author_effort,
                serviceTier=speed,
                reasoningSummary=author_summary,
                node=node_name, round=0,
            )
            author_active.set()
            revision_turn = rpc.call("turn/start", {
                "threadId": thread,
                "input": [{"type": "text", "text": instruction}],
                "summary": author_summary,
            })
            current_turn["id"] = (
                revision_turn.get("turn") or {}
            ).get("id")
            revision_id = current_turn["id"]
            if deadline_expired() and current_turn["id"]:
                rpc.request("turn/interrupt", {
                    "threadId": thread, "turnId": current_turn["id"],
                })
            turn_status = None
            while True:
                try:
                    message = rpc.read()
                except Error:
                    if timed_out.is_set():
                        break
                    raise
                params = message.get("params", {})
                if params.get("threadId") not in {None, thread}:
                    continue
                if (
                    message.get("method") == "turn/completed"
                    and (params.get("turn") or {}).get("id") == revision_id
                ):
                    turn_status = (params.get("turn") or {}).get("status")
                    current_turn["id"] = None
                    if last_author_cache_usage:
                        emit_cache_usage(
                            resume_stage, last_author_cache_usage,
                            label="Author revision cache usage",
                        )
                        last_author_cache_usage = {}
                    if turn_status != "completed" and not timed_out.is_set():
                        raise Error(
                            f"Proof author revision {turn_status}; thread {thread}."
                        )
                    break
            author_active.clear()
            current_turn["id"] = None
            if deadline_expired():
                latest_revision = answers[-1] if answers else ""
                if latest_revision:
                    memory.preserve_latest_candidate(
                        latest_revision, "author_revision_timeout",
                        "timed_out", revision=revision_number,
                    )
                with deadline_lock:
                    final_author_hours = deadline_state["hours"]
                summary = summarize_failure(
                    f"The {final_author_hours:g}-hour total workflow limit "
                    "was reached while the proof author was revising a "
                    "critic-rejected candidate.",
                    previous=latest_revision or solution,
                )
                yield {"outcome": "failure", "output": summary}
                return
            if not answers:
                raise Error("The proof author returned no replacement solution.")
            solution = answers[-1]
            memory.record_candidate(
                solution, "author_revision", revision=revision_number,
                status="awaiting_critic",
            )
            memory.mark_current("awaiting_critic")
            emit(
                "author_result", resume_stage,
                label=f"Revised solution {revision_number}", text=solution,
                node=node_name, round=0,
            )
    except KeyboardInterrupt:
        confirmed = []
        if thread:
            # Wait briefly for persistence; forced cleanup still has a deadline.
            def pause():
                try:
                    rpc.call("thread/goal/set", {
                        "threadId": thread, "objective": prompts["goal"], "status": "paused"
                    })
                    confirmed.append(True)
                except (Error, OSError, json.JSONDecodeError):
                    pass

            worker = threading.Thread(target=pause, daemon=True)
            worker.start()
            worker.join(timeout=2)
            emit(
                "status", initial_stage,
                label="Goal paused" if confirmed else "Pause not confirmed",
                text=f"Thread {thread}",
            )
        raise
    finally:
        if stop_timer:
            stop_timer.set()
        rpc.close()


def _strip(value):
    if not isinstance(value, str):
        raise ValueError("strip() requires a string")
    return value.strip()


def _get(mapping, key, default=None):
    if not isinstance(mapping, dict):
        raise ValueError("get() requires a dictionary")
    return mapping.get(key, default)


_EXPRESSION_FUNCTIONS = {
    "len": len, "str": str, "bool": bool, "int": int, "float": float,
    "all": all, "any": any, "strip": _strip, "text": text,
    "is_string": lambda value: isinstance(value, str),
    "is_bool": lambda value: isinstance(value, bool),
    "is_list": lambda value: isinstance(value, list),
    "is_dict": lambda value: isinstance(value, dict),
    "get": _get, "json": lambda value: json.dumps(value, ensure_ascii=False, indent=2),
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
        if not isinstance(action, dict) or set(action) - {"when", "set", "merge", "emit", "memory", "write"}:
            raise ValueError("Unknown workflow action.")
        if not (set(action) - {"when"}):
            raise ValueError("A guarded action needs an operation.")
        if "when" in action:
            check_expression(action["when"])
        if "merge" in action:
            check_expression(action["merge"])
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
        if "memory" in action and not isinstance(action["memory"], str):
            entry = action["memory"]
            if not isinstance(entry, dict) or set(entry) != {"previous", "candidate", "feedback", "source", "status"}:
                raise ValueError("A ledger entry needs previous, candidate, feedback, source, and status.")
            for expression in entry.values():
                check_expression(expression)

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
        "structured": {"instructions", "inputs", "schema", "features", "require", "error", "parallel", "attempts", "provider_options", "request_label", "activity_label"},
        "goal": {"task", "marker", "lifecycle", "resume", "stages", "recovery"},
    }
    for name, node in nodes.items():
        if not isinstance(node, dict) or not {"run", "prompt", "next"} <= node.keys():
            raise ValueError(f"Node {name} needs run, prompt, and next.")
        _expand_node(node)
        kind = node["run"]
        if not isinstance(kind, str) or kind not in specific or set(node) - (shared | specific[kind]):
            raise ValueError(f"Unsupported operation or fields in node {name}.")
        reference = node["prompt"]
        references = [reference]
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
        else:
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
            if not isinstance(lifecycle, dict) or set(lifecycle) != required or not all(
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
            if not isinstance(bindings, dict) or not {"solution", "bugs", "round"} <= bindings.keys():
                raise ValueError(f"Goal node {name} must bind the candidate, feedback, and round for resumption.")
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


@functools.lru_cache(maxsize=2)
def builtin_workflow(name):
    """Load UI/convenience defaults lazily, without coupling custom graphs to them."""

    return load_workflow(WORKFLOWS / f"{name}.yaml")


def __getattr__(name):
    # These public defaults are used by the UI and existing Python callers.
    # The generic execution path never needs to open either bundled YAML file.
    prompt_names = {
        "AUTHOR_PROMPT": "author", "CRITIC_MEMORY_PROMPT": "critic_memory",
        "CONTINUE_PROMPT": "continuation", "FAILURE_SUMMARY_PROMPT": "failure",
        "AUTHOR_MEMORY_INSTRUCTIONS": "memory", "GOAL": "goal",
    }
    if name in prompt_names or name in {"AUTHOR_PROMPTS", "AUTHOR_WORKFLOW", "CRITIC_PROMPT", "CRITIC_SCHEMA", "DEFAULT_CRITIC_ROUNDS"}:
        workflow = builtin_workflow("author_critic")
        prompts = workflow["prompts"]
        if name in prompt_names:
            return prompts[prompt_names[name]]
        return {
            "AUTHOR_PROMPTS": prompts, "AUTHOR_WORKFLOW": workflow,
            "CRITIC_PROMPT": prompts["critic"] + "\n\n" + prompts["critic_memory"],
            "CRITIC_SCHEMA": workflow["nodes"]["critic"]["schema"],
            "DEFAULT_CRITIC_ROUNDS": workflow["nodes"]["critic"]["next"]["fixed"]["repeat"],
        }[name]
    if name in {"CLEAN_UP_PROMPTS", "CLEAN_UP_WORKFLOW", "FINAL_PROMPT", "FINAL_SCHEMA"}:
        workflow = builtin_workflow("clean_up")
        return {
            "CLEAN_UP_PROMPTS": workflow["prompts"], "CLEAN_UP_WORKFLOW": workflow,
            "FINAL_PROMPT": workflow["prompts"]["final"],
            "FINAL_SCHEMA": workflow["nodes"]["latex_editor"]["schema"],
        }[name]
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

    prompts = {**workflow["prompts"], **options.get("prompts", {})}
    if not all(isinstance(value, str) and value.strip() for value in prompts.values()):
        raise ValueError("Prompt overrides must be nonempty strings.")
    for node in workflow["nodes"].values():
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
            if "recovery" in node:
                _template_parts(prompts[node["recovery"]["prompt"]], {"original_prompt", "statement", "repair"})
            lifecycle = node["lifecycle"]
            if isinstance(lifecycle, list):
                lifecycle = {key: key for key in lifecycle}
            fields = {
                "anchor": {"original_prompt", "statement"},
                "reanchor": {"original_prompt", "statement", "memory_snapshot", "instruction"},
                "repair": {"critic_label", "revision_number", "statement_block", "solution", "bugs"},
                "initial": {"original_prompt", "memory_instructions", "memory_snapshot"},
                "failure_input": {"instructions", "reason", "memory_snapshot"},
            }
            for key, names in fields.items():
                _template_parts(prompts[lifecycle[key]], names)
        for branch in node["next"].values():
            if isinstance(branch, dict) and "repeat" in branch:
                limit = options.get(branch.get("option"), branch["repeat"])
                if type(limit) is not int or limit < 1:
                    raise ValueError("Repeat limits must be positive integers.")
    if options.get("critic_rounds") is not None:
        critic_limit(options["critic_rounds"])
    author_hours(options.get("thinking_hours", DEFAULT_AUTHOR_HOURS))
    prior_elapsed_seconds(options.get("elapsed_seconds", 0))
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


def _parallel_requests(config, prompts, state, options, visit):
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
    def run_one(index, item):
        prompt = _request_prompt(config, prompts, {**context, "item": item})
        report, _ = structured(prompt, config["schema"], stage, features=config.get("features", []), request_label=f"Independent critic audit {index + 1}", activity_label=f"Independent audit {index + 1}", **settings)
        report = dict(report)
        if config.get("item_field"):
            report[config["item_field"]] = item
        return report
    failures = []
    missing = [(index, item) for index, item in enumerate(config["items"]) if reports[index] is None]
    with ThreadPoolExecutor(max_workers=max(1, len(missing))) as pool:
        futures = {pool.submit(run_one, index, item): index for index, item in missing}
        for future in as_completed(futures):
            index = futures[future]
            try:
                reports[index] = future.result()
                if checkpoint:
                    save_critic_audit_checkpoint(reports, "", "", model, effort, "", config=config, identity=identity)
                emit("status", stage, label=f"Independent audit {index + 1} complete", text=reports[index].get("report", ""), node=stage, auditIndex=index + 1, auditVerdict=reports[index].get("verdict"))
            except Exception as exc:
                failures.append((index, str(exc)))
                emit("diagnostic", stage, text=f"Independent audit {index + 1} failed: {exc}", node=stage, auditIndex=index + 1)
    if failures:
        detail = "; ".join(f"audit {index + 1}: {message}" for index, message in sorted(failures))
        raise Error("The controller could not complete all independent audits. " + detail)
    return reports


def critic_audit_prompt(statement, solution, focus, instructions):
    workflow = builtin_workflow("author_critic")
    return render_template(workflow["prompts"]["critic_audit"], {"statement": statement, "solution": solution, "focus": focus, "instructions": instructions})


def independent_critic_audits(statement, solution, model, effort, instructions, speed, summary):
    workflow = builtin_workflow("author_critic")
    config = {**workflow["nodes"]["critic"]["parallel"], "stage": "critic"}
    save_critic_candidate(solution)
    prompts = {**workflow["prompts"], "critic": instructions}
    return _parallel_requests(config, prompts, {"statement": statement, "solution": solution}, {"model": model, "effort": effort, "speed": speed, "summary": summary}, 1)


def _model_call(node, prompts, state, options, visit):
    """Run a configured request, optionally collecting independent inputs first."""
    context = {"state": state, "visit": visit}
    parallel = node.get("parallel")
    if parallel:
        config = {key: node[key] for key in ("role", "stage", "model", "effort") if key in node}
        config.update(parallel)
        context["parallel"] = options.get("parallel_results")
        if context["parallel"] is None:
            context["parallel"] = _parallel_requests(config, prompts, state, options, visit)
    prompt = _request_prompt(node, prompts, context)
    settings = _structured_options(node, options)
    settings.update({key: node[key] for key in ("request_label", "activity_label") if key in node})
    report, raw = structured(prompt, node["schema"], node.get("stage", "model"), features=node.get("features", []), **settings)
    if parallel and parallel.get("output"):
        try:
            for condition in node.get("require", []):
                if not evaluate(condition, {**context, "result": report, "raw": raw}):
                    raise ValueError(f"Unmet response requirement: {condition}")
        except ValueError as exc:
            raise Error(node.get("error", str(exc))) from exc
        report[parallel["output"]] = context["parallel"]
        validate_json_schema(report, node["schema"])
    return report, raw


def _apply_actions(actions, context, stage, revision):
    state = context["state"]
    for action in actions:
        if "when" in action and not evaluate(action["when"], context):
            continue
        if "memory" in action and state.get("memory") is not None:
            memory, entry = state["memory"], action["memory"]
            if isinstance(entry, str):
                memory.mark_current(entry)
            else:
                values = {key: evaluate(expression, context) for key, expression in entry.items()}
                audited_attempt = memory.data.get("currentAttemptId")
                changed = normalized_candidate(values["candidate"]) != normalized_candidate(values["previous"])
                result_attempt = audited_attempt
                if changed:
                    result_attempt = memory.record_candidate(
                        values["candidate"], values["source"], revision=revision,
                        critic_round=context["visit"], status=values["status"], persist=False,
                    )
                memory.record_critic_report(
                    values["feedback"], context["visit"], attempt_id=audited_attempt,
                    result_attempt_id=result_attempt,
                )
                if not changed:
                    memory.mark_current(values["status"])
        if "merge" in action:
            values = evaluate(action["merge"], context)
            if not isinstance(values, dict):
                raise ValueError("Merged state must be a dictionary.")
            state.update(values)
        if "set" in action:
            state.update({key: evaluate(expression, context) for key, expression in action["set"].items()})
        if "write" in action:
            _private_atomic_write(Path.cwd() / action["write"]["path"], evaluate(action["write"]["text"], context))
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
                raise ValueError(f"Unmet response requirement: {condition}")
        outcome = evaluate(node.get("outcome", "'done'"), context)
    except ValueError as exc:
        raise Error(node.get("error", str(exc))) from exc
    if not isinstance(outcome, str) or outcome not in node["next"]:
        raise Error(f"Node returned an unknown outcome: {outcome!r}.")
    context["outcome"] = outcome
    _apply_actions(node.get("after", []), context, node.get("stage", "model"), revision)
    state["outcome"] = outcome
    return context


def _execute(workflow, state, options, prompts):
    nodes = workflow["nodes"]
    current, visits, revision = options.get("start_node", next(iter(nodes))), 0, 0
    if current not in nodes:
        raise ValueError(f"Unknown workflow entry node: {current!r}.")
    started_at = time.monotonic()
    sessions, revisions = {}, {}
    multiple_goals = sum(node["run"] == "goal" for node in nodes.values()) > 1
    try:
        while current != "end":
            node = nodes[current]
            visits += 1
            _apply_actions(node.get("before", []), {"state": state, "visit": visits}, node.get("stage", current), revision)
            if node["run"] == "structured":
                result, raw = _model_call(node, prompts, state, options, visits)
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
                    recovery = node.get("recovery")
                    if recovery and evaluate(recovery["when"], context):
                        feedback = {key: evaluate(expression, context) for key, expression in node["resume"].items()}
                        repair = repair_prompt(task, feedback["solution"], feedback["bugs"], 1, critic_round=feedback["round"], include_statement=False, prompts=session_prompts)
                        prompt = render_template(prompts[recovery["prompt"]], {"original_prompt": prompt, "statement": task, "repair": repair})
                    settings = _settings(node, options)
                    session = author_session(
                        prompt, task, thinking_hours=options.get("thinking_hours", DEFAULT_AUTHOR_HOURS),
                        author_model=settings["model"], effort=settings["effort"], speed=settings["speed"],
                        author_limit_file=options.get("author_limit_file"), elapsed_seconds=options.get("elapsed_seconds", 0) + (time.monotonic() - started_at if options.get("start_node") else 0),
                        prompts=session_prompts,
                        stages=node.get("stages"), node_name=current,
                        summary=settings["summary"], author_steer_file=options.get("author_steer_file"),
                        memory_directory=(Path.cwd() / "goal-memory" / _sha256(current)[:16]) if multiple_goals else None,
                    )
                    sessions[current] = session
                    result = next(session)
                else:
                    revision += 1
                    revisions[current] = revision
                    result = sessions[current].send({key: evaluate(expression, context) for key, expression in node["resume"].items()})
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
    finally:
        for session in sessions.values():
            session.close()


def execute(path, state=None, *, options=None):
    """Execute any graph in the documented format; no node-name dispatch."""

    workflow = load_workflow(path)
    options = {} if options is None else options
    return _execute(workflow, {} if state is None else state, options, prepare(workflow, options))


def execute_workflows(paths, state, options=None):
    """Validate once, then feed state through the graphs until one reports failure."""

    options = {} if options is None else options
    prepared = []
    for index, path in enumerate(paths):
        workflow = load_workflow(path)
        local_options = dict(options)
        if index:
            local_options.pop("start_node", None)
        elif "start_node" in local_options and local_options["start_node"] not in workflow["nodes"]:
            raise ValueError(f"Unknown workflow entry node: {local_options['start_node']!r}.")
        prepared.append((workflow, prepare(workflow, local_options), local_options))
    for workflow, prompts, local_options in prepared:
        _execute(workflow, state, local_options, prompts)
        if state.get("failed"):
            break
    return state


def _builtin_node(workflow_name, node_name, state, options, visit=1, prompts=None):
    """Compatibility convenience calls share the same generic node interpreter."""

    workflow = builtin_workflow(workflow_name)
    resolved = {**workflow["prompts"], **(prompts or {}), **options.get("prompts", {})}
    node = workflow["nodes"][node_name]
    _apply_actions(node.get("before", []), {"state": state, "visit": visit}, node.get("stage", "model"), 0)
    result, raw = _model_call(node, resolved, state, options, visit)
    _complete_node(node, state, result, raw, visit, 0)
    return result


def criticize(statement, solution, round_number, model=CRITIC_MODEL, effort=EFFORT, instructions=None, speed=DEFAULT_SPEED, prompts=None, summary=DEFAULT_REASONING_SUMMARY):
    options = {"model": model, "effort": effort, "speed": speed, "summary": summary, "prompts": {"critic": instructions} if instructions is not None else {}}
    resolved = {**builtin_workflow("author_critic")["prompts"], **(prompts or {}), **options["prompts"]}
    instructions = _instructions(builtin_workflow("author_critic")["nodes"]["critic"], resolved)
    options["parallel_results"] = independent_critic_audits(text(statement), text(solution), chosen_model(model), effective_effort(model, effort), instructions, chosen_speed(speed), chosen_reasoning_summary(summary))
    return _builtin_node("author_critic", "critic", {"statement": statement, "solution": solution}, options, round_number, prompts)


def finalize(statement, solution, model=WRITER_MODEL, effort=EFFORT, instructions=None, speed=DEFAULT_SPEED, prompts=None, summary=DEFAULT_REASONING_SUMMARY):
    state = {"statement": statement, "solution": solution}
    options = {"model": model, "effort": effort, "speed": speed, "summary": summary, "prompts": {"final": instructions} if instructions is not None else {}}
    _builtin_node("clean_up", "latex_editor", state, options, prompts=prompts)
    return state["output"]


def polish(source, model=WRITER_MODEL, effort=EFFORT, instructions=None, speed=DEFAULT_SPEED, prompts=None, summary=DEFAULT_REASONING_SUMMARY):
    state = {"source": source}
    options = {"model": model, "effort": effort, "speed": speed, "summary": summary, "prompts": {"final": instructions} if instructions is not None else {}}
    _builtin_node("clean_up", "latex_editor", state, options, prompts=prompts)
    return state["output"]


def audit_candidate(
    statement, solution, critic_rounds=None,
    thinking_hours=DEFAULT_AUTHOR_HOURS,
    author_model=AUTHOR_MODEL, critic_model=CRITIC_MODEL,
    writer_model=WRITER_MODEL, effort=EFFORT,
    author_effort=None, critic_effort=None, writer_effort=None,
    author_prompt=None, critic_prompt=None,
    final_prompt=None,
    speed=DEFAULT_SPEED, summary=DEFAULT_REASONING_SUMMARY,
    author_limit_file=None, author_steer_file=None,
):
    """Enter the normal proof loop at a fresh critic with a saved proof."""

    audit_started = time.monotonic()
    if critic_rounds is None:
        critic_rounds = builtin_workflow("author_critic")["nodes"]["critic"]["next"]["fixed"]["repeat"]
    critic_rounds = critic_limit(critic_rounds)
    thinking_hours = controlled_author_hours(
        author_limit_file, author_hours(thinking_hours)
    )
    author_model = chosen_model(author_model)
    critic_model = chosen_model(critic_model)
    writer_model = chosen_model(writer_model)
    author_effort = effective_effort(author_model, author_effort or effort)
    critic_effort = effective_effort(critic_model, critic_effort or effort)
    writer_effort = effective_effort(writer_model, writer_effort or effort)
    speed = chosen_speed(speed)
    summary = chosen_reasoning_summary(summary)
    for selected_model in {author_model, critic_model, writer_model}:
        require_model_credentials(selected_model)
    statement, solution = text(statement), text(solution)
    emit(
        "status", "critic", label="Saved candidate audit started",
        text=(
            "The initial proof author is loaded from the source job. A fresh "
            "critic is auditing the complete saved candidate; an author repair "
            "runs only if the critic rejects it."
        ),
    )
    for round_number in range(1, critic_rounds + 1):
        report = criticize(
            statement, solution, round_number,
            model=critic_model, effort=critic_effort,
            instructions=critic_prompt, speed=speed, summary=summary,
        )
        solution = report["solution"].strip()
        if report["verdict"] == "pass" and not report["fixed"]:
            emit(
                "status", "critic", label="Saved candidate approved",
                text=f"Round {round_number} found no bugs to fix.",
            )
            return finalize(
                statement, solution, model=writer_model,
                effort=writer_effort, instructions=final_prompt,
                speed=speed, summary=summary,
            )
        if report["verdict"] == "pass":
            emit(
                "status", "critic", label="Critic repaired saved candidate",
                text=(
                    f"Round {round_number} repaired the proof; a fresh critic "
                    "will recheck it."
                    if round_number < critic_rounds else
                    f"Round {round_number} repaired the proof."
                ),
            )
            continue
        emit(
            "partial_result", "critic", label="Rejected saved candidate",
            text=solution, output=solution,
        )
        emit(
            "status", "repair", label="Returning saved proof to author",
            text=(
                "The entry critic rejected the saved candidate. A proof author "
                "will repair that exact candidate before the normal critic loop "
                "continues."
            ),
            node="author",
        )
        base = make_prompt(statement, author_prompt) if author_prompt else make_prompt(statement)
        repair = repair_prompt(
            statement, solution, report["bugs"], 1,
            critic_round=round_number, include_statement=False,
        )
        resumed_prompt = (
            f"{base}\n\nRECOVERY ENTRY FROM A SAVED CRITIC JOB:\n{repair}"
        )
        return run_goal(
            resumed_prompt, statement,
            critic_rounds=critic_rounds,
            thinking_hours=thinking_hours,
            author_model=author_model,
            critic_model=critic_model,
            writer_model=writer_model,
            effort=effort,
            author_effort=author_effort,
            critic_effort=critic_effort,
            writer_effort=writer_effort,
            critic_prompt=critic_prompt,
            final_prompt=final_prompt,
            speed=speed,
            author_limit_file=author_limit_file,
            elapsed_seconds=time.monotonic() - audit_started,
            summary=summary,
            author_steer_file=author_steer_file,
        )
    emit(
        "status", "critic", label="Critic round limit accepted",
        text=(
            f"All bugs were fixed in {critic_rounds} consecutive critic rounds "
            "without rejection. Accepting the latest repaired solution "
            "without another check."
        ),
    )
    return finalize(
        statement, solution, model=writer_model,
        effort=writer_effort, instructions=final_prompt,
        speed=speed, summary=summary,
    )



def run_goal(prompt, statement, critic_rounds=None, thinking_hours=DEFAULT_AUTHOR_HOURS,
             author_model=AUTHOR_MODEL, critic_model=CRITIC_MODEL, writer_model=WRITER_MODEL,
             effort=EFFORT, author_effort=None, critic_effort=None, writer_effort=None,
             critic_prompt=None, final_prompt=None, speed=DEFAULT_SPEED,
             author_limit_file=None, elapsed_seconds=0, summary=DEFAULT_REASONING_SUMMARY, author_steer_file=None):
    options = dict(
        author_input=prompt, thinking_hours=thinking_hours, author_model=author_model,
        critic_model=critic_model, writer_model=writer_model, effort=effort,
        author_effort=author_effort, critic_effort=critic_effort, writer_effort=writer_effort,
        speed=speed, author_limit_file=author_limit_file, elapsed_seconds=elapsed_seconds, summary=summary, author_steer_file=author_steer_file,
        prompts={key: value for key, value in {"critic": critic_prompt, "final": final_prompt}.items() if value is not None},
    )
    if critic_rounds is not None:
        options["critic_rounds"] = critic_limit(critic_rounds)
    state = execute_workflows([WORKFLOWS / "author_critic.yaml", WORKFLOWS / "clean_up.yaml"], {"statement": statement}, options)
    return state["output"]


def main(argv=None):
    configure_standard_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflows", nargs="+", help="YAML files, executed in order")
    parser.add_argument("--model", choices=MODELS, default=MODEL)
    for role in ("author", "critic", "writer"):
        parser.add_argument(f"--{role}-model", choices=MODELS)
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
        if state.get("failed"):
            return 1
        emit("workflow_result", "workflow", output=state.get("output", ""))
        return 0
    except KeyboardInterrupt:
        print("\nStopped workflow.", file=sys.stderr)
        return 130
    except (Error, ValueError, OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
