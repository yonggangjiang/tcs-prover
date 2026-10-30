"""Proof handoffs bind explicit author references to safe, exact file bytes."""

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import proof_handoff as handoff


class ProofReferenceTests(unittest.TestCase):
    def test_explicit_markers_and_exact_json(self):
        for message in ('PROOF_FILE: proof.md', 'PROOF_FILE: "my proof.md"',
                        '{"proof_file":"proof.md"}', '```json\n{"proof_file":"proof.md"}\n```',
                        'The candidate is ready.\nPROOF_FILE: proof.md'):
            with self.subTest(message=message):
                self.assertEqual(handoff.extract_proof_reference(message),
                                 'my proof.md' if 'my proof' in message else 'proof.md')

    def test_observed_pointer_messages_allow_short_synopsis_and_absolute_links(self):
        messages = [
            'The complete audited proof is in [proof.md](/workspace/proof.md).\n\n'
            'It makes the standard interpretation explicit. It concludes \\[a=b.\\]\n\nElapsed work: 32 minutes.',
            'The [complete, audited proof]( /workspace/proof.md) is saved in `proof.md`.\n\n'
            'It derives three cases and checks all four conditions.',
            'See [proof](</workspace/my proof.md>).',
            'Proof is in [proof](/workspace/proof.md).',
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(handoff.extract_proof_reference(message),
                                 '/workspace/my proof.md' if 'my proof' in message else '/workspace/proof.md')

    def test_inline_proof_and_incidental_links_do_not_select_old_files(self):
        for message in ('**Proof.** Every case follows by induction. See [proof](proof.md).',
                        'Let x be arbitrary. See [proof](proof.md).',
                        'See [proof](proof.md). Here is the full proof: x=x.',
                        'The complete proof is in [proof](proof.md).\n\n**Proof.** x=x.',
                        'Complete proof: x=x.', 'See [notes](proof.md).',
                        'The complete proof is in [proof](proof.md).\n\n' + 'Long argument. ' * 200):
            with self.subTest(message=message[:100]):
                self.assertIsNone(handoff.extract_proof_reference(message))

    def test_invalid_or_conflicting_explicit_references_fail(self):
        for message in ('PROOF_FILE:', 'PROOF_FILE: "unterminated',
                        'PROOF_FILE: proof.md\nPROOF_FILE: other.md',
                        '{"proof_file":"proof.md","proof_file":"other.md"}',
                        '{"proof_file":3}', '{"proof_file":"proof.md","extra":true}',
                        'PROOF_FILE: proof.md\n{"proof_file":"other.md"}',
                        'The complete proof is in [proof](proof.md).\nPROOF_FILE: other.md',
                        'The [complete proof](proof.md) is saved in `other.md`.',
                        'The complete proof is in [proof](proof.md).\n\n{"proof_file":"other.md"}',
                        'The complete proof is in [proof](proof.md).\n\nSee [another](other.md).'):
            with self.subTest(message=message):
                with self.assertRaises(handoff.ProofHandoffError):
                    handoff.extract_proof_reference(message)


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd,
                     'Secure descriptor traversal is required')
class ProofSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'author'
        self.workspace.mkdir()
        self.proof = self.workspace / 'proof.md'
        self.data = 'A complete proof with λ and 😀.\r\nQED.\n'.encode('utf-8')
        self.proof.write_bytes(self.data)

    def resolve(self, reference='proof.md', **kwargs):
        return handoff.resolve_author_proof('PROOF_FILE: ' + reference, self.workspace, **kwargs)

    def test_snapshot_preserves_exact_bytes_and_survives_later_source_changes(self):
        result = self.resolve()
        self.assertEqual(result.content, self.data)
        self.assertEqual(result.sha256, hashlib.sha256(self.data).hexdigest())
        self.assertEqual(result.size_bytes, len(self.data))
        self.assertEqual(result.relative_path, 'proof.md')
        self.assertEqual(result.receipt(), {'proof_file': 'proof.md', 'sha256': result.sha256,
                                           'size_bytes': len(self.data)})
        self.proof.write_text('Changed after handoff')
        self.assertEqual(result.content, self.data)
        with self.assertRaises(FrozenInstanceError):
            result.text = 'changed'

    def test_absolute_inside_and_nested_paths_normalize_to_relative(self):
        self.assertEqual(self.resolve(str(self.proof)).relative_path, 'proof.md')
        subdir = self.workspace / 'proofs'
        subdir.mkdir()
        (subdir / 'full.tex').write_bytes(self.data)
        self.assertEqual(self.resolve('proofs/full.tex').relative_path, 'proofs/full.tex')
        self.assertEqual(self.resolve(max_bytes=len(self.data)).content, self.data)

    def test_inline_output_never_reads_a_stale_default_file(self):
        with patch.object(handoff, '_secure_read', side_effect=AssertionError('Unexpected read')):
            self.assertIsNone(handoff.resolve_author_proof('Here is a complete inline proof.', self.workspace))

    def test_escape_hidden_protected_and_nontext_paths_fail(self):
        refs = ['../proof.md', '/tmp/outside.md', 'proofs/../proof.md', '.codex/auth.md',
                'proofs/.hidden.md', 'prompts/author.md', 'INPUT.MD', 'draft.md',
                'checked-statement.md', 'INITIAL_PROMPT.md', 'AGENTS.md', 'saved-candidate.md',
                'latex-input.md', 'final.tex', 'job-settings.json', 'proof.json',
                'https://example.org/proof.md', 'file:///proof.md', 'C:\\proof.md',
                '//host/proof.md', 'proof.md:secret', 'proof.md#part', 'proof.md?x=1',
                '%2e%2e/proof.md', 'proof\x00.md', 'proof\n.md']
        for reference in refs:
            with self.subTest(reference=reference):
                with self.assertRaises(handoff.ProofHandoffError):
                    self.resolve(reference)

    def test_related_works_and_the_progress_summary_are_never_the_proof(self):
        related = self.workspace / 'related-works'
        related.mkdir()
        (related / 'paper.md').write_bytes(self.data)
        (related / 'notes').mkdir()
        (related / 'notes' / 'proof.md').write_bytes(self.data)
        (self.workspace / 'progress-summary.md').write_bytes(self.data)
        for reference in ('related-works/paper.md', 'related-works/notes/proof.md', 'Related-Works/paper.md',
                          str(related / 'paper.md'), 'progress-summary.md', 'PROGRESS-SUMMARY.md'):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(handoff.ProofHandoffError, 'protected or hidden'):
                    self.resolve(reference)
        # A proof of the author's own beside them is still handed over.
        self.assertEqual(self.resolve().content, self.data)

    def test_missing_empty_invalid_utf8_oversized_and_special_files_fail(self):
        with self.assertRaises(handoff.ProofHandoffError):
            self.resolve('missing.md')
        for data in (b'', b'  \r\n ', b'\xff', b'a\x00b'):
            self.proof.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(handoff.ProofHandoffError):
                self.resolve()
        self.proof.write_bytes(self.data)
        with self.assertRaises(handoff.ProofHandoffError):
            self.resolve(max_bytes=len(self.data) - 1)
        fifo = self.workspace / 'pipe.md'
        os.mkfifo(fifo)
        with self.assertRaises(handoff.ProofHandoffError):
            self.resolve('pipe.md')
        (self.workspace / 'directory.md').mkdir()
        with self.assertRaises(handoff.ProofHandoffError):
            self.resolve('directory.md')

    def test_symlink_files_directories_workspace_and_hardlinks_fail(self):
        (self.workspace / 'linked.md').symlink_to(self.proof)
        os.link(self.proof, self.workspace / 'hard.md')
        (self.workspace / 'linked-dir').symlink_to(self.workspace, target_is_directory=True)
        for reference in ('linked.md', 'linked-dir/proof.md', 'hard.md', 'proof.md'):
            with self.subTest(reference=reference), self.assertRaises(handoff.ProofHandoffError):
                self.resolve(reference)
        (self.workspace / 'hard.md').unlink()
        alias = self.root / 'workspace-alias'
        alias.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(handoff.ProofHandoffError):
            handoff.resolve_author_proof('PROOF_FILE: proof.md', alias)

    def test_file_replacement_or_mutation_during_read_fails(self):
        original_read = os.read
        for replace in (False, True):
            self.proof.write_bytes(self.data)
            changed = False
            def read(fd, size):
                nonlocal changed
                result = original_read(fd, size)
                if not changed:
                    changed = True
                    if replace:
                        self.proof.unlink()
                    self.proof.write_bytes(b'Changed candidate.')
                return result
            with self.subTest(replace=replace), patch.object(handoff.os, 'read', side_effect=read):
                with self.assertRaises(handoff.ProofHandoffError):
                    self.resolve()

    def test_directory_replacement_during_read_fails(self):
        directory = self.workspace / 'proofs'
        directory.mkdir()
        (directory / 'complete.md').write_bytes(self.data)
        original_read = os.read
        changed = False
        def read(fd, size):
            nonlocal changed
            result = original_read(fd, size)
            if not changed:
                changed = True
                directory.rename(self.workspace / 'old-proofs')
                directory.mkdir()
                (directory / 'complete.md').write_bytes(self.data)
            return result
        with patch.object(handoff.os, 'read', side_effect=read):
            with self.assertRaises(handoff.ProofHandoffError):
                self.resolve('proofs/complete.md')

    def test_workspace_swap_during_canonicalization_cannot_read_outside(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'proof.md').write_text('Outside proof must not be read.')
        original_resolve = Path.resolve
        changed = False
        def resolve(path, *args, **kwargs):
            nonlocal changed
            if path == self.workspace and not changed:
                changed = True
                self.workspace.rename(self.root / 'author-old')
                self.workspace.symlink_to(outside, target_is_directory=True)
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve), patch.object(handoff.os, 'read') as read:
            with self.assertRaisesRegex(handoff.ProofHandoffError, 'workspace changed'):
                self.resolve()
            read.assert_not_called()

    def test_foreign_owner_and_invalid_size_limits_fail(self):
        with patch.object(handoff.os, 'geteuid', return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(handoff.ProofHandoffError, 'belong'):
                self.resolve()
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(handoff.ProofHandoffError):
                self.resolve(max_bytes=limit)

    def test_platform_without_secure_descriptor_access_fails_closed(self):
        with patch.object(handoff.os, 'supports_dir_fd', set()):
            with self.assertRaisesRegex(handoff.ProofHandoffError, 'descriptor'):
                self.resolve()
        original_geteuid = handoff.os.geteuid
        try:
            del handoff.os.geteuid
            with self.assertRaisesRegex(handoff.ProofHandoffError, 'descriptor'):
                self.resolve()
        finally:
            handoff.os.geteuid = original_geteuid


if __name__ == '__main__':
    unittest.main()
