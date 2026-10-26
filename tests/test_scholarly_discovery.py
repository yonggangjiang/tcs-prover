"""Offline API-discovery contract tests; source bytes require the broker gate."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from retrieval import historical_search as hs
from retrieval import scholarly_discovery as sd

CUTOFF = datetime(2026, 10, 2, 14, 13, 12, tzinfo=timezone.utc)
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/2102.08260v9</id>
<title>FUTURE SECRET TITLE</title><summary>NEW SOLUTION</summary>
<updated>2099-01-01T00:00:00Z</updated><link href="https://evil.example/future"/></entry>
</feed>'''
CROSSREF = {'message': {'items': [{
    'URL': 'https://doi.org/10.1000/example',
    'title': ['FUTURE SECRET TITLE'], 'abstract': 'NEW SOLUTION', 'score': 999,
    'published': {'date-parts': [[2099]]},
    'resource': {'primary': {'URL': 'https://publisher.example/paper'}},
    'link': [{'URL': 'https://publisher.example/paper.pdf', 'content-type': 'application/pdf'}],
}]}}


class FakeClient:
    def __init__(self, atom=ATOM, crossref=None):
        self.atom = atom
        self.crossref = CROSSREF if crossref is None else crossref
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if urlsplit(url).hostname == sd.ARXIV_HOST:
            if isinstance(self.atom, Exception):
                raise self.atom
            return hs.HttpResponse(self.atom, {'content-type': 'application/atom+xml'}, url)
        if urlsplit(url).hostname == sd.CROSSREF_HOST:
            if isinstance(self.crossref, Exception):
                raise self.crossref
            return hs.HttpResponse(json.dumps(self.crossref).encode(), {'content-type': 'application/json'}, url)
        raise AssertionError('No other endpoint or live source may be requested')


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Pacing itself is tested with a fake clock below; API contract tests do
        # not sleep or use the network.
        self.addCleanup(patch.stopall)
        patch.object(sd._ARXIV_PACER, 'get', side_effect=lambda client, url: client.get(url)).start()
        patch.object(sd._CROSSREF_PACER, 'get', side_effect=lambda client, url: client.get(url)).start()

    def discover(self, client=None, count=10, query='Euler characteristic'):
        return sd.discover(query, CUTOFF, count, client or FakeClient(), hs.HistoricalSearchError, hs.canonical_url)

    def test_returns_only_urls_and_never_current_titles_dates_summaries_or_scores(self):
        client = FakeClient()
        result = self.discover(client)
        self.assertEqual(result, ['https://arxiv.org/abs/2102.08260', 'https://arxiv.org/pdf/2102.08260',
                                  'https://publisher.example/paper', 'https://publisher.example/paper.pdf'])
        for marker in ('FUTURE', 'SECRET', 'SOLUTION', '2099', '999', 'evil.example'):
            self.assertNotIn(marker, json.dumps(result))
        self.assertEqual({urlsplit(url).hostname for url in client.calls}, {sd.ARXIV_HOST, sd.CROSSREF_HOST})

    def test_dates_are_fixed_discovery_hints_and_query_is_encoded_as_text(self):
        client = FakeClient()
        self.discover(client, query='Euler OR characteristic&api_key=attack')
        arxiv = parse_qs(urlsplit(client.calls[0]).query)
        crossref = parse_qs(urlsplit(client.calls[1]).query)
        self.assertIn('submittedDate:[199101010000 TO 202610021413]', arxiv['search_query'][0])
        self.assertNotIn('api_key', arxiv)
        self.assertNotIn('api_key', crossref)
        self.assertEqual(crossref['filter'], ['until-pub-date:2026-10-02'])
        self.assertEqual(crossref['select'], ['URL,link,resource'])

    def test_malformed_and_private_source_links_are_filtered_by_broker_guard(self):
        for url in ('http://127.0.0.1/secret', 'file:///secret', 'https://user:pass@public.example/a',
                    'https://web.archive.org/web/20990101000000/https://example.org/',
                    'https://public.example:444/a', 'https://public.example/%0Aheader'):
            with self.subTest(url=url):
                crossref = {'message': {'items': [{'resource': {'primary': {'URL': url}}}]}}
                result = self.discover(FakeClient(atom=b'<feed xmlns="http://www.w3.org/2005/Atom"/>', crossref=crossref))
                self.assertEqual(result, [])

    def test_arxiv_identity_rejects_fake_hosts_ports_paths_and_encoded_identifiers(self):
        for identity in ('https://evil.example/abs/2102.08260', 'https://arxiv.org:444/abs/2102.08260',
                         'https://user:pass@arxiv.org/abs/2102.08260', 'https://arxiv.org/abs/../secret',
                         'https://arxiv.org/abs/2102.08260%0a', 'https://arxiv.org/api/errors#bad'):
            with self.subTest(identity=identity):
                atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>' + identity + '</id></entry></feed>').encode()
                self.assertEqual(self.discover(FakeClient(atom=atom, crossref={'message': {'items': []}})), [])

    def test_xml_entities_malformed_json_and_oversized_responses_fail_closed(self):
        for body in (b'<!DOCTYPE feed [<!ENTITY secret "bad">]><feed/>', b'<feed',
                     '<!DOCTYPE feed [<!ENTITY secret "bad">]><feed/>'.encode('utf-16'),
                     b'x' * (sd.MAX_DISCOVERY_BYTES + 1), b'<unexpected/>'):
            with self.subTest(body=body[:40]), self.assertRaises(hs.HistoricalSearchError):
                self.discover(FakeClient(atom=body, crossref={'message': {'items': 'bad'}}))

    def test_one_api_failure_retains_only_other_explicit_provider_results(self):
        result = self.discover(FakeClient(atom=hs.HistoricalSearchError('Unavailable')))
        self.assertEqual(result, ['https://publisher.example/paper', 'https://publisher.example/paper.pdf'])
        result = self.discover(FakeClient(crossref=hs.HistoricalSearchError('Unavailable')))
        self.assertEqual(result, ['https://arxiv.org/abs/2102.08260', 'https://arxiv.org/pdf/2102.08260'])

    def test_both_api_failures_return_static_error_without_provider_text(self):
        with self.assertRaisesRegex(hs.HistoricalSearchError, '^Independent scholarly discovery unavailable; no live fallback is permitted.$'):
            self.discover(FakeClient(atom=hs.HistoricalSearchError('SECRET'), crossref=hs.HistoricalSearchError('FUTURE ANSWER')))

    def test_empty_success_cannot_hide_other_provider_failure(self):
        client = FakeClient(atom=b'<feed xmlns="http://www.w3.org/2005/Atom"/>',
                            crossref=hs.HistoricalSearchError('SECRET'))
        with self.assertRaises(hs.HistoricalSearchError) as caught:
            self.discover(client)
        self.assertTrue(caught.exception.operational)
        self.assertEqual(caught.exception.code, 'discovery_unavailable')

    def test_partial_failure_diagnostics_do_not_include_response_or_query_text(self):
        failures = []
        result = sd.discover('Euler characteristic', CUTOFF, 10,
                             FakeClient(atom=hs.HistoricalSearchError('SECRET FUTURE ANSWER')),
                             hs.HistoricalSearchError, hs.canonical_url, on_failure=failures.append)
        self.assertEqual(result, ['https://publisher.example/paper', 'https://publisher.example/paper.pdf'])
        self.assertEqual(len(failures), 1)
        self.assertEqual(set(failures[0]), {'host', 'code', 'status'})
        self.assertEqual(failures[0]['host'], sd.ARXIV_HOST)
        for marker in ('SECRET', 'FUTURE', 'ANSWER', 'Euler'):
            self.assertNotIn(marker, json.dumps(failures))

    def test_audit_failure_or_exhausted_deadline_aborts_without_another_api(self):
        for code in ('audit_failure', 'deadline_exceeded'):
            with self.subTest(code=code):
                error = hs.HistoricalSearchError('Retrieval stopped.', code=code, operational=True)
                client = FakeClient(atom=error)
                failures = []
                with self.assertRaises(hs.HistoricalSearchError) as caught:
                    sd.discover('Euler characteristic', CUTOFF, 10, client,
                                hs.HistoricalSearchError, hs.canonical_url, on_failure=failures.append)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(failures, [])

    def test_limits_are_deterministic_sorted_and_include_both_providers(self):
        result = self.discover(count=2)
        self.assertEqual(result, ['https://arxiv.org/abs/2102.08260', 'https://publisher.example/paper'])
        self.assertEqual(result, sorted(set(result)))
        for count in (0, 21, True):
            with self.subTest(count=count), self.assertRaises(hs.HistoricalSearchError):
                self.discover(count=count)

    def test_response_redirects_status_and_mime_mismatch_never_become_candidates(self):
        for modified in (dict(status=302), dict(url='https://evil.example/'), dict(headers={'content-type': 'text/html'})):
            client = FakeClient()
            original = client.get
            with patch.object(client, 'get', side_effect=lambda url: replace(original(url), **modified)):
                with self.assertRaises(hs.HistoricalSearchError):
                    self.discover(client)


class PacingTests(unittest.TestCase):
    def test_requests_and_failures_are_spaced_without_real_sleep(self):
        now = [0.0]
        starts = []

        def sleep(delay):
            now[0] += delay

        class Client:
            def get(self, url):
                starts.append(now[0])
                if len(starts) == 2:
                    raise OSError('temporary network failure')
                return url

        pacer = sd.PacedRequests(3.0, monotonic=lambda: now[0], sleep=sleep)
        self.assertEqual(pacer.get(Client(), 'first'), 'first')
        with self.assertRaises(OSError):
            pacer.get(Client(), 'second')
        self.assertEqual(pacer.get(Client(), 'third'), 'third')
        self.assertEqual(starts, [0.0, 3.0, 6.0])


if __name__ == '__main__':
    unittest.main()
