"""The page's part of the author's team and goal loop: New problem → Advanced settings → Proof author's "5 · Subagent
models" (one row of Source, Model and Reasoning menus per author subagent) and "6 · Goal loop", what a job, a rerun and
saved settings carry, the Advanced settings summary, Run settings, the activity log's rows for the team and for the
author's update_goal calls, and Setup counting each subagent's program.

The browser checks cut ui/app.js into slices by marker text and run them in Node's vm with a small fake DOM, as the
other UI tests do, against states shaped like the server's (the build spec's contract): goalLoop ("tcs" | "codex"),
authorSubagentModels (null or one {model, effort} per author subagent), authorTeam ([{member, model, modelLabel,
program, effort, connection}]), the subagent records' subagentMember and subagentModel, and "Goal status" records
{goalStatus, note}. The form's checks run with the saved-settings harness (tests/test_presets_ui.py), whose server
fixtures are real (ui/presets.py, ui/server.py and an API connection "Lab key" with its model "Model A"). No server,
model or CLI is started.
"""
import json
import re
import shutil
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import test_coordinator_ui as coordinator
from tests import test_presets_ui as presets_ui
from tests import test_no_web_search as no_web_search
from tests import test_team_goal_loop as team_loop
from tests.test_provider_ui import APP, INDEX, page

# The handlers of Author subagents, Subagent models and Goal loop (index.html's controls as app.js wires them).
HANDLERS = ["// The subagent models show while Author subagents is 1 or more",
            "ui.retrievalMode.onchange = () => {"]
# How the page puts a job's records into the activity log (with coordinator.ACTIVITY: describe and upsertTimeline).
INGEST = ["// Keep only exact application prompts", "function completedRunLabel("]
# Setup's reading of the form (what each program is needed for) and its words.
SETUP_SLICES = [["function setupWords()", "function setupElement("],
                ["function setupFormRoles()", "// Whether the built-in settings, which new problems open with"],
                ["function setupJoin(", "// The programs that need setting up"]]

FORM_SETUP = r"""

// The Proof author's subagent controls as index.html has them.
elements.authorSubagentModelsMode = new Select([new Option("Same as the coordinator", "same"),
  new Option("Choose for each", "each")]);
for (const [id, value] of [["goalLoopTcs", "tcs"], ["goalLoopCodex", "codex"]]) {
  elements[id] = Object.assign(new Element("input"), {type: "radio", value});
}
ui.goalLoopTcs.checked = true;
ui.advancedSettings.append(ui.authorSubagents, ui.authorSubagentModelsMode, ui.authorTeamSetting, ui.goalLoopTcs,
  ui.goalLoopCodex);
ui.authorTeamSetting.hidden = true;
// One subagent's row: [name, source, model, effort, chip].
const shownRows = () => ui.authorTeamRows.children.map((row) => {
  const [name, , source, , model, , effort, chip] = row.children;
  return [name.textContent, source.value, model.value, effort.value, chip.textContent];
});
const choose = (mode) => { ui.authorSubagentModelsMode.value = mode; ui.authorSubagentModelsMode.onchange(); };
const count = (value, leave = true) => {
  ui.authorSubagents.value = String(value);
  ui.authorSubagents.oninput();
  if (leave) ui.authorSubagents.onchange();
};
const loop = (value) => {
  const input = value === "codex" ? ui.goalLoopCodex : ui.goalLoopTcs;
  ui.goalLoopTcs.checked = value !== "codex";
  ui.goalLoopCodex.checked = value === "codex";
  input.onchange();
};
const pick = (index, menu, value) => { ui[`member${index}${menu}`].value = value; ui[`member${index}${menu}`].onchange(); };
const lab = fixture.labModel, labSource = fixture.labModel.split("--m-")[0];
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class TeamFormTests(unittest.TestCase):
    """5 · Subagent models and 6 · Goal loop on the New problem form, with real server fixtures."""

    @classmethod
    def setUpClass(cls):
        presets_ui.PresetBrowserTests.setUpClass.__func__(cls)

    run_app = presets_ui.PresetBrowserTests.run_app

    def run_form(self, checks, extra_slices=()):
        self.run_app(checks, extra_slices=[HANDLERS, *extra_slices],
                     extra_setup="const refresh = () => {};\n" + FORM_SETUP)

    def test_the_choice_shows_with_the_author_stage_and_one_or_more_subagents(self):
        self.run_form(r"""
await openFresh();
// The default saved settings ("Careful proof") have 2 author subagents.
const shown = () => [ui.authorSubagentModelsLabel.hidden, ui.authorSubagentModelsMode.hidden,
  ui.authorSubagentModelsHelp.hidden].map((hidden) => !hidden);
assert.deepEqual(shown(), [true, true, true]);
assert.equal(ui.authorSubagentModelsMode.value, "same");
assert.equal(ui.authorSubagentModelsHelp.textContent, "Every author subagent runs the coordinator's model and reasoning.");
assert.equal(ui.authorTeamSetting.hidden, true);
for (const value of ["0", "", "2.5", "11"]) {
  count(value, false);
  assert.deepEqual(shown(), [false, false, false], JSON.stringify(value));
}
count(1);
assert.deepEqual(shown(), [true, true, true]);
ui.workflowAuthor.checked = false;
setWorkflowStages();
assert.deepEqual(shown(), [false, false, false], "the Author stage is off");
ui.workflowAuthor.checked = true;
setWorkflowStages();
assert.deepEqual(shown(), [true, true, true]);
""")

    def test_choose_for_each_lists_a_row_per_subagent_with_the_roles_menus(self):
        self.run_form(r"""
await openFresh();
count(3);
setChoice(ui.authorModel, "claude-opus-5-5");
ui.authorEffort.value = "high";
choose("each");
assert.equal(ui.authorTeamSetting.hidden, false);
assert.equal(ui.authorSubagentModelsHelp.textContent, "Choose a model and reasoning for each author subagent below: "
  + "Codex CLI, Claude Code or an API connection.");
// Every new row starts as the coordinator's model and reasoning.
assert.deepEqual(shownRows(), [1, 2, 3].map((n) => [`Subagent ${n}`, "claude", "claude-opus-5-5", "high", "Claude Code"]));
// Each row has the roles' three menus, labelled, and the chip under them names the model's program.
const row = ui.authorTeamRows.children[1];
assert.deepEqual(row.children.filter((node) => node.tagName === "LABEL").map((label) => [label.textContent, label.htmlFor]),
  [["1 · Source", "member2Source"], ["2 · Model", "member2Model"], ["3 · Reasoning", "member2Effort"]]);
assert.deepEqual([row.getAttribute("role"), row.getAttribute("aria-labelledby")], ["group", "member2Name"]);
// The source menu offers what the roles' do: the Codex CLI, Claude Code and each API connection.
assert.deepEqual(ui.member2Source.options.map((option) => option.value), ui.authorSource.options.map((option) => option.value));
assert.ok(ui.member2Source.options.some((option) => option.value === labSource));
pick(2, "Source", labSource);
pick(3, "Source", "codex");
pick(3, "Model", "gpt-6-sol");
pick(3, "Effort", "medium");
assert.deepEqual(shownRows().map((item) => item.slice(1, 3)), [["claude", "claude-opus-5-5"], [labSource, lab],
  ["codex", "gpt-6-sol"]]);
assert.match(shownRows()[1][4], /^API · OpenAI format via Codex CLI · api\.lab\.example/);
assert.deepEqual(proofJobSettings().authorSubagentModels, [{model: "claude-opus-5-5", effort: "high"},
  {model: lab, effort: ui.member2Effort.value}, {model: "gpt-6-sol", effort: "medium"}]);
// The reasoning menu offers only the levels each row's model supports (as the roles' do).
const supported = state.workflow.settings.model_efforts[lab];
assert.deepEqual(ui.member2Effort.options.filter((option) => !option.hidden).map((option) => option.value),
  ["low", "medium", "high", "xhigh", "max"].filter((level) => supported.includes(level)).reverse());
// Same as the coordinator again sends no list (every subagent runs the coordinator's model) and keeps the rows.
choose("same");
assert.equal(ui.authorTeamSetting.hidden, true);
assert.equal(proofJobSettings().authorSubagentModels, null);
choose("each");
assert.equal(shownRows()[2][2], "gpt-6-sol");
""")

    def test_changing_the_number_keeps_the_rows_that_remain_and_adds_the_coordinators_model(self):
        self.run_form(r"""
await openFresh();
count(3);
choose("each");
pick(1, "Source", "claude");
pick(2, "Source", labSource);
assert.deepEqual(shownRows().map((row) => row[2]), ["claude-fable-5-1", lab, "gpt-6.1-sol"].map((model, index) =>
  index === 0 ? ui.member1Model.value : model));
const first = ui.member1Model.value;
count(2);
assert.deepEqual(shownRows().map((row) => row[2]), [first, lab]);
assert.equal(proofJobSettings().authorSubagentModels.length, 2);
// New rows take the coordinator's model as it is now.
setChoice(ui.authorModel, "gpt-6-luna");
ui.authorEffort.value = "medium";
count(4);
assert.deepEqual(shownRows().map((row) => [row[2], row[3]]),
  [[first, ui.member1Effort.value], [lab, ui.member2Effort.value], ["gpt-6-luna", "medium"], ["gpt-6-luna", "medium"]]);
// A number being typed only hides rows: "4" → "1" → "" → "4" keeps them all until the field is left.
pick(4, "Model", "gpt-6-astra");
count(1, false);
assert.deepEqual(shownRows().map((row) => row[2]), [first]);
count("", false);
count(4, false);
assert.deepEqual(shownRows().map((row) => row[2]), [first, lab, "gpt-6-luna", "gpt-6-astra"]);
// Leaving the field at 1 drops the others; 3 then adds two rows set to the coordinator's model.
count(1);
count(3);
assert.deepEqual(shownRows().map((row) => row[2]), [first, "gpt-6-luna", "gpt-6-luna"]);
assert.deepEqual(proofJobSettings().authorSubagentModels.map((item) => item.model), [first, "gpt-6-luna", "gpt-6-luna"]);
""")

    def test_a_web_cutoff_fixes_the_choice_to_the_coordinators_model_with_its_reason(self):
        self.run_form(r"""
await openFresh();
count(2);
choose("each");
pick(2, "Source", "claude");
ui.retrievalMode.value = "historical";
ui.retrievalCutoffUtc.value = fixture.cutoff;
renderRetrievalSettings();
assert.deepEqual([ui.authorSubagentModelsMode.value, ui.authorSubagentModelsMode.disabled, ui.authorTeamSetting.hidden],
  ["same", true, true]);
assert.equal(ui.authorSubagentModelsHelp.textContent, "With a web cutoff, author subagents use the coordinator's model.");
assert.equal(proofJobSettings().authorSubagentModels, null);
// A web-cutoff job runs Codex's goal mode (the server records "codex"): the form shows it and says so.
assert.deepEqual([ui.goalLoopTcs.checked, ui.goalLoopTcs.disabled, ui.goalLoopCodex.checked, ui.goalLoopNote.hidden,
  ui.goalLoopNote.textContent], [false, true, true, false, "Web-cutoff runs use Codex's goal mode."]);
assert.equal(proofJobSettings().goalLoop, "codex");
// Without the cutoff the rows come back as they were.
ui.retrievalMode.value = "live";
renderRetrievalSettings();
assert.deepEqual([ui.authorSubagentModelsMode.value, ui.authorSubagentModelsMode.disabled, ui.authorTeamSetting.hidden],
  ["each", false, false]);
assert.equal(shownRows()[1][1], "claude");
assert.equal(proofJobSettings().authorSubagentModels.length, 2);
assert.deepEqual([ui.goalLoopTcs.checked, ui.goalLoopTcs.disabled, ui.goalLoopNote.hidden, proofJobSettings().goalLoop],
  [true, false, true, "tcs"]);
""")

    def test_the_settings_a_job_starts_with(self):
        self.run_form(r"""
await openFresh();
count(2);
assert.deepEqual([proofJobSettings().authorSubagentModels, proofJobSettings().goalLoop], [null, "tcs"]);
choose("each");
assert.equal(proofJobSettings().authorSubagentModels.length, 2);
// No list without subagents, or with the Author stage off: the server takes the coordinator's model then.
count(0);
assert.equal(proofJobSettings().authorSubagentModels, null);
count(2);
ui.workflowAuthor.checked = false;
setWorkflowStages();
assert.equal(proofJobSettings().authorSubagentModels, null);
ui.workflowAuthor.checked = true;
setWorkflowStages();
assert.equal(proofJobSettings().authorSubagentModels.length, 2);
loop("codex");
assert.equal(proofJobSettings().goalLoop, "codex");
""")

    def test_jobs_reruns_and_saved_settings_restore_the_team_and_the_goal_loop(self):
        self.run_form(r"""
await openFresh();
const team = [{model: lab, effort: "high"}, {model: "claude-opus-5-5", effort: "max"}];
applyComposerSettings({...fresh(), authorSubagents: 2, authorSubagentModels: team, goalLoop: "codex"});
assert.deepEqual([ui.authorSubagentModelsMode.value, ui.authorTeamSetting.hidden], ["each", false]);
assert.deepEqual(shownRows().map((row) => [row[1], row[2], row[3]]), [[labSource, lab, "high"], ["claude", "claude-opus-5-5", "max"]]);
assert.deepEqual([ui.goalLoopTcs.checked, ui.goalLoopCodex.checked], [false, true]);
assert.deepEqual(proofJobSettings().authorSubagentModels, team);
// A source without them (older jobs and saved settings): the coordinator's model and TCS Prover's loop.
applyComposerSettings({...fresh(), authorSubagents: 2});
assert.deepEqual([ui.authorSubagentModelsMode.value, ui.authorTeamSetting.hidden, proofJobSettings().goalLoop],
  ["same", true, "tcs"]);
assert.equal(proofJobSettings().authorSubagentModels, null);
// A job from before the goal loop setting ran Codex's goal mode and the CLI's own subagents: a rerun keeps them (see
// test_a_rerun_of_a_job_from_before_keeps_its_goal_loop_and_subagents).
applyComposerSettings({...fresh(), authorSubagents: 2, goalLoop: "codex", goalLoopRecorded: false});
assert.deepEqual([proofJobSettings().goalLoop, proofJobSettings().legacyTeam], ["codex", true]);
applyComposerSettings({...fresh(), authorSubagents: 2, goalLoop: "codex", goalLoopRecorded: true});
assert.deepEqual([proofJobSettings().goalLoop, proofJobSettings().legacyTeam], ["codex", undefined]);
// A job sent back to the form shows its own.
currentJob = "job-1";
composingProblem = false;
previousPhase = "running";
render({...fresh(), runId: "job-1", authorSubagents: 2, authorSubagentModels: team, goalLoop: "tcs"});
assert.deepEqual(proofJobSettings().authorSubagentModels, team);
""")

    def test_a_rerun_of_a_job_from_before_keeps_its_goal_loop_and_subagents(self):
        self.run_form(r"""
await openFresh();
const kept = "As in the job you are rerunning, which started before TCS Prover's team: the author subagents are the "
  + "coordinator's own CLI subagents, each running the coordinator's model and reasoning. Choose for each, or TCS "
  + "Prover as the goal loop, for TCS Prover's team.";
const sent = () => { const settings = proofJobSettings(); return [settings.goalLoop, settings.authorSubagentModels, settings.legacyTeam]; };
// A job from before (it recorded no goal loop: the server restores it with Codex's goal mode), and a job that kept
// such a job's settings: the form shows Codex built-in and the CLI's own subagents, and the job starts with them.
for (const before of [{goalLoopRecorded: false}, {legacyTeam: true}]) {
  applyComposerSettings({...fresh(), authorSubagents: 2, goalLoop: "codex", ...before});
  renderTeamModels();
  assert.deepEqual([ui.goalLoopTcs.checked, ui.goalLoopCodex.checked, ui.authorSubagentModelsMode.value], [false, true, "same"]);
  assert.equal(ui.authorSubagentModelsHelp.textContent, kept);
  assert.deepEqual(sent(), ["codex", null, true]);
  renderAdvancedSummary();
  assert.ok(allChips().some(([text, tone]) => text === "Coordinator, up to 2 author subagents at a time (the CLI's own)"
    && tone === "accent"), JSON.stringify(allChips()));
}
// TCS Prover's loop or a model for each subagent gives TCS Prover's team, as for any new job; back to Codex built-in
// and Same as the coordinator, the job's own again.
loop("tcs");
assert.deepEqual(sent(), ["tcs", null, undefined]);
assert.equal(ui.authorSubagentModelsHelp.textContent, "Every author subagent runs the coordinator's model and reasoning.");
loop("codex");
assert.deepEqual(sent(), ["codex", null, true]);
choose("each");
assert.deepEqual([sent()[1].length, sent()[2]], [2, undefined]);
choose("same");
assert.deepEqual(sent(), ["codex", null, true]);
// Saved settings never hold it, and a fresh form or saved settings never get it.
assert.equal("legacyTeam" in presetFormSettings(), false);
applyComposerSettings({...fresh(), authorSubagents: 2, goalLoop: "codex"});
assert.deepEqual(sent(), ["codex", null, undefined]);
// Rerun with suggested statement takes the job's settings along.
globalThis.promptLabels = {};
globalThis.newProblem ??= async () => {};
currentJob = "old-job";
state = {...state, runId: "old-job", authorSubagents: 2, goalLoop: "codex", goalLoopRecorded: false, legacyTeam: true};
await rerunWithStatement("A clearer statement.");
assert.deepEqual([composerSeed.settings.goalLoop, composerSeed.settings.goalLoopRecorded, composerSeed.settings.legacyTeam],
  ["codex", false, true]);
""", extra_slices=[["async function rerunWithStatement(", "// The New problem page, adding to"]])

    def test_a_job_from_before_revised_in_place_shows_its_goal_loop_and_subagents_fixed(self):
        # A job from before TCS Prover's goal loop and team (or one that kept such a job's settings) sent back to the
        # form by its statement review: Start revises it in its own folder, where the server keeps Codex's goal mode
        # and the CLI's own subagents whatever the form says. The form shows them fixed and says why.
        self.run_form(r"""
await openFresh();
const kept = "This job started with the CLI's own author subagents, each running the coordinator's model and "
  + "reasoning, and keeps them when it is revised; a new problem can use TCS Prover's team.";
for (const before of [{goalLoopRecorded: false}, {legacyTeam: true}]) {
  currentJob = "old-job";
  composingProblem = false;
  previousPhase = "running";
  render({...fresh(), runId: "old-job", authorSubagents: 2, goalLoop: "codex", ...before});
  renderTeamModels();
  assert.deepEqual([ui.authorSubagentModelsMode.value, ui.authorSubagentModelsMode.disabled,
    ui.authorSubagentModelsHelp.textContent], ["same", true, kept], JSON.stringify(before));
  assert.deepEqual([ui.goalLoopTcs.checked, ui.goalLoopTcs.disabled, ui.goalLoopCodex.checked, ui.goalLoopNote.hidden,
    ui.goalLoopNote.textContent], [false, true, true, false,
    "This job started with Codex's goal mode and keeps it when it is revised."]);
  // Nothing on the form can ask for TCS Prover's team for this revision.
  loop("tcs");
  choose("each");
  assert.deepEqual([proofJobSettings().goalLoop, proofJobSettings().authorSubagentModels], ["codex", null]);
  renderAdvancedSummary();
  assert.ok(allChips().some(([text]) => text === "Coordinator, up to 2 author subagents at a time (the CLI's own)"),
    JSON.stringify(allChips()));
  choose("same");
  loop("codex");
}
// A job of today revised in place keeps its own choices, which the form may change.
previousPhase = "running";
render({...fresh(), runId: "old-job", authorSubagents: 2, goalLoop: "tcs", goalLoopRecorded: true});
renderTeamModels();
assert.deepEqual([ui.authorSubagentModelsMode.disabled, ui.goalLoopTcs.disabled, proofJobSettings().goalLoop],
  [false, false, "tcs"]);
""")

    def test_saved_settings_hold_the_team_and_the_goal_loop(self):
        self.run_form(r"""
await openFresh();
count(2);
assert.equal(ui.presetMarks.textContent, " (default)");
// Each choice is a change of the form (Saved settings say "· changed on this form").
choose("each");
renderPresetBar();
assert.equal(ui.presetMarks.textContent, " (default) · changed on this form");
choose("same");
renderPresetBar();
assert.equal(ui.presetMarks.textContent, " (default)");
loop("codex");
renderPresetBar();
assert.equal(ui.presetMarks.textContent, " (default) · changed on this form");
loop("tcs");
choose("each");
pick(2, "Source", "claude");
const settings = presetFormSettings();
assert.deepEqual([settings.goalLoop, settings.authorSubagentModels.map((item) => item.model)],
  ["tcs", ["gpt-6.1-sol", ui.member2Model.value]]);
assert.equal("promptOverrides" in settings, false);
// Saved with a web cutoff on, they keep the loop the form keeps for when the cutoff is off again (the cutoff's job
// itself runs Codex's goal mode); loaded and with the cutoff off, the job runs that loop.
ui.retrievalMode.value = "historical";
ui.retrievalCutoffUtc.value = fixture.cutoff;
renderRetrievalSettings();
assert.equal(proofJobSettings().goalLoop, "codex");
const saved = presetFormSettings();
assert.equal(saved.goalLoop, "tcs");
loop("codex");
applyComposerSettings({...saved, retrievalMode: "live", retrievalCutoffUtc: ""});
renderRetrievalSettings();
assert.equal(proofJobSettings().goalLoop, "tcs");
""")

    def test_a_saved_subagent_model_that_was_removed_loads_as_gpt_6_1_sol_with_a_note(self):
        self.run_form(r"""
await openFresh();
const settings = {...fixture.listing.presets.find((item) => item.name === "Careful proof").settings, authorSubagents: 2,
  authorSubagentModels: [{model: "api:gone--m-0123456789abcdef", effort: "high"}, {model: "claude-opus-5-5", effort: "max"}],
  goalLoop: "codex"};
applyPreset({id: "p-team", name: "Team", settings, labels: {"api:gone--m-0123456789abcdef": "Gone model"}, unavailable: []});
assert.deepEqual(proofJobSettings().authorSubagentModels, [{model: "gpt-6.1-sol", effort: "high"},
  {model: "claude-opus-5-5", effort: "max"}]);
assert.equal(proofJobSettings().goalLoop, "codex");
assert.equal(presetStore().note, "Author subagent 1 model not available (Gone model), so the author subagent 1 uses "
  + "GPT 6.1 Sol, which runs through the Codex CLI. The saved settings are unchanged.");
assert.equal(presetStore().warning, true);
// The server's list names a member's unavailable model by its number: only that member falls back, never the author.
applyPreset({id: "p-team", name: "Team", labels: {}, settings: {...settings, authorModel: "claude-opus-5-5",
  authorSubagentModels: [{model: "claude-opus-5-5", effort: "max"}, {model: "claude-sonnet-5-5", effort: "high"}]},
  unavailable: [{role: "author", member: 2, model: "claude-sonnet-5-5", label: "Claude Sonnet 5.5",
    text: "Author subagent 2 model not available: Claude Sonnet 5.5"}]});
assert.equal(ui.authorModel.value, "claude-opus-5-5");
assert.deepEqual(proofJobSettings().authorSubagentModels.map((item) => item.model), ["claude-opus-5-5", "gpt-6.1-sol"]);
assert.match(presetStore().note, /^Author subagent 2 model not available \(Claude Sonnet 5\.5\)/);
""")

    def test_the_summary_of_advanced_settings_names_the_goal_loop_and_the_team(self):
        self.run_form(r"""
await openFresh();
count(3);
renderAdvancedSummary();
assert.deepEqual(allChips().slice(1), [["Goal loop: TCS Prover", "quiet"],
  ["Coordinator, 3 author subagents: same model as the coordinator", "quiet"]]);
choose("each");
pick(2, "Source", "claude");
pick(2, "Model", "claude-opus-5-5");
pick(3, "Source", labSource);
loop("codex");
renderAdvancedSummary();
assert.deepEqual(allChips().slice(1), [["Goal loop: Codex built-in", "accent"],
  ["Coordinator, 3 author subagents: GPT 6.1 Sol · Claude Opus 5.5 · Model A (API)", "accent"]]);
// A Claude Code author always runs TCS Prover's loop, whatever the choice (kept for a Codex author).
const coordinator = ui.authorModel.value;
setChoice(ui.authorModel, "claude-opus-5-5");
renderAdvancedSummary();
assert.deepEqual(allChips()[1], ["Goal loop: TCS Prover (Claude Code author)", "quiet"]);
setChoice(ui.authorModel, coordinator);
renderAdvancedSummary();
assert.deepEqual(allChips()[1], ["Goal loop: Codex built-in", "accent"]);
// Without subagents only the goal loop; with the Author stage off neither.
count(0);
renderAdvancedSummary();
assert.deepEqual(allChips().slice(1), [["Goal loop: Codex built-in", "accent"]]);
ui.workflowAuthor.checked = false;
renderAdvancedSummary();
assert.deepEqual(allChips().slice(1), []);
""")

    def test_a_removed_api_model_of_a_subagent_falls_back_like_a_roles(self):
        self.run_form(r"""
await openFresh();
count(2);
choose("each");
pick(2, "Source", labSource);
const replaced = adoptApiModels(withoutApi(fixture.apiModels));
assert.deepEqual(replaced, ["member2"]);
assert.equal(defaultModelSentence(replaced, true),
  "The author subagent 2 now uses the default model because its selected model is no longer available.");
assert.deepEqual(shownRows()[1].slice(1, 3), ["codex", "gpt-6.1-sol"]);
""", extra_slices=[["// The roles whose model the menus replaced", "let apiModels = []"]])


class TeamMarkupTests(unittest.TestCase):
    """ui/index.html: the controls after 4 · Subagents and the rows' box."""

    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")
        self.page = page()

    def test_subagent_models_and_goal_loop_follow_the_subagents_field(self):
        box = self.html[self.html.index('<div id="authorModelSetting"'):self.html.index('<div id="criticModelSetting"')]
        order = [box.index(marker) for marker in ('id="authorSubagents"', 'id="authorSubagentModelsLabel"',
                                                   'id="authorSubagentModelsMode"', 'id="goalLoopLabel"',
                                                   'id="goalLoopTcs"', 'id="goalLoopCodex"', 'id="authorTeamSetting"')]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.page.menus["authorSubagentModelsMode"],
                         [["same", True, "Same as the coordinator"], ["each", False, "Choose for each"]])
        self.assertIn('<label id="authorSubagentModelsLabel" for="authorSubagentModelsMode">5 · Subagent models</label>',
                      box)
        self.assertIn('<span id="goalLoopLabel" class="model-setting-label">6 · Goal loop</span>', box)
        radios = {key: self.page.elements[key] for key in ("goalLoopTcs", "goalLoopCodex")}
        self.assertEqual([(item["value"], "checked" in item) for item in radios.values()],
                         [("tcs", True), ("codex", False)])
        names = re.findall(r'<span class="goal-loop-name">([^<]*)</span>', box)
        self.assertEqual(names, ["TCS Prover (editable texts)", "Codex built-in"])
        helps = [" ".join(re.search(rf'<span id="{key}"[^>]*>(.*?)</span>', box, re.S).group(1).split())
                 for key in ("goalLoopTcsHelp", "goalLoopCodexHelp")]
        self.assertEqual(helps, ["TCS Prover starts every turn and sends the continuation itself, with the texts "
                                 "under Edit prompts.",
                                 "The Codex CLI's own goal mode with its own texts, as before; Claude Code authors "
                                 "always use TCS Prover's loop."])
        team = self.page.elements["authorTeamSetting"]
        self.assertIn("hidden", team)
        self.assertEqual(team["role"], "group")
        self.assertIn('>Proof author · subagent models</h3>', self.html)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class TeamJobTests(unittest.TestCase):
    """Run settings, the activity log and Setup for a job with TCS Prover's team."""

    run_app = coordinator.CoordinatorBrowserTests.run_app

    def test_run_settings_name_the_team_and_the_goal_loop(self):
        self.run_app(coordinator.OVERVIEW, coordinator.OVERVIEW_SETUP + r"""
const row = (label) => ui.runMetadata.children.find(item => item.children[0].textContent === label);
const team = [{member: 1, model: 'gpt-6.1-sol', modelLabel: 'GPT 6.1 Sol', program: 'Codex CLI', effort: 'max',
  connection: null}, {member: 2, model: 'claude-opus-5-5', modelLabel: 'Claude Opus 5.5', program: 'Claude Code',
  effort: 'high', connection: null}, {member: 3, model: 'api:lab--m-a', modelLabel: 'Lab model',
  program: 'API connection "Lab gateway"', effort: 'high', connection: 'Lab gateway'}];
""", r"""
const models = team.map(({model, effort}) => ({model, effort}));
assert.equal(proofAuthor({authorSubagents: 3, authorMode: 'coordinator', authorTeam: team, authorSubagentModels: models,
  goalLoop: 'tcs'}), 'gpt-6.1-sol · max · coordinator, 3 author subagents: GPT 6.1 Sol · Claude Opus 5.5 · Lab model (API)');
assert.equal(row('Goal loop').children[1].textContent,
  'TCS Prover (editable texts; this job runs with the texts saved when it started)');
assert.equal(proofAuthor({authorSubagents: 3, authorMode: 'coordinator', authorTeam: team, authorSubagentModels: null,
  goalLoop: 'codex'}), 'gpt-6.1-sol · max · coordinator, 3 author subagents: same model as the coordinator');
assert.equal(row('Goal loop').children[1].textContent, 'Codex built-in (the Codex CLI\'s own goal mode)');
assert.equal(proofAuthor({authorSubagents: 1, authorMode: 'coordinator', authorTeam: team.slice(0, 1),
  authorSubagentModels: null}), 'gpt-6.1-sol · max · coordinator, 1 author subagent: same model as the coordinator');
// A coordinator job without TCS Prover's team (its CLI's own subagents) and older jobs keep their words, and a job
// that records no goal loop shows no row for it.
assert.equal(proofAuthor({authorSubagents: 3, authorMode: 'coordinator'}),
  'gpt-6.1-sol · max · coordinator, up to 3 author subagents at a time');
assert.equal(row('Goal loop'), undefined);
assert.equal(proofAuthor({authorSubagents: 0, authorMode: 'author', goalLoop: 'tcs'}), 'gpt-6.1-sol · max · works alone');
assert.ok(row('Goal loop'));
// An author that runs through Claude Code always ran TCS Prover's loop (Claude Code has no goal mode), also in a job
// from before the setting, which the server restores with Codex built-in, and with an Anthropic-format API model.
proofAuthor({authorModel: 'claude-opus-5-5', authorSubagents: 0, authorMode: 'author', goalLoop: 'codex'});
assert.equal(row('Goal loop').children[1].textContent, 'TCS Prover (Claude Code authors always use it)');
proofAuthor({authorModel: 'claude-opus-5-5', authorSubagents: 0, authorMode: 'author', goalLoop: 'codex',
  promptSet: {goal: 'Prove S.'}});
assert.equal(row('Goal loop').children[1].textContent,
  'TCS Prover (Claude Code authors always use it; this job runs with the texts saved when it started)');
proofAuthor({authorModel: 'api:gate--m-c', authorSubagents: 0, authorMode: 'author', goalLoop: 'codex',
  workflow: {...job.workflow, settings: {model_catalog: [{id: 'api:gate--m-c', label: 'Model C', provider: 'api',
    format: 'anthropic', sourceLabel: 'Gateway', host: 'gate.example'}]}}});
assert.equal(row('Goal loop').children[1].textContent, 'TCS Prover (Claude Code authors always use it)');
proofAuthor({authorModel: 'api:lab--m-a', authorSubagents: 0, authorMode: 'author', goalLoop: 'codex',
  workflow: {...job.workflow, settings: {model_catalog: [{id: 'api:lab--m-a', label: 'Model A', provider: 'api',
    format: 'openai', sourceLabel: 'Lab', host: 'lab.example'}]}}});
assert.equal(row('Goal loop').children[1].textContent, "Codex built-in (the Codex CLI's own goal mode)");
""")

    def test_activity_rows_name_each_subagent_and_its_model(self):
        self.run_app(coordinator.ACTIVITY, coordinator.ACTIVITY_SETUP, r"""
const card = (status, extra) => describe(subagent(status, {subagentMember: 2, subagentModel: 'Claude Opus 5.5',
  subagentsRunning: 2, ...extra}));
assert.deepEqual([card('started').label, card('started').text],
  ['Author subagent 2 (Claude Opus 5.5) started', 'Prove Lemma 3 by induction on n · 2 running']);
assert.equal(card('finished', {subagentsRunning: 0}).label, 'Author subagent 2 (Claude Opus 5.5) finished');
assert.deepEqual([card('failed', {text: 'Claude Code is not signed in.'}).label, card('failed', {text: 'Claude Code is not signed in.'}).text],
  ['Author subagent 2 (Claude Opus 5.5) failed', 'Claude Code is not signed in. · Prove Lemma 3 by induction on n · 2 running']);
assert.equal(card('started').pinned, true);
// A finished member's report file (relative to the working folder) closes its line.
assert.equal(card('finished', {subagentsRunning: 1, subagentReport: 'team/member-2/1-report.md'}).text,
  'Prove Lemma 3 by induction on n · 1 running · report: team/member-2/1-report.md');
assert.equal(card('started', {subagentReport: 'team/member-2/1-report.md'}).text,
  'Prove Lemma 3 by induction on n · 2 running');
// Two members' rows never replace each other.
assert.notEqual(card('started').key, describe(subagent('started', {subagentMember: 3})).key);
// A record from before the team keeps its words.
assert.equal(describe(subagent('started')).label, 'Author subagent started');
// A member's own events: "Subagent 2 …", kept apart from another member's items with the same id.
const event = (member, item, method = 'item.completed') => ({kind: 'codex_event', root: false, stage: 'solve',
  node: 'author', subagentMember: member, subagentId: 'a' + member, time: 't', event: {type: method, item}});
const reasoning = describe(event(2, {id: 'item_1', type: 'reasoning', text: 'Try induction.'}));
assert.equal(reasoning.label, 'Subagent 2 reasoning');
assert.notEqual(reasoning.key, describe(event(3, {id: 'item_1', type: 'reasoning', text: 'x'})).key);
assert.equal(describe(event(2, {id: 'item_2', type: 'agent_message', text: 'Done.'})).label, 'Subagent 2');
assert.equal(describe(event(2, {id: 'item_3', type: 'command_execution', command: 'ls', status: 'completed'})).label,
  'Subagent 2 tool');
assert.equal(describe({...event(2, {}), event: {method: 'turn/started', params: {turn: {id: 't1'}}}}).label,
  'Subagent 2 turn running');
// The coordinator's team tools by their names, from Codex (server "tcs", tool "team_assign") or Claude Code.
const toolCall = (item) => describe({kind: 'codex_event', root: true, stage: 'solve', node: 'author', time: 't',
  event: {method: 'item/completed', params: {item}}});
assert.equal(toolCall({id: 'c1', type: 'mcpToolCall', server: 'tcs', tool: 'team_assign', status: 'completed'}).text,
  'team_assign · completed');
assert.equal(toolCall({id: 'c2', type: 'mcpToolCall', name: 'mcp__tcs__team_wait', status: 'completed'}).text,
  'team_wait · completed');
// The items of the coordinator's direct calls as the real Codex 0.160 recorded them: each tool by its own name.
assert.equal(toolCall({type: 'mcpToolCall', id: 'act-wait1-13', server: 'tcs', tool: 'team_wait',
  status: 'completed', arguments: {timeout_minutes: 2}, error: null, durationMs: 2080,
  result: {content: [{type: 'text', text: 'Member 1 (gpt-6.1-sol) finished the assignment.'}]}}).text,
  'team_wait · completed');
assert.equal(toolCall({type: 'mcpToolCall', id: 'act-complete-28', server: 'tcs', tool: 'update_goal',
  status: 'completed', arguments: {status: 'complete'}}).text, 'update_goal · completed');
assert.equal(toolCall({type: 'mcpToolCall', id: 'act-busy-9', server: 'tcs', tool: 'team_assign', status: 'failed',
  arguments: {member: 1, assignment: 'busy check'}}).text, 'team_assign · failed');
""")

    def test_two_assignments_of_one_member_keep_their_own_rows(self):
        # The records the runner makes for two assignments of member 2, one after the other: each run numbers its
        # items from item_0 again.
        records = team_loop.member_records([(2, "Lemma A\nProve it."), (2, "Lemma B\nProve it.")])
        self.run_app([*coordinator.ACTIVITY, INGEST], coordinator.ACTIVITY_SETUP
                     + f"const detailRows = new Map();\nconst records = {json.dumps(records)};\n", r"""
ingest(records, true);
const rows = ui.timeline.children.map((row) => [row._card.label, row._card.text]);
assert.deepEqual(rows.filter(([label]) => label.startsWith('Subagent 2')), [
  ['Subagent 2 reasoning', 'About Lemma A.'], ['Subagent 2', 'Done: Lemma A'],
  ['Subagent 2 reasoning', 'About Lemma B.'], ['Subagent 2', 'Done: Lemma B']]);
assert.deepEqual(rows.filter(([label]) => label.startsWith('Author subagent 2')).map(([label]) => label), [
  'Author subagent 2 (GPT 6.1 Sol) started', 'Author subagent 2 (GPT 6.1 Sol) finished',
  'Author subagent 2 (GPT 6.1 Sol) started', 'Author subagent 2 (GPT 6.1 Sol) finished']);
// A later poll that brings the second assignment's events again replaces only its own rows.
ingest(records.filter((record) => record.subagentId === 'member-2-2'));
assert.equal(ui.timeline.children.filter((row) => row._card.label === 'Subagent 2').length, 2);
""")

    def test_a_model_without_web_search_is_a_pinned_warning_of_the_activity_log(self):
        record = no_web_search.warning_record()
        self.run_app([*coordinator.ACTIVITY, INGEST], coordinator.ACTIVITY_SETUP
                     + f"const detailRows = new Map();\nconst record = {json.dumps(record)};\n", r"""
const card = describe(record);
assert.deepEqual([card.type, card.label, card.pinned], ['error', 'No web search', true]);
assert.equal(card.text, record.text);
assert.match(card.text, /^Web search is on for this job, but the proof author's model has no web search on its connection: gpt-6.1-sol \(API connection "Lab gateway"\)\./);
// Kept with the prompts, results and errors when routine events leave the page's window.
assert.equal(importantEntry(record), true);
ingest([record], true);
assert.deepEqual(ui.timeline.children.map((row) => [row.dataset.type, row._card.label]), [['error', 'No web search']]);
""")

    def test_a_members_retry_notes_stay_pinned_only_for_the_first_two_attempts(self):
        # As on the server (test_team_server): through a long outage of a member's model, its retry notes and its
        # Claude Code's own retry notes stay pinned only for an assignment's first two attempts.
        records = team_loop.member_retry_records(failures=3, cli_retries=1)
        self.run_app([*coordinator.ACTIVITY, INGEST], coordinator.ACTIVITY_SETUP
                     + f"const detailRows = new Map();\nconst records = {json.dumps(records)};\n", r"""
const notes = (words) => records.filter((record) => record.kind === 'diagnostic' && record.text.includes(words))
  .map((record) => [record.attempt, importantEntry(record)]);
assert.deepEqual(notes('a model call failed'), [[1, true], [2, true], [3, false]]);
assert.deepEqual(notes('Claude API retry'), [[1, true], [2, true], [3, false], [4, false]]);
assert.ok(records.filter((record) => record.authorSubagent === true).every(importantEntry));
""")

    def test_goal_status_records_say_what_the_author_reported_and_what_happens_next(self):
        self.run_app(coordinator.ACTIVITY, coordinator.ACTIVITY_SETUP, r"""
const goal = (goalStatus, note, time = 't1') => describe({kind: 'status', stage: 'solve', node: 'author',
  label: 'Goal status', goalStatus, note, time});
assert.deepEqual([goal('blocked', 'Need the constant of Lemma 4.').label, goal('blocked', 'Need the constant of Lemma 4.').text],
  ['Goal status · blocked', 'Note: Need the constant of Lemma 4.\nTCS Prover does not stop for a blocked goal: it '
    + 'records it and sends the continuation.']);
// What a complete goal leads to is decided when the turn ends (goal_session): the line says when it is not the hand-in.
assert.deepEqual([goal('complete', '').label, goal('complete', '').text],
  ['Goal status · complete', "When this turn ends, TCS Prover hands its final reply on as the author's answer. A turn "
    + "without a final reply, and a coordinator's reply that names no proof file (kept as a report), get the "
    + "continuation instead."]);
assert.equal(goal('paused', '').text,
  'TCS Prover does not stop for a paused goal: it records it and sends the continuation.');
// Each call keeps its row.
assert.notEqual(goal('blocked', 'a', 't1').key, goal('blocked', 'b', 't2').key);
// Codex's own goal events keep their row.
assert.equal(describe({kind: 'codex_event', root: true, stage: 'solve', node: 'author', time: 't',
  event: {method: 'thread/goal/updated', params: {goal: {status: 'complete'}}}}).label, 'Goal status');
""")

    def test_setup_counts_each_subagents_program_like_a_roles(self):
        self.run_app(SETUP_SLICES, r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let composingProblem = true, currentJob = '';
const catalog = [{id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol', provider: 'codex'},
  {id: 'claude-opus-5-5', label: 'Claude Opus 5.5', provider: 'claude'},
  {id: 'api:lab--m-a', label: 'Model A', provider: 'api', format: 'openai', sourceLabel: 'Lab key'},
  {id: 'api:gate--m-c', label: 'Model C', provider: 'api', format: 'anthropic', sourceLabel: 'Gateway'}];
let state = {phase: 'input', workflow: {settings: {model_catalog: catalog}}};
const modelProvider = model => catalog.find(row => row.id === model)?.provider || '';
let stages = {review: false, author: true, critic: true, writing: true};
const selectedWorkflowStages = () => stages;
for (const role of ['review', 'author', 'critic', 'writer']) ui[`${role}Model`].value = 'gpt-6.1-sol';
let team = null;
const teamModelsSetting = (given) => (given.author ? team : null);
let presets = {presets: [], defaultId: ''}, setup = null;
const presetStore = () => presets;
const setupStore = () => ({data: setup});
""", r"""
const rows = () => setupFormRoles().map(({role, on, program, signIn, member}) => [role, on, program, signIn, member]);
assert.equal(setupFormRoles().length, 4);
team = [{model: 'claude-opus-5-5', effort: 'max'}, {model: 'api:lab--m-a', effort: 'high'},
  {model: 'api:gate--m-c', effort: 'high'}, {model: 'gpt-6.1-sol', effort: 'max'}];
assert.deepEqual(rows().slice(4), [['member1', true, 'claude', true, 1], ['member2', true, 'codex', false, 2],
  ['member3', true, 'claude', false, 3], ['member4', true, 'codex', true, 4]]);
// With the Author stage off the subagents need nothing.
stages = {...stages, author: false};
assert.equal(setupFormRoles().length, 4);
stages = {...stages, author: true};
// Setup's sentences name them by number, after the roles of the same model.
const groups = setupRoleGroups(setupFormRoles().filter(row => row.on));
assert.equal(groups, 'Proof author, Independent critic, LaTeX writer and Author subagent 4 (GPT 6.1 Sol); '
  + 'Author subagent 1 (Claude Opus 5.5); Author subagent 2 (API connection “Lab key”, no sign-in needed); '
  + 'Author subagent 3 (API connection “Gateway”, no sign-in needed)');
team = [{model: 'claude-opus-5-5', effort: 'max'}, {model: 'claude-opus-5-5', effort: 'high'}];
assert.equal(setupRoleGroups(setupFormRoles().filter(row => row.on)),
  'Proof author, Independent critic and LaTeX writer (GPT 6.1 Sol); Author subagents 1, 2 (Claude Opus 5.5)');
for (const role of ['review', 'author', 'critic', 'writer']) ui[`${role}Model`].value = 'claude-opus-5-5';
stages = {review: true, author: true, critic: true, writing: true};
assert.equal(setupRoleGroups(setupFormRoles().filter(row => row.on)),
  'all four roles and Author subagents 1, 2 (Claude Opus 5.5)');

// Your default saved settings' subagents count too (GET /setup lists only their roles), with the form closed.
composingProblem = false;
const defaultTeam = [{model: 'claude-opus-5-5', effort: 'max'}, {model: 'api:gate--m-c', effort: 'high'}];
const saved = (settings) => {
  presets = {presets: [{id: 'd', name: 'Team', labels: {}, settings: {workflowStages: {author: true},
    authorSubagents: 2, authorSubagentModels: defaultTeam, ...settings}}], defaultId: 'd'};
};
saved({});
assert.deepEqual(setupDefaultMembers().map(({role, program, signIn, member}) => [role, program, signIn, member]),
  [['member1', 'claude', true, 1], ['member2', 'claude', false, 2]]);
setup = {defaults: {source: 'saved', roles: [{role: 'author', on: true, program: 'codex', signIn: true, model: 'gpt-6.1-sol'}]},
  jobs: {}};
const needs = setupNeeds();
// With the form closed, what the default settings use is what needs each program.
assert.deepEqual([needs.claude.needed, needs.claude.signIn, needs.claude.roles.map((row) => row.role)],
  [true, true, ['member1', 'member2']]);
assert.deepEqual(needs.codex.roles.map((row) => row.role), ['author']);
for (const settings of [{workflowStages: {author: false}}, {authorSubagents: 0}, {authorSubagentModels: null}]) {
  saved(settings);
  assert.deepEqual(setupDefaultMembers(), [], JSON.stringify(settings));
  assert.equal(setupNeeds().claude.needed, false);
}
// Built-in default settings never count.
saved({});
setup.defaults.source = 'built-in';
assert.equal(setupNeeds().claude.needed, false);
""")


if __name__ == "__main__":
    unittest.main()
