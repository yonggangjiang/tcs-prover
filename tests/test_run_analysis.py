import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.analyze_run import analyze_transcript


def usage(thread, inputs, cached, output, reasoning=0, root=True):
    return {"kind": "codex_event", "root": root, "event": {
        "method": "thread/tokenUsage/updated", "params": {
            "threadId": thread, "tokenUsage": {"total": {
                "inputTokens": inputs, "cachedInputTokens": cached,
                "outputTokens": output, "reasoningOutputTokens": reasoning,
                "totalTokens": inputs + output}}}}}


def report(tmp_path, records):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return analyze_transcript(path)


class RunAnalysisTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp_path = Path(folder.name)

    def test_cumulative_snapshots_duplicates_resumed_author_and_child(self):
        result = report(self.tmp_path, [
            usage("a", 100, 50, 20, 10), usage("a", 100, 50, 20, 10),
            usage("a", 300, 250, 40, 25),
            usage("child", 80, 40, 10, root=False),
            usage("a", 60, 20, 10, 5),
            usage("child", 30, 10, 5, root=False),
            {"kind": "status", "label": "Author cache usage", "inputTokens": 999999},
        ])
        assert result["observed_usage"]["input_tokens"] == 470
        assert result["observed_usage"]["output_tokens"] == 65
        assert result["observed_usage"]["reasoning_output_tokens"] == 30
        assert result["observed_usage"]["total_tokens"] == 535
        assert result["usage_by_scope"]["root"]["input_tokens"] == 360
        assert result["usage_by_scope"]["subagent"]["input_tokens"] == 110
        assert sum(t["counter_resets"] for t in result["threads"]) == 2
        assert sum(t["duplicate_snapshots"] for t in result["threads"]) == 1


    def test_missing_audit_usage_is_unknown_and_reasoning_is_not_double_counted(self):
        result = report(self.tmp_path, [
            {"kind": "research_audit", "status": "completed"},
            {"kind": "research_audit", "status": "completed", "usage": {"input_tokens": 12}},
            {"kind": "codex_event", "event": {"type": "thread.started", "thread_id": "exec-1"}},
            {"kind": "codex_event", "event": {"type": "turn.completed", "usage": {
                "input_tokens": 100, "output_tokens": 30, "reasoning_output_tokens": 20}}},
        ])
        assert result["completed_audits_without_usage"] == 1
        assert result["completed_audits_with_usage"] == 1
        assert result["observed_usage"]["output_tokens"] == 30
        assert "total_tokens" not in result["observed_usage"]
        assert "cached_input_tokens" not in result["observed_usage"]
        assert result["cost_usd"] is None


    def test_items_are_deduplicated_by_thread_and_identity_not_command(self):
        def command(thread, identity):
            return {"kind": "codex_event", "root": True, "event": {
                "method": "item/completed", "params": {"threadId": thread,
                    "item": {"id": identity, "type": "commandExecution",
                             "command": "cat PROVED.md", "aggregatedOutput": "proof"}}}}
        result = report(self.tmp_path, [command("a", "1"), command("a", "1"),
                                   command("a", "2"), command("child", "1")])
        assert result["duplicate_item_events"] == 1
        assert result["command_calls"] == 3
        assert result["distinct_commands"] == 1
        assert result["command_output_characters"] == 15


    def test_truncated_tail_and_non_object_records_are_reported(self):
        path = self.tmp_path / "transcript.jsonl"
        path.write_text(json.dumps(usage("a", 100, 50, 20)) + '\n[]\n{"kind":')
        result = analyze_transcript(path)
        assert result["malformed_records"] == 2
        assert result["observed_usage"]["input_tokens"] == 100


    def test_audit_provider_usage_preserves_semantics_and_deduplicates_request(self):
        measured = {"kind": "audit_usage", "requestId": "audit1", "provider": "claude",
                    "coverage": "reported", "outcome": "completed", "usageRecords": [
                        {"basis": "result", "usage": {"input_tokens": 100,
                         "cache_read_input_tokens": 200}, "totalCostUsd": 0.2}]}
        result = report(self.tmp_path, [measured, measured, {"kind": "audit_usage",
            "requestId": "audit2", "provider": "kimi", "coverage": "missing",
            "outcome": "cancelled", "usageRecords": []}])
        assert result["audit_calls_with_reported_usage"] == 1
        assert result["audit_calls_with_missing_usage"] == 1
        assert len(result["audit_call_measurements"]) == 2
        assert result["observed_usage"] == {"cache_accounting_complete": False}
        assert result["cost_usd"] is None

    def test_partial_cumulative_fields_keep_baselines_until_a_reset(self):
        partial = {"kind": "codex_event", "root": True, "event": {
            "method": "thread/tokenUsage/updated", "params": {
                "threadId": "a", "tokenUsage": {"total": {"outputTokens": 20}}}}}
        result = report(self.tmp_path, [usage("a", 100, 50, 10), partial,
                                       usage("a", 200, 150, 30)])
        self.assertEqual(result["observed_usage"]["input_tokens"], 200)
        self.assertEqual(result["observed_usage"]["output_tokens"], 30)
        # The lower output identifies a new epoch even when its input is absent.
        partial["event"]["params"]["tokenUsage"]["total"]["outputTokens"] = 5
        result = report(self.tmp_path, [usage("a", 100, 50, 10), partial,
                                       usage("a", 20, 10, 8)])
        self.assertEqual(result["observed_usage"]["input_tokens"], 120)
        self.assertEqual(result["observed_usage"]["output_tokens"], 18)
        self.assertEqual(result["threads"][0]["counter_resets"], 1)

    def test_completed_audit_coverage_joins_request_ids_and_checks_numeric_evidence(self):
        calls = [
            {"kind": "research_audit", "status": "completed", "requestId": identity}
            for identity in ("measured", "empty", "wrong", "cost")
        ]
        calls.extend([
            {"kind": "audit_usage", "requestId": "measured", "outcome": "completed",
             "coverage": "reported", "usageRecords": [{"modelUsage": {
                 "model": {"inputTokens": 0, "outputTokens": 3}}}]},
            {"kind": "audit_usage", "requestId": "empty", "outcome": "completed",
             "coverage": "reported", "usageRecords": [{"modelUsage": {
                 "model": {"unknown": 20, "inputTokens": True}}}]},
            {"kind": "audit_usage", "requestId": "wrong", "outcome": "cancelled",
             "coverage": "reported", "usageRecords": [{"usage": {"input_tokens": 12}}]},
            {"kind": "audit_usage", "requestId": "cost", "outcome": "completed",
             "coverage": "reported", "usageRecords": [{"totalCostUsd": 0.0}]},
        ])
        result = report(self.tmp_path, calls)
        self.assertEqual(result["completed_audits_with_usage"], 2)
        self.assertEqual(result["completed_audits_without_usage"], 2)
        self.assertEqual(result["audit_calls_with_reported_usage"], 3)
        self.assertEqual(result["audit_calls_with_missing_usage"], 1)

    def test_interleaved_structured_usage_is_not_silently_attributed(self):
        def event(kind, **fields):
            return {"kind": "codex_event", "event": {"type": kind, **fields}}
        result = report(self.tmp_path, [
            event("thread.started", thread_id="a"),
            event("thread.started", thread_id="b"),
            event("turn.completed", usage={"input_tokens": 100}),
            event("turn.completed", usage={"input_tokens": 200}),
            event("thread.started", thread_id="c"),
            event("turn.completed", usage={"input_tokens": 30}),
        ])
        self.assertEqual(len(result["ambiguous_structured_usage"]), 2)
        self.assertEqual(result["observed_usage"]["input_tokens"], 30)

    def test_missing_cache_in_one_thread_makes_aggregate_cache_unknown(self):
        partial = usage("child", 200, 150, 10, root=False)
        del partial["event"]["params"]["tokenUsage"]["total"]["cachedInputTokens"]
        result = report(self.tmp_path, [usage("a", 100, 80, 10), partial])
        observed = result["observed_usage"]
        self.assertEqual(observed["input_tokens"], 300)
        self.assertEqual(observed["cached_input_tokens"], 80)
        self.assertFalse(observed["cache_accounting_complete"])
        self.assertNotIn("uncached_input_tokens", observed)
        self.assertNotIn("cache_hit_percent", observed)
        self.assertTrue(result["usage_by_scope"]["root"]["cache_accounting_complete"])
        self.assertFalse(result["usage_by_scope"]["subagent"]["cache_accounting_complete"])

    def test_missing_cache_in_resumed_epoch_is_not_treated_as_zero(self):
        partial = usage("a", 20, 15, 2)
        del partial["event"]["params"]["tokenUsage"]["total"]["cachedInputTokens"]
        result = report(self.tmp_path, [usage("a", 100, 80, 10), partial,
                                       usage("a", 40, 30, 4)])
        observed = result["observed_usage"]
        self.assertEqual(observed["input_tokens"], 140)
        self.assertEqual(observed["cached_input_tokens"], 110)
        self.assertFalse(observed["cache_accounting_complete"])
        self.assertNotIn("uncached_input_tokens", observed)
        self.assertNotIn("cache_hit_percent", observed)
