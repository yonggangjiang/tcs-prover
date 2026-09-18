"""The workflow rail follows author and audit activity without model requests."""

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
    this.className = ''; this.textContent = '';
    this.classList = {add: name => this.className += ' ' + name};
  }
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children = items;}
  setAttribute(name, value) {this.attributes[name] = value;}
}
const ui = {workflowNodes: new Element('ol')};
const state = {phase: 'running', stage: 'solve', activeNode: 'author', fileManagement: true,
  problemMode: 'statement', skipStatementReview: true, criticRounds: 2,
  workflow: JSON.parse(process.argv[2]), trace: [{node: 'author'}],
  researchAudits: {models: ['gpt-6-astra', 'claude-opus', 'none'], intervalHours: 2},
  researchAuditStatus: {runningSlots: [], warnings: [], batchActive: false}};
const all = node => [node, ...node.children.flatMap(all)];
const row = name => all(ui.workflowNodes).find(node => node.dataset.node === name);
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const context = {ui, state, assert, all, row, text,
  document: {createElement: tag => new Element(tag)}, nodeFromStage: () => ''};
vm.runInNewContext(source.slice(source.indexOf('function renderWorkflow()'), source.indexOf('function renderClock()'))
  + process.argv[3], context);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js"),
             json.dumps(server.PUBLIC_GRAPH), checks],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_author_and_audit_form_a_loop_separate_from_candidate_verification(self):
        self.run_sidebar(r"""
renderWorkflow();
const audit = row('research_audit'), author = row('author');
assert(audit);
const pair = all(ui.workflowNodes).find(node => node.children.includes(author) && node.children.includes(audit));
assert(pair);
assert.match(text(pair), /Pause author → audit → resume author/);
assert.match(text(audit), /2 selected · every 2 author hours or on demand/);
assert.match(text(audit), /Scheduled/);
assert.equal(author.attributes['aria-current'], 'step');
assert.equal(audit.attributes['aria-current'], undefined);
assert(!pair.children.includes(row('critic')));
assert(all(ui.workflowNodes).some(node => node.className === 'loop-back'));
assert(all(ui.workflowNodes).some(node => node.className === 'loop-self'));
assert(all(ui.workflowNodes).indexOf(row('failure_summary')) > all(ui.workflowNodes).indexOf(row('latex_compile')));
assert(!row('statement_reviewer'));
state.skipStatementReview = false;
renderWorkflow();
assert(row('statement_reviewer'));
assert(row('research_audit'));
""")

    def test_highlight_moves_to_audit_while_author_is_paused_and_back_after_reports(self):
        self.run_sidebar(r"""
state.researchAuditStatus = {batchActive: true, runningSlots: [1, 2]};
renderWorkflow();
assert.match(text(row('research_audit')), /Checking auditors/);
assert.equal(row('author').attributes['aria-current'], 'step');
state.auditHoldingAuthor = true;
state.phase = 'pausing';
renderWorkflow();
assert.match(text(row('author')), /Pausing for audits/);
assert.match(text(row('research_audit')), /Waiting for author/);
state.phase = 'paused';
renderWorkflow();
assert.match(row('author').className, /paused/);
assert.equal(row('author').attributes['aria-current'], undefined);
assert.equal(row('research_audit').attributes['aria-current'], 'step');
assert.match(text(row('author')), /Paused for audits/);
assert.match(text(row('research_audit')), /2 auditors active/);
assert.equal(state.activeNode, 'author'); // Rendering must not change the controller's node.
state.phase = 'running'; state.auditHoldingAuthor = false;
state.researchAuditStatus = {runningSlots: [], batchActive: false};
state.trace.push({kind: 'research_audit', model: 'gpt-6-astra', status: 'completed'});
renderWorkflow();
assert.equal(row('author').attributes['aria-current'], 'step');
assert.match(row('research_audit').className, /complete/);
assert.match(text(row('research_audit')), /Reports saved/);
state.phase = 'paused';
renderWorkflow();
assert.match(row('author').className, /paused/);
assert(!text(row('author')).includes('Paused for audits'));
assert.equal(row('research_audit').attributes['aria-current'], undefined);
""")

    def test_disabled_unavailable_and_cancelled_audits_have_distinct_states(self):
        self.run_sidebar(r"""
state.researchAudits.models = ['none', 'none', 'none'];
renderWorkflow();
assert.match(row('research_audit').className, /disabled/);
assert.match(text(row('research_audit')), /No auditors|Select auditors/);
assert.match(text(row('research_audit')), /Disabled/);
state.researchAudits.models[0] = 'gpt-6-astra';
state.researchAuditStatus.warnings = [{slot: 1, model: 'gpt-6-astra', message: 'Unavailable'}];
renderWorkflow();
assert.match(row('research_audit').className, /warning/);
assert.match(text(row('research_audit')), /Needs attention/);
assert.equal(row('author').attributes['aria-current'], 'step');
state.researchAuditStatus = {runningSlots: [], batchActive: false};
state.trace.push({kind: 'research_audit', model: 'gpt-6-astra', status: 'cancelled'});
state.phase = 'paused';
renderWorkflow();
assert.match(text(row('research_audit')), /Cancelled/);
assert.equal(row('research_audit').attributes['aria-current'], undefined);
state.phase = 'done'; state.error = 'Stopped.';
state.researchAuditStatus.batchActive = true; // A stale status must not mark finished jobs active.
renderWorkflow();
assert.equal(row('research_audit').attributes['aria-current'], undefined);
""")

    def test_audits_only_appear_in_workflows_that_use_research_files(self):
        self.run_sidebar(r"""
state.fileManagement = false;
renderWorkflow();
assert(!row('research_audit'));
assert(row('author'));
state.fileManagement = true;
state.statementReviewOnly = true;
renderWorkflow();
assert(!row('research_audit'));
assert(row('statement_reviewer'));
state.statementReviewOnly = false;
for (const mode of ['latex', 'final-resume']) {
  state.problemMode = mode;
  renderWorkflow();
  assert(!row('research_audit'));
  assert(row('latex_editor'));
  assert(row('latex_compile'));
}
state.problemMode = 'critic-resume';
state.trace = [];
state.stage = 'critic'; state.activeNode = 'critic';
renderWorkflow();
assert(row('research_audit'));
assert.match(text(row('author')), /Loaded from the source job/);
assert.equal(row('research_audit').attributes['aria-current'], undefined);
state.problemMode = 'statement'; state.phase = 'prepared';
state.activeNode = 'author'; state.stage = 'solve';
renderWorkflow();
assert.equal(row('author').attributes['aria-current'], undefined);
assert.match(text(row('author')), /Ready to start/);
""")


if __name__ == "__main__":
    unittest.main()
