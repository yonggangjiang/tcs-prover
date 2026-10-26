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
    groups = [[providers.PROVIDER_LABELS[kind], kind, [[row['id'], row['id'] == runtime.MODEL, row['label']]
             for row in providers.BUILTIN_MODELS if row['provider'] == kind]] for kind in ('codex', 'claude')]
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
        for provider, label in (("codex", "Codex CLI"),):
            rows = [row for row in providers.BUILTIN_MODELS if row["provider"] == provider]
            expected.append([label, provider, [
                [row["id"], row["id"] == runtime.MODEL,
                 row["label"] + (" — default" if row["id"] == runtime.MODEL else "")] for row in rows]])
        for role in ROLES:
            with self.subTest(role=role):
                groups = [[label, provider, [[value, selected, text.strip()] for value, selected, text in options]]
                          for label, provider, options in parsed.menus[f"{role}Model"]]
                self.assertEqual(groups, expected)
                source_options = parsed.menus[f"{role}Source"]
                self.assertEqual([option[0] for option in source_options], ['codex', 'claude'])
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
        for identity in ("apiModelBaseUrl", "apiModelRequestSource", "apiModelName", "apiModelKey"):
            self.assertIn(identity, [attrs.get("for") for attrs in _labels(INDEX)])
        self.assertNotIn("apiModelId", parsed.elements)
        self.assertNotIn("Model ID", parsed.texts["apiModelForm"])
        markup = INDEX.read_text(encoding="utf-8")
        self.assertLess(markup.index('id="apiModelBaseUrl"'), markup.index('id="apiModelKey"'))
        self.assertLess(markup.index('id="apiModelKey"'), markup.index('id="apiModelRequestSource"'))
        self.assertIn('class="api-optional-settings"', markup)
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
for (const role of ['review', 'author', 'critic', 'writer']) {
  ui[role + 'Model'] = roleMenu(fixture.menu);
  ui[role + 'Source'] = new Select([new Option('Codex CLI', 'codex'), new Option('Claude CLI', 'claude')]);
  ui[role + 'Effort'] = new Select([new Option('Max', 'max')]);
}
const modelProvider = (id) => fixture.catalog.find((row) => row.id === id)?.provider
  || (id.startsWith('api:') ? 'api' : id.startsWith('claude-') ? 'claude' : 'codex');
""")

    def test_source_model_picker_filters_and_preserves_independent_role_choices(self):
        rows = [{"id": "api:key-a-model-one", "sourceId": "api:key-a", "sourceLabel": "Key A", "label": "Model one"},
                {"id": "api:key-a-model-two", "sourceId": "api:key-a", "sourceLabel": "Key A", "label": "Model two"},
                {"id": "api:key-b-model-one", "sourceId": "api:key-b", "sourceLabel": "Key B", "label": "Model one"}]
        self.run_app(self.MENUS, self.menus_setup(rows), r"""
syncModelMenus();
assert.deepEqual(ui.authorSource.options.map((o) => o.value), ['codex', 'claude', 'api:key-a', 'api:key-b']);
assert(ui.authorModel.options.every((o) => o.value.startsWith('gpt-')));
syncRoleModelMenu('author', 'api:key-a');
assert.equal(ui.authorSource.value, 'api:key-a');
assert.deepEqual(ui.authorModel.options.map((o) => o.value), ['api:key-a-model-one', 'api:key-a-model-two']);
ui.authorModel.value = 'api:key-a-model-two';
syncRoleModelMenu('critic', 'api:key-b');
assert.equal(ui.criticModel.value, 'api:key-b-model-one');
assert.equal(ui.writerSource.value, 'codex');
syncModelMenus();
assert.equal(ui.authorModel.value, 'api:key-a-model-two');
assert.equal(ui.criticModel.value, 'api:key-b-model-one');
state.workflow.settings.model_catalog = state.workflow.settings.model_catalog.map((row) =>
  row.id === 'api:key-a-model-two' ? {...row, label: 'Renamed'} : row);
syncModelMenus();
assert.equal(ui.authorModel.selectedOptions[0].textContent, 'Renamed');
state.workflow.settings.model_catalog = state.workflow.settings.model_catalog.filter((row) => row.sourceId !== 'api:key-a');
assert.deepEqual(syncModelMenus(), ['author']);
assert.equal(ui.authorSource.value, 'codex');
assert.equal(ui.authorModel.value, 'gpt-6.1-sol');
assert.equal(ui.criticModel.value, 'api:key-b-model-one');
syncRoleModelMenu('writer', 'claude');
assert(ui.writerModel.options.every((o) => o.value.startsWith('claude-')));
assert.equal(ui.writerModel.value, 'claude-fable-5-1');
""")

    def test_saved_stand_ins_are_removed_when_selecting_a_new_source_or_job(self):
        self.run_app(self.MENUS, self.menus_setup([]), r"""
syncModelMenus();
setChoice(ui.authorModel, 'api:gone');
setChoice(ui.authorModel, 'api:gone');
assert.equal(ui.authorModel.options.filter((o) => o.value === 'api:gone').length, 1);
assert.equal(ui.authorModel.value, 'api:gone');
assert.equal(ui.authorModel.selectedOptions[0].textContent, 'api:gone (not configured)');
setChoice(ui.criticModel, 'gpt-5.6-sol');
assert.equal(ui.criticModel.selectedOptions[0].textContent, 'gpt-5.6-sol (older)');
setChoice(ui.authorModel, 'gpt-6.1-sol');
assert.equal(ui.authorSource.value, 'codex');
assert(!ui.authorModel.options.some((o) => o.dataset.standIn));
state.workflow.settings.model_sources = [{id:'codex',label:'Codex CLI'}, {id:'api:empty',label:'Empty key'}];
setChoice(ui.authorModel, 'api:missing-source');
assert.equal(state.workflow.settings.model_sources.length,2);
syncRoleModelMenu('writer', 'api:empty');
assert.equal(ui.writerModel.value, '');
assert.equal(ui.writerModel.disabled, true);
assert.equal(ui.writerEffort.disabled, true);
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
const show = (element, visible) => { element.hidden = !visible; };
const selectedProblemMode = () => 'statement';
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
assert.equal(notice, `${rule} Choose a Codex CLI model for the proof author and LaTeX writer.`);
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
assert.equal(ui.retrievalConfigurationNotice.textContent, `${rule}`);
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
  + 'New problem → Advanced settings → API connections with the same settings (Sol via lab gateway · OpenAI format · '
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
  + 'configured. Add it again under New problem → Advanced settings → API connections with the display name “gone” '
  + 'to continue this job.');
""")

    MANAGER = [["// API models (Advanced settings", "// Appearance: the dark theme"]]
    MANAGER_SETUP = r"""
const elements = {};
const $ = (id) => elements[id] ||= new Element(id === 'apiModelList' ? 'ul' : 'div');
const advanced = new Element('details');
const ui = {input: {querySelector: () => advanced}};
$('apiModelFormatOpenai').value = 'openai'; $('apiModelFormatOpenai').checked = true;
$('apiModelFormatAnthropic').value = 'anthropic';
const sessionToken = 'session-token';
let state = {workflow: {settings: {model_catalog: [], model_efforts: {}}}};
const show = (element, visible) => { element.hidden = !visible; };
const syncModelMenus = () => [], syncEffortChoices = () => {}, renderRetrievalSettings = () => {}, updateModelSummary = () => {};
let answer = true;
const confirm = () => answer;
const sent = [];
const fetch = (path, options = {}) => new Promise((resolve) => sent.push({path, options,
  body: options.body ? JSON.parse(options.body) : undefined,
  reply: (status, value) => resolve({ok: status < 400, status, json: async () => value})}));
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const connection = {id: 'api:lab', label: 'Lab key', format: 'openai', baseUrl: 'https://lab.example/v1',
  host:'lab.example', requestSource:'', hasKey:true, keyHint:'…abcd', models:[
    {id:'model-a',label:'Model A',modelId:'api:lab-a'}, {id:'model-b',label:'Model B',modelId:'api:lab-b'}]};
const listing = (connections) => ({connections, models:connections, model_sources:connections.map((c) => ({id:c.id,label:c.label})),
  model_catalog:connections.flatMap((c) => c.models.map((m) => ({id:m.modelId,label:m.label,provider:'api',sourceId:c.id}))),
  model_efforts:{}});
const type = (id, value) => { $(id).value = value; $(id).oninput?.(); };
const rowButton = (action) => $('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === action);
const click = (button) => $('apiModelList').onclick({target:button});
"""

    def test_save_connection_requires_only_url_and_key_and_loads_all_model_choices(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 0);
assert.equal($('apiModelTestResult').textContent, 'Enter the base URL.');
type('apiModelBaseUrl', connection.baseUrl);
$('apiModelSaveButton').onclick();
assert.equal($('apiModelTestResult').textContent, 'Enter the API key.');
type('apiModelKey', 'secret-api-key');
$('apiModelSaveButton').onclick();
assert.equal(sent[0].path, '/api-models');
assert.deepEqual(sent[0].body, {format:'openai',baseUrl:connection.baseUrl,apiKey:'secret-api-key',requestSource:'',label:''});
assert.equal(sent[0].body.model, undefined);
assert.equal(sent[0].body.headerName, undefined);
assert.equal($('apiModels').getAttribute('aria-busy'), 'true');
sent[0].reply(200, {connection,...listing([connection])});
await settle();
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelBaseUrl').value, '');
assert.equal($('apiModels').getAttribute('aria-busy'), 'false');
assert.equal($('apiModelCount').textContent, '1 saved');
assert.match($('apiModelList').textContent, /Lab key.*2 models/);
assert.equal(state.workflow.settings.model_catalog.length, 2);
assert.equal(state.workflow.settings.model_sources[0].id, connection.id);
assert.match($('apiModelTestResult').textContent, /Choose this key under Source/);
assert(!Object.keys(elements).includes('apiModelId'));
""")

    def test_test_connection_has_no_model_and_marks_results_stale_after_edits(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
type('apiModelBaseUrl', connection.baseUrl);
type('apiModelKey', 'secret-api-key');
type('apiModelRequestSource', 'Proof lab');
$('apiModelTestButton').onclick();
assert.equal(sent[0].path, '/api-models/test');
assert.equal(sent[0].body.model, undefined);
assert.equal(sent[0].body.requestSource, 'Proof lab');
type('apiModelKey', 'new-key');
sent[0].reply(200, {ok:true,status:200,message:'Found 2 models.',latencyMs:12});
await settle();
assert.equal($('apiModelTestResult').children[0].textContent, 'Connection details changed');
$('apiModelTestButton').onclick();
sent[1].reply(200, {ok:false,status:401,message:'Invalid API key'});
await settle();
assert.match($('apiModelTestResult').textContent, /401.*Invalid API key/);
type('apiModelName', 'Local label');
assert.match($('apiModelTestResult').className, /error/);
type('apiModelRequestSource', 'New source');
assert.match($('apiModelTestResult').className, /stale/);
""")

    def test_saved_key_edit_refresh_remove_and_secret_binding(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open = true; advanced.listeners.toggle({target:advanced});
assert.equal(sent[0].options.headers['X-TCS-Prover-Token'], sessionToken);
sent[0].reply(200, listing([connection]));
await settle();
click(rowButton('edit'));
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelKey').required, false);
$('apiModelTestButton').onclick();
sent[1].reply(200,{ok:true,status:200,message:'Found 2 models.',recorded:true,
  connection:{...connection,lastTest:{ok:true,at:'2026-10-26',message:'Found 2 models.'}}});
await settle();
assert.match($('apiModelList').textContent,/Connected/);
sent.splice(1,1);
type('apiModelName', 'New local label');
$('apiModelSaveButton').onclick();
assert.equal(sent[1].body.apiKey, '');
assert.equal(sent[1].body.id, connection.id);
const renamed = {...connection,label:'New local label'};
sent[1].reply(200, {connection:renamed,...listing([renamed])});
await settle();
click(rowButton('edit'));
type('apiModelBaseUrl', 'https://other.example/v1');
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 2);
assert.match($('apiModelTestResult').textContent, /Enter the API key again/);
$('apiModelCancelButton').onclick();
click(rowButton('refresh'));
assert.equal(sent[2].path, '/api-models/discover');
assert.deepEqual(sent[2].body, {id:connection.id});
const refreshed = {...renamed,models:[connection.models[0]]};
sent[2].reply(200, {ok:true,message:'Found 1 model.',...listing([refreshed])});
await settle();
assert.equal(state.workflow.settings.model_catalog.length, 1);
answer = false; click(rowButton('remove')); assert.equal(sent.length, 3);
answer = true; click(rowButton('remove'));
assert.equal(sent[3].path, '/api-models/delete');
sent[3].reply(200, {deleted:connection.id,...listing([])});
await settle();
assert.equal($('apiModelCount').textContent, 'None saved');
assert.equal(document.activeElement, $('apiModelsHeading'));
assert(sent.every((request) => !request.path.includes('secret')));
""")

    def test_connection_save_errors_keep_the_key_and_newer_form_edits(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
type('apiModelBaseUrl', connection.baseUrl); type('apiModelKey','secret-api-key');
$('apiModelSaveButton').onclick();
sent[0].reply(400,{error:'The endpoint returned no model choices.'});
await settle();
assert.equal($('apiModelKey').value,'secret-api-key');
assert.match($('apiModelTestResult').textContent,/no model choices/);
$('apiModelSaveButton').onclick();
type('apiModelKey','a-newer-key');
sent[1].reply(200,{connection,...listing([connection])});
await settle();
assert.equal($('apiModelKey').value,'a-newer-key');
assert.equal($('apiModelBaseUrl').value,connection.baseUrl);
$('apiModelSaveButton').onclick();
type('apiModelName','A newer local name');
sent[2].reply(200,{connection,...listing([connection])});
await settle();
assert.equal($('apiModelName').value,'A newer local name');
assert.equal($('apiModelKey').value,'a-newer-key');
""")

    def test_older_server_disables_connection_controls(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open=true; advanced.listeners.toggle({target:advanced});
sent[0].reply(404,{error:'Not found'}); await settle();
assert.match($('apiModelsStatus').textContent,/Restart TCS Prover/);
assert($('apiModelTestButton').disabled && $('apiModelSaveButton').disabled);
""")

    def test_saved_connection_populates_real_role_source_model_and_effort_controls(self):
        setup = self.MANAGER_SETUP.replace(
            "const syncModelMenus = () => [], syncEffortChoices = () => {}, renderRetrievalSettings = () => {}, updateModelSummary = () => {};",
            r"""
const renderRetrievalSettings = () => {}, updateModelSummary = () => {};
const modelProvider = (id) => state.workflow.settings.model_catalog.find((row) => row.id === id)?.provider || 'codex';
for (const role of ['review','author','critic','writer']) {
  ui[role+'Source'] = new Select([new Option('Codex CLI','codex')]);
  ui[role+'Model'] = new Select([new Option('GPT 6.1 Sol','gpt-6.1-sol')]);
  ui[role+'Effort'] = new Select(['ultra','max','high','low'].map((value) => new Option(value,value)));
}
""")
        self.run_app(self.MENUS + self.MANAGER, setup, r"""
type('apiModelBaseUrl',connection.baseUrl); type('apiModelKey','secret-api-key');
$('apiModelSaveButton').onclick();
const data = listing([connection]);
data.model_sources.unshift({id:'codex',label:'Codex CLI'}, {id:'claude',label:'Claude CLI'});
data.model_catalog.unshift({id:'gpt-6.1-sol',label:'GPT 6.1 Sol',provider:'codex',sourceId:'codex'});
data.model_efforts = {'gpt-6.1-sol':['low','high','max','ultra'], 'api:lab-a':['low','high'], 'api:lab-b':['high','max']};
sent[0].reply(200,{connection,...data}); await settle();
assert.deepEqual(ui.authorSource.options.map((o) => o.value), ['codex','claude','api:lab']);
syncRoleModelMenu('author','api:lab'); syncEffortChoices('author');
assert.deepEqual(ui.authorModel.options.map((o) => o.value), ['api:lab-a','api:lab-b']);
assert.equal(ui.authorModel.value,'api:lab-a');
assert.equal(ui.authorEffort.value,'high');
assert(ui.authorEffort.options.find((o) => o.value === 'ultra').hidden);
assert.equal(ui.criticSource.value,'codex');
ui.authorModel.value='api:lab-b'; syncEffortChoices('author');
assert.equal(ui.authorEffort.value,'high');
syncModelMenus(); assert.equal(ui.authorModel.value,'api:lab-b');
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
