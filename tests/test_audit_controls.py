"""Exercise audit settings in the browser without running any model."""

from pathlib import Path
import shutil
import subprocess
import unittest


class AuditControlTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_show_details_keeps_exact_audit_prompts_and_auditor_labels(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function detailCard('), source.indexOf('function upsertDetail('));
const context = {assert};
vm.createContext(context);
vm.runInContext(code, context);
const prompt = 'Audit the following.\n\nPreserve \\lambda, 中文, and whitespace.  \n';
const request = {kind: 'request', stage: 'audit', time: '2026-09-12T17:01:01.123Z',
  label: 'Audit-3 — Kimi K3 (Max) — Agent instructions', text: prompt};
const card = context.detailCard(request);
assert.equal(card.text, prompt);
assert.equal(card.label, request.label);
assert.equal(context.detailCard({...request, stage: 'solve'}).label, 'Prompt to model');
const launch = context.detailCard({...request, time: '2026-09-12T17:01:01.124Z',
  label: 'Audit-3 — Kimi K3 (Max) — User message', text: 'Run the research audit.'});
assert.equal(launch.text, 'Run the research audit.');
assert.notEqual(launch.key, card.key);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(Path(__file__).resolve().parents[1] / "ui" / "app.js")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_audit_pause_keeps_polling_and_allows_manual_pause_or_stop(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const renderer = source.slice(source.indexOf('function managesResearchFiles('), source.indexOf('function selectedAuthorPrompt('))
  + source.slice(source.indexOf('function render(next)'), source.indexOf('async function refresh()'));
const handlers = source.slice(source.indexOf('ui.stop.onclick ='), source.indexOf('ui.home.onclick ='));
const setup = `
let state = {}, previousPhase = 'running', currentJob = 'same-job', timer, clock, polls = 0;
const ui = new Proxy({}, {get(target, name) {return target[name] ||= {
  classList: {toggle() {}}, setAttribute() {}, focus() {}, replaceChildren() {}
};}});
const document = {activeElement: null};
const show = (element, visible) => element.hidden = !visible;
const retainTrace = entries => entries;
const ingest = () => {}, syncMemoryPanel = () => {}, renderWorkflow = () => {};
const renderClock = () => {}, refresh = () => {}, renderResearchAudits = () => {};
const setTimeout = () => ++polls, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
let clicked = '';
const act = path => clicked = path;
`;
const checks = `
const author = {runId: 'audit-test', fileManagement: true, phase: 'pausing', stage: 'solve', activeNode: 'author', auditHoldingAuthor: true};
render(author);
assert.equal(ui.pause.hidden, false);
assert.equal(ui.stop.hidden, false);
assert.match(ui.runTitle.textContent, /research audits/);
render({...author, phase: 'paused'});
assert.equal(ui.resume.disabled, true);
assert.equal(ui.pause.hidden, false);
assert.equal(ui.stop.hidden, false);
assert.match(ui.runDescription.textContent, /resume automatically in the same conversation/);
assert.equal(polls, 2);
ui.pause.onclick(); assert.equal(clicked, '/pause');
ui.stop.onclick(); assert.equal(clicked, '/stop');
render({...author, phase: 'paused', auditHoldingAuthor: false});
assert.equal(ui.resume.disabled, false);
assert.equal(ui.pause.hidden, true);
assert.equal(ui.stop.hidden, true);
assert.equal(polls, 2);
render({...author, phase: 'running', auditHoldingAuthor: false});
assert.equal(ui.pause.hidden, false);
assert.equal(ui.resume.hidden, true);
assert.equal(polls, 3);
render({...author, phase: 'paused'});
assert.match(ui.runDescription.textContent, /resume automatically in the same conversation/);
assert.equal(ui.resume.disabled, true);
assert.equal(polls, 4);
render({...author, phase: 'paused', auditHoldingAuthor: false,
  researchAuditStatus: {batchActive: true}});
assert.equal(ui.resume.disabled, true);
assert.equal(polls, 5); // Keep polling through the batch's final cleanup.
render({...author, phase: 'paused', auditHoldingAuthor: false,
  researchAuditStatus: {batchActive: false}});
assert.equal(ui.resume.disabled, false);
assert.equal(polls, 5);
`;
vm.runInNewContext(setup + renderer + handlers + checks, {assert});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(Path(__file__).resolve().parents[1] / "ui" / "app.js")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_audit_report_picker_and_safe_links(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const selected = source.slice(source.indexOf('function renderMemoryChoices('), source.indexOf('async function loadMemory('))
  + source.slice(source.indexOf('function resolveArchivedMemoryFile('), source.indexOf('function syncMemoryPanel('));
const linkFormatter = source.slice(source.indexOf('function memoryLinkTarget('), source.indexOf('function renderMemoryContent('));
const setup = `
const element = () => ({children: [], options: [], append(child) {this.children.push(child);},
  replaceChildren(...options) {this.options = options;}, setAttribute() {}});
const ui = Object.fromEntries(['memoryPanel', 'memoryApproachPicker', 'memoryApproach', 'memoryDocument',
  'memoryFilename', 'memoryUpdated', 'memoryContent', 'memoryMessage', 'approachGraph'].map(name => [name, element()]));
const memoryTabs = ['INITIAL_PROMPT.md', 'APPROACHES', 'PROVED.md', 'AUDITS', 'audit_history'].map((memory, index) => ({
  dataset: {memory}, id: 'tab' + index, setAttribute() {},
}));
let memoryFile = 'AUDITS', memoryRequest = 0, memoryAnchor = '', memoryVersion = '';
const state = {trace: []};
const document = {createElement: element};
const show = (item, visible) => item.hidden = !visible;
const appendFormattedText = (item, text) => item.append(text);
const requested = [];
const loadMemory = () => requested.push(memoryFile);
const renderApproachGraph = () => {};
`;
const checks = `
const report = 'AUDITS/2026-09-12-audit-one-gpt-6-astra.md';
const files = [report, 'AUDITS/2026-09-12-audit-two-opus.md', 'audit.md'];
renderMemoryChoices({name: report, files});
assert.equal(ui.memoryApproach.value, report);
assert.equal(ui.memoryApproach.options[0].textContent, '2026-09-12-audit-one-gpt-6-astra');
assert.equal(ui.memoryApproach.options[2].textContent, 'Historical audit notebook');
selectMemoryDocument(files[1]);
assert.equal(requested.at(-1), files[1]);
assert.equal(memoryTabs[3].tabIndex, 0);
const links = element();
appendMemoryText(links, '[Full](' + report + ') [Relative](2026-09-12-audit-two-opus.md) '
  + '[Legacy](../audit.md) [Nested](AUDITS/subfolder/report.md) [Escape](AUDITS/../secret.md) '
  + '[Escape2](../../audit.md)');
const buttons = links.children.filter(child => child.className === 'memory-link');
assert.equal(buttons.length, 3);
buttons[0].onclick(); assert.equal(requested.at(-1), report);
buttons[1].onclick(); assert.equal(requested.at(-1), files[1]);
buttons[2].onclick(); assert.equal(requested.at(-1), 'audit.md');
assert.equal(memoryTabs[4].tabIndex, 0);
assert.ok(links.children.includes('[Nested](AUDITS/subfolder/report.md)'));
assert.ok(links.children.includes('[Escape](AUDITS/../secret.md)'));
assert.ok(links.children.includes('[Escape2](../../audit.md)'));
const historical = 'audit_history/2026-09-11-audit-one-gpt-6-astra.md';
selectMemoryDocument(historical);
assert.equal(memoryTabs[4].tabIndex, 0);
assert.equal(requested.at(-1), historical);
const historyLinks = element();
appendMemoryText(historyLinks, '[Previous](' + historical + ') [Peer](other.md) [Current](../' + report + ')');
const historyButtons = historyLinks.children.filter(child => child.className === 'memory-link');
assert.equal(historyButtons.length, 3);
historyButtons[0].onclick(); assert.equal(requested.at(-1), historical);
historyButtons[1].onclick(); assert.equal(requested.at(-1), 'audit_history/other.md');
historyButtons[2].onclick(); assert.equal(requested.at(-1), report);
assert.equal(memoryTabs[3].tabIndex, 0);
// A batch rotation follows the currently displayed report into the history tab.
ui.memoryPanel.open = true;
const archived = 'audit_history/2026-09-12-audit-one-gpt-6-astra.md';
state.trace.push({kind: 'research_audit', status: 'archived', archivedPaths: {[report]: archived}});
const requestVersion = memoryRequest;
followArchivedMemoryFile();
assert.equal(requested.at(-1), archived);
assert.equal(memoryFile, archived);
assert.equal(memoryTabs[4].tabIndex, 0);
assert.ok(memoryRequest > requestVersion); // Invalidates an in-flight read of the former path.
// Existing report buttons and Markdown links resolve from the retained trace too.
buttons[0].onclick(); assert.equal(requested.at(-1), archived);
selectMemoryDocument(report, 'recommendations');
assert.equal(memoryFile, archived);
assert.equal(memoryAnchor, 'recommendations');
// The Current audits alias follows the concrete report already shown on screen.
memoryFile = 'AUDITS'; ui.memoryFilename.textContent = report;
followArchivedMemoryFile(); assert.equal(memoryFile, archived);
assert.equal(resolveArchivedMemoryFile(files[1]), files[1]);
assert.equal(resolveArchivedMemoryFile(report, [{kind:'research_audit', status:'archived',
  archivedPaths:{[report]:'audit_history/../../secret.md'}}]), report);
`;
vm.runInNewContext(setup + linkFormatter + selected + checks, {assert});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(Path(__file__).resolve().parents[1] / "ui" / "app.js")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_controls_preserve_edits_across_polling_and_apply(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const controls = source.slice(source.indexOf('function managesResearchFiles('), source.indexOf('function selectedAuthorPrompt('))
  + source.slice(source.indexOf('function researchAuditValues('),
  source.indexOf('// Merge incremental polling state'));
const setup = `
function element(value = '', tagName = '') {
  return {value, tagName, dataset: {}, children: [], replacements: 0,
    append(...children) { this.children.push(...children); },
    replaceChildren(...options) { this.options = this.children = options; this.replacements++; },
    querySelectorAll(selector) {
      return this.children.flatMap(child => [
        ...((selector === 'details[open]' && child.tagName === 'details' && child.open)
          || (selector === '[data-audit-time]' && child.dataset.auditTime) ? [child] : []),
        ...child.querySelectorAll(selector),
      ]);
    }, focus() {}};
}
const ui = {
  fileManagement: {checked: true},
  researchAuditInterval: element(), researchAuditModels: Array.from({length: 3}, () => element()),
  liveResearchAuditInterval: element(), liveResearchAuditModels: Array.from({length: 3}, () => element()),
  researchAuditsControl: element(), applyResearchAudits: element(),
  researchAuditSummary: element(), researchAuditStatus: element(), researchAuditCountdown: element(),
  researchAuditToolbar: element(), researchAuditCountdownLabel: element(),
  researchAuditTimerState: element(), startResearchAudit: element(),
  researchAuditActivity: element(), memoryPanel: element(),
};
let state = {
  runId: 'first', fileManagement: true,
  researchAudits: {intervalHours: 2, models: ['none', 'none', 'none']},
  researchAuditStatus: {runningSlots: [], lastError: ''},
  workflow: {settings: {research_audits: {choices: [
    {value: 'none', label: 'None'}, {value: 'opus', label: 'Opus'},
    {value: 'gpt-6-astra', label: 'Astra'}]}}},
};
let currentJob = 'first', researchAuditJob = null;
let researchAuditDirty = false, researchAuditSaving = false, researchAuditStarting = false, researchAuditFeedback = '';
const document = {createElement: tag => element('', tag)};
const clockText = value => value || '', elapsedText = () => '1s';
let selectedReport;
const selectMemoryDocument = path => { selectedReport = path; };
const show = (e, visible) => {e.hidden = !visible;};
const pending = [];
const request = (path, body) => new Promise((resolve, reject) => pending.push({path, body, resolve, reject}));
const jobPath = path => path + '?job=' + currentJob;
const render = next => { state = next; renderResearchAudits(true); };
`;
const checks = `
(async () => {
  function assertCountdown(value, label = 'Next audit', timerState = 'author time') {
    assert.equal(ui.researchAuditCountdown.textContent, value);
    assert.equal(ui.researchAuditCountdownLabel.textContent, label);
    assert.equal(ui.researchAuditTimerState.textContent, timerState);
  }
  fillResearchAuditControls();
  renderResearchAudits(true);
  renderResearchAudits(true);
  assert.equal(ui.researchAuditInterval.value, '2');
  assert.deepEqual(researchAuditValues().models, ['none', 'none', 'none']);
  assert.equal(ui.liveResearchAuditModels[0].options[1].textContent, 'Opus');
  assert.equal(ui.liveResearchAuditModels[0].replacements, 1);
  assert.equal(ui.researchAuditSummary.textContent, 'Disabled');
  assert.equal(ui.researchAuditToolbar.hidden, true);
  state.researchAuditStatus = {warnings: [], runningSlots: [], lastError: 'Snapshot unavailable'};
  renderResearchAudits(true);
  assert.equal(ui.researchAuditStatus.textContent, 'Snapshot unavailable');
  assert.equal(ui.researchAuditStatus.hidden, false);
  assert.match(ui.researchAuditStatus.className, /warning/);

  const firstWarning = {slot: 1, model: 'opus', message: 'Audit-1: Claude Opus is unavailable. Retrying at the next interval.'};
  const secondWarning = {slot: 2, model: 'gpt-6-astra', message: 'Audit-2: GPT Astra is unavailable. Retrying at the next interval.'};
  const warningMessages = firstWarning.message + '\\n' + secondWarning.message;
  state.researchAuditStatus = {warnings: [firstWarning, secondWarning], runningSlots: [3],
    lastError: warningMessages + '\\nSnapshot unavailable'};
  ui.researchAuditsControl.open = false;
  renderResearchAudits(true);
  assert.equal(ui.researchAuditStatus.hidden, false);
  assert.match(ui.researchAuditStatus.className, /warning/);
  assert.equal(ui.researchAuditStatus.textContent, 'Snapshot unavailable');
  state.researchAuditStatus.lastError = warningMessages;
  state.researchAuditStatus.runningSlots = [];
  renderResearchAudits(true);
  assert.equal(ui.researchAuditStatus.textContent, '');

  ui.liveResearchAuditInterval.value = '3.5';
  ui.liveResearchAuditModels[0].value = 'opus';
  researchAuditDirty = true;
  state.researchAudits.intervalHours = 4;
  renderResearchAudits(true);
  assert.equal(ui.liveResearchAuditInterval.value, '3.5');
  assert.equal(ui.liveResearchAuditModels[0].value, 'opus');
  assert.equal(ui.applyResearchAudits.disabled, false);
  assert.equal(ui.researchAuditStatus.textContent.includes(firstWarning.message), false);
  assert.ok(ui.researchAuditStatus.textContent.includes('Changes have not been applied.'));

  let applying = applyResearchAudits();
  let request = pending.shift();
  assert.equal(request.path, '/set-research-audits?job=first');
  assert.deepEqual(request.body, {intervalHours: 3.5, models: ['opus', 'none', 'none']});
  ui.liveResearchAuditModels[1].value = 'gpt-6-astra';
  renderResearchAudits(true);
  request.resolve({...state, researchAudits: request.body});
  await applying;
  assert.equal(ui.liveResearchAuditModels[1].value, 'gpt-6-astra');
  assert.equal(researchAuditDirty, true);
  assert.equal(ui.applyResearchAudits.disabled, false);

  applying = applyResearchAudits();
  request = pending.shift();
  request.resolve({...state, researchAudits: request.body});
  await applying;
  assert.equal(researchAuditDirty, false);
  assert.equal(ui.applyResearchAudits.disabled, true);
  assert.match(ui.researchAuditSummary.textContent, /2 selected/);

  ui.liveResearchAuditInterval.value = '0';
  researchAuditDirty = true;
  await applyResearchAudits();
  assert.equal(pending.length, 0);
  assert.match(ui.researchAuditStatus.textContent, /greater than zero/);
  renderResearchAudits(true);
  assert.match(ui.researchAuditStatus.textContent, /greater than zero/);
  assert.equal(ui.researchAuditStatus.textContent.includes(firstWarning.message), false);
  ui.liveResearchAuditInterval.value = '0.0001';
  applying = applyResearchAudits();
  request = pending.shift();
  assert.equal(request.body.intervalHours, 0.0001);
  request.reject(new Error('Connection unavailable'));
  await applying;
  assert.ok(ui.researchAuditStatus.textContent.includes('Connection unavailable'));
  renderResearchAudits(true);
  assert.ok(ui.researchAuditStatus.textContent.includes('Connection unavailable'));
  assert.equal(ui.researchAuditStatus.textContent.includes(firstWarning.message), false);
  assert.equal(ui.liveResearchAuditInterval.value, '0.0001');
  assert.equal(researchAuditDirty, true);

  state.researchAuditStatus.warnings = [secondWarning];
  state.researchAuditStatus.lastError = secondWarning.message;
  renderResearchAudits(true);
  assert.equal(ui.researchAuditStatus.textContent.includes(firstWarning.message), false);
  assert.equal(ui.researchAuditStatus.textContent.includes(secondWarning.message), false);
  state.researchAuditStatus.warnings = [];
  state.researchAuditStatus.lastError = '';
  renderResearchAudits(true);
  assert.equal(ui.researchAuditStatus.textContent.includes(secondWarning.message), false);
  assert.ok(ui.researchAuditStatus.textContent.includes('Connection unavailable'));

  researchAuditDirty = false;
  researchAuditFeedback = '';
  state.phase = 'running';
  state.stage = 'solve';
  state.activeNode = 'author';
  state.researchAudits = {intervalHours: 2, models: ['gpt-6-astra', 'none', 'none']};
  state.researchAuditProgress = {elapsedSeconds: 2700, lastStartedSeconds: 900};
  ui.researchAuditsControl.open = false;
  renderResearchAudits(true);
  assert.equal(ui.researchAuditToolbar.hidden, false);
  assertCountdown('01:30:00');
  state.researchAuditProgress.elapsedSeconds = 2761;
  renderResearchAudits(true);
  assertCountdown('01:28:59');

  state.phase = 'pausing';
  renderResearchAudits(true);
  assertCountdown('01:28:59', 'Next audit', 'paused');
  state.phase = 'paused';
  renderResearchAudits(true);
  const pausedCountdown = ui.researchAuditCountdown.textContent;
  renderResearchAudits(true);
  assert.equal(ui.researchAuditCountdown.textContent, pausedCountdown);
  assert.equal(ui.researchAuditTimerState.textContent, 'paused');
  state.phase = 'running';
  renderResearchAudits(true);
  assertCountdown('01:28:59');
  state.researchAuditProgress.elapsedSeconds = 2821;
  renderResearchAudits(true);
  assertCountdown('01:27:59');

  state.auditHoldingAuthor = true;
  state.phase = 'pausing';
  renderResearchAudits(true);
  assertCountdown('Preparing…', 'Research audits', 'author paused');
  state.phase = 'paused';
  state.researchAuditStatus.runningSlots = [1];
  state.researchAuditProgress.lastStartedSeconds = 2821;
  renderResearchAudits(true);
  assertCountdown('In progress', 'Research audits', 'author paused');
  state.auditHoldingAuthor = false;
  renderResearchAudits(true);
  assertCountdown('In progress', 'Research audits', 'author paused');
  state.researchAuditStatus.runningSlots = [];
  state.phase = 'running';
  renderResearchAudits(true);
  assertCountdown('02:00:00');
  state.researchAuditProgress.elapsedSeconds = 2842;
  renderResearchAudits(true);
  assertCountdown('01:59:39');

  ui.liveResearchAuditInterval.value = '9';
  researchAuditDirty = true;
  renderResearchAudits(true);
  assert.equal(ui.liveResearchAuditInterval.value, '9');
  assertCountdown('01:59:39');
  state.researchAudits.intervalHours = 1;
  renderResearchAudits(true);
  assertCountdown('00:59:39');
  state.researchAudits.intervalHours = 0.005;
  renderResearchAudits(true);
  assertCountdown('00:00:00');
  state.researchAudits.intervalHours = 2;
  state.researchAuditProgress = {elapsedSeconds: 100, lastStartedSeconds: 200};
  renderResearchAudits(true);
  assertCountdown('02:00:00');

  state.researchAudits.models = ['none', 'none', 'none'];
  renderResearchAudits(true);
  assert.equal(ui.researchAuditToolbar.hidden, true);
  state.researchAudits.models[0] = 'gpt-6-astra';
  state.activeNode = 'critic';
  renderResearchAudits(false);
  assert.equal(ui.researchAuditToolbar.hidden, true);
  state.activeNode = 'author';
  state.stage = 'cleanup';
  renderResearchAudits(false);
  assert.equal(ui.researchAuditToolbar.hidden, true);
  state.stage = 'repair';
  renderResearchAudits(true);
  assert.equal(ui.researchAuditToolbar.hidden, false);
  state.phase = 'done';
  renderResearchAudits(false);
  assert.equal(ui.researchAuditToolbar.hidden, true);

  currentJob = 'second';
  state.researchAudits = {intervalHours: 2, models: ['none', 'none', 'none']};
  state.researchAuditStatus = {warnings: [], runningSlots: [], lastError: ''};
  renderResearchAudits(false);
  assert.equal(ui.liveResearchAuditInterval.value, '2');
  assert.equal(ui.liveResearchAuditModels[0].value, 'none');
  assert.equal(ui.researchAuditsControl.hidden, true);
  assert.equal(researchAuditDirty, false);
  assert.equal(ui.researchAuditStatus.textContent, '');
  assert.equal(ui.researchAuditStatus.hidden, true);
  assert.equal(ui.researchAuditStatus.className.includes('warning'), false);

  const ready = {...state, phase: 'running', stage: 'solve', activeNode: 'author',
    auditHoldingAuthor: false,
    researchAudits: {intervalHours: 2, models: ['gpt-6-astra', 'none', 'none']},
    researchAuditStatus: {warnings: [], runningSlots: [], lastError: ''},
    researchAuditProgress: {elapsedSeconds: 300, lastStartedSeconds: 0}};
  render(ready);
  assert.equal(ui.startResearchAudit.disabled, false);
  assert.equal(ui.startResearchAudit.textContent, 'Start audit now');
  let starting = startResearchAudit();
  assert.equal(researchAuditStarting, true);
  assert.equal(ui.startResearchAudit.disabled, true);
  assert.equal(ui.startResearchAudit.textContent, 'Starting…');
  assert.equal(ui.applyResearchAudits.disabled, true);
  await startResearchAudit();
  assert.equal(pending.length, 1);
  request = pending.shift();
  assert.equal(request.path, '/start-research-audit?job=second');
  assert.deepEqual(request.body, {});
  request.resolve({...ready, phase: 'paused', auditHoldingAuthor: true,
    researchAuditStatus: {runningSlots: [1], warnings: [], lastError: ''},
    researchAuditProgress: {elapsedSeconds: 300, lastStartedSeconds: 300}});
  await starting;
  assert.equal(researchAuditStarting, false);
  assert.equal(state.phase, 'paused');
  assert.equal(ui.startResearchAudit.disabled, true);
  assertCountdown('In progress', 'Research audits', 'author paused');
  render({...ready, researchAuditProgress: {elapsedSeconds: 300, lastStartedSeconds: 300}});
  assertCountdown('02:00:00');
  assert.equal(ui.startResearchAudit.disabled, false);

  starting = startResearchAudit();
  request = pending.shift();
  request.reject(new Error('Connection unavailable'));
  await starting;
  assert.equal(researchAuditStarting, false);
  assert.equal(ui.startResearchAudit.disabled, false);
  assert.ok(ui.researchAuditStatus.textContent.includes('Connection unavailable'));
  starting = startResearchAudit();
  request = pending.shift();
  assert.equal(ui.researchAuditStatus.textContent.includes('Connection unavailable'), false);
  request.reject(new Error('Not found.'));
  await starting;
  assert.equal(ui.startResearchAudit.disabled, false);
  assert.equal(ui.researchAuditStatus.textContent,
    'Pause the author and restart the web UI to enable Start audit now.');
  starting = startResearchAudit();
  request = pending.shift();
  request.resolve(ready);
  await starting;
  assert.equal(ui.researchAuditStatus.textContent, '');

  for (const blocked of [
    {phase: 'pausing'}, {phase: 'done'}, {activeNode: 'critic'}, {manuallyStopped: true},
    {auditHoldingAuthor: true}, {researchAuditStatus: {runningSlots: [1]}},
    {researchAuditStatus: {runningSlots: [], batchActive: true}},
    {researchAudits: {intervalHours: 2, models: ['none', 'none', 'none']}},
  ]) {
    render({...ready, ...blocked});
    assert.equal(ui.startResearchAudit.disabled, true);
    await startResearchAudit();
    assert.equal(pending.length, 0);
  }
  render({...ready, researchAuditStatus: {runningSlots: [], batchActive: true}});
  assertCountdown('In progress', 'Research audits', 'author time');
  render({...ready, phase: 'paused', auditHoldingAuthor: false,
    researchAuditStatus: {runningSlots: [], batchActive: true}});
  assertCountdown('In progress', 'Research audits', 'author paused');
  assert.equal(ui.researchAuditToolbar.hidden, false);
  assert.equal(ui.startResearchAudit.disabled, true);
  const paused = {...ready, phase: 'paused'};
  render(paused);
  assert.equal(ui.startResearchAudit.disabled, false);
  assert.match(ui.startResearchAudit.title, /resume the author automatically/);
  starting = startResearchAudit();
  request = pending.shift();
  assert.equal(request.path, '/start-research-audit?job=second');
  request.resolve({...paused, auditHoldingAuthor: true,
    researchAuditStatus: {runningSlots: [1], batchActive: true}});
  await starting;
  assert.equal(ui.startResearchAudit.disabled, true);
  assertCountdown('In progress', 'Research audits', 'author paused');
  render(ready);
  assert.equal(ui.startResearchAudit.disabled, false);
  researchAuditDirty = true;
  renderResearchAudits(true);
  assert.equal(ui.startResearchAudit.disabled, true);
  assert.equal(ui.startResearchAudit.title, 'Apply your audit settings first.');
  await startResearchAudit();
  assert.equal(pending.length, 0);
  researchAuditDirty = false;
  researchAuditSaving = true;
  renderResearchAudits(true);
  assert.equal(ui.startResearchAudit.disabled, true);
  await startResearchAudit();
  assert.equal(pending.length, 0);
  researchAuditSaving = false;
  renderResearchAudits(true);

  starting = startResearchAudit();
  request = pending.shift();
  currentJob = 'third';
  const third = {...ready, runId: 'third', researchAuditProgress: {elapsedSeconds: 42, lastStartedSeconds: 0}};
  render(third);
  assert.equal(researchAuditStarting, false);
  assert.equal(ui.startResearchAudit.disabled, false);
  assertCountdown('01:59:18');
  request.resolve({...ready, phase: 'paused', auditHoldingAuthor: true});
  await starting;
  assert.equal(state, third);
  assert.equal(researchAuditJob, 'third');
  assert.equal(researchAuditStarting, false);
  assert.equal(ui.startResearchAudit.disabled, false);
  assertCountdown('01:59:18');

  starting = startResearchAudit();
  request = pending.shift();
  assert.equal(request.path, '/start-research-audit?job=third');
  currentJob = 'fourth';
  const fourth = {...ready, runId: 'fourth'};
  render(fourth);
  request.reject(new Error('Old job connection failure'));
  await starting;
  assert.equal(state, fourth);
  assert.equal(researchAuditStarting, false);
  assert.equal(ui.researchAuditStatus.textContent, '');
  assert.equal(ui.startResearchAudit.disabled, false);
  // Current availability belongs to each model; earlier errors remain only in its log.
  researchAuditDirty = false;
  state.phase = 'paused';
  state.auditHoldingAuthor = true;
  state.researchAudits.models = ['opus', 'gpt-6-astra', 'none'];
  state.researchAuditStatus = {runningSlots: [1], batchActive: true,
    warnings: [firstWarning, secondWarning], batchError: '', lastError: warningMessages};
  const event = (slot, model, status, text, extra = {}) => ({kind: 'research_audit',
    slot, model, status, text, time: '2026-09-12T16:12:51Z', ...extra});
  state.trace = [event(1, 'opus', 'warning', firstWarning.message),
    event(1, 'opus', 'starting', 'Waiting for a model response.'),
    event(2, 'gpt-6-astra', 'warning', secondWarning.message)];
  renderResearchAudits(true);
  const card = index => ui.researchAuditActivity.children[index];
  const badge = index => card(index).children[0].children[1].textContent;
  const message = index => card(index).children[1].textContent;
  assert.equal(ui.researchAuditActivity.hidden, false);
  assert.equal(ui.researchAuditActivity.children.length, 2);
  assert.equal(badge(0), 'Connecting');
  assert.equal(message(0), 'Waiting for a model response.');
  assert.equal(badge(1), 'Unavailable');
  assert.equal(message(1), secondWarning.message);
  assert.equal(ui.researchAuditStatus.textContent, '');
  card(0).children.find(child => child.tagName === 'details').open = true;
  const replacements = ui.researchAuditActivity.replacements;
  renderResearchAudits(true);
  assert.equal(ui.researchAuditActivity.replacements, replacements);
  state.researchAuditStatus.warnings = [secondWarning];
  state.trace.push(event(1, 'opus', 'working', 'Read: PROVED.md', {recovered: true}));
  renderResearchAudits(true);
  assert.equal(badge(0), 'Working');
  assert.equal(message(0), 'Read: PROVED.md');
  assert.equal(badge(1), 'Unavailable');
  const details = card(0).children.find(child => child.tagName === 'details');
  assert.equal(details.open, true);
  assert.ok(details.children[1].children[0].textContent.includes(firstWarning.message));
  for (let i = 0; i < 40; i++) state.trace.push(event(1, 'opus', 'working', 'Reading.' + i));
  renderResearchAudits(true);
  assert.equal(card(0).children.find(child => child.tagName === 'details').children[1].children.length, 30);
  state.trace.push(event(1, 'opus', 'completed', 'Report saved.', {report: 'AUDITS/one.md'}));
  state.researchAuditStatus.runningSlots = [];
  renderResearchAudits(true);
  assert.equal(badge(0), 'Completed');
  card(0).children.find(child => child.tagName === 'button').onclick();
  assert.equal(selectedReport, 'AUDITS/one.md');
  assert.equal(ui.memoryPanel.open, true);
  state.researchAudits.models = ['gpt-6-astra', 'none', 'none'];
  renderResearchAudits(true);
  assert.equal(ui.researchAuditActivity.children.length, 1);
  assert.equal(badge(0), 'Waiting');
  assert.equal(message(0), 'Waiting for the next audit.');
})();
`;
Promise.resolve(vm.runInNewContext(setup + controls + checks, {assert}))
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(Path(__file__).resolve().parents[1] / "ui" / "app.js")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
