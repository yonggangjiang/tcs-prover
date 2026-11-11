"""Authenticated saved-batch browsing never restores or starts model workers; batches made with New batch."""
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from unittest.mock import patch
from urllib.parse import urlencode

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, providers
from ui import named_batches, server


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

    def test_saved_artifacts_come_only_from_selected_batch(self):
        for batch, folder in (('alpha', self.alpha), ('beta', self.beta)):
            query = urlencode({'batch': batch, 'job': 'same-job'})
            for route, filename in (('download-tex', 'final.tex'), ('download-pdf', 'final.pdf'),
                                    ('transcript', 'transcript.jsonl')):
                with self.subTest(batch=batch, route=route):
                    self.assertEqual(self.request('/' + route + '?' + query),
                                     (200, (folder / filename).read_bytes()))
        self.assertEqual(self.request('/download-tex?batch=beta&job=paused-job')[0], 404)

    def test_saved_gets_do_not_restore_jobs_write_checkpoints_or_start_workers(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.root / 'alpha').rglob('*') if path.is_file()}
        with patch.object(server, 'restore_saved_app', side_effect=AssertionError('Saved run restored')), \
                patch.object(server.App, '_spawn_worker', side_effect=AssertionError('Worker started')), \
                patch.object(server.subprocess, 'Popen', side_effect=AssertionError('Subprocess started')):
            for _ in range(2):
                for path in ('/batches', '/jobs?batch=alpha', '/state?batch=alpha&job=paused-job'):
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

    def test_research_file_routes_are_unknown_for_live_and_saved_jobs(self):
        # The notebooks that older runs left in their folders are not served.
        for folder in (self.direct, self.alpha):
            (folder / 'PROVED.md').write_text('Saved proof')
            (folder / 'APPROACHES').mkdir()
            (folder / 'APPROACHES' / 'A001.md').write_text('# A001 Saved route')
        for batch, unknown in (('', 'Not found.'), ('batch=alpha&', 'Unknown batch, job, or saved artifact.')):
            self.assertEqual(self.request('/unknown?' + batch + 'job=same-job'), (404, {'error': unknown}))
            for path in ('/memory?' + batch + 'job=same-job&file=PROVED.md',
                         '/memory?' + batch + 'job=same-job&file=APPROACHES',
                         '/approach-graph?' + batch + 'job=same-job'):
                with self.subTest(path=path):
                    self.assertEqual(self.request(path), (404, {'error': unknown}))


# A job that starts with the author and nothing after it: one runner process (recorded, never started).
AUTHOR_ONLY = {'review': False, 'author': True, 'critic': False, 'writing': False}


class NamedBatchRouteTests(unittest.TestCase):
    """New batch, New problem in this batch, Rename and Delete batch, through the HTTP routes.

    Each job's folder stays directly under runs/; its batch is a setting. Popen records the runner it
    would start, and every model entry point fails the test: nothing runs.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.runs = self.base / 'runs'
        self.runs.mkdir()
        (self.base / 'bin').mkdir()
        self.launches = []
        environment = {'HOME': str(self.base / 'home'), 'CODEX_HOME': str(self.base / 'codex-home'),
                       'CLAUDE_CONFIG_DIR': str(self.base / 'claude-home'), 'PATH': str(self.base / 'bin'),
                       providers.STORE_ENV: str(self.base / 'state' / 'api-models.json')}
        for change in (patch.dict(os.environ, environment),
                       patch.object(server, 'grant_windows_access'),
                       patch.object(runtime, 'verify_model_credentials'),
                       patch.object(server.App, '_spawn_worker'),
                       patch.object(server.subprocess, 'Popen', side_effect=self.popen),
                       *(patch.object(target, name, side_effect=AssertionError(name + ' reached'))
                         for target, name in ((runtime, 'goal_session'), (runtime, 'author_conversation'),
                                              (claude_cli, 'goal_session'), (claude_cli, 'author_conversation')))):
            change.start()
            self.addCleanup(change.stop)
        self.serve()

    def popen(self, argv, **kwargs):
        self.launches.append((argv, kwargs.get('cwd')))
        return mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(''), poll=mock.Mock(return_value=None))

    def serve(self):
        """A server on port 0, answering HTTP without its retry thread (nothing resumes by itself)."""
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        worker = threading.Thread(target=ThreadingHTTPServer.serve_forever, args=(self.http, 0.01), daemon=True)
        worker.start()

        def stop(http=self.http, worker=worker):
            ThreadingHTTPServer.shutdown(http)
            http.server_close()
            worker.join(timeout=2)
        self.stop_serving = stop
        self.addCleanup(stop)

    def restart(self, meanwhile=None):
        """Stop this server as a restart would, then start another on the same runs folder (``meanwhile`` runs in
        between, as a person editing files would)."""
        for app in list(self.http.jobs.values()):
            app.add_trace({'kind': 'request', 'stage': 'solve', 'text': 'Prove.'})
            app.flush_trace(force=True)
            app._claims.release_all()
        self.stop_serving()
        if meanwhile is not None:
            meanwhile()
        with patch('sys.stderr', new_callable=io.StringIO):  # The warning about a damaged list.
            self.serve()

    def call(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=10)
        request_headers = {'Origin': self.http.origin, 'X-TCS-Prover-Token': self.http.token, **(headers or {})}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            request_headers['Content-Type'] = 'application/json'
        try:
            connection.request('POST' if body is not None else 'GET', path, body=raw, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b'{}')
        finally:
            connection.close()

    def make_batch(self, name=''):
        status, value = self.call('/batches', {'name': name})
        self.assertEqual(status, 200, value)
        return value['batch']['id']

    def start(self, batch=None, content='Every graph has property P.'):
        status, state = self.call('/start-workflow' + ('?' + urlencode({'batch': batch}) if batch else ''),
                                  {'content': content, 'workflowStages': AUTHOR_ONLY})
        self.assertEqual(status, 200, state)
        return state

    def settings(self, run_id):
        return json.loads((self.runs / server.CONTROLLER_FOLDER / run_id / server.JOB_SETTINGS_FILENAME).read_text())

    def idle(self, run_id):
        """Let a job's (recorded) runner end, as a finished job's does."""
        app = self.http.jobs[run_id]
        with app.lock:
            app.state['phase'] = 'done'
            app.process = app.worker_token = None

    def test_new_batch_answers_with_its_card_and_refuses_a_taken_name(self):
        status, value = self.call('/batches')
        self.assertEqual((status, value['batches'], value['canCreateBatches'], value['batchesProblem']),
                         (200, [], True, ''))
        status, value = self.call('/batches', {'name': '  Graph   coloring '})
        self.assertEqual(status, 200, value)
        batch = value['batch']
        self.assertEqual(batch['name'], 'Graph coloring')
        self.assertRegex(batch['id'], r'^batch_[0-9a-f]{10}$')
        [card] = value['batches']
        self.assertEqual({key: card[key] for key in ('id', 'label', 'named', 'addProblems', 'readOnly', 'jobCount',
                                                     'current', 'createdAt', 'counts')},
                         {'id': batch['id'], 'label': 'Graph coloring', 'named': True, 'addProblems': True,
                          'readOnly': False, 'jobCount': 0, 'current': False, 'createdAt': batch['createdAt'],
                          'counts': {}})
        self.assertTrue(value['canCreateBatches'])
        # A name in use, in another case: the refusal quotes the batch that has it, as that batch spells it.
        status, value = self.call('/batches', {'name': 'graph COLORING'})
        self.assertEqual((status, value['error']),
                         (400, 'There is already a batch named “Graph coloring”. Choose another name.'))
        status, value = self.call('/batches', {'name': 'x' * 201})
        self.assertEqual((status, value['error']), (400, 'A batch name can have at most 200 characters.'))
        status, value = self.call('/batches', {})
        self.assertEqual(status, 200, value)
        self.assertTrue(value['batch']['name'].startswith('Batch · '), value)
        # Newest first, both empty and both listed; nothing was started.
        self.assertEqual([card['label'] for card in value['batches']], [value['batch']['name'], 'Graph coloring'])
        self.assertEqual((self.launches, self.http.jobs), ([], {}))
        self.assertEqual([path.name for path in self.runs.iterdir()], [server.CONTROLLER_FOLDER])

    def test_a_problem_started_in_a_batch_keeps_its_folder_in_runs_and_lists_only_there(self):
        batch = self.make_batch('Graph coloring')
        state = self.start(batch)
        run_id = state['runId']
        self.assertEqual(state['batchId'], batch)
        app = self.http.jobs[run_id]
        self.assertEqual(app.run_dir.parent, self.runs)
        [(argv, cwd)] = self.launches
        self.assertEqual(Path(cwd), app.run_dir)
        # The controller's copy (trusted) and the run folder's copy both name the batch.
        self.assertEqual(self.settings(run_id)['batch'], batch)
        self.assertEqual(json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())['batch'], batch)
        root = self.start(content='A statement for the default batch.')
        self.assertEqual(root['batchId'], '__root__')
        self.assertNotIn('batch', self.settings(root['runId']))
        for query, expected in ((f'?batch={batch}', [run_id]), ('?batch=__root__', [root['runId']]),
                                ('', sorted([run_id, root['runId']]))):
            with self.subTest(query=query):
                status, value = self.call('/jobs' + query)
                self.assertEqual(status, 200)
                self.assertEqual(sorted(row['runId'] for row in value['jobs']), expected)
        status, value = self.call('/jobs')
        self.assertEqual({row['runId']: row['batchId'] for row in value['jobs']},
                         {run_id: batch, root['runId']: '__root__'})
        status, value = self.call('/batches')
        cards = {card['id']: card for card in value['batches']}
        self.assertEqual([card['id'] for card in value['batches']], ['__root__', batch])
        self.assertEqual((cards['__root__']['label'], cards['__root__']['default'], cards['__root__']['current']),
                         ('Default batch', True, True))
        self.assertEqual((cards['__root__']['jobCount'], cards[batch]['jobCount']), (1, 1))
        self.assertEqual(cards[batch]['counts'], {'running': 1})
        self.assertTrue(cards[batch]['startedAt'])

    def test_job_routes_act_only_on_the_jobs_of_the_named_batch(self):
        batch = self.make_batch('Graph coloring')
        run_id = self.start(batch)['runId']
        for path in ('/state', '/transcript', '/download-tex'):
            with self.subTest(path=path):
                self.assertEqual(self.call(path + '?' + urlencode({'batch': '__root__', 'job': run_id})),
                                 (404, {'error': 'That job is not in this batch.'}))
        app = self.http.jobs[run_id]
        with patch.object(app, 'pause') as pause:
            self.assertEqual(self.call('/pause?' + urlencode({'batch': '__root__', 'job': run_id}), {}),
                             (404, {'error': 'That job is not in this batch.'}))
            pause.assert_not_called()
            status, value = self.call('/pause?' + urlencode({'batch': batch, 'job': run_id}), {})
            self.assertEqual((status, value['batchId']), (200, batch))
            pause.assert_called_once_with()
        status, value = self.call('/state?' + urlencode({'batch': batch, 'job': run_id}))
        self.assertEqual((status, value['batchId'], value['runId']), (200, batch, run_id))
        # Without a batch (the attention banner, a notification) the answer names the job's batch.
        status, value = self.call('/state?' + urlencode({'job': run_id}))
        self.assertEqual((status, value['batchId']), (200, batch))

    def test_the_new_problem_page_reads_its_empty_state_in_any_batch(self):
        saved = self.runs / 'alpha' / 'old-job'
        saved.mkdir(parents=True)
        (saved / 'draft.md').write_text('# Draft problem\n\nOld.\n')
        batch = self.make_batch('Graph coloring')
        for name in (batch, 'alpha', 'batch_0123456789', 'unknown', '__root__'):
            with self.subTest(batch=name):
                status, value = self.call('/state?' + urlencode({'batch': name, 'after': 0}))
                self.assertEqual((status, value['phase'], value['runId']), (200, 'input', ''))

    def test_a_folder_run_joins_the_batch_and_so_do_its_retried_statements(self):
        batch = self.make_batch('Lower bounds')
        self.start(content='A statement for the default batch.')
        status, result = self.call('/direct-batch?' + urlencode({'batch': batch}), {
            'statements': [{'name': 'a.md', 'statement': 'A.'}, {'name': 'b.md', 'statement': 'B.'}],
            'workflowStages': AUTHOR_ONLY})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['batchId'], batch)
        self.assertEqual(sorted(row['sourceFile'] for row in result['jobs']), ['a.md', 'b.md'])
        self.assertTrue(all(row['batchId'] == batch and row['folderBatch'] == result['folderBatch']
                            for row in result['jobs']))
        for run_id in result['startedJobs']:
            self.assertEqual(self.settings(run_id)['batch'], batch)
            self.assertEqual(self.http.jobs[run_id].run_dir.parent, self.runs)
        # A statement whose job could not be created yet keeps the batch when it is tried again.
        self.http._pending_jobs.append({'name': 'c.md', 'statement': 'C.', 'options': {}, 'folderBatch':
                                        result['folderBatch'], 'batch': batch, 'retryAt': 0, 'error': 'Disk full.'})
        status, value = self.call('/batches/delete?' + urlencode({'batch': batch}), {})
        self.assertEqual((status, value['error']), (400, 'Only an empty batch can be deleted. Delete its problems first.'))
        self.http._create_pending_jobs(1)
        self.assertEqual(self.http._pending_jobs, [])
        [created] = [app for app in self.http.jobs.values() if app.state.get('sourceFile') == 'c.md']
        self.assertEqual((created.batch_id, self.settings(created.state['runId'])['batch']), (batch, batch))
        status, value = self.call('/jobs?' + urlencode({'batch': batch}))
        self.assertEqual(sorted(row['sourceFile'] for row in value['jobs']), ['a.md', 'b.md', 'c.md'])
        # A folder run without a batch goes to the default batch.
        status, result = self.call('/direct-batch', {'statements': [{'name': 'd.md', 'statement': 'D.'}],
                                                     'workflowStages': AUTHOR_ONLY})
        self.assertEqual((status, result['batchId']), (200, '__root__'))
        self.assertEqual(sorted(row['sourceFile'] for row in result['jobs']), ['', 'd.md'])

    def test_rename_and_delete_a_batch(self):
        batch = self.make_batch('Lower bounds')
        other = self.make_batch('Upper bounds')
        status, value = self.call('/batches/rename?' + urlencode({'batch': batch}), {'name': ' LB '})
        self.assertEqual((status, value['batch']['id'], value['batch']['name']), (200, batch, 'LB'))
        self.assertEqual({card['id']: card['label'] for card in value['batches']}, {batch: 'LB', other: 'Upper bounds'})
        for path, body, error in (
                ('/batches/rename?' + urlencode({'batch': batch}), {'name': 'upper BOUNDS'},
                 'There is already a batch named “Upper bounds”. Choose another name.'),
                ('/batches/rename?' + urlencode({'batch': batch}), {'name': ' '}, 'Enter a name for the batch.'),
                ('/batches/rename?batch=__root__', {'name': 'X'}, 'The default batch cannot be renamed.'),
                ('/batches/delete?batch=__root__', {}, 'The default batch cannot be deleted.')):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.call(path, body), (400, {'error': error}))
        for path in ('/batches/rename?batch=batch_0123456789', '/batches/delete?batch=batch_0123456789'):
            self.assertEqual(self.call(path, {'name': 'X'}), (403, {'error': 'That batch no longer exists.'}))
        run_id = self.start(batch)['runId']
        status, value = self.call('/batches/delete?' + urlencode({'batch': batch}), {})
        self.assertEqual((status, value['error']), (400, 'Only an empty batch can be deleted. Delete its problems first.'))
        self.idle(run_id)
        status, value = self.call('/delete-job?' + urlencode({'batch': batch, 'job': run_id}), {})
        self.assertEqual((status, value), (200, {'deleted': run_id}))
        status, value = self.call('/batches/delete?' + urlencode({'batch': batch}), {})
        self.assertEqual((status, value['deleted']), (200, batch))
        self.assertEqual([card['id'] for card in value['batches']], [other])
        saved = json.loads((self.runs / server.CONTROLLER_FOLDER / named_batches.FILENAME).read_text())
        self.assertEqual([row['id'] for row in saved['batches']], [other])

    def test_a_restart_keeps_each_jobs_batch_and_the_names(self):
        batch = self.make_batch('Lower bounds')
        status, result = self.call('/direct-batch?' + urlencode({'batch': batch}), {
            'statements': [{'name': 'a.md', 'statement': 'A.'}, {'name': 'b.md', 'statement': 'B.'}],
            'workflowStages': AUTHOR_ONLY})
        self.assertEqual(status, 200, result)
        root = self.start(content='A statement for the default batch.')
        self.call('/batches/rename?' + urlencode({'batch': batch}), {'name': 'LB'})
        self.restart()
        self.assertEqual({app.batch_id for app in self.http.jobs.values()}, {batch, ''})
        status, value = self.call('/jobs?' + urlencode({'batch': batch}))
        self.assertEqual(sorted(row['sourceFile'] for row in value['jobs']), ['a.md', 'b.md'])
        status, value = self.call('/jobs?batch=__root__')
        self.assertEqual([row['runId'] for row in value['jobs']], [root['runId']])
        status, value = self.call('/batches')
        self.assertEqual([(card['id'], card['label'], card['jobCount']) for card in value['batches']],
                         [('__root__', 'Default batch', 1), (batch, 'LB', 2)])

    def test_a_damaged_list_shows_its_jobs_in_the_default_batch_and_is_never_written(self):
        batch = self.make_batch('Kept')
        run_id = self.start(batch)['runId']
        listing = self.runs / server.CONTROLLER_FOLDER / named_batches.FILENAME
        for app in list(self.http.jobs.values()):
            app.add_trace({'kind': 'request', 'stage': 'solve', 'text': 'Prove.'})
            app.flush_trace(force=True)
            app._claims.release_all()
        self.stop_serving()
        listing.write_text('{not json')
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.serve()
        self.assertIn(f'Warning: The list of batches ({listing}) cannot be read: it is not valid JSON', stderr.getvalue())
        status, value = self.call('/batches')
        self.assertIn('cannot be read', value['batchesProblem'])
        self.assertFalse(value['canCreateBatches'])
        [card] = value['batches']
        self.assertEqual((card['id'], card['jobCount']), ('__root__', 1))
        for path, body in (('/batches', {'name': 'New'}), ('/batches/rename?batch=__root__', {'name': 'New'})):
            status, value = self.call(path, body)
            self.assertEqual(status, 400)
        self.assertEqual(listing.read_text(), '{not json')
        self.assertEqual(self.settings(run_id)['batch'], batch)
        # A link that still names the batch reaches the job in the default batch, where it is shown.
        status, value = self.call('/state?' + urlencode({'batch': batch, 'job': run_id}))
        self.assertEqual((status, value['batchId']), (200, '__root__'))
        app = self.http.jobs[run_id]
        with patch.object(app, 'pause') as pause:
            status, value = self.call('/pause?' + urlencode({'batch': batch, 'job': run_id}), {})
        self.assertEqual((status, value['batchId']), (200, '__root__'))
        pause.assert_called_once_with()
        # A new problem cannot join it: it is gone until the list is repaired.
        status, value = self.call('/start-workflow?' + urlencode({'batch': batch}),
                                  {'content': 'S.', 'workflowStages': AUTHOR_ONLY})
        self.assertEqual((status, value['error']), (403, 'That batch no longer exists.'))
        # Saving the job's settings keeps its batch on disk.
        app._save_job_settings(app.state)
        self.assertEqual(self.settings(run_id)['batch'], batch)

    def test_saved_and_unknown_batches_take_no_new_problem(self):
        saved = self.runs / 'alpha' / 'old-job'
        saved.mkdir(parents=True)
        (saved / 'draft.md').write_text('# Draft problem\n\nOld.\n')
        for path in ('/start-workflow', '/direct', '/review', '/finalize', '/direct-batch'):
            for batch, error in (('alpha', 'Saved batches are read-only.'),
                                 ('batch_0123456789', 'That batch no longer exists.')):
                with self.subTest(path=path, batch=batch):
                    status, value = self.call(path + '?' + urlencode({'batch': batch}), {'content': 'S.'})
                    self.assertEqual((status, value['error']), (403, error))
        self.assertEqual((self.http.jobs, self.launches), ({}, []))

    def test_jobs_made_from_another_job_join_its_batch(self):
        batch = self.make_batch('Graph coloring')
        source = self.http.jobs[self.start(batch)['runId']]
        self.idle(source.state['runId'])
        seen = []

        def record(app, *args, **kwargs):
            seen.append(app.batch_id)
            raise RuntimeError('Recorded.')

        with patch.object(server.App, 'start_critic_resume', record), \
                patch.object(server, 'saved_critic_source', lambda run: {
                    'run_dir': source.run_dir, 'statement': 'S.', 'solution': 'P.', 'author_prompt': '',
                    'critic_prompt': '', 'convention_prompt': '', 'final_prompt': ''}), \
                patch.object(server, 'saved_research_source', lambda run: {
                    'run_dir': source.run_dir, 'settings': {'workflowStages': {**AUTHOR_ONLY, 'critic': True}},
                    'statement': 'S.'}), \
                patch.object(self.http, '_check_author_workspace_idle'):
            with self.assertRaisesRegex(RuntimeError, 'Recorded.'):
                self.http.start_saved_critic_job(source.run_dir)
        with patch.object(server.App, 'start_direct_statement', record), \
                patch.object(server.Server, '_stopped_continuation_plan',
                             staticmethod(lambda app, state=None: {'action': 'author', 'label': 'Continue'})), \
                patch.object(self.http, '_check_author_workspace_idle'), \
                patch.object(server, 'saved_statement', lambda run: 'S.'):
            with self.assertRaisesRegex(RuntimeError, 'Recorded.'):
                self.http._continue_stopped_job_locked(source)
        self.assertEqual(seen, [batch, batch])
        # A source run that is not one of this server's jobs keeps a listed batch it names, and only that.
        self.assertEqual(self.http._run_batch(source.run_dir), batch)
        elsewhere = self.base / 'elsewhere' / 'run'
        elsewhere.mkdir(parents=True)
        (elsewhere / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({'batch': 'batch_0123456789'}))
        self.assertEqual(self.http._new_app(self.http._run_batch(elsewhere)).batch_id, '')
        self.assertEqual(self.http._new_app(self.http._run_batch(elsewhere), from_job=True).batch_id, '')

    def damage_the_list(self):
        """Restart with the list of batches damaged; returns how to repair it."""
        listing = self.runs / server.CONTROLLER_FOLDER / named_batches.FILENAME
        kept = listing.read_bytes()
        self.restart(meanwhile=lambda: listing.write_text('{not json'))
        self.assertTrue(self.http.named_batches.problem)
        return lambda: listing.write_bytes(kept)

    def test_a_job_made_from_another_while_the_list_cannot_be_read_stays_in_its_batch(self):
        batch = self.make_batch('Graph coloring')
        status, state = self.call('/start-workflow?' + urlencode({'batch': batch}), {
            'content': 'Every graph has property P.', 'workflowStages': {**AUTHOR_ONLY, 'critic': True}})
        self.assertEqual(status, 200, state)
        source_id = state['runId']
        self.http.jobs[source_id]._save(runtime.SAVED_CANDIDATE_FILENAME, 'A complete proof.\n')
        self.idle(source_id)
        repair = self.damage_the_list()
        # Resume at critic from the page, which shows the job in the default batch meanwhile.
        status, value = self.call('/resume-critic?' + urlencode({'batch': '__root__', 'job': source_id}), {})
        self.assertEqual(status, 200, value)
        continued_id = value['runId']
        self.assertNotEqual(continued_id, source_id)
        self.assertEqual(value['batchId'], '__root__')
        # Both keep the batch on disk, as the list's own problems do until it is repaired.
        self.assertEqual((self.settings(source_id)['batch'], self.settings(continued_id)['batch']), (batch, batch))
        status, value = self.call('/jobs?batch=__root__')
        self.assertEqual(sorted(row['runId'] for row in value['jobs']), sorted([source_id, continued_id]))
        # Repaired and started again: both are in the batch, the continuation included.
        self.restart(meanwhile=repair)
        self.assertEqual(self.http.named_batches.problem, '')
        status, value = self.call('/jobs?' + urlencode({'batch': batch}))
        self.assertEqual(sorted(row['runId'] for row in value['jobs']), sorted([source_id, continued_id]))
        status, value = self.call('/batches')
        self.assertEqual([(card['id'], card['jobCount']) for card in value['batches']], [(batch, 2)])

    def test_every_job_made_from_another_keeps_its_batch_while_the_list_cannot_be_read(self):
        batch = self.make_batch('Graph coloring')
        source_id = self.start(batch)['runId']
        self.idle(source_id)
        self.damage_the_list()
        source = self.http.jobs[source_id]
        self.assertEqual((source.batch_id, self.http.batch_of(source)), (batch, '__root__'))
        seen = []

        def record(app, *args, **kwargs):
            seen.append(app.batch_id)
            raise RuntimeError('Recorded.')

        sources = (patch.object(server, 'saved_critic_source', lambda run: {
                       'run_dir': source.run_dir, 'statement': 'S.', 'solution': 'P.', 'author_prompt': '',
                       'critic_prompt': '', 'convention_prompt': '', 'final_prompt': ''}),
                   patch.object(server, 'saved_research_source', lambda run: {
                       'run_dir': source.run_dir, 'settings': {'workflowStages': {**AUTHOR_ONLY, 'critic': True}},
                       'statement': 'S.'}),
                   patch.object(self.http, '_check_author_workspace_idle'))
        for change in sources:
            change.start()
            self.addCleanup(change.stop)
        # Resume at critic and a checkpoint (a new critic job), and a terminal resume of saved author work.
        with patch.object(server.App, 'start_critic_resume', record), self.assertRaisesRegex(RuntimeError, 'Recorded.'):
            self.http.start_saved_critic_job(source.run_dir)
        with patch.object(server.App, 'start_direct_statement', record), self.assertRaisesRegex(RuntimeError, 'Recorded.'):
            self.http.start_saved_research_job(source.run_dir)
        # A stopped job continued as a new job.
        with patch.object(server.App, 'start_direct_statement', record), \
                patch.object(server.Server, '_stopped_continuation_plan',
                             staticmethod(lambda app, state=None: {'action': 'author', 'label': 'Continue'})), \
                patch.object(server, 'saved_statement', lambda run: 'S.'), \
                self.assertRaisesRegex(RuntimeError, 'Recorded.'):
            self.http._continue_stopped_job_locked(source)
        self.assertEqual(seen, [batch, batch, batch])
        # A new problem cannot join a batch that is not listed, and an ID that is not a batch's is never kept.
        self.assertEqual(self.http._new_app(batch).batch_id, '')
        self.assertEqual(self.http._new_app('alpha', from_job=True).batch_id, '')

    def test_a_job_in_a_batch_stops_and_continues_in_place_there(self):
        # Stop works like the time limit in every batch (round 4): the job shows Stopped, counted apart on its batch's
        # card like a job at its time limit, and Continue picks it up in the same job, which stays in its batch.
        from test_stop_in_place import AUTHOR_PAUSED, STOPPED
        from test_time_limit_conversation import FakeRunner
        watched = []
        for change in (patch.object(server.App, '_watch_author_pause', autospec=True,
                                    side_effect=lambda app, process, token: watched.append(token)),
                       patch.object(server, 'stop_process_tree')):
            change.start()
            self.addCleanup(change.stop)
        batch = self.make_batch('Graph coloring')
        run_id, limited_id = self.start(batch)['runId'], self.start(batch, 'Every tree is bipartite.')['runId']
        # The other job reaches its time limit at the author.
        limited = self.http.jobs[limited_id]
        now = server.datetime.now(server.timezone.utc).isoformat()
        limited.state.update(thinkingHours=1, elapsedSeconds=7200, resumedAt=now, startedAt=now)
        limited._read_output(FakeRunner([{**AUTHOR_PAUSED, 'reason': 'Workflow time limit reached.'}]),
                             limited.active_token)
        query = lambda batch_id: '?' + urlencode({'job': run_id, 'batch': batch_id})  # noqa: E731
        # Another batch's page cannot stop it.
        self.assertEqual(self.call('/stop' + query('__root__'), {}), (404, {'error': 'That job is not in this batch.'}))
        status, value = self.call('/stop' + query(batch), {})
        self.assertEqual(status, 200, value)
        self.assertEqual((value['phase'], value['pauseCause'], value['batchId']), ('pausing', 'stopped', batch))
        self.assertEqual(len(watched), 1)
        app = self.http.jobs[run_id]
        app._read_output(FakeRunner([STOPPED, AUTHOR_PAUSED]), app.active_token)
        status, value = self.call('/jobs?' + urlencode({'batch': batch}))
        rows = {row['runId']: row for row in value['jobs']}
        self.assertEqual({key: rows[run_id][key] for key in ('phase', 'pauseCause', 'batchId', 'continueStoppedLabel')},
                         {'phase': 'paused', 'pauseCause': 'stopped', 'batchId': batch, 'continueStoppedLabel': 'Continue'})
        self.assertEqual((rows[limited_id]['phase'], rows[limited_id]['pauseCause']), ('paused', 'time-limit'))
        # The batch's card (and so the sidebar and the Overview's totals) counts each apart from paused jobs.
        status, value = self.call('/batches')
        self.assertEqual([(card['id'], card['jobCount'], card['counts']) for card in value['batches']],
                         [(batch, 2, {'stopped': 1, 'limit': 1})])
        # Continue: the same job and run folder reopen the saved conversation, in the same batch.
        self.assertEqual(self.call('/continue-stopped' + query('__root__'), {}),
                         (404, {'error': 'That job is not in this batch.'}))
        launches = len(self.launches)
        status, value = self.call('/continue-stopped' + query(batch), {})
        self.assertEqual(status, 200, value)
        self.assertEqual((value['runId'], value['batchId'], value['phase']), (run_id, batch, 'running'))
        self.assertEqual(len(self.launches), launches + 1)
        argv, cwd = self.launches[-1]
        self.assertIn('goal_require_resume=true', argv)
        self.assertEqual(argv[argv.index('--start-node') + 1], 'author')
        self.assertEqual(self.settings(run_id)['batch'], batch)
        status, value = self.call('/batches')
        self.assertEqual([(card['id'], card['jobCount'], card['counts']) for card in value['batches']],
                         [(batch, 2, {'running': 1, 'limit': 1})])

    def test_a_job_made_from_one_an_earlier_version_stopped_joins_its_batch_with_the_current_author_prompt(self):
        # A job an earlier version stopped ended at the stop: Continue proof author makes a new job (the older job
        # may already have such a continuation). It joins the stopped job's batch, and the earlier default author
        # prompt it was saved with continues as the current default (round 4's author instructions).
        from test_author_prompts import EARLIER_DEFAULT_AUTHOR_PROMPT
        current = server.default_prompts()['author']
        batch = self.make_batch('Graph coloring')
        run_id = self.start(batch)['runId']
        source = self.http.jobs[run_id]
        for path in (source.run_dir / 'prompts/author.txt',
                     server.controller_directory(source.run_dir) / 'prompts/author.txt'):
            path.write_text(EARLIER_DEFAULT_AUTHOR_PROMPT + '\n')
        # The stop's own records cannot be written, so it ends the job as earlier versions did.
        with patch.object(server.App, '_save_pause', side_effect=OSError('disk full')), \
                patch.object(server, 'stop_process_tree'):
            source.stop()
        self.assertTrue(source.state['manuallyStopped'])
        # TCS Prover starts again (the update): the job is read back from its files.
        for app in list(self.http.jobs.values()):
            app.flush_trace(force=True)
            app._claims.release_all()
        self.stop_serving()
        self.serve()
        restored = self.http.jobs[run_id]
        self.assertEqual((restored.state['phase'], restored.state['authorPrompt'], self.http.batch_of(restored)),
                         ('done', current, batch))
        status, value = self.call('/jobs?' + urlencode({'batch': batch}))
        [row] = value['jobs']
        self.assertEqual((row['continueStoppedLabel'], row['continueStoppedInPlace']), ('Continue proof author', False))
        status, value = self.call('/batches')
        self.assertEqual([(card['id'], card['counts']) for card in value['batches']], [(batch, {'stopped': 1})])
        launches = len(self.launches)
        status, value = self.call('/continue-stopped?' + urlencode({'job': run_id, 'batch': batch}), {})
        self.assertEqual(status, 200, value)
        continued_id = value['runId']
        self.assertNotEqual(continued_id, run_id)
        self.assertEqual((value['batchId'], self.settings(continued_id)['batch']), (batch, batch))
        self.assertEqual(len(self.launches), launches + 1)
        argv, cwd = self.launches[-1]
        self.assertEqual(Path(argv[argv.index('--author-prompt-file') + 1]).read_text(), current + '\n')
        continued = self.http.jobs[continued_id]
        self.assertEqual((continued.state['authorPrompt'], continued.state['goalWorkspace']),
                         (current, str(source.run_dir.resolve())))
        status, value = self.call('/batches')
        self.assertEqual([(card['id'], card['jobCount'], card['counts']) for card in value['batches']],
                         [(batch, 2, {'running': 1, 'stopped': 1})])

    def test_batch_routes_need_the_launch_token_host_and_origin(self):
        batch = self.make_batch('Kept')
        for path in ('/batches', '/batches/rename?' + urlencode({'batch': batch}),
                     '/batches/delete?' + urlencode({'batch': batch})):
            for headers in ({'X-TCS-Prover-Token': ''}, {'X-TCS-Prover-Token': 'wrong'},
                            {'Origin': 'https://example.com'}, {'Host': 'evil.test'}):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.call(path, {'name': 'Changed'}, headers=headers)[0], 403)
        saved = json.loads((self.runs / server.CONTROLLER_FOLDER / named_batches.FILENAME).read_text())
        self.assertEqual([(row['id'], row['name']) for row in saved['batches']], [(batch, 'Kept')])


class SavedRunSourceTests(unittest.TestCase):
    """Run settings of a read-only saved job and of a restored paused job name each role's source."""

    KEY = 'sk-tcs-batch-route-test-0123456789'
    PLANTED = 'sk-ant-planted-in-job-settings-0123'
    request = BatchRouteTests.request
    stop = BatchRouteTests.stop

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root = base / 'runs'
        # Nothing here may start a runner, a CLI or a model session.
        for change in (patch.dict(os.environ, {providers.STORE_ENV: str(base / 'state' / 'api-models.json')}),
                       *(patch.object(target, name, side_effect=AssertionError(name + ' reached'))
                         for target, name in ((server.App, '_spawn_worker'), (server.subprocess, 'Popen'),
                                              (runtime, 'goal_session'), (runtime, 'author_conversation'),
                                              (claude_cli, 'goal_session'), (claude_cli, 'author_conversation')))):
            change.start()
            self.addCleanup(change.stop)
        # The connection as it is now (another host than the jobs recorded); the Claude one was removed.
        self.sol = providers.save_api_model({'format': 'openai', 'baseUrl': 'https://new-gateway.example/v1',
                                             'model': 'gpt-6.1-sol', 'apiKey': self.KEY,
                                             'label': 'Sol via lab gateway'})
        settings = {
            'skipStatementReview': False, 'reviewModel': 'api:claude-gateway', 'authorModel': 'claude-opus-5-5',
            'criticModel': self.sol['id'], 'writerModel': 'gpt-5.6-sol', 'elapsedSeconds': 12,
            # The author's workspace can write this record: a key planted in it never reaches the page.
            'apiModels': {
                'api:claude-gateway': {'label': 'Claude gateway', 'format': 'anthropic',
                                       'baseUrl': 'https://claude.example.com', 'headerName': 'x-api-key',
                                       'model': 'claude-gw', 'apiKey': self.PLANTED},
                self.sol['id']: {'label': 'Sol via lab gateway', 'format': 'openai',
                                 'baseUrl': 'https://gateway.lab.example/v1', 'headerName': 'Authorization',
                                 'model': 'gpt-6.1-sol'}}}
        for batch, run_id in (('alpha', 'saved-job'), ('__root__', 'restored-job')):
            folder = self.root / batch / run_id if batch != '__root__' else self.root / run_id
            folder.mkdir(parents=True)
            (folder / 'job-settings.json').write_text(json.dumps(settings))
            (folder / 'draft.md').write_text('# Draft problem\n\nStatement ' + run_id)
            (folder / 'checked-statement.md').write_text('# Checked statement\n\nStatement ' + run_id)
            (folder / 'transcript.jsonl').write_text(json.dumps(
                {'kind': 'status', 'time': '2026-01-01T00:00:00Z', 'stage': 'solve', 'node': 'author',
                 'label': 'Goal started', 'threadId': 'saved-thread'}) + '\n')
            (folder / 'pause.json').write_text(json.dumps({'status': 'paused', 'stage': 'solve', 'node': 'author',
                                                           'elapsedSeconds': 12, 'pauseCause': 'user',
                                                           'goalThreadId': 'saved-thread'}))
        self.http = server.Server((server.HOST, 0), runs=self.root)
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def test_read_only_saved_and_restored_paused_jobs_name_each_roles_source(self):
        for query, read_only in (({'batch': 'alpha', 'job': 'saved-job'}, True), ({'job': 'restored-job'}, False)):
            with self.subTest(**query):
                status, state = self.request('/state?' + urlencode(query))
                self.assertEqual(status, 200)
                self.assertEqual((state['phase'], bool(state.get('readOnly'))), ('paused', read_only))
                sources = state['modelSources']
                self.assertEqual(sources['review'], {
                    'model': 'api:claude-gateway', 'kind': 'api', 'configured': False, 'label': 'claude-gw',
                    'wireModel': 'claude-gw', 'connection': 'Claude gateway', 'format': 'anthropic',
                    'formatLabel': 'Anthropic format', 'host': 'claude.example.com', 'runsThrough': 'claude',
                    'runsThroughLabel': 'Claude Code'})
                # One name for the Claude program in the panel: the source menu's.
                self.assertEqual({key: sources['author'][key] for key in ('kind', 'label', 'source', 'runsThroughLabel')},
                                 {'kind': 'claude', 'label': 'Claude Opus 5.5', 'source': 'Claude Code',
                                  'runsThroughLabel': 'Claude Code'})
                self.assertEqual({key: sources['critic'].get(key) for key in ('configured', 'host', 'recorded')},
                                 {'configured': True, 'host': 'new-gateway.example',
                                  'recorded': {'format': 'openai', 'formatLabel': 'OpenAI format',
                                               'host': 'gateway.lab.example', 'wireModel': 'gpt-6.1-sol'}})
                self.assertEqual((sources['writer']['kind'], sources['writer']['older']), ('codex', True))
                text = json.dumps(state)
                self.assertNotIn(self.KEY, text)
                self.assertNotIn(self.PLANTED, text)


if __name__ == '__main__':
    unittest.main()
