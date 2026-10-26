"""Regression tests for audited workflow-runner defects; no model calls are made."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import workflow_runner as runtime
from ui import server
import test_goal_runtime as goal_test
from test_workflow_runner import LATEX, PROOF, ROOT, review_report, workspace

PROOF_WORKFLOWS = [ROOT / "workflows/author_critic.yaml", ROOT / "workflows/clean_up.yaml"]

# A command whose grandchild keeps running, as pdflatex does under latexmk.
SPAWNER = """
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as stream:
    stream.write(str(grandchild.pid))
print("grandchild started", flush=True)
time.sleep(120)
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed process stays visible as a zombie until it is reaped.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _stop_leftover(pid):
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True).stdout
    if "time.sleep(120)" in command:
        os.kill(pid, signal.SIGKILL)


@unittest.skipIf(os.name == "nt" or not shutil.which("ps"), "POSIX process-tree behavior")
class CommandTreeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.script = self.directory / "spawn.py"
        self.script.write_text(SPAWNER)
        self.pidfile = self.directory / "grandchild.pid"

    def grandchild(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                pid = int(self.pidfile.read_text())
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            self.addCleanup(_stop_leftover, pid)
            return pid
        self.fail("The command never started its grandchild.")

    def assertStops(self, pid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not _alive(pid):
                return
            time.sleep(0.05)
        self.fail(f"Process {pid} outlived its command.")

    def test_timeout_stops_the_whole_command_tree_and_keeps_its_output(self):
        config = {"argv": [sys.executable, str(self.script), str(self.pidfile)], "timeout": 2,
                  "log": "command.log", "result": "build"}
        result = runtime._run_command(config, {"state": {}})
        self.assertEqual(result["status"], "fail")
        self.assertIn("exceeded 2 seconds", result["diagnostic"])
        self.assertIn("grandchild started", result["output"])
        self.assertIn("grandchild started", (self.directory / "command.log").read_text())
        self.assertStops(self.grandchild())

    def test_stopping_the_runner_process_group_still_stops_a_running_command(self):
        # The UI's Stop kills the runner's group; the command must stay inside it.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import workflow_runner as runtime; "
                "runtime._run_command({'argv': [sys.executable, sys.argv[2], sys.argv[3]], "
                "'timeout': 120, 'result': 'build'}, {'state': {}})")
        names = {runtime.retrieval_runtime.POLICY_ENV, runtime.retrieval_runtime.MODE_ENV}
        environment = {key: value for key, value in os.environ.items() if key not in names}
        runner = subprocess.Popen(
            [sys.executable, "-B", "-c", code, str(ROOT), str(self.script), str(self.pidfile)],
            cwd=self.directory, env=environment, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(runner.wait)
        self.addCleanup(lambda: runner.poll() is None and os.killpg(runner.pid, signal.SIGKILL))
        pid = self.grandchild()
        os.killpg(runner.pid, signal.SIGKILL)
        runner.wait(timeout=10)
        self.assertStops(pid)

    def test_bounded_run_returns_the_exit_status_and_complete_output(self):
        result = runtime._run_bounded([sys.executable, "-c", "print('done'); raise SystemExit(3)"],
                                      timeout=30, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True)
        self.assertEqual((result.returncode, result.stdout), (3, "done\n"))


@unittest.skipIf(os.name == "nt", "Uses a POSIX shell script as the stand-in Codex CLI")
class EarlyCodexExitTests(unittest.TestCase):
    def test_large_prompt_reports_the_codex_error_instead_of_a_broken_pipe(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name).resolve()
        fake = folder / "codex"
        fake.write_text("#!/bin/sh\necho \"error: unexpected argument '--ignore-user-config' found\" >&2\nexit 2\n")
        fake.chmod(0o700)
        (folder / "workspace").mkdir()
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(folder),
                       "CODEX_HOME": str(folder / "codex-home")}
        previous = Path.cwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, previous)
        # Larger than any pipe buffer, like a critic prompt embedding a long proof.
        with patch.object(runtime, "codex", return_value=str(fake)), patch.object(runtime, "emit"):
            with self.assertRaises(runtime.Error) as failure:
                runtime._run_structured_process(
                    "x" * 300_000, {"type": "object"}, "critic", runtime.MODEL, runtime.EFFORT,
                    runtime.DEFAULT_SPEED, "concise", timeout=60, activity_label=None,
                    features=(), policy=None, child_environment=environment,
                    workspace=folder / "workspace")
        self.assertIn("Codex critic failed", str(failure.exception))
        self.assertIn("unexpected argument '--ignore-user-config'", str(failure.exception))


class TextExpressionTests(unittest.TestCase):
    def test_text_in_expressions_raises_wrapped_value_errors(self):
        for value in ("", "  \n", 0, None, ["proof"]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, r"text\(\) requires nonempty text"):
                runtime.evaluate("text(result.output)", {"result": {"output": value}})
        self.assertEqual(runtime.evaluate("text(result.output)", {"result": {"output": "  Proof \n"}}), "Proof")
        # Direct Python callers keep the statement-specific failure.
        with self.assertRaisesRegex(runtime.Error, "The problem statement is empty."):
            runtime.text("  ")

    def test_command_requirement_reports_the_node_error(self):
        node = {"run": "command", "require": ["text(result.output)"], "error": "The build printed nothing.",
                "outcome": "result.status", "next": {"pass": "end"}}
        for output in ("", 7):
            with self.subTest(output=output), self.assertRaisesRegex(runtime.Error, r"^The build printed nothing\.$"):
                runtime._complete_node(node, {}, {"status": "pass", "output": output}, "", 1, 0)

    def test_structured_requirement_reports_the_node_error(self):
        node = {"run": "structured", "prompt": "ask", "require": ["text(result.note)"],
                "error": "The reviewer returned no note.",
                "schema": {"type": "object", "properties": {"note": {"type": ["string", "integer"]}}}}
        for note in ("  ", 4):
            with self.subTest(note=note), patch.object(runtime, "structured", return_value=({"note": note}, "")):
                with self.assertRaisesRegex(runtime.Error, r"^The reviewer returned no note\.$"):
                    runtime._model_call(node, {"ask": "Question"}, {}, {}, 1)

    def test_cli_reports_a_failed_text_requirement_without_a_traceback(self):
        workflow = {"prompts": {}, "nodes": {"build": {
            "run": "command", "command": {"argv": ["builder"], "timeout": 5, "result": "build"},
            "require": ["text(result.returncode)"], "error": "The build printed nothing.",
            "outcome": "result.status", "next": {"pass": "end"},
        }}}
        with workspace() as directory, patch.object(runtime, "_run_command", return_value={"status": "pass", "output": "", "returncode": 0}):
            (directory / "build.yaml").write_text(runtime.yaml.safe_dump(workflow))
            (directory / "state.json").write_text("{}")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = runtime.main([str(directory / "build.yaml"), "--state-file", str(directory / "state.json")])
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue().strip(), "error: The build printed nothing.")


class ResumedStateTests(unittest.TestCase):
    @staticmethod
    def model(prompt, schema, stage, **settings):
        value = review_report() if stage == "critic" else {"latex": LATEX}
        return value, json.dumps(value)

    def paused_state(self):
        # The state recorded by a workflow_paused event.
        return {"statement": "Task", "solution": PROOF, "paused": True, "failed": False}

    def test_resuming_a_paused_checkpoint_runs_every_graph(self):
        state = self.paused_state()
        with workspace() as directory, patch.object(runtime, "structured", side_effect=self.model) as calls, patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}):
            result = runtime.execute_workflows(PROOF_WORKFLOWS, state, {"start_node": "critic"})
            self.assertEqual((directory / "final.tex").read_text(), LATEX)
        self.assertEqual([call.args[2] for call in calls.call_args_list], ["critic", "final"])
        self.assertEqual(result["output"], LATEX)
        self.assertNotIn("paused", result)
        self.assertNotIn("failed", result)

    def test_cli_resume_from_a_paused_checkpoint_emits_the_workflow_result(self):
        with workspace() as directory, patch.object(runtime, "structured", side_effect=self.model), patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}):
            (directory / "paused.json").write_text(json.dumps(self.paused_state()))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = runtime.main([*map(str, PROOF_WORKFLOWS), "--state-file", str(directory / "paused.json"),
                                     "--start-node", "critic"])
        self.assertEqual(code, 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(record["kind"] == "final_result" for record in records))
        self.assertIn({"kind": "workflow_result", "stage": "workflow", "output": LATEX}, records)

    def test_single_graph_result_does_not_keep_a_stale_paused_flag(self):
        state = {"source": LATEX, "paused": True}
        with workspace(), patch.object(runtime, "structured", side_effect=self.model), patch.object(runtime, "_run_command", return_value={"status": "pass", "output": ""}):
            result = runtime.execute(ROOT / "workflows/clean_up.yaml", state)
        self.assertEqual(result["output"], LATEX)
        self.assertNotIn("paused", result)


class CriticPointerTests(unittest.TestCase):
    def review(self, answers, reviewed):
        def model(prompt, schema, stage, **settings):
            reviewed.append(dict(settings["input_files"])["proof.md"])
            report = review_report(answers[len(reviewed) - 1])
            return report, json.dumps(report)
        return model

    def test_pointer_answer_stops_before_replacing_the_saved_candidate(self):
        with workspace() as directory:
            (directory / "proof.md").write_text("Real proof from the author file")
            state = {"statement": "Task", "solution": "PROOF_FILE: proof.md"}
            reviewed = []
            with patch.object(runtime, "structured", side_effect=self.review(["PROOF_FILE: proof.md"], reviewed)):
                with self.assertRaisesRegex(runtime.Error, "file pointer instead of the complete proof"):
                    runtime.execute_workflows(PROOF_WORKFLOWS, state, {"start_node": "critic"})
            self.assertEqual(reviewed, ["Real proof from the author file"])
            self.assertEqual((directory / "saved-candidate.md").read_text(), "Real proof from the author file")
            self.assertEqual(state["solution"], "Real proof from the author file")
            self.assertNotIn("output", state)

    def test_pointer_at_the_round_limit_never_becomes_the_accepted_proof(self):
        with workspace() as directory:
            (directory / "proof.md").write_text("Author proof")
            state = {"statement": "Task", "solution": "PROOF_FILE: proof.md"}
            reviewed = []
            answers = ["Critic repaired proof", "PROOF_FILE: proof.md"]
            with patch.object(runtime, "structured", side_effect=self.review(answers, reviewed)):
                with self.assertRaisesRegex(runtime.Error, "file pointer"):
                    runtime.execute_workflows(PROOF_WORKFLOWS, state, {"start_node": "critic", "critic_rounds": 2})
            self.assertEqual(reviewed, ["Author proof", "Critic repaired proof"])
            self.assertEqual((directory / "saved-candidate.md").read_text(), "Critic repaired proof")
            self.assertFalse((directory / "final-input.json").exists())
            self.assertNotIn("output", state)

    def test_unchanged_candidate_that_contains_a_marker_line_can_still_pass(self):
        proof = "Lemma 1 holds.\nPROOF_FILE: appendix.md\nHence the theorem."
        with workspace() as directory:
            (directory / "proof.md").write_text(proof)
            reviewed = []
            with patch.object(runtime, "structured", side_effect=self.review([proof], reviewed)):
                state = runtime.execute_workflows([PROOF_WORKFLOWS[0]],
                    {"statement": "Task", "solution": "PROOF_FILE: proof.md"}, {"start_node": "critic"})
        self.assertEqual(reviewed, [proof])
        self.assertEqual(state["output"], proof)

    def test_edited_complete_proof_that_keeps_a_marker_line_is_reviewed_again(self):
        body = "\n".join(f"Step {index}: a careful argument for case {index}." for index in range(60))
        for line in ("`PROOF_FILE: proof.md`", "PROOF_FILE: proof.md"):
            captured = f"# Proof\n\n{body}\n\nQED.\n\n{line}"
            edited = captured.replace("Step 3:", "Step 3 (corrected):")
            with self.subTest(line=line), workspace() as directory:
                (directory / "proof.md").write_text(captured)
                reviewed = []
                with patch.object(runtime, "structured", side_effect=self.review([edited, edited], reviewed)):
                    state = runtime.execute_workflows([PROOF_WORKFLOWS[0]],
                        {"statement": "Task", "solution": "PROOF_FILE: proof.md"}, {"start_node": "critic"})
                # The second round reviews the critic's edit, never the author's file.
                self.assertEqual(reviewed, [captured, edited])
                self.assertEqual(state["output"], edited)

    def test_a_pointer_the_critic_writes_itself_still_stops(self):
        captured = "Lemma 1 holds.\n`PROOF_FILE: proof.md`\nHence the theorem."
        for answer in ("`PROOF_FILE: proof.md`", "PROOF_FILE: other.md", "Lemma 1 holds.\nPROOF_FILE: proof.md\nHence."):
            with self.subTest(answer=answer), workspace() as directory:
                (directory / "proof.md").write_text(captured)
                with patch.object(runtime, "structured", side_effect=self.review([answer], [])):
                    with self.assertRaisesRegex(runtime.Error, "file pointer"):
                        runtime.execute_workflows([PROOF_WORKFLOWS[0]],
                            {"statement": "Task", "solution": "PROOF_FILE: proof.md"}, {"start_node": "critic"})

    def test_resuming_the_critic_with_a_long_saved_candidate_reviews_it_inline(self):
        candidate = "\n".join(f"Step {index}: case {index} follows." for index in range(100)) + "\n\n`PROOF_FILE: proof.md`"
        with workspace() as directory:
            reviewed = []
            with patch.object(runtime, "structured", side_effect=self.review([candidate], reviewed)):
                state = runtime.execute_workflows([PROOF_WORKFLOWS[0]], {"statement": "Task", "solution": candidate},
                                                  {"start_node": "critic"})
            self.assertFalse((directory / "proof.md").exists())
        self.assertEqual(reviewed, [candidate])
        self.assertEqual(state["output"], candidate)

    def test_an_unresolvable_code_span_pointer_is_reviewed_and_repaired_as_text(self):
        feedback, reviewed = [], []

        def author(module, prompt, **kwargs):
            feedback.append((yield {"outcome": "done", "output": "`PROOF_FILE: missing.md`"}))
            yield {"outcome": "done", "output": PROOF}

        def model(prompt, schema, stage, **settings):
            files = dict(settings["input_files"])
            reviewed.append((files["proof.md"], json.loads(files["manifest.json"])["source"]["kind"]))
            report = review_report(PROOF, verdict="reject" if len(reviewed) == 1 else "pass")
            return report, json.dumps(report)

        with workspace(), patch.object(runtime, "goal_session", side_effect=author), \
                patch.object(runtime, "structured", side_effect=model):
            state = runtime.execute_workflows([PROOF_WORKFLOWS[0]], {"statement": "Task"})
        self.assertEqual(reviewed, [("`PROOF_FILE: missing.md`", "inline"), (PROOF, "inline")])
        self.assertEqual(feedback[0]["bugs"], "A gap remains.")
        self.assertEqual(state["output"], PROOF)

    def test_repeated_visit_reviews_the_previous_answer_as_text(self):
        workflow = {"prompts": {"review": "Review {solution}"}, "nodes": {"check": {
            "run": "structured", "prompt": "review", "inputs": {"solution": "state.solution"},
            "proof_handoff": {"candidate": "solution", "statement": "statement"},
            "response": {"note": "string"},
            "after": [{"set": {"solution": "'PROOF_FILE: proof.md'"}}],
            "next": {"done": {"repeat": 2, "then": "end"}},
        }}}
        with workspace() as directory:
            (directory / "proof.md").write_text("Author file proof")
            (directory / "check.yaml").write_text(runtime.yaml.safe_dump(workflow))
            reviewed = []

            def model(prompt, schema, stage, **settings):
                files = dict(settings["input_files"])
                reviewed.append((files["proof.md"], json.loads(files["manifest.json"])["source"]["kind"]))
                return {"note": "ok"}, '{"note": "ok"}'

            with patch.object(runtime, "structured", side_effect=model):
                runtime.execute(directory / "check.yaml", {"statement": "Task", "solution": "PROOF_FILE: proof.md"})
        self.assertEqual(reviewed, [("Author file proof", "file"), ("PROOF_FILE: proof.md", "inline")])

    def test_convenience_critic_still_resolves_a_submission_at_a_later_round(self):
        with workspace() as directory:
            (directory / "proof.md").write_text("Submitted proof")
            reviewed = []
            with patch.object(runtime, "structured", side_effect=self.review(["Submitted proof"], reviewed)):
                runtime.criticize("Task", "PROOF_FILE: proof.md", 3)
        self.assertEqual(reviewed, ["Submitted proof"])


class SteerIdTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        popen.start()
        self.addCleanup(popen.stop)

    def steered_session(self, command):
        double = goal_test.Runtime([[]])
        double.command = command

        def deliver(rpc):
            self.assertTrue(double.steered.wait(2))
            rpc.messages.extend(goal_test.success())

        double.on_empty = deliver
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings=goal_test.SETTINGS, options={"goal_cwd": str(self.directory)})
        self.addCleanup(session.close)
        self.assertEqual(next(session)["outcome"], "done")
        return [event for event in double.events if event.get("label") == "Live author instruction sent"]

    def test_sent_live_instruction_carries_its_command_id(self):
        sent = self.steered_session(("steer-7", "Try the invariant from my note."))
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["steerId"], sent[0]["text"]), ("steer-7", "Try the invariant from my note."))

    def test_compaction_re_anchor_has_no_steer_id(self):
        compaction = goal_test.event("item/completed", turnId="turn-1",
                                     item={"type": "contextCompaction", "id": "compact-1"})
        double = goal_test.Runtime([[compaction, *goal_test.success()]])
        session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                       settings=goal_test.SETTINGS, options={"goal_cwd": str(self.directory)})
        self.addCleanup(session.close)
        next(session)
        anchors = [event for event in double.events
                   if event.get("label") == "Author context re-anchor after compaction"]
        self.assertEqual(len(anchors), 1)
        self.assertNotIn("steerId", anchors[0])

    def test_restored_block_is_not_collected_again_on_the_next_continuation(self):
        block = "First instruction\n\nSecond instruction"
        sent = self.steered_session(("restored-1", block))
        with tempfile.TemporaryDirectory() as run:
            records = [{"kind": "status", "stage": "solve", "label": "Restored live instructions queued",
                        "text": block, "steerId": "restored-1",
                        "restoredInstructions": ["First instruction", "Second instruction"]}, *sent]
            Path(run, "transcript.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
            self.assertEqual(server.saved_author_instructions(run), ["First instruction", "Second instruction"])


class PlantedLinkWriteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.workspace = root / "run"
        self.workspace.mkdir()
        self.target = self.workspace / "saved-candidate.md"
        self.outside = root / "outside.rc"
        self.outside.write_text("export SAFE=1\n")
        self.outside.chmod(0o644)

    def assertOutsideUntouched(self):
        self.assertEqual(self.outside.read_text(), "export SAFE=1\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), 0o644)

    def test_planted_destination_link_is_replaced_not_followed(self):
        self.target.symlink_to(self.outside)
        runtime._private_atomic_write(self.target, "Proof text\n")
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.target.read_text(), "Proof text\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertOutsideUntouched()

    def test_link_swapped_in_after_the_rename_is_not_followed(self):
        replace = Path.replace

        def replace_then_swap(source, destination):
            result = replace(source, destination)
            Path(destination).unlink()
            Path(destination).symlink_to(self.outside)
            return result

        with patch.object(Path, "replace", replace_then_swap):
            runtime._private_atomic_write(self.target, "Proof text\n")
        self.assertOutsideUntouched()

    def test_temporary_file_swapped_for_a_link_is_not_followed(self):
        original, outside = tempfile.NamedTemporaryFile, self.outside

        class Swapped:
            def __init__(self, *args, **kwargs):
                self.stream = original(*args, **kwargs)

            def __enter__(self):
                return self.stream.__enter__()

            def __exit__(self, *details):
                result = self.stream.__exit__(*details)
                Path(self.stream.name).unlink()
                Path(self.stream.name).symlink_to(outside)
                return result

        with patch.object(runtime.tempfile, "NamedTemporaryFile", Swapped):
            runtime._private_atomic_write(self.target, "Proof text\n")
        self.assertOutsideUntouched()


class LunaEffortTests(unittest.TestCase):
    def test_luna_models_run_ultra_as_max_and_keep_listed_efforts(self):
        for model in ("gpt-6-luna", "gpt-5.6-luna"):
            with self.subTest(model=model):
                self.assertEqual(runtime.effective_effort(model, "ultra"), "max")
                for effort in runtime.MODEL_EFFORTS[model]:
                    self.assertEqual(runtime.effective_effort(model, effort), effort)
        for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra"):
            self.assertEqual(runtime.effective_effort(model, "ultra"), "ultra")
        # Claude lists no Ultra either; its other levels pass through unchanged.
        self.assertEqual(runtime.effective_effort("claude-opus-5-5", "ultra"), "max")
        self.assertEqual(runtime.effective_effort("claude-opus-5-5", "low"), "low")
        for model, efforts in runtime.MODEL_EFFORTS.items():
            for effort in runtime.EFFORTS:
                self.assertIn(runtime.effective_effort(model, effort), efforts)

    def test_cli_role_settings_and_structured_requests_use_max_for_luna(self):
        options = {"critic_model": "gpt-6-luna", "effort": "ultra"}
        self.assertEqual(runtime._structured_options({"role": "critic"}, options)["effort"], "max")
        with patch.object(runtime, "run_structured_attempt", return_value='{}') as attempt, patch.object(runtime, "emit") as emit:
            runtime.structured("Review", {"type": "object"}, "critic", model="gpt-6-luna", effort="ultra")
        self.assertEqual(attempt.call_args.args[4], "max")
        request = next(call.kwargs for call in emit.call_args_list if call.args[0] == "request")
        self.assertEqual(request["reasoningEffort"], "max")

    def test_resumed_luna_author_starts_its_thread_at_max(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()):
            double = goal_test.Runtime([goal_test.success()])
            double.effective_effort = runtime.effective_effort
            session = runtime.goal_session(double, goal_test.PROMPT, prompts=goal_test.PROMPTS,
                                           settings={**goal_test.SETTINGS, "model": "gpt-6-luna", "effort": "ultra"},
                                           options={"goal_cwd": folder, "goal_thread_id": "thread-1"})
            self.addCleanup(session.close)
            next(session)
        thread = next(params for method, params in double.rpc.calls if method == "thread/resume")
        self.assertEqual(thread["config"]["model_reasoning_effort"], "max")


class BaseUrlInheritanceTests(unittest.TestCase):
    def test_openai_base_url_is_inherited_only_for_the_default_provider(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "config.toml").write_text('openai_base_url = "https://proxy.example/v1"\n')
            environment = {"CODEX_HOME": home, "OPENAI_BASE_URL": ""}
            self.assertEqual(
                runtime.inherited_openai_base_url_arguments(overrides={"model_provider": "openai"}, environment=environment),
                ["-c", 'openai_base_url="https://proxy.example/v1"'])
            for overrides in ({"model_provider": runtime.API_PROVIDER}, {"model_provider": "openai", "openai_base_url": "x"}):
                self.assertEqual(runtime.inherited_openai_base_url_arguments(overrides=overrides, environment=environment), [])


if __name__ == "__main__":
    unittest.main()
