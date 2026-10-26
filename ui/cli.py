"""Serve the TCS Prover UI or prove one or more Markdown statements."""

import argparse
import errno
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import workflow_runner as runtime
from .current_run import CurrentRunError, select_current_dashboard
from .review import review_worker_main
from .server import (
    App, Server, DEFAULT_AUTHOR_MODEL, DEFAULT_CRITIC_MODEL,
    DEFAULT_CRITIC_ROUNDS, DEFAULT_REASONING_EFFORT, DEFAULT_REASONING_SUMMARY,
    DEFAULT_SPEED, DEFAULT_THINKING_HOURS, DEFAULT_WRITER_MODEL, EFFORTS, HOST,
    OFFERED_MODELS, PORT, REASONING_SUMMARIES, RUNS, SPEEDS, read_utf8,
    saved_critic_source, saved_research_source,
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
    IMPORTANT_STATUSES = {"Author paused", "Author stopped for safety", runtime.RETRY_LABEL}

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
        if kind == "status" and isinstance(label, str) and (
                label in self.IMPORTANT_STATUSES
                # Parallel requests prefix their own name ("Independent audit 2 · ...").
                or label.endswith(runtime.RETRY_LABEL)):
            detail = str(record.get("text", "")).strip()
            return f"{label}: {detail}" if detail else label

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

        # Every request to the critic is a new independent round. A replacement
        # author proof resets that per-candidate count.
        if node == "critic":
            self.critic_round = self.critic_round + 1 if self.node == node else 1
            self.node = node
            return f"Current step: {title} (round {self.critic_round})"
        if node == self.node:
            return None
        self.node = node
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
            app.stop()
        except ValueError:
            pass

    threads = [threading.Thread(target=stop_one, args=(app,)) for _, app in apps]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _job_active(app):
    """Read one job's phase without a snapshot, which re-reads workflow YAML.

    A reader that is still saving pause.json or partial output keeps the job
    active, so the interpreter cannot exit before those artifacts exist. A
    job paused by an error with an automatic retry scheduled is still active:
    the terminal resumes it like the web server does.
    """

    with app.lock:
        return (app.state["phase"] in ACTIVE_PHASES or app.has_active_worker()
                or (app.state["phase"] == "paused" and bool(app.state.get("autoResumeAt"))))


def _retry_paused_job(app):
    """Resume a job whose error pause has reached its automatic retry time (as the web server does)."""

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
    """Run one Markdown proof or all top-level Markdown proofs in a folder."""

    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    files = markdown_inputs(path)
    statements = [(source, read_utf8(source, "statement")) for source in files]
    options = direct_cli_options(**settings)
    batch = Path(path).expanduser().resolve().is_dir()
    output_lock = threading.Lock()
    apps = []
    try:
        for source, statement in statements:
            if verbose_events:
                job_output = (
                    TaggedJsonlOutput(output_stream, output_lock, source.name)
                    if batch else output_stream
                )
            else:
                job_output = ConciseHeadlessOutput(
                    output_stream, output_lock, source.name
                )
            # A new job creates its own run; never parse another job's transcript.
            app = App(runs=runs, output_stream=job_output, load_latest=False)
            apps.append((source, app))
            app.start_direct_statement(statement=statement, **options)
            print(
                f"[{source.name}] Proof started in {app.run_dir}",
                file=error_stream, flush=True,
            )
    except KeyboardInterrupt:
        _stop_headless_apps(apps)
        print("All active proofs stopped.", file=error_stream, flush=True)
        return 130
    except Exception:
        _stop_headless_apps(apps)
        raise

    try:
        while any(_job_active(app) for _, app in apps):
            for _, app in apps:
                _retry_paused_job(app)
            time.sleep(0.25)
    except KeyboardInterrupt:
        _stop_headless_apps(apps)
        print("All active proofs stopped.", file=error_stream, flush=True)
        return 130

    failed = False
    for source, app in apps:
        with app.lock:
            error = app.state.get("error", "")
        if error:
            failed = True
            print(
                f"[{source.name}] Proof failed: {error}",
                file=error_stream, flush=True,
            )
        else:
            print(
                f"[{source.name}] Proof finished in {app.run_dir}",
                file=error_stream, flush=True,
            )
    return 1 if failed else 0


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
            "open the web UI at a fresh critic using RUN/SOLUTION.md or "
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
        "-thinkingHours", "--thinkingHours", "--thinking-hours",
        dest="thinking_hours", type=float, default=DEFAULT_THINKING_HOURS,
        metavar="HOURS",
        help=(f"total elapsed-workflow limit in hours: 0 for unlimited, or at most "
              f"{runtime.MAX_AUTHOR_HOURS} (default: {DEFAULT_THINKING_HOURS})"),
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
                "model from Advanced settings → API models "
                f"(default: {model_defaults[role]}); the older GPT-5.6 ids "
                "are still accepted"
            ),
        )
    parser.add_argument(
        "-reasoningEffort", "--reasoningEffort", "--reasoning-effort",
        dest="reasoning_effort", choices=EFFORTS,
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
            dest=f"{role}_effort", choices=EFFORTS, default=None,
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
        try:
            return run_headless_markdown(
                args.input_path,
                critic_rounds=args.critic_rounds,
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
            print(f"Cannot start headless proof: {exc}", file=sys.stderr)
            return 1
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
    try:
        server = (Server((HOST, PORT), runs=selected_runs) if selected_runs is not None
                  else Server((HOST, PORT)))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(
            f"Cannot start: local port {PORT} is already in use. "
            "Close the existing process and try again.",
            file=sys.stderr,
        )
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping TCS Prover…")
        server.stop_all()
    finally:
        server.server_close()
    return 0
