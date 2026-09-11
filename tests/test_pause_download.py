"""Pause/resume and authenticated TeX downloads without paid model calls."""
import io
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ui import server


class PauseDownloadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.app = server.App(runs=self.runs)
        self.app._new_run("Exact statement")
        self.run = self.app.run_dir
        self.app._save("checked-statement.md", "# Checked statement\n\nExact statement\n")
        for role in ("author", "critic", "final"):
            self.app._save(f"prompts/{role}.txt", self.app.state[f"{role}Prompt"])
        self.notes = {name: f"Original {name}\n" for name in server.RESEARCH_MEMORY_FILES}
        for name, value in self.notes.items():
            self.app._save(name, value)
        self.app.state.update(phase="running", stage="repair", activeNode="author", round=2,
                              goalThreadId="saved-root", goalWorkspace=str(self.run.resolve()),
                              runId=self.run.name, startedAt=datetime.now(timezone.utc).isoformat())
        self.report = {"verdict": "reject", "solution": "Safe candidate", "bugs": "Missing boundary case"}
        self.app.add_trace({"kind": "critic_result", "stage": "critic", "round": 2, "report": self.report})
        self.app._save_job_settings(self.app.state)
        self.process = Mock(stdout=io.StringIO(""))
        self.process.wait.return_value = 0
        self.process.poll.return_value = 0
        self.token = object()
        self.app.process = self.process
        self.app.worker_token = self.app.active_token = self.token

    def paused(self):
        self.app.pause()
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        state = {**checkpoint["state"], "paused": True}
        self.process.stdout = io.StringIO(json.dumps({"kind": "workflow_paused", "stage": "solve",
                                                     "node": "author", "state": state}) + "\n")
        self.app._read_output(self.process, self.token)

    def test_pause_restores_after_restart_and_resumes_same_directory_and_feedback(self):
        self.app.pause()
        self.assertEqual(self.app.state["phase"], "pausing")
        with self.assertRaises(ValueError):
            self.app.resume()
        self.paused()
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertFalse(self.app.has_active_worker())
        before = self.app.trace_file.read_bytes()
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["phase"], "paused")
        self.assertEqual(restored.state["goalThreadId"], "saved-root")
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(checkpoint["goalThreadId"], "saved-root")
        restored.state["goalThreadId"] = "stale-settings-thread"
        pending_steer = '{"instruction": "Keep the boundary case explicit"}\n'
        self.app._save(server.AUTHOR_STEER_FILENAME, pending_steer)
        def launch(command, **kwargs):
            self.assertEqual(kwargs["cwd"], self.run)
            self.assertFalse((self.run / server.PAUSE_REQUEST_FILENAME).exists())
            self.assertEqual(command[command.index("--elapsed-seconds") + 1], str(checkpoint["elapsedSeconds"]))
            self.assertEqual(command[command.index("--start-node") + 1], "author")
            settings = dict(command[i + 1].split("=", 1) for i, arg in enumerate(command) if arg == "--set")
            self.assertEqual(json.loads(settings["goal_thread_id"]), "saved-root")
            self.assertTrue(json.loads(settings["goal_resume"]))
            self.assertTrue(json.loads(settings["goal_require_resume"]))
            self.assertEqual((self.run / server.AUTHOR_STEER_FILENAME).read_text(), pending_steer)
            saved = json.loads(Path(command[command.index("--state-file") + 1]).read_text())
            self.assertEqual(saved["report"], self.report)
            self.assertEqual(saved["round"], 2)
            self.assertNotIn("paused", saved)
            return Mock(stdin=io.StringIO())
        with patch.object(server.subprocess, "Popen", side_effect=launch) as popen, patch.object(restored, "_spawn_worker"):
            restored.resume()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(restored.state["runId"], self.run.name)
        self.assertEqual(restored.state["phase"], "running")
        self.assertEqual(len(list(self.runs.iterdir())), 1)
        self.assertTrue(restored.trace_file.read_bytes().startswith(before))
        self.assertEqual({name: (self.run / name).read_text() for name in self.notes}, self.notes)
        with self.assertRaises(ValueError):
            restored.resume()

    def test_pause_captures_a_thread_started_after_the_pause_request(self):
        self.app.state["goalThreadId"] = ""
        self.app.pause()
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(checkpoint["goalThreadId"], "")
        self.process.stdout = io.StringIO("\n".join(json.dumps(record) for record in [
            {"kind": "status", "stage": "solve", "node": "author", "threadId": "late-root"},
            {"kind": "workflow_paused", "stage": "solve", "node": "author", "state": checkpoint["state"]},
        ]) + "\n")
        self.app._read_output(self.process, self.token)
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(checkpoint["goalThreadId"], "late-root")
        self.app._save_job_settings({**self.app.state, "goalThreadId": "stale-settings-thread"})
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["goalThreadId"], "late-root")

    def test_pause_before_any_thread_can_resume_initial_start(self):
        self.app.state["goalThreadId"] = ""
        self.paused()
        with patch.object(server.subprocess, "Popen", return_value=Mock(stdin=io.StringIO())) as popen, \
                patch.object(self.app, "_spawn_worker"):
            self.app.resume()
        command = popen.call_args.args[0]
        settings = dict(command[i + 1].split("=", 1) for i, arg in enumerate(command) if arg == "--set")
        self.assertNotIn("goal_thread_id", settings)
        self.assertNotIn("goal_require_resume", settings)

    def test_accepted_author_instruction_is_remembered_on_resume_after_restart(self):
        delivered = self.app._write_author_steer("Keep the boundary case explicit.")
        pending_bytes = (self.run / server.AUTHOR_STEER_FILENAME).read_bytes()
        self.app.pause()
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.process.stdout = io.StringIO("\n".join(json.dumps(record) for record in [
            {"kind": "status", "stage": "repair", "node": "author",
             "label": "Author instruction accepted", "authorSteerDelivered": delivered},
            {"kind": "status", "stage": "repair", "root": False,
             "authorSteerDelivered": "unrelated-subagent-instruction"},
            {"kind": "workflow_paused", "stage": "solve", "node": "author", "state": checkpoint["state"]},
        ]) + "\n")
        self.app._read_output(self.process, self.token)
        settings = json.loads((self.run / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(settings["authorSteerDelivered"], delivered)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["authorSteerDelivered"], delivered)
        with patch.object(server.subprocess, "Popen", return_value=Mock(stdin=io.StringIO())) as popen, \
                patch.object(restored, "_spawn_worker"):
            restored.resume()
        self.assertIn("author_steer_delivered=" + json.dumps(delivered), popen.call_args.args[0])
        self.assertEqual((self.run / server.AUTHOR_STEER_FILENAME).read_bytes(), pending_bytes)

    def test_force_paused_old_worker_uses_saved_controller_fallback(self):
        self.app.pause()
        self.process.wait.return_value = -15
        self.app._read_output(self.process, self.token)
        self.assertEqual(self.app.state["phase"], "paused")
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(checkpoint["state"]["report"], self.report)

    def test_failed_resume_launch_leaves_a_resumable_pause(self):
        self.paused()
        with patch.object(server.subprocess, "Popen", side_effect=OSError("CLI unavailable")):
            with self.assertRaises(OSError):
                self.app.resume()
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertFalse(self.app.has_active_worker())
        self.assertIsNone(self.app.active_token)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["phase"], "paused")

    def test_failed_native_resume_remains_retryable_in_place_after_ui_restart(self):
        self.paused()
        resumed = Mock(stdin=io.StringIO(), stdout=io.StringIO("\n".join(json.dumps(record) for record in [
            {"kind": "failure_result", "stage": "failure", "node": "author",
             "output": "Saved thread unavailable"},
            {"kind": "diagnostic", "stage": "failure", "text": "error: Saved thread unavailable"},
        ]) + "\n"))
        resumed.wait.return_value = resumed.poll.return_value = 1
        with patch.object(server.subprocess, "Popen", return_value=resumed), \
                patch.object(self.app, "_spawn_worker"):
            self.app.resume()
        self.app._read_output(resumed, self.app.active_token)
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertEqual(self.app.state["error"], "Saved thread unavailable")
        self.assertFalse(self.app.has_active_worker())
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(checkpoint["goalThreadId"], "saved-root")
        self.assertEqual(checkpoint["state"]["report"], self.report)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["phase"], "paused")
        self.assertEqual(restored.state["error"], "Saved thread unavailable")
        with patch.object(server.subprocess, "Popen", return_value=Mock(stdin=io.StringIO())) as popen, \
                patch.object(restored, "_spawn_worker"):
            restored.resume()
        command = popen.call_args.args[0]
        self.assertIn('goal_thread_id="saved-root"', command)
        self.assertIn("goal_require_resume=true", command)
        self.assertEqual(popen.call_args.kwargs["cwd"], self.run)
        self.assertEqual(len(list(self.runs.iterdir())), 1)
        self.assertEqual(restored.state["error"], "")

    def test_automatic_author_pause_preserves_reason_state_and_thread_after_restart(self):
        for reason in ("Usage limit reached", "Network connection lost"):
            with self.subTest(reason=reason):
                (self.run / server.PAUSE_FILENAME).unlink(missing_ok=True)
                self.app.state.update(phase="running", stage="solve", activeNode="author")
                self.app.process = self.process
                self.app.active_token = self.app.worker_token = self.token
                workflow_state = {"statement": "Exact statement", "paused": True, "round": 7,
                                  "report": self.report, "solution": self.report["solution"]}
                self.process.stdout = io.StringIO(json.dumps({
                    "kind": "workflow_paused", "stage": "solve", "node": "author",
                    "state": workflow_state, "reason": reason, "threadId": "native-paused-thread",
                }) + "\n")
                self.app._read_output(self.process, self.token)
                self.assertEqual(self.app.state["phase"], "paused")
                self.assertEqual(self.app.state["error"], reason)
                checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
                self.assertEqual(checkpoint["state"], workflow_state)
                self.assertEqual(checkpoint["stage"], "repair")
                self.assertEqual(checkpoint["goalThreadId"], "native-paused-thread")
                self.assertEqual(checkpoint["error"], reason)
                restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
                self.assertEqual(restored.state["phase"], "paused")
                self.assertEqual(restored.state["error"], reason)
                self.assertEqual(restored.state["goalThreadId"], "native-paused-thread")
                self.assertEqual(len(list(self.runs.iterdir())), 1)

    def test_unexpected_author_exit_automatically_creates_resumable_checkpoint(self):
        self.process.stdout = io.StringIO("error: Connection reset\n")
        self.process.wait.return_value = 1
        self.app._read_output(self.process, self.token)
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertIn("Connection reset", self.app.state["error"])
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(checkpoint["goalThreadId"], "saved-root")
        self.assertEqual(checkpoint["state"]["report"], self.report)
        self.assertEqual(checkpoint["stage"], "repair")

    def test_automatic_pause_does_not_override_manual_stop(self):
        self.app.stop()
        self.process.stdout = io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "solve", "node": "author",
            "state": {"statement": "Exact statement"}, "reason": "Connection lost",
            "threadId": "saved-root",
        }) + "\n")
        self.app._read_output(self.process, self.token)
        self.assertEqual(self.app.state["phase"], "done")
        self.assertEqual(self.app.state["error"], "Stopped.")
        self.assertFalse((self.run / server.PAUSE_FILENAME).exists())

    def test_critic_failure_does_not_become_an_author_pause(self):
        self.app.state.update(stage="critic", activeNode="critic")
        self.process.stdout = io.StringIO("error: Critic request failed\n")
        self.process.wait.return_value = 1
        self.app._read_output(self.process, self.token)
        self.assertEqual(self.app.state["phase"], "done")
        self.assertFalse((self.run / server.PAUSE_FILENAME).exists())

    def test_job_manager_resumes_existing_job_and_rejects_concurrent_workspace_use(self):
        self.paused()
        manager = object.__new__(server.Server)
        manager.jobs = {self.run.name: self.app}
        manager.jobs_lock = threading.RLock()
        manager.fixed_app = False
        manager.runs = self.runs
        self.assertEqual(manager.job_list()[0]["continueStoppedLabel"], "Resume")
        busy = server.App(runs=self.runs)
        busy.state.update(phase="running", goalWorkspace=str(self.run.resolve()))
        manager.jobs["busy"] = busy
        with self.assertRaisesRegex(ValueError, "active job"):
            manager.resume_paused_job(self.run.name)
        del manager.jobs["busy"]
        with patch.object(self.app, "resume") as resume:
            resumed = manager.continue_stopped_job(self.run.name)
        self.assertIs(resumed, self.app)
        resume.assert_called_once()
        self.assertEqual(len(manager.jobs), 1)

    def test_paused_time_does_not_consume_remaining_budget(self):
        now = datetime.now(timezone.utc)
        self.app.state.update(startedAt=(now - timedelta(hours=10)).isoformat(),
                              resumedAt=(now - timedelta(seconds=10)).isoformat(), elapsedSeconds=20)
        self.assertAlmostEqual(self.app._elapsed_seconds(), 30, delta=1)

    def test_paused_author_can_extend_its_limit_before_resuming_after_restart(self):
        self.app.state["thinkingHours"] = 1
        self.paused()
        checkpoint = json.loads((self.run / server.PAUSE_FILENAME).read_text())
        self.assertEqual(self.app.set_author_time_limit(2), 2)
        self.assertEqual(json.loads((self.run / server.JOB_SETTINGS_FILENAME).read_text())["thinkingHours"], 2)
        restored = server.restore_saved_app(server.App(self.app.trace_file, self.runs))
        self.assertEqual(restored.state["phase"], "paused")
        self.assertEqual(restored.state["thinkingHours"], 2)
        with patch.object(server.subprocess, "Popen", return_value=Mock(stdin=io.StringIO())) as popen, \
                patch.object(restored, "_spawn_worker"):
            restored.resume()
        command = popen.call_args.args[0]
        self.assertEqual(float(command[command.index("--thinking-hours") + 1]), 2)
        self.assertEqual(float(command[command.index("--elapsed-seconds") + 1]), checkpoint["elapsedSeconds"])

    def test_tex_is_available_only_after_final_output_and_reader_completion(self):
        with self.assertRaises(ValueError):
            self.app.final_tex()
        tex = b"\\documentclass{article}\n\\begin{document}Proof.\\end{document}\n"
        self.app.state.update(phase="running", stage="final", activeNode="latex_editor")
        self.process.stdout = io.StringIO(json.dumps({"kind": "final_result", "stage": "final", "output": tex.decode()}) + "\n")
        self.app._read_output(self.process, self.token)
        self.assertTrue(self.app.snapshot()["canDownloadTex"])
        self.assertEqual(self.app.final_tex(), tex)
        self.assertFalse(any(p.suffix == ".pdf" for p in self.run.iterdir()))

    def test_download_endpoint_requires_auth_and_serves_exact_tex_only(self):
        self.app.state["phase"] = "done"
        self.app.worker_token = None
        self.app._save("final.tex", "\\documentclass{article}\n% Unicode π\n")
        http = server.Server((server.HOST, 0), runs=self.runs)
        http.jobs[self.run.name] = self.app
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        url = http.origin + "/download-tex?job=" + self.run.name
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 403)
        headers = {"X-TCS-Prover-Token": http.token}
        with urlopen(Request(url, headers=headers)) as response:
            self.assertEqual(response.read(), (self.run / "final.tex").read_bytes())
            self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="final.tex"')
        for path in ("/download-tex?job=missing", "/download-pdf?job=" + self.run.name):
            with self.assertRaises(HTTPError):
                urlopen(Request(http.origin + path, headers=headers))
        (self.run / "final.tex").unlink()
        (self.run / "final.tex").symlink_to(self.run / "PROVED.md")
        with self.assertRaises(HTTPError):
            urlopen(Request(url, headers=headers))

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_browser_pause_resume_controls_and_tex_download(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const renderer = source.slice(source.indexOf('function render(next)'), source.indexOf('async function refresh()'));
const handlers = source.slice(source.indexOf('ui.stop.onclick ='), source.indexOf('ui.home.onclick ='));
const setup = `
let state = {}, previousPhase = 'running', currentJob = 'same-job', timer, clock;
const ui = new Proxy({}, {get(target, name) {return target[name] ||= {
  classList: {toggle() {}}, setAttribute() {}, focus() {}, replaceChildren() {}
};}});
const document = {activeElement: null, body: {append() {}}, createElement() {
  return {click() {download = this.download;}, remove() {}};
}};
const show = (element, visible) => element.hidden = !visible;
const retainTrace = entries => entries;
const ingest = () => {}, syncMemoryPanel = () => {}, renderWorkflow = () => {};
const renderClock = () => {}, refresh = () => {};
const setTimeout = () => 1, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
const jobPath = path => path + '?job=same-job', sessionToken = 'secret';
let clicked = '', requested = '', download = '';
const act = path => clicked = path;
const fetch = async (path, options) => {
  requested = path;
  assert.equal(options.headers['X-TCS-Prover-Token'], 'secret');
  return {ok: true, blob: async () => 'Exact saved LaTeX bytes'};
};
const URL = {createObjectURL(blob) {assert.equal(blob, 'Exact saved LaTeX bytes'); return 'blob:tex';}, revokeObjectURL() {}};
`;
const checks = `
(async () => {
  const running = {phase: 'running', stage: 'solve', activeNode: 'author'};
  render(running);
  assert.equal(ui.pause.hidden, false);
  assert.equal(ui.stop.hidden, false);
  assert.equal(ui.resume.hidden, true);
  ui.pause.onclick(); assert.equal(clicked, '/pause');
  render({...running, phase: 'pausing'});
  assert.equal(ui.pause.hidden, true);
  assert.equal(ui.resume.hidden, true);
  assert.equal(ui.stop.hidden, false);
  render({...running, phase: 'paused'});
  assert.equal(ui.run.hidden, false);
  assert.equal(ui.resume.hidden, false);
  assert.equal(ui.stop.hidden, true);
  assert.equal(ui.pause.hidden, true);
  assert.equal(ui.authorSteerControl.hidden, true);
  assert.equal(ui.authorTimeLimitControl.hidden, false);
  assert.equal(ui.setAuthorTimeLimit.disabled, false);
  ui.resume.onclick(); assert.equal(clicked, '/resume');
  render({...running, stage: 'critic', activeNode: 'critic'});
  assert.equal(ui.pause.hidden, true);
  render({...running, phase: 'done', stage: 'final', activeNode: 'latex_editor', canDownloadTex: true});
  assert.equal(ui.downloadTex.hidden, false);
  await ui.downloadTex.onclick();
  assert.equal(requested, '/download-tex?job=same-job');
  assert.equal(download, 'final.tex');
  assert.equal(ui.downloadTex.disabled, false);
})();
`;
Promise.resolve(vm.runInNewContext(setup + renderer + handlers + checks, {assert}))
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js")],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
