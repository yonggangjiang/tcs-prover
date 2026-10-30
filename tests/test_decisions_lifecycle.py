"""The lifecycle decisions: run ownership, restarts, shutdown, damaged runs, readers and the terminal runner.

No test starts a model or a CLI. Launches are mocked (subprocess.Popen) or are
small Python stand-ins (sleep, exit); HOME, CODEX_HOME, CLAUDE_CONFIG_DIR and
the private state folder point at a temporary folder.
"""

import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import critic_doubles
from runner import claude_cli, providers
from ui import cli, live_gateway, ownership, server
import workflow_runner as runtime
import web_ui

WAIT = server.AUTO_RESUME_SECONDS
ROOT = Path(server.__file__).resolve().parents[1]
POSIX = os.name != "nt"


class IsolatedCase(unittest.TestCase):
    """Keep every test away from the user's CLI homes, saved API keys and private state."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {
            "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            providers.STORE_ENV: str(self.home / "state" / "api-models.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.runs = self.home / "runs"
        self.runs.mkdir()

    def author_job(self, statement="Exact statement", **state):
        """A job whose author is working, as the reader sees it."""

        app = server.App(runs=self.runs, load_latest=False)
        app._new_run(statement)
        app._save("checked-statement.md", f"# Checked statement\n\n{statement}\n")
        app.state.update(phase="running", stage="solve", activeNode="author", runId=app.run_dir.name,
                         goalThreadId="saved-thread", elapsedSeconds=12,
                         startedAt=server.datetime.now(server.timezone.utc).isoformat(), **state)
        app._save_job_settings(app.state)
        return app

    @staticmethod
    def records(app, *records):
        for record in records:
            app.add_trace(record)

    @staticmethod
    def checkpoint(app):
        return json.loads((app.run_dir / server.PAUSE_FILENAME).read_text(encoding="utf-8"))

    @staticmethod
    def restored(app):
        return server.restore_one_run(app.trace_file, app.runs)

    @staticmethod
    def launches(popen):
        return [call.args[0] for call in popen.call_args_list]


@contextlib.contextmanager
def foreign_owner(folder):
    """Hold `folder`'s owner lock from another process, as a terminal run or an orphaned runner would."""

    script = ("import sys; sys.path.insert(0, sys.argv[1]); from ui import ownership; "
              "ownership.ROLE = 'terminal run'; ownership.claim(sys.argv[2]); print('locked', flush=True); "
              "sys.stdin.read()")
    child = subprocess.Popen([sys.executable, "-c", script, str(ROOT), str(folder)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        assert child.stdout.readline().strip() == "locked"
        yield child
    finally:
        child.stdin.close()
        child.wait(timeout=10)
        child.stdout.close()


def launch_mock():
    return Mock(stdin=io.StringIO(), pid=None)


@unittest.skipUnless(POSIX, "flock(2) owner locks are a POSIX feature")
class OwnerLockTests(IsolatedCase):
    """I-096: a run owned by another process is shown as running elsewhere and never started here."""

    def test_another_process_holds_the_lock_and_its_runner_inherits_it(self):
        run = self.runs / "job"
        run.mkdir()
        with foreign_owner(run) as child:
            holder = ownership.held_elsewhere(run)
            self.assertEqual((holder["pid"], holder["role"]), (child.pid, "terminal run"))
            with self.assertRaises(ownership.OwnedElsewhere) as raised:
                ownership.claim(run)
            self.assertIn(f"PID {child.pid}", str(raised.exception))
        self.assertIsNone(ownership.held_elsewhere(run))
        # Jobs of one process share the lock; a runner started with it keeps it after the owner lets go.
        first, second = ownership.Claims(), ownership.Claims()
        descriptor = first.claim(run)
        self.assertEqual(second.claim(run), descriptor)
        runner = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                  **server.runner_lock_options([descriptor]))
        first.release_all()
        second.release_all()
        try:
            self.assertIsNotNone(ownership.held_elsewhere(run))
        finally:
            runner.stdin.close()
            runner.wait(timeout=10)
        self.assertIsNone(ownership.held_elsewhere(run))
        # Nothing is added to the runs folder itself.
        self.assertEqual([path.name for path in self.runs.iterdir()], ["job"])

    def test_launches_claim_the_run_and_pass_the_lock_to_the_runner(self):
        app = self.author_job()
        app.state.update(phase="paused")
        app._save_pause({"status": "paused", "node": "author", "stage": "solve", "state": {"statement": "S"},
                         "elapsedSeconds": 12, "pauseCause": "user", "autoResumeAt": 0})
        with patch.object(server.subprocess, "Popen", return_value=launch_mock()) as popen, \
                patch.object(server, "require_model_access"), patch.object(app, "_spawn_worker"):
            app.resume()
        options = popen.call_args.kwargs
        self.assertEqual(options["pass_fds"], (ownership._held[str(app.run_dir.resolve())][0],))
        app.state.update(phase="done")
        app.worker_token = None
        app._settle_run_claim()
        self.assertNotIn(str(app.run_dir.resolve()), ownership._held)

    def test_a_run_owned_elsewhere_is_restored_read_only_and_taken_over_when_free(self):
        app = self.author_job()
        self.records(app, {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        settings = (app.run_dir / server.JOB_SETTINGS_FILENAME).read_bytes()
        with foreign_owner(app.run_dir):
            http = server.Server((server.HOST, 0), runs=self.runs)
            self.addCleanup(http.server_close)
            job = http.jobs[app.run_dir.name]
            self.assertEqual(job.state["phase"], "running")
            self.assertIn("terminal run", job.state["runningElsewhere"])
            self.assertEqual(job.state["autoResumeAt"], 0)
            self.assertFalse((app.run_dir / server.PAUSE_FILENAME).exists())
            self.assertEqual((app.run_dir / server.JOB_SETTINGS_FILENAME).read_bytes(), settings)
            row = next(row for row in http.job_list() if row["runId"] == app.run_dir.name)
            self.assertIn("terminal run", row["runningElsewhere"])
            self.assertFalse(row["canContinueStopped"])
            with patch.object(server.subprocess, "Popen") as popen:
                for action in (job.pause, job.stop, job.resume,
                               lambda: http.delete_job(app.run_dir.name),
                               lambda: http.continue_stopped_job(app.run_dir.name)):
                    with self.assertRaises(ValueError):
                        action()
                http._adopt_released_runs()
            popen.assert_not_called()
            self.assertIs(http.jobs[app.run_dir.name], job)
        # Once the other process ended, the job is read again from its files and continues here.
        http._adopt_released_runs()
        adopted = http.jobs[app.run_dir.name]
        self.assertIsNot(adopted, job)
        self.assertEqual(adopted.state["runningElsewhere"], "")
        self.assertEqual((adopted.state["phase"], self.checkpoint(adopted)["pauseCause"]), ("paused", "interrupted"))
        self.assertGreater(adopted.state["autoResumeAt"], time.time())

    def test_a_continuation_refuses_a_workspace_owned_elsewhere(self):
        source = self.author_job("Source")
        source.state.update(phase="done", goalWorkspace=str(source.run_dir))
        source._save_job_settings(source.state)
        manager = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(manager.server_close)
        with foreign_owner(source.run_dir):
            with self.assertRaises(ownership.OwnedElsewhere):
                manager._check_author_workspace_idle(source.run_dir)
        manager._check_author_workspace_idle(source.run_dir)


class RestartTests(IsolatedCase):
    """I-065 and I-066: work a restart interrupted continues by itself about a minute after the start."""

    def test_an_interrupted_author_resumes_its_conversation_a_minute_after_the_start(self):
        app = self.author_job()
        self.records(app, {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        before = time.time()
        restored = self.restored(app)
        checkpoint = self.checkpoint(restored)
        self.assertEqual((restored.state["phase"], checkpoint["pauseCause"]), ("paused", "interrupted"))
        self.assertIn("continues automatically", restored.state["error"])
        self.assertGreaterEqual(restored.state["autoResumeAt"], before + WAIT)
        self.assertLessEqual(restored.state["autoResumeAt"], time.time() + WAIT)
        retry = Mock()
        self.assertFalse(restored.auto_resume_if_due(retry, now=before + WAIT - 5))
        self.assertTrue(restored.auto_resume_if_due(retry, now=restored.state["autoResumeAt"]))
        retry.assert_called_once_with()
        # The automatic resume reopens the same conversation in the same folder.
        restored = self.restored(app)
        with patch.object(server.subprocess, "Popen", return_value=launch_mock()) as popen, \
                patch.object(server, "require_model_access"), patch.object(restored, "_spawn_worker"):
            restored.auto_resume_if_due(restored.resume, now=restored.state["autoResumeAt"])
        argv = popen.call_args.args[0]
        self.assertIn('goal_thread_id="saved-thread"', argv)
        self.assertEqual(argv[argv.index("--start-node") + 1], "author")
        self.assertEqual(popen.call_args.kwargs["cwd"], app.run_dir)

    def test_keep_paused_and_stop_still_win_over_the_restart(self):
        app = self.author_job()
        self.records(app, {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        restored = self.restored(app)
        restored.pause()  # Keep paused.
        self.assertEqual(self.checkpoint(restored)["pauseCause"], "user")
        again = self.restored(app)
        self.assertEqual(again.state["autoResumeAt"], 0)
        other = self.author_job("Stopped job")
        self.records(other, {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        with patch.object(server, "stop_process_tree"):
            other.stop()
        stopped = self.restored(other)
        self.assertEqual((stopped.state["phase"], stopped.state["autoResumeAt"]), ("done", 0))

    def test_older_interrupted_checkpoints_also_continue(self):
        app = self.author_job()
        self.records(app, {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        app._save_pause({"status": "paused", "node": "author", "stage": "solve", "state": {"statement": "S"},
                         "elapsedSeconds": 30, "pauseCause": "interrupted", "autoResumeAt": 0,
                         "error": "The previous author process ended without a final answer."})
        restored = self.restored(app)
        self.assertGreaterEqual(restored.state["autoResumeAt"], time.time() + WAIT - 5)

    def critic_job(self, records):
        app = self.author_job()
        self.records(app, *records)
        return app

    def test_an_interrupted_critic_round_continues_from_its_exact_candidate(self):
        submitted = "PROOF_FILE: proof.md"
        captured = "The complete proof."
        digest = hashlib.sha256(captured.encode("utf-8")).hexdigest()
        handoff = {"kind": "proof_handoff", "stage": "critic",
                   "manifest": {"files": {"proof.md": {"sha256": digest}}}}
        step = {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"}
        author = {"kind": "author_result", "stage": "solve", "node": "author", "text": submitted}
        for label, records, saved, expected in (
                ("captured candidate", [author, step, handoff], captured, captured),
                ("before the capture", [author, step], "An older candidate.", submitted),
                ("a later round", [author, step, handoff,
                                   {"kind": "critic_result", "stage": "critic", "round": 1,
                                    "report": {"verdict": "pass", "solution": " Repaired proof. ", "bugs": ""}},
                                   step], "Repaired proof.", "Repaired proof."),
                ("a later round whose step names its visit", [author, step, handoff,
                                   {"kind": "critic_result", "stage": "critic", "round": 1,
                                    "report": {"verdict": "pass", "solution": " Repaired proof. ", "bugs": ""}},
                                   {**step, "visit": 2}], "Repaired proof.", "Repaired proof.")):
            with self.subTest(label):
                app = self.critic_job(records)
                app._save(runtime.SAVED_CANDIDATE_FILENAME, saved)
                restored = self.restored(app)
                checkpoint = self.checkpoint(restored)
                self.assertEqual((restored.state["phase"], checkpoint["node"], checkpoint["stage"]),
                                 ("paused", "critic", "critic"))
                # A round whose visit is known continues as that round (its saved reports are reused).
                visit = {runtime.STEP_VISIT_KEY: {"node": "critic", "visit": 2}} if "names its visit" in label else {}
                self.assertEqual(checkpoint["state"], {"statement": "Exact statement", "solution": expected, **visit})
                self.assertGreater(restored.state["autoResumeAt"], time.time())
                with patch.object(server.subprocess, "Popen", return_value=launch_mock()) as popen, \
                        patch.object(server, "require_model_access"), patch.object(restored, "_spawn_worker"):
                    restored.auto_resume_if_due(restored.resume, now=restored.state["autoResumeAt"])
                argv = popen.call_args.args[0]
                self.assertEqual([Path(item).name for item in argv[3:5]], ["author_critic.yaml", "clean_up.yaml"])
                self.assertEqual(argv[argv.index("--start-node") + 1], "critic")
                state = json.loads(Path(argv[argv.index("--state-file") + 1]).read_text())
                self.assertEqual(state["solution"], expected)
                self.assertEqual(state.get(runtime.STEP_VISIT_KEY), visit.get(runtime.STEP_VISIT_KEY))

    def run_critic_step(self, app, state, options, killed=False):
        """Run the runner's critic step in-process in the job's folder (stand-in model calls, no author).

        The critic adds the missing case to its writable proof once; with ``killed`` the runner is
        killed during the read-only final check. Returns the runner's output lines and its model calls.
        """

        calls, output = [], io.StringIO()

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            label = str(settings.get("activity_label") or "")
            if label == critic_doubles.CONVENTION_LABEL:
                calls.append(("convention",))
                return json.dumps(critic_doubles.FAITHFUL)
            edit = settings.get("edit_file")
            calls.append(("coordinator", edit is not None, "FINAL VERIFICATION" in text))
            if edit is None and killed:
                raise KeyboardInterrupt  # The runner is killed during round 2.
            fixes = ""
            if edit is not None:
                path = Path(conversation.workspace) / "proof.md"
                if "Case n = 0" not in path.read_text():
                    path.write_text(self.FIXED)
                    fixes = "Added the case n = 0."
            return json.dumps({"verdict": "pass", "bugs": "", "fixes": fixes})

        previous = Path.cwd()
        os.chdir(app.run_dir)
        try:
            with patch.object(runtime, "run_structured_attempt", side_effect=attempt), \
                    patch.object(runtime, "goal_session", side_effect=AssertionError("no author")), \
                    patch.object(claude_cli, "goal_session", side_effect=AssertionError("no author")), \
                    patch.object(tempfile, "tempdir", str(self.home)), contextlib.redirect_stdout(output):
                try:
                    runtime.execute_workflows([str(ROOT / "workflows/author_critic.yaml")], state, options)
                except KeyboardInterrupt:
                    pass  # The runner was killed.
        finally:
            os.chdir(previous)
        return [line + "\n" for line in output.getvalue().splitlines() if line.startswith("{")], calls

    FIXED = "Lemma 1 holds by induction on n.\nCase n = 0: the claim holds trivially."

    def test_a_later_critic_round_keeps_its_number_when_the_server_dies(self):
        # Round 1 repairs the proof; the runner dies during the read-only final check (round 2, at
        # critic_rounds=1). The records pass through the real reader.
        for case in ("the server dies", "the reader fails, then the runner dies"):
            with self.subTest(case):
                app = self.author_job(criticRounds=1)
                lines, calls = self.run_critic_step(
                    app, {"statement": "Exact statement", "solution": "Lemma 1 holds by induction on n."},
                    {"start_node": "critic", "critic_rounds": 1}, killed=True)
                self.assertEqual(calls, [("coordinator", True, False), ("coordinator", False, True)])

                class ServerDied(BaseException):
                    pass

                def stdout():
                    if case != "the server dies":
                        # A record the reader cannot handle: the job settles from its saved files.
                        yield json.dumps({"kind": "workflow_paused", "stage": "solve", "node": "author"}) + "\n"
                    yield from lines
                    if case == "the server dies":
                        raise ServerDied

                process = Mock(stdout=stdout())
                process.wait.return_value = process.poll.return_value = -9
                token = app.active_token = app.worker_token = object()
                app.process = process
                try:
                    app._read_output(process, token)
                except ServerDied:
                    pass
                saved = app.trace_file.read_text()
                self.assertNotIn('"node_input"', saved)  # The step's input stays out of the transcript.
                restored = self.restored(app) if case == "the server dies" else app
                checkpoint = self.checkpoint(restored)
                round_2 = {"node": "critic", "visit": 2, "verify": "critic_verify"}
                self.assertEqual((restored.state["phase"], checkpoint["node"]), ("paused", "critic"))
                self.assertEqual(checkpoint["state"][runtime.STEP_VISIT_KEY], round_2)
                with patch.object(server.subprocess, "Popen", return_value=launch_mock()) as popen, \
                        patch.object(server, "require_model_access"), patch.object(restored, "_spawn_worker"), \
                        patch.object(restored, "_check_launch_models_locked"):
                    restored.resume()
                argv = popen.call_args.args[0]
                state = json.loads(Path(argv[argv.index("--state-file") + 1]).read_text())
                self.assertEqual(state[runtime.STEP_VISIT_KEY], round_2)
                self.assertFalse(any(item.startswith("critic_audit_file=") for item in argv))
                options = {"start_node": argv[argv.index("--start-node") + 1],
                           "critic_rounds": int(argv[argv.index("--critic-rounds") + 1])}
                # The resumed runner runs the final check again (its critics start afresh), which cannot edit,
                # then the convention critic checks the accepted proof.
                lines, calls = self.run_critic_step(restored, state, options)
                self.assertEqual(calls, [("coordinator", False, True), ("convention",)])
                records = [json.loads(line) for line in lines]
                self.assertEqual([(record["label"], record["outcome"]) for record in records
                                  if record["kind"] == "critic_result"], [("Critic round 2", "pass")])
                self.assertEqual([(record["label"], record["verdict"]) for record in records
                                  if record["kind"] == "convention_result"], [("Convention critic · round 2", "faithful")])

    def test_a_critic_round_resumed_before_its_step_began_keeps_its_number_after_a_restart(self):
        # The run paused before its final check began; Resume started it again; the server then died
        # before the resumed runner reported the step.
        round_2 = {"node": "critic", "visit": 2, "verify": "critic_verify"}
        app = self.author_job(criticRounds=1)
        self.records(app, {"kind": "author_result", "stage": "solve", "node": "author", "text": "Proof."},
                     {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic",
                      "visit": 1},
                     {"kind": "critic_result", "stage": "critic", "round": 1, "outcome": "fixed",
                      "report": {"verdict": "pass", "solution": "Repaired proof.", "bugs": ""}})
        app._save(runtime.SAVED_CANDIDATE_FILENAME, "Repaired proof.")
        app._save_pause({"status": "paused", "node": "critic", "stage": "critic", "pauseCause": "user",
                         "autoResumeAt": 0, "elapsedSeconds": 12,
                         "state": {"statement": "Exact statement", "solution": "Repaired proof.",
                                   runtime.STEP_VISIT_KEY: round_2}})
        app.state.update(phase="paused", stage="critic", activeNode="critic")
        with patch.object(server.subprocess, "Popen", return_value=launch_mock()), \
                patch.object(server, "require_model_access"), patch.object(app, "_spawn_worker"), \
                patch.object(app, "_check_launch_models_locked"):
            app.resume()
        self.assertEqual(self.checkpoint(app)["status"], "running")
        restored = self.restored(app)
        checkpoint = self.checkpoint(restored)
        self.assertEqual((restored.state["phase"], checkpoint["node"], checkpoint["pauseCause"]),
                         ("paused", "critic", "interrupted"))
        self.assertEqual(checkpoint["state"], {"statement": "Exact statement", "solution": "Repaired proof.",
                                               runtime.STEP_VISIT_KEY: round_2})

    def test_an_author_that_starts_again_continues_after_a_restart_before_its_first_request(self):
        # Its first turn may not start for a while (each attempt is retried every minute), and a restart
        # in that time must not end the job.
        reason = "Cannot hand the submitted proof to the critic: (proof.md: file not found)."
        submission = [{"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input",
                       "text": "Prove it."},
                      {"kind": "author_result", "stage": "solve", "node": "author", "text": "PROOF_FILE: proof.md"},
                      {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic",
                       "visit": 1}]
        step = {"kind": "status", "stage": "solve", "node": "author", "label": "Workflow step: author", "visit": 1}
        for case, started in (("right after its Resume", []), ("once its author's step began", [step])):
            with self.subTest(f"a refused submission, {case}"):
                app = self.author_job()
                process = app.process = Mock(stdout=io.StringIO("".join(json.dumps(record) + "\n" for record in (
                    *submission, {"kind": "diagnostic", "stage": "solve", "node": "author", "text": reason},
                    {"kind": "workflow_paused", "stage": "solve", "node": "author", "reason": reason,
                     "state": {"statement": "Exact statement", "handoff_refused": reason, "paused": True}}))),
                    pid=None)
                process.wait.return_value = process.poll.return_value = 0
                token = app.active_token = app.worker_token = object()
                app._read_output(process, token)
                self.assertEqual((app.state["phase"], self.checkpoint(app)["state"]["handoff_refused"]),
                                 ("paused", reason))
                with patch.object(server.subprocess, "Popen", return_value=launch_mock()), \
                        patch.object(server, "require_model_access"), patch.object(app, "_spawn_worker"), \
                        patch.object(app, "_check_launch_models_locked"):
                    app.resume()
                # The resumed runner reported these before the server died.
                self.records(app, *started)
                restored = self.restored(app)
                checkpoint = self.checkpoint(restored)
                self.assertEqual((restored.state["phase"], checkpoint["node"], checkpoint["stage"],
                                  checkpoint["pauseCause"]), ("paused", "author", "solve", "interrupted"))
                self.assertGreater(restored.state["autoResumeAt"], time.time())
                # The author is still told why its submission was refused.
                self.assertEqual(checkpoint["state"].get("handoff_refused"), reason)
        with self.subTest("a rejected proof, once its author's step began"):
            rejection = {"kind": "critic_result", "stage": "critic", "round": 1, "outcome": "reject",
                         "report": {"verdict": "reject", "solution": "Proof.", "bugs": "Gap in step 2."}}
            restored = self.restored(self.critic_job([*submission, rejection, step]))
            checkpoint = self.checkpoint(restored)
            self.assertEqual((restored.state["phase"], checkpoint["node"], checkpoint["stage"]),
                             ("paused", "author", "repair"))
            self.assertEqual(checkpoint["state"]["report"]["bugs"], "Gap in step 2.")

    def test_a_critic_round_that_had_just_finished_continues_with_its_next_step(self):
        step = {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"}
        author = {"kind": "author_result", "stage": "solve", "node": "author", "text": "Proof."}
        reject = {"kind": "critic_result", "stage": "critic", "round": 1,
                  "report": {"verdict": "reject", "solution": "Proof.", "bugs": "Gap in step 2."}}
        checkpoint = self.checkpoint(self.restored(self.critic_job([author, step, reject])))
        self.assertEqual((checkpoint["node"], checkpoint["stage"]), ("author", "repair"))
        self.assertEqual((checkpoint["state"]["report"]["bugs"], checkpoint["state"]["round"]), ("Gap in step 2.", 1))
        passed = {"kind": "critic_result", "stage": "critic", "round": 1,
                  "report": {"verdict": "pass", "solution": "Proof.", "bugs": ""}}
        approved = {"kind": "status", "stage": "critic", "label": "Critic approved"}
        # The critic approved the proof: the convention critic checks it, then writing follows.
        app = self.critic_job([author, step, passed, approved])
        checkpoint = self.checkpoint(self.restored(app))
        self.assertEqual((checkpoint["node"], checkpoint["stage"], checkpoint["state"]),
                         ("convention", "critic", {"statement": "Exact statement", "solution": "Proof.",
                                                   "output": "Proof.", "round": 1}))
        convention = {"kind": "status", "stage": "critic", "node": "convention", "label": "Workflow step: convention"}
        checkpoint = self.checkpoint(self.restored(self.critic_job([author, step, passed, approved, convention])))
        self.assertEqual(checkpoint["node"], "convention")
        verdict = {"kind": "convention_result", "stage": "critic", "round": 1, "verdict": "faithful",
                   "intended": "I.", "assessment": "A.", "loophole": "", "suggestedStatement": ""}
        app = self.critic_job([author, step, passed, approved, convention, verdict])
        runtime.save_final_input("Exact statement", "Proof.", directory=app.run_dir)
        checkpoint = self.checkpoint(self.restored(app))
        self.assertEqual((checkpoint["node"], checkpoint["state"]),
                         ("latex_editor", {"statement": "Exact statement", "solution": "Proof."}))

    def test_an_interrupted_latex_step_continues_in_its_own_graph(self):
        approved = {"kind": "critic_result", "stage": "critic", "round": 1,
                    "report": {"verdict": "pass", "solution": "Approved proof.", "bugs": ""}}
        for node, source in (("latex_compile", r"\documentclass{article}"), ("latex_editor", None)):
            with self.subTest(node):
                app = self.critic_job([approved, {"kind": "status", "stage": "critic", "label": "Critic approved"},
                                       {"kind": "status", "stage": "final", "node": node,
                                        "label": f"Workflow step: {node}"}])
                runtime.save_final_input("Exact statement", "Approved proof.", directory=app.run_dir)
                if source:
                    app._save("latex-source.tex", source)
                checkpoint = self.checkpoint(self.restored(app))
                expected = {"statement": "Exact statement", "solution": "Approved proof."}
                if source:
                    # Repairs keep the content of the saved source (latex package I-051).
                    expected.update(output=source, edited_latex=source)
                self.assertEqual((checkpoint["node"], checkpoint["state"]), (node, expected))
                restored = self.restored(app)
                with patch.object(server.subprocess, "Popen", return_value=launch_mock()) as popen, \
                        patch.object(server, "require_model_access"), patch.object(restored, "_spawn_worker"):
                    restored.auto_resume_if_due(restored.resume, now=restored.state["autoResumeAt"])
                argv = popen.call_args.args[0]
                self.assertEqual(Path(argv[3]).name, "clean_up.yaml")
                self.assertNotIn("author_critic.yaml", " ".join(argv))
                self.assertEqual(argv[argv.index("--start-node") + 1], node)

    def test_a_stage_that_ended_with_an_error_is_not_restarted_but_offers_its_retry(self):
        app = self.critic_job([{"kind": "author_result", "stage": "solve", "text": "Proof."},
                               {"kind": "status", "stage": "critic", "node": "critic",
                                "label": "Workflow step: critic"},
                               {"kind": "diagnostic", "stage": "critic", "text": "error: Configuration broken."}])
        app._save(runtime.SAVED_CANDIDATE_FILENAME, "Proof.")
        restored = self.restored(app)
        self.assertEqual((restored.state["phase"], restored.state["autoResumeAt"]), ("done", 0))
        plan = server.Server._stopped_continuation_plan(restored)
        self.assertEqual(plan["label"], "Continue critic")


class StageRetryTests(IsolatedCase):
    """I-067: an errored or interrupted stage offers its own retry, and LaTeX recompiles its saved source."""

    def done_job(self, **state):
        app = self.author_job()
        app.state.update(phase="done", **state)
        return app

    def test_errored_jobs_get_the_stage_retry_and_finished_ones_do_not(self):
        critic = self.done_job(stage="critic", activeNode="critic", error="The critic failed.")
        critic._save(runtime.SAVED_CANDIDATE_FILENAME, "Candidate.")
        self.assertEqual(server.Server._stopped_continuation_plan(critic)["label"], "Continue critic")
        final = self.done_job(stage="final", activeNode="latex_editor", error="LaTeX failed.", finalInputReady=True)
        self.assertEqual(server.Server._stopped_continuation_plan(final)["label"], "Retry LaTeX editor")
        final._save("latex-source.tex", r"\documentclass{article}")
        self.assertEqual(server.Server._stopped_continuation_plan(final)["label"], "Retry LaTeX compilation")
        review = self.done_job(stage="review", reviewStatement="Draft", error="Review failed: offline")
        self.assertEqual(server.Server._stopped_continuation_plan(review)["label"], "Retry statement review")
        finished = self.done_job(stage="final", resultKind="final", finalInputReady=True)
        self.assertIsNone(server.Server._stopped_continuation_plan(finished))

    def test_the_latex_retry_compiles_the_saved_source_without_the_editor(self):
        source = self.done_job(stage="final", activeNode="latex_compile", error="LaTeX compilation is unavailable.")
        runtime.save_final_input("Exact statement", "Approved proof.", directory=source.run_dir)
        source._save("latex-source.tex", r"\documentclass{article}\begin{document}x\end{document}")
        source.state["finalInputReady"] = True
        manager = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(manager.server_close)
        manager.jobs = {source.run_dir.name: source}
        with patch.object(server.subprocess, "Popen", return_value=launch_mock()) as popen, \
                patch.object(server, "require_model_access"), patch.object(server.App, "_spawn_worker"):
            continued = manager.continue_stopped_job(source.run_dir.name)
        argv = popen.call_args.args[0]
        self.assertEqual(Path(argv[3]).name, "clean_up.yaml")
        self.assertEqual(argv[argv.index("--start-node") + 1], "latex_compile")
        state = json.loads(Path(argv[argv.index("--state-file") + 1]).read_text())
        self.assertEqual(state["output"], r"\documentclass{article}\begin{document}x\end{document}")
        # Without an editor call, repairs keep the content of the saved source (latex package I-051).
        self.assertEqual(state["edited_latex"], state["output"])
        self.assertEqual((continued.run_dir / "latex-source.tex").read_text(), state["output"])
        self.assertNotEqual(continued.run_dir, source.run_dir)


class StageSelectionTests(IsolatedCase):
    """Restarts and stage retries follow the selected workflow stages and never repeat a finished outcome."""

    step = {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"}
    reject = {"kind": "critic_result", "stage": "critic", "round": 1,
              "report": {"verdict": "reject", "solution": "Proof.", "bugs": "Gap in step 2."}}

    def job(self, selected, *records):
        app = self.author_job(workflowStages={name: name in selected for name in server.WORKFLOW_STAGES})
        self.records(app, *records)
        return app

    def assertNotContinued(self, restored):
        self.assertEqual((restored.state["phase"], restored.state["autoResumeAt"]), ("done", 0))
        self.assertFalse((restored.run_dir / server.PAUSE_FILENAME).exists())

    def test_a_finished_critic_round_continues_only_into_a_selected_stage(self):
        passed = {"kind": "critic_result", "stage": "critic", "round": 1,
                  "report": {"verdict": "pass", "solution": "Proof.", "bugs": ""}}
        approved = {"kind": "status", "stage": "critic", "label": "Critic approved"}
        verdict = {"kind": "convention_result", "stage": "critic", "round": 1, "verdict": "faithful",
                   "intended": "I.", "assessment": "A.", "loophole": "", "suggestedStatement": ""}
        # The convention critic belongs to the critic stage: it still checks the approved proof.
        restored = self.restored(self.job({"author", "critic"}, self.step, passed, approved))
        self.assertEqual((restored.state["phase"], self.checkpoint(restored)["node"]), ("paused", "convention"))
        self.assertNotContinued(self.restored(self.job({"author", "critic"}, self.step, passed, approved, verdict)))
        self.assertNotContinued(self.restored(self.job({"critic", "writing"}, self.step, self.reject)))

    def test_a_proof_that_needs_revision_is_neither_restarted_nor_offered_a_stage_retry(self):
        app = self.job({"critic"}, self.step, self.reject,
                       {"kind": "failure_result", "stage": "critic", "node": "critic", "resultKind": "needs-revision",
                        "output": "Proof.", "bugs": "Gap in step 2."})
        app._save(runtime.SAVED_CANDIDATE_FILENAME, "Proof.")
        restored = self.restored(app)
        self.assertNotContinued(restored)
        self.assertEqual(restored.state["resultKind"], "needs-revision")
        self.assertIsNone(server.Server._stopped_continuation_plan(restored))
        # An older run's failure report after the last rejection is a finished outcome too.
        restored = self.restored(self.job(set(server.WORKFLOW_STAGES), self.step, self.reject,
                                          {"kind": "failure_result", "stage": "failure", "node": "failure_summary",
                                           "output": "Why the proof is incomplete."}))
        self.assertNotContinued(restored)
        self.assertEqual(restored.state["resultKind"], "failure")

    def test_a_failed_writing_stage_offers_the_retry_that_matches_its_saved_files(self):
        app = self.job({"writing"}, {"kind": "final_input", "stage": "final", "node": "latex_editor",
                                     "label": "Exact writing input saved"},
                       {"kind": "diagnostic", "stage": "final", "text": "error: The LaTeX editor failed."})
        runtime.save_final_input("Exact statement", "Existing work", directory=app.run_dir)
        restored = self.restored(app)
        self.assertNotContinued(restored)
        self.assertEqual(server.Server._stopped_continuation_plan(restored)["label"], "Retry LaTeX editor")
        app._save("latex-source.tex", r"\documentclass{article}")
        self.assertEqual(server.Server._stopped_continuation_plan(self.restored(app))["label"],
                         "Retry LaTeX compilation")


class DamagedRunTests(IsolatedCase):
    """I-083 and I-108: a damaged run stays listed with a warning, and its checkpoint is rebuilt."""

    def paused_job(self, cause="error"):
        app = self.author_job()
        self.records(app, {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        app._save_pause({"status": "paused", "pauseCause": cause, "autoResumeAt": 5_000, "error": "Connection lost",
                         "elapsedSeconds": 40})  # Neither stage, node nor state.
        return app

    def test_a_checkpoint_without_stage_or_node_is_rebuilt_keeping_its_cause(self):
        for cause, retry in (("error", True), ("user", False)):
            with self.subTest(cause):
                jobs = server.restore_saved_jobs(self.runs)
                app = self.paused_job(cause)
                jobs = server.restore_saved_jobs(self.runs)
                restored = jobs[app.run_dir.name]
                self.assertEqual(restored.state["phase"], "paused")
                self.assertIn("rebuilt", restored.state["settingsWarning"])
                checkpoint = self.checkpoint(restored)
                self.assertTrue(server.complete_pause_checkpoint(checkpoint))
                self.assertEqual((checkpoint["pauseCause"], checkpoint["error"], checkpoint["elapsedSeconds"]),
                                 (cause, "Connection lost", 40))
                self.assertEqual(bool(restored.state["autoResumeAt"]), retry)

    def test_invalid_numeric_settings_are_replaced_with_a_warning(self):
        app = self.author_job()
        # The controller's own copy is the one a restore trusts.
        path = server.controlled_path(app.run_dir, server.JOB_SETTINGS_FILENAME)
        settings = json.loads(path.read_text())
        settings.update(thinkingHours="24h", elapsedSeconds="lots", criticRounds=0, criticCount=11)
        path.write_text(json.dumps(settings))
        self.records(app, {"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        restored = server.restore_saved_jobs(self.runs)[app.run_dir.name]
        self.assertEqual(restored.state["thinkingHours"], server.DEFAULT_THINKING_HOURS)
        self.assertEqual(restored.state["criticRounds"], server.DEFAULT_CRITIC_ROUNDS)
        self.assertEqual(restored.state["criticCount"], server.DEFAULT_CRITIC_COUNT)
        for key in ("thinkingHours", "elapsedSeconds", "criticRounds", "criticCount"):
            self.assertIn(key, restored.state["settingsWarning"])
        self.assertEqual(restored.state["phase"], "paused")
        self.assertTrue(restored.state["autoResumeAt"])

    def test_a_failed_restore_write_keeps_the_job_and_saves_before_the_resume(self):
        app = self.paused_job()
        real = server.App._save_pause

        def full_disk(self, checkpoint):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch.object(server.App, "_save_pause", full_disk):
            restored = server.restore_saved_jobs(self.runs)[app.run_dir.name]
        self.assertIn("could not be saved", restored.state["settingsWarning"])
        self.assertIsNotNone(restored._unsaved_checkpoint)
        with patch.object(server.App, "_save_pause", real), \
                patch.object(server.subprocess, "Popen", return_value=launch_mock()), \
                patch.object(server, "require_model_access"), patch.object(restored, "_spawn_worker"):
            restored.resume()
        self.assertIsNone(restored._unsaved_checkpoint)
        self.assertEqual(self.checkpoint(restored)["status"], "running")

    def test_a_run_that_cannot_be_restored_is_listed_with_the_reason(self):
        healthy = self.author_job("Healthy")
        broken = self.author_job("Broken")
        for app in (healthy, broken):
            self.records(app, {"kind": "final_result", "stage": "final", "output": "Done."})
        real = server.restore_saved_app

        def restore(app, **options):
            if app.run_dir == broken.run_dir:
                raise MemoryError()
            return real(app, **options)

        with patch.object(server, "restore_saved_app", side_effect=restore), \
                patch("sys.stderr", new_callable=io.StringIO):
            jobs = server.restore_saved_jobs(self.runs)
        self.assertEqual(set(jobs), {healthy.run_dir.name, broken.run_dir.name})
        card = jobs[broken.run_dir.name]
        self.assertEqual(card.state["phase"], "done")
        self.assertIn("could not be restored (MemoryError)", card.state["error"])
        self.assertIn("Broken", card.state["draft"])

    def test_an_unreadable_settings_file_holds_a_web_cutoff_run(self):
        for marker, held in ((True, True), (False, False)):
            with self.subTest(input_md=marker):
                app = self.author_job()
                if marker:
                    app._save("input.md", "Exact statement\n")
                # A power loss emptied the copy a restore trusts (the controller's).
                server.controlled_path(app.run_dir, server.JOB_SETTINGS_FILENAME).write_text("")
                restored = self.restored(app)
                self.assertIn("cannot be read", restored.state["settingsWarning"])
                self.assertEqual(bool(restored.state["launchHold"]), held)


class ReaderTests(IsolatedCase):
    """I-103 and I-104: an odd runner event or a failed write never kills the runner or freezes the job."""

    def read(self, app, lines, code=0):
        process = app.process = Mock(stdout=io.StringIO("".join(lines)), pid=None)
        process.wait.return_value = process.poll.return_value = code
        token = app.active_token = app.worker_token = object()
        with patch.object(server.time, "time", return_value=1_000):
            app._read_output(process, token)
        return process

    def test_malformed_records_are_skipped_and_the_job_still_settles(self):
        bad_lines = {
            "huge number": "1" * 5000 + "\n",
            "deep nesting": "[" * 100_000 + "]" * 100_000 + "\n",
            "null params": json.dumps({"kind": "codex_event", "stage": "solve", "event": {"params": None}}) + "\n",
            "pause without state": json.dumps({"kind": "workflow_paused", "stage": "solve", "node": "author"}) + "\n",
            "list stage": json.dumps({"kind": "status", "stage": ["solve"], "node": {}}) + "\n",
            "lone surrogate": '{"kind": "status", "stage": "solve", "text": "\\ud800"}\n',
        }
        for label, line in bad_lines.items():
            with self.subTest(label):
                app = self.author_job()
                self.read(app, [line, json.dumps({"kind": "status", "stage": "solve", "text": "after"}) + "\n"])
                self.assertIsNone(app.worker_token)
                # The author was interrupted: an error pause, retried after the usual wait.
                self.assertEqual(app.state["phase"], "paused", app.state)
                self.assertTrue(app.state["autoResumeAt"])
                self.assertIn("after", app.trace_file.read_text())

    def test_failed_end_of_run_bookkeeping_still_settles_into_an_error_pause(self):
        app = self.author_job()
        (app.run_dir / "checked-statement.md").unlink()  # saved_statement() now raises.
        app.state["draft"] = "Exact statement"
        self.read(app, [json.dumps({"kind": "status", "stage": "solve", "node": "author", "text": "Working"}) + "\n"],
                  code=1)
        self.assertIsNone(app.worker_token)
        self.assertEqual(app.state["phase"], "paused")
        self.assertTrue(app.state["autoResumeAt"])
        self.assertTrue(server.complete_pause_checkpoint(self.checkpoint(app)))

    def test_a_transcript_write_failure_keeps_the_runner_and_its_records(self):
        app = self.author_job()
        real = server.append_run_log
        failing = [True]

        def disk(path, text, durable=False):
            if failing[0]:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, text, durable=durable)

        lines = [json.dumps({"kind": "status", "stage": "solve", "text": "Kept"}) + "\n",
                 json.dumps({"kind": "workflow_paused", "stage": "solve", "node": "author",
                             "state": {"statement": "Exact statement"}, "reason": "Connection lost"}) + "\n"]
        with patch.object(server, "append_run_log", side_effect=disk), \
                patch.object(server, "stop_process_tree") as stopped:
            process = self.read(app, lines, code=0)
        stopped.assert_not_called()
        self.assertEqual(app.state["phase"], "paused")
        self.assertEqual(app.state["error"], "Connection lost")
        self.assertTrue(any("Saving the transcript failed" in str(entry.get("text")) for entry in app.state["trace"]))
        failing[0] = False
        self.assertTrue(app.flush_trace(force=True))
        saved = app.trace_file.read_text()
        self.assertLess(saved.index("Kept"), saved.index("Connection lost"))
        process.wait.assert_called()

    def test_stop_and_pause_settle_a_job_whose_reader_ended_without_settling(self):
        app = self.author_job()
        token = app.worker_token = app.active_token = object()
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        app._worker_thread = (token, dead)
        with patch.object(server, "stop_process_tree"):
            app.stop()
        self.assertEqual((app.state["phase"], app.state["error"]), ("done", "Stopped."))
        self.assertIsNone(app.worker_token)
        paused = self.author_job("Paused job")
        token = paused.worker_token = paused.active_token = object()
        paused._worker_thread = (token, dead)
        paused.process = Mock(pid=None)
        paused.process.wait.return_value = 0
        with patch.object(server.time, "sleep"):
            paused.pause()
            paused._settle_pause_without_reader(token)
        self.assertEqual(paused.state["phase"], "paused")
        self.assertEqual(self.checkpoint(paused)["status"], "paused")


class DurableWriteTests(IsolatedCase):
    """I-108: controller checkpoints and transcript records reach the disk before they are relied on."""

    def test_checkpoints_are_flushed_before_the_replace_and_the_folder_after(self):
        calls = []
        real_sync, real_folder = runtime._sync_file, runtime._sync_folder
        with patch.object(runtime, "_sync_file", side_effect=lambda fd: calls.append("file") or real_sync(fd)), \
                patch.object(runtime, "_sync_folder", side_effect=lambda folder: calls.append(("folder", Path(folder)))
                             or real_folder(folder)):
            runtime._private_atomic_write(self.runs / "pause.json", "{}\n")
        self.assertEqual(calls, ["file", ("folder", self.runs)])
        self.assertEqual((self.runs / "pause.json").read_text(), "{}\n")

    def test_controller_records_are_synced_but_streamed_model_events_are_not(self):
        app = self.author_job()
        with patch.object(server.os, "fsync") as fsync:
            app.add_trace({"kind": "status", "stage": "solve", "text": "Controller record"})
            self.assertEqual(fsync.call_count, 1)
            app.add_trace({"kind": "codex_event", "stage": "solve", "event": {"method": "item/agentMessage/delta"}})
            self.assertEqual(fsync.call_count, 1)


class ShutdownTests(IsolatedCase):
    """I-095: closing the server pauses its authors for a restart and interrupts the other runners."""

    def test_stop_all_pauses_authors_for_a_restart_and_waits_with_a_bound(self):
        author = self.author_job()
        author.process = Mock(pid=None)
        author.process.poll.return_value = None
        critic = self.author_job("Critic job")
        critic.state.update(stage="critic", activeNode="critic")
        critic.process = Mock(pid=None)
        critic.process.poll.return_value = None
        manager = object.__new__(server.Server)
        manager.jobs, manager.jobs_lock = {"a": author, "c": critic}, threading.RLock()
        with patch.object(server, "stop_process_tree") as stopped:
            started = time.monotonic()
            manager.stop_all(wait=0.3)
        self.assertLess(time.monotonic() - started, 5)
        checkpoint = self.checkpoint(author)
        self.assertEqual((checkpoint["status"], checkpoint["pauseCause"]), ("pausing", "restart"))
        self.assertTrue(author._pause_request_path().exists())
        author.process.send_signal.assert_not_called()
        critic.process.send_signal.assert_called_once_with(signal.SIGINT)
        # Neither runner ended in time, so both process trees are stopped.
        self.assertEqual({call.args[0] for call in stopped.call_args_list}, {author.process, critic.process})

    def test_a_restart_pause_resumes_a_minute_after_the_next_start(self):
        app = self.author_job()
        app._save_pause({"status": "pausing", "node": "author", "stage": "solve", "state": {"statement": "S"},
                         "elapsedSeconds": 50, "pauseCause": "restart", "autoResumeAt": 0})
        self.records(app, {"kind": "workflow_paused", "stage": "solve", "node": "author", "state": {"statement": "S"}})
        restored = self.restored(app)
        self.assertEqual(restored.state["phase"], "paused")
        self.assertGreaterEqual(restored.state["autoResumeAt"], time.time() + WAIT - 5)

    def test_the_authors_acknowledgement_keeps_the_restart_cause(self):
        app = self.author_job()
        app.pause(_for_restart=True)
        paused = {"kind": "workflow_paused", "stage": "solve", "node": "author", "state": {"statement": "S"},
                  "reason": "", "threadId": "saved-thread"}
        process = app.process = Mock(stdout=io.StringIO(json.dumps(paused) + "\n"), pid=None)
        process.wait.return_value = process.poll.return_value = 0
        token = app.active_token = app.worker_token = object()
        app._read_output(process, token)
        checkpoint = self.checkpoint(app)
        self.assertEqual((app.state["phase"], checkpoint["status"], checkpoint["pauseCause"]),
                         ("paused", "paused", "restart"))
        self.assertTrue(app.state["autoResumeAt"])
        self.assertGreaterEqual(self.restored(app).state["autoResumeAt"], time.time() + WAIT - 5)

    @unittest.skipUnless(POSIX, "SIGHUP is POSIX")
    def test_terminal_and_server_signal_handlers_are_installed_and_restored(self):
        before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
        previous = cli._handle_signals(signal.SIG_IGN)
        try:
            self.assertIs(signal.getsignal(signal.SIGTERM), cli._interrupt)
            self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
            with self.assertRaises(KeyboardInterrupt):
                cli._interrupt(signal.SIGTERM, None)
        finally:
            cli._restore_signals(previous)
        self.assertEqual({number: signal.getsignal(number) for number in before}, before)

    def test_the_server_pauses_its_jobs_when_it_receives_sigterm(self):
        manager = Mock(origin="http://127.0.0.1:8765", token="private-session-token")

        def serve():
            raise KeyboardInterrupt  # What the SIGTERM handler raises in the main thread.

        manager.serve_forever.side_effect = serve
        with patch.object(cli.sys, "argv", ["web_ui.py", "--no-browser"]), \
                patch.object(cli, "select_current_dashboard", return_value=cli.Selection() if hasattr(
                    cli, "Selection") else __import__("ui.current_run", fromlist=["Selection"]).Selection()), \
                patch.object(cli, "Server", return_value=manager) as constructor, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(constructor.call_args.kwargs["single_instance"])
        manager.stop_all.assert_called_once_with()
        manager.server_close.assert_called_once_with()


class SingleInstanceTests(IsolatedCase):
    """I-117: one server per runs folder, another port when 8765 is taken, and a token that survives restarts."""

    def test_a_second_server_names_the_first_and_the_token_survives_a_restart(self):
        first = server.Server((server.HOST, 0), runs=self.runs, single_instance=True)
        try:
            with self.assertRaises(ownership.AlreadyRunning) as raised:
                server.Server((server.HOST, 0), runs=self.runs, single_instance=True)
            self.assertEqual(raised.exception.url, f"http://{server.HOST}:{first.server_port}/#{first.token}")
            token = first.token
        finally:
            first.server_close()
        second = server.Server((server.HOST, 0), runs=self.runs, single_instance=True)
        self.addCleanup(second.server_close)
        self.assertEqual(second.token, token)
        # A plain server (tests, tools) keeps a token of its own.
        other = server.Server((server.HOST, 0), runs=self.home / "other-runs")
        self.addCleanup(other.server_close)
        self.assertNotEqual(other.token, token)
        # The token stays out of the runs folder and out of model sandboxes.
        self.assertFalse(any(token in path.read_text(errors="ignore") for path in self.runs.rglob("*")
                             if path.is_file()))
        self.assertIn(providers.state_directory().resolve() / providers.SESSIONS, providers.private_paths())

    def test_a_taken_port_moves_the_server_to_a_free_one(self):
        holder = socket.socket()
        holder.bind((server.HOST, 0))
        holder.listen()
        self.addCleanup(holder.close)
        taken = holder.getsockname()[1]
        moved = server.Server((server.HOST, taken), runs=self.runs, single_instance=True)
        self.addCleanup(moved.server_close)
        self.assertNotEqual(moved.server_port, taken)
        self.assertEqual(moved.origin, f"http://{server.HOST}:{moved.server_port}")

    def test_the_launcher_opens_the_running_server_instead_of_starting_another(self):
        running = ownership.AlreadyRunning({"pid": 1, "port": 8765}, "http://127.0.0.1:8765/#token-value")
        output = io.StringIO()
        with patch.object(cli.sys, "argv", ["web_ui.py", "--no-browser"]), \
                patch.object(cli, "select_current_dashboard",
                             return_value=__import__("ui.current_run", fromlist=["Selection"]).Selection()), \
                patch.object(cli, "Server", side_effect=running), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(output.getvalue().strip(), "http://127.0.0.1:8765/#token-value")


class WorkflowFallbackTests(IsolatedCase):
    """I-116: an edit that does not load falls back to the last version that did, with a visible warning."""

    def setUp(self):
        super().setUp()
        self.workflows = self.home / "workflows"
        self.workflows.mkdir()
        for name in ("author_critic.yaml", "clean_up.yaml"):
            (self.workflows / name).write_text((runtime.WORKFLOWS / name).read_text())
        for change in (patch.object(runtime, "WORKFLOWS", self.workflows),
                       patch.object(runtime, "_last_good_copy",
                                    side_effect=lambda path: self.home / "last-good" / Path(path).name)):
            change.start()
            self.addCleanup(change.stop)
        self.addCleanup(runtime._WORKFLOW_WARNINGS.clear)

    def break_file(self, name):
        path = self.workflows / name
        path.write_text(path.read_text().replace("nodes:", "nodes:\n  - broken: [\n", 1))

    def test_a_broken_edit_runs_from_the_last_readable_version_with_a_warning(self):
        good = runtime.builtin_workflow("author_critic")
        self.assertEqual(runtime.workflow_warnings(), "")
        self.break_file("author_critic.yaml")
        self.assertEqual(runtime.builtin_workflow("author_critic"), good)
        warning = runtime.workflow_warnings()
        self.assertIn("author_critic.yaml", warning)
        self.assertIn("line", warning)
        self.assertIn("last version", warning)
        self.assertEqual(runtime.builtin_workflow_path("author_critic"), self.home / "last-good" / "author_critic.yaml")
        app = self.author_job()
        self.assertEqual(app.snapshot()["workflowWarning"], warning)
        # A fixed file is used again, and the warning goes away.
        (self.workflows / "author_critic.yaml").write_text((ROOT / "workflows" / "author_critic.yaml").read_text())
        self.assertEqual(runtime.builtin_workflow_path("author_critic"), self.workflows / "author_critic.yaml")
        self.assertEqual(runtime.workflow_warnings(), "")

    def test_a_launch_uses_the_saved_copy_and_says_so(self):
        runtime.builtin_workflow("author_critic")
        runtime.builtin_workflow("clean_up")
        self.break_file("author_critic.yaml")
        app = self.author_job()
        app.state.update(phase="paused")
        app._save_pause({"status": "paused", "node": "author", "stage": "solve", "state": {"statement": "S"},
                         "elapsedSeconds": 1, "pauseCause": "user", "autoResumeAt": 0})
        with patch.object(server.subprocess, "Popen", return_value=launch_mock()) as popen, \
                patch.object(server, "require_model_access"), patch.object(app, "_spawn_worker"):
            app.resume()
        argv = popen.call_args.args[0]
        self.assertEqual(Path(argv[3]), self.home / "last-good" / "author_critic.yaml")
        self.assertTrue(any("last version" in str(entry.get("text")) for entry in app.state["trace"]))

    def test_without_a_readable_version_the_start_names_the_file_and_line(self):
        self.break_file("clean_up.yaml")
        runtime._LAST_GOOD_WORKFLOWS.pop(str((self.workflows / "clean_up.yaml").resolve()), None)
        with self.assertRaisesRegex(ValueError, r"clean_up\.yaml \(line \d+, column \d+\)"):
            runtime.builtin_workflow("clean_up")
        errors = io.StringIO()
        with patch.object(cli.sys, "argv", ["web_ui.py", "--no-browser"]), \
                patch.object(cli, "select_current_dashboard",
                             return_value=__import__("ui.current_run", fromlist=["Selection"]).Selection()), \
                patch.object(cli, "Server", side_effect=ValueError("Cannot read workflow clean_up.yaml (line 3)")), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(cli.main(), 1)
        self.assertIn("Cannot start TCS Prover: Cannot read workflow clean_up.yaml (line 3)", errors.getvalue())

    def test_the_launcher_reports_an_unreadable_workflow_without_a_traceback(self):
        real_import = __import__

        def imports(name, *args, **kwargs):
            if name == "ui.cli":
                raise ValueError("Cannot read workflow author_critic.yaml (line 93, column 1): mapping values")
            return real_import(name, *args, **kwargs)

        errors = io.StringIO()
        with patch.object(web_ui.sys, "prefix", str(ROOT / ".venv")), \
                patch.object(web_ui, "__file__", str(self.home / "web_ui.py")), \
                patch("builtins.__import__", side_effect=imports), contextlib.redirect_stderr(errors):
            self.assertEqual(web_ui.main(), 1)
        self.assertIn("Cannot start TCS Prover: Cannot read workflow author_critic.yaml (line 93", errors.getvalue())


class TranscriptMemoryTests(IsolatedCase):
    """I-119: a restart streams the transcript, and the kept records are bounded."""

    def test_restore_does_not_build_a_list_of_the_whole_transcript(self):
        app = self.author_job()
        self.records(app, *({"kind": "status", "stage": "solve", "text": f"Event {index}"} for index in range(50)))
        with patch.object(server, "_run_records", side_effect=AssertionError("whole transcript loaded")):
            restored = self.restored(app)
        self.assertEqual(restored.state["phase"], "paused")

    def test_pinned_records_keep_results_and_the_first_and_latest_others(self):
        pinned = [{"kind": "diagnostic", "text": str(index)} for index in range(server.PINNED_LIMIT + 1000)]
        pinned.insert(1500, {"kind": "critic_result", "text": "result"})
        kept = server.bounded_pinned(pinned)
        texts = [record["text"] for record in kept]
        self.assertEqual(len(kept), server.PINNED_LIMIT + 1)
        self.assertIn("result", texts)
        self.assertEqual(texts[:server.PINNED_HEAD], [str(index) for index in range(server.PINNED_HEAD)])
        self.assertEqual(texts[-1], str(server.PINNED_LIMIT + 999))

    def test_streamed_fragments_of_completed_messages_are_not_reloaded(self):
        app = self.author_job()

        def event(method, **params):
            return {"kind": "codex_event", "stage": "solve", "root": True,
                    "event": {"method": method, "params": params}}

        self.records(app, event("item/agentMessage/delta", itemId="m1", delta="Hel"),
                     event("item/agentMessage/delta", itemId="m2", delta="Still streaming"),
                     event("item/completed", item={"type": "agentMessage", "id": "m1", "text": "Hello"}))
        trace = server.App(app.trace_file, self.runs).state["trace"]
        self.assertEqual([record["event"]["method"] for record in trace],
                         ["item/agentMessage/delta", "item/completed"])
        self.assertEqual(trace[0]["event"]["params"]["itemId"], "m2")

    def test_one_run_too_large_to_restore_does_not_hide_the_others(self):
        large, small = self.author_job("Large"), self.author_job("Small")
        real = server.App._load_trace

        def load(self):
            if self.trace_file == large.trace_file:
                raise MemoryError()
            return real(self)

        self.records(large, {"kind": "status", "stage": "solve"})
        self.records(small, {"kind": "status", "stage": "solve"})
        with patch.object(server.App, "_load_trace", load), patch("sys.stderr", new_callable=io.StringIO):
            jobs = server.restore_saved_jobs(self.runs)
        self.assertEqual(set(jobs), {large.run_dir.name, small.run_dir.name})
        self.assertIn("could not be restored", jobs[large.run_dir.name].state["error"])


class TerminalTests(IsolatedCase):
    """I-091 and I-003: Ctrl-C pauses authors in place and every unfinished file says how to continue."""

    def setUp(self):
        super().setUp()
        self.statement = self.home / "problem.md"
        self.statement.write_text("Every finite graph has property P.\n")

    def run_headless(self, start, interrupts=()):
        """Run one file; Ctrl-C arrives during the waits numbered in `interrupts` (1 is the first)."""

        errors = io.StringIO()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) in interrupts:
                raise KeyboardInterrupt
            if len(sleeps) > 200:
                raise AssertionError("The terminal run never finished waiting.")

        with patch.object(cli, "direct_cli_options", return_value={}), \
                patch.object(server.App, "start_direct_statement", autospec=True, side_effect=start), \
                patch.object(cli.time, "sleep", side_effect=sleep):
            code = cli.run_headless_markdown(self.statement, runs=self.runs, output_stream=io.StringIO(),
                                             error_stream=errors)
        return code, errors.getvalue()

    def test_the_first_ctrl_c_pauses_the_author_and_prints_how_to_continue(self):
        def start(app, statement, **options):
            app.run_dir = self.runs / "job"
            app.state.update(phase="running", stage="solve", activeNode="author")

        def pause(app):
            app.state.update(phase="paused", error="", autoResumeAt=0)

        with patch.object(server.App, "pause", autospec=True, side_effect=pause) as paused, \
                patch.object(server.App, "stop", autospec=True) as stopped:
            code, errors = self.run_headless(start, interrupts={1})
        self.assertEqual(code, 130)
        paused.assert_called_once()
        stopped.assert_not_called()
        self.assertIn("[problem.md] Paused.", errors)
        self.assertIn(f"Continue the same conversation with: python3 web_ui.py --resume-research {self.runs / 'job'}",
                      errors)

    def test_a_second_ctrl_c_stops_at_once(self):
        def start(app, statement, **options):
            app.run_dir = self.runs / "job"
            app.state.update(phase="running", stage="solve", activeNode="author")

        def pause(app):
            app.state.update(phase="pausing")  # The author does not answer before the second Ctrl-C.

        def stop(app):
            app.state.update(phase="done", error="Stopped.", manuallyStopped=True)

        with patch.object(server.App, "pause", autospec=True, side_effect=pause), \
                patch.object(server.App, "stop", autospec=True, side_effect=stop) as stopped:
            code, errors = self.run_headless(start, interrupts={1, 2})
        self.assertEqual(code, 130)
        stopped.assert_called()
        self.assertIn("All active proofs stopped.", errors)

    def test_a_stop_that_cannot_be_recorded_still_ends_the_runner(self):
        app = server.App(runs=self.runs, load_latest=False)
        app.process = Mock()
        with patch.object(server.App, "stop", side_effect=OSError(errno.ENOSPC, "No space left on device")), \
                patch.object(cli, "stop_process_tree") as stopped:
            cli._stop_headless_apps([(self.statement, app)])
        stopped.assert_called_once_with(app.process)

    def test_a_pause_that_waits_for_the_user_prints_the_resume_command(self):
        def start(app, statement, **options):
            app.run_dir = self.runs / "job"
            app.state.update(phase="paused", error="Workflow time limit reached.", autoResumeAt=0)

        code, errors = self.run_headless(start)
        self.assertEqual(code, 1)
        self.assertIn("[problem.md] Paused: Workflow time limit reached.", errors)
        self.assertNotIn("Proof failed", errors)
        self.assertIn("--resume-research", errors)


class GatewayTests(unittest.TestCase):
    """I-105: the gateway finds a restarted controller again and pauses its own jobs when it closes."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.record = Path(folder.name) / "launch-status.json"

    def write(self, port, token):
        self.record.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/#{token}"}))

    def test_a_connection_error_or_a_refusal_reads_the_launch_record_again(self):
        self.write(1111, "old-token-value")
        upstream = live_gateway.LaunchRecordUpstream(self.record)
        calls = []

        def request(self, method, path, body=None):
            calls.append((self.port, method, path))
            if self.port == 1111:
                raise live_gateway.GatewayError("The local controller is temporarily unavailable.")
            return 200, "application/json", None, b'{"jobs": []}'

        with patch.object(live_gateway.Upstream, "request", request):
            self.write(2222, "new-token-value")
            self.assertEqual(upstream.jobs(), {"jobs": []})
            self.assertEqual(calls, [(1111, "GET", "/jobs"), (2222, "GET", "/jobs")])
            self.assertEqual((upstream.port, upstream.token), (2222, "new-token-value"))
        # A lost action is never repeated, but the next request goes to the new controller.
        self.write(3333, "third-token-value")
        with patch.object(live_gateway.Upstream, "request",
                          side_effect=live_gateway.GatewayError("not confirmed")) as sent:
            with self.assertRaises(live_gateway.GatewayError):
                upstream.request("POST", "/stop", b"{}")
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(upstream.port, 3333)
        # A refused request (an old token) ran nothing, so it is sent again once.
        replies = [(403, "application/json", None, b"{}"), (200, "application/json", None, b"{}")]
        self.write(4444, "fourth-token-value")
        with patch.object(live_gateway.Upstream, "request", side_effect=replies):
            self.assertEqual(upstream.request("POST", "/stop", b"{}")[0], 200)
        self.assertEqual(upstream.port, 4444)

    def test_closing_the_gateway_pauses_the_new_controllers_jobs(self):
        self.write(1111, "legacy-token-value")
        root = Path(self.record).parent
        current = Mock(server_port=2222, token="current-token-value")
        gateway = Mock(origin="http://127.0.0.1:8772", token="gateway-token-value")
        gateway.serve_forever.side_effect = KeyboardInterrupt
        argv = ["--legacy-launch-status", str(self.record), "--legacy-runs", str(root / "old"),
                "--new-runs", str(root / "new"), "--status-file", str(root / "status" / "gateway.json")]
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, getattr(signal, "SIGHUP", 0))
                    if number}
        self.addCleanup(lambda: [signal.signal(number, handler) for number, handler in previous.items()])
        with patch.object(server, "Server", return_value=current) as constructor, \
                patch.object(live_gateway, "GatewayServer", return_value=gateway), \
                patch.object(live_gateway.threading, "Thread"), \
                patch.object(server, "raise_open_file_limit"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live_gateway.main(argv), 0)
        self.assertTrue(constructor.call_args.kwargs["single_instance"])
        current.stop_all.assert_called_once_with()
        current.server_close.assert_called_once_with()


class SleepTests(IsolatedCase):
    """I-113: machine sleep is left out of the active time, as the runner's own budget clock does."""

    def test_sleep_is_detected_and_left_out_of_a_working_jobs_time(self):
        watch = server.SleepWatch()
        with patch.object(server.time, "time", return_value=watch._wall + 3600 + 5), \
                patch.object(server.time, "monotonic", return_value=watch._awake + 5):
            self.assertAlmostEqual(watch.slept(), 3600, delta=1)
        app = self.author_job(resumedAt="2026-10-01T08:00:00+00:00")
        waiting = app._retry_wait_started = server.datetime(2026, 10, 1, 9, tzinfo=server.timezone.utc)
        app.exclude_sleep(3600)
        self.assertEqual(app.state["resumedAt"], "2026-10-01T09:00:00+00:00")
        self.assertEqual(app._retry_wait_started, waiting + server.timedelta(hours=1))
        self.assertEqual(json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())["resumedAt"],
                         "2026-10-01T09:00:00+00:00")
        app.state["phase"] = "paused"
        app.exclude_sleep(3600)  # A paused job's time is frozen already.
        self.assertEqual(app.state["resumedAt"], "2026-10-01T09:00:00+00:00")


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class LifecycleUiTests(unittest.TestCase):
    """The job card says why a job runs elsewhere, and the job page shows a workflow fallback warning."""

    def run_app(self, slices, setup, checks):
        import test_audit_fixes_frontend as frontend
        payload = json.dumps({"slices": slices, "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", frontend.RUNNER, str(frontend.APP)], input=payload,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_job_owned_elsewhere_shows_why_and_offers_no_actions(self):
        import test_audit_fixes_frontend as frontend
        self.run_app(frontend.BATCHES, frontend.BATCHES_SETUP, r"""
currentBatch = '__root__'; batchCatalog = [{id: '__root__', label: 'Runs', current: true}];
const message = 'This job is running in another terminal run (PID 7). This server takes it over once that process ends.';
renderJobs([{runId: 'r1', title: 'P1', phase: 'running', stageLabel: 'Proof author', runningElsewhere: message}]);
const copy = ui.jobsList.children[0].children[0].children;
assert.ok(copy.some(child => child.textContent === message));
const buttons = ui.jobsList.children[0].children[1].children;
assert.equal(buttons.find(item => item.textContent === 'Delete').hidden, true);
assert.equal(buttons.find(item => item.textContent === 'Continue stopped job').hidden, true);
""")

    def test_the_workflow_fallback_warning_joins_the_settings_warning(self):
        self.run_app([["function jobSettingsWarning()", "function renderRunOverview()"]], r"""
let state = {settingsWarning: 'Saved model replaced.', workflowWarning: 'author_critic.yaml cannot be read.',
             statementReviewOnly: false, problemMode: 'statement', skipStatementReview: true};
const unconfiguredApiModel = () => false, unconfiguredJobModelText = () => '';
""", r"""
assert.equal(jobSettingsWarning(), 'Saved model replaced. author_critic.yaml cannot be read.');
state = {...state, settingsWarning: ''};
assert.equal(jobSettingsWarning(), 'author_critic.yaml cannot be read.');
""")


@unittest.skipUnless(POSIX, "process groups are POSIX")
class ProcessGroupTests(unittest.TestCase):
    """I-110: what a runner leaves in its process group is killed when the runner is reaped."""

    def test_a_leftover_descendant_is_killed_when_the_runner_is_reaped(self):
        script = ("import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(60)']); print(child.pid, flush=True)")
        runner = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                                  start_new_session=True)
        leftover = int(runner.stdout.readline())
        runner.stdout.close()
        self.assertEqual(server.reap_runner(runner), 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and ownership.pid_alive(leftover):
            try:
                os.waitpid(leftover, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.05)
        self.assertFalse(ownership.pid_alive(leftover))


if __name__ == "__main__":
    unittest.main()
