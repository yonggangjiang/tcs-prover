"""Independent audit scheduling and provider isolation; no paid model calls."""

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ui import audits


class Clock:
    now = 0

    def __call__(self):
        return self.now


def settings(*models, seconds=1):
    return {"intervalHours": seconds / 3600, "models": list(models) + ["none"] * (3 - len(models))}


class AuditCatalogTests(unittest.TestCase):
    def test_yaml_catalog_exposes_matching_model_and_effort_choices(self):
        expected = {
            "gpt-6-astra": ("GPT Astra (Ultra)", "codex", "gpt-6-astra", "ultra"),
            "gpt-5.6-sol": ("GPT Sol (Ultra)", "codex", "gpt-5.6-sol", "ultra"),
            "gpt-5.6-terra": ("GPT Terra (Ultra)", "codex", "gpt-5.6-terra", "ultra"),
            "gpt-5.6-luna": ("GPT Luna (Max)", "codex", "gpt-5.6-luna", "max"),
            "claude-fable": ("Claude Fable 5.1 (Max)", "claude", "claude-fable-5-1", "max"),
            "claude-opus": ("Claude Opus (Max)", "claude", "opus", "max"),
            "claude-sonnet": ("Claude Sonnet (Max)", "claude", "sonnet", "max"),
            "claude-haiku": ("Claude Haiku", "claude", "haiku", None),
            "kimi-k3": ("Kimi K3 (Max)", "kimi", "kimi-code/k3", "max"),
            "kimi-code": ("Kimi Code (Max)", "kimi", "kimi-code/kimi-for-coding", "max"),
        }
        rows = {row["value"]: row for row in audits.load_config()["models"]}
        self.assertEqual(rows.pop("none"), {"value": "none", "label": "None"})
        self.assertEqual(set(rows), set(expected))
        for value, fields in expected.items():
            with self.subTest(value=value):
                self.assertEqual(tuple(rows[value].get(key) for key in ("label", "provider", "model", "effort")), fields)
                self.assertIn({"value": value, "label": fields[0]}, audits.model_choices())
                self.assertEqual(audits.normalize_settings(settings(value))["models"], [value, "none", "none"])
        self.assertEqual(audits.model_choices()[0], {"value": "none", "label": "None"})
        self.assertEqual(audits.normalize_settings(), {"intervalHours": 2, "models": ["none"] * 3})


class AuditSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.run = Path(self.folder.name)
        self.clock = Clock()
        self.events, self.calls = [], []
        (self.run / "APPROACHES").mkdir()
        (self.run / "APPROACHES/index.md").write_text("A001 active")
        (self.run / "APPROACHES/A001.md").write_text("Original mathematical record")
        (self.run / "checked-statement.md").write_text("Exact task")
        (self.run / "PROVED.md").write_text("Lemma")

    def provider(self, model, prompt, workspace, cancelled):
        self.calls.append((model, prompt, workspace, cancelled))
        return {"text": "## Coverage\nAll saved records.\n\n## Suggestions\nA concrete next step.", "model": model["model"]}

    def scheduler(self, provider=None, **kwargs):
        kwargs.setdefault("preflight", lambda model: True)
        instance = audits.ResearchAudits(self.run, on_event=self.events.append, clock=self.clock,
                                        provider=provider or self.provider, **kwargs)
        self.addCleanup(instance.close)
        return instance

    def finish(self, scheduler):
        self.assertIsNotNone(scheduler.thread)
        scheduler.thread.join(timeout=3)
        self.assertFalse(scheduler.thread.is_alive())

    def reports(self):
        return sorted((self.run / "AUDITS").glob("*.md"))

    def launch(self, scheduler, options=None):
        options = options or settings("gpt-6-astra")
        scheduler.update(options)
        self.clock.now += 1
        scheduler.update(options)

    def test_none_defaults_never_launch_or_create_audit_file(self):
        before, after = Mock(), Mock()
        scheduler = self.scheduler(before_batch=before, after_batch=after)
        scheduler.update(audits.default_settings())
        self.clock.now = 100000
        scheduler.update(audits.default_settings())
        self.assertIsNone(scheduler.thread)
        self.assertFalse((self.run / "AUDITS").exists())
        self.assertEqual(scheduler.checkpoint()["elapsedSeconds"], 0)
        self.assertFalse(scheduler.active)
        before.assert_not_called()
        after.assert_not_called()
        self.assertEqual(audits.default_settings(), {"intervalHours": 2, "models": ["none"] * 3})

    def test_manual_audit_resets_interval_without_resetting_total_author_time(self):
        scheduler = self.scheduler(progress={"elapsedSeconds": 80, "lastStartedSeconds": 30})
        options = settings("gpt-6-astra", seconds=100)
        scheduler.update(options)
        self.clock.now = 10
        self.assertTrue(scheduler.update(options, start_now=True))
        self.finish(scheduler)
        self.assertEqual(scheduler.checkpoint()["elapsedSeconds"], 90)
        self.assertEqual(scheduler.checkpoint()["lastStartedSeconds"], 90)
        self.assertEqual(scheduler.settings, options)
        self.assertEqual(len(self.calls), 1)
        self.clock.now = 109
        self.assertFalse(scheduler.update(options))
        self.assertEqual(len(self.calls), 1)
        self.clock.now = 110
        self.assertTrue(scheduler.update(options))
        self.finish(scheduler)
        self.assertEqual(len(self.calls), 2)

    def test_manual_audit_rejects_disabled_paused_and_closed_without_resetting_timer(self):
        before = Mock()
        scheduler = self.scheduler(before_batch=before,
                                   progress={"elapsedSeconds": 50, "lastStartedSeconds": 20})
        options = settings("gpt-6-astra", seconds=100)
        for selected, active in ((settings(), True), (options, False)):
            with self.subTest(selected=selected, active=active), self.assertRaises(ValueError):
                scheduler.update(selected, active=active, start_now=True)
            self.assertEqual(scheduler.checkpoint()["lastStartedSeconds"], 20)
        scheduler.close()
        with self.assertRaises(ValueError):
            scheduler.update(options, start_now=True)
        self.assertIsNone(scheduler.thread)
        self.assertEqual(scheduler.checkpoint()["lastStartedSeconds"], 20)
        before.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_manual_and_scheduled_audits_cannot_overlap_preflight_or_resume(self):
        preflight_entered, allow_preflight = threading.Event(), threading.Event()
        finishing, allow_finish = threading.Event(), threading.Event()
        self.addCleanup(allow_preflight.set)
        self.addCleanup(allow_finish.set)

        def preflight(model):
            preflight_entered.set()
            return allow_preflight.wait(3)

        def after(engine):
            finishing.set()
            allow_finish.wait(3)

        scheduler = self.scheduler(preflight=preflight, after_batch=after)
        options = settings("gpt-6-astra", seconds=100)
        scheduler.update(options)
        self.clock.now = 2
        scheduler.update(options, start_now=True)
        self.assertTrue(preflight_entered.wait(2))
        original_thread = scheduler.thread
        self.clock.now = 200
        for phase in ("preflight", "resume"):
            with self.subTest(phase=phase):
                self.assertTrue(scheduler.status()["batchActive"])
                with self.assertRaisesRegex(ValueError, "already in progress"):
                    scheduler.update(options, start_now=True)
                self.assertFalse(scheduler.update(options))
                self.assertIs(scheduler.thread, original_thread)
                self.assertEqual(scheduler.checkpoint()["lastStartedSeconds"], 2)
            if phase == "preflight":
                allow_preflight.set()
                self.assertTrue(finishing.wait(2))
                self.assertEqual(scheduler.status()["runningSlots"], [])
        allow_finish.set()
        self.finish(scheduler)
        self.assertFalse(scheduler.status()["batchActive"])
        self.assertEqual(len(self.calls), 1)

    def test_failed_manual_thread_launch_does_not_reset_interval(self):
        scheduler = self.scheduler(progress={"elapsedSeconds": 80, "lastStartedSeconds": 30})
        options = settings("gpt-6-astra", seconds=100)
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("Thread unavailable")):
            with self.assertRaisesRegex(ValueError, "Thread unavailable"):
                scheduler.update(options, start_now=True)
        self.assertEqual(scheduler.checkpoint()["lastStartedSeconds"], 30)
        self.assertEqual(scheduler.status()["runningSlots"], [])
        self.assertFalse(scheduler.status()["batchActive"])
        self.assertEqual(self.calls, [])

    def test_all_unavailable_auditors_warn_without_pausing_or_calling_models(self):
        before, after = Mock(), Mock()
        scheduler = self.scheduler(preflight=lambda model: False, before_batch=before, after_batch=after)
        self.launch(scheduler, settings("claude-opus", "gpt-6-astra", "kimi-code"))
        self.finish(scheduler)
        before.assert_not_called()
        after.assert_called_once_with(scheduler)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reports(), [])
        self.assertEqual([event["status"] for event in self.events], ["checking", "warning"] * 3)
        self.assertTrue(scheduler.active)

    def test_selected_yaml_model_and_custom_effort_reach_provider(self):
        config = audits.load_config()
        model = next(row for row in config["models"] if row["value"] == "claude-fable")
        model.update(model="claude-custom-model", effort="high")
        scheduler = self.scheduler(config=config)
        self.launch(scheduler, settings("claude-fable"))
        self.finish(scheduler)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], model)
        self.assertEqual(self.calls[0][0]["model"], "claude-custom-model")
        self.assertEqual(self.calls[0][0]["effort"], "high")

    def test_yaml_prompt_reloads_each_batch_and_logged_text_matches_each_call(self):
        config = audits.load_config()
        path = self.run / "research_audit.yaml"
        path.write_text(audits.yaml.safe_dump(config))
        revised = "Audit my revised statement.\n\nPreserve \\lambda, 中文, and trailing whitespace.  \n"
        following = "Use this prompt for the following batch."

        def before(engine):
            # An edit after the batch starts must not split the auditors' instructions.
            config["prompt"] = following
            path.write_text(audits.yaml.safe_dump(config))
            return True

        def provider(model, prompt, workspace, cancelled):
            self.assertTrue(any(event.get("kind") == "request" and event["model"] == model["model"]
                                and event["text"] == prompt for event in self.events))
            return self.provider(model, prompt, workspace, cancelled)

        with patch.object(audits, "CONFIG_PATH", path):
            scheduler = self.scheduler(provider, before_batch=before)
            config["prompt"] = revised
            path.write_text(audits.yaml.safe_dump(config))
            options = settings("gpt-6-astra", "claude-opus", "kimi-k3")
            self.launch(scheduler, options)
            self.finish(scheduler)
            self.assertEqual([call[1] for call in self.calls], [revised] * 3)
            requests = [event for event in self.events if event["kind"] == "request"]
            self.assertEqual(len(requests), 4)
            self.assertEqual([event["text"] for event in requests[:3]], [revised] * 3)
            self.assertEqual(requests[3]["text"], audits.KIMI_START_PROMPT)
            self.assertIn("Agent instructions", requests[2]["label"])
            self.assertIn("User message", requests[3]["label"])
            self.assertEqual([event["model"] for event in requests],
                             ["gpt-6-astra", "opus", "kimi-code/k3", "kimi-code/k3"])
            self.assertEqual(requests[0]["reasoningEffort"], "ultra")
            self.assertEqual(requests[2]["reasoningEffort"], "max")
            self.assertEqual(len({event["auditStartedAt"] for event in requests}), 1)
            self.clock.now += 1
            scheduler.update(options)
            self.finish(scheduler)
            self.assertEqual([call[1] for call in self.calls[3:]], [following] * 3)
            self.assertEqual(requests[0]["text"], revised)  # Earlier records remain exact.

    def test_invalid_live_prompt_warns_without_pausing_and_retries_after_correction(self):
        config = audits.load_config()
        path = self.run / "research_audit.yaml"
        path.write_text(audits.yaml.safe_dump(config))
        before, after = Mock(return_value=True), Mock()
        with patch.object(audits, "CONFIG_PATH", path):
            scheduler = self.scheduler(before_batch=before, after_batch=after)
            config["prompt"] = "   "
            path.write_text(audits.yaml.safe_dump(config))
            self.launch(scheduler)
            self.finish(scheduler)
            before.assert_not_called()
            after.assert_called_once_with(scheduler)
            self.assertEqual(self.calls, [])
            self.assertIn("nonempty prompt", scheduler.status()["batchError"])
            self.assertTrue(scheduler.active)
            config["prompt"] = "Corrected audit instructions."
            path.write_text(audits.yaml.safe_dump(config))
            self.clock.now += 1
            scheduler.update(settings("gpt-6-astra"))
            self.finish(scheduler)
            self.assertEqual(self.calls[0][1], config["prompt"])
            self.assertEqual(scheduler.status()["batchError"], "")

    def test_unavailable_selected_model_is_retried_each_interval_without_clearing_warning(self):
        previous_warning = []
        attempts = []

        def preflight(model):
            attempts.append(model["value"])
            if previous_warning:
                self.assertEqual(scheduler.status()["warnings"], previous_warning)
            return False

        before, after = Mock(), Mock()
        options = settings("claude-opus")
        scheduler = self.scheduler(preflight=preflight, before_batch=before, after_batch=after)
        self.launch(scheduler, options)
        self.finish(scheduler)
        previous_warning[:] = scheduler.status()["warnings"]
        self.assertIn("Claude Opus (Max) (claude-opus)", previous_warning[0]["message"])
        self.assertIn("Warning:", previous_warning[0]["message"])
        scheduler.update(options)
        self.assertEqual(attempts, ["claude-opus"])
        self.clock.now += 1
        scheduler.update(options)
        self.finish(scheduler)
        self.assertEqual(attempts, ["claude-opus", "claude-opus"])
        self.assertEqual(scheduler.settings, options)
        self.assertEqual(scheduler.status()["warnings"], previous_warning)
        before.assert_not_called()
        self.assertEqual(after.call_count, 2)

    def test_warning_clears_only_for_successful_model_while_other_failure_persists(self):
        failed = {"gpt-6-astra", "claude-opus"}

        def provider(model, prompt, workspace, cancelled):
            if model["value"] in failed:
                raise RuntimeError("Usage limit reached")
            return self.provider(model, prompt, workspace, cancelled)

        scheduler = self.scheduler(provider)
        options = settings("gpt-6-astra", "claude-opus")
        self.launch(scheduler, options)
        self.finish(scheduler)
        self.assertEqual(len(scheduler.status()["warnings"]), 2)
        failed.remove("gpt-6-astra")
        self.clock.now += 1
        scheduler.update(options)
        self.finish(scheduler)
        remaining = scheduler.status()["warnings"]
        self.assertEqual([row["model"] for row in remaining], ["claude-opus"])
        self.assertNotIn("GPT Astra", scheduler.status()["lastError"])
        self.assertIn("Claude Opus", scheduler.status()["lastError"])
        failed.clear()
        self.clock.now += 1
        scheduler.update(options)
        self.finish(scheduler)
        self.assertEqual(scheduler.status()["warnings"], [])
        self.assertEqual(scheduler.status()["lastError"], "")

    def test_model_response_clears_only_its_warning_before_report_is_finished(self):
        scheduler = self.scheduler()
        options = settings("gpt-6-astra", "claude-opus")
        scheduler.update(options)
        scheduler._warning(1, "gpt-6-astra", "Usage exhausted")
        scheduler._warning(2, "claude-opus", "Not logged in")
        cancelled = threading.Event()
        scheduler._activity(2, "claude-opus", cancelled, "starting", "CLI started")
        self.assertEqual(len(scheduler.checkpoint()["warnings"]), 2)
        scheduler._activity(2, "claude-opus", cancelled, "working", "Model responded")
        self.assertTrue(self.events[-1]["recovered"])
        self.assertEqual([row["slot"] for row in scheduler.checkpoint()["warnings"]], [1])
        self.assertEqual(self.reports(), [])
        scheduler._activity(2, "claude-opus", cancelled, "retrying", "Request retry 1: 429.")
        self.assertEqual(len(scheduler.checkpoint()["warnings"]), 2)
        scheduler._activity(2, "claude-opus", cancelled, "working", "Read: PROVED.md")
        self.assertTrue(self.events[-1]["recovered"])
        self.assertEqual([row["slot"] for row in scheduler.status()["warnings"]], [1])
        cancelled.set()
        scheduler._activity(1, "gpt-6-astra", cancelled, "working", "Late response")
        self.assertEqual([row["slot"] for row in scheduler.status()["warnings"]], [1])
        scheduler.update(settings("claude-opus", "claude-opus"))
        scheduler._warning(1, "claude-opus", "Unavailable")
        scheduler._activity(1, "gpt-6-astra", threading.Event(), "working", "Old model response")
        self.assertEqual(scheduler.status()["warnings"][0]["model"], "claude-opus")

    def test_default_provider_delivers_activity_to_scheduler_and_matches_completion_model(self):
        scheduler = self.scheduler(audits.run_auditor)
        options = settings("gpt-6-astra")
        scheduler.update(options)
        scheduler._warning(1, "gpt-6-astra", "Previous usage limit")

        def process(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text("An audit report")
            kwargs["stdout"].write(json.dumps({"type": "item.started", "item": {"type": "web_search"}}) + "\n")
            kwargs["stdout"].flush()
            return Mock(stdin=io.StringIO(), returncode=0, poll=lambda: 0)

        with patch.object(audits, "resolve_executable", return_value="codex"), patch.object(audits.subprocess, "Popen", side_effect=process):
            self.launch(scheduler, options)
            self.finish(scheduler)
        working = [event for event in self.events if event["status"] == "working"]
        self.assertEqual(len(working), 1)
        self.assertTrue(working[0]["recovered"])
        self.assertEqual(working[0]["text"], "Searching the web.")
        completed = [event for event in self.events if event["status"] == "completed"][0]
        self.assertEqual(completed["model"], "gpt-6-astra")
        self.assertEqual(completed["actualModel"], "gpt-6-astra")
        self.assertTrue((self.run / completed["report"]).is_file())

    def test_successful_preflight_keeps_warning_until_report_and_runtime_failure_replaces_it(self):
        available = False
        entered, release = threading.Event(), threading.Event()

        def provider(*args):
            entered.set()
            release.wait(2)
            raise RuntimeError("Usage exhausted")

        scheduler = self.scheduler(provider, preflight=lambda model: available)
        options = settings("gpt-6-astra")
        self.launch(scheduler, options)
        self.finish(scheduler)
        warning = scheduler.status()["warnings"]
        available = True
        self.clock.now += 1
        scheduler.update(options)
        self.assertTrue(entered.wait(2))
        self.assertEqual(scheduler.status()["warnings"], warning)
        release.set()
        self.finish(scheduler)
        self.assertEqual(len(scheduler.status()["warnings"]), 1)
        self.assertIn("Usage exhausted", scheduler.status()["warnings"][0]["message"])

    def test_warning_checkpoint_survives_restore_and_selection_changes_remove_it(self):
        scheduler = self.scheduler(preflight=lambda model: False)
        options = settings("claude-opus", "gpt-6-astra")
        self.launch(scheduler, options)
        self.finish(scheduler)
        checkpoint = json.loads(json.dumps(scheduler.checkpoint()))
        scheduler.close()
        restored = self.scheduler(progress=checkpoint)
        restored.update(options, active=False)
        self.assertEqual(restored.status()["warnings"], checkpoint["warnings"])
        restored.update(settings("none", "gpt-6-astra"), active=False)
        self.assertEqual([row["model"] for row in restored.status()["warnings"]], ["gpt-6-astra"])
        restored.update(settings("none", "gpt-5.6-sol"), active=False)
        self.assertEqual(restored.status()["warnings"], [])
        self.assertEqual(restored.status()["lastError"], "")
        disabled = self.scheduler(progress=checkpoint)
        disabled.update(settings(), active=False)
        self.assertEqual(disabled.checkpoint()["warnings"], [])

    def test_late_failure_for_replaced_or_disabled_model_does_not_restore_warning(self):
        for replacement in ("none", "gpt-5.6-sol"):
            with self.subTest(replacement=replacement):
                entered, release = threading.Event(), threading.Event()

                def provider(*args):
                    entered.set()
                    release.wait(2)
                    raise RuntimeError("Late old model failure")

                scheduler = self.scheduler(provider)
                self.launch(scheduler, settings("gpt-6-astra"))
                self.assertTrue(entered.wait(2))
                scheduler.update(settings(replacement))
                release.set()
                self.finish(scheduler)
                self.assertEqual(scheduler.status()["warnings"], [])
                self.assertEqual(scheduler.status()["lastError"], "")

    def test_old_model_success_does_not_clear_replacement_models_warning(self):
        entered, release = threading.Event(), threading.Event()

        def provider(*args):
            entered.set()
            release.wait(2)
            return self.provider(*args)

        scheduler = self.scheduler(provider)
        self.launch(scheduler, settings("gpt-6-astra"))
        self.assertTrue(entered.wait(2))
        scheduler.update(settings("gpt-5.6-sol"))
        scheduler._warning(1, "gpt-5.6-sol", RuntimeError("Replacement model unavailable"))
        release.set()
        self.finish(scheduler)
        self.assertEqual([row["model"] for row in scheduler.status()["warnings"]], ["gpt-5.6-sol"])

    def test_saved_warning_filter_rejects_invalid_rows(self):
        valid = {"slot": 1, "model": "claude-opus", "message": "Warning: Claude Opus unavailable"}
        rows = [valid, None, {**valid, "slot": True}, {**valid, "slot": 4}, {**valid, "message": ""}]
        self.assertEqual(audits.selected_warnings(rows, settings("claude-opus")), [valid])
        self.assertEqual(audits.selected_warnings(rows, settings("gpt-6-astra")), [])

    def test_missing_auditor_is_skipped_while_available_auditor_completes(self):
        def preflight(model):
            if model["provider"] == "claude":
                raise RuntimeError("Claude CLI is not installed")
            return True

        before, after = Mock(return_value=True), Mock()
        scheduler = self.scheduler(preflight=preflight, before_batch=before, after_batch=after)
        self.launch(scheduler, settings("claude-opus", "gpt-6-astra"))
        self.finish(scheduler)
        before.assert_called_once_with(scheduler)
        after.assert_called_once_with(scheduler)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0]["model"], "gpt-6-astra")
        self.assertEqual(len(self.reports()), 1)
        self.assertIn("not installed", scheduler.status()["lastError"])

    def test_all_runtime_failures_warn_and_call_resume_hook(self):
        def provider(model, prompt, workspace, cancelled):
            raise RuntimeError("Usage limit reached")

        options = settings("gpt-6-astra", "claude-opus")

        def before(engine):
            engine.update(options, active=False, paused_for_audit=True)
            return True

        after = Mock(side_effect=lambda engine: engine.update(options, active=True))
        scheduler = self.scheduler(provider, before_batch=before, after_batch=after)
        self.launch(scheduler, options)
        self.finish(scheduler)
        after.assert_called_once_with(scheduler)
        self.assertTrue(scheduler.active)
        self.assertEqual(self.reports(), [])
        self.assertEqual(len([event for event in self.events if event["status"] == "warning"]), 2)
        self.assertFalse(any(event["status"] == "error" for event in self.events))

    def test_empty_or_malformed_provider_result_warns_without_a_report(self):
        for result in (None, "", {}, {"model": "gpt-6-astra", "text": " "}, {"model": [], "text": "Advice"}):
            with self.subTest(result=result):
                after = Mock()
                scheduler = self.scheduler(lambda *args: result, after_batch=after)
                self.launch(scheduler)
                self.finish(scheduler)
                after.assert_called_once_with(scheduler)
                self.assertIn("empty or malformed", scheduler.status()["lastError"])
                self.assertEqual(self.reports(), [])
                self.assertEqual(self.events[-1]["status"], "warning")

    def test_publication_failure_leaves_no_partial_report_and_resumes(self):
        after = Mock()
        scheduler = self.scheduler(after_batch=after)
        with patch.object(audits.os, "link", side_effect=OSError("Cannot publish report")):
            self.launch(scheduler)
            self.finish(scheduler)
        after.assert_called_once_with(scheduler)
        self.assertEqual(self.reports(), [])
        self.assertEqual(list((self.run / "AUDITS").iterdir()), [])
        self.assertIn("Cannot publish report", scheduler.status()["lastError"])

    def test_write_failure_leaves_no_partial_report(self):
        original_fdopen = os.fdopen

        class BrokenStream:
            def __init__(self, *args, **kwargs):
                self.stream = original_fdopen(*args, **kwargs)

            def __enter__(self):
                return self

            def write(self, text):
                self.stream.write(text[:10])
                raise OSError("Disk full")

            def __exit__(self, *args):
                self.stream.close()

        scheduler = self.scheduler()
        with patch.object(audits.os, "fdopen", BrokenStream):
            self.launch(scheduler)
            self.finish(scheduler)
        self.assertEqual(self.reports(), [])
        self.assertEqual(list((self.run / "AUDITS").iterdir()), [])
        self.assertIn("Disk full", scheduler.status()["lastError"])

    def test_elapsed_clock_excludes_pause_and_survives_restore(self):
        scheduler = self.scheduler()
        options = settings("gpt-6-astra", seconds=10)
        scheduler.update(options)
        self.clock.now = 6
        scheduler.update(options, active=False)
        checkpoint = scheduler.checkpoint()
        self.assertEqual(checkpoint["elapsedSeconds"], 6)
        scheduler.close()
        self.clock.now = 100
        resumed = self.scheduler(progress=checkpoint)
        resumed.update(options)
        self.clock.now = 103
        resumed.update(options)
        self.assertIsNone(resumed.thread)
        self.clock.now = 104
        resumed.update(options)
        self.finish(resumed)
        self.assertEqual(len(self.calls), 1)

    def test_enabling_starts_interval_clock_and_interval_change_applies_live(self):
        scheduler = self.scheduler()
        scheduler.update(settings())
        self.clock.now = 100
        scheduler.update(settings("gpt-6-astra", seconds=10))
        self.assertIsNone(scheduler.thread)
        self.clock.now = 105
        scheduler.update(settings("gpt-6-astra", seconds=5))
        self.finish(scheduler)
        self.assertEqual(len(self.calls), 1)

    def test_three_fresh_sessions_read_author_workspace_after_report_rotation(self):
        (self.run / "audit.md").write_text("Previous legacy audit")
        (self.run / "AUDITS").mkdir()
        (self.run / "AUDITS/old.md").write_text("Earlier independent report")
        (self.run / "transcript.jsonl").write_text("Large runtime log")
        seen = []
        barrier = threading.Barrier(3)

        def provider(model, prompt, workspace, cancelled):
            seen.append(workspace)
            self.assertEqual(workspace, self.run.resolve())
            self.assertEqual(list((workspace / "AUDITS").iterdir()), [])
            self.assertEqual((workspace / "audit_history/old.md").read_text(), "Earlier independent report")
            self.assertTrue((workspace / "transcript.jsonl").samefile(self.run / "transcript.jsonl"))
            self.assertEqual((workspace / "checked-statement.md").read_text(), "Exact task")
            self.assertEqual((workspace / "APPROACHES/A001.md").read_text(), "Original mathematical record")
            barrier.wait(timeout=2)
            return self.provider(model, prompt, workspace, cancelled)

        scheduler = self.scheduler(provider)
        self.launch(scheduler, settings("gpt-6-astra", "claude-opus", "gpt-5.6-sol"))
        self.finish(scheduler)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(seen[0].exists())
        self.assertEqual((self.run / "audit.md").read_text(), "Previous legacy audit")
        self.assertFalse((self.run / "AUDITS/old.md").exists())
        self.assertEqual((self.run / "APPROACHES/A001.md").read_text(), "Original mathematical record")
        reports = [path.read_text() for path in self.reports()]
        self.assertEqual(len(reports), 3)
        self.assertTrue(all(report.count("Audit started:") == 1 for report in reports))
        self.assertEqual(len({id(call[3]) for call in self.calls}), 3)
        archived = next(event for event in self.events if event.get("status") == "archived")
        self.assertEqual(archived["archivedPaths"], {"AUDITS/old.md": "audit_history/old.md"})

    def test_disable_cancels_only_its_slot_and_model_change_is_for_next_batch(self):
        entered = threading.Event()
        release = threading.Event()
        cancellations = {}

        def provider(model, prompt, workspace, cancelled):
            cancellations[model["model"]] = cancelled
            if len(cancellations) == 2:
                entered.set()
            release.wait(2)
            return self.provider(model, prompt, workspace, cancelled)

        scheduler = self.scheduler(provider)
        self.launch(scheduler, settings("gpt-6-astra", "claude-opus"))
        self.assertTrue(entered.wait(2))
        scheduler.update(settings("gpt-5.6-sol", "none"))
        self.assertFalse(cancellations["gpt-6-astra"].is_set())
        self.assertTrue(cancellations["opus"].is_set())
        release.set()
        self.finish(scheduler)
        self.assertEqual(len(self.reports()), 1)
        self.assertNotIn("— opus", self.reports()[0].read_text())
        self.clock.now += 1
        scheduler.update(settings("gpt-5.6-sol"))
        self.finish(scheduler)
        self.assertEqual(self.calls[-1][0]["model"], "gpt-5.6-sol")

    def test_no_overlapping_batches_and_pause_cancels_all(self):
        entered = threading.Event()

        def provider(model, prompt, workspace, cancelled):
            self.calls.append(model)
            entered.set()
            self.assertTrue(cancelled.wait(2))
            return None

        scheduler = self.scheduler(provider)
        self.launch(scheduler)
        self.assertTrue(entered.wait(2))
        self.clock.now = 100
        scheduler.update(settings("gpt-6-astra"))
        self.assertEqual(len(self.calls), 1)
        scheduler.update(settings("gpt-6-astra"), active=False)
        self.finish(scheduler)
        self.assertEqual(self.reports(), [])
        self.assertEqual(scheduler.status()["runningSlots"], [])

    def test_failed_auditor_does_not_block_others_or_retry_before_next_interval(self):
        def provider(model, prompt, workspace, cancelled):
            if model["provider"] == "claude":
                raise RuntimeError("Please sign in to Claude Code.")
            return self.provider(model, prompt, workspace, cancelled)

        after = Mock(side_effect=lambda engine: self.assertEqual(len(self.reports()), 1))
        scheduler = self.scheduler(provider, after_batch=after)
        options = settings("claude-opus", "gpt-6-astra")
        self.launch(scheduler, options)
        self.finish(scheduler)
        after.assert_called_once_with(scheduler)
        self.assertIn("sign in", scheduler.status()["lastError"])
        self.assertIn("gpt-6-astra", self.reports()[0].read_text())
        old_thread = scheduler.thread
        scheduler.update(options)
        self.assertIs(scheduler.thread, old_thread)
        self.assertTrue(scheduler.active)

    def test_rotation_preserves_filename_collisions_and_moves_without_copying(self):
        (self.run / "AUDITS").mkdir()
        (self.run / "audit_history").mkdir()
        original = self.run / "AUDITS/report.md"
        original.write_text("Latest old report")
        inode = original.stat().st_ino
        (self.run / "audit_history/report.md").write_text("Earlier version")
        (self.run / "audit_history/report-previous-2.md").write_text("Another earlier version")
        scheduler = self.scheduler()
        self.launch(scheduler)
        self.finish(scheduler)
        self.assertFalse(original.exists())
        self.assertEqual((self.run / "audit_history/report.md").read_text(), "Earlier version")
        self.assertEqual((self.run / "audit_history/report-previous-2.md").read_text(), "Another earlier version")
        archived = self.run / "audit_history/report-previous-3.md"
        self.assertEqual(archived.read_text(), "Latest old report")
        self.assertEqual(archived.stat().st_ino, inode)
        self.assertEqual(len(self.reports()), 1)

    def test_rotation_failure_preserves_original_and_prevents_model_start(self):
        (self.run / "AUDITS").mkdir()
        original = self.run / "AUDITS/old.md"
        original.write_text("Preserve this report")
        after = Mock()
        scheduler = self.scheduler(after_batch=after)
        with patch.object(audits.os, "link", side_effect=OSError("Archive unavailable")):
            self.launch(scheduler)
            self.finish(scheduler)
        self.assertEqual(original.read_text(), "Preserve this report")
        self.assertEqual(self.calls, [])
        self.assertIn("Archive unavailable", scheduler.status()["lastError"])
        after.assert_called_once_with(scheduler)

    def test_history_symlink_does_not_move_reports_outside_workspace(self):
        (self.run / "AUDITS").mkdir()
        (self.run / "AUDITS/old.md").write_text("Old audit")
        (self.run / "audit_history").symlink_to(self.run / "APPROACHES")
        scheduler = self.scheduler()
        self.launch(scheduler)
        self.finish(scheduler)
        self.assertEqual((self.run / "AUDITS/old.md").read_text(), "Old audit")
        self.assertFalse((self.run / "APPROACHES/old.md").exists())
        self.assertEqual(self.calls, [])
        self.assertIn("symlinks", scheduler.status()["lastError"])

    def test_unavailable_or_unpaused_batch_keeps_current_reports(self):
        (self.run / "AUDITS").mkdir()
        original = self.run / "AUDITS/old.md"
        original.write_text("Current advice")
        cases = ({"preflight": lambda model: False}, {"before_batch": lambda engine: False})
        for options in cases:
            scheduler = self.scheduler(**options)
            self.launch(scheduler)
            self.finish(scheduler)
            self.assertEqual(original.read_text(), "Current advice")
            self.assertFalse((self.run / "audit_history").exists())
            self.assertEqual(self.calls, [])

    def test_runtime_failure_leaves_only_history_from_previous_batch(self):
        (self.run / "AUDITS").mkdir()
        (self.run / "AUDITS/old.md").write_text("Old advice")
        scheduler = self.scheduler(Mock(side_effect=RuntimeError("Model unavailable")))
        self.launch(scheduler)
        self.finish(scheduler)
        self.assertEqual(list((self.run / "AUDITS").iterdir()), [])
        self.assertEqual((self.run / "audit_history/old.md").read_text(), "Old advice")

    def test_report_does_not_follow_symlink_or_modify_research_files(self):
        (self.run / "AUDITS").symlink_to(self.run / "APPROACHES")
        scheduler = self.scheduler()
        self.launch(scheduler)
        self.finish(scheduler)
        self.assertEqual((self.run / "PROVED.md").read_text(), "Lemma")
        self.assertEqual(len(list((self.run / "APPROACHES").glob("*.md"))), 2)
        self.assertTrue(scheduler.status()["lastError"])

    def test_hooks_pause_before_workspace_audit_and_resume_after_all_reports(self):
        order = []
        options = settings("gpt-6-astra", "gpt-5.6-sol")

        def before(engine):
            order.append("pause")
            engine.update(options, active=False, paused_for_audit=True)
            (self.run / "PROVED.md").write_text("Saved before pause")
            return True

        def provider(model, prompt, workspace, cancelled):
            self.assertEqual((workspace / "PROVED.md").read_text(), "Saved before pause")
            self.assertFalse(cancelled.is_set())
            order.append("audit")
            self.clock.now += 5
            return self.provider(model, prompt, workspace, cancelled)

        def after(engine):
            self.assertEqual(len(self.reports()), 2)
            self.assertEqual(engine.checkpoint()["elapsedSeconds"], 1)
            order.append("resume")
            engine.update(options, active=True)

        scheduler = self.scheduler(provider, before_batch=before, after_batch=after)
        self.launch(scheduler, options)
        self.finish(scheduler)
        self.assertEqual(order, ["pause", "audit", "audit", "resume"])

    def test_declined_or_failed_pause_hook_never_starts_a_model(self):
        for fail in (False, True):
            after = Mock()

            def before(engine):
                if fail:
                    raise RuntimeError("Cannot pause the author safely")
                return False

            scheduler = self.scheduler(before_batch=before, after_batch=after)
            self.launch(scheduler)
            self.finish(scheduler)
            after.assert_called_once_with(scheduler)
            self.assertEqual(self.calls, [])
            self.assertEqual(self.reports(), [])

    def test_user_pause_during_audit_cancels_batch_and_always_calls_after_hook(self):
        entered = threading.Event()
        after = Mock()
        options = settings("gpt-6-astra")

        def before(engine):
            engine.update(options, active=False, paused_for_audit=True)
            return True

        def provider(model, prompt, workspace, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(2))
            return None

        scheduler = self.scheduler(provider, before_batch=before, after_batch=after)
        self.launch(scheduler, options)
        self.assertTrue(entered.wait(2))
        scheduler.update(options, active=False, paused_for_audit=False)
        self.finish(scheduler)
        after.assert_called_once_with(scheduler)
        self.assertEqual(self.reports(), [])

    def test_consecutive_batches_have_distinct_report_files(self):
        scheduler = self.scheduler()
        self.launch(scheduler)
        self.finish(scheduler)
        first = self.reports()[0]
        original = first.read_text()
        self.clock.now += 1
        scheduler.update(settings("gpt-6-astra"))
        self.finish(scheduler)
        self.assertEqual(len(self.reports()), 1)
        self.assertNotEqual(self.reports()[0].name, first.name)
        self.assertFalse(first.exists())
        self.assertEqual((self.run / "audit_history" / first.name).read_text(), original)


class AuditProviderTests(unittest.TestCase):
    def test_streams_public_activity_before_completion_for_each_provider(self):
        cases = {
            "claude": [
                {"type": "system", "subtype": "init"},
                {"type": "stream_event", "event": {"type": "message_start"}},
                {"type": "stream_event", "event": {"type": "content_block_delta",
                 "delta": {"type": "thinking_delta", "thinking": "Private reasoning must not be copied"}}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "PROVED.md"}}]}},
                {"type": "result", "result": "Independent report", "modelUsage": {"resolved-model": {}}},
            ],
            "kimi": [
                {"role": "assistant", "reasoning_content": "Private reasoning must not be copied",
                 "tool_calls": [{"function": {"name": "Read", "arguments": '{"path":"PROVED.md"}'}}]},
                {"role": "assistant", "content": "Independent report"},
            ],
            "codex": [
                {"type": "thread.started", "thread_id": "example"},
                {"type": "item.started", "item": {"type": "web_search"}},
                {"type": "item.completed", "item": {"type": "reasoning", "text": "Private reasoning must not be copied"}},
                {"type": "item.started", "item": {"type": "command_execution"}},
            ],
        }
        for provider, messages in cases.items():
            with self.subTest(provider=provider):
                activity, finished = [], []

                def process(command, **kwargs):
                    if provider == "claude":
                        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
                        self.assertIn("--verbose", command)
                        self.assertIn("--include-partial-messages", command)
                    # Split a JSON record across reads to exercise incremental buffering.
                    lines = [json.dumps(message) + "\n" for message in messages]
                    chunks = iter([lines[0][:12], lines[0][12:], *lines[1:]])
                    child = Mock(stdin=io.StringIO(), returncode=None)

                    def poll():
                        chunk = next(chunks, None)
                        if chunk is None:
                            if provider == "codex":
                                Path(command[command.index("-o") + 1]).write_text("Independent report")
                            finished.append(True)
                            child.returncode = 0
                            return 0
                        kwargs["stdout"].write(chunk)
                        kwargs["stdout"].flush()
                        return None

                    child.poll.side_effect = poll
                    return child

                def record(status, text):
                    self.assertFalse(finished, "Activity should arrive while the CLI is still running")
                    activity.append((status, text))

                cancel = Mock(is_set=lambda: False, wait=lambda seconds: False)
                with tempfile.TemporaryDirectory() as directory, patch.object(audits, "resolve_executable", return_value=provider), patch.object(audits.subprocess, "Popen", side_effect=process):
                    result = audits.run_auditor({"provider": provider, "model": "selected"},
                                               "Audit", Path(directory), cancel, on_activity=record)
                self.assertEqual(result["text"], "Independent report")
                self.assertTrue(any(status == "working" for status, _ in activity))
                self.assertNotIn("Private reasoning", str(activity))
                if provider == "claude":
                    self.assertEqual(result["model"], "resolved-model")
                if provider in {"claude", "kimi"}:
                    self.assertIn(("working", "Read: PROVED.md"), activity)

    def test_startup_and_auth_errors_do_not_count_as_model_recovery(self):
        for message in (
            {"type": "system", "subtype": "init"}, {"type": "thread.started"},
            {"type": "assistant", "error": "authentication_failed"},
            {"type": "result", "is_error": True, "result": "Not logged in"},
        ):
            self.assertEqual(audits.audit_activity(message), [])
        self.assertEqual(audits.audit_activity({"type": "system", "subtype": "api_retry",
                         "attempt": 2, "error_status": 429, "error": "Rate limit"}),
                         [("retrying", "Request retry 2: 429.")])

    def test_stream_accepts_a_utf8_character_split_between_reads(self):
        payload = json.dumps({"type": "result", "result": "中文 audit"}, ensure_ascii=False).encode()
        split = payload.index("中".encode()) + 1
        chunks = iter([payload[:split], payload[split:]])

        def process(command, **kwargs):
            child = Mock(stdin=io.StringIO(), returncode=None)
            def poll():
                chunk = next(chunks, None)
                if chunk is None:
                    child.returncode = 0
                    return 0
                os.write(kwargs["stdout"].fileno(), chunk)
                return None
            child.poll.side_effect = poll
            return child

        with tempfile.TemporaryDirectory() as directory, patch.object(audits, "resolve_executable", return_value="claude"), patch.object(audits.subprocess, "Popen", side_effect=process):
            report = audits.run_auditor({"provider": "claude", "model": "opus"}, "Audit", Path(directory),
                                       Mock(is_set=lambda: False, wait=lambda seconds: False))
        self.assertEqual(report["text"], "中文 audit")

    def fake_process(self, command, **kwargs):
        self.command = command
        self.environment = kwargs["env"]
        if "--output-format" in command:
            json.dump({"result": "An independent report", "modelUsage": {"claude-opus-4-6": {}}}, kwargs["stdout"])
            kwargs["stdout"].flush()
        else:
            Path(command[command.index("-o") + 1]).write_text("An independent report")
        return Mock(stdin=io.StringIO(), returncode=0, poll=lambda: 0)

    def test_codex_tools_and_session_are_isolated(self):
        model = next(row for row in audits.load_config()["models"] if row["value"] == "gpt-6-astra")
        with tempfile.TemporaryDirectory() as directory, patch.object(audits.shutil, "which", return_value="codex"), patch.object(audits.subprocess, "Popen", side_effect=self.fake_process):
            result = audits.run_auditor(model, "Read and advise", Path(directory), threading.Event())
        self.assertEqual(result["text"], "An independent report")
        self.assertIn("read-only", self.command)
        self.assertIn("--ephemeral", self.command)
        self.assertIn("--ignore-user-config", self.command)
        self.assertIn('web_search="live"', self.command)
        self.assertEqual(self.command[self.command.index("--disable") + 1], "multi_agent")
        self.assertNotIn("resume", self.command)

    def test_codex_uses_selected_effort_including_yaml_customization(self):
        models = [row for row in audits.load_config()["models"] if row.get("provider") == "codex"]
        models.append({"provider": "codex", "model": "custom-codex", "effort": "medium"})
        for model in models:
            with self.subTest(model=model["model"]), tempfile.TemporaryDirectory() as directory, patch.object(audits, "resolve_executable", return_value="codex"), patch.object(audits.subprocess, "Popen", side_effect=self.fake_process):
                audits.run_auditor(model, "Read and advise", Path(directory), threading.Event())
            self.assertEqual(self.command[self.command.index("-m") + 1], model["model"])
            overrides = [self.command[index + 1] for index, arg in enumerate(self.command) if arg == "-c"]
            self.assertIn("model_reasoning_effort=" + json.dumps(model["effort"]), overrides)
            self.assertEqual(sum(value.startswith("model_reasoning_effort=") for value in overrides), 1)

    def test_claude_selected_model_and_effort_override_inherited_low(self):
        models = [row for row in audits.load_config()["models"] if row.get("provider") == "claude"]
        models.append({"provider": "claude", "model": "custom-claude", "effort": "high"})
        for model in models:
            with self.subTest(model=model["model"]), tempfile.TemporaryDirectory() as directory, patch.object(audits, "resolve_executable", return_value="claude"), patch.object(audits.subprocess, "Popen", side_effect=self.fake_process), patch.object(audits.runtime, "environment", return_value={"CLAUDE_CODE_EFFORT_LEVEL": "low"}):
                audits.run_auditor(model, "Read and advise", Path(directory), threading.Event())
            self.assertEqual(self.command[self.command.index("--model") + 1], model["model"])
            if model.get("effort"):
                self.assertEqual(self.command[self.command.index("--effort") + 1], model["effort"])
                self.assertEqual(self.environment["CLAUDE_CODE_EFFORT_LEVEL"], model["effort"])
            else:
                self.assertNotIn("--effort", self.command)
                self.assertEqual(self.environment["CLAUDE_CODE_EFFORT_LEVEL"], "low")

    def test_claude_keeps_subscription_auth_and_reports_resolved_model(self):
        model = next(row for row in audits.load_config()["models"] if row["value"] == "claude-opus")
        with tempfile.TemporaryDirectory() as directory, patch.object(audits.shutil, "which", return_value="claude"), patch.object(audits.subprocess, "Popen", side_effect=self.fake_process), patch.dict(os.environ, {"ANTHROPIC_API_KEY": "unwanted-api-credential", "CLAUDE_CODE_OAUTH_TOKEN": "subscription-credential"}):
            result = audits.run_auditor(model, "Read and advise", Path(directory), threading.Event())
        self.assertEqual(result["model"], "claude-opus-4-6")
        self.assertNotIn("--bare", self.command)
        self.assertIn("--safe-mode", self.command)
        self.assertIn("--strict-mcp-config", self.command)
        self.assertEqual(self.command[self.command.index("--tools") + 1], "Read,Glob,Grep,WebSearch,WebFetch")
        self.assertNotIn("--fallback-model", self.command)
        self.assertNotIn("ANTHROPIC_API_KEY", self.environment)
        self.assertEqual(self.environment["CLAUDE_CODE_OAUTH_TOKEN"], "subscription-credential")

    def test_claude_auth_error_from_stdout_is_visible(self):
        def process(command, **kwargs):
            json.dump({"is_error": True, "result": "Not logged in. Run claude auth login."}, kwargs["stdout"])
            kwargs["stdout"].flush()
            return Mock(stdin=io.StringIO(), returncode=1, poll=lambda: 1)

        model = {"provider": "claude", "model": "opus"}
        with tempfile.TemporaryDirectory() as directory, patch.object(audits.shutil, "which", return_value="claude"), patch.object(audits.subprocess, "Popen", side_effect=process):
            with self.assertRaisesRegex(RuntimeError, "claude auth login"):
                audits.run_auditor(model, "Audit", Path(directory), threading.Event())

    def test_already_cancelled_request_never_starts_a_process(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(audits.subprocess, "Popen") as process:
            self.assertIsNone(audits.run_auditor({}, "", Path("."), cancelled))
        process.assert_not_called()

    def test_local_preflight_uses_same_resolver_without_starting_a_process(self):
        with patch.object(audits.shutil, "which", return_value=None), patch.object(audits.subprocess, "Popen") as process:
            with self.assertRaisesRegex(RuntimeError, "Claude CLI is not installed"):
                audits.resolve_executable({"provider": "claude"})
        process.assert_not_called()

    def test_kimi_discovers_native_install_and_limits_tools(self):
        model = {"provider": "kimi", "model": "kimi-code/kimi-for-coding"}

        def process(command, **kwargs):
            self.command = command
            agent = Path(command[command.index("--agent-file") + 1]).read_text()
            metadata = audits.yaml.safe_load(agent.split("---", 2)[1])
            self.assertEqual(metadata["tools"], ["Read", "Grep", "Glob", "WebSearch", "FetchURL"])
            self.assertEqual(metadata["subagents"], [])
            self.assertIn("Read and advise", agent)
            json.dump({"role": "assistant", "content": "Working", "tool_calls": [{}]}, kwargs["stdout"])
            kwargs["stdout"].write("\n")
            json.dump({"role": "assistant", "content": [{"type": "text", "text": "Independent Kimi report"}]}, kwargs["stdout"])
            kwargs["stdout"].write("\n")
            kwargs["stdout"].flush()
            return Mock(stdin=io.StringIO(), returncode=0, poll=lambda: 0)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = home / ".kimi-code/bin/kimi"
            executable.parent.mkdir(parents=True)
            executable.write_text("Unused executable")
            executable.chmod(0o700)
            with patch.object(audits.shutil, "which", return_value=None), patch.object(audits.Path, "home", return_value=home), patch.object(audits.subprocess, "Popen", side_effect=process):
                result = audits.run_auditor(model, "Read and advise", home, threading.Event())
        self.assertEqual(result["text"], "Independent Kimi report")
        self.assertEqual(self.command[0], str(executable))
        self.assertEqual(self.command[self.command.index("--prompt") + 1], audits.KIMI_START_PROMPT)
        self.assertNotIn("--session", self.command)
        self.assertNotIn("--continue", self.command)

    def test_kimi_selected_model_and_effort_preserve_existing_home_and_auth(self):
        models = [row for row in audits.load_config()["models"] if row.get("provider") == "kimi"]
        models.extend([
            {"provider": "kimi", "model": "custom-kimi", "effort": "high"},
            {"provider": "kimi", "model": "custom-kimi-without-effort"},
        ])

        def process(command, **kwargs):
            self.command = command
            self.environment = kwargs["env"]
            json.dump({"role": "assistant", "content": "Independent Kimi report"}, kwargs["stdout"])
            kwargs["stdout"].write("\n")
            kwargs["stdout"].flush()
            return Mock(stdin=io.StringIO(), returncode=0, poll=lambda: 0)

        for model in models:
            inherited = {"KIMI_CODE_HOME": "/existing/kimi-profile", "KIMI_MODEL_THINKING_EFFORT": "low"}
            with self.subTest(model=model["model"]), tempfile.TemporaryDirectory() as directory, patch.object(audits, "resolve_executable", return_value="kimi"), patch.object(audits.runtime, "environment", return_value=inherited.copy()), patch.object(audits.subprocess, "Popen", side_effect=process):
                result = audits.run_auditor(model, "Read and advise", Path(directory), threading.Event())
            self.assertEqual(result, {"text": "Independent Kimi report", "model": model["model"]})
            self.assertEqual(self.command[self.command.index("--model") + 1], model["model"])
            expected = {**inherited, "KIMI_MODEL_THINKING_EFFORT": model.get("effort", "low")}
            self.assertEqual(self.environment, expected)
            self.assertNotIn("--config", self.command)
            self.assertNotIn("--config-file", self.command)

    def test_invalid_settings_do_not_enable_unsupported_models(self):
        for options in ({"intervalHours": 0}, {"intervalHours": float("inf")},
                        {"intervalHours": True}, {"models": ["none"] * 4},
                        {"models": ["unknown", "none", "none"]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                audits.normalize_settings(options)


if __name__ == "__main__":
    unittest.main()
