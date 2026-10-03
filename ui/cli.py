"""Serve the TCS Prover UI, prove one or more Markdown statements, or check a
supplied proof with the critic only."""

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
from . import batch_summary
from .review import review_worker_main
from .server import (
    App, Server, AUTHOR_WORKFLOWS, CRITIC_REJECTED_MESSAGE, DEFAULT_AUTHOR_MODEL,
    DEFAULT_CRITIC_MODEL, DEFAULT_CRITIC_ROUNDS, DEFAULT_REASONING_EFFORT,
    DEFAULT_REASONING_SUMMARY, DEFAULT_SPEED, DEFAULT_THINKING_HOURS,
    DEFAULT_WRITER_MODEL, EFFORTS, HOST, MODELS, NEW_JOB_AUTHOR_WORKFLOW, PORT,
    REASONING_SUMMARIES, RUNS, SPEEDS, read_utf8,
    saved_critic_source, saved_research_source, workflow_option_defaults,
)

ACTIVE_PHASES = {"reviewing", "running", "stopping"}
WORKFLOW_ALIASES = {"cheap": "author_critic_cheap"}


def workflow_argument(value):
    """Accept an author/critic workflow name, or the alias cheap."""

    name = WORKFLOW_ALIASES.get(value, value)
    if name not in AUTHOR_WORKFLOWS:
        raise argparse.ArgumentTypeError(
            f"invalid workflow {value!r} (choose author_critic, author_critic_cheap, or cheap)"
        )
    return name


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
    author_workflow=None,
    latex_writer=False,
):
    """Load optional prompt files and validate direct-workflow CLI settings.

    Terminal runs default to author_critic_cheap, which always runs the simple
    author without research files, audits, or solvers; author_critic keeps the
    managed author with research files.
    """

    if author_workflow is None:
        author_workflow = NEW_JOB_AUTHOR_WORKFLOW
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
        author_workflow=author_workflow,
        latex_writer=latex_writer,
    )
    return {
        "author_workflow": options["authorWorkflow"],
        "latex_writer": options["latexWriter"],
        "file_management": options["fileManagement"],
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


def research_resume_cli_settings(args, argv):
    """Override saved author settings only for options explicitly supplied."""

    supplied = {argument.split("=", 1)[0] for argument in argv if argument.startswith("-")}
    fields = {
        "criticRounds": ("critic_rounds", "critic-rounds"),
        "thinkingHours": ("thinking_hours", "thinking-hours"),
        "reasoningEffort": ("reasoning_effort", "reasoning-effort"),
        "speedMode": ("speed_mode", "speed-mode"),
        "reasoningSummary": ("reasoning_summary", "reasoning-summary"),
    }
    for role in ("author", "critic", "writer"):
        fields[f"{role}Model"] = (f"{role}_model", f"{role}-model")
        fields[f"{role}Effort"] = (f"{role}_effort", f"{role}-effort")
    options = {
        key: getattr(args, attribute)
        for key, (attribute, kebab) in fields.items()
        if supplied.intersection({f"-{key}", f"--{key}", f"--{kebab}"})
    }
    if "reasoningEffort" in options:
        for role in ("author", "critic", "writer"):
            options.setdefault(f"{role}Effort", options["reasoningEffort"])
    for role in ("author", "critic", "final"):
        path = getattr(args, f"{role}_prompt_file")
        if path:
            options[f"{role}Prompt"] = read_utf8(path, f"{role} prompt")
    # A continuation keeps its saved workflow; a supplied one must match it.
    if getattr(args, "workflow", None) is not None:
        options["authorWorkflow"] = args.workflow
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
    folder = Path(path).expanduser().resolve()
    batch = folder.is_dir()
    batch_id = ""
    if batch:
        # One summary for the whole folder run, rewritten as its jobs finish.
        batch_id = batch_summary.create_batch(
            runs, folder=folder.name, folder_path=str(folder),
            sources=[source.name for source, _ in statements],
            workflow=options["author_workflow"], origin="terminal",
            workflow_options=workflow_option_defaults(options["author_workflow"]),
            custom_prompts=[
                role for role in ("author", "critic", "final")
                if settings.get(f"{role}_prompt_file")
            ],
        )["id"]
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
            app.start_direct_statement(
                statement=statement, source_file=source.name, **options,
            )
            if batch_id:
                batch_summary.add_run(runs, batch_id, app.run_dir.name, source.name)
            print(
                f"[{source.name}] Proof started in {app.run_dir}",
                file=error_stream, flush=True,
            )
    except KeyboardInterrupt:
        _stop_headless_apps(apps)
        print("All active proofs stopped.", file=error_stream, flush=True)
        _print_batch_summary(runs, batch_id, error_stream)
        return 130
    except Exception:
        _stop_headless_apps(apps)
        _print_batch_summary(runs, batch_id, error_stream)
        raise
    if batch_id:
        path = _write_batch_summary(runs, batch_id)
        if path:
            print(
                f"Folder run summary (rewritten as jobs finish): {path}",
                file=error_stream, flush=True,
            )

    try:
        while any(app.snapshot()["phase"] in ACTIVE_PHASES for _, app in apps):
            time.sleep(0.25)
    except KeyboardInterrupt:
        _stop_headless_apps(apps)
        print("All active proofs stopped.", file=error_stream, flush=True)
        _print_batch_summary(runs, batch_id, error_stream)
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
    _print_batch_summary(runs, batch_id, error_stream)
    return 1 if failed else 0


def _write_batch_summary(runs, batch_id):
    """Rewrite one folder run's summary; return its path, or None if that failed."""

    try:
        return batch_summary.write_summary(runs, batch_id)
    except (OSError, ValueError):
        return None


def _print_batch_summary(runs, batch_id, error_stream):
    if not batch_id:
        return
    path = _write_batch_summary(runs, batch_id)
    if path:
        print(f"Folder run summary: {path}", file=error_stream, flush=True)


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


def run_headless_critic_only(
    statement_path, proof_path, runs=RUNS, output_stream=None, error_stream=None,
    verbose_events=False, critic_rounds=DEFAULT_CRITIC_ROUNDS,
    thinking_hours=DEFAULT_THINKING_HOURS,
    critic_model=DEFAULT_CRITIC_MODEL, writer_model=DEFAULT_WRITER_MODEL,
    reasoning_effort=DEFAULT_REASONING_EFFORT,
    critic_effort=None, writer_effort=None,
    speed_mode=DEFAULT_SPEED,
    reasoning_summary=DEFAULT_REASONING_SUMMARY,
    critic_prompt_file=None, final_prompt_file=None,
    author_workflow=None,
    latex_writer=False,
):
    """Check one supplied proof with the critic only: no statement review, no author.

    Returns 0 when the critic passes and the LaTeX editor succeeds, and 1 when
    the critic rejects (its report is saved in the run folder) or a stage fails.
    """

    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    files = markdown_inputs(statement_path)
    if len(files) != 1 or Path(statement_path).expanduser().resolve().is_dir():
        raise ValueError("A critic-only review needs one statement file, not a folder.")
    source = files[0]
    statement = read_utf8(source, "statement")
    proof = read_utf8(proof_path, "proof")
    prompts = {
        "critic_prompt": read_utf8(critic_prompt_file, "critic prompt") if critic_prompt_file else None,
        "final_prompt": read_utf8(final_prompt_file, "final prompt") if final_prompt_file else None,
    }
    lock = threading.Lock()
    job_output = (
        output_stream if verbose_events
        else ConciseHeadlessOutput(output_stream, lock, source.name)
    )
    app = App(runs=runs, output_stream=job_output)
    try:
        app.start_critic_resume(
            statement=statement,
            solution=proof,
            critic_only=True,
            author_workflow=author_workflow or NEW_JOB_AUTHOR_WORKFLOW,
            latex_writer=latex_writer,
            critic_rounds=critic_rounds,
            thinking_hours=thinking_hours,
            critic_model=critic_model,
            writer_model=writer_model,
            reasoning_effort=reasoning_effort,
            critic_effort=critic_effort,
            writer_effort=writer_effort,
            speed_mode=speed_mode,
            reasoning_summary=reasoning_summary,
            source_file=source.name,
            **prompts,
        )
        print(
            f"[{source.name}] Critic-only review started in {app.run_dir}",
            file=error_stream, flush=True,
        )
        while app.snapshot()["phase"] in ACTIVE_PHASES:
            time.sleep(0.25)
    except KeyboardInterrupt:
        try:
            app.stop()
        except ValueError:
            pass
        print(f"[{source.name}] Critic-only review stopped.", file=error_stream, flush=True)
        return 130
    error = app.snapshot().get("error", "")
    if error == CRITIC_REJECTED_MESSAGE:
        print(
            f"[{source.name}] The critic rejected the proof. Report: "
            f"{app.run_dir / 'failure-summary.md'}",
            file=error_stream, flush=True,
        )
        return 1
    if error:
        print(
            f"[{source.name}] Critic-only review failed: {error}",
            file=error_stream, flush=True,
        )
        return 1
    print(
        f"[{source.name}] The critic passed the proof. "
        + (f"LaTeX: {app.run_dir / 'final.tex'}" if latex_writer
           else f"Proof: {app.run_dir / 'final-proof.md'}"),
        file=error_stream, flush=True,
    )
    return 0


def main():
    """Run Markdown proofs from the terminal or start the browser interface."""

    if sys.argv[1:2] == ["--review-worker"]:
        return review_worker_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Markdown runs skip statement review and start with the proof author.\n\n"
            "examples:\n"
            "  python3 web_ui.py statement.md                           "
            "cost-optimized run (default)\n"
            "  python3 web_ui.py statement.md --workflow author_critic  "
            "managed run with research files\n"
            "  python3 web_ui.py statements/                            "
            "one parallel job per top-level .md file\n"
            "  python3 web_ui.py statement.md --critic-only proof.md    "
            "check proof.md with the critic only"
        ),
    )
    parser.add_argument(
        "input_path", nargs="?",
        help="UTF-8 .md statement file or folder of top-level .md files",
    )
    parser.add_argument(
        "-criticOnly", "--criticOnly", "--critic-only", dest="critic_only",
        metavar="PROOF",
        help=(
            "check PROOF, a UTF-8 file with a complete proof of the statement in "
            "input_path, with the critic only: no statement review and no author. "
            "A pass continues to the LaTeX editor (exit status 0); a rejection ends "
            "the job with the critic's report (exit status 1)"
        ),
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
    parser.add_argument(
        "-summary", "--summary", dest="summary", nargs="+", metavar="RUN",
        help=(
            "write the folder-run summary of existing jobs and print its path: a batch "
            "folder under runs/batches/, or run folders under runs/ (a new batch then "
            "lists them, and its summary is kept up to date when they resume)"
        ),
    )
    parser.add_argument(
        "-summaryName", "--summaryName", "--summary-name", dest="summary_name",
        metavar="NAME", default="",
        help="folder name shown in a summary written for run folders",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--verbose-events", action="store_true",
        help="print every public JSONL event during a Markdown terminal run",
    )
    parser.add_argument(
        "-latex", "--latex", dest="latex", action="store_true",
        help=(
            "also run the LaTeX writer and compile a PDF after the critic approves "
            "(off by default: a new run ends with the approved proof in "
            "final-proof.md). A resumed job keeps its saved choice"
        ),
    )
    parser.add_argument(
        "-workflow", "--workflow", dest="workflow", type=workflow_argument,
        default=None, metavar="NAME",
        help=(
            "author/critic workflow for a Markdown or critic-only run: "
            "author_critic_cheap (alias: cheap; the default; the cost-optimized "
            "simple author with no research files, audits, or fresh-eyes solvers) "
            "or author_critic (the managed author with research files, the earlier "
            "terminal default). A resumed job keeps its saved workflow and rejects "
            "a different NAME"
        ),
    )
    parser.add_argument(
        "-criticRounds", "--criticRounds", "--critic-rounds",
        dest="critic_rounds", type=int, default=DEFAULT_CRITIC_ROUNDS,
        metavar="N",
        help=(
            "allow up to N consecutive critic repair rounds "
            f"(default: {DEFAULT_CRITIC_ROUNDS}); an unchanged pass finishes "
            "early, an edited pass finishes at N, and rejection returns to the author "
            "(with --critic-only, a rejection ends the job)"
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
    if args.summary:
        if args.input_path or args.critic_only or args.resume_critic or args.resume_research:
            parser.error("--summary cannot be combined with a statement, --critic-only, or a resume.")
        try:
            path = batch_summary.summarize_existing(RUNS, args.summary, args.summary_name)
        except (OSError, ValueError) as exc:
            print(f"Cannot write the summary: {exc}", file=sys.stderr)
            return 1
        print(path)
        return 0
    if args.summary_name:
        parser.error("--summary-name applies only with --summary.")
    if args.critic_only is not None:
        if args.resume_critic or args.resume_research:
            parser.error("--critic-only cannot be combined with --resume-critic or --resume-author.")
        if not args.input_path:
            parser.error(
                "--critic-only needs the statement file: "
                "python3 web_ui.py statement.md --critic-only proof.md"
            )
        statement_path = Path(args.input_path).expanduser()
        if statement_path.is_dir():
            parser.error(
                f"--critic-only checks the proof of one statement file, not a folder: {args.input_path}"
            )
        if not statement_path.is_file():
            parser.error(f"The statement file does not exist: {args.input_path}")
        if not Path(args.critic_only).expanduser().is_file():
            parser.error(f"The proof file does not exist: {args.critic_only}")
    if sum(bool(value) for value in (args.input_path, args.resume_critic, args.resume_research)) > 1:
        parser.error("Choose only one of input_path, --resume-critic, or --resume-author.")
    if args.workflow is not None and not (args.input_path or args.resume_critic or args.resume_research):
        parser.error("--workflow applies to a Markdown run or a resumed job; in the web UI choose Advanced → Workflow.")
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
        if args.critic_only is not None:
            try:
                return run_headless_critic_only(
                    args.input_path, args.critic_only,
                    critic_rounds=args.critic_rounds,
                    thinking_hours=args.thinking_hours,
                    critic_model=args.critic_model,
                    writer_model=args.writer_model,
                    reasoning_effort=args.reasoning_effort,
                    critic_effort=args.critic_effort,
                    writer_effort=args.writer_effort,
                    speed_mode=args.speed_mode,
                    reasoning_summary=args.reasoning_summary,
                    critic_prompt_file=args.critic_prompt_file,
                    final_prompt_file=args.final_prompt_file,
                    author_workflow=args.workflow,
                    latex_writer=args.latex,
                    verbose_events=args.verbose_events,
                )
            except (OSError, TypeError, ValueError) as exc:
                print(f"Cannot start critic-only review: {exc}", file=sys.stderr)
                return 1
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
                author_workflow=args.workflow,
                latex_writer=args.latex,
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
            # Only an explicit --workflow is forwarded; it must match the saved job.
            if args.workflow is not None:
                resume_settings["authorWorkflow"] = args.workflow
            if args.resume_research:
                resume_app = server.start_saved_research_job(
                    resume_source["run_dir"],
                    research_resume_cli_settings(args, sys.argv[1:]),
                )
            else:
                resume_app = server.start_saved_critic_job(
                    resume_source["run_dir"], resume_settings,
                )
        except (OSError, TypeError, ValueError) as exc:
            server.server_close()
            print(f"Cannot resume saved job: {exc}", file=sys.stderr)
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
