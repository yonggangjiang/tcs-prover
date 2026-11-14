"""Related works shared with every problem in a batch, in the browser: the batch page's section (its list with
Remove, Add files… and Add a note), the line of the New problem page, the count on the Overview's batch card, the
folder-run question, and a job's list that marks the works from its batch and from its folder run.

Like the other UI tests, these cut ui/app.js into slices by marker text and run them in Node's vm with a fake
DOM (tests/test_conversation_ui.py and tests/test_batch_navigation.py have the runners). The server is replaced
by stubs shaped like its routes: GET /batch-shared-works?batch=, POST /batch-shared-works/add?batch= and
/batch-shared-works/remove?batch=, and the sharedWorks of a /batches card.
"""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import test_batch_navigation as navigation
from tests import test_related_works_ui as related

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"
INDEX = ROOT / "ui" / "index.html"

# The batch page and the run page: the related-works span (with the section) and render().
PAGE_SETUP = related.PANEL_SETUP + r"""
let composingProblem = false, confirmAnswer = true;
const questions = [];
const confirm = question => { questions.push(question); return confirmAnswer; };
const graph = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true,
  sharedWorks: {count: 2, names: ['defs.md', 'Plan.md']}};
const defs = {name: 'defs.md', originalName: 'defs.md', kind: 'file', title: '', sizeText: '2.0 KB', textStatus: '',
  addedAt: '2026-11-13T09:00:00+00:00', batch: '', sharedFrom: ''};
const listing = (items, extra = {}) => ({batch: 'batch_00000000a1', available: true, reason: '', count: items.length,
  items, ...extra});
const rowsOf = list => list.children.map(row => [row.children[0].textContent,
  ...row.children[1].children.map(node => node.textContent), row.children[2]?.textContent ?? '']);
"""

# The batch page's section, for the batch navigation runner (the real selectedBatch(), selectBatch() and loadJobs()),
# with what it uses from elsewhere in the page (no backticks: the runner's setup is a template string).
SHARED_SECTION = ["// Shared with every problem in this batch: the works", "async function addBatchSharedFiles("]
SHARED_SECTION_STUBS = r"""
const relatedWorksCount = count => count + ' related work' + (count === 1 ? '' : 's');
const setNoteForm = () => {}, relatedWorkRow = () => new Element();
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class BatchSharedBrowserTests(unittest.TestCase):
    run_app = related.RelatedWorksBrowserTests.run_app
    browser = navigation.BatchNavigationTests.browser

    def test_markers_occur_once(self):
        source = APP.read_text(encoding="utf-8")
        for marker in ("// Shared with every problem in this batch: the works", "function renderBatchShared(",
                       "function renderComposerShared(", "function composerBatchCard(", "function sharedNamesText("):
            with self.subTest(marker=marker):
                self.assertEqual(source.count(marker), 1)

    def test_the_batch_page_lists_what_it_shares_and_adds_and_removes_works(self):
        self.run_app(related.PANEL, PAGE_SETUP, r"""
currentJob = ''; currentBatch = 'batch_00000000a1'; batches = [graph];
answers['/batch-shared-works'] = listing([defs, note]);
renderBatchShared();
// Shown at once with the card's count, then the listing the server sends.
assert.equal(ui.batchShared.hidden, false);
assert.equal(ui.batchSharedCount.textContent, '2 related works');
await tick(); await tick();
assert.deepEqual(sent.map(item => item.path), ['/batch-shared-works?batch=batch_00000000a1']);
assert.deepEqual(rowsOf(ui.batchSharedList), [['MD', 'defs.md', '2.0 KB', 'Remove'],
  ['NOTE', 'Plan.md', 'note “Plan” · 2.0 KB', 'Remove']]);
assert.equal(ui.batchSharedList.hidden, false);
assert.equal(ui.batchSharedEmpty.hidden, true);
assert.equal(ui.batchSharedList.children[0].children[2].attributes['aria-label'], 'Remove defs.md');
// The polls of the batch page read the list again at most every 10 seconds, unless the card's count changed.
renderBatchShared();
assert.equal(sent.length, 1);
batches = [{...graph, sharedWorks: {count: 3, names: ['defs.md', 'Plan.md', 'x.md']}}];
renderBatchShared();
await tick(); await tick();
assert.equal(sent.length, 2);
// Read once for that change, not again while the card stays as it is.
renderBatchShared();
await tick(); await tick();
assert.equal(sent.length, 2);
// Add files…: each file uploads, then one request shares them; the status says who got them.
ui.batchSharedAddFiles.onclick();
assert.equal(ui.batchSharedFiles.clicked, 1);
answers['/batch-shared-works/add'] = (path, body) => listing([defs, note, {...defs, name: 'lemma.pdf'}],
  {summary: '3 problems in this batch got it now, and 1 running or paused author is told. New problems in this '
    + 'batch get it too.', notice: ''});
await addBatchSharedFiles([file('lemma.pdf'), file('empty.md', 0)]);
assert.deepEqual(sent.at(-1), {path: '/batch-shared-works/add?batch=batch_00000000a1', body: {uploads: ['tok-1']}});
assert.equal(ui.batchSharedStatus.textContent, 'Added 1 related work. 3 problems in this batch got it now, and 1 '
  + 'running or paused author is told. New problems in this batch get it too. empty.md is empty.');
assert.equal(ui.batchSharedCount.textContent, '3 related works');
assert.equal(ui.batchSharedList.children.length, 3);
assert.equal(ui.batchSharedAddFiles.disabled, false);
// Add a note: an empty one is refused; a note is staged, then shared, and the form closes.
ui.batchSharedAddNote.onclick();
assert.equal(ui.batchSharedNoteForm.hidden, false);
assert.equal(ui.batchSharedAddNote.attributes['aria-expanded'], 'true');
ui.batchSharedNoteText.value = ' ';
await addBatchSharedNote();
assert.equal(ui.batchSharedStatus.textContent, "Enter the note's text.");
answers['/uploads/note'] = {upload: {token: 'tok-note'}};
answers['/batch-shared-works/add'] = listing([defs], {summary: 'No problem in this batch can take related works '
  + 'now; new problems in this batch get it.', notice: 'Could not add them to every problem: b.md: Another TCS Prover '
  + 'process runs this job.'});
ui.batchSharedNoteTitle.value = 'Definitions';
ui.batchSharedNoteText.value = 'A graph is planar when ...';
await addBatchSharedNote();
assert.deepEqual(sent.slice(-2), [{path: '/uploads/note', body: {title: 'Definitions', text: 'A graph is planar when ...'}},
  {path: '/batch-shared-works/add?batch=batch_00000000a1', body: {uploads: ['tok-note']}}]);
assert.equal(ui.batchSharedStatus.textContent, 'Added 1 related work. No problem in this batch can take related '
  + 'works now; new problems in this batch get it. Could not add them to every problem: b.md: Another TCS Prover '
  + 'process runs this job.');
assert.equal(ui.batchSharedNoteForm.hidden, true);
assert.equal(ui.batchSharedNoteText.value, '');
// A refused share says why and discards the upload.
answers['/batch-shared-works/add'] = new Error('That batch no longer exists.');
await addBatchSharedFiles([file('late.pdf')]);
assert.equal(ui.batchSharedStatus.textContent, 'That batch no longer exists.');
assert.deepEqual(sent.at(-1), {path: '/uploads/discard', body: {token: 'tok-2'}});
// Remove asks first, saying who keeps the work; a No sends nothing.
answers['/batch-shared-works'] = listing([defs, note]);
await loadBatchShared(true);
confirmAnswer = false;
const before = sent.length;
await ui.batchSharedList.children[0].children[2].onclick();
assert.equal(sent.length, before);
assert.equal(questions.at(-1), 'Stop sharing defs.md with the problems in this batch?\n\nNew problems no longer get '
  + 'it, and problems that have not started lose it. Running, paused and finished problems keep their copy.');
confirmAnswer = true;
answers['/batch-shared-works/remove'] = (path, body) => listing([note], {removed: body.name, summary: 'Removed '
  + 'defs.md. New problems in this batch no longer get it. It was taken out of 1 problem that had not started. 2 '
  + 'problems that have started or have a web cutoff keep their copy.', notice: ''});
await ui.batchSharedList.children[0].children[2].onclick();
assert.deepEqual(sent.at(-1), {path: '/batch-shared-works/remove?batch=batch_00000000a1', body: {name: 'defs.md'}});
assert.equal(ui.batchSharedStatus.textContent, 'Removed defs.md. New problems in this batch no longer get it. It was '
  + 'taken out of 1 problem that had not started. 2 problems that have started or have a web cutoff keep their copy.');
assert.deepEqual(rowsOf(ui.batchSharedList), [['NOTE', 'Plan.md', 'note “Plan” · 2.0 KB', 'Remove']]);
assert.equal(ui.batchSharedCount.textContent, '1 related work');
""")

    def test_a_first_read_of_the_list_that_fails_is_not_tried_again_at_once(self):
        self.run_app(related.PANEL, PAGE_SETUP, r"""
currentJob = ''; currentBatch = 'batch_00000000a1'; batches = [graph];
// The page opens while the list cannot be read (TCS Prover restarting, or the batch deleted in another tab); the
// refusal arrives later, as a real request's does.
answers['/batch-shared-works'] = () => new Promise((resolve, reject) => setImmediate(() =>
  reject(new Error('Failed to fetch'))));
renderBatchShared();
for (let step = 0; step < 20; step += 1) await tick();
const reads = sent.length;
answers['/batch-shared-works'] = listing([defs]);
// One read, which says why it failed; the next comes with a later poll, as after a read that worked.
assert.equal(reads, 1);
assert.equal(ui.batchSharedStatus.textContent, 'Failed to fetch');
renderBatchShared();
await tick(); await tick();
assert.equal(sent.length, 1);
// A change of the card's count (a work shared in another tab) reads it again at once.
batches = [{...graph, sharedWorks: {count: 3, names: ['defs.md', 'Plan.md', 'x.md']}}];
renderBatchShared();
await tick(); await tick();
assert.equal(sent.length, 2);
assert.deepEqual(rowsOf(ui.batchSharedList), [['MD', 'defs.md', '2.0 KB', 'Remove']]);
""")

    def test_a_read_of_the_list_that_works_again_takes_back_the_error_it_showed(self):
        self.run_app(related.PANEL, PAGE_SETUP, r"""
currentJob = ''; currentBatch = 'batch_00000000a1'; batches = [graph];
answers['/batch-shared-works'] = listing([defs]);
renderBatchShared();
await tick(); await tick();
// A later read fails (TCS Prover restarting, say): the section says why ...
answers['/batch-shared-works'] = new Error('Failed to fetch');
await loadBatchShared(true);
assert.equal(ui.batchSharedStatus.textContent, 'Failed to fetch');
// ... until a read works again.
answers['/batch-shared-works'] = listing([defs, note]);
await loadBatchShared(true);
assert.equal(ui.batchSharedStatus.textContent, '');
assert.equal(ui.batchSharedList.children.length, 2);
// What a change said stays, through a read that fails and one that works again.
ui.batchSharedStatus.textContent = 'Added 1 related work.';
answers['/batch-shared-works'] = new Error('Failed to fetch');
await loadBatchShared(true);
answers['/batch-shared-works'] = listing([defs]);
await loadBatchShared(true);
assert.equal(ui.batchSharedStatus.textContent, 'Added 1 related work.');
""")

    def test_the_section_shows_only_on_a_live_batch_page_and_says_when_nothing_is_shared(self):
        self.run_app(related.PANEL, PAGE_SETUP, r"""
currentJob = ''; currentBatch = '__root__';
batches = [{id: '__root__', label: 'Default batch', default: true, addProblems: true,
  sharedWorks: {count: 0, names: []}}];
answers['/batch-shared-works'] = {...listing([]), batch: '__root__'};
renderBatchShared();
await tick(); await tick();
assert.deepEqual(sent.map(item => item.path), ['/batch-shared-works?batch=__root__']);
assert.equal(ui.batchShared.hidden, false);
assert.equal(ui.batchSharedCount.textContent, '0 related works');
assert.equal(ui.batchSharedEmpty.hidden, false);
assert.equal(ui.batchSharedList.hidden, true);
// A status line and an open note form belong to the batch they were shown for.
ui.batchSharedStatus.textContent = 'Added 1 related work.';
ui.batchSharedAddNote.onclick();
batches.push(graph); currentBatch = graph.id;
answers['/batch-shared-works'] = listing([defs]);
renderBatchShared();
assert.equal(ui.batchSharedStatus.textContent, '');
assert.equal(ui.batchSharedNoteForm.hidden, true);
// A saved batch, a card without shared works (an older server, the live gateway), a job page and the New problem
// page show no section.
for (const [card, job, composing] of [[{...graph, readOnly: true}, '', false], [{...graph, sharedWorks: undefined}, '', false],
    [graph, 'job-1', false], [graph, '', true]]) {
  batches = [card]; currentJob = job; composingProblem = composing;
  renderBatchShared();
  assert.equal(ui.batchShared.hidden, true);
}
""")

    def test_a_jobs_list_marks_the_works_from_its_batch_and_from_its_folder_run(self):
        self.run_app(related.PANEL, PAGE_SETUP, r"""
const running = {...author, phase: 'running', goalThreadId: 'thread-1'};
const fromBatch = {...defs, batch: 'default', sharedFrom: 'batch'};
const fromRun = {...pdf, name: 'survey.pdf', originalName: 'survey.pdf', batch: 'b_0123456789', sharedFrom: 'folderRun'};
render({...running, relatedWorks: works([pdf, fromBatch, {...note, batch: 'batch_00000000a1', sharedFrom: 'batch'},
  fromRun])});
assert.deepEqual(rowText(ui.relatedWorksList), [
  ['PDF', 'My-Paper.pdf', '1.2 MB · text extracted (12 pages) · uploaded as “My Paper.pdf”'],
  ['MD', 'defs.md', '2.0 KB · from the batch'],
  ['NOTE', 'Plan.md', 'note “Plan” · 2.0 KB · from the batch'],
  ['PDF', 'survey.pdf', '1.2 MB · text extracted (12 pages) · from the folder run']]);
assert.equal(ui.relatedWorksNote.textContent, "Saved in the author's related-works folder. The author is told about "
  + 'new files at its next step. Works marked “from the batch” are shared with every problem in this batch; add or '
  + 'remove them on the batch page.');
// A job of a folder run says where works for the whole run go, after the batch's.
render({...running, folderBatch: 'b_0123456789', relatedWorks: works([fromBatch, fromRun])});
assert.equal(ui.relatedWorksNote.textContent, "Saved in the author's related-works folder. The author is told about "
  + 'new files at its next step. Works marked “from the batch” are shared with every problem in this batch; add or '
  + 'remove them on the batch page. Works for every problem of this folder run are added under Shared with a folder '
  + 'run on the batch page.');
// Without a work from its batch the job says nothing of it.
render({...running, relatedWorks: works([pdf])});
assert.doesNotMatch(ui.relatedWorksNote.textContent, /from the batch/);
// A work removed from the batch's shared works that the job kept says so, and the note says what that means; the
// works still shared keep "from the batch".
const kept = {...fromBatch, name: 'old.md', originalName: 'old.md', noLongerShared: true};
render({...running, relatedWorks: works([kept, fromBatch, {...note, batch: 'default', sharedFrom: 'batch',
  noLongerShared: true}])});
assert.deepEqual(rowText(ui.relatedWorksList), [
  ['MD', 'old.md', '2.0 KB · from the batch, no longer shared'],
  ['MD', 'defs.md', '2.0 KB · from the batch'],
  ['NOTE', 'Plan.md', 'note “Plan” · 2.0 KB · from the batch, no longer shared']]);
assert.equal(ui.relatedWorksNote.textContent, "Saved in the author's related-works folder. The author is told about "
  + 'new files at its next step. Works marked “from the batch” are shared with every problem in this batch; add or '
  + 'remove them on the batch page. Works marked “from the batch, no longer shared” were removed from the batch\'s '
  + 'shared works; this job keeps its copy.');
// With only works the batch no longer shares, the note does not say that works from the batch are shared.
render({...running, relatedWorks: works([pdf, kept])});
assert.equal(ui.relatedWorksNote.textContent, "Saved in the author's related-works folder. The author is told about "
  + 'new files at its next step. Works marked “from the batch, no longer shared” were removed from the batch\'s '
  + 'shared works; this job keeps its copy.');
// A folder run's works are never marked so.
render({...running, relatedWorks: works([{...fromRun, noLongerShared: true}])});
assert.deepEqual(rowText(ui.relatedWorksList), [
  ['PDF', 'survey.pdf', '1.2 MB · text extracted (12 pages) · from the folder run']]);
assert.doesNotMatch(ui.relatedWorksNote.textContent, /no longer shared/);
""")

    def test_the_new_problem_page_names_what_its_batch_shares_and_links_to_the_batch_page(self):
        self.browser(r"""
const graph = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true,
  sharedWorks: {count: 5, names: ['a.md', 'b.md', 'c.md']}};
const root = {id: '__root__', label: 'Default batch', default: true, addProblems: true,
  sharedWorks: {count: 1, names: ['defs.md']}};
adoptBatchCatalog({batches: [root, graph], canCreateBatches: true});
batchCatalog = [root, graph]; composingProblem = true; currentBatch = graph.id;
ui.workflowAuthor.checked = true;
renderBatchNavigation();
assert.equal(ui.composerShared.hidden, false);
const [text, link] = ui.composerShared.children;
assert.equal(text, 'This problem also gets the 5 related works shared with every problem in “Graph coloring”: a.md, '
  + 'b.md, c.md and 2 more. ');
assert.equal(link.textContent, 'Open the batch page');
// In a new window, so what is written on this page stays.
assert.deepEqual([link.href, link.target, link.rel, link['aria-label']],
  ['/?batch=batch_00000000a1#test-token', '_blank', 'noopener', 'Open the batch page in a new window']);
// The names it lists: one, two, three.
for (const [names, count, listed] of [[['a.md'], 1, 'the 1 related work shared with every problem in “Graph coloring”: a.md. '],
    [['a.md', 'b.md'], 2, 'the 2 related works shared with every problem in “Graph coloring”: a.md and b.md. '],
    [['a.md', 'b.md', 'c.md'], 3, 'the 3 related works shared with every problem in “Graph coloring”: a.md, b.md and c.md. ']]) {
  batchCatalog = [root, {...graph, sharedWorks: {count, names}}];
  renderComposerShared();
  assert.equal(ui.composerShared.children[0], 'This problem also gets ' + listed);
}
// With the Author stage off this problem gets none of them, and the line says so.
ui.workflowAuthor.checked = false;
renderComposerShared();
assert.equal(ui.composerShared.children[0], 'The 3 related works shared with every problem in “Graph coloring” are '
  + 'read by the proof author: with the Author stage off, this problem does not get them. ');
// The default batch.
ui.workflowAuthor.checked = true; currentBatch = '';
renderComposerShared();
assert.equal(ui.composerShared.children[0], 'This problem also gets the 1 related work shared with every problem in the '
  + 'default batch: defs.md. ');
assert.equal(ui.composerShared.children[1].href, '/?batch=__root__#test-token');
// Nothing shared, a batch that is not live, a card from a server without shared works, or another page: no line.
for (const [card, job, composing] of [[{...root, sharedWorks: {count: 0, names: []}}, '', true],
    [{...root, readOnly: true}, '', true], [{...root, sharedWorks: undefined}, '', true], [root, 'job-1', true],
    [root, '', false]]) {
  batchCatalog = [card]; currentJob = job; composingProblem = composing;
  renderComposerShared();
  assert.equal(ui.composerShared.hidden, true);
}
""")

    def test_the_batch_page_renders_the_section_as_it_opens_and_with_every_poll(self):
        self.browser(r"""
const graph = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true,
  sharedWorks: {count: 1, names: ['defs.md']}};
setResponses({'/batches': {batches: [graph], canCreateBatches: true}, '/jobs?batch=batch_00000000a1': {jobs: []}});
await selectBatch(graph.id);
const opened = sharedRenders;
assert(opened >= 2, 'rendered when the page opened and after its first poll');
await loadJobs();
assert(sharedRenders > opened, 'rendered again with the next poll');
""", setup="let sharedRenders = 0; const renderBatchShared = () => { sharedRenders += 1; };")

    def test_the_default_batch_page_stays_when_it_has_no_problem_and_shares_nothing_more(self):
        self.browser(r"""
const root = {id: '__root__', label: 'Default batch', default: true, current: true, addProblems: true, jobCount: 0,
  counts: {}, sharedWorks: {count: 1, names: ['defs.md']}};
const graph = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 1,
  counts: {}, sharedWorks: {count: 0, names: []}};
const defs = {name: 'defs.md', kind: 'file', sizeText: '4 B', batch: '', sharedFrom: ''};
const shared = items => ({batch: '__root__', available: true, reason: '', count: items.length, items});
setResponses({'/batches': {batches: [root, graph], canCreateBatches: true}, '/jobs?batch=__root__': {jobs: []},
  '/batch-shared-works?batch=__root__': shared([defs])});
await selectBatch('__root__');
await new Promise(setImmediate);
assert.equal(ui.batchShared.hidden, false);
assert.equal(ui.batchSharedCount.textContent, '1 related work');
// Its last shared work removed, should a list leave out the default batch (no problem in it; the server lists it
// always) ...
setResponses({'/batches': {batches: [graph], canCreateBatches: true}, '/jobs?batch=__root__': {jobs: []},
  '/batch-shared-works?batch=__root__': shared([])});
ui.batchSharedStatus.textContent = 'Removed defs.md. New problems in this batch no longer get it.';
await loadJobs();
await new Promise(setImmediate);
// ... yet its page stays, never said to be gone: what Remove did stays on screen, and works can be shared again.
assert.equal(ui.notice.textContent, '');
assert.equal(ui.jobsHeading.textContent, 'Default batch');
assert.equal(ui.batchProblems.textContent, 'Default batch');
assert.equal(ui.batchShared.hidden, false);
assert.equal(ui.batchSharedCount.textContent, '0 related works');
assert.equal(ui.batchSharedStatus.textContent, 'Removed defs.md. New problems in this batch no longer get it.');
assert.equal(JSON.stringify([ui.batchActions.hidden, ui.renameBatch.hidden, ui.deleteBatch.hidden]),
  JSON.stringify([false, true, true]));
// A batch made with New batch that is gone is still said to be gone, and so is the default batch of a server that
// makes no batches (an older server, the live gateway).
for (const [batch, catalog] of [['batch_00000000b2', {batches: [graph], canCreateBatches: true}],
    ['__root__', {batches: [graph]}]]) {
  setResponses({'/batches': catalog, '/jobs?batch=__root__': {jobs: []}});
  ui.notice.textContent = '';
  await selectBatch(batch);
  assert.equal(ui.notice.textContent, 'This batch is no longer available. Return to All batches.', batch);
  assert.equal(ui.notice.hidden, false, batch);
}
""", slices=[SHARED_SECTION], setup=SHARED_SECTION_STUBS)

    def test_the_new_problem_line_follows_the_author_stage(self):
        self.run_app(related.PANEL, PAGE_SETUP + r"""
let composerSharedRenders = 0;
const renderComposerShared = () => { composerSharedRenders += 1; };
""", r"""
// Turning a stage on or off renders the composer's related works, and the line about the batch's with them.
renderComposerRelatedWorks();
assert.equal(composerSharedRenders, 1);
""")

    def test_delete_batch_says_the_works_it_shares_go_with_it(self):
        self.run_app(related.PANEL + [["async function deleteCurrentBatch(", "function adoptJob("]], PAGE_SETUP + r"""
let batchCatalog = [];
const adoptBatchCatalog = value => value.batches || [];
const selectBatch = async () => {};
""", r"""
currentJob = ''; currentBatch = graph.id; confirmAnswer = false;
for (const [count, said] of [[2, '\n\nThe 2 related works it shares are deleted with it.'],
    [1, '\n\nThe 1 related work it shares is deleted with it.'], [0, '']]) {
  batches = [{...graph, jobCount: 0, sharedWorks: {count, names: []}}];
  await deleteCurrentBatch();
  assert.equal(questions.at(-1), 'Delete the empty batch “Graph coloring”?' + said);
}
assert.equal(sent.length, 0);
""")

    def test_a_batch_card_counts_its_shared_works(self):
        self.browser(r"""
renderBatches([{id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 3,
  counts: {running: 3}, sharedWorks: {count: 2, names: ['a.md', 'b.md']}},
  {id: 'batch_00000000b2', label: 'One', named: true, addProblems: true, jobCount: 1, counts: {done: 1},
   sharedWorks: {count: 1, names: ['a.md']}},
  {id: 'batch_00000000c3', label: 'None', named: true, addProblems: true, jobCount: 0, counts: {},
   sharedWorks: {count: 0, names: []}},
  {id: 'old', label: 'Earlier', readOnly: true, jobCount: 2, counts: {done: 2}, sharedWorks: {count: 4, names: []}}]);
const count = index => ui.batchesList.children[index].children[0].children[1].children[1];
assert.equal(count(0).textContent, '3 problems');
assert.equal(JSON.stringify(count(0).children.map(node => [node.className, node.textContent])),
  JSON.stringify([['visually-hidden', ', '], ['batch-card-shared', '2 shared related works']]));
assert.equal(count(1).children[1].textContent, '1 shared related work');
assert.deepEqual([count(2).children.length, count(3).children.length], [0, 0]);
""")

    def test_a_folder_run_question_names_what_the_batch_shares(self):
        self.run_app(related.COMPOSER, related.COMPOSER_SETUP + r"""
let shared = 2;
const composerBatchCard = () => ({id: 'batch_00000000a1', addProblems: true, sharedWorks: {count: shared, names: []}});
""", r"""
const run = startFolderBatch([file('a.md', 8, 'Prove A.')]);
await until(() => sent.length === 1);
assert.ok(questions[0].includes('Each job uses the selected stages. Every job also gets the 2 related works shared '
  + 'with every problem in this batch.'), questions[0]);
sent[0].resolve({batchId: 'batch_00000000a1', startedJobs: ['j1'], jobs: []});
await run;
// Without the Author stage, or with nothing shared, the question says nothing of them.
for (const [author, count] of [[false, 2], [true, 0]]) {
  chooseStages({review: false, author}); shared = count;
  const next = startFolderBatch([file('b.md', 8, 'Prove B.')]);
  await until(() => sent.length === (author ? 3 : 2));
  assert.doesNotMatch(questions.at(-1), /shared with every problem/);
  sent.at(-1).resolve({batchId: 'batch_00000000a1', startedJobs: ['j2'], jobs: []});
  await next;
}
""")


class BatchSharedMarkupTests(unittest.TestCase):
    Page = related.RelatedWorksMarkupTests.Page

    @classmethod
    def setUpClass(cls):
        cls.page = cls.Page()
        cls.page.feed(INDEX.read_text(encoding="utf-8"))

    def test_the_section_and_the_line_have_unique_ids_labels_and_their_places(self):
        page, order = self.page, self.page.order.index
        self.assertEqual(len(page.order), len(set(page.order)))
        # The section is on the batch page, above the folder runs' and the problem list.
        self.assertIn("jobsPanel", page.parents["batchShared"])
        self.assertLess(order("batchActions"), order("batchShared"))
        self.assertLess(order("batchShared"), order("batchRelatedWorks"))
        self.assertLess(order("batchRelatedWorks"), order("jobsFilter"))
        for identity in ("batchSharedHeading", "batchSharedCount", "batchSharedHelp", "batchSharedList",
                         "batchSharedEmpty", "batchSharedAddFilesButton", "batchSharedAddNoteButton",
                         "batchSharedNoteForm", "batchSharedStatus", "batchSharedFiles"):
            self.assertIn("batchShared", page.parents[identity])
        self.assertEqual(page.elements["batchShared"]["aria-labelledby"], "batchSharedHeading")
        self.assertIn("hidden", page.elements["batchShared"])
        for field in ("Title", "Text"):
            self.assertIn("batchSharedNote" + field, page.labels)
        self.assertEqual(page.elements["batchSharedNoteTitle"]["maxlength"], "200")
        files = page.elements["batchSharedFiles"]
        self.assertEqual((files["type"], "multiple" in files, "hidden" in files), ("file", True, True))
        self.assertEqual(files["aria-label"], "Related works to share with every problem in this batch")
        self.assertNotIn("batchSharedFiles", page.labels)
        self.assertEqual(page.elements["batchSharedStatus"]["role"], "status")
        # The New problem page's line follows the batch it adds to.
        self.assertIn("inputPanel", page.parents["composerShared"])
        self.assertLess(order("composerBatch"), order("composerShared"))
        self.assertLess(order("composerShared"), order("introDescription"))

    def test_the_words_of_the_section(self):
        text = " ".join(INDEX.read_text(encoding="utf-8").split())
        for needed in ('<h2 id="batchSharedHeading" class="batch-related-heading">Shared with every problem in this '
                       'batch</h2>',
                       "Every problem in this batch gets these in its related-works folder: new problems when they "
                       "are created, unfinished ones as soon as you add a work (running and paused authors are told). "
                       "Remove takes a work from new problems and from problems that have not started; running, "
                       "paused and finished problems keep their copy.",
                       '<p id="batchSharedEmpty" class="help" hidden>Nothing is shared yet.</p>',
                       '<h2 id="batchRelatedWorksHeading" class="batch-related-heading">Shared with a folder run</h2>'):
            self.assertIn(needed, text)
        self.assertNotIn("Batch related works", text)

    def test_every_new_element_is_in_the_ui_map(self):
        source = APP.read_text(encoding="utf-8")
        start = source.index("const ui = {")
        mapping = source[start:source.index("\n};", start)]
        for identity in ("composerShared", "batchShared", "batchSharedCount", "batchSharedList", "batchSharedEmpty",
                         "batchSharedAddFilesButton", "batchSharedAddNoteButton", "batchSharedNoteForm",
                         "batchSharedNoteTitle", "batchSharedNoteText", "batchSharedSaveNoteButton",
                         "batchSharedCancelNoteButton", "batchSharedStatus", "batchSharedFiles"):
            with self.subTest(identity=identity):
                self.assertIn(identity, self.page.elements)
                self.assertIn(f'$("{identity}")', mapping)
        self.assertNotRegex(source, re.compile(r"Batch related works"))


if __name__ == "__main__":
    unittest.main()
