"""Keep critic acceptance evidence after long runs and controller restarts."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ui import server


class CriticCycleRetentionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact proof task")

    def test_legacy_limit_acceptance_survives_recent_window_and_reload(self):
        result = {"kind": "critic_result", "round": 2,
                  "report": {"verdict": "pass", "solution": "Changed proof", "bugs": ""}}
        approval = {"kind": "status", "stage": "critic", "label": "Critic approved",
                    "text": "Reached MAX (2 rounds); using the latest repaired proof."}
        with patch.object(server, "TRACE_LIMIT", 3):
            self.app.add_trace(result)
            self.app.add_trace(approval)
            for _ in range(5):
                self.app.add_trace({"kind": "status", "stage": "final", "text": "Still formatting"})
            self.assertFalse(any(row.get("label") == "Critic approved"
                                 for row in self.app.state["trace"]))
            reloaded = server.App(self.app.trace_file, self.runs)
            for app in (self.app, reloaded):
                retained = app.retained_trace()
                approvals = [row for row in retained if row.get("label") == "Critic approved"]
                self.assertEqual(len(approvals), 1)
                self.assertEqual(approvals[0]["text"], approval["text"])
                decisions = [row for row in retained if row.get("kind") == "critic_result"]
                self.assertLess(retained.index(decisions[0]), retained.index(approvals[0]))

    def test_explicit_acceptance_metadata_is_preserved_exactly_once(self):
        approval = {"kind": "status", "stage": "critic", "label": "Critic approved",
                    "text": "Round 1 passed without edits.", "acceptanceReason": "clean_pass",
                    "round": 1, "roundLimit": 2}
        self.app.add_trace(approval)
        reloaded = server.App(self.app.trace_file, self.runs)
        for app in (self.app, reloaded):
            approvals = [row for row in app.retained_trace() if row.get("label") == "Critic approved"]
            self.assertEqual(len(approvals), 1)
            self.assertEqual({key: approvals[0][key] for key in approval}, approval)
        persisted = [json.loads(line) for line in self.app.trace_file.read_text().splitlines()]
        self.assertEqual(len(persisted), 1)

    def test_only_controller_acceptance_status_is_pinned(self):
        approved = {"kind": "status", "label": "Critic approved"}
        self.assertTrue(server.important_record(approved))
        for other in [
            {**approved, "root": False},
            {**approved, "activityLabel": "Independent audit 1"},
            {**approved, "label": "Critic approved?"},
            {"kind": "status", "label": "Workflow step: critic", "node": "critic"},
            {"kind": "status", "label": "Formatting", "stage": "final"},
            {"kind": "codex_event", "label": "Critic approved"},
        ]:
            with self.subTest(event=other):
                self.assertFalse(server.important_record(other))


if __name__ == "__main__":
    unittest.main()
