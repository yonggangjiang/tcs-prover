"""Goal transport behavior with a scripted local RPC double; no model calls."""
from collections import deque
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import workflow_runner as module


def event(method, **params):
    return {"method": method, "params": {"threadId": "thread-1", **params}}


def answer(text, phase="final_answer"):
    return event("item/completed", item={"type": "agentMessage", "text": text, "phase": phase})


def goal(status):
    return event("thread/goal/updated", goal={"threadId": "thread-1", "status": status})


def completed(status="completed", items=None):
    return event("turn/completed", turn={"id": "turn-1", "status": status, "items": items or []})


def success(text="Full proof"):
    return [answer(text), goal("complete"), completed()]


class TransportError(RuntimeError):
    pass


class RPC:
    def __init__(self, runtime, record):
        self.runtime, self.record = runtime, record
        self.messages, self.calls = deque(), []
        self.number, self.turns = 0, 0
        self.capabilities = {}

    def send(self, message):
        self.calls.append((message["method"], message.get("params")))

    def request(self, method, params):
        self.calls.append((method, params))
        self.number += 1
        if method == "turn/steer":
            self.runtime.steered.set()
            self.messages.append({"id": self.number, "result": {}})
        if method == "turn/interrupt":
            self.runtime.interrupted.set()
        return self.number

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "initialize":
            self.capabilities = params.get("capabilities", {})
        if (method in {"thread/start", "thread/resume"}
                and "runtimeWorkspaceRoots" in params
                and not self.capabilities.get("experimentalApi")):
            raise TransportError(f"{method}.runtimeWorkspaceRoots requires experimentalApi capability")
        if method == "thread/resume" and self.runtime.resume_error:
            raise TransportError(self.runtime.resume_error)
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thread-1"}}
        if method == "thread/goal/get":
            return {"goal": self.runtime.saved_goal}
        if method == "turn/start":
            self.turns += 1
            self.messages.append(event("turn/started", turn={"id": f"turn-{self.turns}"}))
            self.messages.extend(self.runtime.scripts.popleft())
            return {"turn": {"id": f"turn-{self.turns}"}}
        return {}

    def read(self):
        if not self.messages and self.runtime.on_empty:
            self.runtime.on_empty(self)
        if not self.messages:
            raise TransportError("Scripted transport disconnected")
        message = self.messages.popleft()
        if isinstance(message, Exception):
            raise message
        self.record(message)
        return message


class Runtime:
    Error = TransportError
    INTERRUPT_GRACE_SECONDS = 0.01

    def __init__(self, scripts):
        self.scripts = deque(scripts)
        self.events, self.stopped = [], []
        self.remaining, self.resume_error, self.on_empty = 100, None, None
        self.steered = threading.Event()
        self.interrupted = threading.Event()
        self.saved_goal = None
        self.command = None

    def RPC(self, process, record):
        self.rpc = RPC(self, record)
        return self.rpc

    def emit(self, kind, stage, **fields):
        self.events.append({"kind": kind, "stage": stage, **fields})

    def workflow_remaining(self, options):
        return self.remaining

    def pending_author_steer(self, path, delivered):
        return self.command if self.command and self.command[0] != delivered else None

    def stop_process(self, process):
        self.stopped.append(process)

    public_event = staticmethod(lambda value: value)
    render_template = staticmethod(lambda template, values: template.format(**values))
    codex = staticmethod(lambda: "codex-test")
    provider_arguments = staticmethod(lambda model: [])
    speed_arguments = staticmethod(lambda speed, model: [])
    context_cache_arguments = staticmethod(lambda: [])
    environment = staticmethod(lambda model: {})
    model_provider = staticmethod(lambda model: "openai")
    effective_effort = staticmethod(lambda model, value: value)
    effective_speed = staticmethod(lambda model, value: value)
    reasoning_summary = staticmethod(lambda model, value: value)


PROMPTS = {
    "goal": "Complete this exact assignment.",
    "continuation": "Continue the same goal using the earlier instructions.",
    "compaction": "Reload your own work in {directory} and continue.",
    "repair": "Revision {revision_number}; critic round {round}: {bugs}\nCandidate: {solution}",
    "resume": "Resume in {directory}; follow your earlier records.\nOriginal assignment:\n{original_prompt}",
}
PROMPT = "Exact assignment"
SETTINGS = {"model": "test-model", "effort": "high", "speed": "standard", "summary": "concise"}


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name).resolve()
        self.popen = patch.object(module.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = self.popen.start()
        self.addCleanup(self.popen.stop)

    def session(self, scripts=None, **kwargs):
        runtime = Runtime(scripts if scripts is not None else [success()])
        options = {"goal_cwd": str(self.directory), **kwargs.pop("options", {})}
        session = module.goal_session(runtime, PROMPT, prompts=PROMPTS,
                                      settings=SETTINGS, options=options, **kwargs)
        self.addCleanup(session.close)
        return runtime, session

    def test_single_goal_and_cleanup_create_no_workspace_files(self):
        runtime, session = self.session()
        self.assertEqual(next(session), {"outcome": "done", "output": "Full proof"})
        self.assertEqual(list(self.directory.iterdir()), [])
        command = self.spawn.call_args.args[0]
        self.assertIn("goals", command)
        self.assertEqual(command[command.index("--disable") + 1], "multi_agent")
        starts = [p for m, p in runtime.rpc.calls if m == "thread/start"]
        self.assertEqual(len(starts), 1)
        self.assertFalse(starts[0]["ephemeral"])
        self.assertFalse(starts[0]["config"]["features"]["multi_agent"])
        self.assertEqual([p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start"], ["Exact assignment"])
        session.close()
        self.assertTrue(runtime.stopped)
        self.assertEqual(runtime.rpc.calls[-1][1]["status"], "active")

    def test_features_enable_multi_agent_and_other_flags_on_start_and_resume(self):
        for options in ({}, {"goal_thread_id": "thread-1"}):
            with self.subTest(options=options):
                runtime, session = self.session(features=["multi_agent", "example_feature"], options=options)
                self.assertEqual(next(session)["outcome"], "done")
                command = self.spawn.call_args.args[0]
                self.assertIn(("--enable", "multi_agent"), list(zip(command, command[1:])))
                self.assertIn(("--enable", "example_feature"), list(zip(command, command[1:])))
                self.assertNotIn(("--disable", "multi_agent"), list(zip(command, command[1:])))
                config = next(p["config"] for m, p in runtime.rpc.calls if m in {"thread/start", "thread/resume"})
                self.assertTrue(config["features"]["multi_agent"])
                self.assertTrue(config["features"]["example_feature"])
                self.assertEqual(config["web_search"], "live")
                self.assertTrue(config["tools"]["web_search"])
                self.assertEqual(sum(m in {"thread/start", "thread/resume"} for m, _ in runtime.rpc.calls), 1)
                session.close()

    def test_experimental_workspace_api_is_negotiated_for_start_and_resume(self):
        for options in ({}, {"goal_thread_id": "thread-1"}):
            with self.subTest(options=options):
                runtime, session = self.session(options=options)
                self.assertEqual(next(session)["outcome"], "done")
                initialization = runtime.rpc.calls[0]
                self.assertEqual(initialization[0], "initialize")
                self.assertTrue(initialization[1]["capabilities"]["experimentalApi"])
                thread = next(params for method, params in runtime.rpc.calls
                              if method in {"thread/start", "thread/resume"})
                self.assertEqual(thread["runtimeWorkspaceRoots"], [str(self.directory)])
                session.close()

    def test_goal_yaml_accepts_features_and_passes_them_to_session(self):
        definition = {"prompts": {**PROMPTS, "assignment": "Do [TASK]."}, "nodes": {
            "worker": {"run": "goal", "prompt": "assignment", "task": "state.input", "marker": "[TASK]",
                       "features": ["multi_agent"], "resume": {}, "outcome": "result.outcome",
                       "after": [{"merge": "result"}], "next": {"done": "end", "failure": "end"}}
        }}
        path = self.directory / "custom.yaml"
        path.write_text(module.yaml.safe_dump(definition))
        workflow = module.load_workflow(path)
        with patch.object(module, "goal_session", return_value=iter([{"outcome": "done", "output": "Complete"}])) as session:
            with patch.object(module, "emit"):
                # The interpreter closes persistent generators at graph exit.
                session.return_value = (item for item in [{"outcome": "done", "output": "Complete"}])
                result = module._execute(workflow, {"input": "the task"}, {}, workflow["prompts"])
        self.assertEqual(result["output"], "Complete")
        self.assertEqual(session.call_args.kwargs["features"], ["multi_agent"])
        for invalid in ("multi_agent", [False], [""]):
            definition["nodes"]["worker"]["features"] = invalid
            path.write_text(module.yaml.safe_dump(definition))
            with self.assertRaisesRegex(ValueError, "features must"):
                module.load_workflow(path)

    def test_explicit_resume_preserves_arbitrary_existing_files_without_reading_them(self):
        contents = {"arbitrary.bin": b"\xff\x00\xfe\r\n", "other.txt": b"Old unrelated assignment\r\n\n"}
        for name, data in contents.items():
            (self.directory / name).write_bytes(data)
        runtime, session = self.session(options={"goal_resume": True})
        with patch("builtins.open", side_effect=AssertionError("No file access")):
            with patch.object(Path, "open", side_effect=AssertionError("No file access")):
                next(session)
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, contents)
        prompt = next(p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start")
        self.assertEqual(prompt, PROMPTS["resume"].format(directory=self.directory, original_prompt=PROMPT))

    def test_existing_files_do_not_implicitly_select_resume_or_change_prompt(self):
        (self.directory / "existing.txt").write_bytes(b"Different contents\r\n")
        runtime, session = self.session()
        next(session)
        prompt = next(p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start")
        self.assertEqual(prompt, PROMPT)
        self.assertEqual((self.directory / "existing.txt").read_bytes(), b"Different contents\r\n")

    def test_ordinary_turn_return_is_not_a_completed_goal(self):
        scripts = [[answer("Partial work"), completed(),
                    event("turn/started", turn={"id": "automatic-2"}), *success("Actual proof")]]
        _, session = self.session(scripts)
        self.assertEqual(next(session)["output"], "Actual proof")

    def test_subagent_goal_notifications_cannot_finish_or_fail_the_root(self):
        child_events = [
            {"method": "thread/goal/updated", "params": {"goal": {"threadId": "child", "status": "complete"}}},
            event("thread/goal/updated", goal={"threadId": "child", "status": "complete"}),
            {"method": "thread/goal/updated", "params": {"goal": {"threadId": "child", "status": "usageLimited"}}},
        ]
        scripts = [[answer("Root partial progress"), completed(), *child_events,
                    event("turn/started", turn={"id": "root-next"}), *success("Root completed result")]]
        runtime, session = self.session(scripts, features=["multi_agent"])
        self.assertEqual(next(session)["output"], "Root completed result")
        logged = [e for e in runtime.events if e.get("event") in child_events]
        self.assertEqual(len(logged), 3)
        self.assertTrue(all(e["root"] is False for e in logged))

    def test_subagent_answers_and_compactions_are_excluded_from_root_state(self):
        child_events = [
            event("item/completed", threadId="child", item={"type": "agentMessage", "phase": "final_answer", "text": "Child result"}),
            {"method": "item/completed", "params": {"item": {"threadId": "child", "type": "agentMessage", "text": "Nested child result"}}},
            event("item/completed", item={"threadId": "child", "type": "agentMessage", "text": "Conflicting-owner child result"}),
            event("item/completed", threadId="child", turnId="child-turn", item={"type": "contextCompaction", "id": "child-compact"}),
            {"method": "turn/completed", "params": {"turn": {"threadId": "child", "id": "child-turn", "status": "failed"}}},
        ]
        runtime, session = self.session([[*child_events, goal("complete"), completed()], success("Root answer")], features=["multi_agent"])
        self.assertEqual(next(session)["output"], "Root answer")
        self.assertEqual(runtime.rpc.turns, 2)
        self.assertFalse(any(m == "turn/steer" for m, _ in runtime.rpc.calls))
        self.assertTrue(all(e["root"] is False for e in runtime.events if e.get("event") in child_events))

    def test_goal_completion_requires_explicit_root_identity(self):
        unknown = {"method": "thread/goal/updated", "params": {"goal": {"status": "complete"}}}
        root = {"method": "thread/goal/updated", "params": {"goal": {"threadId": "thread-1", "status": "complete"}}}
        scripts = [[answer("Root unfinished"), completed(), unknown,
                    event("turn/started", turn={"id": "root-next"}), answer("Root final"), completed(), root]]
        runtime, session = self.session(scripts, features=["multi_agent"])
        self.assertEqual(next(session)["output"], "Root final")
        self.assertTrue(any(e.get("event") == root and e["root"] for e in runtime.events))

    def test_commentary_is_not_a_solution_and_blocked_goal_continues(self):
        scripts = [[answer("Working", "commentary"), goal("complete"), completed()],
                   [answer("This route failed"), goal("blocked"), completed()], success()]
        runtime, session = self.session(scripts)
        self.assertEqual(next(session)["output"], "Full proof")
        self.assertEqual(runtime.rpc.turns, 3)
        self.assertEqual(sum(m == "thread/start" for m, _ in runtime.rpc.calls), 1)
        inputs = [p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start"]
        self.assertEqual(inputs[1:], [PROMPTS["continuation"]] * 2)

    def test_rejected_proof_restarts_goal_on_same_thread_and_waits_for_completion(self):
        runtime, session = self.session([success(), [answer("Still repairing"), goal("blocked"), completed()], success("Repaired proof")])
        next(session)
        result = session.send({"solution": "Safe fixes", "bugs": "Gap in step 2", "round": 3})
        self.assertEqual(result["output"], "Repaired proof")
        turns = [p for m, p in runtime.rpc.calls if m == "turn/start"]
        self.assertEqual({p["threadId"] for p in turns}, {"thread-1"})
        self.assertEqual(turns[1]["input"][0]["text"], "Revision 1; critic round 3: Gap in step 2\nCandidate: Safe fixes")
        self.assertEqual(sum(m == "thread/goal/set" and p["status"] == "active" for m, p in runtime.rpc.calls), 3)

    def test_compaction_uses_yaml_instruction_once(self):
        compaction = event("item/completed", turnId="turn-1", item={"type": "contextCompaction", "id": "compact-1"})
        runtime, session = self.session([[compaction, compaction, *success()]])
        next(session)
        steers = [p for m, p in runtime.rpc.calls if m == "turn/steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["input"][0]["text"], PROMPTS["compaction"].format(directory=self.directory))

    def test_cross_process_resume_overrides_workspace(self):
        runtime, session = self.session(options={"goal_thread_id": "thread-1"})
        next(session)
        resumed = next(p for m, p in runtime.rpc.calls if m == "thread/resume")
        self.assertEqual(resumed["cwd"], str(self.directory))
        self.assertEqual(resumed["runtimeWorkspaceRoots"], [str(self.directory)])
        self.assertFalse(any(m == "thread/start" for m, _ in runtime.rpc.calls))
        self.assertTrue(any(e.get("label") == "Goal resumed" and e["threadId"] == "thread-1" for e in runtime.events))

    def test_missing_saved_thread_falls_back_but_quota_error_does_not(self):
        runtime, session = self.session(options={"goal_thread_id": "missing"})
        runtime.resume_error = "thread/resume failed: thread not found"
        self.assertEqual(next(session)["outcome"], "done")
        self.assertEqual(sum(m == "thread/start" for m, _ in runtime.rpc.calls), 1)
        runtime2, session2 = self.session(options={"goal_thread_id": "missing"})
        runtime2.resume_error = "Quota exceeded"
        with self.assertRaisesRegex(module.WorkflowPaused, "Quota exceeded"):
            next(session2)
        self.assertFalse(any(m == "thread/start" for m, _ in runtime2.rpc.calls))

    def test_paused_run_requires_its_original_thread_even_if_unavailable(self):
        for error in ("thread not found", "Quota exceeded", "Network unavailable"):
            with self.subTest(error=error):
                runtime, session = self.session(options={"goal_thread_id": "thread-1", "goal_require_resume": True})
                runtime.resume_error = error
                with self.assertRaisesRegex(module.WorkflowPaused, "no replacement was started"):
                    next(session)
                self.assertFalse(any(m in {"thread/start", "turn/start"} for m, _ in runtime.rpc.calls))

    def test_strict_resume_without_thread_id_starts_no_process(self):
        _, session = self.session(options={"goal_require_resume": True})
        with self.assertRaisesRegex(module.WorkflowPaused, "thread ID is missing"):
            next(session)
        self.spawn.assert_not_called()

    def test_resume_preserves_saved_goal_objective_and_budget(self):
        runtime, session = self.session(options={"goal_thread_id": "thread-1", "goal_require_resume": True})
        runtime.saved_goal = {"objective": "The original research goal before YAML changed.",
                              "status": "paused", "tokenBudget": 90000, "tokensUsed": 12345}
        self.assertEqual(next(session)["outcome"], "done")
        updates = [p for m, p in runtime.rpc.calls if m == "thread/goal/set"]
        self.assertEqual(updates[0]["objective"], runtime.saved_goal["objective"])
        self.assertTrue(all("objective" not in p for p in updates[1:]))
        self.assertTrue(all("tokenBudget" not in p for p in updates))
        self.assertFalse(any(m in {"thread/start", "thread/fork", "thread/goal/clear"} for m, _ in runtime.rpc.calls))

    def test_usage_network_and_unexpected_errors_pause_without_model_summary(self):
        for messages in ([goal("usageLimited")], [goal("budgetLimited")],
                         [TransportError("Network unavailable")], [completed("failed")],
                         [RuntimeError("Unexpected transport error")]):
            with self.subTest(messages=messages):
                runtime, session = self.session([messages])
                with self.assertRaises(module.WorkflowPaused) as raised:
                    next(session)
                self.assertTrue(raised.exception.reason)
                self.assertEqual(raised.exception.thread_id, "thread-1")
                self.assertEqual(runtime.rpc.turns, 1)
                session.close()
                self.assertEqual(list(self.directory.iterdir()), [])
                self.assertFalse(any(e["kind"] == "failure_result" for e in runtime.events))
                self.assertEqual(runtime.rpc.calls[-1], ("thread/goal/set", {"threadId": "thread-1", "status": "paused"}))

    def test_elapsed_limit_starts_no_model(self):
        runtime, session = self.session()
        runtime.remaining = 0
        with self.assertRaisesRegex(module.WorkflowPaused, "time limit"):
            next(session)
        self.spawn.assert_not_called()

    def test_live_steering_goes_to_current_turn(self):
        runtime, session = self.session([[]])
        runtime.command = ("steer-1", "Try the invariant in my instruction.")
        def wait_for_steer(rpc):
            self.assertTrue(runtime.steered.wait(2))
            rpc.messages.extend(success())
        runtime.on_empty = wait_for_steer
        self.assertEqual(next(session)["outcome"], "done")
        steers = [p for m, p in runtime.rpc.calls if m == "turn/steer"]
        self.assertEqual(steers[0]["expectedTurnId"], "turn-1")
        self.assertEqual(steers[0]["input"][0]["text"], runtime.command[1])
        self.assertTrue(any(e.get("authorSteerDelivered") == "steer-1" for e in runtime.events))

    def test_resume_does_not_replay_an_accepted_live_instruction(self):
        runtime, session = self.session([[]], options={"goal_thread_id": "thread-1", "author_steer_delivered": "steer-1"})
        runtime.command = ("steer-1", "Already accepted before the pause")
        def continue_after_watch(rpc):
            self.assertFalse(runtime.steered.wait(0.25))
            rpc.messages.extend(success())
        runtime.on_empty = continue_after_watch
        self.assertEqual(next(session)["outcome"], "done")
        self.assertFalse(any(m == "turn/steer" for m, _ in runtime.rpc.calls))

    def test_pause_interrupts_current_turn_without_asking_model_to_save(self):
        path = self.directory / "pause-request.json"
        runtime, session = self.session([[]], options={"goal_pause_file": str(path)})
        def pause_after_start(rpc):
            path.write_text("{}")
            self.assertTrue(runtime.interrupted.wait(2))
            rpc.messages.extend([answer("Unfinished argument"), goal("paused"), completed("interrupted")])
        runtime.on_empty = pause_after_start
        with patch.dict(PROMPTS, pause="Legacy save prompt"), self.assertRaises(module.WorkflowPaused):
            next(session)
        self.assertEqual(runtime.rpc.turns, 1)
        self.assertTrue(runtime.stopped)
        self.assertFalse(any(e["kind"] in {"author_result", "failure_result"} for e in runtime.events))
        sent = next(p for m, p in runtime.rpc.calls if m == "turn/interrupt")
        self.assertEqual(sent, {"threadId": "thread-1", "turnId": "turn-1"})
        self.assertFalse(any(m == "turn/steer" for m, _ in runtime.rpc.calls))
        self.assertEqual(runtime.rpc.calls[-1][1]["status"], "paused")

    def test_pause_without_a_responsive_model_retains_last_saved_work(self):
        path = self.directory / "pause-request.json"
        notebook = self.directory / "APPROACHES.md"
        notebook.write_text("Saved route and exact obstacle")
        runtime, session = self.session([[]], options={"goal_pause_file": str(path)})
        stopped = threading.Event()
        runtime.stop_process = lambda process: stopped.set()
        def hang(rpc):
            path.write_text("{}")
            self.assertTrue(stopped.wait(2))
            raise TransportError("Transport ended")
        runtime.on_empty = hang
        with self.assertRaises(module.WorkflowPaused):
            next(session)
        self.assertEqual(notebook.read_text(), "Saved route and exact obstacle")
        self.assertTrue(any("last saved work" in e.get("text", "") for e in runtime.events))
        self.assertFalse(any(e["kind"] == "failure_result" for e in runtime.events))

    def test_preexisting_pause_request_starts_no_process(self):
        path = self.directory / "pause-request.json"
        path.write_text("{}")
        _, session = self.session(options={"goal_pause_file": str(path)})
        with self.assertRaises(module.WorkflowPaused):
            next(session)
        self.spawn.assert_not_called()

    def test_pause_after_turn_before_late_goal_completion_cannot_yield_proof(self):
        path = self.directory / "pause-request.json"
        runtime, session = self.session([[answer("Proof"), completed()]], options={"goal_pause_file": str(path)})
        def pause_at_boundary(rpc):
            path.write_text("{}")
            rpc.messages.append(goal("complete"))
        runtime.on_empty = pause_at_boundary
        with self.assertRaises(module.WorkflowPaused):
            next(session)
        self.assertFalse(any(e["kind"] == "author_result" for e in runtime.events))

    def test_saved_critic_feedback_is_submitted_without_writing_it(self):
        runtime, session = self.session(initial_instruction="Previously rejected: repair step 2.")
        next(session)
        self.assertEqual(list(self.directory.iterdir()), [])
        prompt = next(p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start")
        self.assertIn("Previously rejected: repair step 2.", prompt)

    def test_turn_payload_final_answer_and_late_goal_completion(self):
        final = {"type": "agentMessage", "phase": "final_answer", "text": "Proof from final turn"}
        _, session = self.session([[completed(items=[final]), goal("complete")]])
        self.assertEqual(next(session)["output"], final["text"])

    def test_deadline_interrupts_hung_transport_without_summary_turn(self):
        runtime, session = self.session([[]])
        def deadline(rpc):
            runtime.remaining = 0
            limit = threading.Event()
            for _ in range(30):
                if runtime.stopped:
                    break
                limit.wait(0.01)
            self.assertTrue(runtime.stopped)
            raise TransportError("Process stopped")
        runtime.on_empty = deadline
        with self.assertRaisesRegex(module.WorkflowPaused, "time limit"):
            next(session)
        self.assertEqual(runtime.rpc.turns, 1)
        self.assertTrue(any(m == "turn/interrupt" for m, _ in runtime.rpc.calls))
        self.assertFalse(any(e["kind"] == "failure_result" for e in runtime.events))

    def test_resume_network_error_never_starts_a_replacement_model(self):
        runtime, session = self.session(options={"goal_thread_id": "saved"})
        runtime.resume_error = "thread/resume failed: network unavailable"
        with self.assertRaisesRegex(module.WorkflowPaused, "network unavailable"):
            next(session)
        self.assertFalse(any(m in {"thread/start", "turn/start"} for m, _ in runtime.rpc.calls))

    def test_completed_author_cleanup_does_not_emit_events_over_later_critic_stage(self):
        runtime, session = self.session()
        next(session)
        previous_calls, previous_events = list(runtime.rpc.calls), list(runtime.events)
        session.close()
        self.assertEqual(runtime.rpc.calls, previous_calls)
        self.assertEqual(runtime.events, previous_events)
        self.assertTrue(runtime.stopped)

    def test_deadline_while_yielded_to_critic_does_not_pause_or_stop_completed_author(self):
        runtime, session = self.session()
        next(session)
        previous_calls, previous_events = list(runtime.rpc.calls), list(runtime.events)
        observed = threading.Event()
        def remaining(options):
            observed.set()
            return 0
        runtime.workflow_remaining = remaining
        self.assertTrue(observed.wait(1))
        threading.Event().wait(0.05)
        self.assertEqual(runtime.rpc.calls, previous_calls)
        self.assertEqual(runtime.events, previous_events)
        self.assertEqual(runtime.stopped, [])
        session.close()
        self.assertTrue(runtime.stopped)

    def test_original_prompt_crlf_and_whitespace_are_submitted_verbatim_on_resume(self):
        original = "\r\n Original π statement\r\n  Keep these spaces. \r\n\r\n"
        runtime = Runtime([success()])
        session = module.goal_session(runtime, original, prompts=PROMPTS, settings=SETTINGS,
            options={"goal_cwd": str(self.directory), "goal_resume": True})
        self.addCleanup(session.close)
        self.assertEqual(next(session)["outcome"], "done")
        prompt = next(p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start")
        self.assertEqual(prompt, PROMPTS["resume"].format(directory=self.directory, original_prompt=original))
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
