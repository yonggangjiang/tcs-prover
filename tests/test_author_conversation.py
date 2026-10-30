"""A paused author's conversation turn (workflow_runner.py --converse), its prompts and its retries.

Offline: Codex turns run against the scripted RPC double of tests/test_goal_runtime.py,
Claude turns against the stand-in CLI of tests/test_claude_cli.py, and retry waits on a
patched clock. Popen, goal_session and both author_conversation functions are stubbed
wherever a regression could otherwise start a CLI.
"""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import yaml

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, providers

import test_claude_cli as claude_test
import test_goal_runtime as goal_test
from test_model_retry import CAPACITY, FakeClock, patch_clock

ROOT = Path(runtime.__file__).parent
WORKFLOW = ROOT / "workflows/author_critic.yaml"
CONVERSATION_ID = "c_0123456789"
SETTINGS = {"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"}
AUTHOR_ONLY = {"review": False, "author": True, "critic": False, "writing": False}

# The model-facing texts of the specification (§6), character for character.
SUMMARY_TEXT = ("The proof work is paused. Write a progress summary for the user: what you have proved so far, with "
                "precise statements, and where it is written; what remains open or uncertain, and why; and what is "
                "currently achievable, including the most promising next steps. Do not continue the proof work or "
                "change any files in this turn.")
CONVERSATION_TEXT = ("The proof work is paused. Reply to the user's message below. Do not continue the proof work or "
                     "change any files in this turn; the work continues when the user resumes the run.\n\n"
                     "USER MESSAGE:\n{message}")
RELATED_WORKS_TEXT = ("RELATED WORKS:\nThe user has provided related works for this problem in the related-works "
                      "folder of your working directory; related-works/INDEX.md lists them. Read what is relevant. "
                      "The proof you submit must still be complete and self-contained; the critic does not see these "
                      "files.")
RETRY_NOTE_TEXT = ("RETRY AFTER A MODEL-CALL FAILURE\n"
                   "Your previous reply was cut off by a model-call failure. Write that reply again, in full.")
HARNESS_TEXT = ("TCS PROVER HARNESS NOTE (provider plumbing; the message itself arrives in the user turn)\n"
                "The long-running goal is paused while the user talks with you. Reply in plain text; this turn needs "
                "no structured goal status.")


def pairs(command):
    return list(zip(command, command[1:]))


class RPCRefusal(goal_test.TransportError, runtime.RPCError):
    """A JSON-RPC error answer, as the transport of the goal tests' double raises it."""


class Codex(goal_test.Runtime):
    """The goal tests' runtime double: thread/resume answers with ``reopened`` or raises ``resume_failure``."""

    def __init__(self, scripts, reopened="thread-1", resume_failure=None, **attributes):
        super().__init__(scripts)
        self.reopened, self.resume_failure = reopened, resume_failure
        for name, value in attributes.items():
            setattr(self, name, value)

    def RPC(self, process, record, **kwargs):
        rpc = super().RPC(process, record)
        rpc.stderr_log = kwargs.get("stderr_log")
        call = rpc.call

        def scripted(method, params):
            if method == "thread/resume" and self.resume_failure is not None:
                rpc.calls.append((method, params))
                raise self.resume_failure
            result = call(method, params)
            return {"thread": {"id": self.reopened}} if method == "thread/resume" else result

        rpc.call = scripted
        return rpc


def reply(text="Proved Lemma 1; Lemma 2 is open."):
    return [goal_test.answer(text), goal_test.completed()]


class ConversationPromptTests(unittest.TestCase):
    """The YAML prompts of a conversation turn, their validation and the related-works block (§2.1, §2.2, §6)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()

    def custom(self, change):
        definition = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        change(definition)
        path = self.directory / "custom.yaml"
        path.write_text(yaml.safe_dump(definition), encoding="utf-8")
        return path

    def test_the_built_in_prompts_decode_to_the_approved_texts(self):
        workflow = runtime.load_workflow(WORKFLOW)
        self.assertEqual(workflow["nodes"]["author"]["lifecycle"],
                         ["goal", "continuation", "compaction", "repair", "resume", "reopen",
                          "summary", "conversation", "related_works", "handoff_refused", "subagents"])
        prompts = workflow["prompts"]
        self.assertEqual((prompts["summary"], prompts["conversation"], prompts["related_works"]),
                         (SUMMARY_TEXT, CONVERSATION_TEXT, RELATED_WORKS_TEXT))
        self.assertEqual(runtime.CONVERSATION_RETRY_NOTE, RETRY_NOTE_TEXT)
        self.assertEqual(claude_cli.CONVERSATION_HARNESS_NOTE, HARNESS_TEXT)
        # The block styles of the specification: a folded summary, literal conversation and related-works blocks.
        source = WORKFLOW.read_text(encoding="utf-8")
        for name, style in (("summary", ">-"), ("conversation", "|-"), ("related_works", "|-")):
            self.assertIn(f"\n  {name}: {style}\n", source)
        with mock.patch.object(runtime, "require_model_credentials"):
            prepared = runtime.prepare(workflow, {})
        self.assertEqual(prepared["conversation"], CONVERSATION_TEXT)

    def test_load_workflow_accepts_the_new_lifecycle_keys_and_rejects_others(self):
        runtime.load_workflow(self.custom(lambda definition: definition["nodes"]["author"].update(
            lifecycle={**{key: key for key in runtime._GOAL_LIFECYCLE}, "summary": "summary",
                       "conversation": "conversation", "related_works": "related_works"})))
        runtime.load_workflow(self.custom(lambda definition: definition["nodes"]["author"].update(
            lifecycle=list(runtime._GOAL_LIFECYCLE))))
        for unknown in ("message", "summaries", "notice"):
            with self.subTest(unknown=unknown):
                def change(definition):
                    definition["prompts"][unknown] = "Text."
                    definition["nodes"]["author"]["lifecycle"] = [*runtime._GOAL_LIFECYCLE, unknown]
                with self.assertRaisesRegex(ValueError, "must bind its lifecycle prompts"):
                    runtime.load_workflow(self.custom(change))

    def test_only_the_conversation_prompt_may_quote_the_message(self):
        def prepared(change):
            workflow = runtime.load_workflow(self.custom(change))
            with mock.patch.object(runtime, "require_model_credentials"):
                return runtime.prepare(workflow, {})

        self.assertIn("{message}", prepared(lambda definition: None)["conversation"])
        for name in ("summary", "related_works", "resume", "reopen", "repair"):
            with self.subTest(prompt=name), self.assertRaisesRegex(ValueError, "Unknown prompt input: 'message'"):
                prepared(lambda definition: definition["prompts"].update({name: "Quote {message}."}))
        # The recovery prompt is no conversation prompt either.
        with self.assertRaisesRegex(ValueError, "Unknown prompt input: 'message'"):
            prepared(lambda definition: definition["prompts"].update(repair="Repair {bugs} for {message}."))
        with self.assertRaisesRegex(ValueError, "Unknown prompt input: 'reply'"):
            prepared(lambda definition: definition["prompts"].update(conversation="Answer {message} in {reply}."))
        # The goal sessions never render the new prompts: their values have no message.
        values = {"original_prompt": "P", "directory": "/run", "solution": "", "bugs": "", "round": 0,
                  "revision_number": 0, "instruction": ""}
        with self.assertRaisesRegex(ValueError, "message"):
            runtime.render_template(CONVERSATION_TEXT, values)

    def run_author(self, options, prompts=None, workspace=None):
        """_execute with the author stage only and a stub goal session; return the session's prompt and options."""

        workflow = runtime.load_workflow(WORKFLOW)
        prompts = prompts or workflow["prompts"]
        calls = []

        def session(*args, **kwargs):
            calls.append((args, kwargs))
            yield {"outcome": "done", "output": "A complete inline proof."}

        workspace = workspace or self.directory
        with mock.patch.object(runtime, "goal_session", side_effect=session), \
                mock.patch.object(claude_cli, "goal_session", side_effect=AssertionError("Claude session started")), \
                mock.patch.object(runtime, "emit"):
            state = runtime._execute(workflow, {"statement": "Exact statement S."},
                                     {"workflow_stages": AUTHOR_ONLY, "goal_cwd": str(workspace), **options},
                                     prompts)
        self.assertEqual(state["output"], "A complete inline proof.")
        (args, kwargs), = calls
        return args[1], kwargs

    def test_the_first_prompt_points_to_related_works_only_with_the_option(self):
        workflow = runtime.load_workflow(WORKFLOW)
        plain = workflow["prompts"]["author"].replace("[STATEMENT]", "Exact statement S.", 1)
        prompt, _ = self.run_author({})
        self.assertEqual(prompt, plain)
        prompt, kwargs = self.run_author({"related_works": False})
        self.assertEqual(prompt, plain)
        prompt, kwargs = self.run_author({"related_works": True})
        self.assertEqual(prompt, plain + "\n\n" + RELATED_WORKS_TEXT)
        # The goal sessions get every lifecycle prompt but never render the new ones themselves.
        self.assertEqual(kwargs["prompts"]["related_works"], RELATED_WORKS_TEXT)
        # The exact author input of a continuation gets the same block.
        prompt, _ = self.run_author({"related_works": True, "author_input": "Saved exact author input."})
        self.assertEqual(prompt, "Saved exact author input.\n\n" + RELATED_WORKS_TEXT)
        # Web-cutoff authors get it too (owner decision of 27 Oct): the files are the user's local inputs.
        policy = {"isolation": {"workspace": str(self.directory)}}
        with mock.patch.object(runtime, "retrieval_policy", return_value=policy), \
                mock.patch.object(retrieval_runtime, "policy_for_workspace", return_value=policy):
            prompt, _ = self.run_author({"related_works": True})
        self.assertEqual(prompt, plain + "\n\n" + RELATED_WORKS_TEXT)
        # A workflow whose author binds no related_works prompt has no block.
        prompts = dict(workflow["prompts"])
        workflow_without = runtime.load_workflow(self.custom(lambda definition: definition["nodes"]["author"].update(
            lifecycle=[*runtime._GOAL_LIFECYCLE, "reopen"])))
        with mock.patch.object(runtime, "load_workflow", return_value=workflow_without):
            prompt, _ = self.run_author({"related_works": True}, prompts)
        self.assertNotIn("RELATED WORKS", prompt)

    def test_the_recovery_and_resume_prompts_carry_the_block_and_the_saved_prompt_file_is_untouched(self):
        saved = self.directory / "prompts" / "author.txt"
        saved.parent.mkdir()
        saved.write_bytes(b"Saved author prompt [STATEMENT]\r\n")
        state = {"statement": "Exact statement S.", "report": {"verdict": "reject", "bugs": "Gap in step 2."},
                 "solution": "Old proof.", "round": 1}
        workflow = runtime.load_workflow(WORKFLOW)
        calls = []

        def session(*args, **kwargs):
            calls.append((args, kwargs))
            yield {"outcome": "done", "output": "A complete inline proof."}

        with mock.patch.object(runtime, "goal_session", side_effect=session), mock.patch.object(runtime, "emit"):
            runtime._execute(workflow, state, {"workflow_stages": AUTHOR_ONLY, "goal_cwd": str(self.directory),
                                               "related_works": True}, workflow["prompts"])
        (args, kwargs), = calls
        prompt = args[1]
        self.assertTrue(prompt.endswith("\n\n" + RELATED_WORKS_TEXT))
        resume = runtime.render_template(kwargs["prompts"]["resume"], {"original_prompt": prompt})
        self.assertIn(RELATED_WORKS_TEXT, resume)
        self.assertEqual(saved.read_bytes(), b"Saved author prompt [STATEMENT]\r\n")

    def test_a_reopened_codex_thread_gets_the_full_resume_prompt_with_the_block_after_a_compacted_reply(self):
        """The real Codex goal session behind _execute: goal_reanchor and related_works together."""

        workflow = runtime.load_workflow(WORKFLOW)
        process = SimpleNamespace(poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0,
                                  kill=lambda: None)
        sent = {}
        for reanchor in (False, True):
            double = goal_test.Runtime([goal_test.success()])
            with mock.patch.object(runtime, "RPC", lambda process, record, **kwargs: double.RPC(process, record)), \
                    mock.patch.object(runtime, "codex", return_value="codex-test"), \
                    mock.patch.object(runtime.subprocess, "Popen", return_value=process), \
                    mock.patch.object(runtime, "emit"), \
                    mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live"}):
                os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
                runtime._execute(workflow, {"statement": "Exact statement S."},
                                 {"workflow_stages": AUTHOR_ONLY, "goal_cwd": str(self.directory),
                                  "goal_thread_id": "thread-1", "goal_resume": True, "goal_require_resume": True,
                                  "related_works": True, **({"goal_reanchor": True} if reanchor else {})},
                                 workflow["prompts"])
            sent[reanchor] = [params["input"][0]["text"] for method, params in double.rpc.calls
                              if method == "turn/start"]
        self.assertEqual(sent[False], [workflow["prompts"]["reopen"]])
        original = workflow["prompts"]["author"].replace("[STATEMENT]", "Exact statement S.", 1)
        self.assertEqual(sent[True], [runtime.render_template(workflow["prompts"]["resume"], {
            "original_prompt": original + "\n\n" + RELATED_WORKS_TEXT})])

    def test_a_reopened_codex_thread_is_told_why_its_last_submission_was_refused(self):
        """The real Codex goal session behind _execute: the note follows reopen and any critic feedback, once."""

        workflow = runtime.load_workflow(WORKFLOW)
        prompts = workflow["prompts"]
        process = SimpleNamespace(poll=lambda: 0, terminate=lambda: None, wait=lambda timeout=None: 0,
                                  kill=lambda: None)
        reason = "Cannot hand the submitted proof to the critic: (proof.md: file not found)."
        note = runtime.render_template(prompts["handoff_refused"], {"reason": reason})
        rejected = {"report": {"verdict": "reject", "solution": "Old proof.", "bugs": "Gap in step 2."},
                    "solution": "Old proof.", "round": 1}
        repair = runtime.render_template(prompts["repair"], {"solution": "Old proof.", "bugs": "Gap in step 2."})
        received = {"thread": "thread-1", "digest": runtime._repair_digest(repair)}
        for extra, options, expected in (
                ({}, {}, prompts["reopen"] + "\n\n" + note),
                (rejected, {}, prompts["reopen"] + "\n\n" + repair + "\n\n" + note),
                # Feedback the thread already received is not sent again; the note still is.
                (rejected, {"author_repair_delivered": received}, prompts["reopen"] + "\n\n" + note)):
            with self.subTest(rejected=bool(extra), received=bool(options)):
                double, events = goal_test.Runtime([goal_test.success()]), []
                with mock.patch.object(runtime, "RPC", lambda process, record, **kwargs: double.RPC(process, record)), \
                        mock.patch.object(runtime, "codex", return_value="codex-test"), \
                        mock.patch.object(runtime.subprocess, "Popen", return_value=process), \
                        mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: events.append(
                            {"kind": kind, **fields})), \
                        mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live"}):
                    os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
                    state = runtime._execute(workflow, {"statement": "Exact statement S.", "handoff_refused": reason,
                                                        **extra},
                                             {"workflow_stages": AUTHOR_ONLY, "goal_cwd": str(self.directory),
                                              "goal_thread_id": "thread-1", "goal_resume": True,
                                              "goal_require_resume": True, **options}, prompts)
                self.assertEqual([params["input"][0]["text"] for method, params in double.rpc.calls
                                  if method == "turn/start"], [expected])
                # Only the critic's feedback is recorded as delivered, and the submission ends the note.
                self.assertEqual([event["repairDelivered"] for event in events
                                  if event.get("label") == "Critic feedback delivered"],
                                 [received["digest"]] if repair in expected else [])
                self.assertEqual((state["output"], "handoff_refused" in state), ("Full proof", False))


class CodexConversationTests(unittest.TestCase):
    """The Codex conversation turn against the scripted RPC double (§2.5)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = popen.start()
        self.addCleanup(popen.stop)
        environment = mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)

    def converse(self, double, instruction="Summarize your progress.", features=("multi_agent",), settings=None,
                 **options):
        keywords = {key: options.pop(key) for key in ("attempt", "rewind", "classic") if key in options}
        return runtime.author_conversation(
            double, instruction, settings=settings or goal_test.SETTINGS,
            options={"goal_cwd": str(self.directory), "goal_thread_id": "thread-1", **options},
            features=list(features), conversation_id=CONVERSATION_ID, label="Progress summary requested",
            **keywords)

    @staticmethod
    def methods(double):
        return [method for method, _ in double.rpc.calls]

    def test_one_turn_in_the_saved_thread_with_the_goal_kept_paused(self):
        double = Codex([reply()])
        result = self.converse(double)
        self.assertEqual(result, {"reply": "Proved Lemma 1; Lemma 2 is open.", "threadId": "thread-1",
                                  "compacted": False})
        # Codex's settings are read first: the user's MCP servers, plugins and apps are turned off by name.
        self.assertEqual(self.methods(double), ["initialize", "initialized", "config/read", "thread/resume",
                                                "thread/goal/set", "turn/start", "thread/goal/set"])
        resumed = double.rpc.calls[3][1]
        self.assertEqual((resumed["threadId"], resumed["excludeTurns"], resumed["cwd"]),
                         ("thread-1", True, str(self.directory)))
        self.assertEqual([params for method, params in double.rpc.calls if method == "thread/goal/set"],
                         [{"threadId": "thread-1", "status": "paused"}] * 2)
        turn = next(params for method, params in double.rpc.calls if method == "turn/start")
        # This double has no confined profile: Codex's legacy read-only sandbox, whose network the turn sets.
        self.assertEqual(turn, {"threadId": "thread-1", "cwd": str(self.directory), "summary": "concise",
                                "input": [{"type": "text", "text": "Summarize your progress."}],
                                "sandboxPolicy": {"type": "readOnly", "networkAccess": True}})
        for never in ("thread/start", "turn/steer", "thread/goal/clear", "thread/fork", "turn/interrupt"):
            self.assertNotIn(never, self.methods(double))
        self.assertTrue(double.stopped)
        # Every record is the conversation's, at the author.
        self.assertTrue(double.events)
        for event in double.events:
            self.assertEqual((event["stage"], event["node"], event["conversationId"]),
                             ("conversation", "author", CONVERSATION_ID))
        statuses = [(event["label"], event.get("text")) for event in double.events if event["kind"] == "status"]
        self.assertEqual(statuses, [("Conversation reopened", "Thread thread-1")])
        request, = [event for event in double.events if event["kind"] == "request"]
        self.assertEqual(request, {"kind": "request", "stage": "conversation", "node": "author",
                                   "conversationId": CONVERSATION_ID, "label": "Progress summary requested",
                                   "text": "Summarize your progress.", "threadId": "thread-1", "model": "test-model",
                                   "reasoningEffort": "high", "reasoningSummary": "concise"})
        methods = [event["event"]["method"] for event in double.events if event["kind"] == "codex_event"]
        self.assertEqual(methods, ["turn/started", "item/completed", "turn/completed"])
        self.assertTrue(all(event["root"] for event in double.events if event["kind"] == "codex_event"))

    def test_later_attempts_name_their_number(self):
        double = Codex([reply()])
        self.converse(double, attempt=3)
        request, = [event for event in double.events if event["kind"] == "request"]
        self.assertEqual(request["attempt"], 3)

    def test_live_runs_reply_read_only_without_subagents(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), mock.patch.object(runtime.sys, "platform", platform):
                double = Codex([reply()], private_permissions=staticmethod(runtime.private_permissions))
                self.converse(double, settings=SETTINGS, features=["multi_agent", "example_feature"])
                command = self.spawn.call_args.args[0]
                self.assertIn(("--disable", "multi_agent"), pairs(command))
                self.assertNotIn("--enable", command[command.index("goals") + 1:])
                self.assertEqual(self.spawn.call_args.kwargs["cwd"], str(self.directory))
                resumed = next(params for method, params in double.rpc.calls if method == "thread/resume")
                # Codex lists its collaboration tools to some models even with multi_agent off: a session
                # limit of one thread (the root) refuses every spawn.
                self.assertEqual(resumed["config"]["features"],
                                 {"goals": True, "multi_agent": False, "fast_mode": False, "apps": False,
                                  "plugins": False, "view_image": False,
                                  "multi_agent_v2": {"max_concurrent_threads_per_session": 1}})
                self.assertIn(("-c", "features.multi_agent_v2.max_concurrent_threads_per_session=1"),
                              pairs(command))
                self.assertIn("config/read", self.methods(double))
                if platform == "darwin":
                    self.assertNotIn("sandbox", resumed)
                    self.assertEqual(resumed["permissions"], runtime.PRIVATE_PERMISSIONS)
                    self.assertEqual(resumed["config"]["default_permissions"], runtime.PRIVATE_PERMISSIONS)
                    profile = resumed["config"]["permissions"][runtime.PRIVATE_PERMISSIONS]
                    # The confined profile with a read-only run folder that, in a live run, reaches the internet.
                    self.assertEqual(profile, runtime.private_permissions(":read-only", SETTINGS["model"], live=True))
                    self.assertEqual(profile["filesystem"][":workspace_roots"]["."], "read")
                    self.assertEqual(profile["network"], {"enabled": True})
                else:
                    self.assertEqual(resumed["sandbox"], "read-only")
                    self.assertNotIn("permissions", resumed)

    def test_the_switch_gives_live_runs_the_authors_own_tools(self):
        with mock.patch.object(runtime.sys, "platform", "linux"):
            double = Codex([reply()], CONVERSATION_READ_ONLY=False)
            self.converse(double)
        command = self.spawn.call_args.args[0]
        self.assertIn(("--enable", "multi_agent"), pairs(command))
        self.assertNotIn(("--disable", "multi_agent"), pairs(command))
        resumed = next(params for method, params in double.rpc.calls if method == "thread/resume")
        self.assertEqual(resumed["sandbox"], "workspace-write")
        self.assertTrue(resumed["config"]["features"]["multi_agent"])

    def test_the_reply_is_the_final_answer_or_else_the_last_text(self):
        commentary = goal_test.answer("Reading my notes.", "commentary")
        double = Codex([[commentary, goal_test.answer("The final reply."),
                         goal_test.answer("A closing remark.", "commentary"), goal_test.completed()]])
        self.assertEqual(self.converse(double)["reply"], "The final reply.")
        double = Codex([[commentary, goal_test.answer("Only commentary.", "commentary"), goal_test.completed()]])
        self.assertEqual(self.converse(double)["reply"], "Only commentary.")
        final = {"type": "agentMessage", "phase": "final_answer", "text": "From the turn's items."}
        double = Codex([[goal_test.completed(items=[final])]])
        self.assertEqual(self.converse(double)["reply"], "From the turn's items.")

    def test_a_compacted_context_is_reported_and_never_re_anchored(self):
        compaction = goal_test.event("item/completed", turnId="turn-1",
                                     item={"type": "contextCompaction", "id": "compact-1"})
        double = Codex([[compaction, *reply("After compaction.")]])
        result = self.converse(double)
        self.assertEqual((result["reply"], result["compacted"]), ("After compaction.", True))
        self.assertNotIn("turn/steer", self.methods(double))
        self.assertEqual([method for method in self.methods(double) if method == "turn/start"], ["turn/start"])

    def test_a_missing_or_gone_conversation_is_final_and_never_replaced(self):
        for saved in ("", "  ", None):
            with self.subTest(saved=saved):
                self.spawn.reset_mock()
                with self.assertRaisesRegex(runtime.SavedConversationGone, "has not started its conversation yet"):
                    self.converse(Codex([]), goal_thread_id=saved)
                self.spawn.assert_not_called()
        double = Codex([], resume_failure=goal_test.TransportError("thread/resume failed: thread not found"))
        with self.assertRaisesRegex(runtime.SavedConversationGone,
                                    r"Codex no longer has the saved author conversation \(.*thread not found\)\."):
            self.converse(double)
        self.assertIsInstance(runtime.SavedConversationGone("x"), runtime.FinalError)
        self.assertNotIn("thread/start", self.methods(double))
        self.assertNotIn("turn/start", self.methods(double))
        self.assertTrue(double.stopped)

    def test_a_different_thread_is_a_safety_stop(self):
        double = Codex([reply()], reopened="thread-other")
        with self.assertRaisesRegex(runtime.SafetyStop, "different thread ID"):
            self.converse(double)
        self.assertNotIn("turn/start", self.methods(double))
        self.assertNotIn("thread/goal/set", self.methods(double))  # Never touches the other thread.
        double = Codex([reply()], reopened=None)
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            self.converse(double)
        self.assertIn("without a thread ID", str(raised.exception))

    def test_a_refusal_to_reopen_is_reported_as_such_and_transient_failures_are_retried(self):
        refusal = RPCRefusal("thread/resume failed: the thread is locked by another client")
        with self.assertRaises(runtime.ConversationRefused) as raised:
            self.converse(Codex([], resume_failure=refusal))
        self.assertEqual(raised.exception.refusal, "thread/resume failed: the thread is locked by another client")
        self.assertFalse(raised.exception.retry["delivered"])
        for failure in (RPCRefusal("thread/resume failed: 503 Service Unavailable"),
                        goal_test.TransportError("thread/resume failed: connection reset by peer")):
            with self.subTest(failure=str(failure)), self.assertRaises(runtime.ConversationAttemptFailed) as raised:
                self.converse(Codex([], resume_failure=failure))
            self.assertNotIsInstance(raised.exception, runtime.ConversationRefused)
        with self.assertRaises(runtime.SafetyStop):
            self.converse(Codex([], resume_failure=runtime.SafetyStop("Stopped.")))

    def test_failed_attempts_say_how_the_next_one_continues(self):
        cases = {
            # Codex took the message and the model produced nothing: that turn is removed next time.
            "empty reply": ([goal_test.completed()], {"delivered": True, "output": False, "rewind": "turn-1"},
                            "The author's turn ended without a reply."),
            "failed turn": ([goal_test.completed("failed")], {"delivered": True, "output": False, "rewind": "turn-1"},
                            "Author reply turn failed: no complete result"),
            "provider error": ([goal_test.event("error", error={"message": "usage limit reached"}, willRetry=False)],
                               {"delivered": True, "output": False, "rewind": "turn-1"}, "usage limit reached"),
            # The model worked before the failure: the next attempt asks for the reply again.
            "cut off": ([goal_test.answer("Half a reply", "commentary"), goal_test.completed("interrupted")],
                        {"delivered": True, "output": True, "rewind": None}, "Author reply turn interrupted"),
            "tool runtime": ([runtime.ToolRuntimeUnavailable("Required model tool runtime is unavailable: code "
                                                             "mode is unavailable")],
                             {"delivered": True, "output": False, "rewind": "turn-1", "classic": True},
                             "code mode is unavailable"),
        }
        for name, (script, retry, message) in cases.items():
            with self.subTest(name):
                double = Codex([script])
                with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
                    self.converse(double)
                self.assertNotIsInstance(raised.exception, runtime.FinalError)
                self.assertTrue(runtime.retryable_failure(raised.exception))
                self.assertIn(message, str(raised.exception))
                self.assertEqual(raised.exception.retry, {"classic": False, **retry})
                self.assertTrue(double.stopped)
                self.assertEqual(double.rpc.calls[-1], ("thread/goal/set", {"threadId": "thread-1", "status": "paused"}))
        # A failure before the message reached Codex.
        double = Codex([[]])
        double.RPC = lambda process, record, **kwargs: (_ for _ in ()).throw(OSError("pipe broken"))
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            self.converse(double)
        self.assertEqual(raised.exception.retry, {"delivered": False, "output": False, "rewind": None,
                                                  "classic": False})

    def test_codex_reconnecting_is_shown_and_the_turn_continues(self):
        retrying = goal_test.event("error", error={"message": "Reconnecting... 2/5"}, willRetry=True)
        double = Codex([[retrying, *reply()]])
        self.assertEqual(self.converse(double)["reply"], "Proved Lemma 1; Lemma 2 is open.")
        status = next(event for event in double.events if event.get("label") == "Codex is reconnecting")
        self.assertTrue(status["text"].startswith("Codex is reconnecting (2/5)"))

    def test_a_stalled_turn_is_a_retryable_failure_named_after_its_stall(self):
        double = Codex([[]], AUTHOR_INACTIVITY_SECONDS=0.05, AUTHOR_PROBE_SECONDS=0.05)

        def hang(rpc):
            for _ in range(500):
                if double.stopped:
                    raise goal_test.TransportError("Process stopped")
                threading.Event().wait(0.01)
            raise AssertionError("The watcher never stopped the stalled app-server.")

        double.on_empty = hang
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            self.converse(double)
        self.assertRegex(str(raised.exception), r"^Codex stopped responding: no event for \d+ s and no answer to a "
                                                r"check within 0 s\.$")
        self.assertEqual(raised.exception.retry["rewind"], "turn-1")
        self.assertIn(("thread/goal/get", {"threadId": "thread-1"}), double.rpc.calls)

    def test_a_failed_empty_turn_is_removed_before_the_message_is_sent_again(self):
        double = Codex([reply()])
        self.converse(double, rewind="turn-7")
        self.assertEqual(self.methods(double)[4:7], ["thread/revert", "thread/goal/set", "turn/start"])
        self.assertEqual(double.rpc.calls[4][1], {"threadId": "thread-1", "beforeTurnId": "turn-7"})
        removed = next(event for event in double.events if event.get("label") == "Failed turn removed")
        self.assertEqual(removed["text"], "The failed reply turn ended before the model produced anything; it was "
                                          "removed and the message is sent again.")
        # A revert Codex refuses is noted, and the message is sent all the same.
        double = Codex([reply()])
        call = double.RPC

        def refusing(process, record, **kwargs):
            rpc = call(process, record, **kwargs)
            scripted = rpc.call
            rpc.call = lambda method, params: ((_ for _ in ()).throw(goal_test.TransportError("turn not found"))
                                               if method == "thread/revert" else scripted(method, params))
            return rpc

        double.RPC = refusing
        self.assertEqual(self.converse(double, rewind="turn-7")["reply"], "Proved Lemma 1; Lemma 2 is open.")
        self.assertIn("Could not remove the failed turn (turn not found); sending the message again.",
                      [event.get("text") for event in double.events if event["kind"] == "diagnostic"])

    def test_a_goal_that_cannot_be_set_paused_is_noted(self):
        double = Codex([reply()])
        call = double.RPC

        def failing(process, record, **kwargs):
            rpc = call(process, record, **kwargs)
            scripted = rpc.call
            rpc.call = lambda method, params: ((_ for _ in ()).throw(goal_test.TransportError("no goal"))
                                               if method == "thread/goal/set" else scripted(method, params))
            return rpc

        double.RPC = failing
        self.assertEqual(self.converse(double)["reply"], "Proved Lemma 1; Lemma 2 is open.")
        self.assertIn("Could not confirm that the goal is paused: no goal",
                      [event.get("text") for event in double.events if event["kind"] == "diagnostic"])

    def test_the_classic_shell_and_the_stderr_log_follow_the_author(self):
        log = self.directory / "codex-stderr.log"
        double = Codex([reply()])
        self.converse(double, classic=True, goal_stderr_log=str(log))
        command = self.spawn.call_args.args[0]
        self.assertIn(("--disable", "code_mode"), pairs(command))
        self.assertIn(("--enable", "shell_tool"), pairs(command))
        self.assertEqual(double.rpc.stderr_log, str(log))
        resumed = next(params for method, params in double.rpc.calls if method == "thread/resume")
        self.assertEqual((resumed["config"]["features"]["code_mode"], resumed["config"]["features"]["shell_tool"]),
                         (False, True))


class HistoricalConversationTests(unittest.TestCase):
    """A web-cutoff run's conversation uses the author's unchanged isolated launch (§9)."""

    POLICY = {"isolation": {"workspace": "/run"}, "runId": "P01"}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.child_environment = {"CODEX_HOME": str(self.directory / "codex-home/author")}
        self.locked = {"approval_policy": "never", "default_permissions": retrieval_runtime.PROFILE,
                       "web_search": "disabled", "features": {"multi_agent": True, "shell_tool": True}}
        for target, name, value in (
                (runtime, "retrieval_policy", mock.Mock(return_value=self.POLICY)),
                (retrieval_runtime, "codex_executable", mock.Mock(return_value="/pinned/codex")),
                (retrieval_runtime, "preflight", mock.Mock(side_effect=lambda *args: dict(self.child_environment))),
                (retrieval_runtime, "configuration", mock.Mock(side_effect=lambda policy, role: json.loads(
                    json.dumps(self.locked))))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def launch(self, run):
        """Run `run(double)` with Popen and verify_rpc recorded; return (double, argv, Popen keywords, checks)."""

        double = Codex([goal_test.success()])
        checks = []

        def verified(rpc, policy, role, environment, allow_runtime_overrides=False, agent_limits=None):
            # Recorded among the app-server calls, so its place before turn/start shows.
            rpc.calls.append(("verify_rpc", {"role": role, "environment": environment,
                                             "overrides": allow_runtime_overrides}))
            checks.append(role)

        with mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace()) as spawn, \
                mock.patch.object(retrieval_runtime, "verify_rpc", side_effect=verified):
            run(double)
        return double, spawn.call_args.args[0], spawn.call_args.kwargs, checks

    def test_the_conversation_launch_equals_the_authors_and_is_verified_before_its_turn(self):
        options = {"goal_cwd": str(self.directory), "goal_thread_id": "thread-1"}

        def author(double):
            session = runtime.goal_session(double, "Prove S.", prompts=goal_test.PROMPTS, settings=SETTINGS,
                                           options=options, features=["multi_agent"])
            self.assertEqual(next(session)["outcome"], "done")
            session.close()

        def conversation(double):
            double.scripts.appendleft(reply())
            self.assertEqual(runtime.author_conversation(
                double, "Summarize.", settings=SETTINGS, options={**options, "goal_stderr_log": "/run/log"},
                features=["multi_agent"], conversation_id=CONVERSATION_ID, label="Progress summary requested",
                classic=True)["reply"], "Proved Lemma 1; Lemma 2 is open.")

        goal_double, goal_argv, goal_keywords, _ = self.launch(author)
        double, argv, keywords, checks = self.launch(conversation)
        # The same pinned binary, --strict-config, features and environment; classic is ignored.
        self.assertEqual(argv, goal_argv)
        self.assertEqual(argv[:2], ["/pinned/codex", "--strict-config"])
        self.assertIn(("--enable", "multi_agent"), pairs(argv))
        self.assertNotIn("code_mode", argv)
        self.assertEqual(keywords, goal_keywords)
        self.assertEqual(keywords["env"], self.child_environment)
        # The same historical profile.
        resumed = lambda calls: next(params for method, params in calls if method == "thread/resume")
        self.assertEqual(resumed(double.rpc.calls), resumed(goal_double.rpc.calls))
        self.assertEqual(resumed(double.rpc.calls)["permissions"], retrieval_runtime.PROFILE)
        self.assertNotIn("sandbox", resumed(double.rpc.calls))
        # No turn sandbox policy replaces it (a live run's legacy read-only turn sets one).
        self.assertNotIn("sandboxPolicy", next(params for method, params in double.rpc.calls if method == "turn/start"))
        # Verified at the handshake and again right before the turn; no stderr log in the run folder.
        names = [method for method, _ in double.rpc.calls]
        self.assertEqual(names[:7], ["initialize", "initialized", "verify_rpc", "thread/resume", "thread/goal/set",
                                     "verify_rpc", "turn/start"])
        self.assertEqual(checks, ["author", "author"])
        self.assertTrue(all(params["overrides"] and params["environment"] == self.child_environment
                            for method, params in double.rpc.calls if method == "verify_rpc"))
        self.assertIsNone(double.rpc.stderr_log)

    def test_claude_never_converses_for_a_web_cutoff_run(self):
        with mock.patch.object(claude_cli, "SessionProcess", side_effect=AssertionError("Claude started")):
            with self.assertRaisesRegex(runtime.ConfigurationError, "Web cutoff timestamps require Codex"):
                claude_cli.author_conversation(
                    runtime, "Summarize.", settings={**SETTINGS, "model": "claude-opus-5-5"},
                    options={"goal_cwd": str(self.directory), "goal_thread_id": str(uuid.uuid4())},
                    features=["multi_agent"], conversation_id=CONVERSATION_ID, label="Progress summary requested")


def claude_reply(text="The summary.", *before):
    return ["init", "echo", *before,
            {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": str(uuid.uuid4()),
                      "message": {"content": [{"type": "text", "text": "Working notes."}]}}},
            {"result": {"result": text}}]


class ClaudeConversationTests(claude_test.FakeClaudeCase):
    """The Claude conversation turn against the stand-in CLI (§2.6)."""

    def setUp(self):
        super().setUp()
        self.session = str(uuid.uuid4())  # A session of this test alone: no other test's CLI holds it.

    def converse(self, instruction="Summarize your progress.", features=("multi_agent",), **options):
        keywords = {key: options.pop(key) for key in ("attempt", "rewind") if key in options}
        return claude_cli.author_conversation(
            runtime, instruction, settings={"model": "claude-opus-5-5", "effort": "max", "speed": "standard",
                                            "summary": "concise"},
            options={"goal_cwd": str(self.run_dir), "goal_thread_id": self.session, **options},
            features=list(features), conversation_id=CONVERSATION_ID, label="Message to the paused author",
            **keywords)

    def held_cli(self):
        """A process whose command line looks like an author CLI that an earlier runner left on the session."""

        held = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--resume", self.session,
                                 "--input-format", "stream-json", "--replay-user-messages",
                                 "--append-system-prompt-file", f"/private/run-1/{claude_cli.HARNESS_FILE}"],
                                stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (held.kill(), held.wait()))
        threading.Thread(target=held.wait, daemon=True).start()  # Reaped as soon as it ends.
        return held

    def transcript(self, *records):
        """A saved conversation of the stand-in CLI: Claude Code's own transcript format."""

        path = self.root / "claude-config" / "projects" / "-fake-project" / f"{self.session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps({"sessionId": self.session, **record}) + "\n" for record in records))
        return path

    def test_one_turn_resumes_the_saved_session_read_only_with_the_conversation_note(self):
        self.script([claude_reply("The summary.")])
        result = self.converse()
        self.assertEqual(result, {"reply": "The summary.", "threadId": self.session, "compacted": False})
        start, = self.records("start")
        argv = start["argv"]
        self.assertEqual(argv[:2], ["--resume", self.session])
        for flag in ("--session-id", "--fork-session", "--json-schema", "--resume-session-at",
                     "--no-session-persistence"):
            self.assertNotIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,WebSearch,WebFetch")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,WebSearch,WebFetch")
        self.assertIn("--replay-user-messages", argv)
        self.assertEqual(start["harness"], HARNESS_TEXT)
        self.assertEqual(Path(start["harness_path"]).name, claude_cli.HARNESS_FILE)
        self.assertEqual(start["harness_mode"], "0o600")
        self.assertNotIn("sandbox", start.get("settings") or {})
        self.assertEqual(self.texts(), ["Summarize your progress."])
        self.assertEqual(Path(start["cwd"]), self.run_dir)
        for event in self.events:
            self.assertEqual((event["stage"], event["node"], event["conversationId"]),
                             ("conversation", "author", CONVERSATION_ID))
        self.assertEqual(self.labels("status"), ["Conversation reopened"])
        request, = [event for event in self.events if event["kind"] == "request"]
        self.assertEqual((request["label"], request["text"], request["threadId"], request["model"],
                          request["reasoningEffort"]),
                         ("Message to the paused author", "Summarize your progress.", self.session,
                          "claude-opus-5-5", "max"))
        texts = [event["event"]["params"]["item"].get("text") for event in self.events
                 if event["kind"] == "codex_event"]
        self.assertEqual(texts, ["Working notes."])
        self.assertTrue(all(event["provider"] == "claude" for event in self.events if event["kind"] == "codex_event"))
        self.assertPrivateFilesRemoved()

    def test_without_the_switch_the_author_keeps_its_own_tools(self):
        self.script([claude_reply()])
        with mock.patch.object(runtime, "CONVERSATION_READ_ONLY", False), \
                mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            self.converse()
        argv = self.records("start")[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Task,Bash,Read,Write,Edit,TodoWrite,WebSearch,WebFetch")

    def test_the_reply_falls_back_to_the_last_assistant_text(self):
        self.script([claude_reply("")])
        self.assertEqual(self.converse()["reply"], "Working notes.")
        self.script([["init", "echo", {"result": {"result": "  "}}]])
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            self.converse()
        self.assertIn("The author's turn ended without a reply.", str(raised.exception))

    def test_a_compacted_context_is_reported_without_a_re_anchor(self):
        compacted = {"emit": {"type": "system", "subtype": "compact_boundary", "uuid": "cb-1",
                              "compact_metadata": {"trigger": "auto", "pre_tokens": 190000}}}
        self.script([claude_reply("After compaction.", compacted)])
        result = self.converse()
        self.assertEqual((result["reply"], result["compacted"]), ("After compaction.", True))
        self.assertEqual(self.texts(), ["Summarize your progress."])
        self.assertNotIn("Author context re-anchor after compaction", self.labels("request"))

    def test_a_conversation_claude_lost_is_restored_once_or_else_unavailable(self):
        copies = claude_cli._run_folder(providers.CLAUDE_TRANSCRIPTS, self.run_dir, runtime)
        saved = copies / "-fake-project" / f"{self.session}.jsonl"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps({"type": "assistant", "uuid": str(uuid.uuid4()), "parentUuid": None,
                                     "sessionId": self.session}) + "\n")
        self.script([claude_reply("Restored.")], transcripts_required=True)
        self.assertEqual(self.converse()["reply"], "Restored.")
        self.assertEqual([start["argv"][:2] for start in self.records("start")], [["--resume", self.session]] * 2)
        self.assertIn("Saved conversation restored", self.labels("status"))
        # Gone even after the copy was put back: final, and never a new conversation.
        self.log.unlink()
        self.events.clear()
        self.script([claude_reply()], all_resumes_gone=True)
        restores = []

        def restore(*args):
            # Once only: a second restore would loop for as long as Claude lacks the conversation.
            restores.append(args)
            if len(restores) > 1:
                raise AssertionError("The saved conversation was restored more than once.")
            return True

        with mock.patch.object(claude_cli, "restore_transcript", side_effect=restore):
            with self.assertRaisesRegex(runtime.SavedConversationGone,
                                        r"^Claude Code no longer has the saved author conversation\.$"):
                self.converse()
        self.assertEqual(len(restores), 1)
        starts = self.records("start")
        self.assertEqual([start["argv"][:2] for start in starts], [["--resume", self.session]] * 2)
        self.assertFalse(any("--session-id" in start["argv"] for start in starts))
        self.assertNotIn("Conversation replaced", self.labels("status"))
        # Without a private copy it is unavailable at once.
        self.log.unlink()
        saved.unlink()
        self.script([claude_reply()], all_resumes_gone=True)
        with self.assertRaises(runtime.SavedConversationGone):
            self.converse()
        self.assertEqual(len(self.records("start")), 1)

    def test_a_missing_or_foreign_session_id_is_final(self):
        for saved, message in (("", "has not started its conversation yet"),
                               ("thread-1", "not a Claude session ID")):
            with self.subTest(saved=saved), self.assertRaisesRegex(runtime.SavedConversationGone, message):
                claude_cli.author_conversation(
                    runtime, "Summarize.", settings={"model": "claude-opus-5-5", "effort": "max",
                                                     "speed": "standard", "summary": "concise"},
                    options={"goal_cwd": str(self.run_dir), "goal_thread_id": saved}, features=[],
                    conversation_id=CONVERSATION_ID, label="Progress summary requested")
        self.assertEqual(self.records(), [])

    def test_a_copy_opened_instead_stops_a_leftover_cli_or_fails_for_a_retry(self):
        copy = str(uuid.uuid4())
        self.script([[{"init": {"session_id": copy}}, {"hang": 5}, {"exit": 1}]])
        with mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 0.5, create=True):
            with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
                self.converse()
        self.assertIn("it is open in another Claude Code process", str(raised.exception))
        self.assertNotIsInstance(raised.exception, runtime.ConversationRefused)
        self.assertFalse(raised.exception.retry["delivered"])
        self.assertNotIn("Conversation reopened", self.labels("status"))
        # A CLI an earlier runner left on the conversation is stopped, and the turn starts again.
        held = self.held_cli()
        self.log.unlink()
        self.script([[{"init": {"session_id": copy}}], claude_reply("Reopened.")])
        with mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 1, create=True):
            self.assertEqual(self.converse()["reply"], "Reopened.")
        deadline = time.monotonic() + 5
        while held.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(held.poll())
        self.assertIn(f"(PID {held.pid})", " ".join(event.get("text", "") for event in self.events
                                                    if event["kind"] == "diagnostic"))
        self.assertEqual([start["argv"][:2] for start in self.records("start")], [["--resume", self.session]] * 2)

    def test_a_relaunch_starts_the_turn_afresh(self):
        """What a CLI that opened a copy received never counts for the conversation the turn was meant for."""

        self.held_cli()
        copy = str(uuid.uuid4())
        self.script([["echo", {"init": {"session_id": copy}}, {"hang": 5}, {"exit": 1}],
                     [{"stderr": "fatal: crashed"}, {"exit": 1}]])
        with mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 1, create=True):
            with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
                self.converse()
        self.assertIn("fatal: crashed", str(raised.exception))
        self.assertEqual(raised.exception.retry, {"delivered": False, "output": False, "rewind": None,
                                                  "classic": False})
        self.assertEqual([start["argv"][:2] for start in self.records("start")], [["--resume", self.session]] * 2)

    def test_a_refusal_to_reopen_is_reported_as_such(self):
        refusal = [{"stderr": f"Error: session {self.session} is locked by another client"}, {"exit": 1}]
        self.script([refusal])
        with self.assertRaises(runtime.ConversationRefused) as raised:
            self.converse()
        self.assertEqual(raised.exception.refusal, f"Error: session {self.session} is locked by another client")
        self.script([[{"stderr": "Not logged in · Please run /login"}, {"exit": 1}]])
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            self.converse()
        self.assertNotIsInstance(raised.exception, runtime.ConversationRefused)
        self.assertIn("Claude CLI stopped unexpectedly.", str(raised.exception))

    def test_a_failed_turn_after_the_message_arrived_rewinds_to_the_last_answer(self):
        anchor = str(uuid.uuid4())
        self.transcript({"type": "user", "uuid": str(uuid.uuid4()), "parentUuid": None},
                        {"type": "assistant", "uuid": anchor, "parentUuid": None})
        self.script([["init", "echo", {"api_error": "API Error: 529 Overloaded"},
                      {"result": {"is_error": True, "result": "API Error: 529 Overloaded", "api_error_status": 529}}]])
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            self.converse()
        self.assertIn("Claude reply turn failed: API Error: 529 Overloaded", str(raised.exception))
        self.assertEqual(raised.exception.retry, {"delivered": True, "output": False, "rewind": anchor,
                                                  "classic": False})
        # The next attempt resumes before the failed turn and sends the message again.
        self.log.unlink()
        self.events.clear()
        self.script([claude_reply("Second try.")])
        self.assertEqual(self.converse(rewind=anchor, attempt=2)["reply"], "Second try.")
        argv = self.records("start")[0]["argv"]
        self.assertEqual(argv[:4], ["--resume", self.session, "--resume-session-at", anchor])
        self.assertIn("Failed turn removed", self.labels("status"))
        request, = [event for event in self.events if event["kind"] == "request"]
        self.assertEqual(request["attempt"], 2)

    def test_the_retry_loop_resumes_a_failed_empty_claude_turn_before_it(self):
        """run_author_conversation with the real Claude turn: is_error, then --resume-session-at and the same message."""

        anchor = str(uuid.uuid4())
        self.transcript({"type": "user", "uuid": str(uuid.uuid4()), "parentUuid": None},
                        {"type": "assistant", "uuid": anchor, "parentUuid": None})
        overloaded = ["init", "echo", {"api_error": "API Error: 529 Overloaded"},
                      {"result": {"is_error": True, "result": "API Error: 529 Overloaded", "api_error_status": 529}}]
        self.script([overloaded, claude_reply("The obstacle is step 3.")])
        code = runtime.run_author_conversation(WORKFLOW, {
            "author_model": "claude-opus-5-5", "goal_thread_id": self.session, "goal_cwd": str(self.run_dir)},
            {"schemaVersion": 1, "id": CONVERSATION_ID, "kind": "message", "text": "Which step is hard?",
             "notice": ""})
        self.assertEqual(code, 0)
        starts = [start["argv"] for start in self.records("start")]
        self.assertEqual([argv[:2] for argv in starts], [["--resume", self.session]] * 2)
        self.assertNotIn("--resume-session-at", starts[0])
        self.assertEqual(starts[1][2:4], ["--resume-session-at", anchor])
        message = CONVERSATION_TEXT.format(message="Which step is hard?")
        self.assertEqual(self.texts(), [message, message])
        reply, = [event for event in self.events if event["kind"] == "conversation_reply"]
        self.assertEqual((reply["text"], reply["threadId"], reply["conversationId"]),
                         ("The obstacle is step 3.", self.session, CONVERSATION_ID))
        self.assertTrue([event for event in self.events if event.get("label") == runtime.RETRY_LABEL
                         and event.get("conversationId") == CONVERSATION_ID])
        self.assertFalse(any("--session-id" in argv for argv in starts))

    def test_a_reply_cut_off_after_work_keeps_its_output(self):
        work = {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": str(uuid.uuid4()),
                         "message": {"content": [{"type": "text", "text": "Half a reply"}]}}}
        self.script([["init", "echo", work, {"stderr": "fatal: crashed"}, {"exit": 3}]])
        with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
            self.converse()
        self.assertEqual(raised.exception.retry, {"delivered": True, "output": True, "rewind": None,
                                                  "classic": False})
        self.assertIn("fatal: crashed", str(raised.exception))

    def test_a_quiet_cli_is_checked_and_then_given_up_as_a_failed_attempt(self):
        # The stand-in ends after a while, so a lost liveness check fails this test instead of hanging it.
        self.script([["init", "echo", {"hang": 5}, {"exit": 1}]])
        with mock.patch.object(runtime, "AUTHOR_INACTIVITY_SECONDS", 0.3), \
                mock.patch.object(runtime, "AUTHOR_PROBE_SECONDS", 0.6), \
                mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 0.5, create=True):
            with self.assertRaises(runtime.ConversationAttemptFailed) as raised:
                self.converse()
        self.assertRegex(str(raised.exception), r"^Claude stopped responding: no event for \d+ s and no answer to "
                                                r"a check within \d+ s\.$")
        self.assertIn("No author event for 0 s; checking that the Claude CLI still answers.",
                      [event.get("text") for event in self.events if event["kind"] == "diagnostic"])
        self.assertTrue(raised.exception.retry["delivered"])


class FakeTurns:
    """A stand-in conversation turn: raises the scripted failures in order, then replies."""

    def __init__(self, failures, answer="The reply.", compacted=False):
        self.failures, self.answer, self.compacted, self.calls = list(failures), answer, compacted, []

    def __call__(self, runtime_module, instruction, **kwargs):
        self.calls.append({"instruction": instruction, **kwargs})
        if self.failures:
            raise self.failures.pop(0)
        return {"reply": self.answer, "threadId": kwargs["options"]["goal_thread_id"], "compacted": self.compacted}


def failure(message=f"Author reply turn failed: {CAPACITY}", **retry):
    return runtime.ConversationAttemptFailed(message, retry=retry)


class ConversationRetryTests(unittest.TestCase):
    """run_author_conversation: its preparation, retry rules and final records (§2.7)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.clock = FakeClock()
        patch_clock(self, self.clock)
        self.events = []
        for target, name, value in (
                (runtime, "emit", lambda kind, stage, **fields: self.events.append(
                    {"kind": kind, "stage": stage, **fields})),
                (runtime, "require_model_credentials", mock.Mock()),
                (runtime, "goal_session", mock.Mock(side_effect=AssertionError("goal session started"))),
                (claude_cli, "goal_session", mock.Mock(side_effect=AssertionError("goal session started"))),
                (claude_cli, "author_conversation", mock.Mock(side_effect=AssertionError("Claude turn started"))),
                (runtime.subprocess, "Popen", mock.Mock(side_effect=AssertionError("process started")))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_turn(self, turns, kind="message", text="Which step is the main obstacle?", notice="", **options):
        request = {"schemaVersion": 1, "id": CONVERSATION_ID, "kind": kind, "text": text, "notice": notice}
        with mock.patch.object(runtime, "author_conversation", side_effect=turns):
            code = runtime.run_author_conversation(WORKFLOW, {
                "author_model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise",
                "goal_cwd": str(self.directory), "goal_thread_id": "thread-1", **options}, request)
        self.assertEqual(code, 0)
        return turns

    def records(self, kind=None, label=None):
        return [event for event in self.events if (kind is None or event["kind"] == kind)
                and (label is None or event.get("label") == label)]

    def waits(self):
        """Seconds of retry waiting so far (the patched sleeps take no real time)."""
        return round(sum(self.clock.sleeps), 6)

    def test_a_message_is_answered_with_one_reply_record(self):
        turns = self.run_turn(FakeTurns([], answer="Step 3 is the obstacle.", compacted=True),
                              notice="RELATED WORKS ADDED:\nThe user added a.pdf.")
        call, = turns.calls
        self.assertEqual(call["instruction"], "RELATED WORKS ADDED:\nThe user added a.pdf.\n\n"
                         + CONVERSATION_TEXT.format(message="Which step is the main obstacle?"))
        self.assertEqual((call["conversation_id"], call["label"], call["attempt"], call["rewind"], call["classic"]),
                         (CONVERSATION_ID, "Message to the paused author", 1, None, False))
        # The author's conversation turns never have subagents, whatever its setting.
        self.assertEqual(call["features"], [])
        self.assertEqual(call["settings"], {"model": "gpt-6.1-sol", "effort": "high", "speed": "standard",
                                            "summary": "concise"})
        # Outside the time budget, with no limit, live-instruction or pause file.
        self.assertEqual(call["options"]["workflow_stages"], AUTHOR_ONLY)
        self.assertEqual(call["options"]["thinking_hours"], 0)
        reply, = self.records("conversation_reply")
        self.assertEqual(reply, {"kind": "conversation_reply", "stage": "conversation", "node": "author",
                                 "conversationId": CONVERSATION_ID, "label": "Author reply",
                                 "conversationKind": "message", "text": "Step 3 is the obstacle.",
                                 "threadId": "thread-1", "compacted": True})
        self.assertEqual(self.events, [reply])

    def test_a_summary_uses_the_summary_prompt(self):
        turns = self.run_turn(FakeTurns([], answer="Progress so far."), kind="summary", text="")
        self.assertEqual(turns.calls[0]["instruction"], SUMMARY_TEXT)
        self.assertEqual(turns.calls[0]["label"], "Progress summary requested")
        reply, = self.records("conversation_reply")
        self.assertEqual((reply["label"], reply["conversationKind"], reply["text"]),
                         ("Progress summary", "summary", "Progress so far."))

    def test_each_failure_is_retried_every_minute_in_the_same_conversation(self):
        turns = self.run_turn(FakeTurns([
            failure(delivered=False),
            failure(delivered=True, output=False, rewind="turn-2"),
            failure(delivered=True, output=True),
            failure(delivered=True, output=False, rewind="turn-4"),
            failure(delivered=False),
            failure("Codex app-server stopped unexpectedly.", delivered=True, output=False, rewind="turn-6",
                    classic=True)]))
        message = CONVERSATION_TEXT.format(message="Which step is the main obstacle?")
        self.assertEqual([(call["instruction"], call["rewind"], call["attempt"]) for call in turns.calls], [
            (message, None, 1),
            (message, None, 2),  # Never delivered: the same message again.
            (message, "turn-2", 3),  # Delivered, nothing produced: removed, then sent again.
            (RETRY_NOTE_TEXT, None, 4),  # A reply was cut off: asked for again.
            (RETRY_NOTE_TEXT, "turn-4", 5),
            (RETRY_NOTE_TEXT, None, 6),
            (RETRY_NOTE_TEXT, "turn-6", 7)])
        self.assertEqual([call["classic"] for call in turns.calls], [False] * 6 + [True])
        self.assertEqual(self.waits(), 6 * 60)
        waits = self.records("status", runtime.RETRY_LABEL)
        notices = [event for event in waits if "retryAt" in event]
        self.assertEqual(len(notices), 6)
        for event in waits:
            self.assertEqual((event["stage"], event["node"], event["conversationId"]),
                             ("conversation", "author", CONVERSATION_ID))
        self.assertEqual(notices[0]["text"], "Model call failed (model at capacity) — retrying in 1 min (attempt 2). "
                                             f"Provider message: {CAPACITY}")
        self.assertEqual(waits[1]["text"], "Retrying the model call of the proof author now (attempt 2).")
        # The waits never enter the workflow clock.
        self.assertFalse([event for event in self.events if event.get("label") == "Workflow clock"
                          or "budgetClock" in event])
        self.assertEqual(len(self.records("conversation_reply")), 1)

    def test_waits_happen_even_when_the_authors_time_budget_is_used_up(self):
        limit = self.directory / "author-limit.json"
        limit.write_text(json.dumps({"hours": 0.0001}))
        pause = self.directory / "pause.json"
        pause.write_text("{}")  # The author's pause file never pauses a conversation turn.
        turns = self.run_turn(FakeTurns([failure(delivered=False)] * 2), thinking_hours=0.0001,
                              elapsed_seconds=10 ** 6, author_limit_file=str(limit), goal_pause_file=str(pause),
                              author_steer_file=str(self.directory / "steer.json"))
        self.assertEqual(len(turns.calls), 3)
        self.assertEqual(self.waits(), 2 * 60)
        for name in ("author_limit_file", "goal_pause_file", "author_steer_file"):
            self.assertNotIn(name, turns.calls[0]["options"])
        self.assertTrue(self.records("conversation_reply"))

    def test_failures_no_retry_can_fix_end_with_one_failure_record(self):
        gone = "Codex no longer has the saved author conversation (thread not found)."
        replaced = "Codex returned a different thread ID; refusing to continue a replacement conversation."
        changed = "Historical controller policy or broker changed during the run."
        key = "Add an API key for Gateway in Advanced settings → API models."
        unknown = "model_not_found: The model 'gpt-6.1-sol' does not exist or you do not have access to it."
        cases = (
            (runtime.SavedConversationGone(gone), "unavailable", gone),
            (runtime.SafetyStop(replaced), "safety", replaced),
            (retrieval_runtime.RetrievalPolicyError(changed), "safety", changed),
            (runtime.ConfigurationError(key), "configuration", key),
            # A model this account cannot use: no retry helps, as for the author (settings_failure).
            (failure(unknown), "configuration", str(runtime.settings_failure(failure(unknown), "gpt-6.1-sol"))),
        )
        for exc, kind, text in cases:
            with self.subTest(failure=kind, exc=type(exc).__name__):
                self.events.clear()
                self.clock.sleeps.clear()
                turns = self.run_turn(FakeTurns([exc]))
                self.assertEqual(len(turns.calls), 1)
                self.assertEqual(self.clock.sleeps, [])
                failed, = self.records("status", "Author reply failed")
                self.assertEqual(failed, {"kind": "status", "stage": "conversation", "node": "author",
                                          "conversationId": CONVERSATION_ID, "label": "Author reply failed",
                                          "final": True, "failure": kind, "text": text})
                self.assertFalse(self.records("conversation_reply"))
        self.assertTrue(text.startswith("Model unavailable: "))

    def test_three_identical_refusals_in_a_row_make_the_conversation_unavailable(self):
        refused = lambda text: runtime.ConversationRefused(text)
        turns = self.run_turn(FakeTurns([refused("locked"), refused("busy"), refused("locked"), refused("locked"),
                                         refused("locked")]))
        self.assertEqual(len(turns.calls), 5)
        self.assertEqual(self.waits(), 4 * 60)
        failed, = self.records("status", "Author reply failed")
        self.assertEqual((failed["failure"], failed["text"]),
                         ("unavailable", "Codex refused to reopen the saved author conversation 3 times: locked"))
        # Any other outcome in between starts the count again.
        self.events.clear()
        turns = self.run_turn(FakeTurns([refused("locked"), refused("locked"), failure(delivered=False),
                                         refused("locked"), refused("locked")]))
        self.assertEqual(len(turns.calls), 6)
        self.assertTrue(self.records("conversation_reply"))

    def test_claude_models_converse_through_claude(self):
        turns = FakeTurns([runtime.ConversationRefused("locked")] * 3)
        with mock.patch.object(claude_cli, "author_conversation", side_effect=turns), \
                mock.patch.object(runtime, "author_conversation", side_effect=AssertionError("Codex turn")):
            code = runtime.run_author_conversation(WORKFLOW, {
                "author_model": "claude-opus-5-5", "goal_thread_id": str(uuid.uuid4()),
                "goal_cwd": str(self.directory)}, {"schemaVersion": 1, "id": CONVERSATION_ID, "kind": "summary",
                                                  "text": "", "notice": ""})
        self.assertEqual(code, 0)
        self.assertEqual(turns.calls[0]["settings"]["model"], "claude-opus-5-5")
        failed, = self.records("status", "Author reply failed")
        self.assertEqual(failed["text"], "Claude refused to reopen the saved author conversation 3 times: locked")

    def test_a_workflow_without_the_prompt_or_a_goal_node_fails_without_a_turn(self):
        definition = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        definition["nodes"]["author"]["lifecycle"] = [*runtime._GOAL_LIFECYCLE, "reopen", "summary"]
        path = self.directory / "custom.yaml"
        path.write_text(yaml.safe_dump(definition), encoding="utf-8")
        for workflow, kind, text in (
                (path, "message", "The workflow's author node binds no conversation prompt."),
                (ROOT / "workflows/clean_up.yaml", "summary", "The workflow has no goal node for the proof author.")):
            with self.subTest(workflow=Path(workflow).name):
                self.events.clear()
                turns = FakeTurns([])
                with mock.patch.object(runtime, "author_conversation", side_effect=turns):
                    self.assertEqual(runtime.run_author_conversation(workflow, {"goal_thread_id": "thread-1"}, {
                        "schemaVersion": 1, "id": CONVERSATION_ID, "kind": kind, "text": "" if kind == "summary"
                        else "Hello?", "notice": ""}), 0)
                self.assertEqual(turns.calls, [])
                failed, = self.records("status", "Author reply failed")
                self.assertEqual((failed["failure"], failed["text"]), ("configuration", text))

    def test_a_cli_missing_for_a_moment_is_waited_for(self):
        checked = []

        def check(model):
            checked.append(model)
            if len(checked) == 1:  # Claude Code updates itself.
                raise runtime.CLIUnavailable("Claude CLI is not installed or is not on PATH.")

        runtime.require_model_credentials.side_effect = check
        turns = self.run_turn(FakeTurns([]))
        self.assertEqual((len(checked), len(turns.calls)), (2, 1))
        self.assertEqual(self.waits(), 60)
        notice, = [event for event in self.records("status", runtime.RETRY_LABEL) if "retryAt" in event]
        self.assertEqual(notice["failure"], "the CLI is not available")
        self.assertTrue(self.records("conversation_reply"))

    def test_stop_ends_a_wait_at_once(self):
        with mock.patch.object(runtime, "_retry_sleep", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.run_turn(FakeTurns([failure(delivered=False)]))
        self.assertFalse(self.records("status", "Author reply failed"))


class ConverseEntryTests(unittest.TestCase):
    """main(--converse): the stdin request, the author-only preparation and the exit codes (§2.7)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.events = []
        self.checked = []
        for target, name, value in (
                (runtime, "emit", lambda kind, stage, **fields: self.events.append(
                    {"kind": kind, "stage": stage, **fields})),
                (runtime, "require_model_credentials", self.checked.append),
                (runtime, "check_clis", mock.Mock(side_effect=AssertionError("CLI check"))),
                (runtime, "goal_session", mock.Mock(side_effect=AssertionError("goal session started"))),
                (claude_cli, "goal_session", mock.Mock(side_effect=AssertionError("goal session started"))),
                (claude_cli, "author_conversation", mock.Mock(side_effect=AssertionError("Claude turn started"))),
                (runtime.subprocess, "Popen", mock.Mock(side_effect=AssertionError("process started")))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, request, *arguments, turns=None):
        stdin = io.StringIO(request if isinstance(request, str) else json.dumps(request))
        stderr = io.StringIO()
        turns = turns or FakeTurns([], answer="Step 3.")
        with mock.patch.object(runtime.sys, "stdin", stdin), mock.patch.object(runtime.sys, "stderr", stderr), \
                mock.patch.object(runtime, "configure_standard_streams"), \
                mock.patch.object(runtime, "author_conversation", side_effect=turns):
            code = runtime.main(["--converse", *(arguments or (str(WORKFLOW),)), "--author-model", "gpt-6.1-sol",
                                 "--critic-model", "claude-sonnet-5-5", "--thinking-hours", "0",
                                 "--set", 'workflow_stages={"review":false,"author":true,"critic":false,'
                                          '"writing":false}',
                                 "--set", "goal_thread_id=thread-1", "--set", f"goal_cwd={self.directory}",
                                 "--set", "goal_require_resume=true"])
        return code, stderr.getvalue(), turns

    def request(self, **changes):
        return {"schemaVersion": 1, "id": CONVERSATION_ID, "kind": "message", "text": "Why lemma 2?",
                "notice": "", **changes}

    def test_a_valid_request_runs_one_turn_of_the_author_only(self):
        code, stderr, turns = self.main(self.request(text="  Why lemma 2?  "))
        self.assertEqual((code, stderr), (0, ""))
        call, = turns.calls
        self.assertEqual(call["instruction"], CONVERSATION_TEXT.format(message="Why lemma 2?"))
        self.assertTrue(call["options"]["goal_require_resume"])
        self.assertNotIn("converse", call["options"])
        # Only the author's model and credentials are checked: the critic's Claude CLI is never needed.
        self.assertEqual(self.checked, ["gpt-6.1-sol"])
        reply, = [event for event in self.events if event["kind"] == "conversation_reply"]
        self.assertEqual((reply["text"], reply["conversationId"]), ("Step 3.", CONVERSATION_ID))
        self.assertTrue(all(event["stage"] == "conversation" for event in self.events))

    def test_a_final_failure_also_exits_zero(self):
        code, stderr, _ = self.main(self.request(), turns=FakeTurns([runtime.SavedConversationGone("Gone.")]))
        self.assertEqual((code, stderr), (0, ""))
        failed, = [event for event in self.events if event.get("label") == "Author reply failed"]
        self.assertEqual(failed["failure"], "unavailable")

    def test_invalid_requests_exit_1_before_any_turn(self):
        long = "x" * (runtime.CONVERSATION_MAX_CHARS + 1)
        invalid = [
            "", "not json", "[]", json.dumps(self.request())[:-1],
            self.request(schemaVersion=2), self.request(schemaVersion=True), self.request(schemaVersion="1"),
            self.request(id="c_012345678"), self.request(id="c_0123456789a"), self.request(id="x_0123456789"),
            self.request(id="c_012345678g"), self.request(id=7),
            self.request(kind="question"), self.request(kind=["message"]),
            self.request(text=""), self.request(text=" \n "), self.request(text=long), self.request(text=7),
            self.request(text="a\0b"), self.request(kind="summary", text="A summary needs no text."),
            self.request(notice=long), self.request(notice="a\0b"), self.request(notice=None),
            {key: value for key, value in self.request().items() if key != "notice"},
        ]
        for request in invalid:
            with self.subTest(request=request if isinstance(request, str) else json.dumps(request)[:80]):
                self.events.clear()
                code, stderr, turns = self.main(request)
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "error: Invalid conversation request.\n")
                self.assertEqual(turns.calls, [])
                self.assertEqual(self.events, [])
        for request in (self.request(text="x" * runtime.CONVERSATION_MAX_CHARS),
                        self.request(kind="summary", text=""), self.request(kind="summary", text="  "),
                        self.request(notice="n" * runtime.CONVERSATION_MAX_CHARS), self.request(id="c_ABCDEF0123")):
            with self.subTest(valid=json.dumps(request)[:80]):
                self.assertEqual(self.main(request)[0], 0)

    def test_exactly_one_workflow(self):
        code, stderr, turns = self.main(self.request(), str(WORKFLOW), str(ROOT / "workflows/clean_up.yaml"))
        self.assertEqual((code, stderr), (1, "error: --converse takes exactly one workflow.\n"))
        self.assertEqual(turns.calls, [])

    def test_stop_exits_130(self):
        code, _, _ = self.main(self.request(), turns=FakeTurns([KeyboardInterrupt()]))
        self.assertEqual(code, 130)


if __name__ == "__main__":
    unittest.main()
