"""The job page around a paused author: the time-limit pause, the limit control, the steer box that
asks the paused author, the conversation card and the Resume, Summarize and Stop reply buttons.

These checks cut ui/app.js into slices by marker text and run them in Node's vm with a small fake
DOM, as the other UI tests do, against snapshots shaped like the server's (spec §4.1): pauseCause,
timeLimitExhausted, criticFeedbackPending and conversation = {available, reason, busy, readOnly,
items}. No server, model or CLI is involved.
"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"

# Runs `setup`, the named slices of app.js (located by marker text) and then `checks` as an async
# function body in one vm context. Fails if a marker is missing or the checks never finish.
RUNNER = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const {slices, setup, checks} = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = slices.map(([from, to]) => {
  const start = source.indexOf(from), end = source.indexOf(to, start + 1);
  if (start < 0 || end < 0) throw new Error('Marker not found: ' + (start < 0 ? from : to));
  return source.slice(start, end);
}).join('\n');
// The fake DOM is defined inside the vm, so its arrays compare equal to the checks' own.
const dom = String.raw`
class Element {
  constructor(tagName = 'div') {
    this.tagName = String(tagName).toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
    this.parent = null; this.hidden = false; this.disabled = false; this.open = false; this.value = '';
    this.textContent = ''; this.className = ''; this.title = ''; this.placeholder = ''; this.type = '';
    this.scrollTop = 0; this.scrollHeight = 0; this.clicked = 0;
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
  focus() { this.focused = true; }
  click() { this.clicked += 1; }
  scrollIntoView() { this.scrolled = true; }
  matches(selector) {
    return selector.startsWith('.') ? this.className.split(/\s+/).includes(selector.slice(1))
      : this.tagName === selector.toUpperCase();
  }
  descendants() {
    return this.children.flatMap(child => child instanceof Element ? [child, ...child.descendants()] : []);
  }
  querySelectorAll(selector) { return this.descendants().filter(node => node.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parent) if (node.matches(selector)) return node;
    return null;
  }
}`;
let finished = false;
const context = {assert, URLSearchParams, setImmediate, TextEncoder, console, finish: () => { finished = true; }};
process.on('exit', () => {
  if (!finished && !process.exitCode) { console.error('The checks did not finish.'); process.exitCode = 1; }
});
Promise.resolve(vm.runInNewContext(`${dom}\n${setup}\n${code}\n(async () => {\n${checks}\nfinish();\n})()`, context))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""

# render() with the helpers after it, the time helpers it uses, the related-works row and the
# Resume, Summarize and Stop reply handlers.
RENDER = [
    ["function completedRunLabel(", "function localDateTime("],
    ["function runElapsedSeconds()", "function unconfiguredApiModel("],
    ["function render(next)", "async function refresh()"],
    ["function composerRelatedWorks()", "// Read files directly"],
    ["ui.stop.onclick =", "ui.home.onclick ="],
]
RENDER_SETUP = r"""
let state = {}, previousPhase = '', currentJob = 'job', currentBatch = '__root__', reviewPending = false;
let timer, clock, stateReceivedAt = Date.now(), created = 0;
const polls = [];
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const document = {activeElement: null, body: new Element('body'),
  createElement: tag => { created += 1; return new Element(tag); }};
const show = (element, visible) => { element.hidden = !visible; };
const retainTrace = entries => entries;
const ingest = () => {}, renderWorkflow = () => {}, renderClock = () => {};
const renderBatchNavigation = () => {}, refresh = () => {};
const restoreRetrievalSettings = () => {}, syncPrompts = () => {}, updateModelSummary = () => {};
const appendFormattedText = () => {}, checkEdited = () => {}, restoreWorkflowStages = () => {};
const setTimeout = (callback, delay) => { polls.push(delay); return polls.length; };
const clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
const jobPath = path => path + '?job=job', sessionToken = 'secret';
const acted = [];
let actResult = true, afterAct = () => {};
// Like act(): the request is sent at once, and the job's new state arrives later.
const act = async (path, body) => {
  acted.push({path, body});
  const result = actResult;
  await null;
  afterAct(path, body);
  return result;
};
const author = {runId: 'r1', phase: 'paused', stage: 'solve', activeNode: 'author', thinkingHours: 2,
  activeElapsedSeconds: 7230, workflowStages: {review: false, author: true, critic: true, writing: true}};
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class ConversationBrowserTests(unittest.TestCase):
    def run_app(self, slices, setup, checks):
        payload = json.dumps({"slices": slices, "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(APP)], input=payload,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_markers_occur_once(self):
        source = APP.read_text(encoding="utf-8")
        markers = {marker for pair in RENDER + CLOCK + SEND + LIMIT + OVERVIEW for marker in pair}
        for marker in sorted(markers):
            with self.subTest(marker=marker):
                self.assertEqual(source.count(marker), 1)

    def test_a_time_limit_pause_says_why_opens_the_limit_once_and_offers_to_raise_it(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const summary = {id: 'c_0000000001', kind: 'summary', origin: 'time-limit', status: 'running', createdAt: 1761580800,
  text: '', reply: '', error: ''};
const base = {...author, pauseCause: 'time-limit', timeLimitExhausted: true,
  conversation: {available: true, busy: true, readOnly: true, reason: '', items: [summary]}};
render(base);
assert.equal(ui.runTitle.textContent, 'Time limit reached');
assert.equal(ui.runDescription.textContent, 'The proof author reached the time limit of 2 hours after 2h 0m of '
  + 'active work and stopped. It is writing a progress summary below. Raise the time limit to continue.');
assert.equal(ui.authorTimeLimitControl.hidden, false);
assert.equal(ui.authorTimeLimitControl.open, true);
assert.equal(ui.authorLimitHours.value, '2');
// Closed again by the user, it stays closed while the same run is polled.
ui.authorTimeLimitControl.open = false;
render(base);
assert.equal(ui.authorTimeLimitControl.open, false);
// The summary is written; the critic's rejection has not reached the author yet.
render({...base, criticFeedbackPending: true, conversation: {...base.conversation, busy: false,
  items: [{...summary, status: 'answered', reply: 'Lemma 1 is proved in proof.md.'}]}});
assert.equal(ui.runDescription.textContent, 'The proof author reached the time limit of 2 hours after 2h 0m of '
  + 'active work and stopped. Raise the time limit to continue. The critic rejected the latest proof; the author '
  + 'receives its report when the run continues.');
// A raised limit says what is left, and that Continue goes on; 0 turns it off.
render({...base, thinkingHours: 3, timeLimitExhausted: false, conversation: {...base.conversation, busy: false}});
assert.equal(ui.runDescription.textContent, 'The time limit is now 3 hours (59m 30s left). Choose Continue to go on.');
render({...base, thinkingHours: 0, timeLimitExhausted: false, conversation: {...base.conversation, busy: false}});
assert.equal(ui.runDescription.textContent, 'The time limit is now off. Choose Continue to go on.');
render({...base, thinkingHours: 1, timeLimitExhausted: true, activeElapsedSeconds: 3600,
  conversation: {available: false, reason: 'x', items: []}});
assert.match(ui.runDescription.textContent, /^The proof author reached the time limit of 1 hour after 1h 0m /);
// Another run opens its own control.
render({...base, runId: 'r2'});
assert.equal(ui.authorTimeLimitControl.open, true);
// Any other pause keeps its title, and offers the conversation before the automatic-retry note.
render({...author, runId: 'r3', pauseCause: 'error', error: 'Connection lost.', autoResumeAt: 2000000000,
  conversation: {available: true, busy: false, items: []}});
assert.equal(ui.runTitle.textContent, 'Ready to resume');
assert.match(ui.runDescription.textContent,
  /^Connection lost\. You can also ask the author questions below\. Automatic retry at .+\. Choose Keep paused/);
render({...author, runId: 'r3'});
assert.equal(ui.runDescription.textContent, 'Resume reopens the saved author conversation in this same run folder. '
  + 'You can update your CLI login or API model settings before resuming.');
""")

    def test_the_limit_control_follows_the_phase_and_keeps_a_limit_typed_but_not_set(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
for (const phase of ['reviewing', 'prepared', 'running', 'pausing', 'paused']) {
  render({...author, phase, stage: 'critic', activeNode: 'critic'});
  assert.equal(ui.authorTimeLimitControl.hidden, false, phase);
  assert.equal(ui.setAuthorTimeLimit.disabled, false, phase);
}
for (const phase of ['input', 'reviewed', 'stopping', 'done']) {
  render({...author, phase, review: {statement: 'S', notes: ''}});
  assert.equal(ui.authorTimeLimitControl.hidden, true, phase);
  assert.equal(ui.setAuthorTimeLimit.disabled, true, phase);
}
render({...author, workflowStages: {review: false, author: false, critic: true, writing: true}});
assert.equal(ui.authorTimeLimitControl.hidden, true);
render({...author, readOnly: true});
assert.equal(ui.authorTimeLimitControl.hidden, true);
// The field follows the job until the user types a limit; a typed limit stays for Resume to set.
render(author);
assert.equal(ui.authorLimitHours.value, '2');
render({...author, thinkingHours: 2.5});
assert.equal(ui.authorLimitHours.value, '2.5');
ui.authorLimitHours.value = '4';
render({...author, thinkingHours: 2.5});
assert.equal(ui.authorLimitHours.value, '4');
render({...author, thinkingHours: 4});
render({...author, thinkingHours: 6});
assert.equal(ui.authorLimitHours.value, '6', 'once set, the field follows the job again');
ui.authorLimitHours.value = '9';
render({...author, runId: 'another', thinkingHours: 1});
assert.equal(ui.authorLimitHours.value, '1', 'another job shows its own limit');
// While the field is focused, nothing is written into it.
ui.authorLimitHours.value = '7';
document.activeElement = ui.authorLimitHours;
render({...author, runId: 'another', thinkingHours: 1});
assert.equal(ui.authorLimitHours.value, '7');
document.activeElement = null;
""")

    def test_the_paused_box_asks_the_author_and_waits_for_one_reply(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const running = {...author, phase: 'running'};
render(running);
assert.equal(ui.authorSteerControl.hidden, false);
assert.equal(ui.requestSummary.hidden, true);
assert.equal(ui.stopReply.hidden, true);
assert.equal(ui.sendAuthorSteer.disabled, false);
// Nothing changes the running box's own texts.
assert.equal(ui.authorSteerLabel.textContent, '');
const available = {available: true, busy: false, readOnly: true, reason: '', items: []};
render({...author, conversation: available});
assert.equal(ui.authorSteerControl.hidden, false);
assert.equal(ui.authorSteerLabel.textContent, 'Ask the paused author');
assert.equal(ui.authorSteerInstruction.placeholder, 'For example: which step in the summary is the main obstacle?');
assert.equal(ui.sendAuthorSteerText.textContent, 'Send');
assert.equal(ui.sendAuthorSteer.disabled, false);
assert.equal(ui.requestSummary.hidden, false);
assert.equal(ui.stopReply.hidden, true);
assert.equal(ui.resume.disabled, false);
assert.equal(polls.at(-1), 2500);
// While a reply is written: Send and Summarize wait, Stop reply and a faster poll; Resume waits too.
render({...author, conversation: {...available, busy: true}});
assert.equal(ui.sendAuthorSteer.disabled, true);
assert.equal(ui.requestSummary.hidden, true);
assert.equal(ui.stopReply.hidden, false);
assert.equal(ui.resume.disabled, true);
assert.equal(ui.resume.title, 'The author is replying. Wait for the reply or stop it.');
assert.equal(polls.at(-1), 700);
// Back to running: the running texts again.
render(running);
assert.equal(ui.authorSteerLabel.textContent, 'Guide the running proof author');
assert.equal(ui.authorSteerInstruction.placeholder,
  'For example: try a potential-function argument before the case analysis.');
assert.equal(ui.sendAuthorSteerText.textContent, 'Send instruction now');
assert.equal(ui.resume.title, '');
// Unavailable, read-only, or paused at the critic: no box.
for (const next of [{...author, conversation: {...available, available: false, reason: 'No thread.'}},
    {...author, readOnly: true, conversation: available}, {...author, conversation: undefined},
    {...author, phase: 'pausing', conversation: available}]) {
  render(next);
  assert.equal(ui.authorSteerControl.hidden, true, JSON.stringify(next));
}
""")

    def test_conversation_items_render_by_status_and_only_when_they_change(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const at = (hour, minute) => new Date(2026, 9, 27, hour, minute).getTime() / 1000;
const clock = (hour, minute) => new Date(2026, 9, 27, hour, minute).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
const items = [
  {id: 'c_1', kind: 'summary', origin: 'time-limit', status: 'answered', createdAt: at(9, 5), finishedAt: at(9, 9),
   text: '', reply: 'Proved Lemma 1.\nOpen: the lower bound.'},
  {id: 'c_2', kind: 'message', origin: 'user', status: 'answered', createdAt: at(9, 12), finishedAt: at(9, 14),
   text: 'Which step is hardest?', reply: 'Step 3.'},
  {id: 'c_3', kind: 'message', origin: 'user', status: 'failed', createdAt: at(9, 20), text: 'Again?',
   error: 'The saved author conversation is no longer available.'},
  {id: 'c_4', kind: 'message', origin: 'user', status: 'stopped', createdAt: at(9, 21), text: 'Stop me.'},
  {id: 'c_5', kind: 'summary', origin: 'user', status: 'queued', createdAt: at(9, 22),
   error: 'Could not start the reply: busy. It is tried again in 1 minute.'},
];
const conversation = {available: true, busy: true, readOnly: true, reason: '', items};
render({...author, conversation});
assert.equal(ui.authorConversation.hidden, false);
assert.equal(ui.authorConversationNote.textContent, 'The author answers in its saved conversation without changing '
  + 'files, and remembers the exchange when the run resumes. Replies do not use the time limit.');
const rows = ui.authorConversationList.children;
assert.deepEqual(rows.map(row => row.dataset.status), ['answered', 'answered', 'failed', 'stopped', 'queued']);
assert.deepEqual(rows[0].children.map(node => [node.className, node.textContent]), [
  ['conversation-meta', `Progress summary at the time limit · ${clock(9, 5)}`],
  ['conversation-reply', 'Proved Lemma 1.\nOpen: the lower bound.']]);
assert.deepEqual(rows[1].children.map(node => [node.className, node.textContent]), [
  ['conversation-meta', `You · ${clock(9, 12)}`], ['conversation-text', 'Which step is hardest?'],
  ['conversation-meta', `Author · ${clock(9, 14)}`], ['conversation-reply', 'Step 3.']]);
assert.equal(rows[2].children.at(-1).textContent, 'No reply: The saved author conversation is no longer available.');
assert.equal(rows[3].children.at(-1).textContent, 'Reply stopped.');
assert.equal(rows[4].children[0].textContent, `Progress summary · ${clock(9, 22)} · requested`);
assert.equal(rows[4].children.at(-1).textContent,
  'Waiting to start: Could not start the reply: busy. It is tried again in 1 minute.');
assert.equal(ui.authorConversationList.scrollTop, ui.authorConversationList.scrollHeight);
// The same items again: nothing is rebuilt.
const before = created;
render({...author, conversation: JSON.parse(JSON.stringify(conversation))});
assert.equal(created, before);
// A reply being written, then retried after a model-call failure; a plain queued item.
const retryAt = at(9, 31);
render({...author, conversation: {...conversation, items: [
  {...items[1], id: 'c_6', status: 'running', reply: ''},
  {...items[0], id: 'c_7', status: 'running', reply: '', origin: 'user'},
  {...items[1], id: 'c_8', status: 'running', reply: '',
   retry: {text: 'Model call failed (model at capacity) — retrying in 1 min (attempt 2).', retryAt}},
  {...items[1], id: 'c_9', status: 'queued', reply: ''}]}});
assert.deepEqual(ui.authorConversationList.children.map(row => row.children.at(-1).textContent), [
  'The author is replying…', 'The author is writing the summary…',
  `Model call failed (model at capacity) — retrying in 1 min (attempt 2). Next attempt at ${new Date(retryAt * 1000).toLocaleTimeString()}.`,
  'Waiting to start…']);
// The newest summary of a pause whose critic report the author has not seen says so.
render({...author, criticFeedbackPending: true, conversation: {...conversation, busy: false,
  items: [items[0], {...items[0], id: 'c_10', createdAt: at(9, 40)}, items[1]]}});
const notes = ui.authorConversationList.children.map(row => row.children.at(-1).textContent);
assert.equal(notes[1], "The author has not seen the critic's latest report yet; it receives it when the run resumes.");
assert.notEqual(notes[0], notes[1]);
// A web-cutoff run answers with the author's own audited launch: no claim about files.
render({...author, conversation: {...conversation, readOnly: false}});
assert.equal(ui.authorConversationNote.textContent, 'The author answers in its saved conversation, and remembers '
  + 'the exchange when the run resumes. Replies do not use the time limit.');
// Unavailable: the reason. Running with items: the live-instruction note. Done: no note.
render({...author, conversation: {...conversation, available: false, reason: 'The author has not started its conversation yet.'}});
assert.equal(ui.authorConversationNote.textContent, 'The author has not started its conversation yet.');
render({...author, phase: 'running', conversation: {...conversation, available: false}});
assert.equal(ui.authorConversationNote.textContent,
  'Live instructions reach the author at its next step; it does not reply here.');
render({...author, phase: 'done', conversation: {...conversation, available: false}});
assert.equal(ui.authorConversation.hidden, false);
assert.equal(ui.authorConversationNote.hidden, true);
// Without items and without the paused input the card is hidden; paused and available, it shows empty.
render({...author, phase: 'done', conversation: {available: false, items: []}});
assert.equal(ui.authorConversation.hidden, true);
render({...author, conversation: {available: true, busy: false, items: []}});
assert.equal(ui.authorConversation.hidden, false);
assert.equal(ui.authorConversationList.children.length, 0);
// A saved conversation too large to show is named, in any phase.
const notice = 'The saved conversation is too large to show here (17.2 MB). See /runs/.controller/job/conversation.json.';
for (const phase of ['paused', 'done']) {
  render({...author, phase, readOnly: true, conversation: {available: false, reason: 'Saved batch.', items: [], notice}});
  assert.equal(ui.authorConversation.hidden, false);
  assert.equal(ui.authorConversationNote.textContent, notice);
  assert.equal(ui.authorConversationNote.hidden, false);
  assert.equal(ui.authorConversationList.children.length, 0);
}
""")

    def test_no_card_or_row_is_built_without_conversation_or_related_works(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
for (const next of [{phase: 'running', stage: 'solve', activeNode: 'author'}, {...author}, {phase: 'done'},
    {phase: 'prepared'}, {...author, pauseCause: 'user'}]) {
  render(next);
  assert.equal(ui.authorConversation.hidden, true);
  assert.equal(ui.relatedWorksPanel.hidden, true);
}
assert.equal(created, 0);
assert.equal(ui.authorConversationList.children.length, 0);
""")

    def test_resume_sets_a_typed_limit_first_and_refuses_a_used_up_one(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const paused = {...author, pauseCause: 'time-limit', timeLimitExhausted: true,
  conversation: {available: true, busy: false, items: []}};
render(paused);
// A used-up limit: no request; the control opens on its field with the reason.
ui.authorTimeLimitControl.open = false;
await ui.resume.onclick();
assert.deepEqual(acted, []);
assert.equal(ui.authorTimeLimitControl.open, true);
assert.equal(ui.authorLimitHours.focused, true);
assert.equal(ui.notice.hidden, false);
assert.equal(ui.notice.textContent,
  'The time limit is used up (2h 0m of 2 hours). Raise it, or set 0 for no limit, then choose Continue.');
// A typed limit is set first; Resume follows once the job has it.
ui.authorLimitHours.value = '3';
afterAct = (path, body) => { if (path === '/set-author-time-limit') render({...paused, thinkingHours: body.hours,
  timeLimitExhausted: false}); };
await ui.resume.onclick();
assert.deepEqual(acted, [{path: '/set-author-time-limit', body: {hours: 3}}, {path: '/resume', body: undefined}]);
// A refused limit (or a job that is no longer paused) is not followed by Resume.
acted.length = 0;
render(paused);
ui.authorLimitHours.value = '5';
actResult = false; afterAct = () => {};
await ui.resume.onclick();
assert.deepEqual(acted, [{path: '/set-author-time-limit', body: {hours: 5}}]);
actResult = true; acted.length = 0;
afterAct = (path, body) => { if (path === '/set-author-time-limit') render({...paused, phase: 'running', thinkingHours: 5}); };
render(paused);
ui.authorLimitHours.value = '5';
await ui.resume.onclick();
assert.deepEqual(acted.map(item => item.path), ['/set-author-time-limit']);
// No change in the field: Resume at once, synchronously.
acted.length = 0; afterAct = () => {};
ui.authorLimitHours.value = '2';
render({...author, pauseCause: 'user'});
const pending = ui.resume.onclick();
assert.deepEqual(acted, [{path: '/resume', body: undefined}]);
await pending;
// A field that does not hold a limit (blank, negative) is not a change.
for (const value of ['', '-1', 'x']) {
  acted.length = 0;
  ui.authorLimitHours.value = value;
  await ui.resume.onclick();
  assert.deepEqual(acted.map(item => item.path), ['/resume'], value);
}
// A prepared run starts as before.
acted.length = 0;
render({...author, phase: 'prepared'});
await ui.resume.onclick();
assert.deepEqual(acted.map(item => item.path), ['/continue-stopped']);
""")

    def test_summarize_and_stop_reply_post_their_routes(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const items = [{id: 'c_1', kind: 'summary', status: 'answered'}, {id: 'c_2', kind: 'message', status: 'running'}];
render({...author, conversation: {available: true, busy: true, items}});
await ui.requestSummary.onclick();
await ui.stopReply.onclick();
assert.deepEqual(acted, [{path: '/author-summary', body: {}}, {path: '/stop-author-reply', body: {id: 'c_2'}}]);
""")

    def test_the_run_header_says_used_up_and_author_replying(self):
        self.run_app(OVERVIEW, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let stateReceivedAt = Date.now();
const document = {createElement: tag => new Element(tag)};
const show = (element, visible) => { element.hidden = !visible; };
const clockText = () => '', describe = () => null;
let state = {runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', thinkingHours: 2,
  activeElapsedSeconds: 7230, processAlive: false, workerActive: false, timeLimitExhausted: true,
  workflow: {nodes: {author: {label: 'Proof author'}}}, trace: [], statement: 'S', draft: 'S'};
""", r"""
renderRunOverview();
assert.equal(ui.runBudget.textContent, '2h · used up');
assert.equal(ui.runHealth.textContent, 'Paused · saved conversation');
state.processAlive = true;
renderRunOverview();
assert.equal(ui.runHealth.textContent, 'Paused · author replying');
state = {...state, timeLimitExhausted: false, activeElapsedSeconds: 3600, processAlive: false};
renderRunOverview();
assert.equal(ui.runBudget.textContent, '2h · 1h 0m left');
assert.equal(ui.runHealth.textContent, 'Paused · saved conversation');
state = {...state, phase: 'running', processAlive: true};
renderRunOverview();
assert.equal(ui.runHealth.textContent, 'Running');
""")

    def test_the_limit_hint_and_the_values_set_limit_reads(self):
        self.run_app(CLOCK, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let state = {phase: 'running', traceVersion: 0};
let active = 7230;
const durationText = seconds => Math.floor(seconds / 3600) + 'h ' + Math.floor(seconds % 3600 / 60) + 'm';
const runElapsedSeconds = () => active, renderRunOverview = () => {};
""", r"""
ui.authorLimitHours.value = '3';
renderClock();
assert.equal(ui.authorLimitHint.textContent, 'Active time so far: 2h 0m.');
assert.deepEqual({...ui.authorLimitHours.dataset}, {elapsed: '7230', running: '1'});
ui.authorLimitHours.value = '2';
renderAuthorLimitHint();
assert.equal(ui.authorLimitHint.textContent,
  'Active time so far: 2h 0m. A limit at or below this stops the author at its next step.');
state.phase = 'pausing';
renderAuthorLimitHint();
assert.match(ui.authorLimitHint.textContent, /stops the author at its next step\.$/);
assert.equal(ui.authorLimitHours.dataset.running, '');
state.phase = 'paused';
renderAuthorLimitHint();
assert.equal(ui.authorLimitHint.textContent,
  'Active time so far: 2h 0m. Set more than this, or 0 for no limit, to resume.');
for (const value of ['0', '', '2.5']) {
  ui.authorLimitHours.value = value;
  renderAuthorLimitHint();
  assert.equal(ui.authorLimitHint.textContent, 'Active time so far: 2h 0m.', value);
}
""")

    def test_set_limit_asks_before_stopping_a_running_author(self):
        self.run_app(LIMIT, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const show = (element, visible) => { element.hidden = !visible; };
const durationText = seconds => Math.floor(seconds / 3600) + 'h ' + Math.floor(seconds % 3600 / 60) + 'm';
const acted = [], questions = [];
let answer = false;
const act = (path, body) => acted.push({path, body});
const confirm = question => { questions.push(question); return answer; };
""", r"""
Object.assign(ui.authorLimitHours.dataset, {elapsed: '7230', running: '1'});
ui.authorLimitHours.value = '2';
ui.setAuthorTimeLimit.onclick();
assert.deepEqual(questions, ['Set the time limit to 2 hours? That is at or below the 2h 0m of active work so far: '
  + 'the proof author stops at its next step and writes a progress summary. The critic and LaTeX steps still finish.']);
assert.deepEqual(acted, []);
answer = true;
ui.authorLimitHours.value = '1';
ui.setAuthorTimeLimit.onclick();
assert.match(questions[1], /^Set the time limit to 1 hour\? /);
// The server writes limits the same way (tests/test_time_limit_conversation.py checks hours_text).
assert.deepEqual([1, 1.5, 0.25, 2, 1000, 1.004].map(limitHoursText),
  ['1 hour', '1.5 hours', '0.25 hours', '2 hours', '1,000 hours', '1 hour']);
assert.deepEqual(acted, [{path: '/set-author-time-limit', body: {hours: 1}}]);
// Above the active time, 0, or a paused author: no question.
for (const [value, running] of [['3', '1'], ['0', '1'], ['1', '']]) {
  ui.authorLimitHours.value = value;
  ui.authorLimitHours.dataset.running = running;
  ui.setAuthorTimeLimit.onclick();
}
assert.equal(questions.length, 2);
assert.deepEqual(acted.map(item => item.body.hours), [1, 3, 0, 1]);
""")

    def test_a_paused_message_is_sent_to_ask_author_and_then_cleared(self):
        self.run_app(SEND, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
ui.authorSteerInstruction.addEventListener = () => {};
ui.authorSteerInstruction.dataset.maxChars = '20';
const show = (element, visible) => { element.hidden = !visible; };
let authorSteerPaused = true, result = true;
const acted = [];
const act = async (path, body) => { acted.push({path, body}); return result; };
""", r"""
ui.authorSteerInstruction.value = '   ';
ui.sendAuthorSteer.onclick();
assert.equal(ui.notice.textContent, 'Enter a message for the author.');
ui.authorSteerInstruction.value = 'x'.repeat(25);
ui.sendAuthorSteer.onclick();
assert.equal(ui.notice.textContent, 'Keep the message at or below 20 characters (it has 25); shorten it or send it in parts.');
assert.deepEqual(acted, []);
// Refused by the server: the text stays to send again.
result = false;
ui.authorSteerInstruction.value = ' Which lemma? ';
ui.sendAuthorSteer.onclick();
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(acted, [{path: '/ask-author', body: {text: 'Which lemma?'}}]);
assert.equal(ui.authorSteerInstruction.value, ' Which lemma? ');
result = true;
ui.sendAuthorSteer.onclick();
await new Promise(resolve => setImmediate(resolve));
assert.equal(ui.authorSteerInstruction.value, '');
assert.equal(ui.authorSteerCount.textContent, '');
// The running box still sends live instructions, as before.
authorSteerPaused = false;
ui.authorSteerInstruction.value = 'Use the lemma.';
ui.sendAuthorSteer.onclick();
assert.deepEqual(acted.at(-1), {path: '/steer-author', body: {instruction: 'Use the lemma.'}});
assert.equal(ui.authorSteerInstruction.value, 'Use the lemma.');
""")


CLOCK = [["function renderClock()", "// Merge incremental polling state"]]
SEND = [["ui.sendAuthorSteer.onclick =", "ui.stop.onclick ="]]
LIMIT = [["function limitHoursText(", "// The now-card at a time-limit pause"],
         ["function hoursText(", "// A duration in words as the runner writes it"],
         ["ui.setAuthorTimeLimit.onclick =", "ui.sendAuthorSteer.onclick ="]]
OVERVIEW = [
    ["function retrievalConfiguration(", "function renderJobs("],
    ["function completedRunLabel(", "function renderWorkflow()"],
]


if __name__ == "__main__":
    unittest.main()
