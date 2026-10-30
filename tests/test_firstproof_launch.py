"""The batch launcher validates every pinned job before releasing any holds."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from experiments import launch_firstproof as launch
from experiments import prepare_firstproof as preparation


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runs, self.controls = self.root / 'runs', self.root / 'controls'
        self.codex = self.root / 'codex'
        self.codex.write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 28)
        self.codex.chmod(0o700)
        resolver = patch.object(launch.retrieval, 'codex_executable', return_value=str(self.codex))
        resolver.start()
        self.addCleanup(resolver.stop)
        preparation.prepare(self.runs, self.controls)

    def test_plan_is_inert_and_retains_all_holds(self):
        with patch.object(launch.subprocess, 'Popen', side_effect=AssertionError('spawned')):
            plans = launch.batch_plan(self.runs, self.controls)
        self.assertEqual([p['id'] for p in plans], [f'P{n:02}' for n in range(1, 11)])
        self.assertTrue(all(p['settings']['launchHold'] for p in plans))
        self.assertFalse(any((p['folder'] / 'transcript.jsonl').exists() for p in plans))

    def test_tenth_input_corruption_blocks_entire_batch(self):
        (self.runs / 'firstproof-b3-p10/input.md').write_text('altered')
        with self.assertRaisesRegex(ValueError, 'official statement changed'):
            launch.batch_plan(self.runs, self.controls)
        for settings in self.runs.glob('*/job-settings.json'):
            self.assertTrue(json.loads(settings.read_text())['launchHold'])

    def test_same_input_hash_does_not_allow_changed_author_assignment(self):
        (self.runs / 'firstproof-b3-p01/checked-statement.md').write_text(
            '# Statement sent directly to the proof author\n\nchanged')
        with self.assertRaisesRegex(ValueError, 'official statement changed'):
            launch.batch_plan(self.runs, self.controls)

    def test_started_runs_cannot_be_started_twice(self):
        (self.runs / 'firstproof-b3-p01/transcript.jsonl').touch()
        with self.assertRaisesRegex(ValueError, 'existing model work'):
            launch.batch_plan(self.runs, self.controls)

    def test_changed_prompt_blocks_launch(self):
        (self.runs / 'firstproof-b3-p05/prompts/author.txt').write_text('changed prompt')
        with self.assertRaisesRegex(ValueError, 'author prompt changed'):
            launch.batch_plan(self.runs, self.controls)

    def test_duplicate_launchers_cannot_control_same_batch(self):
        with launch.batch_lock(self.controls):
            with self.assertRaisesRegex(ValueError, 'Another launcher'):
                with launch.batch_lock(self.controls):
                    self.fail('Acquired a duplicate lock')

    def test_failed_start_restores_hold(self):
        plans = launch.batch_plan(self.runs, self.controls)
        with patch('ui.server.App.start_prepared_run', side_effect=RuntimeError('injected start failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected start failure'):
                launch.serve_batch(plans, self.controls, 0)
        for path in self.runs.glob('*/job-settings.json'):
            self.assertTrue(json.loads(path.read_text())['launchHold'])
        self.assertEqual(len(launch.batch_plan(self.runs, self.controls)), 10)

    def subset(self):
        runs, controls = self.root / 'subset-runs', self.root / 'subset-controls'
        preparation.prepare(runs, controls, ['P10', 'P09'])
        return runs, controls

    def test_plan_uses_recorded_subset_and_requires_matching_explicit_selection(self):
        runs, controls = self.subset()
        for selection in (None, ['P09', 'P10'], ['P10', 'P09']):
            plans = launch.batch_plan(runs, controls, selection)
            self.assertEqual([plan['id'] for plan in plans], ['P09', 'P10'])
            self.assertTrue(all(plan['settings']['launchHold'] for plan in plans))
        for selection in ([], ['P09'], ['P01', 'P09', 'P10'], ['P09', 'P09']):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                launch.batch_plan(runs, controls, selection)

    def test_fresh_launch_requires_a_bound_current_navigation_interface(self):
        record_path = self.controls / 'prepared-runs.json'
        original_record = record_path.read_text()
        for version in (None, 'unknown-version'):
            record = json.loads(original_record)
            if version is None:
                record.pop('navigationVersion')
            else:
                record['navigationVersion'] = version
            record_path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'Preparation record'):
                launch.batch_plan(self.runs, self.controls)
        record_path.write_text(original_record)
        policy_path = self.controls / 'P09/retrieval-policy.json'
        policy = json.loads(policy_path.read_text())
        policy['metadata'].pop('navigationVersion')
        launch.write_json(policy_path, policy)
        with self.assertRaisesRegex(ValueError, 'navigation interface changed'):
            launch.batch_plan(self.runs, self.controls)

    def test_changed_preparation_manifest_or_selection_blocks_launch(self):
        runs, controls = self.subset()
        path = controls / 'prepared-runs.json'
        original = path.read_text()
        mutations = (
            lambda record: record['manifest']['runs'][8].update(cutoffExclusiveUtc='2026-10-03T00:00:00Z'),
            lambda record: record.update(selectedProblemIds=['P10', 'P09']),
            lambda record: record.update(selectedProblemIds=['P09']),
            lambda record: record['runs'][0].update(folder=str(self.root / 'other')),
        )
        for mutate in mutations:
            record = json.loads(original)
            mutate(record)
            path.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                launch.batch_plan(runs, controls)
        path.write_text(original)
        self.assertEqual(len(launch.batch_plan(runs, controls)), 2)

    def test_unselected_jobs_in_runs_root_block_launch_before_server_creation(self):
        runs, controls = self.subset()
        plans = launch.batch_plan(runs, controls)
        other = runs / 'firstproof-b3-p01'
        other.mkdir()
        (other / 'transcript.jsonl').write_text('existing work')
        with patch.object(launch, 'Server') as server:
            with self.assertRaisesRegex(ValueError, 'Unselected jobs'):
                launch.serve_batch(plans, controls, 0)
            server.assert_not_called()

    def test_serve_cannot_change_a_verified_subset(self):
        runs, controls = self.subset()
        plans = launch.batch_plan(runs, controls)
        with patch.object(launch, 'Server') as server:
            with self.assertRaisesRegex(ValueError, 'selection does not match'):
                launch.serve_batch(plans[:1], controls, 0)
            server.assert_not_called()

    def test_released_hold_or_existing_proof_blocks_a_fresh_launch(self):
        runs, controls = self.subset()
        settings_path = runs / 'firstproof-b3-p09/job-settings.json'
        original = settings_path.read_text()
        settings = json.loads(original)
        settings['launchHold'] = ''
        settings_path.write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError, 'launch hold'):
            launch.batch_plan(runs, controls)
        settings_path.write_text(original)
        (settings_path.parent / 'proof.md').write_text('earlier work')
        with self.assertRaisesRegex(ValueError, 'existing work'):
            launch.batch_plan(runs, controls)

    def test_settings_and_policy_are_bound_to_the_preparation_record(self):
        runs, controls = self.subset()
        settings_path = runs / 'firstproof-b3-p09/job-settings.json'
        original = settings_path.read_text()
        settings = json.loads(original)
        settings['goalResume'] = True
        launch.write_json(settings_path, settings)
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            launch.batch_plan(runs, controls)
        # The prepared number of independent critics is the default too.
        launch.write_json(settings_path, {**json.loads(original), 'criticCount': 5})
        with self.assertRaisesRegex(ValueError, 'settings changed or run already started'):
            launch.batch_plan(runs, controls)
        settings_path.write_text(original)
        policy_path = controls / 'P09/retrieval-policy.json'
        policy = json.loads(policy_path.read_text())
        policy['metadata']['userApprovedLaunch'] = True
        launch.write_json(policy_path, policy)
        with self.assertRaisesRegex(ValueError, 'retrieval policy changed'):
            launch.batch_plan(runs, controls)

    def test_preflight_and_serve_visit_only_selected_jobs(self):
        runs, controls = self.subset()
        plans = launch.batch_plan(runs, controls)
        def preflight(plan, auth_home):
            self.assertTrue(plan['settings']['launchHold'])
            for role in ('author', 'structured'):
                launch.retrieval.isolated_environment(plan['policy'], role, {})
            return {'id': plan['id'], 'runId': plan['runId']}
        with patch.object(launch, 'preflight_one', side_effect=preflight) as probe, \
             patch('retrieval.isolated_compiler.preflight', return_value={'status': 'pass'}):
            report = launch.preflight_batch(plans, self.root / 'auth', controls)
        self.assertEqual({call.args[0]['id'] for call in probe.call_args_list}, {'P09', 'P10'})
        self.assertEqual(report['selectedProblemIds'], ['P09', 'P10'])
        self.assertEqual(report['modelsStarted'], 0)
        self.assertTrue(all(plan['settings']['launchHold'] for plan in launch.batch_plan(runs, controls)))
        started = []
        def start(app):
            self.assertFalse(app.state['launchHold'])
            started.append(app.state['runId'])
        with patch('ui.server.App.start_prepared_run', autospec=True, side_effect=start), \
             patch.object(launch.Server, 'serve_forever'):
            launch.serve_batch(plans, controls, 0)
        self.assertEqual(started, ['firstproof-b3-p09', 'firstproof-b3-p10'])
        status = json.loads((controls / 'launch-status.json').read_text())
        self.assertEqual(status['selectedProblemIds'], ['P09', 'P10'])
        self.assertEqual([row['runId'] for row in status['runs']], started)

    def test_changed_budget_or_search_cutoff_blocks_launch(self):
        policy_path = self.controls / 'P01/retrieval-policy.json'
        policy = json.loads(policy_path.read_text())
        policy['cutoffExclusiveUtc'] = '2026-10-14T19:00:01Z'
        policy['broker']['args'][2] = policy['cutoffExclusiveUtc']
        launch.write_json(policy_path, policy)
        with self.assertRaisesRegex(ValueError, 'cutoff mismatch'):
            launch.batch_plan(self.runs, self.controls)

    def test_auth_copies_only_secret_file_and_preserves_refreshed_login(self):
        plan = launch.batch_plan(self.runs, self.controls)[0]
        auth_home = self.root / 'source-auth'
        auth_home.mkdir()
        (auth_home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-only'}}))
        (auth_home / 'config.toml').write_text('untrusted = true')
        launch.provision_auth(plan['policy'], auth_home)
        homes = [launch.retrieval.role_home(plan['policy'], role) for role in ('author', 'structured')]
        homes.append(self.controls / 'P01/discovery-auth')
        for home in homes:
            self.assertFalse((home / 'config.toml').exists())
            self.assertEqual({path.name for path in home.iterdir()}, {'auth.json'})
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            target = home / 'auth.json'
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            target.write_text('{"tokens":{"access_token":"refreshed-test-only"}}')
        launch.provision_auth(plan['policy'], auth_home)
        for home in homes:
            self.assertIn('refreshed-test-only', (home / 'auth.json').read_text())

    def test_discovery_credentials_exist_before_broker_preflight(self):
        plan = launch.batch_plan(self.runs, self.controls)[0]
        auth_home = self.root / 'source-auth'
        auth_home.mkdir()
        (auth_home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-only'}}))
        discovery_auth = self.controls / 'P01/discovery-auth/auth.json'
        def run_broker(command, **kwargs):
            self.assertTrue(discovery_auth.is_file())
            self.assertEqual(discovery_auth.stat().st_mode & 0o777, 0o600)
            self.assertEqual(command[-1], '--preflight')
            return SimpleNamespace(stdout=json.dumps({'status': 'ready', 'pdf_supported': True,
                                                     'provider': 'codex_wayback', 'discovery_preflight': 'ready'}))
        with patch.object(launch.subprocess, 'run', side_effect=run_broker), \
             patch.object(launch.retrieval, 'preflight') as probe:
            report = launch.preflight_one(plan, auth_home)
        self.assertEqual(report['rolesVerified'], ['author', 'structured'])
        self.assertEqual(probe.call_count, 2)
        self.assertFalse((plan['folder'] / 'transcript.jsonl').exists())

    def test_broker_preflight_must_verify_the_new_discovery_controller(self):
        plan = launch.batch_plan(self.runs, self.controls)[0]
        result = SimpleNamespace(stdout=json.dumps({'status': 'ready', 'pdf_supported': True,
                                                   'provider': 'codex_wayback'}))
        with patch.object(launch, 'provision_auth'), patch.object(launch.subprocess, 'run', return_value=result), \
             patch.object(launch.retrieval, 'preflight') as probe:
            with self.assertRaisesRegex(ValueError, 'broker preflight failed'):
                launch.preflight_one(plan, self.root / 'unused-auth')
        probe.assert_not_called()

    def test_discovery_auth_home_cannot_redirect_credential_copy(self):
        plan = launch.batch_plan(self.runs, self.controls)[0]
        auth_home = self.root / 'source-auth'
        auth_home.mkdir()
        (auth_home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-only'}}))
        destination = self.root / 'unrelated'
        destination.mkdir()
        (self.controls / 'P01/discovery-auth').symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            launch.provision_auth(plan['policy'], auth_home)
        self.assertFalse((destination / 'auth.json').exists())
        self.assertFalse((self.controls / 'P01/codex-home').exists())

    def test_broker_policy_mutation_blocks_all_credential_provisioning(self):
        plan = launch.batch_plan(self.runs, self.controls)[0]
        auth_home = self.root / 'source-auth'
        auth_home.mkdir()
        (auth_home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-only'}}))
        broker_path = Path(plan['policy']['broker']['policyFile'])
        broker = json.loads(broker_path.read_text())
        broker['discovery']['auth_home'] = str(self.root / 'unrelated')
        launch.write_json(broker_path, broker)
        with self.assertRaisesRegex(ValueError, 'policy changed before credential'):
            launch.provision_auth(plan['policy'], auth_home)
        self.assertFalse((self.controls / 'P01/codex-home').exists())
        self.assertFalse((self.root / 'unrelated').exists())

    def test_fresh_launch_rejects_old_discovery_provider_records(self):
        record_path = self.controls / 'prepared-runs.json'
        record = json.loads(record_path.read_text())
        record['discoveryProvider'] = 'scholarly_wayback'
        launch.write_json(record_path, record)
        with self.assertRaisesRegex(ValueError, 'Preparation record'):
            launch.batch_plan(self.runs, self.controls)


if __name__ == '__main__':
    unittest.main()
