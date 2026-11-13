"""One proof author prompt is used across launch, lifecycle, and restart."""

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from ui import server


# The round-4 wording of the proof author's prompts, character for character; any change needs the owner's approval.
APPROVED_AUTHOR_PROMPTS = {
    "author": (
        "The following is the statement to prove. Solving it is important to my life, so you must solve it, even if "
        "it looks like a long-standing open problem. Do not give up just because a problem is hard or open: I know "
        "the answer, and I need you to come up with the solution.\n\n"
        "Think deeply and hard. Explore substantially diversified approaches: when one line of attack stalls, try a "
        "genuinely different idea instead of small variations of the same one, and keep track of what you have "
        "ruled out. Submit only a proof you have checked.\n\n"
        "When your proof is complete, write it to proof.md and reply with exactly:\n"
        "PROOF_FILE: proof.md\n"
        "The critic reads only proof.md and the statement, so make the proof complete and self-contained.\n\n"
        "STATEMENT:\n[STATEMENT]"),
    "goal": ("Prove the exact statement given in the initial prompt with a complete, rigorous argument. Do not give "
             "up: think deeply and hard, and try substantially different approaches until the proof is complete."),
    "continuation": ("Keep working on the same statement and do not give up. Think deeply and hard, and try "
                     "approaches substantially different from the ones that failed; I know the statement can be "
                     "proved. When your proof is complete, write it to proof.md and reply with exactly "
                     "PROOF_FILE: proof.md."),
    "reopen": ("Keep working on the same statement and do not give up. Think deeply and hard, and try approaches "
               "substantially different from the ones that failed."),
    "resume": ("Keep working on the statement below, building on any previous work in this conversation or in your "
               "working directory. Do not give up: think deeply and hard, and try substantially different approaches "
               "where earlier ones failed.\n\nORIGINAL AUTHOR PROMPT:\n{original_prompt}"),
    "repair": ("Fix the unresolved errors the critic found in this candidate, then write the complete corrected proof "
               "to proof.md and reply with exactly PROOF_FILE: proof.md. Do not give up on the statement.\n\n"
               "CURRENT CANDIDATE:\n{solution}\n\nCRITIC BUGS:\n{bugs}"),
}

# The default author prompt from 26 Oct (82e29ac) until round 4, character for character: a job saved with it unedited
# gets the current default (server.RETIRED_DEFAULT_PROMPTS).
EARLIER_DEFAULT_AUTHOR_PROMPT = (
    "The following is the statement to prove. Try to prove it with a complete, rigorous argument.\n"
    "\n"
    "On macOS or Linux, when complete, write the entire self-contained proof to\n"
    "proof.md in your working directory and submit exactly PROOF_FILE: proof.md\n"
    "as your final response. Include every argument needed by the critic in that\n"
    "file. The critic receives a read-only snapshot of this file and the exact\n"
    "statement. After a rejection, update proof.md with the revised complete\n"
    "proof and submit it again. A complete inline proof is also accepted.\n"
    "On Windows, return the entire self-contained proof inline in your final\n"
    "response, including after a rejection; do not submit a file pointer.\n"
    "\n"
    "STATEMENT:\n"
    "[STATEMENT]")

# The opening and notebook layout of an earlier research-notebook default author prompt (7b33e62). A job started with
# such a prompt keeps it: its run depends on the notebooks the prompt set up.
NOTEBOOK_AUTHOR_PROMPT = (
    "Resolve the exact statement below with a complete, rigorous proof. Think deeply and consider substantially "
    "diversified approaches.\n\n"
    "You, the author LLM, create, read, search, and update your research records and thinking process using file "
    "tools. Inspect the working directory first. For a new run, create INITIAL_PROMPT.md with this entire initial "
    "author prompt verbatim, including the exact statement and every instruction; create APPROACHES/index.md with "
    "the heading \"# Attempted approaches\" and PROVED.md with \"# Proved lemmas\". Create files only if absent. "
    "For an existing run, read and follow its saved INITIAL_PROMPT.md and memory layout.\n\n"
    "STATEMENT:\n[STATEMENT]")


class AuthorPromptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name)
        self.manager = object.__new__(server.Server)
        self.manager.runs = self.runs
        self.manager.jobs = {}
        self.manager.jobs_lock = threading.RLock()
        self.manager.fixed_app = False
        for change in (patch.object(runtime, "verify_model_credentials"),
                       patch.object(server.App, "_spawn_worker"),
                       patch.object(server.App, "_launch_solver_locked", return_value=(object(), object()))):
            change.start()
            self.addCleanup(change.stop)

    def test_single_author_prompt_survives_restart(self):
        app = self.manager.start_direct_job({
            "statement": "Exact statement",
            "promptOverrides": {"author": "Custom [STATEMENT]"},
        })
        self.assertEqual(app.state["authorPrompt"], "Custom [STATEMENT]")
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["authorPrompt"], app.state["authorPrompt"])

    def test_every_author_lifecycle_uses_the_same_proof_prompt_family(self):
        workflow = runtime.builtin_workflow("author_critic")
        with patch.object(runtime, "require_model_credentials"):
            prompts = runtime.prepare(workflow, {})
        for name in ("author", "goal", "continuation", "compaction", "resume", "repair", "reopen",
                     "summary", "conversation", "related_works", "handoff_refused"):
            self.assertEqual(prompts[name], workflow["prompts"][name])
            for record in ("INITIAL_PROMPT.md", "APPROACHES", "PROVED.md"):
                self.assertNotIn(record, prompts[name])
        self.assertEqual(prompts["author"].count("[STATEMENT]"), 1)
        # The paused author's conversation prompts, the related-works block and the note on a refused
        # submission never name the statement marker; only the conversation prompt quotes the user's
        # message, and only the note the reason the controller refused the submission.
        for name in ("summary", "conversation", "related_works", "handoff_refused"):
            self.assertIn(name, workflow["nodes"]["author"]["lifecycle"])
            self.assertNotIn("[STATEMENT]", prompts[name])
            self.assertEqual("{message}" in prompts[name], name == "conversation")
        self.assertEqual([name for name, text in prompts.items() if "{reason}" in text], ["handoff_refused"])
        # The owner's approved wording, character for character.
        self.assertEqual(prompts["handoff_refused"],
                         "Your last submission was not handed to the critic, so it has not been reviewed:\n"
                         "{reason}\n\nFix this and submit the complete proof again.")
        self.assertFalse(any(name.endswith("_simple") for name in prompts))
        with patch.object(runtime, "require_model_credentials"):
            custom = runtime.prepare(workflow, {"prompts": {"author": "Custom [STATEMENT]"}})
        self.assertEqual(custom["author"], "Custom [STATEMENT]")
        self.assertEqual(custom["resume"], prompts["resume"])

    def test_the_proof_author_prompts_are_the_approved_wording(self):
        prompts = runtime.builtin_workflow("author_critic")["prompts"]
        for name, text in APPROVED_AUTHOR_PROMPTS.items():
            with self.subTest(prompt=name):
                self.assertEqual(prompts[name], text)
        # Only the author's first prompt, continuation and repair, and the coordinator's first prompt and its
        # answer to a reply that is not a hand-in, ask for the file in their own words (the coordinator's
        # other prompts repeat its first prompt; Claude Code's goal note gives it as an example of a final
        # answer, as its code text did), and no prompt names a system: the runner adds the note for a
        # computer that cannot read proof.md (INLINE_PROOF_NOTE).
        for name, text in prompts.items():
            with self.subTest(prompt=name):
                self.assertEqual("PROOF_FILE: proof.md" in text, name in {
                    "author", "continuation", "repair", "coordinator", "coordinator_report", "claude_goal_harness"})
                for word in ("Windows", "macOS", "Linux"):
                    self.assertNotIn(word, text)
        # That note, and the reason a PROOF_FILE reply is refused there, which the resumed author is told.
        self.assertEqual(runtime.INLINE_PROOF_NOTE,
                         "PROOF FORMAT ON THIS COMPUTER:\nThe critic cannot receive proof.md here; it receives your "
                         "reply instead. When your proof is complete, reply with the complete proof itself instead "
                         "of PROOF_FILE: proof.md.")
        self.assertEqual(runtime.INLINE_PROOF_PROMPTS, ("continuation", "repair"))
        self.assertEqual(runtime.proof_handoff.NO_FILE_HANDOFF,
                         "This computer cannot read a proof file safely, so PROOF_FILE does not work here; reply "
                         "with the complete proof itself.")

    def test_a_job_saved_with_the_earlier_default_author_prompt_gets_the_current_one(self):
        # Only that one earlier default is replaced: a research-notebook default, or a prompt the user edited, stays.
        self.assertEqual(server.RETIRED_DEFAULT_PROMPTS["author"], (EARLIER_DEFAULT_AUTHOR_PROMPT,))
        current = server.default_prompts()["author"]
        self.assertEqual(server.current_prompt("author", EARLIER_DEFAULT_AUTHOR_PROMPT + "\n"), current)
        for saved, expected in ((EARLIER_DEFAULT_AUTHOR_PROMPT, current),
                                ("Custom [STATEMENT]", "Custom [STATEMENT]"),
                                (NOTEBOOK_AUTHOR_PROMPT, NOTEBOOK_AUTHOR_PROMPT)):
            with self.subTest(saved=saved[:30]):
                self.assertEqual(server.current_prompt("author", saved), expected)
                app = self.manager.start_direct_job({"statement": "Exact statement"})
                for path in (app.run_dir / "prompts/author.txt",
                             server.controller_directory(app.run_dir) / "prompts/author.txt"):
                    path.write_text(saved + "\n")
                restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
                self.assertEqual(restored.state["authorPrompt"], expected)
                self.assertEqual(server.saved_research_source(app.run_dir)["settings"]["authorPrompt"].strip(),
                                 expected)
                (app.run_dir / runtime.SAVED_CANDIDATE_FILENAME).write_text("Candidate")
                self.assertEqual(server.saved_critic_source(app.run_dir)["author_prompt"].strip(), expected)

    def test_continuing_a_job_saved_with_the_earlier_default_author_prompt_uses_the_current_one(self):
        # Every continuation keeps the job's author prompt, except that the earlier default continues as the current
        # one; an edited prompt stays as it is. Continue on a job stopped with Stop picks it up in the same job
        # (round 4: Stop works like the time limit). Continue proof author and the CLI's --resume-author start a new
        # job on the saved conversation, for a job an earlier version stopped (which ended it) or one whose saved
        # conversation was lost.
        for target in ((server.subprocess, "Popen"), (runtime, "goal_session"), (runtime.claude_cli, "goal_session"),
                       (runtime, "author_conversation"), (runtime.claude_cli, "author_conversation")):
            change = patch.object(*target, side_effect=AssertionError("No model or CLI starts in this test."))
            change.start()
            self.addCleanup(change.stop)
        current = server.default_prompts()["author"]

        def saved_job(prompt, ending):
            """A job saved with ``prompt`` whose author was stopped, or lost its saved conversation, after a restart."""

            app = self.manager.start_direct_job({"statement": "Exact statement"})
            for path in (app.run_dir / "prompts/author.txt",
                         server.controller_directory(app.run_dir) / "prompts/author.txt"):
                path.write_text(prompt + "\n")
            app.state.update(phase="running", stage="solve", activeNode="author")
            if ending == "stopped":
                app.stop()  # In place: it shows Stopped, and Continue picks it up in this job.
            elif ending == "stopped by an earlier version":
                # The stop's own records cannot be written, so it ends the job as earlier versions did: the same
                # legacy marker and record, and no checkpoint to continue in place.
                with patch.object(server.App, "_save_pause", side_effect=OSError("disk full")):
                    app.stop()
                self.assertEqual((app.state["phase"], app.state["error"]), ("done", "Stopped."))
            else:
                process = Mock(stdout=io.StringIO(json.dumps({
                    "kind": "workflow_paused", "stage": "solve", "node": "author", "sessionUnavailable": True,
                    "state": {"statement": "Exact statement", "paused": True},
                    "reason": "Could not resume the saved Claude conversation; no replacement was started.",
                }) + "\n"))
                process.wait.return_value = process.poll.return_value = 1
                token = object()
                app.process, app.active_token, app.worker_token = process, token, token
                app._read_output(process, token)
            restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
            self.assertEqual(restored.state["authorPrompt"], current if prompt == EARLIER_DEFAULT_AUTHOR_PROMPT
                             else prompt)
            plan = server.Server._stopped_continuation_plan(restored)
            self.assertEqual((plan["action"], plan["label"]), ("resume", "Continue") if ending == "stopped"
                             else ("author", "Continue proof author"))
            self.manager.jobs = {restored.state["runId"]: restored}
            return restored

        def assert_launched_with(app, prompt, options=None):
            """The job runs ``prompt``: its state, its saved copies, and the file the runner reads (``options``, the
            launch's own options; by default the ones its next launch gets)."""

            self.assertEqual(app.state["authorPrompt"], prompt)
            options = app._proof_options_locked() if options is None else options
            for path in (app.run_dir / "prompts/author.txt",
                         server.controller_directory(app.run_dir) / "prompts/author.txt"):
                self.assertEqual(path.read_text(), prompt + "\n")
            self.assertEqual(Path(options[options.index("--author-prompt-file") + 1]).read_text(), prompt + "\n")

        def continued_in_place(source):
            """Continue on a job stopped with Stop: the same job and run folder launch its author again."""

            launches = []

            def launch(app, workflows, statement, options, *args, **kwargs):
                launches.append((app, options))
                return object(), object()

            with patch.object(server.App, "_launch_workflow_locked", autospec=True, side_effect=launch):
                continued = self.manager.continue_stopped_job(source.state["runId"])
            self.assertIs(continued, source)
            [(launched, options)] = launches
            self.assertIs(launched, source)
            self.assertEqual(options[options.index("--start-node") + 1], "author")
            # (The launch itself is recorded here: it would set the phase to running.)
            self.assertEqual(self.labels(continued)[-1], "Resume requested")
            return options

        source = saved_job(EARLIER_DEFAULT_AUTHOR_PROMPT, "stopped")
        assert_launched_with(source, current, continued_in_place(source))
        for ending in ("stopped by an earlier version", "conversation lost"):
            with self.subTest(ending=ending):
                source = saved_job(EARLIER_DEFAULT_AUTHOR_PROMPT, ending)
                continued = self.manager.continue_stopped_job(source.state["runId"])
                self.assertIsNot(continued, source)
                self.assertEqual(continued.state["goalWorkspace"], str(source.run_dir.resolve()))
                assert_launched_with(continued, current)
        # --resume-author, also when its --author-prompt-file repeats the earlier default.
        for settings in (None, {"authorPrompt": EARLIER_DEFAULT_AUTHOR_PROMPT + "\n"}):
            with self.subTest(settings=settings):
                assert_launched_with(self.manager.start_saved_research_job(source.run_dir, settings), current)
        edited = saved_job("Custom [STATEMENT]", "stopped")
        assert_launched_with(edited, "Custom [STATEMENT]", continued_in_place(edited))
        edited = saved_job("Custom [STATEMENT]", "stopped by an earlier version")
        assert_launched_with(self.manager.continue_stopped_job(edited.state["runId"]), "Custom [STATEMENT]")
        # A continuation cannot change its author prompt, whether the saved one was edited or the earlier default.
        with patch.object(server.App, "_launch_solver_locked") as launch:
            for saved, changed in ((edited, current), (edited, EARLIER_DEFAULT_AUTHOR_PROMPT),
                                   (source, "Edited [STATEMENT]")):
                with self.subTest(changed=changed[:30]), self.assertRaisesRegex(
                        ValueError, "A continued author must keep its original author prompt"):
                    self.manager.start_saved_research_job(saved.run_dir, {"authorPrompt": changed})
        launch.assert_not_called()

    @staticmethod
    def labels(app):
        return [record.get("label") for record in app.state["trace"]]

    def test_only_the_note_on_a_refused_submission_takes_the_reason_and_only_the_reason(self):
        def prepared(**changes):
            workflow = runtime.builtin_workflow("author_critic")
            workflow["prompts"].update(changes)
            with patch.object(runtime, "require_model_credentials"):
                return runtime.prepare(workflow, {})

        self.assertIn("{reason}", prepared()["handoff_refused"])
        for name in ("goal", "continuation", "compaction", "resume", "repair", "reopen", "summary",
                     "conversation"):
            with self.subTest(prompt=name), self.assertRaisesRegex(ValueError, "Unknown prompt input: 'reason'"):
                prepared(**{name: "Why: {reason}."})
        # The related works note is appended to the first prompt as written: its braces are plain text.
        self.assertEqual(prepared(related_works="Why: {reason}.")["related_works"], "Why: {reason}.")
        # The controller renders the note with the reason alone, so no other value can appear in it.
        for field in ("message", "solution", "bugs", "original_prompt", "instruction"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"Unknown prompt input: '{field}'"):
                prepared(handoff_refused="{reason} {" + field + "}")

    def test_restoring_legacy_settings_preserves_reports_without_restarting_audits(self):
        app = self.manager.start_direct_job({"statement": "Exact statement"})
        settings_path = app.run_dir / server.JOB_SETTINGS_FILENAME
        settings = json.loads(settings_path.read_text())
        legacy = {
            "researchAudits": {"models": ["gpt-6-astra", "none", "none"], "intervalHours": 1},
            "researchAuditProgress": {"elapsedSeconds": 3600},
            "auditHoldingAuthor": True,
        }
        settings_path.write_text(json.dumps({**settings, **legacy}))
        report = app.run_dir / "AUDITS" / "saved.md"
        report.parent.mkdir()
        report.write_text("Previously saved advice")
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertTrue(all(key not in restored.state for key in legacy))
        restored._save_job_settings(restored.state)
        self.assertTrue(all(key not in json.loads(settings_path.read_text()) for key in legacy))
        self.assertEqual(report.read_text(), "Previously saved advice")

    def test_new_run_and_saved_continuation_use_the_same_default(self):
        app = self.manager.start_direct_job({"statement": "Exact statement"})
        app.trace_file.write_text("")
        self.assertEqual(app.state["authorPrompt"], server.default_prompts()["author"])
        continued = self.manager.start_saved_research_job(app.run_dir)
        self.assertEqual(continued.state["goalWorkspace"], str(app.run_dir.resolve()))
        self.assertEqual(continued.state["authorPrompt"], app.state["authorPrompt"])

    def test_saved_run_without_prompt_uses_current_author_default(self):
        source = self.manager.start_direct_job({"statement": "Exact statement"})
        (source.run_dir / "prompts/author.txt").unlink()
        # The controller's own copy, outside the run folder, is the one it trusts; an older run has none.
        (server.controller_directory(source.run_dir) / "prompts/author.txt").unlink()
        (source.run_dir / runtime.SAVED_CANDIDATE_FILENAME).write_text("Candidate")
        self.assertEqual(server.saved_critic_source(source.run_dir)["author_prompt"],
                         server.default_prompts()["author"])
        continued = server.App(runs=self.runs)
        continued.start_direct_statement("Exact statement", continuation_source=source.run_dir)
        self.assertEqual(continued.state["authorPrompt"], server.default_prompts()["author"])
        critic = server.App(runs=self.runs)
        with patch.object(critic, "_launch_critic_resume_locked", return_value=(object(), object())):
            critic.start_critic_resume("Exact statement", "Candidate", source_run=source.run_dir)
        self.assertEqual(critic.state["authorPrompt"], server.default_prompts()["author"])

    def test_prepared_workspace_is_discoverable_and_starts_in_place(self):
        directory = self.runs / "prepared"
        directory.mkdir()
        statement = "Exact prepared statement"
        (directory / "checked-statement.md").write_text("# Statement sent directly to the proof author\n\n" + statement)
        (directory / "draft.md").write_text("# Draft problem\n\n" + statement)
        (directory / "INITIAL_PROMPT.md").write_text("Preserved curated instructions")
        settings = server.App._workflow_options(include_review=False)
        settings.update(preparedRun=True, skipStatementReview=True)
        (directory / "job-settings.json").write_text(json.dumps(settings))
        jobs = server.restore_saved_jobs(self.runs)
        app = jobs["prepared"]
        self.assertEqual(app.state["phase"], "prepared")
        self.assertFalse((directory / "transcript.jsonl").exists())
        self.manager.jobs.update(jobs)
        self.assertEqual(self.manager.job_list()[0]["continueStoppedLabel"], "Start prepared run")

        def launch(actual):
            self.assertEqual(actual, statement)
            self.assertEqual(app.run_dir, directory)
            self.assertEqual(app.state["goalThreadId"], "")
            self.assertFalse(app.state["goalResume"])
            app.state["phase"] = "running"
            return object(), object()

        with patch.object(app, "_launch_solver_locked", side_effect=launch):
            started = self.manager.continue_stopped_job("prepared")
        self.assertIs(started, app)
        self.assertEqual(app.state["phase"], "running")
        self.assertFalse(app.state["preparedRun"])
        self.assertEqual((directory / "INITIAL_PROMPT.md").read_text(), "Preserved curated instructions")
        # Beside the run, only the controller's hidden folder of its own copies.
        self.assertEqual([path for path in self.runs.iterdir() if not path.name.startswith(".")], [directory])


if __name__ == "__main__":
    unittest.main()
