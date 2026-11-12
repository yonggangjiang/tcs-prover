"""The page in coordinator mode: the Author subagents field and Start's check of it, the Prompts dialog's
Coordinator tab, Run settings, the job page's Progress log and the activity log's rows for the author
coordinator's subagents, time checks and reports.

The browser checks cut ui/app.js into slices by marker text and run them in Node's vm with a small fake DOM, as
the other UI tests do, against job states shaped like the server's (build spec, section 3): authorMode
("coordinator" or "author"), authorSubagents, authorModeEffective, coordinatorProgress = {updatedAt, truncated,
chars, bytes, version} (null until memory/progress.md exists; chars: how many characters its text holds, bytes: the
whole file's size; the text itself comes from GET /progress-log when the version changes), and
the status records of the author's subagents (authorSubagent, subagentId, subagentTask, subagentState,
subagentsRunning), of its time checks ("Coordinator time check", elapsedSeconds) and of its replies that name no
proof file ("Coordinator report"). No server, model or CLI is involved.
"""
from pathlib import Path
import re
import shutil
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import test_audit_fixes_frontend as composer
from tests import test_conversation_ui as conversation

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"
INDEX = ROOT / "ui" / "index.html"

# The field's help, as the build spec words it.
SUBAGENTS_HELP = (
    "0: one author works on the proof by itself. 1 or more (default 3): the author is a coordinator: it does not "
    "prove the statement itself, but runs up to this many author subagents at a time, keeps the team's memory and "
    "checks that progress keeps growing; its progress log appears on the job page.")
SUBAGENTS_REFUSAL = ("Enter 0 to 10 author subagents: 0 for one author working alone, 1 or more for an author "
                     "coordinator with that many author subagents.")

# The stage switches with the Prompts dialog (its tabs, help and Apply checks) and the rerun's settings.
PROMPTS = [
    ["function selectedWorkflowStages()", "function selectedRetrievalMode()"],
    ["const promptLabels =", "function clockText("],
    ["async function rerunWithStatement(", "// The New problem page, adding to"],
]
PROMPTS_SETUP = r"""
const defaults = {review: 'Review.', author: 'Author. [STATEMENT]', coordinator: 'Coordinate {author_subagents}. [STATEMENT]',
  critic: 'Critic {count}.', convention: 'Convention {statement}.', final: 'Final.'};
let state = {phase: 'input', workflow: {settings: {prompts: {...defaults}}}};
let currentJob = '', currentBatch = '', activePrompt = 'review', composerSeed = null;
let promptValues = {}, promptDrafts = {}, promptOriginals = {}, promptOverrides = {};
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
ui.authorSubagents.value = '3';
ui.promptDialog.showModal = function () { this.open = true; };
ui.promptDialog.close = function () { this.open = false; };
const show = (element, visible) => { element.hidden = !visible; };
const updateModelSummary = () => {}, renderRetrievalSettings = () => {};
const jobPath = path => path;
const request = async () => ({workflow: state.workflow});
const selectedBatch = () => null;
let opened = 0;
const newProblem = async () => { opened += 1; };
const stages = (author = true) => ({review: true, author, critic: true, writing: true});
"""

# Run settings: the job header and its rows (as tests/test_run_overview.py runs them).
OVERVIEW = [
    ["function retrievalConfiguration(", "function renderJobs("],
    ["function completedRunLabel(", "function renderWorkflow()"],
]
OVERVIEW_SETUP = r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const document = {createElement: tag => new Element(tag)};
const show = (element, visible) => { element.hidden = !visible; };
const clockText = () => '', describe = () => null;
let stateReceivedAt = Date.now();
const job = {runId: 'saved-run', phase: 'running', stage: 'solve', activeNode: 'author', statement: 'S.',
  processAlive: true, workerActive: true, activeElapsedSeconds: 180, thinkingHours: 0,
  workflow: {nodes: {author: {label: 'Proof author'}}}, trace: [],
  workflowStages: {review: false, author: true, critic: true, writing: true},
  authorModel: 'gpt-6.1-sol', authorEffort: 'max', criticModel: 'gpt-6.1-sol', criticEffort: 'high',
  criticRounds: 2, criticCount: 3};
let state = {...job};
const proofAuthor = (changes) => {
  state = {...job, ...changes};
  renderRunOverview();
  const row = ui.runMetadata.children.find(item => item.children[0].textContent === 'Proof author');
  return row.children[1].textContent.split(String.fromCharCode(10))[0];
};
"""

# The progress log alone (its function and the job it was last drawn for).
PROGRESS = [["// The author coordinator's progress log, for a job recorded", "// The running author's open question"],
            ["// A byte count as the server writes it (related_works.size_text)",
             "// Where the author stands for a chat message"]]
PROGRESS_SETUP = r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const document = {createElement: tag => { throw new Error('The progress log never builds elements: ' + tag); }};
const show = (element, visible) => { element.hidden = !visible; };
// A box as tall as its lines (20 px each) with room for five of them.
const box = ui.progressLogText;
Object.defineProperty(box, 'scrollHeight', {get() { return 20 * String(this.textContent).split('\n').length; }});
box.clientHeight = 100;
const lines = (count, word = 'Line') => Array.from({length: count}, (_, index) => `${word} ${index + 1}`).join('\n');
const coordinatorJob = {runId: 'r1', batchId: '__root__', phase: 'running', authorMode: 'coordinator',
  authorSubagents: 3, workflowStages: {review: false, author: true, critic: true, writing: true},
  goalWorkspace: '/Users/me/runs/r1', coordinatorProgress: null};
let state = {...coordinatorJob};
const now = () => Math.floor(Date.now() / 1000);
const shown = () => ({card: !ui.progressLog.hidden, meta: ui.progressLogMeta.hidden ? '' : ui.progressLogMeta.textContent,
  text: ui.progressLogText.hidden ? null : ui.progressLogText.textContent, empty: !ui.progressLogEmpty.hidden,
  note: ui.progressLogNote.hidden ? '' : ui.progressLogNote.textContent});
// The server: /state names the log's version (coordinatorProgress, without the text); /progress-log gives the text.
let served = {text: null, version: null}, fetched = [], failing = false, held = null;
const jobPath = path => `${path}?job=${state.runId}`;
// With ``held`` (a list), each answer waits there until the test releases it.
const request = async path => {
  fetched.push(path);
  if (failing) throw new Error('offline');
  const answer = served;
  return held ? new Promise(resolve => held.push(() => resolve(answer))) : answer;
};
const settle = () => new Promise(resolve => setImmediate(() => setImmediate(resolve)));
const draw = async (changes = {}) => { state = {...state, ...changes}; renderProgressLog(); await settle(); return shown(); };
const progress = (text, extra = {}) => {
  const log = {updatedAt: now() - 240, truncated: false, chars: text.length, bytes: new TextEncoder().encode(text).length,
    version: 'v' + text.length + ':' + text.slice(-24), ...extra};
  served = {...log, text};
  return {coordinatorProgress: log};
};
"""

# The activity log: pinned kinds, the page's window, the rows and their bound.
ACTIVITY = [
    ["const pinnedKinds = new Set([", "// Serialize requests so a slow poll"],
    ["// Preserve prompts, results, errors, and completed root-model answers.", "// Convert one raw record"],
    ["function describe(entry)", "// Keep only exact application prompts"],
]
ACTIVITY_SETUP = r"""
let state = {reasoningSummary: 'concise'}, activeFilter = 'all';
const timelineRows = new Map();
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const document = {createElement: tag => new Element(tag), createTextNode: text => String(text)};
const clockText = () => '';
const subagent = (status, extra = {}) => ({kind: 'status', stage: 'solve', node: 'author',
  label: 'Author subagent ' + status, authorSubagent: true, subagentId: '019a-hubble',
  subagentTask: 'Prove Lemma 3 by induction on n', subagentState: status, time: '2026-11-12T10:00:00Z', ...extra});
const routine = index => ({kind: 'codex_event', stage: 'solve', node: 'author', time: 't' + index,
  event: {method: 'item/started', params: {item: {id: 'i' + index, type: 'commandExecution', command: 'ls'}}}});
"""


class CoordinatorMarkupTests(unittest.TestCase):
    """ui/index.html: the field, the Prompts dialog's tabs and the Progress log card."""

    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")

    def test_author_subagents_defaults_to_three_and_says_what_each_value_does(self):
        field = re.search(r'<input id="authorSubagents"[^>]*>', self.html).group(0)
        for attribute in ('type="number"', 'min="0"', 'max="10"', 'step="1"', 'value="3"'):
            self.assertIn(attribute, field)
        text = re.search(r'<p id="authorSubagentsHelp"[^>]*>(.*?)</p>', self.html, re.S).group(1)
        self.assertEqual(" ".join(text.split()), SUBAGENTS_HELP)

    def test_the_prompts_dialog_has_a_coordinator_tab_after_the_author_tab(self):
        tabs = self.html[self.html.index('id="promptTabs"'):self.html.index('id="promptPanel"')]
        self.assertEqual(re.findall(r'data-prompt="(\w+)"', tabs),
                         ["review", "author", "coordinator", "critic", "convention", "final"])
        tab = re.search(r'<button id="coordinatorPromptTab"[^>]*>([^<]*)</button>', tabs)
        self.assertEqual(tab.group(1), "Coordinator")
        self.assertIn('role="tab"', tab.group(0))

    def test_the_progress_log_follows_the_status_and_comes_before_the_chat(self):
        start = self.html.index('<div class="run-now card">')
        order = [self.html.index(marker, start) for marker in
                 ('id="runActivityAge"', 'id="jobRecovery"', 'id="progressLog"', 'id="authorConversation"')]
        self.assertEqual(order, sorted(order))
        begin = self.html.index('<section id="progressLog"')
        section = self.html[begin:self.html.index("</section>", begin)]
        self.assertIn(" hidden>", section.split("\n")[0])
        self.assertIn('<h3 id="progressLogHeading">Progress log</h3>', section)
        self.assertIn(">The coordinator has not written its progress log yet.</p>", section)
        # A box that scrolls must take the keyboard's focus to be scrolled without a mouse.
        box = re.search(r'<pre id="progressLogText"[^>]*>', section).group(0)
        self.assertIn('tabindex="0"', box)
        self.assertIn('aria-labelledby="progressLogHeading"', box)

    def test_the_progress_log_is_never_markup(self):
        # Its text comes from the model: app.js sets it as text, and builds no element for it.
        source = APP.read_text(encoding="utf-8")
        start = source.index("function renderProgressLog()")
        body = source[start:source.index("\n}\n", start)]
        self.assertIn("box.textContent = text", body)
        self.assertNotRegex(body, r"innerHTML|outerHTML|insertAdjacentHTML|createElement|DOMParser")
        self.assertNotIn("innerHTML", source)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class CoordinatorBrowserTests(unittest.TestCase):
    run_app = conversation.ConversationBrowserTests.run_app
    run_composer = composer.FrontendAuditFixTests.run_app

    def test_markers_occur_once(self):
        source = APP.read_text(encoding="utf-8")
        for marker in sorted({marker for pair in PROMPTS + OVERVIEW + PROGRESS + ACTIVITY for marker in pair}):
            with self.subTest(marker=marker):
                self.assertEqual(source.count(marker), 1)

    def test_start_and_run_a_folder_refuse_an_empty_or_invalid_author_subagents_field(self):
        # Number("") is 0: an empty field would turn coordinator mode off without a word.
        self.run_composer(composer.COMPOSER, composer.COMPOSER_SETUP, r"""
advanced.append(ui.authorSubagents);
for (const value of ['', '   ', '2.5', '-1', '11', 'three']) {
  Object.assign(ui.authorSubagents, {value, focused: false});
  advanced.open = false; ui.notice.hidden = true;
  await startReview('Prove S.');
  assert.equal(sent.length, 0, JSON.stringify(value));
  assert.deepEqual([ui.notice.hidden, ui.notice.textContent], [false, %(refusal)r], JSON.stringify(value));
  assert.equal(advanced.open, true, 'the closed Advanced settings open on the field');
  assert.equal(ui.authorSubagents.focused, true);
  assert.equal(ui.check.disabled, false);
}
// Run a folder… refuses it before the folder is read,
let read = 0;
await startFolderBatch([{...file('statements/a.md', 'Prove A.'), text: async () => { read += 1; return 'Prove A.'; }}]);
assert.deepEqual([read, sent.length], [0, 0]);
// and again once the folder is read, if the field was emptied meanwhile.
ui.authorSubagents.value = '3';
let release;
const slow = {name: 'b.md', webkitRelativePath: 'statements/b.md', size: 8,
  text: () => new Promise(resolve => { release = () => resolve('Prove B.'); })};
const folderRun = startFolderBatch([slow]);
await tick();
ui.authorSubagents.value = '';
ui.notice.textContent = '';
release();
await folderRun;
assert.deepEqual([sent.length, questions.length, ui.notice.textContent], [0, 0, %(refusal)r]);

// Without the Author stage the hidden field is not checked.
chooseStages({review: true, author: false, critic: false, writing: false});
const reviewOnly = startReview('Prove U.');
assert.equal(sent.length, 1);
sent[0].resolve({runId: 'job-1', phase: 'reviewing'});
await reviewOnly;
// A whole number from 0 to 10 goes as it is: 0 runs the author alone, 1 or more an author coordinator.
for (const [value, count] of [['0', 0], [' 3 ', 3], ['10', 10]]) {
  state = {phase: 'input'}; currentJob = '';
  chooseStages();
  ui.authorSubagents.value = value;
  const start = startReview('Prove V.');
  assert.equal(sent.at(-1).body.authorSubagents, count, value);
  sent.at(-1).resolve({runId: 'job-' + sent.length, phase: 'reviewing'});
  await start;
}
""" % {"refusal": SUBAGENTS_REFUSAL})

    def test_the_coordinator_tab_shows_with_the_author_stage_and_one_or_more_author_subagents(self):
        self.run_app(PROMPTS, PROMPTS_SETUP, r"""
setWorkflowStages(stages());
assert.equal(ui.coordinatorPromptTab.hidden, false);
for (const value of ['0', '', '1.5', 'many']) {
  ui.authorSubagents.value = value;
  showCoordinatorPromptTab();
  assert.equal(ui.coordinatorPromptTab.hidden, true, JSON.stringify(value));
}
ui.authorSubagents.value = '1';
showCoordinatorPromptTab();
assert.equal(ui.coordinatorPromptTab.hidden, false);
setWorkflowStages(stages(false));
assert.equal(ui.coordinatorPromptTab.hidden, true, 'the Author stage is off');
setWorkflowStages(stages());
assert.equal(ui.coordinatorPromptTab.hidden, false);
// A server without the coordinator prompt (an older one, which would refuse the edit) shows no tab.
delete state.workflow.settings.prompts.coordinator;
showCoordinatorPromptTab();
assert.equal(ui.coordinatorPromptTab.hidden, true);
""")
        # Editing the field updates the tab at once.
        source = APP.read_text(encoding="utf-8")
        self.assertIn("ui.authorSubagents.oninput = () => showCoordinatorPromptTab();", source)

    def test_the_coordinator_prompt_is_explained_checked_and_kept_for_its_job(self):
        self.run_app(PROMPTS, PROMPTS_SETUP, r"""
setWorkflowStages(stages());
await openPromptEditor();
assert.equal(ui.promptDialog.open, true);
assert.equal(promptDrafts.coordinator, defaults.coordinator);
selectPrompt('coordinator');
assert.equal(ui.promptEditorLabel.textContent, 'Coordinator prompt — current default');
assert.equal(ui.promptEditorHelp.textContent, 'This prompt replaces the Author prompt for coordinator jobs (Author '
  + 'subagents 1 or more): the author coordinator follows it to run the author subagents instead of proving the '
  + 'statement itself. Keep exactly one [STATEMENT], which the workflow replaces with your statement. '
  + '{author_subagents} is the number of author subagents; write any other brace twice: {{ and }}. A model without a '
  + 'subagent tool works alone with the Author prompt instead. New jobs load defaults from the workflow files; '
  + 'browser edits are not remembered.');
// The Author tab says when it still applies; with 0 author subagents it is the author's only prompt.
selectPrompt('author');
assert(ui.promptEditorHelp.textContent.includes(' With Author subagents 1 or more the author coordinator follows the '
  + "Coordinator prompt instead; this one is used when the author's model has no subagent tool."));
ui.authorSubagents.value = '0';
selectPrompt('author');
assert.doesNotMatch(ui.promptEditorHelp.textContent, /Coordinator/);
ui.authorSubagents.value = '3';
// Apply checks it as the server does: one [STATEMENT], {author_subagents} and doubled braces only.
const marker = 'The coordinator prompt must contain exactly one [STATEMENT].';
const braces = 'Write {author_subagents} where the number of author subagents goes, and write any other brace '
  + 'twice: {{ and }}.';
for (const [text, message] of [['  ', 'Every role prompt must contain instructions.'], ['No marker.', marker],
    ['[STATEMENT] and [STATEMENT]', marker], ['Run {count} of them. [STATEMENT]', braces],
    ['The set {x : x > 0}. [STATEMENT]', braces], ['A lone } brace. [STATEMENT]', braces]]) {
  selectPrompt('coordinator');
  ui.promptEditor.value = text;
  selectPrompt('review');
  savePrompts();
  assert.deepEqual([ui.promptDialog.open, activePrompt, ui.promptError.hidden, ui.promptError.textContent],
    [true, 'coordinator', false, message], text);
}
const edited = 'Run up to {author_subagents} of them; the set {{x : x > 0}} is open. [STATEMENT]';
ui.promptEditor.value = edited;
savePrompts();
assert.equal(ui.promptDialog.open, false);
assert.deepEqual(promptOverrides, {coordinator: edited});

// A coordinator job shows the coordinator prompt saved with it; a job saved without one shows the default.
syncPrompts({...state, runId: 'coordinated', coordinatorPrompt: 'Saved coordinator. [STATEMENT]'});
assert.equal(promptValues.coordinator, 'Saved coordinator. [STATEMENT]');
syncPrompts({...state, runId: 'older'});
assert.equal(promptValues.coordinator, defaults.coordinator);
// Rerun with suggested statement takes the job's coordinator prompt and its number of author subagents along.
state = {...state, runId: 'coordinated', authorSubagents: 3, authorMode: 'coordinator',
  coordinatorPrompt: 'Saved coordinator. [STATEMENT]'};
await rerunWithStatement('A clearer statement.');
assert.equal(opened, 1);
assert.deepEqual([composerSeed.settings.authorSubagents, composerSeed.settings.coordinatorPrompt],
  [3, 'Saved coordinator. [STATEMENT]']);
""")

    def test_run_settings_name_the_author_coordinator_or_say_the_author_works_alone(self):
        self.run_app(OVERVIEW, OVERVIEW_SETUP, r"""
const model = 'gpt-6.1-sol · max';
for (const [changes, expected] of [
  [{authorMode: 'coordinator', authorSubagents: 3}, 'coordinator, up to 3 author subagents at a time'],
  [{authorMode: 'coordinator', authorSubagents: 1}, 'coordinator, up to 1 author subagent at a time'],
  [{authorMode: 'author', authorSubagents: 0}, 'works alone'],
  [{authorMode: 'coordinator', authorSubagents: 0}, 'works alone'],
  // A coordinator job whose runner said that its model has no subagent tool.
  [{authorMode: 'coordinator', authorSubagents: 3, authorModeEffective: {mode: 'author', modelLabel: 'GPT 5.6 Luna (older)'}},
    'works alone: GPT 5.6 Luna (older) has no subagent tool (coordinator mode needs one)'],
  // A job recorded without the mode (every job from before coordinator mode) keeps its earlier wording.
  [{authorMode: 'author', authorSubagents: 2}, 'up to 2 subagents at a time'],
  [{authorSubagents: 1}, 'up to 1 subagent at a time'],
  [{}, 'works alone'],
]) {
  assert.equal(proofAuthor(changes), `${model} · ${expected}`, JSON.stringify(changes));
}
// A job from before the Author subagents setting is not described with it.
assert.equal(proofAuthor({authorSubagentsRecorded: false, authorSubagents: 3, authorMode: 'coordinator'}), model);
assert.equal(proofAuthor({authorMode: 'coordinator', authorSubagents: 3,
  workflowStages: {review: true, author: false, critic: true, writing: true}}), 'Off');
""")

    def test_the_workflow_panel_says_that_the_author_coordinates(self):
        self.run_app([["// Group the recorded critic calls", "function renderCriticReviewHistory()"],
                      ["function completedRunLabel(", "function localDateTime("],
                      ["function renderWorkflow()", "function renderClock()"]], r"""
const ui = {workflowNodes: new Element('ol')};
const document = {createElement: tag => new Element(tag)};
const nodeFromStage = () => '';
const node = label => ({label, description: label});
let state = {};
const author = (changes) => {
  state = {runId: 'r', phase: 'running', stage: 'solve', activeNode: 'author', skipStatementReview: true, trace: [],
    workflow: {nodes: {author: node('Proof author'), critic: node('Independent critic')}}, ...changes};
  ui.workflowNodes.replaceChildren();
  renderWorkflow();
  const row = [ui.workflowNodes, ...ui.workflowNodes.descendants()].find(item => item.dataset.node === 'author');
  return row.querySelector('.node-copy').children.find(item => item.tagName === 'SPAN' && !item.className).textContent;
};
""", r"""
assert.equal(author({authorMode: 'coordinator', authorSubagents: 3}),
  'Coordinates up to 3 author subagents and checks that progress keeps growing.');
assert.equal(author({authorMode: 'coordinator', authorSubagents: 1}),
  'Coordinates up to 1 author subagent and checks that progress keeps growing.');
// Every other job, and the New problem page (no job yet), keep the author's description.
for (const changes of [{authorMode: 'author', authorSubagents: 2}, {authorMode: 'author', authorSubagents: 0},
    {authorSubagents: 3}, {authorMode: 'coordinator', authorSubagents: 0},
    {authorMode: 'coordinator', authorSubagents: 3, authorModeEffective: {mode: 'author', modelLabel: 'GPT 5.6 Luna'}},
    {runId: '', authorMode: 'coordinator', authorSubagents: 3}]) {
  assert.equal(author(changes), 'Explore approaches and build a proof.', JSON.stringify(changes));
}
""")

    def test_the_progress_log_shows_the_coordinators_file_as_plain_text_and_follows_its_end(self):
        self.run_app(PROGRESS, PROGRESS_SETUP, r"""
// Before the file exists: the card with its empty state, nothing else, and nothing fetched.
assert.deepEqual(await draw(), {card: true, meta: '', text: null, empty: true, note: ''});
assert.deepEqual(fetched, []);
// The text exactly as the file has it: markup in it stays text. /state names its version; the page fetches it.
const first = '2026-11-12 10:00 · 1 h 0 min — <b>Lemma 1</b> & <script>alert(1)</script> verified\n' + lines(9);
assert.deepEqual(await draw(progress(first)), {card: true,
  meta: 'Written by the author coordinator in memory/progress.md · updated 4 min ago', text: first, empty: false,
  note: ''});
assert.deepEqual(fetched, ['/progress-log?job=r1']);
// It opens at the end of the log.
assert.equal(box.scrollTop, 200);
// A reader who scrolled up keeps their place while the log grows ...
box.scrollTop = 20;
await draw(progress(first + '\n' + lines(5, 'More')));
assert.equal(box.scrollTop, 20);
// ... and one at the end follows it.
box.scrollTop = 200;
await draw(progress(first + '\n' + lines(10, 'More')));
assert.equal(box.scrollTop, 400);
// An unchanged log is not fetched again (the polls of a running job) and leaves the box alone.
box.scrollTop = 150;
fetched = [];
for (let poll = 0; poll < 3; poll += 1) await draw({...state});
assert.equal(box.scrollTop, 150);
assert.deepEqual(fetched, []);
// Another job opens at the end of its own log.
assert.equal((await draw({...coordinatorJob, runId: 'r2', ...progress(lines(7))})).text, lines(7));
assert.equal(box.scrollTop, 140);
// A shortened file says so, how large the whole file is and where it is: the server's chars is how many characters
// the text holds (the last 100,000 at most), its bytes the size of the whole file.
assert.equal((await draw(progress(lines(3), {truncated: true, chars: 100000, bytes: 412345}))).note,
  'Showing the last 100,000 characters; the whole file (402.7 KB) is /Users/me/runs/r1/memory/progress.md.');
assert.equal((await draw({goalWorkspace: 'C:\\runs\\r1\\'})).note,
  'Showing the last 100,000 characters; the whole file (402.7 KB) is C:\\runs\\r1\\memory\\progress.md.');
// Fewer when the file held characters the page never shows (control characters are removed).
assert.equal((await draw(progress(lines(3), {truncated: true, chars: 99950, bytes: 2500000}))).note,
  'Showing the last 99,950 characters; the whole file (2.4 MB) is C:\\runs\\r1\\memory\\progress.md.');
assert.equal((await draw({goalWorkspace: '', ...progress(lines(3), {truncated: true, chars: undefined, bytes: undefined})})).note,
  "Showing the last 20 characters; the whole file is memory/progress.md in the author's working folder.");
// When it was last changed: epoch seconds or an ISO time; none known, none said.
const ago = async (updatedAt) => (await draw(progress('Log', {updatedAt}))).meta;
assert.equal(await ago(now()), 'Written by the author coordinator in memory/progress.md · updated just now');
assert.equal(await ago(new Date(Date.now() - 125 * 60000).toISOString()),
  'Written by the author coordinator in memory/progress.md · updated 2 h 5 min ago');
assert.equal(await ago(now() - 3600), 'Written by the author coordinator in memory/progress.md · updated 1 h ago');
assert.equal(await ago(now() - 3 * 86400), 'Written by the author coordinator in memory/progress.md · updated 3 days ago');
assert.equal(await ago(undefined), 'Written by the author coordinator in memory/progress.md');
// An empty file has not been written yet either.
assert.deepEqual(await draw(progress('  \n')), {card: true,
  meta: 'Written by the author coordinator in memory/progress.md · updated 4 min ago', text: null, empty: true, note: ''});
// Opening the job page again fetches its text again, the copy staying on screen meanwhile; a version that changed
// is fetched once, and one whose fetch failed is tried again 10 s later, the text on screen kept.
await draw(progress('Log'));
await draw({authorMode: 'author'});  // Another page (no card).
fetched = [];
state = {...state, authorMode: 'coordinator', ...progress('Log')};
renderProgressLog();
assert.equal(ui.progressLogText.textContent, 'Log');
await settle();
assert.deepEqual(fetched, ['/progress-log?job=r2']);
failing = true;
fetched = [];
const later = progress('Log\nMore');
assert.equal((await draw(later)).text, 'Log');
assert.equal((await draw({...state})).text, 'Log');
assert.deepEqual(fetched, ['/progress-log?job=r2']);
failing = false;
const realNow = Date.now;
Date.now = () => realNow() + 11000;
assert.equal((await draw({...state})).text, 'Log\nMore');
Date.now = realNow;
// A fetch that answers for another job than the one now on screen shows nothing of it, and never replaces that
// job's text.
fetched = [];
held = [];
state = {...state, ...progress('Job r2 only.')};
renderProgressLog();
state = {...state, runId: 'r4', ...progress('Job r4.')};
renderProgressLog();
const [r2Answer, r4Answer] = held;
held = null;
r4Answer();
await settle();
assert.equal(shown().text, 'Job r4.');
r2Answer();
await settle();
renderProgressLog();
assert.equal(shown().text, 'Job r4.');
assert.deepEqual(fetched, ['/progress-log?job=r2', '/progress-log?job=r4']);  // Nothing to fetch again.
// Only a coordinator job with the Author stage has the card.
for (const changes of [{authorMode: 'author'}, {authorMode: undefined},
    {authorMode: 'coordinator', workflowStages: {review: true, author: false, critic: true, writing: true}}]) {
  assert.equal((await draw({...coordinatorJob, ...progress('Log'), ...changes})).card, false, JSON.stringify(changes));
}
""")

    def test_render_draws_the_progress_log_of_a_coordinator_job(self):
        self.run_app(conversation.RENDER, conversation.RENDER_SETUP, r"""
const coordinator = {...author, phase: 'running', authorMode: 'coordinator', authorSubagents: 3,
  coordinatorProgress: {updatedAt: Math.floor(Date.now() / 1000), truncated: false, chars: 17, version: 'v1'}};
request = async path => (path.startsWith('/progress-log') ? {text: 'Lemma 1 verified.', version: 'v1'} : {});
render(coordinator);
await new Promise(resolve => setImmediate(() => setImmediate(resolve)));
assert.deepEqual([ui.progressLog.hidden, ui.progressLogText.textContent], [false, 'Lemma 1 verified.']);
render({...coordinator, authorMode: 'author', authorSubagents: 0, coordinatorProgress: null});
assert.equal(ui.progressLog.hidden, true);
""")

    def test_a_form_without_the_setting_takes_the_servers_default_else_three(self):
        # A source without Author subagents (a job saved before the setting, saved settings without it) shows the
        # server's default, and without one 3, an author coordinator; the review page does the same.
        self.run_app(conversation.RENDER, conversation.RENDER_SETUP, r"""
const settings = (value) => ({settings: value === undefined ? {} : {author_subagents: {default: value, minimum: 0, maximum: 10}}});
for (const [source, workflow, shown] of [[{}, settings(3), 3], [{}, settings(0), 0], [{}, settings(), 3],
    [{authorSubagents: 0}, settings(3), 0], [{authorSubagents: 5}, settings(), 5]]) {
  state = {workflow};
  applyComposerSettings(source);
  assert.equal(Number(ui.authorSubagents.value), shown, JSON.stringify([source, workflow]));
}
const review = {statement: 'S.', notes: ''};
for (const [job, shown] of [[{workflow: settings()}, 3], [{workflow: settings(0)}, 0], [{authorSubagents: 2, workflow: settings()}, 2]]) {
  previousPhase = '';
  render({...author, phase: 'reviewed', review, ...job});
  assert.equal(Number(ui.authorSubagents.value), shown, JSON.stringify(job));
}
""")

    def test_the_activity_log_shows_the_author_coordinators_records_as_tidy_lines(self):
        self.run_app(ACTIVITY, ACTIVITY_SETUP, r"""
const row = entry => { const card = describe(entry); return [card.label, card.text]; };
// Its subagents: started with the assignment, finished, failed with the reason; how many run now.
assert.deepEqual(row(subagent('started', {subagentsRunning: 2, text: 'An author subagent started.'})),
  ['Author subagent started', 'Prove Lemma 3 by induction on n · 2 running']);
assert.deepEqual(row(subagent('finished', {subagentsRunning: 1})),
  ['Author subagent finished', 'Prove Lemma 3 by induction on n · 1 running']);
assert.deepEqual(row(subagent('failed', {subagentsRunning: 0, text: "The model's usage limit was reached."})),
  ['Author subagent failed', "The model's usage limit was reached. · Prove Lemma 3 by induction on n · none running"]);
assert.deepEqual(row(subagent('failed', {text: ''})), ['Author subagent failed', 'Prove Lemma 3 by induction on n']);
// Without its assignment the subagent's id names it; without a recorded state the label says it.
assert.deepEqual(row(subagent('started', {subagentTask: '', subagentId: 'toolu_01'})),
  ['Author subagent started', 'Subagent toolu_01']);
assert.deepEqual(row(subagent('finished', {subagentState: undefined})),
  ['Author subagent finished', 'Prove Lemma 3 by induction on n']);
// Pinned on the page as on the server, each in a row of its own.
const started = subagent('started'), finished = subagent('finished');
assert.equal(describe(started).pinned, true);
assert.equal(importantEntry(started), true);
assert.notEqual(describe(started).key, describe(finished).key);
assert.notEqual(describe(started).key, describe(subagent('started', {subagentId: 'other'})).key);
const kept = retainTrace([started, ...Array.from({length: 1600}, (_, index) => routine(index)), finished]);
assert.equal(kept.length, 1501);
assert.deepEqual([kept[0], kept.at(-1)], [started, finished]);
// The subagents' own events keep a window of their own, as on the server: the newest 300 of them, which never push
// the job's own records out of its 1,500, in their order.
const subagentEvent = index => ({kind: 'codex_event', stage: 'solve', node: 'author', root: false, time: 's' + index,
  event: {method: 'item/completed', params: {item: {id: 's' + index, type: 'reasoning', summary: ['Step.']}}}});
const own = Array.from({length: 1400}, (_, index) => routine(index));
const theirs = Array.from({length: 1600}, (_, index) => subagentEvent(index));
const mixed = theirs.flatMap((event, index) => (index < own.length ? [own[index], event] : [event]));
const newest = new Set(theirs.slice(-300));
assert.deepEqual(retainTrace(mixed), mixed.filter(entry => entry.root !== false || newest.has(entry)));
const busy = retainTrace([...Array.from({length: 1600}, (_, index) => routine(index)), ...theirs.slice(0, 10)]);
assert.deepEqual([busy.filter(entry => entry.root !== false).length, busy.filter(entry => entry.root === false).length],
  [1500, 10]);
// A subagent's own record (root false) is no such line.
assert.equal(importantEntry({...started, root: false}), false);
assert.equal(describe({...started, root: false}).pinned, undefined);
// The routine rows' bound never drops them.
upsertTimeline(describe(started));
for (let index = 0; index < 310; index += 1) upsertTimeline(describe(routine(index)));
assert.equal(ui.timeline.children.filter(item => item.dataset.key === describe(started).key).length, 1);
assert.equal(ui.timeline.children.length, 301);

// The hourly time check, named with the job's working time when it was sent; each keeps its row.
const check = (elapsedSeconds, time) => ({kind: 'status', stage: 'solve', node: 'author', label: 'Coordinator time check',
  elapsedSeconds, time, text: '[TIME CHECK FROM TCS PROVER]\nElapsed: 5 h 12 min. No time limit.'});
assert.deepEqual(row(check(18720, 'a')), ['Author coordinator time check · 5 h 12 min',
  '[TIME CHECK FROM TCS PROVER]\nElapsed: 5 h 12 min. No time limit.']);
assert.equal(describe(check(2700, 'b')).label, 'Author coordinator time check · 45 min');
assert.equal(describe(check(3600, 'c')).label, 'Author coordinator time check · 1 h');
assert.equal(describe({...check(undefined, 'd'), text: ''}).label, 'Author coordinator time check');
assert.equal(describe({...check(undefined, 'd'), text: ''}).text, 'TCS Prover sent the author coordinator its working time so far.');
assert.notEqual(describe(check(3600, 'c')).key, describe(check(7200, 'e')).key);
// A reply that names no proof file: why it did not go to the critic, and where the reply is (not its text again).
const report = {kind: 'status', stage: 'solve', node: 'author', label: 'Coordinator report', time: 'f',
  text: 'Lemma 3 is verified; the team continues on Lemma 4.'};
assert.deepEqual(row(report), ['Author coordinator report', 'Not handed to the critic: this reply names no proof '
  + 'file, so TCS Prover asked the coordinator to continue. The reply is the Author row above.']);
assert.notEqual(describe(report).key, describe({...report, time: 'g'}).key);
// Both are pinned as on the server: they stay in the page's window and among its rows, however busy the log.
for (const entry of [check(3600, 'c'), report]) {
  assert.equal(importantEntry(entry), true, entry.label);
  assert.equal(describe(entry).pinned, true, entry.label);
  assert.equal(importantEntry({...entry, root: false}), false);
  assert.deepEqual(retainTrace([entry, ...Array.from({length: 1600}, (_, index) => routine(index))])[0], entry);
}
assert.equal(importantEntry({...report, stage: 'conversation'}), false);
// The critic's own progress keeps its rows.
assert.equal(describe({kind: 'status', label: 'Independent critic 1 started', subagentProgress: true, auditIndex: 1,
  time: 'h', text: 'x'}).label, 'Independent critic 1 started');
assert.equal(describe({kind: 'status', label: 'Goal status', time: 'i', text: 'active'}).key, 'status:Goal status');
""")


if __name__ == "__main__":
    unittest.main()
