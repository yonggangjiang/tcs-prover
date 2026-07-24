#!/usr/bin/env python3
"""Review a TCS statement or run an approved statement as a Codex goal."""

import argparse
import json
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
AUTHOR_MODEL = CRITIC_MODEL = WRITER_MODEL = MODEL

# Every role defaults to Sol and Ultra; both choices are configurable.
REVIEW_MODEL = MODEL
REVIEW_MODELS = MODELS
REVIEW_EFFORT = EFFORT

# These values connect the approved statement to the user's prompt template.
MARKER, TEMPLATE = "[STATEMENT]", ROOT / "prompt.txt"
GOAL = "Complete the task supplied in the first turn and continue until done."
DEFAULT_CRITIC_ROUNDS, MAX_CRITIC_ROUNDS = 4, 100
DEFAULT_AUTHOR_HOURS, MAX_AUTHOR_HOURS = 8, 168
INTERRUPT_GRACE_SECONDS = 30
SUMMARY_GRACE_SECONDS = 300
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
    },
    "required": ["checks", "verdict", "fixed", "solution", "bugs"],
    "additionalProperties": False,
}
FINAL_SCHEMA = {
    "type": "object",
    "properties": {"latex": {"type": "string"}},
    "required": ["latex"],
    "additionalProperties": False,
}
REVIEW_PROMPT = """
Rewrite the draft as a rigorous, self-contained theoretical-computer-science
problem statement without changing its intended claim. Check quantifiers,
models, encodings, parameters, promises, asymptotics, and corner cases. Fix
clear typos or omissions that make the goal trivial, impossible, or false. If
the intended repair is uncertain, explain that plainly in notes so the author
can edit and retry. Return only the requested JSON.
""".strip()
# A feedback pass uses the same instructions, changing only Rewrite to Revise.
REVISION_PROMPT = REVIEW_PROMPT.replace("Rewrite", "Revise", 1)
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
""".strip()
FINAL_PROMPT = """
Act as the TCS editor and turn the solution below into a latex proof. Preserve its mathematical
content while removing repetition and process commentary. Produce a
cleaned-up, self-contained, organized, rigorous, readable LaTeX proof
with clearly stated theorems and logically ordered sections that is considered as a well-written TCS paper.
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
        raise Error("The author time limit must be a number of hours.") from exc
    if not 0 < value <= MAX_AUTHOR_HOURS:
        raise Error(f"Choose more than 0 and at most {MAX_AUTHOR_HOURS} hours.")
    return value


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
        revision = instructions.replace("Rewrite", "Revise", 1)
        return (
            f"{revision}\n\nCURRENT CHECKED STATEMENT:\n{text(draft)}"
            f"\n\nAUTHOR REVISION REQUEST:\n{feedback}"
        )
    return f"{instructions}\n\nDRAFT:\n{text(draft)}"


def structured(prompt, schema_value, stage, model=MODEL, effort=EFFORT):
    """Run one read-only structured Codex call and relay its visible events."""

    emit(
        "request", stage, label=f"Exact {stage} input", text=prompt,
        model=model, reasoningEffort=effort, reasoningSummary="detailed",
        responseSchema=schema_value,
    )
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        schema = folder / "schema.json"
        answer = folder / "answer.json"
        schema.write_text(json.dumps(schema_value), encoding="utf-8")
        command = [
            codex(), "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            "-c", 'model_reasoning_summary="detailed"',
            *(["--enable", "multi_agent"] if stage == "critic" else []),
            "-C", str(folder), "-s", "read-only", "-a", "never", "exec",
            "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
            "--output-schema", str(schema), "-o", str(answer), "-",
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=environment(),
        )
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = public_event(json.loads(line))
                    if event is not None:
                        emit("codex_event", stage, event=event)
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
    instructions=REVIEW_PROMPT,
):
    """Review with the user's chosen model."""

    try:
        chosen_model(model)
        effort = chosen_effort(effort)
        report, raw = structured(
            review_prompt(draft, feedback, instructions), SCHEMA, "review",
            model=model, effort=effort,
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


def criticize(
    statement, solution, round_number, model=CRITIC_MODEL, effort=EFFORT,
    instructions=CRITIC_PROMPT,
):
    """Have independent auditors guide one critic repair attempt."""

    prompt = (
        f"{text(instructions)}\n\nSTATEMENT:\n{text(statement)}"
        f"\n\nCANDIDATE SOLUTION (critic round {round_number}):\n{text(solution)}"
    )
    report, raw = structured(
        prompt, CRITIC_SCHEMA, "critic", model=chosen_model(model),
        effort=chosen_effort(effort),
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


def repair_prompt(statement, solution, bugs, round_number):
    """Give unresolved critic bugs back to the original author thread."""

    return f"""
The independent critic panel rejected candidate solution {round_number}.
The critic already applied every fix it could do confidently. Fix every
remaining bug below, then return a complete replacement solution rather than a
patch or discussion. Do not change the problem statement. Recheck all affected
steps and preserve correct details from the current candidate.

STATEMENT:
{text(statement)}

CURRENT CANDIDATE:
{text(solution)}

CRITIC BUGS:
{text(bugs)}
""".strip()


def finalize(
    statement, solution, model=WRITER_MODEL, effort=EFFORT,
    instructions=FINAL_PROMPT,
):
    """Use a fresh final editor to turn the latest solution into LaTeX."""

    prompt = (
        f"{text(instructions)}\n\nSTATEMENT:\n{text(statement)}"
        f"\n\nLATEST SOLUTION:\n{text(solution)}"
    )
    report, raw = structured(
        prompt, FINAL_SCHEMA, "final", model=chosen_model(model),
        effort=chosen_effort(effort),
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
):
    """Solve, require a clean critic pass, then LaTeX-edit the result."""

    critic_rounds = critic_limit(critic_rounds)
    thinking_hours = author_hours(thinking_hours)
    author_model = chosen_model(author_model)
    critic_model = chosen_model(critic_model)
    writer_model = chosen_model(writer_model)
    author_effort = chosen_effort(author_effort or effort)
    critic_effort = chosen_effort(critic_effort or effort)
    writer_effort = chosen_effort(writer_effort or effort)
    emit(
        "request", "solve", label="Exact solve input", text=prompt,
        model=author_model, reasoningEffort=author_effort, reasoningSummary="detailed",
    )
    emit(
        "request", "solve", label="Goal continuation instruction", text=GOAL,
        model=author_model, reasoningEffort=author_effort, reasoningSummary="detailed",
    )
    command = [codex(), "app-server", "--enable", "goals"]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=environment(),
    )

    # Keep the latest complete answer from the original author thread.
    answers, stage = [], "solve"

    # Every app-server response and notification passes through one filter.
    def record(message):
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
        if (
            root and message.get("method") == "item/completed"
            and item.get("type") in {"agentMessage", "agent_message"}
            and item.get("text")
        ):
            answers.append(item["text"])

    rpc, thread, stop_timer = RPC(process, record), None, None
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
            },
        })
        thread = started["thread"]["id"]

        # Paused avoids an empty automatic turn before the real prompt starts.
        goal = {"threadId": thread, "objective": GOAL}
        rpc.call("thread/goal/set", {**goal, "status": "paused"})
        started_turn = rpc.call("turn/start", {
            "threadId": thread,
            "input": [{"type": "text", "text": prompt}],
            "summary": "detailed",
        })
        rpc.call("thread/goal/set", {**goal, "status": "active"})
        current_turn = {"id": (started_turn.get("turn") or {}).get("id")}
        timed_out, stop_timer = threading.Event(), threading.Event()
        deadline_lock = threading.Lock()
        deadline = time.monotonic() + thinking_hours * 3600

        # Pause Goal mode first, then interrupt the active author turn.
        def enforce_deadline():
            if stop_timer.wait(max(0, deadline - time.monotonic())):
                return
            with deadline_lock:
                timed_out.set()
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
            # If interruption itself hangs, unblock the reader for a fallback.
            if (
                not stop_timer.wait(INTERRUPT_GRACE_SECONDS)
                and process.poll() is None
            ):
                process.terminate()

        timer = threading.Thread(target=enforce_deadline, daemon=True)
        timer.start()
        emit(
            "status", "solve", label="Goal started",
            text=f"Thread {thread}; author limit {thinking_hours:g} hours.",
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
                if time.monotonic() >= deadline:
                    timed_out.set()
                    break
                if answers:
                    last_author_output = answers[-1]
                    answers.clear()
                emit(
                    "request", "solve", label="Author continuation",
                    text=CONTINUE_PROMPT, model=author_model,
                    reasoningEffort=author_effort,
                    reasoningSummary="detailed",
                )
                # Explicit turns start while Goal mode is paused.
                rpc.call("thread/goal/set", {**goal, "status": "paused"})
                if timed_out.is_set():
                    break
                resumed = rpc.call("turn/start", {
                    "threadId": thread,
                    "input": [{"type": "text", "text": CONTINUE_PROMPT}],
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

        stop_timer.set()
        if timed_out.is_set() or status in {
            "turnFailed", "usageLimited", "budgetLimited"
        }:
            reason = (
                f"The {thinking_hours:g}-hour author limit was reached."
                if timed_out.is_set()
                else f"The author stopped because its state became {status}."
            )
            try:
                rpc.call("thread/goal/set", {**goal, "status": "paused"})
            except (Error, OSError):
                pass
            previous = answers[-1] if answers else last_author_output
            stage, answers[:] = "failure", []
            summary_prompt = f"{FAILURE_SUMMARY_PROMPT}\n\nSTOP REASON:\n{reason}"
            emit(
                "request", "failure", label="Failure summary request",
                text=summary_prompt, model=author_model,
                reasoningEffort=author_effort,
                reasoningSummary="detailed",
            )
            summary_stop, summary_expired = threading.Event(), threading.Event()
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
                "failure_result", "failure", label="Author failure summary",
                text=summary, output=summary,
            )
            return summary

        emit(
            "status", "solve", label="Goal complete",
            text=f"Thread {thread}", threadId=thread,
        )

        solution = answers[-1]
        emit(
            "status", "critic", label="Critic loop started",
            text=f"Maximum rounds: {critic_rounds}.",
        )
        approved = False
        for round_number in range(1, critic_rounds + 1):
            critic_options = {
                "model": critic_model, "effort": critic_effort,
            }
            if critic_prompt != CRITIC_PROMPT:
                critic_options["instructions"] = critic_prompt
            report = criticize(
                statement, solution, round_number, **critic_options
            )
            solution = report["solution"].strip()

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
                instruction = repair_prompt(
                    statement, solution, report["bugs"], round_number
                )
                stage, answers[:] = "repair", []
                emit(
                    "request", "repair",
                    label=f"Proof author revision {round_number}",
                    text=instruction, model=author_model,
                    reasoningEffort=author_effort,
                    reasoningSummary="detailed",
                )
                rpc.call("turn/start", {
                    "threadId": thread,
                    "input": [{"type": "text", "text": instruction}],
                    "summary": "detailed",
                })
                while True:
                    message = rpc.read()
                    params = message.get("params", {})
                    if params.get("threadId") not in {None, thread}:
                        continue
                    if message.get("method") == "turn/completed":
                        turn_status = (params.get("turn") or {}).get("status")
                        if turn_status != "completed":
                            raise Error(
                                f"Proof author revision {turn_status}; thread {thread}."
                            )
                        break
                if not answers:
                    raise Error("The proof author returned no replacement solution.")
                solution = answers[-1]
                emit(
                    "author_result", "repair",
                    label=f"Revised solution {round_number}", text=solution,
                )

            # Record that the next call is a new independent critic.
            if round_number < critic_rounds:
                emit(
                    "status", "critic", label="Fresh critic requested",
                    text=f"Starting independent critic round {round_number + 1}.",
                )

        if not approved:
            # Preserve the latest candidate, but never present it as final.
            emit(
                "partial_result", "critic", label="Unverified candidate",
                text=solution, output=solution,
            )
            raise Error(
                f"Reached {critic_rounds} critic rounds without a clean pass."
            )

        final_options = {"model": writer_model, "effort": writer_effort}
        if final_prompt != FINAL_PROMPT:
            final_options["instructions"] = final_prompt
        return finalize(statement, solution, **final_options)
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

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["review", "solve"])
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
    parser.add_argument("--review-prompt-file")
    parser.add_argument("--author-prompt-file")
    parser.add_argument("--critic-prompt-file")
    parser.add_argument("--final-prompt-file")
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
                )
            else:
                review(
                    statement, feedback, args.review_model, args.review_effort
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
                )
            else:
                run_goal(*common)
        return 0
    except KeyboardInterrupt:
        message = (
            "Stopped review." if action == "review"
            else "Stopped. A goal pause was requested."
        )
        print(f"\n{message}", file=sys.stderr)
        return 130
    except (Error, OSError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
