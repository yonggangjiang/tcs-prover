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
  conversation: {available: true, busy: true, readOnly: true, reason: '', items: [summary], mode: 'busy'}};
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
  conversation: {available: true, busy: false, items: [], mode: 'paused'}});
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

    def test_send_and_the_summary_button_follow_the_servers_chat_mode(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const chat = (mode, summary, note = 'Note.', summaryNote = '') => ({available: mode === 'paused', busy: mode === 'busy',
  readOnly: true, reason: '', items: [], mode, note, summary, summaryNote});
const running = {...author, phase: 'running'};
// The author works: Send queues a live instruction; the summary button pauses first.
render({...running, conversation: chat('author', 'pause', 'Your message reaches the author at its next step.')});
assert.equal(ui.authorConversation.hidden, false);
assert.equal(ui.authorSteerControl.hidden, false);
assert.equal(ui.authorConversationNote.textContent, 'Your message reaches the author at its next step.');
assert.equal(ui.sendAuthorSteer.disabled, false);
assert.equal(ui.sendAuthorSteer.title, '');
assert.equal(ui.authorSteerInstruction.placeholder, 'Ask a question, or tell the author what to try next.');
assert.deepEqual([ui.requestSummary.hidden, ui.requestSummary.disabled, ui.requestSummary.textContent],
  [false, false, 'Pause and summarize']);
assert.equal(ui.requestSummary.title,
  'Pauses the author like Pause, then it writes a progress summary here. Resume continues the work.');
assert.equal(ui.stopReply.hidden, true);
await ui.requestSummary.onclick();
assert.deepEqual(acted.at(-1), {path: '/author-summary', body: {}});
// Paused at the author: a reply in its saved conversation, and a summary at once.
render({...author, conversation: chat('paused', 'now')});
assert.equal(ui.authorSteerInstruction.placeholder, 'Ask the author a question.');
// A job stopped by you or at its time limit is in the same mode, and the box never calls it paused.
for (const pauseCause of ['user', 'stopped', 'time-limit']) {
  render({...author, pauseCause, conversation: chat('paused', 'now')});
  assert.equal(ui.authorSteerInstruction.placeholder, 'Ask the author a question.', pauseCause);
  assert.doesNotMatch(ui.authorSteerInstruction.placeholder, /paused/, pauseCause);
}
assert.equal(ui.sendAuthorSteer.disabled, false);
assert.deepEqual([ui.requestSummary.disabled, ui.requestSummary.textContent], [false, 'Summarize progress']);
assert.equal(ui.resume.disabled, false);
assert.equal(polls.at(-1), 2500);
// While a reply is written: Send and Summarize wait, Stop reply shows, Resume waits, the poll is faster.
render({...author, conversation: chat('busy', 'off', 'The author is replying. Wait for the reply or stop it.',
  'The author is replying. Wait for the reply or stop it.')});
assert.equal(ui.sendAuthorSteer.disabled, true);
assert.equal(ui.sendAuthorSteer.title, 'The author is replying. Wait for the reply or stop it.');
assert.deepEqual([ui.requestSummary.hidden, ui.requestSummary.disabled], [false, true]);
assert.equal(ui.requestSummary.title, 'The author is replying. Wait for the reply or stop it.');
assert.equal(ui.stopReply.hidden, false);
assert.equal(ui.resume.disabled, true);
assert.equal(ui.resume.title, 'The author is replying. Wait for the reply or stop it.');
assert.equal(polls.at(-1), 700);
// Pausing: Send waits for the pause; the summary is written once paused.
render({...author, phase: 'pausing', conversation: chat('off', 'queue', 'The author is pausing.')});
assert.equal(ui.sendAuthorSteer.disabled, true);
assert.equal(ui.sendAuthorSteer.title, 'The author is pausing.');
assert.deepEqual([ui.requestSummary.disabled, ui.requestSummary.title],
  [false, 'The author writes a progress summary here once it has paused.']);
// Another step works: Send is off with the reason, and what was typed stays.
ui.authorSteerInstruction.value = 'Typed meanwhile.';
render({...running, stage: 'critic', activeNode: 'critic', conversation: chat('off', 'off', 'The critic is reviewing.',
  'The critic is reviewing.')});
assert.equal(ui.authorSteerControl.hidden, false);
assert.equal(ui.sendAuthorSteer.disabled, true);
assert.equal(ui.authorConversationNote.textContent, 'The critic is reviewing.');
assert.equal(ui.authorSteerInstruction.value, 'Typed meanwhile.');
// A finished, read-only or elsewhere-running job has no box.
for (const next of [{...author, phase: 'done', conversation: chat('off', 'off')},
    {...author, readOnly: true, conversation: chat('off', 'off')},
    {...author, runningElsewhere: 'Running in another process.', conversation: chat('off', 'off')}]) {
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
const note = 'The author answers in its saved conversation without changing files, and remembers the exchange when '
  + 'the work continues. Replies do not use the time limit.';
const conversation = {available: true, busy: true, readOnly: true, reason: '', items, mode: 'busy', note};
render({...author, conversation});
assert.equal(ui.authorConversation.hidden, false);
// The note is the server's (what Send does now, or why it is off).
assert.equal(ui.authorConversationNote.textContent, note);
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
// A summary asked for with Pause and summarize waits for the pause.
render({...author, phase: 'pausing', conversation: {...conversation, items: [{...items[4], error: ''}]}});
assert.equal(ui.authorConversationList.children[0].children.at(-1).textContent,
  'The author is pausing; it writes the summary once it has paused.');
// The newest summary of a pause whose critic report the author has not seen says so.
render({...author, criticFeedbackPending: true, conversation: {...conversation, busy: false,
  items: [items[0], {...items[0], id: 'c_10', createdAt: at(9, 40)}, items[1]]}});
const notes = ui.authorConversationList.children.map(row => row.children.at(-1).textContent);
assert.equal(notes[1], "The author has not seen the critic's latest report yet; it receives it when the run resumes.");
assert.notEqual(notes[0], notes[1]);
// A finished job keeps its history, without the box; with none, the card is hidden.
render({...author, phase: 'done', conversation: {...conversation, mode: 'off',
  note: 'This job has finished, so the author can no longer be asked.'}});
assert.equal(ui.authorConversation.hidden, false);
assert.equal(ui.authorSteerControl.hidden, true);
render({...author, phase: 'done', conversation: {available: false, items: [], mode: 'off'}});
assert.equal(ui.authorConversation.hidden, true);
// A saved conversation too large to show is named, in any phase, as the chat's only row.
const notice = 'The saved conversation is too large to show here (17.2 MB). See /runs/.controller/job/conversation.json.';
for (const phase of ['paused', 'done']) {
  render({...author, phase, readOnly: true, conversation: {available: false, reason: 'Saved batch.', items: [], notice}});
  assert.equal(ui.authorConversation.hidden, false);
  assert.deepEqual(ui.authorConversationList.children.map(row => [row.dataset.status, row.children[0].textContent]),
    [['notice', notice]]);
}
""")

    def test_chat_messages_answers_and_questions_render_as_text_by_status(self):
        self.run_app(RENDER, RENDER_SETUP + r"""
let fetched = '';
const fetch = async (path) => { fetched = path; return {ok: true, blob: async () => 'bytes'}; };
const URL = {createObjectURL: () => 'blob:x', revokeObjectURL() {}};
""", r"""
const at = (hour, minute) => new Date(2026, 9, 27, hour, minute).getTime() / 1000;
const iso = (hour, minute) => new Date(2026, 9, 27, hour, minute).toISOString();
const clock = (hour, minute) => new Date(2026, 9, 27, hour, minute).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
const message = (id, fields) => ({id, kind: 'instruction', origin: 'user', status: 'queued', createdAt: at(10, 0),
  text: 'Why <b>this</b> bound?', reply: '', error: '', steerId: `s_${id}`, answerFile: `chat/answer-${id}.md`,
  deliveredAt: '', ...fields});
const question = (id, fields) => ({id, kind: 'question', origin: 'author', status: 'open', createdAt: at(10, 8),
  text: '', reply: '', error: '', questionId: `call_${id}`, questions: [{title: 'Which norm?', options: ['L1', 'L2']}],
  ...fields});
const items = [
  message('c_a', {}),
  message('c_b', {status: 'delivered', deliveredAt: iso(10, 2)}),
  message('c_c', {status: 'answered', deliveredAt: iso(10, 2), finishedAt: iso(10, 5), answerUpdatedAt: iso(10, 7),
    reply: '<img src=x onerror=alert(1)> Lemma 2 is open.', answerShortened: true, answerBytes: 5000000,
    answerPath: '/runs/r1/chat/answer-c_c.md'}),
  question('c_d', {status: 'answered', finishedAt: iso(10, 9), deliveredAt: iso(10, 10),
    reply: 'Answer to your question "Which norm?": L2', steerId: 's_d'}),
  question('c_e', {status: 'dismissed'}),
  question('c_f', {status: 'closed', error: "The author's question closed: the proof author is no longer running."}),
  question('c_g', {}),
];
const chat = {mode: 'author', note: 'Your message reaches the author at its next step.', summary: 'pause', items};
render({...author, phase: 'running', conversation: chat});
const rows = ui.authorConversationList.children;
const text = row => row.children.map(node => [node.className, node.textContent]);
assert.deepEqual(text(rows[0]), [['conversation-meta', `You · ${clock(10, 0)}`],
  ['conversation-text', 'Why <b>this</b> bound?'],
  ['conversation-status', 'Waiting for the author to take it in at its next step.']]);
assert.deepEqual(text(rows[1]).at(-1), ['conversation-status', `Delivered to the author · ${clock(10, 2)}`]);
// The answer is the author's untrusted output: set as text, never as markup; a long one says so.
assert.deepEqual(text(rows[2]).slice(2), [
  ['conversation-meta', `Author · ${clock(10, 5)} · updated ${clock(10, 7)}`],
  ['conversation-reply', '<img src=x onerror=alert(1)> Lemma 2 is open.'],
  ['conversation-status', "Shortened: the author's answer is 4.8 MB long, so the chat shows its first 100,000 "
    + 'characters. The whole answer is in /runs/r1/chat/answer-c_c.md.'],
  ['ghost compact conversation-download', 'Download the whole answer']]);
assert.equal(rows[2].children[3].tagName, 'DIV');
await rows[2].children.at(-1).onclick();
assert.equal(fetched, '/chat-answer?job=job');
// The author's questions: asked, how they ended, and what was sent.
assert.deepEqual(text(rows[3]), [['conversation-meta', `The author asks · ${clock(10, 8)}`],
  ['conversation-question', 'Which norm?'], ['conversation-options', 'Options: L1 · L2'],
  ['conversation-meta', `You answered · ${clock(10, 9)}`],
  ['conversation-text', 'Answer to your question "Which norm?": L2'],
  ['conversation-status', `Delivered to the author · ${clock(10, 10)}`]]);
assert.deepEqual(text(rows[4]).at(-1), ['conversation-status', 'Dismissed: nothing was sent to the author.']);
assert.deepEqual(text(rows[5]).at(-1),
  ['conversation-status', "The author's question closed: the proof author is no longer running."]);
assert.deepEqual(text(rows[6]).at(-1), ['conversation-status', 'Open: you can answer when the author works again.']);
assert.deepEqual(rows.map(row => row.dataset.kind),
  ['instruction', 'instruction', 'instruction', 'question', 'question', 'question', 'question']);
// A message the author has not taken in says when it will, wherever the job is.
const queued = {...chat, items: [items[0]]};
for (const [where, words] of [
    [{...author}, 'Not delivered yet: the author receives it when it works again.'],
    [{...author, phase: 'running', stage: 'critic', activeNode: 'critic'},
     'Not delivered yet: the author receives it at its next turn, if it works again in this run.'],
    [{...author, phase: 'done'}, 'Not delivered: the job ended before the author received it.']]) {
  render({...where, conversation: queued});
  assert.equal(ui.authorConversationList.children[0].children.at(-1).textContent, words, JSON.stringify(where));
}
// A saved batch offers no download.
render({...author, phase: 'done', readOnly: true, conversation: {...chat, items: [items[2]]}});
assert.notEqual(ui.authorConversationList.children[0].children.at(-1).tagName, 'BUTTON');
""")
        source = APP.read_text(encoding="utf-8")
        chat = source[source.index("function conversationEntry("):source.index("// The run page's related works")]
        self.assertNotIn("innerHTML", chat)
        self.assertNotIn("insertAdjacentHTML", chat)

    def test_stop_says_stop_now_while_pausing_and_waits_for_its_request(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const running = {...author, phase: 'running'};
render(running);
assert.deepEqual([ui.stop.hidden, ui.stopText.textContent], [false, 'Stop']);
assert.equal(ui.stop.title, 'Stop the job: the author ends its current step (up to 30 seconds), then the job stays '
  + 'stopped until you continue it.');
render({...running, stage: 'critic', activeNode: 'critic'});
assert.equal(ui.stop.title, 'Stop the job at once; Continue runs this step again later.');
render({...author, phase: 'pausing', pauseCause: 'stopped'});
assert.deepEqual([ui.stop.hidden, ui.stopText.textContent], [false, 'Stop now']);
assert.equal(ui.stop.title, 'Stop at once instead of waiting; the job still continues where it stopped.');
// One request at a time: a double click never turns the first Stop into the second.
render(running);
const pending = ui.stop.onclick();
assert.equal(ui.stop.disabled, true);
await ui.stop.onclick();
await pending;
assert.deepEqual(acted.map(entry => entry.path), ['/stop']);
assert.equal(ui.stop.disabled, false);
""")

    def test_a_job_you_stopped_says_stopped_and_offers_continue(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const chat = {available: true, busy: false, items: [], mode: 'paused', note: '', summary: 'now'};
render({...author, pauseCause: 'stopped', conversation: chat});
assert.deepEqual([ui.globalStatus.textContent, ui.runLabel.textContent, ui.runTitle.textContent],
  ['Stopped', 'STOPPED', 'Stopped']);
assert.equal(ui.runDescription.textContent, 'You stopped the proof author. Its saved conversation is kept; Continue '
  + 'picks the work up in this same job, and the time already used still counts. You can still talk to the author '
  + 'or ask for a progress summary below.');
assert.deepEqual([ui.resume.hidden, ui.resume.textContent], [false, 'Continue']);
render({...author, pauseCause: 'stopped', timeLimitExhausted: true, criticFeedbackPending: true,
  conversation: {...chat, mode: 'off'}});
assert.equal(ui.runDescription.textContent, 'You stopped the proof author. Its saved conversation is kept; Continue '
  + 'picks the work up in this same job, and the time already used still counts. The time limit is used up: raise '
  + 'it, or set 0 for no limit, before you continue. The critic rejected the latest proof; the author receives its '
  + 'report when you continue.');
render({...author, pauseCause: 'stopped', stage: 'final', activeNode: 'latex_editor'});
assert.equal(ui.runDescription.textContent,
  'You stopped the LaTeX editor. Continue runs it again in this same job, from the same saved input.');
render({...author, phase: 'pausing', pauseCause: 'stopped'});
assert.deepEqual([ui.globalStatus.textContent, ui.runLabel.textContent, ui.runTitle.textContent],
  ['Stopping', 'STOPPING', 'Stopping…']);
assert.equal(ui.runDescription.textContent, 'Stopping the proof author: it may finish its current step for up to 30 '
  + 'seconds, then its conversation is saved. Stop now stops it at once.');
render({...author, phase: 'input', manuallyStopped: true});
assert.equal(ui.globalStatus.textContent, 'Stopped');
""")

    def test_a_stopped_statement_review_reads_stopped_with_a_neutral_note(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
// A revision you stopped is back at the earlier review (the server's REVISION_STOPPED_TEXT).
const revision = {runId: 'rev', phase: 'reviewed', stage: 'review', activeNode: 'statement_reviewer',
  manuallyStopped: true, stoppedStage: 'review', review: {statement: 'S.', notes: ''}, reviewFeedback: 'F.',
  error: 'You stopped the statement review. Retry with feedback runs it again in this job.',
  workflowStages: author.workflowStages};
render(revision);
assert.equal(ui.review.hidden, false);
assert.equal(ui.globalStatus.textContent, 'Stopped');
// A stop is no failure: a neutral note says how it runs again, never the error banner.
assert.equal(ui.notice.hidden, true);
assert.deepEqual([ui.stoppedNote.hidden, ui.stoppedNote.textContent], [false, revision.error]);
// Retry with feedback has the feedback again.
assert.equal(ui.feedback.value, 'F.');
// A first review you stopped is back at the composer, whose button is Start workflow (REVIEW_STOPPED_TEXT).
const first = {...revision, runId: 'first', phase: 'input', review: null, reviewFeedback: '',
  error: 'You stopped the statement review. Start workflow runs it again in this job.'};
render(first);
assert.equal(ui.input.hidden, false);
assert.equal(ui.globalStatus.textContent, 'Stopped');
assert.equal(ui.notice.hidden, true);
assert.deepEqual([ui.stoppedNote.hidden, ui.stoppedNote.textContent], [false, first.error]);
// A review that failed is still an error; nothing of the stop is left.
render({...first, runId: 'failed', manuallyStopped: false, error: 'The statement review failed.'});
assert.deepEqual([ui.notice.hidden, ui.notice.textContent], [false, 'The statement review failed.']);
assert.equal(ui.stoppedNote.hidden, true);
render({...revision, runId: 'waiting', manuallyStopped: false, error: ''});
assert.equal(ui.globalStatus.textContent, 'Waiting for approval');
assert.equal(ui.stoppedNote.hidden, true);
""")
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<p id="stoppedNote" class="readonly-note stopped-note" role="status" hidden></p>', page)
        self.assertLess(page.index('id="notice"'), page.index('id="stoppedNote"'))
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".stopped-note::before { top: 15px; left: 19px; width: 8px; height: 8px; border-radius: 1.5px; "
                      'content: "" / ""; }', css)

    def test_no_row_is_built_without_items_or_related_works(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
// A job with an Author stage shows the chat's box while it can still reach its author; a finished one without
// any item, or one without an Author stage, shows none. No row is built without items.
for (const [next, hidden] of [[{phase: 'running', stage: 'solve', activeNode: 'author'}, false], [{...author}, false],
    [{phase: 'prepared'}, false], [{...author, pauseCause: 'user'}, false], [{phase: 'done'}, true],
    [{...author, workflowStages: {review: false, author: false, critic: true, writing: true}}, true]]) {
  render(next);
  assert.equal(ui.authorConversation.hidden, hidden, JSON.stringify(next));
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
// A chat message the working author has not taken in yet is no reply: Stop reply names the reply being written.
const items = [{id: 'c_0', kind: 'instruction', status: 'queued', steerId: 's0', answerFile: 'chat/answer-c_0.md'},
  {id: 'c_1', kind: 'summary', status: 'answered'}, {id: 'c_2', kind: 'message', status: 'running'}];
render({...author, conversation: {available: true, busy: true, items}});
await ui.requestSummary.onclick();
await ui.stopReply.onclick();
assert.deepEqual(acted, [{path: '/author-summary', body: {}}, {path: '/stop-author-reply', body: {id: 'c_2'}}]);
// A summary still waiting to start is the reply too, behind the queued message.
acted.length = 0;
render({...author, conversation: {available: true, busy: true, mode: 'busy', items: [items[0],
  {id: 'c_3', kind: 'summary', status: 'queued'}]}});
await ui.stopReply.onclick();
assert.deepEqual(acted, [{path: '/stop-author-reply', body: {id: 'c_3'}}]);
""")

    def test_a_new_or_updated_answer_is_brought_into_view(self):
        self.run_app(RENDER, RENDER_SETUP + r"""
// Each row sits 100 px below the one before it; the list shows from its scrollTop.
Element.prototype.getBoundingClientRect = function () {
  const list = this.parent;
  return {top: list ? list.children.indexOf(this) * 100 - list.scrollTop : 0};
};
""", r"""
const message = (id, fields = {}) => ({id, kind: 'instruction', origin: 'user', status: 'delivered', createdAt: 1761580800,
  text: `Question ${id}?`, reply: '', error: '', steerId: `s_${id}`, answerFile: `chat/answer-${id}.md`,
  deliveredAt: '2026-10-27T10:00:00Z', ...fields});
const answered = (id, reply, fields = {}) => message(id, {status: 'answered', reply, answerStat: [reply.length, 1],
  finishedAt: '2026-10-27T10:05:00Z', ...fields});
const chat = items => ({mode: 'author', note: '', summary: 'pause', items});
const running = {...author, phase: 'running'};
const list = ui.authorConversationList;
render({...running, conversation: chat([message('c_1'), message('c_2'), message('c_3'), message('c_4')])});
assert.equal(list.scrollTop, list.scrollHeight, 'it opens at its newest item');
// The answer to the second message arrives: its row is brought into view, not the newest item.
render({...running, conversation: chat([message('c_1'), answered('c_2', 'Lemma 3.'), message('c_3'),
  message('c_4')])});
assert.equal(list.scrollTop, 100);
// The same answer again (a poll): nothing moves.
list.scrollTop = 0;
render({...running, conversation: chat([message('c_1'), answered('c_2', 'Lemma 3.'), message('c_3'),
  message('c_4'), message('c_5')])});
assert.equal(list.scrollTop, list.scrollHeight, 'a new message of yours: the newest item');
// The first message gets its answer: the top row is brought into view.
render({...running, conversation: chat([answered('c_1', 'First.'), answered('c_2', 'Lemma 3.'), message('c_3'),
  message('c_4'), message('c_5')])});
assert.equal(list.scrollTop, 0);
// The author changes its answer to the second message: that row is brought into view again.
render({...running, conversation: chat([answered('c_1', 'First.'), answered('c_2', 'Lemma 3 and Lemma 4.',
  {answerUpdatedAt: '2026-10-27T10:09:00Z'}), message('c_3'), message('c_4'), message('c_5')])});
assert.equal(list.scrollTop, 100);
""")

    def test_a_summary_asked_for_while_the_author_stops_never_reads_as_paused(self):
        self.run_app(RENDER, RENDER_SETUP, r"""
const summary = {id: 'c_1', kind: 'summary', origin: 'user', status: 'queued', createdAt: 1761580800, text: '',
  reply: '', error: ''};
const queued = 'A progress summary is already asked for; the author writes it once it has stopped.';
const stopping = {...author, phase: 'pausing', pauseCause: 'stopped'};
const status = () => ui.authorConversationList.children[0].children.at(-1).textContent;
// Stop pressed once: the author ends its current step, and a summary asked for now waits for the stop.
render({...stopping, conversation: {mode: 'off', note: 'The author is stopping.', summary: 'queue', items: []}});
assert.deepEqual([ui.requestSummary.disabled, ui.requestSummary.title],
  [false, 'The author writes a progress summary here once it has stopped.']);
render({...stopping, conversation: {mode: 'off', note: 'The author is stopping.', summary: 'off', summaryNote: queued,
  items: [summary]}});
assert.equal(status(), 'The author is stopping; it writes the summary once it has stopped.');
assert.deepEqual([ui.requestSummary.disabled, ui.requestSummary.title], [true, queued]);
// Pause and summarize reads as a pause, until a Stop pressed meanwhile makes it a stop.
render({...author, runId: 'r2', phase: 'pausing', pauseCause: 'user', conversation: {mode: 'off',
  note: 'The author is pausing.', summary: 'off', items: [summary]}});
assert.equal(status(), 'The author is pausing; it writes the summary once it has paused.');
render({...stopping, runId: 'r2', conversation: {mode: 'off', note: 'The author is stopping.', summary: 'off',
  summaryNote: queued, items: [summary]}});
assert.equal(status(), 'The author is stopping; it writes the summary once it has stopped.');
""")

    def test_the_authors_question_is_scrolled_to_in_the_list_which_stays_put_while_you_answer(self):
        self.run_app(RENDER, RENDER_SETUP + r"""
// Each row sits 100 px below the one before it; the list shows from its scrollTop.
Element.prototype.getBoundingClientRect = function () {
  const list = this.parent;
  return {top: list ? list.children.indexOf(this) * 100 - list.scrollTop : 0};
};
Element.prototype.contains = function (node) {
  for (let parent = node; parent; parent = parent.parent) if (parent === this) return true;
  return false;
};
ui.authorQuestionForm.append(ui.authorQuestionList);
""", r"""
const message = (id, fields = {}) => ({id, kind: 'instruction', origin: 'user', status: 'delivered', createdAt: 1761580800,
  text: `Question ${id}?`, reply: '', error: '', steerId: `s_${id}`, answerFile: `chat/answer-${id}.md`,
  deliveredAt: '2026-10-27T10:00:00Z', ...fields});
const question = {id: 'call_1', askedAt: 1761580900, closesAt: 1761581500, turnEnded: false, turnId: 'turn-1',
  questions: [{title: 'Lists of exactly 3 colours?', options: ['Yes', 'No']}]};
const asked = {id: 'c_q', kind: 'question', origin: 'author', status: 'open', createdAt: 1761580900, text: '',
  reply: '', error: '', questionId: question.id, questions: question.questions};
const chat = items => ({mode: 'author', note: '', summary: 'pause', items});
const running = {...author, phase: 'running'};
const list = ui.authorConversationList;
const before = [message('c_1'), message('c_2')], after = [message('c_3'), message('c_4'), message('c_5')];
render({...running, conversation: chat([...before, ...after])});
assert.equal(list.scrollTop, list.scrollHeight, 'it opens at its newest item');
// The author asks: the list (which keeps its height) scrolls to the question's row, which holds its form, and the
// form is brought into view on the page.
render({...running, authorQuestion: question, conversation: chat([...before, asked, ...after])});
const row = list.children[2];
assert.equal(ui.authorQuestionForm.parent, row);
assert.equal(list.scrollTop, 200);
assert.equal(ui.authorQuestionForm.scrolled, true);
// While you write the answer, a rebuild (your message delivered) leaves the list where it is.
list.scrollTop = 150;
document.activeElement = ui.authorQuestionList.children[0].querySelector('textarea');
render({...running, authorQuestion: question, conversation: chat([...before, asked, ...after,
  message('c_6', {status: 'queued', deliveredAt: ''})])});
assert.equal(ui.authorQuestionForm.parent, list.children[2]);
assert.equal(list.scrollTop, 150);
document.activeElement = null;
// Otherwise a rebuild opens the list at its newest item, as before.
render({...running, authorQuestion: question, conversation: chat([...before, asked, ...after, message('c_6')])});
assert.equal(list.scrollTop, list.scrollHeight);
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

    def test_the_run_header_says_stopped_and_stopping_never_paused(self):
        self.run_app(OVERVIEW, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let stateReceivedAt = Date.now();
const document = {createElement: tag => new Element(tag)};
const show = (element, visible) => { element.hidden = !visible; };
const clockText = () => '', describe = () => null;
const base = {runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', pauseCause: 'stopped',
  thinkingHours: 2, activeElapsedSeconds: 600, processAlive: false, workerActive: false, trace: [], statement: 'S',
  draft: 'S', workflow: {nodes: {author: {label: 'Proof author'}, critic: {label: 'Independent critic'},
    statement_reviewer: {label: 'Statement reviewer'}}}};
let state = base;
""", r"""
renderRunOverview();
assert.deepEqual([ui.runStage.textContent, ui.runHealth.textContent],
  ['Proof author · stopped', 'Stopped · saved conversation']);
// Stopped at another step: its saved input is kept, not a conversation.
state = {...base, stage: 'critic', activeNode: 'critic'};
renderRunOverview();
assert.deepEqual([ui.runStage.textContent, ui.runHealth.textContent],
  ['Independent critic · stopped', 'Stopped · saved step']);
// The stopped author writes a summary or a reply.
state = {...base, processAlive: true};
renderRunOverview();
assert.equal(ui.runHealth.textContent, 'Stopped · author replying');
// While the author ends its current step, the job is stopping.
state = {...base, phase: 'pausing', processAlive: true};
renderRunOverview();
assert.deepEqual([ui.runStage.textContent, ui.runHealth.textContent], ['Proof author · stopping', 'Running']);
// A pause by you still reads paused.
state = {...base, pauseCause: 'user'};
renderRunOverview();
assert.deepEqual([ui.runStage.textContent, ui.runHealth.textContent],
  ['Proof author · paused', 'Paused · saved conversation']);
state = {...base, phase: 'pausing', pauseCause: 'user', processAlive: true};
renderRunOverview();
assert.equal(ui.runStage.textContent, 'Proof author · pausing');
// A revision of the statement review you stopped is back at the earlier review: stopped there, not waiting.
state = {...base, phase: 'reviewed', stage: 'review', activeNode: 'statement_reviewer', pauseCause: '',
  manuallyStopped: true, review: {statement: 'S.', notes: ''}};
renderRunOverview();
assert.equal(ui.runStage.textContent, 'Statement reviewer · stopped');
assert.equal(ui.runOverview.dataset.status, 'stopped');
assert.equal(ui.runNextStep.textContent,
  'The earlier review is shown. Approve its statement to continue the selected stages.');
state = {...state, manuallyStopped: false};
renderRunOverview();
assert.equal(ui.runStage.textContent, 'Waiting for approval');
assert.equal(ui.runNextStep.textContent,
  'Statement review finished. Approve the statement to continue the selected stages.');
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

    def test_a_message_is_sent_to_the_chat_route_and_its_draft_is_kept_per_job(self):
        self.run_app([*RENDER, *SEND], RENDER_SETUP + r"""
const listeners = {}, storage = new Map();
ui.authorSteerInstruction.addEventListener = (type, handler) => { listeners[type] = handler; };
ui.authorSteerInstruction.dataset.maxChars = '20';
const localStorage = {getItem: key => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)};
""", r"""
const chat = {mode: 'author', note: '', summary: 'pause', items: []};
const job = runId => ({...author, runId, phase: 'running', conversation: chat});
render(job('r1'));
ui.authorSteerInstruction.value = '   ';
await ui.sendAuthorSteer.onclick();
assert.equal(ui.notice.textContent, 'Enter a message for the author.');
ui.authorSteerInstruction.value = 'x'.repeat(25);
await ui.sendAuthorSteer.onclick();
assert.equal(ui.notice.textContent, 'Keep the message at or below 20 characters (it has 25); shorten it or send it in parts.');
assert.deepEqual(acted, []);
// What is typed is kept for this job, in this browser too.
ui.authorSteerInstruction.value = ' Which lemma? ';
listeners.input();
assert.equal(storage.get('tcs-prover-chat-draft:r1'), ' Which lemma? ');
// Another job has its own box; back on this one, its draft returns. Polls never overwrite it.
render(job('r2'));
assert.equal(ui.authorSteerInstruction.value, '');
render(job('r1'));
assert.equal(ui.authorSteerInstruction.value, ' Which lemma? ');
render(job('r1'));
assert.equal(ui.authorSteerInstruction.value, ' Which lemma? ');
// Refused by the server: the text stays to send again.
actResult = false;
await ui.sendAuthorSteer.onclick();
assert.deepEqual(acted, [{path: '/chat', body: {text: 'Which lemma?'}}]);
assert.equal(ui.authorSteerInstruction.value, ' Which lemma? ');
assert.equal(ui.sendAuthorSteer.disabled, false);
// Accepted: the box and its draft are cleared.
actResult = true;
await ui.sendAuthorSteer.onclick();
assert.equal(ui.authorSteerInstruction.value, '');
assert.equal(storage.has('tcs-prover-chat-draft:r1'), false);
assert.equal(ui.authorSteerCount.textContent, '');
// Sent from a job the page left meanwhile: the message went, so that job's draft goes too (it never comes back
// to be sent twice); the job on screen keeps its own box.
ui.authorSteerInstruction.value = 'Later.';
listeners.input();
afterAct = () => render(job('r3'));
await ui.sendAuthorSteer.onclick();
assert.deepEqual(acted.at(-1), {path: '/chat', body: {text: 'Later.'}});
assert.equal(storage.has('tcs-prover-chat-draft:r1'), false);
assert.equal(ui.authorSteerInstruction.value, '');
afterAct = () => {};
render(job('r1'));
assert.equal(ui.authorSteerInstruction.value, '');
// Left and back again before the answer came: the box it was sent from is cleared too.
ui.authorSteerInstruction.value = 'Round trip.';
listeners.input();
afterAct = () => { render(job('r3')); render(job('r1')); };
await ui.sendAuthorSteer.onclick();
assert.equal(ui.authorSteerInstruction.value, '');
assert.equal(storage.has('tcs-prover-chat-draft:r1'), false);
// What was typed while the message was on its way stays, in the box and in its draft.
ui.authorSteerInstruction.value = 'First.';
listeners.input();
afterAct = () => { ui.authorSteerInstruction.value = 'First. And second?'; listeners.input(); };
await ui.sendAuthorSteer.onclick();
assert.deepEqual(acted.at(-1), {path: '/chat', body: {text: 'First.'}});
assert.equal(ui.authorSteerInstruction.value, 'First. And second?');
assert.equal(storage.get('tcs-prover-chat-draft:r1'), 'First. And second?');
// The draft of a page that was closed is read back from this browser.
afterAct = () => {};
render(job('r2'));
chatDrafts.clear();
render(job('r1'));
assert.equal(ui.authorSteerInstruction.value, 'First. And second?');
""")

    def test_send_stays_off_while_its_message_is_on_its_way(self):
        # A poll that arrives while /chat is pending must not turn Send back on: a second click would send the
        # same message twice.
        self.run_app([*RENDER, *SEND], RENDER_SETUP + r"""
ui.authorSteerInstruction.addEventListener = () => {};
""", r"""
const job = {...author, phase: 'running', conversation: {mode: 'author', note: '', summary: 'pause', items: []}};
render(job);
assert.equal(ui.sendAuthorSteer.disabled, false);
ui.authorSteerInstruction.value = 'Which lemma?';
let polled;
afterAct = () => { render(job); polled = ui.sendAuthorSteer.disabled; };
const pending = ui.sendAuthorSteer.onclick();
assert.equal(ui.sendAuthorSteer.disabled, true);
render(job);
assert.equal(ui.sendAuthorSteer.disabled, true, 'a poll while the message is on its way');
await ui.sendAuthorSteer.onclick();
assert.deepEqual(acted.map(entry => entry.path), ['/chat'], 'the second click sent nothing');
await pending;
assert.equal(polled, true, 'the reply to the request itself');
assert.equal(ui.sendAuthorSteer.disabled, false);
render(job);
assert.equal(ui.sendAuthorSteer.disabled, false);
""")

    def test_a_job_of_the_earlier_controller_takes_the_routes_the_page_used_before_the_chat(self):
        # Through the live gateway, a job of the earlier controller has a snapshot without a chat mode, note or
        # summary (main 5381081, server.py:2839). Its running author still takes live instructions (/steer-author,
        # which the gateway forwards); its paused author a message (/ask-author, which the gateway may refuse with
        # its own text).
        self.run_app([*RENDER, *SEND], RENDER_SETUP + r"""
ui.authorSteerInstruction.addEventListener = () => {};
""", r"""
const earlier = {available: false, busy: false, readOnly: true, items: [],
  reason: 'Talking to the author is available while the run is paused at the proof author.'};
const steerNote = 'This job belongs to the earlier controller: your message reaches the working author as a live '
  + 'instruction, but this chat does not list it or show an answer.';
render({...author, phase: 'running', conversation: earlier});
assert.equal(ui.authorSteerControl.hidden, false);
assert.equal(ui.sendAuthorSteer.disabled, false);
assert.equal(ui.authorConversationNote.textContent, steerNote);
assert.equal(ui.authorConversationNote.hidden, false);
assert.equal(ui.authorSteerInstruction.placeholder, 'Tell the author what to try next.');
assert.deepEqual([ui.requestSummary.disabled, ui.requestSummary.title], [true, steerNote]);
ui.authorSteerInstruction.value = ' Try the potential function first. ';
await ui.sendAuthorSteer.onclick();
assert.deepEqual(acted.at(-1), {path: '/steer-author', body: {instruction: 'Try the potential function first.'}});
assert.equal(ui.authorSteerInstruction.value, '');
// A web-cutoff run: that controller would send the instruction unrecorded in the run's audit log, so none goes.
const cutoffNote = 'This job belongs to the earlier controller, which does not record live instructions in the '
  + "run's web-cutoff audit log, so the chat sends none to this job.";
for (const cutoff of [{retrievalPolicyPath: '/runs/.controller/x/policy.json'}, {retrieval: {mode: 'historical'}},
    {retrievalMode: 'historical'}]) {
  render({...author, runId: 'cutoff', phase: 'running', ...cutoff, conversation: earlier});
  assert.equal(ui.sendAuthorSteer.disabled, true, JSON.stringify(cutoff));
  assert.equal(ui.authorConversationNote.textContent, cutoffNote);
  assert.equal(ui.sendAuthorSteer.title, cutoffNote);
  ui.authorSteerInstruction.value = 'Unrecorded?';
  const before = acted.length;
  await ui.sendAuthorSteer.onclick();
  assert.equal(acted.length, before + 1);
  assert.deepEqual(acted.at(-1), {path: '/chat', body: {text: 'Unrecorded?'}}, 'never /steer-author');
}
// Web search turned off (-1) keeps no audit log: its author takes live instructions as before.
render({...author, runId: 'off', phase: 'running', retrievalMode: 'disabled', conversation: earlier});
assert.equal(ui.sendAuthorSteer.disabled, false);
// Paused with its conversation: a message to the paused author, and a summary, by the earlier routes.
render({...author, conversation: {...earlier, available: true, reason: ''}});
assert.equal(ui.sendAuthorSteer.disabled, false);
assert.equal(ui.authorSteerInstruction.placeholder, 'Ask the author a question.');
assert.equal(ui.requestSummary.disabled, false);
ui.authorSteerInstruction.value = 'Which step fails?';
await ui.sendAuthorSteer.onclick();
assert.deepEqual(acted.at(-1), {path: '/ask-author', body: {text: 'Which step fails?'}});
await ui.requestSummary.onclick();
assert.deepEqual(acted.at(-1), {path: '/author-summary', body: {}});
// A reply being written: Send waits and says why; Stop reply shows.
render({...author, conversation: {...earlier, available: true, busy: true, reason: ''}});
assert.equal(ui.sendAuthorSteer.disabled, true);
assert.equal(ui.authorConversationNote.textContent, 'The author is replying. Wait for the reply or stop it.');
assert.equal(ui.stopReply.hidden, false);
// Another step works: Send is off with the earlier controller's own reason, never with no reason at all.
render({...author, phase: 'running', stage: 'critic', activeNode: 'critic', conversation: earlier});
assert.equal(ui.sendAuthorSteer.disabled, true);
assert.equal(ui.sendAuthorSteer.title, earlier.reason);
assert.equal(ui.authorConversationNote.textContent, earlier.reason);
// A controller without any conversation, paused: Send is off and says when it works.
render({...author, conversation: undefined});
assert.equal(ui.sendAuthorSteer.disabled, true);
assert.equal(ui.authorConversationNote.textContent, 'This job belongs to the earlier controller: you can write to '
  + 'its author while the author works.');
assert.equal(ui.sendAuthorSteer.title, ui.authorConversationNote.textContent);
// This controller's jobs always say what Send does: the chat route.
render({...author, phase: 'running', conversation: {mode: 'author', note: 'N.', summary: 'pause', items: []}});
ui.authorSteerInstruction.value = 'Hello.';
await ui.sendAuthorSteer.onclick();
assert.deepEqual(acted.at(-1), {path: '/chat', body: {text: 'Hello.'}});
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
