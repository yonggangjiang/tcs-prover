"""The Saved settings panel of the New problem form (the first panel of Advanced settings), its Save and Load
dialogs, and the summary of Advanced settings that names the saved settings and counts the related works while
the section is closed (ui/app.js).

The browser checks cut ui/app.js into slices by marker text and run them in Node's vm with the
small fake DOM of test_provider_ui, through the real render(). Requests go to a fake fetch that
answers as the server does: its lists come from ui/presets.py with a temporary state folder.
Nothing reaches a server, a CLI or a model.
"""

import json
import os
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers
from tests.test_provider_ui import APP, DOM, INDEX, RUNNER, menu_fixture, page
from ui import presets, server

STYLES = APP.parent / "styles.css"
CUTOFF = "2026-01-15T00:00:00Z"
KEY = "sk-tcs-presets-ui-test-0123456789"

# The form's filling (render's input block, applyComposerSettings and the model menus), the settings
# a job sends, how the page adopts the model menus of GET /api-models, and the saved-settings code itself.
SLICES = [
    ["function retrievalConfiguration(", "function renderJobs("],
    ["function selectedWorkflowStages(", "const promptLabels ="],
    ["// A job the author time limit stopped", "// A time limit as the server writes it (hours_text)"],
    ["function hoursText(", "// A duration in words as the runner writes it"],
    ["function render(next)", "async function refresh()"],
    ["function proofJobSettings()", "// An empty or malformed number field"],
    ["async function apiModelRequest(", "async function loadApiModels()"],
    ["// Saved settings: the first panel of Advanced settings on the New problem card",
     "// Reduced motion: two page calls"],
]
# Names that lower case and casefold() tell apart: the page and the server must agree on each.
NAMES = ["Straße", "STRASSE", "STRAẞE", "İstanbul", "ΟΔΟΣ ΟΔΟΣ", "ǅemal", "ﬁle", "\u212a", "\u2126",
         "MAX"]
REFRESH = ["async function refresh()", "function checkEdited("]
# The related works listed for the next problem (the summary of Advanced settings counts them).
RELATED = ["function composerRelatedWorks()", "// Read files directly"]

# The page as index.html has it, as far as these slices read it, plus a server that keeps saved
# settings the way ui/presets.py does (fixture.listing is its answer when the page opens).
SETUP = r"""
Element.prototype.contains = function (node) {
  for (let item = node; item; item = item.parentElement) if (item === this) return true;
  return false;
};
const elements = {};
const ui = new Proxy(elements, {
  get: (target, name) => (typeof name === "string" ? (target[name] ??= new Element()) : undefined),
});
const $ = (id) => ui[id];
for (const role of ["review", "author", "critic", "writer"]) {
  elements[`${role}Model`] = roleMenu(fixture.menu);
  elements[`${role}Source`] = new Select([new Option("Codex CLI", "codex"), new Option("Claude Code", "claude")]);
  elements[`${role}Effort`] = new Select(["max", "xhigh", "high", "medium", "low"].map((value) => new Option(value, value)));
}
elements.speedMode = new Select([new Option("Standard", "standard"), new Option("Fast", "fast")]);
elements.reasoningSummary = new Select(["none", "concise", "detailed"].map((value) => new Option(value, value)));
elements.reasoningSummary.value = "concise";
elements.retrievalMode = new Select([new Option("Off", "live"), new Option("On", "historical")]);
const field = (id, value = "", min = "", max = "") => Object.assign(elements[id] = new Element("input"), { value, min, max });
field("criticRounds", "2", "1", "100");
field("criticCount", "3", "1", "10");
field("authorSubagents", "3", "0", "10");
field("thinkingHours", "0", "0");
field("retrievalCutoffUtc");
field("presetSaveName");
elements.problem = new Element("textarea");
for (const id of ["workflowReview", "workflowAuthor", "workflowCritic", "workflowWriting"]) {
  elements[id] = Object.assign(new Element("input"), { checked: true });
}
elements.presetSaveDefault = Object.assign(new Element("input"), { checked: false });
elements.promptDialog = Object.assign(new Element("dialog"), { open: false });
ui.notice.hidden = true;
// As in a browser: a disabled control cannot take the focus, which then stays where it was (or on the page).
document.body = new Element("body");
Element.prototype.focus = function () { if (!this.disabled) document.activeElement = this; };
// The two modal dialogs as a browser runs them: closing one gives the focus back to what had it when it
// opened (the page itself when that cannot take it now), then "close" fires.
for (const id of ["presetSaveDialog", "presetLoadDialog"]) {
  elements[id] = Object.assign(new Element("dialog"), {
    open: false,
    showModal() { this.opener = document.activeElement; this.open = true; },
    close() {
      if (!this.open) return;
      this.open = false;
      document.activeElement = document.body;
      this.opener?.focus();
      this.listeners?.close?.();
    },
  });
}
// Escape in a dialog: "cancel", then the dialog closes unless that was prevented.
const escape = (dialog) => {
  const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  dialog.listeners?.cancel?.(event);
  if (!event.defaultPrevented) dialog.close();
  return event;
};
// The New problem card as index.html has it: the statement and the stages, then Advanced settings (closed), whose
// summary holds the chips and whose first panel is Saved settings (its note at its foot), then the controls
// whose edits the panel follows.
const card = Object.assign(new Element("div"), { className: "composer" });
elements.input = new Element("section");
elements.input.append(card);
elements.advancedSettings = Object.assign(new Element("details"), { className: "advanced", open: false });
const summary = new Element("summary");
summary.append(ui.advancedSummary);
ui.presetBar.append(ui.presetName, ui.presetMarks, ui.savePresetButton, ui.loadPresetButton, ui.presetNote);
ui.advancedSettings.append(summary, ui.presetBar, ui.criticCount, ui.thinkingHours, ui.retrievalMode,
  ui.retrievalCutoffUtc);
card.append(ui.problem, ui.workflowReview, ui.workflowAuthor, ui.workflowCritic, ui.workflowWriting,
  ui.advancedSettings);
// The summary of Advanced settings: each chip's words and tone.
const chips = () => ui.advancedSummary.children.filter((node) => node.className === "advanced-chip")
  .map((chip) => [chip.textContent, chip.dataset.tone]);

let state = { trace: [], traceVersion: 0, workflow: { nodes: {}, edges: [] } };
let previousPhase = "", currentJob = "", currentBatch = "", composingProblem = true, composerSeed = null;
let reviewPending = false, timer, clock, promptOverrides = {}, promptValues = {};
const sessionToken = "page-token";
// What adoptApiModels keeps besides the menus (the API connections list itself is not drawn here).
let apiModels = [], apiInvalid = [], apiStoreProblem = "", apiStateProblem = "";
let apiModelsUnavailable = false, apiModelBusy = false;
const roleNames = { review: "statement reviewer", author: "proof author", critic: "independent critic",
  writer: "LaTeX writer" };
const renderApiModels = () => {};
const show = (element, visible) => { element.hidden = !visible; };
const retainTrace = (entries) => entries;
const ingest = () => {}, renderWorkflow = () => {}, renderClock = () => {}, renderBatchNavigation = () => {};
const appendFormattedText = () => {}, syncPrompts = () => {}, checkEdited = () => {};
const setTimeout = () => 1, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
const confirms = [];
let confirmAnswer = true;
const confirm = (text) => { confirms.push(text); return confirmAnswer; };

const copy = (value) => JSON.parse(JSON.stringify(value));
// refuse: the error a save gets (400), as when the file cannot be written.
const server = { listing: copy(fixture.listing), apiModels: copy(fixture.apiModels), status: 200, delays: 0,
  refuse: "" };
const requests = [];
const tick = () => new Promise((resolve) => setImmediate(resolve));
const fetch = async (path, options = {}) => {
  const body = options.body === undefined ? undefined : JSON.parse(options.body);
  requests.push({ path, method: options.method || "GET", body, token: options.headers?.["X-TCS-Prover-Token"] });
  for (let index = 0; index < server.delays; index += 1) await tick();
  const answer = (status, data) => ({ status, ok: status < 400, json: async () => copy(data) });
  if (server.status === 404) return answer(404, { error: "Not found." });
  // A server that fails (a full disk, say): every request is refused with its reason.
  if (server.status >= 500) return answer(server.status, { error: "The server could not read its files." });
  if (path === "/api-models" && !body) return answer(200, server.apiModels);
  if (path === "/presets" && body && server.refuse) return answer(400, { error: server.refuse });
  const listing = server.listing;
  const flags = () => { for (const item of listing.presets) if (!item.unusable) item.isDefault = item.id === listing.defaultId; };
  const find = (id) => listing.presets.find((item) => item.id === id);
  if (path === "/presets" && !body) return answer(200, listing);
  if (path === "/presets") {
    let preset = body.replaceId ? find(body.replaceId) : null;
    if (!preset) {
      preset = { id: `p-${String(listing.presets.length).padStart(12, "0")}`, unavailable: [], labels: {} };
      listing.presets.push(preset);
    }
    Object.assign(preset, { name: body.name, settings: body.settings });
    if (body.makeDefault) listing.defaultId = preset.id;
    flags();
    return answer(200, { savedId: preset.id, ...listing });
  }
  if (body.id && !find(body.id)) {
    return answer(400, { error: "Those saved settings no longer exist. Open Load saved settings… again to see the "
      + "current list." });
  }
  if (path === "/presets/rename") find(body.id).name = body.name;
  else if (path === "/presets/default") listing.defaultId = body.id;
  else if (path === "/presets/delete") {
    listing.presets = listing.presets.filter((item) => item.id !== body.id);
    if (listing.defaultId === body.id) listing.defaultId = "";
  } else return answer(404, { error: "Not found." });
  flags();
  return answer(200, listing);
};
const posts = () => requests.filter((item) => item.method === "POST");
const id = (name) => fixture.listing.presets.find((item) => item.name === name).id;
const fresh = () => ({ ...copy(fixture.empty), phase: "input" });
// A fresh New problem form, as refresh() opens one: the list first, then the form's state.
async function openFresh() {
  await loadPresets();
  previousPhase = "";
  render(fresh());
}
const edit = (control, value) => {
  if (typeof value === "boolean") control.checked = value; else control.value = value;
  card.listeners.input({ target: control });
};
const rows = () => ui.presetList.children.filter((item) => item.classList?.contains("preset-row"));
const rowNames = () => rows().map((row) => row.querySelector(".preset-row-name")?.textContent);
const actions = (name) => {
  const row = rows().find((item) => item.querySelector(".preset-row-name")?.textContent === name);
  return row.children.slice(1).map((cell) => cell.tagName === "BUTTON" ? cell.textContent : "");
};
const button = (name, action) => ui.presetList.querySelectorAll("button[data-preset-action]")
  .find((item) => item.getAttribute("aria-label") === `${action} ${name}`);
const click = async (target) => { ui.presetList.onclick({ target }); for (let i = 0; i < 4; i += 1) await tick(); };
const note = () => (ui.presetNote.hidden ? "" : ui.presetNote.textContent);
// The bar's buttons and the Save form, as the page wires them.
const clickSave = () => ui.savePresetButton.onclick();
const clickLoad = () => ui.loadPresetButton.onclick();
const submitSave = async () => {
  const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  await ui.presetSaveForm.listeners.submit(event);
  return event;
};
const enter = (target, extra = {}) => ui.presetList.onkeydown({ target, key: "Enter", preventDefault() {}, ...extra });
// An API listing without any API connection (all of them removed).
const withoutApi = (listing) => ({ ...copy(listing), connections: [], models: [],
  model_catalog: listing.model_catalog.filter((row) => row.provider !== "api"),
  model_sources: listing.model_sources.filter((row) => row.provider !== "api") });
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class PresetBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Real answers of ui/presets.py: saved settings with an API connection, one of whose models
        # was removed since ("Gateway critic"), and the page state of a fresh form without it.
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        with mock.patch.dict(os.environ, {providers.STORE_ENV: str(Path(folder.name) / "api-models.json")}):
            lab = providers.save_api_connection(
                {"label": "Lab key", "format": "openai", "baseUrl": "https://api.lab.example/v1", "apiKey": KEY},
                [{"id": "model-a", "label": "Model A"}])
            gateway = providers.save_api_connection(
                {"label": "Old gateway", "format": "anthropic", "baseUrl": "https://gateway.example", "apiKey": KEY},
                [{"id": "model-c", "label": "Model C"}])
            base = presets.builtin_settings()
            presets.save("Careful proof", {**base, "criticModel": "claude-opus-5-5", "criticEffort": "high",
                                           "criticCount": 5, "criticRounds": 3, "thinkingHours": 24,
                                           "authorSubagents": 2}, make_default=True)
            presets.save("Quick check", {**base, "authorModel": "gpt-6-luna", "authorEffort": "medium",
                                         "workflowStages": {"review": True, "author": True, "critic": False,
                                                            "writing": False}})
            presets.save("Historical cutoff", {**base, "retrievalMode": "historical", "retrievalCutoffUtc": CUTOFF,
                                               "workflowStages": {"review": False, "author": True, "critic": True,
                                                                  "writing": True}})
            presets.save("Lab key author", {**base, "authorModel": lab["models"][0]["modelId"],
                                            "authorEffort": "high", "speedMode": "fast",
                                            "reasoningSummary": "detailed"})
            presets.save("Gateway critic", {**base, "criticModel": gateway["models"][0]["modelId"],
                                            "criticEffort": "high"})
            providers.delete_api_model(gateway["id"])
            listing = presets.listing()
            empty = server.empty_state()
            # GET /api-models as the server answers it now: the connections (never a key) and the menus.
            api_models = server.Server.api_model_listing(types.SimpleNamespace())
        cls.fixture = {"menu": menu_fixture(), "listing": listing, "empty": empty, "settingKeys": list(presets.SETTINGS),
                       "labModel": lab["models"][0]["modelId"], "cutoff": CUTOFF, "apiModels": api_models,
                       "names": NAMES, "nameKeys": [presets.name_key(name) for name in NAMES]}
        assert KEY not in json.dumps(cls.fixture)

    def run_app(self, checks, *, extra_slices=(), extra_setup=""):
        setup = f"const fixture = {json.dumps(self.fixture)};\n" + SETUP + extra_setup
        payload = json.dumps({"dom": DOM, "slices": [*SLICES, *extra_slices], "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(APP)], input=payload,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_form(self, checks):
        # Without refresh() itself, render()'s polls would call it.
        self.run_app(checks, extra_setup="const refresh = () => {};\n")

    def test_a_fresh_form_opens_with_the_default_and_save_sends_only_its_settings(self):
        self.run_form(r"""
assert.equal(ui.presetName.textContent, "built-in settings");
await openFresh();
assert.deepEqual(requests.map((item) => [item.method, item.path, item.token]), [["GET", "/presets", "page-token"]]);
assert.equal(ui.presetName.textContent, "Careful proof");
assert.equal(ui.presetMarks.textContent, " (default)");
assert.equal(note(), "");
assert.deepEqual([ui.criticModel.value, ui.criticEffort.value, ui.criticSource.value], ["claude-opus-5-5", "high", "claude"]);
assert.deepEqual([ui.criticCount.value, ui.criticRounds.value, ui.thinkingHours.value, ui.authorSubagents.value].map(String),
  ["5", "3", "24", "2"]);
assert.match(ui.modelSummary.textContent.replace(/\u00a0/g, " "), /Opus 5\.5\/High critic/);

// Save… opens the Save dialog: Set as default is ticked whenever it opens; the placeholder is the name it gets.
ui.presetSaveDefault.checked = false;
promptOverrides = { author: "Edited author prompt [STATEMENT]" };
clickSave();
assert.deepEqual([ui.presetSaveDialog.open, ui.presetLoadDialog.open], [true, false]);
assert.equal(document.activeElement, ui.presetSaveName);
assert.equal(ui.presetSaveDefault.checked, true);
assert.equal(ui.presetSaveName.value, "");
assert.match(ui.presetSaveName.placeholder, /^GPT 6\.1 Sol\/Max author · \d{1,2} [A-Z][a-z]{2} \d\d:\d\d$/);
assert.equal(ui.presetSaveSummary.textContent.replace(/[\u00a0\u2060]/g, (c) => (c === "\u00a0" ? " " : "")),
  "All stages · GPT 6.1 Sol/Max author · Claude Opus 5.5/High critics × 5, 3 rounds · Time limit 24 hours · Cutoff off");
await tick();
ui.presetSaveName.value = "  Mine  ";
// Submitting the form saves; the browser's own closing of a method="dialog" form is prevented.
const submitted = await submitSave();
assert.equal(submitted.defaultPrevented, true);
const [sent] = posts();
assert.equal(sent.path, "/presets");
assert.deepEqual(Object.keys(sent.body).sort(), ["makeDefault", "name", "settings"]);
assert.deepEqual(Object.keys(sent.body.settings).sort(), [...fixture.settingKeys].sort());
assert.deepEqual([sent.body.name, sent.body.makeDefault], ["Mine", true]);
assert.deepEqual([sent.body.settings.criticModel, sent.body.settings.criticCount, sent.body.settings.thinkingHours],
  ["claude-opus-5-5", 5, 24]);
assert.equal(JSON.stringify(sent.body).includes("Edited"), false, "prompts are never saved");
assert.equal(ui.presetSaveDialog.open, false);
assert.equal(ui.presetName.textContent, "Mine");
assert.equal(ui.presetMarks.textContent, " (default)");
assert.equal(note(), "Saved as “Mine”. New problems open with these settings.");
assert.equal(ui.presetNote.dataset.tone, "info");

// Unticked, a save leaves the default alone; the name it proposes then avoids a name in use.
clickSave();
ui.presetSaveDefault.checked = false;
await submitSave();
const second = posts().at(-1);
assert.equal(second.body.makeDefault, false);
assert.match(second.body.name, /^GPT 6\.1 Sol\/Max author · /);
assert.equal(ui.presetMarks.textContent, "");
""")

    def test_changed_follows_every_covered_setting_and_clears_on_save_and_load(self):
        self.run_form(r"""
await openFresh();
edit(ui.criticCount, "4");
assert.equal(ui.presetMarks.textContent, " (default) · changed on this form");
assert.equal(ui.presetBar.dataset.changed, "true");
edit(ui.criticCount, "5");
assert.equal(ui.presetMarks.textContent, " (default)", "back to the saved values");
// The statement and other fields are not settings.
edit(ui.problem, "Prove that every planar graph is 4-colourable.");
assert.equal(ui.presetMarks.textContent, " (default)");
for (const [control, value] of [[ui.workflowWriting, false], [ui.thinkingHours, "2"], [ui.retrievalMode, "historical"],
  [ui.authorSubagents, "3"], [ui.speedMode, "fast"], [ui.reasoningSummary, "none"], [ui.writerEffort, "low"],
  [ui.authorModel, "gpt-6-luna"], [ui.criticRounds, "4"], [ui.reviewEffort, "low"], [ui.authorEffort, "low"],
  [ui.criticEffort, "medium"]]) {
  await openFresh();
  const shown = () => (typeof value === "boolean" ? control.checked : String(control.value));
  assert.notEqual(shown(), value, "a value the default does not have");
  edit(control, value);
  assert.equal(shown(), value, "a value the control offers");
  assert.equal(ui.presetMarks.textContent, " (default) · changed on this form", String(value));
}
// A confirmation gives way to the next edit; a save and a load clear the mark.
clickSave();
await submitSave();
assert.equal(ui.presetMarks.textContent, " (default)", "saved, and the default (ticked)");
assert.match(note(), /^Saved as /);
edit(ui.criticCount, "6");
assert.equal(ui.presetMarks.textContent, " (default) · changed on this form");
assert.equal(note(), "");
await clickLoad();
await click(button("Careful proof", "Load"));
// The save above made its own settings the default.
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent], ["Careful proof", ""]);
assert.equal(String(ui.criticCount.value), "5");
// The cutoff time itself is a setting, not only whether there is one.
await clickLoad();
await click(button("Historical cutoff", "Load"));
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent], ["Historical cutoff", ""]);
edit(ui.retrievalCutoffUtc, "2026-02-01T00:00:00Z");
assert.equal(ui.presetMarks.textContent, " · changed on this form");
edit(ui.retrievalCutoffUtc, fixture.cutoff);
assert.equal(ui.presetMarks.textContent, "");
""")

    def test_the_generated_name_and_a_name_in_use(self):
        self.run_form(r"""
await openFresh();
const october = new Date(2026, 9, 31, 17, 48);
assert.equal(presetGeneratedName(october), "GPT 6.1 Sol/Max author · 31 Oct 17:48");
presetStore().presets.push({ id: "p-taken", name: "GPT 6.1 Sol/Max author · 31 Oct 17:48" },
  { id: "p-taken-2", name: "gpt 6.1 sol/max AUTHOR · 31 Oct 17:48 (2)" });
assert.equal(presetGeneratedName(october), "GPT 6.1 Sol/Max author · 31 Oct 17:48 (3)");
// Without the author, the first selected role names it.
edit(ui.workflowReview, false);
edit(ui.workflowAuthor, false);
assert.equal(presetGeneratedName(new Date(2026, 0, 5, 9, 3)), "Claude Opus 5.5/High critic · 5 Jan 09:03");
presetStore().presets.splice(-2);

// A typed name in use, ignoring case, asks before replacing those settings.
clickSave();
ui.presetSaveName.value = "careful PROOF";
confirmAnswer = false;
await submitSave();
assert.deepEqual(confirms, ["Replace the saved settings “Careful proof”?"]);
assert.equal(posts().length, 0);
assert.equal(ui.presetSaveDialog.open, true);
confirmAnswer = true;
await submitSave();
assert.deepEqual([posts()[0].body.replaceId, posts()[0].body.name], [id("Careful proof"), "careful PROOF"]);
""")

    def test_load_sets_every_setting_and_keeps_the_statement_prompts_and_a_cutoff(self):
        self.run_form(r"""
await openFresh();
ui.problem.value = "My own statement.";
promptOverrides = { author: "Edited author prompt [STATEMENT]" };
await clickLoad();
assert.equal(ui.presetLoadDialog.open, true);
await click(button("Historical cutoff", "Load"));
assert.equal(ui.presetLoadDialog.open, false);
// The cutoff comes from the saved settings, although the form's state says unrestricted search.
assert.equal(state.retrieval.mode, "live");
assert.deepEqual([ui.retrievalMode.value, ui.retrievalCutoffUtc.value], ["historical", fixture.cutoff]);
assert.deepEqual(selectedRetrievalSettings(), { retrievalMode: "historical", retrievalCutoffUtc: fixture.cutoff });
assert.deepEqual(selectedWorkflowStages(), { review: false, author: true, critic: true, writing: true });
assert.deepEqual([ui.criticModel.value, String(ui.criticCount.value), String(ui.thinkingHours.value)], ["gpt-6.1-sol", "3", "0"]);
assert.equal(ui.problem.value, "My own statement.");
assert.deepEqual(promptOverrides, { author: "Edited author prompt [STATEMENT]" });
assert.equal(ui.presetName.textContent, "Historical cutoff");
assert.equal(ui.presetMarks.textContent, "");
assert.equal(note(), "Loaded “Historical cutoff”.");

// Every other setting comes from the saved settings too: an API model, speed, log detail.
await clickLoad();
await click(button("Lab key author", "Load"));
assert.deepEqual([ui.authorModel.value, ui.authorSource.value, ui.authorEffort.value, ui.speedMode.value,
  ui.reasoningSummary.value], [fixture.labModel, fixture.labModel.split("--m-")[0], "high", "fast", "detailed"]);
assert.deepEqual(selectedRetrievalSettings(), { retrievalMode: "live", retrievalCutoffUtc: "" });
await clickLoad();
await click(button("Quick check", "Load"));
assert.deepEqual(selectedWorkflowStages(), { review: true, author: true, critic: false, writing: false });
assert.deepEqual([ui.authorModel.value, ui.authorEffort.value], ["gpt-6-luna", "medium"]);
assert.equal(ui.problem.value, "My own statement.");
assert.equal(posts().length, 0, "loading saves nothing");
""")

    def test_a_removed_model_loads_as_gpt_6_1_sol_with_a_note_and_nothing_is_saved_again(self):
        self.run_form(r"""
await openFresh();
await clickLoad();
const warning = rows().find((row) => row.dataset.preset === id("Gateway critic")).querySelector(".preset-row-warning");
assert.equal(warning.textContent, "Critic model not available: Old gateway · Model C; it loads GPT 6.1 Sol instead.");
await click(button("Gateway critic", "Load"));
assert.deepEqual([ui.criticModel.value, ui.criticSource.value, ui.criticEffort.value], ["gpt-6.1-sol", "codex", "high"]);
assert.equal(note(), "Critic model not available (Old gateway · Model C), so the critic uses GPT 6.1 Sol, which runs "
  + "through the Codex CLI. The saved settings are unchanged.");
assert.equal(ui.presetNote.dataset.tone, "warning");
assert.equal(ui.presetMarks.textContent, "");
// The note stays through edits; the next load replaces it.
edit(ui.criticCount, "4");
assert.match(note(), /^Critic model not available/);
assert.equal(posts().length, 0, "the saved settings are never saved again");

// As the default, a fresh form opens with the same replacement and note.
server.listing.defaultId = id("Gateway critic");
await openFresh();
assert.deepEqual([ui.presetName.textContent, ui.criticModel.value], ["Gateway critic", "gpt-6.1-sol"]);
assert.match(note(), /^Critic model not available \(Old gateway · Model C\)/);
assert.equal(posts().length, 0);
""")

    def test_the_load_list_its_order_rename_default_delete_and_built_in_settings(self):
        self.run_form(r"""
server.listing.presets.push({ id: "invalid-5-0123456789ab", name: "Broken", unusable: true,
  problem: "These settings cannot be used: Choose 1 to 10 independent critics." });
await openFresh();
await clickLoad();
// The default first, then by name, the ones that cannot be used, and Built-in settings last.
assert.deepEqual(rowNames(), ["Careful proof", "Gateway critic", "Historical cutoff", "Lab key author", "Quick check",
  "Broken", "Built-in settings"]);
assert.deepEqual(actions("Careful proof"), ["Load", "Rename", "Remove default", "Delete"]);
assert.deepEqual(actions("Quick check"), ["Load", "Rename", "Make default", "Delete"]);
assert.deepEqual(actions("Broken"), ["", "", "", "Delete"]);
assert.deepEqual(actions("Built-in settings"), ["Load", "", "Make default", ""]);
const careful = rows()[0];
assert.equal(careful.querySelector(".preset-default-badge").textContent, "Default");
assert.match(rows()[5].querySelector(".preset-row-warning").textContent, /can only be deleted/);
assert.equal(ui.presetLoadStatus.textContent, "");

// Rename in place: a name in use is refused before any request; Enter saves.
await click(button("Quick check", "Rename"));
const field = ui.presetList.querySelector("input[data-preset-rename]");
assert.equal(field.value, "Quick check");
assert.equal(document.activeElement, field);
field.value = "CAREFUL proof";
enter(field);
assert.match(ui.presetLoadStatus.textContent, /Saved settings called “Careful proof” already exist/);
assert.equal(posts().length, 0);
field.value = "  Quick look ";
enter(field);
for (let i = 0; i < 4; i += 1) await tick();
assert.deepEqual(posts().at(-1), { path: "/presets/rename", method: "POST", body: { id: id("Quick check"), name: "Quick look" },
  token: "page-token" });
assert.equal(ui.presetLoadStatus.textContent, "Renamed to “Quick look”.");
assert.equal(ui.presetList.querySelector("input[data-preset-rename]"), null);
assert.ok(rowNames().includes("Quick look"));
// Escape ends a renaming, not the list.
await click(button("Quick look", "Rename"));
const cancel = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
ui.presetLoadDialog.listeners.cancel(cancel);
assert.equal(cancel.defaultPrevented, true);
assert.equal(ui.presetList.querySelector("input[data-preset-rename]"), null);

// Make default, then Built-in settings as the default; the bar follows.
await click(button("Quick look", "Make default"));
assert.deepEqual(posts().at(-1).body, { id: id("Quick check") });
assert.equal(rowNames()[0], "Quick look");
assert.equal(ui.presetLoadStatus.textContent, "New problems now open with “Quick look”.");
assert.equal(ui.presetMarks.textContent, "", "the form's settings are still Careful proof");
await click(button("Built-in settings", "Make default"));
assert.deepEqual(posts().at(-1).body, { id: "" });
assert.deepEqual(actions("Built-in settings"), ["Load", "", "", ""]);
assert.ok(rows().at(-1).querySelector(".preset-default-badge"));
assert.equal(ui.presetLoadStatus.textContent, "New problems now open with the built-in settings.");
await click(button("Careful proof", "Make default"));
assert.equal(document.activeElement, button("Careful proof", "Remove default"), "the focus stays on the row");
await click(button("Careful proof", "Remove default"));
assert.deepEqual(posts().slice(-2).map((item) => item.body), [{ id: id("Careful proof") }, { id: "" }]);
assert.equal(document.activeElement, button("Careful proof", "Make default"));

// Delete asks first; deleting the settings the form came from says so in the bar.
confirmAnswer = false;
await click(button("Careful proof", "Delete"));
assert.deepEqual(confirms, ["Delete the saved settings “Careful proof”?"]);
assert.equal(posts().at(-1).path, "/presets/default");
confirmAnswer = true;
await click(button("Careful proof", "Delete"));
assert.deepEqual(posts().at(-1), { path: "/presets/delete", method: "POST", body: { id: id("Careful proof") },
  token: "page-token" });
assert.equal(ui.presetLoadStatus.textContent, "Deleted “Careful proof”.");
assert.ok(!rowNames().includes("Careful proof"));
assert.equal(ui.presetName.textContent, "Careful proof");
assert.equal(ui.presetMarks.textContent, " (deleted)");
await click(button("Broken", "Delete"));
assert.deepEqual(posts().at(-1).body, { id: "invalid-5-0123456789ab" });

// Built-in settings load TCS Prover's own settings.
await click(button("Built-in settings", "Load"));
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent, note()], ["built-in settings", "", "Loaded “Built-in settings”."]);
assert.deepEqual([ui.criticModel.value, String(ui.criticCount.value), String(ui.criticRounds.value), String(ui.thinkingHours.value)],
  ["gpt-6.1-sol", "3", "2", "0"]);
assert.deepEqual(selectedWorkflowStages(), { review: true, author: true, critic: true, writing: true });

// A refused change says why and shows the list as it is now (deleted meanwhile in another tab).
await clickLoad();
server.listing.presets = server.listing.presets.filter((item) => item.name !== "Lab key author");
await click(button("Lab key author", "Rename"));
const renaming = ui.presetList.querySelector("input[data-preset-rename]");
renaming.value = "Elsewhere";
enter(renaming);
for (let i = 0; i < 6; i += 1) await tick();
assert.equal(ui.presetLoadStatus.textContent,
  "Those saved settings no longer exist. Open Load saved settings… again to see the current list.");
assert.ok(!rowNames().includes("Lab key author"));
assert.equal(ui.presetList.querySelector("input[data-preset-rename]"), null);
""")

    def test_the_bar_follows_a_rename_and_a_delete_of_the_settings_it_names(self):
        self.run_form(r"""
await openFresh();
await clickLoad();
await click(button("Quick check", "Load"));
assert.equal(ui.presetName.textContent, "Quick check");
await clickLoad();
await click(button("Quick check", "Rename"));
const field = ui.presetList.querySelector("input[data-preset-rename]");
field.value = "Quick look";
enter(field);
for (let i = 0; i < 4; i += 1) await tick();
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent], ["Quick look", ""]);
await click(button("Quick look", "Delete"));
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent], ["Quick look", " (deleted)"]);
// Renamed in another tab: the next read of the list gives the bar the new name.
await clickLoad();
await click(button("Careful proof", "Load"));
server.listing.presets.find((item) => item.name === "Careful proof").name = "Careful proof, renamed";
await loadPresets();
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent], ["Careful proof, renamed", " (default)"]);
""")

    def test_a_change_of_the_default_has_the_setup_page_read_it_at_once(self):
        # The Setup page counts the default saved settings, also beside this form (app.js setupNeeds). A change that
        # may change them has it read them again at once (loadSetup("poll"): the server's last check, no program
        # starts); a rename, another preset's delete or save, and a refused change do not.
        self.run_app(r"""
await openFresh();
await clickLoad();
await click(button("Quick check", "Rename"));
const field = ui.presetList.querySelector("input[data-preset-rename]");
field.value = "Quick look";
enter(field);
for (let i = 0; i < 4; i += 1) await tick();
assert.equal(ui.presetLoadStatus.textContent, "Renamed to “Quick look”.");
assert.deepEqual(setupReads, []);
// Make default, Remove default, and the built-in settings as the default.
await click(button("Quick look", "Make default"));
assert.deepEqual(setupReads, ["poll"]);
await click(button("Quick look", "Remove default"));
assert.equal(server.listing.defaultId, "");
assert.deepEqual(setupReads, ["poll", "poll"]);
await click(button("Careful proof", "Make default"));
await click(button("Built-in settings", "Make default"));
assert.equal(setupReads.length, 4);
// Deleting other saved settings changes nothing there; deleting the default does.
await click(button("Careful proof", "Make default"));
await click(button("Quick look", "Delete"));
assert.equal(ui.presetLoadStatus.textContent, "Deleted “Quick look”.");
assert.equal(setupReads.length, 5);
await click(button("Careful proof", "Delete"));
assert.equal(server.listing.defaultId, "");
assert.equal(setupReads.length, 6);
// A refused change.
server.listing.presets = server.listing.presets.filter((item) => item.name !== "Lab key author");
await click(button("Lab key author", "Make default"));
assert.match(ui.presetLoadStatus.textContent, /no longer exist/);
assert.equal(setupReads.length, 6);
ui.presetLoadDialog.close();
// Save…: with Set as default, then without it under a new name, then without it over the default.
const save = async (name, makeDefault) => {
  clickSave();
  await tick();
  ui.presetSaveName.value = name;
  ui.presetSaveDefault.checked = makeDefault;
  await submitSave();
};
await save("Mine", true);
assert.equal(server.listing.defaultId, server.listing.presets.find((item) => item.name === "Mine").id);
assert.equal(setupReads.length, 7);
await save("Other", false);
assert.equal(setupReads.length, 7);
await save("Mine", false);
assert.equal(confirms.at(-1), "Replace the saved settings “Mine”?");
assert.equal(setupReads.length, 8);
server.refuse = "The settings file cannot be written.";
await save("Third", true);
assert.equal(ui.presetSaveError.textContent, "The settings file cannot be written.");
assert.equal(setupReads.length, 8);
assert.deepEqual([...new Set(setupReads)], ["poll"]);
""", extra_setup="const refresh = () => {};\nconst setupReads = [];\n"
                        "const loadSetup = (kind) => { setupReads.push(kind); };\n")

    def test_the_list_takes_no_action_until_it_is_read_again(self):
        self.run_form(r"""
await openFresh();
server.delays = 8;
const opening = clickLoad();
assert.equal(ui.presetLoadDialog.open, true);
assert.equal(ui.presetLoadStatus.textContent, "Reading saved settings…");
// The rows as last read are shown, but none of them acts until the list arrives again.
assert.ok(rows().length > 1);
assert.ok(ui.presetList.querySelectorAll("button").every((item) => item.disabled));
await click(button("Quick check", "Rename"));
assert.equal(ui.presetList.querySelector("input[data-preset-rename]"), null);
await opening;
assert.equal(ui.presetLoadStatus.textContent, "");
assert.equal(button("Quick check", "Rename").disabled, false);
await click(button("Quick check", "Rename"));
assert.equal(document.activeElement, ui.presetList.querySelector("input[data-preset-rename]"));
""")

    def test_an_empty_list_says_how_to_save(self):
        self.run_form(r"""
server.listing.presets = [];
server.listing.defaultId = "";
await openFresh();
assert.equal(ui.presetName.textContent, "built-in settings");
await clickLoad();
assert.deepEqual(rowNames(), ["Built-in settings"]);
assert.equal(ui.presetList.querySelector(".preset-empty").textContent,
  "No saved settings yet. Choose Save these settings… to keep this form's settings under a name.");
assert.deepEqual(actions("Built-in settings"), ["Load", "", "", ""]);
""")

    def test_the_default_is_not_applied_to_a_rerun_a_job_sent_back_or_a_later_render(self):
        self.run_form(r"""
await loadPresets();
// "Rerun with suggested statement": that statement with the job's settings.
composerSeed = { statement: "Suggested statement", settings: { runId: "job-0", criticModel: "gpt-6-sol",
  criticCount: 7, workflowStages: { review: false, author: true, critic: true, writing: true } } };
previousPhase = "";
render(fresh());
assert.deepEqual([ui.problem.value, ui.criticModel.value, String(ui.criticCount.value)], ["Suggested statement", "gpt-6-sol", "7"]);
assert.equal(ui.presetName.textContent, "the job's settings");
// A job sent back to the form keeps its own settings.
currentJob = "job-1";
previousPhase = "";
render({ ...fresh(), runId: "job-1", originalContent: "Saved statement", criticCount: 6, criticModel: "gpt-6-astra" });
assert.deepEqual([ui.problem.value, String(ui.criticCount.value), ui.criticModel.value], ["Saved statement", "6", "gpt-6-astra"]);
assert.equal(ui.presetName.textContent, "the job's settings");
edit(ui.criticCount, "9");
assert.equal(ui.presetMarks.textContent, " · changed on this form");
// A fresh form takes the default; a later render of the same form (a poll, or a return from Models &
// API) keeps what was edited.
currentJob = "";
previousPhase = "";
render(fresh());
assert.deepEqual([ui.presetName.textContent, String(ui.criticCount.value)], ["Careful proof", "5"]);
edit(ui.criticCount, "8");
render(fresh());
assert.equal(String(ui.criticCount.value), "8");
assert.equal(ui.presetMarks.textContent, " (default) · changed on this form");
""")

    def test_a_new_problem_page_reads_the_list_before_drawing_the_form(self):
        # refresh() itself, as a ?new=1 page load or New problem runs it: the list may answer after the state.
        # The page reads its batches first (the New problem page names the batch it adds to); saved settings
        # never name one, so the default fills the form of a page adding to a batch and that batch stays.
        self.run_app(r"""
server.delays = 6;
await refresh();
assert.deepEqual(requests.map((item) => item.path), ["/presets"]);
assert.deepEqual(pageRequests, ["/batches", "/state"]);
assert.deepEqual([ui.presetName.textContent, String(ui.criticCount.value)], ["Careful proof", "5"]);
assert.equal(ui.notice.hidden, true);
// New problem in this batch: the same default, and the page keeps adding to that batch.
currentBatch = "batch_0123456789";
previousPhase = "";
await refresh();
assert.deepEqual(requests.map((item) => item.path), ["/presets", "/presets"]);
assert.equal(pageRequests.at(-1), "/state?batch=batch_0123456789");
assert.deepEqual([ui.presetName.textContent, String(ui.criticCount.value), currentBatch],
  ["Careful proof", "5", "batch_0123456789"]);
// Loading and saving settings there leave the batch alone, and no request about them names it.
await clickLoad();
await click(button("Quick check", "Load"));
assert.deepEqual([ui.presetName.textContent, currentBatch], ["Quick check", "batch_0123456789"]);
clickSave();
await submitSave();
assert.equal(currentBatch, "batch_0123456789");
assert.equal(posts().length, 1);
assert(requests.every((item) => !item.path.includes("batch") && !JSON.stringify(item.body || {}).includes("batch")));
// A job's page reads no saved settings.
currentJob = "job-1";
currentBatch = "";
previousPhase = "";
const read = requests.length;
await refresh();
assert.equal(requests.length, read);
""", extra_slices=[REFRESH], extra_setup=r"""
let batchCatalog = [], pollNotice = "";
const pageRequests = [];
const jobPath = (path) => (currentBatch ? `${path}?batch=${currentBatch}` : path);
const selectedBatch = () => batchCatalog.find((item) => item.id === currentBatch) || null;
const adoptBatchCatalog = (catalog) => catalog.batches || [];
const loadJobs = async () => {}, loadJobRecovery = () => {};
const request = async (path) => {
  pageRequests.push(path);
  await tick();
  if (path === "/batches") {
    return { batches: [{ id: "__root__", label: "Default batch", addProblems: true },
      { id: "batch_0123456789", label: "Graph coloring", addProblems: true, named: true }], canCreateBatches: true };
  }
  return currentJob ? { ...fresh(), runId: currentJob, phase: "running" } : fresh();
};
""")

    def test_an_older_server_disables_save_and_load_and_the_form_keeps_working(self):
        self.run_form(r"""
server.status = 404;
await openFresh();
assert.deepEqual([ui.presetName.textContent, String(ui.criticCount.value), ui.criticModel.value], ["built-in settings", "3", "gpt-6.1-sol"]);
assert.equal(note(), "This TCS Prover server cannot save settings. Restart TCS Prover to update it.");
assert.equal(ui.presetNote.dataset.tone, "warning");
assert.deepEqual([ui.loadPresetButton.disabled, ui.savePresetButton.disabled], [true, true]);
clickSave();
await clickLoad();
assert.deepEqual([ui.presetSaveDialog.open, ui.presetLoadDialog.open], [false, false]);
assert.equal(requests.length, 1);
""")

    def test_a_file_that_cannot_be_used_allows_built_in_settings_only(self):
        self.run_form(r"""
server.listing = { ...server.listing, presets: [], defaultId: "", problem: "The saved settings file is damaged; fix "
  + "or remove it. Until then new problems open with the built-in settings, and settings cannot be saved." };
await openFresh();
assert.equal(ui.presetName.textContent, "built-in settings");
assert.match(note(), /is damaged; fix or remove it/);
assert.deepEqual([ui.loadPresetButton.disabled, ui.savePresetButton.disabled], [false, true]);
await clickLoad();
assert.match(ui.presetLoadStatus.textContent, /is damaged/);
assert.deepEqual(rowNames(), ["Built-in settings"]);
assert.equal(ui.presetList.querySelector(".preset-empty"), null);
assert.deepEqual(actions("Built-in settings"), ["Load", "", "", ""]);
""")

    def test_settings_the_form_cannot_save_are_named_before_the_dialog_opens(self):
        self.run_form(r"""
await openFresh();
for (const [change, message, control] of [
  [() => { ui.thinkingHours.value = ""; }, "Enter a time limit: 0 for unlimited, or a positive number of hours.", ui.thinkingHours],
  [() => { ui.criticCount.value = "11"; }, "Enter 1 to 10 independent critics.", ui.criticCount],
  [() => { ui.criticRounds.value = "2.5"; }, "Enter 1 to 100 critic rounds.", ui.criticRounds],
  [() => { ui.authorSubagents.value = "-1"; }, "Enter 0 to 10 author subagents.", ui.authorSubagents],
  // As the mode menu's change handler does, the cutoff field is enabled for a cutoff.
  [() => {
    ui.retrievalMode.value = "historical";
    renderRetrievalSettings();
    ui.retrievalCutoffUtc.value = "2026-01-15";
  },
    "Enter -1 to disable web-search tools, or a valid UTC cutoff such as 2026-10-02T14:13:12Z. Include seconds and the UTC timezone.",
    ui.retrievalCutoffUtc],
  [() => { for (const stage of ["Review", "Author", "Critic", "Writing"]) ui[`workflow${stage}`].checked = false; },
    "Select at least one workflow stage before saving settings.", ui.workflowReview],
]) {
  await openFresh();
  change();
  clickSave();
  assert.equal(ui.presetSaveDialog.open, false, message);
  assert.deepEqual([ui.notice.textContent, ui.notice.hidden, document.activeElement], [message, false, control]);
}
// -1 (no web-search tools) and an exact UTC time can be saved; whether they can run is checked at Start.
await openFresh();
ui.retrievalMode.value = "historical";
ui.retrievalCutoffUtc.value = "-1";
clickSave();
assert.equal(ui.presetSaveDialog.open, true);
await submitSave();
assert.deepEqual([posts()[0].body.settings.retrievalMode, posts()[0].body.settings.retrievalCutoffUtc], ["disabled", "-1"]);
""")


    def test_the_bar_buttons_and_the_dialog_buttons_open_and_close_their_own_dialog(self):
        # The buttons as the page wires them, and where the focus goes as each dialog closes.
        self.run_form(r"""
await openFresh();
// Save… opens the Save dialog; Cancel closes it with the focus back on Save…, whatever had it before.
document.activeElement = document.body;
clickSave();
assert.deepEqual([ui.presetSaveDialog.open, ui.presetLoadDialog.open], [true, false]);
ui.cancelPresetSaveButton.onclick();
assert.equal(ui.presetSaveDialog.open, false);
assert.equal(document.activeElement, ui.savePresetButton);
assert.equal(posts().length, 0);
// A save closes it with the focus on Save… too (the button is disabled while the save runs).
document.activeElement = document.body;
clickSave();
ui.presetSaveName.value = "Through the form";
await submitSave();
assert.deepEqual(posts().map((item) => [item.path, item.body.name]), [["/presets", "Through the form"]]);
assert.equal(ui.presetSaveDialog.open, false);
assert.equal(document.activeElement, ui.savePresetButton);
// Load… opens the Load dialog, busy (aria-busy) until the list and the model menus are read again.
document.activeElement = document.body;
const opening = clickLoad();
assert.deepEqual([ui.presetLoadDialog.open, ui.presetSaveDialog.open], [true, false]);
assert.equal(ui.presetLoadDialog.getAttribute("aria-busy"), "true");
await opening;
assert.equal(ui.presetLoadDialog.getAttribute("aria-busy"), "false");
assert.deepEqual(requests.slice(-2).map((item) => item.path).sort(), ["/api-models", "/presets"]);
// Close closes it, the focus back on Load…; so does a row's Load.
ui.closePresetLoadButton.onclick();
assert.equal(ui.presetLoadDialog.open, false);
assert.equal(document.activeElement, ui.loadPresetButton);
document.activeElement = document.body;
await clickLoad();
await click(button("Quick check", "Load"));
assert.equal(ui.presetLoadDialog.open, false);
assert.equal(document.activeElement, ui.loadPresetButton);
// After Delete the list stays open, the focus on its Close button (the row is gone); the list is busy
// while the request runs.
await clickLoad();
server.delays = 3;
ui.presetList.onclick({ target: button("Quick check", "Delete") });
assert.equal(ui.presetLoadDialog.getAttribute("aria-busy"), "true");
for (let i = 0; i < 8; i += 1) await tick();
assert.equal(ui.presetLoadDialog.getAttribute("aria-busy"), "false");
assert.equal(ui.presetLoadDialog.open, true);
assert.ok(!rowNames().includes("Quick check"));
assert.equal(document.activeElement, ui.closePresetLoadButton);
// The rename field has no visible label: its accessible name says what it is.
await click(button("Careful proof", "Rename"));
assert.equal(ui.presetList.querySelector("input[data-preset-rename]").getAttribute("aria-label"),
  "New name for Careful proof");
""")

    def test_escape_while_a_request_runs_gives_the_focus_back_when_it_ends(self):
        self.run_form(r"""
await openFresh();
server.delays = 8;
// Escape while the list is read: Load… is disabled until then, and takes the focus once it has arrived.
document.activeElement = document.body;
const opening = clickLoad();
escape(ui.presetLoadDialog);
assert.equal(ui.presetLoadDialog.open, false);
assert.equal(document.activeElement, document.body);
await opening;
assert.equal(ui.loadPresetButton.disabled, false);
assert.equal(document.activeElement, ui.loadPresetButton);
// Escape while a save runs: the settings are saved, and Save… takes the focus when it ends.
clickSave();
ui.presetSaveName.value = "Slow save";
const saving = submitSave();
escape(ui.presetSaveDialog);
assert.equal(document.activeElement, document.body);
await saving;
assert.equal(note(), "Saved as “Slow save”. New problems open with these settings.");
assert.equal(document.activeElement, ui.savePresetButton);
// Unless the focus went somewhere else meanwhile.
const reading = clickLoad();
escape(ui.presetLoadDialog);
ui.problem.focus();
await reading;
assert.equal(document.activeElement, ui.problem);
""")

    def test_a_tab_left_open_across_the_restart_saves_and_loads_from_its_next_fresh_form(self):
        self.run_form(r"""
// An older server: Load… and Save… are disabled, and the bar says to restart TCS Prover.
server.status = 404;
await openFresh();
assert.deepEqual([ui.loadPresetButton.disabled, ui.savePresetButton.disabled], [true, true]);
assert.equal(note(), "This TCS Prover server cannot save settings. Restart TCS Prover to update it.");
assert.deepEqual([ui.presetName.textContent, String(ui.criticCount.value)], ["built-in settings", "3"]);
// Restarted with the update (the tab keeps its token): the next fresh form takes the default, and both work.
server.status = 200;
await openFresh();
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent, note()], ["Careful proof", " (default)", ""]);
assert.equal(String(ui.criticCount.value), "5");
assert.deepEqual([ui.loadPresetButton.disabled, ui.savePresetButton.disabled], [false, false]);
await clickLoad();
assert.deepEqual([ui.presetLoadDialog.open, ui.presetLoadStatus.textContent], [true, ""]);
assert.ok(rowNames().includes("Quick check"));
""")

    def test_load_reads_the_model_menus_again_before_it_fills_the_form(self):
        self.run_form(r"""
// This form opened before "Lab key" was added in another tab: its menus lack Model A.
await loadPresets();
const stale = fresh();
stale.workflow.settings.model_catalog = stale.workflow.settings.model_catalog.filter((row) => row.provider !== "api");
stale.workflow.settings.model_sources = stale.workflow.settings.model_sources.filter((row) => row.provider !== "api");
previousPhase = "";
render(stale);
assert.ok(!state.workflow.settings.model_catalog.some((row) => row.id === fixture.labModel));
// Load reads the menus with the list, so the saved model, which the server lists as available, loads as saved.
await clickLoad();
assert.ok(requests.some((item) => item.path === "/api-models"));
const labRow = rows().find((row) => row.dataset.preset === id("Lab key author"));
assert.deepEqual(labRow.querySelectorAll(".preset-row-warning"), []);
await click(button("Lab key author", "Load"));
assert.deepEqual([ui.authorModel.value, ui.authorSource.value, ui.authorEffort.value],
  [fixture.labModel, fixture.labModel.split("--m-")[0], "high"]);
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent, note()],
  ["Lab key author", "", "Loaded “Lab key author”."]);
// Removed after the list was read (the menus read with it no longer have it): GPT 6.1 Sol, with the note.
server.apiModels = withoutApi(fixture.apiModels);
await clickLoad();
await click(button("Lab key author", "Load"));
assert.deepEqual([ui.authorModel.value, ui.authorSource.value], ["gpt-6.1-sol", "codex"]);
assert.equal(note(), "Author model not available (Model A), so the author uses GPT 6.1 Sol, which runs through the "
  + "Codex CLI. The saved settings are unchanged.");
assert.equal(posts().length, 0);
""")

    def test_a_removed_connection_marks_the_loaded_settings_changed(self):
        self.run_form(r"""
await openFresh();
await clickLoad();
await click(button("Lab key author", "Load"));
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent, ui.authorModel.value],
  ["Lab key author", "", fixture.labModel]);
// Removing "Lab key" under API connections: its answer brings menus without its models, and the author falls
// back to the default model; the bar says the settings are no longer those loaded.
adoptApiModels(withoutApi(fixture.apiModels));
assert.equal(ui.authorModel.value, "gpt-6.1-sol");
assert.equal(ui.presetMarks.textContent, " · changed on this form");
// Menus that leave the form as it is change nothing.
await clickLoad();
await click(button("Careful proof", "Load"));
assert.equal(ui.presetMarks.textContent, " (default)");
adoptApiModels(withoutApi(fixture.apiModels));
assert.equal(ui.presetMarks.textContent, " (default)");
""")

    def test_names_in_use_follow_the_same_rule_as_the_server(self):
        self.run_form(r"""
// The page takes a name as taken exactly when the server does (lower case on both sides).
assert.deepEqual(fixture.names.map(presetNameKey), fixture.nameKeys);
server.listing.presets.push({ ...copy(server.listing.presets.find((item) => item.name === "Quick check")),
  id: "p-5ce1e0000000", name: "Straße" });
await openFresh();
// "STRASSE" is another name on both sides: saved as new settings, without asking.
clickSave();
ui.presetSaveName.value = "STRASSE";
await submitSave();
assert.deepEqual(confirms, []);
assert.deepEqual([posts().at(-1).body.name, "replaceId" in posts().at(-1).body], ["STRASSE", false]);
// "STRAẞE" (a capital sharp s) is the same name on both sides: the page asks before replacing.
clickSave();
ui.presetSaveName.value = "STRAẞE";
await submitSave();
assert.deepEqual(confirms, ["Replace the saved settings “Straße”?"]);
assert.equal(posts().at(-1).body.replaceId, "p-5ce1e0000000");
// A rename follows the same rule.
await clickLoad();
await click(button("STRASSE", "Rename"));
const field = ui.presetList.querySelector("input[data-preset-rename]");
field.value = "straße";
enter(field);
assert.match(ui.presetLoadStatus.textContent, /Saved settings called “STRAẞE” already exist/);
field.value = "strasse";
enter(field);
for (let i = 0; i < 4; i += 1) await tick();
const strasse = server.listing.presets.find((item) => item.name === "strasse");
assert.deepEqual(posts().at(-1).body, { id: strasse.id, name: "strasse" });
""")

    def test_fields_a_stage_that_is_off_hides_never_stop_a_save(self):
        self.run_form(r"""
await openFresh();
// The critics' fields hide with Critic off, the time limit and subagents with Author off. Start checks
// them only while their stage is on, and so does Save…: it saves a valid value as it is, and the
// built-in value for one that is not.
edit(ui.criticCount, "");
edit(ui.workflowCritic, false);
clickSave();
assert.equal(ui.presetSaveDialog.open, true, "nothing to fix");
ui.presetSaveDefault.checked = false;
await submitSave();
let sent = posts().at(-1).body.settings;
assert.deepEqual([sent.workflowStages.critic, sent.criticCount, sent.criticRounds], [false, 3, 3]);
await openFresh();
edit(ui.thinkingHours, "");
edit(ui.authorSubagents, "-1");
edit(ui.workflowAuthor, false);
clickSave();
assert.equal(ui.presetSaveDialog.open, true);
await submitSave();
sent = posts().at(-1).body.settings;
// The built-in value is the server's (presets.builtin_settings(): the Author subagents default).
assert.deepEqual([sent.workflowStages.author, sent.thinkingHours, sent.authorSubagents, sent.criticCount],
  [false, 0, fixture.listing.builtIn.settings.authorSubagents, 5]);
// With the stage on, the same field is named and focused before the dialog opens.
await openFresh();
edit(ui.criticCount, "");
clickSave();
assert.equal(ui.presetSaveDialog.open, false);
assert.deepEqual([ui.notice.textContent, document.activeElement],
  ["Enter 1 to 10 independent critics.", ui.criticCount]);
""")

    def test_without_the_servers_built_in_settings_a_hidden_field_saves_the_pages_own(self):
        # A list without built-in settings (an older server): a hidden field that is not valid saves the page's own
        # built-in value, which for Author subagents is 3, an author coordinator, as the server's default is.
        self.run_form(r"""
server.listing = { ...server.listing };
delete server.listing.builtIn;
await openFresh();
edit(ui.thinkingHours, "");
edit(ui.authorSubagents, "");
edit(ui.workflowAuthor, false);
clickSave();
assert.equal(ui.presetSaveDialog.open, true);
await submitSave();
const sent = posts().at(-1).body.settings;
assert.deepEqual([sent.workflowStages.author, sent.thinkingHours, sent.authorSubagents], [false, 0, 3]);
""")

    def test_a_form_problem_goes_once_the_settings_are_saved_or_loaded(self):
        self.run_form(r"""
await openFresh();
const problem = "Enter a time limit: 0 for unlimited, or a positive number of hours.";
edit(ui.thinkingHours, "");
clickSave();
assert.deepEqual([ui.presetSaveDialog.open, ui.notice.hidden, ui.notice.textContent], [false, false, problem]);
// Fixed: the next Save… takes the problem away as its dialog opens.
edit(ui.thinkingHours, "3");
clickSave();
assert.deepEqual([ui.presetSaveDialog.open, ui.notice.hidden], [true, true]);
ui.presetSaveName.value = "After fix";
await submitSave();
assert.equal(ui.notice.hidden, true);
assert.equal(note(), "Saved as “After fix”. New problems open with these settings.");
// Loading saved settings fixes it too.
edit(ui.criticRounds, "0");
clickSave();
assert.deepEqual([ui.notice.hidden, ui.notice.textContent], [false, "Enter 1 to 100 critic rounds."]);
await clickLoad();
await click(button("Careful proof", "Load"));
assert.equal(ui.notice.hidden, true);
// Another message on that line since (a failed start, say) stays.
edit(ui.thinkingHours, "");
clickSave();
ui.notice.textContent = "The job could not start.";
await clickLoad();
await click(button("Careful proof", "Load"));
assert.deepEqual([ui.notice.hidden, ui.notice.textContent], [false, "The job could not start."]);
""")

    def test_enter_that_ends_an_input_method_composition_does_not_rename(self):
        self.run_form(r"""
await openFresh();
await clickLoad();
await click(button("Quick check", "Rename"));
const field = ui.presetList.querySelector("input[data-preset-rename]");
// Pinyin still being composed: its Enter (isComposing, or keyCode 229 in some browsers) only ends the composition.
field.value = "Quick kanyikan";
enter(field, { isComposing: true });
enter(field, { keyCode: 229 });
for (let i = 0; i < 4; i += 1) await tick();
assert.equal(posts().length, 0);
assert.equal(ui.presetList.querySelector("input[data-preset-rename]"), field);
assert.equal(ui.presetLoadStatus.textContent, "");
field.value = "Quick 看一看";
enter(field);
for (let i = 0; i < 4; i += 1) await tick();
assert.deepEqual(posts().at(-1).body, { id: id("Quick check"), name: "Quick 看一看" });
assert.equal(ui.presetLoadStatus.textContent, "Renamed to “Quick 看一看”.");
""")

    def test_a_refused_save_keeps_its_dialog_and_a_blank_rename_sends_nothing(self):
        self.run_form(r"""
await openFresh();
server.refuse = "Cannot save the settings (No space left on device).";
clickSave();
ui.presetSaveName.value = "Will fail";
await submitSave();
// The dialog stays open with the server's reason; the bar is unchanged, and the list is read again.
assert.equal(ui.presetSaveDialog.open, true);
assert.deepEqual([ui.presetSaveError.hidden, ui.presetSaveError.textContent],
  [false, "Cannot save the settings (No space left on device)."]);
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent, note()], ["Careful proof", " (default)", ""]);
assert.deepEqual([requests.at(-1).method, requests.at(-1).path], ["GET", "/presets"]);
assert.equal(ui.confirmPresetSaveButton.disabled, false, "it can be tried again");
// Tried again once the problem is gone: saved, the error gone with the dialog.
server.refuse = "";
await submitSave();
assert.deepEqual([ui.presetSaveDialog.open, ui.presetSaveError.hidden, ui.presetName.textContent],
  [false, true, "Will fail"]);
assert.equal(posts().length, 2);
// A blank name is refused before any request, the focus kept in the field.
await clickLoad();
await click(button("Quick check", "Rename"));
const field = ui.presetList.querySelector("input[data-preset-rename]");
field.value = "   ";
enter(field);
for (let i = 0; i < 4; i += 1) await tick();
assert.equal(ui.presetLoadStatus.textContent, "Enter a name.");
assert.equal(posts().length, 2);
assert.equal(document.activeElement, field);
""")

    def test_the_summary_of_advanced_settings_names_the_saved_settings_while_closed(self):
        self.run_form(r"""
// Before the list is read: the built-in settings, quietly, beside the list of what is inside.
assert.deepEqual(chips(), [["Saved settings: built-in", "quiet"]]);
const summary = ui.advancedSummary.parentElement;
assert.equal(summary.dataset.marked, "false");
// A fresh form with a default other than the built-in settings: named and marked while the section stays closed.
await openFresh();
assert.equal(ui.advancedSettings.open, false);
assert.deepEqual(chips(), [["Saved settings: Careful proof", "accent"]]);
assert.equal(summary.dataset.marked, "true");
// An edit says so; back at the saved values, it does not.
edit(ui.criticCount, "4");
assert.deepEqual(chips(), [["Saved settings: Careful proof · changed", "accent"]]);
edit(ui.criticCount, "5");
assert.deepEqual(chips(), [["Saved settings: Careful proof", "accent"]]);
// The built-in settings: quiet again, beside the list of what is inside.
await clickLoad();
await click(button("Built-in settings", "Load"));
assert.deepEqual(chips(), [["Saved settings: built-in", "quiet"]]);
assert.equal(summary.dataset.marked, "false");
edit(ui.criticCount, "4");
assert.deepEqual(chips(), [["Saved settings: built-in · changed", "quiet"]]);
// A model that is gone: a warning that says so; the panel's note names the role and the model.
await clickLoad();
await click(button("Gateway critic", "Load"));
assert.deepEqual(chips(), [["Saved settings: Gateway critic · model not available", "warning"]]);
assert.equal(ui.presetNote.dataset.tone, "warning");
server.listing.presets.find((item) => item.name === "Gateway critic").unavailable.push({ role: "author",
  label: "Old gateway · Model D", text: "Author model not available: Old gateway · Model D" });
await clickLoad();
await click(button("Gateway critic", "Load"));
assert.deepEqual(chips(), [["Saved settings: Gateway critic · 2 models not available", "warning"]]);
// Loaded, then deleted: the name stays, marked deleted.
await clickLoad();
await click(button("Quick check", "Load"));
assert.deepEqual(chips(), [["Saved settings: Quick check", "accent"]]);
await clickLoad();
await click(button("Quick check", "Delete"));
assert.deepEqual(chips(), [["Saved settings: Quick check (deleted)", "accent"]]);
// A rerun keeps the job's settings, which the summary names too.
composerSeed = { statement: "Suggested statement", settings: { runId: "job-0", criticCount: 7 } };
previousPhase = "";
render(fresh());
assert.deepEqual(chips(), [["Settings from the job", "accent"]]);
edit(ui.criticCount, "8");
assert.deepEqual(chips(), [["Settings from the job · changed", "accent"]]);
assert.equal(ui.advancedSettings.open, false, "nothing opens the section by itself");
""")

    def test_the_summary_warns_when_saved_settings_cannot_be_read_or_used(self):
        self.run_form(r"""
// The server cannot read its files: a fresh form opens with the built-in settings, and the summary says the
// saved ones are not available (the panel's note says why).
server.status = 500;
await openFresh();
assert.deepEqual([ui.presetName.textContent, String(ui.criticCount.value)], ["built-in settings", "3"]);
assert.equal(note(), "Saved settings cannot be read now: The server could not read its files.");
assert.deepEqual(chips(), [["Saved settings: not available", "warning"]]);
// Read again: the default fills the next fresh form.
server.status = 200;
await openFresh();
assert.deepEqual(chips(), [["Saved settings: Careful proof", "accent"]]);
// The list cannot be read again when Load saved settings… opens: the form keeps the default that filled it,
// and the summary's warning says why after its name, as the panel's note does.
const unreadable = "Saved settings cannot be read now: The server could not read its files.";
server.status = 500;
await clickLoad();
assert.equal(ui.presetLoadStatus.textContent, unreadable);
ui.presetLoadDialog.close();
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent, String(ui.criticCount.value), note()],
  ["Careful proof", " (default)", "5", unreadable]);
assert.deepEqual(chips(), [["Saved settings: Careful proof · saved settings not available", "warning"]]);
edit(ui.criticCount, "4");
assert.deepEqual(chips(), [["Saved settings: Careful proof · saved settings not available · changed", "warning"]]);
// The next fresh form: the list this page read before may be out of date, so it has the built-in settings,
// as when the page could never read it.
await openFresh();
assert.deepEqual([ui.presetName.textContent, ui.presetMarks.textContent, String(ui.criticCount.value), note()],
  ["built-in settings", "", "3", unreadable]);
assert.deepEqual(chips(), [["Saved settings: not available", "warning"]]);
// Read again, the default fills the next one; then Save these settings… reads the list and finds the file
// damaged: the same warning after the name.
server.status = 200;
await openFresh();
assert.deepEqual(chips(), [["Saved settings: Careful proof", "accent"]]);
const listing = server.listing;
server.listing = { ...listing, presets: [], defaultId: "", problem: "The saved settings file is damaged." };
clickSave();
for (let i = 0; i < 4; i += 1) await tick();
ui.presetSaveDialog.close();
assert.deepEqual([ui.presetName.textContent, String(ui.criticCount.value), note()],
  ["Careful proof", "5", "The saved settings file is damaged."]);
assert.deepEqual(chips(), [["Saved settings: Careful proof · saved settings not available", "warning"]]);
// A rerun keeps the job's settings, on a server without saved settings too: the summary says both.
server.listing = listing;
server.status = 404;
composerSeed = { statement: "Suggested statement", settings: { runId: "job-0", criticCount: 7 } };
await openFresh();
assert.equal(String(ui.criticCount.value), "7");
assert.deepEqual(chips(), [["Settings from the job · saved settings not available", "warning"]]);
// An older server, and a file that cannot be used, say the same.
await openFresh();
assert.deepEqual(chips(), [["Saved settings: not available", "warning"]]);
server.status = 200;
server.listing = { ...server.listing, presets: [], defaultId: "", problem: "The saved settings file is damaged." };
await openFresh();
assert.deepEqual(chips(), [["Saved settings: not available", "warning"]]);
// Built-in settings still load: the summary then names them, as the panel does.
await clickLoad();
await click(button("Built-in settings", "Load"));
assert.deepEqual(chips(), [["Saved settings: built-in", "quiet"]]);
""")

    def test_the_summary_counts_the_related_works(self):
        self.run_app(r"""
await openFresh();
const list = composerRelatedWorks();
const work = (name, status = "ready") => ({ kind: "file", name, status,
  error: status === "failed" ? `${name} is empty.` : "",
  ...(status === "ready" ? { upload: { token: `t-${name}`, kind: "file", name, sizeText: "1 KB" } } : {}) });
list.items = [work("a.pdf")];
renderComposerRelatedWorks();
assert.deepEqual(chips(), [["Saved settings: Careful proof", "accent"], ["1 related work", "accent"]]);
list.items = [work("a.pdf"), work("Plan.md")];
renderComposerRelatedWorks();
assert.deepEqual(chips(), [["Saved settings: Careful proof", "accent"], ["2 related works", "accent"]]);
// The summary names the disclosure button: its chips are read as separate phrases, after the title.
assert.deepEqual(ui.advancedSummary.children.map((node) => [node.className, node.textContent]),
  [["visually-hidden", ", "], ["advanced-chip", "Saved settings: Careful proof"], ["visually-hidden", ", "],
    ["advanced-chip", "2 related works"]]);
// One still uploading, one that failed, and the Author stage that reads them turned off.
list.items = [work("a.pdf", "uploading"), work("Plan.md")];
renderComposerRelatedWorks();
assert.deepEqual(chips()[1], ["2 related works · uploading", "accent"]);
list.items = [work("a.pdf", "failed"), work("Plan.md")];
renderComposerRelatedWorks();
assert.deepEqual(chips()[1], ["2 related works · 1 failed to upload", "warning"]);
list.items = [work("Plan.md")];
edit(ui.workflowAuthor, false);
renderComposerRelatedWorks();
assert.deepEqual(chips(), [["Saved settings: Careful proof · changed", "accent"],
  ["1 related work · Author stage off", "warning"]]);
// The Author stage being off is a warning whatever else the chip says: its words say so too.
list.items = [work("a.pdf", "uploading"), work("Plan.md")];
renderComposerRelatedWorks();
assert.deepEqual(chips()[1], ["2 related works · uploading · Author stage off", "warning"]);
list.items = [work("a.pdf", "failed"), work("Plan.md")];
renderComposerRelatedWorks();
assert.deepEqual(chips()[1], ["2 related works · 1 failed to upload · Author stage off", "warning"]);
list.items = [work("Plan.md")];
// With the built-in settings, a related work alone marks the summary.
edit(ui.workflowAuthor, true);
await clickLoad();
await click(button("Built-in settings", "Load"));
assert.deepEqual(chips(), [["Saved settings: built-in", "quiet"], ["1 related work", "accent"]]);
assert.equal(ui.advancedSummary.parentElement.dataset.marked, "true");
// None listed: the related works' chip is gone.
list.items = [];
renderComposerRelatedWorks();
assert.deepEqual(chips(), [["Saved settings: built-in", "quiet"]]);
assert.equal(ui.advancedSummary.parentElement.dataset.marked, "false");
""", extra_slices=[RELATED], extra_setup="const refresh = () => {};\n")

    def test_a_save_problem_opens_advanced_settings_at_its_field(self):
        self.run_form(r"""
await openFresh();
assert.equal(ui.advancedSettings.open, false);
edit(ui.criticCount, "11");
clickSave();
assert.equal(ui.presetSaveDialog.open, false);
assert.deepEqual([ui.notice.textContent, document.activeElement, ui.advancedSettings.open],
  ["Enter 1 to 10 independent critics.", ui.criticCount, true]);
""")

    def test_the_note_at_the_foot_of_the_panel_stays_rendered_while_empty(self):
        # A live region: a screen reader hears its first message only if it was there before.
        self.run_form(r"""
await openFresh();
assert.deepEqual([ui.presetNote.hidden, ui.presetNote.textContent], [false, ""]);
await clickLoad();
await click(button("Quick check", "Load"));
assert.deepEqual([ui.presetNote.hidden, ui.presetNote.textContent], [false, "Loaded “Quick check”."]);
edit(ui.thinkingHours, "5");
assert.deepEqual([ui.presetNote.hidden, ui.presetNote.textContent], [false, ""]);
""")


class Ancestors(HTMLParser):
    """The ids of each element's ancestors (outermost first), by id."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.parents, self._open = {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.parents[attrs["id"]] = [identity for _, identity in self._open if identity]
        if tag not in self.VOID:
            self._open.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        while self._open:
            if self._open.pop()[0] == tag:
                break


class PresetMarkupTests(unittest.TestCase):
    def test_the_panel_opens_advanced_settings_before_the_related_works(self):
        parsed = page()
        markup = INDEX.read_text(encoding="utf-8")
        tree = Ancestors()
        tree.feed(markup)
        card = markup.index('<div class="composer card">')
        # The batch the problem joins comes first, under the New problem title ("Adding to batch …"), then the
        # card, which opens with the statement: saved settings never name a batch.
        head = markup.index('<div class="page-head composer-head">')
        self.assertLess(head, markup.index('id="composerHeading"'))
        self.assertLess(markup.index('id="composerHeading"'), markup.index('id="composerBatch"'))
        self.assertLess(markup.index('id="composerBatch"'), card)
        between = markup[card + len('<div class="composer card">'):markup.index('<div id="statementFields"')]
        self.assertEqual(re.sub(r"<!--.*?-->", "", between, flags=re.S).strip(), "")
        # Advanced settings follow the stages. The Saved settings panel is the first thing inside them, then the
        # related works, then the settings themselves; the panel's note is its own.
        details = markup.index('<details id="advancedSettings" class="advanced">')
        self.assertLess(markup.index('id="workflowStageHelp"'), details)
        body = markup.index('<div class="advanced-body">', details)
        first = markup[body + len('<div class="advanced-body">'):markup.index('<div id="presetBar"')]
        self.assertEqual(re.sub(r"<!--.*?-->", "", first, flags=re.S).strip(), "")
        places = [markup.index(f'id="{identity}"') for identity in (
            "advancedSummary", "presetBar", "presetBarLabel", "presetHelp", "presetName", "presetMarks",
            "savePresetButton", "loadPresetButton", "presetNote", "relatedWorksField", "reviewModelSetting")]
        self.assertEqual(places, sorted(places))
        for identity in ("presetBar", "relatedWorksField", "composerRelatedList", "composerAddFilesButton"):
            with self.subTest(identity=identity):
                self.assertIn("advancedSettings", tree.parents[identity])
                self.assertIn("inputPanel", tree.parents[identity])
        for identity in ("presetBarLabel", "presetHelp", "presetName", "presetMarks", "savePresetButton",
                         "loadPresetButton", "presetNote"):
            with self.subTest(identity=identity):
                self.assertIn("presetBar", tree.parents[identity])
        self.assertNotIn("presetBar", tree.parents["relatedWorksField"])
        # The panel says what saved settings are, which settings fill the form, and what each button does.
        self.assertEqual(parsed.elements["presetBar"]["role"], "group")
        self.assertEqual(parsed.elements["presetBar"]["aria-labelledby"], "presetBarLabel")
        self.assertEqual(parsed.elements["presetBar"]["aria-describedby"], "presetHelp")
        self.assertEqual(parsed.elements["presetBarLabel"]["tag"], "h3")
        self.assertEqual(" ".join(parsed.texts["presetBar"].split()),
                         "Saved settings Save the workflow stages, models, limits and web search cutoff under a name "
                         "to reuse them, and choose which saved settings new problems open with. Now using: built-in "
                         "settings Save these settings… Load saved settings…")
        # The summary names the settings that fill the form, in view while the section is closed.
        self.assertEqual(" ".join(parsed.texts["advancedSummary"].split()), ", Saved settings: built-in")
        self.assertIn("advancedSettings", tree.parents["advancedSummary"])
        summary = markup[details:markup.index("</summary>", details)]
        self.assertIn('<span class="advanced-title">Advanced settings</span>', summary)
        self.assertIn('id="advancedSummary"', summary)
        # The related works are a group of their own there, titled by a heading as the panel is. Not a label of
        # the hidden file input: a click on the title would open the file chooser without clearing it first, as
        # Add files… does, and the same file chosen again would add nothing.
        related = parsed.elements["relatedWorksField"]
        self.assertEqual((related["role"], related["aria-labelledby"]), ("group", "relatedWorksLabel"))
        self.assertIn("settings-group", related["class"].split())
        self.assertEqual(parsed.elements["relatedWorksLabel"]["tag"], "h3")
        self.assertEqual(" ".join(parsed.texts["relatedWorksLabel"].split()), "Related works (optional)")
        self.assertIsNone(re.search(r'<label\b[^>]*\bfor="composerRelatedFiles"', markup))
        self.assertEqual(parsed.elements["composerRelatedFiles"]["aria-label"], "Related works to add")
        self.assertTrue(" ".join(parsed.texts["runFolderHelp"].split()).endswith(
            " Related works listed under Advanced settings are shared by every job of the folder run."))

    def test_the_dialogs(self):
        parsed = page()
        markup = INDEX.read_text(encoding="utf-8")
        # The live regions are always rendered (empty, they take no room; see the styles test), so a screen
        # reader hears their first message; the Save dialog's error interrupts.
        for identity, role in (("presetNote", "status"), ("presetLoadStatus", "status"), ("presetSaveError", "alert")):
            with self.subTest(identity=identity):
                self.assertEqual(parsed.elements[identity]["role"], role)
        self.assertNotIn("hidden", parsed.elements["presetNote"])
        self.assertNotIn("hidden", parsed.elements["presetLoadStatus"])
        # Save: an optional name, Set as default ticked, and what is saved. No maxlength: a name over 200
        # characters is refused by the server with its reason instead of being cut silently.
        name = parsed.elements["presetSaveName"]
        self.assertEqual((name["tag"], name["type"]), ("input", "text"))
        self.assertNotIn("maxlength", name)
        self.assertNotIn("required", name)
        box = parsed.elements["presetSaveDefault"]
        self.assertEqual((box["type"], "checked" in box), ("checkbox", True))
        label = markup[markup.index('for="presetSaveDefault"'):]
        self.assertIn("<span>Set as default — new problems open with these settings</span>",
                      label[:label.index("</label>")])
        self.assertEqual(" ".join(parsed.texts["presetSaveScope"].split()),
                         "Saves stages, models and reasoning, subagents, critics and rounds, time limit, web search "
                         "cutoff, speed and activity log detail. Not saved: the statement, related works and edited "
                         "prompts.")
        # Each dialog is titled as the button that opens it.
        for dialog, title, button in (("presetSaveDialog", "Save these settings", "savePresetButton"),
                                      ("presetLoadDialog", "Load saved settings", "loadPresetButton")):
            with self.subTest(dialog=dialog):
                self.assertEqual(parsed.elements[dialog]["tag"], "dialog")
                self.assertEqual(parsed.texts[parsed.elements[dialog]["aria-labelledby"]].strip(), title)
                self.assertEqual(parsed.texts[button].strip(), f"{title}…")
                self.assertEqual(parsed.elements[button]["aria-haspopup"], "dialog")
        self.assertEqual(parsed.elements["confirmPresetSaveButton"]["type"], "submit")
        self.assertEqual(parsed.elements["presetList"]["tag"], "ul")
        # Element ids stay unique.
        ids = re.findall(r'\sid="([^"]+)"', markup)
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_panel_and_the_related_works_are_hidden_in_the_models_view_and_the_panel_in_print(self):
        styles = STYLES.read_text(encoding="utf-8")
        hidden = styles[styles.index('[data-view="models"] .models-head'):]
        hidden = hidden[:hidden.index("{ display: none !important; }")]
        # Inside Advanced settings, which the Models & API view shows without its summary.
        rule = re.search(r'\[data-view="models"\] \.advanced-body > :is\(([^)]*)\)', hidden).group(1)
        for name in ("#presetBar", ".related-works-field", "#retrievalSetting", ".limits-group"):
            with self.subTest(name=name):
                self.assertIn(name, rule)
        self.assertIn('[data-view="models"] .advanced > summary', hidden)
        printing = styles[styles.index("@media print"):]
        self.assertIn(".preset-panel", printing[:printing.index("{ display: none !important; }")])

    def test_long_names_wrap_and_empty_live_regions_take_no_room(self):
        rules = style_rules()
        # A saved name can be one long word of 200 characters: the lines that quote one wrap anywhere (the
        # summary's chip too, whose row has no room to spare on a phone), and the dialogs' single column never
        # grows past the dialog (or the list's buttons go out of view).
        for selector in (".preset-note", ".preset-load-status", "#presetSaveError", ".preset-state",
                         ".preset-row-name", ".advanced-chip"):
            with self.subTest(selector=selector):
                self.assertTrue(any("overflow-wrap: anywhere" in body for body in rules[selector]))
        self.assertTrue(any("grid-template-columns: minmax(0, 1fr)" in body for body in rules[".preset-dialog-body"]))
        # Empty, the note and the status line stay rendered but take no room, like the API status lines:
        # no rule takes them out of the page (display), as a screen reader would then miss their first message.
        live = rules[".api-models-status:empty"]
        for selector in (".preset-note:empty", ".preset-load-status:empty"):
            with self.subTest(selector=selector):
                self.assertIn(live[0], rules[selector])
                self.assertIn("position: absolute", live[0])
                self.assertIn("clip: rect(0 0 0 0)", live[0])
        # (The Models & API view hides the whole panel on purpose: that rule names it.)
        for selector, bodies in rules.items():
            if (re.search(r"(\.preset-note|\.preset-load-status|#presetNote|#presetLoadStatus)\b", selector)
                    and not selector.startswith('[data-view="models"]')):
                with self.subTest(selector=selector):
                    self.assertFalse(any(re.search(r"\bdisplay:\s*none", body) for body in bodies))

    def test_a_warning_chip_looks_unlike_a_marked_one_in_every_theme(self):
        # Its words always name the warning (see the browser tests); its mark differs as well: an amber dot beside
        # the accent dot, and in Ink, whose accent and warnings are both ink, a diamond beside the round dot.
        rules = style_rules()
        marked = rules['.advanced-chip:is([data-tone="accent"], [data-tone="warning"])::before']
        self.assertTrue(any("border-radius: 50%" in body and "background: var(--accent)" in body for body in marked))
        self.assertIn("background: var(--waiting);", rules['.advanced-chip[data-tone="warning"]::before'])
        ink = rules[':root[data-theme="ink"] .advanced-chip[data-tone="warning"]::before']
        self.assertTrue(any("transform: rotate(45deg)" in body and "border-radius: 1px" in body for body in ink))


def style_rules():
    """Each selector of ui/styles.css, with the declarations of every rule naming it (outside @media)."""

    styles = re.sub(r"/\*.*?\*/", "", STYLES.read_text(encoding="utf-8"), flags=re.S)
    rules, depth, start = {}, 0, 0
    for match in re.finditer(r"[{}]", styles):
        if match.group() == "{":
            if depth == 0:
                start = match.start()
            depth += 1
            continue
        depth -= 1
        if depth == 0:
            head = styles[styles.rfind("}", 0, start) + 1:start]
            body = styles[start + 1:match.start()]
            if "{" not in body and not head.strip().startswith("@"):
                # The selector list, split at its own commas (not those inside :is(…)).
                for selector in re.split(r",(?![^()]*\))", head):
                    rules.setdefault(" ".join(selector.split()), []).append(" ".join(body.split()))
    return rules


if __name__ == "__main__":
    unittest.main()
