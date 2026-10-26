"""Persistent capture bytes stay bounded, private and intact across processes."""
import base64
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from retrieval.historical_cache import CacheError, CaptureCache, MAX_BODY_BYTES, MAX_JSON_BYTES


URL = "https://papers.example/archived-paper"


def record(url=URL, body=b"Archived source content"):
    return {"url": url, "capture": "20200101120000",
            "archive_url": "https://web.archive.org/web/20200101120000id_/" + url,
            "mime_type": "text/plain", "cdx_digest": base64.b32encode(hashlib.sha1(body).digest()).decode("ascii"),
            "memento_datetime": "Wed, 01 Jan 2020 12:00:00 GMT",
            "body": base64.b64encode(body).decode("ascii"),
            "body_sha256": hashlib.sha256(body).hexdigest(), "content_encoding": "identity"}


def cache_writer(directory, workspace, number, events):
    try:
        cache = CaptureCache(directory, workspace_root=workspace)
        for index in range(40):
            url = URL + f"/{number}/{index}"
            cache.put(url, record(url))
            cache.put(URL, record(body=f"Complete body from process {number}, entry {index}".encode()))
            retrieved = cache.get(URL)
            if hashlib.sha256(base64.b64decode(retrieved["body"])).hexdigest() != retrieved["body_sha256"]:
                raise AssertionError("Concurrent cache read observed partial bytes")
        events.put("ok")
    except Exception as exc:
        events.put(str(exc))


def cache_lock_holder(directory, workspace, ready, release):
    cache = CaptureCache(directory, workspace_root=workspace)
    with cache._locked():
        ready.set()
        release.wait(10)


class CaptureCacheFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "solver"
        self.workspace.mkdir()
        self.directory = self.root / "controller" / "captures"
        self.cache = self.reopen()

    def reopen(self):
        return CaptureCache(self.directory, workspace_root=self.workspace)

    def entry_path(self, url=URL):
        return self.directory / (hashlib.sha256(url.encode()).hexdigest() + ".json")


class CaptureCacheTests(CaptureCacheFixture, unittest.TestCase):
    def test_complete_record_survives_instance_restart(self):
        self.assertIsNone(self.cache.get(URL))
        self.cache.put(URL, record())
        self.assertEqual(self.reopen().get(URL), record())
        self.assertIsNone(self.cache.get(URL + "/missing"))
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual({path.name for path in self.directory.iterdir()}, {".cache.lock", self.entry_path().name})

    def test_record_is_copied_and_replacement_is_complete(self):
        original = record()
        self.cache.put(URL, original)
        original["body"] = "changed caller state"
        retrieved = self.cache.get(URL)
        retrieved["capture"] = "changed returned state"
        self.assertEqual(self.reopen().get(URL), record())
        changed = record(body=b"Another complete archived capture")
        self.cache.put(URL, changed)
        self.assertEqual(self.cache.get(URL), changed)

    def test_record_schema_rejects_query_text_wrong_source_or_byte_tampering(self):
        variants = [dict(record(), query="model query"), dict(record(), text="model text"),
                    dict(record(), url=URL + "/wrong"), dict(record(), body="aW52YWxpZA=="),
                    dict(record(), body="%%%"), dict(record(), body=""),
                    dict(record(), body_sha256="0" * 64), dict(record(), capture=123)]
        for value in variants:
            with self.subTest(fields=set(value)):
                with self.assertRaises(CacheError) as error:
                    self.cache.put(URL, value)
                self.assertNotIn("model", str(error.exception))
        self.assertFalse(self.entry_path().exists())

    def test_maximum_body_round_trips_and_oversized_body_is_rejected(self):
        maximum = record(body=b"x" * MAX_BODY_BYTES)
        self.cache.put(URL, maximum)
        self.assertLess(self.entry_path().stat().st_size, MAX_JSON_BYTES)
        self.assertEqual(self.reopen().get(URL), maximum)
        with self.assertRaises(CacheError):
            self.cache.put(URL, record(body=b"x" * (MAX_BODY_BYTES + 1)))
        self.assertEqual(self.cache.get(URL), maximum)

    def test_corrupt_or_mismatched_disk_record_fails_closed_on_read_and_write(self):
        self.cache.put(URL, record())
        original = self.entry_path().read_text()
        envelope = json.loads(original)
        invalid = ["not JSON", "[" * 1001, "{}", original + " " * MAX_JSON_BYTES,
                   original.replace('"schema_version":1', '"schema_version":true'),
                   original.replace('"schema_version":1', '"schema_version":1,"schema_version":1')]
        envelope["url_sha256"] = "0" * 64
        invalid.append(json.dumps(envelope))
        envelope = json.loads(original)
        envelope["record"]["body"] = base64.b64encode(b"tampered").decode()
        invalid.append(json.dumps(envelope))
        for text in invalid:
            with self.subTest(prefix=text[:50]):
                self.entry_path().write_text(text)
                with self.assertRaises(CacheError):
                    self.cache.get(URL)
                with self.assertRaises(CacheError):
                    self.cache.put(URL, record())

    def test_hashed_filename_does_not_expose_url_or_enable_path_traversal(self):
        url = "https://papers.example/../../private?source=archive"
        self.cache.put(url, record(url))
        self.assertTrue(self.entry_path(url).exists())
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)
        for invalid in (None, b"url", "", "x" * 8193, "\ud800"):
            with self.assertRaises(CacheError):
                self.cache.get(invalid)

    def test_controller_directory_is_absolute_nonlinked_and_outside_workspace(self):
        link = self.root / "linked"
        link.symlink_to(self.directory, target_is_directory=True)
        for directory in (None, "relative", self.workspace, self.workspace / "cache", link, link / "child"):
            with self.subTest(directory=directory):
                with self.assertRaises(CacheError) as error:
                    CaptureCache(directory, workspace_root=self.workspace)
                self.assertNotIn(str(self.root), str(error.exception))

    def test_replaced_directory_is_rejected_before_open(self):
        self.directory.rmdir()
        self.directory.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(CacheError):
            self.cache.get(URL)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_linked_lock_or_record_is_rejected_without_reading_or_modifying_target(self):
        source = self.root / "private-source"
        source.write_text("private contents")
        source.chmod(0o600)
        for path in (self.directory / ".cache.lock", self.entry_path()):
            for hardlink in (False, True):
                with self.subTest(path=path.name, hardlink=hardlink):
                    path.unlink(missing_ok=True)
                    if hardlink:
                        os.link(source, path)
                    else:
                        path.symlink_to(source)
                    with self.assertRaises(CacheError) as error:
                        self.cache.get(URL)
                    self.assertNotIn("private contents", str(error.exception))
                    with self.assertRaises(CacheError):
                        self.cache.put(URL, record())
                    self.assertEqual(source.read_text(), "private contents")
                    path.unlink()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX named pipes")
    def test_nonregular_state_is_rejected_without_blocking(self):
        os.mkfifo(self.entry_path(), 0o600)
        started = time.monotonic()
        with self.assertRaises(CacheError):
            self.cache.get(URL)
        self.assertLess(time.monotonic() - started, 1)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_private_directory_and_files_are_required(self):
        self.cache.put(URL, record())
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in self.directory.iterdir()))
        self.entry_path().chmod(0o644)
        with self.assertRaises(CacheError):
            self.cache.get(URL)
        self.entry_path().chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaises(CacheError):
            self.cache.get(URL)

    def test_atomic_failure_preserves_previous_record_and_cleans_private_temporary(self):
        self.cache.put(URL, record())
        old = self.entry_path().read_bytes()

        def reject(temporary, target):
            self.assertEqual(temporary.parent, self.directory)
            self.assertFalse(temporary.is_relative_to(self.workspace))
            self.assertEqual(Path(target).read_bytes(), old)
            raise OSError("secret local path")

        with patch.object(Path, "replace", reject):
            with self.assertRaises(CacheError) as error:
                self.cache.put(URL, record(body=b"replacement"))
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.entry_path().read_bytes(), old)
        self.assertEqual(list(self.directory.glob(".capture-*.tmp")), [])

    def test_oldest_write_is_evicted_at_sixty_four_records(self):
        self.cache.put(URL, record())
        os.utime(self.entry_path(), (1, 1))
        for index in range(64):
            url = URL + f"/{index}"
            self.cache.put(url, record(url))
        self.assertEqual(len(list(self.directory.glob("*.json"))), 64)
        self.assertIsNone(self.cache.get(URL))
        self.assertEqual(self.reopen().get(URL + "/63"), record(URL + "/63"))

    def test_interrupted_temporary_is_cleaned_under_lock_and_unknown_files_rejected(self):
        temporary = self.directory / ".capture-abandoned.tmp"
        temporary.write_text("incomplete source bytes")
        temporary.chmod(0o600)
        self.cache.put(URL, record())
        self.assertFalse(temporary.exists())
        unexpected = self.directory / "unexpected.txt"
        unexpected.write_text("unexpected text")
        with self.assertRaises(CacheError):
            self.cache.put(URL, record())


class MultiProcessCacheTests(CaptureCacheFixture, unittest.TestCase):
    """Use spawn so each writer owns an independent interpreter and descriptors."""

    def process(self, target, args):
        process = multiprocessing.get_context("spawn").Process(target=target, args=args)
        process.start()

        def stop():
            if process.is_alive():
                process.terminate()
            process.join(5)
            process.close()

        self.addCleanup(stop)
        return process

    def test_concurrent_writers_and_readers_observe_complete_bounded_records(self):
        context = multiprocessing.get_context("spawn")
        events = context.Queue()
        self.addCleanup(events.close)
        processes = [self.process(cache_writer, (str(self.directory), str(self.workspace), number, events))
                     for number in range(2)]
        self.assertEqual([events.get(timeout=20) for _ in processes], ["ok", "ok"])
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 64)
        self.assertIsNotNone(self.reopen().get(URL))
        self.assertEqual(list(self.directory.glob(".capture-*.tmp")), [])

    def test_lock_wait_is_bounded_and_crashed_owner_releases_lock(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = self.process(cache_lock_holder, (str(self.directory), str(self.workspace), ready, release))
        self.assertTrue(ready.wait(5))
        started = time.monotonic()
        with patch("retrieval.historical_cache.LOCK_TIMEOUT", 0.1):
            with self.assertRaises(CacheError):
                self.cache.get(URL)
        self.assertLess(time.monotonic() - started, 1)
        process.terminate()
        process.join(5)
        self.cache.put(URL, record())
        self.assertEqual(self.cache.get(URL), record())


if __name__ == "__main__":
    unittest.main()
