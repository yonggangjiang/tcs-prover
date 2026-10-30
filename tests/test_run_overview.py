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

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
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
        self.app.state.update(skipStatementReview=True,
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
        # I-065: the interrupted author also continues by itself, a minute after the start.
        self.assertTrue(state["autoResumeAt"])
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
  constructor(tagName) { this.tagName = tagName; this.textContent = ''; this.dataset = {}; this.children = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelectorAll(tagName) { return this.children.flatMap(child =>
    [...(child.tagName === tagName ? [child] : []), ...child.querySelectorAll(tagName)]); }
}
const ui = new Proxy({}, {get(target, name) {return target[name] ||= new Element();}});
const state = {runId: 'saved-run', phase: 'running', stage: 'solve', activeNode: 'author',
  draft: 'Original negative-weight statement', statement: 'Exact approved statement <script>text</script>',
  review: {statement: 'Earlier reviewed version'},
  processAlive: true, workerActive: true, activeElapsedSeconds: 180, thinkingHours: 168,
  workflow: {nodes: {author: {label: 'Proof author'}}}, trace: [],
  authorModel: 'gpt-6.1-sol', authorEffort: 'max', criticModel: 'gpt-6-astra', criticEffort: 'high',
  criticRounds: 2, criticCount: 3, goalThreadId: 'saved-author', output: ''};
const context = {ui, state, assert, stateReceivedAt: Date.now(),
  document: {createElement: tagName => new Element(tagName)}, show: (element, visible) => element.hidden = !visible,
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
const settingsRow = label => ui.runMetadata.children.find(row => row.children[0].textContent === label).children[1].textContent;
assert.equal(settingsRow('Independent critic'), 'gpt-6-astra · high · 3 independent critics as subagents · max 2 consecutive rounds');
state.criticCount = 1;
renderRunOverview();
assert.equal(settingsRow('Independent critic'), 'gpt-6-astra · high · 1 independent critic as a subagent · max 2 consecutive rounds');
// An older job that did not record its number of critics is not credited with any.
state.criticCount = 3; state.criticCountRecorded = false;
renderRunOverview();
assert.equal(settingsRow('Independent critic'), 'gpt-6-astra · high · max 2 consecutive rounds');
delete state.criticCountRecorded;
// A job from before the Author subagents setting: its critic ran its independent critics itself, and its author
// is not described with the setting.
assert.equal(settingsRow('Proof author'), 'gpt-6.1-sol · max · no subagents');
state.authorSubagentsRecorded = false;
renderRunOverview();
assert.equal(settingsRow('Independent critic'), 'gpt-6-astra · high · 3 independent critics · max 2 consecutive rounds');
assert.equal(settingsRow('Proof author'), 'gpt-6.1-sol · max');
delete state.authorSubagentsRecorded;
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
renderRunOverview(); assert.match(ui.runNextStep.textContent, /Approve the statement to continue/);
`;
vm.runInNewContext(source.slice(source.indexOf('function retrievalConfiguration('), source.indexOf('function renderJobs('))
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function renderWorkflow()')) + checks, context);
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js")],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_stage_line_counts_the_round_whose_critics_are_running(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor(tagName) { this.tagName = tagName; this.textContent = ''; this.dataset = {}; this.children = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelectorAll(tagName) { return this.children.flatMap(child =>
    [...(child.tagName === tagName ? [child] : []), ...child.querySelectorAll(tagName)]); }
}
const ui = new Proxy({}, {get(target, name) {return target[name] ||= new Element();}});
let clock = 0;
const at = () => new Date(Date.UTC(2026, 9, 28, 10, 0, clock++)).toISOString();
// The runner's records of one critic visit: its step and proof, its three critics, then, once they
// have reported, the coordinator's request and decision.
const visit = (number, decision) => [
  {kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: number, time: at()},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: number,
   manifest: {visit: number}, time: at()},
  {kind: 'status', stage: 'critic', node: 'critic', label: '3 independent critics started', auditCount: 3, time: at()},
  ...[1, 2, 3].map(index => ({kind: 'request', stage: 'critic', label: `Independent critic audit ${index}`,
    attempt: 1, text: `Critic ${index} of 3`, time: at()})),
  ...(decision ? [
    {kind: 'request', stage: 'critic', label: 'Critic coordinator', attempt: 1, text: 'Reports',
     candidateText: 'Proof', time: at()},
    {kind: 'critic_result', stage: 'critic', label: `Critic round ${number}`, round: number, outcome: decision,
     time: at(), report: {verdict: decision === 'reject' ? 'reject' : 'pass',
       bugs: decision === 'reject' ? 'A gap.' : '', solution: 'Proof, edited'}}] : [])];
const author = stage => [
  {kind: 'status', stage, node: 'author', label: 'Workflow step: author', visit: 1, time: at()},
  {kind: 'request', stage, node: 'author', label: 'Exact solve input', attempt: 1, time: at()},
  {kind: 'author_result', stage, label: 'Author result', text: 'Proof', time: at()}];
const state = {runId: 'r', phase: 'running', stage: 'critic', activeNode: 'critic', statement: 'S', draft: 'S',
  processAlive: true, workerActive: true, activeElapsedSeconds: 60, thinkingHours: 0, criticRounds: 2,
  criticCount: 3, workflow: {nodes: {author: {label: 'Proof author'}, critic: {label: 'Independent critic'}}},
  output: ''};
const context = {ui, state, assert, visit, author, at, stateReceivedAt: Date.now(),
  document: {createElement: tagName => new Element(tagName)}, show: (element, visible) => element.hidden = !visible,
  clockText: () => '', describe: () => null};
const checks = `
// Round 2 while its critics run, before its coordinator is asked: the round badge (state.round) says 2 too.
state.trace = [...author('solve'), ...visit(1, 'fixed'), ...visit(2)];
for (const round of [2, undefined]) {
  state.round = round;
  renderRunOverview();
  assert.equal(ui.runStage.textContent, 'Independent critic · cycle 1 · round 2');
}
// After a rejection, the repaired proof's first round while its critics run.
state.trace = [...author('solve'), ...visit(1, 'fixed'), ...visit(2, 'reject'), ...author('repair'), ...visit(1)];
for (const round of [1, undefined]) {
  state.round = round;
  renderRunOverview();
  assert.equal(ui.runStage.textContent, 'Independent critic · cycle 2 · round 1');
}
// Round 1's coordinator was interrupted and the same visit resumed: the stage line and the review
// history name round 2 alike once it runs.
const interrupted = visit(1).concat([{kind: 'request', stage: 'critic', label: 'Critic coordinator', attempt: 1,
  text: 'Reports', candidateText: 'Proof', time: at()}, {kind: 'workflow_paused', stage: 'critic', node: 'critic',
  time: at()}]);
state.trace = [...author('solve'), ...interrupted, ...visit(1, 'fixed'), ...visit(2)];
for (const round of [2, undefined]) {
  state.round = round;
  renderRunOverview();
  assert.equal(ui.runStage.textContent, 'Independent critic · cycle 1 · round 2');
  const history = ui.criticReviewHistory.children;
  assert.match(history[0].textContent, /1 cycle · 2 rounds/);
  assert.equal(history.filter(node => node.className === 'critic-cycle')[0].children
    .filter(node => node.className === 'critic-review').map(node => node.children[0].textContent.split(' · ')[0])
    .join(', '), 'Round 1 (resumed), Round 2');
}
`;
vm.runInNewContext(source.slice(source.indexOf('function retrievalConfiguration('), source.indexOf('function renderJobs('))
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function renderWorkflow()')) + checks, context);
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js")],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
