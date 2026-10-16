"""Authentication refresh persistence never overwrites a newer controller login."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import historical_discovery_auth as auth


class DiscoveryAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.master = self.root / 'master' / 'auth.json'
        self.child = self.root / 'child' / 'auth.json'
        for path in (self.master, self.child):
            path.parent.mkdir(mode=0o700)
        self.original = self.credentials('original')
        self.refreshed = self.credentials('refreshed')
        self.write(self.master, self.original)
        self.write(self.child, self.refreshed)
        self.original_sha = hashlib.sha256(self.original).hexdigest()

    @staticmethod
    def credentials(marker):
        return (json.dumps({'tokens': {'access_token': marker, 'refresh_token': marker + '-refresh'},
                            'last_refresh': '2026-10-16T00:00:00Z'}) + '\n').encode()

    @staticmethod
    def write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def preserve(self):
        return auth.preserve_refresh(self.master, self.child, self.original_sha)

    def test_refreshed_auth_replaces_only_the_original_master_privately(self):
        (self.child.parent / 'config.toml').write_text('untrusted = true')
        (self.child.parent / 'session.jsonl').write_text('private child state')
        self.assertTrue(self.preserve())
        self.assertEqual(self.master.read_bytes(), self.refreshed)
        self.assertEqual(self.master.stat().st_mode & 0o777, 0o600)
        self.assertEqual({path.name for path in self.master.parent.iterdir()},
                         {'auth.json', '.auth-refresh.lock'})
        self.assertEqual(self.child.read_bytes(), self.refreshed)

    def test_unchanged_child_does_not_replace_or_create_a_lock(self):
        self.write(self.child, self.original)
        inode = self.master.stat().st_ino
        self.assertFalse(self.preserve())
        self.assertEqual(self.master.stat().st_ino, inode)
        self.assertEqual(list(self.master.parent.iterdir()), [self.master])

    def test_newer_master_cannot_be_overwritten_by_a_stale_child(self):
        newer = self.credentials('newer-master')
        self.write(self.master, newer)
        self.assertFalse(self.preserve())
        self.assertEqual(self.master.read_bytes(), newer)

    def test_concurrent_children_preserve_exactly_one_refresh(self):
        second = self.root / 'second-child' / 'auth.json'
        second.parent.mkdir(mode=0o700)
        newer = self.credentials('second-refresh')
        self.write(second, newer)
        barrier = threading.Barrier(2)
        def preserve(path):
            barrier.wait(timeout=5)
            return auth.preserve_refresh(self.master, path, self.original_sha)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(preserve, path) for path in (self.child, second)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(results), [False, True])
        self.assertIn(self.master.read_bytes(), (self.refreshed, newer))

    def test_invalid_auth_json_or_credentials_never_replace_master(self):
        invalid = [b'{', b'[]', b'{}', b'{"tokens":true}', b'{"tokens":{}}',
                   b'{"OPENAI_API_KEY":123}', b'{"OPENAI_API_KEY":" "}',
                   b'{"tokens":{"access_token":"x"},"tokens":{}}',
                   b'{"OPENAI_API_KEY":"x","extra":NaN}', b'\xff']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.write(self.child, raw)
                self.assertFalse(self.preserve())
                self.assertEqual(self.master.read_bytes(), self.original)

    def test_api_key_auth_is_supported_but_malformed_master_is_not_replaced(self):
        self.write(self.child, b'{"OPENAI_API_KEY":"fixture-refreshed-key"}')
        self.assertTrue(self.preserve())
        malformed = b'{"tokens":[]}'
        self.write(self.master, malformed)
        self.assertFalse(auth.preserve_refresh(self.master, self.child, hashlib.sha256(malformed).hexdigest()))
        self.assertEqual(self.master.read_bytes(), malformed)

    def test_missing_or_oversized_auth_does_not_replace_master(self):
        self.child.unlink()
        self.assertFalse(self.preserve())
        self.write(self.child, b' ' * (auth.MAX_AUTH_BYTES + 1))
        self.assertFalse(self.preserve())
        self.assertEqual(self.master.read_bytes(), self.original)
        self.write(self.child, self.refreshed)
        self.master.unlink()
        self.assertFalse(self.preserve())
        self.assertFalse(self.master.exists())

    def test_linked_master_child_and_lock_are_rejected(self):
        for which in ('master', 'child', 'lock'):
            with self.subTest(which=which):
                target = self.root / ('protected-' + which)
                self.write(target, self.original if which == 'master' else self.refreshed)
                path = self.master if which == 'master' else self.child if which == 'child' else self.master.parent / '.auth-refresh.lock'
                path.unlink(missing_ok=True)
                path.symlink_to(target)
                before = target.read_bytes()
                self.assertFalse(self.preserve())
                self.assertEqual(target.read_bytes(), before)
                path.unlink()
                if which != 'lock':
                    self.write(path, self.original if which == 'master' else self.refreshed)

    def test_linked_parent_and_hardlinked_auth_are_rejected(self):
        alias = self.root / 'child-alias'
        alias.symlink_to(self.child.parent, target_is_directory=True)
        self.assertFalse(auth.preserve_refresh(self.master, alias / 'auth.json', self.original_sha))
        os.link(self.child, self.root / 'child-hardlink')
        self.assertFalse(self.preserve())
        self.assertEqual(self.master.read_bytes(), self.original)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX ownership')
    def test_unowned_or_group_writable_files_and_parents_are_rejected(self):
        with patch.object(auth.os, 'getuid', return_value=os.getuid() + 1):
            self.assertFalse(self.preserve())
        for path in (self.master, self.child, self.master.parent, self.child.parent):
            previous = path.stat().st_mode & 0o777
            with self.subTest(path=path.name):
                path.chmod(previous | 0o020)
                try:
                    self.assertFalse(self.preserve())
                    self.assertEqual(self.master.read_bytes(), self.original)
                finally:
                    path.chmod(previous)

    def test_failed_atomic_replace_leaves_master_and_cleans_staged_file(self):
        with patch.object(auth.os, 'replace', side_effect=OSError('injected failure')):
            self.assertFalse(self.preserve())
        self.assertEqual(self.master.read_bytes(), self.original)
        self.assertFalse(list(self.master.parent.glob('*.tmp')))

    def test_master_changed_while_staging_is_not_overwritten(self):
        real_mkstemp = auth.tempfile.mkstemp
        newer = self.credentials('changed-during-staging')
        def stage(**kwargs):
            result = real_mkstemp(**kwargs)
            self.write(self.master, newer)
            return result
        with patch.object(auth.tempfile, 'mkstemp', side_effect=stage):
            self.assertFalse(self.preserve())
        self.assertEqual(self.master.read_bytes(), newer)
        self.assertFalse(list(self.master.parent.glob('*.tmp')))

    def test_child_changed_while_staging_is_not_copied(self):
        real_mkstemp = auth.tempfile.mkstemp
        def stage(**kwargs):
            result = real_mkstemp(**kwargs)
            self.write(self.child, self.credentials('changed-during-staging'))
            return result
        with patch.object(auth.tempfile, 'mkstemp', side_effect=stage):
            self.assertFalse(self.preserve())
        self.assertEqual(self.master.read_bytes(), self.original)

    def test_invalid_digest_and_non_auth_destinations_are_inert(self):
        for digest in (None, '', 'A' * 64, 'a' * 63):
            self.assertFalse(auth.preserve_refresh(self.master, self.child, digest))
        other = self.master.with_name('config.json')
        self.write(other, self.original)
        self.assertFalse(auth.preserve_refresh(other, self.child, self.original_sha))
        self.assertFalse(auth.preserve_refresh(self.master, self.master, self.original_sha))
        self.assertEqual(other.read_bytes(), self.original)

    def test_contended_lock_is_bounded_and_does_not_overwrite_credentials(self):
        with auth._locked(self.master.parent), patch.object(auth, 'LOCK_WAIT_SECONDS', 0.02):
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(self.preserve).result(timeout=2)
        self.assertFalse(result)
        self.assertEqual(self.master.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
