"""Regression tests for audited ui/server.py bugs, without model calls."""

import contextlib
from datetime import datetime, timedelta, timezone
import errno
import http.client
import io
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import threading
import unittest
from unittest import mock

import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from ui import retrieval, server

CUTOFF = "2026-10-02T14:13:12Z"
REJECTION = {"verdict": "reject", "solution": "Candidate proof", "bugs": "Step 3 fails."}


class Clock(datetime):
    """A wall clock the test moves by hand."""

    current = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz else cls.current.replace(tzinfo=None)




def socketless_server(runs):
    """A job manager without a listening socket, as other server tests build it."""

    manager = object.__new__(server.Server)
    manager.runs, manager.jobs, manager.jobs_lock = Path(runs), {}, threading.RLock()
    return manager


def message(text, identity, stage="repair"):
    return {"kind": "codex_event", "stage": stage, "node": "author", "root": True,
            "event": {"method": "item/completed",
                      "params": {"item": {"type": "agentMessage", "id": identity, "text": text}}}}


# Windows reparse tags; ``stat`` defines its few constants only on Windows.
MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
CLOUD_TAG, DEDUP_TAG = 0x9000001A, 0x80000013  # IO_REPARSE_TAG_CLOUD, IO_REPARSE_TAG_DEDUP


class WindowsInfo:
    """``lstat`` information carrying the reparse-point fields Windows reports."""

    def __init__(self, info, tag, mode=None):
        self._info, self.st_reparse_tag = info, tag
        self.st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        self.st_mode = info.st_mode if mode is None else mode

    def __getattr__(self, name):
        return getattr(self._info, name)


class TemporaryRuns(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        for change in (mock.patch.object(server, "grant_windows_access"),
                       mock.patch.object(runtime, "verify_model_credentials")):
            change.start()
            self.addCleanup(change.stop)

    def new_app(self, statement="Exact statement"):
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run(statement)
        app._save("checked-statement.md", f"# Checked statement\n\n{statement}\n")
        app.state["runId"] = app.run_dir.name
        return app


class RunFolderWriteTests(TemporaryRuns):
    """handoff-dir-timestamp-race and controller-writes-follow-author-symlinks."""

    def setUp(self):
        super().setUp()
        self.outside = self.root / "outside.txt"
        self.outside.write_text("outside content\n")
        self.outside.chmod(0o644)
        self.app = self.new_app()
        self.run = self.app.run_dir

    def assert_outside_untouched(self):
        self.assertEqual(self.outside.read_text(), "outside content\n")
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), 0o644)
        self.assertEqual(self.outside.stat().st_nlink, 1)

    def test_records_do_not_touch_the_run_folder_metadata(self):
        self.app.add_trace({"kind": "status", "stage": "solve", "label": "First"})
        before = self.run.stat()
        with mock.patch.object(Path, "chmod", side_effect=AssertionError("chmod per record")):
            for index in range(5):
                self.app.add_trace({"kind": "status", "stage": "solve", "label": f"Record {index}"})
            reloaded = server.App(self.app.trace_file, self.runs)
        after = self.run.stat()
        self.assertEqual((after.st_ctime_ns, after.st_mtime_ns), (before.st_ctime_ns, before.st_mtime_ns))
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o700)
        self.assertEqual(len(reloaded.state["trace"]), 6)

    def test_planted_transcript_entries_are_replaced_never_followed(self):
        cases = {
            "symlink": lambda path: path.symlink_to(self.outside),
            "hard link": lambda path: os.link(self.outside, path),
            "directory": lambda path: path.mkdir(),
        }
        if hasattr(os, "mkfifo"):
            cases["fifo"] = os.mkfifo
        for name, plant in cases.items():
            with self.subTest(entry=name):
                app = self.new_app(f"Statement with a planted {name}")
                plant(app.trace_file)
                app.add_trace({"kind": "status", "stage": "solve", "label": "After planting"})
                self.assert_outside_untouched()
                info = app.trace_file.lstat()
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual((info.st_nlink, stat.S_IMODE(info.st_mode)), (1, 0o600))
                lines = [json.loads(line) for line in app.trace_file.read_text().splitlines()]
                self.assertEqual([line["kind"] for line in lines], ["status", "diagnostic"])
                self.assertEqual(app.state["trace"][-1]["kind"], "diagnostic")
                moved = list(app.run_dir.glob(".transcript.jsonl.*.replaced"))
                self.assertEqual(len(moved), 1 if name == "directory" else 0)

    def test_resource_errors_are_reported_without_replacing_the_log(self):
        self.app.add_trace({"kind": "status", "stage": "solve", "label": "Kept record"})
        real_open = os.open

        def exhausted(path, *args, **kwargs):
            if Path(path) == self.app.trace_file:
                raise OSError(errno.EMFILE, "Too many open files")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(server.os, "open", side_effect=exhausted):
            with self.assertRaises(OSError):
                self.app.add_trace({"kind": "status", "stage": "solve", "label": "Not written"})
        self.assertEqual(list(self.run.glob(".transcript.jsonl.*")), [])
        self.assertIn("Kept record", self.app.trace_file.read_text())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_log_without_write_permission_is_kept_beside_a_new_one(self):
        self.app.add_trace({"kind": "status", "stage": "solve", "label": "Earlier record"})
        self.app.trace_file.chmod(0o400)
        self.app.add_trace({"kind": "status", "stage": "solve", "label": "Later record"})
        moved = list(self.run.glob(".transcript.jsonl.*.replaced"))
        self.assertEqual(len(moved), 1)
        self.assertIn("Earlier record", moved[0].read_text())
        self.assertIn("Later record", self.app.trace_file.read_text())
        self.assertEqual(stat.S_IMODE(self.app.trace_file.stat().st_mode), 0o600)

    def test_stop_and_pause_still_end_the_child_with_planted_links(self):
        for action in ("stop", "pause"):
            with self.subTest(action=action):
                app = self.new_app(f"Statement for {action}")
                for name in ("transcript.jsonl", server.PAUSE_FILENAME, server.PAUSE_REQUEST_FILENAME,
                             server.MANUAL_STOP_FILENAME, server.JOB_SETTINGS_FILENAME):
                    (app.run_dir / name).unlink(missing_ok=True)
                    (app.run_dir / name).symlink_to(self.outside)
                process = mock.Mock()
                process.poll.return_value = None
                process.wait.return_value = 0
                app.process = process
                app.state.update(phase="running", stage="solve", activeNode="author")
                with mock.patch.object(server, "stop_process_tree") as stop_tree:
                    getattr(app, action)()
                self.assert_outside_untouched()
                if action == "stop":
                    stop_tree.assert_called_once_with(process)
                    self.assertEqual(app.state["phase"], "done")
                else:
                    self.assertEqual(app.state["phase"], "pausing")
                    self.assertTrue(stat.S_ISREG((app.run_dir / server.PAUSE_FILENAME).lstat().st_mode))

    def test_saved_artifacts_replace_planted_links(self):
        names = ["partial-output.md", server.PAUSE_FILENAME, server.PAUSE_REQUEST_FILENAME,
                 server.AUTHOR_LIMIT_FILENAME, server.AUTHOR_STEER_FILENAME, server.MANUAL_STOP_FILENAME]
        legacy_temporaries = [".pause.tmp", ".author-limit.json.tmp", ".author-steer.json.tmp"]
        for name in names + legacy_temporaries:
            (self.run / name).symlink_to(self.outside)
        self.app._save("partial-output.md", "Partial work\n")
        self.app._save_pause({"status": "paused"})
        self.app._save(server.PAUSE_REQUEST_FILENAME, "{}\n")
        self.app._write_author_limit(3)
        command_id = self.app._write_author_steer("Use the lemma.")
        self.app._save(server.MANUAL_STOP_FILENAME, "{}\n")
        self.assert_outside_untouched()
        for name in names:
            info = (self.run / name).lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode), name)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, name)
        self.assertEqual(json.loads((self.run / server.AUTHOR_LIMIT_FILENAME).read_text()), {"hours": 3})
        self.assertEqual(json.loads((self.run / server.AUTHOR_STEER_FILENAME).read_text())["id"], command_id)
        # The workspace temporary names are no longer used, and staging is cleaned up.
        self.assertTrue(all((self.run / name).is_symlink() for name in legacy_temporaries))
        self.assertEqual([path for path in self.runs.iterdir() if path.is_file()], [])

    def test_linked_subfolder_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.run / "prompts").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(OSError, "prompts"):
            self.app._save("prompts/author.txt", "Prompt\n")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_loading_a_linked_transcript_neither_follows_nor_chmods_it(self):
        other = self.root / "other-job.jsonl"
        other.write_text(json.dumps({"kind": "final_result", "output": "Other job"}) + "\n")
        other.chmod(0o644)
        self.app.trace_file.unlink(missing_ok=True)
        self.app.trace_file.symlink_to(other)
        loaded = server.App(self.app.trace_file, self.runs)
        self.assertEqual((loaded.state["trace"], loaded.pinned), ([], []))
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o644)
        self.assertEqual(server._run_records(self.run), [])

    def as_windows(self, describe):
        """Simulate Windows: no O_NOFOLLOW, and ``lstat`` reporting reparse points.

        ``describe(path, info)`` returns ``(tag, mode)`` for an entry Windows
        reports as a reparse point (``mode`` None keeps the real one), or None.
        """

        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            reparse = describe(Path(os.fsdecode(path)), info)
            return info if reparse is None else WindowsInfo(info, *reparse)

        stack = contextlib.ExitStack()
        for change in (mock.patch.object(server, "NOFOLLOW", 0),
                       mock.patch.object(server.os, "lstat", lstat),
                       mock.patch.object(Path, "lstat", lambda path: lstat(path))):
            stack.enter_context(change)
        return stack

    def test_windows_cloud_and_deduplicated_files_are_used_in_place(self):
        # In a synced or deduplicated folder every entry is a reparse point.
        # ``lstat`` reads through them and reports tag 0; a non-link tag that
        # it did report is no planted link either.
        for tag in (0, CLOUD_TAG, DEDUP_TAG):
            with self.subTest(tag=hex(tag)):
                app = self.new_app(f"Statement in a synced folder {tag}")
                for label in ("First", "Second", "Third"):
                    app.add_trace({"kind": "status", "stage": "solve", "label": label})
                (app.run_dir / "prompts").mkdir()

                def synced(path, info, run=app.run_dir, tag=tag):
                    return (tag, None) if run in (path, *path.parents) else None

                with self.as_windows(synced):
                    restored = server.App(app.trace_file, self.runs)
                    self.assertEqual([entry.get("label") for entry in restored.state["trace"]],
                                     ["First", "Second", "Third"])
                    restored.add_trace({"kind": "status", "stage": "solve", "label": "Fourth"})
                    restored._save("prompts/review.txt", "Prompt\n")
                lines = [json.loads(line) for line in app.trace_file.read_text().splitlines()]
                self.assertEqual([line.get("label") for line in lines], ["First", "Second", "Third", "Fourth"])
                self.assertEqual(list(app.run_dir.glob(".transcript.jsonl.*")), [])
                self.assertEqual((app.run_dir / "prompts" / "review.txt").read_text(), "Prompt\n")

    def test_windows_symlinks_and_junctions_are_still_refused(self):
        other = self.root / "other-job.jsonl"
        record = json.dumps({"kind": "final_result", "output": "Other job"}) + "\n"
        other.write_text(record)
        other.chmod(0o644)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        # Windows opens through a junction, as the planted symlinks are opened
        # here, but reports it as a folder carrying the mount-point tag.
        def junction(path, info):
            return (MOUNT_POINT_TAG, stat.S_IFDIR | 0o700) if stat.S_ISLNK(info.st_mode) else None

        for name, describe in (("symlink", lambda path, info: None), ("junction", junction)):
            with self.subTest(link=name):
                app = self.new_app(f"Statement with a planted {name}")
                app.trace_file.unlink(missing_ok=True)
                app.trace_file.symlink_to(other)
                (app.run_dir / "prompts").symlink_to(elsewhere, target_is_directory=True)
                with self.as_windows(describe):
                    restored = server.App(app.trace_file, self.runs)
                    self.assertEqual((restored.state["trace"], restored.pinned), ([], []))
                    restored.add_trace({"kind": "status", "stage": "solve", "label": "After planting"})
                    with self.assertRaisesRegex(OSError, "prompts"):
                        restored._save("prompts/review.txt", "Prompt\n")
                self.assertEqual((other.read_text(), stat.S_IMODE(other.stat().st_mode)), (record, 0o644))
                self.assertEqual(list(elsewhere.iterdir()), [])
                self.assertTrue(stat.S_ISREG(app.trace_file.lstat().st_mode))
                lines = [json.loads(line) for line in app.trace_file.read_text().splitlines()]
                self.assertEqual([line.get("label") for line in lines], ["After planting", "Transcript replaced"])


class DamagedTranscriptTests(TemporaryRuns):
    """damaged-transcript-record-blocks-startup-and-new-jobs."""

    DAMAGED = [
        {"kind": []}, {"kind": "codex_event", "event": "x"},
        {"kind": "codex_event", "event": {"params": [1]}},
        {"kind": "codex_event", "event": {"item": "text"}},
        {"stage": []}, {"node": ["author"]}, {"label": {"text": "x"}},
        {"kind": "status", "stage": {"solve": 1}},
    ]

    def saved_run(self, name, extra_lines=()):
        run = self.runs / name
        run.mkdir(parents=True)
        (run / "draft.md").write_text("# Draft problem\n\nExact statement\n")
        (run / "checked-statement.md").write_text("# Checked statement\n\nExact statement\n")
        records = [{"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input",
                    "time": "2026-10-01T08:00:00+00:00"},
                   *self.DAMAGED,
                   {"kind": "final_result", "stage": "final", "node": "latex_compile",
                    "output": "Complete document", "time": "2026-10-01T09:00:00+00:00"}]
        lines = [json.dumps(record) for record in records] + ["[" * 100_000, "not json", *extra_lines]
        (run / "transcript.jsonl").write_text("\n".join(lines) + "\n")
        return run

    def test_important_record_rejects_odd_field_types(self):
        for record in self.DAMAGED:
            with self.subTest(record=record):
                self.assertFalse(server.important_record(record))
        self.assertTrue(server.important_record(message("Answer", "item-1")))

    def test_restart_and_new_jobs_survive_damaged_records(self):
        self.saved_run("damaged")
        jobs = server.restore_saved_jobs(self.runs)
        state = jobs["damaged"].snapshot()
        self.assertEqual((state["phase"], state["resultKind"]), ("done", "final"))
        # Records whose routing fields are not text are dropped; odd event bodies are harmless.
        self.assertEqual([record["kind"] for record in state["trace"]],
                         ["request", "codex_event", "codex_event", "codex_event", "final_result"])
        http = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(http.server_close)
        self.assertIn("damaged", http.jobs)
        with mock.patch.object(server.subprocess, "Popen", return_value=mock.Mock(stdin=io.StringIO())), \
                mock.patch.object(server.App, "_spawn_worker"):
            app = http.start_direct_job({"statement": "A new problem."})
        self.assertNotEqual(app.run_dir.name, "damaged")

    def test_one_unrestorable_run_is_skipped_with_a_message(self):
        self.saved_run("healthy")
        broken = self.runs / "broken"
        broken.mkdir(parents=True)
        (broken / "checked-statement.md").write_text("# Checked statement\n\nExact statement\n")
        (broken / "transcript.jsonl").write_text(json.dumps(
            {"kind": "request", "stage": "solve", "node": "author", "time": "2026-10-01T08:00:00+00:00"}) + "\n")
        (broken / server.PAUSE_FILENAME).write_text(json.dumps({"status": "running", "state": [1, 2]}))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
            jobs = server.restore_saved_jobs(self.runs)
        self.assertEqual(set(jobs), {"healthy"})
        self.assertIn("Skipped damaged saved run broken", errors.getvalue())

    def test_newest_transcript_vanishing_during_lookup_is_ignored(self):
        self.saved_run("kept")
        self.saved_run("deleted")
        real = server.direct_run_directory

        def deleted_meanwhile(path, runs):
            # Another request deletes this job after the folder listing.
            if Path(path).name == "deleted":
                (Path(path) / "transcript.jsonl").unlink()
            return real(path, runs)

        with mock.patch.object(server, "direct_run_directory", side_effect=deleted_meanwhile):
            app = server.App(runs=self.runs)
        self.assertEqual(app.run_dir.name, "kept")


class ApprovalClockTests(TemporaryRuns):
    """approve-elapsed-clock-includes-approval-wait."""

    def setUp(self):
        super().setUp()
        self.launches = []
        for change in (mock.patch.object(server, "datetime", Clock),
                       mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(server.subprocess, "Popen", side_effect=self.popen)):
            change.start()
            self.addCleanup(change.stop)

    def popen(self, argv, **kwargs):
        self.launches.append(argv)
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(""))
        process.poll.return_value = None
        process.wait.return_value = 0
        return process

    def elapsed_argument(self):
        argv = self.launches[-1]
        return float(argv[argv.index("--elapsed-seconds") + 1])

    def test_approval_wait_is_not_charged_after_pause_and_resume(self):
        start = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        Clock.current = start
        app = self.new_app()
        app.state.update(phase="reviewed", review={"statement": "Exact statement", "notes": ""},
                         startedAt=start.isoformat(), finishedAt="",
                         lastActivityAt=(start + timedelta(minutes=5)).isoformat())
        approved_at = Clock.current = start + timedelta(hours=12)  # Approved the next day.
        app.approve()
        self.assertEqual(self.elapsed_argument(), 300)
        Clock.current += timedelta(minutes=1)
        self.assertEqual(app.snapshot()["activeElapsedSeconds"], 360)
        self.assertEqual(app.state["startedAt"], start.isoformat())
        saved = json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual((saved["elapsedSeconds"], saved["resumedAt"]), (300, approved_at.isoformat()))
        app.pause()
        self.assertEqual(json.loads((app.run_dir / server.PAUSE_FILENAME).read_text())["elapsedSeconds"], 360)
        app._read_output(app.process, app.active_token)  # The runner acknowledges the pause.
        self.assertEqual(app.state["phase"], "paused")
        Clock.current += timedelta(hours=3)
        app.resume()
        self.assertEqual(self.elapsed_argument(), 360)

    def test_failed_launch_restores_the_previous_clock(self):
        Clock.current = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        app = self.new_app()
        app.state.update(phase="reviewed", review={"statement": "Exact statement", "notes": ""},
                         startedAt=Clock.current.isoformat(), lastActivityAt=Clock.current.isoformat())
        with mock.patch.object(server.subprocess, "Popen", side_effect=OSError("cannot start")):
            with self.assertRaisesRegex(OSError, "cannot start"):
                app.approve()
        self.assertNotIn("elapsedSeconds", app.state)
        self.assertNotIn("resumedAt", app.state)
        self.assertEqual(app.state["phase"], "reviewed")


class RestoredInstructionTests(TemporaryRuns):
    """steer-event-missing-steerid."""

    def write(self, run, records):
        run.mkdir(parents=True, exist_ok=True)
        (run / "transcript.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def test_restored_author_instructions_do_not_duplicate_on_next_resume(self):
        run = self.root / "run"
        self.write(run, [
            {"kind": "status", "stage": "solve", "node": "author",
             "label": "Restored live instructions queued", "text": "FIRST\n\nSECOND",
             "steerId": "replay-1", "restoredInstructions": ["FIRST", "SECOND"]},
            {"kind": "status", "stage": "solve", "node": "author",
             "label": "Live author instruction sent", "text": "FIRST\n\nSECOND", "steerId": "replay-1"},
        ])
        self.assertEqual(server.saved_author_instructions(run), ["FIRST", "SECOND"])

    def test_restored_block_echo_without_steer_id_is_not_a_new_instruction(self):
        run = self.root / "run"
        self.write(run, [
            {"kind": "status", "stage": "solve", "node": "author",
             "label": "Restored live instructions queued", "text": "FIRST\n\nSECOND",
             "steerId": "replay-1", "restoredInstructions": ["FIRST", "SECOND"]},
            {"kind": "request", "stage": "solve", "node": "author",
             "label": "Live author instruction sent", "text": "FIRST\n\nSECOND"},
            {"kind": "status", "stage": "solve", "node": "author",
             "label": "Live instruction queued", "text": "THIRD", "steerId": "new"},
            {"kind": "request", "stage": "solve", "node": "author",
             "label": "Live author instruction sent", "text": ["not", "text"]},
        ])
        self.assertEqual(server.saved_author_instructions(run), ["FIRST", "SECOND", "THIRD"])

    def test_chain_of_two_continuations_keeps_each_instruction_once(self):
        source = self.root / "first-run"
        self.write(source, [
            {"kind": "status", "stage": "solve", "node": "author",
             "label": "Live instruction queued", "text": "FIRST", "steerId": "a"},
            {"kind": "request", "stage": "solve", "node": "author",
             "label": "Live author instruction sent", "text": "FIRST"},
            {"kind": "status", "stage": "solve", "node": "author",
             "label": "Live instruction queued", "text": "SECOND", "steerId": "b"},
            {"kind": "request", "stage": "solve", "node": "author",
             "label": "Live author instruction sent", "text": "SECOND"},
        ])
        for number in (2, 3):
            app = self.new_app(f"Continuation {number}")
            server.Server._queue_saved_author_instructions(source, app)
            steer = json.loads((app.run_dir / server.AUTHOR_STEER_FILENAME).read_text())
            self.assertEqual(steer["instruction"], "FIRST\n\nSECOND")
            # That run's runner delivered the block without a steerId.
            app.add_trace({"kind": "request", "stage": "solve", "node": "author",
                           "label": "Live author instruction sent", "text": steer["instruction"]})
            self.assertEqual(server.saved_author_instructions(app.run_dir), ["FIRST", "SECOND"])
            source = app.run_dir

    def test_blocks_saved_by_earlier_continuations_are_not_repeated(self):
        run = self.root / "run"
        self.write(run, [{
            "kind": "status", "stage": "solve", "node": "author",
            "label": "Restored live instructions queued",
            "text": "FIRST\n\nSECOND\n\nFIRST\n\nSECOND", "steerId": "replay",
            "restoredInstructions": ["FIRST", "SECOND", "FIRST\n\nSECOND"],
        }])
        self.assertEqual(server.saved_author_instructions(run), ["FIRST", "SECOND"])


class FailedReviewRetryTests(TemporaryRuns):
    """failed-review-retry-duplicates-job."""

    def setUp(self):
        super().setUp()
        workers = mock.patch.object(server.App, "_spawn_worker")
        workers.start()
        self.addCleanup(workers.stop)
        self.manager = socketless_server(self.runs)

    def folders(self):
        return sorted(path.name for path in self.runs.iterdir() if path.is_dir())

    def test_failed_review_retries_in_its_own_folder(self):
        app = self.manager.start_job({"statement": "Original draft"})
        run_id = app.state["runId"]
        with app.lock:  # The review failed and the job page shows the input form.
            app.state.update(phase="input", error="Review failed: connection reset")
            app.active_token = app.worker_token = None
        again = self.manager.start_job({"statement": "Edited draft"}, run_id)
        self.assertIs(again, app)
        self.assertEqual((list(self.manager.jobs), self.folders()), ([run_id], [run_id]))
        self.assertEqual((app.run_dir.name, app.state["phase"]), (run_id, "reviewing"))
        self.assertIn("Edited draft", (app.run_dir / "draft.md").read_text())
        review_input = json.loads((app.run_dir / server.REVIEW_INPUT_FILENAME).read_text())
        self.assertEqual(review_input["statement"], "Edited draft")
        self.assertEqual([job["runId"] for job in self.manager.job_list()], [run_id])

    def test_named_finished_job_cannot_be_turned_into_another_review(self):
        app = self.manager.start_job({"statement": "Draft"})
        run_id = app.state["runId"]
        for phase in ("done", "paused", "prepared"):
            with self.subTest(phase=phase):
                app.state["phase"] = phase
                app.active_token = app.worker_token = None
                with self.assertRaisesRegex(ValueError, "home screen"):
                    self.manager.start_job({"statement": "Other"}, run_id)
                self.assertEqual((list(self.manager.jobs), self.folders()), ([run_id], [run_id]))


class HttpErrorTests(TemporaryRuns):
    """post-handler-drops-runtimeerror, and the removed fixed-app mode."""

    def setUp(self):
        super().setUp()
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)

    def request(self, path, body=None):
        headers = {"Origin": self.http.origin, "X-TCS-Prover-Token": self.http.token}
        raw = None
        if body is not None:
            raw, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_runtime_errors_are_reported_instead_of_dropping_the_connection(self):
        failure = retrieval_runtime.RetrievalPolicyError(
            "Historical execution requires an absolute native Mach-O codex binary.")
        with mock.patch.object(self.http, "start_direct_job", side_effect=failure):
            self.assertEqual(self.request("/direct", {"statement": "S"}), (400, {"error": str(failure)}))
        with mock.patch.object(self.http, "resume_paused_job", side_effect=runtime.Error("No saved session.")):
            self.assertEqual(self.request("/resume?job=x", {}), (400, {"error": "No saved session."}))

    def test_unexpected_errors_still_get_a_json_reply(self):
        with mock.patch.object(self.http, "start_job", side_effect=KeyError("phase")), \
                mock.patch("traceback.print_exc"):
            status, value = self.request("/review", {"statement": "S"})
        self.assertEqual(status, 500)
        self.assertIn("phase", value["error"])

    def test_state_without_a_job_is_the_empty_state(self):
        self.assertFalse(hasattr(self.http, "app"))
        status, value = self.request("/state")
        self.assertEqual((status, value["phase"], value["traceFrom"]), (200, "input", 0))

    def test_removed_research_audit_routes_are_unavailable(self):
        app = self.new_app()
        identity = app.state["runId"]
        self.http.jobs[identity] = app
        for route in ("/start-research-audit", "/set-research-audits"):
            with self.subTest(route=route):
                self.assertEqual(self.request(f"{route}?job={identity}", {}),
                                 (404, {"error": "Not found."}))

    def test_prepared_run_credential_error_is_a_user_error(self):
        app = self.new_app()
        app.state.update(phase="prepared", preparedRun=True)
        with mock.patch.object(runtime, "verify_model_credentials",
                               side_effect=runtime.Error("Missing provider credentials.")):
            with self.assertRaisesRegex(ValueError, "Missing provider credentials"):
                app.start_prepared_run()
        self.assertEqual(app.state["phase"], "prepared")


class CrashRecoveryTests(TemporaryRuns):
    """restore-crash-recovery-drops-critic-rejection and restore-keeps-stale-error-on-completed-run."""

    def setUp(self):
        super().setUp()
        self.app = self.new_app()
        self.app.state.update(goalThreadId="saved-author")
        self.app._save_job_settings(self.app.state)

    def events(self, *records):
        for record in records:
            self.app.add_trace(record)

    def restore(self):
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        checkpoint = json.loads((self.app.run_dir / server.PAUSE_FILENAME).read_text()) \
            if (self.app.run_dir / server.PAUSE_FILENAME).exists() else {}
        return restored, checkpoint

    def rejection(self, round_number=1):
        return [{"kind": "request", "stage": "critic", "node": "critic", "label": "Critic review"},
                {"kind": "critic_result", "stage": "critic", "node": "critic", "round": round_number,
                 "report": REJECTION}]

    def assert_repair_checkpoint(self, restored, checkpoint, round_number=1):
        self.assertEqual(restored.state["phase"], "paused")
        self.assertEqual((checkpoint["stage"], restored.state["stage"]), ("repair", "repair"))
        self.assertEqual(checkpoint["state"]["report"], REJECTION)
        self.assertEqual(checkpoint["state"]["solution"], "Candidate proof")
        self.assertEqual(checkpoint["state"]["round"], round_number)
        self.assertEqual(checkpoint["state"]["statement"], "Exact statement")

    def test_crash_during_a_new_session_repair_keeps_the_rejection(self):
        # After 'Continue critic', the repair runs in a new goal session logged as 'solve'.
        self.events(*self.rejection(),
                    {"kind": "status", "stage": "solve", "node": "author", "label": "Goal started",
                     "threadId": "saved-author"},
                    {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"},
                    message("Working on the repair", "item-1", stage="solve"))
        self.assert_repair_checkpoint(*self.restore())

    def test_trailing_status_record_does_not_hide_the_rejection(self):
        self.events({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"},
                    {"kind": "author_result", "stage": "solve", "node": "author", "text": "Proof"},
                    *self.rejection(),
                    {"kind": "request", "stage": "repair", "node": "author", "label": "Exact solve input"},
                    message("Repairing", "item-2"),
                    {"kind": "status", "stage": "solve", "label": "Saved progress", "text": "Saved."})
        self.assert_repair_checkpoint(*self.restore())

    def test_newer_rejection_wins_over_a_stale_running_checkpoint(self):
        self.app._save_pause({"status": "running", "node": "author", "stage": "solve",
                              "state": {"statement": "Exact statement"}, "elapsedSeconds": 10})
        self.events({"kind": "author_result", "stage": "solve", "node": "author", "text": "Proof"},
                    *self.rejection(2),
                    {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        self.assert_repair_checkpoint(*self.restore(), round_number=2)

    def test_completed_run_restores_without_its_recovered_error(self):
        self.events({"kind": "diagnostic", "stage": "solve", "text": "error: Connection reset"},
                    {"kind": "status", "stage": "solve", "node": "author", "label": "Resume requested"},
                    {"kind": "final_result", "stage": "final", "node": "latex_compile",
                     "output": "Complete document"})
        restored, _ = self.restore()
        self.assertEqual((restored.state["phase"], restored.state["resultKind"]), ("done", "final"))
        self.assertEqual(restored.state["error"], "")

    def test_final_tex_alone_also_clears_the_recovered_error(self):
        self.events({"kind": "diagnostic", "stage": "solve", "text": "error: Connection reset"})
        self.app._save("final.tex", "\\documentclass{article}\n")
        restored, _ = self.restore()
        self.assertEqual((restored.state["resultKind"], restored.state["error"]), ("final", ""))


class RepairOutputTests(TemporaryRuns):
    """repair-steer-clears-partial-output."""

    def test_live_instructions_and_continuations_extend_the_repair_answer(self):
        app = self.new_app()
        records = [
            {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"},
            message("Initial proof", "item-0", stage="solve"),
            {"kind": "author_result", "stage": "solve", "node": "author", "text": "Initial proof"},
            {"kind": "request", "stage": "critic", "node": "critic", "label": "Critic review"},
            {"kind": "critic_result", "stage": "critic", "node": "critic", "round": 1, "report": REJECTION},
            {"kind": "request", "stage": "repair", "node": "author", "label": "Exact solve input"},
            message("Partial work A. ", "item-1"),
            {"kind": "request", "stage": "repair", "node": "author", "label": "Live author instruction sent",
             "text": "Try B.", "steerId": "b"},
            message("Partial work B. ", "item-2"),
            {"kind": "request", "stage": "repair", "node": "author",
             "label": "Author context re-anchor after compaction", "text": "Read INITIAL_PROMPT.md."},
            message("Partial work C. ", "item-3"),
            {"kind": "request", "stage": "repair", "node": "author", "label": "Exact solve input",
             "text": "Continue."},
            message("Partial work D.", "item-4"),
        ]
        process = mock.Mock()
        process.stdout = iter(json.dumps(record) + "\n" for record in records)
        process.wait.return_value = 0
        token = app.active_token = app.worker_token = object()
        app.process = process
        app.state.update(phase="running", stage="solve", activeNode="author")
        app._read_output(process, token)
        expected = "Partial work A. Partial work B. Partial work C. Partial work D."
        self.assertEqual(app.state["output"], expected)
        self.assertEqual((app.run_dir / "partial-output.md").read_text(), expected)

    def test_a_new_rejection_still_starts_a_fresh_repair_answer(self):
        app = self.new_app()
        records = [
            {"kind": "request", "stage": "repair", "node": "author", "label": "Exact solve input"},
            message("First repair", "item-1"),
            {"kind": "author_result", "stage": "repair", "node": "author", "text": "First repair"},
            {"kind": "critic_result", "stage": "critic", "node": "critic", "round": 2, "report": REJECTION},
            {"kind": "request", "stage": "repair", "node": "author", "label": "Exact solve input"},
            message("Second repair", "item-2"),
        ]
        process = mock.Mock()
        process.stdout = iter(json.dumps(record) + "\n" for record in records)
        process.wait.return_value = 0
        token = app.active_token = app.worker_token = object()
        app.process = process
        app.state.update(phase="running", stage="repair", activeNode="author")
        app._read_output(process, token)
        self.assertEqual(app.state["output"], "Second repair")


class JobListTests(TemporaryRuns):
    """delete-job-during-paused-audit and historical-continuations-advertised."""

    def setUp(self):
        super().setUp()
        self.manager = socketless_server(self.runs)
        self.app = self.new_app()
        self.app._save(runtime.SAVED_CANDIDATE_FILENAME, "Complete candidate proof.\n")
        self.run_id = self.app.run_dir.name
        self.manager.jobs[self.run_id] = self.app
        retrieval._CACHE.clear()



    def historical(self, **state):
        self.app.state.update(retrievalPolicyPath=str(self.root / "controller" / "retrieval-policy.json"),
                              retrievalMode="historical", retrievalCutoffUtc=CUTOFF, **state)

    def test_stopped_historical_job_offers_no_continuation_that_creates_a_run(self):
        self.historical(phase="done", stage="solve", activeNode="author", manuallyStopped=True,
                        stoppedStage="solve", error="Stopped.")
        self.assertIsNone(server.Server._stopped_continuation_plan(self.app))
        row = self.manager.job_list()[0]
        self.assertFalse(row["canContinueStopped"] or row["canResumeCritic"])
        self.assertTrue(row["checkpoints"])
        self.assertFalse(any(checkpoint["resumable"] for checkpoint in row["checkpoints"]))
        # The job's own cached checkpoints are not modified.
        self.assertTrue(all(checkpoint["resumable"] for checkpoint in self.app.snapshot()["checkpoints"]))

    def test_paused_historical_job_still_resumes_in_place(self):
        self.historical(phase="paused", stage="solve", activeNode="author")
        self.assertEqual(server.Server._stopped_continuation_plan(self.app)["action"], "resume")

    def test_live_job_keeps_its_continuations(self):
        self.app.state.update(phase="done", stage="solve", activeNode="author", manuallyStopped=True,
                              stoppedStage="solve", error="Stopped.")
        row = self.manager.job_list()[0]
        self.assertTrue(row["canContinueStopped"] and row["canResumeCritic"])
        self.assertTrue(all(checkpoint["resumable"] for checkpoint in row["checkpoints"]))


class ServerLifecycleTests(TemporaryRuns):
    """stop-all-windows-sigint, symlinked-runs-dir-crashes-web-ui-startup, windows-port-reuse."""

    def live_apps(self, manager, count=3):
        apps = []
        for index in range(count):
            app = server.App(runs=self.runs, load_latest=False)
            app.process = mock.Mock()
            app.process.send_signal.side_effect = ValueError("Unsupported signal: 2")
            manager.jobs[f"job-{index}"] = app
            apps.append(app)
        return apps

    def test_windows_stop_all_stops_every_tree_even_when_one_fails(self):
        manager = socketless_server(self.runs)
        apps = self.live_apps(manager)
        stopped = []

        def stop_tree(process):
            stopped.append(process)
            if len(stopped) == 1:
                raise OSError("taskkill failed")

        # Only the platform check runs under the patched name; nothing builds a Path.
        with mock.patch.object(server, "stop_process_tree", side_effect=stop_tree), \
                mock.patch.object(server.os, "name", "nt"):
            manager.stop_all()
        self.assertEqual(stopped, [app.process for app in apps])
        for app in apps:
            app.process.send_signal.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX runners receive SIGINT")
    def test_posix_stop_all_interrupts_every_runner(self):
        manager = socketless_server(self.runs)
        apps = self.live_apps(manager)
        apps[1].process.send_signal.side_effect = None
        apps[1].process = None  # Finished meanwhile.
        manager.stop_all()
        for app in (apps[0], apps[2]):
            app.process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_server_starts_with_a_symlinked_runs_folder(self):
        real = self.root / "data-disk" / "runs"
        (real / "saved-run").mkdir(parents=True)
        (real / "saved-run" / "checked-statement.md").write_text("# Checked statement\n\nExact statement\n")
        (real / "saved-run" / "transcript.jsonl").write_text(json.dumps(
            {"kind": "final_result", "stage": "final", "output": "Done",
             "time": "2026-10-01T08:00:00+00:00"}) + "\n")
        link = self.root / "linked-runs"
        link.symlink_to(real, target_is_directory=True)
        http = server.Server((server.HOST, 0), runs=link)
        self.addCleanup(http.server_close)
        self.assertIn("saved-run", http.jobs)
        self.assertEqual(http.batch_catalog.root, real.resolve())
        self.assertIsInstance(http.batch_catalog.list_batches(), list)

    def test_windows_servers_do_not_share_a_listening_port(self):
        self.assertEqual(server.Server.__dict__["allow_reuse_address"], os.name != "nt")

    @unittest.skipUnless(os.name == "nt", "SO_REUSEADDR shares listening ports only on Windows")
    def test_second_windows_server_cannot_bind_the_same_port(self):
        first = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError) as raised:
            server.Server((server.HOST, first.server_port), runs=self.runs)
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)


class LiveWorkflowEditTests(TemporaryRuns):
    """state-depends-on-live-yaml-parse."""

    def test_job_pages_survive_a_broken_yaml_while_launches_stay_strict(self):
        app = self.new_app()
        app._save(runtime.SAVED_CANDIDATE_FILENAME, "Complete candidate proof.\n")
        app.state.update(phase="running", stage="solve", activeNode="author")
        manager = socketless_server(self.runs)
        manager.jobs[app.state["runId"]] = app
        good = app.snapshot()
        self.assertEqual(good["workflowWarning"], "")
        self.assertEqual([checkpoint["id"] for checkpoint in good["checkpoints"]], ["candidate"])
        fresh = server.App(runs=self.runs, load_latest=False)
        broken = ValueError("Cannot read workflow author_critic.yaml: mapping values are not allowed here")
        with mock.patch.object(runtime, "builtin_workflow", side_effect=broken):
            # A changed checkpoint file must not drop the known checkpoints meanwhile.
            app._save(runtime.SAVED_CANDIDATE_FILENAME, "Revised candidate proof.\n")
            state = app.snapshot()
            self.assertIn("mapping values", state["workflowWarning"])
            self.assertEqual(state["workflow"]["settings"]["prompts"], good["workflow"]["settings"]["prompts"])
            self.assertEqual(state["checkpoints"], good["checkpoints"])
            self.assertEqual(len(manager.job_list()), 1)
            with self.assertRaisesRegex(ValueError, "mapping values"):
                fresh.start_direct_statement("A new problem.")
        self.assertEqual(app.snapshot()["workflowWarning"], "")

    def test_broken_shared_workflow_still_stops_startup_instead_of_hiding_jobs(self):
        self.new_app().add_trace({"kind": "request", "stage": "solve", "label": "Exact solve input"})
        broken = ValueError("Cannot read workflow author_critic.yaml: mapping values are not allowed here")
        with mock.patch.object(runtime, "builtin_workflow", side_effect=broken):
            with self.assertRaisesRegex(ValueError, "mapping values"):
                server.restore_saved_jobs(self.runs)


class NewJobAndDashboardTests(TemporaryRuns):
    """new-job-parses-latest-transcript, dashboard-rehashes-codex-binary, metadata-failure-erases-cutoff-display."""

    def setUp(self):
        super().setUp()
        retrieval._CACHE.clear()
        for change in (mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(server.subprocess, "Popen",
                                         side_effect=lambda *a, **k: mock.Mock(stdin=io.StringIO()))):
            change.start()
            self.addCleanup(change.stop)

    def test_new_jobs_do_not_parse_the_newest_other_transcript(self):
        other = self.new_app("Another long-running job")
        other.add_trace({"kind": "request", "stage": "solve", "label": "Exact solve input"})
        manager = socketless_server(self.runs)
        with mock.patch.object(server.App, "_latest_trace", side_effect=AssertionError("parsed another job")):
            created = [
                manager.start_job({"statement": "Reviewed problem."}),
                manager.start_direct_job({"statement": "Direct problem."}),
                manager.start_latex_job({"content": "A theorem and its proof."}),
                *manager.start_direct_batch({"statements": [{"name": "a.md", "statement": "Batch problem."}]}),
            ]
        self.assertNotIn(other.run_dir, [app.run_dir for app in created])
        self.assertEqual(len({app.run_dir for app in created}), 4)
        blank = server.App(runs=self.runs, load_latest=False)
        self.assertEqual((blank.trace_file, blank.run_dir, blank.state["runId"]), (None, None, ""))

    def historical_app(self):
        app = self.new_app()
        app.state.update(retrievalPolicyPath=str(self.root / "controller" / "missing-policy.json"),
                         retrievalMode="historical", retrievalCutoffUtc=CUTOFF)
        return app

    def test_policy_metadata_is_read_without_the_job_lock(self):
        app = self.historical_app()
        real, free = retrieval.public_retrieval_info, []

        def read(*args, **kwargs):
            def probe():
                free.append(app.lock.acquire(timeout=2))
                if free[-1]:
                    app.lock.release()
            worker = threading.Thread(target=probe)
            worker.start()
            worker.join()
            return real(*args, **kwargs)

        with mock.patch.object(retrieval, "public_retrieval_info", side_effect=read):
            app.snapshot()
        self.assertEqual(free, [True])

    def test_unloadable_policy_keeps_the_saved_cutoff(self):
        state = self.historical_app().snapshot()
        self.assertFalse(state["retrieval"]["available"])
        self.assertEqual(state["retrieval"]["cutoffExclusiveUtc"], CUTOFF)
        self.assertEqual((state["retrievalMode"], state["retrievalCutoffUtc"]), ("historical", CUTOFF))


class HistoricalBatchErrorTests(TemporaryRuns):
    """historical-batch-error-misattributed."""

    def setUp(self):
        super().setUp()
        codex = self.root / "codex"
        codex.write_bytes(b"\xcf\xfa\xed\xfe" + b"unit-test-native-binary")
        codex.chmod(0o700)
        self.manager = socketless_server(self.runs)
        self.launches = []
        real_prepare = retrieval.prepare_policy

        def prepare(workspace, cutoff):
            return real_prepare(workspace, cutoff, controller_root=self.root / "controller")

        for change in (mock.patch.object(retrieval_runtime, "codex_executable", return_value=str(codex)),
                       mock.patch.object(retrieval, "capabilities", return_value={"historicalAvailable": True}),
                       mock.patch.object(retrieval, "prepare_policy", side_effect=prepare),
                       mock.patch.object(retrieval, "preflight_policy"),
                       mock.patch.object(retrieval, "preflight_compiler"),
                       mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(server.subprocess, "Popen", side_effect=self.launch)):
            change.start()
            self.addCleanup(change.stop)
        retrieval._CACHE.clear()

    def launch(self, argv, **kwargs):
        self.launches.append(kwargs["cwd"])
        return mock.Mock(stdin=io.StringIO(), poll=mock.Mock(return_value=None))

    def start(self, *names):
        return self.manager.start_direct_batch({
            "retrievalMode": "historical", "retrievalCutoffUtc": CUTOFF,
            "statements": [{"name": f"{name}.md", "statement": f"The {name} statement."} for name in names]})

    def test_one_failed_preflight_names_its_own_file_and_holds_the_rest(self):
        def preflight(path):
            if "_the-first-statement-" in str(path):
                raise ValueError("sandbox unavailable")

        with mock.patch.object(retrieval, "preflight_policy", side_effect=preflight):
            with self.assertRaises(ValueError) as raised:
                self.start("first", "second", "third")
        self.assertEqual(str(raised.exception), "Could not start the job for first.md: sandbox unavailable. "
                                                "The 3 prepared jobs are held and will not start.")
        self.assertEqual(self.launches, [])
        self.assertTrue(all(app.state["launchHold"] for app in self.manager.jobs.values()))

    def test_shared_compiler_failure_names_the_batch(self):
        with mock.patch.object(retrieval, "preflight_compiler", side_effect=ValueError("no LaTeX sandbox")):
            with self.assertRaises(ValueError) as raised:
                self.start("first", "second")
        self.assertEqual(str(raised.exception), "Could not start the historical batch: no LaTeX sandbox. "
                                                "The 2 prepared jobs are held and will not start.")
        self.assertEqual(self.launches, [])

    def test_launch_failure_counts_started_and_held_jobs(self):
        def launch(argv, **kwargs):
            if self.launches:
                raise OSError("No space left on device")
            return self.launch(argv, **kwargs)

        stopped = []
        with mock.patch.object(server.subprocess, "Popen", side_effect=launch), \
                mock.patch.object(server.App, "stop", autospec=True, side_effect=stopped.append):
            with self.assertRaises(ValueError) as raised:
                self.start("first", "second", "third")
        self.assertEqual(str(raised.exception),
                         "Could not start the job for second.md: No space left on device. "
                         "The 1 job it had already started were stopped. "
                         "The 2 prepared jobs are held and will not start.")
        self.assertEqual([app.state["sourceFile"] for app in stopped], ["first.md"])


if __name__ == "__main__":
    unittest.main()
