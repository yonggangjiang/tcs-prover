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
from .review import review_worker_main
from .server import (
    App, Server, DEFAULT_AUTHOR_MODEL, DEFAULT_CRITIC_MODEL,
    DEFAULT_CRITIC_ROUNDS, DEFAULT_REASONING_EFFORT, DEFAULT_REASONING_SUMMARY,
    DEFAULT_SPEED, DEFAULT_THINKING_HOURS, DEFAULT_WRITER_MODEL, EFFORTS, HOST,
    MODELS, PORT, REASONING_SUMMARIES, RUNS, SPEEDS, read_utf8,
    saved_critic_source,
)

ACTIVE_PHASES = {"reviewing", "running", "stopping"}

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
    IMPORTANT_STATUSES = {"Goal paused", "Pause not confirmed"}

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

        if kind == "status" and record.get("label") in self.IMPORTANT_STATUSES:
            label = record["label"]
            detail = str(record.get("text", "")).strip()
            return f"{label}: {detail}" if detail else label

        if kind != "request":
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
            if stage == "repair" and record.get("label"):
                title = str(record["label"])
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
            app = App(runs=runs, output_stream=job_output)
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
        while any(app.snapshot()["phase"] in ACTIVE_PHASES for _, app in apps):
            time.sleep(0.25)
    except KeyboardInterrupt:
        _stop_headless_apps(apps)
        print("All active proofs stopped.", file=error_stream, flush=True)
        return 130

    failed = False
    for source, app in apps:
        state = app.snapshot()
        error = state.get("error", "")
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


def run_headless_critic_resume(
    source_run, runs=RUNS, output_stream=None, error_stream=None,
    verbose_events=False, critic_rounds=DEFAULT_CRITIC_ROUNDS,
    critic_model=DEFAULT_CRITIC_MODEL, writer_model=DEFAULT_WRITER_MODEL,
    reasoning_effort=DEFAULT_REASONING_EFFORT,
    critic_effort=None, writer_effort=None,
    speed_mode=DEFAULT_SPEED,
    reasoning_summary=DEFAULT_REASONING_SUMMARY,
    critic_prompt_file=None, final_prompt_file=None,
):
    """Audit a complete proof saved by an earlier author job."""

    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    source = saved_critic_source(source_run)
    critic_prompt = (
        read_utf8(critic_prompt_file, "critic prompt")
        if critic_prompt_file else source["critic_prompt"]
    )
    final_prompt = (
        read_utf8(final_prompt_file, "final prompt")
        if final_prompt_file else source["final_prompt"]
    )
    lock = threading.Lock()
    job_output = (
        output_stream if verbose_events
        else ConciseHeadlessOutput(output_stream, lock, source["run_dir"].name)
    )
    app = App(runs=runs, output_stream=job_output)
    try:
        app.start_critic_resume(
            statement=source["statement"],
            solution=source["solution"],
            source_run=source["run_dir"],
            critic_rounds=critic_rounds,
            critic_model=critic_model,
            writer_model=writer_model,
            reasoning_effort=reasoning_effort,
            critic_effort=critic_effort,
            writer_effort=writer_effort,
            author_prompt=source["author_prompt"],
            critic_prompt=critic_prompt,
            final_prompt=final_prompt,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
        )
        print(
            f"Saved-candidate audit started in {app.run_dir}",
            file=error_stream, flush=True,
        )
        while app.snapshot()["phase"] in ACTIVE_PHASES:
            time.sleep(0.25)
    except KeyboardInterrupt:
        try:
            app.stop()
        except ValueError:
            pass
        print("Saved-candidate audit stopped.", file=error_stream, flush=True)
        return 130
    state = app.snapshot()
    if state.get("error"):
        print(
            f"Saved-candidate audit failed: {state['error']}",
            file=error_stream, flush=True,
        )
        return 1
    print(
        f"Saved-candidate audit finished in {app.run_dir}",
        file=error_stream, flush=True,
    )
    return 0


def main():
    """Run Markdown proofs from the terminal or start the browser interface."""

    if sys.argv[1:2] == ["--review-worker"]:
        return review_worker_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
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
            f"accept after N non-rejecting critic rounds (default: {DEFAULT_CRITIC_ROUNDS})"
        ),
    )
    parser.add_argument(
        "-thinkingHours", "--thinkingHours", "--thinking-hours",
        dest="thinking_hours", type=float, default=DEFAULT_THINKING_HOURS,
        metavar="HOURS",
        help=f"total elapsed-workflow limit (default: {DEFAULT_THINKING_HOURS})",
    )
    model_defaults = {
        "author": DEFAULT_AUTHOR_MODEL,
        "critic": DEFAULT_CRITIC_MODEL,
        "writer": DEFAULT_WRITER_MODEL,
    }
    for role in ("author", "critic", "writer"):
        camel = f"{role}Model"
        parser.add_argument(
            f"-{camel}", f"--{camel}", f"--{role}-model",
            dest=f"{role}_model", choices=MODELS,
            default=model_defaults[role],
            help=f"{role} model",
        )
    parser.add_argument(
        "-reasoningEffort", "--reasoningEffort", "--reasoning-effort",
        dest="reasoning_effort", choices=EFFORTS,
        default=DEFAULT_REASONING_EFFORT,
        help="fallback reasoning effort for every proof role",
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
        help=f"generation speed (default: {DEFAULT_SPEED})",
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
    if args.resume_critic:
        if args.input_path:
            parser.error("Do not combine input_path with --resume-critic.")
        try:
            resume_source = saved_critic_source(args.resume_critic)
        except (OSError, TypeError, ValueError) as exc:
            print(f"Cannot resume saved critic: {exc}", file=sys.stderr)
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
    resume_app = None
    if resume_source:
        try:
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
                resume_source["run_dir"], resume_settings
            )
        except (OSError, TypeError, ValueError) as exc:
            server.server_close()
            print(f"Cannot resume saved critic: {exc}", file=sys.stderr)
            return 1
    # A URL fragment stays in the browser; JavaScript sends it as a secret header.
    job_query = (
        f"?job={quote(resume_app.state['runId'])}" if resume_app else ""
    )
    url = f"{server.origin}/{job_query}#{server.token}"
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
