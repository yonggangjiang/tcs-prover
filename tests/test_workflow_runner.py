"""Offline behavioral tests. No paid model calls are made."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from tests import critic_doubles

ROOT = Path(runtime.__file__).parent
PROOF = 'Every allowed case follows by the explicit argument supplied here.'
LATEX = r'\documentclass{article}\begin{document}An explicit argument.\end{document}'


def review_report(solution=PROOF, verdict='pass'):
    return {'verdict': verdict, 'solution': solution,
            'bugs': 'A gap remains.' if verdict == 'reject' else ''}



@contextlib.contextmanager
def workspace():
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as folder:
        os.chdir(folder)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                yield Path(folder)
        finally:
            os.chdir(previous)


class PipelineTests(unittest.TestCase):
    def model(self, prompt, schema, stage, **settings):
        # A stand-in for structured() (critic_doubles answers the convention critic): the edited
        # file's text comes back in its result field.
        self.calls.append(settings.get('request_label', stage))
        if stage == 'critic':
            # One critic call, which starts its independent critics as subagents.
            self.assertEqual((settings['features'], settings['subagents']), (['multi_agent'], 3))
            value, field = {**review_report(), 'fixes': ''}, 'solution'
        elif stage == 'final':
            value, field = {'summary': 'Wrote the paper.', 'latex': LATEX}, 'latex'
        else:
            raise AssertionError(stage)
        self.assertEqual(settings['edit_file'].field, field)
        runtime.validate_json_schema({key: item for key, item in value.items() if key != field}, schema)
        return value, json.dumps(value)

    def setUp(self):
        self.calls = []
        self.goal_calls = []
        def goal(runtime, prompt, **kwargs):
            self.goal_calls.append(prompt)
            self.assertFalse(any(Path.cwd().iterdir()))
            yield {'outcome': 'done', 'output': PROOF}
        patcher = patch.object(runtime, 'goal_session', side_effect=goal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unchanged_first_critic_pass_goes_directly_to_final_document(self):
        with workspace() as directory, patch.object(runtime, 'structured', side_effect=critic_doubles.coordinator(self.model)), patch.object(runtime, '_run_command', return_value={'status': 'pass', 'output': ''}) as command:
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml', ROOT/'workflows/clean_up.yaml'], {'statement': 'Exact test task'})
            self.assertNotIn('failed', state)
            self.assertEqual(state['output'], LATEX)
            # The convention critic (answered by critic_doubles) runs between the critic and the writer.
            self.assertEqual(self.calls, ['Critic coordinator', 'final'])
            command.assert_called_once()
            self.assertEqual((directory/'final.tex').read_text(), LATEX)
            self.assertFalse((directory/'formatted-candidate.tex').exists())
            self.assertEqual(len(self.goal_calls), 1)
            self.assertFalse((directory/'research.sqlite3').exists())

    def test_edited_passes_up_to_max_end_with_a_read_only_verification_round(self):
        reports, events = [], []
        def model(prompt, schema, stage, **settings):
            self.assertEqual(stage, 'critic')
            self.assertEqual(settings['features'], ['multi_agent'])
            # The candidate is the critic's proof.md, never repeated in its prompt.
            candidate = dict(settings['input_files'])['proof.md']
            self.assertEqual(candidate, reports[-1]['solution'] if reports else PROOF)
            self.assertNotIn(candidate, prompt)
            verifying = len(reports) == 3
            self.assertEqual('edit_file' in settings, not verifying)
            self.assertEqual('FINAL VERIFICATION' in prompt, verifying)
            # Whatever the verification round answers, it cannot change the proof.
            report = review_report(PROOF + ' repaired' * (len(reports) + 1))
            reports.append(report)
            return report, json.dumps(report)
        with workspace() as directory, patch.object(runtime, 'structured', side_effect=critic_doubles.coordinator(model)), patch.object(runtime, 'emit', side_effect=lambda *args, **kwargs: events.append((args, kwargs))):
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],
                {'statement': 'Task', 'solution': PROOF}, {'start_node': 'critic', 'critic_rounds': 3})
            self.assertFalse(state.get('failed'))
            self.assertEqual(len(reports), 4)
            self.assertEqual(state['output'], PROOF + ' repaired' * 3)
            self.assertEqual((directory/'saved-candidate.md').read_text().strip(), state['output'])
            approvals = [i for i, (_, fields) in enumerate(events) if fields.get('label') == 'Critic approved']
            critic_results = [i for i, (args, _) in enumerate(events) if args[0] == 'critic_result']
            self.assertEqual(len(approvals), 1)
            self.assertGreater(approvals[0], critic_results[-1])
            self.assertEqual([events[i][1]['outcome'] for i in critic_results], ['fixed'] * 3 + ['pass'])
            acceptance = events[approvals[0]][1]
            self.assertEqual(acceptance['acceptanceReason'], 'clean_pass')
            self.assertEqual(acceptance['round'], 4)

    def test_unchanged_pass_after_repair_finishes_before_max(self):
        repaired = PROOF + ' repaired'
        report = review_report(repaired)
        decisions = critic_doubles.coordinator(lambda *args, **kwargs: (report, json.dumps(report)))
        with workspace(), patch.object(runtime, 'structured', side_effect=decisions) as model, patch.object(runtime, 'emit') as emit:
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],
                {'statement': 'Task', 'solution': PROOF}, {'start_node': 'critic', 'critic_rounds': 5})
            labels = [call.kwargs['request_label'] for call in model.call_args_list]
            self.assertEqual(labels.count('Critic coordinator'), 2)
            self.assertEqual(state['output'], repaired)
            decisions = [call.kwargs for call in emit.call_args_list if call.args[0] == 'critic_result']
            self.assertEqual([entry['outcome'] for entry in decisions], ['fixed', 'pass'])
            approval = next(call.kwargs for call in emit.call_args_list if call.kwargs.get('label') == 'Critic approved')
            self.assertEqual(approval['acceptanceReason'], 'clean_pass')
            self.assertEqual(approval['round'], 2)

    def test_rejection_at_max_returns_to_same_author_and_resets_rounds(self):
        revised = PROOF + ' with the objection resolved.'
        feedback, rounds = [], []
        def author(runtime, prompt, **kwargs):
            feedback.append((yield {'outcome': 'done', 'output': PROOF}))
            yield {'outcome': 'done', 'output': revised}
        def model(prompt, schema, stage, **settings):
            self.assertEqual(stage, 'critic')
            rounds.append(prompt)
            if len(rounds) == 2:
                report = review_report(PROOF + ' safe repair', verdict='reject')
            else:
                report = review_report((PROOF if len(rounds) == 1 else revised) + ' repaired' * len(rounds))
            return report, json.dumps(report)
        with workspace() as directory, patch.object(runtime, 'goal_session', side_effect=author) as session, patch.object(runtime, 'structured', side_effect=critic_doubles.coordinator(model)):
            result = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'], {'statement': 'Task'})
            self.assertEqual(session.call_count, 1)
            self.assertEqual(feedback[0]['bugs'], 'A gap remains.')
            self.assertEqual(feedback[0]['solution'], PROOF + ' safe repair')
            self.assertEqual(feedback[0]['round'], 2)
            self.assertEqual(result['output'], revised + ' repaired' * 4)
            # The fifth round verifies the last edits read-only.
            self.assertEqual(len(rounds), 5)
            self.assertEqual((directory/'saved-candidate.md').read_text().strip(), result['output'])

    def test_convenience_critic_runs_one_critic_call_with_its_subagents(self):
        report = {**review_report(), 'fixes': ''}
        decisions = critic_doubles.coordinator(lambda *args, **kwargs: (report, json.dumps(report)))
        with workspace(), patch.object(runtime, 'structured', side_effect=decisions) as model:
            result = runtime.criticize('Task', PROOF, 1)
            # The record of the proof's changes joins the decision; nothing else does.
            joined = {'proofChanged', 'proofDiff'}
            self.assertEqual({key: value for key, value in result.items() if key not in joined}, report)
            self.assertEqual((result['proofChanged'], result['proofDiff']), (False, ''))
            self.assertEqual([call.kwargs['request_label'] for call in model.call_args_list], ['Critic coordinator'])
            call = model.call_args_list[0]
            self.assertEqual((call.kwargs['features'], call.kwargs['subagents']), (['multi_agent'], 3))
            self.assertIn('Spawn exactly 3 fresh independent subagent(s)', call.args[0])

    def test_invalid_critic_result_cannot_approve_or_erase_saved_candidate(self):
        invalid = [review_report(''), {**review_report(), 'bugs': 'An unresolved gap.'},
                   {**review_report(verdict='reject'), 'bugs': ''}]
        for report in invalid:
            decisions = critic_doubles.coordinator(lambda *args, report=report, **kwargs: (report, json.dumps(report)))
            with self.subTest(report=report), workspace() as directory, patch.object(runtime, 'structured', side_effect=decisions), patch.object(runtime, 'emit') as emit:
                state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],
                    {'statement': 'Task', 'solution': PROOF}, {'start_node': 'critic'})
                # The critic step pauses with its exact input and is resumed there automatically.
                self.assertTrue(state['paused'])
                self.assertNotIn('output', state)
                self.assertEqual(state['solution'], PROOF)
                pause = next(call.kwargs for call in emit.call_args_list if call.args[0] == 'workflow_paused')
                self.assertEqual(pause['node'], 'critic')
                self.assertNotIn('safetyStop', pause)
                self.assertEqual((directory/'saved-candidate.md').read_text().strip(), PROOF)

    def test_no_call_starts_after_shared_deadline(self):
        with workspace(),patch.object(runtime,'structured') as model:
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],{'statement':'Task'},
                {'thinking_hours':0.001,'elapsed_seconds':10})
            self.assertTrue(state['paused'])
            model.assert_not_called()

    def test_bounded_calls_have_no_stale_internal_retry_budget(self):
        options = {'thinking_hours': 1, 'elapsed_seconds': 3599}
        settings = runtime._bounded_request_settings({'attempts': 2, 'timeout': 900}, options)
        # No internal attempt budget: structured() retries every failure, rechecking
        # the shared deadline before each attempt, so none can outlive it. The
        # node's timeout is its inactivity limit, not a deadline.
        self.assertNotIn('attempts', settings)
        self.assertEqual(settings['timeout'], 900)
        self.assertLessEqual(runtime._attempt_timeout(None, options), 1)
        self.assertIs(settings['options'], options)
        with patch.object(runtime, 'workflow_remaining', return_value=0):
            with self.assertRaisesRegex(runtime.Error, 'time limit'):
                runtime._bounded_request_settings({'timeout': 900}, options)

    def test_successful_cleanup_is_one_editor_call_then_compilation(self):
        for initial in ({'statement': 'Task', 'solution': PROOF}, {'source': LATEX}):
            source = initial.get('solution', initial.get('source'))
            def model(prompt, schema, stage, **settings):
                self.assertEqual(stage, 'final')
                self.assertEqual(prompt, runtime.FINAL_PROMPT.strip() + '\n\nEXACT STATEMENT (when supplied):\n'
                                 + initial.get('statement', '') + '\n\nSOLUTION:\n' + source)
                self.assertEqual(settings['features'], [])
                # The editor writes latex-source.tex in its own workspace; it starts absent.
                self.assertEqual((settings['edit_file'].name, settings['edit_file'].initial), ('latex-source.tex', None))
                return {'summary': 'Wrote it.', 'latex': LATEX}, json.dumps({'summary': 'Wrote it.'})
            with self.subTest(initial=initial), workspace() as directory, patch.object(runtime, 'structured', side_effect=model) as editor, patch.object(runtime, '_run_command', return_value={'status': 'pass', 'output': ''}) as command, patch.object(runtime, 'emit') as emit:
                state = runtime.execute_workflows([ROOT/'workflows/clean_up.yaml'], dict(initial))
                self.assertEqual(editor.call_count, 1)
                self.assertEqual(state['output'], LATEX)
                self.assertEqual((directory/'final.tex').read_text(), LATEX)
                self.assertEqual((directory/'latex-source.tex').read_text(), LATEX)
                command.assert_called_once()
                completion = [call for call in emit.call_args_list if call.args[0] == 'final_result']
                self.assertEqual(len(completion), 1)
                self.assertEqual(completion[0].kwargs['output'], LATEX)

    def test_pausing_repair_checkpoints_exact_feedback_and_stops_the_chain(self):
        report = review_report(verdict='reject')
        initial = {'statement': 'Task', 'solution': PROOF, 'report': report, 'round': 2}
        with workspace(), patch.object(runtime, 'goal_session', side_effect=runtime.WorkflowPaused), patch.object(runtime, 'structured') as model, patch.object(runtime, 'emit') as emit:
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml', ROOT/'workflows/clean_up.yaml'], initial)
        self.assertTrue(state['paused'])
        self.assertEqual(state['report'], report)
        self.assertEqual(state['round'], 2)
        model.assert_not_called()
        checkpoint = next(call.kwargs for call in emit.call_args_list if call.args[0] == 'workflow_paused')
        self.assertEqual(checkpoint['node'], 'author')
        self.assertEqual(checkpoint['state'], state)

    def test_author_errors_checkpoint_reason_and_never_enter_critic_or_cleanup(self):
        interruptions = [runtime.WorkflowPaused('Connection lost', reason='Connection lost', thread_id='saved-root'),
                         RuntimeError('Unexpected author error')]
        for interruption in interruptions:
            with self.subTest(interruption=interruption), workspace(), patch.object(runtime, 'goal_session', side_effect=interruption), patch.object(runtime, 'structured') as model, patch.object(runtime, 'emit') as emit:
                state = runtime.execute_workflows(
                    [ROOT/'workflows/author_critic.yaml', ROOT/'workflows/clean_up.yaml'],
                    {'statement': 'Task', 'solution': PROOF}, options={'goal_thread_id': 'saved-root'})
            self.assertTrue(state['paused'])
            self.assertFalse(state.get('failed'))
            self.assertEqual(state['solution'], PROOF)
            model.assert_not_called()
            checkpoint = next(call.kwargs for call in emit.call_args_list if call.args[0] == 'workflow_paused')
            self.assertEqual(checkpoint['reason'], str(interruption))
            self.assertEqual(checkpoint['threadId'], 'saved-root')

    def test_elapsed_author_limit_pauses_before_starting_a_model(self):
        with workspace(), patch.object(runtime, 'workflow_remaining', return_value=0), patch.object(runtime, 'goal_session') as author, patch.object(runtime, 'emit') as emit:
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'], {'statement': 'Task'})
        self.assertTrue(state['paused'])
        author.assert_not_called()
        checkpoint = next(call.kwargs for call in emit.call_args_list if call.args[0] == 'workflow_paused')
        self.assertIn('time limit', checkpoint['reason'])

    def test_native_web_search_modes_cover_structured_stages_and_retries(self):
        for mode in ('live', 'disabled'):
            with patch.dict(runtime.os.environ, {runtime.retrieval_runtime.MODE_ENV: mode}):
                for stage in ('review', 'critic', 'final'):
                    for features in ([], ['multi_agent']):
                        arguments = runtime.structured_tool_arguments(stage, features)
                        self.assertIn(f'web_search="{mode}"', arguments)
                        self.assertIn('tools.web_search=' + str(mode == 'live').lower(), arguments)
                        historical = runtime.structured_tool_arguments(stage, features, policy={'mode': 'historical'})
                        self.assertIn('web_search="disabled"', historical)
        self.assertIn('using the supplied information', runtime.structured_retry_prompt('Task', '', {}))


if __name__=='__main__':
    unittest.main()
