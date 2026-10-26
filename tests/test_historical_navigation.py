"""Offline navigation reaches full archived text without weakening its gate."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from retrieval import historical_search as hs
import test_historical_search as fixtures


class HistoricalNavigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audit = self.root / 'audit.jsonl'

    def engine(self, text='Archive text.', *, limit=20000, client=None, extra_policy=None):
        self.archived = replace(fixtures.snapshot(body=text.encode()), mime_type='text/plain')
        self.client = client or fixtures.FakeClient(captures=[self.archived])
        policy = {'schema_version': 1, 'provider': 'brave_wayback',
                  'max_content_chars': limit, **(extra_policy or {})}
        path = self.root / 'policy.json'
        path.write_text(json.dumps(policy))
        return hs.HistoricalSearchEngine(path, fixtures.CUTOFF, self.audit,
                                         hs.sha256(path.read_bytes()), client=self.client,
                                         environment={'BRAVE_SEARCH_API_KEY': 'fixture'})

    @staticmethod
    def call(server, name, arguments):
        return server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                              'params': {'name': name, 'arguments': arguments}})

    def test_find_reaches_definition_after_115000_characters_then_open_reads_it(self):
        content = 'x' * 115275 + 'Definition 4.11. Exact archived definition. ' + 'y' * 5000
        engine = self.engine(content)
        initial = engine.open(fixtures.SOURCE, length=20000)['result']
        self.assertEqual(len(initial['text']), 20000)
        self.assertNotIn('Definition', initial['text'])
        self.assertTrue(initial['has_more'])
        found = engine.find(fixtures.SOURCE, 'Definition 4.11', sha256=initial['sha256'])['result']
        match = found['matches'][0]
        self.assertEqual(match['offset'], 115275)
        self.assertIn('Exact archived definition.', match['text'])
        page = engine.open(fixtures.SOURCE, offset=match['offset'], length=200,
                           sha256=found['sha256'])['result']
        self.assertEqual(page['text'], content[115275:115475])
        self.assertEqual(page['total_chars'], len(content))
        self.assertEqual(page['archive_url'], initial['archive_url'])
        self.assertEqual({urlsplit(url).hostname for url, _ in self.client.calls}, {'web.archive.org'})

    def test_unicode_pages_reconstruct_document_and_explicit_end_is_empty(self):
        content = ('αβ🙂café\n' * 43) + 'END'
        engine = self.engine(content, limit=100)
        pages, offset, digest = [], 0, None
        while True:
            result = engine.open(fixtures.SOURCE, offset=offset, length=73, sha256=digest)['result']
            digest = result['sha256']
            self.assertEqual(result['offset'], offset)
            self.assertEqual(result['end_offset'], offset + len(result['text']))
            self.assertEqual(result['offset_unit'], 'unicode_code_points')
            self.assertEqual(result['document_text_sha256'], hs.sha256(content.encode()))
            self.assertEqual(result['text_sha256'], hs.sha256(result['text'].encode()))
            pages.append(result['text'])
            if not result['has_more']:
                self.assertIsNone(result['next_offset'])
                break
            self.assertGreater(result['next_offset'], offset)
            offset = result['next_offset']
        self.assertEqual(''.join(pages), content)
        final = engine.open(fixtures.SOURCE, offset=len(content), sha256=digest)['result']
        self.assertEqual(final['text'], '')
        self.assertFalse(final['has_more'])
        self.assertFalse(final['truncated'])
        self.assertIsNone(final['next_offset'])
        with self.assertRaisesRegex(hs.HistoricalSearchError, 'beyond'):
            engine.open(fixtures.SOURCE, offset=len(content) + 1)

    def test_find_is_literal_case_sensitive_and_retains_overlapping_matches(self):
        engine = self.engine('aaaa.*AA literal.* tail', limit=100)
        first = engine.find(fixtures.SOURCE, 'aa', max_matches=1)['result']
        self.assertEqual([item['offset'] for item in first['matches']], [0])
        self.assertEqual(first['next_offset'], 1)
        second = engine.find(fixtures.SOURCE, 'aa', offset=first['next_offset'],
                             sha256=first['sha256'])['result']
        self.assertEqual([item['offset'] for item in second['matches']], [1, 2])
        self.assertFalse(second['has_more'])
        literal = engine.find(fixtures.SOURCE, '.*')['result']
        self.assertEqual([item['offset'] for item in literal['matches']], [4, 16])
        empty = engine.find(fixtures.SOURCE, 'absent')['result']
        self.assertEqual(empty['matches'], [])
        self.assertIsNone(empty['next_offset'])
        self.assertFalse(empty['has_more'])
        with self.assertRaisesRegex(hs.HistoricalSearchError, 'beyond'):
            engine.find(fixtures.SOURCE, 'aa', offset=999)

    def test_find_pages_obey_requested_match_count_and_every_cursor_advances(self):
        content = 'needle ' * 400
        engine = self.engine(content, limit=100)
        positions, offset = [], 0
        for _ in range(401):
            result = engine.find(fixtures.SOURCE, 'needle', offset=offset, max_matches=57)['result']
            self.assertLessEqual(len(result['matches']), 57)
            for match in result['matches']:
                self.assertEqual(match['text'], content[match['excerpt_offset']:match['excerpt_end_offset']])
                self.assertEqual(content[match['offset']:match['end_offset']], 'needle')
                self.assertEqual(match['text_sha256'], hs.sha256(match['text'].encode()))
            positions.extend(item['offset'] for item in result['matches'])
            if not result['has_more']:
                break
            self.assertGreater(result['next_offset'], offset)
            offset = result['next_offset']
        self.assertEqual(positions, list(range(0, len(content), 7)))
        all_matches = engine.find(fixtures.SOURCE, 'needle', max_matches=2 ** 53 - 1)['result']
        self.assertEqual(len(all_matches['matches']), 400)
        self.assertGreater(sum(len(item['text']) for item in all_matches['matches']), 20000)
        self.assertFalse(all_matches['has_more'])

    def test_document_digest_pin_rejects_changed_verified_capture(self):
        engine = self.engine('Old verified text ' * 20, limit=100)
        first = engine.open(fixtures.SOURCE, length=100)['result']
        changed = replace(fixtures.snapshot(body=b'Different archived text'), mime_type='text/plain')
        with patch.object(engine.provider, 'open', return_value=changed):
            for name, args in (('open', {'offset': 0}), ('find', {'pattern': 'archived'})):
                with self.subTest(name=name), self.assertRaisesRegex(hs.HistoricalSearchError, 'changed'):
                    getattr(engine, name)(fixtures.SOURCE, sha256=first['sha256'], **args)
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(sum(row['event'] == 'open_completed' for row in events), 1)
        self.assertFalse(any(row['event'] == 'find_completed' for row in events))

    def test_every_page_and_find_revalidates_cutoff_cdx_and_body_integrity(self):
        engine = self.engine('Earlier archived text ' * 20, limit=100)
        first = engine.open(fixtures.SOURCE, length=100)['result']
        bad = [replace(self.archived, capture='20261002120000'),
               replace(self.archived, body=self.archived.body + b'CURRENT CONTENT'),
               replace(self.archived, cdx_digest='A' * 32),
               replace(self.archived, memento_datetime='Fri, 02 Oct 2026 12:00:00 GMT')]
        for archived in bad:
            with self.subTest(archived=archived.capture), patch.object(engine.provider, 'open', return_value=archived):
                for name, args in (('open', {'offset': first['next_offset']}), ('find', {'pattern': 'text'})):
                    with self.assertRaises(hs.HistoricalSearchError):
                        getattr(engine, name)(fixtures.SOURCE, **args)

    def test_navigation_denies_benchmark_paths_before_provider_even_after_cached_open(self):
        engine = self.engine('Verified source')
        engine.open(fixtures.SOURCE)
        engine.denied_prefixes = (fixtures.SOURCE,)
        with patch.object(engine.provider, 'open', side_effect=AssertionError('Denied provider call')):
            for name, args in (('open', {'offset': 1}), ('find', {'pattern': 'source'})):
                with self.assertRaisesRegex(hs.HistoricalSearchError, 'excluded'):
                    getattr(engine, name)(fixtures.SOURCE, **args)

    def test_missing_or_postcutoff_capture_has_no_live_fallback(self):
        for captures in ([], [fixtures.snapshot(capture='20261002120000')]):
            client = fixtures.FakeClient(captures=captures)
            engine = self.engine(client=client)
            for name, args in (('open', {'offset': 1}), ('find', {'pattern': 'lemma'})):
                with self.assertRaises(hs.HistoricalSearchError):
                    getattr(engine, name)(fixtures.SOURCE, **args)
            self.assertFalse(any(url == fixtures.SOURCE for url, _ in client.calls))

    def test_mcp_strict_navigation_arguments_are_rejected_before_provider(self):
        engine = self.engine('Archived text', limit=100)
        server = hs.StdioMCPServer(engine)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        invalid = [('open', {'offset': bad}) for bad in (-1, True, 1.5, '1', None, 2 ** 53)]
        invalid += [('open', {'length': bad}) for bad in (0, -1, True, 1.5, '5', None, 2 ** 53)]
        invalid += [('open', {'sha256': bad}) for bad in ('', 'A' * 64, 'a' * 63, None)]
        invalid += [('open', {key: 'override'}) for key in ('cutoff', 'policy', 'headers', 'capture_time')]
        invalid += [('find', {'pattern': bad}) for bad in ('', None, 3, 'x' * 501, 'x\0y')]
        invalid += [('find', {'pattern': 'text', 'max_matches': bad}) for bad in (0, 2 ** 53, True, 1.5, '2')]
        invalid += [('find', {'pattern': 'text', 'offset': bad}) for bad in (-1, True, 1.5, None)]
        invalid += [('find', {'pattern': 'text', 'regex': True}), ('find', {'pattern': 'text', 'cutoff': 'future'})]
        with patch.object(engine.provider, 'open', side_effect=AssertionError('Invalid provider call')):
            for name, args in invalid:
                with self.subTest(name=name, args=args):
                    response = self.call(server, name, {'url': fixtures.SOURCE, **args})
                    self.assertEqual(response['error']['code'], -32602)

    def test_mcp_open_compatibility_find_schema_and_receipts_bind_entire_result(self):
        engine = self.engine('Intro ' * 40 + 'needle tail', limit=100)
        server = hs.StdioMCPServer(engine)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        for name, args in (('open', {'url': fixtures.SOURCE}),
                           ('find', {'url': fixtures.SOURCE, 'pattern': 'needle'})):
            reply = self.call(server, name, args)
            self.assertFalse(reply['result']['isError'])
            value = json.loads(reply['result']['content'][0]['text'])
            result = value['result']
            for field in ('url', 'title', 'capture_time', 'archive_url', 'sha256', 'untrusted_source_content'):
                self.assertIn(field, result)
            receipt = next(json.loads(line) for line in reversed(self.audit.read_text().splitlines())
                           if json.loads(line)['event'] == name + '_completed')
            self.assertEqual(receipt['result_sha256'], hs.sha256(hs.canonical_json(result)))
            self.assertEqual(receipt['document_text_sha256'], result['document_text_sha256'])
            if name == 'find':
                self.assertEqual(receipt['matches'][0]['text_sha256'], result['matches'][0]['text_sha256'])
                self.assertNotIn('text', receipt['matches'][0])
        self.assertNotIn('needle', self.audit.read_text())

    def test_mcp_schema_advertises_full_default_independent_of_search_policy(self):
        def inventory(server):
            server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
            response = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
            return {tool['name']: tool for tool in response['result']['tools']}

        batch_server = hs.StdioMCPServer(self.engine(limit=20000))
        batch = inventory(batch_server)
        length = batch['open']['inputSchema']['properties']['length']
        self.assertNotIn('maximum', length)
        self.assertNotIn('default', length)
        self.assertIn('full remaining document', length['description'])
        self.assertIn('full remaining document', batch['open']['description'])
        self.assertNotIn('maximum', batch['find']['inputSchema']['properties']['max_matches'])
        self.assertEqual(batch['find']['inputSchema']['properties']['max_matches']['default'], 10)
        self.assertEqual(batch['find']['inputSchema']['properties']['pattern']['maxLength'], 500)

        small = inventory(hs.StdioMCPServer(self.engine(limit=100)))
        self.assertEqual(small, batch)
        self.assertEqual(inventory(batch_server), batch)
        small['open']['inputSchema']['properties']['length']['maximum'] = 1
        self.assertEqual(inventory(batch_server), batch)

    def test_mcp_full_default_and_requested_large_pages_have_no_policy_cap(self):
        content = 'x' * 175001 + 'Definition after the former caps.'
        engine = self.engine(content, limit=100)
        server = hs.StdioMCPServer(engine)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        with patch.object(engine.provider, 'open', side_effect=AssertionError('Invalid provider call')):
            reply = self.call(server, 'open', {'url': 'private-submitted-sentinel', 'length': 0})
        self.assertEqual(reply['error']['code'], -32602)
        self.assertIn('positive safe integer', reply['error']['message'])
        self.assertIn('full remaining document', reply['error']['message'])
        self.assertIn('next_offset', reply['error']['message'])
        self.assertNotIn('private-submitted-sentinel', json.dumps(reply))
        for arguments, expected in (({'url': fixtures.SOURCE}, content),
                                    ({'url': fixtures.SOURCE, 'length': 30000}, content[:30000]),
                                    ({'url': fixtures.SOURCE, 'length': 80000}, content[:80000]),
                                    ({'url': fixtures.SOURCE, 'offset': 115275}, content[115275:]),
                                    ({'url': fixtures.SOURCE, 'length': 2 ** 53 - 1}, content)):
            result = self.call(server, 'open', arguments)['result']
            self.assertFalse(result['isError'])
            page = json.loads(result['content'][0]['text'])['result']
            self.assertEqual(page['text'], expected)
            remaining = page['end_offset'] < len(content)
            self.assertEqual(page['has_more'], remaining)
            self.assertEqual(page['next_offset'], page['end_offset'] if remaining else None)

    def test_mcp_pattern_input_limit_is_independent_of_search_policy(self):
        engine = self.engine(limit=100)
        server = hs.StdioMCPServer(engine)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        with patch.object(engine.provider, 'open', side_effect=AssertionError('Invalid provider call')):
            reply = self.call(server, 'find', {'url': fixtures.SOURCE, 'pattern': 'private-sentinel-' * 40})
        self.assertEqual(reply['error']['code'], -32602)
        self.assertIn('1 to 500', reply['error']['message'])
        self.assertNotIn('private-sentinel', json.dumps(reply))
        reply = self.call(server, 'find', {'url': fixtures.SOURCE, 'pattern': 'x' * 500})
        self.assertFalse(reply['result']['isError'])

    def test_legacy_character_policy_still_bounds_search_excerpts_only(self):
        content = 'needle ' * 10000 + 'END'
        engine = self.engine(content, limit=100)
        results = engine.search('needle')['results']
        self.assertEqual(len(results), 1)
        self.assertLessEqual(len(results[0]['text']), 100)
        self.assertEqual(engine.open(fixtures.SOURCE)['result']['text'], content)
        default_matches = engine.find(fixtures.SOURCE, 'needle')['result']
        self.assertEqual(len(default_matches['matches']), 10)
        self.assertGreater(sum(len(match['text']) for match in default_matches['matches']), 100)

    def test_audit_failure_prevents_page_or_find_delivery(self):
        engine = self.engine('Archived needle text')
        original = engine.audit.write

        def write(event, **fields):
            if event in ('open_completed', 'find_completed'):
                raise hs.HistoricalSearchError('Audit unavailable.', code='audit_failure')
            return original(event, **fields)

        with patch.object(engine.audit, 'write', side_effect=write):
            for name, args in (('open', {}), ('find', {'pattern': 'needle'})):
                with self.assertRaisesRegex(hs.HistoricalSearchError, 'Audit'):
                    getattr(engine, name)(fixtures.SOURCE, **args)

    def test_real_stdio_protocol_navigates_sealed_document_without_network(self):
        content = 'x' * 115275 + 'Definition 4.11. Archived theorem tail.'
        archived = replace(fixtures.snapshot(body=content.encode()), mime_type='text/plain')
        key = b'controller-only-navigation-fixture-key'
        corpus = self.root / 'corpus'
        manifest = hs.seal_snapshots([archived], corpus, key)
        (self.root / 'manifest.json').write_bytes(manifest)
        (self.root / 'key').write_bytes(key)
        policy = {'schema_version': 1, 'provider': 'sealed_corpus', 'max_content_chars': 20000,
                  'corpus_root': str(corpus), 'manifest_path': str(self.root / 'manifest.json'),
                  'manifest_sha256': hs.sha256(manifest), 'signing_key_path': str(self.root / 'key')}
        path = self.root / 'policy.json'
        path.write_text(json.dumps(policy))
        calls = [('open', {'url': fixtures.SOURCE}),
                 ('find', {'url': fixtures.SOURCE, 'pattern': 'Definition 4.11', 'sha256': archived.body_sha256}),
                 ('open', {'url': fixtures.SOURCE, 'offset': 115275, 'sha256': archived.body_sha256})]
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}]
        messages += [{'jsonrpc': '2.0', 'id': number, 'method': 'tools/call',
                      'params': {'name': name, 'arguments': args}}
                     for number, (name, args) in enumerate(calls, 2)]
        process = subprocess.run([sys.executable, str(Path(hs.__file__)), '--cutoff', fixtures.CUTOFF,
                                  '--policy-file', str(path), '--audit-log', str(self.audit)],
                                 input=''.join(json.dumps(value) + '\n' for value in messages),
                                 text=True, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = {value['id']: value for value in map(json.loads, process.stdout.splitlines())}
        self.assertEqual(set(replies), {1, 2, 3, 4})
        results = {}
        for number in (2, 3, 4):
            self.assertFalse(replies[number]['result']['isError'])
            results[number] = json.loads(replies[number]['result']['content'][0]['text'])['result']
        self.assertEqual(results[2]['text'], content)
        self.assertIsNone(results[2]['next_offset'])
        self.assertFalse(results[2]['truncated'])
        self.assertEqual(results[3]['matches'][0]['offset'], 115275)
        self.assertEqual(results[4]['text'], content[115275:])
        self.assertFalse(results[4]['has_more'])


if __name__ == '__main__':
    unittest.main()
