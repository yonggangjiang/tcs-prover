"""Zero, the default, disables research deadlines; finite limits of any length stay available."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from ui import server


class UnlimitedRuntimeTests(unittest.TestCase):
    def test_zero_is_unlimited_and_nonfinite_values_are_rejected(self):
        self.assertEqual(runtime.author_hours('0'), 0)
        for value in (-1, 'nan', 'inf'):
            with self.subTest(value=value), self.assertRaises(runtime.Error):
                runtime.author_hours(value)
        # Finite limits above one week are allowed, and unlimited is the default.
        self.assertEqual(runtime.author_hours(169), 169)
        self.assertEqual(runtime.DEFAULT_AUTHOR_HOURS, 0)

    def test_elapsed_time_never_expires_unlimited_job(self):
        self.assertTrue(math.isinf(runtime.workflow_remaining({'thinking_hours': 0, 'elapsed_seconds': 10**12})))
        self.assertEqual(runtime.workflow_remaining({'thinking_hours': 1, 'elapsed_seconds': 4000}), 0)

    def test_no_model_request_deadline_for_unlimited_job(self):
        # No deadline ever ends an unlimited job's model call, and no wall-clock
        # limit either: an attempt is stopped (and retried) only after its CLI
        # wrote nothing for STRUCTURED_STALL_SECONDS, or a node's own timeout.
        options = {'thinking_hours': 0, 'elapsed_seconds': 10**12}
        settings = runtime._bounded_request_settings({}, options)
        self.assertNotIn('timeout', settings)
        self.assertIsNone(runtime._attempt_timeout(None, options))
        self.assertEqual(runtime.STRUCTURED_STALL_SECONDS, 3600)
        finite = runtime._bounded_request_settings({'timeout': 900}, {'thinking_hours': 1})
        self.assertEqual(finite['timeout'], 900)

    def test_live_limit_file_can_disable_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'limit.json'
            path.write_text(json.dumps({'hours': 0}))
            self.assertTrue(math.isinf(runtime.workflow_remaining({'thinking_hours': 1, 'author_limit_file': str(path)})))

    def test_unlimited_error_retry_is_not_treated_as_exhausted(self):
        with tempfile.TemporaryDirectory() as temp:
            app = server.App(runs=Path(temp))
            app.state['thinkingHours'] = 0
            checkpoint = {'pauseCause': 'error', 'error': 'Connection lost', 'elapsedSeconds': 10**12}
            app._configure_auto_resume(checkpoint, now=1000)
            self.assertEqual(checkpoint['autoResumeAt'], 1000 + server.AUTO_RESUME_SECONDS)

    def test_the_limit_changes_until_the_job_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = server.App(runs=Path(temp))
            with patch.object(app, '_write_author_limit') as write, patch.object(app, '_save_job_settings'), \
                    patch.object(app, 'add_trace'):
                for phase in ('reviewing', 'reviewed', 'prepared', 'pausing'):
                    app.state.update(phase=phase, activeNode='statement_reviewer', stage='review')
                    self.assertEqual(app.set_author_time_limit(2.5), 2.5)
                self.assertEqual(write.call_count, 4)
                for phase in ('input', 'stopping', 'done'):
                    app.state.update(phase=phase)
                    with self.assertRaisesRegex(ValueError, 'until the job finishes'):
                        app.set_author_time_limit(2)
            self.assertEqual(app.state['thinkingHours'], 2.5)

    def test_live_setting_accepts_zero_without_launching(self):
        with tempfile.TemporaryDirectory() as temp:
            app = server.App(runs=Path(temp))
            app.state.update(phase='paused', activeNode='author', stage='solve')
            with patch.object(app, '_write_author_limit') as write, patch.object(app, '_save_job_settings'), patch.object(app, 'add_trace'):
                self.assertEqual(app.set_author_time_limit(0), 0)
                write.assert_called_once_with(0)

if __name__ == '__main__':
    unittest.main()
