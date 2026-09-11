"""New web jobs use current YAML, never remembered browser or server defaults."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from ui import server


class PromptDefaultsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.workflows = self.directory / "workflows"
        shutil.copytree(server.WORKFLOWS, self.workflows)
        for change in (
            patch.object(server.runtime, "WORKFLOWS", self.workflows),
            patch.object(server.runtime, "verify_model_credentials"),
            patch.object(server.App, "_spawn_worker"),
            patch.object(server.App, "_launch_solver_locked", return_value=(object(), object())),
            patch.object(server.App, "_launch_final_locked", return_value=(object(), object())),
        ):
            change.start()
            self.addCleanup(change.stop)
        self.manager = object.__new__(server.Server)
        self.manager.runs = self.directory / "runs"
        self.manager.jobs = {}
        self.manager.jobs_lock = threading.RLock()
        self.manager.fixed_app = False

    def edit_yaml(self, version):
        for filename, names in (("author_critic.yaml", ("author", "critic")),
                                ("clean_up.yaml", ("final",))):
            path = self.workflows / filename
            workflow = yaml.safe_load(path.read_text())
            for name in names:
                workflow["prompts"][name] = f"{version} {name} policy." + (
                    "\n\n[STATEMENT]" if name == "author" else ""
                )
            path.write_text(yaml.safe_dump(workflow, sort_keys=False))

    def test_defaults_reload_after_yaml_changes_in_the_same_server(self):
        self.edit_yaml("First")
        original = server.empty_state()
        app = server.App(runs=self.manager.runs)
        self.edit_yaml("Current")
        latest = server.empty_state()
        self.assertTrue(original["authorPrompt"].startswith("First"))
        for name in ("author", "critic", "final"):
            expected = server.default_prompts()[name]
            self.assertEqual(latest[f"{name}Prompt"], expected)
            self.assertEqual(latest["workflow"]["settings"]["prompts"][name], expected)
            self.assertEqual(app.snapshot()["workflow"]["settings"]["prompts"][name], expected)

    def test_new_web_jobs_ignore_old_page_prompts_and_save_current_yaml(self):
        self.edit_yaml("Old page")
        old_page = server.empty_state()
        self.edit_yaml("Current")
        body = {**old_page, "statement": "Exact statement", "content": "A proof"}
        for launch, roles in ((self.manager.start_job, ("author", "critic", "final")),
                              (self.manager.start_direct_job, ("author", "critic", "final")),
                              (self.manager.start_latex_job, ("final",))):
            with self.subTest(launch=launch.__name__):
                app = launch(body)
                for name in roles:
                    expected = server.default_prompts()[name]
                    self.assertEqual(app.state[f"{name}Prompt"], expected)
                    self.assertEqual((app.run_dir / "prompts" / f"{name}.txt").read_text(), expected + "\n")
                if "author" in roles:
                    options = app._proof_options_locked()
                    path = Path(options[options.index("--author-prompt-file") + 1])
                    self.assertEqual(path.read_text().strip(), server.default_prompts()["author"])
                for name in server.RESEARCH_MEMORY_FILES:
                    self.assertFalse((app.run_dir / name).exists(), "Only the LLM creates memory")

    def test_explicit_browser_edit_applies_to_one_job_only(self):
        self.edit_yaml("Current")
        custom = "Deliberate per-job instructions. [STATEMENT]"
        edited = self.manager.start_direct_job({
            "statement": "First task", "promptOverrides": {"author": custom},
        })
        fresh = self.manager.start_direct_job({"statement": "Next task"})
        self.assertEqual(edited.state["authorPrompt"], custom)
        self.assertEqual(fresh.state["authorPrompt"], server.default_prompts()["author"])
        self.assertEqual(edited.state["criticPrompt"], fresh.state["criticPrompt"])

    def test_review_retry_and_run_history_keep_the_job_prompt(self):
        self.edit_yaml("Original")
        app = self.manager.start_job({"statement": "Task to review"})
        original = app.state["authorPrompt"]
        app.state["phase"] = "reviewed"
        self.edit_yaml("Current")
        snapshot = app.snapshot()
        self.assertEqual(snapshot["authorPrompt"], original)
        self.assertEqual(snapshot["workflow"]["settings"]["prompts"]["author"],
                         server.default_prompts()["author"])
        retried = self.manager.start_job({"statement": "Revised task"}, app.state["runId"])
        self.assertEqual(retried.state["authorPrompt"], original)


class BrowserPromptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_stale_storage_and_unedited_roles_never_become_overrides(self):
        # Exercise the actual prompt editor functions without starting a model or browser.
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const app = fs.readFileSync(process.argv[1], 'utf8');
const functions = app.slice(app.indexOf('const promptLabels ='), app.indexOf('function clockText('));
const setup = `
let currentJob = '', activePrompt = 'review';
let promptValues = {}, promptDrafts = {}, promptOriginals = {}, promptOverrides = {};
const defaults = {review: 'Review', author: 'YAML author [STATEMENT]', critic: 'Critic', final: 'Final'};
let state = {phase: 'input', authorPrompt: 'Old state [STATEMENT]', workflow: {settings: {prompts: defaults}}};
const localStorage = {getItem: () => JSON.stringify({author: 'Old browser [STATEMENT]'}),
                      setItem: () => {throw Error('Prompts must not be persisted');}};
const ui = {promptEditor: {value: ''}, notice: {}, promptEditorLabel: {}, promptEditorHelp: {},
  promptTabs: {querySelectorAll: () => []}, skipStatementReview: {checked: false},
  promptDialog: {open: false, showModal() {this.open = true;}, close() {this.open = false;}}};
const show = () => {};
const selectedProblemMode = () => 'statement';
const jobPath = path => path;
let diskPrompts = defaults;
const request = async () => ({workflow: {settings: {prompts: diskPrompts}}});
`;
const checks = `
(async () => {
  syncPrompts();
  assert.equal(promptValues.author, defaults.author);
  await openPromptEditor();
  ui.promptEditor.value = 'Custom review';
  savePrompts();
  assert.deepEqual(promptOverrides, {review: 'Custom review'});

  // Editing YAML with the page still open refreshes unedited roles on reopening.
  diskPrompts = {...defaults, author: 'Latest YAML [STATEMENT]'};
  await openPromptEditor();
  assert.equal(promptDrafts.author, diskPrompts.author);
  assert.equal(promptDrafts.review, 'Custom review');

  // A refresh while the dialog is open must not turn untouched text into an edit.
  diskPrompts = {...diskPrompts, author: 'Even newer YAML [STATEMENT]'};
  await currentPromptDefaults();
  ui.promptEditor.value = 'Another custom review';
  savePrompts();
  assert.deepEqual(promptOverrides, {review: 'Another custom review'});

  // Returning home/new job clears all deliberate edits as well as legacy text.
  syncPrompts();
  assert.deepEqual(promptOverrides, {});
  assert.equal(promptValues.author, diskPrompts.author);
  assert.equal(promptValues.review, defaults.review);

  // Historical jobs still display their actual prompt, rather than rewriting history.
  syncPrompts({...state, runId: 'old-job', authorPrompt: 'Actual saved author [STATEMENT]'});
  assert.equal(promptValues.author, 'Actual saved author [STATEMENT]');
})();
`;
Promise.resolve(vm.runInNewContext(setup + functions + checks, {assert}))
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js")],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
