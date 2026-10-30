"""Graph package decisions: file edits, the verify-only round, pauses at any step, handoff refusals, time limits.

Offline: stand-in attempts, the stand-in Codex and Claude CLIs and a patched
clock; no model call is made. Every test that could reach the author patches
both goal sessions, so a regression fails the test instead of starting a CLI.
"""

import errno
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, proof_handoff, providers
from tests import critic_doubles
from ui import server

import test_audit_fixes_frontend as frontend_test
import test_claude_cli as claude_test
import test_model_retry as retry_test

ROOT = Path(runtime.__file__).parent
AUTHOR_CRITIC, CLEAN_UP = ROOT / "workflows/author_critic.yaml", ROOT / "workflows/clean_up.yaml"
PROOF = "Lemma 1 holds by induction on n.\nHence the theorem holds."
LATEX = r"\documentclass{article}\begin{document}Proof.\end{document}"


class GraphCase(retry_test.RetryCase):
    def setUp(self):
        super().setUp()
        self.guard_author()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        # Writable workspaces are temporary folders: keep them inside this test's folder.
        temp = self.root / "tmp"
        temp.mkdir()
        patcher = mock.patch.object(tempfile, "tempdir", str(temp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def guard_author(self, session=None):
        """Reaching the author fails at once, unless a stand-in session is given."""

        for module in (runtime, claude_cli):
            patcher = mock.patch.object(module, "goal_session", side_effect=session or retry_test.AuthorStarted(
                "the author must not run"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def scripted(self, steps):
        """A stand-in attempt per step: {"write": text} edits the file, then "answer" is returned.

        The convention critic finds the proof faithful (critic_doubles); only the other calls follow the
        steps. A critic answer without `fixes` gets the one the critic must give: a note when its step
        changed proof.md, else "".
        """

        calls, sessions = [], retry_test.SessionRecorder()

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            workspace = Path(conversation.workspace) if conversation is not None and conversation.workspace else None
            edit = settings.get("edit_file")
            calls.append({"text": text, "schema": schema, "stage": stage, "edit": edit,
                          "fork": conversation.fork_from if conversation is not None else None,
                          "input_files": settings.get("input_files"), "workspace": workspace,
                          "files": {path.name: path.read_text() for path in workspace.iterdir() if path.is_file()}
                          if workspace else {}})
            step = steps[min(len(calls), len(steps)) - 1]
            sessions.open(conversation, f"s-{len(calls)}", output=step.get("output", True))
            if "write" in step:
                target = workspace / edit.name
                if target.is_symlink():
                    target.unlink()
                target.write_text(step["write"])
            if step.get("link"):
                (workspace / edit.name).unlink()
                (workspace / edit.name).symlink_to(self.root / "outside.md")
            if "fail" in step:
                raise step["fail"]
            answer = step["answer"]
            if stage == "critic" and {"verdict", "bugs"} <= set(answer) and "fixes" not in answer:
                submitted = runtime.normalized_text(dict(settings.get("input_files") or ()).get("proof.md") or "")
                # Its edited proof.md, or (a web-cutoff run) the proof in its answer.
                changed = (edit is not None and "write" in step and runtime.normalized_text(step["write"]) != submitted
                           or isinstance(answer.get("solution"), str)
                           and runtime.normalized_text(answer["solution"]) != submitted)
                answer = {**answer, "fixes": "Fixed the gap." if changed else ""}
            return json.dumps(answer)

        return calls, critic_doubles.coordinator_attempts(attempt), sessions

    def critic(self, state=None):
        workflow = runtime.builtin_workflow("author_critic")
        state = state or {"statement": "Task", "solution": PROOF}
        node = workflow["nodes"]["critic"]
        return node, runtime.prepare(workflow, {}), state, runtime._prepare_node_files(node, state, 1, str(self.root))

    def kinds(self, kind):
        return [event for event in self.events if event["kind"] == kind]


class EditedFileTests(GraphCase):
    """I-046: the critic and the LaTeX writers edit a writable copy in small steps and answer with a short status."""

    def test_the_critic_edits_its_copy_of_proof_md_and_answers_only_verdict_and_bugs(self):
        calls, attempt, _ = self.scripted([{"write": PROOF + "\nQED.", "answer": {"verdict": "pass", "bugs": ""}}])
        node, prompts, state, files = self.critic()
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(node, prompts, state, {}, 1, input_files=files)
        call = calls[0]
        # The answer has no proof field (only what the critic fixed), and the prompt does not repeat the proof.
        self.assertEqual(call["schema"]["properties"].keys(), {"verdict", "bugs", "fixes"})
        self.assertNotIn(PROOF, call["text"])
        self.assertIn("PROOF FILES:\nYour working directory contains proof.md (a writable copy of the exact "
                      "candidate)", call["text"])
        self.assertIn("EDITING proof.md:\nWrite all changes to proof.md in place, in small steps", call["text"])
        self.assertNotIn("Return the complete proof text in solution", call["text"])
        self.assertEqual(call["files"], {"proof.md": PROOF, "statement.md": "Task", "manifest.json":
                                         dict(files)["manifest.json"]})
        self.assertEqual(stat.S_IMODE((call["workspace"] / "proof.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((call["workspace"] / "statement.md").stat().st_mode), 0o400)
        # The file read back is the result's proof; the outcome compares it with the captured candidate.
        self.assertEqual(report["solution"], PROOF + "\nQED.")
        self.assertEqual(runtime.evaluate(node["outcome"], {"result": report, "state": state}), "fixed")
        request = next(event for event in self.kinds("request") if event["label"] == "Critic coordinator")
        self.assertEqual(request["candidateText"], PROOF)

    def test_an_untouched_copy_is_a_clean_pass(self):
        calls, attempt, _ = self.scripted([{"answer": {"verdict": "pass", "bugs": ""}}])
        node, prompts, state, files = self.critic()
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(node, prompts, state, {}, 1, input_files=files)
        self.assertEqual(report["solution"], PROOF)
        self.assertEqual(runtime.evaluate(node["outcome"], {"result": report, "state": state}), "pass")

    def test_an_emptied_or_linked_copy_is_retried_in_the_same_conversation_and_files(self):
        calls, attempt, _ = self.scripted([
            {"write": "", "answer": {"verdict": "pass", "bugs": ""}},
            {"link": True, "answer": {"verdict": "pass", "bugs": ""}},
            {"write": "Repaired proof.", "answer": {"verdict": "pass", "bugs": ""}}])
        (self.root / "outside.md").write_text("A file outside the workspace.")
        node, prompts, state, files = self.critic()
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(node, prompts, state, {}, 1, input_files=files)
        self.assertEqual(report["solution"], "Repaired proof.")
        self.assertEqual([call["fork"] for call in calls], [None, "s-1", "s-2"])
        self.assertIn("could not be accepted: proof.md is empty; it must hold the complete final text.",
                      calls[1]["text"])
        self.assertIn("could not be accepted: proof.md must be a regular file, not a link or folder",
                      calls[2]["text"])
        # Each fork finds the file exactly as the attempt it continues left it.
        self.assertEqual(calls[1]["files"]["proof.md"], "")
        self.assertNotIn("proof.md", calls[2]["files"])
        self.assertEqual(len({call["workspace"] for call in calls}), 1)

    def test_a_new_conversation_starts_again_from_the_captured_candidate(self):
        overflow = runtime.Error("Codex critic failed: context_length_exceeded: input exceeds the context window")
        calls, attempt, _ = self.scripted([{"write": "Half-edited proof.", "fail": overflow},
                                           {"answer": {"verdict": "pass", "bugs": ""}}])
        node, prompts, state, files = self.critic()
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(node, prompts, state, {}, 1, input_files=files)
        self.assertEqual(calls[1]["fork"], None)
        self.assertEqual(calls[1]["files"]["proof.md"], PROOF)
        self.assertEqual(report["solution"], PROOF)

    def test_web_cutoff_runs_keep_the_snapshot_read_only_and_take_the_text_from_the_answer(self):
        calls, attempt, _ = self.scripted([{"answer": {"verdict": "pass", "bugs": "", "solution": "Edited inline."}}])
        node, prompts, state, files = self.critic()
        with mock.patch.object(runtime, "retrieval_policy", return_value={"mode": "historical"}), \
                mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(node, prompts, state, {}, 1, input_files=files)
        call = calls[0]
        self.assertIsNone(call["edit"])
        self.assertIsNone(call["workspace"])
        self.assertEqual(call["schema"]["required"], ["verdict", "bugs", "fixes", "solution"])
        self.assertIn("READ-ONLY PROOF FILES:", call["text"])
        self.assertIn("This run cannot give you a writable proof.md. Return the complete final text of proof.md "
                      "in the solution field of your JSON answer, never a file pointer.", call["text"])
        self.assertEqual(report["solution"], "Edited inline.")

    def test_the_latex_editor_creates_latex_source_tex_and_the_repair_edits_it(self):
        broken = LATEX.replace("Proof.", r"\undefined Proof.")
        calls, attempt, _ = self.scripted([{"write": broken, "answer": {"summary": "Wrote the paper."}},
                                           {"write": LATEX, "answer": {"summary": "Removed \\undefined."}}])
        results = [{"status": "fail", "diagnostic": "Command exited with code 12.", "returncode": 12,
                    "output": "./latex-source.tex:1: Undefined control sequence."},
                   {"status": "pass", "diagnostic": "Command exited with code 0.", "returncode": 0, "output": ""}]
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt), \
                mock.patch.object(runtime, "_run_command", side_effect=results):
            state = runtime.execute_workflows([CLEAN_UP], {"solution": PROOF})
        editor, repair = calls
        self.assertEqual(editor["schema"]["properties"].keys(), {"summary"})
        self.assertEqual(editor["files"], {})
        self.assertIn("EDITING latex-source.tex:\nCreate latex-source.tex in your working directory; it does not "
                      "exist yet.", editor["text"])
        self.assertEqual(repair["files"], {"latex-source.tex": broken})
        self.assertNotIn(broken, repair["text"])
        self.assertIn("Undefined control sequence", repair["text"])
        self.assertIn("Your working directory contains a writable copy of latex-source.tex.", repair["text"])
        self.assertNotEqual(editor["workspace"], repair["workspace"])
        self.assertEqual(state["output"], LATEX)
        self.assertEqual((self.root / "final.tex").read_text(), LATEX)

    def test_a_rule_on_the_edited_document_names_the_file(self):
        # latex_intact() reads the edited file, so its explanation names that file, not a JSON field.
        elided = LATEX.replace("Proof.", "Proof.\n% ... rest of the document unchanged ...\n")
        calls, attempt, _ = self.scripted([{"write": elided, "answer": {"summary": "Wrote the paper."}},
                                           {"write": LATEX, "answer": {"summary": "Wrote the whole paper."}}])
        workflow = runtime.builtin_workflow("clean_up")
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(workflow["nodes"]["latex_editor"], runtime.prepare(workflow, {}),
                                            {"solution": PROOF}, {}, 1)
        self.assertEqual(report["latex"], LATEX)
        self.assertEqual(calls[1]["fork"], "s-1")
        self.assertIn("could not be accepted: latex-source.tex has a comment in place of text", calls[1]["text"])
        self.assertNotIn("the latex field", calls[1]["text"])

    def test_web_cutoff_repairs_receive_the_source_in_the_prompt_and_return_it(self):
        node = runtime.builtin_workflow("clean_up")["nodes"]["latex_repair"]
        prompts = runtime.prepare(runtime.builtin_workflow("clean_up"), {})
        state = {"output": "BROKEN SOURCE", "compilation": {"diagnostic": "Command exited with code 12.",
                                                             "output": "Undefined control sequence."}}
        calls, attempt, _ = self.scripted([{"answer": {"summary": "Fixed.", "latex": "FIXED SOURCE"}}])
        with mock.patch.object(runtime, "retrieval_policy", return_value={"mode": "historical"}), \
                mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            report, _ = runtime._model_call(node, prompts, state, {}, 1)
        self.assertIn("\n\nCURRENT latex-source.tex:\nBROKEN SOURCE", calls[0]["text"])
        self.assertEqual(report["latex"], "FIXED SOURCE")

    def test_a_whole_run_edits_only_copies_and_never_the_authors_proof_md(self):
        (self.root / "proof.md").write_text(PROOF)

        def author(module, prompt, **kwargs):
            yield {"outcome": "done", "output": "PROOF_FILE: proof.md"}
        self.guard_author(author)
        calls, attempt, _ = self.scripted([
            {"write": PROOF + "\nQED.", "answer": {"verdict": "pass", "bugs": ""}},
            {"answer": {"verdict": "pass", "bugs": ""}},
            {"write": LATEX, "answer": {"summary": "Wrote the paper."}}])
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt), \
                mock.patch.object(runtime, "_run_command",
                                  return_value={"status": "pass", "output": "", "returncode": 0}):
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task"}, {})
        self.assertEqual([call["stage"] for call in calls], ["critic", "critic", "final"])
        self.assertEqual(calls[1]["files"]["proof.md"], PROOF + "\nQED.")
        self.assertEqual(state["solution"], PROOF + "\nQED.")
        self.assertEqual(state["output"], LATEX)
        self.assertEqual((self.root / "proof.md").read_text(), PROOF)
        self.assertEqual((self.root / "saved-candidate.md").read_text(), PROOF + "\nQED.")
        self.assertEqual((self.root / "final.tex").read_text(), LATEX)
        self.assertTrue(all(call["workspace"].parent == self.root / "tmp" for call in calls))

    def test_the_workflow_declares_each_edited_file_and_rejects_invalid_ones(self):
        critic = runtime.builtin_workflow("author_critic")["nodes"]["critic"]
        self.assertEqual(critic["edit"], {"file": "proof.md", "field": "solution"})
        self.assertNotIn("{solution}", runtime.builtin_workflow("author_critic")["prompts"]["critic_input"])
        writer = runtime.builtin_workflow("clean_up")["nodes"]
        self.assertEqual(writer["latex_repair"]["edit"], {"file": "latex-source.tex", "field": "latex", "input": "latex"})
        self.assertNotIn("{latex}", runtime.builtin_workflow("clean_up")["prompts"]["compilation_repair"])
        base = {"run": "structured", "prompt": "ask", "response": {"note": "string"}, "next": "end"}
        for edit in ({"file": "../proof.md", "field": "text"}, {"file": ".hidden", "field": "text"},
                     {"file": "a.md", "field": "note"}, {"file": "a.md", "field": "text", "input": "missing"},
                     {"file": "a.md"}):
            path = self.root / "edit.yaml"
            path.write_text(runtime.yaml.safe_dump({"prompts": {"ask": "Ask."}, "nodes": {"n": {**base, "edit": edit}}}))
            with self.subTest(edit=edit), self.assertRaisesRegex(ValueError, "edit needs a plain file name"):
                runtime.load_workflow(path)


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class CodexEditTests(GraphCase):
    def setUp(self):
        super().setUp()
        state = self.root / "codex-state"
        state.mkdir()
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n{retry_test.CODEX_FAKE}")
        fake.chmod(0o700)
        environment = mock.patch.dict(os.environ, {"TCS_FAKE_CODEX_STATE": str(state),
                                                    "CODEX_HOME": str(self.root / "codex-home"),
                                                    "TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        located = mock.patch.object(runtime, "codex", return_value=str(fake))
        located.start()
        self.addCleanup(located.stop)
        self.state = state

    def test_an_editing_call_runs_in_a_writable_sandbox_of_its_own_workspace(self):
        (self.state / "script.json").write_text(json.dumps([{"answer": '{"verdict": "pass", "bugs": "", '
                                                                       '"fixes": "Rewrote the proof."}'}]))
        original, seen = runtime._run_structured_process, []

        def edit(*args, **kwargs):
            seen.append(kwargs)
            (Path(kwargs["workspace"]) / "proof.md").write_text("Edited by Codex.")
            return original(*args, **kwargs)

        node, prompts, state, files = self.critic()
        with mock.patch.object(runtime, "_run_structured_process", side_effect=edit):
            report, _ = runtime._model_call(node, prompts, state, {}, 1, input_files=files)
        self.assertEqual(report["solution"], "Edited by Codex.")
        self.assertTrue(seen[0]["writable"])
        self.assertIn("shell_tool", seen[0]["features"])
        argv = json.loads((self.state / "log.jsonl").read_text().splitlines()[0])["argv"]
        if sys.platform == "darwin":
            # The confined profile, with its own workspace writable.
            profile = next(value for value in argv if value.startswith("permissions.tcs_prover_private.filesystem="))
            self.assertIn('":workspace_roots"={"."="write"', profile)
        else:
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("-C") + 1], str(seen[0]["workspace"]))


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class ClaudeEditTests(claude_test.FakeClaudeCase):
    def test_structured_tools_add_write_and_edit_only_for_an_edited_file(self):
        def tools(features, bare=False):
            return claude_cli.tool_permissions("structured", features, live_search=False, bash=False, bare=bare)
        self.assertEqual(tools(("multi_agent",)), (["Read", "Task"], ["Read", "Task"]))
        self.assertEqual(tools(("multi_agent", "file_edit")),
                         (["Read", "Task", "Write", "Edit"], ["Read", "Task", "Edit(./**)"]))
        self.assertEqual(tools(("file_edit",), bare=True)[0], ["Read", "Edit"])

    def test_a_claude_critic_edits_proof_md_in_its_workspace(self):
        # One critic call, with the Task tool for its independent critics (the prompt is their only limit).
        self.script([["init", {"write": {"path": "proof.md", "text": "Edited by Claude."}},
                      {"structured": {"verdict": "pass", "bugs": "", "fixes": "Rewrote the proof."}}]])
        workflow = runtime.builtin_workflow("author_critic")
        node = workflow["nodes"]["critic"]
        state = {"statement": "Task", "solution": PROOF}
        files = runtime._prepare_node_files(node, state, 1, str(self.run_dir))
        report, _ = runtime._model_call(node, runtime.prepare(workflow, {}), state,
                                        {"critic_model": "claude-opus-5-5", "critic_count": 2}, 1, input_files=files)
        self.assertEqual((report["solution"], report["fixes"]), ("Edited by Claude.", "Rewrote the proof."))
        [critic] = self.records("start")
        # It may start subagents (Task) and write, but only its own copy of proof.md.
        argv = critic["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Task,Write,Edit,WebSearch,WebFetch")
        self.assertEqual(sorted(path.name for path in Path(critic["cwd"]).iterdir()),
                         ["manifest.json", "proof.md", "statement.md"])
        text = self.records("stdin")[0]["text"]
        self.assertNotIn(PROOF, text)
        self.assertIn("Spawn exactly 2 fresh independent subagent(s)", text)


class VerifyRoundTests(GraphCase):
    """I-048: at the round limit one more critic round reviews the latest edits read-only."""

    def test_edited_passes_at_the_limit_end_with_a_read_only_verification(self):
        calls, attempt, _ = self.scripted([{"write": "P1", "answer": {"verdict": "pass", "bugs": ""}},
                                           {"write": "P2", "answer": {"verdict": "pass", "bugs": ""}},
                                           {"answer": {"verdict": "pass", "bugs": ""}}])
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            state = runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": PROOF},
                                              {"start_node": "critic", "critic_rounds": 2})
        self.assertEqual(state["output"], "P2")
        verify = calls[2]
        self.assertIsNone(verify["edit"])
        self.assertEqual(dict(verify["input_files"])["proof.md"], "P2")
        self.assertIn("FINAL VERIFICATION: this proof has reached the limit of edited critic rounds", verify["text"])
        self.assertIn("READ-ONLY PROOF FILES:", verify["text"])
        self.assertNotIn("EDITING proof.md", verify["text"])
        self.assertEqual([event["outcome"] for event in self.kinds("critic_result")], ["fixed", "fixed", "pass"])
        approval = next(event for event in self.kinds("status") if event.get("label") == "Critic approved")
        self.assertEqual((approval["acceptanceReason"], approval["round"]), ("clean_pass", 3))

    def test_a_verification_that_finds_bugs_returns_the_verified_proof_to_the_author(self):
        instructions = []

        def author(module, prompt, *, initial_instruction="", **kwargs):
            instructions.append(initial_instruction)
            yield {"outcome": "done", "output": "Revised proof."}
        self.guard_author(author)
        calls, attempt, _ = self.scripted([{"write": "P1", "answer": {"verdict": "pass", "bugs": ""}},
                                           {"answer": {"verdict": "reject", "bugs": "Step 3 is unproved."}},
                                           {"answer": {"verdict": "pass", "bugs": ""}}])
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            state = runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": PROOF},
                                              {"start_node": "critic", "critic_rounds": 1})
        self.assertEqual(state["output"], "Revised proof.")
        self.assertIn("CURRENT CANDIDATE:\nP1\n\nCRITIC BUGS:\nStep 3 is unproved.", instructions[0])

    def test_a_verification_round_cannot_accept_edits(self):
        reports = [{"verdict": "pass", "solution": "P1", "bugs": ""},
                   {"verdict": "pass", "solution": "An unreviewed edit.", "bugs": ""}]
        with mock.patch.object(runtime, "structured", side_effect=critic_doubles.coordinator(
                [(report, json.dumps(report)) for report in reports])):
            state = runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": PROOF},
                                              {"start_node": "critic", "critic_rounds": 1})
        self.assertEqual(state["output"], "P1")
        self.assertEqual([event["outcome"] for event in self.kinds("critic_result")], ["fixed", "pass"])

    def test_the_verify_prompt_belongs_to_the_critic_stage(self):
        workflow = runtime.builtin_workflow("author_critic")
        self.assertIn("critic_verify", runtime._node_prompt_names(workflow["nodes"]["critic"]))
        # With the critic off, an override of its verify prompt is ignored like its other prompts.
        prompts = runtime.prepare(workflow, {"workflow_stages": {"critic": False}, "prompts": {"critic_verify": ""}})
        self.assertEqual(prompts["critic_verify"], workflow["prompts"]["critic_verify"])

    def test_a_verify_branch_names_an_existing_prompt(self):
        workflow = {"prompts": {"ask": "Ask."}, "nodes": {"n": {
            "run": "structured", "prompt": "ask", "response": {"note": "string"}, "outcome": "'again'",
            "next": {"again": {"repeat": 1, "verify": "missing"}}}}}
        path = self.root / "verify.yaml"
        path.write_text(runtime.yaml.safe_dump(workflow))
        with self.assertRaisesRegex(ValueError, "Invalid verify prompt"):
            runtime.load_workflow(path)


class HandoffTests(GraphCase):
    """I-004 and the I-046 size check: a refused submission pauses at the author as it was before it.

    The resumed author is told why (its handoff_refused prompt) until it submits again.
    """

    def author_submits(self, *answers):
        def author(module, prompt, **kwargs):
            for answer in answers:
                yield {"outcome": "done", "output": answer}
        self.guard_author(author)

    def run_proof(self, state, workflows=(AUTHOR_CRITIC, CLEAN_UP)):
        with mock.patch.object(runtime, "run_structured_attempt",
                               side_effect=retry_test.AuthorStarted("no critic call may start")):
            return runtime.execute_workflows(list(workflows), state, {})

    def pause(self):
        return self.kinds("workflow_paused")[-1]

    def note(self, reason):
        return runtime.render_template(runtime.builtin_workflow("author_critic")["prompts"]["handoff_refused"],
                                       {"reason": reason})

    def resume_author(self, paused, *answers, critic=(), workflow=AUTHOR_CRITIC):
        """Run the resumed runner on a paused state, as App.resume() starts it, with a stand-in author.

        Each answer is one submission and ``critic`` scripts the critic's answers; return the final
        state, each launch's keywords and the feedback each later submission received.
        """

        launches, feedback = [], []

        def author(module, prompt, **kwargs):
            launches.append(kwargs)
            for answer in answers:
                feedback.append((yield {"outcome": "done", "output": answer}))
        self.guard_author(author)
        review = self.scripted(list(critic))[1] if critic else retry_test.AuthorStarted("no critic call may start")
        state = {key: value for key, value in paused.items() if key != "paused"}
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=review):
            state = runtime.execute_workflows([workflow], state, {"start_node": "author"})
        return state, launches, feedback

    def server_resume(self, app):
        """App.resume() with a stand-in process: its command line and the state the runner would read."""

        for role in ("author", "critic", "final"):
            app._save(f"prompts/{role}.txt", "Prompt.\n")
        with mock.patch.object(server.subprocess, "Popen", return_value=mock.Mock(stdin=io.StringIO())) as popen, \
                mock.patch.object(app, "_spawn_worker"), mock.patch.object(app, "_check_launch_models_locked"):
            app.resume()
        command = popen.call_args.args[0]
        return command, json.loads(Path(command[command.index("--state-file") + 1]).read_text())

    def test_a_missing_proof_file_pauses_at_the_author_and_is_retried_automatically(self):
        self.author_submits("PROOF_FILE: proof.md")
        state = self.run_proof({"statement": "Task"})
        pause = self.pause()
        self.assertEqual((pause["node"], pause["stage"]), ("author", "solve"))
        self.assertTrue(pause["reason"].startswith("Cannot hand the submitted proof to the critic:"))
        self.assertNotIn("safetyStop", pause)
        # The checkpoint is the author's state before it submitted, with the reason it is told when it resumes.
        self.assertEqual(state, {"statement": "Task", "paused": True, "handoff_refused": pause["reason"]})
        self.assertIn(pause["reason"], [event["text"] for event in self.kinds("diagnostic")])

    def test_the_resumed_author_is_told_the_reason_once_until_it_submits_again(self):
        self.author_submits("PROOF_FILE: proof.md")
        paused = self.run_proof({"statement": "Task"})
        reason = self.pause()["reason"]
        (self.root / "proof.md").write_text(PROOF)
        state, launches, feedback = self.resume_author(
            paused, "PROOF_FILE: proof.md", "PROOF_FILE: revised.md",
            critic=[{"answer": {"verdict": "reject", "bugs": "Step 2 fails."}}])
        # One session, told once and apart from its first instruction (there is no critic feedback).
        self.assertEqual([(launch["initial_instruction"], launch["handoff_note"]) for launch in launches],
                         [("", self.note(reason))])
        # The next submission went to the critic without the reason, and the revision after its
        # rejection received only the critic's feedback.
        self.assertEqual([event["state"].get("handoff_refused") for event in self.kinds("node_input")], [None])
        self.assertEqual(feedback, [{"solution": PROOF, "bugs": "Step 2 fails.", "round": 1}])
        # The refused revision pauses with its own reason beside the rejection.
        second = self.pause()["reason"]
        self.assertIn("revised.md", second)
        self.assertEqual((state["handoff_refused"], state["report"]["verdict"]), (second, "reject"))
        # A reviewed submission ends the note for good.
        state, _, _ = self.resume_author(paused, "PROOF_FILE: proof.md",
                                         critic=[{"answer": {"verdict": "pass", "bugs": ""}}])
        self.assertEqual((state["output"], "handoff_refused" in state), (PROOF, False))

    def test_a_workflow_without_the_prompt_keeps_no_reason_and_sends_no_note(self):
        definition = runtime.yaml.safe_load(AUTHOR_CRITIC.read_text(encoding="utf-8"))
        definition["nodes"]["author"]["lifecycle"].remove("handoff_refused")
        del definition["prompts"]["handoff_refused"]
        workflow = self.root / "custom.yaml"
        workflow.write_text(runtime.yaml.safe_dump(definition), encoding="utf-8")
        self.author_submits("PROOF_FILE: proof.md")
        state = self.run_proof({"statement": "Task"}, [workflow])
        self.assertEqual((self.pause()["node"], state), ("author", {"statement": "Task", "paused": True}))
        # A reason saved under another workflow is never sent and ends with the next submission.
        (self.root / "proof.md").write_text(PROOF)
        state, launches, _ = self.resume_author({"statement": "Task", "handoff_refused": "Old reason."},
                                                "PROOF_FILE: proof.md", workflow=workflow,
                                                critic=[{"answer": {"verdict": "pass", "bugs": ""}}])
        self.assertNotIn("handoff_note", launches[0])
        self.assertEqual((state["output"], "handoff_refused" in state), (PROOF, False))

    def test_every_attempt_until_the_next_submission_is_told_and_no_later_one(self):
        # A model-call failure before and one after the next submission: each continues the same thread.
        (self.root / "proof.md").write_text(PROOF)
        reason = "Cannot hand the submitted proof to the critic: (proof.md: file not found)."
        launches = []

        def failure(stage):
            return runtime.WorkflowPaused("Author turn failed: overloaded", reason="Author turn failed: overloaded",
                                          thread_id="thread-1", retry={"thread": "thread-1", "stage": stage,
                                                                       "instruction": "Turn.", "delivered": True})

        def author(module, prompt, **kwargs):
            launches.append(kwargs)
            if len(launches) == 1:
                raise failure("solve")
            yield {"outcome": "done", "output": "PROOF_FILE: proof.md"}
            if len(launches) == 2:
                raise failure("repair")
        self.guard_author(author)
        _, review, _ = self.scripted([{"answer": {"verdict": "reject", "bugs": "Step 2 fails."}},
                                      {"answer": {"verdict": "pass", "bugs": ""}}])
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=review):
            state = runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "handoff_refused": reason},
                                              {"start_node": "author"})
        self.assertEqual(state["output"], PROOF)
        self.assertEqual([launch.get("handoff_note") for launch in launches], [self.note(reason)] * 2 + [None])
        self.assertEqual(launches[2]["options"]["goal_retry"]["stage"], "repair")

    def test_a_refused_repair_keeps_the_rejection_and_its_candidate(self):
        self.author_submits("PROOF_FILE: ../outside.md")
        report = {"verdict": "reject", "solution": "Critic candidate", "bugs": "Lemma 2 fails."}
        state = self.run_proof({"statement": "Task", "solution": "Critic candidate", "report": report, "round": 2})
        pause = self.pause()
        reason = pause["reason"]
        self.assertEqual(pause["node"], "author")
        self.assertEqual((state["solution"], state["report"], state["round"], state["handoff_refused"]),
                         ("Critic candidate", report, 2, reason))
        # The resumed author gets the critic's feedback as its first instruction and the reason apart
        # from it; both goal sessions send the reason after the feedback.
        _, launches, _ = self.resume_author(state, "PROOF_FILE: ../outside.md")
        repair = runtime.render_template(runtime.builtin_workflow("author_critic")["prompts"]["repair"],
                                         {"solution": "Critic candidate", "bugs": "Lemma 2 fails."})
        self.assertEqual([(launch["initial_instruction"], launch["handoff_note"]) for launch in launches],
                         [(repair, self.note(reason))])

    def test_a_proof_too_large_for_the_critic_is_retried_like_any_refused_handoff(self):
        proof = PROOF * 10
        size = len(proof.encode("utf-8"))
        self.author_submits(proof)
        with mock.patch.object(runtime, "model_context_tokens", return_value=50):
            state = self.run_proof({"statement": "Task"})
        pause = self.pause()
        self.assertEqual(pause["node"], "author")
        self.assertNotIn("safetyStop", pause)
        self.assertEqual(pause["reason"], "Cannot hand the submitted proof to the critic: the proof is about "
                                          f"{-(-size // 4):,} tokens ({size:,} bytes), more than the 50-token "
                                          "context window of GPT 6.1 Sol.")
        self.assertEqual(state, {"statement": "Task", "paused": True, "handoff_refused": pause["reason"]})
        # The server resumes the author after a minute, as after every refused handoff.
        app = server_app(self.root / "server")
        feed(app, [pause], code=0, now=1_000)
        checkpoint = json.loads((app.run_dir / server.PAUSE_FILENAME).read_text())
        self.assertEqual((checkpoint["node"], checkpoint["autoResumeAt"]), ("author", 1_000 + server.AUTO_RESUME_SECONDS))

    def test_a_proof_over_8_mib_is_refused_in_the_same_words_inline_or_as_a_file(self):
        large = "x" * (proof_handoff.MAX_PROOF_BYTES + 10)
        (self.root / "proof.md").write_text(large)
        for submission in (large, "PROOF_FILE: proof.md"):
            with self.subTest(file=submission.startswith("PROOF_FILE")):
                self.events.clear()
                self.author_submits(submission)
                state = self.run_proof({"statement": "Task"})
                pause = self.pause()
                self.assertEqual(pause["node"], "author")
                self.assertEqual(pause["reason"], "Cannot hand the submitted proof to the critic: the proof is "
                                                  "8,388,618 bytes, more than the 8,388,608 bytes (8 MiB) the critic "
                                                  "accepts.")
                self.assertNotIn("safetyStop", pause)
                self.assertEqual(state, {"statement": "Task", "paused": True, "handoff_refused": pause["reason"]})

    def test_the_size_check_covers_inline_proofs_and_known_context_windows(self):
        limit = runtime.proof_handoff.MAX_PROOF_BYTES
        self.assertEqual(runtime.candidate_size_problem("x" * limit), "")
        self.assertEqual(runtime.candidate_size_problem("x" * (limit + 1)),
                         "the proof is 8,388,609 bytes, more than the 8,388,608 bytes (8 MiB) the critic accepts")
        with mock.patch.object(providers, "api_model", return_value={"contextWindow": 1000}), \
                mock.patch.object(providers, "model_label", return_value="Gateway"):
            self.assertEqual(runtime.model_context_tokens("api:gateway"), 1000)
            self.assertEqual(runtime.candidate_size_problem("x" * 4000, "api:gateway"), "")
            self.assertEqual(runtime.candidate_size_problem("x" * 4004, "api:gateway"),
                             "the proof is about 1,001 tokens (4,004 bytes), more than the 1,000-token context "
                             "window of Gateway")
        self.assertIsNone(runtime.model_context_tokens("gpt-6.1-sol"))

    def test_the_server_retries_a_refused_handoff_after_a_minute(self):
        app = server_app(self.root)
        reason = "Cannot hand the submitted proof to the critic: missing file."
        record = {"kind": "workflow_paused", "stage": "solve", "node": "author", "reason": reason,
                  "state": {"statement": "Task", "handoff_refused": reason, "paused": True}}
        feed(app, [record], code=0, now=1_000)
        checkpoint = json.loads((app.run_dir / server.PAUSE_FILENAME).read_text())
        self.assertEqual((checkpoint["node"], checkpoint["pauseCause"]), ("author", "error"))
        self.assertEqual(checkpoint["autoResumeAt"], 1_000 + server.AUTO_RESUME_SECONDS)
        self.assertEqual(app.state["phase"], "paused")
        # The resumed runner starts at the author with the saved state, so the reason reaches the author.
        command, state = self.server_resume(app)
        self.assertEqual(command[command.index("--start-node") + 1], "author")
        self.assertEqual(state, {"statement": "Task", "handoff_refused": reason})

    def test_a_checkpoint_rebuilt_after_a_crash_keeps_the_reason_only_until_the_next_submission(self):
        # When the resumed runner, or the server itself, ends without pausing, the server rebuilds the
        # author's checkpoint from the state Resume saved: it keeps the reason only until the author
        # submits again, as the runner does.
        self.author_submits("PROOF_FILE: proof.md")
        self.run_proof({"statement": "Task"})
        reason = self.pause()["reason"]
        # The refused submission itself comes before the Resume: it is not the next submission.
        refusal = [{"kind": "author_result", "stage": "solve", "node": "author", "label": "Author solution",
                    "text": "PROOF_FILE: proof.md", "threadId": "thread-1"}, self.pause()]
        report = {"verdict": "reject", "solution": PROOF, "bugs": "Step 2 fails."}
        working = [{"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input",
                    "text": "Continue.\n\n" + self.note(reason), "threadId": "thread-1"}]
        repairing = [*working,
                     {"kind": "author_result", "stage": "solve", "node": "author", "label": "Author solution",
                      "text": "PROOF_FILE: proof.md", "threadId": "thread-1"},
                     {"kind": "status", "stage": "critic", "node": "critic", "label": "Workflow step: critic"},
                     {"kind": "critic_result", "stage": "critic", "node": "critic", "round": 1, "report": report},
                     {"kind": "request", "stage": "repair", "node": "author", "label": "Exact solve input",
                      "text": "Correct the errors.", "threadId": "thread-1"}]
        for records, stage, kept in ((working, "solve", reason), (repairing, "repair", None)):
            for ended in ("runner", "server"):
                with self.subTest(stage=stage, ended=ended):
                    app = server_app(self.root / stage / ended)
                    feed(app, refusal, code=0, now=1_000)
                    self.assertEqual(self.server_resume(app)[1]["handoff_refused"], reason)
                    if ended == "runner":
                        feed(app, records, code=-9, now=2_000)  # Killed, for example out of memory.
                    else:
                        # The server ends with the runner: its records are saved, its checkpoint says running.
                        for record in records:
                            app.add_trace(record)
                        app = server.restore_saved_app(server.App(app.trace_file, app.runs))
                    checkpoint = json.loads((app.run_dir / server.PAUSE_FILENAME).read_text())
                    self.assertEqual((checkpoint["status"], checkpoint["node"], checkpoint["stage"]),
                                     ("paused", "author", stage))
                    self.assertEqual(checkpoint["state"].get("handoff_refused"), kept)
                    # The next Resume tells the author the reason only before its next submission.
                    self.assertEqual(self.server_resume(app)[1].get("handoff_refused"), kept)


class StepPauseTests(GraphCase):
    """I-002 and I-023: a later step pauses with its exact input instead of ending the run."""

    def test_an_unexpected_failure_pauses_the_critic_with_its_captured_input(self):
        (self.root / "proof.md").write_text("The submitted proof.")
        with mock.patch.object(runtime, "structured", side_effect=RuntimeError("An unexpected harness error.")):
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP],
                                              {"statement": "Task", "solution": "PROOF_FILE: proof.md"},
                                              {"start_node": "critic"})
        pause = self.kinds("workflow_paused")[-1]
        self.assertEqual((pause["node"], pause["reason"]), ("critic", "An unexpected harness error."))
        self.assertNotIn("safetyStop", pause)
        self.assertEqual(state["solution"], "The submitted proof.")
        self.assertTrue(state["paused"])
        step = self.kinds("node_input")[-1]
        self.assertEqual(step["node"], "critic")
        self.assertEqual(step["state"]["solution"], "The submitted proof.")

    def test_configuration_and_safety_errors_pause_without_automatic_retry(self):
        for error, start in ((runtime.ConfigurationError("Add an API key for Gateway."), "Add an API key"),
                             (runtime.SafetyStop("Claude CLI enabled unexpected tools (Artifact)."),
                              "Stopped for safety: Claude CLI enabled unexpected tools")):
            self.events.clear()
            with self.subTest(error=type(error).__name__), mock.patch.object(runtime, "structured", side_effect=error):
                runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": PROOF},
                                          {"start_node": "critic"})
            pause = self.kinds("workflow_paused")[-1]
            self.assertEqual(pause["node"], "critic")
            self.assertTrue(pause["safetyStop"])
            self.assertTrue(pause["reason"].startswith(start))
            self.assertTrue(pause["reason"].endswith(runtime.SAFETY_STOP_HINT))

    def test_a_full_disk_is_retried_in_place_after_the_result_is_recorded(self):
        real, failures = runtime._private_atomic_write, []

        def write(path, content, **kwargs):
            if Path(path).name == "saved-candidate.md" and content.startswith("Edited") and len(failures) < 2:
                failures.append(path)
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, content, **kwargs)

        report = {"verdict": "pass", "solution": "Edited proof.", "bugs": ""}
        clean = {"verdict": "pass", "solution": "Edited proof.", "bugs": ""}
        with mock.patch.object(runtime, "_private_atomic_write", side_effect=write), \
                mock.patch.object(runtime, "structured",
                                  side_effect=critic_doubles.coordinator([(report, "{}"), (clean, "{}")])):
            state = runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": PROOF},
                                              {"start_node": "critic"})
        self.assertEqual(state["output"], "Edited proof.")
        self.assertEqual((self.root / "saved-candidate.md").read_text(), "Edited proof.")
        self.assertEqual(self.waits(), 120)
        notices = [event for event in self.kinds("status") if event.get("label") == runtime.STORAGE_RETRY_LABEL
                   and "retryAt" in event]
        self.assertEqual([event["text"] for event in notices],
                         ["Could not save saved-candidate.md (disk full) — retrying in 1 min (attempt 2).",
                          "Could not save saved-candidate.md (disk full) — retrying in 1 min "
                          "(attempt 3, retrying for 1 min)."])
        # The critic's result was recorded before the write that failed.
        first_result = next(index for index, event in enumerate(self.events) if event["kind"] == "critic_result")
        self.assertLess(first_result, self.events.index(notices[0]))
        self.assertEqual([event["budgetClock"] for event in self.kinds("status") if "budgetClock" in event],
                         ["paused", "running", "paused", "running"])

    def test_a_pause_request_during_a_storage_wait_pauses_the_step_with_its_input(self):
        pause_file = self.root / "pause-request.json"

        def write(path, content, **kwargs):
            raise OSError(errno.EIO, "Input/output error")

        def sleep(seconds, cancel=None):
            pause_file.write_text("{}")

        with mock.patch.object(runtime, "_private_atomic_write", side_effect=write), \
                mock.patch.object(runtime, "_retry_sleep", side_effect=retry_test.guarded(sleep, limit=5)), \
                mock.patch.object(runtime, "structured") as model:
            state = runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": PROOF},
                                              {"start_node": "critic", "goal_pause_file": str(pause_file)})
        model.assert_not_called()
        self.assertEqual(self.kinds("workflow_paused")[-1]["node"], "critic")
        self.assertEqual(state, {"statement": "Task", "solution": PROOF, "paused": True,
                                 "proof_handoff": state["proof_handoff"]})

    def test_a_command_whose_files_cannot_be_written_is_retried(self):
        results = [OSError(errno.EIO, "Input/output error"), {"status": "pass", "output": "", "returncode": 0}]
        with mock.patch.object(runtime, "_run_command", side_effect=results) as command:
            state = runtime.execute_workflows([CLEAN_UP], {"solution": PROOF, "output": LATEX},
                                              {"start_node": "latex_compile"})
        self.assertEqual(command.call_count, 2)
        self.assertEqual(state["output"], LATEX)
        self.assertEqual(self.waits(), 60)


class ResumeAnyStepTests(GraphCase):
    """I-122: a checkpoint at any node resumes in the graph that contains it."""

    def test_a_latex_checkpoint_starts_in_the_latex_graph(self):
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=retry_test.AuthorStarted("no model")), \
                mock.patch.object(runtime, "_run_command",
                                  return_value={"status": "pass", "output": "", "returncode": 0}) as command:
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task", "output": LATEX},
                                              {"start_node": "latex_compile"})
        command.assert_called_once()
        self.assertEqual(state["output"], LATEX)
        steps = [event["node"] for event in self.kinds("status") if str(event.get("label")).startswith("Workflow step")]
        self.assertEqual(steps, ["latex_compile"])
        with self.assertRaisesRegex(ValueError, "Unknown workflow entry node: 'nowhere'"):
            runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {}, {"start_node": "nowhere"})

    def test_a_resumed_critic_reviews_the_text_it_captured_without_resolving_it_again(self):
        proof = "Lemma 1 holds.\nPROOF_FILE: appendix.md\nHence the theorem."
        (self.root / "proof.md").write_text(proof)
        with mock.patch.object(runtime, "structured", side_effect=RuntimeError("Interrupted.")):
            runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": "PROOF_FILE: proof.md"},
                                      {"start_node": "critic"})
        checkpoint = json.loads(json.dumps(self.kinds("workflow_paused")[-1]["state"]))
        self.assertEqual(checkpoint["solution"], proof)
        reviewed = []

        def review(prompt, schema, stage, **settings):
            reviewed.append(dict(settings["input_files"])["proof.md"])
            return {"verdict": "pass", "solution": proof, "bugs": ""}, "{}"

        with mock.patch.object(runtime, "structured", side_effect=critic_doubles.coordinator(review)):
            state = runtime.execute_workflows([AUTHOR_CRITIC], checkpoint, {"start_node": "critic"})
        self.assertEqual(reviewed, [proof])
        self.assertEqual(state["output"], proof)


class TimeLimitTests(GraphCase):
    """I-043 and I-044: unlimited by default, and the limit stops only the author."""

    def test_unlimited_is_the_default_and_any_positive_limit_is_allowed(self):
        self.assertEqual(runtime.DEFAULT_AUTHOR_HOURS, 0)
        self.assertIsNone(runtime.MAX_AUTHOR_HOURS)
        self.assertEqual(runtime.author_hours(10_000), 10_000)
        with self.assertRaisesRegex(runtime.Error, "a positive number of hours"):
            runtime.author_hours(-1)
        settings = server.PUBLIC_GRAPH["settings"]["thinking_hours"]
        self.assertEqual((settings["default"], settings["maximum"]), (0, None))
        app = server.App(runs=self.root / "runs")
        self.assertEqual(app.state["thinkingHours"], 0)
        app.state.update(phase="paused", activeNode="author", stage="solve")
        with mock.patch.object(app, "_write_author_limit"), mock.patch.object(app, "_save_job_settings"), \
                mock.patch.object(app, "add_trace"):
            self.assertEqual(app.set_author_time_limit(1_000), 1_000)
        with self.assertRaisesRegex(ValueError, "a positive number of hours"):
            app.set_author_time_limit(-2)

    def test_an_exhausted_limit_never_cuts_off_the_critic_or_latex(self):
        options = {"start_node": "critic", "thinking_hours": 1, "elapsed_seconds": 7200}
        report = {"verdict": "pass", "solution": PROOF, "bugs": ""}
        latex = {"summary": "Wrote it.", "latex": LATEX}
        with mock.patch.object(runtime, "structured",
                               side_effect=critic_doubles.coordinator([(report, "{}"), (latex, "{}")])) as model, \
                mock.patch.object(runtime, "_run_command",
                                  return_value={"status": "pass", "output": "", "returncode": 0}) as command:
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task", "solution": PROOF},
                                              options)
        self.assertEqual(state["output"], LATEX)
        self.assertNotIn("paused", state)
        for call in model.call_args_list:
            # Only the default stall limit applies, and no deadline.
            self.assertIsNone(call.kwargs.get("timeout"))
            self.assertTrue(math_inf(runtime.workflow_remaining(call.kwargs["options"])))
        self.assertEqual(command.call_args.args[0]["timeout"],
                         runtime.builtin_workflow("clean_up")["nodes"]["latex_compile"]["command"]["timeout"])

    def test_an_exhausted_limit_still_lets_a_timed_out_compile_run_with_longer_limits(self):
        limits = []

        def compile(argv, timeout, **kwargs):
            limits.append(timeout)
            if timeout < 1800:
                raise subprocess.TimeoutExpired(argv, timeout, output="[1] [2]")
            (self.root / "final.pdf").write_bytes(b"%PDF-1.5\n%%EOF")
            return subprocess.CompletedProcess(argv, 0, "Output written on final.pdf")

        latex = {"summary": "Wrote it.", "latex": LATEX}
        with mock.patch.object(runtime, "structured", return_value=(latex, "{}")), \
                mock.patch.object(runtime.shutil, "which", return_value="/tools/tex"), \
                mock.patch.object(runtime, "_run_bounded", side_effect=compile):
            state = runtime.execute_workflows([CLEAN_UP], {"statement": "Task", "solution": PROOF},
                                              {"thinking_hours": 1, "elapsed_seconds": 7200})
        # The compile's own longer limits (latex package) still apply; the spent budget cuts none of them.
        self.assertEqual(limits, [300, 900, 1800])
        self.assertEqual(state["output"], LATEX)
        self.assertEqual((self.root / "final.tex").read_text(), LATEX)

    def test_a_rejection_after_the_limit_pauses_at_the_author(self):
        report = {"verdict": "reject", "solution": PROOF, "bugs": "Lemma 2 fails."}
        with mock.patch.object(runtime, "structured", side_effect=critic_doubles.coordinator([(report, "{}")])):
            state = runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task", "solution": PROOF},
                                              {"start_node": "critic", "thinking_hours": 1, "elapsed_seconds": 7200})
        pause = self.kinds("workflow_paused")[-1]
        self.assertEqual((pause["node"], pause["reason"]), ("author", "Workflow time limit reached."))
        self.assertEqual(state["report"]["bugs"], "Lemma 2 fails.")

    def test_only_an_author_checkpoint_counts_as_an_exhausted_limit(self):
        app = server_app(self.root)
        app.state["thinkingHours"] = 1
        for node, retried in (("author", False), ("critic", True), ("latex_compile", True)):
            checkpoint = {"node": node, "pauseCause": "error", "error": "Connection lost", "elapsedSeconds": 7200}
            app._configure_auto_resume(checkpoint, now=1_000)
            self.assertEqual(bool(checkpoint["autoResumeAt"]), retried, node)

    def test_later_steps_share_their_options_so_a_workflow_note_is_said_once(self):
        # The deadline-free options of the steps after the author keep structured's one-time
        # note that a workflow's attempts setting is ignored (I-007), however often a step runs.
        workflow = self.root / "check.yaml"
        workflow.write_text(runtime.yaml.safe_dump({
            "nodes": {"check": {"run": "structured", "prompt": "check", "attempts": 2,
                                "inputs": {"content": "state.input"},
                                "response": {"statement": "string", "notes": "string"},
                                "after": [{"set": {"output": "result.statement"}}],
                                "next": {"done": {"repeat": 3, "then": "end"}}}},
            "prompts": {"check": "Check {content}"}}))
        answer = json.dumps({"statement": "S", "notes": ""})
        with mock.patch.object(runtime, "run_structured_attempt", return_value=answer) as attempt:
            state = runtime.execute_workflows([workflow], {"input": "S"}, {})
        self.assertEqual((state["output"], attempt.call_count), ("S", 3))
        self.assertEqual(len([event for event in self.kinds("diagnostic") if "sets attempts" in event["text"]]), 1)


class AuthorFailureRouteTests(GraphCase):
    """I-123 (P-3 is moot: upstream keeps only the canonical repair prompt)."""

    def test_the_author_has_no_route_that_ends_the_run(self):
        author = runtime.builtin_workflow("author_critic")["nodes"]["author"]
        self.assertEqual(author["next"], {"done": "critic"})
        self.assertNotIn("failed", json.dumps(author["after"]))

        def failing(module, prompt, **kwargs):
            yield {"outcome": "failure", "output": ""}
        self.guard_author(failing)
        with mock.patch.object(runtime, "structured") as model:
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task"}, {})
        model.assert_not_called()
        self.assertTrue(state["paused"])
        self.assertNotIn("failed", state)
        self.assertEqual(self.kinds("workflow_paused")[-1]["node"], "author")


class StageSelectionTests(GraphCase):
    """The graph decisions with upstream's independently selectable stages."""

    def test_a_rejection_without_the_author_keeps_the_critics_edited_copy(self):
        edited = PROOF + "\nSafe repairs."
        calls, attempt, _ = self.scripted([{"write": edited,
                                            "answer": {"verdict": "reject", "bugs": "Step 3 is unproved."}}])
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task", "solution": PROOF},
                                              {"workflow_stages": {"review": False, "author": False}})
        self.assertEqual([call["stage"] for call in calls], ["critic"])
        self.assertEqual((state["failed"], state["revisionRequired"], state["output"]), (True, True, edited))
        self.assertEqual(self.kinds("critic_result")[-1]["outcome"], "needs_revision")
        result = self.kinds("failure_result")[-1]
        self.assertEqual((result["resultKind"], result["output"], result["bugs"]),
                         ("needs-revision", edited, "Step 3 is unproved."))
        self.assertFalse(self.kinds("final_result"))

    def test_a_verification_without_the_author_ends_as_needing_revision(self):
        calls, attempt, _ = self.scripted([{"write": "P1", "answer": {"verdict": "pass", "bugs": ""}},
                                           {"answer": {"verdict": "reject", "bugs": "Step 3 is unproved."}}])
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt):
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task", "solution": PROOF},
                                              {"workflow_stages": {"author": False}, "critic_rounds": 1})
        self.assertIsNone(calls[1]["edit"])
        self.assertIn("FINAL VERIFICATION", calls[1]["text"])
        self.assertEqual([event["outcome"] for event in self.kinds("critic_result")], ["fixed", "needs_revision"])
        self.assertEqual((state["output"], state["revisionRequired"]), ("P1", True))
        self.assertFalse(any(event.get("acceptanceReason") == "round_limit" for event in self.kinds("status")))

    def test_an_unreadable_submission_without_the_critic_pauses_at_the_author(self):
        def author(module, prompt, **kwargs):
            yield {"outcome": "done", "output": "PROOF_FILE: proof.md"}
        self.guard_author(author)
        with mock.patch.object(runtime, "run_structured_attempt",
                               side_effect=retry_test.AuthorStarted("no writer call may start")):
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task"},
                                              {"workflow_stages": {"critic": False}})
        pause = self.kinds("workflow_paused")[-1]
        self.assertEqual((pause["node"], pause["stage"]), ("author", "solve"))
        self.assertTrue(pause["reason"].startswith("Cannot read the author's submitted proof:"))
        self.assertNotIn("safetyStop", pause)
        self.assertEqual(state, {"statement": "Task", "paused": True, "handoff_refused": pause["reason"]})

    def test_a_supplied_proof_the_critic_cannot_take_waits_at_the_critic(self):
        # Without the author no retry can change the candidate: the step waits for the user.
        with mock.patch.object(runtime, "model_context_tokens", return_value=5), \
                mock.patch.object(runtime, "run_structured_attempt",
                                  side_effect=retry_test.AuthorStarted("no critic call may start")):
            state = runtime.execute_workflows([AUTHOR_CRITIC, CLEAN_UP], {"statement": "Task", "solution": PROOF},
                                              {"workflow_stages": {"author": False}})
        pause = self.kinds("workflow_paused")[-1]
        self.assertEqual(pause["node"], "critic")
        self.assertTrue(pause["safetyStop"])
        self.assertTrue(pause["reason"].startswith("Cannot hand the submitted proof to the critic: the proof is about"))
        self.assertTrue(pause["reason"].endswith(runtime.SAFETY_STOP_HINT))
        # No author resumes, so no reason is kept for one.
        self.assertEqual(state, {"statement": "Task", "solution": PROOF, "paused": True})


class ServerStepTests(unittest.TestCase):
    """I-002 and I-122 in the web server: a runner that died during a step, and Resume at any step."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_a_runner_killed_during_the_critic_pauses_there_and_retries_after_a_minute(self):
        app = server_app(self.root, stage="critic", node="critic")
        state = {"statement": "Task", "solution": "Captured proof."}
        records = [{"kind": "status", "stage": "critic", "label": "Workflow step: critic", "node": "critic", "visit": 1},
                   {"kind": "node_input", "stage": "critic", "node": "critic", "visit": 1, "state": state},
                   {"kind": "request", "stage": "critic", "attempt": 1, "text": "Review."}]
        feed(app, records, code=-9, now=2_000)
        checkpoint = json.loads((app.run_dir / server.PAUSE_FILENAME).read_text())
        self.assertEqual((checkpoint["node"], checkpoint["stage"], checkpoint["state"]), ("critic", "critic", state))
        self.assertEqual(checkpoint["autoResumeAt"], 2_000 + server.AUTO_RESUME_SECONDS)
        self.assertEqual(app.state["phase"], "paused")
        self.assertIn("The runner stopped unexpectedly during the Independent critic step (exit code -9).",
                      app.state["error"])
        # The step input is kept in memory only, never in the transcript.
        self.assertFalse(any(record.get("kind") == "node_input" for record in app.retained_trace()))

    def test_the_step_input_stays_out_of_the_transcript_after_an_event_the_reader_cannot_handle(self):
        # After a record the reader cannot handle (a pause without its state), the runner's other
        # records are still saved (lifecycle I-103), but the step input stays in memory only.
        app = server_app(self.root, stage="critic", node="critic")
        feed(app, [{"kind": "workflow_paused", "stage": "solve", "node": "author"},
                   {"kind": "node_input", "stage": "critic", "node": "critic", "visit": 1,
                    "state": {"statement": "Task", "solution": "Captured proof."}},
                   {"kind": "status", "stage": "critic", "text": "Saved after the failure."}])
        saved = app.trace_file.read_text()
        self.assertIn("could not be handled", saved)
        self.assertIn("Saved after the failure.", saved)
        self.assertNotIn("node_input", saved)
        self.assertFalse(any(record.get("kind") == "node_input" for record in app.retained_trace()))

    def test_a_storage_wait_is_shown_on_the_job_page_like_a_model_retry(self):
        app = server_app(self.root, stage="critic", node="critic")
        text = "Could not save saved-candidate.md (disk full) — retrying in 1 min (attempt 2)."
        app._follow_model_retry({"kind": "status", "stage": "critic", "label": runtime.STORAGE_RETRY_LABEL,
                                 "text": text, "retryAt": 5_000.0, "retryAttempt": 2, "budgetClock": "paused"})
        self.assertEqual(app.state["modelRetry"]["text"], text)
        app._follow_model_retry({"kind": "status", "stage": "critic", "label": runtime.STORAGE_RETRY_LABEL,
                                 "text": "Saving saved-candidate.md again now (attempt 2).", "budgetClock": "running"})
        self.assertIsNone(app.state["modelRetry"])

    def test_a_resumed_step_whose_runner_dies_before_reporting_its_input_pauses_there(self):
        for node, stage, error in (("latex_compile", "final", ""), ("critic", "critic", "error: Cannot read a file.")):
            with self.subTest(node=node):
                app = server_app(self.root / node)
                state = {"statement": "Task", "solution": "Proof.", "output": LATEX}
                self.resume(app, node, stage, state)
                # The relaunched runner ends during its start, before it reports the step's input.
                feed(app, [{"kind": "diagnostic", "stage": stage, "text": error}] if error else [], code=-9, now=3_000)
                checkpoint = json.loads((app.run_dir / server.PAUSE_FILENAME).read_text())
                self.assertEqual((checkpoint["status"], checkpoint["node"], checkpoint["stage"], checkpoint["state"]),
                                 ("paused", node, stage, state))
                self.assertEqual(checkpoint["autoResumeAt"], 3_000 + server.AUTO_RESUME_SECONDS)
                self.assertEqual((app.state["phase"], app.state["activeNode"]), ("paused", node))

    def resume(self, app, node, stage="final", state=None, saved=False):
        """Resume `app` at `node`: from the given checkpoint, or with `saved` from the one its reader saved."""

        if not saved:
            app._save_pause({"status": "paused", "node": node, "stage": stage, "state": state or {"output": LATEX},
                             "pauseCause": "error", "autoResumeAt": 0, "elapsedSeconds": 30, "error": "Disk full."})
            app.state.update(phase="paused", stage=stage, activeNode=node)
        for role in ("author", "critic", "final"):
            app._save(f"prompts/{role}.txt", "Prompt.\n")
        with mock.patch.object(server.subprocess, "Popen", return_value=mock.Mock(stdin=io.StringIO())) as popen, \
                mock.patch.object(app, "_spawn_worker"), mock.patch.object(app, "_check_launch_models_locked"):
            app.resume()
        return popen.call_args.args[0]

    def test_a_later_critic_round_resumes_with_the_visit_its_input_records(self):
        # The runner's input of round 2 records its visit (see runtime._step_visit); Resume passes it back unchanged,
        # after a pause and after a runner that died during the step.
        state = {"statement": "Task", "solution": "Repaired proof.", "round": 1,
                 runtime.STEP_VISIT_KEY: {"node": "critic", "visit": 2, "verify": "critic_verify"}}
        step = [{"kind": "status", "stage": "critic", "label": "Workflow step: critic", "node": "critic", "visit": 2},
                {"kind": "node_input", "stage": "critic", "node": "critic", "visit": 2, "state": state}]
        paused = {"kind": "workflow_paused", "stage": "critic", "node": "critic", "state": {**state, "paused": True},
                  "reason": "Codex critic failed: Selected model is at capacity."}
        for name, records, code in (("paused", [*step, paused], 0), ("runner died", step, -9)):
            with self.subTest(name):
                app = server_app(self.root / name, stage="critic", node="critic")
                feed(app, records, code=code)
                self.assertEqual((app.state["phase"], app.state["activeNode"]), ("paused", "critic"))
                command = self.resume(app, "critic", saved=True)
                self.assertEqual(command[command.index("--start-node") + 1], "critic")
                self.assertEqual(json.loads((app.run_dir / "workflow-input.json").read_text()), state)

    def test_resume_at_a_latex_step_starts_there_for_every_kind_of_job(self):
        app = server_app(self.root)
        command = self.resume(app, "latex_compile")
        # Only the LaTeX graph remains, with only the writer's settings.
        self.assertEqual([Path(argument).name for argument in command if argument.endswith(".yaml")],
                         ["clean_up.yaml"])
        self.assertEqual(command[command.index("--start-node") + 1], "latex_compile")
        self.assertNotIn("--author-model", command)
        self.assertNotIn("--critic-model", command)
        # Without the editor's version, a repair keeps the content of the source being compiled
        # (latex_kept), as after "Retry LaTeX compilation"; a saved editor's version is kept.
        self.assertEqual(json.loads((app.run_dir / "workflow-input.json").read_text()),
                         {"output": LATEX, "edited_latex": LATEX})
        edited = server_app(self.root / "edited")
        self.resume(edited, "latex_repair", state={"output": LATEX, "edited_latex": "The editor's version."})
        self.assertEqual(json.loads((edited.run_dir / "workflow-input.json").read_text())["edited_latex"],
                         "The editor's version.")
        command = self.resume(server_app(self.root / "critic"), "critic", "critic", {"statement": "Task",
                                                                                     "solution": PROOF})
        self.assertEqual([Path(argument).name for argument in command if argument.endswith(".yaml")],
                         ["author_critic.yaml", "clean_up.yaml"])
        self.assertEqual(command[command.index("--start-node") + 1], "critic")
        latex_only = server_app(self.root / "latex")
        latex_only.state["problemMode"] = "latex"
        command = self.resume(latex_only, "latex_repair")
        self.assertEqual([Path(argument).name for argument in command if argument.endswith(".yaml")],
                         ["clean_up.yaml"])
        self.assertEqual(command[command.index("--start-node") + 1], "latex_repair")
        self.assertNotIn("--elapsed-seconds", command)
        self.assertNotIn("--author-prompt-file", command)


class ResumeRolesTests(unittest.TestCase):
    """A paused step checks only the models of the roles its remaining steps can use."""

    REMOVED = "api:removed-gateway"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        # An empty API-model store: every api: model id is one the user has removed.
        store = mock.patch.dict(os.environ, {"TCS_PROVER_API_MODELS": str(self.root / "state" / "api-models.json")})
        store.start()
        self.addCleanup(store.stop)

    def paused(self, name, node, stage, **models):
        app = server_app(self.root / name, stage=stage, node=node)
        app.state.update(models)
        app.add_trace({"kind": "status", "stage": stage, "node": node, "label": f"Workflow step: {node}"})
        for role in ("author", "critic", "final"):
            app._save(f"prompts/{role}.txt", "Prompt.\n")
        app._save_pause({"status": "paused", "node": node, "stage": stage, "pauseCause": "error", "autoResumeAt": 900,
                         "elapsedSeconds": 30, "error": "Disk full.",
                         "state": {"statement": "Task", "solution": PROOF, "output": LATEX}})
        app.state.update(phase="paused", autoResumeAt=900)
        app._save_job_settings(app.state)
        return app

    def auto_resume(self, app):
        with mock.patch.object(server.subprocess, "Popen", return_value=mock.Mock(stdin=io.StringIO())) as popen, \
                mock.patch.object(app, "_spawn_worker"):
            app.auto_resume_if_due(app.resume, now=1_000)
        return popen

    def test_a_latex_step_needs_only_the_writer_model(self):
        app = self.paused("latex", "latex_compile", "final", authorModel=self.REMOVED, criticModel=self.REMOVED)
        # A server restart keeps its automatic retry.
        restored = server.restore_saved_app(server.App(app.trace_file, app.runs))
        self.assertEqual((restored.state["phase"], restored.state["activeNode"]), ("paused", "latex_compile"))
        self.assertTrue(restored.state["autoResumeAt"])
        popen = self.auto_resume(app)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual([Path(argument).name for argument in command if argument.endswith(".yaml")],
                         ["clean_up.yaml"])
        self.assertEqual(command[command.index("--start-node") + 1], "latex_compile")
        self.assertEqual(command[command.index("--writer-model") + 1], runtime.WRITER_MODEL)
        self.assertNotIn("--author-model", command)
        self.assertNotIn("--critic-model", command)
        self.assertEqual(app.state["phase"], "running")

    def test_a_removed_writer_model_still_keeps_a_latex_step_from_starting(self):
        # The job checks again every minute until the model is configured again (I-087).
        app = self.paused("writer", "latex_editor", "final", writerModel=self.REMOVED)
        restored = server.restore_saved_app(server.App(app.trace_file, app.runs))
        self.assertIn("removed-gateway", restored.state["settingsWarning"])
        popen = self.auto_resume(app)
        popen.assert_not_called()
        self.assertEqual(app.state["autoResumeAt"], 1_000 + server.AUTO_RESUME_SECONDS)
        self.assertIn("removed-gateway", app.state["waitingForSettings"])
        with self.assertRaisesRegex(ValueError, "removed-gateway"):
            app.resume()

    def test_only_the_selected_stages_need_their_models(self):
        self.assertEqual(server.checkpoint_roles("author"), ("author", "critic", "writer"))
        self.assertEqual(server.checkpoint_roles("latex_compile"), ("writer",))
        self.assertEqual(server.checkpoint_roles("author", {"critic": False, "writing": False}), ("author",))
        self.assertEqual(server.checkpoint_roles("critic", {"author": False}), ("critic", "writer"))
        app = self.paused("author-only", "author", "solve", criticModel=self.REMOVED, writerModel=self.REMOVED,
                          workflowStages={"review": False, "author": True, "critic": False, "writing": False})
        restored = server.restore_saved_app(server.App(app.trace_file, app.runs))
        self.assertTrue(restored.state["autoResumeAt"])
        popen = self.auto_resume(app)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--start-node") + 1], "author")
        self.assertNotIn("--critic-model", command)
        self.assertNotIn("--writer-model", command)

    def test_a_critic_step_still_needs_the_author_and_writer_models(self):
        # A reject goes back to the author, a pass on to the writer, and the runner checks
        # every model it is given when it starts.
        for role in ("authorModel", "writerModel"):
            with self.subTest(role=role):
                app = self.paused(role, "critic", "critic", **{role: self.REMOVED})
                popen = self.auto_resume(app)
                popen.assert_not_called()
                # Checked again every minute until the model is configured again (I-087).
                self.assertEqual(app.state["autoResumeAt"], 1_000 + server.AUTO_RESUME_SECONDS)
                self.assertIn("removed-gateway", app.state["waitingForSettings"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TimeLimitPageTests(unittest.TestCase):
    """I-043 on the job page: without a maximum, any positive limit can be set."""

    def run_app(self, slices, setup, checks):
        payload = json.dumps({"slices": slices, "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", frontend_test.RUNNER, str(frontend_test.APP)],
                                input=payload, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_limits_above_one_week_are_accepted_and_blank_fields_still_refused(self):
        self.run_app([["ui.setAuthorTimeLimit.onclick =", "ui.sendAuthorSteer.onclick ="],
                      ["function validateWorkflowHours()", "// Read files directly inside the chosen folder"]], r"""
const ui = new Proxy({}, {get: (target, name) => target[name] ||= new Element()});
const show = (element, visible) => { element.hidden = !visible; };
const acted = [];
const act = (path, body) => acted.push({path, body});
""", r"""
ui.authorLimitHours.max = '';
ui.authorLimitHours.value = '1000';
ui.setAuthorTimeLimit.onclick();
assert.deepEqual(acted, [{path: '/set-author-time-limit', body: {hours: 1000}}]);
ui.authorLimitHours.value = ' ';
ui.setAuthorTimeLimit.onclick();
assert.equal(acted.length, 1);
assert.equal(ui.notice.textContent, 'Set 0 for unlimited, or a positive number of hours.');
Object.assign(ui.thinkingHours, {value: '', max: ''});
assert.equal(validateWorkflowHours(), false);
assert.equal(ui.notice.textContent, 'Enter a time limit: 0 for unlimited, or a positive number of hours.');
ui.thinkingHours.value = '500';
assert.equal(validateWorkflowHours(), true);
finish();
""")


def math_inf(value):
    return value == float("inf")


def server_app(root, stage="solve", node="author"):
    app = server.App(runs=Path(root) / "runs")
    app._new_run("Task")
    app._save("checked-statement.md", "# Checked statement\n\nTask\n")
    app.state.update(phase="running", stage=stage, activeNode=node, runId=app.run_dir.name, goalThreadId="thread-1")
    app._save_job_settings(app.state)
    return app


def feed(app, records, code=0, now=1_000):
    """Let App._read_output read one runner's records and exit status."""

    process = mock.Mock(stdout=io.StringIO("".join(json.dumps(record) + "\n" for record in records)))
    process.wait.return_value = code
    process.poll.return_value = code
    token = app.active_token = app.worker_token = object()
    app.process = process
    with mock.patch.object(server.time, "time", return_value=now):
        app._read_output(process, token)


if __name__ == "__main__":
    unittest.main()
