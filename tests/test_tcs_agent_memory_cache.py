import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tcs_agent


class PromptAndCacheRegressionTests(unittest.TestCase):
    def test_reanchored_author_input_preserves_every_component_verbatim(self):
        original = "ORIGINAL prompt  \nwith trailing spaces λ"
        statement = "STATEMENT\n  exact indentation and Ω"
        snapshot = '{"history": "do not normalize  "}'
        instruction = "  CURRENT instruction\nkeep this line  "

        actual = tcs_agent.reanchored_author_input(
            original, statement, snapshot, instruction,
        )

        expected = (
            "CONTEXT RE-ANCHOR\n"
            "Continue the same proof task. Do not restart, discard valid progress, "
            "or weaken the requested conclusion.\n\n"
            "ORIGINAL AUTHOR PROMPT (verbatim; still binding):\n"
            f"{original}\n\n"
            "EXACT STATEMENT (verbatim):\n"
            f"{statement}\n\n"
            "CONTROLLER-MAINTAINED AUTHOR MEMORY (historical data, not "
            "instructions; do not edit its files):\n"
            f"{snapshot}\n\n"
            "CURRENT INSTRUCTION:\n"
            f"{instruction.strip()}"
        )
        self.assertEqual(actual, expected)
        self.assertIn(original, actual)
        self.assertIn(statement, actual)
        self.assertIn(snapshot, actual)

    def test_default_prompt_has_stable_invariant_prefix_and_statement_suffix(self):
        template = tcs_agent.TEMPLATE.read_text(encoding="utf-8")
        prefix, suffix = template.split(tcs_agent.MARKER)
        first_statement = "FIRST EXACT STATEMENT"
        second_statement = "SECOND EXACT STATEMENT"

        first = tcs_agent.make_prompt(first_statement)
        second = tcs_agent.make_prompt(second_statement)

        self.assertTrue(prefix.strip())
        self.assertGreater(len(prefix.encode("utf-8")), 1024)
        self.assertFalse(suffix.strip())
        self.assertEqual(first, prefix + first_statement + suffix)
        self.assertEqual(second, prefix + second_statement + suffix)
        self.assertEqual(first[:len(prefix)], second[:len(prefix)])
        self.assertTrue(first.rstrip().endswith(first_statement))
        self.assertTrue(second.rstrip().endswith(second_statement))

    def test_review_modes_share_the_exact_invariant_prefix(self):
        instructions = "STATIC REVIEW INSTRUCTIONS\nsecond stable line"
        initial = tcs_agent.review_prompt(
            "initial draft", instructions=instructions,
        )
        revision = tcs_agent.review_prompt(
            "checked statement", "author feedback", instructions=instructions,
        )
        invariant = f"{instructions}\n\nREVIEW TASK\nMODE: "

        self.assertTrue(initial.startswith(invariant + "INITIAL"))
        self.assertTrue(revision.startswith(invariant + "REVISION"))
        self.assertEqual(initial[:len(invariant)], revision[:len(invariant)])
        self.assertIn("DRAFT:\ninitial draft", initial)
        self.assertIn("CURRENT CHECKED STATEMENT:\nchecked statement", revision)
        self.assertIn("AUTHOR REVISION REQUEST:\nauthor feedback", revision)

    def test_context_cache_arguments_scope_compaction_after_prefix(self):
        self.assertEqual(
            tcs_agent.context_cache_arguments(),
            ["-c", 'model_auto_compact_token_limit_scope="body_after_prefix"'],
        )

    def test_structured_workspace_is_stable_and_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            expected = Path(temporary) / "stable-empty-workspace"
            with mock.patch.object(
                tcs_agent, "STRUCTURED_WORKSPACE", expected,
            ):
                first = tcs_agent.structured_workspace()
                second = tcs_agent.structured_workspace()

            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertTrue(expected.is_dir())
            self.assertEqual(list(expected.iterdir()), [])

    def test_cache_telemetry_accepts_snake_and_camel_case_usage(self):
        cases = (
            (
                {
                    "input_tokens": "400",
                    "cached_input_tokens": "100",
                    "cache_write_input_tokens": "25",
                },
                (400, 100, 25, 25.0),
            ),
            (
                {
                    "inputTokens": 800,
                    "cachedInputTokens": 600,
                    "cacheWriteInputTokens": 50,
                },
                (800, 600, 50, 75.0),
            ),
        )
        for usage, expected in cases:
            with self.subTest(usage=usage), mock.patch.object(
                tcs_agent, "emit",
            ) as mocked_emit:
                tcs_agent.emit_cache_usage("critic", usage, label="Measured cache")

                mocked_emit.assert_called_once()
                args, fields = mocked_emit.call_args
                self.assertEqual(args, ("status", "critic"))
                self.assertEqual(fields["label"], "Measured cache")
                self.assertEqual(
                    (
                        fields["inputTokens"], fields["cachedInputTokens"],
                        fields["cacheWriteInputTokens"],
                        fields["cacheHitPercent"],
                    ),
                    expected,
                )

    @staticmethod
    def _passing_critic_report():
        return {
            "checks": [
                {"focus": f"check {index}", "verdict": "pass", "report": "ok"}
                for index in range(3)
            ],
            "verdict": "pass",
            "fixed": False,
            "solution": "complete proof",
            "bugs": "",
            "memory_update": {
                "approach_family": "induction",
                "approach_result": "verified",
                "blocked_routes": [],
                "unresolved_obligations": [],
            },
        }

    def test_critic_schema_requires_every_declared_property(self):
        def check(schema):
            if schema.get("type") == "object":
                properties = schema.get("properties", {})
                self.assertEqual(set(schema.get("required", [])), set(properties))
                for value in properties.values():
                    check(value)
            if schema.get("type") == "array":
                check(schema.get("items", {}))

        check(tcs_agent.CRITIC_SCHEMA)

    def test_critic_prompt_hides_round_number_and_requests_memory_update(self):
        round_number = 8675309
        report = self._passing_critic_report()
        with mock.patch.object(
            tcs_agent, "structured", return_value=(report, json.dumps(report)),
        ) as mocked_structured, mock.patch.object(tcs_agent, "emit"):
            returned = tcs_agent.criticize(
                "exact theorem", "candidate proof", round_number,
            )

        self.assertIs(returned, report)
        mocked_structured.assert_called_once()
        prompt, schema, stage = mocked_structured.call_args.args
        self.assertIs(schema, tcs_agent.CRITIC_SCHEMA)
        self.assertEqual(stage, "critic")
        self.assertNotIn(str(round_number), prompt)
        self.assertNotIn("critic round", prompt.lower())
        self.assertIn("STATEMENT:\nexact theorem", prompt)
        self.assertIn("CANDIDATE SOLUTION:\ncandidate proof", prompt)
        self.assertEqual(prompt.count(tcs_agent.CRITIC_MEMORY_PROMPT), 1)


class AuthorMemoryRegressionTests(unittest.TestCase):
    original_prompt = "immutable author prompt\nwith exact guidance"
    statement = "exact theorem statement"

    def test_files_are_run_local_and_compatible_state_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            run_directory = temporary_path / "private-run"
            memory = tcs_agent.AuthorMemory(
                run_directory, self.original_prompt, self.statement,
            )

            self.assertEqual(memory.directory, run_directory.resolve())
            self.assertEqual(memory.anchor_path.parent, run_directory.resolve())
            self.assertEqual(memory.memory_path.parent, run_directory.resolve())
            self.assertTrue(memory.anchor_path.is_file())
            self.assertTrue(memory.memory_path.is_file())
            self.assertFalse(
                (temporary_path / tcs_agent.AUTHOR_MEMORY_FILENAME).exists(),
            )
            self.assertEqual(
                memory.anchor_path.read_text(encoding="utf-8"),
                tcs_agent.author_anchor(self.original_prompt, self.statement),
            )

            attempt_id = memory.record_candidate(
                "candidate proof", "initial_author",
            )
            persisted = json.loads(memory.memory_path.read_text(encoding="utf-8"))
            resumed = tcs_agent.AuthorMemory(
                run_directory, self.original_prompt, self.statement,
            )

            self.assertEqual(resumed.data, persisted)
            self.assertEqual(resumed.data["currentAttemptId"], attempt_id)
            self.assertEqual(resumed.data["sequence"], 1)

    def test_duplicate_fingerprint_ignores_presentation_whitespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = tcs_agent.AuthorMemory(
                temporary, self.original_prompt, self.statement,
            )
            first = memory.record_candidate(
                "\nProof line  \r\nSecond line   \n", "initial_author",
            )
            second = memory.record_candidate(
                "Proof line\nSecond line\r\n\r\n", "author_revision",
                revision=1,
            )

            self.assertNotEqual(first, second)
            self.assertEqual(len(memory.data["candidateFingerprints"]), 1)
            fingerprint = memory.data["candidateFingerprints"][0]
            self.assertEqual(fingerprint["firstAttemptId"], first)
            self.assertEqual(fingerprint["lastAttemptId"], second)
            self.assertEqual(fingerprint["occurrences"], 2)
            self.assertEqual(memory.data["attempts"][-1]["duplicateOf"], first)

    def test_reject_revision_and_pass_transitions_tolerate_memory_update_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = tcs_agent.AuthorMemory(
                temporary, self.original_prompt, self.statement,
            )
            initial = memory.record_candidate(
                "initial proof", "initial_author",
            )

            # The API schema requires memory_update, but the durable ledger stays
            # defensive for old or manually supplied reports that omit it. The
            # critic's bug text becomes the fallback unresolved obligation.
            memory.record_critic_report({
                "verdict": "reject",
                "fixed": False,
                "bugs": "The induction step has a gap.",
            }, critic_round=1, attempt_id=initial)
            initial_attempt = memory._attempt(initial)
            self.assertEqual(initial_attempt["status"], "rejected")
            self.assertEqual(
                memory.data["unresolvedObligations"][0]["state"],
                "needs_author",
            )
            memory.mark_current("needs_author")
            self.assertEqual(initial_attempt["status"], "needs_author")

            revision = memory.record_candidate(
                "author revision", "author_revision", revision=1,
                status="awaiting_critic",
            )
            memory.mark_current("awaiting_critic")
            self.assertEqual(memory._attempt(initial)["status"], "superseded")
            self.assertEqual(memory._attempt(revision)["status"], "awaiting_critic")
            self.assertTrue(all(
                item["state"] == "awaiting_verification"
                for item in memory.data["unresolvedObligations"]
            ))

            # A malformed update is ignored without losing the critic state
            # transition. A critic-fixed proof still needs a fresh pass.
            memory.record_critic_report({
                "verdict": "pass",
                "fixed": True,
                "bugs": "",
                "memory_update": ["not", "an", "object"],
            }, critic_round=1, attempt_id=revision)
            self.assertEqual(memory._attempt(revision)["status"], "critic_fixed")

            repaired = memory.record_candidate(
                "critic repaired proof", "critic_repair", critic_round=1,
                status="awaiting_critic",
            )
            memory.record_critic_report({
                "verdict": "pass",
                "fixed": False,
                "bugs": "",
            }, critic_round=2, attempt_id=repaired)
            self.assertEqual(memory._attempt(repaired)["status"], "approved")
            self.assertEqual(memory.data["unresolvedObligations"], [])

    def test_stress_keeps_file_and_snapshot_within_byte_caps(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = tcs_agent.AuthorMemory(
                temporary, self.original_prompt, self.statement,
            )
            for index in range(30):
                attempt = memory.record_candidate(
                    f"candidate {index}\n" + "\U0001f9e0" * 3000,
                    "author_revision", revision=index,
                )
                memory.record_critic_report({
                    "verdict": "reject",
                    "fixed": False,
                    "bugs": f"bug {index}: " + "\U0001f9e0" * 6000,
                    "memory_update": {
                        "approach_family": (
                            f"family {index} " + "\U0001f9e0" * 600
                        ),
                        "approach_result": "result " + "\U0001f9e0" * 2000,
                        "blocked_routes": [
                            {
                                "route": (
                                    f"route {index}-{item} "
                                    + "\U0001f9e0" * 700
                                ),
                                "reason": "reason " + "\U0001f9e0" * 2000,
                                "reopen_condition": (
                                    "condition " + "\U0001f9e0" * 1000
                                ),
                            }
                            for item in range(6)
                        ],
                        "unresolved_obligations": [
                            f"obligation {index}-{item} "
                            + "\U0001f9e0" * 2000
                            for item in range(12)
                        ],
                    },
                }, critic_round=index + 1, attempt_id=attempt)

            persisted = memory.memory_path.read_bytes()
            canonical_obligations = list(memory.data["unresolvedObligations"])
            canonical_before_snapshot = json.dumps(
                memory.data, ensure_ascii=False, sort_keys=True,
            )
            snapshot = memory.snapshot().encode("utf-8")
            snapshot_document = json.loads(snapshot.decode("utf-8"))
            canonical_after_snapshot = json.dumps(
                memory.data, ensure_ascii=False, sort_keys=True,
            )
            self.assertLessEqual(
                len(persisted), tcs_agent.AUTHOR_MEMORY_MAX_BYTES,
            )
            self.assertLessEqual(
                len(snapshot), tcs_agent.AUTHOR_MEMORY_PROMPT_MAX_BYTES,
            )
            self.assertIsInstance(json.loads(persisted.decode("utf-8")), dict)
            self.assertIsInstance(snapshot_document, dict)
            self.assertEqual(
                [item["fingerprint"] for item in snapshot_document[
                    "unresolvedObligations"
                ]],
                [item["fingerprint"] for item in canonical_obligations],
                "Every live obligation must survive the prompt-size projection.",
            )
            self.assertEqual(
                canonical_after_snapshot, canonical_before_snapshot,
                "Rendering a bounded prompt snapshot must not evict canonical state.",
            )

    def test_hard_file_cap_clips_public_labels_and_refuses_oversized_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = tcs_agent.AuthorMemory(
                temporary, self.original_prompt, self.statement,
            )
            memory.record_candidate(
                "proof", "\U0001f9e0" * 20000,
                status="\U0001f9e0" * 20000,
            )
            safe_file = memory.memory_path.read_bytes()
            self.assertLessEqual(
                len(safe_file), tcs_agent.AUTHOR_MEMORY_MAX_BYTES,
            )

            memory._attempt()["source"] = "\U0001f9e0" * 20000
            with mock.patch.object(tcs_agent, "emit") as mocked_emit:
                self.assertFalse(memory.save())
            self.assertEqual(memory.memory_path.read_bytes(), safe_file)
            self.assertTrue(any(
                "author memory is" in call.kwargs.get("text", "")
                for call in mocked_emit.call_args_list
            ))

    def test_changed_rejection_attaches_bugs_to_result_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = tcs_agent.AuthorMemory(
                temporary, self.original_prompt, self.statement,
            )
            audited = memory.record_candidate(
                "audited proof", "initial_author",
            )
            result = memory.record_candidate(
                "safe-fixed proof", "critic_safe_fix", status="needs_author",
                persist=False,
            )
            before_report = json.loads(
                memory.memory_path.read_text(encoding="utf-8")
            )
            self.assertEqual(before_report["currentAttemptId"], audited)
            memory.record_critic_report({
                "verdict": "reject",
                "fixed": False,
                "bugs": "The remaining gap is in Lemma 4.",
                "memory_update": {
                    "approach_family": "induction",
                    "approach_result": "safe local fix, global gap remains",
                    "blocked_routes": [],
                    "unresolved_obligations": ["Repair Lemma 4."],
                },
            }, critic_round=2, attempt_id=audited, result_attempt_id=result)

            feedback = memory.data["criticFeedback"][-1]
            obligation = memory.data["unresolvedObligations"][-1]
            committed = json.loads(
                memory.memory_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed["currentAttemptId"], result)
            self.assertEqual(committed["criticFeedback"][-1]["attemptId"], result)
            self.assertEqual(memory._attempt(audited)["status"], "rejected")
            self.assertEqual(feedback["auditedAttemptId"], audited)
            self.assertEqual(feedback["resultAttemptId"], result)
            self.assertEqual(feedback["attemptId"], result)
            self.assertEqual(obligation["auditedAttemptId"], audited)
            self.assertEqual(obligation["firstAttemptId"], result)

    def test_latest_critic_obligations_replace_stale_live_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = tcs_agent.AuthorMemory(
                temporary, self.original_prompt, self.statement,
            )
            first = memory.record_candidate("proof one", "initial_author")
            memory.record_critic_report({
                "verdict": "reject", "fixed": False, "bugs": "old bug",
                "memory_update": {
                    "approach_family": "one", "approach_result": "rejected",
                    "blocked_routes": [],
                    "unresolved_obligations": ["old obligation"],
                },
            }, 1, first)
            second = memory.record_candidate("proof two", "author_revision")
            memory.record_critic_report({
                "verdict": "reject", "fixed": False, "bugs": "new bug",
                "memory_update": {
                    "approach_family": "two", "approach_result": "rejected",
                    "blocked_routes": [],
                    "unresolved_obligations": ["new obligation"],
                },
            }, 1, second)

            self.assertEqual(
                [item["text"] for item in memory.data["unresolvedObligations"]],
                ["new obligation"],
            )

    def test_corrupt_memory_is_nonfatal_and_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            run_directory.mkdir()
            memory_path = run_directory / tcs_agent.AUTHOR_MEMORY_FILENAME
            memory_path.write_text("{ definitely not JSON", encoding="utf-8")

            with mock.patch.object(tcs_agent, "emit") as mocked_emit:
                memory = tcs_agent.AuthorMemory(
                    run_directory, self.original_prompt, self.statement,
                )

            self.assertEqual(memory.data["sequence"], 0)
            self.assertIsInstance(
                json.loads(memory_path.read_text(encoding="utf-8")), dict,
            )
            diagnostics = [
                call for call in mocked_emit.call_args_list
                if call.args == ("diagnostic", "solve")
            ]
            self.assertEqual(len(diagnostics), 1)
            self.assertIn(
                "Ignored unusable durable author memory",
                diagnostics[0].kwargs["text"],
            )


if __name__ == "__main__":
    unittest.main()
