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
from persistent_research import ResearchMemory
from goal_runtime import _seed_files
from research_journal import ResearchJournal

ROOT = Path(runtime.__file__).parent
PROOF = 'Every allowed case follows by the explicit argument supplied here.'
LATEX = r'\documentclass{article}\begin{document}An explicit argument.\end{document}'


def review_report(solution=PROOF, verdict='pass', fixed=False, issues=None):
    return {'checks': [{'focus': focus, 'verdict': 'pass', 'report': 'Checked.'}
                       for focus in runtime.builtin_workflow('author_critic')['nodes']['critic']['parallel']['items']],
            'verdict': verdict, 'fixed': fixed, 'solution': solution,
            'bugs': 'A gap remains.' if verdict == 'reject' else '',
            'memory_update': {'approach_family': 'family 0', 'approach_result': 'Reviewed.',
                              'blocked_routes': [], 'unresolved_obligations': issues or []},
            'resolved_obligations': []}


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
            _seed_files(runtime, kwargs['directory'], kwargs['files'], kwargs['prompt_file'])
            if len(self.goal_calls) == 1:
                self.assertFalse((kwargs['directory'] / 'research.sqlite3').exists())
            yield {'outcome': 'proof', 'solution': PROOF}
        patcher = patch('goal_runtime.goal_session', side_effect=goal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_goal_feeds_unchanged_critic_and_final_document(self):
        with workspace() as directory, patch.object(runtime, 'structured', side_effect=self.model), patch('latex_verification.verify_latex', return_value={'status': 'pass', 'diagnostic': 'Compiled', 'engine': 'mock'}):
            state = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml', ROOT/'workflows/clean_up.yaml'], {'statement': 'Exact test task'})
            self.assertFalse(state['failed'])
            self.assertEqual(state['output'], LATEX)
            self.assertEqual(len([x for x in self.calls if x.startswith('Independent critic audit')]), 3)
            self.assertTrue((directory/'formatted-candidate.tex').is_file())
            with ResearchJournal(directory, 'Exact test task') as journal:
                kinds = {e['kind'] for e in journal.events()}
                self.assertTrue({'critic','latex_compilation'} <= kinds)
                self.assertFalse({'portfolio','novelty','assignment','attempt','research_review'} & kinds)
                self.assertEqual(len(self.goal_calls), 1)
                self.assertEqual(journal.get_state('candidate_status'), 'approved')
                self.assertEqual(journal.get_state('workflow_checkpoint')['node'], 'end')

    def test_real_transport_wrapper_archives_every_prompt_and_full_reply(self):
        def transport(prompt,schema,stage,*args,**kwargs):
            label=kwargs.get('activity_label') or ''
            if label.startswith('Independent audit '):
                label=label.replace('Independent audit ','Independent critic audit ')
            if 'checks' in schema['properties']:
                label='Critic coordinator adjudication'
            return self.model(prompt,schema,stage,request_label=label)[1]
        with workspace() as directory,patch.object(runtime,'run_structured_attempt',side_effect=transport),patch('latex_verification.verify_latex',return_value={'status':'pass','diagnostic':'Compiled','engine':'mock'}):
            runtime.execute_workflows([ROOT/'workflows/author_critic.yaml',ROOT/'workflows/clean_up.yaml'],{'statement':'Exact task'})
            with ResearchJournal(directory,'Exact task') as journal:
                requests=list(journal.events('model_request'))
                replies=list(journal.events('model_response'))
                self.assertEqual(len(requests),6)
                self.assertEqual(len(replies),6)
                self.assertEqual({event['id'] for event in requests},
                                 {event['payload']['request_id'] for event in replies})
                self.assertTrue(all(event['payload']['prompt'] and event['payload']['schema'] for event in requests))

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
            with ResearchJournal(directory,'Task') as journal:
                self.assertEqual(journal.get_state('candidate_status'),'awaiting_critic')
                self.assertEqual(journal.get_state('candidate'), PROOF+' repaired')

    def test_new_author_candidate_gets_its_own_critic_record(self):
        revised = PROOF + ' with the objection resolved.'
        feedback = []
        rounds = []
        def author(runtime, prompt, **kwargs):
            _seed_files(runtime, kwargs['directory'], kwargs['files'], kwargs['prompt_file'])
            feedback.append((yield {'outcome': 'proof', 'solution': PROOF}))
            yield {'outcome': 'proof', 'solution': revised}
        def model(prompt, schema, stage, **settings):
            if settings.get('request_label') == 'Critic coordinator adjudication':
                rounds.append(prompt)
                report = review_report(verdict='reject') if len(rounds) == 1 else review_report(revised)
                return report, json.dumps(report)
            return self.model(prompt, schema, stage, **settings)
        with workspace() as directory, patch('goal_runtime.goal_session', side_effect=author) as session, patch.object(runtime, 'structured', side_effect=model):
            result = runtime.execute_workflows([ROOT/'workflows/author_critic.yaml'], {'statement': 'Task'})
            self.assertEqual(session.call_count, 1)
            self.assertEqual(feedback[0]['bugs'], 'A gap remains.')
            self.assertEqual(result['output'], revised)
            with ResearchJournal(directory, 'Task') as journal:
                reports = list(journal.events('critic'))
                self.assertEqual(len(reports), 2)
                self.assertNotEqual(reports[0]['payload']['attempt_id'], reports[1]['payload']['attempt_id'])
                self.assertEqual(journal.get_state('candidate'), revised)

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

    def test_raw_response_survives_crash_before_interpreting_it(self):
        with workspace() as directory,ResearchJournal(directory,'Task') as journal:
            journal.commit('model_response',{'raw':'{"value":"done"}'}, {'raw:checkpoint':'{"value":"done"}'})
            schema=runtime._response_fields({'value':'string'})
            with patch.object(runtime,'run_structured_attempt') as transport:
                result,_=runtime.structured('Prompt',schema,'solve',journal=journal,cache_key='checkpoint')
                self.assertEqual(result['value'],'done')
                transport.assert_not_called()

    def test_formatter_rejection_prevents_success_and_compilation(self):
        def model(prompt,schema,stage,**settings):
            value={'latex':LATEX} if 'latex' in schema['properties'] else {'verdict':'reject','bugs':'Dropped a necessary assumption.'}
            return value,json.dumps(value)
        with workspace(),patch.object(runtime,'structured',side_effect=model),patch('latex_verification.verify_latex') as compiler:
            state=runtime.execute_workflows([ROOT/'workflows/clean_up.yaml'], {'statement':'Task','solution':PROOF})
            self.assertTrue(state['failed'])
            self.assertIn('Dropped a necessary assumption',state['output'])
            self.assertEqual(state['solution'],PROOF)
            compiler.assert_not_called()


class CriticMemoryTests(unittest.TestCase):
    def test_obligation_resolution_waits_for_unchanged_pass(self):
        with workspace() as directory,ResearchJournal(directory,'Task') as journal:
            memory=ResearchMemory(journal)
            memory.record_candidate(PROOF,'test')
            memory.record_critic_report(review_report(verdict='reject',issues=['Prove step 2']),1)
            issue=journal.get_state('open_issues')[0]['id']
            fixed=review_report(PROOF+' change',fixed=True)
            fixed['resolved_obligations']=[{'id':issue,'evidence':'New argument'}]
            memory.record_critic_report(fixed,2)
            self.assertEqual(len(journal.get_state('open_issues')),1)
            fixed['fixed']=False
            memory.record_critic_report(fixed,3)
            self.assertEqual(journal.get_state('open_issues'),[])

    def test_unchanged_review_can_close_some_issues_without_approving_others(self):
        with workspace() as directory,ResearchJournal(directory,'Task') as journal:
            memory=ResearchMemory(journal)
            identity=memory.record_candidate(PROOF,'test')
            memory.record_critic_report(review_report(verdict='reject',issues=['First issue','Second issue']),1,
                                        attempt_id=identity,result_attempt_id=identity)
            first=journal.get_state('open_issues')[0]['id']
            report=review_report(verdict='reject')
            report['resolved_obligations']=[{'id':first,'evidence':'This step already follows from the stated premise.'}]
            memory.record_critic_report(report,2,attempt_id=identity,result_attempt_id=identity)
            self.assertEqual([issue['description'] for issue in journal.get_state('open_issues')],['Second issue'])


if __name__=='__main__':
    unittest.main()
