"""The UI selects its local Python before importing version-specific modules."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import web_ui


class LauncherTests(unittest.TestCase):
    def test_local_environment_receives_arguments_before_ui_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copyfile(web_ui.__file__, root / 'web_ui.py')
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')],
                           check=True, capture_output=True, timeout=30)
            (root / 'ui').mkdir()
            (root / 'ui/__init__.py').write_text('')
            (root / 'ui/cli.py').write_text(
                'import json, sys\n'
                'def main():\n'
                '    print(json.dumps({"prefix":sys.prefix,"args":sys.argv[1:],"noBytecode":sys.dont_write_bytecode}))\n'
                '    return 7\n')
            result = subprocess.run([sys.executable, '-B', str(root / 'web_ui.py'), '--no-browser', 'two words.md'],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 7, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(Path(report['prefix']).resolve(), (root / '.venv').resolve())
            self.assertEqual(report['args'], ['--no-browser', 'two words.md'])
            self.assertTrue(report['noBytecode'])

    def test_unsupported_interpreter_reports_setup_before_importing_ui(self):
        class OldVersion(tuple):
            major, minor = 3, 9
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()) as output, \
                patch.object(web_ui, '__file__', str(Path(temporary) / 'web_ui.py')), \
                patch.object(sys, 'version_info', OldVersion((3, 9, 16))), \
                patch.dict(sys.modules, {'ui.cli': None}):
            self.assertEqual(web_ui.main(), 1)
        self.assertIn('requires Python 3.11', output.getvalue())
        self.assertIn('this interpreter is 3.9', output.getvalue())
        self.assertIn('-m venv .venv', output.getvalue())

    def test_already_selected_environment_does_not_reexec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(web_ui, '__file__', str(root / 'web_ui.py')), \
                    patch.object(sys, 'prefix', str(root / '.venv')), \
                    patch.object(os, 'execv') as replace, \
                    patch.dict(sys.modules, {'ui.cli': SimpleNamespace(main=lambda: 0)}):
                self.assertEqual(web_ui.main(), 0)
            replace.assert_not_called()


if __name__ == '__main__':
    unittest.main()
