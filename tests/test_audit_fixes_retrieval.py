"""Regression tests for audited retrieval defects; offline, with no model calls."""
import base64
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from retrieval import historical_codex_discovery as discovery
from retrieval import historical_search as hs
from retrieval import isolated_compiler
from retrieval import runtime as retrieval_runtime
from ui import retrieval
import test_historical_search as fixtures
from test_historical_search import SOURCE, FakeClient, snapshot
from test_historical_trajectory_audit import DOCUMENT, THREAD, TrajectoryAuditFixture, tool_event

UI_CUTOFF = '2025-06-01T00:00:00Z'
CANARIES = retrieval_runtime.SETTINGS_BOUNDARY_CANARIES
OLDER_BODY = b'<html><head><title>Older lemma</title></head><body>Graph lemma from an older capture.</body></html>'
MALFORMED = b'<html><head><title>Broken</title></head><body>Graph lemma t<![0,1] remains</body></html>'
CAPTCHA = (b'<html><head><title>arXiv reCAPTCHA</title></head><body>'
           b'<form><div class="g-recaptcha" data-sitekey="example"></div></form></body></html>')


def cache_record(item):
    return {**item.__dict__, 'body': base64.b64encode(item.body).decode('ascii')}


class RuntimePolicyFixture(unittest.TestCase):
    """A controller policy whose broker and Codex binaries are never started."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'run'
        self.workspace.mkdir()
        control = self.root / 'control'
        control.mkdir()
        script = control / 'historical_search.py'
        script.write_text('# unused mock broker', encoding='utf-8')
        for name in retrieval_runtime.BROKER_MODULES:
            (control / name).write_text('# unused mock dependency', encoding='utf-8')
        broker_policy = control / 'broker.json'
        broker_policy.write_text('{"schema_version":1}', encoding='utf-8')
        audit = str(control / 'search-audit.jsonl')
        policy_path = control / 'retrieval-policy.json'
        policy_path.write_text(json.dumps({
            'schemaVersion': 1, 'mode': 'historical', 'runId': 'P01',
            'cutoffExclusiveUtc': UI_CUTOFF,
            'broker': {'command': sys.executable, 'policyFile': str(broker_policy), 'auditLog': audit,
                       'args': [str(script), '--cutoff', UI_CUTOFF, '--policy-file', str(broker_policy),
                                '--policy-sha256', hashlib.sha256(broker_policy.read_bytes()).hexdigest(),
                                '--audit-log', audit]},
            'isolation': {'status': 'requires-runtime-preflight', 'adapter': retrieval_runtime.MACOS_ADAPTER,
                          'codexHome': str(control / 'codex-home'), 'workspace': str(self.workspace)},
        }), encoding='utf-8')
        (self.workspace / 'job-settings.json').write_text(
            json.dumps({'retrievalPolicyPath': str(policy_path)}), encoding='utf-8')
        environment = patch.dict(os.environ, {'PATH': '/controller/original/bin'})
        environment.start()
        self.addCleanup(environment.stop)
        self.policy = retrieval_runtime.load_policy(policy_path)


class SavedEnvironmentRefreshTests(RuntimePolicyFixture):
    """path-baked-into-isolated-config"""

    def config(self):
        environment = retrieval_runtime.isolated_environment(self.policy, 'author', {})
        return Path(environment['CODEX_HOME']) / 'config.toml'

    @staticmethod
    def shell_path(config):
        return tomllib.loads(config.read_text())['shell_environment_policy']['set']['PATH']

    def test_restarted_controller_path_is_rewritten_before_start(self):
        config = self.config()
        self.assertEqual(self.shell_path(config), '/controller/original/bin')
        os.environ['PATH'] = '/restarted/venv/bin'
        self.config()
        self.assertEqual(config.read_text(), retrieval_runtime.config_text(self.policy, 'author'))
        self.assertEqual(self.shell_path(config), '/restarted/venv/bin')

    def test_path_drift_with_native_trust_record_is_rewritten(self):
        config = self.config()
        persisted = tomllib.loads(config.read_text())
        persisted['projects'] = {str(self.workspace): {'trust_level': 'trusted'}}
        config.write_text('\n'.join(retrieval_runtime._toml_table(persisted)) + '\n')
        os.environ['PATH'] = '/restarted/terminal/bin'
        self.config()
        self.assertEqual(config.read_text(), retrieval_runtime.config_text(self.policy, 'author'))
        self.assertNotIn('projects', tomllib.loads(config.read_text()))

    def test_refresh_never_accepts_secrets_inheritance_or_other_edits(self):
        config = self.config()
        changes = {
            'secret': lambda value: value['shell_environment_policy']['set'].update(OPENAI_API_KEY='secret'),
            'inherit': lambda value: value['shell_environment_policy'].update(inherit='all'),
            'extra key': lambda value: value['shell_environment_policy'].update(exclude=['*KEY*']),
            'missing set': lambda value: value['shell_environment_policy'].pop('set'),
            'non-string': lambda value: value['shell_environment_policy']['set'].update(PATH=7),
            'other edit': lambda value: value['features'].update(view_image=True),
        }
        for label, change in changes.items():
            actual = retrieval_runtime.configuration(self.policy, 'author')
            change(actual)
            config.write_text('\n'.join(retrieval_runtime._toml_table(actual)) + '\n')
            before = config.read_bytes()
            with self.subTest(label), patch.dict(os.environ, {'PATH': '/restarted/venv/bin'}):
                with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'configuration has changed'):
                    retrieval_runtime.isolated_environment(self.policy, 'author', {})
            self.assertEqual(config.read_bytes(), before)

    def test_live_raw_layer_check_stays_exact(self):
        actual = retrieval_runtime.configuration(self.policy, 'author')
        actual['shell_environment_policy']['set']['PATH'] = '/other/bin'
        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'raw user config'):
            retrieval_runtime._validate_user_configuration(self.policy, 'author', actual, 'Historical raw user config')
        self.assertTrue(retrieval_runtime._validate_user_configuration(
            self.policy, 'author', actual, refresh_environment=True))


class LegacyBrokerLayoutTests(unittest.TestCase):
    """legacy-historical-policies-unloadable: policies prepared before the broker moved into retrieval/."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / 'tcs-prover'
        self.package = self.checkout / 'retrieval'
        self.package.mkdir(parents=True)
        for name in ('historical_search.py', *retrieval_runtime.BROKER_MODULES):
            (self.package / name).write_text(f'# current {name}', encoding='utf-8')
        self.legacy = self.checkout / 'historical_search.py'  # Deleted by the in-place update.
        for name, value in (('LEGACY_BROKER_SCRIPT', self.legacy),
                            ('CURRENT_BROKER_SCRIPT', self.package / 'historical_search.py')):
            change = patch.object(retrieval_runtime, name, value)
            change.start()
            self.addCleanup(change.stop)
        self.workspace = self.checkout / 'runs' / 'job'
        self.workspace.mkdir(parents=True)
        self.control = self.checkout / '.experiment-control' / 'ui' / 'job'
        self.control.mkdir(parents=True)
        environment = patch.dict(os.environ, {'PATH': '/controller/bin'})
        environment.start()
        self.addCleanup(environment.stop)

    def write_policy(self, script):
        broker_policy = self.control / 'broker-policy.json'
        broker_policy.write_text('{"schema_version":1}', encoding='utf-8')
        audit = str(self.control / 'historical-search.jsonl')
        path = self.control / 'retrieval-policy.json'
        path.write_text(json.dumps({
            'schemaVersion': 1, 'mode': 'historical', 'runId': 'job', 'cutoffExclusiveUtc': UI_CUTOFF,
            'broker': {'command': sys.executable, 'policyFile': str(broker_policy), 'auditLog': audit,
                       'args': [str(script), '--cutoff', UI_CUTOFF, '--policy-file', str(broker_policy),
                                '--policy-sha256', hashlib.sha256(broker_policy.read_bytes()).hexdigest(),
                                '--audit-log', audit]},
            'isolation': {'status': 'requires-runtime-preflight', 'adapter': retrieval_runtime.MACOS_ADAPTER,
                          'codexHome': str(self.control / 'codex-home'), 'workspace': str(self.workspace)},
        }), encoding='utf-8')
        (self.workspace / 'job-settings.json').write_text(json.dumps({'retrievalPolicyPath': str(path)}), encoding='utf-8')
        return path

    def test_a_policy_naming_the_removed_root_script_runs_the_broker_from_retrieval(self):
        path = self.write_policy(self.legacy)
        saved = path.read_bytes()
        policy = retrieval_runtime.load_policy(path)
        current = self.package / 'historical_search.py'
        self.assertEqual(policy['broker']['args'][0], str(current))
        self.assertEqual(policy['_legacyBrokerArgs0'], str(self.legacy))
        self.assertEqual(policy['broker']['args'][1:], json.loads(saved)['broker']['args'][1:])
        # The policy file is never rewritten, so every recorded hash of it still matches.
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(policy['_sha256'], hashlib.sha256(saved).hexdigest())
        self.assertEqual(set(policy['_brokerModuleSha256']),
                         {str(self.package / name) for name in retrieval_runtime.BROKER_MODULES})
        self.assertEqual(retrieval_runtime.configuration(policy, 'author')['mcp_servers']['historical_search']['args'],
                         policy['broker']['args'])
        self.assertEqual(retrieval_runtime.policy_for_workspace(self.workspace)['broker'], policy['broker'])
        # Any other missing script still fails closed, and so does a root script that exists again.
        for script in (self.checkout / 'elsewhere' / 'historical_search.py', self.root / 'historical_search.py'):
            with self.subTest(script=script), self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'script is missing'):
                retrieval_runtime.load_policy(self.write_policy(script))
        self.legacy.write_text('# a restored root script', encoding='utf-8')
        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'module is missing'):
            retrieval_runtime.load_policy(self.write_policy(self.legacy))

    def test_the_saved_author_config_moves_only_the_broker_path_before_start(self):
        policy = retrieval_runtime.load_policy(self.write_policy(self.legacy))
        config = Path(policy['isolation']['codexHome']) / 'author' / 'config.toml'
        config.parent.mkdir(parents=True)

        def saved(change=None):
            value = retrieval_runtime.configuration(policy, 'author')
            value['mcp_servers']['historical_search']['args'][0] = str(self.legacy)
            if change:
                change(value)
            config.write_text('\n'.join(retrieval_runtime._toml_table(value)) + '\n', encoding='utf-8')
            return value

        legacy = saved()
        retrieval_runtime.isolated_environment(policy, 'author', {})
        self.assertEqual(config.read_text(encoding='utf-8'), retrieval_runtime.config_text(policy, 'author'))
        # Live raw layers stay exact: Codex must load the rewritten file.
        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'raw user config'):
            retrieval_runtime._validate_user_configuration(policy, 'author', legacy, 'Historical raw user config')
        changes = {
            'other script': lambda value: value['mcp_servers']['historical_search']['args'].__setitem__(0, '/tmp/x.py'),
            'other argument': lambda value: value['mcp_servers']['historical_search']['args'].__setitem__(2, '2020-01-01T00:00:00Z'),
            'other command': lambda value: value['mcp_servers']['historical_search'].update(command='/bin/sh'),
            'other edit': lambda value: value['features'].update(view_image=True),
        }
        for label, change in changes.items():
            saved(change)
            before = config.read_bytes()
            with self.subTest(label), self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'configuration has changed'):
                retrieval_runtime.isolated_environment(policy, 'author', {})
            self.assertEqual(config.read_bytes(), before)
        # A current policy never accepts the legacy path either.
        current = retrieval_runtime.load_policy(self.write_policy(self.package / 'historical_search.py'))
        self.assertNotIn('_legacyBrokerArgs0', current)
        saved()
        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'configuration has changed'):
            retrieval_runtime.isolated_environment(current, 'author', {})

    def test_an_old_layout_checkout_hashes_its_own_six_modules(self):
        old = self.root / 'old-checkout'
        old.mkdir()
        for name in ('historical_search.py', *retrieval_runtime.LEGACY_BROKER_MODULES):
            (old / name).write_text(f'# old {name}', encoding='utf-8')
        path = self.write_policy(old / 'historical_search.py')
        policy = retrieval_runtime.load_policy(path)
        self.assertEqual(set(policy['_brokerModuleSha256']),
                         {str(old / name) for name in retrieval_runtime.LEGACY_BROKER_MODULES})
        self.assertEqual(retrieval.display_policy(path)['broker'], policy['broker'])
        (old / 'historical_cache.py').unlink()
        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'module is missing'):
            retrieval_runtime.load_policy(path)
        # The packaged layout always pins its package marker.
        (self.package / '__init__.py').unlink()
        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'module is missing'):
            retrieval_runtime.load_policy(self.write_policy(self.package / 'historical_search.py'))


class StaleCanaryTests(RuntimePolicyFixture):
    """stale-canary-sentinel-blocks-resume"""

    NONCE = 'stalecanarynonce'

    def rpc(self):
        report = {'nonce': self.NONCE, 'insideOk': True, 'insideWrite': True, 'protectedWrite': False,
                  'settingsWrite': False, 'settingsDelete': False, 'settingsRename': False,
                  'settingsCanariesRead': True, 'outsideRead': False, 'network': False, 'publicNetwork': False}
        observed = []

        def call(method, params):
            observed.append([(self.workspace / name).read_text() for name in CANARIES])
            return {'exitCode': 0, 'stdout': json.dumps(report)}
        rpc = Mock()
        rpc.call.side_effect = call
        return rpc, observed

    def run_canaries(self, rpc):
        with patch.object(retrieval_runtime.uuid, 'uuid4', return_value=Mock(hex=self.NONCE)), \
                patch.object(retrieval_runtime, '_public_network_control'):
            retrieval_runtime._sandbox_canaries(rpc, self.policy, 'author')

    def test_canaries_left_by_a_killed_preflight_do_not_block_resume(self):
        for name in CANARIES:
            (self.workspace / name).write_text('nonce from a killed controller')
        rpc, observed = self.rpc()
        self.run_canaries(rpc)
        self.assertEqual(observed, [[self.NONCE, self.NONCE]])
        for name in CANARIES:
            self.assertFalse((self.workspace / name).exists())

    def test_links_and_other_entries_are_refused_and_left_in_place(self):
        target = self.root / 'outside-target.txt'
        target.write_text('must not change')
        source = self.workspace / 'hard-link-source.txt'
        source.write_text('linked twice')
        plants = {
            'symlink': lambda path: path.symlink_to(target),
            'dangling symlink': lambda path: path.symlink_to(self.root / 'missing'),
            'hard link': lambda path: os.link(source, path),
            'directory': lambda path: path.mkdir(),
        }
        for label, plant in plants.items():
            for name in CANARIES:
                path = self.workspace / name
                with self.subTest(label, canary=name):
                    plant(path)
                    try:
                        rpc, _ = self.rpc()
                        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, 'inspect and remove'):
                            self.run_canaries(rpc)
                        rpc.call.assert_not_called()
                        self.assertTrue(path.is_symlink() or path.exists())
                        self.assertEqual([other for other in CANARIES if (self.workspace / other).exists()
                                          or (self.workspace / other).is_symlink()], [name])
                    finally:
                        for other in CANARIES:
                            other = self.workspace / other
                            if other.is_dir() and not other.is_symlink():
                                other.rmdir()
                            else:
                                other.unlink(missing_ok=True)
        self.assertEqual(target.read_text(), 'must not change')
        self.assertEqual(source.read_text(), 'linked twice')

    def test_sentinel_write_failure_leaves_no_untracked_canary(self):
        original = Path.open

        class FailingWrite:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *details):
                self.stream.close()

            def write(self, data):
                raise OSError('simulated full disk')

        def opener(path, mode='r', *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            return FailingWrite(stream) if mode == 'x' else stream

        rpc, _ = self.rpc()
        with patch.object(Path, 'open', opener):
            with self.assertRaisesRegex(OSError, 'simulated full disk'):
                self.run_canaries(rpc)
        for name in CANARIES:
            self.assertFalse((self.workspace / name).exists())


class UIRetrievalFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'runs' / 'job'
        self.workspace.mkdir(parents=True)
        (self.workspace / 'input.md').write_text('Prove a harmless identity.\n')
        self.codex = self.root / 'codex'
        self.codex.write_bytes(b'\xcf\xfa\xed\xfe' + b'unit-test-native-binary')
        self.codex.chmod(0o700)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('TCS_PROVER_UI_RETRIEVAL_STATE', None)
        resolver = patch.object(retrieval_runtime, 'codex_executable', return_value=str(self.codex))
        resolver.start()
        self.addCleanup(resolver.stop)
        for cache in (retrieval._CACHE, retrieval._POLICY_MEMO):
            cache.clear()
            self.addCleanup(cache.clear)

    def prepare(self):
        return retrieval.prepare_policy(self.workspace, UI_CUTOFF, controller_root=self.root / 'controller')

    def info(self, policy_path, **options):
        retrieval._CACHE.clear()  # Expire the five-second dashboard cache.
        return retrieval.public_retrieval_info(policy_path, run_dir=self.workspace, **options)

    def update_codex(self):
        with self.codex.open('ab') as stream:
            stream.write(b'-updated')


class WindowsCapabilityTests(UIRetrievalFixture):
    """windows-historical-advertised-but-unlaunchable"""

    def test_windows_does_not_offer_historical_search_but_keeps_other_modes(self):
        with patch.object(isolated_compiler.sys, 'platform', 'win32'):
            # A real Windows host; os.name stays patched only here because
            # pathlib picks WindowsPath from it, so build no Path inside.
            with patch.object(retrieval.os, 'name', 'nt'):
                self.assertFalse(isolated_compiler.supported())
                capabilities = retrieval.capabilities()
                self.assertFalse(capabilities['historicalAvailable'])
                self.assertEqual(capabilities['reason'], retrieval.PLATFORM_REASON)
                with self.assertRaisesRegex(ValueError, 'requires macOS'):
                    retrieval.validate_options('historical', UI_CUTOFF)
            disabled = {'retrievalMode': 'disabled', 'retrievalCutoffUtc': '-1'}
            self.assertEqual(retrieval.validate_options('disabled', '-1'), disabled)
            self.assertEqual(retrieval.validate_options('historical', '-1'), disabled)
            self.assertEqual(retrieval.validate_options('live', ''), {'retrievalMode': 'live', 'retrievalCutoffUtc': ''})
            with patch.object(retrieval_runtime, 'load_policy') as load:
                with self.assertRaisesRegex(ValueError, 'requires macOS'):
                    retrieval.preflight_compiler(str(self.root / 'retrieval-policy.json'))
                load.assert_not_called()
        with patch.object(isolated_compiler.sys, 'platform', 'darwin'):
            self.assertTrue(retrieval.capabilities()['historicalAvailable'])

    def test_compiler_refuses_non_macos_platform_even_with_macos_adapter(self):
        policy = {'isolation': {'adapter': isolated_compiler.ADAPTER, 'workspace': str(self.workspace)}}
        with patch.object(isolated_compiler.sys, 'platform', 'win32'), \
                patch.object(isolated_compiler, 'toolchain', side_effect=AssertionError('no toolchain lookup')):
            with self.assertRaises(isolated_compiler.CompilerIsolationError):
                isolated_compiler.compile_command(policy, {}, isolated_compiler.LATEX_ARGV, self.workspace)


class PostErrorTests(UIRetrievalFixture):
    """post-handler-drops-runtimeerror (ui/retrieval.py part)"""

    def test_codex_resolution_failure_is_a_reportable_value_error(self):
        message = ('Historical execution requires an absolute native Mach-O codex binary outside '
                   'the run workspace; script shims are not supported.')
        with patch.object(retrieval_runtime, 'codex_executable',
                          side_effect=retrieval_runtime.RetrievalPolicyError(message)):
            with self.assertRaises(ValueError) as caught:
                self.prepare()
        self.assertEqual(str(caught.exception), message)
        self.assertFalse((self.root / 'controller').exists())

    def test_compiler_preflight_timeout_is_a_reportable_value_error(self):
        with patch.object(retrieval_runtime, 'load_policy', return_value={}), \
                patch.object(isolated_compiler, 'preflight',
                             side_effect=subprocess.TimeoutExpired(['/usr/bin/perl'], 15)):
            with self.assertRaisesRegex(ValueError, 'compiler preflight failed'):
                retrieval.preflight_compiler(str(self.root / 'retrieval-policy.json'))


class DashboardMetadataTests(UIRetrievalFixture):
    """metadata-failure-erases-cutoff-display and dashboard-rehashes-codex-binary"""

    def test_saved_cutoff_stays_visible_when_policy_metadata_fails(self):
        policy_path = self.prepare()
        loaded = self.info(policy_path, saved_cutoff=UI_CUTOFF)
        self.assertTrue(loaded['available'])
        self.assertEqual(loaded['cutoffExclusiveUtc'], UI_CUTOFF)
        self.update_codex()
        failed = self.info(policy_path, saved_cutoff=UI_CUTOFF)
        self.assertEqual((failed['mode'], failed['available'], failed['fixed']), ('historical', False, True))
        self.assertEqual(failed['cutoffExclusiveUtc'], UI_CUTOFF)
        self.assertIn('metadata is unavailable', failed['error'])
        for saved in ('', '-1', '2025-06-01', None):
            with self.subTest(saved=saved):
                self.assertEqual(self.info(policy_path, saved_cutoff=saved)['cutoffExclusiveUtc'], '')

    def test_loaded_policy_cutoff_overrides_the_saved_value(self):
        policy_path = self.prepare()
        info = self.info(policy_path, saved_cutoff='2024-01-01T00:00:00Z')
        self.assertTrue(info['available'])
        self.assertEqual(info['cutoffExclusiveUtc'], UI_CUTOFF)

    def test_dashboard_refresh_does_not_rehash_an_unchanged_codex_binary(self):
        policy_path = self.prepare()
        with patch.object(discovery, '_hash', wraps=discovery._hash) as hashed:
            self.assertTrue(self.info(policy_path)['available'])
            self.assertTrue(self.info(policy_path)['available'])
            self.assertEqual(hashed.call_count, 1)
            self.update_codex()
            self.assertFalse(self.info(policy_path)['available'])
            self.assertEqual(hashed.call_count, 2)

    def test_display_memo_rechecks_changed_controller_files(self):
        policy_path = self.prepare()
        self.assertTrue(self.info(policy_path)['available'])
        broker = Path(json.loads(Path(policy_path).read_text())['broker']['policyFile'])
        broker.write_text(broker.read_text() + ' ')
        self.assertFalse(self.info(policy_path)['available'])

    def test_display_memo_returns_independent_copies(self):
        path = Path(self.prepare())
        first = retrieval.display_policy(path)
        first['cutoffExclusiveUtc'] = 'changed by a caller'
        first['broker']['args'].clear()
        second = retrieval.display_policy(path)
        self.assertEqual(second['cutoffExclusiveUtc'], UI_CUTOFF)
        self.assertEqual(len(second['broker']['args']), 9)
        second['broker']['args'].clear()
        self.assertEqual(len(retrieval.display_policy(path)['broker']['args']), 9)


class SubagentTrajectoryAuditTests(TrajectoryAuditFixture):
    """auditor-subagent-thread-mismatch"""

    CHILD = 'fixture-subagent-thread'

    def subagent(self, **options):
        record = tool_event(thread=self.CHILD, call_id='call-subagent-1', **options)
        record['root'] = False
        return record

    def codes(self, report):
        return {issue['code'] for issue in report['issues']}

    def test_author_subagent_replies_are_reconciled_without_thread_mismatch(self):
        self.trajectory_records += [self.subagent(status='inProgress', method='item/started'), self.subagent()]
        report = self.audit()
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['counts']['matched_documents'], 2)
        self.assertEqual(report['thread_ids'], sorted([THREAD, self.CHILD]))

    def test_subagent_reply_without_broker_evidence_still_fails(self):
        self.trajectory_records.append(self.subagent(documents=[dict(DOCUMENT, sha256='b' * 64)]))
        codes = self.codes(self.audit())
        self.assertIn('source_evidence_missing', codes)
        self.assertNotIn('thread_mismatch', codes)

    def test_root_or_unmarked_foreign_thread_is_still_a_mismatch(self):
        for root in (True, None):
            record = tool_event(thread='fixture-thread-P02')
            if root is None:
                record.pop('root')
            with self.subTest(root=root):
                self.trajectory_records = [record]
                self.assertIn('thread_mismatch', self.codes(self.audit()))


class DiscoveryReconnectTests(unittest.TestCase):
    """discovery-reconnect-error-rejects-completed-trace"""

    URL = 'https://example.org/paper.pdf'
    RECONNECT = {'type': 'error', 'message': 'Reconnecting... 1/5 (stream disconnected before completion)'}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def trace(self, *extra, completed=True):
        final = json.dumps({'urls': [self.URL]})
        events = [{'type': 'thread.started', 'thread_id': 't1'}, {'type': 'turn.started'}, *extra,
                  {'type': 'item.completed', 'item': {'id': 'w1', 'type': 'web_search', 'status': 'completed',
                                                     'action': {'type': 'search', 'query': 'mathematics'},
                                                     'results': [{'url': self.URL}]}},
                  {'type': 'item.completed', 'item': {'id': 'a1', 'type': 'agent_message', 'text': final}}]
        if completed:
            events.append({'type': 'turn.completed', 'usage': {}})
        trace, output = self.root / 'events.jsonl', self.root / 'urls.json'
        trace.write_text(''.join(json.dumps(event) + '\n' for event in events))
        output.write_text(final)
        return trace, output

    def test_reconnect_followed_by_completed_grounded_turn_is_accepted(self):
        self.assertEqual(discovery._trace(*self.trace(self.RECONNECT, self.RECONNECT), 3), ([self.URL], 1, 0))

    def test_reconnect_never_excuses_failed_or_incomplete_turns(self):
        failed = {'type': 'turn.failed', 'error': {'message': 'stream closed'}}
        for extra, completed in (((self.RECONNECT, failed), True), ((self.RECONNECT,), False)):
            with self.subTest(extra=extra, completed=completed), self.assertRaises(discovery.DiscoveryError):
                discovery._trace(*self.trace(*extra, completed=completed), 3)

    def test_other_error_events_are_still_rejected(self):
        for event in ({'type': 'error', 'message': 'Stream disconnected'}, {'type': 'error'},
                      {'type': 'error', 'message': ['Reconnecting...']},
                      {'type': 'error', 'message': ' Reconnecting...'}):
            with self.subTest(event=event), self.assertRaises(discovery.DiscoveryError):
                discovery._trace(*self.trace(event), 3)


class BlockingInput:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, limit):
        return self.lines.get(timeout=10)

    def send(self, message):
        self.lines.put(json.dumps(message) + '\n')


class LineSink(io.RawIOBase):
    """Bytes after a real TextIOWrapper's strict encoding, one record per line."""

    def __init__(self, encoding):
        super().__init__()
        self.encoding, self.pending, self.records = encoding, b'', queue.Queue()

    def writable(self):
        return True

    def write(self, data):
        self.pending += bytes(data)
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            self.records.put(json.loads(line.decode(self.encoding)))
        return len(data)


class RecordingAudit:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, fields))


class StaticEngine:
    operation_timeout = 2.0

    def __init__(self, text):
        self.text, self.audit = text, RecordingAudit()

    def search(self, query):
        return {'results': [{'text': self.text if query == 'unencodable' else 'plain'}]}


class EncodingTests(unittest.TestCase):
    """windows-broker-stdio-not-utf8 and pdf-lone-surrogates-mute-broker"""

    setUp = fixtures.EngineTests.setUp
    engine = fixtures.EngineTests.engine
    corpus_engine = fixtures.EngineTests.corpus_engine

    def serve(self, engine, encoding):
        source = BlockingInput()
        sink = LineSink(encoding)
        output = io.TextIOWrapper(io.BufferedWriter(sink), encoding=encoding, errors='strict',
                                  newline='\n', write_through=True)
        server = hs.StdioMCPServer(engine, engine_factory=lambda: engine)
        thread = threading.Thread(target=server.serve, args=(source, output), daemon=True)
        thread.start()

        def close():
            source.lines.put('')
            thread.join(timeout=5)
        self.addCleanup(close)

        def exchange(message):
            source.send(message)
            return sink.records.get(timeout=5)
        self.assertIn('result', exchange({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}))
        return exchange

    @staticmethod
    def search(request_id, query):
        return {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                'params': {'name': 'search', 'arguments': {'query': query}}}

    def assert_stays_responsive(self, engine, encoding):
        exchange = self.serve(engine, encoding)
        failed = exchange(self.search(2, 'unencodable'))
        self.assertEqual(failed['id'], 2)
        self.assertTrue(failed['result']['isError'])
        self.assertEqual(failed['result']['content'][0]['text'], 'Historical retrieval failed closed.')
        self.assertEqual(exchange({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}), {'jsonrpc': '2.0', 'id': 3, 'result': {}})
        self.assertFalse(exchange(self.search(4, 'readable'))['result']['isError'])

    def test_lone_surrogate_result_fails_closed_and_is_audited_as_error(self):
        engine = StaticEngine('excerpt with a lone \ud835 surrogate')
        self.assert_stays_responsive(engine, 'utf-8')
        ready = [fields['is_error'] for event, fields in engine.audit.events if event == 'mcp_response_ready']
        self.assertEqual(ready, [True, False])

    def test_reply_a_narrow_stream_cannot_encode_is_replaced_without_closing_it(self):
        self.assert_stays_responsive(StaticEngine('Greek χ is outside cp1252'), 'cp1252')

    def test_well_formed_text_rejoins_pairs_and_replaces_lone_surrogates(self):
        self.assertEqual(hs.well_formed_text('a𝐀b\ud835c\udc00'), 'a\U0001d400b�c�')
        self.assertEqual(hs.well_formed_text('plain ﬁnite χ'), 'plain ﬁnite χ')

    @unittest.skipUnless(hs.pdf_supported(), 'Optional pypdf>=6.20.1,<7 not installed')
    def test_pdf_lone_surrogates_are_replaced_before_navigation_and_digests(self):
        class Page:
            def get_contents(self):
                return None

            def extract_text(self):
                return 'Graph lemma \ud835 holds for 𝐀 graphs.'

        class Reader:
            def __init__(self, *args, **kwargs):
                self.is_encrypted, self.pages = False, [Page()]
                self.metadata = SimpleNamespace(title='Archived \udc00 paper')

        paper = replace(snapshot(body=fixtures.pdf_fixture()), mime_type='application/pdf')
        engine = self.engine(FakeClient(captures=[paper]))
        with patch('pypdf.PdfReader', Reader):
            self.assertEqual(hs.extract_pdf(paper.body),
                             ('Archived � paper', 'Graph lemma � holds for \U0001d400 graphs.'))
            opened = engine.open(SOURCE)['result']
            found = engine.find(SOURCE, 'holds')['result']
            searched = engine.search('graph lemma')['results'][0]
        self.assertEqual(opened['text'], 'Graph lemma � holds for \U0001d400 graphs.')
        self.assertEqual(opened['title'], 'Archived � paper')
        self.assertEqual(found['matches'][0]['offset'], opened['text'].index('holds'))
        json.dumps([opened, found, searched], ensure_ascii=False).encode('utf-8')
        events = [json.loads(line)['event'] for line in self.audit.read_text().splitlines()]
        self.assertIn('open_completed', events)
        self.assertIn('find_completed', events)

    def test_real_broker_uses_utf8_stdio_when_platform_pipes_default_to_cp1252(self):
        body = ('<html><head><title>Archived χ-bound</title></head>'
                '<body>Lemma: the χ-bound holds for café graphs.</body></html>').encode('utf-8')
        self.corpus_engine([snapshot(body=body)])
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'open', 'arguments': {'url': SOURCE}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'find', 'arguments': {'url': SOURCE, 'pattern': 'χ-bound'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'},
        ]
        completed = subprocess.run(
            [sys.executable, '-u', str(Path(hs.__file__)), '--cutoff', fixtures.CUTOFF,
             '--policy-file', str(self.policy_path), '--policy-sha256', hs.sha256(self.policy_path.read_bytes()),
             '--audit-log', str(self.root / 'subprocess-audit.jsonl')],
            input=''.join(json.dumps(message, ensure_ascii=False) + '\n' for message in messages).encode('utf-8'),
            capture_output=True, timeout=30, check=False,
            env={**os.environ, 'PYTHONIOENCODING': 'cp1252:surrogateescape'})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = {item['id']: item for item in map(json.loads, completed.stdout.decode('utf-8').splitlines())}
        self.assertEqual(set(responses), {1, 2, 3, 4})
        opened = json.loads(responses[2]['result']['content'][0]['text'])['result']
        self.assertIn('the χ-bound holds for café graphs', opened['text'])
        found = json.loads(responses[3]['result']['content'][0]['text'])['result']
        self.assertEqual(len(found['matches']), 2)
        self.assertEqual(responses[4]['result'], {})

    def test_cli_reconfigures_stdio_to_utf8_before_serving(self):
        self.policy_path.write_text(json.dumps({'schema_version': 1, 'provider': 'duckduckgo_wayback'}))
        stdin, stdout = Mock(), Mock()
        with patch.object(hs.sys, 'stdin', stdin), patch.object(hs.sys, 'stdout', stdout), \
                patch.object(hs, 'StdioMCPServer') as server:
            self.assertEqual(hs.main(['--cutoff', fixtures.CUTOFF, '--policy-file', str(self.policy_path),
                                      '--audit-log', str(self.audit)]), 0)
        stdin.reconfigure.assert_called_once_with(encoding='utf-8', errors='replace')
        stdout.reconfigure.assert_called_once_with(encoding='utf-8', errors='replace', newline='\n')
        server.return_value.serve.assert_called_once_with(stdin, stdout)


class ArchiveCacheAndParsingTests(unittest.TestCase):
    """shared-capture-cache-cross-cutoff-overwrite, htmlparser-assertion-aborts-search,
    search-excerpt-casefold-offset and per-call-engine-disables-memory-caches"""

    setUp = fixtures.EngineTests.setUp
    engine = fixtures.EngineTests.engine
    EARLIER = '2026-06-01T00:00:00Z'

    def shared_policy(self, provider='scholarly_wayback'):
        return {'schema_version': 1, 'provider': provider,
                'rate_limit_directory': str(self.root.resolve() / 'pacing'),
                'solver_workspace': str(self.root.resolve() / 'solver')}

    def events(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def test_earlier_cutoff_job_cannot_replace_later_jobs_pinned_capture(self):
        newer, older = snapshot(), snapshot('20250101000000', body=OLDER_BODY)
        policy = self.shared_policy()
        later = self.engine(FakeClient(captures=[newer, older]), policy=policy).open(SOURCE)['result']
        self.assertEqual(later['sha256'], newer.body_sha256)
        earlier = self.engine(FakeClient(captures=[newer, older]), policy=policy, cutoff=self.EARLIER)
        self.assertEqual(earlier.open(SOURCE)['result']['sha256'], older.body_sha256)
        offline = FakeClient(captures=[])
        with patch.object(offline, 'get', side_effect=AssertionError('No archive request may occur')):
            pinned = self.engine(offline, policy=policy).open(SOURCE, sha256=newer.body_sha256)['result']
        self.assertEqual(pinned['sha256'], newer.body_sha256)

    def test_older_or_placeholder_entries_are_still_replaced(self):
        engine = self.engine(FakeClient(), policy=self.shared_policy())
        cache, current = engine.provider.persistent_cache, snapshot()
        stale = replace(snapshot('20261001120000'), memento_datetime='Fri, 02 Oct 2026 12:00:00 GMT')
        for existing in (snapshot('20250101000000', body=OLDER_BODY), snapshot('20261001120000', body=CAPTCHA), stale):
            with self.subTest(capture=existing.capture, body=existing.body[:40]):
                cache.put(SOURCE, cache_record(existing))
                engine.provider._store_capture(current)
                self.assertEqual(cache.get(SOURCE), cache_record(current))

    def test_malformed_marked_section_rejects_only_that_capture(self):
        with self.assertRaises(hs.HistoricalSearchError) as caught:
            hs.extract_text(MALFORMED, 'text/html')
        self.assertEqual(caught.exception.code, 'unreadable_capture')
        other = 'https://example.net/broken'
        client = FakeClient(captures=[snapshot(), snapshot(url=other, body=MALFORMED)],
                            discoveries=[{'url': SOURCE}, {'url': other}])
        result = self.engine(client).search('graph lemma')
        self.assertEqual([item['url'] for item in result['results']], [SOURCE])
        events = self.events()
        rejected = [event for event in events if event['event'] == 'capture_rejected']
        self.assertEqual([event['url_sha256'] for event in rejected], [hs.sha256(other.encode())])
        self.assertEqual(events[-1]['event'], 'search_completed')
        self.assertEqual(events[-1]['returned'], 1)

    def test_open_falls_back_to_an_older_readable_capture(self):
        newest, older = snapshot('20261001120000', body=MALFORMED), snapshot()
        self.assertEqual(self.engine(FakeClient(captures=[newest, older])).open(SOURCE)['result']['sha256'],
                         older.body_sha256)

    def test_malformed_public_search_markup_is_a_static_discovery_error(self):
        client = fixtures.FakeDuckDuckGoClient(body=b'<html><body>t<![0,1]</body></html>')
        engine = self.engine(client, policy={'schema_version': 1, 'provider': 'duckduckgo_wayback'})
        with self.assertRaisesRegex(hs.HistoricalSearchError, 'markup could not be validated'):
            engine.provider.candidates('graph lemma')

    def test_unfolded_offset_matches_a_character_by_character_reference(self):
        def reference(text, folded_offset):
            consumed = 0
            for index, character in enumerate(text):
                consumed += len(character.casefold())
                if consumed > folded_offset:
                    return index
            return len(text)

        for text in ('ﬁ' * 4100 + ' finite fields', 'Straße İstanbul ŉ ﬃ ' * 300 + 'target',
                     'plain text', 'ΣΑΣ ΟΔΟΣ' * 50):
            folded = len(text.casefold())
            offsets = set(range(0, folded, 97)) | {0, folded - 1} | set(range(8180, min(folded, 8220)))
            for offset in sorted(item for item in offsets if 0 <= item < folded):
                self.assertEqual(hs.unfolded_offset(text, offset), reference(text, offset), (text[:20], offset))

    def test_search_excerpt_contains_match_after_casefold_expansions(self):
        prefix = 'ﬁ' * 3000 + ' ß' * 200 + ' '
        content = prefix + 'The target theorem concerns ﬁnite ﬁelds.'
        body = ('<html><body>' + content + '</body></html>').encode('utf-8')
        for query, expected in (('target theorem', 'The target theorem'), ('finite fields', 'ﬁnite ﬁelds')):
            with self.subTest(query=query):
                result = self.engine(FakeClient(captures=[snapshot(body=body)])).search(query)['results'][0]
                self.assertIn(expected, result['text'])

    def test_cli_per_call_engines_share_only_bounded_memory_caches(self):
        self.policy_path.write_text(json.dumps(self.shared_policy()))
        engines = []

        class Server:
            def __init__(self, engine, *, engine_factory):
                self.engine, self.factory = engine, engine_factory

            def serve(self, input_stream, output_stream):
                engines.extend([self.engine, self.factory(), self.factory()])

        with patch.object(hs, 'StdioMCPServer', Server), patch.object(hs.sys, 'stdin', io.StringIO()), \
                patch.object(hs.sys, 'stdout', io.StringIO()):
            self.assertEqual(hs.main(['--cutoff', fixtures.CUTOFF, '--policy-file', str(self.policy_path),
                                      '--audit-log', str(self.audit)]), 0)
        base, first, second = (engine.provider for engine in engines)
        for provider in (first, second):
            for name in ('cache_lock', 'cache', 'negative_cache', 'discovery_cache'):
                self.assertIs(getattr(provider, name), getattr(base, name), name)
            for name in ('client', 'persistent_cache'):
                self.assertIsNot(getattr(provider, name), getattr(base, name), name)
            self.assertIsNot(provider.audit.__self__, base.audit.__self__)
        self.assertIsNot(first.client, second.client)

    def test_shared_memory_caches_avoid_repeat_archive_requests(self):
        first, second = self.engine(FakeClient()), self.engine(FakeClient())
        second.provider.share_memory_caches(first.provider)
        missing = 'https://example.org/missing'
        first.open(SOURCE)
        with self.assertRaises(hs.HistoricalSearchError):
            first.open(missing)
        self.assertIn('Graph lemma', second.open(SOURCE)['result']['text'])
        with self.assertRaises(hs.HistoricalSearchError) as caught:
            second.open(missing)
        self.assertEqual(caught.exception.code, 'no_capture')
        self.assertEqual(second.client.calls, [])
        other = self.engine(FakeClient(), cutoff=self.EARLIER)
        other.provider.share_memory_caches(first.provider)
        self.assertIsNot(other.provider.cache, first.provider.cache)

    def test_shared_scholarly_discovery_cache_skips_repeat_discovery(self):
        policy = self.shared_policy()
        with patch('retrieval.scholarly_discovery._arxiv_urls', return_value=[SOURCE]) as arxiv, \
                patch('retrieval.scholarly_discovery._crossref_urls', return_value=[]):
            first, second = self.engine(FakeClient(), policy=policy), self.engine(FakeClient(), policy=policy)
            second.provider.share_memory_caches(first.provider)
            self.assertEqual(len(first.search('graph lemma')['results']), 1)
            self.assertEqual(len(second.search('graph lemma')['results']), 1)
        self.assertEqual(arxiv.call_count, 1)


if __name__ == '__main__':
    unittest.main()
