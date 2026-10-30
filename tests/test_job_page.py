"""Job-page behaviour the redesigned UI relies on: notice, bounded log, recovery, stable lists."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class JobPageBrowserTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run([shutil.which("node"), "-e", script, str(ROOT / "ui" / "app.js")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_notice_keeps_feedback_and_shows_errors_the_run_panel_cannot(self):
        self.run_node(r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const renderer = source.slice(source.indexOf('function render(next)'), source.indexOf('async function refresh()'))
  // The status classification render() reads (a stop at the time limit).
  + source.slice(source.indexOf('// A job the author time limit stopped'),
    source.indexOf('// A time limit as the server writes it (hours_text)'));
const setup = `
let state = {}, previousPhase = 'reviewing', currentJob = 'job-1', reviewPending = false, timer, clock, polls = 0;
const ui = new Proxy({}, {get(target, name) {return target[name] ||= {
  classList: {toggle() {}}, setAttribute() {}, focus() {}, replaceChildren() {}, textContent: '', hidden: false,
};}});
const document = {activeElement: null};
const show = (element, visible) => element.hidden = !visible;
const retainTrace = entries => entries;
const ingest = () => {}, renderWorkflow = () => {}, renderClock = () => {};
const renderBatchNavigation = () => {}, refresh = () => {};
const restoreRetrievalSettings = () => {}, syncPrompts = () => {}, updateModelSummary = () => {};
const setProblemMode = () => {}, restoreWorkflowStages = () => {}, appendFormattedText = () => {};
const checkEdited = () => {};
const setTimeout = () => ++polls, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
`;
const checks = `
// A failed statement review returns the job to the composer: its error must be in the notice.
const failure = 'Statement review failed: the model returned invalid JSON.';
render({phase: 'input', error: failure});
assert.equal(ui.run.hidden, true);
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent, failure);
// A running job states its error once, in the now-card.
render({phase: 'running', stage: 'solve', activeNode: 'author', error: 'Provider retrying.'});
assert.equal(ui.notice.hidden, true);
assert.equal(ui.runDescription.textContent, 'Provider retrying.');
// Action feedback survives a poll in the same phase and is cleared by the next phase.
ui.notice.textContent = 'Enter an instruction for the proof author.';
show(ui.notice, true);
render({phase: 'running', stage: 'solve', activeNode: 'author'});
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent, 'Enter an instruction for the proof author.');
assert.equal(ui.runDescription.textContent, '');
render({phase: 'paused', stage: 'solve', activeNode: 'author'});
assert.equal(ui.notice.hidden, true);
// The bounded log opens at its newest event on a phase change and is left alone otherwise.
ui.timeline.scrollHeight = 900;
ui.timeline.scrollTop = 0;
render({phase: 'running', stage: 'solve', activeNode: 'author'});
assert.equal(ui.timeline.scrollTop, 900);
ui.timeline.scrollTop = 120;
render({phase: 'running', stage: 'solve', activeNode: 'author'});
assert.equal(ui.timeline.scrollTop, 120);
// Every working or waiting render still schedules exactly one poll.
assert.equal(polls, 5);
`;
vm.runInNewContext(setup + renderer + checks, {assert});
""")

    def test_job_page_offers_recovery_only_for_incomplete_writable_jobs(self):
        self.run_node(r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = slice('function completedRunLabel(', 'function localDateTime(')
  + slice('async function loadJobRecovery(', 'async function startReview(');
class Element {
  constructor(tagName) { this.tagName = tagName; this.children = []; this.dataset = {}; this.hidden = false;
    this.textContent = ''; this.className = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}
const setup = `
let currentJob = 'failed-job', currentBatch = '__root__', state = {};
const ui = {jobRecovery: new Element('div'), runProblemTitle: {textContent: 'P1.md'}};
const document = {createElement: tagName => new Element(tagName)};
const show = (element, visible) => element.hidden = !visible;
const jobPath = path => path + '?job=' + currentJob + '&batch=' + currentBatch;
const requested = [], called = [];
let reply = {jobs: []};
const request = async path => { requested.push(path); return reply; };
const continueStopped = (job, title) => called.push(['continue', job.runId, title]);
const resumeCheckpoint = (job, checkpoint, title) => called.push(['checkpoint', checkpoint.id, title]);
const resumeCritic = (job, title) => called.push(['critic', job.runId, title]);
`;
const checks = `
(async () => {
  // A finished answer offers nothing and asks the server nothing.
  state = {phase: 'done', resultKind: 'final', canDownloadTex: true};
  await loadJobRecovery();
  assert.equal(ui.jobRecovery.hidden, true);
  assert.equal(requested.length, 0);
  // An incomplete job lists its continuation, then its resumable checkpoints.
  state = {phase: 'done', resultKind: 'failure', error: 'LaTeX compilation is unavailable.',
    checkpoints: [{id: 'c1', resumable: true}]};
  reply = {jobs: [{runId: 'failed-job', canContinueStopped: true, continueStoppedLabel: 'Retry LaTeX editor',
    continueStoppedDescription: 'Starts only a new LaTeX-editor request.', canResumeCritic: true,
    checkpoints: [{id: 'c1', label: 'Saved proof candidate', resumable: true, resumeLabel: 'Continue at critic'},
      {id: 'c2', label: 'Old candidate', resumable: false}]}]};
  await loadJobRecovery();
  assert.deepEqual(requested, ['/jobs?job=failed-job&batch=__root__']);
  assert.equal(ui.jobRecovery.hidden, false);
  assert.equal(ui.jobRecovery.children.length, 3);
  const [heading, first, second] = ui.jobRecovery.children;
  assert.equal(heading.textContent, 'Continue from saved work');
  assert.equal(first.children[0].textContent, 'Retry LaTeX editor');
  assert.match(first.children[0].className, /primary/);
  assert.equal(second.children[0].textContent, 'Continue at critic');
  assert.match(second.children[0].className, /secondary/);
  first.children[0].onclick();
  second.children[0].onclick();
  assert.deepEqual(called, [['continue', 'failed-job', 'P1.md'], ['checkpoint', 'c1', 'P1.md']]);
  // The same state does not ask again.
  await loadJobRecovery();
  assert.equal(requested.length, 1);
  // A saved (read-only) batch never offers recovery.
  state = {...state, readOnly: true};
  await loadJobRecovery();
  assert.equal(ui.jobRecovery.hidden, true);
  assert.equal(requested.length, 1);
  // A reply for a job the user has already left is ignored.
  state = {phase: 'done', manuallyStopped: true};
  const pending = loadJobRecovery();
  currentJob = 'another-job';
  await pending;
  assert.equal(ui.jobRecovery.hidden, true);
  assert.equal(ui.jobRecovery.children.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
`;
vm.runInNewContext(setup + code + checks, {assert, console, process, Element});
""")

    def test_batch_list_keeps_its_rows_across_telemetry_only_polls(self):
        self.run_node(r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function request('), source.indexOf('function selectedProblemMode('))
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function localDateTime('));
class Element {
  constructor() { this.children = []; this.dataset = {}; this.hidden = false; this.textContent = ''; this.classList = {toggle() {}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this[key] = value; }
  remove() {}
}
const ui = new Proxy({}, {get: (target, key) => target[key] ||= new Element()});
const setup = `
let currentJob = '', currentBatch = 'v4', composingProblem = false, batchCatalog = [{id: 'v4', label: 'Batch four', current: true}];
let state = {phase: 'input'}, requests = Promise.resolve(), previousPhase = '', timer, clock, jobsTimer;
const sessionToken = 'token', timelineRows = new Map(), detailRows = new Map();
const location = {pathname: '/'}, history = {pushState() {}};
const clearTimeout = () => {}, clearInterval = () => {}, setTimeout = () => 1;
const document = {createElement: () => new Element()};
const window = {open() {}};
const localDateTime = value => value;
const confirm = () => true, alert = () => {};
const refresh = async () => {};
`;
const checks = `
const job = {runId: 'r1', title: 'P1', phase: 'running', stageLabel: 'Proof author', startedAt: 's',
  activeElapsedSeconds: 10, processAlive: true, workerActive: true, lastActivityAt: 't1',
  retrieval: {mode: 'historical', cutoffExclusiveUtc: '2024-01-01T00:00:00Z', counts: {searches: 1}}};
renderJobs([job]);
const card = ui.jobsList.children[0];
const remove = card.children[1].children.find(button => button.textContent === 'Delete');
assert.equal(remove.hidden, true); // an active job cannot be deleted, so Delete is not offered
// A poll that changes only telemetry keeps the same rows (keyboard focus and hover survive).
renderJobs([{...job, activeElapsedSeconds: 12, processAlive: false, lastActivityAt: 't2',
  retrieval: {...job.retrieval, counts: {searches: 2}}}]);
assert.equal(ui.jobsList.children[0], card);
// A visible change rebuilds the list and its summary.
renderJobs([{...job, phase: 'paused'}]);
assert.notEqual(ui.jobsList.children[0], card);
assert.equal(ui.jobsCount.textContent, '1 problem · 1 paused');
assert.equal(ui.jobsList.children[0].children[1].children.find(button => button.textContent === 'Delete').hidden, false);
`;
vm.runInNewContext(setup + code + checks, {ui, assert, URLSearchParams, Element});
""")

    def test_first_reasoning_summary_replaces_the_placeholder_then_appends(self):
        self.run_node(r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = slice('function describe(entry)', 'function cardIcon(')
  + slice('function upsertTimeline(', '// Keep only exact application prompts');
class Element {
  constructor(tagName) { this.tagName = tagName; this.children = []; this.dataset = {}; this.hidden = false; this.textContent = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  remove() {}
}
const setup = `
const state = {reasoningSummary: 'concise'};
const ui = {timeline: new Element('ol')};
ui.timeline.scrollHeight = 0; ui.timeline.scrollTop = 0; ui.timeline.clientHeight = 0;
const timelineRows = new Map();
let activeFilter = 'all';
const document = {createElement: tagName => new Element(tagName)};
const appendFormattedText = (element, value) => { element.textContent = value; };
const clockText = () => '', cardIcon = () => 'R';
`;
const checks = `
const event = (method, params) => ({kind: 'codex_event', time: 't', event: {method, params}});
const started = describe(event('item/started', {item: {id: 'r1', type: 'reasoning'}}));
assert.equal(started.placeholder, true);
upsertTimeline(started);
assert.match(timelineRows.get('reasoning:r1')._card.text, /^Reasoning has started/);
upsertTimeline(describe(event('item/reasoning/summaryTextDelta', {itemId: 'r1', delta: '**Packing bound** '})));
assert.equal(timelineRows.get('reasoning:r1')._card.text, '**Packing bound** ');
upsertTimeline(describe(event('item/reasoning/summaryTextDelta', {itemId: 'r1', delta: 'The ball of radius 2p.'})));
assert.equal(timelineRows.get('reasoning:r1')._card.text, '**Packing bound** The ball of radius 2p.');
assert.equal(ui.timeline.children.length, 1);
`;
vm.runInNewContext(setup + code + checks, {assert, Element});
""")


if __name__ == "__main__":
    unittest.main()
