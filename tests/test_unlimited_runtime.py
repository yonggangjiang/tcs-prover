"""Zero disables research deadlines without changing default finite jobs."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as runtime
from ui import server


class UnlimitedRuntimeTests(unittest.TestCase):
    def test_zero_is_unlimited_and_nonfinite_values_are_rejected(self):
        self.assertEqual(runtime.author_hours('0'), 0)
        for value in (-1, 'nan', 'inf', 169):
            with self.subTest(value=value), self.assertRaises(runtime.Error):
                runtime.author_hours(value)
        self.assertEqual(runtime.DEFAULT_AUTHOR_HOURS, 168)

    def test_elapsed_time_never_expires_unlimited_job(self):
        self.assertTrue(math.isinf(runtime.workflow_remaining({'thinking_hours': 0, 'elapsed_seconds': 10**12})))
        self.assertEqual(runtime.workflow_remaining({'thinking_hours': 1, 'elapsed_seconds': 4000}), 0)

    def test_no_model_request_deadline_for_unlimited_job(self):
        # No deadline ever ends an unlimited job's model call, but each attempt
        # still has a timeout: a hung request is stopped and retried (each
        # consecutive timeout doubles the next attempt's).
        options = {'thinking_hours': 0, 'elapsed_seconds': 10**12}
        settings = runtime._bounded_request_settings({}, options)
        self.assertEqual(settings['timeout'], runtime.STRUCTURED_TIMEOUT_SECONDS)
        self.assertEqual(runtime._attempt_timeout(settings['timeout'], options), runtime.STRUCTURED_TIMEOUT_SECONDS)
        self.assertEqual(runtime._attempt_timeout(runtime._grown_timeout(900, 2), options), 3600)
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

    def test_live_setting_accepts_zero_without_launching(self):
        with tempfile.TemporaryDirectory() as temp:
            app = server.App(runs=Path(temp))
            app.state.update(phase='paused', activeNode='author', stage='solve')
            with patch.object(app, '_write_author_limit') as write, patch.object(app, '_save_job_settings'), patch.object(app, 'add_trace'):
                self.assertEqual(app.set_author_time_limit(0), 0)
                write.assert_called_once_with(0)

if __name__ == '__main__':
    unittest.main()
