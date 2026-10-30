import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import historical_codex_discovery as discovery


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.executable = self.root / 'codex'
        self.executable.write_bytes(b'\xcf\xfa\xed\xfe' + b'native-test-only')
        self.executable.chmod(0o700)
        with patch.object(discovery.sys, 'platform', 'darwin'):
            self.settings = discovery.build_settings(self.executable, self.root / 'auth',
                                                      self.root / 'state', 'gpt-6.1-sol')

    def settings_check(self, value=None, workspace=None):
        with patch.object(discovery.sys, 'platform', 'darwin'):
            return discovery.validate_settings(value or self.settings, workspace)

    def trace(self, *, final=None, items=None, completed=True, text=None):
        final = {'urls': ['https://example.org/paper.pdf']} if final is None else final
        raw = json.dumps(final) if text is None else text
        items = ([{'id': 'w1', 'type': 'web_search', 'status': 'completed',
                   'action': {'type': 'search', 'query': 'mathematics'},
                   'results': [{'url': 'https://example.org/paper.pdf'}]}]
                 if items is None else items)
        events = [{'type': 'thread.started', 'thread_id': 't1'}, {'type': 'turn.started'}]
        events += [{'type': 'item.completed', 'item': item} for item in items]
        events += [{'type': 'item.completed', 'item': {'id': 'a1', 'type': 'agent_message', 'text': raw}}]
        if completed:
            events.append({'type': 'turn.completed', 'usage': {}})
        trace, output = self.root / 'events.jsonl', self.root / 'urls.json'
        trace.write_text('\n'.join(json.dumps(event) for event in events) + '\n')
        output.write_text(raw)
        return trace, output

    def test_preparation_validation_is_read_only_and_allows_unprovisioned_auth(self):
        self.assertEqual(self.settings_check(), self.settings)
        self.assertFalse((self.root / 'auth').exists())
        self.assertFalse((self.root / 'state').exists())

    def test_executable_replacement_and_script_shims_fail_closed(self):
        self.executable.write_bytes(b'#!/bin/sh\n')
        with self.assertRaises(discovery.DiscoveryError):
            self.settings_check()
        self.executable.write_bytes(b'\xcf\xfa\xed\xfe' + b'changed-native')
        with self.assertRaises(discovery.DiscoveryError):
            self.settings_check()

    def test_paths_inside_solver_and_symlinked_auth_are_rejected(self):
        with self.assertRaises(discovery.DiscoveryError):
            self.settings_check(workspace=self.root)
        (self.root / 'auth').symlink_to(self.root / 'elsewhere')
        with self.assertRaises(discovery.DiscoveryError):
            self.settings_check()

    def test_unknown_settings_and_invalid_timeout_are_rejected(self):
        for updates in ({'shell': True}, {'timeout_seconds': float('nan')},
                        {'timeout_seconds': True}, {'model': 'name --unsafe'}):
            with self.subTest(updates=updates), self.assertRaises(discovery.DiscoveryError):
                self.settings_check({**self.settings, **updates})

    def test_fresh_home_copies_only_auth_and_strips_provider_and_proxy_environment(self):
        auth = self.root / 'auth'; auth.mkdir()
        (auth / 'auth.json').write_text('{"tokens":{"access_token":"test"}}')
        (auth / 'config.toml').write_text('malicious = true')
        (auth / 'AGENTS.md').write_text('malicious instructions')
        with patch.object(discovery.sys, 'platform', 'darwin'), patch.dict(os.environ, {
                'OPENAI_API_KEY': 'secret', 'HTTP_PROXY': 'secret', 'HTTPS_PROXY': 'secret',
                'ALL_PROXY': 'secret', 'DYLD_INSERT_LIBRARIES': 'secret', 'CODEX_HOME': 'wrong'}):
            prepared = discovery._prepare(self.settings)
        _, root, home, workspace, environment, config = prepared
        self.assertEqual((home / 'auth.json').read_bytes(), (auth / 'auth.json').read_bytes())
        self.assertEqual((home / 'config.toml').read_text(), '')
        self.assertFalse((home / 'AGENTS.md').exists())
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(environment['CODEX_HOME'], str(home))
        for name in ('OPENAI_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'DYLD_INSERT_LIBRARIES'):
            self.assertNotIn(name, environment)
        self.assertFalse(config['features']['shell_tool'])
        self.assertFalse(config['features']['multi_agent'])
        self.assertEqual(config['web_search'], 'live')
        self.assertEqual(config['mcp_servers'], {})
        self.assertEqual(config['permissions']['discovery']['network'], {'enabled': False})

    def test_actual_search_required_and_only_url_candidates_leave_trace(self):
        trace, final = self.trace(items=[{'id': 'reasoning', 'type': 'reasoning', 'text': 'CURRENT SECRET'},
                                        {'id': 'w1', 'type': 'web_search', 'status': 'completed',
                                         'action': {'type': 'search', 'query': 'query'},
                                         'results': [{'url': 'https://example.org/paper.pdf', 'snippet': 'CURRENT SECRET'}]}])
        self.assertEqual(discovery._trace(trace, final, 3), (['https://example.org/paper.pdf'], 1, 0))
        for items in ([], [{'id': 'w1', 'type': 'web_search', 'action': {'type': 'openPage', 'url': 'https://example.org'}}],
                      [{'id': 'w1', 'type': 'web_search', 'status': 'failed', 'action': {'type': 'search'}}],
                      [{'type': 'web_search', 'action': {'type': 'search'}}]):
            with self.subTest(items=items), self.assertRaises(discovery.DiscoveryError):
                discovery._trace(*self.trace(items=items), 3)

    def test_other_tool_execution_is_rejected_even_with_valid_urls(self):
        for kind in ('command_execution', 'file_change', 'mcp_tool_call', 'collab_tool_call'):
            with self.subTest(kind=kind), self.assertRaises(discovery.DiscoveryError):
                discovery._trace(*self.trace(items=[{'type': kind, 'id': 'x'}]), 3)

    def test_incomplete_turn_and_final_message_mismatch_are_rejected(self):
        with self.assertRaises(discovery.DiscoveryError):
            discovery._trace(*self.trace(completed=False), 3)
        trace, final = self.trace()
        final.write_text('{"urls":[]}')
        with self.assertRaises(discovery.DiscoveryError):
            discovery._trace(trace, final, 3)

    def test_final_shape_invalid_urls_and_oversize_candidate_lists_are_rejected(self):
        for value in ({'urls': [], 'answer': 'current proof'}, ['https://example.org'],
                      {'urls': ['file:///private/data']}, {'urls': ['https://user:password@example.org']},
                      {'urls': ['https://example.org/a\nb']}, {'urls': ['https://one.org', 'https://two.org']}):
            with self.subTest(value=value), self.assertRaises(discovery.DiscoveryError):
                discovery._trace(*self.trace(final=value), 1)

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_nonobjects(self):
        for raw in ('{"urls":[],"urls":[]}', '{"urls":NaN}', '{"urls":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(discovery.DiscoveryError):
                discovery._trace(*self.trace(text=raw), 3)
        trace, final = self.trace()
        trace.write_text('[]\n')
        with self.assertRaises(discovery.DiscoveryError):
            discovery._trace(trace, final, 3)

    def test_only_actual_search_result_urls_survive_model_selection(self):
        trace, final = self.trace(final={'urls': ['https://invented.example/proof']})
        self.assertEqual(discovery._trace(trace, final, 3),
                         (['https://example.org/paper.pdf'], 1, 1))
        items = [{'id': 'w1', 'type': 'web_search', 'action': {'type': 'search'},
                  'results': [{'url': 'https://example.org/a', 'snippet': 'PRIVATE CURRENT FACT'},
                              {'url': 'https://example.org/b'}]}]
        trace, final = self.trace(items=items, final={'urls': ['https://example.org/b#section',
                                                             'https://invented.example/proof']})
        self.assertEqual(discovery._trace(trace, final, 3), (['https://example.org/b'], 1, 1))

    def test_missing_native_results_rejected_but_observed_empty_search_is_valid(self):
        item = {'id': 'w1', 'type': 'web_search', 'action': {'type': 'search'}}
        with self.assertRaises(discovery.DiscoveryError):
            discovery._trace(*self.trace(items=[item]), 3)
        self.assertEqual(discovery._trace(*self.trace(items=[{**item, 'results': []}],
                                                       final={'urls': []}), 3), ([], 1, 0))

    def test_native_duplicate_web_item_id_compatibility_is_narrow(self):
        raw = ('{"type":"web_search","id":"item_0",'
               '"id":"exec-60a21c52-0db0-4054-8a8c-2d8145c52245"}')
        self.assertEqual(discovery._json(raw, native_trace=True)['id'], 'item_0')
        for value in (raw, raw.replace('web_search', 'agent_message'),
                      raw.replace('item_0', 'arbitrary'),
                      raw[:-1] + ',"type":"web_search"}'):
            with self.subTest(value=value), self.assertRaises(discovery.DiscoveryError):
                discovery._json(value, native_trace=value != raw)

    def test_auth_refresh_is_delegated_after_preflight_even_on_failure(self):
        auth = self.root / 'auth'; auth.mkdir()
        (auth / 'auth.json').write_text('{"tokens":{"access_token":"test"}}')
        with patch.object(discovery.sys, 'platform', 'darwin'), \
                patch.object(discovery, '_verify', side_effect=discovery.DiscoveryError()), \
                patch('retrieval.historical_discovery_auth.preserve_refresh', return_value=False) as preserve:
            with self.assertRaises(discovery.DiscoveryError):
                discovery.preflight(self.settings)
        self.assertEqual(preserve.call_count, 1)
        self.assertEqual(preserve.call_args.args[0], auth / 'auth.json')
        self.assertEqual(preserve.call_args.args[2], discovery._hash(auth / 'auth.json'))

    def test_expired_deadline_starts_no_process(self):
        with patch.object(discovery.subprocess, 'Popen') as process:
            with self.assertRaises(discovery.DiscoveryError) as error:
                discovery.discover('query', '2020-01-01T00:00:00Z', 3,
                                   self.settings, lambda *args, **kwargs: None, deadline=0)
        self.assertEqual(error.exception.code, 'discovery_timeout')
        process.assert_not_called()

    def test_auth_and_cli_failure_messages_never_leak_raw_content(self):
        for raw in ('not-json SECRET', '{"OPENAI_API_KEY":NaN}', '{}'):
            path = self.root / 'auth.json'; path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(discovery.DiscoveryError) as error:
                discovery._auth(path)
            self.assertNotIn('SECRET', str(error.exception))


if __name__ == '__main__':
    unittest.main()
