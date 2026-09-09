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
_GOAL_LIFECYCLE = ("goal", "continuation", "compaction", "repair", "resume")
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


def goal_session(runtime, prompt, *, prompts, settings, options,
                 node_name="author", stages=None, initial_instruction=""):
    """Run one goal-enabled thread with lifecycle instructions supplied by YAML.

    Yield its final answer only after goal completion; accept follow-up feedback
    through ``send``. The model manages its own work in the supplied workspace.
    """
    directory = os.path.abspath(os.fspath(options.get("goal_cwd") or os.getcwd()))
    stages = stages or {"initial": "solve", "resume": "repair", "failure": "failure"}
    stage = stages["initial"]
    values = dict(original_prompt=prompt, directory=directory,
                  solution="", bugs="", round=0, revision_number=0, instruction=initial_instruction)

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
    state = {"turn": None, "active": True, "steer": None}
    pending_steers = {}
    pending_compaction = None
    seen_compactions = set()
    goal = {}
    failure = None
    last_usage = {}

    def record(message):
        event = runtime.public_event(message)
        if event is not None:
            emit("codex_event", event=event,
                 root=event.get("params", {}).get("threadId") in {None, thread})

    def steer(instruction, identity, compaction=False):
        request = rpc.request("turn/steer", {
            "threadId": thread, "expectedTurnId": state["turn"],
            "input": [{"type": "text", "text": instruction}],
        })
        pending_steers[request] = (identity, instruction, compaction)
        emit("request", label="Author context re-anchor after compaction" if compaction
             else "Live author instruction sent", text=instruction, threadId=thread)

    def watch():
        while not stop.wait(0.1):
            if runtime.workflow_remaining(options) <= 0:
                expired.set()
                if not state["active"]:
                    return
                try:
                    if thread:
                        rpc.request("thread/goal/set", {**goal, "status": "paused"})
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
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        rpc.call("thread/goal/set", {**goal, "status": "paused"})
        emit("request", label="Exact solve input", text=instruction, threadId=thread,
             model=model, reasoningEffort=settings["effort"], reasoningSummary=summary)
        state["active"] = True
        result = rpc.call("turn/start", {"threadId": thread, "cwd": str(directory),
            "input": [{"type": "text", "text": instruction}], "summary": summary})
        state["turn"] = (result.get("turn") or {}).get("id")
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        rpc.call("thread/goal/set", {**goal, "status": "active"})

    def collect(item, answers):
        if (item.get("type") in {"agentMessage", "agent_message"}
                and item.get("phase") in {None, "final_answer"}
                and isinstance(item.get("text"), str) and item["text"].strip()):
            answers.append(item["text"])

    try:
        if runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        process = subprocess.Popen([
            runtime.codex(), "app-server", "--enable", "goals", "--disable", "multi_agent",
            *runtime.provider_arguments(model), *runtime.speed_arguments(settings["speed"], model),
            *runtime.context_cache_arguments(),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(directory),
            env=runtime.environment(model))
        rpc = runtime.RPC(process, record)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        rpc.call("initialize", {"clientInfo": {"name": "tcs_prover", "title": "TCS Prover", "version": "1"}})
        rpc.send({"method": "initialized", "params": {}})
        thread_options = {
            "model": model, "cwd": str(directory), "runtimeWorkspaceRoots": [str(directory)],
            "sandbox": "workspace-write", "approvalPolicy": "never",
            "config": {"model_reasoning_effort": settings["effort"], "model_reasoning_summary": summary,
                "features": {"goals": True, "multi_agent": False, "fast_mode": settings["speed"] == "fast"}},
        }
        if hasattr(runtime, "model_provider"):
            thread_options["modelProvider"] = runtime.model_provider(model)
        resumed = False
        if options.get("goal_thread_id"):
            try:
                result = rpc.call("thread/resume", {**thread_options, "threadId": options["goal_thread_id"],
                                                    "excludeTurns": True})
                resumed = True
            except runtime.Error as exc:
                missing = ("not found", "not_found", "notfound", "unknown thread", "does not exist",
                           "no rollout", "failed to load thread", "failed to load rollout",
                           "could not find thread", "thread unavailable", "thread is unavailable")
                if not any(word in str(exc).lower() for word in missing):
                    raise
                emit("diagnostic", text=f"Saved thread unavailable; starting a new session with the supplied resume instructions: {exc}")
        if not resumed:
            result = rpc.call("thread/start", {**thread_options, "ephemeral": False})
        thread = result["thread"]["id"]
        goal = {"threadId": thread, "objective": render("goal")}
        emit("status", label="Goal resumed" if resumed else "Goal started", threadId=thread,
             text=f"Thread {thread}")
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
            if params.get("threadId") not in {None, thread}:
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
            method, turn = message.get("method"), params.get("turn") or {}
            if method == "turn/started":
                running, answers = True, []
                state["turn"] = turn.get("id")
                if pending_compaction and state["turn"]:
                    steer(pending_compaction, "retry", True)
                    pending_compaction = None
            elif method == "item/completed":
                item = params.get("item") or {}
                collect(item, answers)
                if item.get("type") == "contextCompaction":
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
                if turn.get("status") in {"failed", "interrupted"}:
                    raise runtime.Error(f"Author turn {turn['status']}: {turn.get('error') or 'no complete result'}")
            elif method == "error" and not params.get("willRetry", False):
                raise runtime.Error(str(params.get("error") or params))
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
    except (runtime.Error, OSError, ValueError) as exc:
        stage = stages["failure"]
        diagnostic = "Workflow time limit reached." if expired.is_set() else str(exc)
        emit("diagnostic", text=diagnostic, threadId=thread)
        emit("failure_result", label="Author stopped", text=diagnostic, output=diagnostic, threadId=thread)
        failure = {"outcome": "failure", "output": diagnostic}
    finally:
        stop.set()
        if rpc and thread and state["active"]:
            def pause():
                try:
                    rpc.call("thread/goal/set", {**goal, "status": "paused"})
                except (runtime.Error, OSError, ValueError):
                    pass
            cleanup = threading.Thread(target=pause, daemon=True)
            cleanup.start()
            cleanup.join(timeout=1)
        if process:
            runtime.stop_process(process)
    if failure:
        yield failure


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
            if not isinstance(config, dict) or not {"argv", "timeout", "result"} <= config.keys() or set(config) - {"argv", "cwd", "env", "timeout", "produces", "log", "result"}:
                raise ValueError("A command needs argv, timeout, and a result state key.")
            if not isinstance(config["argv"], list) or not config["argv"] or not all(isinstance(arg, str) and arg for arg in config["argv"]):
                raise ValueError("Command argv must be a nonempty list of strings.")
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
        "structured": {"instructions", "inputs", "schema", "features", "require", "error", "parallel", "attempts", "provider_options", "request_label", "activity_label", "normalize"},
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


@functools.lru_cache(maxsize=2)
def builtin_workflow(name):
    """Load UI/convenience defaults lazily, without coupling custom graphs to them."""

    return load_workflow(WORKFLOWS / f"{name}.yaml")


def __getattr__(name):
    # These public defaults are used by the UI and existing Python callers.
    # The generic execution path never needs to open either bundled YAML file.
    prompt_names = {
        "AUTHOR_PROMPT": "author",
        "CONTINUE_PROMPT": "continuation", "GOAL": "goal",
    }
    if name in prompt_names or name in {"AUTHOR_PROMPTS", "AUTHOR_WORKFLOW", "CRITIC_PROMPT", "CRITIC_SCHEMA", "DEFAULT_CRITIC_ROUNDS"}:
        workflow = builtin_workflow("author_critic")
        prompts = workflow["prompts"]
        if name in prompt_names:
            return prompts[prompt_names[name]]
        return {
            "AUTHOR_PROMPTS": prompts, "AUTHOR_WORKFLOW": workflow,
            "CRITIC_PROMPT": prompts["critic"],
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
    """One elapsed-time budget across research, audits, and final editing."""
    started = options.setdefault("_workflow_started", time.monotonic())
    hours = controlled_author_hours(options.get("author_limit_file"),
                                   options.get("thinking_hours", DEFAULT_AUTHOR_HOURS))
    return max(0.0, hours * 3600 - options.get("elapsed_seconds", 0)
               - (time.monotonic() - started))


def _bounded_request_settings(settings, options):
    remaining = workflow_remaining(options)
    if remaining <= 0:
        raise Error("Workflow time limit reached; saved work remains resumable.")
    return {**settings, "timeout": min(settings.get("timeout") or 900, remaining),
            "attempts": 1}


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
        report, _ = structured(prompt, config["schema"], stage, features=config.get("features", []), request_label=f"Independent critic audit {index + 1}", activity_label=f"Independent audit {index + 1}", **_bounded_request_settings(settings, options))
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
    report, raw = structured(prompt, node["schema"], node.get("stage", "model"), features=node.get("features", []), **_bounded_request_settings(settings, options))
    if parallel and parallel.get("output"):
        report[parallel["output"]] = context["parallel"]
    for key, expression in node.get("normalize", {}).items():
        report[key] = evaluate(expression, {**context, "result": report, "raw": raw})
    validate_json_schema(report, node["schema"])
    for condition in node.get("require", []):
        if not evaluate(condition, {**context, "result": report, "raw": raw}):
            raise Error(node.get("error", "Invalid model response: " + condition))
    return report, raw


def _run_command(config, context):
    """Run a bounded argv command and check only its declared output files."""
    argv = [render_template(value, context) for value in config["argv"]]
    directory = (Path.cwd() / render_template(config.get("cwd", "."), context)).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs = [directory / name for name in config.get("produces", [])]
    for path in outputs:
        path.unlink(missing_ok=True)
    engine = shutil.which(argv[0])
    code, output = None, ""
    if engine is None:
        status, diagnostic = "unavailable", f"Command unavailable: {argv[0]}."
    else:
        environment = {**os.environ, **{key: render_template(value, context) for key, value in config.get("env", {}).items()}}
        try:
            result = subprocess.run([engine, *argv[1:]], cwd=directory, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", timeout=config["timeout"], check=False,
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
            status, diagnostic = "fail", f"Command could not run: {exc}"
    if config.get("log"):
        _private_atomic_write(directory / config["log"], diagnostic + "\n\n" + output)
    return {"status": status, "diagnostic": diagnostic, "returncode": code}


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
    sessions, revisions = {}, {}
    options.setdefault("_workflow_started", time.monotonic())
    try:
        while current != "end":
            node = nodes[current]
            if workflow_remaining(options) <= 0:
                raise Error("Workflow time limit reached.")
            visits += 1
            emit("status", node.get("stage", current), label=f"Workflow step: {current}",
                 node=current, visit=visits)
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
                    directory = options.get("goal_cwd") or str(Path.cwd())
                    recovery = node.get("recovery")
                    instruction = ""
                    if recovery and evaluate(recovery["when"], context):
                        feedback = {key: evaluate(expression, context) for key, expression in node["resume"].items()}
                        instruction = render_template(prompts[recovery["prompt"]], {
                            "original_prompt": prompt, "directory": str(directory),
                            "instruction": "", "revision_number": 1, **feedback,
                        })
                    session = goal_session(
                        sys.modules[__name__], prompt, prompts=session_prompts,
                        settings=_settings(node, options), options=options,
                        stages=node.get("stages"), node_name=current, initial_instruction=instruction,
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

    options = {} if options is None else dict(options)
    options.setdefault("_workflow_started", time.monotonic())
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
    options["prompts"] = {**(prompts or {}), **options["prompts"]}
    execute_workflows([WORKFLOWS / "clean_up.yaml"], state, options)
    if state.get("failed"):
        raise Error(state["output"])
    return state["output"]


def polish(source, model=WRITER_MODEL, effort=EFFORT, instructions=None, speed=DEFAULT_SPEED, prompts=None, summary=DEFAULT_REASONING_SUMMARY):
    state = {"source": source}
    options = {"model": model, "effort": effort, "speed": speed, "summary": summary, "prompts": {"final": instructions} if instructions is not None else {}}
    options["prompts"] = {**(prompts or {}), **options["prompts"]}
    execute_workflows([WORKFLOWS / "clean_up.yaml"], state, options)
    if state.get("failed"):
        raise Error(state["output"])
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

    options = dict(start_node="critic", critic_rounds=critic_rounds,
        thinking_hours=thinking_hours, author_model=author_model, critic_model=critic_model,
        writer_model=writer_model, effort=effort, author_effort=author_effort,
        critic_effort=critic_effort, writer_effort=writer_effort, speed=speed,
        author_limit_file=author_limit_file, summary=summary,
        author_steer_file=author_steer_file,
        prompts={key: value for key, value in {"author": author_prompt,
            "critic": critic_prompt, "final": final_prompt}.items() if value is not None})
    if critic_rounds is None:
        options.pop("critic_rounds")
    state = execute_workflows([WORKFLOWS / "author_critic.yaml", WORKFLOWS / "clean_up.yaml"],
                              {"statement": text(statement), "solution": text(solution)}, options)
    if state.get("failed"):
        raise Error(state["output"])
    return state["output"]



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
