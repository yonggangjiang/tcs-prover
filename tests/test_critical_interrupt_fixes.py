"""Regression tests for the critical interrupt fixes I-011, I-012, I-032, I-033 and I-076.

No test starts a model or a CLI: model calls are patched, launches are mocked,
and HOME, CODEX_HOME, CLAUDE_CONFIG_DIR and the API model store point at a
temporary folder.
"""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import isolated_compiler as compiler
from runner import providers
from ui import server
import workflow_runner as runtime


WAIT = server.AUTO_RESUME_SECONDS
LATEX = r"\documentclass{article}\begin{document}\section{Proof}\label{proof}See Section~\ref{proof}.\end{document}"
BROKEN = LATEX.replace("See Section", r"\undefinedcommand See Section")


class IsolatedTestCase(unittest.TestCase):
    """Keep every test away from the user's CLI homes and saved API keys."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        environment = patch.dict(os.environ, {
            "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            providers.STORE_ENV: str(self.home / "state" / "api-models.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def new_runs(self):
        folder = tempfile.TemporaryDirectory(dir=self.home)
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    @staticmethod
    def error_paused_job(runs, statement, reason="Connection lost", now=1_000, thinking_hours=None):
        """An author paused by a model-call error, the way the runner reports one."""

        app = server.App(runs=runs, load_latest=False)
        app._new_run(statement)
        app._save("checked-statement.md", f"# Checked statement\n\n{statement}\n")
        app.state.update(phase="running", stage="solve", activeNode="author", runId=app.run_dir.name,
                         goalThreadId="saved-thread", elapsedSeconds=12)
        if thinking_hours is not None:
            app.state["thinkingHours"] = thinking_hours
        app._save_job_settings(app.state)
        token = app.active_token = app.worker_token = object()
        process = app.process = Mock(stdout=io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "solve", "node": "author",
            "state": {"statement": statement, "paused": True}, "reason": reason,
        }) + "\n"))
        process.wait.return_value = process.poll.return_value = 1
        with patch.object(server.time, "time", return_value=now):
            app._read_output(process, token)
        return app

    @staticmethod
    def checkpoint(app):
        return json.loads((app.run_dir / server.PAUSE_FILENAME).read_text(encoding="utf-8"))

    @staticmethod
    def saved_checkpoint(app):
        """The checkpoint the controller trusts: its own copy, outside the author's folder."""

        return server.controller_directory(app.run_dir) / server.PAUSE_FILENAME

    @staticmethod
    def diagnostics(app):
        return [entry.get("text", "") for entry in app.state["trace"] if entry.get("kind") == "diagnostic"]


class DamagedCheckpointRetryTests(IsolatedTestCase):
    """I-011: one damaged pause.json must never stop the automatic retries of other jobs."""

    def damages(self, app):
        complete = self.checkpoint(app)

        def missing(*keys):
            return json.dumps({key: value for key, value in complete.items() if key not in keys})

        return {
            "not an object": "[]\n",
            "deeply nested": "[" * 100_000 + "]" * 100_000,
            "not JSON": "{",
            "missing state": missing("state"),
            "missing elapsed time": missing("elapsedSeconds"),
            "missing node and stage": missing("node", "stage"),
        }

    def test_a_damaged_checkpoint_never_stops_the_other_jobs_retries(self):
        probe = self.error_paused_job(self.new_runs(), "Probe")
        for label, text in self.damages(probe).items():
            with self.subTest(label):
                runs = self.new_runs()
                damaged = self.error_paused_job(runs, "Damaged job")
                healthy = self.error_paused_job(runs, "Healthy job")
                http = server.Server((server.HOST, 0), runs=runs)
                self.addCleanup(http.server_close)
                # The damaged job comes first, so its failure would end the pass.
                http.jobs = {name: http.jobs[name] for name in (damaged.run_dir.name, healthy.run_dir.name)}
                broken, waiting = http.jobs[damaged.run_dir.name], http.jobs[healthy.run_dir.name]
                self.saved_checkpoint(damaged).write_text(text, encoding="utf-8")
                with patch.object(waiting, "resume") as resumed, \
                        patch.object(server, "require_model_access"), \
                        patch.object(server.subprocess, "Popen") as popen:
                    http._retry_paused_jobs(now=1_000 + WAIT)
                    resumed.assert_called_once_with()
                    popen.assert_not_called()
                    # The damaged job reports why and keeps its place in the schedule.
                    self.assertEqual(broken.state["phase"], "paused")
                    self.assertEqual(broken.state["autoResumeAt"], 1_000 + 2 * WAIT)
                    failures = [line for line in self.diagnostics(broken)
                                if line.startswith("Automatic resume failed:")]
                    self.assertEqual(len(failures), 1, self.diagnostics(broken))
                    self.assertIn(server.PAUSE_FILENAME, failures[0])
                    self.assertTrue(failures[0].endswith(f"Retrying in {server.retry_interval_text()}."))
                    # No retry before the next minute, then one more report.
                    http._retry_paused_jobs(now=1_000 + 2 * WAIT - 1)
                    self.assertEqual(len([line for line in self.diagnostics(broken)
                                          if line.startswith("Automatic resume failed:")]), 1)
                    http._retry_paused_jobs(now=1_000 + 2 * WAIT)
                    self.assertEqual(broken.state["autoResumeAt"], 1_000 + 3 * WAIT)
                    popen.assert_not_called()
                # A checkpoint damaged before a restart is rebuilt from the transcript (I-083),
                # keeping its cause and retry time, so the retry resumes the saved conversation.
                if label == "missing state":
                    restarted = server.Server((server.HOST, 0), runs=runs)
                    self.addCleanup(restarted.server_close)
                    again = restarted.jobs[damaged.run_dir.name]
                    self.assertEqual(again.state["phase"], "paused")
                    self.assertTrue(again.state["autoResumeAt"])
                    self.assertIn("rebuilt", again.state["settingsWarning"])
                    self.assertEqual(self.checkpoint(again)["pauseCause"], "error")
                    self.assertTrue(server.complete_pause_checkpoint(self.checkpoint(again)))
                    with patch.object(restarted.jobs[healthy.run_dir.name], "resume"), \
                            patch.object(again, "_spawn_worker"), \
                            patch.object(server, "require_model_access"), \
                            patch.object(server.subprocess, "Popen") as popen:
                        restarted._retry_paused_jobs(now=again.state["autoResumeAt"])
                    popen.assert_called_once()
                    self.assertIn('goal_thread_id="saved-thread"', popen.call_args.args[0])

    def test_resume_refuses_a_damaged_checkpoint_before_changing_anything(self):
        app = self.error_paused_job(self.new_runs(), "Exact statement")
        settings = (app.run_dir / server.JOB_SETTINGS_FILENAME).read_bytes()
        for label, text in self.damages(app).items():
            with self.subTest(label):
                self.saved_checkpoint(app).write_text(text, encoding="utf-8")
                with patch.object(server, "require_model_access"), \
                        patch.object(server.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(ValueError, "pause.json"):
                        app.resume()
                popen.assert_not_called()
                self.assertEqual(self.saved_checkpoint(app).read_text(encoding="utf-8"), text)
                self.assertEqual((app.run_dir / server.JOB_SETTINGS_FILENAME).read_bytes(), settings)
                self.assertEqual((app.state["phase"], app.state["error"], app.state["autoResumeAt"]),
                                 ("paused", "Connection lost", 1_000 + WAIT))

    def test_the_retry_thread_survives_an_unexpected_failure(self):
        http = server.Server((server.HOST, 0), runs=self.new_runs())
        self.addCleanup(http.server_close)
        calls, survived = [], threading.Event()

        def retry_pass(now=None):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("unexpected")
            survived.set()

        with patch.object(http, "_retry_paused_jobs", side_effect=retry_pass), \
                patch.object(server.traceback, "print_exc") as reported:
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(survived.wait(5), "The retry thread ended after one failed pass")
            finally:
                http.shutdown()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        reported.assert_called()

    def test_the_running_server_still_retries_a_healthy_job_after_a_damaged_one(self):
        runs = self.new_runs()
        damaged = self.error_paused_job(runs, "Damaged job", now=1)
        healthy = self.error_paused_job(runs, "Healthy job", now=1)
        http = server.Server((server.HOST, 0), runs=runs)
        self.addCleanup(http.server_close)
        http.jobs = {name: http.jobs[name] for name in (damaged.run_dir.name, healthy.run_dir.name)}
        self.saved_checkpoint(damaged).write_text("[]\n", encoding="utf-8")
        resumed = threading.Event()
        waiting = http.jobs[healthy.run_dir.name]

        def resume():
            waiting.state.update(phase="running", autoResumeAt=0)
            resumed.set()

        with patch.object(waiting, "resume", side_effect=resume), \
                patch.object(server, "require_model_access"), patch.object(server.subprocess, "Popen") as popen:
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                self.assertTrue(resumed.wait(5), "A damaged checkpoint stopped every automatic retry")
            finally:
                http.shutdown()
                worker.join(5)
        popen.assert_not_called()
        self.assertTrue(any(text.startswith("Automatic resume failed:")
                            for text in self.diagnostics(http.jobs[damaged.run_dir.name])))


class StaleLatexAuxiliaryFileTests(IsolatedTestCase):
    """I-012: a failed compile's auxiliary files must not make a corrected source fail."""

    AUXILIARY = ("final.aux", "final.toc", "final.lof", "final.lot", "final.out", "final.bbl", "final.bcf",
                 "final.blg", "final.run.xml", "final.fls", "final.fdb_latexmk", "final.nav", "final.snm",
                 "final.log")

    def setUp(self):
        super().setUp()
        self.directory = self.new_runs()
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)

    def execute(self, model):
        with patch.object(runtime, "structured", side_effect=model), \
                patch.object(runtime, "emit"), patch.object(runtime, "retrieval_policy", return_value=None):
            return runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": BROKEN}, {})

    def test_every_compile_starts_without_the_previous_attempts_auxiliary_files(self):
        # Files of the author's workspace that are not this build's auxiliary files.
        unrelated = {"refs.bib": "@book{key, title={T}}", "figure.pdf": "%PDF-figure", "final.md": "Notes",
                     "notes.aux": "Another document", "final.tex.bak": "Backup"}
        for name, text in unrelated.items():
            (self.directory / name).write_text(text)
        candidates, present, commands = [BROKEN, LATEX], [], []

        def model(prompt, schema, stage, **settings):
            value = {"latex": candidates[min(len(commands), 1)]}
            return value, json.dumps(value)

        def command(argv, **kwargs):
            commands.append(argv)
            present.append(sorted(name for name in self.AUXILIARY if (self.directory / name).exists()))
            if len(commands) == 1:
                # A failed attempt leaves auxiliary files with a removed package's commands.
                for name in self.AUXILIARY:
                    (self.directory / name).write_text("\\relax\n\\@LN{0}{0}\n")
                return subprocess.CompletedProcess(argv, 12, "./final.aux:2: Undefined control sequence.")
            (self.directory / "final.pdf").write_bytes(b"%PDF-1.5\nCompiled\n%%EOF")
            (self.directory / "final.aux").write_text("\\relax\n")
            return subprocess.CompletedProcess(argv, 0, "Output written on final.pdf")

        with patch.object(runtime.shutil, "which", return_value="/tools/latexmk"), \
                patch.object(runtime, "_run_bounded", side_effect=command):
            state = self.execute(model)
        self.assertEqual(present, [[], []])
        self.assertEqual(len(commands), 2)
        # The published results are unchanged.
        self.assertEqual(state["output"], LATEX)
        self.assertEqual((self.directory / "final.tex").read_text(), LATEX)
        self.assertEqual((self.directory / "latex-source.tex").read_text(), LATEX)
        self.assertTrue((self.directory / "final.pdf").read_bytes().startswith(b"%PDF-"))
        self.assertIn("Output written on final.pdf", (self.directory / "latex-compile.log").read_text())
        self.assertEqual((self.directory / "final.aux").read_text(), "\\relax\n")
        for name, text in unrelated.items():
            self.assertEqual((self.directory / name).read_text(), text, name)

    def test_only_a_plain_jobname_in_the_build_folder_is_cleaned(self):
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "final.aux").write_text("keep")
        (self.directory / "final.aux").write_text("keep")
        for jobname in ("outside/final", "../final", str(outside / "final"), ".."):
            with self.subTest(jobname=jobname):
                config = {"argv": ["latexmk", "-pdf", f"-jobname={jobname}", "latex-source.tex"], "timeout": 5,
                          "produces": [], "result": "compilation"}
                with patch.object(runtime.shutil, "which", return_value="/tools/latexmk"), \
                        patch.object(runtime, "retrieval_policy", return_value=None), \
                        patch.object(runtime, "_run_bounded", return_value=subprocess.CompletedProcess([], 0, "")):
                    runtime._run_command(config, {"state": {}})
                self.assertEqual((outside / "final.aux").read_text(), "keep")
                self.assertEqual((self.directory / "final.aux").read_text(), "keep")
        # Commands without a jobname are not TeX builds this cleanup knows about.
        config = {"argv": ["builder", "final"], "timeout": 5, "produces": [], "result": "build"}
        with patch.object(runtime.shutil, "which", return_value="/tools/builder"), \
                patch.object(runtime, "retrieval_policy", return_value=None), \
                patch.object(runtime, "_run_bounded", return_value=subprocess.CompletedProcess([], 0, "")):
            runtime._run_command(config, {"state": {}})
        self.assertEqual((self.directory / "final.aux").read_text(), "keep")

    @unittest.skipUnless(shutil.which("latexmk") and shutil.which("pdflatex"), "TeX is not installed")
    def test_real_compile_recovers_after_a_failed_attempt_left_package_commands_in_final_aux(self):
        # lineno writes \@LN into final.aux on each shipped page; without lineno
        # the next compile stops at "./final.aux:2: Undefined control sequence".
        failed = (r"\documentclass{article}\usepackage[pagewise]{lineno}\linenumbers"
                  r"\begin{document}Hello.\newpage World.\undefinedcommand\end{document}")
        repaired = r"\documentclass{article}\begin{document}Hello.\newpage World.\end{document}"
        calls = []

        def model(prompt, *args, **kwargs):
            calls.append(prompt)
            self.assertLessEqual(len(calls), 2, "The repaired source did not compile:\n" + prompt[-1500:])
            if len(calls) == 2:
                self.assertIn("Undefined control sequence", prompt)
            return {"latex": failed if len(calls) == 1 else repaired}, ""

        self.execute(model)
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.directory / "final.tex").read_text(), repaired)
        self.assertEqual((self.directory / "latex-source.tex").read_text(), repaired)
        self.assertTrue((self.directory / "final.pdf").read_bytes().startswith(b"%PDF-"))
        self.assertTrue((self.directory / "latex-compile.log").is_file())
        self.assertNotIn(r"\@LN", (self.directory / "final.aux").read_text())


class TimeLimitClassificationTests(IsolatedTestCase):
    """I-032: only the runner's own time limit, not any text mentioning one, switches retries off."""

    def test_provider_messages_that_mention_a_time_limit_are_retried(self):
        reasons = ("upstream gateway time limit exceeded (504)", "Request exceeded the time limit; please retry",
                   "idle time limit reached", "Author turn failed: Time Limit Exceeded")
        for hours in (0, 168):
            for reason in reasons:
                with self.subTest(hours=hours, reason=reason):
                    runs = self.new_runs()
                    app = self.error_paused_job(runs, "Exact statement", reason=reason, thinking_hours=hours)
                    self.assertEqual((self.checkpoint(app)["pauseCause"], app.state["autoResumeAt"]),
                                     ("error", 1_000 + WAIT))
                    restored = server.restore_saved_app(server.App(app.trace_file, runs))
                    self.assertEqual(restored.state["autoResumeAt"], 1_000 + WAIT)
                    retry = Mock()
                    self.assertTrue(app.auto_resume_if_due(retry, now=1_000 + WAIT))
                    retry.assert_called_once_with()

    def test_the_runners_own_time_limit_still_ends_retries(self):
        # The text the runner raises when the budget runs out during a retry wait.
        with patch.object(runtime, "emit"), patch.object(runtime, "workflow_remaining", return_value=0):
            with self.assertRaises(runtime.Error) as raised:
                runtime.wait_before_retry({}, "solve", 2, runtime.Error("Connection lost"))
        for reason in ("Workflow time limit reached.", str(raised.exception)):
            with self.subTest(reason=reason):
                runs = self.new_runs()
                app = self.error_paused_job(runs, "Exact statement", reason=reason, thinking_hours=0)
                self.assertEqual(app.state["autoResumeAt"], 0)
                restored = server.restore_saved_app(server.App(app.trace_file, runs))
                self.assertEqual(restored.state["autoResumeAt"], 0)
        # An elapsed budget still counts, whatever the message says.
        app = self.error_paused_job(self.new_runs(), "Exact statement", reason="Connection lost", thinking_hours=1)
        checkpoint = {**self.checkpoint(app), "elapsedSeconds": 3600}
        app._configure_auto_resume(checkpoint, now=1_000)
        self.assertEqual(checkpoint["autoResumeAt"], 0)


class CompilerCanaryTests(IsolatedTestCase):
    """I-033: a canary that times out or cannot start is a clean isolation error, not a traceback."""

    CONFIG = {"argv": compiler.LATEX_ARGV, "requires": ["pdflatex"],
              "env": {"openin_any": "p", "openout_any": "p"}, "timeout": 120,
              "produces": ["final.pdf"], "log": "latex-compile.log", "result": "compilation"}

    def setUp(self):
        super().setUp()
        self.workspace = self.new_runs().resolve()
        self.policy = {"isolation": {"adapter": compiler.ADAPTER, "workspace": str(self.workspace)}}
        (self.workspace / "latex-source.tex").write_text(LATEX, encoding="utf-8")
        self.output = self.workspace / "final.pdf"
        self.output.write_bytes(b"existing output")
        tools = (Path("/usr/local/texlive"), Path("/Library/TeX/texbin"), Path("/Library/TeX/texbin/latexmk"))
        for patcher in (patch.object(compiler, "supported", return_value=True),
                        patch.object(compiler, "toolchain", return_value=tools)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def failures(self):
        profile = compiler.sandbox_profile(self.workspace, Path("/usr/local/texlive"))
        return {
            "timeout": subprocess.TimeoutExpired(["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/perl"], 15,
                                                 output="partial"),
            "subprocess error": subprocess.SubprocessError("sandbox-exec could not start"),
            "operating system error": BlockingIOError(35, "Resource temporarily unavailable"),
        }

    def test_canary_failures_raise_compiler_isolation_error_and_keep_outputs(self):
        for label, failure in self.failures().items():
            with self.subTest(label):
                with patch.object(compiler, "_run", side_effect=failure) as run:
                    with self.assertRaises(compiler.CompilerIsolationError) as raised:
                        compiler.compile_command(self.policy, self.CONFIG, self.CONFIG["argv"], self.workspace)
                run.assert_called_once()  # Only the canary ran; TeX never started.
                self.assertTrue(str(raised.exception).startswith("Compiler sandbox canaries "), raised.exception)
                if label == "timeout":
                    self.assertIn("within 15 seconds", str(raised.exception))
                self.assertNotIn("(deny default)", str(raised.exception))
                self.assertEqual(self.output.read_bytes(), b"existing output")
                self.assertFalse((self.workspace / "latex-compile.log").exists())

    def test_the_runner_reports_a_canary_timeout_as_a_plain_error(self):
        previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)
        for label, failure in self.failures().items():
            with self.subTest(label):
                with patch.object(runtime, "retrieval_policy", return_value=self.policy), \
                        patch.object(compiler, "_run", side_effect=failure):
                    result = runtime._run_command(self.CONFIG, {"state": {}})
                # I-069: the checks run again before the next attempt; nothing compiled.
                self.assertEqual(result["status"], "unavailable")
                self.assertIn("Compiler sandbox canaries ", result["diagnostic"])
                self.assertNotIn("(deny default)", result["diagnostic"])
                self.assertEqual(self.output.read_bytes(), b"existing output")


class UserPauseErrorTests(IsolatedTestCase):
    """An author error that reaches a user pause still waits for the user."""

    def setUp(self):
        super().setUp()
        self.runs = self.new_runs()
        self.app = server.App(runs=self.runs, load_latest=False)
        self.app._new_run("Exact task")
        self.app._save("checked-statement.md", "# Checked statement\n\nExact task\n")
        self.app.state.update(phase="running", activeNode="author", stage="solve", runId=self.app.run_dir.name,
                              goalWorkspace=str(self.app.run_dir),
                              goalThreadId="saved-author-thread")
        self.app._save_job_settings(self.app.state)




    def test_an_error_during_a_users_pause_still_waits_for_the_user(self):
        app = self.app
        process = app.process = Mock(stdout=io.StringIO(""))
        process.wait.return_value = 1
        token = app.worker_token = app.active_token = object()
        app.pause()
        process.stdout = io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "solve", "node": "author",
            "state": self.checkpoint(app)["state"], "reason": "Connection dropped while pausing"}) + "\n")
        with patch.object(server.time, "time", return_value=1_000):
            app._read_output(process, token)
        self.assertEqual((self.checkpoint(app)["pauseCause"], app.state["autoResumeAt"]), ("user", 0))


class DamagedCheckpointReaderTests(IsolatedTestCase):
    """I-011, other readers: a damaged pause.json breaks neither the job list nor Keep paused."""

    DAMAGES = {"not an object": "[]\n", "not JSON": "{", "deeply nested": "[" * 100_000 + "]" * 100_000}

    def test_the_job_list_and_snapshot_survive_a_damaged_checkpoint(self):
        for label, text in self.DAMAGES.items():
            with self.subTest(label):
                runs = self.new_runs()
                app = self.error_paused_job(runs, "Damaged job")
                self.saved_checkpoint(app).write_text(text, encoding="utf-8")
                self.assertEqual(app.snapshot()["phase"], "paused")
                http = server.Server((server.HOST, 0), runs=runs)
                self.addCleanup(http.server_close)
                # A restart keeps the job listed, and the list itself still renders.
                self.assertIn(app.run_dir.name, http.jobs)
                self.assertIn(app.run_dir.name, [row["runId"] for row in http.job_list()])

    def test_keep_paused_cancels_the_retry_of_a_damaged_checkpoint(self):
        for label, text in self.DAMAGES.items():
            with self.subTest(label):
                app = self.error_paused_job(self.new_runs(), "Damaged job")
                self.saved_checkpoint(app).write_text(text, encoding="utf-8")
                app.pause()
                self.assertEqual(app.state["autoResumeAt"], 0)
                retry = Mock()
                self.assertFalse(app.auto_resume_if_due(retry, now=10**9))
                retry.assert_not_called()
                # The damaged file is left as it was, for Resume to report.
                self.assertEqual(self.saved_checkpoint(app).read_text(encoding="utf-8"), text)

    def test_the_output_reader_survives_a_damaged_checkpoint_while_pausing(self):
        app = self.error_paused_job(self.new_runs(), "Damaged job")
        app.state.update(phase="pausing")
        self.saved_checkpoint(app).write_text("[]\n", encoding="utf-8")
        token = app.active_token = app.worker_token = object()
        process = app.process = Mock(stdout=io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "solve", "node": "author",
            "state": {"statement": "Damaged job", "paused": True}, "reason": "Connection lost",
        }) + "\n"))
        process.wait.return_value = process.poll.return_value = 1
        with patch.object(server.time, "time", return_value=2_000):
            app._read_output(process, token)
        # The reader finished normally and scheduled the usual retry for the error.
        self.assertEqual(app.state["phase"], "paused")
        self.assertEqual(app.state["autoResumeAt"], 2_000 + WAIT)


if __name__ == "__main__":
    unittest.main()
