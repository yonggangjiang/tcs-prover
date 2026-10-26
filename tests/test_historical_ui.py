"""Exercise historical controls and run cards without starting model work."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for browser logic tests')
class HistoricalFrontendTests(unittest.TestCase):
    def browser(self, checks):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.value = ''; this.textContent = '';
    this.hidden = false; this.checked = false; this.disabled = false; this.required = false;
    this.details = {open:false}; this.option = {disabled:false}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelector() { return this.option; }
  querySelectorAll(tag) { return this.children.flatMap(child => [
    ...(child.tagName === tag ? [child] : []), ...child.querySelectorAll(tag)]); }
  closest() { return this.details; }
  focus() { this.focused = true; }
}
const elements = {}, ui = new Proxy(elements, {get:(target,key) => target[key] ||= new Element()});
ui.researchAuditModels = [new Element(),new Element(),new Element()];
ui.retrievalMode.value = 'live'; ui.skipStatementReview.checked = true;
for (const role of ['author','critic','writer']) ui[role+'Model'].value = 'gpt-6.1-sol';
const state = {phase:'running',stage:'solve',activeNode:'author',problemMode:'statement',
  runId:'fixture-run',draft:'# A theorem',statement:'A precise theorem',thinkingHours:0,
  activeElapsedSeconds:15,processAlive:true,workerActive:true,trace:[],criticRounds:2,
  workflow:{nodes:{author:{label:'Proof author'}}},
  retrievalCapabilities:{historicalAvailable:true},
  retrieval:{mode:'disabled',fixed:false,available:true}};
let mode = 'statement', auditModels = ['none','none','none'], managed = false;
const sent = [];
const context = {ui,state,assert,Date,Number,sent,document:{createElement:tag=>new Element(tag)},
  show:(element,visible)=>element.hidden=!visible,selectedProblemMode:()=>mode,
  managesResearchFiles:()=>managed,researchAuditValues:()=>({models:auditModels}),
  promptOverrides:{},stateReceivedAt:Date.now(),describe:()=>null,clockText:()=>'',
  timer:null,jobsTimer:null,clearTimeout:()=>{},promptValues:{final:'Writer prompt'},
  request:async(path,body)=>{sent.push({path,body});return {runId:'created'};},
  adoptJob:()=>{},render:()=>{},
  setMode:value=>mode=value,setAudits:value=>auditModels=value,setManaged:value=>managed=value};
const selected = source.slice(source.indexOf('function retrievalConfiguration('), source.indexOf('function renderJobs('))
  + source.slice(source.indexOf('function selectedRetrievalMode()'), source.indexOf('// Keep the compact footer'))
  + source.slice(source.indexOf('function describe(entry)'), source.indexOf('function cardIcon('))
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('// Show the author/audit cycle'))
  + source.slice(source.indexOf('function renderResearchAudits('), source.indexOf('function renderResearchAuditActivity('))
  + source.slice(source.indexOf('function proofJobSettings()'), source.indexOf('// Read files directly'))
  + source.slice(source.indexOf('async function startLatexOnly()'), source.indexOf('async function act('));
Promise.resolve(vm.runInNewContext(selected + '\n(async()=>{' + process.argv[2] + '})()', context))
  .catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script,
                                 str(ROOT / 'ui/app.js'), checks],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_critic_timeline_keeps_repeated_round_numbers_and_never_invents_a_pass(self):
        self.browser(r"""
const first = describe({kind:'critic_result',round:1,time:'2026-10-16T13:00:00Z',report:{verdict:'reject',bugs:'Missing proof.'}});
const second = describe({kind:'critic_result',round:1,time:'2026-10-16T13:15:00Z',report:{verdict:'pass'}});
assert.notEqual(first.key,second.key);
assert.equal(first.text,'Missing proof.');
assert.match(second.text,/Pass — edits and next step not recorded/);
assert.match(describe({kind:'critic_result',report:{}}).text,/No valid critic verdict/);
assert.equal(describe({kind:'critic_result',report:{verdict:'REJECT',bugs:'Unresolved gap.'}}).text,'Unresolved gap.');
state.trace = [
  {kind:'critic_result',time:'first',round:1,outcome:'fixed',report:{verdict:'pass'}},
  {kind:'status',node:'critic',label:'Workflow step: critic',visit:2},
  {kind:'critic_result',time:'second',round:2,outcome:'fixed',report:{verdict:'pass'}},
  {kind:'status',label:'Critic approved',acceptanceReason:'round_limit',roundLimit:2}
];
const edited = describe(state.trace[0]), accepted = describe(state.trace[2]);
assert.match(edited.label,/Review cycle 1 · round 1.*Pass with fixes/);
assert.match(accepted.label,/Review cycle 1 · round 2.*accepted at review limit/);
assert.match(accepted.text,/without another critic round/);
state.trace = [{kind:'critic_result',time:'clean',outcome:'pass',report:{verdict:'pass'}}];
assert.match(describe(state.trace[0]).label,/Clean pass/);
""")

    def test_unrestricted_default_hides_cutoff_and_omits_stale_value_from_request(self):
        self.browser(r"""
ui.retrievalCutoffUtc.value = '2026-10-02T14:13:12Z';
renderRetrievalSettings();
assert(ui.retrievalCutoffSetting.hidden);
assert(ui.retrievalCutoffUtc.disabled);
assert.equal(proofJobSettings().retrievalMode,'live');
assert.equal(proofJobSettings().retrievalCutoffUtc,'');
assert(validateRetrievalSettings());
""")

    def test_missing_backend_capability_fails_closed_even_if_mode_was_manually_selected(self):
        self.browser(r"""
delete state.retrievalCapabilities;
ui.retrievalMode.value = 'historical'; ui.retrievalCutoffUtc.value = '2020-01-01T00:00:00Z';
renderRetrievalSettings();
assert(!ui.retrievalMode.option.disabled); // -1 remains usable without archive support.
assert.equal(validateRetrievalSettings(),false);
assert.match(ui.notice.textContent,/updated TCS Prover interface/);
""")

    def test_explicit_utc_cutoff_is_preserved_without_timezone_or_microsecond_loss(self):
        self.browser(r"""
ui.retrievalMode.value = 'historical';
for (const value of ['2020-01-01T00:00:00Z','2020-01-01T00:00:00.000001Z','2020-01-01T00:00:00+00:00']) {
  ui.retrievalCutoffUtc.value=value;
  assert(validateRetrievalSettings());
  assert.equal(proofJobSettings().retrievalCutoffUtc,value);
}
renderRetrievalSettings();
assert(!ui.retrievalCutoffSetting.hidden);
assert(ui.retrievalCutoffUtc.required);
""")

    def test_ambiguous_invalid_or_future_cutoffs_are_rejected_before_submission(self):
        self.browser(r"""
ui.retrievalMode.value='historical';
for (const value of ['', '2020-01-01','2020-01-01T00:00:00','2020-02-30T00:00:00Z',
  '2020-01-01T24:00:00Z','2020-01-01T00:00:00+02:00','2020-01-01T00:00:00.1234567Z',
  '2999-01-01T00:00:00Z']) {
  ui.retrievalCutoffUtc.value=value;
  assert.equal(validateRetrievalSettings(),false,value);
}
""")

    def test_direct_codex_only_validation_does_not_silently_change_selected_models(self):
        self.browser(r"""
ui.retrievalMode.value='historical';ui.retrievalCutoffUtc.value='2020-01-01T00:00:00Z';
ui.skipStatementReview.checked=false;
assert.equal(validateRetrievalSettings(),false);
assert(validateRetrievalSettings({folder:true}));
ui.skipStatementReview.checked=true;
// Web cutoff timestamps need native Codex CLI models: not Claude CLI models, not API models.
for (const model of ['claude-opus-5-5','api:lab-sol']) {
  ui.criticModel.value=model;ui.criticModel.focused=false;
  assert.equal(validateRetrievalSettings(),false);
  assert.equal(ui.criticModel.value,model);
  assert.equal(ui.notice.textContent,'Web cutoff timestamps require Codex CLI models for every proof role. '
    +'Choose a Codex CLI model for the independent critic.');
  assert.equal(ui.criticModel.focused,true);
}
ui.criticModel.value='gpt-6.1-sol';setManaged(true);setAudits(['gpt-6-astra','none','none']);
assert.equal(validateRetrievalSettings(),false);
setAudits(['none','none','none']);assert(validateRetrievalSettings());
""")

    def test_disabled_cutoff_is_available_without_archive_support_and_preserves_all_modes(self):
        self.browser(r"""
delete state.retrievalCapabilities;
ui.retrievalMode.value='historical';ui.retrievalCutoffUtc.value=' -1 ';
ui.skipStatementReview.checked=false;ui.criticModel.value='claude-opus-5-5';
setManaged(true);setAudits(['gpt-6-astra','none','none']);
renderRetrievalSettings();
assert(!ui.retrievalCutoffSetting.hidden);assert(!ui.retrievalCutoffUtc.disabled);
assert.equal(proofJobSettings().retrievalMode,'disabled');
assert.equal(proofJobSettings().retrievalCutoffUtc,'-1');
assert(validateRetrievalSettings());
for (const mode of ['statement','latex']) {
  setMode(mode);
  assert.equal(proofJobSettings().retrievalMode,'disabled');
  assert.equal(proofJobSettings().retrievalCutoffUtc,'-1');
  assert(validateRetrievalSettings());
}
setMode('statement');ui.statementReviewOnly.checked=true;
assert.equal(proofJobSettings().retrievalMode,'disabled');
assert(validateRetrievalSettings());
ui.retrievalMode.value='live';
assert.equal(proofJobSettings().retrievalMode,'live');
assert.equal(proofJobSettings().retrievalCutoffUtc,'');
assert(validateRetrievalSettings());
""")

    def test_latex_and_review_only_reject_unsupported_historical_choice_without_changing_it(self):
        self.browser(r"""
ui.retrievalMode.value='historical';ui.retrievalCutoffUtc.value='2020-01-01T00:00:00Z';
setMode('latex');assert.equal(proofJobSettings().retrievalMode,'historical');
assert.equal(proofJobSettings().retrievalCutoffUtc,'2020-01-01T00:00:00Z');
assert.equal(validateRetrievalSettings(),false);
assert.match(ui.notice.textContent,/full proof job/);
setMode('statement');ui.statementReviewOnly.checked=true;
assert.equal(proofJobSettings().retrievalMode,'historical');
assert.equal(validateRetrievalSettings(),false);
assert.match(ui.notice.textContent,/full proof job/);
""")

    def test_new_jobs_default_live_but_saved_modes_restore_without_changing_search_access(self):
        self.browser(r"""
restoreRetrievalSettings({});
assert.equal(ui.retrievalMode.value,'live');assert.equal(ui.retrievalCutoffUtc.value,'');
restoreRetrievalSettings({runId:'old-main'});
assert.equal(ui.retrievalMode.value,'live');assert.equal(selectedRetrievalMode(),'live');
for (const saved of [
  {runId:'old',retrievalMode:'disabled'},
  {runId:'old',retrieval:{mode:'disabled'},retrievalCutoffUtc:''}
]) {
  restoreRetrievalSettings(saved);
  assert.equal(ui.retrievalMode.value,'historical');assert.equal(ui.retrievalCutoffUtc.value,'-1');
  assert.equal(selectedRetrievalMode(),'disabled');
}
restoreRetrievalSettings({runId:'native',retrieval:{mode:'live'}});
assert.equal(ui.retrievalMode.value,'live');assert.equal(selectedRetrievalMode(),'live');
restoreRetrievalSettings({runId:'archived',retrievalMode:'historical',retrievalCutoffUtc:'2020-01-01T00:00:00.123456Z'});
assert.equal(ui.retrievalMode.value,'historical');
assert.equal(ui.retrievalCutoffUtc.value,'2020-01-01T00:00:00.123456Z');
assert.equal(selectedRetrievalMode(),'historical');
restoreRetrievalSettings({runId:'legacy',retrievalPolicyPath:'/policy.json'});
assert.equal(ui.retrievalMode.value,'historical');assert.equal(ui.retrievalCutoffUtc.value,'');
assert.equal(selectedRetrievalMode(),'historical');
""")

    def test_latex_start_sends_search_choice_and_blocks_unsupported_historical_dates(self):
        self.browser(r"""
setMode('latex');ui.latexInput.value='An existing theorem and proof.';
await startLatexOnly();
assert.equal(sent.at(-1).path,'/finalize');
assert.equal(sent.at(-1).body.retrievalMode,'live');
assert.equal(sent.at(-1).body.retrievalCutoffUtc,'');
ui.retrievalMode.value='historical';ui.retrievalCutoffUtc.value='-1';
await startLatexOnly();
assert.equal(sent.length,2);
assert.equal(sent.at(-1).body.retrievalMode,'disabled');
assert.equal(sent.at(-1).body.retrievalCutoffUtc,'-1');
ui.retrievalCutoffUtc.value='2020-01-01T00:00:00Z';
await startLatexOnly();
assert.equal(sent.length,2);assert.match(ui.notice.textContent,/full proof job/);
""")

    def test_overview_preserves_zero_unlimited_and_unknown_process_telemetry(self):
        self.browser(r"""
state.processAlive=null;state.workerActive=null;renderRunOverview();
assert.equal(ui.runBudget.textContent,'Unlimited');
assert.match(ui.runHealth.textContent,/Unknown/);
assert.equal(ui.runStatement.textContent,'A precise theorem');
assert(!ui.originalStatementDetails.hidden);
assert.equal(ui.runOriginalStatement.textContent,'# A theorem');
state.sourceFile='statements/P10.md';renderRunOverview();
assert.equal(ui.runProblemTitle.textContent,'P10.md');
assert.equal(ui.runStatement.textContent,'A precise theorem');
""")

    def test_historical_runs_do_not_offer_unsupported_optional_audits(self):
        self.browser(r"""
setManaged(true);state.retrieval={mode:'historical'};
renderResearchAudits(true);
for (const name of ['researchAuditsControl','researchAuditToolbar','researchAuditActivity','researchAuditStatus']) {
  assert(ui[name].hidden);
  ui[name].hidden=false;
}
delete state.retrieval;state.retrievalPolicyPath='/controller/policy.json';
renderResearchAudits(true);
for (const name of ['researchAuditsControl','researchAuditToolbar','researchAuditActivity','researchAuditStatus']) {
  assert(ui[name].hidden);
}
""")

    def test_prepared_hold_and_incomplete_output_are_never_presented_as_a_finished_answer(self):
        self.browser(r"""
state.phase='prepared';state.launchHold='Awaiting controller review';renderRunOverview();
assert.equal(ui.runStage.textContent,'Awaiting review');
assert.equal(ui.runNextStep.textContent,state.launchHold);
state.phase='done';state.resultKind='failure';state.output='Preserved failure report';renderRunOverview();
assert.equal(completedRunLabel(state),'Incomplete');
assert.equal(ui.resultHeading.textContent,'Failure report');
assert.equal(ui.resultOutput.textContent,state.output);
assert(ui.resultPanel.open);
""")

    def test_retrieval_card_shows_exact_policy_and_marks_candidate_counts_as_distinct_from_delivery(self):
        self.browser(r"""
state.retrieval={mode:'historical',fixed:true,available:true,cutoffExclusiveUtc:'2026-10-02T14:13:12.000001Z',
 counts:{searches:5,opens:2,finds:3,successes:4,errors:1,pending:2,verifiedCandidates:19},limitation:'Coverage varies.'};
renderRetrievalOverview();
assert.equal(ui.retrievalCutoffValue.textContent,state.retrieval.cutoffExclusiveUtc);
assert.equal(ui.retrievalStatus.textContent,'1 failed request in history');
assert.equal(ui.retrievalCounts.children.length,7);
assert.match(ui.retrievalCounts.children.flatMap(row=>row.children).map(item=>item.textContent).join(' '),/Find requests 3/);
assert.match(ui.retrievalCountNote.textContent,/never returned/);
assert.match(ui.retrievalCountNote.textContent,/no terminal event/);
assert.match(ui.retrievalPolicyNote.textContent,/including resumes/);
assert.equal(ui.retrievalLimitation.textContent,'Coverage varies.');
assert.match(ui.retrievalRequestSummary.textContent,/over this run/);
assert.match(ui.retrievalRequestSummary.textContent,/does not by itself/);
""")

    def test_success_after_failed_opens_shows_latest_success_and_preserves_failure_history(self):
        self.browser(r"""
state.retrieval={mode:'historical',fixed:true,available:true,
 counts:{searches:7,opens:14,finds:8,successes:26,errors:3,pending:0,verifiedCandidates:23},
 lastCompleted:{tool:'find',status:'success'},
 requestFailures:[{code:'no_capture',label:'No verified pre-cutoff capture',count:1},
 {code:'transport_failure',label:'Archive connection failed',count:1},
 {code:'response_too_large',label:'Archive response exceeded the transport limit',count:1}]};
renderRetrievalOverview();
assert.equal(ui.retrievalStatus.textContent,'Last find succeeded');
assert.equal(ui.retrievalStatus.dataset.tone,'neutral');
assert.match(ui.retrievalRequestSummary.textContent,/26 successful and 3 failed/);
assert.equal(ui.retrievalFailures.children.length,3);
assert.match(ui.retrievalFailures.children.map(row=>row.textContent).join(' '),/transport limit/);
state.retrieval.lastCompleted={tool:'open',status:'error'};
renderRetrievalOverview();
assert.equal(ui.retrievalStatus.textContent,'Last open failed');
assert.equal(ui.retrievalStatus.dataset.tone,'warning');
state.retrieval.available=false;
renderRetrievalOverview();
assert(ui.retrievalRequestSummary.hidden);
assert(ui.retrievalFailures.hidden);
state.retrieval={mode:'disabled'};
renderRetrievalOverview();
assert.equal(ui.retrievalRequestSummary.textContent,'');
assert(ui.retrievalFailures.hidden);
""")

    def test_unavailable_retrieval_metadata_does_not_show_zero_counts_as_verified_statistics(self):
        self.browser(r"""
state.retrieval={mode:'historical',fixed:true,available:false,counts:{searches:0,opens:0},error:'Audit log unavailable.'};
renderRetrievalOverview();
assert.equal(ui.retrievalStatus.textContent,'Status unavailable');
assert(ui.retrievalCounts.hidden);
assert.match(ui.retrievalCountNote.textContent,/Audit log unavailable/);
""")

    def test_legacy_fixed_policy_is_never_mislabeled_as_disabled_without_new_metadata(self):
        self.browser(r"""
delete state.retrieval;state.retrievalPolicyPath='/controller/policy.json';
renderRunOverview();
assert.equal(ui.retrievalHeading.textContent,'Historical search');
assert.equal(ui.retrievalStatus.textContent,'Status unavailable');
assert.equal(ui.retrievalCutoffValue.textContent,'Not recorded');
assert(ui.retrievalCounts.hidden);
assert.match(ui.runMetadata.children.flatMap(row=>row.children).map(item=>item.textContent).join(' '),/historical archives/);
""")

    def test_native_and_disabled_run_panels_and_metadata_match_the_actual_policy(self):
        self.browser(r"""
state.retrieval={mode:'live'};
renderRunOverview();
assert.equal(ui.retrievalPanel.dataset.mode,'live');
assert.equal(ui.retrievalHeading.textContent,'Unrestricted web search');
assert.equal(ui.retrievalStatus.textContent,'Cutoff off');
assert(ui.retrievalCutoffDisplay.hidden);assert(ui.retrievalCounts.hidden);assert(ui.retrievalDetails.hidden);
assert.match(ui.runMetadata.children.flatMap(row=>row.children).map(item=>item.textContent).join(' '),/Cutoff off.*unrestricted/);
state.retrieval={mode:'disabled'};
renderRunOverview();
assert.equal(ui.retrievalPanel.dataset.mode,'disabled');
assert.equal(ui.retrievalHeading.textContent,'Web-search tools disabled');
assert.equal(ui.retrievalStatus.textContent,'Cutoff on · -1');
assert(!ui.retrievalCutoffDisplay.hidden);assert.equal(ui.retrievalCutoffValue.textContent,'-1');
assert(ui.retrievalCounts.hidden);assert(ui.retrievalDetails.hidden);
assert.match(ui.runMetadata.children.flatMap(row=>row.children).map(item=>item.textContent).join(' '),/-1.*web-search tools disabled/);
assert.match(ui.retrievalDescription.textContent,/does not isolate ordinary shell network access/);
assert.match(retrievalSummary({runId:'native',retrievalMode:'live'}),/Cutoff off/);
assert.match(retrievalSummary({runId:'old-main'}),/Cutoff off/);
""")


class HistoricalMarkupTests(unittest.TestCase):
    def test_new_controls_and_summary_have_unique_ids_and_accessible_labels(self):
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids, self.labels = [], set()

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.ids.append(attrs['id'])
                if tag == 'label' and 'for' in attrs:
                    self.labels.add(attrs['for'])

        page = Markup()
        page.feed((ROOT / 'ui/index.html').read_text())
        self.assertEqual(len(page.ids), len(set(page.ids)))
        for name in ('retrievalMode', 'retrievalCutoffUtc'):
            self.assertIn(name, page.ids)
            self.assertIn(name, page.labels)
        self.assertIn('runOverview', page.ids)
        self.assertIn('retrievalPanel', page.ids)
        self.assertIn('resultOutput', page.ids)


if __name__ == '__main__':
    unittest.main()
