"""Batch navigation retains folder identity and keeps archives read-only."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for browser logic tests')
class BatchNavigationTests(unittest.TestCase):
    def browser(self, checks):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function request('), source.indexOf('function selectedProblemMode('));
class Element {
  constructor() { this.children=[]; this.dataset={}; this.hidden=false; this.textContent=''; this.classList={toggle(){}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  setAttribute(key,value) { this[key]=value; }
  remove() {}
}
const ui = new Proxy({}, {get:(target,key)=>target[key] ||= new Element()});
const setup = `
let currentJob='', currentBatch='', composingProblem=false, batchCatalog=[], state={phase:'input'};
let requests=Promise.resolve(), previousPhase='', timer, clock, jobsTimer, memoryRequest=0, memoryTimer, memoryJob=null;
const sessionToken='test-token', timelineRows=new Map(), detailRows=new Map();
const location={pathname:'/'}, history={urls:[],pushState(a,b,url){this.urls.push(url);}};
const clearTimeout=()=>{}, clearInterval=()=>{}, setTimeout=()=>1;
const document={createElement:()=>new Element()};
const window={open:(...args)=>opened.push(args)};
const localDateTime=value=>value;
const completedRunLabel=job=>job.resultKind==='final'?'Answer ready':'Incomplete';
const confirm=()=>true, alert=()=>{};
let refreshed=0;
const refresh=async()=>{refreshed+=1;};
`;
const sent=[], opened=[];
let response={'/batches':{batches:[]}}, responder=null;
const context={ui,assert,Element,URLSearchParams,sent,opened,setImmediate,
  fetch:async(path,options)=>{sent.push({path,options});return {ok:true,json:async()=>responder?responder(path):response[path]};},
  setResponses:value=>response=value,setResponder:value=>responder=value};
Promise.resolve(vm.runInNewContext(setup+code+'\n(async()=>{'+process.argv[2]+'})()',context))
  .catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script,
                                 str(ROOT / 'ui/app.js'), checks],
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


if __name__ == '__main__':
    unittest.main()
