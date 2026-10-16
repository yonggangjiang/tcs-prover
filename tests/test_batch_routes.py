"""Authenticated saved-batch browsing never restores or starts model workers."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from ui import server


def saved_run(root, batch, marker, run_id='same-job', *, final=True):
    folder = root / batch / run_id if batch != '__root__' else root / run_id
    folder.mkdir(parents=True)
    settings = {'sourceFile': marker + '.md', 'preparedRun': True,
                'goalWorkspace': '/not/a/permitted/saved/workspace', 'elapsedSeconds': 12}
    (folder / 'job-settings.json').write_text(json.dumps(settings))
    (folder / 'draft.md').write_text('# Draft problem\n\nStatement ' + marker)
    (folder / 'checked-statement.md').write_text('# Checked statement\n\nStatement ' + marker)
    records = [{'kind': 'status', 'time': '2026-01-01T00:00:00Z', 'stage': 'solve',
                'node': 'author', 'label': marker}]
    (folder / 'transcript.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records))
    (folder / 'pause.json').write_text(json.dumps({'status': 'paused', 'stage': 'solve',
        'node': 'author', 'elapsedSeconds': 12, 'autoResumeAt': 1, 'pauseCause': 'rate_limit'}))
    (folder / 'PROVED.md').write_text('Saved proof ' + marker)
    if final:
        (folder / 'final.tex').write_text('\\documentclass{article}\n% ' + marker + '\n')
        (folder / 'final.pdf').write_bytes(b'%PDF-1.4\n\x00\xff ' + marker.encode() + b'\n%%EOF\n')
    return folder


class BatchRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'runs'
        self.alpha = saved_run(self.root, 'alpha', 'ALPHA')
        self.beta = saved_run(self.root, 'beta', 'BETA')
        self.paused = saved_run(self.root, 'alpha', 'PAUSED', 'paused-job', final=False)
        self.direct = saved_run(self.root, '__root__', 'DIRECT')
        self.http = server.Server((server.HOST, 0), runs=self.root)
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, method='GET', body=None, headers=None, authenticate=True):
        request_headers = {'Origin': self.http.origin}
        if authenticate:
            request_headers['X-TCS-Prover-Token'] = self.http.token
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=request_headers)
            response = connection.getresponse()
            value = response.read()
            if response.getheader('Content-Type', '').startswith('application/json'):
                value = json.loads(value)
            return response.status, value
        finally:
            connection.close()

    def test_batch_cards_distinguish_direct_live_jobs_from_saved_folders(self):
        status, value = self.request('/batches')
        self.assertEqual(status, 200)
        self.assertEqual(value['currentBatch'], '__root__')
        cards = {row['id']: row for row in value['batches']}
        self.assertEqual(set(cards), {'__root__', 'alpha', 'beta'})
        self.assertEqual(cards['alpha']['jobCount'], 2)
        self.assertTrue(cards['alpha']['readOnly'])
        self.assertTrue(cards['beta']['readOnly'])
        self.assertFalse(cards['__root__']['readOnly'])
        self.assertTrue(cards['__root__']['current'])
        self.assertEqual(set(self.http.jobs), {'same-job'})

    def test_duplicate_ids_have_distinct_job_lists_and_states(self):
        for batch, marker in (('__root__', 'DIRECT'), ('alpha', 'ALPHA'), ('beta', 'BETA')):
            with self.subTest(batch=batch):
                status, value = self.request('/jobs?' + urlencode({'batch': batch}))
                self.assertEqual(status, 200)
                job = next(row for row in value['jobs'] if row['runId'] == 'same-job')
                self.assertEqual(job['batchId'], batch)
                self.assertEqual(job['sourceFile'], marker + '.md')
                status, state = self.request('/state?' + urlencode({'batch': batch, 'job': 'same-job'}))
                self.assertEqual(status, 200)
                self.assertEqual(state['batchId'], batch)
                self.assertIn(marker, state['draft'])
                if batch != '__root__':
                    self.assertTrue(state['readOnly'])
                    self.assertFalse(state['workerActive'])

    def test_saved_artifacts_and_memory_come_only_from_selected_batch(self):
        for batch, folder, marker in (('alpha', self.alpha, 'ALPHA'), ('beta', self.beta, 'BETA')):
            query = urlencode({'batch': batch, 'job': 'same-job'})
            for route, filename in (('download-tex', 'final.tex'), ('download-pdf', 'final.pdf'),
                                    ('transcript', 'transcript.jsonl')):
                with self.subTest(batch=batch, route=route):
                    self.assertEqual(self.request('/' + route + '?' + query),
                                     (200, (folder / filename).read_bytes()))
            status, memory = self.request('/memory?' + query + '&file=PROVED.md')
            self.assertEqual(status, 200)
            self.assertEqual(memory['content'], 'Saved proof ' + marker)
        self.assertEqual(self.request('/download-tex?batch=beta&job=paused-job')[0], 404)

    def test_saved_gets_do_not_restore_jobs_write_checkpoints_or_start_workers(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.root / 'alpha').rglob('*') if path.is_file()}
        with patch.object(server, 'restore_saved_app', side_effect=AssertionError('Saved run restored')), \
                patch.object(server.App, '_spawn_worker', side_effect=AssertionError('Worker started')), \
                patch.object(server.subprocess, 'Popen', side_effect=AssertionError('Subprocess started')):
            for _ in range(2):
                for path in ('/batches', '/jobs?batch=alpha', '/state?batch=alpha&job=paused-job',
                             '/memory?batch=alpha&job=paused-job&file=PROVED.md',
                             '/approach-graph?batch=alpha&job=paused-job'):
                    self.assertEqual(self.request(path)[0], 200, path)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in (self.root / 'alpha').rglob('*') if path.is_file()})
        self.assertEqual(set(self.http.jobs), {'same-job'})

    def test_every_saved_post_is_rejected_before_live_job_lookup(self):
        with patch.object(self.http, 'get_job', side_effect=AssertionError('Live job selected')), \
                patch.object(self.http, 'start_direct_job', side_effect=AssertionError('Job started')), \
                patch.object(self.http, 'delete_job', side_effect=AssertionError('Job deleted')):
            for route in ('resume', 'continue-stopped', 'pause', 'stop', 'reset', 'clear-trace',
                          'delete-job', 'direct', 'direct-batch', 'review', 'finalize', 'unknown'):
                with self.subTest(route=route):
                    self.assertEqual(self.request('/' + route + '?batch=alpha&job=same-job',
                                                  method='POST', body={})[0], 403)
            self.assertEqual(self.request('/direct?batch=alpha', method='POST', body={})[0], 403)

    def test_explicit_root_controls_use_the_existing_live_app(self):
        app = self.http.jobs['same-job']
        with patch.object(app, 'pause') as pause:
            status, value = self.request('/pause?batch=__root__&job=same-job', method='POST', body={})
        self.assertEqual(status, 200)
        self.assertEqual(value['batchId'], '__root__')
        pause.assert_called_once_with()

    def test_saved_reads_require_correct_host_origin_and_token(self):
        for path in ('/batches', '/jobs?batch=alpha', '/state?batch=alpha&job=same-job',
                     '/download-tex?batch=alpha&job=same-job', '/transcript?batch=alpha&job=same-job'):
            for options in ({'authenticate': False}, {'headers': {'Origin': 'https://example.com'}},
                            {'headers': {'Host': 'evil.test'}}):
                with self.subTest(path=path, options=options):
                    self.assertEqual(self.request(path, **options)[0], 403)

    def test_invalid_batch_job_and_duplicate_query_never_fall_back_to_live_job(self):
        for batch in ('', 'missing', '..', '../alpha', '/alpha', 'alpha/../beta', 'alpha\\beta'):
            self.assertEqual(self.request('/state?' + urlencode({'batch': batch, 'job': 'same-job'}))[0], 404)
        for job in ('../same-job', '/same-job', 'same-job/../same-job', 'same-job\\child'):
            self.assertEqual(self.request('/state?' + urlencode({'batch': 'alpha', 'job': job}))[0], 404)
        for query in ('batch=alpha&batch=__root__&job=same-job', 'batch=alpha&job=same-job&job=paused-job'):
            self.assertEqual(self.request('/state?' + query)[0], 400)
            self.assertEqual(self.request('/stop?' + query, method='POST', body={})[0], 400)

    def test_saved_symlinks_cannot_read_other_batches_or_external_artifacts(self):
        (self.root / 'linked-batch').symlink_to(self.root / 'beta', target_is_directory=True)
        (self.root / 'alpha' / 'linked-job').symlink_to(self.beta, target_is_directory=True)
        for path in ('/jobs?batch=linked-batch', '/state?batch=alpha&job=linked-job'):
            self.assertEqual(self.request(path)[0], 404)
        for filename, route in (('final.tex', 'download-tex'), ('final.pdf', 'download-pdf'),
                                ('transcript.jsonl', 'transcript')):
            (self.alpha / filename).unlink()
            (self.alpha / filename).symlink_to(self.beta / filename)
            self.assertEqual(self.request('/' + route + '?batch=alpha&job=same-job')[0], 404)
        (self.alpha / 'PROVED.md').unlink()
        (self.alpha / 'PROVED.md').symlink_to(self.beta / 'PROVED.md')
        status, value = self.request('/memory?batch=alpha&job=same-job&file=PROVED.md')
        self.assertEqual(status, 200)
        self.assertNotIn('content', value)


if __name__ == '__main__':
    unittest.main()
