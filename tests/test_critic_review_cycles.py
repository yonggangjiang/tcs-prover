"""Critic metadata distinguishes proof edits, acceptance, and submission cycles."""

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class CriticReviewCycleTests(unittest.TestCase):
    def check(self, assertions):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let source = fs.readFileSync(process.argv[1], 'utf8');
source = source.slice(source.indexOf('// Group the recorded critic calls'), source.indexOf('function criticReviewModel('));
const request = (time, proof, extra = {}) => ({kind: 'request', stage: 'critic', time, attempt: 1,
  text: 'CRITIC INSTRUCTIONS:\nCheck every step.\n\nSTATEMENT:\nThe statement.\n\nCANDIDATE SOLUTION:\n' + proof + '\n', ...extra});
const result = (time, verdict, proof, extra = {}) => ({kind: 'critic_result', stage: 'critic', time,
  round: 1, report: {verdict, solution: proof, bugs: verdict === 'reject' ? 'The exact rejection.' : ''},
  text: 'RAW DECISION ' + time, ...extra});
const step = (node, visit = 1) => ({kind: 'status', label: 'Workflow step: ' + node, node, visit});
const author = time => ({kind: 'author_result', time, text: 'The new submitted proof.'});
const approved = (text, extra = {}) => ({kind: 'status', label: 'Critic approved', text, ...extra});
vm.runInNewContext(source + process.argv[2], {assert, request, result, step, author, approved});
"""
        source = Path(__file__).resolve().parents[1] / "ui" / "app.js"
        completed = subprocess.run([shutil.which("node"), "-e", script, str(source), assertions],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_live_sequence_groups_three_submissions_and_rounds(self):
        self.check(r"""
const cycles = collectCriticReviewCycles([
 author('a1'), step('critic'), request('q1', 'A'), result('r1', 'reject', 'A'), step('author'),
 author('a2'), step('critic'), request('q2', 'B'), result('r2', 'pass', 'B fixed'),
 step('critic', 2), request('q3', 'B fixed'), result('r3', 'reject', 'B repaired', {round: 2}), step('author'),
 author('a3'), step('critic'), request('q4', 'C'), result('r4', 'pass', 'C fixed'),
 step('critic', 2), request('q5', 'C fixed'), result('r5', 'pass', 'C fixed again', {round: 2}),
 approved('Reached MAX (2 rounds); using the latest repaired proof.'), step('latex_editor')]);
assert.deepEqual(cycles.map(c => c.rounds.length), [1, 2, 2]);
assert.deepEqual(cycles.map(c => c.outcome), ['reject', 'reject', 'pass_edited_limit']);
assert.equal(cycles[1].rounds[0].outcome, 'pass_edited_continue');
assert.equal(cycles[2].rounds[0].outcome, 'pass_edited_continue');
assert.equal(cycles[2].rounds[1].roundLimit, 2);
assert.match(criticCycleOutcomeLabel(cycles[2]), /accepted at review limit/);
assert.equal(cycles[1].rounds[1].result.report.bugs, 'The exact rejection.');
""")

    def test_clean_pass_uses_exact_workflow_normalization(self):
        self.check(r"""
let cycles = collectCriticReviewCycles([request('q1', '\nA  \r\n B\t\n'), result('r1', 'pass', 'A\n B')]);
assert.equal(cycles[0].outcome, 'pass_clean');
assert.equal(cycles[0].rounds[0].outcomeEvidence, 'candidate_comparison');
assert.equal(criticCycleOutcomeLabel(cycles[0]), 'Clean pass — proof accepted');
for (const edited of ['A\nB', 'A B', 'A\n  B']) {
 cycles = collectCriticReviewCycles([request('q1', 'A\n B'), result('r1', 'pass', edited)]);
 assert.equal(cycles[0].outcome, 'pass_unknown');
 assert.equal(cycles[0].rounds[0].proofChanged, true);
}
""")

    def test_approval_evidence_overrides_missing_input(self):
        self.check(r"""
const clean = collectCriticReviewCycles([result('r1', 'pass', 'A'), approved('Round 1 passed without edits.')]);
assert.equal(clean[0].outcome, 'pass_clean');
const limit = collectCriticReviewCycles([result('r1', 'pass', 'A'),
 approved('Custom label', {acceptanceReason: 'round_limit', roundLimit: 4})]);
assert.equal(limit[0].outcome, 'pass_edited_limit');
assert.equal(limit[0].rounds[0].roundLimit, 4);
const unknown = collectCriticReviewCycles([result('r1', 'pass', 'A'), approved('Something passed.')]);
assert.equal(unknown[0].outcome, 'pass_unknown');
""")

    def test_explicit_runtime_outcomes_and_transitions(self):
        self.check(r"""
for (const [outcome, expected] of [['pass', 'pass_clean'], ['reject', 'reject'],
 ['pass_edited_limit', 'pass_edited_limit'], ['pass_edited_continue', 'pass_edited_continue']]) {
 const cycles = collectCriticReviewCycles([result('r1', outcome === 'reject' ? 'reject' : 'pass', 'A', {outcome})]);
 assert.equal(cycles[0].outcome, expected);
}
let cycles = collectCriticReviewCycles([result('r1', 'pass', 'A', {outcome: 'fixed'})]);
assert.equal(cycles[0].outcome, 'pass_unknown');
assert.match(criticCycleOutcomeLabel(cycles[0]), /Pass with changes/);
cycles = collectCriticReviewCycles([result('r1', 'pass', 'A', {outcome: 'fixed', transition: 'review_again'})]);
assert.equal(cycles[0].outcome, 'pass_edited_continue');
cycles = collectCriticReviewCycles([result('r1', 'pass', 'A', {outcome: 'fixed'}), step('critic', 2)]);
assert.equal(cycles[0].outcome, 'pass_edited_continue');
""")

    def test_unknown_pass_is_not_accepted_from_current_settings(self):
        self.check(r"""
for (const criticRounds of [1, 2, 4]) {
 const cycles = collectCriticReviewCycles([result('r1', 'pass', 'A')], {phase: 'done', criticRounds});
 assert.equal(cycles[0].outcome, 'pass_unknown'); assert.equal(cycles[0].accepted, false);
 assert.match(criticCycleOutcomeLabel(cycles[0]), /not recorded/);
}
const cycles = collectCriticReviewCycles([request('q1', 'A'), result('r1', 'pass', 'B'), step('latex_editor')]);
assert.equal(cycles[0].outcome, 'pass_edited_limit');
assert.equal(cycles[0].rounds[0].outcomeEvidence, 'final_transition');
""")

    def test_attempt_retries_are_one_round_and_replays_are_deduplicated(self):
        self.check(r"""
const q1 = request('q1', 'A'), q2 = request('q2', 'Retry context', {attempt: 2});
const r1 = result('r1', 'reject', 'A');
const cycles = collectCriticReviewCycles([q1, q1, q2, q2, r1, r1]);
assert.equal(cycles.length, 1); assert.equal(cycles[0].rounds.length, 1);
assert.equal(cycles[0].rounds[0].requests.length, 2);
assert.equal(cycles[0].rounds[0].requests[1].text, q2.text);
""")

    def test_interrupted_rounds_stay_in_same_cycle_on_critic_resume(self):
        self.check(r"""
const cycles = collectCriticReviewCycles([author('a'), step('critic'), request('q1', 'A'),
 {kind: 'workflow_paused', node: 'critic'}, step('critic'), request('q2', 'A'), result('r1', 'pass', 'B'),
 step('critic', 2), request('q3', 'B')], {phase: 'running', activeNode: 'critic'});
assert.equal(cycles.length, 1); assert.equal(cycles[0].rounds.length, 3);
assert.deepEqual(cycles[0].rounds.map(r => r.outcome), ['interrupted', 'pass_edited_continue', 'pending']);
assert.deepEqual(cycles[0].rounds.map(r => r.number), [1, 2, 3]);
assert.deepEqual(cycles[0].rounds.map(r => r.roundNumber), [1, 1, 2]);
""")

    def test_author_boundary_does_not_borrow_abandoned_input(self):
        self.check(r"""
for (const boundary of [author('a'), step('author'), {kind:'request', stage:'repair'},
 {kind:'workflow_paused', node:'author'}]) {
 const cycles = collectCriticReviewCycles([request('q1', 'A'), boundary, result('r1', 'reject', 'B')]);
 assert.equal(cycles.length, 2); assert.equal(cycles[0].rounds[0].outcome, 'interrupted');
 assert.equal(cycles[1].rounds[0].requests.length, 0);
}
""")

    def test_out_of_order_ids_preserve_inputs(self):
        self.check(r"""
const cycles = collectCriticReviewCycles([request('q1', 'A', {id:'a'}), request('q2', 'B', {id:'b'}),
 result('r2', 'reject', 'B', {requestId:'b'}), result('r1', 'pass', 'A', {requestId:'a'})]);
assert.equal(cycles.length, 1); assert.equal(cycles[0].rounds.length, 2);
assert.equal(cycles[0].rounds[0].result.requestId, 'a');
assert.equal(cycles[0].rounds[1].result.requestId, 'b');
assert.equal(cycles[0].outcome, 'reject');
""")

    def test_independent_audits_are_excluded_and_long_text_preserved(self):
        self.check(r"""
const long = '<script>plain text</script>' + 'x'.repeat(90000);
const cycles = collectCriticReviewCycles([request('q0', 'Unrelated', {label:'Independent critic audit 1'}),
 request('q00', 'Unrelated', {activityLabel:'Audit 2'}), request('q000', 'Unrelated', {root:false}),
 request('q1', long), result('r1', 'reject', long)]);
assert.equal(cycles.length, 1); assert.equal(cycles[0].rounds[0].requests.length, 1);
assert(cycles[0].rounds[0].requests[0].text.includes(long));
assert.equal(cycles[0].rounds[0].result.report.solution, long);
""")

    def test_file_snapshot_suffix_is_not_part_of_proof(self):
        self.check(r"""
const q = request('q1', 'The proof.');
q.text += '\n\nREAD-ONLY PROOF FILES:\nYour working directory contains proof.md (the exact candidate), '
 + 'statement.md (the exact task), and manifest.json (content hashes and provenance). '
 + 'Read both files completely; use successive ranges if tool output is truncated. '
 + 'Give independent subagents these same files and working directory. '
 + "Review only this snapshot. Do not resolve links into the author's workspace. "
 + 'Return the complete proof text in solution, never a file pointer.';
let cycles = collectCriticReviewCycles([q, result('r1', 'pass', 'The proof.')]);
assert.equal(cycles[0].outcome, 'pass_clean');
q.text += '\n\nOUTPUT JSON CONTRACT\nReturn exactly one JSON object and no markdown fences or commentary. '
 + 'The object must match this JSON Schema exactly:\n{"type":"object"}';
cycles = collectCriticReviewCycles([q, result('r1', 'pass', 'The proof.')]);
assert.equal(cycles[0].outcome, 'pass_clean');
""")

    def test_ambiguous_candidate_prompt_and_retry_without_base_remain_unknown(self):
        self.check(r"""
for (const q of [request('q1', 'A\n\nCANDIDATE SOLUTION:\nA'),
 request('q2', 'A', {attempt: 2}), request('q3', 'A', {text:'Unrecognized prompt A'}),
 request('q4', 'A\n\nREAD-ONLY PROOF FILES:\nunknown appendix')]) {
 const cycles = collectCriticReviewCycles([q, result('r1', 'pass', 'A')]);
 assert.equal(cycles[0].outcome, 'pass_unknown');
 assert.equal(cycles[0].rounds[0].candidateText, null);
}
""")

    def test_proof_handoff_is_round_metadata_not_a_new_cycle(self):
        self.check(r"""
const cycles = collectCriticReviewCycles([author('a'), step('critic'),
 {kind:'proof_handoff', round:1, manifest:{visit:1}}, request('q1', 'A'), result('r1', 'pass', 'B'),
 step('critic', 2), {kind:'proof_handoff', round:2, manifest:{visit:2}}, request('q2', 'B')]);
assert.equal(cycles.length, 1); assert.equal(cycles[0].rounds.length, 2);
assert.equal(cycles[0].rounds[0].handoff.manifest.visit, 1);
assert.equal(cycles[0].rounds[1].handoff.manifest.visit, 2);
""")

    def test_pruning_unrelated_events_preserves_expanded_detail_keys(self):
        self.check(r"""
const trace = [author('a'), {kind:'diagnostic', text:'Some progress'}, step('critic'), request('q1', 'A'),
 {kind:'delta', text:'Thinking'}, result('r1', 'pass', 'B'), step('critic', 2), request('q2', 'B')];
const before = collectCriticReviewCycles(trace);
const after = collectCriticReviewCycles(trace.filter(e => !['diagnostic','delta'].includes(e.kind)));
assert.equal(before[0].key, after[0].key);
assert.deepEqual(before[0].rounds.map(r => r.key), after[0].rounds.map(r => r.key));
""")

    def test_labels_handle_absent_legacy_decisions(self):
        self.check(r"""
assert.equal(criticRoundOutcomeLabel(), 'Verdict unavailable');
assert.equal(criticCycleOutcomeLabel(), 'Verdict unavailable');
assert.equal(criticCycleOutcomeLabel({outcome: 'pending'}), 'Awaiting decision');
""")

    def test_incremental_recovered_approval_refreshes_existing_timeline_card(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const model = source.slice(source.indexOf('// Group the recorded critic calls'),
  source.indexOf('function renderCriticReviewHistory('));
const describe = source.slice(source.indexOf('function describe(entry)'), source.indexOf('function cardIcon('));
const start = source.indexOf('function render(next) {');
// Exercise the real incremental merge/redraw code without mocking unrelated DOM.
const render = source.slice(start, source.indexOf('  const phase = state.phase;', start)) + '\n}';
const refreshed = [], ingested = [];
const context = {assert, refreshed, ingested, retainTrace: entries => entries,
  ingest: (entries, reset) => ingested.push({entries, reset}),
  upsertTimeline: card => refreshed.push(card),
  state: {runId: 'a', phase: 'running', activeNode: 'critic', traceVersion: 17,
    criticAcceptances: [], trace: [{kind: 'critic_result', stage: 'critic',
      time: '2026-10-16T14:01:00Z', round: 2, outcome: 'fixed',
      report: {verdict: 'pass', solution: 'Repaired proof', bugs: ''}}]}};
vm.runInNewContext(model + describe + render + `
const before = describe(state.trace[0]);
assert.match(before.label, /Pass with changes — next step not recorded/);
const original = state.trace[0];
const approval = {kind: 'status', label: 'Critic approved', time: '2026-10-16T14:02:00Z',
  text: 'Reached MAX (2 rounds); using the latest repaired proof.'};
render({...state, trace: [], traceFrom: 17, criticAcceptances: [approval]});
assert.equal(ingested.length, 1); assert.equal(ingested[0].entries.length, 0);
assert.equal(ingested[0].reset, false);
assert.equal(refreshed.length, 1);
assert.equal(refreshed[0].key, before.key);
assert.match(refreshed[0].label, /Pass with changes — accepted at review limit/);
assert.match(refreshed[0].text, /without another critic round/);
assert.equal(state.trace.length, 1); assert.equal(state.trace[0], original);
assert.equal(state.traceVersion, 17); assert.equal(state.traceFrom, 17);
render({...state, trace: [], traceFrom: 17, criticAcceptances: [approval]});
assert.equal(refreshed.length, 1, 'Unchanged polling metadata does not redraw the same card again');
`, context);
"""
        source = Path(__file__).resolve().parents[1] / "ui" / "app.js"
        completed = subprocess.run([shutil.which("node"), "-e", script, str(source)],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
