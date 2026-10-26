"""Regression tests for the web UI fixes from the audit, run offline (no model calls, no network)."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"

# Loads the named app.js slices (located by marker text, as the other UI tests do) into one vm
# context after `setup`, then runs `checks` as an async function body. Fails if a marker is
# missing or the checks never finish (an await on a request nobody answered).
RUNNER = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const {slices, setup, checks} = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = slices.map(([from, to, suffix = '']) => {
  const start = source.indexOf(from), end = source.indexOf(to, start + 1);
  if (start < 0 || end < 0) throw new Error('Marker not found: ' + (start < 0 ? from : to));
  return source.slice(start, end) + suffix;
}).join('\n');
class Element {
  constructor(tagName = 'div') {
    this.tagName = String(tagName).toUpperCase(); this.children = []; this.dataset = {};
    this.attributes = {}; this.style = {}; this.parent = null;
    this.hidden = false; this.disabled = false; this.checked = false; this.open = false;
    this.value = ''; this.textContent = ''; this.className = ''; this.title = '';
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0;
    this.classList = {
      add: (...names) => { this.className = [this.className, ...names].filter(Boolean).join(' '); },
      toggle: () => {},
    };
  }
  append(...items) {
    for (const item of items) {
      if (item instanceof Element) { item.remove(); item.parent = this; }
      this.children.push(item);
    }
  }
  replaceChildren(...items) {
    for (const child of this.children) if (child instanceof Element) child.parent = null;
    this.children = [];
    this.append(...items);
  }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter(child => child !== this);
    this.parent = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled = true; }
  matches(selector) {
    return selector.startsWith('.') ? this.className.split(/\s+/).includes(selector.slice(1))
      : this.tagName === selector.toUpperCase();
  }
  descendants() {
    return this.children.flatMap(child => child instanceof Element ? [child, ...child.descendants()] : []);
  }
  querySelectorAll(selector) {
    const [outer, inner] = selector.split(/\s*>\s*/);
    const found = this.descendants().filter(node => node.matches(outer));
    return inner ? found.flatMap(node => node.children.filter(child => child instanceof Element && child.matches(inner)))
      : found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parent) if (node.matches(selector)) return node;
    return null;
  }
}
let finished = false;
const context = {assert, Element, URLSearchParams, setImmediate, TextEncoder, console,
  finish: () => { finished = true; }};
process.on('exit', () => {
  if (!finished && !process.exitCode) { console.error('The checks did not finish.'); process.exitCode = 1; }
});
Promise.resolve(vm.runInNewContext(`${setup}\n${code}\n(async () => {\n${checks}\nfinish();\n})()`, context))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""

# The composer and job controls: create requests, folder runs and act().
COMPOSER = [
    ["async function startReview(", "async function startLatexOnly("],
    ["async function startLatexOnly()", "async function act("],
    ["async function act(", "// The drawer is a modal <dialog>"],
]
COMPOSER_SETUP = r"""
let currentJob = '', currentBatch = '', timer = 0, jobsTimer = 0, reviewPending = false;
let state = {phase: 'input'};
let promptValues = {review: 'Review.', author: 'Author.', final: 'Final.'};
let promptOverrides = {};
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const advanced = new Element('details');
advanced.append(ui.thinkingHours);
Object.assign(ui.thinkingHours, {value: '168', max: '168'});
ui.criticRounds.value = '2';
ui.latexInput.value = 'An existing theorem and proof.';
ui.check.textContent = 'Check statement';
const show = (element, visible) => { element.hidden = !visible; };
const validateRetrievalSettings = () => true;
const syncPrompts = () => {};
const researchAuditValues = () => ({intervalHours: 2, models: ['none', 'none', 'none']});
const selectedRetrievalSettings = () => ({retrievalMode: 'live', retrievalCutoffUtc: ''});
const jobPath = (path, values = {}, runId = currentJob) => path + (runId ? '?job=' + runId : '');
const sent = [], adopted = [], rendered = [], timers = [], questions = [];
const request = (path, body) => new Promise((resolve, reject) => sent.push({path, body, resolve, reject}));
const adoptJob = next => { adopted.push(next.runId); currentJob = next.runId; };
const render = next => { rendered.push(next); state = next; };
const refresh = () => {};
const setTimeout = (callback, delay) => timers.push({callback, delay});
const clearTimeout = () => {};
let answer = true;
const confirm = question => { questions.push(question); return answer; };
const renderJobs = () => {}, loadJobs = () => {}, renderBatchNavigation = () => {};
const batchUrl = id => '/?batch=' + id, history = {pushState() {}};
const file = (path, content) => ({name: path.split('/').at(-1), webkitRelativePath: path,
  size: content.length, text: async () => content});
const tick = () => new Promise(resolve => setImmediate(resolve));
const sentCount = async count => { for (let i = 0; i < 50 && sent.length < count; i += 1) await tick(); };
"""

# Batch and job lists: the same slice and fakes as tests/test_batch_navigation.py, plus Back.
BATCHES = [
    ["function request(", "function selectedProblemMode("],
    ["window.onpopstate =", "\nrefresh();"],
]
BATCHES_SETUP = r"""
let currentJob = '', currentBatch = '', composingProblem = false, batchCatalog = [], state = {phase: 'input'};
let requests = Promise.resolve(), previousPhase = '', timer, clock, jobsTimer, memoryRequest = 0, memoryTimer;
let memoryJob = null;
const sessionToken = 'test-token', timelineRows = new Map(), detailRows = new Map();
const location = {pathname: '/', search: ''};
const history = {urls: [], pushState(a, b, url) { this.urls.push(url); }};
const clearTimeout = () => {}, clearInterval = () => {}, setTimeout = () => 1;
const document = {createElement: tag => new Element(tag)};
const window = {open() {}};
const localDateTime = value => value;
const completedRunLabel = job => job.resultKind === 'final' ? 'Answer ready' : 'Incomplete';
const confirm = () => true, alert = () => {};
let refreshed = 0;
const refresh = async () => { refreshed += 1; };
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const sent = [];
let responder = () => ({batches: []});
const fetch = async (path, options = {}) => {
  sent.push(path);
  const reply = await responder(path);
  if (reply instanceof Error) throw reply;
  return {ok: !reply.error, json: async () => reply};
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const sentCount = async count => { for (let i = 0; i < 50 && sent.length < count; i += 1) await tick(); };
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class FrontendAuditFixTests(unittest.TestCase):
    def run_app(self, slices, setup, checks):
        payload = json.dumps({"slices": slices, "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(APP)], input=payload,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # double-submit-duplicate-jobs
    def test_double_click_on_start_creates_one_job(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
const first = startReview('Prove P.'), second = startReview('Prove P.');
assert.equal(sent.length, 1, 'the second click in the same tick sends nothing');
assert.equal(sent[0].path, '/review');
assert.equal(ui.check.disabled, true);
assert.equal(ui.runFolder.disabled, true);
assert.equal(ui.check.textContent, 'Starting…');
await startFolderBatch([file('statements/a.md', 'Prove A.')]);
assert.equal(sent.length, 1, 'no folder run starts while a job is being created');
sent[0].resolve({runId: 'job-1', phase: 'reviewing'});
await Promise.all([first, second]);
assert.deepEqual(adopted, ['job-1']);
assert.equal(ui.check.disabled, false);
assert.equal(ui.runFolder.disabled, false);
assert.equal(ui.check.textContent, 'Check statement');

// Polish LaTeX: one request, and a label the selected mode changed meanwhile is kept.
state = {phase: 'input'}; currentJob = '';
const polish = startLatexOnly(), again = startLatexOnly();
assert.equal(sent.length, 2);
assert.equal(sent[1].path, '/finalize');
ui.check.textContent = 'Polish LaTeX';
sent[1].reject(new Error('Writer unavailable.'));
await Promise.all([polish, again]);
assert.equal(ui.notice.textContent, 'Writer unavailable.');
assert.equal(ui.check.disabled, false);
assert.equal(ui.check.textContent, 'Polish LaTeX');

// While a folder run is being prepared, Check statement sends nothing either.
let release;
const slow = {name: 'a.md', webkitRelativePath: 'statements/a.md', size: 8,
  text: () => new Promise(resolve => { release = () => resolve('Prove A.'); })};
const folderRun = startFolderBatch([slow]);
assert.equal(ui.check.disabled, true);
await startReview('Prove B.');
assert.equal(sent.length, 2);
await tick(); release(); await sentCount(3);
assert.equal(sent[2].path, '/direct-batch');
sent[2].resolve({batchId: '__root__', startedJobs: ['job-2'], jobs: []});
await folderRun;
assert.equal(ui.check.disabled, false);
assert.equal(ui.runFolder.disabled, false);
""")

    # folder-run-includes-hidden-system-files
    def test_folder_run_ignores_hidden_files_and_reports_binary_ones(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
const png = '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR';
const folder = [
  file('statements/.DS_Store', '\x00\x00\x00\x01Bud1\x00'), file('statements/._a.md', '\x00\x05\x16\x07'),
  file('statements/.gitignore', '*.pdf\n'), file('statements/Thumbs.db', 'x'),
  file('statements/desktop.ini', '[.ShellClassInfo]'), file('statements/a.md', 'Prove A.'),
  file('statements/logo.png', png), file('statements/empty.md', ' \n'),
];
assert.deepEqual(folderStatementFiles(folder).map(item => item.name), ['a.md', 'empty.md', 'logo.png']);
const read = await folderStatements(folder);
assert.deepEqual(read.statements.map(item => item.name), ['a.md']);
assert.deepEqual(read.empty, ['empty.md']);
assert.deepEqual(read.binary, ['logo.png']);

const run = startFolderBatch(folder);
await sentCount(1);
assert.equal(questions.length, 1);
assert.match(questions[0], /^Start 1 parallel job\?/);
assert.match(questions[0], /\n\nSkipped 1 empty file: empty\.md\. Skipped 1 non-text file: logo\.png\.$/);
assert.deepEqual(sent[0].body.statements.map(item => item.name), ['a.md']);
sent[0].resolve({batchId: '__root__', startedJobs: ['job-1'], jobs: []});
await run;
assert.equal(ui.batchStatus.textContent,
  'Started 1 job from statements.\nSkipped 1 empty file: empty.md. Skipped 1 non-text file: logo.png.');

// A folder holding only system and binary files starts nothing and says why.
await startFolderBatch([file('photos/.DS_Store', '\x00\x00\x00\x01Bud1'), file('photos/cat.png', png)]);
assert.equal(sent.length, 1);
assert.equal(questions.length, 1);
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent, 'No statements were found directly inside photos. '
  + 'Skipped 1 non-text file: cat.png. Choose a folder that contains non-empty text files (any extension or none).');
""")

    # UIF-2: errors after a click on the sticky footer are brought into view
    def test_create_errors_scroll_the_notice_into_view(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
const start = startReview('Prove S.');
sent[0].reject(new Error('Claude CLI is not installed or is not on PATH.'));
await start;
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent, 'Claude CLI is not installed or is not on PATH.');
assert.equal(ui.notice.scrolled, true);
assert.notEqual(ui.notice.focused, true, 'an alert is announced, not focused');
ui.notice.scrolled = false;
const polish = startLatexOnly();
sent[1].reject(new Error('Add an API key for Gateway in Advanced settings → API models.'));
await polish;
assert.equal(ui.notice.scrolled, true);
ui.notice.scrolled = false;
await startFolderBatch([file('photos/cat.png', '\x89PNG\x00')]);
assert.match(ui.notice.textContent, /^No statements were found directly inside photos\./);
assert.equal(ui.notice.scrolled, true);
ui.notice.scrolled = false;
const run = startFolderBatch([file('statements/a.md', 'Prove A.')]);
await sentCount(3);
sent[2].reject(new Error('Preflight failed.'));
await run;
assert.equal(ui.notice.textContent, 'Preflight failed.');
assert.equal(ui.notice.scrolled, true);
""")

    # UIF-1: an in-page fragment is never taken for the launch token
    def test_only_a_token_shaped_fragment_replaces_the_stored_token(self):
        token = "kIGHSXKOxTxX7yRsXDFcrhJRfiuePmRUvztZ-ewWPVY"
        self.run_app([["function launchToken(", "const launchFragment"],
                      ["function followInPageLink(", "document.addEventListener(\"click\""]], r"""
const ids = new Map([['main', new Element('main')], ['retrievalPanel', new Element('section')]]);
const document = {getElementById: id => ids.get(id) || null};
""", rf"""
assert.equal(launchToken('{token}'), '{token}');
for (const fragment of ['main', 'retrievalPanel', '', 'not a token', 'x'.repeat(31)]) {{
  assert.equal(launchToken(fragment), '', fragment);
}}
// Even a long fragment is a page target when an element has that id.
ids.set('{token}', new Element());
assert.equal(launchToken('{token}'), '');

// The skip link and the web search badges move focus without changing the URL.
const target = ids.get('retrievalPanel');
let prevented = false;
const link = {{getAttribute: () => '#retrievalPanel'}};
followInPageLink({{target: {{closest: selector => selector === 'a[href^="#"]' ? link : null}},
                  preventDefault: () => {{ prevented = true; }}}});
assert.equal(prevented, true);
assert.equal(target.scrolled, true);
assert.equal(target.focused, true);
// Other clicks are left alone.
prevented = false;
followInPageLink({{target: {{closest: () => null}}, preventDefault: () => {{ prevented = true; }}}});
followInPageLink({{target: {{closest: () => ({{getAttribute: () => '#missing'}})}}, preventDefault: () => {{ prevented = true; }}}});
assert.equal(prevented, false);
""")

    # act-failure-stops-polling
    def test_rejected_job_control_keeps_live_polling(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
currentJob = 'job-5'; currentBatch = '__root__';
const stop = act('/stop');
assert.equal(sent[0].path, '/stop?job=job-5');
sent[0].reject(new Error('Codex is not working.'));
await stop;
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent, 'Codex is not working.');
assert.equal(timers.length, 1);
assert.equal(timers[0].callback, refresh);
assert.equal(timers[0].delay, 700);

// After leaving the job, the new view polls on its own.
const pause = act('/pause');
currentJob = 'job-6';
sent[1].reject(new Error('Not running.'));
await pause;
assert.equal(timers.length, 1);

// A successful control renders its reply; render schedules the next poll itself.
currentJob = 'job-5';
const limit = act('/set-author-time-limit', {hours: 4});
sent[2].resolve({runId: 'job-5', phase: 'running'});
await limit;
assert.equal(rendered.at(-1).runId, 'job-5');
assert.equal(timers.length, 1);

// A failed Retry keeps its review page polling; the composer never polls.
state = {phase: 'reviewed', runId: 'job-7'}; currentJob = 'job-7';
const retry = startReview('Proposed statement.', 'Keep the original bound.');
sent[3].reject(new Error('Codex is busy.'));
await retry;
assert.equal(ui.notice.textContent, 'Codex is busy.');
assert.equal(timers.length, 2);
assert.equal(timers[1].callback, refresh);
state = {phase: 'input'}; currentJob = '';
const start = startReview('Prove S.');
sent[4].reject(new Error('Preflight failed.'));
await start;
assert.equal(timers.length, 2);
""")

    # review-retry-uses-stale-composer-mode
    def test_retry_with_feedback_keeps_the_reviewed_jobs_workflow(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
// Choices left in the hidden composer from an earlier job.
ui.statementReviewOnly.checked = true; ui.skipStatementReview.checked = true;
state = {phase: 'reviewed', runId: 'job-2'}; currentJob = 'job-2';
const retry = startReview('Proposed statement.', '');
assert.equal(sent.length, 1);
assert.equal(sent[0].path, '/review?job=job-2');
assert.equal(sent[0].body.statementReviewOnly, false);
sent[0].resolve({runId: 'job-2', phase: 'reviewing'});
await retry;

// On the composer the same toggles still choose the workflow.
state = {phase: 'input'}; currentJob = '';
ui.skipStatementReview.checked = false;
const reviewOnly = startReview('Prove Q.');
assert.equal(sent[1].path, '/review');
assert.equal(sent[1].body.statementReviewOnly, true);
sent[1].resolve({runId: 'job-3', phase: 'reviewing'});
await reviewOnly;
state = {phase: 'input'}; currentJob = '';
ui.statementReviewOnly.checked = false; ui.skipStatementReview.checked = true;
const direct = startReview('Prove R.');
assert.equal(sent[2].path, '/direct');
sent[2].resolve({runId: 'job-4', phase: 'running'});
await direct;
""")

    # blank-hours-become-unlimited (new jobs)
    def test_blank_workflow_hours_are_refused_instead_of_meaning_unlimited(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
ui.thinkingHours.value = '';
await startReview('Prove T.');
assert.equal(sent.length, 0);
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent, 'Enter a time limit: 0 for unlimited, or at most 168 hours.');
assert.equal(advanced.open, true);
assert.equal(ui.thinkingHours.focused, true);
assert.equal(ui.check.disabled, false);
let read = 0;
await startFolderBatch([{...file('statements/a.md', 'Prove A.'), text: async () => { read += 1; return 'Prove A.'; }}]);
assert.equal(read, 0);
assert.equal(sent.length, 0);

// Statement review only never uses the hidden field.
ui.statementReviewOnly.checked = true;
const reviewOnly = startReview('Prove U.');
assert.equal(sent.length, 1);
sent[0].resolve({runId: 'job-1', phase: 'reviewing'});
await reviewOnly;

// An explicit 0 still means unlimited.
state = {phase: 'input'}; currentJob = '';
ui.statementReviewOnly.checked = false; ui.thinkingHours.value = '0';
const unlimited = startReview('Prove V.');
assert.equal(sent[1].body.thinkingHours, 0);
sent[1].resolve({runId: 'job-2', phase: 'reviewing'});
await unlimited;
""")

    # blank-hours-become-unlimited (Set limit on a running job)
    def test_blank_set_limit_is_refused_instead_of_removing_the_time_limit(self):
        self.run_app([["ui.setAuthorTimeLimit.onclick =", "ui.sendAuthorSteer.onclick ="]], r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
ui.authorLimitHours.max = '168';
const show = (element, visible) => { element.hidden = !visible; };
const acted = [];
const act = (path, body) => acted.push({path, body});
""", r"""
for (const value of ['', '   ']) {
  ui.authorLimitHours.value = value; ui.authorLimitHours.focused = false;
  ui.setAuthorTimeLimit.onclick();
  assert.equal(acted.length, 0, JSON.stringify(value));
  assert.equal(ui.notice.textContent, 'Set 0 for unlimited, or at most 168 hours.');
  assert.equal(ui.notice.hidden, false);
  assert.equal(ui.authorLimitHours.focused, true);
}
ui.authorLimitHours.value = '0'; ui.setAuthorTimeLimit.onclick();
ui.authorLimitHours.value = '2.5'; ui.setAuthorTimeLimit.onclick();
assert.deepEqual(acted, [{path: '/set-author-time-limit', body: {hours: 0}},
  {path: '/set-author-time-limit', body: {hours: 2.5}}]);
""")

    # timeline-evicts-newest-rows
    def test_timeline_keeps_new_rows_once_300_rows_are_pinned(self):
        self.run_app([["function upsertTimeline(", "// Keep only exact application prompts"]], r"""
const ui = {timeline: new Element('ol')};
const timelineRows = new Map();
let activeFilter = 'all';
const document = {createElement: tag => new Element(tag)};
const appendFormattedText = (element, value) => { element.textContent = value; };
const clockText = () => '', cardIcon = () => '·';
""", r"""
for (let index = 0; index < 300; index += 1) {
  upsertTimeline({key: 'prompt:' + index, type: 'prompt', label: 'Prompt sent', text: 'Exact prompt', pinned: true});
}
upsertTimeline({key: 'reasoning:r1', type: 'reasoning', label: 'Reasoning summary', text: 'Checking'});
assert.equal(ui.timeline.children.length, 301);
assert.equal(ui.timeline.children.at(-1).dataset.key, 'reasoning:r1');
const row = timelineRows.get('reasoning:r1');
upsertTimeline({key: 'reasoning:r1', type: 'reasoning', label: 'Reasoning summary', text: ' the bound', append: true});
assert.equal(timelineRows.get('reasoning:r1'), row, 'a streamed delta updates its row, not a new one');
assert.equal(row._card.text, 'Checking the bound');
assert.equal(ui.timeline.children.at(-1), row);

// Routine rows are bounded at 300, oldest first; every pinned row stays.
for (let index = 0; index < 320; index += 1) {
  upsertTimeline({key: 'tool:' + index, type: 'tool', label: 'Tool activity', text: 'command · completed'});
}
const keys = ui.timeline.children.map(item => item.dataset.key);
assert.equal(keys.filter(key => key.startsWith('prompt:')).length, 300);
const routine = keys.filter(key => !key.startsWith('prompt:'));
assert.equal(routine.length, 300);
assert.equal(routine[0], 'tool:20');
assert.equal(routine.at(-1), 'tool:319');
assert.equal(timelineRows.has('reasoning:r1'), false);
assert.equal(timelineRows.has('tool:19'), false);
assert.equal(timelineRows.has('tool:20'), true);
""")

    # stale-list-error-banner
    def test_list_errors_clear_when_polling_recovers_and_on_back(self):
        self.run_app(BATCHES, BATCHES_SETUP, r"""
const working = path => path === '/batches' ? {batches: [{id: 'v4', label: 'Batch four', current: true}]}
  : {jobs: [{runId: 'r1', title: 'P1', phase: 'running'}]};
currentBatch = 'v4';
responder = path => path === '/batches' ? new Error('Failed to fetch') : working(path);
await loadJobs();
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent, 'Failed to fetch');
responder = working;
await loadJobs();
assert.equal(ui.notice.hidden, true, 'a recovered poll clears its own error');
assert.equal(ui.jobsList.children.length, 1);

// A gateway error reply is a poll error too.
responder = path => path === '/batches' ? {error: 'The local controller is temporarily unavailable.'} : working(path);
await loadJobs();
assert.equal(ui.notice.textContent, 'The local controller is temporarily unavailable.');
responder = working;
await loadJobs();
assert.equal(ui.notice.hidden, true);

// An action's error survives the next successful poll.
ui.notice.textContent = 'Stop this job before deleting it.'; show(ui.notice, true);
await loadJobs();
assert.equal(ui.notice.hidden, false);

// Browser Back never carries a job's error onto the list.
ui.notice.textContent = 'Provider error from the job page.'; show(ui.notice, true);
location.search = '?batch=v4';
window.onpopstate();
assert.equal(ui.notice.hidden, true);
assert.equal(currentBatch, 'v4');
assert.equal(refreshed, 1);
""")

    # control-action-rebuilds-timeline
    def test_control_reply_extends_the_timeline_instead_of_rebuilding_it(self):
        # The head of render() up to its first phase-dependent line, as test_critic_review_cycles uses it.
        slices = [["function render(next) {", "  const phase = state.phase;", "\n}"]]
        self.run_app(slices, r"""
const entry = index => ({kind: 'codex_event', time: 't' + index, index});
let state = {runId: 'r', batchId: '__root__', phase: 'running', traceVersion: 10,
  trace: Array.from({length: 10}, (_, index) => entry(index))};
const ingested = [];
const retainTrace = entries => entries;
const ingest = (entries, reset) => ingested.push({entries: entries.map(item => item.index), reset});
const upsertTimeline = () => {}, describe = () => null, criticReviewModel = () => ({rounds: []});
// A POST reply: pinned history plus the server's recent window [4, 12).
const reply = (overrides = {}) => ({runId: 'r', batchId: '__root__', phase: 'running', traceFrom: 4, traceVersion: 12,
  trace: [{kind: 'request', index: 'pinned'}, ...Array.from({length: 8}, (_, index) => entry(index + 4))], ...overrides});
""", r"""
render(reply());
assert.deepEqual(ingested.at(-1), {entries: [10, 11], reset: false});
assert.equal(state.traceVersion, 12);
assert.deepEqual(state.trace.map(item => item.index), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
render(reply());
assert.deepEqual(ingested.at(-1), {entries: [], reset: false}, 'nothing new is nothing to redraw');
assert.equal(state.trace.length, 12);
// An ordinary poll still appends.
render(reply({traceFrom: 12, traceVersion: 13, trace: [entry(12)]}));
assert.deepEqual(ingested.at(-1), {entries: [12], reset: false});

// Another batch, another run, a cleared trace or a view behind the window still rebuild.
render(reply({batchId: 'saved', traceFrom: 4, traceVersion: 13}));
assert.equal(ingested.at(-1).reset, true);
render(reply({runId: 'other', traceFrom: 0, traceVersion: 3, trace: [entry(0), entry(1), entry(2)]}));
assert.deepEqual(ingested.at(-1), {entries: [0, 1, 2], reset: true});
render(reply({runId: 'other', traceFrom: 0, traceVersion: 1, trace: [entry(0)]}));
assert.equal(ingested.at(-1).reset, true);
render(reply({runId: 'other', traceFrom: 5, traceVersion: 9, trace: [5, 6, 7, 8].map(entry)}));
assert.deepEqual(ingested.at(-1), {entries: [5, 6, 7, 8], reset: true});
""")

    # latest-activity-shows-delta-fragment
    def test_latest_activity_shows_the_streamed_text_so_far(self):
        slices = [
            ["function retrievalConfiguration(", "function renderJobs("],
            ["function describe(entry)", "function cardIcon("],
            ["function completedRunLabel(", "function renderWorkflow()"],
        ]
        self.run_app(slices, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let stateReceivedAt = Date.now();
const document = {createElement: tag => new Element(tag)};
const show = (element, visible) => { element.hidden = !visible; };
const clockText = () => '12:00:00';
const timelineRows = new Map();
const event = (method, params) => ({kind: 'codex_event', time: 't', event: {method, params}});
let state = {runId: 'r', phase: 'running', stage: 'solve', activeNode: 'author', statement: 'S', draft: 'S',
  reasoningSummary: 'concise', workflow: {nodes: {author: {label: 'Proof author'}}}, trace: []};
""", r"""
renderRunOverview();
assert.equal(ui.runLatestAction.textContent, 'Public activity appears below as the model works.');
state.trace = [event('item/started', {item: {id: 'r1', type: 'reasoning'}}),
  event('item/reasoning/summaryTextDelta', {itemId: 'r1', delta: '**Checking** the'}),
  event('item/reasoning/summaryTextDelta', {itemId: 'r1', delta: ' bound'})];
timelineRows.set('reasoning:r1', {_card: {text: '**Checking** the bound'}});
renderRunOverview();
assert.equal(ui.runLatestAction.textContent, 'Latest activity · 12:00:00 · Reasoning summary: Checking the bound');
// Without its timeline row the fragment is still shown rather than nothing.
timelineRows.clear();
renderRunOverview();
assert.match(ui.runLatestAction.textContent, /Reasoning summary: +bound$/);
// A completed item carries its own full text.
state.trace.push(event('item/completed', {item: {id: 'a1', type: 'agentMessage', text: 'Final **answer** text.'}}));
renderRunOverview();
assert.equal(ui.runLatestAction.textContent, 'Latest activity · 12:00:00 · Author: Final answer text.');
""")

    # rail-critic-history-scroll-reset
    def test_critic_history_keeps_its_scroll_position_across_polls(self):
        slices = [
            ["// Group the recorded critic calls", "function renderCriticReviewHistory()"],
            ["function renderWorkflow()", "function renderClock()"],
        ]
        self.run_app(slices, r"""
const ui = {workflowNodes: new Element('ol')};
const document = {createElement: tag => new Element(tag)};
const nodeFromStage = () => '';
const node = label => ({label, description: label});
const critic = (time, bugs) => [
  {kind: 'request', stage: 'critic', node: 'critic', time: time + ':request', text: 'Review.'},
  {kind: 'critic_result', stage: 'critic', time, round: 1, report: {verdict: 'reject', bugs}},
  {kind: 'request', stage: 'repair', node: 'author', time: time + ':repair'},
];
const state = {runId: 'r', phase: 'running', stage: 'critic', activeNode: 'critic', skipStatementReview: true,
  criticRounds: 2, traceVersion: 9,
  workflow: {nodes: {author: node('Proof author'), critic: node('Independent critic'),
    latex_editor: node('LaTeX editor'), latex_compile: node('Compile PDF'), failure_summary: node('Saved progress')}},
  trace: [...critic('t1', 'Gap one.'), ...critic('t2', 'Gap two.'), ...critic('t3', 'Gap three.')]};
""", r"""
renderWorkflow();
const list = ui.workflowNodes.querySelector('.critic-history > ol');
assert(list, 'the critic node lists its recorded cycles');
assert.equal(list.children.length, 3);
ui.workflowNodes.querySelector('.critic-history').open = true;
list.scrollTop = 120;
renderWorkflow();
const rebuilt = ui.workflowNodes.querySelector('.critic-history > ol');
assert.notEqual(rebuilt, list);
assert.equal(ui.workflowNodes.querySelector('.critic-history').open, true);
assert.equal(rebuilt.scrollTop, 120);
// A fresh rail (another job) starts at the top.
ui.workflowNodes.replaceChildren();
renderWorkflow();
assert.equal(ui.workflowNodes.querySelector('.critic-history > ol').scrollTop, 0);
""")

    # stale-live-activity-subtitle
    def test_live_activity_subtitle_resets_for_a_job_without_activity(self):
        self.run_app([["function renderClock()", "function researchAuditValues("]], r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let state = {phase: 'running', startedAt: 's', processAlive: true, traceVersion: 523,
  lastActivityAt: new Date(Date.now() - 60000).toISOString()};
const durationText = seconds => Math.round(seconds) + 's', runElapsedSeconds = () => 5;
const renderRunOverview = () => {};
""", r"""
renderClock();
assert.match(ui.lastActivity.textContent, /^Local process running · last public event .* · 523 events recorded$/);
state = {phase: 'prepared', traceVersion: 0};
renderClock();
assert.equal(ui.lastActivity.textContent, 'Waiting for the first update…');
""")

    # delete-job-during-paused-audit (job list part; the server must send auditHoldingAuthor)
    def test_job_paused_for_research_audits_offers_no_delete_or_resume(self):
        self.run_app(BATCHES, BATCHES_SETUP, r"""
currentBatch = '__root__'; batchCatalog = [{id: '__root__', label: 'Runs', current: true}];
const paused = {runId: 'r1', title: 'P1', phase: 'paused', canContinueStopped: true, continueStoppedLabel: 'Resume'};
const buttons = () => ui.jobsList.children[0].children[1].children;
const button = label => buttons().find(item => item.textContent === label);
renderJobs([{...paused, auditHoldingAuthor: true}]);
assert.equal(ui.jobsList.children[0].children[0].children[1].textContent, 'Paused · auditing');
assert.equal(button('Delete').hidden, true);
assert.equal(button('Resume').disabled, true);
renderJobs([paused]);
assert.equal(ui.jobsList.children[0].children[0].children[1].textContent, 'Paused');
assert.equal(button('Delete').hidden, false);
assert.equal(button('Resume').disabled, false);
""")

    # trash-listed-as-batch (list part; the catalog must also hide runs/.trash)
    def test_deleting_the_last_problem_returns_to_all_batches(self):
        self.run_app(BATCHES, BATCHES_SETUP, r"""
const root = {id: '__root__', label: 'Runs', current: true};
currentBatch = '__root__'; batchCatalog = [root];
ui.notice.hidden = true;
renderJobs([{runId: 'r1', title: 'P1', phase: 'done'}, {runId: 'r2', title: 'P2', phase: 'done'}]);
responder = path => path.startsWith('/delete-job') ? {deleted: 'r1'}
  : path === '/batches' ? {batches: [root]} : {jobs: [{runId: 'r2', title: 'P2', phase: 'done'}]};
await deleteJob({runId: 'r1'}, 'P1');
await sentCount(3); await tick(); // the list reloads in the background
assert.deepEqual(sent, ['/delete-job?job=r1&batch=__root__', '/batches', '/jobs?batch=__root__']);
assert.equal(currentBatch, '__root__');
assert.equal(ui.jobsList.children.length, 1);

// The last problem takes the batch with it: show All batches, not a missing-batch error.
sent.length = 0;
responder = path => path.startsWith('/delete-job') ? {deleted: 'r2'} : {batches: []};
await deleteJob({runId: 'r2'}, 'P2');
assert.deepEqual(sent, ['/delete-job?job=r2&batch=__root__', '/batches']);
assert.equal(currentBatch, '');
assert.equal(history.urls.at(-1), '/');
assert.equal(ui.notice.hidden, true);
assert.equal(ui.batchesPanel.hidden, false);
assert.equal(ui.jobsPanel.hidden, true);
""")

    # exec-collab-tool-call-dropped (live timeline part)
    def test_critic_subagent_calls_from_codex_exec_appear_as_tool_activity(self):
        self.run_app([["function describe(entry)", "function cardIcon("]], r"""
const state = {};
const collab = (completed, tool = 'spawn_agent') => ({kind: 'codex_event', stage: 'critic', time: 't', activityLabel: '',
  event: {type: completed ? 'item.completed' : 'item.started', item: {id: 'item_0', type: 'collab_tool_call', tool,
    prompt: 'Audit Lemma 2', status: completed ? 'completed' : 'in_progress'}}});
""", r"""
const started = describe(collab(false));
assert.equal(started.type, 'tool');
assert.equal(started.key, 'tool:item_0');
assert.equal(started.text, 'spawn_agent · in_progress');
const completed = describe(collab(true));
assert.equal(completed.key, started.key);
assert.equal(completed.text, 'spawn_agent · completed');
assert.equal(describe(collab(true, 'wait')).text, 'wait · completed');
""")

    # folder-batch-status-never-visible
    def test_folder_run_result_is_shown_where_the_run_started_and_cleared_on_navigation(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
// A failed statement review shows the composer on its own job page.
currentJob = 'failed-review';
const run = startFolderBatch([file('statements/a.md', 'Prove A.'), file('statements/b.md', 'Prove B.')]);
await sentCount(1);
sent[0].resolve({batchId: '__root__', startedJobs: ['job-1', 'job-2'], jobs: []});
await run;
assert.equal(ui.batchStatus.hidden, false);
assert.equal(ui.batchStatus.textContent, 'Started 2 jobs from statements.');
assert.equal(ui.batchStatus.scrolled, true);
assert.equal(currentJob, 'failed-review');
""")
        self.run_app(BATCHES, BATCHES_SETUP, r"""
for (const navigate of [() => selectJob('r1'), () => newProblem(), () => window.onpopstate()]) {
  show(ui.batchStatus, true);
  await navigate();
  assert.equal(ui.batchStatus.hidden, true);
}
""")

    # job-list-rebuilt-every-poll (fixed by the redesign; guards Back to problems)
    def test_back_to_problems_rebuilds_a_list_that_polling_keeps(self):
        self.run_app(BATCHES, BATCHES_SETUP, r"""
const rows = [{runId: 'r1', title: 'P1', phase: 'paused'}];
batchCatalog = [{id: 'v4', label: 'Batch four', current: true}];
currentBatch = 'v4';
responder = path => path === '/batches' ? {batches: batchCatalog} : {jobs: rows};
await loadJobs();
const card = ui.jobsList.children[0];
await loadJobs();
assert.equal(ui.jobsList.children[0], card, 'an unchanged poll keeps the same cards');
await goHome();
assert.equal(ui.jobsList.children.length, 1, 'Back to problems refills the list it emptied');
assert.notEqual(ui.jobsList.children[0], card);
assert.equal(ui.jobsCount.textContent, '1 problem · 1 paused');
""")


class FolderStatusMarkupTests(unittest.TestCase):
    # folder-batch-status-never-visible: the status must not sit inside a panel the app hides.
    def test_batch_status_is_outside_every_view_panel(self):
        class Ancestry(HTMLParser):
            VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
                    "track", "wbr"}

            def __init__(self):
                super().__init__()
                self.stack, self.found = [], None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("id") == "batchStatus":
                    self.found = list(self.stack)
                if tag not in self.VOID:
                    self.stack.append((tag, attrs.get("id"), "hidden" in attrs))

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, -1, -1):
                    if self.stack[index][0] == tag:
                        del self.stack[index:]
                        break

        page = Ancestry()
        page.feed((ROOT / "ui" / "index.html").read_text(encoding="utf-8"))
        self.assertIsNotNone(page.found)
        self.assertIn(("main", "main", False), page.found)
        self.assertEqual([item for item in page.found if item[2]], [])
        panels = {"inputPanel", "jobsPanel", "batchesPanel", "runOverview", "reviewPanel", "runPanel"}
        self.assertEqual(panels & {item[1] for item in page.found}, set())



class StickyFooterTests(unittest.TestCase):
    """UIF-4: keyboard focus scrolls fields clear of the composer footer's real height."""

    def test_scroll_padding_follows_the_measured_footer_height(self):
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        rule = next(line for line in css.splitlines() if "html:has(#inputPanel:not([hidden]))" in line)
        self.assertIn("scroll-padding-bottom: calc(var(--composer-footer-h", rule)
        self.assertNotRegex(rule, r"scroll-padding-bottom:\s*\d+px")
        observer = app[app.index('document.querySelector(".composer-actions")'):]
        self.assertIn("new ResizeObserver(", observer[:600])
        self.assertIn('"--composer-footer-h"', observer[:600])


if __name__ == "__main__":
    unittest.main()
