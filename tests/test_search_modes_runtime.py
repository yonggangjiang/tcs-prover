"""Selected search modes reach actual process boundaries without model calls."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from retrieval import runtime as retrieval
import workflow_runner as runtime
from test_goal_runtime import Runtime, PROMPTS, PROMPT, SETTINGS, success


class SearchModesRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(retrieval.MODE_ENV, None)
        os.environ.pop(retrieval.POLICY_ENV, None)

    def save(self, value):
        (self.workspace / 'job-settings.json').write_text(json.dumps(value))

    def test_saved_modes_and_legacy_main_default(self):
        self.assertEqual(retrieval.search_mode(self.workspace), 'live')
        self.save({})
        self.assertEqual(retrieval.search_mode(self.workspace), 'live')
        for mode in ('live', 'disabled'):
            self.save({'retrievalMode': mode})
            self.assertEqual(retrieval.search_mode(self.workspace), mode)
            self.assertIn(f'web_search="{mode}"', retrieval.native_search_arguments(self.workspace))

    def test_saved_mode_cannot_change_through_child_environment(self):
        self.save({'retrievalMode': 'disabled'})
        with patch.dict(os.environ, {retrieval.MODE_ENV: 'live'}):
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, 'differs'):
                retrieval.search_mode(self.workspace)
        self.save({'retrievalMode': 'live'})
        with patch.dict(os.environ, {retrieval.MODE_ENV: 'disabled'}):
            with self.assertRaisesRegex(retrieval.RetrievalPolicyError, 'differs'):
                retrieval.search_mode(self.workspace)

    def test_missing_historical_policy_and_invalid_mode_fail_before_process(self):
        for mode in ('historical', 'unexpected'):
            self.save({'retrievalMode': mode})
            with patch.object(Path, 'cwd', return_value=self.workspace), patch.object(runtime.subprocess, 'Popen') as spawn:
                with self.assertRaises(runtime.Error):
                    runtime.run_structured_attempt('Task', {}, 'review', runtime.MODEL, 'max', 'fast', 'concise')
                spawn.assert_not_called()

    def test_historical_policy_overrides_ambient_live_mode(self):
        self.save({'retrievalMode': 'live'})
        with patch.dict(os.environ, {retrieval.MODE_ENV: 'live'}):
            arguments = retrieval.native_search_arguments(self.workspace, policy={'mode': 'historical'})
        self.assertIn('web_search="disabled"', arguments)
        self.assertIn('tools.web_search=false', arguments)

    def test_structured_processes_use_selected_mode_on_every_stage(self):
        for mode in ('live', 'disabled'):
            self.save({'retrievalMode': mode})
            for stage in ('review', 'critic', 'final'):
                with self.subTest(mode=mode, stage=stage), patch.object(Path, 'cwd', return_value=self.workspace), \
                     patch.object(runtime, 'codex', return_value='mock-codex'), \
                     patch.object(runtime.subprocess, 'Popen', side_effect=RuntimeError('process boundary')) as spawn:
                    with self.assertRaisesRegex(RuntimeError, 'process boundary'):
                        runtime.run_structured_attempt('Task', {}, stage, runtime.MODEL, 'max', 'fast', 'concise')
                    self.assertIn(f'web_search="{mode}"', spawn.call_args.args[0])

    def test_critic_snapshot_keeps_exact_read_only_files_in_both_modes(self):
        files = (('proof.md', 'Exact proof\n'), ('statement.md', 'Exact statement\n'), ('manifest.json', '{}'))
        for mode in ('live', 'disabled'):
            self.save({'retrievalMode': mode})
            with self.subTest(mode=mode), patch.object(Path, 'cwd', return_value=self.workspace), \
                 patch.object(runtime, 'codex', return_value='mock-codex'), patch.object(runtime, 'emit'), \
                 patch.object(runtime.subprocess, 'Popen', side_effect=RuntimeError('process boundary')) as spawn:
                with self.assertRaisesRegex(RuntimeError, 'process boundary'):
                    runtime.run_structured_attempt('Read proof.md', {}, 'critic', runtime.MODEL, 'max', 'fast',
                                                   'concise', input_files=files)
                command = spawn.call_args.args[0]
                snapshot = Path(command[command.index('-C') + 1])
                self.addCleanup(shutil.rmtree, snapshot)
                self.assertIn(f'web_search="{mode}"', command)
                # Read-only either way; on macOS the profile also hides TCS Prover's private state.
                if runtime.private_permissions(':read-only', runtime.MODEL) is None:
                    self.assertEqual(command[command.index('-s') + 1], 'read-only')
                else:
                    self.assertNotIn('-s', command)
                    self.assertIn(f'permissions.{runtime.PRIVATE_PERMISSIONS}.extends=":read-only"', command)
                for name, value in files:
                    self.assertEqual((snapshot / name).read_text(), value)
                    if os.name != 'nt':
                        self.assertEqual((snapshot / name).stat().st_mode & 0o777, 0o400)

    def test_author_start_resume_and_repairs_keep_saved_mode(self):
        for mode in ('live', 'disabled'):
            self.save({'retrievalMode': mode})
            for resume in (False, True):
                instance = Runtime([success('proof'), success('repaired')])
                options = {'goal_cwd': str(self.workspace), **({'goal_thread_id': 'thread-1'} if resume else {})}
                with self.subTest(mode=mode, resume=resume), patch.object(runtime.subprocess, 'Popen', return_value=Mock()) as spawn:
                    session = runtime.goal_session(instance, PROMPT, prompts=PROMPTS, settings=SETTINGS, options=options)
                    try:
                        self.assertEqual(next(session)['outcome'], 'done')
                        self.assertEqual(session.send({'solution': 'proof', 'bugs': 'repair', 'round': 1})['outcome'], 'done')
                    finally:
                        session.close()
                    self.assertIn(f'web_search="{mode}"', spawn.call_args.args[0])
                    config = next(params['config'] for method, params in instance.rpc.calls
                                  if method in ('thread/start', 'thread/resume'))
                    self.assertEqual(config['web_search'], mode)
                    self.assertEqual(config['tools']['web_search'], mode == 'live')


if __name__ == '__main__':
    unittest.main()
