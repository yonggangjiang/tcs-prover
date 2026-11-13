"""New web jobs use current YAML, never remembered browser or server defaults."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers
from ui import presets, server

# The notebook files of older runs: only the author ever wrote them, never the controller.
RESEARCH_MEMORY_FILES = ("INITIAL_PROMPT.md", "APPROACHES.md", "PROVED.md", "audit.md")


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
            self.assertEqual(latest["workflow"]["settings"]["prompts"][name], server.default_prompts()[name])
            self.assertEqual(app.snapshot()["workflow"]["settings"]["prompts"][name], server.default_prompts()[name])

    def test_new_web_jobs_ignore_old_page_prompts_and_save_current_yaml(self):
        self.edit_yaml("Old page")
        old_page = server.empty_state()
        self.edit_yaml("Current")
        body = {**old_page, "statement": "Exact statement", "content": "A proof"}
        # These legacy entry routes derive their own equivalent stage choices.
        body.pop("workflowStages", None)
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
                for name in RESEARCH_MEMORY_FILES:
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
        # The actual prompt editor functions, without a model or browser, with a server that lists the role prompts
        # only (no catalog: an older one); the catalog's own checks are in tests/test_prompt_catalog_ui.py.
        from tests import test_prompt_catalog_ui as dialog

        source = (server.UI / "app.js").read_text(encoding="utf-8")
        self.assertIn('try { localStorage.removeItem("tcs-prover-role-prompts"); } catch (_) {}', source)
        self.assertNotIn("localStorage.setItem(\"tcs-prover-role-prompts", source)
        roles = {"groups": ["Statement review", "Author", "Coordinator", "Critic", "Writer"], "prompts": [
            dialog.entry("review", "Statement review", "Reviewer prompt", [], "Review"),
            dialog.entry("author", "Author", "Author prompt", ["[STATEMENT]"], "YAML author [STATEMENT]"),
            dialog.entry("critic", "Critic", "Critic prompt", ["{count}"], "Critic"),
            dialog.entry("convention", "Critic", "Convention critic prompt", ["{statement}"],
                         "Convention critic: {statement}"),
            dialog.entry("final", "Writer", "Final writer prompt", [], "Final")]}
        dialog.PromptCatalogBrowserTests.run_app(self, r"""
server.catalog = false;
state.authorPrompt = "Old state [STATEMENT]";
syncPrompts();
assert.equal(promptValues.author, "YAML author [STATEMENT]");
await open();
type("Custom review");
await apply();
assert.deepEqual(promptOverrides, {review: "Custom review"});

// Editing YAML with the page still open refreshes unedited roles on reopening.
catalog.prompts.find((item) => item.name === "author").text = "Latest YAML [STATEMENT]";
await open();
assert.equal(promptDrafts.author, "Latest YAML [STATEMENT]");
assert.equal(promptDrafts.review, "Custom review");

// A refresh while the dialog is open must not turn untouched text into an edit.
catalog.prompts.find((item) => item.name === "author").text = "Even newer YAML [STATEMENT]";
await currentPromptDefaults();
selectPrompt("review");
type("Another custom review");
await apply();
assert.deepEqual(promptOverrides, {review: "Another custom review"});

// Returning home/new job clears all deliberate edits as well as legacy text.
syncPrompts();
assert.deepEqual(promptOverrides, {});
assert.equal(promptValues.author, "Even newer YAML [STATEMENT]");
assert.equal(promptValues.review, "Review");

// Historical jobs still display their actual prompt, rather than rewriting history.
syncPrompts({...state, runId: "old-job", authorPrompt: "Actual saved author [STATEMENT]"});
assert.equal(promptValues.author, "Actual saved author [STATEMENT]");

// Moving between roles preserves the deliberate author edit.
syncPrompts();
await open();
selectPrompt("author");
type("Custom author [STATEMENT]");
selectPrompt("review");
selectPrompt("author");
assert.equal(ui.promptEditor.value, "Custom author [STATEMENT]");
await apply();
assert.deepEqual(promptOverrides, {author: "Custom author [STATEMENT]"});

// An invalid Apply keeps the modal dialog open and explains why inside it, on the prompt to fix; the page
// notice behind the dialog is never used.
ui.notice.textContent = "";
await open();
assert.equal(ui.promptError.hidden, true);
selectPrompt("author");
type("No marker here");
selectPrompt("review");
await apply();
assert.deepEqual([ui.promptDialog.open, ui.promptError.hidden, ui.promptError.textContent, activePrompt],
  [true, false, "The author prompt must contain exactly one [STATEMENT].", "author"]);
assert.equal(ui.promptEditor.focused, true);
assert.equal(ui.notice.textContent, "");
// Editing clears it; an empty prompt elsewhere is shown on that prompt.
type("Fixed [STATEMENT]");
assert.equal(ui.promptError.hidden, true);
selectPrompt("critic");
type("   ");
selectPrompt("review");
await apply();
assert.deepEqual([activePrompt, ui.promptError.textContent, ui.promptDialog.open],
  ["critic", "Every role prompt must contain instructions.", true]);
type("Critic");
await apply();
assert.equal(ui.promptDialog.open, false);
assert.equal(ui.notice.textContent, "");

// The dialog opens on the first stage that is on.
stages = {review: false, author: false, critic: false, writing: true};
await open();
assert.equal(activePrompt, "final");
""", catalog=roles)


class SavedSettingsPromptTests(unittest.TestCase):
    """Saved New problem settings never hold prompts: every new job starts from the workflow files."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = patch.dict(os.environ, {providers.STORE_ENV: str(Path(folder.name) / "api-models.json")})
        store.start()
        self.addCleanup(store.stop)

    def test_saving_with_edited_prompts_stores_none(self):
        edited = "Deliberate per-job instructions. [STATEMENT]"
        presets.save("Edited prompts", {**presets.builtin_settings(), "promptOverrides": {"author": edited},
                                        "authorPrompt": edited, "criticPrompt": edited, "finalPrompt": edited},
                     make_default=True)
        text = presets.path().read_text(encoding="utf-8")
        self.assertNotIn("Deliberate", text)
        self.assertNotIn("Prompt", text)
        [saved] = presets.listing()["presets"]
        self.assertEqual(set(saved["settings"]), set(presets.SETTINGS))
        # A new problem opened with these settings as the default still offers the workflow files' prompts.
        self.assertEqual(server.empty_state()["authorPrompt"], server.default_prompts()["author"])


if __name__ == "__main__":
    unittest.main()
