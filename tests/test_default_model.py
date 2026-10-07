"""GPT-6.1 Sol at Max is the default, and every option matches Codex; no model calls."""

import contextlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import workflow_runner as runtime
from ui import audits, cli, review, server

MODEL, EFFORT = "gpt-6.1-sol", "max"
ROLES = ("review", "author", "critic", "writer")
INDEX = server.UI / "index.html"
README = Path(__file__).resolve().parents[1] / "README.md"


class Launched(Exception):
    """Raised by a fake Popen after it has recorded the command line."""


def captured_command(launch):
    """Return the argv a code path would give to subprocess.Popen."""
    commands = []

    def popen(command, **kwargs):
        commands.append(command)
        raise Launched

    with patch.object(subprocess, "Popen", side_effect=popen):
        try:
            launch()
        except Launched:
            pass
    if not commands:
        raise AssertionError("No process was launched.")
    return commands[0]


def option_after(command, flag):
    return command[command.index(flag) + 1]


class RuntimeDefaultTests(unittest.TestCase):
    def test_every_role_defaults_to_6_1_sol_at_max(self):
        self.assertEqual((runtime.MODEL, runtime.EFFORT), (MODEL, EFFORT))
        self.assertEqual(
            {runtime.AUTHOR_MODEL, runtime.CRITIC_MODEL, runtime.WRITER_MODEL, runtime.REVIEW_MODEL},
            {MODEL},
        )
        self.assertEqual(runtime.REVIEW_EFFORT, EFFORT)
        for role in ("author", "critic", "writer"):
            with self.subTest(role=role):
                settings = runtime._settings({"role": role}, {})
                self.assertEqual((settings["model"], settings["effort"]), (MODEL, EFFORT))

    def test_explicit_choices_still_override_the_default(self):
        settings = runtime._settings(
            {"role": "author"}, {"author_model": "gpt-6-astra", "author_effort": "ultra"},
        )
        self.assertEqual((settings["model"], settings["effort"]), ("gpt-6-astra", "ultra"))

    def test_current_codex_models_are_offered_and_max_reaches_codex_unchanged(self):
        self.assertEqual(
            runtime.OFFERED_MODELS,
            ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", runtime.DEEPSEEK_MODEL),
        )
        self.assertEqual(runtime.chosen_model(MODEL), MODEL)
        self.assertEqual(runtime.effective_effort(MODEL, "max"), "max")
        self.assertEqual(runtime.effective_speed(MODEL, "fast"), "fast")
        self.assertEqual(runtime.model_provider(MODEL), runtime.OPENAI_PROVIDER)
        runtime.require_model_credentials(MODEL)

    def test_older_models_are_accepted_for_saved_runs_but_no_longer_offered(self):
        self.assertEqual(runtime.LEGACY_MODELS, ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"))
        self.assertEqual(runtime.MODELS, runtime.OFFERED_MODELS + runtime.LEGACY_MODELS)
        for model in runtime.LEGACY_MODELS:
            with self.subTest(model=model):
                self.assertNotIn(model, runtime.OFFERED_MODELS)
                self.assertEqual(runtime.chosen_model(model), model)
                options = server.App._workflow_options(
                    review_model=model, author_model=model, critic_model=model, writer_model=model,
                )
                self.assertEqual(options["authorModel"], model)

    def test_unknown_models_are_rejected_with_the_current_names(self):
        with self.assertRaisesRegex(runtime.Error, r"6\.1 Sol, Astra, 6 Sol, 6 Luna, or DeepSeek V4 Pro"):
            runtime.chosen_model("gpt-9")

    def test_effort_table_covers_every_model_and_the_default_is_valid_for_all(self):
        self.assertEqual(set(runtime.MODEL_EFFORTS), set(runtime.MODELS))
        for model, efforts in runtime.MODEL_EFFORTS.items():
            with self.subTest(model=model):
                # Same order as the shared menu, and the default is valid whichever model is picked.
                self.assertEqual(list(efforts), [e for e in runtime.EFFORTS if e in efforts])
                self.assertIn(EFFORT, efforts)
        for model in (MODEL, "gpt-6-astra", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra"):
            self.assertIn("ultra", runtime.MODEL_EFFORTS[model])
        for model in ("gpt-6-luna", "gpt-5.6-luna"):
            self.assertNotIn("ultra", runtime.MODEL_EFFORTS[model])

    def test_structured_requests_launch_codex_with_the_default_model_and_effort(self):
        def launch():
            with patch.object(runtime, "codex", return_value="codex"):
                runtime.run_structured_attempt(
                    "Prompt", {"type": "object"}, "critic",
                    runtime.MODEL, runtime.EFFORT, runtime.DEFAULT_SPEED, "concise",
                )

        command = captured_command(launch)
        self.assertEqual(option_after(command, "-m"), MODEL)
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertIn('service_tier="fast"', command)


class ServerDefaultTests(unittest.TestCase):
    def test_new_job_state_uses_the_default_for_every_role(self):
        state = server.empty_state()
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(state[f"{role}Model"], MODEL)
                self.assertEqual(state[f"{role}Effort"], EFFORT)
        self.assertEqual(state["reasoningEffort"], EFFORT)

    def test_job_options_default_to_the_model_and_accept_it_explicitly(self):
        for options in (
            server.App._workflow_options(),
            server.App._workflow_options(
                review_model=MODEL, author_model=MODEL, critic_model=MODEL, writer_model=MODEL,
                review_effort="max", author_effort="max", critic_effort="max", writer_effort="max",
            ),
        ):
            for role in ROLES:
                with self.subTest(role=role):
                    self.assertEqual(options[f"{role}Model"], MODEL)
                    self.assertEqual(options[f"{role}Effort"], EFFORT)
            self.assertEqual(options["reasoningEffort"], EFFORT)

    def test_invalid_model_errors_name_the_current_choices(self):
        for field in ("review_model", "author_model"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, r"6\.1 Sol, Astra, 6 Sol, 6 Luna"):
                server.App._workflow_options(**{field: "gpt-9"})

    def test_public_settings_describe_the_default_and_keep_audits_opt_in(self):
        settings = server.PUBLIC_GRAPH["settings"]
        self.assertEqual((settings["model"], settings["review_model"]), (MODEL, MODEL))
        self.assertEqual(settings["reasoning_effort"], EFFORT)
        self.assertEqual(settings["models"], list(runtime.OFFERED_MODELS))
        self.assertEqual(settings["review_models"], list(runtime.OFFERED_MODELS))
        self.assertEqual(
            settings["model_efforts"], {m: list(e) for m, e in runtime.MODEL_EFFORTS.items()},
        )
        self.assertEqual(settings["model_summary"], "6.1 Sol/Max review · 6.1 Sol/Max author, critic, writer")
        # Adding the model to the audit menu must not switch paid audits on.
        self.assertEqual(settings["research_audits"]["models"], ["none"] * 3)
        self.assertIn(
            {"value": MODEL, "label": "GPT 6.1 Sol (Max)"}, settings["research_audits"]["choices"],
        )


class SavedRunCompatibilityTests(unittest.TestCase):
    """Pruned menus must never strand a run that was saved with older choices."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)

    def prepared_run(self, name, **settings):
        directory = self.runs / name
        directory.mkdir()
        statement = "Exact prepared statement"
        (directory / "checked-statement.md").write_text(
            "# Statement sent directly to the proof author\n\n" + statement
        )
        (directory / "draft.md").write_text("# Draft problem\n\n" + statement)
        (directory / "INITIAL_PROMPT.md").write_text("Preserved curated instructions")
        options = server.App._workflow_options(file_management=True, include_review=False)
        options.update(preparedRun=True, skipStatementReview=True, **settings)
        (directory / "job-settings.json").write_text(json.dumps(options))

    def saved_choices(self, model, effort):
        return {
            **{f"{role}Model": model for role in ("review", "author", "critic", "writer")},
            **{f"{role}Effort": effort for role in ("review", "author", "critic", "writer")},
            "reasoningEffort": effort,
        }

    def test_astra_at_ultra_restores_exactly(self):
        self.prepared_run("saved", **self.saved_choices("gpt-6-astra", "ultra"))
        app = server.restore_saved_jobs(self.runs)["saved"]
        for role in ("author", "critic", "writer"):
            self.assertEqual(app.state[f"{role}Model"], "gpt-6-astra")
            self.assertEqual(app.state[f"{role}Effort"], "ultra")
        self.assertEqual(app.state["settingsWarning"], "")

    def test_older_models_restore_exactly(self):
        for model in runtime.LEGACY_MODELS:
            self.prepared_run(model, **self.saved_choices(model, "max"))
        jobs = server.restore_saved_jobs(self.runs)
        for model in runtime.LEGACY_MODELS:
            with self.subTest(model=model):
                self.assertEqual(jobs[model].state["authorModel"], model)
                self.assertEqual(jobs[model].state["writerModel"], model)
                self.assertEqual(jobs[model].state["authorEffort"], "max")

    def test_audit_choices_saved_with_older_models_stay_valid(self):
        self.prepared_run("audited", researchAudits={"intervalHours": 2, "models": list(runtime.LEGACY_MODELS)})
        app = server.restore_saved_jobs(self.runs)["audited"]
        self.assertEqual(app.state["researchAudits"]["models"], list(runtime.LEGACY_MODELS))
        self.assertEqual(app.state["settingsWarning"], "")


class CommandLineDefaultTests(unittest.TestCase):
    def run_cli(self, *arguments):
        output = io.StringIO()
        with patch.object(sys, "argv", ["web_ui.py", *arguments]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stop:
                    cli.main()
        return stop.exception.code, " ".join(output.getvalue().split())

    def test_help_lists_only_current_models_and_shows_the_new_defaults(self):
        code, text = self.run_cli("--help")
        self.assertEqual(code, 0)
        offered = ", ".join(runtime.OFFERED_MODELS)
        for role in ("author", "critic", "writer"):
            with self.subTest(role=role):
                self.assertIn(f"{role} model: {offered} (default: {MODEL}); the older GPT-5.6 ids are still accepted", text)
        self.assertIn("fallback reasoning effort for every proof role (default: max)", text)
        self.assertNotIn("gpt-5.6-terra", text)

    def test_older_model_ids_still_parse_and_unknown_ones_do_not(self):
        # An option before --help is validated first, so the exit code shows acceptance.
        self.assertEqual(self.run_cli("--author-model", "gpt-5.6-terra", "--help")[0], 0)
        self.assertEqual(self.run_cli("--author-model", "gpt-9", "--help")[0], 2)

    def test_runner_and_review_worker_keep_accepting_older_ids(self):
        # Saved jobs reach these parsers through subprocesses, so they must not narrow to the offered list.
        def exit_code(entry, *arguments):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stop:
                    entry([*arguments, "--help"])
            return stop.exception.code

        for model in runtime.LEGACY_MODELS:
            with self.subTest(model=model):
                self.assertEqual(exit_code(
                    runtime.main, "--model", model, "--author-model", model,
                    "--critic-model", model, "--writer-model", model,
                ), 0)
                self.assertEqual(exit_code(review.review_worker_main, "--review-model", model), 0)
        self.assertEqual(exit_code(runtime.main, "--model", "gpt-9"), 2)
        self.assertEqual(exit_code(review.review_worker_main, "--review-model", "gpt-9"), 2)

    def test_runner_cli_accepts_the_model_and_effort(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stop:
            runtime.main(["--help"])
        self.assertEqual(stop.exception.code, 0)
        self.assertIn(MODEL, output.getvalue())


class AuditMenuTests(unittest.TestCase):
    def test_audit_menu_runs_codex_with_6_1_sol_at_max(self):
        row = next(row for row in audits.load_config()["models"] if row["value"] == MODEL)
        self.assertEqual((row["provider"], row["model"], row["effort"]), ("codex", MODEL, "max"))

        def launch():
            with patch.object(audits, "resolve_executable", return_value="codex"):
                audits.run_auditor(row, "Prompt", ".", threading.Event())

        command = captured_command(launch)
        self.assertEqual(option_after(command, "-m"), MODEL)
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertEqual(audits.normalize_settings({"models": [MODEL, "none", "none"]})["models"], [MODEL, "none", "none"])

    def test_current_codex_models_lead_and_older_ones_trail_but_stay_valid(self):
        rows = audits.load_config()["models"]
        codex = [row for row in rows if row.get("provider") == "codex"]
        current = [row["model"] for row in codex if "older" not in row["label"]]
        older = [row for row in codex if "older" in row["label"]]
        self.assertEqual(current, [m for m in runtime.OFFERED_MODELS if m != runtime.DEEPSEEK_MODEL])
        self.assertEqual([row["model"] for row in older], list(runtime.LEGACY_MODELS))
        # The older entries sit at the very end, after every other provider.
        self.assertEqual(rows[-len(older):], older)
        for row in codex:
            with self.subTest(model=row["model"]):
                self.assertIn(row["effort"], runtime.MODEL_EFFORTS[row["model"]])
                self.assertEqual(audits.normalize_settings({"models": [row["value"], "none", "none"]})["models"][0], row["value"])


class Menus(HTMLParser):
    """Collect every select's [value, selected, label] options and hidden inputs."""

    def __init__(self):
        super().__init__()
        self.options, self.hidden, self._select, self._option = {}, {}, None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self._select = attrs.get("id")
            self.options[self._select] = []
        elif tag == "option" and self._select:
            self._option = [attrs.get("value"), "selected" in attrs, ""]
            self.options[self._select].append(self._option)
        elif tag == "input" and attrs.get("type") == "hidden":
            self.hidden[attrs.get("id")] = attrs.get("value")

    def handle_data(self, data):
        if self._option is not None:
            self._option[2] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self._option = None
        elif tag == "select":
            self._select = None


def menus():
    page = Menus()
    page.feed(INDEX.read_text(encoding="utf-8"))
    return page


class BrowserDefaultTests(unittest.TestCase):
    def test_role_menus_offer_the_current_models_and_preselect_the_default(self):
        page = menus()
        for role in ROLES:
            with self.subTest(role=role):
                models = page.options[f"{role}Model"]
                self.assertEqual([value for value, *_ in models], list(runtime.OFFERED_MODELS))
                self.assertEqual([value for value, selected, _ in models if selected], [MODEL])
                efforts = page.options[f"{role}Effort"]
                self.assertEqual(sorted(value for value, *_ in efforts), sorted(runtime.EFFORTS))
                self.assertEqual([value for value, selected, _ in efforts if selected], [EFFORT])
        self.assertEqual(page.hidden["reasoningEffort"], EFFORT)

    def test_labels_no_longer_make_stale_claims(self):
        page = menus()
        labels = {value: label for value, _, label in page.options["authorEffort"]}
        # The catalog calls ultra "Maximum reasoning with automatic task delegation".
        self.assertEqual(labels["ultra"], "Ultra — Max with task delegation")
        self.assertNotIn("highest", labels["ultra"])
        speed = {value: label for value, _, label in page.options["speedMode"]}
        self.assertIn("up to 2×", speed["fast"])
        self.assertNotIn("1.5", speed["fast"])
        self.assertEqual(
            [value for value, selected, _ in page.options["speedMode"] if selected], [runtime.DEFAULT_SPEED],
        )

    def test_readme_documents_the_current_models_and_defaults(self):
        text = README.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| `-authorModel MODEL`"))
        self.assertIn(f"| `{MODEL}` |", row)
        for model in runtime.OFFERED_MODELS:
            self.assertIn(f"`{model}`", row)
        self.assertIn("| `-reasoningEffort LEVEL` | `max` |", text)
        self.assertNotIn("1.5× on", text.replace("1.5× on the other models", ""))

    def test_fallbacks_for_a_state_without_settings_match_the_python_defaults(self):
        source = (server.UI / "app.js").read_text(encoding="utf-8")
        models = re.findall(r'state\.(?:review|author|critic|writer)Model \|\| "([^"]+)"', source)
        efforts = re.findall(
            r'state\.(?:review|author|critic|writer)Effort \|\| (?:state\.reasoningEffort \|\| )?"([^"]+)"',
            source,
        )
        self.assertEqual(models, [MODEL] * 8)
        self.assertEqual(efforts, [EFFORT] * 8)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_footer_summary_names_every_model_distinctly(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const app = fs.readFileSync(process.argv[1], 'utf8');
const functions = app.slice(app.indexOf('function updateModelSummary()'), app.indexOf('const promptLabels ='));
const setup = `
const ui = {
  reviewModel: {value: 'gpt-6.1-sol'}, authorModel: {value: 'gpt-6.1-sol'},
  criticModel: {value: 'gpt-6.1-sol'}, writerModel: {value: 'gpt-6.1-sol'},
  reviewEffort: {value: 'max'}, authorEffort: {value: 'max'},
  criticEffort: {value: 'max'}, writerEffort: {value: 'max'},
  speedMode: {value: 'fast'}, reasoningSummary: {value: 'concise'},
  skipStatementReview: {checked: false}, statementReviewOnly: {checked: false},
  modelSummary: {textContent: ''},
};
const selectedProblemMode = () => 'statement';
`;
const checks = `
updateModelSummary();
assert.equal(ui.modelSummary.textContent, 'Fast speed · Concise activity log · '
  + '6.1 Sol/Max review · 6.1 Sol/Max author · 6.1 Sol/Max critic · 6.1 Sol/Max writer');

ui.reviewModel.value = 'gpt-6-luna'; ui.reviewEffort.value = 'high';
ui.authorModel.value = 'gpt-6-sol'; ui.authorEffort.value = 'ultra';
ui.criticModel.value = 'gpt-6-astra';
ui.writerModel.value = 'gpt-5.6-terra';
updateModelSummary();
assert.match(ui.modelSummary.textContent,
  /6 Luna\\/High review · 6 Sol\\/Ultra author · Astra\\/Max critic · 5\\.6 Terra\\/Max writer$/);

ui.writerModel.value = 'deepseek-v4-pro'; ui.writerEffort.value = 'ultra';
updateModelSummary();
assert.match(ui.modelSummary.textContent, /^Fast for ChatGPT · Standard for DeepSeek/);
assert.match(ui.modelSummary.textContent, /DeepSeek V4 Pro\\/Max writer$/);

// Every accepted model gets its own name, never a bare "Sol", "Luna" or "Terra".
const ids = ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol',
  'gpt-5.6-terra', 'gpt-5.6-luna', 'deepseek-v4-pro'];
const names = ids.map((id) => {
  ui.authorModel.value = id; ui.authorEffort.value = 'max';
  updateModelSummary();
  return ui.modelSummary.textContent.match(/([^·]+)\\/Max author/)[1].trim();
});
assert.equal(new Set(names).size, ids.length, names.join(' | '));
assert.ok(!names.some((name) => ['Sol', 'Luna', 'Terra'].includes(name)), names.join(' | '));
`;
vm.runInNewContext(setup + functions + checks, {assert});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js")],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_effort_menus_follow_the_selected_model_and_saved_older_models_stay_visible(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const app = fs.readFileSync(process.argv[1], 'utf8');
const functions = app.slice(app.indexOf('function setChoice('), app.indexOf('async function refresh()'));
const setup = `
class Option {
  constructor(text, value) { this.text = text; this.value = value; this.disabled = false; }
}
const select = (values, value) => ({
  options: values.map((item) => new Option(item, item)), value,
  add(option) { this.options.push(option); },
});
const everyEffort = ['ultra', 'max', 'xhigh', 'high', 'medium', 'low'];
let state = {workflow: {settings: {model_efforts: {
  'gpt-6.1-sol': everyEffort, 'gpt-6-luna': everyEffort.slice(1), 'deepseek-v4-pro': everyEffort,
}}}};
const ui = {
  authorModel: select(['gpt-6.1-sol', 'gpt-6-luna', 'deepseek-v4-pro'], 'gpt-6.1-sol'),
  authorEffort: select(everyEffort, 'ultra'),
};
const disabled = () => ui.authorEffort.options.filter((option) => option.disabled).map((option) => option.value);
`;
const checks = `
// A model that supports Ultra keeps every level.
syncEffortChoices('author');
assert.deepEqual(disabled(), []);
assert.equal(ui.authorEffort.value, 'ultra');

// Luna does not list Ultra: it is disabled and a chosen Ultra moves to Max.
ui.authorModel.value = 'gpt-6-luna';
syncEffortChoices('author');
assert.deepEqual(disabled(), ['ultra']);
assert.equal(ui.authorEffort.value, 'max');

// Switching back re-enables Ultra without silently re-selecting it.
ui.authorModel.value = 'gpt-6.1-sol';
syncEffortChoices('author');
assert.deepEqual(disabled(), []);
assert.equal(ui.authorEffort.value, 'max');

// DeepSeek maps the shared menu itself, and unknown models are never filtered.
ui.authorModel.value = 'deepseek-v4-pro';
syncEffortChoices('author');
assert.deepEqual(disabled(), []);
ui.authorModel.value = 'something-new'; ui.authorEffort.value = 'ultra';
syncEffortChoices('author');
assert.deepEqual(disabled(), []);
assert.equal(ui.authorEffort.value, 'ultra');

// Without published support data nothing changes.
state = {};
ui.authorModel.value = 'gpt-6-luna';
syncEffortChoices('author');
assert.deepEqual(disabled(), []);

// A saved job's older model is kept visible once, not blanked and not duplicated.
setChoice(ui.authorModel, 'gpt-5.6-terra');
assert.equal(ui.authorModel.value, 'gpt-5.6-terra');
assert.equal(ui.authorModel.options.at(-1).text, 'gpt-5.6-terra (older)');
const count = ui.authorModel.options.length;
setChoice(ui.authorModel, 'gpt-5.6-terra');
setChoice(ui.authorModel, 'gpt-6-luna');
assert.equal(ui.authorModel.options.length, count);
assert.equal(ui.authorModel.value, 'gpt-6-luna');

// Elements that expose no options (like the stubs other tests use) are left alone.
const bare = {};
setChoice(bare, 'gpt-6.1-sol');
assert.equal(bare.value, 'gpt-6.1-sol');
ui.authorEffort = {};
syncEffortChoices('author');
`;
vm.runInNewContext(setup + functions + checks, {assert});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js")],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_render_and_the_model_menus_apply_the_model_support_rules(self):
        # Drives the real render() and the real model-menu handlers against fake selects built from index.html.
        page = menus()
        fixture = {
            "selects": {
                menu: [{"value": value, "selected": selected, "text": label.strip()} for value, selected, label in rows]
                for menu, rows in page.options.items()
            },
            "empty": server.empty_state(),
        }
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const app = fs.readFileSync(process.argv[1], 'utf8');

class Option {
  constructor(text, value) { this.text = text; this.value = value === undefined ? String(text) : value; this.disabled = false; }
}
// Like a real <select>, assigning a value it has no option for blanks it.
function select(rows) {
  const element = {
    options: rows.map((row) => new Option(row.text, row.value)), current: '',
    dataset: {}, classList: {toggle() {}}, setAttribute() {}, focus() {},
    add(option) { this.options.push(option); },
    get value() { return this.current; },
    set value(value) { this.current = this.options.some((option) => option.value === value) ? value : ''; },
  };
  const chosen = rows.find((row) => row.selected);
  element.current = chosen ? chosen.value : (rows.length ? rows[0].value : '');
  return element;
}
const stub = () => ({classList: {toggle() {}}, setAttribute() {}, focus() {}, replaceChildren() {},
  dataset: {}, textContent: '', value: '', checked: false, hidden: false});

function load() {
  const elements = Object.fromEntries(Object.entries(fixture.selects).map(([id, rows]) => [id, select(rows)]));
  elements.problemModes = [{value: 'statement', checked: true}, {value: 'latex', checked: false}];
  const ui = new Proxy(elements, {get(target, name) { return target[name] ??= stub(); }});
  const slice = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
  const code = slice('function selectedProblemMode(', 'const promptLabels =')
    + slice('function render(next)', 'async function refresh()');
  const handlers = slice('for (const role of ["review", "author", "critic", "writer"]) {', 'ui.speedMode.onchange');
  const setup = `
    let state = {workflow: null, trace: []}, previousPhase = '', currentJob = '', reviewPending = false, timer, clock;
    const document = {activeElement: null};
    const show = (element, visible) => element.hidden = !visible;
    const retainTrace = (entries) => entries;
    const ingest = () => {}, syncMemoryPanel = () => {}, renderWorkflow = () => {}, renderClock = () => {};
    const renderResearchAudits = () => {}, fillResearchAuditControls = () => {}, appendFormattedText = () => {};
    const syncPrompts = () => {}, checkEdited = () => {}, refresh = () => {};
    const setTimeout = () => 1, clearTimeout = () => {}, setInterval = () => 1, clearInterval = () => {};
  `;
  const context = {ui, Option, assert};
  vm.createContext(context);
  vm.runInContext(setup + code + ';' + handlers
    + ';this.api = {render, setPending: (value) => reviewPending = value};', context);
  return {ui, api: context.api};
}

const roles = ['review', 'author', 'critic', 'writer'];
const disabled = (ui, role) => ui[`${role}Effort`].options.filter((option) => option.disabled).map((option) => option.value);
const summary = (ui) => ui.modelSummary.textContent;
const saved = {...fixture.empty, phase: 'reviewed', runId: 'run-1', review: {statement: 'S', notes: 'n'}};

// 1. A new job shows the defaults, offers five models and disables nothing.
{
  const {ui, api} = load();
  api.render({...fixture.empty, phase: 'input'});
  for (const role of roles) {
    assert.equal(ui[`${role}Model`].value, 'gpt-6.1-sol');
    assert.equal(ui[`${role}Model`].options.length, 5);
    assert.equal(ui[`${role}Effort`].value, 'max');
    assert.deepEqual(disabled(ui, role), []);
  }
  assert.match(summary(ui), /^Fast speed · Concise activity log · 6\.1 Sol\/Max review/);

  // 2. Picking 6 Luna while Ultra is selected disables Ultra, moves the effort to Max, and updates the footer.
  ui.authorEffort.value = 'ultra';
  ui.authorEffort.onchange();
  assert.match(summary(ui), /6\.1 Sol\/Ultra author/);
  ui.authorModel.value = 'gpt-6-luna';
  ui.authorModel.onchange();
  assert.deepEqual(disabled(ui, 'author'), ['ultra']);
  assert.equal(ui.authorEffort.value, 'max');
  assert.match(summary(ui), /6 Luna\/Max author/);
  ui.authorModel.value = 'gpt-6-sol';
  ui.authorModel.onchange();
  assert.deepEqual(disabled(ui, 'author'), []);
}

// 3. A saved job keeps its older model visible, and Luna's unsupported Ultra is corrected.
{
  const {ui, api} = load();
  const job = {...saved, reviewModel: 'gpt-5.6-terra', reviewEffort: 'ultra', authorModel: 'gpt-6-luna',
    authorEffort: 'ultra', criticModel: 'gpt-5.6-terra', criticEffort: 'ultra', writerModel: 'gpt-6.1-sol', writerEffort: 'high'};
  api.setPending(true);
  api.render(job);
  for (const role of roles) {
    assert.notEqual(ui[`${role}Model`].value, '');
    assert.notEqual(ui[`${role}Effort`].value, '');
  }
  assert.equal(ui.reviewModel.value, 'gpt-5.6-terra');
  assert.equal(ui.reviewModel.options.filter((option) => /older/.test(option.text)).length, 1);
  assert.equal(ui.authorEffort.value, 'max');
  assert.equal(ui.reviewEffort.value, 'ultra');
  assert.equal(ui.writerEffort.value, 'high');
  assert.match(summary(ui),
    /5\.6 Terra\/Ultra review · 6 Luna\/Max author · 5\.6 Terra\/Ultra critic · 6\.1 Sol\/High writer$/);

  // Rendering the same job again must not add a second stand-in option.
  api.setPending(true);
  api.render(job);
  assert.equal(ui.reviewModel.options.length, 6);
}

// 4. A state that records no model or effort falls back to the defaults.
{
  const {ui, api} = load();
  const bare = {...saved};
  for (const key of Object.keys(bare)) if (/(Model|Effort)$/.test(key)) delete bare[key];
  api.setPending(true);
  api.render(bare);
  for (const role of roles) {
    assert.equal(ui[`${role}Model`].value, 'gpt-6.1-sol');
    assert.equal(ui[`${role}Effort`].value, 'max');
  }
}

// 5. Every role keeps an older model chosen in a saved job, in both form branches.
for (const role of roles) {
  for (const phase of ['input', 'reviewed']) {
    const {ui, api} = load();
    api.setPending(true);
    api.render({...saved, phase, [`${role}Model`]: 'gpt-5.6-terra'});
    assert.equal(ui[`${role}Model`].value, 'gpt-5.6-terra', `${role} ${phase}`);
    assert.equal(ui[`${role}Model`].options.at(-1).text, 'gpt-5.6-terra (older)', `${role} ${phase}`);
  }
}
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(server.UI / "app.js")],
            input=json.dumps(fixture), text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
