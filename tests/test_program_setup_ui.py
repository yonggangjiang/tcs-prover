"""The Setup page in the browser: routing (?setup=1), the three cards, the confirmation, a step's panel, the
sign-ins, the badge, the Overview notice, the role chips and hints, and the links to Setup from jobs.

The checks cut ui/app.js into slices by marker text and run them in Node's vm with a small fake DOM and a fake
fetch, as the other UI tests do. No server, process or network is involved.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"
INDEX = ROOT / "ui" / "index.html"
NODE = shutil.which("node")

SETUP = ["// Setup page: what this computer has and what new problems and jobs need.",
         "// Saved settings: the first panel of Advanced settings on the New problem card"]
PAGES = ["function request(", "function selectedProblemMode("]
OUTCOME = ["function completedRunLabel(", "function localDateTime("]
MODELS = ["function modelProvider(", "function renderRetrievalSettings()"]
REFRESH = ["async function refresh()", "function checkEdited()"]
POPSTATE = ["window.onpopstate = () => {", "\nrefresh();\n\n// The sticky composer footer"]
ROUTE = ["  function route() {", "  let advancedWasOpen = null;"]
COMPOSER = ["  async function openComposer(models) {", '  sidebar.addEventListener("click"']
JOB_PAGE = ["  const attention = state.needsAttention && !state.readOnly", "  renderConventionWarning();"]

RUNNER = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const {dom, slices, setup, checks} = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = slices.map(([from, to, wrap]) => {
  const start = source.indexOf(from), end = source.indexOf(to, start + 1);
  if (start < 0 || end < 0) throw new Error('Marker not found: ' + (start < 0 ? from : to));
  const text = source.slice(start, end);
  return wrap ? `function ${wrap}() {\n${text}\n}` : text;
}).join('\n');
let finished = false;
const context = {assert, console, URLSearchParams, setImmediate, finish: () => { finished = true; }};
process.on('exit', () => {
  if (!finished && !process.exitCode) { console.error('The checks did not finish.'); process.exitCode = 1; }
});
Promise.resolve(vm.runInNewContext(`${dom}\n${setup}\n${code}\n(async () => {\n${checks}\nfinish();\n})()`, context))
  .catch((error) => { console.error(error); process.exitCode = 1; });
"""

# A small DOM: elements with children, text, attributes, dataset, classes, listeners and simple selectors
# (tag, .class, [data-x], [data-x="v"], [attr], and tag[attr]). Focus behaves as in a browser: a disabled or
# hidden element takes none, and the focus falls to the body when the focused element is removed, disabled or
# hidden, or when a dialog closes whose opener is gone.
DOM = r"""
const document = {activeElement: null, listeners: {}, addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
  hidden: false};
const loseFocus = (node) => { if (document.activeElement && (document.activeElement === node
  || (node.descendants && node.descendants().includes(document.activeElement)))) document.activeElement = document.body; };
const window = {listeners: {}, addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }};
const matches = (node, selector) => {
  const match = /^([a-z0-9]*)(?:\.([\w-]+))?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/i.exec(selector.trim());
  if (!match) throw new Error('Unsupported selector ' + selector);
  const [, tag, cls, attr, value] = match;
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  if (cls && !String(node.className).split(/\s+/).includes(cls)) return false;
  if (attr) {
    const key = attr.startsWith('data-') ? attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
    const actual = key ? node.dataset[key] : node.attributes[attr];
    if (actual === undefined || (value !== undefined && actual !== value)) return false;
  }
  return true;
};
class Element {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parentElement = null; this.dataset = {};
    this.attributes = {}; this.className = ''; this._hidden = false; this._disabled = false; this.checked = false;
    this.text = ''; this.value = ''; this.listeners = {}; this.open = false; this.scrollTop = 0;
    this.clientHeight = 100; this.scrollHeight = 100;
    const list = () => String(this.className).split(/\s+/).filter(Boolean);
    this.classList = {
      contains: (name) => list().includes(name),
      add: (...names) => { this.className = [...new Set([...list(), ...names])].join(' '); },
      remove: (...names) => { this.className = list().filter((name) => !names.includes(name)).join(' '); },
      toggle: (name, force) => { const on = force === undefined ? !list().includes(name) : Boolean(force);
        on ? this.classList.add(name) : this.classList.remove(name); return on; },
    };
  }
  get hidden() { return this._hidden; }
  set hidden(value) { this._hidden = Boolean(value); if (this._hidden) loseFocus(this); }
  get disabled() { return this._disabled; }
  set disabled(value) { this._disabled = Boolean(value); if (this._disabled) loseFocus(this); }
  get isConnected() { let node = this; while (node.parentElement) node = node.parentElement; return node === document.body || node.root === true; }
  get textContent() { return this.text + this.children.map((child) => child instanceof Element ? child.textContent : String(child)).join(''); }
  set textContent(value) { this.replaceChildren(); this.text = String(value); }
  get lastChild() { return this.children.at(-1) || null; }
  append(...items) { for (const item of items) { if (item instanceof Element) { item.remove(); item.parentElement = this; } this.children.push(item); } }
  insertBefore(node, reference) { node.remove(); node.parentElement = this;
    const index = reference ? this.children.indexOf(reference) : -1;
    if (index < 0) this.children.push(node); else this.children.splice(index, 0, node); return node; }
  replaceChildren(...items) { for (const child of this.children) if (child instanceof Element) { loseFocus(child); child.parentElement = null; }
    this.children = []; this.text = ''; this.append(...items); }
  remove() { if (!this.parentElement) return; loseFocus(this); this.parentElement.children = this.parentElement.children.filter((c) => c !== this); this.parentElement = null; }
  descendants() { return this.children.flatMap((child) => child instanceof Element ? [child, ...child.descendants()] : []); }
  querySelectorAll(selector) { return this.descendants().filter((node) => matches(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (matches(node, selector)) return node; return null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { if (!this.disabled && !this.hidden) document.activeElement = this; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  showModal() { this.open = true; this.opener = document.activeElement; }
  // As a browser does: the focus goes back to the element that had it when the dialog opened, if it is still there.
  close() { this.open = false; document.activeElement = this.opener && this.opener.isConnected ? this.opener : document.body;
    for (const fn of this.listeners.close || []) fn({}); }
  click() { const event = {target: this, button: 0, preventDefault() { this.defaultPrevented = true; }};
    if (this.onclick) this.onclick(event); for (const fn of this.listeners.click || []) fn(event);
    for (const fn of document.listeners.click || []) fn(event); return event; }
}
document.createElement = (tag) => new Element(tag);
document.body = Object.assign(new Element('body'), {root: true});
document.activeElement = document.body;
const elements = {};
const $ = (id) => (elements[id] ||= Object.assign(new Element(id.endsWith('Dialog') ? 'dialog' : 'div'), {id, root: true}));
// Every text a node shows, depth first.
const texts = (node) => node.descendants().map((child) => child.text).filter(Boolean);
const find = (node, predicate) => node.descendants().find(predicate);
"""

# What the page code around the slices needs, as stubs (the real ones live elsewhere in app.js).
BASE = r"""
let currentJob = '', currentBatch = '', composingProblem = false, batchCatalog = [], timer, clock, jobsTimer;
let settingUp = false;
let state = {phase: 'input', workflow: {nodes: {}, edges: []}};
let requests = Promise.resolve(), previousPhase = '';
const sessionToken = 'token';
const location = {pathname: '/', search: '', hash: ''};
const history = {urls: [], pushState(a, b, url) { this.urls.push(url); }, replaceState(a, b, url) { this.urls.push(url); }};
const timers = [];
const setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
const clearTimeout = () => {}, setInterval = () => 0, clearInterval = () => {};
const navigator = {clipboard: {copied: [], async writeText(text) { this.copied.push(text); }}};
const ui = new Proxy({}, {get: (target, key) => (target[key] ??= Object.assign(new Element('div'), {id: String(key), root: true}))});
const timelineRows = new Map(), detailRows = new Map();
const sent = [];
let responder = () => ({status: 200, body: {}});
const fetch = async (path, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : undefined;
  sent.push({path, body});
  const answer = await responder(path, body);
  return {ok: answer.status < 400, status: answer.status, json: async () => answer.body};
};
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const confirm = () => true, alert = () => {};
const opened = [];
"""

# The page functions the Setup code calls, for the tests that load it without the rest of the page.
STUBS = r"""
const show = (element, visible) => { element.hidden = !visible; };
let clears = 0;
const clearJobView = () => { clears += 1; };
const renderBatchNavigation = () => {};
const selectedWorkflowStages = () => ({review: ui.workflowReview.checked, author: ui.workflowAuthor.checked,
  critic: ui.workflowCritic.checked, writing: ui.workflowWriting.checked});
const openAttentionJob = (runId) => opened.push(runId);
const modelProvider = (model) => (state.workflow?.settings?.model_catalog || []).find((row) => row.id === model)?.provider || '';
"""

# A GET /setup answer: the Codex CLI installed and signed in by OpenAI's installer, Claude Code missing with a job
# that needs it, LaTeX found. ``status(changes)`` returns a copy with deep changes applied.
STATUS = r"""
const codexCommand = '/bin/bash -o pipefail -c \'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh\'';
const claudeCommand = '/bin/bash -o pipefail -c \'curl -fsSL https://claude.ai/install.sh | bash\'';
const BASE_STATUS = {
  checkedAt: '2026-11-11T17:48:03Z', platform: 'macos', elevated: '', elevatedText: '',
  limits: {keptLines: 2000, lineChars: 2000, checkSeconds: 20, answerSeconds: 120},
  programs: {
    codex: {name: 'Codex CLI', state: 'works', path: '/Users/u/.local/bin/codex', shownPath: '~/.local/bin/codex',
      kind: 'installer', kindText: "installed by OpenAI's installer", offPath: '', shownOffPath: '', remembered: '',
      otherCopies: [], version: 'codex-cli 0.160.0', problem: '', target: '~/.local/bin/codex',
      signIn: {state: 'signed-in', text: 'with ChatGPT', method: 'ChatGPT'}, notes: [],
      actions: {install: {shown: false, available: false, reason: '', dialog: null},
        update: {shown: true, available: true, reason: '', dialog: {title: 'Update the Codex CLI', lede: 'TCS Prover will run OpenAI\'s official installer again; it installs the newest version:',
          command: codexCommand, what: ['Replaces codex-cli 0.160.0 with the newest version; your login and settings stay.'], warning: ''}},
        signIn: {shown: true, available: true, reason: '', label: 'Sign in again', dialog: {title: 'Sign in to the Codex CLI',
          lede: 'TCS Prover will start the Codex CLI and ask it for a ChatGPT sign-in:', legend: 'How to sign in',
          options: [{id: 'browser', label: 'In this browser', help: 'Opens the ChatGPT sign-in page here.', command: '/Users/u/.local/bin/codex app-server'},
                    {id: 'code', label: 'With a one-time code', help: 'For TCS Prover on another computer.', command: '/Users/u/.local/bin/codex app-server'}],
          what: ['You sign in on ChatGPT\'s own page; you type nothing into TCS Prover.'], warning: 'This replaces the current login (ChatGPT).', button: 'Start sign-in'}},
        useFolder: {shown: false, available: false, reason: '', folder: ''}},
      commands: [{group: 'update', label: 'In a terminal', command: 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'},
                 {group: 'sign-in', label: 'Sign in in a terminal', command: 'codex login'}],
      task: null},
    claude: {name: 'Claude Code', state: 'missing', path: '', shownPath: '', kind: '', kindText: '', offPath: '',
      shownOffPath: '', remembered: '', otherCopies: [], version: '', problem: '', target: '~/.local/bin/claude',
      signIn: {state: 'not-checked', text: '', method: ''}, notes: [],
      actions: {install: {shown: true, available: true, reason: '', dialog: {title: 'Install Claude Code',
          lede: 'TCS Prover will run Anthropic\'s official installer:', command: claudeCommand,
          what: ['Downloads Claude Code from claude.ai and downloads.claude.ai and checks its checksum.', 'Needs no administrator rights.'],
          warning: '', checkbox: {label: 'Then sign in with your Claude account', checked: true, option: 'subscription'}}},
        update: {shown: false, available: false, reason: '', dialog: null},
        signIn: {shown: false, available: false, reason: '', dialog: null},
        useFolder: {shown: false, available: false, reason: '', folder: ''}},
      commands: [{group: 'install', label: 'Official installer, in a terminal', command: 'curl -fsSL https://claude.ai/install.sh | bash'},
                 {group: 'install', label: 'Homebrew', command: 'brew install --cask claude-code'}],
      task: null},
    latex: {name: 'LaTeX', state: 'works', problem: '', help: 'TCS Prover does not run these.', notes: [],
      tools: [{name: 'latexmk', path: '/Library/TeX/texbin/latexmk', shownPath: '/Library/TeX/texbin/latexmk', onPath: true, version: 'latexmk 4.77', problem: ''},
              {name: 'pdflatex', path: '/Library/TeX/texbin/pdflatex', shownPath: '/Library/TeX/texbin/pdflatex', onPath: true, version: 'pdfTeX 3.14 (TeX Live 2022)', problem: ''}],
      commands: [{group: 'install', label: 'MacTeX', text: 'Download it from', link: 'https://tug.org/mactex/'},
                 {group: 'install', label: 'Homebrew', command: 'brew install --cask mactex-no-gui', note: 'About 5 GB.'}]}},
  defaults: {source: 'saved', name: 'Claude critic', roles: [
    {role: 'review', stage: 'review', on: true, model: 'gpt-6.1-sol', modelLabel: 'GPT 6.1 Sol', program: 'codex', signIn: true, connection: ''},
    {role: 'author', stage: 'author', on: true, model: 'gpt-6.1-sol', modelLabel: 'GPT 6.1 Sol', program: 'codex', signIn: true, connection: ''},
    {role: 'critic', stage: 'critic', on: true, model: 'claude-opus-5-5', modelLabel: 'Claude Opus 5.5', program: 'claude', signIn: true, connection: ''},
    {role: 'writer', stage: 'writing', on: true, model: 'gpt-6.1-sol', modelLabel: 'GPT 6.1 Sol', program: 'codex', signIn: true, connection: ''}]},
  jobs: {codex: {count: 2, running: 1, signIn: 2, items: [{runId: 'r1', title: 'Online matching', running: true, batchId: '__root__', status: 'running now'},
                                                          {runId: 'r2', title: 'Expanders', running: false, batchId: '__root__', status: 'paused'}]},
         claude: {count: 1, running: 0, signIn: 1, items: [{runId: 'r1', title: 'Online <img src=x onerror=alert(1)> matching', running: false, batchId: '__root__', status: 'paused'}]},
         latex: {count: 2, running: 0, items: []}},
  connections: {codex: ['Lab key'], claude: []},
};
const status = (change = (value) => value) => change(JSON.parse(JSON.stringify(BASE_STATUS)));
"""


def page():
    """Every element id in index.html with its tag and attributes."""

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.elements = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.elements[attrs["id"]] = {"tag": tag, **attrs}

    parser = Parser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser.elements


@unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
class SetupPageTests(unittest.TestCase):
    def run_app(self, checks, slices=(SETUP,), setup=""):
        # A test of the Setup code alone gets the page functions it calls as stubs; one with the page's own
        # slices (PAGES) gets the real ones.
        stubs = "" if any(list(item[:2]) == PAGES for item in slices) else STUBS
        payload = json.dumps({"dom": DOM, "slices": [list(item) for item in slices],
                              "setup": BASE + stubs + STATUS + setup, "checks": checks})
        result = subprocess.run([NODE, "-e", RUNNER, str(APP)], input=payload, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_markup_has_the_page_the_dialog_the_badge_and_the_hints(self):
        elements = page()
        self.assertEqual(elements["setupPanel"]["tag"], "section")
        self.assertIn("hidden", elements["setupPanel"])
        self.assertEqual(elements["setupList"]["role"], "list")
        self.assertEqual((elements["setupDialog"]["tag"], elements["setupDialog"]["class"]),
                         ("dialog", "prompt-dialog setup-dialog"))
        self.assertIn("autofocus", elements["setupDialogCancel"])
        self.assertEqual(elements["navSetupBadge"]["aria-hidden"], "true")
        for name in ("modelSetupHint", "startSetupHint", "setupBanner"):
            self.assertIn("hidden", elements[name])
        html = INDEX.read_text(encoding="utf-8")
        # Setup comes first under Configure, before Models & API.
        self.assertLess(html.index('data-nav="setup"'), html.index('data-nav="models"'))
        self.assertLess(html.index('id="setupBanner"'), html.index('id="attentionBanner"'))
        self.assertIn("The Codex CLI must be installed on this computer (see Setup), but needs no login.", html)

    def test_the_cards_show_each_program_with_its_buttons_reasons_and_commands(self):
        self.run_app(r"""
settingUp = true;
responder = () => ({status: 200, body: status((s) => {
  s.programs.codex.actions.update = {shown: true, available: false, reason: 'Unavailable while 1 job uses the Codex CLI: “Online matching”. Pause or stop it, or let it finish.', dialog: s.programs.codex.actions.update.dialog};
  s.programs.codex.shownPath = '~/<img src=x onerror=alert(1)>/codex';
  return s; })});
await loadSetup(true);
await settle();
const els = setupEls();
assert.equal(els.meta.children.map((node) => node.textContent).join(' | '), 'Codex CLI 0.160.0 | Claude Code not installed | LaTeX found | checked ' + new Date('2026-11-11T17:48:03Z').toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}));
const cards = els.list.children;
assert.deepEqual(cards.map((card) => card.dataset.program), ['codex', 'claude', 'latex']);
assert.deepEqual(cards.map((card) => card.className), ['setup-card card tone-done', 'setup-card card tone-failed', 'setup-card card tone-done']);
assert.deepEqual(cards.map((card) => card.querySelector('.setup-pill').textContent), ['Installed', 'Not installed', 'Found']);
const codex = cards[0];
// A path is text, never markup.
assert(texts(codex).includes('~/<img src=x onerror=alert(1)>/codex'));
assert.equal(codex.querySelectorAll('img').length, 0);
const update = codex.querySelectorAll('button').find((button) => button.textContent === 'Update');
assert.equal(update.disabled, true);
const reason = codex.querySelector('.setup-reason');
assert.equal(reason.textContent, 'Unavailable while 1 job uses the Codex CLI: “Online matching”. Pause or stop it, or let it finish.');
assert.equal(update.getAttribute('aria-describedby'), reason.id);
assert.equal(codex.querySelectorAll('button').find((button) => button.textContent === 'Sign in again').className, 'ghost compact');
// What needs it: the New problem roles (the default settings), the jobs, the API connections.
const needs = codex.querySelector('.setup-needs');
assert.equal(needs.children[0].textContent, 'New problem: Statement reviewer, Proof author and LaTeX writer (GPT 6.1 Sol)');
assert.equal(needs.querySelector('summary').textContent, '2 jobs (1 running now)');
assert.equal(needs.children[2].textContent, 'API connections: “Lab key” (no sign-in needed)');
const claude = cards[1];
const install = claude.querySelectorAll('button').find((button) => button.textContent === 'Install');
assert.equal(install.className, 'primary compact');
assert.equal(claude.querySelector('.setup-needs').children[0].textContent, 'New problem: Independent critic (Claude Opus 5.5)');
// Its jobs are open (Claude Code is needed and missing); a job title is text.
const jobs = claude.querySelector('details');
assert.equal(jobs.open, true);
assert(texts(jobs).includes('Online <img src=x onerror=alert(1)> matching'));
jobs.querySelector('a').onclick({button: 0, preventDefault() {}});
assert.deepEqual(opened, ['r1']);
// The commands: closed while Install runs it; LaTeX's link opens in a new tab, safely.
const ways = claude.querySelector('details.setup-commands');
assert.equal(ways.querySelector('summary').textContent, 'Other ways to install');
assert.equal(ways.open, false);
const latex = cards[2];
const link = latex.querySelector('a');
assert.equal(link.href, 'https://tug.org/mactex/');
assert.equal(link.rel, 'noopener noreferrer');
assert.equal(link.target, '_blank');
assert.deepEqual(latex.querySelector('.setup-needs').children.map((node) => node.textContent), ['New problem: Writing & polishing', '2 jobs']);
// The limits are said on the page, also the Codex sign-in's answer time.
assert.equal(els.limits.textContent, "Each step's output keeps its newest 2,000 lines, and each check waits at most 20 seconds for a program to answer. Installs, updates and sign-ins have no time limit (in a Codex sign-in, each answer of the Codex CLI must come within 120 seconds); Stop ends them.");
""")

    def test_copy_only_steps_open_their_commands_and_the_administrator_gets_no_buttons(self):
        self.run_app(r"""
settingUp = true;
let answer = status((s) => {
  s.programs.claude.actions.install = {shown: false, available: false, reason: '', dialog: null};
  s.programs.claude.notes = ['~/.local/bin/claude is your own script, so it decides which Claude Code runs.'];
  return s; });
responder = () => ({status: 200, body: answer});
await loadSetup(true);
const claude = setupEls().list.children[1];
const ways = claude.querySelector('details.setup-commands');
assert.equal(ways.querySelector('summary').textContent, 'How to install');
assert.equal(ways.open, true);
assert.equal(claude.querySelector('.setup-note').textContent, '~/.local/bin/claude is your own script, so it decides which Claude Code runs.');
assert.equal(claude.querySelectorAll('button').filter((button) => button.textContent === 'Install').length, 0);
const copy = claude.querySelectorAll('button').find((button) => button.textContent === 'Copy');
await copy.onclick();
assert.deepEqual(navigator.clipboard.copied, ['curl -fsSL https://claude.ai/install.sh | bash']);
assert.equal(copy.textContent, 'Copied');
answer = status((s) => ({...s, elevated: 'root', elevatedText: 'TCS Prover is running with administrator rights (root).'}));
await loadSetup(true);
assert.equal(setupEls().notice.textContent, 'TCS Prover is running with administrator rights (root).');
assert.equal(setupEls().notice.hidden, false);
assert.equal(setupEls().list.querySelectorAll('button').filter((button) => !button.className.includes('setup-copy')).length, 0);
""")

    def test_a_server_without_setup_says_so_and_shows_no_badge(self):
        self.run_app(r"""
settingUp = true;
responder = () => ({status: 404, body: {error: 'Not found.'}});
await loadSetup(true);
const els = setupEls();
assert.equal(els.notice.textContent, 'This TCS Prover server has no Setup page. Restart TCS Prover to update it, or open it from its own launch link.');
assert.equal(els.list.hidden, true);
assert.equal(els.check.disabled, true);
renderSetupBadge();
assert.equal(els.badge.hidden, true);
// Polls stop: nothing more is asked.
const before = sent.length;
await loadSetup('poll');
assert.equal(sent.length, before);
""")

    def test_what_needs_each_program_feeds_the_badge_the_notice_the_chips_and_the_hints(self):
        self.run_app(r"""
responder = () => ({status: 200, body: status((s) => { s.programs.codex.signIn = {state: 'signed-out', text: '', method: ''}; return s; })});
await loadSetup();
await settle();
// Two programs need setting up: Claude Code (missing, a role and a job use it) and the Codex CLI (signed out).
renderSetupBadge();
assert.equal(setupEls().badge.textContent, '2');
assert.equal(setupEls().badge.hidden, false);
assert.equal(setupEls().words.textContent, ', 2 programs need setting up');
renderSetupBanner();
const items = setupEls().bannerList.children;
assert.deepEqual(items.map((item) => item.children[0].textContent), ['Codex CLI not signed in', 'Claude Code not installed']);
assert.deepEqual(items.map((item) => item.children[1].textContent), ['New problems and 2 jobs use your ChatGPT login through it.', 'New problems and 1 job use it.']);
assert.equal(items[0].children[2].getAttribute ? items[0].children[2].href : '', '/?setup=1');
assert.equal(setupEls().banner.hidden, false);
// The chips under the role menus (an API model needs no sign-in).
assert.equal(setupChipSuffix('claude'), ' · not installed');
assert.equal(setupChipSuffix('codex'), ' · not signed in');
assert.equal(setupChipSuffix('api', {format: 'openai'}), '');
assert.equal(setupChipSuffix('api', {format: 'anthropic'}), ' · not installed');
// The hints follow the form when it is open: here the critic uses Claude Code, the others a Codex model.
composingProblem = true;
state = {phase: 'input', workflow: {settings: {model_catalog: [
  {id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol', provider: 'codex'}, {id: 'claude-opus-5-5', label: 'Claude Opus 5.5', provider: 'claude'},
  {id: 'api:lab', label: 'Lab model', provider: 'api', format: 'openai', sourceLabel: 'Lab key'}]}}};
for (const [role, model] of [['review', 'api:lab'], ['author', 'gpt-6.1-sol'], ['critic', 'claude-opus-5-5'], ['writer', 'gpt-6.1-sol']]) {
  ui[role + 'Model'].value = model;
}
for (const stage of ['workflowReview', 'workflowAuthor', 'workflowCritic', 'workflowWriting']) ui[stage].checked = true;
const needs = setupNeeds();
assert.equal(setupRoleGroups(needs.codex.roles), 'Statement reviewer (API connection “Lab key”, no sign-in needed); Proof author and LaTeX writer (GPT 6.1 Sol)');
renderSetupHints();
assert.equal(setupEls().modelHint.textContent, 'The Codex CLI is not signed in; the proof author and LaTeX writer use your ChatGPT login through it. Claude Code is not installed on this computer; the independent critic uses it. Open Setup');
assert.equal(setupEls().startHint.textContent, 'The Codex CLI is not signed in; the proof author and LaTeX writer use your ChatGPT login through it. Claude Code is not installed on this computer; the independent critic uses it. You can start now: the job retries every minute until it works. Open Setup');
assert.equal(setupEls().modelHint.hidden, false);
// Writing off: LaTeX is not needed by the form.
ui.workflowWriting.checked = false;
assert.equal(setupNeeds().latex.roles.length, 0);
""", slices=(SETUP,))

    def test_a_problem_of_an_author_subagents_program_says_what_happens_to_its_assignments(self):
        # A role's model call is retried every minute once the job runs; an author subagent's assignment is not: a
        # missing program comes back to the coordinator as a failed assignment with the reason (member_problem()).
        self.run_app(r"""
responder = () => ({status: 200, body: status()});
await loadSetup();
await settle();
composingProblem = true;
state = {phase: 'input', workflow: {settings: {model_catalog: [
  {id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol', provider: 'codex'},
  {id: 'claude-opus-5-5', label: 'Claude Opus 5.5', provider: 'claude'}]}}};
for (const role of ['review', 'author', 'critic', 'writer']) ui[role + 'Model'].value = 'gpt-6.1-sol';
for (const stage of ['workflowReview', 'workflowAuthor', 'workflowCritic', 'workflowWriting']) ui[stage].checked = true;
// Choose for each: only subagent 2 runs a Claude model, and Claude Code is not installed.
globalThis.teamModelsSetting = () => [{model: 'gpt-6.1-sol', effort: 'high'}, {model: 'claude-opus-5-5', effort: 'max'},
  {model: 'gpt-6.1-sol', effort: 'high'}];
renderSetupHints();
const problem = 'Claude Code is not installed on this computer; the author subagent 2 uses it.';
assert.equal(setupEls().startHint.textContent, `${problem} You can start now, but until this is fixed the `
  + "coordinator's assignments to author subagent 2 fail and come back to it with the reason. Open Setup");
assert.equal(setupEls().modelHint.textContent, `${problem} Open Setup`);
// A role on Claude Code too: the job retries every minute, and the subagent's assignments still come back.
ui.criticModel.value = 'claude-opus-5-5';
renderSetupHints();
assert.equal(setupEls().startHint.textContent, 'Claude Code is not installed on this computer; the independent '
  + 'critic and author subagent 2 use it. You can start now: the job retries every minute until it works, and until '
  + "then the coordinator's assignments to author subagent 2 fail and come back to it with the reason. Open Setup");
// A role alone, as before.
globalThis.teamModelsSetting = () => null;
renderSetupHints();
assert.equal(setupEls().startHint.textContent, 'Claude Code is not installed on this computer; the independent '
  + 'critic uses it. You can start now: the job retries every minute until it works. Open Setup');
""", slices=(SETUP,))

    def test_the_dialog_shows_the_exact_command_and_cancel_sends_nothing(self):
        self.run_app(r"""
settingUp = true;
let answer = status();
responder = (path, body) => path.startsWith('/setup/action')
  ? {status: 409, body: {error: 'The command changed since you opened this dialog. Check it and confirm again.',
      status: status((s) => { s.programs.claude.actions.install.dialog.command = '/bin/bash -o pipefail -c NEW'; return s; })}}
  : {status: 200, body: answer};
await loadSetup(true);
openSetupDialog('claude', 'install');
const els = setupEls();
assert.equal(els.dialog.open, true);
assert.equal(document.activeElement, els.dialogCancel);  // Enter never starts a step by accident.
assert.equal(els.dialogTitle.textContent, 'Install Claude Code');
assert.equal(els.dialogCommand.textContent, claudeCommand);
assert.deepEqual(els.dialogWhat.children.map((item) => item.textContent), BASE_STATUS.programs.claude.actions.install.dialog.what);
assert.equal(els.dialogThen.hidden, false);  // A role signs in with Claude: install, then sign in.
assert.equal(els.dialogThenText.textContent, 'Then sign in with your Claude account');
assert.equal(els.dialogConfirm.textContent, 'Install');
const before = sent.length;
els.dialog.close();  // Cancel or Escape.
assert.equal(sent.length, before);
openSetupDialog('claude', 'install');
await confirmSetup();
await settle();
const post = sent.find((request) => request.path === '/setup/action');
assert.deepEqual(post.body, {program: 'claude', action: 'install', command: claudeCommand, signInAfter: true, signInOption: 'subscription'});
// A 409 shows the new command and why; the dialog stays open.
assert.equal(els.dialog.open, true);
assert.equal(els.dialogCommand.textContent, '/bin/bash -o pipefail -c NEW');
assert.equal(els.dialogError.textContent, 'The command changed since you opened this dialog. Check it and confirm again.');
assert.equal(els.dialogError.hidden, false);
""")

    def test_a_sign_in_dialog_sends_the_chosen_option_and_its_command(self):
        self.run_app(r"""
settingUp = true;
const signing = {id: 's-1', program: 'codex', action: 'sign-in', title: 'Signing in to the Codex CLI', state: 'running', lines: [], first: 0, next: 0, dropped: 0, partial: '', command: 'x', input: {sent: false, message: ''}, result: null, nextTask: '', keptLines: 2000};
responder = (path) => path.startsWith('/setup/action') || path.startsWith('/setup/task')
  ? {status: 200, body: {task: signing}} : {status: 200, body: status()};
await loadSetup(true);
openSetupDialog('codex', 'sign-in');
const els = setupEls();
assert.equal(els.dialogOptions.hidden, false);
assert.equal(els.dialogLegend.textContent, 'How to sign in');
assert.equal(els.dialogWarning.textContent, 'This replaces the current login (ChatGPT).');
assert.equal(els.dialogThen.hidden, true);
assert.equal(els.dialogConfirm.textContent, 'Start sign-in');
const radios = els.dialogOptionList.querySelectorAll('input');
radios[1].checked = true;
radios[1].onchange();
await confirmSetup();
const post = sent.find((request) => request.path === '/setup/action');
assert.deepEqual(post.body, {program: 'codex', action: 'sign-in', command: '/Users/u/.local/bin/codex app-server', option: 'code'});
assert.equal(els.dialog.open, false);
""")

    def test_choosing_a_sign_in_option_keeps_the_focus_on_it(self):
        self.run_app(r"""
settingUp = true;
responder = () => ({status: 200, body: status((s) => {
  s.programs.claude = {...s.programs.claude, state: 'works', path: '/u/.local/bin/claude', signIn: {state: 'signed-out', text: '', method: ''}};
  s.programs.claude.actions.signIn = {shown: true, available: true, reason: '', label: 'Sign in', dialog: {title: 'Sign in to Claude Code',
    lede: 'TCS Prover will run Claude Code\'s sign-in:', legend: 'Account', warning: '', button: 'Start sign-in', what: [],
    options: [{id: 'subscription', label: 'Claude subscription', help: '', command: '/u/.local/bin/claude auth login'},
              {id: 'console', label: 'Anthropic Console account', help: '', command: '/u/.local/bin/claude auth login --console'}]}};
  return s; })});
await loadSetup(true);
const els = setupEls();
openSetupDialog('claude', 'sign-in');
const radios = els.dialogOptionList.querySelectorAll('input');
assert.deepEqual(radios.map((radio) => radio.checked), [true, false]);
// Arrow down (or a click) on the second option: the same radios stay, the focus stays on the chosen one.
radios[1].focus();
radios[1].checked = true;
radios[1].onchange();
const after = els.dialogOptionList.querySelectorAll('input');
assert.equal(after[1], radios[1]);
assert.equal(document.activeElement, radios[1]);
assert.deepEqual(after.map((radio) => radio.checked), [false, true]);
assert.equal(els.dialogCommand.textContent, '/u/.local/bin/claude auth login --console');
// And back.
radios[0].focus();
radios[0].checked = true;
radios[0].onchange();
assert.equal(document.activeElement, radios[0]);
assert.equal(els.dialogCommand.textContent, '/u/.local/bin/claude auth login');
// Another dialog draws its own options, which follow the dialog that is open now.
els.dialog.close();
openSetupDialog('codex', 'sign-in');
const codex = els.dialogOptionList.querySelectorAll('input');
assert.notEqual(codex[0], radios[0]);
codex[1].checked = true;
codex[1].onchange();
assert.equal(setupStore().dialog.option, 'code');
""")

    def test_the_focus_survives_a_check_a_redraw_and_a_refusal(self):
        self.run_app(r"""
settingUp = true;
let hold = false, release = null;
let answer = status();
responder = (path) => {
  if (path === '/setup/action') return {status: 409, body: {error: 'The command changed since you opened this dialog. Check it and confirm again.',
    status: status((s) => { s.programs.codex.actions.update.dialog.command = 'NEW'; return s; })}};
  if (hold && path.startsWith('/setup?refresh')) return new Promise((resolve) => { release = () => resolve({status: 200, body: answer}); });
  return {status: 200, body: answer};
};
await loadSetup(true);
await settle();
const els = setupEls();
const button = (program, label) => els.list.querySelector(`[data-program="${program}"]`).querySelectorAll('button').find((item) => item.textContent === label);
button('codex', 'Update').focus();
// A return to the window checks again: the buttons wait, keep the focus, and do nothing while they wait.
hold = true;
setupStore().refreshedAt = 0;
window.listeners.focus.forEach((listener) => listener());
await settle();
assert.equal(setupStore().checking, 1);
assert.equal(document.activeElement, button('codex', 'Update'));
assert.equal(button('codex', 'Update').getAttribute('aria-disabled'), 'true');
button('codex', 'Update').click();
assert.equal(els.dialog.open, false);
assert.equal(els.check.getAttribute('aria-disabled'), 'true');
assert.equal(els.check.disabled, false);
release();
await settle();
assert.equal(setupStore().checking, 0);
assert.equal(document.activeElement, button('codex', 'Update'));
assert.equal(button('codex', 'Update').getAttribute('aria-disabled'), null);
// Check again keeps the focus too.
hold = false;
els.check.focus();
els.check.onclick();
await settle();
assert.equal(document.activeElement, els.check);
// A disclosure and a Copy button keep the focus across a redraw (here, new job data).
const summary = els.list.querySelector('[data-program="codex"]').querySelector('summary');
summary.focus();
answer = status((s) => { s.jobs.codex.items[1].status = 'paused · usage limit reached'; return s; });
await loadSetup('poll');
assert.equal(document.activeElement.dataset.setupFocus, summary.dataset.setupFocus);
assert.notEqual(document.activeElement, summary);
// A refusal after which the step is no longer offered (your own script where the installer writes): nothing to
// confirm again, and the dialog says so.
const unoffered = {status: 409, body: {error: 'The command changed since you opened this dialog. Check it and confirm again.',
  status: status((s) => { s.programs.codex.actions.update = {shown: false, available: false, reason: '', dialog: null}; return s; })}};
const before = responder;
responder = (path) => path === '/setup/action' ? unoffered : before(path);
button('codex', 'Update').focus();
openSetupDialog('codex', 'update');
await confirmSetup();
await settle();
assert.equal(els.dialog.open, true);
assert.equal(els.dialogConfirm.disabled, true);
assert.equal(els.dialogError.textContent, 'This step is not offered now. Close this and look at the page again.');
els.dialog.close();
assert.equal(document.activeElement, button('codex', 'Sign in again'));  // Update is gone: its card's first button.
responder = before;
await loadSetup(true);
// Escape after a refusal: the button that opened the dialog was drawn again; it gets the focus back.
button('codex', 'Update').focus();
openSetupDialog('codex', 'update');
await confirmSetup();
await settle();
assert.equal(els.dialog.open, true);
assert.equal(els.dialogCommand.textContent, 'NEW');
els.dialog.close();
assert.equal(document.activeElement, button('codex', 'Update'));
""")

    def test_use_it_for_jobs_and_stop_hand_the_focus_on(self):
        self.run_app(r"""
settingUp = true;
const offpath = status((s) => {
  s.programs.codex = {...s.programs.codex, state: 'offpath', kind: 'installer', offPath: '/u/.local/bin', shownOffPath: '~/.local/bin', version: ''};
  s.programs.codex.actions = {install: {shown: false, available: false, reason: '', dialog: null}, update: {shown: false, available: false, reason: '', dialog: null},
    signIn: {shown: false, available: false, reason: '', dialog: null}, useFolder: {shown: true, available: true, reason: '', folder: '/u/.local/bin'}};
  return s; });
const step = (changes) => ({id: 's-9', program: 'claude', action: 'install', option: '', title: 'Installing Claude Code',
  state: 'running', stopping: false, elapsedSeconds: 1, command: claudeCommand, lines: ['Downloading'], first: 0, next: 1, dropped: 0,
  partial: '', link: null, code: null, input: {sent: false, message: ''}, result: null, nextTask: '', keptLines: 2000,
  signInAfter: false, checking: false, ...changes});
let next = step();
responder = (path) => {
  if (path === '/setup/action') return {status: 200, body: {status: status()}};
  if (path === '/setup/stop') return {status: 200, body: {task: step({stopping: true})}};
  if (path.startsWith('/setup/task')) return {status: 200, body: {task: next}};
  return {status: 200, body: offpath};
};
await loadSetup(true);
const els = setupEls();
const card = (program) => els.list.querySelector(`[data-program="${program}"]`);
const use = card('codex').querySelectorAll('button').find((item) => item.textContent === 'Use it for jobs');
use.focus();
use.click();
assert.equal(use.getAttribute('aria-disabled'), 'true');  // It waits, and keeps the focus.
assert.equal(document.activeElement, use);
await settle();
// Use it for jobs is gone: the card's first new button has the focus.
assert.equal(document.activeElement, card('codex').querySelectorAll('button')[0]);
assert.equal(document.activeElement.textContent, 'Update');
// Stop: it waits while the step stops, then the result has the focus.
mergeSetupTask('claude', step());
renderSetupTasks();
const panel = setupStore().panels.claude;
panel.stop.focus();
await stopSetupTask('claude');
assert.equal(panel.stop.getAttribute('aria-disabled'), 'true');
assert.equal(panel.stop.textContent, 'Stopping…');
assert.equal(document.activeElement, panel.stop);
await settle();  // The read that Stop starts.
next = step({state: 'stopped', stopping: false, result: {tone: 'stopped', text: 'Stopped. Run Install again to finish or repair it.', exitCode: -15}});
await pollSetupTask('claude', 's-9');
await settle();
assert.equal(panel.stop.hidden, true);
assert.equal(document.activeElement, panel.result);
""")

    def test_a_panel_that_stays_keeps_the_focus_in_the_code_box(self):
        self.run_app(r"""
settingUp = true;
responder = () => ({status: 200, body: status()});
await loadSetup(true);
const signing = {id: 's-2', program: 'claude', action: 'sign-in', option: 'subscription', title: 'Signing in to Claude Code',
  state: 'waiting', stopping: false, elapsedSeconds: 3, command: 'claude auth login', lines: [], first: 0, next: 0, dropped: 0,
  partial: '', code: null, input: {sent: false, message: ''}, result: null, nextTask: '', keptLines: 2000, checking: false,
  link: {url: 'https://claude.ai/oauth/authorize?state=S', trusted: true, host: 'claude.ai'}};
mergeSetupTask('claude', signing);
renderSetupTasks();
const input = setupStore().panels.claude.box.parts.input;
input.focus();
input.value = 'abc#de';
// A Codex step starts from another tab: its panel comes first, the Claude panel stays where it is.
mergeSetupTask('codex', {...signing, id: 's-3', program: 'codex', action: 'install', title: 'Installing the Codex CLI', state: 'running', link: null});
renderSetupTasks();
assert.deepEqual(setupEls().tasks.children.map((node) => node.id), ['setupTask-codex', 'setupTask-claude']);
assert.equal(document.activeElement, input);
assert.equal(input.isConnected, true);
assert.equal(input.value, 'abc#de');
// The Codex panel goes (hidden): the Claude panel still keeps the focus.
setupStore().hidden.add('s-3');
renderSetupTasks();
assert.deepEqual(setupEls().tasks.children.map((node) => node.id), ['setupTask-claude']);
assert.equal(document.activeElement, input);
""")

    def test_a_check_that_failed_is_said_and_never_freezes_the_page(self):
        self.run_app(r"""
settingUp = true;
// The server could not check LaTeX or the Codex CLI (a full disk): it says why, and LaTeX has no tools.
responder = () => ({status: 200, body: status((s) => {
  s.programs.latex = {name: 'LaTeX', state: 'unknown', problem: 'TCS Prover could not check LaTeX (OSError). Click Check again.', tools: {}, commands: s.programs.latex.commands, help: '', notes: []};
  s.programs.codex = {...s.programs.codex, state: 'unknown', problem: 'TCS Prover could not check the Codex CLI (OSError). Click Check again.',
    path: '', shownPath: '', kind: '', kindText: '', version: '', signIn: {state: 'unknown', text: '', method: ''},
    actions: {install: {shown: false}, update: {shown: false}, signIn: {shown: false}, useFolder: {shown: false}}, commands: []};
  return s; })});
await loadSetup(true);
const els = setupEls();
const cards = els.list.children;
assert.deepEqual(cards.map((card) => card.querySelector('.setup-pill').textContent), ['Could not check', 'Not installed', 'Could not check']);
assert.equal(cards[0].querySelector('.setup-problem').textContent, 'TCS Prover could not check the Codex CLI (OSError). Click Check again.');
assert.equal(cards[2].querySelector('.setup-problem').textContent, 'TCS Prover could not check LaTeX (OSError). Click Check again.');
assert.equal(els.meta.children[0].textContent, 'Codex CLI could not be checked');
assert.equal(els.meta.children[2].textContent, 'LaTeX could not be checked');
// A first check that fails (500): "Could not check", never a "Checking…" that goes on.
setupStore.value = undefined;
responder = () => ({status: 500, body: {error: 'Setup failed (RuntimeError)'}});
await loadSetup(true);
assert.equal(els.meta.textContent, 'Could not check');
assert.deepEqual(els.list.children.map((card) => card.querySelector('.setup-pill').textContent), ['Could not check', 'Could not check', 'Could not check']);
assert.equal(els.notice.textContent, 'TCS Prover could not check the programs: Setup failed (RuntimeError). Click Check again.');
// An answer the page cannot draw says so, and the next read still comes.
responder = () => ({status: 200, body: status((s) => { s.programs.codex.notes = 5; return s; })});
const before = timers.length;
await loadSetup(true);
assert.equal(els.notice.textContent, 'The Setup page could not show the programs (TypeError). Click Check again.');
assert.equal(els.notice.hidden, false);
assert.equal(timers.length, before + 1);
assert.equal(timers.at(-1).ms, 5000);
""")

    def test_each_read_asks_for_what_it_may_start(self):
        self.run_app(r"""
let answer = status();
responder = () => ({status: 200, body: answer});
// The first read of any page: the last check younger than ten minutes, else a new one.
await loadSetup();
assert.equal(sent.at(-1).path, '/setup');
// The background read every minute (every 5 s on Setup) never starts a process.
await loadSetup('poll');
assert.equal(sent.at(-1).path, '/setup?cached=1');
assert.equal(timers.at(-1).ms, 60000);
// Opening Setup, Check again and a return to the window on Setup check again.
await loadSetup(true);
assert.equal(sent.at(-1).path, '/setup?refresh=1');
// A server with no check yet (it restarted) answers the background read without one: the page keeps its own
// last check, with the fresh job data.
answer = status((s) => {
  s.checkedAt = '';
  for (const program of ['codex', 'claude', 'latex']) s.programs[program] = {state: 'checking', tools: []};
  s.jobs.codex.count = 3;
  return s; });
await loadSetup('poll');
assert.equal(setupStore().data.checkedAt, '2026-11-11T17:48:03Z');
assert.equal(setupStore().data.programs.codex.state, 'works');
assert.equal(setupStore().data.jobs.codex.count, 3);
""")

    def test_a_step_panel_follows_the_output_stops_and_says_the_result(self):
        self.run_app(r"""
settingUp = true;
const step = (changes) => ({id: 's-1', program: 'claude', action: 'install', option: '', title: 'Installing Claude Code',
  state: 'running', stopping: false, elapsedSeconds: 42, command: claudeCommand, lines: [], first: 0, next: 0, dropped: 0,
  partial: '', link: null, code: null, input: {sent: false, message: ''}, result: null, nextTask: '', keptLines: 2000,
  signInAfter: true, ...changes});
const answers = [step({lines: ['Checking this computer', 'Downloading'], next: 2}),
                 step({lines: Array.from({length: 2000}, (_, index) => `line ${501 + index}`), first: 501, next: 2501, dropped: 501})];
const asked = [];
responder = (path, body) => {
  if (path.startsWith('/setup/task')) { asked.push(path); return {status: 200, body: {task: answers.shift() || step({state: 'stopped', next: 2501, lines: [], result: {tone: 'stopped', text: 'Stopped. Run Install again to finish or repair it.', exitCode: 143}})}}; }
  if (path === '/setup/stop') return {status: 200, body: {task: step({stopping: true, next: 2501})}};
  return {status: 200, body: status((s) => { s.programs.claude.task = {id: 's-1', action: 'install', state: 'running', title: 'Installing Claude Code'}; return s; })};
};
await loadSetup(true);
await settle();
const panel = setupStore().panels.claude;
assert.equal(asked[0], '/setup/task?id=s-1&after=0');
assert.equal(panel.log.textContent, 'Checking this computer\nDownloading');
assert.equal(panel.elapsed.textContent, '0:42');
assert.equal(panel.stop.hidden, false);
assert.deepEqual(panel.note.children.map((line) => [line.className, line.textContent]), [
  ['setup-note-line', "Claude Code's installer shows no progress while it downloads; this can take a few minutes."],
  ['setup-note-line setup-next', 'Next: sign in with your Claude account, once the install succeeds.']]);
// Once the installer has ended, the panel says its result is being checked.
mergeSetupTask('claude', step({lines: ['Checking this computer', 'Downloading'], next: 2, checking: true}));
renderSetupTasks();
assert.equal(panel.note.children[1].textContent, 'The installer has ended; TCS Prover is checking the result…');
mergeSetupTask('claude', step({lines: ['Checking this computer', 'Downloading'], next: 2}));
renderSetupTasks();
// The card says where the output is, instead of its buttons.
const card = setupEls().list.children[1];
assert.equal(card.querySelector('.setup-pill').textContent, 'Installing…');
assert.equal(card.querySelector('.setup-card-busy').textContent, '0:42 · Output and Stop below');
// The next poll asks only for the lines after the ones the page has; dropped lines are said.
await pollSetupTask('claude', 's-1');
assert.equal(asked[1], '/setup/task?id=s-1&after=2');
assert.equal(panel.dropped.textContent, 'Earlier output: 501 lines are not kept (the newest 2,000 are shown).');
assert.equal(panel.log.textContent.split('\n')[0], 'line 501');
assert.equal(panel.dropped.hidden, false);
await stopSetupTask('claude');
assert.equal(sent.find((request) => request.path === '/setup/stop').body.id, 's-1');
await pollSetupTask('claude', 's-1');
await settle();
assert.equal(panel.result.textContent, 'Stopped. Run Install again to finish or repair it.');
assert.equal(panel.result.dataset.tone, 'stopped');
assert.equal(panel.stop.hidden, true);
assert.equal(panel.after.hidden, false);  // Try again and Copy command.
""")

    def test_the_sign_in_boxes_link_safely_and_send_the_code(self):
        self.run_app(r"""
settingUp = true;
const claudeTask = (changes) => ({id: 's-2', program: 'claude', action: 'sign-in', option: 'subscription', title: 'Signing in to Claude Code',
  state: 'waiting', stopping: false, elapsedSeconds: 3, command: 'claude auth login', lines: [], first: 0, next: 0, dropped: 0,
  partial: 'Paste code here if prompted > ', code: null, input: {sent: false, message: ''}, result: null, nextTask: '', keptLines: 2000,
  link: {url: 'https://claude.ai/oauth/authorize?state=S', trusted: true, host: 'claude.ai'}, ...changes});
responder = (path, body) => {
  if (path === '/setup/input') return body.text === 'abc#def' ? {status: 200, body: {task: claudeTask({input: {sent: true, message: ''}})}}
    : {status: 400, body: {error: 'Paste the whole code from the sign-in page; it contains #.'}};
  return {status: 200, body: status()};
};
await loadSetup(true);
mergeSetupTask('claude', claudeTask());
renderSetupTasks();
const box = setupStore().panels.claude.box;
const anchor = box.box.querySelector('a');
assert.equal(anchor.href, 'https://claude.ai/oauth/authorize?state=S');
assert.equal(anchor.target, '_blank');
assert.equal(anchor.rel, 'noopener noreferrer');
assert.equal(anchor.textContent, 'Open the Claude sign-in page');
box.parts.input.value = 'no-hash';
await box.parts.send.onclick();
assert.equal(box.parts.message.textContent, 'Paste the whole code from the sign-in page; it contains #.');
assert.equal(box.parts.input.value, 'no-hash');
box.parts.input.value = ' abc#def ';
await box.parts.send.onclick();
assert.deepEqual(sent.filter((request) => request.path === '/setup/input').map((request) => request.body), [{id: 's-2', text: 'no-hash'}, {id: 's-2', text: 'abc#def'}]);
assert.equal(box.parts.input.value, '');
assert.equal(box.parts.message.textContent, 'Code sent. Waiting for Claude Code…');
// A link to an unexpected host is shown as text with a warning, never as a link.
const odd = setupSignInBox('claude', claudeTask({link: {url: 'https://claude.ai.evil.invalid/x', trusted: false, host: 'claude.ai.evil.invalid'}}));
assert.equal(odd.box.querySelectorAll('a').length, 0);
assert(texts(odd.box).includes('This link goes to an unexpected address (claude.ai.evil.invalid). Check it before you open it:'));
assert(texts(odd.box).includes('https://claude.ai.evil.invalid/x'));
// The Codex one-time code.
const code = setupSignInBox('codex', {id: 's-3', link: null, code: {url: 'https://auth.openai.com/codex/device', trusted: true, host: 'auth.openai.com', userCode: 'ABCD-1234'}});
assert.equal(code.box.querySelector('.setup-code').textContent, 'ABCD-1234');
assert.equal(code.box.querySelector('a').rel, 'noopener noreferrer');
assert(texts(code.box).includes('The code expires in 15 minutes. Only enter it if you started this sign-in here; never enter a code someone gave you.'));
""")

    def test_setup_keeps_a_new_problem_draft_and_its_batch(self):
        self.run_app(r"""
responder = (path) => ({status: 200, body: path === '/batches' ? {batches: []} : status()});
let clears = 0;
const realClear = clearJobView;
clearJobView = (...args) => { clears += 1; return realClear(...args); };
// A draft in batch X, then Setup from the sidebar.
composingProblem = true; currentBatch = 'batch_00000000a1';
await openSetup();
assert.equal(settingUp, true);
assert.equal(composingProblem, true);
assert.equal(currentBatch, 'batch_00000000a1');  // S1: the draft's batch stays.
assert.equal(history.urls.at(-1), '/?setup=1');
assert.equal(ui.setupPanel.hidden, false);
assert.equal(ui.input.hidden, true);
assert.equal(ui.batchNavigation.hidden, true);
assert.equal(ui.globalStatus.textContent, 'Setup');
assert.equal(clears, 0);  // Nothing of the draft is cleared.
// Back: the view switches to the draft, which is still there.
location.search = '?new=1&batch=batch_00000000a1';
window.onpopstate();
assert.equal(settingUp, false);
assert.equal(composingProblem, true);
assert.equal(currentBatch, 'batch_00000000a1');
assert.equal(ui.input.hidden, false);
assert.equal(ui.setupPanel.hidden, true);
assert.equal(clears, 0);
// Forward to Setup again.
location.search = '?setup=1';
window.onpopstate();
assert.equal(settingUp, true);
assert.equal(currentBatch, 'batch_00000000a1');
// Without a draft Setup belongs to no batch, and a job page is left for it.
composingProblem = false; currentJob = 'job-1'; currentBatch = 'v4'; settingUp = false;
await openSetup();
assert.equal(currentJob, '');
assert.equal(currentBatch, '');
assert.equal(clears, 1);
// Every other page leaves Setup.
for (const leave of [() => selectBatch(''), () => newProblem(), () => selectJob('job-2')]) {
  settingUp = true;
  await leave();
  assert.equal(settingUp, false);
}
""", slices=(PAGES, OUTCOME, POPSTATE, SETUP),
            setup="const refresh = async () => {};")

    def test_the_shell_routes_setup_and_returns_to_the_draft(self):
        self.run_app(r"""
responder = (path) => ({status: 200, body: path === '/batches' ? {batches: []} : status()});
assert.equal(route(), 'home');
settingUp = true;
assert.equal(route(), 'setup');
currentJob = 'job-1';
assert.equal(route(), 'job');
currentJob = '';
// The sidebar's New problem from Setup with a draft: the view switches back, the draft and its batch stay.
composingProblem = true; currentBatch = 'batch_00000000a1';
renderBatchNavigation();
assert.equal(ui.input.hidden, true);
assert.equal(ui.setupPanel.hidden, false);
await openComposer(false);
assert.equal(settingUp, false);
assert.equal(composingProblem, true);
assert.equal(history.urls.at(-1), '/?new=1&batch=batch_00000000a1');
assert.equal(ui.input.hidden, false);
assert.equal(ui.setupPanel.hidden, true);
// The sidebar's Setup item opens the page.
await go({dataset: {nav: 'setup'}});
assert.equal(settingUp, true);
assert.equal(route(), 'setup');
assert.equal(history.urls.at(-1), '/?setup=1');
""", slices=(PAGES, OUTCOME, ROUTE, COMPOSER, SETUP),
            setup="const refresh = async () => {}; const sync = () => {}, applyView = () => {};"
                  "const openPromptEditor = async () => {};")

    def test_jobs_whose_program_is_missing_or_signed_out_link_to_setup(self):
        self.run_app(r"""
responder = (path) => ({status: 200, body: path === '/batches' ? {batches: []} : status()});
const login = {kind: 'login', title: 'Codex login needed', text: 'Sign in again on the Setup page, or run codex login in a terminal; the job continues by itself.', retrying: true};
const usage = {kind: 'usage', title: 'Usage limit reached', text: 'Wait.', retrying: true};
renderAttention([{id: '__root__', attention: [{...login, runId: 'a', job: 'P1.md', batchId: '__root__'}, {...usage, runId: 'b', job: 'P2.md'}]}]);
const rows = ui.attentionList.children;
assert.deepEqual(rows[0].children.map((child) => child.textContent), ['Codex login needed', 'P1.md', login.text, 'Open Setup']);
assert.equal(rows[0].children[3].href, '/?setup=1');
assert.equal(rows[0].children[3].dataset.setupLink, '');
assert.equal(rows[1].children.length, 3);  // A usage limit is not Setup's.
renderJobs([{runId: 'j1', title: 'P1', phase: 'paused', needsAttention: {...login, kind: 'cli', title: 'Claude Code not installed or not working'}}]);
const line = ui.jobsList.children[0].querySelector('.job-attention');
assert.equal(line.textContent, 'Claude Code not installed or not working: ' + login.text + ' Open Setup');
// The job page.
state = {needsAttention: login, readOnly: false};
jobPageAttention();
assert.equal(ui.runAttention.textContent, 'Codex login needed: ' + login.text + ' Open Setup');
assert.equal(ui.runAttention.children.at(-1).dataset.setupLink, '');
state = {needsAttention: usage, readOnly: false};
jobPageAttention();
assert.equal(ui.runAttention.textContent, 'Usage limit reached: Wait.');
// A plain click on a Setup link opens the page in place; a click with a modifier follows the link.
const link = setupLink();
link.click();
assert.equal(settingUp, true);
// The empty Overview leads to Setup first.
renderBatches([]);
assert.equal(ui.batchesStatus.textContent, 'No batches yet. Open Setup to check the programs your models need, then create a problem.');
// So does one that lists only the default batch, with no problem and nothing shared (as a new installation's does).
// The default batch's row says it has no problems yet; the line does not say it again.
renderBatches([{id: '__root__', label: 'Default batch', default: true, current: true, jobCount: 0, counts: {},
  sharedWorks: {count: 0, names: []}}]);
assert.equal(ui.batchesStatus.textContent, 'Open Setup to check the programs your models need, then create a problem.');
assert.equal(ui.batchesStatus.children[0].dataset.setupLink, '');
assert.equal(ui.batchesStatus.hidden, false);
""", slices=(PAGES, OUTCOME, [JOB_PAGE[0], JOB_PAGE[1], "jobPageAttention"], SETUP),
            setup="const refresh = async () => {}; const localDateTime = (v) => v;")


if __name__ == "__main__":
    unittest.main()
