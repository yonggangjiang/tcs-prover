import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import workflow_runner as runner
from ui import cli, server as web_ui


class WebUiFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = os.getcwd()
        os.chdir(directory.name)
        self.addCleanup(os.chdir, previous)

    @staticmethod
    def save_valid_audits(run, statement, solution, verdicts):
        reports = [
            (
                {
                    "focus": runner.CRITIC_AUDIT_FOCI[index],
                    "verdict": verdict,
                    "report": f"audit {index + 1} report",
                }
                if verdict is not None else None
            )
            for index, verdict in enumerate(verdicts)
        ]
        instructions = web_ui.DEFAULT_PROMPTS["critic"]
        if runner.CRITIC_MEMORY_PROMPT not in instructions:
            instructions += "\n\n" + runner.CRITIC_MEMORY_PROMPT
        runner.save_critic_audit_checkpoint(
            reports, statement, solution, web_ui.DEFAULT_CRITIC_MODEL,
            web_ui.DEFAULT_REASONING_EFFORT, instructions, directory=run,
        )
        return reports

    def test_extracts_provider_failure_from_codex_event(self):
        self.assertEqual(
            web_ui.App.failure_text({
                "kind": "codex_event",
                "event": {
                    "type": "turn.failed",
                    "error": {"message": "401 Unauthorized"},
                },
            }),
            "401 Unauthorized",
        )

    def test_extracts_tagged_controller_diagnostic(self):
        self.assertEqual(
            web_ui.App.failure_text({
                "kind": "diagnostic",
                "text": "error: Codex review failed.",
            }),
            "Codex review failed.",
        )

    def test_stop_is_preserved_in_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "transcript.jsonl"
            app = web_ui.App(trace_file=trace)
            app.state.update(
                phase="running", stage="solve", activeNode="author",
            )
            app.process = mock.Mock()
            app.active_token = object()

            with mock.patch.object(web_ui, "stop_process_tree") as stop:
                app.stop()

            stop.assert_called_once()
            self.assertEqual(app.state["phase"], "done")
            self.assertEqual(app.state["error"], "Stopped.")
            self.assertTrue(app.state["manuallyStopped"])
            self.assertEqual(app.state["stoppedStage"], "solve")
            self.assertTrue(app.state["finishedAt"])
            self.assertEqual(app.state["trace"][-1]["label"], "Stop requested")
            self.assertIn("being stopped", trace.read_text(encoding="utf-8"))
            marker = json.loads(
                (Path(directory) / web_ui.MANUAL_STOP_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["stage"], "solve")

    def test_live_instruction_is_queued_only_for_a_running_author(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "transcript.jsonl"
            app = web_ui.App(trace_file=trace)
            app.process = mock.Mock()
            app.process.poll.return_value = None

            with self.assertRaisesRegex(ValueError, "only be sent"):
                app.steer_author("Stop experiments.")

            app.state.update(
                phase="running", stage="solve", activeNode="author",
            )
            command_id = app.steer_author("Stop experiments.")
            payload = json.loads(
                (Path(directory) / web_ui.AUTHOR_STEER_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["id"], command_id)
            self.assertEqual(payload["instruction"], "Stop experiments.")
            self.assertEqual(
                app.state["trace"][-1]["label"], "Live instruction queued"
            )

    def test_restored_author_instructions_do_not_duplicate_on_next_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            records = [
                {
                    "kind": "status", "stage": "solve", "node": "author",
                    "label": "Restored live instructions queued",
                    "text": "FIRST\n\nSECOND", "steerId": "replay-1",
                    "restoredInstructions": ["FIRST", "SECOND"],
                },
                {
                    "kind": "status", "stage": "solve", "node": "author",
                    "label": "Live author instruction sent",
                    "text": "FIRST\n\nSECOND", "steerId": "replay-1",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            self.assertEqual(
                web_ui.saved_author_instructions(run), ["FIRST", "SECOND"]
            )

    def test_saved_author_instruction_accepts_the_exact_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            instruction = "x" * runner.AUTHOR_STEER_MAX_CHARS
            (run / "transcript.jsonl").write_text(
                json.dumps({
                    "kind": "status", "stage": "solve", "node": "author",
                    "label": "Live instruction queued", "text": instruction,
                }) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                web_ui.saved_author_instructions(run), [instruction]
            )

    def test_complete_final_result_wins_a_live_stop_race(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "transcript.jsonl"
            app = web_ui.App(trace_file=trace)
            process, token = mock.Mock(), object()
            app.process = process
            app.active_token = token
            app.worker_token = token
            app.state.update(
                phase="running", stage="final", activeNode="final",
            )

            def output_then_stop():
                yield json.dumps({
                    "kind": "final_result", "stage": "final",
                    "node": "final", "output": "COMPLETE LATEX",
                }) + "\n"
                app.stop()

            process.stdout = output_then_stop()
            process.wait.return_value = -15
            with mock.patch.object(web_ui, "stop_process_tree"):
                app._read_output(process, token)

            self.assertEqual(app.state["phase"], "done")
            self.assertEqual(app.state["error"], "")
            self.assertFalse(app.state["manuallyStopped"])
            self.assertEqual(app.state["output"], "COMPLETE LATEX")
            self.assertFalse((Path(directory) / web_ui.MANUAL_STOP_FILENAME).exists())

    def test_buffered_final_result_keeps_continue_hidden_until_reader_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "transcript.jsonl"
            app = web_ui.App(trace_file=trace)
            process, token = mock.Mock(), object()
            ready = web_ui.threading.Event()
            release = web_ui.threading.Event()
            app.process = process
            app.active_token = token
            app.worker_token = token
            app.state.update(
                phase="running", stage="final", activeNode="latex_editor",
                finalInputReady=True,
            )

            def buffered_output():
                ready.set()
                release.wait(2)
                yield json.dumps({
                    "kind": "final_result", "stage": "final",
                    "node": "final", "output": "BUFFERED COMPLETE LATEX",
                }) + "\n"

            process.stdout = buffered_output()
            process.wait.return_value = -15
            reader = web_ui.threading.Thread(
                target=app._read_output, args=(process, token)
            )
            with mock.patch.object(web_ui, "stop_process_tree"):
                reader.start()
                self.assertTrue(ready.wait(2))
                app.stop()
                self.assertEqual(app.state["phase"], "stopping")
                self.assertIsNone(
                    web_ui.Server._stopped_continuation_plan(app)
                )
                release.set()
                reader.join(2)

            self.assertFalse(reader.is_alive())
            self.assertEqual(app.state["phase"], "done")
            self.assertEqual(app.state["error"], "")
            self.assertFalse(app.state["manuallyStopped"])
            self.assertFalse(app.has_active_worker())
            self.assertEqual(app.state["output"], "BUFFERED COMPLETE LATEX")

    def test_buffered_review_result_keeps_continue_hidden_until_reader_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "transcript.jsonl"
            app = web_ui.App(trace_file=trace)
            process, token = mock.Mock(), object()
            ready = web_ui.threading.Event()
            release = web_ui.threading.Event()
            app.active_token = token
            app.state.update(
                phase="reviewing", stage="review",
                activeNode="statement_reviewer",
            )

            def buffered_output():
                ready.set()
                release.wait(2)
                yield json.dumps({
                    "kind": "review_result", "stage": "review",
                    "review": {"statement": "CHECKED", "notes": ""},
                }) + "\n"

            process.stdout = buffered_output()
            process.wait.return_value = -15
            process.poll.return_value = -15
            with mock.patch.object(
                web_ui.subprocess, "Popen", return_value=process
            ), mock.patch.object(web_ui, "stop_process_tree"):
                reader = app._spawn_worker(
                    app._review,
                    ("DRAFT", "", web_ui.DEFAULT_REVIEW_MODEL,
                     web_ui.DEFAULT_REVIEW_EFFORT, token),
                    token,
                )
                self.assertTrue(ready.wait(2))
                app.stop()
                self.assertEqual(app.state["phase"], "stopping")
                self.assertIsNone(
                    web_ui.Server._stopped_continuation_plan(app)
                )
                release.set()
                reader.join(2)

            self.assertFalse(reader.is_alive())
            self.assertEqual(app.state["phase"], "reviewed")
            self.assertEqual(app.state["error"], "")
            self.assertFalse(app.state["manuallyStopped"])
            self.assertFalse(app.has_active_worker())
            self.assertEqual(app.state["review"]["statement"], "CHECKED")

    def test_restore_and_continue_reject_run_directory_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, external = root / "runs", root / "external-run"
            runs.mkdir()
            external.mkdir()
            (external / "draft.md").write_text("DRAFT\n", encoding="utf-8")
            (external / "transcript.jsonl").write_text(
                json.dumps({
                    "kind": "status", "stage": "review",
                    "label": "Stop requested",
                }) + "\n",
                encoding="utf-8",
            )
            link = runs / "escape"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            self.assertEqual(web_ui.restore_saved_jobs(runs), {})

            escaped = web_ui.restore_saved_app(
                web_ui.App(link / "transcript.jsonl", runs)
            )
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = {"escape": escaped}
            server.jobs_lock = web_ui.threading.RLock()
            with self.assertRaisesRegex(ValueError, "outside the runs"):
                server.continue_stopped_job("escape")

    def test_continue_rejects_symbolic_link_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, run = root / "runs", root / "runs" / "stopped"
            runs.mkdir()
            run.mkdir()
            (run / "draft.md").write_text("DRAFT\n", encoding="utf-8")
            (run / "transcript.jsonl").write_text(
                json.dumps({
                    "kind": "status", "stage": "review",
                    "label": "Stop requested",
                }) + "\n",
                encoding="utf-8",
            )
            external = root / "external-memory.json"
            external.write_text("{}\n", encoding="utf-8")
            try:
                (run / runner.AUTHOR_MEMORY_FILENAME).symlink_to(external)
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()

            with self.assertRaisesRegex(ValueError, "symbolic-link artifact"):
                server.continue_stopped_job(run.name)

    def test_saved_critic_source_uses_complete_solution_and_checked_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "prompts").mkdir()
            (run / "SOLUTION.md").write_text(
                "COMPLETE PROOF\n", encoding="utf-8"
            )
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nEXACT STATEMENT\n\n"
                "# Reviewer notes\n\nnotes\n\n# Reviewer notes\n\nmore notes\n",
                encoding="utf-8",
            )
            (run / runner.AUTHOR_ANCHOR_FILENAME).write_text(
                "# Immutable author contract\n\n"
                "## Exact statement (verbatim)\n\nEXACT STATEMENT\n",
                encoding="utf-8",
            )
            (run / "prompts" / "critic.txt").write_text(
                "CRITIC PROMPT\n", encoding="utf-8"
            )
            (run / "prompts" / "author.txt").write_text(
                "AUTHOR [STATEMENT] PROMPT\n", encoding="utf-8"
            )
            (run / "prompts" / "final.txt").write_text(
                "FINAL PROMPT\n", encoding="utf-8"
            )

            source = web_ui.saved_critic_source(run)

            self.assertEqual(source["statement"], "EXACT STATEMENT")
            self.assertEqual(source["solution"], "COMPLETE PROOF\n")
            self.assertEqual(
                source["author_prompt"], "AUTHOR [STATEMENT] PROMPT\n"
            )
            self.assertEqual(source["critic_prompt"], "CRITIC PROMPT\n")
            self.assertEqual(source["audit_checkpoint"], "")

    def test_saved_critic_source_accepts_resume_candidate_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "saved-candidate.md").write_text(
                "SAVED CANDIDATE\n", encoding="utf-8"
            )
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nEXACT STATEMENT\n", encoding="utf-8"
            )
            checkpoint = '{"schemaVersion": 1}\n'
            (run / runner.CRITIC_AUDIT_CHECKPOINT_FILENAME).write_text(
                checkpoint, encoding="utf-8"
            )

            source = web_ui.saved_critic_source(run)

            self.assertEqual(source["solution"], "SAVED CANDIDATE\n")
            self.assertEqual(source["audit_checkpoint"], checkpoint)

            (
                run / runner.CRITIC_AUDIT_CHECKPOINT_FILENAME
            ).write_text("", encoding="utf-8")
            self.assertEqual(
                web_ui.saved_critic_source(run)["audit_checkpoint"], "",
            )
            self.assertEqual(
                [item["id"] for item in web_ui.saved_run_checkpoints(run)],
                ["candidate"],
            )

    def test_controller_candidate_takes_precedence_over_stale_solution_file(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "SOLUTION.md").write_text(
                "STALE AGENT FILE\n", encoding="utf-8"
            )
            (run / runner.SAVED_CANDIDATE_FILENAME).write_text(
                "CURRENT CONTROLLER CANDIDATE\n", encoding="utf-8"
            )
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )

            source = web_ui.saved_critic_source(run)

            self.assertEqual(
                source["solution"], "CURRENT CONTROLLER CANDIDATE\n",
            )

    def test_saved_critic_source_accepts_direct_multiline_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            statement = (
                "FIRST LINE\n\n# Statement review\n\n"
                "THIS HEADING IS PART OF THE STATEMENT\n\nSECOND LINE"
            )
            (run / "SOLUTION.md").write_text(
                "COMPLETE PROOF\n", encoding="utf-8"
            )
            (run / "checked-statement.md").write_text(
                "# Statement sent directly to the proof author\n\n"
                f"{statement}\n\n# Statement review\n\nSkipped by the user.\n",
                encoding="utf-8",
            )

            source = web_ui.saved_critic_source(run)

            self.assertEqual(source["statement"], statement)
            self.assertEqual(source["solution"], "COMPLETE PROOF\n")

    def test_resume_critic_command_opens_the_browser_job(self):
        class FakeApp:
            state = {"runId": "new-critic-job"}

        class FakeServer:
            origin = "http://127.0.0.1:8765"
            token = "secret"

            def __init__(self, _address):
                self.source = None

            def start_saved_critic_job(self, source, settings):
                self.source = source
                self.settings = settings
                return FakeApp()

            def serve_forever(self):
                return None

            def stop_all(self):
                return None

            def server_close(self):
                return None

        source = Path("/tmp/example-saved-run")
        fake_server = FakeServer(None)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", [
                "web_ui.py", "--resume-critic", str(source),
            ]))
            stack.enter_context(mock.patch.object(
                cli, "saved_critic_source",
                return_value={"run_dir": source},
            ))
            stack.enter_context(mock.patch.object(
                cli, "Server", return_value=fake_server
            ))
            open_browser = stack.enter_context(mock.patch.object(
                cli.webbrowser, "open"
            ))
            stack.enter_context(mock.patch.object(
                cli, "run_headless_critic_resume",
                side_effect=AssertionError("must use the browser UI"),
            ))
            result = cli.main()

        self.assertEqual(result, 0)
        self.assertEqual(fake_server.source, source)
        open_browser.assert_called_once_with(
            "http://127.0.0.1:8765/?job=new-critic-job#secret"
        )

    def test_critic_resume_keeps_the_full_workflow_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            app = web_ui.App(runs=Path(directory))
            process, token = mock.Mock(), object()
            with mock.patch.object(
                runner, "verify_model_credentials"
            ), mock.patch.object(
                app, "_launch_critic_resume_locked",
                return_value=(process, token),
            ), mock.patch.object(web_ui.threading, "Thread"):
                app.start_critic_resume(
                    "EXACT STATEMENT", "COMPLETE PROOF",
                    author_model="gpt-5.6-luna",
                    critic_model="gpt-5.6-luna",
                    writer_model="gpt-5.6-luna",
                    audit_checkpoint='{"schemaVersion": 1}\n',
                )

            self.assertIs(app.state["workflow"], web_ui.PUBLIC_GRAPH)
            self.assertEqual(
                set(app.state["workflow"]["nodes"]),
                {"statement_reviewer", "author", "critic",
                 "latex_editor", "failure_summary"},
            )
            self.assertTrue((app.run_dir / "prompts" / "author.txt").is_file())
            self.assertEqual(
                (app.run_dir / runner.CRITIC_AUDIT_CHECKPOINT_FILENAME)
                .read_text(encoding="utf-8"),
                '{"schemaVersion": 1}\n',
            )
            settings = json.loads(
                (app.run_dir / web_ui.JOB_SETTINGS_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(settings["criticModel"], "gpt-5.6-luna")
            self.assertEqual(settings["speedMode"], web_ui.DEFAULT_SPEED)

    def test_saved_run_checkpoints_include_failed_coordinator(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            (run / "saved-candidate.md").write_text(
                "PROOF\n", encoding="utf-8"
            )
            self.save_valid_audits(
                run, "STATEMENT", "PROOF", ["pass", "fail", "fail"],
            )
            records = [
                {
                    "kind": "request",
                    "label": "Critic coordinator adjudication",
                    "stage": "critic",
                    "time": "2026-08-26T21:18:35+00:00",
                },
                {
                    "kind": "diagnostic",
                    "stage": "critic",
                    "text": "error: coordinator timed out",
                    "time": "2026-08-26T22:18:35+00:00",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            checkpoints = web_ui.saved_run_checkpoints(run)

            self.assertEqual(
                [item["id"] for item in checkpoints],
                ["candidate", "independent-audits", "critic-coordinator"],
            )
            audits = checkpoints[1]
            self.assertEqual(audits["completedAuditCount"], 3)
            self.assertEqual(audits["failedAuditCount"], 2)
            self.assertEqual(audits["resumeLabel"], "Continue coordinator")
            coordinator = checkpoints[2]
            self.assertEqual(coordinator["status"], "failed")
            self.assertEqual(coordinator["resumeLabel"], "Retry coordinator")

            checkpoint_path = (
                run / runner.CRITIC_AUDIT_CHECKPOINT_FILENAME
            )
            invalid = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            invalid["assignmentSha256"] = "wrong-proof"
            checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
            self.assertEqual(
                [item["id"] for item in web_ui.saved_run_checkpoints(run)],
                ["candidate"],
            )

    def test_server_restores_historical_jobs_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run = runs / "2026-08-26_22-58-52_example"
            run.mkdir()
            (run / "draft.md").write_text(
                "# Draft problem\n\nA saved theorem\n", encoding="utf-8"
            )
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            (run / "saved-candidate.md").write_text(
                "PROOF\n", encoding="utf-8"
            )
            (run / web_ui.JOB_SETTINGS_FILENAME).write_text(
                json.dumps({
                    "criticModel": "deepseek/deepseek-v4-pro",
                    "criticEffort": "max",
                    "speedMode": "fast",
                }),
                encoding="utf-8",
            )
            (run / "transcript.jsonl").write_text(
                json.dumps({
                    "kind": "request",
                    "stage": "critic",
                    "label": "Independent critic audit 1",
                    "model": "deepseek-v4-pro",
                    "reasoningEffort": "high",
                    "serviceTier": "standard",
                    "time": "2026-08-26T22:58:52+00:00",
                }) + "\n",
                encoding="utf-8",
            )

            jobs_by_id = web_ui.restore_saved_jobs(runs)
            restored = jobs_by_id[run.name].snapshot()
            server = object.__new__(web_ui.Server)
            server.fixed_app = False
            server.jobs = jobs_by_id
            server.jobs_lock = web_ui.threading.RLock()
            jobs = server.job_list()

            self.assertEqual(restored["phase"], "done")
            self.assertEqual(restored["draft"], "A saved theorem")
            self.assertEqual(restored["criticModel"], "deepseek-v4-pro")
            self.assertEqual(restored["criticEffort"], "max")
            self.assertEqual(restored["speedMode"], "fast")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["runId"], run.name)
            self.assertEqual(
                [item["id"] for item in jobs[0]["checkpoints"]],
                ["candidate"],
            )
            with mock.patch.object(
                server, "start_saved_critic_job", return_value="next-job",
            ) as start:
                self.assertEqual(server.resume_critic_job(run.name), "next-job")
            resumed_settings = start.call_args.args[1]
            self.assertEqual(resumed_settings["criticEffort"], "max")
            self.assertEqual(resumed_settings["speedMode"], "fast")

    def test_legacy_mixed_provider_restore_keeps_fast_if_any_request_was_fast(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "legacy-job"
            run.mkdir()
            (run / "draft.md").write_text("A theorem\n", encoding="utf-8")
            records = [
                {
                    "kind": "request", "stage": "solve",
                    "model": "gpt-5.6-sol", "reasoningEffort": "ultra",
                    "serviceTier": "fast", "time": "2026-08-26T10:00:00+00:00",
                },
                {
                    "kind": "request", "stage": "critic",
                    "label": "Independent critic audit 1",
                    "model": "deepseek-v4-pro", "reasoningEffort": "high",
                    "serviceTier": "standard",
                    "time": "2026-08-26T10:01:00+00:00",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            restored = web_ui.restore_saved_jobs(run.parent)[run.name].snapshot()

            self.assertEqual(restored["speedMode"], "fast")
            self.assertEqual(restored["authorEffort"], "ultra")
            self.assertEqual(restored["criticEffort"], "ultra")

    def test_resume_checkpoint_delegates_to_saved_critic_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run = runs / "saved-job"
            run.mkdir()
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            (run / "saved-candidate.md").write_text(
                "PROOF\n", encoding="utf-8"
            )
            self.save_valid_audits(
                run, "STATEMENT", "PROOF", ["pass", None, None],
            )
            (run / "transcript.jsonl").write_text(
                json.dumps({
                    "kind": "request",
                    "stage": "solve",
                    "time": "2026-08-26T22:58:52+00:00",
                }) + "\n",
                encoding="utf-8",
            )
            server = object.__new__(web_ui.Server)
            server.fixed_app = False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()
            with mock.patch.object(
                server, "resume_critic_job", return_value="next-job"
            ) as resume:
                result = server.resume_checkpoint_job(
                    run.name, "candidate"
                )
                later_result = server.resume_checkpoint_job(
                    run.name, "independent-audits"
                )

            self.assertEqual(result, "next-job")
            self.assertEqual(later_result, "next-job")
            self.assertEqual(resume.call_args_list, [
                mock.call(run.name, include_audit_checkpoint=False),
                mock.call(run.name, include_audit_checkpoint=True),
            ])

    def test_saved_critic_job_can_restart_before_saved_audits(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            source = runs / "source"
            source.mkdir()
            (source / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            (source / "saved-candidate.md").write_text(
                "PROOF\n", encoding="utf-8"
            )
            checkpoint = json.dumps({
                "schemaVersion": 1,
                "reports": [{"verdict": "pass"}, None, None],
            })
            (source / runner.CRITIC_AUDIT_CHECKPOINT_FILENAME).write_text(
                checkpoint, encoding="utf-8"
            )
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.jobs = {}
            server.jobs_lock = web_ui.threading.RLock()

            with mock.patch.object(
                web_ui.App, "start_critic_resume"
            ) as start:
                server.start_saved_critic_job(
                    source, include_audit_checkpoint=False
                )
                fresh_audit = start.call_args.kwargs["audit_checkpoint"]
                fresh_recovery = start.call_args.kwargs[
                    "recover_audit_checkpoint"
                ]
                server.start_saved_critic_job(
                    source, include_audit_checkpoint=True
                )
                restored_audit = start.call_args.kwargs["audit_checkpoint"]
                restored_recovery = start.call_args.kwargs[
                    "recover_audit_checkpoint"
                ]

            self.assertEqual(fresh_audit, "")
            self.assertFalse(fresh_recovery)
            self.assertEqual(restored_audit, checkpoint)
            self.assertTrue(restored_recovery)

    def test_active_snapshot_does_not_rescan_growing_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            transcript = run / "transcript.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            app = web_ui.App(trace_file=transcript)
            app.state.update(phase="running", stage="solve")

            with mock.patch.object(
                web_ui, "saved_run_checkpoints", return_value=[],
            ) as checkpoints:
                app.snapshot()
                transcript.write_text("{}\n{}\n", encoding="utf-8")
                app.snapshot()
                self.assertEqual(checkpoints.call_count, 1)

                app.state["phase"] = "done"
                app.snapshot()
                self.assertEqual(checkpoints.call_count, 2)

    def test_restore_preserves_review_only_and_waiting_approval_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            review_only = runs / "review-only"
            review_only.mkdir()
            (review_only / "prompts").mkdir()
            (review_only / "prompts" / "review.txt").write_text(
                "REVIEW PROMPT\n", encoding="utf-8"
            )
            (review_only / "draft.md").write_text(
                "# Draft problem\n\nDRAFT\n", encoding="utf-8"
            )
            review_record = {
                "kind": "review_result", "stage": "review",
                "review": {"statement": "CHECKED", "notes": "NOTES"},
                "time": "2026-08-26T10:00:00+00:00",
            }
            (review_only / "transcript.jsonl").write_text(
                json.dumps(review_record) + "\n", encoding="utf-8"
            )

            awaiting = runs / "awaiting-approval"
            awaiting.mkdir()
            (awaiting / "prompts").mkdir()
            for name in ("review", "author", "critic", "final"):
                (awaiting / "prompts" / f"{name}.txt").write_text(
                    web_ui.DEFAULT_PROMPTS[name] + "\n", encoding="utf-8"
                )
            (awaiting / "draft.md").write_text(
                "# Draft problem\n\nDRAFT\n", encoding="utf-8"
            )
            (awaiting / "checked-statement.md").write_text(
                "# Checked statement\n\nCHECKED\n\n# Reviewer notes\n\nNOTES\n",
                encoding="utf-8",
            )
            (awaiting / "transcript.jsonl").write_text(
                json.dumps(review_record) + "\n", encoding="utf-8"
            )

            restored = web_ui.restore_saved_jobs(runs)
            review_state = restored[review_only.name].snapshot()
            approval_app = restored[awaiting.name]
            approval_state = approval_app.snapshot()

            self.assertTrue(review_state["statementReviewOnly"])
            self.assertIs(review_state["workflow"], web_ui.REVIEW_ONLY_GRAPH)
            self.assertEqual(review_state["phase"], "done")
            self.assertEqual(review_state["output"], "CHECKED")
            self.assertEqual(approval_state["phase"], "reviewed")
            self.assertFalse(approval_state["skipStatementReview"])
            self.assertIs(approval_state["workflow"], web_ui.PUBLIC_GRAPH)
            process, token = mock.Mock(), object()
            with mock.patch.object(
                approval_app, "_launch_solver_locked",
                return_value=(process, token),
            ) as launch, mock.patch.object(web_ui.threading, "Thread"):
                approval_app.approve()
            launch.assert_called_once_with("CHECKED")

    def test_restore_infers_direct_mode_and_persists_changed_time_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            transcript = run / "transcript.jsonl"
            transcript.write_text("", encoding="utf-8")
            (run / "checked-statement.md").write_text(
                "# Statement sent directly to the proof author\n\nSTATEMENT\n\n"
                "# Statement review\n\nSkipped by the user.\n",
                encoding="utf-8",
            )
            app = web_ui.App(trace_file=transcript)
            app.state.update(
                phase="running", stage="solve", activeNode="author",
                thinkingHours=168, problemMode="statement",
                skipStatementReview=True, statementReviewOnly=False,
            )
            app.process = mock.Mock()
            app.process.poll.return_value = None

            self.assertEqual(app.set_author_time_limit(36), 36)
            restored = web_ui.restore_saved_app(
                web_ui.App(trace_file=transcript)
            ).snapshot()

            self.assertEqual(restored["thinkingHours"], 36)
            self.assertTrue(restored["skipStatementReview"])
            self.assertIs(restored["workflow"], web_ui.DIRECT_GRAPH)

    def test_legacy_algorithmic_job_continues_from_its_saved_exact_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            source = runs / "legacy-algorithmic"
            source.mkdir()
            (source / "draft.md").write_text("Old preset title\n", encoding="utf-8")
            (source / "checked-statement.md").write_text(
                "# Checked statement\n\nEXACT COMPOSED CLAIM\n", encoding="utf-8",
            )
            (source / web_ui.JOB_SETTINGS_FILENAME).write_text(json.dumps({
                "problemMode": "algorithmic", "skipStatementReview": False,
            }), encoding="utf-8")
            records = [
                {"kind": "request", "stage": "solve"},
                {"kind": "status", "stage": "solve", "node": "author", "label": "Stop requested"},
            ]
            (source / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
            )
            server = object.__new__(web_ui.Server)
            server.runs, server.fixed_app = runs, False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()
            state = server.jobs[source.name].snapshot()
            self.assertEqual(state["problemMode"], "statement")
            self.assertTrue(state["skipStatementReview"])
            self.assertIs(state["workflow"], web_ui.DIRECT_GRAPH)
            before = {path.name: path.read_bytes() for path in source.iterdir()}
            with (
                mock.patch.object(runner, "verify_model_credentials"),
                mock.patch.object(
                    web_ui.App, "_launch_solver_locked", return_value=(mock.Mock(), object()),
                ) as launch,
                mock.patch.object(web_ui.threading, "Thread"),
            ):
                continued = server.continue_stopped_job(source.name)
            launch.assert_called_once_with("EXACT COMPOSED CLAIM")
            self.assertEqual(continued.state["problemMode"], "statement")
            self.assertTrue(continued.state["skipStatementReview"])
            self.assertEqual({path.name: path.read_bytes() for path in source.iterdir()}, before)

    def test_legacy_manual_stop_is_restored_and_exposed_to_the_home_page(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run = runs / "legacy-stopped-review"
            run.mkdir()
            (run / "draft.md").write_text(
                "# Draft problem\n\nDRAFT STATEMENT\n", encoding="utf-8"
            )
            records = [
                {
                    "kind": "request", "stage": "review",
                    "model": web_ui.DEFAULT_REVIEW_MODEL,
                    "reasoningEffort": web_ui.DEFAULT_REVIEW_EFFORT,
                    "serviceTier": web_ui.DEFAULT_SPEED,
                    "time": "2026-08-26T10:00:00+00:00",
                },
                {
                    "kind": "status", "stage": "review",
                    "node": "statement_reviewer", "label": "Stop requested",
                    "time": "2026-08-26T10:01:00+00:00",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            jobs = web_ui.restore_saved_jobs(runs)
            restored = jobs[run.name].snapshot()
            server = object.__new__(web_ui.Server)
            server.fixed_app = False
            server.jobs = jobs
            server.jobs_lock = web_ui.threading.RLock()
            listed = server.job_list()[0]

            self.assertEqual(restored["phase"], "done")
            self.assertEqual(restored["error"], "Stopped.")
            self.assertTrue(restored["manuallyStopped"])
            self.assertEqual(restored["stoppedStage"], "review")
            self.assertTrue(listed["canContinueStopped"])
            self.assertEqual(
                listed["continueStoppedLabel"], "Retry statement review"
            )
            self.assertIn("current defaults", listed["settingsWarning"])

    def test_completed_review_wins_a_legacy_stop_race(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "review-race"
            run.mkdir()
            (run / "draft.md").write_text("DRAFT\n", encoding="utf-8")
            records = [
                {
                    "kind": "request", "stage": "review",
                    "time": "2026-08-26T09:59:00+00:00",
                },
                {
                    "kind": "review_result", "stage": "review",
                    "review": {"statement": "CHECKED", "notes": ""},
                    "time": "2026-08-26T10:00:00+00:00",
                },
                {
                    "kind": "status", "stage": "review",
                    "label": "Stop requested",
                    "time": "2026-08-26T10:00:01+00:00",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            restored = web_ui.restore_saved_jobs(run.parent)[run.name].snapshot()

            self.assertFalse(restored["manuallyStopped"])
            self.assertEqual(restored["phase"], "reviewed")
            self.assertEqual(restored["review"]["statement"], "CHECKED")

    def test_completed_review_wins_a_durable_marker_stop_race(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "review-marker-race"
            run.mkdir()
            (run / "draft.md").write_text("DRAFT\n", encoding="utf-8")
            (run / web_ui.MANUAL_STOP_FILENAME).write_text(
                json.dumps({
                    "schemaVersion": 1, "stage": "review",
                    "node": "statement_reviewer", "problemMode": "statement",
                    "stoppedAt": "2026-08-26T10:00:01+00:00",
                }),
                encoding="utf-8",
            )
            records = [
                {"kind": "request", "stage": "review"},
                {
                    "kind": "review_result", "stage": "review",
                    "review": {"statement": "CHECKED", "notes": ""},
                },
                {
                    "kind": "status", "stage": "review",
                    "label": "Stop requested",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            restored = web_ui.restore_saved_jobs(run.parent)[run.name].snapshot()

            self.assertFalse(restored["manuallyStopped"])
            self.assertEqual(restored["phase"], "reviewed")
            self.assertEqual(restored["review"]["statement"], "CHECKED")

    def test_stopped_feedback_retry_is_not_hidden_by_an_earlier_review(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "stopped-review-retry"
            run.mkdir()
            (run / "draft.md").write_text("ORIGINAL\n", encoding="utf-8")
            (run / web_ui.MANUAL_STOP_FILENAME).write_text(
                json.dumps({
                    "schemaVersion": 1, "stage": "review",
                    "node": "statement_reviewer", "problemMode": "statement",
                    "stoppedAt": "2026-08-26T10:01:00+00:00",
                }),
                encoding="utf-8",
            )
            records = [
                {"kind": "request", "stage": "review"},
                {
                    "kind": "review_result", "stage": "review",
                    "review": {"statement": "FIRST CHECKED", "notes": ""},
                },
                {"kind": "request", "stage": "review"},
                {
                    "kind": "status", "stage": "review",
                    "label": "Stop requested",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            restored = web_ui.restore_saved_jobs(run.parent)[run.name].snapshot()

            self.assertTrue(restored["manuallyStopped"])
            self.assertEqual(restored["stoppedStage"], "review")
            self.assertFalse(restored["reviewInputRecorded"])

    def test_legacy_final_continuation_plan_does_not_reparse_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run = runs / "legacy-stopped-final"
            run.mkdir()
            (run / "draft.md").write_text("STATEMENT\n", encoding="utf-8")
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            records = [
                {"kind": "request", "stage": "critic"},
                {
                    "kind": "critic_result", "stage": "critic",
                    "report": {
                        "verdict": "pass", "fixed": False,
                        "solution": "CLEAN PROOF",
                    },
                },
                {"kind": "request", "stage": "final"},
                {
                    "kind": "status", "stage": "final",
                    "label": "Stop requested",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            jobs = web_ui.restore_saved_jobs(runs)
            self.assertTrue(jobs[run.name].state["finalInputReady"])
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = jobs
            server.jobs_lock = web_ui.threading.RLock()

            with mock.patch.object(
                web_ui, "_run_records",
                side_effect=AssertionError("home polling reparsed transcript"),
            ):
                first = server.job_list()[0]
                second = server.job_list()[0]

            self.assertTrue(first["canContinueStopped"])
            self.assertEqual(first["continueStoppedLabel"], "Retry LaTeX editor")
            self.assertEqual(second["continueStoppedLabel"], "Retry LaTeX editor")

    def test_continuing_stopped_review_preserves_settings_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            source = runs / "stopped-review"
            source.mkdir()
            (source / "prompts").mkdir()
            (source / "draft.md").write_text(
                "# Draft problem\n\nDRAFT\n", encoding="utf-8"
            )
            (source / web_ui.REVIEW_INPUT_FILENAME).write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "statement": "RETRY STATEMENT",
                    "feedback": "Keep the quantifier literal.",
                }),
                encoding="utf-8",
            )
            for name in ("review", "author", "critic", "final"):
                (source / "prompts" / f"{name}.txt").write_text(
                    web_ui.DEFAULT_PROMPTS[name] + "\n", encoding="utf-8"
                )
            (source / web_ui.JOB_SETTINGS_FILENAME).write_text(
                json.dumps({
                    "reviewModel": "gpt-5.6-luna",
                    "reviewEffort": "xhigh",
                    "speedMode": "fast",
                    "reasoningSummary": "detailed",
                    "problemMode": "statement",
                    "statementReviewOnly": False,
                }),
                encoding="utf-8",
            )
            records = [
                {
                    "kind": "request", "stage": "review",
                    "time": "2026-08-26T10:00:00+00:00",
                },
                {
                    "kind": "status", "stage": "review",
                    "label": "Stop requested",
                    "time": "2026-08-26T10:01:00+00:00",
                },
            ]
            (source / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()
            before = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }

            with mock.patch.object(
                runner, "verify_model_credentials"
            ), mock.patch.object(web_ui.threading, "Thread"):
                continued = server.continue_stopped_job(source.name)

            self.assertNotEqual(continued.run_dir, source)
            self.assertEqual(continued.state["phase"], "reviewing")
            self.assertEqual(continued.state["reviewModel"], "gpt-5.6-luna")
            self.assertEqual(continued.state["reviewEffort"], "xhigh")
            self.assertEqual(continued.state["speedMode"], "fast")
            self.assertEqual(continued.state["draft"], "RETRY STATEMENT")
            self.assertEqual(
                continued.state["reviewFeedback"],
                "Keep the quantifier literal.",
            )
            provenance = json.loads(
                (continued.run_dir / web_ui.CONTINUATION_SOURCE_FILENAME)
                .read_text(encoding="utf-8")
            )
            self.assertEqual(Path(provenance["sourceRun"]), source.resolve())
            self.assertEqual(provenance["stoppedStage"], "review")
            after = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)

    def test_continuing_stopped_author_copies_compatible_durable_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            source = runs / "stopped-author"
            source.mkdir()
            (source / "prompts").mkdir()
            statement = "EXACT STATEMENT"
            for name in ("author", "critic", "final"):
                (source / "prompts" / f"{name}.txt").write_text(
                    web_ui.DEFAULT_PROMPTS[name] + "\n", encoding="utf-8"
                )
            (source / "draft.md").write_text(
                f"# Draft problem\n\n{statement}\n", encoding="utf-8"
            )
            (source / "checked-statement.md").write_text(
                f"# Checked statement\n\n{statement}\n", encoding="utf-8"
            )
            original_prompt = runner.make_prompt(
                statement, web_ui.DEFAULT_PROMPTS["author"]
            )
            memory = runner.AuthorMemory(
                source, original_prompt, statement
            )
            memory.record_candidate(
                "INCOMPLETE ATTEMPT", "initial_author", status="working"
            )
            records = [
                {
                    "kind": "request", "stage": "solve",
                    "time": "2026-08-26T10:00:00+00:00",
                },
                {
                    "kind": "status", "stage": "solve", "node": "author",
                    "label": "Live instruction queued",
                    "text": "Do not run more experiments or write code.",
                    "steerId": "instruction-1",
                    "time": "2026-08-26T10:00:30+00:00",
                },
                {
                    "kind": "status", "stage": "solve", "node": "author",
                    "label": "Stop requested",
                    "time": "2026-08-26T10:01:00+00:00",
                },
            ]
            (source / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()
            before = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            process, token = mock.Mock(), object()

            with mock.patch.object(
                runner, "verify_model_credentials"
            ), mock.patch.object(
                web_ui.App, "_launch_solver_locked",
                return_value=(process, token),
            ), mock.patch.object(web_ui.threading, "Thread"):
                continued = server.continue_stopped_job(source.name)

            copied = (
                continued.run_dir / runner.AUTHOR_MEMORY_FILENAME
            ).read_text(encoding="utf-8")
            self.assertEqual(
                copied,
                (source / runner.AUTHOR_MEMORY_FILENAME).read_text(
                    encoding="utf-8"
                ),
            )
            resumed_memory = runner.AuthorMemory(
                continued.run_dir, original_prompt, statement
            )
            self.assertEqual(resumed_memory.data["sequence"], 1)
            self.assertEqual(
                resumed_memory.data["attempts"][0]["status"], "working"
            )
            provenance = json.loads(
                (continued.run_dir / web_ui.CONTINUATION_SOURCE_FILENAME)
                .read_text(encoding="utf-8")
            )
            self.assertEqual(
                provenance["copiedArtifacts"],
                [runner.AUTHOR_MEMORY_FILENAME],
            )
            restored_steer = json.loads(
                (continued.run_dir / web_ui.AUTHOR_STEER_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn(
                "Do not run more experiments or write code.",
                restored_steer["instruction"],
            )
            self.assertEqual(
                web_ui.saved_author_instructions(continued.run_dir),
                ["Do not run more experiments or write code."],
            )
            after = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)

    def test_continuing_stopped_critic_uses_the_latest_audit_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            source = runs / "stopped-critic"
            source.mkdir()
            (source / "draft.md").write_text("STATEMENT\n", encoding="utf-8")
            (source / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            (source / runner.SAVED_CANDIDATE_FILENAME).write_text(
                "PROOF\n", encoding="utf-8"
            )
            records = [
                {
                    "kind": "request", "stage": "critic",
                    "time": "2026-08-26T10:00:00+00:00",
                },
                {
                    "kind": "status", "stage": "critic", "node": "critic",
                    "label": "Stop requested",
                    "time": "2026-08-26T10:01:00+00:00",
                },
            ]
            (source / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()

            with mock.patch.object(
                server, "resume_critic_job", return_value="continued"
            ) as resume:
                result = server.continue_stopped_job(source.name)

            self.assertEqual(result, "continued")
            resume.assert_called_once_with(
                source.name, include_audit_checkpoint=True
            )

    def test_continuing_stopped_final_retries_only_exact_final_input(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            source = runs / "stopped-final"
            source.mkdir()
            (source / "draft.md").write_text("STATEMENT\n", encoding="utf-8")
            runner.save_final_input(
                "EXACT STATEMENT", "CLEAN PROOF", directory=source
            )
            records = [
                {
                    "kind": "request", "stage": "final",
                    "time": "2026-08-26T10:00:00+00:00",
                },
                {
                    "kind": "status", "stage": "final",
                    "node": "latex_editor", "label": "Stop requested",
                    "time": "2026-08-26T10:01:00+00:00",
                },
            ]
            (source / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()
            before = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            process, token = mock.Mock(), object()

            with mock.patch.object(
                runner, "verify_model_credentials"
            ), mock.patch.object(
                web_ui.App, "_launch_saved_final_locked",
                return_value=(process, token),
            ) as final, mock.patch.object(web_ui.threading, "Thread"):
                continued = server.continue_stopped_job(source.name)

            final.assert_called_once_with("EXACT STATEMENT", "CLEAN PROOF")
            self.assertEqual(continued.state["problemMode"], "final-resume")
            after = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)

    def test_saved_final_cli_keeps_the_original_two_part_prompt(self):
        report = {"latex": "LATEX"}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            saved_state = Path(directory) / "final-input.json"
            saved_state.write_text(json.dumps({
                "statement": "EXACT STATEMENT", "solution": "CLEAN PROOF",
            }), encoding="utf-8")
            with (
                mock.patch.object(runner, "configure_standard_streams"),
                mock.patch.object(runner.Path, "cwd", return_value=Path(directory)),
                mock.patch.object(sys, "stdin", io.StringIO("")),
                mock.patch.object(sys, "stdout", output),
                mock.patch.object(
                    runner, "structured", return_value=(report, json.dumps(report)),
                ) as structured,
            ):
                result = runner.main([
                    str(runner.WORKFLOWS / "clean_up.yaml"),
                    "--state-file", str(saved_state),
                ])

        self.assertEqual(result, 0)
        structured.assert_called_once()
        self.assertEqual(
            structured.call_args.args[0],
            runner.FINAL_PROMPT + "\n\nSTATEMENT:\nEXACT STATEMENT"
            "\n\nLATEST SOLUTION:\nCLEAN PROOF",
        )
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(
            event.get("kind") == "final_result" and event.get("output") == "LATEX"
            for event in events
        ))

    def test_stopped_failure_summary_can_restart_the_proof_author(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "stopped-failure"
            run.mkdir()
            (run / "draft.md").write_text("STATEMENT\n", encoding="utf-8")
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            records = [
                {"kind": "request", "stage": "solve"},
                {"kind": "request", "stage": "failure"},
                {
                    "kind": "status", "stage": "failure",
                    "node": "failure_summary", "label": "Stop requested",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            app = web_ui.restore_saved_jobs(run.parent)[run.name]
            state = app.snapshot()
            plan = web_ui.Server._stopped_continuation_plan(app, state)

            self.assertTrue(state["manuallyStopped"])
            self.assertEqual(state["stoppedStage"], "failure")
            self.assertEqual(state["activeNode"], "failure_summary")
            self.assertEqual(plan["action"], "author")

    def test_stopped_critic_resume_reenters_critic_not_generic_author(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run = runs / "stopped-critic-repair"
            run.mkdir()
            (run / "draft.md").write_text("STATEMENT\n", encoding="utf-8")
            (run / "checked-statement.md").write_text(
                "# Checked statement\n\nSTATEMENT\n", encoding="utf-8"
            )
            (run / runner.SAVED_CANDIDATE_FILENAME).write_text(
                "PROOF\n", encoding="utf-8"
            )
            (run / "resume-source.json").write_text(
                json.dumps({"sourceRun": "older"}), encoding="utf-8"
            )
            records = [
                {"kind": "request", "stage": "repair"},
                {
                    "kind": "status", "stage": "repair", "node": "author",
                    "label": "Stop requested",
                },
            ]
            (run / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            server = object.__new__(web_ui.Server)
            server.runs = runs
            server.fixed_app = False
            server.jobs = web_ui.restore_saved_jobs(runs)
            server.jobs_lock = web_ui.threading.RLock()

            with mock.patch.object(
                server, "resume_critic_job", return_value="continued"
            ) as resume:
                result = server.continue_stopped_job(run.name)

            self.assertEqual(result, "continued")
            resume.assert_called_once_with(
                run.name, include_audit_checkpoint=True
            )

    def test_web_ui_includes_the_manual_stop_continuation_route(self):
        script = (web_ui.UI / "app.js").read_text(encoding="utf-8")

        self.assertIn("/continue-stopped?job=", script)
        self.assertIn("Continue stopped job", script)
        self.assertIn("Legacy settings warning", script)


if __name__ == "__main__":
    unittest.main()
