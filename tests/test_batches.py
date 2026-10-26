"""Saved batch views remain bounded, read-only, and confined to their folders."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ui import batches
from ui import server
from ui import retrieval


class BatchCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'runs'
        self.root.mkdir()
        self.original = self.make_run(self.root, 'same-job', sourceFile='P01.md')
        self.old_root = self.root / 'search-v2'
        self.old_root.mkdir()
        self.old = self.make_run(self.old_root, 'same-job', sourceFile='P02.md')
        self.current_root = self.root / 'search-v4'
        self.current_root.mkdir()
        self.current = self.make_run(self.current_root, 'current-job', preparedRun=True)
        self.catalog = batches.BatchCatalog(self.root, current_root=self.current_root)

    @staticmethod
    def make_run(root, name, **settings):
        directory = root / name
        directory.mkdir()
        (directory / 'draft.md').write_text('# Draft problem\n\nA finite theorem.\n')
        (directory / 'checked-statement.md').write_text(
            '# Statement sent directly to the proof author\n\nA finite theorem.\n'
            '\n# Statement review\n\nSkipped by the user.')
        (directory / 'job-settings.json').write_text(json.dumps(settings))
        return directory

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value))

    def test_catalog_lists_original_and_nested_batches_with_current_marker(self):
        self.write_json(self.old / 'pause.json', {'status': 'paused', 'stage': 'solve', 'node': 'author'})
        cards = self.catalog.list_batches()
        self.assertEqual(cards[0]['id'], 'search-v4')
        lookup = {row['id']: row for row in cards}
        self.assertEqual(set(lookup), {'__root__', 'search-v2', 'search-v4'})
        self.assertEqual(lookup['__root__']['name'], 'Original batch')
        self.assertEqual(lookup['search-v2']['counts']['paused'], 1)
        self.assertEqual(lookup['search-v4']['counts']['prepared'], 1)
        self.assertTrue(lookup['search-v4']['current'])
        self.assertFalse(lookup['search-v4']['readOnly'])
        self.assertEqual(lookup['search-v2']['folder'], 'runs/search-v2')
        self.assertTrue(all(row['jobCount'] == 1 for row in cards))

    def test_catalog_does_not_read_transcripts_or_instantiate_apps(self):
        for directory in (self.original, self.old, self.current):
            (directory / 'transcript.jsonl').write_text('{"kind":"status"}\n')
        with patch.object(self.catalog, '_trace', side_effect=AssertionError('trace read')):
            with patch.object(server, 'App', side_effect=AssertionError('App created')):
                self.catalog.list_batches()
                self.catalog.jobs('__root__')

    def test_duplicate_job_identifiers_stay_within_selected_batch(self):
        original = self.catalog.jobs('__root__')[0]
        historical = self.catalog.jobs('search-v2')[0]
        self.assertEqual(original['sourceFile'], 'P01.md')
        self.assertEqual(historical['sourceFile'], 'P02.md')
        self.assertEqual(historical['batchId'], 'search-v2')
        self.assertTrue(historical['readOnly'])
        with self.assertRaises(ValueError):
            self.catalog.state('search-v2', 'current-job')

    def test_selected_state_reads_saved_data_without_rewriting_pause_or_modes(self):
        self.write_json(self.old / 'pause.json', {'status': 'paused', 'stage': 'solve', 'node': 'author',
                                                'elapsedSeconds': 120, 'autoResumeAt': 0})
        self.write_json(self.old / 'job-settings.json', {'authorModel': 'saved-model', 'authorEffort': 'max',
            'skipStatementReview': True, 'retrievalMode': 'historical', 'retrievalCutoffUtc': '2026-01-01T00:00:00Z'})
        trace = [{'kind': 'status', 'stage': 'solve', 'time': '2026-01-01T01:00:00Z'},
                 {'kind': 'workflow_paused', 'stage': 'solve', 'time': '2026-01-01T02:00:00Z'}]
        (self.old / 'transcript.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in trace))
        before = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                  for path in self.old.iterdir()}
        with patch.object(server, 'App', side_effect=AssertionError('App created')):
            with patch.object(server, 'restore_saved_app', side_effect=AssertionError('App restored')):
                state = self.catalog.state('search-v2', 'same-job')
                again = self.catalog.state('search-v2', 'same-job', state['traceVersion'])
        self.assertEqual(state['phase'], 'paused')
        self.assertEqual(state['statement'], 'A finite theorem.')
        self.assertEqual(state['activeElapsedSeconds'], 120)
        self.assertEqual(state['authorModel'], 'saved-model')
        # A direct-proof job uses its own graph, and still carries the model catalog.
        self.assertEqual(state['workflow']['nodes'], server.DIRECT_GRAPH['nodes'])
        settings = state['workflow']['settings']
        self.assertTrue(set(server.OFFERED_MODELS) <= {row['id'] for row in settings['model_catalog']})
        self.assertIn('claude-opus-5-5', settings['model_efforts'])
        self.assertNotIn('model_catalog', server.DIRECT_GRAPH['settings'])
        self.assertEqual(state['retrieval']['cutoffExclusiveUtc'], '2026-01-01T00:00:00Z')
        self.assertEqual(state['autoResumeAt'], 0)
        self.assertTrue(state['readOnly'])
        self.assertFalse(state['workerActive'])
        self.assertEqual(state['trace'], trace)
        self.assertEqual(again['trace'], [])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                                  for path in self.old.iterdir()})

    def test_large_sparse_transcript_reads_recent_events_and_flags_truncation(self):
        path = self.old / 'transcript.jsonl'
        with path.open('wb') as stream:
            stream.write(b'{"kind":"status","label":"old"}\n')
            stream.seek(64 * 1024 * 1024)
            stream.write(b'\n{"kind":"status","stage":"solve","label":"recent"}\n')
        state = self.catalog.state('search-v2', 'same-job')
        self.assertEqual([row['label'] for row in state['trace']], ['recent'])
        self.assertTrue(state['traceTruncated'])
        self.assertIn('complete history', state['traceNotice'])
        response, _, _ = self.catalog.response('/transcript', 'search-v2', 'same-job')
        self.assertEqual(response, path)

    def test_updated_bounded_tail_changes_trace_version_even_at_record_limit(self):
        path = self.old / 'transcript.jsonl'
        path.write_text(''.join(json.dumps({'kind': 'status', 'label': str(i)}) + '\n' for i in range(3)))
        with patch.object(batches, 'MAX_TRACE_RECORDS', 2):
            first = self.catalog.state('search-v2', 'same-job')
            with path.open('a') as stream:
                stream.write('{"kind":"status","label":"last"}\n')
            second = self.catalog.state('search-v2', 'same-job', first['traceVersion'])
        self.assertGreater(second['traceVersion'], first['traceVersion'])
        self.assertEqual(second['traceFrom'], 0)
        self.assertEqual(second['trace'][-1]['label'], 'last')

    def test_symlink_batches_runs_and_artifacts_are_not_followed(self):
        (self.root / 'linked-batch').symlink_to(self.old_root, target_is_directory=True)
        (self.old_root / 'linked-job').symlink_to(self.original, target_is_directory=True)
        external = self.root.parent / 'secret.txt'
        external.write_text('not a run artifact')
        (self.old / 'final.tex').symlink_to(external)
        (self.old / 'PROVED.md').symlink_to(external)
        self.assertNotIn('linked-batch', {row['id'] for row in self.catalog.list_batches()})
        self.assertEqual(len(self.catalog.jobs('search-v2')), 1)
        with self.assertRaises(ValueError):
            self.catalog.artifact('search-v2', 'same-job', 'final.tex')
        self.assertEqual(self.catalog.memory('search-v2', 'same-job', 'PROVED.md')['status'], 'missing')
        with self.assertRaises(ValueError):
            batches.BatchCatalog(self.root / 'linked-batch')

    def test_root_replaced_by_symlink_after_construction_cannot_be_read(self):
        renamed = self.root.with_name('moved-runs')
        self.root.rename(renamed)
        self.root.symlink_to(renamed, target_is_directory=True)
        self.assertEqual(self.catalog.list_batches(), [])
        with self.assertRaises(ValueError):
            self.catalog.state('search-v2', 'same-job')

    def test_download_open_rejects_symlink_swapped_after_validation(self):
        path = self.old / 'final.tex'
        path.write_text('saved output')
        approved = self.catalog.artifact('search-v2', 'same-job', 'final.tex')
        path.unlink()
        path.symlink_to(self.original / 'draft.md')
        with self.assertRaises((ValueError, OSError)):
            with self.catalog.open_artifact('search-v2', 'same-job', approved.name):
                self.fail('Followed a swapped symlink')

    def test_traversal_reserved_and_unrelated_folders_are_rejected(self):
        (self.root / 'unrelated').mkdir()
        (self.root / '__root__').mkdir()
        self.make_run(self.root / '__root__', 'hidden')
        for batch_id in ('../', '/tmp', 'unrelated', 'search-v2/..'):
            with self.subTest(batch=batch_id), self.assertRaises(ValueError):
                self.catalog.jobs(batch_id)
        self.assertEqual(len(self.catalog.list_batches()), 3)
        for job_id in ('../same-job', '..', '/tmp', 'other\\job', ''):
            with self.subTest(job=job_id), self.assertRaises(ValueError):
                self.catalog.state('search-v2', job_id)
        with self.assertRaises(ValueError):
            self.catalog.memory('search-v2', 'same-job', '../draft.md')

    def test_memory_and_approach_graph_read_only_allowlist(self):
        folder = self.old / 'APPROACHES'
        folder.mkdir()
        (folder / 'index.md').write_text('# Saved approaches')
        (folder / 'A001-first.md').write_text('# A001 First plan\nStatus: ACTIVE\n\n## Context and objective\nTry this.')
        (folder / 'secret.txt').write_text('Not a displayed memory artifact')
        first = self.catalog.memory('search-v2', 'same-job', 'APPROACHES')
        self.assertEqual(first['name'], 'APPROACHES/index.md')
        again = self.catalog.memory('search-v2', 'same-job', first['name'], first['version'])
        self.assertTrue(again['unchanged'])
        graph = self.catalog.approach_graph('search-v2', 'same-job')
        self.assertEqual(graph['indexFile'], 'APPROACHES/index.md')
        self.assertEqual([row['id'] for row in graph['nodes']], ['A001'])
        self.assertEqual(graph['nodes'][0]['title'], 'First plan')
        self.assertEqual(len(graph['files']), 2)

    def test_responses_downloads_and_invalid_requests(self):
        (self.old / 'final.tex').write_text('saved TeX')
        (self.old / 'final.pdf').write_bytes(b'%PDF-test')
        value, kind, headers = self.catalog.response('/download-pdf', 'search-v2', 'same-job')
        self.assertEqual(kind, 'application/pdf')
        self.assertIn('final.pdf', headers['Content-Disposition'])
        with self.catalog.open_artifact('search-v2', 'same-job', value.name) as stream:
            self.assertEqual(stream.read(), b'%PDF-test')
        value, kind, headers = self.catalog.response('/jobs', 'search-v2')
        self.assertEqual(kind, 'application/json')
        self.assertEqual(value['jobs'][0]['resultKind'], 'final')
        for path, query in (('/missing', {}), ('/state', {'after': 'wrong'})):
            with self.assertRaises(ValueError):
                self.catalog.response(path, 'search-v2', 'same-job', query)

    def test_missing_root_and_corrupt_artifacts_fail_without_side_effects(self):
        missing = self.root / 'not-created'
        self.assertEqual(batches.BatchCatalog(missing).list_batches(), [])
        self.assertFalse(missing.exists())
        (self.old / 'job-settings.json').write_text('[]')
        (self.old / 'pause.json').write_text('not JSON')
        (self.old / 'transcript.jsonl').write_text('bad JSON\n[]\n{"kind":[],"node":{}}\n')
        state = self.catalog.state('search-v2', 'same-job')
        self.assertTrue(state['readOnly'])
        self.assertEqual(state['phase'], 'done')

    def test_legacy_path_only_retrieval_policy_keeps_fixed_cutoff_visible(self):
        cutoff = '2026-01-01T00:00:00Z'
        (self.old / 'input.md').write_text('A finite theorem.')
        policy_path = retrieval.prepare_policy(self.old, cutoff, controller_root=self.root.parent / 'controller')
        policy = json.loads(Path(policy_path).read_text())
        events = [{'event': 'search_requested', 'cutoff': cutoff, 'query': 'PRIVATE QUERY'},
                  {'event': 'search_completed', 'cutoff': cutoff, 'url': 'https://private.example/'}]
        Path(policy['broker']['auditLog']).write_text(''.join(json.dumps(row) + '\n' for row in events))
        self.write_json(self.old / 'job-settings.json', {'retrievalPolicyPath': policy_path})
        job = self.catalog.jobs('search-v2')[0]
        self.assertEqual(job['retrievalMode'], 'historical')
        self.assertEqual(job['retrievalCutoffUtc'], cutoff)
        state = self.catalog.state('search-v2', 'same-job')
        self.assertEqual(state['retrieval']['mode'], 'historical')
        self.assertEqual(state['retrieval']['cutoffExclusiveUtc'], cutoff)
        self.assertTrue(state['retrieval']['fixed'])
        self.assertEqual(state['retrieval']['counts']['searches'], 1)
        self.assertNotIn('PRIVATE QUERY', json.dumps(state))
        self.assertNotIn('private.example', json.dumps(state))
        self.assertEqual(state['retrievalPolicyPath'], '')

    def test_saved_main_jobs_default_to_live_and_explicit_disabled_stays_disabled(self):
        for settings, expected, cutoff in (({}, 'live', ''),
                ({'retrievalMode': 'live'}, 'live', ''),
                ({'retrievalMode': 'disabled'}, 'disabled', '-1')):
            with self.subTest(settings=settings):
                self.write_json(self.old / 'job-settings.json', settings)
                job = self.catalog.jobs('search-v2')[0]
                state = self.catalog.state('search-v2', 'same-job')
                for entry in (job, state):
                    self.assertEqual(entry['retrievalMode'], expected)
                    self.assertEqual(entry['retrieval']['mode'], expected)
                    self.assertEqual(entry['retrievalCutoffUtc'], cutoff)
                    self.assertEqual(entry['retrieval']['cutoffExclusiveUtc'], cutoff)


if __name__ == '__main__':
    unittest.main()
