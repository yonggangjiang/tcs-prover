"""Manage proof jobs and serve the local browser interface."""

import getpass
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import workflow_runner as runtime
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
WINDOWS_EVERYONE_SID = "*S-1-1-0"
AUTHOR_LIMIT_FILENAME = "author-limit.json"
AUTHOR_STEER_FILENAME = "author-steer.json"
PAUSE_FILENAME = "pause.json"
PAUSE_REQUEST_FILENAME = "pause-request.json"
JOB_SETTINGS_FILENAME = "job-settings.json"
MANUAL_STOP_FILENAME = "manual-stop.json"
CONTINUATION_SOURCE_FILENAME = "continuation-source.json"
REVIEW_INPUT_FILENAME = "review-input.json"
RESEARCH_MEMORY_FILES = {"INITIAL_PROMPT.md", "APPROACHES.md", "PROVED.md"}
APPROACH_MEMORY_FILE = re.compile(r"APPROACHES/(?:INDEX|A[0-9]{3,}(?:-[A-Za-z0-9_-]+)?)\.md\Z")
LEGACY_MODEL_ALIASES = {"deepseek/deepseek-v4-pro": runtime.DEEPSEEK_MODEL}
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
    """Force-stop one Codex wrapper and every process it launched."""

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
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # This fallback is only for a failed platform tree-stop operation.
        process.kill()
        process.wait()


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


def default_prompts():
    """Read workflow prompts at use time, including edits made while serving."""

    author_critic = runtime.builtin_workflow("author_critic")["prompts"]
    return {
        "review": REVIEW_PROMPT,
        "author": author_critic["author"],
        "critic": author_critic["critic"],
        "final": runtime.builtin_workflow("clean_up")["prompts"]["final"],
    }


def web_prompt_options(body, roles, source=None):
    """Accept only deliberate per-job edits; old browser defaults are ignored."""

    overrides = body.get("promptOverrides", {})
    if not isinstance(overrides, dict) or any(
        name not in {"review", "author", "critic", "final"}
        or not isinstance(value, str) for name, value in overrides.items()
    ):
        raise ValueError("Prompt overrides must map role names to prompt text.")
    return {
        f"{name}_prompt": overrides.get(name, (source or {}).get(f"{name}Prompt"))
        for name in roles
    }


REVIEW_MODELS = MODELS
TRACE_LIMIT = 1500
PINNED_KINDS = {
    "request", "review_result", "critic_result", "author_result",
    "final_result", "failure_result", "partial_result", "diagnostic", "error",
}


def important_record(record):
    """Recognize prompts and other milestones that must stay visible."""

    if record.get("kind") in PINNED_KINDS:
        return True
    if record.get("kind") != "codex_event" or record.get("root") is False:
        return False
    event = record.get("event") or {}
    params = event.get("params") or {}
    item = params.get("item") or event.get("item") or {}
    return (
        (event.get("method") or event.get("type")) in {
            "item/completed", "item.completed"
        }
        and item.get("type") in {"agentMessage", "agent_message"}
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
        "review_models": list(REVIEW_MODELS),
        "models": list(MODELS),
        "review_reasoning_effort": DEFAULT_REVIEW_EFFORT,
        "revision_reasoning_effort": DEFAULT_REVIEW_EFFORT,
        "model_summary": "Astra/Ultra review · Astra/Ultra author, critic, writer",
        "critic_rounds": {
            "default": DEFAULT_CRITIC_ROUNDS,
            "minimum": 1,
            "maximum": MAX_CRITIC_ROUNDS,
        },
        "thinking_hours": {
            "default": DEFAULT_THINKING_HOURS,
            "minimum": 0.01,
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
            "description": "The LLM explores in one session and maintains its notebooks following the YAML prompts.",
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
            "description": "Polishes the passing proof into LaTeX in one editor call, then returns it.",
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
            "description": "Polishes the supplied writing into LaTeX in one editor call, then returns it.",
        },
    },
    "edges": [],
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
        "statementReviewOnly": False,
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
        "authorPrompt": prompts["author"],
        "criticPrompt": prompts["critic"],
        "finalPrompt": prompts["final"],
        "criticRounds": DEFAULT_CRITIC_ROUNDS,
        "thinkingHours": DEFAULT_THINKING_HOURS,
        "stage": "",
        "activeNode": "",
        "round": 0,
        "startedAt": "",
        "finishedAt": "",
        "lastActivityAt": "",
        "output": "",
        "error": "",
        "manuallyStopped": False,
        "stoppedStage": "",
        "settingsWarning": "",
        "runId": "",
        "checkpoints": [],
        "workflow": {**PUBLIC_GRAPH, "settings": {**PUBLIC_GRAPH["settings"], "prompts": prompts}},
        "trace": trace if trace is not None else [],
        "traceVersion": trace_version,
    }


def restored_model(value):
    """Map discontinued saved model routes to their supported replacement."""

    return LEGACY_MODEL_ALIASES.get(value, value)


class App:
    """Own one review-and-solve workflow."""

    def __init__(self, trace_file=None, runs=RUNS, output_stream=None):
        # Tests may supply one fixed log; the real app uses one folder per run.
        self.runs, self.fixed_trace = Path(runs), trace_file is not None
        self.output_stream = output_stream
        self.trace_file = Path(trace_file) if trace_file else self._latest_trace()
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

    def _latest_trace(self):
        """Find the most recently changed run log, if one exists."""

        if not self.runs.exists():
            return None
        logs = (
            path for path in self.runs.glob("*/transcript.jsonl")
            if not path.is_symlink()
            and direct_run_directory(path.parent, self.runs)
        )
        return max(logs, key=lambda path: path.stat().st_mtime, default=None)

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
            self.run_dir = self.runs / f"{stamp}_{slug}"
            number = 2
            while self.run_dir.exists():
                self.run_dir = self.runs / f"{stamp}_{slug}-{number}"
                number += 1
            self.run_dir.mkdir(mode=0o700)
            self.trace_file = self.run_dir / "transcript.jsonl"
        prepare_private_directory(self.run_dir)
        self._save("draft.md", f"# Draft problem\n\n{statement}\n")

    def _save(self, name, content):
        """Save one private, human-readable run artifact."""

        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    def _save_job_settings(self, options):
        """Persist restart-safe workflow settings without prompt duplication."""

        keys = (
            "reviewModel", "authorModel", "criticModel", "writerModel",
            "reasoningEffort", "reviewEffort", "authorEffort",
            "criticEffort", "writerEffort", "criticRounds",
            "thinkingHours", "speedMode", "reasoningSummary",
            "problemMode", "skipStatementReview", "statementReviewOnly",
            "goalThreadId", "goalWorkspace", "goalResume", "authorSteerDelivered", "elapsedSeconds", "resumedAt",
        )
        self._save(
            JOB_SETTINGS_FILENAME,
            json.dumps(
                {key: options[key] for key in keys if key in options},
                ensure_ascii=False, indent=2, sort_keys=True,
            ) + "\n",
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
            return worker
        except Exception:
            with self.lock:
                if self.worker_token is token:
                    self.worker_token = None
            raise

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

        path = self.run_dir / AUTHOR_LIMIT_FILENAME
        temporary = path.with_name(f".{AUTHOR_LIMIT_FILENAME}.tmp")
        try:
            temporary.write_text(
                json.dumps({"hours": hours}), encoding="utf-8"
            )
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _write_author_steer(self, instruction):
        """Atomically publish one live instruction to the active author."""

        path = self.run_dir / AUTHOR_STEER_FILENAME
        temporary = path.with_name(f".{AUTHOR_STEER_FILENAME}.tmp")
        command_id = secrets.token_hex(12)
        try:
            temporary.write_text(json.dumps({
                "id": command_id,
                "instruction": instruction,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return command_id

    def _load_trace(self):
        """Restore the append-only local transcript after a restart."""

        if not self.trace_file or not self.trace_file.exists():
            return [], 0, []
        try:
            records, pinned, total = deque(maxlen=TRACE_LIMIT), [], 0
            self.trace_file.parent.chmod(0o700)
            self.trace_file.chmod(0o600)
            with self.trace_file.open(
                encoding="utf-8", errors="replace"
            ) as stream:
                for line in stream:
                    total += 1
                    try:
                        if line.strip():
                            record = json.loads(line)
                            if not isinstance(record, dict):
                                continue
                            records.append(record)
                            if important_record(record):
                                pinned.append(record)
                    except json.JSONDecodeError:
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
            self.trace_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.trace_file.parent.chmod(0o700)
            self.trace_file.touch(exist_ok=True, mode=0o600)
            self.trace_file.chmod(0o600)
            with self.trace_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
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

    def memory_file(self, name, version=""):
        """Display one author-owned file on demand, without modifying it."""

        approach = name in {"APPROACHES", "APPROACHES.md"} or bool(APPROACH_MEMORY_FILE.fullmatch(name))
        if name not in RESEARCH_MEMORY_FILES and not approach:
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
                return self._memory_file_portable(directory, name, version, result, approach)
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
                    else:
                        opened.callback(os.close, folder)
                        entries = []
                        for entry in os.listdir(folder):
                            relative = f"APPROACHES/{entry}"
                            if not APPROACH_MEMORY_FILE.fullmatch(relative) or entry == "INDEX.md":
                                continue
                            try:
                                if stat.S_ISREG(os.stat(entry, dir_fd=folder, follow_symlinks=False).st_mode):
                                    entries.append(relative)
                            except FileNotFoundError:
                                continue
                        result["files"] = ["APPROACHES/INDEX.md", *sorted(entries)]
                        try:
                            if stat.S_ISREG(os.stat("APPROACHES.md", dir_fd=parent, follow_symlinks=False).st_mode):
                                result["files"].append("APPROACHES.md")
                        except FileNotFoundError:
                            pass
                        if name == "APPROACHES.md":
                            filename = name
                        else:
                            parent = folder
                            filename = "INDEX.md" if name == "APPROACHES" else name.split("/")[1]
                            result["name"] = f"APPROACHES/{filename}"
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
    def _memory_file_portable(directory, name, version, result, approach):
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
                result["files"] = ["APPROACHES/INDEX.md", *sorted(
                    f"APPROACHES/{entry.name}" for entry in folder.iterdir()
                    if entry.name != "INDEX.md" and APPROACH_MEMORY_FILE.fullmatch(f"APPROACHES/{entry.name}")
                    and not linked(entry) and entry.is_file()
                )]
                legacy = directory / "APPROACHES.md"
                if not linked(legacy) and legacy.is_file():
                    result["files"].append("APPROACHES.md")
                name = "APPROACHES/INDEX.md" if name == "APPROACHES" else name
            elif name == "APPROACHES":
                name = "APPROACHES.md"
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
            graph = state["workflow"]
            state["workflow"] = {
                **graph, "settings": {**graph["settings"], "prompts": default_prompts()},
            }
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
            state["checkpoints"] = list(self._checkpoint_cache)
            state["canDownloadTex"] = bool(
                state["phase"] == "done" and not self.has_active_worker()
                and self.run_dir and (self.run_dir / "final.tex").is_file()
                and not (self.run_dir / "final.tex").is_symlink()
            )
            trace, version = self.state["trace"], self.state["traceVersion"]
            first = version - len(trace)
            if isinstance(after, int) and first <= after <= version:
                state["trace"], state["traceFrom"] = list(trace[after - first:]), after
            else:
                state["trace"], state["traceFrom"] = self.retained_trace(), first
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
    ):
        """Normalize and validate settings shared by both input modes."""

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
        prompts = {
            name: str(
                defaults[name] if value is None else value
            ).strip()
            for name, value in supplied_prompts.items()
        }
        if include_review and review_model not in REVIEW_MODELS:
            raise ValueError(
                "Choose Astra, Sol, Terra, Luna, or DeepSeek V4 Pro for statement "
                "review."
            )
        if any(
            model not in MODELS
            for model in (author_model, critic_model, writer_model)
        ):
            raise ValueError(
                "Choose Astra, Sol, Terra, Luna, or DeepSeek V4 Pro for every proof "
                "stage."
            )
        efforts = [author_effort, critic_effort, writer_effort]
        if include_review:
            efforts.append(review_effort)
        if any(effort not in EFFORTS for effort in efforts):
            raise ValueError("Choose a valid reasoning effort for every role.")
        selected_models = [author_model, critic_model, writer_model]
        if include_review:
            selected_models.append(review_model)
        try:
            for selected_model in set(selected_models):
                runtime.verify_model_credentials(selected_model)
        except runtime.Error as exc:
            raise ValueError(str(exc)) from exc
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
        if not 0 < thinking_hours <= MAX_THINKING_HOURS:
            raise ValueError(
                f"Choose more than 0 and at most {MAX_THINKING_HOURS} hours."
            )
        return {
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
    ):
        """Start the review and return immediately so the page can poll."""

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
        )
        if not statement:
            raise ValueError("Enter a problem statement.")
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Codex is already working.")
            # Feedback retries stay in the same problem folder.
            retry = self.state["phase"] == "reviewed" and self.run_dir is not None
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
                    "Codex was stopped before it produced a checked statement.",
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
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=ROOT,
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
                        "Codex was stopped before it produced a checked statement.",
                    )
                return
            process.stdin.write(statement + ("\0" + feedback if feedback else ""))
            process.stdin.close()
            for line in process.stdout:
                record = self.parse_line(line, "review")
                self.add_trace(record)
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
                    "Codex was stopped before it produced a checked statement.",
                )
            elif problem:
                self.state["phase"], self.state["error"] = "input", problem
            if self.active_token is token:
                self.active_token = None
            if self.worker_token is token:
                self.worker_token = None

    def _launch_workflow_locked(self, workflows, source, options, stage, node, state=None):
        """Launch YAML graphs in this job, preserving worker ownership on errors."""

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
            phase="running", stage=stage, activeNode=node, round=0, output="",
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

        try:
            started_at = datetime.fromisoformat(self.state.get("resumedAt") or self.state["startedAt"])
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            elapsed_seconds = max(
                0.0, (datetime.now(timezone.utc) - started_at).total_seconds()
            )
        except (KeyError, TypeError, ValueError):
            elapsed_seconds = 0.0
        return float(self.state.get("elapsedSeconds", 0)) + elapsed_seconds

    def _save_pause(self, checkpoint):
        """Atomically store controller state; research notebooks belong to the LLM."""

        self._save(".pause.tmp", json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n")
        (self.run_dir / ".pause.tmp").replace(self.run_dir / PAUSE_FILENAME)

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

    def pause(self):
        """Interrupt the author while retaining its native session for resume."""

        with self.lock:
            if self.state["phase"] == "pausing":
                return
            if (self.state["phase"] != "running" or self.state["activeNode"] != "author"
                    or self.state["stage"] not in {"solve", "repair"}):
                raise ValueError("Pause is available while the proof author is working.")
            checkpoint_state = self._author_checkpoint_state()
            self._save_pause({"status": "pausing", "node": "author", "state": checkpoint_state,
                              "elapsedSeconds": self._elapsed_seconds(), "stage": self.state["stage"],
                              "goalThreadId": self.state.get("goalThreadId", "")})
            self._save(PAUSE_REQUEST_FILENAME, "{}\n")
            self.state.update(phase="pausing", error="")
            self.add_trace({"kind": "status", "stage": self.state["stage"], "node": "author",
                            "label": "Pause requested", "text": "Interrupting Codex and preserving its saved conversation for Resume."})
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
                                "text": "Codex did not acknowledge the pause; closing its process. Resume will reopen the saved conversation."})
            stop_process_tree(process)

        threading.Thread(target=finish_if_unresponsive, daemon=True).start()

    def resume(self):
        """Reopen this run with a new CLI process and the saved goal/thread."""

        with self.lock:
            if self.state["phase"] != "paused" or self.has_active_worker():
                raise ValueError("Wait until the author is paused before resuming.")
            checkpoint = json.loads((self.run_dir / PAUSE_FILENAME).read_text(encoding="utf-8"))
            workflow_state = dict(checkpoint["state"])
            workflow_state.pop("paused", None)
            workflow_state.pop("failed", None)
            previous = dict(self.state)
            if checkpoint.get("goalThreadId"):
                self.state["goalThreadId"] = checkpoint["goalThreadId"]
            self.state.update(goalResume=True, elapsedSeconds=checkpoint["elapsedSeconds"],
                              resumedAt=datetime.now(timezone.utc).isoformat(), finishedAt="", error="")
            (self.run_dir / PAUSE_REQUEST_FILENAME).unlink(missing_ok=True)
            process = None
            try:
                self._save_pause({**checkpoint, "status": "running", "error": ""})
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
                            "label": "Resume requested", "text": "Reopening the saved Codex conversation in this same run folder."})

    def final_tex(self):
        """Read the finished LaTeX artifact for an authenticated download."""

        with self.lock:
            if not self.snapshot()["canDownloadTex"]:
                raise ValueError("A completed final.tex is not available for this job.")
            return (self.run_dir / "final.tex").read_bytes()

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
    ):
        """Send a statement directly to the proof author without review."""

        statement = str(statement).strip()
        if not statement:
            raise ValueError("Enter a problem statement.")
        if "\0" in statement:
            raise ValueError("The problem statement cannot contain NUL characters.")
        if continuation_source:
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
        )
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Codex is already working.")
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
    ):
        """Polish one combined theorem-and-proof input without earlier stages."""

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
        )
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Codex is already working.")
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
    ):
        """Retry normal finalization with its exact statement/proof contract."""

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
        )
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Codex is already working.")
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
    ):
        """Create a new job that audits one complete saved candidate proof."""

        statement, solution = str(statement).strip(), str(solution).strip()
        if not statement or not solution:
            raise ValueError("A saved statement and complete proof are required.")
        if "\0" in statement or "\0" in solution:
            raise ValueError("Saved critic inputs cannot contain NUL characters.")
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
        )
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Codex is already working.")
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

        with self.lock:
            if self.state.get("statementReviewOnly", False):
                raise ValueError(
                    "This job was configured for statement review only."
                )
            if self.state["phase"] != "reviewed":
                raise ValueError("There is no reviewed statement to approve.")
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
            process, token = self._launch_solver_locked(statement)
        self._spawn_worker(self._read_output, (process, token), token)

    def _read_output(self, process, token=None):
        """Store tagged solver events and build the visible final answer."""

        problem, answers, order, final, failed = "", {}, [], False, False
        latest_critic_solution = ""
        last_work_node = self.state["activeNode"]
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
                        self.state["stage"] = record_stage
                        self.state["activeNode"] = event_node(record, self.state["activeNode"])
                if isinstance(record.get("round"), int):
                    with self.lock:
                        self.state["round"] = record["round"]
                elif (
                    record.get("kind") == "request"
                    and record_stage == "critic"
                ):
                    with self.lock:
                        self.state["round"] += 1
                # A repair replaces the prior answer; the final proof replaces all.
                if (
                    record.get("kind") == "request"
                    and record_stage == "repair"
                ):
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
                            try:
                                checkpoint = json.loads((self.run_dir / PAUSE_FILENAME).read_text(encoding="utf-8"))
                            except (OSError, ValueError):
                                checkpoint = {}
                            reason = record.get("reason", "")
                            paused_stage = record["stage"]
                            if (record["node"] == "author"
                                    and record["state"].get("report", {}).get("verdict") == "reject"):
                                paused_stage = "repair"
                            self._save_pause({**checkpoint, "status": "pausing", "state": record["state"],
                                              "node": record["node"], "error": reason,
                                              "elapsedSeconds": self._elapsed_seconds(),
                                              "goalThreadId": self.state.get("goalThreadId", ""),
                                              "stage": paused_stage})
                            self.state.update(phase="pausing", error=reason)
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
                    with self.lock:
                        self.state["output"] = record.get("output", "")
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
            if self.process is process:
                self.process = None
            if self.active_token is token:
                self.active_token = None
            if self.worker_token is token:
                self.worker_token = None
            self.state["phase"] = "done"
            self.state["finishedAt"] = datetime.now(timezone.utc).isoformat()
            try:
                checkpoint = json.loads((self.run_dir / PAUSE_FILENAME).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                checkpoint = {}
            author_interrupted = (not stopped and last_work_node == "author" and (not final or failed))
            if (paused or author_interrupted) and (not final or failed):
                error = checkpoint.get("error", "") if paused else (
                    problem or (self.state["output"] if failed else "")
                    or f"The author exited before completing the task (code {code}).")
                if not paused:
                    workflow_state = {**checkpoint.get("state", {}), **self._author_checkpoint_state()}
                    checkpoint.update(state=workflow_state, node="author",
                                      stage="repair" if workflow_state.get("report") else "solve")
                self._save_pause({**checkpoint, "status": "paused", "elapsedSeconds": self._elapsed_seconds(),
                                  "goalThreadId": self.state.get("goalThreadId", ""), "error": error})
                self.state.update(phase="paused", error=error, activeNode=checkpoint["node"], stage=checkpoint["stage"])
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
                    "Codex was stopped before it produced an answer."
                )
            elif problem or code:
                self.state["error"] = problem or f"The solver exited with code {code}."
            if self.state["output"] and not final:
                self._save("partial-output.md", self.state["output"])

    def clear_trace(self):
        """Clear the saved transcript only when no request is active."""

        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Wait for the current Codex request to finish.")
            self.state["trace"] = []
            self.state["traceVersion"] = 0
            self.pinned = []
            if self.trace_file:
                self.trace_file.unlink(missing_ok=True)

    def stop(self):
        """Immediately stop Codex and all of its descendants."""

        with self.lock:
            if self.state["phase"] == "stopping" or (
                self.state["phase"] == "done"
                and self.state["error"] == "Stopped."
            ):
                return
            if self.state["phase"] not in {"reviewing", "running", "pausing"}:
                raise ValueError("Codex is not working.")
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
                stoppedStage=self.state["stage"],
            )
            (self.run_dir / PAUSE_REQUEST_FILENAME).unlink(missing_ok=True)
            (self.run_dir / PAUSE_FILENAME).unlink(missing_ok=True)
            process, self.state["phase"] = self.process, "stopping"
        stop_process_tree(process)
        with self.lock:
            # Real jobs stay non-continuable until their reader has drained all
            # buffered output. Synthetic/no-worker callers can settle here.
            if self.state["phase"] == "stopping" and self.worker_token is None:
                self._finish_stopped_locked(
                    self.active_token,
                    "Codex was stopped before it produced an answer.",
                )

    def set_author_time_limit(self, hours):
        """Replace a running or paused author's total-workflow deadline."""

        try:
            hours = float(hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("The total time limit must be a number of hours.") from exc
        if not 0 < hours <= MAX_THINKING_HOURS:
            raise ValueError(
                f"Set the total time limit above 0 and at most "
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
                "text": f"The total workflow time limit is now {hours:g} hours.",
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

        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping", "pausing"}:
                raise ValueError("Stop the current work first.")
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
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass


def saved_author_instructions(run_dir):
    """Return bounded, deduplicated user steering from a stopped author run."""

    labels = {
        "Live instruction queued", "Live author instruction sent",
        "Restored live instructions queued",
    }
    records = [record for record in _iter_run_records(run_dir)
               if record.get("label") in labels]
    restored_ids = {
        str(record.get("steerId") or "").strip()
        for record in records
        if record.get("label") == "Restored live instructions queued"
        and str(record.get("steerId") or "").strip()
    }
    instructions, seen = [], set()
    for record in records:
        if record.get("label") not in labels:
            continue
        steer_id = str(record.get("steerId") or "").strip()
        if (
            record.get("label") == "Live author instruction sent"
            and steer_id in restored_ids
        ):
            continue
        values = record.get("restoredInstructions")
        values = values if isinstance(values, list) else [record.get("text")]
        for value in values:
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                continue
            key = value.strip()
            if key in seen:
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
        audit_effort = (
            runtime.effective_effort(model, "high")
            if runtime.is_deepseek_model(model)
            else runtime.effective_effort(model, effort)
        )
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
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(settings, dict):
            for key in (
                "reviewModel", "authorModel", "criticModel", "writerModel",
                "reasoningEffort", "reviewEffort", "authorEffort",
                "criticEffort", "writerEffort", "criticRounds",
                "thinkingHours", "speedMode", "reasoningSummary",
                "problemMode", "skipStatementReview", "statementReviewOnly",
                "goalThreadId", "goalWorkspace", "goalResume", "authorSteerDelivered", "elapsedSeconds", "resumedAt",
            ):
                if key in settings:
                    if key in {
                        "skipStatementReview", "statementReviewOnly",
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
                    state[key] = (
                        restored_model(settings[key])
                        if key.endswith("Model") else settings[key]
                    )
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
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
        labels = {
            "review": "statement reviewer",
            "author": "proof author",
            "critic": "critic",
            "writer": "LaTeX editor",
        }
        names = ", ".join(labels[role] for role in unknown_roles)
        state["settingsWarning"] = (
            f"This older run did not record the {names} model settings. "
            "Those roles will use the current defaults if you continue."
        )

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
        path = run_dir / filename
        if path.is_file():
            try:
                state["output"] = path.read_text(encoding="utf-8")
                break
            except (OSError, UnicodeError):
                pass
    if state["statementReviewOnly"] and state["review"]:
        state["output"] = state["review"].get("statement", "")
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
    try:
        checkpoint = json.loads((run_dir / PAUSE_FILENAME).read_text(encoding="utf-8"))
        if checkpoint["status"] in {"paused", "pausing"} and not (run_dir / "final.tex").is_file() and manual_stop is None:
            state.update(phase="paused", stage=checkpoint["stage"], activeNode=checkpoint["node"],
                         error=checkpoint.get("error", ""))
            if checkpoint.get("goalThreadId"):
                state["goalThreadId"] = checkpoint["goalThreadId"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    state["checkpoints"] = saved_run_checkpoints(run_dir)
    app._checkpoint_cache = list(state["checkpoints"])
    app._checkpoint_cache_signature = checkpoint_artifact_signature(run_dir)
    app.state = state
    app.fixed_trace = False
    return app


def restore_saved_jobs(runs):
    """Load every historical run as an idle job after a Web UI restart."""

    jobs = {}
    for transcript in sorted(Path(runs).glob("*/transcript.jsonl")):
        if transcript.is_symlink() or not direct_run_directory(
            transcript.parent, runs
        ):
            continue
        app = restore_saved_app(App(transcript, runs))
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
    statement = ""
    checked_path = run_dir / "checked-statement.md"
    if not statement and checked_path.exists():
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
    elif not statement:
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
    for role in ("author", "critic", "writer"):
        if f"{role}Model" in settings:
            settings[f"{role}Model"] = restored_model(settings[f"{role}Model"])
    defaults = default_prompts()
    for role in ("author", "critic", "final"):
        path = source / "prompts" / f"{role}.txt"
        settings[f"{role}Prompt"] = (
            read_utf8(path, f"saved {role} prompt") if path.exists()
            else defaults[role]
        )
    return {"run_dir": source, "statement": saved_statement(source), "settings": settings}


class Server(ThreadingHTTPServer):
    """Keep independent jobs alive while browser tabs come and go."""

    def __init__(self, address, trace_file=None, runs=RUNS):
        self.runs = Path(runs)
        self.app = App(trace_file, self.runs)
        self.fixed_app = trace_file is not None
        self.jobs, self.jobs_lock = {}, threading.RLock()
        super().__init__(address, Handler)
        if not self.fixed_app:
            self.jobs.update(restore_saved_jobs(self.runs))
            if self.jobs:
                self.app = max(
                    self.jobs.values(),
                    key=lambda item: item.state.get("lastActivityAt", ""),
                )
        # Only the unguessable launch URL can create this server's session.
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://{HOST}:{self.server_port}"

    def get_job(self, run_id):
        """Return one exact job; tests may use their fixed single app."""

        if not run_id and self.fixed_app:
            return self.app
        with self.jobs_lock:
            app = self.jobs.get(run_id)
        if not app:
            raise ValueError("That job is not available in this TCS Prover session.")
        return app

    def start_job(self, body, run_id=""):
        """Create a new job, or revise the selected existing job."""

        app = self.get_job(run_id) if run_id else (
            self.app if self.fixed_app else App(runs=self.runs)
        )
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
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        self.app = app  # Preserve the small single-app testing interface.
        return app


    def start_direct_job(self, body, run_id=""):
        """Create a statement job that starts directly with the author."""

        if run_id and not self.fixed_app:
            raise ValueError("Start a direct proof from the home screen.")
        app = self.get_job(run_id) if run_id else (
            self.app if self.fixed_app else App(runs=self.runs)
        )
        legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
        app.start_direct_statement(
            statement=body.get("statement", ""),
            critic_rounds=body.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
            thinking_hours=body.get("thinkingHours", DEFAULT_THINKING_HOURS),
            author_model=body.get("authorModel", DEFAULT_AUTHOR_MODEL),
            critic_model=body.get("criticModel", DEFAULT_CRITIC_MODEL),
            writer_model=body.get("writerModel", DEFAULT_WRITER_MODEL),
            reasoning_effort=legacy_effort,
            author_effort=body.get("authorEffort", legacy_effort),
            critic_effort=body.get("criticEffort", legacy_effort),
            writer_effort=body.get("writerEffort", legacy_effort),
            **web_prompt_options(body, ("author", "critic", "final")),
            speed_mode=body.get("speedMode", DEFAULT_SPEED),
            reasoning_summary=body.get(
                "reasoningSummary", DEFAULT_REASONING_SUMMARY
            ),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        self.app = app
        return app

    def start_latex_job(self, body, run_id=""):
        """Create a job that runs only the final LaTeX editor."""

        if run_id and not self.fixed_app:
            raise ValueError("Start LaTeX-only editing from the home screen.")
        app = self.get_job(run_id) if run_id else (
            self.app if self.fixed_app else App(runs=self.runs)
        )
        legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
        app.start_latex_only(
            source=body.get("content", ""),
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
        self.app = app
        return app

    def start_saved_critic_job(
        self, source_run, settings=None, include_audit_checkpoint=True,
    ):
        """Create a browser-visible job from one saved complete proof."""

        settings = dict(settings or {})
        source = saved_critic_source(source_run)
        with self.jobs_lock:
            self._check_author_workspace_idle(source["run_dir"])
        app = App(runs=self.runs)
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
        self.app = app
        return app

    def start_saved_research_job(self, source_run, settings=None):
        """Continue saved author work with its original prompt and a fresh budget."""

        source = saved_research_source(source_run)
        with self.jobs_lock:
            self._check_author_workspace_idle(source["run_dir"])
        options = {**source["settings"], **(settings or {})}
        app = App(runs=self.runs)
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
        )
        self._queue_saved_author_instructions(source["run_dir"], app)
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        self.app = app
        return app

    def _check_author_workspace_idle(self, source_run):
        """Keep two author processes from updating one shared workspace."""

        workspace = saved_goal_options(source_run)["goalWorkspace"]
        for previous in self.jobs.values():
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
            self._check_author_workspace_idle(app.run_dir)
            app.resume()
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
        if state.get("phase") == "paused":
            return {"action": "resume", "label": "Resume", "description": "Continue in the same run folder using the saved author session and notebooks."}
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
        if plan["action"] == "critic":
            continued = self.resume_critic_job(
                source_state["runId"], include_audit_checkpoint=True,
            )
            if isinstance(continued, App):
                self._queue_saved_author_instructions(source_app, continued)
            return continued

        app = App(runs=self.runs)
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
        self.app = app
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
                )
            }
            job["title"] = (
                state["problemDescription"].strip().split("\n", 1)[0]
                if state["problemMode"] == "algorithmic"
                else state["draft"].strip().split("\n", 1)[0]
            )
            run_dir = app.run_dir
            checkpoints = state.get("checkpoints", [])
            job["checkpoints"] = checkpoints
            job["canResumeCritic"] = bool(
                state["phase"] not in {"reviewing", "running", "stopping", "pausing", "paused"}
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
                if (
                    app.state["phase"] in {"reviewing", "running", "stopping", "pausing"}
                    or app.worker_token is not None
                ):
                    raise ValueError("Stop this job before deleting it.")
                folder = app.run_dir
                if not folder or folder.resolve().parent != self.runs.resolve():
                    raise ValueError("The job folder is outside the runs directory.")
                for other in self.jobs.values():
                    if other is app or not (other.has_active_worker() or other.state["phase"] in {
                        "reviewing", "running", "stopping", "pausing",
                    }):
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
                folder.rename(target)
                del self.jobs[run_id]
        return {"deleted": run_id}

    def stop_all(self):
        """Interrupt every live child when the local server itself closes."""

        with self.jobs_lock:
            apps = list({id(app): app for app in self.jobs.values()}.values())
        for app in apps:
            if app.process:
                try:
                    app.process.send_signal(signal.SIGINT)
                except OSError:
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
        query = parse_qs(request.query)
        run_id = (query.get("job") or [""])[0]
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            return self.send({"error": "Untrusted local host."}, status=403)
        if request.path == "/download-tex":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            try:
                return self.send(self.server.get_job(run_id).final_tex(), "application/x-tex; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="final.tex"'})
            except (ValueError, OSError) as exc:
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
                if not run_id and not self.server.fixed_app:
                    state = empty_state()
                    state["traceFrom"] = 0
                    return self.send(state)
                values = query.get("after")
                after = int(values[0]) if values else None
                return self.send(self.server.get_job(run_id).snapshot(after))
            except ValueError:
                return self.send({"error": "Invalid job or transcript position."}, status=400)
        if request.path == "/jobs":
            if not self.authorized():
                return self.send({"error": "Open TCS Prover from its launch URL."}, status=403)
            return self.send({"jobs": self.server.job_list()})
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
        }
        if request.path not in files or (request.query and request.path != "/"):
            return self.send({"error": "Not found."}, status=404)
        name, kind = files[request.path]
        self.send((UI / name).read_bytes(), kind)

    def do_POST(self):
        """Call one workflow action and return its new state."""

        try:
            request = urlsplit(self.path)
            query = parse_qs(request.query)
            run_id = (query.get("job") or [""])[0]
            if not self.authorized():
                return self.send({"error": "Unauthorized local request."}, status=403)
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a JSON request.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 100_000:
                raise ValueError("Request is too large.")
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            if request.path == "/delete-job":
                return self.send(self.server.delete_job(run_id))
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
                "/resume-critic", "/resume-checkpoint", "/continue-stopped", "/resume",
            }:
                return self.send({"error": "Not found."}, status=404)
            self.send(app.snapshot())
        except (OSError, TypeError, ValueError) as exc:
            self.send({"error": str(exc)}, status=400)
