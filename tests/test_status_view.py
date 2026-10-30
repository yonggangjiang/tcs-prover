"""Clearer status: one outcome classification for every list and the job page, the outcome wording per
selected stages, the full workflow rail with its position mark, the activity age, the author time limit
and its history, the time-limit summary in the conversation, and the Download .md label.

The browser checks cut ui/app.js into slices by marker text and run them in Node's vm with a small fake
DOM, as the other UI tests do. The server checks use App and the saved-batch catalog directly. Popen,
goal_session and both author_conversation functions are stubbed: no CLI or model can start.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli
from ui import batches, server

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"

RUNNER = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const {slices, setup, checks} = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = slices.map(([from, to]) => {
  const start = source.indexOf(from), end = source.indexOf(to, start + 1);
  if (start < 0 || end < 0) throw new Error('Marker not found: ' + (start < 0 ? from : to));
  return source.slice(start, end);
}).join('\n');
const dom = String.raw`
class Element {
  constructor(tagName = 'div') {
    this.tagName = String(tagName).toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
    this.parent = null; this.hidden = false; this.disabled = false; this.open = false; this.value = '';
    this.textContent = ''; this.className = ''; this.title = ''; this.scrollTop = 0; this.scrollHeight = 0;
    this.top = 0;
    this.classList = {
      add: (...names) => { this.className = [this.className, ...names].filter(Boolean).join(' '); },
      toggle: () => {},
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
  }
  append(...items) {
    for (const item of items) {
      if (item instanceof Element) { item.remove(); item.parent = this; }
      this.children.push(item);
    }
    this.scrollHeight = this.children.length * 100;
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
  getAttribute(name) { return this.attributes[name] ?? null; }
  getBoundingClientRect() { return {top: this.top}; }
  focus() { this.focused = true; }
  matches(selector) {
    return selector.startsWith('.') ? this.className.split(/\s+/).includes(selector.slice(1))
      : this.tagName === selector.toUpperCase();
  }
  descendants() {
    return this.children.flatMap(child => child instanceof Element ? [child, ...child.descendants()] : []);
  }
  querySelectorAll(selector) { return this.descendants().filter(node => node.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const text = node => [node.textContent, ...node.children.map(text)].join(' ').replace(/\s+/g, ' ').trim();
const stages = (review, author, critic, writing) => ({review, author, critic, writing});`;
let finished = false;
const context = {assert, URLSearchParams, setImmediate, console, finish: () => { finished = true; }};
process.on('exit', () => {
  if (!finished && !process.exitCode) { console.error('The checks did not finish.'); process.exitCode = 1; }
});
Promise.resolve(vm.runInNewContext(`${dom}\n${setup}\n${code}\n(async () => {\n${checks}\nfinish();\n})()`, context))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""

OUTCOME = [["function completedRunLabel(", "function localDateTime("]]
LISTS = [["function request(", "function selectedProblemMode("], *OUTCOME]
LISTS_SETUP = r"""
let currentJob = '', currentBatch = '__root__', composingProblem = false, state = {phase: 'input'};
let batchCatalog = [{id: '__root__', label: 'Runs', current: true}];
let requests = Promise.resolve(), previousPhase = '', timer, clock, jobsTimer;
const sessionToken = 'token', timelineRows = new Map(), detailRows = new Map();
const location = {pathname: '/'}, history = {pushState() {}};
const clearTimeout = () => {}, clearInterval = () => {}, setTimeout = () => 1;
const document = {createElement: tag => new Element(tag)};
const window = {open() {}};
const localDateTime = value => value;
const confirm = () => true, alert = () => {};
const refresh = async () => {};
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
"""
OVERVIEW = [["function retrievalConfiguration(", "function renderJobs("],
            ["function completedRunLabel(", "function renderWorkflow()"]]
OVERVIEW_SETUP = r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let state = {}, stateReceivedAt = Date.now();
const document = {createElement: tag => new Element(tag), title: ''};
const show = (element, visible) => { element.hidden = !visible; };
// The local clock time of an ISO time, as the page writes it.
const clockText = value => value ? new Date(value).toISOString().slice(11, 19) : '';
const describe = () => null;
const job = (fields) => ({runId: 'r', phase: 'done', statement: 'S', draft: 'S', trace: [], thinkingHours: 0,
  criticRounds: 2, criticCount: 3, workflow: {nodes: {}}, output: 'Proof', ...fields});
const row = label => ui.runMetadata.children.find(item => item.children[0].textContent === label)?.children[1].textContent;
"""
RENDER = [*OUTCOME,
          ["function runElapsedSeconds()", "function unconfiguredApiModel("],
          ["function render(next)", "async function refresh()"],
          ["function composerRelatedWorks()", "// Read files directly"]]
RENDER_SETUP = r"""
let state = {}, previousPhase = '', currentJob = 'job', currentBatch = '__root__', reviewPending = false;
let timer, clock, stateReceivedAt = Date.now();
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const document = {activeElement: null, body: new Element('body'), createElement: tag => new Element(tag)};
const show = (element, visible) => { element.hidden = !visible; };
const retainTrace = entries => entries;
const ingest = () => {}, renderWorkflow = () => {}, renderClock = () => {};
const renderBatchNavigation = () => {}, refresh = () => {};
const restoreRetrievalSettings = () => {}, syncPrompts = () => {}, updateModelSummary = () => {};
const appendFormattedText = () => {}, checkEdited = () => {}, restoreWorkflowStages = () => {};
const setTimeout = () => 1, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
const jobPath = path => path + '?job=job', sessionToken = 'secret';
const act = async () => true;
"""
# The now-card, and the words for what waits to retry (a model call, a command, a file write).
CLOCK = [["function renderClock()", "// Merge incremental polling state"],
         ["function retryWaitText(", "// A time limit as the server writes it"]]
CLOCK_SETUP = r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let state = {};
const durationText = seconds => `${Math.floor(seconds / 60)}m ${String(Math.floor(seconds % 60)).padStart(2, '0')}s`;
const runElapsedSeconds = () => 0, renderRunOverview = () => {};
"""

# Every finished outcome, by selected stages: [job fields, label, tone]. The lists count "done" under
# Complete and everything else under Incomplete.
OUTCOMES = r"""
const finishedJobs = [
  [{resultKind: 'proof', workflowStages: stages(false, true, true, false)}, 'Proof accepted', 'done'],
  [{resultKind: 'proof', workflowStages: stages(false, false, true, false)}, 'Proof accepted', 'done'],
  [{resultKind: 'proof', workflowStages: stages(true, true, false, false)}, 'Proof written (not reviewed)', 'done'],
  [{resultKind: 'final', workflowStages: stages(false, true, false, true)}, 'Proof written (not reviewed)', 'done'],
  [{resultKind: 'final', workflowStages: stages(true, true, true, true)}, 'Answer ready', 'done'],
  [{resultKind: 'final', workflowStages: stages(false, false, true, true)}, 'Answer ready', 'done'],
  [{resultKind: 'final', workflowStages: stages(false, false, false, true)}, 'Answer ready', 'done'],
  [{resultKind: 'final'}, 'Answer ready', 'done'],
  [{resultKind: 'review', workflowStages: stages(true, false, false, false)}, 'Review complete', 'done'],
  [{resultKind: 'needs-revision', workflowStages: stages(false, false, true, false)}, 'Needs revision', 'failed'],
  [{resultKind: 'failure', error: 'LaTeX compilation is unavailable.'}, 'Incomplete', 'failed'],
  [{resultKind: '', error: ''}, 'Incomplete', 'failed'],
  [{interrupted: true, error: 'Interrupted: the run stopped part-way.'}, 'Interrupted', 'failed'],
  [{manuallyStopped: true, error: 'Stopped.', stoppedStage: 'critic'}, 'Stopped', 'stopped'],
].map(([fields, label, tone]) => [{phase: 'done', ...fields}, label, tone]);
const liveJobs = [
  [{phase: 'running'}, 'running'], [{phase: 'reviewing'}, 'running'], [{phase: 'pausing'}, 'running'],
  [{phase: 'stopping'}, 'running'], [{phase: 'stopping', manuallyStopped: true, stoppedStage: 'critic'}, 'running'],
  [{phase: 'paused'}, 'paused'], [{phase: 'paused', pauseCause: 'user'}, 'paused'],
  [{phase: 'paused', pauseCause: 'restart'}, 'paused'], [{phase: 'paused', pauseCause: 'time-limit'}, 'limit'],
  [{phase: 'reviewed'}, 'waiting'],
  [{phase: 'prepared'}, 'waiting'], [{phase: 'prepared', launchQueued: true}, 'queued'],
];
"""


def run_node(case, slices, setup, checks):
    payload = json.dumps({"slices": slices, "setup": setup, "checks": checks})
    result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(APP)], input=payload,
                            text=True, capture_output=True, timeout=20)
    case.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return result


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class OutcomeClassificationTests(unittest.TestCase):
    def test_every_list_and_the_job_page_share_one_classification(self):
        run_node(self, LISTS, LISTS_SETUP,
                 OUTCOMES + r"""
// The batch list: each finished job's status words and mark come from completedRunOutcome.
const jobs = [...finishedJobs.map(([job], index) => ({runId: `f${index}`, title: `F${index}`, ...job})),
  ...liveJobs.map(([job], index) => ({runId: `l${index}`, title: `L${index}`, ...job}))];
renderJobs(jobs);
const rows = ui.jobsList.children;
assert.equal(rows.length, jobs.length);
for (const [index, [job, label, tone]] of finishedJobs.entries()) {
  assert.equal(completedRunLabel(job), label, JSON.stringify(job));
  assert.equal(jobTone(job), tone, JSON.stringify(job));
  assert.match(rows[index].className, new RegExp(`tone-${tone}( |$)`), label);
  const status = rows[index].children[0].children[1];
  assert.match(status.className, new RegExp(`tone-${tone}`));
  if (!job.manuallyStopped) assert.equal(status.textContent, label);
}
for (const [index, [job, tone]] of liveJobs.entries()) {
  assert.equal(jobTone(job), tone, JSON.stringify(job));
  assert.match(rows[finishedJobs.length + index].className, new RegExp(`tone-${tone}( |$)`));
}
// A job being stopped is still running, and says so, on its page and in the list.
const stopping = rows[finishedJobs.length + liveJobs.findIndex(([job]) => job.manuallyStopped)];
assert.equal(stopping.children[0].children[1].textContent, 'Stopping');
// The proof of a run whose critic passed and whose writing was off counts under Complete.
const proof = rows[0];
assert.equal(proof.children[0].children[1].textContent, 'Proof accepted');
assert.match(proof.className, /tone-done/);
assert(!text(ui.jobsList).includes('Workflow complete'));
// The Complete filter shows the done rows, Incomplete the failed and stopped ones (index.html CSS and
// the filter read these classes).
const tones = rows.map(row => row.className.match(/tone-(\w+)/)[1]);
assert.equal(tones.filter(tone => tone === 'done').length, 9);
assert.equal(tones.filter(tone => ['failed', 'stopped'].includes(tone)).length, 5);
""")

    def test_the_job_page_status_label_and_subtitle_follow_the_selected_stages(self):
        run_node(self, OVERVIEW, OVERVIEW_SETUP, OUTCOMES + r"""
for (const [fields, label, tone] of finishedJobs) {
  state = job(fields);
  renderRunOverview();
  assert.equal(ui.runOverview.dataset.status, tone, label);
  assert.equal(ui.runStage.textContent, label);
  assert.equal(ui.runNextStep.textContent, completedRunOutcome(state).summary);
}
for (const [fields, tone] of liveJobs) {
  state = job({...fields, stage: 'solve', activeNode: 'author'});
  renderRunOverview();
  assert.equal(ui.runOverview.dataset.status, tone, JSON.stringify(fields));
}
// The run of point 2: the critic passed and writing was off.
state = job({resultKind: 'proof', workflowStages: stages(false, true, true, false)});
renderRunOverview();
assert.equal(ui.runStage.textContent, 'Proof accepted');
assert.equal(ui.runNextStep.textContent, 'The critic accepted the proof. Writing & polishing was not selected for '
  + 'this run, so no PDF was made.');
assert.equal(ui.resultHeading.textContent, 'Accepted proof');
assert.equal(ui.resultDescription.textContent, 'The complete proof, as the critic accepted it.');
// The critic off: a proof that nobody reviewed, said so.
state = job({resultKind: 'proof', workflowStages: stages(false, true, false, false)});
renderRunOverview();
assert.equal(ui.runStage.textContent, 'Proof written (not reviewed)');
assert.equal(ui.runNextStep.textContent, 'The proof author wrote a proof. The critic was not selected for this run, '
  + 'so no one reviewed it, and Writing & polishing was not selected, so no PDF was made.');
assert.equal(ui.resultHeading.textContent, 'Proof (not reviewed)');
state = job({resultKind: 'final', canDownloadTex: true, canDownloadPdf: true, workflowStages: stages(true, true, false, true)});
renderRunOverview();
assert.equal(ui.runStage.textContent, 'Proof written (not reviewed)');
assert.match(ui.runNextStep.textContent, /Writing & polishing made the LaTeX document\. The critic was not selected/);
// Statement review only: as before.
state = job({resultKind: 'review', statementReviewOnly: true, workflowStages: stages(true, false, false, false)});
renderRunOverview();
assert.equal(ui.runStage.textContent, 'Review complete');
assert.equal(ui.runNextStep.textContent, 'The statement review is saved.');
""")

    def test_the_badge_header_and_now_card_say_the_same_outcome(self):
        run_node(self, RENDER, RENDER_SETUP, r"""
render({runId: 'r', phase: 'done', resultKind: 'proof', output: 'Proof', trace: [], traceVersion: 0, traceFrom: 0,
  workflowStages: stages(false, true, true, false), workflow: {nodes: {critic: {label: 'Independent critic'}}},
  activeNode: 'critic', stage: 'critic'});
assert.equal(ui.globalStatus.textContent, 'Proof accepted');
assert.equal(ui.runTitle.textContent, 'Proof accepted');
assert.equal(ui.runLabel.textContent, 'PROOF ACCEPTED');
""")

    def test_a_finished_successful_job_offers_no_recovery_on_its_page(self):
        run_node(self, [*OUTCOME, ["async function loadJobRecovery(", "async function startReview("]], r"""
let currentJob = 'job', currentBatch = '__root__', state = {};
const ui = {jobRecovery: new Element('div'), runProblemTitle: {textContent: 'P'}};
const document = {createElement: tag => new Element(tag)};
const show = (element, visible) => { element.hidden = !visible; };
const jobPath = path => path;
const requested = [];
const request = async path => { requested.push(path); return {jobs: []}; };
""", OUTCOMES + r"""
for (const [fields, label, tone] of finishedJobs) {
  state = fields;
  ui.jobRecovery.dataset.key = '';
  await loadJobRecovery();
  // Only an unsuccessful outcome asks which saved work can continue.
  assert.equal(requested.length > 0, tone !== 'done', label);
  requested.length = 0;
}
""")


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class TimeLimitPageTests(unittest.TestCase):
    HISTORY = r"""
const history = [
  {event: 'reached', time: '2026-10-29T00:09:29+00:00', hours: 0.003, elapsedSeconds: 13.04},
  {event: 'summary', time: '2026-10-29T02:48:54+00:00'},
  {event: 'set', time: '2026-10-29T02:50:06+00:00', hours: 0},
  {event: 'resumed', time: '2026-10-29T02:50:06+00:00', hours: 0},
  {event: 'set', time: '2026-10-29T03:00:00+00:00', hours: 1.5},
];
"""

    def test_the_limit_and_its_history_show_on_the_page_also_after_the_job_finished(self):
        run_node(self, OVERVIEW, OVERVIEW_SETUP, self.HISTORY + r"""
assert.equal(limitSettingText(0), 'No time limit');
assert.equal(limitSettingText(0.003), '0.003 hours (11 s)');
assert.equal(limitSettingText(2), '2 hours');
assert.equal(limitSettingText(0.25), '0.25 hours (15 min)');
state = job({resultKind: 'final', canDownloadTex: true, thinkingHours: 0, startedAt: '2026-10-29T00:05:07+00:00',
  workflowStages: stages(false, true, true, true), timeLimitHistory: history});
renderRunOverview();
assert.equal(ui.runTimeLimitNote.hidden, false);
assert.equal(ui.runTimeLimitNote.textContent, 'Stopped at the time limit (0.003 hours) at 00:09:29 · progress summary '
  + 'written · resumed at 02:50:06 with no limit');
assert.equal(row('Author time limit'), 'No time limit');
const local = value => localDateTime(value);
assert.deepEqual(row('Time limit history').split('\n'), [
  `${local('2026-10-29T00:05:07+00:00')} · Started with 0.003 hours (11 s)`,
  `${local('2026-10-29T00:09:29+00:00')} · Stopped at the time limit (0.003 hours) after 13 s of active work`,
  `${local('2026-10-29T02:48:54+00:00')} · Progress summary written`,
  `${local('2026-10-29T02:50:06+00:00')} · Changed to no time limit`,
  `${local('2026-10-29T02:50:06+00:00')} · Resumed with no time limit`,
  `${local('2026-10-29T03:00:00+00:00')} · Changed to 1.5 hours (90 min)`]);
// Two pauses at the limit: one line each; a pause not resumed yet ends after its summary.
state = job({phase: 'paused', thinkingHours: 0.003, timeLimitHistory: [...history.slice(0, 4),
  {event: 'reached', time: '2026-10-29T04:00:00+00:00', hours: 0.5, elapsedSeconds: 1800}]});
renderRunOverview();
assert.deepEqual(ui.runTimeLimitNote.textContent.split('\n'), [
  'Stopped at the time limit (0.003 hours) at 00:09:29 · progress summary written · resumed at 02:50:06 with no limit',
  'Stopped at the time limit (0.5 hours) at 04:00:00']);
assert.equal(row('Author time limit'), '0.003 hours (11 s)');
// Without a history the note is hidden and Run settings show only the limit; no author, no limit.
state = job({phase: 'running', thinkingHours: 2, stage: 'solve', activeNode: 'author'});
renderRunOverview();
assert.equal(ui.runTimeLimitNote.hidden, true);
assert.equal(row('Author time limit'), '2 hours');
assert.equal(row('Time limit history'), undefined);
state = job({resultKind: 'final', workflowStages: stages(false, false, false, true)});
renderRunOverview();
assert.equal(row('Author time limit'), 'Off');
""")

    def test_the_conversation_labels_the_time_limit_summary_and_links_to_it(self):
        run_node(self, RENDER, RENDER_SETUP, r"""
const at = minute => new Date(2026, 9, 29, 1, minute).getTime() / 1000;
const clock = minute => new Date(2026, 9, 29, 1, minute).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
const items = [
  {id: 'c_1', kind: 'summary', origin: 'time-limit', status: 'answered', createdAt: at(9), finishedAt: at(12),
   text: '', reply: 'No results yet.'},
  {id: 'c_2', kind: 'message', origin: 'user', status: 'answered', createdAt: at(20), text: 'Which lemma?', reply: 'Lemma 2.'},
  {id: 'c_3', kind: 'summary', origin: 'user', status: 'answered', createdAt: at(30), text: '', reply: 'Asked for.'},
];
render({runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', trace: [], traceVersion: 0, traceFrom: 0,
  workflowStages: stages(false, true, true, true), conversation: {available: true, busy: false, readOnly: true, items}});
const rows = ui.authorConversationList.children;
assert.equal(rows[0].children[0].textContent, `Progress summary at the time limit · ${clock(9)}`);
assert.equal(rows[2].children[0].textContent, `Progress summary · ${clock(30)} · requested`);
// The box opens at its newest item; the link above it leads to the summary written at the limit.
assert.equal(ui.authorConversationList.scrollTop, ui.authorConversationList.scrollHeight);
assert.equal(ui.conversationSummaryLink.hidden, false);
assert.equal(ui.conversationSummaryLink.textContent, `Go to the progress summary at the time limit (${clock(9)})`);
ui.authorConversationList.scrollTop = 900; ui.authorConversationList.top = 100; rows[0].top = -700;
ui.conversationSummaryLink.onclick();
assert.equal(ui.authorConversationList.scrollTop, 100);
assert.equal(rows[0].focused, true);
// Without a summary at the limit there is no link.
render({runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', trace: [], traceVersion: 0, traceFrom: 0,
  workflowStages: stages(false, true, true, true),
  conversation: {available: true, busy: false, readOnly: true, items: items.slice(1)}});
assert.equal(ui.conversationSummaryLink.hidden, true);
""")


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class ActivityAgeTests(unittest.TestCase):
    def test_the_now_card_says_when_the_last_event_arrived_and_since_when_a_call_retries(self):
        run_node(self, CLOCK, CLOCK_SETUP, r"""
const ago = seconds => new Date(Date.now() - seconds * 1000).toISOString();
state = {phase: 'running', lastActivityAt: ago(125), traceVersion: 3};
renderClock();
assert.equal(ui.runActivityAge.hidden, false);
assert.equal(ui.runActivityAge.textContent, 'Last activity 2m 05s ago');
const since = new Date(Date.now() - 600000);
state = {...state, modelRetry: {text: 'Model call failed', retryAt: Date.now() / 1000 + 30, since: since.toISOString()}};
renderClock();
assert.equal(ui.runActivityAge.textContent, 'Waiting to retry a failed model call (retries every minute since '
  + `${since.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}) · Last activity 2m 05s ago`);
assert.equal(ui.runActivityAge.dataset.retry, '1');
// Not working: nothing to say here.
state = {phase: 'paused', lastActivityAt: ago(5)};
renderClock();
assert.equal(ui.runActivityAge.hidden, true);
""")


class DownloadLabelTests(unittest.TestCase):
    def test_the_proof_download_is_named_download_md_and_keeps_its_id(self):
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.found, self.inside, self.text = {}, None, ""

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "button" and attrs.get("id", "").startswith("download"):
                    self.inside, self.text = attrs, ""

            def handle_data(self, data):
                if self.inside is not None:
                    self.text += data

            def handle_endtag(self, tag):
                if tag == "button" and self.inside is not None:
                    self.found[self.inside["id"]] = (" ".join(self.text.split()), self.inside.get("title", ""))
                    self.inside = None

        parser = Buttons()
        parser.feed((ROOT / "ui" / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(parser.found["downloadProofButton"],
                         ("Download .md", "The proof as Markdown, as handed to Writing & polishing"))
        self.assertEqual(parser.found["downloadTexButton"][0], "Download .tex")
        self.assertEqual(parser.found["downloadPdfButton"][0], "Download PDF")


class ModelStarted(BaseException):
    """A model process was about to start."""


class NoModelCase(unittest.TestCase):
    def setUp(self):
        for target, name in ((runtime, "goal_session"), (claude_cli, "goal_session"),
                             (runtime, "author_conversation"), (claude_cli, "author_conversation"),
                             (server.subprocess, "Popen")):
            patcher = mock.patch.object(target, name, side_effect=ModelStarted(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)

    def at(self, time, record):
        with mock.patch.object(server, "datetime") as clock:
            clock.now.return_value = datetime.fromisoformat(time)
            self.app.add_trace(record)


class TimeLimitHistoryTests(NoModelCase):
    RECORDS = [
        {"kind": "status", "stage": "solve", "label": "Workflow step: author", "node": "author",
         "time": "2026-10-29T00:05:07+00:00"},
        {"kind": "status", "stage": "solve", "node": "author", "label": "Time limit reached", "elapsedSeconds": 13.04,
         "authorLimitHours": 0.003, "time": "2026-10-29T00:09:29+00:00"},
        {"kind": "conversation_reply", "stage": "conversation", "label": "Progress summary",
         "conversationKind": "summary", "text": "No results.", "time": "2026-10-29T02:48:54+00:00"},
        {"kind": "conversation_reply", "stage": "conversation", "label": "Progress summary",
         "conversationKind": "summary", "text": "Again.", "time": "2026-10-29T02:49:00+00:00"},
        {"kind": "status", "stage": "solve", "node": "author", "label": "Total time limit set",
         "authorLimitHours": 0.0, "time": "2026-10-29T02:50:06+00:00"},
        {"kind": "status", "stage": "solve", "node": "author", "label": "Resume requested",
         "time": "2026-10-29T02:50:07+00:00"},
        # A later Resume (after a pause by the user) and summary are no time-limit events.
        {"kind": "status", "stage": "solve", "node": "author", "label": "Resume requested",
         "time": "2026-10-29T03:00:00+00:00"},
        {"kind": "conversation_reply", "stage": "conversation", "conversationKind": "summary", "text": "Later.",
         "time": "2026-10-29T03:10:00+00:00"},
    ]
    EXPECTED = [
        {"event": "reached", "hours": 0.003, "elapsedSeconds": 13.04, "time": "2026-10-29T00:09:29+00:00"},
        {"event": "summary", "time": "2026-10-29T02:48:54+00:00"},
        {"event": "set", "hours": 0.0, "time": "2026-10-29T02:50:06+00:00"},
        {"event": "resumed", "hours": 0.0, "time": "2026-10-29T02:50:07+00:00"},
    ]

    def test_the_history_records_changes_pauses_the_summary_and_the_resume(self):
        history = []
        changed = [server.follow_time_limit(history, record) for record in self.RECORDS]
        self.assertEqual(history, self.EXPECTED)
        self.assertEqual(changed, [False, True, True, False, True, True, False, False])
        # A Resume before any pause at the limit, and records without a time, are not events.
        self.assertFalse(server.follow_time_limit([], self.RECORDS[5]))
        self.assertFalse(server.follow_time_limit([], {**self.RECORDS[1], "time": None}))
        self.assertEqual(server.saved_time_limit_history(history + [{"event": "other", "time": "t"}, "x",
                                                                    {"event": "set", "time": "t", "hours": -1}]),
                         history)

    def test_limits_under_a_hundredth_of_an_hour_keep_their_digits(self):
        self.assertEqual([server.hours_text(value) for value in (0.003, 0.0005, 0, 0.01, 2)],
                         ["0.003 hours", "0.0005 hours", "0 hours", "0.01 hours", "2 hours"])

    def test_the_server_keeps_the_history_saves_it_and_rebuilds_it_after_a_restart(self):
        self.app = server.App(runs=self.runs)
        self.app._new_run("Prove that every planar graph is 5-colorable.")
        self.app._save_job_settings(self.app.state)
        for record in self.RECORDS:
            self.at(record["time"], {key: value for key, value in record.items() if key != "time"})
        self.assertEqual(self.app.snapshot()["timeLimitHistory"], self.EXPECTED)
        saved = json.loads(server.controlled_path(self.app.run_dir, server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(saved["timeLimitHistory"], self.EXPECTED)
        # A restart rebuilds it from the transcript, even when the saved copy is lost.
        saved.pop("timeLimitHistory")
        for folder in (self.app._controller_directory(), self.app.run_dir):
            (folder / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(saved))
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.snapshot()["timeLimitHistory"], self.EXPECTED)

    def test_a_saved_batch_shows_the_saved_history_or_rebuilds_it_from_recent_records(self):
        root = self.runs / "runs"
        saved_batch = root / "earlier"
        run = saved_batch / "job"
        run.mkdir(parents=True)
        (root / "current").mkdir()
        (run / "draft.md").write_text("# Draft problem\n\nA theorem.\n")
        (run / "transcript.jsonl").write_text("".join(json.dumps(record) + "\n" for record in self.RECORDS))
        (run / "job-settings.json").write_text(json.dumps({"thinkingHours": 0}))
        catalog = batches.BatchCatalog(root, current_root=root / "current")
        self.assertEqual(catalog.state("earlier", "job")["timeLimitHistory"], self.EXPECTED)
        kept = [{"event": "set", "hours": 2.0, "time": "2026-10-28T00:00:00+00:00"}]
        (run / "job-settings.json").write_text(json.dumps({"thinkingHours": 2, "timeLimitHistory": kept}))
        (run / "transcript.jsonl").write_text(json.dumps(self.RECORDS[0]) + "\n")
        self.assertEqual(catalog.state("earlier", "job")["timeLimitHistory"], kept)


class CriticProgressTests(NoModelCase):
    def test_the_server_counts_the_critics_of_the_round_that_runs(self):
        self.app = server.App(runs=self.runs)
        self.app._new_run("A theorem.")
        self.app.state["criticCount"] = 3
        progress = lambda label, **fields: {"kind": "status", "stage": "critic", "label": label,  # noqa: E731
                                            "subagentProgress": True, "auditCount": 3, **fields}
        empty = {"count": 3, "requested": False, "started": [], "finished": [], "failed": [], "waiting": False}
        step = {"kind": "status", "stage": "critic", "label": "Workflow step: critic", "node": "critic", "visit": 1}
        self.app.add_trace(step)
        self.assertEqual(self.app.snapshot()["criticProgress"], empty)
        before = self.app.snapshot()["criticProgress"]
        for record in (
                {"kind": "request", "stage": "critic", "label": "Critic coordinator", "attempt": 1},
                progress("3 independent critics requested", attempt=1),
                progress("Independent critic 1 started", auditIndex=1),
                progress("Independent critic 2 started", auditIndex=2),
                progress("Independent critic 3 started", auditIndex=3),
                progress("Independent critic 2 finished", auditIndex=2),
                progress("Independent critic 3 finished", auditIndex=3, failed=True),
                progress("Independent critic 3 finished", auditIndex=3),
                # A subagent's records, the paused author's replies and statuses that are no progress record
                # say nothing about the round.
                progress("Independent critic 1 finished", auditIndex=1, root=False),
                {**progress("Independent critic 1 finished", auditIndex=1), "stage": "conversation"},
                {**progress("Independent critic 1 finished", auditIndex=1), "subagentProgress": False},
                progress("Independent critic 1 finished")):
            self.app.add_trace(record)
        self.assertEqual(self.app.snapshot()["criticProgress"],
                         {**empty, "requested": True, "started": [1, 2, 3], "finished": [2, 3], "failed": [3]})
        # A snapshot taken earlier keeps what it copied.
        self.assertEqual(before, empty)
        # Codex shows only the wait for models of multi-agent version 2.
        self.app.add_trace(progress("Waiting for the independent critics", waiting=True))
        self.assertTrue(self.app.snapshot()["criticProgress"]["waiting"])
        # A retried attempt starts its critics anew, with its own number of them.
        self.app.add_trace(progress("2 independent critics requested", attempt=2, auditCount=2))
        self.assertEqual(self.app.snapshot()["criticProgress"], {**empty, "count": 2, "requested": True})
        self.app.add_trace(progress("Waiting for the independent critics", waiting=True))
        self.assertEqual(self.app.snapshot()["criticProgress"], {**empty, "count": 2, "requested": True, "waiting": True})
        self.app.add_trace({"kind": "critic_result", "stage": "critic", "round": 1, "outcome": "pass",
                            "report": {"verdict": "pass"}})
        self.assertIsNone(self.app.snapshot()["criticProgress"])
        self.app.add_trace(step)
        self.app.add_trace({**step, "label": "Workflow step: convention", "node": "convention"})
        self.assertIsNone(self.app.snapshot()["criticProgress"])

    def test_the_progress_follows_the_runners_own_labels(self):
        """The server reads exactly what workflow_runner.SubagentProgress writes."""

        records = []
        with mock.patch.object(server.runtime, "emit", lambda kind, stage, **fields: records.append(
                {"kind": kind, "stage": stage, **fields})):
            tracker = server.runtime.SubagentProgress("critic", 2)
            tracker.start_attempt(1)
            tracker.subagent("thread-a", "started")
            tracker.subagent("thread-b", "started")
            tracker.subagent("thread-b", "failed")
            tracker.waiting()
        progress = server.follow_critic_progress(None, {"kind": "status", "label": "Workflow step: critic",
                                                        "node": "critic"}, 3)
        for record in records:
            progress = server.follow_critic_progress(progress, record, 3)
        self.assertEqual(progress, {"count": 2, "requested": True, "started": [1, 2], "finished": [2], "failed": [2],
                                    "waiting": True})

    def test_the_critics_of_a_failed_call_are_gone_while_it_waits_to_retry(self):
        progress = {"count": 3, "requested": True, "started": [1, 2], "finished": [1], "failed": [], "waiting": True}
        retry = {"kind": "status", "stage": "critic", "label": "Critic coordinator · " + runtime.RETRY_LABEL,
                 "activityLabel": "Critic coordinator", "retryAt": 2e9, "text": "Model call failed."}
        self.assertEqual(server.follow_critic_progress(progress, retry, 3),
                         {"count": 3, "requested": True, "started": [], "finished": [], "failed": [], "waiting": False})
        # The retry's own start (no retry time) and another step's wait leave it as it is.
        for record in ({**retry, "retryAt": None, "text": "Retrying the model call of the critic now (attempt 2)."},
                       {**retry, "stage": "solve"}, {**retry, "root": False}):
            self.assertIs(server.follow_critic_progress(progress, record, 3), progress)

    def test_a_model_call_that_waits_to_retry_says_since_when_it_fails(self):
        self.app = server.App(runs=self.runs)
        self.app._new_run("A theorem.")
        now = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
        with mock.patch.object(server, "datetime") as clock:
            clock.now.return_value = now
            self.app._follow_model_retry({"kind": "status", "stage": "critic", "label": "Critic 2 of 3 · "
                                          + runtime.RETRY_LABEL, "activityLabel": "Critic 2 of 3",
                                          "retryAt": now.timestamp() + 60, "retryingSeconds": 300,
                                          "text": "Model call failed."})
        self.assertEqual(self.app.state["modelRetry"]["since"], "2026-10-29T09:55:00+00:00")


# The review of this package: the lists' finished counts, what a retry waits for, a saved batch's paused
# job, and the rail's mark when all critics have finished.
SIDEBAR = [["  // Finished jobs count as complete", "  function renderActive(where)"]]
SIDEBAR_SETUP = r"""
Object.defineProperty(Element.prototype, 'style', {get() { return this._style ||= {}; }});
const document = {createElement: tag => new Element(tag), createElementNS: (namespace, tag) => new Element(tag),
  createTextNode: value => Object.assign(new Element('#text'), {textContent: value})};
const svgNS = 'http://www.w3.org/2000/svg', batchUrl = id => `/?batch=${id}`, liveWords = new Element('span');
const stats = {};
for (const key of ['running', 'waiting', 'paused', 'limit', 'done']) {
  const stat = new Element('div'), value = Object.assign(new Element('strong'), {closest: () => stat});
  stats[`[data-stat="${key}"]`] = value;
}
stats['[data-stat-note="done"]'] = new Element('span');
const shell = {batches: new Element('ol'), batchTotal: new Element('span'), live: new Element('span'),
  foot: new Element('p'), overviewMeta: new Element('p'), overviewStats: {querySelector: selector => stats[selector] || null}};
"""
# A saved batch with two finished jobs that did not succeed, one complete job and one paused job.
SAVED_COUNTS = "{running: 0, done: 1, paused: 1, prepared: 0, incomplete: 2}"


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class FinishedCountTests(unittest.TestCase):
    def test_the_server_classifies_every_job_as_the_page_does(self):
        result = run_node(self, LISTS, LISTS_SETUP, OUTCOMES + r"""
const more = [{phase: 'done', statementReviewOnly: true}, {phase: 'done', revisionRequired: true},
  {phase: 'done', canDownloadTex: true}, {phase: 'done', resultKind: 'final', error: 'Stopped.', manuallyStopped: true},
  {phase: 'done', resultKind: 'proof', interrupted: true}, {phase: 'input'}, {phase: 'input', manuallyStopped: true}];
const jobs = [...finishedJobs.map(([job]) => job), ...liveJobs.map(([job]) => job), ...more];
console.log(JSON.stringify(jobs.map(job => [job, jobTone(job)])));
""")
        classified = json.loads(result.stdout)
        self.assertEqual(len(classified), 33)
        for job, tone in classified:
            self.assertEqual(batches.job_tone(job), tone, job)
        # The batch counts hold each job once; a finished job is done (complete) or incomplete.
        rows = [job for job, _ in classified]
        finished = [tone for job, tone in classified if job["phase"] == "done"]
        counts = batches.batch_counts(rows)
        self.assertEqual(sum(counts.values()), len(rows))
        self.assertEqual(counts["done"], sum(1 for tone in finished if tone == "done"))
        self.assertEqual(counts["incomplete"], sum(1 for tone in finished if tone != "done"))

    def test_the_sidebar_and_overview_give_the_check_mark_only_to_complete_jobs(self):
        run_node(self, SIDEBAR, SIDEBAR_SETUP, r"""
const batches = [{id: 'earlier', label: 'Earlier', readOnly: true, jobCount: 5, counts: """ + SAVED_COUNTS + r"""},
  {id: '__root__', label: 'Runs', current: true, jobCount: 2, counts: {running: 1, done: 1}}];
renderNavBatches(batches);
const [saved, live] = shell.batches.children.map(item => item.children[0]);
const part = (link, name) => link.children.find(child => child.className === name);
const glyphs = link => part(link, 'nav-counts').children.filter(child => child.className.startsWith('nc '))
  .map(child => [child.className, text(child)]);
// One complete job carries the check mark; the two incomplete ones are counted apart, with a cross.
assert.deepEqual(glyphs(saved), [['nc paused', '1'], ['nc done', '1'], ['nc incomplete', '2']]);
assert.deepEqual(part(saved, 'nav-meter').children.map(child => [child.className, child.style.flexGrow]),
  [['paused', '1'], ['done', '1'], ['incomplete', '2'], ['other', '1']]);
assert.equal(saved.dataset.tipMeta, 'Read-only · 5 problems · 1 paused · 1 complete · 2 incomplete');
assert.match(text(part(saved, 'visually-hidden')), /1 complete, 2 incomplete$/);
assert.deepEqual(glyphs(live), [['nc running', '1'], ['nc done', '1']]);
renderTotals(batches);
assert.equal(stats['[data-stat="done"]'].textContent, '2');
assert.equal(stats['[data-stat-note="done"]'].textContent, 'finished successfully · 2 incomplete');
assert.deepEqual(shell.overviewMeta.children.map(child => [child.className, child.textContent]),
  [['', '2 batches'], ['', '7 problems'], ['meta-running', '1 running'], ['meta-paused', '1 paused'],
   ['meta-done', '2 complete'], ['meta-incomplete', '2 incomplete']]);
renderTotals(batches.slice(1));
assert.equal(stats['[data-stat-note="done"]'].textContent, 'finished successfully');
""")

    def test_the_overview_batch_cards_count_complete_and_incomplete_jobs(self):
        run_node(self, LISTS, LISTS_SETUP, r"""
renderBatches([{id: 'earlier', label: 'Earlier', readOnly: true, jobCount: 5, counts: """ + SAVED_COUNTS + r"""}]);
const progress = ui.batchesList.children[0].children[0].children[2];
assert.deepEqual(progress.children.map(child => [child.className, child.textContent]),
  [['paused', '1 paused'], ['done', '1 complete'], ['incomplete', '2 incomplete']]);
""")

    def test_the_styles_mark_incomplete_counts_with_the_failed_cross(self):
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        for rule in (".nc.incomplete { --tone: var(--failed); }", ".nc.incomplete .nc-glyph {",
                     ".nav-meter i.incomplete { background: rgb(var(--failed-rgb) / 0.6); }",
                     ".batch-progress .incomplete { --tone: var(--failed); }",
                     ":is(.batch-progress .incomplete, .job-status.tone-failed)::before {",
                     '.nav-meter i.incomplete { background: rgb(var(--accent-rgb) / 0.18); }'):
            self.assertIn(rule, css)
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<dt>Complete</dt><dd><strong data-stat="done">–</strong><span class="stat-note" '
                      'data-stat-note="done">finished successfully</span>', html)


def saved_job(folder, *, settings=None, records=(), files=None):
    """A saved job folder: its draft, settings, transcript and the given files (name: text or JSON)."""
    folder.mkdir(parents=True)
    (folder / "draft.md").write_text("# Draft problem\n\nA theorem.\n")
    (folder / "job-settings.json").write_text(json.dumps(settings or {"thinkingHours": 0}))
    (folder / "transcript.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records or [
        AUTHOR_STEP]))
    for name, content in (files or {}).items():
        (folder / name).write_text(content if isinstance(content, str) else json.dumps(content))
    return folder


# Three finished jobs: complete, failed and stopped while the PDF compiled.
FINISHED = {
    "ok": ({"final.tex": "\\documentclass{article}\n"}, []),
    "failed": ({"failure-summary.md": "No proof.\n"},
               [{"kind": "author_result", "stage": "solve", "node": "author", "time": "2026-10-29T00:09:00+00:00"},
                {"kind": "failure_result", "stage": "failure", "node": "failure_summary", "label": "Failure summary",
                 "time": "2026-10-29T00:10:00+00:00"}]),
    "stopped": ({"manual-stop.json": {"schemaVersion": 1, "stage": "final", "node": "latex_compile"}},
                [{"kind": "status", "stage": "final", "node": "latex_compile", "label": "Workflow step: latex_compile",
                  "time": "2026-10-29T00:10:00+00:00"},
                 {"kind": "status", "stage": "final", "node": "latex_compile", "label": "Stop requested",
                  "time": "2026-10-29T00:11:00+00:00"}]),
}
AUTHOR_STEP = {"kind": "status", "stage": "solve", "node": "author", "label": "Workflow step: author",
               "time": "2026-10-29T00:00:00+00:00"}


class BatchCardCountTests(NoModelCase):
    def test_a_saved_batch_card_counts_its_incomplete_finished_jobs_apart(self):
        root = self.runs / "runs"
        (root / "current").mkdir(parents=True)
        for name, (files, records) in FINISHED.items():
            saved_job(root / "earlier" / name, files=files, records=[AUTHOR_STEP, *records])
        catalog = batches.BatchCatalog(root, current_root=root / "current")
        card = next(card for card in catalog.list_batches() if card["id"] == "earlier")
        self.assertEqual(card["counts"], {"running": 0, "done": 1, "paused": 0, "limit": 0, "prepared": 0,
                                          "incomplete": 2})
        jobs = {job["runId"]: job for job in catalog.jobs("earlier")}
        self.assertEqual({name: batches.job_tone(job) for name, job in jobs.items()},
                         {"ok": "done", "failed": "failed", "stopped": "stopped"})
        # The stop marker names the step it stopped at: the compile step, not only the writing stage.
        self.assertEqual(jobs["stopped"]["activeNode"], "latex_compile")
        self.assertEqual(catalog.state("earlier", "stopped")["activeNode"], "latex_compile")

    def test_the_live_batch_card_counts_its_incomplete_finished_jobs_apart(self):
        import http.client
        import threading

        root = self.runs / "runs"
        for name, (files, records) in FINISHED.items():
            saved_job(root / name, files=files, records=[AUTHOR_STEP, *records])
        http_server = server.Server((server.HOST, 0), runs=root)
        # Restored after a restart, the job stopped while its PDF compiled still names that step.
        self.assertEqual(http_server.jobs["stopped"].state["activeNode"], "latex_compile")
        thread = threading.Thread(target=http_server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(server.HOST, http_server.server_port, timeout=3)
            connection.request("GET", "/batches", headers={"Origin": http_server.origin,
                                                           "X-TCS-Prover-Token": http_server.token})
            cards = json.loads(connection.getresponse().read())["batches"]
            connection.close()
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=2)
        root_card = next(card for card in cards if card["id"] == "__root__")
        self.assertEqual(root_card["counts"], {"done": 1, "incomplete": 2})

    def test_the_gateway_counts_its_controllers_incomplete_jobs_apart(self):
        from types import SimpleNamespace
        from ui import live_gateway

        def controller(*phases):
            rows = [{"runId": f"r{index}", "startedAt": "", **fields} for index, fields in enumerate(phases)]
            return SimpleNamespace(jobs=lambda: {"jobs": rows})

        gateway = SimpleNamespace(
            catalog=None, legacy_batch="v4", current_batch="new",
            legacy=controller({"phase": "done", "resultKind": "final"}, {"phase": "done", "error": "Failed."}),
            current=controller({"phase": "running"}, {"phase": "done", "manuallyStopped": True}))
        cards = {card["id"]: card for card in live_gateway.GatewayServer.batch_cards(gateway)["batches"]}
        self.assertEqual(cards["v4"]["counts"], {"done": 1, "incomplete": 1})
        self.assertEqual(cards["new"]["counts"], {"running": 1, "incomplete": 1})


class RetryKindTests(NoModelCase):
    def follow(self, time, record):
        with mock.patch.object(server, "datetime") as clock:
            clock.now.return_value = datetime.fromisoformat(time)
            self.app._follow_model_retry(record)
        return self.app.state["modelRetry"]

    def test_the_server_says_what_waits_and_keeps_when_its_failures_began(self):
        self.app = server.App(runs=self.runs)
        self.app._new_run("A theorem.")
        write = {"kind": "status", "stage": "final", "label": runtime.STORAGE_RETRY_LABEL, "failure": "disk full",
                 "failureKind": "storage", "retryInSeconds": 60, "text": "Could not save the LaTeX source (disk full)."}
        # A file write sends no retrying time: a later attempt keeps the time of the first failure.
        first = self.follow("2026-10-29T10:00:00+00:00", {**write, "retryAttempt": 2, "retryAt": 2e9})
        self.assertEqual((first["kind"], first["since"]), ("storage", "2026-10-29T10:00:00+00:00"))
        self.assertIsNone(self.follow("2026-10-29T10:01:00+00:00", {**write, "retryAttempt": 2,
                                                                     "text": "Saving it again now (attempt 2)."}))
        later = self.follow("2026-10-29T10:01:05+00:00", {**write, "retryAttempt": 3, "retryAt": 2e9})
        self.assertEqual(later["since"], "2026-10-29T10:00:00+00:00")
        # The next failing write starts again at attempt 2: its own time.
        self.follow("2026-10-29T10:02:00+00:00", {**write, "retryAttempt": 3})
        again = self.follow("2026-10-29T11:00:00+00:00", {**write, "retryAttempt": 2, "retryAt": 2e9})
        self.assertEqual(again["since"], "2026-10-29T11:00:00+00:00")
        # A command that could not run (TeX), and a model call: the runner's retrying time counts.
        command = self.follow("2026-10-29T12:00:00+00:00", {
            "kind": "status", "stage": "final", "node": "latex_compile", "label": runtime.COMMAND_RETRY_LABEL,
            "retryAttempt": 3, "retryAt": 2e9, "retryingSeconds": 120, "failureKind": "command",
            "text": "latexmk is not available. Trying again in 1 min (attempt 3)."})
        self.assertEqual((command["kind"], command["since"]), ("command", "2026-10-29T11:58:00+00:00"))
        model = self.follow("2026-10-29T12:00:00+00:00", {
            "kind": "status", "stage": "critic", "label": "Critic 1 of 3 · " + runtime.RETRY_LABEL,
            "activityLabel": "Critic 1 of 3", "retryAttempt": 2, "retryAt": 2e9, "text": "Model call failed."})
        self.assertEqual((model["kind"], model["since"]), ("model", "2026-10-29T12:00:00+00:00"))


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class RetryWaitTextTests(unittest.TestCase):
    def test_the_now_card_says_what_waits_to_retry(self):
        run_node(self, CLOCK, CLOCK_SETUP, r"""
const since = new Date(Date.now() - 600000), clock = since.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
const retry = kind => ({text: 'x', retryAt: Date.now() / 1000 + 30, since: since.toISOString(), ...(kind ? {kind} : {})});
for (const [kind, what] of [['command', 'a command that could not run'], ['storage', 'saving a file'],
    ['model', 'a failed model call'], ['', 'a failed model call']]) {
  state = {phase: 'running', activeNode: 'latex_compile', modelRetry: retry(kind)};
  renderClock();
  assert.equal(ui.runActivityAge.textContent, `Waiting to retry ${what} (retries every minute since ${clock})`);
}
""")


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class ReadOnlyPauseTests(unittest.TestCase):
    def test_a_saved_paused_job_with_its_cause_offers_no_resume_or_new_limit(self):
        run_node(self, RENDER, RENDER_SETUP, r"""
const paused = {runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', trace: [], traceVersion: 0,
  traceFrom: 0, workflowStages: stages(false, true, true, true), readOnly: true, thinkingHours: 0.003,
  activeElapsedSeconds: 13};
for (const [fields, title] of [[{pauseCause: 'time-limit', timeLimitExhausted: true}, 'Time limit reached'],
  [{pauseCause: 'user'}, 'Paused'], [{pauseCause: ''}, 'Paused']]) {
  render({...paused, ...fields});
  assert.equal(ui.runTitle.textContent, title);
  assert.equal(ui.runDescription.textContent, '');
  assert.equal(ui.resume.hidden, true);
  assert.equal(ui.authorTimeLimitControl.hidden, true);
}
""")


# The batch page's status filter, as the workbench shell renders its segments and counts.
FILTER = [["  // Batch page: show one status at a time", "  function renderModelsMeta()"]]
FILTER_SETUP = r"""
class MutationObserver { observe() {} }
const route = () => 'batch';
let currentBatch = 'b';
const ui = {jobsList: new Element('ol')};
const buttons = ['all', 'running', 'waiting', 'paused', 'limit', 'done', 'failed'].map(key => {
  const button = new Element('button'), count = new Element('span');
  button.dataset.show = key;
  count.className = 'seg-count';
  button.append(count);
  return button;
});
const shell = {filter: {querySelectorAll: () => buttons}, filterEmpty: new Element('p')};
"""
# A job stopped at its time limit, a user's pause and the pauses of a restart, an interruption and a failure.
PAUSES = r"""
const pauses = [['time-limit', 'limit'], ['user', 'paused'], ['restart', 'paused'], ['interrupted', 'paused'],
  ['error', 'paused'], ['', 'paused']];
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class TimeLimitStatusTests(unittest.TestCase):
    """A job stopped at its time limit reads "Time limit reached" (a clock) everywhere, never as paused."""

    def test_the_classification_puts_a_stop_at_the_time_limit_apart_from_every_pause(self):
        result = run_node(self, LISTS, LISTS_SETUP, PAUSES + r"""
for (const [pauseCause, tone] of pauses) {
  assert.equal(jobTone({phase: 'paused', pauseCause}), tone, pauseCause);
  assert.equal(timeLimitStopped({phase: 'paused', pauseCause}), tone === 'limit', pauseCause);
}
// Only a paused job is stopped at its limit: while it pauses it still works, and a finished job is finished.
assert.equal(jobTone({phase: 'pausing', pauseCause: 'time-limit'}), 'running');
assert.equal(timeLimitStopped({phase: 'done', pauseCause: 'time-limit', resultKind: 'final'}), false);
assert.equal(jobTone({phase: 'done', pauseCause: 'time-limit', resultKind: 'final'}), 'done');
console.log(JSON.stringify(pauses.map(([pauseCause, tone]) => [{phase: 'paused', pauseCause}, tone])));
""")
        # The server classifies and counts the same way (the batch cards and the sidebar read its counts).
        rows = []
        for job, tone in json.loads(result.stdout):
            self.assertEqual(batches.job_tone(job), tone, job)
            rows.append(job)
        self.assertEqual(batches.batch_counts(rows + [{"phase": "done", "resultKind": "final"}]),
                         {"limit": 1, "paused": 5, "done": 1})

    def test_the_batch_list_says_time_limit_reached_and_counts_it_apart(self):
        run_node(self, LISTS, LISTS_SETUP, r"""
renderJobs([
  {runId: 'limit', title: 'L', phase: 'paused', pauseCause: 'time-limit', error: 'Workflow time limit reached.'},
  {runId: 'user', title: 'U', phase: 'paused', pauseCause: 'user'},
  {runId: 'restart', title: 'R', phase: 'paused', pauseCause: 'restart', error: 'The server closed.'},
  {runId: 'done', title: 'D', phase: 'done', resultKind: 'final'}]);
const [limit, user, restart] = ui.jobsList.children;
const status = row => row.children[0].children[1];
const errors = row => row.children[0].children.filter(child => child.className === 'job-error').map(text);
assert.equal(status(limit).textContent, 'Time limit reached');
assert.match(limit.className, /(^| )tone-limit( |$)/);
assert.match(status(limit).className, /(^| )tone-limit( |$)/);
assert.doesNotMatch(`${limit.className} ${status(limit).className}`, /tone-paused/);
// A stop at the time limit is no failure: no red error line under it.
assert.deepEqual(errors(limit), []);
// Every other pause still reads Paused, with its reason.
for (const row of [user, restart]) {
  assert.equal(status(row).textContent, 'Paused');
  assert.match(row.className, /(^| )tone-paused( |$)/);
}
assert.deepEqual(errors(restart), ['The server closed.']);
assert.equal(ui.jobsCount.textContent, '4 problems · 2 paused · 1 time limit reached · 1 finished');
// The overview's batch rows: problems by status, the time limit apart from the pauses.
renderBatches([{id: 'b', label: 'B', readOnly: true, jobCount: 4, counts: {paused: 1, limit: 2, done: 1}}]);
const progress = ui.batchesList.children[0].children[0].children[2];
assert.deepEqual(progress.children.map(child => [child.className, child.textContent]),
  [['paused', '1 paused'], ['limit', '2 time limit reached'], ['done', '1 complete']]);
""")

    def test_the_time_limit_filter_shows_and_counts_only_those_jobs(self):
        run_node(self, FILTER, FILTER_SETUP, r"""
for (const tone of ['limit', 'limit', 'paused', 'done', 'failed']) {
  ui.jobsList.append(Object.assign(new Element('li'), {className: `job-card tone-${tone}`}));
}
ui.jobsList.dataset.signature = 'rows';
renderFilter();
const counts = () => buttons.map(button => [button.dataset.show, button.querySelector('.seg-count').textContent]);
assert.deepEqual(counts(), [['all', '5'], ['running', '0'], ['waiting', '0'], ['paused', '1'], ['limit', '2'],
  ['done', '1'], ['failed', '1']]);
shell.filter.onclick({target: {closest: () => buttons[4]}});
assert.equal(ui.jobsList.dataset.show, 'limit');
assert.equal(buttons[4].getAttribute('aria-pressed'), 'true');
assert.equal(buttons[3].getAttribute('aria-pressed'), 'false');
assert.equal(shell.filterEmpty.hidden, true);
""")
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<button type="button" class="seg" data-show="paused" aria-pressed="false">Paused <span '
                      'class="seg-count" data-count="paused"></span></button>\n              <button type="button" '
                      'class="seg" data-show="limit" aria-pressed="false">Time limit <span class="seg-count" '
                      'data-count="limit"></span></button>', html)
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('.jobs-list[data-show="limit"] > li:not(.tone-limit),', css)

    def test_the_sidebar_and_the_overview_count_time_limit_reached_apart(self):
        run_node(self, SIDEBAR, SIDEBAR_SETUP, r"""
const batches = [{id: 'earlier', label: 'Earlier', readOnly: true, jobCount: 4, counts: {paused: 1, limit: 2, done: 1}},
  {id: '__root__', label: 'Runs', current: true, jobCount: 3, counts: {running: 1, paused: 1, limit: 1}}];
renderNavBatches(batches);
const [saved, live] = shell.batches.children.map(item => item.children[0]);
const part = (link, name) => link.children.find(child => child.className === name);
const glyphs = link => part(link, 'nav-counts').children.filter(child => child.className.startsWith('nc '))
  .map(child => [child.className, text(child)]);
assert.deepEqual(glyphs(saved), [['nc paused', '1'], ['nc limit', '2'], ['nc done', '1']]);
assert.deepEqual(part(saved, 'nav-meter').children.map(child => [child.className, child.style.flexGrow]),
  [['paused', '1'], ['limit', '2'], ['done', '1']]);
assert.equal(saved.dataset.tipMeta, 'Read-only · 4 problems · 1 paused · 2 time limit reached · 1 complete');
assert.match(text(part(saved, 'visually-hidden')), /1 paused, 2 time limit reached, 1 complete$/);
assert.deepEqual(glyphs(live), [['nc running', '1'], ['nc paused', '1'], ['nc limit', '1']]);
renderTotals(batches);
// The tiles: Paused counts only the pauses; Time limit reached has its own.
assert.equal(stats['[data-stat="paused"]'].textContent, '2');
assert.equal(stats['[data-stat="limit"]'].textContent, '3');
assert.equal(stats['[data-stat="done"]'].textContent, '1');
assert.deepEqual(shell.overviewMeta.children.map(child => [child.className, child.textContent]),
  [['', '2 batches'], ['', '7 problems'], ['meta-running', '1 running'], ['meta-paused', '2 paused'],
   ['meta-limit', '3 time limit reached'], ['meta-done', '1 complete']]);
""")
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        tiles = ['<dt>Paused</dt><dd><strong data-stat="paused">–</strong><span class="stat-note">saved, can resume'
                 '</span>', '<div class="stat tone-limit"><dt>Time limit reached</dt><dd><strong data-stat="limit">'
                 '–</strong><span class="stat-note">can continue</span>', '<dt>Complete</dt>']
        self.assertLess(html.index(tiles[0]), html.index(tiles[1]))
        self.assertLess(html.index(tiles[1]), html.index(tiles[2]))
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        for rule in (".stat-strip { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));",
                     ".nc.limit { --tone: var(--stopped); }", ".batch-progress .limit, .tone-limit { --tone: "
                     "var(--stopped); }", ".job[data-status=\"limit\"] { --status: var(--stopped); }",
                     ".page-meta .meta-limit { color: var(--stopped); }"):
            self.assertIn(rule, css)
        # The clock is drawn with a border and gradients: the page's policy loads no data: images.
        self.assertNotIn("data:image", css)

    def test_the_job_header_says_time_limit_reached_and_a_pause_says_paused(self):
        run_node(self, OVERVIEW, OVERVIEW_SETUP, r"""
const author = {phase: 'paused', stage: 'solve', activeNode: 'author', workflow: {nodes: {author: {label: 'Proof author'}}},
  processAlive: false, workerActive: false, thinkingHours: 29.8, activeElapsedSeconds: 107424, timeLimitExhausted: true,
  pauseCause: 'time-limit'};
state = job(author);
renderRunOverview();
assert.equal(ui.runOverview.dataset.status, 'limit');
assert.equal(ui.runStage.textContent, 'Proof author · time limit reached');
assert.equal(ui.runHealth.textContent, 'Stopped at the time limit · saved conversation');
assert.equal(ui.runBudget.textContent, '29.8h · used up');
// The automatic progress summary, queued or being written, is said; a reply to a message is the author replying.
for (const status of ['queued', 'running']) {
  state = job({...author, processAlive: status === 'running',
    conversation: {items: [{kind: 'summary', origin: 'time-limit', status}]}});
  renderRunOverview();
  assert.equal(ui.runHealth.textContent, 'Time limit reached · writing progress summary');
}
state = job({...author, processAlive: true, conversation: {items: [
  {kind: 'summary', origin: 'time-limit', status: 'answered'}, {kind: 'message', origin: 'user', status: 'running'}]}});
renderRunOverview();
assert.equal(ui.runHealth.textContent, 'Stopped at the time limit · author replying');
// A saved (read-only) job writes nothing.
state = job({...author, readOnly: true, conversation: {items: [{kind: 'summary', origin: 'time-limit', status: 'queued'}]}});
renderRunOverview();
assert.equal(ui.runHealth.textContent, 'Stopped at the time limit · saved conversation');
// Every other pause reads Paused.
for (const pauseCause of ['user', 'restart', 'interrupted', 'error', '']) {
  state = job({...author, pauseCause, timeLimitExhausted: false});
  renderRunOverview();
  assert.equal(ui.runOverview.dataset.status, 'paused', pauseCause);
  assert.equal(ui.runStage.textContent, 'Proof author · paused');
  assert.equal(ui.runHealth.textContent, 'Paused · saved conversation');
}
""")

    def test_the_pill_and_now_card_say_time_limit_reached_and_offer_continue(self):
        run_node(self, RENDER, RENDER_SETUP, r"""
const base = {runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', trace: [], traceVersion: 0, traceFrom: 0,
  workflowStages: stages(false, true, true, true), workflow: {nodes: {author: {label: 'Proof author'}}},
  thinkingHours: 2, activeElapsedSeconds: 7230, conversation: {available: true, busy: false, items: []}};
render({...base, pauseCause: 'time-limit', timeLimitExhausted: true});
assert.equal(ui.globalStatus.textContent, 'Time limit reached');
assert.equal(ui.runLabel.textContent, 'TIME LIMIT REACHED');
assert.equal(ui.runTitle.textContent, 'Time limit reached');
assert.equal(ui.runDescription.textContent, 'The proof author reached the time limit of 2 hours after 2h 0m of active '
  + 'work and stopped. Raise the time limit to continue.');
// It can still raise the limit and continue: the button reads Continue.
assert.equal(ui.authorTimeLimitControl.hidden, false);
assert.equal(ui.resume.hidden, false);
assert.equal(ui.resume.textContent, 'Continue');
assert.equal(ui.authorSteerLabel.textContent, 'Ask the author');
// Raised, it still continues (not resumes).
render({...base, pauseCause: 'time-limit', thinkingHours: 3, timeLimitExhausted: false});
assert.equal(ui.globalStatus.textContent, 'Time limit reached');
assert.equal(ui.resume.textContent, 'Continue');
assert.match(ui.runDescription.textContent, /Choose Continue to go on\.$/);
// A pause by you, a restart, an interruption or a failure: Paused, and Resume.
for (const [index, pauseCause] of ['user', 'restart', 'interrupted', 'error', ''].entries()) {
  render({...base, runId: `p${index}`, pauseCause});
  assert.equal(ui.globalStatus.textContent, 'Paused', pauseCause);
  assert.equal(ui.runLabel.textContent, 'RUN PAUSED');
  assert.equal(ui.runTitle.textContent, 'Ready to resume');
  assert.equal(ui.resume.textContent, 'Resume');
  assert.equal(ui.authorSteerLabel.textContent, 'Ask the paused author');
}
// Back at the time limit, the box asks the author again.
render({...base, pauseCause: 'time-limit', timeLimitExhausted: true});
assert.equal(ui.authorSteerLabel.textContent, 'Ask the author');
""")


class SavedPauseCauseTests(NoModelCase):
    def test_a_saved_batch_job_paused_at_its_time_limit_says_so(self):
        root = self.runs / "runs"
        (root / "current").mkdir(parents=True)
        pause = {"status": "paused", "pauseCause": "time-limit", "node": "author", "stage": "solve", "error": "",
                 "elapsedSeconds": 13, "state": {"statement": "A theorem."}}
        saved_job(root / "earlier" / "job", records=TimeLimitHistoryTests.RECORDS[:2], files={"pause.json": pause},
                  settings={"thinkingHours": 0.003,
                            "workflowStages": {"review": False, "author": True, "critic": True, "writing": True}})
        # A cause this version does not know reads as none.
        saved_job(root / "earlier" / "other", files={"pause.json": {**pause, "pauseCause": "rate_limit"}})
        catalog = batches.BatchCatalog(root, current_root=root / "current")
        state = catalog.state("earlier", "job")
        self.assertEqual((state["phase"], state["pauseCause"], state["timeLimitExhausted"], state["readOnly"]),
                         ("paused", "time-limit", True, True))
        self.assertEqual({job["runId"]: job["pauseCause"] for job in catalog.jobs("earlier")},
                         {"job": "time-limit", "other": ""})
        self.assertFalse(catalog.state("earlier", "other")["timeLimitExhausted"])
        # The saved batch classifies and counts it as the live one does: apart from the paused job.
        self.assertEqual({job["runId"]: batches.job_tone(job) for job in catalog.jobs("earlier")},
                         {"job": "limit", "other": "paused"})
        card = next(card for card in catalog.list_batches() if card["id"] == "earlier")
        self.assertEqual((card["counts"]["limit"], card["counts"]["paused"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
