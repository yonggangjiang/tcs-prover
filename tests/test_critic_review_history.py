"""Coordinator's exact inputs and decisions stay paired and readable in metadata."""

import shutil
import subprocess
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from ui import server


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for browser logic tests')
class CriticReviewHistoryTests(unittest.TestCase):
    def run_history(self, checks):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const all = node => [node, ...node.children.flatMap(all)];
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.open = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelectorAll(tag) { return all(this).slice(1).filter(node => node.tagName === tag); }
}
const ui = {criticReviewHistory: new Element('details')};
const state = {runId: 'run-a', phase: 'running', activeNode: 'critic', trace: []};
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const cycles = () => ui.criticReviewHistory.children.filter(node => node.className === 'critic-cycle');
const reviews = () => cycles().flatMap(cycle => cycle.children.filter(node => node.className === 'critic-review'));
const request = (time, input, extra = {}) => ({kind: 'request', stage: 'critic', time, text: input, attempt: 1, ...extra});
const result = (time, verdict, bugs = '', extra = {}) => ({kind: 'critic_result', stage: 'critic', time, round: 1,
  report: {verdict, bugs, solution: 'The complete candidate.'}, text: 'RAW ' + verdict + ': ' + bugs, ...extra});
const context = {assert, ui, state, all, text, cycles, reviews, request, result,
  document: {createElement: tag => new Element(tag)}, localDateTime: value => value};
vm.runInNewContext(source.slice(source.indexOf('// Group the recorded critic calls'), source.indexOf('function renderRetrievalOverview()'))
  + process.argv[2], context);
"""
        completed = subprocess.run([shutil.which('node'), '-e', script, str(server.UI / 'app.js'), checks],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_repeated_round_numbers_keep_exact_distinct_inputs_and_rejections(self):
        self.run_history(r"""
state.trace = [request('t1', 'FIRST exact proof'), result('t2', 'reject', 'FIRST exact bugs'),
  {kind: 'request', stage: 'repair'}, request('t3', 'SECOND exact proof'), result('t4', 'reject', 'SECOND exact bugs')];
renderCriticReviewHistory();
const rows = reviews();
assert.equal(rows.length, 2);
assert.equal(cycles().length, 2);
assert.match(text(cycles()[0]), /Review cycle 1.*Round 1/);
assert.match(text(cycles()[1]), /Review cycle 2.*Round 1/);
assert.match(text(rows[0]), /FIRST exact proof/); assert.match(text(rows[0]), /FIRST exact bugs/);
assert(!text(rows[0]).includes('SECOND exact'));
assert.match(text(rows[1]), /SECOND exact proof/); assert.match(text(rows[1]), /SECOND exact bugs/);
assert(!text(rows[1]).includes('FIRST exact'));
assert.notEqual(rows[0].dataset.reviewKey, rows[1].dataset.reviewKey);
assert.match(text(rows[0]), /Coordinator's raw decision RAW reject: FIRST exact bugs/);
assert.equal(ui.criticReviewHistory.open, false);
assert(all(ui.criticReviewHistory).filter(node => node.tagName === 'details').every(node => !node.open));
""")

    def test_pending_requests_and_recovery_attempts_are_preserved(self):
        self.run_history(r"""
state.trace = [request('t1', 'First attempt'), request('t2', 'Exact recovery input', {attempt: 2})];
renderCriticReviewHistory();
assert.equal(reviews().length, 1);
assert.match(text(reviews()[0]), /Awaiting decision/);
assert.match(text(reviews()[0]), /Coordinator's exact input · attempt 1 First attempt/);
assert.match(text(reviews()[0]), /Coordinator's exact input · attempt 2 Exact recovery input/);
state.trace.push(result('t3', 'pass'));
renderCriticReviewHistory();
assert.match(text(reviews()[0]), /Round 1 · Pass — edits and next step not recorded/);
assert.match(text(reviews()[0]), /RAW pass/);
state.trace.push(request('t4', 'A later candidate'));
renderCriticReviewHistory();
assert.equal(reviews().length, 2);
assert.equal(cycles().length, 1);
assert.match(text(reviews()[0]), /Pass with fixes — another review/);
assert.match(text(reviews()[1]), /Awaiting decision/);
state.phase = 'done';
renderCriticReviewHistory();
assert.match(text(reviews()[1]), /No recorded decision/);
""")

    def test_missing_input_does_not_borrow_previous_cycle_or_independent_audit(self):
        self.run_history(r"""
state.trace = [request('t1', 'Abandoned review'), {kind: 'request', stage: 'repair'},
  request('t2', 'Independent audit input', {label: 'Independent critic audit 1'}),
  request('t2b', 'Another independent audit input', {activityLabel: 'Audit 2'}),
  result('t3', 'reject', 'New rejection')];
renderCriticReviewHistory();
assert.equal(reviews().length, 2);
assert.match(text(reviews()[0]), /No recorded decision/);
assert.match(text(reviews()[1]), /exact input was not recorded/);
assert(!text(reviews()[1]).includes('Abandoned review'));
assert(!text(ui.criticReviewHistory).includes('Independent audit input'));
assert(!text(ui.criticReviewHistory).includes('Another independent audit input'));
""")

    def test_a_round_whose_critics_run_awaits_its_coordinator_in_the_same_row(self):
        self.run_history(r"""
const visit = (tag, number) => [
  {kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: number, time: `${tag}-step`},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: number, time: `${tag}-proof`},
  ...[1, 2, 3].map(index => request(`${tag}-audit${index}`, `Audit input ${index}`,
    {label: `Independent critic audit ${index}`}))];
state.trace = [...visit('v1', 1), request('v1-c', 'First coordinator input', {label: 'Critic coordinator'}),
  result('v1-r', 'pass', '', {outcome: 'fixed'}), ...visit('v2', 2)];
renderCriticReviewHistory();
assert.equal(reviews().length, 2);
assert.match(text(reviews()[1]), /Round 2 · Awaiting decision/);
// An older transcript's own critics' requests are no inputs of the round: it awaits its critic's input.
assert.match(text(reviews()[1]), /The critic is starting\. Its exact input appears here once it is sent\./);
assert(!text(ui.criticReviewHistory).includes('Audit input'));
state.trace = [...state.trace, request('v2-c', 'Second coordinator input', {label: 'Critic coordinator'})];
renderCriticReviewHistory();
assert.equal(reviews().length, 2);
assert.match(text(reviews()[1]), /Coordinator's exact input Second coordinator input/);
assert(!text(reviews()[1]).includes('The critic is starting'));
assert.match(text(reviews()[1]), /The critic is reviewing the proof\./);
// A finished job keeps no round for a visit that ended before its coordinator was asked.
state.trace = state.trace.slice(0, -1); state.phase = 'done';
renderCriticReviewHistory();
assert.equal(reviews().length, 1);
""")

    def test_explicit_request_identity_matches_even_when_arrival_order_differs(self):
        self.run_history(r"""
state.trace = [request('t1', 'Input A', {id: 'a'}), request('t2', 'Input B', {id: 'b'}),
  result('t3', 'reject', 'B bugs', {requestId: 'b'}), result('t4', 'pass', '', {requestId: 'a'})];
renderCriticReviewHistory();
assert.equal(reviews().length, 2);
assert.match(text(reviews()[0]), /Round 1 · Pass — edits and next step not recorded/);
assert.match(text(reviews()[0]), /Input A/);
assert(!text(reviews()[0]).includes('B bugs'));
assert.match(text(reviews()[1]), /Round 2 · Rejected — returned to author/);
assert.match(text(reviews()[1]), /Input B/);
assert.match(text(reviews()[1]), /B bugs/);
""")

    def test_long_text_is_complete_and_rendered_as_plain_text(self):
        self.run_history(r"""
const input = '<script>not executable</script>\n' + 'a'.repeat(90000) + '\nINPUT END';
const bugs = 'b'.repeat(80000) + '\nBUGS END';
state.trace = [request('t1', input), result('t2', 'reject', bugs)];
renderCriticReviewHistory();
const copies = all(reviews()[0]).filter(node => node.tagName === 'pre').map(node => node.textContent);
assert(copies.includes(input)); assert(copies.includes(bugs)); assert(copies.includes('RAW reject: ' + bugs));
assert(!all(reviews()[0]).some(node => node.tagName === 'script'));
""")

    def test_expansion_survives_live_updates_and_resets_for_a_different_job(self):
        self.run_history(r"""
state.trace = [request('t1', 'Input')];
renderCriticReviewHistory();
ui.criticReviewHistory.open = true;
cycles()[0].open = true;
reviews()[0].open = true;
reviews()[0].children.find(node => node.tagName === 'details').open = true;
state.trace.push(result('t2', 'reject', 'Bugs'));
renderCriticReviewHistory();
assert(ui.criticReviewHistory.open); assert(cycles()[0].open); assert(reviews()[0].open);
assert(reviews()[0].children.find(node => node.tagName === 'details').open);
state.runId = 'another-job';
renderCriticReviewHistory();
assert(!ui.criticReviewHistory.open); assert(!cycles()[0].open); assert(!reviews()[0].open);
ui.criticReviewHistory.open = true; cycles()[0].open = true; reviews()[0].open = true;
state.batchId = 'another-batch';
renderCriticReviewHistory();
assert(!ui.criticReviewHistory.open); assert(!cycles()[0].open); assert(!reviews()[0].open);
""")

    def test_workflow_boundaries_cannot_assign_an_abandoned_input_to_a_later_result(self):
        self.run_history(r"""
for (const boundary of [
  {kind: 'author_result', stage: 'repair'},
  {kind: 'status', label: 'Workflow step: author', node: 'author'},
  {kind: 'status', label: 'Workflow step: critic', node: 'critic'},
  {kind: 'workflow_paused', node: 'author'},
  {kind: 'proof_handoff', stage: 'critic'},
  {kind: 'final_result'}, {kind: 'failure_result'},
]) {
  state.trace = [request('t1', 'Abandoned input'), boundary, result('t2', 'REJECT', 'Later bugs')];
  renderCriticReviewHistory();
  assert.equal(reviews().length, 2);
  assert.match(text(reviews()[1]), /Rejected — returned to author/);
  assert.match(text(reviews()[1]), /Critical bugs: Later bugs/);
  assert.match(text(reviews()[1]), /exact input was not recorded/);
  assert(!text(reviews()[1]).includes('Abandoned input'));
}
""")

    def test_legacy_reports_show_available_data_without_inventing_text(self):
        self.run_history(r"""
state.trace = [{kind: 'critic_result', round: 1, report: {verdict: 'reject', bugs: 'Legacy bugs', solution: 'Legacy proof'}}];
renderCriticReviewHistory();
assert.match(text(reviews()[0]), /exact input was not recorded/);
assert.match(text(reviews()[0]), /Legacy bugs/);
assert.match(text(reviews()[0]), /Legacy proof/);
state.trace = [];
renderCriticReviewHistory();
assert.match(text(ui.criticReviewHistory), /No critic input or decision has been recorded/);
""")

    def test_cycles_group_rounds_and_distinguish_all_three_pass_paths(self):
        self.run_history(r"""
state.trace = [
  request('t1', 'First submission'), result('t2', 'reject', 'First rejection', {outcome: 'reject'}),
  {kind: 'author_result'},
  request('t3', 'Repaired submission'), result('t4', 'pass', '', {outcome: 'fixed'}),
  request('t5', 'Critic revision'), result('t6', 'reject', 'Second rejection', {round: 2, outcome: 'reject'}),
  {kind: 'author_result'},
  request('t7', 'Third submission'), result('t8', 'pass', '', {outcome: 'fixed'}),
  request('t9', 'Latest revision'), result('t10', 'pass', '', {round: 2, outcome: 'fixed'}),
  {kind: 'status', label: 'Critic approved', acceptanceReason: 'round_limit', round: 2, roundLimit: 2},
  {kind: 'author_result'},
  request('t11', 'Unchanged proof'), result('t12', 'pass', '', {outcome: 'pass'}),
];
renderCriticReviewHistory();
assert.equal(cycles().length, 4);
assert.equal(reviews().length, 6);
assert.equal(cycles().map(cycle => cycle.children.filter(row => row.className === 'critic-review').length).join(','), '1,2,2,1');
assert.match(text(cycles()[1]), /Review cycle 2 · Rejected — returned to author · 2 rounds/);
assert.match(text(cycles()[1]), /Round 1 · Pass with fixes — another review.*Round 2 · Rejected — returned to author/);
assert.match(text(cycles()[2]), /Review cycle 3 · Pass with changes — accepted at review limit · 2 rounds/);
assert.match(text(cycles()[2]), /without another critic round; this is not a clean pass/);
assert.match(text(cycles()[3]), /Review cycle 4 · Clean pass — proof accepted · 1 round/);
assert.equal(reviews()[2].dataset.outcome, 'reject');
assert.equal(reviews()[4].dataset.outcome, 'pass_edited_limit');
assert.equal(reviews()[5].dataset.outcome, 'pass_clean');
""")

    def test_recovered_approval_is_grouped_once_without_changing_incremental_trace(self):
        self.run_history(r"""
const approval = {kind: 'status', label: 'Critic approved', time: '2026-10-16T14:02:00Z',
  text: 'Reached MAX (2 rounds); using the latest repaired proof.'};
state.trace = [request('2026-10-16T14:00:00Z', 'Input'),
  result('2026-10-16T14:01:00Z', 'pass', '', {round: 2, outcome: 'fixed'}),
  {kind: 'request', stage: 'final', time: '2026-10-16T14:03:00Z'}];
state.criticAcceptances = [approval];
state.traceVersion = 999; state.traceFrom = 998;
state.phase = 'done'; state.activeNode = 'latex_editor';
const original = JSON.stringify(state.trace);
renderCriticReviewHistory();
assert.equal(cycles().length, 1); assert.equal(reviews().length, 1);
assert.match(text(cycles()[0]), /Pass with changes — accepted at review limit/);
assert.equal(JSON.stringify(state.trace), original);
assert.equal(state.traceVersion, 999); assert.equal(state.traceFrom, 998);
state.trace = [state.trace[0], state.trace[1], approval, state.trace[2]];
renderCriticReviewHistory();
assert.equal(cycles().length, 1); assert.equal(reviews().length, 1);
assert.equal(reviews()[0].dataset.outcome, 'pass_edited_limit');
""")

    def test_a_round_awaiting_its_coordinator_says_what_the_job_does(self):
        self.run_history(r"""
state.trace = [{kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: 1, time: 'v1'},
  request('v1-audit1', 'Audit input', {label: 'Independent critic audit 1'})];
const note = () => text(reviews()[0]);
renderCriticReviewHistory();
assert.match(note(), /Round 1 · Awaiting decision/);
assert.match(note(), /The critic is starting\. Its exact input appears here once it is sent\./);
assert.match(note(), /Coordinator decision Not decided yet\./);
// No critic runs while the job is paused (by an error, a safety stop or a restart): the round starts again.
for (const phase of ['paused', 'pausing']) {
  state.phase = phase;
  renderCriticReviewHistory();
  assert.match(note(), /Round 1 · Awaiting decision/);
  assert(note().includes('Paused before the critic was asked. The round starts again, with fresh independent '
    + 'critics, when the job resumes.'), note());
  assert(!note().includes('The critic is starting'));
}
state.phase = 'stopping';
renderCriticReviewHistory();
assert.match(note(), /The job is stopping before the critic was asked\./);
""")

    def test_open_rows_stay_open_when_the_coordinator_is_asked(self):
        self.run_history(r"""
// A round is keyed by its visit's start until its coordinator's input exists, then by that input.
state.trace = [{kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: 1, time: 'v1'},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: 1, time: 'v1-proof'},
  ...[1, 2, 3].map(index => request(`v1-audit${index}`, 'Audit input', {label: `Independent critic audit ${index}`}))];
renderCriticReviewHistory();
cycles()[0].open = true; reviews()[0].open = true;
const keys = () => [cycles()[0].dataset.reviewKey, reviews()[0].dataset.reviewKey];
const before = keys();
state.trace = [...state.trace, request('v1-coordinator', 'Coordinator input', {label: 'Critic coordinator'})];
renderCriticReviewHistory();
assert.notDeepEqual(keys(), before);
assert(cycles()[0].open); assert(reviews()[0].open);
state.trace = [...state.trace, result('v1-result', 'pass', '', {outcome: 'fixed'})];
renderCriticReviewHistory();
assert(cycles()[0].open); assert(reviews()[0].open);
""")

    def test_a_resumed_round_is_one_round_with_its_earlier_input(self):
        self.run_history(r"""
// Round 1's coordinator was interrupted; the restarted runner continued the same visit.
const visit = (tag, number) => [
  {kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: number, time: `${tag}-step`},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: number, time: `${tag}-proof`},
  ...[1, 2, 3].map(index => request(`${tag}-audit${index}`, 'Audit input', {label: `Independent critic audit ${index}`}))];
state.trace = [...visit('v1', 1), request('v1-c', 'Interrupted coordinator input', {label: 'Critic coordinator'}),
  {kind: 'workflow_paused', stage: 'critic', node: 'critic', time: 'v1-paused'},
  ...visit('v1b', 1), request('v1b-c', 'Resumed coordinator input', {label: 'Critic coordinator'}),
  result('v1b-r', 'pass', '', {outcome: 'fixed'}), ...visit('v2', 2)];
renderCriticReviewHistory();
assert.match(text(ui.criticReviewHistory.children[0]), /1 cycle · 2 rounds/);
assert.equal(reviews().length, 2);
assert.match(text(reviews()[0]), /^\s*Round 1 \(resumed\) · Pass with fixes — another review/);
assert.match(text(reviews()[0]), /Coordinator's exact input · attempt 1 Interrupted coordinator input/);
assert.match(text(reviews()[0]), /Coordinator's exact input · attempt 1 after resume Resumed coordinator input/);
assert.match(text(reviews()[1]), /^\s*Round 2 · Awaiting decision/);
""")


if __name__ == '__main__':
    unittest.main()
