"""The workflow rail follows author and critic activity without model requests."""

import json
import shutil
import subprocess
import unittest

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
const context = {ui, state, assert, all, row, text,
  document: {createElement: tag => new Element(tag)}, nodeFromStage: () => ''};
vm.runInNewContext(source.slice(source.indexOf('// Group the recorded critic calls'), source.indexOf('function renderCriticReviewHistory()'))
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
assert(!row('statement_reviewer'));
state.skipStatementReview = false;
renderWorkflow();
assert(row('statement_reviewer'));
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
assert.match(text(row('author')), /Paused · repairing critic objections/);
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


if __name__ == "__main__":
    unittest.main()
