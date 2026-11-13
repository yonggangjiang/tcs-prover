"""The Prompts dialog over every text of the workflow: the server's catalog (GET /prompt-catalog) in a searchable
list of collapsible groups beside one text's editor, edits for the next job by name (promptOverrides) checked as the
server checks them (POST /prompt-catalog/check), a job's saved texts (state.promptSet) read-only, and an older server
without the catalog (the six role prompts, as before).

The checks cut ui/app.js into slices by marker text and run them in Node's vm with the small fake DOM of
tests/test_provider_ui.py, against a catalog shaped as the build contract says: {groups, prompts: [{name, group,
label, when, placeholders, text, emptyAllowed}]}, placeholders written as in the text ("{objective}", "[STATEMENT]"),
an empty list for a text sent as written. No server, model or CLI is involved.
"""
import json
import re
import shutil
import subprocess
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests.test_provider_ui import APP, DOM, INDEX, RUNNER, page

STYLES = APP.parent / "styles.css"
SLICES = [["// --- Prompts: every text the workflow sends its models", "function clockText("]]

# A catalog as GET /prompt-catalog answers (the build contract, section 1), short texts.
GROUPS = ["Statement review", "Author", "Coordinator", "Goal loop", "Team", "Team tools", "Critic", "Writer", "Other"]


def entry(name, group, label, placeholders, text, when="", empty=False):
    return {"name": name, "group": group, "label": label, "when": when or f"When {label} is used.",
            "placeholders": placeholders, "text": text, "emptyAllowed": empty}


CATALOG = {"groups": GROUPS, "prompts": [
    entry("review", "Statement review", "Reviewer prompt", [], "Review the statement."),
    entry("author", "Author", "Author prompt", ["[STATEMENT]"], "Prove it.\n\n[STATEMENT]"),
    entry("continuation", "Author", "Author continuation", ["{original_prompt}", "{directory}"],
          "Continue: {original_prompt} in {directory}."),
    entry("coordinator", "Coordinator", "Coordinator prompt", ["[STATEMENT]", "{author_subagents}"],
          "Coordinate {author_subagents} subagents.\n\n[STATEMENT]"),
    entry("coordinator_checkin", "Coordinator", "Coordinator time check", ["{elapsed}", "{limit}"],
          "You have worked {elapsed}. {limit}"),
    entry("goal_continuation", "Goal loop", "Goal continuation",
          ["{continuation}", "{objective}", "{elapsed}", "{limit}"],
          "{continuation}\n\nContinue working toward the active thread goal.\n<objective>\n{objective}\n</objective>",
          when="Sent by TCS Prover after each turn that did not end the goal (Goal loop: TCS Prover)."),
    entry("goal_tool_update_goal", "Goal loop", "update_goal tool", [],
          "Update the existing goal. Use {status} \"complete\" only when the objective is achieved."),
    entry("codex_compact_prompt", "Goal loop", "Codex compaction summary", [], "", empty=True),
    entry("team_note", "Team", "Team note", ["{members}"], "YOUR TEAM\n{members}"),
    entry("team_member", "Team", "Member assignment", ["{assignment}"],
          "{assignment}\n\nYour final reply is your report to the coordinator."),
    entry("team_tool_assign", "Team tools", "team_assign tool", [], "Give a member an assignment."),
    entry("team_tool_wait", "Team tools", "team_wait tool", [], "Wait for reports."),
    entry("critic", "Critic", "Critic prompt", ["{count}"], "Start {count} critics."),
    entry("convention", "Critic", "Convention critic prompt", ["{statement}"], "Judge {statement}."),
    entry("final", "Writer", "Final writer prompt", [], "Write LaTeX."),
]}

SETUP = r"""
Element.prototype.contains = function (node) {
  for (let item = node; item; item = item.parentElement) if (item === this) return true;
  return false;
};
const elements = {};
const ui = new Proxy(elements, {
  get: (target, name) => (typeof name === "string" ? (target[name] ??= new Element()) : undefined),
});
document.body = new Element("body");
ui.promptDialog.showModal = function () { this.open = true; };
ui.promptDialog.close = function () { this.open = false; };
ui.promptEditor.focus = function () { document.activeElement = this; this.focused = true; };
const show = (element, visible) => { element.hidden = !visible; };
const window = { matchMedia: () => ({ matches: false }) };
const CSS = { escape: (value) => String(value) };
let currentJob = "", currentBatch = "", activePrompt = "review";
let promptValues = {}, promptDrafts = {}, promptOriginals = {}, promptOverrides = {};
let stages = { review: true, author: true, critic: true, writing: true };
const selectedWorkflowStages = () => stages;
const workflowStagesFor = (source) => source.workflowStages || { review: true, author: true, critic: true, writing: true };
const jobPath = (path) => path;
const copy = (value) => JSON.parse(JSON.stringify(value));
const roleDefaults = () => Object.fromEntries(catalog.prompts.filter((item) =>
  ["review", "author", "coordinator", "critic", "convention", "final"].includes(item.name)).map((item) => [item.name, item.text]));
let catalog = copy(fixture.catalog);
let state = { phase: "input", workflow: { settings: { prompts: roleDefaults() } } };
// The server: the catalog (404 for an older server), its check (errors by name, or 404) and /state.
const server = { catalog: true, check: true, errors: {}, fail: "" };
const requests = [];
const notFound = () => Object.assign(new Error("Not found."), { status: 404 });
const request = async (path, body) => {
  requests.push({ path, body: body === undefined ? undefined : copy(body) });
  await null;
  if (path === "/prompt-catalog") {
    if (!server.catalog) throw notFound();
    return copy(catalog);
  }
  if (path === "/prompt-catalog/check") {
    if (server.fail) throw Object.assign(new Error(server.fail), { status: 500 });
    if (!server.check) throw notFound();
    return { errors: copy(server.errors) };
  }
  if (path === "/state") return { workflow: { settings: { prompts: roleDefaults() } } };
  throw new Error("Unexpected request " + path);
};
const open = async (options) => { await openPromptEditor(options); };
const type = (text) => { ui.promptEditor.value = text; ui.promptEditor.oninput(); };
const apply = async () => { await savePrompts(); };
// The list as shown: [group, open, count, [[name, label, mark, current]]].
const listed = () => ui.promptList.children.map((section) => {
  const toggle = section.querySelector("button");
  const items = section.querySelector("ul");
  return [toggle.querySelector("span").textContent, toggle.getAttribute("aria-expanded") === "true",
    toggle.children[1].textContent, items.hidden ? [] : items.querySelectorAll("button").map((button) => [
      button.dataset.prompt, button.children[0].textContent, button.children[1]?.textContent || "",
      button.getAttribute("aria-current") === "true"])];
});
const groupNames = () => listed().map(([group]) => group);
const fields = () => ui.promptEditorFields.children.map((node) => (typeof node === "string" ? node : node.textContent));
"""

# The handlers of the dialog's list, search, editor and buttons as app.js wires them.
HANDLERS = ["ui.editPrompts.onclick = () => openPromptEditor();", "ui.savePrompts.onclick = savePrompts;"]


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class PromptCatalogBrowserTests(unittest.TestCase):
    def run_app(self, checks, *, extra_slices=(), extra_setup="", catalog=CATALOG):
        setup = f"const fixture = {json.dumps({'catalog': catalog})};\n" + SETUP + extra_setup
        payload = json.dumps({"dom": DOM, "slices": [*SLICES, HANDLERS, *extra_slices], "setup": setup,
                              "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(APP)], input=payload,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_markers_occur_once(self):
        source = APP.read_text(encoding="utf-8")
        for marker in sorted({marker for pair in [*SLICES, HANDLERS] for marker in pair}):
            with self.subTest(marker=marker):
                self.assertEqual(source.count(marker), 1)

    def test_every_text_is_listed_by_group_in_the_servers_order(self):
        self.run_app(r"""
await open();
assert.equal(ui.promptDialog.open, true);
assert.deepEqual(requests.map((item) => item.path), ["/prompt-catalog"]);
assert.deepEqual([ui.promptDialogTitle.textContent, ui.promptDialogEyebrow.textContent], ["Edit prompts", "Advanced"]);
// Every group with a text, in the server's order ("Other" has none); the first stage's text is shown, its group open.
assert.deepEqual(groupNames(), ["Statement review", "Author", "Coordinator", "Goal loop", "Team", "Team tools",
  "Critic", "Writer"]);
assert.deepEqual(listed()[0], ["Statement review", true, "1", [["review", "Reviewer prompt", "", true]]]);
assert.deepEqual(listed().slice(1).map(([group, opened, count]) => [group, opened, count]),
  [["Author", false, "2"], ["Coordinator", false, "2"], ["Goal loop", false, "3"], ["Team", false, "2"],
    ["Team tools", false, "2"], ["Critic", false, "2"], ["Writer", false, "1"]]);
assert.equal(activePrompt, "review");
// On a narrow screen the list comes first.
assert.equal(ui.promptDialog.dataset.view, "list");
// Opening a group lists its texts; the list keeps one stop in the tab order.
ui.promptList.onclick({ target: ui.promptList.querySelectorAll("button").find((item) => item.dataset.group === "Goal loop") });
assert.deepEqual(listed()[3][3].map(([name, label]) => [name, label]), [["goal_continuation", "Goal continuation"],
  ["goal_tool_update_goal", "update_goal tool"], ["codex_compact_prompt", "Codex compaction summary"]]);
assert.deepEqual(ui.promptList.querySelectorAll("button").filter((item) => item.tabIndex === 0).map((item) => item.dataset.prompt),
  ["review"]);
ui.promptList.onclick({ target: ui.promptList.querySelectorAll("button").find((item) => item.dataset.prompt === "goal_continuation") });
assert.equal(activePrompt, "goal_continuation");
assert.equal(ui.promptDialog.dataset.view, "text");
""")

    def test_each_text_says_when_it_is_used_its_placeholders_and_its_rules(self):
        self.run_app(r"""
await open();
selectPrompt("goal_continuation");
assert.deepEqual([ui.promptEditorGroup.textContent, ui.promptEditorLabel.textContent, ui.promptEditorName.textContent,
  ui.promptEditorState.textContent], ["Goal loop", "Goal continuation", "goal_continuation", "Default"]);
assert.equal(ui.promptEditorWhen.textContent,
  "Sent by TCS Prover after each turn that did not end the goal (Goal loop: TCS Prover).");
assert.deepEqual(fields(), ["{continuation}", "{objective}", "{elapsed}", "{limit}"]);
assert.equal(ui.promptEditor.value, fixture.catalog.prompts.find((item) => item.name === "goal_continuation").text);
assert.equal(ui.promptEditorHelp.textContent, "Write the placeholders as listed; write any other brace twice: {{ and }}. "
  + "An edit applies to the next job you start from this form; new problems start from the workflow files' texts again.");
selectPrompt("author");
assert.equal(ui.promptEditorHelp.textContent.split(" An edit")[0],
  "Keep exactly one [STATEMENT]: the workflow puts the statement there.");
// A text without placeholders is sent as written, braces included.
selectPrompt("goal_tool_update_goal");
assert.deepEqual(fields(), ["None: the text is sent as written, braces included."]);
selectPrompt("codex_compact_prompt");
assert.equal(ui.promptEditor.value, "");
assert.match(ui.promptEditorHelp.textContent, /^Leave it empty to use the program's own text\./);
assert.equal(ui.promptEditCount.textContent, "No text edited: the next job uses the defaults.");
""")

    def test_search_finds_texts_by_name_use_or_wording(self):
        self.run_app(r"""
await open();
const search = (text) => { ui.promptSearch.value = text; ui.promptSearch.oninput(); };
search("continu");
// Matching groups open; the others are left out.
assert.deepEqual(listed().map(([group, opened, , items]) => [group, opened, items.map(([name]) => name)]),
  [["Author", true, ["continuation"]], ["Goal loop", true, ["goal_continuation"]]]);
search("REPORT TO THE COORDINATOR");
assert.deepEqual(listed().map(([, , , items]) => items.map(([name]) => name)), [["team_member"]]);
search("team_tool");
assert.deepEqual(groupNames(), ["Team tools"]);
search("no such words");
assert.deepEqual([listed(), ui.promptSearchEmpty.hidden, ui.promptSearchEmpty.textContent],
  [[], false, "No text matches “no such words”."]);
search("");
assert.equal(ui.promptSearchEmpty.hidden, true);
assert.equal(groupNames().length, 8);
""")

    def test_the_keys_move_through_the_list_and_open_and_close_groups(self):
        self.run_app(r"""
await open();
const key = (name) => ui.promptList.onkeydown({ key: name, preventDefault() {} });
const focused = () => document.activeElement.dataset.prompt || "group:" + document.activeElement.dataset.group;
ui.promptList.querySelectorAll("button").find((item) => item.dataset.prompt === "review").focus();
key("ArrowDown");
assert.equal(focused(), "group:Author");
key("ArrowRight");
assert.equal(listed()[1][1], true);
key("ArrowDown");
assert.equal(focused(), "author");
key("ArrowLeft");
assert.equal(focused(), "group:Author");
key("ArrowLeft");
assert.equal(listed()[1][1], false);
key("End");
assert.equal(focused(), "group:Writer");
key("Home");
assert.equal(focused(), "group:Statement review");
""")

    def test_edits_go_to_the_next_job_by_name_only_when_they_change_a_text(self):
        self.run_app(r"""
await open();
selectPrompt("goal_continuation");
type("{continuation}\n\nKeep going on {objective}. Time: {elapsed}; {limit}");
assert.equal(ui.promptEditorState.textContent, "Edited for the next job");
assert.equal(ui.promptEditCount.textContent, "1 text edited for the next job");
selectPrompt("team_tool_assign");
type("Give a member one assignment; {braces} are words here.");
// A text typed back to its default is no edit.
selectPrompt("critic");
type("Start {count} critics. ");
assert.equal(ui.promptEditorState.textContent, "Default");
await apply();
assert.equal(ui.promptDialog.open, false);
const edited = { goal_continuation: "{continuation}\n\nKeep going on {objective}. Time: {elapsed}; {limit}",
  team_tool_assign: "Give a member one assignment; {braces} are words here." };
assert.deepEqual(promptOverrides, edited);
assert.deepEqual(requests.at(-1), { path: "/prompt-catalog/check", body: { promptOverrides: edited } });
assert.equal(ui.promptSettingText.textContent,
  "2 texts edited for the next job; the others are the workflow files' defaults");
// Reopening shows them, marked in the list and their groups open.
await open();
assert.equal(promptDrafts.team_tool_assign, edited.team_tool_assign);
const goal = listed().find(([group]) => group === "Goal loop");
assert.deepEqual([goal[1], goal[2], goal[3][0]], [true, "1 of 3 edited",
  ["goal_continuation", "Goal continuation", "edited", false]]);
assert.equal(activePrompt, "review");
// Reset to default undoes one; Apply then sends only the other.
selectPrompt("team_tool_assign");
ui.resetPrompt.onclick();
assert.equal(ui.promptEditor.value, "Give a member an assignment.");
await apply();
assert.deepEqual(promptOverrides, { goal_continuation: edited.goal_continuation });
// Closing without Apply keeps what was applied.
await open();
selectPrompt("final");
type("Something else.");
ui.promptDialog.close();
assert.deepEqual(promptOverrides, { goal_continuation: edited.goal_continuation });
""")

    def test_apply_checks_each_edited_text_as_the_server_does(self):
        self.run_app(r"""
await open();
const refused = async (name, text, message) => {
  selectPrompt(name);
  type(text);
  selectPrompt("review");
  await apply();
  assert.deepEqual([ui.promptDialog.open, activePrompt, ui.promptError.hidden, ui.promptError.textContent],
    [true, name, false, message], text);
  assert.equal(ui.promptEditor.focused, true);
  type(fixture.catalog.prompts.find((item) => item.name === name).text);
};
const checks = () => requests.filter((item) => item.path === "/prompt-catalog/check").length;
await refused("author", "No marker.", "The author prompt must contain exactly one [STATEMENT].");
await refused("coordinator", "Run {count}. [STATEMENT]",
  "Write {author_subagents} where the number of author subagents goes, and write any other brace twice: {{ and }}.");
await refused("goal_continuation", "Go on with {objectve}.",
  "Use only {continuation}, {objective}, {elapsed} and {limit} as placeholders, and write any other brace twice: {{ and }}.");
await refused("goal_continuation", "A lone } brace {objective}.",
  "Use only {continuation}, {objective}, {elapsed} and {limit} as placeholders, and write any other brace twice: {{ and }}.");
await refused("goal_continuation", "{objective!r} and {elapsed:>5}",
  "Use only {continuation}, {objective}, {elapsed} and {limit} as placeholders, and write any other brace twice: {{ and }}.");
await refused("team_note", "   ", "This text cannot be empty: write it, or reset it to the default.");
await refused("critic", "", "Every role prompt must contain instructions.");
assert.equal(checks(), 0, "nothing reaches the server while the page sees a problem");
// Doubled braces, and an empty text where the catalog allows it, are fine; a text without placeholders takes any brace.
selectPrompt("team_note");
type("{{team}}: {members}");
selectPrompt("codex_compact_prompt");
type("");
selectPrompt("codex_compact_prompt");
// The server's own check has the last word: its message, on its text.
server.errors = { team_note: "Team note: the server refuses this." };
await apply();
assert.deepEqual([ui.promptDialog.open, activePrompt, ui.promptError.textContent],
  [true, "team_note", "Team note: the server refuses this."]);
server.errors = {};
server.fail = "The server could not be reached.";
await apply();
assert.deepEqual([ui.promptDialog.open, ui.promptError.textContent],
  [true, "The texts could not be checked: The server could not be reached."]);
server.fail = "";
await apply();
assert.equal(ui.promptDialog.open, false);
assert.deepEqual(promptOverrides, { team_note: "{{team}}: {members}" });
// A server without the check: the page's own checks are all there are.
server.check = false;
await open();
selectPrompt("final");
type("Write it in LaTeX.");
await apply();
assert.equal(ui.promptDialog.open, false);
assert.deepEqual(promptOverrides, { team_note: "{{team}}: {members}", final: "Write it in LaTeX." });
""")

    def test_untouched_texts_never_become_edits_and_defaults_are_read_at_every_opening(self):
        self.run_app(r"""
syncPrompts();
assert.deepEqual(promptOverrides, {});
await open();
selectPrompt("review");
type("Custom review");
await apply();
assert.deepEqual(promptOverrides, { review: "Custom review" });
// A workflow file edited while the page is open: an unedited text shows the new default when the dialog opens again.
catalog.prompts.find((item) => item.name === "author").text = "Latest YAML author. [STATEMENT]";
await open();
assert.equal(promptDrafts.author, "Latest YAML author. [STATEMENT]");
assert.equal(promptDrafts.review, "Custom review");
// A default changed while the dialog is open never turns an untouched text into an edit.
catalog.prompts.find((item) => item.name === "team_note").text = "Even newer note {members}";
await loadPromptCatalog();
selectPrompt("review");
type("Another custom review");
await apply();
assert.deepEqual(promptOverrides, { review: "Another custom review" });
// A new problem starts again from the defaults.
syncPrompts();
assert.deepEqual(promptOverrides, {});
assert.equal(promptValues.author, "Latest YAML author. [STATEMENT]");
assert.equal(promptValues.goal_continuation, fixture.catalog.prompts.find((item) => item.name === "goal_continuation").text);
// Old browser storage is never read: the page clears it once (app.js) and never writes it.
""")

    def test_a_job_shows_the_texts_it_runs_with_read_only(self):
        self.run_app(r"""
const defaults = Object.fromEntries(fixture.catalog.prompts.map((item) => [item.name, item.text]));
currentJob = "job-1";
state = { ...state, runId: "job-1", phase: "running",
  promptSet: { ...defaults, goal_continuation: "Saved continuation {objective}.", retired_text: "A text since removed." } };
await open({ job: true });
assert.deepEqual([ui.promptDialogTitle.textContent, ui.promptDialogEyebrow.textContent], ["This job's prompts", "Read-only"]);
assert.equal(ui.promptDialogLede.textContent, "The texts this job runs with, saved when it started, by where they are "
  + "used. Edit the texts for a new problem from New problem → Advanced settings → Edit prompts.");
assert.deepEqual([ui.resetPrompt.hidden, ui.savePrompts.hidden, ui.promptDialogNote.hidden], [true, true, true]);
assert.equal(ui.promptEditCount.textContent, "Read-only: the texts this job runs with.");
// Its texts, each with the catalog's description; one the catalog no longer lists, under Other.
assert.deepEqual(groupNames(), [...fixture.catalog.groups]);
selectPrompt("goal_continuation");
assert.deepEqual([ui.promptEditor.value, ui.promptEditor.readOnly, ui.promptEditorState.textContent],
  ["Saved continuation {objective}.", true, "This job's text · differs from today's default"]);
assert.deepEqual(listed().find(([group]) => group === "Goal loop").slice(1, 3), [true, "1 of 3 changed"]);
selectPrompt("retired_text");
assert.deepEqual([ui.promptEditorGroup.textContent, ui.promptEditor.value], ["Other", "A text since removed."]);
selectPrompt("author");
assert.equal(ui.promptEditorState.textContent, "This job's text · today's default");
// Typing changes nothing, and Apply only closes the dialog.
ui.promptEditor.value = "Changed?";
ui.promptEditor.oninput();
await apply();
assert.deepEqual([ui.promptDialog.open, promptOverrides, requests.filter((item) => item.path.endsWith("/check")).length],
  [false, {}, 0]);

// A job from before every text was saved: the role prompts it saved, and a note.
state = { phase: "running", runId: "old-job", workflow: state.workflow, reviewPrompt: "Old review.",
  authorPrompt: "Old author. [STATEMENT]", criticPrompt: "Old critic {count}.", conventionPrompt: "Judge {statement}.",
  finalPrompt: "Write LaTeX." };
await open({ job: true });
assert.deepEqual(listed().map(([group, , count]) => [group, count]), [["Statement review", "1 of 1 changed"],
  ["Author", "1 of 1 changed"], ["Critic", "1 of 2 changed"], ["Writer", "1"]]);
assert.deepEqual([ui.promptDialogNote.hidden, ui.promptDialogNote.textContent], [false, "This job started before TCS "
  + "Prover saved every text with its job: these are the role prompts it saved. Its other texts came from the "
  + "workflow files each time it started or resumed."]);
selectPrompt("author");
assert.equal(ui.promptEditor.value, "Old author. [STATEMENT]");
""")

    def test_an_older_server_without_the_catalog_offers_the_role_prompts(self):
        self.run_app(r"""
server.catalog = false;
await open();
assert.deepEqual(requests.map((item) => item.path), ["/prompt-catalog", "/state"]);
assert.deepEqual(groupNames(), ["Statement review", "Author", "Coordinator", "Critic", "Writer"]);
selectPrompt("critic");
assert.deepEqual([ui.promptEditorLabel.textContent, ui.promptEditorWhen.textContent], ["Critic prompt", promptHelp.critic]);
type("Start {count} critics, carefully.");
await apply();
// It has no check: the page's own rules decide.
assert.deepEqual([ui.promptDialog.open, promptOverrides], [false, { critic: "Start {count} critics, carefully." }]);
assert.equal(requests.filter((item) => item.path === "/prompt-catalog/check").length, 0);
""")

    def test_a_job_sent_back_to_the_form_edits_from_its_own_texts(self):
        self.run_app(r"""
const defaults = Object.fromEntries(fixture.catalog.prompts.map((item) => [item.name, item.text]));
currentJob = "job-2";
state = { ...state, runId: "job-2", phase: "input", promptSet: { ...defaults, team_note: "Our team: {members}" } };
// Before the catalog is read the form knows the role prompts only; then every text.
syncPrompts(state);
assert.deepEqual(Object.keys(promptValues), ["review", "author", "coordinator", "critic", "convention", "final"]);
await loadPromptCatalog();
syncPrompts(state);
assert.equal(promptValues.team_note, "Our team: {members}");
await open();
selectPrompt("team_note");
// The job's own text is what an unedited text is: not an edit.
assert.deepEqual([ui.promptEditor.value, ui.promptEditorState.textContent], ["Our team: {members}", "Default"]);
type("Our team, again: {members}");
await apply();
assert.deepEqual(promptOverrides, { team_note: "Our team, again: {members}" });
""")


class PromptCatalogMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")
        self.page = page()

    def test_the_dialog_has_a_searchable_list_beside_one_texts_editor(self):
        dialog = self.html[self.html.index('<dialog id="promptDialog"'):self.html.index("</dialog>",
                                                                                     self.html.index('<dialog id="promptDialog"'))]
        order = [dialog.index(f'id="{key}"') for key in ("promptDialogTitle", "promptSearch", "promptList",
                                                         "promptPanel", "promptEditorLabel", "promptEditorWhen",
                                                         "promptEditorFields", "promptEditor", "promptEditorHelp",
                                                         "promptEditorError", "promptEditCount", "resetPromptButton",
                                                         "savePromptsButton")]
        self.assertEqual(order, sorted(order))
        self.assertIn('>When it is used</dt>', dialog)
        self.assertIn('>Placeholders</dt>', dialog)
        self.assertIn('>Apply to the next job</button>', dialog)
        self.assertEqual(self.page.elements["promptSearch"]["type"], "search")
        self.assertEqual(self.page.elements["promptDialog"]["data-view"], "list")
        self.assertNotIn("prompt-tab", self.html)
        # Run settings offer the job's own texts; the composer's group says what Edit prompts covers.
        self.assertIn(">View this job's prompts</button>", self.html)
        self.assertLess(self.html.index('id="runMetadata"'), self.html.index('id="jobPromptsButton"'))
        self.assertIn("<strong>Prompts</strong>", self.html)

    def test_the_dialog_never_builds_markup_from_a_text(self):
        source = APP.read_text(encoding="utf-8")
        module = source[source.index(SLICES[0][0]):source.index(SLICES[0][1])]
        self.assertNotRegex(module, r"innerHTML|outerHTML|insertAdjacentHTML|DOMParser")
        self.assertIn("label.textContent = entry.label;", module)

    def test_the_two_views_of_a_narrow_screen(self):
        styles = STYLES.read_text(encoding="utf-8")
        block = styles[styles.index("@media (max-width: 720px) {\n  .prompt-browser"):]
        block = block[:block.index("\n}\n")]
        self.assertIn('.prompt-catalog-dialog[data-view="list"] .prompt-panel', block)
        self.assertIn('.prompt-catalog-dialog[data-view="text"] .prompt-nav { display: none; }', block)
        self.assertIn(".prompt-back { display: inline-flex; }", block)


if __name__ == "__main__":
    unittest.main()
