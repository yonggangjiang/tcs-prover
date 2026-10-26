"""Historical UI policy creation and public summaries, without model calls."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from retrieval import runtime as retrieval_runtime
from ui import retrieval, server

CUTOFF = '2026-10-02T14:13:12Z'


class RetrievalUITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.runs = self.root / 'runs'
        self.codex = self.root / 'codex'
        self.codex.write_bytes(b'\xcf\xfa\xed\xfe' + b'unit-test-native-binary')
        self.codex.chmod(0o700)
        self.manager = object.__new__(server.Server)
        self.manager.runs = self.runs
        self.manager.jobs = {}
        self.manager.jobs_lock = threading.RLock()
        self.manager.fixed_app = False
        self.order = []
        real_prepare = retrieval.prepare_policy
        def prepare(workspace, cutoff):
            return real_prepare(workspace, cutoff, controller_root=self.root / 'controller')
        changes = [patch.object(retrieval_runtime, 'codex_executable', return_value=str(self.codex)),
                   patch.object(server.runtime, 'verify_model_credentials'),
                   patch.object(server, 'grant_windows_access'),
                   patch.object(retrieval, 'capabilities', return_value={'historicalAvailable': True}),
                   patch.object(retrieval, 'prepare_policy', side_effect=prepare),
                   patch.object(retrieval, 'preflight_policy', side_effect=lambda p: self.order.append(('preflight', p))),
                   patch.object(retrieval, 'preflight_compiler', side_effect=lambda p: self.order.append(('compiler', p))),
                   patch.object(server.App, '_spawn_worker'),
                   patch.object(server.subprocess, 'Popen', side_effect=self.launch)]
        for change in changes:
            change.start()
            self.addCleanup(change.stop)
        retrieval._CACHE.clear()

    def launch(self, args, **kwargs):
        self.order.append(('launch', kwargs))
        return Mock(stdin=io.StringIO(), poll=Mock(return_value=None))

    def body(self, **extra):
        return {'statement': 'Prove a harmless identity.', 'retrievalMode': 'historical',
                'retrievalCutoffUtc': CUTOFF, **extra}

    def test_live_default_does_not_create_policy_or_preflight(self):
        app = self.manager.start_direct_job({'statement': 'Prove one equals one.'})
        self.assertEqual(app.state['retrievalMode'], 'live')
        self.assertFalse(app.state['retrievalPolicyPath'])
        self.assertEqual([x[0] for x in self.order], ['launch'])
        self.assertNotIn(retrieval_runtime.POLICY_ENV, self.order[0][1]['env'])
        self.assertEqual(self.order[0][1]['env'][retrieval_runtime.MODE_ENV], 'live')
        self.assertEqual(app.snapshot()['retrieval']['mode'], 'live')

    def test_direct_historical_policy_precedes_preflight_and_launch(self):
        app = self.manager.start_direct_job(self.body())
        policy_path = app.state['retrievalPolicyPath']
        policy = retrieval_runtime.load_policy(policy_path)
        self.assertEqual([x[0] for x in self.order], ['preflight', 'compiler', 'launch'])
        self.assertEqual(policy['cutoffExclusiveUtc'], CUTOFF)
        self.assertEqual(Path(policy['isolation']['workspace']), app.run_dir)
        self.assertFalse(Path(policy_path).is_relative_to(app.run_dir))
        self.assertEqual(self.order[-1][1]['env'][retrieval_runtime.POLICY_ENV], policy_path)
        saved = json.loads((app.run_dir / 'job-settings.json').read_text())
        self.assertEqual(saved['retrievalPolicyPath'], policy_path)
        self.assertEqual(saved['retrievalMode'], 'historical')
        self.assertEqual(retrieval_runtime.configuration(policy, 'structured')['mcp_servers'], {})
        self.assertEqual(set(retrieval_runtime.configuration(policy, 'author')['mcp_servers']), {'historical_search'})

    def test_minus_one_normalizes_and_never_creates_historical_policy(self):
        for mode, cutoff in [('disabled', ''), ('disabled', '-1'), ('historical', '-1')]:
            with self.subTest(mode=mode, cutoff=cutoff):
                app = self.manager.start_direct_job(self.body(retrievalMode=mode, retrievalCutoffUtc=cutoff))
                self.assertEqual((app.state['retrievalMode'], app.state['retrievalCutoffUtc']), ('disabled', '-1'))
                self.assertFalse(app.state['retrievalPolicyPath'])
                self.assertEqual(self.order[-1][1]['env'][retrieval_runtime.MODE_ENV], 'disabled')
                self.assertEqual(app.snapshot()['retrieval']['mode'], 'disabled')
        self.assertEqual([row[0] for row in self.order], ['launch'] * 3)

    def test_review_latex_and_folder_launches_keep_selected_mode(self):
        for mode, cutoff in [('live', ''), ('disabled', '-1')]:
            for review_only in (False, True):
                app = self.manager.start_job(self.body(retrievalMode=mode, retrievalCutoffUtc=cutoff,
                                                       statementReviewOnly=review_only))
                self.assertEqual(app.state['retrievalMode'], mode)
                self.assertEqual(app._retrieval_child_environment()[retrieval_runtime.MODE_ENV], mode)
                saved = json.loads((app.run_dir / 'job-settings.json').read_text())
                self.assertEqual(saved['retrievalMode'], mode)
            app = self.manager.start_latex_job({'content': 'A theorem and its proof.',
                'retrievalMode': mode, 'retrievalCutoffUtc': cutoff})
            self.assertEqual(self.order[-1][1]['env'][retrieval_runtime.MODE_ENV], mode)
            apps = self.manager.start_direct_batch({'retrievalMode': mode, 'retrievalCutoffUtc': cutoff,
                'statements': [{'name': 'a.md', 'statement': 'A.'}, {'name': 'b.md', 'statement': 'B.'}]})
            self.assertTrue(all(app.state['retrievalMode'] == mode for app in apps))
        self.assertFalse(any(row[0] in ('preflight', 'compiler') for row in self.order))

    def test_minus_one_folder_does_not_take_historical_preflight_path(self):
        apps = self.manager.start_direct_batch({'retrievalMode': 'historical', 'retrievalCutoffUtc': '-1',
            'statements': [{'name': 'a.md', 'statement': 'A.'}]})
        self.assertEqual(apps[0].state['retrievalMode'], 'disabled')
        self.assertEqual([row[0] for row in self.order], ['launch'])

    def test_launch_overrides_inherited_other_job_policy_and_mode(self):
        with patch.dict(os.environ, {retrieval_runtime.POLICY_ENV: '/other/job/policy.json',
                                     retrieval_runtime.MODE_ENV: 'historical'}):
            app = self.manager.start_direct_job({'statement': 'A.', 'retrievalMode': 'live'})
        self.assertNotIn(retrieval_runtime.POLICY_ENV, self.order[-1][1]['env'])
        self.assertEqual(self.order[-1][1]['env'][retrieval_runtime.MODE_ENV], 'live')

    def test_legacy_main_job_stays_live_and_continuations_preserve_mode(self):
        legacy = self.runs / 'legacy'
        legacy.mkdir(parents=True)
        (legacy / 'job-settings.json').write_text('{}')
        (legacy / 'input.md').write_text('A.')
        restored = server.restore_saved_app(server.App(legacy / 'transcript.jsonl', self.runs))
        self.assertEqual(restored.state['retrievalMode'], 'live')
        self.assertEqual(retrieval.continuation_options(legacy),
                         {'retrievalMode': 'live', 'retrievalCutoffUtc': ''})
        for mode, cutoff in [('live', ''), ('disabled', '-1')]:
            (legacy / 'job-settings.json').write_text(json.dumps({'retrievalMode': mode, 'retrievalCutoffUtc': cutoff}))
            self.assertEqual(retrieval.continuation_options(legacy)['retrievalMode'], mode)

    def test_historical_continuation_cannot_drop_its_policy(self):
        app = self.manager.start_direct_job(self.body())
        with self.assertRaisesRegex(ValueError, 'original workspace'):
            retrieval.continuation_options(app.run_dir)

    def test_statement_revision_cannot_change_search_mode(self):
        app = self.manager.start_job({'statement': 'A.', 'retrievalMode': 'disabled', 'retrievalCutoffUtc': '-1'})
        app.state['phase'] = 'reviewed'
        with self.assertRaisesRegex(ValueError, 'keeps this run'):
            app.start_review('A.', retrieval_mode='live')

    def test_statement_worker_and_approval_inherit_selected_search_mode(self):
        for mode, cutoff in [('live', ''), ('disabled', '-1')]:
            app = self.manager.start_job({'statement': 'A.', 'retrievalMode': mode, 'retrievalCutoffUtc': cutoff})
            process = Mock(stdin=io.StringIO(), stdout=iter([json.dumps({
                'kind': 'review_result', 'review': {'statement': 'Checked A.', 'notes': ''}}) + '\n']),
                poll=Mock(return_value=0), wait=Mock(return_value=0))
            with patch.object(server.subprocess, 'Popen', return_value=process) as spawn:
                app._review('A.', token=app.active_token)
            self.assertEqual(spawn.call_args.kwargs['cwd'], app.run_dir)
            self.assertEqual(spawn.call_args.kwargs['env'][retrieval_runtime.MODE_ENV], mode)
            self.assertEqual(app.state['phase'], 'reviewed')
            app.approve()
            self.assertEqual(self.order[-1][1]['env'][retrieval_runtime.MODE_ENV], mode)

    def test_new_continuation_workers_keep_explicit_disabled_mode(self):
        source = self.manager.start_direct_job({'statement': 'A.', 'retrievalMode': 'disabled'})
        starters = [
            lambda app: app.start_direct_statement('A.', continuation_source=source.run_dir),
            lambda app: app.start_review('A.', continuation_source=source.run_dir),
            lambda app: app.start_latex_only('A theorem and proof.', continuation_source=source.run_dir),
            lambda app: app.start_final_resume('A.', 'Proof.', continuation_source=source.run_dir),
            lambda app: app.start_critic_resume('A.', 'Proof.', source_run=source.run_dir),
        ]
        for start in starters:
            app = server.App(runs=self.runs)
            start(app)
            self.assertEqual(app.state['retrievalMode'], 'disabled')
            self.assertEqual(app._retrieval_child_environment()[retrieval_runtime.MODE_ENV], 'disabled')
            saved = json.loads((app.run_dir / 'job-settings.json').read_text())
            self.assertEqual(saved['retrievalMode'], 'disabled')

    def test_batch_all_preflights_and_compiler_before_first_model(self):
        body = self.body(statements=[{'name': 'a.md', 'statement': 'First.'}, {'name': 'b.md', 'statement': 'Second.'}])
        apps = self.manager.start_direct_batch(body)
        self.assertEqual([x[0] for x in self.order], ['preflight', 'preflight', 'compiler', 'launch', 'launch'])
        self.assertEqual(len({app.state['retrievalPolicyPath'] for app in apps}), 2)
        self.assertTrue(all(app.state['phase'] == 'running' for app in apps))

    def test_batch_failed_preflight_starts_no_model_and_keeps_holds(self):
        with patch.object(retrieval, 'preflight_policy', side_effect=ValueError('sandbox unavailable')):
            with self.assertRaisesRegex(ValueError, 'sandbox unavailable'):
                self.manager.start_direct_batch(self.body(statements=[{'statement': 'First.'}, {'statement': 'Second.'}]))
        self.assertFalse(any(x[0] == 'launch' for x in self.order))
        self.assertTrue(all(app.state['launchHold'] for app in self.manager.jobs.values()))

    def test_concurrent_continue_cannot_bypass_pending_batch_preflight(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocked(_path):
            entered.set()
            if not release.wait(5):
                raise ValueError('test preflight wait expired')
            raise ValueError('sandbox unavailable')
        def start():
            try:
                self.manager.start_direct_batch(self.body(statements=[{'statement': 'First.'}, {'statement': 'Second.'}]))
            except ValueError as exc:
                errors.append(str(exc))
        with patch.object(retrieval, 'preflight_policy', side_effect=blocked):
            worker = threading.Thread(target=start)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(len(self.manager.jobs), 2)
                for app in list(self.manager.jobs.values()):
                    self.assertTrue(app.state['launchHold'])
                    with self.assertRaises(ValueError):
                        self.manager.continue_stopped_job(app.state['runId'])
                    with self.assertRaisesRegex(ValueError, 'held'):
                        app.start_prepared_run()
                self.assertFalse(any(x[0] == 'launch' for x in self.order))
            finally:
                release.set()
                worker.join(5)
        self.assertTrue(errors)
        self.assertFalse(worker.is_alive())
        self.assertFalse(any(x[0] == 'launch' for x in self.order))

    def test_invalid_cutoffs_modes_and_models_cannot_start_any_model(self):
        bad = [{'retrievalMode': 'unknown'}, {'retrievalCutoffUtc': '2026-10-02'},
               {'retrievalCutoffUtc': '2026-10-02T14:13:12+01:00'},
               {'retrievalCutoffUtc': '2999-01-01T00:00:00Z'},
               {'authorModel': 'claude-opus-5-5'}]
        for options in bad:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.manager.start_direct_job(self.body(**options))
        self.assertFalse(self.order)

    def test_non_author_endpoints_reject_historical_setting(self):
        for method in (self.manager.start_job, self.manager.start_latex_job):
            with self.assertRaisesRegex(ValueError, 'direct proof|Skip statement review'):
                method(self.body(content='A theorem and its proof.'))
        self.assertFalse(self.order)

    def test_historical_existing_inputs_cannot_be_replaced(self):
        app = self.manager.start_direct_job(self.body())
        app.state['phase'] = 'done'
        with self.assertRaisesRegex(ValueError, 'fixed'):
            app.start_direct_statement('Replace the statement.', retrieval_mode='disabled')

    def test_public_summary_aggregates_no_queries_or_sources(self):
        app = self.manager.start_direct_job(self.body())
        policy = retrieval_runtime.load_policy(app.state['retrievalPolicyPath'])
        events = [
            {'event': 'mcp_accepted', 'operation_id': 'a'},
            {'event': 'search_requested', 'query': 'SECRET QUERY'},
            {'event': 'capture_verified', 'url': 'https://secret.example/path'},
            {'event': 'mcp_response_ready', 'operation_id': 'a', 'is_error': False},
            {'event': 'mcp_accepted', 'operation_id': 'b'},
            {'event': 'open_requested'},
            {'event': 'mcp_deadline', 'operation_id': 'b'},
            {'event': 'mcp_accepted', 'operation_id': 'c'},
            {'event': 'mcp_busy'},
        ]
        Path(policy['broker']['auditLog']).write_text(''.join(json.dumps({**event, 'cutoff': CUTOFF,
            'time': '2026-10-16T10:00:00Z'}) + '\n' for event in events) + '{partial')
        public = retrieval.public_retrieval_info(policy['_path'], run_dir=app.run_dir)
        self.assertTrue(public['available'])
        self.assertTrue(public['partialTrailingLine'])
        self.assertEqual(public['counts'], {'searches': 1, 'opens': 1, 'finds': 0, 'successes': 1, 'errors': 2,
            'pending': 1, 'verifiedCandidates': 1, 'busy': 1, 'cancelled': 0, 'deadlines': 1})
        text = json.dumps(public)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('secret.example', text)
        self.assertNotIn(str(self.root), text)

    def test_wrong_workspace_or_malformed_audit_never_reports_available(self):
        app = self.manager.start_direct_job(self.body())
        policy = retrieval_runtime.load_policy(app.state['retrievalPolicyPath'])
        self.assertFalse(retrieval.public_retrieval_info(policy['_path'], run_dir=self.root)['available'])
        Path(policy['broker']['auditLog']).write_text('{bad}\n')
        self.assertFalse(retrieval.public_retrieval_info(policy['_path'], run_dir=app.run_dir)['available'])

    def public_events(self, events):
        app = self.manager.start_direct_job(self.body())
        policy = retrieval_runtime.load_policy(app.state['retrievalPolicyPath'])
        Path(policy['broker']['auditLog']).write_text(''.join(json.dumps({
            'cutoff': CUTOFF, 'time': f'2026-10-16T10:00:{index:02d}Z', **event}) + '\n'
            for index, event in enumerate(events)))
        return retrieval.public_retrieval_info(policy['_path'], run_dir=app.run_dir)

    def test_failed_requests_count_terminal_operations_not_http_retries(self):
        public = self.public_events([
            {'event': 'mcp_accepted', 'operation_id': 'a'},
            {'event': 'open_requested', 'operation_id': 'a', 'url': 'https://secret.example'},
            *[{'event': 'http_failure', 'operation_id': 'a', 'code': 'transport_failure',
               'message': 'SECRET provider diagnostic'} for _ in range(3)],
            {'event': 'open_rejected', 'operation_id': 'a', 'failure_code': 'transport_failure'},
            {'event': 'mcp_response_ready', 'operation_id': 'a', 'is_error': True},
            {'event': 'mcp_response_ready', 'operation_id': 'a', 'is_error': True},
            {'event': 'mcp_accepted', 'operation_id': 'b'},
            {'event': 'search_requested', 'operation_id': 'b', 'query': 'SECRET query'},
            {'event': 'http_failure', 'operation_id': 'b', 'code': 'response_too_large'},
            {'event': 'capture_rejected', 'operation_id': 'b', 'failure_code': 'no_capture'},
            {'event': 'search_completed', 'operation_id': 'b', 'returned': 1},
            {'event': 'mcp_response_ready', 'operation_id': 'b', 'is_error': False},
        ])
        self.assertEqual(public['requestFailures'], [{'code': 'transport_failure',
            'label': 'Archive connection failed', 'count': 1}])
        self.assertEqual(public['lastCompleted'], {'tool': 'search', 'status': 'success',
                                                   'time': '2026-10-16T10:00:13Z'})
        self.assertEqual(public['lastSuccessAt'], '2026-10-16T10:00:13Z')
        self.assertEqual(public['lastFailureAt'], '2026-10-16T10:00:06Z')
        self.assertEqual(public['counts']['errors'], 1)
        self.assertNotIn('SECRET', json.dumps(public))
        self.assertNotIn('secret.example', json.dumps(public))

    def test_oversize_reason_is_scoped_to_failed_operation_and_overrides_no_capture(self):
        public = self.public_events([
            {'event': 'mcp_accepted', 'operation_id': 'big'},
            {'event': 'open_requested', 'operation_id': 'big'},
            {'event': 'http_failure', 'operation_id': 'big', 'code': 'response_too_large'},
            {'event': 'open_rejected', 'operation_id': 'big', 'failure_code': 'no_capture'},
            {'event': 'mcp_response_ready', 'operation_id': 'big', 'is_error': True},
            {'event': 'mcp_accepted', 'operation_id': 'missing'},
            {'event': 'open_requested', 'operation_id': 'missing'},
            {'event': 'open_rejected', 'operation_id': 'missing', 'failure_code': 'no_capture'},
            {'event': 'mcp_response_ready', 'operation_id': 'missing', 'is_error': True},
        ])
        self.assertEqual({row['code']: row['count'] for row in public['requestFailures']},
                         {'response_too_large': 1, 'no_capture': 1})
        self.assertEqual(public['lastCompleted']['tool'], 'open')
        self.assertEqual(public['lastCompleted']['status'], 'error')
        self.assertEqual(public['lastFailureAt'], '2026-10-16T10:00:08Z')

    def test_deadline_busy_cancel_and_unknown_errors_have_static_metadata(self):
        public = self.public_events([
            {'event': 'mcp_accepted', 'operation_id': 'timeout'},
            {'event': 'find_requested', 'operation_id': 'timeout'},
            {'event': 'mcp_deadline', 'operation_id': 'timeout'},
            {'event': 'mcp_response_ready', 'operation_id': 'timeout', 'is_error': True},
            {'event': 'mcp_busy', 'operation_id': 'busy'},
            {'event': 'mcp_busy', 'operation_id': 'busy'},
            {'event': 'mcp_accepted', 'operation_id': 'unknown'},
            {'event': 'open_requested', 'operation_id': 'unknown'},
            {'event': 'open_rejected', 'operation_id': 'unknown', 'failure_code': 'SECRET injected code'},
            {'event': 'mcp_response_ready', 'operation_id': 'unknown', 'is_error': True},
            {'event': 'mcp_accepted', 'operation_id': 'cancel'},
            {'event': 'search_requested', 'operation_id': 'cancel'},
            {'event': 'mcp_cancelled', 'operation_id': 'cancel'},
        ])
        self.assertEqual({row['code']: row['count'] for row in public['requestFailures']},
                         {'deadline': 1, 'busy': 1, 'unknown': 1})
        self.assertEqual(public['counts']['errors'], 3)
        self.assertEqual(public['counts']['busy'], 1)
        self.assertEqual(public['counts']['cancelled'], 1)
        self.assertEqual(public['lastCompleted'], {'tool': 'search', 'status': 'cancelled',
                                                   'time': '2026-10-16T10:00:12Z'})
        self.assertEqual(public['lastFailureAt'], '2026-10-16T10:00:09Z')
        self.assertNotIn('SECRET', json.dumps(public))

    def test_legacy_summary_reports_safe_failure_and_latest_completion(self):
        public = self.public_events([
            {'event': 'search_requested'},
            {'event': 'search_completed'},
            {'event': 'open_requested'},
            {'event': 'open_rejected', 'failure_code': 'no_capture'},
            {'event': 'http_failure', 'code': 'response_too_large'},
            {'event': 'find_requested'},
            {'event': 'find_rejected', 'failure_code': 'SECRET', 'time': 'SECRET timestamp'},
        ])
        self.assertEqual({row['code']: row['count'] for row in public['requestFailures']},
                         {'no_capture': 1, 'unknown': 1})
        self.assertEqual(public['lastCompleted'], {'tool': 'open', 'status': 'error',
                                                   'time': '2026-10-16T10:00:03Z'})
        self.assertEqual(public['lastSuccessAt'], '2026-10-16T10:00:01Z')
        self.assertEqual(public['lastFailureAt'], '2026-10-16T10:00:03Z')
        self.assertNotIn('SECRET', json.dumps(public))

    def test_empty_and_disabled_metadata_have_no_synthetic_completion(self):
        for public in (retrieval.public_retrieval_info(''), self.public_events([])):
            self.assertEqual(public['requestFailures'], [])
            self.assertIsNone(public['lastCompleted'])
            self.assertEqual(public['lastSuccessAt'], '')
            self.assertEqual(public['lastFailureAt'], '')

    def test_legacy_operation_ids_deduplicate_request_failures_and_counts(self):
        public = self.public_events([
            {'event': 'open_requested', 'operation_id': 'old'},
            {'event': 'open_rejected', 'operation_id': 'old', 'failure_code': 'no_capture'},
            {'event': 'open_rejected', 'operation_id': 'old', 'failure_code': 'no_capture'},
        ])
        self.assertEqual(public['counts']['errors'], 1)
        self.assertEqual(public['requestFailures'][0]['count'], 1)
        self.assertEqual(public['lastFailureAt'], '2026-10-16T10:00:01Z')

    def test_missing_policy_restoration_holds_historical_job(self):
        app = self.manager.start_direct_job(self.body())
        settings = json.loads((app.run_dir / 'job-settings.json').read_text())
        settings['retrievalPolicyPath'] = ''
        (app.run_dir / 'job-settings.json').write_text(json.dumps(settings))
        restored = server.restore_saved_app(server.App(app.run_dir / 'transcript.jsonl', self.runs))
        self.assertIn('policy is missing', restored.state['launchHold'])

    def test_broker_python_preserves_virtualenv_symlink_path(self):
        launch_path = str(self.root / '.venv/bin/python')
        Path(launch_path).parent.mkdir(parents=True)
        Path(launch_path).symlink_to(os.sys.executable)
        with patch.object(retrieval.sys, 'executable', launch_path):
            app = self.manager.start_direct_job(self.body())
        policy = json.loads(Path(app.state['retrievalPolicyPath']).read_text())
        self.assertEqual(policy['broker']['command'], launch_path)

    def test_operator_shared_limiter_is_used_but_must_stay_outside_workspace(self):
        shared = self.root / 'shared-retrieval-state'
        with patch.dict(os.environ, {'TCS_PROVER_UI_RETRIEVAL_STATE': str(shared)}):
            app = self.manager.start_direct_job(self.body())
        policy = retrieval_runtime.load_policy(app.state['retrievalPolicyPath'])
        broker = json.loads(Path(policy['broker']['policyFile']).read_text())
        self.assertEqual(broker['rate_limit_directory'], str(shared))
        workspace = self.root / 'unsafe-workspace'
        workspace.mkdir()
        (workspace / 'input.md').write_text('Harmless input.')
        with patch.dict(os.environ, {'TCS_PROVER_UI_RETRIEVAL_STATE': str(workspace / 'state')}):
            with self.assertRaisesRegex(ValueError, 'outside'):
                retrieval.prepare_policy(workspace, CUTOFF)


if __name__ == '__main__':
    unittest.main()
