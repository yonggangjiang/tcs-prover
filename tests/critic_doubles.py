"""Stand-ins for the convention critic and the critic's `fixes` field, for tests of the critic step.

The critic is one call (the "Critic coordinator") that starts its independent
critics as its own subagents, so a test scripts that call like any other. After
the critic accepts a proof, the convention critic runs once (its own workflow
step, "Convention critic"). A test that scripts answers by call order passes
every other model call to its script and lets these stand-ins answer the
convention critic, told apart by its activity label, never by call order.

The critic's answer has a `fixes` field: the bugs it fixed in proof.md. A
scripted critic answer without one gets the field the workflow requires: a
short note when its proof differs from the submitted one, else "" (see
with_fixes).
"""

import json

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime

CONVENTION_LABEL = "Convention critic"
COORDINATOR_LABEL = "Critic coordinator"
FAITHFUL = {"verdict": "faithful", "intended": "A proof of the statement as a mathematician reads it.",
            "assessment": "The proof does what the statement asks.", "loophole": "", "suggested_statement": ""}
LOOPHOLE = {"verdict": "loophole", "intended": "An efficient algorithm, as a complexity theorist reads it.",
            "assessment": "The proof stores every answer in a table.",
            "loophole": "The statement bounds no space, so a brute-force table of all answers satisfies it.",
            "suggested_statement": "Prove the bound for an algorithm that uses polynomial space."}
FIXED_NOTE = "Expanded the omitted step."


def convention_critic(settings):
    """Whether a structured() or run_structured_attempt() call is the convention critic's."""

    return settings.get("activity_label") == CONVENTION_LABEL


def with_fixes(value, settings):
    """A scripted critic answer with the `fixes` field the critic's schema requires.

    An answer that has the field keeps it. Otherwise the field says what was
    fixed when the answer's proof differs from the submitted proof.md (the
    workflow requires that), and is empty when it does not.
    """

    if (not isinstance(value, dict) or "fixes" in value or "verdict" not in value or "bugs" not in value
            or settings.get("activity_label", settings.get("request_label")) != COORDINATOR_LABEL):
        return value
    submitted = dict(settings.get("input_files") or ()).get("proof.md")
    solution = value.get("solution")
    # A read-only visit (the final check, which gets no editable file) keeps the proof whatever it answers.
    changed = (isinstance(solution, str) and submitted is not None and "edit_file" in settings
               and runtime.normalized_text(solution) != runtime.normalized_text(submitted))
    return {**value, "fixes": FIXED_NOTE if changed else ""}


def coordinator(effect, convention=FAITHFUL):
    """A stand-in structured(): the convention critic answers `convention`; other calls follow `effect` like a
    mock side_effect, and a critic answer gets its `fixes` field (with_fixes).

    `effect` is a function called with the call's arguments, an exception
    raised by every call, or a list of results (or exceptions) returned one
    per call, in order.
    """

    results = iter(effect) if isinstance(effect, (list, tuple)) else None

    def structured(prompt, schema, stage, **settings):
        if convention_critic(settings):
            return dict(convention), json.dumps(convention)
        if results is not None:
            value = next(results)
            if isinstance(value, BaseException):
                raise value
        elif isinstance(effect, BaseException):
            raise effect
        else:
            value = effect(prompt, schema, stage, **settings)
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], dict):
            answer = with_fixes(value[0], settings)
            if answer is not value[0]:
                return answer, json.dumps(answer)
        return value

    return structured


def coordinator_attempts(attempt, convention=FAITHFUL):
    """A stand-in run_structured_attempt(): the convention critic answers `convention`; other attempts go to
    `attempt`."""

    def run(text, schema, stage, *args, **settings):
        if convention_critic(settings):
            return json.dumps(convention)
        return attempt(text, schema, stage, *args, **settings)

    return run
