"""Small offline tests for the two Codex actions."""

import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tcs_agent as agent


class AgentTests(unittest.TestCase):
    """Check local logic without calling the subscription."""

    def test_standard_streams_use_fault_tolerant_utf8(self):
        streams = [mock.Mock(), mock.Mock(), mock.Mock()]
        with mock.patch.object(agent.sys, "stdin", streams[0]), mock.patch.object(
            agent.sys, "stdout", streams[1]
        ), mock.patch.object(agent.sys, "stderr", streams[2]):
            agent.configure_standard_streams()
        for stream in streams:
            stream.reconfigure.assert_called_once_with(
                encoding="utf-8", errors="replace"
            )

    def test_empty_statement_is_rejected(self):
        with self.assertRaises(agent.Error):
            agent.text("  ")

    def test_prompt_replaces_one_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "prompt.txt"
            template.write_text("Before [STATEMENT] after", encoding="utf-8")
            with mock.patch.object(agent, "TEMPLATE", template):
                self.assertEqual(agent.make_prompt("Problem"), "Before Problem after")

    def test_prompt_needs_exactly_one_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "prompt.txt"
            template.write_text("No marker", encoding="utf-8")
            with mock.patch.object(agent, "TEMPLATE", template):
                with self.assertRaises(agent.Error):
                    agent.make_prompt("Problem")

    def test_first_review_uses_terra_ultra_and_reads_json(self):
        process = SimpleNamespace(
            stdin=mock.Mock(),
            stdout=io.StringIO(
                '{"type":"item.completed","item":{"type":"reasoning","text":"Checked cases."}}\n'
            ),
            wait=mock.Mock(return_value=0),
            poll=mock.Mock(return_value=0),
        )

        def fake_popen(command, **_):
            answer = Path(command[command.index("-o") + 1])
            answer.write_text(
                json.dumps({"statement": "Rigorous", "notes": "Looks sound."}),
                encoding="utf-8",
            )
            return process

        output = io.StringIO()
        with mock.patch.object(agent, "codex", return_value="codex"), mock.patch.object(
            agent.subprocess, "Popen", side_effect=fake_popen
        ) as popen, mock.patch("sys.stdout", output):
            self.assertEqual(agent.review("draft")["statement"], "Rigorous")
        command = popen.call_args.args[0]
        self.assertIn(agent.REVIEW_MODEL, command)
        self.assertIn(
            f'model_reasoning_effort="{agent.REVIEW_EFFORT}"', command
        )
        self.assertIn(f'service_tier="{agent.SERVICE_TIER}"', command)
        self.assertIn("fast_mode", command)
        self.assertIn('model_reasoning_summary="detailed"', command)
        self.assertIn("--json", command)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]["kind"], "request")
        self.assertTrue(records[0]["text"].endswith("DRAFT:\ndraft"))
        self.assertEqual(records[0]["model"], agent.REVIEW_MODEL)
        self.assertEqual(records[0]["reasoningEffort"], agent.REVIEW_EFFORT)
        self.assertEqual(records[0]["serviceTier"], "fast")
        self.assertEqual(records[-1]["kind"], "review_result")

    def test_feedback_prompt_changes_only_rewrite_to_revise(self):
        prompt = agent.review_prompt("Original claim", "Keep the time bound.")
        self.assertEqual(agent.REVIEW_PROMPT.count("Rewrite"), 1)
        self.assertEqual(
            agent.REVISION_PROMPT,
            agent.REVIEW_PROMPT.replace("Rewrite", "Revise", 1),
        )
        self.assertTrue(prompt.startswith(agent.REVISION_PROMPT))
        self.assertIn("CURRENT CHECKED STATEMENT:\nOriginal claim", prompt)
        self.assertNotIn("\n\nDRAFT:", prompt)
        self.assertTrue(prompt.endswith(
            "AUTHOR REVISION REQUEST:\n"
            "Keep the time bound."
        ))

    def test_custom_review_and_author_prompts_are_used(self):
        self.assertTrue(
            agent.review_prompt("Claim", instructions="Rewrite carefully.")
            .startswith("Rewrite carefully.")
        )
        self.assertEqual(
            agent.review_prompt(
                "Claim", "Keep n.", instructions="Rewrite carefully."
            ).splitlines()[0],
            "Revise carefully.",
        )
        self.assertEqual(
            agent.make_prompt("Claim", "Custom [STATEMENT] prompt"),
            "Custom Claim prompt",
        )

    def test_feedback_revision_uses_terra_ultra(self):
        report = {"statement": "Revised", "notes": ""}
        with mock.patch.object(
            agent, "structured", return_value=(report, json.dumps(report))
        ) as structured, mock.patch("sys.stdout", io.StringIO()):
            agent.review("Checked", "Clarify the parameter.")
        self.assertEqual(structured.call_args.kwargs, {
            "model": agent.REVIEW_MODEL,
            "effort": agent.REVIEW_EFFORT,
            "speed": agent.DEFAULT_SPEED,
        })

    def test_user_can_choose_sol_for_statement_review(self):
        report = {"statement": "Reviewed", "notes": ""}
        with mock.patch.object(
            agent, "structured", return_value=(report, json.dumps(report))
        ) as structured, mock.patch("sys.stdout", io.StringIO()):
            agent.review("Draft", model="gpt-5.6-sol")
        self.assertEqual(structured.call_args.kwargs, {
            "model": "gpt-5.6-sol",
            "effort": agent.REVIEW_EFFORT,
            "speed": agent.DEFAULT_SPEED,
        })

    def test_user_can_choose_luna_for_statement_review(self):
        report = {"statement": "Reviewed", "notes": ""}
        with mock.patch.object(
            agent, "structured", return_value=(report, json.dumps(report))
        ) as structured, mock.patch("sys.stdout", io.StringIO()):
            agent.review("Draft", model="gpt-5.6-luna")
        self.assertEqual(structured.call_args.kwargs["model"], "gpt-5.6-luna")
        self.assertEqual(structured.call_args.kwargs["effort"], "ultra")

    def test_user_can_choose_a_lower_reasoning_effort(self):
        report = {"statement": "Reviewed", "notes": ""}
        with mock.patch.object(
            agent, "structured", return_value=(report, json.dumps(report))
        ) as structured, mock.patch("sys.stdout", io.StringIO()):
            agent.review("Draft", model="gpt-5.6-sol", effort="high")
        self.assertEqual(structured.call_args.kwargs, {
            "model": "gpt-5.6-sol", "effort": "high",
            "speed": agent.DEFAULT_SPEED,
        })

    def test_unknown_review_model_is_rejected_before_codex_runs(self):
        with mock.patch.object(agent, "structured") as structured:
            with self.assertRaises(agent.Error):
                agent.review("Draft", model="unknown")
        structured.assert_not_called()

    def test_private_reasoning_content_is_removed(self):
        event = agent.public_event({
            "type": "reasoning",
            "summary": ["Visible summary"],
            "content": ["Private reasoning"],
            "encrypted_content": "opaque",
        })
        self.assertEqual(event, {"type": "reasoning", "summary": ["Visible summary"]})

    def test_raw_reasoning_delta_is_not_recorded(self):
        event = {
            "method": "item/reasoning/textDelta",
            "params": {"itemId": "reasoning-1", "delta": "Private reasoning"},
        }
        self.assertIsNone(agent.public_event(event))

    def test_rpc_keeps_notifications_while_waiting_for_a_reply(self):
        process = SimpleNamespace(
            stdin=io.StringIO(),
            stdout=io.StringIO(
                '{"method":"notice","params":{}}\n'
                '{"id":1,"result":{"ok":true}}\n'
            ),
        )
        rpc = agent.RPC(process)
        self.assertEqual(rpc.call("test", {}), {"ok": True})
        self.assertEqual(rpc.read()["method"], "notice")

    def test_rpc_records_then_rejects_an_interactive_request(self):
        messages = []
        process = SimpleNamespace(
            stdin=io.StringIO(),
            stdout=io.StringIO('{"id":7,"method":"item/tool/requestUserInput","params":{}}\n'),
        )
        rpc = agent.RPC(process, messages.append)
        with self.assertRaisesRegex(agent.Error, "Interactive Codex request"):
            rpc.wire()
        self.assertEqual(messages[0]["id"], 7)

    def test_stop_process_kills_a_child_that_ignores_terminate(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("codex", 5), 0]
        agent.stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_main_splits_web_feedback_from_the_statement(self):
        with mock.patch("sys.argv", ["tcs_agent.py", "review"]), mock.patch(
            "sys.stdin", io.StringIO("claim\0clarify it")
        ), mock.patch.object(agent, "review") as review:
            self.assertEqual(agent.main(), 0)
        review.assert_called_once_with(
            "claim", "clarify it", agent.REVIEW_MODEL, agent.REVIEW_EFFORT,
            speed=agent.DEFAULT_SPEED,
        )

    def test_main_sends_the_ui_critic_limit_to_the_goal(self):
        with mock.patch("sys.argv", ["tcs_agent.py", "solve"]), mock.patch(
            "sys.stdin", io.StringIO("claim\0" + "7")
        ), mock.patch.object(
            agent, "make_prompt", return_value="FULL"
        ), mock.patch.object(agent, "run_goal") as run_goal:
            self.assertEqual(agent.main(), 0)
        run_goal.assert_called_once_with(
            "FULL", "claim", "7", agent.DEFAULT_AUTHOR_HOURS,
            agent.AUTHOR_MODEL, agent.CRITIC_MODEL, agent.WRITER_MODEL,
            agent.EFFORT, speed=agent.DEFAULT_SPEED,
        )

    def test_main_sends_the_ui_author_limit_to_the_goal(self):
        with mock.patch("sys.argv", ["tcs_agent.py", "solve"]), mock.patch(
            "sys.stdin", io.StringIO("claim\0" + "7\0" + "1.5")
        ), mock.patch.object(
            agent, "make_prompt", return_value="FULL"
        ), mock.patch.object(agent, "run_goal") as run_goal:
            self.assertEqual(agent.main(), 0)
        run_goal.assert_called_once_with(
            "FULL", "claim", "7", "1.5",
            agent.AUTHOR_MODEL, agent.CRITIC_MODEL, agent.WRITER_MODEL,
            agent.EFFORT, speed=agent.DEFAULT_SPEED,
        )

    def test_main_sends_each_selected_proof_model_to_the_goal(self):
        argv = [
            "tcs_agent.py", "solve",
            "--author-model", "gpt-5.6-terra",
            "--critic-model", "gpt-5.6-luna",
            "--writer-model", "gpt-5.6-sol",
        ]
        with mock.patch("sys.argv", argv), mock.patch(
            "sys.stdin", io.StringIO("claim")
        ), mock.patch.object(
            agent, "make_prompt", return_value="FULL"
        ), mock.patch.object(agent, "run_goal") as run_goal:
            self.assertEqual(agent.main(), 0)
        run_goal.assert_called_once_with(
            "FULL", "claim", agent.DEFAULT_CRITIC_ROUNDS,
            agent.DEFAULT_AUTHOR_HOURS,
            "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol",
            agent.EFFORT, speed=agent.DEFAULT_SPEED,
        )

    def test_main_sends_the_selected_reasoning_effort_to_every_role(self):
        argv = ["tcs_agent.py", "solve", "--reasoning-effort", "high"]
        with mock.patch("sys.argv", argv), mock.patch(
            "sys.stdin", io.StringIO("claim")
        ), mock.patch.object(
            agent, "make_prompt", return_value="FULL"
        ), mock.patch.object(agent, "run_goal") as run_goal:
            self.assertEqual(agent.main(), 0)
        self.assertEqual(run_goal.call_args.args[-1], "high")

    def test_main_sends_standard_speed_to_the_goal(self):
        argv = ["tcs_agent.py", "solve", "--speed", "standard"]
        with mock.patch("sys.argv", argv), mock.patch(
            "sys.stdin", io.StringIO("claim")
        ), mock.patch.object(
            agent, "make_prompt", return_value="FULL"
        ), mock.patch.object(agent, "run_goal") as run_goal:
            self.assertEqual(agent.main(), 0)
        self.assertEqual(run_goal.call_args.kwargs["speed"], "standard")

    def test_speed_arguments_switch_fast_mode_explicitly(self):
        self.assertEqual(
            agent.speed_arguments("fast"),
            ["-c", 'service_tier="fast"', "--enable", "fast_mode"],
        )
        self.assertEqual(
            agent.speed_arguments("standard"), ["--disable", "fast_mode"]
        )
        with self.assertRaises(agent.Error):
            agent.speed_arguments("turbo")

    def test_main_sends_independent_efforts_and_custom_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            author = folder / "author.txt"
            critic = folder / "critic.txt"
            final = folder / "final.txt"
            author.write_text("Solve [STATEMENT].", encoding="utf-8")
            critic.write_text("Custom critic.", encoding="utf-8")
            final.write_text("Custom final.", encoding="utf-8")
            argv = [
                "tcs_agent.py", "solve",
                "--author-effort", "medium",
                "--critic-effort", "high",
                "--writer-effort", "max",
                "--author-prompt-file", str(author),
                "--critic-prompt-file", str(critic),
                "--final-prompt-file", str(final),
            ]
            with mock.patch("sys.argv", argv), mock.patch(
                "sys.stdin", io.StringIO("claim")
            ), mock.patch.object(agent, "run_goal") as run_goal:
                self.assertEqual(agent.main(), 0)
        self.assertEqual(run_goal.call_args.args[0], "Solve claim.")
        self.assertEqual(run_goal.call_args.kwargs["author_effort"], "medium")
        self.assertEqual(run_goal.call_args.kwargs["critic_effort"], "high")
        self.assertEqual(run_goal.call_args.kwargs["writer_effort"], "max")
        self.assertEqual(run_goal.call_args.kwargs["critic_prompt"], "Custom critic.")
        self.assertEqual(run_goal.call_args.kwargs["final_prompt"], "Custom final.")

    def test_critic_limit_defaults_to_four_and_is_bounded(self):
        self.assertEqual(agent.critic_limit(agent.DEFAULT_CRITIC_ROUNDS), 4)
        for value in (0, 101, "not-a-number"):
            with self.subTest(value=value), self.assertRaises(agent.Error):
                agent.critic_limit(value)

    def test_author_limit_defaults_to_twenty_four_hours_and_is_bounded(self):
        self.assertEqual(agent.author_hours(agent.DEFAULT_AUTHOR_HOURS), 24)
        for value in (0, 169, "not-a-number"):
            with self.subTest(value=value), self.assertRaises(agent.Error):
                agent.author_hours(value)

    def test_blocked_author_is_continued_before_the_deadline(self):
        class FakeRPC:
            def __init__(self, _, record=None):
                self.record, self.calls, self.turns = record, [], 0
                self.events = iter([
                    {
                        "method": "thread/goal/updated",
                        "params": {
                            "threadId": "thread-1",
                            "goal": {"threadId": "thread-1", "status": "blocked"},
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    },
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1", "turnId": "turn-2",
                            "item": {"type": "agentMessage", "text": "Solution"},
                        },
                    },
                    {
                        "method": "thread/goal/updated",
                        "params": {
                            "threadId": "thread-1",
                            "goal": {"threadId": "thread-1", "status": "complete"},
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-2", "status": "completed"},
                        },
                    },
                ])

            def call(self, method, params):
                self.calls.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thread-1"}}
                if method == "turn/start":
                    self.turns += 1
                    return {"turn": {"id": f"turn-{self.turns}"}}
                return {}

            def request(self, method, params):
                self.calls.append((method, params))
                return len(self.calls)

            def send(self, _):
                pass

            def read(self):
                message = next(self.events)
                self.record(message)
                return message

            def close(self):
                pass

        passed = {
            "checks": [
                {"focus": name, "verdict": "pass", "report": "Correct."}
                for name in ("proof", "bounds", "cases")
            ],
            "verdict": "pass", "fixed": False,
            "solution": "Solution", "bugs": "",
        }
        output, holder = io.StringIO(), {}

        def make_rpc(child, record):
            holder["rpc"] = FakeRPC(child, record)
            return holder["rpc"]

        with mock.patch.object(agent, "codex", return_value="codex"), mock.patch.object(
            agent.subprocess, "Popen",
            return_value=SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO()),
        ), mock.patch.object(agent, "RPC", side_effect=make_rpc), mock.patch.object(
            agent, "criticize", return_value=passed
        ), mock.patch.object(
            agent, "finalize", return_value="LATEX"
        ), mock.patch("sys.stdout", output):
            self.assertEqual(agent.run_goal("FULL", "STATEMENT"), "LATEX")

        calls = holder["rpc"].calls
        continuation = next(
            index for index, call in enumerate(calls)
            if call[0] == "turn/start"
            and call[1]["input"][0]["text"] == agent.CONTINUE_PROMPT
        )
        self.assertEqual(calls[continuation - 1][1]["status"], "paused")
        self.assertEqual(calls[continuation + 1][1]["status"], "active")
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(
            record.get("text") == agent.CONTINUE_PROMPT for record in records
        ))

    def test_author_deadline_interrupts_and_returns_a_failure_summary(self):
        gate = threading.Event()

        class FakeRPC:
            def __init__(self, _, record=None):
                self.record, self.requests, self.turns = record, [], 0
                self.summary_events = iter([
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1", "turnId": "summary",
                            "item": {
                                "type": "agentMessage",
                                "text": "Progress summary",
                            },
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "summary", "status": "completed"},
                        },
                    },
                ])
                self.author_done = False

            def call(self, method, params):
                if method == "thread/start":
                    return {"thread": {"id": "thread-1"}}
                if method == "turn/start":
                    self.turns += 1
                    return {
                        "turn": {
                            "id": "turn-1" if self.turns == 1 else "summary"
                        }
                    }
                return {}

            def request(self, method, params):
                self.requests.append((method, params))
                if method == "turn/interrupt" and params["turnId"] == "turn-1":
                    gate.set()
                return len(self.requests)

            def send(self, _):
                pass

            def read(self):
                if not self.author_done:
                    if not gate.wait(1):
                        raise AssertionError("The author turn was not interrupted.")
                    self.author_done = True
                    message = {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "interrupted"},
                        },
                    }
                else:
                    message = next(self.summary_events)
                self.record(message)
                return message

            def close(self):
                pass

        process = SimpleNamespace(
            stdin=io.StringIO(), stdout=io.StringIO(),
            poll=mock.Mock(return_value=0), terminate=mock.Mock(),
        )
        holder, output = {}, io.StringIO()

        def make_rpc(child, record):
            holder["rpc"] = FakeRPC(child, record)
            return holder["rpc"]

        with mock.patch.object(agent, "codex", return_value="codex"), mock.patch.object(
            agent.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(agent, "RPC", side_effect=make_rpc), mock.patch.object(
            agent, "criticize"
        ) as criticize, mock.patch.object(
            agent, "finalize"
        ) as finalize, mock.patch("sys.stdout", output):
            result = agent.run_goal("FULL", "STATEMENT", thinking_hours=0.000001)

        self.assertEqual(result, "Progress summary")
        self.assertEqual(holder["rpc"].requests[0][0], "thread/goal/set")
        self.assertEqual(holder["rpc"].requests[0][1]["status"], "paused")
        self.assertEqual(holder["rpc"].requests[1], (
            "turn/interrupt",
            {"threadId": "thread-1", "turnId": "turn-1"},
        ))
        criticize.assert_not_called()
        finalize.assert_not_called()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[-1]["kind"], "failure_result")

    def test_goal_repairs_a_failed_critique_then_finalizes(self):
        class FakeRPC:
            def __init__(self, _, record=None):
                self.record = record
                self.calls = []
                self.events = iter([
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "item": {
                                "type": "agentMessage",
                                "text": "First solution",
                            },
                        },
                    },
                    {
                        "method": "thread/goal/updated",
                        "params": {
                            "threadId": "thread-1",
                            "goal": {"threadId": "thread-1", "status": "complete"},
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"status": "completed"},
                        },
                    },
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "item": {
                                "type": "agentMessage",
                                "text": "Corrected solution",
                            },
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"status": "completed"},
                        },
                    },
                ])

            def call(self, method, params):
                self.calls.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thread-1"}}
                return {}

            def send(self, _):
                pass

            def read(self):
                message = next(self.events)
                self.record(message)
                return message

            def close(self):
                pass

        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO())
        rpc = None
        output = io.StringIO()
        failed = {
            "checks": [
                {"focus": "line", "verdict": "fail", "report": "Gap at line 2."},
                {"focus": "rebuild", "verdict": "pass", "report": "Rebuilt."},
                {"focus": "edges", "verdict": "pass", "report": "Cases checked."},
            ],
            "verdict": "reject",
            "fixed": False,
            "solution": "Critic partial solution",
            "bugs": "Gap at line 2.",
        }
        fixed = {
            "checks": [
                {"focus": name, "verdict": "pass", "report": "Minor fix made."}
                for name in ("line", "rebuild", "edges")
            ],
            "verdict": "pass",
            "fixed": True,
            "solution": "Critic-corrected solution",
            "bugs": "",
        }
        passed = {
            "checks": [
                {"focus": name, "verdict": "pass", "report": "Correct."}
                for name in ("line", "rebuild", "edges")
            ],
            "verdict": "pass",
            "fixed": False,
            "solution": "Critic-corrected solution",
            "bugs": "",
        }

        def make_rpc(child, record):
            nonlocal rpc
            rpc = FakeRPC(child, record)
            return rpc

        with mock.patch.object(agent, "codex", return_value="codex"), mock.patch.object(
            agent.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(agent, "RPC", side_effect=make_rpc), mock.patch.object(
            agent, "criticize", side_effect=[failed, fixed, passed]
        ) as criticize, mock.patch.object(
            agent, "finalize", return_value="LATEX"
        ) as finalize, mock.patch("sys.stdout", output):
            result = agent.run_goal("FULL PROMPT", "STATEMENT", 3)
        self.assertEqual(result, "LATEX")
        self.assertEqual(
            [call.args[1] for call in criticize.call_args_list],
            [
                "First solution",
                "Corrected solution",
                "Critic-corrected solution",
            ],
        )
        finalize.assert_called_once_with(
            "STATEMENT", "Critic-corrected solution", model=agent.WRITER_MODEL,
            effort=agent.EFFORT, speed=agent.DEFAULT_SPEED,
        )
        calls = rpc.calls
        methods = [method for method, _ in calls]
        self.assertEqual(
            methods,
            [
                "initialize",
                "thread/start",
                "thread/goal/set",
                "turn/start",
                "thread/goal/set",
                "turn/start",
            ],
        )
        self.assertEqual(calls[2][1]["status"], "paused")
        self.assertEqual(calls[3][1]["input"][0]["text"], "FULL PROMPT")
        self.assertEqual(calls[4][1]["status"], "active")
        self.assertEqual(calls[1][1]["cwd"], str(Path.cwd()))
        self.assertEqual(
            calls[1][1]["config"]["service_tier"], agent.SERVICE_TIER
        )
        self.assertTrue(calls[1][1]["config"]["features"]["fast_mode"])
        command = popen.call_args.args[0]
        self.assertIn(f'service_tier="{agent.SERVICE_TIER}"', command)
        self.assertIn("fast_mode", command)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")
        self.assertIn("Gap at line 2.", calls[5][1]["input"][0]["text"])
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]["text"], "FULL PROMPT")
        self.assertEqual(records[1]["text"], agent.GOAL)
        self.assertEqual(records[0]["model"], agent.MODEL)

    def test_goal_never_finalizes_without_a_clean_critic_pass(self):
        class FakeRPC:
            def __init__(self, _, record=None):
                self.record = record
                self.calls = []
                self.events = iter([
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "item": {
                                "type": "agentMessage", "text": "Candidate",
                            },
                        },
                    },
                    {
                        "method": "thread/goal/updated",
                        "params": {
                            "threadId": "thread-1",
                            "goal": {"threadId": "thread-1", "status": "complete"},
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"status": "completed"},
                        },
                    },
                ])

            def call(self, method, params):
                self.calls.append((method, params))
                return {"thread": {"id": "thread-1"}} if method == "thread/start" else {}

            def send(self, _):
                pass

            def read(self):
                message = next(self.events)
                self.record(message)
                return message

            def close(self):
                pass

        fixed = {
            "checks": [
                {"focus": name, "verdict": "pass", "report": "Minor issue fixed."}
                for name in ("proof", "rebuild", "edges")
            ],
            "verdict": "pass",
            "fixed": True,
            "solution": "Critic-repaired candidate",
            "bugs": "",
        }
        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO())
        output = io.StringIO()
        with mock.patch.object(agent, "codex", return_value="codex"), mock.patch.object(
            agent.subprocess, "Popen", return_value=process
        ), mock.patch.object(agent, "RPC", FakeRPC), mock.patch.object(
            agent, "criticize", return_value=fixed
        ), mock.patch.object(
            agent, "finalize", return_value="LATEX"
        ) as finalize, mock.patch(
            "sys.stdout", output
        ):
            with self.assertRaisesRegex(agent.Error, "without a clean pass"):
                agent.run_goal("FULL", "STATEMENT", 1)
        finalize.assert_not_called()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        partial = next(
            record for record in records if record["kind"] == "partial_result"
        )
        self.assertEqual(partial["output"], "Critic-repaired candidate")

    def test_critic_requires_three_independent_verdicts(self):
        report = {
            "checks": [
                {"focus": name, "verdict": "pass", "report": "Correct."}
                for name in ("hostile", "rebuild", "edges")
            ],
            "verdict": "pass",
            "fixed": False,
            "solution": "P",
            "bugs": "",
        }
        with mock.patch.object(
            agent, "structured", return_value=(report, json.dumps(report))
        ) as structured, mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(agent.criticize("S", "P", 1), report)
        prompt, schema, stage = structured.call_args.args
        self.assertIn("wait for all three", prompt)
        self.assertIn("hostile\nline-by-line proof audits", prompt)
        self.assertIn("try to fix every reported bug", prompt)
        self.assertIs(schema, agent.CRITIC_SCHEMA)
        self.assertEqual(stage, "critic")
        self.assertEqual(structured.call_args.kwargs, {
            "model": agent.CRITIC_MODEL,
            "effort": "ultra",
            "speed": agent.DEFAULT_SPEED,
        })

    def test_critic_can_fix_minor_bugs_before_the_next_round(self):
        report = {
            "checks": [
                {"focus": name, "verdict": "pass", "report": "Minor fix only."}
                for name in ("hostile", "rebuild", "edges")
            ],
            "verdict": "pass",
            "fixed": True,
            "solution": "Repaired proof",
            "bugs": "",
        }
        with mock.patch.object(
            agent, "structured", return_value=(report, json.dumps(report))
        ), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(
                agent.criticize("S", "P", 1)["solution"], "Repaired proof"
            )

    def test_critic_call_enables_codex_multi_agent(self):
        process = SimpleNamespace(
            stdin=mock.Mock(), stdout=io.StringIO(""),
            wait=mock.Mock(return_value=0), poll=mock.Mock(return_value=0),
        )

        def fake_popen(command, **_):
            Path(command[command.index("-o") + 1]).write_text("{}", encoding="utf-8")
            return process

        with mock.patch.object(agent, "codex", return_value="codex"), mock.patch.object(
            agent.subprocess, "Popen", side_effect=fake_popen
        ) as popen, mock.patch("sys.stdout", io.StringIO()):
            agent.structured("prompt", {}, "critic")
        command = popen.call_args.args[0]
        self.assertIn("multi_agent", command)
        self.assertIn("fast_mode", command)

    def test_final_editor_emits_the_latex_output(self):
        report = {"latex": "\\begin{proof}Done.\\end{proof}"}
        output = io.StringIO()
        with mock.patch.object(
            agent, "structured", return_value=(report, json.dumps(report))
        ) as structured, mock.patch("sys.stdout", output):
            self.assertEqual(
                agent.finalize("S", "P", "gpt-5.6-terra"), report["latex"]
            )
        self.assertEqual(structured.call_args.kwargs, {
            "model": "gpt-5.6-terra",
            "effort": "ultra",
            "speed": agent.DEFAULT_SPEED,
        })
        record = json.loads(output.getvalue())
        self.assertEqual(record["kind"], "final_result")
        self.assertEqual(record["output"], report["latex"])


if __name__ == "__main__":
    unittest.main()
