"""Run a folder of statements as parallel direct proof jobs, offline (no model calls)."""

from datetime import datetime
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
from urllib.request import Request, urlopen

import workflow_runner as runtime
from ui import server

CHEAP = "author_critic_cheap"
STANDARD = "author_critic"


class FixedClock(datetime):
    """Start every job in the same second."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 2, 12, 0, 0, tzinfo=tz)


class FolderBatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        self.manager = object.__new__(server.Server)
        self.manager.runs = self.runs
        self.manager.jobs = {}
        self.manager.jobs_lock = threading.RLock()
        self.manager.fixed_app = False
        self.launches = []
        for change in (mock.patch.object(runtime, "verify_model_credentials"),
                       mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(server, "datetime", FixedClock),
                       mock.patch.object(server.subprocess, "Popen", side_effect=self.popen)):
            change.start()
            self.addCleanup(change.stop)

    def popen(self, argv, **kwargs):
        self.launches.append((argv, kwargs["cwd"]))
        return mock.Mock(stdin=io.StringIO())

    @staticmethod
    def statements(*names):
        return [{"name": name, "statement": f"Statement of {name}: every task has property P."}
                for name in names]

    def test_one_direct_job_per_statement_with_shared_settings_and_unique_folders(self):
        statements = [
            {"name": "a.md", "statement": "Same statement."},
            {"name": "b.md", "statement": "Same statement."},
            {"name": "c.md", "statement": "  Another statement.\n"},
        ]
        started = self.manager.start_direct_batch({
            "statements": statements, "criticRounds": 3, "thinkingHours": 4,
            "authorModel": "gpt-5.6-sol", "criticEffort": "high", "speedMode": "standard",
            "promptOverrides": {"critic": "Shared critic."},
        })
        self.assertEqual(len(started), 3)
        self.assertEqual(len(self.launches), 3)
        names = [app.run_dir.name for app in started]
        self.assertEqual(names, ["2026-10-02_12-00-00_same-statement", "2026-10-02_12-00-00_same-statement-2",
                                 "2026-10-02_12-00-00_another-statement"])
        for app, item, (argv, cwd) in zip(started, statements, self.launches):
            with self.subTest(source=item["name"]):
                self.assertEqual(cwd, app.run_dir)
                runner = argv.index(str(server.ROOT / "workflow_runner.py"))
                self.assertEqual([Path(path).name for path in argv[runner + 1:] if path.endswith(".yaml")],
                                 [f"{CHEAP}.yaml"])
                self.assertNotIn("--start-node", argv)
                self.assertEqual(argv[argv.index("--critic-rounds") + 1], "3")
                self.assertEqual(float(argv[argv.index("--thinking-hours") + 1]), 4)
                self.assertEqual(argv[argv.index("--author-model") + 1], "gpt-5.6-sol")
                self.assertEqual(argv[argv.index("--critic-effort") + 1], "high")
                self.assertEqual(argv[argv.index("--speed") + 1], "standard")
                self.assertEqual(app.state["phase"], "running")
                self.assertEqual((app.state["stage"], app.state["activeNode"]), ("solve", "author"))
                self.assertEqual(app.state["draft"], item["statement"].strip())
                self.assertEqual(app.state["sourceFile"], item["name"])
                self.assertTrue(app.state["skipStatementReview"])
                self.assertIs(app.state["workflow"], server.DIRECT_GRAPH)
                self.assertEqual((app.state["authorWorkflow"], app.state["fileManagement"]), (CHEAP, False))
                self.assertEqual((app.run_dir / "prompts/critic.txt").read_text(), "Shared critic.\n")
                self.assertTrue((app.run_dir / "checked-statement.md").read_text().startswith(
                    "# Statement sent directly to the proof author"))
                settings = json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())
                self.assertEqual((settings["sourceFile"], settings["skipStatementReview"]), (item["name"], True))
                self.assertIs(self.manager.jobs[app.state["runId"]], app)
        listed = {job["runId"]: job for job in self.manager.job_list()}
        self.assertEqual({listed[name]["sourceFile"] for name in names}, {"a.md", "b.md", "c.md"})
        self.assertFalse(any(job["criticOnly"] for job in listed.values()))
        # The source file survives a restart.
        started[0].add_trace({"kind": "request", "stage": "solve", "text": "Prove."})
        restored = server.restore_saved_app(server.App(started[0].trace_file, self.runs))
        self.assertEqual(restored.state["sourceFile"], "a.md")

    def test_explicit_standard_workflow_and_file_management_apply_to_every_job(self):
        started = self.manager.start_direct_batch({
            "statements": self.statements("a.md", "b.md"), "authorWorkflow": STANDARD, "fileManagement": True,
            "researchAudits": {"models": ["gpt-6-astra", "none", "none"], "intervalHours": 1},
        })
        for app in started:
            self.assertEqual((app.state["authorWorkflow"], app.state["fileManagement"]), (STANDARD, True))
            self.assertEqual(app.state["researchAudits"]["models"][0], "gpt-6-astra")
        for argv, _ in self.launches:
            self.assertIn("file_management=true", argv)

    def test_everything_is_validated_before_any_job_starts(self):
        good = self.statements("a.md", "b.md")
        invalid = (
            ({"statements": []}, "between 1 and 200"),
            ({}, "between 1 and 200"),
            ({"statements": "a.md"}, "between 1 and 200"),
            ({"statements": self.statements(*(f"{index}.md" for index in range(201)))}, "between 1 and 200"),
            ({"statements": [*good, "text"]}, "Statement 3 must have"),
            ({"statements": [*good, {"name": "c.md", "statement": "  "}]}, "c.md has no statement"),
            ({"statements": [*good, {"name": "c.md"}]}, "c.md has no statement"),
            ({"statements": [*good, {"name": "c.md", "statement": 7}]}, "c.md has no statement"),
            ({"statements": [*good, {"statement": ""}]}, "Statement 3 has no statement"),
            ({"statements": [*good, {"name": "c.md", "statement": "Task\0"}]}, "c.md contains a NUL"),
            ({"statements": [*good, {"name": "c.md", "statement": "x" * (server.MAX_BATCH_STATEMENT_CHARS + 1)}]},
             "c.md is longer than"),
            ({"statements": [*good, {"name": ["c.md"], "statement": "Task"}]}, "Statement 3: A source file name"),
            ({"statements": [*good, {"name": "c\0.md", "statement": "Task"}]}, "Statement 3: A source file name"),
            ({"statements": good, "authorWorkflow": "bogus"}, "workflow"),
            ({"statements": good, "authorModel": "bogus"}, "Choose"),
            ({"statements": good, "criticRounds": 0}, "round"),
            ({"statements": good, "thinkingHours": -1}, "hours"),
            ({"statements": good, "speedMode": "warp"}, "speed"),
            ({"statements": good, "authorWorkflow": STANDARD, "fileManagement": "yes"}, "File management"),
            ({"statements": good, "authorWorkflow": STANDARD, "fileManagement": True,
              "researchAudits": {"models": ["bogus"]}}, ""),
            ({"statements": good, "promptOverrides": {"author_simple": "No marker."}}, "STATEMENT"),
            ({"statements": good, "promptOverrides": {"bogus": "x"}}, "Prompt overrides"),
        )
        for body, message in invalid:
            with self.subTest(body=str(body)[:120]):
                with self.assertRaisesRegex(ValueError, message):
                    self.manager.start_direct_batch(body)
        self.assertEqual(self.launches, [])
        self.assertFalse(self.runs.exists() and any(self.runs.iterdir()))
        self.assertEqual(self.manager.jobs, {})
        with self.assertRaisesRegex(ValueError, "home screen"):
            self.manager.start_direct_batch({"statements": good}, "some-job")

    def test_a_launch_failure_stops_the_jobs_already_started(self):
        calls = []

        def popen(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 2:
                raise OSError("No space left on device")
            return mock.Mock(stdin=io.StringIO())

        stopped = []
        with mock.patch.object(server.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(server.App, "stop", autospec=True, side_effect=stopped.append):
            with self.assertRaisesRegex(ValueError, r"b\.md: No space left on device\. The 1 job it had "
                                                    r"already started were stopped\."):
                self.manager.start_direct_batch({"statements": self.statements("a.md", "b.md", "c.md")})
        self.assertEqual(len(calls), 2, "No job starts after a failure")
        self.assertEqual([app.state["sourceFile"] for app in stopped], ["a.md"])
        self.assertEqual([app.state["sourceFile"] for app in self.manager.jobs.values()], ["a.md"])

    def test_run_folders_stay_unique_when_jobs_start_together(self):
        count = 24
        barrier = threading.Barrier(count)
        apps, errors = [server.App(runs=self.runs) for _ in range(count)], []

        def create(app):
            barrier.wait()
            try:
                app._new_run("Same statement for every job")
            except Exception as exc:  # A collision would surface as FileExistsError.
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(app,)) for app in apps]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        stem = "2026-10-02_12-00-00_same-statement-for-every-job"
        self.assertEqual(sorted(app.run_dir.name for app in apps),
                         sorted([stem] + [f"{stem}-{number}" for number in range(2, count + 1)]))
        self.assertTrue(all((app.run_dir / "draft.md").is_file() for app in apps))

    def test_http_batch_accepts_a_large_folder_and_returns_the_job_list(self):
        http_server = server.Server((server.HOST, 0), runs=self.runs)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http_server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http_server.shutdown)
        statements = [{"name": f"{index:02d}.md", "statement": f"Statement {index}. " + "Detail. " * 5_000}
                      for index in range(4)]
        body = json.dumps({"statements": statements}).encode()
        self.assertGreater(len(body), server.MAX_REQUEST_BYTES)
        headers = {"Content-Type": "application/json", "X-TCS-Prover-Token": http_server.token}
        with urlopen(Request(http_server.origin + "/direct-batch", data=body, headers=headers)) as response:
            result = json.load(response)
        self.assertEqual(len(result["startedJobs"]), 4)
        self.assertEqual(len(set(result["startedJobs"])), 4)
        listed = {job["runId"]: job for job in result["jobs"]}
        self.assertEqual(sorted(listed[run_id]["sourceFile"] for run_id in result["startedJobs"]),
                         ["00.md", "01.md", "02.md", "03.md"])
        self.assertTrue(all(listed[run_id]["phase"] == "running" for run_id in result["startedJobs"]))
        self.assertEqual(len(self.launches), 4)

        connection = http.client.HTTPConnection(server.HOST, http_server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/direct-batch", body=b'{"statements": []}', headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("between 1 and 200", json.loads(response.read())["error"])


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class FolderBatchBrowserTests(unittest.TestCase):
    def test_folder_filtering_confirmation_and_request_body(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const slice = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
const code = slice('function managesResearchFiles(', 'function selectedAuthorPrompt(')
  + slice('const promptLabels =', 'function clockText(')
  + slice('async function startReview(', 'async function startLatexOnly(');
const setup = `
let currentJob = '', activePrompt = 'review', timer, jobsTimer, reviewPending = false;
let promptValues = {}, promptDrafts = {}, promptOriginals = {}, promptOverrides = {critic: 'Shared critic.'};
let state = {phase: 'input', workflow: {settings: {}}};
const simpleOnlyWorkflows = new Set(['author_critic_cheap']);
const values = {authorWorkflow: 'author_critic_cheap', authorModel: 'gpt-6-astra', criticModel: 'gpt-5.6-sol',
  writerModel: 'gpt-5.6-luna', authorEffort: 'ultra', criticEffort: 'high', writerEffort: 'medium',
  speedMode: 'fast', reasoningSummary: 'concise', criticRounds: '2', thinkingHours: '168'};
const ui = new Proxy({fileManagement: {checked: false}}, {get(target, name) {
  return target[name] ||= {dataset: {}, value: values[name], scrollIntoView() { this.scrolled = true; }};
}});
const show = (element, visible) => { element.hidden = !visible; };
const clearTimeout = () => {};
const researchAuditValues = () => ({intervalHours: 2, models: ['gpt-6-astra', 'none', 'none']});
let questions = [], answer = true, sent = [], failure = null, rendered = null, polled = 0;
const confirm = question => { questions.push(question); return answer; };
const request = async (path, body) => {
  sent.push({path, body});
  if (failure) throw new Error(failure);
  return {startedJobs: body.statements.map((item, index) => 'job-' + index), jobs: [{runId: 'job-0'}],
          summaryPath: '/runs/batches/b1/summary.md'};
};
const renderJobs = jobs => { rendered = jobs; };
const loadJobs = () => { polled += 1; };
const file = (path, content) => ({name: path.split('/').at(-1), webkitRelativePath: path,
  size: content.length, text: async () => content});
`;
const checks = `
(async () => {
  const folder = [
    file('statements/b.md', 'Prove B.'), file('statements/A.md', 'Prove A.'),
    file('statements/c.MD', 'Prove C.'), file('statements/notes.txt', 'Not a statement.'),
    file('statements/nested/d.md', 'Nested.'), file('statements/.md', 'No name.'),
    file('statements/empty.md', '  \\n'), file('statements/a.markdown', 'Other suffix.'),
  ];
  assert.deepEqual(folderMarkdownFiles(folder).map(item => item.name), ['A.md', 'b.md', 'c.MD', 'empty.md']);
  assert.deepEqual(folderMarkdownFiles([file('loose.md', 'No folder.')]), []);
  const read = await folderStatements(folder);
  assert.deepEqual(JSON.parse(JSON.stringify(read)), {
    statements: [{name: 'A.md', statement: 'Prove A.'}, {name: 'b.md', statement: 'Prove B.'},
                 {name: 'c.MD', statement: 'Prove C.'}],
    empty: ['empty.md'],
  });

  await startFolderBatch(folder);
  assert.equal(questions.length, 1);
  assert.match(questions[0], /^Start 3 parallel jobs\\? Each runs the selected workflow and uses its own quota\\./);
  assert.match(questions[0], /Folder: statements\\nWorkflow: author_critic_cheap\\n/);
  assert.match(questions[0], /statement review is skipped/);
  assert.match(questions[0], /Skipped 1 empty file: empty\\.md\\./);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].path, '/direct-batch');
  const body = JSON.parse(JSON.stringify(sent[0].body));
  assert.deepEqual(body.statements.map(item => item.name), ['A.md', 'b.md', 'c.MD']);
  assert.equal(body.statements[0].statement, 'Prove A.');
  assert.deepEqual(body, {
    statements: body.statements, folder: 'statements',
    authorModel: 'gpt-6-astra', criticModel: 'gpt-5.6-sol', writerModel: 'gpt-5.6-luna',
    authorEffort: 'ultra', criticEffort: 'high', writerEffort: 'medium',
    promptOverrides: {critic: 'Shared critic.'}, authorWorkflow: 'author_critic_cheap', latexWriter: false, fileManagement: false,
    criticRounds: 2, thinkingHours: 168,
    researchAudits: {intervalHours: 0, models: ['none', 'none', 'none']},
    speedMode: 'fast', reasoningSummary: 'concise',
  });
  assert.equal(ui.batchStatus.hidden, false);
  assert.equal(ui.batchStatus.textContent, 'Started 3 jobs from statements.'
    + '\\nSummary of this folder run, rewritten as jobs finish: /runs/batches/b1/summary.md'
    + '\\nSkipped 1 empty file: empty.md.');
  assert.deepEqual(rendered, [{runId: 'job-0'}]);
  assert.equal(ui.jobsPanel.scrolled, true);
  assert.equal(polled, 1);

  // A second change event while a batch is being sent starts nothing more.
  questions = [];
  await Promise.all([startFolderBatch(folder), startFolderBatch(folder)]);
  assert.equal(questions.length, 1);
  assert.equal(sent.length, 2);
  assert.equal(ui.runFolder.disabled, false);

  // Cancelling the confirmation sends nothing.
  answer = false;
  await startFolderBatch(folder);
  assert.equal(sent.length, 2);
  answer = true;

  // A folder without usable Markdown files is an error before any question.
  questions = [];
  await startFolderBatch([file('drafts/empty.md', ''), file('drafts/notes.txt', 'x'), file('drafts/sub/a.md', 'x')]);
  assert.equal(questions.length, 0);
  assert.equal(sent.length, 2);
  assert.equal(ui.notice.hidden, false);
  assert.match(ui.notice.textContent, /No Markdown statements were found directly inside drafts\\. Skipped 1 empty file: empty\\.md\\./);

  // A server error is shown, and the job list keeps polling.
  failure = 'Could not start the job for b.md: disk full.';
  await startFolderBatch(folder);
  assert.equal(sent.length, 3);
  assert.equal(ui.notice.textContent, failure);
  assert.equal(ui.batchStatus.hidden, true);
  failure = null;

  // Files the server could never accept are refused before reading or sending them.
  const huge = {...file('big/huge.md', 'x'), size: 9 * 1024 * 1024};
  await startFolderBatch([huge]);
  assert.equal(sent.length, 3);
  assert.match(ui.notice.textContent, /huge\\.md is larger than 8 MB/);
})();
`;
Promise.resolve(vm.runInNewContext(setup + code + checks, {assert, TextEncoder}))
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js")],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_home_page_markup_offers_the_new_defaults_and_controls(self):
        page = (server.UI / "index.html").read_text(encoding="utf-8")
        self.assertRegex(page, r'<input id="skipStatementReview" type="checkbox" checked>')
        self.assertRegex(page, r'<option value="author_critic_cheap" selected>[^<]*default')
        self.assertNotRegex(page, r'<option value="author_critic" selected>')
        self.assertIn('<input type="radio" name="problemMode" value="critic">', page)
        self.assertIn('<textarea id="proofInput"', page)
        self.assertRegex(page, r'<input id="folderInput" type="file" webkitdirectory multiple hidden')
        self.assertIn('id="runFolderButton"', page)
        self.assertIn('id="batchStatus"', page)
        script = (server.UI / "app.js").read_text(encoding="utf-8")
        # Before the first poll, and after leaving a job, the form keeps review skipped.
        self.assertEqual(script.count('problemMode: "statement", skipStatementReview: true,'), 2)
        self.assertNotIn("skipStatementReview: false", script)


if __name__ == "__main__":
    unittest.main()
