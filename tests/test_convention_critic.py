"""The convention critic and the critic's transparency records.

The convention critic is its own workflow step: it runs once, after the critic
accepts a proof (a clean pass, or a pass accepted at the round limit) and
before writing or the end, on that accepted proof with the critic's settings.
Its verdict goes to the user (a loophole warning on the job page) and never
changes the run's route. Critic results record whether the critic changed the
proof, with the diff; the job page shows the critic's one output per round.

Offline: stand-in model attempts, a patched clock and stubbed runners; no
model call is made and no real CLI starts.
"""

import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from tests import critic_doubles
from ui import server

import test_critic_subagents as subagents

PROOF, FIXED, AUTHOR_CRITIC, CLEAN_UP = subagents.PROOF, subagents.FIXED, subagents.AUTHOR_CRITIC, subagents.CLEAN_UP
STATEMENT = "Every finite task has property P."

# The prompt exactly as the owner approved it (its first sentence updated on 30 Oct: it runs after acceptance).
PROMPT = """You are the convention critic. You run after the proof critics have accepted the proof, independently of them:
you do not judge whether the proof is correct, and you do not edit any file.

Examine the problem statement below and the proposed proof in proof.md. Try to guess what the user, as a
conventional mathematician or theoretical computer science researcher, actually wants from this problem. Then
decide whether the proof does what the user actually wants, or whether it is "cheating" by exploiting loopholes
in the problem description, such as a brute-force construction that stores all answers, so that it satisfies
the letter of the statement but not what the user meant.

Return verdict=loophole only if the proof very much exploits a loophole. Then explain in loophole how it exploits
the problem description, and give in suggested_statement a modified problem statement that fits what the user
needs, so that the user can rerun with it. Otherwise return verdict=faithful and leave loophole and
suggested_statement empty. In intended, say in a few sentences what the user most likely wants; in assessment,
say how the proof relates to that. Return only the requested JSON.

STATEMENT:
{statement}"""

LOOPHOLE = {"verdict": "loophole", "intended": "INTENDED-MARK: an algorithm for every n.",
            "assessment": "ASSESSMENT-MARK: the proof stores a table.",
            "loophole": "LOOPHOLE-MARK: the statement fixes n, so a lookup table of all answers satisfies it.",
            "suggested_statement": "SUGGESTED-MARK: For every n, give a polynomial-time algorithm."}


class DeclarationTests(unittest.TestCase):
    def test_the_convention_critic_is_its_own_step_between_the_critic_and_writing(self):
        workflow = runtime.builtin_workflow("author_critic")
        nodes = workflow["nodes"]
        self.assertEqual(list(nodes), ["author", "critic", "convention"])
        self.assertEqual(nodes["critic"]["next"]["pass"]["to"], "convention")
        self.assertEqual(nodes["critic"]["next"]["reject"], "author")
        self.assertEqual(nodes["critic"]["next"]["needs_revision"]["to"], "end")
        node = nodes["convention"]
        self.assertEqual({key: node[key] for key in ("run", "workflow_stage", "skip", "role", "stage", "prompt")},
                         {"run": "structured", "workflow_stage": "critic", "skip": "end", "role": "critic",
                          "stage": "critic", "prompt": "convention_critic"})
        self.assertEqual(node["inputs"], {"statement": "state.statement"})
        self.assertEqual(node["proof_handoff"], {"candidate": "solution", "statement": "statement"})
        self.assertNotIn("edit", node)
        self.assertNotIn("features", node)
        self.assertNotIn("subagents", node)
        self.assertEqual(node["next"], {"done": "end"})
        self.assertEqual(node["schema"]["properties"]["verdict"]["enum"], ["faithful", "loophole"])
        self.assertEqual(set(node["schema"]["required"]),
                         {"verdict", "intended", "assessment", "loophole", "suggested_statement"})
        self.assertEqual(workflow["prompts"]["convention_critic"], PROMPT)

    def test_its_rules_require_the_reasons_and_a_suggestion_only_for_a_loophole(self):
        node = runtime.builtin_workflow("author_critic")["nodes"]["convention"]
        context = {"state": {"statement": "S", "solution": "P"}, "visit": 1}
        accept = lambda answer: runtime._accept_node_answer(node, context, None, answer, "{}")
        self.assertEqual(accept(dict(LOOPHOLE)), LOOPHOLE)
        self.assertEqual(accept(dict(critic_doubles.FAITHFUL)), critic_doubles.FAITHFUL)
        for change, problem in ((dict(intended=" "), "the intended field must not be empty"),
                                (dict(assessment=""), "the assessment field must not be empty"),
                                (dict(loophole=""), "the loophole field must be non-empty when verdict is loophole"),
                                (dict(suggested_statement=""), "the suggested_statement field must be non-empty")):
            with self.subTest(change=change), self.assertRaisesRegex(runtime.InvalidAnswer, problem):
                accept({**LOOPHOLE, **change})
        with self.assertRaisesRegex(runtime.InvalidAnswer, "the loophole field must be non-empty when verdict is "
                                                            "loophole and empty when verdict is faithful"):
            accept({**critic_doubles.FAITHFUL, "loophole": "A loophole."})

    def test_a_per_job_convention_prompt_replaces_the_workflow_prompt(self):
        workflow = runtime.builtin_workflow("author_critic")
        prompts = runtime.prepare(workflow, {"prompts": {"convention_critic": "Judge {statement}."}})
        self.assertEqual(prompts["convention_critic"], "Judge {statement}.")
        self.assertEqual(runtime.PROMPT_FILE_OPTIONS["convention"], "convention_critic")


class RunnerTests(subagents.CriticCase):
    def test_a_clean_pass_runs_it_once_on_the_accepted_proof_before_the_writer(self):
        self.convention = lambda call: dict(LOOPHOLE)
        writer = []

        def attempt_writer(call):
            writer.append(call)
            (call["workspace"] / "latex-source.tex").write_text(subagents.LATEX)
            return {"summary": "Wrote the paper."}

        def model(call):
            return attempt_writer(call) if call["stage"] == "final" else subagents.critic_answer()

        with mock.patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}):
            state = self.run_critic(model, workflows=(AUTHOR_CRITIC, CLEAN_UP))
        self.assertEqual(state["output"], subagents.LATEX)
        [convention] = self.conventions()
        # Its own read-only call on the accepted proof, with the statement, no subagents and no editable file.
        self.assertTrue(convention["text"].startswith(PROMPT.replace("{statement}", "Task")))
        self.assertEqual(dict(convention["input_files"])["proof.md"], PROOF)
        self.assertIsNone(convention["edit"])
        self.assertEqual((convention["features"], convention["subagents"]), ((), None))
        self.assertIn("READ-ONLY PROOF FILES:", convention["text"])
        # After the critic's decision and before the writer: one record, of the accepting round.
        order = [event.get("kind") if event.get("kind") != "request" else event.get("label") for event in self.events
                 if event.get("kind") in {"critic_result", "convention_result"}
                 or event.get("kind") == "request" and event.get("label") in {"Convention critic", "Critic coordinator"}
                 or event.get("kind") == "request" and event.get("stage") == "final"]
        self.assertEqual(order, ["Critic coordinator", "critic_result", "Convention critic", "convention_result",
                                 "Exact final input"])
        [record] = self.kinds("convention_result")
        self.assertEqual({key: record[key] for key in ("label", "round", "verdict", "loophole", "suggestedStatement")},
                         {"label": "Convention critic · round 1", "round": 1, "verdict": "loophole",
                          "loophole": LOOPHOLE["loophole"], "suggestedStatement": LOOPHOLE["suggested_statement"]})
        self.assertEqual(len(writer), 1)

    def test_a_pass_accepted_at_the_round_limit_runs_it_once_on_the_repaired_proof(self):
        state = self.run_critic(subagents.fixes, {"critic_rounds": 1})
        self.assertEqual(state["output"], FIXED)
        [convention] = self.conventions()
        self.assertEqual(dict(convention["input_files"])["proof.md"], FIXED)
        self.assertEqual([record["round"] for record in self.kinds("convention_result")], [2])
        # The critic's rounds never ran it: only after the final check accepted the proof.
        labels = [call["label"] for call in self.calls]
        self.assertEqual(labels, ["Critic coordinator", "Critic coordinator", "Convention critic"])

    def test_without_writing_it_runs_just_before_the_run_ends(self):
        state = self.run_critic(subagents.unchanged, {"workflow_stages": {"review": False, "author": False,
                                                                          "critic": True, "writing": False}},
                                workflows=(AUTHOR_CRITIC, CLEAN_UP))
        self.assertEqual(len(self.conventions()), 1)
        self.assertEqual(state["resultKind"], "proof")
        kinds = [event["kind"] for event in self.events if event["kind"] in {"convention_result", "final_result"}]
        self.assertEqual(kinds, ["convention_result", "final_result"])

    def test_a_rejection_never_runs_it(self):
        reject = lambda call: subagents.critic_answer(verdict="reject", bugs="Step 2 divides by n = 0.")
        state = self.run_critic(reject, {"workflow_stages": {"review": False, "author": False, "critic": True,
                                                            "writing": True}})
        self.assertTrue(state["failed"])
        self.assertEqual(self.conventions(), [])
        self.assertEqual(self.kinds("convention_result"), [])

    def test_its_verdict_never_changes_the_route_or_the_proof(self):
        for verdict in (LOOPHOLE, critic_doubles.FAITHFUL):
            with self.subTest(verdict=verdict["verdict"]):
                self.calls.clear()
                self.convention = lambda call, verdict=verdict: dict(verdict)
                state = self.run_critic(subagents.unchanged)
                self.assertEqual((state["output"], state.get("failed"), state.get("paused")), (PROOF, None, None))
                self.assertEqual(state["solution"], PROOF)

    def test_a_failed_convention_critic_is_retried_every_minute_and_never_rejects(self):
        answers = iter([runtime.Error(f"Codex critic failed: {subagents.retry_test.CAPACITY}"), dict(LOOPHOLE)])
        self.convention = lambda call: next(answers)
        state = self.run_critic(subagents.unchanged)
        self.assertEqual(state["output"], PROOF)
        self.assertEqual(len(self.conventions()), 2)
        self.assertEqual(self.waits(), 60)
        notice = self.notices("Convention critic · " + runtime.RETRY_LABEL)
        self.assertEqual([event["activityLabel"] for event in notice], ["Convention critic"])
        self.assertEqual([event["verdict"] for event in self.kinds("convention_result")], ["loophole"])

    def test_an_answer_that_breaks_its_rules_is_asked_again(self):
        answers = iter([{**LOOPHOLE, "suggested_statement": ""}, dict(LOOPHOLE)])
        self.convention = lambda call: next(answers)
        self.run_critic(subagents.unchanged)
        first, retry = self.conventions()
        self.assertIn("the suggested_statement field must be non-empty when verdict is loophole", retry["text"])
        self.assertEqual(len(self.kinds("convention_result")), 1)

    def test_a_run_paused_in_the_step_runs_it_again_when_resumed(self):
        pause = self.root / "pause-request.json"

        def convention(call):
            pause.write_text("{}")  # The user pauses while the convention critic runs; its call fails.
            raise runtime.Error("Codex critic failed: stopped")

        self.convention = convention
        state = self.run_critic(subagents.unchanged, {"goal_pause_file": str(pause)})
        self.assertTrue(state["paused"])
        [paused] = self.kinds("workflow_paused")
        self.assertEqual(paused["node"], "convention")
        self.assertEqual(paused["state"]["solution"], PROOF)
        self.assertEqual(self.kinds("convention_result"), [])
        # Resume: the step runs again from its saved input, then the run ends.
        pause.unlink()
        self.calls.clear()
        self.convention = lambda call: dict(critic_doubles.FAITHFUL)
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=self.attempts(subagents.unchanged)):
            state = runtime.execute_workflows([AUTHOR_CRITIC], dict(paused["state"]), {"start_node": "convention"})
        self.assertEqual([call["label"] for call in self.calls], ["Convention critic"])
        self.assertEqual(state["output"], PROOF)
        self.assertEqual([record["round"] for record in self.kinds("convention_result")], [1])


class ProofChangeTests(subagents.CriticCase):
    def test_a_clean_pass_records_an_unchanged_proof(self):
        self.run_critic(subagents.unchanged)
        report = self.kinds("critic_result")[0]["report"]
        self.assertEqual((report["proofChanged"], report["proofDiff"]), (False, ""))

    def test_a_pass_with_fixes_records_the_unified_diff_of_the_changes(self):
        self.run_critic(subagents.fixes, {"critic_rounds": 1})
        first, final_check = (event["report"] for event in self.kinds("critic_result"))
        self.assertTrue(first["proofChanged"])
        self.assertEqual(first["proofDiff"].splitlines()[:2], ["--- submitted proof", "+++ coordinator's proof"])
        self.assertIn("+Case n = 0: the claim holds trivially.", first["proofDiff"])
        self.assertEqual(first["proofDiffLines"], len(first["proofDiff"].splitlines()))
        # The final check cannot edit: unchanged.
        self.assertEqual((final_check["proofChanged"], final_check["proofDiff"]), (False, ""))

    def test_spacing_alone_is_no_change(self):
        self.assertEqual(runtime._proof_change("A\nB\n", "\nA  \nB\n\n", {}, 1, "critic"),
                         {"proofChanged": False, "proofDiff": ""})

    def test_a_very_long_diff_is_saved_whole_in_the_controller_folder(self):
        before = "\n".join(f"Line {index}." for index in range(40000))
        after = "\n".join(f"Line {index} (expanded)." for index in range(40000))
        folder = self.root / "controller" / "critic-diffs"
        with mock.patch.object(runtime, "PROOF_DIFF_INLINE_CHARS", 1000):
            change = runtime._proof_change(before, after, {"critic_diff_dir": str(folder)}, 2, "critic")
            inline = runtime._proof_change(before, after, {}, 2, "critic")
        self.assertTrue(change["proofChanged"])
        self.assertEqual(change["proofDiff"], "")
        saved = (folder / change["proofDiffFile"]).read_text()
        self.assertEqual(saved, inline["proofDiff"] + "\n")
        self.assertEqual(change["proofDiffChars"], len(inline["proofDiff"]))
        self.assertTrue(server.CRITIC_DIFF_NAME.fullmatch(change["proofDiffFile"]))
        self.assertIn("too long to show here", change["proofDiffNote"])
        self.assertIn("can be downloaded", change["proofDiffNote"])
        # Without the folder (a terminal run) nothing is shortened: the whole diff stays in the result.
        self.assertGreater(len(inline["proofDiff"]), 1000)
        self.assertNotIn("proofDiffFile", inline)

    def test_a_diff_that_cannot_be_saved_stays_whole_in_the_result(self):
        with mock.patch.object(runtime, "PROOF_DIFF_INLINE_CHARS", 10), \
                mock.patch.object(runtime, "_private_atomic_write", side_effect=OSError("No space left on device")):
            change = runtime._proof_change("A\nB", "A\nC", {"critic_diff_dir": str(self.root / "diffs")}, 1, "critic")
        self.assertIn("+C", change["proofDiff"])
        self.assertNotIn("proofDiffFile", change)
        self.assertTrue(any("the whole diff stays in the critic result" in event["text"]
                            for event in self.kinds("diagnostic")))


def convention_record(round_number=1, verdict="loophole", **extra):
    fields = {"intended": "A real algorithm.", "assessment": "It stores answers.",
              "loophole": "It stores every answer." if verdict == "loophole" else "",
              "suggestedStatement": "For every n, ..." if verdict == "loophole" else ""}
    return {"kind": "convention_result", "stage": "critic", "label": f"Convention critic · round {round_number}",
            "round": round_number, "verdict": verdict, **fields, **extra}


class ServerCase(unittest.TestCase):
    """A job of a server App whose runner launches are recorded, never started."""

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
        self.launches.append(argv)
        return mock.Mock(stdin=io.StringIO())

    def job(self, **options):
        app = server.App(runs=self.runs, load_latest=False)
        app.start_direct_statement(STATEMENT, **options)
        return app

    def feed(self, app, records):
        process = mock.Mock(stdout=io.StringIO("".join(json.dumps(record) + "\n" for record in records)))
        process.wait.return_value = process.poll.return_value = 0
        token = app.active_token = app.worker_token = object()
        app.process = process
        app._read_output(process, token)

    def restored(self, app):
        return server.restore_saved_app(server.App(app.trace_file, self.runs))


class ServerTests(ServerCase):
    def test_a_loophole_verdict_is_kept_persisted_and_listed_until_ignored(self):
        app = self.job()
        self.feed(app, [convention_record(1, "faithful"), convention_record(2)])
        warning = app.snapshot()["conventionWarning"]
        self.assertEqual({key: warning[key] for key in ("round", "loophole", "suggestedStatement", "dismissed")},
                         {"round": 2, "loophole": "It stores every answer.", "suggestedStatement": "For every n, ...",
                          "dismissed": False})
        self.assertTrue(warning["time"])
        saved = json.loads((server.controller_directory(app.run_dir) / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(saved["conventionWarning"], warning)
        manager = object.__new__(server.Server)
        manager.jobs, manager.jobs_lock = {app.state["runId"]: app}, threading.RLock()
        self.assertTrue(manager.job_list()[0]["loopholeWarning"])
        # A restart shows it again.
        self.assertEqual(self.restored(app).state["conventionWarning"], warning)
        # Ignore: only this warning, by its time.
        with self.assertRaisesRegex(ValueError, "newer loophole warning"):
            app.dismiss_convention_warning("2000-01-01T00:00:00+00:00")
        app.dismiss_convention_warning(warning["time"])
        self.assertTrue(app.snapshot()["conventionWarning"]["dismissed"])
        self.assertFalse(manager.job_list()[0]["loopholeWarning"])
        with self.assertRaisesRegex(ValueError, "no loophole warning"):
            app.dismiss_convention_warning(warning["time"])
        # It stays ignored after a restart.
        restored = self.restored(app)
        self.assertTrue(restored.state["conventionWarning"]["dismissed"])
        # A later round's new loophole verdict shows again, also after a restart.
        self.feed(app, [convention_record(3)])
        self.assertEqual((app.state["conventionWarning"]["round"], app.state["conventionWarning"]["dismissed"]),
                         (3, False))
        self.assertFalse(self.restored(app).state["conventionWarning"]["dismissed"])
        # The latest verdict decides: a faithful verdict on a later accepted proof closes the warning, also
        # after a restart.
        self.feed(app, [convention_record(5, "faithful")])
        self.assertIsNone(app.state["conventionWarning"])
        self.assertFalse(manager.job_list()[0]["loopholeWarning"])
        saved = json.loads((server.controller_directory(app.run_dir) / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertIsNone(saved["conventionWarning"])
        self.assertIsNone(self.restored(app).state["conventionWarning"])

    def test_faithful_and_incomplete_records_raise_no_warning(self):
        app = self.job()
        self.feed(app, [convention_record(1, "faithful"), convention_record(2, loophole=""),
                        convention_record(3, root=False)])
        self.assertIsNone(app.snapshot()["conventionWarning"])
        manager = object.__new__(server.Server)
        manager.jobs, manager.jobs_lock = {app.state["runId"]: app}, threading.RLock()
        self.assertFalse(manager.job_list()[0]["loopholeWarning"])

    def test_a_saved_batch_shows_the_saved_warning_and_tags_it_until_it_was_ignored(self):
        from ui import batches
        root = self.runs / "batches"
        warning = server.convention_warning({**convention_record(2), "time": "2026-10-29T01:02:03+00:00"})
        saved = {"open": warning, "ignored": {**warning, "dismissed": True}, "none": None,
                 "damaged": {**warning, "loophole": 5}}
        for name, value in saved.items():
            directory = root / name / "job"
            directory.mkdir(parents=True)
            (directory / "draft.md").write_text("# Draft problem\n\nA finite theorem.\n")
            (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({"conventionWarning": value}))
        catalog = batches.BatchCatalog(root, current_root=root / "none")
        self.assertEqual([catalog.jobs(name)[0]["loopholeWarning"] for name in saved], [True, False, False, False])
        self.assertEqual(catalog.state("open", "job")["conventionWarning"], warning)
        self.assertTrue(catalog.state("ignored", "job")["conventionWarning"]["dismissed"])
        for name in ("none", "damaged"):
            self.assertIsNone(catalog.state(name, "job")["conventionWarning"])

    def test_the_dismiss_route_is_refused_for_a_job_another_process_owns(self):
        app = self.job()
        self.feed(app, [convention_record(1)])
        app.state["runningElsewhere"] = "Another TCS Prover runs this job."
        with self.assertRaisesRegex(ValueError, "Another TCS Prover"):
            app.dismiss_convention_warning(app.state["conventionWarning"]["time"])

    def test_convention_records_and_critic_progress_stay_pinned(self):
        self.assertTrue(server.important_record(convention_record()))
        self.assertTrue(server.important_record({"kind": "status", "label": "Independent critic 2 finished",
                                                 "auditIndex": 2, "auditCount": 3, "subagentProgress": True}))
        self.assertTrue(server.important_record({"kind": "status", "label": "3 independent critics requested",
                                                 "auditCount": 3, "subagentProgress": True}))
        self.assertFalse(server.important_record({"kind": "status", "label": "Independent critic 1 started",
                                                  "subagentProgress": True, "root": False}))
        self.assertFalse(server.important_record({"kind": "status", "label": "Critic coordinator · Retrying",
                                                  "activityLabel": "Critic coordinator"}))
        self.assertIn("convention_result", server.LASTING_KINDS)

    def test_the_launch_passes_the_diff_folder_and_the_job_convention_prompt(self):
        app = self.job(convention_prompt="Judge the proof of {statement}.")
        argv = self.launches[-1]
        settings = [argv[index + 1] for index, item in enumerate(argv) if item == "--set"]
        folders = [json.loads(item.partition("=")[2]) for item in settings if item.startswith("critic_diff_dir=")]
        self.assertEqual(folders, [str(server.controller_directory(app.run_dir) / server.CRITIC_DIFFS_FOLDER)])
        prompt = Path(argv[argv.index("--convention-prompt-file") + 1])
        self.assertEqual(prompt.read_text(), "Judge the proof of {statement}.\n")
        self.assertTrue(prompt.is_relative_to(server.controller_directory(app.run_dir)))
        self.assertEqual(app.state["conventionPrompt"], "Judge the proof of {statement}.")
        # Restored with the job, like the other per-job prompts.
        app.process = app.active_token = app.worker_token = None
        self.assertEqual(self.restored(app).state["conventionPrompt"], "Judge the proof of {statement}.")

    def test_a_convention_prompt_the_workflow_cannot_fill_is_refused_before_launch(self):
        with self.assertRaisesRegex(ValueError, "convention critic prompt cannot be used"):
            server.App._workflow_options(convention_prompt="Judge {proof}.", include_review=False)
        # Without the critic stage it is not used, so not checked.
        server.App._workflow_options(convention_prompt="Judge {proof}.", include_review=False,
                                     workflow_stages={"review": False, "author": True, "critic": False,
                                                      "writing": True})
        self.assertEqual(server.default_prompts()["convention"], PROMPT)
        self.assertEqual(server.PROMPT_ROLES["convention"], "critic")

    def test_a_saved_long_diff_can_be_downloaded_and_nothing_else(self):
        app = self.job()
        folder = server.controller_directory(app.run_dir) / server.CRITIC_DIFFS_FOLDER
        folder.mkdir(parents=True)
        name = "round-2-20261029T010203Z-0123abcd.diff"
        (folder / name).write_text("--- submitted proof\n+++ coordinator's proof\n")
        self.assertEqual(app.critic_diff(name), b"--- submitted proof\n+++ coordinator's proof\n")
        for bad in ("../job-settings.json", "round-2.diff", "", "round-2-20261029T010203Z-0123abcd.diff/x"):
            with self.subTest(bad), self.assertRaises(ValueError):
                app.critic_diff(bad)

    def test_the_gateway_passes_the_ignore_and_the_diff_download_to_the_updated_controller(self):
        from ui import live_gateway
        self.assertIn("/dismiss-convention-warning", live_gateway.UPDATED_CONTROL_ROUTES)
        self.assertIn("/dismiss-convention-warning", live_gateway.CONTROL_ROUTES)
        self.assertEqual(live_gateway.GET_ROUTES["/download-critic-diff"], {"job", "file"})


class RouteTests(ServerCase):
    """The convention critic's routes on a real local Server with its session token."""

    def setUp(self):
        super().setUp()
        from runner import claude_cli
        for target, name in ((runtime, "goal_session"), (claude_cli, "goal_session"),
                             (runtime, "author_conversation"), (claude_cli, "author_conversation")):
            patcher = mock.patch.object(target, name, side_effect=AssertionError(f"{name} must not run here"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)
        self.app = self.job()
        self.feed(self.app, [convention_record(2)])
        self.run_id = self.app.state["runId"]
        with self.http.jobs_lock:
            self.http.jobs[self.run_id] = self.app
        self.diffs = server.controller_directory(self.app.run_dir) / server.CRITIC_DIFFS_FOLDER
        self.diffs.mkdir(parents=True)
        self.name = "round-2-20261029T010203Z-0123abcd.diff"
        self.diff = "--- submitted proof\n+++ coordinator's proof\n@@ -1 +1,2 @@\n Step 1.\n+Case n = 0.\n".encode()
        (self.diffs / self.name).write_bytes(self.diff)

    def call(self, path, body=None, token=True):
        """(status, headers, body bytes) of one request, with the session token or without it."""

        import http.client
        headers = {"Origin": self.http.origin, **({"X-TCS-Prover-Token": self.http.token} if token else {})}
        raw = None
        if body is not None:
            raw, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=20)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_the_long_diff_download_needs_the_token_and_serves_only_saved_diffs(self):
        path = f"/download-critic-diff?job={self.run_id}&file={self.name}"
        status, _, body = self.call(path, token=False)
        self.assertEqual((status, json.loads(body)), (403, {"error": "Open TCS Prover from its launch URL."}))
        status, headers, body = self.call(path)
        self.assertEqual((status, body), (200, self.diff))
        self.assertEqual(headers["Content-Disposition"], f'attachment; filename="critic-changes-{self.name}"')
        self.assertEqual(headers["Content-Type"], "text/x-diff; charset=utf-8")
        for bad in ("../job-settings.json", "round-2.diff", "", f"{self.name}/x"):
            with self.subTest(bad=bad):
                status, _, body = self.call(f"/download-critic-diff?job={self.run_id}&file={quote(bad, safe='')}")
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"error": "Unknown saved changes."})
        # A saved diff replaced by a link is never followed.
        secret = self.runs.parent / "secret.txt"
        secret.write_text("not for download")
        linked = "round-3-20261029T010203Z-0123abcd.diff"
        (self.diffs / linked).symlink_to(secret)
        status, _, body = self.call(f"/download-critic-diff?job={self.run_id}&file={linked}")
        self.assertEqual((status, json.loads(body)), (400, {"error": "These saved changes are no longer available."}))

    def test_ignore_dismisses_the_warning_for_every_page_and_needs_the_token(self):
        warning = self.app.state["conventionWarning"]
        status, _, body = self.call(f"/dismiss-convention-warning?job={self.run_id}", {"time": warning["time"]},
                                    token=False)
        self.assertEqual((status, json.loads(body)), (403, {"error": "Unauthorized local request."}))
        self.assertFalse(self.app.state["conventionWarning"]["dismissed"])
        status, _, listing = self.call("/jobs")
        self.assertEqual([job["loopholeWarning"] for job in json.loads(listing)["jobs"]], [True])
        status, _, body = self.call(f"/dismiss-convention-warning?job={self.run_id}", {"time": warning["time"]})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["conventionWarning"]["dismissed"])
        status, _, listing = self.call("/jobs")
        self.assertEqual([job["loopholeWarning"] for job in json.loads(listing)["jobs"]], [False])
        # Sent again (a double click): refused plainly.
        status, _, body = self.call(f"/dismiss-convention-warning?job={self.run_id}", {"time": warning["time"]})
        self.assertEqual((status, json.loads(body)), (400, {"error": "There is no loophole warning to ignore."}))


class TranscriptReaderTests(unittest.TestCase):
    def test_the_narrative_shows_what_the_critic_fixed_and_the_convention_verdict(self):
        from transcript import view_transcript

        output = io.StringIO()
        viewer = view_transcript.TranscriptViewer(output)
        for record in (
                {"kind": "critic_result", "stage": "critic", "label": "Critic round 1", "time": "2026-10-29T01:00:00Z",
                 "outcome": "fixed", "report": {"verdict": "pass", "bugs": "", "fixes": "FIXED-TEXT", "solution": "P"}},
                {"kind": "convention_result", "stage": "critic", "label": "Convention critic · round 2",
                 "time": "2026-10-29T01:01:00Z", "round": 2, "verdict": "loophole", "intended": "INTENDED-TEXT",
                 "assessment": "ASSESSMENT-TEXT", "loophole": "LOOPHOLE-TEXT", "suggestedStatement": "SUGGESTED-TEXT"}):
            viewer.consume(record)
        viewer.finish()
        text = output.getvalue()
        self.assertIn("Fixed:\n  FIXED-TEXT", text)
        self.assertIn("Convention critic · round 2 — LOOPHOLE", text)
        for words in ("INTENDED-TEXT", "ASSESSMENT-TEXT", "How it exploits the statement:", "LOOPHOLE-TEXT",
                      "Suggested statement:", "SUGGESTED-TEXT"):
            self.assertIn(words, text)


# A fixture of one job page: critic rounds (the critic and the progress of its three subagents) and the convention
# critic after the accepting round.
UI_FIXTURE = r"""
const status = (label, time, extra = {}) => ({kind: 'status', stage: 'critic', node: 'critic', label, time, text: label, ...extra});
const progress = (label, time, extra = {}) => ({kind: 'status', stage: 'critic', label, time, subagentProgress: true, auditCount: 3, ...extra});
const coordinator = (tag) => ({kind: 'request', stage: 'critic', label: 'Critic coordinator', attempt: 1, time: tag + '-h',
  text: 'COORDINATOR-INPUT ' + tag});
const visit = (tag, number, verify) => [
  {kind: 'status', stage: 'critic', node: 'critic', label: 'Workflow step: critic', visit: number, time: tag + '-a', ...(verify ? {verify} : {})},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: number, time: tag + '-b'},
  coordinator(tag), progress('3 independent critics requested', tag + '-c', {attempt: 1})];
const conventionStep = (tag, number, verdict) => [
  {kind: 'status', stage: 'critic', node: 'convention', label: 'Workflow step: convention', visit: 1, time: tag + '-k'},
  {kind: 'proof_handoff', stage: 'critic', label: 'Submitted proof captured', round: 1, time: tag + '-l'},
  {kind: 'request', stage: 'critic', label: 'Convention critic', attempt: 1, time: tag + '-m', text: 'You are the convention critic.'},
  ...(verdict ? [{kind: 'convention_result', stage: 'critic', label: `Convention critic · round ${number}`, round: number, verdict,
    intended: 'INTENDED-TEXT', assessment: 'ASSESSMENT-TEXT', time: tag + '-n',
    loophole: verdict === 'loophole' ? 'LOOPHOLE-TEXT' : '', suggestedStatement: verdict === 'loophole' ? 'SUGGESTED-TEXT' : ''}] : [])];
const result = (tag, number, report, outcome) => ({kind: 'critic_result', stage: 'critic', label: `Critic round ${number}`,
  round: number, outcome, time: tag + '-i', text: 'RAW-DECISION ' + tag, report: {bugs: '', fixes: '', solution: 'P', ...report}});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class InterfaceTests(unittest.TestCase):
    def run_page(self, checks, slices=(("// Group the recorded critic calls", "function renderRetrievalOverview()"),)):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const slices = JSON.parse(process.argv[3]);
const code = slices.map(([start, end]) => {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  if (from < 0 || to < 0) throw new Error('missing marker ' + start);
  return source.slice(from, to);
}).join('\n');
const all = node => [node, ...node.children.flatMap(all)];
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.open = false; this.hidden = false; this.classList = {add: () => {}}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  querySelectorAll(tag) { return all(this).slice(1).filter(node => node.tagName === tag); }
  querySelector() { return null; }
}
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
const ui = new Proxy({criticReviewHistory: new Element('details'), workflowNodes: new Element('ol')},
  {get: (target, name) => target[name] ||= new Element('p')});
const state = {runId: 'run-a', phase: 'running', activeNode: 'critic', criticCount: 3, trace: []};
const show = (element, visible) => { element.hidden = !visible; };
const context = {assert, ui, state, text, all, show, Element, document: {createElement: tag => new Element(tag)},
  localDateTime: value => value, downloadArtifact: () => {}, nodeFromStage: stage => stage === 'critic' ? 'critic' : stage};
vm.runInNewContext(code + process.argv[2] + process.argv[4], context);
"""
        completed = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js"), UI_FIXTURE,
                                    json.dumps(slices), checks], capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_history_shows_the_one_decision_its_fixes_bugs_and_changes(self):
        self.run_page(r"""
state.trace = [...visit('r1', 1), progress('Independent critic 1 started', 'r1-d1', {auditIndex: 1}),
  progress('Independent critic 2 started', 'r1-d2', {auditIndex: 2}), progress('Independent critic 1 finished', 'r1-d3', {auditIndex: 1})];
renderCriticReviewHistory();
const rounds = () => all(ui.criticReviewHistory).filter(node => node.className === 'critic-review');
// While the round runs: the small progress line, without any subagent text.
let round = text(rounds()[0]);
assert.match(round, /The critic is reviewing the proof\./);
assert.match(round, /Critic 1 of 3 finished · Critic 2 of 3 working/);
assert.doesNotMatch(round, /Independent critics \(3\)|Focus/);
assert.match(text(ui.criticReviewHistory), /the critic starts 3 independent critics as its subagents, receives all 3 reports and makes the one decision shown here/);
// A job from before the Author subagents setting ran its independent critics itself, before its coordinator.
state.authorSubagentsRecorded = false;
renderCriticReviewHistory();
assert.match(text(ui.criticReviewHistory), /In each round 3 independent critics reviewed the proof, and the critic coordinator received all their reports and made the one decision shown here/);
assert.doesNotMatch(text(ui.criticReviewHistory), /as its subagents/);
delete state.authorSubagentsRecorded;
// Round 1 fixes the proof; round 2 is the final check and passes clean, then the convention critic checks the
// accepted proof; a new cycle is rejected.
state.trace = [...visit('r1', 1),
  result('r1', 1, {verdict: 'pass', fixes: 'FIXED-TEXT: expanded step 2.', proofChanged: true,
    proofDiff: '--- submitted proof\n+++ coordinator\'s proof\n@@ -1 +1,2 @@\n A\n+DIFF-ADDED-LINE', proofDiffLines: 5,
    checks: [{focus: 'F', verdict: 'fail', report: 'SUBAGENT-REPORT-TEXT'}]}, 'fixed'),
  ...visit('r2', 2, 'critic_verify'),
  // A coordinator may still write something in fixes for an unchanged proof: it is not shown.
  result('r2', 2, {verdict: 'pass', fixes: 'NO-EDITS-NEEDED', proofChanged: false, proofDiff: ''}, 'pass'),
  status('Critic approved', 'r2-j', {acceptanceReason: 'clean_pass', text: 'Round 2 passed without edits.'}),
  ...conventionStep('r2', 2, 'loophole'),
  {kind: 'request', stage: 'repair', time: 'r3-0'},
  ...visit('r3', 1),
  result('r3', 1, {verdict: 'reject', bugs: 'CRITICAL-BUG at Lemma 2.', fixes: 'SAFE-FIX in Lemma 1.', proofChanged: true,
    proofDiff: '--- submitted proof\n+++ coordinator\'s proof\n+SAFE'}, 'reject')];
state.phase = 'done';
renderCriticReviewHistory();
const [fixed, clean, rejected] = rounds().map(text);
assert.equal(rounds().length, 3);
assert.match(fixed, /Coordinator decision Pass with fixes Another review round follows: the final check, which cannot edit the proof\./);
assert.match(fixed, /Fixed: FIXED-TEXT: expanded step 2\./);
assert.match(fixed, /Show changes to the proof \(5 diff lines\) [^]*\n\+DIFF-ADDED-LINE/);
assert.doesNotMatch(fixed, /Critical bugs/);
// The subagents' own outputs are not shown, even when an older record carries them.
assert.doesNotMatch(fixed, /SUBAGENT-REPORT-TEXT|Critic 1 of/);
assert.match(clean, /Clean pass The proof is unchanged\./);
assert.doesNotMatch(clean, /Show changes|Fixed:|NO-EDITS-NEEDED/);
// The convention critic belongs to the accepting round.
assert.match(clean, /Convention critic .*Loophole: the proof may exploit a loophole .*INTENDED-TEXT .*ASSESSMENT-TEXT .*How it exploits the statement: LOOPHOLE-TEXT .*Suggested statement: SUGGESTED-TEXT/);
assert.doesNotMatch(fixed, /Convention critic/);
assert.match(rejected, /Rejected The proof returns to the author with the critical bugs\./);
assert.match(rejected, /Fixed: SAFE-FIX in Lemma 1\. .*Critical bugs: CRITICAL-BUG at Lemma 2\./);
assert.match(rejected, /Show changes to the proof/);
// The coordinator's exact input and its raw decision stay available below, collapsed.
assert.match(fixed, /Coordinator's exact input COORDINATOR-INPUT r1 .*Coordinator's raw decision RAW-DECISION r1/);
assert(all(ui.criticReviewHistory).filter(node => node.tagName === 'details').every(node => !node.open));
// The convention step's records never open a critic round.
assert.equal(text(ui.criticReviewHistory).match(/Review cycle \d/g).length, 2);
""")

    def test_a_faithful_verdict_shows_under_the_accepting_round_and_a_running_one_says_so(self):
        self.run_page(r"""
state.phase = 'running'; state.activeNode = 'convention';
state.trace = [...visit('r1', 1), result('r1', 1, {verdict: 'pass', proofChanged: false, proofDiff: ''}, 'pass'),
  status('Critic approved', 'r1-j', {acceptanceReason: 'clean_pass', text: 'Round 1 passed without edits.'}),
  ...conventionStep('r1', 1, null)];
renderCriticReviewHistory();
let round = text(all(ui.criticReviewHistory).find(node => node.className === 'critic-review'));
assert.match(round, /Clean pass .*Convention critic .*Checking the accepted proof\./);
state.trace = [...state.trace, ...conventionStep('r1', 1, 'faithful').slice(-1)];
state.phase = 'done';
renderCriticReviewHistory();
round = text(all(ui.criticReviewHistory).find(node => node.className === 'critic-review'));
assert.match(round, /Faithful: the proof does what the user most likely wants\. .*What the user most likely wants: INTENDED-TEXT .*How the proof relates to it: ASSESSMENT-TEXT/);
assert.doesNotMatch(round, /LOOPHOLE-TEXT|Suggested statement/);
""")

    def test_long_diffs_offer_the_download_and_version_2_streams_show_the_wait(self):
        self.run_page(r"""
state.trace = [...visit('n1', 1), progress('Waiting for the independent critics', 'n1-w', {waiting: true})];
renderCriticReviewHistory();
// Where only the wait is shown, the line says that the critic started them, not that it still waits.
assert.match(text(ui.criticReviewHistory), /The critic has started its independent critics \(3 requested\)/);
assert.doesNotMatch(text(ui.criticReviewHistory), /Waiting for/);
state.phase = 'done';
state.trace = [...visit('n1', 1), result('n1', 1, {verdict: 'pass', fixes: 'Expanded everything.', proofChanged: true,
  proofDiff: '', proofDiffFile: 'round-1-20261029T010203Z-0123abcd.diff',
  proofDiffNote: 'The coordinator\'s changes are 90,000 diff lines, too long to show here.'}, 'fixed')];
renderCriticReviewHistory();
const round = all(ui.criticReviewHistory).find(node => node.className === 'critic-review');
assert.match(text(round), /too long to show here/);
const button = all(round).find(node => node.className?.includes('critic-diff-download'));
assert.equal(button.textContent, 'Download the changes');
assert.equal(button.dataset.diffFile, 'round-1-20261029T010203Z-0123abcd.diff');
""")

    def test_the_activity_card_of_a_critic_result_shows_the_same_round(self):
        self.run_page(r"""
state.trace = [...visit('r1', 1), result('r1', 1, {verdict: 'pass', proofChanged: false, proofDiff: ''}, 'pass'),
  status('Critic approved', 'r1-j', {acceptanceReason: 'clean_pass', text: 'Round 1 passed without edits.'}),
  ...conventionStep('r1', 1, 'faithful')];
const card = describe(state.trace.find(entry => entry.kind === 'critic_result'));
assert.match(card.label, /Review cycle 1 · round 1/);
assert.equal(card.checks, undefined);
const parts = new Element('div');
parts.append(...criticRoundParts(card.critic.round, card.critic.next, (key, label, content) => {
  const detail = new Element('details'); detail.textContent = label;
  if (content !== undefined) { const copy = new Element('pre'); copy.textContent = content; detail.append(copy); }
  return detail;
}));
const shown = text(parts);
assert.match(shown, /Coordinator decision Clean pass/);
assert.match(shown, /Convention critic .*Faithful/);
const record = describe(state.trace.find(entry => entry.kind === 'convention_result'));
assert.match(record.label, /Convention critic · round 1 · Faithful/);
assert.match(record.text, /What the user most likely wants: INTENDED-TEXT/);
""", slices=(("// Group the recorded critic calls", "function renderRetrievalOverview()"),
             ("function describe(entry)", "function cardIcon(")))

    def test_the_rail_shows_the_convention_step_and_the_critics_progress(self):
        self.run_page(r"""
state.workflow = {nodes: {statement_reviewer: {label: 'Statement reviewer', stage: 'review'},
  author: {label: 'Proof author', stage: 'solve'}, critic: {label: 'Independent critic', stage: 'critic'},
  convention: {label: 'Convention critic', stage: 'critic'}, latex_editor: {label: 'LaTeX editor', stage: 'final'},
  failure_summary: {label: 'Saved progress', stage: 'failure'}}};
state.workflowStages = {review: false, author: true, critic: true, writing: true};
state.criticRounds = 2; state.skipStatementReview = true;
state.trace = [...visit('r1', 1), progress('Independent critic 1 started', 'r1-d1', {auditIndex: 1})];
// The server's count of those records (follow_critic_progress), which the rail's Now mark reads.
state.criticProgress = {count: 3, requested: true, started: [1], finished: [], failed: [], waiting: false};
renderWorkflow();
let rail = text(ui.workflowNodes);
assert.match(rail, /Independent critic .*Now Critic 1 of 3 working/);
assert.match(rail, /Convention critic .*Checks once whether the accepted proof does what you most likely want/);
const order = all(ui.workflowNodes).filter(node => node.dataset?.node).map(node => node.dataset.node);
assert.equal(order.filter(name => ['critic', 'convention', 'latex_editor'].includes(name)).join(), 'critic,convention,latex_editor');
// Finished: the convention step says its verdict.
state.phase = 'done'; state.activeNode = 'latex_editor'; state.criticProgress = null;
state.trace = [...visit('r1', 1), result('r1', 1, {verdict: 'pass', proofChanged: false, proofDiff: ''}, 'pass'),
  status('Critic approved', 'r1-j', {acceptanceReason: 'clean_pass', text: 'Round 1 passed without edits.'}),
  ...conventionStep('r1', 1, 'loophole')];
// The warning shows on the page: the step points to it.
state.conventionWarning = {round: 1, time: 'r1-n', loophole: 'LOOPHOLE-TEXT', suggestedStatement: 'S', dismissed: false};
renderWorkflow();
rail = text(ui.workflowNodes);
assert.match(rail, /Convention critic .*May exploit a loophole: see the warning above/);
assert.doesNotMatch(rail, /working/);
// Ignored, or not kept (an older saved job): no warning is shown, so the step points to the history.
state.conventionWarning = {...state.conventionWarning, dismissed: true};
renderWorkflow();
assert.match(text(ui.workflowNodes), /Convention critic .*May exploit a loophole \(warning ignored\); see the critic review history/);
state.conventionWarning = null;
renderWorkflow();
assert.match(text(ui.workflowNodes), /Convention critic .*May exploit a loophole; see the critic review history/);
assert.doesNotMatch(text(ui.workflowNodes), /warning above/);
// Without the writing stage the convention step is still listed after the critic, and the run ends there.
state.workflowStages = {review: false, author: true, critic: true, writing: false};
state.activeNode = 'convention'; state.resultKind = 'proof';
renderWorkflow();
const steps = all(ui.workflowNodes).filter(node => node.dataset?.node);
assert.equal(steps.map(node => node.dataset.node).join(),
  'statement_reviewer,author,critic,convention,latex_editor,latex_compile,failure_summary');
const convention = steps.find(node => node.dataset.node === 'convention');
assert.match(text(convention), /Ended here Accepted by the critic; writing not selected/);
assert.doesNotMatch(text(convention), /Not selected/);
assert.match(text(steps.find(node => node.dataset.node === 'latex_editor')), /Not selected for this run/);
// Without the critic stage the convention critic does not run either.
state.workflowStages = {review: false, author: true, critic: false, writing: true};
state.phase = 'running'; state.activeNode = 'author'; state.resultKind = '';
renderWorkflow();
assert.match(text(all(ui.workflowNodes).find(node => node.dataset?.node === 'convention')), /Not selected for this run/);
""", slices=(("// Group the recorded critic calls", "function renderRetrievalOverview()"),
             ("function completedRunLabel(", "function localDateTime("),
             ("// Show the author and critic/repair cycle.", "function renderClock()")))

    # The rail's steps, as the job page shows them (see test_the_rail_shows_the_convention_step_and_the_critics_progress).
    RAIL = r"""
state.workflow = {nodes: {statement_reviewer: {label: 'Statement reviewer', stage: 'review'},
  author: {label: 'Proof author', stage: 'solve'}, critic: {label: 'Independent critic', stage: 'critic'},
  convention: {label: 'Convention critic', stage: 'critic'}, latex_editor: {label: 'LaTeX editor', stage: 'final'},
  latex_compile: {label: 'Compile PDF', stage: 'final'}, failure_summary: {label: 'Saved progress', stage: 'failure'}}};
state.workflowStages = {review: false, author: true, critic: true, writing: true};
state.criticRounds = 2; state.skipStatementReview = true;
const node = name => all(ui.workflowNodes).find(item => item.dataset?.node === name);
const note = name => all(node(name)).find(item => item.className === 'node-status')?.textContent || '';
const accepted = verdict => [...visit('r1', 1), result('r1', 1, {verdict: 'pass', proofChanged: false, proofDiff: ''}, 'pass'),
  status('Critic approved', 'r1-j', {acceptanceReason: 'clean_pass', text: 'Round 1 passed without edits.'}),
  ...conventionStep('r1', 1, verdict)];
"""
    RAIL_SLICES = (("// Group the recorded critic calls", "function renderRetrievalOverview()"),
                   ("function completedRunLabel(", "function localDateTime("),
                   ("// Show the author and critic/repair cycle.", "function renderClock()"))

    def test_a_paused_or_stopping_round_says_that_nothing_works_on_it(self):
        self.run_page(r"""
state.activeNode = 'critic'; state.stage = 'critic'; state.pauseCause = 'user';
// Paused after critic 1 finished and critic 2 started.
state.trace = [...visit('r1', 1), progress('Independent critic 1 started', 'r1-d1', {auditIndex: 1}),
  progress('Independent critic 2 started', 'r1-d2', {auditIndex: 2}), progress('Independent critic 1 finished', 'r1-d3', {auditIndex: 1}),
  {kind: 'workflow_paused', stage: 'critic', node: 'critic', label: 'Workflow paused', time: 'r1-p'}];
const round = () => text(all(ui.criticReviewHistory).find(item => item.className === 'critic-review'));
for (const [phase, said] of [
    ['paused', /Paused before the critic decided\. On Resume, the round starts again with fresh independent critics\./],
    ['pausing', /Pausing before the critic decided\. On Resume, the round starts again with fresh independent critics\./],
    ['stopping', /Stopping before the critic decided\. The round ends without a decision\./]]) {
  state.phase = phase;
  renderCriticReviewHistory();
  assert.match(round(), said);
  assert.doesNotMatch(round(), /reviewing the proof|working|Critic \d of 3/);
}
state.phase = 'running';
renderCriticReviewHistory();
assert.match(round(), /The critic is reviewing the proof\. .*Critic 1 of 3 finished · Critic 2 of 3 working/);
// Its failed call waits to retry: those critics ended with it, and the retry starts them again.
state.modelRetry = {kind: 'model', stage: 'critic', retryAt: 9e9, since: '2026-10-29T10:00:00Z'};
renderCriticReviewHistory();
assert.match(round(), /The critic's model call failed; the round is retried\. Waiting to retry a failed model call; its 3 independent critics start again with it/);
assert.doesNotMatch(round(), /Critic 2 of 3 working|reviewing the proof/);
delete state.modelRetry;
// A critic beyond those requested is named as such.
state.trace = [...visit('r1', 1), ...[1, 2, 3, 4].map(index => progress(`Independent critic ${index} started`, `r1-s${index}`,
  {auditIndex: index})), ...[1, 2, 3].map(index => progress(`Independent critic ${index} finished`, `r1-f${index}`, {auditIndex: index}))];
renderCriticReviewHistory();
assert.match(round(), /Critic 3 of 3 finished · Critic 4 \(beyond the 3 requested\) working/);
assert.doesNotMatch(round(), /4 of 3/);
// The convention critic that has not answered yet, by phase.
state.activeNode = 'convention';
state.trace = [...visit('r1', 1), result('r1', 1, {verdict: 'pass', proofChanged: false, proofDiff: ''}, 'pass'),
  status('Critic approved', 'r1-j', {acceptanceReason: 'clean_pass', text: 'Round 1 passed without edits.'}),
  ...conventionStep('r1', 1, null)];
for (const [phase, said] of [['running', /Convention critic .*Checking the accepted proof\./],
    ['paused', /Paused before the convention critic finished; it checks the accepted proof again on Resume\./],
    ['stopping', /Stopping before the convention critic finished\./],
    ['done', /Check interrupted: the convention critic did not finish\./]]) {
  state.phase = phase;
  renderCriticReviewHistory();
  assert.match(round(), said);
  if (phase !== 'running') assert.doesNotMatch(round(), /Checking the accepted proof/);
}
""")

    def test_a_paused_critic_step_names_its_open_round_not_an_earlier_verdict(self):
        self.run_page(self.RAIL + r"""
state.activeNode = 'critic'; state.stage = 'critic'; state.pauseCause = 'user';
const author = tag => ({kind: 'author_result', node: 'author', time: tag});
for (const [trace, open] of [
    [[author('a1'), ...visit('r1', 1), progress('Independent critic 1 started', 'r1-d1', {auditIndex: 1})], 'cycle 1, round 1'],
    [[author('a1'), ...visit('r1', 1), result('r1', 1, {verdict: 'pass', fixes: 'F', proofChanged: true, proofDiff: 'x'}, 'fixed'),
      ...visit('r2', 2, 'critic_verify')], 'cycle 1, round 2'],
    [[author('a1'), ...visit('r1', 1), result('r1', 1, {verdict: 'reject', bugs: 'B', proofChanged: false, proofDiff: ''}, 'reject'),
      {kind: 'request', stage: 'repair', node: 'author', time: 'q1'}, author('a2'), ...visit('r3', 1)], 'cycle 2, round 1']]) {
  for (const phase of ['paused', 'pausing']) {
    state.phase = phase; state.trace = trace;
    renderWorkflow();
    assert.equal(note('critic'), `Candidate under review · ${open}`);
    assert.doesNotMatch(note('critic'), /Verdict unavailable|needs repair|another review/);
  }
}
// A paused convention step gives no earlier verdict either: its mark says where the run paused.
state.activeNode = 'convention'; state.phase = 'paused'; state.trace = accepted(null);
renderWorkflow();
assert.equal(note('convention'), '');
assert.match(text(node('convention')), /Paused here/);
""", slices=self.RAIL_SLICES)

    def test_a_job_stopped_in_the_convention_step_ends_there(self):
        self.run_page(self.RAIL + r"""
state.phase = 'done'; state.activeNode = 'convention'; state.manuallyStopped = true; state.stoppedStage = 'critic';
state.error = 'Stopped.'; state.trace = accepted(null);
renderWorkflow();
assert.match(text(node('convention')), /Ended here Stopped by you/);
assert.match(note('convention'), /Check interrupted/);
assert.doesNotMatch(text(node('critic')), /Ended here/);
assert.match(text(node('critic')), /Clean pass — proof accepted/);
// Stopped in the critic step itself, the mark stays there.
state.activeNode = 'critic'; state.trace = [...visit('r1', 1)];
renderWorkflow();
assert.match(text(node('critic')), /Ended here Stopped by you/);
assert.doesNotMatch(text(node('convention')), /Ended here/);
""", slices=self.RAIL_SLICES)

    def test_a_job_from_before_the_convention_critic_shows_it_did_not_run(self):
        self.run_page(self.RAIL + r"""
state.phase = 'done'; state.activeNode = 'latex_compile'; state.resultKind = 'final';
state.canDownloadTex = true; state.canDownloadPdf = true;
const older = [{kind: 'author_result', node: 'author', time: 'a1'}, ...visit('r1', 1),
  result('r1', 1, {verdict: 'pass', proofChanged: false, proofDiff: ''}, 'pass'),
  status('Critic approved', 'r1-j', {acceptanceReason: 'clean_pass', text: 'Round 1 passed without edits.'}),
  {kind: 'status', stage: 'final', node: 'latex_editor', label: 'Workflow step: latex_editor', time: 'w1'},
  {kind: 'status', stage: 'final', node: 'latex_compile', label: 'Workflow step: latex_compile', time: 'w2'}];
state.trace = older;
renderWorkflow();
assert.match(node('convention').className, /disabled/);
assert.equal(node('convention').children[0].textContent, '–');
assert.equal(note('convention'), 'Not run (older job)');
// Without writing it ended at the critic.
state.workflowStages = {review: false, author: true, critic: true, writing: false};
state.activeNode = 'critic'; state.resultKind = 'proof'; state.canDownloadTex = false; state.trace = older.slice(0, -2);
renderWorkflow();
assert.equal(note('convention'), 'Not run (older job)');
// A current job's convention step ran: its verdict, not this note.
state.trace = accepted('faithful');
renderWorkflow();
assert.equal(note('convention'), 'Faithful to the statement');
assert.doesNotMatch(node('convention').className, /disabled/);
// A job that has not reached the step yet, or ended before it, is not said to be older.
state.phase = 'running'; state.activeNode = 'critic'; state.resultKind = ''; state.trace = [...visit('r1', 1)];
renderWorkflow();
assert.equal(note('convention'), '');
state.phase = 'done'; state.resultKind = 'needs-revision';
renderWorkflow();
assert.equal(note('convention'), '');
""", slices=self.RAIL_SLICES)

    def test_the_loophole_warning_shows_until_it_is_ignored(self):
        self.run_page(r"""
state.conventionWarning = {round: 2, time: '2026-10-29T01:02:03Z', loophole: 'LOOPHOLE-TEXT',
  suggestedStatement: 'SUGGESTED-TEXT', intended: 'I', assessment: 'A', dismissed: false};
renderConventionWarning();
assert.equal(ui.conventionWarning.hidden, false);
assert.equal(ui.conventionWarningLoophole.textContent, 'LOOPHOLE-TEXT');
assert.equal(ui.conventionWarningStatement.textContent, 'SUGGESTED-TEXT');
assert.match(ui.conventionWarningRound.textContent, /Found in critic round 2/);
assert.equal(ui.conventionWarningIgnore.hidden, false);
// A saved batch is read-only: the warning shows, without Ignore.
state.readOnly = true;
renderConventionWarning();
assert.equal(ui.conventionWarningIgnore.hidden, true);
state.conventionWarning = {...state.conventionWarning, dismissed: true};
renderConventionWarning();
assert.equal(ui.conventionWarning.hidden, true);
state.conventionWarning = null;
renderConventionWarning();
assert.equal(ui.conventionWarning.hidden, true);
""", slices=(("function renderConventionWarning(", "// Group the recorded critic calls"),))

    def test_the_prompt_editor_lists_and_checks_the_convention_prompt(self):
        self.run_page(r"""
assert.equal(promptLabels.convention, 'Convention critic prompt');
assert.match(promptHelp.convention, /once the critic has accepted it, before writing/);
let stages = {review: false, author: true, critic: true, writing: true};
const selectedWorkflowStages = () => stages;
promptDrafts = {author: 'Prove [STATEMENT].', critic: 'Check.', final: 'Write.', convention: 'Judge {statement}; {{x}}.'};
assert.equal(promptProblem(), null);
promptDrafts.convention = 'Judge {proof}.';
assert.deepEqual(promptProblem()[0], 'convention');
promptDrafts.convention = ' ';
assert.deepEqual(promptProblem(), ['convention', 'Every role prompt must contain instructions.']);
// Without the critic stage the convention critic does not run: its prompt is not checked.
stages = {...stages, critic: false};
assert.equal(promptProblem(), null);
""", slices=(("const promptLabels =", "function updatePromptHelp()"),
             ("// The first draft that cannot be applied", "// The dialog is modal")))

    def test_rerun_opens_the_composer_with_the_suggestion_the_jobs_settings_and_the_author_on(self):
        self.run_page(r"""
let composerSeedOpened = 0;
const newProblem = async () => { composerSeedOpened += 1; };
Object.assign(state, {runId: 'job-1', criticModel: 'claude-opus-5-5', criticEffort: 'high', criticCount: 5,
  authorSubagents: 2, criticRounds: 4, thinkingHours: 2, retrievalMode: 'live', problemMode: 'critic-resume',
  workflowStages: {review: false, author: false, critic: true, writing: false},
  criticPrompt: 'JOB CRITIC PROMPT', authorPrompt: 'DEFAULT AUTHOR', conventionPrompt: 'DEFAULT CONVENTION'});
rerunWithStatement('SUGGESTED-TEXT');
assert.equal(composerSeedOpened, 1);
assert.equal(composerSeed.statement, 'SUGGESTED-TEXT');
assert.equal(composerSeed.settings.criticModel, 'claude-opus-5-5');
assert.equal(composerSeed.settings.criticCount, 5);
assert.equal(composerSeed.settings.authorSubagents, 2);
// A new statement needs a new proof: the author stage is on, whatever the job selected.
assert.deepEqual(composerSeed.settings.workflowStages, {review: false, author: true, critic: true, writing: false});
assert.equal(composerSeed.settings.problemMode, undefined);
assert.equal(composerSeed.settings.criticPrompt, 'JOB CRITIC PROMPT');
// It starts nothing: no request is made.
""", slices=(("function workflowStagesFor(", "// Read historical mode names"),
             ("const promptLabels =", "function syncPrompts("),
             ("// Settings and statement the composer opens with once", "async function newProblem()")))
        source = (server.UI / "app.js").read_text()
        block = source[source.index('if (phase === "input" && previousPhase !== "input") {'):]
        block = block[:block.index("restoreWorkflowStages(source);")]
        for line in ("const source = seed ? { ...state, ...seed.settings } : state;",
                     "ui.problem.value = seed ? seed.statement :", "setChoice(ui.criticModel, source.criticModel",
                     "ui.criticCount.value = source.criticCount", "ui.authorSubagents.value =",
                     "syncPrompts(source);",
                     "if (prompt && prompt.trim() !== (defaults[name] || \"\").trim()) promptOverrides[name] = prompt.trim();"):
            self.assertIn(line, block)

    def test_the_markup_and_the_jobs_list(self):
        markup = (server.UI / "index.html").read_text()
        box = markup[markup.index('id="conventionWarning"'):markup.index("</section>", markup.index('id="conventionWarning"'))]
        for words in ("Convention critic: the proof may exploit a loophole in the problem statement",
                      "How it exploits the statement:", "Suggested statement:", ">Copy</button>",
                      "Consider rerunning with a statement that says what you want. This is only a warning: the run "
                      "continues as normal.", ">Rerun with suggested statement</button>", ">Ignore</button>"):
            self.assertIn(words, box)
        # Near the top of the job view: right after its header, before the panels.
        self.assertLess(markup.index('id="runAttention"'), markup.index('id="conventionWarning"'))
        self.assertLess(markup.index('id="conventionWarning"'), markup.index('id="reviewPanel"'))
        self.assertIn('data-prompt="convention">Convention critic</button>', markup)
        source = (server.UI / "app.js").read_text()
        jobs = source[source.index("function renderJobs("):source.index("function renderJobs(") + 9000]
        self.assertIn('tag.textContent = "Loophole warning";', jobs)
        self.assertIn('if (job.loopholeWarning)', jobs)
        self.assertIn('act("/dismiss-convention-warning", { time: state.conventionWarning?.time || "" })', source)
        self.assertIn('rerunWithStatement(state.conventionWarning?.suggestedStatement || "")', source)


if __name__ == "__main__":
    unittest.main()
