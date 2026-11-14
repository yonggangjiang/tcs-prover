"""The Records page and a record's Model calls page (layout option B, the owner's pick of 13-14 Nov 2026), and the
way in from a job's Live activity card: "Model calls · N" and "Training record".

The pages read the record routes (contract: ~/tcs-work/fixes/record-contract.md): GET /records, DELETE
/records/<run folder>, GET /jobs/<id>/model-calls[/<callId>[/raw]] and the streamed GET /jobs/<id>/training-record.
The browser checks cut the part of ui/app.js between its markers ("// --- Records and model calls", "// --- End of
records and model calls") and run it in Node's vm with a small fake DOM and scripted answers of those routes, as the
other UI tests do. The answers have the shapes the routes give for the first real job's record (its calls c000003 and
c000010) and the contract's other kinds (a Claude Code call, a failed one, a code cell with a web search, a team
member, a web-cutoff one-shot row). No server, no model.
"""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"
INDEX = ROOT / "ui" / "index.html"
STYLES = ROOT / "ui" / "styles.css"
NODE = shutil.which("node")
START = "// --- Records and model calls"
END = "// --- End of records and model calls"
RUN = "2026-11-14_03-05-16_prove-that-the-sum-of-two-odd-integers-is-even"

RUNNER = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const {start, end, setup, checks} = JSON.parse(fs.readFileSync(0, 'utf8'));
const from = source.indexOf(start), to = source.indexOf(end, from);
if (from < 0 || to < 0) throw new Error('Markers not found');
const code = source.slice(from, to);
let finished = false;
const context = {assert, console, setImmediate, URLSearchParams, finish: () => { finished = true; }};
process.on('exit', () => {
  if (!finished && !process.exitCode) { console.error('The checks did not finish.'); process.exitCode = 1; }
});
Promise.resolve(vm.runInNewContext(`${setup}\n${code}\n(async () => {\n${checks}\nfinish();\n})()`, context))
  .catch((error) => { console.error(error); process.exitCode = 1; });
"""

# A small DOM: elements with children (text as strings), attributes, dataset, classes, style, and the selectors the
# pages use (tag, .class, [data-x], [data-x="v"]); scripted fetch answers; timers kept for the checks to run.
DOM = r"""
const matches = (node, selector) => {
  const match = /^([a-z][a-z0-9]*)?(?:\.([\w-]+))?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/i.exec(selector.trim());
  if (!match) throw new Error('Unsupported selector ' + selector);
  const [, tag, cls, attr, value] = match;
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  if (cls && !node.className.split(/\s+/).includes(cls)) return false;
  if (attr) {
    const key = attr.startsWith('data-') ? attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
    const actual = key ? node.dataset[key] : node.attributes[attr];
    if (actual === undefined || (value !== undefined && actual !== value)) return false;
  }
  return true;
};
class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = String(tag).toUpperCase(); this.id = id; this.children = []; this.parentElement = null;
    this.dataset = {}; this.attributes = {}; this.className = ''; this.hidden = false; this.disabled = false;
    this.style = {}; this.title = ''; this.ownValue = ''; this.listeners = {};
    const list = () => this.className.split(/\s+/).filter(Boolean);
    this.classList = {
      contains: (name) => list().includes(name),
      add: (...names) => { this.className = [...new Set([...list(), ...names])].join(' '); },
      remove: (...names) => { this.className = list().filter((name) => !names.includes(name)).join(' '); },
      toggle: (name, force) => {
        const on = force === undefined ? !list().includes(name) : Boolean(force);
        on ? this.classList.add(name) : this.classList.remove(name);
        return on;
      },
    };
  }
  get textContent() { return this.children.map((child) => child instanceof Element ? child.textContent : String(child)).join(''); }
  set textContent(value) { this.replaceChildren(String(value)); }
  get innerText() { return this.textContent; }
  get value() { return this.ownValue; }
  set value(value) { this.ownValue = String(value); }
  get firstChild() { return this.children[0] ?? null; }
  get lastChild() { return this.children.at(-1) ?? null; }
  get isConnected() { let node = this; while (node.parentElement) node = node.parentElement; return node === document.body; }
  get nextElementSibling() {
    const siblings = this.parentElement ? this.parentElement.children.filter((child) => child instanceof Element) : [];
    return siblings[siblings.indexOf(this) + 1] ?? null;
  }
  append(...items) {
    for (const item of items) {
      if (item instanceof Element) { item.remove(); item.parentElement = this; }
      this.children.push(item);
    }
  }
  replaceChildren(...items) {
    for (const child of this.children) if (child instanceof Element) child.parentElement = null;
    this.children = [];
    this.append(...items.filter((item) => item !== '' && item !== null && item !== undefined));
  }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
    this.parentElement = null;
  }
  after(node) {
    node.remove();
    const siblings = this.parentElement.children;
    siblings.splice(siblings.indexOf(this) + 1, 0, node);
    node.parentElement = this.parentElement;
  }
  descendants() { return this.children.flatMap((child) => child instanceof Element ? [child, ...child.descendants()] : []); }
  querySelectorAll(selector) { return this.descendants().filter((node) => matches(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (matches(node, selector)) return node;
    return null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type, event = {}) {
    for (const listener of this.listeners[type] || []) listener({target: this, preventDefault() { event.prevented = true; }, ...event});
  }
  click() { if (this.onclick) this.onclick({target: this, button: 0, preventDefault() {}}); this.clicked = (this.clicked || 0) + 1; }
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatch('close'); }
}
const document = {
  body: new Element('body'), hidden: false, activeElement: null, listeners: {},
  createElement: (tag) => new Element(tag), createElementNS: (ns, tag) => new Element(tag),
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); },
  getElementById: (id) => elements[id] || null,
};
const elements = {};
const $ = (id) => elements[id] ||= (() => { const node = new Element(id === 'recordsSort' ? 'select' : 'div', id); document.body.append(node); return node; })();
const urls = [], requests = [], timers = [], saved = [];
const location = {pathname: '/', search: ''};
const history = {pushState(a, b, url) { urls.push(url); location.search = url.slice(1); },
                 replaceState(a, b, url) { urls.push(url); location.search = url.slice(1); }};
const setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
const clearTimeout = () => {}, clearInterval = () => {};
class Blob { constructor(parts, options = {}) { this.parts = parts; this.type = options.type; this.size = parts.reduce((n, p) => n + (p.length || 0), 0); } }
const URL = {createObjectURL: (blob) => { saved.push(blob); return 'blob:' + saved.length; }, revokeObjectURL() {}};
// fetch: answers[path] is {status, body} or a function of the request; every request is kept.
const answers = {};
const respond = (status, body) => ({ok: status >= 200 && status < 300, status,
  json: async () => body === undefined ? Promise.reject(new Error('no JSON')) : JSON.parse(JSON.stringify(body)),
  body: null, blob: async () => new Blob([JSON.stringify(body)])});
const fetch = async (path, options = {}) => {
  requests.push({path, method: options.method || 'GET', token: options.headers?.['X-TCS-Prover-Token']});
  const answer = answers[path] ?? answers[path.split('?')[0]];
  if (typeof answer === 'function') return answer(path, options);
  if (!answer) return respond(404, {error: 'Not found.'});
  return respond(answer.status ?? 200, answer.body);
};
const settle = async () => { for (let i = 0; i < 40; i++) await new Promise((resolve) => setImmediate(resolve)); };
"""

# The app.js globals the section uses, as the rest of the page defines them.
SETUP = r"""
const sessionToken = 'session-token';
let currentJob = '', currentBatch = '', composingProblem = false, settingUp = false, viewingRecords = false;
let callsRecord = '', timer, clock, jobsTimer;
const ui = {notice: $('notice'), callsJobCrumb: $('callsJobCrumb')};
const show = (element, visible) => { element.hidden = !visible; };
const views = [];
const clearJobView = () => views.push('clear'), renderBatchNavigation = () => views.push('nav');
const roleModelCatalog = () => [{id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol — default'}, {id: 'claude-opus-5-5', label: 'Claude Opus 5.5'}];
const jobs = [];
const selectJob = (run) => { jobs.push(run); currentJob = run; callsRecord = ''; };
// The markup's filter groups (index.html).
for (const [key, label] of [['all', 'All'], ['kept', 'Job kept'], ['deleted', 'Job deleted']]) {
  const button = new Element('button'); button.dataset.records = key; button.className = 'seg';
  const count = new Element('span'); count.className = 'seg-count';
  button.append(label + ' ', count); $('recordsFilter').append(button);
}
for (const [key, label] of [['all', 'All'], ['author', 'Coordinator'], ['members', 'Members'], ['critics', 'Critics']]) {
  const button = new Element('button'); button.dataset.calls = key; button.className = 'filter'; button.textContent = label;
  $('callsFilter').append(button);
}
const navItem = new Element('a'); navItem.className = 'nav-item'; navItem.append($('navRecordsSize'));
"""

# Three records: two whose job is listed, one of a deleted job.
RECORDS = [
    {"runFolder": RUN, "title": "Prove that the sum of two odd integers is even.", "created": "2026-11-14T02:05:16.726Z",
     "sizeBytes": 512621, "jobExists": True},
    {"runFolder": "2026-11-13_09-02-00_mixing", "title": "Let G be a d-regular graph.", "created": "2026-11-13T08:02:00Z",
     "sizeBytes": 225_000_000, "jobExists": True},
    {"runFolder": "2026-11-09_08-31-00_sensitivity", "title": "Prove that s(f) >= sqrt(deg(f)).",
     "created": "2026-11-09T07:31:00Z", "sizeBytes": 642_000_000, "jobExists": False},
]


def row(identity, **values):
    """A list row as GET /jobs/<id>/model-calls gives it."""

    return {"id": identity, "time": "2026-11-14T02:05:49.265Z", "role": "critic", "label": "Critic coordinator · round 1",
            "member": None, "model": "gpt-6.1-sol", "program": "codex", "durationMs": 3408,
            "tokens": {"input": 13646, "cached": 13184, "output": 122, "reasoning": 0}, "status": "completed",
            "error": None, "placeholderKinds": ["token_probabilities", "network_only"], "kind": "request",
            "threadId": "01a1d7cd-4866-7ee2-8213-e4198a8a39e8", **values}


OPENAI_PLACEHOLDERS = [
    {"kind": "token_probabilities", "facts": {}, "withheldBy": "OpenAI", "why": "Refused by OpenAI for reasoning models."},
    {"kind": "network_only", "facts": {"details": ["per-word arrival times", "response headers", "moderation notes"]},
     "withheldBy": "TCS Prover",
     "why": "Not recorded on this route: TCS Prover records the programs' own copies, not the network."}]
ROWS = [
    row("c000001", role="author", label="Author", durationMs=365, status="cancelled",
        tokens={"input": None, "cached": None, "output": None, "reasoning": None},
        error="response stream dropped before provider terminal event", time="2026-11-14T02:05:18.786Z"),
    row("c000009"),
    row("c000010", durationMs=1848, tokens={"input": 13791, "cached": 13568, "output": 36, "reasoning": 13},
        placeholderKinds=["hidden_reasoning", "token_probabilities", "network_only"], time="2026-11-14T02:05:52.823Z"),
    row("c000011", role="independent critic", member="/root/independent_review", time="2026-11-14T02:05:53.705Z"),
    row("c000020", role="member", label="Member 2", member="member-2-3", model="claude-opus-5-5", program="claude",
        durationMs=None, tokens={"input": 41200, "cached": 30000, "output": None, "reasoning": None},
        placeholderKinds=["hidden_reasoning", "token_probabilities", "network_only"], time="2026-11-14T02:06:00Z"),
    row("c000021", role="coordinator", label="Coordinator", status="failed", tokens={"input": None, "cached": None,
        "output": None, "reasoning": None}, error="API Error: 529 overloaded", time="2026-11-14T02:06:02Z"),
    row("p000001", role="convention critic", label="Convention critic", status="not recorded", durationMs=None,
        tokens={"input": None, "cached": None, "output": None, "reasoning": None},
        placeholderKinds=["web_cutoff_one_shot"], threadId=None, time="2026-11-14T02:06:04Z"),
    row("c000016", role="latex editor", label="latex_editor", time="2026-11-14T02:06:14.140Z"),
]
# Call c000010 as the route gives it (the first real job's record): a reasoning item, then a tool call with its output.
DETAIL_10 = {
    "sent": {"instructions": None, "tools": None, "previousCallId": "c000009", "newItems": [
        {"type": "function_call_output", "item": {"type": "function_call_output", "call_id": "call_31db",
                                                  "output": "{\"task_name\":\"/root/independent_review\"}"},
         "output": None, "nestedToolCalls": []}]},
    "received": {"items": [
        {"type": "reasoning", "item": {"type": "reasoning", "summary": [{"type": "summary_text",
                                                                         "text": "**Waiting to proceed**"}],
                                       "encrypted_content": "gAAAAABq98IC"}, "output": None, "nestedToolCalls": []},
        {"type": "function_call", "item": {"type": "function_call", "name": "wait_agent", "namespace": "collaboration",
                                           "arguments": "{\"timeout_ms\":10000}", "call_id": "call_7fa4"},
         "output": {"type": "function_call_output", "call_id": "call_7fa4",
                    "output": "{\"message\":\"Wait completed.\",\"timed_out\":false}"}, "nestedToolCalls": []}]},
    "usage": {"input_tokens": 13791, "cached_input_tokens": 13568, "output_tokens": 36, "reasoning_output_tokens": 13,
              "total_tokens": 13827},
    "placeholders": [{"kind": "hidden_reasoning", "facts": {"reasoningTokens": 13, "encryptedCopyKept": True},
                      "withheldBy": "OpenAI", "why": "OpenAI does not send raw reasoning; its encrypted copy is kept."},
                     *OPENAI_PLACEHOLDERS],
    "rawAvailable": True, "error": None}
# Call c000009: the spawn_agent call whose output c000010 sends.
DETAIL_9 = {"sent": {"instructions": None, "tools": None, "previousCallId": None, "newItems": []},
            "received": {"items": [{"type": "function_call", "item": {"type": "function_call", "name": "spawn_agent",
                                                                       "arguments": "{}", "call_id": "call_31db"},
                                    "output": None, "nestedToolCalls": []}]},
            "usage": None, "placeholders": OPENAI_PLACEHOLDERS, "rawAvailable": True, "error": None}
# A code cell with a web search and a command inside, as the trace records them (web.run's result is the text read).
SEARCH_TEXT = "Hoory, Linial, Wigderson. Expander graphs and their applications. (ams.org)\n" * 40
DETAIL_1 = {
    "sent": {"instructions": "You coordinate a small research team.", "tools": [
        {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"},
                                                            {"type": "function", "name": "update_goal"}]}],
        "previousCallId": None, "newItems": [
            {"type": "message", "item": {"type": "message", "role": "developer",
                                         "content": [{"type": "input_text", "text": "Work in the sandbox."}]},
             "output": None, "nestedToolCalls": []}]},
    "received": {"items": [
        {"type": "custom_tool_call", "item": {"type": "custom_tool_call", "name": "exec", "call_id": "call_1",
                                              "input": "const found = await tools.web.run({search_query: [{q: 'expander mixing lemma'}]});"},
         "output": {"type": "custom_tool_call_output", "call_id": "call_1",
                    "output": [{"type": "input_text", "text": "Script completed"}]},
         "nestedToolCalls": [
             {"tool": "web.run", "input": {"tool_name": "run", "tool_namespace": "web", "payload": {
                 "type": "function", "arguments": "{\"search_query\":[{\"q\":\"expander mixing lemma\"}]}"}},
              "output": {"type": "code_mode_response", "value": SEARCH_TEXT}, "status": "completed"},
             {"tool": "exec_command", "input": {"tool_name": "exec_command", "payload": {
                 "type": "function", "arguments": "{\"cmd\":\"python3 check.py --n 64\"}"}},
              "output": {"type": "code_mode_response", "value": {"exit_code": 0, "wall_time_seconds": 0.9,
                                                                 "output": "bound holds"}}, "status": "completed"}]}]},
    "usage": None, "placeholders": OPENAI_PLACEHOLDERS, "rawAvailable": True,
    "error": "response stream dropped before provider terminal event"}
# A Claude Code team member's call: its prompt first, a thinking block, an answer.
DETAIL_20 = {
    "sent": {"instructions": None, "tools": None, "previousCallId": None, "newItems": [
        {"type": "text", "item": {"type": "text", "text": "Member 2: prove Lemma B1."}, "output": None,
         "nestedToolCalls": []}]},
    "received": {"items": [
        {"type": "thinking", "item": {"type": "thinking", "thinking": "**Planning B1** Use the spectral bound.",
                                      "signature": "EqQB"}, "output": None, "nestedToolCalls": []},
        {"type": "text", "item": {"type": "text", "text": "B1 holds for T = V \\ S."}, "output": None,
         "nestedToolCalls": []}]},
    "usage": {"input_tokens": 11200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 30000},
    "placeholders": [
        {"kind": "hidden_reasoning", "facts": {"reasoningTokens": None, "encryptedCopyKept": True},
         "withheldBy": "Anthropic", "why": "Anthropic does not send raw reasoning; its encrypted copy is kept."},
        {"kind": "token_probabilities", "facts": {}, "withheldBy": "Anthropic",
         "why": "Anthropic's API does not offer token probabilities."},
        OPENAI_PLACEHOLDERS[1],
        {"kind": "network_only", "facts": {"details": ["system prompt", "tool definitions", "request parameters"]},
         "withheldBy": "Claude Code", "why": "Claude Code's output stream does not carry the request it sent."}],
    "rawAvailable": True, "error": None}
DETAIL_21 = {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": "c000001"},
             "received": {"items": []}, "usage": None, "placeholders": OPENAI_PLACEHOLDERS, "rawAvailable": True,
             "error": "API Error: 529 overloaded"}
DETAIL_P1 = {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": None},
             "received": {"items": []}, "usage": None, "rawAvailable": False, "error": None,
             "placeholders": [{"kind": "web_cutoff_one_shot", "facts": {"label": "Convention critic"},
                               "withheldBy": "TCS Prover",
                               "why": "Not recorded: web-cutoff one-shot calls leave no files."}]}


def answers(**extra):
    """The routes' answers for the fixture record (JS object source)."""

    base = f"/jobs/{RUN}/model-calls"
    table = {
        "/records": {"body": RECORDS},
        f"{base}?offset=0&limit=1000": {"body": {"total": len(ROWS), "calls": ROWS,
                                                  "recordState": {"available": True, "problems": []}}},
        f"{base}?offset=0&limit=1": {"body": {"total": len(ROWS), "calls": ROWS[:1],
                                               "recordState": {"available": True, "problems": []}}},
        f"{base}/c000001": {"body": DETAIL_1}, f"{base}/c000009": {"body": DETAIL_9},
        f"{base}/c000010": {"body": DETAIL_10}, f"{base}/c000020": {"body": DETAIL_20},
        f"{base}/p000001": {"body": DETAIL_P1}, f"{base}/c000021": {"body": DETAIL_21},
        f"{base}/c000010/raw": {"body": {"format": "codex-trace", "request": json.dumps(
            {"model": "gpt-6.1-sol", "input": [{"type": "message"}] * 13}), "response": "{}"}},
        **extra,
    }
    return f"Object.assign(answers, {json.dumps(table)});\n"


@unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
class PageLogicTests(unittest.TestCase):
    def run_pages(self, checks, extra=""):
        payload = json.dumps({"start": START, "end": END, "setup": DOM + SETUP + answers() + extra,
                              "checks": checks})
        result = subprocess.run([NODE, "-e", RUNNER, str(APP)], input=payload, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_records_page_lists_sorts_filters_and_sizes_the_records(self):
        self.run_pages(r"""
await settle();
// The sidebar names the size of them all, read when the page loads.
assert.equal($('navRecordsSize').textContent, '827.3 MB');
assert.equal($('navRecordsSize').hidden, false);
await openRecords();
await settle();
assert.equal(viewingRecords, true);
assert.equal(urls.at(-1), '/?records=1');
assert.equal($('recordsMeta').textContent, '3 records827.3 MB in all1 of deleted job');
const counts = () => $('recordsFilter').querySelectorAll('[data-records]').map((button) =>
  [button.dataset.records, button.querySelector('.seg-count').textContent, button.getAttribute('aria-pressed')]);
assert.deepEqual(counts(), [['all', '3', 'true'], ['kept', '2', 'false'], ['deleted', '1', 'false']]);
const rows = () => $('recordsList').children.map((item) => item.dataset.record);
// Newest first: the record's start.
assert.deepEqual(rows(), [RUN, '2026-11-13_09-02-00_mixing', '2026-11-09_08-31-00_sensitivity']);
const first = $('recordsList').children[0];
assert.equal(first.querySelector('.rec-title').textContent, 'Prove that the sum of two odd integers is even.' + RUN);
assert.equal(first.querySelector('.rec-size').querySelector('b').textContent, '500.6 KB');
assert.equal(first.querySelector('.rec-job').textContent, 'Kept Open');
// The bar is the record's share of the largest.
assert.equal($('recordsList').children[2].querySelector('.rec-bar').firstChild.style.width, '100%');
assert.equal($('recordsList').children[2].querySelector('.rec-job').textContent, 'Deleted');
assert.equal($('recordsList').children[2].querySelector('.rec-job').className, 'rec-job gone');
// Largest first.
$('recordsSort').value = 'largest'; $('recordsSort').onchange();
assert.deepEqual(rows(), ['2026-11-09_08-31-00_sensitivity', '2026-11-13_09-02-00_mixing', RUN]);
// Job deleted only, then Job kept only.
$('recordsFilter').onclick({target: $('recordsFilter').children[2]});
assert.deepEqual(rows(), ['2026-11-09_08-31-00_sensitivity']);
$('recordsFilter').onclick({target: $('recordsFilter').children[1]});
assert.deepEqual(rows(), ['2026-11-13_09-02-00_mixing', RUN]);
assert.equal($('recordsEmpty').hidden, true);
// Open leads to the job's page; the title to the record's Model calls.
$('recordsList').children[1].querySelector('.rec-job').querySelector('a').onclick({button: 0, preventDefault() {}});
assert.deepEqual(jobs, [RUN]);
assert.match($('recordsList').children[1].querySelector('.rec-open').href, /\?calls=2026-11-14_03-05-16_/);
// Every request carried the session token.
assert(requests.every((request) => request.token === 'session-token'));
""".replace("RUN", json.dumps(RUN)))

    def test_delete_asks_first_says_what_it_frees_and_shows_a_refusal(self):
        self.run_pages(r"""
await openRecords();
await settle();
const deleted = $('recordsList').children[2];
deleted.querySelector('.rec-del').onclick();
assert.equal($('recordDeleteDialog').open, true);
assert.equal($('recordDeleteName').textContent, 'Prove that s(f) >= sqrt(deg(f)).');
assert.match($('recordDeleteFacts').textContent, /^2026-11-09_08-31-00_sensitivity · 612\.3 MB · started 9 Nov 2026 \d\d:31$/);
const lines = $('recordDeleteList').children.map((item) => item.textContent);
assert.deepEqual(lines, [
  'Removes every recorded model call of this job from this computer: this cannot be undone.',
  'Its job was deleted earlier, so this record is the last copy of what its models were sent and answered.',
  'Frees 612.3 MB.']);
assert.equal(document.activeElement, $('recordDeleteCancel'));
// A record whose job is listed: the job stays.
$('recordDeleteDialog').close();
$('recordsList').children[0].querySelector('.rec-del').onclick();
assert.match($('recordDeleteList').children[1].textContent, /^Its job stays, with its proof, files and activity log/);
// The server refuses while the job works: the window stays open and says so.
answers['/records/' + RUN] = {status: 409, body: {error: 'The job is working: stop it, or wait for its reply, before deleting its record.'}};
await $('recordDeleteConfirm').onclick();
await settle();
assert.equal($('recordDeleteDialog').open, true);
assert.equal($('recordDeleteError').hidden, false);
assert.equal($('recordDeleteError').textContent,
  'Not deleted: The job is working: stop it, or wait for its reply, before deleting its record.');
assert.deepEqual(requests.filter((request) => request.method === 'DELETE').map((request) => request.path),
  ['/records/' + RUN]);
// Deleted: the window closes, the row goes, and the sidebar's size follows.
answers['/records/' + RUN] = {status: 200, body: {deleted: RUN}};
answers['/records'] = {body: [RECORDS[1], RECORDS[2]]};
await $('recordDeleteConfirm').onclick();
await settle();
assert.equal($('recordDeleteDialog').open, false);
assert.deepEqual($('recordsList').children.map((item) => item.dataset.record),
  ['2026-11-13_09-02-00_mixing', '2026-11-09_08-31-00_sensitivity']);
assert.equal($('navRecordsSize').textContent, '826.8 MB');
""".replace("RUN", json.dumps(RUN)), extra=f"const RECORDS = {json.dumps(RECORDS)};\n")

    def test_the_model_calls_page_lists_the_calls_and_shows_the_chosen_one(self):
        self.run_pages(r"""
await settle();
currentJob = RUN; currentBatch = '__root__';
await openModelCalls(RUN, {from: 'job'});
await settle();
assert.equal(callsRecord, RUN);
assert.equal(currentJob, '');
// The first call is shown beside the list; the address names only a call you choose.
assert.equal(urls.at(-1), '/?calls=' + RUN + '&batch=__root__');
// The head: how many calls, tokens in (cached) and out, the time of the calls.
assert.equal($('callsMeta').textContent, '8 calls · 95.9k in (83.1k cached) · 402 out · 16s of calls'.replace(/ · /g, ''));
assert.equal($('callsBackText').textContent, 'Back to the job');
assert.equal($('callsTrainingButton').hidden, false);
assert.equal(ui.callsJobCrumb.textContent, 'Prove that the sum of two odd integers is even.');
// One row per call: its number, role and detail, time, model, numbers, withheld mark and status.
const items = () => $('callsList').children.map((item) => item.firstChild);
assert.equal(items().length, 8);
const titles = items().map((button) => button.querySelector('.mcb-role').textContent);
assert.deepEqual(titles, ['Author', 'Critic coordinator · round 1', 'Critic coordinator · round 1',
  'Independent critic · round 1 · independent_review', 'Member 2 · assignment 3', 'Coordinator',
  'Convention critic', 'LaTeX editor']);
assert.equal(items()[2].querySelector('.mcb-line2').textContent, 'GPT 6.1 Sol · 1.8s · 13.8k → 36');
assert.equal(items()[4].querySelector('.mcb-line2').textContent, 'Claude Opus 5.5 · 41.2k → –');
assert.equal(items()[2].querySelector('.mcb-hide').title, '13 reasoning tokens hidden; token probabilities and network details not recorded');
assert.equal(items()[4].querySelector('.mcb-hide').title, "Claude's thinking: only its summary; token probabilities and network details not recorded");
assert.equal(items()[1].querySelector('.mcb-hide'), null);
assert.deepEqual(items().map((button) => button.querySelector('.mc-st').textContent),
  ['Cancelled', 'Done', 'Done', 'Done', 'Done', 'Failed', 'Not recorded', 'Done']);
// The first call is chosen: its failure, what was sent (instructions and tools in one line each) and what came back.
const pane = () => $('callPane');
assert.equal(items()[0].getAttribute('aria-current'), 'true');
assert.equal(pane().querySelector('h2').textContent, 'Call 1 · Author Cancelled');
assert.equal(pane().querySelector('.mc-error').textContent, 'Cancelledresponse stream dropped before provider terminal event');
const sent = pane().querySelector('.mc-sent');
assert.equal(sent.querySelector('.mc-sec-head').textContent, 'Sent1 item: the first request of its conversation');
const lines = sent.querySelector('.mc-sent-lines').children.map((line) => line.querySelector('dd').textContent);
assert.deepEqual(lines, ['You coordinate a small research team.37 characters', 'exec · update_goal2 tools']);
assert.equal(sent.querySelector('.mc-items').children[0].querySelector('.mc-kind').textContent, 'Developer message');
// A code cell: its script, its web search with the text it read one click away, its command with what came back.
const cell = pane().querySelector('.mc-received').querySelector('.mc-code');
assert.equal(cell.querySelector('.mc-item-note').textContent, 'JavaScript · 2 calls inside');
const [search, command] = cell.querySelector('.mc-nested').children;
assert.equal(search.querySelector('.mc-nested-head').textContent, 'Web searchexpander mixing lemma');
const read = search.querySelector('.mc-link');
assert.equal(read.textContent, 'Show the text it read · ' + (SEARCH_TEXT.length).toLocaleString('en-US') + ' characters');
read.onclick();
assert.equal(search.querySelector('.mc-read').textContent, SEARCH_TEXT);
assert.equal(command.querySelector('.mc-nested-head').textContent, 'Commandpython3 check.py --n 64exit 0 · 0.9 s');
assert.equal(command.querySelector('.mc-out').textContent, 'bound holds');
assert.equal(cell.querySelector('.mc-output').textContent, 'What the cell returnedScript completed');
// What the record does not hold.
const notes = () => pane().querySelector('.mc-notrec').children.map((item) => item.textContent);
assert.deepEqual(notes(), ['Token probabilities · OpenAI refuses them for reasoning models',
  'Network details · per-word arrival times, response headers, moderation notes: not recorded on this route']);
""".replace("RUN", json.dumps(RUN)), extra=f"const SEARCH_TEXT = {json.dumps(SEARCH_TEXT)};\n")

    def test_a_call_with_hidden_reasoning_shows_its_placeholder_after_the_summary_and_steps_on(self):
        self.run_pages(r"""
await settle();
await openModelCalls(RUN, {from: 'records', call: 'c000010'});
await settle();
assert.equal($('callsBrowser').dataset.view, 'detail');
const pane = () => $('callPane');
assert.equal(pane().querySelector('h2').textContent, 'Call 3 · Critic coordinator, round 1 Done');
assert.equal(pane().querySelector('.mc-meta').textContent.split(' · ').slice(0, 2).join(' · '), 'GPT 6.1 Sol · Codex CLI');
// Took, input (cached), output (reasoning).
assert.deepEqual(pane().querySelector('.mc-facts').children.map((fact) => fact.textContent),
  ['Took1.8s', 'Input13,791 (13,568 cached)', 'Output36 (13 reasoning)']);
// Sent: one item new since call 2, the output of the call 2 made (named once call 2 was read).
const sent = pane().querySelector('.mc-sent');
assert.equal(sent.querySelector('.mc-sec-head').textContent, 'Sent1 item new since call 2 · the rest as before');
assert.equal(sent.querySelector('.mc-items').children[0].querySelector('.mc-kind').textContent, 'Output of spawn_agent');
assert.equal(sent.querySelector('.mc-items').children[0].querySelector('.mc-item-note').textContent, 'from call 2');
assert.equal(sent.querySelector('.mc-sent-lines').children[1].querySelector('dd').textContent,
  'None in this request: it goes on with the tools of the requests before it.');
// Received: the summary, the dashed placeholder of the hidden reasoning, the tool call with its output.
const received = pane().querySelector('.mc-received').querySelector('.mc-items').children;
assert.deepEqual(received.map((item) => item.className), ['mc-item', 'mc-withheld', 'mc-item']);
assert.equal(received[0].querySelector('.mc-summary').textContent, 'Waiting to proceed');
assert.equal(received[1].querySelector('strong').textContent, 'Hidden reasoning · 13 tokens');
assert.equal(received[1].querySelector('p').textContent, "OpenAI does not return the model's own reasoning, only its "
  + 'summary. The record keeps the encrypted copy that came back; only the provider can read it.');
assert.equal(received[2].querySelector('.mc-kind').textContent, 'Tool call · wait_agent');
assert.equal(received[2].querySelector('.mc-output').textContent, 'Output{"message":"Wait completed.","timed_out":false}');
assert.equal(pane().querySelector('.mc-notrec').children[0].textContent, 'Hidden reasoning · 13 tokens, encrypted copy kept');
assert.deepEqual(pane().querySelector('.mc-foot').children[0].querySelector('dl').children.map((node) => node.textContent),
  ['Input', '13,791', 'of it cached', '13,568', 'Output', '36', 'of it reasoning', '13', 'Total', '13,827']);
// The full request, one click away: read once from /raw.
const full = pane().querySelector('.mc-detail-actions').children[1];
assert.equal(full.textContent, 'Full request');
await full.onclick();
await settle();
const box = pane().querySelector('.mc-full');
assert.equal(box.hidden, false);
assert.match(box.querySelector('.mc-sec-head').textContent, /^Full request\d[\d,]* characters · 13 input items · exactly as Codex's trace wrote it$/);
// The raw JSON is saved as a file.
const raw = pane().querySelector('.mc-detail-actions').children[2];
await raw.onclick();
await settle();
assert.equal(saved.at(-1).type, 'application/json');
assert.equal(requests.filter((request) => request.path.endsWith('/c000010/raw')).length, 1);
// Next and previous step through the list.
const [previous, next] = pane().querySelector('.mcb-step').children;
assert.equal(previous.textContent, '2');
assert.equal(next.textContent, '4');
next.onclick();
await settle();
assert.equal(callsStore().selected, 'c000011');
assert.equal(urls.at(-1), '/?calls=' + RUN + '&call=c000011');
// The arrow keys step while the list has the focus.
$('callsList').dispatch('keydown', {key: 'ArrowUp'});
await settle();
assert.equal(callsStore().selected, 'c000010');
// On a phone, "All calls" goes back to the list, and a call chosen in it takes its place.
pane().querySelector('.mcb-back').onclick();
assert.equal($('callsBrowser').dataset.view, 'list');
$('callsList').children[0].firstChild.onclick();
await settle();
assert.equal($('callsBrowser').dataset.view, 'detail');
assert.equal(callsStore().selected, 'c000001');
""".replace("RUN", json.dumps(RUN)))

    def test_claude_calls_unrecorded_rows_filters_and_words(self):
        self.run_pages(r"""
await settle();
await openModelCalls(RUN, {from: 'job'});
await settle();
// A Claude Code member: its prompt first, its thinking summary, its answer; the request is not on this route.
await showCall('c000020');
await settle();
const pane = () => $('callPane');
const sent = pane().querySelector('.mc-sent');
assert.deepEqual(sent.querySelector('.mc-sent-lines').children.map((line) => line.querySelector('dd').textContent), [
  "Not recorded on this route: Claude Code's output does not carry the request it sent.",
  "Not recorded on this route: Claude Code's output does not carry the request it sent."]);
assert.equal(sent.querySelector('.mc-items').children[0].querySelector('.mc-kind').textContent, 'Text sent');
const received = pane().querySelector('.mc-received').querySelector('.mc-items').children;
assert.deepEqual(received.map((item) => item.className === 'mc-withheld' ? item.querySelector('strong').textContent
  : item.querySelector('.mc-kind').textContent), ['Thinking summary', 'Hidden reasoning', 'Answer']);
assert.equal(received[0].querySelector('.mc-summary').textContent, 'Planning B1 Use the spectral bound.');
assert.deepEqual(pane().querySelector('.mc-notrec').children.map((item) => item.textContent), [
  'Hidden reasoning · encrypted copy kept', "Token probabilities · Anthropic's API does not offer them",
  'Network details · per-word arrival times, response headers, moderation notes: not recorded on this route',
  "The request Claude Code sent · system prompt, tool definitions, request parameters: Claude Code's output does not carry the request it sent"]);
// Claude Code's stream gives no time per request: when it was sent only.
assert.deepEqual(pane().querySelector('.mc-foot').children[1].querySelector('dl').children.map((node) => node.textContent)
  .slice(0, 1), ['Sent']);
assert.equal(pane().querySelector('.mc-foot').children[1].querySelector('dl').children.length, 2);
// A failed call says why first.
await showCall('c000021');
await settle();
assert.equal(pane().querySelector('.mc-error').textContent, 'FailedAPI Error: 529 overloaded');
// A web-cutoff one-shot call: not recorded, said where its items would be.
await showCall('p000001');
await settle();
assert.equal(pane().querySelector('.mc-sent').querySelector('.mc-sec-head').textContent, 'SentNot recorded');
assert.equal(pane().querySelector('.mc-received').querySelector('.mc-withheld').textContent,
  'Not recordedNot recorded: web-cutoff one-shot calls leave no files.');
assert.equal(pane().querySelector('.mc-detail-actions').children.length, 1);
// The filter by role: the author's own turns (named after the coordinator the job has), members, critics.
const filter = (key) => $('callsFilter').onclick({target: $('callsFilter').querySelector('[data-calls="' + key + '"]')});
const shown = () => $('callsList').children.map((item) => item.firstChild.dataset.call);
assert.equal($('callsFilter').querySelector('[data-calls="author"]').textContent, 'Coordinator');
filter('author');
assert.deepEqual(shown(), ['c000001', 'c000021']);
filter('members');
assert.deepEqual(shown(), ['c000020']);
filter('critics');
assert.deepEqual(shown(), ['c000009', 'c000010', 'c000011', 'p000001']);
assert.match($('callsCount').textContent, /^4 calls of 8 · ↑↓ steps · /);
// Words: the role, the model, the status, the failure.
filter('all');
$('callsFilterText').value = 'claude';
$('callsFilterText').dispatch('input');
assert.deepEqual(shown(), ['c000020']);
$('callsFilterText').value = 'overloaded';
$('callsFilterText').dispatch('input');
assert.deepEqual(shown(), ['c000021']);
$('callsFilterText').value = 'latex editor';
$('callsFilterText').dispatch('input');
assert.deepEqual(shown(), ['c000016']);
""".replace("RUN", json.dumps(RUN)))

    def test_a_live_jobs_list_grows_and_a_job_without_a_record_says_so(self):
        self.run_pages(r"""
await settle();
await openModelCalls(RUN, {from: 'job'});
await settle();
assert.equal($('callsList').children.length, 8);
const base = '/jobs/' + RUN + '/model-calls';
// Every 5 s the page looks at the count only; the list is read again when the count (or what the record could not
// do) changed. After a minute without a change it looks every 30 s.
const lists = () => requests.filter((request) => request.path === base + '?offset=0&limit=1000').length;
const looks = () => requests.filter((request) => request.path === base + '?offset=0&limit=1').length;
const read = lists();
for (let index = 0; index < 12; index++) {
  const due = timers.filter(([fn, ms]) => ms === 5000).at(-1);
  timers.length = 0;
  due[0]();
  await settle();
}
assert.equal(lists(), read);
assert.equal(looks(), 12);
assert.deepEqual(timers.filter(([fn, ms]) => ms === 30000).length, 1);
timers.push([timers.filter(([fn, ms]) => ms === 30000)[0][0], 5000]);
const more = [...ROWS, {...ROWS[1], id: 'c000030', time: '2026-11-14T02:07:00Z'}];
answers[base + '?offset=0&limit=1'] = {body: {total: 9, calls: more.slice(0, 1), recordState: {available: true, problems: []}}};
answers[base + '?offset=0&limit=1000'] = {body: {total: 9, calls: more, recordState: {available: true,
  problems: ['Raw trace files of 2 Codex processes were kept: their format is not recognized.']}}};
const poll = timers.filter(([fn, ms]) => ms === 5000).at(-1);
assert(poll, 'a look every 5 s');
poll[0]();
await settle();
assert.equal($('callsList').children.length, 9);
assert.equal($('callsMeta').textContent.startsWith('9 calls'), true);
// What the record could not do is said above the list.
assert.equal($('callsProblems').hidden, false);
assert.equal($('callsProblems').textContent, 'What this record could not keepRaw trace files of 2 Codex processes were kept: their format is not recognized.');
// A job whose record is gone (or that ran before records): said plainly; no list, no training download.
answers[base + '?offset=0&limit=1000'] = {status: 404, body: {error: 'This job has no record.'}};
await openModelCalls(RUN, {from: 'job'});
await settle();
assert.equal($('callsEmpty').hidden, false);
assert.equal($('callsEmpty').textContent, 'This job has no record of its model calls. Jobs started before TCS Prover '
  + 'kept records have none, and a record deleted on the Records page is gone.');
assert.equal($('callsBrowser').hidden, true);
assert.equal($('callsTrainingButton').hidden, true);
assert.equal($('callsMeta').textContent, 'No record');
// A deleted job's record: back to Records, and its title in the crumbs leads nowhere.
const gone = '2026-11-09_08-31-00_sensitivity';
answers['/jobs/' + gone + '/model-calls?offset=0&limit=1000'] = {body: {total: 0, calls: [], recordState: {available: true, problems: []}}};
await openModelCalls(gone, {from: 'records'});
await settle();
assert.equal($('callsBackText').textContent, 'Back to Records');
assert.equal(ui.callsJobCrumb.textContent, 'Prove that s(f) >= sqrt(deg(f)).');
assert.equal(ui.callsJobCrumb.disabled, true);
assert.equal($('callsEmpty').textContent, 'No model call recorded yet. Calls appear here as the job makes them.');
""".replace("RUN", json.dumps(RUN)), extra=f"const ROWS = {json.dumps(ROWS)};\n")

    def test_the_training_record_downloads_and_a_failed_download_shows_as_failed(self):
        self.run_pages(r"""
await settle();
const path = '/jobs/' + RUN + '/training-record';
const stream = (chunks, failAfter) => ({ok: true, status: 200, json: async () => ({}), body: {getReader() {
  let index = 0;
  return {async read() {
    if (failAfter !== undefined && index === failAfter) throw new TypeError('network error');
    return index < chunks.length ? {done: false, value: chunks[index++]} : {done: true};
  }};
}}});
const button = $('callsTrainingButton'), label = $('callsTrainingText'), notice = $('callsNotice');
// A whole archive: saved under the job's name.
answers[path] = () => stream([new Uint8Array(70000), new Uint8Array(30000)]);
const links = [];
const create = document.createElement;
document.createElement = (tag) => { const node = create(tag); if (tag === 'a') links.push(node); return node; };
await downloadTrainingRecord(RUN, button, label, notice);
assert.equal(links.at(-1).download, RUN + '-training-record.zip');
assert.equal(links.at(-1).clicked, 1);
assert.equal(saved.at(-1).size, 100000);
assert.equal(button.dataset.state, 'done');
assert.equal(label.textContent, 'Training record');
// The server ends a failing archive without its last chunk: the read fails, nothing is saved, the button says so.
answers[path] = () => stream([new Uint8Array(70000)], 1);
const before = saved.length;
await downloadTrainingRecord(RUN, button, label, notice);
assert.equal(saved.length, before);
assert.equal(button.dataset.state, 'failed');
assert.equal(label.textContent, 'Download failed');
assert.equal(notice.hidden, false);
assert.equal(notice.textContent, "The training record download failed (network error); nothing was saved. Try again; the server's log says why.");
// A job without a record: the route's words.
answers[path] = {status: 404, body: {error: 'This job has no record.'}};
await downloadTrainingRecord(RUN, button, label, notice);
assert.equal(label.textContent, 'Download failed');
assert.match(notice.textContent, /^The training record download failed \(This job has no record\.\)/);
""".replace("RUN", json.dumps(RUN)))

    def test_the_job_pages_way_in_counts_the_calls_and_hides_the_download_without_a_record(self):
        self.run_pages(r"""
await settle();
currentJob = RUN;
renderJobCallsEntry();
await settle();
assert.equal($('modelCallsText').textContent, 'Model calls · 8');
assert.equal($('trainingRecordButton').hidden, false);
// Read again at most every 10 s.
const reads = () => requests.filter((request) => request.path.includes('model-calls?offset=0&limit=1')).length;
renderJobCallsEntry();
await settle();
assert.equal(reads(), 1);
// The button opens the job's Model calls page.
$('modelCallsButton').onclick();
await settle();
assert.equal(callsRecord, RUN);
assert.equal(callsStore().from, 'job');
// A job without a record: "Model calls" without a count, no training download.
currentJob = 'old-job';
answers['/jobs/old-job/model-calls'] = {status: 404, body: {error: 'This job has no record.'}};
renderJobCallsEntry();
await settle();
assert.equal($('modelCallsText').textContent, 'Model calls');
assert.equal($('trainingRecordButton').hidden, true);
assert.equal($('modelCallsButton').title, 'This job has no record of its model calls.');
""".replace("RUN", json.dumps(RUN)))


class PageMarkup(HTMLParser):
    """Every element by id, with its attributes and the ids of the elements around it."""

    def __init__(self):
        super().__init__()
        self.elements, self.stack = {}, []
        self.feed(INDEX.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.elements[attrs["id"]] = {"tag": tag, "attrs": attrs, "inside": [entry for entry in self.stack if entry]}
        if tag not in {"input", "img", "br", "meta", "link", "path", "circle", "rect"}:
            self.stack.append(attrs.get("id") or attrs.get("class", "").split(" ")[0] or "")

    def handle_endtag(self, tag):
        if self.stack and tag not in {"path", "circle", "rect"}:
            self.stack.pop()


class MarkupTests(unittest.TestCase):
    def test_records_is_a_page_under_configure_with_its_size(self):
        html = INDEX.read_text(encoding="utf-8")
        configure = html[html.index('id="navConfigureLabel"'):html.index("</nav>")]
        self.assertIn('href="/?records=1" data-nav="records"', configure)
        self.assertIn('id="navRecordsSize" class="nav-meta"', configure)
        markup = PageMarkup()
        for identity in ("recordsPanel", "recordsFilter", "recordsSort", "recordsList", "recordDeleteDialog",
                         "callsPanel", "callsBrowser", "callsList", "callPane", "callsFilterText",
                         "callsTrainingButton", "modelCallsButton", "trainingRecordButton"):
            self.assertIn(identity, markup.elements)
        # The way in sits in the Live activity card's head, before its filters.
        card = html[html.index('id="activityHeading"'):html.index('id="filters"')]
        self.assertIn('id="modelCallsButton"', card)
        self.assertIn('id="trainingRecordButton"', card)
        # Sort: newest or largest first; filter: all, job kept, job deleted.
        self.assertIn('<option value="newest">Newest first</option><option value="largest">Largest first</option>',
                      html)
        self.assertEqual(re.findall(r'data-records="(\w+)"', html), ["all", "kept", "deleted"])
        self.assertEqual(re.findall(r'data-calls="(\w+)"', html), ["all", "author", "members", "critics"])

    def test_the_new_pages_use_theme_tokens_only_and_never_scroll_sideways_on_a_phone(self):
        styles = STYLES.read_text(encoding="utf-8")
        part = styles[styles.index("Records and Model calls (option B"):]
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", part)
        checked = 0
        for selector, body in rules:
            for declaration in body.split(";"):
                if ":" not in declaration:
                    continue
                name, value = (text.strip() for text in declaration.split(":", 1))
                if name in ("color", "background", "box-shadow", "outline") or name.startswith("border"):
                    checked += 1
                    with self.subTest(selector=selector.strip(), property=name):
                        self.assertNotRegex(value, r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\((?!var\()")
                        self.assertNotRegex(value, r"\b(?:white|black|red|green|blue|gray|grey|silver|orange)\b")
        self.assertGreater(checked, 40)
        # On a phone the list and the call take turns, and the records become blocks of lines.
        phone = part[part.index("@media (max-width: 720px)"):]
        self.assertIn('.mcb-browser[data-view="detail"] .mcb-nav, .mcb-browser[data-view="list"] .mcb-pane '
                      "{ display: none; }", phone)
        self.assertIn(".rec-thead { display: none; }", phone)
        # Long words wrap rather than widen the page.
        for selector in (".mc-pre {", ".mc-out {", ".mc-item-body {", ".mc-code .mc-script {"):
            start = part.index("\n" + selector)
            block = part[start:part.index("}", start)]
            self.assertIn("overflow-wrap: anywhere", block, selector)


class GuideTests(unittest.TestCase):
    def test_the_job_page_guide_describes_both_pages_in_their_own_words(self):
        guide = " ".join((ROOT / "docs" / "job-page.md").read_text(encoding="utf-8").split())
        page = INDEX.read_text(encoding="utf-8") + APP.read_text(encoding="utf-8")
        for words in ("Model calls", "Training record", "Show the text it read", "Not in this record", "Full request",
                      "Raw JSON", "All calls", "Job kept", "Job deleted", "Newest first", "Largest first",
                      "Hidden reasoning", "Records"):
            with self.subTest(words=words):
                self.assertIn(words, guide)
                self.assertIn(words, page)
        self.assertIn("the list grows as calls are recorded (the page looks every 5 seconds, every 30 once nothing "
                      "changed for a minute)", guide)
        self.assertIn("a download that fails part way saves nothing, and the button and the page say it failed", guide)
        self.assertIn("| [The job page](docs/job-page.md) | Status, the progress log, the chat, Pause, Summary, Stop, "
                      "the time limit, related works, downloads, the model calls, the training record, the Records "
                      "page |", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
