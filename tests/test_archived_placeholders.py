"""Authenticated archive placeholders cannot hide older readable evidence."""
import base64
from dataclasses import replace
import unittest
from unittest.mock import patch

from retrieval import historical_search as hs
import test_historical_search as fixtures
from test_historical_search import FakeClient, SOURCE, snapshot


CAPTCHA = (b'<html><head><title>arXiv reCAPTCHA</title></head><body>'
           b'We gratefully acknowledge support from the Simons Foundation and member institutions. '
           b'<form><div class="g-recaptcha" data-sitekey="example"></div></form>'
           b'Web Accessibility Assistance</body></html>')
REDIRECT = (b'<html><head><title>Redirecting</title>'
            b'<meta http-equiv="REFRESH" content="2; url=https://example.org/live"/>'
            b'</head><body><script>location.href="https://example.org/live"</script></body></html>')


class PlaceholderTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    engine = fixtures.EngineTests.engine

    def test_newest_placeholder_uses_older_readable_capture_without_following_actions(self):
        for body in (CAPTCHA, REDIRECT):
            with self.subTest(body=body[:60]):
                newest, older = snapshot('20261001120000', body=body), snapshot()
                client = FakeClient(captures=[newest, older])
                result = self.engine(client).open(SOURCE)['result']
                self.assertEqual(result['sha256'], older.body_sha256)
                self.assertEqual([url for url, _ in client.calls][1:], [newest.archive_url, older.archive_url])

    def test_search_and_direct_snapshot_output_never_release_matching_placeholder(self):
        for body, query in ((CAPTCHA, 'arxiv recaptcha'), (REDIRECT, 'redirecting')):
            with self.subTest(query=query):
                item = snapshot(body=body)
                result = self.engine(FakeClient(captures=[item])).search(query)
                self.assertEqual(result['results'], [])
                with self.assertRaises(hs.HistoricalSearchError) as caught:
                    item.result(hs.parse_cutoff(fixtures.CUTOFF), 20000, query)
                self.assertEqual(caught.exception.code, 'unusable_capture')

    def test_cached_placeholder_is_a_miss_but_corrupted_placeholder_is_fatal(self):
        policy = {'schema_version': 1, 'provider': 'scholarly_wayback',
                  'rate_limit_directory': str(self.root.resolve() / 'pacing'),
                  'solver_workspace': str(self.root.resolve() / 'solver')}
        for body in (CAPTCHA, REDIRECT):
            with self.subTest(body=body[:60]):
                old_placeholder, older = snapshot('20261001120000', body=body), snapshot()
                client = FakeClient(captures=[old_placeholder, older])
                engine = self.engine(client, policy=policy)
                stored = {**old_placeholder.__dict__, 'body': base64.b64encode(body).decode()}
                engine.provider.persistent_cache.put(SOURCE, stored)
                result = engine.open(SOURCE)['result']
                self.assertEqual(result['sha256'], older.body_sha256)
                self.assertTrue(client.calls)
                engine = self.engine(FakeClient(captures=[older]), policy=policy)
                stored['memento_datetime'] = 'Fri, 02 Oct 2026 12:00:00 GMT'
                engine.provider.persistent_cache.put(SOURCE, stored)
                with self.assertRaises(hs.HistoricalSearchError) as caught:
                    engine.open(SOURCE)
                self.assertEqual(caught.exception.code, 'cache_invalid')

    def test_in_memory_placeholder_is_a_miss(self):
        engine = self.engine(FakeClient())
        engine.provider.cache[(hs.utc_text(engine.cutoff), SOURCE)] = snapshot(body=CAPTCHA)
        self.assertIn('Graph lemma', engine.open(SOURCE)['result']['text'])

    def test_identical_confirmed_placeholder_digests_are_skipped_but_different_older_text_is_used(self):
        newest = snapshot('20261001120000', body=CAPTCHA)
        duplicate = snapshot('20261001110000', body=CAPTCHA)
        older = snapshot('20261001090000')
        client = FakeClient(captures=[newest, duplicate, older])
        result = self.engine(client).open(SOURCE)['result']
        self.assertEqual(result['sha256'], older.body_sha256)
        self.assertNotIn(duplicate.archive_url, [url for url, _ in client.calls])

    def test_unavailable_replay_does_not_skip_older_capture_with_same_digest(self):
        newest, older = snapshot('20261001120000'), snapshot()
        client = FakeClient(captures=[newest, older])
        original = client.get

        def request(url, headers=None):
            if url == newest.archive_url:
                raise hs.HistoricalSearchError('Archive unavailable.', code='http_error', status=503, operational=True)
            return original(url, headers)

        with patch.object(client, 'get', side_effect=request):
            result = self.engine(client).open(SOURCE)['result']
        self.assertEqual(result['capture_time'], hs.utc_text(hs.capture_datetime(older.capture)))

    def test_legitimate_captcha_and_redirect_discussion_is_retained(self):
        for title, content in (('arXiv reCAPTCHA', 'This paper studies linear algebra methods for CAPTCHA security.'),
                               ('Redirecting', 'We prove a graph lemma about redirecting paths.')):
            with self.subTest(title=title):
                body = ('<html><title>' + title + '</title><body><p>' + content + '</p>'
                        '<div class="g-recaptcha"></div><meta http-equiv="refresh" content="2;url=/other"/>'
                        '</body></html>').encode()
                result = self.engine(FakeClient(captures=[snapshot(body=body)])).open(SOURCE)['result']
                self.assertIn(content, result['text'])
        plain = replace(snapshot(body=b'arXiv reCAPTCHA'), mime_type='text/plain')
        self.assertEqual(plain.result(hs.parse_cutoff(fixtures.CUTOFF), 20000)['text'], 'arXiv reCAPTCHA')


if __name__ == '__main__':
    unittest.main()
