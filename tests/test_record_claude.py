"""How a live job's record keeps Claude Code's calls (plan v3 section 1.3-1.4): every stream-json line after TCS
Prover's redaction, each saved session copied before it is deleted, and the author's session when it stops.

Runs the stand-in Claude CLI of tests/test_claude_cli.py (no model, no network).
"""

import gzip
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, records
from ui import model_calls

import test_claude_cli as claude_test


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class ClaudeRecordTests(claude_test.FakeClaudeCase):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "store" / "records" / "run-1"
        for change in (mock.patch.dict(os.environ, {records.RECORD_ENV: str(self.folder)}),
                       mock.patch.object(records, "_RECORDER", None)):
            change.start()
            self.addCleanup(change.stop)
        self.addCleanup(records.close)

    def saved(self, session):
        """The CLI's own saved files of ``session`` (the record's copies left out)."""

        return [path for path in self.root.rglob(f"{session}.jsonl") if self.folder not in path.parents]

    def streams(self):
        return {path.name: gzip.decompress(path.read_bytes()).decode().splitlines()
                for path in (self.folder / "claude").glob("*.jsonl.gz")}

    def test_a_structured_call_keeps_every_line_redacted_and_its_session_before_deleting_it(self):
        model = self.api_model()
        leak = {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {
            "id": "msg_1", "content": [{"type": "text", "text": f"the key is {claude_test.SECRET}"}]}}}
        self.script([["init", leak, {"structured": {"statement": "S", "notes": ""}}]])
        with records.labelled(role="reviewer", label="Statement review"):
            runtime.structured("Review.", claude_test.SCHEMA, "review", model=model, summary="concise")
        [(name, lines)] = self.streams().items()
        self.assertTrue(any("the key is [redacted]" in line for line in lines))
        self.assertNotIn(claude_test.SECRET, "\n".join(lines))
        self.assertEqual(json.loads((self.folder / "claude" / name.replace(".jsonl.gz", ".json")).read_text())
                         ["role"], "reviewer")
        # Its saved session went only after the record kept a copy that reads back the same.
        session = next(row["argv"][row["argv"].index("--session-id") + 1] for row in self.records("start"))
        copy = self.folder / "sources" / "claude" / "-fake-project" / f"{session}.jsonl.gz"
        self.assertIn(b'"type": "assistant"', records.read_source(copy))
        self.assertEqual(self.saved(session), [])
        calls = records.Record(self.folder).calls.read()
        self.assertEqual([(call["program"], call["callId"]) for call in calls], [("claude", "msg_1")])
        # The prompt went to the CLI's input, which its stream never repeats: the record keeps it for the request.
        [prompt] = model_calls._claude_detail(records.Record(self.folder), calls[0])["sent"]["newItems"]
        self.assertEqual(prompt["type"], "text")
        self.assertIn("Review.", prompt["item"]["text"])

    def test_a_session_whose_copy_fails_is_kept(self):
        model = self.api_model()
        self.script([["init", {"structured": {"statement": "S", "notes": ""}}]])
        with mock.patch.object(records.Recorder, "keep_claude_session", lambda recorder, home, session: False):
            runtime.structured("Review.", claude_test.SCHEMA, "review", model=model)
        session = next(row["argv"][row["argv"].index("--session-id") + 1] for row in self.records("start"))
        self.assertEqual(len(self.saved(session)), 1)

    def test_the_author_s_session_is_copied_when_it_stops_and_its_stream_kept(self):
        copies = []
        with mock.patch.object(records.Recorder, "copy_claude_session",
                               lambda recorder, home, session: copies.append(session)):
            self.script([["init", {"structured": {"goal_status": "complete", "final_answer": "PROOF_FILE: proof.md",
                                                  "progress_note": ""}}]])
            session = self.goal()
            self.assertEqual(next(session)["outcome"], "done")
            self.assertEqual(copies, [])
            session.close()
        self.assertEqual(len(copies), 1)
        [(name, lines)] = self.streams().items()
        self.assertTrue(lines)
        self.assertEqual(json.loads((self.folder / "claude" / name.replace(".jsonl.gz", ".json")).read_text())
                         ["role"], "author")


if __name__ == "__main__":
    unittest.main()
