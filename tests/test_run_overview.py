"""Honest run outcomes and crash recovery, without model requests."""

import io
import json
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ui import server


class RunRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.app = server.App(runs=self.runs)
        self.app._new_run("Original negative-weight problem")
        self.run = self.app.run_dir
        self.statement = "Exact approved negative-weight shortest-path statement"
        self.app._save("checked-statement.md", "# Checked statement\n\n" + self.statement)
        self.app.state.update(fileManagement=False, skipStatementReview=True,
                              goalThreadId="saved-author", elapsedSeconds=100,
                              resumedAt="2026-10-12T10:00:00+00:00")
        self.app._save_job_settings(self.app.state)

    def events(self, *events):
        for event in events:
            if event.get("time"):
                with patch.object(server, "datetime") as clock:
                    clock.now.return_value = datetime.fromisoformat(event["time"])
                    self.app.add_trace(event)
            else:
                self.app.add_trace(event)

    def restore(self):
        return server.restore_saved_app(server.App(self.app.trace_file, self.runs))

    def author_events(self):
        self.events(
            {"kind": "request", "stage": "solve", "node": "author", "time": "2026-10-12T10:00:00+00:00"},
            {"kind": "status", "stage": "solve", "node": "author", "label": "Goal resumed",
             "threadId": "saved-author", "time": "2026-10-12T10:00:01+00:00"},
            {"kind": "codex_event", "stage": "solve", "node": "author", "event": {},
             "time": "2026-10-12T10:03:00+00:00"},
        )

    def test_running_checkpoint_after_crash_is_paused_and_resumes_exact_thread(self):
        self.author_events()
        self.app._save_pause({"status": "running", "node": "author", "stage": "solve",
                              "state": {"statement": self.statement}, "goalThreadId": "saved-author",
                              "elapsedSeconds": 100})
        self.app._save("partial-output.md", "Exploration, not a finished proof")
        restored = self.restore()
        state = restored.snapshot()
        self.assertEqual(state["phase"], "paused")
        self.assertEqual(state["finishedAt"], "")
        self.assertEqual(state["resultKind"], "")
        self.assertFalse(state["workerActive"])
        self.assertFalse(state["processAlive"])
        self.assertEqual(state["statement"], self.statement)
        self.assertEqual(state["activeElapsedSeconds"], 280)
        self.assertEqual(state["autoResumeAt"], 0)
        before = self.app.trace_file.read_bytes()
        with patch.object(server.subprocess, "Popen", return_value=Mock(stdin=io.StringIO())) as launch, \
                patch.object(restored, "_spawn_worker"):
            restored.resume()
        argv = launch.call_args.args[0]
        self.assertEqual(launch.call_args.kwargs["cwd"], self.run)
        self.assertIn('goal_thread_id="saved-author"', argv)
        self.assertIn("goal_require_resume=true", argv)
        self.assertEqual(argv[argv.index("--elapsed-seconds") + 1], "280.0")
        self.assertEqual(json.loads((self.run / "workflow-input.json").read_text())["statement"], self.statement)
        self.assertEqual(restored.state["phase"], "running")
        self.assertTrue(self.app.trace_file.read_bytes().startswith(before))

    def test_crash_before_first_pause_creates_a_resumable_checkpoint(self):
        self.author_events()
        restored = self.restore()
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(restored.state["phase"], "paused")
        self.assertEqual(checkpoint["state"]["statement"], self.statement)
        self.assertEqual(checkpoint["goalThreadId"], "saved-author")

    def test_completed_answer_wins_over_stale_running_checkpoint_and_partial_work(self):
        self.author_events()
        self.app._save_pause({"status": "running", "node": "author", "stage": "solve",
                              "state": {"statement": self.statement}, "elapsedSeconds": 100})
        self.app._save("partial-output.md", "Old partial text")
        self.events({"kind": "final_result", "stage": "final", "node": "latex_compile",
                     "output": "Complete saved document"})
        restored = self.restore()
        self.assertEqual(restored.state["phase"], "done")
        self.assertEqual(restored.state["resultKind"], "final")
        self.assertEqual(restored.state["output"], "Complete saved document")
        self.assertEqual(restored.state["error"], "")

    def test_failure_report_and_clean_exit_without_result_are_incomplete(self):
        self.events({"kind": "failure_result", "stage": "final", "node": "latex_editor",
                     "summary": "Formatting failed"})
        restored = self.restore()
        self.assertEqual(restored.state["resultKind"], "failure")
        self.assertIn("incomplete", restored.state["error"])
        self.assertEqual(restored.state["output"], "Formatting failed")
        for record in (None, {"kind": "final_result", "stage": "final", "output": "  "}):
            with self.subTest(record=record):
                process = Mock(stdout=io.StringIO(json.dumps(record) + "\n" if record else ""))
                process.wait.return_value = 0
                self.app.state.update(phase="running", stage="final", activeNode="latex_editor", resultKind="")
                token = self.app.active_token = self.app.worker_token = object()
                self.app.process = process
                self.app._read_output(process, token)
                self.assertEqual(self.app.state["resultKind"], "")
                self.assertIn("final answer", self.app.state["error"])
                self.assertFalse((self.run / "final.tex").exists())


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class RunOverviewTests(unittest.TestCase):
    def test_exact_statement_progress_health_and_saved_results(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor() { this.textContent = ''; this.dataset = {}; this.children = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}
const ui = new Proxy({}, {get(target, name) {return target[name] ||= new Element();}});
const state = {runId: 'saved-run', phase: 'running', stage: 'solve', activeNode: 'author',
  draft: 'Original negative-weight statement', statement: 'Exact approved statement <script>text</script>',
  review: {statement: 'Earlier reviewed version'}, fileManagement: false,
  processAlive: true, workerActive: true, activeElapsedSeconds: 180, thinkingHours: 168,
  workflow: {nodes: {author: {label: 'Proof author'}}}, trace: [],
  authorModel: 'gpt-6.1-sol', authorEffort: 'max', criticModel: 'gpt-6-astra', criticEffort: 'high',
  criticRounds: 2, goalThreadId: 'saved-author', output: ''};
const context = {ui, state, assert, stateReceivedAt: Date.now(),
  document: {createElement: () => new Element()}, show: (element, visible) => element.hidden = !visible,
  clockText: () => '', describe: entry => entry.card || null};
const checks = `
renderRunOverview();
assert.equal(ui.runStatement.textContent, state.statement);
assert.equal(ui.runOriginalStatement.textContent, state.draft);
assert.equal(ui.originalStatementDetails.hidden, false);
assert.match(ui.runStage.textContent, /Proof author/);
assert.match(ui.runNextStep.textContent, /independent critic/);
assert.match(ui.runHealth.textContent, /^Running$/);
assert.equal(ui.resultPanel.hidden, true);
assert(ui.runMetadata.children.some(row => row.children[1].textContent.includes('gpt-6.1-sol')));
state.processAlive = false; state.workerActive = false;
renderRunOverview(); assert.match(ui.runHealth.textContent, /No active process/);
state.phase = 'paused'; state.output = 'Saved exploration';
renderRunOverview();
assert.match(ui.runNextStep.textContent, /No final answer yet/);
assert.equal(ui.resultHeading.textContent, 'Saved partial work');
assert.equal(ui.resultOutput.textContent, state.output);
assert.equal(ui.resultPanel.hidden, false);
state.phase = 'done'; state.resultKind = 'final'; state.output = 'Complete proof';
renderRunOverview();
assert.equal(ui.resultPanel.open, true);
assert.equal(ui.resultOutput.textContent, 'Complete proof');
assert.equal(completedRunLabel(state), 'Answer ready');
ui.resultPanel.open = false;
renderRunOverview(); assert.equal(ui.resultPanel.open, false);
state.resultKind = ''; state.output = '';
renderRunOverview(); assert.equal(completedRunLabel(state), 'Incomplete');
assert.match(ui.runNextStep.textContent, /No completed answer/);
state.phase = 'reviewed';
renderRunOverview(); assert.match(ui.runNextStep.textContent, /Approval is needed/);
`;
vm.runInNewContext(source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function renderWorkflow()')) + checks, context);
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js")],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
