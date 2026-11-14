"""A job whose model calls keep failing the same way (owner's decision, 14 Nov 2026).

It is still retried every minute without limit, and it is listed under Needs your attention on the Overview (and
marked on its job page) while it does: at once when the failure says that the account is out of credit (Claude Code's
"Credit balance is too low") or that a usage limit was reached, otherwise once the same failure has repeated for
server.FAILING_ATTENTION_SECONDS (15 minutes). The mark clears when a model call works again (App._follow_failures()).
No test starts a model: the server takes the runner's records as the runner writes them, and the team's stand-in
Claude Code replays the lines Claude Code 2.1.289 printed for a key out of credit (tests/test_claude_request_rejected).
"""

import json
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers
from ui import server
import workflow_runner as runtime

import test_claude_request_rejected as rejected_test
import test_decisions_jobs as jobs_test
import test_team_goal_loop as team_test
import test_time_limit_conversation as limit_test

START = 1_800_000_000.0
# The runner's words for Claude Code's report of a key out of credit (CAPTURED_CREDIT), as the critic's retry says it.
CREDIT = "Credit balance is too low"


def retry(failure, attempt=2, activity="Critic coordinator", stage="critic", text=None, **fields):
    """The runner's status for a failed attempt that waits to retry (workflow_runner.wait_before_retry)."""

    return {"kind": "status", "stage": stage, "label": f"{activity} · {runtime.RETRY_LABEL}" if activity
            else runtime.RETRY_LABEL, "text": text or f"Model call failed ({failure}) — retrying in 1 min (attempt "
            f"{attempt}).", "retryAttempt": attempt, "retryInSeconds": 60, "retryAt": START + 10**6,
            "failure": failure, "failureKind": "model_call", **({"activityLabel": activity} if activity else {}),
            **fields}


def retrying_now(attempt=2, activity="Critic coordinator", stage="critic"):
    """The status that ends the wait: the next attempt starts (no retry time)."""

    return {"kind": "status", "stage": stage, "label": f"{activity} · {runtime.RETRY_LABEL}", "retryAttempt": attempt,
            "text": f"Retrying the model call of the critic now (attempt {attempt}).", "activityLabel": activity}


def item_event(item_type="agent_message", activity="Critic coordinator", stage="critic", **fields):
    """A Codex event of a structured call with an item of the model's own (as run_structured_attempt relays it)."""

    return {"kind": "codex_event", "stage": stage, "activityLabel": activity,
            "event": {"type": "item.completed", "item": {"id": "item_1", "type": item_type, "text": "Checked."}},
            **fields}


class FailingCallTests(jobs_test.Isolated):
    """The server's view of a running job's retries: what Needs your attention says, and when."""

    def setUp(self):
        super().setUp()
        self.now = START
        clock = mock.patch.object(server.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def running(self, **values):
        """A job whose runner works on the critic (an author that paused once by error, then resumed)."""

        app = self.paused_job()
        app.state.update({"phase": "running", "stage": "critic", "activeNode": "critic", "criticModel": "gpt-6.1-sol",
                          "authorModel": "gpt-6.1-sol", **values})
        return app

    @staticmethod
    def follow(app, record):
        """As _read_records takes one runner record."""

        app._follow_model_retry(record)
        app._follow_failures(record)

    def fail_for(self, app, minutes, failure="model at capacity", **fields):
        """The same failure every minute for ``minutes`` minutes (the first at the current time)."""

        for minute in range(minutes + 1):
            if minute:
                self.now += 60
                self.follow(app, retrying_now(minute + 1, **{key: fields[key] for key in ("activity", "stage")
                                                             if key in fields}))
            self.follow(app, retry(failure, attempt=minute + 2, **fields))

    def test_the_same_failure_for_15_minutes_lists_the_job_while_its_retries_go_on(self):
        app = self.running()
        self.assertEqual(server.FAILING_ATTENTION_SECONDS, 15 * 60)
        self.fail_for(app, 14)
        self.assertIsNone(app.attention())
        self.now += 60
        self.follow(app, retry("model at capacity", attempt=17))
        attention = app.attention()
        self.assertEqual((attention["kind"], attention["title"], attention["retrying"]),
                         ("failing", "Model calls keep failing", True))
        # It names the call, how long and why, that the retries go on, and the 15 minutes after which it is listed.
        self.assertEqual(attention["text"], "The model call of the critic coordinator has failed the same way for "
                                            "15 min (model at capacity). It is retried every 1 minute, and the job "
                                            "continues by itself once a call works; a call is listed here once the "
                                            "same failure has repeated for 15 minutes.")
        # Between two attempts no call waits to retry: the job stays listed while its call is tried again.
        self.now += 60
        self.follow(app, retrying_now(18))
        self.assertIsNone(app.state["modelRetry"])
        self.assertEqual(app.attention()["kind"], "failing")
        self.assertIn("for 16 min", app.attention()["text"])
        # The job page and the Overview read it from the job's state.
        self.assertEqual(app.snapshot()["needsAttention"]["kind"], "failing")

    def test_another_failure_starts_the_count_again(self):
        app = self.running()
        self.fail_for(app, 10)
        self.now += 60
        self.fail_for(app, 10, failure="connection lost")
        # 21 minutes of failures, but only 10 of the same one.
        self.assertIsNone(app.attention())
        self.now += 5 * 60
        self.follow(app, retry("connection lost", attempt=30))
        self.assertIn("for 15 min (connection lost)", app.attention()["text"])

    def test_numbers_and_ids_that_change_between_attempts_are_the_same_failure(self):
        app = self.running()
        for minute in range(16):
            self.follow(app, retry(f"stream error: request req_{minute:04d}abcdef0123 failed after {30 + minute} s",
                                   attempt=minute + 2))
            self.now += 60
        self.assertEqual(app.attention()["kind"], "failing")
        self.assertEqual(server._failure_signature("Request 5f0c2a9e-1b2c-4d3e-8f9a-0b1c2d3e4f50 timed out after 12 s"),
                         server._failure_signature("request 0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d timed out after 7 s"))
        self.assertNotEqual(server._failure_signature("model at capacity"), server._failure_signature("connection lost"))
        # A cause in the runner's plain words is one failure whatever the provider's line; a CLI that stopped
        # unexpectedly is told apart by the provider's line.
        self.assertEqual(server._failure_signature("model at capacity", "Selected model is at capacity."),
                         server._failure_signature("model at capacity", "The server is overloaded."))
        self.assertNotEqual(server._failure_signature("the CLI stopped unexpectedly", CREDIT),
                            server._failure_signature("the CLI stopped unexpectedly", "Overloaded"))

    def test_the_mark_clears_when_a_model_call_works_again(self):
        app = self.running()
        self.fail_for(app, 15)
        self.assertEqual(app.attention()["kind"], "failing")
        self.follow(app, retrying_now(18))
        # Events that are no output of the model's own leave it: the turn's start, the user's message, an error item.
        for event in ({"type": "turn.started"}, {"type": "item.completed", "item": {"type": "error", "message": "x"}},
                      {"method": "item/started", "params": {"item": {"type": "userMessage"}}}):
            self.follow(app, {"kind": "codex_event", "stage": "critic", "activityLabel": "Critic coordinator",
                              "event": event})
            self.assertEqual(app.attention()["kind"], "failing", event)
        # Another call's output leaves it too.
        self.follow(app, item_event(activity="Convention critic"))
        self.assertEqual(app.attention()["kind"], "failing")
        # The model worked in this call: listed no more.
        self.follow(app, item_event())
        self.assertIsNone(app.attention())
        # The author's Codex events (app-server notifications) end the author's call the same way.
        author = self.running(stage="solve", activeNode="author")
        for minute in range(16):
            self.follow(author, retry("model at capacity", attempt=minute + 2, activity="", stage="solve", node="author"))
            self.now += 60
        self.assertEqual(author.attention()["kind"], "failing")
        self.assertIn("The model call of the proof author has failed", author.attention()["text"])
        self.follow(author, {"kind": "codex_event", "stage": "solve", "node": "author", "root": True,
                             "event": {"method": "item/started", "params": {"item": {"type": "reasoning"}}}})
        self.assertIsNone(author.attention())

    def answered_and_refused(self, app, minutes, failure, **fields):
        """Every minute the model answers (its message) and the runner refuses the answer the same way; ``seen`` has the
        job's attention after each answer and after each refusal."""

        seen = []
        activity = {key: fields.pop(key) for key in ("activity", "stage") if key in fields}
        for minute in range(minutes + 1):
            if minute:
                self.now += 60
                self.follow(app, retrying_now(minute + 1, **activity))
                self.follow(app, item_event(**activity))
                seen.append(app.attention())
            self.follow(app, retry(failure, attempt=minute + 2, **activity, **fields))
            seen.append(app.attention())
        return [item and item["kind"] for item in seen]

    def test_an_answer_refused_every_minute_is_listed_after_15_minutes_until_one_is_accepted(self):
        # A critic on a gateway that ignores the answer schema: it answers every minute, and every answer is refused
        # the same way. None of these attempts worked, so its answers do not start the 15 minutes again.
        app = self.running()
        kinds = self.answered_and_refused(app, 60, "the final answer was not one complete JSON object",
                                          failureKind="invalid_answer")
        # Listed from the 15th minute on, also between an answer and its refusal (it never goes and comes back).
        self.assertEqual(kinds, [None] * 29 + ["failing"] * 92)
        self.assertTrue(app.attention()["text"].startswith(
            "The model call of the critic coordinator has failed the same way for 60 min (the final answer was not "
            "one complete JSON object)."), app.attention())
        # An answer accepted: the step's result ends it.
        self.follow(app, item_event())
        self.assertEqual(app.attention()["kind"], "failing")
        self.follow(app, {"kind": "critic_result", "stage": "critic", "report": {"verdict": "pass"}})
        self.assertIsNone(app.attention())
        # So does the next step for a call whose step has no result of its own: the LaTeX editor's.
        for minute in range(16):
            self.follow(app, retry("proof.md does not exist", attempt=minute + 2, activity="", stage="final",
                                   failureKind="invalid_answer"))
            self.now += 60
            self.follow(app, item_event(activity="", stage="final"))
        self.assertEqual(app.attention()["kind"], "failing")
        self.follow(app, {"kind": "status", "stage": "final", "label": "Workflow step: latex_compile",
                          "node": "latex_compile", "visit": 1})
        self.assertIsNone(app.attention())

    def test_an_answer_set_aside_and_a_stream_that_drops_after_some_output_keep_counting(self):
        # An answer set aside after failed subagent calls, every minute the same way.
        app = self.running()
        kinds = self.answered_and_refused(app, 16, "2 subagent calls failed (model at capacity)",
                                          failureKind="subagent", failureReason="Selected model is at capacity.")
        self.assertEqual(kinds, [None] * 29 + ["failing"] * 4)
        # A stream that drops after some output: the runner says the model worked in that attempt (modelOutput).
        app = self.running()
        kinds = self.answered_and_refused(app, 16, "connection lost", modelOutput=True)
        self.assertEqual(kinds, [None] * 29 + ["failing"] * 4)
        # A failure before any output stays one the model's first output ends (the provider works again).
        self.follow(app, retry("model at capacity", attempt=30))
        self.now += 15 * 60
        self.follow(app, retry("model at capacity", attempt=31))
        self.assertEqual(app.attention()["kind"], "failing")
        self.follow(app, item_event())
        self.assertIsNone(app.attention())

    def test_a_steps_result_ends_its_calls_that_kept_failing(self):
        app = self.running()
        self.fail_for(app, 15)
        self.follow(app, {"kind": "critic_result", "stage": "critic", "report": {"verdict": "pass"}})
        self.assertIsNone(app.attention())

    def test_a_runner_that_ends_takes_its_calls_with_it(self):
        app = self.running()
        self.fail_for(app, 15)
        self.assertEqual(app.attention()["kind"], "failing")
        app._model_retries, app._retry_starts, app._failure_streaks = {}, {}, {}
        self.assertIsNone(app.attention())

    def test_an_account_out_of_credit_is_listed_at_once(self):
        model = providers.save_api_model({"format": "anthropic", "baseUrl": "https://gateway.example.com",
                                          "headerName": "x-api-key", "model": "claude-gw", "apiKey": jobs_test.KEY,
                                          "label": "Gateway"})["id"]
        app = self.running(criticModel=model)
        # The runner's words for Claude Code's report (its plain words are the provider's own line).
        text = rejected_test.runner_text(rejected_test.CAPTURED_CREDIT)
        self.assertEqual(runtime.describe_failure(runtime.Error(text)), (CREDIT, CREDIT))
        self.follow(app, retry(CREDIT))
        attention = app.attention()
        self.assertEqual((attention["kind"], attention["title"], attention["retrying"]), ("credit", "Out of credit", True))
        self.assertEqual(attention["text"], "Gateway reports that its credit is used up (Credit balance is too low). "
                                            "The job retries every 1 minute and continues by itself once credit is "
                                            "added.")
        # Listed while its call is tried again, and no more once a call works.
        self.follow(app, retrying_now(3))
        self.assertEqual(app.attention()["kind"], "credit")
        self.follow(app, item_event())
        self.assertIsNone(app.attention())
        # Other gateways' words for it.
        for words in ("402 Payment Required", "Insufficient credits", "insufficient_balance: top up your account",
                      "This request requires more credits, or fewer max_tokens."):
            with self.subTest(words=words):
                self.assertTrue(runtime.out_of_credit(words))
        self.assertFalse(runtime.out_of_credit("model at capacity"))

    def test_a_usage_limit_stays_listed_between_its_attempts(self):
        app = self.running()
        self.follow(app, retry("usage limit reached"))
        self.assertEqual(app.attention()["kind"], "usage")
        self.follow(app, retrying_now(3))
        self.assertIsNone(app.state["modelRetry"])
        self.assertEqual(app.attention()["kind"], "usage")
        self.follow(app, item_event())
        self.assertIsNone(app.attention())

    def test_a_team_members_usage_limit_is_listed_at_once_whatever_the_other_calls_do(self):
        app = self.running(stage="solve", activeNode="author")

        def member_failed(number, model, failure, attempt=1):
            return {"kind": "diagnostic", "stage": "solve", "node": "author", "subagentMember": number,
                    "subagentModel": model, "subagentId": f"member-{number}-1", "attempt": attempt,
                    "text": f"Member {number} ({model}): a model call failed ({failure}); retrying in 60 s, running "
                            "the assignment again.", "failure": failure, "retryAt": START + 10**6}

        # Member 1 (its own API connection) is at its usage limit while the coordinator's call waits to retry a
        # connection that was lost.
        self.follow(app, member_failed(1, "Lab Claude", "usage limit reached"))
        self.now += 5
        self.follow(app, retry("connection lost", activity="", stage="solve", node="author"))
        attention = app.attention()
        self.assertEqual((attention["kind"], attention["title"]), ("usage", "Usage limit reached"))
        self.assertTrue(attention["text"].startswith("Lab Claude reports a usage limit."), attention)
        # Listed every minute after, while member 2 fails later for another reason and the coordinator goes on.
        for minute in range(1, 6):
            self.now += 60
            self.follow(app, member_failed(1, "Lab Claude", "usage limit reached", minute + 1))
            self.now += 5
            self.follow(app, member_failed(2, "Other gateway", "model at capacity", minute))
            self.follow(app, retrying_now(minute + 2, activity="", stage="solve"))
            self.assertEqual(app.attention()["kind"], "usage", minute)
        # Member 1 works again: the others' failures are not yet 15 minutes old.
        self.follow(app, {"kind": "codex_event", "stage": "solve", "node": "author", "root": False,
                          "subagentId": "member-1-1", "subagentMember": 1,
                          "event": {"type": "item.completed", "item": {"type": "agent_message", "text": "On it."}}})
        self.assertIsNone(app.attention())

    def test_a_team_member_that_keeps_failing(self):
        app = self.running(stage="solve", activeNode="author")

        def member_failed(failure, attempt):
            # The team's note of a member's failed attempt (runner/team.py Team._run).
            return {"kind": "diagnostic", "stage": "solve", "node": "author", "subagentMember": 2,
                    "subagentModel": "Lab Claude", "subagentId": "member-2-1", "attempt": attempt,
                    "text": f"Member 2 (Lab Claude): a model call failed ({failure}); retrying in 60 s, running the "
                            "assignment again.", "failure": failure, "retryAt": START + 10**6}

        self.follow(app, member_failed(CREDIT, 1))
        attention = app.attention()
        self.assertEqual(attention["kind"], "credit")
        self.assertTrue(attention["text"].startswith("Lab Claude reports that its credit is used up"), attention)
        # Its own output ends it, not the coordinator's (the same node, no assignment).
        self.follow(app, {"kind": "codex_event", "stage": "solve", "node": "author", "root": True,
                          "event": {"method": "item/completed", "params": {"item": {"type": "agentMessage"}}}})
        self.assertEqual(app.attention()["kind"], "credit")
        self.follow(app, {"kind": "codex_event", "stage": "solve", "node": "author", "root": False,
                          "subagentId": "member-2-1", "subagentMember": 2,
                          "event": {"type": "item.completed", "item": {"type": "agent_message", "text": "On it."}}})
        self.assertIsNone(app.attention())
        # 15 minutes of the same failure; the end of its assignment ends it too.
        for minute in range(16):
            self.follow(app, member_failed("model at capacity", minute + 2))
            self.now += 60
        self.assertIn("The model call of author subagent 2 (Lab Claude) has failed the same way for 16 min",
                      app.attention()["text"])
        self.follow(app, {"kind": "status", "stage": "solve", "node": "author", "authorSubagent": True,
                          "subagentId": "member-2-1", "subagentState": "finished", "subagentMember": 2})
        self.assertIsNone(app.attention())

    def test_a_paused_authors_reply_that_keeps_failing(self):
        app = self.running()
        app.state.update(phase="paused", stage="solve", activeNode="author")
        prefix = "conversation:c_0123456789:"
        for minute in range(16):
            app._follow_failures(retry("model at capacity", attempt=minute + 2, activity="", stage="conversation",
                                       node="author", conversationId="c_0123456789"), key_prefix=prefix)
            self.now += 60
        attention = app.attention()
        self.assertEqual(attention["kind"], "failing")
        self.assertTrue(attention["text"].startswith("The author's reply has failed the same way for 16 min"),
                        attention)
        # A running job does not take a reply's failures (and the other way round).
        app.state["phase"] = "running"
        self.assertIsNone(app.attention())

    def test_the_overview_lists_the_job(self):
        app = self.paused_job(sourceFile="P07.md")
        app.state.update(phase="running", stage="critic", activeNode="critic")
        manager = self.server(serve=True)
        manager.jobs = {app.run_dir.name: app}
        self.fail_for(app, 15)
        status, value = self.call(manager, "/batches")
        root = next(card for card in value["batches"] if card["id"] == "__root__")
        [item] = root["attention"]
        self.assertEqual((item["runId"], item["job"], item["kind"], item["title"]),
                         (app.run_dir.name, "P07.md", "failing", "Model calls keep failing"))


class ReaderTests(limit_test.PausedAuthorCase):
    """The server's readers of a runner's and a reply's records follow their failures as the records arrive."""

    def stream(self, records, seen, clock):
        """``records`` as a runner prints them, one at a time; ``seen`` gets the job's attention at each None."""

        for record in records:
            if record is None:
                seen.append(self.app.attention())
                continue
            clock[0] = START + 60 * (record.pop("minute", 0))
            yield json.dumps(record) + "\n"

    def test_the_runners_reader_lists_a_call_that_keeps_failing_until_it_works_and_the_runner_ends(self):
        seen, clock = [], [START]
        records = [{**retry("model at capacity", attempt=minute + 2), "minute": minute} for minute in range(16)]
        records += [None, {**item_event(), "minute": 15}, None, {**retry("model at capacity", attempt=30),
                                                                 "minute": 16}, {**retry(CREDIT), "minute": 17}, None]
        token = object()
        process = limit_test.FakeRunner(code=1)
        process.stdout = self.stream(records, seen, clock)
        self.app.process, self.app.active_token, self.app.worker_token = process, token, token
        with mock.patch.object(server.time, "time", side_effect=lambda: clock[0]):
            self.app._read_output(process, token)
            # The runner ended: none of its calls fails any more.
            self.assertIsNone(self.app._failure_streaks.get("Critic coordinator"))
        self.assertEqual([item and item["kind"] for item in seen], ["failing", None, "credit"])

    def test_a_replys_reader_lists_the_paused_job_until_the_reply_works(self):
        self.time_limit_pause()
        seen, clock = [], [START]
        failed = {"kind": "status", "stage": "conversation", "node": "author", "label": runtime.RETRY_LABEL,
                  "text": "Model call failed (model at capacity) — retrying in 1 min.", "retryAt": START + 10**6,
                  "failure": "model at capacity"}
        records = [{**failed, "retryAttempt": minute + 2, "minute": minute} for minute in range(16)]
        records += [None, {"kind": "conversation_reply", "stage": "conversation", "label": "Author reply",
                           "conversationKind": "summary", "text": "Progress so far.", "threadId": "thread-1",
                           "minute": 16}, None]
        runner = limit_test.FakeRunner()
        runner.stdout = self.stream(records, seen, clock)
        self.runners.append(runner)
        with mock.patch.object(server.time, "time", side_effect=lambda: clock[0]):
            self.assertTrue(self.app.start_conversation_if_due())
            target, args = self.spawned[-1]
            target(*args)
        self.assertEqual(seen[0]["kind"], "failing")
        self.assertTrue(seen[0]["text"].startswith("The author's reply has failed the same way for 15 min (model at "
                                                   "capacity)."), seen[0])
        self.assertIsNone(seen[1])
        self.assertEqual(self.app._failure_streaks, {})

    def test_a_reply_runner_that_ends_takes_its_calls_with_it(self):
        self.time_limit_pause()
        seen, clock = [], [START]
        failed = {"kind": "status", "stage": "conversation", "node": "author", "label": runtime.RETRY_LABEL,
                  "text": "Model call failed (model at capacity) — retrying in 1 min.", "retryAt": START + 10**6,
                  "failure": "model at capacity"}
        runner = limit_test.FakeRunner(code=1)
        runner.stdout = self.stream([{**failed, "retryAttempt": minute + 2, "minute": minute} for minute in range(16)]
                                    + [None], seen, clock)
        self.runners.append(runner)
        with mock.patch.object(server.time, "time", side_effect=lambda: clock[0]):
            self.assertTrue(self.app.start_conversation_if_due())
            target, args = self.spawned[-1]
            target(*args)
            # The reply is asked again later: until then, nothing of it fails.
            self.assertEqual(seen[0]["kind"], "failing")
            self.assertEqual(self.app._failure_streaks, {})
            self.assertIsNone(self.app.attention())


class MemberNoteTests(team_test.TeamCase):
    """A team member's failed attempt says its failure in plain words and when it is retried (for the server)."""

    def test_the_retry_note_carries_the_failure_and_its_retry_time(self):
        model = providers.save_api_model({"format": "anthropic", "baseUrl": "https://lab.example.com",
                                          "headerName": "x-api-key", "model": "claude-gw",
                                          "apiKey": team_test.API_KEY, "label": "Lab Claude"})["id"]
        # Claude Code's last line for a key out of credit (it reports no failed message first), then a turn that works.
        self.scenario.write_text(json.dumps({"turns": [
            ["init", rejected_test.replayed(rejected_test.CAPTURED_CREDIT),
             *({"stderr": line} for line in rejected_test.CAPTURED_STDERR), {"exit": 1}],
            ["init", {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "m1",
                               "message": {"content": [{"type": "text", "text": "Lemma A holds."}]}}},
             {"result": {"result": "Lemma A holds."}}]]}))
        crew = self.crew([{"model": model, "effort": "high"}])
        crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
        report, = self.collect(crew, 1)
        self.assertIn("REPORT:\nLemma A holds.", report)
        note, = [event for event in self.events if event["kind"] == "diagnostic" and event.get("subagentId")
                 and "a model call failed" in event.get("text", "")]
        # The CLI ended with an error code (the runner's plain words), and the provider's line says why.
        self.assertEqual((note["failure"], note["failureReason"]), ("the CLI stopped unexpectedly", CREDIT))
        self.assertTrue(server.finite_seconds(note["retryAt"]))
        self.assertTrue(runtime.out_of_credit(note["failureReason"]))
        # Such a note lists its job at once, with the provider's words.
        app = server.App(runs=self.root / "runs", load_latest=False)
        app.state.update(phase="running", stage="solve", activeNode="author", authorModel="gpt-6.1-sol")
        app._follow_failures(note)
        attention = app.attention()
        self.assertEqual(attention["kind"], "credit")
        self.assertTrue(attention["text"].startswith("claude-gw reports that its credit is used up (Credit balance is "
                                                     "too low)."), attention)


if __name__ == "__main__":
    unittest.main()
