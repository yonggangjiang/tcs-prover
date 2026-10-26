"""Preparation is inert, pinned, restart-safe and cannot accidentally launch."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import prepare_firstproof
from ui import server

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.controls = self.root / 'controller'
        self.codex = self.root / 'codex'
        self.codex.write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 28)
        self.codex.chmod(0o700)
        resolver = patch.object(prepare_firstproof.retrieval, 'codex_executable', return_value=str(self.codex.resolve()))
        resolver.start()
        self.addCleanup(resolver.stop)

    def prepare(self, problems=None):
        with patch.object(server.subprocess, 'Popen', side_effect=AssertionError('Preparation launched a process')):
            return prepare_firstproof.prepare(self.runs, self.controls, problems)

    def test_all_ten_inputs_defaults_and_held_restore(self):
        result = self.prepare()
        self.assertEqual(len(result['runs']), 10)
        jobs = server.restore_saved_jobs(self.runs)
        self.assertEqual(len(jobs), 10)
        default = server.empty_state()
        for row in result['runs']:
            app = jobs[row['runId']]
            self.assertEqual(app.state['phase'], 'prepared')
            self.assertTrue(app.state['launchHold'])
            self.assertEqual(app.state['retrievalPolicyPath'], row['policy'])
            self.assertTrue(app.state['skipStatementReview'])
            self.assertFalse(app.state['fileManagement'])
            self.assertEqual(app.state['speedMode'], 'fast')
            self.assertEqual(app.state['thinkingHours'], 0)
            for key in ('authorModel', 'criticModel', 'writerModel', 'authorEffort', 'criticEffort', 'writerEffort', 'criticRounds', 'reasoningSummary', 'researchAudits'):
                self.assertEqual(app.state[key], default[key])
            expected = (prepare_firstproof.EXPERIMENT / 'inputs' / (row['id'] + '.md')).read_bytes()
            self.assertEqual((app.run_dir / 'input.md').read_bytes(), expected)
            self.assertEqual(server.saved_statement(app.run_dir), expected.decode('utf-8').strip())
            self.assertFalse((app.run_dir / 'transcript.jsonl').exists())
            with patch.object(server.runtime, 'verify_model_credentials') as credentials, patch.object(app, '_launch_solver_locked') as launch:
                with self.assertRaisesRegex(ValueError, 'held'):
                    app.start_prepared_run()
                credentials.assert_not_called()
                launch.assert_not_called()
            self.assertEqual(app.state['startedAt'], '')
            self.assertIsNone(server.Server._stopped_continuation_plan(app))

    def test_repeat_preparation_never_overwrites_existing_work(self):
        result = self.prepare()
        sentinel = Path(result['runs'][0]['folder']) / 'keep.txt'
        sentinel.write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.prepare()
        self.assertEqual(sentinel.read_text(), 'preserve')

    def test_explicit_subset_is_held_in_manifest_order_with_full_manifest(self):
        result = self.prepare(['P10', 'P09'])
        self.assertEqual(result['selectedProblemIds'], ['P09', 'P10'])
        self.assertEqual([row['id'] for row in result['runs']], ['P09', 'P10'])
        self.assertEqual([row['id'] for row in result['manifest']['runs']],
                         [f'P{n:02}' for n in range(1, 11)])
        self.assertEqual(sorted(path.name for path in self.runs.iterdir()),
                         ['firstproof-b3-p09', 'firstproof-b3-p10'])
        self.assertFalse((self.controls / 'P01').exists())
        jobs = server.restore_saved_jobs(self.runs)
        self.assertEqual(len(jobs), 2)
        for row in result['runs']:
            self.assertTrue(jobs[row['runId']].state['launchHold'])
            self.assertEqual(Path(row['folder'], 'input.md').read_bytes(),
                             (prepare_firstproof.EXPERIMENT / 'inputs' / (row['id'] + '.md')).read_bytes())
        self.assertEqual(result, json.loads((self.controls / 'prepared-runs.json').read_text()))

    def test_invalid_selections_make_no_directories(self):
        for selection in ([], ['P09', 'P09'], ['P11'], ['p09'], 'P09'):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.prepare(selection)
            self.assertFalse(self.runs.exists())
            self.assertFalse(self.controls.exists())

    def test_different_subset_cannot_overwrite_preparation_record(self):
        self.prepare(['P09'])
        before = (self.controls / 'prepared-runs.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            prepare_firstproof.prepare(self.root / 'other-runs', self.controls, ['P10'])
        self.assertEqual((self.controls / 'prepared-runs.json').read_bytes(), before)
        self.assertFalse((self.controls / 'P10').exists())
        self.assertFalse((self.root / 'other-runs').exists())

    def test_existing_runs_cannot_be_reused_for_another_subset(self):
        self.prepare(['P09'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            prepare_firstproof.prepare(self.runs, self.root / 'other-controls', ['P10'])
        self.assertFalse((self.root / 'other-controls').exists())
        self.assertFalse((self.runs / 'firstproof-b3-p10').exists())

    def test_controller_cannot_be_inside_runs(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            prepare_firstproof.prepare(self.runs, self.runs / 'controller')
        self.assertFalse(self.runs.exists())

    def test_controller_policy_hash_and_cutoff_bound_to_every_broker(self):
        result = self.prepare()
        self.assertEqual(result['navigationVersion'], 'full-document-v1')
        self.assertEqual(result['discoveryProvider'], 'codex_wayback')
        for row in result['runs']:
            policy = json.loads(Path(row['policy']).read_text())
            self.assertEqual(policy['metadata']['navigationVersion'], result['navigationVersion'])
            self.assertEqual(policy['metadata']['discoveryProvider'], result['discoveryProvider'])
            args = policy['broker']['args']
            self.assertEqual(args[args.index('--cutoff') + 1], row['cutoffExclusiveUtc'])
            self.assertEqual(args[args.index('--policy-sha256') + 1], prepare_firstproof.sha(Path(policy['broker']['policyFile'])))
            self.assertFalse(policy['metadata']['userApprovedLaunch'])
            self.assertFalse(Path(row['policy']).is_relative_to(self.runs))
            broker = json.loads(Path(policy['broker']['policyFile']).read_text())
            self.assertEqual(broker['provider'], 'codex_wayback')
            discovery = broker['discovery']
            self.assertEqual(set(discovery), {'codex_path', 'codex_sha256', 'auth_home',
                                             'state_root', 'model', 'timeout_seconds'})
            self.assertTrue(Path(discovery['codex_path']).is_absolute())
            self.assertEqual(discovery['codex_sha256'], prepare_firstproof.sha(Path(discovery['codex_path'])))
            self.assertEqual(discovery['auth_home'], str(self.controls.resolve() / row['id'] / 'discovery-auth'))
            self.assertEqual(discovery['state_root'], str(self.controls.resolve() / row['id'] / 'discovery-state'))
            self.assertEqual(discovery['model'], server.empty_state()['authorModel'])
            self.assertEqual(discovery['timeout_seconds'], 120)
            self.assertFalse(Path(discovery['auth_home']).exists())
            self.assertFalse(Path(discovery['state_root']).exists())
            self.assertEqual(broker['solver_workspace'], row['folder'])
            self.assertEqual(broker['rate_limit_directory'], str(self.controls.resolve() / 'retrieval-state'))
            self.assertLess(broker['operation_timeout_seconds'], 600)

    def test_missing_native_discovery_binary_fails_before_preparation_writes(self):
        with patch.object(prepare_firstproof.retrieval, 'codex_executable',
                          side_effect=prepare_firstproof.retrieval.RetrievalPolicyError('native executable unavailable')):
            with self.assertRaisesRegex(prepare_firstproof.retrieval.RetrievalPolicyError, 'native executable unavailable'):
                self.prepare(['P09', 'P10'])
        self.assertFalse(self.runs.exists())
        self.assertFalse(self.controls.exists())

    def test_invalid_persisted_controls_fail_closed(self):
        result = self.prepare()
        folder = Path(result['runs'][0]['folder'])
        settings_path = folder / 'job-settings.json'
        settings = json.loads(settings_path.read_text())
        settings['launchHold'] = ''
        settings['retrievalPolicyPath'] = 123
        settings_path.write_text(json.dumps(settings))
        app = server.restore_saved_app(server.App(folder / 'transcript.jsonl', self.runs))
        self.assertIn('Invalid', app.state['launchHold'])

    def test_historical_clone_cannot_drop_policy(self):
        result = self.prepare()
        target = server.App(runs=self.runs)
        target.run_dir = self.runs / 'new-run'
        target.run_dir.mkdir()
        with self.assertRaisesRegex(ValueError, 'unrestricted continuation'):
            target._prepare_continuation(result['runs'][0]['folder'])

    def test_reused_app_entrypoints_cannot_erase_hold_or_start_any_role(self):
        result = self.prepare()
        app = server.restore_saved_jobs(self.runs)[result['runs'][0]['runId']]
        attempts = [
            ('start_review', ('changed statement',)),
            ('start_direct_statement', ('changed statement',)),
            ('start_latex_only', ('proof text',)),
            ('start_final_resume', ('statement', 'candidate')),
            ('start_critic_resume', ('statement', 'candidate')),
            ('reset', ()), ('resume', ()), ('approve', ()),
            ('start_research_audit_now', ()),
        ]
        before = dict(app.state)
        with patch.object(app, '_workflow_options', side_effect=AssertionError('Reached credential/settings preparation')), \
             patch.object(app, '_spawn_worker', side_effect=AssertionError('Started worker')):
            for name, args in attempts:
                with self.subTest(method=name), self.assertRaisesRegex(ValueError, 'held'):
                    getattr(app, name)(*args)
                self.assertEqual(app.state, before)

    def test_released_historical_app_still_cannot_reset_policy(self):
        result = self.prepare()
        app = server.restore_saved_jobs(self.runs)[result['runs'][0]['runId']]
        app.state['launchHold'] = ''
        with self.assertRaisesRegex(ValueError, 'policy are fixed'):
            app.reset()

if __name__ == '__main__':
    unittest.main()
