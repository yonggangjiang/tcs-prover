"""A program nothing needs is never a warning (the owner: "not everyone has Claude").

Claude Code, the Codex CLI and LaTeX are a warning (a red or amber mark, the sidebar badge, the Overview notice, the
hints beside the role menus and Start) only when something needs them: the New problem form while it is open, the
saved default settings, or a job that can still run (in a stage still ahead: GET /setup, tested in
test_program_setup.py). The built-in settings, which nobody chose, never count, and neither does an API connection
alone. Otherwise the page shows them calmly: the same words with a hollow ring and no mark, "Nothing uses it now ·
only needed for Claude models", "Not signed in · Not needed by your settings or jobs", and no highlighted button.
Each surface is checked both ways.

The checks cut ui/app.js into slices by marker text and run them in Node's vm with the small fake DOM and fake fetch
of the Setup page tests. No server, process or network is involved.
"""
import json
import subprocess
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import test_program_setup_ui as setup_ui

APP, NODE = setup_ui.APP, setup_ui.NODE
SETUP, PAGES, OUTCOME, MODELS = setup_ui.SETUP, setup_ui.PAGES, setup_ui.OUTCOME, setup_ui.MODELS
JOB_PAGE = [*setup_ui.JOB_PAGE, "jobPageAttention"]
SOURCES = ["function shortModelName(label) {", "// Each role first picks a source, then sees only that source's models."]
RUN_SETTINGS = ["function roleModelSource(role, model) {", "// A saved job cannot change its models"]
NOTIFY = ["function attentionAlertsOn() {", "jobsUi.save.onclick = saveRunningJobs;"]
API_FORMATS = ["const apiFormats = {", "const roleNames = {"]
MODELS_META = ["  function renderModelsMeta() {", '  let lastRoute = "";']

# A Codex-only computer, and helpers to change it (each test changes one thing at a time).
FIXTURES = r"""
const ROLES = [['review', 'review'], ['author', 'author'], ['critic', 'critic'], ['writer', 'writing']];
const gpt = (role, stage) => ({role, stage, on: true, model: 'gpt-6.1-sol', modelLabel: 'GPT 6.1 Sol', program: 'codex',
  signIn: true, connection: ''});
const opus = (role, stage) => ({role, stage, on: true, model: 'claude-opus-5-5', modelLabel: 'Claude Opus 5.5',
  program: 'claude', signIn: true, connection: ''});
const claudeJob = () => ({count: 1, running: 0, signIn: 1, items: [{runId: 'c1', title: 'Online matching', running: false,
  batchId: '__root__', status: 'paused'}]});
const noJobs = () => ({count: 0, running: 0, signIn: 0, items: []});
// The Codex CLI installed and signed in, Claude Code not installed, LaTeX found, nothing saved (GET /setup then sends
// the built-in settings: GPT 6.1 Sol in every role, Writing & polishing on), one paused GPT job, and no API connection.
const quiet = (change = () => {}) => status((s) => {
  s.defaults = {source: 'built-in', name: '', roles: ROLES.map(([role, stage]) => gpt(role, stage))};
  s.jobs = {codex: {count: 1, running: 0, signIn: 1, items: [{runId: 'g1', title: 'Expanders', running: false,
    batchId: '__root__', status: 'paused'}]}, claude: noJobs(), latex: {count: 1, running: 0, items: []}};
  s.connections = {codex: [], claude: []};
  change(s);
  return s;
});
// Default settings you saved (still GPT 6.1 Sol in every role until a test changes a role).
const saved = (s) => Object.assign(s.defaults, {source: 'saved', name: 'My settings'});
// A Claude-only computer: Claude Code installed and signed in, the Codex CLI not installed, and no GPT job.
const claudeOnly = (s) => {
  const codex = s.programs.codex;
  s.programs.codex = {...JSON.parse(JSON.stringify(s.programs.claude)), name: 'Codex CLI'};
  s.programs.claude = {...codex, name: 'Claude Code'};
  s.jobs.codex = noJobs();
};
// Claude Code installed by Anthropic's installer, and signed out.
const signedOutClaude = (s) => {
  s.programs.claude = {...s.programs.codex, name: 'Claude Code', path: '/Users/u/.local/bin/claude',
    shownPath: '~/.local/bin/claude', kindText: "installed by Anthropic's installer", version: '2.1.289 (Claude Code)',
    signIn: {state: 'signed-out', text: '', method: ''},
    commands: [{group: 'sign-in', label: 'Sign in in a terminal', command: 'claude auth login'}],
    actions: {install: {shown: false, available: false, reason: '', dialog: null},
      update: {shown: false, available: false, reason: '', dialog: null},
      signIn: {shown: true, available: true, reason: '', label: 'Sign in', dialog: {title: 'Sign in to Claude Code',
        lede: '', options: [], what: [], warning: '', button: 'Start sign-in'}},
      useFolder: {shown: false, available: false, reason: '', folder: ''}}};
};
// LaTeX not found.
const noLatex = (s) => {
  s.programs.latex = {...s.programs.latex, state: 'missing', problem: 'latexmk and pdflatex are missing',
    tools: s.programs.latex.tools.map((tool) => ({...tool, path: '', shownPath: '', onPath: false, version: ''}))};
};
// LaTeX found, but pdflatex does not run.
const brokenLatex = (s) => {
  s.programs.latex = {...s.programs.latex, state: 'broken', problem: 'pdflatex --version ended with code 1'};
};
// No job uses LaTeX.
const noLatexJob = (s) => { s.jobs.latex = {count: 0, running: 0, items: []}; };
const CATALOG = [{id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol', provider: 'codex'},
  {id: 'claude-opus-5-5', label: 'Claude Opus 5.5', provider: 'claude'},
  {id: 'api:lab', label: 'Lab model', provider: 'api', format: 'anthropic', sourceLabel: 'Lab gateway', host: 'lab.invalid'}];
// The New problem form open in this tab, with these models and every stage on.
const openForm = (models) => {
  composingProblem = true;
  state = {phase: 'input', workflow: {settings: {model_catalog: CATALOG}}};
  for (const [role, model] of Object.entries(models)) ui[role + 'Model'].value = model;
  for (const stage of ['workflowReview', 'workflowAuthor', 'workflowCritic', 'workflowWriting']) ui[stage].checked = true;
};
let answer = quiet();
responder = (path) => ({status: 200, body: path === '/batches' ? {batches: []} : answer});
const load = async (next) => { answer = next; await loadSetup(true); await settle(); };
const card = (program) => setupEls().list.children.find((node) => node.dataset.program === program);
const pill = (program) => card(program).querySelector('.setup-pill');
const needList = (program) => card(program).querySelector('.setup-needs').children.map((node) => node.textContent);
const button = (program, text) => card(program).querySelectorAll('button').find((node) => node.textContent === text);
const signLine = (program) => card(program).querySelector('.setup-dd-status').children
  .map((node) => [node.className, node.textContent]);
// The sidebar badge as it reads ('' when hidden) and the Overview's Setup notice as [title, text] lines.
const badge = () => { renderSetupBadge(); return setupEls().badge.hidden ? '' : setupEls().badge.textContent; };
const notice = () => {
  renderSetupBanner();
  return setupEls().banner.hidden ? []
    : setupEls().bannerList.children.map((item) => [item.children[0].textContent, item.children[1].textContent]);
};
"""

# The page functions the Setup code and the role menus call, without the rest of the page.
CHIP_STUBS = r"""
const show = (element, visible) => { element.hidden = !visible; };
const clearJobView = () => {}, renderBatchNavigation = () => {}, openAttentionJob = () => {};
const selectedWorkflowStages = () => ({review: ui.workflowReview.checked, author: ui.workflowAuthor.checked,
  critic: ui.workflowCritic.checked, writing: ui.workflowWriting.checked});
const selectedRetrievalMode = () => 'live';
class Option extends Element {
  constructor(text = '', value = '') { super('option'); this.text = String(text); this.value = String(value); }
}
for (const role of ['review', 'author', 'critic', 'writer']) {
  for (const name of [role + 'Model', role + 'Source']) {
    Object.defineProperty(ui[name], 'options', {get() { return this.children; }});
  }
}
"""

# What the job surfaces need around the page's own slices: notifications that are on, and the Models & API meta line.
PAGE_STUBS = r"""
const refresh = async () => {};
const localDateTime = (value) => value;
const notes = [];
const Notification = class { constructor(title, options) { notes.push([title, options.body]); } };
Notification.permission = 'granted';
const localStorage = {getItem: () => '1', setItem() {}};
window.focus = () => {};
const route = () => 'models';
const setText = (node, text) => { node.textContent = text; };
const shell = {modelsMeta: document.createElement('p')};
const roleModelCatalog = () => CATALOG;
let apiModels = [], apiModelsLoaded = true;
"""


class CalmStyleTests(unittest.TestCase):
    def test_the_calm_state_has_a_hollow_neutral_ring_and_no_mark(self):
        css = (APP.parent / "styles.css").read_text(encoding="utf-8")
        rules = [line for line in css.splitlines() if "tone-idle" in line]
        self.assertEqual(rules, [
            ".setup-view .tone-idle { --tone: var(--stopped); }",
            ".setup-view .job-status.tone-idle::before { border: 1.5px solid currentColor; background: none; }",
            ".setup-card.tone-idle::before { content: none; }"])
        for rule in rules:
            self.assertNotRegex(rule, r"--(failed|waiting|paused)")


@unittest.skipUnless(NODE, "Node.js is needed for browser logic tests")
class QuietUnusedProgramTests(unittest.TestCase):
    def run_app(self, checks, slices=(SETUP,), stubs=setup_ui.STUBS):
        payload = json.dumps({"dom": setup_ui.DOM, "slices": [list(item) for item in slices],
                              "setup": setup_ui.BASE + stubs + setup_ui.STATUS + FIXTURES, "checks": checks})
        result = subprocess.run([NODE, "-e", setup_ui.RUNNER, str(APP)], input=payload, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_missing_program_is_calm_on_its_card_until_something_needs_it(self):
        self.run_app(r"""
settingUp = true;
await load(quiet());
// Nothing needs Claude Code: the page lists it, with Install, in the same words, calmly.
assert.equal(card('claude').className, 'setup-card card tone-idle');
assert.equal(pill('claude').className, 'job-status tone-idle setup-pill');
assert.equal(pill('claude').textContent, 'Not installed');
assert.deepEqual(needList('claude'), ['Nothing uses it now · only needed for Claude models']);
assert.equal(button('claude', 'Install').className, 'secondary compact');
assert.equal(button('claude', 'Install').disabled, false);
assert.equal(card('claude').querySelector('details.setup-commands').open, false);
assert(setupEls().meta.children.some((node) => node.textContent === 'Claude Code not installed'));
assert.equal(badge(), '');
assert.equal(setupEls().words.textContent, '');
assert.deepEqual(notice(), []);
// The default settings you saved have their critic on Claude Opus 5.5: the same Claude Code is a warning.
await load(quiet((s) => { saved(s); s.defaults.roles[2] = opus('critic', 'critic'); }));
assert.equal(card('claude').className, 'setup-card card tone-failed');
assert.equal(pill('claude').className, 'job-status tone-failed setup-pill');
assert.deepEqual(needList('claude'), ['New problem: Independent critic (Claude Opus 5.5)']);
assert.equal(button('claude', 'Install').className, 'primary compact');
assert.equal(badge(), '1');
assert.equal(setupEls().words.textContent, ', 1 program needs setting up');
assert.deepEqual(notice(), [['Claude Code not installed', 'New problems use it.']]);
// A job that can still run is enough.
await load(quiet((s) => { s.jobs.claude = claudeJob(); }));
assert.equal(card('claude').className, 'setup-card card tone-failed');
assert.equal(card('claude').querySelector('details.setup-jobs').open, true);
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['Claude Code not installed', '1 job uses it.']]);
// A Claude Code that does not run, or that jobs cannot find (Use it for jobs, no Install): calm until a job needs it,
// and the step that fixes it is highlighted only then.
for (const [state, words, tone] of [['broken', 'Does not run', 'failed'], ['offpath', 'Jobs cannot find it', 'waiting']]) {
  const found = (s) => {
    Object.assign(s.programs.claude, {state, path: '/opt/claude/bin/claude',
      shownPath: '/opt/claude/bin/claude', offPath: '/opt/claude/bin', shownOffPath: '/opt/claude/bin',
      problem: state === 'broken' ? 'claude --version ended with code 1' : ''});
    if (state === 'offpath') {
      s.programs.claude.actions.install = {shown: false, available: false, reason: '', dialog: null};
      s.programs.claude.actions.useFolder = {shown: true, available: true, reason: '', folder: '/opt/claude/bin'};
    }
  };
  const buttons = () => card('claude').querySelector('.setup-card-actions').querySelectorAll('button')
    .map((node) => [node.textContent, node.className]);
  await load(quiet(found));
  assert.equal(pill('claude').textContent, words);
  assert.equal(card('claude').className, 'setup-card card tone-idle', state);
  assert.equal(pill('claude').className, 'job-status tone-idle setup-pill', state);
  assert.deepEqual(needList('claude'), ['Nothing uses it now · only needed for Claude models']);
  assert.deepEqual(buttons(), state === 'offpath' ? [['Use it for jobs', 'secondary compact']]
    : [['Install', 'secondary compact']], state);
  assert.equal(badge(), '', state);
  assert.deepEqual(notice(), [], state);
  await load(quiet((s) => { found(s); s.jobs.claude = claudeJob(); }));
  assert.equal(card('claude').className, `setup-card card tone-${tone}`, state);
  assert.equal(pill('claude').className, `job-status tone-${tone} setup-pill`, state);
  // (Install is highlighted only for a program that is missing, as it was before.)
  assert.deepEqual(buttons(), state === 'offpath' ? [['Use it for jobs', 'primary compact']]
    : [['Install', 'secondary compact']], state);
  assert.equal(badge(), '1', state);
  assert.deepEqual(notice(), [[state === 'offpath' ? 'Jobs cannot find Claude Code' : 'Claude Code does not run',
    '1 job uses it.']], state);
}
// A saved API connection in Anthropic format that no role or job uses needs nothing either.
await load(quiet((s) => { s.connections.claude = ['Lab gateway']; }));
assert.equal(card('claude').className, 'setup-card card tone-idle');
assert.deepEqual(needList('claude'), ['API connections: “Lab gateway” (no sign-in needed)', 'No role or job uses it now']);
assert.equal(badge(), '');
// The other way round: a Claude-only computer without the Codex CLI. Nothing is saved: the built-in settings (GPT 6.1
// Sol), which nobody chose, never make the Codex CLI needed; neither do saved settings on Claude Opus 5.5.
await load(quiet(claudeOnly));
assert.equal(card('codex').className, 'setup-card card tone-idle');
assert.equal(pill('codex').className, 'job-status tone-idle setup-pill');
assert.deepEqual(needList('codex'), ['Nothing uses it now · only needed for GPT models']);
assert.equal(button('codex', 'Install').className, 'secondary compact');
assert.equal(badge(), '');
assert.deepEqual(notice(), []);
await load(quiet((s) => {
  claudeOnly(s); saved(s); s.defaults.roles = ROLES.map(([role, stage]) => opus(role, stage));
}));
assert.equal(card('codex').className, 'setup-card card tone-idle');
assert.equal(badge(), '');
// Saved settings on GPT 6.1 Sol need it.
await load(quiet((s) => { claudeOnly(s); saved(s); }));
assert.equal(card('codex').className, 'setup-card card tone-failed');
assert.deepEqual(needList('codex'), ['New problem: all four roles (GPT 6.1 Sol)']);
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['Codex CLI not installed', 'New problems use it.']]);
// LaTeX not found. Calm while nothing has Writing & polishing on: the built-in settings have it on, but nobody chose
// them, and saved settings may have it off.
for (const change of [() => {}, (s) => { saved(s); s.defaults.roles[3].on = false; }]) {
  await load(quiet((s) => { noLatex(s); noLatexJob(s); change(s); }));
  assert.equal(card('latex').className, 'setup-card card tone-idle');
  assert.equal(pill('latex').className, 'job-status tone-idle setup-pill');
  assert.equal(pill('latex').textContent, 'Not found');
  assert.deepEqual(needList('latex'), ['Nothing uses it now · only needed for Writing & polishing']);
  assert.equal(card('latex').querySelector('details.setup-commands').open, false);
  assert.equal(badge(), '');
  assert.deepEqual(notice(), []);
}
// A warning when a job or the saved settings have Writing & polishing on.
await load(quiet(noLatex));
assert.equal(card('latex').className, 'setup-card card tone-failed');
assert.equal(pill('latex').className, 'job-status tone-failed setup-pill');
assert.deepEqual(needList('latex'), ['1 job']);
assert.equal(card('latex').querySelector('details.setup-commands').open, true);
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['LaTeX not found', 'Jobs cannot make the PDF without it.']]);
await load(quiet((s) => { noLatex(s); noLatexJob(s); saved(s); }));
assert.equal(card('latex').className, 'setup-card card tone-failed');
assert.deepEqual(needList('latex'), ['New problem: Writing & polishing']);
assert.equal(badge(), '1');
// LaTeX that does not run: the same both ways, in its own words.
await load(quiet((s) => { brokenLatex(s); noLatexJob(s); }));
assert.equal(card('latex').className, 'setup-card card tone-idle');
assert.equal(pill('latex').className, 'job-status tone-idle setup-pill');
assert.equal(pill('latex').textContent, 'Does not run');
assert.deepEqual(needList('latex'), ['Nothing uses it now · only needed for Writing & polishing']);
assert.equal(card('latex').querySelector('details.setup-commands').open, false);
assert(setupEls().meta.children.some((node) => node.textContent === 'LaTeX does not run'));
assert.equal(badge(), '');
assert.deepEqual(notice(), []);
for (const change of [() => {}, (s) => { noLatexJob(s); saved(s); }]) {
  await load(quiet((s) => { brokenLatex(s); change(s); }));
  assert.equal(card('latex').className, 'setup-card card tone-failed');
  assert.equal(pill('latex').className, 'job-status tone-failed setup-pill');
  assert.equal(pill('latex').textContent, 'Does not run');
  assert.equal(card('latex').querySelector('details.setup-commands').open, true);
  assert.equal(badge(), '1');
  assert.deepEqual(notice(), [['LaTeX does not run', 'Jobs cannot make the PDF without it.']]);
}
""")

    def test_a_signed_out_program_is_calm_unless_something_uses_its_login(self):
        self.run_app(r"""
settingUp = true;
await load(quiet(signedOutClaude));
// Installed, signed out, and nothing uses Claude Code.
assert.equal(card('claude').className, 'setup-card card tone-done');
assert.deepEqual(signLine('claude'), [['job-status tone-idle', 'Not signed in'], ['', 'Not needed by your settings or jobs']]);
assert.equal(button('claude', 'Sign in').className, 'secondary compact');
assert.equal(badge(), '');
assert.deepEqual(notice(), []);
// The saved default settings' critic uses Claude Opus 5.5, which signs in through it: a warning.
await load(quiet((s) => { signedOutClaude(s); saved(s); s.defaults.roles[2] = opus('critic', 'critic'); }));
assert.equal(card('claude').className, 'setup-card card tone-waiting');
assert.deepEqual(signLine('claude'), [['job-status tone-waiting', 'Not signed in'], ['', 'New problems use your Claude login']]);
assert.equal(button('claude', 'Sign in').className, 'primary compact');
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['Claude Code not signed in', 'New problems use your Claude login through it.']]);
// A job alone.
await load(quiet((s) => { signedOutClaude(s); s.jobs.claude = claudeJob(); }));
assert.deepEqual(signLine('claude'), [['job-status tone-waiting', 'Not signed in'], ['', '1 job uses your Claude login']]);
assert.deepEqual(notice(), [['Claude Code not signed in', '1 job uses your Claude login through it.']]);
// The critic uses an Anthropic-format API connection: Claude Code is needed, its sign-in is not.
await load(quiet((s) => {
  signedOutClaude(s);
  saved(s);
  s.defaults.roles[2] = {...opus('critic', 'critic'), model: 'api:lab', modelLabel: 'Lab model', signIn: false,
    connection: 'Lab gateway'};
  s.connections.claude = ['Lab gateway'];
}));
assert.equal(card('claude').className, 'setup-card card tone-done');
assert.deepEqual(needList('claude'), [
  'New problem: Independent critic (API connection “Lab gateway”, no sign-in needed)',
  'API connections: “Lab gateway” (no sign-in needed)']);
assert.deepEqual(signLine('claude'), [['job-status tone-idle', 'Not signed in'], ['', 'Not needed: API connections need no sign-in.']]);
assert.equal(badge(), '');
// The Codex CLI signed out, with nothing saved and no job: the built-in settings' GPT 6.1 Sol never makes its login a
// warning, but the calm line says that new problems open with them (Claude Code's line above names nothing, as the
// built-in settings do not use it).
const signedOutCodex = (s) => {
  s.programs.codex.signIn = {state: 'signed-out', text: '', method: ''};
  s.programs.codex.actions.signIn.label = 'Sign in';
  s.jobs.codex = noJobs();
};
await load(quiet(signedOutCodex));
assert.equal(card('codex').className, 'setup-card card tone-done');
assert.deepEqual(signLine('codex'), [['job-status tone-idle', 'Not signed in'],
  ['', 'New problems open with the built-in settings, which use your ChatGPT login']]);
assert.equal(button('codex', 'Sign in').className, 'secondary compact');
assert.equal(badge(), '');
assert.deepEqual(notice(), []);
// The New problem form open on Claude Opus 5.5 in every role counts by itself: nothing here uses the login.
openForm({review: 'claude-opus-5-5', author: 'claude-opus-5-5', critic: 'claude-opus-5-5', writer: 'claude-opus-5-5'});
renderSetup();
assert.deepEqual(signLine('codex'), [['job-status tone-idle', 'Not signed in'], ['', 'Not needed by your settings or jobs']]);
composingProblem = false;
// Saved on GPT 6.1 Sol, the same default settings need it.
await load(quiet((s) => { signedOutCodex(s); saved(s); }));
assert.equal(card('codex').className, 'setup-card card tone-waiting');
assert.deepEqual(signLine('codex'), [['job-status tone-waiting', 'Not signed in'],
  ['', 'New problems use your ChatGPT login']]);
assert.equal(button('codex', 'Sign in').className, 'primary compact');
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['Codex CLI not signed in', 'New problems use your ChatGPT login through it.']]);
""")

    def test_the_form_the_default_settings_and_the_jobs_each_make_a_program_needed(self):
        self.run_app(r"""
settingUp = true;
// The saved default settings' critic uses Claude Opus 5.5; the form in this tab uses GPT 6.1 Sol everywhere.
openForm({review: 'gpt-6.1-sol', author: 'gpt-6.1-sol', critic: 'gpt-6.1-sol', writer: 'gpt-6.1-sol'});
await load(quiet((s) => { saved(s); s.defaults.roles[2] = opus('critic', 'critic'); }));
let needs = setupNeeds();
assert.deepEqual(needs.claude.roles, []);
assert.deepEqual(needs.claude.defaults.map((row) => row.role), ['critic']);
assert.equal(needs.claude.needed, true);
assert.equal(badge(), '1');  // The default settings still need it ...
assert.deepEqual(notice(), [['Claude Code not installed', 'New problems use it.']]);
renderSetupHints();
assert.equal(setupEls().modelHint.hidden, true);  // ... but the form's roles do not.
assert.equal(setupEls().startHint.hidden, true);
assert.deepEqual(needList('claude'), ['Default settings: Independent critic (Claude Opus 5.5)']);
assert.equal(card('claude').className, 'setup-card card tone-failed');
// The form explains the Codex CLI already: no second line for the default settings (then its one job).
assert.equal(needList('codex')[0], 'New problem: all four roles (GPT 6.1 Sol)');
assert.equal(needList('codex').filter((line) => line.startsWith('Default settings')).length, 0);
assert.equal(needList('codex').length, 2);
// The form's critic uses Claude Opus 5.5 too: one line, the form's.
ui.criticModel.value = 'claude-opus-5-5';
renderSetup();
assert.deepEqual(needList('claude'), ['New problem: Independent critic (Claude Opus 5.5)']);
renderSetupHints();
assert.equal(setupEls().modelHint.textContent, 'Claude Code is not installed on this computer; the independent critic uses it. Open Setup');
// Nothing saved and the form's critic on Claude Opus 5.5: needed while the form is open, and no longer once it is left.
await load(quiet());
assert.equal(badge(), '1');
assert.deepEqual(needList('claude'), ['New problem: Independent critic (Claude Opus 5.5)']);
composingProblem = false;
assert.equal(setupNeeds().claude.needed, false);
assert.equal(badge(), '');
renderSetup();
assert.equal(card('claude').className, 'setup-card card tone-idle');
// A Claude-only computer, nothing saved, the form on Claude Opus 5.5 in all four roles: the built-in settings (GPT 6.1
// Sol) are not counted beside the form, so the Codex CLI stays calm while you compose without it ...
const opusForm = {review: 'claude-opus-5-5', author: 'claude-opus-5-5', critic: 'claude-opus-5-5',
  writer: 'claude-opus-5-5'};
openForm(opusForm);
await load(quiet(claudeOnly));
assert.deepEqual(setupNeeds().codex.defaults, []);
assert.equal(card('codex').className, 'setup-card card tone-idle');
assert.deepEqual(needList('codex'), ['Nothing uses it now · only needed for GPT models']);
assert.equal(badge(), '');
assert.deepEqual(notice(), []);
renderSetupHints();
assert.equal(setupEls().startHint.hidden, true);
// ... until the form, or the settings you saved, use a GPT model.
ui.criticModel.value = 'gpt-6.1-sol';
renderSetup();
assert.equal(card('codex').className, 'setup-card card tone-failed');
assert.deepEqual(needList('codex'), ['New problem: Independent critic (GPT 6.1 Sol)']);
assert.equal(badge(), '1');
openForm(opusForm);
await load(quiet((s) => { claudeOnly(s); saved(s); }));
assert.equal(card('codex').className, 'setup-card card tone-failed');
assert.deepEqual(needList('codex'), ['Default settings: all four roles (GPT 6.1 Sol)']);
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['Codex CLI not installed', 'New problems use it.']]);
renderSetupHints();
assert.equal(setupEls().startHint.hidden, true);  // The form's roles do not use it.
// LaTeX not found, nothing saved, no job, the form with Writing & polishing off: calm; saved with it on: a warning.
const gptForm = {review: 'gpt-6.1-sol', author: 'gpt-6.1-sol', critic: 'gpt-6.1-sol', writer: 'gpt-6.1-sol'};
openForm(gptForm);
ui.workflowWriting.checked = false;
await load(quiet((s) => { noLatex(s); noLatexJob(s); }));
assert.equal(card('latex').className, 'setup-card card tone-idle');
assert.deepEqual(needList('latex'), ['Nothing uses it now · only needed for Writing & polishing']);
assert.equal(badge(), '');
assert.deepEqual(notice(), []);
await load(quiet((s) => { noLatex(s); noLatexJob(s); saved(s); }));
assert.equal(card('latex').className, 'setup-card card tone-failed');
assert.deepEqual(needList('latex'), ['Default settings: Writing & polishing']);
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['LaTeX not found', 'Jobs cannot make the PDF without it.']]);
// LaTeX that does not run, beside the form with Writing & polishing on: a warning, in its own words.
ui.workflowWriting.checked = true;
await load(quiet((s) => { brokenLatex(s); noLatexJob(s); }));
assert.equal(card('latex').className, 'setup-card card tone-failed');
assert.deepEqual(needList('latex'), ['New problem: Writing & polishing']);
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['LaTeX does not run', 'Jobs cannot make the PDF without it.']]);
renderSetupHints();
assert.equal(setupEls().modelHint.textContent,
  'LaTeX does not run, so Writing & polishing cannot make the PDF. Open Setup');
// Claude Code installed and signed out; the form's critic uses an Anthropic-format API connection (no sign-in), the
// saved default settings' critic Claude Opus 5.5 (a sign-in): the default settings are named for the sign-in.
openForm({review: 'gpt-6.1-sol', author: 'gpt-6.1-sol', critic: 'api:lab', writer: 'gpt-6.1-sol'});
await load(quiet((s) => {
  signedOutClaude(s); saved(s); s.defaults.roles[2] = opus('critic', 'critic'); s.connections.claude = ['Lab gateway'];
}));
assert.deepEqual(needList('claude'), ['New problem: Independent critic (API connection “Lab gateway”, no sign-in needed)',
  'Default settings: Independent critic (Claude Opus 5.5)', 'API connections: “Lab gateway” (no sign-in needed)']);
assert.deepEqual(signLine('claude'), [['job-status tone-waiting', 'Not signed in'], ['', 'Your default settings use your Claude login']]);
assert.equal(badge(), '1');
assert.deepEqual(notice(), [['Claude Code not signed in', 'New problems use your Claude login through it.']]);
renderSetupHints();
assert.equal(setupEls().modelHint.hidden, true);  // The form's critic signs in to nothing.
// The Install dialog offers the sign-in afterwards only when something uses that login.
await load(quiet());
composingProblem = false;
openSetupDialog('claude', 'install');
assert.equal(setupEls().dialogThen.hidden, true);
setupEls().dialog.close();
await load(quiet((s) => { s.jobs.claude = claudeJob(); }));
openSetupDialog('claude', 'install');
assert.equal(setupEls().dialogThen.hidden, false);
""")

    def test_role_chips_and_hints_name_only_a_chosen_model_and_the_source_menus_never_mark_a_source(self):
        self.run_app(r"""
openForm({});
const roles = ['review', 'author', 'critic', 'writer'];
const chips = () => roles.map((role) => [ui[role + 'ModelProvider'].textContent, ui[role + 'ModelProvider'].dataset.state]);
const sources = (role) => ui[role + 'Source'].options.map((option) => option.textContent);
const SOURCES = ['Codex CLI', 'Claude Code', 'API key · Lab gateway'];
await load(quiet());
for (const role of roles) syncRoleModelMenu(role, 'codex', 'gpt-6.1-sol');
renderModelChoices();
// Claude Code is not installed and no chosen model uses it: no chip, menu or hint mentions it.
assert.deepEqual(chips(), roles.map(() => ['Codex CLI', '']));
for (const role of roles) assert.deepEqual(sources(role), SOURCES);
assert.equal(setupEls().modelHint.hidden, true);
assert.equal(setupEls().startHint.hidden, true);
assert.equal(badge(), '');
// The critic's model is now Claude Opus 5.5: its chip and the hints say so, right there; the menus stay as they are.
syncRoleModelMenu('critic', 'claude', 'claude-opus-5-5');
renderModelChoices();
assert.deepEqual(chips(), [['Codex CLI', ''], ['Codex CLI', ''], ['Claude Code · not installed', 'warning'], ['Codex CLI', '']]);
for (const role of roles) assert.deepEqual(sources(role), SOURCES);
assert.equal(setupEls().modelHint.textContent, 'Claude Code is not installed on this computer; the independent critic uses it. Open Setup');
assert.equal(setupEls().startHint.textContent, 'Claude Code is not installed on this computer; the independent critic uses it. '
  + 'You can start now: the job retries every minute until it works. Open Setup');
assert.equal(badge(), '1');
// The Critic stage off (its role is hidden): no selected role uses Claude Code, so the hints and the badge are calm.
ui.workflowCritic.checked = false;
renderModelChoices();
assert.equal(setupEls().modelHint.hidden, true);
assert.equal(setupEls().startHint.hidden, true);
assert.equal(badge(), '');
// An Anthropic-format API model runs through Claude Code: chosen, it says so too.
ui.workflowCritic.checked = true;
syncRoleModelMenu('critic', 'api:lab', 'api:lab');
renderModelChoices();
assert.deepEqual(chips()[2], ['API · Anthropic format via Claude Code · lab.invalid · not installed', 'warning']);
""", slices=(MODELS, SOURCES, SETUP), stubs=CHIP_STUBS)

    def test_a_computer_with_neither_program_gets_one_plain_line_until_a_program_has_its_own(self):
        self.run_app(r"""
settingUp = true;
// Neither the Codex CLI nor Claude Code is installed, nothing is saved and no job uses either: no model can run, so
// one line says so, on the Overview, on Setup and as the badge; both cards stay calm, with Install not highlighted.
const neither = (s) => {
  s.programs.codex = {...JSON.parse(JSON.stringify(s.programs.claude)), name: 'Codex CLI'};
  s.jobs.codex = noJobs();
};
const line = ['Neither the Codex CLI nor Claude Code is installed', 'TCS Prover runs every model, API keys included, '
  + 'through one of them: install the Codex CLI for GPT models (the default) or Claude Code for Claude models.'];
const first = () => setupEls().first.hidden ? '' : setupEls().first.textContent;
await load(quiet(neither));
assert.equal(badge(), '1');
assert.equal(setupEls().words.textContent, ', 1 program needs setting up');
assert.deepEqual(notice(), [line]);
assert.equal(setupEls().bannerList.children[0].className, 'attention-item setup-item none');
assert.equal(setupEls().bannerList.children[0].children.at(-1).textContent, 'Open Setup');
assert.equal(first(), `${line[0]}. ${line[1]}`);
assert.equal(setupEls().first.children[0].tagName, 'STRONG');
for (const program of ['codex', 'claude']) {
  assert.equal(card(program).className, 'setup-card card tone-idle', program);
  assert.equal(button(program, 'Install').className, 'secondary compact', program);
}
assert.deepEqual(needList('codex'), ['Nothing uses it now · only needed for GPT models']);
// A job on Claude Opus 5.5: Claude Code's own line takes its place.
await load(quiet((s) => { neither(s); s.jobs.claude = claudeJob(); }));
assert.deepEqual(notice(), [['Claude Code not installed', '1 job uses it.']]);
assert.equal(badge(), '1');
assert.equal(first(), '');
assert.equal(card('codex').className, 'setup-card card tone-idle');
// The New problem form (the built-in GPT 6.1 Sol in every role): the Codex CLI's own line and hints.
openForm({review: 'gpt-6.1-sol', author: 'gpt-6.1-sol', critic: 'gpt-6.1-sol', writer: 'gpt-6.1-sol'});
await load(quiet(neither));
assert.deepEqual(notice(), [['Codex CLI not installed', 'New problems use it.']]);
assert.equal(first(), '');
renderSetupHints();
assert.equal(setupEls().modelHint.textContent, 'The Codex CLI is not installed on this computer; the statement reviewer, '
  + 'proof author, independent critic and LaTeX writer use it. Open Setup');
// Every stage of the form off: no role uses a program, so the one line is back, and the form adds no hint.
for (const stage of ['workflowReview', 'workflowAuthor', 'workflowCritic', 'workflowWriting']) ui[stage].checked = false;
renderSetup();
assert.deepEqual(notice(), [line]);
assert.equal(first(), `${line[0]}. ${line[1]}`);
renderSetupHints();
assert.equal(setupEls().modelHint.hidden, true);
assert.equal(setupEls().startHint.hidden, true);
composingProblem = false;
// One of them installed, or installed but not running, is not this case: calm as before.
await load(quiet());
assert.equal(badge(), '');
assert.equal(first(), '');
await load(quiet((s) => { neither(s); Object.assign(s.programs.codex, {state: 'broken', path: '/opt/codex/bin/codex',
  shownPath: '/opt/codex/bin/codex', problem: 'codex --version ended with code 1'}); }));
assert.equal(badge(), '');
assert.deepEqual(notice(), []);
assert.equal(first(), '');
// Install offers to sign in right after (under the same confirmation) on this computer for either program: the one
// installed first is the one whose models you will use.
const offered = async (program, answer) => {
  await load(answer);
  openSetupDialog(program, 'install');
  const shown = !setupEls().dialogThen.hidden;
  setupEls().dialog.close();
  return shown;
};
assert.equal(await offered('codex', quiet(neither)), true);
assert.equal(await offered('claude', quiet(neither)), true);
// A Claude-only computer with nothing saved: the built-in settings new problems open with sign in through the Codex
// CLI, so its install offers the sign-in; once settings on Claude Opus 5.5 are saved, or while the form uses only
// Claude Opus 5.5, nothing uses that login and the install does not offer it.
const opusDefaults = (s) => { saved(s); s.defaults.roles = ROLES.map(([role, stage]) => opus(role, stage)); };
assert.equal(await offered('codex', quiet(claudeOnly)), true);
assert.equal(await offered('codex', quiet((s) => { claudeOnly(s); opusDefaults(s); })), false);
openForm({review: 'claude-opus-5-5', author: 'claude-opus-5-5', critic: 'claude-opus-5-5', writer: 'claude-opus-5-5'});
assert.equal(await offered('codex', quiet(claudeOnly)), false);
composingProblem = false;
// Without a Setup page on the server the line is gone with the cards.
await load(quiet(neither));
setupStore().missing = true;
renderSetup();
assert.equal(setupEls().first.hidden, true);
assert.equal(badge(), '');
""")

    def test_job_surfaces_follow_the_jobs_and_the_other_pages_say_nothing_about_a_program_nobody_uses(self):
        self.run_app(r"""
// Claude Code is not installed and nothing needs it.
await load(quiet());
const emptyOverview = () => { ui.batchesList.dataset.signature = ''; renderBatches([]); return ui.batchesStatus.textContent; };
const calmOverview = emptyOverview();
assert.equal(calmOverview, 'No batches yet. Open Setup to check the programs your models need, then create a problem.');
// No job needs it: no line under Needs your attention, no line on a row, nothing on the job page, no notification.
const gptJob = {runId: 'g1', title: 'Expanders', phase: 'paused'};
renderAttention([{id: '__root__', attention: []}]);
assert.equal(ui.attentionBanner.hidden, true);
renderJobs([gptJob]);
assert.equal(ui.jobsList.children[0].querySelector('.job-attention'), null);
state = {needsAttention: null, readOnly: false};
jobPageAttention();
assert.equal(ui.runAttention.hidden, true);
notifyAttention([{id: '__root__', attention: []}]);
notifyAttention([{id: '__root__', attention: []}]);
assert.deepEqual(notes, []);
// Run settings and the Models & API page name programs, never whether they are installed.
state = {modelSources: {critic: {model: 'claude-opus-5-5', kind: 'claude', label: 'Claude Opus 5.5', source: 'Claude Code'}},
  workflow: {settings: {model_catalog: CATALOG}}};
const runSettings = roleModelText('critic', 'claude-opus-5-5', 'max');
assert.deepEqual(runSettings, ['Claude Opus 5.5 · max', "Source: Claude Code · this computer's Claude login"]);
renderModelsMeta();
const meta = shell.modelsMeta.textContent;
assert.equal(meta, '1 Codex CLI · 1 Claude Code · 0 API keys');
assert.equal(apiFormats.anthropic.help.includes('Claude Code must be installed on this computer (see Setup), but needs no login.'), true);
// A job whose Claude Code is missing (its model call failed three times): every job surface says so, with Setup.
await load(quiet((s) => { s.jobs.claude = claudeJob(); }));
assert.equal(emptyOverview(), calmOverview);  // The empty Overview reads the same either way.
const cli = {kind: 'cli', title: 'Claude Code not installed or not working', retrying: true,
  text: 'Claude Code missing or incompatible — retrying in 1 min (attempt 4).'};
renderAttention([{id: '__root__', attention: [{...cli, runId: 'c1', job: 'P1.md', batchId: '__root__'}]}]);
assert.equal(ui.attentionBanner.hidden, false);
assert.equal(ui.attentionList.children[0].children.at(-1).textContent, 'Open Setup');
renderJobs([{runId: 'c1', title: 'Online matching', phase: 'running', needsAttention: cli}]);
assert.equal(ui.jobsList.children[0].querySelector('.job-attention').textContent, `${cli.title}: ${cli.text} Open Setup`);
state = {needsAttention: cli, readOnly: false};
jobPageAttention();
assert.equal(ui.runAttention.hidden, false);
assert.equal(ui.runAttention.textContent, `${cli.title}: ${cli.text} Open Setup`);
notifyAttention([{id: '__root__', attention: [{...cli, runId: 'c1', job: 'P1.md'}]}]);
assert.deepEqual(notes, [['TCS Prover · Claude Code not installed or not working', `P1.md: ${cli.text}`]]);
// Run settings and the Models & API page are the same as before.
state = {modelSources: {critic: {model: 'claude-opus-5-5', kind: 'claude', label: 'Claude Opus 5.5', source: 'Claude Code'}},
  workflow: {settings: {model_catalog: CATALOG}}};
assert.deepEqual(roleModelText('critic', 'claude-opus-5-5', 'max'), runSettings);
renderModelsMeta();
assert.equal(shell.modelsMeta.textContent, meta);
""", slices=(PAGES, OUTCOME, RUN_SETTINGS, JOB_PAGE, NOTIFY, API_FORMATS, MODELS_META, SETUP), stubs=PAGE_STUBS)


if __name__ == "__main__":
    unittest.main()
