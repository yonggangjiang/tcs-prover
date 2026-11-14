"""A coordinator that cannot use TCS Prover's team fails loudly, and its run goes on (workflow_runner.CoordinatorWatch).

After the first real coordinator trial (14 Nov 2026), a coordinator whose requests reach the model without TCS
Prover's team tools (an API connection or proxy that drops them) could neither delegate nor hand in, unseen. Now the
job is listed under Needs your attention, while the work goes on, when the coordinator with TCS Prover's team makes
COORDINATOR_TEAMLESS_CALLS (10) model calls or works COORDINATOR_TEAMLESS_SECONDS (20 minutes) without any team call,
when Codex refuses a team tool call ("unsupported call" in its app-server's error output: the real Codex 0.160
against a scripted model server printed it for the flattened name, a tool the namespace does not have and a
namespace no server has, and sent no notification), and when a coordinator under TCS Prover's goal loop ends
COORDINATOR_UNTAKEN_HANDINS (3) turns with a reply that names its proof file without update_goal's "complete". Each
warning clears by itself: at the next team call, the next team call that reaches TCS Prover, the next hand-in.

The author's "Author cache usage" status covers the whole turn's model requests, not only its last one.

Offline: the scripted RPC double of tests/test_goal_runtime.py plays Codex's app-server; no CLI is started.
"""

import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import team
from ui import server
import workflow_runner as runtime

import test_decisions_jobs as jobs_test
import test_goal_runtime as goal_test
import test_team_goal_loop as team_test

POINTER = team_test.POINTER
# Codex 0.160's app-server error output for three refused team tool calls of a scripted coordinator (14 Nov 2026,
# ~/tcs-work/scratch/coord-ready/e2e): its terminal colours as an earlier run's log kept them, then plain.
REFUSED_LINES = (
    "\x1b[2m2026-11-13T14:47:13.437108Z\x1b[0m \x1b[31mERROR\x1b[0m \x1b[2mcodex_core::tools::router\x1b[0m\x1b[2m:"
    "\x1b[0m \x1b[3merror\x1b[0m\x1b[2m=\x1b[0munsupported call: mcp__tcs__team_members",
    "2026-11-14T14:56:45.057940Z ERROR codex_core::tools::router: error=unsupported call: mcp__tcsteam_status",
    "2026-11-14T14:56:45.065204Z ERROR codex_core::tools::router: error=unsupported call: mcp__teamteam_wait",
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.records, self.clock = [], Clock()
        self.watch = runtime.CoordinatorWatch(lambda kind, **fields: self.records.append({"kind": kind, **fields}),
                                              clock=self.clock)

    def warnings(self, kind=None):
        return [record for record in self.records if record.get("coordinatorWarning")
                and (kind is None or record["coordinatorWarning"] == kind)]

    def cleared(self):
        return [record["coordinatorWarningCleared"] for record in self.records
                if record.get("coordinatorWarningCleared")]

    def test_the_limits_are_named(self):
        self.assertEqual((runtime.COORDINATOR_TEAMLESS_CALLS, runtime.COORDINATOR_TEAMLESS_SECONDS,
                          runtime.COORDINATOR_UNTAKEN_HANDINS), (10, 20 * 60, 3))

    def test_ten_model_calls_without_a_team_call_warn_once_until_a_team_call(self):
        for _ in range(9):
            self.watch.model_call()
        self.assertEqual(self.records, [])
        self.clock.now += 125
        self.watch.model_call()
        warning, = self.warnings("teamless")
        self.assertEqual((warning["kind"], warning["label"], warning["needsAttention"], warning["modelCalls"]),
                         ("diagnostic", "Coordinator works without its team", True, 10))
        self.assertEqual(warning["text"], (
            "The coordinator has made 10 model calls in 2 min without calling any team tool: its requests may reach "
            "the model without TCS Prover's team tools (an API connection or proxy that drops them, for example), "
            "so the work is not delegated. The run goes on, and the job is listed under Needs your attention until "
            "the coordinator calls a team tool (listed after 10 model calls or 20 min without one)."))
        for _ in range(15):
            self.watch.model_call()
        self.assertEqual(len(self.warnings()), 1)  # Once.
        self.watch.team_call("call-1")
        self.assertEqual(self.cleared(), ["teamless"])
        cleared = self.records[-1]
        self.assertEqual((cleared["kind"], cleared["label"]), ("status", "Coordinator warning cleared"))
        self.watch.team_call("call-1", finished=True)
        # The count starts again.
        for _ in range(9):
            self.watch.model_call()
        self.assertEqual(len(self.warnings()), 1)
        self.watch.model_call()
        self.assertEqual(len(self.warnings()), 2)

    def test_twenty_minutes_without_a_team_call_warn_but_a_running_team_call_or_a_waiting_goal_stop_the_clock(self):
        self.watch.tick()
        self.clock.now += 20 * 60 - 1
        self.watch.tick()
        self.assertEqual(self.records, [])
        # A team_wait of 30 minutes: no time without a team call passes while it runs.
        self.watch.team_call("wait-1")
        self.clock.now += 30 * 60
        self.watch.tick()
        self.watch.team_call("wait-1", finished=True)
        self.clock.now += 19 * 60
        self.watch.tick()
        # The goal waits for the critic (or a pause): no time passes either.
        self.watch.tick(active=False)
        self.clock.now += 3600
        self.watch.tick(active=False)
        self.watch.tick()
        self.assertEqual(self.records, [])
        self.clock.now += 20 * 60
        self.watch.tick()
        warning, = self.warnings("teamless")
        self.assertIn("made 0 model calls in 20 min without calling any team tool", warning["text"])
        # A turn that ended clears its calls that never ended (an interrupted team_wait).
        self.watch.team_call("wait-2")
        self.watch.turn_ended()
        self.assertEqual(self.watch.running, set())

    def test_a_team_call_that_runs_holds_the_count_of_model_calls(self):
        self.watch.team_call("wait-1")
        for _ in range(12):
            self.watch.model_call()
        self.assertEqual(self.warnings(), [])

    def test_a_refused_team_call_warns_until_a_team_call_reaches_tcs_prover(self):
        self.watch.refused("unsupported call: mcp__tcs__team_members")
        self.watch.refused("unsupported call: mcp__tcs__team_assign")
        warning, = self.warnings("refused")
        self.assertEqual((warning["label"], warning["needsAttention"]), ("Team tool call refused", True))
        self.assertEqual(warning["text"], (
            "Codex refused a team tool call of the coordinator (unsupported call: mcp__tcs__team_members): the call "
            "never reached TCS Prover's team, so its work is not delegated. The run goes on, and the job is listed "
            "under Needs your attention until a team tool call of the coordinator works."))
        self.watch.team_call("call-1", finished=True)
        self.assertEqual(self.cleared(), ["refused"])

    def test_three_replies_that_name_the_proof_file_without_a_hand_in_warn_until_the_proof_is_handed_in(self):
        self.watch.untaken_handin()
        self.watch.untaken_handin()
        self.assertEqual(self.records, [])
        self.watch.untaken_handin()
        warning, = self.warnings("handin")
        self.assertEqual((warning["label"], warning["needsAttention"]), ("Proof not handed in", True))
        self.assertEqual(warning["text"], (
            "The coordinator has ended 3 turns with a reply that names its proof file while its goal stayed "
            "incomplete (it did not call update_goal with the status complete), so the proof was not handed to the "
            "critic. The run goes on, and the job is listed under Needs your attention until the coordinator hands "
            "its proof in (listed after 3 such replies)."))
        self.watch.untaken_handin()
        self.assertEqual(len(self.warnings()), 1)
        self.watch.handed_in()
        self.assertEqual(self.cleared(), ["handin"])
        # A team call does not hand the proof in; the count starts again after the hand-in.
        for _ in range(2):
            self.watch.untaken_handin()
        self.assertEqual(len(self.warnings()), 1)

    def test_without_a_team_only_the_hand_ins_are_watched(self):
        watch = runtime.CoordinatorWatch(lambda kind, **fields: self.records.append(fields), team=False,
                                         clock=self.clock)
        for _ in range(30):
            watch.model_call()
        self.clock.now += 3600
        watch.tick()
        self.assertEqual(self.records, [])
        for _ in range(3):
            watch.untaken_handin()
        self.assertEqual([record["coordinatorWarning"] for record in self.records], ["handin"])

    def test_codex_s_error_output_names_a_refused_team_tool_call(self):
        self.assertEqual([runtime.refused_team_call(line) for line in REFUSED_LINES],
                         ["unsupported call: mcp__tcs__team_members", "unsupported call: mcp__tcsteam_status",
                          "unsupported call: mcp__teamteam_wait"])
        # A refused call of another tool, and other error output, are no team calls.
        for line in ("2026-11-13T12:28:26.906033Z ERROR codex_core::tools::router: error=unsupported call: "
                     "shell_command", "Reading additional input from stdin...", ""):
            self.assertEqual(runtime.refused_team_call(line), "")


def usage(number, cached=0, inputs=1000):
    """Codex's thread/tokenUsage/updated after the thread's ``number``-th model request of ``inputs`` tokens."""

    last = {"inputTokens": inputs, "cachedInputTokens": cached, "outputTokens": 10, "reasoningOutputTokens": 0,
            "totalTokens": inputs + 10}
    total = {key: value * number for key, value in last.items()}
    return goal_test.event("thread/tokenUsage/updated", turnId="turn-1", tokenUsage={"last": last, "total": total})


def team_item(method, tool="team_members", identity="call-1"):
    return goal_test.event(method, item={"type": "mcpToolCall", "id": identity, "server": "tcs", "tool": tool,
                                         "status": "completed" if method == "item/completed" else "inProgress"})


class SessionTests(unittest.TestCase):
    """The Codex coordinator's goal session reports to the watch as Codex's app-server works."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        for patcher in (mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()),
                        mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = team.Service()
        self.addCleanup(self.service.close)
        self.crew = team.Team([], prompts=team_test.TEXTS, directory=str(self.directory), attempt=None,
                              configuration=None, emit=lambda kind, **fields: None, render=None, retry_note="")

    def session(self, double, crew=True, coordinator=True):
        double.service = self.service
        session = runtime.goal_session(
            double, "Coordinate.", prompts=team_test.GOAL_PROMPTS,
            settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"},
            features=[], coordinator=coordinator, texts=team_test.TEXTS, service=self.service,
            team=self.crew if crew else None, options={"goal_cwd": str(self.directory), "goal_loop": "tcs"})
        self.addCleanup(session.close)
        return session

    def done(self, rpc):
        self.service.call("update_goal", {"status": "complete"})
        rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

    @staticmethod
    def labelled(double, label):
        return [event for event in double.events if event.get("label") == label]

    def test_ten_model_calls_without_a_team_call_are_listed_and_the_next_team_call_clears_it(self):
        def quiet(rpc):
            rpc.messages.extend([usage(number) for number in range(1, 11)])
            rpc.messages.extend([goal_test.answer("Working alone."), goal_test.completed()])

        def team_turn(rpc):
            rpc.messages.extend([team_item("item/started"), team_item("item/completed"), usage(11)])
            self.done(rpc)

        double = team_test.LoopDouble([quiet, team_turn])
        self.assertEqual(next(self.session(double))["output"], POINTER)
        warning, = self.labelled(double, "Coordinator works without its team")
        self.assertEqual((warning["kind"], warning["coordinatorWarning"], warning["modelCalls"]),
                         ("diagnostic", "teamless", 10))
        cleared, = self.labelled(double, "Coordinator warning cleared")
        self.assertEqual(cleared["coordinatorWarningCleared"], "teamless")
        labels = [event.get("label") for event in double.events]
        self.assertLess(labels.index("Coordinator works without its team"), labels.index("Coordinator warning cleared"))

    def test_a_team_call_that_runs_holds_the_clock_and_one_cut_short_ends_with_its_turn(self):
        # 0.4 s stand for the 20 minutes: a team_wait of 1.2 s is no time without a team call.
        def started(rpc):
            rpc.messages.append(team_item("item/started", tool="team_wait", identity="wait-1"))

        def finished(rpc):
            time.sleep(1.2)
            rpc.messages.extend([team_item("item/completed", tool="team_wait", identity="wait-1"),
                                 team_item("item/started", tool="team_wait", identity="wait-2"),
                                 goal_test.answer("Waiting."), goal_test.completed()])

        def quiet(rpc):
            # wait-2 never completed (its turn ended): ten model calls now are ten without a team call.
            rpc.messages.extend([usage(number) for number in range(1, 11)])
            self.done(rpc)

        double = team_test.LoopDouble([started, finished, quiet])
        with mock.patch.object(runtime, "COORDINATOR_TEAMLESS_SECONDS", 0.4):
            next(self.session(double))
        warning, = self.labelled(double, "Coordinator works without its team")
        self.assertEqual(warning["modelCalls"], 10)

    def test_a_repeated_token_count_is_no_model_call(self):
        def turn(rpc):
            rpc.messages.extend([usage(number) for number in range(1, 10)] + [usage(9)] * 5)
            self.done(rpc)

        double = team_test.LoopDouble([turn])
        next(self.session(double))
        self.assertEqual(self.labelled(double, "Coordinator works without its team"), [])

    def test_a_team_tool_call_codex_refused_is_listed_until_a_team_call_works(self):
        def refused(rpc):
            for line in REFUSED_LINES:
                double.rpc.stderr_watch(line)
            rpc.messages.extend([goal_test.answer("The tools are missing."), goal_test.completed()])

        def team_turn(rpc):
            rpc.messages.extend([team_item("item/started"), team_item("item/completed")])
            self.done(rpc)

        double = team_test.LoopDouble([refused, team_turn])
        next(self.session(double))
        warning, = self.labelled(double, "Team tool call refused")
        self.assertIn("(unsupported call: mcp__tcs__team_members)", warning["text"])
        self.assertEqual(warning["coordinatorWarning"], "refused")
        self.assertEqual([event["coordinatorWarningCleared"] for event in self.labelled(
            double, "Coordinator warning cleared")], ["refused"])

    def test_three_replies_with_the_proof_file_but_no_update_goal_are_listed_until_the_hand_in(self):
        def pointer(rpc):
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double = team_test.LoopDouble([pointer, pointer, pointer, self.done])
        self.assertEqual(next(self.session(double))["output"], POINTER)
        warning, = self.labelled(double, "Proof not handed in")
        self.assertIn("ended 3 turns with a reply that names its proof file", warning["text"])
        labels = [event.get("label") for event in double.events]
        self.assertLess(labels.index("Proof not handed in"), labels.index("Coordinator warning cleared"))
        self.assertLess(labels.index("Coordinator warning cleared"), labels.index("Goal complete"))

    def test_two_such_replies_are_not_listed(self):
        def pointer(rpc):
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double = team_test.LoopDouble([pointer, pointer, self.done])
        next(self.session(double))
        self.assertEqual(self.labelled(double, "Proof not handed in"), [])

    def test_an_author_working_alone_gets_no_warning(self):
        def quiet(rpc):
            rpc.messages.extend([usage(number) for number in range(1, 13)])
            for line in REFUSED_LINES:
                double.rpc.stderr_watch(line) if getattr(double.rpc, "stderr_watch", None) else None
            rpc.messages.extend([goal_test.answer(POINTER), goal_test.completed()])

        double = team_test.LoopDouble([quiet, quiet, quiet, self.done])
        next(self.session(double, crew=False, coordinator=False))
        self.assertFalse([event for event in double.events if event.get("coordinatorWarning")])
        self.assertIsNone(getattr(double.rpc, "stderr_watch", None))

    def test_the_author_cache_usage_covers_every_model_request_of_the_turn(self):
        def turn(rpc):
            rpc.messages.extend([usage(1, cached=0, inputs=20000), usage(2, cached=19000, inputs=20000),
                                 usage(2, cached=19000, inputs=20000), usage(3, cached=19500, inputs=20000)])
            self.done(rpc)

        double = team_test.LoopDouble([turn])
        double.emit_cache_usage = runtime.emit_cache_usage  # The runner's own, which records through emit().
        with mock.patch.object(runtime, "emit", side_effect=double.emit):
            next(self.session(double))
        line, = self.labelled(double, "Author cache usage")
        # 3 requests (one count repeated): 60,000 input tokens of which 38,500 cached, not the last request's alone.
        self.assertEqual((line["inputTokens"], line["cachedInputTokens"], line["modelRequests"]), (60000, 38500, 3))
        self.assertEqual(line["text"], "38,500 of 60,000 input tokens cached (64.2%) in the turn's 3 model "
                                       "requests; 0 cache-write tokens.")


class ClaudeTests(team_test.TeamCase):
    """A Claude coordinator against the stand-in CLI: one assistant message ID per model request."""

    coordinator = team_test.ClaudeCoordinatorTests.coordinator
    logged = team_test.ClaudeCoordinatorTests.logged
    results = team_test.ClaudeCoordinatorTests.results

    @staticmethod
    def answer(number, block=0):
        return {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": f"a{number}-{block}", "message": {
            "id": f"msg_{number}", "content": [{"type": "text", "text": f"Working alone ({number}, {block})."}]}}}

    def test_ten_answers_without_a_team_call_are_listed_and_a_team_call_clears_it(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        # 10 answers, each in two messages (two content blocks of one request), then a team call.
        quiet = [self.answer(number, block) for number in range(1, 11) for block in (0, 1)]
        _, session = self.coordinator(crew, [[
            "init", "echo", *quiet, {"mcp_call": {"name": "mcp__tcs__team_members"}},
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]])
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            self.assertEqual(next(session)["output"], POINTER)
        warning, = self.labelled("Coordinator works without its team")
        self.assertEqual((warning["coordinatorWarning"], warning["modelCalls"]), ("teamless", 10))
        cleared, = self.labelled("Coordinator warning cleared")
        self.assertEqual(cleared["coordinatorWarningCleared"], "teamless")

    def test_a_team_wait_that_runs_holds_the_clock(self):
        # 0.4 s stand for the 20 minutes: the member works for 1.2 s while team_wait waits for it.
        release = self.held_member()
        threading.Timer(1.2, release.set).start()
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        _, session = self.coordinator(crew, [[
            "init", "echo",
            {"mcp_call": {"name": "mcp__tcs__team_assign", "arguments": {"member": 1, "assignment": "Lemma A"}}},
            {"mcp_call": {"name": "mcp__tcs__team_wait", "arguments": {"timeout_minutes": 1}}},
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]])
        with mock.patch.object(runtime, "COORDINATOR_TEAMLESS_SECONDS", 0.4), \
                mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            self.assertEqual(next(session)["output"], POINTER)
        self.assertIn("Done: ", self.results()["mcp__tcs__team_wait"]["content"][0]["text"])
        self.assertEqual(self.labelled("Coordinator works without its team"), [])

    def test_nine_answers_in_eighteen_messages_are_not_listed(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}], coordinator="claude-opus-5-5")
        _, session = self.coordinator(crew, [[
            "init", "echo", *[self.answer(number, block) for number in range(1, 10) for block in (0, 1)],
            {"structured": {"goal_status": "complete", "final_answer": POINTER, "progress_note": ""}}]])
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True):
            next(session)
        self.assertEqual(self.labelled("Coordinator works without its team"), [])


class ServerTests(jobs_test.Isolated):
    """The server lists a running job's coordinator warning under Needs your attention until it is cleared."""

    def running(self):
        app = self.paused_job()
        app.state.update({"phase": "running", "stage": "solve", "activeNode": "author"})
        return app

    @staticmethod
    def warning(kind="teamless", label="Coordinator works without its team", text="The coordinator has made 10 model "
                "calls in 2 min without calling any team tool."):
        return {"kind": "diagnostic", "stage": "solve", "node": "author", "label": label, "text": text,
                "coordinatorWarning": kind, "needsAttention": True, "threadId": "thread-1"}

    @staticmethod
    def cleared(kind="teamless"):
        return {"kind": "status", "stage": "solve", "node": "author", "label": "Coordinator warning cleared",
                "coordinatorWarningCleared": kind, "text": "The coordinator called a team tool again."}

    def test_a_warning_is_listed_while_the_job_runs_and_its_status_clears_it(self):
        app = self.running()
        self.assertIsNone(app.attention())
        app._follow_coordinator_warnings(app.add_trace(self.warning()))
        attention = app.attention()
        self.assertEqual(attention, {"kind": "coordinator", "warning": "teamless", "retrying": True,
                                     "title": "Coordinator works without its team",
                                     "text": "The coordinator has made 10 model calls in 2 min without calling any "
                                             "team tool."})
        self.assertEqual(app.snapshot()["needsAttention"]["kind"], "coordinator")
        # The newest of two warnings is shown; clearing it shows the other.
        app._follow_coordinator_warnings(app.add_trace(self.warning("refused", "Team tool call refused", "Refused.")))
        self.assertEqual(app.attention()["title"], "Team tool call refused")
        app._follow_coordinator_warnings(app.add_trace(self.cleared("refused")))
        self.assertEqual(app.attention()["warning"], "teamless")
        app._follow_coordinator_warnings(app.add_trace(self.cleared()))
        self.assertIsNone(app.attention())
        # The warning and its end stay pinned in the activity log.
        self.assertTrue(server.important_record(self.warning()))
        self.assertTrue(server.important_record(self.cleared()))

    def test_a_more_pressing_cause_comes_first_and_a_job_that_is_not_running_is_not_listed_for_it(self):
        app = self.running()
        app._follow_coordinator_warnings(app.add_trace(self.warning()))
        app.state["phase"] = "paused"
        self.assertNotEqual((app.attention() or {}).get("kind"), "coordinator")
        app.state["phase"] = "running"
        app._follow_failures({"kind": "status", "stage": "solve", "label": runtime.RETRY_LABEL,
                              "text": "Model call failed (usage limit reached).", "retryAt": 2e9,
                              "failure": "usage limit reached", "retryAttempt": 2})
        self.assertEqual(app.attention()["kind"], "usage")

    def test_the_runner_s_records_reach_it_and_its_end_clears_the_warnings(self):
        app = self.running()
        seen = []

        def lines():
            yield json.dumps(self.warning()) + "\n"
            seen.append(app.attention())
            yield json.dumps({"kind": "workflow_result", "stage": "workflow", "output": "done"}) + "\n"

        token = app.active_token = app.worker_token = object()
        process = app.process = mock.Mock(stdout=lines())
        process.wait.return_value = process.poll.return_value = 0
        app._read_output(process, token)
        self.assertEqual(seen[0]["kind"], "coordinator")
        self.assertEqual(app._coordinator_warnings, {})


if __name__ == "__main__":
    unittest.main()
