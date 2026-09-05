import copy
import json
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import workflow_runner as runner


class GoalIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = os.getcwd()
        os.chdir(directory.name)
        self.addCleanup(os.chdir, previous)

    def test_distinct_goals_keep_their_own_durable_history_and_revision_numbers(self):
        original = runner.builtin_workflow("author_critic")
        workflow = {"prompts": copy.deepcopy(original["prompts"]), "nodes": {}}
        for name, following in (("first", "second_goal"), ("second", "end")):
            workflow["prompts"][f"{name}_assignment"] = f"{name} instructions\n[STATEMENT]"
            goal = copy.deepcopy(original["nodes"]["author"])
            goal.update(prompt=f"{name}_assignment", task=f"state.{name}_task")
            goal["after"].append({"set": {"statement": f"state.{name}_task"}})
            goal["next"]["proof"] = f"{name}_check"
            checker = copy.deepcopy(original["nodes"]["critic"])
            checker["next"]["pass"]["to"] = following
            checker["next"]["reject"] = f"{name}_goal"
            checker["next"]["fixed"]["then"] = following
            workflow["nodes"][f"{name}_goal"] = goal
            workflow["nodes"][f"{name}_check"] = checker

        sessions, resumptions, closed = {}, [], []

        def goal_session(prompt, task, *, memory_directory, node_name, prompts, **options):
            memory = runner.AuthorMemory(memory_directory, prompt, task, prompts=prompts)
            sessions[node_name] = memory
            revision = 0
            try:
                while True:
                    candidate = f"{node_name} author candidate {revision}"
                    memory.record_candidate(
                        candidate, "initial_author" if revision == 0 else "author_revision",
                        revision=revision,
                    )
                    feedback = yield {"outcome": "proof", "solution": candidate, "memory": memory}
                    resumptions.append((node_name, feedback["solution"], feedback["bugs"]))
                    revision += 1
            finally:
                closed.append(node_name)

        reports = []
        for name, audits in (("first", 3), ("second", 2)):
            for number in range(1, audits + 1):
                rejected = number < audits
                reports.append({
                    "checks": [
                        {"focus": f"audit {index}", "verdict": "fail" if rejected else "pass", "report": "checked"}
                        for index in range(3)
                    ],
                    "verdict": "reject" if rejected else "pass",
                    "fixed": False,
                    "solution": f"{name} audited candidate {number}",
                    "bugs": f"{name} unresolved bug {number}" if rejected else "",
                    "memory_update": {
                        "approach_family": f"{name} approach", "approach_result": "audited",
                        "blocked_routes": [],
                        "unresolved_obligations": [f"{name} obligation {number}"] if rejected else [],
                    },
                })

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            path = workspace / "two_goals.yaml"
            path.write_text(json.dumps(workflow), encoding="utf-8")
            with (
                patch.object(runner.Path, "cwd", return_value=workspace),
                patch.object(runner, "author_session", side_effect=goal_session) as goals,
                patch.object(
                    runner, "_parallel_requests",
                    side_effect=[copy.deepcopy(report["checks"]) for report in reports],
                ) as audits,
                patch.object(runner, "structured", side_effect=[(report, json.dumps(report)) for report in reports]) as calls,
                patch.object(runner, "emit"),
            ):
                result = runner.execute(path, {"first_task": "First exact task.", "second_task": "Second exact task."})

            self.assertEqual(goals.call_count, 2, "Each resumed goal must reuse its own session.")
            self.assertEqual(calls.call_count, 5)
            self.assertEqual(audits.call_count, 5)
            self.assertEqual(result["output"], "second audited candidate 2")
            self.assertCountEqual(closed, ["first_goal", "second_goal"])
            self.assertEqual(resumptions, [
                ("first_goal", "first audited candidate 1", "first unresolved bug 1"),
                ("first_goal", "first audited candidate 2", "first unresolved bug 2"),
                ("second_goal", "second audited candidate 1", "second unresolved bug 1"),
            ])
            self.assertNotEqual(sessions["first_goal"].directory, sessions["second_goal"].directory)
            self.assertFalse((workspace / runner.AUTHOR_MEMORY_FILENAME).exists())
            for name, revisions in (("first", [0, 1, 2]), ("second", [0, 1])):
                memory = sessions[f"{name}_goal"]
                self.assertTrue(memory.directory.is_relative_to(workspace))
                persisted = json.loads(memory.memory_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted, memory.data)
                self.assertIn(f"{name} instructions", memory.anchor_path.read_text(encoding="utf-8"))
                self.assertIn(f"{name.title()} exact task.", memory.anchor_path.read_text(encoding="utf-8"))
                audited = [attempt for attempt in persisted["attempts"] if attempt["source"] in {"critic_safe_fix", "critic_repair"}]
                self.assertEqual([attempt["revision"] for attempt in audited], revisions)
                self.assertEqual(audited[-1]["status"], "approved")
                self.assertEqual(persisted["unresolvedObligations"], [])

    def test_goal_model_thread_runs_beside_its_isolated_ledger(self):
        calls, closed = [], []

        class FakeRPC:
            def __init__(self, process, record):
                self.record = record
                self.events = deque([
                    {"method": "item/completed", "params": {
                        "threadId": "root-thread", "turnId": "turn-1",
                        "item": {"type": "agentMessage", "id": "answer", "text": "Complete candidate"},
                    }},
                    {"method": "thread/goal/updated", "params": {
                        "threadId": "root-thread", "goal": {"threadId": "root-thread", "status": "complete"},
                    }},
                    {"method": "turn/completed", "params": {
                        "threadId": "root-thread", "turn": {"id": "turn-1", "status": "completed"},
                    }},
                ])

            def call(self, method, params):
                calls.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "root-thread"}}
                if method == "turn/start":
                    return {"turn": {"id": "turn-1"}}
                return {}

            def send(self, message):
                pass

            def read(self):
                message = self.events.popleft()
                self.record(message)
                return message

            def close(self):
                closed.append(True)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            memory_directory = workspace / "isolated_goal"
            with (
                patch.object(runner.Path, "cwd", return_value=workspace),
                patch.object(runner, "codex", return_value="codex-fake"),
                patch.object(runner.subprocess, "Popen"),
                patch.object(runner, "RPC", FakeRPC),
                patch.object(runner, "emit"),
            ):
                session = runner.author_session(
                    "Exact assignment", "Exact task", thinking_hours=1,
                    memory_directory=memory_directory, node_name="custom_goal",
                )
                try:
                    result = next(session)
                finally:
                    session.close()

            started = next(params for method, params in calls if method == "thread/start")
            self.assertEqual(started["cwd"], str(memory_directory.resolve()))
            self.assertEqual(result["solution"], "Complete candidate")
            self.assertTrue((memory_directory / runner.AUTHOR_MEMORY_FILENAME).is_file())
            self.assertTrue((memory_directory / runner.AUTHOR_ANCHOR_FILENAME).is_file())
            self.assertFalse((workspace / runner.AUTHOR_MEMORY_FILENAME).exists())
            self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
