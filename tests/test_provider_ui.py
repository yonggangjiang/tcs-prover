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

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
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
// URL is a browser global that a new vm context lacks (the API connection form reads base URLs).
const context = {assert, setImmediate, console, URL, finish: () => { finished = true; }};
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
                # The source menus' names, as the server names the sources (providers.PROVIDER_LABELS).
                self.assertEqual([option[2] for option in source_options],
                                 [providers.PROVIDER_LABELS[option[0]] for option in source_options])
                self.assertEqual(source_options[1][2], "Claude Code")
                chip = parsed.elements[f"{role}Model"].get("aria-describedby")
                self.assertEqual(chip, f"{role}ModelProvider")
                self.assertEqual(parsed.elements[chip]["class"], "provider-chip")
                self.assertEqual(parsed.texts[chip].strip(), "Codex CLI")

    def test_the_page_and_the_readme_call_the_claude_program_claude_code(self):
        # The owner's decision C: "Claude Code" everywhere, while the Codex CLI keeps its name.
        self.assertNotIn("Claude CLI", INDEX.read_text(encoding="utf-8"))
        parsed = page()
        self.assertEqual(parsed.texts["modelsMeta"].strip(), "Codex CLI · Claude Code · API models")
        self.assertIn("Codex CLI and Claude Code models use the logins on this computer",
                      " ".join(parsed.texts["modelHelp"].split()))
        self.assertIn("leave room for the max output (Claude Code also asks for at most that much).",
                      " ".join(parsed.texts["apiModelLimitsHelp"].split()))
        # The script says "Claude CLI" only to recognize an earlier controller's words (which it shows as now).
        code = "\n".join(line for line in APP.read_text(encoding="utf-8").splitlines()
                         if not line.strip().startswith("//"))
        before = re.findall(r'(.{0,4})"(?:the )?Claude CLI"', code)
        self.assertTrue(before)
        self.assertTrue(all(text.endswith("=== ") for text in before), before)
        # The README and its guide pages (docs/) name the earlier name once, in the Claude Code section of the
        # models guide, for the activity logs of older jobs.
        guides = {path.name: path.read_text(encoding="utf-8")
                  for path in (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")))}
        self.assertIn("models.md", guides)
        self.assertEqual(sum(" ".join(text.split()).count("Claude CLI") for text in guides.values()), 1)
        self.assertIn("Earlier versions of TCS Prover called this program the Claude CLI; the activity logs of jobs "
                      "run then keep that name.", " ".join(guides["models.md"].split()))
        self.assertRegex(guides["models.md"], r"(?m)^#{2,3} Claude Code$")
        for name, text in guides.items():
            self.assertNotIn("#claude-cli", text, name)

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
        self.assertIn("Claude Code models and API models always run at standard speed", speed)

    def test_api_model_form_keeps_the_key_out_of_sight(self):
        parsed = page()
        key = parsed.elements["apiModelKey"]
        # new-password: no browser fills a saved login password into the key field.
        self.assertEqual((key["tag"], key["type"], key["autocomplete"]), ("input", "password", "new-password"))
        self.assertNotIn("value", key)
        self.assertEqual(key["aria-describedby"].split(), ["apiModelSavedKey", "apiModelOtherServer", "apiModelKeyHelp"])
        # Editing keeps the saved key: its line, Replace key / Keep saved key, and Use saved URL for
        # a base URL on another server. The buttons start hidden; app.js shows them.
        self.assertEqual(parsed.elements["apiModelSavedKey"]["tag"], "p")
        # The sentence naming the server typed in the base URL is no live region: typing is not announced.
        other = parsed.elements["apiModelOtherServer"]
        self.assertEqual((other["tag"], other.get("role"), other.get("aria-live"), parsed.texts["apiModelOtherServer"]),
                         ("p", None, None, ""))
        self.assertNotIn("hidden", other)
        for identity, text in (("apiModelReplaceKey", "Replace key"), ("apiModelKeepKey", "Keep saved key"),
                               ("apiModelUseSavedUrl", "Use saved URL")):
            with self.subTest(button=identity):
                self.assertEqual((parsed.elements[identity]["tag"], parsed.elements[identity]["type"]),
                                 ("button", "button"))
                self.assertIn("hidden", parsed.elements[identity])
                self.assertEqual(parsed.texts[identity].strip(), text)
        for identity in ("apiModelTestResult", "apiModelsStatus", "apiModelSavedKey"):
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
        self.assertGreater(len(markers), 30)
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
  ui[role + 'Source'] = new Select([new Option('Codex CLI', 'codex'), new Option('Claude Code', 'claude')]);
  ui[role + 'Effort'] = new Select([new Option('Max', 'max')]);
}
const modelProvider = (id) => fixture.catalog.find((row) => row.id === id)?.provider
  || (id.startsWith('api:') ? 'api' : id.startsWith('claude-') ? 'claude' : 'codex');
""")

    def test_the_source_menus_call_the_claude_program_claude_code(self):
        self.run_app(self.MENUS, self.menus_setup([]), r"""
const names = (role) => ui[role + 'Source'].options.map((option) => [option.value, option.textContent]);
// Before the server names the sources, the page's own names.
syncModelMenus();
for (const role of ['review', 'author', 'critic', 'writer']) {
  assert.deepEqual(names(role), [['codex', 'Codex CLI'], ['claude', 'Claude Code']]);
}
// As the server names them now, and as an earlier controller still names Claude Code.
for (const label of ['Claude Code', 'Claude CLI']) {
  state.workflow.settings.model_sources = [{id: 'codex', label: 'Codex CLI', provider: 'codex'},
    {id: 'claude', label, provider: 'claude'}];
  syncModelMenus();
  assert.deepEqual(names('author'), [['codex', 'Codex CLI'], ['claude', 'Claude Code']], label);
}
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
// Claude Code models list no Ultra: it is hidden, and a chosen Ultra runs as Max.
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
ui.workflowReview.checked = false;
const show = (element, visible) => { element.hidden = !visible; };
const selectedProblemMode = () => 'statement';
const selectedWorkflowStages = () => ({review:ui.workflowReview.checked,author:true,critic:true,writing:true});
const chip = (role) => ui[role + 'ModelProvider'];
const disabledGroups = (role) => ui[role + 'Model'].children.filter((group) => group.disabled).map((group) => group.label);
""")

    def test_provider_chips_name_the_cli_and_the_api_endpoint(self):
        rows = [{"id": "api:lab-sol", "label": "Sol via lab gateway", "format": "openai", "host": "gateway.lab.example"},
                {"id": "api:keyless", "label": "Keyless", "format": "anthropic", "host": "llm.example.org",
                 "hasKey": False}]
        self.run_app(self.PROVIDERS, self.providers_setup(rows), r"""
for (const [model, text, provider] of [
  ['gpt-6.1-sol', 'Codex CLI', 'codex'], ['claude-haiku-5-5', 'Claude Code', 'claude'],
  ['gpt-5.6-terra', 'Codex CLI · older model', 'codex'],
  ['api:lab-sol', 'API · OpenAI format via Codex CLI · gateway.lab.example', 'api'],
  ['api:gone', 'API · not configured', 'api'],
  ['api:keyless', 'API · Anthropic format via Claude Code · llm.example.org · no key', 'api'],
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
for (const role of ['author', 'critic', 'writer']) assert.deepEqual(disabledGroups(role), ['Claude Code', 'API']);
assert.deepEqual(disabledGroups('review'), []);
assert.equal(chip('author').textContent, 'Claude Code · not with a web cutoff');
assert.equal(chip('author').dataset.state, 'warning');
assert.equal(chip('writer').textContent, 'API · OpenAI format via Codex CLI · gateway.lab.example · not with a web cutoff');
assert.equal(chip('critic').textContent, 'Codex CLI');
assert.equal(chip('review').textContent, 'Claude Code');
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
ui.workflowReview.checked = true;
renderRetrievalSettings();
assert.equal(ui.retrievalConfigurationNotice.textContent,
  `Turn Statement review off to start a historical proof job. ${rule}`);
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
let stages = {review:true,author:true,critic:true,writing:true};
const selectedWorkflowStages = () => stages;
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
stages.review = false;
for (const role of ['author', 'critic', 'writer']) ui[role + 'Model'] = field('gpt-6-luna');
updateModelSummary();
assert.match(text(), /^Fast speed · /);
// LaTeX polish runs the writer only.
mode = 'latex';
stages = {review:false,author:false,critic:false,writing:true};
ui.writerModel = field('claude-haiku-5-5');
updateModelSummary();
assert.equal(text(), 'Standard speed · Fast is Codex CLI only · Detailed activity log · Haiku 5.5/Low writer');
""")

    OVERVIEW = [["function retrievalConfiguration(", "function renderJobs("],
                ["function describe(entry)", "function cardIcon("],
                ["function completedRunLabel(", "function renderWorkflow()"]]

    OVERVIEW_SETUP = r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let stateReceivedAt = Date.now();
const show = (element, visible) => { element.hidden = !visible; };
const clockText = () => '12:00:00';
const timelineRows = new Map();
document.createElement = (tag) => new Element(tag);
let state = {runId: 'r', phase: 'paused', stage: 'solve', activeNode: 'author', statement: 'S', draft: 'S',
  workflow: {nodes: {author: {label: 'Proof author'}}}, trace: [], skipStatementReview: true, criticRounds: 2,
  criticCount: 3,
  authorModel: 'claude-opus-5-5', authorEffort: 'max', criticModel: 'api:lab-sol', criticEffort: 'high',
  writerModel: 'gpt-6.1-sol', writerEffort: 'max',
  apiModels: {'api:lab-sol': {label: 'Sol via lab gateway', format: 'openai', baseUrl: 'https://gateway.lab.example/v1',
    headerName: 'Authorization', model: 'gpt-6.1-sol', sourceLabel: 'Sol via lab gateway'}},
  settingsWarning: 'Statement reviewer: DeepSeek is no longer supported; this role now uses GPT 6.1 Sol.'};
const rows = () => Object.fromEntries(ui.runMetadata.children.map((row) => [row.children[0].textContent, row.children[1].textContent]));
// A role's row: its model, effort and settings, then on a second line where the model runs.
const lines = (label) => rows()[label].split('\n');
const solRow = {id: 'api:lab-sol', label: 'gpt-6.1-sol', provider: 'api', sourceLabel: 'Sol via lab gateway',
  format: 'openai', host: 'gateway.lab.example', wireModel: 'gpt-6.1-sol'};
"""

    def test_job_page_states_the_settings_warning_and_the_api_endpoints(self):
        # No source entries: an older controller reached through the live gateway. The same lines come
        # from the catalog and the job's key-free record of its API models.
        self.run_app(self.OVERVIEW, self.OVERVIEW_SETUP, r"""
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, false);
assert.equal(ui.runSettingsWarning.textContent, state.settingsWarning);
assert.equal(rows()['Settings warning'], state.settingsWarning);
assert.equal(rows()['Proof author'], "claude-opus-5-5 · max · works alone\nSource: Claude Code · this computer's Claude login");
// Without a catalog nothing is claimed about the connection: the line is what the job recorded, never its URL.
assert.equal(rows()['Independent critic'], 'gpt-6.1-sol · high · 3 independent critics as subagents · max 2 consecutive rounds\n'
  + 'Source: API connection “Sol via lab gateway” · as this job recorded it · OpenAI format · gateway.lab.example · '
  + 'runs through the Codex CLI · id api:lab-sol');
assert.doesNotMatch(rows()['Independent critic'], /https:/);
assert.equal(rows()['LaTeX writer'], "gpt-6.1-sol · max\nSource: Codex CLI · this computer's Codex login");
// No warning: the header line is hidden and the row is left out.
state = {...state, settingsWarning: ''};
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, true);
assert.equal(rows()['Settings warning'], undefined);

// The API model is removed while the page is open: /state's catalog no longer lists it. The
// run settings mark it, and the header says what Resume would say, in the server's words.
// The earlier controller's catalog still calls Claude Code "Claude CLI".
const catalog = [{id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol', provider: 'codex', providerLabel: 'Codex CLI'},
  {id: 'claude-opus-5-5', label: 'Claude Opus 5.5', provider: 'claude', providerLabel: 'Claude CLI'}];
// A job cannot switch models: the only remedy is adding it again with the settings it recorded.
const missing = 'The API model api:lab-sol chosen for the critic is not configured. Add it again under '
  + 'New problem → Advanced settings → API connections with the same settings (Sol via lab gateway · OpenAI format · '
  + 'https://gateway.lab.example/v1 · model gpt-6.1-sol) to continue this job.';
state = {...state, workflow: {...state.workflow, settings: {model_catalog: catalog}}};
renderRunOverview();
assert.deepEqual(lines('Independent critic'), ['gpt-6.1-sol · high · 3 independent critics as subagents · max 2 consecutive rounds',
  'Source: API connection “Sol via lab gateway” · not configured now; as this job recorded it · OpenAI format · '
  + 'gateway.lab.example · runs through the Codex CLI · id api:lab-sol']);
// With a catalog, built-in models are named as the menus name them, and the program as the menus now name it.
assert.deepEqual(lines('Proof author'), ['Claude Opus 5.5 · max · works alone', "Source: Claude Code · this computer's Claude login"]);
assert.deepEqual(lines('LaTeX writer'), ['GPT 6.1 Sol · max', "Source: Codex CLI · this computer's Codex login"]);
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
assert.match(lines('Independent critic')[1], /· not configured now; as this job recorded it ·/);
// Configured again: the mark and the warning go away.
state = {...state, readOnly: false, workflow: {...state.workflow, settings: {model_catalog: [...catalog, solRow]}}};
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, true);
assert.equal(lines('Independent critic')[1], 'Source: API connection “Sol via lab gateway” · OpenAI format · '
  + 'gateway.lab.example · runs through the Codex CLI · id api:lab-sol');
// Edited since the job started: the line names the endpoint the job started with too.
state = {...state, workflow: {...state.workflow, settings: {model_catalog: [...catalog,
  {...solRow, format: 'anthropic', host: 'claude.lab.example'}]}}};
renderRunOverview();
assert.equal(lines('Independent critic')[1], 'Source: API connection “Sol via lab gateway” · Anthropic format · '
  + 'claude.lab.example · runs through Claude Code · this job started with gateway.lab.example (OpenAI format, '
  + 'model gpt-6.1-sol) · id api:lab-sol');
// One name for the Claude program in the panel: the source menu's, for the author's CLI and for the format's.
assert.match(lines('Proof author')[1], /^Source: Claude Code ·/);
assert.doesNotMatch(Object.values(rows()).join('\n'), /Claude CLI|the Claude Code/);
// Another wire model under the same id: the first line names the model the next call uses, the source
// line the one the job started with.
state = {...state, workflow: {...state.workflow, settings: {model_catalog: [...catalog,
  {...solRow, label: 'gpt-7-sol', wireModel: 'gpt-7-sol'}]}}};
renderRunOverview();
assert.deepEqual(lines('Independent critic'), ['gpt-7-sol · high · 3 independent critics as subagents · max 2 consecutive rounds',
  'Source: API connection “Sol via lab gateway” · OpenAI format · gateway.lab.example · runs through the Codex CLI · '
  + 'this job started with gateway.lab.example (OpenAI format, model gpt-6.1-sol) · id api:lab-sol']);
// Unchanged, whatever the recorded base URL's spelling: the host compares as the catalog names it (an IPv6
// address without its brackets, lowercase), and the URL never shows.
const unchanged = 'Source: API connection “Sol via lab gateway” · OpenAI format · %s · runs through the Codex CLI · id api:lab-sol';
for (const [baseUrl, host] of [['http://[::1]:8000/v1', '::1'], ['https://Gateway.Lab.EXAMPLE/v1', 'gateway.lab.example'],
  ['http://[FE80::1]:8000', 'fe80::1']]) {
  state = {...state, apiModels: {'api:lab-sol': {...state.apiModels['api:lab-sol'], baseUrl}},
    workflow: {...state.workflow, settings: {model_catalog: [...catalog, {...solRow, host}]}}};
  renderRunOverview();
  assert.equal(lines('Independent critic')[1], unchanged.replace('%s', host));
}
// Removed: the recorded IPv6 host is named as the catalog would name it.
state = {...state, workflow: {...state.workflow, settings: {model_catalog: catalog}}};
renderRunOverview();
assert.equal(lines('Independent critic')[1], 'Source: API connection “Sol via lab gateway” · not configured now; as this '
  + 'job recorded it · OpenAI format · fe80::1 · runs through the Codex CLI · id api:lab-sol');
state = {...state, apiModels: {'api:lab-sol': {...state.apiModels['api:lab-sol'], baseUrl: 'https://gateway.lab.example/v1'}}};
// An older Codex id is not in the menus' catalog: it still runs through the Codex CLI.
state = {...state, writerModel: 'gpt-5.6-sol', workflow: {...state.workflow, settings: {model_catalog: [...catalog, solRow]}}};
renderRunOverview();
assert.deepEqual(lines('LaTeX writer'), ['gpt-5.6-sol · max', "Source: Codex CLI · this computer's Codex login · older model"]);
// Only roles the job still runs count: a skipped reviewer's model is not checked.
state = {...state, reviewModel: 'api:gone', reviewEffort: 'max'};
renderRunOverview();
assert.equal(ui.runSettingsWarning.hidden, true);
assert.equal(rows()['Statement reviewer'], 'Off');
state = {...state, skipStatementReview: false};
renderRunOverview();
// Without a recorded snapshot, the display name that gives the same id.
assert.equal(ui.runSettingsWarning.textContent, 'The API model api:gone chosen for the statement reviewer is not '
  + 'configured. Add it again under New problem → Advanced settings → API connections with the display name “gone” '
  + 'to continue this job.');
assert.deepEqual(lines('Statement reviewer'), ['api:gone · max', 'Source: API connection not configured · id api:gone']);
""")

    def test_run_settings_name_each_roles_source_as_the_server_reports_it(self):
        self.run_app(self.OVERVIEW, self.OVERVIEW_SETUP, r"""
const codex = (model, label, older = false) => ({model, kind: 'codex', label, source: 'Codex CLI', runsThrough: 'codex',
  runsThroughLabel: 'the Codex CLI', older});
const api = (model, values) => ({model, kind: 'api', configured: true, label: 'claude-gw', wireModel: 'claude-gw',
  connection: 'Claude gateway', format: 'anthropic', formatLabel: 'Anthropic format', host: 'claude.example.com',
  runsThrough: 'claude', runsThroughLabel: 'Claude Code', ...values});
const started = {format: 'openai', formatLabel: 'OpenAI format', host: 'gateway.lab.example', wireModel: 'gpt-6.1-sol'};
state = {...state, settingsWarning: '', skipStatementReview: false, reviewModel: 'api:claude-gw', reviewEffort: 'ultra',
  workflow: {...state.workflow, settings: {model_catalog: []}},
  modelSources: {
    review: api('api:claude-gw'),
    author: {model: 'claude-opus-5-5', kind: 'claude', label: 'Claude Opus 5.5', source: 'Claude Code',
      runsThrough: 'claude', runsThroughLabel: 'Claude Code', older: false},
    critic: {...api('api:lab-sol'), label: 'gpt-6.1-sol', wireModel: 'gpt-6.1-sol', connection: 'Sol via lab gateway',
      format: 'openai', formatLabel: 'OpenAI format', host: 'new-gateway.example', runsThrough: 'codex',
      runsThroughLabel: 'the Codex CLI', recorded: started},
    writer: codex('gpt-5.6-sol', 'GPT 5.6 Sol (older)', true)},
  writerModel: 'gpt-5.6-sol'};
renderRunOverview();
assert.deepEqual(lines('Statement reviewer'), ['claude-gw · max (Ultra is no longer offered)',
  'Source: API connection “Claude gateway” · Anthropic format · claude.example.com · runs through Claude Code · '
  + 'id api:claude-gw']);
assert.deepEqual(lines('Proof author'), ['Claude Opus 5.5 · max · works alone', "Source: Claude Code · this computer's Claude login"]);
assert.deepEqual(lines('Independent critic'), ['gpt-6.1-sol · high · 3 independent critics as subagents · max 2 consecutive rounds',
  'Source: API connection “Sol via lab gateway” · OpenAI format · new-gateway.example · runs through the Codex CLI · '
  + 'this job started with gateway.lab.example (OpenAI format, model gpt-6.1-sol) · id api:lab-sol']);
assert.deepEqual(lines('LaTeX writer'), ['GPT 5.6 Sol (older) · max', "Source: Codex CLI · this computer's Codex login · older model"]);
// The server's entry decides, although this catalog lists none of these models: no header warning.
assert.equal(ui.runSettingsWarning.hidden, true);

// Edits that keep the host: another port; another path only (the host, port, format and model read the same,
// so the line says only that it was edited); another wire model under the same id (the first line names the
// model the next call uses, the source line the one the job started with).
const local = {...started, host: 'localhost:8000'};
for (const [values, first, edit] of [
  [{host: 'localhost:9000'}, 'gpt-6.1-sol', 'this job started with localhost:8000 (OpenAI format, model gpt-6.1-sol)'],
  [{host: 'localhost:8000'}, 'gpt-6.1-sol', 'edited since this job started'],
  [{host: 'localhost:8000', label: 'gpt-7-sol', wireModel: 'gpt-7-sol'}, 'gpt-7-sol',
    'this job started with localhost:8000 (OpenAI format, model gpt-6.1-sol)']]) {
  state.modelSources.critic = {...state.modelSources.critic, label: 'gpt-6.1-sol', wireModel: 'gpt-6.1-sol', recorded: local,
    ...values};
  renderRunOverview();
  assert.deepEqual(lines('Independent critic'), [`${first} · high · 3 independent critics as subagents · max 2 consecutive rounds`,
    `Source: API connection “Sol via lab gateway” · OpenAI format · ${values.host} · runs through the Codex CLI · ${edit} · `
    + 'id api:lab-sol']);
}
// Another format only: the format the job started with. A record without a wire model or a host names what it has.
state.modelSources.critic = {...state.modelSources.critic, label: 'gpt-6.1-sol', wireModel: 'gpt-6.1-sol',
  host: 'localhost:8000', format: 'anthropic', formatLabel: 'Anthropic format', runsThroughLabel: 'Claude Code',
  recorded: local};
renderRunOverview();
assert.equal(lines('Independent critic')[1], 'Source: API connection “Sol via lab gateway” · Anthropic format · localhost:8000 · '
  + 'runs through Claude Code · this job started with localhost:8000 (OpenAI format, model gpt-6.1-sol) · id api:lab-sol');
for (const [recorded, edit] of [[{format: 'openai', host: 'localhost:8000'}, 'localhost:8000 (OpenAI format)'],
  [{format: 'openai', host: '', wireModel: 'gpt-6.1-sol'}, 'OpenAI format, model gpt-6.1-sol']]) {
  state.modelSources.critic.recorded = recorded;
  renderRunOverview();
  assert.equal(lines('Independent critic')[1].split(' · this job started with ')[1], `${edit} · id api:lab-sol`);
}
// One name for the Claude program in the panel: the source menu's.
assert.doesNotMatch(Object.values(rows()).join('\n'), /Claude CLI|the Claude Code/);
// An earlier controller's entries still say "Claude CLI": the panel names the program as the menus now do.
const earlier = {...state.modelSources};
state.modelSources = {...earlier, author: {...earlier.author, source: 'Claude CLI', runsThroughLabel: 'the Claude CLI'},
  review: api('api:claude-gw', {runsThroughLabel: 'the Claude CLI'})};
renderRunOverview();
assert.deepEqual(lines('Proof author'), ['Claude Opus 5.5 · max · works alone', "Source: Claude Code · this computer's Claude login"]);
assert.equal(lines('Statement reviewer')[1], 'Source: API connection “Claude gateway” · Anthropic format · claude.example.com · '
  + 'runs through Claude Code · id api:claude-gw');
state.modelSources = earlier;

// The connection list cannot be read now: the line says so, never "removed", and the header claims nothing.
state.modelSources.critic = {model: 'api:lab-sol', kind: 'api', configured: null, reason: 'connection list cannot be read now',
  label: 'gpt-6.1-sol', wireModel: 'gpt-6.1-sol', connection: 'Sol via lab gateway', format: 'openai',
  formatLabel: 'OpenAI format', host: 'gateway.lab.example', runsThrough: 'codex', runsThroughLabel: 'the Codex CLI'};
state.modelSources.review = {model: 'api:claude-gw', kind: 'api', configured: null, reason: 'connection list cannot be read now'};
renderRunOverview();
assert.equal(lines('Independent critic')[1], 'Source: API connection “Sol via lab gateway” · connection list cannot be read '
  + 'now; as this job recorded it · OpenAI format · gateway.lab.example · runs through the Codex CLI · id api:lab-sol');
assert.equal(lines('Statement reviewer')[1], 'Source: API connection · connection list cannot be read now · id api:claude-gw');
assert.doesNotMatch(rows()['Independent critic'] + rows()['Statement reviewer'], /not configured|removed/);
assert.equal(ui.runSettingsWarning.hidden, true);

// Removed: what the job recorded, marked; without a record only the id. The header says how to restore it.
state.modelSources.critic = {...state.modelSources.critic, configured: false};
delete state.modelSources.critic.reason;
state.modelSources.review = {model: 'api:claude-gw', kind: 'api', configured: false};
renderRunOverview();
assert.equal(lines('Independent critic')[1], 'Source: API connection “Sol via lab gateway” · not configured now; as this job '
  + 'recorded it · OpenAI format · gateway.lab.example · runs through the Codex CLI · id api:lab-sol');
assert.deepEqual(lines('Statement reviewer'), ['api:claude-gw · max (Ultra is no longer offered)',
  'Source: API connection not configured · id api:claude-gw']);
assert.match(ui.runSettingsWarning.textContent, /^The API model api:claude-gw chosen for the statement reviewer is not configured\./);
assert.match(ui.runSettingsWarning.textContent, /The API model api:lab-sol chosen for the critic is not configured\./);

// A model TCS Prover no longer supports, and an id it does not know.
state.authorModel = 'retired-model';
state.modelSources.author = {model: 'retired-model', kind: 'removed', replacement: 'gpt-6.1-sol', replacementLabel: 'GPT 6.1 Sol'};
state.writerModel = 'mystery';
state.modelSources.writer = {model: 'mystery', kind: 'unknown'};
renderRunOverview();
assert.deepEqual(lines('Proof author'), ['retired-model · max · works alone', 'Source: no longer supported']);
assert.deepEqual(lines('LaTeX writer'), ['mystery · max', 'Source: unknown model']);
// An entry for another model (the role changed since) is not used: the catalog and the record are.
state.writerModel = 'gpt-6.1-sol';
state.workflow.settings.model_catalog = [{id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol', provider: 'codex', providerLabel: 'Codex CLI'}];
renderRunOverview();
assert.deepEqual(lines('LaTeX writer'), ['GPT 6.1 Sol · max', "Source: Codex CLI · this computer's Codex login"]);
// A stage that is off says so.
state.workflowStages = {review: false, author: true, critic: true, writing: false};
renderRunOverview();
assert.equal(rows()['LaTeX writer'], 'Off');
assert.equal(rows()['Statement reviewer'], 'Off');
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
// The saved key's note as it reads: the live line, then the sentence that names the server typed in the base URL.
const note = () => [$('apiModelSavedKey').textContent, $('apiModelOtherServer').textContent].filter(Boolean).join(' ');
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

    def test_web_search_goes_only_when_on_or_turned_off_and_its_test_search_is_shown(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
// OpenAI-compatible connections only.
assert.equal($('apiModelWebSearchLine').hidden, false);
$('apiModelFormatOpenai').checked = false; $('apiModelFormatAnthropic').checked = true;
$('apiModelFormatAnthropic').onchange();
assert.equal($('apiModelWebSearchLine').hidden, true);
$('apiModelFormatAnthropic').checked = false; $('apiModelFormatOpenai').checked = true;
$('apiModelFormatOpenai').onchange();
type('apiModelBaseUrl', connection.baseUrl);
type('apiModelKey', 'secret-api-key');
$('apiModelWebSearch').checked = true; $('apiModelWebSearch').onchange();
$('apiModelSaveButton').onclick();
assert.equal(sent[0].body.webSearch, true);
// The service did not answer the test search: the switch stays off and the form says why.
sent[0].reply(200, {connection: {...connection, webSearch: false, webSearchCheck: {ok: false,
  message: 'The probe search failed: HTTP 404.'}}, ...listing([connection])});
await settle();
assert.match($('apiModelTestResult').textContent, /stays off: The probe search failed: HTTP 404/);
assert.match($('apiModelTestResult').className, /error/);
// An edited connection that has it on shows it on; turning it off sends that.
const searching = {...connection, webSearch: true, webSearchCheck: {ok: true, message: 'The probe search answered.'}};
sent.length = 0;
apiModels = [searching];
renderApiModels();
click(rowButton('edit'));
assert.equal($('apiModelWebSearch').checked, true);
assert.match($('apiModelList').textContent, /web search/);
$('apiModelWebSearch').checked = false; $('apiModelWebSearch').onchange();
$('apiModelSaveButton').onclick();
assert.equal(sent[0].body.webSearch, false);
""")

    def test_a_save_that_sends_the_test_search_says_so_while_it_runs(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
const testing = 'Loading models, testing web search and saving…', plain = 'Loading models and saving…';
// What the page says while a save runs: the Save button and the result line.
const busy = () => [$('apiModelSaveButton').textContent, $('apiModelTestResult').textContent,
  $('apiModelTestResult').className, $('apiModelSaveButton').disabled];
const searching = {...connection, webSearch: true, webSearchCheck: {ok: true,
  message: 'The probe search answered (514 characters).'}};
let reply = 0;
const save = async (expected, answer, why) => {
  $('apiModelSaveButton').onclick();
  assert.deepEqual(busy(), [expected, expected, 'api-test-result pending', true], why);
  sent[reply++].reply(200, {connection: answer, ...listing([answer])});
  await settle();
  assert.notEqual($('apiModelSaveButton').textContent, expected, why);
};
// A new connection: switched off, saving only loads the models and saves; switched on, it sends the test search.
type('apiModelBaseUrl', connection.baseUrl); type('apiModelKey', 'secret-api-key');
await save(plain, connection, 'new, off');
type('apiModelBaseUrl', connection.baseUrl); type('apiModelKey', 'secret-api-key');
$('apiModelWebSearch').checked = true; $('apiModelWebSearch').onchange();
await save(testing, searching, 'new, on');
// A saved connection that has it on and still goes to the same place (format, base URL, key, request source)
// is not tested again: renamed, given token limits, or with a slash after its base URL.
const edit = () => { click(rowButton('edit')); assert.equal($('apiModelWebSearch').checked, true); };
edit(); type('apiModelName', 'Renamed');
await save(plain, searching, 'renamed');
edit(); type('apiModelContextWindow', '200000');
await save(plain, searching, 'token limits');
edit(); type('apiModelBaseUrl', connection.baseUrl + '/');
await save(plain, searching, 'trailing slash');
// It is tested again when the connection goes elsewhere: another base URL, a new key or another request source.
edit(); type('apiModelBaseUrl', 'https://lab.example/v2');
await save(testing, searching, 'base URL');
edit(); $('apiModelReplaceKey').onclick(); type('apiModelKey', 'new-api-key');
await save(testing, searching, 'new key');
edit(); type('apiModelRequestSource', 'Proof lab');
await save(testing, searching, 'request source');
// Turned off, or an Anthropic connection (which never has it): no test search.
edit(); $('apiModelWebSearch').checked = false; $('apiModelWebSearch').onchange();
await save(plain, connection, 'turned off');
// A saved connection that has it off, turned on: tested.
click(rowButton('edit')); $('apiModelWebSearch').checked = true; $('apiModelWebSearch').onchange();
await save(testing, searching, 'turned on');
edit(); $('apiModelFormatOpenai').checked = false; $('apiModelFormatAnthropic').checked = true;
$('apiModelFormatAnthropic').onchange();
await save(plain, {...connection, format: 'anthropic'}, 'Anthropic');
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
// The saved key is kept, never asked for: its line takes the place of the hidden, disabled field.
assert.equal($('apiModelSavedKey').textContent, 'Saved key …abcd, kept when you save');
assert.equal($('apiModelForm').dataset.keyState, 'kept');
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelKey').hidden, true);
assert.equal($('apiModelKey').disabled, true);
assert.equal($('apiModelKey').required, false);
assert.equal($('apiModelReplaceKey').hidden, false);
assert.equal($('apiModelUseSavedUrl').hidden, true);
$('apiModelTestButton').onclick();
assert.equal('apiKey' in sent[1].body, false);
sent[1].reply(200,{ok:true,status:200,message:'Found 2 models.',recorded:true,
  connection:{...connection,lastTest:{ok:true,at:'2026-10-26',message:'Found 2 models.'}}});
await settle();
assert.match($('apiModelList').textContent,/Connected/);
sent.splice(1,1);
type('apiModelName', 'New local label');
$('apiModelSaveButton').onclick();
assert.equal('apiKey' in sent[1].body, false);
assert.equal(sent[1].body.id, connection.id);
const renamed = {...connection,label:'New local label'};
sent[1].reply(200, {connection:renamed,...listing([renamed])});
await settle();
click(rowButton('edit'));
type('apiModelBaseUrl', 'https://other.example/v1');
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 2);
assert.equal($('apiModelTestResult').textContent, 'Enter the API key for other.example, or use the saved URL.');
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

    def test_roles_moved_to_the_default_model_are_named_in_one_correct_sentence(self):
        # syncModelMenus answers the roles whose chosen model the new menus no longer list.
        setup = self.MANAGER_SETUP.replace("const syncModelMenus = () => [],",
                                           "let replacedRoles = [];\nconst syncModelMenus = () => replacedRoles,")
        self.assertIn("replacedRoles", setup)
        self.run_app(self.MANAGER, setup, r"""
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([connection]));
await settle();
// Load models again: the connection lists one model fewer, the one two roles had chosen.
replacedRoles = ['review', 'author'];
click(rowButton('refresh'));
sent[1].reply(200, {ok:true,message:'Found 1 model.',...listing([{...connection,models:[connection.models[0]]}])});
await settle();
assert.equal($('apiModelsStatus').textContent, 'Lab key: Found 1 model. The statement reviewer and the proof author '
  + 'now use the default model because their selected models are no longer available.');
replacedRoles = ['critic'];
click(rowButton('refresh'));
sent[2].reply(200, {ok:true,message:'Found 1 model.',...listing([{...connection,models:[connection.models[0]]}])});
await settle();
assert.equal($('apiModelsStatus').textContent, 'Lab key: Found 1 model. The independent critic now uses the default '
  + 'model because its selected model is no longer available.');
// Remove the connection: the author had chosen one of its models.
replacedRoles = ['author'];
click(rowButton('remove'));
assert.equal(sent[3].path, '/api-models/delete');
sent[3].reply(200, {deleted:connection.id,...listing([])});
await settle();
assert.equal($('apiModelsStatus').textContent, 'Removed Lab key. The proof author now uses the default model.');
""")

    def test_editing_keeps_the_saved_key_on_its_server_and_asks_only_for_another_server(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([connection]));
await settle();
click(rowButton('edit'));
// Another path, a spelling of the same host and port, another format and a new name stay on the
// saved key's server: nothing asks for the key, and none goes with the edit.
type('apiModelBaseUrl', 'https://LAB.example:443/v2/');
$('apiModelFormatOpenai').checked = false; $('apiModelFormatAnthropic').checked = true;
$('apiModelFormatAnthropic').onchange();
type('apiModelName', 'Renamed');
assert.equal($('apiModelKey').hidden, true);
assert.equal($('apiModelKey').required, false);
assert.equal($('apiModelSavedKey').textContent, 'Saved key …abcd, kept when you save');
$('apiModelSaveButton').onclick();
assert.equal(sent[1].path, '/api-models');
assert.equal(sent[1].body.baseUrl, 'https://LAB.example:443/v2/');
assert.equal(sent[1].body.format, 'anthropic');
assert.equal('apiKey' in sent[1].body, false);
sent[1].reply(400, {error:'The endpoint returned no model choices.'});
await settle();
// Another host or port: the field shows, required, with why and Use saved URL. Only what happens to the
// saved key is in the live region; the sentence naming the typed server follows it.
for (const [url, other] of [['https://other.example/v1', 'other.example'], ['https://lab.example:8443/v1', 'lab.example:8443']]) {
  type('apiModelBaseUrl', url);
  assert.equal($('apiModelSavedKey').textContent, 'The saved key …abcd is sent only to lab.example.');
  assert.equal(note(), `The saved key …abcd is sent only to lab.example. Enter the key for ${other}, or use the saved URL.`);
  assert.equal($('apiModelForm').dataset.keyState, 'other-server');
  assert.equal($('apiModelKey').hidden, false);
  assert.equal($('apiModelKey').disabled, false);
  assert.equal($('apiModelKey').required, true);
  assert.equal($('apiModelUseSavedUrl').hidden, false);
  assert.equal($('apiModelReplaceKey').hidden, true);
  assert.equal($('apiModelKeepKey').hidden, true);
}
// A URL the server refuses whatever the key asks nothing, as the server names what is wrong with it first:
// one the browser cannot read, plain http to another computer, a query, a fragment, credentials, a space,
// a backslash or a character other than printable ASCII.
for (const url of ['lab.example/v1', 'http://lab.example/v1', 'http://other.example/v1', 'https://other.example/v1?v=1',
  'https://other.example/v1#models', 'https://user@other.example/v1', 'https://user:pw@other.example/v1',
  'https://other.example/v 1', 'https://other.example\\v1', 'https://othér.example/v1', 'https://other.example/v1\u0001']) {
  type('apiModelBaseUrl', url);
  assert.equal($('apiModelForm').dataset.keyState, 'kept', url);
  assert.equal($('apiModelKey').hidden, true, url);
  assert.equal(note(), 'Saved key …abcd, kept when you save', url);
}
type('apiModelBaseUrl', 'https://other.example/v1');
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 2);
assert.equal($('apiModelTestResult').textContent, 'Enter the API key for other.example, or use the saved URL.');
assert.equal(document.activeElement, $('apiModelKey'));
// Use saved URL: back on the saved server, the saved key applies again and the typed one is dropped.
type('apiModelKey', 'typed-for-other');
$('apiModelUseSavedUrl').onclick();
assert.equal($('apiModelBaseUrl').value, connection.baseUrl);
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelKey').hidden, true);
assert.equal(document.activeElement, $('apiModelBaseUrl'));
// With the other server's key typed, that key goes with the edit.
type('apiModelBaseUrl', 'https://other.example/v1'); type('apiModelKey', 'key-for-other');
$('apiModelTestButton').onclick();
assert.equal(sent[2].path, '/api-models/test');
assert.equal(sent[2].body.apiKey, 'key-for-other');
sent[2].reply(200, {ok:false,status:401,message:'Invalid API key'});
await settle();
// Replace key: the empty field, focused, whose key replaces the saved one when saved.
$('apiModelUseSavedUrl').onclick();
$('apiModelReplaceKey').onclick();
assert.equal($('apiModelForm').dataset.keyState, 'replace');
assert.equal($('apiModelKey').hidden, false);
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelKey').required, true);
assert.equal(document.activeElement, $('apiModelKey'));
assert.equal($('apiModelSavedKey').textContent, '');
assert.equal($('apiModelKeyHelp').textContent, 'The new key replaces …abcd when you save.');
assert.equal($('apiModelKeepKey').hidden, false);
assert.equal($('apiModelReplaceKey').hidden, true);
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 3);
assert.equal($('apiModelTestResult').textContent, 'Enter the new API key, or choose Keep saved key.');
type('apiModelKey', 'replacement-key');
$('apiModelSaveButton').onclick();
assert.equal(sent[3].body.apiKey, 'replacement-key');
sent[3].reply(400, {error:'The endpoint returned no model choices.'});
await settle();
// Keep saved key hides the field again and drops the typed key.
$('apiModelKeepKey').onclick();
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelKey').hidden, true);
assert.equal($('apiModelKey').disabled, true);
assert.equal(document.activeElement, $('apiModelReplaceKey'));
$('apiModelTestButton').onclick();
assert.equal('apiKey' in sent[4].body, false);
sent[4].reply(200, {ok:true,status:200,message:'Found 2 models.'});
await settle();
// Cancel resets Replace key: a new connection needs its key, and editing again keeps the saved one.
$('apiModelReplaceKey').onclick();
$('apiModelCancelButton').onclick();
assert.equal($('apiModelKey').hidden, false);
assert.equal($('apiModelKey').required, true);
assert.equal($('apiModelSavedKey').textContent, '');
assert.equal($('apiModelKeyHelp').textContent, 'Stored only on this computer and never shown again.');
click(rowButton('edit'));
assert.equal($('apiModelKey').hidden, true);
assert.equal($('apiModelKeepKey').hidden, true);
assert.equal($('apiModelSavedKey').textContent, 'Saved key …abcd, kept when you save');
""")

    def test_a_short_key_shows_no_characters_and_a_keyless_connection_offers_the_field(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
const short = {...connection, id:'api:short', label:'Short key', keyHint:'…'};
const keyless = {...connection, id:'api:none', label:'No key', hasKey:false, keyHint:''};
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([short, keyless]));
await settle();
const edit = (id) => click($('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === 'edit'
  && b.dataset.apiModel === id));
// A key under 12 characters: the page has no characters of it to show.
edit('api:short');
assert.equal($('apiModelSavedKey').textContent, 'Saved key, kept when you save');
type('apiModelBaseUrl', 'https://other.example/v1');
assert.equal(note(), 'The saved key is sent only to lab.example. Enter the key for other.example, or use the saved URL.');
$('apiModelUseSavedUrl').onclick();
$('apiModelReplaceKey').onclick();
assert.equal($('apiModelKeyHelp').textContent, 'The new key replaces the saved key when you save.');
// No saved key: nothing to keep, so the field shows (optional, as the server keeps it keyless).
edit('api:none');
assert.equal($('apiModelForm').dataset.keyState, '');
assert.equal($('apiModelKey').hidden, false);
assert.equal($('apiModelKey').required, false);
assert.equal($('apiModelSavedKey').textContent, '');
assert.equal($('apiModelReplaceKey').hidden, true);
assert.equal($('apiModelKeyHelp').textContent, 'No key is saved for this connection; enter one to use it.');
type('apiModelBaseUrl', 'https://other.example/v1');
assert.equal($('apiModelUseSavedUrl').hidden, true);
$('apiModelSaveButton').onclick();
assert.equal('apiKey' in sent[1].body, false);
sent[1].reply(400, {error:'x'});
await settle();
type('apiModelKey', 'a-first-key-1234');
$('apiModelSaveButton').onclick();
assert.equal(sent[2].body.apiKey, 'a-first-key-1234');
""")

    def test_repair_keeps_a_key_the_entry_still_holds_on_its_server(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
const broken = {id:'api:broken', ref:'invalid-0-abc', invalid:true, problem:'The label must be text.',
  label:'Broken', model:'gpt-old', format:'openai', host:'broken.example', hasKey:true, keyHint:'…1234',
  origin:'https://broken.example'};
const lost = {id:'api:lost', ref:'invalid-1-def', invalid:true, problem:'Remove the query string from the base URL.',
  label:'Lost', model:'gpt-old', format:'openai', host:'lost.example', hasKey:true, keyHint:'', origin:''};
const ported = {id:'api:ported', ref:'invalid-2-ghi', invalid:true, problem:'The label must be text.',
  label:'Ported', model:'gpt-old', format:'openai', host:'old.lab.example', hasKey:true, keyHint:'…77d2',
  origin:'https://old.lab.example:8443'};
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, {...listing([]), invalid:[broken, lost, ported]});
await settle();
const repair = (ref) => click($('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === 'repair'
  && b.dataset.apiModel === ref));
repair(broken.ref);
assert.equal($('apiModelFormHeading').textContent, 'Repair Broken');
assert.equal($('apiModelSavedKey').textContent, 'Saved key …1234, kept when you save');
assert.equal($('apiModelKey').hidden, true);
assert.equal($('apiModelKey').required, false);
assert.equal($('apiModelTestResult').textContent, 'Enter the base URL (it was saved for broken.example), then save.');
type('apiModelBaseUrl', 'https://broken.example/v2');
assert.equal($('apiModelKey').hidden, true);
$('apiModelTestButton').onclick();
assert.deepEqual(Object.keys(sent[1].body).sort(), ['baseUrl', 'format', 'id', 'label', 'requestSource']);
assert.equal(sent[1].body.id, 'api:broken');
sent[1].reply(200, {ok:true,status:200,message:'Found 1 model.'});
await settle();
// The server sends the entry's origin (never its path): another host or port asks for its key (there is
// no saved URL to use), as the server would.
for (const [url, other] of [['https://other.example/v1', 'other.example'],
  ['https://broken.example:8443/v1', 'broken.example:8443']]) {
  type('apiModelBaseUrl', url);
  assert.equal(note(), `The saved key …1234 is sent only to broken.example. Enter the key for ${other}, `
    + 'or use a base URL on broken.example.');
  assert.equal($('apiModelKey').hidden, false);
  assert.equal($('apiModelKey').required, true);
  assert.equal($('apiModelUseSavedUrl').hidden, true);
}
type('apiModelBaseUrl', 'https://other.example/v1');
$('apiModelSaveButton').onclick();
assert.equal($('apiModelTestResult').textContent, 'Enter the API key for other.example.');
type('apiModelBaseUrl', 'https://broken.example/v1');
$('apiModelSaveButton').onclick();
assert.equal(sent[2].path, '/api-models');
assert.equal('apiKey' in sent[2].body, false);
sent[2].reply(400, {error:'x'});
await settle();
// An entry whose key cannot be kept (its URL cannot be read) asks for the key, as before.
repair(lost.ref);
assert.equal($('apiModelSavedKey').textContent, '');
assert.equal($('apiModelKey').hidden, false);
assert.equal($('apiModelKey').required, true);
assert.equal($('apiModelKeyHelp').textContent, 'Enter the API key again to repair this entry.');
assert.equal($('apiModelTestResult').textContent, 'Enter the base URL (it was saved for lost.example) and the API key, then save.');
type('apiModelBaseUrl', 'https://lost.example/v1');
$('apiModelSaveButton').onclick();
assert.equal($('apiModelTestResult').textContent, 'Enter the API key to repair this entry.');
assert.equal(sent.length, 3);
// An entry saved for another port: the form names that port, and only that server keeps the key.
repair(ported.ref);
assert.equal($('apiModelTestResult').textContent, 'Enter the base URL (it was saved for old.lab.example:8443), then save.');
type('apiModelBaseUrl', 'https://old.lab.example/v1');
assert.equal(note(), 'The saved key …77d2 is sent only to old.lab.example:8443. Enter the key for old.lab.example, '
  + 'or use a base URL on old.lab.example:8443.');
type('apiModelBaseUrl', 'https://OLD.lab.example:8443/v2');
assert.equal(note(), 'Saved key …77d2, kept when you save');
assert.equal($('apiModelKey').hidden, true);
$('apiModelSaveButton').onclick();
assert.equal('apiKey' in sent[3].body, false);
""")

    def test_a_refusal_that_asks_for_the_key_shows_the_key_field_required_and_focused(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([connection]));
await settle();
click(rowButton('edit'));
// Another tab removed the connection: the server keeps no key for it and asks for one (keyRequired).
$('apiModelSaveButton').onclick();
assert.equal('apiKey' in sent[1].body, false);
sent[1].reply(400, {error:'Enter the API key.', keyRequired:true});
await settle();
assert.equal($('apiModelTestResult').textContent, 'Not savedEnter the API key.');
// The key field shows, required and focused, as after Replace key.
assert.equal($('apiModelForm').dataset.keyState, 'replace');
assert.equal($('apiModelKey').hidden, false);
assert.equal($('apiModelKey').disabled, false);
assert.equal($('apiModelKey').required, true);
assert.equal(document.activeElement, $('apiModelKey'));
assert.equal(note(), '');
assert.equal($('apiModelKeyHelp').textContent, 'The new key replaces …abcd when you save.');
type('apiModelKey', 'typed-key-0001');
$('apiModelSaveButton').onclick();
assert.equal(sent[2].body.apiKey, 'typed-key-0001');
sent[2].reply(400, {error:'The endpoint returned no model choices.'});
await settle();
assert.equal($('apiModelKey').hidden, false);
assert.equal($('apiModelKey').value, 'typed-key-0001');
// Test: another tab moved the connection to another server, which the saved key never leaves.
$('apiModelKeepKey').onclick();
assert.equal($('apiModelKey').hidden, true);
$('apiModelTestButton').onclick();
assert.equal('apiKey' in sent[3].body, false);
sent[3].reply(400, {error:'Enter the API key for lab.example: a saved key is sent only to the server it was saved '
  + 'for (moved.example).', keyRequired:true});
await settle();
assert.equal($('apiModelForm').dataset.keyState, 'replace');
assert.equal($('apiModelKey').required, true);
assert.equal(document.activeElement, $('apiModelKey'));
assert.match($('apiModelTestResult').textContent, /Enter the API key for lab\.example: a saved key is sent only/);
// A refusal for another reason keeps the saved key's line, and so does one for settings changed meanwhile.
$('apiModelKeepKey').onclick();
$('apiModelSaveButton').onclick();
sent[4].reply(400, {error:'The endpoint returned no model choices.'});
await settle();
assert.equal($('apiModelForm').dataset.keyState, 'kept');
assert.equal($('apiModelKey').hidden, true);
assert.equal(note(), 'Saved key …abcd, kept when you save');
$('apiModelTestButton').onclick();
type('apiModelRequestSource', 'Proof lab');
sent[5].reply(400, {error:'Enter the API key.', keyRequired:true});
await settle();
assert.equal($('apiModelTestResult').children[0].textContent, 'Connection details changed');
assert.equal($('apiModelKey').hidden, true);
// Adding a connection: the field is already shown; it gets the focus back.
$('apiModelCancelButton').onclick();
type('apiModelBaseUrl', 'https://new.example/v1'); type('apiModelKey', 'Bearer');
$('apiModelSaveButton').onclick();
$('apiModelSaveButton').focus();
sent[6].reply(400, {error:'Enter the API key.', keyRequired:true});
await settle();
assert.equal($('apiModelKey').hidden, false);
assert.equal(document.activeElement, $('apiModelKey'));
""")

    def test_a_key_typed_for_another_server_is_not_sent_once_the_saved_url_is_typed_back(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([connection]));
await settle();
click(rowButton('edit'));
type('apiModelBaseUrl', 'https://other.example/v1');
type('apiModelKey', 'key-typed-for-other');
// Back on the saved server by typing (not Use saved URL): the field hides and the saved key is kept.
type('apiModelBaseUrl', 'https://lab.example/v2');
assert.equal($('apiModelKey').hidden, true);
assert.equal($('apiModelKey').disabled, true);
assert.equal(note(), 'Saved key …abcd, kept when you save');
$('apiModelSaveButton').onclick();
assert.equal(sent[1].path, '/api-models');
assert.equal(sent[1].body.baseUrl, 'https://lab.example/v2');
assert.equal('apiKey' in sent[1].body, false, 'a key typed for other.example must not go to lab.example');
sent[1].reply(400, {error:'The endpoint returned no model choices.'});
await settle();
$('apiModelTestButton').onclick();
assert.equal('apiKey' in sent[2].body, false);
""")

    def test_editing_another_connection_starts_with_its_saved_key_kept(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
const second = {...connection, id:'api:two', label:'Two', baseUrl:'https://two.example/v1', host:'two.example',
  keyHint:'…wxyz'};
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([connection, second]));
await settle();
const edit = (id) => click($('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === 'edit'
  && b.dataset.apiModel === id));
edit('api:lab');
$('apiModelReplaceKey').onclick();
type('apiModelKey', 'replacement-for-lab');
assert.equal($('apiModelKey').hidden, false);
// Replace key belongs to the connection it was chosen for.
edit('api:two');
assert.equal($('apiModelForm').dataset.keyState, 'kept');
assert.equal($('apiModelKey').hidden, true);
assert.equal($('apiModelKey').value, '');
assert.equal($('apiModelKeepKey').hidden, true);
assert.equal(note(), 'Saved key …wxyz, kept when you save');
$('apiModelSaveButton').onclick();
assert.equal(sent[1].body.id, 'api:two');
assert.equal('apiKey' in sent[1].body, false);
""")

    def test_a_url_the_server_refuses_sends_no_key_and_a_local_scheme_change_asks_for_one(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
const local = {...connection, id:'api:local', label:'Local', baseUrl:'http://localhost:8000/v1', host:'localhost',
  keyHint:'…9876'};
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([connection, local]));
await settle();
const edit = (id) => click($('apiModelList').querySelectorAll('button').find((b) => b.dataset.apiAction === 'edit'
  && b.dataset.apiModel === id));
// Plain http to another computer can never be saved: no key is asked for, none is sent, and the
// server's reason shows.
edit('api:lab');
type('apiModelBaseUrl', 'http://gateway.lab.example/v1');
assert.equal($('apiModelKey').hidden, true);
$('apiModelSaveButton').onclick();
assert.equal(sent[1].body.baseUrl, 'http://gateway.lab.example/v1');
assert.equal('apiKey' in sent[1].body, false);
sent[1].reply(400, {error:'Use https for remote endpoints; plain http is allowed only for localhost.'});
await settle();
assert.match($('apiModelTestResult').textContent, /Use https for remote endpoints/);
assert.equal($('apiModelKey').hidden, true);
// On this computer http is allowed, so a change of scheme alone is another server.
edit('api:local');
type('apiModelBaseUrl', 'http://LOCALHOST:8000/v2');
assert.equal(note(), 'Saved key …9876, kept when you save');
for (const [url, saved, other] of [['https://localhost:8000/v1', 'http://localhost:8000', 'https://localhost:8000'],
  ['http://127.0.0.1:8000/v1', 'localhost:8000', '127.0.0.1:8000'], ['http://[::1]:8000/v1', 'localhost:8000', '[::1]:8000']]) {
  type('apiModelBaseUrl', url);
  assert.equal(note(), `The saved key …9876 is sent only to ${saved}. Enter the key for ${other}, or use the saved URL.`);
  assert.equal($('apiModelKey').required, true);
}
$('apiModelSaveButton').onclick();
assert.equal(sent.length, 2);
assert.equal($('apiModelTestResult').textContent, 'Enter the API key for [::1]:8000, or use the saved URL.');
""")

    def test_the_saved_key_line_changes_only_with_what_happens_to_the_key(self):
        self.run_app(self.MANAGER, self.MANAGER_SETUP, r"""
advanced.open = true; advanced.listeners.toggle({target:advanced});
sent[0].reply(200, listing([connection]));
await settle();
click(rowButton('edit'));
// Every change of the live region, which a screen reader announces.
const region = $('apiModelSavedKey'), announced = [];
const replace = region.replaceChildren.bind(region);
region.replaceChildren = (...items) => { replace(...items); announced.push(region.textContent); };
// Editing the path, one keystroke at a time, leaves the line as it is.
for (const character of '/extra') type('apiModelBaseUrl', $('apiModelBaseUrl').value + character);
assert.deepEqual(announced, []);
// Typing another server's URL from scratch changes it once, when the key stops being kept; the sentence
// naming the typed server, outside the live region, follows every keystroke.
type('apiModelBaseUrl', '');
const typed = [];
for (const character of 'https://gateway.other.example/v1') {
  type('apiModelBaseUrl', $('apiModelBaseUrl').value + character);
  typed.push($('apiModelOtherServer').textContent);
}
assert.deepEqual(announced, ['The saved key …abcd is sent only to lab.example.']);
assert(typed.includes('Enter the key for gat, or use the saved URL.'));
assert.equal($('apiModelOtherServer').textContent, 'Enter the key for gateway.other.example, or use the saved URL.');
// Back on the saved server it changes once more, and so do Replace key and Keep saved key.
$('apiModelUseSavedUrl').onclick();
$('apiModelReplaceKey').onclick();
$('apiModelKeepKey').onclick();
assert.deepEqual(announced, ['The saved key …abcd is sent only to lab.example.', 'Saved key …abcd, kept when you save',
  '', 'Saved key …abcd, kept when you save']);
assert.equal($('apiModelOtherServer').textContent, '');
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
data.model_sources.unshift({id:'codex',label:'Codex CLI'}, {id:'claude',label:'Claude Code'});
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
        streamed = json.loads(self.rfile.read(size) or b"{}").get("stream")
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        kind = "application/json"
        if key == "sk-good-0123456789" and streamed:
            # The Test streams like a run; the model answers through the offered tool.
            status, kind, events = 200, "text/event-stream", [
                {"type": "response.created", "response": {"model": "gpt-6.1-sol-2026-10"}},
                {"type": "response.output_item.done", "item": {
                    "type": "function_call", "name": "report_result", "arguments": '{"ok": true}'}},
                {"type": "response.completed", "response": {"model": "gpt-6.1-sol-2026-10"}}]
            data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        elif key == "sk-good-0123456789":
            status, data = 200, json.dumps({"model": "gpt-6.1-sol-2026-10", "output_text": "OK"}).encode()
        else:
            status = 401
            data = json.dumps({"error": {"message": f"Incorrect API key provided: {key}."}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
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
        self.assertEqual(result["message"], 'The model answered through the report_result tool: {"ok": true}')
        self.assertEqual(result["model"], "gpt-6.1-sol-2026-10")


if __name__ == "__main__":
    unittest.main()
