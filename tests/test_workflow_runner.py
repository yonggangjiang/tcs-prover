"""Offline behavioral tests. No paid model calls are made."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as runtime

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
        self.calls.append(settings.get('request_label', stage))
        if stage == 'critic':
            self.assertEqual(settings['features'], ['multi_agent'])
            value = review_report()
        elif 'latex' in schema['properties']:
            value = {'latex': LATEX}
        else:
            raise AssertionError(stage)
        runtime.validate_json_schema(value, schema)
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
        with workspace() as directory, patch.object(runtime, 'structured', side_effect=self.model), patch.object(runtime, '_run_command') as command:
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml', ROOT/'workflows/clean_up.yaml'], {'statement': 'Exact test task'})
            self.assertFalse(state['failed'])
            self.assertEqual(state['output'], LATEX)
            self.assertEqual(self.calls.count('critic'), 1)
            self.assertEqual(self.calls, ['critic', 'final'])
            command.assert_not_called()
            self.assertFalse((directory/'formatted-candidate.tex').exists())
            self.assertEqual(len(self.goal_calls), 1)
            self.assertFalse((directory/'research.sqlite3').exists())

    def test_edited_pass_repeats_to_max_then_outputs_latest_proof(self):
        reports, events = [], []
        def model(prompt, schema, stage, **settings):
            self.assertEqual(stage, 'critic')
            self.assertEqual(settings['features'], ['multi_agent'])
            self.assertIn(reports[-1]['solution'] if reports else PROOF, prompt)
            report = review_report(PROOF + ' repaired' * (len(reports) + 1))
            reports.append(report)
            return report, json.dumps(report)
        with workspace() as directory, patch.object(runtime, 'structured', side_effect=model), patch.object(runtime, 'emit', side_effect=lambda *args, **kwargs: events.append((args, kwargs))):
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],
                {'statement': 'Task', 'solution': PROOF}, {'start_node': 'critic', 'critic_rounds': 3})
            self.assertFalse(state.get('failed'))
            self.assertEqual(len(reports), 3)
            self.assertEqual(state['output'], PROOF + ' repaired' * 3)
            self.assertEqual((directory/'saved-candidate.md').read_text().strip(), state['output'])
            approvals = [i for i, (_, fields) in enumerate(events) if fields.get('label') == 'Critic approved']
            critic_results = [i for i, (args, _) in enumerate(events) if args[0] == 'critic_result']
            self.assertEqual(len(approvals), 1)
            self.assertGreater(approvals[0], critic_results[-1])

    def test_unchanged_pass_after_repair_finishes_before_max(self):
        repaired = PROOF + ' repaired'
        report = review_report(repaired)
        with workspace(), patch.object(runtime, 'structured', return_value=(report, json.dumps(report))) as model:
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],
                {'statement': 'Task', 'solution': PROOF}, {'start_node': 'critic', 'critic_rounds': 5})
            self.assertEqual(model.call_count, 2)
            self.assertEqual(state['output'], repaired)

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
        with workspace() as directory, patch.object(runtime, 'goal_session', side_effect=author) as session, patch.object(runtime, 'structured', side_effect=model):
            result = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'], {'statement': 'Task'})
            self.assertEqual(session.call_count, 1)
            self.assertEqual(feedback[0]['bugs'], 'A gap remains.')
            self.assertEqual(feedback[0]['solution'], PROOF + ' safe repair')
            self.assertEqual(feedback[0]['round'], 2)
            self.assertEqual(result['output'], revised + ' repaired' * 4)
            self.assertEqual(len(rounds), 4)
            self.assertEqual((directory/'saved-candidate.md').read_text().strip(), result['output'])

    def test_convenience_critic_makes_one_llm_call_with_subagents_enabled(self):
        report = review_report()
        with workspace(), patch.object(runtime, 'structured', return_value=(report, json.dumps(report))) as model:
            self.assertEqual(runtime.criticize('Task', PROOF, 1), report)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(model.call_args.kwargs['features'], ['multi_agent'])

    def test_invalid_critic_result_cannot_approve_or_erase_saved_candidate(self):
        invalid = [review_report(''), {**review_report(), 'bugs': 'An unresolved gap.'},
                   {**review_report(verdict='reject'), 'bugs': ''}]
        for report in invalid:
            with self.subTest(report=report), workspace() as directory, patch.object(runtime, 'structured', return_value=(report, json.dumps(report))):
                with self.assertRaises(runtime.Error):
                    runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],
                        {'statement': 'Task', 'solution': PROOF}, {'start_node': 'critic'})
                self.assertEqual((directory/'saved-candidate.md').read_text().strip(), PROOF)

    def test_no_call_starts_after_shared_deadline(self):
        with workspace(),patch.object(runtime,'structured') as model:
            with self.assertRaises(runtime.Error):
                runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'],{'statement':'Task'},
                    {'thinking_hours':0.001,'elapsed_seconds':10})
            model.assert_not_called()

    def test_bounded_calls_have_no_stale_internal_retry_budget(self):
        settings=runtime._bounded_request_settings({'attempts':2,'timeout':900},
                    {'thinking_hours':1,'elapsed_seconds':3599})
        self.assertEqual(settings['attempts'],1)
        self.assertLessEqual(settings['timeout'],1)

    def test_cleanup_is_one_editor_call_for_proof_or_standalone_tex(self):
        for initial in ({'statement': 'Task', 'solution': PROOF}, {'source': LATEX}):
            source = initial.get('solution', initial.get('source'))
            def model(prompt, schema, stage, **settings):
                self.assertEqual(stage, 'final')
                self.assertEqual(prompt, runtime.FINAL_PROMPT + '\n\nSOLUTION:\n' + source)
                self.assertEqual(settings['features'], [])
                return {'latex': LATEX}, json.dumps({'latex': LATEX})
            with self.subTest(initial=initial), workspace() as directory, patch.object(runtime, 'structured', side_effect=model) as editor, patch.object(runtime, '_run_command') as command, patch.object(runtime, 'emit') as emit:
                state = runtime.execute_workflows([ROOT/'workflows/clean_up.yaml'], dict(initial))
                self.assertEqual(editor.call_count, 1)
                self.assertEqual(state['output'], LATEX)
                self.assertEqual(list(directory.iterdir()), [])
                command.assert_not_called()
                completion = [call for call in emit.call_args_list if call.args[0] == 'final_result']
                self.assertEqual(len(completion), 1)
                self.assertEqual(completion[0].kwargs['output'], LATEX)


if __name__=='__main__':
    unittest.main()
