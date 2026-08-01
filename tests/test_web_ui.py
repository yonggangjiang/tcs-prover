"""Small offline tests for the local browser wrapper."""

import http.client
import io
import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import web_ui

REAL_GRANT_WINDOWS_ACCESS = web_ui.grant_windows_access


class AppTests(unittest.TestCase):
    """Check that approval uses the server-stored review."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.trace = Path(self.folder.name) / "transcript.jsonl"
        self.access_patcher = mock.patch.object(web_ui, "grant_windows_access")
        self.access = self.access_patcher.start()
        self.addCleanup(self.access_patcher.stop)

    def tearDown(self):
        self.folder.cleanup()

    def app(self):
        return web_ui.App(self.trace)

    def test_algorithmic_catalogs_have_only_names_and_descriptions(self):
        for filename, presets in (
            ("model.json", web_ui.MODEL_PRESETS),
            ("problem.json", web_ui.PROBLEM_PRESETS),
        ):
            raw = json.loads(
                (web_ui.ALGORITHMIC / filename).read_text(encoding="utf-8")
            )
            self.assertEqual(raw, presets)
            self.assertTrue(presets)
            self.assertEqual(len({entry["name"] for entry in presets}), len(presets))
            for entry in presets:
                self.assertEqual(set(entry), {"name", "description"})
                self.assertTrue(entry["name"].strip())
                self.assertTrue(entry["description"].strip())

    def test_algorithmic_catalog_loader_rejects_extra_metadata(self):
        path = Path(self.folder.name) / "bad.json"
        path.write_text(
            json.dumps([{"name": "RAM", "description": "Words.", "id": 1}]),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "only name and description"):
            web_ui.load_algorithmic_catalog(path)
        path.write_text(
            json.dumps([
                {"name": "RAM", "description": "Words."},
                {"name": "ram", "description": "Duplicate."},
            ]),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicated"):
            web_ui.load_algorithmic_catalog(path)

    def test_algorithmic_statement_is_canonical_and_trimmed(self):
        self.assertEqual(
            web_ui.algorithmic_statement(
                "  word-RAM  ", "  Sort n integers.  ", "  Prove O(n log n).  "
            ),
            "MODEL OF COMPUTATION:\nword-RAM\n\n"
            "PROBLEM DESCRIPTION:\nSort n integers.\n\n"
            "GOAL (ASYMPTOTIC UPPER OR LOWER BOUND):\nProve O(n log n).",
        )

    def test_algorithmic_statement_requires_every_field(self):
        cases = (
            ("", "Problem", "Goal", "model of computation"),
            ("Model", "  ", "Goal", "problem description"),
            ("Model", "Problem", None, "upper- or lower-bound goal"),
        )
        for model, problem, goal, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                web_ui.algorithmic_statement(model, problem, goal)
        with self.assertRaisesRegex(ValueError, "NUL"):
            web_ui.algorithmic_statement("word\0RAM", "Problem", "Goal")

    def test_markdown_inputs_accept_a_file_or_sorted_top_level_folder(self):
        folder = Path(self.folder.name)
        alpha = folder / "Alpha.MD"
        zeta = folder / "zeta.md"
        alpha.write_text("Alpha theorem.", encoding="utf-8")
        zeta.write_text("Zeta theorem.", encoding="utf-8")
        (folder / "notes.txt").write_text("Ignore me.", encoding="utf-8")
        nested = folder / "nested"
        nested.mkdir()
        (nested / "nested.md").write_text("Not recursive.", encoding="utf-8")

        self.assertEqual(web_ui.markdown_inputs(alpha), [alpha.resolve()])
        self.assertEqual(
            web_ui.markdown_inputs(folder), [alpha.resolve(), zeta.resolve()]
        )

    def test_markdown_inputs_reject_missing_non_markdown_and_empty_paths(self):
        folder = Path(self.folder.name)
        text = folder / "statement.txt"
        text.write_text("Theorem.", encoding="utf-8")
        empty = folder / "empty"
        empty.mkdir()

        with self.assertRaisesRegex(ValueError, "must end in .md"):
            web_ui.markdown_inputs(text)
        with self.assertRaisesRegex(ValueError, "contains no .md"):
            web_ui.markdown_inputs(empty)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            web_ui.markdown_inputs(folder / "missing.md")

    def test_utf8_statement_preserves_paragraphs_quotes_and_backslashes(self):
        source = Path(self.folder.name) / "statement.md"
        statement = 'First paragraph with "quotes".\n\n\\alpha + \\beta.\n'
        source.write_text(statement, encoding="utf-8")

        self.assertEqual(web_ui.read_utf8(source, "statement"), statement)

        source.write_text(" \n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "statement is empty"):
            web_ui.read_utf8(source, "statement")
        source.write_text("Theorem\0invalid", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "NUL"):
            web_ui.read_utf8(source, "statement")
        source.write_bytes(b"Theorem \x9d")
        with self.assertRaisesRegex(ValueError, "Cannot read statement"):
            web_ui.read_utf8(source, "statement")

    def test_direct_cli_options_use_defaults_and_load_prompt_files(self):
        author = Path(self.folder.name) / "author.txt"
        author.write_text("Prove this:\n[STATEMENT]", encoding="utf-8")

        values = web_ui.direct_cli_options(
            critic_rounds=7,
            thinking_hours=36,
            author_model="gpt-5.6-terra",
            critic_model="gpt-5.6-luna",
            reasoning_effort="high",
            critic_effort="max",
            speed_mode="standard",
            author_prompt_file=author,
        )

        self.assertEqual(values["critic_rounds"], 7)
        self.assertEqual(values["thinking_hours"], 36)
        self.assertEqual(values["author_model"], "gpt-5.6-terra")
        self.assertEqual(values["critic_model"], "gpt-5.6-luna")
        self.assertEqual(values["author_effort"], "high")
        self.assertEqual(values["critic_effort"], "max")
        self.assertEqual(values["writer_effort"], "high")
        self.assertEqual(values["speed_mode"], "standard")
        self.assertEqual(values["author_prompt"], "Prove this:\n[STATEMENT]")

        defaults = web_ui.direct_cli_options()
        self.assertEqual(defaults["speed_mode"], "fast")
        self.assertEqual(defaults["critic_rounds"], 4)
        self.assertEqual(defaults["thinking_hours"], 24)

    def test_algorithmic_mode_starts_the_author_without_review(self):
        app = self.app()
        process = SimpleNamespace(stdin=mock.Mock(), stdout=mock.Mock())
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(web_ui.threading, "Thread") as thread, \
                mock.patch.object(app, "_review") as review:
            app.start_algorithmic(
                "  word-RAM  ", "  Sort n integers.  ", "  Prove O(n log n).  ",
                critic_rounds=6, thinking_hours=1.5,
                author_model="gpt-5.6-terra",
                critic_model="gpt-5.6-luna",
                writer_model="gpt-5.6-sol",
                author_effort="high", critic_effort="max",
                writer_effort="medium",
                speed_mode="standard",
            )
        statement = web_ui.algorithmic_statement(
            "word-RAM", "Sort n integers.", "Prove O(n log n)."
        )
        review.assert_not_called()
        process.stdin.write.assert_called_once_with(f"{statement}\0{6}\0{1.5}")
        process.stdin.close.assert_called_once()
        self.assertIs(thread.call_args.kwargs["target"].__self__, app)
        self.assertEqual(thread.call_args.kwargs["target"].__func__, app._read_output.__func__)
        self.assertIs(thread.call_args.kwargs["args"][0], process)
        self.assertIs(thread.call_args.kwargs["args"][1], app.active_token)
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()
        state = app.snapshot()
        self.assertEqual(state["phase"], "running")
        self.assertEqual(state["problemMode"], "algorithmic")
        self.assertEqual(state["activeNode"], "author")
        self.assertEqual(state["stage"], "solve")
        self.assertIsNone(state["review"])
        self.assertEqual(state["modelOfComputation"], "word-RAM")
        self.assertEqual(state["problemDescription"], "Sort n integers.")
        self.assertEqual(state["goal"], "Prove O(n log n).")
        self.assertNotIn("statement_reviewer", state["workflow"]["nodes"])
        self.assertEqual(state["authorModel"], "gpt-5.6-terra")
        self.assertEqual(state["criticEffort"], "max")
        self.assertEqual(state["speedMode"], "standard")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--speed") + 1], "standard")
        self.assertEqual(
            (app.run_dir / "algorithmic-problem.md").read_text(encoding="utf-8"),
            statement + "\n",
        )
        self.assertEqual(
            json.loads((app.run_dir / "algorithmic-input.json").read_text(
                encoding="utf-8"
            )),
            {
                "modelOfComputation": "word-RAM",
                "problemDescription": "Sort n integers.",
                "goal": "Prove O(n log n).",
            },
        )
        self.assertFalse((app.run_dir / "checked-statement.md").exists())
        self.assertEqual(popen.call_args.kwargs["cwd"], app.run_dir)

    def test_statement_can_start_the_author_without_review(self):
        app = self.app()
        process = SimpleNamespace(stdin=mock.Mock(), stdout=mock.Mock())
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(web_ui.threading, "Thread") as thread, \
                mock.patch.object(app, "_review") as review:
            app.start_direct_statement(
                "  Exact theorem statement.  ", critic_rounds=3,
                thinking_hours=2, author_effort="high",
            )
        review.assert_not_called()
        process.stdin.write.assert_called_once_with(
            "Exact theorem statement.\0" + "3\0" + "2.0"
        )
        state = app.snapshot()
        self.assertEqual(state["phase"], "running")
        self.assertEqual(state["problemMode"], "statement")
        self.assertTrue(state["skipStatementReview"])
        self.assertEqual(state["activeNode"], "author")
        self.assertNotIn("statement_reviewer", state["workflow"]["nodes"])
        self.assertEqual(state["authorEffort"], "high")
        self.assertIn(
            "Skipped by the user.",
            (app.run_dir / "checked-statement.md").read_text(encoding="utf-8"),
        )
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--author-effort") + 1], "high")
        self.assertIs(thread.call_args.kwargs["args"][1], app.active_token)

    def test_latex_mode_runs_only_the_final_editor(self):
        app = self.app()
        process = SimpleNamespace(stdin=mock.Mock(), stdout=mock.Mock())
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(web_ui.threading, "Thread") as thread, \
                mock.patch.object(app, "_review") as review, \
                mock.patch.object(app, "_launch_solver_locked") as solver:
            app.start_latex_only(
                "  Theorem statement.\n\nExisting proof.  ",
                writer_model="gpt-5.6-terra", writer_effort="high",
                speed_mode="standard",
            )
        review.assert_not_called()
        solver.assert_not_called()
        process.stdin.write.assert_called_once_with(
            "Theorem statement.\n\nExisting proof."
        )
        command = popen.call_args.args[0]
        self.assertIn("finalize", command)
        self.assertNotIn("solve", command)
        self.assertEqual(
            command[command.index("--writer-model") + 1], "gpt-5.6-terra"
        )
        state = app.snapshot()
        self.assertEqual(state["problemMode"], "latex")
        self.assertEqual(state["activeNode"], "latex_editor")
        self.assertEqual(set(state["workflow"]["nodes"]), {"latex_editor"})
        self.assertEqual(
            state["latexInput"], "Theorem statement.\n\nExisting proof."
        )
        self.assertEqual(
            (app.run_dir / "latex-input.md").read_text(encoding="utf-8"),
            "Theorem statement.\n\nExisting proof.\n",
        )
        self.assertIs(thread.call_args.kwargs["args"][1], app.active_token)

    def test_review_starts_in_the_background(self):
        app = self.app()
        with mock.patch.object(web_ui.threading, "Thread") as thread:
            app.start_review("draft", "clarify it", 7)
        self.assertEqual(app.snapshot()["phase"], "reviewing")
        self.assertEqual(app.snapshot()["criticRounds"], 7)
        self.assertEqual(app.snapshot()["thinkingHours"], 24)
        self.assertEqual(app.snapshot()["speedMode"], web_ui.DEFAULT_SPEED)
        self.assertEqual(app.snapshot()["authorModel"], "gpt-5.6-sol")
        self.assertEqual(app.snapshot()["criticModel"], "gpt-5.6-sol")
        self.assertEqual(app.snapshot()["writerModel"], "gpt-5.6-sol")
        args = thread.call_args.kwargs["args"]
        self.assertEqual(
            args[:4],
            (
                "draft", "clarify it", web_ui.DEFAULT_REVIEW_MODEL,
                web_ui.DEFAULT_REASONING_EFFORT,
            ),
        )
        self.assertIs(args[4], app.active_token)
        thread.return_value.start.assert_called_once()

    def test_each_role_effort_and_prompt_is_saved_for_the_job(self):
        app = self.app()
        prompts = {
            "review_prompt": "Rewrite this custom review.",
            "author_prompt": "Solve [STATEMENT] carefully.",
            "critic_prompt": "Audit this custom proof.",
            "final_prompt": "Write this custom LaTeX.",
        }
        with mock.patch.object(web_ui.threading, "Thread"):
            app.start_review(
                "draft", review_effort="low", author_effort="medium",
                critic_effort="high", writer_effort="max", **prompts,
            )
        state = app.snapshot()
        self.assertEqual(
            [
                state["reviewEffort"], state["authorEffort"],
                state["criticEffort"], state["writerEffort"],
            ],
            ["low", "medium", "high", "max"],
        )
        for name in ("review", "author", "critic", "final"):
            self.assertEqual(
                (app.run_dir / "prompts" / f"{name}.txt").read_text().strip(),
                prompts[f"{name}_prompt"],
            )

    def test_real_app_creates_one_private_folder_per_problem(self):
        runs = Path(self.folder.name) / "runs"
        app = web_ui.App(runs=runs)
        with mock.patch.object(web_ui.threading, "Thread"):
            app.start_review("Shortest paths with negative weights.")
        self.assertEqual(app.run_dir.parent, runs)
        self.assertEqual(app.trace_file, app.run_dir / "transcript.jsonl")
        self.assertIn("Shortest paths", (app.run_dir / "draft.md").read_text())
        if web_ui.os.name != "nt":
            self.assertEqual(stat.S_IMODE(app.run_dir.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((app.run_dir / "draft.md").stat().st_mode), 0o600
            )

    def test_windows_runs_parent_allows_traversal_without_inheriting_access(self):
        runs = Path(self.folder.name) / "runs"
        with mock.patch.object(web_ui.os, "name", "nt"), mock.patch.object(
            web_ui, "current_windows_account", return_value="DOMAIN\\user"
        ):
            web_ui.prepare_runs_directory(runs)
        self.assertTrue(runs.is_dir())
        self.assertEqual(
            self.access.call_args_list,
            [
                mock.call(runs, "DOMAIN\\user", "(OI)(CI)(F)"),
                mock.call(runs, web_ui.WINDOWS_EVERYONE_SID, "(RX)"),
            ],
        )
        parent_permission = self.access.call_args_list[-1].args[-1]
        self.assertNotIn("(OI)", parent_permission)
        self.assertNotIn("(CI)", parent_permission)

    def test_windows_acl_command_is_explicit_and_noninteractive(self):
        completed = SimpleNamespace(returncode=0, stderr="")
        path = Path("runs")
        with mock.patch.object(
            web_ui.subprocess, "run", return_value=completed
        ) as run:
            REAL_GRANT_WINDOWS_ACCESS(path, "DOMAIN\\user", "(RX)")
        self.assertEqual(
            run.call_args.args[0],
            ["icacls", str(path), "/grant:r", "DOMAIN\\user:(RX)"],
        )
        self.assertFalse(run.call_args.kwargs["check"])

    def test_feedback_retry_stays_in_the_same_problem_folder(self):
        runs = Path(self.folder.name) / "runs"
        app = web_ui.App(runs=runs)
        with mock.patch.object(web_ui.threading, "Thread"):
            app.start_review("Original draft.")
            first = app.run_dir
            app.state["phase"] = "reviewed"
            app.start_review("Edited statement.", "Keep the bound.")
        self.assertEqual(app.run_dir, first)
        self.assertIn("Original draft.", (first / "draft.md").read_text())

    def test_review_receives_feedback_separately(self):
        app = self.app()
        app.state["phase"] = "reviewing"
        report = {"statement": "Rigorous", "notes": "Sound."}
        record = {"kind": "review_result", "review": report}
        process = SimpleNamespace(
            stdin=mock.Mock(),
            stdout=io.StringIO(json.dumps(record) + "\n"),
            wait=mock.Mock(return_value=0),
            poll=mock.Mock(return_value=0),
        )
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ) as popen:
            app._review("draft", "clarify it")
        process.stdin.write.assert_called_once_with("draft\0clarify it")
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")
        self.assertEqual(app.snapshot()["review"], report)
        self.assertEqual(app.snapshot()["phase"], "reviewed")
        self.assertIn(
            "Rigorous", (self.trace.parent / "checked-statement.md").read_text()
        )

    def test_statement_review_only_finishes_without_proof_stages(self):
        app = self.app()
        with mock.patch.object(web_ui.threading, "Thread"):
            app.start_review(
                "Rough theorem.",
                author_model="unsupported-but-unused",
                author_prompt="Unused prompt without a marker.",
                critic_rounds=9,
                thinking_hours=72,
                review_only=True,
            )

        state = app.snapshot()
        self.assertEqual(state["phase"], "reviewing")
        self.assertTrue(state["statementReviewOnly"])
        self.assertEqual(set(state["workflow"]["nodes"]), {"statement_reviewer"})
        self.assertEqual(state["workflow"]["edges"], [])
        self.assertEqual(state["criticRounds"], web_ui.DEFAULT_CRITIC_ROUNDS)
        self.assertEqual(state["thinkingHours"], web_ui.DEFAULT_THINKING_HOURS)
        self.assertTrue((self.trace.parent / "prompts/review.txt").exists())
        self.assertFalse((self.trace.parent / "prompts/author.txt").exists())

        report = {"statement": "Checked theorem.", "notes": "Now precise."}
        process = SimpleNamespace(
            stdin=mock.Mock(),
            stdout=io.StringIO(json.dumps({
                "kind": "review_result", "stage": "review", "review": report,
            }) + "\n"),
            wait=mock.Mock(return_value=0),
            poll=mock.Mock(return_value=0),
        )
        with mock.patch.object(web_ui.subprocess, "Popen", return_value=process):
            app._review("Rough theorem.", token=app.active_token)

        state = app.snapshot()
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["review"], report)
        self.assertEqual(state["output"], "Checked theorem.")
        self.assertIn(
            "Checked theorem.",
            (self.trace.parent / "checked-statement.md").read_text(encoding="utf-8"),
        )
        with self.assertRaisesRegex(ValueError, "review only"):
            app.approve()

    def test_statement_review_only_requires_a_boolean(self):
        with self.assertRaisesRegex(ValueError, "enabled or disabled"):
            self.app().start_review("Draft.", review_only="true")

    def test_stop_before_review_attaches_preserves_visible_output(self):
        app = self.app()
        app.state["phase"] = "reviewing"
        token = app.active_token = object()
        record = {
            "kind": "codex_event",
            "event": {
                "type": "item.completed",
                "item": {"type": "reasoning", "text": "Checked corner cases."},
            },
        }
        process = SimpleNamespace(
            stdin=mock.Mock(),
            stdout=io.StringIO(json.dumps(record) + "\n"),
            wait=mock.Mock(return_value=130),
            poll=mock.Mock(return_value=130),
        )

        def attach_after_stop(*_, **__):
            app.stop()
            return process

        with mock.patch.object(
            web_ui.subprocess, "Popen", side_effect=attach_after_stop
        ), mock.patch.object(web_ui, "stop_process_tree") as stop_tree:
            app._review("draft", token=token)
        self.assertEqual(stop_tree.call_args_list, [mock.call(None), mock.call(process)])
        self.assertEqual(app.snapshot()["phase"], "done")
        self.assertEqual(app.snapshot()["error"], "Stopped.")

    def test_review_reaps_a_child_after_broken_stdin(self):
        app = self.app()
        app.state["phase"] = "reviewing"
        process = mock.Mock()
        process.stdin.write.side_effect = BrokenPipeError("closed")
        process.poll.return_value = None
        process.wait.return_value = 130
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ), mock.patch.object(web_ui, "stop_process_tree") as stop_tree:
            app._review("draft")
        stop_tree.assert_called_once_with(process)
        self.assertEqual(app.snapshot()["phase"], "input")

    def test_stop_is_safe_to_click_twice(self):
        app = self.app()
        process = mock.Mock()
        app.state["phase"], app.process = "running", process
        with mock.patch.object(web_ui, "stop_process_tree") as stop_tree:
            app.stop()
            app.stop()
        stop_tree.assert_called_once_with(process)
        self.assertEqual(app.snapshot()["phase"], "done")
        self.assertEqual(app.snapshot()["error"], "Stopped.")

    def test_author_time_limit_can_be_replaced_while_initial_author_runs(self):
        app = self.app()
        process = mock.Mock()
        process.poll.return_value = None
        app.process = process
        app.state.update(
            phase="running", stage="solve", activeNode="author",
            thinkingHours=24,
        )
        app._write_author_limit(24)

        self.assertEqual(app.set_author_time_limit("6.5"), 6.5)

        state = app.snapshot()
        self.assertEqual(state["thinkingHours"], 6.5)
        self.assertEqual(
            json.loads((self.trace.parent / web_ui.AUTHOR_LIMIT_FILENAME).read_text(
                encoding="utf-8"
            )),
            {"hours": 6.5},
        )
        self.assertEqual(state["trace"][-1]["label"], "Author time limit set")
        self.assertIn("now 6.5 hours", state["trace"][-1]["text"])

    def test_author_time_setting_is_limited_to_the_active_initial_author(self):
        app = self.app()
        process = mock.Mock()
        process.poll.return_value = None
        app.process = process
        app.state.update(
            phase="running", stage="critic", activeNode="critic",
            thinkingHours=24,
        )
        with self.assertRaisesRegex(ValueError, "initial proof author"):
            app.set_author_time_limit(1)
        app.state.update(stage="solve", activeNode="author")
        for value in (0, 169):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "at most 168"
            ):
                app.set_author_time_limit(value)

    def test_windows_stop_force_kills_the_complete_process_tree(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = None
        process.wait.return_value = 1
        with mock.patch.object(web_ui.os, "name", "nt"), mock.patch.object(
            web_ui.subprocess, "run"
        ) as run:
            web_ui.stop_process_tree(process)
        run.assert_called_once_with(
            ["taskkill", "/PID", "1234", "/T", "/F"],
            stdin=web_ui.subprocess.DEVNULL,
            stdout=web_ui.subprocess.DEVNULL,
            stderr=web_ui.subprocess.DEVNULL,
            timeout=web_ui.STOP_TIMEOUT_SECONDS,
            check=False,
        )
        process.wait.assert_called_once_with(timeout=web_ui.STOP_TIMEOUT_SECONDS)

    def test_solver_receives_the_reviewed_statement_and_critic_limit(self):
        app = self.app()
        app.state.update(
            phase="reviewed",
            review={"statement": "Rigorous", "notes": "Sound."},
            criticRounds=7,
        )
        process = SimpleNamespace(
            stdin=mock.Mock(),
            stdout=mock.Mock(),
        )
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(web_ui.threading, "Thread"):
            app.approve()
        process.stdin.write.assert_called_once_with("Rigorous\0" + "7\0" + "24")
        command = popen.call_args.args[0]
        self.assertEqual(
            command[command.index("--author-model") + 1], "gpt-5.6-sol"
        )
        self.assertEqual(
            command[command.index("--critic-model") + 1], "gpt-5.6-sol"
        )
        self.assertEqual(
            command[command.index("--writer-model") + 1], "gpt-5.6-sol"
        )
        self.assertEqual(
            command[command.index("--reasoning-effort") + 1], "ultra"
        )
        self.assertEqual(
            command[command.index("--speed") + 1], web_ui.DEFAULT_SPEED
        )
        limit_path = Path(command[command.index("--author-limit-file") + 1])
        self.assertEqual(limit_path, self.trace.parent / web_ui.AUTHOR_LIMIT_FILENAME)
        self.assertEqual(
            json.loads(limit_path.read_text(encoding="utf-8")), {"hours": 24}
        )
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")
        self.assertEqual(popen.call_args.kwargs["cwd"], self.trace.parent)

    def test_solver_is_reaped_if_initial_input_breaks(self):
        app = self.app()
        app.state.update(
            phase="reviewed",
            review={"statement": "Rigorous", "notes": "Sound."},
        )
        process = mock.Mock()
        process.stdin.write.side_effect = BrokenPipeError("closed")
        process.poll.return_value = None
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ), mock.patch.object(web_ui, "stop_process_tree") as stop_tree:
            with self.assertRaises(BrokenPipeError):
                app.approve()
        stop_tree.assert_called_once_with(process)
        self.assertIsNone(app.process)
        self.assertEqual(app.snapshot()["phase"], "reviewed")

    def test_solver_receives_each_selected_proof_model(self):
        app = self.app()
        app.state.update(
            phase="reviewed",
            review={"statement": "Rigorous", "notes": "Sound."},
            authorModel="gpt-5.6-terra",
            criticModel="gpt-5.6-luna",
            writerModel="gpt-5.6-sol",
        )
        process = SimpleNamespace(stdin=mock.Mock(), stdout=mock.Mock())
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(web_ui.threading, "Thread"):
            app.approve()
        command = popen.call_args.args[0]
        self.assertEqual(
            command[command.index("--author-model") + 1], "gpt-5.6-terra"
        )
        self.assertEqual(
            command[command.index("--critic-model") + 1], "gpt-5.6-luna"
        )
        self.assertEqual(
            command[command.index("--writer-model") + 1], "gpt-5.6-sol"
        )

    def test_solver_receives_the_selected_reasoning_effort(self):
        app = self.app()
        app.state.update(
            phase="reviewed",
            review={"statement": "Rigorous", "notes": "Sound."},
            reasoningEffort="high",
        )
        process = SimpleNamespace(stdin=mock.Mock(), stdout=mock.Mock())
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(web_ui.threading, "Thread"):
            app.approve()
        command = popen.call_args.args[0]
        self.assertEqual(
            command[command.index("--reasoning-effort") + 1], "high"
        )

    def test_direct_statement_edit_can_be_approved_without_another_review(self):
        app = self.app()
        app.state.update(
            phase="reviewed",
            review={"statement": "Reviewed version", "notes": "Previously sound."},
        )
        process = SimpleNamespace(stdin=mock.Mock(), stdout=mock.Mock())
        with mock.patch.object(
            web_ui.subprocess, "Popen", return_value=process
        ), mock.patch.object(web_ui.threading, "Thread"):
            app.approve("  Author's final version  ")
        process.stdin.write.assert_called_once_with(
            "Author's final version\0" + "4\0" + "24"
        )
        self.assertEqual(
            app.snapshot()["review"]["statement"], "Author's final version"
        )
        saved = (self.trace.parent / "checked-statement.md").read_text()
        self.assertIn("Author's final version", saved)
        self.assertIn("directly approved by the author", saved)

    def test_transcript_survives_reset_and_restart(self):
        app = self.app()
        app.add_trace({"kind": "request", "stage": "review", "text": "Exact prompt"})
        app.reset()
        self.assertEqual(app.snapshot()["trace"][0]["text"], "Exact prompt")
        self.assertEqual(web_ui.App(self.trace).snapshot()["trace"][0]["text"], "Exact prompt")

    def test_snapshot_can_return_only_new_transcript_records(self):
        app = self.app()
        app.add_trace({"kind": "status", "text": "First"})
        position = app.snapshot()["traceVersion"]
        app.add_trace({"kind": "status", "text": "Second"})
        update = app.snapshot(position)
        self.assertEqual(update["traceFrom"], position)
        self.assertEqual([entry["text"] for entry in update["trace"]], ["Second"])

    def test_prompts_survive_the_event_limit_and_restart(self):
        app = self.app()
        with mock.patch.object(web_ui, "TRACE_LIMIT", 3):
            app.add_trace({"kind": "request", "text": "Pinned prompt"})
            for number in range(5):
                app.add_trace({"kind": "status", "text": f"Routine {number}"})
            self.assertIn(
                "Pinned prompt",
                [entry["text"] for entry in app.snapshot(-1)["trace"]],
            )
            restored = web_ui.App(self.trace)
            self.assertIn(
                "Pinned prompt",
                [entry["text"] for entry in restored.snapshot(-1)["trace"]],
            )

    def test_transcript_is_private_and_recovers_before_a_bad_tail(self):
        app = self.app()
        app.add_trace({"kind": "status", "stage": "review", "text": "Kept"})
        if web_ui.os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.trace.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.trace.parent.stat().st_mode), 0o700)
        with self.trace.open("a", encoding="utf-8") as stream:
            stream.write("{unfinished\n")
        self.assertEqual(web_ui.App(self.trace).snapshot()["trace"][0]["text"], "Kept")

    def test_completed_message_is_the_solver_output_fallback(self):
        app = self.app()
        app.state["phase"] = "running"
        record = {
            "kind": "codex_event",
            "stage": "solve",
            "root": True,
            "event": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "text": "Complete answer",
                    }
                },
            },
        }
        process = SimpleNamespace(
            stdout=io.StringIO(json.dumps(record) + "\n"),
            wait=mock.Mock(return_value=0),
        )
        app._read_output(process)
        self.assertEqual(app.snapshot()["output"], "Complete answer")

    def test_app_output_stream_receives_original_events_before_filtering(self):
        terminal = io.StringIO()
        app = web_ui.App(self.trace, output_stream=terminal)
        app.state["phase"] = "running"
        record = {
            "kind": "status", "stage": "solve", "text": "Author started."
        }
        line = json.dumps(record) + "\n"
        process = SimpleNamespace(
            stdout=io.StringIO(line), wait=mock.Mock(return_value=0),
        )

        app._read_output(process)

        self.assertEqual(terminal.getvalue(), line)

    def test_concise_headless_output_reports_only_steps_and_diagnostics(self):
        terminal = io.StringIO()
        output = web_ui.ConciseHeadlessOutput(
            terminal, threading.Lock(), "alpha.md"
        )
        records = [
            {
                "kind": "request", "stage": "solve",
                "label": "Exact solve input", "text": "SECRET PROMPT",
            },
            {
                "kind": "request", "stage": "solve",
                "label": "Goal continuation instruction", "text": "SECRET GOAL",
            },
            {
                "kind": "codex_event", "stage": "solve",
                "event": {"method": "item/reasoning", "text": "SECRET REASONING"},
            },
            {"kind": "request", "stage": "critic", "text": "SECRET CRITIC"},
            {
                "kind": "critic_result", "stage": "critic",
                "text": "SECRET CRITIC RESULT",
            },
            {
                "kind": "request", "stage": "repair",
                "label": "Proof author revision 1", "text": "SECRET REPAIR",
            },
            {"kind": "request", "stage": "critic", "text": "SECRET CRITIC"},
            {"kind": "request", "stage": "critic", "text": "SECRET CRITIC"},
            {"kind": "request", "stage": "final", "text": "SECRET FINAL"},
            {
                "kind": "final_result", "stage": "final",
                "output": "SECRET PROOF",
            },
            {"kind": "diagnostic", "stage": "final", "text": "error: disk full"},
        ]
        for record in records:
            output.write(json.dumps(record) + "\n")
        output.write('{"text":"SECRET MALFORMED"\n')

        self.assertEqual(terminal.getvalue().splitlines(), [
            "[alpha.md] Current step: Proof author",
            "[alpha.md] Current step: Independent critic (round 1)",
            "[alpha.md] Current step: Proof author revision 1",
            "[alpha.md] Current step: Independent critic (round 1)",
            "[alpha.md] Current step: Independent critic (round 2)",
            "[alpha.md] Current step: LaTeX editor",
            "[alpha.md] Error: disk full",
            "[alpha.md] Diagnostic: Malformed solver event.",
        ])
        self.assertNotIn("SECRET", terminal.getvalue())

    def test_verbose_jsonl_output_identifies_each_batch_input(self):
        terminal, lock = io.StringIO(), threading.Lock()
        output = web_ui.TaggedJsonlOutput(terminal, lock, "alpha.md")

        output.write(json.dumps({"kind": "status", "text": "Started."}) + "\n")
        output.write("plain diagnostic\n")

        records = [json.loads(line) for line in terminal.getvalue().splitlines()]
        self.assertEqual(records[0]["inputFile"], "alpha.md")
        self.assertEqual(records[0]["kind"], "status")
        self.assertEqual(records[1], {
            "kind": "diagnostic",
            "text": "plain diagnostic",
            "inputFile": "alpha.md",
        })

    def test_headless_runner_uses_the_direct_markdown_pipeline(self):
        folder = Path(self.folder.name)
        statement = folder / "proof.md"
        statement.write_text("Exact theorem.\n", encoding="utf-8")
        options = {
            "thinking_hours": 12,
            "author_effort": "high",
            "speed_mode": "fast",
        }
        fake = mock.Mock()
        fake.run_dir = folder / "run"
        fake.snapshot.return_value = {"phase": "done", "error": ""}
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(
            web_ui, "direct_cli_options", return_value=options
        ) as normalize, mock.patch.object(
            web_ui, "App", return_value=fake
        ) as app_type:
            code = web_ui.run_headless_markdown(
                statement, runs=folder / "runs",
                output_stream=output, error_stream=errors,
                thinking_hours=12, author_effort="high",
            )

        self.assertEqual(code, 0)
        normalize.assert_called_once_with(thinking_hours=12, author_effort="high")
        app_type.assert_called_once()
        app_output = app_type.call_args.kwargs["output_stream"]
        self.assertIsInstance(app_output, web_ui.ConciseHeadlessOutput)
        self.assertIs(app_output.stream, output)
        self.assertEqual(app_output.input_file, "proof.md")
        self.assertEqual(app_type.call_args.kwargs["runs"], folder / "runs")
        call = fake.start_direct_statement.call_args.kwargs
        self.assertEqual(call["statement"], "Exact theorem.\n")
        self.assertEqual(call["thinking_hours"], 12)
        self.assertEqual(call["author_effort"], "high")
        self.assertIn("[proof.md] Proof started", errors.getvalue())
        self.assertIn("[proof.md] Proof finished", errors.getvalue())

    def test_folder_runner_starts_every_markdown_job_before_polling(self):
        folder = Path(self.folder.name)
        (folder / "b.md").write_text("Theorem B.", encoding="utf-8")
        (folder / "a.md").write_text("Theorem A.", encoding="utf-8")
        started = []
        first, second = mock.Mock(), mock.Mock()
        first.run_dir, second.run_dir = folder / "run-a", folder / "run-b"
        first.start_direct_statement.side_effect = lambda **_: started.append("a")
        second.start_direct_statement.side_effect = lambda **_: started.append("b")

        def first_state():
            self.assertEqual(started, ["a", "b"])
            return {"phase": "done", "error": ""}

        first.snapshot.side_effect = first_state
        second.snapshot.return_value = {"phase": "done", "error": "failed B"}
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(
            web_ui, "direct_cli_options", return_value={}
        ), mock.patch.object(
            web_ui, "App", side_effect=[first, second]
        ) as app_type:
            code = web_ui.run_headless_markdown(
                folder, runs=folder / "runs",
                output_stream=output, error_stream=errors,
            )

        self.assertEqual(code, 1)
        self.assertEqual(app_type.call_count, 2)
        for call in app_type.call_args_list:
            self.assertIsInstance(
                call.kwargs["output_stream"], web_ui.ConciseHeadlessOutput
            )
        self.assertEqual(
            first.start_direct_statement.call_args.kwargs["statement"],
            "Theorem A.",
        )
        self.assertEqual(
            second.start_direct_statement.call_args.kwargs["statement"],
            "Theorem B.",
        )
        self.assertIn("[b.md] Proof failed: failed B", errors.getvalue())

    def test_ctrl_c_stops_every_active_folder_job(self):
        folder = Path(self.folder.name)
        (folder / "a.md").write_text("A.", encoding="utf-8")
        (folder / "b.md").write_text("B.", encoding="utf-8")
        apps = [mock.Mock(), mock.Mock()]
        for number, app in enumerate(apps):
            app.run_dir = folder / f"run-{number}"
            app.snapshot.return_value = {"phase": "running", "error": ""}
        with mock.patch.object(
            web_ui, "direct_cli_options", return_value={}
        ), mock.patch.object(
            web_ui, "App", side_effect=apps
        ), mock.patch.object(
            web_ui.time, "sleep", side_effect=KeyboardInterrupt
        ):
            code = web_ui.run_headless_markdown(
                folder, output_stream=io.StringIO(), error_stream=io.StringIO()
            )

        self.assertEqual(code, 130)
        for app in apps:
            app.stop.assert_called_once_with()

    def test_ctrl_c_during_folder_startup_stops_already_created_jobs(self):
        folder = Path(self.folder.name)
        (folder / "a.md").write_text("A.", encoding="utf-8")
        app = mock.Mock()
        app.start_direct_statement.side_effect = KeyboardInterrupt
        with mock.patch.object(
            web_ui, "direct_cli_options", return_value={}
        ), mock.patch.object(web_ui, "App", return_value=app):
            code = web_ui.run_headless_markdown(
                folder, output_stream=io.StringIO(), error_stream=io.StringIO()
            )

        self.assertEqual(code, 130)
        app.stop.assert_called_once_with()

    def test_markdown_positional_argument_and_overrides_select_headless_mode(self):
        with mock.patch(
            "sys.argv", [
                "web_ui.py", "statement.md",
                "-criticRounds", "6", "-authorEffort", "high",
                "-speedMode", "standard",
            ]
        ), mock.patch.object(
            web_ui.tcs_agent, "configure_standard_streams"
        ) as configure, mock.patch.object(
            web_ui, "run_headless_markdown", return_value=0
        ) as run, mock.patch.object(web_ui, "Server") as server:
            self.assertEqual(web_ui.main(), 0)

        configure.assert_called_once_with()
        call = run.call_args
        self.assertEqual(call.args, ("statement.md",))
        self.assertEqual(call.kwargs["critic_rounds"], 6)
        self.assertEqual(call.kwargs["author_effort"], "high")
        self.assertEqual(call.kwargs["speed_mode"], "standard")
        self.assertEqual(call.kwargs["thinking_hours"], 24)
        self.assertFalse(call.kwargs["verbose_events"])
        server.assert_not_called()

    def test_final_latex_replaces_the_working_solution(self):
        app = self.app()
        app.state["phase"] = "running"
        records = [
            {
                "kind": "codex_event", "stage": "solve", "root": True,
                "event": {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "answer-1", "type": "agentMessage",
                            "text": "Working solution",
                        }
                    },
                },
            },
            {
                "kind": "final_result", "stage": "final",
                "output": "\\begin{proof}Final.\\end{proof}",
            },
        ]
        process = SimpleNamespace(
            stdout=io.StringIO("".join(json.dumps(record) + "\n" for record in records)),
            wait=mock.Mock(return_value=0),
        )
        app._read_output(process)
        self.assertEqual(app.snapshot()["stage"], "final")
        self.assertEqual(
            app.snapshot()["output"], "\\begin{proof}Final.\\end{proof}"
        )
        self.assertEqual(
            (self.trace.parent / "final.tex").read_text(),
            "\\begin{proof}Final.\\end{proof}",
        )

    def test_stopped_solver_explains_when_no_answer_exists(self):
        app = self.app()
        app.state["phase"] = "stopping"
        process = SimpleNamespace(
            stdout=io.StringIO(""),
            wait=mock.Mock(return_value=130),
        )
        app._read_output(process)
        self.assertEqual(app.snapshot()["error"], "Stopped.")
        self.assertIn("before it produced an answer", app.snapshot()["output"])

    def test_stopped_solver_saves_its_partial_output(self):
        app = self.app()
        app.state.update(phase="stopping", output="Partial proof")
        process = SimpleNamespace(
            stdout=io.StringIO(""),
            wait=mock.Mock(return_value=130),
        )
        app._read_output(process)
        self.assertEqual(
            (self.trace.parent / "partial-output.md").read_text(), "Partial proof"
        )

    def test_unverified_critic_candidate_never_becomes_final_output(self):
        app = self.app()
        app.state["phase"] = "running"
        record = {
            "kind": "partial_result", "stage": "critic",
            "output": "Latest unverified candidate",
        }
        process = SimpleNamespace(
            stdout=io.StringIO(json.dumps(record) + "\n"),
            wait=mock.Mock(return_value=1),
        )
        app._read_output(process)
        self.assertEqual(app.snapshot()["output"], "Latest unverified candidate")
        self.assertTrue((self.trace.parent / "partial-output.md").exists())
        self.assertFalse((self.trace.parent / "final.tex").exists())

    def test_revision_stage_uses_the_same_proof_author_node(self):
        app = self.app()
        app.state.update(phase="running", activeNode="critic", round=2)
        record = {
            "kind": "request", "stage": "repair", "node": "author",
            "round": 0, "text": "Fix this gap.",
        }
        process = SimpleNamespace(
            stdout=io.StringIO(json.dumps(record) + "\n"),
            wait=mock.Mock(return_value=0),
        )
        app._read_output(process)
        self.assertEqual(app.snapshot()["activeNode"], "author")
        self.assertEqual(app.snapshot()["round"], 0)

    def test_author_deadline_saves_a_failure_summary(self):
        app = self.app()
        app.state["phase"] = "running"
        record = {
            "kind": "failure_result", "stage": "failure",
            "output": "Tried a reduction; one lemma remains open.",
        }
        process = SimpleNamespace(
            stdout=io.StringIO(json.dumps(record) + "\n"),
            wait=mock.Mock(return_value=0),
        )
        app._read_output(process)
        state = app.snapshot()
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["activeNode"], "failure_summary")
        self.assertEqual(state["output"], record["output"])
        self.assertEqual(
            (self.trace.parent / "failure-summary.md").read_text(),
            record["output"],
        )
        self.assertFalse((self.trace.parent / "final.tex").exists())


class HttpTests(unittest.TestCase):
    """Check the initial page and tiny state endpoint."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        trace = Path(self.folder.name) / "transcript.jsonl"
        self.server = web_ui.Server((web_ui.HOST, 0), trace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.http = http.client.HTTPConnection(
            web_ui.HOST, self.server.server_port, timeout=2
        )
        self.token_header = {"X-TCS-Prover-Token": self.server.token}

    def tearDown(self):
        self.http.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.folder.cleanup()

    def get(self, path):
        self.http.request("GET", path, headers=self.token_header)
        response = self.http.getresponse()
        return response, response.read()

    def post(self, path, body):
        self.http.request(
            "POST", path, json.dumps(body),
            {"Content-Type": "application/json", **self.token_header},
        )
        response = self.http.getresponse()
        return response, response.read()

    def test_first_page_is_basic_and_advanced_is_closed(self):
        response, body = self.get("/")
        page = body.decode()
        self.assertEqual(response.status, 200)
        self.assertIn("Problem statement", page)
        self.assertIn('name="problemMode" value="statement" checked', page)
        self.assertIn('name="problemMode" value="algorithmic"', page)
        self.assertIn('name="problemMode" value="latex"', page)
        self.assertIn('id="algorithmicFields" class="algorithmic-fields" hidden', page)
        self.assertIn('for="modelOfComputation">Model of computation', page)
        self.assertIn('for="problemDescription">Problem description', page)
        self.assertIn('id="latexInput"', page)
        self.assertNotIn('id="latexTheorem"', page)
        self.assertNotIn('id="latexProof"', page)
        self.assertIn('id="modelPresets" class="preset-options"', page)
        self.assertIn('id="problemPresets" class="preset-options"', page)
        self.assertIn("Goal (asymptotic upper or lower bound)", page)
        self.assertIn("<summary>Advanced</summary>", page)
        self.assertIn('label for="reviewModel"', page)
        self.assertIn('value="gpt-5.6-terra"', page)
        self.assertIn('value="gpt-5.6-sol"', page)
        self.assertIn('value="gpt-5.6-luna"', page)
        self.assertIn('id="authorModel"', page)
        self.assertIn('id="criticModel"', page)
        self.assertIn('id="writerModel"', page)
        self.assertIn('id="reasoningEffort"', page)
        self.assertIn("Ultra is the default for every role.", page)
        self.assertIn('id="activityPanel" class="audit-drawer"', page)
        self.assertIn('aria-labelledby="auditTitle" hidden', page)
        self.assertIn("Private chain-of-thought", page)
        self.assertIn("Show details", page)
        self.assertIn("Prompts and responses", page)
        self.assertIn("count resets after an author rewrite", page)
        self.assertIn("LaTeX editing requires", page)
        self.assertNotIn("Download all JSONL", page)
        self.assertIn('id="criticRounds"', page)
        self.assertIn('id="thinkingHours"', page)
        self.assertIn('id="speedMode"', page)
        self.assertIn('id="skipStatementReview"', page)
        self.assertIn("Send the statement directly to the proof author", page)
        self.assertIn('id="statementReviewOnly"', page)
        self.assertIn("Statement review only", page)
        self.assertIn("The proof\n                    author, critic, and LaTeX editor will not run", page)
        self.assertIn('value="standard"', page)
        self.assertIn('value="fast"', page)
        self.assertIn("Saved edits are reused for future jobs", page)
        self.assertIn('value="24"', page)
        self.assertIn('id="feedback"', page)
        self.assertIn('id="workflowNodes"', page)
        self.assertIn('id="timelineList"', page)
        self.assertIn('id="stopButton" class="danger">Stop</button>', page)
        self.assertIn('id="authorTimeLimitControl"', page)
        self.assertIn('id="authorLimitHours"', page)
        self.assertIn('id="setAuthorTimeLimitButton"', page)
        self.assertIn('id="homeButton" class="secondary">Return home</button>', page)
        self.assertIn('id="jobsList" class="jobs-list"', page)
        self.assertNotIn("Latest complete output", page)

    def test_details_show_only_prompt_and_returned_text(self):
        response, body = self.get("/app.js")
        script = body.decode()
        self.assertEqual(response.status, 200)
        self.assertIn('"Prompt to OpenAI"', script)
        self.assertIn('"Returned text from OpenAI"', script)
        self.assertIn('"Repeat until a clean PASS"', script)
        self.assertIn('"loop-back"', script)
        self.assertIn("state.problemMode === \"algorithmic\"", script)
        self.assertIn('request("/algorithmic"', script)
        self.assertIn('request("/finalize"', script)
        self.assertIn('skipReview ? "/direct" : "/review"', script)
        self.assertIn("state.skipStatementReview", script)
        self.assertIn("statementReviewOnly: reviewOnly", script)
        self.assertIn(
            'ui.workflowNodes.replaceChildren(makeNode("statement_reviewer", "1"))',
            script,
        )
        self.assertIn("show(ui.approve, !reviewOnlyResult)", script)
        self.assertIn(
            "show(ui.authorModelSetting, !latexOnly && !reviewOnly)", script
        )
        self.assertIn(
            "show(ui.thinkingHoursSetting, !latexOnly && !reviewOnly)", script
        )
        self.assertIn(
            "ui.skipStatementReview.checked = false", script
        )
        self.assertIn("ui.workflowNodes.replaceChildren(branch)", script)
        self.assertIn("button.textContent = entry.name", script)
        self.assertIn("field.value = entry.description", script)
        self.assertIn("renderAlgorithmicPresets(state)", script)
        self.assertIn('"Clean PASS only"', script)
        self.assertIn("{ statement: ui.proposed.value }", script)
        self.assertIn("hasFeedback || !ui.proposed.value.trim()", script)
        self.assertIn('"critic-pass-stem"', script)
        self.assertIn('reviewer.classList.add("pre-loop")', script)
        self.assertIn(
            "branch.append(failureNode, failureRoute, loop, passRoute, editor)",
            script,
        )
        self.assertIn("appendFormattedText", script)
        self.assertIn("importantEntry", script)
        self.assertIn('item.dataset.pinned !== "true"', script)
        self.assertIn("document.createTextNode", script)
        self.assertNotIn(".innerHTML", script)
        self.assertNotIn('"author_repair"', script)
        self.assertNotIn("JSON.stringify(entry", script)
        self.assertNotIn("appendAudit", script)
        self.assertIn('separate.textContent = "Open in new window"', script)
        self.assertIn('remove.textContent = "Delete"', script)
        self.assertIn("/delete-job?job=", script)
        self.assertIn("item.dataset.job = job.runId", script)
        self.assertIn("Restart TCS Prover if it was already open", script)
        self.assertIn('jobPath("/state"', script)
        self.assertIn('"tcs-prover-role-prompts"', script)
        self.assertIn("localStorage.setItem(promptStorageKey", script)
        self.assertIn('act("/set-author-time-limit", { hours })', script)
        self.assertIn('state.stage === "solve"', script)

    def test_hidden_workflow_rail_cannot_collapse_the_input_form(self):
        response, body = self.get("/styles.css")
        self.assertEqual(response.status, 200)
        styles = body.decode()
        self.assertIn(".workflow-rail[hidden] + .work-pane", styles)
        self.assertIn(".workflow-loop", styles)
        self.assertIn(".loop-back::before", styles)
        self.assertIn('[data-node="author"]::before', styles)
        self.assertIn(".failure-branch .node-dot::after", styles)
        self.assertIn(".post-loop .node-dot::before", styles)
        self.assertIn("grid-row: 1 / 4", styles)
        self.assertIn(".critic-pass", styles)
        self.assertIn(".critic-pass-stem", styles)
        self.assertIn(".pre-loop-arrow", styles)
        self.assertIn(".post-loop", styles)
        self.assertIn(".mode-options", styles)
        self.assertIn(".algorithmic-fields", styles)
        self.assertIn(".preset-options", styles)
        self.assertIn(".preset-option.selected", styles)
        self.assertIn("#speedMode", styles)
        self.assertIn("width: min(100%, 260px)", styles)
        self.assertIn(".advanced-toggle", styles)

    def test_state_starts_at_input(self):
        response, body = self.get("/state")
        self.assertEqual(response.status, 200)
        state = json.loads(body)
        self.assertEqual(state["phase"], "input")
        self.assertEqual(state["problemMode"], "statement")
        self.assertFalse(state["skipStatementReview"])
        self.assertFalse(state["statementReviewOnly"])
        self.assertEqual(state["latexInput"], "")
        self.assertEqual(state["criticRounds"], 4)
        self.assertEqual(state["thinkingHours"], 24)
        self.assertEqual(state["speedMode"], web_ui.DEFAULT_SPEED)
        self.assertEqual(state["reviewModel"], "gpt-5.6-sol")
        self.assertEqual(state["authorModel"], "gpt-5.6-sol")
        self.assertEqual(state["criticModel"], "gpt-5.6-sol")
        self.assertEqual(state["writerModel"], "gpt-5.6-sol")
        self.assertEqual(state["reasoningEffort"], "ultra")
        self.assertEqual(state["workflow"]["settings"]["reasoning_effort"], "ultra")
        presets = state["workflow"]["settings"]["algorithmic_presets"]
        self.assertEqual(presets["models"], web_ui.MODEL_PRESETS)
        self.assertEqual(presets["problems"], web_ui.PROBLEM_PRESETS)
        self.assertIn("critic", state["workflow"]["nodes"])
        self.assertIn("failure_summary", state["workflow"]["nodes"])
        self.assertNotIn("author_repair", state["workflow"]["nodes"])
        self.assertIn("repair", state["workflow"]["nodes"]["author"]["stages"])
        self.assertIn("prompt_change", state["workflow"]["edges"][0])
        reject_edge = next(
            edge for edge in state["workflow"]["edges"]
            if edge["from"] == "critic" and edge["to"] == "author"
        )
        self.assertEqual(reject_edge["when"], "critic rejects")
        failure_edge = next(
            edge for edge in state["workflow"]["edges"]
            if edge["from"] == "author" and edge["to"] == "failure_summary"
        )
        self.assertEqual(failure_edge["when"], "author time limit is reached")
        final_edge = state["workflow"]["edges"][-1]
        self.assertEqual(
            (final_edge["from"], final_edge["to"]),
            ("critic", "latex_editor"),
        )
        self.assertEqual(final_edge["when"], "critic gives a clean pass")

    def test_transcript_endpoint_returns_the_complete_disk_log(self):
        self.server.app.add_trace({
            "kind": "status", "stage": "review", "text": "Saved event",
        })
        response, body = self.get("/transcript")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/x-ndjson")
        self.assertIn(b"Saved event", body)

    def test_review_endpoint_forwards_feedback(self):
        with mock.patch.object(self.server.app, "start_review") as start:
            response, _ = self.post("/review", {
                "statement": "draft", "feedback": "clarify",
                "criticRounds": 6, "reviewModel": "gpt-5.6-sol",
                "thinkingHours": 1.5,
                "authorModel": "gpt-5.6-terra",
                "criticModel": "gpt-5.6-luna",
                "writerModel": "gpt-5.6-sol",
                "reasoningEffort": "high",
            })
        self.assertEqual(response.status, 200)
        start.assert_called_once_with(
            "draft", "clarify", 6, "gpt-5.6-sol", 1.5,
            "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol",
            "high", speed_mode=web_ui.DEFAULT_SPEED,
        )

    def test_review_endpoint_forwards_statement_review_only(self):
        with mock.patch.object(self.server.app, "start_review") as start:
            response, _ = self.post("/review", {
                "statement": "draft", "statementReviewOnly": True,
            })

        self.assertEqual(response.status, 200)
        self.assertTrue(start.call_args.kwargs["review_only"])

    def test_review_endpoint_rejects_non_boolean_review_only(self):
        response, body = self.post("/review", {
            "statement": "draft", "statementReviewOnly": "true",
        })

        self.assertEqual(response.status, 400)
        self.assertIn("enabled or disabled", json.loads(body)["error"])

    def test_review_endpoint_forwards_role_efforts_and_prompts(self):
        values = {
            "statement": "draft",
            "reviewEffort": "low", "authorEffort": "medium",
            "criticEffort": "high", "writerEffort": "max",
            "reviewPrompt": "Review.", "authorPrompt": "Solve [STATEMENT].",
            "criticPrompt": "Critic.", "finalPrompt": "Final.",
        }
        with mock.patch.object(self.server.app, "start_review") as start:
            response, _ = self.post("/review", values)
        self.assertEqual(response.status, 200)
        options = start.call_args.kwargs
        self.assertEqual(options["review_effort"], "low")
        self.assertEqual(options["author_effort"], "medium")
        self.assertEqual(options["critic_effort"], "high")
        self.assertEqual(options["writer_effort"], "max")
        self.assertEqual(options["author_prompt"], "Solve [STATEMENT].")

    def test_algorithmic_endpoint_forwards_fields_and_proof_settings(self):
        values = {
            "modelOfComputation": "word-RAM",
            "problemDescription": "Sort integers.",
            "goal": "Prove O(n log n).",
            "criticRounds": 5, "thinkingHours": 2.5,
            "authorModel": "gpt-5.6-terra",
            "criticModel": "gpt-5.6-luna",
            "writerModel": "gpt-5.6-sol",
            "authorEffort": "high", "criticEffort": "max",
            "writerEffort": "medium",
            "authorPrompt": "Solve [STATEMENT].",
            "criticPrompt": "Audit it.", "finalPrompt": "Write LaTeX.",
            "speedMode": "standard",
        }
        with mock.patch.object(
            self.server.app, "start_algorithmic"
        ) as start:
            response, _ = self.post("/algorithmic", values)
        self.assertEqual(response.status, 200)
        options = start.call_args.kwargs
        self.assertEqual(options["model_of_computation"], "word-RAM")
        self.assertEqual(options["problem_description"], "Sort integers.")
        self.assertEqual(options["goal"], "Prove O(n log n).")
        self.assertEqual(options["critic_rounds"], 5)
        self.assertEqual(options["author_model"], "gpt-5.6-terra")
        self.assertEqual(options["critic_effort"], "max")
        self.assertEqual(options["author_prompt"], "Solve [STATEMENT].")
        self.assertEqual(options["speed_mode"], "standard")

    def test_direct_endpoint_starts_author_with_proof_settings(self):
        values = {
            "statement": "Exact theorem.",
            "criticRounds": 5, "thinkingHours": 2.5,
            "authorModel": "gpt-5.6-terra",
            "criticModel": "gpt-5.6-luna",
            "writerModel": "gpt-5.6-sol",
            "authorEffort": "high", "criticEffort": "max",
            "writerEffort": "medium",
            "authorPrompt": "Solve [STATEMENT].",
            "criticPrompt": "Audit it.", "finalPrompt": "Write LaTeX.",
            "speedMode": "standard",
        }
        with mock.patch.object(
            self.server.app, "start_direct_statement"
        ) as start:
            response, _ = self.post("/direct", values)
        self.assertEqual(response.status, 200)
        options = start.call_args.kwargs
        self.assertEqual(options["statement"], "Exact theorem.")
        self.assertEqual(options["critic_rounds"], 5)
        self.assertEqual(options["author_model"], "gpt-5.6-terra")
        self.assertEqual(options["critic_effort"], "max")
        self.assertEqual(options["speed_mode"], "standard")

    def test_finalize_endpoint_forwards_theorem_proof_and_writer_settings(self):
        values = {
            "content": "Theorem.\n\nProof.",
            "writerModel": "gpt-5.6-terra", "writerEffort": "high",
            "finalPrompt": "Polish carefully.", "speedMode": "standard",
        }
        with mock.patch.object(
            self.server.app, "start_latex_only"
        ) as start:
            response, _ = self.post("/finalize", values)
        self.assertEqual(response.status, 200)
        options = start.call_args.kwargs
        self.assertEqual(options["source"], "Theorem.\n\nProof.")
        self.assertEqual(options["writer_model"], "gpt-5.6-terra")
        self.assertEqual(options["writer_effort"], "high")
        self.assertEqual(options["final_prompt"], "Polish carefully.")
        self.assertEqual(options["speed_mode"], "standard")

    def test_algorithmic_endpoint_rejects_a_missing_field(self):
        response, body = self.post("/algorithmic", {
            "modelOfComputation": "word-RAM",
            "problemDescription": "",
            "goal": "Prove O(n log n).",
        })
        self.assertEqual(response.status, 400)
        self.assertIn("problem description", json.loads(body)["error"])

    def test_review_rejects_an_unknown_model(self):
        response, body = self.post("/review", {
            "statement": "draft", "reviewModel": "unknown",
        })
        self.assertEqual(response.status, 400)
        self.assertIn("Choose Sol, Terra, or Luna", json.loads(body)["error"])

    def test_review_rejects_an_unknown_proof_stage_model(self):
        response, body = self.post("/review", {
            "statement": "draft", "criticModel": "unknown",
        })
        self.assertEqual(response.status, 400)
        self.assertIn("every proof stage", json.loads(body)["error"])

    def test_review_rejects_an_unknown_reasoning_effort(self):
        response, body = self.post("/review", {
            "statement": "draft", "reasoningEffort": "unknown",
        })
        self.assertEqual(response.status, 400)
        self.assertIn("reasoning effort", json.loads(body)["error"])

    def test_review_rejects_an_unknown_speed(self):
        response, body = self.post("/review", {
            "statement": "draft", "speedMode": "turbo",
        })
        self.assertEqual(response.status, 400)
        self.assertIn("Standard or Fast", json.loads(body)["error"])

    def test_review_rejects_an_invalid_critic_limit(self):
        response, body = self.post("/review", {
            "statement": "draft", "criticRounds": 0,
        })
        self.assertEqual(response.status, 400)
        self.assertIn("Choose 1 to 100", json.loads(body)["error"])

    def test_review_rejects_an_invalid_author_limit(self):
        response, body = self.post("/review", {
            "statement": "draft", "thinkingHours": 0,
        })
        self.assertEqual(response.status, 400)
        self.assertIn("at most 168 hours", json.loads(body)["error"])

    def test_stop_endpoint_accepts_a_review(self):
        self.server.app.state["phase"] = "reviewing"
        response, body = self.post("/stop", {})
        self.assertEqual(response.status, 200)
        state = json.loads(body)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["error"], "Stopped.")

    def test_set_author_time_limit_endpoint_replaces_the_live_limit(self):
        process = mock.Mock()
        process.poll.return_value = None
        self.server.app.process = process
        self.server.app.state.update(
            phase="running", stage="solve", activeNode="author",
            thinkingHours=24,
        )
        self.server.app._write_author_limit(24)

        response, body = self.post("/set-author-time-limit", {"hours": 12})

        self.assertEqual(response.status, 200)
        state = json.loads(body)
        self.assertEqual(state["thinkingHours"], 12)
        self.assertEqual(state["trace"][-1]["label"], "Author time limit set")

    def test_state_rejects_a_request_without_the_launch_session(self):
        other = http.client.HTTPConnection(
            web_ui.HOST, self.server.server_port, timeout=2
        )
        try:
            other.request("GET", "/state")
            response = other.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
        finally:
            other.close()

    def test_actions_reject_a_foreign_browser_origin(self):
        self.http.request(
            "POST",
            "/reset",
            body="{}",
            headers={
                "Content-Type": "application/json",
                **self.token_header,
                "Origin": "https://example.com",
            },
        )
        response = self.http.getresponse()
        response.read()
        self.assertEqual(response.status, 403)


class ParallelJobTests(unittest.TestCase):
    """Check that independent browser jobs never replace one another."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        runs = Path(self.folder.name) / "runs"
        self.server = web_ui.Server((web_ui.HOST, 0), runs=runs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.http = http.client.HTTPConnection(
            web_ui.HOST, self.server.server_port, timeout=2
        )
        self.headers = {
            "Content-Type": "application/json",
            "X-TCS-Prover-Token": self.server.token,
        }

    def tearDown(self):
        self.http.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.folder.cleanup()

    def request(self, method, path, body=None):
        self.http.request(
            method, path, json.dumps(body or {}) if body is not None else None,
            self.headers,
        )
        response = self.http.getresponse()
        raw = response.read()
        return response, (
            json.loads(raw) if response.getheader("Content-Type") == "application/json"
            else raw.decode()
        )

    def test_two_jobs_run_independently_and_can_be_reopened(self):
        # Keep the test offline; only the local job coordinator is exercised.
        with mock.patch.object(web_ui.App, "_review", return_value=None):
            first_response, first = self.request(
                "POST", "/review", {"statement": "Problem one"}
            )
            second_response, second = self.request(
                "POST", "/review", {"statement": "Problem two"}
            )

        self.assertEqual(first_response.status, 200)
        self.assertEqual(second_response.status, 200)
        self.assertNotEqual(first["runId"], second["runId"])
        self.assertEqual(len(self.server.jobs), 2)

        response, listing = self.request("GET", "/jobs")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            {job["runId"] for job in listing["jobs"]},
            {first["runId"], second["runId"]},
        )

        response, reopened = self.request(
            "GET", f"/state?job={first['runId']}"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(reopened["draft"], "Problem one")
        self.assertEqual(
            self.server.jobs[second["runId"]].state["draft"], "Problem two"
        )

        response, page = self.request(
            "GET", f"/?job={first['runId']}"
        )
        self.assertEqual(response.status, 200)
        self.assertIn("TCS Prover", page)

        response, error = self.request(
            "POST", f"/delete-job?job={first['runId']}", {}
        )
        self.assertEqual(response.status, 400)
        self.assertIn("Stop this job", error["error"])

        first_app = self.server.jobs[first["runId"]]
        first_folder = first_app.run_dir
        first_app.state["phase"] = "done"
        response, deleted = self.request(
            "POST", f"/delete-job?job={first['runId']}", {}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(deleted["deleted"], first["runId"])
        self.assertNotIn(first["runId"], self.server.jobs)
        self.assertIn(second["runId"], self.server.jobs)
        self.assertFalse(first_folder.exists())
        self.assertTrue(
            (self.server.runs / ".trash" / first_folder.name).exists()
        )


if __name__ == "__main__":
    unittest.main()
