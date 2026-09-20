"""Incremental audit hints preserve independent coverage and exact custom prompts."""

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ui import audits
from ui.audit_context import audit_identity, audit_prompt, research_snapshot


class AuditContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "APPROACHES").mkdir()
        (self.root / "INITIAL_PROMPT.md").write_text("Exact task")
        (self.root / "PROVED.md").write_text("Lemma")
        (self.root / "APPROACHES/index.md").write_text("A001")
        (self.root / "APPROACHES/A001.md").write_text("Bridge")
        self.config = audits.load_config()
        self.config["prompt"] = "Audit independently.\n[RESEARCH_CONTEXT]\n[AUDIT_FOCUS]"
        self.model = {"value": "gpt-6-astra", "provider": "codex", "model": "gpt-6-astra"}
        self.identity = audit_identity(self.model, self.config["prompt"], self.config)

    def render(self, snapshot, previous=None, slot=1, identity=None):
        return audit_prompt(self.config["prompt"], snapshot, previous, identity or self.identity, slot, self.config)

    def test_snapshot_excludes_logs_reports_and_symlinks(self):
        (self.root / "transcript.jsonl").write_text("Huge runtime log")
        (self.root / "APPROACHES/link.md").symlink_to(self.root / "PROVED.md")
        (self.root / "AUDITS").mkdir()
        (self.root / "AUDITS/report.md").write_text("Previous verdict")
        result = research_snapshot(self.root)
        self.assertEqual(set(result), {"INITIAL_PROMPT.md", "PROVED.md", "APPROACHES/index.md", "APPROACHES/A001.md"})
        self.assertEqual(result["PROVED.md"]["bytes"], 5)
        self.assertEqual(len(result["PROVED.md"]["sha256"]), 64)
        self.assertNotIn("Lemma", json.dumps(result))

    def test_delta_removed_paths_full_cadence_and_changed_task(self):
        first = research_snapshot(self.root)
        baseline = {"identity": self.identity, "snapshot": first, "reviews": 1}
        self.assertIn("Full independent review", self.render(first))
        self.assertIn("No recorded mathematical files changed", self.render(first, baseline))
        (self.root / "PROVED.md").write_text("Corrected lemma")
        (self.root / "APPROACHES/A001.md").unlink()
        next_snapshot = research_snapshot(self.root)
        delta = self.render(next_snapshot, baseline)
        self.assertIn("Change-focused independent review", delta)
        self.assertIn('"PROVED.md": 15 bytes', delta)
        self.assertIn('"APPROACHES/A001.md": removed', delta)
        self.assertIn("transitive dependencies", delta)
        self.assertIn("sha256 prefix " + next_snapshot["PROVED.md"]["sha256"][:12], delta)
        self.assertNotIn(next_snapshot["PROVED.md"]["sha256"], delta)
        self.assertIn("Full independent review", self.render(next_snapshot, {**baseline, "reviews": 3}))
        self.assertIn("Full independent review", self.render(next_snapshot, baseline, identity="new-model-or-prompt"))
        (self.root / "INITIAL_PROMPT.md").write_text("Changed task")
        self.assertIn("Full independent review", self.render(research_snapshot(self.root), baseline))

    def test_bounded_manifest_retains_full_access_and_distinct_focus(self):
        self.config["context"] = {"maxPaths": 1, "fullEvery": 4}
        snapshot = research_snapshot(self.root)
        prompt = self.render(snapshot)
        self.assertIn("3 further paths omitted", prompt)
        self.assertIn("Full independent review", prompt)
        self.assertIn("inspect the index", prompt)
        self.assertNotEqual(prompt, self.render(snapshot, slot=2))
        custom = "My exact prompt.  \n"
        self.assertEqual(audit_prompt(custom, snapshot, {}, "id", 1, self.config), custom)

    def test_invalid_persisted_baseline_falls_back_to_full_review(self):
        snapshot = research_snapshot(self.root)
        invalid_snapshots = (None, [], "old-format", {3: {}}, {"PROVED.md": None},
                             {"PROVED.md": {"sha256": "digest", "bytes": -1}})
        for invalid in invalid_snapshots:
            with self.subTest(invalid=invalid):
                prompt = self.render(snapshot, {"identity": self.identity, "snapshot": invalid, "reviews": 1})
                self.assertIn("Full independent review", prompt)
                self.assertIn("changed/new: 4; removed: 0", prompt)

    def test_new_model_does_not_inherit_old_models_unchanged_manifest(self):
        snapshot = research_snapshot(self.root)
        prompt = self.render(snapshot, {"identity": "old-model", "snapshot": snapshot, "reviews": 2})
        self.assertIn("Full independent review", prompt)
        self.assertIn("changed/new: 4; removed: 0", prompt)

    def test_success_baseline_survives_restart_but_failed_slot_does_not_advance(self):
        calls = []
        options = {"intervalHours": 1, "models": ["gpt-6-astra", "gpt-5.6-sol", "none"]}

        def provider(model, prompt, workspace, cancelled):
            calls.append((model["model"], prompt))
            if model["model"] == "gpt-5.6-sol":
                raise RuntimeError("Provider unavailable")
            return {"text": "Independent report", "model": model["model"]}

        def launch(progress=None):
            engine = audits.ResearchAudits(self.root, provider=provider, config=self.config,
                                           preflight=lambda model: True, progress=progress)
            self.addCleanup(engine.close)
            engine.update(options, start_now=True)
            engine.thread.join(3)
            self.assertFalse(engine.thread.is_alive())
            return engine

        first = launch()
        saved = json.loads(json.dumps(first.checkpoint()))
        self.assertEqual(set(saved["contextBaselines"]), {"1"})
        first.close()
        (self.root / "PROVED.md").write_text("New result")
        second = launch(saved)
        prompts = dict(calls[-2:])
        self.assertIn("Change-focused independent review", prompts["gpt-6-astra"])
        self.assertIn("Full independent review", prompts["gpt-5.6-sol"])
        self.assertEqual(second.checkpoint()["contextBaselines"]["1"]["reviews"], 2)

    def test_failure_after_success_keeps_last_successful_revision_and_cadence(self):
        snapshot = research_snapshot(self.root)
        model = next(row for row in self.config["models"] if row["value"] == "gpt-6-astra")
        identity = audit_identity(model, self.config["prompt"], self.config)
        baseline = {"identity": identity, "snapshot": snapshot, "reviews": 3}
        (self.root / "PROVED.md").write_text("Changed after third successful audit")
        prompts = []

        def failed_provider(model, prompt, workspace, cancelled):
            prompts.append(prompt)
            raise RuntimeError("No quota")

        engine = audits.ResearchAudits(self.root, provider=failed_provider, config=self.config,
                                       preflight=lambda model: True,
                                       progress={"contextBaselines": {"1": baseline}})
        self.addCleanup(engine.close)
        engine.update({"intervalHours": 1, "models": ["gpt-6-astra", "none", "none"]}, start_now=True)
        engine.thread.join(3)
        self.assertFalse(engine.thread.is_alive())
        self.assertIn("Full independent review", prompts[0])
        self.assertEqual(engine.checkpoint()["contextBaselines"]["1"], baseline)
        self.assertIn("Full independent review", self.render(research_snapshot(self.root), baseline, identity=identity))


class AuditUsageTests(unittest.TestCase):
    def invoke(self, provider, messages, exit_code=0):
        recorded = []

        def process(command, **kwargs):
            for message in messages:
                kwargs["stdout"].write(json.dumps(message) + "\n")
            kwargs["stdout"].flush()
            if provider == "codex":
                Path(command[command.index("-o") + 1]).write_text("Independent report")
            return Mock(stdin=io.StringIO(), returncode=exit_code, poll=lambda: exit_code)

        with tempfile.TemporaryDirectory() as directory, patch.object(audits, "resolve_executable", return_value=provider), patch.object(audits.subprocess, "Popen", side_effect=process):
            try:
                audits.run_auditor({"provider": provider, "model": "selected"}, "Audit", Path(directory),
                                   threading.Event(), on_usage=recorded.append)
            except RuntimeError:
                self.assertNotEqual(exit_code, 0)
        self.assertEqual(len(recorded), 1)
        return recorded[0]

    def test_codex_usage_is_retained_without_derived_cost_or_private_content(self):
        usage = {"input_tokens": 120, "cached_input_tokens": 100, "output_tokens": 20}
        result = self.invoke("codex", [{"type": "turn.completed", "usage": usage},
                                       {"type": "reasoning", "content": "Private"}])
        self.assertEqual(result, {"coverage": "reported", "outcome": "completed",
                                 "usageRecords": [{"basis": "turn.completed", "usage": usage}]})

    def test_failed_claude_request_keeps_reported_usage(self):
        result = self.invoke("claude", [{"type": "result", "result": "Failure", "is_error": True,
                                         "usage": {"input_tokens": 50}, "total_cost_usd": 0.2}], exit_code=1)
        self.assertEqual(result["coverage"], "reported")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["usageRecords"][0]["totalCostUsd"], 0.2)

    def test_missing_usage_is_not_zero(self):
        result = self.invoke("kimi", [{"role": "assistant", "content": "Report"}])
        self.assertEqual(result, {"coverage": "missing", "outcome": "completed", "usageRecords": []})

    def test_cost_only_and_zero_counters_are_measurements_but_unknown_fields_are_not(self):
        for message, expected in (
            ({"total_cost_usd": 0.2}, "reported"),
            ({"usage": {"input_tokens": 0}}, "reported"),
            ({"modelUsage": {"actual-model": {}}}, "missing"),
            ({"usage": {"input_tokens": True}}, "missing"),
            ({"usage": {"input_tokens": -1}}, "missing"),
            ({"usage": {"new_unrecognized_field": 17}}, "missing"),
            ({"total_cost_usd": float("nan")}, "missing"),
        ):
            with self.subTest(message=message):
                report = self.invoke("claude", [{"type": "result", "result": "Report", **message}])
                self.assertEqual(report["coverage"], expected)

    def test_usage_callback_failure_preserves_report_and_original_provider_error(self):
        def callback(accounting):
            raise ValueError("Telemetry sink is closed")

        for exit_code in (0, 1):
            def process(command, **kwargs):
                kwargs["stdout"].write(json.dumps({"type": "result", "result": "Report" if not exit_code else "Provider rejected request"}) + "\n")
                kwargs["stdout"].flush()
                return Mock(stdin=io.StringIO(), returncode=exit_code, poll=lambda: exit_code)

            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory, \
                    patch.object(audits, "resolve_executable", return_value="claude"), \
                    patch.object(audits.subprocess, "Popen", side_effect=process):
                if exit_code:
                    with self.assertRaisesRegex(RuntimeError, "Provider rejected request"):
                        audits.run_auditor({"provider": "claude", "model": "selected"}, "Audit", Path(directory),
                                           threading.Event(), on_usage=callback)
                else:
                    result = audits.run_auditor({"provider": "claude", "model": "selected"}, "Audit", Path(directory),
                                               threading.Event(), on_usage=callback)
                    self.assertEqual(result["text"], "Report")

    def test_cancelled_audit_retains_final_usage_without_returning_report(self):
        observed = []
        cancelled = Mock(is_set=Mock(side_effect=[False, True]))

        def process(command, **kwargs):
            kwargs["stdout"].write(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 42}}) + "\n")
            kwargs["stdout"].flush()
            return Mock(stdin=io.StringIO(), returncode=0, poll=lambda: 0)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(audits, "resolve_executable", return_value="codex"), \
                patch.object(audits.subprocess, "Popen", side_effect=process):
            result = audits.run_auditor({"provider": "codex", "model": "selected"}, "Audit", Path(directory),
                                       cancelled, on_usage=observed.append)
        self.assertIsNone(result)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["outcome"], "cancelled")
        self.assertEqual(observed[0]["coverage"], "reported")
        self.assertEqual(observed[0]["usageRecords"][0]["usage"]["input_tokens"], 42)

    def test_scheduler_links_prompt_usage_and_completion_to_one_request(self):
        observed = []

        def process(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text("Independent report")
            kwargs["stdout"].write(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 42}}) + "\n")
            kwargs["stdout"].flush()
            return Mock(stdin=io.StringIO(), returncode=0, poll=lambda: 0)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(audits, "resolve_executable", return_value="codex"), \
                patch.object(audits.subprocess, "Popen", side_effect=process):
            engine = audits.ResearchAudits(Path(directory), on_event=observed.append,
                                           provider=audits.run_auditor, config=audits.load_config(),
                                           preflight=lambda model: True)
            self.addCleanup(engine.close)
            engine.update({"intervalHours": 1, "models": ["gpt-6-astra", "none", "none"]}, start_now=True)
            engine.thread.join(3)
            self.assertFalse(engine.thread.is_alive())
            events = [event for event in observed if event["kind"] in {"request", "audit_usage"}
                      or event.get("status") == "completed"]
            self.assertEqual(len(events), 3)
            self.assertEqual(len({event["requestId"] for event in events}), 1)


if __name__ == "__main__":
    unittest.main()
