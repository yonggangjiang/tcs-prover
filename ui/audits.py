"""Optional independent research advice, scheduled in active author time."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

import yaml
import workflow_runner as runtime


CONFIG_PATH = runtime.WORKFLOWS / "research_audit.yaml"
KIMI_START_PROMPT = "Run the research audit."
DIGEST_FILENAME = "audit-digest.md"
DEFAULT_TIMEOUT_MINUTES = 25
SOLVER_BRIEF_LIMIT = 40000
SOLVER_LEMMA_LIMIT = 1500
VERDICT_LINE = re.compile(r"^\s*VERDICT\s+(A\d{3})\s*:\s*(CONTINUE|STOP|REDIRECT)\b\s*(.*)$", re.IGNORECASE)
DIRECTION_LINE = re.compile(r"^\s*DIRECTION\s*\d*\s*:\s*(.+)$", re.IGNORECASE)
PREMISE_LINE = re.compile(r"^\s*PREMISE\s*:\s*(.+)$", re.IGNORECASE)
LEMMA_HEADING = re.compile(r"^## (L\d{3}\b.*)$")
PROOF_START = re.compile(r"^(\*\*Proof|Proof\.|### )")


def load_config():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def timeout_seconds(config):
    """Per-auditor time box; zero disables it."""
    try:
        minutes = float(config.get("timeoutMinutes", DEFAULT_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_TIMEOUT_MINUTES
    return minutes * 60 if math.isfinite(minutes) and minutes > 0 else 0


DEFAULT_SOLVER_INTERVAL_MINUTES = 60
DEFAULT_SOLVER_FIRST_AFTER_MINUTES = 20
DEFAULT_SOLVER_COUNT = 3
DEFAULT_SOLVER_SEEDS = [
    {"name": "standard_primitives", "text": (
        "STANDARD PRIMITIVES. Use polylogarithmically many calls of known near-linear routines of the "
        "field, classical rounding or decomposition theorems as black boxes, or an iterative process "
        "that maintains one candidate with a provable per-step guarantee.")},
]


SOLVER_DELIVERY_MESSAGE = (
    "Controller: {count} fresh-eyes solver report(s) were saved:\n{listing}\n"
    "{digest} was rebuilt from every report in AUDITS/. At your next PLAN rewrite, read the "
    "DIRECTION lines of {digest} and add one AUDIT RESPONSES line per direction: ADOPTED (node "
    "ID), REJECTED (a refuting lemma, or a written refutation of the strongest version of the "
    "direction), or DEFERRED (reason). Before rejecting a direction that names a specific "
    "auxiliary instance, rounding, or per-step guarantee, open that report with the record tool "
    "(audits) and test its strongest version against the current MISSING STEP. A report that "
    "marks none of its gaps CONJECTURE is tested first: open a node for it, restate its step "
    "problem in your own words, and send its key lemma with the report to one fresh verification "
    "subagent. If a direction would close the MISSING STEP, open its node before continuing "
    "your current route."
)


def solver_would_run(settings, author_model=None, config=None):
    """Whether the fresh-eyes solvers can run for these audit settings and this author model."""
    config = config or load_config()
    solver = solver_settings(config)
    if not solver["enabled"]:
        return False
    settings = normalize_settings(settings, config)
    catalog = {row["value"] for row in config["models"]}
    reference = solver["model"]
    if reference == "author":
        candidates = [author_model] + [value for value in settings["models"] if value != "none"]
    elif reference == "slot-1":
        candidates = [value for value in settings["models"] if value != "none"]
    else:
        candidates = [reference]
    return any(value and value != "none" and value in catalog for value in candidates)


def solver_settings(config):
    """The fresh-eyes solvers: schedule, count, seeds, model reference, and prompt."""
    solver = config.get("solver") if isinstance(config.get("solver"), dict) else {}
    prompt = config.get("solver_prompt")
    enabled = bool(solver.get("enabled")) and isinstance(prompt, str) and bool(prompt.strip())

    def minutes(key, default):
        try:
            value = float(solver.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) and value >= 0 else default

    seeds = solver.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        seeds = DEFAULT_SOLVER_SEEDS
    seeds = [{"name": re.sub(r"[^a-zA-Z0-9_]", "_", str(row.get("name") or f"seed{index}"))[:40],
              "text": str(row.get("text") or "")}
             for index, row in enumerate(seeds, 1) if isinstance(row, dict) and str(row.get("text") or "").strip()]
    try:
        count = int(solver.get("count", DEFAULT_SOLVER_COUNT))
    except (TypeError, ValueError):
        count = DEFAULT_SOLVER_COUNT
    return {"enabled": enabled and bool(seeds), "model": str(solver.get("model") or "author"),
            "prompt": prompt if enabled else "", "seeds": seeds,
            "count": max(1, min(count, len(seeds) or 1)),
            "intervalSeconds": minutes("intervalMinutes", DEFAULT_SOLVER_INTERVAL_MINUTES) * 60,
            "firstAfterSeconds": minutes("firstAfterMinutes", DEFAULT_SOLVER_FIRST_AFTER_MINUTES) * 60}


def statement_text(run_dir):
    """The exact task, without the author's file instructions."""
    path = Path(run_dir) / "INITIAL_PROMPT.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = re.search(r"^STATEMENT:\s*$", text, re.MULTILINE)
    return (text[marker.end():] if marker else text).strip()


def solver_brief(run_dir, limit=SOLVER_BRIEF_LIMIT):
    """The author's PLAN plus lemma statements without proofs or node bodies."""
    run_dir = Path(run_dir)
    parts = []
    index = run_dir / "APPROACHES" / "index.md"
    if index.is_file():
        plan, inside = [], False
        for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## "):
                if inside:
                    break
                inside = line.strip().lower().startswith("## plan")
            if inside:
                plan.append(line)
        parts.append("\n".join(plan) if plan else "## PLAN\n(the author has not written a PLAN section yet)")
    proved = run_dir / "PROVED.md"
    if proved.is_file():
        parts.append("\n## Lemma statements (proofs omitted)")
        current, block = None, []

        def flush():
            if current is not None:
                body = "\n".join(block).strip()
                parts.append(f"\n### {current}\n{body[:SOLVER_LEMMA_LIMIT]}")

        for line in proved.read_text(encoding="utf-8", errors="replace").splitlines():
            match = LEMMA_HEADING.match(line)
            if match:
                flush()
                current, block = match.group(1).strip(), []
                continue
            if current is None:
                continue
            if PROOF_START.match(line.strip()):
                flush()
                current, block = None, []
                continue
            block.append(line)
        flush()
    text = "\n".join(parts)
    data = text.encode("utf-8")
    if len(data) > limit:
        text = data[:limit].decode("utf-8", errors="ignore") + "\n[brief cut at the size limit]"
    return text


def report_label(path):
    """A short label for one saved report: 'audit-2 model' or 'fresh-eyes seed model'."""
    name = Path(path).name
    # A seeded solver file separates the seed from the model with a double dash; a seedless
    # (older) solver file keeps its whole model name.
    match = re.match(r"^\d{4}-?\d{2}-?\d{2}T\d{6}Z-(audit-\d+|fresh-eyes-[a-zA-Z0-9_]+(?=--)|fresh-eyes)-{1,2}(.+?)-[0-9a-f]{8}\.md$", name)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return name[:-3] if name.endswith(".md") else name


def digest_from_folder(run_dir, config, stamp):
    """Rebuild audit-digest.md from every report currently in the output folder."""
    directory = Path(run_dir) / config.get("output", "AUDITS")
    reports = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            try:
                reports.append((report_label(path), path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    if not reports:
        return ""
    return write_digest(run_dir, config, reports, stamp)


def write_digest(run_dir, config, reports, stamp):
    """One page the author reads first: verdict tallies, directions, premise gaps, summaries."""
    tallies, directions, premises, summaries = {}, [], [], []
    for label, text in reports:
        for line in text.splitlines():
            match = VERDICT_LINE.match(line)
            if match:
                tallies.setdefault(match.group(1).upper(), []).append(
                    (match.group(2).upper(), label, match.group(3).strip(" —–-:")[:160]))
                continue
            match = DIRECTION_LINE.match(line)
            if match:
                directions.append((label, match.group(1).strip()[:240]))
                continue
            match = PREMISE_LINE.match(line)
            if match:
                premises.append((label, match.group(1).strip()[:240]))
        body = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
        summaries.append((label, " ".join(body[:10])[:900]))
    output = config.get("output", "AUDITS")
    lines = [f"# Audit digest — {stamp}", "",
             f"{len(reports)} report(s) in {output}/. Answer every verdict and direction in the PLAN's "
             "AUDIT RESPONSES; a STOP from two or more auditors sets the node BLOCKED unless you rebut it.",
             "", "## Verdicts by node"]
    for node in sorted(tallies):
        counts = {}
        for verdict, _, _ in tallies[node]:
            counts[verdict] = counts.get(verdict, 0) + 1
        summary = ", ".join(f"{verdict} x{count}" for verdict, count in sorted(counts.items()))
        flag = "  <- STOP by 2+ auditors" if counts.get("STOP", 0) >= 2 else ""
        lines.append(f"- {node}: {summary}{flag}")
        for verdict, label, reason in tallies[node]:
            lines.append(f"  - {verdict} ({label}): {reason}")
    if not tallies:
        lines.append("(no VERDICT lines found)")
    lines += ["", "## Directions"] + ([f"- ({label}) {text}" for label, text in directions] or ["(none)"])
    lines += ["", "## Premise gaps"] + ([f"- ({label}) {text}" for label, text in premises] or ["(none)"])
    lines += ["", "## Executive summaries"] + [f"- {label}: {text}" for label, text in summaries]
    text = "\n".join(lines) + "\n"
    runtime._private_atomic_write(Path(run_dir) / DIGEST_FILENAME, text)
    return text


def default_settings():
    return load_config()["defaults"]


def model_choices():
    return [{"value": row["value"], "label": row["label"]}
            for row in load_config()["models"]]


def normalize_settings(value=None, config=None):
    config = config or load_config()
    value = config["defaults"] if value is None else value
    if not isinstance(value, dict):
        raise ValueError("Research audit settings must be an object.")
    interval = value.get("intervalHours", config["defaults"]["intervalHours"])
    try:
        hours = float(interval)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Choose a positive audit interval in hours.") from exc
    if isinstance(interval, bool) or not math.isfinite(hours) or hours <= 0:
        raise ValueError("Choose a positive audit interval in hours.")
    models = value.get("models", config["defaults"]["models"])
    supported = {row["value"] for row in config["models"]}
    if (not isinstance(models, list) or len(models) != 3
            or any(not isinstance(model, str) or model not in supported for model in models)):
        raise ValueError("Choose a supported model or None for each of the three auditors.")
    return {"intervalHours": hours, "models": list(models)}


def selected_warnings(warnings, settings=None):
    """Validate saved warnings and retain only the currently selected slot/model."""
    rows = {}
    for row in warnings if isinstance(warnings, list) else []:
        if (not isinstance(row, dict) or type(row.get("slot")) is not int or not 1 <= row["slot"] <= 3
                or not isinstance(row.get("model"), str) or row["model"] == "none"
                or not isinstance(row.get("message"), str) or not row["message"].strip()):
            continue
        if settings is None or settings["models"][row["slot"] - 1] == row["model"]:
            rows[row["slot"]] = {key: row[key] for key in ("slot", "model", "message")}
    return [rows[slot] for slot in sorted(rows)]


def _stop(process):
    """Stop only the process group owned by this audit invocation."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
    process.wait()


def resolve_executable(model):
    """Check local availability without making a model or quota request."""
    provider = model["provider"]
    if provider not in {"codex", "claude", "kimi"}:
        raise RuntimeError(f"Unsupported audit provider: {provider}.")
    executable = runtime.codex() if provider == "codex" else shutil.which(provider)
    if not executable and provider == "kimi":
        installed = Path.home() / ".kimi-code" / "bin" / "kimi"
        if installed.is_file() and os.access(installed, os.X_OK):
            executable = str(installed)
    if not executable:
        raise RuntimeError(f"{model['provider'].title()} CLI is not installed or is not on PATH.")
    return executable


def audit_activity(message):
    """Describe public CLI activity without copying private reasoning or tool output."""
    kind = message.get("type")
    if kind == "system" and message.get("subtype") == "api_retry":
        return [("retrying", f"Request retry {message.get('attempt', '?')}: {message.get('error_status') or 'connection error'}.")]
    if message.get("error") or message.get("is_error"):
        return []
    if kind == "stream_event":
        event = message.get("event", {})
        if event.get("type") == "message_start":
            return [("working", "Model responded; generating the audit.")]
        block = event.get("content_block") or event.get("delta") or {}
        if block.get("type") in {"thinking", "thinking_delta"}:
            return [("working", "Reasoning.")]
        if block.get("type") in {"text", "text_delta"}:
            return [("working", "Writing a response.")]
        return []
    if kind in {"item.started", "item.completed"}:
        item = message.get("item", {})
        labels = {"command_execution": "Running a command.", "web_search": "Searching the web.",
                  "reasoning": "Reasoning.", "agent_message": "Writing a response.",
                  "mcp_tool_call": f"Using {item.get('tool', 'a tool')}."}
        if item.get("type") in labels:
            return [("working", labels[item["type"]])]
    if kind == "assistant" or message.get("role") == "assistant":
        body = message.get("message", message)
        content = body.get("content", [])
        tools = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"] if isinstance(content, list) else []
        tools += [{"name": call.get("function", {}).get("name", "tool"),
                   "input": call.get("function", {}).get("arguments", {})}
                  for call in body.get("tool_calls") or []]
        activity = []
        for tool in tools:
            data = tool.get("input", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {}
            target = next((data[key] for key in ("file_path", "path", "pattern", "query", "url")
                           if isinstance(data, dict) and isinstance(data.get(key), str)), "")
            activity.append(("working", f"{tool.get('name', 'Tool')}: {target[:200]}".rstrip(": ")))
        return activity or [("working", "Model responded; preparing the audit.")]
    return []


def run_auditor(model, prompt, workspace, cancelled, on_activity=None):
    """One fresh CLI session, with read-only tools and no delegated agents."""
    if cancelled.is_set():
        return None
    provider = model["provider"]
    effort = model.get("effort")
    executable = resolve_executable(model)
    with tempfile.TemporaryDirectory(prefix="tcs-audit-answer-") as directory:
        answer = Path(directory) / "answer.md"
        if model["provider"] == "codex":
            sandboxed = runtime.run_sandbox_enabled()
            command = [
                os.path.realpath(executable) if sandboxed else executable, "-m", model["model"],
                *(["-c", f"model_reasoning_effort={json.dumps(effort)}"] if effort else []),
                "-c", 'web_search="live"', "-c", "tools.web_search=true",
                "--disable", "multi_agent", "--enable", "shell_tool",
                # Read-only, and on macOS limited to this audit's workspace.
                *(runtime.run_sandbox_arguments(workspace, writable=False) if sandboxed
                  else ["-s", "read-only"]),
                "-C", str(workspace), "-a", "never", "exec",
                "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                "-o", str(answer), "-",
            ]
        elif provider == "claude":
            command = [
                executable, "-p", "--model", model["model"], "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                *(["--effort", effort] if effort else []),
                "--no-session-persistence", "--safe-mode", "--restricted",
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--tools", "Read,Glob,Grep,WebSearch,WebFetch",
                "--allowedTools", "Read,Glob,Grep,WebSearch,WebFetch",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--disable-slash-commands", "--no-chrome",
            ]
        elif provider == "kimi":
            profile = Path(directory) / "research-auditor.md"
            profile.write_text("---\n" + yaml.safe_dump({
                "name": "research-auditor", "description": "Independent research audit",
                "tools": ["Read", "Grep", "Glob", "WebSearch", "FetchURL"], "subagents": [],
            }) + "---\n\n" + prompt, encoding="utf-8")
            skills = Path(directory) / "skills"
            skills.mkdir()
            command = [executable, "--model", model["model"], "--agent-file", str(profile),
                       "--skills-dir", str(skills), "--prompt", KIMI_START_PROMPT,
                       "--output-format", "stream-json"]
        else:
            raise RuntimeError(f"Unsupported audit provider: {provider}.")
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        environment = runtime.environment()
        if provider == "kimi" and effort:
            # Kimi Code v2 applies this override to configured models too.
            environment["KIMI_MODEL_THINKING_EFFORT"] = effort
        if model["provider"] == "claude":
            if effort:
                # Claude's environment setting takes precedence over --effort.
                environment["CLAUDE_CODE_EFFORT_LEVEL"] = effort
            # These auditors use the user's Claude subscription, not API billing.
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
                environment.pop(key, None)
        # A separate reader avoids moving the child's shared stdout file offset.
        output_path = Path(directory) / "activity.jsonl"
        with output_path.open("w", encoding="utf-8") as output, output_path.open("rb") as reader:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
                process = subprocess.Popen(
                    command, cwd=workspace, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                    text=True, encoding="utf-8", env=environment, **options,
                )
                try:
                    if on_activity:
                        on_activity("starting", "CLI started; waiting for a model response.")
                    pending, report, actual_model = b"", "", model["model"]
                    final_result = {}
                    last_activity, last_activity_at = None, 0

                    def consume(final=False):
                        nonlocal pending, report, actual_model, final_result, last_activity, last_activity_at
                        pending += reader.read()
                        lines = pending.split(b"\n")
                        pending = lines.pop()
                        if final and pending:
                            lines.append(pending)
                            pending = b""
                        for line in lines:
                            try:
                                message = json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(message, dict):
                                continue
                            if provider == "claude" and (message.get("type") == "result" or "result" in message):
                                final_result = message
                                report = message.get("result", "")
                                actual_model = ", ".join(message.get("modelUsage") or {}) or actual_model
                            elif provider == "kimi" and message.get("role") == "assistant" and not message.get("tool_calls"):
                                content = message.get("content", "")
                                if isinstance(content, list):
                                    content = "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
                                if content:
                                    report, actual_model = content, message.get("model") or actual_model
                            if on_activity:
                                for status, text in audit_activity(message):
                                    now = time.monotonic()
                                    if (status, text) != last_activity or now - last_activity_at >= 10:
                                        on_activity(status, text)
                                        last_activity, last_activity_at = (status, text), now

                    if provider != "kimi":
                        process.stdin.write(prompt)
                    process.stdin.close()
                    while process.poll() is None:
                        consume()
                        if cancelled.wait(0.2):
                            _stop(process)
                            return None
                    if cancelled.is_set():
                        return None
                    consume(final=True)
                    if process.returncode:
                        errors.seek(0)
                        detail = errors.read()[-2000:].strip()
                        if provider == "claude":
                            detail = final_result.get("result") or detail
                        raise RuntimeError(detail or f"Audit request exited with code {process.returncode}.")
                    if model["provider"] == "codex":
                        report = answer.read_text(encoding="utf-8")
                    elif provider == "claude":
                        if final_result.get("is_error"):
                            raise RuntimeError(str(final_result.get("result") or "Claude audit request failed."))
                    if not isinstance(report, str) or not report.strip():
                        raise RuntimeError("The auditor returned an empty report.")
                    return {"text": report, "model": actual_model}
                finally:
                    _stop(process)


class ResearchAudits:
    """Ticked by the UI; each batch can suspend and then resume the same author."""

    def __init__(self, run_dir, on_event=None, progress=None, *, clock=time.monotonic,
                 provider=run_auditor, config=None, before_batch=None, after_batch=None,
                 preflight=resolve_executable, author_model=None, after_solver_batch=None):
        self.run_dir = Path(run_dir)
        self.reload_prompt = config is None
        self.config = config or load_config()
        self.settings = normalize_settings(config=self.config)
        self.on_event = on_event or (lambda event: None)
        self.before_batch = before_batch or (lambda engine: True)
        self.after_batch = after_batch or (lambda engine: None)
        self.after_solver_batch = after_solver_batch or (lambda engine, paths: None)
        self.author_model = author_model
        self.preflight = preflight
        self.clock, self.provider = clock, provider
        self.lock = threading.RLock()
        progress = progress or {}
        self.elapsed = max(0, float(progress.get("elapsedSeconds", 0)))
        self.last_started = max(0, float(progress.get("lastStartedSeconds", 0)))
        self.solver_last_started = max(0, float(progress.get("solverLastStartedSeconds", 0)))
        self.solver_batches = max(0, int(progress.get("solverBatches", 0) or 0))
        self.solver_thread = None
        self.solver_cancel = threading.Event()
        previous = progress.get("solverPreviousFiles")
        self.solver_previous_files = [str(name) for name in previous] if isinstance(previous, list) else []
        self.last_tick = clock()
        self.active = self.closed = self.initialized = False
        self.thread = None
        self.running = {}
        self.warnings = {row["slot"]: row for row in selected_warnings(progress.get("warnings"))}
        self.last_error = ""

    def _advance(self):
        now = self.clock()
        if self.active:
            self.elapsed += max(0, now - self.last_tick)
        self.last_tick = now

    def checkpoint(self):
        with self.lock:
            self._advance()
            return {"elapsedSeconds": self.elapsed, "lastStartedSeconds": self.last_started,
                    "solverLastStartedSeconds": self.solver_last_started, "solverBatches": self.solver_batches,
                    "solverPreviousFiles": list(self.solver_previous_files),
                    "warnings": [dict(self.warnings[slot]) for slot in sorted(self.warnings)]}

    def status(self):
        with self.lock:
            warnings = [dict(self.warnings[slot]) for slot in sorted(self.warnings)]
            return {"runningSlots": [slot for slot, cancel in self.running.items() if not cancel.is_set()],
                    "batchActive": self.thread is not None and self.thread.is_alive(),
                    "solverActive": self.solver_thread is not None and self.solver_thread.is_alive(),
                    "solverBatches": self.solver_batches,
                    "warnings": warnings,
                    "batchError": self.last_error,
                    "lastError": "\n".join([row["message"] for row in warnings]
                                           + ([self.last_error] if self.last_error else []))}

    def update(self, settings, active=True, paused_for_audit=False, start_now=False):
        settings = normalize_settings(settings, self.config)
        with self.lock:
            self._advance()
            enabled = any(model != "none" for model in settings["models"])
            busy = self.thread is not None and self.thread.is_alive()
            if start_now:
                if self.closed or not (active or paused_for_audit):
                    raise ValueError("Start an audit while the author is running or paused for auditing.")
                if not enabled:
                    raise ValueError("Select and save at least one auditor first.")
                if busy:
                    raise ValueError("A research audit batch is already in progress.")
            if self.closed:
                return False
            previous = self.settings
            if self.initialized and enabled and all(model == "none" for model in previous["models"]):
                self.last_started = self.elapsed
            self.initialized = True
            solver = solver_settings(self.config)
            solver_ready = (solver["enabled"] and self._solver_model(solver, settings) is not None
                            and (self.run_dir / "INITIAL_PROMPT.md").is_file())
            self.settings, self.active = settings, bool(active and (enabled or solver_ready))
            self.warnings = {row["slot"]: row for row in selected_warnings(list(self.warnings.values()), settings)}
            for slot, cancel in self.running.items():
                if (not active and not paused_for_audit) or settings["models"][slot - 1] == "none":
                    cancel.set()
            if not active and not paused_for_audit:
                self.solver_cancel.set()
            solver_busy = self.solver_thread is not None and self.solver_thread.is_alive()
            due = solver["firstAfterSeconds"] if self.solver_batches == 0 else solver["intervalSeconds"]
            if (solver_ready and active and not solver_busy and not start_now
                    and self.elapsed - self.solver_last_started >= due):
                self.solver_last_started = self.elapsed
                self.solver_cancel = threading.Event()
                try:
                    self.solver_thread = threading.Thread(target=self._solver_batch, args=(solver, settings, self.solver_cancel), daemon=True)
                    self.solver_thread.start()
                except Exception as exc:
                    self.solver_thread = None
                    self._event("warning", 0, "", f"Could not start the fresh-eyes solvers: {exc}")
            if (enabled and not busy and ((start_now and (active or paused_for_audit))
                    or (active and self.elapsed - self.last_started >= settings["intervalHours"] * 3600))):
                previous_start, previous_thread = self.last_started, self.thread
                self.last_started = self.elapsed
                self.last_error = ""
                selected = [(slot, model) for slot, model in enumerate(settings["models"], 1) if model != "none"]
                self.running = {slot: threading.Event() for slot, _ in selected}
                try:
                    self.thread = threading.Thread(target=self._batch, args=(selected,), daemon=True)
                    self.thread.start()
                except Exception as exc:
                    self.last_started, self.thread = previous_start, previous_thread
                    self.running.clear()
                    self.last_error = f"Could not start the research audit: {exc}"
                    raise ValueError(self.last_error) from exc
                return True
            return False

    def close(self):
        with self.lock:
            self._advance()
            self.active = False
            self.closed = True
            for cancel in self.running.values():
                cancel.set()
            self.solver_cancel.set()

    def _write_digest(self, stamp):
        try:
            text = digest_from_folder(self.run_dir, self.config, stamp)
            if text:
                self._event("digest", 0, "", f"Rebuilt {DIGEST_FILENAME} from the reports in {self.config['output']}/.")
        except Exception as exc:
            self._event("warning", 0, "", f"Could not write {DIGEST_FILENAME}: {exc}")

    def _rotate_solver_reports(self, keep_names):
        """Move fresh-eyes reports of older batches to history; keep the previous batch."""
        output = self.run_dir / self.config["output"]
        history = self.run_dir / self.config.get("history", "audit_history")
        if not output.is_dir() or output.is_symlink():
            return
        history.mkdir(mode=0o700, exist_ok=True)
        for path in sorted(output.glob("*-fresh-eyes-*.md")):
            if path.is_symlink() or not path.is_file() or path.name in keep_names:
                continue
            destination, version = history / path.name, 1
            while True:
                try:
                    os.link(path, destination, follow_symlinks=False)
                    break
                except FileExistsError:
                    version += 1
                    destination = history / f"{path.stem}-previous-{version}{path.suffix}"
            path.unlink()

    def _solver_batch(self, solver, settings, cancel):
        """Run seeded fresh solvers on the statement and a brief; never pause the author."""
        try:
            model = self._solver_model(solver, settings)
            if model is None or not (self.run_dir / "INITIAL_PROMPT.md").is_file():
                return
            try:
                if not self.preflight(model):
                    raise RuntimeError("The solver model is unavailable")
            except Exception as exc:
                self._event("warning", 0, model["value"], f"Fresh-eyes solvers skipped: {exc}")
                return
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            file_stamp = stamp.replace(":", "").replace("+0000", "Z")
            statement = statement_text(self.run_dir)
            brief = solver_brief(self.run_dir)
            with self.lock:
                batch_index = self.solver_batches
                self.solver_batches += 1
                self._rotate_solver_reports(set(self.solver_previous_files))
                self.solver_previous_files = []
            seeds = solver["seeds"]
            offset = (batch_index * solver["count"]) % len(seeds)
            chosen = [seeds[(offset + index) % len(seeds)] for index in range(solver["count"])]
            self._event("starting", 0, model["value"],
                        f"Starting {len(chosen)} fresh-eyes solver(s) [{', '.join(seed['name'] for seed in chosen)}] "
                        f"on the statement and brief at {stamp}; the author keeps running.")
            saved = []
            with tempfile.TemporaryDirectory(prefix="tcs-fresh-eyes-") as root:
                with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
                    futures = {}
                    for seed in chosen:
                        workspace = Path(root) / seed["name"]
                        workspace.mkdir()
                        (workspace / "STATEMENT.md").write_text(statement + "\n", encoding="utf-8")
                        (workspace / "BRIEF.md").write_text(brief + "\n", encoding="utf-8")
                        prompt = solver["prompt"].replace("{seed}", seed["text"])
                        self.on_event({"kind": "request", "stage": "audit", "status": "prompt", "slot": 0,
                                       "model": model["model"], "provider": model["provider"],
                                       "reasoningEffort": model.get("effort"), "auditStartedAt": stamp,
                                       "label": f"Fresh-eyes solver [{seed['name']}] — {model['label']} — Prompt to model",
                                       "text": prompt})
                        options = {}
                        if self.provider is run_auditor:
                            options["on_activity"] = lambda status, text, value=model["value"]: self._event(status, 0, value, text)
                        futures[pool.submit(self.provider, model, prompt, workspace, cancel, **options)] = seed
                    timeout = timeout_seconds(self.config)
                    expired = threading.Event()

                    def expire():
                        expired.set()
                        cancel.set()

                    timer = threading.Timer(timeout, expire) if timeout else None
                    if timer:
                        timer.daemon = True
                        timer.start()
                    try:
                        for future in as_completed(futures):
                            seed = futures[future]
                            try:
                                result = future.result()
                                if cancel.is_set():
                                    self._event("cancelled", 0, model["value"],
                                                f"Fresh-eyes solver [{seed['name']}] "
                                                + ("stopped at the time box; no report saved." if expired.is_set() else "cancelled."))
                                    continue
                                if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                                        or not result["text"].strip() or not isinstance(result.get("model"), str)):
                                    raise RuntimeError("the solver returned an empty or malformed report")
                                directory = self.run_dir / self.config["output"]
                                model_name = re.sub(r"[^a-zA-Z0-9_-]", "-", result["model"])[:80]
                                filename = f"{file_stamp}-fresh-eyes-{seed['name']}--{model_name}-{uuid.uuid4().hex[:8]}.md"
                                with self.lock:
                                    directory.mkdir(mode=0o700, exist_ok=True)
                                    if directory.is_symlink():
                                        raise RuntimeError("The audit report directory cannot be a symlink.")
                                    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".audit-", suffix=".tmp")
                                    try:
                                        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                                            stream.write(f"# Fresh-eyes solver [{seed['name']}] — {stamp} — {result['model']}\n\n"
                                                         f"{result['text'].strip()}\n\n## Technique family\n{seed['text']}\n")
                                        os.link(temporary, directory / filename)
                                    finally:
                                        os.unlink(temporary)
                                saved.append(f"{self.config['output']}/{filename}")
                                with self.lock:
                                    self.solver_previous_files.append(filename)
                                self._event("completed", 0, model["value"],
                                            f"Fresh-eyes solver [{seed['name']}] report saved to {self.config['output']}/{filename}.",
                                            actualModel=result["model"], report=f"{self.config['output']}/{filename}")
                            except Exception as exc:
                                self._event("warning", 0, model["value"],
                                            f"Fresh-eyes solver [{seed['name']}] skipped: {str(exc) or type(exc).__name__}.")
                    finally:
                        if timer:
                            timer.cancel()
            if saved:
                self._write_digest(stamp)
                try:
                    self.after_solver_batch(self, saved)
                except Exception as exc:
                    self._event("warning", 0, "", f"Could not deliver the solver reports: {exc}")
        except Exception as exc:
            self._event("warning", 0, "", f"Fresh-eyes solver batch failed: {str(exc) or type(exc).__name__}")

    def _solver_model(self, solver, settings):
        """Resolve 'author', 'slot-1', or a catalog value to a model row."""
        models = {row["value"]: row for row in self.config["models"]}
        reference = solver["model"]
        candidates = []
        if reference == "author":
            candidates = [self.author_model] + [value for value in settings["models"] if value != "none"]
        elif reference == "slot-1":
            candidates = [value for value in settings["models"] if value != "none"]
        else:
            candidates = [reference]
        for value in candidates:
            if value and value != "none" and value in models:
                return models[value]
        return None

    def _event(self, status, slot, model, text, **details):
        self.on_event({"kind": "research_audit", "stage": "audit", "status": status,
                       "slot": slot, "model": model, "text": text,
                       "label": f"Audit-{slot}: {status}" if slot else f"Research audits: {status}", **details})

    def _activity(self, slot, model, cancelled, status, text):
        with self.lock:
            if cancelled.is_set() or self.closed:
                return
            recovered = False
            if self.settings["models"][slot - 1] == model:
                if status == "working":
                    recovered = self.warnings.pop(slot, None) is not None
                elif status == "retrying":
                    self.warnings[slot] = {"slot": slot, "model": model, "message": text}
        self._event(status, slot, model, text, recovered=recovered)

    def _warning(self, slot, model, error):
        model_label = next((row["label"] for row in self.config["models"] if row["value"] == model), model)
        label = f"Audit-{slot} — {model_label} ({model})" if slot else "Research audit batch"
        message = f"Warning: {label} skipped: {str(error) or type(error).__name__}. No report saved."
        with self.lock:
            if slot:
                if self.settings["models"][slot - 1] != model:
                    return
                self.warnings[slot] = {"slot": slot, "model": model, "message": message}
            else:
                self.last_error = message
        self._event("warning", slot, model, message)

    def _rotate_reports(self):
        """Move previous reports into history without copying or replacing files."""
        output = self.config["output"]
        history = self.config.get("history", "audit_history")
        if (any(not isinstance(name, str) or Path(name).name != name
                or name in {"", ".", ".."} for name in (output, history))
                or output == history):
            raise ValueError("Audit output and history must be distinct workspace folder names.")
        directory, archive = self.run_dir / output, self.run_dir / history
        for folder in (directory, archive):
            if folder.is_symlink():
                raise RuntimeError("Audit report and history directories cannot be symlinks.")
            folder.mkdir(mode=0o700, exist_ok=True)
        paths = sorted(directory.iterdir())
        if any(path.is_dir() and not path.is_symlink() for path in paths):
            raise RuntimeError("The audit report directory must contain files, not subdirectories.")
        archived = {}
        for path in paths:
            if "-fresh-eyes-" in path.name and path.name.endswith(".md"):
                continue  # solver reports rotate on the solver schedule
            destination, version = archive / path.name, 1
            while True:
                try:
                    # An exclusive hard link followed by unlink is a move on this
                    # workspace filesystem; interrupted moves never lose the source.
                    os.link(path, destination, follow_symlinks=False)
                    break
                except FileExistsError:
                    version += 1
                    destination = archive / f"{path.stem}-previous-{version}{path.suffix}"
            path.unlink()
            archived[f"{output}/{path.name}"] = f"{history}/{destination.name}"
        return archived

    def _batch(self, selected):
        models = {row["value"]: row for row in self.config["models"]}
        try:
            # Freeze one current prompt for the batch, including all parallel auditors.
            prompt = (load_config() if self.reload_prompt else self.config).get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("research_audit.yaml must contain a nonempty prompt.")
            eligible = []
            for slot, value in selected:
                if self.running[slot].is_set():
                    continue
                self._event("checking", slot, value, "Checking the selected CLI before retrying the audit.")
                try:
                    if not self.preflight(models[value]):
                        raise RuntimeError("The selected auditor is unavailable")
                    eligible.append((slot, value))
                    self._event("waiting", slot, value, "CLI found; waiting for the author to pause.")
                except Exception as exc:
                    self.running[slot].set()
                    self._warning(slot, value, exc)
            with self.lock:
                selected = [(slot, value) for slot, value in eligible if not self.running[slot].is_set()]
                if self.closed or not selected:
                    return
            if not self.before_batch(self):
                return
            with self.lock:
                if self.closed or all(cancel.is_set() for cancel in self.running.values()):
                    return
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            workspace = self.run_dir.resolve()
            archived = self._rotate_reports()
            if archived:
                self._event("archived", 0, "", f"Moved {len(archived)} previous audit files to {self.config.get('history', 'audit_history')}/.",
                            archivedPaths=archived)
            saved_reports = []
            with ThreadPoolExecutor(max_workers=len(selected)) as pool:
                futures = {}
                for slot, value in selected:
                    cancel = self.running[slot]
                    if not cancel.is_set():
                        self._event("starting", slot, value, f"Starting audit in the paused author workspace at {stamp}.")
                        model = models[value]
                        request = {"kind": "request", "stage": "audit", "status": "prompt", "slot": slot,
                                   "model": model["model"], "provider": model["provider"],
                                   "reasoningEffort": model.get("effort"), "auditStartedAt": stamp}
                        role = "Agent instructions" if model["provider"] == "kimi" else "Prompt to model"
                        label = f"Audit-{slot} — {model['label']}"
                        self.on_event({**request, "label": f"{label} — {role}", "text": prompt})
                        if model["provider"] == "kimi":
                            self.on_event({**request, "label": f"{label} — User message", "text": KIMI_START_PROMPT})
                        options = {}
                        if self.provider is run_auditor:
                            options["on_activity"] = lambda status, text, slot=slot, value=value, cancel=cancel: self._activity(slot, value, cancel, status, text)
                        futures[pool.submit(self.provider, model, prompt, workspace, cancel, **options)] = (slot, value, cancel)
                timeout = timeout_seconds(self.config)
                expired = threading.Event()
                cancels = [cancel for _, _, cancel in futures.values()]

                def expire():
                    expired.set()
                    for cancel in cancels:
                        cancel.set()

                timer = threading.Timer(timeout, expire) if timeout else None
                if timer:
                    timer.daemon = True
                    timer.start()
                try:
                    for future in as_completed(futures):
                        slot, value, cancel = futures[future]
                        try:
                            result = future.result()
                            if cancel.is_set():
                                self._event("cancelled", slot, value,
                                            f"Time box of {timeout / 60:g} minutes reached; no report saved."
                                            if expired.is_set() else "Research audit cancelled.")
                                continue
                            if (not isinstance(result, dict)
                                    or not isinstance(result.get("text"), str) or not result["text"].strip()
                                    or not isinstance(result.get("model"), str) or not result["model"].strip()):
                                raise RuntimeError("The auditor returned an empty or malformed report")
                            completed = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            directory = self.run_dir / self.config["output"]
                            model_name = re.sub(r"[^a-zA-Z0-9_-]", "-", result["model"])[:80]
                            kind = "fresh-eyes" if slot == 0 else f"audit-{slot}"
                            filename = f"{completed.replace(':', '').replace('+0000', 'Z')}-{kind}-{model_name}-{uuid.uuid4().hex[:8]}.md"
                            title = "Fresh-eyes solver" if slot == 0 else f"Audit-{slot}"
                            with self.lock:
                                if cancel.is_set():
                                    continue
                                directory.mkdir(mode=0o700, exist_ok=True)
                                if directory.is_symlink():
                                    raise RuntimeError("The audit report directory cannot be a symlink.")
                                descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".audit-", suffix=".tmp")
                                try:
                                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                                        stream.write(f"# {title} — {completed} — {result['model']}\n\n"
                                                     f"Audit started: {stamp}\n\n{result['text'].strip()}\n")
                                    # Publish the complete file without replacing any existing report.
                                    os.link(temporary, directory / filename)
                                finally:
                                    os.unlink(temporary)
                                if slot and self.settings["models"][slot - 1] == value:
                                    self.warnings.pop(slot, None)
                            saved_reports.append((f"{kind} {result['model']}", result["text"]))
                            self._event("completed", slot, value, f"{title} report saved to {self.config['output']}/{filename}.",
                                        actualModel=result["model"], report=f"{self.config['output']}/{filename}")
                        except Exception as exc:
                            if slot:
                                self._warning(slot, value, exc)
                            else:
                                self._event("warning", 0, value, f"Fresh-eyes solver skipped: {str(exc) or type(exc).__name__}.")
                        finally:
                            with self.lock:
                                self.running.pop(slot, None)
                finally:
                    if timer:
                        timer.cancel()
            if saved_reports:
                self._write_digest(stamp)
        except Exception as exc:
            self._warning(0, "", exc)
        finally:
            with self.lock:
                self.running.clear()
            try:
                # Release a manually paused workspace even when preflight fails.
                self.after_batch(self)
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
                self._event("warning", 0, "", str(exc))
