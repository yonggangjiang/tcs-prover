"""Continuation delivery, checkpoints, subagent re-anchoring, and the token meter; no model calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_goal_runtime import (PROMPT, PROMPTS, SETTINGS, Runtime, answer, completed,  # noqa: E402
                               event, goal, success)


FULL_PROMPTS = {
    **PROMPTS,
    "checkpoint": "Write the PLAN checkpoint now.",
    "subagent_compaction": "Subagent: restate your task and finish it.",
}


def usage(thread, total, last_input=1000, output=10):
    return {"method": "thread/tokenUsage/updated", "params": {"threadId": thread, "tokenUsage": {
        "total": {"totalTokens": total, "inputTokens": total - output, "cachedInputTokens": total // 2,
                  "outputTokens": output, "reasoningOutputTokens": output // 2},
        "last": {"totalTokens": last_input + output, "inputTokens": last_input, "cachedInputTokens": 0,
                 "outputTokens": output, "reasoningOutputTokens": 0}}}}


class ContextEconomyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name).resolve()
        popen = patch.object(module.subprocess, "Popen", return_value=object())
        popen.start()
        self.addCleanup(popen.stop)

    def session(self, scripts, prompts=FULL_PROMPTS, **options):
        runtime = Runtime(scripts)
        session = module.goal_session(runtime, PROMPT, prompts=prompts, settings=SETTINGS,
                                      options={"goal_cwd": str(self.directory), **options}, features=["multi_agent"])
        self.addCleanup(session.close)
        return runtime, session

    def steers(self, runtime):
        return [p for m, p in runtime.rpc.calls if m == "turn/steer"]

    def test_auto_started_turn_receives_the_continuation_instruction_once(self):
        scripts = [[answer("Partial work"), completed(),
                    event("turn/started", turn={"id": "automatic-2"}), *success("Actual proof")]]
        runtime, session = self.session(scripts)
        self.assertEqual(next(session)["output"], "Actual proof")
        steers = self.steers(runtime)
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["expectedTurnId"], "automatic-2")
        self.assertEqual(steers[0]["input"][0]["text"], PROMPTS["continuation"])
        self.assertTrue(any(e.get("label") == "Continuation instruction sent" for e in runtime.events))

    def test_controller_started_turns_and_disabled_option_send_no_continuation(self):
        scripts = [[answer("Partial work"), completed(),
                    event("turn/started", turn={"id": "automatic-2"}), *success("Actual proof")]]
        runtime, session = self.session(scripts, continuation_steer=False)
        self.assertEqual(next(session)["output"], "Actual proof")
        self.assertEqual(self.steers(runtime), [])
        runtime, session = self.session([[answer("Working"), goal("blocked"), completed()], success()])
        self.assertEqual(next(session)["output"], "Full proof")
        self.assertEqual(self.steers(runtime), [])

    def test_checkpoint_is_requested_once_per_window_before_compaction(self):
        compaction = event("item/completed", turnId="turn-1", item={"type": "contextCompaction", "id": "compact-1"})
        scripts = [[usage("thread-1", 200000, last_input=50000), usage("thread-1", 300000, last_input=125000),
                    usage("thread-1", 400000, last_input=130000), compaction,
                    usage("thread-1", 450000, last_input=20000), usage("thread-1", 600000, last_input=140000), *success()]]
        runtime, session = self.session(scripts, checkpoint_tokens=120000)
        next(session)
        texts = [p["input"][0]["text"] for p in self.steers(runtime)]
        self.assertEqual(texts, [FULL_PROMPTS["checkpoint"], PROMPTS["compaction"].format(directory=self.directory),
                                 FULL_PROMPTS["checkpoint"]])

    def test_compaction_limit_is_configured_on_the_thread(self):
        runtime, session = self.session([success()], compaction_tokens=90000)
        next(session)
        config = next(p["config"] for m, p in runtime.rpc.calls if m == "thread/start")
        self.assertEqual(config["model_auto_compact_token_limit"], 90000)
        runtime, session = self.session([success()], compaction_tokens=0)
        next(session)
        config = next(p["config"] for m, p in runtime.rpc.calls if m == "thread/start")
        self.assertNotIn("model_auto_compact_token_limit", config)

    def test_compacted_subagent_is_re_anchored_on_its_own_thread(self):
        child = event("item/completed", threadId="child", turnId="child-turn",
                      item={"type": "contextCompaction", "id": "child-compact"})
        runtime, session = self.session([[child, child, *success()]])
        next(session)
        steers = self.steers(runtime)
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["threadId"], "child")
        self.assertEqual(steers[0]["expectedTurnId"], "child-turn")
        self.assertEqual(steers[0]["input"][0]["text"], FULL_PROMPTS["subagent_compaction"])
        runtime, session = self.session([[child, *success()]], prompts=PROMPTS)
        next(session)
        self.assertEqual(self.steers(runtime), [])

    def test_token_meter_sums_all_threads_and_survives_counter_resets(self):
        scripts = [[usage("thread-1", 1000), usage("child", 5000), usage("thread-1", 3000),
                    usage("thread-1", 500), *success()]]
        runtime, session = self.session(scripts, token_budget=8500)
        with self.assertRaises(module.WorkflowPaused) as raised:
            next(session)
        self.assertIn("Token budget reached", raised.exception.reason)
        meter = json.loads((self.directory / module.TOKEN_USAGE_FILENAME).read_text())
        self.assertEqual(meter["totals"]["totalTokens"], 3000 + 500 + 5000)
        self.assertEqual(meter["totals"]["calls"], 4)
        self.assertEqual(meter["threads"]["thread-1"]["totalTokens"], 3500)
        self.assertTrue(any(e.get("label") == "Token meter" for e in runtime.events))

    def test_no_budget_means_no_pause_and_no_meter_file(self):
        runtime, session = self.session([[usage("thread-1", 1000), *success()]])
        self.assertEqual(next(session)["output"], "Full proof")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_fatal_error_keeps_subagent_final_answers(self):
        child = event("item/completed", threadId="child",
                      item={"type": "agentMessage", "phase": "final_answer", "text": "Lemma 7 verified; constants 3/4."})
        runtime, session = self.session([[child, completed("failed")]])
        with self.assertRaises(module.WorkflowPaused):
            next(session)
        saved = (self.directory / module.UNSAVED_SUBAGENT_FILENAME).read_text()
        self.assertIn("Lemma 7 verified; constants 3/4.", saved)
        self.assertIn("Thread child", saved)
        self.assertTrue(any(e.get("label") == "Unsaved subagent results kept" for e in runtime.events))

    def test_workflow_yaml_binds_the_optional_lifecycle_prompts(self):
        workflow = module.load_workflow(module.WORKFLOWS / "author_critic.yaml")
        lifecycle = workflow["nodes"]["author"]["lifecycle"]
        self.assertIn("checkpoint", lifecycle)
        self.assertIn("subagent_compaction", lifecycle)
        prompts = module.prepare(workflow, {})
        self.assertIn("records.py", prompts["compaction"])
        self.assertIn("PLAN", prompts["author"])
        simple = module.prepare(workflow, {"file_management": False})
        self.assertNotIn("INITIAL_PROMPT.md", simple["compaction"])
        self.assertNotIn("INITIAL_PROMPT.md", simple["checkpoint"])

    def test_standard_speed_is_the_default_and_tools_are_exported(self):
        self.assertEqual(module.DEFAULT_SPEED, "standard")
        self.assertEqual(module.speed_arguments("standard"), ["--disable", "fast_mode"])
        self.assertEqual(module.environment()["TCS_PROVER_TOOLS"], str(module.TOOLS))
        self.assertTrue((module.TOOLS / "records.py").is_file())


if __name__ == "__main__":
    unittest.main()


def rate_limits(used, resets=1789916433):
    return {"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": resets}, "planType": "pro"}}}


def web_search(thread="thread-1"):
    return event("item/completed", threadId=thread, item={"type": "webSearch", "id": "ws", "query": "q"})


class ProviderBudgetTests(ContextEconomyTests):
    def test_usage_quota_is_reported_per_decile_and_pauses_before_exhaustion(self):
        runtime, session = self.session([[rate_limits(0), rate_limits(4), rate_limits(31), *success()]])
        self.assertEqual(next(session)["output"], "Full proof")
        quota = [e for e in runtime.events if e.get("label") == "Usage quota"]
        self.assertEqual([e["usedPercent"] for e in quota], [0, 31])
        runtime, session = self.session([[rate_limits(96), *success()]], quota_pause_percent=95)
        with self.assertRaises(module.WorkflowPaused) as raised:
            next(session)
        self.assertIn("Usage quota at 96%", raised.exception.reason)
        self.assertIn("2026-09-20", raised.exception.reason)
        runtime, session = self.session([[rate_limits(99), *success()]], quota_pause_percent=0)
        self.assertEqual(next(session)["output"], "Full proof")
        runtime, session = self.session([[rate_limits(86), rate_limits(88), *success()]], quota_pause_percent=90)
        self.assertEqual(next(session)["output"], "Full proof")
        steers = self.steers(runtime)
        self.assertEqual([p["input"][0]["text"] for p in steers], [FULL_PROMPTS["checkpoint"]])

    def test_web_action_budget_steers_the_author_once_per_hour(self):
        searches = [web_search("thread-1" if i % 2 else "child") for i in range(6)]
        runtime, session = self.session([[*searches, *success()]], web_actions_per_hour=4)
        self.assertEqual(next(session)["output"], "Full proof")
        steers = self.steers(runtime)
        self.assertEqual(len(steers), 1)
        self.assertIn("Web action budget reached: 5", steers[0]["input"][0]["text"])
        self.assertTrue(any(e.get("label") == "Web action budget" for e in runtime.events))
        runtime, session = self.session([[*searches, *success()]], web_actions_per_hour=0)
        next(session)
        self.assertEqual(self.steers(runtime), [])

    def test_codex_caps_are_configured_on_the_thread(self):
        runtime, session = self.session([success()], subagent_effort="high")
        next(session)
        config = next(p["config"] for m, p in runtime.rpc.calls if m == "thread/start")
        self.assertEqual(config["tool_output_token_limit"], module.DEFAULT_TOOL_OUTPUT_TOKENS)
        self.assertIs(config["include_apps_instructions"], False)
        self.assertEqual(config["agents"], {"max_concurrent_threads_per_session": 2,
                                            "default_subagent_reasoning_effort": "high"})
        command = module.subprocess.Popen.call_args.args[0]
        # codex app-server has no --ignore-user-config flag (only `exec` does).
        self.assertNotIn("--ignore-user-config", command)
        runtime, session = self.session([success()], tool_output_tokens=0, subagent_threads=0, apps_instructions=True)
        next(session)
        config = next(p["config"] for m, p in runtime.rpc.calls if m == "thread/start")
        self.assertNotIn("tool_output_token_limit", config)
        self.assertNotIn("agents", config)
        self.assertNotIn("include_apps_instructions", config)


class SubagentLifetimeTests(ContextEconomyTests):
    def test_meter_ignores_re_emitted_usage_and_caps_long_subagents(self):
        child_turn = {"method": "turn/started", "params": {"threadId": "child", "turn": {"id": "child-turn-1"}}}
        events = [child_turn, usage("child", 1000), usage("child", 1000), usage("child", 2000), usage("child", 3000),
                  usage("child", 3000), usage("child", 4000), usage("child", 5000)]
        runtime, session = self.session([[*events, *success()]], subagent_call_cap=4, token_budget=100000)
        self.assertEqual(next(session)["output"], "Full proof")
        steers = self.steers(runtime)
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["threadId"], "child")
        self.assertEqual(steers[0]["expectedTurnId"], "child-turn-1")
        self.assertIn("used 4 model calls", steers[0]["input"][0]["text"])
        self.assertTrue(any(e.get("label") == "Subagent call cap" for e in runtime.events))
        runtime, session = self.session([[*events, *success()]], subagent_call_cap=0, token_budget=100000)
        next(session)
        self.assertEqual(self.steers(runtime), [])

    def test_compaction_message_carries_the_statement(self):
        prompts = {**FULL_PROMPTS, "compaction": "Compacted. Statement: {statement}"}
        compaction = event("item/completed", turnId="turn-1", item={"type": "contextCompaction", "id": "compact-1"})
        runtime, session = self.session([[compaction, *success()]], prompts=prompts, goal_statement="Prove P != NP.")
        next(session)
        self.assertEqual(self.steers(runtime)[0]["input"][0]["text"], "Compacted. Statement: Prove P != NP.")

    def test_workflow_passes_the_statement_to_the_goal_session(self):
        workflow = module.load_workflow(module.WORKFLOWS / "author_critic.yaml")
        prompts = module.prepare(workflow, {})
        self.assertIn("{statement}", prompts["compaction"])
        captured = {}

        def fake_session(runtime, prompt, **kwargs):
            captured.update(kwargs["options"])
            yield {"outcome": "failure", "output": ""}

        with patch.object(module, "goal_session", side_effect=fake_session), patch.object(module, "emit"):
            module._execute(workflow, {"statement": "Exact task text"}, {}, prompts)
        self.assertEqual(captured["goal_statement"], "Exact task text")
