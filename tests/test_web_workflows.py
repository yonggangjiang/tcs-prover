"""Exercise the UI-to-workflow boundary without launching Codex."""

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ui import cli, review, server
import workflow_runner


class WebWorkflowTests(unittest.TestCase):
    def app(self, directory):
        return server.App(trace_file=Path(directory) / "transcript.jsonl")

    def test_defaults_offer_astra_for_every_node(self):
        state = server.empty_state()
        for role in ("review", "author", "critic", "writer"):
            self.assertEqual(state[f"{role}Model"], "gpt-6-astra")
        self.assertIn("gpt-6-astra", state["workflow"]["settings"]["models"])
        self.assertEqual(state["criticRounds"], 2)
        self.assertNotIn("algorithmic_presets", state["workflow"]["settings"])

    def test_solver_uses_both_workflows_and_plain_statement_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            app.state.update(criticRounds=3, thinkingHours=4)
            child = mock.Mock()
            with mock.patch.object(server.subprocess, "Popen", return_value=child) as popen:
                app._launch_solver_locked("A mathematical statement.")
            argv = popen.call_args.args[0]
            self.assertEqual(argv[2:5], [
                str(ROOT / "workflow_runner.py"),
                str(ROOT / "workflows/author_critic.yaml"),
                str(ROOT / "workflows/clean_up.yaml"),
            ])
            self.assertEqual(argv[argv.index("--critic-rounds") + 1], "3")
            self.assertEqual(argv[argv.index("--thinking-hours") + 1], "4")
            self.assertEqual(argv[argv.index("--author-model") + 1], "gpt-6-astra")
            child.stdin.write.assert_called_once_with("A mathematical statement.")
            child.stdin.close.assert_called_once()
            self.assertEqual(popen.call_args.kwargs["cwd"], Path(directory))
            self.assertEqual(json.loads((Path(directory) / server.AUTHOR_LIMIT_FILENAME).read_text()), {"hours": 4})

    def test_latex_only_uses_cleanup_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            child = mock.Mock()
            with mock.patch.object(server.subprocess, "Popen", return_value=child) as popen:
                app._launch_final_locked("Theorem and proof.")
            argv = popen.call_args.args[0]
            self.assertEqual(argv[2:4], [
                str(ROOT / "workflow_runner.py"),
                str(ROOT / "workflows/clean_up.yaml"),
            ])
            self.assertNotIn(str(ROOT / "workflows/author_critic.yaml"), argv)
            child.stdin.write.assert_called_once_with("Theorem and proof.")

    def test_failed_proof_keeps_summary_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            token = app.active_token = object()
            app.state.update(phase="running", stage="solve")
            child = mock.Mock()
            child.stdout = io.StringIO(json.dumps({
                "kind": "failure_result", "stage": "failure",
                "output": "Partial progress and remaining obstacles.",
            }) + "\n")
            # Even a legacy worker exiting with zero must remain a failure.
            child.wait.return_value = 0
            app._read_output(child, token)
            state = app.snapshot()
            self.assertEqual(state["phase"], "done")
            self.assertIn("Proof incomplete", state["error"])
            self.assertEqual((Path(directory) / "failure-summary.md").read_text(), state["output"])
            self.assertFalse((Path(directory) / "final.tex").exists())

    def test_fractional_critic_rounds_are_rejected(self):
        for value in (2.9, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.App._workflow_options(critic_rounds=value)

    def test_review_rejects_reserved_feedback_delimiter(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            for statement, feedback in (("Claim.\0Hidden.", ""), ("Claim.", "Fix.\0Hidden.")):
                with self.subTest(statement=statement, feedback=feedback), self.assertRaisesRegex(ValueError, "NUL"):
                    app.start_review(statement, feedback)

    def test_review_uses_local_prompt_and_schema(self):
        expected = {"statement": "Checked claim.", "notes": "No concerns."}
        with mock.patch.object(workflow_runner, "structured", return_value=(expected, json.dumps(expected))) as structured, mock.patch.object(workflow_runner, "emit"):
            self.assertEqual(review.review("Draft claim."), expected)
        self.assertEqual(structured.call_args.args, (
            review.review_prompt("Draft claim."), review.REVIEW_SCHEMA, "review",
        ))
        self.assertEqual(structured.call_args.kwargs["model"], "gpt-6-astra")

    def test_review_worker_preserves_revision_feedback(self):
        with mock.patch.object(workflow_runner, "configure_standard_streams"), mock.patch.object(review.sys, "stdin", io.StringIO("Claim.\0Fix wording.")), mock.patch.object(review, "review") as check:
            self.assertEqual(review.review_worker_main([]), 0)
        check.assert_called_once_with(
            "Claim.", "Fix wording.", "gpt-6-astra", "ultra",
            review.REVIEW_PROMPT, speed=server.DEFAULT_SPEED,
            summary=workflow_runner.DEFAULT_REASONING_SUMMARY,
        )

    def test_review_subprocess_runs_web_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory)
            token = app.active_token = object()
            child = mock.Mock()
            child.stdout = io.StringIO(json.dumps({
                "kind": "review_result", "stage": "review",
                "review": {"statement": "Checked.", "notes": "Fine."},
            }) + "\n")
            child.wait.return_value = 0
            child.poll.return_value = 0
            with mock.patch.object(server.subprocess, "Popen", return_value=child) as popen:
                app._review("Draft.", "Feedback.", token=token)
            argv = popen.call_args.args[0]
            self.assertEqual(argv[2:4], [str(ROOT / "web_ui.py"), "--review-worker"])
            child.stdin.write.assert_called_once_with("Draft.\0Feedback.")
            self.assertEqual(app.snapshot()["phase"], "reviewed")


class UIEntrypointTests(unittest.TestCase):
    def test_root_launcher_and_review_worker_load_outside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            for options in (["--help"], ["--review-worker", "--help"]):
                with self.subTest(options=options):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "web_ui.py"), *options],
                        cwd=directory, capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("gpt-6-astra", result.stdout)

    def test_moved_server_serves_assets_and_preserves_state_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            http_server = server.Server((server.HOST, 0), runs=directory)
            thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                server.HOST, http_server.server_port, timeout=5,
            )
            try:
                for route, filename in (
                    ("/", "index.html"), ("/app.js", "app.js"),
                    ("/styles.css", "styles.css"),
                ):
                    with self.subTest(route=route):
                        connection.request("GET", route)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), (ROOT / "ui" / filename).read_bytes())
                connection.request("GET", "/state")
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.request(
                    "GET", "/state",
                    headers={"X-TCS-Prover-Token": http_server.token},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                state = json.loads(response.read())
                self.assertEqual(state["phase"], "input")
                self.assertEqual(state["criticRounds"], 2)
                self.assertEqual(state["reviewModel"], "gpt-6-astra")
            finally:
                connection.close()
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=5)


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = os.getcwd()
        os.chdir(directory.name)
        self.addCleanup(os.chdir, previous)

    def test_cleanup_workflows_feed_polished_source_forward(self):
        cleanup = ROOT / "workflows/clean_up.yaml"
        outputs = [
            "Theorem and proof polished", "Theorem and proof polished polished",
        ]
        responses = [({"latex": output}, json.dumps({"latex": output})) for output in outputs]
        with mock.patch.object(workflow_runner, "structured", side_effect=responses) as structured, mock.patch.object(workflow_runner, "emit") as emit:
            state = workflow_runner.execute_workflows(
                [cleanup, cleanup], {"source": "Theorem and proof"},
            )
        self.assertEqual(structured.call_count, 2)
        for call, source in zip(structured.call_args_list, ["Theorem and proof", outputs[0]]):
            self.assertTrue(call.args[0].endswith(f"THEOREM AND PROOF TO POLISH:\n{source}"))
        self.assertEqual(state["output"], outputs[-1])
        self.assertEqual(state["source"], outputs[-1])
        self.assertTrue(any(
            call.args == ("final_result", "final") and call.kwargs.get("output") == outputs[-1]
            for call in emit.call_args_list
        ))

    def test_cleanup_workflows_feed_polished_solution_forward(self):
        cleanup = ROOT / "workflows/clean_up.yaml"
        outputs = ["Accepted proof polished", "Accepted proof polished polished"]
        responses = [({"latex": output}, json.dumps({"latex": output})) for output in outputs]
        with mock.patch.object(workflow_runner, "structured", side_effect=responses) as structured, mock.patch.object(workflow_runner, "emit"):
            state = workflow_runner.execute_workflows(
                [cleanup, cleanup], {"statement": "Theorem", "solution": "Accepted proof"},
            )
        self.assertEqual(structured.call_count, 2)
        for call, solution in zip(structured.call_args_list, ["Accepted proof", outputs[0]]):
            self.assertTrue(call.args[0].endswith(f"STATEMENT:\nTheorem\n\nLATEST SOLUTION:\n{solution}"))
        self.assertEqual(state["output"], outputs[-1])
        self.assertEqual(state["solution"], outputs[-1])

    def test_runner_cli_returns_failure_and_skips_cleanup(self):
        session_closed = []

        def failed_author(*args, **kwargs):
            try:
                workflow_runner.emit("failure_result", "failure", output="Unfinished proof.")
                yield {"outcome": "failure", "output": "Unfinished proof."}
            finally:
                session_closed.append(True)

        output = io.StringIO()
        with mock.patch.object(workflow_runner, "configure_standard_streams"), mock.patch.object(workflow_runner, "author_session", side_effect=failed_author), mock.patch.object(workflow_runner, "structured") as structured, mock.patch.object(sys, "stdin", io.StringIO("Claim.")), mock.patch.object(sys, "stdout", output):
            code = workflow_runner.main([
                str(ROOT / "workflows/author_critic.yaml"),
                str(ROOT / "workflows/clean_up.yaml"),
            ])
        self.assertEqual(code, 1)
        self.assertEqual(session_closed, [True])
        structured.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["kind"], "failure_result")

    def test_markdown_file_runs_through_workflow_child_and_saves_final(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            statement = folder / "claim.md"
            statement.write_text("A theorem.", encoding="utf-8")
            child = mock.Mock()
            child.stdout = io.StringIO(json.dumps({
                "kind": "final_result", "stage": "final", "output": "FINAL LATEX",
            }) + "\n")
            child.wait.return_value = 0
            with mock.patch.object(server.subprocess, "Popen", return_value=child) as popen:
                code = cli.run_headless_markdown(
                    statement, runs=folder / "runs", output_stream=io.StringIO(), error_stream=io.StringIO(),
                )
            self.assertEqual(code, 0)
            self.assertIn(str(ROOT / "workflows/author_critic.yaml"), popen.call_args.args[0])
            self.assertIn(str(ROOT / "workflows/clean_up.yaml"), popen.call_args.args[0])
            run = popen.call_args.kwargs["cwd"]
            self.assertEqual((run / "final.tex").read_text(), "FINAL LATEX")
            child.stdin.write.assert_called_once_with("A theorem.")

    def test_markdown_folder_isolates_runs_and_reports_any_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            inputs = folder / "inputs"
            inputs.mkdir()
            (inputs / "a.md").write_text("First theorem.", encoding="utf-8")
            (inputs / "b.md").write_text("Second theorem.", encoding="utf-8")
            (inputs / "notes.txt").write_text("Ignore me.", encoding="utf-8")
            children = []
            for kind, stage, output, code in (
                ("final_result", "final", "FINAL LATEX", 0),
                ("failure_result", "failure", "UNFINISHED", 1),
            ):
                child = mock.Mock()
                child.stdout = io.StringIO(json.dumps({
                    "kind": kind, "stage": stage, "output": output,
                }) + "\n")
                child.wait.return_value = code
                children.append(child)
            errors = io.StringIO()
            with mock.patch.object(server.subprocess, "Popen", side_effect=children) as popen:
                code = cli.run_headless_markdown(
                    inputs, runs=folder / "runs", output_stream=io.StringIO(), error_stream=errors,
                )
            self.assertEqual(code, 1)
            self.assertEqual(popen.call_count, 2)
            run_a, run_b = [call.kwargs["cwd"] for call in popen.call_args_list]
            self.assertNotEqual(run_a, run_b)
            self.assertEqual((run_a / "final.tex").read_text(), "FINAL LATEX")
            self.assertEqual((run_b / "failure-summary.md").read_text(), "UNFINISHED")
            self.assertIn("[b.md] Proof failed", errors.getvalue())
            children[0].stdin.write.assert_called_once_with("First theorem.")
            children[1].stdin.write.assert_called_once_with("Second theorem.")


if __name__ == "__main__":
    unittest.main()
