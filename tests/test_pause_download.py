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
        def launch(command, **kwargs):
            self.assertEqual(kwargs["cwd"], self.run)
            self.assertFalse((self.run / server.PAUSE_REQUEST_FILENAME).exists())
            self.assertEqual(command[command.index("--elapsed-seconds") + 1], str(checkpoint["elapsedSeconds"]))
            self.assertEqual(command[command.index("--start-node") + 1], "author")
            settings = dict(command[i + 1].split("=", 1) for i, arg in enumerate(command) if arg == "--set")
            self.assertEqual(json.loads(settings["goal_thread_id"]), "saved-root")
            self.assertTrue(json.loads(settings["goal_resume"]))
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
