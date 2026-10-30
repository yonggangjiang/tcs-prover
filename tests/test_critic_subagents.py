"""The critic as one call with X built-in subagents, the author's subagents setting, and Ultra's removal.

Offline: stand-in model attempts, stand-in Codex and Claude CLIs on an isolated PATH and HOME, a stubbed
proof author and a patched clock; no model call is made and no wait takes real time. The recorded Codex
event streams below were captured from Codex 0.160 `codex exec --json` against a local stand-in for the
model API (an isolated home, no login, no model).
"""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, providers
from tests import critic_doubles
from ui import batches, cli, review, server

import test_model_retry as retry_test

ROOT = Path(runtime.__file__).parent
AUTHOR_CRITIC, CLEAN_UP = ROOT / "workflows/author_critic.yaml", ROOT / "workflows/clean_up.yaml"
PROOF = "Lemma 1 holds by induction on n.\nStep 2 divides by n.\nHence the theorem holds."
FIXED = PROOF + "\nCase n = 0: the claim holds trivially."
LATEX = r"\documentclass{article}\begin{document}Proof.\end{document}"
STATEMENT = "Every finite task has property P."

# The owner's critic prompt, exactly.
CRITIC_PROMPT = """Act as a rigorous proof critic. Spawn exactly {count} fresh independent subagent(s) to aggressively find bugs in the
exact statement and the candidate proof in proof.md. They must work independently, without communicating with one
another, and report back to you. Wait for all their reports before making your decision.

Check every claim and inference, including boundary cases, hidden assumptions, circular reasoning, undefined terms
or symbols, and omitted proof details. Treat statements that hide an argument behind words such as "clearly",
"standard", or "similarly" as gaps when the necessary justification is absent. Ask the subagents to identify
exactly what definitions and omitted arguments must be expanded to make the proof complete and rigorous.

Examine their objections and decide which are valid. Try to fix every valid bug yourself by editing proof.md,
defining missing terms and fully expanding omitted arguments, and list each bug you fixed in fixes. Reject only
for a critical error that a subagent found and that you confirm but cannot rigorously fix: then keep the safe
fixes in proof.md and return verdict=reject with every unresolved bug and its exact location in bugs. Otherwise
return verdict=pass and an empty bugs string. If no edits are needed, leave proof.md unchanged and leave fixes
empty. Return only the requested JSON."""
AUTHOR_SUBAGENTS_PROMPT = ("You may start up to {author_subagents} independent subagents at a time, for example to "
                           "explore other approaches or to check calculations; you do not need to ask the user.")
REVIEW_LINE = ("Also try to guess what the user, as a conventional mathematician or theoretical computer science "
               "researcher, actually wants from this problem, and check whether the statement has loopholes that a "
               "proof could exploit to satisfy its letter without doing what the user wants (for example, a "
               "brute-force construction that stores all answers). If it does, close them in the way the user most "
               "likely intends, and explain each change in the notes.")

# Codex 0.160, multi-agent version 1 (a model without catalog metadata, as API models are): each spawn_agent
# names its new thread, and a wait reports each thread's final state.
RECORDED_V1 = [
    {"type": "thread.started", "thread_id": "01a18874-ea94-7602-8f0a-0ebc4132a45d"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_1", "type": "collab_tool_call", "tool": "spawn_agent",
     "sender_thread_id": "01a18874-ea94-7602-8f0a-0ebc4132a45d", "receiver_thread_ids": [],
     "prompt": "Audit the proof.", "agents_states": {}, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "collab_tool_call", "tool": "spawn_agent",
     "sender_thread_id": "01a18874-ea94-7602-8f0a-0ebc4132a45d",
     "receiver_thread_ids": ["01a18874-eaa9-7dd3-a5ce-08ee7dece908"], "prompt": "Audit the proof.",
     "agents_states": {"01a18874-eaa9-7dd3-a5ce-08ee7dece908": {"status": "pending_init", "message": None}},
     "status": "completed"}},
    {"type": "item.started", "item": {"id": "item_2", "type": "collab_tool_call", "tool": "spawn_agent",
     "sender_thread_id": "01a18874-ea94-7602-8f0a-0ebc4132a45d", "receiver_thread_ids": [],
     "prompt": "Audit the proof again.", "agents_states": {}, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "collab_tool_call", "tool": "spawn_agent",
     "sender_thread_id": "01a18874-ea94-7602-8f0a-0ebc4132a45d",
     "receiver_thread_ids": ["01a18874-eab0-7703-bf62-77eefb8c4f16"], "prompt": "Audit the proof again.",
     "agents_states": {"01a18874-eab0-7703-bf62-77eefb8c4f16": {"status": "pending_init", "message": None}},
     "status": "completed"}},
    {"type": "item.started", "item": {"id": "item_3", "type": "collab_tool_call", "tool": "wait",
     "sender_thread_id": "01a18874-ea94-7602-8f0a-0ebc4132a45d",
     "receiver_thread_ids": ["01a18874-eaa9-7dd3-a5ce-08ee7dece908", "01a18874-eab0-7703-bf62-77eefb8c4f16"],
     "prompt": None, "agents_states": {}, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "collab_tool_call", "tool": "wait",
     "sender_thread_id": "01a18874-ea94-7602-8f0a-0ebc4132a45d",
     "receiver_thread_ids": ["01a18874-eaa9-7dd3-a5ce-08ee7dece908", "01a18874-eab0-7703-bf62-77eefb8c4f16"],
     "prompt": None, "agents_states": {
         "01a18874-eab0-7703-bf62-77eefb8c4f16": {"status": "completed", "message": "No issues found."},
         "01a18874-eaa9-7dd3-a5ce-08ee7dece908": {"status": "completed", "message": "No issues found."}},
     "status": "completed"}},
]
# Codex 0.160, multi-agent version 2 (GPT 6.x, GPT 5.6 Sol and Terra): no item for spawn_agent; a wait names no
# thread and no state.
RECORDED_V2 = [
    {"type": "thread.started", "thread_id": "01a1885f-22b4-70d2-b42e-87424af792e5"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
     "sender_thread_id": "01a1885f-22b4-70d2-b42e-87424af792e5", "receiver_thread_ids": [], "prompt": None,
     "agents_states": {}, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
     "sender_thread_id": "01a1885f-22b4-70d2-b42e-87424af792e5", "receiver_thread_ids": [], "prompt": None,
     "agents_states": {}, "status": "completed"}},
]


def critic_answer(fixes="", verdict="pass", bugs=""):
    return {"verdict": verdict, "bugs": bugs, "fixes": fixes}


class CriticCase(retry_test.RetryCase):
    """A run folder as working directory, the author guarded, and every model call scripted per role."""

    def setUp(self):
        super().setUp()
        for module in (runtime, claude_cli):
            patcher = mock.patch.object(module, "goal_session", side_effect=retry_test.AuthorStarted(
                "the author must not run"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        previous = Path.cwd()
        os.chdir(self.run_dir)
        self.addCleanup(os.chdir, previous)
        # Proof snapshots and writable workspaces stay inside this test's folder.
        temp = self.root / "tmp"
        temp.mkdir()
        patcher = mock.patch.object(tempfile, "tempdir", str(temp))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        self.convention = lambda call: dict(critic_doubles.FAITHFUL)

    def attempts(self, critic):
        """A stand-in run_structured_attempt(): `critic(call)` answers the critic, self.convention the other."""

        def attempt(text, schema, stage, *args, conversation=None, **settings):
            call = {"text": text, "schema": schema, "stage": stage, "label": settings.get("activity_label"),
                    "features": tuple(settings.get("features", ())), "subagents": settings.get("subagents"),
                    "input_files": settings.get("input_files"), "edit": settings.get("edit_file"),
                    "conversation": conversation,
                    "workspace": Path(conversation.workspace) if conversation is not None and conversation.workspace
                    else None}
            self.calls.append(call)
            answer = (self.convention(call) if call["label"] == critic_doubles.CONVENTION_LABEL else critic(call))
            if isinstance(answer, BaseException):
                raise answer
            return json.dumps(answer)

        return attempt

    def run_critic(self, critic, options=None, solution=PROOF, workflows=(AUTHOR_CRITIC,)):
        with mock.patch.object(runtime, "run_structured_attempt", side_effect=self.attempts(critic)):
            return runtime.execute_workflows(list(workflows), {"statement": "Task", "solution": solution},
                                             {"start_node": "critic", **(options or {})})

    def critics(self):
        return [call for call in self.calls if call["label"] == "Critic coordinator"]

    def conventions(self):
        return [call for call in self.calls if call["label"] == critic_doubles.CONVENTION_LABEL]

    def kinds(self, kind):
        return [event for event in self.events if event["kind"] == kind]

    def labelled(self, label):
        return [event for event in self.events if event.get("label") == label]


def unchanged(call):
    return critic_answer()


def fixes(call):
    """A critic that adds the missing case to its writable proof.md once, and says so."""

    if call["edit"] is not None and "Case n = 0" not in (call["workspace"] / "proof.md").read_text():
        (call["workspace"] / "proof.md").write_text(FIXED)
        return critic_answer("Step 2 divided by n = 0: added the case n = 0.")
    return critic_answer()


class DeclarationTests(unittest.TestCase):
    def test_the_critic_is_one_call_with_its_independent_critics_as_subagents(self):
        workflow = runtime.builtin_workflow("author_critic")
        node = workflow["nodes"]["critic"]
        self.assertNotIn("parallel", node)
        self.assertNotIn("companion", node)
        self.assertEqual(node["features"], ["multi_agent"])
        self.assertEqual(node["subagents"], {"option": "critic_count", "default": 3, "minimum": 1})
        self.assertEqual(node["schema"]["properties"].keys(), {"verdict", "bugs", "fixes"})
        self.assertEqual(node["schema"]["properties"]["verdict"]["enum"], ["pass", "reject"])
        self.assertEqual(workflow["prompts"]["critic"], CRITIC_PROMPT)
        self.assertEqual(workflow["prompts"]["critic_input"], "CRITIC INSTRUCTIONS:\n{instructions}\n\n"
                                                              "STATEMENT:\n{statement}\n")
        self.assertNotIn("critic_audit", workflow["prompts"])
        self.assertEqual(runtime.DEFAULT_CRITIC_COUNT, 3)
        for name in ("CRITIC_AUDIT_CHECKPOINT_FILENAME", "PARALLEL_REQUEST_LABEL", "_parallel_requests",
                     "load_critic_audit_checkpoint", "CRITIC_AUDIT_FOCI"):
            self.assertFalse(hasattr(runtime, name), name)
        self.assertFalse(hasattr(server, "CRITIC_AUDITS_FILENAME"))

    def test_the_author_gets_subagents_only_from_its_setting(self):
        workflow = runtime.builtin_workflow("author_critic")
        node = workflow["nodes"]["author"]
        self.assertNotIn("features", node)
        self.assertEqual(node["subagents"], {"option": "author_subagents", "default": 0, "minimum": 0})
        self.assertIn("subagents", node["lifecycle"])
        self.assertEqual(workflow["prompts"]["subagents"], AUTHOR_SUBAGENTS_PROMPT)
        self.assertEqual(runtime.DEFAULT_AUTHOR_SUBAGENTS, 0)
        self.assertEqual(runtime.node_features(node, {}), ([], 0))
        self.assertEqual(runtime.node_features(node, {"author_subagents": 4}), (["multi_agent"], 4))
        self.assertEqual(runtime.node_features(workflow["nodes"]["critic"], {"critic_count": 2}), (["multi_agent"], 2))
        # The convention critic, the reviewer and the writer never get subagents.
        self.assertEqual(runtime.node_features(workflow["nodes"]["convention"], {}), ([], None))
        for node in runtime.builtin_workflow("clean_up")["nodes"].values():
            self.assertNotIn("multi_agent", node.get("features", []))

    def test_counts_out_of_range_are_refused_before_any_model_starts(self):
        workflow = runtime.builtin_workflow("author_critic")
        for option, value, message in (("critic_count", 0, "Choose 1 to 10 independent critics"),
                                       ("critic_count", 11, "Choose 1 to 10 independent critics"),
                                       ("author_subagents", -1, "Choose 0 to 10 author subagents"),
                                       ("author_subagents", 11, "Choose 0 to 10 author subagents"),
                                       ("author_subagents", 2.5, "must be an integer")):
            with self.subTest(option=option, value=value), self.assertRaisesRegex(runtime.Error, message):
                runtime.prepare(workflow, {option: value})
        for config in ({"option": "x"}, {"default": 11}, {"default": 1, "minimum": 2}, {"default": True}, "three"):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, "subagents"):
                runtime._check_subagents(config, "critic")

    def test_the_statement_review_prompt_asks_for_loopholes_in_its_own_line(self):
        lines = review.REVIEW_PROMPT.split("\n")
        scanning = next(index for index, line in enumerate(lines) if line.startswith("Do a initial scanning"))
        self.assertEqual(lines[scanning + 1], REVIEW_LINE)
        self.assertTrue(lines[scanning + 2].startswith("If you found the statement is trivial or false"))
        self.assertEqual(server.default_prompts()["review"], review.REVIEW_PROMPT)


class CriticCallTests(CriticCase):
    def test_the_number_fills_the_prompt_and_reaches_the_call(self):
        state = self.run_critic(unchanged, {"critic_count": 5})
        self.assertEqual(state["output"], PROOF)
        [critic] = self.critics()
        self.assertIn("Spawn exactly 5 fresh independent subagent(s) to aggressively find bugs", critic["text"])
        self.assertNotIn("{count}", critic["text"])
        self.assertTrue(critic["text"].startswith("CRITIC INSTRUCTIONS:\n"))
        self.assertEqual((critic["features"], critic["subagents"]), (("multi_agent",), 5))
        # The proof is a file of its workspace: the prompt says the critic and its subagents read it there.
        self.assertIn("Give independent subagents these same files and working directory.", critic["text"])

    def test_the_final_check_also_starts_its_subagents_and_answers_without_fixes(self):
        self.run_critic(fixes, {"critic_rounds": 1, "critic_count": 2})
        first, final = self.critics()
        self.assertIsNotNone(first["edit"])
        self.assertIsNone(final["edit"])
        self.assertIn("FINAL VERIFICATION", final["text"])
        self.assertIn("Spawn exactly 2 fresh independent subagent(s)", final["text"])
        self.assertEqual(final["subagents"], 2)
        results = [event["report"] for event in self.kinds("critic_result")]
        self.assertEqual([(report["fixes"], report["proofChanged"]) for report in results],
                         [("Step 2 divided by n = 0: added the case n = 0.", True), ("", False)])

    def test_a_changed_proof_without_fixes_is_asked_again_never_rejected(self):
        answers = iter([critic_answer(), critic_answer("Added the case n = 0.")])

        def critic(call):
            if call["edit"] is not None:
                (call["workspace"] / "proof.md").write_text(FIXED)
            return next(answers, critic_answer())

        self.run_critic(critic, {"critic_rounds": 1})
        first, retry, final = self.critics()
        self.assertIn("the fixes field must not be empty when you changed proof.md", retry["text"])
        self.assertEqual([event["outcome"] for event in self.kinds("critic_result")], ["fixed", "pass"])
        self.assertEqual(self.waits(), 60)

    def test_fixes_text_for_an_unchanged_proof_is_accepted_as_a_clean_pass(self):
        # Coordinators often write "No edits needed" there; asking again would only cost a minute.
        self.run_critic(lambda call: critic_answer("No edits needed; the proof is complete."))
        self.assertEqual(len(self.critics()), 1)
        self.assertEqual([(event["outcome"], event["report"]["proofChanged"])
                          for event in self.kinds("critic_result")], [("pass", False)])

    def test_a_failed_subagent_is_run_again_and_never_rejects(self):
        def critic(call):
            if len(self.critics()) == 1:
                call["conversation"].subagent("agent-1", "failed", retry_test.CAPACITY)
                return critic_answer(verdict="reject", bugs="Review incomplete: a subagent failed.")
            return critic_answer()

        state = self.run_critic(critic)
        self.assertEqual(state["output"], PROOF)
        self.assertEqual(len(self.critics()), 2)
        self.assertEqual([event["report"]["verdict"] for event in self.kinds("critic_result")], ["pass"])

    def test_the_progress_of_the_subagents_is_reported_without_their_text(self):
        def critic(call):
            conversation = call["conversation"]
            for identity in ("thread-a", "thread-b"):
                conversation.subagent(identity, "started")
            conversation.subagent(None, "waiting")
            conversation.subagent("thread-b", "completed")
            conversation.subagent("thread-a", "failed", "SECRET SUBAGENT TEXT")
            conversation.subagent("thread-a", "completed")
            return critic_answer()

        self.run_critic(critic, {"critic_count": 2})
        progress = [event for event in self.events if event.get("subagentProgress")]
        self.assertEqual([event["label"] for event in progress],
                         ["2 independent critics requested", "Independent critic 1 started",
                          "Independent critic 2 started", "Waiting for the independent critics",
                          "Independent critic 2 finished", "Independent critic 1 finished"])
        self.assertTrue(all(event["auditCount"] == 2 and "activityLabel" not in event for event in progress))
        self.assertEqual([event.get("auditIndex") for event in progress], [None, 1, 2, None, 2, 1])
        self.assertTrue(progress[-1]["failed"])
        self.assertNotIn("SECRET SUBAGENT TEXT", json.dumps(progress))
        # The convention critic has no subagents and reports none.
        self.assertEqual(self.conventions()[0]["subagents"], None)


class CodexLimitTests(unittest.TestCase):
    HINT = ("Multi-agent delegation is allowed for this task: the root agent may start up to 3 independent "
            "subagents at a time without asking the user. Subagents do not start subagents of their own.")

    def pairs(self, argv):
        return [argv[index:index + 2] for index in range(len(argv) - 1) if argv[index] == "-c"]

    def test_the_critic_gets_codex_own_limits_and_the_delegation_hint(self):
        argv = runtime.structured_tool_arguments("critic", ("multi_agent", "shell_tool"), None, 3)
        self.assertNotIn("--disable", [argv[index] for index in range(len(argv)) if argv[index + 1:index + 2] ==
                                       ["multi_agent"]])
        self.assertEqual([pair[1] for pair in self.pairs(argv) if "agent" in pair[1]], [
            "features.multi_agent_v2.max_concurrent_threads_per_session=4",
            "features.multi_agent_v2.multi_agent_mode_hint_text=" + json.dumps(self.HINT),
            "agents.max_concurrent_threads_per_session=3", "agents.max_depth=1"])
        self.assertEqual(runtime.subagent_limits(["multi_agent"], 1)["features"]["multi_agent_v2"]
                         ["multi_agent_mode_hint_text"].count("up to 1 independent subagent at a time"), 1)

    def test_every_call_without_subagents_gets_a_session_of_one_thread(self):
        for stage in ("review", "final", "critic"):
            with self.subTest(stage=stage):
                argv = runtime.structured_tool_arguments(stage, (), None)
                self.assertIn(["--disable", "multi_agent"], [argv[index:index + 2] for index in range(len(argv))])
                self.assertEqual([pair[1] for pair in self.pairs(argv) if "agent" in pair[1]],
                                 ["features.multi_agent_v2.max_concurrent_threads_per_session=1"])
        # A workflow node that asks for multi_agent without a number keeps Codex's own limits.
        self.assertEqual(runtime.subagent_limits(["multi_agent"], None), {})

    def test_the_reviewer_and_the_writer_have_no_subagent_tool(self):
        calls = []

        def attempt(text, schema, stage, *args, **settings):
            calls.append(settings)
            return json.dumps({"statement": "S.", "notes": "Fine."})

        with mock.patch.object(runtime, "run_structured_attempt", side_effect=attempt), \
                mock.patch.object(runtime, "emit"):
            review.review("Draft")
        self.assertEqual((calls[0]["features"], calls[0].get("subagents")), ((), None))
        for node in runtime.builtin_workflow("clean_up")["nodes"].values():
            if node["run"] == "structured":
                self.assertEqual(runtime.node_features(node, {}), ([], None))

    def test_the_author_app_server_and_thread_follow_the_setting(self):
        class Runtime:
            environment = staticmethod(lambda model: {"PATH": "/usr/bin"})
            codex = staticmethod(lambda: "/fake/codex")
            provider_arguments = staticmethod(lambda model: [])
            speed_arguments = staticmethod(lambda speed, model: [])
            context_cache_arguments = staticmethod(lambda: [])
            Error = runtime.Error
            wire_model = staticmethod(lambda model: model)

        class RPC:
            def call(self, method, params):
                return {"config": {}}

        settings = {"model": "gpt-6.1-sol", "effort": "max", "speed": "standard", "summary": "concise"}
        for count, features in ((0, []), (4, ["multi_agent"])):
            with self.subTest(count=count):
                values = {**settings, "subagents": count}
                argv, _, _ = runtime._author_app_server(Runtime, directory="/tmp/x", policy=None, role="author",
                                                        model="gpt-6.1-sol", settings=values, features=features,
                                                        classic=False)
                options = runtime._author_thread_options(Runtime, RPC(), directory="/tmp/x", policy=None,
                                                         role="author", model="gpt-6.1-sol", settings=values,
                                                         summary="concise", features=features, classic=False,
                                                         emit=lambda *a, **k: None)
                config = options["config"]
                flags = [pair[1] for pair in self.pairs(argv) if "agent" in pair[1]]
                if count:
                    self.assertIn(["--enable", "multi_agent"], [argv[index:index + 2] for index in range(len(argv))])
                    self.assertIn("features.multi_agent_v2.max_concurrent_threads_per_session=5", flags)
                    self.assertIn("agents.max_concurrent_threads_per_session=4", flags)
                    self.assertEqual(config["features"]["multi_agent_v2"]["max_concurrent_threads_per_session"], 5)
                    self.assertIn("up to 4 independent subagents",
                                  config["features"]["multi_agent_v2"]["multi_agent_mode_hint_text"])
                    self.assertEqual(config["agents"], {"max_concurrent_threads_per_session": 4, "max_depth": 1})
                    self.assertTrue(config["features"]["multi_agent"])
                else:
                    self.assertIn(["--disable", "multi_agent"], [argv[index:index + 2] for index in range(len(argv))])
                    self.assertEqual(flags, ["features.multi_agent_v2.max_concurrent_threads_per_session=1"])
                    self.assertEqual(config["features"]["multi_agent_v2"], {"max_concurrent_threads_per_session": 1})
                    self.assertFalse(config["features"]["multi_agent"])
                    self.assertNotIn("agents", config)

    def test_the_web_cutoff_audit_accepts_exactly_the_limits_of_the_session(self):
        limits = retrieval_runtime.agent_limits(2, "Hint.")
        config = {"features": {"multi_agent_v2": {"enabled": False, "max_concurrent_threads_per_session": 3,
                                                  "multi_agent_mode_hint_text": "Hint."}},
                  "agents": {"enabled": None, "max_concurrent_threads_per_session": 2, "max_depth": 1,
                             "default_subagent_model": None}}
        retrieval_runtime._check_agent_limits(config, limits)
        retrieval_runtime._check_agent_limits({"features": {"multi_agent_v2": False}, "agents": None}, None)
        retrieval_runtime._check_agent_limits(
            {"features": {"multi_agent_v2": {"enabled": False, "max_concurrent_threads_per_session": 1}}},
            retrieval_runtime.agent_limits(0))
        for change, message in (
                (lambda value: value["features"]["multi_agent_v2"].update(max_concurrent_threads_per_session=9),
                 "unexpected subagent limits"),
                (lambda value: value["features"]["multi_agent_v2"].update(usage_hint_text="More."),
                 "unexpected subagent limits"),
                (lambda value: value["agents"].update(max_depth=2), "inherited agents"),
                (lambda value: value["agents"].update(default_subagent_model="other"), "inherited agents")):
            changed = json.loads(json.dumps(config))
            change(changed)
            with self.subTest(change=change), self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, message):
                retrieval_runtime._check_agent_limits(changed, limits)
        with self.assertRaisesRegex(retrieval_runtime.RetrievalPolicyError, "inherited agents"):
            retrieval_runtime._check_agent_limits({"features": {"multi_agent_v2": False},
                                                   "agents": {"max_depth": 1}}, None)
        self.assertEqual(retrieval_runtime.agent_limit_arguments(limits), [
            "-c", "features.multi_agent_v2.max_concurrent_threads_per_session=3",
            "-c", 'features.multi_agent_v2.multi_agent_mode_hint_text="Hint."',
            "-c", "agents.max_concurrent_threads_per_session=2", "-c", "agents.max_depth=1"])


class ClaudeToolTests(unittest.TestCase):
    def test_only_a_call_with_subagents_gets_the_task_tool(self):
        def tools(role, features):
            return claude_cli.tool_permissions(role, features, live_search=False, bash=False, bare=False)[0]

        self.assertIn("Task", tools("structured", ("multi_agent", "file_edit")))  # The critic.
        self.assertNotIn("Task", tools("structured", ()))  # The reviewer, the writer, the convention critic.
        self.assertNotIn("Task", tools("structured", ("file_edit",)))  # The writer editing its source.
        self.assertNotIn("Task", tools("author", ()))  # Author subagents 0.
        self.assertIn("Task", tools("author", ("multi_agent",)))  # Author subagents 1 or more.


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class RecordedStreamTests(retry_test.RetryCase):
    """The progress records from Codex 0.160's own event streams of a call with subagents."""

    def setUp(self):
        super().setUp()
        self.state = self.root / "codex-state"
        self.state.mkdir()
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
argv = sys.argv[1:]
if "exec" not in argv:
    sys.exit(0)
sys.stdin.read()
open(os.path.join(os.environ["TCS_FAKE_CODEX_STATE"], "argv.json"), "w").write(json.dumps(argv))
for event in json.load(open(os.environ["TCS_FAKE_CODEX_EVENTS"])):
    sys.stdout.write(json.dumps(event) + "\n")
text = json.dumps({"statement": "S.", "notes": "Fine."})
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": text}}) + "\n")
open(argv[argv.index("-o") + 1], "w").write(text)
''')
        fake.chmod(0o700)
        self.events_file = self.root / "events.json"
        environment = mock.patch.dict(os.environ, {"TCS_FAKE_CODEX_STATE": str(self.state),
                                                    "TCS_FAKE_CODEX_EVENTS": str(self.events_file),
                                                    "CODEX_HOME": str(self.root / "codex-home"),
                                                    "TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        patcher = mock.patch.object(runtime, "codex", return_value=str(fake))
        patcher.start()
        self.addCleanup(patcher.stop)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def progress(self, recorded, count=2):
        self.events_file.write_text(json.dumps(recorded))
        runtime.structured("Check S.", retry_test.SCHEMA, "critic", features=("multi_agent",), subagents=count)
        argv = json.loads((self.state / "argv.json").read_text())
        self.assertIn("agents.max_concurrent_threads_per_session=%d" % count, argv)
        return [(event["label"], event.get("auditIndex")) for event in self.events if event.get("subagentProgress")]

    def test_version_1_streams_name_each_critic_start_and_end(self):
        self.assertEqual(self.progress(RECORDED_V1), [
            ("2 independent critics requested", None), ("Independent critic 1 started", 1),
            ("Independent critic 2 started", 2), ("Waiting for the independent critics", None),
            ("Independent critic 2 finished", 2), ("Independent critic 1 finished", 1)])

    def test_version_2_streams_show_only_that_the_critic_waits(self):
        self.assertEqual(self.progress(RECORDED_V2, 3), [("3 independent critics requested", None),
                                                         ("Waiting for the independent critics", None)])


# Codex 0.160 (gpt-6.1-sol, multi-agent version 2) against a local stand-in for the model API whose subagent's
# model call failed (HTTP 500): `codex exec --json` shows the coordinator only waiting, with no error, while its
# saved session has the subagent's start and the "Agent errored" message it received. The retry (`codex exec
# fork`) started the subagent again, which reported. Real records (isolated home, no login, no model); the
# answer text is the test's.
V2_FAILED = {
    "thread": "01a188e3-cbb2-7f12-9a77-bf90b1367f85",
    "stream": [
        {"type": "thread.started", "thread_id": "01a188e3-cbb2-7f12-9a77-bf90b1367f85"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
         "sender_thread_id": "01a188e3-cbb2-7f12-9a77-bf90b1367f85", "receiver_thread_ids": [], "prompt": None,
         "agents_states": {}, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
         "sender_thread_id": "01a188e3-cbb2-7f12-9a77-bf90b1367f85", "receiver_thread_ids": [], "prompt": None,
         "agents_states": {}, "status": "completed"}},
        {"type": "item.started", "item": {"id": "item_1", "type": "collab_tool_call", "tool": "wait",
         "sender_thread_id": "01a188e3-cbb2-7f12-9a77-bf90b1367f85", "receiver_thread_ids": [], "prompt": None,
         "agents_states": {}, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "collab_tool_call", "tool": "wait",
         "sender_thread_id": "01a188e3-cbb2-7f12-9a77-bf90b1367f85", "receiver_thread_ids": [], "prompt": None,
         "agents_states": {}, "status": "completed"}},
    ],
    "rollout": [
        {"timestamp": "2026-10-29T18:19:52.640Z", "ordinal": 1, "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "01a188e3-cbbd-7d43-a264-a865a74ef77b",
            "root_turn_id": "01a188e3-cbbd-7d43-a264-a865a74ef77b", "started_at": 1793297992,
            "model_context_window": 258400, "collaboration_mode_kind": "default"}},
        {"timestamp": "2026-10-29T18:19:52.690Z", "ordinal": 12, "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "01a188e3-cbb2-7f12-9a77-bf90b1367f85",
            "turn_id": "01a188e3-cbbd-7d43-a264-a865a74ef77b", "item": {
                "type": "SubAgentActivity", "id": "call_1", "kind": "started",
                "agent_thread_id": "01a188e3-cbd5-7081-975a-47b2e5571838", "agent_path": "/root/checker"},
            "started_at_ms": 1793297992690, "completed_at_ms": 1793297992690}},
        {"timestamp": "2026-10-29T18:19:52.702Z", "ordinal": 22, "type": "response_item", "payload": {
            "type": "agent_message", "id": "amsg_01a188e3-cbfe-7751-8f4c-a0322e3f766a", "author": "/root/checker",
            "recipient": "/root", "content": [{"type": "input_text", "text": (
                "Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/checker\nPayload:\nAgent errored: "
                "We’re currently experiencing high demand, which may cause temporary errors.\n\nThis agent's "
                "turn failed. If you still need this agent, use the available collaboration tools to give it "
                "another task.")}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "01a188e3-cbbd-7d43-a264-a865a74ef77b",
                                                           "create_time": 1793297992.702031}},
         "metadata": {"client_authored": False, "user_input_order": 3}},
        {"timestamp": "2026-10-29T18:20:07.734Z", "ordinal": 32, "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "01a188e3-cbbd-7d43-a264-a865a74ef77b", "last_agent_message": "done",
            "started_at": 1793297992, "completed_at": 1793298007, "duration_ms": 15094,
            "time_to_first_token_ms": 20}},
    ],
}
V2_RETRIED = {
    "thread": "01a188e4-af20-7161-80a6-844c6914c5f0",
    "stream": [
        {"type": "thread.started", "thread_id": "01a188e4-af20-7161-80a6-844c6914c5f0"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
         "sender_thread_id": "01a188e4-af20-7161-80a6-844c6914c5f0", "receiver_thread_ids": [], "prompt": None,
         "agents_states": {}, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "wait",
         "sender_thread_id": "01a188e4-af20-7161-80a6-844c6914c5f0", "receiver_thread_ids": [], "prompt": None,
         "agents_states": {}, "status": "completed"}},
    ],
    "rollout": [
        {"timestamp": "2026-10-29T18:20:50.930Z", "ordinal": 43, "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "01a188e4-af20-7161-80a6-844c6914c5f0",
            "turn_id": "01a188e4-af47-77a1-a138-3fd6a53a3fc6", "item": {
                "type": "SubAgentActivity", "id": "call_1", "kind": "started",
                "agent_thread_id": "01a188e4-af58-79b1-82af-7164ce6d2869", "agent_path": "/root/checker"},
            "started_at_ms": 1793298050930, "completed_at_ms": 1793298050930}},
        {"timestamp": "2026-10-29T18:20:54.958Z", "ordinal": 49, "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "01a188e4-af20-7161-80a6-844c6914c5f0",
            "turn_id": "01a188e4-af47-77a1-a138-3fd6a53a3fc6", "item": {
                "type": "SubAgentActivity", "id": "subagent-completed-01a188e4-af72-7d93-bef8-1dd3318c12bf",
                "kind": "completed", "agent_thread_id": "01a188e4-af58-79b1-82af-7164ce6d2869",
                "agent_path": "/root/checker"},
            "started_at_ms": 1793298054958, "completed_at_ms": 1793298054958}},
        {"timestamp": "2026-10-29T18:20:54.961Z", "ordinal": 54, "type": "response_item", "payload": {
            "type": "agent_message", "id": "amsg_01a188e4-bf31-72d1-9df8-e7eeff4c51c5", "author": "/root/checker",
            "recipient": "/root", "content": [{"type": "input_text", "text": (
                "Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/checker\nPayload:\n"
                "SUBAGENT REPORT: no issues")}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "01a188e4-af47-77a1-a138-3fd6a53a3fc6",
                                                           "create_time": 1793298054.961443}},
         "metadata": {"client_authored": False, "user_input_order": 9}},
    ],
}

# Each attempt plays the next recorded attempt: its stream, and its saved session in Codex's sessions folder (a
# launcher that resets CODEX_HOME writes it elsewhere: TCS_FAKE_CODEX_SESSIONS).
SESSION_CLI = r"""
import json, os, sys, time
argv = sys.argv[1:]
if "exec" not in argv:
    sys.exit(0)
sys.stdin.read()
state = os.environ["TCS_FAKE_CODEX_STATE"]
attempts = sorted(name for name in os.listdir(state) if name.startswith("argv-"))
open(os.path.join(state, "argv-%d.json" % len(attempts)), "w").write(json.dumps(argv))
plays = json.load(open(os.environ["TCS_FAKE_CODEX_EVENTS"]))
play = plays[min(len(attempts), len(plays) - 1)]
home = os.environ.get("TCS_FAKE_CODEX_SESSIONS") or os.environ["CODEX_HOME"]
folder = os.path.join(home, "sessions", time.strftime("%Y/%m/%d"))
os.makedirs(folder, exist_ok=True)
name = "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S"), play["thread"])
with open(os.path.join(folder, name), "w") as out:
    for record in play["rollout"]:
        out.write(json.dumps(record) + "\n")
for event in play["stream"]:
    sys.stdout.write(json.dumps(event) + "\n")
text = json.dumps({"statement": "S.", "notes": "Fine."})
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": text}}) + "\n")
open(argv[argv.index("-o") + 1], "w").write(text)
"""

# A coordinator that waits 1.5 s for its subagents, silent as Codex is while its root agent waits.
WAITING_CLI = r"""
import json, sys, time
argv = sys.argv[1:]
if "exec" not in argv:
    sys.exit(0)
sys.stdin.read()
wait = {"id": "item_0", "type": "collab_tool_call", "tool": "wait", "sender_thread_id": "t",
        "receiver_thread_ids": [], "prompt": None, "agents_states": {}}
sys.stdout.write(json.dumps({"type": "item.started", "item": {**wait, "status": "in_progress"}}) + "\n")
sys.stdout.flush()
time.sleep(1.5)
sys.stdout.write(json.dumps({"type": "item.completed", "item": {**wait, "status": "completed"}}) + "\n")
open(argv[argv.index("-o") + 1], "w").write(json.dumps({"statement": "S.", "notes": "Fine."}))
"""


class StandInCodex(retry_test.RetryCase):
    """A stand-in Codex CLI (`script`) on an isolated CODEX_HOME in a live run, in the test's folder."""

    script = ""

    def setUp(self):
        super().setUp()
        self.state = self.root / "codex-state"
        self.state.mkdir()
        self.home = self.root / "codex-home"
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n" + self.script)
        fake.chmod(0o700)
        self.events_file = self.root / "events.json"
        environment = mock.patch.dict(os.environ, {"TCS_FAKE_CODEX_STATE": str(self.state),
                                                    "TCS_FAKE_CODEX_EVENTS": str(self.events_file),
                                                    "CODEX_HOME": str(self.home),
                                                    "TCS_PROVER_RETRIEVAL_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        # The saved session is read when each attempt ends (a poll during these short attempts would only
        # reorder the progress records).
        for name, value in (("codex", lambda: str(fake)), ("ROLLOUT_POLL_SECONDS", 3600)):
            patcher = mock.patch.object(runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def progress(self):
        return [(event["label"], event.get("auditIndex"), event.get("failed")) for event in self.events
                if event.get("subagentProgress")]


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class SavedSessionTests(StandInCodex):
    """Codex 0.160 shows the subagents of version 2 only in the call's saved session (runtime.CodexRollout)."""

    script = SESSION_CLI

    def critic(self, *plays, count=3):
        self.events_file.write_text(json.dumps(plays))
        runtime.structured("Check S.", retry_test.SCHEMA, "critic", model="gpt-6.1-sol",
                           features=("multi_agent",), subagents=count)
        return [json.loads(path.read_text()) for path in sorted(self.state.glob("argv-*.json"))]

    def test_a_failed_subagent_of_a_version_2_critic_is_run_again_never_used(self):
        attempts = self.critic(V2_FAILED, V2_RETRIED)
        self.assertEqual(len(attempts), 2)
        # The retry continues the coordinator's conversation and asks it to run the failed subagent again.
        self.assertIn("fork", attempts[1])
        self.assertEqual(attempts[1][-2:], [V2_FAILED["thread"], "-"])
        requests = [event for event in self.events if event["kind"] == "request"]
        self.assertEqual([request["attempt"] for request in requests], [1, 2])
        self.assertIn("RETRY AFTER FAILED SUBAGENT CALLS", requests[1]["text"])
        self.assertIn("1 subagent call in your previous attempt failed", requests[1]["text"])
        set_aside = [event["text"] for event in self.events if event["kind"] == "diagnostic"]
        self.assertEqual(len(set_aside), 1)
        self.assertIn("was set aside because 1 subagent call failed", set_aside[0])
        self.assertIn("experiencing high demand", set_aside[0])
        # The saved session also names each critic's start and end, which the stream does not.
        self.assertEqual(self.progress(), [
            ("3 independent critics requested", None, None), ("Waiting for the independent critics", None, None),
            ("Independent critic 1 started", 1, None), ("Independent critic 1 finished", 1, True),
            ("3 independent critics requested", None, None), ("Waiting for the independent critics", None, None),
            ("Independent critic 1 started", 1, None), ("Independent critic 1 finished", 1, None)])

    def test_a_subagent_that_errored_and_then_reported_counts_as_finished(self):
        recovered = {**V2_FAILED, "rollout": [*V2_FAILED["rollout"], *V2_RETRIED["rollout"][2:]]}
        self.assertEqual(len(self.critic(recovered)), 1)
        self.assertEqual([event for event in self.events if event["kind"] == "diagnostic"], [])

    def test_a_saved_session_that_cannot_be_found_is_said(self):
        # A launcher that resets CODEX_HOME saves the session where the runner does not look.
        with mock.patch.dict(os.environ, {"TCS_FAKE_CODEX_SESSIONS": str(self.root / "elsewhere")}):
            self.assertEqual(len(self.critic(V2_FAILED)), 1)
        diagnostics = [event["text"] for event in self.events if event["kind"] == "diagnostic"]
        self.assertEqual(diagnostics, ["Codex's saved session of this attempt could not be read, so whether one of "
                                       "its independent critics' model calls failed is not known for this attempt."])

    def test_the_session_is_followed_while_the_call_runs(self):
        conversation = runtime.StructuredConversation()
        conversation.progress = runtime.SubagentProgress("critic", 3)
        folder = self.home / "sessions" / time.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        rollout = runtime.CodexRollout(self.home, V2_RETRIED["thread"], conversation)
        self.assertFalse(rollout.read())  # Not written yet.
        path = folder / f"rollout-2026-10-29T19-20-50-{V2_RETRIED['thread']}.jsonl"
        started, completed, report = (json.dumps(record) for record in V2_RETRIED["rollout"])
        path.write_text(started + "\n" + completed[:40])  # A record Codex is still writing waits for its end.
        self.assertTrue(rollout.read())
        self.assertEqual(self.progress(), [("Independent critic 1 started", 1, None)])
        with path.open("a") as stream:
            stream.write(completed[40:] + "\n" + report + "\n")
        with mock.patch.object(runtime, "ROLLOUT_POLL_SECONDS", 0.01):
            rollout.follow()
            deadline = time.monotonic() + 10
            while len(self.progress()) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(rollout.stop())
        self.assertEqual(self.progress(), [("Independent critic 1 started", 1, None),
                                           ("Independent critic 1 finished", 1, None)])
        self.assertEqual(conversation.subagent_failures, {})


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class WaitingCoordinatorTests(StandInCodex):
    """A coordinator that waits for its subagents writes nothing; the stall watchdog allows Codex's longest wait."""

    script = WAITING_CLI

    def call(self):
        return runtime.structured("Check S.", retry_test.SCHEMA, "critic", features=("multi_agent",), subagents=2,
                                  timeout=0.5, attempts=1)[0]

    def test_an_open_wait_is_not_a_stall(self):
        # Scaled down: the watchdog allows 0.5 s of silence; the coordinator waits 1.5 s.
        with mock.patch.object(runtime, "CODEX_LONGEST_WAIT_SECONDS", 10):
            self.assertEqual(self.call(), {"statement": "S.", "notes": "Fine."})

    def test_the_wait_still_has_a_bound(self):
        with mock.patch.object(runtime, "CODEX_LONGEST_WAIT_SECONDS", 0.2), \
                self.assertRaisesRegex(runtime.StructuredAttemptTimeout, "wrote no output for"):
            self.call()


class NoSubagentToolTests(CriticCase):
    """GPT 5.6 Luna (older) gets no subagent tool from Codex 0.160: the run says so."""

    def test_the_critic_and_the_author_say_that_luna_has_no_subagents(self):
        self.assertEqual([runtime.multi_agent_version(model) for model in
                          ("gpt-6.1-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-luna", "gpt-5.6-luna",
                           "claude-opus-5-5")], [2, 2, 2, 2, 0, None])
        self.run_critic(unchanged, {"critic_model": "gpt-5.6-luna"})
        notes = self.labelled("No subagents with this model")
        self.assertEqual([note["text"] for note in notes], [
            "GPT 5.6 Luna (older) gets no subagent tool from Codex, so the critic reviews alone, without its 3 "
            "independent critics. Choose another model to have them."])
        self.events.clear()
        self.run_critic(unchanged)
        self.assertEqual(self.labelled("No subagents with this model"), [])
        runtime.note_no_subagent_tool("solve", "gpt-5.6-luna", 2)
        runtime.note_no_subagent_tool("solve", "gpt-6.1-sol", 2)
        runtime.note_no_subagent_tool("solve", "gpt-5.6-luna", 0)
        self.assertEqual([event["text"] for event in self.labelled("No subagents with this model")], [
            "GPT 5.6 Luna (older) gets no subagent tool from Codex, so the author works without the 2 subagents that "
            "Author subagents allows. Choose another model to have them."])

    def test_a_web_cutoff_critic_of_version_2_says_what_is_not_observable(self):
        runtime.note_unobservable_subagents("critic", "gpt-6.1-sol", 3, {"mode": "historical"})
        for model, policy in (("gpt-6.1-sol", None), ("claude-opus-5-5", {"mode": "historical"})):
            runtime.note_unobservable_subagents("critic", model, 3, policy)
        notes = self.labelled("Independent critics not observable")
        self.assertEqual(len(notes), 1)
        self.assertIn("whether one of their model calls failed, is not known", notes[0]["text"])


class UltraTests(unittest.TestCase):
    def test_ultra_is_no_longer_offered_and_runs_as_max(self):
        self.assertNotIn("ultra", runtime.EFFORTS)
        self.assertEqual(providers.RETIRED_EFFORTS, {"ultra": "max"})
        for model in runtime.MODELS:
            self.assertNotIn("ultra", runtime.MODEL_EFFORTS[model])
            self.assertEqual(runtime.effective_effort(model, "ultra"), "max")
        self.assertEqual(runtime.chosen_effort("ultra"), "ultra")  # Accepted from saved jobs and scripts.
        markup = (server.UI / "index.html").read_text()
        self.assertNotIn('value="ultra"', markup)
        self.assertNotIn("Ultra", markup)
        self.assertNotIn("ultra", server.PUBLIC_GRAPH["settings"]["reasoning_efforts"])

    def test_a_flag_that_says_ultra_is_accepted_and_said_once(self):
        records = []
        with mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: records.append(fields)):
            runtime.note_retired_efforts(["ultra", "max", "ultra"])
            runtime.note_retired_efforts(["max", None])
        self.assertEqual([record["text"] for record in records], ["Ultra is no longer offered; this run uses Max."])
        parser = __import__("argparse").ArgumentParser()
        parser.add_argument("--effort", type=runtime.effort_argument, metavar=runtime.EFFORT_METAVAR)
        self.assertEqual(parser.parse_args(["--effort", "ultra"]).effort, "ultra")
        with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
            parser.parse_args(["--effort", "turbo"])
        self.assertIn("Choose one of: low, medium, high, xhigh, max", error.getvalue())
        self.assertEqual(runtime.EFFORT_METAVAR, "{low,medium,high,xhigh,max}")
        # The documented choices of the terminal run name no Ultra.
        with contextlib.redirect_stdout(io.StringIO()) as help_text, mock.patch.object(cli.sys, "argv",
                                                                                      ["web_ui.py", "--help"]):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertNotIn("ultra", help_text.getvalue())
        self.assertIn("-authorSubagents", help_text.getvalue())

    def test_the_runner_says_once_that_a_saved_ultra_runs_as_max(self):
        records = []
        with mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: records.append(fields)), \
                mock.patch.object(runtime, "execute_workflows", return_value={"output": "x"}), \
                mock.patch.object(runtime, "check_clis"), mock.patch.object(runtime.claude_cli, "sweep_stale_settings"), \
                mock.patch.object(runtime.sys, "stdin", io.StringIO("Task")):
            self.assertEqual(runtime.main([str(AUTHOR_CRITIC), "--author-effort", "ultra", "--critic-effort",
                                           "ultra", "--author-subagents", "2"]), 0)
        self.assertEqual([record.get("text") for record in records if record.get("retiredEffort")],
                         ["Ultra is no longer offered; this run uses Max."])
        self.assertEqual(runtime.effective_effort("gpt-6.1-sol", "ultra"), "max")


class AuthorSubagentsSettingTests(unittest.TestCase):
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

    def runner_value(self):
        argv = self.launches[-1]
        return argv[argv.index("--author-subagents") + 1]

    def test_the_setting_has_its_range_default_and_validation(self):
        self.assertEqual(server.PUBLIC_GRAPH["settings"]["author_subagents"], {"default": 0, "minimum": 0, "maximum": 10})
        self.assertEqual(server.empty_state()["authorSubagents"], 0)
        for value, message in ((-1, "Choose 0 to 10 author subagents"), (11, "Choose 0 to 10 author subagents"),
                               (1.5, "must be an integer"), ("two", "must be an integer"), (True, "must be an integer")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                server.App._workflow_options(author_subagents=value, include_review=False)
        self.assertEqual(server.App._workflow_options(author_subagents="4", include_review=False)["authorSubagents"], 4)
        # Without the author stage it is not used.
        options = server.App._workflow_options(author_subagents=99, workflow_stages={
            "review": False, "author": False, "critic": True, "writing": False})
        self.assertEqual(options["authorSubagents"], 0)

    def test_a_job_saves_it_passes_it_resumes_with_it_and_restores_it(self):
        app = server.App(runs=self.runs, load_latest=False)
        app.start_direct_statement(STATEMENT, author_subagents=3)
        self.assertEqual(self.runner_value(), "3")
        self.assertEqual(app.snapshot()["authorSubagents"], 3)
        for path in (server.controller_directory(app.run_dir), app.run_dir):
            self.assertEqual(json.loads((path / server.JOB_SETTINGS_FILENAME).read_text())["authorSubagents"], 3)
        app.process = app.active_token = app.worker_token = None
        app.add_trace({"kind": "request", "stage": "solve", "node": "author", "label": "Exact solve input"})
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["authorSubagents"], 3)
        restored.resume()
        self.assertEqual(self.runner_value(), "3")
        # A saved value out of range is reported (the default is used); a valid one is not.
        self.assertIn("authorSubagents setting", server.saved_setting_problem("authorSubagents", 12))
        self.assertEqual(server.saved_setting_problem("authorSubagents", 0), "")

    def test_the_terminal_and_the_batches_carry_it(self):
        self.assertEqual(cli.direct_cli_options(author_subagents=2)["author_subagents"], 2)
        with self.assertRaisesRegex(ValueError, "Choose 0 to 10 author subagents"):
            cli.direct_cli_options(author_subagents=12)
        root = self.runs / "batches"
        directory = root / "search" / "job"
        directory.mkdir(parents=True)
        (directory / "draft.md").write_text("# Draft problem\n\nA finite theorem.\n")
        (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps({"authorSubagents": 5}))
        self.assertEqual(batches.BatchCatalog(root, current_root=root / "other").state("search", "job")
                         ["authorSubagents"], 5)


class AuthorSubagentsRunnerTests(CriticCase):
    def run_author(self, options):
        launched = []

        def author(module, prompt, **kwargs):
            launched.append((prompt, kwargs))
            yield {"outcome": "done", "output": PROOF}

        with mock.patch.object(runtime, "goal_session", side_effect=author), \
                mock.patch.object(runtime, "run_structured_attempt", side_effect=self.attempts(unchanged)):
            runtime.execute_workflows([AUTHOR_CRITIC], {"statement": "Task"}, options)
        return launched[0]

    def test_without_subagents_the_author_gets_no_tool_and_no_sentence(self):
        prompt, kwargs = self.run_author({})
        self.assertEqual(kwargs["features"], [])
        self.assertEqual(kwargs["settings"]["subagents"], 0)
        self.assertNotIn("independent subagents", prompt)
        self.assertNotIn("subagents", kwargs["prompts"])

    def test_with_subagents_the_author_gets_the_tool_its_limit_and_the_sentence(self):
        prompt, kwargs = self.run_author({"author_subagents": 3})
        self.assertEqual(kwargs["features"], ["multi_agent"])
        self.assertEqual(kwargs["settings"]["subagents"], 3)
        self.assertTrue(prompt.endswith("\n\n" + AUTHOR_SUBAGENTS_PROMPT.format(author_subagents=3)))
        self.assertIn("Task", prompt)


class RetiredPromptTests(unittest.TestCase):
    def test_a_saved_retired_default_critic_prompt_gives_way_to_the_current_one(self):
        retired = server.RETIRED_DEFAULT_PROMPTS["critic"][0]
        self.assertIn("The controller runs several fresh, independent critics", retired)
        self.assertEqual(server.current_prompt("critic", retired + "\n"), server.default_prompts()["critic"])
        self.assertEqual(server.current_prompt("critic", "My own critic prompt."), "My own critic prompt.")
        self.assertEqual(server.current_prompt("author", retired), retired)
        with self.assertRaisesRegex(ValueError, "The critic prompt cannot be used"):
            server.App._workflow_options(critic_prompt="Use {critics} critics.", include_review=False)


if __name__ == "__main__":
    unittest.main()
