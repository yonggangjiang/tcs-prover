"""The LaTeX stage decisions (I-013 ... I-130): the compile and repair loop, its checks and the isolated compiler.

Offline: a stand-in compiler or real latexmk/pdflatex, stand-in model answers,
and patched retry waits; no model call is made and no wait takes real time.
"""

import contextlib
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import isolated_compiler as compiler
from runner import claude_cli
from ui import server
import workflow_runner as runtime

GOOD = r"\documentclass{article}\begin{document}\section{Proof}\label{proof}See Section~\ref{proof}.\end{document}"
HAS_TEX = bool(shutil.which("latexmk") and shutil.which("pdflatex"))
UNDEFINED = "./latex-source.tex:3: Undefined control sequence.\nl.3 \\undefinedcommand\n"


class AuthorStarted(BaseException):
    """A workflow test reached the author, which would start a real CLI session."""


class Runaway(BaseException):
    """A retry wait that never ends fails its test at once."""


def answer(value):
    return value, json.dumps(value)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def paper(sections=12, label="sec"):
    """A LaTeX paper big enough for the content checks, with theorem environments and labels."""
    body = "".join(rf"\section{{Part {index}}}\label{{{label}:{index}}}\begin{{lemma}}Statement {index}.\end{{lemma}}"
                   rf"\begin{{proof}}{'Careful argument. ' * 20}\end{{proof}}" for index in range(sections))
    return r"\documentclass{article}\newtheorem{lemma}{Lemma}\begin{document}" + body + r"\end{document}"


class LatexCase(unittest.TestCase):
    """A temporary run folder, recorded events, and retry waits that take no real time."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.events, self.sleeps, self.prompts, self.problems = [], [], [], []
        for target, name, value in (
                (runtime, "emit", lambda kind, stage, **fields: self.events.append({"kind": kind, "stage": stage, **fields})),
                (runtime, "_retry_sleep", self.sleep), (runtime, "_retry_jitter", lambda seconds: 0.0),
                (runtime, "MODEL_RETRY_SECONDS", 60), (runtime, "retrieval_policy", lambda *args, **kwargs: None),
                # The LaTeX workflow has no author; reaching one fails the test before any CLI starts.
                (runtime, "goal_session", mock.Mock(side_effect=AuthorStarted("the author must not run"))),
                (claude_cli, "goal_session", mock.Mock(side_effect=AuthorStarted("the author must not run")))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.on_sleep = None

    def sleep(self, seconds, cancel=None):
        self.sleeps.append(seconds)
        if len(self.sleeps) > 20000:
            raise Runaway("runaway retry loop")
        if self.on_sleep:
            self.on_sleep(sum(self.sleeps))

    def model(self, answers):
        """A writer that answers in order; each answer goes through the node's checks like a real call."""
        queue = list(answers)

        def call(prompt, schema, stage, **settings):
            self.prompts.append(prompt)
            if len(self.prompts) > 50:
                raise Runaway("runaway compile and repair loop")
            for _ in range(50):
                value, raw = answer({"latex": queue.pop(0) if len(queue) > 1 else queue[0]})
                try:
                    return settings["validate"](value, raw), raw
                except runtime.InvalidAnswer as exc:
                    self.problems.append(exc.problem)
            raise Runaway("every answer was rejected")
        return call

    def execute(self, answers, compile, state=None, which="/tools/tex"):
        with mock.patch.object(runtime, "structured", side_effect=self.model(answers)), \
                mock.patch.object(runtime.shutil, "which", return_value=which), \
                mock.patch.object(runtime, "_run_bounded", side_effect=compile):
            return runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"],
                                             state or {"solution": "Approved proof", "statement": "Task"}, {})

    def kinds(self, kind, label=None):
        return [event for event in self.events if event["kind"] == kind and (label is None or event.get("label") == label)]

    def compiled(self, argv, code=0, output="Output written on final.pdf"):
        if code == 0:
            (self.directory / "final.pdf").write_bytes(b"%PDF-1.5\ncompiled\n%%EOF")
        return subprocess.CompletedProcess(argv, code, output)


class LoopProgressTests(LatexCase):
    """I-013: a repair loop without progress ends with the source and a 'PDF not compiled' note."""

    def test_the_same_first_error_three_times_publishes_the_source_without_a_pdf(self):
        builds = []

        def compile(argv, **kwargs):
            builds.append((self.directory / "latex-source.tex").read_text())
            return self.compiled(argv, 12, "This is pdfTeX\n" + UNDEFINED + "\nLatexmk: Errors")

        state = self.execute([GOOD + " %1", GOOD + " %2", GOOD + " %3"], compile)
        self.assertEqual(len(builds), 3)
        self.assertEqual(len(self.prompts), 3)  # the editor and two repairs
        self.assertEqual(state["outcome"], "uncompiled")
        self.assertEqual((self.directory / "final.tex").read_text(), GOOD + " %3")
        self.assertFalse((self.directory / "final.pdf").exists())
        note = self.kinds("diagnostic", "PDF not compiled")
        self.assertEqual(len(note), 1)
        self.assertIn("3 attempts failed the same way", note[0]["text"])
        self.assertIn("./latex-source.tex:3: Undefined control sequence.", note[0]["text"])
        self.assertIn("latex-compile-N.log", note[0]["text"])
        final = self.kinds("final_result")
        self.assertEqual([event["label"] for event in final], ["LaTeX document without a compiled PDF"])
        self.assertEqual(final[0]["output"], GOOD + " %3")
        self.assertEqual(sorted(path.name for path in self.directory.glob("latex-compile-*.log")),
                         ["latex-compile-1.log", "latex-compile-2.log", "latex-compile-3.log"])

    def test_new_errors_are_progress_and_an_alternation_still_ends(self):
        errors = iter([f"./latex-source.tex:{line}: Undefined control sequence.\nl.{line} \\macro{line}\n"
                       for line in range(3, 8)])

        def compile(argv, **kwargs):
            error = next(errors, None)
            return self.compiled(argv, 12, error) if error else self.compiled(argv)

        state = self.execute([GOOD + f" %{index}" for index in range(7)], compile)
        self.assertEqual(state["outcome"], "pass")
        self.assertEqual(len(self.prompts), 6)  # five different errors, five repairs
        self.assertTrue((self.directory / "final.pdf").is_file())
        self.events.clear(), self.prompts.clear()
        a, b = UNDEFINED, "./latex-source.tex:5: Missing $ inserted.\nl.5 x^2\n"
        alternation = iter([a, b, a, b, a])

        def flip(argv, **kwargs):
            return self.compiled(argv, 12, next(alternation))

        state = self.execute([GOOD + f" %{index}" for index in range(7)], flip)
        self.assertEqual(state["outcome"], "uncompiled")
        self.assertEqual(len(self.prompts), 5)

    def test_a_repair_that_returns_an_already_failed_source_counts_as_no_progress(self):
        count = iter(range(100))

        def compile(argv, **kwargs):
            # A different report every time, from the same source.
            return self.compiled(argv, 12, f"./latex-source.tex:{next(count) + 3}: Emergency stop.\n")

        state = self.execute([GOOD], compile)
        self.assertEqual(state["outcome"], "uncompiled")
        self.assertEqual(len(self.prompts), 3)

    def test_a_finished_loop_leaves_no_failures_to_the_next_one(self):
        config = dict(runtime.builtin_workflow("clean_up")["nodes"]["latex_compile"]["command"])
        (self.directory / "latex-source.tex").write_text(GOOD)
        failed = {"signature": "./latex-source.tex:: Undefined control sequence. l. \\undefinedcommand", "input_sha256": "x"}
        transitions = {"pass": "end", "fail": "latex_repair", "uncompiled": "end"}
        for status, expected in (("uncompiled", "fail"), ("pass", "fail"), ("fail", "uncompiled")):
            state = {"compilation": {"status": status, "attempt": 5, "history": [failed, failed]}}
            with self.subTest(status), mock.patch.object(runtime.shutil, "which", return_value="/tools/tex"), \
                    mock.patch.object(runtime, "_run_bounded", side_effect=lambda argv, **kwargs: self.compiled(argv, 12, UNDEFINED)):
                result = runtime._command_step(config, state, {}, 1, "latex_compile", "final", transitions)
            self.assertEqual((result["status"], result["attempt"]), (expected, 6))
            self.assertTrue((self.directory / "latex-compile-6.log").is_file())

    def test_the_attempts_are_counted_in_the_step_status(self):
        outputs = iter([UNDEFINED, "./latex-source.tex:9: Missing $ inserted.\nl.9 y\n"])

        def compile(argv, **kwargs):
            output = next(outputs, None)
            return self.compiled(argv, 12, output) if output else self.compiled(argv)

        self.execute([GOOD + " %1", GOOD + " %2", GOOD + " %3"], compile)
        steps = [event for event in self.kinds("status", "Workflow step: latex_compile") if "attempt" in event]
        self.assertEqual([(event["attempt"], event["text"]) for event in steps],
                         [(2, "Attempt 2 of this step."), (3, "Attempt 3 of this step.")])
        self.assertEqual([event["text"] for event in self.kinds("diagnostic", "Fixing LaTeX compilation")],
                         ["Command exited with code 12: ./latex-source.tex:3: Undefined control sequence.",
                          "Command exited with code 12: ./latex-source.tex:9: Missing $ inserted."])

    def test_the_approved_proof_and_the_working_source_can_be_downloaded_before_the_pdf(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        runs = Path(folder.name)
        app = server.App(runs=runs)
        app._new_run("Exact statement")
        run = app.run_dir
        self.assertFalse(app.snapshot()["canDownloadProof"])
        self.assertFalse(app.snapshot()["canDownloadSource"])
        runtime.save_final_input("Exact statement", "The approved proof.", directory=run)
        (run / "latex-source.tex").write_text(GOOD)
        app.state.update(phase="running", stage="final", activeNode="latex_compile", finalInputReady=True)
        snapshot = app.snapshot()
        self.assertTrue(snapshot["canDownloadProof"])
        self.assertTrue(snapshot["canDownloadSource"])
        self.assertFalse(snapshot["canDownloadTex"])
        http = server.Server((server.HOST, 0), runs=runs)
        http.jobs[run.name] = app
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        headers = {"X-TCS-Prover-Token": http.token}
        for route, body, filename in (("proof", b"The approved proof.\n", "approved-proof.md"),
                                      ("source", GOOD.encode(), "latex-source.tex")):
            url = f"{http.origin}/download-{route}?job={run.name}"
            with self.assertRaises(HTTPError) as error:
                urlopen(url)
            self.assertEqual(error.exception.code, 403)
            with urlopen(Request(url, headers=headers)) as response:
                self.assertEqual(response.read(), body)
                self.assertEqual(response.headers["Content-Disposition"], f'attachment; filename="{filename}"')
        # Once final.tex is published, the .tex download replaces the source download.
        app.state["phase"] = "done"
        (run / "final.tex").write_text(GOOD)
        self.assertTrue(app.snapshot()["canDownloadTex"])
        self.assertFalse(app.snapshot()["canDownloadSource"])
        (run / "final.tex").unlink()
        (run / "latex-source.tex").unlink()
        (run / "latex-source.tex").symlink_to(run / "final-input.json")
        self.assertFalse(app.snapshot()["canDownloadSource"])
        with self.assertRaises(ValueError):
            app.latex_source()
        # Without the writing stage the proof is the job's own result: no separate download.
        app.state["workflowStages"] = {"review": False, "author": True, "critic": True, "writing": False}
        self.assertFalse(app.snapshot()["canDownloadProof"])
        with self.assertRaises(ValueError):
            app.approved_proof()

    def test_a_compile_restarted_without_the_editor_keeps_the_saved_sources_content(self):
        # Lifecycle restarts at latex_compile pass the saved source as the editor's version.
        noted = paper().replace(r"\end{document}", "% (appendix omitted)\n\\end{document}")
        builds = []

        def compile(argv, **kwargs):
            builds.append((self.directory / "latex-source.tex").read_text())
            return self.compiled(argv, 12, UNDEFINED) if len(builds) == 1 else self.compiled(argv)

        shrunk = noted.split(r"\section{Part 2}")[0] + r"\end{document}"
        with mock.patch.object(runtime, "structured", side_effect=self.model([shrunk, noted + " "])), \
                mock.patch.object(runtime.shutil, "which", return_value="/tools/tex"), \
                mock.patch.object(runtime, "_run_bounded", side_effect=compile):
            state = runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"],
                                              {"statement": "Task", "solution": "Proof", "output": noted,
                                               "edited_latex": noted}, {"start_node": "latex_compile"})
        self.assertEqual(state["outcome"], "pass")
        self.assertEqual(len(self.problems), 1)
        self.assertIn("drops content of the editor's version", self.problems[0])
        self.assertEqual(builds, [noted, noted + " "])


class TransientStartTests(LatexCase):
    """I-022: only a missing tool means 'not installed'; other start failures are retried every minute."""

    def test_errno_decides_between_missing_and_transient(self):
        config = {"argv": ["latexmk", "-jobname=final", "latex-source.tex"], "timeout": 5, "result": "compilation"}
        for error, transient in ((FileNotFoundError(errno.ENOENT, "No such file"), False),
                                 (PermissionError(errno.EACCES, "Permission denied"), False),
                                 (OSError(errno.ENOEXEC, "Exec format error"), False),
                                 (BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"), True),
                                 (OSError(errno.EMFILE, "Too many open files"), True)):
            with self.subTest(error=error), mock.patch.object(runtime.shutil, "which", return_value="/tools/latexmk"), \
                    mock.patch.object(runtime, "_run_bounded", side_effect=error):
                result = runtime._run_command(config, {"state": {}})
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(bool(result.get("transient")), transient)
            self.assertIn(f"[Errno {error.errno}]", result["diagnostic"])

    def test_a_start_failure_is_retried_every_minute_without_a_repair(self):
        failures = [BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")] * 2

        def compile(argv, **kwargs):
            if failures:
                raise failures.pop()
            return self.compiled(argv)

        state = self.execute([GOOD], compile)
        self.assertEqual(state["outcome"], "pass")
        self.assertEqual(len(self.prompts), 1)
        notices = [event for event in self.kinds("status", runtime.COMMAND_RETRY_LABEL) if "retryAt" in event]
        self.assertEqual(len(notices), 2)
        self.assertIn(f"[Errno {errno.EAGAIN}] Resource temporarily unavailable", notices[0]["text"])
        self.assertIn("Trying again in 1 min (attempt 1)", notices[0]["text"])
        self.assertEqual(round(sum(self.sleeps)), 120)
        self.assertTrue((self.directory / "final.pdf").is_file())

    def test_a_full_disk_while_compiling_is_retried_too(self):
        failures = [OSError(errno.ENOSPC, "No space left on device")]
        real = runtime._run_command

        def run_command(config, context, attempt=None):
            if failures:
                raise failures.pop()
            return real(config, context, attempt=attempt)

        with mock.patch.object(runtime, "_run_command", side_effect=run_command):
            state = self.execute([GOOD], lambda argv, **kwargs: self.compiled(argv))
        self.assertEqual(state["outcome"], "pass")
        self.assertIn("No space left on device", self.kinds("status", runtime.COMMAND_RETRY_LABEL)[0]["text"])

    def test_stop_and_pause_end_the_wait(self):
        pause = self.directory / "pause-request"
        self.on_sleep = lambda waited: pause.write_text("pause")
        with mock.patch.object(runtime, "structured", side_effect=self.model([GOOD])), \
                mock.patch.object(runtime.shutil, "which", return_value=None):
            state = runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"solution": "Proof"},
                                              {"goal_pause_file": str(pause)})
        self.assertTrue(state["paused"])
        paused = self.kinds("workflow_paused")
        self.assertEqual(paused[0]["node"], "latex_compile")
        self.on_sleep = lambda waited: (_ for _ in ()).throw(KeyboardInterrupt())
        with mock.patch.object(runtime, "structured", side_effect=self.model([GOOD])), \
                mock.patch.object(runtime.shutil, "which", return_value=None), self.assertRaises(KeyboardInterrupt):
            runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"solution": "Proof"}, {})

    def test_the_server_shows_the_wait_like_a_model_retry(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        app = server.App(runs=Path(folder.name))
        app._new_run("Exact statement")
        app._follow_model_retry({"kind": "status", "stage": "final", "node": "latex_compile",
                                 "label": runtime.COMMAND_RETRY_LABEL, "text": "Command unavailable: latexmk.",
                                 "retryAt": time.time() + 60, "retryAttempt": 1, "budgetClock": "paused"})
        self.assertEqual(app.state["modelRetry"]["text"], "Command unavailable: latexmk.")
        app._follow_model_retry({"kind": "status", "stage": "final", "node": "latex_compile",
                                 "label": runtime.COMMAND_RETRY_LABEL, "text": "Running the command again now.",
                                 "budgetClock": "running"})
        self.assertIsNone(app.state["modelRetry"])


class KillRaceTests(unittest.TestCase):
    """I-035, I-111, I-112: the isolated compiler's session is always stopped, with bounded waits."""

    def run_isolated(self, script, timeout, **patches):
        """compiler._run with the sandbox replaced by a plain interpreter running `script`."""
        started = []
        real = subprocess.Popen

        def popen(argv, **kwargs):
            process = real([sys.executable, "-c", script], **kwargs)
            started.append(process)
            return process

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(compiler.subprocess, "Popen", side_effect=popen))
            for target in patches.get("extra", ()):
                stack.enter_context(mock.patch.object(*target))
            try:
                return compiler._run("(version 1)", ["latexmk"], Path(folder.name), dict(os.environ), timeout), started
            except BaseException as exc:
                exc.started = started
                raise

    def assertStopped(self, process):
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(process.poll(), "the compile outlived its session")

    def test_a_kill_that_meets_an_exiting_session_is_a_normal_timeout(self):
        def killpg(pid, sig):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        script = "import time; print('working', flush=True); time.sleep(60)"
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            self.run_isolated(script, 1, extra=[(compiler.os, "killpg", killpg)])
        self.assertIn("working", raised.exception.stdout)
        self.assertStopped(raised.exception.started[0])
        tools = (Path("/usr/local/texlive"), Path("/Library/TeX/texbin"), Path("/Library/TeX/texbin/latexmk"))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        workspace = Path(folder.name).resolve()
        (workspace / "latex-source.tex").write_text(GOOD)
        policy = {"isolation": {"adapter": compiler.ADAPTER, "workspace": str(workspace)}}
        config = {"argv": compiler.LATEX_ARGV, "requires": ["pdflatex"], "env": {"openin_any": "p", "openout_any": "p"},
                  "timeout": 300, "produces": ["final.pdf"], "log": "latex-compile.log", "result": "compilation"}
        with mock.patch.object(compiler, "supported", return_value=True), \
                mock.patch.object(compiler, "toolchain", return_value=tools), \
                mock.patch.object(compiler, "verify_sandbox"), \
                mock.patch.object(compiler, "_run", side_effect=subprocess.TimeoutExpired(["latexmk"], 300, "loop")):
            result = compiler.compile_command(policy, config, compiler.LATEX_ARGV, workspace)
        self.assertEqual((result["status"], result["timed_out"]), ("fail", True))
        self.assertEqual(result["diagnostic"], "Command exceeded 300 seconds.")

    def test_stop_during_a_compile_kills_its_session(self):
        calls = {"count": 0}
        real = time.monotonic

        def monotonic():
            calls["count"] += 1
            if calls["count"] == 3:
                raise KeyboardInterrupt
            return real()

        script = "import time; print('working', flush=True); time.sleep(60)"
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.run_isolated(script, 30, extra=[(compiler.time, "monotonic", monotonic)])
        self.assertStopped(raised.exception.started[0])

    def test_a_pipe_held_outside_the_session_delays_the_timeout_by_seconds_only(self):
        # A grandchild in its own session keeps the output pipe open for 30 s.
        script = ("import subprocess, sys, time; holder = subprocess.Popen([sys.executable, '-c', 'import time; "
                  "time.sleep(30)'], start_new_session=True); print('holder', holder.pid, flush=True); time.sleep(60)")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            self.run_isolated(script, 1)
        self.assertLess(time.monotonic() - started, 15)
        holder = int(raised.exception.stdout.split("holder ")[1].split()[0])
        self.addCleanup(lambda: os.kill(holder, signal.SIGKILL) if _alive(holder) else None)
        self.assertStopped(raised.exception.started[0])

    def test_runaway_output_stops_the_compile_and_keeps_its_ends(self):
        # The stand-in prints 24 MB at most, so a regression cannot run away itself.
        script = "import sys, time\nfor _ in range(300000): sys.stdout.write('runaway line ' * 6 + '\\n')\ntime.sleep(30)"
        started = time.monotonic()
        with mock.patch.object(compiler, "OUTPUT_LIMIT", 2_000_000), \
                mock.patch.object(compiler._Output.__init__, "__defaults__", (1000,)), \
                self.assertRaises(subprocess.TimeoutExpired) as raised:
            self.run_isolated(script, 20)
        self.assertLess(time.monotonic() - started, 10, "stopped only by its time limit")
        self.assertEqual(getattr(raised.exception, "runaway", ""), "printed more than 1 MB of output")
        self.assertRegex(raised.exception.stdout, r"\n\[\d+ characters omitted\]\n")
        self.assertLess(len(raised.exception.stdout), 2200)
        self.assertStopped(raised.exception.started[0])


class TimeoutTests(LatexCase):
    """I-045: a timed-out compile runs again with longer limits; then the repair is told it did not finish."""

    def test_the_base_limit_is_five_minutes_and_longer_limits_come_before_any_repair(self):
        command = runtime.builtin_workflow("clean_up")["nodes"]["latex_compile"]["command"]
        self.assertEqual(command["timeout"], 300)
        limits = []

        def compile(argv, timeout, **kwargs):
            limits.append(timeout)
            if timeout < 1800:
                raise subprocess.TimeoutExpired(argv, timeout, output=b"[1] [2] [3]")
            return self.compiled(argv)

        state = self.execute([GOOD], compile)
        self.assertEqual(limits, [300, 900, 1800])
        self.assertEqual(state["outcome"], "pass")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual([event["text"] for event in self.kinds("status", "Workflow step: latex_compile") if "attempt" in event],
                         ["Attempt 1 did not finish within 5 min; attempt 2 may run 15 min.",
                          "Attempt 2 did not finish within 15 min; attempt 3 may run 30 min."])

    def test_a_compile_that_never_finishes_gets_the_infinite_loop_prompt(self):
        limits = []

        def compile(argv, timeout, **kwargs):
            limits.append(timeout)
            if len(limits) <= 3:
                raise subprocess.TimeoutExpired(argv, timeout, output=b"looping")
            return self.compiled(argv)

        state = self.execute([GOOD + " %loop", GOOD], compile)
        self.assertEqual(limits, [300, 900, 1800, 300])
        self.assertEqual(state["outcome"], "pass")
        repair = self.prompts[1]
        self.assertTrue(repair.startswith("The LaTeX compilation below did not finish. Check the source for an infinite loop"))
        self.assertIn("Command exceeded 1800 seconds. Earlier attempts stopped after 5 min and 15 min.", repair)
        self.assertNotIn("Fix only the LaTeX compilation errors", repair)

    def test_runaway_output_goes_to_the_repair_without_longer_runs(self):
        limits = []

        def compile(argv, timeout, **kwargs):
            limits.append(timeout)
            if len(limits) == 1:
                runaway = subprocess.TimeoutExpired(argv, timeout, output="loop " * 10)
                runaway.runaway = "printed more than 256 MB of output"
                raise runaway
            return self.compiled(argv)

        self.execute([GOOD + " %loop", GOOD], compile)
        self.assertEqual(limits, [300, 300])
        self.assertIn("Command printed more than 256 MB of output and was stopped.", self.prompts[1])
        self.assertTrue(self.prompts[1].startswith("The LaTeX compilation below did not finish."))


class AllErrorsTests(LatexCase):
    """I-049 and I-059: one compile reports every error, and a long output keeps them and its end."""

    def test_both_compilers_run_without_halt_on_error_and_agree(self):
        argv = runtime.builtin_workflow("clean_up")["nodes"]["latex_compile"]["command"]["argv"]
        self.assertEqual(argv, compiler.LATEX_ARGV)
        self.assertNotIn("-halt-on-error", argv)
        self.assertIn("-interaction=nonstopmode", argv)

    def test_a_long_output_keeps_every_error_then_its_end(self):
        errors = [f"./latex-source.tex:{line}: Undefined control sequence.\nl.{line} \\bad{line}\n" for line in (5, 9, 14)]
        output = "Banner\n" + "Underfull \\hbox in paragraph\n" * 2000 + "\n".join(errors) + "filler line\n" * 3000 + "THE END\n"
        excerpt = runtime._command_excerpt(output, runtime._tex_errors(output))
        self.assertLessEqual(len(excerpt), runtime.COMMAND_OUTPUT_CHARS)
        for error in errors:
            self.assertIn(error.strip(), excerpt)
        self.assertTrue(excerpt.rstrip().endswith("THE END"))
        omitted = int(excerpt.split("[")[-1].split(" earlier characters omitted]")[0])
        self.assertEqual(omitted, len(output) - len(excerpt.split("earlier characters omitted]\n", 1)[1]))
        short = "Short output\n" + errors[0]
        self.assertEqual(runtime._command_excerpt(short, runtime._tex_errors(short)), short)

    def test_an_omitted_middle_is_counted_in_the_marker(self):
        output = "x" * 30000 + "\n[1000000 characters omitted]\n" + "y" * 30000
        excerpt = runtime._command_excerpt(output)
        omitted = int(excerpt.split("[")[1].split(" earlier")[0])
        self.assertEqual(omitted, len(output) - len(excerpt.split("omitted]\n", 1)[1]) + 1000000
                         - len("\n[1000000 characters omitted]\n"))

    def test_error_blocks_keep_their_context_lines(self):
        output = ("(./final.aux)\n./latex-source.tex:4: Undefined control sequence.\nl.4 \\undefinedcommandone\n"
                  "                         See Section~\\ref{proof}.\n\nLaTeX Warning: Reference\n"
                  "! LaTeX Error: File `nonexistent.sty' not found.\n\nType X to quit\n"
                  "Latexmk: Missing input file\n  '! LaTeX Error: File `x.sty' not found.'\n")
        blocks = runtime._tex_errors(output)
        self.assertEqual(blocks, ["./latex-source.tex:4: Undefined control sequence.\nl.4 \\undefinedcommandone\n"
                                  "                         See Section~\\ref{proof}.",
                                  "! LaTeX Error: File `nonexistent.sty' not found."])

    @unittest.skipUnless(HAS_TEX, "TeX is not installed")
    def test_real_latex_reports_three_errors_in_one_compile(self):
        broken = GOOD.replace("See Section", r"\undefinedone See Section").replace(
            r"\end{document}", r"\undefinedtwo{x} $\undefinedthree$\end{document}")
        with mock.patch.object(runtime, "structured", side_effect=self.model([broken, GOOD])):
            state = runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": "Paper"}, {})
        self.assertEqual(state["outcome"], "pass")
        self.assertEqual(len(self.prompts), 2)
        for name in ("undefinedone", "undefinedtwo", "undefinedthree"):
            self.assertIn("\\" + name, self.prompts[1].split("LATEX SOURCE:")[0])
        self.assertIn("(3 errors in all.)", self.prompts[1])


class UnresolvedReferenceTests(LatexCase):
    """I-050: unresolved citations or references go to the repair, which is told to inline the bibliography."""

    # TeX breaks log lines at 79 characters.
    LONG = "Package natbib Warning: Citation `averyveryveryveryveryverylongcitationkeyfortest' on page 1 undefined on input line 5."
    LOG = ("LaTeX Warning: Citation `knuth' on page 1 undefined on input line 4.\n\n"
           + LONG[:79] + "\n" + LONG[79:] + "\n\nLaTeX Warning: There were undefined references.\n")

    def test_the_editor_is_asked_for_an_inline_bibliography(self):
        self.assertIn("inline thebibliography environment", runtime.builtin_workflow("clean_up")["prompts"]["final"])

    def test_the_log_is_read_with_wrapped_lines(self):
        log = self.directory / "final.log"
        log.write_text(self.LOG)
        names, found = runtime._tex_unresolved(log)
        self.assertTrue(found)
        self.assertEqual(names, ["citation knuth", "citation averyveryveryveryveryverylongcitationkeyfortest"])
        log.write_text("LaTeX Warning: There were undefined references.\n")
        self.assertEqual(runtime._tex_unresolved(log), ([], True))
        log.write_text("Output written on final.pdf\n")
        self.assertEqual(runtime._tex_unresolved(log), ([], False))
        log.unlink()
        log.symlink_to(self.directory / "missing")
        self.assertEqual(runtime._tex_unresolved(log), ([], False))

    def test_unresolved_references_are_repaired_and_a_persistent_one_keeps_its_pdf(self):
        def compile(argv, **kwargs):
            (self.directory / "final.log").write_text(self.LOG if "thebibliography" not in
                                                      (self.directory / "latex-source.tex").read_text() else "")
            return self.compiled(argv)

        fixed = GOOD.replace(r"\end{document}", r"\begin{thebibliography}{1}\bibitem{knuth}K.\end{thebibliography}\end{document}")
        state = self.execute([GOOD, fixed], compile)
        self.assertEqual(state["outcome"], "pass")
        self.assertIn("LaTeX left 2 citations undefined: citation knuth", self.prompts[1])
        self.assertIn("inline thebibliography environment", self.prompts[1])
        self.assertEqual((self.directory / "final.tex").read_text(), fixed)
        self.events.clear(), self.prompts.clear()
        (self.directory / "final.tex").unlink()
        state = self.execute([GOOD], compile)
        self.assertEqual(state["outcome"], "pass")
        self.assertEqual(len(self.prompts), 3)
        self.assertTrue((self.directory / "final.pdf").is_file())
        self.assertEqual(len(self.kinds("diagnostic", "References left unresolved")), 1)
        self.assertEqual([event["label"] for event in self.kinds("final_result")], ["Compiled LaTeX document and PDF"])

    @unittest.skipUnless(HAS_TEX, "TeX is not installed")
    def test_real_latex_unresolved_citation_is_sent_to_the_repair(self):
        cited = GOOD.replace("See Section", r"See \cite{knuth} and Section")
        fixed = cited.replace(r"\end{document}", r"\begin{thebibliography}{1}\bibitem{knuth}D. Knuth.\end{thebibliography}\end{document}")
        with mock.patch.object(runtime, "structured", side_effect=self.model([cited, fixed])):
            state = runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": "Paper"}, {})
        self.assertEqual(state["outcome"], "pass")
        self.assertIn("citation knuth", self.prompts[1])
        self.assertNotIn("undefined references", (self.directory / "final.log").read_text())


class ContentGuardTests(LatexCase):
    """I-051 and I-029: answers that drop content or carry JSON-escape damage are re-asked."""

    def test_content_losses_against_the_editors_version_are_rejected(self):
        reference = paper()
        self.assertTrue(runtime._latex_kept(reference, reference))
        # Commented-out text still counts as kept; one broken macro may go.
        self.assertTrue(runtime._latex_kept(reference.replace(r"\section{Part 3}", r"%\section{Part 3}"), reference))
        self.assertTrue(runtime._latex_kept(reference.replace("Careful argument. ", "", 3), reference))
        cases = {
            "elision": (reference.split(r"\section{Part 4}")[0] + "% ... rest of the document unchanged ...\n\\end{document}",
                        "a comment in place of text"),
            "shorter": (reference.replace("Careful argument. " * 20, "Short.", 6), "it keeps"),
            "environments": (reference.replace(r"\begin{lemma}", r"\textbf{Lemma.}", 4).replace(r"\end{lemma}", "", 4),
                             "4 of its 24 theorem-like environments are missing"),
            "labels": (reference.replace(r"\label{sec:1}", "").replace(r"\label{sec:2}", ""),
                       "2 of its 12 labels are missing, such as sec:1, sec:2"),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name), self.assertRaises(runtime._RuleProblem) as raised:
                runtime._latex_kept(text, reference)
            self.assertIn(expected, str(raised.exception))
        # A comment the input already had is not the answer's elision: the editor keeps a user's own.
        noted = reference.replace(r"\end{document}", "% (appendix omitted)\n\\end{document}")
        self.assertTrue(runtime._latex_kept(noted, noted))
        self.assertTrue(runtime._latex_intact(noted, "Supplied LaTeX ... % (appendix omitted)\n"))
        with self.assertRaises(runtime._RuleProblem):
            runtime._latex_intact(noted, "")
        # Without the editor's version (an older checkpoint) only the answer itself is checked.
        self.assertTrue(runtime._latex_kept(reference.split(r"\section{Part 2}")[0] + r"\end{document}", ""))

    def test_json_escape_damage_is_rejected_with_an_escaping_reminder(self):
        for damaged, example in (("\\documentclass{article}\x08egin{document}", 'a backspace before "egin" where \\begin was meant'),
                                 ("$\x0crac{1}{2}$", 'a form feed before "rac" where \\frac was meant'),
                                 ("$\\left(x\right)$", 'a carriage return before "ight" where \\right was meant'),
                                 ("A \textbf{bold} claim", 'a tab before "extbf" where \\textbf was meant'),
                                 ("Line one\newline two", 'a line break before "ewline" where \\newline was meant'),
                                 ("$x \to y$", 'a tab before "o" where \\to was meant')):
            with self.subTest(example), self.assertRaises(runtime._RuleProblem) as raised:
                runtime._latex_intact(damaged)
            self.assertIn(example, str(raised.exception))
            self.assertIn("write each LaTeX backslash as two backslashes in the JSON string", str(raised.exception))
        for fine in ("Windows lines\r\nend", "\tIndented text\n\\begin{proof}", "Line\nnew idea", "i.e.\ne.g.\nequation",
                     "\textra tab text"):
            self.assertTrue(runtime._latex_intact(fine), fine)
        # What the input itself contains is the input's, not damage done by the answer.
        self.assertTrue(runtime._latex_intact("Kept \to as typed", "User text: Kept \to as typed"))

    def test_the_workflow_re_asks_a_damaged_editor_answer_and_a_shrunken_repair(self):
        edited = paper()
        broken = edited.replace(r"\end{document}", r"\undefinedcommand\end{document}")
        shrunk = edited.split(r"\section{Part 2}")[0] + r"\end{document}"
        builds = []

        def compile(argv, **kwargs):
            builds.append((self.directory / "latex-source.tex").read_text())
            return self.compiled(argv, 12, UNDEFINED) if len(builds) == 1 else self.compiled(argv)

        state = self.execute([broken.replace("\\textbf", "\textbf").replace(r"\begin{proof}", "\x08egin{proof}", 1),
                              broken, shrunk, edited], compile)
        self.assertEqual(state["outcome"], "pass")
        self.assertEqual(len(self.problems), 2)
        self.assertIn("JSON escapes made of LaTeX commands", self.problems[0])
        self.assertIn("drops content of the editor's version", self.problems[1])
        self.assertEqual(builds, [broken, edited])
        self.assertEqual(state["edited_latex"], broken)


class IsolatedRefusalTests(LatexCase):
    """I-061 and I-069: the isolated compiler's refusals finish without a PDF; failed checks are retried."""

    def setUp(self):
        super().setUp()
        self.policy = {"isolation": {"adapter": compiler.ADAPTER, "workspace": str(self.directory)}}
        patcher = mock.patch.object(runtime, "retrieval_policy", lambda *args, **kwargs: self.policy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def isolated(self, effects):
        def compile_command(policy, config, argv, directory):
            effect = effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            (directory / "final.pdf").write_bytes(b"%PDF-1.5\n%%EOF")
            return {"status": "pass", "diagnostic": "Isolated LaTeX command exited with code 0.", "returncode": 0,
                    "output": "Output written on final.pdf"}
        return mock.patch.object(compiler, "compile_command", side_effect=compile_command)

    def test_a_source_over_the_size_limit_publishes_the_source_without_a_pdf(self):
        (self.directory / "final.pdf").write_bytes(b"%PDF-1.5\nfrom an older attempt\n%%EOF")
        with self.isolated([compiler.CompilerRefusal("Compiler input exceeds its size limit: latex-source.tex")]), \
                mock.patch.object(runtime, "structured", side_effect=self.model([GOOD])):
            state = runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": "Paper"}, {})
        self.assertEqual(state["outcome"], "uncompiled")
        self.assertEqual((self.directory / "final.tex").read_text(), GOOD)
        self.assertFalse((self.directory / "final.pdf").exists())
        self.assertIn("refused to compile: Compiler input exceeds its size limit",
                      self.kinds("diagnostic", "PDF not compiled")[0]["text"])
        self.assertEqual(len(self.prompts), 1)

    def test_failed_toolchain_or_sandbox_checks_are_checked_again_every_minute(self):
        effects = [compiler.CompilerNotReady("Compiler toolchain is not root-owned and protected: /usr/local/texlive"),
                   compiler.CompilerNotReady("Compiler sandbox canaries failed (exit 1): outside read allowed"), None]
        with self.isolated(effects), mock.patch.object(runtime, "structured", side_effect=self.model([GOOD])):
            state = runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": "Paper"}, {})
        self.assertEqual(state["outcome"], "pass")
        notices = [event for event in self.kinds("status", runtime.COMMAND_RETRY_LABEL) if "retryAt" in event]
        self.assertEqual(len(notices), 2)
        self.assertIn("The isolated compiler is not ready: Compiler toolchain is not root-owned", notices[0]["text"])
        self.assertEqual(len(self.prompts), 1)
        self.assertTrue((self.directory / "final.pdf").is_file())

    def test_an_unsupported_command_is_still_a_visible_stop(self):
        # The step pauses with its input (graph I-002) and, as no retry can change the command, waits for Resume.
        with self.isolated([compiler.CompilerIsolationError("The isolated compiler/command adapter supports only the "
                                                            "built-in LaTeX command.")]), \
                mock.patch.object(runtime, "structured", side_effect=self.model([GOOD])):
            state = runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": "Paper"}, {})
        self.assertTrue(state["paused"])
        pause = self.kinds("workflow_paused")[-1]
        self.assertEqual(pause["node"], "latex_compile")
        self.assertTrue(pause["safetyStop"])
        self.assertTrue(pause["reason"].startswith("The isolated compiler/command adapter supports only the built-in "
                                                   "LaTeX command."))
        self.assertFalse((self.directory / "final.tex").exists())

    def test_boundary_failures_are_classified(self):
        self.assertTrue(issubclass(compiler.CompilerNotReady, compiler.CompilerIsolationError))
        self.assertTrue(issubclass(compiler.CompilerRefusal, compiler.CompilerIsolationError))
        large = self.directory / "large.tex"
        large.write_bytes(b"x" * 20)
        with self.assertRaises(compiler.CompilerRefusal):
            compiler._regular_file(large, 10)
        with mock.patch.object(compiler.os, "open", side_effect=OSError(errno.EMFILE, "Too many open files")), \
                self.assertRaises(OSError) as raised:
            compiler._regular_file(large)
        self.assertNotIsInstance(raised.exception, compiler.CompilerIsolationError)
        with mock.patch.object(compiler, "_root_owned", side_effect=OSError(errno.ENOENT, "No such file")), \
                self.assertRaises(compiler.CompilerNotReady):
            compiler.toolchain()


class BoundedOutputTests(LatexCase):
    """I-124: runaway output is bounded in memory and in the logs; a failed log write is only a diagnostic."""

    def test_the_output_keeps_its_head_and_tail(self):
        for keeper in (runtime._CommandOutput(keep=10), compiler._Output(keep=10)):
            for part in ("0123456789", "abcdefghij", "KLMNOPQRST", "uvwxyz"):
                keeper.add(part)
            self.assertEqual(keeper.text(), "0123456789\n[16 characters omitted]\nQRSTuvwxyz")
            self.assertEqual(keeper.size, 36)

    def test_a_runaway_command_is_stopped_with_its_ends_kept(self):
        # The stand-in prints 24 MB at most, so a regression cannot run away itself.
        script = "import sys, time\nfor _ in range(300000): sys.stdout.write('runaway line ' * 6 + '\\n')\ntime.sleep(30)"
        started = time.monotonic()
        with mock.patch.object(runtime, "COMMAND_OUTPUT_LIMIT", 3_000_000), \
                mock.patch.object(runtime._CommandOutput.__init__, "__defaults__", (500,)), \
                self.assertRaises(subprocess.TimeoutExpired) as raised:
            runtime._run_bounded([sys.executable, "-c", script], timeout=20, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        self.assertLess(time.monotonic() - started, 10, "stopped only by its time limit")
        self.assertEqual(getattr(raised.exception, "runaway", ""), "printed more than 2 MB of output")
        self.assertRegex(raised.exception.stdout, r"(?s)^runaway line .*\n\[\d+ characters omitted\]\n")
        self.assertLess(len(raised.exception.stdout), 1200)

    def test_a_log_that_grows_without_end_stops_the_command(self):
        # The stand-in writes 64 MB at most, so a regression cannot fill the disk.
        log = self.directory / "final.log"
        script = (f"import time\nf = open({str(log)!r}, 'w')\nfor _ in range(1024): f.write('x' * 65536); f.flush()\n"
                  "time.sleep(30)")
        started = time.monotonic()
        with mock.patch.object(runtime, "COMMAND_OUTPUT_LIMIT", 4 * 2 ** 20), \
                self.assertRaises(subprocess.TimeoutExpired) as raised:
            runtime._run_bounded([sys.executable, "-c", script], timeout=20, watch=[log], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        self.assertLess(time.monotonic() - started, 10, "stopped only by its time limit")
        self.assertEqual(getattr(raised.exception, "runaway", ""), "wrote more than 4 MB to final.log")

    def test_the_isolated_copies_of_large_logs_keep_their_ends(self):
        log = self.directory / "stage.log"
        log.write_bytes(b"H" * 100 + b"M" * 1000 + b"T" * 100)
        data = compiler._regular_file(log, keep=200)
        self.assertEqual(data, b"H" * 100 + b"\n[1000 bytes omitted]\n" + b"T" * 100)

    def test_a_failed_log_write_is_a_diagnostic_and_the_compile_continues(self):
        real = runtime._private_atomic_write

        def write(path, content, **kwargs):
            if Path(path).name.startswith("latex-compile"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, content, **kwargs)

        with mock.patch.object(runtime, "_private_atomic_write", side_effect=write):
            state = self.execute([GOOD], lambda argv, **kwargs: self.compiled(argv))
        self.assertEqual(state["outcome"], "pass")
        self.assertTrue(any("latex-compile.log could not be written" in event["text"]
                            for event in self.kinds("diagnostic")))


class ToolLookupTests(LatexCase):
    """I-068: TeX is found in its standard folders, checked at launch, and waited for at the end."""

    def test_tools_outside_path_are_found_in_the_tex_folders_and_put_on_the_childs_path(self):
        folder = self.directory / "texlive" / "2031" / "bin" / "universal-darwin"
        folder.mkdir(parents=True)
        for name in ("latexmk", "pdflatex"):
            (folder / name).write_text("#!/bin/sh\nexit 0\n")
            (folder / name).chmod(0o755)
        patterns = (str(self.directory / "texlive" / "*" / "bin" / "*"),)
        with mock.patch.object(runtime, "TEX_TOOL_FOLDERS", patterns), \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.assertEqual(runtime._find_tool("latexmk"), str(folder / "latexmk"))
            self.assertIsNone(runtime._find_tool("missing-tool"))
            seen = {}

            def run(argv, **kwargs):
                seen.update(argv=argv, path=kwargs["env"]["PATH"])
                return self.compiled(argv)

            config = runtime.builtin_workflow("clean_up")["nodes"]["latex_compile"]["command"]
            (self.directory / "latex-source.tex").write_text(GOOD)
            with mock.patch.object(runtime, "_run_bounded", side_effect=run):
                self.assertEqual(runtime._run_command(config, {"state": {}})["status"], "pass")
        self.assertEqual(seen["argv"][0], str(folder / "latexmk"))
        self.assertEqual(seen["path"], f"/usr/bin:/bin:{folder}")

    def test_missing_tools_are_reported_when_the_run_starts(self):
        workflows = [runtime.load_workflow(runtime.WORKFLOWS / name) for name in ("author_critic.yaml", "clean_up.yaml")]
        with mock.patch.object(runtime, "_find_tool", side_effect=lambda name: None if name == "pdflatex" else "/tex/" + name):
            self.assertEqual(runtime._missing_command_tools(workflows), ["pdflatex"])
        found = []

        def find(name):
            return "/tex/" + name if found else None

        self.on_sleep = lambda waited: found.append(True)
        with mock.patch.object(runtime, "_find_tool", side_effect=find), \
                mock.patch.object(runtime, "structured", side_effect=self.model([GOOD])), \
                mock.patch.object(runtime, "_run_bounded", side_effect=lambda argv, **kwargs: self.compiled(argv)):
            runtime.execute_workflows([runtime.WORKFLOWS / "clean_up.yaml"], {"source": "Paper"}, {})
        launch = self.kinds("diagnostic", "Compiler tools not found")
        self.assertEqual(len(launch), 1)
        self.assertLess(self.events.index(launch[0]), self.events.index(self.kinds("status")[0]))
        self.assertIn("latexmk, pdflatex not found on PATH or in the standard TeX folders", launch[0]["text"])
        self.assertTrue((self.directory / "final.pdf").is_file())

    def test_a_run_without_its_writing_stage_needs_no_tools(self):
        # The server launches both graphs for every selection of stages.
        workflows = [runtime.load_workflow(runtime.WORKFLOWS / name) for name in ("author_critic.yaml", "clean_up.yaml")]
        with mock.patch.object(runtime, "_find_tool", return_value=None):
            self.assertEqual(runtime._missing_command_tools(workflows, {"workflow_stages": {"writing": False}}), [])
            self.assertEqual(runtime._missing_command_tools(workflows, {"workflow_stages": {"author": False}}),
                             ["latexmk", "pdflatex"])


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class DiagnosticLabelTests(unittest.TestCase):
    """I-130: the activity log shows a diagnostic's own label."""

    def test_diagnostics_keep_their_label(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function describe(entry)'), source.indexOf('function cardIcon('));
const context = {state: {}, assert};
vm.runInNewContext(code + `
assert.equal(describe({kind: 'diagnostic', label: 'Fixing LaTeX compilation', text: 'x', time: 't'}).label,
             'Fixing LaTeX compilation');
assert.equal(describe({kind: 'diagnostic', text: 'x', time: 't'}).label, 'Diagnostic');
`, context);
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js")],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
