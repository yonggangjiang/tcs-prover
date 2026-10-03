import json
import tempfile
import unittest
from pathlib import Path

from ui import cli, server


class QuotaPauseOptionTests(unittest.TestCase):
    def test_default_pauses_with_ten_percent_of_the_weekly_quota_left(self):
        self.assertEqual(server.DEFAULT_QUOTA_PAUSE_REMAINING, 10)
        self.assertEqual(server.quota_remaining(None), 10)
        self.assertEqual(server.quota_pause_percent(10), 90)
        self.assertEqual(cli.direct_cli_options()["quota_pause_remaining"], 10)

    def test_a_chosen_point_reaches_the_runner_threshold(self):
        self.assertEqual(cli.direct_cli_options(quota_pause_remaining=25)["quota_pause_remaining"], 25)
        self.assertEqual(server.quota_pause_percent("25"), 75)
        # Zero leaves the whole window to the job: the runner's 0 disables the pause.
        self.assertEqual(server.quota_pause_percent(0), 0)

    def test_rejects_points_outside_whole_percentages(self):
        for value in (-1, 100, 2.5, True, "half", [10]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.quota_remaining(value)

    def test_saved_jobs_keep_their_point_and_older_jobs_use_the_default(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            self.assertEqual(server.saved_quota_pause_remaining(run), 10)
            (run / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({"quotaPauseRemaining": 30}), encoding="utf-8")
            self.assertEqual(server.saved_quota_pause_remaining(run), 30)
            (run / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({"quotaPauseRemaining": 150}), encoding="utf-8")
            self.assertEqual(server.saved_quota_pause_remaining(run), 10)


if __name__ == "__main__":
    unittest.main()
