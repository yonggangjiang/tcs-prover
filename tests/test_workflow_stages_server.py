"""Selected workflow stages persist and launch only their enabled model roles."""

import io
import itertools
import json
import threading
import unittest
from unittest import mock

from tests.test_api_models_server import HttpCase, StoreCase
from ui import server
from ui.batches import BatchCatalog, ORIGINAL_BATCH


def stages(*selected):
    return {name: name in selected for name in server.WORKFLOW_STAGES}


class StageValidationTests(StoreCase):
    def test_normalization_rejects_empty_selection_and_non_boolean_switches(self):
        self.assertEqual(server.normalize_workflow_stages(), dict.fromkeys(server.WORKFLOW_STAGES, True))
        self.assertEqual(server.normalize_workflow_stages({"author": False})["author"], False)
        for value in (stages(), {"review": 1}, {"author": "false"}, {"extra": True}, [], "author"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.normalize_workflow_stages(value)

    def test_disabled_roles_need_no_model_effort_or_prompt_and_are_not_checked(self):
        for active in server.WORKFLOW_STAGES:
            chosen = stages(active)
            options = {"workflow_stages": chosen}
            for stage, role in server.STAGE_ROLES.items():
                options[f"{role}_model"] = "gpt-6.1-sol" if stage == active else "api:missing-disabled"
                options[f"{role}_effort"] = "max" if stage == active else "invalid-disabled"
            options.update(review_prompt="Review", author_prompt="Prove [STATEMENT]",
                           critic_prompt="Check", final_prompt="Write")
            for prompt, role in server.PROMPT_ROLES.items():
                if not chosen[server.ROLE_STAGES[role]]:
                    options[f"{prompt}_prompt"] = ""
            with self.subTest(active=active), mock.patch.object(server, "require_model_access") as access:
                normalized = server.App._workflow_options(**options)
            self.assertEqual(access.call_args.args[0], ["gpt-6.1-sol"])
            self.assertEqual(normalized["workflowStages"], chosen)

    def test_disabled_prompt_override_values_are_ignored(self):
        body = {"workflowStages": stages("writing"), "promptOverrides": {"author": {"unused": True}, "final": "Write"}}
        result = server.web_prompt_options(body, ("author", "final"))
        with mock.patch.object(server, "require_model_access"):
            normalized = server.App._workflow_options(workflow_stages=body["workflowStages"], **result)
        self.assertEqual(normalized["finalPrompt"], "Write")

    def test_hidden_author_time_limit_cannot_block_a_workflow_without_author(self):
        with mock.patch.object(server, "require_model_access"):
            for selected in (stages("review"), stages("critic"), stages("writing")):
                result = server.App._workflow_options(workflow_stages=selected, thinking_hours="unused-invalid")
                self.assertEqual(result["thinkingHours"], server.DEFAULT_THINKING_HOURS)
            with self.assertRaises(ValueError):
                server.App._workflow_options(workflow_stages=stages("author"), thinking_hours="invalid")

    def test_selected_graph_has_only_enabled_model_stages(self):
        graph = server.workflow_graph(stages("review", "critic"))
        self.assertEqual(set(graph["nodes"]), {"statement_reviewer", "critic", "failure_summary"})
        self.assertTrue(any(edge["from"] == "statement_reviewer" and edge["to"] == "critic" for edge in graph["edges"]))
        self.assertFalse(any(edge["to"] == "author" for edge in graph["edges"]))


class WorkflowStageEndpointTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.launches = []
        launch = mock.patch.object(server.App, "_launch_workflow_locked", autospec=True, side_effect=self.launch)
        worker = mock.patch.object(server.App, "_spawn_worker")
        access = mock.patch.object(server, "require_model_access")
        for patcher in (launch, worker, access):
            patcher.start()
            self.addCleanup(patcher.stop)

    def launch(self, app, workflows, source, options, stage, node, state=None):
        self.launches.append({"app": app, "workflows": workflows, "source": source, "options": options,
                              "stage": stage, "node": node, "state": state})
        app.state.update(phase="running", stage=stage, activeNode=node)
        return mock.Mock(), object()

    def complete_review(self, app, checked="Checked exact statement"):
        report = {"statement": checked, "notes": "Review notes"}
        process = mock.Mock(stdout=io.StringIO(json.dumps({"kind": "review_result", "stage": "review", "review": report}) + "\n"),
                            stdin=io.StringIO())
        process.wait.return_value = process.poll.return_value = 0
        token = app.active_token
        with mock.patch.object(server.subprocess, "Popen", return_value=process):
            app._review(app.state["reviewStatement"], token=token)

    def test_all_nonempty_stage_combinations_launch_in_order_and_preserve_input(self):
        content = "Exact task.\n\nExisting complete candidate argument."
        for flags in itertools.product((False, True), repeat=4):
            selected = dict(zip(server.WORKFLOW_STAGES, flags))
            if not any(flags):
                continue
            with self.subTest(stages=selected):
                self.launches.clear()
                status, result = self.call("/start-workflow", {"content": content, "workflowStages": selected})
                self.assertEqual(status, 200, result)
                app = self.http.jobs[result["runId"]]
                self.assertEqual(result["workflowStages"], selected)
                self.assertEqual(result["originalContent"], content)
                settings = json.loads((app.run_dir / server.JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(settings["workflowStages"], selected)
                self.assertEqual(settings["originalContent"], content)
                if selected["review"]:
                    self.assertEqual(result["phase"], "reviewing")
                    self.assertEqual(self.launches, [])
                    self.complete_review(app)
                    if not any(selected[name] for name in ("author", "critic", "writing")):
                        self.assertEqual((app.state["phase"], app.state["resultKind"]), ("done", "review"))
                        self.assertEqual(self.launches, [])
                        continue
                    self.assertEqual(app.state["phase"], "reviewed")
                    status, result = self.call(f"/approve?job={app.state['runId']}", {"statement": "Approved checked task"})
                    self.assertEqual(status, 200, result)
                [launch] = self.launches
                first = "author" if selected["author"] else "critic" if selected["critic"] else "latex_editor"
                self.assertEqual(launch["node"], first)
                argument = next(value for value in launch["options"] if value.startswith("workflow_stages="))
                self.assertEqual(json.loads(argument.split("=", 1)[1]), selected)
                if not selected["author"]:
                    self.assertEqual(launch["state"]["solution"], content)
                    self.assertEqual(launch["state"]["statement"], "Approved checked task" if selected["review"] else content)
                for stage, role in server.STAGE_ROLES.items():
                    if stage == "review":
                        continue
                    self.assertEqual(f"--{role}-model" in launch["options"], selected[stage])

    def test_stage_validation_fails_before_creating_a_job(self):
        status, result = self.call("/start-workflow", {"content": "Input", "workflowStages": stages()})
        self.assertEqual(status, 400)
        self.assertIn("at least one", result["error"])
        self.assertEqual(self.http.jobs, {})

    def test_review_revision_keeps_original_candidate_and_selected_stages(self):
        selected = stages("review", "critic", "writing")
        status, result = self.call("/start-workflow", {"content": "Original candidate", "workflowStages": selected})
        self.assertEqual(status, 200)
        app = self.http.jobs[result["runId"]]
        self.complete_review(app)
        status, result = self.call(f"/start-workflow?job={app.state['runId']}", {
            "content": "Revised task", "feedback": "Use stronger assumptions", "workflowStages": selected})
        self.assertEqual(status, 200)
        self.assertEqual(result["originalContent"], "Original candidate")
        self.assertEqual(result["workflowStages"], selected)

    def test_folder_batch_honors_review_only_selection(self):
        status, result = self.call("/direct-batch", {"workflowStages": stages("review"), "statements": [
            {"name": "one.md", "statement": "First"}, {"name": "two.md", "statement": "Second"}]})
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["startedJobs"]), 2)
        self.assertTrue(all(self.http.jobs[identity].state["phase"] == "reviewing" for identity in result["startedJobs"]))
        self.assertEqual(self.launches, [])


class WorkflowStageResultTests(StoreCase):
    def app(self, selected):
        app = self.new_app()
        app.state.update(workflowStages=selected, originalContent="Original candidate", phase="running",
                         stage="critic" if selected["critic"] else "solve", activeNode="critic" if selected["critic"] else "author")
        app._save_job_settings(app.state)
        return app

    def feed(self, app, record, code=0):
        token = app.active_token = app.worker_token = object()
        process = mock.Mock(stdout=io.StringIO(json.dumps(record) + "\n"))
        process.wait.return_value = process.poll.return_value = code
        app._read_output(process, token)

    def test_proof_without_writing_is_durable_without_a_latex_document(self):
        app = self.app(stages("author"))
        self.feed(app, {"kind": "final_result", "stage": "solve", "node": "author", "resultKind": "proof", "output": "Complete argument"})
        self.assertEqual((app.state["phase"], app.state["resultKind"], app.state["error"]), ("done", "proof", ""))
        self.assertFalse((app.run_dir / "final.tex").exists())
        self.assertEqual((app.run_dir / "proof-output.md").read_text(encoding="utf-8"), "Complete argument")
        app.add_trace({"kind": "workflow_result", "stage": "solve", "output": "Complete argument"})
        restored = server.restore_saved_app(server.App(app.trace_file, runs=self.runs))
        self.assertEqual(restored.state["workflowStages"], stages("author"))
        self.assertEqual((restored.state["resultKind"], restored.state["output"]), ("proof", "Complete argument"))
        self.assertIsNone(server.Server._stopped_continuation_plan(restored))

    def test_critic_rejection_is_needs_revision_without_writer_or_author_launch(self):
        app = self.app(stages("critic", "writing"))
        self.feed(app, {"kind": "failure_result", "stage": "critic", "node": "critic", "resultKind": "needs-revision",
                        "output": "Unproved candidate", "bugs": ["Missing base case"]}, code=1)
        self.assertEqual((app.state["phase"], app.state["resultKind"]), ("done", "needs-revision"))
        self.assertTrue(app.state["revisionRequired"])
        self.assertIn("Missing base case", app.state["output"])
        self.assertFalse((app.run_dir / "final.tex").exists())
        restored = server.restore_saved_app(server.App(app.trace_file, runs=self.runs))
        self.assertEqual(restored.state["resultKind"], "needs-revision")
        self.assertTrue(restored.state["revisionRequired"])
        self.assertIn("Missing base case", restored.state["output"])

    def test_restored_workflow_ignores_disabled_missing_api_models(self):
        app = self.app(stages("writing"))
        app.state.update(authorModel="api:missing", criticModel="api:missing")
        app._save_job_settings(app.state)
        self.feed(app, {"kind": "final_result", "stage": "final", "node": "latex_editor", "output": "LaTeX"})
        restored = server.restore_saved_app(server.App(app.trace_file, runs=self.runs))
        self.assertEqual(restored.state["workflowStages"], stages("writing"))
        self.assertNotIn("not configured", restored.state["settingsWarning"])

    def manager(self):
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock = self.runs, {}, threading.RLock()
        return manager

    def test_author_continuation_preserves_disabled_critic_and_writing(self):
        app = self.app(stages("author"))
        app.state.update(phase="done", error="Stopped.", manuallyStopped=True, stoppedStage="solve")
        app._save_job_settings(app.state)
        manager = self.manager()
        manager.jobs[app.state["runId"]] = app
        with mock.patch.object(server.App, "_launch_solver_locked", return_value=(mock.Mock(), object())) as launch, \
                mock.patch.object(server.App, "_spawn_worker"):
            continued = manager.continue_stopped_job(app.state["runId"])
        self.assertEqual(continued.state["workflowStages"], stages("author"))
        self.assertEqual(continued.state["originalContent"], "Original candidate")
        self.assertEqual(launch.call_count, 1)
        self.assertFalse((continued.run_dir / "prompts/critic.txt").exists())
        self.assertFalse((continued.run_dir / "prompts/final.txt").exists())

    def test_critic_continuation_does_not_enable_author_or_writing(self):
        app = self.app(stages("critic"))
        app.state.update(phase="done", stage="critic", activeNode="critic", error="Stopped.")
        app._save_job_settings(app.state)
        app._save(server.runtime.SAVED_CANDIDATE_FILENAME, "Saved candidate")
        manager = self.manager()
        with mock.patch.object(server.App, "_launch_critic_resume_locked", return_value=(mock.Mock(), object())) as launch, \
                mock.patch.object(server.App, "_spawn_worker"):
            continued = manager.start_saved_critic_job(app.run_dir)
        self.assertEqual(continued.state["workflowStages"], stages("critic"))
        self.assertEqual(continued.state["originalContent"], "Original candidate")
        self.assertEqual(launch.call_count, 1)
        self.assertFalse((continued.run_dir / "prompts/author.txt").exists())
        self.assertFalse((continued.run_dir / "prompts/final.txt").exists())

    def test_saved_batch_preserves_stage_selection_original_content_and_plain_proof(self):
        app = self.app(stages("author"))
        self.feed(app, {"kind": "final_result", "stage": "solve", "node": "author", "resultKind": "proof", "output": "Complete proof"})
        catalog = BatchCatalog(self.runs)
        [summary] = catalog.jobs(ORIGINAL_BATCH)
        self.assertEqual((summary["resultKind"], summary["workflowStages"]), ("proof", stages("author")))
        view = catalog.state(ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual(view["workflowStages"], stages("author"))
        self.assertEqual(view["originalContent"], "Original candidate")
        self.assertEqual(view["output"], "Complete proof")
        self.assertNotIn("critic", view["workflow"]["nodes"])
        self.assertNotIn("latex_editor", view["workflow"]["nodes"])

    def test_saved_batch_reports_rejected_candidate_as_needs_revision(self):
        app = self.app(stages("critic"))
        self.feed(app, {"kind": "failure_result", "stage": "critic", "node": "critic", "resultKind": "needs-revision",
                        "output": "Candidate", "bugs": ["Gap"]}, code=1)
        catalog = BatchCatalog(self.runs)
        self.assertEqual(catalog.jobs(ORIGINAL_BATCH)[0]["resultKind"], "needs-revision")
        view = catalog.state(ORIGINAL_BATCH, app.run_dir.name)
        self.assertEqual(view["resultKind"], "needs-revision")
        self.assertTrue(view["revisionRequired"])
        self.assertIn("Gap", view["output"])

    def test_writer_only_interruption_retains_exact_input_for_retry(self):
        app = self.app(stages("writing"))
        app.state.update(stage="final", activeNode="latex_editor")
        server.runtime.save_final_input("Approved task", "Existing work", directory=app.run_dir)
        self.feed(app, {"kind": "final_input", "stage": "final", "node": "latex_editor",
                        "label": "Exact writing input saved"}, code=1)
        self.assertTrue(app.state["finalInputReady"])
        plan = server.Server._stopped_continuation_plan(app)
        self.assertEqual(plan["action"], "final")
        restored = server.restore_saved_app(server.App(app.trace_file, runs=self.runs))
        self.assertTrue(restored.state["finalInputReady"])
        self.assertEqual(server.Server._stopped_continuation_plan(restored)["action"], "final")


if __name__ == "__main__":
    unittest.main()
