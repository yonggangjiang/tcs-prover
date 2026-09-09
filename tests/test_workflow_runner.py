"""Offline behavioral tests. No paid model calls are made."""
import contextlib
import copy
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


def review_report(solution=PROOF, verdict='pass', fixed=False):
    return {'checks': [{'focus': focus, 'verdict': 'pass', 'report': 'Checked.'}
                       for focus in runtime.builtin_workflow('author_critic')['nodes']['critic']['parallel']['items']],
            'verdict': verdict, 'fixed': fixed, 'solution': solution,
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
        label = settings.get('request_label', '')
        if label.startswith('Independent critic audit'):
            value = {'focus': 'The supplied focus', 'verdict': 'pass', 'report': 'Checked all steps.'}
        elif label == 'Critic coordinator adjudication':
            value = review_report()
        elif 'latex' in schema['properties']:
            value = {'latex': LATEX}
        elif 'verdict' in schema['properties']:
            value = {'verdict': 'pass', 'bugs': ''}
        else:
            raise AssertionError(label)
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

    def test_single_goal_feeds_unchanged_critic_and_final_document(self):
        with workspace() as directory, patch.object(runtime, 'structured', side_effect=self.model), patch.object(runtime, '_run_command', return_value={'status': 'pass', 'diagnostic': 'Compiled', 'engine': 'mock'}):
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml', ROOT/'workflows/clean_up.yaml'], {'statement': 'Exact test task'})
            self.assertFalse(state['failed'])
            self.assertEqual(state['output'], LATEX)
            self.assertEqual(len([x for x in self.calls if x.startswith('Independent critic audit')]), 3)
            self.assertTrue((directory/'formatted-candidate.tex').is_file())
            self.assertEqual(len(self.goal_calls), 1)
            self.assertFalse((directory/'research.sqlite3').exists())

    def test_repair_limit_preserves_candidate_without_approval_or_cleanup(self):
        def model(prompt, schema, stage, **settings):
            if settings.get('request_label') == 'Critic coordinator adjudication':
                report = review_report(PROOF + ' repaired', fixed=True)
                return report, json.dumps(report)
            return self.model(prompt, schema, stage, **settings)
        with workspace() as directory, patch.object(runtime, 'structured', side_effect=model):
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml', ROOT/'workflows/clean_up.yaml'],
                {'statement': 'Task', 'solution': PROOF}, {'start_node':'critic','critic_rounds':1})
            self.assertTrue(state['failed'])
            self.assertIn('Verification incomplete', state['output'])
            self.assertFalse((directory/'formatted-candidate.tex').exists())
            self.assertEqual((directory/'saved-candidate.md').read_text().strip(), PROOF+' repaired')

    def test_critic_rejection_returns_to_the_same_author_session(self):
        revised = PROOF + ' with the objection resolved.'
        feedback = []
        rounds = []
        def author(runtime, prompt, **kwargs):
            feedback.append((yield {'outcome': 'done', 'output': PROOF}))
            yield {'outcome': 'done', 'output': revised}
        def model(prompt, schema, stage, **settings):
            if settings.get('request_label') == 'Critic coordinator adjudication':
                rounds.append(prompt)
                report = review_report(verdict='reject') if len(rounds) == 1 else review_report(revised)
                return report, json.dumps(report)
            return self.model(prompt, schema, stage, **settings)
        with workspace() as directory, patch.object(runtime, 'goal_session', side_effect=author) as session, patch.object(runtime, 'structured', side_effect=model):
            result = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'], {'statement': 'Task'})
            self.assertEqual(session.call_count, 1)
            self.assertEqual(feedback[0]['bugs'], 'A gap remains.')
            self.assertEqual(result['output'], revised)
            self.assertEqual(len(rounds), 2)
            self.assertEqual((directory/'saved-candidate.md').read_text().strip(), revised)

    def test_changed_proof_cannot_hide_behind_fixed_false(self):
        node = runtime.builtin_workflow('author_critic')['nodes']['critic']
        report = review_report(PROOF+' changed',fixed=False)
        with workspace(), patch.object(runtime,'structured',return_value=(report,json.dumps(report))):
            actual,_=runtime._model_call(node,runtime.builtin_workflow('author_critic')['prompts'],
                {'statement':'Task','solution':PROOF}, {'parallel_results':report['checks']},1)
            self.assertTrue(actual['fixed'])

    def test_fresh_auditor_fail_cannot_be_replaced_by_coordinator_pass(self):
        workflow=runtime.builtin_workflow('author_critic')
        node=workflow['nodes']['critic']
        report=review_report()
        audits=copy.deepcopy(report['checks'])
        audits[0]['verdict']='fail'
        with workspace(),patch.object(runtime,'structured',return_value=(report,json.dumps(report))):
            state={'statement':'Task','solution':PROOF}
            with self.assertRaises(runtime.Error):
                runtime._model_call(node,workflow['prompts'],state,{'parallel_results':audits},1)

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

    def test_formatter_rejection_prevents_success_and_compilation(self):
        def model(prompt,schema,stage,**settings):
            value={'latex':LATEX} if 'latex' in schema['properties'] else {'verdict':'reject','bugs':'Dropped a necessary assumption.'}
            return value,json.dumps(value)
        with workspace(),patch.object(runtime,'structured',side_effect=model),patch.object(runtime, '_run_command') as compiler:
            state=runtime.execute_workflows([ROOT/'workflows/clean_up.yaml'], {'statement':'Task','solution':PROOF})
            self.assertTrue(state['failed'])
            self.assertIn('Dropped a necessary assumption',state['output'])
            self.assertEqual(state['solution'],PROOF)
            compiler.assert_not_called()


if __name__=='__main__':
    unittest.main()
