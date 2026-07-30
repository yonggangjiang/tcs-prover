#!/usr/bin/env python3
"""Serve the small local browser interface for TCS Prover."""

import argparse
import errno
import json
import re
import signal
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import tcs_agent


# Everything is local and uses only Python's standard library.
ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui"
RUNS = ROOT / "runs"
ALGORITHMIC = ROOT / "algorithmic"
HOST, PORT = "127.0.0.1", 8765
DEFAULT_CRITIC_ROUNDS, MAX_CRITIC_ROUNDS = 4, 100
DEFAULT_THINKING_HOURS, MAX_THINKING_HOURS = 8, 168
MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_REVIEW_MODEL = "gpt-5.6-sol"
DEFAULT_AUTHOR_MODEL = DEFAULT_CRITIC_MODEL = DEFAULT_WRITER_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "ultra"


def load_algorithmic_catalog(path):
    """Load one strict list of named algorithmic-mode descriptions."""

    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read algorithmic preset file {path}: {exc}") from exc
    if not isinstance(values, list) or not values:
        raise ValueError(f"Algorithmic preset file {path} must be a nonempty list.")
    result, names = [], set()
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict) or set(value) != {"name", "description"}:
            raise ValueError(
                f"Entry {index} in {path} must contain only name and description."
            )
        name, description = value["name"], value["description"]
        if not all(isinstance(item, str) and item.strip() for item in (
            name, description,
        )):
            raise ValueError(
                f"Entry {index} in {path} needs a nonempty name and description."
            )
        name, description = name.strip(), description.strip()
        if "\0" in name or "\0" in description:
            raise ValueError(f"Entry {index} in {path} cannot contain NUL characters.")
        name_key = name.casefold()
        if name_key in names:
            raise ValueError(f"Algorithmic preset name {name!r} is duplicated in {path}.")
        names.add(name_key)
        result.append({"name": name, "description": description})
    return result


def optional_algorithmic_catalog(path):
    """Keep manual algorithmic input usable if a local preset file is broken."""

    try:
        return load_algorithmic_catalog(path)
    except ValueError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return []


MODEL_PRESETS = optional_algorithmic_catalog(ALGORITHMIC / "model.json")
PROBLEM_PRESETS = optional_algorithmic_catalog(ALGORITHMIC / "problem.json")
DEFAULT_PROMPTS = {
    "review": tcs_agent.REVIEW_PROMPT,
    "author": tcs_agent.TEMPLATE.read_text(encoding="utf-8"),
    "critic": tcs_agent.CRITIC_PROMPT,
    "final": tcs_agent.FINAL_PROMPT,
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

# UI-only labels mirror the four runtime stages and the statement review.
PUBLIC_GRAPH = {
    "settings": {
        "model": "gpt-5.6-sol",
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "reasoning_efforts": list(EFFORTS),
        "review_model": DEFAULT_REVIEW_MODEL,
        "review_models": list(REVIEW_MODELS),
        "models": list(MODELS),
        "review_reasoning_effort": DEFAULT_REASONING_EFFORT,
        "revision_reasoning_effort": DEFAULT_REASONING_EFFORT,
        "prompts": DEFAULT_PROMPTS,
        "algorithmic_presets": {
            "models": MODEL_PRESETS,
            "problems": PROBLEM_PRESETS,
        },
        "model_summary": "Sol/Ultra review · Sol/Ultra author, critic, writer",
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
            "description": "Writes the proof and revises it after critic rejection.",
        },
        "failure_summary": {
            "label": "Failure summary", "short_label": "Summary",
            "stage": "failure",
            "description": "Preserves progress when the author time limit is reached.",
        },
        "critic": {
            "label": "Independent critic", "short_label": "Critic",
            "stage": "critic",
            "description": "Audits the latest proof. A clean PASS is the only exit.",
        },
        "latex_editor": {
            "label": "LaTeX editor", "short_label": "Polish", "stage": "final",
            "description": "Runs only after the critic gives a clean PASS.",
        },
    },
    "edges": [
        {
            "from": "statement_reviewer", "to": "author",
            "label": "Approve statement", "when": "user approves",
            "prompt_change": "Insert the approved statement into prompt.txt.",
        },
        {
            "from": "author", "to": "critic",
            "label": "Audit candidate", "when": "author writes or revises the proof",
            "prompt_change": "Send the latest candidate to a fresh critic.",
        },
        {
            "from": "author", "to": "failure_summary",
            "label": "Summarize failure", "when": "author time limit is reached",
            "prompt_change": "Stop solving and summarize progress and obstacles.",
        },
        {
            "from": "critic", "to": "critic",
            "label": "Recheck repair", "when": "critic fixes all bugs",
            "prompt_change": "Send the repaired solution to a fresh critic.",
        },
        {
            "from": "critic", "to": "author",
            "label": "Return bugs", "when": "critic rejects",
            "prompt_change": "Send unresolved bugs back to the proof author.",
        },
        {
            "from": "critic", "to": "latex_editor",
            "label": "Final edit", "when": "critic gives a clean pass",
            "prompt_change": "Send the latest complete solution to the LaTeX editor.",
        },
    ],
}

ALGORITHMIC_GRAPH = {
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


def algorithmic_statement(model_of_computation, problem_description, goal):
    """Validate and combine the three fields that define an algorithmic task."""

    values = []
    for value, message in (
        (model_of_computation, "Enter the model of computation."),
        (problem_description, "Enter the problem description."),
        (goal, "Enter the asymptotic upper- or lower-bound goal."),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(message)
        value = value.strip()
        if "\0" in value:
            raise ValueError("Algorithmic problem fields cannot contain NUL characters.")
        values.append(value)
    model_of_computation, problem_description, goal = values
    return (
        f"MODEL OF COMPUTATION:\n{model_of_computation}\n\n"
        f"PROBLEM DESCRIPTION:\n{problem_description}\n\n"
        f"GOAL (ASYMPTOTIC UPPER OR LOWER BOUND):\n{goal}"
    )


def empty_state(trace=None, trace_version=0):
    """Return the complete, intentionally small UI state."""

    return {
        "phase": "input",
        "problemMode": "statement",
        "draft": "",
        "modelOfComputation": "",
        "problemDescription": "",
        "goal": "",
        "review": None,
        "reviewModel": DEFAULT_REVIEW_MODEL,
        "authorModel": DEFAULT_AUTHOR_MODEL,
        "criticModel": DEFAULT_CRITIC_MODEL,
        "writerModel": DEFAULT_WRITER_MODEL,
        "reviewEffort": DEFAULT_REASONING_EFFORT,
        "authorEffort": DEFAULT_REASONING_EFFORT,
        "criticEffort": DEFAULT_REASONING_EFFORT,
        "writerEffort": DEFAULT_REASONING_EFFORT,
        # Kept for old clients; new clients send one effort per role.
        "reasoningEffort": DEFAULT_REASONING_EFFORT,
        "reviewPrompt": DEFAULT_PROMPTS["review"],
        "authorPrompt": DEFAULT_PROMPTS["author"],
        "criticPrompt": DEFAULT_PROMPTS["critic"],
        "finalPrompt": DEFAULT_PROMPTS["final"],
        "criticRounds": DEFAULT_CRITIC_ROUNDS,
        "thinkingHours": DEFAULT_THINKING_HOURS,
        "stage": "",
        "activeNode": "",
        "round": 0,
        "startedAt": "",
        "lastActivityAt": "",
        "output": "",
        "error": "",
        "runId": "",
        "workflow": PUBLIC_GRAPH,
        "trace": trace if trace is not None else [],
        "traceVersion": trace_version,
    }


class App:
    """Own one review-and-solve workflow."""

    def __init__(self, trace_file=None, runs=RUNS):
        # Tests may supply one fixed log; the real app uses one folder per run.
        self.runs, self.fixed_trace = Path(runs), trace_file is not None
        self.trace_file = Path(trace_file) if trace_file else self._latest_trace()
        self.run_dir = self.trace_file.parent if self.trace_file else None
        trace, total, self.pinned = self._load_trace()
        self.state = empty_state(trace, total)
        self.state["runId"] = self.run_dir.name if self.run_dir else ""
        self.process = None
        self.lock = threading.RLock()

    def _latest_trace(self):
        """Find the most recently changed run log, if one exists."""

        if not self.runs.exists():
            return None
        logs = self.runs.glob("*/transcript.jsonl")
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
            self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.runs.chmod(0o700)
            self.run_dir = self.runs / f"{stamp}_{slug}"
            number = 2
            while self.run_dir.exists():
                self.run_dir = self.runs / f"{stamp}_{slug}-{number}"
                number += 1
            self.run_dir.mkdir(mode=0o700)
            self.trace_file = self.run_dir / "transcript.jsonl"
        self.run_dir.chmod(0o700)
        self._save("draft.md", f"# Draft problem\n\n{statement}\n")

    def _save(self, name, content):
        """Save one private, human-readable run artifact."""

        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

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

    def snapshot(self, after=None):
        """Copy state, optionally returning only newly appended records."""

        with self.lock:
            state = dict(self.state)
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
    ):
        """Normalize and validate settings shared by both input modes."""

        review_model = str(review_model or "")
        author_model = str(author_model or "")
        critic_model = str(critic_model or "")
        writer_model = str(writer_model or "")
        reasoning_effort = str(reasoning_effort or "")
        review_effort = str(review_effort or reasoning_effort)
        author_effort = str(author_effort or reasoning_effort)
        critic_effort = str(critic_effort or reasoning_effort)
        writer_effort = str(writer_effort or reasoning_effort)
        supplied_prompts = {
            "review": review_prompt, "author": author_prompt,
            "critic": critic_prompt, "final": final_prompt,
        }
        prompts = {
            name: str(
                DEFAULT_PROMPTS[name] if value is None else value
            ).strip()
            for name, value in supplied_prompts.items()
        }
        if include_review and review_model not in REVIEW_MODELS:
            raise ValueError("Choose Sol, Terra, or Luna for statement review.")
        if any(
            model not in MODELS
            for model in (author_model, critic_model, writer_model)
        ):
            raise ValueError("Choose Sol, Terra, or Luna for every proof stage.")
        efforts = [author_effort, critic_effort, writer_effort]
        if include_review:
            efforts.append(review_effort)
        if any(effort not in EFFORTS for effort in efforts):
            raise ValueError("Choose a valid reasoning effort for every role.")
        required_prompts = [prompts[name] for name in ("author", "critic", "final")]
        if include_review:
            required_prompts.append(prompts["review"])
        if any(not prompt for prompt in required_prompts):
            raise ValueError("Every role prompt must contain instructions.")
        if prompts["author"].count(tcs_agent.MARKER) != 1:
            raise ValueError(
                f"The author prompt must contain exactly one {tcs_agent.MARKER}."
            )
        try:
            critic_rounds = int(critic_rounds)
        except (TypeError, ValueError) as exc:
            raise ValueError("The critic round limit must be an integer.") from exc
        if not 1 <= critic_rounds <= MAX_CRITIC_ROUNDS:
            raise ValueError(f"Choose 1 to {MAX_CRITIC_ROUNDS} critic rounds.")
        try:
            thinking_hours = float(thinking_hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("The author time limit must be a number.") from exc
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
                review_effort if include_review else DEFAULT_REASONING_EFFORT
            ),
            "authorEffort": author_effort,
            "criticEffort": critic_effort,
            "writerEffort": writer_effort,
            "reviewPrompt": (
                prompts["review"] if include_review else DEFAULT_PROMPTS["review"]
            ),
            "authorPrompt": prompts["author"],
            "criticPrompt": prompts["critic"],
            "finalPrompt": prompts["final"],
            "criticRounds": critic_rounds,
            "thinkingHours": thinking_hours,
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
    ):
        """Start the review and return immediately so the page can poll."""

        statement = str(statement).strip()
        feedback = str(feedback or "").strip()
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
        )
        if not statement:
            raise ValueError("Enter a problem statement.")
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping"}:
                raise ValueError("Codex is already working.")
            # Feedback retries stay in the same problem folder.
            retry = self.state["phase"] == "reviewed" and self.run_dir is not None
            if not retry:
                self._new_run(statement)
                if not self.fixed_trace:
                    self.pinned = []
            for name in ("review", "author", "critic", "final"):
                self._save(f"prompts/{name}.txt", options[f"{name}Prompt"] + "\n")
            trace = self.state["trace"] if retry or self.fixed_trace else []
            version = self.state["traceVersion"] if retry or self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "phase": "reviewing",
                "draft": statement,
                **options,
                "activeNode": "statement_reviewer",
                "stage": "review",
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
        threading.Thread(
            target=self._review,
            args=(
                statement, feedback, options["reviewModel"],
                options["reviewEffort"],
            ),
            daemon=True,
        ).start()

    def _review(
        self, statement, feedback="", review_model=DEFAULT_REVIEW_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
    ):
        """Stream the review transcript and keep its final structured result."""

        process, report, problem, visible = None, None, "", []
        # A stop click can arrive before this background thread starts.
        with self.lock:
            if self.state["phase"] == "stopping":
                self.state.update(
                    phase="done", error="Stopped.",
                    output="Codex was stopped before it produced a checked statement.",
                )
                return
        try:
            process = subprocess.Popen(
                [
                    sys.executable, str(ROOT / "tcs_agent.py"), "review",
                    "--review-model", review_model,
                    "--review-effort", reasoning_effort,
                    "--review-prompt-file",
                    str(self.run_dir / "prompts/review.txt"),
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=ROOT,
            )
            with self.lock:
                self.process = process
                stop_now = self.state["phase"] == "stopping"
            process.stdin.write(statement + ("\0" + feedback if feedback else ""))
            process.stdin.close()
            # Honor a stop that arrived while Popen was attaching.
            if stop_now:
                try:
                    process.send_signal(signal.SIGINT)
                except OSError:
                    pass
            for line in process.stdout:
                record = self.parse_line(line, "review")
                self.add_trace(record)
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
                problem = "Review failed. See the transcript for details."
        except (OSError, TypeError, AttributeError) as exc:
            problem = str(exc)
        finally:
            # Reap a child even if a quick stop breaks its stdin or transcript.
            try:
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            except OSError:
                pass
        with self.lock:
            if self.process is process:
                self.process = None
            valid = isinstance(report, dict) and all(
                isinstance(report.get(key), str) for key in ("statement", "notes")
            )
            stopped = self.state["phase"] == "stopping"
            if valid:
                self._save(
                    "checked-statement.md",
                    f"# Checked statement\n\n{report['statement']}\n\n"
                    f"# Reviewer notes\n\n{report['notes'] or 'None.'}\n",
                )
                # A completed review wins a stop/exit race.
                self.state.update(phase="reviewed", review=report, error="", output="")
            elif stopped:
                self.state["phase"], self.state["error"] = "done", "Stopped."
                self.state["output"] = self.state["output"] or (
                    "Codex was stopped before it produced a checked statement."
                )
            elif problem:
                self.state["phase"], self.state["error"] = "input", problem

    def _launch_solver_locked(self, statement):
        """Attach the existing proof pipeline; the caller holds ``self.lock``."""

        process = subprocess.Popen(
            [
                sys.executable, "-u", str(ROOT / "tcs_agent.py"), "solve",
                "--author-model", self.state["authorModel"],
                "--critic-model", self.state["criticModel"],
                "--writer-model", self.state["writerModel"],
                "--reasoning-effort", self.state["reasoningEffort"],
                "--author-effort", self.state["authorEffort"],
                "--critic-effort", self.state["criticEffort"],
                "--writer-effort", self.state["writerEffort"],
                "--author-prompt-file",
                str(self.run_dir / "prompts/author.txt"),
                "--critic-prompt-file",
                str(self.run_dir / "prompts/critic.txt"),
                "--final-prompt-file",
                str(self.run_dir / "prompts/final.txt"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Goal mode writes its durable proof files into this run only.
            cwd=self.run_dir,
        )
        self.process = process
        try:
            process.stdin.write(
                f"{statement}\0{self.state['criticRounds']}"
                f"\0{self.state['thinkingHours']}"
            )
            process.stdin.close()
        except (OSError, TypeError, ValueError):
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            except OSError:
                pass
            if self.process is process:
                self.process = None
            raise
        self.state.update(
            phase="running", stage="solve",
            activeNode="author", round=0, output="",
        )
        return process

    def start_algorithmic(
        self, model_of_computation, problem_description, goal,
        critic_rounds=DEFAULT_CRITIC_ROUNDS,
        thinking_hours=DEFAULT_THINKING_HOURS,
        author_model=DEFAULT_AUTHOR_MODEL,
        critic_model=DEFAULT_CRITIC_MODEL,
        writer_model=DEFAULT_WRITER_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        author_effort=None, critic_effort=None, writer_effort=None,
        author_prompt=None, critic_prompt=None, final_prompt=None,
    ):
        """Start an algorithmic task directly at the proof-author stage."""

        statement = algorithmic_statement(
            model_of_computation, problem_description, goal
        )
        fields = {
            "modelOfComputation": model_of_computation.strip(),
            "problemDescription": problem_description.strip(),
            "goal": goal.strip(),
        }
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
            include_review=False,
        )
        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping"}:
                raise ValueError("Codex is already working.")
            self._new_run(statement, fields["problemDescription"])
            if not self.fixed_trace:
                self.pinned = []
            for name in ("author", "critic", "final"):
                self._save(f"prompts/{name}.txt", options[f"{name}Prompt"] + "\n")
            self._save("algorithmic-problem.md", statement + "\n")
            self._save(
                "algorithmic-input.json",
                json.dumps(fields, ensure_ascii=False, indent=2) + "\n",
            )
            trace = self.state["trace"] if self.fixed_trace else []
            version = self.state["traceVersion"] if self.fixed_trace else 0
            self.state = {
                **empty_state(trace, version),
                "problemMode": "algorithmic",
                "draft": statement,
                **fields,
                **options,
                "workflow": ALGORITHMIC_GRAPH,
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "runId": self.run_dir.name,
            }
            process = self._launch_solver_locked(statement)
        threading.Thread(
            target=self._read_output, args=(process,), daemon=True
        ).start()

    def approve(self, edited_statement=None):
        """Solve the reviewed statement, including any direct author edit."""

        with self.lock:
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
            process = self._launch_solver_locked(statement)
        threading.Thread(target=self._read_output, args=(process,), daemon=True).start()

    def _read_output(self, process):
        """Store tagged solver events and build the visible final answer."""

        problem, answers, order, final = "", {}, [], False
        try:
            for line in process.stdout:
                # Untagged errors belong to the most recently active stage.
                record = self.parse_line(line, self.state["stage"] or "solve")
                self.add_trace(record)
                if (
                    record.get("kind") == "diagnostic"
                    and record.get("text", "").startswith("error: ")
                ):
                    problem = record["text"][7:]
                record_stage, record_node = record.get("stage"), record.get("node")
                stages = {
                    stage: name
                    for name, item in PUBLIC_GRAPH["nodes"].items()
                    for stage in item.get("stages", [item["stage"]])
                }
                if record_stage in stages:
                    with self.lock:
                        self.state["stage"] = record_stage
                        self.state["activeNode"] = record_node or stages[record_stage]
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
                if record.get("kind") == "final_result":
                    with self.lock:
                        self.state["output"] = record.get("output", "")
                        final = True
                        self._save("final.tex", self.state["output"])
                    continue
                if record.get("kind") == "failure_result":
                    with self.lock:
                        self.state["output"] = record.get("output", "")
                        final = True
                        self._save("failure-summary.md", self.state["output"])
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
                if item_id not in answers:
                    order.append(item_id)
                    answers[item_id] = ""
                answers[item_id] = answers[item_id] + answer if append else answer
                with self.lock:
                    self.state["output"] = "".join(answers[key] for key in order)
            code = process.wait()
        except OSError as exc:
            problem = str(exc)
            process.terminate()
            code = process.wait()
        with self.lock:
            stopped = self.state["phase"] == "stopping"
            self.process = None
            self.state["phase"] = "done"
            if stopped and code:
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
            if self.state["phase"] in {"reviewing", "running", "stopping"}:
                raise ValueError("Wait for the current Codex request to finish.")
            self.state["trace"] = []
            self.state["traceVersion"] = 0
            self.pinned = []
            if self.trace_file:
                self.trace_file.unlink(missing_ok=True)

    def stop(self):
        """Stop either Codex stage while retaining its visible output."""

        with self.lock:
            if self.state["phase"] == "stopping":
                return
            if self.state["phase"] not in {"reviewing", "running"}:
                raise ValueError("Codex is not working.")
            process, self.state["phase"] = self.process, "stopping"
        if process:
            try:
                process.send_signal(signal.SIGINT)
            except OSError:
                pass

    def reset(self):
        """Return to the first screen when no child is active."""

        with self.lock:
            if self.state["phase"] in {"reviewing", "running", "stopping"}:
                raise ValueError("Stop the current work first.")
            trace, version = self.state["trace"], self.state["traceVersion"]
            self.state = empty_state(trace, version)
            self.state["runId"] = self.run_dir.name if self.run_dir else ""


class Server(ThreadingHTTPServer):
    """Keep independent jobs alive while browser tabs come and go."""

    def __init__(self, address, trace_file=None, runs=RUNS):
        self.runs = Path(runs)
        self.app = App(trace_file, self.runs)
        self.fixed_app = trace_file is not None
        self.jobs, self.jobs_lock = {}, threading.RLock()
        super().__init__(address, Handler)
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
        new_settings = any(key in body for key in (
            "reviewEffort", "authorEffort", "criticEffort", "writerEffort",
            "reviewPrompt", "authorPrompt", "criticPrompt", "finalPrompt",
        ))
        if not new_settings:
            app.start_review(
                body.get("statement", ""), body.get("feedback", ""),
                body.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
                body.get("reviewModel", DEFAULT_REVIEW_MODEL),
                body.get("thinkingHours", DEFAULT_THINKING_HOURS),
                body.get("authorModel", DEFAULT_AUTHOR_MODEL),
                body.get("criticModel", DEFAULT_CRITIC_MODEL),
                body.get("writerModel", DEFAULT_WRITER_MODEL),
                legacy_effort,
            )
            with self.jobs_lock:
                self.jobs[app.state["runId"]] = app
            self.app = app
            return app
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
            review_effort=body.get("reviewEffort", legacy_effort),
            author_effort=body.get("authorEffort", legacy_effort),
            critic_effort=body.get("criticEffort", legacy_effort),
            writer_effort=body.get("writerEffort", legacy_effort),
            review_prompt=body.get("reviewPrompt"),
            author_prompt=body.get("authorPrompt"),
            critic_prompt=body.get("criticPrompt"),
            final_prompt=body.get("finalPrompt"),
        )
        with self.jobs_lock:
            self.jobs[app.state["runId"]] = app
        self.app = app  # Preserve the small single-app testing interface.
        return app

    def start_algorithmic_job(self, body, run_id=""):
        """Create an algorithmic job that starts directly with the author."""

        if run_id and not self.fixed_app:
            raise ValueError("Start an algorithmic problem from the home screen.")
        app = self.get_job(run_id) if run_id else (
            self.app if self.fixed_app else App(runs=self.runs)
        )
        legacy_effort = body.get("reasoningEffort", DEFAULT_REASONING_EFFORT)
        app.start_algorithmic(
            model_of_computation=body.get("modelOfComputation", ""),
            problem_description=body.get("problemDescription", ""),
            goal=body.get("goal", ""),
            critic_rounds=body.get("criticRounds", DEFAULT_CRITIC_ROUNDS),
            thinking_hours=body.get("thinkingHours", DEFAULT_THINKING_HOURS),
            author_model=body.get("authorModel", DEFAULT_AUTHOR_MODEL),
            critic_model=body.get("criticModel", DEFAULT_CRITIC_MODEL),
            writer_model=body.get("writerModel", DEFAULT_WRITER_MODEL),
            reasoning_effort=legacy_effort,
            author_effort=body.get("authorEffort", legacy_effort),
            critic_effort=body.get("criticEffort", legacy_effort),
            writer_effort=body.get("writerEffort", legacy_effort),
            author_prompt=body.get("authorPrompt"),
            critic_prompt=body.get("criticPrompt"),
            final_prompt=body.get("finalPrompt"),
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
                    "lastActivityAt", "error", "problemMode",
                )
            }
            job["title"] = (
                state["problemDescription"].strip().split("\n", 1)[0]
                if state["problemMode"] == "algorithmic"
                else state["draft"].strip().split("\n", 1)[0]
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
                if app.state["phase"] in {"reviewing", "running", "stopping"}:
                    raise ValueError("Stop this job before deleting it.")
                folder = app.run_dir
                if not folder or folder.resolve().parent != self.runs.resolve():
                    raise ValueError("The job folder is outside the runs directory.")
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
            if request.path == "/review":
                app = self.server.start_job(body, run_id)
            elif request.path == "/algorithmic":
                app = self.server.start_algorithmic_job(body, run_id)
            else:
                app = self.server.get_job(run_id)
            if request.path == "/approve":
                app.approve(body.get("statement"))
            elif request.path == "/stop":
                app.stop()
            elif request.path == "/reset":
                app.reset()
            elif request.path == "/clear-trace":
                app.clear_trace()
            elif request.path not in {"/review", "/algorithmic"}:
                return self.send({"error": "Not found."}, status=404)
            self.send(app.snapshot())
        except (OSError, TypeError, ValueError) as exc:
            self.send({"error": str(exc)}, status=400)


def main():
    """Start locally and open the user's default browser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = Server((HOST, PORT))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(
            f"Cannot start: local port {PORT} is already in use. "
            "Close the existing process and try again.",
            file=sys.stderr,
        )
        return 1
    # A URL fragment stays in the browser; JavaScript sends it as a secret header.
    url = f"{server.origin}/#{server.token}"
    print(f"TCS Prover is ready at {url}\nPress Ctrl-C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping TCS Prover…")
        server.stop_all()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
