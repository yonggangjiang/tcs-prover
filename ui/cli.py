"""Serve the TCS Prover UI or prove one or more Markdown statements."""

import argparse
import errno
import json
import math
import shlex
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import workflow_runner as runtime
from . import ownership
from .current_run import CurrentRunError, select_current_dashboard
from .review import review_worker_main
from .server import (
    App, Server, AUTO_LAUNCH_SPACING_SECONDS, AUTO_RESUME_SECONDS, DEFAULT_AUTHOR_MODEL, DEFAULT_AUTHOR_SUBAGENTS,
    DEFAULT_CRITIC_COUNT, MAX_AUTHOR_SUBAGENTS,
    DEFAULT_CRITIC_MODEL, DEFAULT_CRITIC_ROUNDS, DEFAULT_REASONING_EFFORT, DEFAULT_REASONING_SUMMARY,
    DEFAULT_SPEED, DEFAULT_THINKING_HOURS, DEFAULT_WRITER_MODEL, HOST,
    MAX_CRITIC_COUNT, MAX_STATEMENT_CHARS, OFFERED_MODELS, PORT, REASONING_SUMMARIES, RUNS, SPEEDS, SleepWatch,
    load_installation_settings, raise_open_file_limit, read_utf8, retry_interval_text, saved_critic_source,
    saved_research_source, stop_process_tree,
)

ACTIVE_PHASES = {"reviewing", "running", "stopping", "pausing"}
_UNSET = object()

class ConciseHeadlessOutput:
    """Print only headless workflow transitions and diagnostics."""

    STAGE_NODES = {
        "review": ("review", "Statement reviewer"),
        "solve": ("author", "Proof author"),
        "repair": ("author", "Proof author"),
        "critic": ("critic", "Independent critic"),
        "final": ("final", "LaTeX editor"),
        "failure": ("failure", "Failure summary"),
    }
    IMPORTANT_STATUSES = {"Author paused", "Author stopped for safety", runtime.RETRY_LABEL,
                          runtime.COMMAND_RETRY_LABEL, runtime.STORAGE_RETRY_LABEL, "Conversation replaced",
                          "Author needs attention"}

    def __init__(self, stream, lock, input_file):
        self.stream, self.lock = stream, lock
        self.input_file = input_file
        self.node = None
        self.critic_round = 0

    @staticmethod
    def _record(line):
        """Parse one child line without exposing malformed payloads verbatim."""

        try:
            record = json.loads(line)
            if isinstance(record, dict):
                return record
        except json.JSONDecodeError:
            pass
        detail = line.rstrip("\r\n")
        if detail.lstrip().startswith(("{", "[")):
            detail = "Malformed solver event."
        return {"kind": "diagnostic", "text": detail}

    def _message(self, record):
        """Return one short terminal message, or None for verbose events."""

        kind = record.get("kind")
        if kind == "diagnostic":
            detail = str(record.get("text", "")).strip()
            if not detail:
                return None
            if detail.lower().startswith("error:"):
                detail = detail[6:].strip()
                return f"Error: {detail}"
            return f"Diagnostic: {detail}"

        label = record.get("label")
        # The paused author's progress summary (after the time limit) is printed whole.
        if kind == "conversation_reply" and record.get("conversationKind") == "summary":
            return f"Progress summary:\n{str(record.get('text') or '').strip()}"
        if kind == "status" and label == "Author reply failed":
            return f"Progress summary failed: {str(record.get('text') or '').strip()}"
        if kind == "status" and isinstance(label, str) and (
                label in self.IMPORTANT_STATUSES
                # A labelled call prefixes its own name ("Critic coordinator · ...").
                or label.endswith(runtime.RETRY_LABEL)):
            detail = str(record.get("text", "")).strip()
            return f"{label}: {detail}" if detail else label
        if (kind == "status" and record.get("stage") == "critic" and isinstance(label, str)
                and label.startswith("Workflow step:")):
            # A critic round is one visit of the critic step, however many critics,
            # coordinator calls and retries it makes. The runner numbers the visits
            # of one candidate, so a replacement author proof starts at round 1 again.
            visit = record.get("visit")
            self.critic_round = (visit if isinstance(visit, int) and not isinstance(visit, bool) and visit > 0
                                 else self.critic_round + 1)
            self.node = "critic"
            return f"Current step: {self.STAGE_NODES['critic'][1]} (round {self.critic_round})"

        if kind != "request":
            return None
        # A retry of the same model call is not a new step or critic round.
        if isinstance(record.get("attempt"), int) and record["attempt"] > 1:
            return None
        stage = record.get("stage")
        step = self.STAGE_NODES.get(stage)
        if step is None:
            return None
        node, title = step
        if node == self.node:
            return None
        self.node = node
        if node == "critic":
            # Its step was not announced (a runner without step records): the round starts here.
            self.critic_round = 1
            return f"Current step: {title} (round {self.critic_round})"
        if node == "author":
            self.critic_round = 0
            # Every author request is labelled "Exact solve input", so the
            # stage, not the label, identifies a repair turn.
            if stage == "repair":
                title = "Proof author (repairing critic objections)"
        return f"Current step: {title}"

    def write(self, line):
        if not line:
            return 0
        message = self._message(self._record(line))
        if message:
            with self.lock:
                self.stream.write(f"[{self.input_file}] {message}\n")
                self.stream.flush()
        return len(line)

    def flush(self):
        with self.lock:
            self.stream.flush()


class TaggedJsonlOutput:
    """Serialize one batch job's terminal events with its input filename."""

    def __init__(self, stream, lock, input_file):
        self.stream, self.lock = stream, lock
        self.input_file = input_file

    def write(self, line):
        if not line:
            return 0
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            record = {"kind": "diagnostic", "text": line.rstrip("\r\n")}
        record["inputFile"] = self.input_file
        rendered = json.dumps(record, ensure_ascii=False) + "\n"
        with self.lock:
            self.stream.write(rendered)
            self.stream.flush()
        return len(line)

    def flush(self):
        with self.lock:
            self.stream.flush()


def markdown_inputs(path):
    """Resolve one Markdown file or the top-level Markdown files in a folder."""

    source = Path(path).expanduser().resolve()
    if source.is_file():
        if source.suffix.lower() != ".md":
            raise ValueError(f"The input file must end in .md: {source}")
        return [source]
    if source.is_dir():
        files = sorted(
            (
                item for item in source.iterdir()
                if item.is_file() and item.suffix.lower() == ".md"
            ),
            key=lambda item: (item.name.casefold(), item.name),
        )
        if not files:
            raise ValueError(f"The input folder contains no .md files: {source}")
        return files
    raise ValueError(f"The input path does not exist: {source}")


def direct_cli_options(
    critic_rounds=DEFAULT_CRITIC_ROUNDS,
    critic_count=DEFAULT_CRITIC_COUNT,
    author_subagents=DEFAULT_AUTHOR_SUBAGENTS,
    thinking_hours=DEFAULT_THINKING_HOURS,
    author_model=DEFAULT_AUTHOR_MODEL,
    critic_model=DEFAULT_CRITIC_MODEL,
    writer_model=DEFAULT_WRITER_MODEL,
    reasoning_effort=DEFAULT_REASONING_EFFORT,
    author_effort=None, critic_effort=None, writer_effort=None,
    speed_mode=DEFAULT_SPEED,
    reasoning_summary=DEFAULT_REASONING_SUMMARY,
    author_prompt_file=None, critic_prompt_file=None, final_prompt_file=None,
):
    """Load optional prompt files and validate direct-workflow CLI settings."""

    prompt_files = {
        "author": author_prompt_file,
        "critic": critic_prompt_file,
        "final": final_prompt_file,
    }
    prompts = {
        name: read_utf8(path, f"{name} prompt") if path else None
        for name, path in prompt_files.items()
    }
    options = App._workflow_options(
        critic_rounds=critic_rounds,
        critic_count=critic_count,
        author_subagents=author_subagents,
        thinking_hours=thinking_hours,
        author_model=author_model,
        critic_model=critic_model,
        writer_model=writer_model,
        reasoning_effort=reasoning_effort,
        author_effort=author_effort,
        critic_effort=critic_effort,
        writer_effort=writer_effort,
        author_prompt=prompts["author"],
        critic_prompt=prompts["critic"],
        final_prompt=prompts["final"],
        speed_mode=speed_mode,
        reasoning_summary=reasoning_summary,
        include_review=False,
    )
    return {
        "critic_rounds": options["criticRounds"],
        "critic_count": options["criticCount"],
        "author_subagents": options["authorSubagents"],
        "thinking_hours": options["thinkingHours"],
        "author_model": options["authorModel"],
        "critic_model": options["criticModel"],
        "writer_model": options["writerModel"],
        "reasoning_effort": options["reasoningEffort"],
        "author_effort": options["authorEffort"],
        "critic_effort": options["criticEffort"],
        "writer_effort": options["writerEffort"],
        "author_prompt": options["authorPrompt"],
        "critic_prompt": options["criticPrompt"],
        "final_prompt": options["finalPrompt"],
        "speed_mode": options["speedMode"],
        "reasoning_summary": options["reasoningSummary"],
    }


def research_resume_cli_settings(args, parser, argv):
    """Override saved author settings only for options explicitly supplied."""

    fields = {
        "criticRounds": "critic_rounds",
        "criticCount": "critic_count",
        "authorSubagents": "author_subagents",
        "thinkingHours": "thinking_hours",
        "reasoningEffort": "reasoning_effort",
        "speedMode": "speed_mode",
        "reasoningSummary": "reasoning_summary",
    }
    for role in ("author", "critic", "writer"):
        fields[f"{role}Model"] = f"{role}_model"
        fields[f"{role}Effort"] = f"{role}_effort"
    # Let argparse report what was supplied: it also accepts unambiguous
    # abbreviations such as -thinking 24, which no spelling list can cover.
    probe = parser.parse_args(
        argv, namespace=argparse.Namespace(**dict.fromkeys(fields.values(), _UNSET)),
    )
    options = {
        key: getattr(args, attribute)
        for key, attribute in fields.items()
        if getattr(probe, attribute) is not _UNSET
    }
    if "reasoningEffort" in options:
        for role in ("author", "critic", "writer"):
            options.setdefault(f"{role}Effort", options["reasoningEffort"])
    for role in ("author", "critic", "final"):
        path = getattr(args, f"{role}_prompt_file")
        if path:
            options[f"{role}Prompt"] = read_utf8(path, f"{role} prompt")
    return options


def _stop_headless_apps(apps):
    """Stop several independent jobs concurrently after Ctrl-C or launch failure."""

    def stop_one(app):
        try:
            # A paused author's reply runs in its own process group: end it too.
            if _conversation_busy(app):
                app.stop_author_reply()
        except Exception:
            pass
        try:
            app.stop()
        except ValueError:
            pass
        except Exception:
            # The stop could not be recorded (for example on a full disk); the runner still ends.
            with app.lock:
                process = app.process
            stop_process_tree(process)

    _each_concurrently(stop_one, apps)


def _each_concurrently(action, apps):
    threads = [threading.Thread(target=action, args=(app,)) for _, app in apps]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _pause_headless_apps(apps):
    """First Ctrl-C: pause working authors in their saved conversations and stop every other stage.

    A paused author can be resumed in place; an error pause waiting for its
    automatic retry stays paused instead (as Keep paused does).
    """

    def settle(app):
        try:
            # A summary or reply the paused author is writing ends; the job stays paused.
            if _conversation_busy(app):
                app.stop_author_reply()
        except Exception:
            pass
        with app.lock:
            phase, stage = app.state["phase"], app.state["stage"]
            author = app.state["activeNode"] == "author" and stage in {"solve", "repair"}
            retrying = phase == "paused" and bool(app.state.get("autoResumeAt"))
            active = phase in ACTIVE_PHASES or app.has_active_worker()
        if phase == "pausing":
            return  # Its pause is already on the way.
        try:
            if (phase == "running" and author) or retrying:
                app.pause()
                return
        except Exception:
            pass  # A pause that cannot be recorded becomes a stop: the runner must not keep working.
        if active:
            _stop_headless_apps([(None, app)])

    _each_concurrently(settle, apps)


def _say(stream, text):
    """Print one line for the user; a closed terminal must not end the remaining jobs."""

    try:
        print(text, file=stream, flush=True)
    except (OSError, ValueError):
        pass


def _continue_command(option, run_dir):
    return f"python3 web_ui.py {option} {shlex.quote(str(run_dir))}"


def _report_headless(apps, error_stream):
    """Print each file's outcome and how to continue an unfinished one; return whether any did not finish."""

    unfinished = False
    for source, app in apps:
        with app.lock:
            phase, error, run_dir = app.state["phase"], app.state.get("error", ""), app.run_dir
            stopped = app.state.get("manuallyStopped")
            stage = app.state.get("stoppedStage") or app.state.get("stage")
        if run_dir is None or phase in {"input", "prepared"}:
            unfinished = True
            # A queued job that Ctrl-C took out of the queue stays prepared.
            _say(error_stream, f"[{source.name}] Not started." + (
                " Start it from its job in the web UI: python3 web_ui.py" if phase == "prepared" else ""))
        elif phase == "paused":
            unfinished = True
            _say(error_stream, f"[{source.name}] Paused{': ' + error if error else '.'}")
            # Resume is a button of the web UI; from a terminal the same conversation continues with this.
            _say(error_stream, f"[{source.name}] Continue the same conversation with: "
                               + _continue_command("--resume-research", run_dir))
        elif error:
            unfinished = True
            _say(error_stream, f"[{source.name}] " + ("Stopped." if stopped else f"Proof failed: {error}"))
            if stopped and stage in {"solve", "repair", "failure"}:
                _say(error_stream, f"[{source.name}] Continue the same conversation with: "
                                   + _continue_command("--resume-research", run_dir))
            elif stopped and stage == "critic":
                _say(error_stream, f"[{source.name}] Start fresh critics on the saved proof with: "
                                   + _continue_command("--resume-critic", run_dir))
            elif stopped:
                _say(error_stream, f"[{source.name}] Continue it from its job in the web UI: python3 web_ui.py")
        else:
            _say(error_stream, f"[{source.name}] Proof finished in {run_dir}")
    return unfinished


def _job_active(app):
    """Read one job's phase without a snapshot, which re-reads workflow YAML.

    A reader that is still saving pause.json or partial output keeps the job
    active, so the interpreter cannot exit before those artifacts exist. A
    job paused by an error with an automatic retry scheduled is still active:
    the terminal resumes it like the web server does. So is a job waiting in
    the queue for a running slot (or for its next try after a failed start),
    and a paused one whose author is asked for its progress summary.
    """

    with app.lock:
        active = (app.state["phase"] in ACTIVE_PHASES or app.has_active_worker()
                  or (app.state["phase"] == "paused" and bool(app.state.get("autoResumeAt")))
                  or (app.state["phase"] == "prepared" and bool(app.state.get("launchQueued"))))
    return active or _conversation_busy(app)


def _conversation_busy(app):
    """Whether a paused author's summary or reply is queued or being written."""

    busy = getattr(app, "conversation_busy", None)
    return bool(busy()) if callable(busy) else False


def _launch_due(apps, limit, error_stream, last_launch=-math.inf, now=None):
    """Start what is due, as the web server does; return when the last start was.

    Error pauses whose automatic retry has come and jobs waiting in the queue
    start in file order, within the running-jobs limit (0: no limit) and at
    most one every AUTO_LAUNCH_SPACING_SECONDS, so jobs that came due together
    reach the provider spread out.
    """

    now = time.time() if now is None else now
    for _, app in apps:
        # A paused author's summary or reply is exempt from the limit and the spacing, as on the server.
        _start_conversation(app, now)
    free = max(0, limit - sum(app.occupies_slot() for _, app in apps)) if limit else math.inf
    for source, app in apps:
        if free <= 0 or now - last_launch < AUTO_LAUNCH_SPACING_SECONDS:
            break
        with app.lock:
            phase = app.state["phase"]
        if phase == "paused":
            _retry_paused_job(app)
        elif not (phase == "prepared" and app.start_queued_if_due(now)):
            continue
        with app.lock:
            started, error = app.state["phase"] != phase, app.state.get("launchError") or ""
        if started:
            free, last_launch = free - 1, now
            if phase == "prepared":
                _say(error_stream, f"[{source.name}] Proof started in {app.run_dir}")
        elif phase == "prepared" and error:
            _say(error_stream, f"[{source.name}] {error} Trying again in {retry_interval_text()}.")
    return last_launch


def _leave_queue(apps):
    """After Ctrl-C, no queued job may start later (a web server would start queued jobs by itself)."""

    for _, app in apps:
        try:
            app.leave_queue()
        except (AttributeError, OSError, ValueError):
            pass


def _start_conversation(app, now=None):
    """Start a paused author's due summary or reply (a failure is reported on its job and tried again)."""

    start = getattr(app, "start_conversation_if_due", None)
    if not callable(start):
        return
    try:
        start(now)
    except Exception as exc:
        try:
            app.defer_conversation(exc, now)
        except Exception:
            pass


def _retry_paused_job(app):
    """Resume a job whose error pause has reached its automatic retry time (as the web server does).

    A summary or reply the paused author owes comes first; the resume waits for it.
    """

    _start_conversation(app)
    try:
        app.auto_resume_if_due(app.resume)
    except Exception:
        # Nothing one job raises may end the other jobs' retries. A damaged
        # checkpoint is reported on its job, which tries again after the usual wait.
        pass


def run_headless_markdown(
    path, runs=RUNS, output_stream=None, error_stream=None,
    verbose_events=False, **settings
):
    """Run one Markdown proof or all top-level Markdown proofs in a folder.

    The jobs start at once up to the running-jobs limit and wait in a queue
    after that; a file whose job cannot be created or started stops no other
    and is tried again every minute. The first Ctrl-C (or SIGTERM) takes the
    queued jobs out of the queue, pauses each working author in its saved
    conversation, stops the other stages and prints how to continue each
    file; a second Ctrl-C stops everything at once.
    """

    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    files = markdown_inputs(path)
    statements = [(source, read_utf8(source, "statement")) for source in files]
    # Check every file before any job starts: a statement that can never
    # start is the user's to fix, not one to try again every minute.
    for source, statement in statements:
        if not statement.strip():
            raise ValueError(f"{source.name} has no statement.")
        if "\0" in statement:
            raise ValueError(f"{source.name} contains a NUL character.")
        if len(statement.strip()) > MAX_STATEMENT_CHARS:
            raise ValueError(f"{source.name} is longer than {MAX_STATEMENT_CHARS:,} characters.")
    options = direct_cli_options(**settings)
    batch = Path(path).expanduser().resolve().is_dir()
    output_lock = threading.Lock()
    limit = load_installation_settings()["maxRunningJobs"]
    apps, pending = [], []

    def output(source):
        if verbose_events:
            return TaggedJsonlOutput(output_stream, output_lock, source.name) if batch else output_stream
        return ConciseHeadlessOutput(output_stream, output_lock, source.name)

    def create(source, statement):
        # A new job creates its own run; never parse another job's transcript.
        app = App(runs=runs, output_stream=output(source), load_latest=False)
        try:
            # Beyond the running-jobs limit a job waits in the queue.
            app.start_direct_statement(statement=statement, _prepare_only=True, **options)
        except Exception as exc:
            # Nothing started: drop the half-made folder and try again every minute;
            # one file that cannot start never stops the others.
            app.discard_unstarted_run()
            pending.append([source, statement, time.time() + AUTO_RESUME_SECONDS])
            _say(error_stream, f"[{source.name}] Cannot start yet: {str(exc).rstrip('.') or type(exc).__name__}. "
                               f"Trying again in {retry_interval_text(AUTO_RESUME_SECONDS)}.")
            return
        apps.append((source, app))
        if limit and sum(other.occupies_slot() for _, other in apps) >= limit:
            _say(error_stream, f"[{source.name}] Queued: at most {limit} "
                               f"job{'s run' if limit != 1 else ' runs'} at once.")

    last_launch = -math.inf
    try:
        for source, statement in statements:
            create(source, statement)
            # The files start at once, up to the running-jobs limit.
            last_launch = max(last_launch, _launch_due(apps, limit, error_stream))
        sleep = SleepWatch()
        while pending or any(_job_active(app) for _, app in apps):
            slept = sleep.slept()
            if slept:
                for _, app in apps:
                    app.exclude_sleep(slept)
            for item in [item for item in pending if time.time() >= item[2]]:
                pending.remove(item)
                create(item[0], item[1])
            last_launch = _launch_due(apps, limit, error_stream, last_launch)
            time.sleep(0.25)
    except KeyboardInterrupt:
        return _interrupted(apps, error_stream, pending)

    return 1 if _report_headless(apps, error_stream) else 0


def _interrupted(apps, error_stream, pending=()):
    """First Ctrl-C: take queued jobs out of the queue, pause the authors and report how to continue;
    a second one stops everything."""

    try:
        _say(error_stream, "Pausing the proof authors in their saved conversations and stopping the other "
                           "stages. Press Ctrl-C again to stop everything at once.")
        _leave_queue(apps)
        _pause_headless_apps(apps)
        while any(_job_active(app) for _, app in apps):
            time.sleep(0.25)
    except KeyboardInterrupt:
        _stop_headless_apps(apps)
        _say(error_stream, "All active proofs stopped.")
        return 130
    for source, _, _ in pending:
        _say(error_stream, f"[{source.name}] Not started.")
    _report_headless(apps, error_stream)
    return 130


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _handle_signals(hangup):
    """Make SIGTERM stop like Ctrl-C, and SIGHUP do `hangup`; return the previous handlers."""

    previous = {}
    for name, handler in (("SIGTERM", _interrupt), ("SIGHUP", hangup)):
        number = getattr(signal, name, None)
        try:
            # A signal the launching shell ignores (nohup) stays ignored.
            if number is not None and signal.getsignal(number) != signal.SIG_IGN:
                previous[number] = signal.signal(number, handler)
        except (OSError, ValueError):
            pass  # Not the main thread, or not supported here.
    return previous


def _restore_signals(previous):
    for number, handler in previous.items():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError, TypeError):
            pass


def main():
    """Run Markdown proofs from the terminal or start the browser interface."""

    if sys.argv[1:2] == ["--review-worker"]:
        return review_worker_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=runtime.HelpFormatter)
    parser.add_argument(
        "input_path", nargs="?",
        help="UTF-8 .md statement file or folder of top-level .md files",
    )
    parser.add_argument(
        "--resume-critic", metavar="RUN",
        help=(
            "open the web UI with fresh critics on RUN/SOLUTION.md or "
            f"RUN/{runtime.SAVED_CANDIDATE_FILENAME}"
        ),
    )
    parser.add_argument(
        "--resume-author", "--resume-research", dest="resume_research", metavar="RUN",
        help=(
            "continue RUN's saved author session in its original workspace with a new time "
            "budget; saved settings are kept unless explicitly overridden"
        ),
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--verbose-events", action="store_true",
        help="print every public JSONL event during a Markdown terminal run",
    )
    parser.add_argument(
        "-criticRounds", "--criticRounds", "--critic-rounds",
        dest="critic_rounds", type=int, default=DEFAULT_CRITIC_ROUNDS,
        metavar="N",
        help=(
            "allow up to N consecutive critic repair rounds "
            f"(default: {DEFAULT_CRITIC_ROUNDS}); an unchanged pass finishes "
            "early, an edited pass finishes at N, and rejection returns to the author"
        ),
    )
    parser.add_argument(
        "-criticCount", "--criticCount", "--critic-count",
        dest="critic_count", type=int, default=DEFAULT_CRITIC_COUNT,
        metavar="N",
        help=(
            f"the critic starts N fresh independent critics (its subagents) on each proof version, 1 to "
            f"{MAX_CRITIC_COUNT} (default: {DEFAULT_CRITIC_COUNT}), and decides on their reports"
        ),
    )
    parser.add_argument(
        "-authorSubagents", "--authorSubagents", "--author-subagents",
        dest="author_subagents", type=int, default=DEFAULT_AUTHOR_SUBAGENTS,
        metavar="N",
        help=(
            f"let the proof author start up to N subagents at a time, 0 to {MAX_AUTHOR_SUBAGENTS} "
            f"(default: {DEFAULT_AUTHOR_SUBAGENTS}, no subagent tool)"
        ),
    )
    parser.add_argument(
        "-thinkingHours", "--thinkingHours", "--thinking-hours",
        dest="thinking_hours", type=float, default=DEFAULT_THINKING_HOURS,
        metavar="HOURS",
        help=(f"total elapsed-workflow limit in hours: 0 for unlimited, or any positive number (default: "
              f"{DEFAULT_THINKING_HOURS:g}); it pauses the proof author, while critic and LaTeX steps always finish"),
    )
    model_defaults = {
        "author": DEFAULT_AUTHOR_MODEL,
        "critic": DEFAULT_CRITIC_MODEL,
        "writer": DEFAULT_WRITER_MODEL,
    }
    for role in ("author", "critic", "writer"):
        camel = f"{role}Model"
        # Configured API models are user data, so the registry validates ids
        # instead of a fixed choices list.
        parser.add_argument(
            f"-{camel}", f"--{camel}", f"--{role}-model",
            dest=f"{role}_model", type=runtime.model_argument, metavar="MODEL",
            default=model_defaults[role],
            help=(
                f"{role} model: {', '.join(OFFERED_MODELS)}, or an api:<id> "
                "model from Advanced settings → API connections "
                f"(default: {model_defaults[role]}); the older GPT-5.6 ids "
                "are still accepted"
            ),
        )
    parser.add_argument(
        "-reasoningEffort", "--reasoningEffort", "--reasoning-effort",
        dest="reasoning_effort", type=runtime.effort_argument, metavar=runtime.EFFORT_METAVAR,
        default=DEFAULT_REASONING_EFFORT,
        help=(
            "fallback reasoning effort for every proof role "
            f"(default: {DEFAULT_REASONING_EFFORT})"
        ),
    )
    for role in ("author", "critic", "writer"):
        camel = f"{role}Effort"
        parser.add_argument(
            f"-{camel}", f"--{camel}", f"--{role}-effort",
            dest=f"{role}_effort", type=runtime.effort_argument, metavar=runtime.EFFORT_METAVAR, default=None,
            help=f"override the {role} reasoning effort",
        )
    parser.add_argument(
        "-speedMode", "--speedMode", "--speed-mode",
        dest="speed_mode", choices=SPEEDS, default=DEFAULT_SPEED,
        help=(
            f"generation speed (default: {DEFAULT_SPEED}); fast applies to Codex "
            "CLI models only, other models run standard"
        ),
    )
    parser.add_argument(
        "-reasoningSummary", "--reasoningSummary", "--reasoning-summary",
        dest="reasoning_summary", choices=REASONING_SUMMARIES,
        default=DEFAULT_REASONING_SUMMARY,
        help="public reasoning-summary detail shown in the activity log",
    )
    for role in ("author", "critic", "final"):
        camel = f"{role}PromptFile"
        parser.add_argument(
            f"-{camel}", f"--{camel}", f"--{role}-prompt-file",
            dest=f"{role}_prompt_file", metavar="PATH",
            help=f"UTF-8 file containing the custom {role} prompt",
        )
    args = parser.parse_args()
    raise_open_file_limit()
    resume_source = None
    if sum(bool(value) for value in (args.input_path, args.resume_critic, args.resume_research)) > 1:
        parser.error("Choose only one of input_path, --resume-critic, or --resume-author.")
    if args.resume_critic or args.resume_research:
        try:
            resume_source = (
                saved_research_source(args.resume_research) if args.resume_research
                else saved_critic_source(args.resume_critic)
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"Cannot resume saved job: {exc}", file=sys.stderr)
            return 1
    if args.input_path:
        runtime.configure_standard_streams()
        ownership.ROLE = "terminal run"
        # SIGTERM pauses like Ctrl-C; closing the terminal leaves the jobs running.
        previous = _handle_signals(signal.SIG_IGN)
        try:
            return run_headless_markdown(
                args.input_path,
                critic_rounds=args.critic_rounds,
                critic_count=args.critic_count,
                author_subagents=args.author_subagents,
                thinking_hours=args.thinking_hours,
                author_model=args.author_model,
                critic_model=args.critic_model,
                writer_model=args.writer_model,
                reasoning_effort=args.reasoning_effort,
                author_effort=args.author_effort,
                critic_effort=args.critic_effort,
                writer_effort=args.writer_effort,
                speed_mode=args.speed_mode,
                reasoning_summary=args.reasoning_summary,
                author_prompt_file=args.author_prompt_file,
                critic_prompt_file=args.critic_prompt_file,
                final_prompt_file=args.final_prompt_file,
                verbose_events=args.verbose_events,
            )
        except (OSError, TypeError, ValueError) as exc:
            _say(sys.stderr, f"Cannot start headless proof: {exc}")
            return 1
        finally:
            _restore_signals(previous)
    selected_runs = None
    if not resume_source:
        try:
            selection = select_current_dashboard()
        except CurrentRunError as exc:
            print(f"Cannot open the current run: {exc}", file=sys.stderr)
            return 1
        if selection.attached:
            print(selection.url if args.no_browser else f"TCS Prover is already running at {selection.url}")
            if not args.no_browser:
                webbrowser.open(selection.url)
            return 0
        selected_runs = selection.runs_root
    ownership.ROLE = "web server"
    try:
        server = (Server((HOST, PORT), runs=selected_runs, single_instance=True) if selected_runs is not None
                  else Server((HOST, PORT), single_instance=True))
    except ownership.AlreadyRunning as exc:
        # One server owns a runs folder: a second one would resume the same jobs.
        if resume_source:
            print(f"Cannot resume saved job: TCS Prover is already running at {exc.url or 'another port'}; "
                  "continue the job there.", file=sys.stderr)
            return 1
        print(exc.url if args.no_browser and exc.url else str(exc))
        if exc.url and not args.no_browser:
            webbrowser.open(exc.url)
        return 0
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(
            f"Cannot start: local port {PORT} is already in use. "
            "Close the existing process and try again.",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        # A workflow file that cannot be read, with no earlier readable version to use.
        print(f"Cannot start TCS Prover: {exc}", file=sys.stderr)
        return 1
    resume_app = None
    if resume_source:
        try:
            if args.resume_research:
                resume_app = server.start_saved_research_job(
                    resume_source["run_dir"],
                    research_resume_cli_settings(args, parser, sys.argv[1:]),
                )
            else:
                resume_settings = {
                    "criticRounds": args.critic_rounds,
                    "criticCount": args.critic_count,
                    "authorSubagents": args.author_subagents,
                    "thinkingHours": args.thinking_hours,
                    "authorModel": args.author_model,
                    "criticModel": args.critic_model,
                    "writerModel": args.writer_model,
                    "reasoningEffort": args.reasoning_effort,
                    "authorEffort": args.author_effort,
                    "criticEffort": args.critic_effort,
                    "writerEffort": args.writer_effort,
                    "speedMode": args.speed_mode,
                    "reasoningSummary": args.reasoning_summary,
                }
                if args.author_prompt_file:
                    resume_settings["authorPrompt"] = read_utf8(
                        args.author_prompt_file, "author prompt"
                    )
                if args.critic_prompt_file:
                    resume_settings["criticPrompt"] = read_utf8(
                        args.critic_prompt_file, "critic prompt"
                    )
                if args.final_prompt_file:
                    resume_settings["finalPrompt"] = read_utf8(
                        args.final_prompt_file, "final prompt"
                    )
                resume_app = server.start_saved_critic_job(
                    resume_source["run_dir"], resume_settings,
                )
        except (OSError, TypeError, ValueError) as exc:
            server.server_close()
            print(f"Cannot resume saved job: {exc}", file=sys.stderr)
            return 1
        if resume_app.state.get("settingsWarning"):
            # For example, a saved DeepSeek role now runs as GPT 6.1 Sol.
            print(f"Note: {resume_app.state['settingsWarning']}", file=sys.stderr, flush=True)
    # A URL fragment stays in the browser; JavaScript sends it as a secret header.
    job_query = (
        f"?job={quote(resume_app.state['runId'])}" if resume_app else ""
    )
    url = f"{server.origin}/{job_query}#{server.token}"
    # Flush before blocking: with --no-browser this line may be the only copy
    # of the session URL, and a pipe or log file would otherwise hold it back.
    print(f"TCS Prover is ready at {url}\nPress Ctrl-C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    # SIGTERM and SIGHUP (logout, a closed terminal) close the server like Ctrl-C.
    previous = _handle_signals(_interrupt)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _say(sys.stdout, "\nPausing the running authors and stopping TCS Prover; they resume when it "
                         "starts again. Press Ctrl-C again to stop at once.")
        try:
            server.stop_all()
        except KeyboardInterrupt:
            server.stop_all(wait=0)
    finally:
        _restore_signals(previous)
        server.server_close()
    return 0
