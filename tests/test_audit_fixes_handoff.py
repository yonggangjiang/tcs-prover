"""Audit regressions: decorated PROOF_FILE markers and a busy run folder."""

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from runner import proof_handoff as handoff


class DecoratedMarkerTests(unittest.TestCase):
    def test_backticks_and_trailing_periods_around_marker_paths_are_accepted(self):
        for message in ('PROOF_FILE: `proof.md`', 'PROOF_FILE: proof.md.', 'PROOF_FILE: `proof.md`.',
                        'PROOF_FILE: "proof.md".', '`PROOF_FILE: proof.md`', '`PROOF_FILE: proof.md`.',
                        'The proof is written.\n  `PROOF_FILE: "proof.md"`  ',
                        'Done.\r\n`PROOF_FILE: proof.md`\r\nThanks.',
                        'PROOF_FILE: proof.md\nPROOF_FILE: `proof.md`.'):
            with self.subTest(message=message):
                self.assertEqual(handoff.extract_proof_reference(message), 'proof.md')
        self.assertEqual(handoff.extract_proof_reference('PROOF_FILE: "my proof.md".'), 'my proof.md')
        self.assertEqual(handoff.extract_proof_reference('PROOF_FILE: `proofs/full.tex`'), 'proofs/full.tex')

    def test_decorations_never_rescue_empty_colonless_or_conflicting_markers(self):
        for message in ('PROOF_FILE: ``', 'PROOF_FILE: ` `', '`PROOF_FILE:`', 'PROOF_FILE `proof.md`',
                        'PROOF_FILE proof.md.', 'PROOF_FILE: "unterminated.',
                        'PROOF_FILE: `proof.md`\nPROOF_FILE: `other.md`.'):
            with self.subTest(message=message), self.assertRaises(handoff.ProofHandoffError):
                handoff.extract_proof_reference(message)

    def test_marker_inside_a_sentence_is_still_inline_text(self):
        self.assertIsNone(handoff.extract_proof_reference('I did not use `PROOF_FILE: proof.md` here.'))

    def test_code_span_marker_beside_another_pointer_stays_text_as_before(self):
        # Neither a conflicting reference nor a mixed pointer.
        for message, expected in (
                ('PROOF_FILE: proof.md\n\nThe rejected draft was\n`PROOF_FILE: draft.md`', 'proof.md'),
                ('Not {"proof_file": "x.md"}, but\n`PROOF_FILE: proof.md`', None)):
            with self.subTest(message=message):
                self.assertEqual(handoff.extract_proof_reference(message), expected)


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd,
                     'Secure descriptor traversal is required')
class HandoffWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'author'
        self.folder = self.workspace / 'proofs'
        self.folder.mkdir(parents=True)
        self.data = 'A complete proof with λ.\nQED.\n'.encode('utf-8')
        for path in (self.workspace / 'proof.md', self.folder / 'complete.md'):
            path.write_bytes(self.data)
        self.writes = 0

    def resolve(self, message):
        return handoff.resolve_author_proof(message, self.workspace)

    def controller_activity(self):
        # Like the UI while the critic starts: re-apply the run folder's mode and
        # add unrelated entries beside the author's files.
        self.writes += 1
        for folder in (self.workspace, self.folder):
            folder.chmod(stat.S_IMODE(folder.stat().st_mode))
            staged = folder / f'settings-{self.writes}.tmp'
            staged.write_text('{}')
            staged.replace(folder / f'settings-{self.writes}.json')
            (folder / f'state-{self.writes}').mkdir()

    def test_decorated_marker_reads_the_named_file(self):
        snapshot = self.resolve('PROOF_FILE: `proofs/complete.md`.')
        self.assertEqual((snapshot.relative_path, snapshot.content), ('proofs/complete.md', self.data))

    def test_link_pointer_with_a_code_span_marker_line_reads_the_linked_file(self):
        # The link pointer treats that line as synopsis text, not a mixed pointer.
        link = f'{self.workspace}/proof.md'
        for message in (f'The complete proof is in [proof.md]({link}).\n\n`PROOF_FILE: proof.md`',
                        f'See [proof]({link}).\n\n`PROOF_FILE: proof.md`',
                        f'The [complete proof]({link}) is saved in `proof.md`.\n\n`PROOF_FILE: proof.md`'):
            with self.subTest(message=message):
                snapshot = self.resolve(message)
                self.assertEqual((snapshot.relative_path, snapshot.content), ('proof.md', self.data))

    def test_a_code_span_pointer_that_cannot_be_read_is_reviewed_as_text(self):
        # Before code-span markers were recognized these were inline text; a
        # refused handoff ends the run, so they stay text unless the file resolves.
        (self.workspace / 'blank.md').write_text('  \n')
        for message in ('`PROOF_FILE: missing.md`', '`PROOF_FILE: proof`', '`PROOF_FILE:`', '`PROOF_FILE: blank.md`',
                        '`PROOF_FILE: ../proof.md`', '`PROOF_FILE: proof.md`\n`PROOF_FILE: proofs/complete.md`'):
            with self.subTest(message=message):
                self.assertIsNone(self.resolve(message))
        snapshot = self.resolve('Done.\n`PROOF_FILE: proofs/complete.md`.')
        self.assertEqual((snapshot.relative_path, snapshot.content), ('proofs/complete.md', self.data))
        # A plain marker still refuses a bad path, as before.
        with self.assertRaisesRegex(handoff.ProofHandoffError, 'could not be safely read'):
            self.resolve('PROOF_FILE: missing.md')

    def test_a_complete_proof_ending_in_a_code_span_marker_line_is_inline(self):
        proof = 'Step 1 holds.\n' * 200 + '\n`PROOF_FILE: proof.md`'
        self.assertIsNone(handoff.extract_proof_reference(proof))
        self.assertIsNone(self.resolve(proof))

    def test_decorations_do_not_bypass_path_checks(self):
        (self.root / 'proof.md').write_bytes(self.data)
        (self.workspace / 'input.md').write_bytes(self.data)
        (self.workspace / 'proof.json').write_bytes(self.data)
        for reference, reason in (('`../proof.md`', 'parent directories'), ('`input.md`', 'protected'),
                                  ('`proof.json`', 'Markdown'), ('proof.md/..', 'parent directories')):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(handoff.ProofHandoffError, reason):
                    self.resolve('PROOF_FILE: ' + reference)
        # Pathlib already drops a final "." component, so this stays the same value.
        self.assertEqual(handoff.extract_proof_reference('PROOF_FILE: proof.md/.'), 'proof.md/.')

    def test_harmless_folder_activity_during_handoff_succeeds(self):
        original_read, original_fstat, original_resolve = os.read, os.fstat, Path.resolve
        def read(fd, size):
            self.controller_activity()
            return original_read(fd, size)
        def fstat(fd):
            self.controller_activity()
            return original_fstat(fd)
        def resolve(path, *args, **kwargs):
            self.controller_activity()
            return original_resolve(path, *args, **kwargs)
        with patch.object(handoff.os, 'read', side_effect=read), \
                patch.object(handoff.os, 'fstat', side_effect=fstat), patch.object(Path, 'resolve', resolve):
            for reference in ('proof.md', 'proofs/complete.md'):
                with self.subTest(reference=reference):
                    snapshot = self.resolve('PROOF_FILE: ' + reference)
                    self.assertEqual((snapshot.relative_path, snapshot.content), (reference, self.data))
        self.assertGreater(self.writes, 0)

    def test_replacing_the_workspace_during_read_still_fails_closed(self):
        original_read = os.read
        for replacement in ('directory', 'symlink'):
            moved = self.root / ('author-' + replacement)
            changed = False
            def read(fd, size):
                nonlocal changed
                if not changed:
                    changed = True
                    self.workspace.rename(moved)
                    if replacement == 'symlink':
                        self.workspace.symlink_to(moved, target_is_directory=True)
                    else:
                        self.workspace.mkdir()
                        (self.workspace / 'proof.md').write_bytes(self.data)
                return original_read(fd, size)
            with self.subTest(replacement=replacement), patch.object(handoff.os, 'read', side_effect=read):
                with self.assertRaisesRegex(handoff.ProofHandoffError, 'workspace changed'):
                    self.resolve('PROOF_FILE: proof.md')
                self.assertTrue(changed)


if __name__ == '__main__':
    unittest.main()
