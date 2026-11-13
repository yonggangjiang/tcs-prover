"""Batch navigation retains folder identity and keeps archives read-only."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


# More of the page for the tests that need it, cut out by marker text: the real refresh(), the buttons' wiring,
# the Back and Forward handler, and the sidebar's New batch item and its reads of the list of batches.
REFRESH = ['async function refresh()', 'function checkEdited()']
WIRING = ['ui.home.onclick = goHome;', "// The author's question: Send queues"]
POPSTATE = ['window.onpopstate = () => {', '\nrefresh();\n\n// The sticky composer footer']
SIDEBAR_GO = ['  async function go(item)', '  sidebar.addEventListener("click"']
SIDEBAR_POLL = ['  let polled = null;', '  // Finished jobs count as complete']
SIDEBAR_CLICK = ['  sidebar.addEventListener("click", (event) => {', '  // Batch counts: the pages read /batches']


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for browser logic tests')
class BatchNavigationTests(unittest.TestCase):
    def browser(self, checks, slices=(), stub_refresh=True, setup=''):
        """Run ``checks`` against the page's batch code (and ``slices``), with refresh() stubbed unless the real
        one is among the slices. ``setup`` runs before the page's code (what its slices need when they load); it
        sits in a JavaScript template string, so it has no backticks."""
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const piece = ([from, to]) => {
  const start = source.indexOf(from), end = source.indexOf(to, start + 1);
  if (start < 0 || end < 0) throw new Error('Marker not found: ' + (start < 0 ? from : to));
  return source.slice(start, end);
};
const code = source.slice(source.indexOf('function request('), source.indexOf('function selectedProblemMode('))
  // The lists classify each job as the job page does (completedRunOutcome, jobTone).
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function localDateTime('))
  + '\n' + JSON.parse(process.argv[3]).map(piece).join('\n');
class Element {
  constructor() { this.children=[]; this.dataset={}; this.hidden=false; this.textContent=''; this.classList={toggle(){}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  setAttribute(key,value) { this[key]=value; }
  querySelector(selector) { return (this.found ||= {})[selector] ||= new Element(); }
  remove() {}
}
const ui = new Proxy({}, {get:(target,key)=>target[key] ||= new Element()});
const setup = `
let currentJob='', currentBatch='', composingProblem=false, batchCatalog=[], state={phase:'input'};
let requests=Promise.resolve(), previousPhase='', timer, clock, jobsTimer;
const sessionToken='test-token', timelineRows=new Map(), detailRows=new Map();
const location={pathname:'/', search:'', hash:''}, history={urls:[],pushState(a,b,url){this.urls.push(url);}};
const clearTimeout=()=>{}, clearInterval=()=>{}, setTimeout=()=>1;
const document={createElement:()=>new Element()};
const window={open:(...args)=>opened.push(args)};
const localDateTime=value=>value;
const confirm=()=>true, alert=()=>{};
let refreshed=0;
REFRESH_STUB
`;
const sent=[], opened=[];
let response={'/batches':{batches:[]}}, responder=null, finished=false;
const context={ui,assert,Element,URLSearchParams,sent,opened,setImmediate,
  fetch:async(path,options)=>{sent.push({path,options});return {ok:true,json:async()=>responder?responder(path):response[path]};},
  setResponses:value=>response=value,setResponder:value=>responder=value,finish:()=>{finished=true;}};
// Checks left waiting on a promise that never settles end the process quietly: count that as a failure.
process.on('exit',()=>{if(!finished&&!process.exitCode){console.error('The checks did not finish.');process.exitCode=1;}});
Promise.resolve(vm.runInNewContext(setup+code+'\n(async()=>{'+process.argv[2]+'\nfinish();})()',context))
  .catch(error=>{console.error(error);process.exitCode=1;});
""".replace('REFRESH_STUB', ('const refresh=async()=>{refreshed+=1;};' if stub_refresh else '') + setup)
        result = subprocess.run([shutil.which('node'), '-e', script,
                                 str(ROOT / 'ui/app.js'), checks, json.dumps(list(slices))],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_home_shows_batch_cards_with_current_marker_and_safe_labels(self):
        self.browser(r"""
const current={id:'v4',label:'Current <script>batch</script>',current:true,jobCount:10,counts:{running:10}};
renderBatches([current,{id:'v2',label:'Earlier batch',readOnly:true,jobCount:10,counts:{done:10}}]);
assert(!ui.batchesPanel.hidden);assert(ui.input.hidden);assert(ui.jobsPanel.hidden);
assert.equal(ui.batchesList.children.length,2);
const card=ui.batchesList.children[0],link=card.children[0];
assert.match(card.className,/current/);
assert.equal(link.href,'/?batch=v4');
assert.equal(link.children[0].children[1].textContent,'Current batch');
assert.equal(link.children[1].children[0].textContent,current.label);
assert.equal(link.children[1].children[1].textContent,'10 problems');
assert.equal(link.children[2].children[0].textContent,'10 running');
""")

    def test_select_batch_loads_only_its_jobs_and_back_preserves_batch(self):
        self.browser(r"""
setResponses({'/batches':{batches:[{id:'v4',label:'Batch four',current:true}]},
 '/jobs?batch=v4':{jobs:[{runId:'same-name',title:'P1',phase:'running'}]}});
await selectBatch('v4');
assert.equal(sent.map(item=>item.path).join(','),'/batches,/jobs?batch=v4');
assert(ui.batchesPanel.hidden);assert(!ui.jobsPanel.hidden);assert(ui.input.hidden);
assert.equal(ui.jobsHeading.textContent,'Batch four');
assert.equal(ui.jobsList.children.length,1);
selectJob('same-name');
assert.equal(history.urls.at(-1),'/?job=same-name&batch=v4');
assert.equal(jobPath('/state',{after:4}),'/state?after=4&job=same-name&batch=v4');
assert.equal(jobPath('/download-pdf'),'/download-pdf?job=same-name&batch=v4');
assert.equal(jobUrl('same-name',true),'/?job=same-name&batch=v4#test-token');
await goHome();assert.equal(currentJob,'');assert.equal(currentBatch,'v4');
assert.equal(history.urls.at(-1),'/?batch=v4');
await selectBatch('');assert(!ui.batchesPanel.hidden);assert(ui.jobsPanel.hidden);
""")

    def test_archived_cards_hide_mutations_and_request_guard_blocks_posts(self):
        self.browser(r"""
currentBatch='old';renderBatches([{id:'old',label:'Old batch',readOnly:true}]);
renderJobs([{runId:'same-name',phase:'done',canContinueStopped:true,canResumeCritic:true,
 checkpoints:[{label:'Saved proof',resumable:true}]}]);
const card=ui.jobsList.children[0],actions=card.children[1].children;
assert(actions.find(item=>item.textContent==='Delete').hidden);
assert(actions.find(item=>item.textContent==='Continue stopped job').hidden);
assert(actions.find(item=>item.textContent==='Resume at critic').hidden);
assert.equal(card.children[0].children.at(-1).children[0].children.length,1);
assert(!ui.batchReadOnlyNotice.hidden);
await assert.rejects(request('/resume?job=same-name&batch=old',{}),/read-only/);
assert.equal(sent.length,0);
setResponses({'/state?job=same-name&batch=old':{phase:'done'}});
await request('/state?job=same-name&batch=old');assert.equal(sent.length,1);
""")

    def test_home_navigation_does_not_render_a_previous_batch_response(self):
        self.browser(r"""
let release;
currentBatch='v2';batchCatalog=[{id:'v2'}];
setResponder(path=>path==='/batches'?new Promise(resolve=>release=resolve):{jobs:[{runId:'old'}]});
const loading=loadJobs();
await new Promise(resolve=>setImmediate(resolve));
currentBatch='v4';
release({batches:[{id:'v2'}]});
await loading;
assert.equal(sent.length,1);assert.equal(ui.jobsList.children.length,0);
""")

    def test_new_problem_clears_archive_context_and_new_job_uses_response_batch(self):
        self.browser(r"""
currentBatch='old';currentJob='saved';state={readOnly:true};batchCatalog=[{id:'old',readOnly:true}];
await newProblem();
assert.equal(currentBatch,'');assert.equal(currentJob,'');assert(composingProblem);
assert.equal(history.urls.at(-1),'/?new=1');assert(!ui.input.hidden);
assert.equal(state.readOnly,undefined);assert.equal(refreshed,1);
adoptJob({runId:'new-job',batchId:'__root__'});
assert.equal(currentBatch,'__root__');assert(!composingProblem);
assert.equal(history.urls.at(-1),'/?job=new-job&batch=__root__');
""")

    # The batch dialog's browser methods, stubbed as the page has them.
    DIALOG = r"""
Element.prototype.removeAttribute = function (key) { delete this[key]; };
ui.batchDialog.showModal = function () { this.open = true; };
ui.batchDialog.close = function () { this.open = false; };
ui.batchNameInput.focus = () => {};
ui.batchNameInput.select = function () { this.selected = true; };
history.replaceState = (a, b, url) => history.urls.push(url); location.hash = '';
"""

    def test_a_new_batch_opens_ready_for_its_first_problem(self):
        self.browser(self.DIALOG + r"""
const created = {id: 'batch_00000000a1', name: 'Graph coloring', createdAt: '2026-11-10T10:00:00+00:00'};
const card = {id: created.id, label: 'Graph coloring', named: true, addProblems: true, readOnly: false, jobCount: 0};
setResponder(path => path === '/batches' && sent.at(-1).options.method === 'POST'
  ? {batch: created, batches: [card], canCreateBatches: true, batchesProblem: ''}
  : path === '/batches' ? {batches: [card], canCreateBatches: true} : {jobs: []});
// A saved batch's page: making a batch changes nothing in it, so the read-only guard lets it through.
currentBatch = 'old'; batchCatalog = [{id: 'old', readOnly: true}];
openBatchDialog('create');
assert(ui.batchDialog.open); assert.equal(ui.batchDialogTitle.textContent, 'New batch');
assert.equal(ui.saveBatchDialog.textContent, 'Create batch');
assert.equal(ui.batchNameLabel.children.map(child => child.textContent || child).join(''), 'Name (optional)');
assert(!ui.batchNameHelp.hidden); assert.equal(ui.batchNameInput['aria-describedby'], 'batchNameHelp');
assert.equal(ui.batchNameInput.value, '');
ui.batchNameInput.value = '  Graph coloring ';
await saveBatchDialog({preventDefault() {}});
const post = sent.find(item => item.options.method === 'POST');
assert.equal(post.path, '/batches');
assert.deepEqual(JSON.parse(post.options.body), {name: '  Graph coloring '});
assert(!ui.batchDialog.open); assert.equal(ui.saveBatchDialog.disabled, false);
// It opens at once: its page, its actions (empty, so Delete batch too), and New problem in this batch.
assert.equal(currentBatch, created.id); assert.equal(history.urls.at(-1), '/?batch=batch_00000000a1');
assert.equal(ui.jobsHeading.textContent, 'Graph coloring');
assert(!ui.batchActions.hidden); assert(!ui.renameBatch.hidden); assert(!ui.deleteBatch.hidden);
// New problem in this batch: the page says where the problem goes, and its requests name the batch.
await newProblem(currentBatch);
assert.equal(history.urls.at(-1), '/?new=1&batch=batch_00000000a1');
assert.equal(ui.composerBatchText.textContent, 'Adding to batch “Graph coloring”.');
assert(!ui.composerBatch.hidden); assert(!ui.composerDefaultBatch.hidden);
assert(ui.batchActions.hidden);
assert.equal(ui.batchProblems.disabled, false); assert.equal(ui.batchProblems.textContent, 'Graph coloring');
assert.equal(jobPath('/start-workflow'), '/start-workflow?batch=batch_00000000a1');
assert.equal(composerTarget().id, created.id); assert.equal(composerTarget().label, 'Graph coloring');
// Add to the default batch instead: the same page and draft, for the default batch.
addToDefaultBatch();
assert.equal(currentBatch, ''); assert.equal(history.urls.at(-1), '/?new=1'); assert(composingProblem);
assert.equal(ui.composerBatchText.textContent, 'Adding to the default batch.');
assert(ui.composerDefaultBatch.hidden);
assert.equal(jobPath('/start-workflow'), '/start-workflow');
assert.equal(JSON.stringify(composerTarget()), JSON.stringify({id: '', label: 'Default batch'}));
// The started job is shown in its batch.
currentBatch = created.id;
adoptJob({runId: 'job-1', batchId: created.id});
assert.equal(history.urls.at(-1), '/?job=job-1&batch=batch_00000000a1');
""")

    def test_the_default_batch_and_batch_actions(self):
        self.browser(r"""
adoptBatchCatalog({canCreateBatches: true});
const cards = [{id: '__root__', label: 'Default batch', current: true, default: true, addProblems: true, jobCount: 2, counts: {}},
  {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 3, counts: {},
   createdAt: '2026-11-08T09:00:00+00:00', startedAt: '2026-11-09T09:00:00+00:00'},
  {id: 'batch_00000000b2', label: 'Empty', named: true, addProblems: true, jobCount: 0, counts: {},
   createdAt: '2026-11-09T09:00:00+00:00'},
  {id: 'old', label: 'old', readOnly: true, jobCount: 1, counts: {}}];
renderBatches(cards);
assert(!ui.newBatch.hidden); assert(!ui.navNewBatchItem.hidden); assert(ui.batchesStatus.hidden);
// Only the saved batch has a badge: the default batch says so in its name.
const badges = ui.batchesList.children.map(card => card.children[0].children[0].children.slice(1).map(child => child.textContent));
assert.equal(JSON.stringify(badges), JSON.stringify([[], [], [], ['Previous batch · read-only']]));
assert.equal(ui.batchesList.children[0].className, 'batch-card current');
// The default batch with no start date shows none: "Saved research workspace" is a saved batch's.
const times = ui.batchesList.children.map(card => card.children[0].children[3].textContent);
assert.equal(JSON.stringify(times), JSON.stringify(['', 'Started 2026-11-09T09:00:00+00:00',
  'Created 2026-11-09T09:00:00+00:00', 'Saved research workspace']));
for (const [id, actions, rename, remove] of [['__root__', true, false, false], ['batch_00000000a1', true, true, false],
    ['batch_00000000b2', true, true, true], ['old', false, false, false]]) {
  currentBatch = id; renderBatchNavigation();
  assert.equal(JSON.stringify([!ui.batchActions.hidden, !ui.renameBatch.hidden, !ui.deleteBatch.hidden]),
    JSON.stringify([actions, rename, remove]), id);
  assert.equal(ui.selectedBatchBadge.hidden, true, id);
}
// New problem in the default batch's page: the default batch, named in the address.
currentBatch = '__root__';
await newProblem(currentBatch);
assert.equal(history.urls.at(-1), '/?new=1&batch=__root__');
assert.equal(ui.composerBatchText.textContent, 'Adding to the default batch.'); assert(ui.composerDefaultBatch.hidden);
assert.equal(jobPath('/start-workflow'), '/start-workflow?batch=__root__');
// The Overview's New problem adds to the default batch.
await newProblem();
assert.equal(history.urls.at(-1), '/?new=1'); assert.equal(currentBatch, '');
assert.equal(ui.composerBatchText.textContent, 'Adding to the default batch.');
assert(ui.composerDefaultBatch.hidden); assert(ui.batchProblems.hidden);
// Without the server's offer (an older server or the live gateway) nothing new appears.
renderBatches(adoptBatchCatalog({batches: cards}));
assert(ui.newBatch.hidden); assert(ui.navNewBatchItem.hidden); assert(ui.composerBatch.hidden);
currentBatch = '__root__'; composingProblem = false; renderBatchNavigation();
assert(!ui.batchActions.hidden);  // The server's own cards still say which batches take problems.
renderBatches(adoptBatchCatalog({batches: cards.map(({addProblems, ...card}) => card)}));
assert(ui.batchActions.hidden);
""")

    def test_the_empty_default_batch_is_listed_and_opens_with_nothing_made_yet(self):
        self.browser(r"""
const root = {id: '__root__', name: 'Default batch', label: 'Default batch', folder: 'runs', jobCount: 0, counts: {},
  startedAt: '', readOnly: false, addProblems: true, attention: [], queued: 0, sharedWorks: {count: 0, names: []},
  current: true, default: true, runningLimit: 0};
renderBatches(adoptBatchCatalog({batches: [root], canCreateBatches: true, batchesProblem: ''}));
// Its card, with no problem and no date, opens its page (where works can be shared with it).
assert.equal(ui.batchesList.children.length, 1);
const [card] = ui.batchesList.children, link = card.children[0];
assert.equal(link.href, '/?batch=__root__');
assert.equal(link.children[1].children.map(node => node.textContent).join('|'), 'Default batch|0 problems');
assert.equal(link.children[3].textContent, '');
// Its row says it has no problems yet (its empty progress, which the page fills with "No problems yet") ...
assert.equal(link.children[2].className, 'batch-progress');
assert.equal(link.children[2].children.length, 0);
// ... so the line above the table, which still says how to begin as nothing is made yet, does not say it again.
assert(!ui.batchesStatus.hidden);
assert.equal(ui.batchesStatus.textContent, 'Create a problem to get started.');
// Once it has a problem or shares a work, or another batch is listed, the line goes.
for (const batches of [[{...root, jobCount: 1}], [{...root, sharedWorks: {count: 1, names: ['defs.md']}}],
    [root, {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 0, counts: {},
            createdAt: '2026-11-09T09:00:00+00:00'}], [root, {id: 'old', label: 'old', readOnly: true, jobCount: 0}]]) {
  renderBatches(adoptBatchCatalog({batches, canCreateBatches: true, batchesProblem: ''}));
  assert(ui.batchesStatus.hidden, JSON.stringify(batches));
  assert.equal(ui.batchesStatus.textContent, '');
}
// An empty list (a server that lists no batch) says there is none.
renderBatches(adoptBatchCatalog({batches: [], canCreateBatches: false}));
assert(!ui.batchesStatus.hidden);
assert.equal(ui.batchesStatus.textContent, 'No batches yet. Create a problem to get started.');
// Its page opens from the card as any batch's does.
setResponses({'/batches': {batches: [root], canCreateBatches: true, batchesProblem: ''},
  '/jobs?batch=__root__': {jobs: []}});
await selectBatch('__root__');
assert.equal(ui.notice.textContent, '');
assert.equal(ui.batchProblems.textContent, 'Default batch');
""")

    def test_rename_and_delete_batch_keep_the_dialog_open_on_a_refusal(self):
        self.browser(self.DIALOG + r"""
let card = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 0};
let refuse = 'There is already a batch named “Upper bounds”. Choose another name.';
fetch = async (path, options) => {
  sent.push({path, options});
  if (refuse && options.method === 'POST') return {ok: false, json: async () => ({error: refuse})};
  if (path.startsWith('/batches/rename')) card = {...card, label: JSON.parse(options.body).name};
  const answer = path.startsWith('/batches/delete') ? {deleted: card.id, batches: []}
    : path.startsWith('/batches') ? {batch: {id: card.id, name: card.label}, batches: [card], canCreateBatches: true}
    : {jobs: []};
  return {ok: true, json: async () => answer};
};
renderBatches(adoptBatchCatalog({batches: [card], canCreateBatches: true}));
await selectBatch(card.id);
openBatchDialog('rename');
assert.equal(ui.batchDialogTitle.textContent, 'Rename batch'); assert.equal(ui.saveBatchDialog.textContent, 'Rename');
assert.equal(ui.batchNameLabel.children.join(''), 'Name'); assert(ui.batchNameHelp.hidden);
assert.equal(ui.batchNameInput['aria-describedby'], undefined);
assert.equal(ui.batchNameInput.value, 'Graph coloring'); assert(ui.batchNameInput.selected);
ui.batchNameInput.value = 'Upper bounds';
await saveBatchDialog({preventDefault() {}});
// Refused: the dialog stays open with what was typed, and says why.
assert(ui.batchDialog.open); assert.equal(ui.batchNameInput.value, 'Upper bounds');
assert.equal(ui.batchDialogError.textContent, refuse); assert(!ui.batchDialogError.hidden);
assert.equal(sent.at(-1).path, '/batches/rename?batch=batch_00000000a1');
refuse = '';
ui.batchNameInput.value = 'Lower bounds';
await saveBatchDialog({preventDefault() {}});
assert(!ui.batchDialog.open); assert(ui.batchDialogError.hidden);
assert.equal(ui.jobsHeading.textContent, 'Lower bounds'); assert.equal(ui.batchProblems.textContent, 'Lower bounds');
// Delete batch asks first (confirm() answers yes here), then returns to the Overview.
await deleteCurrentBatch();
assert.equal(sent.find(item => item.path.startsWith('/batches/delete')).path, '/batches/delete?batch=batch_00000000a1');
assert.equal(currentBatch, ''); assert.equal(history.urls.at(-1), '/'); assert(!ui.batchesPanel.hidden);
// Rename offers itself only for a batch made with New batch.
currentBatch = '__root__'; batchCatalog = [{id: '__root__', default: true, addProblems: true}];
ui.batchDialog.open = false; openBatchDialog('rename');
assert(!ui.batchDialog.open);
""")

    def test_the_new_problem_page_keeps_its_batch_or_says_it_is_gone(self):
        self.browser(r"""
const named = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 1};
// Before the list of batches arrives, the page names no batch (it would only guess).
batchOptions.value = undefined;
await newProblem('batch_00000000a1');
assert.equal(ui.composerBatchText.textContent, ''); assert(ui.composerBatch.hidden);
// Once it arrives: the batch is named, or said to be gone with the way back to the default batch.
renderBatches(adoptBatchCatalog({batches: [named], canCreateBatches: true}));
assert.equal(ui.composerBatchText.textContent, 'Adding to batch “Graph coloring”.');
renderBatches(adoptBatchCatalog({batches: [], canCreateBatches: true}));
assert.equal(ui.composerBatchText.textContent, 'The batch this page was adding to is no longer available.');
assert(!ui.composerDefaultBatch.hidden);
renderBatches(adoptBatchCatalog({batches: [{id: 'old', readOnly: true}], canCreateBatches: true}));
currentBatch = 'old'; renderBatchNavigation();
assert.equal(ui.composerBatchText.textContent, 'The batch this page was adding to is no longer available.');
// Rerun with suggested statement: the job's batch while it takes problems, else the default batch.
renderBatches(adoptBatchCatalog({batches: [named], canCreateBatches: true}));
globalThis.promptLabels = {};
for (const [batch, readOnly, expected] of [['batch_00000000a1', false, '/?new=1&batch=batch_00000000a1'],
    ['batch_00000000a1', true, '/?new=1'], ['old', false, '/?new=1']]) {
  currentJob = 'job-1'; currentBatch = batch; composingProblem = false;
  state = {readOnly, phase: 'done'};
  await rerunWithStatement('A better statement.');
  assert.equal(history.urls.at(-1), expected, batch + ' ' + readOnly);
}
// A batch's last problem deleted: a batch made with New batch stays on its (now empty) page.
currentJob = ''; currentBatch = 'batch_00000000a1'; composingProblem = false;
renderJobs([{runId: 'r1', title: 'P1', phase: 'done'}]);
assert.equal(ui.jobsList.children.length, 1);
ui.jobsList.children[0].remove = () => { ui.jobsList.children = []; };
setResponder(path => path.startsWith('/delete-job') ? {deleted: 'r1'}
  : path === '/batches' ? {batches: [{...named, jobCount: 0}], canCreateBatches: true} : {jobs: []});
await deleteJob({runId: 'r1'}, 'P1');
assert.equal(ui.jobsList.children.length, 0);
assert.equal(currentBatch, 'batch_00000000a1'); assert.equal(ui.jobsCount.textContent, '0 problems');
// Its page reads the batch's (now empty) list again.
for (let tick = 0; tick < 20 && !sent.some(item => item.path === '/jobs?batch=batch_00000000a1'); tick += 1) {
  await new Promise(resolve => setImmediate(resolve));
}
assert(sent.some(item => item.path === '/jobs?batch=batch_00000000a1'));
""")

    def test_a_batch_made_on_the_new_problem_page_takes_the_problem_being_written(self):
        self.browser(self.DIALOG + r"""
const replaced = [];
history.replaceState = (a, b, url) => { history.urls.push(url); replaced.push(url); };
// Leaving the New problem page throws its related works away (they are discarded on the server).
const discarded = [];
globalThis.clearComposerRelatedWorks = discard => discarded.push(discard);
const created = {id: 'batch_00000000c3', name: 'For my draft', createdAt: '2026-11-10T10:00:00+00:00'};
const card = {id: created.id, label: 'For my draft', named: true, addProblems: true, readOnly: false, jobCount: 0};
setResponder(path => path === '/batches' && sent.at(-1).options.method === 'POST'
  ? {batch: created, batches: [card], canCreateBatches: true, batchesProblem: ''}
  : path === '/batches' ? {batches: [card], canCreateBatches: true} : {jobs: []});
// The New problem page of the default batch, with a statement written and a related work listed.
await newProblem();
batchCatalog = adoptBatchCatalog({batches: [], canCreateBatches: true}); renderBatchNavigation();
discarded.length = 0;
ui.problem.value = 'My carefully typed draft statement.';
const pushes = history.urls.length - replaced.length, opened = refreshed;
assert.equal(ui.composerBatchText.textContent, 'Adding to the default batch.');
// New batch (the sidebar's item or the Overview's button), then Create batch.
openBatchDialog('create');
ui.batchNameInput.value = 'For my draft';
await saveBatchDialog({preventDefault() {}});
assert(!ui.batchDialog.open);
// The page stays and now adds to the new batch: the address, the note and the crumb name it.
assert(composingProblem); assert.equal(currentJob, ''); assert.equal(currentBatch, created.id);
assert.equal(replaced.at(-1), '/?new=1&batch=batch_00000000c3'); assert.equal(history.urls.at(-1), replaced.at(-1));
assert.equal(ui.composerBatchText.textContent, 'Adding to batch “For my draft”.');
assert(!ui.composerDefaultBatch.hidden);
assert(!ui.batchProblems.hidden); assert.equal(ui.batchProblems.textContent, 'For my draft');
assert(!ui.input.hidden); assert(ui.jobsPanel.hidden); assert(ui.batchActions.hidden);
assert.equal(jobPath('/start-workflow'), '/start-workflow?batch=batch_00000000c3');
// What was written stays: the page was not opened again, nothing was thrown away, and no page was added
// to the browser's history.
assert.equal(ui.problem.value, 'My carefully typed draft statement.');
assert.equal(discarded.length, 0); assert.equal(refreshed, opened);
assert.equal(history.urls.length - replaced.length, pushes);
// Made from Models & API (the New problem page's settings): that view stays too.
location.hash = '#models';
openBatchDialog('create');
await saveBatchDialog({preventDefault() {}});
assert.equal(replaced.at(-1), '/?new=1&batch=batch_00000000c3#models'); assert(composingProblem);
assert.equal(discarded.length, 0);
// From any other page the new batch's own page opens, as before.
location.hash = '';
await selectBatch('');
openBatchDialog('create');
await saveBatchDialog({preventDefault() {}});
assert(!composingProblem); assert.equal(history.urls.at(-1), '/?batch=batch_00000000c3');
assert.equal(ui.jobsHeading.textContent, 'For my draft');
""")

    def test_rename_acts_on_the_batch_it_was_opened_for_and_back_closes_the_dialog(self):
        self.browser(self.DIALOG + r"""
const a = {id: 'batch_000000000a', label: 'Batch A', named: true, addProblems: true, jobCount: 0};
const b = {id: 'batch_000000000b', label: 'Batch B', named: true, addProblems: true, jobCount: 0};
setResponder(path => path.startsWith('/batches/rename')
  ? {batch: {id: a.id, name: 'Batch A (lower bounds)'}, batches: [{...a, label: 'Batch A (lower bounds)'}, b],
     canCreateBatches: true}
  : path === '/batches' ? {batches: [a, b], canCreateBatches: true} : {jobs: []});
renderBatches(adoptBatchCatalog({batches: [a, b], canCreateBatches: true}));
currentBatch = a.id; renderBatchNavigation();
openBatchDialog('rename');
assert.equal(ui.batchNameInput.value, 'Batch A');
// The page shows batch B by the time the name is saved (Back or Forward while the dialog was open): the
// dialog still renames the batch it was opened for.
currentBatch = b.id;
ui.batchNameInput.value = 'Batch A (lower bounds)';
await saveBatchDialog({preventDefault() {}});
assert.equal(sent.find(item => item.options.method === 'POST').path, '/batches/rename?batch=batch_000000000a');
assert(!ui.batchDialog.open);
// Back or Forward closes the dialog, whose batch is no longer the page's.
openBatchDialog('rename');
assert(ui.batchDialog.open); assert.equal(ui.batchNameInput.value, 'Batch B');
location.search = '?batch=batch_000000000a';
window.onpopstate();
assert(!ui.batchDialog.open); assert.equal(currentBatch, a.id);
""", slices=[POPSTATE])

    def test_attention_links_name_each_jobs_own_batch(self):
        self.browser(r"""
const items = [
  {runId: 'root-job', job: 'P1.md', kind: 'login', title: 'Codex login needed', text: 'Log in.', batchId: '__root__'},
  {runId: 'older-server-job', job: 'P3.md', kind: 'login', title: 'Codex login needed', text: 'Log in.'},
  {runId: 'named-job', job: 'P2.md', kind: 'usage', title: 'Usage limit', text: 'Wait.', batchId: 'batch_00000000a1'}];
const cards = [{id: '__root__', label: 'Default batch', default: true, current: true, addProblems: true, jobCount: 2,
                counts: {}, attention: items.slice(0, 2)},
               {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 1,
                counts: {}, attention: items.slice(2)}];
const links = () => JSON.stringify(ui.attentionList.children.map(row => row.children[1].href));
const expected = JSON.stringify(['/?job=root-job&batch=__root__', '/?job=older-server-job',
  '/?job=named-job&batch=batch_00000000a1']);
// The list is built while a batch's page is open (its poll renders it too), then shown on the Overview:
// each link names its own job's batch, never the page's.
currentBatch = 'batch_00000000a1';
renderBatches(adoptBatchCatalog({batches: cards, canCreateBatches: true}));
assert.equal(links(), expected);
currentBatch = '';
renderBatches(adoptBatchCatalog({batches: cards, canCreateBatches: true}));
assert.equal(links(), expected);
currentBatch = '__root__';
renderBatches(adoptBatchCatalog({batches: cards, canCreateBatches: true}));
assert.equal(links(), expected);
""")

    def test_the_new_problem_page_hears_that_its_batch_was_deleted_elsewhere(self):
        self.browser(r"""
let syncs = 0;
globalThis.sync = () => { syncs += 1; };
const root = {id: '__root__', label: 'Default batch', default: true, current: true, addProblems: true, jobCount: 1};
const gone = {id: 'batch_00000000d4', label: 'Soon gone', named: true, addProblems: true, jobCount: 0};
let listed = [root, gone];
setResponder(path => path === '/batches' ? {batches: listed, canCreateBatches: true} : {});
// The sidebar's read alone tells the page what the server offers (a page that has read no list yet).
await pollBatches();
assert(batchOptions().create); assert(batchOptions().loaded);
renderBatches(adoptBatchCatalog({batches: listed, canCreateBatches: true}));
await newProblem(gone.id);
assert.equal(ui.composerBatchText.textContent, 'Adding to batch “Soon gone”.');
assert(!ui.batchProblems.hidden); assert.equal(ui.batchProblems.textContent, 'Soon gone');
// Another tab deletes the batch. The sidebar reads the list every few seconds, and the page with it.
listed = [root];
await pollBatches();
assert.equal(ui.composerBatchText.textContent, 'The batch this page was adding to is no longer available.');
assert(!ui.composerDefaultBatch.hidden);
assert(ui.batchProblems.hidden);  // The crumb names no batch that is gone.
assert(composerBatchGone());
assert(syncs > 0);
// A refused start reads the list again at once (here the batch is back, as after an undo elsewhere).
listed = [root, gone];
await reloadComposerBatches();
assert.equal(ui.composerBatchText.textContent, 'Adding to batch “Soon gone”.'); assert(!composerBatchGone());
listed = [root];
await reloadComposerBatches();
assert.equal(ui.composerBatchText.textContent, 'The batch this page was adding to is no longer available.');
// The page took a list of its own while the sidebar's read was on its way (New batch, say): the sidebar's
// answer may be older, so neither the page nor the sidebar takes it.
const made = {id: 'batch_00000000e5', label: 'Made meanwhile', named: true, addProblems: true, jobCount: 0};
currentBatch = made.id;
setResponder(path => {
  batchCatalog = adoptBatchCatalog({batches: [root, made], canCreateBatches: true});
  return {batches: [root], canCreateBatches: true};
});
await pollBatches();
assert.equal(selectedBatch()?.label, 'Made meanwhile');
assert.equal(JSON.stringify(catalog().map(batch => batch.id)), JSON.stringify(['__root__', made.id]));
// The next read is taken again, by the sidebar and by the page.
setResponder(path => ({batches: [root, made, gone], canCreateBatches: true}));
await pollBatches();
assert.equal(JSON.stringify(catalog().map(batch => batch.id)), JSON.stringify(['__root__', made.id, gone.id]));
assert.equal(batchCatalog.length, 3);
// Away from the New problem page, the sidebar's read leaves the page's own list alone.
composingProblem = false; currentJob = 'job-1';
setResponder(path => ({batches: [root], canCreateBatches: true}));
await pollBatches();
assert.equal(batchCatalog.length, 3);
""", slices=[SIDEBAR_POLL])

    def test_new_batch_in_the_phone_drawer_leaves_the_focus_in_its_dialog(self):
        self.browser(r"""
const event = nav => ({target: {closest: selector => selector === '.nav-item' ? {tagName: 'BUTTON', dataset: {nav}} : null},
  button: 0, preventDefault() {}});
for (const nav of ['new-batch', 'prompts', 'home']) {
  drawer = true;
  sidebarClick(event(nav));
  await new Promise(resolve => setImmediate(resolve));
}
// The drawer closed each time; only the page (Overview) took the focus: a dialog keeps its own.
assert.equal(JSON.stringify(went), JSON.stringify(['new-batch', 'prompts', 'home']));
assert.equal(focused, 1); assert.equal(drawer, false);
""", slices=[SIDEBAR_CLICK], setup="""
let drawer = true, focused = 0, sidebarClick = null;
const went = [];
const sidebar = {addEventListener: (type, handler) => { if (type === 'click') sidebarClick = handler; }};
const setDrawer = open => { drawer = open; }, hideTip = () => {}, sync = () => {};
const go = async item => { went.push(item.dataset.nav); };
document.getElementById = id => id === 'main' ? {focus: () => { focused += 1; }} : null;
""")

    def test_the_buttons_and_the_sidebar_open_what_they_say(self):
        self.browser(self.DIALOG + r"""
const card = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 0};
setResponder(path => path === '/batches' ? {batches: [card], canCreateBatches: true} : {jobs: []});
// The batch page's New problem in this batch: the page adds to that batch.
await selectBatch(card.id);
await ui.newProblemInBatch.onclick();
assert.equal(history.urls.at(-1), '/?new=1&batch=batch_00000000a1'); assert.equal(currentBatch, card.id);
assert.equal(ui.composerBatchText.textContent, 'Adding to batch “Graph coloring”.');
// The Overview's New problem, clicked: the click is no batch, and the page adds to the default batch.
await selectBatch('');
await ui.newProblem.onclick({type: 'click', button: 0});
assert.equal(history.urls.at(-1), '/?new=1'); assert.equal(currentBatch, '');
assert.equal(ui.composerBatchText.textContent, 'Adding to the default batch.');
// The sidebar's New batch, and the Overview's: the dialog opens for a new batch.
let syncs = 0;
globalThis.sync = () => { syncs += 1; };
await go({dataset: {nav: 'new-batch'}});
assert(ui.batchDialog.open); assert.equal(ui.batchDialogTitle.textContent, 'New batch'); assert(syncs > 0);
ui.cancelBatchDialog.onclick();
assert(!ui.batchDialog.open);
ui.newBatch.onclick();
assert(ui.batchDialog.open); assert.equal(ui.saveBatchDialog.textContent, 'Create batch');
assert.equal(ui.batchDialog.querySelector('form').onsubmit, saveBatchDialog);
ui.batchDialog.close();
// Rename and Delete batch act on the batch whose page is open.
await selectBatch(card.id);
ui.renameBatch.onclick();
assert(ui.batchDialog.open); assert.equal(ui.batchNameInput.value, 'Graph coloring');
ui.batchDialog.close();
assert.equal(ui.deleteBatch.onclick, deleteCurrentBatch);
// Add to the default batch instead, on the New problem page.
await ui.newProblemInBatch.onclick();
ui.composerDefaultBatch.onclick();
assert.equal(currentBatch, ''); assert.equal(history.urls.at(-1), '/?new=1'); assert(composingProblem);
""", slices=[WIRING, SIDEBAR_GO])

    def test_the_page_reads_its_batch_before_naming_it_and_follows_the_server(self):
        self.browser(r"""
history.replaceState = (a, b, url) => history.urls.push(url);
const rendered = [];
globalThis.render = next => rendered.push(next);
globalThis.loadJobRecovery = () => {};
const root = {id: '__root__', label: 'Default batch', default: true, current: true, addProblems: true, jobCount: 1};
const card = {id: 'batch_00000000a1', label: 'Graph coloring', named: true, addProblems: true, jobCount: 1};
// A reload of the New problem page of a batch: the list of batches first, then the note and the crumb.
currentBatch = card.id; composingProblem = true;
setResponder(path => path === '/batches' ? {batches: [root, card], canCreateBatches: true} : {phase: 'input'});
await refresh();
assert.equal(sent[0].path, '/batches');
assert.equal(ui.composerBatchText.textContent, 'Adding to batch “Graph coloring”.');
assert(!ui.batchProblems.hidden); assert.equal(ui.batchProblems.textContent, 'Graph coloring');
// A job link naming a batch that is no longer listed: the page takes the batch the server shows the job in.
composingProblem = false; currentJob = 'job-1'; currentBatch = 'batch_00000000ff'; batchCatalog = [];
setResponder(path => path === '/batches' ? {batches: [root, card], canCreateBatches: true}
  : {runId: 'job-1', phase: 'done', batchId: '__root__'});
await refresh();
assert.equal(currentBatch, '__root__'); assert.equal(history.urls.at(-1), '/?job=job-1&batch=__root__');
assert.equal(rendered.at(-1).runId, 'job-1');
// A job link naming another listed batch, which the job is not in (a link kept from before the list was
// repaired): the server refuses it, the page asks again without the batch and takes the job's own.
currentJob = 'job-2'; currentBatch = '__root__';
const asked = [];
fetch = async (path, options) => {
  sent.push({path, options}); asked.push(path);
  if (path.startsWith('/state') && path.includes('batch=')) {
    return {ok: false, status: 404, json: async () => ({error: 'That job is not in this batch.'})};
  }
  return {ok: true, json: async () => ({runId: 'job-2', phase: 'done', batchId: card.id})};
};
await refresh();
assert.equal(JSON.stringify(asked), JSON.stringify(['/state?after=0&job=job-2&batch=__root__', '/state?after=0&job=job-2']));
assert.equal(currentBatch, card.id); assert.equal(history.urls.at(-1), '/?job=job-2&batch=batch_00000000a1');
assert.notEqual(ui.notice.textContent, 'That job is not in this batch.');
// Any other refusal is shown, and the page tries again later.
fetch = async (path, options) => ({ok: false, status: 404, json: async () => ({error: 'Unknown batch, job, or saved artifact.'})});
currentJob = 'job-3'; currentBatch = 'old';
await refresh();
assert.equal(currentBatch, 'old'); assert.equal(ui.notice.textContent, 'Unknown batch, job, or saved artifact.');
""", slices=[REFRESH], stub_refresh=False)

    def test_the_overview_warns_above_the_table_when_the_list_cannot_be_read(self):
        self.browser(r"""
const problem = 'The list of batches (/runs/.controller/batches.json) cannot be read: it is not valid JSON '
  + '(line 1, column 2). Until it is repaired and TCS Prover is started again, batches cannot be made, renamed or '
  + 'deleted, and the problems of the batches it lists are shown in the default batch.';
const root = {id: '__root__', label: 'Default batch', default: true, current: true, addProblems: true, jobCount: 1,
              counts: {}};
renderBatches(adoptBatchCatalog({batches: [root], canCreateBatches: false, batchesProblem: problem}));
assert.equal(ui.batchesProblem.textContent, problem); assert(!ui.batchesProblem.hidden);
// The table's own status line keeps to loading and empty lists.
assert(ui.batchesStatus.hidden); assert.equal(ui.batchesStatus.textContent, '');
assert(ui.newBatch.hidden); assert(ui.navNewBatchItem.hidden);
// Unchanged lists keep the warning (the cards are not rebuilt).
renderBatches(adoptBatchCatalog({batches: [root], canCreateBatches: false, batchesProblem: problem}));
assert(!ui.batchesProblem.hidden);
// No batch at all: the warning, and the table's empty text.
renderBatches(adoptBatchCatalog({batches: [], canCreateBatches: false, batchesProblem: problem}));
assert(!ui.batchesProblem.hidden);
assert(!ui.batchesStatus.hidden); assert.equal(ui.batchesStatus.textContent, 'No batches yet. Create a problem to get started.');
// A list that can be read: no warning.
renderBatches(adoptBatchCatalog({batches: [root], canCreateBatches: true, batchesProblem: ''}));
assert(ui.batchesProblem.hidden); assert.equal(ui.batchesProblem.textContent, ''); assert(!ui.newBatch.hidden);
""")


class BatchMarkupTests(unittest.TestCase):
    def test_jobs_panel_is_independent_of_composer_and_labels_are_unique(self):
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sections, self.ids, self.parents = [], [], {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.ids.append(attrs['id'])
                    self.parents[attrs['id']] = list(self.sections)
                if tag == 'section':
                    self.sections.append(attrs.get('id'))

            def handle_endtag(self, tag):
                if tag == 'section':
                    self.sections.pop()

        page = Markup()
        page.feed((ROOT / 'ui/index.html').read_text())
        self.assertEqual(len(page.ids), len(set(page.ids)))
        self.assertNotIn('inputPanel', page.parents['jobsPanel'])
        for identifier in ('batchesPanel', 'batchesList', 'batchNavigation', 'newProblemButton'):
            self.assertIn(identifier, page.ids)

    def test_the_unreadable_list_warning_is_a_notice_above_the_table(self):
        html = (ROOT / 'ui/index.html').read_text()
        warning = html.index('id="batchesProblem"')
        self.assertLess(warning, html.index('batch-table-head'))
        self.assertLess(warning, html.index('id="batchesStatus"'))
        tag = html[html.rindex('<', 0, warning):html.index('>', warning)]
        self.assertIn('class="notice batches-problem"', tag)
        self.assertIn('hidden', tag)

    def test_the_status_line_sits_above_the_table_never_between_its_heading_row_and_its_rows(self):
        # Nothing made yet, the line shows while the table lists the default batch's row (and so its heading row).
        html = (ROOT / 'ui/index.html').read_text()
        line, head, rows = (html.index(marker) for marker in ('id="batchesStatus"', 'batch-table-head',
                                                               'id="batchesList"'))
        self.assertLess(line, head)
        self.assertLess(head, rows)
        # With a gap under it, off the table's top edge.
        self.assertIn('.batches-panel #batchesStatus { margin-bottom: var(--space-3); }',
                      (ROOT / 'ui/styles.css').read_text())


if __name__ == '__main__':
    unittest.main()
