"""Search availability failures must remain distinct from valid empty evidence."""
import base64
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import historical_search as hs
import test_historical_search as fixtures
from test_historical_search import FakeClient, SOURCE, snapshot


class ReliabilityTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    engine = fixtures.EngineTests.engine

    def test_operational_cdx_failure_is_not_reported_as_empty_search(self):
        client = FakeClient()
        original = client.get

        def unavailable(url, headers=None):
            if '/cdx/' in url:
                raise hs.HistoricalSearchError('Archive unavailable.', code='http_error',
                                               status=503, operational=True)
            return original(url, headers)

        with patch.object(client, 'get', side_effect=unavailable):
            with self.assertRaisesRegex(hs.HistoricalSearchError, 'Archive services were unavailable'):
                self.engine(client).search('graph')
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'search_failed')
        rejection = next(e for e in events if e['event'] == 'capture_rejected')
        self.assertEqual(rejection['http_status'], 503)
        self.assertTrue(rejection['operational'])

    def test_operational_replay_failure_survives_without_false_no_capture(self):
        def unavailable(url):
            raise hs.HistoricalSearchError('Archive timed out.', code='transport_error', operational=True)

        engine = self.engine(FakeClient(replay=unavailable))
        with self.assertRaises(hs.HistoricalSearchError) as error:
            engine.open(SOURCE)
        self.assertEqual(error.exception.code, 'transport_error')
        self.assertFalse(engine.provider.negative_cache)

    def test_success_status_with_invalid_archive_payload_is_not_empty_success(self):
        for body in (b'not-json', b'{"message":"unavailable"}', b'[["unexpected header"]]'):
            with self.subTest(body=body):
                client = FakeClient()
                original = client.get
                with patch.object(client, 'get', side_effect=lambda url, headers=None:
                                  hs.HttpResponse(body, {}, url) if '/cdx/' in url else original(url, headers)):
                    with self.assertRaises(hs.HistoricalSearchError) as error:
                        self.engine(client).search('graph')
                self.assertEqual(error.exception.code, 'archive_unavailable')

    def test_unreadable_newest_capture_does_not_hide_older_readable_capture(self):
        newest = snapshot('20261001120000', body=b'<html><script>unextractable</script></html>')
        old = snapshot()
        result = self.engine(FakeClient(captures=[newest, old])).open(SOURCE)['result']
        self.assertEqual(result['sha256'], old.body_sha256)
        self.assertIn('Graph lemma', result['text'])

    def test_unavailable_newest_replay_does_not_hide_same_digest_older_capture(self):
        newest, old = snapshot('20261001120000'), snapshot()
        client = FakeClient(captures=[newest, old])
        original = client.get

        def unavailable_newest(url, headers=None):
            if url == newest.archive_url:
                raise hs.HistoricalSearchError('Unavailable replay.', code='http_error', status=404)
            return original(url, headers)

        with patch.object(client, 'get', side_effect=unavailable_newest):
            result = self.engine(client).open(SOURCE)['result']
        self.assertEqual(result['capture_time'], hs.utc_text(hs.capture_datetime(old.capture)))

    def scholarly_engine(self, client=None):
        return self.engine(client or FakeClient(), policy={
            'schema_version': 1, 'provider': 'scholarly_wayback',
            'rate_limit_directory': str(self.root.resolve() / 'pacing'),
            'solver_workspace': str(self.root.resolve() / 'solver'),
        })

    def test_scholarly_partial_discovery_integrates_callback_and_is_not_cached(self):
        with patch('scholarly_discovery._arxiv_urls', side_effect=hs.HistoricalSearchError(
                'Unavailable.', code='http_error', status=503, operational=True)), \
             patch('scholarly_discovery._crossref_urls', return_value=[SOURCE]):
            engine = self.scholarly_engine()
            result = engine.search('graph')
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['results'][0]['url'], SOURCE)
        self.assertFalse(engine.provider.discovery_cache)

    def test_scholarly_discovery_audit_failure_aborts_before_second_provider(self):
        client = FakeClient()
        client.audit = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('private path'))
        with patch('scholarly_discovery._arxiv_urls', side_effect=hs.HistoricalSearchError(
                'Unavailable.', code='http_error', status=503, operational=True)), \
             patch('scholarly_discovery._crossref_urls', return_value=[SOURCE]) as other:
            with self.assertRaises(hs.HistoricalSearchError) as error:
                self.scholarly_engine(client).search('graph')
        self.assertEqual(error.exception.code, 'audit_failure')
        self.assertNotIn('private path', str(error.exception))
        other.assert_not_called()

    def test_verified_capture_survives_broker_restart_without_network(self):
        first = self.scholarly_engine().open(SOURCE)['result']
        client = FakeClient()
        with patch.object(client, 'get', side_effect=AssertionError('No repeat archive fetch')):
            second = self.scholarly_engine(client).open(SOURCE)['result']
        self.assertEqual(second, first)
        events = [json.loads(line)['event'] for line in self.audit.read_text().splitlines()]
        self.assertIn('archive_cache_hit', events)

    def test_shared_cache_from_later_cutoff_never_releases_future_content(self):
        engine = self.scholarly_engine()
        future = snapshot('20261003120000')
        engine.provider.persistent_cache.put(SOURCE, {
            **future.__dict__, 'body': base64.b64encode(future.body).decode('ascii')})
        result = engine.open(SOURCE)['result']
        self.assertEqual(result['capture_time'], hs.utc_text(hs.capture_datetime(snapshot().capture)))
        self.assertNotEqual(result['capture_time'], hs.utc_text(hs.capture_datetime(future.capture)))

    def test_persistent_cache_revalidates_archive_provenance_before_output(self):
        engine = self.scholarly_engine()
        stored = snapshot()
        engine.provider.persistent_cache.put(SOURCE, {
            **stored.__dict__, 'body': base64.b64encode(stored.body).decode('ascii'),
            'memento_datetime': 'Fri, 02 Oct 2026 12:00:00 GMT'})
        with self.assertRaises(hs.HistoricalSearchError) as error:
            engine.open(SOURCE)
        self.assertEqual(error.exception.code, 'cache_invalid')

    def test_transient_cache_hit_audit_failure_aborts_entire_search(self):
        self.scholarly_engine().open(SOURCE)
        engine = self.scholarly_engine()
        original = engine.audit.path

        class TransientAuditPath:
            writes = 0

            def open(self, *args, **kwargs):
                self.writes += 1
                if self.writes == 2:  # search_requested succeeds; archive_cache_hit fails.
                    raise OSError('simulated private diagnostic')
                return original.open(*args, **kwargs)

        engine.audit.path = TransientAuditPath()
        with patch.object(engine.provider, 'candidates', return_value=[SOURCE]):
            with self.assertRaises(hs.HistoricalSearchError) as error:
                engine.search('graph')
        self.assertEqual(error.exception.code, 'audit_failure')
        self.assertNotIn('private', str(error.exception))

    def test_partial_archive_outage_returns_only_verified_results_with_warning(self):
        unavailable_source = 'https://other.example/paper'
        client = FakeClient(discoveries=[{'url': SOURCE}, {'url': unavailable_source}])
        original = client.get

        def outage(url, headers=None):
            if '/cdx/' in url and 'other.example' in url:
                raise hs.HistoricalSearchError('Unavailable.', code='http_error', status=503, operational=True)
            return original(url, headers)

        with patch.object(client, 'get', side_effect=outage):
            result = self.engine(client).search('graph')
        self.assertTrue(result['incomplete'])
        self.assertEqual([r['url'] for r in result['results']], [SOURCE])

    def test_missing_capture_is_cached_briefly_without_any_live_request(self):
        client = FakeClient(captures=[])
        engine = self.engine(client)
        with patch.object(hs.time, 'monotonic', return_value=100):
            for _ in range(2):
                with self.assertRaises(hs.HistoricalSearchError):
                    engine.open(SOURCE)
        self.assertEqual(len(client.calls), 1)
        with patch.object(hs.time, 'monotonic', return_value=161):
            with self.assertRaises(hs.HistoricalSearchError):
                engine.open(SOURCE)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual({urlsplit(url).hostname for url, _ in client.calls}, {'web.archive.org'})


if __name__ == '__main__':
    unittest.main()
