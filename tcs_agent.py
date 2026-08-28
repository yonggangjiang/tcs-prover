#!/usr/bin/env python3
"""Review a TCS statement or run an approved statement as a Codex goal."""

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# Preserve the original ChatGPT defaults; DeepSeek is an additional provider.
ROOT = Path(__file__).resolve().parent
DEEPSEEK_MODEL_CATALOG = ROOT / "deepseek-models.json"
DEEPSEEK_MODEL = "deepseek-v4-pro"
MODELS = (
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    DEEPSEEK_MODEL,
)
MODEL, EFFORT = "gpt-5.6-sol", "ultra"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
SPEEDS, DEFAULT_SPEED = ("standard", "fast"), "fast"
REASONING_SUMMARIES = ("none", "concise", "detailed")
DEFAULT_REASONING_SUMMARY = "concise"
# Kept as the default-tier name for older callers and trace consumers.
SERVICE_TIER = DEFAULT_SPEED
AUTHOR_MODEL = CRITIC_MODEL = WRITER_MODEL = MODEL

OPENAI_PROVIDER = "openai"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_TOKEN_ENV = "TCS_PROVER_DEEPSEEK_TOKEN"
CUSTOM_PROVIDER_LOGIN_PLACEHOLDER = "tcs-prover-custom-provider"

# Every role retains the original Sol/Ultra default and remains configurable.
REVIEW_MODEL = MODEL
REVIEW_MODELS = MODELS
REVIEW_EFFORT = EFFORT

# These values connect the approved statement to the user's prompt template.
MARKER, TEMPLATE = "[STATEMENT]", ROOT / "prompt.txt"
GOAL = "Complete the task supplied in the first turn and continue until done."
DEFAULT_CRITIC_ROUNDS, MAX_CRITIC_ROUNDS = 100, 100
MAX_AUTHOR_HOURS = 168
DEFAULT_AUTHOR_HOURS = MAX_AUTHOR_HOURS
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
AUTHOR_LIMIT_POLL_SECONDS = 0.25
AUTHOR_STEER_POLL_SECONDS = 0.25
AUTHOR_STEER_MAX_CHARS = 12000
INTERRUPT_GRACE_SECONDS = 30
SUMMARY_GRACE_SECONDS = 300
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
STRUCTURED_WORKSPACE = ROOT / ".codex-structured-workspace"
EMIT_LOCK = threading.Lock()
CONTINUE_PROMPT = """
The previous author attempt ended without a complete solution. Continue working
toward a complete rigorous solution. Do not stop, give up, or summarize while
time remains. Mark the goal complete only after returning the full solution.
""".strip()
FAILURE_SUMMARY_PROMPT = """
The author must stop now. Stop trying to solve the problem.
Return a concise failure summary covering the progress made, approaches tried,
partial results, unresolved obstacles, and the best next steps. Do not claim
that the problem was solved.
""".strip()

# Each non-Goal call returns a tiny, strict JSON object.
SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["statement", "notes"],
    "additionalProperties": False,
}
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "focus": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail"]},
                    "report": {"type": "string"},
                },
                "required": ["focus", "verdict", "report"],
                "additionalProperties": False,
            },
        },
        "verdict": {"type": "string", "enum": ["pass", "reject"]},
        "fixed": {"type": "boolean"},
        "solution": {"type": "string"},
        "bugs": {"type": "string"},
        "memory_update": {
            "type": "object",
            "properties": {
                "approach_family": {"type": "string"},
                "approach_result": {"type": "string"},
                "blocked_routes": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "route": {"type": "string"},
                            "reason": {"type": "string"},
                            "reopen_condition": {"type": "string"},
                        },
                        "required": ["route", "reason", "reopen_condition"],
                        "additionalProperties": False,
                    },
                },
                "unresolved_obligations": {
                    "type": "array",
                    "maxItems": 12,
                    "items": {"type": "string"},
                },
            },
            "required": [
                "approach_family", "approach_result", "blocked_routes",
                "unresolved_obligations",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "checks", "verdict", "fixed", "solution", "bugs", "memory_update",
    ],
    "additionalProperties": False,
}
CRITIC_CHECK_SCHEMA = CRITIC_SCHEMA["properties"]["checks"]["items"]
CRITIC_AUDIT_FOCI = (
    (
        "Definitions, quantifiers, game semantics, and line-by-line "
        "correctness of every construction and invariant."
    ),
    (
        "Communication protocol implementability and complete accounting of "
        "bits, rounds, synchronization, adaptivity, and hidden disclosures."
    ),
    (
        "Hostile counterexample search: boundary cases, ownership mistakes, "
        "sampling/deletion claims, circular lemmas, and theorem-strength gaps."
    ),
)
FINAL_SCHEMA = {
    "type": "object",
    "properties": {"latex": {"type": "string"}},
    "required": ["latex"],
    "additionalProperties": False,
}
REVIEW_PROMPT = """
Read the current statement below carefully and produce a rigorous, self-contained problem statement without changing its intended claim. 
Do a initial scanning on corner cases, edge cases, counter examples to see if the statement is trivial or false. 
If you found the statement is trivial or false, first try to clear typos, fix any ambiguities, or add missing context or conventional assumptions to make the statement non-trivial. If you can fix it, explain the fix in the note, and return the final problem statement. Remember to check the problem statement again until it passed your audit. If you cannot fix it, explain why in the notes and return the version you think is the best possible statement.
If the statement remains non-trivial and open after your scanning, then return a complete, rigorous, self-contained problem statement.
The returned problem statement should just be a complete, rigorous, self-contained problem statement without any commentary or notes. The notes field should contain your reasoning, explanation of any fixes, and any remaining concerns about the statement.
Return only the requested JSON.
""".strip()
# Kept for callers that imported the former alias. Review mode now lives in the
# trailing task block so the reusable instruction prefix remains byte-identical.
REVISION_PROMPT = REVIEW_PROMPT
CRITIC_MEMORY_PROMPT = """
Also return concise historical metadata for the durable author ledger: name the
candidate's approach family, state what the audit established about it, list
routes this audit actually blocks, and list the proof obligations that remain.
This metadata records the audit; it must not change the mathematical verdict.
""".strip()
CRITIC_PROMPT = """
Act as a coordinating proof critic. The controller will run three fresh,
independent hostile auditors in parallel and provide their completed reports.
Do not spawn or wait for subagents yourself.

After collecting all three audits, read them and try to fix every reported bug
yourself in a complete replacement solution. If you fix them all, return pass,
an empty bugs string, the repaired solution, and fixed=true so another fresh
critic can check it. If no fix was needed, return pass, the unchanged solution,
and fixed=false. If you cannot confidently fix every bug, return reject,
fixed=false, your best complete solution after safe fixes, and every unresolved
bug with its exact location and required repair for the author. Return only the
requested JSON.
""".strip() + "\n\n" + CRITIC_MEMORY_PROMPT
FINAL_PROMPT = """
Act as the TCS editor and turn the solution below into a latex proof. Preserve its mathematical
content while removing repetition and process commentary. Produce a
cleaned-up, self-contained, organized, rigorous, readable LaTeX proof
with clearly stated theorems and logically ordered sections that is considered as a well-written TCS paper in a tree-lick structure:
(1) Use definitions and terminologys following the convention of previous works, if there are any,
(2) For algorithmic tasks, split the proof into algorithm description, correctness proof, and complexity analysis,
(3) Each section and subsectionstarts with a clear statement of the theorem or lemma being proved, the proof should also be well-structures and splits into lemmas if necessary.
(4) Each new definition and lemma should have a short explanation of motivation and context before introducing it.
(5) Expand on any ambiguous or underspecified details in the proof, and provide clear explanations for any non-trivial steps or reasoning.
The final output should be a complete LaTeX document that can be compiled without errors.
Return only the requested JSON.
""".strip()


class Error(RuntimeError):
    """Show a short, understandable failure."""


class StructuredAttemptTimeout(Error):
    """One bounded structured request stopped waiting for its provider."""


def text(value):
    """Require nonempty text."""

    value = value.strip()
    if not value:
        raise Error("The problem statement is empty.")
    return value


def critic_limit(value):
    """Require a small positive critic-round limit."""

    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
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


def chosen_model(value):
    """Require one supported base model."""

    if value not in MODELS:
        raise Error("Choose Sol, Terra, Luna, or DeepSeek V4 Pro.")
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


def review_prompt(draft, feedback="", instructions=REVIEW_PROMPT):
    """Return the exact review prompt sent to Codex."""

    instructions = text(instructions)
    feedback = feedback.strip()
    if feedback:
        return (
            f"{instructions}\n\nREVIEW TASK\nMODE: REVISION\n"
            "Revise the current checked statement in response to the author's "
            "request while preserving the intended claim."
            f"\n\nCURRENT CHECKED STATEMENT:\n{text(draft)}"
            f"\n\nAUTHOR REVISION REQUEST:\n{feedback}"
        )
    return (
        f"{instructions}\n\nREVIEW TASK\nMODE: INITIAL\n"
        f"\nDRAFT:\n{text(draft)}"
    )


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
    """Validate the strict JSON-Schema subset used by this harness."""

    expected = schema.get("type")
    valid_type = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
    }.get(expected)
    if valid_type and not valid_type(value):
        raise Error(f"Structured output field {path} must be {expected}.")
    if "enum" in schema and value not in schema["enum"]:
        raise Error(f"Structured output field {path} has an unsupported value.")
    if expected == "object":
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        missing = required.difference(value)
        if missing:
            raise Error(
                f"Structured output field {path} is missing "
                f"{', '.join(sorted(missing))}."
            )
        if schema.get("additionalProperties") is False:
            extras = set(value).difference(properties)
            if extras:
                raise Error(
                    f"Structured output field {path} contains unsupported "
                    f"properties: {', '.join(sorted(extras))}."
                )
        for key, item in value.items():
            if key in properties:
                validate_json_schema(item, properties[key], f"{path}.{key}")
    elif expected == "array":
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise Error(f"Structured output field {path} has too few items.")
        if maximum is not None and len(value) > maximum:
            raise Error(f"Structured output field {path} has too many items.")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, f"{path}[{index}]")
    return value


def output_schema_arguments(model, schema_path):
    """Use provider enforcement only when the selected model supports it."""

    if is_deepseek_model(model):
        return []
    return ["--output-schema", str(schema_path)]


def structured_tool_arguments(stage):
    """Keep strict structured stages focused on producing their final object."""

    return [
        "-c", 'web_search="disabled"',
        "-c", "tools.web_search=false",
        "-c", "tools.view_image=false",
        "--disable", "shell_tool",
        "--disable", "multi_agent",
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
    timeout=None, activity_label=None,
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
            *structured_tool_arguments(stage),
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


def structured(
    prompt, schema_value, stage, model=MODEL, effort=EFFORT,
    speed=DEFAULT_SPEED, summary=DEFAULT_REASONING_SUMMARY,
    timeout=None, attempts=STRUCTURED_MAX_ATTEMPTS,
    request_label=None, activity_label=None,
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
                activity_label=activity_label or request_label,
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


def review(
    draft, feedback="", model=REVIEW_MODEL, effort=REVIEW_EFFORT,
    instructions=REVIEW_PROMPT, speed=DEFAULT_SPEED,
    summary=DEFAULT_REASONING_SUMMARY,
):
    """Review with the user's chosen model."""

    try:
        chosen_model(model)
        effort = chosen_effort(effort)
        report, raw = structured(
            review_prompt(draft, feedback, instructions), SCHEMA, "review",
            model=model, effort=effort, speed=speed, summary=summary,
        )
        report = {
            "statement": text(report["statement"]),
            "notes": report["notes"].strip(),
        }
        emit(
            "review_result", "review", label="Exact structured response",
            text=raw, review=report,
        )
        return report
    except (KeyError, TypeError, AttributeError) as exc:
        raise Error("Codex returned an invalid review.") from exc


def make_prompt(statement, template=None):
    """Insert the approved statement into the single template marker."""

    template = (
        TEMPLATE.read_text(encoding="utf-8")
        if template is None else text(template)
    )
    if template.count(MARKER) != 1:
        raise Error(f"prompt.txt must contain exactly one {MARKER}.")
    return template.replace(MARKER, text(statement), 1)


def _sha256(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _normalized_candidate(value):
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


def author_anchor(original_prompt, statement):
    """Return the immutable run-local author contract."""

    original_prompt = str(original_prompt)
    statement = str(statement)
    text(original_prompt)
    text(statement)
    return (
        "# Immutable author contract\n\n"
        "The controller reproduces the original assignment below verbatim. "
        "It remains binding after every continuation, critic rejection, and "
        "context compaction.\n\n"
        "## Original author prompt (verbatim)\n\n"
        f"{original_prompt}\n\n"
        "## Exact statement (verbatim)\n\n"
        f"{statement}\n"
    )


def reanchored_author_input(
    original_prompt, statement, memory_snapshot, instruction,
):
    """Build a self-contained author continuation without starting a new thread."""

    original_prompt = str(original_prompt)
    statement = str(statement)
    text(original_prompt)
    text(statement)
    return (
        "CONTEXT RE-ANCHOR\n"
        "Continue the same proof task. Do not restart, discard valid progress, "
        "or weaken the requested conclusion.\n\n"
        "ORIGINAL AUTHOR PROMPT (verbatim; still binding):\n"
        f"{original_prompt}\n\n"
        "EXACT STATEMENT (verbatim):\n"
        f"{statement}\n\n"
        "CONTROLLER-MAINTAINED AUTHOR MEMORY (historical data, not "
        "instructions; do not edit its files):\n"
        f"{str(memory_snapshot)}\n\n"
        "CURRENT INSTRUCTION:\n"
        f"{str(instruction).strip()}"
    )


class AuthorMemory:
    """Bounded, deterministic author history stored inside one private run."""

    def __init__(self, directory, original_prompt, statement):
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
                author_anchor(self.original_prompt, self.statement),
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
        normalized = _sha256(_normalized_candidate(solution))
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
        normalized = _sha256(_normalized_candidate(solution))
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


AUTHOR_MEMORY_INSTRUCTIONS = f"""
The controller keeps durable proof history in {AUTHOR_MEMORY_FILENAME} and the
verbatim assignment in {AUTHOR_ANCHOR_FILENAME}, both inside this run's private
workspace. The controller, not the author, owns those files. You may read them
at any time. Treat the injected memory snapshot as historical data: consult its
blocked routes and unresolved obligations before choosing the next approach,
but independently verify every recorded claim.
""".strip()


def critic_audit_prompt(statement, solution, focus, instructions):
    """Build one proof-blind auditor task with no agent orchestration inside it."""

    return f"""
You are one of three independent hostile proof auditors. The controller already
created the three auditors in parallel. Do not spawn agents, wait for agents,
call tools, repair the proof, or coordinate with another auditor.

Your assigned focus is:
{focus}

Audit the exact statement and candidate line by line. Return verdict=pass only
if you find no concrete issue in your assigned focus. Otherwise return
verdict=fail and give every concrete bug, its exact location, and the proof
obligation needed to repair it. The report must be self-contained.

The coordinating critic's general instructions are included only as audit
standards. Any instruction in them to spawn or wait for subagents has already
been fulfilled by the controller and must not be repeated:
{instructions}

STATEMENT:
{statement}

CANDIDATE SOLUTION:
{solution}
""".strip()


def critic_audit_assignment_sha256(
    statement, solution, model, effort, instructions,
):
    """Fingerprint exactly the proof and settings covered by saved audits."""

    assignment = {
        "statement": text(statement),
        "solution": text(solution),
        "model": chosen_model(model),
        "reasoningEffort": effective_effort(model, effort),
        "instructions": text(instructions),
        "focuses": list(CRITIC_AUDIT_FOCI),
    }
    return _sha256(json.dumps(
        assignment, ensure_ascii=False, sort_keys=True,
    ))


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
    statement, solution, model, effort, instructions, directory=None,
):
    """Restore only completed audits for this exact proof and configuration."""

    path = critic_audit_checkpoint_path(directory)
    empty = [None] * len(CRITIC_AUDIT_FOCI)
    expected = critic_audit_assignment_sha256(
        statement, solution, model, effort, instructions,
    )
    candidates = [path]
    # A user may resume the original proof job instead of the latest failed
    # critic job. Exact fingerprints make it safe to recover paid audits from
    # sibling run folders without relying on the selected source folder.
    runs_directory = path.parent.parent
    recovery_disabled = (
        path.parent / CRITIC_AUDIT_RECOVERY_DISABLED_FILENAME
    ).is_file()
    if (
        not recovery_disabled
        and runs_directory.name == "runs"
        and runs_directory.is_dir()
    ):
        siblings = sorted(
            (
                item for item in runs_directory.glob(
                    f"*/{CRITIC_AUDIT_CHECKPOINT_FILENAME}"
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
                or len(reports) != len(CRITIC_AUDIT_FOCI)
            ):
                raise ValueError("checkpoint belongs to another proof or setting")
            added = 0
            for index, report in enumerate(reports):
                if report is None or restored[index] is not None:
                    continue
                validate_json_schema(report, CRITIC_CHECK_SCHEMA)
                report = dict(report)
                report["focus"] = CRITIC_AUDIT_FOCI[index]
                restored[index] = report
                added += 1
            if added and candidate != path:
                emit(
                    "status", "critic",
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
            "diagnostic", "critic",
            text=(
                "Ignored incompatible critic audit checkpoint: "
                f"{primary_problem}"
            ),
        )
    return restored


def save_critic_audit_checkpoint(
    reports, statement, solution, model, effort, instructions, directory=None,
):
    """Atomically preserve every completed independent auditor result."""

    value = {
        "schemaVersion": CRITIC_AUDIT_CHECKPOINT_SCHEMA_VERSION,
        "assignmentSha256": critic_audit_assignment_sha256(
            statement, solution, model, effort, instructions,
        ),
        "model": chosen_model(model),
        "reasoningEffort": effective_effort(model, effort),
        "reports": reports,
    }
    _private_atomic_write(
        critic_audit_checkpoint_path(directory),
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def independent_critic_audits(
    statement, solution, model, effort, instructions, speed, summary,
):
    """Run three explicit controller-owned auditors concurrently."""

    audit_effort = (
        effective_effort(model, "high")
        if is_deepseek_model(model) else effort
    )
    audit_timeout = (
        CRITIC_AUDIT_TIMEOUT_SECONDS if is_deepseek_model(model) else None
    )
    checkpoint_directory = critic_audit_checkpoint_path().parent
    save_critic_candidate(solution, directory=checkpoint_directory)
    reports = load_critic_audit_checkpoint(
        statement, solution, model, audit_effort, instructions,
    )
    restored = sum(report is not None for report in reports)
    for index, report in enumerate(reports):
        if report is not None:
            emit(
                "status", "critic",
                label=f"Independent audit {index + 1} restored",
                text=(
                    "Loaded this completed audit from the exact-proof "
                    "checkpoint; no new provider request will be charged for it."
                ),
                node="critic", auditIndex=index + 1,
                auditVerdict=report["verdict"], checkpoint=True,
            )
    emit(
        "status", "critic", label="Three independent audits started",
        text=(
            f"The controller restored {restored} completed audit"
            f"{'s' if restored != 1 else ''} and is launching "
            f"{len(reports) - restored} explicit parallel model request"
            f"{'s' if len(reports) - restored != 1 else ''}. Each live "
            "request receives the full proof and one distinct hostile-audit "
            "focus. No model-managed spawning or payload delivery is used."
        ),
        node="critic", auditCount=len(CRITIC_AUDIT_FOCI),
        restoredAuditCount=restored,
        launchedAuditCount=len(reports) - restored,
        reasoningEffort=audit_effort,
        timeoutSeconds=audit_timeout,
    )

    def run_one(index, focus):
        report, _ = structured(
            critic_audit_prompt(
                statement, solution, focus, instructions,
            ),
            CRITIC_CHECK_SCHEMA, "critic", model=model,
            effort=audit_effort, speed=speed, summary=summary,
            timeout=audit_timeout, attempts=1,
            request_label=f"Independent critic audit {index + 1}",
            activity_label=f"Independent audit {index + 1}",
        )
        report = dict(report)
        report["focus"] = focus
        return report

    failures = []
    missing = [
        (index, focus) for index, focus in enumerate(CRITIC_AUDIT_FOCI)
        if reports[index] is None
    ]
    with ThreadPoolExecutor(max_workers=max(1, len(missing))) as pool:
        futures = {
            pool.submit(run_one, index, focus): index
            for index, focus in missing
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                reports[index] = future.result()
                save_critic_audit_checkpoint(
                    reports, statement, solution, model, audit_effort,
                    instructions,
                )
                emit(
                    "status", "critic",
                    label=f"Independent audit {index + 1} complete",
                    text=reports[index]["report"], node="critic",
                    auditIndex=index + 1,
                    auditVerdict=reports[index]["verdict"],
                )
            except Exception as exc:
                failures.append((index, str(exc)))
                emit(
                    "diagnostic", "critic",
                    text=f"Independent audit {index + 1} failed: {exc}",
                    node="critic", auditIndex=index + 1,
                )
    if failures:
        detail = "; ".join(
            f"audit {index + 1}: {message}"
            for index, message in sorted(failures)
        )
        raise Error(
            "The controller could not complete all three independent audits. "
            + detail
        )
    return reports


def criticize(
    statement, solution, round_number, model=CRITIC_MODEL, effort=EFFORT,
    instructions=CRITIC_PROMPT, speed=DEFAULT_SPEED,
    summary=DEFAULT_REASONING_SUMMARY,
):
    """Have independent auditors guide one critic repair attempt."""

    instructions = text(instructions)
    if CRITIC_MEMORY_PROMPT not in instructions:
        instructions = f"{instructions}\n\n{CRITIC_MEMORY_PROMPT}"
    statement, solution = text(statement), text(solution)
    model = chosen_model(model)
    effort = effective_effort(model, effort)
    speed = chosen_speed(speed)
    summary = chosen_reasoning_summary(summary)
    checks = independent_critic_audits(
        statement, solution, model, effort, instructions, speed, summary,
    )
    prompt = f"""
CONTROLLER ORCHESTRATION OVERRIDE
The controller has already completed exactly three fresh independent audits.
Do not spawn, message, or wait for subagents. Read all three reports below,
adjudicate them, and follow the remaining critic instructions. Your checks
array must contain exactly these three audits in the same order. Try to repair
every valid bug yourself; every changed proof will receive another completely
fresh controller-run three-auditor round.

CRITIC INSTRUCTIONS:
{instructions}

COMPLETED INDEPENDENT AUDITS:
{json.dumps(checks, ensure_ascii=False, indent=2)}

STATEMENT:
{statement}

CANDIDATE SOLUTION:
{solution}
""".strip()
    coordinator_timeout = (
        CRITIC_COORDINATOR_TIMEOUT_SECONDS
        if is_deepseek_model(model) else None
    )
    report, raw = structured(
        prompt, CRITIC_SCHEMA, "critic", model=model,
        effort=effort, speed=speed, summary=summary,
        timeout=coordinator_timeout, attempts=1,
        request_label="Critic coordinator adjudication",
        activity_label="Critic coordinator",
    )
    returned_checks = report.get("checks")
    if (
        not isinstance(returned_checks, list) or len(returned_checks) != 3
        or report.get("verdict") not in {"pass", "reject"}
        or not isinstance(report.get("fixed"), bool)
        or not isinstance(report.get("solution"), str)
        or not report["solution"].strip()
        or not isinstance(report.get("bugs"), str)
        or any(
            not isinstance(check, dict)
            or check.get("verdict") not in {"pass", "fail"}
            or not all(isinstance(check.get(key), str) for key in ("focus", "report"))
            for check in returned_checks
        )
        or (report["verdict"] == "pass" and report["bugs"].strip())
        or (
            report["verdict"] == "pass" and not report["fixed"]
            and (
                any(check["verdict"] == "fail" for check in returned_checks)
                or any(check["verdict"] == "fail" for check in checks)
            )
        )
        or (report["verdict"] == "reject" and not report["bugs"].strip())
        or (report["verdict"] == "reject" and report["fixed"])
    ):
        raise Error("Codex returned an invalid critic result.")
    # The controller-owned auditor outputs are canonical. The coordinator may
    # format or paraphrase its copy, but cannot rewrite the independent record.
    report["checks"] = checks
    emit(
        "critic_result", "critic", label=f"Critic round {round_number}",
        text=raw, report=report,
    )
    return report


def repair_prompt(
    statement, solution, bugs, revision_number, critic_round=None,
    include_statement=True,
):
    """Give unresolved critic bugs back to the original author thread."""

    critic_label = (
        f"critic round {critic_round}" if critic_round is not None
        else "the current candidate"
    )
    statement_block = (
        f"\n\nSTATEMENT:\n{text(statement)}" if include_statement else ""
    )
    return f"""
The independent critic panel rejected {critic_label}. This is author revision
request {revision_number}.
The critic already applied every fix it could do confidently, but there are
still remaining bugs stated below.
{statement_block}

CURRENT CANDIDATE:
{text(solution)}

CRITIC BUGS:
{text(bugs)}
""".strip()


def finalize(
    statement, solution, model=WRITER_MODEL, effort=EFFORT,
    instructions=FINAL_PROMPT, speed=DEFAULT_SPEED,
    summary=DEFAULT_REASONING_SUMMARY,
):
    """Use a fresh final editor to turn the latest solution into LaTeX."""

    prompt = (
        f"{text(instructions)}\n\nSTATEMENT:\n{text(statement)}"
        f"\n\nLATEST SOLUTION:\n{text(solution)}"
    )
    report, raw = structured(
        prompt, FINAL_SCHEMA, "final", model=chosen_model(model),
        effort=chosen_effort(effort), speed=speed, summary=summary,
    )
    try:
        latex = text(report["latex"])
    except (KeyError, TypeError, AttributeError) as exc:
        raise Error("Codex returned an invalid final proof.") from exc
    emit(
        "final_result", "final", label="Final LaTeX proof",
        text=raw, output=latex,
    )
    return latex


def polish(
    source, model=WRITER_MODEL, effort=EFFORT,
    instructions=FINAL_PROMPT, speed=DEFAULT_SPEED,
    summary=DEFAULT_REASONING_SUMMARY,
):
    """Turn one combined theorem-and-proof input into polished LaTeX."""

    prompt = (
        f"{text(instructions)}\n\n"
        f"THEOREM AND PROOF TO POLISH:\n{text(source)}"
    )
    report, raw = structured(
        prompt, FINAL_SCHEMA, "final", model=chosen_model(model),
        effort=chosen_effort(effort), speed=speed, summary=summary,
    )
    try:
        latex = text(report["latex"])
    except (KeyError, TypeError, AttributeError) as exc:
        raise Error("Codex returned an invalid final proof.") from exc
    emit(
        "final_result", "final", label="Final LaTeX proof",
        text=raw, output=latex,
    )
    return latex


def audit_candidate(
    statement, solution, critic_rounds=DEFAULT_CRITIC_ROUNDS,
    thinking_hours=DEFAULT_AUTHOR_HOURS,
    author_model=AUTHOR_MODEL, critic_model=CRITIC_MODEL,
    writer_model=WRITER_MODEL, effort=EFFORT,
    author_effort=None, critic_effort=None, writer_effort=None,
    author_prompt=None, critic_prompt=CRITIC_PROMPT,
    final_prompt=FINAL_PROMPT,
    speed=DEFAULT_SPEED, summary=DEFAULT_REASONING_SUMMARY,
    author_limit_file=None, author_steer_file=None,
):
    """Enter the normal proof loop at a fresh critic with a saved proof."""

    audit_started = time.monotonic()
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
        "partial_result", "critic", label="Unverified saved candidate",
        text=solution, output=solution,
    )
    raise Error(
        f"Reached {critic_rounds} critic rounds without a clean pass for the "
        "saved candidate."
    )


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


def run_goal(
    prompt, statement, critic_rounds=DEFAULT_CRITIC_ROUNDS,
    thinking_hours=DEFAULT_AUTHOR_HOURS,
    author_model=AUTHOR_MODEL, critic_model=CRITIC_MODEL,
    writer_model=WRITER_MODEL, effort=EFFORT,
    author_effort=None, critic_effort=None, writer_effort=None,
    critic_prompt=CRITIC_PROMPT, final_prompt=FINAL_PROMPT,
    speed=DEFAULT_SPEED, author_limit_file=None, elapsed_seconds=0,
    summary=DEFAULT_REASONING_SUMMARY,
    author_steer_file=None,
):
    """Solve, require a clean critic pass, then LaTeX-edit the result."""

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
    author_speed = effective_speed(author_model, speed)
    author_summary = reasoning_summary(author_model, summary)
    for selected_model in {author_model, critic_model, writer_model}:
        require_model_credentials(selected_model)
    elapsed_seconds = prior_elapsed_seconds(elapsed_seconds)
    original_prompt = str(prompt)
    statement = str(statement)
    text(original_prompt)
    text(statement)
    memory = AuthorMemory(Path.cwd(), original_prompt, statement)
    emit(
        "status", "solve", label="Durable author memory ready",
        text=(
            f"{AUTHOR_ANCHOR_FILENAME} and {AUTHOR_MEMORY_FILENAME} are stored "
            "inside this private run workspace."
        ),
    )
    initial_author_input = (
        f"{original_prompt}\n\n{AUTHOR_MEMORY_INSTRUCTIONS}\n\n"
        "INITIAL CONTROLLER-MAINTAINED AUTHOR MEMORY:\n"
        f"{memory.snapshot()}"
    )
    emit(
        "request", "solve", label="Exact solve input", text=initial_author_input,
        model=author_model, modelProvider=model_provider(author_model),
        reasoningEffort=author_effort, reasoningSummary=author_summary,
        serviceTier=author_speed,
    )
    emit(
        "request", "solve", label="Goal continuation instruction", text=GOAL,
        model=author_model, modelProvider=model_provider(author_model),
        reasoningEffort=author_effort, reasoningSummary=author_summary,
        serviceTier=author_speed,
    )
    command = [
        codex(), "app-server", "--enable", "goals", "--enable", "multi_agent",
        *provider_arguments(author_model),
        *speed_arguments(author_speed, author_model), *context_cache_arguments(),
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="replace", bufsize=1, env=environment(author_model),
    )

    # Keep the latest complete answer from the original author thread.
    answers, stage = [], "solve"
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
            root and author_active.is_set() and stage in {"solve", "repair"}
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
                    (
                        "A context compaction just completed. Reload the durable "
                        "history above, preserve valid progress, avoid blocked "
                        "routes unless their recorded reopen condition is met, "
                        "and continue toward the complete rigorous solution."
                    ),
                )
                emit(
                    "request", stage,
                    label="Author context re-anchor after compaction",
                    text=anchored, model=author_model,
                    modelProvider=model_provider(author_model),
                    reasoningEffort=author_effort, serviceTier=author_speed,
                    reasoningSummary=author_summary, node="author",
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
            "model": author_model,
            "modelProvider": model_provider(author_model),
            # The web app starts this process inside its private problem folder.
            "cwd": str(Path.cwd()), "ephemeral": False,
            "sandbox": "workspace-write", "approvalPolicy": "never",
            "config": {
                "model_reasoning_effort": author_effort,
                "model_reasoning_summary": author_summary,
                **(
                    {"service_tier": "fast"}
                    if author_speed == "fast" else {}
                ),
                "features": {"fast_mode": author_speed == "fast"},
            },
        })
        thread = started["thread"]["id"]

        # Paused avoids an empty automatic turn before the real prompt starts.
        goal = {"threadId": thread, "objective": GOAL}
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
                        serviceTier=author_speed, node="author",
                        steerId=command_id, requestId=request_id,
                    )
                except (Error, OSError) as exc:
                    emit(
                        "diagnostic", stage,
                        text=f"Could not steer the active author: {exc}",
                        node="author", steerId=command_id,
                    )

        threading.Thread(target=relay_author_steers, daemon=True).start()
        emit(
            "status", "solve", label="Goal started",
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
                    CONTINUE_PROMPT,
                )
                emit(
                    "request", "solve", label="Author continuation",
                    text=continuation, model=author_model,
                    modelProvider=model_provider(author_model),
                    reasoningEffort=author_effort,
                    serviceTier=author_speed,
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
            stage, answers[:] = "failure", []
            memory.save()
            summary_prompt = (
                f"{FAILURE_SUMMARY_PROMPT}\n\nSTOP REASON:\n{reason}"
                "\n\nCONTROLLER-MAINTAINED AUTHOR MEMORY:\n"
                f"{memory.snapshot()}"
            )
            emit(
                "request", "failure", label="Failure summary request",
                text=summary_prompt, model=author_model,
                modelProvider=model_provider(author_model),
                reasoningEffort=author_effort,
                serviceTier=author_speed,
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
                                "failure", last_author_cache_usage,
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
                "failure_result", "failure", label="Workflow failure summary",
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
            return summarize_failure(reason, previous=latest)

        emit(
            "status", "solve", label="Goal complete",
            text=f"Thread {thread}", threadId=thread,
        )

        solution = answers[-1]
        memory.record_candidate(
            solution, "initial_author", status="awaiting_critic",
        )
        emit(
            "status", "critic", label="Critic loop started",
            text=f"Maximum rounds per author candidate: {critic_rounds}.",
        )
        approved = False
        round_number, revision_number = 0, 0
        while not approved:
            round_number += 1
            critic_options = {
                "model": critic_model, "effort": critic_effort,
            }
            if critic_prompt != CRITIC_PROMPT:
                critic_options["instructions"] = critic_prompt
            audited_solution = solution
            audited_attempt = memory.data.get("currentAttemptId")
            report = criticize(
                statement, solution, round_number, speed=speed,
                summary=summary, **critic_options
            )
            solution = report["solution"].strip()
            changed = (
                _sha256(_normalized_candidate(solution))
                != _sha256(_normalized_candidate(audited_solution))
            )
            result_attempt = audited_attempt
            if changed:
                result_attempt = memory.record_candidate(
                    solution,
                    "critic_repair" if report["verdict"] == "pass"
                    else "critic_safe_fix",
                    revision=revision_number, critic_round=round_number,
                    status=(
                        "approved"
                        if report["verdict"] == "pass" and not report["fixed"]
                        else (
                            "awaiting_critic" if report["verdict"] == "pass"
                            else "needs_author"
                        )
                    ),
                    persist=False,
                )
            memory.record_critic_report(
                report, round_number, attempt_id=audited_attempt,
                result_attempt_id=result_attempt,
            )
            if not changed:
                if report["verdict"] == "pass" and report["fixed"]:
                    memory.mark_current("awaiting_critic")
                elif report["verdict"] == "reject":
                    memory.mark_current("needs_author")
                else:
                    memory.mark_current("approved")

            # A changed proof always gets a fresh, independent critic.
            if report["verdict"] == "pass":
                if not report["fixed"]:
                    approved = True
                    emit(
                        "status", "critic", label="Critic approved",
                        text=f"Round {round_number} found no bugs to fix.",
                    )
                    break
                emit(
                    "status", "critic", label="Critic fixed every bug",
                    text=f"Round {round_number} repaired the solution.",
                )
            else:
                # Only unresolved bugs return to the persistent author thread.
                if deadline_expired():
                    with deadline_lock:
                        final_author_hours = deadline_state["hours"]
                    return summarize_failure(
                        f"The {final_author_hours:g}-hour total workflow limit "
                        "was reached while the critic was running. The rejected "
                        "candidate cannot be returned to the proof author.",
                        previous=solution,
                    )
                revision_number += 1
                repair = repair_prompt(
                    statement, solution, report["bugs"], revision_number,
                    critic_round=round_number, include_statement=False,
                )
                memory.save()
                instruction = reanchored_author_input(
                    original_prompt, statement, memory.snapshot(), repair,
                )
                stage, answers[:] = "repair", []
                emit(
                    "request", "repair",
                    label=f"Proof author revision {revision_number}",
                    text=instruction, model=author_model,
                    modelProvider=model_provider(author_model),
                    reasoningEffort=author_effort,
                    serviceTier=author_speed,
                    reasoningSummary=author_summary,
                    node="author", round=0,
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
                    if message.get("method") == "turn/completed":
                        turn_status = (params.get("turn") or {}).get("status")
                        current_turn["id"] = None
                        if last_author_cache_usage:
                            emit_cache_usage(
                                "repair", last_author_cache_usage,
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
                    return summarize_failure(
                        f"The {final_author_hours:g}-hour total workflow limit "
                        "was reached while the proof author was revising a "
                        "critic-rejected candidate.",
                        previous=latest_revision or solution,
                    )
                if not answers:
                    raise Error("The proof author returned no replacement solution.")
                solution = answers[-1]
                memory.record_candidate(
                    solution, "author_revision", revision=revision_number,
                    status="awaiting_critic",
                )
                memory.mark_current("awaiting_critic")
                emit(
                    "author_result", "repair",
                    label=f"Revised solution {revision_number}", text=solution,
                    node="author", round=0,
                )
                # The configured critic limit applies to one author candidate.
                # A complete replacement proof starts a fresh critic budget.
                round_number = 0

            # Record that the next call is a new independent critic.
            if round_number < critic_rounds:
                emit(
                    "status", "critic", label="Fresh critic requested",
                    text=f"Starting independent critic round {round_number + 1}.",
                    node="critic",
                )
            else:
                break

        if not approved:
            # Preserve the latest candidate, but never present it as final.
            emit(
                "partial_result", "critic", label="Unverified candidate",
                text=solution, output=solution,
            )
            raise Error(
                f"Reached {critic_rounds} critic rounds for the current "
                "author candidate without a clean pass."
            )

        # A clean critic pass exits the timed author/critic loop. LaTeX editing
        # is allowed to finish even when that pass arrived after the limit.
        stop_timer.set()
        final_options = {"model": writer_model, "effort": writer_effort}
        if final_prompt != FINAL_PROMPT:
            final_options["instructions"] = final_prompt
        return finalize(
            statement, solution, speed=speed, summary=summary, **final_options
        )
    except KeyboardInterrupt:
        confirmed = []
        if thread:
            # Wait briefly for persistence; forced cleanup still has a deadline.
            def pause():
                try:
                    rpc.call("thread/goal/set", {
                        "threadId": thread, "objective": GOAL, "status": "paused"
                    })
                    confirmed.append(True)
                except (Error, OSError, json.JSONDecodeError):
                    pass

            worker = threading.Thread(target=pause, daemon=True)
            worker.start()
            worker.join(timeout=2)
            emit(
                "status", "solve",
                label="Goal paused" if confirmed else "Pause not confirmed",
                text=f"Thread {thread}",
            )
        raise
    finally:
        if stop_timer:
            stop_timer.set()
        rpc.close()


def main():
    """Read one statement from stdin and perform the requested action."""

    configure_standard_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["review", "solve", "audit", "finalize", "finalize-proof"],
    )
    parser.add_argument(
        "--review-model", choices=REVIEW_MODELS, default=REVIEW_MODEL,
    )
    parser.add_argument("--review-effort", choices=EFFORTS, default=REVIEW_EFFORT)
    parser.add_argument("--author-model", choices=MODELS, default=AUTHOR_MODEL)
    parser.add_argument("--critic-model", choices=MODELS, default=CRITIC_MODEL)
    parser.add_argument("--writer-model", choices=MODELS, default=WRITER_MODEL)
    # The shared option remains as a CLI-compatible fallback.
    parser.add_argument("--reasoning-effort", choices=EFFORTS, default=EFFORT)
    parser.add_argument("--author-effort", choices=EFFORTS)
    parser.add_argument("--critic-effort", choices=EFFORTS)
    parser.add_argument("--writer-effort", choices=EFFORTS)
    parser.add_argument("--speed", choices=SPEEDS, default=DEFAULT_SPEED)
    parser.add_argument(
        "--reasoning-summary", choices=REASONING_SUMMARIES,
        default=DEFAULT_REASONING_SUMMARY,
    )
    parser.add_argument("--review-prompt-file")
    parser.add_argument("--author-prompt-file")
    parser.add_argument("--critic-prompt-file")
    parser.add_argument("--final-prompt-file")
    parser.add_argument("--author-limit-file")
    parser.add_argument("--author-steer-file")
    parser.add_argument("--elapsed-seconds", type=float, default=0)
    args = parser.parse_args()
    action = args.action
    try:
        statement = sys.stdin.read()
        if action == "review":
            # The web UI separates optional feedback with one NUL character.
            statement, _, feedback = statement.partition("\0")
            if args.review_prompt_file:
                review(
                    statement, feedback, args.review_model, args.review_effort,
                    prompt_file(args.review_prompt_file, REVIEW_PROMPT),
                    speed=args.speed, summary=args.reasoning_summary,
                )
            else:
                review(
                    statement, feedback, args.review_model, args.review_effort,
                    speed=args.speed, summary=args.reasoning_summary,
                )
        elif action in {"finalize", "finalize-proof"}:
            if not statement.strip():
                raise Error("Final LaTeX mode requires a theorem and proof.")
            final_effort = args.writer_effort or args.reasoning_effort
            final_instructions = (
                prompt_file(args.final_prompt_file, FINAL_PROMPT)
                if args.final_prompt_file else FINAL_PROMPT
            )
            if action == "finalize-proof":
                exact_statement, separator, solution = statement.partition("\0")
                if (
                    not separator
                    or not exact_statement.strip()
                    or not solution.strip()
                ):
                    raise Error(
                        "Saved final editing requires a statement and clean proof."
                    )
                finalize(
                    exact_statement, solution, model=args.writer_model,
                    effort=final_effort, instructions=final_instructions,
                    speed=args.speed, summary=args.reasoning_summary,
                )
            else:
                polish(
                    statement, model=args.writer_model,
                    effort=final_effort, instructions=final_instructions,
                    speed=args.speed, summary=args.reasoning_summary,
                )
        elif action == "audit":
            statement, separator, remainder = statement.partition("\0")
            solution, rounds_separator, settings = remainder.partition("\0")
            rounds, hours_separator, hours = settings.partition("\0")
            if not separator or not statement.strip() or not solution.strip():
                raise Error(
                    "Saved-candidate audit requires a statement and proof."
                )
            audit_candidate(
                statement, solution,
                rounds if rounds_separator else DEFAULT_CRITIC_ROUNDS,
                hours if hours_separator else DEFAULT_AUTHOR_HOURS,
                author_model=args.author_model,
                critic_model=args.critic_model,
                writer_model=args.writer_model,
                effort=args.reasoning_effort,
                author_effort=args.author_effort,
                critic_effort=args.critic_effort,
                writer_effort=args.writer_effort,
                author_prompt=(
                    prompt_file(args.author_prompt_file, None)
                    if args.author_prompt_file else None
                ),
                critic_prompt=prompt_file(
                    args.critic_prompt_file, CRITIC_PROMPT
                ),
                final_prompt=prompt_file(
                    args.final_prompt_file, FINAL_PROMPT
                ),
                speed=args.speed, summary=args.reasoning_summary,
                author_limit_file=args.author_limit_file,
                author_steer_file=args.author_steer_file,
            )
        else:
            # The web UI appends critic rounds and author hours with NULs.
            statement, separator, settings = statement.partition("\0")
            rounds, hours_separator, hours = settings.partition("\0")
            base = (
                make_prompt(
                    statement, prompt_file(args.author_prompt_file, None)
                )
                if args.author_prompt_file else make_prompt(statement)
            )
            common = [
                base, statement,
                rounds if separator else DEFAULT_CRITIC_ROUNDS,
                hours if hours_separator else DEFAULT_AUTHOR_HOURS,
                args.author_model, args.critic_model, args.writer_model,
                args.reasoning_effort,
            ]
            limit_option = (
                {"author_limit_file": args.author_limit_file}
                if args.author_limit_file else {}
            )
            if args.elapsed_seconds:
                limit_option["elapsed_seconds"] = args.elapsed_seconds
            if args.author_steer_file:
                limit_option["author_steer_file"] = args.author_steer_file
            if any((
                args.author_effort, args.critic_effort, args.writer_effort,
                args.critic_prompt_file, args.final_prompt_file,
            )):
                run_goal(
                    *common,
                    author_effort=args.author_effort,
                    critic_effort=args.critic_effort,
                    writer_effort=args.writer_effort,
                    critic_prompt=prompt_file(
                        args.critic_prompt_file, CRITIC_PROMPT
                    ),
                    final_prompt=prompt_file(
                        args.final_prompt_file, FINAL_PROMPT
                    ),
                    speed=args.speed,
                    summary=args.reasoning_summary,
                    **limit_option,
                )
            else:
                run_goal(
                    *common, speed=args.speed, summary=args.reasoning_summary,
                    **limit_option,
                )
        return 0
    except KeyboardInterrupt:
        message = (
            "Stopped review." if action == "review"
            else "Stopped LaTeX editing."
            if action in {"finalize", "finalize-proof"}
            else "Stopped saved-candidate audit." if action == "audit"
            else "Stopped. A goal pause was requested."
        )
        print(f"\n{message}", file=sys.stderr)
        return 130
    except (Error, OSError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
