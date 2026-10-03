"""Critic-only jobs: a supplied statement and proof, no statement review and no author.

Everything runs offline: the runner process is mocked and no model is called.
"""

import http.client
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import workflow_runner as runtime
from ui import cli, server

CHEAP = "author_critic_cheap"
STANDARD = "author_critic"
STATEMENT = "Every finite graph with minimum degree two contains a cycle."
PROOF = "Walk along unused edges from any vertex; finiteness forces a repeated vertex."
REPORT = "The critic rejected the proof. Unresolved bugs:\n\nThe walk may revisit the previous vertex."


def runner_argv(popen):
    """Return the workflow_runner command of the one mocked launch."""

    return popen.call_args.args[0]


def workflow_files(argv):
    runner = argv.index(str(server.ROOT / "workflow_runner.py"))
    return [Path(item).name for item in argv[runner + 1:] if item.endswith(".yaml")]


def state_file(argv):
    return json.loads(Path(argv[argv.index("--state-file") + 1]).read_text(encoding="utf-8"))


class CriticOnlyJobTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        self.manager = object.__new__(server.Server)
        self.manager.runs = self.runs
        self.manager.jobs = {}
        self.manager.jobs_lock = threading.RLock()
        self.manager.fixed_app = False
        for change in (mock.patch.object(runtime, "verify_model_credentials"),
                       mock.patch.object(server.App, "_spawn_worker")):
            change.start()
            self.addCleanup(change.stop)

    def launch(self, start):
        """Run one job action with the runner process mocked; return its result and argv."""

        with mock.patch.object(server.subprocess, "Popen",
                               return_value=mock.Mock(stdin=io.StringIO())) as popen:
            result = start()
        self.assertEqual(popen.call_count, 1)
        return result, runner_argv(popen)

    def start(self, **body):
        return self.launch(lambda: self.manager.start_critic_job(
            {"statement": STATEMENT, "proof": PROOF, **body}))

    @staticmethod
    def settle(app, **state):
        """Mark a launched job idle, as its finished reader would."""

        app.process = app.active_token = app.worker_token = None
        app.state.update({"phase": "done", **state})

    def read_output(self, app, records, code):
        """Feed runner records to the job's reader as if the runner printed them."""

        process, token = mock.Mock(), object()
        process.stdout = iter(json.dumps(record) + "\n" for record in records)
        process.wait.return_value = code
        app.process, app.active_token, app.worker_token = process, token, token
        app.state.update(phase="running", stage="critic", activeNode="critic")
        app._read_output(process, token)

    def test_critic_endpoint_launches_only_the_critic_with_critic_only_state(self):
        app, argv = self.start(criticRounds=3, thinkingHours=2, criticModel="gpt-5.6-sol",
                               criticEffort="high", writerModel="gpt-5.6-luna", writerEffort="medium",
                               speedMode="standard", reasoningSummary="detailed")
        self.assertEqual(workflow_files(argv), [f"{CHEAP}.yaml"])
        self.assertEqual(argv[argv.index("--start-node") + 1], "critic")
        self.assertEqual(state_file(argv), {"statement": STATEMENT, "solution": PROOF, "critic_only": True})
        self.assertEqual(argv[argv.index("--critic-rounds") + 1], "3")
        self.assertEqual(float(argv[argv.index("--thinking-hours") + 1]), 2)
        self.assertEqual(argv[argv.index("--critic-model") + 1], "gpt-5.6-sol")
        self.assertEqual(argv[argv.index("--critic-effort") + 1], "high")
        self.assertEqual(argv[argv.index("--writer-model") + 1], "gpt-5.6-luna")
        self.assertEqual(argv[argv.index("--speed") + 1], "standard")
        self.assertIn("file_management=false", argv)
        self.assertNotIn("goal_resume=true", argv)
        self.assertEqual(app.state["phase"], "running")
        self.assertEqual((app.state["stage"], app.state["activeNode"]), ("critic", "critic"))
        self.assertEqual(app.state["problemMode"], "critic-resume")
        self.assertTrue(app.state["criticOnly"])
        self.assertTrue(app.state["skipStatementReview"])
        self.assertFalse(app.state["fileManagement"])
        self.assertEqual(app.state["authorWorkflow"], CHEAP)
        self.assertIs(app.state["workflow"], server.CRITIC_ONLY_GRAPH)
        self.assertNotIn("author", server.CRITIC_ONLY_GRAPH["nodes"])
        self.assertIs(self.manager.jobs[app.state["runId"]], app)
        self.assertIn("_critic-only-every-finite-graph-with-minimum", app.run_dir.name)
        settings = json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual((settings["criticOnly"], settings["problemMode"], settings["fileManagement"]),
                         (True, "critic-resume", False))
        self.assertEqual(server.saved_statement(app.run_dir), STATEMENT)
        self.assertEqual((app.run_dir / runtime.SAVED_CANDIDATE_FILENAME).read_text(), PROOF + "\n")
        self.assertEqual((app.run_dir / "supplied-proof.md").read_text(), PROOF + "\n")
        saved = server.saved_critic_source(app.run_dir)
        self.assertEqual((saved["solution"].strip(), saved["critic_only"]), (PROOF, True))
        self.assertEqual((app.run_dir / "prompts/critic.txt").read_text().strip(),
                         server.default_prompts(CHEAP)["critic"].strip())
        # No research audits or solvers can start without an author.
        self.assertIsNone(app._start_research_audits(app.worker_token))

    def test_standard_workflow_and_prompt_overrides_are_kept(self):
        app, argv = self.start(authorWorkflow=STANDARD,
                               promptOverrides={"critic": "Custom critic.", "final": "Custom final."},
                               fileManagement=True)
        self.assertEqual(workflow_files(argv), [f"{STANDARD}.yaml"])
        self.assertTrue(state_file(argv)["critic_only"])
        self.assertEqual(app.state["authorWorkflow"], STANDARD)
        self.assertFalse(app.state["fileManagement"], "A critic-only job never manages research files")
        self.assertEqual((app.run_dir / "prompts/critic.txt").read_text(), "Custom critic.\n")
        self.assertEqual((app.run_dir / "prompts/final.txt").read_text(), "Custom final.\n")

    def test_invalid_input_is_rejected_before_creating_a_run(self):
        for body, message in (
            ({"statement": "", "proof": PROOF}, "statement"),
            ({"statement": "  \n", "proof": PROOF}, "statement"),
            ({"statement": STATEMENT, "proof": ""}, "proof"),
            ({"statement": STATEMENT}, "proof"),
            ({"statement": STATEMENT + "\0", "proof": PROOF}, "NUL"),
            ({"statement": STATEMENT, "proof": PROOF + "\0"}, "NUL"),
            ({"statement": STATEMENT, "proof": ["not text"]}, "text"),
            ({"statement": None, "proof": PROOF}, "text"),
            ({"statement": STATEMENT, "proof": PROOF, "authorWorkflow": "bogus"}, "workflow"),
            ({"statement": STATEMENT, "proof": PROOF, "criticModel": "bogus"}, "Choose"),
            ({"statement": STATEMENT, "proof": PROOF, "criticRounds": 0}, "round"),
            ({"statement": STATEMENT, "proof": PROOF, "promptOverrides": {"critic": 3}}, "Prompt"),
        ):
            with self.subTest(body=body):
                with mock.patch.object(server.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(ValueError, message):
                        self.manager.start_critic_job(body)
                popen.assert_not_called()
        self.assertFalse(self.runs.exists() and any(self.runs.iterdir()))
        with self.assertRaisesRegex(ValueError, "home screen"):
            self.manager.start_critic_job({"statement": STATEMENT, "proof": PROOF}, "some-job")

    def test_settings_persist_and_restore_after_a_restart(self):
        app, _ = self.start(criticRounds=4)
        app.add_trace({"kind": "request", "stage": "critic", "text": "Review."})
        self.settle(app)
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertTrue(restored.state["criticOnly"])
        self.assertEqual(restored.state["problemMode"], "critic-resume")
        self.assertTrue(restored.state["skipStatementReview"])
        self.assertFalse(restored.state["fileManagement"])
        self.assertEqual((restored.state["authorWorkflow"], restored.state["criticRounds"]), (CHEAP, 4))
        self.assertIs(restored.state["workflow"], server.CRITIC_ONLY_GRAPH)
        self.assertEqual(restored.state["settingsWarning"], "")
        jobs = server.restore_saved_jobs(self.runs)
        self.assertTrue(jobs[app.state["runId"]].state["criticOnly"])
        self.manager.jobs = jobs
        listed = self.manager.job_list()[0]
        self.assertTrue(listed["criticOnly"])
        self.assertEqual(listed["problemMode"], "critic-resume")
        # A relaunch from the restored job keeps critic_only.
        with mock.patch.object(server.subprocess, "Popen", return_value=mock.Mock(stdin=io.StringIO())) as popen:
            with restored.lock:
                restored._launch_critic_resume_locked(STATEMENT, PROOF)
        self.assertTrue(state_file(runner_argv(popen))["critic_only"])
        # Other jobs never restore as critic-only, whatever their saved settings say.
        other = self.runs / "not-a-critic-job"
        other.mkdir()
        (other / "transcript.jsonl").write_text("")
        (other / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({"criticOnly": True, "problemMode": "statement"}))
        self.assertFalse(server.restore_saved_app(server.App(other / "transcript.jsonl", self.runs)).state["criticOnly"])

    def test_a_rejection_finishes_the_job_with_the_critic_report(self):
        app, _ = self.start()
        records = [
            {"kind": "request", "stage": "critic", "label": "Critic round 1", "text": "Review."},
            {"kind": "critic_result", "stage": "critic", "round": 1, "label": "Critic round 1",
             "report": {"verdict": "reject", "solution": "", "bugs": "The walk may revisit the previous vertex."}},
            {"kind": "failure_result", "stage": "critic", "label": "Critic rejected the proof", "output": REPORT},
        ]
        self.read_output(app, records, 1)
        self.assertEqual(app.state["phase"], "done")
        self.assertEqual(app.state["error"], server.CRITIC_REJECTED_MESSAGE)
        self.assertEqual(app.state["output"], REPORT)
        self.assertEqual((app.run_dir / "failure-summary.md").read_text(), REPORT)
        self.assertFalse((app.run_dir / "final.tex").exists())
        self.assertFalse((app.run_dir / server.PAUSE_FILENAME).exists())
        self.assertEqual(app.state["activeNode"], "critic")
        # The report survives a restart, with the same message.
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual((restored.state["phase"], restored.state["error"]), ("done", server.CRITIC_REJECTED_MESSAGE))
        self.assertEqual(restored.state["output"], REPORT)
        # No author continuation is offered for it.
        self.manager.jobs = {app.state["runId"]: app}
        listed = self.manager.job_list()[0]
        self.assertNotIn("author", listed["continueStoppedLabel"].lower())

        # The same records in a job with an author keep the generic incomplete message.
        normal = server.App(runs=self.runs)
        with mock.patch.object(normal, "_launch_critic_resume_locked", return_value=(object(), object())):
            normal.start_critic_resume(STATEMENT, PROOF)
        self.assertFalse(normal.state["criticOnly"])
        self.read_output(normal, records, 1)
        self.assertIn("Workflow incomplete", normal.state["error"])

    def test_by_default_a_pass_finishes_with_the_approved_proof(self):
        app, argv = self.start()
        self.assertFalse(app.state["latexWriter"])
        self.read_output(app, [
            {"kind": "critic_result", "stage": "critic", "round": 1,
             "report": {"verdict": "pass", "solution": PROOF, "bugs": ""}},
            {"kind": "status", "stage": "critic", "label": "Critic approved", "text": "Round 1 passed."},
        ], 0)
        self.assertEqual((app.state["phase"], app.state["error"]), ("done", ""))
        self.assertEqual((app.run_dir / "final-proof.md").read_text().strip(), PROOF.strip())
        self.assertFalse((app.run_dir / "final.tex").exists())

    def test_a_pass_finishes_with_the_latex_output(self):
        app, argv = self.start(latexWriter=True)
        self.assertEqual(workflow_files(argv), [f"{CHEAP}.yaml", "clean_up.yaml"])
        records = [
            {"kind": "request", "stage": "critic", "label": "Critic round 1", "text": "Review."},
            {"kind": "critic_result", "stage": "critic", "round": 1,
             "report": {"verdict": "pass", "solution": PROOF, "bugs": ""}},
            {"kind": "status", "stage": "critic", "label": "Critic approved", "text": "Round 1 passed."},
            {"kind": "request", "stage": "final", "node": "latex_editor", "text": "Polish."},
            {"kind": "final_result", "stage": "final", "node": "latex_compile", "output": "\\documentclass{article}"},
        ]
        self.read_output(app, records, 0)
        self.assertEqual((app.state["phase"], app.state["error"]), ("done", ""))
        self.assertEqual((app.run_dir / "final.tex").read_text(), "\\documentclass{article}")
        self.assertTrue(app.state["finalInputReady"])

    def test_continue_critic_after_a_stop_stays_critic_only(self):
        source, _ = self.start()
        self.settle(source, stage="critic", activeNode="critic", error="Stopped.",
                    manuallyStopped=True, stoppedStage="critic")
        plan = self.manager._stopped_continuation_plan(source)
        self.assertEqual((plan["action"], plan["label"]), ("critic", "Continue critic"))
        self.assertIn("critic-only", plan["description"])
        continued, argv = self.launch(lambda: self.manager.continue_stopped_job(source.state["runId"]))
        self.assertIsNot(continued, source)
        self.assertEqual(argv[argv.index("--start-node") + 1], "critic")
        self.assertEqual(state_file(argv), {"statement": STATEMENT, "solution": PROOF, "critic_only": True})
        self.assertEqual(workflow_files(argv), [f"{CHEAP}.yaml"])
        self.assertTrue(continued.state["criticOnly"])
        self.assertIs(continued.state["workflow"], server.CRITIC_ONLY_GRAPH)
        self.assertNotIn("goal_resume=true", argv)
        self.assertTrue(json.loads((continued.run_dir / server.JOB_SETTINGS_FILENAME).read_text())["criticOnly"])
        source_record = json.loads((continued.run_dir / server.CONTINUATION_SOURCE_FILENAME).read_text())
        self.assertEqual(source_record["sourceRun"], str(source.run_dir.resolve()))

        # Neither a failure stage nor an author stage offers an author continuation.
        self.settle(continued)
        for stage in ("solve", "repair", "failure", "critic"):
            for stopped in (True, False):
                with self.subTest(stage=stage, stopped=stopped):
                    source.state.update(stage=stage, stoppedStage=stage if stopped else "",
                                        manuallyStopped=stopped)
                    plan = self.manager._stopped_continuation_plan(source)
                    self.assertTrue(plan is None or plan["action"] == "critic", plan)

    def test_checkpoint_and_resume_at_critic_stay_critic_only(self):
        source, _ = self.start()
        self.settle(source)
        checkpoints = source.snapshot()["checkpoints"]
        self.assertEqual([item["id"] for item in checkpoints], ["candidate"])
        self.assertIn("critic-only review", checkpoints[0]["description"])
        for start in (lambda: self.manager.resume_checkpoint_job(source.state["runId"], "candidate"),
                      lambda: self.manager.resume_critic_job(source.state["runId"])):
            continued, argv = self.launch(start)
            self.assertTrue(state_file(argv)["critic_only"])
            self.assertTrue(continued.state["criticOnly"])
            self.settle(continued)
        # The terminal --resume-critic path keeps it as well.
        continued, argv = self.launch(lambda: self.manager.start_saved_critic_job(source.run_dir, {}))
        self.assertTrue(state_file(argv)["critic_only"])
        self.assertTrue(continued.state["criticOnly"])
        # A critic-only run has no author to continue.
        with self.assertRaisesRegex(ValueError, "no proof author"):
            server.saved_research_source(source.run_dir)

    def test_resume_after_a_pause_keeps_critic_only(self):
        app, _ = self.start()
        self.settle(app, phase="paused", stage="critic", activeNode="critic")
        app._save_pause({"status": "paused", "node": "critic", "stage": "critic",
                         "state": {"statement": STATEMENT, "solution": PROOF, "paused": True},
                         "elapsedSeconds": 3.0, "goalThreadId": ""})
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["phase"], "paused")
        plan = self.manager._stopped_continuation_plan(restored)
        self.assertEqual(plan["action"], "resume")
        self.assertIn("critic-only", plan["description"])
        _, argv = self.launch(restored.resume)
        self.assertEqual(argv[argv.index("--start-node") + 1], "critic")
        self.assertEqual(state_file(argv), {"statement": STATEMENT, "solution": PROOF, "critic_only": True})

        # A checkpoint can never reopen an author for a critic-only job.
        self.settle(restored, phase="paused")
        restored._save_pause({"status": "paused", "node": "author", "stage": "solve",
                              "state": {"statement": STATEMENT}, "elapsedSeconds": 3.0, "goalThreadId": ""})
        with mock.patch.object(server.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "no proof author"):
                restored.resume()
        popen.assert_not_called()

    def test_document_endpoints_accept_large_bodies_with_a_cap(self):
        http_server = server.Server((server.HOST, 0), runs=self.runs)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http_server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http_server.shutdown)
        headers = {"Content-Type": "application/json", "X-TCS-Prover-Token": http_server.token}
        long_proof = PROOF + " Details." * 30_000  # About 270 KB, above the 100 KB general cap.
        body = json.dumps({"statement": STATEMENT, "proof": long_proof}).encode()
        self.assertGreater(len(body), server.MAX_REQUEST_BYTES)
        with mock.patch.object(server.subprocess, "Popen", return_value=mock.Mock(stdin=io.StringIO())):
            with urlopen(Request(http_server.origin + "/critic", data=body, headers=headers)) as response:
                state = json.load(response)
        self.assertTrue(state["criticOnly"])
        self.assertEqual(state["phase"], "running")
        job = http_server.get_job(state["runId"])
        self.assertEqual((job.run_dir / runtime.SAVED_CANDIDATE_FILENAME).read_text(), long_proof.strip() + "\n")
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(http_server.origin + "/critic", data=body, headers={"Content-Type": "application/json"}))
        self.assertEqual(error.exception.code, 403)

        def declared(path, size):
            """Send only headers declaring a body of this size; the cap applies before reading."""

            connection = http.client.HTTPConnection(server.HOST, http_server.server_port, timeout=10)
            self.addCleanup(connection.close)
            connection.putrequest("POST", path)
            for name, value in {**headers, "Content-Length": str(size)}.items():
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        for path in ("/critic", "/direct-batch"):
            with self.subTest(path=path):
                status, payload = declared(path, server.MAX_DOCUMENT_REQUEST_BYTES + 1)
                self.assertEqual(status, 400)
                self.assertIn("Request is too large", payload["error"])
        status, payload = declared("/direct", server.MAX_REQUEST_BYTES + 1)
        self.assertEqual((status, "Request is too large" in payload["error"]), (400, True))


class CriticOnlyCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.runs = self.directory / "runs"
        self.statement = self.directory / "statement.md"
        self.statement.write_text(STATEMENT + "\n", encoding="utf-8")
        self.proof = self.directory / "proof.md"
        self.proof.write_text(PROOF + "\n", encoding="utf-8")
        for change in (mock.patch.object(runtime, "verify_model_credentials"),
                       mock.patch.object(cli.runtime, "configure_standard_streams")):
            change.start()
            self.addCleanup(change.stop)

    def run_cli(self, *arguments, records=(), code=0):
        """Run the terminal command; the runner prints the given records and exits with code."""

        launched, output, errors = [], io.StringIO(), io.StringIO()

        def launch(app, statement, solution):
            process = mock.Mock()
            process.stdout = iter(json.dumps(record) + "\n" for record in records)
            process.wait.return_value = code
            token = object()
            app.process, app.active_token, app.worker_token = process, token, token
            app.state.update(phase="running", stage="critic", activeNode="critic")
            launched.append((app, statement, solution))
            return process, token

        original = cli.run_headless_critic_only

        def run(statement_path, proof_path, **settings):
            return original(statement_path, proof_path, runs=self.runs, output_stream=output,
                            error_stream=errors, **settings)

        with mock.patch.object(cli.sys, "argv", ["web_ui.py", *arguments]), \
                mock.patch.object(cli, "run_headless_critic_only", side_effect=run), \
                mock.patch.object(server.App, "_launch_critic_resume_locked", autospec=True, side_effect=launch), \
                mock.patch.object(server.App, "_spawn_worker", autospec=True,
                                  side_effect=lambda app, target, args, token: target(*args)):
            status = cli.main()
        return status, launched, output.getvalue(), errors.getvalue()

    def test_every_spelling_runs_a_critic_only_job_with_the_settings_flags(self):
        for flags in (("--critic-only", str(self.proof)), (f"--critic-only={self.proof}",),
                      ("-criticOnly", str(self.proof)), ("--criticOnly", str(self.proof))):
            with self.subTest(flags=flags):
                status, launched, _, errors = self.run_cli(str(self.statement), *flags, "-criticRounds", "3",
                                                           "--critic-model", "gpt-5.6-sol", "-thinkingHours", "2")
                self.assertEqual(status, 0)
                [(app, statement, solution)] = launched
                self.assertEqual((statement, solution), (STATEMENT, PROOF))
                self.assertTrue(app.state["criticOnly"])
                self.assertEqual(app.state["authorWorkflow"], CHEAP)
                self.assertEqual((app.state["criticRounds"], app.state["thinkingHours"]), (3, 2.0))
                self.assertEqual(app.state["criticModel"], "gpt-5.6-sol")
                self.assertEqual(app.state["sourceFile"], "statement.md")
                self.assertIn("Critic-only review started", errors)
        status, launched, _, _ = self.run_cli(str(self.statement), "--critic-only", str(self.proof),
                                              "--workflow", STANDARD)
        self.assertEqual((status, launched[0][0].state["authorWorkflow"]), (0, STANDARD))

    def test_exit_status_reports_a_pass_or_a_rejection(self):
        passed = [
            {"kind": "request", "stage": "critic", "text": "Review."},
            {"kind": "critic_result", "stage": "critic", "round": 1,
             "report": {"verdict": "pass", "solution": PROOF, "bugs": ""}},
            {"kind": "status", "stage": "critic", "label": "Critic approved"},
            {"kind": "request", "stage": "final", "text": "Polish."},
            {"kind": "final_result", "stage": "final", "output": "\\documentclass{article}"},
        ]
        status, launched, output, errors = self.run_cli(str(self.statement), "--critic-only", str(self.proof),
                                                        "--latex", records=passed)
        self.assertEqual(status, 0)
        self.assertIn("[statement.md] Current step: Independent critic (round 1)", output)
        self.assertIn("[statement.md] Current step: LaTeX editor", output)
        self.assertIn("The critic passed the proof", errors)
        self.assertIn(str(launched[0][0].run_dir / "final.tex"), errors)

        # By default there is no LaTeX writer: the approved proof is the result.
        status, launched, output, errors = self.run_cli(str(self.statement), "--critic-only", str(self.proof),
                                                        records=passed[:3])
        self.assertEqual(status, 0)
        self.assertNotIn("LaTeX editor", output)
        final = launched[0][0].run_dir / "final-proof.md"
        self.assertIn(str(final), errors)
        self.assertEqual(final.read_text().strip(), PROOF.strip())

        rejected = [
            {"kind": "request", "stage": "critic", "text": "Review."},
            {"kind": "critic_result", "stage": "critic", "round": 1,
             "report": {"verdict": "reject", "solution": "", "bugs": "Gap."}},
            {"kind": "failure_result", "stage": "critic", "label": "Critic rejected the proof", "output": REPORT},
        ]
        status, launched, _, errors = self.run_cli(str(self.statement), "--critic-only", str(self.proof),
                                                   records=rejected, code=1)
        self.assertEqual(status, 1)
        report = launched[0][0].run_dir / "failure-summary.md"
        self.assertIn(f"[statement.md] The critic rejected the proof. Report: {report}", errors)
        self.assertEqual(report.read_text(), REPORT)

        status, _, _, errors = self.run_cli(
            str(self.statement), "--critic-only", str(self.proof),
            records=[{"kind": "diagnostic", "stage": "critic", "text": "error: Workflow time limit reached."}], code=1)
        self.assertEqual(status, 1)
        self.assertIn("Critic-only review failed: Workflow time limit reached.", errors)

    def test_invalid_combinations_are_argparse_errors(self):
        folder = self.directory / "statements"
        folder.mkdir()
        cases = (
            ((str(folder), "--critic-only", str(self.proof)), "not a folder"),
            (("--critic-only", str(self.proof)), "needs the statement file"),
            ((str(self.directory / "missing.md"), "--critic-only", str(self.proof)), "statement file does not exist"),
            ((str(self.statement), "--critic-only", str(self.directory / "missing-proof.md")),
             "proof file does not exist"),
            (("--critic-only", str(self.proof), "--resume-critic", str(self.directory)), "cannot be combined"),
            (("--critic-only", str(self.proof), "--resume-author", str(self.directory)), "cannot be combined"),
            ((str(self.statement), "--critic-only", str(self.proof), "--resume-critic", str(self.directory)),
             "cannot be combined"),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                with mock.patch.object(cli.sys, "argv", ["web_ui.py", *arguments]), \
                        mock.patch.object(cli.sys, "stderr", io.StringIO()) as error, \
                        mock.patch.object(server.App, "start_critic_resume") as start:
                    with self.assertRaises(SystemExit) as stopped:
                        cli.main()
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn(message, error.getvalue())
                start.assert_not_called()
        self.assertFalse(self.runs.exists())

    def test_unreadable_inputs_fail_without_starting(self):
        self.proof.write_text("  \n", encoding="utf-8")
        with mock.patch.object(cli.sys, "argv", ["web_ui.py", str(self.statement), "--critic-only", str(self.proof)]), \
                mock.patch.object(cli.sys, "stderr", io.StringIO()) as error, \
                mock.patch.object(server.App, "start_critic_resume") as start:
            self.assertEqual(cli.main(), 1)
        self.assertIn("Cannot start critic-only review", error.getvalue())
        self.assertIn("proof is empty", error.getvalue())
        start.assert_not_called()


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class CriticOnlyBrowserTests(unittest.TestCase):
    def run_node(self, checks):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const slice = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
const code = slice('function selectedProblemMode()', '// Keep the compact footer label')
  + slice('function updateModelSummary()', 'const promptLabels =')
  + slice('const promptLabels =', 'function clockText(')
  + slice('async function startReview(', 'async function startLatexOnly(');
const setup = `
let currentJob = '', activePrompt = 'review', timer, jobsTimer, reviewPending = false;
let promptValues = {}, promptDrafts = {}, promptOriginals = {}, promptOverrides = {};
let state = {phase: 'input', workflow: WORKFLOW};
const simpleOnlyWorkflows = new Set(['author_critic_cheap']);
const values = {authorWorkflow: 'author_critic_cheap', reviewModel: 'gpt-6-astra', authorModel: 'gpt-6-astra',
  criticModel: 'gpt-5.6-sol', writerModel: 'gpt-5.6-luna', reviewEffort: 'ultra', authorEffort: 'ultra',
  criticEffort: 'high', writerEffort: 'medium', speedMode: 'fast', reasoningSummary: 'concise',
  criticRounds: '3', thinkingHours: '2'};
const ui = new Proxy({
  fileManagement: {checked: false, disabled: false},
  statementReviewOnly: {checked: false}, skipStatementReview: {checked: true},
  promptDialog: {open: false},
  problemModes: ['statement', 'critic', 'latex'].map(value => ({value, checked: value === 'statement'})),
}, {get(target, name) {
  return target[name] ||= {dataset: {}, value: values[name], focus() { this.focused = true; }};
}});
const show = (element, visible) => { element.hidden = !visible; };
const clearTimeout = () => {}, history = {pushState() {}}, jobUrl = id => '?job=' + id, jobPath = path => path;
const researchAuditValues = () => ({intervalHours: 2, models: ['gpt-6-astra', 'none', 'none']});
let rendered = null, sent = null;
const render = next => { rendered = next; };
const request = async (path, body) => { sent = {path, body}; return {runId: 'critic-job'}; };
`;
Promise.resolve(vm.runInNewContext(setup + code + process.argv[3], {assert, WORKFLOW: JSON.parse(process.argv[2])}))
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
        from ui import server as ui_server
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(ui_server.UI / "app.js"),
             json.dumps(ui_server.home_state()["workflow"]), checks],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_critic_mode_hides_author_settings_and_posts_the_critic_request(self):
        self.run_node(r"""
(async () => {
  setProblemMode('statement');
  assert.equal(selectedProblemMode(), 'statement');
  assert.equal(ui.criticFields.hidden, true);
  assert.equal(ui.authorModelSetting.hidden, false);
  assert.equal(ui.runFolder.hidden, false);
  assert.equal(ui.check.textContent, 'Start proof author');
  assert.match(ui.roundRejectHelp.textContent, /return to the author/);

  setProblemMode('critic');
  assert.equal(selectedProblemMode(), 'critic');
  for (const name of ['statementFields', 'criticFields', 'criticModelSetting',
      'criticRoundSetting', 'thinkingHoursSetting', 'authorWorkflowSetting', 'criticPromptTab', 'latexWriterSetting']) {
    assert.equal(ui[name].hidden, false, name);
  }
  for (const name of ['latexFields', 'authorModelSetting', 'reviewModelSetting', 'fileManagementSetting',
      'researchAuditsSetting', 'skipReviewSetting', 'reviewOnlySetting', 'authorPromptTab', 'reviewPromptTab',
      'runFolder', 'runFolderHelp', 'writerModelSetting', 'finalPromptTab']) {
    assert.equal(ui[name].hidden, true, name);
  }
  // Turning the LaTeX writer on brings back its model and prompt.
  ui.latexWriter.checked = true;
  setProblemMode('critic');
  for (const name of ['writerModelSetting', 'finalPromptTab']) assert.equal(ui[name].hidden, false, name);
  ui.latexWriter.checked = false;
  setProblemMode('critic');
  for (const name of ['writerModelSetting', 'finalPromptTab']) {
    assert.equal(ui[name].hidden, true, name);
  }
  assert.equal(ui.check.textContent, 'Start critic');
  assert.match(ui.roundRejectHelp.textContent, /end the job with the critic's report/);
  assert.equal(ui.proofInput.required, true);
  assert.equal(ui.problem.required, true);
  assert.match(ui.introDescription.textContent, /critic/);
  assert.match(ui.modelSummary.textContent, /Critic only · Sol\/High critic$/);
  ui.latexWriter.checked = true;
  updateModelSummary();
  assert.match(ui.modelSummary.textContent, /Critic only · Sol\/High critic · Luna\/Medium writer/);
  ui.latexWriter.checked = false;
  assert.doesNotMatch(ui.modelSummary.textContent, /author|review/);

  // Missing input never reaches the server.
  ui.problem.value = 'Exact statement'; ui.proofInput.value = '  ';
  await startCriticOnly();
  assert.equal(sent, null);
  assert.match(ui.notice.textContent, /complete proof/);
  assert.equal(ui.proofInput.focused, true);

  ui.proofInput.value = 'Complete proof.';
  promptOverrides = {critic: 'Custom critic.', author_simple: 'Author [STATEMENT]', review: 'Review'};
  promptValues = {critic: 'Custom critic.'};
  await startCriticOnly();
  assert.equal(sent.path, '/critic');
  assert.deepEqual(JSON.parse(JSON.stringify(sent.body)), {
    statement: 'Exact statement', proof: 'Complete proof.', authorWorkflow: 'author_critic_cheap',
    criticModel: 'gpt-5.6-sol', criticEffort: 'high', writerModel: 'gpt-5.6-luna', writerEffort: 'medium',
    promptOverrides: {critic: 'Custom critic.'}, criticRounds: 3, thinkingHours: 2, latexWriter: false,
    speedMode: 'fast', reasoningSummary: 'concise',
  });
  assert.equal(currentJob, 'critic-job');
  assert.deepEqual(rendered, {runId: 'critic-job'});

  setProblemMode('latex');
  assert.equal(ui.criticFields.hidden, true);
  assert.equal(ui.statementFields.hidden, true);
  assert.equal(ui.criticModelSetting.hidden, true);
})();
""")

    def test_sidebar_shows_only_the_critic_and_latex_stages(self):
        from ui import server as ui_server
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.className = ''; this.textContent = '';
    this.attributes = {}; this.classList = {add: name => this.className += ' ' + name}; }
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children = items;}
  setAttribute(name, value) {this.attributes[name] = value;}
}
const ui = {workflowNodes: new Element()};
const state = {phase: 'running', stage: 'critic', activeNode: 'critic', problemMode: 'critic-resume',
  criticOnly: true, skipStatementReview: true, fileManagement: false, round: 1, criticRounds: 2,
  workflow: JSON.parse(process.argv[2]), trace: [{stage: 'critic'}]};
const context = {ui, state, document: {createElement: () => new Element()}, nodeFromStage: () => 'critic'};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderWorkflow()'), source.indexOf('function renderClock()')), context);
const all = node => [node, ...node.children.flatMap(all)];
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
context.renderWorkflow();
const rows = all(ui.workflowNodes).filter(row => row.dataset.node).map(row => row.dataset.node);
assert.deepEqual(rows, ['critic', 'latex_editor', 'latex_compile']);
const critic = all(ui.workflowNodes).find(row => row.dataset.node === 'critic');
assert.equal(critic.attributes['aria-current'], 'step');
assert.match(text(critic), /rejection ends the job/);
assert.match(text(critic), /Round 1 of 2/);
state.phase = 'done'; state.error = 'The critic rejected the proof; see its report.';
context.renderWorkflow();
assert.match(all(ui.workflowNodes).find(row => row.dataset.node === 'critic').className, /failed/);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(ui_server.UI / "app.js"),
             json.dumps(ui_server.CRITIC_ONLY_GRAPH)],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
