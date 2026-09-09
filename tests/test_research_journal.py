"""Contract tests for durable research history; no model/network calls required."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research_journal import DATABASE_FILENAME, ResearchJournal, copy_research_archive


class ResearchJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "run"
        self.statement = "For every assignment, establish the exact requested claim. 数学 ∀x."
        self.journal = ResearchJournal(self.directory, self.statement)
        self.addCleanup(self.journal.close)

    def test_full_history_survives_large_number_of_attempts_and_reopen(self):
        proof = "Full proof, including each intermediate step.\n" * 3000
        first = self.journal.commit("review", {"status": "refuted", "evidence": "pigeonhole obstruction", "proof": proof})
        for index in range(205):
            self.journal.commit("attempt", {"number": index, "result": "distinct recorded experiment"})
        self.journal.close()
        with ResearchJournal(self.directory, self.statement) as reopened:
            self.assertEqual(len(list(reopened.events())), 206)
            self.assertEqual(reopened.get(first)["payload"]["proof"], proof)
            self.assertEqual(reopened.search("pigeonhole obstruction")[0]["id"], first)
            self.assertEqual(len(reopened.recent()), 12)
            self.assertEqual(reopened.recent(limit=2)[0]["id"], "e000205")
            self.assertIn(proof, (reopened.views / "records" / f"{first}.md").read_text())
            self.assertNotIn(f"[{first}", (reopened.views / "INDEX.md").read_text())
            self.assertIn(f"[{first}", (reopened.views / "FAILED.md").read_text())

    def test_prompt_versions_never_change_statement_identity(self):
        self.journal.commit("configuration", {"prompt_version": "first wording"}, {"stage": "propose"})
        self.journal.close()
        with ResearchJournal(self.directory, self.statement) as reopened:
            reopened.commit("configuration", {"prompt_version": "improved wording"})
            self.assertEqual(len(list(reopened.events())), 2)
            self.assertEqual(reopened.get_state("stage"), "propose")

    def test_statement_identity_mismatch_preserves_original_database(self):
        self.journal.commit("attempt", {"important": "retained"})
        self.journal.close()
        before = (self.directory / DATABASE_FILENAME).read_bytes()
        with self.assertRaisesRegex(ValueError, "different statement"):
            ResearchJournal(self.directory, self.statement + " ")
        self.assertEqual((self.directory / DATABASE_FILENAME).read_bytes(), before)
        with ResearchJournal(self.directory, self.statement) as original:
            self.assertEqual(original.get("e000001")["payload"]["important"], "retained")

    def test_event_and_checkpoint_rollback_together_on_database_error(self):
        self.journal.commit("attempt", {"stage": "original"}, {"phase": "before"})
        self.journal._db.execute("""CREATE TRIGGER checkpoint_test BEFORE INSERT ON checkpoint
            WHEN NEW.key = 'explode' BEGIN SELECT RAISE(ABORT, 'deliberate test failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "deliberate test failure"):
            self.journal.commit("review", {"evidence": "shouldrollbacktoken"},
                                {"phase": "after", "explode": True})
        self.assertEqual(self.journal.get_state("phase"), "before")
        self.assertEqual(len(list(self.journal.events())), 1)
        self.assertEqual(self.journal.search("shouldrollbacktoken"), [])
        self.assertFalse((self.journal.views / "records" / "e000002.md").exists())

    def test_search_treats_query_as_data_and_has_complete_fallback(self):
        first = self.journal.commit("novelty_review", {"decision": "duplicate", "reason": "greedy allocation repeats the bottleneck"})
        second = self.journal.commit("attempt", {"mechanism": "spectral expansion"})
        self.assertEqual(self.journal.search('greedy OR " DROP TABLE events; --')[0]["id"], first)
        self.assertEqual(len(list(self.journal.events())), 2)
        self.assertEqual(self.journal.search("spectral")[0]["id"], second)
        self.journal._fts = False
        self.assertEqual(self.journal.search("greedy allocation")[0]["id"], first)
        self.assertEqual(self.journal.search(""), [])
        self.assertEqual(self.journal.search("greedy", limit=0), [])

    def test_backup_restores_checkpoint_and_independent_full_archive(self):
        event = self.journal.commit("research_result", {"status": "incomplete", "details": "all work retained"},
                                    {"next_action": {"phase": "novelty", "proposal": "new idea"}})
        destination = Path(self.temporary.name) / "continued"
        self.assertEqual(self.journal.backup_to(destination), destination / DATABASE_FILENAME)
        with ResearchJournal(destination, self.statement) as restored:
            self.assertEqual(restored.get(event), self.journal.get(event))
            self.assertEqual(restored.get_state("next_action"), self.journal.get_state("next_action"))
            restored.commit("attempt", {"new": True})
            self.assertEqual(len(list(restored.events())), 2)
        self.assertEqual(len(list(self.journal.events())), 1)
        self.assertTrue((destination / "research" / "records" / f"{event}.md").is_file())
        with self.assertRaises(FileExistsError):
            self.journal.backup_to(destination)
        with ResearchJournal(destination, self.statement) as restored:
            self.assertEqual(len(list(restored.events())), 2)

    def test_copy_helper_is_read_only_on_source_and_legacy_missing_is_false(self):
        self.journal.commit("review", {"verdict": "refuted", "evidence": "counterexample"}, {"attempt": 1})
        self.journal.close()
        shutil.rmtree(self.directory / "research")
        source_bytes = (self.directory / DATABASE_FILENAME).read_bytes()
        source_mtime = (self.directory / DATABASE_FILENAME).stat().st_mtime_ns
        destination = Path(self.temporary.name) / "copied"
        self.assertTrue(copy_research_archive(self.directory, destination))
        self.assertEqual((self.directory / DATABASE_FILENAME).read_bytes(), source_bytes)
        self.assertEqual((self.directory / DATABASE_FILENAME).stat().st_mtime_ns, source_mtime)
        self.assertFalse((self.directory / "research").exists())
        self.assertTrue((destination / "research" / "records" / "e000001.md").is_file())
        self.assertFalse(copy_research_archive(Path(self.temporary.name) / "legacy", destination))

    def test_unicode_human_views_preserve_all_fields_and_are_private(self):
        payload = {"approach_family": "局部选择 — greedy", "status": "refuted",
                   "counterexample": {"workers": ["Alice", "Bob"], "jobs": ["A→1,2", "B→1"]},
                   "reopen_condition": "Provide a mechanism protecting the restricted worker.",
                   "proof": "∀x ∈ ℝ, consider\nα + β = γ\n```literal fence```"}
        event = self.journal.commit("critic_review", payload, {"current": payload})
        card = (self.journal.views / "records" / f"{event}.md").read_text(encoding="utf-8")
        self.assertIn("局部选择 — greedy", card)
        self.assertIn("A→1,2", card)
        self.assertIn(payload["reopen_condition"], card)
        self.assertIn(payload["proof"], card)
        self.assertIn(event, (self.journal.views / "FAILED.md").read_text())
        self.assertIn(self.statement, (self.journal.views / "STATEMENT.md").read_text())
        self.assertIn("局部选择", (self.journal.views / "STATE.md").read_text())
        self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.journal.views / "records" / f"{event}.md").stat().st_mode & 0o777, 0o600)

    def test_corrupted_database_is_not_overwritten(self):
        self.journal.close()
        corruption = b"This is not SQLite. Retain evidence for recovery.\x00\xff"
        self.journal.path.write_bytes(corruption)
        with self.assertRaises(sqlite3.DatabaseError):
            ResearchJournal(self.directory, self.statement)
        self.assertEqual(self.journal.path.read_bytes(), corruption)
        with self.assertRaises(sqlite3.DatabaseError):
            copy_research_archive(self.directory, Path(self.temporary.name) / "bad-copy")
        self.assertEqual(self.journal.path.read_bytes(), corruption)

    def test_unrelated_database_is_not_repurposed(self):
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        db = sqlite3.connect(other / DATABASE_FILENAME)
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.commit()
        db.close()
        before = (other / DATABASE_FILENAME).read_bytes()
        with self.assertRaisesRegex(ValueError, "not a research archive"):
            ResearchJournal(other, self.statement)
        self.assertEqual((other / DATABASE_FILENAME).read_bytes(), before)

    def test_export_write_error_is_visible_and_committed_event_is_recoverable(self):
        with patch("research_journal._atomic_text", side_effect=PermissionError("disk refused write")):
            with self.assertRaisesRegex(PermissionError, "disk refused write"):
                self.journal.commit("attempt", {"result": "valuable discovery"}, {"phase": "review"})
        self.assertEqual(self.journal.get("e000001")["payload"], {"result": "valuable discovery"})
        self.assertEqual(self.journal.get_state("phase"), "review")
        self.journal.close()
        with ResearchJournal(self.directory, self.statement) as restored:
            card = restored.views / "records" / "e000001.md"
            self.assertIn("valuable discovery", card.read_text())
            card.write_text("damaged derived view")
            restored.rebuild_views()
            self.assertIn("valuable discovery", card.read_text())

    def test_database_write_error_is_not_swallowed(self):
        self.journal._db.execute("PRAGMA query_only = ON")
        with self.assertRaises(sqlite3.OperationalError):
            self.journal.commit("attempt", {"result": "cannot persist"})
        self.assertEqual(list(self.journal.events()), [])

    def test_events_cannot_be_updated_or_deleted(self):
        self.journal.commit("attempt", {"original": True})
        for sql in ("UPDATE events SET kind = 'replacement'", "DELETE FROM events"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                self.journal._db.execute(sql)
        self.assertEqual(self.journal.get("e000001")["payload"], {"original": True})

    def test_parallel_commits_are_serialized_without_lost_records(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(lambda number: self.journal.commit("attempt", {"number": number}), range(12)))
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual({event["payload"]["number"] for event in self.journal.events()}, set(range(12)))

    def test_invalid_payload_cannot_partially_change_checkpoint(self):
        with self.assertRaises(ValueError):
            self.journal.commit("attempt", {"bad": float("nan")}, {"phase": "incorrect"})
        with self.assertRaises(TypeError):
            self.journal.commit("attempt", {}, {"phase": object()})
        self.assertEqual(list(self.journal.events()), [])
        self.assertIsNone(self.journal.get_state("phase"))
        self.assertEqual(self.journal.get_state("missing", "fallback"), "fallback")

    def test_filtered_search_applies_kinds_before_result_limit(self):
        target = self.journal.commit("research_review", {"verdict": "refuted", "reason": "greedy bottleneck"})
        for _ in range(20):
            self.journal.commit("model_request", {"prompt": "greedy bottleneck"})
        for use_fts in (True, False):
            self.journal._fts = use_fts
            results = self.journal.search("greedy bottleneck", limit=1, kinds=["research_review", "critic"])
            self.assertEqual([record["id"] for record in results], [target])
        self.assertEqual(self.journal.search("greedy", kinds=[]), [])

    def test_readable_checkpoint_summarizes_caches_and_large_bodies(self):
        proof = "full-proof-step " * 5000
        candidate = self.journal.commit("candidate", {"solution": proof, "status": "awaiting_critic"})
        self.journal.commit("checkpoint", {}, {
            "response:hash": {"text": "large cached output " * 5000},
            "raw:hash": "large raw output " * 5000,
            "retrieval:hash": {"text": "large retrieval " * 5000},
            "mechanism:hash": "historical mechanism", "proof:hash": candidate,
            "import:hash": True, "currentAttemptId": candidate,
            "candidate": proof, "candidate_status": "awaiting_critic",
            "research_checkpoint": {"phase": "candidate", "solution": proof},
            "workflow_checkpoint": {"node": "critic", "state": {"solution": proof}},
            "recent_families": ["greedy", "structural"],
            "open_issues": [{"id": "issue1", "description": "missing compatibility argument"}],
            "workflow_version": "new-version"})
        state = (self.journal.views / "STATE.md").read_text()
        self.assertLess(len(state), 7000)
        self.assertNotIn("large cached output", state)
        self.assertNotIn("large raw output", state)
        self.assertNotIn("large retrieval", state)
        self.assertNotIn(proof, state)
        self.assertIn(f"records/{candidate}.md", state)
        self.assertIn("missing compatibility argument", state)
        self.assertIn("new-version", state)
        self.assertEqual(self.journal.get_state("candidate"), proof)
        self.assertIn(proof, (self.journal.views / "records" / f"{candidate}.md").read_text())

    def test_reviewed_results_index_does_not_promote_candidates_to_proofs(self):
        event = self.journal.commit("research_review", {
            "review": {"verdict": "incomplete", "reusable_results": "The local lemma holds under these assumptions."}})
        notebook = (self.journal.views / "PROVED.md").read_text()
        self.assertIn(event, notebook)
        self.assertIn("incomplete", notebook)
        self.assertIn("not formal proof verification", notebook)
        rejected = self.journal.commit("novelty", {"accepted": False, "reason": "same mechanism"})
        self.assertIn(rejected, (self.journal.views / "FAILED.md").read_text())


if __name__ == "__main__":
    unittest.main()
