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
    'The returned problem statement should just be a complete, rigorous, self-contained problem statement without any commentary or notes. The notes field should contain your reasoning, explanation of any fixes, and any remaining concerns about the statement.\n'
    'Return only the requested JSON.'
)


def review_prompt(draft, feedback="", instructions=REVIEW_PROMPT):
    """Return the exact review prompt sent to Codex."""

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
    draft, feedback="", model=runtime.MODEL, effort=runtime.EFFORT,
    instructions=REVIEW_PROMPT, speed=runtime.DEFAULT_SPEED,
):
    """Review with the user's chosen model."""

    try:
        runtime.chosen_model(model)
        effort = runtime.chosen_effort(effort)
        report, raw = runtime.structured(
            review_prompt(draft, feedback, instructions), REVIEW_SCHEMA, "review",
            model=model, effort=effort, speed=speed,
        )
        report = {
            "statement": runtime.text(report["statement"]),
            "notes": report["notes"].strip(),
        }
        runtime.emit(
            "review_result", "review", label="Exact structured response",
            text=raw, review=report,
        )
        return report
    except (KeyError, TypeError, AttributeError) as exc:
        raise runtime.Error("Codex returned an invalid review.") from exc


def review_worker_main(argv):
    """Execute the independent statement-review node in an isolated process."""

    runtime.configure_standard_streams()
    parser = argparse.ArgumentParser(description="Internal statement-review worker")
    parser.add_argument("--review-model", choices=runtime.MODELS, default=runtime.MODEL)
    parser.add_argument("--review-effort", choices=runtime.EFFORTS, default=runtime.EFFORT)
    parser.add_argument("--speed", choices=runtime.SPEEDS, default=runtime.DEFAULT_SPEED)
    parser.add_argument("--review-prompt-file")
    args = parser.parse_args(argv)
    try:
        statement, _, feedback = sys.stdin.read().partition("\0")
        review(
            statement, feedback, args.review_model, args.review_effort,
            runtime.prompt_file(args.review_prompt_file, REVIEW_PROMPT),
            speed=args.speed,
        )
        return 0
    except KeyboardInterrupt:
        print("\nStopped review.", file=sys.stderr)
        return 130
    except (runtime.Error, OSError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
