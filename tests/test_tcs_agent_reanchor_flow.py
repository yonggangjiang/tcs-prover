import copy
import os
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import tcs_agent


ORIGINAL_PROMPT = "IMMUTABLE-GUIDANCE-\u03a9\nSolve exactly the supplied theorem."
EXACT_STATEMENT = "STATEMENT-\u03b2\nProve the exact quantified claim."
MEMORY_SNAPSHOT = '{"durableMemory":"MEMORY-\u03bb"}'


def item_event(method, thread_id, turn_id, item_type, item_id, text=None):
    item = {"type": item_type, "id": item_id}
    if text is not None:
        item["text"] = text
    return {
        "method": method,
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": item,
        },
    }


def goal_event(status):
    return {
        "method": "thread/goal/updated",
        "params": {
            "threadId": "root-thread",
            "goal": {"threadId": "root-thread", "status": status},
        },
    }


def turn_completed(turn_id):
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "root-thread",
            "turn": {"id": turn_id, "status": "completed"},
        },
    }


class FakeProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


class ReanchorFlowTests(unittest.TestCase):
    def run_scripted_goal(
        self, events, reports, on_critic=None, on_read=None, **goal_options,
    ):
        # Most re-anchor tests exercise provider-independent author behavior.
        goal_options.setdefault("author_model", "gpt-5.6-sol")
        goal_options.setdefault("critic_model", "gpt-5.6-sol")
        goal_options.setdefault("writer_model", "gpt-5.6-sol")
        holder = {}
        traces = []
        critic_calls = []
        process = FakeProcess()

        class FakeRPC:
            def __init__(self, supplied_process, record):
                self.process = supplied_process
                self.record = record
                self.events = deque(copy.deepcopy(events))
                self.calls = []
                self.requests = []
                self.turn_number = 0
                self.request_number = 100
                self.closed = False
                holder["rpc"] = self

            def call(self, method, params):
                self.calls.append((method, copy.deepcopy(params)))
                if method == "thread/start":
                    return {"thread": {"id": "root-thread"}}
                if method == "turn/start":
                    self.turn_number += 1
                    return {"turn": {"id": f"turn-{self.turn_number}"}}
                return {}

            def request(self, method, params):
                self.request_number += 1
                self.requests.append((method, copy.deepcopy(params)))
                return self.request_number

            def send(self, message):
                self.calls.append(("notification", copy.deepcopy(message)))

            def read(self):
                if on_read is not None:
                    on_read(self)
                if not self.events:
                    raise AssertionError("The fake app-server event script was exhausted.")
                message = self.events.popleft()
                self.record(message)
                return message

            def close(self):
                self.closed = True

        report_queue = deque(copy.deepcopy(reports))

        def fake_criticize(statement, solution, round_number, **options):
            critic_calls.append((statement, solution, round_number, options))
            if on_critic is not None:
                on_critic(holder["rpc"], len(critic_calls))
            if not report_queue:
                raise AssertionError("The fake critic report script was exhausted.")
            return report_queue.popleft()

        def capture_emit(kind, stage, **fields):
            traces.append({"kind": kind, "stage": stage, **copy.deepcopy(fields)})

        def fake_popen(command, **options):
            holder["command"] = copy.deepcopy(command)
            holder["process_options"] = options
            process.launch_command = copy.deepcopy(command)
            process.launch_options = copy.deepcopy(options)
            return process

        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(tcs_agent.Path, "cwd", return_value=Path(folder)),
                patch.object(tcs_agent, "codex", return_value="codex-fake"),
                patch.object(tcs_agent, "context_cache_arguments", return_value=[]),
                patch.object(tcs_agent.subprocess, "Popen", side_effect=fake_popen),
                patch.object(tcs_agent, "RPC", FakeRPC),
                patch.object(tcs_agent, "criticize", side_effect=fake_criticize),
                patch.object(tcs_agent, "finalize", return_value="FINAL-LATEX"),
                patch.object(tcs_agent, "emit", side_effect=capture_emit),
                patch.object(
                    tcs_agent.AuthorMemory,
                    "snapshot",
                    return_value=MEMORY_SNAPSHOT,
                ),
            ):
                result = tcs_agent.run_goal(
                    ORIGINAL_PROMPT,
                    EXACT_STATEMENT,
                    critic_rounds=3,
                    thinking_hours=1,
                    speed="standard",
                    **goal_options,
                )

        self.assertFalse(report_queue, "Every scripted critic report should be used.")
        return result, holder["rpc"], traces, critic_calls

    def test_live_instruction_steers_the_active_author_turn(self):
        events = [
            item_event(
                "item/completed", "root-thread", "turn-1", "agentMessage",
                "answer-1", "CANDIDATE",
            ),
            goal_event("complete"),
            turn_completed("turn-1"),
        ]
        reports = [{
            "verdict": "pass", "fixed": False,
            "solution": "CANDIDATE", "bugs": "",
        }]

        def wait_for_steer(rpc):
            deadline = time.monotonic() + 1
            while (
                not any(method == "turn/steer" for method, _ in rpc.requests)
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "author-steer.json"
            path.write_text(
                '{"id":"live-1","instruction":"Stop experiments now."}',
                encoding="utf-8",
            )
            with patch.object(tcs_agent, "AUTHOR_STEER_POLL_SECONDS", 0.001):
                result, rpc, traces, _ = self.run_scripted_goal(
                    events, reports, on_read=wait_for_steer,
                    author_steer_file=path,
                )

        self.assertEqual(result, "FINAL-LATEX")
        steers = [
            params for method, params in rpc.requests
            if method == "turn/steer"
        ]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["expectedTurnId"], "turn-1")
        self.assertEqual(
            steers[0]["input"],
            [{"type": "text", "text": "Stop experiments now."}],
        )
        self.assertTrue(any(
            trace.get("label") == "Live author instruction sent"
            for trace in traces
        ))

    def test_deepseek_author_keeps_goal_and_multi_agent_app_server(self):
        events = [
            item_event(
                "item/completed", "root-thread", "turn-1", "agentMessage",
                "answer-1", "DEEPSEEK-CANDIDATE",
            ),
            goal_event("complete"),
            turn_completed("turn-1"),
        ]
        reports = [{
            "verdict": "pass",
            "fixed": False,
            "solution": "DEEPSEEK-CANDIDATE",
            "bugs": "",
        }]
        with patch.dict(
            os.environ, {tcs_agent.DEEPSEEK_KEY_ENV: "test-key"}, clear=False,
        ):
            result, rpc, traces, _ = self.run_scripted_goal(
                events, reports, author_model=tcs_agent.DEEPSEEK_MODEL,
            )

        self.assertEqual(result, "FINAL-LATEX")
        command = next(
            trace for trace in traces
            if trace.get("label") == "Exact solve input"
        )
        self.assertEqual(command["modelProvider"], tcs_agent.DEEPSEEK_PROVIDER)
        self.assertEqual(command["reasoningEffort"], "max")
        self.assertEqual(command["reasoningSummary"], "concise")
        self.assertEqual(command["serviceTier"], "standard")

        launch = rpc.process.launch_command
        self.assertEqual(launch[:2], ["codex-fake", "app-server"])
        self.assertIn("multi_agent", launch)
        self.assertIn('model_provider="deepseek"', launch)
        self.assertIn(
            'model_providers.deepseek.wire_api="responses"', launch,
        )
        self.assertIn(
            'model_providers.deepseek.env_key='
            '"TCS_PROVER_DEEPSEEK_TOKEN"',
            launch,
        )
        self.assertIn('forced_login_method="api"', launch)
        self.assertEqual(
            rpc.process.launch_options["env"][tcs_agent.DEEPSEEK_TOKEN_ENV],
            "test-key",
        )
        self.assertEqual(
            rpc.process.launch_options["env"]["OPENAI_API_KEY"],
            tcs_agent.CUSTOM_PROVIDER_LOGIN_PLACEHOLDER,
        )
        self.assertNotIn(
            tcs_agent.DEEPSEEK_KEY_ENV, rpc.process.launch_options["env"],
        )
        self.assertNotIn("test-key", " ".join(launch))

        thread_start = next(
            params for method, params in rpc.calls if method == "thread/start"
        )
        self.assertEqual(thread_start["model"], tcs_agent.DEEPSEEK_MODEL)
        self.assertEqual(
            thread_start["modelProvider"], tcs_agent.DEEPSEEK_PROVIDER,
        )
        self.assertEqual(thread_start["config"]["model_reasoning_effort"], "max")
        self.assertEqual(
            thread_start["config"]["model_reasoning_summary"], "concise"
        )
        self.assertFalse(thread_start["config"]["features"]["fast_mode"])

        goal_calls = [
            params for method, params in rpc.calls if method == "thread/goal/set"
        ]
        self.assertEqual(
            [params["status"] for params in goal_calls[:2]],
            ["paused", "active"],
        )

    def test_completed_root_author_compaction_steers_exactly_once(self):
        root_compaction = item_event(
            "item/completed",
            "root-thread",
            "turn-1",
            "contextCompaction",
            "compact-root",
        )
        events = [
            item_event(
                "item/started",
                "root-thread",
                "turn-1",
                "contextCompaction",
                "compact-start-only",
            ),
            root_compaction,
            copy.deepcopy(root_compaction),
            item_event(
                "item/completed",
                "child-thread",
                "child-turn",
                "contextCompaction",
                "compact-child",
            ),
            item_event(
                "item/completed",
                "root-thread",
                "turn-1",
                "agentMessage",
                "answer-1",
                "INITIAL-CANDIDATE",
            ),
            goal_event("complete"),
            turn_completed("turn-1"),
        ]

        def emit_non_author_compaction(rpc, call_number):
            self.assertEqual(call_number, 1)
            rpc.record(item_event(
                "item/completed",
                "root-thread",
                "critic-turn",
                "contextCompaction",
                "compact-while-author-inactive",
            ))

        result, rpc, traces, _ = self.run_scripted_goal(
            events,
            [{
                "verdict": "pass",
                "fixed": False,
                "solution": "INITIAL-CANDIDATE",
                "bugs": "",
            }],
            on_critic=emit_non_author_compaction,
        )

        self.assertEqual(result, "FINAL-LATEX")
        steers = [params for method, params in rpc.requests if method == "turn/steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["threadId"], "root-thread")
        self.assertEqual(steers[0]["expectedTurnId"], "turn-1")
        self.assertEqual(len(steers[0]["input"]), 1)
        anchored = steers[0]["input"][0]["text"]
        self.assertIn(ORIGINAL_PROMPT, anchored)
        self.assertIn(EXACT_STATEMENT, anchored)
        self.assertIn(MEMORY_SNAPSHOT, anchored)

        reanchors = [
            trace for trace in traces
            if trace.get("label") == "Author context re-anchor after compaction"
        ]
        self.assertEqual(len(reanchors), 1)
        self.assertEqual(reanchors[0].get("compactionId"), "compact-root")

    def test_explicit_continuation_turn_is_self_contained(self):
        events = [
            item_event(
                "item/completed",
                "root-thread",
                "turn-1",
                "agentMessage",
                "partial-answer",
                "PARTIAL-CANDIDATE",
            ),
            goal_event("blocked"),
            turn_completed("turn-1"),
            item_event(
                "item/completed",
                "root-thread",
                "turn-2",
                "agentMessage",
                "complete-answer",
                "COMPLETE-CANDIDATE",
            ),
            goal_event("complete"),
            turn_completed("turn-2"),
        ]
        result, rpc, _, _ = self.run_scripted_goal(
            events,
            [{
                "verdict": "pass",
                "fixed": False,
                "solution": "COMPLETE-CANDIDATE",
                "bugs": "",
            }],
        )

        self.assertEqual(result, "FINAL-LATEX")
        turns = [params for method, params in rpc.calls if method == "turn/start"]
        self.assertEqual(len(turns), 2)
        continuation = turns[1]["input"][0]["text"]
        self.assertIn(ORIGINAL_PROMPT, continuation)
        self.assertIn(EXACT_STATEMENT, continuation)
        self.assertIn(MEMORY_SNAPSHOT, continuation)
        self.assertIn(tcs_agent.CONTINUE_PROMPT, continuation)

    def test_critic_rejection_turn_is_self_contained(self):
        events = [
            item_event(
                "item/completed",
                "root-thread",
                "turn-1",
                "agentMessage",
                "initial-answer",
                "INITIAL-CANDIDATE",
            ),
            goal_event("complete"),
            turn_completed("turn-1"),
            item_event(
                "item/completed",
                "root-thread",
                "turn-2",
                "agentMessage",
                "revision-answer",
                "REVISED-CANDIDATE",
            ),
            turn_completed("turn-2"),
        ]
        reports = [
            {
                "verdict": "reject",
                "fixed": False,
                "solution": "CRITIC-SAFE-CANDIDATE",
                "bugs": "BUG-OBLIGATION-\u03c0",
                "memory_update": {
                    "approach_family": "family-a",
                    "approach_result": "failed at obligation pi",
                    "blocked_routes": [],
                    "unresolved_obligations": ["BUG-OBLIGATION-\u03c0"],
                },
            },
            {
                "verdict": "pass",
                "fixed": False,
                "solution": "REVISED-CANDIDATE",
                "bugs": "",
            },
        ]
        result, rpc, _, _ = self.run_scripted_goal(events, reports)

        self.assertEqual(result, "FINAL-LATEX")
        turns = [params for method, params in rpc.calls if method == "turn/start"]
        self.assertEqual(len(turns), 2)
        rejection = turns[1]["input"][0]["text"]
        self.assertIn(ORIGINAL_PROMPT, rejection)
        self.assertIn(EXACT_STATEMENT, rejection)
        self.assertIn(MEMORY_SNAPSHOT, rejection)
        self.assertIn("CRITIC-SAFE-CANDIDATE", rejection)
        self.assertIn("BUG-OBLIGATION-\u03c0", rejection)


if __name__ == "__main__":
    unittest.main()
