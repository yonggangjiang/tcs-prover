"""The model provider controls: grouped role menus, provider chips, efforts, the API models
manager and the web cutoff restriction. Run offline: no model call and no network beyond a
loopback mock in the Test endpoint check.

The browser checks cut ui/app.js into slices by marker text and run them in Node's vm with a
small fake DOM, as the other UI tests do.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import workflow_runner as runtime
from runner import providers
from ui import server

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"
INDEX = ROOT / "ui" / "index.html"
ROLES = ("review", "author", "critic", "writer")

# Loads the named app.js slices into one vm context after `setup`, then runs `checks` as an
# async function body. Fails when a marker is missing or the checks never finish.
RUNNER = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const {dom, slices, setup, checks} = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = slices.map(([from, to]) => {
  const start = source.indexOf(from), end = source.indexOf(to, start + 1);
  if (start < 0 || end < 0) throw new Error('Marker not found: ' + (start < 0 ? from : to));
  return source.slice(start, end);
}).join('\n');
let finished = false;
const context = {assert, setImmediate, console, finish: () => { finished = true; }};
process.on('exit', () => {
  if (!finished && !process.exitCode) { console.error('The checks did not finish.'); process.exitCode = 1; }
});
Promise.resolve(vm.runInNewContext(`${dom}\n${setup}\n${code}\n(async () => {\n${checks}\nfinish();\n})()`, context))
  .catch((error) => { console.error(error); process.exitCode = 1; });
"""

# A small DOM, evaluated inside the test's context: enough for selects with option groups,
# buttons, fields and text. Selectors: tag, .class and [data-x] / [data-x="v"] / [attr].
DOM = r"""
const document = {activeElement: null};
const matches = (node, selector) => {
  const match = /^([a-z]*)(?:\.([\w-]+))?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/i.exec(selector.trim());
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
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parentElement = null;
    this.dataset = {}; this.attributes = {}; this.className = ''; this.hidden = false;
    this.disabled = false; this.checked = false; this.required = false; this.placeholder = '';
    this.label = ''; this.text = ''; this.ownValue = '';
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
  get textContent() {
    return this.text + this.children.map((child) => child instanceof Element ? child.textContent : String(child)).join('');
  }
  set textContent(value) { this.replaceChildren(); this.text = String(value); }
  get value() { return this.ownValue; }
  set value(value) { this.ownValue = String(value); }
  append(...items) {
    for (const item of items) {
      if (item instanceof Element) { item.remove(); item.parentElement = this; }
      this.children.push(item);
    }
  }
  replaceChildren(...items) {
    for (const child of this.children) if (child instanceof Element) child.parentElement = null;
    this.children = []; this.text = '';
    this.append(...items);
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
  descendants() {
    return this.children.flatMap((child) => child instanceof Element ? [child, ...child.descendants()] : []);
  }
  querySelectorAll(selector) { return this.descendants().filter((node) => matches(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (matches(node, selector)) return node;
    return null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  focus() { document.activeElement = this; }
  addEventListener(type, listener) { (this.listeners ||= {})[type] = listener; }
}
class Option extends Element {
  constructor(text = '', value) { super('option'); this.text = String(text); this.ownValue = value === undefined ? String(text) : String(value); }
}
// Like a real single <select>: an unknown value selects nothing; losing the chosen option selects the first.
class Select extends Element {
  constructor(rows = []) {
    super('select');
    for (const row of rows) this.append(row);
    this.chosen = this.options.find((option) => option.selected) || this.options[0] || null;
  }
  get options() { return this.descendants().filter((node) => node.tagName === 'OPTION'); }
  get selectedOptions() { return this.chosen && this.options.includes(this.chosen) ? [this.chosen] : []; }
  get value() {
    if (this.chosen && !this.options.includes(this.chosen)) this.chosen = this.options[0] || null;
    return this.chosen ? this.chosen.value : '';
  }
  set value(value) { this.chosen = this.options.find((option) => option.value === String(value)) || null; }
  add(option) { this.append(option); }
}
document.createElement = (tag) => tag === 'select' ? new Select() : new Element(tag);
// The role menus as index.html writes them (fixture.menus: [[groupLabel, provider, [[value, text]]]]).
const roleMenu = (groups) => new Select(groups.map(([label, provider, rows]) => {
  const group = new Element('optgroup');
  group.label = label; group.dataset.provider = provider;
  group.append(...rows.map(([value, text]) => new Option(text, value)));
  return group;
}));
"""


class Page(HTMLParser):
    """Every element's attributes by id, and each select's option groups and options."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.elements, self.menus, self.texts = {}, {}, {}
        self._select = self._group = self._option = None
        self._open = []  # [tag, id or None] for every element whose end tag is still to come

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.elements[attrs["id"]] = {"tag": tag, **attrs}
            self.texts[attrs["id"]] = ""
        if tag not in self.VOID:
            self._open.append([tag, attrs.get("id")])
        if tag == "select":
            self._select = attrs.get("id")
            self.menus[self._select] = []
        elif tag == "optgroup" and self._select:
            self._group = [attrs.get("label"), attrs.get("data-provider"), []]
            self.menus[self._select].append(self._group)
        elif tag == "option" and self._select:
            self._option = [attrs.get("value"), "selected" in attrs, ""]
            target = self._group[2] if self._group else self.menus[self._select]
            target.append(self._option)

    def handle_endtag(self, tag):
        while self._open:
            if self._open.pop()[0] == tag:
                break
        if tag == "option":
            self._option = None
        elif tag == "optgroup":
            self._group = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._option is not None:
            self._option[2] += data
        for _, identity in self._open:
            if identity:
                self.texts[identity] += data


def page():
    parsed = Page()
    parsed.feed(INDEX.read_text(encoding="utf-8"))
    return parsed


def menu_fixture():
    """The static role menu of index.html, for the fake selects."""
    groups = page().menus["authorModel"]
    return [[label, provider, [[value, text.strip()] for value, _, text in options]]
            for label, provider, options in groups]


def catalog(*api_rows):
    """A /state-like catalog: the built-in rows plus the given API rows."""
    rows = [{"id": row["id"], "label": row["label"], "provider": row["provider"]} for row in providers.BUILTIN_MODELS]
    return rows + [{"provider": "api", "hasKey": True, **row} for row in api_rows]


class ProviderMarkupTests(unittest.TestCase):
    def test_role_menus_group_the_built_in_models_by_cli_in_registry_order(self):
        parsed = page()
        expected = []
        for provider, label in (("codex", "Codex CLI"), ("claude", "Claude CLI")):
            rows = [row for row in providers.BUILTIN_MODELS if row["provider"] == provider]
            expected.append([label, provider, [
                [row["id"], row["id"] == runtime.MODEL,
                 row["label"] + (" — default" if row["id"] == runtime.MODEL else "")] for row in rows]])
        for role in ROLES:
            with self.subTest(role=role):
                groups = [[label, provider, [[value, selected, text.strip()] for value, selected, text in options]]
                          for label, provider, options in parsed.menus[f"{role}Model"]]
                self.assertEqual(groups, expected)
                chip = parsed.elements[f"{role}Model"].get("aria-describedby")
                self.assertEqual(chip, f"{role}ModelProvider")
                self.assertEqual(parsed.elements[chip]["class"], "provider-chip")
                self.assertEqual(parsed.texts[chip].strip(), "Codex CLI")

    def test_deepseek_and_the_static_preview_are_gone(self):
        for name in ("index.html", "app.js", "styles.css"):
            with self.subTest(file=name):
                text = (ROOT / "ui" / name).read_text(encoding="utf-8")
                self.assertNotIn("deepseek", text.lower())
                self.assertNotIn("providerPreview", text)
                self.assertNotIn("preview-chip", text)
        parsed = page()
        speed = " ".join(parsed.texts["speedHelp"].split())
        self.assertIn("Fast applies to Codex CLI models only", speed)
        self.assertIn("Claude CLI and API models always run at standard speed", speed)

    def test_api_model_form_keeps_the_key_out_of_sight(self):
        parsed = page()
        key = parsed.elements["apiModelKey"]
        # new-password: no browser fills a saved login password into the key field.
        self.assertEqual((key["tag"], key["type"], key["autocomplete"]), ("input", "password", "new-password"))
        self.assertNotIn("value", key)
        for identity in ("apiModelTestResult", "apiModelsStatus"):
            # Live regions are rendered (empty) before their first message, never hidden.
            self.assertEqual(parsed.elements[identity].get("role"), "status")
            self.assertNotIn("hidden", parsed.elements[identity])
            self.assertEqual(parsed.texts[identity], "")
        self.assertEqual(parsed.elements["apiModelFormatOpenai"]["value"], "openai")
        self.assertEqual(parsed.elements["apiModelFormatAnthropic"]["value"], "anthropic")
        self.assertIn("checked", parsed.elements["apiModelFormatOpenai"])
        self.assertNotIn("apiModelHeaderName", parsed.elements)
        self.assertIn("Authentication is automatic", parsed.texts["apiModelAuthHelp"])
        self.assertIn("not sent to the router", parsed.texts["apiModelNameHelp"])
        help_text = " ".join(parsed.texts["apiModelFormatHelp"].split())
        self.assertIn("Codex CLI", help_text)
        for identity in ("apiModelBaseUrl", "apiModelRequestSource", "apiModelId", "apiModelName", "apiModelKey"):
            self.assertIn(identity, [attrs.get("for") for attrs in _labels(INDEX)])
        self.assertEqual(parsed.elements["apiModelId"]["tag"], "select")
        self.assertIn("disabled", parsed.elements["apiModelId"])
        self.assertNotIn("Model ID", parsed.texts["apiModelForm"])
        self.assertEqual(parsed.elements["apiModelLoadButton"]["type"], "button")
        self.assertIn('id="runSettingsWarning"', INDEX.read_text(encoding="utf-8"))

    def test_slice_markers_used_by_the_ui_tests_occur_once(self):
        # Every UI test cuts app.js by marker text; a second copy of a marker silently moves a slice.
        source = APP.read_text(encoding="utf-8")
        markers = set()
        for test in (ROOT / "tests").glob("test_*.py"):
            text = test.read_text(encoding="utf-8")
            if "app.js" not in text or test.name == Path(__file__).name:
                continue
            markers.update(match.group(2) for match in re.finditer(r"""indexOf\((['"])(.+?)\1""", text))
            for match in re.finditer(r"""slice\((['"])(.+?)\1\s*,\s*(['"])(.+?)\3""", text):
                markers.update((match.group(2), match.group(4)))
        markers = {marker for marker in markers if "\\" not in marker}
        self.assertGreater(len(markers), 40)
        for marker in sorted(markers):
            with self.subTest(marker=marker):
                self.assertEqual(source.count(marker), 1)


def _labels(path):
    labels = []

    class Labels(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "label":
                labels.append(dict(attrs))

    Labels().feed(path.read_text(encoding="utf-8"))
    return labels


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class ProviderBrowserTests(unittest.TestCase):
    def run_app(self, slices, setup, checks):
        payload = json.dumps({"dom": DOM, "slices": slices, "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(APP)], input=payload,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    MENUS = [["function setChoice(", "async function refresh()"]]

    def menus_setup(self, rows):
        return (f"const fixture = {json.dumps({'menu': menu_fixture(), 'catalog': catalog(*rows)})};\n"
                + r"""
let state = {workflow: {settings: {model_catalog: fixture.catalog, model_efforts: {}}}};
const ui = {};
for (const role of ['review', 'author', 'critic', 'writer']) ui[role + 'Model'] = roleMenu(fixture.menu);
const apiValues = (select) => (select.querySelector('optgroup[data-provider="api"]')?.children || []).map((o) => o.value);
""")

    def test_api_group_follows_the_catalog_and_keeps_valid_choices(self):
        rows = [{"id": "api:lab-sol", "label": "Sol via lab gateway", "format": "openai", "host": "gateway.lab.example"},
                {"id": "api:opus-direct", "label": "Opus 5.5 · direct", "format": "anthropic", "host": "api.anthropic.com"}]
        self.run_app(self.MENUS, self.menus_setup(rows), r"""
// No API model yet: the built-in menus are left alone (no DOM work at all).
const saved = state.workflow.settings.model_catalog;
state.workflow.settings.model_catalog = saved.filter((row) => row.provider !== 'api');
assert.deepEqual(syncModelMenus(), []);
assert.equal(ui.authorModel.children.length, 2);
assert.equal(ui.authorModel.dataset.apiModels, undefined);

// Two API models: one "API" group after the built-in groups, in every role menu.
state.workflow.settings.model_catalog = saved;
ui.criticModel.value = 'claude-opus-5-5';
assert.deepEqual(syncModelMenus(), []);
for (const role of ['review', 'author', 'critic', 'writer']) {
  const select = ui[role + 'Model'];
  assert.deepEqual(select.children.map((group) => group.label), ['Codex CLI', 'Claude CLI', 'API']);
  assert.deepEqual(apiValues(select), ['api:lab-sol', 'api:opus-direct']);
}
const option = ui.authorModel.querySelector('optgroup[data-provider="api"]').children[1];
assert.equal(option.textContent, 'Opus 5.5 · direct');
// The footer's short name drops the "·" that separates the footer's own parts.
assert.equal(option.dataset.short, 'Opus 5.5 direct');
// A name over 24 characters is cut where a word ends (or after a "-", "." or "/" inside one),
// never mid-word unless that would leave too little of it.
for (const [label, short] of [
  ['Sol via lab gateway preview', 'Sol via lab gateway…'],
  ['Sol extended-context preview', 'Sol extended-context…'],
  ['Sol via lab gateway and more', 'Sol via lab gateway and…'],
  ['gpt-6-astra · llm-proxy.example.org', 'gpt-6-astra llm-proxy…'],
  ['supercalifragilisticexpialidocious', 'supercalifragilisticexp…'],
  ['Exactly twenty-four char', 'Exactly twenty-four char'],
]) assert.equal(shortModelName(label), short, label);
assert.equal(ui.criticModel.value, 'claude-opus-5-5');
assert.equal(ui.reviewModel.value, 'gpt-6.1-sol');

// A role on an API model keeps it through a rename; an unchanged catalog changes nothing.
ui.writerModel.value = 'api:lab-sol';
state.workflow.settings.model_catalog = saved.map((row) => row.id === 'api:lab-sol' ? {...row, label: 'Lab Sol'} : row);
assert.deepEqual(syncModelMenus(), []);
assert.equal(ui.writerModel.value, 'api:lab-sol');
assert.equal(ui.writerModel.selectedOptions[0].textContent, 'Lab Sol');
const group = ui.writerModel.querySelector('optgroup[data-provider="api"]');
syncModelMenus();
assert.equal(ui.writerModel.querySelector('optgroup[data-provider="api"]'), group);

// Removing the writer's model: the writer falls back to the default, and the caller is told.
ui.authorModel.value = 'api:opus-direct';
state.workflow.settings.model_catalog = saved.filter((row) => row.id !== 'api:lab-sol');
assert.deepEqual(syncModelMenus(), ['writer']);
assert.equal(ui.writerModel.value, 'gpt-6.1-sol');
assert.equal(ui.authorModel.value, 'api:opus-direct');
assert.deepEqual(apiValues(ui.writerModel), ['api:opus-direct']);

// The fallback is the default model itself, not whichever option happens to come first.
ui.reviewModel = roleMenu([...fixture.menu].reverse());
state.workflow.settings.model_catalog = saved;
syncModelMenus();
ui.reviewModel.value = 'api:opus-direct';
state.workflow.settings.model_catalog = saved.filter((row) => row.id !== 'api:opus-direct');
assert.deepEqual(syncModelMenus(), ['review', 'author']);
assert.equal(ui.reviewModel.value, 'gpt-6.1-sol');
assert.equal(ui.authorModel.value, 'gpt-6.1-sol');

// The last one removed: the API group goes away.
ui.authorModel.value = 'api:lab-sol';
state.workflow.settings.model_catalog = saved.filter((row) => row.provider !== 'api');
assert.deepEqual(syncModelMenus(), ['author']);
assert.equal(ui.authorModel.querySelector('optgroup[data-provider="api"]'), null);

// A saved job naming a removed API model keeps it visible, named for what it is; once the
// model is configured again the stand-in gives way to the real option.
setChoice(ui.criticModel, 'api:lab-sol');
assert.equal(ui.criticModel.value, 'api:lab-sol');
assert.equal(ui.criticModel.options.at(-1).textContent, 'api:lab-sol (not configured)');
setChoice(ui.reviewModel, 'gpt-5.6-terra');
assert.equal(ui.reviewModel.options.at(-1).textContent, 'gpt-5.6-terra (older)');
state.workflow.settings.model_catalog = saved;
syncModelMenus();
assert.equal(ui.criticModel.options.filter((o) => o.value === 'api:lab-sol').length, 1);
assert.equal(ui.criticModel.value, 'api:lab-sol');
assert.equal(ui.criticModel.selectedOptions[0].parentElement.label, 'API');

// An older server publishes no catalog: the static menus stay as they are.
state = {workflow: {settings: {}}};
assert.deepEqual(syncModelMenus(), []);
""")

    def test_a_viewed_jobs_stand_in_models_never_reach_the_next_choice(self):
        rows = [{"id": "api:lab-sol", "label": "Sol via lab gateway", "format": "openai", "host": "gateway.lab.example"}]
        self.run_app(self.MENUS, self.menus_setup(rows), r"""
syncModelMenus();
// A reviewed job whose author's API model was removed, and whose critic is a retired model.
setChoice(ui.authorModel, 'api:lab-claude');
setChoice(ui.criticModel, 'gpt-5.6-sol');
assert.equal(ui.authorModel.selectedOptions[0].textContent, 'api:lab-claude (not configured)');
assert.equal(ui.criticModel.selectedOptions[0].textContent, 'gpt-5.6-sol (older)');
// Choosing again for the same job keeps exactly one stand-in.
setChoice(ui.authorModel, 'api:lab-claude');
assert.equal(ui.authorModel.options.filter((option) => option.value === 'api:lab-claude').length, 1);
// New problem resets every role to the default: no stand-in remains to be chosen.
for (const role of ['author', 'critic']) {
  const select = ui[role + 'Model'];
  setChoice(select, 'gpt-6.1-sol');
  assert.equal(select.value, 'gpt-6.1-sol');
  assert.deepEqual(select.options.filter((option) => option.dataset.standIn).map((option) => option.value), []);
}
assert.ok(!ui.authorModel.options.some((option) => option.value === 'api:lab-claude'));
assert.ok(!ui.criticModel.options.some((option) => option.value === 'gpt-5.6-sol'));
// Another job's stand-in replaces this one's rather than adding to it.
setChoice(ui.authorModel, 'api:gone-1');
setChoice(ui.authorModel, 'api:gone-2');
assert.deepEqual(ui.authorModel.options.filter((option) => option.dataset.standIn).map((option) => option.value),
                 ['api:gone-2']);
// Real options are never removed.
assert.deepEqual(apiValues(ui.authorModel), ['api:lab-sol']);
""")

    def test_efforts_offer_exactly_the_models_levels_and_round_down(self):
        self.run_app(self.MENUS, r"""
const levels = ['ultra', 'max', 'xhigh', 'high', 'medium', 'low'];
const efforts = () => new Select(levels.map((level) => new Option(level, level)));
let state = {workflow: {settings: {model_efforts: {
  'gpt-6.1-sol': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  'claude-opus-5-5': ['low', 'medium', 'high', 'xhigh', 'max'],
  'api:small': ['low', 'medium', 'high'],
  'api:upper': ['medium', 'high', 'max'],
}}}};
const ui = {authorModel: {value: 'gpt-6.1-sol'}, authorEffort: efforts()};
const offered = () => ui.authorEffort.options.filter((o) => !o.hidden && !o.disabled).map((o) => o.value);
""", r"""
ui.authorEffort.value = 'ultra';
syncEffortChoices('author');
assert.deepEqual(offered(), levels);
// Claude CLI models list no Ultra: it is hidden, and a chosen Ultra runs as Max.
ui.authorModel.value = 'claude-opus-5-5';
syncEffortChoices('author');
assert.deepEqual(offered(), ['max', 'xhigh', 'high', 'medium', 'low']);
assert.equal(ui.authorEffort.value, 'max');
// The nearest lower level the model offers: Max becomes High.
ui.authorModel.value = 'api:small';
syncEffortChoices('author');
assert.deepEqual(offered(), ['high', 'medium', 'low']);
assert.equal(ui.authorEffort.value, 'high');
// Nothing lower is offered: the lowest level the model has.
ui.authorEffort.value = 'low';
ui.authorModel.value = 'api:upper';
syncEffortChoices('author');
assert.equal(ui.authorEffort.value, 'medium');
// Back to a model with every level: all are offered again and the choice stays.
ui.authorModel.value = 'gpt-6.1-sol';
syncEffortChoices('author');
assert.deepEqual(offered(), levels);
assert.equal(ui.authorEffort.value, 'medium');
""")

    PROVIDERS = [["function selectedRetrievalMode()", "// Keep the compact footer"]]

    def providers_setup(self, rows):
        # The menus as syncModelMenus leaves them: the built-in groups, then the API group.
        menu = menu_fixture() + ([["API", "api", [[row["id"], row["label"]] for row in rows]]] if rows else [])
        return (f"const fixture = {json.dumps({'menu': menu, 'catalog': catalog(*rows)})};\n"
                + r"""
let state = {workflow: {settings: {model_catalog: fixture.catalog}}, retrievalCapabilities: {historicalAvailable: true}};
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
for (const role of ['review', 'author', 'critic', 'writer']) ui[role + 'Model'] = roleMenu(fixture.menu);
ui.retrievalMode.value = 'live';
ui.retrievalSetting = new Element('section');
new Element('details').append(ui.retrievalSetting);
ui.skipStatementReview.checked = true;
ui.researchAuditModels = [new Element()];
const show = (element, visible) => { element.hidden = !visible; };
const selectedProblemMode = () => 'statement';
const researchAuditValues = () => ({models: ['none', 'none', 'none']});
const chip = (role) => ui[role + 'ModelProvider'];
const disabledGroups = (role) => ui[role + 'Model'].children.filter((group) => group.disabled).map((group) => group.label);
""")

    def test_provider_chips_name_the_cli_and_the_api_endpoint(self):
        rows = [{"id": "api:lab-sol", "label": "Sol via lab gateway", "format": "openai", "host": "gateway.lab.example"},
                {"id": "api:keyless", "label": "Keyless", "format": "anthropic", "host": "llm.example.org",
                 "hasKey": False}]
        self.run_app(self.PROVIDERS, self.providers_setup(rows), r"""
for (const [model, text, provider] of [
  ['gpt-6.1-sol', 'Codex CLI', 'codex'], ['claude-haiku-5-5', 'Claude CLI', 'claude'],
  ['gpt-5.6-terra', 'Codex CLI · older model', 'codex'],
  ['api:lab-sol', 'API · OpenAI format · gateway.lab.example', 'api'],
  ['api:gone', 'API · not configured', 'api'],
  ['api:keyless', 'API · Anthropic format · llm.example.org · no key', 'api'],
]) {
  const select = ui.authorModel;
  setChoiceForTest(select, model);
  renderModelChoices();
  assert.equal(chip('author').textContent, text, model);
  assert.equal(chip('author').dataset.provider, provider, model);
  assert.equal(chip('author').dataset.state, ['api:gone', 'api:keyless'].includes(model) ? 'warning' : '', model);
}
// The catalog decides; without one (an older server) the built-in ids still read correctly.
state.workflow.settings = {};
assert.deepEqual(['gpt-6-luna', 'claude-opus-5-5', 'api:x', 'other'].map(modelProvider), ['codex', 'claude', 'api', '']);
function setChoiceForTest(select, value) {
  if (!select.options.some((option) => option.value === value)) select.add(new Option(value, value));
  select.value = value;
}
""")

    def test_web_cutoff_requires_codex_cli_models_in_words_and_in_the_menus(self):
        rows = [{"id": "api:lab-sol", "label": "Sol via lab gateway", "format": "openai", "host": "gateway.lab.example"}]
        rule = runtime.HISTORICAL_CODEX_ONLY
        self.run_app(self.PROVIDERS, self.providers_setup(rows), f"const rule = {json.dumps(rule)};\n" + r"""
ui.authorModel.value = 'claude-opus-5-5';
ui.writerModel.value = 'api:lab-sol';
ui.reviewModel.value = 'claude-haiku-5-5';
renderRetrievalSettings();
// Unrestricted search: every group is offered and nothing is flagged.
for (const role of ['review', 'author', 'critic', 'writer']) assert.deepEqual(disabledGroups(role), []);
assert.equal(chip('author').dataset.state, '');
assert.equal(ui.retrievalConfigurationNotice.hidden, true);

// A web cutoff: the proof roles offer only Codex CLI models; the chosen ones are flagged, not changed.
ui.retrievalMode.value = 'historical';
ui.retrievalCutoffUtc.value = '2024-03-31T00:00:00Z';
renderRetrievalSettings();
for (const role of ['author', 'critic', 'writer']) assert.deepEqual(disabledGroups(role), ['Claude CLI', 'API']);
assert.deepEqual(disabledGroups('review'), []);
assert.equal(chip('author').textContent, 'Claude CLI · not with a web cutoff');
assert.equal(chip('author').dataset.state, 'warning');
assert.equal(chip('writer').textContent, 'API · OpenAI format · gateway.lab.example · not with a web cutoff');
assert.equal(chip('critic').textContent, 'Codex CLI');
assert.equal(chip('review').textContent, 'Claude CLI');
assert.equal(ui.authorModel.value, 'claude-opus-5-5');
const notice = ui.retrievalConfigurationNotice.textContent;
assert.equal(notice, `${rule} Choose a Codex CLI model for the proof author and LaTeX writer. Set optional research audits to None.`);
assert.equal(ui.retrievalConfigurationNotice.hidden, false);

// Starting is refused with the server's own words, and the first such menu takes focus.
assert.equal(validateRetrievalSettings(), false);
assert.equal(ui.notice.textContent, `${rule} Choose a Codex CLI model for the proof author and LaTeX writer.`);
assert.equal(document.activeElement, ui.authorModel);
assert.equal(ui.retrievalSetting.parentElement.open, true);

// With Codex CLI models only, the rule stays stated and the job may start.
ui.authorModel.value = 'gpt-6-astra';
ui.writerModel.value = 'gpt-6.1-sol';
renderRetrievalSettings();
assert.equal(ui.retrievalConfigurationNotice.textContent, `${rule} Set optional research audits to None.`);
assert.equal(chip('author').dataset.state, '');
assert.equal(validateRetrievalSettings(), true);

// Without Skip statement review the notice asks for it and still states the rule.
ui.skipStatementReview.checked = false;
renderRetrievalSettings();
assert.equal(ui.retrievalConfigurationNotice.textContent,
  `Select Skip statement review under Workflow to start a historical proof job. ${rule}`);
""")

    FOOTER = [["function updateModelSummary()", "const promptLabels ="]]

    def test_footer_names_claude_and_api_models_and_where_fast_applies(self):
        self.run_app(self.FOOTER, r"""
const field = (value) => ({value});
const ui = {
  reviewModel: field('gpt-6.1-sol'), authorModel: field('claude-fable-5-1'), criticModel: field('api:lab-sol'),
  writerModel: field('claude-sonnet-5-5'), reviewEffort: field('max'), authorEffort: field('xhigh'),
  criticEffort: field('high'), writerEffort: field('low'), speedMode: field('standard'),
  reasoningSummary: field('detailed'), skipStatementReview: {checked: false}, statementReviewOnly: {checked: false},
  modelSummary: {textContent: ''},
};
ui.criticModel.selectedOptions = [{dataset: {short: 'Sol via lab gateway'}}];
let mode = 'statement';
const selectedProblemMode = () => mode;
// The words of the summary; each part is held together by no-break spaces and word joiners.
const text = () => ui.modelSummary.textContent.replace(/\u00a0/g, ' ').replace(/\u2060/g, '');
""", r"""
updateModelSummary();
assert.equal(text(), 'Standard speed · Detailed activity log · 6.1 Sol/Max review · '
  + 'Fable 5.1/Extra high author · Sol via lab gateway/High critic · Sonnet 5.5/Low writer');
// A wrapped summary breaks only after a "·": no line starts with one, no model name is split.
assert.doesNotMatch(ui.modelSummary.textContent, /[^·] /);
assert.match(ui.modelSummary.textContent, /Sol\u00a0via\u00a0lab\u00a0gateway\/High\u00a0critic\u00a0· /);
// A hyphen in an API model's name is no break either.
ui.criticModel.selectedOptions = [{dataset: {short: 'gpt-6-astra llm-proxy.example…'}}];
updateModelSummary();
assert.match(text(), / · gpt-6-astra llm-proxy\.example…\/High critic · /);
assert.doesNotMatch(ui.modelSummary.textContent, /-(?!\u2060)/);
ui.criticModel.selectedOptions = [{dataset: {short: 'Sol via lab gateway'}}];
ui.speedMode.value = 'fast';
updateModelSummary();
assert.match(text(), /^Fast for Codex CLI · Standard for others · /);
assert.match(ui.modelSummary.textContent, /^Fast\u00a0for\u00a0Codex\u00a0CLI\u00a0· Standard/);
// Fast reaches every role only when all of them run native Codex CLI models.
ui.skipStatementReview.checked = true;
for (const role of ['author', 'critic', 'writer']) ui[role + 'Model'] = field('gpt-6-luna');
updateModelSummary();
assert.match(text(), /^Fast speed · /);
// LaTeX polish runs the writer only.
mode = 'latex';
ui.writerModel = field('claude-haiku-5-5');
updateModelSummary();
assert.equal(text(), 'Standard speed · Fast is Codex CLI only · Detailed activity log · Haiku 5.5/Low writer');
""")

    OVERVIEW = [["function retrievalConfiguration(", "function renderJobs("],
                ["function describe(entry)", "function cardIcon("],
                ["function completedRunLabel(", "function renderWorkflow()"]]

    def test_job_page_states_the_settings_warning_and_the_api_endpoints(self):
        self.run_app(self.OVERVIEW, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let stateReceivedAt = Date.now();
const show = (element, visible) => { element.hidden = !visible; };
const clockText = () => '12:00:00';
const timelineRows = new Map();
document.createElement = (tag) => new Element(tag);
let state = {runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', statement: 'S', draft: 'S',
  workflow: {nodes: {author: {label: 'Proof author'}}}, trace: [], skipStatementReview: true, criticRounds: 2,
  authorModel: 'claude-opus-5-5', authorEffort: 'max', criticModel: 'api:lab-sol', criticEffort: 'high',
  writerModel: 'gpt-6.1-sol', writerEffort: 'max',
  apiModels: {'api:lab-sol': {label: 'Sol via lab gateway', format: 'openai', baseUrl: 'https://gateway.lab.example/v1',
    headerName: 'Authorization', model: 'gpt-6.1-sol'}},
  settingsWarning: 'Statement reviewer: DeepSeek is no longer supported; this role now uses GPT 6.1 Sol.'};
const rows = () => Object.fromEntries(ui.runMetadata.children.map((row) => [row.children[0].textContent, row.children[1].textContent]));
""", r"""
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, false);
assert.equal(ui.runSettingsWarning.textContent, state.settingsWarning);
assert.equal(rows()['Settings warning'], state.settingsWarning);
assert.equal(rows()['Proof author'], 'claude-opus-5-5 · max');
assert.equal(rows()['Independent critic'], 'api:lab-sol · high · Sol via lab gateway · OpenAI format · '
  + 'https://gateway.lab.example/v1 · model gpt-6.1-sol · max 2 consecutive rounds');
assert.equal(rows()['LaTeX writer'], 'gpt-6.1-sol · max');
// No warning: the header line is hidden and the row is left out.
state = {...state, settingsWarning: ''};
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, true);
assert.equal(rows()['Settings warning'], undefined);

// The API model is removed while the page is open: /state's catalog no longer lists it. The
// run settings mark it, and the header says what Resume would say, in the server's words.
const catalog = [{id: 'gpt-6.1-sol', provider: 'codex'}, {id: 'claude-opus-5-5', provider: 'claude'}];
// A job cannot switch models: the only remedy is adding it again with the settings it recorded.
const missing = 'The API model api:lab-sol chosen for the critic is not configured. Add it again under '
  + 'New problem → Advanced settings → API models with the same settings (Sol via lab gateway · OpenAI format · '
  + 'https://gateway.lab.example/v1 · model gpt-6.1-sol) to continue this job.';
state = {...state, workflow: {...state.workflow, settings: {model_catalog: catalog}}};
renderRunOverview();
assert.equal(rows()['Independent critic'], 'api:lab-sol (not configured) · high · Sol via lab gateway · OpenAI format · '
  + 'https://gateway.lab.example/v1 · model gpt-6.1-sol · max 2 consecutive rounds');
assert.equal(ui.runSettingsWarning.hidden, false);
assert.equal(ui.runSettingsWarning.textContent, missing);
assert.equal(rows()['Settings warning'], missing);
// After the server states it (a reload or a restart), it is not repeated.
state = {...state, settingsWarning: `${missing} Automatic retry is off; choose Resume once the model is configured.`};
renderRunOverview();
assert.equal(ui.runSettingsWarning.textContent, state.settingsWarning);
// A read-only saved batch cannot resume: the run settings still mark the model, the header adds nothing.
state = {...state, settingsWarning: '', readOnly: true};
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, true);
assert.equal(rows()['Independent critic'].split(' · ')[0], 'api:lab-sol (not configured)');
// Configured again: the mark and the warning go away.
state = {...state, readOnly: false, workflow: {...state.workflow, settings: {model_catalog: [...catalog,
  {id: 'api:lab-sol', provider: 'api'}]}}};
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, true);
assert.equal(rows()['Independent critic'].split(' · ')[0], 'api:lab-sol');
// Only roles the job still runs count: a skipped reviewer's model is not checked.
state = {...state, reviewModel: 'api:gone'};
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, true);
state = {...state, skipStatementReview: false};
renderRunOverview();
// Without a recorded snapshot, the display name that gives the same id.
assert.equal(ui.runSettingsWarning.textContent, 'The API model api:gone chosen for the statement reviewer is not '
  + 'configured. Add it again under New problem → Advanced settings → API models with the display name “gone” '
  + 'to continue this job.');
""")

    MANAGER = [["// API models (Advanced settings", "// Appearance: the dark theme"]]
    MANAGER_SETUP = r"""
const elements = {};
const $ = (id) => elements[id] ||= id === 'apiModelId' ? new Select() : new Element(['apiModelList'].includes(id) ? 'ul' : 'div');
const advanced = new Element('details');
const ui = {input: {querySelector: (selector) => selector === 'details.advanced' ? advanced : null}};
for (const [id, value] of [['apiModelFormatOpenai', 'openai'], ['apiModelFormatAnthropic', 'anthropic']]) {
  $(id).value = value;
}
$('apiModelFormatOpenai').checked = true;
const sessionToken = 'session-token';
let state = {workflow: {nodes: {}, settings: {model_catalog: [], model_efforts: {}}}};
const show = (element, visible) => { element.hidden = !visible; };
const calls = [];
const syncModelMenus = () => { calls.push('menus'); return pendingReplaced.splice(0); };
const syncEffortChoices = (role) => calls.push('efforts:' + role);
const renderRetrievalSettings = () => calls.push('retrieval');
const updateModelSummary = () => calls.push('summary');
let pendingReplaced = [];
let answer = true;
const questions = [];
const confirm = (question) => { questions.push(question); return answer; };
document.createElement = (tag) => tag === 'select' ? new Select() : new Element(tag);
const sent = [];
const fetch = (path, options = {}) => new Promise((resolve) => sent.push({path, options,
  body: options.body ? JSON.parse(options.body) : undefined,
  reply: (status, value) => resolve({ok: status < 400, status, json: async () => value})}));
const tick = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 20; i += 1) await tick(); };
const listing = (models) => ({models, formats: [
  {id: 'openai', label: 'OpenAI-compatible (Responses API)', cli: 'Codex CLI', headerName: 'Authorization'},
  {id: 'anthropic', label: 'Anthropic (Messages API)', cli: 'Claude CLI', headerName: 'x-api-key'}],
  model_catalog: models.map((model) => ({id: model.id, label: model.label, provider: 'api', format: model.format,
    host: model.host, hasKey: model.hasKey})),
  model_efforts: Object.fromEntries(models.map((model) => [model.id, ['low', 'medium', 'high', 'xhigh', 'max']]))});
const lab = {id: 'api:lab-sol', label: 'Sol via lab gateway', format: 'openai', baseUrl: 'https://gateway.lab.example/v1',
  host: 'gateway.lab.example', headerName: 'Authorization', model: 'gpt-6.1-sol', hasKey: true, keyHint: '…2223',
  lastTest: null};
const type = (id, value) => { $(id).value = value; $(id).oninput?.(); };
// Existing manager checks begin with model discovery already completed. The discovery checks
// below exercise the HTTP flow and actual select behavior separately.
const discovered = () => setApiModelOptions([{id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol'}]);
const rowButton = (action, id) => $('apiModelList').querySelector(`button[data-api-action="${action}"]`)
  && $('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === action && b.dataset.apiModel === id);
const click = (button) => $('apiModelList').onclick({target: button});
const buttons = () => [$('apiModelLoadButton'), $('apiModelTestButton'), $('apiModelSaveButton'), ...$('apiModelList').querySelectorAll('button')];
"""

    def test_api_model_manager_lists_tests_saves_edits_and_removes(self):
        key, other = "sk-test-0123456789abcdef", "sk-other-fedcba9876543210"
        self.run_app(self.MANAGER, self.MANAGER_SETUP, f"const key = {json.dumps(key)}, other = {json.dumps(other)};\n"
                     + r"""
// The list is read when Advanced settings opens: a plain GET with the session token.
assert.equal($('apiModelFormHeading').textContent, 'Add an API model');
advanced.open = true;
advanced.listeners.toggle({target: advanced});
assert.equal(sent.length, 1);
assert.equal(sent[0].path, '/api-models');
assert.equal(sent[0].options.method, undefined);
assert.equal(sent[0].options.headers['X-TCS-Prover-Token'], 'session-token');
assert(buttons().every((button) => button.disabled));
sent[0].reply(200, listing([]));
await settle();
assert.equal($('apiModelCount').textContent, 'None saved');
assert.equal($('apiModelEmpty').hidden, false);
assert.equal($('apiModelList').hidden, true);
assert(!$('apiModelTestButton').disabled && !$('apiModelSaveButton').disabled);

// Missing fields are pointed at before anything is sent.
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 1);
assert.equal($('apiModelTestResult').textContent, 'Enter the base URL.');
assert.equal(document.activeElement, $('apiModelBaseUrl'));
type('apiModelBaseUrl', 'https://gateway.lab.example/v1');
type('apiModelId', 'gpt-6.1-sol');
$('apiModelTestButton').onclick();
assert.equal($('apiModelTestResult').textContent, 'Enter the API key.');
assert.equal(document.activeElement, $('apiModelKey'));

// Test the unsaved form: the key only in the POST body; every button waits; the result inline.
type('apiModelKey', other);
discovered();
type('apiModelName', 'Sol via lab gateway');
$('apiModelTestButton').onclick();
assert.equal(sent.length, 2);
assert.equal(sent[1].path, '/api-models/test');
assert.equal(sent[1].options.method, 'POST');
assert.deepEqual(sent[1].body, {format: 'openai', baseUrl: 'https://gateway.lab.example/v1', requestSource: '',
  apiKey: other, model: 'gpt-6.1-sol', label: 'Sol via lab gateway'});
assert(!sent[1].path.includes(other));
assert.equal($('apiModelTestButton').textContent, 'Testing…');
assert(buttons().every((button) => button.disabled));
assert.equal($('apiModels').getAttribute('aria-busy'), 'true');
sent[1].reply(200, {ok: false, status: 401, latencyMs: 188, message: 'Incorrect API key provided: [redacted].',
  model: 'gpt-6.1-sol', testedAt: '2026-10-22T09:44:51+00:00', recorded: false});
await settle();
const parts = () => $('apiModelTestResult').children.map((part) => [part.tagName, part.className, part.textContent]);
assert.deepEqual(parts(), [['STRONG', '', 'Failed · 401'],
  ['SPAN', 'api-test-detail', 'Incorrect API key provided: [redacted].']]);
assert.match($('apiModelTestResult').className, /\berror\b/);
assert.equal($('apiModelTestButton').textContent, 'Test connection');
assert(!$('apiModelTestButton').disabled);
// The outcome is written once the manager is no longer busy, so that it is announced.
assert.equal($('apiModels').getAttribute('aria-busy'), 'false');

// Editing what was tested makes the shown result stale (here: a new key); the display name does not.
type('apiModelName', 'Sol via lab gateway');
assert.match($('apiModelTestResult').className, /\berror\b/);
type('apiModelKey', 'sk-typing');
assert.match($('apiModelTestResult').className, /\bstale\b/);
assert.deepEqual(parts().map((part) => part[2]), ['Changed since the last test',
  'Last result: Failed · 401. Test again to check these settings.']);

type('apiModelKey', key);
discovered();
$('apiModelTestButton').onclick();
sent[2].reply(200, {ok: true, status: 200, latencyMs: 412.4, message: 'The model replied: OK',
  model: 'gpt-6.1-sol-2026-10', testedAt: '2026-10-22T09:45:51+00:00', recorded: false});
await settle();
assert.equal($('apiModelTestResult').children[0].textContent, 'Connected · 412 ms');
assert.equal($('apiModelTestResult').children[1].textContent, 'The model replied: OK · model gpt-6.1-sol-2026-10');
assert.equal($('apiModelKey').value, key);

// A network failure has no status: its message stands alone.
$('apiModelTestButton').onclick();
sent[3].reply(200, {ok: false, status: null, latencyMs: 30000, message: 'No response within 30 seconds (TimeoutError).',
  model: 'gpt-6.1-sol', testedAt: '2026-10-22T09:46:51+00:00', recorded: false});
await settle();
assert.deepEqual(parts().map((part) => part[2]), ['Connection failed', 'No response within 30 seconds (TimeoutError).']);

// A field edited while a test runs: its result arrives already marked stale.
$('apiModelTestButton').onclick();
type('apiModelId', 'gpt-6.1-sol');
sent[4].reply(200, {ok: true, status: 200, latencyMs: 90, message: 'The model replied: OK', model: 'gpt-6.1-sol',
  testedAt: '2026-10-22T09:47:51+00:00', recorded: false});
await settle();
assert.deepEqual(parts().map((part) => part[2]), ['Changed since the last test',
  'Last result: Connected · 90 ms. Test again to check these settings.']);
sent.splice(4, 1);

// Save: the key goes once, the field is cleared, the list and every role menu refresh.
$('apiModelSaveButton').onclick();
assert.equal(sent[4].path, '/api-models');
assert.equal(sent[4].body.apiKey, key);
assert.equal(sent[4].body.id, undefined);
assert.equal($('apiModelSaveButton').textContent, 'Saving…');
calls.length = 0;
sent[4].reply(200, {model: lab, ...listing([lab])});
await settle();
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelBaseUrl').value, '');
assert.equal($('apiModelSaveButton').textContent, 'Save model');
assert.equal($('apiModelTestResult').children[0].textContent, 'Saved Sol via lab gateway.');
type('apiModelBaseUrl', 'h');
assert.equal($('apiModelTestResult').children[0].textContent, 'Saved Sol via lab gateway.');
type('apiModelBaseUrl', '');
assert.deepEqual(calls, ['menus', 'efforts:review', 'efforts:author', 'efforts:critic', 'efforts:writer', 'retrieval', 'summary']);
assert.deepEqual(state.workflow.settings.model_catalog.map((row) => row.id), ['api:lab-sol']);
assert.deepEqual(state.workflow.settings.model_efforts['api:lab-sol'], ['low', 'medium', 'high', 'xhigh', 'max']);
assert.deepEqual(state.workflow.nodes, {});
assert.equal($('apiModelCount').textContent, '1 saved');
const row = $('apiModelList').children[0];
assert.equal(row.querySelector('strong').textContent, 'Sol via lab gateway');
assert.match(row.textContent, /gpt-6\.1-sol.*gateway\.lab\.example.*key …2223.*not tested yet/);
assert.equal(row.querySelector('span.format-chip').textContent, 'OpenAI');
assert.equal(row.querySelector('span.status-chip').textContent, 'Not tested');
assert.equal(rowButton('remove', 'api:lab-sol').getAttribute('aria-label'), 'Remove Sol via lab gateway');

// Test a saved model by its id: the recorded result replaces the row; focus returns to its Test.
rowButton('test', 'api:lab-sol').focus();
click(rowButton('test', 'api:lab-sol'));
assert.deepEqual(sent[5].body, {id: 'api:lab-sol'});
const tested = {...lab, lastTest: {ok: false, at: '2026-10-22T09:50:00+00:00', status: 401, message: 'invalid key', latencyMs: 90}};
sent[5].reply(200, {ok: false, status: 401, latencyMs: 90, message: 'invalid key', model: 'gpt-6.1-sol',
  testedAt: '2026-10-22T09:50:00+00:00', recorded: true, apiModel: tested});
await settle();
assert.equal($('apiModelList').children[0].querySelector('span.status-chip').textContent, 'Failed · 401');
assert.equal($('apiModelList').children[0].querySelector('p.api-test-result').textContent, '401 · invalid key');
assert.equal($('apiModelsStatus').textContent, 'Sol via lab gateway: 401 · invalid key');
assert.equal(document.activeElement, rowButton('test', 'api:lab-sol'));

// Edit: the saved settings fill the form, never the key; a blank key keeps the saved one.
click(rowButton('edit', 'api:lab-sol'));
assert.equal($('apiModelFormHeading').textContent, 'Edit Sol via lab gateway');
assert.equal($('apiModelBaseUrl').value, 'https://gateway.lab.example/v1');
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelKey').placeholder, 'Saved key …2223');
assert.equal($('apiModelKeyHelp').textContent, 'Leave blank to keep the saved key; a new key replaces it.');
assert.equal($('apiModelSaveButton').textContent, 'Save changes');
assert.equal($('apiModelCancelButton').hidden, false);
assert($('apiModelList').children[0].classList.contains('editing'));
type('apiModelName', 'Lab Sol');
$('apiModelSaveButton').onclick();
assert.deepEqual(sent[6].body, {format: 'openai', baseUrl: 'https://gateway.lab.example/v1', requestSource: '',
  apiKey: '', model: 'gpt-6.1-sol', label: 'Lab Sol', id: 'api:lab-sol'});
const renamed = {...tested, label: 'Lab Sol'};
sent[6].reply(200, {model: renamed, ...listing([renamed])});
await settle();
assert.equal($('apiModelTestResult').children[0].textContent, 'Saved changes to Lab Sol.');
assert.equal($('apiModelFormHeading').textContent, 'Add an API model');

// Another destination needs the key again (the server would refuse to reuse it).
click(rowButton('edit', 'api:lab-sol'));
type('apiModelBaseUrl', 'https://other.example/v1');
assert.match($('apiModelKeyHelp').textContent, /^Enter the API key again/);
assert.equal($('apiModelKey').required, true);
$('apiModelTestButton').onclick();
assert.equal(sent.length, 7);
assert.match($('apiModelTestResult').textContent, /^Enter the API key again/);
assert.equal(document.activeElement, $('apiModelKey'));
// The optional request source is independent of the API format; authentication is automatic.
type('apiModelRequestSource', 'Zephy-TCS-Prover');
$('apiModelFormatAnthropic').checked = true; $('apiModelFormatOpenai').checked = false;
$('apiModelFormatAnthropic').onchange();
assert.equal($('apiModelRequestSource').value, 'Zephy-TCS-Prover');
assert.match($('apiModelFormatHelp').textContent, /Claude CLI in bare mode.*no subagents, no web tools/);
$('apiModelFormatOpenai').checked = true; $('apiModelFormatAnthropic').checked = false;
$('apiModelFormatOpenai').onchange();
assert.equal($('apiModelRequestSource').value, 'Zephy-TCS-Prover');
$('apiModelCancelButton').onclick();
assert.equal($('apiModelFormHeading').textContent, 'Add an API model');
assert.equal($('apiModelRequestSource').value, '');

// Remove asks first; a refusal leaves everything as it was.
answer = false;
click(rowButton('remove', 'api:lab-sol'));
assert.equal(sent.length, 7);
assert.match(questions[0], /^Remove Lab Sol\?/);
answer = true;
pendingReplaced = ['author', 'critic'];
click(rowButton('remove', 'api:lab-sol'));
assert.deepEqual(sent[7].body, {id: 'api:lab-sol'});
assert.equal(sent[7].path, '/api-models/delete');
sent[7].reply(200, {deleted: 'api:lab-sol', warning: '1 job waiting to start or resume still uses Lab Sol; it cannot continue until that API model is configured again.', ...listing([])});
await settle();
assert.equal($('apiModelsStatus').textContent, 'Removed Lab Sol. 1 job waiting to start or resume still uses Lab Sol; '
  + 'it cannot continue until that API model is configured again. The proof author and independent critic now use GPT 6.1 Sol.');
assert.equal($('apiModelCount').textContent, 'None saved');
assert.equal(document.activeElement, $('apiModelsHeading'));

// The server's refusal (a running job uses it) is shown as it is.
pendingReplaced = [];
const listed = {...lab, id: 'api:busy', label: 'Busy'};
sent.length = 0;
advanced.listeners.toggle({target: advanced});
sent[0].reply(200, listing([listed]));
await settle();
click(rowButton('remove', 'api:busy'));
sent[1].reply(400, {error: 'Busy is in use by a running job. Stop that job before removing this API model.'});
await settle();
assert.equal($('apiModelsStatus').textContent, 'Busy is in use by a running job. Stop that job before removing this API model.');
assert.match($('apiModelsStatus').className, /\bwarning\b/);
assert.equal($('apiModelCount').textContent, '1 saved');

// Nothing the page shows or sends in a URL carries a key.
const shown = Object.values(elements).map((element) => element.textContent + element.placeholder).join('\n');
assert(!shown.includes(key) && !shown.includes(other));
assert(sent.every((request) => !request.path.includes('sk-')));
""")

    def test_api_model_manager_gives_focus_back_and_writes_outcomes_once_ready(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
// A browser moves focus to the page when the focused button is disabled; this fake does it by hand.
document.body = new Element('body');
const dropFocus = () => { document.activeElement = document.body; };
// Record whether the manager was still busy when each outcome was written (a busy live region's
// news may go unannounced) and where focus was by then.
const written = [];
const proto = Object.getOwnPropertyDescriptor(Element.prototype, 'textContent');
Object.defineProperty($('apiModelsStatus'), 'textContent', {get() { return proto.get.call(this); }, set(value) {
  if (value) written.push(['status', $('apiModels').getAttribute('aria-busy'), document.activeElement]);
  proto.set.call(this, value);
}});
const replace = $('apiModelTestResult').replaceChildren;
$('apiModelTestResult').replaceChildren = function (...items) {
  if (items.length) written.push(['result', $('apiModels').getAttribute('aria-busy'), document.activeElement]);
  return replace.apply(this, items);
};
advanced.open = true;
advanced.listeners.toggle({target: advanced});
sent[0].reply(200, listing([lab]));
await settle();
type('apiModelBaseUrl', 'https://gateway.lab.example/v1');
type('apiModelKey', 'sk-test-0123456789abcdef');
discovered();
type('apiModelId', 'gpt-6.1-sol');

// Test connection from the keyboard: focus comes back to the button, then the outcome is written.
$('apiModelTestButton').focus();
$('apiModelTestButton').onclick();
dropFocus();
written.length = 0;
sent[1].reply(200, {ok: true, status: 200, latencyMs: 12, message: 'The model replied: OK', model: 'gpt-6.1-sol',
  testedAt: '2026-10-22T10:00:00+00:00', recorded: false});
await settle();
assert.equal(document.activeElement, $('apiModelTestButton'));
assert.deepEqual(written, [['result', 'false', $('apiModelTestButton')]]);

// Focus the user moved elsewhere meanwhile is left alone.
$('apiModelTestButton').onclick();
$('apiModelName').focus();
sent[2].reply(200, {ok: true, status: 200, latencyMs: 12, message: 'The model replied: OK', model: 'gpt-6.1-sol',
  testedAt: '2026-10-22T10:01:00+00:00', recorded: false});
await settle();
assert.equal(document.activeElement, $('apiModelName'));

// Save: refused, then saved; focus returns to Save both times.
$('apiModelSaveButton').focus();
$('apiModelSaveButton').onclick();
dropFocus();
sent[3].reply(400, {error: 'The API format decides which CLI runs this model, so it cannot change while jobs use it.'});
await settle();
assert.equal(document.activeElement, $('apiModelSaveButton'));
assert.deepEqual($('apiModelTestResult').children.map((part) => part.textContent),
  ['Not saved', 'The API format decides which CLI runs this model, so it cannot change while jobs use it.']);
$('apiModelSaveButton').onclick();
dropFocus();
written.length = 0;
const other = {...lab, id: 'api:other', label: 'Other'};
sent[4].reply(200, {model: other, ...listing([lab, other])});
await settle();
assert.equal(document.activeElement, $('apiModelSaveButton'));
assert.deepEqual(written, [['result', 'false', $('apiModelSaveButton')]]);

// A row's Test: while it runs its name says so; then focus moves to the rebuilt row's Test.
const test = rowButton('test', 'api:lab-sol');
test.focus();
click(test);
assert.equal(test.textContent, 'Testing…');
assert.equal(test.getAttribute('aria-label'), 'Testing Sol via lab gateway…');
dropFocus();
written.length = 0;
sent[5].reply(200, {ok: true, status: 200, latencyMs: 30, message: 'The model replied: OK', model: 'gpt-6.1-sol',
  testedAt: '2026-10-22T10:02:00+00:00', recorded: true, apiModel: {...lab, lastTest: {ok: true,
  at: '2026-10-22T10:02:00+00:00', status: 200, message: 'The model replied: OK', latencyMs: 30}}});
await settle();
assert.notEqual(rowButton('test', 'api:lab-sol'), test);
assert.equal(document.activeElement, rowButton('test', 'api:lab-sol'));
assert.equal(rowButton('test', 'api:lab-sol').getAttribute('aria-label'), 'Test Sol via lab gateway');
assert.deepEqual(written, [['status', 'false', rowButton('test', 'api:lab-sol')]]);

// Remove, refused: focus returns to the row's Remove. Removed: the list heading takes it first.
click(rowButton('remove', 'api:other'));
dropFocus();
sent[6].reply(400, {error: 'Other is in use by a running job. Stop that job before removing this API model.'});
await settle();
assert.equal(document.activeElement, rowButton('remove', 'api:other'));
click(rowButton('remove', 'api:other'));
dropFocus();
written.length = 0;
sent[7].reply(200, {deleted: 'api:other', warning: '', ...listing([lab])});
await settle();
assert.equal(document.activeElement, $('apiModelsHeading'));
assert.deepEqual(written, [['status', 'false', $('apiModelsHeading')]]);
assert.equal($('apiModelsStatus').textContent, 'Removed Other.');
""")

    def test_api_model_discovery_populates_a_picker_without_typing_a_model_id(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
assert.equal($('apiModelId').tagName, 'SELECT');
assert.equal($('apiModelId').disabled, true);
$('apiModelLoadButton').onclick();
assert.equal(sent.length, 0);
assert.equal($('apiModelTestResult').textContent, 'Enter the base URL.');
type('apiModelBaseUrl', 'https://gateway.lab.example/v1');
type('apiModelKey', 'sk-discovery-secret');
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 0);
assert.equal($('apiModelTestResult').textContent, 'Load the available models, then choose a model.');
assert.equal(document.activeElement, $('apiModelLoadButton'));
$('apiModelLoadButton').onclick();
assert.equal(sent[0].path, '/api-models/discover');
assert.equal(sent[0].options.method, 'POST');
assert.deepEqual(sent[0].body, {format: 'openai', baseUrl: 'https://gateway.lab.example/v1',
  requestSource: '', apiKey: 'sk-discovery-secret'});
assert.equal($('apiModelLoadButton').textContent, 'Loading models…');
assert(buttons().every((button) => button.disabled));
sent[0].reply(200, {ok: true, status: 200, models: [{id: 'model-one', label: 'First model'},
  {id: 'model-two', label: '<script>plain text</script>'}]});
await settle();
assert.equal($('apiModelId').disabled, false);
assert.equal($('apiModelId').value, 'model-one');
assert.deepEqual($('apiModelId').options.map((option) => [option.value, option.textContent]),
  [['model-one', 'First model'], ['model-two', '<script>plain text</script>']]);
assert.equal($('apiModelTestResult').children[0].textContent, 'Loaded 2 models.');
$('apiModelId').value = 'model-two';
$('apiModelId').onchange();
$('apiModelTestButton').onclick();
assert.equal(sent[1].path, '/api-models/test');
assert.equal(sent[1].body.model, 'model-two');
sent[1].reply(200, {ok: true, status: 200, model: 'model-two', message: 'The model replied: OK'});
await settle();
type('apiModelName', 'My model');
assert.match($('apiModelTestResult').className, /\bok\b/);
type('apiModelRequestSource', 'Zephy-TCS-Prover');
assert.equal($('apiModelId').value, '');
assert.equal($('apiModelId').disabled, true);
assert.match($('apiModelTestResult').className, /\bstale\b/);
type('apiModelBaseUrl', 'https://other.example/v1');
assert.equal($('apiModelId').value, '');
assert.equal($('apiModelId').disabled, true);
assert.match($('apiModelTestResult').className, /\bstale\b/);
""")

    def test_api_model_discovery_ignores_results_for_edited_credentials_and_clears_failures(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
type('apiModelBaseUrl', 'https://gateway.lab.example/v1');
type('apiModelKey', 'sk-discovery-secret');
$('apiModelLoadButton').onclick();
type('apiModelKey', 'sk-another-secret');
sent[0].reply(200, {ok: true, status: 200, models: [{id: 'wrong-account-model', label: 'Old model'}]});
await settle();
assert.equal($('apiModelId').value, '');
assert.equal($('apiModelId').disabled, true);
assert.equal($('apiModelTestResult').children[0].textContent, 'Connection details changed');
$('apiModelLoadButton').onclick();
sent[1].reply(200, {ok: true, status: 200, models: [{id: 'new-model', label: 'New model'}]});
await settle();
assert.equal($('apiModelId').value, 'new-model');
$('apiModelLoadButton').onclick();
sent[2].reply(200, {ok: false, status: 401, message: 'Missing API key', models: []});
await settle();
assert.equal($('apiModelId').value, '');
assert.equal($('apiModelId').disabled, true);
assert.deepEqual($('apiModelTestResult').children.map((part) => part.textContent), ['Failed · 401', 'Missing API key']);
$('apiModelLoadButton').onclick();
sent[3].reply(200, {ok: false, status: 200, models: [], message: 'No models are available for this API key.'});
await settle();
assert.equal($('apiModelTestResult').children[0].textContent, 'No models loaded');
assert.equal($('apiModelTestResult').children[1].textContent, 'No models are available for this API key.');
$('apiModelLoadButton').onclick();
sent[4].reply(400, {error: 'The base URL is invalid.'});
await settle();
assert.equal($('apiModelTestResult').children[1].textContent, 'The base URL is invalid.');
assert.equal($('apiModels').getAttribute('aria-busy'), 'false');
$('apiModelLoadButton').onclick();
type('apiModelRequestSource', 'Another router source');
sent[5].reply(200, {ok: true, status: 200, models: [{id: 'old-source-model', label: 'Old source'}]});
await settle();
assert.equal($('apiModelId').value, '');
assert.equal($('apiModelTestResult').children[0].textContent, 'Connection details changed');
""")

    def test_api_model_discovery_refreshes_saved_choices_without_exposing_the_saved_key(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open = true;
advanced.listeners.toggle({target: advanced});
sent[0].reply(200, listing([lab]));
await settle();
click(rowButton('edit', lab.id));
assert.equal($('apiModelId').value, lab.model);
assert.equal($('apiModelId').disabled, false);
type('apiModelRequestSource', 'Proof lab');
assert.equal($('apiModelKey').required, false);
assert.equal($('apiModelId').value, '');
$('apiModelLoadButton').onclick();
assert.deepEqual(sent[1].body, {id: lab.id, format: 'openai', baseUrl: lab.baseUrl,
  requestSource: 'Proof lab', apiKey: ''});
sent[1].reply(200, {ok: true, status: 200, models: [{id: lab.model, label: 'Sol'},
  {id: 'other-model', label: 'Other'}]});
await settle();
assert.equal($('apiModelId').value, lab.model);
assert.equal($('apiModelKey').value, '');
$('apiModelSaveButton').onclick();
assert.equal(sent[2].path, '/api-models');
assert.equal(sent[2].body.requestSource, 'Proof lab');
assert.equal(sent[2].body.apiKey, '');
assert.equal(sent[2].body.headerName, undefined);
const sourced = {...lab, requestSource: 'Proof lab'};
sent[2].reply(200, {model: sourced, ...listing([sourced])});
await settle();
assert.equal($('apiModelRequestSource').value, '');
click(rowButton('edit', lab.id));
assert.equal($('apiModelRequestSource').value, 'Proof lab');
type('apiModelBaseUrl', 'https://other.example/v1');
$('apiModelLoadButton').onclick();
assert.equal(sent.length, 3);
assert.match($('apiModelTestResult').textContent, /^Enter the API key again/);
$('apiModelCancelButton').onclick();
assert.equal($('apiModelId').value, '');
assert.equal($('apiModelId').disabled, true);
""")

    def test_api_model_manager_explains_an_older_server(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open = true;
advanced.listeners.toggle({target: advanced});
sent[0].reply(404, {error: 'Not found.'});
await settle();
assert.equal($('apiModelsStatus').textContent, 'This TCS Prover server cannot manage API models. Restart TCS Prover to update it.');
assert.equal($('apiModelCount').textContent, 'Unavailable');
assert($('apiModelTestButton').disabled && $('apiModelSaveButton').disabled);
""")


class _ClosingEndpoint(BaseHTTPRequestHandler):
    """An endpoint that ends the connection after each response (HTTP/1.0)."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if key == "sk-good-0123456789":
            status, value = 200, {"model": "gpt-6.1-sol-2026-10", "output_text": "OK"}
        else:
            status, value = 401, {"error": {"message": f"Incorrect API key provided: {key}."}}
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class ApiModelTestEndpointTests(unittest.TestCase):
    """The Test button's server request reads the reply of an endpoint that closes the connection."""

    def setUp(self):
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _ClosingEndpoint)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def entry(self, key):
        return {"format": "openai", "baseUrl": f"http://127.0.0.1:{self.http.server_port}/v1",
                "headerName": "Authorization", "model": "gpt-6.1-sol", "apiKey": key}

    def test_the_providers_message_is_kept_with_the_key_redacted(self):
        result = server.probe_api_endpoint(self.entry("sk-wrong-0123456789"), timeout=10)
        self.assertEqual((result["ok"], result["status"]), (False, 401))
        self.assertEqual(result["message"], "Incorrect API key provided: [redacted].")

    def test_a_reply_and_the_reported_model_are_kept(self):
        result = server.probe_api_endpoint(self.entry("sk-good-0123456789"), timeout=10)
        self.assertEqual((result["ok"], result["status"]), (True, 200))
        self.assertEqual(result["message"], "The model replied: OK")
        self.assertEqual(result["model"], "gpt-6.1-sol-2026-10")


if __name__ == "__main__":
    unittest.main()
