"""Related works in the browser: the composer's list (uploads, notes, the start and folder-run
refusals and request bodies), the run page's panel and its add flow, and the batch page's folder runs.

Like the other UI tests, these cut ui/app.js into slices by marker text and run them in Node's vm
with a fake DOM (tests/test_conversation_ui.py has the runner). The server is replaced by stubs
shaped like its routes: POST /uploads?name= (raw bytes), /uploads/note, /uploads/discard,
/related-works/add?job=, GET /batch-related-works?batch= and POST /batch-related-works/add?batch=.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import test_conversation_ui as conversation

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"
INDEX = ROOT / "ui" / "index.html"

# The composer: stages, startReview with the folder run, and the related-works span.
COMPOSER = [
    ["function selectedWorkflowStages(", "// Read historical mode names"],
    ["async function startReview(", "async function act("],
]
COMPOSER_SETUP = r"""
let currentJob = '', currentBatch = '', timer = 0, jobsTimer = 0, reviewPending = false;
let state = {phase: 'input'};
let promptValues = {review: 'Review.', author: 'Author.'}, promptOverrides = {};
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
Object.assign(ui.thinkingHours, {value: '0', max: ''});
ui.criticRounds.value = '2';
ui.check.textContent = 'Start workflow';
const chooseStages = (values = {}) => {
  for (const [stage, control] of Object.entries({review: ui.workflowReview, author: ui.workflowAuthor,
    critic: ui.workflowCritic, writing: ui.workflowWriting})) control.checked = values[stage] ?? true;
};
chooseStages({review: false});
const show = (element, visible) => { element.hidden = !visible; };
const validateRetrievalSettings = () => true, syncPrompts = () => {};
const selectedRetrievalSettings = () => ({retrievalMode: 'live', retrievalCutoffUtc: ''});
const jobPath = (path, values = {}, runId = currentJob) => path + (runId ? '?job=' + runId : '');
const sent = [], adopted = [], rendered = [], questions = [];
const request = (path, body) => new Promise((resolve, reject) => sent.push({path, body, resolve, reject}));
const adoptJob = next => { adopted.push(next.runId); currentJob = next.runId; };
const render = next => { rendered.push(next); state = next; };
const refresh = () => {};
const setTimeout = () => 1, clearTimeout = () => {};
const confirm = question => { questions.push(question); return true; };
const renderJobs = () => {}, loadJobs = () => {}, renderBatchNavigation = () => {};
const batchUrl = id => '/?batch=' + id, history = {pushState() {}};
const document = {createElement: tag => new Element(tag)};
const sessionToken = 'secret';
// Uploads go through fetch, one file per request; each waits until the check answers it.
const uploads = [];
const fetch = (path, options) => new Promise(resolve => uploads.push({path, options,
  answer: (status, data) => resolve({ok: status < 300, status, json: async () => data})}));
const file = (name, size, content = 'x') => ({name, size, webkitRelativePath: 'statements/' + name, text: async () => content});
const staged = (token, name, extra = {}) => ({upload: {token, kind: 'file', name, originalName: name, title: '',
  size: 1234, sizeText: '1.2 KB', sha256: '0'.repeat(64), mediaType: '', text: null, textProblem: '', textStatus: '', ...extra}});
const tick = () => new Promise(resolve => setImmediate(resolve));
const until = async (check) => { for (let i = 0; i < 50 && !check(); i += 1) await tick(); assert(check()); };
const rows = () => ui.composerRelatedList.children.map(row => [row.dataset.state,
  row.children[1].children.map(node => node.textContent).join(' | ')]);
"""

# The run page's panel: render() and the related-works span, and the add flows of the run page.
PANEL = conversation.RENDER + [
    ["// Related works on the run page and on the batch page.", "// API models (Advanced settings"],
]
PANEL_SETUP = conversation.RENDER_SETUP + r"""
Element.prototype.addEventListener = function () {};
const renderAuthorLimitHint = () => {}, selectedBatch = () => batches.find(batch => batch.id === currentBatch);
let batches = [{id: '__root__', current: true}];
const tick = () => new Promise(resolve => setImmediate(resolve));
const sent = [];
let answers = {};
const request = async (path, body) => {
  sent.push({path, body});
  const answer = answers[path.split('?')[0]];
  const value = typeof answer === 'function' ? answer(path, body) : answer;
  if (value instanceof Error) throw value;
  return value ?? {};
};
const uploads = [];
let uploadAnswer = () => [200, {upload: {token: 'tok-' + uploads.length}}];
const fetch = async (path, options) => {
  uploads.push({path, options});
  const [status, data] = uploadAnswer(path, options);
  return {ok: status < 300, status, json: async () => data};
};
const file = (name, size = 100) => ({name, size});
const works = (items, extra = {}) => ({available: true, reason: '', count: items.length, items, ...extra});
const pdf = {name: 'My-Paper.pdf', originalName: 'My Paper.pdf', kind: 'file', title: '', sizeText: '1.2 MB',
  textStatus: 'text extracted (12 pages)', addedAt: '2026-10-27T09:00:00+00:00', batch: ''};
const note = {name: 'Plan.md', originalName: '', kind: 'note', title: 'Plan', sizeText: '2.0 KB', textStatus: '',
  addedAt: '2026-10-27T09:01:00+00:00', batch: ''};
const rowText = list => list.children.map(row => [row.children[0].textContent,
  ...row.children[1].children.map(node => node.textContent)]);
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class RelatedWorksBrowserTests(unittest.TestCase):
    run_app = conversation.ConversationBrowserTests.run_app

    def test_markers_occur_once(self):
        source = APP.read_text(encoding="utf-8")
        for marker in sorted({marker for pair in COMPOSER + PANEL for marker in pair}):
            with self.subTest(marker=marker):
                self.assertEqual(source.count(marker), 1)

    def test_composer_uploads_files_at_once_and_starts_only_with_finished_uploads(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
// Nothing listed: the request is the same as before.
let start = startReview('Prove P.');
assert.equal(sent.length, 1);
assert.equal('relatedWorks' in sent[0].body, false);
sent[0].resolve({runId: 'job-1', phase: 'running'});
await start;
currentJob = ''; state = {phase: 'input'};
// Each file uploads at once, with the launch token, as raw bytes named in the query.
const big = file('huge.pdf', 70 * 1024 * 1024), paper = file('My Paper.pdf', 5000);
const adding = addComposerFiles([paper, file('empty.md', 0), big]);
assert.deepEqual(rows(), [['uploading', 'My Paper.pdf | uploading…'],
  ['failed', 'empty.md | Upload failed: empty.md is empty.'],
  ['failed', 'huge.pdf | Upload failed: huge.pdf is 70.0 MB; the limit is 64 MB per file.']]);
await until(() => uploads.length === 1);
assert.equal(uploads[0].path, '/uploads?name=My%20Paper.pdf');
assert.equal(uploads[0].options.method, 'POST');
assert.equal(uploads[0].options.body, paper);
assert.deepEqual(uploads[0].options.headers, {'X-TCS-Prover-Token': 'secret', 'Content-Type': 'application/octet-stream'});
// Start waits for the upload, then for the failed rows to be removed.
await startReview('Prove P.');
assert.equal(ui.notice.textContent, 'Wait for the related works to finish uploading.');
assert.equal(sent.length, 1);
uploads[0].answer(200, staged('tok-paper', 'My-Paper.pdf', {mediaType: 'application/pdf',
  textStatus: 'text extracted (3 pages)', originalName: 'My Paper.pdf'}));
await adding;
assert.deepEqual(rows()[0], ['ready', 'My Paper.pdf | 1.2 KB · text extracted (3 pages)']);
await startReview('Prove P.');
assert.equal(ui.notice.textContent, 'Remove the related works that could not be uploaded, then start.');
const remove = index => ui.composerRelatedList.children[index].children[2];
assert.equal(remove(1).attributes['aria-label'], 'Remove empty.md');
remove(2).onclick(); remove(1).onclick();
assert.deepEqual(sent.slice(1), [], 'a failed upload has nothing to discard');
// Related works need the Author stage.
chooseStages({review: false, author: false});
await startReview('Prove P.');
assert.equal(ui.notice.textContent, 'Related works are read by the proof author. Turn on the Author stage or remove them.');
chooseStages({review: false});
start = startReview('Prove P.');
assert.deepEqual(sent[1].body.relatedWorks, ['tok-paper']);
sent[1].resolve({runId: 'job-2', phase: 'running'});
await start;
assert.equal(ui.composerRelatedList.children.length, 0, 'a successful start clears the list');
assert.equal(sent.length, 2, 'the uploads now belong to the job: nothing is discarded');
// A retried review (from its review page) never takes the composer's list.
currentJob = 'job-2'; state = {phase: 'reviewed', workflowStages: {review: true, author: true, critic: true, writing: true}};
const more = addComposerFiles([file('b.tex', 10)]);
await until(() => uploads.length === 2);
uploads[1].answer(200, staged('tok-b', 'b.tex'));
await more;
start = startReview('Revised.', 'Use n.');
assert.equal('relatedWorks' in sent[2].body, false);
sent[2].resolve({runId: 'job-3', phase: 'reviewing'});
await start;
// Removing a staged upload discards it on the server; one removed while uploading is discarded later.
currentJob = ''; state = {phase: 'input'};
ui.composerRelatedList.children[0].children[2].onclick();
assert.deepEqual(sent.at(-1).path, '/uploads/discard');
assert.deepEqual(sent.at(-1).body, {token: 'tok-b'});
const late = addComposerFiles([file('late.pdf', 10)]);
await until(() => uploads.length === 3);
ui.composerRelatedList.children[0].children[2].onclick();
uploads[2].answer(200, staged('tok-late', 'late.pdf'));
await late;
assert.deepEqual(sent.at(-1).body, {token: 'tok-late'});
assert.equal(ui.composerRelatedList.children.length, 0);
// The server's refusal is the row's reason.
const refused = addComposerFiles([file('scan.pdf', 10)]);
await until(() => uploads.length === 4);
uploads[3].answer(413, {error: 'This file is 80.0 MB; the limit is 64 MB per file.'});
await refused;
assert.deepEqual(rows(), [['failed', 'scan.pdf | Upload failed: This file is 80.0 MB; the limit is 64 MB per file.']]);
// Leaving the composer drops the list and discards what was staged.
ui.composerRelatedList.children[0].children[2].onclick();
const kept = addComposerFiles([file('kept.md', 10)]);
await until(() => uploads.length === 5);
uploads[4].answer(200, staged('tok-kept', 'kept.md'));
await kept;
clearComposerRelatedWorks(true);
assert.deepEqual(sent.at(-1).body, {token: 'tok-kept'});
assert.equal(ui.composerRelatedList.children.length, 0);
""")

    def test_a_folder_run_takes_the_related_works_to_its_batch(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
const adding = addComposerFiles([file('survey.pdf', 10)]);
await until(() => uploads.length === 1);
// Refused while the upload runs (before any folder is read).
await startFolderBatch([file('a.md', 8, 'Prove A.')]);
assert.equal(ui.notice.textContent, 'Wait for the related works to finish uploading.');
assert.equal(questions.length, 0);
uploads[0].answer(200, staged('tok-survey', 'survey.pdf'));
await adding;
const run = startFolderBatch([file('a.md', 8, 'Prove A.'), file('b.md', 8, 'Prove B.')]);
await until(() => sent.length === 1);
assert.match(questions[0], /Each job uses the selected stages\. Every job gets the 1 related work listed above\./);
assert.equal(sent[0].path, '/direct-batch');
assert.deepEqual(sent[0].body.relatedWorks, ['tok-survey']);
assert.deepEqual(sent[0].body.statements.map(item => item.name), ['a.md', 'b.md']);
sent[0].resolve({batchId: '__root__', startedJobs: ['j1', 'j2'], jobs: []});
await run;
assert.equal(ui.composerRelatedList.children.length, 0);
// Without related works the folder run's body is unchanged.
const plain = startFolderBatch([file('c.md', 8, 'Prove C.')]);
await until(() => sent.length === 2);
assert.equal('relatedWorks' in sent[1].body, false);
assert.doesNotMatch(questions[1], /related work/);
sent[1].resolve({batchId: '__root__', startedJobs: ['j3'], jobs: []});
await plain;
""")

    def test_a_composer_note_is_staged_and_a_refusal_keeps_its_text(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
ui.composerNoteText.value = '   ';
await addComposerNote();
assert.equal(ui.notice.textContent, "Enter the note's text.");
assert.equal(sent.length, 0);
ui.composerNoteTitle.value = ' Plan ';
ui.composerNoteText.value = 'Try induction on n.';
ui.composerNoteForm.hidden = false;
let adding = addComposerNote();
assert.deepEqual(sent[0].path, '/uploads/note');
assert.deepEqual(sent[0].body, {title: 'Plan', text: 'Try induction on n.'});
// While the note is staged, Start waits.
await startReview('Prove P.');
assert.equal(ui.notice.textContent, 'Wait for the related works to finish uploading.');
sent[0].reject(new Error('Keep a note at or below 2,000,000 characters.'));
await adding;
assert.equal(ui.notice.textContent, 'Keep a note at or below 2,000,000 characters.');
assert.equal(ui.composerNoteText.value, 'Try induction on n.');
assert.equal(ui.composerNoteForm.hidden, false);
assert.equal(ui.composerRelatedList.children.length, 0);
adding = addComposerNote();
sent[1].resolve({upload: {token: 'tok-note', kind: 'note', name: 'Plan.md', originalName: '', title: 'Plan', size: 30,
  sizeText: '30 B', sha256: '0'.repeat(64), mediaType: '', text: null, textProblem: '', textStatus: ''}});
await adding;
// The row names the saved file the author sees; the title is in the detail line.
assert.deepEqual(rows(), [['ready', 'Plan.md | note “Plan” · 30 B']]);
assert.equal(ui.composerRelatedList.children[0].children[0].textContent, 'NOTE');
assert.equal(ui.composerRelatedList.children[0].children[2].attributes['aria-label'], 'Remove Plan.md');
assert.equal(ui.composerNoteText.value, '');
assert.equal(ui.composerNoteForm.hidden, true);
const start = startReview('Prove P.');
assert.deepEqual(sent[2].body.relatedWorks, ['tok-note']);
sent[2].resolve({runId: 'job-1', phase: 'running'});
await start;
""")

    def test_the_composer_field_follows_the_author_stage(self):
        self.run_app(COMPOSER, COMPOSER_SETUP, r"""
const help = 'Papers, drafts or notes the proof author can read while it works. Text is extracted from PDFs for the '
  + 'author. Up to 64 MB per file. With Run a folder…, every problem of the folder run gets them.';
renderComposerRelatedWorks();
assert.equal(ui.relatedWorksField.hidden, false);
assert.equal(ui.relatedWorksHelp.textContent, help);
assert.equal(ui.composerRelatedList.hidden, true);
chooseStages({review: false, author: false});
renderComposerRelatedWorks();
assert.equal(ui.relatedWorksField.hidden, true);
const adding = addComposerFiles([file('a.pdf', 10)]);
assert.equal(ui.relatedWorksField.hidden, false);
assert.equal(ui.relatedWorksHelp.textContent,
  'Related works are read by the proof author; turn on the Author stage, or remove them.');
await until(() => uploads.length === 1);
uploads[0].answer(200, staged('tok-a', 'a.pdf'));
await adding;
chooseStages({review: false});
renderComposerRelatedWorks();
assert.equal(ui.relatedWorksHelp.textContent, help);
""")

    def test_the_run_page_panel_lists_related_works_by_phase(self):
        self.run_app(PANEL, PANEL_SETUP, r"""
const running = {...author, phase: 'running', goalThreadId: 'thread-1'};
// Shown when the job can take related works, with none yet.
render({...author, phase: 'prepared', relatedWorks: works([])});
assert.equal(ui.relatedWorksPanel.hidden, false);
assert.equal(ui.relatedWorksHeading.textContent, 'Related works');
assert.equal(ui.relatedWorksNote.textContent, "The author's first prompt points to these files.");
assert.equal(ui.relatedWorksEmpty.hidden, false);
assert.equal(ui.relatedWorksActions.hidden, false);
render({...running, relatedWorks: works([pdf, note, {...pdf, name: 'shared.pdf', originalName: 'shared.pdf', batch: 'b_1'}])});
assert.equal(ui.relatedWorksHeading.textContent, 'Related works · 3');
assert.equal(ui.relatedWorksNote.textContent,
  'Saved in the author\'s related-works folder. The author is told about new files at its next step.');
assert.equal(ui.relatedWorksEmpty.hidden, true);
assert.deepEqual(rowText(ui.relatedWorksList), [
  ['PDF', 'My-Paper.pdf', '1.2 MB · text extracted (12 pages) · uploaded as “My Paper.pdf”'],
  ['NOTE', 'Plan.md', 'note “Plan” · 2.0 KB'],
  ['PDF', 'shared.pdf', '1.2 MB · text extracted (12 pages) · from the batch']]);
assert(ui.relatedWorksList.children.every(row => row.children.length === 2), 'nothing can be removed');
for (const phase of ['pausing', 'paused']) {
  render({...running, phase, relatedWorks: works([pdf])});
  assert.equal(ui.relatedWorksNote.textContent, 'Saved in the author\'s related-works folder. The author is told '
    + 'about new files with your next message or when the run resumes.');
}
// A job of a folder run says where works for the whole run are added.
render({...running, folderBatch: 'b_0123456789', relatedWorks: works([pdf])});
assert.equal(ui.relatedWorksNote.textContent, 'Saved in the author\'s related-works folder. The author is told '
  + 'about new files at its next step. Works for every problem of this folder run are added under Batch related works '
  + 'on the batch page.');
// Finished: the list stays, with the reason and no actions; nothing at all without any.
render({...running, phase: 'done', relatedWorks: works([pdf], {available: false,
  reason: 'This job has finished; continue it to add related works.'})});
assert.equal(ui.relatedWorksPanel.hidden, false);
assert.equal(ui.relatedWorksNote.textContent, 'This job has finished; continue it to add related works.');
assert.equal(ui.relatedWorksActions.hidden, true);
render({...running, phase: 'done', relatedWorks: works([], {available: false, reason: 'Finished.'})});
assert.equal(ui.relatedWorksPanel.hidden, true);
// A read-only job lists its works and offers no action.
render({...running, readOnly: true, relatedWorks: works([pdf])});
assert.equal(ui.relatedWorksPanel.hidden, false);
assert.equal(ui.relatedWorksActions.hidden, true);
render({...running, runningElsewhere: 'Another process runs this job.', relatedWorks: works([pdf])});
assert.equal(ui.relatedWorksActions.hidden, true);
""")

    def test_the_run_page_uploads_each_file_then_adds_them_in_one_step(self):
        self.run_app(PANEL, PANEL_SETUP, r"""
render({...author, relatedWorks: works([])});
const statuses = [];
Object.defineProperty(ui.relatedWorksStatus, 'textContent', {get() { return statuses.at(-1) ?? ''; },
  set(value) { statuses.push(value); }});
answers['/related-works/add'] = (path, body) => ({...author, relatedWorks: works(body.uploads.map(token => ({...pdf, name: token})))});
uploadAnswer = (path) => path.includes('big') ? [413, {error: 'This file is 70.0 MB; the limit is 64 MB per file.'}]
  : [200, {upload: {token: 'tok-' + uploads.length}}];
await addRelatedFiles([file('a.pdf'), file('empty.md', 0), file('big.pdf'), file('c.tex')]);
assert.deepEqual(statuses.slice(0, 4), ['Uploading 1 of 4: a.pdf…', 'Uploading 2 of 4: empty.md…',
  'Uploading 3 of 4: big.pdf…', 'Uploading 4 of 4: c.tex…']);
assert.deepEqual(uploads.map(upload => upload.path), ['/uploads?name=a.pdf', '/uploads?name=big.pdf', '/uploads?name=c.tex']);
assert.deepEqual(acted, [{path: '/related-works/add', body: {uploads: ['tok-1', 'tok-3']}}]);
assert.equal(ui.relatedWorksStatus.textContent, 'Added 2 related works. empty.md is empty. '
  + 'Could not upload big.pdf: This file is 70.0 MB; the limit is 64 MB per file.');
assert.equal(ui.addRelatedFiles.disabled, false);
// A refused add discards the staged uploads.
actResult = false;
await addRelatedFiles([file('d.pdf')]);
assert.deepEqual(sent.at(-1), {path: '/uploads/discard', body: {token: 'tok-4'}});
assert.equal(ui.relatedWorksStatus.textContent, '');
actResult = true;
// Leaving the job while files upload adds nothing there and discards them.
uploadAnswer = () => { currentJob = 'other'; return [200, {upload: {token: 'tok-gone'}}]; };
acted.length = 0;
await addRelatedFiles([file('e.pdf'), file('f.pdf')]);
assert.deepEqual(acted, []);
assert.deepEqual(sent.at(-1), {path: '/uploads/discard', body: {token: 'tok-gone'}});
assert.equal(uploads.length, 5, 'the second file is not uploaded for a job no longer on screen');
""")

    def test_a_run_page_note_is_staged_then_added(self):
        self.run_app(PANEL, PANEL_SETUP, r"""
render({...author, relatedWorks: works([])});
ui.addRelatedNote.onclick();
assert.equal(ui.relatedNoteForm.hidden, false);
assert.equal(ui.addRelatedNote.attributes['aria-expanded'], 'true');
ui.relatedNoteText.value = ' ';
await addRelatedNote();
assert.equal(ui.relatedWorksStatus.textContent, "Enter the note's text.");
assert.equal(sent.length, 0);
answers['/uploads/note'] = new Error('Keep the note title to one line of at most 200 characters.');
ui.relatedNoteTitle.value = 'T'.repeat(201);
ui.relatedNoteText.value = 'Use Lemma 2.';
await addRelatedNote();
assert.equal(ui.relatedWorksStatus.textContent, 'Keep the note title to one line of at most 200 characters.');
assert.equal(ui.relatedNoteText.value, 'Use Lemma 2.');
assert.deepEqual(acted, []);
answers['/uploads/note'] = {upload: {token: 'tok-note'}};
ui.relatedNoteTitle.value = 'Plan';
await addRelatedNote();
assert.deepEqual(sent.at(-1), {path: '/uploads/note', body: {title: 'Plan', text: 'Use Lemma 2.'}});
assert.deepEqual(acted, [{path: '/related-works/add', body: {uploads: ['tok-note']}}]);
assert.equal(ui.relatedWorksStatus.textContent, 'Added 1 related work.');
assert.equal(ui.relatedNoteForm.hidden, true);
assert.equal(ui.relatedNoteText.value, '');
assert.equal(ui.relatedNoteTitle.value, '');
""")

    def test_the_batch_page_lists_folder_runs_and_adds_to_every_job_of_one(self):
        self.run_app(PANEL, PANEL_SETUP, r"""
currentJob = '';
const jobs = [{runId: 'j1', sourceFile: 'a.md', phase: 'running', folderBatch: 'b_0123456789'},
  {runId: 'j2', sourceFile: 'b.md', phase: 'prepared', folderBatch: 'b_0123456789'}, {runId: 'j3', title: 'Alone'},
  {runId: 'j4', sourceFile: 'old.md', phase: 'stopping', folderBatch: 'b_00000000f1'},
  {runId: 'j5', sourceFile: 'done.md', phase: 'done', folderBatch: 'b_00000000d1'}];
answers['/batch-related-works'] = path => path.endsWith('b_00000000f1') ? works([pdf], {available: false, reason: 'Finished.'})
  : works([{...pdf, name: 'survey.pdf', originalName: 'survey.pdf'}]);
renderBatchRelatedWorks(jobs);
await tick(); await tick();
// A folder run whose jobs have all finished is not even read.
assert.deepEqual(sent.map(item => item.path),
  ['/batch-related-works?batch=b_0123456789', '/batch-related-works?batch=b_00000000f1']);
assert.equal(ui.batchRelatedWorks.hidden, false);
const groups = ui.batchRelatedGroups.children;
assert.equal(groups.length, 1, 'a folder run whose store takes no more related works is not shown');
assert.equal(groups[0].dataset.batch, 'b_0123456789');
assert.equal(groups[0].children[0].children.map(node => node.textContent).join(' | '), 'Folder run | 1 related work');
assert.equal(groups[0].children[1].textContent, '2 problems: a.md, b.md');
assert.deepEqual(rowText(groups[0].children[2]), [['PDF', 'survey.pdf', '1.2 MB · text extracted (12 pages)']]);
const [addFiles, addNote] = groups[0].children[3].children;
assert.equal(addFiles.textContent, 'Add files…');
assert.equal(addNote.textContent, 'Add a note');
// The polls of the batch page read a store again at most every 10 seconds.
renderBatchRelatedWorks(jobs);
assert.equal(sent.length, 2);
// Add files…: each file uploads, then one request adds them to the folder run's store.
addFiles.onclick();
assert.equal(ui.batchRelatedFiles.clicked, 1);
answers['/batch-related-works/add'] = (path, body) => ({...works([pdf, pdf]), notice: ''});
await addBatchRelatedFiles([file('lemma.pdf'), file('empty.md', 0)]);
assert.deepEqual(sent.at(-1), {path: '/batch-related-works/add?batch=b_0123456789', body: {uploads: ['tok-1']}});
assert.equal(ui.batchRelatedStatus.textContent, 'Added 1 related work to the folder run. empty.md is empty.');
assert.equal(ui.batchRelatedGroups.children[0].children[0].children[1].textContent, '2 related works');
// Add a note: the shared form, for that folder run.
ui.batchRelatedGroups.children[0].children[3].children[1].onclick();
assert.equal(ui.batchNoteForm.hidden, false);
assert.equal(ui.batchNoteTarget.textContent, 'For every job of the folder run of 2 problems: a.md, b.md.');
answers['/uploads/note'] = {upload: {token: 'tok-note'}};
answers['/batch-related-works/add'] = {...works([pdf, pdf, note]), notice: 'One job got its own copy.'};
ui.batchNoteText.value = 'Shared hint.';
await addBatchNote();
assert.deepEqual(sent.slice(-2), [{path: '/uploads/note', body: {title: '', text: 'Shared hint.'}},
  {path: '/batch-related-works/add?batch=b_0123456789', body: {uploads: ['tok-note']}}]);
assert.equal(ui.batchRelatedStatus.textContent, 'Added 1 related work to the folder run. One job got its own copy.');
assert.equal(ui.batchNoteForm.hidden, true);
// A refused add says why and discards the upload.
answers['/batch-related-works/add'] = new Error('Every job of this folder run has finished.');
await addBatchRelatedFiles([file('late.pdf')]);
assert.equal(ui.batchRelatedStatus.textContent, 'Every job of this folder run has finished.');
assert.deepEqual(sent.at(-1), {path: '/uploads/discard', body: {token: 'tok-2'}});
// A saved (read-only) batch shows none; nor does a batch without folder runs.
batches = [{id: 'saved', readOnly: true}]; currentBatch = 'saved';
renderBatchRelatedWorks(jobs);
assert.equal(ui.batchRelatedWorks.hidden, true);
batches = [{id: '__root__', current: true}]; currentBatch = '__root__';
renderBatchRelatedWorks([{runId: 'j3'}]);
assert.equal(ui.batchRelatedWorks.hidden, true);
""")


class RelatedWorksMarkupTests(unittest.TestCase):
    class Page(HTMLParser):
        VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

        def __init__(self):
            super().__init__()
            self.elements, self.order, self.parents, self.labels = {}, [], {}, set()
            self._open = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.elements[attrs["id"]] = {"tag": tag, **attrs}
                self.order.append(attrs["id"])
                self.parents[attrs["id"]] = [identity for _, identity in self._open if identity]
            if tag == "label" and "for" in attrs:
                self.labels.add(attrs["for"])
            if tag not in self.VOID:
                self._open.append((tag, attrs.get("id")))

        def handle_endtag(self, tag):
            while self._open:
                if self._open.pop()[0] == tag:
                    break

    @classmethod
    def setUpClass(cls):
        cls.page = cls.Page()
        cls.page.feed(INDEX.read_text(encoding="utf-8"))

    def test_new_controls_have_unique_ids_labels_and_their_places(self):
        page = self.page
        self.assertEqual(len(page.order), len(set(page.order)))
        # The conversation is inside the run status card, between the recovery block and the box.
        for identity in ("authorConversation", "authorSteerControl", "jobRecovery"):
            self.assertIn("runPanel", page.parents[identity])
        order = page.order.index
        self.assertLess(order("jobRecovery"), order("authorConversation"))
        self.assertLess(order("authorConversation"), order("authorSteerControl"))
        for identity in ("authorConversationHeading", "authorConversationNote", "authorConversationList"):
            self.assertIn("authorConversation", page.parents[identity])
        for identity in ("authorSteerLabel", "sendAuthorSteerText", "requestSummaryButton", "stopReplyButton",
                         "authorSteerCount"):
            self.assertIn("authorSteerControl", page.parents[identity])
        self.assertIn("hidden", page.elements["requestSummaryButton"])
        self.assertIn("hidden", page.elements["stopReplyButton"])
        self.assertEqual(page.elements["authorSteerInstruction"]["data-max-chars"], "12000")
        self.assertIn("authorTimeLimitControl", page.parents["authorLimitHint"])
        self.assertLess(order("setAuthorTimeLimitButton"), order("authorLimitHint"))
        # The run page's panel follows the statement; the composer field follows the workflow stages.
        self.assertLess(order("statementDetails"), order("relatedWorksPanel"))
        self.assertLess(order("relatedWorksPanel"), order("resultPanel"))
        self.assertLess(order("workflowWriting"), order("relatedWorksField"))
        self.assertLess(order("problem"), order("workflowReview"))
        self.assertIn("inputPanel", page.parents["relatedWorksField"])
        self.assertIn("jobsPanel", page.parents["batchRelatedWorks"])
        for prefix in ("relatedNote", "composerNote", "batchNote"):
            for field in ("Title", "Text"):
                self.assertIn(prefix + field, page.labels)
            self.assertEqual(page.elements[prefix + "Title"]["maxlength"], "200")
        for identity in ("relatedFilesInput", "composerRelatedFiles", "batchRelatedFiles"):
            element = page.elements[identity]
            self.assertEqual((element["type"], "multiple" in element, "hidden" in element), ("file", True, True))
            self.assertTrue(element.get("aria-label") or identity in page.labels, identity)

    def test_every_new_element_is_in_the_ui_map(self):
        source = APP.read_text(encoding="utf-8")
        start = source.index("const ui = {")
        mapping = source[start:source.index("\n};", start)]
        for identity in ("authorConversation", "authorConversationNote", "authorConversationList", "authorSteerLabel",
                         "sendAuthorSteerText", "requestSummaryButton", "stopReplyButton", "authorLimitHint",
                         "relatedWorksPanel", "relatedWorksHeading", "relatedWorksNote", "relatedWorksList",
                         "relatedWorksEmpty", "relatedWorksActions", "addRelatedFilesButton", "addRelatedNoteButton",
                         "relatedNoteForm", "relatedNoteTitle", "relatedNoteText", "saveRelatedNoteButton",
                         "cancelRelatedNoteButton", "relatedWorksStatus", "relatedFilesInput", "relatedWorksField",
                         "relatedWorksLabel", "composerRelatedList", "composerAddFilesButton", "composerAddNoteButton",
                         "composerNoteForm", "composerNoteTitle", "composerNoteText", "composerSaveNoteButton",
                         "composerCancelNoteButton", "relatedWorksHelp", "composerRelatedFiles", "batchRelatedWorks",
                         "batchRelatedGroups", "batchNoteForm", "batchNoteTitle", "batchNoteText",
                         "batchSaveNoteButton", "batchCancelNoteButton", "batchRelatedStatus", "batchRelatedFiles"):
            with self.subTest(identity=identity):
                self.assertIn(identity, self.page.elements)
                self.assertIn(f'$("{identity}")', mapping)

    def test_uploads_and_notes_follow_the_server_limits(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("const maxRelatedFileBytes = 64 * 1024 * 1024;", source)
        paths = re.search(r"const jobRequestPaths = \[([^\]]*)\]", source).group(1)
        self.assertIn('"/uploads/note"', paths)
        self.assertIn('"/start-workflow"', paths)


if __name__ == "__main__":
    unittest.main()
