"""Read-only enrichment must never recover or relaunch a live legacy run."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from ui.batches import BatchCatalog
from ui.legacy_overview import augment_legacy


class LegacyOverviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "problem-01"
        self.run.mkdir()
        (self.run / "checked-statement.md").write_text("# Statement sent directly to the proof author\n\nExact assignment\n")
        self.status = self.root / "status.json"
        self.now = datetime(2026, 10, 16, 11, tzinfo=timezone.utc)
        self.state = {"runId": self.run.name, "phase": "running", "activeNode": "author", "thinkingHours": 0,
                      "startedAt": "2026-10-16T10:00:00Z", "resumedAt": "2026-10-16T10:30:00Z", "elapsedSeconds": 45,
                      "retrievalPolicyPath": "/controller/policy.json"}
        self.status.write_text(json.dumps({"updatedAtUtc": "2026-10-16T10:59:58Z", "runs": [
            {**self.state, "pid": 12345}]}))

    def augment(self, payload=None, **kwargs):
        return augment_legacy(payload or self.state, runs_root=self.root, status_path=self.status,
                              now=self.now, retrieval_loader=lambda path: {"mode": "historical", "policy": path}, **kwargs)

    def test_live_enrichment_is_read_only_and_preserves_budget_phase_and_trace(self):
        self.state.update(trace=[{"kind": "status"}], traceFrom=10, traceVersion=11)
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch("ui.legacy_overview.os.kill") as check:
            result = self.augment()
        check.assert_called_once_with(12345, 0)
        self.assertTrue(result["processAlive"])
        self.assertEqual(result["activeElapsedSeconds"], 1845)
        self.assertEqual(result["statement"].strip(), "Exact assignment")
        self.assertEqual(result["thinkingHours"], 0)
        for name in ("phase", "trace", "traceFrom", "traceVersion"):
            self.assertEqual(result[name], self.state[name])
        self.assertNotIn("processAlive", self.state)
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_stale_heartbeat_reports_unknown_not_dead(self):
        self.status.write_text(json.dumps({"updatedAtUtc": "2026-10-16T10:00:00Z", "runs": [{**self.state, "pid": 12345}]}))
        with patch("ui.legacy_overview.os.kill") as check:
            result = self.augment()
        check.assert_not_called()
        self.assertIsNone(result["processAlive"])
        self.assertIsNone(result["workerActive"])

    def test_paused_and_terminal_timers_are_frozen_and_only_final_records_mean_success(self):
        (self.run / "pause.json").write_text('{"status":"paused","elapsedSeconds":121}')
        self.state["phase"] = "paused"
        self.assertEqual(self.augment()["activeElapsedSeconds"], 121)
        self.state.update(phase="done", finishedAt="2026-10-16T10:35:00Z", output="Partial work")
        (self.run / "transcript.jsonl").write_text('{"kind":"final_result","output":"  "}\n')
        result = self.augment()
        self.assertEqual(result["resultKind"], "")
        self.assertEqual(result["activeElapsedSeconds"], 345)
        (self.run / "transcript.jsonl").write_text('{"kind":"final_result","output":"Complete proof"}\n')
        self.assertEqual(self.augment()["resultKind"], "final")

    def test_job_lists_do_not_read_statements_and_invalid_paths_are_not_read(self):
        with patch("ui.legacy_overview._statement") as read:
            result = self.augment({"jobs": [self.state]}, is_job_list=True)
        read.assert_not_called()
        self.assertEqual(result["jobs"][0]["retrieval"]["mode"], "historical")
        self.assertEqual(result["jobs"][0]["stageLabel"], "Proof author")
        self.state["runId"] = "../escape"
        self.assertEqual(self.augment(), self.state)

    def test_main_legacy_mode_is_live_unless_disabled_was_explicitly_recorded(self):
        for mode in (None, "live", "disabled"):
            with self.subTest(mode=mode):
                source = {key: value for key, value in self.state.items() if key != "retrievalPolicyPath"}
                if mode is not None:
                    source["retrievalMode"] = mode
                original = dict(source)
                result = augment_legacy(source, runs_root=self.root, status_path=self.status, now=self.now)
                expected = mode or "live"
                self.assertEqual(result["retrievalMode"], expected)
                self.assertEqual(result["retrieval"]["mode"], expected)
                self.assertEqual(result["retrievalCutoffUtc"], "-1" if mode == "disabled" else "")
                self.assertEqual(source, original)

    def test_recovers_old_acceptances_without_changing_incremental_trace(self):
        approval = {"kind": "status", "stage": "critic", "label": "Critic approved",
                    "time": "2026-10-16T10:20:00Z",
                    "text": "Reached MAX (2 rounds); using the latest repaired proof."}
        recent = {"kind": "status", "stage": "final", "text": "Still formatting"}
        records = [approval, *([recent] * 1600)]
        (self.run / "transcript.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
        self.state.update(trace=[recent], traceFrom=1600, traceVersion=1601)
        original = json.loads(json.dumps(self.state))
        with patch("ui.server.App", side_effect=AssertionError("Never restore a live job")):
            result = self.augment()
        self.assertEqual(result["criticAcceptances"], [approval])
        for key in ("trace", "traceFrom", "traceVersion"):
            self.assertEqual(result[key], original[key])
        self.assertEqual(self.state, original)
        self.assertEqual((self.run / "transcript.jsonl").read_text(),
                         "".join(json.dumps(record) + "\n" for record in records))

    def test_acceptances_only_include_root_controller_decisions(self):
        approval = {"kind": "status", "label": "Critic approved", "acceptanceReason": "clean_pass",
                    "round": 1, "roundLimit": 3, "time": "2026-10-16T10:20:00Z"}
        others = [{**approval, "root": False}, {**approval, "activityLabel": "Independent audit 1"},
                  {**approval, "label": "Critic approved?"}, {**approval, "kind": "codex_event"}, []]
        (self.run / "transcript.jsonl").write_text("invalid\n" + "".join(
            json.dumps(record) + "\n" for record in [*others, approval]))
        self.assertEqual(self.augment()["criticAcceptances"], [approval])

    def test_one_cached_scan_serves_outcome_and_acceptances_and_invalidates_on_changes(self):
        self.state["phase"] = "done"
        transcript = self.run / "transcript.jsonl"
        approval = {"kind": "status", "label": "Critic approved", "text": "Round 1 passed without edits."}
        final = {"kind": "final_result", "output": "Complete proof"}
        transcript.write_text(json.dumps(approval) + "\n" + json.dumps(final) + "\n")
        with patch.object(BatchCatalog, "_open", autospec=True, side_effect=BatchCatalog._open) as opened:
            first = self.augment()
            first["criticAcceptances"][0]["text"] = "Browser response mutation"
            second = self.augment()
            self.assertEqual(second["criticAcceptances"], [approval])
            self.assertEqual(second["resultKind"], "final")
            self.assertEqual(opened.call_count, 1)
            replacement = {**approval, "text": "Reached MAX (2 rounds); using the latest repaired proof."}
            transcript.write_text(json.dumps(replacement) + "\n" + json.dumps(final) + "\n")
            self.assertEqual(self.augment()["criticAcceptances"], [replacement])
            self.assertEqual(opened.call_count, 2)

    def test_replacing_transcript_with_same_size_and_timestamp_invalidates_cache(self):
        transcript = self.run / "transcript.jsonl"
        first = {"kind": "status", "label": "Critic approved", "text": "First"}
        second = {**first, "text": "Other"}
        transcript.write_text(json.dumps(first) + "\n")
        self.assertEqual(self.augment()["criticAcceptances"], [first])
        before = transcript.stat()
        replacement = self.run / "replacement.jsonl"
        replacement.write_text(json.dumps(second) + "\n")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(transcript)
        self.assertEqual(transcript.stat().st_size, before.st_size)
        self.assertEqual(self.augment()["criticAcceptances"], [second])

    def test_symlinked_transcript_cannot_supply_acceptance_or_success(self):
        outside = self.root / "another-job-transcript.jsonl"
        outside.write_text(json.dumps({"kind": "status", "label": "Critic approved"}) + "\n"
                           + json.dumps({"kind": "final_result", "output": "Other job"}) + "\n")
        (self.run / "transcript.jsonl").symlink_to(outside)
        self.state["phase"] = "done"
        result = self.augment()
        self.assertEqual(result["criticAcceptances"], [])
        self.assertEqual(result["resultKind"], "")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Named pipes require POSIX")
    def test_nonregular_transcript_is_rejected_without_blocking(self):
        os.mkfifo(self.run / "transcript.jsonl")
        self.assertEqual(self.augment()["criticAcceptances"], [])


if __name__ == "__main__":
    unittest.main()
