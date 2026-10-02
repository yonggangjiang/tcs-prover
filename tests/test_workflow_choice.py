"""A job's author/critic workflow is chosen once and kept by every relaunch, offline."""

import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import workflow_runner as runtime
from ui import cli, server

CHEAP = "author_critic_cheap"
STANDARD = "author_critic"


def set_options(argv):
    """Return the runner's --set NAME=VALUE options as a dictionary."""

    return dict(argv[index + 1].split("=", 1) for index, argument in enumerate(argv) if argument == "--set")


class WorkflowChoiceTests(unittest.TestCase):
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
        self.cheap = runtime.builtin_workflow(CHEAP)["prompts"]
        self.standard = runtime.builtin_workflow(STANDARD)["prompts"]

    def launch(self, start):
        """Run one job action with the runner process mocked; return its result and argv."""

        with mock.patch.object(server.subprocess, "Popen",
                               return_value=mock.Mock(stdin=io.StringIO())) as popen:
            result = start()
        self.assertEqual(popen.call_count, 1)
        return result, popen.call_args.args[0]

    def assert_graphs(self, argv, workflow):
        runner = argv.index(str(server.ROOT / "workflow_runner.py"))
        self.assertEqual(argv[runner + 1:runner + 3],
                         [str(server.WORKFLOWS / f"{workflow}.yaml"), str(server.WORKFLOWS / "clean_up.yaml")])

    def start_cheap_job(self, **body):
        return self.launch(lambda: self.manager.start_direct_job(
            {"statement": "Exact statement", "authorWorkflow": CHEAP, **body}))

    @staticmethod
    def settle(app, **state):
        """Mark a launched job idle, as its finished reader would."""

        app.process = app.active_token = app.worker_token = None
        app.state.update(phase="done", **state)

    def test_default_prompts_follow_the_selected_workflow(self):
        standard = server.default_prompts()
        self.assertEqual(standard, server.default_prompts(STANDARD))
        for name in ("author", "author_simple", "critic"):
            self.assertEqual(standard[name], self.standard[name])
        cheap = server.default_prompts(CHEAP)
        self.assertEqual(cheap["author"], self.cheap["author"])
        self.assertEqual(cheap["author_simple"], cheap["author"])
        self.assertEqual(cheap["critic"], self.cheap["critic"])
        self.assertEqual((cheap["review"], cheap["final"]), (standard["review"], standard["final"]))
        self.assertEqual(server.home_state()["workflow"]["settings"]["promptsByWorkflow"],
                         {STANDARD: standard, CHEAP: cheap})
        with self.assertRaisesRegex(ValueError, "workflow"):
            server.default_prompts("clean_up")

    def test_new_web_job_runs_the_cheap_graph_in_simple_mode_with_its_own_prompts(self):
        app, argv = self.start_cheap_job(
            fileManagement=True, researchAudits={"models": ["gpt-6-astra", "none", "none"], "intervalHours": 1})
        self.assert_graphs(argv, CHEAP)
        self.assertIs(json.loads(set_options(argv)["file_management"]), False)
        self.assertEqual(app.state["authorWorkflow"], CHEAP)
        self.assertIs(app.state["fileManagement"], False)
        self.assertEqual(app.state["researchAudits"]["models"], ["none"] * 3)
        prompt_files = {}
        for role in ("author", "critic", "final"):
            path = Path(argv[argv.index(f"--{role}-prompt-file") + 1])
            self.assertEqual(path, app.run_dir / "prompts" / f"{role}.txt")
            prompt_files[role] = path.read_text(encoding="utf-8")
        self.assertEqual(prompt_files["author"].strip(), self.cheap["author"].strip())
        self.assertEqual(prompt_files["critic"].strip(), self.cheap["critic"].strip())
        # The runner lets these files replace the YAML prompts of the same names.
        with mock.patch.object(runtime, "require_model_credentials"):
            prompts = runtime.prepare(runtime.builtin_workflow(CHEAP),
                                      {"file_management": False, "prompts": prompt_files})
        self.assertEqual(prompts["author"].strip(), self.cheap["author"].strip())
        self.assertEqual(prompts["critic"].strip(), self.cheap["critic"].strip())
        settings = json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual((settings["authorWorkflow"], settings["fileManagement"]), (CHEAP, False))
        # No research audits or fresh-eyes solvers start for a simple-mode job.
        self.assertIsNone(app._start_research_audits(app.worker_token))
        self.assertIsNone(app.research_audits)
        self.assertEqual(app.snapshot()["workflow"]["settings"]["prompts"]["critic"], self.cheap["critic"])

    def test_standard_web_job_is_unchanged(self):
        app, argv = self.launch(lambda: self.manager.start_direct_job({"statement": "Exact statement"}))
        self.assert_graphs(argv, STANDARD)
        self.assertEqual(app.state["authorWorkflow"], STANDARD)
        self.assertEqual((app.run_dir / "prompts/author.txt").read_text().strip(),
                         self.standard["author_simple"].strip())
        self.assertEqual((app.run_dir / "prompts/critic.txt").read_text().strip(), self.standard["critic"].strip())

    def test_reviewed_cheap_statement_is_approved_into_the_cheap_graph(self):
        app = self.manager.start_job({"statement": "Rough task", "authorWorkflow": CHEAP})
        self.assertEqual(app.state["phase"], "reviewing")
        self.assertEqual((app.run_dir / "prompts/author.txt").read_text().strip(), self.cheap["author"].strip())
        app.state.update(phase="reviewed", review={"statement": "Exact statement", "notes": ""})
        _, argv = self.launch(lambda: app.approve())
        self.assert_graphs(argv, CHEAP)

    def test_paused_cheap_job_resumes_the_cheap_graph_after_a_restart(self):
        app, _ = self.start_cheap_job()
        self.settle(app)
        app._save_pause({"status": "paused", "node": "author", "stage": "solve",
                         "state": {"statement": "Exact statement"}, "elapsedSeconds": 12.5,
                         "goalThreadId": "root-thread"})
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual((restored.state["phase"], restored.state["authorWorkflow"]), ("paused", CHEAP))
        self.assertIs(restored.state["fileManagement"], False)
        _, argv = self.launch(restored.resume)
        self.assert_graphs(argv, CHEAP)
        self.assertEqual(argv[argv.index("--start-node") + 1], "author")
        self.assertIs(json.loads(set_options(argv)["file_management"]), False)
        self.assertEqual(json.loads(set_options(argv)["goal_thread_id"]), "root-thread")

    def test_critic_and_author_continuations_keep_the_cheap_graph(self):
        source, _ = self.start_cheap_job()
        source._save(runtime.SAVED_CANDIDATE_FILENAME, "Complete candidate proof.\n")
        self.settle(source, stage="critic", activeNode="critic", error="Stopped.",
                    manuallyStopped=True, stoppedStage="critic")
        critic, argv = self.launch(lambda: self.manager.continue_stopped_job(source.state["runId"]))
        self.assertIsNot(critic, source)
        self.assert_graphs(argv, CHEAP)
        self.assertEqual(argv[argv.index("--start-node") + 1], "critic")
        self.assertEqual((critic.state["authorWorkflow"], critic.state["fileManagement"]), (CHEAP, False))
        self.assertEqual((critic.run_dir / "prompts/critic.txt").read_text().strip(), self.cheap["critic"].strip())
        self.assertEqual(json.loads((critic.run_dir / server.JOB_SETTINGS_FILENAME).read_text())["authorWorkflow"],
                         CHEAP)

        self.settle(critic)
        source.state.update(stage="solve", activeNode="author", stoppedStage="solve")
        author, argv = self.launch(lambda: self.manager.continue_stopped_job(source.state["runId"]))
        self.assert_graphs(argv, CHEAP)
        self.assertEqual(author.state["authorWorkflow"], CHEAP)
        self.assertEqual(json.loads(set_options(argv)["goal_cwd"]), str(source.run_dir.resolve()))

        self.settle(author)
        research, argv = self.launch(lambda: self.manager.start_saved_research_job(source.run_dir))
        self.assert_graphs(argv, CHEAP)
        self.assertEqual(research.state["authorWorkflow"], CHEAP)

    def test_job_settings_round_trip_and_legacy_runs_default_to_author_critic(self):
        app, _ = self.start_cheap_job()
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["authorWorkflow"], CHEAP)
        self.assertEqual(restored.state["criticPrompt"], self.cheap["critic"].strip())
        self.assertEqual(restored._proof_workflows_locked(), [f"{CHEAP}.yaml", "clean_up.yaml"])
        self.assertIn("nodes", restored.snapshot()["workflow"], "The sidebar graph keeps its own key")
        self.assertEqual(server.saved_author_workflow(app.run_dir), CHEAP)
        self.assertEqual(server.saved_research_source(app.run_dir)["settings"]["authorWorkflow"], CHEAP)

        for index, saved in enumerate((None, {"thinkingHours": 2}, {"authorWorkflow": "retired_graph"},
                                       {"authorWorkflow": [CHEAP]})):
            with self.subTest(saved=saved):
                directory = self.runs / f"legacy-{index}"
                directory.mkdir()
                (directory / "transcript.jsonl").write_text("")
                if saved is not None:
                    (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(saved))
                legacy = server.restore_saved_app(server.App(directory / "transcript.jsonl", self.runs))
                self.assertEqual(legacy.state["authorWorkflow"], STANDARD)
                self.assertTrue(legacy.state["fileManagement"])
                self.assertEqual(legacy.state["authorPrompt"], server.default_prompts()["author"])
                self.assertEqual(legacy._proof_workflows_locked(), ["author_critic.yaml", "clean_up.yaml"])
                self.assertEqual(server.saved_author_workflow(directory), STANDARD)
                self.assertEqual(legacy.snapshot()["workflow"]["settings"]["prompts"], server.default_prompts())

    def test_unreadable_cheap_yaml_only_breaks_cheap_launches(self):
        app, _ = self.start_cheap_job()
        self.settle(app)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        workflows = Path(folder.name) / "workflows"
        shutil.copytree(server.WORKFLOWS, workflows)
        (workflows / f"{CHEAP}.yaml").write_text("nodes: [\n", encoding="utf-8")
        with mock.patch.object(server.runtime, "WORKFLOWS", workflows):
            self.assertEqual(list(server.home_state()["workflow"]["settings"]["promptsByWorkflow"]), [STANDARD])
            self.assertEqual(app.snapshot()["workflow"]["settings"]["prompts"], {})
            self.assertEqual([job["runId"] for job in self.manager.job_list()], [app.state["runId"]])
            restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
            self.assertEqual(restored.state["authorWorkflow"], CHEAP)
            with self.assertRaisesRegex(ValueError, "Cannot read workflow"):
                self.manager.start_direct_job({"statement": "Task", "authorWorkflow": CHEAP})
            _, argv = self.launch(lambda: self.manager.start_direct_job({"statement": "Task"}))
        self.assert_graphs(argv, STANDARD)

    def test_invalid_workflow_is_rejected_before_creating_a_run(self):
        for invalid in ("bogus", "cheap", f"{CHEAP}.yaml", "clean_up", "research_audit", "", 1, [CHEAP], {"name": CHEAP}):
            for start in (self.manager.start_direct_job, self.manager.start_job):
                with self.subTest(invalid=invalid, start=start.__name__):
                    with self.assertRaisesRegex(ValueError, "workflow"):
                        start({"statement": "Task", "authorWorkflow": invalid})
        with self.assertRaisesRegex(ValueError, "workflow"):
            server.App._workflow_options(author_workflow="bogus")
        self.assertFalse(self.runs.exists())

    def test_retries_and_continuations_cannot_switch_workflow(self):
        reviewed = self.manager.start_job({"statement": "Rough task", "authorWorkflow": CHEAP})
        reviewed.state["phase"] = "reviewed"
        with self.assertRaisesRegex(ValueError, "new job"):
            self.manager.start_job({"statement": "Revised task", "authorWorkflow": STANDARD},
                                   reviewed.state["runId"])
        retried = self.manager.start_job({"statement": "Revised task", "feedback": "Clarify."},
                                         reviewed.state["runId"])
        self.assertEqual(retried.state["authorWorkflow"], CHEAP)
        self.assertEqual(server.web_author_workflow({}, {"fileManagement": True}), STANDARD)
        with self.assertRaisesRegex(ValueError, "new job"):
            server.web_author_workflow({"authorWorkflow": CHEAP}, {"fileManagement": True})

        source, _ = self.start_cheap_job()
        self.settle(source)
        runs_before = set(self.runs.iterdir())
        author = server.App(runs=self.runs)
        with mock.patch.object(author, "_launch_solver_locked") as launch:
            with self.assertRaisesRegex(ValueError, "original workflow"):
                author.start_direct_statement("Exact statement", continuation_source=source.run_dir,
                                              author_workflow=STANDARD)
        launch.assert_not_called()
        with self.assertRaisesRegex(ValueError, "original workflow"):
            self.manager.start_saved_research_job(source.run_dir, {"authorWorkflow": STANDARD})
        critic = server.App(runs=self.runs)
        with mock.patch.object(critic, "_launch_critic_resume_locked") as launch:
            with self.assertRaisesRegex(ValueError, "original workflow"):
                critic.start_critic_resume("Exact statement", "Candidate.", source_run=source.run_dir,
                                           author_workflow=STANDARD)
        launch.assert_not_called()
        self.assertEqual(set(self.runs.iterdir()), runs_before)
        with mock.patch.object(author, "_launch_solver_locked", return_value=(object(), object())):
            author.start_direct_statement("Exact statement", continuation_source=source.run_dir)
        self.assertEqual(author.state["authorWorkflow"], CHEAP)


class WorkflowCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.runs = self.directory / "runs"
        self.statement = self.directory / "statement.md"
        self.statement.write_text("Exact statement.\n", encoding="utf-8")
        for change in (mock.patch.object(runtime, "verify_model_credentials"),
                       mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(cli.runtime, "configure_standard_streams")):
            change.start()
            self.addCleanup(change.stop)

    def run_markdown(self, *flags):
        """Run the terminal Markdown command up to the runner launch."""

        launched = {}

        def launch(app, statement):
            launched.update(
                workflow=app.state["authorWorkflow"], managed=app.state["fileManagement"],
                graphs=app._proof_workflows_locked(), settings=set_options(app._proof_options_locked()),
                author=(app.run_dir / "prompts" / "author.txt").read_text(encoding="utf-8"),
            )
            return object(), object()

        original = cli.run_headless_markdown

        def run(path, **settings):
            return original(path, runs=self.runs, output_stream=io.StringIO(),
                            error_stream=io.StringIO(), **settings)

        with mock.patch.object(cli.sys, "argv", ["web_ui.py", str(self.statement), *flags]), \
                mock.patch.object(cli, "run_headless_markdown", side_effect=run), \
                mock.patch.object(server.App, "_launch_solver_locked", autospec=True, side_effect=launch):
            self.assertEqual(cli.main(), 0)
        return launched

    def test_markdown_run_default_is_unchanged_and_flag_selects_cheap_simple_mode(self):
        default = self.run_markdown()
        self.assertEqual((default["workflow"], default["managed"]), (STANDARD, True))
        self.assertEqual(default["graphs"], ["author_critic.yaml", "clean_up.yaml"])
        self.assertEqual(default["settings"]["file_management"], "true")
        self.assertEqual(default["author"].strip(), server.default_prompts()["author"].strip())
        cheap_author = server.default_prompts(CHEAP)["author"].strip()
        for flags in (("--workflow", "cheap"), ("-workflow", CHEAP), (f"--workflow={CHEAP}",)):
            with self.subTest(flags=flags):
                launched = self.run_markdown(*flags)
                self.assertEqual((launched["workflow"], launched["managed"]), (CHEAP, False))
                self.assertEqual(launched["graphs"], [f"{CHEAP}.yaml", "clean_up.yaml"])
                self.assertEqual(launched["settings"]["file_management"], "false")
                self.assertEqual(launched["author"].strip(), cheap_author)

    def test_unknown_workflow_flag_is_rejected(self):
        with mock.patch.object(cli.sys, "argv", ["web_ui.py", str(self.statement), "--workflow", "bogus"]), \
                mock.patch.object(cli.sys, "stderr", io.StringIO()) as error:
            with self.assertRaises(SystemExit) as stopped:
                cli.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("invalid workflow", error.getvalue())

    def test_resume_paths_forward_the_workflow_only_when_supplied(self):
        source = self.runs / "saved"
        source.mkdir(parents=True)
        (source / "checked-statement.md").write_text("# Checked statement\n\nExact statement.\n")
        (source / runtime.SAVED_CANDIDATE_FILENAME).write_text("Complete proof.\n")
        (source / server.JOB_SETTINGS_FILENAME).write_text(
            json.dumps({"authorWorkflow": CHEAP, "fileManagement": False}))
        for flags, expected in (((), None), (("--workflow", "cheap"), CHEAP), (("-workflow=author_critic",), STANDARD)):
            for option, method in (("--resume-author", "start_saved_research_job"),
                                   ("--resume-critic", "start_saved_critic_job")):
                with self.subTest(flags=flags, option=option):
                    manager = mock.Mock()
                    getattr(manager, method).return_value.state = {"runId": "continued"}
                    manager.serve_forever.side_effect = KeyboardInterrupt
                    with mock.patch.object(cli.sys, "argv", ["web_ui.py", option, str(source), "--no-browser", *flags]), \
                            mock.patch.object(cli, "Server", return_value=manager), \
                            mock.patch.object(cli.sys, "stdout", io.StringIO()):
                        self.assertEqual(cli.main(), 0)
                    settings = getattr(manager, method).call_args.args[1]
                    if option == "--resume-author":
                        self.assertEqual(settings, {} if expected is None else {"authorWorkflow": expected})
                    elif expected is None:
                        self.assertNotIn("authorWorkflow", settings)
                    else:
                        self.assertEqual(settings["authorWorkflow"], expected)


class BrowserWorkflowChoiceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_selection_locks_file_management_loads_its_prompts_and_reaches_the_request(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function managesResearchFiles('), source.indexOf('// Keep the compact footer label'))
  + source.slice(source.indexOf('const promptLabels ='), source.indexOf('function clockText('))
  + source.slice(source.indexOf('async function startReview('), source.indexOf('async function startLatexOnly('));
const setup = `
let currentJob = '', activePrompt = 'review', timer, jobsTimer, reviewPending = false;
let promptValues = {}, promptDrafts = {}, promptOriginals = {}, promptOverrides = {};
let state = {phase: 'input', workflow: WORKFLOW};
const simpleOnlyWorkflows = new Set(['author_critic_cheap']);
const ui = new Proxy({
  authorWorkflow: {value: 'author_critic', disabled: false},
  fileManagement: {checked: true, disabled: false},
  statementReviewOnly: {checked: false}, skipStatementReview: {checked: true},
  promptDialog: {open: false}, problemModes: [],
}, {get(target, name) {return target[name] ||= {dataset: {}};}});
const show = (element, visible) => { element.hidden = !visible; };
const updateModelSummary = () => {}, clearTimeout = () => {}, render = () => {};
const history = {pushState() {}}, jobUrl = () => '', jobPath = path => path;
const researchAuditValues = () => ({intervalHours: 2, models: ['gpt-6-astra', 'none', 'none']});
let sent;
const request = async (path, body) => { sent = {path, body}; return {runId: 'new-job'}; };
`;
const checks = `
(async () => {
  const standard = WORKFLOW.settings.promptsByWorkflow.author_critic;
  const cheap = WORKFLOW.settings.promptsByWorkflow.author_critic_cheap;
  setProblemMode('statement');
  assert.equal(ui.fileManagement.disabled, false);
  assert.equal(ui.fileManagement.checked, true);
  assert.equal(ui.authorWorkflowSetting.hidden, false);
  syncPrompts();
  assert.equal(promptValues.critic, standard.critic);

  ui.authorWorkflow.value = 'author_critic_cheap';
  setProblemMode('statement');
  assert.equal(ui.fileManagement.checked, false);
  assert.equal(ui.fileManagement.disabled, true);
  assert.equal(ui.researchAuditsSetting.hidden, true);
  assert.equal(ui.authorPromptTab.dataset.prompt, 'author_simple');
  assert.match(ui.fileManagementHelp.textContent, /cost-optimized/);
  syncPrompts();
  assert.equal(workflowPrompts(), cheap);
  assert.equal(promptValues.author_simple, cheap.author_simple);
  assert.equal(promptValues.critic, cheap.critic);
  await startReview('Exact statement');
  assert.equal(sent.path, '/direct');
  assert.equal(sent.body.authorWorkflow, 'author_critic_cheap');
  assert.equal(sent.body.fileManagement, false);
  assert.deepEqual([...sent.body.researchAudits.models], ['none', 'none', 'none']);

  ui.authorWorkflow.value = 'author_critic';
  setProblemMode('statement');
  assert.equal(ui.fileManagement.disabled, false);
  assert.equal(workflowPrompts(), standard);
  ui.skipStatementReview.checked = false;
  currentJob = '';
  await startReview('Rough task');
  assert.equal(sent.path, '/review');
  assert.equal(sent.body.authorWorkflow, 'author_critic');

  ui.statementReviewOnly.checked = true;
  setProblemMode('statement');
  assert.equal(ui.authorWorkflowSetting.hidden, true);
  ui.statementReviewOnly.checked = false;
  setProblemMode('latex');
  assert.equal(ui.authorWorkflowSetting.hidden, true);

  // A saved job keeps its workflow whatever the home page selection says.
  state = {runId: 'saved-job', phase: 'input', authorWorkflow: 'author_critic_cheap', fileManagement: false,
    workflow: {settings: {prompts: cheap}}};
  setProblemMode('statement');
  assert.equal(selectedAuthorWorkflow(), 'author_critic_cheap');
  assert.equal(ui.authorWorkflow.disabled, true);
  assert.equal(ui.fileManagement.disabled, true);
  assert.equal(workflowPrompts(), cheap);
})();
`;
Promise.resolve(vm.runInNewContext(setup + code + checks, {assert, WORKFLOW: JSON.parse(process.argv[2])}))
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js"),
             json.dumps(server.home_state()["workflow"])],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
