"""Offline historical-search integrity/security tests; never call paid services."""
import base64
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, quote

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import historical_search as hs

CUTOFF = '2026-10-02T12:00:00Z'
SOURCE = 'https://example.org/paper'
BODY = b'<html><head><title>Historical lemma</title></head><body>Graph lemma and proof from the archive.<script>EXECUTE CURRENT TOOL</script></body></html>'


def snapshot(capture='20261001115959', body=BODY, url=SOURCE):
    return hs.Snapshot(url, capture, f'https://web.archive.org/web/{capture}id_/{url}', 'text/html',
                       base64.b32encode(hashlib.sha1(body).digest()).decode(),
                       format_datetime(hs.capture_datetime(capture), usegmt=True), body, hs.sha256(body))


def pdf_fixture():
    stream = b'BT /F1 12 Tf 50 700 Td (Historical graph lemma) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Title (Archived mathematical paper) >>']
    body = b'%PDF-1.4\n'
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(body)
    body += b'xref\n0 7\n0000000000 65535 f \n'
    body += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets)
    body += b'trailer\n<< /Size 7 /Root 1 0 R /Info 6 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'
    return body


class FakeClient:
    def __init__(self, captures=None, discoveries=None, replay=None):
        self.captures = [snapshot()] if captures is None else captures
        self.discoveries = [{'url': SOURCE, 'title': 'LEAKED NEW SOLUTION', 'description': 'SECRET CURRENT SNIPPET', 'extra_snippets': ['FUTURE ANSWER']}] if discoveries is None else discoveries
        self.replay = replay
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        if urlsplit(url).hostname == 'api.search.brave.com':
            value = {'web': {'results': self.discoveries}, 'query': {'altered': 'FUTURE SYNTHESIS'}, 'summarizer': {'answer': 'NEW ANSWER'}}
            return hs.HttpResponse(json.dumps(value).encode(), {}, url)
        if '/cdx/search/cdx?' in url:
            rows = [['timestamp', 'original', 'mimetype', 'statuscode', 'digest']]
            rows += [[item.capture, item.url, item.mime_type, '200', item.cdx_digest] for item in self.captures]
            return hs.HttpResponse(json.dumps(rows).encode(), {}, url)
        if self.replay is not None:
            return self.replay(url)
        for item in self.captures:
            if url == item.archive_url:
                return hs.HttpResponse(item.body, {'memento-datetime': item.memento_datetime,
                                                   'content-encoding': item.content_encoding}, url)
        raise AssertionError('Unexpected request; no live source fetch may occur')


class FakeDuckDuckGoClient(FakeClient):
    def __init__(self, body=None, status=200, redirected=False, **kwargs):
        super().__init__(**kwargs)
        self.html = ("<html><body><a class='result-link' href='//duckduckgo.com/l/?uddg="
                     + quote(SOURCE, safe='') + "&amp;rut=unused'>CURRENT SECRET TITLE</a>"
                     "<td class='result-snippet'>FUTURE SYNTHESIZED ANSWER</td></body></html>").encode() if body is None else body
        self.status, self.redirected = status, redirected

    def get(self, url, headers=None):
        if urlsplit(url).hostname == 'lite.duckduckgo.com':
            self.calls.append((url, headers))
            return hs.HttpResponse(self.html, {'content-type': 'text/html; charset=UTF-8'},
                                   'https://duckduckgo.com/challenge' if self.redirected else url, self.status)
        return super().get(url, headers)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy_path = self.root / 'policy.json'
        self.audit = self.root / 'audit.jsonl'

    def engine(self, client=None, policy=None, cutoff=CUTOFF):
        policy = policy or {'schema_version': 1, 'provider': 'brave_wayback'}
        self.policy_path.write_text(json.dumps(policy), encoding='utf-8')
        return hs.HistoricalSearchEngine(self.policy_path, cutoff, self.audit,
                                         hs.sha256(self.policy_path.read_bytes()),
                                         client=client or FakeClient(),
                                         environment={'BRAVE_SEARCH_API_KEY': 'test-secret-never-print'})

    def corpus_engine(self, snapshots=None):
        corpus_root = self.root / 'corpus'
        key = b'controller-key-at-least-32-bytes-long'
        key_path = self.root / 'secret.key'
        key_path.write_bytes(key)
        manifest = hs.seal_snapshots([snapshot()] if snapshots is None else snapshots, corpus_root, key)
        manifest_path = self.root / 'manifest.json'
        manifest_path.write_bytes(manifest)
        policy = {'schema_version': 1, 'provider': 'sealed_corpus', 'corpus_root': str(corpus_root),
                  'manifest_path': str(manifest_path), 'manifest_sha256': hs.sha256(manifest),
                  'signing_key_path': str(key_path)}
        return self.engine(policy=policy), policy

    def test_current_snippets_summaries_titles_never_returned(self):
        client = FakeClient()
        engine = self.engine(client)
        result = engine.search('graph lemma')
        serialized = json.dumps(result)
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['title'], 'Historical lemma')
        for marker in ('LEAKED', 'SECRET CURRENT', 'FUTURE', 'NEW ANSWER', 'EXECUTE CURRENT TOOL', 'test-secret'):
            self.assertNotIn(marker, serialized)
            self.assertNotIn(marker, self.audit.read_text())
        self.assertEqual({urlsplit(url).hostname for url, _ in client.calls}, {'api.search.brave.com', 'web.archive.org'})
        discovery = parse_qs(urlsplit(client.calls[0][0]).query)
        self.assertEqual(discovery['extra_snippets'], ['false'])
        self.assertEqual(discovery['freshness'], ['1900-01-01to2026-10-02'])

    def test_open_and_search_share_gate_and_use_cached_verified_bytes(self):
        client = FakeClient()
        engine = self.engine(client)
        first = engine.search('lemma')['results'][0]
        count = len(client.calls)
        second = engine.open(SOURCE)['result']
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertEqual(len(client.calls), count)
        self.assertIn('Graph lemma', second['text'])

    def test_equal_after_and_undated_captures_are_not_exposed(self):
        for capture in ('20261002120000', '20261003120000', ''):
            with self.subTest(capture=capture):
                client = FakeClient(captures=[replace(snapshot(), capture=capture)])
                engine = self.engine(client)
                self.assertEqual(engine.search('lemma')['results'], [])
                with self.assertRaises(hs.HistoricalSearchError):
                    engine.open(SOURCE)
                self.assertFalse(any(urlsplit(url).path.startswith('/web/') for url, _ in client.calls))

    def test_cdx_inclusive_to_is_below_exact_cutoff(self):
        client = FakeClient()
        self.engine(client).open(SOURCE)
        cdx = next(url for url, _ in client.calls if '/cdx/' in url)
        self.assertEqual(parse_qs(urlsplit(cdx).query)['to'], ['20261002115959'])

    def test_no_capture_is_not_a_live_fallback(self):
        client = FakeClient(captures=[])
        engine = self.engine(client)
        self.assertEqual(engine.search('lemma')['results'], [])
        self.assertFalse(any(url == SOURCE for url, _ in client.calls))

    def test_changed_replay_bytes_fail_cdx_digest_even_if_title_looks_old(self):
        client = FakeClient(replay=lambda url: hs.HttpResponse(BODY + b'NEW INFORMATION', {'memento-datetime': snapshot().memento_datetime}, url))
        engine = self.engine(client)
        self.assertEqual(engine.search('lemma')['results'], [])
        with self.assertRaises(hs.HistoricalSearchError):
            engine.open(SOURCE)

    def test_preserved_gzip_header_accepts_only_exact_verified_archive_bytes(self):
        client = FakeClient(replay=lambda url: hs.HttpResponse(
            BODY, {'memento-datetime': snapshot().memento_datetime,
                   'content-encoding': 'gzip'}, url))
        engine = self.engine(client)
        result = engine.open(SOURCE)['result']
        self.assertEqual(result['sha256'], hs.sha256(BODY))
        self.assertEqual(result['title'], 'Historical lemma')
        self.assertIn('Graph lemma', result['text'])
        self.assertEqual(len(engine.search('lemma')['results']), 1)

    def test_gzip_bytes_with_nonmatching_cdx_digest_fail_before_decompression(self):
        client = FakeClient(replay=lambda url: hs.HttpResponse(
            gzip.compress(BODY), {'memento-datetime': snapshot().memento_datetime,
                                 'content-encoding': 'gzip'}, url))
        engine = self.engine(client)
        with patch.object(hs, 'decode_verified_payload', side_effect=AssertionError('Unverified bytes must not be decoded')):
            with self.assertRaises(hs.HistoricalSearchError):
                engine.open(SOURCE)
            self.assertEqual(engine.search('lemma')['results'], [])

    def test_authenticated_gzip_payload_extracts_readable_text_and_retains_raw_digest(self):
        compressed = gzip.compress(BODY)
        archived = replace(snapshot(body=compressed), content_encoding='gzip')
        engine = self.engine(FakeClient(captures=[archived]))
        result = engine.open(SOURCE)['result']
        self.assertEqual(result['sha256'], hs.sha256(compressed))
        self.assertEqual(result['title'], 'Historical lemma')
        self.assertIn('Graph lemma', result['text'])
        self.assertEqual(len(engine.search('graph lemma')['results']), 1)
        corpus, _ = self.corpus_engine([archived])
        self.assertEqual(corpus.open(SOURCE)['result'], result)

    def test_authenticated_gzip_rejects_bombs_malformed_and_missing_header(self):
        compressed = gzip.compress(BODY)
        cases = [replace(snapshot(body=gzip.compress(b'x' * (hs.MAX_BYTES + 1))), content_encoding='gzip'),
                 replace(snapshot(body=compressed[:-3]), content_encoding='gzip'),
                 replace(snapshot(body=compressed + compressed), content_encoding='gzip'),
                 replace(snapshot(body=b'\x1f\x8bbroken'), content_encoding='gzip'),
                 snapshot(body=compressed)]
        for archived in cases:
            with self.subTest(bytes=len(archived.body), encoding=archived.content_encoding):
                engine = self.engine(FakeClient(captures=[archived]))
                with self.assertRaises(hs.HistoricalSearchError):
                    engine.open(SOURCE)
                self.assertEqual(engine.search('graph')['results'], [])

    def test_missing_memento_and_mismatched_memento_fail_closed(self):
        for value in ('', 'not a date', 'Fri, 02 Oct 2026 12:00:00 GMT'):
            with self.subTest(value=value):
                client = FakeClient(replay=lambda url: hs.HttpResponse(BODY, {'memento-datetime': value}, url))
                with self.assertRaises(hs.HistoricalSearchError):
                    self.engine(client).open(SOURCE)

    def test_redirects_never_become_outputs(self):
        for status, returned_url in ((302, SOURCE), (200, SOURCE), (200, snapshot('20261003120000').archive_url)):
            with self.subTest(status=status, returned_url=returned_url):
                client = FakeClient(replay=lambda url: hs.HttpResponse(BODY, {'memento-datetime': snapshot().memento_datetime}, returned_url, status))
                self.assertEqual(self.engine(client).search('lemma')['results'], [])
        with self.assertRaises(hs.HistoricalSearchError):
            hs.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'http://127.0.0.1/secret')

    def test_cached_entries_are_revalidated_and_bound_to_cutoff(self):
        engine = self.engine()
        engine.open(SOURCE)
        key = (hs.utc_text(engine.cutoff), SOURCE)
        engine.provider.cache[key] = snapshot('20261003120000')
        with self.assertRaises(hs.HistoricalSearchError):
            engine.open(SOURCE)
        engine.provider.cache[key] = replace(snapshot(), body=BODY + b'tampering')
        with self.assertRaises(hs.HistoricalSearchError):
            engine.open(SOURCE)

    def test_source_identity_mismatch_is_rejected(self):
        client = FakeClient(captures=[snapshot(url='https://elsewhere.org/paper')])
        self.assertEqual(self.engine(client).search('lemma')['results'], [])

    def test_nontext_capture_is_rejected_without_replay(self):
        client = FakeClient(captures=[replace(snapshot(), mime_type='application/octet-stream')])
        self.assertEqual(self.engine(client).search('lemma')['results'], [])
        self.assertFalse(any(urlsplit(url).path.startswith('/web/') for url, _ in client.calls))

    def test_archive_errors_do_not_expose_unverified_response(self):
        client = FakeClient(replay=lambda url: (_ for _ in ()).throw(hs.HistoricalSearchError('Archive/search request failed; no live fallback is permitted.')))
        engine = self.engine(client)
        self.assertEqual(engine.search('lemma')['results'], [])

    def test_no_current_ranking_and_duplicate_urls_deterministic(self):
        second_url = 'https://aaa.org/paper'
        client = FakeClient(captures=[snapshot(), snapshot(url=second_url)], discoveries=[{'url': SOURCE}, {'url': second_url}, {'url': SOURCE}])
        result = self.engine(client).search('lemma')
        self.assertEqual([row['url'] for row in result['results']], [second_url, SOURCE])

    def test_invalid_urls_never_reach_any_provider(self):
        urls = ['http://127.0.0.1/a', 'http://localhost/a', 'https://host.internal/a', 'http://169.254.169.254/latest',
                'file:///secret', 'https://user:pass@example.org/a', 'https://example.org:444/a',
                'http://[::1]/', 'http://10.0.0.1/', 'http://2130706433/', 'https://example.org/%0d%0aHeader:bad',
                'https://web.archive.org/web/20261003120000id_/https://example.org/']
        client = FakeClient()
        engine = self.engine(client)
        for url in urls:
            with self.subTest(url=url), self.assertRaises(hs.HistoricalSearchError):
                engine.open(url)
        self.assertEqual(client.calls, [])

    def test_network_client_rejects_non_allowlisted_endpoint_and_local_resolution(self):
        client = hs.RestrictedHttpClient()
        for url in ('https://example.org/', 'http://web.archive.org/', 'https://web.archive.org:444/', 'https://evil@web.archive.org/'):
            with self.subTest(url=url), self.assertRaises(hs.HistoricalSearchError):
                client.get(url)
        with patch('retrieval.historical_search.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(hs.HistoricalSearchError):
                client.get('https://web.archive.org/cdx/search/cdx')

    def test_sealed_corpus_excludes_future_and_preserves_historical_content(self):
        engine, _ = self.corpus_engine([snapshot(), snapshot('20261003120000', BODY + b'future')])
        result = engine.search('graph')['results'][0]
        self.assertEqual(result['sha256'], hs.sha256(BODY))
        self.assertNotIn('future', json.dumps(result))

    def test_corpus_pinned_manifest_detects_changed_timestamp(self):
        _, policy = self.corpus_engine()
        path = Path(policy['manifest_path'])
        data = json.loads(path.read_text())
        data['entries'][0]['capture'] = '20000101000000'
        path.write_text(json.dumps(data))
        with self.assertRaises(hs.HistoricalSearchError):
            self.engine(policy=policy)

    def test_even_repinning_forged_manifest_does_not_replace_controller_signature(self):
        _, policy = self.corpus_engine()
        path = Path(policy['manifest_path'])
        data = json.loads(path.read_text())
        data['entries'][0]['body_sha256'] = '0' * 64
        path.write_text(json.dumps(data))
        policy['manifest_sha256'] = hs.sha256(path.read_bytes())
        with self.assertRaises(hs.HistoricalSearchError):
            self.engine(policy=policy)

    def test_corpus_byte_tampering_and_key_inside_corpus_fail(self):
        _, policy = self.corpus_engine()
        body_path = Path(policy['corpus_root']) / (hs.sha256(BODY) + '.bin')
        body_path.write_bytes(BODY + b'current')
        with self.assertRaises(hs.HistoricalSearchError):
            self.engine(policy=policy)
        engine, policy = self.corpus_engine()
        copied_key = Path(policy['corpus_root']) / 'secret.key'
        copied_key.write_bytes(Path(policy['signing_key_path']).read_bytes())
        policy['signing_key_path'] = str(copied_key)
        with self.assertRaises(hs.HistoricalSearchError):
            self.engine(policy=policy)

    def test_loaded_corpus_is_immutable_in_memory_after_initial_verification(self):
        engine, policy = self.corpus_engine()
        body_path = Path(policy['corpus_root']) / (hs.sha256(BODY) + '.bin')
        body_path.write_bytes(b'new content')
        self.assertEqual(engine.open(SOURCE)['result']['sha256'], hs.sha256(BODY))

    def test_preflight_never_calls_network_and_does_not_print_credential(self):
        self.policy_path.write_text('{"schema_version":1,"provider":"brave_wayback"}')
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', {'BRAVE_SEARCH_API_KEY': 'secret-test-key'}), patch.object(hs.RestrictedHttpClient, 'get', side_effect=AssertionError('network prohibited')), redirect_stdout(output), redirect_stderr(errors):
            code = hs.main(['--cutoff', CUTOFF, '--policy-file', str(self.policy_path), '--audit-log', str(self.audit), '--preflight'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())['network_checked'])
        self.assertNotIn('secret-test-key', output.getvalue() + errors.getvalue() + self.audit.read_text())

    def test_missing_credentials_and_wrong_policy_pin_fail_without_network(self):
        self.policy_path.write_text('{"schema_version":1,"provider":"brave_wayback"}')
        with self.assertRaises(hs.HistoricalSearchError):
            hs.HistoricalSearchEngine(self.policy_path, CUTOFF, self.audit, environment={})
        with self.assertRaises(hs.HistoricalSearchError):
            hs.HistoricalSearchEngine(self.policy_path, CUTOFF, self.audit, '0' * 64, environment={'BRAVE_SEARCH_API_KEY': 'valid'})

    def test_unknown_policy_fields_cannot_smuggle_caller_provenance(self):
        with self.assertRaises(hs.HistoricalSearchError):
            self.engine(policy={'schema_version': 1, 'provider': 'brave_wayback', 'cutoff': '2099-01-01T00:00:00Z'})

    def test_explicit_benchmark_exclusions_apply_to_search_open_and_cache(self):
        client = FakeClient()
        engine = self.engine(client, policy={'schema_version': 1, 'provider': 'brave_wayback', 'deny_url_prefixes': ['http://example.org/paper']})
        self.assertEqual(engine.search('graph')['results'], [])
        self.assertEqual(len(client.calls), 1)
        for url in (SOURCE, 'https://example.org/%70aper/solution.pdf'):
            with self.assertRaises(hs.HistoricalSearchError):
                engine.open(url)
        engine.provider.cache[(hs.utc_text(engine.cutoff), SOURCE)] = snapshot()
        with self.assertRaises(hs.HistoricalSearchError):
            engine._output(snapshot())

    @unittest.skipUnless(hs.pdf_supported(), 'Optional pypdf>=6.20.1,<7 not installed')
    def test_verified_pdf_text_and_title_derive_only_from_archived_bytes(self):
        paper = replace(snapshot(body=pdf_fixture()), mime_type='application/pdf')
        client = FakeClient(captures=[paper])
        engine = self.engine(client)
        result = engine.search('graph lemma')['results'][0]
        self.assertEqual(result['title'], 'Archived mathematical paper')
        self.assertIn('Historical graph lemma', result['text'])
        self.assertEqual(engine.open(SOURCE)['result']['sha256'], paper.body_sha256)
        self.assertEqual(len(client.calls), 3)

    def test_pdf_without_supported_decoder_fails_closed(self):
        paper = replace(snapshot(body=pdf_fixture()), mime_type='application/pdf')
        engine = self.engine(FakeClient(captures=[paper]))
        with patch.object(hs, 'pdf_supported', return_value=False):
            self.assertEqual(engine.search('graph')['results'], [])
            with self.assertRaises(hs.HistoricalSearchError):
                engine.open(SOURCE)

    def test_pdf_decoder_is_never_called_before_archive_integrity_gate(self):
        paper = replace(snapshot(body=pdf_fixture()), mime_type='application/pdf')
        client = FakeClient(captures=[paper], replay=lambda url: hs.HttpResponse(paper.body + b'current', {'memento-datetime': paper.memento_datetime}, url))
        with patch.object(hs, 'extract_pdf', side_effect=AssertionError('must not parse unverifiable content')):
            self.assertEqual(self.engine(client).search('graph')['results'], [])

    def test_audit_failure_blocks_result_release(self):
        engine = self.engine()
        with patch.object(engine.audit, 'write', side_effect=hs.HistoricalSearchError('Audit logging failed; retrieval is disabled.')):
            with self.assertRaises(hs.HistoricalSearchError):
                engine.open(SOURCE)

    def test_transient_capture_audit_failure_aborts_search_without_releasing_results(self):
        engine = self.engine()
        original_write = engine.audit.write

        def fail_capture_once(event, **fields):
            if event == 'capture_verified':
                raise hs.HistoricalSearchError('Audit logging failed; retrieval is disabled.')
            return original_write(event, **fields)

        with patch.object(engine.audit, 'write', side_effect=fail_capture_once):
            with self.assertRaises(hs.HistoricalSearchError):
                engine.search('graph')
        events = [json.loads(line)['event'] for line in self.audit.read_text().splitlines()]
        self.assertEqual(events, ['startup', 'search_requested', 'search_failed'])

    def test_mcp_handshake_and_no_cutoff_or_path_override(self):
        engine = self.engine()
        server = hs.StdioMCPServer(engine)
        before = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertIn('error', before)
        response = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        self.assertEqual(response['result']['protocolVersion'], '2025-06-18')
        self.assertIsNone(server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        tools = server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})['result']['tools']
        self.assertEqual([tool['name'] for tool in tools], ['search', 'open', 'find'])
        for tool in tools:
            self.assertEqual(tool['annotations'], {
                'readOnlyHint': True, 'destructiveHint': False,
                'idempotentHint': True, 'openWorldHint': True,
            })
        for extra in ('cutoff', 'policy_file', 'capture_time', 'sha256', 'headers'):
            response = server.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'search', 'arguments': {'query': 'lemma', extra: 'arbitrary'}}})
            self.assertIn('error', response)
        response = server.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'search', 'arguments': {'query': 'lemma'}}})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(len(json.loads(response['result']['content'][0]['text'])['results']), 1)

    def test_mcp_json_lines_parse_errors_and_no_provider_exception_leak(self):
        engine = self.engine()
        server = hs.StdioMCPServer(engine)
        messages = ['garbage', json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}), json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}), json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'search', 'arguments': {'query': 'lemma'}}})]
        output = io.StringIO()
        with patch.object(engine, 'search', side_effect=RuntimeError('SECRET FUTURE PROVIDER TEXT')):
            server.serve(io.StringIO('\n'.join(messages) + '\n'), output)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]['error']['code'], -32700)
        self.assertTrue(records[-1]['result']['isError'])
        self.assertNotIn('SECRET', output.getvalue())

    def test_real_stdio_process_offline_handshake_and_open(self):
        self.corpus_engine()
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'open', 'arguments': {'url': SOURCE}}},
        ]
        completed = subprocess.run([sys.executable, '-u', str(Path(hs.__file__)), '--cutoff', CUTOFF,
                                    '--policy-file', str(self.policy_path), '--policy-sha256', hs.sha256(self.policy_path.read_bytes()),
                                    '--audit-log', str(self.root / 'subprocess-audit.jsonl')],
                                   input=''.join(json.dumps(message) + '\n' for message in messages),
                                   text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, '')
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([response['id'] for response in responses], [1, 2, 3])
        result = json.loads(responses[-1]['result']['content'][0]['text'])['result']
        self.assertEqual(result['sha256'], hs.sha256(BODY))
        self.assertIn('Graph lemma', result['text'])

    def test_cutoff_requires_explicit_utc_and_strict_equality_is_excluded(self):
        for value in ('2026-10-02', '2026-10-02T12:00:00', '2026-10-02T14:00:00+02:00'):
            with self.subTest(value=value), self.assertRaises(hs.HistoricalSearchError):
                hs.parse_cutoff(value)
        with self.assertRaises(hs.HistoricalSearchError):
            snapshot('20261002120000').validate(hs.parse_cutoff(CUTOFF))

    def test_duckduckgo_discovery_uses_only_urls_no_credentials_or_live_metadata(self):
        client = FakeDuckDuckGoClient()
        policy = {'schema_version': 1, 'provider': 'duckduckgo_wayback'}
        self.policy_path.write_text(json.dumps(policy))
        engine = hs.HistoricalSearchEngine(self.policy_path, CUTOFF, self.audit,
                                           client=client, environment={})
        result = engine.search('graph lemma')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['title'], 'Historical lemma')
        self.assertNotIn('CURRENT SECRET', json.dumps(result))
        self.assertNotIn('FUTURE SYNTHESIZED', json.dumps(result))
        self.assertTrue(all(headers is None for _, headers in client.calls))
        self.assertEqual({urlsplit(url).hostname for url, _ in client.calls}, {'lite.duckduckgo.com', 'web.archive.org'})
        self.assertFalse(any('/l/' == urlsplit(url).path for url, _ in client.calls))

    def test_duckduckgo_challenge_errors_redirects_stop_without_other_endpoint(self):
        cases = [FakeDuckDuckGoClient(status=202), FakeDuckDuckGoClient(status=302),
                 FakeDuckDuckGoClient(redirected=True),
                 FakeDuckDuckGoClient(body=b'<html><form id="challenge-form" action="/anomaly.js"></form></html>'),
                 FakeDuckDuckGoClient(body=b'<html><div class="anomaly-modal">challenge</div></html>'),
                 FakeDuckDuckGoClient(body=b'<html>Unrecognized result markup</html>')]
        for client in cases:
            with self.subTest(status=client.status, body=client.html[:60]):
                engine = self.engine(client, policy={'schema_version': 1, 'provider': 'duckduckgo_wayback'})
                with self.assertRaises(hs.HistoricalSearchError):
                    engine.search('graph')
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(urlsplit(client.calls[0][0]).hostname, 'lite.duckduckgo.com')

    def test_duckduckgo_empty_result_marker_is_not_a_live_fallback(self):
        client = FakeDuckDuckGoClient(body=b'<html><div class="no-results">No results.</div></html>')
        result = self.engine(client, policy={'schema_version': 1, 'provider': 'duckduckgo_wayback'}).search('graph')
        self.assertEqual(result['results'], [])
        self.assertEqual(len(client.calls), 1)

    def test_duckduckgo_wrappers_are_decoded_locally_and_invalid_targets_rejected(self):
        good = '//duckduckgo.com/l/?uddg=' + quote(SOURCE, safe='') + '&rut=opaque'
        self.assertEqual(hs.duckduckgo_source_url(good), SOURCE)
        self.assertEqual(hs.duckduckgo_source_url(SOURCE), SOURCE)
        targets = ['http://127.0.0.1/private', 'file:///secret', 'javascript:alert(1)',
                   'https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org']
        for target in targets:
            with self.subTest(target=target), self.assertRaises(hs.HistoricalSearchError):
                hs.duckduckgo_source_url('//duckduckgo.com/l/?uddg=' + quote(target, safe=''))
        for wrapper in ('https://duckduckgo.com/l/?uddg=a&uddg=b',
                        'https://user:pass@duckduckgo.com/l/?uddg=' + quote(SOURCE, safe=''),
                        'https://duckduckgo.com:444/l/?uddg=' + quote(SOURCE, safe=''),
                        'https://html.duckduckgo.com/html/?q=graph'):
            with self.subTest(wrapper=wrapper), self.assertRaises(hs.HistoricalSearchError):
                hs.duckduckgo_source_url(wrapper)

    def test_duckduckgo_candidates_cross_unchanged_archive_time_and_integrity_gate(self):
        cases = [FakeDuckDuckGoClient(captures=[snapshot('20261002120000')]),
                 FakeDuckDuckGoClient(replay=lambda url: hs.HttpResponse(BODY + b'future', {'memento-datetime': snapshot().memento_datetime}, url))]
        for client in cases:
            engine = self.engine(client, policy={'schema_version': 1, 'provider': 'duckduckgo_wayback'})
            self.assertEqual(engine.search('graph')['results'], [])
            with self.assertRaises(hs.HistoricalSearchError):
                engine.open(SOURCE)

    def test_duckduckgo_policy_rejects_credential_forwarding_setting(self):
        with self.assertRaises(hs.HistoricalSearchError):
            self.engine(FakeDuckDuckGoClient(), policy={'schema_version': 1, 'provider': 'duckduckgo_wayback', 'credential_env': 'BRAVE_SEARCH_API_KEY'})

    def test_duckduckgo_preflight_works_without_credentials_and_without_network(self):
        self.policy_path.write_text('{"schema_version":1,"provider":"duckduckgo_wayback"}')
        output = io.StringIO()
        # Retain Windows system environment needed to initialize TLS; no key is usable.
        with patch.dict('os.environ', {'BRAVE_SEARCH_API_KEY': ''}), patch.object(hs.RestrictedHttpClient, 'get', side_effect=AssertionError('no network preflight')), redirect_stdout(output):
            code = hs.main(['--cutoff', CUTOFF, '--policy-file', str(self.policy_path), '--audit-log', str(self.audit), '--preflight'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['provider'], 'duckduckgo_wayback')


if __name__ == '__main__':
    unittest.main()
