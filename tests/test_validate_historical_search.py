"""Offline checks for concurrent load orchestration and evidence validation."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import validate_historical_search as validator


CUTOFF = '2026-10-02T14:13:12Z'
DOCUMENT = {'url': 'https://example.org/paper', 'capture_time': '2026-01-01T00:00:00Z',
            'sha256': 'a' * 64, 'text': 'Readable archived scholarly text.'}


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.policy = self.root / 'base.json'
        self.policy.write_text(json.dumps({'schema_version': 1, 'provider': 'scholarly_wayback',
                                          'operation_timeout_seconds': 300, 'max_candidates': 6,
                                          'deny_url_prefixes': ['https://excluded.example/benchmark']}))
        self.output = self.root / 'evidence'

    def response(self, policy, cutoff, audit, name, arguments):
        document = deepcopy(DOCUMENT)
        audit.write_text(json.dumps({'event': 'capture_verified' if name == 'search' else 'open_completed',
                                      'url_sha256': hashlib.sha256(document['url'].encode()).hexdigest(),
                                      'body_sha256': document['sha256'], 'capture': document['capture_time']}) + '\n')
        return {'cutoff': cutoff, 'results': [document]} if name == 'search' else {'cutoff': cutoff, 'result': document}

    def run_validation(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return validator.validate(self.policy, CUTOFF, self.output, **kwargs)

    def test_ten_searches_overlap_with_distinct_policies_and_one_cold_shared_state(self):
        entered = threading.Barrier(10)
        calls = []
        lock = threading.Lock()

        def respond(*args):
            with lock:
                calls.append(args)
            if args[3] == 'search':
                entered.wait(timeout=5)  # Fails if orchestration is sequential.
            return self.response(*args)

        with patch.object(validator, 'call', side_effect=respond):
            report = self.run_validation(concurrent10=True)
        self.assertTrue(report['passed'])
        self.assertEqual(len(calls), 20)
        searches = [args for args in calls if args[3] == 'search']
        self.assertEqual(len({args[4]['query'] for args in searches}), 10)
        self.assertEqual(len({args[0] for args in searches}), 10)
        policies = [json.loads(args[0].read_text()) for args in searches]
        self.assertEqual(len({policy['rate_limit_directory'] for policy in policies}), 1)
        self.assertEqual(len({policy['solver_workspace'] for policy in policies}), 10)
        self.assertTrue(all(policy['operation_timeout_seconds'] == 300 and policy['max_candidates'] == 6 for policy in policies))
        self.assertEqual(report['maxCandidates'], 6)
        self.assertEqual(report['operationTimeoutSeconds'], 300)
        self.assertTrue(all(policy['deny_url_prefixes'] == ['https://excluded.example/benchmark'] for policy in policies))
        self.assertTrue(report['archiveCacheInitiallyEmpty'])
        self.assertEqual(report['evidence']['verifiedCaptures'], 10)

    def test_one_failure_does_not_discard_other_nine_trials_or_its_error_artifact(self):
        def respond(*args):
            if args[3] == 'search' and args[4]['query'] == validator.CONCURRENT_QUERIES[0]:
                raise RuntimeError('Provider unavailable.')
            return self.response(*args)

        with patch.object(validator, 'call', side_effect=respond):
            report = self.run_validation(concurrent10=True)
        self.assertFalse(report['passed'])
        self.assertEqual(sum(row['passed'] for row in report['trials']), 9)
        self.assertTrue((self.output / 'trial-01/search-error.json').is_file())
        self.assertEqual(len(json.loads((self.output / 'report.json').read_text())['trials']), 10)

    def test_future_capture_and_mismatched_open_hash_fail_validation(self):
        for mutation in ('future', 'hash'):
            with self.subTest(mutation=mutation):
                self.output = self.root / mutation

                def respond(*args):
                    result = self.response(*args)
                    if mutation == 'future':
                        result['results'][0]['capture_time'] = CUTOFF
                    elif args[3] == 'open':
                        result['result']['sha256'] = 'b' * 64
                    return result

                with patch.object(validator, 'call', side_effect=respond):
                    report = self.run_validation(repeats=1)
                self.assertFalse(report['passed'])
                self.assertEqual(report['trials'][0]['failedPhase'], 'search' if mutation == 'future' else 'open')

    def test_valid_response_without_corresponding_audit_capture_is_rejected(self):
        def respond(*args):
            result = self.response(*args)
            args[2].write_text('{}\n')
            return result

        with patch.object(validator, 'call', side_effect=respond):
            report = self.run_validation(repeats=1)
        self.assertFalse(report['passed'])
        self.assertIn('matching broker audit', report['trials'][0]['error'])

    def test_existing_evidence_is_preserved(self):
        self.output.mkdir()
        evidence = self.output / 'report.json'
        evidence.write_text('previous evidence')
        with self.assertRaisesRegex(ValueError, 'must be empty'):
            self.run_validation(concurrent10=True)
        self.assertEqual(evidence.read_text(), 'previous evidence')

    def test_source_changes_during_validation_invalidate_overall_result(self):
        with patch.object(validator, 'call', side_effect=self.response), \
             patch.object(validator, 'source_hashes', side_effect=[{'broker': 'before'}, {'broker': 'after'}]):
            report = self.run_validation(repeats=1)
        self.assertTrue(report['trials'][0]['passed'])
        self.assertFalse(report['brokerSourcesUnchanged'])
        self.assertFalse(report['passed'])


if __name__ == '__main__':
    unittest.main()
