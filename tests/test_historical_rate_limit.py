"""Provider pacing stays shared and bounded throughout concurrent body reads."""
import json
import multiprocessing
import os
from pathlib import Path
import queue
import stat
import tempfile
import time
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval.historical_rate_limit import (MAX_COOLDOWN_SECONDS, MAX_SLEEP_SECONDS, QUARANTINE, RateLimitError,
                                             SharedHostLimiter)


class FakeTime:
    def __init__(self):
        self.monotonic = 100.0
        self.wall = 1000.0
        self.sleeps = []

    def clock(self):
        return self.monotonic

    def wall_clock(self):
        return self.wall

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.monotonic += seconds
        self.wall += seconds


def request_process(directory, workspace, host, hold_seconds, events, release=None, deadline_seconds=15):
    """Use a fresh interpreter's limiter and record real request boundaries."""
    try:
        limiter = SharedHostLimiter(directory, workspace_root=workspace)
        events.put(("waiting", time.monotonic()))
        with limiter.slot(host, time.monotonic() + deadline_seconds):
            events.put(("start", time.monotonic()))
            if release is None:
                time.sleep(hold_seconds)
            else:
                release.wait(45)
            events.put(("end", time.monotonic()))
    except Exception as exc:
        events.put(("error", str(exc)))


class SharedRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "solver"
        self.workspace.mkdir()
        self.directory = self.root / "controller" / "rates"
        self.fake = FakeTime()
        self.limiter = self.new_limiter()

    def new_limiter(self):
        return SharedHostLimiter(self.directory, workspace_root=self.workspace,
                                 clock=self.fake.clock, wall_clock=self.fake.wall_clock,
                                 sleep=self.fake.sleep)

    def enter(self, host, *, limiter=None, duration=100):
        with (limiter or self.limiter).slot(host, self.fake.clock() + duration):
            return self.fake.clock()

    def write_state(self, content):
        path = self.directory / "arxiv.json"
        path.write_text(content)
        path.chmod(0o600)
        return path

    def test_arxiv_aliases_share_three_seconds_between_request_starts(self):
        self.assertEqual(self.enter("export.arxiv.org"), 100)
        self.assertEqual(self.enter("ARXIV.ORG.", limiter=self.new_limiter()), 103)
        self.assertEqual(self.enter("export.arxiv.org"), 106)
        self.assertTrue(all(0 < duration <= MAX_SLEEP_SECONDS for duration in self.fake.sleeps))

    def test_other_provider_buckets_are_independent_with_one_second_spacing(self):
        hosts = ["api.crossref.org", "api.search.brave.com", "lite.duckduckgo.com"]
        for host in hosts:
            self.assertEqual(self.enter(host), 100)
        for host in hosts:
            self.assertEqual(self.enter(host, limiter=self.new_limiter()), 101)

    def test_wayback_starts_are_limited_to_one_every_three_seconds(self):
        self.assertEqual(self.enter("web.archive.org"), 100)
        self.assertEqual(self.enter("web.archive.org", limiter=self.new_limiter()), 103)
        self.assertEqual(self.enter("web.archive.org"), 106)

    def test_full_long_retry_after_is_shared_without_sleeping_inside_slot(self):
        with self.limiter.slot("export.arxiv.org", 105):
            self.limiter.defer("arxiv.org", 86400, deadline=105)
            self.limiter.defer("export.arxiv.org", 1200, deadline=105)
        self.assertEqual(self.fake.clock(), 100)
        shared = self.new_limiter()
        with self.assertRaises(RateLimitError) as error:
            with shared.slot("export.arxiv.org", 101):
                self.fail("A new request bypassed Retry-After.")
        # The cooldown outlasts the deadline: it fails at once and says when.
        self.assertEqual(error.exception.code, "provider_cooldown")
        self.assertEqual(error.exception.retry_at, 1000 + MAX_COOLDOWN_SECONDS)
        state = json.loads((self.directory / "arxiv.json").read_text())
        self.assertEqual(state["next_monotonic"], 100 + MAX_COOLDOWN_SECONDS)
        self.assertEqual(state["next_wall"], 1000 + MAX_COOLDOWN_SECONDS)
        self.assertEqual(self.fake.clock(), 100)

    def test_defer_outside_slot_retains_maximum_existing_cooldown(self):
        self.limiter.defer("api.crossref.org", 500, deadline=101)
        self.fake.sleep(10)
        self.new_limiter().defer("api.crossref.org", 1, deadline=111)
        state = json.loads((self.directory / "crossref.json").read_text())
        self.assertEqual(state["next_monotonic"], 600)

    def test_waiting_admission_reloads_cooldown_without_holding_timing_mutex(self):
        self.enter("web.archive.org")
        waiting = self.new_limiter()
        changed = False

        def extend_cooldown_while_waiting(seconds):
            nonlocal changed
            if not changed:
                changed = True
                self.new_limiter().defer("web.archive.org", 10, deadline=101)
            self.fake.sleep(seconds)

        waiting.sleep = extend_cooldown_while_waiting
        with self.assertRaises(RateLimitError) as error:
            self.enter("web.archive.org", limiter=waiting, duration=4)
        self.assertTrue(changed)
        # The new cooldown ends after the deadline: no sleeping until then.
        self.assertEqual(error.exception.code, "provider_cooldown")
        self.assertLess(self.fake.clock(), 104)
        state = json.loads((self.directory / "wayback.json").read_text())
        self.assertEqual(state["next_monotonic"], 110)

    def test_wall_clock_jump_forward_cannot_bypass_monotonic_spacing(self):
        self.enter("export.arxiv.org")
        self.fake.wall += 100000
        self.assertEqual(self.enter("export.arxiv.org"), 103)

    def test_reboot_uses_remaining_wall_clock_cooldown(self):
        self.limiter.defer("export.arxiv.org", 500)
        self.fake.monotonic = 5
        self.fake.wall += 499
        self.assertEqual(self.enter("export.arxiv.org"), 6)

    def test_deadline_and_cooldown_values_fail_closed(self):
        for deadline in (None, True, -1, 100, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(deadline=repr(deadline)[:40]):
                with self.assertRaises(RateLimitError) as error:
                    with self.limiter.slot("export.arxiv.org", deadline):
                        self.fail("Invalid deadline admitted a request.")
                self.assertEqual(error.exception.code, "deadline_exceeded")
        for seconds in (None, True, -1, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(seconds=repr(seconds)[:40]):
                with self.assertRaises(RateLimitError) as error:
                    self.limiter.defer("export.arxiv.org", seconds)
                self.assertEqual(error.exception.code, "rate_limit_policy")

    def test_callers_exception_propagates_and_releases_slot(self):
        exception = OSError("private request detail")
        with self.assertRaises(OSError) as error:
            with self.limiter.slot("export.arxiv.org", 110):
                raise exception
        self.assertIs(error.exception, exception)
        self.assertEqual(self.enter("export.arxiv.org", limiter=self.new_limiter()), 103)

    def test_nested_same_provider_slot_is_rejected_without_deadlock(self):
        with self.limiter.slot("export.arxiv.org", 110):
            with self.assertRaises(RateLimitError) as error:
                self.enter("arxiv.org")
            self.assertEqual(error.exception.code, "rate_limit_policy")

    def test_unapproved_host_does_not_become_a_state_filename(self):
        with self.assertRaises(RateLimitError) as error:
            self.enter("../../private.example?query=secret")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_directory_must_be_absolute_outside_workspace_and_nonlinked(self):
        link = self.root / "linked"
        link.symlink_to(self.directory, target_is_directory=True)
        candidates = [None, "relative", self.workspace, self.workspace / "rates", link, link / "child"]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(RateLimitError) as error:
                    SharedHostLimiter(candidate, workspace_root=self.workspace)
                self.assertEqual(error.exception.code, "rate_limit_policy")
                self.assertNotIn(str(self.root), str(error.exception))

    def test_replaced_directory_is_rejected_before_lock_creation(self):
        self.directory.rmdir()
        self.directory.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(RateLimitError):
            self.enter("export.arxiv.org")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_symlink_and_hardlink_state_files_are_moved_aside_without_reading_or_modifying_target(self):
        outside = self.root / "private-source"
        outside.write_text("private-query-must-not-leak")
        outside.chmod(0o600)
        for name in ("arxiv.lock", "arxiv-lane-0.lock", "arxiv.json"):
            for hardlink in (False, True):
                with self.subTest(name=name, hardlink=hardlink):
                    path = self.directory / name
                    path.unlink(missing_ok=True)
                    if hardlink:
                        os.link(outside, path)
                    else:
                        path.symlink_to(outside)
                    before = len(list((self.directory / QUARANTINE).iterdir())) if (self.directory / QUARANTINE).exists() else 0
                    self.enter("export.arxiv.org", limiter=self.new_limiter(), duration=1000)
                    self.assertEqual(outside.read_text(), "private-query-must-not-leak")
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(path.stat().st_nlink if path.exists() else 1, 1)
                    self.assertEqual(len(list((self.directory / QUARANTINE).iterdir())), before + 1)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_group_writable_directory_is_tightened_and_state_never_trusted(self):
        self.directory.chmod(0o770)
        self.enter("export.arxiv.org")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        state = self.write_state(json.dumps({"version": 1, "next_wall": 0, "next_monotonic": 0,
                                             "written_monotonic": 0}))
        state.chmod(0o660)
        before = self.fake.clock()
        started = self.enter("export.arxiv.org", duration=1000)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)  # Rewritten fresh, not trusted.
        self.assertEqual(started, before + 3)

    def test_malformed_or_oversized_timing_state_is_moved_aside_and_pacing_restarts(self):
        valid = {"version": 1, "next_wall": 0, "next_monotonic": 0, "written_monotonic": 0}
        invalid = ["not json", "{}", "[]", json.dumps(valid) + " " * 4096]
        for key, value in [("version", True), ("next_wall", float("inf")),
                           ("next_wall", True), ("next_monotonic", -1),
                           ("written_monotonic", 1), ("next_wall", 10 ** 1000)]:
            invalid.append(json.dumps(dict(valid, **{key: value})))
        invalid.append(json.dumps(dict(valid, query="private query")))
        for content in invalid:
            with self.subTest(content=content[:100]):
                self.write_state(content)
                before = self.fake.clock()
                # Corrupt timing never disables retrieval; it restarts one interval later.
                self.assertEqual(self.enter("export.arxiv.org", duration=1000), before + 3)
                state = json.loads((self.directory / "arxiv.json").read_text())
                self.assertEqual(set(state), set(valid))
        self.assertEqual(len(list((self.directory / QUARANTINE).iterdir())), len(invalid))

    def test_only_timestamp_metadata_is_stored_outside_workspace(self):
        self.enter("export.arxiv.org")
        paths = sorted(self.directory.iterdir())
        self.assertEqual([path.name for path in paths], ["arxiv-lane-0.lock", "arxiv.json", "arxiv.lock"])
        state = json.loads((self.directory / "arxiv.json").read_text())
        self.assertEqual(set(state), {"version", "next_wall", "next_monotonic", "written_monotonic"})
        self.assertEqual(list(self.workspace.iterdir()), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths))


class CrossProcessRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "solver"
        self.workspace.mkdir()
        self.directory = self.root / "controller-rates"
        self.context = multiprocessing.get_context("spawn")

    def start(self, host, hold_seconds=0, release=None, deadline_seconds=15):
        events = self.context.Queue()
        self.addCleanup(events.close)
        process = self.context.Process(target=request_process,
            args=(str(self.directory), str(self.workspace), host, hold_seconds, events, release, deadline_seconds))
        process.start()

        def stop():
            if process.is_alive():
                process.terminate()
            process.join(5)
            process.close()

        self.addCleanup(stop)
        self.assertEqual(events.get(timeout=5)[0], "waiting")
        return process, events

    def event(self, events, expected):
        result = events.get(timeout=10)
        self.assertEqual(result[0], expected, result)
        return result[1]

    def test_independent_processes_share_arxiv_start_spacing(self):
        first, first_events = self.start("export.arxiv.org")
        first_start = self.event(first_events, "start")
        self.event(first_events, "end")
        second, second_events = self.start("arxiv.org")
        second_start = self.event(second_events, "start")
        self.event(second_events, "end")
        self.assertGreaterEqual(second_start - first_start, 2.98)

    def test_connection_lock_outlasts_spacing_and_contention_has_deadline(self):
        first, first_events = self.start("export.arxiv.org", hold_seconds=3.4)
        self.event(first_events, "start")
        second, second_events = self.start("arxiv.org")
        local = SharedHostLimiter(self.directory, workspace_root=self.workspace)
        started = time.monotonic()
        with self.assertRaises(RateLimitError) as error:
            with local.slot("export.arxiv.org", started + 0.15):
                self.fail("Concurrent connection bypassed the held lock.")
        self.assertEqual(error.exception.code, "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 1)
        first_end = self.event(first_events, "end")
        second_start = self.event(second_events, "start")
        self.event(second_events, "end")
        self.assertGreaterEqual(second_start, first_end)

    def test_crashed_process_releases_os_lock_but_retains_pacing(self):
        release = self.context.Event()
        first, first_events = self.start("api.crossref.org", release=release)
        first_start = self.event(first_events, "start")
        first.terminate()
        first.join(5)
        second, second_events = self.start("api.crossref.org")
        second_start = self.event(second_events, "start")
        self.event(second_events, "end")
        self.assertGreaterEqual(second_start - first_start, 0.98)
        self.assertLess(second_start - first_start, 5)

    def test_slow_wayback_body_does_not_block_other_archive_requests(self):
        release = self.context.Event()
        first, first_events = self.start("web.archive.org", release=release)
        first_start = self.event(first_events, "start")
        second, second_events = self.start("web.archive.org")
        second_start = self.event(second_events, "start")
        self.event(second_events, "end")
        self.assertTrue(first.is_alive())
        self.assertGreaterEqual(second_start - first_start, 2.98)
        self.assertLess(second_start - first_start, 6)
        release.set()
        first_end = self.event(first_events, "end")
        self.assertLess(second_start, first_end)

    def test_wayback_has_four_shared_lanes_with_spaced_starts_and_crash_recovery(self):
        holders, starts = [], []
        for _ in range(4):
            # Keep each simulated request's notification primitive separate: a
            # killed multiprocessing.Event waiter can strand its shared notify.
            release = self.context.Event()
            process, events = self.start("web.archive.org", release=release)
            starts.append(self.event(events, "start"))
            holders.append((process, events, release))
        self.assertTrue(all(process.is_alive() for process, _, _ in holders))
        waiters = [self.start("web.archive.org") for _ in range(3)]
        for _, events in waiters:
            with self.assertRaises(queue.Empty):
                events.get(timeout=0.05)
        local = SharedHostLimiter(self.directory, workspace_root=self.workspace)
        with self.assertRaises(RateLimitError) as error:
            with local.slot("web.archive.org", time.monotonic() + 0.15):
                self.fail("A fifth active archive request exceeded the shared limit.")
        self.assertEqual(error.exception.code, "deadline_exceeded")
        # A dead request releases only its lane; other in-flight bodies continue.
        holders[0][0].terminate()
        holders[0][0].join(5)
        # All three queued processes eventually use the freed lane, while the
        # remaining three slow requests are still reading their bodies.
        for _, events in waiters:
            starts.append(self.event(events, "start"))
            self.event(events, "end")
        starts.sort()
        self.assertTrue(all(process.is_alive() for process, _, _ in holders[1:]))
        self.assertTrue(all(right - left >= 2.98 for left, right in zip(starts, starts[1:])))
        for _, events, release in holders[1:]:
            release.set()
            self.event(events, "end")

    def test_retry_after_can_be_recorded_while_other_process_waits_for_start(self):
        local = SharedHostLimiter(self.directory, workspace_root=self.workspace)
        with local.slot("web.archive.org", time.monotonic() + 5):
            waiting, events = self.start("web.archive.org", deadline_seconds=0.6)
            time.sleep(0.1)
            # Another controller instance can update timing while this request
            # holds a lane and a second process waits on the start interval.
            other = SharedHostLimiter(self.directory, workspace_root=self.workspace)
            started = time.monotonic()
            other.defer("web.archive.org", 86400, deadline=started + 0.3)
            self.assertLess(time.monotonic() - started, 0.3)
            self.event(events, "error")
        state = json.loads((self.directory / "wayback.json").read_text())
        # Recorded, and capped at the longest pause any provider signal is honoured for.
        self.assertGreaterEqual(state["next_monotonic"], started + MAX_COOLDOWN_SECONDS)
        self.assertLess(state["next_monotonic"], started + 86400)


if __name__ == "__main__":
    unittest.main()
