"""The number of independent critics per critic round (the subagents the critic starts): the job
setting, its form field, the runner options of every launch, resume and continuation, and the round
count. Offline: the runner is never started; tests feed the records it would print (one critic round:
its proof handoff, the critic's request, the progress of its subagents, then its result).
"""

import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import test_provider_ui as browser
from ui import batches, cli, server

STATEMENT = "Every finite task has property P."


def critic_round(visit, count=3):
    """The runner's records of one visit of the critic step (one critic round)."""

    return [
        {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic", "visit": visit},
        {"kind": "proof_handoff", "stage": "critic", "label": "Submitted proof captured", "round": visit,
         "manifest": {"schemaVersion": 1, "visit": visit}},
        {"kind": "request", "stage": "critic", "label": "Critic coordinator", "attempt": 1,
         "text": f"Spawn exactly {count} fresh independent subagent(s)", "candidateText": "Proof."},
        {"kind": "status", "stage": "critic", "label": f"{count} independent critics requested",
         "subagentProgress": True, "auditCount": count, "attempt": 1},
        *({"kind": "status", "stage": "critic", "label": f"Independent critic {index} started",
           "subagentProgress": True, "auditCount": count, "auditIndex": index} for index in range(1, count + 1)),
        *({"kind": "status", "stage": "critic", "label": f"Independent critic {index} finished",
           "subagentProgress": True, "auditCount": count, "auditIndex": index} for index in range(1, count + 1)),
        {"kind": "critic_result", "stage": "critic", "label": f"Critic round {visit}", "round": visit,
         "outcome": "fixed", "report": {"verdict": "pass", "bugs": "", "fixes": "Expanded step 2.",
                                        "solution": "Edited proof."}},
    ]


class CriticCountServerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        self.launches = []
        for change in (mock.patch.object(server.runtime, "verify_model_credentials"),
                       mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(server.subprocess, "Popen", side_effect=self.popen)):
            change.start()
            self.addCleanup(change.stop)

    def popen(self, argv, **kwargs):
        self.launches.append((argv, kwargs["cwd"]))
        return mock.Mock(stdin=io.StringIO())

    def runner_options(self):
        """The latest launch's critic count; no launch names a checkpoint of the critics' reports any more."""

        argv, _ = self.launches[-1]
        self.assertFalse([item for item in argv if item.startswith("critic_audit_file=")], argv)
        return argv[argv.index("--critic-count") + 1]

    def manager(self):
        manager = object.__new__(server.Server)
        manager.runs = self.runs
        manager.jobs = {}
        manager.jobs_lock = threading.RLock()
        manager.fixed_app = False
        return manager

    def direct_job(self, count):
        app = server.App(runs=self.runs, load_latest=False)
        app.start_direct_statement(STATEMENT, critic_count=count)
        return app

    @staticmethod
    def settle(app):
        """The runner ended: the job no longer has a process or reader."""

        app.process = app.active_token = app.worker_token = None

    def test_the_setting_has_its_range_and_default(self):
        self.assertEqual(server.PUBLIC_GRAPH["settings"]["critic_count"], {"default": 3, "minimum": 1, "maximum": 10})
        self.assertEqual(server.empty_state()["criticCount"], 3)
        self.assertEqual(server.App(runs=self.runs, load_latest=False).snapshot()["workflow"]["settings"]
                         ["critic_count"]["maximum"], 10)

    def test_the_count_is_validated_with_the_critic_stage_and_ignored_without_it(self):
        for value, message in ((0, "Choose 1 to 10 independent critics"), (11, "Choose 1 to 10 independent critics"),
                               (-3, "Choose 1 to 10"), (2.5, "must be an integer"), ("3.5", "must be an integer"),
                               ("three", "must be an integer"), (True, "must be an integer"),
                               (None, "must be an integer"), (float("inf"), "must be an integer")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    server.App._workflow_options(critic_count=value, include_review=False)
        for value, expected in ((1, 1), ("7", 7), (10.0, 10)):
            with self.subTest(value=value):
                options = server.App._workflow_options(critic_count=value, include_review=False)
                self.assertIs(type(options["criticCount"]), int)
                self.assertEqual(options["criticCount"], expected)
        without_critic = server.App._workflow_options(
            critic_count=0, workflow_stages={"review": False, "author": True, "critic": False, "writing": True})
        self.assertEqual(without_critic["criticCount"], server.DEFAULT_CRITIC_COUNT)
        with self.assertRaisesRegex(ValueError, "independent critics"):
            server.Server.start_direct_job(self.manager(), {"statement": STATEMENT, "criticCount": 12})
        self.assertEqual(self.launches, [])

    def test_a_new_job_saves_its_count_and_passes_it_to_its_runner(self):
        app = self.direct_job(4)
        self.assertEqual(self.runner_options(), "4")
        self.assertFalse((app.run_dir / "critic-audits.json").exists())
        self.assertEqual(app.snapshot()["criticCount"], 4)
        for path in (server.controller_directory(app.run_dir), app.run_dir):
            self.assertEqual(json.loads((path / server.JOB_SETTINGS_FILENAME).read_text())["criticCount"], 4)

    def test_the_count_survives_a_restart_and_a_reviewed_statement_keeps_it(self):
        app = self.direct_job(7)
        self.settle(app)
        app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["criticCount"], 7)
        self.assertEqual(restored.state["settingsWarning"], "")
        self.assertTrue(restored.state["criticCountRecorded"])

        review = server.App(runs=self.runs, load_latest=False)
        review.start_review(STATEMENT, critic_count=2)
        self.assertEqual((review.state["phase"], review.state["criticCount"]), ("reviewing", 2))
        review.state.update(phase="input", error="The reviewer stopped.")
        review.worker_token = None
        with mock.patch.object(server.App, "start_review") as again:
            review.retry_review()
        self.assertEqual(again.call_args.kwargs["critic_count"], 2)

    def test_an_older_job_is_not_credited_with_a_count_it_never_recorded(self):
        app = self.direct_job(3)
        self.settle(app)
        app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        for path in (server.controller_directory(app.run_dir), app.run_dir):
            settings = json.loads((path / server.JOB_SETTINGS_FILENAME).read_text())
            del settings["criticCount"]
            (path / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(settings))
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        # Its run settings name no independent critics; a resume runs (and records) the default.
        self.assertEqual((restored.state["criticCount"], restored.state["criticCountRecorded"]),
                         (server.DEFAULT_CRITIC_COUNT, False))
        self.assertEqual(restored.state["phase"], "paused")
        restored.resume()
        self.assertEqual(self.runner_options(), str(server.DEFAULT_CRITIC_COUNT))
        self.assertTrue(restored.state["criticCountRecorded"])

    def test_a_job_from_before_the_author_subagents_setting_is_described_as_it_ran(self):
        app = self.direct_job(3)
        self.settle(app)
        self.assertTrue(app.state["authorSubagentsRecorded"])
        app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        self.assertTrue(server.restore_saved_app(server.App(app.trace_file, self.runs)).state["authorSubagentsRecorded"])
        for path in (server.controller_directory(app.run_dir), app.run_dir):
            settings = json.loads((path / server.JOB_SETTINGS_FILENAME).read_text())
            del settings["authorSubagents"]
            (path / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(settings))
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertFalse(restored.state["authorSubagentsRecorded"])
        # A resume runs with (and records) the current settings.
        restored.resume()
        self.assertTrue(restored.state["authorSubagentsRecorded"])
        # A saved batch says the same.
        root = self.runs / "batches"
        for name, settings in (("with", {"authorSubagents": 2}), ("before", {})):
            directory = root / name / "job"
            directory.mkdir(parents=True)
            (directory / "draft.md").write_text("# Draft problem\n\nA finite theorem.\n")
            (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(settings))
        catalog = batches.BatchCatalog(root, current_root=root / "before")
        self.assertEqual([catalog.state(name, "job")["authorSubagentsRecorded"] for name in ("with", "before")],
                         [True, False])

    def test_resuming_a_paused_critic_step_passes_the_same_count(self):
        app = self.direct_job(6)
        # The critic step paused: for example a coordinator call stopped for safety.
        token = app.active_token = app.worker_token = object()
        process = app.process = mock.Mock(stdout=io.StringIO(json.dumps({
            "kind": "workflow_paused", "stage": "critic", "node": "critic", "safetyStop": True,
            "state": {"statement": STATEMENT, "solution": "Proof.", "paused": True},
            "reason": "Stopped for safety: refused."}) + "\n"))
        process.wait.return_value = process.poll.return_value = 1
        app._read_output(process, token)
        self.assertEqual(app.state["phase"], "paused")
        app.resume()
        argv, cwd = self.launches[-1]
        self.assertEqual((argv[argv.index("--start-node") + 1], cwd), ("critic", app.run_dir))
        # The resumed critic starts its independent critics afresh.
        self.assertEqual(self.runner_options(), "6")

    def test_continuations_in_a_new_run_keep_the_count_and_start_fresh_critics(self):
        manager = self.manager()
        source = self.direct_job(5)
        self.settle(source)
        source._save(server.runtime.SAVED_CANDIDATE_FILENAME, "Saved proof.\n")
        source.state.update(phase="done", stage="critic", activeNode="critic", stoppedStage="critic",
                            manuallyStopped=True, error="Stopped by the user.")
        manager.jobs[source.state["runId"]] = source
        for continue_job in (lambda: manager.resume_critic_job(source.state["runId"]),
                             lambda: manager.resume_checkpoint_job(source.state["runId"], "candidate"),
                             lambda: manager.continue_stopped_job(source.state["runId"])):
            with self.subTest(len(self.launches)):
                continued = continue_job()
                self.assertIsNot(continued, source)
                argv, cwd = self.launches[-1]
                self.assertEqual((argv[argv.index("--start-node") + 1], cwd), ("critic", continued.run_dir))
                self.assertEqual(self.runner_options(), "5")
                self.assertEqual(continued.state["criticCount"], 5)
                self.settle(continued)
                continued.state["phase"] = "done"

    def test_the_round_is_the_critic_visit_while_running_and_after_a_restart(self):
        app = self.direct_job(3)
        rounds = []

        class Output:
            def __iter__(self):
                for record in (*critic_round(1), *critic_round(2, count=4)):
                    yield json.dumps(record) + "\n"
                    rounds.append(app.state["round"])

        token = app.active_token = app.worker_token = object()
        process = app.process = mock.Mock(stdout=Output())
        process.wait.return_value = process.poll.return_value = 0
        app.state.update(phase="running", stage="critic", activeNode="critic")
        app._read_output(process, token)
        visit = len(critic_round(1))
        # The badge's round is the visit: the critics and the coordinator are not rounds.
        self.assertEqual(rounds[1:visit], [1] * (visit - 1))
        self.assertEqual(rounds[visit + 1:], [2] * (len(critic_round(2, count=4)) - 1))
        self.settle(app)
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["round"], 2)

    def test_a_saved_batch_view_shows_the_saved_count(self):
        root = self.runs / "batches"
        for name, settings in (("search-v2", {"criticCount": 7}), ("search-v4", {})):
            directory = root / name / "job"
            directory.mkdir(parents=True)
            (directory / "draft.md").write_text("# Draft problem\n\nA finite theorem.\n")
            (directory / "checked-statement.md").write_text(
                "# Statement sent directly to the proof author\n\nA finite theorem.\n")
            (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(settings))
        catalog = batches.BatchCatalog(root, current_root=root / "search-v4")
        self.assertEqual(catalog.state("search-v2", "job")["criticCount"], 7)
        self.assertEqual(catalog.state("search-v4", "job")["criticCount"], server.DEFAULT_CRITIC_COUNT)
        # Only the saved count is named in its run settings.
        self.assertEqual([catalog.state(name, "job")["criticCountRecorded"] for name in ("search-v2", "search-v4")],
                         [True, False])


class CriticCountCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.statement = self.root / "statement.md"
        self.statement.write_text(STATEMENT, encoding="utf-8")

    def main(self, *arguments):
        """Run the CLI's main() without starting anything; return what it would have started."""

        manager = mock.Mock()
        manager.start_saved_critic_job.return_value.state = {"runId": "continued"}
        manager.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(cli.sys, "argv", ["web_ui.py", *map(str, arguments)]), \
                mock.patch.object(cli, "run_headless_markdown", return_value=0) as headless, \
                mock.patch.object(cli, "saved_critic_source", return_value={"run_dir": self.root}), \
                mock.patch.object(cli, "Server", return_value=manager), \
                mock.patch.object(cli.runtime, "configure_standard_streams"), \
                mock.patch.object(cli.ownership, "ROLE", cli.ownership.ROLE), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        return headless, manager

    def test_every_spelling_sets_the_count_of_a_terminal_run_and_a_critic_resume(self):
        for spelling in ("-criticCount", "--criticCount", "--critic-count"):
            with self.subTest(spelling=spelling):
                headless, _ = self.main(self.statement, spelling, 5)
                self.assertEqual(headless.call_args.kwargs["critic_count"], 5)
                _, manager = self.main("--resume-critic", self.root, "--no-browser", spelling, 4)
                self.assertEqual(manager.start_saved_critic_job.call_args.args[1]["criticCount"], 4)
        headless, _ = self.main(self.statement)
        self.assertEqual(headless.call_args.kwargs["critic_count"], server.DEFAULT_CRITIC_COUNT)

    def test_a_terminal_run_validates_the_count_and_passes_it_to_its_runner(self):
        with self.assertRaisesRegex(ValueError, "Choose 1 to 10 independent critics"):
            cli.direct_cli_options(critic_count=0)
        self.assertEqual(cli.direct_cli_options(critic_count=8)["critic_count"], 8)
        launches = []

        def popen(argv, **kwargs):
            launches.append(argv)
            return mock.Mock(stdin=io.StringIO())

        def finish(app, target, args, token):
            app.process = app.active_token = app.worker_token = None
            app.state.update(phase="done", error="", resultKind="final")

        with mock.patch.object(server.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(server.App, "_spawn_worker", finish), \
                mock.patch.object(server.runtime, "verify_model_credentials"), \
                mock.patch.object(cli, "AUTO_LAUNCH_SPACING_SECONDS", 0):
            code = cli.run_headless_markdown(self.statement, runs=self.root / "runs", output_stream=io.StringIO(),
                                             error_stream=io.StringIO(), critic_count=8)
        self.assertEqual(code, 0)
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][launches[0].index("--critic-count") + 1], "8")
        self.assertFalse(any(item.startswith("critic_audit_file=") for item in launches[0]))


class CriticCountMarkupTests(unittest.TestCase):
    def test_the_field_sits_beside_the_critic_round_limit(self):
        parsed = browser.page()
        markup = browser.INDEX.read_text(encoding="utf-8")
        field = parsed.elements["criticCount"]
        self.assertEqual({key: field.get(key) for key in ("type", "min", "max", "step", "value")},
                         {"type": "number", "min": "1", "max": "10", "step": "1", "value": "3"})
        # The page's fallbacks are the server's default.
        fallbacks = re.findall(r"(?:state|source)\.criticCount \|\| (\d+)", browser.APP.read_text(encoding="utf-8"))
        self.assertEqual(fallbacks, [str(server.DEFAULT_CRITIC_COUNT)] * 2)
        self.assertEqual(field["value"], str(server.DEFAULT_CRITIC_COUNT))
        self.assertEqual(field["aria-describedby"], "criticCountHelp")
        self.assertIn('<label for="criticCount">Independent critics</label>', markup)
        help_text = " ".join(markup[markup.index('id="criticCountHelp"'):].split("</p>")[0].split())
        self.assertTrue(help_text.endswith("How many independent critics the critic starts as its subagents on "
                                           "each proof version (1-10); it weighs their reports and makes the "
                                           "decision."), help_text)
        limits = markup.index("<legend>Limits and logging</legend>")
        rounds, count = markup.index('id="criticRoundSetting"'), markup.index('id="criticCountSetting"')
        self.assertLess(limits, rounds)
        self.assertLess(rounds, count)
        self.assertLess(count, markup.index('id="thinkingHoursSetting"'))


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class CriticCountBrowserTests(unittest.TestCase):
    run_app = browser.ProviderBrowserTests.run_app

    def test_every_job_request_sends_the_count(self):
        self.run_app([["function proofJobSettings()", "// An empty or malformed number field"]], r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
let promptOverrides = {};
const selectedWorkflowStages = () => ({review: false, author: true, critic: true, writing: true});
const selectedRetrievalSettings = () => ({retrievalMode: 'live', retrievalCutoffUtc: ''});
""", r"""
ui.criticCount.value = '5'; ui.criticRounds.value = '2'; ui.thinkingHours.value = '0';
assert.equal(proofJobSettings().criticCount, 5);
assert.equal(proofJobSettings().criticRounds, 2);
""")

    def test_the_activity_log_names_the_coordinator_not_the_author(self):
        self.run_app([["function describe(entry)", "function cardIcon("]], "let state = {};", r"""
const completed = fields => describe({kind: 'codex_event', time: 't', ...fields,
  event: {method: 'item/completed', params: {item: {type: 'agentMessage', id: 'm1', text: 'Report text'}}}});
const streamed = fields => describe({kind: 'codex_event', time: 't', ...fields,
  event: {method: 'item/agentMessage/delta', params: {itemId: 'm1', delta: 'Report'}}});
for (const message of [completed, streamed]) {
  assert.equal(message({stage: 'critic', activityLabel: 'Critic coordinator'}).label, 'Critic coordinator');
  assert.equal(message({stage: 'critic', activityLabel: 'Convention critic'}).label, 'Convention critic');
  // The author's own messages, also those of a record without a stage (older transcripts).
  for (const stage of ['solve', 'repair', 'conversation']) assert.equal(message({stage, node: 'author'}).label, 'Author');
  assert.equal(message({}).label, 'Author');
  // Other calls without a label of their own: an older job's single critic, the reviewer, the editor.
  assert.equal(message({stage: 'critic'}).label, 'Critic');
  assert.equal(message({stage: 'review'}).label, 'Statement reviewer');
  assert.equal(message({stage: 'final'}).label, 'LaTeX editor');
}
""")

    def test_the_prompt_editor_describes_the_critic_and_its_subagents(self):
        self.run_app([["const promptLabels =", "function syncPrompts("]], "", r"""
assert.match(promptHelp.critic, /starts the independent critics as its subagents/);
assert.match(promptHelp.critic, /\{count\} is the number of independent critics/);
assert.match(promptHelp.convention, /once the critic has accepted it, before writing/);
""")


if __name__ == "__main__":
    unittest.main()
