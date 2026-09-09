"""Generic YAML command actions preserve output and cannot reuse stale results."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as runtime


class CommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.config = {'argv': ['builder', '{state.input}'], 'cwd': 'output',
                       'env': {'CHECK_MODE': 'strict'}, 'timeout': 3,
                       'produces': ['result.txt'], 'log': 'command.log', 'result': 'build'}
        self.context = {'state': {'input': 'source.txt'}}

    def test_missing_command_is_explicit(self):
        with patch.object(runtime.shutil, 'which', return_value=None):
            result = runtime._run_command(self.config, self.context)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('builder', result['diagnostic'])
        self.assertIn('unavailable', (self.directory/'output/command.log').read_text())

    def test_success_requires_output_and_uses_configured_argv_and_environment(self):
        def command(argv, **kwargs):
            self.assertEqual(argv, ['/tools/builder', 'source.txt'])
            self.assertEqual(kwargs['timeout'], 3)
            self.assertEqual(kwargs['env']['CHECK_MODE'], 'strict')
            self.assertNotIn('shell', kwargs)
            (kwargs['cwd']/'result.txt').write_text('Built result')
            return subprocess.CompletedProcess(argv, 0, 'Complete output ∀x')
        with patch.object(runtime.shutil, 'which', return_value='/tools/builder'), patch.object(runtime.subprocess, 'run', side_effect=command):
            runtime._apply_actions([{'command': self.config}], self.context, 'build', 0)
        self.assertEqual(self.context['state']['build']['status'], 'pass')
        self.assertIn('Complete output ∀x', (self.directory/'output/command.log').read_text())

    def test_stale_result_does_not_make_an_empty_run_successful(self):
        (self.directory/'output').mkdir()
        (self.directory/'output/result.txt').write_text('Old result')
        with patch.object(runtime.shutil, 'which', return_value='/tools/builder'), patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'No new output')):
            result = runtime._run_command(self.config, self.context)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('missing or empty', result['diagnostic'])

    def test_timeout_preserves_partial_output(self):
        with patch.object(runtime.shutil, 'which', return_value='/tools/builder'), patch.object(runtime.subprocess, 'run', side_effect=subprocess.TimeoutExpired('builder', 3, output=b'partial output')):
            result = runtime._run_command(self.config, self.context)
        self.assertEqual(result['status'], 'fail')
        log = (self.directory/'output/command.log').read_text()
        self.assertIn('3 seconds', log)
        self.assertIn('partial output', log)

    def test_storage_errors_propagate(self):
        with patch.object(runtime.shutil, 'which', return_value=None), patch.object(runtime, '_private_atomic_write', side_effect=PermissionError('disk full')):
            with self.assertRaisesRegex(PermissionError, 'disk full'):
                runtime._run_command(self.config, self.context)

    def test_invalid_timeouts_and_escape_outputs_are_rejected(self):
        for timeout in (0, -1, True, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                runtime._check_actions([{'command': {**self.config, 'timeout': timeout}}])
        with self.assertRaises(ValueError):
            runtime._check_actions([{'command': {**self.config, 'produces': ['../outside.txt']}}])


if __name__ == '__main__':
    unittest.main()
