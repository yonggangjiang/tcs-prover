"""The server side of TCS Prover's goal loop, the coordinator's team and the editable texts: the job settings
goalLoop, authorSubagentModels, authorTeam and promptSet, their checks, the runner options of every launch, older
jobs, saved settings, folder runs, continuations, the terminal and the Prompts dialog's API.

Offline: runners are stand-in processes (Popen is patched); no CLI or model starts.
"""

import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import test_no_web_search as no_web_search
from tests import test_team_goal_loop as team_loop
import workflow_runner as runtime
from runner import prompt_catalog, providers
from ui import batches, cli, presets, server

STATEMENT = "Every finite task has property P."
MODELS = [{"model": "gpt-6.1-sol", "effort": "high"}, {"model": "claude-opus-5-5", "effort": "max"},
          {"model": "gpt-6-luna", "effort": "low"}]


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


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

    def job(self, body=None, **settings):
        app = server.App(runs=self.runs, load_latest=False)
        app.start_direct_statement(STATEMENT, team_options=server.web_team_options(body or {}), **settings)
        return app

    def saved(self, app):
        return json.loads((server.controller_directory(app.run_dir) / server.JOB_SETTINGS_FILENAME).read_text())

    def restored(self, app):
        app.process = app.active_token = app.worker_token = None
        app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        return server.restore_saved_app(server.App(app.trace_file, self.runs))

    def make_old(self, app):
        """Turn a job into one saved before the goal loop and the team existed."""

        for folder in (server.controller_directory(app.run_dir), app.run_dir):
            path = folder / server.JOB_SETTINGS_FILENAME
            settings = {key: value for key, value in json.loads(path.read_text()).items()
                        if key not in {"goalLoop", "authorSubagentModels", "authorTeam"}}
            path.write_text(json.dumps(settings))
            (folder / server.PROMPT_SET_FILENAME).unlink()
            (folder / server.PROMPT_EDITS_FILENAME).unlink(missing_ok=True)

    def forget_edits(self, app):
        """Turn a job into one saved before the texts edited for it were (its texts are saved, its edits not)."""

        for folder in (server.controller_directory(app.run_dir), app.run_dir):
            (folder / server.PROMPT_EDITS_FILENAME).unlink()


class JobTests(ServerCase):
    def test_a_new_job_records_the_loop_its_team_and_every_text_and_passes_them_at_every_launch(self):
        app = self.job({"authorSubagentModels": MODELS,
                        "promptOverrides": {"goal_continuation": "Edited: {continuation} {objective}",
                                            "team_tool_wait": "Wait {as you like}."}})
        argv = self.launches[-1]
        self.assertEqual((option(argv, "--goal-loop"), option(argv, "--author-team")), ("tcs", "tcs"))
        self.assertEqual(json.loads(option(argv, "--author-subagent-models")), MODELS)
        texts_path = Path(option(argv, "--prompt-set-file"))
        self.assertEqual(texts_path, server.controller_directory(app.run_dir) / server.PROMPT_SET_FILENAME)
        texts = json.loads(texts_path.read_text())
        self.assertEqual(set(texts), {entry["name"] for entry in prompt_catalog.entries(runtime, server.REVIEW_PROMPT)})
        self.assertEqual(texts["goal_continuation"], "Edited: {continuation} {objective}")
        self.assertEqual(texts["team_tool_wait"], "Wait {as you like}.")
        self.assertEqual(texts["author"], app.state["authorPrompt"])
        self.assertEqual(json.loads((app.run_dir / server.PROMPT_SET_FILENAME).read_text()), texts)
        self.assertEqual(app.state["promptSet"], texts)
        self.assertEqual(app.state["authorTeam"], [
            {"member": 1, "model": "gpt-6.1-sol", "effort": "high", "modelLabel": "GPT 6.1 Sol",
             "program": "Codex CLI", "connection": None},
            {"member": 2, "model": "claude-opus-5-5", "effort": "max", "modelLabel": "Claude Opus 5.5",
             "program": "Claude Code", "connection": None},
            {"member": 3, "model": "gpt-6-luna", "effort": "low", "modelLabel": "GPT 6 Luna",
             "program": "Codex CLI", "connection": None}])
        saved = self.saved(app)
        self.assertEqual((saved["goalLoop"], saved["authorSubagentModels"]), ("tcs", MODELS))
        self.assertEqual(saved["authorTeam"], app.state["authorTeam"])
        # Later edits of the workflow never reach this job: its runner and server read its own copy.
        restored = self.restored(app)
        self.assertEqual({key: restored.state[key] for key in ("goalLoop", "authorSubagentModels", "authorTeam",
                                                               "promptSet")},
                         {key: app.state[key] for key in ("goalLoop", "authorSubagentModels", "authorTeam",
                                                          "promptSet")})
        with mock.patch.object(server.prompt_catalog, "entries", side_effect=AssertionError("live workflow read")):
            restored.resume()
        argv = self.launches[-1]
        self.assertEqual(json.loads(Path(option(argv, "--prompt-set-file")).read_text()), texts)
        self.assertEqual(json.loads(option(argv, "--author-subagent-models")), MODELS)
        # A reply turn of the paused author: how its loop ran, its team and its own summary texts.
        converse = restored._conversation_options_locked()
        self.assertEqual((option(converse, "--goal-loop"), option(converse, "--author-team")), ("tcs", "tcs"))
        self.assertIsNotNone(option(converse, "--prompt-set-file"))
        # A chat message is framed with the job's own chat text.
        restored.state["promptSet"] = {**texts, "chat": "MSG {message} -> {answer_file}"}
        self.assertEqual(server.chat_template(restored.state["promptSet"]), "MSG {message} -> {answer_file}")

    def test_same_as_the_coordinator_and_an_author_alone(self):
        app = self.job()
        self.assertEqual(app.state["authorSubagentModels"], None)
        self.assertEqual([member["model"] for member in app.state["authorTeam"]], [server.DEFAULT_AUTHOR_MODEL] * 3)
        argv = self.launches[-1]
        self.assertIsNone(option(argv, "--author-subagent-models"))
        self.assertEqual(option(argv, "--author-team"), "tcs")
        alone = self.job({"authorSubagentModels": MODELS}, author_subagents=0)
        self.assertEqual((alone.state["authorTeam"], alone.state["authorSubagentModels"]), (None, None))
        self.assertEqual(option(self.launches[-1], "--author-team"), "builtin")
        codex = self.job({"goalLoop": "codex"})
        self.assertEqual(option(self.launches[-1], "--goal-loop"), "codex")
        self.assertEqual(codex.state["goalLoop"], "codex")

    def test_jobs_from_before_keep_codex_s_goal_mode_the_built_in_subagents_and_the_live_texts(self):
        app = self.job()
        self.make_old(app)
        restored = self.restored(app)
        self.assertEqual({key: restored.state[key] for key in ("goalLoop", "authorTeam", "authorSubagentModels",
                                                               "promptSet")},
                         {"goalLoop": "codex", "authorTeam": None, "authorSubagentModels": None, "promptSet": None})
        restored.resume()
        argv = self.launches[-1]
        self.assertEqual((option(argv, "--goal-loop"), option(argv, "--author-team")), ("codex", "builtin"))
        self.assertNotIn("--prompt-set-file", argv)
        self.assertNotIn("--author-subagent-models", argv)
        converse = restored._conversation_options_locked()
        self.assertEqual((option(converse, "--goal-loop"), option(converse, "--author-team")), ("codex", "builtin"))
        self.assertNotIn("--prompt-set-file", converse)
        # Revised in place (its own state), it stays as it was.
        self.assertTrue(server.saved_team_options(restored.state)["legacy_team"])
        self.assertTrue(server.web_team_options({"goalLoop": "tcs"}, restored.state)["legacy_team"])
        self.assertNotIn("goalLoop", self.saved(restored))
        # A job made from it (a continuation of its author's work) keeps them too.
        continued = server.App(runs=self.runs, load_latest=False)
        continued.start_direct_statement(STATEMENT, continuation_source=str(app.run_dir), stopped_stage="solve",
                                         allow_external_source=True)
        self.assertEqual((continued.state["goalLoop"], continued.state["authorTeam"]), ("codex", None))
        self.assertEqual(option(self.launches[-1], "--author-team"), "builtin")
        # A continuation of a new job carries its loop, its models and its texts.
        source = self.job({"authorSubagentModels": MODELS, "promptOverrides": {"team_note": "Team: {members}"}})
        continued = server.App(runs=self.runs, load_latest=False)
        continued.start_direct_statement(STATEMENT, continuation_source=str(source.run_dir), stopped_stage="solve",
                                         allow_external_source=True)
        self.assertEqual(continued.state["authorSubagentModels"], MODELS)
        self.assertEqual(continued.state["promptSet"]["team_note"], "Team: {members}")

    def test_a_rerun_of_a_job_from_before_keeps_codex_s_goal_mode_and_the_built_in_subagents(self):
        old = self.job()
        self.make_old(old)
        restored = self.restored(old)
        self.assertEqual((restored.state["goalLoop"], restored.state["legacyTeam"]), ("codex", True))
        # The page's rerun of it asks for its goal loop and subagents (legacyTeam), whatever goal loop it sends.
        for loop in ("codex", "tcs"):
            with self.subTest(loop=loop):
                app = self.job({"legacyTeam": True, "goalLoop": loop})
                argv = self.launches[-1]
                self.assertEqual((option(argv, "--goal-loop"), option(argv, "--author-team")), ("codex", "builtin"))
                self.assertEqual((app.state["goalLoop"], app.state["authorTeam"], app.state["legacyTeam"]),
                                 ("codex", None, True))
                saved = self.saved(app)
                self.assertEqual((saved["goalLoop"], saved["legacyTeam"]), ("codex", True))
                # A new job all the same: it saves the texts it runs with.
                self.assertEqual(json.loads(Path(option(argv, "--prompt-set-file")).read_text()), app.state["promptSet"])
        # Restored, resumed, revised or continued, it keeps them, as a job from before does.
        restored = self.restored(app)
        self.assertEqual((restored.state["goalLoop"], restored.state["authorTeam"], restored.state["legacyTeam"]),
                         ("codex", None, True))
        restored.resume()
        self.assertEqual(option(self.launches[-1], "--author-team"), "builtin")
        self.assertTrue(server.web_team_options({"goalLoop": "tcs"}, restored.state)["legacy_team"])
        continued = server.App(runs=self.runs, load_latest=False)
        continued.start_direct_statement(STATEMENT, continuation_source=str(app.run_dir), stopped_stage="solve",
                                         allow_external_source=True)
        self.assertEqual((continued.state["goalLoop"], continued.state["authorTeam"], continued.state["legacyTeam"]),
                         ("codex", None, True))
        self.assertEqual(option(self.launches[-1], "--author-team"), "builtin")
        # Any other job runs TCS Prover's team, and saves no such setting.
        app = self.job({"goalLoop": "codex"})
        self.assertEqual((option(self.launches[-1], "--author-team"), app.state["legacyTeam"]), ("tcs", False))
        self.assertNotIn("legacyTeam", self.saved(app))
        self.assertFalse(self.restored(app).state["legacyTeam"])

    def test_the_subagent_models_are_checked_as_the_role_models_are(self):
        options = server.App._workflow_options
        for models, message in ((MODELS[:2], "Choose a model for each of the 3 author subagents"),
                                ([*MODELS[:2], {"model": "nonexistent", "effort": "high"}],
                                 "available model for author subagent 3"),
                                ([*MODELS[:2], {"model": "gpt-6.1-sol", "effort": "huge"}],
                                 "valid reasoning effort for author subagent 3"),
                                ([*MODELS[:2], {"model": "gpt-6.1-sol"}], "Author subagent 3 needs a model"),
                                ("all of them", "Same as the coordinator or a model for each")):
            with self.subTest(models=models), self.assertRaisesRegex(ValueError, message):
                options(include_review=False, author_subagent_models=models)
        # A setting no stage uses is not kept.
        off = {"review": False, "author": False, "critic": True, "writing": False}
        self.assertIsNone(options(include_review=False, author_subagent_models=MODELS[:1],
                                  workflow_stages=off)["authorSubagentModels"])
        with self.assertRaisesRegex(ValueError, "Choose TCS Prover or Codex as the goal loop"):
            options(include_review=False, goal_loop="mine")

    def test_a_web_cutoff_job_runs_codex_s_goal_mode_with_the_coordinator_s_model(self):
        historical = {"retrievalMode": "historical", "retrievalCutoffUtc": "2026-01-01T00:00:00Z"}
        with mock.patch.object(server.retrieval, "validate_options", return_value=historical):
            options = server.App._workflow_options(include_review=False, goal_loop="tcs")
            self.assertEqual((options["goalLoop"], options["authorTeam"]), ("codex", None))
            with self.assertRaisesRegex(ValueError, "With a web cutoff, author subagents use the coordinator's model"):
                server.App._workflow_options(include_review=False, author_subagent_models=MODELS)

    def test_edited_texts_are_checked_as_the_runner_checks_them(self):
        for overrides, message in (({"goal_continuation": "{nope}"}, "Goal continuation.*nope"),
                                   ({"team_note": "No members."}, None),
                                   ({"team_member": "{assignment} and {other}"}, "Member assignment.*other"),
                                   ({"nothing": "x"}, "no prompt of this name"),
                                   ({"codex_compact_prompt": ""}, None)):
            with self.subTest(overrides=overrides):
                if message is None:
                    self.assertEqual(server.App._workflow_options(include_review=False, prompt_overrides=overrides)[
                        "promptSet"][next(iter(overrides))], next(iter(overrides.values())))
                    continue
                with self.assertRaisesRegex(ValueError, message):
                    server.App._workflow_options(include_review=False, prompt_overrides=overrides)
        with self.assertRaisesRegex(ValueError, "Prompt overrides must map prompt names to prompt text"):
            server.web_prompt_options({"promptOverrides": {"team_note": 7}}, server.PROOF_PROMPTS)


class PromptEditTests(ServerCase):
    """The texts edited for a job (promptEdits), saved beside every text it runs with: what a rerun carries."""

    def test_a_job_saves_the_texts_edited_for_it_beside_its_texts(self):
        defaults = server.prompt_defaults()
        app = self.job({"promptOverrides": {"goal_continuation": "Go on: {continuation} {objective}",
                                            "team_note": defaults["team_note"]}},
                       author_prompt="My author. [STATEMENT]  ", critic_prompt=defaults["critic"])
        # Each edit as the job keeps it; one that left a text as its default is no edit.
        edits = {"goal_continuation": "Go on: {continuation} {objective}", "author": "My author. [STATEMENT]"}
        self.assertEqual(app.state["promptEdits"], edits)
        self.assertEqual({name: app.state["promptSet"][name] for name in edits}, edits)
        for folder in (server.controller_directory(app.run_dir), app.run_dir):
            self.assertEqual(json.loads((folder / server.PROMPT_EDITS_FILENAME).read_text()), edits)
        restored = self.restored(app)
        self.assertEqual(restored.state["promptEdits"], edits)
        # A job without edits saves that it has none.
        plain = self.job()
        self.assertEqual(plain.state["promptEdits"], {})
        self.assertEqual(self.restored(plain).state["promptEdits"], {})
        # A job whose texts were saved before their edits were, and a job from before: none known.
        self.forget_edits(app)
        self.assertIsNone(self.restored(app).state["promptEdits"])
        self.make_old(plain)
        self.assertIsNone(self.restored(plain).state["promptEdits"])

    def test_a_job_made_from_another_keeps_its_edits_and_adds_the_requests(self):
        source = self.job({"promptOverrides": {"team_note": "Team: {members}"}}, author_prompt="Mine. [STATEMENT]")
        edits = {"team_note": "Team: {members}", "author": "Mine. [STATEMENT]"}
        # The source ran another default of team_wait's description than today's workflow has.
        source.state["promptSet"] = {**source.state["promptSet"], "team_tool_wait": "Yesterday's wait."}
        for folder in (server.controller_directory(source.run_dir), source.run_dir):
            (folder / server.PROMPT_SET_FILENAME).write_text(json.dumps(source.state["promptSet"]))
        # A continuation runs the source's texts and keeps its edits.
        continued = server.App(runs=self.runs, load_latest=False)
        continued.start_direct_statement(STATEMENT, continuation_source=str(source.run_dir), stopped_stage="solve",
                                         allow_external_source=True)
        self.assertEqual(continued.state["promptSet"], source.state["promptSet"])
        self.assertEqual(continued.state["promptEdits"], edits)
        # A revision of a job keeps its edits and adds the request's.
        team = server.web_team_options({"promptOverrides": {"goal_continuation": "On: {continuation} {objective}"}},
                                       source.state)
        self.assertEqual(team["prompt_edits"], {**edits, "goal_continuation": "On: {continuation} {objective}"})
        options = server.App._workflow_options(include_review=False, author_prompt=source.state["authorPrompt"],
                                               **team)
        self.assertEqual(options["promptEdits"], {**edits, "goal_continuation": "On: {continuation} {objective}"})
        # Made from a job that saved no edits: none known, so it saves none (a rerun then compares with the defaults).
        self.forget_edits(source)
        continued = server.App(runs=self.runs, load_latest=False)
        continued.start_direct_statement(STATEMENT, continuation_source=str(source.run_dir), stopped_stage="solve",
                                         allow_external_source=True)
        self.assertEqual(continued.state["promptSet"], source.state["promptSet"])
        self.assertIsNone(continued.state["promptEdits"])
        self.assertFalse((server.controller_directory(continued.run_dir) / server.PROMPT_EDITS_FILENAME).exists())
        self.assertIs(server.web_team_options({}, {**source.state, "promptEdits": None})["prompt_edits"], False)


class MemberRecordTests(ServerCase):
    def test_two_assignments_of_one_member_are_both_listed_with_their_own_ids(self):
        records = team_loop.member_records([(1, "Lemma A\nProve it."), (1, "Lemma B\nProve it.")])
        app = self.job()
        for record in records:
            app.add_trace(record)

        def listed(trace):
            events = [(record["subagentId"], record["event"]["item"]["id"], record["event"]["item"]["text"])
                      for record in trace if record.get("kind") == "codex_event" and record.get("root") is False
                      and "item" in record["event"]]
            statuses = [(record["subagentId"], record["subagentState"]) for record in trace
                        if record.get("authorSubagent") is True]
            return events, statuses

        # The job's live window, and the job restored from its transcript after a restart.
        for trace in (app.state["trace"], self.restored(app).state["trace"]):
            events, statuses = listed(trace)
            self.assertEqual(events, [("member-1-1", "item_0", "About Lemma A."),
                                      ("member-1-1", "item_1", "Done: Lemma A"),
                                      ("member-1-2", "item_0", "About Lemma B."),
                                      ("member-1-2", "item_1", "Done: Lemma B")])
            self.assertEqual(statuses, [("member-1-1", "started"), ("member-1-1", "finished"),
                                        ("member-1-2", "started"), ("member-1-2", "finished")])


class NoWebSearchTests(ServerCase):
    def test_the_warning_stays_listed_with_the_job_s_other_warnings(self):
        warning = no_web_search.warning_record()
        app = self.job()
        app.add_trace(warning)
        for index in range(server.TRACE_LIMIT + 10):
            app.add_trace({"kind": "status", "stage": "solve", "node": "author", "label": "Routine", "text": str(index)})
        # Routine records pushed it out of the live window; it stays pinned, in the page's trace and after a restart.
        self.assertNotIn(warning["text"], [record.get("text") for record in app.state["trace"]])
        for job in (app, self.restored(app)):
            listed = [record for record in job.retained_trace() if record.get("label") == "No web search"]
            self.assertEqual([(record["kind"], record["text"]) for record in listed],
                             [("diagnostic", warning["text"])])


class SettingsTests(ServerCase):
    def setUp(self):
        super().setUp()
        store = mock.patch.dict("os.environ", {"TCS_PROVER_API_MODELS": str(self.root / "state" / "api.json")})
        store.start()
        self.addCleanup(store.stop)

    def test_saved_settings_keep_the_loop_and_the_models_and_older_ones_load_with_the_defaults(self):
        builtin = presets.builtin_settings()
        self.assertEqual((builtin["goalLoop"], builtin["authorSubagentModels"]), ("tcs", None))
        settings = presets.valid_settings({"authorSubagents": 3, "authorSubagentModels": MODELS, "goalLoop": "codex"})
        self.assertEqual((settings["goalLoop"], settings["authorSubagentModels"]), ("codex", MODELS))
        older = presets.valid_settings({"authorSubagents": 2})
        self.assertEqual((older["goalLoop"], older["authorSubagentModels"]), ("tcs", None))
        for values, message in (({"authorSubagents": 2, "authorSubagentModels": MODELS}, "each of the 2"),
                                ({"goalLoop": "x"}, "goal loop")):
            with self.subTest(values=values), self.assertRaisesRegex(presets.PresetError, message):
                presets.valid_settings(values)
        self.assertIsNone(presets.valid_settings({"authorSubagents": 0, "authorSubagentModels": MODELS})[
            "authorSubagentModels"])
        identity = presets.save("Team", {"authorSubagents": 3, "authorSubagentModels": MODELS})
        listed = next(row for row in presets.listing()["presets"] if row["id"] == identity)
        self.assertEqual(listed["settings"]["authorSubagentModels"], MODELS)
        self.assertEqual(listed["labels"]["gpt-6-luna"], "GPT 6 Luna")

    def test_a_folder_run_and_the_terminal_carry_the_models(self):
        manager = server.Server.__new__(server.Server)
        with mock.patch.object(server.App, "start_direct_statement") as start:
            options = server.Server._direct_options({"authorSubagentModels": MODELS, "goalLoop": "codex",
                                                     "promptOverrides": {"team_note": "T {members}"}})
        self.assertEqual(options["team_options"]["author_subagent_models"], MODELS)
        self.assertEqual(options["team_options"]["goal_loop"], "codex")
        self.assertEqual(options["team_options"]["prompt_overrides"], {"team_note": "T {members}"})
        terminal = cli.direct_cli_options(author_subagent_models=json.dumps(MODELS), goal_loop="codex")
        self.assertEqual(terminal["team_options"], {"goal_loop": "codex", "author_subagent_models": MODELS,
                                                    "prompt_overrides": {}})
        del manager, start


class CatalogRouteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.http = server.Server((server.HOST, 0), runs=Path(folder.name) / "runs")
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)

    def request(self, path, body=None, token=True):
        headers = {"Origin": self.http.origin, **({"X-TCS-Prover-Token": self.http.token} if token else {})}
        raw = None
        if body is not None:
            raw, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=10)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_the_prompts_dialog_gets_every_text_with_its_metadata_and_checks_edits(self):
        status, listing = self.request("/prompt-catalog")
        self.assertEqual(status, 200)
        self.assertEqual(listing["groups"][:6], ["Statement review", "Author", "Coordinator", "Goal loop", "Team",
                                                  "Team tools"])
        entry = next(item for item in listing["prompts"] if item["name"] == "goal_continuation")
        self.assertEqual(set(entry), {"name", "group", "label", "when", "placeholders", "text", "emptyAllowed"})
        self.assertEqual((entry["group"], entry["placeholders"]),
                         ("Goal loop", ["{continuation}", "{elapsed}", "{limit}", "{objective}"]))
        self.assertEqual(entry["text"], runtime.builtin_workflow("author_critic")["prompts"]["goal_continuation"])
        review = next(item for item in listing["prompts"] if item["name"] == "review")
        self.assertEqual(review["text"], server.REVIEW_PROMPT)
        self.assertTrue(next(item for item in listing["prompts"] if item["name"] == "codex_compact_prompt")[
            "emptyAllowed"])
        status, checked = self.request("/prompt-catalog/check", {"promptOverrides": {
            "goal_continuation": "{bad}", "author": "Fine [STATEMENT]", "x": "y"}})
        self.assertEqual(status, 200)
        self.assertEqual(set(checked["errors"]), {"goal_continuation", "x"})
        self.assertEqual(self.request("/prompt-catalog/check", {})[0], 400)
        self.assertEqual(self.request("/prompt-catalog", token=False)[0], 403)


class SetupTests(ServerCase):
    def test_a_members_program_counts_as_needed_for_the_author_stage(self):
        state = {**server.empty_state(), "phase": "running", "stage": "solve",
                 "authorModel": "gpt-6.1-sol", "criticModel": "gpt-6.1-sol", "writerModel": "gpt-6.1-sol",
                 "reviewModel": "gpt-6.1-sol",
                 "authorTeam": [{"member": 1, "model": "claude-opus-5-5"}, {"member": 2, "model": "gpt-6.1-sol"}]}
        programs, sign_in = server.Server._setup_programs(state)
        self.assertIn("claude", programs)
        self.assertIn("claude", sign_in)


class BatchViewTests(ServerCase):
    def test_a_saved_job_in_a_batch_shows_its_loop_team_and_texts(self):
        app = self.job({"authorSubagentModels": MODELS, "promptOverrides": {"team_note": "Team: {members}"}})
        catalog = batches.BatchCatalog(self.runs) if hasattr(batches, "BatchCatalog") else None
        if catalog is None:
            self.skipTest("no batch catalog class")
        state = catalog.state(batches.ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual((state["goalLoop"], state["authorSubagentModels"]), ("tcs", MODELS))
        self.assertEqual(state["promptSet"], app.state["promptSet"])
        # The texts edited for it, which a rerun from the saved batch carries.
        self.assertEqual(state["promptEdits"], {"team_note": "Team: {members}"})
        self.assertEqual((state["goalLoopRecorded"], state["legacyTeam"]), (True, False))
        self.make_old(app)
        state = catalog.state(batches.ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual((state["goalLoop"], state["authorTeam"], state["promptSet"]), ("codex", None, None))
        self.assertIsNone(state["promptEdits"])
        # As a restored job from before: a rerun from the saved batch keeps its goal loop and subagents.
        self.assertEqual((state["goalLoopRecorded"], state["legacyTeam"]), (False, True))
