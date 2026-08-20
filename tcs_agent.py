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
from pathlib import Path


# Every role defaults to the strongest model and reasoning setting.
ROOT = Path(__file__).resolve().parent
MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
MODEL, EFFORT = "gpt-5.6-sol", "ultra"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
SPEEDS, DEFAULT_SPEED = ("standard", "fast"), "fast"
# Kept as the default-tier name for older callers and trace consumers.
SERVICE_TIER = DEFAULT_SPEED
AUTHOR_MODEL = CRITIC_MODEL = WRITER_MODEL = MODEL

# Every role defaults to Sol and Ultra; both choices are configurable.
REVIEW_MODEL = MODEL
REVIEW_MODELS = MODELS
REVIEW_EFFORT = EFFORT

# These values connect the approved statement to the user's prompt template.
MARKER, TEMPLATE = "[STATEMENT]", ROOT / "prompt.txt"
GOAL = "Complete the task supplied in the first turn and continue until done."
DEFAULT_CRITIC_ROUNDS, MAX_CRITIC_ROUNDS = 100, 100
MAX_AUTHOR_HOURS = 168
DEFAULT_AUTHOR_HOURS = MAX_AUTHOR_HOURS
AUTHOR_LIMIT_POLL_SECONDS = 0.25
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
Act as a coordinating proof critic. Spawn fresh subagents in
parallel and give each only the statement and candidate solution below. Keep
their work independent and wait for all three. Make sure they are hostile
line-by-line proof audits.

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


def chosen_model(value):
    """Require one supported GPT-5.6 model."""

    if value not in MODELS:
        raise Error("Choose Sol, Terra, or Luna.")
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


def speed_arguments(speed):
    """Return explicit Codex flags for one selected speed mode."""

    speed = chosen_speed(speed)
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


def environment():
    """Use the saved ChatGPT login with quiet, predictable child logging."""

    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
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

    print(json.dumps({"kind": kind, "stage": stage, **fields}, ensure_ascii=False), flush=True)


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


def structured(
    prompt, schema_value, stage, model=MODEL, effort=EFFORT,
    speed=DEFAULT_SPEED,
):
    """Run one read-only structured Codex call and relay its visible events."""

    emit(
        "request", stage, label=f"Exact {stage} input", text=prompt,
        model=model, reasoningEffort=effort, reasoningSummary="detailed",
        serviceTier=chosen_speed(speed),
        responseSchema=schema_value,
    )
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        workspace = structured_workspace()
        schema = folder / "schema.json"
        answer = folder / "answer.json"
        schema.write_text(json.dumps(schema_value), encoding="utf-8")
        command = [
            codex(), "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            *speed_arguments(speed), *context_cache_arguments(),
            "-c", 'model_reasoning_summary="detailed"',
            *(["--enable", "multi_agent"] if stage == "critic" else []),
            "-C", str(workspace), "-s", "read-only", "-a", "never", "exec",
            "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
            "--output-schema", str(schema), "-o", str(answer), "-",
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", env=environment(),
        )
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                try:
                    raw_event = json.loads(line)
                    event = public_event(raw_event)
                    if event is not None:
                        emit("codex_event", stage, event=event)
                    if raw_event.get("type") == "turn.completed":
                        emit_cache_usage(stage, raw_event.get("usage"))
                except json.JSONDecodeError:
                    emit("diagnostic", stage, text="Codex returned a malformed event.")
            code = process.wait()
        finally:
            stop_process(process)
        if code:
            raise Error(f"Codex {stage} failed.")
        raw = answer.read_text(encoding="utf-8")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise Error(f"Codex returned an invalid {stage} result.")
        return result, raw


def review(
    draft, feedback="", model=REVIEW_MODEL, effort=REVIEW_EFFORT,
    instructions=REVIEW_PROMPT, speed=DEFAULT_SPEED,
):
    """Review with the user's chosen model."""

    try:
        chosen_model(model)
        effort = chosen_effort(effort)
        report, raw = structured(
            review_prompt(draft, feedback, instructions), SCHEMA, "review",
            model=model, effort=effort, speed=speed,
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


def criticize(
    statement, solution, round_number, model=CRITIC_MODEL, effort=EFFORT,
    instructions=CRITIC_PROMPT, speed=DEFAULT_SPEED,
):
    """Have independent auditors guide one critic repair attempt."""

    instructions = text(instructions)
    if CRITIC_MEMORY_PROMPT not in instructions:
        instructions = f"{instructions}\n\n{CRITIC_MEMORY_PROMPT}"
    prompt = (
        f"{instructions}\n\nSTATEMENT:\n{text(statement)}"
        f"\n\nCANDIDATE SOLUTION:\n{text(solution)}"
    )
    report, raw = structured(
        prompt, CRITIC_SCHEMA, "critic", model=chosen_model(model),
        effort=chosen_effort(effort), speed=speed,
    )
    checks = report.get("checks")
    if (
        not isinstance(checks, list) or len(checks) != 3
        or report.get("verdict") not in {"pass", "reject"}
        or not isinstance(report.get("fixed"), bool)
        or not isinstance(report.get("solution"), str)
        or not report["solution"].strip()
        or not isinstance(report.get("bugs"), str)
        or any(
            not isinstance(check, dict)
            or check.get("verdict") not in {"pass", "fail"}
            or not all(isinstance(check.get(key), str) for key in ("focus", "report"))
            for check in checks
        )
        or (report["verdict"] == "pass" and report["bugs"].strip())
        or (
            report["verdict"] == "pass" and not report["fixed"]
            and any(check["verdict"] == "fail" for check in checks)
        )
        or (report["verdict"] == "reject" and not report["bugs"].strip())
        or (report["verdict"] == "reject" and report["fixed"])
    ):
        raise Error("Codex returned an invalid critic result.")
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
):
    """Use a fresh final editor to turn the latest solution into LaTeX."""

    prompt = (
        f"{text(instructions)}\n\nSTATEMENT:\n{text(statement)}"
        f"\n\nLATEST SOLUTION:\n{text(solution)}"
    )
    report, raw = structured(
        prompt, FINAL_SCHEMA, "final", model=chosen_model(model),
        effort=chosen_effort(effort), speed=speed,
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
):
    """Turn one combined theorem-and-proof input into polished LaTeX."""

    prompt = (
        f"{text(instructions)}\n\n"
        f"THEOREM AND PROOF TO POLISH:\n{text(source)}"
    )
    report, raw = structured(
        prompt, FINAL_SCHEMA, "final", model=chosen_model(model),
        effort=chosen_effort(effort), speed=speed,
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
):
    """Solve, require a clean critic pass, then LaTeX-edit the result."""

    critic_rounds = critic_limit(critic_rounds)
    thinking_hours = controlled_author_hours(
        author_limit_file, author_hours(thinking_hours)
    )
    author_model = chosen_model(author_model)
    critic_model = chosen_model(critic_model)
    writer_model = chosen_model(writer_model)
    author_effort = chosen_effort(author_effort or effort)
    critic_effort = chosen_effort(critic_effort or effort)
    writer_effort = chosen_effort(writer_effort or effort)
    speed = chosen_speed(speed)
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
        model=author_model, reasoningEffort=author_effort, reasoningSummary="detailed",
        serviceTier=speed,
    )
    emit(
        "request", "solve", label="Goal continuation instruction", text=GOAL,
        model=author_model, reasoningEffort=author_effort, reasoningSummary="detailed",
        serviceTier=speed,
    )
    command = [
        codex(), "app-server", "--enable", "goals", "--enable", "multi_agent",
        *speed_arguments(speed), *context_cache_arguments(),
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="replace", bufsize=1, env=environment(),
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
                    reasoningEffort=author_effort, serviceTier=speed,
                    reasoningSummary="detailed", node="author",
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
            "model": author_model, "modelProvider": "openai",
            # The web app starts this process inside its private problem folder.
            "cwd": str(Path.cwd()), "ephemeral": False,
            "sandbox": "workspace-write", "approvalPolicy": "never",
            "config": {
                "model_reasoning_effort": author_effort,
                "model_reasoning_summary": "detailed",
                **({"service_tier": "fast"} if speed == "fast" else {}),
                "features": {"fast_mode": speed == "fast"},
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
            "summary": "detailed",
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
                    reasoningEffort=author_effort,
                    serviceTier=speed,
                    reasoningSummary="detailed",
                )
                # Explicit turns start while Goal mode is paused.
                rpc.call("thread/goal/set", {**goal, "status": "paused"})
                if timed_out.is_set():
                    break
                resumed = rpc.call("turn/start", {
                    "threadId": thread,
                    "input": [{"type": "text", "text": continuation}],
                    "summary": "detailed",
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
                reasoningEffort=author_effort,
                serviceTier=speed,
                reasoningSummary="detailed",
            )
            summary_stop = threading.Event()
            summary_expired = threading.Event()
            try:
                summary_turn = rpc.call("turn/start", {
                    "threadId": thread,
                    "input": [{"type": "text", "text": summary_prompt}],
                    "summary": "detailed",
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
                statement, solution, round_number, speed=speed, **critic_options
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
                    reasoningEffort=author_effort,
                    serviceTier=speed,
                    reasoningSummary="detailed",
                    node="author", round=0,
                )
                author_active.set()
                revision_turn = rpc.call("turn/start", {
                    "threadId": thread,
                    "input": [{"type": "text", "text": instruction}],
                    "summary": "detailed",
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
        return finalize(statement, solution, speed=speed, **final_options)
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
    parser.add_argument("action", choices=["review", "solve", "finalize"])
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
    parser.add_argument("--review-prompt-file")
    parser.add_argument("--author-prompt-file")
    parser.add_argument("--critic-prompt-file")
    parser.add_argument("--final-prompt-file")
    parser.add_argument("--author-limit-file")
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
                    speed=args.speed,
                )
            else:
                review(
                    statement, feedback, args.review_model, args.review_effort,
                    speed=args.speed,
                )
        elif action == "finalize":
            if not statement.strip():
                raise Error("Final LaTeX mode requires a theorem and proof.")
            final_effort = args.writer_effort or args.reasoning_effort
            final_instructions = (
                prompt_file(args.final_prompt_file, FINAL_PROMPT)
                if args.final_prompt_file else FINAL_PROMPT
            )
            polish(
                statement, model=args.writer_model,
                effort=final_effort, instructions=final_instructions,
                speed=args.speed,
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
                    **limit_option,
                )
            else:
                run_goal(
                    *common, speed=args.speed,
                    **limit_option,
                )
        return 0
    except KeyboardInterrupt:
        message = (
            "Stopped review." if action == "review"
            else "Stopped LaTeX editing." if action == "finalize"
            else "Stopped. A goal pause was requested."
        )
        print(f"\n{message}", file=sys.stderr)
        return 130
    except (Error, OSError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
