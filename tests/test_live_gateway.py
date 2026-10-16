"""Loopback-only gateway routing and authorization regressions, no models."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from ui.batches import BatchCatalog
from ui.live_gateway import GatewayError, GatewayServer, Upstream, validate_separate_runs


class FakeController(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, run_ids):
        self.token = 'private-controller-token-' + str(id(self))
        self.jobs = [{'runId': ident, 'startedAt': ident} for ident in run_ids]
        self.calls = []
        super().__init__(('127.0.0.1', 0), FakeHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, body, status=200, content_type='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self, method):
        auth = (self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'
                and self.headers.get('Origin') == self.server.origin
                and self.headers.get('X-TCS-Prover-Token') == self.server.token)
        if not auth:
            return self.respond({'error': 'bad upstream authorization'}, 403)
        body = self.rfile.read(int(self.headers.get('Content-Length', '0'))) if method == 'POST' else b''
        self.server.calls.append((method, self.path, body))
        if self.path == '/jobs':
            return self.respond({'jobs': list(self.server.jobs)})
        if self.path.startswith('/download-pdf'):
            return self.respond(b'%PDF-TEST', content_type='application/pdf')
        if self.path.startswith('/transcript'):
            return self.respond(b'{"event":"test"}\n', content_type='application/x-ndjson')
        if method == 'POST' and self.path == '/direct-batch':
            self.server.jobs.append({'runId': 'created', 'startedAt': 'created'})
            return self.respond({'startedJobs': ['created'], 'jobs': list(self.server.jobs)})
        return self.respond({'phase': 'running', 'route': self.path, 'method': method})

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')


class _GatewayFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('index.html', 'app.js', 'styles.css'):
            (self.root / name).write_text('new assets ' + name)
        self.legacy = FakeController(['legacy'])
        self.current = FakeController(['current'])
        self.augmented = []

        def augment(value, *, is_job_list=False):
            self.augmented.append(is_job_list)
            if is_job_list:
                return {'jobs': [{**row, 'retrieval': {'mode': 'historical'}} for row in value['jobs']]}
            return {**value, 'retrieval': {'mode': 'historical'}}

        self.gateway = GatewayServer(('127.0.0.1', 0),
            legacy=Upstream(self.legacy.server_port, self.legacy.token),
            current=Upstream(self.current.server_port, self.current.token),
            augment_legacy=augment, ui_directory=self.root, **self.gateway_options())
        for server in (self.legacy, self.current, self.gateway):
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            self.addCleanup(self.stop, server, thread)

    def gateway_options(self):
        return {}

    @staticmethod
    def stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def request(self, path, *, method='GET', body=None, headers=None, authenticate=True):
        request_headers = {'Origin': self.gateway.origin}
        if authenticate:
            request_headers['X-TCS-Prover-Token'] = self.gateway.token
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=request_headers)
            response = connection.getresponse()
            value = response.read()
            if response.getheader('Content-Type', '').startswith('application/json'):
                value = json.loads(value)
            return response.status, value
        finally:
            connection.close()


class GatewayTests(_GatewayFixture):
    def test_assets_are_new_and_need_no_controller_or_token(self):
        status, value = self.request('/app.js', authenticate=False)
        self.assertEqual((status, value), (200, b'new assets app.js'))
        self.assertEqual(self.legacy.calls, [])
        self.assertEqual(self.current.calls, [])
        self.assertEqual(self.request('/?job=legacy', authenticate=False)[0], 200)

    def test_host_origin_and_gateway_token_are_required(self):
        for options in ({'authenticate': False},
                        {'headers': {'Origin': 'https://example.com'}},
                        {'headers': {'Host': 'evil.test'}},
                        {'headers': {'X-TCS-Prover-Token': self.legacy.token}}):
            with self.subTest(options=list(options)):
                self.assertEqual(self.request('/jobs', **options)[0], 403)
        self.assertEqual(self.request('/app.js', headers={'Host': 'evil.test'})[0], 403)
        self.assertEqual(self.legacy.calls, [])
        self.assertEqual(self.current.calls, [])

    def test_merges_lists_and_augments_only_legacy_response_copies(self):
        status, value = self.request('/jobs')
        self.assertEqual(status, 200)
        jobs = {row['runId']: row for row in value['jobs']}
        self.assertEqual(set(jobs), {'legacy', 'current'})
        self.assertEqual(jobs['legacy']['retrieval']['mode'], 'historical')
        self.assertNotIn('retrieval', jobs['current'])
        self.assertNotIn('retrieval', self.legacy.jobs[0])
        self.assertEqual(self.augmented, [True])

    def test_state_routes_to_owner_and_home_to_current(self):
        for key, expected_retrieval in (('legacy', True), ('current', False), ('', False)):
            status, value = self.request('/state' + ('?job=' + key if key else ''))
            self.assertEqual(status, 200)
            self.assertEqual('retrieval' in value, expected_retrieval)
        self.assertTrue(any(path == '/state?job=legacy' for _, path, _ in self.legacy.calls))
        self.assertFalse(any(path == '/state?job=legacy' for _, path, _ in self.current.calls))

    def test_existing_control_actions_use_upstream_own_security(self):
        routes = ('pause', 'resume', 'stop', 'set-author-time-limit', 'set-research-audits',
                  'steer-author', 'clear-trace', 'resume-checkpoint', 'continue-stopped',
                  'start-research-audit', 'resume-critic')
        for route in routes:
            status, value = self.request('/' + route + '?job=legacy', method='POST', body={'instruction': 'continue'})
            self.assertEqual(status, 200)
            self.assertEqual(value['retrieval']['mode'], 'historical')
        legacy_posts = [path for method, path, _ in self.legacy.calls if method == 'POST']
        self.assertEqual(legacy_posts, ['/' + route + '?job=legacy' for route in routes])
        self.assertFalse(any(method == 'POST' for method, _, _ in self.current.calls))

    def test_creation_only_current_and_batch_refresh_keeps_both_job_sets(self):
        status, value = self.request('/direct-batch', method='POST',
            body={'statements': [{'name': 'test', 'statement': 'x'}], 'retrievalMode': 'historical'})
        self.assertEqual(status, 200)
        self.assertEqual(value['startedJobs'], ['created'])
        self.assertEqual({row['runId'] for row in value['jobs']}, {'legacy', 'current', 'created'})
        self.assertFalse(any(method == 'POST' for method, _, _ in self.legacy.calls))
        self.assertEqual(self.request('/direct?job=legacy', method='POST', body={})[0], 400)

    def test_legacy_policy_changes_rejected_including_nested_fields(self):
        bodies = ({'retrievalMode': 'disabled'}, {'cutoffExclusiveUtc': '2030-01-01T00:00:00Z'},
                  {'settings': [{'retrievalPolicyPath': '/tmp/replacement'}]})
        for body in bodies:
            self.assertEqual(self.request('/resume?job=legacy', method='POST', body=body)[0], 400)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.legacy.calls))

    def test_collision_fails_closed_for_lists_reads_and_mutations(self):
        self.current.jobs.append({'runId': 'legacy', 'startedAt': ''})
        for path, method in (('/jobs', 'GET'), ('/state?job=legacy', 'GET'),
                             ('/stop?job=legacy', 'POST'), ('/direct', 'POST')):
            self.assertEqual(self.request(path, method=method, body={} if method == 'POST' else None)[0], 409)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.legacy.calls + self.current.calls))

    def test_no_user_controlled_upstream_path_or_unknown_job(self):
        for path in ('/state?job=legacy&upstream=http://example.com',
                     '/state?job=legacy&job=current', '/../../auth.json',
                     'http://example.com/jobs', '/unknown'):
            self.assertIn(self.request(path)[0], (400, 403, 404))
        self.assertEqual(self.request('/state?job=unknown')[0], 404)
        self.assertEqual(self.request('/stop', method='POST', body={})[0], 400)

    def test_downloads_and_transcripts_preserve_bytes(self):
        self.assertEqual(self.request('/download-pdf?job=legacy'), (200, b'%PDF-TEST'))
        self.assertEqual(self.request('/transcript?job=legacy'), (200, b'{"event":"test"}\n'))

    def test_launch_record_is_loopback_only_and_tokens_not_in_repr(self):
        path = self.root / 'launch.json'
        path.write_text(json.dumps({'url': self.legacy.origin + '/#' + self.legacy.token}))
        upstream = Upstream.from_launch_status(path)
        self.assertEqual(upstream.port, self.legacy.server_port)
        self.assertNotIn(self.legacy.token, repr(upstream))
        for url in ('https://127.0.0.1:1234/#token', 'http://example.com:1234/#token',
                    'http://user@127.0.0.1:1234/#token', 'http://127.0.0.1:1234/path#token'):
            path.write_text(json.dumps({'url': url}))
            with self.assertRaises(ValueError):
                Upstream.from_launch_status(path)

    def test_runs_must_be_disjoint(self):
        for new in (self.root, self.root / 'nested', self.root.parent):
            with self.assertRaises(ValueError):
                validate_separate_runs(self.root, new)
        self.assertEqual(validate_separate_runs(self.root / 'old', self.root / 'new'),
                         ((self.root / 'old').resolve(), (self.root / 'new').resolve()))

    def test_preflight_posts_have_long_timeout_and_are_never_retried(self):
        upstream = Upstream(self.legacy.server_port, self.legacy.token)
        for method, path, expected in (('GET', '/jobs', 15), ('POST', '/direct', 600),
                                       ('POST', '/direct-batch', 1800)):
            connection = MagicMock()
            connection.request.side_effect = TimeoutError('PRIVATE INTERNAL CONTROLLER DETAIL')
            with patch('ui.live_gateway.http.client.HTTPConnection', return_value=connection) as constructor:
                with self.assertRaises(GatewayError) as caught:
                    upstream.request(method, path, b'{}' if method == 'POST' else None)
                constructor.assert_called_once_with('127.0.0.1', self.legacy.server_port, timeout=expected)
                self.assertEqual(connection.request.call_count, 1)
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertNotIn(self.legacy.token, str(caught.exception))
                if method == 'POST':
                    self.assertIn('Refresh job status before retrying', str(caught.exception))


class GatewayBatchTests(_GatewayFixture):
    def gateway_options(self):
        self.runs = self.root / 'runs'
        self.saved = {}
        for batch, marker in (('__root__', 'ORIGINAL'), ('old-one', 'OLD ONE'),
                              ('old-two', 'OLD TWO'), ('v4', 'STALE V4'), ('new', 'STALE NEW')):
            folder = self.runs / 'same-job' if batch == '__root__' else self.runs / batch / 'same-job'
            folder.mkdir(parents=True)
            (folder / 'draft.md').write_text('# Draft problem\n\n' + marker)
            (folder / 'job-settings.json').write_text(json.dumps({'sourceFile': marker + '.md'}))
            (folder / 'transcript.jsonl').write_text(json.dumps({'kind': 'status', 'label': marker}) + '\n')
            (folder / 'pause.json').write_text(json.dumps({'status': 'paused', 'stage': 'solve',
                                                         'node': 'author', 'autoResumeAt': 1}))
            (folder / 'final.tex').write_text('% ' + marker + '\n')
            (folder / 'final.pdf').write_bytes(b'%PDF-1.4\n\x00\xff ' + marker.encode())
            (folder / 'PROVED.md').write_text('Proof ' + marker)
            self.saved[batch] = folder
        for controller in (self.legacy, self.current):
            controller.jobs.append({'runId': 'same-job', 'startedAt': '2026-01-01T00:00:00Z',
                                    'phase': 'running'})
        return {'catalog': BatchCatalog(self.runs, current_root=self.runs / 'v4'),
                'legacy_batch': 'v4', 'current_batch': 'new'}

    def test_batch_cards_use_controller_counts_despite_duplicate_ids(self):
        status, value = self.request('/batches')
        self.assertEqual(status, 200)
        self.assertEqual(value['currentBatch'], 'v4')
        cards = {row['id']: row for row in value['batches']}
        self.assertEqual(set(cards), {'__root__', 'old-one', 'old-two', 'v4', 'new'})
        for batch in ('v4', 'new'):
            self.assertEqual(cards[batch]['jobCount'], 2)
            self.assertFalse(cards[batch]['readOnly'])
            self.assertEqual(cards[batch]['counts']['running'], 1)
        self.assertTrue(cards['v4']['current'])
        self.assertTrue(cards['old-one']['readOnly'])
        # Ambiguous legacy callers still fail closed without an explicit batch.
        self.assertEqual(self.request('/jobs')[0], 409)

    def test_live_lists_and_states_use_only_selected_authoritative_controller(self):
        for batch, controller, other in (('v4', self.legacy, self.current),
                                         ('new', self.current, self.legacy)):
            controller.calls.clear()
            other.calls.clear()
            status, value = self.request('/jobs?batch=' + batch)
            self.assertEqual(status, 200)
            self.assertEqual({row['runId'] for row in value['jobs']},
                             {row['runId'] for row in controller.jobs})
            self.assertTrue(all(row['batchId'] == batch for row in value['jobs']))
            status, state = self.request('/state?batch=' + batch + '&job=same-job&after=4')
            self.assertEqual(status, 200)
            self.assertEqual(state['batchId'], batch)
            self.assertEqual(state['phase'], 'running')
            self.assertEqual('retrieval' in state, batch == 'v4')
            self.assertEqual(other.calls, [])
            self.assertTrue(all('batch' not in parse_qs(urlsplit(path).query)
                                for _, path, _ in controller.calls))
        self.assertEqual(self.request('/state?batch=v4&job=current')[0], 404)
        self.assertEqual(self.request('/state?batch=new&job=legacy')[0], 404)

    def test_live_control_strips_batch_and_never_mutates_same_id_in_other_batch(self):
        for batch, controller, other in (('v4', self.legacy, self.current),
                                         ('new', self.current, self.legacy)):
            controller.calls.clear()
            other.calls.clear()
            status, value = self.request('/pause?batch=' + batch + '&job=same-job',
                                         method='POST', body={'instruction': 'wait'})
            self.assertEqual(status, 200)
            self.assertEqual(value['batchId'], batch)
            self.assertEqual([(method, path) for method, path, _ in controller.calls if method == 'POST'],
                             [('POST', '/pause?job=same-job')])
            self.assertEqual(other.calls, [])
        before = list(self.current.calls)
        self.assertEqual(self.request('/stop?batch=new&job=legacy', method='POST', body={})[0], 404)
        self.assertEqual([call for call in self.current.calls[len(before):] if call[0] == 'POST'], [])

    def test_explicit_creation_refreshes_only_new_batch_even_with_duplicate_live_ids(self):
        status, value = self.request('/direct-batch?batch=new', method='POST',
                                     body={'statements': [{'name': 'P01', 'statement': 'x'}]})
        self.assertEqual(status, 200)
        self.assertEqual(value['batchId'], 'new')
        self.assertEqual(value['startedJobs'], ['created'])
        self.assertEqual({row['runId'] for row in value['jobs']}, {'current', 'same-job', 'created'})
        self.assertTrue(all(row['batchId'] == 'new' for row in value['jobs']))
        self.assertFalse(any(method == 'POST' for method, _, _ in self.legacy.calls))
        self.assertEqual(self.request('/direct?batch=v4', method='POST', body={})[0], 400)

    def test_explicit_legacy_batch_keeps_historical_policy_fixed(self):
        for body in ({'retrievalMode': 'disabled'}, {'settings': {'retrievalCutoffUtc': '2030-01-01T00:00:00Z'}}):
            self.assertEqual(self.request('/resume?batch=v4&job=same-job', method='POST', body=body)[0], 400)
        self.assertEqual(self.request('/direct?batch=v4&job=same-job', method='POST', body={})[0], 400)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.legacy.calls))
        self.assertEqual(self.current.calls, [])

    def test_live_artifacts_and_memory_do_not_use_stale_catalog_files(self):
        for batch, controller, other in (('v4', self.legacy, self.current),
                                         ('new', self.current, self.legacy)):
            controller.calls.clear()
            other.calls.clear()
            self.assertEqual(self.request('/download-pdf?batch=' + batch + '&job=same-job'),
                             (200, b'%PDF-TEST'))
            self.assertEqual(self.request('/transcript?batch=' + batch + '&job=same-job'),
                             (200, b'{"event":"test"}\n'))
            status, value = self.request('/memory?batch=' + batch + '&job=same-job&file=PROVED.md&version=abc')
            self.assertEqual(status, 200)
            self.assertEqual(parse_qs(urlsplit(value['route']).query),
                             {'job': ['same-job'], 'file': ['PROVED.md'], 'version': ['abc']})
            self.assertEqual(other.calls, [])

    def test_saved_duplicate_ids_return_selected_bytes_without_upstream_requests(self):
        for batch in ('__root__', 'old-one', 'old-two'):
            query = urlencode({'batch': batch, 'job': 'same-job'})
            status, value = self.request('/jobs?batch=' + batch)
            self.assertEqual(status, 200)
            self.assertEqual([row['runId'] for row in value['jobs']], ['same-job'])
            self.assertTrue(value['jobs'][0]['readOnly'])
            status, state = self.request('/state?' + query)
            self.assertEqual(status, 200)
            self.assertEqual(state['batchId'], batch)
            self.assertTrue(state['readOnly'])
            for route, filename in (('download-tex', 'final.tex'), ('download-pdf', 'final.pdf'),
                                    ('transcript', 'transcript.jsonl')):
                self.assertEqual(self.request('/' + route + '?' + query),
                                 (200, (self.saved[batch] / filename).read_bytes()))
            status, value = self.request('/memory?' + query + '&file=PROVED.md')
            self.assertEqual(status, 200)
            self.assertEqual(value['content'], (self.saved[batch] / 'PROVED.md').read_text())
        self.assertEqual(self.legacy.calls, [])
        self.assertEqual(self.current.calls, [])

    def test_saved_posts_fail_before_any_upstream_action_or_file_change(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.saved['old-one'].iterdir() if path.is_file()}
        for route in ('pause', 'resume', 'stop', 'delete-job', 'reset', 'clear-trace', 'continue-stopped',
                      'direct', 'direct-batch', 'review', 'finalize', 'set-author-time-limit'):
            for batch in ('__root__', 'old-one', 'old-two'):
                with self.subTest(batch=batch, route=route):
                    self.assertEqual(self.request('/' + route + '?batch=' + batch + '&job=same-job',
                                                  method='POST', body={})[0], 403)
        self.assertEqual(self.request('/direct?batch=old-one', method='POST', body={})[0], 403)
        self.assertEqual(self.legacy.calls, [])
        self.assertEqual(self.current.calls, [])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.saved['old-one'].iterdir() if path.is_file()})

    def test_batch_routes_require_gateway_authorization(self):
        for path in ('/batches', '/jobs?batch=old-one', '/state?batch=old-one&job=same-job',
                     '/download-tex?batch=old-one&job=same-job'):
            for options in ({'authenticate': False}, {'headers': {'Origin': 'https://example.com'}},
                            {'headers': {'Host': 'evil.test'}},
                            {'headers': {'X-TCS-Prover-Token': self.legacy.token}}):
                self.assertEqual(self.request(path, **options)[0], 403)
        self.assertEqual(self.request('/stop?batch=v4&job=same-job', method='POST', body={},
                                      authenticate=False)[0], 403)
        self.assertEqual(self.legacy.calls, [])
        self.assertEqual(self.current.calls, [])
        self.assertEqual(self.request('/?batch=old-one&job=same-job', authenticate=False)[0], 200)

    def test_invalid_or_duplicate_batch_never_falls_back_to_live_controller(self):
        for batch in ('', 'missing', '..', '../v4', '/v4', 'old-one/../v4', 'old-one\\v4'):
            query = urlencode({'batch': batch, 'job': 'same-job'})
            self.assertEqual(self.request('/state?' + query)[0], 404)
            self.assertEqual(self.request('/stop?' + query, method='POST', body={})[0], 403)
        for query in ('batch=old-one&batch=v4&job=same-job', 'batch=v4&job=same-job&job=legacy'):
            self.assertEqual(self.request('/state?' + query)[0], 400)
            self.assertEqual(self.request('/stop?' + query, method='POST', body={})[0], 400)
        self.assertEqual(self.request('/state?batch=v4&job=same-job&upstream=http://example.com')[0], 404)
        self.assertEqual(self.legacy.calls, [])
        self.assertEqual(self.current.calls, [])


if __name__ == '__main__':
    unittest.main()
