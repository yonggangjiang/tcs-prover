"""Crash/restart and acceptance regressions; all provider calls are simulated."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import persistent_research as research
from research_journal import ResearchJournal
import workflow_runner as runtime


class ResearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        previous_directory = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous_directory)
        credentials = mock.patch.object(runtime, "require_model_credentials")
        credentials.start()
        self.addCleanup(credentials.stop)
        events = mock.patch.object(runtime, "emit")
        events.start()
        self.addCleanup(events.stop)
        self.statement = "Establish the requested property."
        self.solution = "A complete candidate argument for the requested property."
        self.workflow = runtime.load_workflow(runtime.WORKFLOWS / "author_critic.yaml")
        self.node = self.workflow["nodes"]["author"]
        self.prompts = self.workflow["prompts"]
        self.proposal = {
            "family": "Exchange", "mechanism": "Preserve a completion under exchanges",
            "assumptions": "The original assumptions", "obstacle": "A constrained resource",
            "decisive_test": "Show the exchange preserves completion", "novelty": "New invariant",
        }
        self.result = {
            "status": "candidate", "work": self.solution, "evidence": "A complete argument",
            "candidate": self.solution, "remaining_obligations": [], "next_test": "Independent review",
            "search_queries": [], "read_requests": [],
        }
        self.review = {
            "verdict": "candidate", "reason": "The argument closes the stated gap",
            "checked_evidence": "Each stated inference was checked", "failure_scope": "",
            "reopen_condition": "", "reusable_results": "The proved exchange invariant",
            "search_queries": [], "read_requests": [],
        }

    def seed_round(self, journal, phase):
        checkpoint = {
            "phase": phase, "round": 7, "proposal": self.proposal,
            "related": [], "decision_id": "e000001", "family": "exchange",
        }
        if phase == "review":
            checkpoint["result"] = self.result
        journal.commit("test_setup", {}, {"research_checkpoint": checkpoint})

    def test_pipeline_resume_enters_saved_cleanup_node_without_author_or_editor(self):
        document = "\\documentclass{article}\n\\begin{document}Proved.\\end{document}"
        with ResearchJournal(self.directory, self.statement) as journal:
            journal.commit("test_checkpoint", {}, {"workflow_checkpoint": {
                "node": "final_verifier", "visits": 0,
                "state": {
                    "statement": self.statement, "solution": self.solution,
                    "accepted_solution": self.solution, "output": document, "failed": False,
                },
            }})
        with mock.patch.object(runtime, "run_structured_attempt", return_value=json.dumps({
            "verdict": "pass", "bugs": "",
        })) as provider:
            with mock.patch("latex_verification.verify_latex", return_value={
                "status": "pass", "diagnostic": "Compiled", "engine": "simulated",
            }):
                state = runtime.execute_workflows(
                    [runtime.WORKFLOWS / "author_critic.yaml", runtime.WORKFLOWS / "clean_up.yaml"],
                    {"statement": self.statement}, {"thinking_hours": 0.1},
                )
        self.assertEqual(provider.call_count, 1)
        self.assertIn("FORMATTED LATEX TO CHECK", provider.call_args.args[0])
        self.assertEqual(state["output"], document)
        self.assertFalse(state["failed"])

    def test_stale_critic_rejection_preserves_in_progress_exploration(self):
        with ResearchJournal(self.directory, self.statement) as journal:
            self.seed_round(journal, "explore")
            state = {
                "statement": self.statement, "memory": research.ResearchMemory(journal),
                "report": {"verdict": "reject", "bugs": "An old rejected candidate"},
            }
            session = research.research_session(runtime, self.node, self.prompts, state, {}, journal)
            with mock.patch.object(research, "_request", side_effect=[self.result, self.review]) as request:
                candidate = next(session)
            session.close()
            self.assertEqual(candidate["solution"], self.solution)
            phases = [call.args[2]["prompt"] for call in request.call_args_list]
            self.assertEqual(phases, ["research_explore", "research_review"])
            self.assertEqual(journal.get_state("research_checkpoint")["round"], 7)
            self.assertEqual(list(journal.events("recovery_feedback")), [])

    def test_candidate_checkpoint_and_duplicate_marker_commit_together(self):
        with ResearchJournal(self.directory, self.statement) as journal:
            self.seed_round(journal, "review")
            state = {"statement": self.statement, "memory": research.ResearchMemory(journal)}
            original_commit = journal.commit

            def crash_before_review(kind, payload, state_updates=None):
                if kind == "research_review":
                    raise OSError("Simulated loss before commit")
                return original_commit(kind, payload, state_updates)

            session = research.research_session(runtime, self.node, self.prompts, state, {}, journal)
            with mock.patch.object(research, "_request", return_value=self.review):
                with mock.patch.object(journal, "commit", side_effect=crash_before_review):
                    with self.assertRaisesRegex(OSError, "before commit"):
                        next(session)
            session.close()
            self.assertEqual(journal.get_state("research_checkpoint")["phase"], "review")
            self.assertIsNone(journal.get_state("proof:" + research.fingerprint(self.solution)))
            self.assertIsNone(journal.get_state("candidate"))

            def crash_after_review(kind, payload, state_updates=None):
                result = original_commit(kind, payload, state_updates)
                if kind == "research_review":
                    raise OSError("Simulated loss after commit")
                return result

            session = research.research_session(runtime, self.node, self.prompts, state, {}, journal)
            with mock.patch.object(research, "_request", return_value=self.review):
                with mock.patch.object(journal, "commit", side_effect=crash_after_review):
                    with self.assertRaisesRegex(OSError, "after commit"):
                        next(session)
            session.close()

        with ResearchJournal(self.directory, self.statement) as journal:
            state = {"statement": self.statement, "memory": research.ResearchMemory(journal)}
            self.assertEqual(journal.get_state("research_checkpoint")["phase"], "candidate")
            self.assertEqual(journal.get_state("candidate"), self.solution)
            self.assertTrue(journal.get_state("proof:" + research.fingerprint(self.solution)))
            session = research.research_session(runtime, self.node, self.prompts, state, {}, journal)
            with mock.patch.object(research, "_request") as request:
                candidate = next(session)
            session.close()
            request.assert_not_called()
            self.assertEqual(candidate["solution"], self.solution)

    def test_newly_reported_obligation_prevents_a_clean_pass(self):
        node = self.workflow["nodes"]["critic"]
        audits = [
            {"focus": focus, "verdict": "pass", "report": "No issue found in this focus"}
            for focus in node["parallel"]["items"]
        ]
        report = {
            "checks": audits, "verdict": "pass", "fixed": False,
            "solution": self.solution, "bugs": "", "resolved_obligations": [],
            "memory_update": {
                "approach_family": "Exchange", "approach_result": "An invariant needs proof",
                "blocked_routes": [], "unresolved_obligations": ["Prove the exchange invariant"],
            },
        }
        with ResearchJournal(self.directory, self.statement) as journal:
            memory = research.ResearchMemory(journal)
            memory.record_candidate(self.solution, "test")
            state = {"statement": self.statement, "solution": self.solution,
                     "memory": memory, "open_issues": []}
            with mock.patch.object(runtime, "structured", return_value=(report, json.dumps(report))):
                adjusted, raw = runtime._model_call(
                    node, self.prompts, state, {"parallel_results": audits}, 1,
                )
            self.assertEqual(adjusted["verdict"], "reject")
            context = runtime._complete_node(node, state, adjusted, raw, 1, 0)
            self.assertEqual(context["outcome"], "reject")
            self.assertEqual(journal.get_state("open_issues")[0]["description"], "Prove the exchange invariant")
            self.assertNotEqual(journal.get_state("candidate_status"), "approved")

    def test_invalid_planner_response_does_not_poison_resume_cache(self):
        config = self.node["research"]["steps"]["propose"]
        values = {
            "assignment": self.statement, "statement": self.statement, "memory": "{}",
            "feedback": "{}", "instruction": "", "proposal": "{}", "result": "{}", "related": "[]",
        }
        invalid = {"proposals": [], "search_queries": [], "read_requests": []}
        with ResearchJournal(self.directory, self.statement) as journal:
            with mock.patch.object(runtime, "run_structured_attempt", return_value=json.dumps(invalid)) as provider:
                with self.assertRaisesRegex(runtime.Error, "three invalid"):
                    research._request(runtime, journal, config, self.prompts, values, {})
            self.assertEqual(provider.call_count, 3)

        valid = {"proposals": [
            {**self.proposal, "mechanism": mechanism}
            for mechanism in ("Exchange invariant", "Structural induction", "Dual certificate")
        ], "search_queries": [], "read_requests": []}
        with ResearchJournal(self.directory, self.statement) as journal:
            with mock.patch.object(runtime, "run_structured_attempt", return_value=json.dumps(valid)) as provider:
                recovered = research._request(runtime, journal, config, self.prompts, values, {})
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(recovered, valid)
            self.assertEqual(len(list(journal.events("protocol_error"))), 3)

    def test_semantically_invalid_verifier_response_is_not_replayed_after_resume(self):
        cleanup = runtime.load_workflow(runtime.WORKFLOWS / "clean_up.yaml")
        node = cleanup["nodes"]["final_verifier"]
        state = {
            "statement": self.statement, "accepted_solution": self.solution,
            "output": "\\documentclass{article}\\begin{document}Proved.\\end{document}",
        }
        invalid = {"verdict": "pass", "bugs": "A pass cannot retain this bug."}
        with ResearchJournal(self.directory, self.statement) as journal:
            with mock.patch.object(runtime, "run_structured_attempt", return_value=json.dumps(invalid)) as provider:
                with self.assertRaisesRegex(runtime.Error, "inconsistent review"):
                    runtime._model_call(node, cleanup["prompts"], state, {"_journal": journal}, 1)
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(len(list(journal.events("protocol_error"))), 1)

        valid = {"verdict": "pass", "bugs": ""}
        with ResearchJournal(self.directory, self.statement) as journal:
            with mock.patch.object(runtime, "run_structured_attempt", return_value=json.dumps(valid)) as provider:
                result, _ = runtime._model_call(
                    node, cleanup["prompts"], state, {"_journal": journal}, 1,
                )
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(result, valid)

    def test_terminal_formatting_failure_resumes_editor_with_feedback(self):
        document = "\\documentclass{article}\\begin{document}Incomplete formatting.\\end{document}"
        corrected = document.replace("Incomplete formatting.", "Complete formatting.")
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=[
            json.dumps({"latex": document}),
            json.dumps({"verdict": "reject", "bugs": "The formatting omitted a necessary lemma."}),
        ]) as provider:
            first = runtime.execute_workflows(
                [runtime.WORKFLOWS / "clean_up.yaml"],
                {"statement": self.statement, "solution": self.solution},
                {"thinking_hours": 0.1},
            )
        self.assertTrue(first["failed"])
        self.assertEqual(provider.call_count, 2)
        with ResearchJournal(self.directory, self.statement) as journal:
            saved = journal.get_state("workflow_checkpoint")
            self.assertEqual(saved["node"], "latex_editor")
            self.assertEqual(saved["state"]["solution"], self.solution)

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=[
            json.dumps({"latex": corrected}), json.dumps({"verdict": "pass", "bugs": ""}),
        ]) as provider:
            with mock.patch("latex_verification.verify_latex", return_value={
                "status": "pass", "diagnostic": "Compiled", "engine": "simulated",
            }):
                resumed = runtime.execute_workflows(
                    [runtime.WORKFLOWS / "author_critic.yaml", runtime.WORKFLOWS / "clean_up.yaml"],
                    {"statement": self.statement}, {"thinking_hours": 0.1},
                )
        self.assertEqual(provider.call_count, 2)
        self.assertIn("omitted a necessary lemma", provider.call_args_list[0].args[0])
        self.assertTrue(all(call.args[2] == "final" for call in provider.call_args_list))
        self.assertFalse(resumed["failed"])
        self.assertEqual(resumed["output"], corrected)


if __name__ == "__main__":
    unittest.main()
