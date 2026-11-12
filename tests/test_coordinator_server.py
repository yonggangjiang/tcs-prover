"""The author coordinator on the server: the recorded author mode and its safety for older jobs, the coordinator
prompt as a per-job prompt, the runner options, the page's live window, and the progress log the job page shows.

Offline: runners are stand-in processes (Popen is patched); no CLI or model starts.
"""

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from ui import batches, cli, server

import test_author_coordinator as coordinator_test
import test_chat as chat_test

STATEMENT = "Every finite task has property P."


def option(argv, name):
    return argv[argv.index(name) + 1]


class ServerCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.runs = self.root / "runs"
        self.launches = []
        for change in (mock.patch.object(server.runtime, "verify_model_credentials"),
                       mock.patch.object(server.App, "_spawn_worker"),
                       mock.patch.object(server.subprocess, "Popen", side_effect=self.popen)):
            change.start()
            self.addCleanup(change.stop)

    def popen(self, argv, **kwargs):
        self.launches.append(argv)
        return mock.Mock(stdin=io.StringIO())

    def job(self, **settings):
        app = server.App(runs=self.runs, load_latest=False)
        app.start_direct_statement(STATEMENT, **settings)
        return app

    def saved(self, app):
        return json.loads((server.controller_directory(app.run_dir) / server.JOB_SETTINGS_FILENAME).read_text())

    def restored(self, app):
        app.process = app.active_token = app.worker_token = None
        app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        return server.restore_saved_app(server.App(app.trace_file, self.runs))


class AuthorModeTests(ServerCase):
    def test_the_defaults_and_the_coordinator_prompt_role(self):
        prompts = server.default_prompts()
        self.assertEqual(prompts["coordinator"], coordinator_test.COORDINATOR_PROMPTS["coordinator"])
        self.assertEqual(server.PROMPT_ROLES["coordinator"], "author")
        self.assertEqual(server.DEFAULT_AUTHOR_SUBAGENTS, 3)
        self.assertEqual(server.PUBLIC_GRAPH["settings"]["author_subagents"]["default"], 3)
        state = server.empty_state()
        self.assertEqual({key: state[key] for key in ("authorSubagents", "authorMode", "authorModeRecorded",
                                                       "coordinatorProgress", "coordinatorPrompt")},
                         {"authorSubagents": 3, "authorMode": "coordinator", "authorModeRecorded": True,
                          "coordinatorProgress": None, "coordinatorPrompt": prompts["coordinator"]})

    def test_a_new_job_records_its_mode_from_its_number(self):
        options = server.App._workflow_options
        for settings, mode in (({}, "coordinator"), ({"author_subagents": 1}, "coordinator"),
                               ({"author_subagents": 0}, "author"),
                               ({"author_subagents": 2, "author_mode": "author"}, "author"),
                               ({"author_subagents": 2, "author_mode": "coordinator"}, "coordinator"),
                               ({"workflow_stages": {"review": False, "author": False, "critic": True,
                                                     "writing": False}}, "author")):
            with self.subTest(settings=settings):
                self.assertEqual(options(include_review=False, **settings)["authorMode"], mode)
        with self.assertRaisesRegex(ValueError, "needs Author subagents 1 or more"):
            options(include_review=False, author_subagents=0, author_mode="coordinator")
        with self.assertRaisesRegex(ValueError, "Choose coordinator or author"):
            options(include_review=False, author_mode="auto")

    def test_the_coordinator_prompt_is_checked_and_kept_only_for_a_coordinator(self):
        options = server.App._workflow_options
        custom = "My team works on [STATEMENT], {author_subagents} at a time."
        self.assertEqual(options(include_review=False, coordinator_prompt=custom)["coordinatorPrompt"], custom)
        for prompt, message in (("No statement.", "exactly one \\[STATEMENT\\]"),
                                ("[STATEMENT] and [STATEMENT]", "exactly one \\[STATEMENT\\]"),
                                ("[STATEMENT] {count}", "Write {author_subagents} where the number of author "
                                                       "subagents goes, and double any other brace: {{ and }}"),
                                ("   ", "Every role prompt must contain instructions")):
            with self.subTest(prompt=prompt), self.assertRaisesRegex(ValueError, message):
                options(include_review=False, coordinator_prompt=prompt)
            # An author that works alone never uses it: the default is kept, and nothing is refused.
            self.assertEqual(options(include_review=False, author_subagents=0, coordinator_prompt=prompt)[
                "coordinatorPrompt"], server.default_prompts()["coordinator"])

    def test_a_job_saves_its_mode_and_coordinator_prompt_and_passes_them_at_every_launch(self):
        custom = "Coordinate [STATEMENT] with {author_subagents} subagents."
        app = self.job(coordinator_prompt=custom)
        argv = self.launches[-1]
        self.assertEqual((option(argv, "--author-subagents"), option(argv, "--author-mode")), ("3", "coordinator"))
        self.assertEqual(Path(option(argv, "--coordinator-prompt-file")).read_text(), custom + "\n")
        for folder in (server.controller_directory(app.run_dir), app.run_dir):
            self.assertEqual(json.loads((folder / server.JOB_SETTINGS_FILENAME).read_text())["authorMode"],
                             "coordinator")
            self.assertEqual((folder / "prompts/coordinator.txt").read_text(), custom + "\n")
        restored = self.restored(app)
        self.assertEqual((restored.state["authorMode"], restored.state["coordinatorPrompt"]), ("coordinator", custom))
        restored.resume()
        self.assertEqual(option(self.launches[-1], "--author-mode"), "coordinator")
        # A paused coordinator's reply turn uses its own summary prompt: the runner learns the mode.
        self.assertEqual(option(restored._conversation_options_locked(), "--author-mode"), "coordinator")
        alone = self.job(author_subagents=0)
        self.assertEqual(option(self.launches[-1], "--author-mode"), "author")
        self.assertEqual(self.saved(alone)["authorMode"], "author")

    def test_an_older_job_stays_a_single_author_when_it_is_resumed_revised_or_continued(self):
        for saved_number in (None, 2):
            with self.subTest(authorSubagents=saved_number):
                app = self.job()
                for folder in (server.controller_directory(app.run_dir), app.run_dir):
                    path = folder / server.JOB_SETTINGS_FILENAME
                    settings = {key: value for key, value in json.loads(path.read_text()).items()
                                if key not in {"authorMode", "authorSubagents"}}
                    if saved_number is not None:
                        settings["authorSubagents"] = saved_number
                    path.write_text(json.dumps(settings))
                restored = self.restored(app)
                # No recorded number counts as 0; a recorded one keeps today's meaning (author plus its subagents).
                self.assertEqual((restored.state["authorMode"], restored.state["authorSubagents"],
                                  restored.state["authorModeRecorded"]), ("author", saved_number or 0, False))
                restored.resume()
                argv = self.launches[-1]
                self.assertEqual((option(argv, "--author-mode"), option(argv, "--author-subagents")),
                                 ("author", str(saved_number or 0)))
                # Its saves record no mode, so every later restart sees the same older job.
                self.assertNotIn("authorMode", self.saved(restored))
                self.assertEqual(server.saved_author_mode(restored.run_dir), "author")
                self.assertEqual(restored._kept_author_mode(None, in_place=True), "author")
                self.assertEqual(option(restored._conversation_options_locked(), "--author-mode"), "author")
                # A continuation of it keeps the single author.
                continued = server.App(runs=self.runs, load_latest=False)
                continued.start_direct_statement(STATEMENT, author_subagents=3, continuation_source=restored.run_dir,
                                                 stopped_stage="solve")
                self.assertEqual(option(self.launches[-1], "--author-mode"), "author")
        # A continuation of a coordinator job takes its mode from its own number.
        source = self.job()
        continued = server.App(runs=self.runs, load_latest=False)
        continued.start_direct_statement(STATEMENT, author_subagents=3, continuation_source=source.run_dir,
                                         stopped_stage="solve")
        self.assertEqual(option(self.launches[-1], "--author-mode"), "coordinator")
        self.assertEqual(server.App(runs=self.runs, load_latest=False)._kept_author_mode(None, source.run_dir), None)

    def test_an_older_job_stays_a_single_author_through_every_revision_in_place(self):
        def finish_review(app, statement, feedback=""):
            checked = {"kind": "review_result", "review": {"statement": "Checked S.", "notes": "None."}}
            runner = mock.Mock(stdin=io.StringIO(), stdout=iter([json.dumps(checked) + "\n"]),
                               poll=mock.Mock(return_value=0), wait=mock.Mock(return_value=0))
            with mock.patch.object(server.subprocess, "Popen", return_value=runner):
                app._review(statement, feedback, token=app.active_token)
            self.assertEqual(app.state["phase"], "reviewed")

        app = server.App(runs=self.runs, load_latest=False)
        app.start_review(STATEMENT, author_subagents=2)
        finish_review(app, STATEMENT)
        # A reviewed job from before the coordinator: no authorMode saved, Author subagents 2.
        for folder in (server.controller_directory(app.run_dir), app.run_dir):
            path = folder / server.JOB_SETTINGS_FILENAME
            path.write_text(json.dumps({key: value for key, value in json.loads(path.read_text()).items()
                                        if key != "authorMode"}))
        app.process = app.active_token = app.worker_token = None
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        manager = object.__new__(server.Server)
        manager.jobs, manager.jobs_lock = {restored.run_dir.name: restored}, threading.RLock()
        for number in (1, 2, 3):
            # Send feedback on the review page: the form filled from the job.
            state = restored.state
            body = {"content": state["review"]["statement"], "feedback": f"Revision {number}.",
                    "workflowStages": state["workflowStages"], "authorSubagents": state["authorSubagents"],
                    **{key: state[key] for key in ("reviewModel", "reviewEffort", "authorModel", "criticModel",
                                                   "writerModel", "authorEffort", "criticEffort", "writerEffort",
                                                   "criticRounds", "criticCount", "thinkingHours", "speedMode",
                                                   "reasoningSummary")}}
            manager.start_workflow(body, restored.run_dir.name)
            with self.subTest(revision=number):
                self.assertEqual((restored.state["authorMode"], restored.state["authorModeRecorded"]),
                                 ("author", False))
                self.assertNotIn("authorMode", self.saved(restored))
            finish_review(restored, body["content"], body["feedback"])
        restored.approve()
        argv = self.launches[-1]
        self.assertEqual((option(argv, "--author-mode"), option(argv, "--author-subagents")), ("author", "2"))
        # A continuation of an older job (a new job that recorded "author" with 2) stays one too when revised.
        self.assertEqual(server.App(runs=self.runs, load_latest=False)._kept_author_mode(None, in_place=True), None)
        restored.state.update(authorModeRecorded=True)
        self.assertEqual(restored._kept_author_mode(None, in_place=True), "author")
        restored.state.update(authorSubagents=0)
        self.assertIsNone(restored._kept_author_mode(None, in_place=True))

    def test_a_continued_coordinator_keeps_its_coordinator_prompt(self):
        custom = "Coordinate [STATEMENT] with {author_subagents} subagents."
        source = self.job(coordinator_prompt=custom)
        continued = server.App(runs=self.runs, load_latest=False)
        continued.start_direct_statement(STATEMENT, continuation_source=source.run_dir, stopped_stage="solve")
        self.assertEqual(continued.state["coordinatorPrompt"], custom)
        with self.assertRaisesRegex(ValueError, "must keep its original coordinator prompt"):
            server.App(runs=self.runs, load_latest=False).start_direct_statement(
                STATEMENT, continuation_source=source.run_dir, stopped_stage="solve",
                coordinator_prompt="Another team on [STATEMENT].")

    def test_saved_settings_sources_and_batches_carry_the_mode_and_the_prompt(self):
        custom = "Coordinate [STATEMENT] with {author_subagents} subagents."
        app = self.job(coordinator_prompt=custom)
        self.assertEqual(server.saved_research_source(app.run_dir)["settings"]["coordinatorPrompt"].strip(), custom)
        (app.run_dir / runtime.SAVED_CANDIDATE_FILENAME).write_text("Candidate")
        self.assertEqual(server.saved_critic_source(app.run_dir)["coordinator_prompt"].strip(), custom)
        state = batches.BatchCatalog(self.runs, current_root=self.root / "other").state(
            batches.ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual((state["authorMode"], state["coordinatorPrompt"].strip(), state["coordinatorProgress"]),
                         ("coordinator", custom, None))
        (app.run_dir / "memory").mkdir()
        (app.run_dir / "memory/progress.md").write_text("- 0 h 40 min: Lemma 1 verified.\n")
        catalog = batches.BatchCatalog(self.runs, current_root=self.root / "other")
        state = catalog.state(batches.ORIGINAL_BATCH, app.run_dir.name)
        # The state names the log's version; its text comes apart, as a live job's does.
        self.assertNotIn("text", state["coordinatorProgress"])
        log, kind, _ = catalog.response("/progress-log", batches.ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual((log["text"], log["version"], kind),
                         ("- 0 h 40 min: Lemma 1 verified.\n", state["coordinatorProgress"]["version"],
                          "application/json"))
        # A saved job from before the coordinator.
        path = server.controller_directory(app.run_dir) / server.JOB_SETTINGS_FILENAME
        settings = {key: value for key, value in json.loads(path.read_text()).items()
                    if key not in {"authorMode", "authorSubagents"}}
        for folder in (server.controller_directory(app.run_dir), app.run_dir):
            (folder / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(settings))
        state = batches.BatchCatalog(self.runs, current_root=self.root / "other").state(
            batches.ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual((state["authorMode"], state["authorSubagents"], state["coordinatorProgress"]),
                         ("author", 0, None))
        self.assertEqual(catalog.response("/progress-log", batches.ORIGINAL_BATCH, app.run_dir.name)[0],
                         {"text": None, "version": None})

    def test_terminal_continuations_keep_the_saved_number_of_author_subagents(self):
        class Captured(BaseException):
            pass

        given = []

        class Recorder:
            """The terminal's server, which only records the settings a continuation gets (no port is bound)."""

            def __init__(self, *args, **kwargs):
                pass

            def start_saved_research_job(self, run_dir, settings):
                given.append(settings)
                raise Captured()

            start_saved_critic_job = start_saved_research_job

        def terminal(*argv):
            with mock.patch.object(cli, "Server", Recorder), mock.patch.object(cli.sys, "argv", ["web_ui.py", *argv]), \
                    mock.patch.object(cli, "raise_open_file_limit"), self.assertRaises(Captured):
                cli.main()
            return given[-1]

        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock = self.runs, {}, threading.RLock()
        older, alone = self.job(), self.job(author_subagents=0)
        # A job from before the Author subagents setting: neither the number nor the mode saved.
        for folder in (server.controller_directory(older.run_dir), older.run_dir):
            path = folder / server.JOB_SETTINGS_FILENAME
            path.write_text(json.dumps({key: value for key, value in json.loads(path.read_text()).items()
                                        if key not in {"authorMode", "authorSubagents"}}))
        for name, source in (("older", older), ("works alone", alone)):
            (source.run_dir / runtime.SAVED_CANDIDATE_FILENAME).write_text("A complete candidate proof.")
            source.process = source.active_token = source.worker_token = None
            with self.subTest(source=name, option="--resume-research"):
                continued = manager.start_saved_research_job(
                    source.run_dir, terminal("--resume-research", str(source.run_dir), "--no-browser"))
                argv = self.launches[-1]
                self.assertEqual((option(argv, "--author-subagents"), option(argv, "--author-mode")), ("0", "author"))
                self.assertEqual((continued.state["authorSubagents"], continued.state["authorMode"]), (0, "author"))
            manager.jobs.clear()  # The continued author, which shares the workspace, is done.
            with self.subTest(source=name, option="--resume-critic"):
                settings = terminal("--resume-critic", str(source.run_dir), "--no-browser")
                self.assertNotIn("authorSubagents", settings)
                critic = manager.start_saved_critic_job(source.run_dir, settings)
                self.assertEqual((critic.state["authorSubagents"], critic.state["authorMode"]), (0, "author"))
        # A number given on the command line is taken.
        self.assertEqual(terminal("--resume-critic", str(alone.run_dir), "--no-browser", "-authorSubagents", "2")[
            "authorSubagents"], 2)
        self.assertEqual(server.saved_research_source(older.run_dir)["settings"]["authorSubagents"], 0)

    def test_the_terminal_takes_the_coordinator_prompt_and_states_the_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "coordinator.txt"
            path.write_text("Coordinate [STATEMENT].", encoding="utf-8")
            options = cli.direct_cli_options(coordinator_prompt_file=str(path))
        self.assertEqual(options["coordinator_prompt"], "Coordinate [STATEMENT].")
        self.assertEqual(options["author_subagents"], 3)
        with self.assertRaisesRegex(ValueError, "exactly one \\[STATEMENT\\]"):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "coordinator.txt"
                path.write_text("Coordinate.", encoding="utf-8")
                cli.direct_cli_options(coordinator_prompt_file=str(path))
        with mock.patch.object(cli.sys, "argv", ["web_ui.py", "--help"]), \
                mock.patch.object(cli.sys, "stdout", io.StringIO()) as output, self.assertRaises(SystemExit):
            cli.main()
        text = " ".join(output.getvalue().split())
        self.assertIn("(default: 3): with 1 or more the proof author is a coordinator", text)
        self.assertIn("-coordinatorPromptFile", text)


class EffectiveModeTests(ServerCase):
    """A coordinator job whose model has no subagent tool: its author works alone, as its runner says."""

    NOTE = {"kind": "status", "stage": "solve", "node": "author", "label": "No subagents with this model",
            "noSubagentTool": True, "subagentCount": 3, "authorModeEffective": "author",
            "modelLabel": "GPT 5.6 Luna (older)",
            "text": "GPT 5.6 Luna (older) gets no subagent tool from Codex, so the author works alone instead of "
                    "coordinating up to 3 author subagents. Choose another model to have them."}
    STARTED = {"kind": "status", "stage": "solve", "node": "author", "label": "Goal started", "threadId": "t",
               "authorModeEffective": "coordinator", "text": "Thread t"}

    def test_the_server_keeps_how_the_runner_says_the_author_works(self):
        app = self.job()
        self.assertIsNone(app.snapshot()["authorModeEffective"])
        app.add_trace(self.NOTE)
        alone = {"mode": "author", "modelLabel": "GPT 5.6 Luna (older)", "reason": self.NOTE["text"]}
        self.assertEqual(app.snapshot()["authorModeEffective"], alone)
        # A reply turn of the paused author then uses the author's own summary prompt.
        self.assertEqual(option(app._conversation_options_locked(), "--author-mode"), "author")
        # Kept in the state only: the job settings keep the recorded mode.
        app._save_job_settings(app.state)
        self.assertEqual(self.saved(app)["authorMode"], "coordinator")
        self.assertNotIn("authorModeEffective", self.saved(app))
        # A restart reads it from the transcript, and so does a saved batch.
        restored = self.restored(app)
        self.assertEqual(restored.state["authorModeEffective"], alone)
        state = batches.BatchCatalog(self.runs, current_root=self.root / "other").state(
            batches.ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual(state["authorModeEffective"], alone)
        # A subagent's record or a reply turn's says nothing about it; a later launch that coordinates does.
        restored.add_trace({**self.STARTED, "root": False})
        restored.add_trace({**self.STARTED, "stage": "conversation"})
        self.assertEqual(restored.state["authorModeEffective"], alone)
        restored.add_trace(self.STARTED)
        self.assertIsNone(restored.state["authorModeEffective"])
        self.assertEqual(option(restored._conversation_options_locked(), "--author-mode"), "coordinator")
        self.assertIsNone(self.restored(restored).state["authorModeEffective"])


class LiveWindowTests(ServerCase):
    """The page's live window: the subagents' own events keep a smaller window of their own."""

    def setUp(self):
        super().setUp()
        self.app = server.App(runs=self.runs, load_latest=False)
        self.app._new_run("Exact proof task")

    def own(self, number):
        return {"kind": "codex_event", "stage": "solve", "root": True, "event": {"method": "item/started", "n": number}}

    def subagent(self, number):
        return {"kind": "codex_event", "stage": "solve", "root": False, "event": {"method": "item/started", "n": number}}

    def test_subagent_events_never_push_the_coordinators_own_records_out(self):
        with mock.patch.object(server, "TRACE_LIMIT", 5), mock.patch.object(server, "SUBAGENT_TRACE_LIMIT", 3):
            for number in range(4):
                self.app.add_trace(self.own(number))
            for number in range(10):
                self.app.add_trace(self.subagent(number))
            trace = self.app.state["trace"]
            self.assertEqual([(record["root"], record["event"]["n"]) for record in trace],
                             [(True, 0), (True, 1), (True, 2), (True, 3), (False, 7), (False, 8), (False, 9)])
            for number in range(4, 8):
                self.app.add_trace(self.own(number))
            self.assertEqual([(record["root"], record["event"]["n"]) for record in self.app.state["trace"]],
                             [(True, 3), (False, 7), (False, 8), (False, 9), (True, 4), (True, 5), (True, 6),
                              (True, 7)])
            # A poll after the 14th record gets exactly the records added since, gaps and all.
            snapshot = self.app.snapshot(after=14)
            self.assertEqual((snapshot["traceFrom"], [record["event"]["n"] for record in snapshot["trace"]]),
                             (14, [4, 5, 6, 7]))
            self.assertEqual(self.app.snapshot(after=self.app.state["traceVersion"])["trace"], [])
            # A poll from before the gap (the subagents' records 5 to 11 left their window) gets the rest only.
            snapshot = self.app.snapshot(after=4)
            self.assertEqual((snapshot["traceFrom"], [(record["root"], record["event"]["n"])
                                                      for record in snapshot["trace"]]),
                             (4, [(False, 7), (False, 8), (False, 9), (True, 4), (True, 5), (True, 6), (True, 7)]))
            # A full copy names the version after which nothing is missing: records 12 to 18 are all there.
            snapshot = self.app.snapshot()
            self.assertEqual(snapshot["traceFrom"], 11)
            self.assertEqual([record["event"]["n"] for record in snapshot["trace"][-7:]], [7, 8, 9, 4, 5, 6, 7])
            self.assertIn("with only the latest 3 of the subagents' own", snapshot["traceNotice"])
            # A restart keeps the same windows, with each record's own number.
            restarted = server.App(self.app.trace_file, self.runs)
            self.assertEqual([(record["root"], record["event"]["n"]) for record in restarted.state["trace"]],
                             [(True, 3), (False, 7), (False, 8), (False, 9), (True, 4), (True, 5), (True, 6),
                              (True, 7)])
            self.assertEqual(restarted.snapshot(after=14)["trace"], restarted.state["trace"][4:])

    def test_the_coordinators_time_checks_and_reports_are_pinned(self):
        records = [{"kind": "status", "stage": "solve", "node": "author", "label": "Coordinator time check",
                    "elapsedSeconds": 3600, "text": "[TIME CHECK FROM TCS PROVER]\nElapsed: 1 h. No time limit."},
                   {"kind": "status", "stage": "repair", "node": "author", "label": "Coordinator report",
                    "text": "Lemma 3 is verified."}]
        for record in records:
            self.assertTrue(server.important_record(record))
            self.assertFalse(server.important_record({**record, "root": False}))
            self.assertFalse(server.important_record({**record, "stage": "conversation"}))
        self.assertFalse(server.important_record({**records[0], "label": "Coordinator time checks"}))
        with mock.patch.object(server, "TRACE_LIMIT", 2):
            for record in records:
                self.app.add_trace(record)
            for number in range(5):
                self.app.add_trace(self.own(number))
            self.assertEqual([entry.get("label") for entry in self.app.retained_trace()][:2],
                             ["Coordinator time check", "Coordinator report"])

    def test_the_author_subagent_records_are_pinned(self):
        record = {"kind": "status", "stage": "solve", "label": "Author subagent started", "authorSubagent": True,
                  "subagentId": "agent-a", "subagentTask": "Prove Lemma 2.", "subagentState": "started",
                  "subagentsRunning": 1, "text": "Prove Lemma 2."}
        self.assertTrue(server.important_record(record))
        self.assertFalse(server.important_record({**record, "root": False}))
        self.assertFalse(server.important_record({**record, "authorSubagent": False}))
        with mock.patch.object(server, "TRACE_LIMIT", 2):
            self.app.add_trace(record)
            for number in range(5):
                self.app.add_trace(self.own(number))
            self.assertNotIn(record["label"], [entry.get("label") for entry in self.app.state["trace"]])
            self.assertEqual([entry.get("label") for entry in self.app.retained_trace()][0], "Author subagent started")


class ProgressLogTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.app = self.job()
        self.memory = self.app.run_dir / "memory"

    def write(self, data):
        self.memory.mkdir(exist_ok=True)
        (self.memory / "progress.md").write_bytes(data)

    def test_the_log_is_read_as_untrusted_text(self):
        self.assertIsNone(server.read_progress_log(self.app.run_dir))
        self.write("- 1 h 2 min: Lemma 1 verified (checked by B).\r\n- 1 h 9 min: \x1b[31mApproach X\x07 ruled out.\r"
                   .encode("utf-8") + b"\xff end")
        log = server.read_progress_log(self.app.run_dir)
        self.assertEqual(log["text"], "- 1 h 2 min: Lemma 1 verified (checked by B).\n- 1 h 9 min: [31mApproach X ruled "
                                      "out.\n� end")
        self.assertEqual((log["chars"], log["truncated"], log["bytes"]),
                         (len(log["text"]), False, (self.memory / "progress.md").stat().st_size))
        self.assertTrue(log["updatedAt"].endswith("+00:00"))

    def test_a_long_log_keeps_its_last_100000_characters(self):
        lines = "".join(f"- {number} min: αβγ step {number} verified.\n" for number in range(20_000))
        self.write(lines.encode("utf-8"))
        log = server.read_progress_log(self.app.run_dir)
        self.assertEqual((log["chars"], log["truncated"]), (server.MAX_PROGRESS_LOG_CHARS, True))
        self.assertEqual(log["text"], lines[-server.MAX_PROGRESS_LOG_CHARS:])
        self.assertEqual(log["bytes"], len(lines.encode("utf-8")))
        # A read that starts inside a character begins at the next one, also when removed control characters
        # leave less than the window to show: no half character becomes a replacement mark.
        # The read starts at the second of the three bytes of "€".
        data = "x€".encode("utf-8") + b"\x01" * (server.PROGRESS_LOG_READ_BYTES - 14) + "- 9 h: done.".encode("utf-8")
        self.assertEqual(len(data) - server.PROGRESS_LOG_READ_BYTES, 2)
        self.write(data)
        log = server.read_progress_log(self.app.run_dir)
        self.assertEqual((log["text"], log["truncated"], log["bytes"]), ("- 9 h: done.", True, len(data)))

    @unittest.skipIf(os.name == "nt", "POSIX links and FIFOs")
    def test_links_fifos_and_second_hard_links_are_never_read(self):
        outside = self.root / "outside.md"
        outside.write_text("Not the log.")
        self.memory.mkdir()
        (self.memory / "progress.md").symlink_to(outside)
        self.assertIsNone(server.read_progress_log(self.app.run_dir))
        (self.memory / "progress.md").unlink()
        os.link(outside, self.memory / "progress.md")
        self.assertIsNone(server.read_progress_log(self.app.run_dir))
        (self.memory / "progress.md").unlink()
        os.mkfifo(self.memory / "progress.md")
        self.assertIsNone(server.read_progress_log(self.app.run_dir))
        (self.memory / "progress.md").unlink()
        self.memory.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "progress.md").write_text("Elsewhere.")
        self.memory.symlink_to(elsewhere)
        self.assertIsNone(server.read_progress_log(self.app.run_dir))

    def test_the_tick_shows_the_log_of_a_coordinator_job_when_it_changes(self):
        self.app.collect_progress_log()
        self.assertIsNone(self.app.snapshot()["coordinatorProgress"])
        self.assertEqual(self.app.progress_log(), {"text": None, "version": None})
        self.write(b"- 1 h: Lemma 1 verified.\n")
        self.app.collect_progress_log()
        # The state names the log (its version, when it changed, how long it is); the text comes apart.
        shown = self.app.snapshot()["coordinatorProgress"]
        self.assertEqual(set(shown), {"updatedAt", "truncated", "chars", "bytes", "version"})
        self.assertEqual(self.app.progress_log(), {**shown, "text": "- 1 h: Lemma 1 verified.\n"})
        # Unchanged: not read again.
        with mock.patch.object(server, "read_progress_log", side_effect=AssertionError("read again")):
            self.app.collect_progress_log()
        self.write(b"- 1 h: Lemma 1 verified.\n- 2 h: Lemma 2 verified.\n")
        self.app.collect_progress_log()
        self.assertIn("Lemma 2 verified", self.app.progress_log()["text"])
        self.assertNotEqual(self.app.state["coordinatorProgress"]["version"], shown["version"])
        (self.memory / "progress.md").unlink()
        self.app.collect_progress_log()
        self.assertIsNone(self.app.state["coordinatorProgress"])
        self.assertEqual(self.app.progress_log(), {"text": None, "version": None})
        # A job whose author works alone has no progress log.
        alone = self.job(author_subagents=0)
        (alone.run_dir / "memory").mkdir()
        (alone.run_dir / "memory/progress.md").write_text("- Written anyway.\n")
        alone.collect_progress_log()
        self.assertIsNone(alone.state["coordinatorProgress"])

    def test_state_polls_carry_the_logs_version_and_the_page_fetches_its_text(self):
        self.write(("- 1 h: Lemma 1 verified.\n" * 4000).encode())
        self.app.collect_progress_log()
        http_server = chat_test.serve(self, self.app)
        job = self.app.run_dir.name
        status, state = chat_test.call(http_server, f"/state?job={job}")
        self.assertEqual(status, 200)
        self.assertNotIn("text", state["coordinatorProgress"])
        self.assertNotIn("Lemma 1 verified", json.dumps(state))
        status, log = chat_test.call(http_server, f"/progress-log?job={job}")
        self.assertEqual((status, log["text"], log["version"]),
                         (200, "- 1 h: Lemma 1 verified.\n" * 4000, state["coordinatorProgress"]["version"]))
        # The same checks as the chat answer route: the session token, and a job of this server.
        self.assertEqual(chat_test.call(http_server, f"/progress-log?job={job}", token=False)[0], 403)
        self.assertEqual(chat_test.call(http_server, "/progress-log?job=missing")[0], 400)
        # The gateway forwards it to the job's controller.
        from ui import live_gateway

        self.assertEqual(live_gateway.GET_ROUTES["/progress-log"], {"job"})

    def test_a_coordinator_job_another_process_runs_shows_its_log(self):
        # A terminal run, or a runner a crashed server left behind: the page shows it read-only, as it is.
        self.app.state["runningElsewhere"] = "terminal run"
        self.write(b"- 1 h: Lemma 1 verified.\n")
        self.app.collect_progress_log()
        self.assertEqual(self.app.progress_log()["text"], "- 1 h: Lemma 1 verified.\n")
        # A read-only saved job's log is read by its batch instead.
        self.app.state.update(readOnly=True, coordinatorProgress=None)
        self.app.collect_progress_log()
        self.assertIsNone(self.app.state["coordinatorProgress"])

    def test_a_run_moved_into_a_batch_folder_shows_its_own_log(self):
        # Its settings still name the folder it was moved from as the author's working folder.
        self.app.state["goalWorkspace"] = str(self.app.run_dir.resolve())
        self.app._save_job_settings(self.app.state)
        self.write(b"- 1 h: Lemma 1 verified.\n")
        self.app.process = self.app.active_token = self.app.worker_token = None
        archive = self.runs / "archive"
        archive.mkdir()
        controller = server.controller_directory(self.app.run_dir)
        (archive / ".controller").mkdir()
        controller.rename(archive / ".controller" / controller.name)
        self.app.run_dir.rename(archive / self.app.run_dir.name)
        catalog = batches.BatchCatalog(self.runs, current_root=self.runs)
        state = catalog.state("archive", self.app.run_dir.name)
        self.assertEqual(state["authorMode"], "coordinator")
        self.assertEqual(state["coordinatorProgress"]["chars"], len("- 1 h: Lemma 1 verified.\n"))
        self.assertEqual(catalog.response("/progress-log", "archive", self.app.run_dir.name)[0]["text"],
                         "- 1 h: Lemma 1 verified.\n")

    def test_the_servers_tick_reads_the_logs(self):
        manager = object.__new__(server.Server)
        manager.jobs, manager.jobs_lock = {"job": self.app}, threading.RLock()
        manager._sleep = server.SleepWatch()
        self.write(b"- 1 h: Lemma 1 verified.\n")
        manager._tick_jobs()
        self.assertEqual(self.app.progress_log()["text"], "- 1 h: Lemma 1 verified.\n")


if __name__ == "__main__":
    unittest.main()
