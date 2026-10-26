"""Every failed model call is retried every minute without a limit, keeping its conversation.

Offline: stand-in Codex and Claude CLIs, scripted app-server doubles and a
patched clock; no model call is made and no wait takes real time. Every
stand-in sleep stops a runaway retry loop with Runaway, so a regression fails
its test instead of hanging the suite.
"""

import functools
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import workflow_runner as runtime
from runner import claude_cli
from ui import cli as terminal
from ui import server

import test_claude_cli as claude_test

ROOT = Path(runtime.__file__).parent
SCHEMA = {"type": "object", "properties": {"statement": {"type": "string"}, "notes": {"type": "string"}},
          "required": ["statement", "notes"], "additionalProperties": False}
CAPACITY = "Selected model is at capacity. Please try a different model."
# Provider failures as Codex and Claude report them (verified against Codex 0.160 and Claude Code 2.1.289).
FAILURES = (
    CAPACITY,
    "API Error: 529 Overloaded. This is a server-side issue, usually temporary.",
    "exceeded retry limit, last status: 429 Too Many Requests",
    "unexpected status 503 Service Unavailable: upstream connect error",
    "unexpected status 500 Internal Server Error",
    "stream disconnected before completion: stream closed before response.completed",
)
VALID = '{"statement": "S", "notes": "ok"}'
# The unpatched jitter (RetryCase replaces it with 0 so that waits are exact).
REAL_JITTER = runtime._retry_jitter


class Runaway(BaseException):
    """A stand-in sleep's limit: a retry loop that never ends fails its test at once."""


class FakeClock:
    """Real elapsed time plus every patched retry wait, which takes no real time."""

    def __init__(self, limit=24 * 3600):
        self.offset, self.sleeps, self.limit = 0.0, [], limit

    def now(self):
        return time.monotonic() + self.offset

    def wall(self):
        return time.time() + self.offset

    def sleep(self, seconds, cancel=None):
        self.sleeps.append(seconds)
        self.offset += seconds
        if self.offset >= self.limit:
            raise Runaway("runaway retry loop")


def guarded(action=None, limit=2000):
    """A stand-in sleep that runs `action` and raises Runaway after `limit` calls."""

    calls = {"count": 0}

    def sleep(seconds, cancel=None):
        calls["count"] += 1
        if calls["count"] > limit:
            raise Runaway("runaway retry loop")
        if action is not None:
            return action(seconds, cancel)

    sleep.calls = calls
    return sleep


def patch_clock(case, clock):
    for name, value in (("_now", clock.now), ("_wall_clock", clock.wall), ("_retry_sleep", clock.sleep),
                        ("_retry_jitter", lambda seconds: 0.0), ("MODEL_RETRY_SECONDS", 60)):
        patcher = mock.patch.object(runtime, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)


class RetryCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.clock = FakeClock()
        patch_clock(self, self.clock)
        self.events = []
        emitter = mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: self.events.append(
            {"kind": kind, "stage": stage, **fields}))
        emitter.start()
        self.addCleanup(emitter.stop)

    def statuses(self, label=runtime.RETRY_LABEL):
        return [event for event in self.events if event["kind"] == "status" and event.get("label") == label]

    def notices(self, label=runtime.RETRY_LABEL):
        return [event for event in self.statuses(label) if "retryAt" in event]

    def waits(self):
        """Seconds of retry waiting so far (the patched sleeps take no real time)."""
        return round(sum(self.clock.sleeps), 6)


class SessionRecorder:
    """Persisted sessions a stand-in attempt reports to its conversation, and the ones deleted."""

    def __init__(self):
        self.deleted = []

    def delete(self, session_id):
        self.deleted.append(session_id)
        return True

    def open(self, conversation, session_id, output):
        conversation.session_id, conversation.confirmed, conversation.output = session_id, True, output
        conversation.created(session_id, self.delete)


class StructuredRetryTests(RetryCase):
    def run_attempts(self, outcomes, **kwargs):
        """structured() with a patched attempt that fails or answers as `outcomes` says."""
        calls = []

        def attempt(text, schema, stage, *args, **settings):
            calls.append({"text": text, **settings})
            outcome = outcomes[min(len(calls), len(outcomes)) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            result = runtime.structured("Review S.", SCHEMA, "review", **kwargs)
        return result, calls

    def test_twenty_five_provider_failures_then_success_with_one_minute_spacing(self):
        failures = [runtime.Error(f"Codex review failed: {FAILURES[index % len(FAILURES)]}") for index in range(25)]
        (result, raw), calls = self.run_attempts([*failures, VALID])
        self.assertEqual(result, {"statement": "S", "notes": "ok"})
        self.assertEqual(len(calls), 26)
        # Each wait is exactly MODEL_RETRY_SECONDS (jitter patched to 0), in slices Stop and pause can interrupt.
        self.assertEqual(runtime.MODEL_RETRY_SECONDS, 60)
        self.assertEqual(self.waits(), 25 * 60)
        self.assertTrue(all(step <= runtime.RETRY_POLL_SECONDS for step in self.clock.sleeps))
        notices = self.notices()
        self.assertEqual(len(notices), 25)
        self.assertEqual(notices[0]["text"], "Model call failed (model at capacity) — retrying in 1 min (attempt 2). "
                                             f"Provider message: {CAPACITY}")
        self.assertEqual([event["failure"] for event in notices[:6]],
                         ["model at capacity", "model at capacity", "rate limited", "model at capacity",
                          "provider server error", "connection lost"])
        # The job page says how long the call has been failing.
        self.assertIn("(attempt 26, retrying for 24 min)", notices[-1]["text"])
        self.assertEqual(notices[-1]["retryingSeconds"], 24 * 60)
        self.assertTrue(all(event["retryInSeconds"] == 60 for event in notices))
        requests = [event for event in self.events if event["kind"] == "request"]
        self.assertEqual([event["attempt"] for event in requests], list(range(1, 27)))
        # Without any model output there is nothing to continue: each retry re-runs the exact input.
        self.assertTrue(all(call["text"] == calls[0]["text"] for call in calls))
        self.assertTrue(all(call["conversation"].fork_from is None for call in calls))
        self.assertEqual(requests[1]["text"], "(Identical to the exact input of attempt 1.)")
        diagnostics = [event for event in self.events if event["kind"] == "diagnostic"]
        self.assertEqual([event["attempt"] for event in diagnostics], list(range(1, 26)))

    def test_retries_are_spread_by_a_few_seconds_of_jitter(self):
        spread = []

        def jitter(seconds):
            spread.append(REAL_JITTER(seconds))
            return spread[-1]

        with mock.patch.object(runtime, "_retry_jitter", side_effect=jitter):
            self.run_attempts([runtime.Error(f"Codex review failed: {CAPACITY}")] * 8 + [VALID])
        self.assertEqual(len(spread), 8)
        self.assertTrue(all(0.0 <= value <= runtime.MODEL_RETRY_JITTER_SECONDS == 5 for value in spread))
        self.assertAlmostEqual(self.waits(), 8 * 60 + sum(spread), places=6)
        # The nominal interval stays one minute.
        self.assertTrue(all(event["retryInSeconds"] == 60 and "retrying in 1 min" in event["text"]
                            for event in self.notices()))
        # Short waits get at most a twelfth of their length.
        self.assertEqual(REAL_JITTER(0), 0.0)
        self.assertLessEqual(max(REAL_JITTER(2) for _ in range(100)), 2 / 12)
        self.assertLessEqual(max(REAL_JITTER(60) for _ in range(200)), 5.0)

    def test_waits_do_not_consume_the_workflow_budget(self):
        options = {"thinking_hours": 0.01}  # 36 seconds.
        failures = [runtime.Error(f"Claude review failed: {CAPACITY}")] * 5
        (result, _), calls = self.run_attempts([*failures, '{"statement": "S", "notes": ""}'], options=options)
        self.assertEqual(result["statement"], "S")
        self.assertEqual(self.waits(), 300)
        # Five minutes of waiting left the 36-second budget almost untouched.
        self.assertGreater(runtime.workflow_remaining(options), 30)
        self.assertAlmostEqual(runtime.retry_clock(options).excluded(), 300, delta=1)
        clock = [event["budgetClock"] for event in self.statuses() if "budgetClock" in event]
        self.assertEqual(clock, ["paused", "running"] * 5)

    def test_no_attempt_starts_after_the_deadline_and_none_outlives_it(self):
        options = {"thinking_hours": 1, "elapsed_seconds": 3600 - 10}
        timeouts = []

        def attempt(text, schema, stage, *args, **settings):
            timeouts.append(settings["timeout"])
            self.clock.offset += 6  # Each failed attempt works for six seconds.
            raise runtime.Error("Codex review failed: stream disconnected before completion")

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            with self.assertRaisesRegex(runtime.Error, "Workflow time limit reached"):
                runtime.structured("Review S.", SCHEMA, "review", timeout=900, options=options)
        self.assertEqual(len(timeouts), 2)
        self.assertLessEqual(timeouts[0], 10)
        self.assertLessEqual(timeouts[1], 4)
        # The one-minute wait between them did not count: only 12 working seconds elapsed.
        self.assertEqual(self.waits(), 60)

    def test_stop_ends_a_wait_at_once(self):
        calls = []
        with mock.patch.object(runtime, "_retry_sleep", side_effect=guarded(
                lambda seconds, cancel: (_ for _ in ()).throw(KeyboardInterrupt))), \
                mock.patch.object(runtime, "run_structured_attempt",
                                  side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                                      runtime.Error(f"Codex review failed: {CAPACITY}"))):
            with self.assertRaises(KeyboardInterrupt):
                runtime.structured("Review S.", SCHEMA, "review", options={})
        self.assertEqual(len(calls), 1)
        self.assertEqual([event["budgetClock"] for event in self.events if "budgetClock" in event],
                         ["paused", "running"])

    def test_a_pause_request_during_a_wait_pauses_the_workflow(self):
        pause = self.root / "pause-request.json"
        sleep = guarded(lambda seconds, cancel: pause.write_text("{}"), limit=10)
        with mock.patch.object(runtime, "_retry_sleep", side_effect=sleep), \
                mock.patch.object(runtime, "run_structured_attempt",
                                  side_effect=runtime.Error(f"Codex critic failed: {CAPACITY}")) as attempt:
            with self.assertRaises(runtime.WorkflowPaused):
                runtime.structured("Review S.", SCHEMA, "critic", options={"goal_pause_file": str(pause)})
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(sleep.calls["count"], 1)

    def test_unparsable_and_schema_invalid_answers_are_retried_with_plain_feedback(self):
        (result, _), calls = self.run_attempts(
            ["I think it is fine.", '{"statement": 4, "notes": ""}', "", '{"statement": "S", "notes": "n"}'])
        self.assertEqual(result["statement"], "S")
        self.assertEqual(len(calls), 4)
        # A new conversation cannot see the previous answer, so the exact input carries it.
        self.assertIn("STRUCTURED OUTPUT RECOVERY RETRY", calls[1]["text"])
        self.assertIn("could not be accepted: the final answer was not one complete JSON object", calls[1]["text"])
        self.assertIn("I think it is fine.", calls[1]["text"])
        self.assertIn("the statement field must be a string", calls[2]["text"])
        self.assertIn("no final answer was returned", calls[3]["text"])
        self.assertTrue(all(call["text"].startswith("Review S.") for call in calls))
        notices = [event["text"] for event in self.notices()]
        self.assertTrue(notices[0].startswith("The model's answer could not be accepted (the final answer was not "
                                              "one complete JSON object) — retrying in 1 min (attempt 2)."))

    def test_a_conversation_with_work_is_continued_by_forks_of_its_base(self):
        sessions, forks = SessionRecorder(), []

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            forks.append((text, conversation.fork_from))
            number = len(forks)
            if number == 1:
                sessions.open(conversation, "base-1", output=True)  # Worked for 15 min, then timed out.
                raise runtime.StructuredAttemptTimeout("The review model did not respond within 900 seconds.", 900)
            if number == 2:
                sessions.open(conversation, "fork-2", output=False)  # At capacity before any output.
                raise runtime.Error(f"Codex review failed: {CAPACITY}")
            if number == 3:
                sessions.open(conversation, "fork-3", output=True)
                return '{"statement": "", "notes": "Could not decide."}'
            sessions.open(conversation, "fork-4", output=True)
            return '{"statement": "S", "notes": "fixed"}'

        def nonempty(report, raw):
            if not report["statement"].strip():
                raise runtime.InvalidAnswer("the statement field must not be empty")
            return report

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            result, _ = runtime.structured("Review S.", SCHEMA, "review", validate=nonempty)
        self.assertEqual(result["notes"], "fixed")
        self.assertEqual(forks[0], ("Review S.", None))
        self.assertEqual(forks[1], (runtime.STRUCTURED_RETRY_NOTE.format(cause="was stopped after 15 min"), "base-1"))
        # The fork that produced nothing was deleted; the next retry forks the same base
        # again with the same note (the model never saw the failed fork).
        self.assertEqual(forks[2], forks[1])
        note, base = forks[3]
        self.assertEqual(base, "fork-3")  # The rejected answer is work: its session became the base.
        self.assertTrue(note.startswith("STRUCTURED OUTPUT RECOVERY RETRY\nYour previous final answer could not be "
                                        "accepted: the statement field must not be empty.\n"))
        self.assertIn('"notes": "Could not decide."', note)  # The model's own explanation.
        self.assertNotIn("Review S.", note)  # The conversation already holds the task.
        # Deleted at once when useless, the rest newest first when the call ended.
        self.assertEqual(sessions.deleted, ["fork-2", "fork-4", "fork-3", "base-1"])
        requests = [event for event in self.events if event["kind"] == "request"]
        self.assertEqual([event.get("conversation") for event in requests], [None, "continued", "continued",
                                                                            "continued"])
        notice = self.notices()[-1]["text"]
        self.assertIn("The model said: notes: Could not decide.", notice)

    def test_failures_that_produce_nothing_never_add_a_second_note(self):
        """Twenty failures without output: every fork carries the base plus exactly one note."""
        histories, sessions = {}, SessionRecorder()

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            number = len(histories) + 1
            history = (list(histories[conversation.fork_from]) if conversation.fork_from else []) + [text]
            histories[f"s-{number}"] = history
            if number == 1:
                histories["s-1"].append("<work>")
                sessions.open(conversation, "s-1", output=True)
                raise runtime.Error("Codex review failed: stream disconnected before completion")
            sessions.open(conversation, f"s-{number}", output=False)
            if number < 22:
                raise runtime.Error(f"Codex review failed: {CAPACITY}")
            return VALID

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            runtime.structured("Review S.", SCHEMA, "review")
        note = runtime.STRUCTURED_RETRY_NOTE.format(cause="ended early because of a model-call failure")
        self.assertEqual(histories["s-1"], ["Review S.", "<work>"])  # The base never grows.
        for name in (f"s-{number}" for number in range(2, 23)):
            self.assertEqual(histories[name], ["Review S.", "<work>", note])
        self.assertEqual(sessions.deleted[:20], [f"s-{number}" for number in range(2, 22)])
        self.assertEqual(self.waits(), 21 * 60)

    def test_a_conversation_that_outgrew_the_context_starts_over_from_the_exact_input(self):
        sessions, forks = SessionRecorder(), []

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            forks.append((text, conversation.fork_from))
            if len(forks) == 1:
                sessions.open(conversation, "base-1", output=True)
                raise runtime.Error("Codex review failed: stream disconnected before completion")
            if len(forks) == 2:
                sessions.open(conversation, "fork-2", output=False)
                raise runtime.Error("Codex review failed: context_length_exceeded: input exceeds the context window")
            sessions.open(conversation, "fresh-3", output=True)
            return VALID

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            runtime.structured("Review S.", SCHEMA, "review")
        self.assertEqual(forks[2], ("Review S.", None))
        self.assertEqual(self.notices()[-1]["failure"], "the conversation is too long for the model")

    def test_consecutive_timeouts_double_the_next_attempt(self):
        timeouts, outcomes = [], [
            runtime.StructuredAttemptTimeout("slow", 900), runtime.StructuredAttemptTimeout("slow", 1800),
            runtime.StructuredAttemptTimeout("slow", 3600), runtime.Error(f"Codex critic failed: {CAPACITY}"),
            runtime.StructuredAttemptTimeout("slow", 900), VALID]

        def attempt(text, schema, stage, *args, timeout=None, **settings):
            timeouts.append(timeout)
            outcome = outcomes[len(timeouts) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            runtime.structured("Review S.", SCHEMA, "critic", timeout=900, options={"thinking_hours": 0})
        # Doubled after each consecutive timeout, back to 15 min after any other failure.
        self.assertEqual(timeouts, [900, 1800, 3600, 7200, 900, 1800])
        notices = [event["text"] for event in self.notices()]
        self.assertTrue(notices[0].startswith("Model call failed (no final answer within 15 min) — retrying in 1 min "
                                              "(attempt 2, the next attempt may run 30 min)."), notices[0])
        self.assertIn("(attempt 3, retrying for 1 min, 2 timeouts in a row; the next attempt may run 60 min)",
                      notices[1])
        self.assertIn("3 timeouts in a row; the next attempt may run 2 h", notices[2])
        self.assertNotIn("timeouts in a row", notices[3])
        # A finite budget still caps each attempt by the time left.
        timeouts.clear()
        outcomes[:] = [runtime.StructuredAttemptTimeout("slow", 900), VALID]
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            runtime.structured("Review S.", SCHEMA, "critic", timeout=900,
                               options={"thinking_hours": 1, "elapsed_seconds": 3600 - 1000})
        self.assertEqual(timeouts[0], 900)
        self.assertLessEqual(timeouts[1], 1000)

    def test_failures_a_retry_cannot_fix_end_the_call_at_once(self):
        def bug(report, raw):
            return report["statement"] + 1  # A harness bug (TypeError), not the model's.

        cases = (
            ("harness TypeError", {"validate": bug}, VALID, TypeError),
            ("harness KeyError", {}, KeyError("statement"), KeyError),
            ("harness AssertionError", {}, AssertionError("broken invariant"), AssertionError),
            ("configuration", {}, runtime.ConfigurationError(runtime.HISTORICAL_CODEX_ONLY), runtime.ConfigurationError),
            ("invalid bundle", {}, runtime.ConfigurationError("Invalid proof snapshot bundle."),
             runtime.ConfigurationError),
            ("safety refusal", {}, runtime.SafetyStop("Claude CLI enabled unexpected tools (RemoteTrigger)."),
             runtime.SafetyStop),
            ("workflow rule", {}, runtime.WorkflowDefinitionError("Cannot evaluate workflow expression"),
             runtime.WorkflowDefinitionError),
        )
        for name, kwargs, outcome, error in cases:
            with self.subTest(name), mock.patch.object(runtime, "run_structured_attempt",
                                                       side_effect=[outcome, VALID]) as attempt:
                with self.assertRaises(error):
                    runtime.structured("Review S.", SCHEMA, "review", **kwargs)
                self.assertEqual(attempt.call_count, 1)  # No second model call.
        self.assertEqual(self.clock.sleeps, [])
        # Transport failures and an unacceptable answer are retried.
        for outcome in (OSError(2, "No such file or directory: 'claude'"),
                        runtime.CLIUnavailable("Claude CLI is not installed or is not on PATH."),
                        runtime.ConversationUnavailable("gone"), runtime.InvalidAnswer("bad")):
            self.assertTrue(runtime.retryable_failure(outcome), outcome)
        for outcome in (runtime.FinalError("x"), runtime.WorkflowPaused(), runtime.RetryCancelled("x"),
                        TypeError(), KeyError(), AttributeError(), NameError(), RecursionError(), ValueError()):
            self.assertFalse(runtime.retryable_failure(outcome), outcome)

    def test_a_missing_api_key_is_reported_before_any_attempt(self):
        with mock.patch.dict(os.environ, {"TCS_PROVER_API_MODELS": str(self.root / "state" / "api-models.json")}):
            model = runtime.providers.save_api_model({
                "format": "openai", "baseUrl": "https://gateway.example.com/v1", "headerName": "Authorization",
                "model": "gateway-model", "apiKey": "sk-test-0123456789", "label": "Gateway"})["id"]
            with mock.patch.object(runtime.providers, "api_model_secret", return_value=""), \
                    mock.patch.object(runtime, "run_structured_attempt") as attempt:
                with self.assertRaisesRegex(runtime.ConfigurationError, "Add an API key for Gateway"):
                    runtime.structured("Review S.", SCHEMA, "review", model=model, effort="high")
        attempt.assert_not_called()

    def test_a_cli_that_is_missing_for_a_moment_is_waited_for(self):
        with mock.patch.object(runtime, "require_model_credentials",
                               side_effect=runtime.CLIUnavailable("Claude CLI is not installed or is not on PATH.")):
            (result, _), calls = self.run_attempts([
                runtime.CLIUnavailable("Claude CLI is not installed or is not on PATH."),
                runtime.CLIUnavailable("Claude CLI is not installed or is not on PATH."), VALID])
        self.assertEqual(len(calls), 3)
        self.assertEqual([event["failure"] for event in self.notices()], ["the CLI is not available"] * 2)

    def test_a_cap_is_only_for_direct_callers_that_ask(self):
        with mock.patch.object(runtime, "run_structured_attempt",
                               side_effect=runtime.Error(f"Codex review failed: {CAPACITY}")) as attempt:
            with self.assertRaisesRegex(runtime.Error, "at capacity"):
                runtime.structured("Review S.", SCHEMA, "review", attempts=3)
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(self.waits(), 120)


class AuthorStarted(BaseException):
    """A workflow test reached the author, which would start a real CLI session."""


class RequirementTests(RetryCase):
    def setUp(self):
        super().setUp()
        # A critic answer that wrongly reaches the author fails the test at once.
        for module in (runtime, claude_cli):
            patcher = mock.patch.object(module, "goal_session", side_effect=AuthorStarted("the author must not run"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_requirements_are_described_in_plain_words(self):
        critic = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
        writer = runtime.builtin_workflow("clean_up")["nodes"]["latex_editor"]
        self.assertEqual([runtime.describe_requirement(rule, critic["schema"]) for rule in critic["require"]],
                         ["the solution field must not be empty",
                          "the bugs field must be empty when verdict is pass and non-empty when verdict is reject"])
        self.assertEqual(runtime.describe_requirement(writer["require"][0], writer["schema"]),
                         "the latex field must not be empty")
        for rule, words in (("text(result.note)", "the note field must not be empty"),
                            ("len(result.items) > 0", "the items field must not be empty"),
                            ("result.verdict == 'pass'", "the verdict field must be pass"),
                            ("len(result.checks) == 3", "the answer must satisfy the rule len(result.checks) == 3")):
            self.assertEqual(runtime.describe_requirement(rule), words)

    def test_the_verdict_rule_names_the_answer_and_never_suggests_a_pass(self):
        critic = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
        rule, schema = critic["require"][1], critic["schema"]
        base = "the bugs field must be empty when verdict is pass and non-empty when verdict is reject"
        self.assertEqual(
            runtime.describe_requirement(rule, schema, {"verdict": "reject", "solution": "P", "bugs": " "}),
            base + " (your answer has verdict reject and an empty bugs field: keep verdict reject and fill in the "
                   "bugs field)")
        self.assertEqual(
            runtime.describe_requirement(rule, schema, {"verdict": "pass", "solution": "P", "bugs": "A gap."}),
            base + " (your answer has verdict pass and a non-empty bugs field: with verdict pass the bugs field "
                   "must be empty; if what you wrote in bugs still applies, the verdict should be reject)")

    def critic_node(self):
        critic = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
        return {"run": "structured", "prompt": "ask", "stage": "critic", "require": critic["require"],
                "schema": critic["schema"]}

    def test_a_reject_without_bugs_keeps_its_verdict_and_is_asked_for_the_bugs(self):
        sessions, texts = SessionRecorder(), []
        answers = [{"verdict": "reject", "solution": "P", "bugs": ""},
                   {"verdict": "reject", "solution": "P", "bugs": "Step 2 divides by zero."}]

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            texts.append(text)
            sessions.open(conversation, f"critic-{len(texts)}", output=True)
            return json.dumps(answers[len(texts) - 1])

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(self.critic_node(), {"ask": "Review P."}, {}, {}, 1)
        self.assertEqual(report["verdict"], "reject")
        self.assertIn("(your answer has verdict reject and an empty bugs field: keep verdict reject and fill in the "
                      "bugs field)", texts[1])
        self.assertNotIn("verdict should be pass", texts[1])

    def test_an_empty_critic_solution_is_retried_and_its_explanation_kept(self):
        """The reported failure: a critic hit capacity and returned an empty solution."""
        empty = {"verdict": "reject", "solution": "", "bugs": "Every audit subagent failed: model at capacity."}
        passed = {"verdict": "pass", "solution": "The complete proof.", "bugs": ""}
        texts, sessions = [], SessionRecorder()

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            texts.append(text)
            sessions.open(conversation, f"critic-{len(texts)}", output=True)
            return json.dumps(empty if len(texts) == 1 else passed)

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            state = runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"],
                                              {"statement": "Task", "solution": "The complete proof."},
                                              {"start_node": "critic"})
        self.assertEqual(state["output"], "The complete proof.")
        self.assertEqual(len(texts), 2)
        self.assertIn("could not be accepted: the solution field must not be empty.", texts[1])
        self.assertIn("Every audit subagent failed: model at capacity.", texts[1])
        notice = self.notices()[0]
        self.assertIn("(the solution field must not be empty)", notice["text"])
        self.assertIn("The model said: verdict: reject; bugs: Every audit subagent failed", notice["text"])
        diagnostic = next(event["text"] for event in self.events if event["kind"] == "diagnostic")
        self.assertIn("could not be accepted: the solution field must not be empty.", diagnostic)
        self.assertFalse([event for event in self.events if "Invalid model response" in str(event.get("text"))])
        # The rejected answer never replaced the saved candidate.
        results = [event for event in self.events if event["kind"] == "critic_result"]
        self.assertEqual([event["report"]["verdict"] for event in results], ["pass"])
        self.assertEqual(sessions.deleted, ["critic-2", "critic-1"])

    def test_a_reject_after_failed_subagents_never_reaches_the_author(self):
        """Only proof errors make a critic reject; failed subagent calls are retried instead."""
        proof = "The complete proof."
        reject = {"verdict": "reject", "solution": proof,
                  "bugs": "Review incomplete: every audit subagent failed (model at capacity)."}
        passed = {"verdict": "pass", "solution": proof, "bugs": ""}
        texts, sessions = [], SessionRecorder()

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            texts.append(text)
            sessions.open(conversation, f"critic-{len(texts)}", output=True)
            if len(texts) == 1:
                for agent in ("agent-1", "agent-2", "agent-3"):
                    conversation.subagent(agent, "failed", CAPACITY)
                return json.dumps(reject)
            conversation.subagent("agent-4", "completed")
            return json.dumps(passed)

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            state = runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"],
                                              {"statement": "Task", "solution": proof}, {"start_node": "critic"})
        # The author (which this test cannot run) was never asked to repair anything.
        self.assertEqual(state["output"], proof)
        self.assertEqual(len(texts), 2)
        self.assertEqual(texts[1], runtime.SUBAGENT_RETRY_NOTE.format(failed="3 subagent calls",
                                                                      reasons=" (model at capacity)"))
        results = [event for event in self.events if event["kind"] == "critic_result"]
        self.assertEqual([event["report"]["verdict"] for event in results], ["pass"])
        self.assertEqual(self.waits(), 60)

    def test_a_failed_require_rule_is_named_in_plain_words_with_the_rule(self):
        node = {"run": "structured", "prompt": "ask", "stage": "critic", "require": ["bool(strip(result.solution))"],
                "schema": {"type": "object", "properties": {"solution": {"type": "string"}},
                           "required": ["solution"], "additionalProperties": False}}
        with mock.patch.object(runtime, "run_structured_attempt",
                               side_effect=['{"solution": " "}', '{"solution": "Proof."}']) as attempt:
            report, _ = runtime._model_call(node, {"ask": "Question"}, {}, {}, 1)
        self.assertEqual(report, {"solution": "Proof."})
        self.assertIn("could not be accepted: the solution field must not be empty.",
                      attempt.call_args_list[1].args[0])
        diagnostic = next(event["text"] for event in self.events if event["kind"] == "diagnostic")
        self.assertIn("the solution field must not be empty. Rule: bool(strip(result.solution)).", diagnostic)
        notice = self.notices()[0]["text"]
        self.assertTrue(notice.startswith("The model's answer could not be accepted (the solution field must not "
                                          "be empty) — retrying in 1 min (attempt 2)."))
        self.assertNotIn("bool(strip", notice)

    def test_a_text_rule_on_an_empty_field_is_retried(self):
        node = {"run": "structured", "prompt": "ask", "stage": "review", "require": ["text(result.notes)"],
                "schema": SCHEMA}
        with mock.patch.object(runtime, "run_structured_attempt",
                               side_effect=['{"statement": "S", "notes": " "}', VALID]) as attempt:
            report, _ = runtime._model_call(node, {"ask": "Question"}, {}, {}, 1)
        self.assertEqual(report["notes"], "ok")
        self.assertIn("the notes field must not be empty", attempt.call_args_list[1].args[0])

    def test_rules_that_cannot_be_evaluated_end_the_run_without_a_second_call(self):
        cases = (
            ("require names a field the schema lacks", {"require": ["len(result.items) > 0"]}, "Missing expression key"),
            ("the node's own error text", {"require": ["len(result.items) > 0"], "error": "Broken rule."},
             "Broken rule."),
            ("a normalize rule that fails", {"normalize": {"notes": "strip(result.missing)"}}, "Missing expression key"),
            ("normalize breaks the schema", {"normalize": {"notes": "len(result.statement)"}}, "notes"),
        )
        for name, rules, message in cases:
            node = {"run": "structured", "prompt": "ask", "stage": "review", "schema": SCHEMA, **rules}
            with self.subTest(name), mock.patch.object(runtime, "run_structured_attempt",
                                                       side_effect=[VALID, VALID]) as attempt:
                with self.assertRaisesRegex(runtime.WorkflowDefinitionError, message):
                    runtime._model_call(node, {"ask": "Question"}, {}, {}, 1)
                self.assertEqual(attempt.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_the_verdict_rule_and_a_file_pointer_are_retried(self):
        answers = [{"verdict": "pass", "solution": "Proof.", "bugs": "A remaining gap."},
                   {"verdict": "pass", "solution": "PROOF_FILE: proof.md", "bugs": ""},
                   {"verdict": "pass", "solution": "Proof.", "bugs": ""}]
        texts, sessions = [], SessionRecorder()

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            texts.append(text)
            sessions.open(conversation, f"critic-{len(texts)}", output=True)
            return json.dumps(answers[len(texts) - 1])

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            state = runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"],
                                              {"statement": "Task", "solution": "Proof."}, {"start_node": "critic"})
        self.assertEqual(state["output"], "Proof.")
        self.assertIn("the bugs field must be empty when verdict is pass and non-empty when verdict is reject "
                      "(your answer has verdict pass and a non-empty bugs field", texts[1])
        self.assertIn("the solution field must contain the complete proof text, not a file pointer", texts[2])

    def test_an_empty_latex_document_is_retried(self):
        latex = r"\documentclass{article}\begin{document}x\end{document}"
        with mock.patch.object(runtime, "run_structured_attempt",
                               side_effect=['{"latex": "  "}', json.dumps({"latex": latex})]) as attempt, \
                mock.patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}):
            previous = Path.cwd()
            os.chdir(self.root)
            self.addCleanup(os.chdir, previous)
            state = runtime.execute_workflows([ROOT / "workflows/clean_up.yaml"], {"solution": "Proof."})
        self.assertEqual(state["output"], latex)
        self.assertEqual(attempt.call_count, 2)
        self.assertIn("the latex field must not be empty", attempt.call_args_list[1].args[0])

    def test_an_empty_reviewer_statement_is_retried_within_a_timeout_and_the_job_budget(self):
        from ui import review
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=[
                '{"statement": "", "notes": "The draft is ambiguous."}', '{"statement": "S.", "notes": "Fixed."}']) \
                as attempt:
            self.assertEqual(review.review("Draft", thinking_hours=2), {"statement": "S.", "notes": "Fixed."})
        self.assertEqual(self.waits(), 60)
        # Like a workflow node: each attempt has a timeout, and the job's limit bounds the review.
        self.assertEqual([call.kwargs["timeout"] for call in attempt.call_args_list],
                         [runtime.STRUCTURED_TIMEOUT_SECONDS] * 2)
        with mock.patch.object(runtime, "structured", return_value=({"statement": "S", "notes": ""}, "{}")) as call:
            review.review("Draft", thinking_hours=0)
        self.assertEqual(call.call_args.kwargs["options"], {"thinking_hours": 0})
        self.assertEqual(call.call_args.kwargs["timeout"], runtime.STRUCTURED_TIMEOUT_SECONDS)


class ParallelRetryTests(RetryCase):
    def test_independent_requests_retry_alone_and_a_stop_cancels_waiting_siblings(self):
        config = {"run": "structured", "items": ["a", "b"], "prompt": "check", "stage": "critic",
                  "schema": SCHEMA, "inputs": {"item": "item"}}
        calls = {"a": 0, "b": 0}

        def attempt(text, schema, stage, *args, **settings):
            item = text[-1]
            calls[item] += 1
            if item == "a" and calls[item] < 4:
                raise runtime.Error(f"Codex critic failed: {CAPACITY}")
            return '{"statement": "S", "notes": "%s"}' % item

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            reports = runtime._parallel_requests(config, {"check": "Check {item}"}, {}, {}, 1)
        self.assertEqual([report["notes"] for report in reports], ["a", "b"])
        self.assertEqual(calls, {"a": 4, "b": 1})
        labels = {event["label"] for event in self.statuses("Independent audit 1 · " + runtime.RETRY_LABEL)}
        self.assertEqual(labels, {"Independent audit 1 · " + runtime.RETRY_LABEL})

        # A request that ends the step (here a pause) cancels the others' waits at once.
        cancel_seen, waiting = threading.Event(), threading.Event()
        attempts = {"a": 0}

        def stop(text, schema, stage, *args, **settings):
            if text.endswith("b"):
                self.assertTrue(waiting.wait(5))  # Request a is waiting to retry.
                raise runtime.WorkflowPaused("Paused before the next workflow step.")
            attempts["a"] += 1
            raise runtime.Error(f"Codex critic failed: {CAPACITY}")

        def sleep(seconds, cancel=None):
            waiting.set()
            if cancel is not None and cancel.wait(5):
                cancel_seen.set()

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=stop), \
                mock.patch.object(runtime, "_retry_sleep", side_effect=guarded(sleep, limit=20)):
            with self.assertRaises(runtime.WorkflowPaused):
                runtime._parallel_requests(config, {"check": "Check {item}"}, {}, {}, 1)
        self.assertTrue(cancel_seen.is_set())
        self.assertEqual(attempts["a"], 1)


# The stand-in Codex CLI: `codex exec` and `codex exec fork <id> -` answer as
# scripted and keep each persisted session's history (user inputs and model
# items) like Codex; `codex delete --force <id>` refuses a session that still
# has forks, as Codex 0.160 does.
CODEX_FAKE = r'''
import json, os, sys, time, uuid
STATE = os.environ["TCS_FAKE_CODEX_STATE"]
argv = sys.argv[1:]


def load(name, default):
    path = os.path.join(STATE, name)
    return json.load(open(path)) if os.path.exists(path) else default


def save(name, value):
    json.dump(value, open(os.path.join(STATE, name), "w"))


def log(record):
    with open(os.path.join(STATE, "log.jsonl"), "a") as stream:
        stream.write(json.dumps(record) + "\n")


sessions = load("sessions.json", {})
if "exec" not in argv:
    target = argv[-1]
    removable = target in sessions and not any(entry.get("parent") == target for entry in sessions.values())
    log({"delete": target, "removed": removable, "history": sessions.get(target, {}).get("history")})
    if removable:
        del sessions[target]
        save("sessions.json", sessions)
    sys.exit(0 if removable else 1)
stdin = sys.stdin.read()
tail = argv[argv.index("exec") + 1:]
fork = tail[:1] == ["fork"]
source = tail[-2] if fork else None
workspace = argv[argv.index("-C") + 1]
log({"argv": argv, "stdin": stdin, "files": sorted(os.listdir(workspace)) if os.path.isdir(workspace) else None})
if fork and source not in sessions:
    sys.stderr.write("Error: thread/fork: thread/fork failed: no rollout found for thread id %s (code -32600)\n" % source)
    sys.exit(1)
script = load("script.json", [])
position = load("next.json", 0)
save("next.json", position + 1)
behaviour = script[min(position, len(script) - 1)]
thread = behaviour.get("thread_id") or str(uuid.uuid4())
history = (list(sessions[source]["history"]) if fork else []) + [{"user": stdin}]


def keep():
    if "--ephemeral" not in argv:
        sessions[thread] = {"history": history, "parent": source}
        save("sessions.json", sessions)


def event(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


keep()
event({"type": "thread.started", "thread_id": thread})
event({"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Model metadata not found."}})
event({"type": "turn.started"})
if "work" in behaviour:
    event({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": behaviour["work"]}})
    history.append({"model": behaviour["work"]})
    keep()
for index, agent in enumerate(behaviour.get("subagents", [])):
    # A wait call reports the subagent's state, as Codex 0.160 does.
    event({"type": "item.completed", "item": {
        "id": "item_c%d" % index, "type": "collab_tool_call", "tool": "wait", "sender_thread_id": thread,
        "receiver_thread_ids": [agent["thread"]], "prompt": None, "status": "completed",
        "agents_states": {agent["thread"]: {"status": agent["status"], "message": agent.get("message")}}}})
    history.append({"subagent": agent["thread"], "status": agent["status"]})
    keep()
if "hang" in behaviour:
    time.sleep(behaviour["hang"])
if "fail" in behaviour:
    event({"type": "error", "message": behaviour["fail"]})
    event({"type": "turn.failed", "error": {"message": behaviour["fail"]}})
    sys.exit(1)
event({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": behaviour["answer"]}})
history.append({"model": behaviour["answer"]})
keep()
open(argv[argv.index("-o") + 1], "w").write(behaviour["answer"])
event({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 2}})
'''


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class CodexStructuredTests(RetryCase):
    """Sessions persist; a retry forks the base (`codex exec fork <id> -`); every session is deleted afterwards."""

    def setUp(self):
        super().setUp()
        self.state = self.root / "codex-state"
        self.state.mkdir()
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n{CODEX_FAKE}")
        fake.chmod(0o700)
        environment = mock.patch.dict(os.environ, {"TCS_FAKE_CODEX_STATE": str(self.state),
                                                    "CODEX_HOME": str(self.root / "codex-home"),
                                                    "TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        located = mock.patch.object(runtime, "codex", return_value=str(fake))
        located.start()
        self.addCleanup(located.stop)
        self.fake = str(fake)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def script(self, *behaviours):
        (self.state / "script.json").write_text(json.dumps(list(behaviours)))

    def log(self):
        path = self.state / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def execs(self):
        return [entry for entry in self.log() if "argv" in entry]

    def deletes(self):
        return [entry for entry in self.log() if "delete" in entry]

    def sessions(self):
        path = self.state / "sessions.json"
        return json.loads(path.read_text()) if path.exists() else {}

    @staticmethod
    def tail(entry):
        argv = entry["argv"]
        return argv[argv.index("exec") + 1:]

    def test_failures_before_any_output_rerun_the_exact_input_and_leave_no_session(self):
        self.script(*({"fail": message} for message in FAILURES), {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol", effort="high")
        self.assertEqual(result, {"statement": "S", "notes": "ok"})
        execs = self.execs()
        self.assertEqual(len(execs), len(FAILURES) + 1)
        for entry in execs:
            self.assertEqual(entry["stdin"], "Review S.")  # Nothing was produced, so nothing is lost.
            self.assertNotIn("fork", self.tail(entry))
            self.assertNotIn("--ephemeral", entry["argv"])
            argv = entry["argv"]
            self.assertIn("memories.generate_memories=false", argv[:argv.index("exec")])
        # Each useless session was deleted right after its failure, the answered one after the call.
        log = self.log()
        self.assertEqual(["delete" if "delete" in entry else "exec" for entry in log],
                         ["exec", "delete"] * (len(FAILURES) + 1))
        self.assertTrue(all(entry["removed"] for entry in self.deletes()))
        self.assertEqual(self.sessions(), {})
        self.assertEqual(self.waits(), 60 * len(FAILURES))

    def test_work_then_failures_fork_the_same_base_and_carry_exactly_one_note(self):
        self.script({"work": "Audit 1 is done.", "fail": FAILURES[5]}, *({"fail": CAPACITY} for _ in range(8)),
                    {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol", effort="high")
        self.assertEqual(result["notes"], "ok")
        execs = self.execs()
        self.assertEqual(len(execs), 10)
        base = self.tail(execs[1])[-2]
        note = runtime.STRUCTURED_RETRY_NOTE.format(cause="ended early because of a model-call failure")
        first = execs[0]["argv"]
        for entry in execs[1:]:
            argv = entry["argv"]
            self.assertEqual(self.tail(entry)[:1], ["fork"])
            self.assertEqual(self.tail(entry)[-2:], [base, "-"])
            self.assertEqual(entry["stdin"], note)
            # Every root flag (model, effort, features, workspace, sandbox, config) is passed again.
            self.assertEqual(argv[:argv.index("exec")], first[:first.index("exec")])
            for flag in ("--json", "--skip-git-repo-check", "--ignore-user-config", "--output-schema", "-o"):
                self.assertIn(flag, self.tail(entry))
            self.assertNotIn("--ephemeral", argv)
        histories = {entry["delete"]: entry["history"] for entry in self.deletes() if entry["history"]}
        # The base never grows; every fork holds the base plus one note, however many failed.
        self.assertEqual(histories[base], [{"user": "Review S."}, {"model": "Audit 1 is done."}])
        forks = [history for name, history in histories.items() if name != base]
        self.assertEqual(len(forks), 9)
        for history in forks:
            self.assertEqual(history[:3], [{"user": "Review S."}, {"model": "Audit 1 is done."}, {"user": note}])
            self.assertEqual(sum(1 for item in history if item.get("user") == note), 1)
        # Forks went first; the base could go only after them.
        self.assertEqual(self.deletes()[-1]["delete"], base)
        self.assertTrue(all(entry["removed"] for entry in self.deletes()))
        self.assertEqual(self.sessions(), {})

    def test_a_base_codex_no_longer_has_is_rerun_with_the_exact_input(self):
        self.script({"work": "w", "fail": CAPACITY}, {"answer": '{"statement": "S", "notes": "fresh"}'})
        original = runtime._run_structured_process

        def forget_sessions(*args, **kwargs):
            # Codex lost the saved session between the attempts.
            if kwargs["conversation"].fork_from:
                (self.state / "sessions.json").write_text("{}")
            return original(*args, **kwargs)

        with mock.patch.object(runtime, "_run_structured_process", side_effect=forget_sessions):
            result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol")
        self.assertEqual(result["notes"], "fresh")
        execs = self.execs()
        self.assertEqual(len(execs), 3)
        self.assertEqual(self.tail(execs[1])[:1], ["fork"])
        self.assertNotIn("fork", self.tail(execs[2]))
        self.assertEqual(execs[2]["stdin"], "Review S.")  # The exact saved input, nothing lost.
        self.assertEqual(self.waits(), 60)  # The fallback runs at once.
        self.assertTrue(any("no longer available" in event["text"] for event in self.statuses()))

    def test_a_proof_review_keeps_its_snapshot_for_the_forked_session(self):
        self.script({"work": "w", "fail": CAPACITY}, {"answer": VALID})
        files = (("proof.md", "Proof"), ("statement.md", "Task"), ("manifest.json", "{}"))
        runtime.structured("Review.", SCHEMA, "critic", model="gpt-6.1-sol", input_files=files)
        execs = self.execs()
        workspaces = [entry["argv"][entry["argv"].index("-C") + 1] for entry in execs]
        self.addCleanup(shutil.rmtree, workspaces[0], True)
        self.assertEqual(len(set(workspaces)), 1)
        self.assertEqual([entry["files"] for entry in execs], [["manifest.json", "proof.md", "statement.md"]] * 2)
        self.assertEqual((Path(workspaces[0]) / "proof.md").read_text(), "Proof")
        self.assertEqual(self.tail(execs[1])[:1], ["fork"])

    def test_a_vanished_snapshot_is_written_again_for_a_new_conversation(self):
        self.script({"work": "w", "fail": CAPACITY}, {"answer": VALID})
        files = (("proof.md", "Proof"), ("statement.md", "Task"), ("manifest.json", "{}"))
        original, removed = runtime._run_structured_process, []

        def clean_temp(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                if not removed:
                    # A temp-folder cleaner removed the snapshot during the wait.
                    removed.append(kwargs["workspace"])
                    shutil.rmtree(kwargs["workspace"])

        with mock.patch.object(runtime, "_run_structured_process", side_effect=clean_temp):
            runtime.structured("Review.", SCHEMA, "critic", model="gpt-6.1-sol", input_files=files)
        execs = self.execs()
        self.assertEqual(len(execs), 2)
        second = execs[1]["argv"][execs[1]["argv"].index("-C") + 1]
        self.addCleanup(shutil.rmtree, second, True)
        self.assertNotEqual(second, str(removed[0]))
        self.assertEqual(execs[1]["files"], ["manifest.json", "proof.md", "statement.md"])
        # Files the saved conversation read are gone: it starts over from the exact input.
        self.assertNotIn("fork", self.tail(execs[1]))
        self.assertTrue(execs[1]["stdin"].startswith("Review.\n\nREAD-ONLY PROOF FILES:"))
        self.assertTrue(any("disappeared or changed" in str(event.get("text")) for event in self.events))

    def test_a_partly_written_snapshot_is_removed_and_written_again(self):
        self.script({"answer": VALID})
        files = (("proof.md", "Proof"), ("statement.md", "Task"), ("manifest.json", "{}"))
        real_open, failed = os.open, []

        def full_disk(path, flags, *args, **kwargs):
            if str(path).endswith("statement.md") and not failed:
                failed.append(Path(path).parent)
                raise OSError(28, "No space left on device")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(runtime.os, "open", side_effect=full_disk):
            runtime.structured("Review.", SCHEMA, "critic", model="gpt-6.1-sol", input_files=files)
        execs = self.execs()
        self.addCleanup(shutil.rmtree, execs[0]["argv"][execs[0]["argv"].index("-C") + 1], True)
        self.assertFalse(failed[0].exists())  # The partial snapshot was removed.
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["files"], ["manifest.json", "proof.md", "statement.md"])
        self.assertEqual(self.waits(), 60)

    def test_a_session_id_that_is_not_a_uuid_is_never_forked(self):
        self.script({"thread_id": "--last", "work": "w", "fail": CAPACITY}, {"answer": VALID})
        runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol")
        execs = self.execs()
        self.assertNotIn("fork", self.tail(execs[1]))
        self.assertNotIn("--last", execs[1]["argv"])
        self.assertEqual(execs[1]["stdin"], "Review S.")
        self.assertNotIn("--last", [entry["delete"] for entry in self.deletes()])

    def test_web_cutoff_calls_stay_ephemeral_and_rerun_the_exact_input(self):
        """A historical (web-cutoff) policy never persists or continues a session (end to end)."""
        self.script({"work": "w", "fail": CAPACITY}, {"answer": VALID})
        workspace = self.root / "historical-workspace"
        workspace.mkdir()
        policy = {"_path": str(self.root / "policy.json")}
        patches = (
            mock.patch.object(runtime, "retrieval_policy", return_value=policy),
            mock.patch.object(runtime, "retrieval_environment", side_effect=lambda *args, **kwargs: dict(os.environ)),
            mock.patch.object(runtime.retrieval_runtime, "codex_executable", return_value=self.fake),
            mock.patch.object(runtime.retrieval_runtime, "role_workspace", return_value=workspace),
            mock.patch.object(runtime.retrieval_runtime, "native_search_arguments", return_value=[]),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        result, _ = runtime.structured("Review S.", SCHEMA, "review", model="gpt-6.1-sol")
        self.assertEqual(result["notes"], "ok")
        execs = self.execs()
        self.assertEqual(len(execs), 2)
        for entry in execs:
            self.assertIn("--ephemeral", entry["argv"])
            self.assertIn("--strict-config", entry["argv"])
            self.assertNotIn("fork", self.tail(entry))
            self.assertNotIn("resume", self.tail(entry))
            self.assertNotIn("memories.generate_memories=false", entry["argv"])
            self.assertEqual(entry["stdin"], "Review S.")
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.sessions(), {})

    def test_a_timed_out_attempt_with_work_continues_with_a_longer_timeout(self):
        self.script({"work": "Halfway.", "hang": 30}, {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", timeout=1.5)
        self.assertEqual(result["notes"], "ok")
        execs = self.execs()
        self.assertEqual(self.tail(execs[1])[:1], ["fork"])
        self.assertEqual(execs[1]["stdin"], runtime.STRUCTURED_RETRY_NOTE.format(cause="was stopped after 1.5 s"))
        notice = self.notices()[0]["text"]
        self.assertTrue(notice.startswith("Model call failed (no final answer within 1.5 s) — retrying in 1 min "
                                          "(attempt 2, the next attempt may run 3 s)."), notice)

    def test_an_answer_after_failed_subagents_is_set_aside_and_they_run_again(self):
        """The reported bug: subagents at capacity made the critic reject instead of retrying them."""
        reject = json.dumps({"statement": "S", "notes": "Review incomplete: an audit subagent failed."})
        self.script({"subagents": [{"thread": "agent-1", "status": "completed", "message": "No bugs."},
                                   {"thread": "agent-2", "status": "errored", "message": CAPACITY}],
                     "answer": reject},
                    {"subagents": [{"thread": "agent-3", "status": "completed", "message": "No bugs."}],
                     "answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")  # The reject was never returned.
        execs = self.execs()
        self.assertEqual(len(execs), 2)
        note = runtime.SUBAGENT_RETRY_NOTE.format(failed="1 subagent call", reasons=" (model at capacity)")
        self.assertEqual(self.tail(execs[1])[:1], ["fork"])  # It keeps the report of agent-1.
        self.assertEqual(execs[1]["stdin"], note)
        self.assertTrue(note.startswith("RETRY AFTER FAILED SUBAGENT CALLS\n1 subagent call in your previous attempt "
                                        "failed before finishing (model at capacity). These are model-call failures, "
                                        "not findings about the task"))
        self.assertEqual(self.waits(), 60)
        notice = self.notices()[0]
        self.assertEqual(notice["failureKind"], "subagent")
        self.assertTrue(notice["text"].startswith(
            "The answer was set aside because 1 subagent call failed (model at capacity) — retrying in 1 min "
            "(attempt 2)."), notice["text"])
        self.assertIn(f"Provider message: {CAPACITY}", notice["text"])
        diagnostic = next(event for event in self.events if event["kind"] == "diagnostic"
                          and "set aside" in str(event.get("text")))
        self.assertIn("Review incomplete", diagnostic["rawResponse"])
        self.assertIn(f"Subagent errors: {CAPACITY}", diagnostic["text"])
        self.assertEqual(self.sessions(), {})

    def test_a_pass_after_failed_subagents_is_not_accepted_either(self):
        passed = json.dumps({"statement": "S", "notes": "Looks right to me."})
        self.script({"subagents": [{"thread": "agent-1", "status": "errored", "message": FAILURES[2]},
                                   {"thread": "agent-2", "status": "errored", "message": FAILURES[5]}],
                     "answer": passed},
                    {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(self.execs()[1]["stdin"], runtime.SUBAGENT_RETRY_NOTE.format(
            failed="2 subagent calls", reasons=" (rate limited; connection lost)"))

    def test_a_subagent_that_failed_and_then_finished_counts_as_finished(self):
        self.script({"subagents": [{"thread": "agent-1", "status": "errored", "message": CAPACITY},
                                   {"thread": "agent-1", "status": "completed", "message": "No bugs."}],
                     "answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(len(self.execs()), 1)
        self.assertEqual(self.waits(), 0)

    def test_finished_and_closed_subagents_never_trigger_a_retry(self):
        self.script({"subagents": [{"thread": "agent-1", "status": "completed", "message": "Bug in step 2."},
                                   {"thread": "agent-1", "status": "shutdown"},
                                   {"thread": "agent-2", "status": "not_found"}],
                     "answer": VALID})
        runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", features=("multi_agent",))
        self.assertEqual(len(self.execs()), 1)

    def test_a_timed_out_attempt_with_failed_subagents_asks_to_run_them_again(self):
        self.script({"subagents": [{"thread": "agent-1", "status": "errored", "message": CAPACITY}], "hang": 30},
                    {"fail": CAPACITY},  # The fork fails before any output and is deleted.
                    {"answer": VALID})
        result, _ = runtime.structured("Review S.", SCHEMA, "critic", model="gpt-6.1-sol", timeout=1.5,
                                       features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        execs = self.execs()
        note = (runtime.STRUCTURED_RETRY_NOTE.format(cause="was stopped after 1.5 s") + "\n"
                + runtime.SUBAGENT_RERUN_NOTE.format(failed="1 subagent call", reasons=" (model at capacity)"))
        # Both forks of the same base carry the same note.
        self.assertEqual([entry["stdin"] for entry in execs[1:]], [note, note])
        self.assertEqual(self.tail(execs[1])[-2], self.tail(execs[2])[-2])


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class ClaudeStructuredTests(claude_test.FakeClaudeCase):
    """Claude persists sessions (--session-id); a retry forks the base (--resume <id> --fork-session)."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        patch_clock(self, self.clock)
        real = self.real_deleter = claude_cli.session_deleter
        self.transcripts = {}

        def recording(config_dir):
            delete = real(config_dir)

            def remove(session_id):
                for path in (Path(config_dir) / "projects").glob(f"*/{session_id}.jsonl"):
                    self.transcripts[session_id] = [json.loads(line).get("text") for line in
                                                    path.read_text().splitlines()
                                                    if json.loads(line).get("type") == "user"]
                return delete(session_id)

            return remove

        patcher = mock.patch.object(claude_cli, "session_deleter", side_effect=recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def overloaded(status=529, message="API Error: 529 Overloaded"):
        return ["init", {"result": {"is_error": True, "result": message, "api_error_status": status}}, {"exit": 1}]

    @staticmethod
    def worked_then_failed():
        return ["init", {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1",
                                  "message": {"content": [{"type": "text", "text": "Checked half."}]}}},
                {"result": {"is_error": True, "result": "API Error: 500 Internal server error",
                            "api_error_status": 500}}, {"exit": 1}]

    def notices(self):
        return [event for event in self.events if event.get("label") == runtime.RETRY_LABEL and "retryAt" in event]

    def saved(self):
        return sorted(path.name for path in (self.root / "claude-config").rglob("*.jsonl"))

    def test_failures_before_any_output_rerun_the_exact_input_and_leave_no_session(self):
        self.script([self.overloaded(), self.overloaded(429, "API Error: 429 rate_limit_error"),
                     self.overloaded(500, "API Error: 500 Internal server error"),
                     ["init", {"exit": 1}],  # The CLI dropped the stream and exited.
                     ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
        self.assertEqual(result["notes"], "ok")
        starts = [row["argv"] for row in self.records("start")]
        self.assertEqual(len(starts), 5)
        self.assertEqual([argv[0] for argv in starts], ["--session-id"] * 5)
        self.assertEqual(len({argv[1] for argv in starts}), 5)
        self.assertEqual({argv[2:] == starts[0][2:] for argv in starts}, {True})  # Every flag again.
        self.assertEqual([row["text"] for row in self.records("stdin")], ["Review."] * 5)
        self.assertEqual([event["failure"] for event in self.notices()[:3]],
                         ["model at capacity", "rate limited", "provider server error"])
        self.assertEqual(sum(self.clock.sleeps), 4 * 60)
        self.assertEqual(self.saved(), [])  # Every persisted session was deleted.
        self.assertPrivateFilesRemoved()

    def test_a_failure_after_work_forks_the_same_base_and_carries_exactly_one_note(self):
        self.script([self.worked_then_failed(), self.overloaded(), self.overloaded(),
                     ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
        self.assertEqual(result["notes"], "ok")
        starts = [row["argv"] for row in self.records("start")]
        base = starts[0][1]
        note = runtime.STRUCTURED_RETRY_NOTE.format(cause="ended early because of a model-call failure")
        for argv in starts[1:]:
            self.assertEqual(argv[:4], ["--resume", base, "--fork-session", "--session-id"])
            self.assertTrue(claude_cli._session_id(argv[4]))
            self.assertEqual(argv[5:], starts[0][2:])  # Every flag and settings choice again.
        self.assertEqual([row["text"] for row in self.records("stdin")], ["Review.", note, note, note])
        # The base holds only the task; each fork the base plus one note.
        self.assertEqual(self.transcripts[base], ["Review."])
        for argv in starts[1:]:
            self.assertEqual(self.transcripts[argv[4]], ["Review.", note])
        self.assertEqual(self.saved(), [])

    def test_an_answer_without_structured_output_forks_with_feedback(self):
        self.script([["init", {"result": {"result": "I think the statement is fine."}}],
                     ["init", {"structured": {"statement": "S", "notes": "retried"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "review", model="claude-sonnet-5-5")
        self.assertEqual(result["notes"], "retried")
        starts = [row["argv"] for row in self.records("start")]
        self.assertEqual(starts[1][:3], ["--resume", starts[0][1], "--fork-session"])
        note = self.records("stdin")[1]["text"]
        self.assertIn("could not be accepted: the final answer was not one complete JSON object.", note)
        self.assertIn("I think the statement is fine.", note)

    def test_a_base_claude_no_longer_has_is_rerun_with_the_exact_input(self):
        self.script([self.worked_then_failed(), ["init", {"structured": {"statement": "S", "notes": "fresh"}}]],
                    all_resumes_gone=True)
        result, _ = runtime.structured("Review.", SCHEMA, "review", model="claude-haiku-5-5")
        self.assertEqual(result["notes"], "fresh")
        starts = [row["argv"] for row in self.records("start")]
        self.assertEqual([argv[0] for argv in starts], ["--session-id", "--resume", "--session-id"])
        self.assertNotEqual(starts[2][1], starts[0][1])
        self.assertEqual(self.records("stdin")[-1]["text"], "Review.")
        self.assertEqual(sum(self.clock.sleeps), 60)

    def test_a_timed_out_attempt_names_its_duration(self):
        self.script([["init", {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1",
                                        "message": {"content": [{"type": "text", "text": "Halfway."}]}}},
                      {"hang": 30}],
                     ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", timeout=1.5)
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(self.records("stdin")[1]["text"],
                         runtime.STRUCTURED_RETRY_NOTE.format(cause="was stopped after 1.5 s"))
        self.assertTrue(self.notices()[0]["text"].startswith(
            "Model call failed (no final answer within 1.5 s) — retrying in 1 min (attempt 2, the next attempt "
            "may run 3 s)."))

    def test_the_sessions_of_an_api_model_are_deleted_from_its_own_home(self):
        self.script([self.worked_then_failed(), ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        runtime.structured("Review.", SCHEMA, "review", model=self.api_model())
        starts = self.records("start")
        self.assertNotIn("CLAUDE_CONFIG_DIR", starts[0]["env"])  # API models never see the login folder.
        home = self.root / "home" / ".claude" / "projects"
        self.assertEqual(sorted(self.transcripts), sorted([starts[0]["argv"][1], starts[1]["argv"][4]]))
        self.assertEqual(list(home.rglob("*.jsonl")), [])
        self.assertEqual(list(home.iterdir()), [])  # The emptied project folder went too.
        self.assertNoSecretLeaks()

    def test_deleting_a_session_touches_nothing_else(self):
        projects = self.root / "claude-config" / "projects"
        mine, other = "0b1e5c43-7a2e-4c56-9d1f-2b3c4d5e6f70", "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
        for folder, name in (("-run", f"{mine}.jsonl"), ("-run", f"{other}.jsonl"), ("-solo", f"{mine}.jsonl")):
            (projects / folder).mkdir(parents=True, exist_ok=True)
            (projects / folder / name).write_text("{}\n")
        (projects / "-run" / mine / "subagents").mkdir(parents=True)
        delete = self.real_deleter(self.root / "claude-config")
        self.assertTrue(delete(mine))
        self.assertEqual(sorted(str(path.relative_to(projects)) for path in projects.rglob("*")),
                         ["-run", f"-run/{other}.jsonl"])
        self.assertFalse(delete("../../etc"))  # Only session ids (UUIDs) are ever deleted.

    OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'

    @staticmethod
    def critic_turn(results, answer, failed=None):
        """A coordinator that launches one Agent call per tool result body in `results`, then answers."""
        calls = [{"type": "tool_use", "id": f"task{index}", "name": "Agent",
                  "input": {"description": f"Audit {index}", "prompt": "Audit proof.md"}}
                 for index in range(1, len(results) + 1)]
        stats = {"spawned": len(results), "completed": len(results) - (failed or 0), "failed": failed,
                 "killed": {"parent": 0, "user": 0, "system": 0},
                 "refused": {"depth_limit": 0, "concurrency_limit": 0, "budget": 0}}
        return ["init",
                {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {"content": [
                    {"type": "text", "text": "Launching the audits."}, *calls]}}},
                {"emit": {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"task{index}", **result}
                    for index, result in enumerate(results, 1)]}}},
                {"structured": answer, **({"result_fields": {"subagent_stats": stats}} if failed is not None else {})}]

    def test_a_failed_subagent_sets_the_answer_aside_and_runs_again_in_the_same_conversation(self):
        reject = {"statement": "S", "notes": "Every audit failed with API Error 529."}
        self.script([self.critic_turn([{"content": "No bugs."}, {"is_error": True, "content": self.OVERLOADED}],
                                      reject, failed=1),
                     ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        starts = [row["argv"] for row in self.records("start")]
        self.assertEqual(starts[1][:3], ["--resume", starts[0][1], "--fork-session"])
        self.assertEqual(self.records("stdin")[1]["text"], runtime.SUBAGENT_RETRY_NOTE.format(
            failed="1 subagent call", reasons=" (model at capacity)"))
        self.assertEqual(sum(self.clock.sleeps), 60)
        self.assertEqual(self.notices()[0]["failureKind"], "subagent")
        self.assertEqual(self.saved(), [])

    def test_a_subagent_cut_off_by_an_api_error_counts_as_failed(self):
        partial = (f"API Error: 529 Overloaded\n{claude_cli.PARTIAL_SUBAGENT_NOTE} The agent did NOT finish its "
                   "task — treat these results as incomplete.\n\nChecked Lemma 1 only.")
        self.script([self.critic_turn([{"content": [{"type": "text", "text": partial}]}],
                                      {"statement": "S", "notes": "Partial review."}, failed=0),
                     ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(self.records("stdin")[1]["text"], runtime.SUBAGENT_RETRY_NOTE.format(
            failed="1 subagent call", reasons=" (model at capacity)"))

    def test_calls_that_ran_no_subagent_are_not_failures(self):
        refusal = ("<tool_use_error>Concurrent subagent limit reached. You can run 4 subagents at once. Do not "
                   "retry.</tool_use_error>")
        self.script([self.critic_turn([{"is_error": True, "content": refusal}, {"content": "No bugs."}],
                                      {"statement": "S", "notes": "ok"}, failed=0)])
        result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(len(self.records("start")), 1)

    def test_the_failed_count_of_the_result_decides_which_error_results_were_failures(self):
        # Two error results, one counted failure: the API error is the one named.
        self.script([self.critic_turn([{"is_error": True, "content": "Unexpected tool input."},
                                       {"is_error": True, "content": self.OVERLOADED}],
                                      {"statement": "S", "notes": "?"}, failed=1),
                     # One counted failure without any error result (a stalled background subagent).
                     self.critic_turn([{"content": "No bugs."}], {"statement": "S", "notes": "?"}, failed=1),
                     ["init", {"structured": {"statement": "S", "notes": "ok"}}]])
        result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        texts = [row["text"] for row in self.records("stdin")]
        self.assertEqual(texts[1], runtime.SUBAGENT_RETRY_NOTE.format(failed="1 subagent call",
                                                                      reasons=" (model at capacity)"))
        self.assertEqual(texts[2], runtime.SUBAGENT_RETRY_NOTE.format(failed="1 subagent call", reasons=""))


class FakeSession:
    """A goal session double that fails as scripted, then completes the goal."""

    def __init__(self, failures, retry=None):
        self.failures, self.calls = list(failures), []
        self.retry = retry or {"thread": "thread-1", "stage": "solve", "values": {"revision_number": 0},
                               "instruction": "Exact assignment", "delivered": True, "output": True,
                               "steer": "steer-3"}

    def __call__(self, runtime_module, prompt, *, options, initial_instruction="", **kwargs):
        self.calls.append({"options": dict(options), "initial_instruction": initial_instruction, **kwargs})
        failures, retry = self.failures, self.retry

        def session():
            if failures:
                failure = failures.pop(0)
                if isinstance(failure, BaseException):
                    raise failure
                if isinstance(failure, dict):
                    raise runtime.WorkflowPaused(failure.get("reason", "Author turn failed: " + CAPACITY),
                                                 reason=failure.get("reason", "Author turn failed: " + CAPACITY),
                                                 retry=failure)
                raise runtime.WorkflowPaused(failure, reason=failure, thread_id="thread-1", retry=dict(retry))
            yield {"outcome": "done", "output": "Proof."}
        return session()


class AuthorRetryTests(RetryCase):
    def step(self, failures, options=None, retry=None):
        session = FakeSession(failures, retry)
        options = {} if options is None else options
        sessions = {"author": session(runtime, "Prompt", options=options, initial_instruction="Recovery")}
        result = runtime._goal_step(sessions, "author", functools.partial(session, runtime, "Prompt"),
                                    "Recovery", options, next, "solve")
        return result, session

    def test_twenty_five_failures_then_success_resume_the_same_thread(self):
        reasons = [f"Author turn failed: {FAILURES[index % len(FAILURES)]}" for index in range(25)]
        result, session = self.step(reasons)
        self.assertEqual(result, {"outcome": "done", "output": "Proof."})
        self.assertEqual(len(session.calls), 26)
        for call in session.calls[1:]:
            options = call["options"]
            self.assertEqual((options["goal_thread_id"], options["goal_require_resume"], options["goal_resume"]),
                             ("thread-1", True, True))
            # The model worked in the failed turn: a short note asks it to continue.
            self.assertEqual(options["goal_retry"]["instruction"], runtime.AUTHOR_RETRY_NOTE)
            self.assertIsNone(options["goal_retry"]["rewind"])
            self.assertEqual(options["author_steer_delivered"], "steer-3")
            self.assertEqual(call["initial_instruction"], "")
        self.assertEqual([call["options"]["goal_retry"]["attempt"] for call in session.calls[1:]],
                         list(range(2, 27)))
        self.assertEqual(self.waits(), 25 * 60)
        notices = self.notices()
        self.assertEqual(notices[0]["text"], "Model call failed (model at capacity) — retrying in 1 min (attempt 2). "
                                             f"Provider message: {CAPACITY}")
        self.assertEqual(notices[0]["node"], "author")

    def test_a_turn_that_produced_nothing_is_rewound_and_sent_again(self):
        empty = {"thread": "thread-1", "stage": "repair", "values": {"revision_number": 2},
                 "instruction": "Repair: gap in step 2", "delivered": True, "output": False,
                 "rewind": {"turn": "turn-9"}}
        result, session = self.step([empty, {**empty, "rewind": {"turn": "turn-10"}}])
        self.assertEqual(result["output"], "Proof.")
        for call, turn in zip(session.calls[1:], ("turn-9", "turn-10")):
            retry = call["options"]["goal_retry"]
            # The same instruction again, never a note: the failed empty turn is removed first.
            self.assertEqual((retry["instruction"], retry["rewind"], retry["stage"]),
                             ("Repair: gap in step 2", {"turn": turn}, "repair"))

    def test_an_undelivered_instruction_is_sent_again(self):
        undelivered = {"thread": "thread-1", "stage": "repair", "values": {"revision_number": 2},
                       "instruction": "Repair: gap in step 2", "delivered": False}
        result, session = self.step([undelivered])
        self.assertEqual(result["output"], "Proof.")
        retry = session.calls[1]["options"]["goal_retry"]
        self.assertEqual((retry["instruction"], retry["stage"], retry["values"]),
                         ("Repair: gap in step 2", "repair", {"revision_number": 2}))

    def test_what_a_failing_retry_does_not_learn_keeps_its_last_known_value(self):
        """Attempt 2 fails before it resumed the thread: attempt 3 still resumes it with the repair instruction."""
        first = {"thread": "thread-1", "stage": "repair", "values": {"revision_number": 2, "bugs": "Gap."},
                 "instruction": "Repair: Gap.", "delivered": False, "steer": "steer-3"}
        early = {"thread": None, "stage": "repair", "values": {"revision_number": 2, "bugs": "Gap."},
                 "instruction": None, "delivered": False, "steer": "steer-3",
                 "reason": "Could not resume the saved Codex conversation; no replacement was started. "
                           "connection refused"}
        result, session = self.step([first, early])
        self.assertEqual(result["output"], "Proof.")
        for call in session.calls[1:]:
            options = call["options"]
            self.assertEqual(options["goal_thread_id"], "thread-1")
            self.assertEqual(options["goal_retry"]["instruction"], "Repair: Gap.")
            self.assertEqual(options["goal_retry"]["values"]["bugs"], "Gap.")

    def test_without_a_thread_the_same_first_attempt_runs_again(self):
        result, session = self.step([{"thread": None, "stage": "solve", "instruction": None,
                                      "reason": "Please run codex login"}])
        call = session.calls[1]
        self.assertNotIn("goal_thread_id", call["options"])
        self.assertEqual(call["initial_instruction"], "Recovery")
        self.assertIn("(authentication failed)", self.notices()[0]["text"])

    def test_a_new_conversation_whose_first_turn_produced_nothing_starts_over(self):
        first = {"thread": "session-1", "stage": "solve", "values": {}, "instruction": "Prove S.",
                 "delivered": True, "output": False, "rewind": {"fresh": True}}
        result, session = self.step([first], options={"goal_cwd": str(self.root)})
        options = session.calls[1]["options"]
        self.assertNotIn("goal_thread_id", options)
        self.assertNotIn("goal_require_resume", options)
        self.assertEqual(options["goal_retry"]["instruction"], "Prove S.")
        self.assertIsNone(options["goal_retry"]["rewind"])

    def test_failures_no_retry_can_fix_are_not_retried(self):
        for failure in (runtime.WorkflowPaused("Author paused at the current turn boundary."),
                        runtime.WorkflowPaused("Workflow time limit reached.", reason="Workflow time limit reached."),
                        runtime.WorkflowPaused("Could not resume", reason="Could not resume",
                                               session_unavailable=True),
                        runtime.WorkflowPaused("Stopped for safety", reason="Stopped for safety", safety_stop=True)):
            with self.subTest(failure=str(failure)), self.assertRaises(runtime.WorkflowPaused):
                self.step([failure])
        self.assertEqual(self.clock.sleeps, [])

    def test_stop_and_pause_end_an_author_wait(self):
        with mock.patch.object(runtime, "_retry_sleep", side_effect=guarded(
                lambda seconds, cancel: (_ for _ in ()).throw(KeyboardInterrupt), limit=5)):
            with self.assertRaises(KeyboardInterrupt):
                self.step(["Author turn failed: " + CAPACITY])
        pause = self.root / "pause-request.json"
        sleep = guarded(lambda seconds, cancel: pause.write_text("{}"), limit=5)
        with mock.patch.object(runtime, "_retry_sleep", side_effect=sleep):
            with self.assertRaises(runtime.WorkflowPaused) as raised:
                self.step(["Author turn failed: " + CAPACITY], {"goal_pause_file": str(pause)})
        self.assertEqual(raised.exception.thread_id, "thread-1")
        self.assertEqual(sleep.calls["count"], 1)

    def test_author_waits_do_not_consume_the_budget(self):
        options = {"thinking_hours": 0.01}
        runtime.workflow_remaining(options)
        self.step(["Author stopped: usageLimited."] * 10, options)
        self.assertEqual(self.waits(), 600)
        self.assertGreater(runtime.workflow_remaining(options), 30)

    def test_the_graph_retries_the_author_and_keeps_its_checkpoint_state(self):
        session = FakeSession(["Author turn failed: " + CAPACITY, "Author turn failed: " + CAPACITY])
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        report = {"verdict": "pass", "solution": "Proof.", "bugs": ""}
        with mock.patch.object(runtime, "goal_session", side_effect=session), \
                mock.patch.object(runtime, "structured", return_value=(report, json.dumps(report))):
            state = runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"], {"statement": "Task"})
        self.assertEqual(state["output"], "Proof.")
        self.assertFalse(state.get("paused"))
        self.assertEqual(len(session.calls), 3)
        self.assertFalse([event for event in self.events if event["kind"] == "workflow_paused"])

    def test_a_safety_stop_pauses_the_graph_without_automatic_resume(self):
        session = FakeSession([runtime.WorkflowPaused(
            "Stopped for safety: Codex returned a different thread ID.", reason="Stopped for safety: Codex "
            "returned a different thread ID.", thread_id="thread-1", safety_stop=True)])
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with mock.patch.object(runtime, "goal_session", side_effect=session):
            state = runtime.execute_workflows([ROOT / "workflows/author_critic.yaml"], {"statement": "Task"})
        self.assertTrue(state["paused"])
        paused = next(event for event in self.events if event["kind"] == "workflow_paused")
        self.assertTrue(paused["safetyStop"])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.clock.sleeps, [])


class AppServer:
    """A scripted Codex app-server, one per attempt: threads keep their turns, and thread/revert drops them."""

    def __init__(self, test, process, record):
        self.test, self.record = test, record
        self.index = len(test.servers) + 1
        self.calls, self.messages = [], []
        test.servers.append(self)

    def send(self, message):
        self.calls.append((message["method"], message.get("params")))

    def request(self, method, params):
        self.calls.append((method, params))
        return len(self.calls)

    def queue(self, method, thread, **params):
        self.messages.append({"method": method, "params": {"threadId": thread, **params}})

    def call(self, method, params):
        self.calls.append((method, params))
        test = self.test
        failure = test.failures.pop((self.index, method), None)
        if failure is not None:
            raise failure
        if method == "initialize":
            return {"codexHome": test.codex_home} if test.codex_home else {}
        if method == "thread/start":
            thread = f"thread-{len(test.threads) + 1}"
            test.threads[thread] = []
            return {"thread": {"id": thread}}
        if method == "thread/resume":
            if params["threadId"] not in test.threads:
                raise runtime.Error(f"thread/resume failed: no rollout found for thread id {params['threadId']}")
            return {"thread": {"id": test.replacement or params["threadId"]}}
        if method == "thread/revert":
            turns = test.threads[params["threadId"]]
            ids = [turn["id"] for turn in turns]
            if params["beforeTurnId"] not in ids:
                raise runtime.Error("thread/revert failed: turn not found")
            del turns[ids.index(params["beforeTurnId"]):]
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/goal/get":
            return {"goal": {"objective": "Saved objective", "status": "paused"}}
        if method == "turn/start":
            script = test.scripts.pop(0)
            if "start_error" in script:
                raise runtime.Error("turn/start failed: " + script["start_error"])
            thread = params["threadId"]
            test.turn_count += 1
            turn = f"turn-{test.turn_count}"
            entry = {"id": turn, "input": params["input"][0]["text"], "items": []}
            test.threads[thread].append(entry)
            self.queue("turn/started", thread, turn={"id": turn})
            if "work" in script:
                entry["items"].append(script["work"])
                self.queue("item/completed", thread, turnId=turn,
                           item={"type": "reasoning", "id": f"{turn}-work", "text": script["work"]})
            if "error" in script:
                self.queue("error", thread, error={"message": script["error"]}, willRetry=False)
            if "continued" in script:
                # Codex ends this turn and continues the active goal in a turn of its own.
                self.queue("turn/completed", thread, turn={"id": turn, "status": "completed", "items": []})
                test.turn_count += 1
                own = f"turn-{test.turn_count}"
                test.threads[thread].append({"id": own, "input": None, "items": []})
                self.queue("turn/started", thread, turn={"id": own})
                self.queue("error", thread, error={"message": script["continued"]}, willRetry=False)
            if "answer" in script:
                entry["items"].append(script["answer"])
                self.queue("item/completed", thread, turnId=turn,
                           item={"type": "agentMessage", "text": script["answer"], "phase": "final_answer"})
                self.queue("thread/goal/updated", thread, goal={"threadId": thread, "status": "complete"})
                self.queue("turn/completed", thread, turn={"id": turn, "status": "completed", "items": []})
            if "safety" in script:
                test.safety_reads.append(self.index)
            return {"turn": {"id": turn}}
        return {}

    def read(self):
        if self.index in self.test.safety_reads:
            raise runtime.SafetyStop("Required model tool runtime is unavailable: code mode is unavailable.")
        if not self.messages:
            raise runtime.Error("Codex app-server stopped unexpectedly.")
        message = self.messages.pop(0)
        self.record(message)
        return message


class CodexAuthorTests(RetryCase):
    """The real Codex goal session with a scripted app-server: retries resume the same thread."""

    PROMPTS = {"goal": "Prove S.", "continuation": "Continue.", "compaction": "Re-read {directory}.",
               "repair": "Repair round {round}: {bugs}", "resume": "Resume in {directory}.\n{original_prompt}"}

    def setUp(self):
        super().setUp()
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.servers, self.scripts, self.threads, self.failures = [], [], {}, {}
        self.turn_count, self.codex_home, self.replacement, self.safety_reads = 0, None, None, []
        for name, value in (("RPC", functools.partial(AppServer, self)), ("codex", lambda: "codex-test")):
            patcher = mock.patch.object(runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace(
            poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0, kill=lambda: None))
        popen.start()
        self.addCleanup(popen.stop)
        environment = mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live",
                                                    "CODEX_HOME": str(self.root / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)

    def launcher(self, model="gpt-6.1-sol"):
        settings = {"model": model, "effort": "high", "speed": "standard", "summary": "concise"}
        return functools.partial(runtime.goal_session, runtime, "Prove S exactly.", prompts=self.PROMPTS,
                                 settings=settings, features=[], stages=None, node_name="author")

    def run_goal(self, options=None, step=next, stage="solve", sessions=None, launch=None):
        launch = launch or self.launcher()
        options = {"goal_cwd": str(self.directory), **(options or {})}
        sessions = sessions or {"author": launch(options=options, initial_instruction="")}
        return runtime._goal_step(sessions, "author", launch, "", options, step, stage)

    def names(self, server_double):
        return [name for name, _ in server_double.calls]

    def sent(self, server_double):
        return [params["input"][0]["text"] for name, params in server_double.calls if name == "turn/start"]

    def test_failures_resume_the_same_thread_and_never_pile_up_notes(self):
        """Ten failures, the first after real work: one note in the thread, the empty turns reverted."""
        self.scripts = ([{"work": "Lemma 1 holds.", "error": FAILURES[5]}]
                        + [{"error": message} for message in FAILURES[:5] * 2][:9] + [{"answer": "Full proof"}])
        result = self.run_goal()
        self.assertEqual(result, {"outcome": "done", "output": "Full proof"})
        self.assertEqual(len(self.servers), 11)  # A fresh app-server for every attempt.
        self.assertIn("thread/start", self.names(self.servers[0]))
        for index, server_double in enumerate(self.servers[1:], start=2):
            names = self.names(server_double)
            self.assertIn("thread/resume", names)
            self.assertNotIn("thread/start", names)
            self.assertEqual(self.sent(server_double), [runtime.AUTHOR_RETRY_NOTE])
            reverted = [params for name, params in server_double.calls if name == "thread/revert"]
            # From the third attempt on, the previous (empty) note turn is removed first.
            self.assertEqual(reverted, [] if index == 2 else [{"threadId": "thread-1",
                                                               "beforeTurnId": f"turn-{index - 1}"}])
        turns = self.threads["thread-1"]
        self.assertEqual([turn["input"] for turn in turns], ["Prove S exactly.", runtime.AUTHOR_RETRY_NOTE])
        self.assertEqual(turns[0]["items"], ["Lemma 1 holds."])
        self.assertEqual(self.waits(), 10 * 60)
        self.assertEqual([event["failure"] for event in self.notices()[:2]], ["connection lost", "model at capacity"])
        self.assertFalse([event for event in self.events if event.get("label") == "Author paused"])

    def test_a_first_turn_that_produced_nothing_is_reverted_and_sent_again(self):
        self.scripts = [{"error": CAPACITY}, {"answer": "Full proof"}]
        self.assertEqual(self.run_goal()["output"], "Full proof")
        second = self.servers[1]
        self.assertEqual([params for name, params in second.calls if name == "thread/revert"],
                         [{"threadId": "thread-1", "beforeTurnId": "turn-1"}])
        self.assertEqual(self.sent(second), ["Prove S exactly."])
        self.assertEqual([turn["input"] for turn in self.threads["thread-1"]], ["Prove S exactly."])
        self.assertTrue(any(event.get("label") == "Failed turn removed" for event in self.events))

    def test_a_failed_turn_that_codex_started_by_itself_is_continued_with_the_note(self):
        """Our instruction was answered; the goal's own next turn failed empty: never send the task again."""
        self.scripts = [{"work": "Lemma 1 holds.", "continued": CAPACITY}, {"answer": "Full proof"}]
        self.assertEqual(self.run_goal()["output"], "Full proof")
        second = self.servers[1]
        self.assertNotIn("thread/revert", self.names(second))
        self.assertEqual(self.sent(second), [runtime.AUTHOR_RETRY_NOTE])
        self.assertEqual([turn["input"] for turn in self.threads["thread-1"]],
                         ["Prove S exactly.", None, runtime.AUTHOR_RETRY_NOTE])

    def test_an_unacknowledged_repair_turn_is_sent_again_not_the_note(self):
        self.scripts = [{"answer": "First proof"}, {"start_error": "connection reset by peer"},
                        {"answer": "Repaired proof"}]
        launch = self.launcher()
        options = {"goal_cwd": str(self.directory)}
        sessions = {"author": launch(options=options, initial_instruction="")}
        self.assertEqual(next(sessions["author"])["output"], "First proof")
        feedback = {"bugs": "Step 2 divides by zero.", "round": 1, "solution": "First proof"}
        result = runtime._goal_step(sessions, "author", launch, "", options,
                                    lambda session: session.send(feedback), "repair")
        self.assertEqual(result["output"], "Repaired proof")
        repair = "Repair round 1: Step 2 divides by zero."
        self.assertEqual(self.sent(self.servers[0]), ["Prove S exactly.", repair])
        self.assertEqual(self.sent(self.servers[1]), [repair])  # The repair, with the critic's bugs, never lost.
        self.assertNotIn("thread/revert", self.names(self.servers[1]))

    def test_a_retry_that_fails_before_resuming_keeps_the_thread_and_the_instruction(self):
        self.scripts = [{"work": "w", "error": FAILURES[5]}, {"answer": "Full proof"}]
        # Attempt 2's resume fails transiently (not a vanished thread).
        self.failures[(2, "thread/resume")] = runtime.Error(
            "thread/resume failed: error sending request: connection refused")
        self.assertEqual(self.run_goal()["output"], "Full proof")
        self.assertEqual(len(self.servers), 3)
        third = self.servers[2]
        self.assertIn("thread/resume", self.names(third))
        self.assertNotIn("thread/start", self.names(third))
        self.assertEqual(self.sent(third), [runtime.AUTHOR_RETRY_NOTE])
        self.assertEqual(list(self.threads), ["thread-1"])

    def test_a_thread_codex_no_longer_has_waits_for_the_user(self):
        self.scripts = [{"work": "w", "error": FAILURES[5]}]
        self.failures[(2, "thread/resume")] = runtime.Error(
            "thread/resume failed: no rollout found for thread id thread-1")
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            self.run_goal()
        self.assertTrue(raised.exception.session_unavailable)
        self.assertIsNone(raised.exception.retry)
        self.assertIn("automatic retry is off", raised.exception.reason)
        self.assertEqual(len(self.servers), 2)
        self.assertEqual(self.waits(), 60)  # Only the wait before the attempt that found it gone.

    def test_a_missing_thread_id_waits_for_the_user(self):
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            self.run_goal({"goal_require_resume": True, "goal_resume": True})
        self.assertTrue(raised.exception.session_unavailable)
        self.assertIsNone(raised.exception.retry)
        self.assertEqual(self.servers, [])
        self.assertEqual(self.clock.sleeps, [])

    def test_safety_refusals_are_never_retried(self):
        cases = {
            "a replaced thread": lambda: setattr(self, "replacement", "thread-other"),
            "a missing tool runtime": lambda: self.scripts.insert(1, {"safety": True}),
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                self.servers.clear()
                self.events.clear()
                self.threads, self.replacement, self.safety_reads, self.turn_count = {}, None, [], 0
                self.scripts = [{"work": "w", "error": FAILURES[5]}, {"answer": "Full proof"}]
                arrange()
                with self.assertRaises(runtime.WorkflowPaused) as raised:
                    self.run_goal()
                self.assertTrue(raised.exception.safety_stop)
                self.assertIsNone(raised.exception.retry)
                self.assertTrue(raised.exception.reason.startswith("Stopped for safety: "))
                self.assertIn("Automatic retry is off", raised.exception.reason)
                self.assertEqual(len(self.servers), 2)  # Never a third launch.
                self.assertTrue(any(event.get("label") == "Author stopped for safety" for event in self.events))
        self.assertEqual(self.waits(), 2 * 60)

    def test_an_api_model_home_that_codex_ignored_is_a_safety_stop(self):
        home = self.root / "api-home"
        home.mkdir()
        self.codex_home = str(self.root / "elsewhere")
        patches = (mock.patch.object(runtime, "model_provider", return_value=runtime.API_PROVIDER),
                   mock.patch.object(runtime, "api_codex_executable", return_value="codex-test"),
                   mock.patch.object(runtime, "api_codex_home", return_value=home),
                   mock.patch.object(runtime, "provider_arguments", return_value=[]),
                   mock.patch.object(runtime, "speed_arguments", return_value=[]),
                   mock.patch.object(runtime, "retrieval_environment", return_value={"CODEX_HOME": str(home)}))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            self.run_goal()
        self.assertTrue(raised.exception.safety_stop)
        self.assertIn("refusing to continue", raised.exception.reason)
        self.assertEqual(len(self.servers), 1)
        self.assertEqual(self.clock.sleeps, [])


class RPCTests(unittest.TestCase):
    def rpc(self, *lines):
        process = SimpleNamespace(stdout=io.StringIO("".join(line + "\n" for line in lines)), stdin=io.StringIO(),
                                  stderr=None)
        return runtime.RPC(process)

    def test_refusals_of_the_app_server_are_safety_stops_and_garbage_is_a_transport_error(self):
        with self.assertRaises(runtime.SafetyStop):
            self.rpc(json.dumps({"method": "warning", "params": {"message": "Code mode is unavailable here."}})).wire()
        with self.assertRaises(runtime.SafetyStop):
            self.rpc(json.dumps({"id": 7, "method": "item/commandExecution/requestApproval"})).wire()
        with self.assertRaises(runtime.Error) as raised:
            self.rpc("not json at all").wire()
        self.assertTrue(runtime.retryable_failure(raised.exception))
        self.assertTrue(runtime.retryable_failure(self.capture(self.rpc().wire)))

    @staticmethod
    def capture(function):
        try:
            function()
        except Exception as exc:  # noqa: BLE001 - the test inspects it.
            return exc
        return None


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class ClaudeAuthorTests(claude_test.FakeClaudeCase):
    """The real Claude goal session with the stand-in CLI: retries resume the same session."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        patch_clock(self, self.clock)

    def launcher(self, prompt="Prove S exactly."):
        return functools.partial(claude_cli.goal_session, runtime, prompt, prompts=claude_test.PROMPTS,
                                 settings={"model": "claude-opus-5-5", "effort": "max", "speed": "standard",
                                           "summary": "concise"},
                                 features=[], stages=None, node_name="author")

    def run_goal(self, options=None, launch=None, step=next, stage="solve", sessions=None):
        launch = launch or self.launcher()
        options = {"goal_cwd": str(self.run_dir), "goal_pause_file": str(self.run_dir / "pause-request.json"),
                   **(options or {})}
        self.sessions = sessions = sessions or {"author": launch(options=options, initial_instruction="")}
        self.addCleanup(lambda: sessions["author"].close())
        return runtime._goal_step(sessions, "author", launch, "", options, step, stage)

    @staticmethod
    def overloaded(*before):
        return ["init", "echo", *before, {"api_error": "API Error: 529 Overloaded"},
                {"result": {"is_error": True, "result": "API Error: 529 Overloaded", "api_error_status": 529}}]

    @staticmethod
    def work(uuid_value):
        return {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": uuid_value,
                         "message": {"content": [{"type": "text", "text": "Lemma 1 holds."}]}}}

    def branch_texts(self, session):
        """The user messages on the conversation branch a plain --resume continues (the newest leaf)."""
        path = next((self.root / "claude-config" / "projects").glob(f"*/{session}.jsonl"))
        records = {}
        leaf = None
        for line in path.read_text().splitlines():
            record = json.loads(line)
            records[record["uuid"]] = record
            leaf = record["uuid"]
        texts = []
        while leaf in records:
            if records[leaf]["type"] == "user":
                texts.append(records[leaf].get("text"))
            leaf = records[leaf]["parentUuid"]
        return texts[::-1]

    def test_a_first_turn_that_produced_nothing_starts_over_with_the_same_instruction(self):
        self.script([self.overloaded(), ["init", "echo", claude_test.complete("Inline proof.")]])
        self.assertEqual(self.run_goal(), {"outcome": "done", "output": "Inline proof."})
        starts = [row["argv"] for row in self.records("start")]
        self.assertEqual([argv[0] for argv in starts], ["--session-id", "--session-id"])
        self.assertNotEqual(starts[0][1], starts[1][1])
        self.assertEqual(self.texts(), ["Prove S exactly.", "Prove S exactly."])
        self.assertEqual(sum(self.clock.sleeps), 60)
        self.sessions["author"].close()
        self.assertPrivateFilesRemoved()

    def test_an_empty_failure_after_work_resumes_before_it_with_one_note(self):
        anchor = "0c6f7f5e-58f6-4b8e-9f37-0d9b3b0c2a11"
        worked = ["init", "echo", self.work(anchor),
                  {"result": {"is_error": True, "result": "API Error: 500 Internal server error",
                              "api_error_status": 500}}]
        self.script([worked, self.overloaded(), self.overloaded(), ["init", "echo", claude_test.complete("Done.")]])
        self.assertEqual(self.run_goal()["output"], "Done.")
        starts = [row["argv"] for row in self.records("start")]
        session = starts[0][1]
        self.assertEqual(starts[1][:2], ["--resume", session])  # After real work: a note, no rewind.
        self.assertNotIn("--resume-session-at", starts[1])
        for argv in starts[2:]:
            # The empty note turn is left behind: resume at the last answer and send the note again.
            self.assertEqual(argv[:4], ["--resume", session, "--resume-session-at", anchor])
        self.assertEqual(self.texts(), ["Prove S exactly."] + [runtime.AUTHOR_RETRY_NOTE] * 3)
        self.assertEqual(self.branch_texts(session), ["Prove S exactly.", runtime.AUTHOR_RETRY_NOTE])
        self.assertEqual(sum(self.clock.sleeps), 180)

    def test_a_relaunch_before_the_retried_turn_arrived_resumes_at_the_same_point(self):
        anchor = "5b8f0e8e-1c1f-4b55-9a3a-6f1f3b8e9c22"
        worked = ["init", "echo", self.work(anchor),
                  {"result": {"is_error": True, "result": "API Error: 500", "api_error_status": 500}}]
        # Attempt 3's CLI starts, then ends before it takes the message: the session relaunches it.
        self.script([worked, self.overloaded(), ["init", {"exit": 1}], ["init", "echo", claude_test.complete("Done.")]])
        self.assertEqual(self.run_goal()["output"], "Done.")
        starts = [row["argv"] for row in self.records("start")]
        session = starts[0][1]
        self.assertEqual([argv[:4] for argv in starts[2:]],
                         [["--resume", session, "--resume-session-at", anchor]] * 2)
        self.assertEqual(self.branch_texts(session), ["Prove S exactly.", runtime.AUTHOR_RETRY_NOTE])

    def test_a_cli_that_cannot_resume_at_a_message_is_reopened_plainly(self):
        anchor = "7c9d0e1f-2a3b-4c5d-8e6f-7a8b9c0d1e2f"
        worked = ["init", "echo", self.work(anchor),
                  {"result": {"is_error": True, "result": "API Error: 500", "api_error_status": 500}}]
        rejected = [{"stderr": "error: unknown option '--resume-session-at'"}, {"exit": 1}]
        self.script([worked, self.overloaded(), rejected, ["init", "echo", claude_test.complete("Done.")]])
        self.assertEqual(self.run_goal()["output"], "Done.")
        starts = [row["argv"] for row in self.records("start")]
        session = starts[0][1]
        self.assertEqual(starts[2][:4], ["--resume", session, "--resume-session-at", anchor])
        self.assertEqual(starts[3][:2], ["--resume", session])
        self.assertNotIn("--resume-session-at", starts[3])
        self.assertEqual(self.texts()[-1], runtime.AUTHOR_RETRY_NOTE)
        self.assertTrue(any("could not reopen the conversation" in str(event.get("text")) for event in self.events))

    def test_a_cli_that_exits_before_echoing_the_repair_is_sent_the_repair_again(self):
        self.script([["init", "echo", claude_test.complete("First proof.")],
                     [{"exit": 1}], [{"exit": 1}],  # The repair turn never reached the model, twice.
                     ["init", "echo", claude_test.complete("Repaired.")]])
        launch = self.launcher()
        options = {"goal_cwd": str(self.run_dir)}
        sessions = {"author": launch(options=options, initial_instruction="")}
        self.addCleanup(lambda: sessions["author"].close())
        self.assertEqual(next(sessions["author"])["output"], "First proof.")
        feedback = {"bugs": "Step 2 divides by zero.", "round": 1, "solution": "First proof."}
        result = runtime._goal_step(sessions, "author", launch, "", options,
                                    lambda session: session.send(feedback), "repair")
        self.assertEqual(result["output"], "Repaired.")
        repair = self.texts()[1]
        self.assertIn("Step 2 divides by zero.", repair)
        self.assertEqual(self.texts()[-1], repair)  # Never replaced by the retry note.
        self.assertNotIn(runtime.AUTHOR_RETRY_NOTE, self.texts())

    def test_a_retry_that_exits_before_init_keeps_the_session_and_the_note(self):
        worked = ["init", "echo", self.work("4f1d8a52-6a3c-4f69-9a49-2b5c0f2d7e10"),
                  {"result": {"is_error": True, "result": "API Error: 500", "api_error_status": 500}}]
        self.script([worked, [{"exit": 134}], ["init", "echo", claude_test.complete("Done.")]])
        self.assertEqual(self.run_goal()["output"], "Done.")
        starts = [row["argv"] for row in self.records("start")]
        session = starts[0][1]
        self.assertEqual([argv[:2] for argv in starts[1:]], [["--resume", session]] * 2)
        self.assertEqual(self.texts()[-1], runtime.AUTHOR_RETRY_NOTE)

    def test_a_conversation_claude_no_longer_has_waits_for_the_user(self):
        worked = ["init", "echo", self.work("6a0b6a1e-2f6b-4fd0-8a77-2a4f3c1b9d01"),
                  {"result": {"is_error": True, "result": "API Error: 500", "api_error_status": 500}}]
        self.script([worked], all_resumes_gone=True)
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            self.run_goal()
        self.assertTrue(raised.exception.session_unavailable)
        self.assertIsNone(raised.exception.retry)
        self.assertEqual(sum(self.clock.sleeps), 60)  # One retry found the conversation gone.

    def test_a_missing_session_id_waits_for_the_user(self):
        with self.assertRaises(runtime.WorkflowPaused) as raised:
            self.run_goal({"goal_require_resume": True, "goal_resume": True})
        self.assertTrue(raised.exception.session_unavailable)
        self.assertIsNone(raised.exception.retry)
        self.assertEqual(self.records("start"), [])
        self.assertEqual(self.clock.sleeps, [])

    def test_unexpected_tools_and_a_replaced_session_are_never_retried(self):
        for name, init in (("unexpected tools", {"init": {"extra_tools": ["RemoteTrigger"]}}),
                           ("replaced session", {"init": {"session_id": "11111111-2222-4333-8444-555555555555"}})):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                self.script([[init, "echo", claude_test.complete()]] * 3)
                with self.assertRaises(runtime.WorkflowPaused) as raised:
                    self.run_goal()
                self.assertTrue(raised.exception.safety_stop)
                self.assertIsNone(raised.exception.retry)
                self.assertTrue(raised.exception.reason.startswith("Stopped for safety: "))
                self.assertEqual(len(self.records("start")), 1)  # Never relaunched.
        self.assertEqual(self.clock.sleeps, [])

    def test_a_resumed_conversation_this_process_never_read_is_rewound_from_its_transcript(self):
        # An earlier runner process completed a turn of session S.
        self.script([["init", "echo", claude_test.complete("Earlier.")]])
        launch = self.launcher()
        earlier = launch(options={"goal_cwd": str(self.run_dir)}, initial_instruction="")
        self.assertEqual(next(earlier)["output"], "Earlier.")
        earlier.close()
        session = self.records("start")[0]["argv"][1]
        # This process resumes S; its first turn fails before the model produced anything.
        self.script([self.overloaded(), ["init", "echo", claude_test.complete("Resumed.")]])
        options = {"goal_thread_id": session, "goal_resume": True, "goal_require_resume": True}
        self.assertEqual(self.run_goal(options)["output"], "Resumed.")
        starts = [row["argv"] for row in self.records("start")][1:]
        self.assertEqual(starts[0][:2], ["--resume", session])
        anchor = starts[1][starts[1].index("--resume-session-at") + 1]
        self.assertTrue(claude_cli._session_id(anchor))
        resume_text = self.texts()[1]
        self.assertTrue(resume_text.startswith("Resume in "))
        self.assertEqual(self.texts()[-1], resume_text)
        self.assertEqual(self.branch_texts(session), ["Prove S exactly.", resume_text])


class RetryClockTests(RetryCase):
    def test_the_budget_pauses_only_while_every_call_in_progress_waits(self):
        clock = runtime.RetryClock()
        with clock.call("critic"), clock.call("critic"):
            # One parallel request waits while the other still works: the budget runs on.
            self.assertIsNone(clock.wait_started())
            self.clock.offset += 30
            self.assertEqual(clock.excluded(), 0)
            # Both wait: the budget pauses until one runs again.
            self.assertEqual(clock.wait_started(), "paused")
            self.clock.offset += 45
            self.assertEqual(clock.wait_ended(), "running")
            self.assertIsNone(clock.wait_ended())
        self.assertAlmostEqual(clock.excluded(), 45, delta=0.5)
        self.assertEqual([event for event in self.events if "budgetClock" in event], [])


class ServerRetryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = server.App(runs=Path(folder.name))
        self.app._new_run("Exact statement")
        self.app.state.update(phase="running", stage="critic", activeNode="critic",
                              resumedAt="2026-10-23T10:00:00+00:00", elapsedSeconds=0)

    @staticmethod
    def wait(attempt=2, activity=None, retry_at=2_000_000_000.0):
        label = f"{activity} · {runtime.RETRY_LABEL}" if activity else runtime.RETRY_LABEL
        return {"kind": "status", "stage": "critic", "label": label, "retryAt": retry_at, "retryAttempt": attempt,
                "text": f"Model call failed (model at capacity) — retrying in 1 min (attempt {attempt}).",
                **({"activityLabel": activity} if activity else {})}

    @staticmethod
    def retrying(attempt=2, activity=None):
        label = f"{activity} · {runtime.RETRY_LABEL}" if activity else runtime.RETRY_LABEL
        return {"kind": "status", "stage": "critic", "label": label, "retryAttempt": attempt,
                "text": f"Retrying the model call of the critic now (attempt {attempt}).",
                **({"activityLabel": activity} if activity else {})}

    def test_retry_waits_are_shown_and_never_charged_as_active_time(self):
        clock = {"now": server.datetime(2026, 10, 23, 10, 10, tzinfo=server.timezone.utc)}

        class Clock(server.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        with mock.patch.object(server, "datetime", Clock):
            self.app._follow_model_retry({**self.wait(), "budgetClock": "paused"})
            self.assertEqual(self.app.state["modelRetry"]["attempt"], 2)
            clock["now"] += server.timedelta(seconds=45)
            # Ten minutes of work; the 45 seconds of waiting so far do not count.
            self.assertAlmostEqual(self.app._elapsed_seconds(), 600, delta=0.01)
            clock["now"] += server.timedelta(seconds=15)
            self.app._follow_model_retry({**self.retrying(), "budgetClock": "running"})
            self.assertIsNone(self.app.state["modelRetry"])
            self.assertEqual(self.app.state["resumedAt"], "2026-10-23T10:01:00+00:00")
            clock["now"] += server.timedelta(seconds=30)
            self.assertAlmostEqual(self.app._elapsed_seconds(), 630, delta=0.01)
        saved = json.loads((self.app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(saved["resumedAt"], "2026-10-23T10:01:00+00:00")

    def run_reader(self, lines, between=None):
        """Feed runner lines to App._read_output; `between(index)` runs after each line was handled."""
        app, token = self.app, object()

        class Stdout:
            def __iter__(self):
                for index, line in enumerate(lines):
                    yield json.dumps(line) + "\n"
                    if between:
                        between(index)

        process = mock.Mock(stdout=Stdout())
        process.wait.return_value = 0
        process.poll.return_value = 0
        app.process, app.active_token, app.worker_token = process, token, token
        app._read_output(process, token)

    def test_the_reader_follows_retry_waits_and_ends_them_when_the_runner_exits(self):
        seen = {}

        def between(index):
            seen[index] = (self.app.state.get("modelRetry"), self.app._retry_wait_started is not None)

        self.run_reader([{**self.wait(activity="Independent audit 2"), "budgetClock": "paused"}], between)
        waiting, clock_paused = seen[0]
        self.assertEqual((waiting["attempt"], clock_paused), (2, True))
        self.assertTrue(waiting["text"].startswith("Model call failed (model at capacity)"))
        # The runner ended during the wait: it is shown no longer and was not active time.
        self.assertIsNone(self.app.state["modelRetry"])
        self.assertIsNone(self.app._retry_wait_started)
        self.assertNotEqual(self.app.state["resumedAt"], "2026-10-23T10:00:00+00:00")

    def test_parallel_waits_are_followed_separately(self):
        first, second = "Independent audit 1", "Independent audit 2"
        self.app._follow_model_retry(self.wait(activity=first, retry_at=2_000_000_100.0))
        self.app._follow_model_retry(self.wait(activity=second, retry_at=2_000_000_200.0))
        self.assertEqual(self.app.state["modelRetry"]["retryAt"], 2_000_000_200.0)
        # Audit 2 retries now; audit 1 still waits and stays shown.
        self.app._follow_model_retry(self.retrying(activity=second))
        self.assertEqual(self.app.state["modelRetry"]["retryAt"], 2_000_000_100.0)
        self.app._follow_model_retry(self.retrying(activity=first))
        self.assertIsNone(self.app.state["modelRetry"])

    def test_retry_requests_are_not_new_critic_rounds(self):
        self.run_reader([{"kind": "request", "stage": "critic", "label": "Exact critic input", "attempt": 1, "text": "x"},
                         {"kind": "request", "stage": "critic", "label": "Exact critic retry", "attempt": 2, "text": "y"},
                         {"kind": "request", "stage": "critic", "label": "Exact critic retry", "attempt": 3, "text": "z"}])
        self.assertEqual(self.app.state["round"], 1)

    def test_repeated_retry_records_are_not_pinned(self):
        cases = (({"kind": "request", "attempt": 1}, True), ({"kind": "request", "attempt": 2}, True),
                 ({"kind": "request", "attempt": 3}, False), ({"kind": "diagnostic", "attempt": 2}, True),
                 ({"kind": "diagnostic", "attempt": 40}, False), ({"kind": "diagnostic"}, True),
                 ({"kind": "critic_result", "attempt": 9}, True))
        for record, pinned in cases:
            self.assertEqual(server.important_record(record), pinned, record)
        before = len(self.app.pinned)
        for attempt in range(1, 501):  # About eight hours of an outage.
            self.app.add_trace({"kind": "request", "stage": "critic", "attempt": attempt, "text": "(Identical)"})
            self.app.add_trace({"kind": "diagnostic", "stage": "critic", "attempt": attempt, "text": "at capacity"})
        self.assertEqual(len(self.app.pinned) - before, 4)


class TerminalTests(unittest.TestCase):
    def test_concise_output_prints_every_retry_and_counts_a_critic_round_once(self):
        stream = io.StringIO()
        output = terminal.ConciseHeadlessOutput(stream, threading.Lock(), "p.md")
        records = [
            {"kind": "request", "stage": "critic", "attempt": 1, "text": "x"},
            {"kind": "status", "stage": "critic", "label": runtime.RETRY_LABEL,
             "text": "Model call failed (model at capacity) — retrying in 1 min (attempt 2)."},
            {"kind": "request", "stage": "critic", "attempt": 2, "text": "y"},
            {"kind": "status", "stage": "critic", "label": "Independent audit 2 · " + runtime.RETRY_LABEL,
             "text": "Model call failed (rate limited) — retrying in 1 min (attempt 3)."},
            {"kind": "status", "stage": "solve", "label": "Author stopped for safety", "text": "Stopped for safety: x"},
            {"kind": "request", "stage": "critic", "attempt": 1, "text": "z"},
        ]
        for record in records:
            output.write(json.dumps(record) + "\n")
        self.assertEqual(stream.getvalue().splitlines(), [
            "[p.md] Current step: Independent critic (round 1)",
            "[p.md] Retrying model call: Model call failed (model at capacity) — retrying in 1 min (attempt 2).",
            "[p.md] Independent audit 2 · Retrying model call: Model call failed (rate limited) — retrying in 1 min "
            "(attempt 3).",
            "[p.md] Author stopped for safety: Stopped for safety: x",
            "[p.md] Current step: Independent critic (round 2)",
        ])

    def test_terminal_runs_resume_error_pauses_like_the_web_server(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        statement = Path(folder.name) / "p.md"
        statement.write_text("Prove S.")
        jobs = []

        class Job:
            def __init__(self, runs=None, output_stream=None, load_latest=True):
                self.lock, self.run_dir, self.resumed = threading.Lock(), Path(folder.name) / "run", 0
                self.state = {"phase": "idle", "error": "", "autoResumeAt": 0}
                jobs.append(self)

            def start_direct_statement(self, statement, **options):
                # The runner itself ended: an error pause with an automatic retry scheduled.
                self.state.update(phase="paused", autoResumeAt=1, error="The author exited (code 1).")

            def has_active_worker(self):
                return False

            def auto_resume_if_due(self, resume):
                resume()
                return True

            def resume(self):
                self.resumed += 1
                self.state.update(phase="done", autoResumeAt=0, error="")

            def stop(self):
                pass

        errors = io.StringIO()
        with mock.patch.object(terminal, "App", Job), mock.patch.object(terminal, "direct_cli_options", return_value={}):
            code = terminal.run_headless_markdown(statement, runs=Path(folder.name), output_stream=io.StringIO(),
                                                  error_stream=errors)
        self.assertEqual((code, jobs[0].resumed), (0, 1))
        self.assertIn("Proof finished", errors.getvalue())
        # A pause that waits for the user (no retry scheduled) ends the terminal run.
        waiting = SimpleNamespace(lock=threading.Lock(), state={"phase": "paused", "autoResumeAt": 0},
                                  has_active_worker=lambda: False)
        self.assertFalse(terminal._job_active(waiting))
        broken = SimpleNamespace(auto_resume_if_due=mock.Mock(side_effect=ValueError("damaged checkpoint")),
                                 resume=None)
        terminal._retry_paused_job(broken)  # A damaged checkpoint does not crash the terminal.


class RunPageTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_a_running_job_says_why_it_waits_and_when_it_retries(self):
        import subprocess
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const renderer = source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function localDateTime('))
  + source.slice(source.indexOf('function render(next)'), source.indexOf('async function refresh()'));
const setup = `
let state = {}, previousPhase = 'running', currentJob = 'same-job', timer, clock;
const ui = new Proxy({}, {get(target, name) {return target[name] ||= {
  classList: {toggle() {}}, setAttribute() {}, focus() {}, replaceChildren() {}
};}});
const document = {activeElement: null, body: {append() {}}, createElement() {return {click() {}, remove() {}};}};
const show = (element, visible) => element.hidden = !visible;
const retainTrace = entries => entries;
const ingest = () => {}, syncMemoryPanel = () => {}, renderWorkflow = () => {};
const renderClock = () => {}, renderBatchNavigation = () => {}, refresh = () => {};
const renderResearchAudits = () => {};
const setTimeout = () => 1, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
`;
const checks = `
const running = {phase: 'running', stage: 'critic', activeNode: 'critic'};
render({...running, modelRetry: {text: 'Model call failed (model at capacity) — retrying in 1 min (attempt 7).',
                                 retryAt: 2000000000, attempt: 7}});
assert.match(ui.runDescription.textContent,
  /^Model call failed \\(model at capacity\\) — retrying in 1 min \\(attempt 7\\)\\. Next attempt at /);
assert.equal(ui.stop.hidden, false);
assert.equal(ui.resume.hidden, true);
render({...running, modelRetry: null});
assert.equal(ui.runDescription.textContent, '');
`;
vm.runInNewContext(setup + renderer + checks, {assert});
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js")],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
