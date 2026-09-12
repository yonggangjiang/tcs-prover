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


def load_config():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


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
            command = [
                executable, "-m", model["model"],
                *(["-c", f"model_reasoning_effort={json.dumps(effort)}"] if effort else []),
                "-c", 'web_search="live"', "-c", "tools.web_search=true",
                "--disable", "multi_agent", "--enable", "shell_tool",
                "-C", str(workspace), "-s", "read-only", "-a", "never", "exec",
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
                 preflight=resolve_executable):
        self.run_dir = Path(run_dir)
        self.reload_prompt = config is None
        self.config = config or load_config()
        self.settings = normalize_settings(config=self.config)
        self.on_event = on_event or (lambda event: None)
        self.before_batch = before_batch or (lambda engine: True)
        self.after_batch = after_batch or (lambda engine: None)
        self.preflight = preflight
        self.clock, self.provider = clock, provider
        self.lock = threading.RLock()
        progress = progress or {}
        self.elapsed = max(0, float(progress.get("elapsedSeconds", 0)))
        self.last_started = max(0, float(progress.get("lastStartedSeconds", 0)))
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
                    "warnings": [dict(self.warnings[slot]) for slot in sorted(self.warnings)]}

    def status(self):
        with self.lock:
            warnings = [dict(self.warnings[slot]) for slot in sorted(self.warnings)]
            return {"runningSlots": [slot for slot, cancel in self.running.items() if not cancel.is_set()],
                    "batchActive": self.thread is not None and self.thread.is_alive(),
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
                if self.closed or not active:
                    raise ValueError("Start an audit while the author is running.")
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
            self.settings, self.active = settings, bool(active and enabled)
            self.warnings = {row["slot"]: row for row in selected_warnings(list(self.warnings.values()), settings)}
            for slot, cancel in self.running.items():
                if (not active and not paused_for_audit) or settings["models"][slot - 1] == "none":
                    cancel.set()
            if (active and enabled and not busy
                    and (start_now or self.elapsed - self.last_started >= settings["intervalHours"] * 3600)):
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

    def _event(self, status, slot, model, text, **details):
        self.on_event({"kind": "research_audit", "stage": "audit", "status": status,
                       "slot": slot, "model": model, "text": text,
                       "label": f"Audit-{slot}: {status}", **details})

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

    def _snapshot(self, directory):
        """Copy saved files once; all auditors read the same captured view."""
        root = self.run_dir.resolve()
        paths = sorted({path for pattern in self.config["snapshotFiles"] for path in root.glob(pattern)})
        for path in paths:
            relative = path.relative_to(root)
            if not path.is_file() or any(part.is_symlink() for part in [path, *path.parents] if part != root):
                continue
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)

    def _batch(self, selected):
        models = {row["value"]: row for row in self.config["models"]}
        pause_requested = False
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
            pause_requested = True
            if not self.before_batch(self):
                return
            with self.lock:
                if self.closed or all(cancel.is_set() for cancel in self.running.values()):
                    return
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with tempfile.TemporaryDirectory(prefix="tcs-research-audit-") as folder:
                workspace = Path(folder)
                self._snapshot(workspace)
                with ThreadPoolExecutor(max_workers=len(selected)) as pool:
                    futures = {}
                    for slot, value in selected:
                        cancel = self.running[slot]
                        if not cancel.is_set():
                            self._event("starting", slot, value, f"Starting audit of the research snapshot captured at {stamp}.")
                            model = models[value]
                            request = {"kind": "request", "stage": "audit", "status": "prompt", "slot": slot,
                                       "model": model["model"], "provider": model["provider"],
                                       "reasoningEffort": model.get("effort"), "snapshotAt": stamp}
                            role = "Agent instructions" if model["provider"] == "kimi" else "Prompt to model"
                            label = f"Audit-{slot} — {model['label']}"
                            self.on_event({**request, "label": f"{label} — {role}", "text": prompt})
                            if model["provider"] == "kimi":
                                self.on_event({**request, "label": f"{label} — User message", "text": KIMI_START_PROMPT})
                            options = {}
                            if self.provider is run_auditor:
                                options["on_activity"] = lambda status, text, slot=slot, value=value, cancel=cancel: self._activity(slot, value, cancel, status, text)
                            futures[pool.submit(self.provider, model, prompt, workspace, cancel, **options)] = (slot, value, cancel)
                    for future in as_completed(futures):
                        slot, value, cancel = futures[future]
                        try:
                            result = future.result()
                            if cancel.is_set():
                                self._event("cancelled", slot, value, "Research audit cancelled.")
                                continue
                            if (not isinstance(result, dict)
                                    or not isinstance(result.get("text"), str) or not result["text"].strip()
                                    or not isinstance(result.get("model"), str) or not result["model"].strip()):
                                raise RuntimeError("The auditor returned an empty or malformed report")
                            completed = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            directory = self.run_dir / self.config["output"]
                            model_name = re.sub(r"[^a-zA-Z0-9_-]", "-", result["model"])[:80]
                            filename = f"{completed.replace(':', '').replace('+0000', 'Z')}-audit-{slot}-{model_name}-{uuid.uuid4().hex[:8]}.md"
                            with self.lock:
                                if cancel.is_set():
                                    continue
                                directory.mkdir(mode=0o700, exist_ok=True)
                                if directory.is_symlink():
                                    raise RuntimeError("The audit report directory cannot be a symlink.")
                                descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".audit-", suffix=".tmp")
                                try:
                                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                                        stream.write(f"# Audit-{slot} — {completed} — {result['model']}\n\n"
                                                     f"Research snapshot: {stamp}\n\n{result['text'].strip()}\n")
                                    # Publish the complete file without replacing any existing report.
                                    os.link(temporary, directory / filename)
                                finally:
                                    os.unlink(temporary)
                                if self.settings["models"][slot - 1] == value:
                                    self.warnings.pop(slot, None)
                            self._event("completed", slot, value, f"Report saved to {self.config['output']}/{filename}.",
                                        actualModel=result["model"], report=f"{self.config['output']}/{filename}")
                        except Exception as exc:
                            self._warning(slot, value, exc)
                        finally:
                            with self.lock:
                                self.running.pop(slot, None)
        except Exception as exc:
            self._warning(0, "", exc)
        finally:
            with self.lock:
                self.running.clear()
            try:
                if pause_requested:
                    self.after_batch(self)
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
                self._event("warning", 0, "", str(exc))
