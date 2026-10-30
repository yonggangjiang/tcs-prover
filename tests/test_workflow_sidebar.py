"""The workflow rail follows author and critic activity without model requests."""

import json
import shutil
import subprocess
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from ui import server


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class WorkflowSidebarTests(unittest.TestCase):
    def run_sidebar(self, checks):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
    this.className = ''; this.textContent = ''; this.open = false;
    this.classList = {add: name => this.className += ' ' + name};
  }
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children = items;}
  setAttribute(name, value) {this.attributes[name] = value;}
  querySelector(selector) {return all(this).find(node => selector.startsWith('.') && node.className.split(' ').includes(selector.slice(1)));}
}
const ui = {workflowNodes: new Element('ol')};
const state = {phase: 'running', stage: 'solve', activeNode: 'author',
  problemMode: 'statement', skipStatementReview: true, criticRounds: 2,
  workflow: JSON.parse(process.argv[2]), trace: [{node: 'author'}]};
const all = node => [node, ...node.children.flatMap(all)];
const row = name => all(ui.workflowNodes).find(node => node.dataset.node === name);
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const WORKFLOW = JSON.parse(process.argv[2]);
const context = {ui, state, assert, all, row, text, WORKFLOW,
  document: {createElement: tag => new Element(tag)}, nodeFromStage: () => ''};
vm.runInNewContext(source.slice(source.indexOf('// Group the recorded critic calls'), source.indexOf('function renderCriticReviewHistory()'))
  + source.slice(source.indexOf('function completedRunLabel('), source.indexOf('function localDateTime('))
  + source.slice(source.indexOf('function renderWorkflow()'), source.indexOf('function renderClock()'))
  + process.argv[3], context);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js"),
             json.dumps(server.PUBLIC_GRAPH), checks],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_author_and_critic_form_the_candidate_verification_loop(self):
        self.run_sidebar(r"""
renderWorkflow();
assert.equal(row('author').attributes['aria-current'], 'step');
assert(row('critic'));
assert(all(ui.workflowNodes).some(node => node.className === 'loop-back'));
assert(all(ui.workflowNodes).some(node => node.className === 'loop-self'));
assert(all(ui.workflowNodes).indexOf(row('failure_summary')) > all(ui.workflowNodes).indexOf(row('latex_compile')));
// A skipped statement review stays on the rail, muted.
assert.match(row('statement_reviewer').className, /disabled/);
assert.match(text(row('statement_reviewer')), /Not selected for this run/);
state.skipStatementReview = false;
renderWorkflow();
assert(!row('statement_reviewer').className.includes('disabled'));
assert(!text(ui.workflowNodes).includes('Not selected for this run'));
""")

    def test_the_critic_is_described_as_one_critic_with_its_independent_critics(self):
        self.assertEqual(server.PUBLIC_GRAPH["nodes"]["critic"]["description"],
                         "One critic starts independent critics as its subagents, weighs their reports and fixes "
                         "the valid issues. Unfixable issues return to the author.")
        critic_texts = [f"{edge['when']} {edge['prompt_change']}" for edge in server.PUBLIC_GRAPH["edges"]
                        if "critic" in (edge["from"], edge["to"])]
        for described in critic_texts:
            self.assertNotIn("parallel", described)
            self.assertNotIn("coordinator", described)
        self.run_sidebar(r"""
renderWorkflow();
assert.match(text(row('critic')), /The critic starts independent critics on the candidate and decides; rejected proofs return to the author\./);
state.workflowStages = {review: false, author: false, critic: true, writing: true};
renderWorkflow();
assert.match(text(row('critic')), /The critic starts independent critics on the supplied work and reports unresolved errors\./);
""")

    def test_the_rail_counts_the_round_whose_critics_are_running(self):
        self.run_sidebar(r"""
// One critic visit: its step and proof, its three critics, then the coordinator's request and decision.
const visit = (tag, number, decision) => [
  {kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: number, time: `${tag}-step`},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: number, time: `${tag}-proof`},
  ...[1, 2, 3].map(index => ({kind: 'request', stage: 'critic', label: `Independent critic audit ${index}`,
    attempt: 1, time: `${tag}-audit${index}`})),
  ...(decision ? [
    {kind: 'request', stage: 'critic', label: 'Critic coordinator', attempt: 1, time: `${tag}-coordinator`},
    {kind: 'critic_result', stage: 'critic', round: number, outcome: decision, time: `${tag}-result`,
     report: {verdict: decision === 'reject' ? 'reject' : 'pass', bugs: decision === 'reject' ? 'A gap.' : ''}}] : [])];
state.activeNode = 'critic'; state.stage = 'critic';
state.trace = [{kind: 'author_result', time: 'a1'}, ...visit('v1', 1, 'fixed'), ...visit('v2', 2)];
for (const round of [undefined, 2]) {
  state.round = round;
  renderWorkflow();
  assert.match(text(row('critic')), /Reviewing candidate · cycle 1, round 2/);
  assert.match(text(row('critic')), /Round 1: Pass with fixes — another review.*Round 2: Awaiting decision/);
}
// After a rejection, the repaired proof's first round while its critics run.
state.trace = [{kind: 'author_result', time: 'a1'}, ...visit('v1', 1, 'fixed'), ...visit('v2', 2, 'reject'),
  {kind: 'request', stage: 'repair', time: 'r1'}, {kind: 'author_result', time: 'a2'}, ...visit('v3', 1)];
for (const round of [undefined, 1]) {
  state.round = round;
  renderWorkflow();
  assert.match(text(row('critic')), /Reviewing candidate · cycle 2, round 1/);
}
""")

    def test_a_resumed_round_keeps_its_number_on_the_rail(self):
        self.run_sidebar(r"""
// Round 1's coordinator was interrupted and the same visit resumed; then round 2's critics run.
const visit = (tag, number) => [
  {kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: number, time: `${tag}-step`},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: number, time: `${tag}-proof`},
  ...[1, 2, 3].map(index => ({kind: 'request', stage: 'critic', label: `Independent critic audit ${index}`,
    attempt: 1, time: `${tag}-audit${index}`}))];
const coordinator = tag => ({kind: 'request', stage: 'critic', label: 'Critic coordinator', attempt: 1,
  time: `${tag}-coordinator`});
state.activeNode = 'critic'; state.stage = 'critic'; state.round = 2;
state.trace = [{kind: 'author_result', time: 'a1'}, ...visit('v1', 1), coordinator('v1'),
  {kind: 'workflow_paused', stage: 'critic', node: 'critic', time: 'v1-paused'}, ...visit('v1b', 1), coordinator('v1b'),
  {kind: 'critic_result', stage: 'critic', round: 1, outcome: 'fixed', time: 'v1b-result',
   report: {verdict: 'pass', bugs: ''}}, ...visit('v2', 2)];
renderWorkflow();
assert.match(text(row('critic')), /Reviewing candidate · cycle 1, round 2/);
assert.match(text(row('critic')), /Review cycle 1 · 2 rounds/);
assert.match(text(row('critic')), /Round 1 \(resumed\): Pass with fixes — another review.*Round 2: Awaiting decision/);
assert(!text(row('critic')).includes('Round 3'));
""")

    def test_stage_visibility_follows_the_selected_workflow(self):
        self.run_sidebar(r"""
renderWorkflow();

assert(row('author'));
state.statementReviewOnly = true;
renderWorkflow();

assert(row('statement_reviewer'));
state.statementReviewOnly = false;
for (const mode of ['latex', 'final-resume']) {
  state.problemMode = mode;
  renderWorkflow();

  assert(row('latex_editor'));
  assert(row('latex_compile'));
}
state.problemMode = 'critic-resume';
state.trace = [];
state.stage = 'critic'; state.activeNode = 'critic';
renderWorkflow();

assert.match(text(row('author')), /Loaded from the source job/);
state.problemMode = 'statement'; state.phase = 'prepared';
state.activeNode = 'author'; state.stage = 'solve';
renderWorkflow();
assert.equal(row('author').attributes['aria-current'], undefined);
assert.match(text(row('author')), /Ready to start/);
""")

    def test_each_selected_combination_shows_the_whole_workflow_and_mutes_the_rest(self):
        self.run_sidebar(r"""
state.trace = []; state.phase = 'prepared'; state.activeNode = ''; state.stage = '';
const steps = {statement_reviewer: 'review', author: 'author', critic: 'critic', convention: 'critic',
  latex_editor: 'writing', latex_compile: 'writing'};
for (let mask = 1; mask < 16; mask++) {
  const stages = Object.fromEntries(['review', 'author', 'critic', 'writing'].map(
    (name, index) => [name, Boolean(mask & (1 << index))]));
  state.workflowStages = stages;
  state.skipStatementReview = !stages.review;
  // The server's graph of the run lists only its selected steps; the rail still shows every step.
  state.workflow = {nodes: Object.fromEntries(Object.entries(WORKFLOW.nodes)
    .filter(([name]) => !steps[name] || stages[steps[name]]))};
  renderWorkflow();
  let number = 0;
  for (const [node, stage] of Object.entries(steps)) {
    assert(row(node), `${mask}: ${node}`);
    assert.equal(row(node).className.includes('disabled'), !stages[stage], `${mask}: ${node}`);
    assert.equal(text(row(node)).includes('Not selected for this run'), !stages[stage], `${mask}: ${node}`);
    // The selected steps are numbered in the order they run.
    assert.equal(row(node).children[0].textContent, stages[stage] ? String(++number) : '–', `${mask}: ${node}`);
  }
  // A step the server's graph leaves out keeps its name.
  assert.match(text(row('latex_compile')), /Compile PDF/);
  const routes = all(ui.workflowNodes);
  const loopBack = routes.find(node => node.className.split(' ').includes('loop-back'));
  assert.equal(loopBack.className.includes('not-selected'), !(stages.author && stages.critic), `${mask}: reject route`);
  if (!stages.author) assert.equal(loopBack.attributes['aria-label'], "On rejection, the run ends: the critic's report is saved");
  const writing = routes.find(node => node.className.split(' ').includes('writing-group'));
  assert.equal(writing.children[0].textContent, 'Writing & polishing');
  assert.equal(writing.className.includes('not-selected'), !stages.writing, `${mask}: writing group`);
  // It starts at the first selected stage, also when the server prepared it at the author (a folder
  // batch's job without a statement review), and never on a step that is not selected.
  const first = Object.entries(steps).find(([, stage]) => stages[stage])[0];
  for (const active of ['', 'author']) {
    state.activeNode = active;
    renderWorkflow();
    const marks = all(ui.workflowNodes).filter(node => node.className.split(' ').includes('node-position'));
    assert.equal(marks.length, 1, `${mask} ${active}`);
    assert.match(text(row(active === 'author' && stages.author ? 'author' : first)), /Starts here Ready to start/,
      `${mask} ${active}`);
  }
  state.activeNode = '';
}
""")

    def test_a_prepared_or_failed_critic_only_job_is_marked_at_the_critic(self):
        self.run_sidebar(r"""
const marks = () => all(ui.workflowNodes).filter(node => node.className.split(' ').includes('node-position'))
  .map(node => [all(ui.workflowNodes).find(item => item.dataset.node && all(item).includes(node)).dataset.node,
    text(node).replace(/\s+/g, ' ').trim()]);
// As the server prepares and restores a folder-batch job without a statement review: activeNode "author".
Object.assign(state, {phase: 'prepared', trace: [], stage: 'solve', activeNode: 'author', launchQueued: true,
  workflowStages: {review: false, author: false, critic: true, writing: false}});
renderWorkflow();
assert.equal(JSON.stringify(marks()), JSON.stringify([['critic', 'Starts here Queued: starts when a running slot is free']]));
assert.match(row('author').className, /disabled/);
assert.match(text(row('author')), /Not selected for this run/);
// It could not start: the end is marked at the critic too, and the muted author is not marked failed.
Object.assign(state, {phase: 'done', launchQueued: false, resultKind: 'failure', error: 'Could not start.'});
renderWorkflow();
assert.equal(JSON.stringify(marks()), JSON.stringify([['critic', 'Ended here Could not start.']]));
assert.match(row('author').className, /disabled/);
assert.match(row('critic').className, /failed/);
for (const key of ['resultKind', 'error', 'launchQueued']) delete state[key];
""")

    def test_critic_only_rejection_does_not_claim_an_author_handoff(self):
        self.run_sidebar(r"""
state.workflowStages = {review: false, author: false, critic: true, writing: false};
state.activeNode = 'critic'; state.stage = 'critic'; state.phase = 'done';
state.resultKind = 'needs-revision'; state.error = 'Proof needs revision.';
state.trace = [{kind: 'critic_result', node: 'critic', outcome: 'needs_revision',
  report: {verdict: 'reject', bugs: 'Missing argument'}}];
renderWorkflow();
assert(row('critic'));
assert.match(row('author').className, /disabled/);
assert.match(text(row('author')), /Not selected for this run/);
assert.match(text(row('critic')), /Rejected — needs revision/);
assert(!text(row('critic')).includes('returned to author'));
assert.match(text(row('critic')), /Ended here The critic found errors it could not fix/);
""")

    def test_rejected_critic_is_not_a_completed_pass_during_author_repair(self):
        self.run_sidebar(r"""

state.stage = 'repair'; state.round = 1;
state.trace.push({kind: 'critic_result', node: 'critic', round: 1, report: {verdict: 'reject'}});
state.finalInputReady = true; // Stale approval cannot override the newest verdict.
renderWorkflow();
assert.match(row('critic').className, /rejected/);
assert(!row('critic').className.includes('complete'));
assert.equal(row('critic').children[0].textContent, '×');
assert.match(text(row('critic')), /Latest completed round: Rejected — returned to author/);
assert.match(text(row('critic')), /Recorded: 1 cycle · 1 completed round · 1 rejection/);
assert(!text(row('critic')).includes('Consecutive recorded PASS'));
assert.match(text(row('author')), /Repairing critic objections/);
assert.equal(row('author').attributes['aria-current'], 'step');
state.phase = 'paused';
renderWorkflow();
assert.match(row('author').className, /paused/);
assert.match(text(row('author')), /Paused here The work so far is saved; Resume continues it.*Repairing critic objections/);
assert.equal(row('critic').children[0].textContent, '×');
""")

    def test_new_critic_review_preserves_latest_rejection_and_execution_state(self):
        self.run_sidebar(r"""
state.trace.push({kind: 'critic_result', node: 'critic', round: 1, report: {verdict: 'reject'}});
state.activeNode = 'critic'; state.stage = 'critic'; state.round = 2;
renderWorkflow();
assert.match(row('critic').className, /active/);
assert.equal(row('critic').attributes['aria-current'], 'step');
assert.match(text(row('critic')), /Reviewing candidate/);
assert.match(text(row('critic')), /Latest completed round: Rejected — returned to author/);
assert(!text(row('critic')).includes('Round 2 of 2'));
for (const phase of ['paused', 'pausing']) {
  state.phase = phase;
  renderWorkflow();
  assert.match(row('critic').className, /paused/);
  assert.equal(row('critic').children[0].textContent, 'Ⅱ');
  assert.match(text(row('critic')), /Latest completed round: Rejected — returned to author/);
}
state.phase = 'done'; state.error = 'Review interrupted';
renderWorkflow();
assert.match(row('critic').className, /failed/);
assert.equal(row('critic').children[0].textContent, '!');
assert.match(text(row('critic')), /Review interrupted/);
""")

    def test_resumed_author_solve_stage_keeps_unresolved_critic_repair_label(self):
        self.run_sidebar(r"""
state.activeNode = 'author'; state.stage = 'solve';
state.trace.push({kind: 'status', label: 'Critic approved'},
  {kind: 'critic_result', node: 'critic', report: {verdict: 'reject'}},
  {kind: 'status', node: 'author', stage: 'solve', label: 'Goal resumed'});
state.finalInputReady = true; // An older durable approval does not resolve this rejection.
for (const phase of ['running', 'paused', 'pausing', 'stopping']) {
  state.phase = phase;
  renderWorkflow();
  assert.match(text(row('author')), /[Rr]epairing critic objections/);
  assert.match(text(row('critic')), /Latest completed round: Rejected — returned to author/);
  if (phase === 'paused' || phase === 'pausing') assert.match(row('author').className, /paused/);
  else assert.equal(row('author').attributes['aria-current'], 'step');
}
state.phase = 'done';
renderWorkflow();
assert(!text(row('author')).includes('Repairing critic objections'));
state.phase = 'running';
state.trace.push({kind: 'status', label: 'Critic approved'});
renderWorkflow();
assert(!text(row('author')).includes('Repairing critic objections'));
state.trace = [...state.trace.slice(0, -1), {kind: 'critic_result', node: 'critic', report: {verdict: 'pass'}}];
renderWorkflow();
assert(!text(row('author')).includes('Repairing critic objections'));
state.trace = [{node: 'author', stage: 'solve'}];
renderWorkflow();
assert(!text(row('author')).includes('Repairing critic objections'));
""")

    def test_history_groups_restarted_rounds_and_explains_acceptance_at_limit(self):
        self.run_sidebar(r"""
state.trace.push(...[
  {round: 1, outcome: 'fixed', report: {verdict: 'pass'}},
  {round: 2, report: {verdict: 'reject'}},
  {round: 1, outcome: 'fixed', report: {verdict: 'pass'}},
].map(entry => ({kind: 'critic_result', node: 'critic', ...entry})));
renderWorkflow();
assert.match(row('critic').className, /reviewed/);
assert.notEqual(row('critic').children[0].textContent, '✓');
assert.match(text(row('critic')), /Pass with changes — next step not recorded/);
assert.match(text(row('critic')), /Recorded: 2 cycles · 3 completed rounds · 1 rejection/);
assert.match(text(row('critic')), /Cycle 1 · Rejected.*Round 1: Pass with fixes — another review.*Round 2: Rejected/);
assert.match(text(row('critic')), /Cycle 2 · Pass with changes.*Round 1: Pass with changes/);
state.trace.push({kind: 'critic_result', node: 'critic', round: 2, outcome: 'fixed', report: {verdict: 'pass'}});
state.trace.push({kind: 'status', label: 'Critic approved', acceptanceReason: 'round_limit', round: 2, roundLimit: 2});
renderWorkflow();
assert.match(row('critic').className, /complete/);
assert.equal(row('critic').children[0].textContent, '✓');
assert.match(text(row('critic')), /Pass with changes — accepted at review limit/);
assert.match(text(row('critic')), /Review cycle 2 · 2 rounds/);
assert.match(text(row('critic')), /Cycle 2.*Round 1: Pass with fixes — another review.*Round 2: Pass with changes — accepted at review limit/);
assert(!text(row('critic')).includes('Clean pass'));
// An unknown later result also must not inherit the earlier approval.
state.trace.push({kind: 'critic_result', node: 'critic', report: {verdict: 'unexpected'}});
renderWorkflow();
assert.match(row('critic').className, /reviewed/);
assert.match(text(row('critic')), /Latest completed round: Verdict unavailable/);
assert.notEqual(row('critic').children[0].textContent, '✓');
""")

    def test_old_jobs_without_verdicts_stay_neutral_and_durable_approval_survives(self):
        self.run_sidebar(r"""
state.trace.push({node: 'critic'}, {node: 'latex_editor'});
state.activeNode = 'latex_editor'; state.stage = 'final';
renderWorkflow();
assert.match(row('critic').className, /reviewed/);
assert.match(text(row('critic')), /Verdict unavailable/);
assert.notEqual(row('critic').children[0].textContent, '✓');
state.trace = [{kind: 'critic_result', node: 'critic', report: {verdict: 'pass'}}];
state.finalInputReady = true;
renderWorkflow();
assert.equal(row('critic').children[0].textContent, '✓');
assert.match(text(row('critic')), /Pass — edits and next step not recorded/);
delete state.finalInputReady;
state.trace.push({node: 'latex_editor'});
renderWorkflow();
assert.equal(row('critic').children[0].textContent, '✓');
""")

    def test_edited_pass_shows_next_round_and_clean_pass_accepts_same_cycle(self):
        self.run_sidebar(r"""
state.activeNode = 'critic'; state.stage = 'critic';
state.trace = [
  {kind: 'request', stage: 'critic', time: 't1', attempt: 1, text: 'Original proof'},
  {kind: 'critic_result', time: 't2', round: 1, outcome: 'fixed', report: {verdict: 'pass', solution: 'Revised proof'}},
  {kind: 'request', stage: 'critic', time: 't3', attempt: 1, text: 'Revised proof'},
];
renderWorkflow();
assert.match(row('critic').className, /active/);
assert.match(text(row('critic')), /Reviewing candidate · cycle 1, round 2/);
assert.match(text(row('critic')), /Latest completed round: Pass with fixes — another review/);
assert.match(text(row('critic')), /Round 2: Awaiting decision/);
assert.notEqual(row('critic').children[0].textContent, '✓');
ui.workflowNodes.querySelector('.critic-history').open = true;
state.trace.push({kind: 'critic_result', time: 't4', round: 2, outcome: 'pass',
  report: {verdict: 'pass', solution: 'Revised proof'}});
state.activeNode = 'latex_editor'; state.stage = 'final';
renderWorkflow();
assert.equal(row('critic').children[0].textContent, '✓');
assert.match(text(row('critic')), /Latest completed round: Clean pass — proof accepted/);
assert.match(text(row('critic')), /Recorded: 1 cycle · 2 completed rounds · 0 rejections/);
assert.match(text(row('critic')), /Round 1: Pass with fixes — another review.*Round 2: Clean pass — proof accepted/);
assert(!text(row('critic')).includes('accepted at review limit'));
assert(ui.workflowNodes.querySelector('.critic-history').open);
""")

    def test_the_running_step_is_marked_now_with_its_critics(self):
        self.run_sidebar(r"""
const marks = () => all(ui.workflowNodes).filter(node => node.className.split(' ').includes('node-position'));
const words = node => text(node).replace(/\s+/g, ' ').trim();
state.workflowStages = {review: false, author: true, critic: true, writing: false};
state.activeNode = 'critic'; state.stage = 'critic'; state.trace = [];
// The server's count (follow_critic_progress) of what the critic's event stream shows of its critics.
const counted = (count, fields = {}) => ({count, requested: true, started: [], finished: [], failed: [], waiting: false,
  ...fields});
for (const [progress, step] of [
    [counted(3, {started: [1, 2, 3], finished: [1, 3]}), 'Critic 2 of 3 working · 2 of 3 finished'],
    [counted(3, {started: [1, 2, 3]}), '3 critics working'],
    [counted(3, {started: [1, 2, 3], finished: [2]}), 'Critic 1 of 3 and Critic 3 of 3 working · 1 of 3 finished'],
    [counted(5, {started: [1, 2, 3, 4, 5], finished: [2]}), 'Critics 1, 3, 4 and 5 of 5 working · 1 of 5 finished'],
    [counted(1, {started: [1]}), 'Critic 1 of 1 working'],
    [counted(3, {started: [1, 2]}), 'Critic 1 of 3 and Critic 2 of 3 working'],
    [counted(3, {started: [1, 2], finished: [1], failed: [1]}), 'Critic 2 of 3 working · 1 of 3 finished (1 stopped early)'],
    // Every critic it started finished: it decides, or starts the others.
    [counted(3, {started: [1, 2, 3], finished: [1, 2, 3]}), 'Critic coordinator deciding · all 3 critics finished'],
    [counted(1, {started: [1], finished: [1]}), 'Critic coordinator deciding · the critic finished'],
    [counted(3, {started: [1], finished: [1]}), 'Critic coordinator working · 1 of 3 critics finished'],
    // Where Codex shows only the wait (a web-cutoff call of multi-agent version 2), that it started them;
    // never that it still waits, which nothing ends.
    [counted(3, {waiting: true}), 'Critic working · independent critics started (3 requested)'],
    [counted(1, {waiting: true}), 'Critic working · independent critics started (1 requested)'],
    // A critic started beyond those requested (one after another finished) is named as such.
    [counted(3, {started: [1, 2, 3, 4], finished: [1, 2, 3]}), 'Critic 4 (beyond the 3 requested) working · 3 of 4 finished'],
    [counted(3, {started: [1, 2, 3, 4], finished: [1, 2, 4]}), 'Critic 3 of 3 working · 3 of 4 finished'],
    [counted(3, {started: [1, 2, 3, 4, 5], finished: [1]}), 'Critics 2, 3, 4 and 5 (3 requested) working · 1 of 5 finished'],
    [counted(3, {started: [1, 2, 3, 4], finished: [1, 2, 3, 4]}), 'Critic coordinator deciding · all 4 critics finished'],
    [counted(3), '3 independent critics requested'],
    [counted(3, {requested: false}), 'Reviewing the candidate'],
    [null, 'Reviewing the candidate']]) {
  state.criticProgress = progress;
  renderWorkflow();
  assert.equal(marks().length, 1);
  assert.equal(words(marks()[0]), `Now ${step}`);
  assert.equal(marks()[0].attributes['aria-label'], `Now: ${step}`);
  assert.equal(row('critic').attributes['aria-current'], 'step');
}
state.modelRetry = {text: 'Model call failed', retryAt: 2e9, kind: 'model', stage: 'critic'};
renderWorkflow();
// The failed call's critics ended with it: the retry starts them again.
assert.match(text(row('critic')), /Now Waiting to retry a failed model call; its independent critics start again with it/);
state.criticProgress = counted(3, {started: [1, 2], finished: [1]});
renderWorkflow();
assert.match(text(row('critic')), /Now Waiting to retry a failed model call; its 3 independent critics start again with it/);
assert.doesNotMatch(text(row('critic')), /working/);
state.criticProgress = null;
// Another step's wait to retry is said after that step.
state.activeNode = 'author'; state.stage = 'solve'; state.modelRetry = {text: 'Model call failed', retryAt: 2e9, stage: 'solve'};
renderWorkflow();
assert.match(text(row('author')), /Now Working on the proof · waiting to retry a failed model call/);
state.activeNode = 'critic'; state.stage = 'critic';
// A command that could not run (TeX) and a file write say what waits.
state.activeNode = 'latex_compile'; state.workflowStages = {review: false, author: true, critic: true, writing: true};
state.modelRetry = {text: 'latexmk is not available.', retryAt: 2e9, kind: 'command'};
renderWorkflow();
assert.match(text(row('latex_compile')), /Now Compiling the PDF · waiting to retry a command that could not run/);
state.modelRetry = {text: 'Could not save the LaTeX source.', retryAt: 2e9, kind: 'storage'};
renderWorkflow();
assert.match(text(row('latex_compile')), /Now Compiling the PDF · waiting to retry saving a file/);
assert(!/failed model call/.test(text(ui.workflowNodes)));
delete state.modelRetry;
state.workflowStages = {review: true, author: true, critic: true, writing: true};
for (const [node, active, step] of [['author', 'author', 'Working on the proof'],
    ['latex_editor', 'latex_editor', 'Writing the LaTeX document'], ['latex_compile', 'latex_repair', 'Fixing LaTeX errors'],
    ['statement_reviewer', 'statement_reviewer', 'Checking the statement']]) {
  state.activeNode = active;
  renderWorkflow();
  assert.match(text(row(node)), new RegExp(`Now ${step}`));
}
state.activeNode = 'author'; state.phase = 'stopping';
renderWorkflow();
assert.match(text(row('author')), /Now Stopping/);
""")

    def test_a_paused_or_waiting_job_marks_where_and_why(self):
        self.run_sidebar(r"""
state.workflowStages = {review: true, author: true, critic: true, writing: true};
state.phase = 'paused'; state.activeNode = 'author'; state.stage = 'solve';
state.pauseCause = 'time-limit'; state.thinkingHours = 2;
state.timeLimitHistory = [{event: 'reached', time: 't', hours: 0.003, elapsedSeconds: 13}];
renderWorkflow();
// A stop at the time limit is no pause: "Stopped here", and it can continue.
assert.match(text(row('author')), /Stopped here Time limit reached \(0.003 hours\); you can continue/);
assert(!/Paused/.test(text(row('author'))));
// Its step has the clock (drawn by the stylesheet) and its own mark, not the pause bars.
assert.equal(row('author').className, 'workflow-node limit');
assert.equal(row('author').querySelector('.node-dot').textContent, '');
assert(row('author').querySelector('.node-position').className.split(' ').includes('limit'));
// While the automatic progress summary is written, the mark says so (Continue waits for it).
state.conversation = {items: [{kind: 'summary', origin: 'time-limit', status: 'running'}]};
renderWorkflow();
assert.match(text(row('author')), /Stopped here Time limit reached \(0.003 hours\) · writing progress summary/);
delete state.conversation;
state.pauseCause = 'user';
renderWorkflow();
assert.match(text(row('author')), /Paused here Paused by you/);
assert.equal(row('author').className, 'workflow-node paused');
assert.equal(row('author').querySelector('.node-dot').textContent, 'Ⅱ');
assert(row('author').querySelector('.node-position').className.split(' ').includes('paused'));
state.pauseCause = 'error'; state.error = 'The usage limit is reached.'; state.autoResumeAt = 2e9;
renderWorkflow();
assert.match(text(row('author')), /Paused here The usage limit is reached; it retries by itself at /);
// A saved batch's job is read only: its cause is stated, and no Resume is offered.
state.readOnly = true; state.error = ''; state.autoResumeAt = 0;
state.pauseCause = 'time-limit';
renderWorkflow();
assert.match(text(row('author')), /Stopped here Time limit reached \(0.003 hours\) /);
assert(!/continue|Paused/.test(text(row('author'))));
state.pauseCause = '';
renderWorkflow();
assert.match(text(row('author')), /Paused here The work so far is saved/);
assert(!text(row('author')).includes('Resume'));
delete state.readOnly;
state.pauseCause = ''; state.error = ''; state.autoResumeAt = 0;
state.phase = 'reviewed'; state.activeNode = 'statement_reviewer';
renderWorkflow();
assert.match(text(row('statement_reviewer')), /Waiting here Approve the checked statement to continue/);
state.phase = 'prepared'; state.activeNode = ''; state.launchQueued = true;
renderWorkflow();
assert.match(text(row('statement_reviewer')), /Starts here Queued: starts when a running slot is free/);
""")

    def test_a_finished_job_marks_where_it_ended_and_why(self):
        self.run_sidebar(r"""
state.phase = 'done'; state.trace = [];
const ended = () => all(ui.workflowNodes).filter(node => node.className.split(' ').includes('node-position'))
  .map(node => [all(ui.workflowNodes).find(item => item.dataset.node && all(item).includes(node)).dataset.node,
    text(node).replace(/\s+/g, ' ').trim()]);
for (const [fields, mark] of [
    // Point 2: the critic accepted the proof and writing was not selected.
    [{workflowStages: {review: false, author: true, critic: true, writing: false}, resultKind: 'proof',
      activeNode: 'critic'}, ['critic', 'Ended here Accepted by the critic; writing not selected']],
    [{workflowStages: {review: false, author: true, critic: false, writing: false}, resultKind: 'proof',
      activeNode: 'author'}, ['author', 'Ended here Proof written; critic and writing not selected']],
    [{workflowStages: {review: true, author: true, critic: true, writing: true}, resultKind: 'final',
      canDownloadTex: true, canDownloadPdf: true, activeNode: 'latex_compile'},
     ['latex_compile', 'Ended here LaTeX document and PDF made']],
    [{workflowStages: {review: true, author: true, critic: true, writing: true}, manuallyStopped: true,
      stoppedStage: 'solve', error: 'Stopped.', activeNode: 'author'}, ['author', 'Ended here Stopped by you']],
    // Stopped in Writing & polishing: at the step that ran (a LaTeX repair is the compile step).
    ...[['latex_compile', 'latex_compile'], ['latex_repair', 'latex_compile'], ['latex_editor', 'latex_editor'],
        ['author', 'latex_editor'], ['', 'latex_editor']].map(([active, node]) => [
      {workflowStages: {review: true, author: true, critic: true, writing: true}, manuallyStopped: true,
       stoppedStage: 'final', error: 'Stopped.', activeNode: active}, [node, 'Ended here Stopped by you']]),
    [{workflowStages: {review: true, author: true, critic: true, writing: true}, resultKind: 'failure',
      error: 'LaTeX compilation is unavailable.', activeNode: 'latex_editor'},
     ['latex_editor', 'Ended here LaTeX compilation is unavailable.']],
    [{workflowStages: {review: true, author: false, critic: false, writing: false}, resultKind: 'review',
      statementReviewOnly: true, activeNode: 'statement_reviewer'},
     ['statement_reviewer', 'Ended here Statement checked; no proof stages were selected']]]) {
  for (const key of ['resultKind', 'error', 'manuallyStopped', 'stoppedStage', 'canDownloadTex', 'canDownloadPdf',
      'statementReviewOnly']) delete state[key];
  Object.assign(state, fields);
  renderWorkflow();
  assert.equal(JSON.stringify(ended()), JSON.stringify([mark]), JSON.stringify(fields));
}
// Point 2's rail: the whole workflow, the unselected stages muted.
Object.assign(state, {workflowStages: {review: false, author: true, critic: true, writing: false}, resultKind: 'proof',
  activeNode: 'critic', statementReviewOnly: false});
delete state.error; delete state.manuallyStopped;
renderWorkflow();
for (const node of ['statement_reviewer', 'latex_editor', 'latex_compile']) {
  assert.match(text(row(node)), /Not selected for this run/, node);
}
""")


if __name__ == "__main__":
    unittest.main()
