"""Goal transport behavior with a scripted local RPC double; no model calls."""
from collections import deque
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from goal_runtime import goal_session


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

    def send(self, message):
        self.calls.append((message["method"], message.get("params")))

    def request(self, method, params):
        self.calls.append((method, params))
        self.number += 1
        if method == "turn/steer":
            self.runtime.steered.set()
            self.messages.append({"id": self.number, "result": {}})
        return self.number

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/resume" and self.runtime.resume_error:
            raise TransportError(self.runtime.resume_error)
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thread-1"}}
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
    "continuation": "Continue the same goal and read {prompt_file}.",
    "compaction": "Reload the notebooks in {directory}, keeping {prompt_file} fixed.",
    "repair": "Revision {revision_number}; critic round {round}: {bugs}\nCandidate: {solution}",
    "resume": "Resume in {directory}; read {prompt_file} and both research notebooks.",
}
FILES = {"assignment.txt": "Exact assignment", "attempts.md": "# Attempts\n", "failed.md": "# Failures\n"}
SETTINGS = {"model": "test-model", "effort": "high", "speed": "standard", "summary": "concise"}


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name).resolve()
        self.popen = patch("goal_runtime.subprocess.Popen", return_value=SimpleNamespace())
        self.spawn = self.popen.start()
        self.addCleanup(self.popen.stop)

    def session(self, scripts=None, **kwargs):
        runtime = Runtime(scripts if scripts is not None else [success()])
        session = goal_session(runtime, FILES["assignment.txt"], prompts=PROMPTS, files=FILES,
                               prompt_file="assignment.txt", directory=self.directory,
                               settings=SETTINGS, options=kwargs.pop("options", {}), **kwargs)
        self.addCleanup(session.close)
        return runtime, session

    def test_initial_files_single_goal_and_cleanup(self):
        runtime, session = self.session()
        self.assertEqual(next(session), {"outcome": "proof", "solution": "Full proof"})
        self.assertEqual({p.name: p.read_text() for p in self.directory.iterdir()}, FILES)
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

    def test_existing_notebooks_preserved_and_resume_instructions_used(self):
        for name, value in FILES.items():
            (self.directory / name).write_text(value)
        (self.directory / "failed.md").write_bytes(b"Complete old failures\r\n\n")
        runtime, session = self.session()
        next(session)
        self.assertEqual((self.directory / "failed.md").read_bytes(), b"Complete old failures\r\n\n")
        prompt = next(p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start")
        self.assertIn("Resume in", prompt)

    def test_changed_assignment_rejected_before_any_call_or_notebook_write(self):
        (self.directory / "assignment.txt").write_text("Another assignment")
        _, session = self.session()
        with self.assertRaisesRegex(TransportError, "saved initial prompt differs"):
            next(session)
        self.spawn.assert_not_called()
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_ordinary_turn_return_is_not_a_completed_goal(self):
        scripts = [[answer("Partial work"), completed(),
                    event("turn/started", turn={"id": "automatic-2"}), *success("Actual proof")]]
        _, session = self.session(scripts)
        self.assertEqual(next(session)["solution"], "Actual proof")

    def test_commentary_is_not_a_solution_and_blocked_goal_continues(self):
        scripts = [[answer("Working", "commentary"), goal("complete"), completed()],
                   [answer("This route failed"), goal("blocked"), completed()], success()]
        runtime, session = self.session(scripts)
        self.assertEqual(next(session)["solution"], "Full proof")
        self.assertEqual(runtime.rpc.turns, 3)
        self.assertEqual(sum(m == "thread/start" for m, _ in runtime.rpc.calls), 1)
        inputs = [p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start"]
        self.assertEqual(inputs[1:], ["Continue the same goal and read assignment.txt."] * 2)

    def test_rejected_proof_restarts_goal_on_same_thread_and_waits_for_completion(self):
        runtime, session = self.session([success(), [answer("Still repairing"), goal("blocked"), completed()], success("Repaired proof")])
        next(session)
        result = session.send({"solution": "Safe fixes", "bugs": "Gap in step 2", "round": 3})
        self.assertEqual(result["solution"], "Repaired proof")
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
        self.assertEqual(steers[0]["input"][0]["text"], PROMPTS["compaction"].format(directory=self.directory, prompt_file="assignment.txt"))

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
        self.assertEqual(next(session)["outcome"], "proof")
        self.assertEqual(sum(m == "thread/start" for m, _ in runtime.rpc.calls), 1)
        runtime2, session2 = self.session(options={"goal_thread_id": "missing"})
        runtime2.resume_error = "Quota exceeded"
        self.assertEqual(next(session2)["outcome"], "failure")
        self.assertFalse(any(m == "thread/start" for m, _ in runtime2.rpc.calls))

    def test_usage_or_network_failure_never_requests_model_summary(self):
        for messages in ([goal("usageLimited")], [TransportError("Network unavailable")], [completed("failed")]):
            with self.subTest(messages=messages):
                runtime, session = self.session([messages])
                self.assertEqual(next(session)["outcome"], "failure")
                self.assertEqual(runtime.rpc.turns, 1)
                session.close()
                self.assertEqual({p.name for p in self.directory.iterdir()}, set(FILES))

    def test_elapsed_limit_starts_no_model(self):
        runtime, session = self.session()
        runtime.remaining = 0
        self.assertEqual(next(session)["outcome"], "failure")
        self.spawn.assert_not_called()

    def test_live_steering_goes_to_current_turn(self):
        runtime, session = self.session([[]])
        runtime.command = ("steer-1", "Try the invariant in my instruction.")
        def wait_for_steer(rpc):
            self.assertTrue(runtime.steered.wait(2))
            rpc.messages.extend(success())
        runtime.on_empty = wait_for_steer
        self.assertEqual(next(session)["outcome"], "proof")
        steers = [p for m, p in runtime.rpc.calls if m == "turn/steer"]
        self.assertEqual(steers[0]["expectedTurnId"], "turn-1")
        self.assertEqual(steers[0]["input"][0]["text"], runtime.command[1])

    def test_saved_critic_feedback_does_not_change_original_prompt_file(self):
        runtime, session = self.session(initial_instruction="Previously rejected: repair step 2.")
        next(session)
        self.assertEqual((self.directory / "assignment.txt").read_text(), "Exact assignment")
        prompt = next(p["input"][0]["text"] for m, p in runtime.rpc.calls if m == "turn/start")
        self.assertIn("Previously rejected: repair step 2.", prompt)

    def test_turn_payload_final_answer_and_late_goal_completion(self):
        final = {"type": "agentMessage", "phase": "final_answer", "text": "Proof from final turn"}
        _, session = self.session([[completed(items=[final]), goal("complete")]])
        self.assertEqual(next(session)["solution"], final["text"])

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
        result = next(session)
        self.assertEqual(result["outcome"], "failure")
        self.assertIn("time limit", result["output"])
        self.assertEqual(runtime.rpc.turns, 1)
        self.assertTrue(any(m == "turn/interrupt" for m, _ in runtime.rpc.calls))
        self.assertTrue(any(e["kind"] == "failure_result" for e in runtime.events))

    def test_resume_network_error_never_starts_a_replacement_model(self):
        runtime, session = self.session(options={"goal_thread_id": "saved"})
        runtime.resume_error = "thread/resume failed: network unavailable"
        self.assertEqual(next(session)["outcome"], "failure")
        self.assertFalse(any(m in {"thread/start", "turn/start"} for m, _ in runtime.rpc.calls))

    def test_workspace_escape_rejected(self):
        runtime = Runtime([success()])
        session = goal_session(runtime, "Exact assignment", prompts=PROMPTS,
            files={**FILES, "../outside.txt": "bad"}, prompt_file="assignment.txt",
            directory=self.directory, settings=SETTINGS, options={})
        with self.assertRaisesRegex(TransportError, "relative paths"):
            next(session)
        self.spawn.assert_not_called()

    def test_completed_author_cleanup_does_not_emit_events_over_later_critic_stage(self):
        runtime, session = self.session()
        next(session)
        previous_calls, previous_events = list(runtime.rpc.calls), list(runtime.events)
        session.close()
        self.assertEqual(runtime.rpc.calls, previous_calls)
        self.assertEqual(runtime.events, previous_events)
        self.assertTrue(runtime.stopped)

    def test_initial_prompt_crlf_and_whitespace_survive_resumption_exactly(self):
        original = "\r\n Original π statement\r\n  Keep these spaces. \r\n\r\n"
        files = {**FILES, "assignment.txt": original}
        for name, text in files.items():
            (self.directory / name).write_bytes(text.encode("utf-8"))
        runtime = Runtime([success()])
        session = goal_session(runtime, original, prompts=PROMPTS, files=files,
            prompt_file="assignment.txt", directory=self.directory, settings=SETTINGS, options={})
        self.addCleanup(session.close)
        self.assertEqual(next(session)["outcome"], "proof")
        self.assertEqual((self.directory / "assignment.txt").read_bytes(), original.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
