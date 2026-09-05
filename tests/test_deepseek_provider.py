import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import workflow_runner as runner
from ui import review, server


class DeepSeekProviderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = os.getcwd()
        os.chdir(directory.name)
        self.addCleanup(os.chdir, previous)

    def test_astra_defaults_remain_global_with_optional_provider(self):
        self.assertEqual(runner.MODEL, "gpt-6-astra")
        self.assertEqual(server.DEFAULT_REVIEW_MODEL, "gpt-6-astra")
        self.assertEqual(runner.AUTHOR_MODEL, "gpt-6-astra")
        self.assertEqual(runner.CRITIC_MODEL, "gpt-6-astra")
        self.assertEqual(runner.WRITER_MODEL, "gpt-6-astra")
        self.assertEqual(runner.DEFAULT_SPEED, "fast")
        self.assertEqual(server.DEFAULT_REVIEW_EFFORT, "ultra")

        state = server.empty_state()
        for role in ("reviewModel", "authorModel", "criticModel", "writerModel"):
            self.assertEqual(state[role], "gpt-6-astra")
        self.assertEqual(state["speedMode"], "fast")
        self.assertEqual(state["reasoningSummary"], "concise")
        for role in (
            "reviewEffort", "authorEffort", "criticEffort", "writerEffort",
        ):
            self.assertEqual(state[role], "ultra")
        self.assertEqual(
            server.PUBLIC_GRAPH["settings"]["model"],
            "gpt-6-astra",
        )
        self.assertEqual(server.PUBLIC_GRAPH["settings"]["speed"], "fast")

    def test_optional_deepseek_uses_official_responses_provider(self):
        self.assertIn(runner.DEEPSEEK_MODEL, runner.MODELS)
        self.assertEqual(
            runner.MODELS,
            (
                "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                runner.DEEPSEEK_MODEL,
            ),
        )
        self.assertEqual(
            runner.model_provider(runner.DEEPSEEK_MODEL),
            runner.DEEPSEEK_PROVIDER,
        )
        arguments = runner.provider_arguments(runner.DEEPSEEK_MODEL)
        self.assertIn(
            f'model_catalog_json="{runner.DEEPSEEK_MODEL_CATALOG}"',
            arguments,
        )
        self.assertIn('model_provider="deepseek"', arguments)
        self.assertIn(
            'model_providers.deepseek.base_url="https://api.deepseek.com"',
            arguments,
        )
        self.assertIn(
            'model_providers.deepseek.env_key="TCS_PROVER_DEEPSEEK_TOKEN"',
            arguments,
        )
        self.assertIn('preferred_auth_method="apikey"', arguments)
        self.assertIn('forced_login_method="api"', arguments)
        self.assertNotIn("auth.command", " ".join(arguments))
        self.assertNotIn("env_http_headers", " ".join(arguments))
        self.assertNotIn("requires_openai_auth", " ".join(arguments))
        self.assertIn(
            'model_providers.deepseek.wire_api="responses"', arguments,
        )
        self.assertNotIn("test-secret", " ".join(arguments))

    def test_bundled_catalog_declares_only_official_deepseek(self):
        catalog = json.loads(
            runner.DEEPSEEK_MODEL_CATALOG.read_text(encoding="utf-8")
        )
        models = {entry["slug"]: entry for entry in catalog["models"]}
        self.assertEqual(set(models), {runner.DEEPSEEK_MODEL})
        for model in models.values():
            self.assertEqual(model["context_window"], 1048576)
            self.assertEqual(model["multi_agent_version"], "v2")
            self.assertEqual(model["apply_patch_tool_type"], "freeform")
            self.assertTrue(model["base_instructions"])
        self.assertEqual(
            [
                level["effort"]
                for level in models[runner.DEEPSEEK_MODEL][
                    "supported_reasoning_levels"
                ]
            ],
            ["low", "high", "max"],
        )

    def test_deepseek_key_becomes_child_only_provider_token(self):
        with mock.patch.dict(
            os.environ,
            {
                runner.DEEPSEEK_KEY_ENV: "  test-secret  ",
                runner.DEEPSEEK_TOKEN_ENV: "stale-value",
                "OPENROUTER_API_KEY": "obsolete-secret",
                "TCS_PROVER_OPENROUTER_TOKEN": "obsolete-token",
            },
            clear=True,
        ):
            child = runner.environment(runner.DEEPSEEK_MODEL)
            openai_child = runner.environment("gpt-5.6-sol")

            self.assertEqual(
                child[runner.DEEPSEEK_TOKEN_ENV], "test-secret",
            )
            self.assertEqual(
                child["OPENAI_API_KEY"],
                runner.CUSTOM_PROVIDER_LOGIN_PLACEHOLDER,
            )
            self.assertNotEqual(child["OPENAI_API_KEY"], "test-secret")
            self.assertEqual(
                os.environ[runner.DEEPSEEK_KEY_ENV], "  test-secret  ",
            )

        self.assertNotIn(runner.DEEPSEEK_KEY_ENV, child)
        self.assertNotIn(runner.DEEPSEEK_TOKEN_ENV, openai_child)
        self.assertNotIn("OPENAI_API_KEY", openai_child)
        self.assertNotIn("OPENROUTER_API_KEY", child)
        self.assertNotIn("TCS_PROVER_OPENROUTER_TOKEN", child)

    def test_web_ui_lists_only_the_supported_deepseek_model(self):
        index = (server.UI / "index.html").read_text(encoding="utf-8")
        self.assertEqual(index.count('value="deepseek-v4-pro"'), 4)
        self.assertEqual(
            index.count('value="gpt-6-astra" selected'), 4,
        )
        self.assertNotIn('value="deepseek-v4-pro" selected', index)
        self.assertIn('value="fast" selected', index)
        self.assertNotIn('value="standard" selected', index)
        self.assertNotIn('value="deepseek/', index)

        script = (server.UI / "app.js").read_text(encoding="utf-8")
        self.assertIn("selectedModels.includes(deepseekModel)", script)
        self.assertNotIn("deepseekModels", script)
        self.assertNotIn("effort.value =", script)

    def test_deepseek_key_accepts_and_removes_bearer_prefix(self):
        with mock.patch.dict(
            os.environ,
            {runner.DEEPSEEK_KEY_ENV: "  Bearer deepseek-secret  "},
            clear=True,
        ):
            self.assertEqual(runner.deepseek_key(), "deepseek-secret")
            child = runner.environment(runner.DEEPSEEK_MODEL)
        self.assertEqual(
            child[runner.DEEPSEEK_TOKEN_ENV], "deepseek-secret",
        )

    def test_official_deepseek_requires_its_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            runner.require_model_credentials("gpt-5.6-sol")
            with self.assertRaisesRegex(
                runner.Error, "Set DEEPSEEK_API_KEY",
            ):
                runner.require_model_credentials(runner.DEEPSEEK_MODEL)

        with mock.patch.dict(
            os.environ, {runner.DEEPSEEK_KEY_ENV: "test-secret"},
            clear=True,
        ):
            runner.require_model_credentials(runner.DEEPSEEK_MODEL)

    def test_deepseek_capabilities_normalize_effort_speed_and_summary(self):
        expected = {
            "low": "high", "medium": "high", "high": "high",
            "xhigh": "max", "max": "max", "ultra": "max",
        }
        for selected, effective in expected.items():
            with self.subTest(selected=selected):
                self.assertEqual(
                    runner.effective_effort(
                        runner.DEEPSEEK_MODEL, selected
                    ),
                    effective,
                )
        self.assertEqual(
            runner.effective_speed(runner.DEEPSEEK_MODEL, "fast"),
            "standard",
        )
        self.assertEqual(
            runner.speed_arguments("fast", runner.DEEPSEEK_MODEL),
            ["--disable", "fast_mode"],
        )
        self.assertEqual(
            runner.reasoning_summary(runner.DEEPSEEK_MODEL), "concise"
        )
        for level in runner.REASONING_SUMMARIES:
            self.assertEqual(
                runner.reasoning_summary(runner.DEEPSEEK_MODEL, level),
                level,
            )
        with self.assertRaisesRegex(runner.Error, "Choose Status only"):
            runner.reasoning_summary(runner.DEEPSEEK_MODEL, "private")

    def test_deepseek_prompt_carries_json_schema_contract(self):
        prompt = runner.structured_prompt_for_model(
            "Return the review.", review.REVIEW_SCHEMA, runner.DEEPSEEK_MODEL,
        )
        self.assertTrue(prompt.startswith("Return the review."))
        self.assertIn("OUTPUT JSON CONTRACT", prompt)
        self.assertIn('"statement"', prompt)
        self.assertIn('"additionalProperties": false', prompt)
        self.assertEqual(
            runner.structured_prompt_for_model(
                "unchanged", review.REVIEW_SCHEMA, "gpt-5.6-sol",
            ),
            "unchanged",
        )

    def test_deepseek_uses_prompted_json_with_local_schema_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            schema_path = Path(temporary) / "schema.json"
            self.assertEqual(
                runner.output_schema_arguments(
                    runner.DEEPSEEK_MODEL, schema_path,
                ),
                [],
            )
            self.assertEqual(
                runner.output_schema_arguments("gpt-5.6-sol", schema_path),
                ["--output-schema", str(schema_path)],
            )

        decoded = runner.decoded_json_object(
            '```json\n{"statement":"s","notes":"n"}\n```',
        )
        self.assertEqual(
            runner.validate_json_schema(decoded, review.REVIEW_SCHEMA), decoded,
        )
        with self.assertRaisesRegex(runner.Error, "unsupported properties"):
            runner.validate_json_schema(
                {"statement": "s", "notes": "n", "extra": True},
                review.REVIEW_SCHEMA,
            )
        with self.assertRaisesRegex(runner.Error, "malformed"):
            runner.decoded_json_object("not-json")
        with self.assertRaisesRegex(runner.Error, "without a final"):
            runner.decoded_json_object("  \n")
        self.assertEqual(
            runner.decoded_json_object(
                'I checked the result.\n{"statement":"s","notes":"n"}\nDone.'
            ),
            {"statement": "s", "notes": "n"},
        )

    def test_structured_stages_disable_tools_and_retry_an_empty_answer(self):
        self.assertEqual(
            runner.structured_tool_arguments("review"),
            [
                "-c", 'web_search="disabled"',
                "-c", "tools.web_search=false",
                "-c", "tools.view_image=false",
                "--disable", "shell_tool",
                "--disable", "multi_agent",
            ],
        )
        self.assertEqual(
            runner.structured_tool_arguments("critic"),
            runner.structured_tool_arguments("review"),
        )
        valid = '{"statement":"checked","notes":"complete"}'
        with mock.patch.dict(
            os.environ, {runner.DEEPSEEK_KEY_ENV: "test-secret"}, clear=True,
        ), mock.patch.object(
            runner, "run_structured_attempt", side_effect=["", valid],
        ) as run, mock.patch.object(runner, "emit"):
            result, raw = runner.structured(
                "Review this.", review.REVIEW_SCHEMA, "review",
                model=runner.DEEPSEEK_MODEL, effort="max",
            )

        self.assertEqual(result, {"statement": "checked", "notes": "complete"})
        self.assertEqual(raw, valid)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(
            all(call.args[6] == "concise" for call in run.call_args_list)
        )
        retry_prompt = run.call_args_list[1].args[0]
        self.assertIn("STRUCTURED OUTPUT RECOVERY RETRY", retry_prompt)
        self.assertIn("previous attempt produced no final message", retry_prompt)

    def test_structured_watchdog_is_deepseek_specific_by_default(self):
        valid = '{"statement":"checked","notes":"complete"}'
        with mock.patch.dict(
            os.environ, {runner.DEEPSEEK_KEY_ENV: "test-secret"}, clear=True,
        ), mock.patch.object(
            runner, "run_structured_attempt", return_value=valid,
        ) as run, mock.patch.object(runner, "emit"):
            runner.structured(
                "Review this.", review.REVIEW_SCHEMA, "review",
                model="gpt-5.6-sol", effort="ultra",
            )
            openai_timeout = run.call_args.kwargs["timeout"]
            runner.structured(
                "Review this.", review.REVIEW_SCHEMA, "review",
                model=runner.DEEPSEEK_MODEL, effort="max",
            )
            deepseek_timeout = run.call_args.kwargs["timeout"]

        self.assertIsNone(openai_timeout)
        self.assertEqual(
            deepseek_timeout,
            runner.STRUCTURED_ATTEMPT_TIMEOUT_SECONDS["review"],
        )

    def test_structured_attempt_timeout_stops_the_process(self):
        real_popen = subprocess.Popen
        processes = []

        def sleeping_process(*_args, **_kwargs):
            process = real_popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            processes.append(process)
            return process

        try:
            with mock.patch.object(
                runner.subprocess, "Popen", side_effect=sleeping_process,
            ), mock.patch.object(
                runner, "codex", return_value="codex",
            ), mock.patch.dict(
                os.environ, {runner.DEEPSEEK_KEY_ENV: "test-secret"}, clear=True,
            ), mock.patch.object(
                runner, "STRUCTURED_HEARTBEAT_SECONDS", 0.01,
            ), mock.patch.object(runner, "emit") as emit:
                with self.assertRaisesRegex(
                    runner.StructuredAttemptTimeout, "did not respond",
                ):
                    runner.run_structured_attempt(
                        "Review this.", review.REVIEW_SCHEMA, "review",
                        runner.DEEPSEEK_MODEL, "max", "standard", "none",
                        timeout=0.05,
                    )
                self.assertTrue(any(
                    call.args[:2] == ("status", "review")
                    and call.kwargs.get("heartbeat") is True
                    and call.kwargs.get("processAlive") is True
                    and call.kwargs.get("contentEventCount") == 0
                    for call in emit.call_args_list
                ))
        finally:
            for process in processes:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def test_structured_attempt_preserves_stderr_failure_detail(self):
        real_popen = subprocess.Popen

        def failing_process(*_args, **_kwargs):
            return real_popen(
                [
                    sys.executable, "-c",
                    "import sys; sys.stdin.read(); "
                    "print('provider quota exhausted', file=sys.stderr); "
                    "raise SystemExit(2)",
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )

        with mock.patch.object(
            runner.subprocess, "Popen", side_effect=failing_process,
        ), mock.patch.object(
            runner, "codex", return_value="codex",
        ), mock.patch.dict(
            os.environ, {runner.DEEPSEEK_KEY_ENV: "test-secret"}, clear=True,
        ):
            with self.assertRaisesRegex(
                runner.Error, "provider quota exhausted",
            ):
                runner.run_structured_attempt(
                    "Review this.", review.REVIEW_SCHEMA, "critic",
                    runner.DEEPSEEK_MODEL, "high", "standard", "none",
                    timeout=2,
                )

    def test_timed_out_deepseek_review_retries_at_lower_effort(self):
        valid = '{"statement":"checked","notes":"complete"}'
        timeout = runner.StructuredAttemptTimeout(
            "The review model did not respond within 300 seconds."
        )
        with mock.patch.dict(
            os.environ, {runner.DEEPSEEK_KEY_ENV: "test-secret"}, clear=True,
        ), mock.patch.object(
            runner, "run_structured_attempt", side_effect=[timeout, valid],
        ) as run, mock.patch.object(runner, "emit"):
            result, _ = runner.structured(
                "Review this.", review.REVIEW_SCHEMA, "review",
                model=runner.DEEPSEEK_MODEL, effort="max",
            )

        self.assertEqual(result["statement"], "checked")
        self.assertEqual(run.call_args_list[0].args[4], "max")
        self.assertEqual(run.call_args_list[1].args[4], "high")

    def test_controller_runs_three_explicit_parallel_critic_audits(self):
        def fake_structured(_prompt, schema, _stage, **options):
            index = int(options["request_label"].rsplit(" ", 1)[1])
            result = {
                "focus": f"focus {index}",
                "verdict": "pass",
                "report": f"audit {index} complete",
            }
            self.assertIs(schema, runner.CRITIC_CHECK_SCHEMA)
            return result, json.dumps(result)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner.Path, "cwd", return_value=Path(directory),
        ), mock.patch.object(
            runner, "structured", side_effect=fake_structured,
        ) as structured, mock.patch.object(runner, "emit"):
            reports = runner.independent_critic_audits(
                "statement", "candidate", runner.DEEPSEEK_MODEL, "max",
                runner.CRITIC_PROMPT, "standard", "concise",
            )
            restored = runner.independent_critic_audits(
                "statement", "candidate", runner.DEEPSEEK_MODEL, "max",
                runner.CRITIC_PROMPT, "standard", "concise",
            )
            replacement = runner.independent_critic_audits(
                "statement", "replacement candidate",
                runner.DEEPSEEK_MODEL, "max", runner.CRITIC_PROMPT,
                "standard", "concise",
            )
            candidate_contents = (
                Path(directory) / runner.SAVED_CANDIDATE_FILENAME
            ).read_text(encoding="utf-8")

        self.assertEqual(candidate_contents, "replacement candidate\n")
        self.assertEqual(
            [item["report"] for item in reports],
            ["audit 1 complete", "audit 2 complete", "audit 3 complete"],
        )
        self.assertEqual(structured.call_count, 6)
        self.assertEqual(restored, reports)
        self.assertEqual(replacement, reports)
        for call in structured.call_args_list:
            self.assertEqual(call.kwargs["effort"], "high")
            self.assertEqual(call.kwargs["attempts"], 1)
            self.assertEqual(
                call.kwargs["timeout"],
                runner.CRITIC_AUDIT_TIMEOUT_SECONDS,
            )

    def test_critic_checkpoint_only_reruns_missing_audits(self):
        saved = {
            "focus": runner.CRITIC_AUDIT_FOCI[1],
            "verdict": "fail",
            "report": "paid audit already complete",
        }

        def fake_structured(_prompt, _schema, _stage, **options):
            index = int(options["request_label"].rsplit(" ", 1)[1])
            return {
                "focus": "returned focus",
                "verdict": "pass",
                "report": f"new audit {index}",
            }, "{}"

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner.Path, "cwd", return_value=Path(directory),
        ), mock.patch.object(runner, "emit"), mock.patch.object(
            runner, "structured", side_effect=fake_structured,
        ) as structured:
            runner.save_critic_audit_checkpoint(
                [None, saved, None], "statement", "candidate",
                runner.DEEPSEEK_MODEL, "high", runner.CRITIC_PROMPT,
            )
            reports = runner.independent_critic_audits(
                "statement", "candidate", runner.DEEPSEEK_MODEL, "max",
                runner.CRITIC_PROMPT, "standard", "concise",
            )

        self.assertEqual(structured.call_count, 2)
        self.assertEqual(reports[1], saved)
        self.assertEqual(
            {call.kwargs["request_label"] for call in structured.call_args_list},
            {"Independent critic audit 1", "Independent critic audit 3"},
        )

    def test_critic_checkpoint_is_recovered_from_sibling_job(self):
        saved = {
            "focus": runner.CRITIC_AUDIT_FOCI[0],
            "verdict": "pass",
            "report": "prior paid audit",
        }
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory) / "runs"
            previous, current = runs / "previous-job", runs / "current-job"
            previous.mkdir(parents=True)
            current.mkdir()
            runner.save_critic_audit_checkpoint(
                [saved, None, None], "statement", "candidate",
                runner.DEEPSEEK_MODEL, "high", runner.CRITIC_PROMPT,
                directory=previous,
            )
            with mock.patch.object(runner, "emit") as emit:
                reports = runner.load_critic_audit_checkpoint(
                    "statement", "candidate", runner.DEEPSEEK_MODEL,
                    "high", runner.CRITIC_PROMPT, directory=current,
                )
            (
                current / runner.CRITIC_AUDIT_RECOVERY_DISABLED_FILENAME
            ).write_text("fresh audits\n", encoding="utf-8")
            with mock.patch.object(runner, "emit"):
                fresh_reports = runner.load_critic_audit_checkpoint(
                    "statement", "candidate", runner.DEEPSEEK_MODEL,
                    "high", runner.CRITIC_PROMPT, directory=current,
                )

        self.assertEqual(reports, [saved, None, None])
        self.assertEqual(fresh_reports, [None, None, None])
        self.assertTrue(any(
            call.kwargs.get("checkpointSource") == "previous-job"
            for call in emit.call_args_list
        ))

    def test_web_workflow_accepts_deepseek_and_records_effective_settings(self):
        with mock.patch.dict(
            os.environ, {runner.DEEPSEEK_KEY_ENV: "test-secret"},
            clear=False,
        ):
            options = server.App._workflow_options(
                include_review=False,
                author_model=runner.DEEPSEEK_MODEL,
                critic_model=runner.DEEPSEEK_MODEL,
                writer_model=runner.DEEPSEEK_MODEL,
                reasoning_effort="ultra",
                reasoning_summary="detailed",
            )

        self.assertEqual(options["authorModel"], runner.DEEPSEEK_MODEL)
        self.assertEqual(options["authorEffort"], "max")
        self.assertEqual(options["criticEffort"], "max")
        self.assertEqual(options["writerEffort"], "max")
        self.assertEqual(options["reasoningSummary"], "detailed")
        self.assertIn(runner.DEEPSEEK_MODEL, server.PUBLIC_GRAPH["settings"]["models"])

    def test_public_event_keeps_summaries_but_drops_private_reasoning(self):
        summary = {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"delta": "Checking the boundary case."},
        }
        self.assertEqual(runner.public_event(summary), summary)
        self.assertIsNone(runner.public_event({
            "method": "item/reasoning/textDelta",
            "params": {"delta": "private reasoning"},
        }))
        self.assertEqual(
            runner.public_event({
                "type": "reasoning", "summary": ["Public summary"],
                "content": ["private reasoning"],
            }),
            {"type": "reasoning", "summary": ["Public summary"]},
        )

    def test_live_author_steer_file_is_bounded_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "author-steer.json"
            path.write_text(json.dumps({
                "id": "command-1",
                "instruction": "Stop experiments and prove symbolically.",
            }), encoding="utf-8")
            self.assertEqual(
                runner.pending_author_steer(path),
                ("command-1", "Stop experiments and prove symbolically."),
            )
            self.assertIsNone(
                runner.pending_author_steer(path, "command-1")
            )
            path.write_text(json.dumps({
                "id": "command-2",
                "instruction": "x" * (runner.AUTHOR_STEER_MAX_CHARS + 1),
            }), encoding="utf-8")
            self.assertIsNone(runner.pending_author_steer(path))

    def test_saved_candidate_accepts_the_last_repair_at_the_round_limit(self):
        reports = [
            {"verdict": "pass", "fixed": True, "solution": f"REPAIR {number}", "bugs": ""}
            for number in (1, 2)
        ]
        with (
            mock.patch.object(runner, "criticize", side_effect=reports) as critic,
            mock.patch.object(runner, "finalize", return_value="FINAL LATEX") as final,
            mock.patch.object(runner, "run_goal") as author,
            mock.patch.object(runner, "emit"),
        ):
            result = runner.audit_candidate("STATEMENT", "SAVED PROOF", critic_rounds=2)

        self.assertEqual(result, "FINAL LATEX")
        self.assertEqual(critic.call_count, 2)
        self.assertEqual([call.args[:3] for call in critic.call_args_list], [
            ("STATEMENT", "SAVED PROOF", 1), ("STATEMENT", "REPAIR 1", 2),
        ])
        self.assertEqual(final.call_args.args[:2], ("STATEMENT", "REPAIR 2"))
        author.assert_not_called()

    def test_saved_candidate_starts_at_critic_and_then_finalizes(self):
        report = {
            "verdict": "pass", "fixed": False,
            "solution": "AUDITED PROOF", "bugs": "",
        }
        with mock.patch.object(
            runner, "criticize", return_value=report,
        ) as critic, mock.patch.object(
            runner, "finalize", return_value="FINAL LATEX",
        ) as final, mock.patch.object(runner, "emit"):
            result = runner.audit_candidate(
                "STATEMENT", "SAVED PROOF", critic_rounds=3,
                author_model="gpt-5.6-luna",
                critic_model="gpt-5.6-luna", writer_model="gpt-5.6-luna",
            )

        self.assertEqual(result, "FINAL LATEX")
        self.assertEqual(critic.call_args.args[:3], (
            "STATEMENT", "SAVED PROOF", 1,
        ))
        self.assertEqual(final.call_args.args[:2], (
            "STATEMENT", "AUDITED PROOF",
        ))

    def test_rejected_saved_candidate_enters_normal_author_repair_loop(self):
        report = {
            "verdict": "reject", "fixed": False,
            "solution": "SAFE PARTIAL FIX", "bugs": "missing lemma",
        }
        with mock.patch.object(
            runner, "criticize", return_value=report,
        ), mock.patch.object(runner, "finalize") as final, mock.patch.object(
            runner, "emit"
        ), mock.patch.object(
            runner, "run_goal", return_value="REPAIRED FINAL"
        ) as author_loop:
            result = runner.audit_candidate(
                "STATEMENT", "SAVED PROOF", critic_rounds=3,
                author_model="gpt-5.6-luna",
                critic_model="gpt-5.6-luna",
                writer_model="gpt-5.6-luna",
            )
        self.assertEqual(result, "REPAIRED FINAL")
        self.assertIn("SAFE PARTIAL FIX", author_loop.call_args.args[0])
        self.assertIn("missing lemma", author_loop.call_args.args[0])
        final.assert_not_called()


if __name__ == "__main__":
    unittest.main()
