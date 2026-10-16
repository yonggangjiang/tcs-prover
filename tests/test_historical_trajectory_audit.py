"""Independent evidence checks using Codex's actual completed MCP wrapper.

The broker hash identifies raw archive bytes, not the returned excerpt. These
tests reconcile metadata with the controller's log and never pretend to hash
the excerpt back into the original archive payload.
"""
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import audit_historical_trajectory as auditor


CUTOFF = '2026-10-02T14:13:12Z'
CAPTURE = '2026-01-01T00:00:00Z'
THREAD = 'fixture-thread-P01'
URL = 'https://example.org/paper'
DOCUMENT = {
    'url': URL, 'title': 'An archived paper', 'capture_time': CAPTURE,
    'archive_url': 'https://web.archive.org/web/20260101000000id_/' + URL,
    'sha256': 'a' * 64, 'text': 'An excerpt of verified historical source text.',
    'untrusted_source_content': True,
}


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def broker_event(name, **fields):
    return {'event': name, 'time': '2026-10-16T09:00:00Z',
            'cutoff': CUTOFF, 'provider': 'scholarly_wayback', **fields}


def capture_event(document=None, event='capture_verified'):
    document = DOCUMENT if document is None else document
    return broker_event(event, url_sha256=digest(document['url']),
                        capture=document['capture_time'], body_sha256=document['sha256'])


def tool_event(tool='search', *, documents=None, call_id='call-search-1',
               status='completed', server='historical_search', error=None,
               method='item/completed', cutoff=CUTOFF, thread=THREAD):
    """Match the real app-server record, including a result without isError."""
    documents = [deepcopy(DOCUMENT)] if documents is None else documents
    payload = {'cutoff': cutoff}
    payload.update({'results': documents} if tool == 'search' else {'result': documents[0]})
    item = {
        'type': 'mcpToolCall', 'id': call_id, 'server': server, 'tool': tool,
        'status': status,
        'arguments': {'query': 'old theorem'} if tool == 'search' else {'url': URL},
        'appContext': None, 'mcpAppUi': None, 'pluginId': None, 'readOnlyHint': True,
        'result': {'content': [{'type': 'text', 'text': json.dumps(payload)}],
                   'structuredContent': None, '_meta': None},
        'error': error, 'durationMs': 1200,
    }
    if status != 'completed':
        item['result'] = None
    return {'kind': 'codex_event', 'stage': 'solve', 'node': 'author',
            'event': {'method': method, 'params': {'item': item, 'threadId': thread,
                                                   'turnId': 'fixture-turn'}},
            'root': True, 'time': '2026-10-16T09:00:01Z'}


def tool_item(record):
    return record['event']['params']['item']


def native_tool_event(record=None):
    record = tool_event() if record is None else deepcopy(record)
    params = record['event']['params']
    item = params['item']
    item['type'] = 'McpToolCall'
    item.pop('durationMs', None)
    item.pop('appContext', None)
    item.pop('mcpAppUi', None)
    item.pop('pluginId', None)
    item['duration'] = {'secs': 1, 'nanos': 200_000_000}
    if isinstance(item.get('result'), dict):
        item['result']['isError'] = False
    return {'type': 'event_msg', 'payload': {'type': 'item_completed',
            'thread_id': params['threadId'], 'turn_id': params['turnId'], 'item': item}}


class TrajectoryAuditFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.transcript = self.root / 'transcript.jsonl'
        self.broker_log = self.root / 'historical-search.jsonl'
        self.broker_records = [
            broker_event('startup', status='ready'),
            broker_event('search_requested', query_sha256=digest('old theorem')),
            capture_event(),
            broker_event('search_completed', query_sha256=digest('old theorem'),
                         returned=1, unavailable_candidates=0),
        ]
        self.trajectory_records = [
            tool_event(status='inProgress', method='item/started'), tool_event(),
        ]

    def write_records(self, path, records, suffix=''):
        path.write_text(''.join(json.dumps(row) + '\n' for row in records) + suffix,
                        encoding='utf-8')

    def audit(self, *, cutoff=CUTOFF, thread=THREAD, transcript_suffix='', broker_suffix='',
              native_records=None, native_suffix='', full_document_default=False, content_limit=20000):
        self.write_records(self.transcript, self.trajectory_records, transcript_suffix)
        self.write_records(self.broker_log, self.broker_records, broker_suffix)
        optional = {}
        if native_records is not None:
            native_path = self.root / 'native-session.jsonl'
            self.write_records(native_path, native_records, native_suffix)
            optional['native_session_path'] = native_path
        return auditor.audit_problem('P01', cutoff, self.transcript, self.broker_log,
                                     expected_thread_id=thread, full_document_default=full_document_default,
                                     content_limit=content_limit, **optional)


class HistoricalTrajectoryAuditTests(TrajectoryAuditFixture):
    def test_actual_codex_completed_wrapper_reconciles_the_delivered_capture(self):
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['problem_id'], 'P01')
        self.assertEqual(report['counts']['completed_calls'], 1)
        self.assertEqual(report['counts']['successful_calls'], 1)
        self.assertEqual(report['counts']['returned_documents'], 1)
        self.assertEqual(report['counts']['matched_documents'], 1)
        self.assertEqual(report['counts']['pending_calls'], 0)
        self.assertEqual(report['counts']['error_calls'], 0)
        self.assertEqual(len(report['sources']), 1)

    def test_empty_success_is_valid_and_has_no_retrieved_documents(self):
        self.trajectory_records = [tool_event(documents=[])]
        self.broker_records = [broker_event('search_requested', query_sha256=digest('old theorem')),
                               broker_event('search_completed', query_sha256=digest('old theorem'),
                                            returned=0, unavailable_candidates=0)]
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['successful_calls'], 1)
        self.assertEqual(report['counts']['returned_documents'], 0)

    def test_changed_url_hash_or_capture_cannot_borrow_the_original_evidence(self):
        for field, value in [('url', 'https://example.org/other-paper'),
                             ('sha256', 'b' * 64),
                             ('capture_time', '2025-12-31T23:59:59Z')]:
            with self.subTest(field=field):
                document = {**DOCUMENT, field: value}
                self.trajectory_records = [tool_event(documents=[document])]
                report = self.audit()
                self.assertFalse(report['passed'])
                self.assertTrue(report['issues'])
                self.assertEqual(report['counts']['matched_documents'], 0)

    def test_archive_replay_url_must_match_the_source_and_capture(self):
        for archive_url in ['https://web.archive.org/web/20260102000000id_/' + URL,
                            'https://web.archive.org/web/20260101000000id_/https://other.example/paper',
                            'https://example.org/current-paper']:
            with self.subTest(archive_url=archive_url):
                self.trajectory_records = [tool_event(documents=[{**DOCUMENT, 'archive_url': archive_url}])]
                self.assertFalse(self.audit()['passed'])

    def test_source_fields_require_bounded_typed_metadata(self):
        for field, value in [('sha256', 'A' * 64), ('sha256', 'a' * 63),
                             ('text', {'not': 'text'}), ('title', None),
                             ('untrusted_source_content', False),
                             ('url', 'file:///etc/passwd')]:
            with self.subTest(field=field, value=value):
                document = {**DOCUMENT, field: value}
                self.trajectory_records = [tool_event(documents=[document])]
                self.broker_records = [capture_event(document)]
                self.assertFalse(self.audit()['passed'])

    def test_absent_capture_event_cannot_be_replaced_by_candidates_or_cache_hits(self):
        for event in ['capture_rejected', 'archive_cache_hit', 'search_requested',
                      'source_verified', 'open_requested']:
            with self.subTest(event=event):
                self.broker_records = [capture_event(event=event)]
                report = self.audit()
                self.assertFalse(report['passed'])
                self.assertEqual(report['counts']['matched_documents'], 0)

    def test_open_requires_its_own_completion_evidence(self):
        self.trajectory_records = [tool_event('open')]
        self.broker_records = [capture_event()]
        self.assertFalse(self.audit()['passed'])
        self.broker_records = [broker_event('open_requested', url_sha256=digest(URL)),
                               capture_event(event='open_completed')]
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['matched_documents'], 1)

    def test_cutoff_equality_and_future_capture_fail_even_with_matching_broker_event(self):
        for capture in [CUTOFF, '2026-10-02T14:13:12.000001Z', '2027-01-01T00:00:00Z']:
            with self.subTest(capture=capture):
                document = {**DOCUMENT, 'capture_time': capture}
                self.trajectory_records = [tool_event(documents=[document])]
                self.broker_records = [capture_event(document)]
                report = self.audit()
                self.assertFalse(report['passed'])
                self.assertEqual(report['counts']['matched_documents'], 0)

    def test_naive_missing_and_impossible_capture_times_fail_closed(self):
        for capture in [None, '', '2026-01-01', '2026-01-01T00:00:00',
                        '2026-02-30T00:00:00Z', 1767225600]:
            with self.subTest(capture=capture):
                document = {**DOCUMENT, 'capture_time': capture}
                self.trajectory_records = [tool_event(documents=[document])]
                self.broker_records = [capture_event(document)]
                self.assertFalse(self.audit()['passed'])

    def test_exact_subsecond_cutoff_comparison_does_not_round_up(self):
        limit = '2026-10-02T14:13:12.000001Z'
        document = {**DOCUMENT, 'capture_time': CUTOFF,
                    'archive_url': 'https://web.archive.org/web/20261002141312id_/' + URL}
        self.trajectory_records = [tool_event(documents=[document], cutoff=limit)]
        self.broker_records = [{**capture_event(document), 'cutoff': limit}]
        report = self.audit(cutoff=limit)
        self.assertTrue(report['passed'], report['issues'])

    def test_response_cutoff_is_bound_to_the_controller_problem(self):
        self.trajectory_records = [tool_event(cutoff='2026-10-05T00:37:13Z')]
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertEqual(report['counts']['matched_documents'], 0)

    def test_another_problems_broker_log_cannot_authenticate_this_transcript(self):
        self.broker_records = [{**row, 'cutoff': '2026-10-05T00:37:13Z'}
                               for row in self.broker_records]
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertEqual(report['counts']['matched_documents'], 0)

    def test_expected_thread_binding_detects_cross_problem_transcript_swap(self):
        self.trajectory_records = [tool_event(thread='fixture-thread-P02')]
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertTrue(report['issues'])

    def test_malformed_complete_lines_are_issues_in_either_input(self):
        for which in ['transcript', 'broker']:
            with self.subTest(which=which):
                kwargs = {which + '_suffix': '{"malformed":\n'}
                report = self.audit(**kwargs)
                self.assertFalse(report['passed'])
                self.assertTrue(report['issues'])

    def test_malformed_success_payload_is_not_a_valid_empty_search(self):
        for payload in [None, {}, {'cutoff': CUTOFF, 'results': {}},
                        {'cutoff': CUTOFF}, {'cutoff': CUTOFF, 'results': [None]}]:
            with self.subTest(payload=payload):
                record = tool_event()
                tool_item(record)['result']['content'][0]['text'] = json.dumps(payload)
                self.trajectory_records = [record]
                self.assertFalse(self.audit()['passed'])

    def test_extra_unvalidated_content_blocks_cannot_hide_after_a_valid_result(self):
        for extra in [{'type': 'text', 'text': 'UNVERIFIED_LIVE_TEXT_SENTINEL'},
                      {'type': 'image', 'data': 'ZmFrZQ==', 'mimeType': 'image/png'}]:
            with self.subTest(kind=extra['type']):
                record = tool_event()
                tool_item(record)['result']['content'].append(extra)
                self.trajectory_records = [record]
                self.assertFalse(self.audit()['passed'])

    def test_incomplete_final_lines_are_excluded_from_audited_checkpoint(self):
        transcript_tail = '{"kind":"codex_event","event":'
        broker_tail = '{"event":"capture_verified"'
        report = self.audit(transcript_suffix=transcript_tail, broker_suffix=broker_tail)
        self.assertEqual(report['counts']['returned_documents'], 1)
        self.assertEqual(report['counts']['matched_documents'], 1)
        for kind, path, tail in [('transcript', self.transcript, transcript_tail),
                                 ('broker', self.broker_log, broker_tail)]:
            checkpoint = report['checkpoint'][kind]
            self.assertEqual(checkpoint['trailing_bytes'], len(tail.encode()))
            self.assertEqual(checkpoint['bytes_read'], path.stat().st_size)
            self.assertEqual(checkpoint['complete_bytes'] + checkpoint['trailing_bytes'],
                             checkpoint['bytes_read'])
            prefix = path.read_bytes()[:checkpoint['complete_bytes']]
            self.assertEqual(checkpoint['sha256'], hashlib.sha256(prefix).hexdigest())
            self.assertEqual(checkpoint['complete_lines'], prefix.count(b'\n'))
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['status'], 'pending')
        self.assertTrue(report['checkpoint_pending'])

    def test_started_call_without_reply_is_pending_and_not_an_error(self):
        self.trajectory_records.append(tool_event(call_id='pending-2', status='inProgress',
                                                  method='item/started'))
        self.broker_records.append(broker_event('search_requested', query_sha256=digest('pending query')))
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['pending_calls'], 1)
        self.assertEqual(report['counts']['completed_calls'], 1)
        self.assertEqual(report['counts']['error_calls'], 0)

    def test_duplicate_same_completion_does_not_double_count_delivery(self):
        self.trajectory_records.append(deepcopy(self.trajectory_records[-1]))
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['completed_calls'], 1)
        self.assertEqual(report['counts']['returned_documents'], 1)

    def test_conflicting_terminal_records_for_one_call_are_not_silently_merged(self):
        conflict = tool_event(documents=[])
        self.trajectory_records.append(conflict)
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertTrue(any(issue['code'] == 'conflicting_tool_completion'
                            for issue in report['issues']), report['issues'])

    def test_transport_error_is_counted_without_becoming_a_source_violation(self):
        self.trajectory_records.extend([
            tool_event(call_id='failed-2', status='inProgress', method='item/started'),
            tool_event(call_id='failed-2', status='failed',
                       error={'message': 'MCP tool call timed out.'}),
        ])
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['successful_calls'], 1)
        self.assertEqual(report['counts']['error_calls'], 1)
        self.assertEqual(report['counts']['pending_calls'], 0)
        self.assertEqual(report['counts']['returned_documents'], 1)

    def test_mcp_is_error_body_is_not_parsed_as_a_historical_source(self):
        failed = tool_event(call_id='busy-2')
        tool_item(failed)['result'] = {'isError': True, 'content': [
            {'type': 'text', 'text': 'Historical retrieval is busy; retry later.'}]}
        self.trajectory_records.append(failed)
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['error_calls'], 1)
        self.assertEqual(report['counts']['returned_documents'], 1)

    def test_cancelled_terminal_call_is_separate_from_pending_and_failed_calls(self):
        self.trajectory_records.extend([
            tool_event(call_id='cancelled-2', status='inProgress', method='item/started'),
            tool_event(call_id='cancelled-2', status='cancelled',
                       error={'message': 'Request cancelled.'}),
        ])
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['cancelled_calls'], 1)
        self.assertEqual(report['counts']['pending_calls'], 0)
        self.assertEqual(report['counts']['error_calls'], 0)

    def test_unused_verified_capture_does_not_prove_delivery(self):
        unused = {**DOCUMENT, 'url': 'https://example.org/unused'}
        self.broker_records.append(capture_event(unused))
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['returned_documents'], 1)
        self.assertEqual(report['counts']['matched_documents'], 1)

    def test_native_web_search_item_is_an_explicit_boundary_issue(self):
        record = tool_event()
        record['event']['params']['item'] = {
            'type': 'webSearch', 'id': 'native-web-2', 'status': 'completed',
            'action': {'type': 'search', 'query': 'a live search'},
        }
        self.trajectory_records.append(record)
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertEqual(report['counts']['native_web_calls'], 1)

    def test_unexpected_mcp_server_or_tool_is_reported(self):
        for kwargs in [{'server': 'unapproved_live_search'}, {'tool': 'fetch_live'}]:
            with self.subTest(kwargs=kwargs):
                self.trajectory_records = [tool_event(**kwargs)]
                report = self.audit()
                self.assertFalse(report['passed'])
                self.assertEqual(report['counts']['unexpected_mcp_calls'], 1)

    def test_quoted_tool_names_in_source_text_are_not_tool_execution_signals(self):
        document = {**DOCUMENT, 'text': 'A paper discussing web_search and mcp__live__search.'}
        self.trajectory_records = [tool_event(documents=[document])]
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['native_web_calls'], 0)
        self.assertEqual(report['counts']['unexpected_mcp_calls'], 0)
        self.assertTrue(any('raw' in value.lower() and ('excerpt' in value.lower() or 'text' in value.lower())
                            for value in report['limitations']), report['limitations'])


class BatchAuditFixture(TrajectoryAuditFixture):
    """Use all ten same-cutoff fixtures so path/identity binding does real work."""
    def setUp(self):
        super().setUp()
        self.control = self.root / 'control'
        self.manifest_path = self.root / 'manifest.json'
        self.manifest = {'runs': []}
        self.prepared = {'runs': []}
        self.fixtures = {}
        for number in range(1, 11):
            problem = f'P{number:02}'
            run_id = 'firstproof-b3-' + problem.lower()
            folder = self.root / 'runs' / run_id
            controller = self.control / problem
            folder.mkdir(parents=True)
            controller.mkdir(parents=True)
            input_text = f'Official fixture statement for {problem}.\n'
            (folder / 'input.md').write_text(input_text, encoding='utf-8')
            input_sha = digest(input_text)
            policy_path = controller / 'retrieval-policy.json'
            broker_path = controller / 'broker-policy.json'
            log_path = controller / 'historical-search.jsonl'
            home = controller / 'codex-home'
            thread = 'fixture-thread-' + problem
            broker_policy = {'schema_version': 1, 'provider': 'scholarly_wayback',
                             'solver_workspace': str(folder), 'deny_url_prefixes': []}
            self.write_json(broker_path, broker_policy)
            policy = {
                'schemaVersion': 1, 'mode': 'historical', 'runId': run_id,
                'cutoffExclusiveUtc': CUTOFF,
                'metadata': {'problemId': f'prob-{number:03}', 'inputSha256': input_sha},
                'isolation': {'workspace': str(folder), 'codexHome': str(home)},
                'broker': {'auditLog': str(log_path), 'policyFile': str(broker_path),
                           'args': ['historical_search.py', '--cutoff', CUTOFF,
                                    '--policy-file', str(broker_path),
                                    '--policy-sha256', hashlib.sha256(broker_path.read_bytes()).hexdigest(),
                                    '--audit-log', str(log_path)]},
            }
            self.write_json(policy_path, policy)
            self.write_json(folder / 'job-settings.json',
                            {'retrievalPolicyPath': str(policy_path), 'goalThreadId': thread})
            session = home / 'author' / 'sessions' / ('rollout-' + thread + '.jsonl')
            session.parent.mkdir(parents=True)
            self.write_records(session, [{'type': 'session_meta',
                                          'payload': {'id': thread, 'cwd': str(folder)}}])
            self.write_records(folder / 'transcript.jsonl', [tool_event(thread=thread)])
            self.write_records(log_path, self.broker_records)
            self.manifest['runs'].append({'id': problem, 'problemId': f'prob-{number:03}',
                                          'cutoffExclusiveUtc': CUTOFF,
                                          'statementUtf8Sha256': input_sha})
            self.prepared['runs'].append({'id': problem, 'runId': run_id, 'folder': str(folder),
                                          'policy': str(policy_path), 'inputSha256': input_sha,
                                          'cutoffExclusiveUtc': CUTOFF})
            self.fixtures[problem] = {'folder': folder, 'controller': controller,
                                      'policy_path': policy_path, 'policy': policy,
                                      'broker_path': broker_path, 'broker_policy': broker_policy,
                                      'log_path': log_path, 'session': session, 'thread': thread}
        self.write_json(self.manifest_path, self.manifest)
        self.write_json(self.control / 'prepared-runs.json', self.prepared)

    def write_json(self, path, value):
        path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

    def batch(self):
        return auditor.audit_batch(self.control, self.manifest_path)


class HistoricalBatchBindingTests(BatchAuditFixture):
    def test_ten_runs_bind_inputs_policies_native_sessions_and_returned_evidence(self):
        report = self.batch()
        self.assertTrue(report['passed'], [row['issues'] for row in report['runs']])
        self.assertEqual(len(report['runs']), 10)
        self.assertEqual(report['counts']['returned_documents'], 10)
        self.assertEqual(report['counts']['matched_documents'], 10)
        self.assertTrue(all('native_session_bound' in row['ownership'] for row in report['runs']))

    def test_same_cutoff_other_problem_log_path_fails_ownership_binding(self):
        first, other = self.fixtures['P01'], self.fixtures['P02']
        # The bytes/cutoffs even match: source metadata alone cannot catch this.
        self.assertEqual(first['log_path'].read_bytes(), other['log_path'].read_bytes())
        first['policy']['broker']['auditLog'] = str(other['log_path'])
        self.write_json(first['policy_path'], first['policy'])
        report = self.batch()
        self.assertFalse(report['passed'])
        self.assertFalse(report['runs'][0]['passed'])
        self.assertTrue(all(row['passed'] for row in report['runs'][1:]))
        self.assertEqual(report['runs'][0]['issues'][0]['code'], 'run_binding_failed')

    def test_native_session_workspace_mismatch_is_bound_to_its_problem(self):
        first = self.fixtures['P01']
        self.write_records(first['session'], [{'type': 'session_meta', 'payload': {
            'id': first['thread'], 'cwd': str(self.fixtures['P02']['folder'])}}])
        report = self.batch()
        self.assertFalse(report['passed'])
        self.assertFalse(report['runs'][0]['passed'])
        self.assertTrue(all(row['passed'] for row in report['runs'][1:]))

    def test_changed_official_input_fails_without_discarding_other_nine_reports(self):
        (self.fixtures['P01']['folder'] / 'input.md').write_text('A different problem.\n')
        report = self.batch()
        self.assertFalse(report['passed'])
        self.assertEqual(sum(row['passed'] for row in report['runs']), 9)
        self.assertEqual(report['counts']['matched_documents'], 9)

    def test_missing_prepared_problem_is_a_failed_row_not_a_silent_smaller_batch(self):
        self.prepared['runs'].pop(0)
        self.write_json(self.control / 'prepared-runs.json', self.prepared)
        report = self.batch()
        self.assertFalse(report['passed'])
        self.assertEqual(len(report['runs']), 10)
        self.assertEqual(sum(row['passed'] for row in report['runs']), 9)

    def test_pinned_deny_prefix_is_enforced_even_when_capture_evidence_matches(self):
        first = self.fixtures['P01']
        first['broker_policy']['deny_url_prefixes'] = ['https://example.org/paper']
        self.write_json(first['broker_path'], first['broker_policy'])
        args = first['policy']['broker']['args']
        args[args.index('--policy-sha256') + 1] = hashlib.sha256(first['broker_path'].read_bytes()).hexdigest()
        self.write_json(first['policy_path'], first['policy'])
        report = self.batch()
        self.assertFalse(report['passed'])
        self.assertTrue(any(issue['code'] == 'denied_source' for issue in report['runs'][0]['issues']))

    def test_cli_refuses_to_overwrite_input_controller_evidence_or_native_session(self):
        first = self.fixtures['P01']
        for target in [first['folder'] / 'input.md', first['log_path'],
                       first['policy_path'], first['broker_path'], first['session'],
                       self.manifest_path, self.control / 'prepared-runs.json']:
            with self.subTest(target=target):
                original = target.read_bytes()
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        auditor.main(['--control-root', str(self.control),
                                      '--manifest', str(self.manifest_path), '--output', str(target)])
                self.assertEqual(stopped.exception.code, 2)
                self.assertEqual(target.read_bytes(), original)


class HistoricalNativeSessionTests(TrajectoryAuditFixture):
    def test_native_only_timeout_clears_appserver_pending_call(self):
        self.trajectory_records = [tool_event(status='inProgress', method='item/started')]
        failed = native_tool_event(tool_event(status='failed', error={'message': 'MCP timeout after 600s.'}))
        report = self.audit(native_records=[failed])
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['error_calls'], 1)
        self.assertEqual(report['counts']['pending_calls'], 0)
        self.assertEqual(report['counts']['returned_documents'], 0)

    def test_native_and_appserver_success_are_counted_once_despite_metadata_differences(self):
        report = self.audit(native_records=[native_tool_event()])
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['completed_calls'], 1)
        self.assertEqual(report['counts']['successful_calls'], 1)
        self.assertEqual(report['counts']['returned_documents'], 1)
        self.assertEqual(report['counts']['matched_documents'], 1)

    def test_native_only_completed_source_is_reconciled_with_broker_evidence(self):
        self.trajectory_records = [tool_event(status='inProgress', method='item/started')]
        report = self.audit(native_records=[native_tool_event()])
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['pending_calls'], 0)
        self.assertEqual(report['counts']['returned_documents'], 1)
        self.assertEqual(report['counts']['matched_documents'], 1)

    def test_native_conflicting_reply_is_not_hidden_by_the_appserver_completion(self):
        report = self.audit(native_records=[native_tool_event(tool_event(documents=[]))])
        self.assertFalse(report['passed'])
        self.assertTrue(any(issue['code'] == 'conflicting_tool_completion'
                            for issue in report['issues']), report['issues'])

    def test_partial_native_terminal_record_does_not_clear_pending_call(self):
        self.trajectory_records = [tool_event(status='inProgress', method='item/started')]
        tail = json.dumps(native_tool_event())[:-10]
        report = self.audit(native_records=[], native_suffix=tail)
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['pending_calls'], 1)
        self.assertEqual(report['counts']['returned_documents'], 0)
        self.assertTrue(report['checkpoint_pending'])

    def test_native_thread_swap_is_a_binding_violation(self):
        native = native_tool_event(tool_event(thread='fixture-thread-P02'))
        report = self.audit(native_records=[native])
        self.assertFalse(report['passed'])
        self.assertTrue(any(issue['code'] == 'thread_mismatch'
                            for issue in report['issues']), report['issues'])


class HistoricalMetadataEnumerationTests(TrajectoryAuditFixture):
    @staticmethod
    def enumeration(tool='list_mcp_resources', payload=None):
        field = 'resources' if tool == 'list_mcp_resources' else 'resourceTemplates'
        record = tool_event(tool=tool, server='codex', call_id='metadata-enumeration')
        item = tool_item(record)
        item['arguments'] = {}
        item['result']['content'][0]['text'] = json.dumps({field: []} if payload is None else payload)
        return record

    def test_exact_empty_builtin_inventory_is_visible_as_warning_without_source_release(self):
        for tool in ['list_mcp_resources', 'list_mcp_resource_templates']:
            with self.subTest(tool=tool):
                record = self.enumeration(tool)
                started = deepcopy(record)
                started['event']['method'] = 'item/started'
                tool_item(started).update(status='inProgress', result=None)
                self.trajectory_records = [started, record]
                report = self.audit()
                self.assertTrue(report['passed'], report['issues'])
                self.assertEqual(report['counts']['unexpected_mcp_calls'], 1)
                self.assertEqual(report['counts']['returned_documents'], 0)
                self.assertEqual(len(report['warnings']), 1)
                self.assertEqual(report['warnings'][0]['code'], 'empty_builtin_enumeration')

    def test_nonempty_or_extended_metadata_response_still_fails(self):
        for payload in [{'resources': [{'uri': 'https://example.org/live', 'text': 'unverified'}]},
                        {'resources': [], 'additional_text': 'unverified'},
                        {'resourceTemplates': [{'uriTemplate': 'https://example.org/{name}'}]}]:
            with self.subTest(payload=payload):
                tool = 'list_mcp_resource_templates' if 'resourceTemplates' in payload else 'list_mcp_resources'
                self.trajectory_records = [self.enumeration(tool, payload)]
                self.assertFalse(self.audit()['passed'])

    def test_pending_or_failed_enumeration_cannot_claim_an_empty_completed_inventory(self):
        for status, method in [('inProgress', 'item/started'), ('failed', 'item/completed')]:
            with self.subTest(status=status):
                record = self.enumeration()
                tool_item(record)['status'] = status
                record['event']['method'] = method
                self.trajectory_records = [record]
                self.assertFalse(self.audit()['passed'])

    def test_extra_unvalidated_blocks_or_metadata_do_not_qualify_for_warning(self):
        for mutation in ['extra_block', 'extra_meta', 'other_server']:
            with self.subTest(mutation=mutation):
                record = self.enumeration()
                item = tool_item(record)
                if mutation == 'extra_block':
                    item['result']['content'].append({'type': 'text', 'text': 'LIVE_TEXT_SENTINEL'})
                elif mutation == 'extra_meta':
                    item['result']['_meta'] = {'live_text': 'LIVE_TEXT_SENTINEL'}
                else:
                    item['server'] = 'unexpected_other_server'
                self.trajectory_records = [record]
                self.assertFalse(self.audit()['passed'])

    def test_later_empty_completion_cannot_erase_earlier_nonempty_inventory(self):
        nonempty = self.enumeration(payload={'resources': [{'uri': 'https://example.org/live'}]})
        empty = self.enumeration()
        for records in [[nonempty, empty], [empty, nonempty]]:
            with self.subTest(order=records[0] is empty):
                self.trajectory_records = records
                report = self.audit()
                self.assertFalse(report['passed'])
                self.assertEqual(report['counts']['unexpected_mcp_calls'], 1)


if __name__ == '__main__':
    unittest.main()
