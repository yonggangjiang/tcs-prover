"""Independent statement checking, including its isolated worker entry point."""

import argparse
import json
import sys

import workflow_runner as runtime


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["statement", "notes"],
    "additionalProperties": False,
}

REVIEW_PROMPT = (
    'Read the current statement below carefully and produce a rigorous, self-contained problem statement without changing its intended claim. \n'
    'Do a initial scanning on corner cases, edge cases, counter examples to see if the statement is trivial or false. \n'
    'If you found the statement is trivial or false, first try to clear typos, fix any ambiguities, or add missing context or conventional assumptions to make the statement non-trivial. If you can fix it, explain the fix in the note, and return the final problem statement. Remember to check the problem statement again until it passed your audit. If you cannot fix it, explain why in the notes and return the version you think is the best possible statement.\n'
    'If the statement remains non-trivial and open after your scanning, then return a complete, rigorous, self-contained problem statement.\n'
    'The returned problem statement should use conventional notations and terminologies in TCS, especially asymptotic bounds notations, to avoid overly complex expressions and guarantee conciseness, while maintaining the rigour of the original statement. \n'
    'Return only the requested JSON.'
)


def review_prompt(draft, feedback="", instructions=REVIEW_PROMPT):
    """Return the exact review prompt sent to the reviewer model."""

    instructions = runtime.text(instructions)
    feedback = feedback.strip()
    if feedback:
        return (
            f"{instructions}\n\nREVIEW TASK\nMODE: REVISION\n"
            "Revise the current checked statement in response to the author's "
            "request while preserving the intended claim."
            f"\n\nCURRENT CHECKED STATEMENT:\n{runtime.text(draft)}"
            f"\n\nAUTHOR REVISION REQUEST:\n{feedback}"
        )
    return (
        f"{instructions}\n\nREVIEW TASK\nMODE: INITIAL\n"
        f"\nDRAFT:\n{runtime.text(draft)}"
    )


def review(
    draft, feedback="", model=runtime.REVIEW_MODEL, effort=runtime.REVIEW_EFFORT,
    instructions=REVIEW_PROMPT, speed=runtime.DEFAULT_SPEED,
    summary=runtime.DEFAULT_REASONING_SUMMARY, thinking_hours=None,
):
    """Review with the user's chosen model.

    Like a workflow node, the review retries every failed attempt each
    minute, each attempt has a timeout (doubled after consecutive
    timeouts), and the job's total time limit (``thinking_hours``, 0 for
    unlimited) bounds the whole review; retry waits do not count against it.
    """

    options = {"thinking_hours": runtime.DEFAULT_AUTHOR_HOURS if thinking_hours is None else thinking_hours}

    def empty_statement(notes):
        # Blame the reviewer, not the user's draft, and keep its notes.
        # The UI reads one "error:" line, so the notes become one line.
        detail = " ".join(notes.split())
        if len(detail) > 1000:
            detail = detail[:1000] + "..."
        return ("The reviewer returned an empty statement"
                + (f". Reviewer notes: {detail}" if detail
                   else "; see the activity log for its output."))

    def check(report, raw):
        # An empty statement is an unusable answer: the shared retry loop
        # asks the reviewer again, with its own notes in view.
        if not report["statement"].strip():
            raise runtime.InvalidAnswer("the statement field must not be empty",
                                        detail=empty_statement(report["notes"]),
                                        message=empty_statement(report["notes"]))
        return report

    try:
        runtime.chosen_model(model)
        effort = runtime.chosen_effort(effort)
        report, raw = runtime.structured(
            review_prompt(draft, feedback, instructions), REVIEW_SCHEMA, "review",
            model=model, effort=effort, speed=speed, summary=summary, validate=check,
            timeout=runtime.STRUCTURED_TIMEOUT_SECONDS, options=options,
        )
        statement = report["statement"].strip()
        notes = report["notes"].strip()
        if not statement:
            raise runtime.Error(empty_statement(notes))
        report = {"statement": statement, "notes": notes}
        runtime.emit(
            "review_result", "review", label="Exact structured response",
            text=raw, review=report,
        )
        return report
    except (KeyError, TypeError, AttributeError) as exc:
        raise runtime.Error("The reviewer returned an invalid review.") from exc


def review_worker_main(argv):
    """Execute the independent statement-review node in an isolated process."""

    runtime.configure_standard_streams()
    parser = argparse.ArgumentParser(description="Internal statement-review worker")
    # Any id the registry knows: built-in, older Codex, or a configured api:<id>.
    parser.add_argument("--review-model", type=runtime.model_argument, default=runtime.REVIEW_MODEL)
    parser.add_argument("--review-effort", choices=runtime.EFFORTS, default=runtime.REVIEW_EFFORT)
    parser.add_argument("--speed", choices=runtime.SPEEDS, default=runtime.DEFAULT_SPEED)
    parser.add_argument("--reasoning-summary", choices=runtime.REASONING_SUMMARIES, default=runtime.DEFAULT_REASONING_SUMMARY)
    parser.add_argument("--review-prompt-file")
    parser.add_argument("--thinking-hours", type=runtime.author_hours, default=runtime.DEFAULT_AUTHOR_HOURS)
    args = parser.parse_args(argv)
    try:
        statement, _, feedback = sys.stdin.read().partition("\0")
        review(
            statement, feedback, args.review_model, args.review_effort,
            runtime.prompt_file(args.review_prompt_file, REVIEW_PROMPT),
            speed=args.speed, summary=args.reasoning_summary, thinking_hours=args.thinking_hours,
        )
        return 0
    except KeyboardInterrupt:
        print("\nStopped review.", file=sys.stderr)
        return 130
    except (runtime.Error, OSError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {runtime.redact(exc)}", file=sys.stderr)
        return 1
