"""A broken tool host must stop goal execution instead of spending repeat turns."""
import io
import json
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import workflow_runner as runtime
from ui import server


class ToolHealthTests(unittest.TestCase):
    def rpc(self, messages):
        process = Mock(stdout=io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)),
                       stderr=io.StringIO(''))
        return runtime.RPC(process)

    def test_unavailable_host_warning_aborts_before_next_rpc_response(self):
        rpc = self.rpc([
            {'method': 'warning', 'params': {'message': 'Code Mode is unavailable because code-mode host is disabled.'}},
            {'id': 1, 'result': {'thread': {'id': 'never-start-a-model-turn'}}},
        ])
        with self.assertRaisesRegex(runtime.Error, 'Required model tool runtime is unavailable'):
            rpc.call('thread/start', {})

    def test_model_quoting_old_tool_failure_does_not_abort_recovery(self):
        message = {'method': 'item/completed', 'params': {'item': {
            'type': 'agentMessage', 'text': 'Previously Code Mode is unavailable; testing repaired tools now.'}}}
        self.assertEqual(self.rpc([message]).read(), message)

    def test_other_warning_is_retained(self):
        message = {'method': 'warning', 'params': {'message': 'Under-development features enabled.'}}
        self.assertEqual(self.rpc([message]).read(), message)

    def test_settings_readers_keep_complete_old_snapshot_during_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            app = server.App(runs=Path(folder))
            app.run_dir = Path(folder)
            app._save_job_settings({'goalThreadId': 'original-thread'})
            path = app.run_dir / server.JOB_SETTINGS_FILENAME
            with path.open() as existing_reader:
                app._save_job_settings({'goalThreadId': 'same-thread-resumed'})
                self.assertEqual(json.load(existing_reader)['goalThreadId'], 'original-thread')
            self.assertEqual(json.loads(path.read_text())['goalThreadId'], 'same-thread-resumed')

    def test_protected_settings_are_staged_privately_outside_author_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            app = server.App(runs=root)
            app.run_dir = root / 'run'
            app._save_job_settings({'goalThreadId': 'original-thread'})
            target = app.run_dir / server.JOB_SETTINGS_FILENAME
            old = target.read_bytes()
            replace = Path.replace
            staged_paths = []

            def inspect_replacement(temporary, destination):
                staged_paths.append(temporary)
                self.assertEqual(temporary.parent, root)
                self.assertFalse(temporary.is_relative_to(app.run_dir))
                if os.name != 'nt':
                    self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o600)
                self.assertEqual(target.read_bytes(), old)
                self.assertEqual(list(app.run_dir.glob('.job-settings.json.*')), [])
                return replace(temporary, destination)

            with patch.object(Path, 'replace', inspect_replacement):
                app._save_job_settings({'goalThreadId': 'same-thread-resumed'})
            self.assertEqual(len(staged_paths), 1)
            self.assertFalse(staged_paths[0].exists())
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(json.loads(target.read_text())['goalThreadId'], 'same-thread-resumed')

    def test_settings_replacement_failure_preserves_old_file_without_nonatomic_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            app = server.App(runs=root)
            app.run_dir = root / 'run'
            app._save_job_settings({'goalThreadId': 'original-thread'})
            target = app.run_dir / server.JOB_SETTINGS_FILENAME
            old = target.read_bytes()
            with patch.object(Path, 'replace', side_effect=OSError(errno.EXDEV, 'cross-device replacement')):
                with self.assertRaises(OSError):
                    app._save_job_settings({'goalThreadId': 'replacement'})
            self.assertEqual(target.read_bytes(), old)
            self.assertEqual(list(root.glob('.job-settings.json.*')), [])
            with self.assertRaisesRegex(runtime.Error, 'outside the writable run workspace'):
                runtime._private_atomic_write(target, 'unsafe', staging_directory=app.run_dir)
            self.assertEqual(target.read_bytes(), old)


if __name__ == '__main__':
    unittest.main()
