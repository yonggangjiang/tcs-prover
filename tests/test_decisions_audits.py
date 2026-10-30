"""Decisions of the audits package: proof-pointer parsing and the author pause message.

No model call is made: the scripted Codex app-server double (tests/test_goal_runtime.py)
stands in for the author.
"""

import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import proof_handoff as handoff

import test_goal_runtime as goal_test


SECURE = hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd


class ProofPointerTests(unittest.TestCase):
    """I-030 and I-031: what a final message's PROOF_FILE line means."""

    def test_near_miss_pointers_with_a_clear_intent_are_accepted(self):
        for message in ("PROOF_FILE proof.md", "Done.\nPROOF_FILE proof.md", "PROOF_FILE proof.md.",
                        "PROOF_FILE `proof.md`", "**PROOF_FILE:** proof.md", "**PROOF_FILE**: proof.md",
                        "*PROOF_FILE:* proof.md", "proof_file: proof.md", "**PROOF_FILE: proof.md**",
                        "PROOF_FILE: **proof.md**", "PROOF_FILE: *proof.md*.",
                        "PROOF_FILE: proof.md (complete)", "PROOF_FILE: proof.md — the full proof",
                        "PROOF_FILE: proof.md, as requested.", "PROOF_FILE: `proof.md` (final)",
                        "PROOF_FILE: proof.md\nPROOF_FILE: ./proof.md",
                        "PROOF_FILE: proof.md\n\nIt uses [Lemma 2](APPROACHES/A002.md) and [notes](notes.md)."):
            with self.subTest(message=message):
                self.assertEqual(handoff.extract_proof_reference(message), "proof.md")
        self.assertEqual(handoff.extract_proof_reference('PROOF_FILE: "my proof.md" (complete)'), "my proof.md")
        self.assertEqual(handoff.extract_proof_reference("PROOF_FILE: my proof.md"), "my proof.md")
        self.assertEqual(handoff.extract_proof_reference("PROOF_FILE: proofs/full.tex (all cases)"), "proofs/full.tex")

    def test_a_suffix_inside_a_longer_path_is_never_cut_out(self):
        # The whole value is kept, and the path checks refuse it.
        for value in ("proof.md?x=1", "proof.md:secret", "proof.md#part", "proof.md.bak", "proof.md_old"):
            with self.subTest(value=value):
                self.assertEqual(handoff.extract_proof_reference("PROOF_FILE: " + value), value)

    def test_pointers_without_a_clear_intent_are_refused_with_their_reason(self):
        cases = {
            "PROOF_FILE: proof.md\nSee [notes](notes.md).": "mixed pointers are ambiguous",
            "See [notes](notes.md).\n\nPROOF_FILE: proof.md": "mixed pointers are ambiguous",
            'PROOF_FILE: proof.md\n\n{"proof_file": "other.md"}': "mixed pointers are ambiguous",
            "PROOF_FILE: proof.md\nPROOF_FILE: other.md": "conflicting proof file references (proof.md, other.md)",
            '{"proof_file":"proof.md","extra":true}': "only a nonempty proof_file string",
            "PROOF_FILE": "followed by a local proof path",
            "PROOF_FILE: ``": "nonempty path",
        }
        for message, reason in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(handoff.ProofHandoffError, re.escape(reason)):
                    handoff.extract_proof_reference(message)

    def test_a_marker_in_a_long_message_is_not_a_pointer(self):
        proof = "The claim holds for every n by induction on n.\n" * 40
        for message in (proof + "PROOF_FILE: proof.md", "PROOF_FILE: proof.md\n\n" + proof,
                        proof + "**PROOF_FILE:** proof.md\nSee [notes](notes.md)."):
            with self.subTest(message=message[-40:]):
                self.assertIsNone(handoff.extract_proof_reference(message))
        # A pointer-sized message stays a pointer, however it is spread out.
        self.assertEqual(handoff.extract_proof_reference("Summary.\n" * 100 + "PROOF_FILE: proof.md"), "proof.md")

    def test_a_near_miss_marker_kept_from_the_captured_proof_is_text_of_the_edited_proof(self):
        # The critic keeps a line of the proof it reviews; only a pointer it writes itself is refused.
        node = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
        for marker in ("PROOF_FILE proof.md", "**PROOF_FILE:** proof.md", "proof_file: proof.md"):
            captured = f"Proof.\nStep 1 holds.\n{marker}\nStep 2 holds.\n"
            files = (("proof.md", captured), ("statement.md", "S"))
            context = {"state": {"statement": "S", "solution": captured}, "visit": 1}
            with self.subTest(marker=marker):
                edited = {"verdict": "pass", "solution": captured.replace("Step 2 holds.", "Step 2 holds by (1)."),
                          "bugs": "", "fixes": "Justified step 2."}
                self.assertEqual(runtime._accept_node_answer(node, context, files, edited, "{}"), edited)
                pointer = {"verdict": "pass", "solution": captured.replace(marker, "PROOF_FILE other.md"), "bugs": "",
                           "fixes": "Pointed to the file."}
                with self.assertRaisesRegex(runtime.InvalidAnswer, "file pointer instead of the complete proof"):
                    runtime._accept_node_answer(node, context, files, pointer, "{}")


@unittest.skipUnless(SECURE, "Secure descriptor traversal is required")
class ProofResolutionTests(unittest.TestCase):
    """I-030 (a complete inline proof with a leftover marker) and I-129 (read failures)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name).resolve()
        self.proof = "# Proof\n\nStep 1 holds by induction.\n" + "Each case is checked in detail.\n" * 60

    def message(self, marker="PROOF_FILE: proof.md"):
        return "Here is the complete proof.\n\n" + self.proof + "\n" + marker + "\n"

    def test_a_complete_inline_proof_is_reviewed_without_its_marker_line(self):
        (self.workspace / "proof.md").write_text(self.proof)
        for marker in ("PROOF_FILE: proof.md", "**PROOF_FILE:** proof.md", "PROOF_FILE proof.md"):
            with self.subTest(marker=marker):
                snapshot = handoff.resolve_author_proof(self.message(marker), self.workspace)
                self.assertNotIn("PROOF_FILE", snapshot.text)
                self.assertIn(self.proof.strip(), snapshot.text)
                self.assertEqual(snapshot.receipt()["kind"], "inline")
                self.assertEqual(snapshot.receipt()["removed_marker_lines"], 1)

    def test_a_missing_or_unreadable_named_file_leaves_the_inline_proof(self):
        for content in (None, b"\xff\xfe not text"):
            with self.subTest(content=content):
                (self.workspace / "proof.md").unlink(missing_ok=True)
                if content is not None:
                    (self.workspace / "proof.md").write_bytes(content)
                snapshot = handoff.resolve_author_proof(self.message(), self.workspace)
                self.assertNotIn("PROOF_FILE", snapshot.text)
                self.assertEqual(snapshot.receipt()["kind"], "inline")

    def test_a_named_file_with_another_proof_asks_which_one_is_submitted(self):
        (self.workspace / "proof.md").write_text("An older proof with a gap.\n")
        with self.assertRaisesRegex(handoff.ProofHandoffError,
                                    "also names proof.md as the proof file, and the two differ. Submit one"):
            handoff.resolve_author_proof(self.message(), self.workspace)

    def test_the_critic_reviews_the_inline_proof_without_the_marker(self):
        (self.workspace / "proof.md").write_text(self.proof)
        node = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
        state = {"statement": "Task", "solution": self.message()}
        with patch.object(runtime, "emit"):
            files = dict(runtime._prepare_node_files(node, state, 1, str(self.workspace)))
        self.assertNotIn("PROOF_FILE", files["proof.md"])
        self.assertIn(self.proof.strip(), files["proof.md"])
        self.assertEqual(json.loads(files["manifest.json"])["source"]["kind"], "inline")
        self.assertEqual(state["solution"], files["proof.md"])

    def test_words_around_the_path_still_name_the_proof_file(self):
        # Review finding audits-decisions-6: words before the path made it unreadable.
        (self.workspace / "proof.md").write_text(self.proof)
        for message in ("PROOF_FILE: see proof.md", "PROOF_FILE: the proof is in `proof.md`",
                        "PROOF_FILE: saved as **proof.md** (complete)", 'PROOF_FILE: in "proof.md".'):
            with self.subTest(message=message):
                snapshot = handoff.resolve_author_proof(message, self.workspace)
                self.assertEqual((snapshot.relative_path, snapshot.text), ("proof.md", self.proof))
        # The whole value still wins when it names a file, spaces included.
        (self.workspace / "my proof.md").write_text("The spaced file's proof.\n")
        snapshot = handoff.resolve_author_proof("PROOF_FILE: my proof.md", self.workspace)
        self.assertEqual(snapshot.relative_path, "my proof.md")
        # Neither candidate readable: the reason names the path as written.
        with self.assertRaisesRegex(handoff.ProofHandoffError, r"\(see missing\.md: file not found\)"):
            handoff.resolve_author_proof("PROOF_FILE: see missing.md", self.workspace)
        # The fallback never reopens a refused path: every check still runs on the word.
        (self.workspace / "proofs").mkdir()
        (self.workspace / "input.md").write_text(self.proof)
        (self.workspace / ".hidden.md").write_text(self.proof)
        for reference in ("see ../proof.md", "see input.md", "see proof.md/..", "see proofs/../proof.md",
                          "see .hidden.md", "proof.md/..", f"see {self.workspace.parent}/proof.md"):
            with self.subTest(reference=reference), self.assertRaises(handoff.ProofHandoffError):
                handoff.resolve_author_proof("PROOF_FILE: " + reference, self.workspace)

    def test_a_named_file_with_only_another_title_or_spacing_is_the_same_proof(self):
        # Review finding audits-decisions-1: a title line alone made the two "differ".
        body = "Step 1 holds by induction.\n" + "Each case is checked in detail.\n" * 60
        message = "Here is the complete proof.\n\n" + body + "\nPROOF_FILE: proof.md\n"
        for named in ("# Proof\n\n" + body, "## Proof of the theorem\n" + body.replace("\n", "\n\n"),
                      body.replace("induction.", "induction.  ")):
            with self.subTest(named=named[:30]):
                (self.workspace / "proof.md").write_text(named)
                snapshot = handoff.resolve_author_proof(message, self.workspace)
                self.assertEqual(snapshot.receipt()["kind"], "inline")
                self.assertNotIn("PROOF_FILE", snapshot.text)
        (self.workspace / "proof.md").write_text("# Proof\n\n" + body.replace("induction", "contradiction"))
        with self.assertRaisesRegex(handoff.ProofHandoffError, "the two differ"):
            handoff.resolve_author_proof(message, self.workspace)

    def test_a_different_named_file_stops_the_handoff_with_its_reason(self):
        # Owner decision (audits-decisions-1): the resumed author is told this reason by its
        # handoff_refused prompt (see HandoffTests in tests/test_decisions_graph.py).
        (self.workspace / "proof.md").write_text("An older proof with a gap.\n")
        node = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
        state = {"statement": "Task", "solution": self.message()}
        with patch.object(runtime, "emit"), self.assertRaisesRegex(
                runtime.Error, "Cannot hand the submitted proof to the critic: The response contains a complete proof"):
            runtime._prepare_node_files(node, state, 1, str(self.workspace))

    def test_read_failures_name_the_file_and_the_cause_but_never_the_content(self):
        (self.workspace / "bad.md").write_bytes(b"secret-words \xff tail")
        cases = {"missing.md": "(missing.md: file not found)", "bad.md": "(bad.md: not UTF-8 text at byte 13)",
                 "sub/proof.md": "(sub/proof.md: folder sub not found)"}
        for reference, cause in cases.items():
            with self.subTest(reference=reference):
                with self.assertRaises(handoff.ProofHandoffError) as failure:
                    handoff.resolve_author_proof("PROOF_FILE: " + reference, self.workspace)
                self.assertIn("The referenced proof file could not be safely read " + cause, str(failure.exception))
                self.assertNotIn("secret-words", str(failure.exception))
        if os.geteuid() != 0:
            locked = self.workspace / "locked.md"
            locked.write_text("Hidden content")
            locked.chmod(0)
            self.addCleanup(locked.chmod, 0o600)
            with self.assertRaisesRegex(handoff.ProofHandoffError, r"\(locked\.md: permission denied\)\.$"):
                handoff.resolve_author_proof("PROOF_FILE: locked.md", self.workspace)


class CodexPauseMessageTests(unittest.TestCase):
    """The pause status says whether the author's turn was interrupted (Claude: tests/test_claude_cli.py)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = patch.object(runtime.subprocess, "Popen", return_value=Mock())
        popen.start()
        self.addCleanup(popen.stop)
        self.pause = self.directory / "pause-request.json"

    def paused(self, ending):
        """Pause a running turn that then ends with `ending`; the author's pause status texts."""
        fake = goal_test.Runtime([[]])
        session = runtime.goal_session(fake, goal_test.PROMPT, prompts=goal_test.PROMPTS, settings=goal_test.SETTINGS,
                                       options={"goal_cwd": str(self.directory), "goal_pause_file": str(self.pause)})
        self.addCleanup(session.close)

        def pause(rpc):
            self.pause.write_text("{}\n")
            self.assertTrue(fake.interrupted.wait(3))
            rpc.messages.extend([goal_test.goal("paused"), goal_test.completed(ending)])

        fake.on_empty = pause
        with self.assertRaises(runtime.WorkflowPaused):
            next(session)
        return [event["text"] for event in fake.events if event.get("label") == "Author paused"]

    def test_an_interrupted_turn_says_so(self):
        self.assertEqual(self.paused("interrupted")[:1], ["Author paused; its current turn was interrupted."])

    def test_a_turn_that_ended_before_the_interrupt_pauses_at_its_boundary(self):
        self.assertEqual(self.paused("completed")[:1], ["Author paused at the current turn boundary."])


if __name__ == "__main__":
    unittest.main()
