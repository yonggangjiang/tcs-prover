"""The author coordinator (Author subagents 1 or more): its prompts and their checks, how a run chooses it, its
launch, its time checks, the hand-in guard, its subagents' progress records and its Codex and Claude Code settings.

Offline: the Codex author runs against the scripted RPC double of tests/test_goal_runtime.py, the Claude author
against the stand-in CLI of tests/test_claude_cli.py; goal sessions and conversation turns are stubbed wherever a
regression could start a CLI. No model is started.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli

import test_author_conversation as conversation_test
import test_claude_cli as claude_test
import test_critic_subagents as critic_test
import test_goal_runtime as goal_test

ROOT = Path(runtime.__file__).parent
WORKFLOW = ROOT / "workflows/author_critic.yaml"
AUTHOR_ONLY = {"review": False, "author": True, "critic": False, "writing": False}

# The author coordinator's prompts, the owner's final wording of 12 Nov 2026 (coordinator-prompts-final.md),
# character for character; any change needs the owner's approval.
COORDINATOR_PROMPTS = {
    "coordinator": (
        "You are the coordinator of a team of author subagents working on the statement below. You do "
        "not try to prove the statement yourself. Your job is to run and check the healthiness of the "
        "team so that progress on solving the problem grows steadily with the time spent, until the team "
        "has a complete proof that has been checked.\n"
        "\n"
        "The team must solve it, even if it looks like a long-standing open problem. Do not let the team "
        "give up because the problem is hard or open.\n"
        "\n"
        "YOUR TEAM\n"
        "You can run up to {author_subagents} author subagents at a time. You should provide them with "
        "the exact statement to be solved, ask them to think deeply and hard, and try substantially "
        "diversified approaches to make sure the progress grows steadily.\n"
        "\n"
        "YOUR MEMORY\n"
        "Your conversation will be compacted from time to time, and whatever is not written down is "
        "lost, including what your subagents are doing. Keep the team's memory in the memory folder of "
        "your working directory, organized as you see fit. You should observe and decide how the memory "
        "should be maintained, so that the author team with this memory can make steady progress along "
        "the time, instead of circulating or being stuck. Keep a log of the team's progress in "
        "memory/progress.md; the user reads it on the job page.\n"
        "\n"
        "EXAMINING PROGRESS\n"
        "The whole trajectory of all your and the author team's memory is in transcript.jsonl in your "
        "working directory for you to examine the progress (it is large, so search it rather than read "
        "it whole). There are no clear criteria on the progress towards an open problem, and you are "
        "responsible to use the trajectory and the memory to judge yourself about whether the team is "
        "making steady progress towards resolving the problem as time grows (TCS Prover sends you the "
        "elapsed time in a time check every hour). If you realize any ill behavior that the progress is "
        "blocked or growing slowly with the time, you should examine the prompt, how you organize the "
        "team, and the memory of your design, to try to guide the team back to steady progress.\n"
        "\n"
        "HANDING IN\n"
        "When the team has a complete proof that has been checked, write it to proof.md and reply with "
        "exactly this line and nothing else:\n"
        "PROOF_FILE: proof.md\n"
        "The critic reads only proof.md and the statement, so the proof must be complete and "
        "self-contained.\n"
        "\n"
        "STATEMENT:\n"
        "[STATEMENT]"
    ),
    "coordinator_goal": (
        "Coordinate the team as your coordinator prompt says, until the team hands in a complete proof "
        "that has been checked."
    ),
    "coordinator_continuation": (
        "Continue coordinating the team as your coordinator prompt says; do not end the work."
    ),
    "coordinator_reopen": (
        "ORIGINAL COORDINATOR PROMPT:\n"
        "{original_prompt}"
    ),
    "coordinator_compaction": (
        "ORIGINAL COORDINATOR PROMPT:\n"
        "{original_prompt}"
    ),
    "coordinator_resume": (
        "ORIGINAL COORDINATOR PROMPT:\n"
        "{original_prompt}"
    ),
    "coordinator_repair": (
        "The critic rejected the proof the team handed in. Save the current candidate below to a file "
        "first: it holds the critic's safe fixes, which proof.md does not. Record the critic's findings "
        "in your memory and have the team fix every unresolved error in that candidate. If not fixable, "
        "resume the solution finding progress using the original prompt.\n"
        "\n"
        "CURRENT CANDIDATE:\n"
        "{solution}\n"
        "\n"
        "CRITIC BUGS:\n"
        "{bugs}\n"
        "\n"
        "ORIGINAL COORDINATOR PROMPT:\n"
        "{original_prompt}"
    ),
    "coordinator_summary": (
        "The work is paused. Write a progress summary for the user from the team's memory: what has been "
        "verified so far, with precise statements, and where it is written; what remains open or "
        "uncertain, and why; and the most promising next steps. Do not continue the work, start "
        "subagents or change any files in this turn."
    ),
    "coordinator_checkin": (
        "[TIME CHECK FROM TCS PROVER]\n"
        "Elapsed: {elapsed}. {limit}"
    ),
    "coordinator_report": (
        "Your last reply was not handed to the critic: TCS Prover takes a reply as a hand-in only when "
        "it is the line PROOF_FILE: proof.md and nothing else, so it was kept as a progress report. If "
        "the team's proof is complete and checked, write it to proof.md and reply with exactly that "
        "line. Otherwise keep coordinating the team; do not end the work."
    ),
}
# The coordinator's Codex delegation hint (workflow_runner.COORDINATOR_HINT), approved with them.
COORDINATOR_HINT = (
    "Multi-agent delegation is the purpose of this task: the root agent is a coordinator that hands "
    "the problem-solving to up to {count} subagent{s} at a time without asking the user, and waiting "
    "for them is part of its normal work. Start each subagent fresh, without the root's conversation "
    "history (set spawn_agent's fork_turns to \"none\", or fork_context to false, where the option "
    "exists), with a self-contained assignment; wait for subagents with waits of up to 30 minutes; "
    "where close_agent exists, close each finished subagent after recording its outcome. Subagents "
    "do not start subagents of their own."
)
# The lifecycle prompts the coordinator binding replaces or adds; the others stay the author's.
COORDINATOR_LIFECYCLE = {"goal": "coordinator_goal", "continuation": "coordinator_continuation",
                         "reopen": "coordinator_reopen", "compaction": "coordinator_compaction",
                         "resume": "coordinator_resume", "repair": "coordinator_repair",
                         "summary": "coordinator_summary", "checkin": "coordinator_checkin",
                         "report": "coordinator_report"}


def custom_workflow(directory, change):
    """A copy of the bundled workflow with ``change`` applied to its YAML data, loaded."""

    definition = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    change(definition)
    path = Path(directory) / "custom.yaml"
    path.write_text(yaml.safe_dump(definition), encoding="utf-8")
    return runtime.load_workflow(path)


class CoordinatorPromptTests(unittest.TestCase):
    """The YAML: the default 3, the coordinator binding and its prompts, and how they are checked."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)

    def prepared(self, change=None, **options):
        workflow = custom_workflow(self.directory, change or (lambda definition: None))
        with mock.patch.object(runtime, "require_model_credentials"):
            return runtime.prepare(workflow, options)

    def test_only_a_coordinator_binding_answers_a_reply_that_is_not_a_hand_in(self):
        node = runtime.load_workflow(WORKFLOW)["nodes"]["author"]
        self.assertEqual(node["coordinator"]["lifecycle"]["report"], "coordinator_report")

        def bind_report_to_the_author(workflow):
            workflow["nodes"]["author"]["lifecycle"].append("report")
            workflow["prompts"]["report"] = "Not handed in."

        with self.assertRaises(ValueError):
            custom_workflow(self.directory, bind_report_to_the_author)

    def test_the_coordinator_prompts_are_the_texts_awaiting_the_owners_review(self):
        workflow = runtime.builtin_workflow("author_critic")
        node = workflow["nodes"]["author"]
        self.assertEqual(node["subagents"], {"option": "author_subagents", "default": 3, "minimum": 0})
        self.assertEqual(node["coordinator"], {"prompt": "coordinator", "lifecycle": COORDINATOR_LIFECYCLE})
        for name, text in COORDINATOR_PROMPTS.items():
            with self.subTest(prompt=name):
                self.assertEqual(workflow["prompts"][name], text)
        self.assertEqual(runtime.COORDINATOR_HINT, COORDINATOR_HINT)
        self.assertEqual(runtime.COORDINATOR_WAIT_MS, 1_800_000)
        # Every prompt the binding names is one of these, and the single author's prompts are unchanged.
        self.assertEqual(set(node["coordinator"]["lifecycle"].values()) | {"coordinator"}, set(COORDINATOR_PROMPTS))
        self.assertEqual(node["lifecycle"], ["goal", "continuation", "compaction", "repair", "resume", "reopen",
                                             "summary", "conversation", "chat", "related_works", "handoff_refused",
                                             "subagents"])

    def test_the_first_prompt_needs_one_statement_and_may_name_only_its_number_of_subagents(self):
        self.assertEqual(self.prepared()["coordinator"], COORDINATOR_PROMPTS["coordinator"])
        for template, message in (("Coordinate.", "exactly one \\[STATEMENT\\]"),
                                  ("[STATEMENT] [STATEMENT]", "exactly one \\[STATEMENT\\]"),
                                  ("Run {author_subagents} on [STATEMENT] in {directory}.", "Unknown prompt input"),
                                  ("Run {count} on [STATEMENT].", "Unknown prompt input")):
            with self.subTest(template=template), self.assertRaisesRegex(ValueError, message):
                self.prepared(lambda definition: definition["prompts"].update(coordinator=template))
            # A per-job override is checked the same way.
            with self.subTest(override=template), self.assertRaisesRegex(ValueError, message):
                self.prepared(prompts={"coordinator": template})
        # An author that works alone never uses them: a broken coordinator prompt does not stop it.
        self.prepared(prompts={"coordinator": "Coordinate {nothing}."}, author_subagents=0)
        self.prepared(prompts={"coordinator": "Coordinate {nothing}."}, author_mode="author")

    def test_each_lifecycle_prompt_may_use_exactly_what_its_author_counterpart_may(self):
        def broken(name, text):
            return lambda definition: definition["prompts"].update({name: text})

        # The time check names the elapsed time and the time limit, and nothing else.
        self.prepared(broken("coordinator_checkin", "{elapsed} {limit}"))
        for text in ("{elapsed} {original_prompt}", "{solution}", "{message}"):
            with self.subTest(checkin=text), self.assertRaisesRegex(ValueError, "Unknown prompt input"):
                self.prepared(broken("coordinator_checkin", text))
        # The others: the author's fields ({original_prompt}, {solution}, ...), never the time check's.
        self.prepared(broken("coordinator_compaction", "{original_prompt} {directory} {round}"))
        for name, text in (("coordinator_compaction", "{elapsed}"), ("coordinator_resume", "{limit}"),
                           ("coordinator_repair", "{reason}"), ("coordinator_summary", "{message}"),
                           ("coordinator_goal", "{author_subagents}")):
            with self.subTest(prompt=name), self.assertRaisesRegex(ValueError, "Unknown prompt input"):
                self.prepared(broken(name, text))

    def test_checkin_is_a_key_of_the_coordinator_binding_only_and_the_binding_needs_subagents(self):
        def with_author_checkin(definition):
            definition["nodes"]["author"]["lifecycle"].append("checkin")
            definition["prompts"]["checkin"] = "Check."

        with self.assertRaisesRegex(ValueError, "must bind its lifecycle prompts"):
            custom_workflow(self.directory, with_author_checkin)
        for key in ("chat", "subagents", "pause", "progress"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "coordinator needs a prompt"):
                custom_workflow(self.directory, lambda definition: definition["nodes"]["author"]["coordinator"][
                    "lifecycle"].update({key: "coordinator_goal"}))
        for change in (lambda definition: definition["nodes"]["author"]["coordinator"].update(prompt="missing"),
                       lambda definition: definition["nodes"]["author"]["coordinator"].update(extra="x"),
                       lambda definition: definition["nodes"]["author"]["coordinator"]["lifecycle"].update(
                           goal="missing")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "coordinator needs a prompt"):
                custom_workflow(self.directory, change)
        with self.assertRaisesRegex(ValueError, "needs subagents for its coordinator"):
            custom_workflow(self.directory, lambda definition: definition["nodes"]["author"].pop("subagents"))
        # The binding may replace any other lifecycle prompt the runner renders (as a list too).
        custom_workflow(self.directory, lambda definition: definition["nodes"]["author"]["coordinator"].update(
            lifecycle=["goal", "conversation", "handoff_refused"]))

    def test_a_coordinator_run_of_a_workflow_without_the_binding_stops_before_any_model_starts(self):
        def without_binding(definition):
            del definition["nodes"]["author"]["coordinator"]

        with self.assertRaisesRegex(runtime.ConfigurationError, "has no coordinator prompts"):
            self.prepared(without_binding, author_mode="coordinator", author_subagents=3)
        self.prepared(without_binding, author_subagents=3)  # auto: the author alone.
        # The launch itself: a settings error before the author's session, never a pause that is retried.
        workflow = custom_workflow(self.directory, without_binding)
        events = []
        with mock.patch.object(runtime, "require_model_credentials"), \
                mock.patch.object(runtime, "goal_session", side_effect=AssertionError("a model started")), \
                mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: events.append(kind)), \
                self.assertRaisesRegex(runtime.ConfigurationError, "has no coordinator prompts"):
            runtime.execute_workflows([self.directory / "custom.yaml"], {"statement": "S."}, {
                "workflow_stages": AUTHOR_ONLY, "author_mode": "coordinator", "author_subagents": 3,
                "goal_cwd": str(self.directory)})
        self.assertNotIn("workflow_paused", events)
        self.assertEqual(workflow["nodes"]["author"].get("coordinator"), None)

    def test_the_coordinator_prompts_are_per_job_like_the_author_prompt(self):
        self.assertEqual(runtime.PROMPT_FILE_OPTIONS["coordinator"], "coordinator")
        self.assertEqual(self.prepared(prompts={"coordinator": "My team on [STATEMENT], {author_subagents}."})[
            "coordinator"], "My team on [STATEMENT], {author_subagents}.")


class AuthorModeTests(unittest.TestCase):
    """How a run's author works (--author-mode, author_mode())."""

    def setUp(self):
        self.node = runtime.builtin_workflow("author_critic")["nodes"]["author"]

    def test_the_mode_follows_the_number_unless_a_mode_is_given(self):
        for options, mode in (({}, "coordinator"), ({"author_subagents": 1}, "coordinator"),
                              ({"author_subagents": 0}, "author"), ({"author_mode": "auto"}, "coordinator"),
                              ({"author_mode": "author"}, "author"),
                              ({"author_mode": "author", "author_subagents": 0}, "author"),
                              ({"author_mode": "coordinator", "author_subagents": 2}, "coordinator")):
            with self.subTest(options=options):
                self.assertEqual(runtime.author_mode(self.node, options), mode)
        # A mode the run cannot have is a settings problem, never retried.
        with self.assertRaisesRegex(runtime.ConfigurationError, "needs Author subagents 1 or more"):
            runtime.author_mode(self.node, {"author_mode": "coordinator", "author_subagents": 0})
        with self.assertRaisesRegex(runtime.ConfigurationError, "Choose auto, coordinator or author"):
            runtime.author_mode(self.node, {"author_mode": "team"})
        # A goal node without a coordinator binding has an author only.
        plain = {key: value for key, value in self.node.items() if key != "coordinator"}
        self.assertEqual(runtime.author_mode(plain, {"author_subagents": 3}), "author")
        with self.assertRaisesRegex(runtime.ConfigurationError, "has no coordinator prompts"):
            runtime.author_mode(plain, {"author_mode": "coordinator"})

    def test_the_command_line_takes_the_mode_and_the_coordinator_prompt_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "coordinator.txt"
            path.write_text("Coordinate [STATEMENT] with {author_subagents}.", encoding="utf-8")
            captured = []
            with mock.patch.object(runtime, "execute_workflows",
                                   side_effect=lambda paths, state, options: captured.append(options) or {}), \
                    mock.patch.object(runtime, "check_clis"), mock.patch.object(claude_cli, "sweep_stale_settings"):
                for arguments in (["--author-mode", "author", "--coordinator-prompt-file", str(path)], []):
                    with mock.patch.object(runtime.sys, "stdin", io.StringIO("Task")):
                        self.assertEqual(runtime.main([str(WORKFLOW), *arguments]), 0)
        self.assertEqual(captured[0]["author_mode"], "author")
        self.assertEqual(captured[0]["prompts"]["coordinator"], "Coordinate [STATEMENT] with {author_subagents}.")
        # Without the options the runner's default is auto, with the workflow's Author subagents default (3).
        self.assertNotIn("author_mode", captured[1])
        self.assertNotIn("author_subagents", captured[1])
        with mock.patch.object(runtime.sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
            runtime.main([str(WORKFLOW), "--author-mode", "team"])


class LaunchTests(unittest.TestCase):
    """_execute: what the author's goal session is started with, for each way the author works."""

    STATEMENT = "Exact statement {x} with braces."

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.workflow = runtime.load_workflow(WORKFLOW)
        self.prompts = self.workflow["prompts"]
        self.events = []
        handoff = mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=True)
        handoff.start()
        self.addCleanup(handoff.stop)

    def launch(self, options=None, state=None, claude=False):
        """Run the author node once with a stub session; return (first prompt, the session's keywords)."""

        calls = []

        def session(module, prompt, **kwargs):
            calls.append((prompt, kwargs))
            yield {"outcome": "done", "output": "A complete inline proof."}

        codex = mock.patch.object(runtime, "goal_session",
                                  side_effect=AssertionError("Codex session") if claude else session)
        anthropic = mock.patch.object(claude_cli, "goal_session",
                                      side_effect=session if claude else AssertionError("Claude session"))
        with codex, anthropic, mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields:
                                                 self.events.append({"kind": kind, "stage": stage, **fields})):
            runtime._execute(self.workflow, {"statement": self.STATEMENT, **(state or {})},
                             {"workflow_stages": AUTHOR_ONLY, "goal_cwd": str(self.directory), **(options or {})},
                             self.prompts)
        (prompt, kwargs), = calls
        return prompt, kwargs

    def coordinator_prompt(self, count=3):
        return COORDINATOR_PROMPTS["coordinator"].replace("{author_subagents}", str(count)).replace(
            "[STATEMENT]", self.STATEMENT)

    def test_a_coordinator_starts_with_its_own_prompts_its_subagents_and_its_memory_folder(self):
        prompt, kwargs = self.launch()
        # The number fills the coordinator prompt; the statement's braces stay text.
        self.assertEqual(prompt, self.coordinator_prompt())
        self.assertNotIn("You may start up to", prompt)
        self.assertIs(kwargs["coordinator"], True)
        self.assertNotIn("alone", kwargs)
        self.assertEqual((kwargs["features"], kwargs["settings"]["subagents"]), (["multi_agent"], 3))
        # Its lifecycle prompts replace the author's of the same key; the chat framing, the conversation
        # reply, the related-works block and the note on a refused submission stay shared.
        expected = {key: self.prompts[COORDINATOR_LIFECYCLE.get(key, key)]
                    for key in [*self.workflow["nodes"]["author"]["lifecycle"], "checkin", "report"]
                    if key != "subagents"}
        self.assertEqual(kwargs["prompts"], expected)
        for key in ("chat", "conversation", "related_works", "handoff_refused"):
            self.assertEqual(kwargs["prompts"][key], self.prompts[key])
        # The memory folder, owner-only, and nothing else in the working folder.
        memory = self.directory / "memory"
        self.assertTrue(memory.is_dir())
        self.assertEqual(stat.S_IMODE(memory.stat().st_mode) & 0o077, 0)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["memory"])

    def test_the_memory_folder_never_replaces_what_is_there(self):
        (self.directory / "memory").write_text("the coordinator's own file")
        self.launch()
        self.assertEqual((self.directory / "memory").read_text(), "the coordinator's own file")
        (self.directory / "memory").unlink()
        (self.directory / "memory").mkdir(mode=0o755)
        (self.directory / "memory" / "progress.md").write_text("- 1 h: Lemma 1 verified.\n")
        os.chmod(self.directory / "memory", 0o755)
        self.launch()
        self.assertEqual((self.directory / "memory" / "progress.md").read_text(), "- 1 h: Lemma 1 verified.\n")
        self.assertEqual(stat.S_IMODE((self.directory / "memory").stat().st_mode), 0o755)
        self.assertFalse([event for event in self.events if event["kind"] == "diagnostic"])
        # A folder that cannot be made is said; the coordinator still starts.
        (self.directory / "memory" / "progress.md").unlink()
        (self.directory / "memory").rmdir()
        with mock.patch.object(runtime.os, "mkdir", side_effect=PermissionError("denied")):
            self.launch()
        diagnostic, = [event for event in self.events if event["kind"] == "diagnostic"]
        self.assertIn("Could not create the coordinator's memory folder (denied)", diagnostic["text"])

    def test_the_coordinator_gets_related_works_and_the_critic_feedback_in_its_own_words(self):
        rejected = {"report": {"verdict": "reject", "bugs": "Gap in step 2."}, "solution": "Old proof.", "round": 1}
        prompt, kwargs = self.launch({"related_works": True}, rejected)
        self.assertEqual(prompt, self.coordinator_prompt() + "\n\n" + self.prompts["related_works"])
        self.assertEqual(kwargs["initial_instruction"], runtime.render_template(
            COORDINATOR_PROMPTS["coordinator_repair"], {"solution": "Old proof.", "bugs": "Gap in step 2.",
                                                        "original_prompt": prompt}))
        self.assertTrue(kwargs["initial_instruction"].endswith("ORIGINAL COORDINATOR PROMPT:\n" + prompt))
        # The single author keeps its own repair prompt.
        _, kwargs = self.launch({"author_subagents": 0}, rejected)
        self.assertEqual(kwargs["initial_instruction"], runtime.render_template(
            self.prompts["repair"], {"solution": "Old proof.", "bugs": "Gap in step 2."}))

    def test_the_note_on_a_refused_submission_is_the_bound_one(self):
        reason = "Cannot hand the submitted proof to the critic: (proof.md: file not found)."
        _, kwargs = self.launch(state={"handoff_refused": reason})
        self.assertEqual(kwargs["handoff_note"], runtime.render_template(self.prompts["handoff_refused"],
                                                                         {"reason": reason}))

        def own_note(definition):
            definition["prompts"]["coordinator_refused"] = "The team's last proof was refused: {reason}"
            definition["nodes"]["author"]["coordinator"]["lifecycle"]["handoff_refused"] = "coordinator_refused"

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workflow = custom_workflow(folder.name, own_note)
        self.prompts = self.workflow["prompts"]
        _, kwargs = self.launch(state={"handoff_refused": reason})
        self.assertEqual(kwargs["handoff_note"], "The team's last proof was refused: " + reason)
        _, kwargs = self.launch({"author_subagents": 0}, {"handoff_refused": reason})
        self.assertTrue(kwargs["handoff_note"].startswith("Your last submission was not handed to the critic"))

    def test_where_proof_files_cannot_be_read_the_coordinator_is_asked_for_the_proof_in_its_reply_too(self):
        note = "\n\n" + runtime.INLINE_PROOF_NOTE
        rejected = {"report": {"verdict": "reject", "bugs": "Gap."}, "solution": "Old.", "round": 1}
        with mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=False):
            prompt, kwargs = self.launch(state=rejected)
        self.assertEqual(prompt, self.coordinator_prompt() + note)
        for key, text in kwargs["prompts"].items():
            with self.subTest(prompt=key):
                self.assertEqual(text.endswith(note), key in ("continuation", "repair"))
        self.assertTrue(kwargs["prompts"]["continuation"].startswith(COORDINATOR_PROMPTS["coordinator_continuation"]))
        self.assertTrue(kwargs["initial_instruction"].endswith(note))

    def test_without_subagents_the_author_works_alone_as_before(self):
        prompt, kwargs = self.launch({"author_subagents": 0})
        self.assertEqual(prompt, self.prompts["author"].replace("[STATEMENT]", self.STATEMENT))
        self.assertNotIn("coordinator", kwargs)
        self.assertNotIn("checkin", kwargs["prompts"])
        self.assertEqual(kwargs["prompts"]["goal"], self.prompts["goal"])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_a_codex_model_without_a_subagent_tool_works_alone_and_says_so(self):
        prompt, kwargs = self.launch({"author_model": "gpt-5.6-luna"})
        # Today's single-author prompts, without the subagents note, and no subagent feature.
        self.assertEqual(prompt, self.prompts["author"].replace("[STATEMENT]", self.STATEMENT))
        self.assertNotIn("coordinator", kwargs)
        self.assertEqual(kwargs["features"], [])
        self.assertEqual(kwargs["prompts"]["continuation"], self.prompts["continuation"])
        self.assertEqual(list(self.directory.iterdir()), [])
        note, = [event for event in self.events if event.get("label") == "No subagents with this model"]
        self.assertEqual(note["text"], "GPT 5.6 Luna (older) gets no subagent tool from Codex, so the author works "
                                       "alone instead of coordinating up to 3 author subagents. Choose another "
                                       "model to have them.")
        # The server shows it as the way the job's author works (follow_author_mode()).
        self.assertEqual((note["authorModeEffective"], note["modelLabel"]), ("author", "GPT 5.6 Luna (older)"))
        # A job recorded as a single author with subagents keeps today's note.
        self.events.clear()
        self.launch({"author_model": "gpt-5.6-luna", "author_mode": "author"})
        note, = [event for event in self.events if event.get("label") == "No subagents with this model"]
        self.assertIn("so the author works without the 3 subagents that Author subagents allows", note["text"])
        self.assertNotIn("authorModeEffective", note)

    def test_a_claude_coordinator_carries_the_single_author_for_a_cli_without_the_subagent_tool(self):
        prompt, kwargs = self.launch({"author_model": "claude-opus-5-5"}, claude=True)
        self.assertEqual(prompt, self.coordinator_prompt())
        self.assertIs(kwargs["coordinator"], True)
        alone = kwargs["alone"]
        self.assertEqual(alone["prompt"], self.prompts["author"].replace("[STATEMENT]", self.STATEMENT))
        self.assertEqual(alone["prompts"]["continuation"], self.prompts["continuation"])
        self.assertNotIn("checkin", alone["prompts"])
        self.assertEqual(alone["recovery"], "")


class Coordinator(goal_test.Runtime):
    """The goal tests' runtime double with the job's working time (workflow_elapsed) under the test's control.

    ``refusals`` time checks are refused by Codex (an error answer to turn/steer); with ``answers_probes`` the
    double answers the cheap check (thread/goal/get) a silent turn gets, as Codex's app-server does.
    """

    def __init__(self, scripts, elapsed=0.0, refusals=0, answers_probes=False):
        super().__init__(scripts)
        self.elapsed = elapsed
        self.refusals, self.answers_probes = refusals, answers_probes
        self.refused, self.probed = threading.Event(), threading.Event()

    def workflow_elapsed(self, options):
        return self.elapsed

    def RPC(self, process, record):
        rpc = super().RPC(process, record)
        request = rpc.request

        def scripted(method, params, before_send=None):
            if (method == "turn/steer" and self.refusals
                    and params["input"][0]["text"].startswith("[TIME CHECK FROM TCS PROVER]")):
                self.refusals -= 1
                rpc.calls.append((method, params))
                rpc.number += 1
                if before_send is not None:
                    before_send(rpc.number)
                rpc.messages.append({"id": rpc.number, "error": {"code": -32600, "message": "no active turn to steer"}})
                self.refused.set()
                return rpc.number
            number = request(method, params, before_send)
            if method == "thread/goal/get" and self.answers_probes:
                rpc.messages.append({"id": number, "result": {"goal": {"status": "active"}}})
                self.probed.set()
            return number

        rpc.request = scripted
        return rpc


class SavedSession(Coordinator):
    """The double whose thread/start and thread/resume name the thread's saved session, as Codex 0.160 does."""

    def __init__(self, scripts, path, **kwargs):
        super().__init__(scripts, **kwargs)
        self.path = path

    def RPC(self, process, record):
        rpc = super().RPC(process, record)
        call = rpc.call

        def named(method, params):
            result = call(method, params)
            if method in {"thread/start", "thread/resume"}:
                result = {**result, "thread": {**result["thread"], "path": str(self.path)}}
            return result

        rpc.call = named
        return rpc


def collab(tool, receivers=(), prompt=None, states=None, status="completed", **params):
    """A root collabAgentToolCall item of the Codex app-server (item/completed)."""

    return goal_test.event("item/completed", item={
        "type": "collabAgentToolCall", "id": f"{tool}-{'-'.join(receivers)}", "tool": tool, "status": status,
        "senderThreadId": "thread-1", "receiverThreadIds": list(receivers), "prompt": prompt,
        "agentsStates": states or {}}, **params)


def keeps_codex_subagent_tools(launches):
    """Whether Codex 0.160 still offers a thread's model its subagent tools after ``launches``, the params of the
    thread's thread/start and thread/resume calls in order: one with agents.enabled false takes them for good, also
    from a later resume with agents.enabled true (captured with the real Codex against a scripted model server)."""

    return not any((params["config"].get("agents") or {}).get("enabled") is False for params in launches)


PROMPTS = {**goal_test.PROMPTS, "continuation": "Keep coordinating the team.",
           "checkin": COORDINATOR_PROMPTS["coordinator_checkin"]}
SETTINGS = {"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise", "subagents": 3}
POINTER = "PROOF_FILE: proof.md"
HOUR = 3600


class CodexCoordinatorTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        self.spawn = popen.start()
        self.addCleanup(popen.stop)

    def session(self, runtime_double, coordinator=True, **options):
        session = runtime.goal_session(runtime_double, "Coordinate the team on S.", prompts=PROMPTS,
                                       settings=SETTINGS, features=["multi_agent"], coordinator=coordinator,
                                       options={"goal_cwd": str(self.directory), **options})
        self.addCleanup(session.close)
        return session

    @staticmethod
    def turns(double):
        return [params["input"][0]["text"] for method, params in double.rpc.calls if method == "turn/start"]

    @staticmethod
    def checks(double):
        return [params["input"][0]["text"] for method, params in double.rpc.calls
                if method == "turn/steer" and params["input"][0]["text"].startswith("[TIME CHECK FROM TCS PROVER]")]

    @staticmethod
    def labelled(double, label):
        return [event for event in double.events if event.get("label") == label]

    def test_a_reply_that_names_no_proof_file_is_a_report_and_the_coordinator_continues(self):
        report = "Status: Lemma 1 is verified; Lemma 2 is open."
        double = Coordinator([[goal_test.answer(report), goal_test.goal("complete"), goal_test.completed()],
                              goal_test.success(POINTER)])
        self.assertEqual(next(self.session(double)), {"outcome": "done", "output": POINTER})
        self.assertEqual(self.turns(double), ["Coordinate the team on S.", "Keep coordinating the team."])
        shown, = self.labelled(double, "Coordinator report")
        self.assertEqual(shown["text"], report)
        # Only the reply that names the proof file was handed on.
        self.assertEqual([event["text"] for event in double.events if event["kind"] == "author_result"], [POINTER])


    def test_a_binding_with_report_answers_a_reply_that_is_not_a_hand_in_with_it(self):
        # The bundled coordinator binding has "report"; without it (the test above), the continuation answers.
        double = Coordinator([[goal_test.answer("Status: Lemma 2 is open."), goal_test.goal("complete"),
                               goal_test.completed()], goal_test.success(POINTER)])
        session = runtime.goal_session(double, "Coordinate the team on S.",
                                       prompts={**PROMPTS, "report": COORDINATOR_PROMPTS["coordinator_report"]},
                                       settings=SETTINGS, features=["multi_agent"], coordinator=True,
                                       options={"goal_cwd": str(self.directory)})
        self.addCleanup(session.close)
        self.assertEqual(next(session), {"outcome": "done", "output": POINTER})
        self.assertEqual(self.turns(double), ["Coordinate the team on S.", COORDINATOR_PROMPTS["coordinator_report"]])

    def test_a_coordinators_start_says_that_it_coordinates(self):
        for coordinator in (True, False):
            with self.subTest(coordinator=coordinator):
                double = Coordinator([goal_test.success(POINTER)])
                next(self.session(double, coordinator=coordinator))
                started, = self.labelled(double, "Goal started")
                self.assertEqual(started.get("authorModeEffective"), "coordinator" if coordinator else None)

    def test_a_long_report_is_shortened_and_a_pointer_it_cannot_use_is_still_handed_on(self):
        report = "Step. " * 1000
        double = Coordinator([[goal_test.answer(report), goal_test.goal("complete"), goal_test.completed()],
                              goal_test.success("PROOF_FILE: a.md\nPROOF_FILE: b.md")])
        self.assertEqual(next(self.session(double))["output"], "PROOF_FILE: a.md\nPROOF_FILE: b.md")
        shown, = self.labelled(double, "Coordinator report")
        self.assertEqual(len(shown["text"]), runtime.COORDINATOR_REPORT_CHARS)
        self.assertIn("... [truncated; sha256=", shown["text"])

    def test_where_proof_files_cannot_be_read_and_for_an_author_any_final_reply_is_handed_on(self):
        for coordinator, supported in ((True, False), (False, True)):
            with self.subTest(coordinator=coordinator, file_handoff=supported), \
                    mock.patch.object(runtime.proof_handoff, "file_handoff_supported", return_value=supported):
                double = Coordinator([goal_test.success("The complete inline proof.")])
                self.assertEqual(next(self.session(double, coordinator=coordinator))["output"],
                                 "The complete inline proof.")
                self.assertEqual(self.labelled(double, "Coordinator report"), [])

    def test_a_time_check_goes_into_the_running_turn_on_each_whole_hour_of_working_time(self):
        # Resumed at 5 h 20 min: the next check is at 6 h.
        double = Coordinator([[]], elapsed=5 * HOUR + 20 * 60)
        double.remaining = 24 * HOUR - (6 * HOUR + 5 * 60)

        def turn(rpc):
            self.assertFalse(double.steered.wait(0.4))  # Nothing before the hour.
            double.elapsed = 6 * HOUR + 5 * 60
            self.assertTrue(double.steered.wait(5))
            rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = turn
        self.assertEqual(next(self.session(double, thinking_hours=24))["output"], POINTER)
        text = "[TIME CHECK FROM TCS PROVER]\nElapsed: 6 h 5 min. Time limit: 24 h, about 17 h 55 min left."
        self.assertEqual(self.checks(double), [text])
        steer = next(params for method, params in double.rpc.calls if method == "turn/steer")
        self.assertEqual(steer["expectedTurnId"], "turn-1")
        check, = self.labelled(double, "Coordinator time check")
        self.assertEqual((check["elapsedSeconds"], check["text"], check["kind"]), (6 * HOUR + 5 * 60, text, "status"))
        # It is no live instruction of the user's.
        self.assertEqual(self.labelled(double, "Live author instruction sent"), [])

    def test_a_check_that_comes_due_between_turns_goes_with_the_next_turn(self):
        double = Coordinator([goal_test.success(POINTER), []], elapsed=50 * 60)
        session = self.session(double)
        self.assertEqual(next(session)["output"], POINTER)
        # While the critic reviews (the session waits) no check is sent, however much time passes.
        double.elapsed = 3 * HOUR + 10 * 60
        self.assertFalse(double.steered.wait(0.4))

        def repair(rpc):
            self.assertTrue(double.steered.wait(5))
            rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = repair
        self.assertEqual(session.send({"solution": "Fixed", "bugs": "Gap.", "round": 1})["output"], POINTER)
        # One check for the hours that passed meanwhile, with the time of its sending, in the repair turn.
        check, = self.checks(double)
        self.assertIn("Elapsed: 3 h 10 min. No time limit.", check)
        self.assertEqual(next(params for method, params in double.rpc.calls if method == "turn/steer")[
            "expectedTurnId"], "turn-2")

    def test_a_runner_that_starts_at_the_critic_sends_the_check_that_came_due_during_the_review(self):
        # The runner started at 5 h 50 min of working time at the critic; its author begins at 6 h 30 min.
        double = Coordinator([[]], elapsed=6 * HOUR + 30 * 60)

        def turn(rpc):
            self.assertTrue(double.steered.wait(5))
            rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = turn
        self.assertEqual(next(self.session(double, elapsed_seconds=5 * HOUR + 50 * 60))["output"], POINTER)
        check, = self.checks(double)
        self.assertIn("Elapsed: 6 h 30 min.", check)
        # One that started at 6 h 10 min sends none before 7 h.
        quiet = Coordinator([[]], elapsed=6 * HOUR + 30 * 60)

        def silent(rpc):
            self.assertFalse(quiet.steered.wait(0.4))
            rpc.messages.extend(goal_test.success(POINTER))

        quiet.on_empty = silent
        next(self.session(quiet, elapsed_seconds=6 * HOUR + 10 * 60))
        self.assertEqual(self.checks(quiet), [])

    def test_a_check_codex_refuses_is_sent_again_in_the_next_turn(self):
        double = Coordinator([[], []], elapsed=50 * 60, refusals=1)
        steps = []

        def turns(rpc):
            steps.append(len(steps))
            if len(steps) == 1:
                double.elapsed = HOUR + 60
                self.assertTrue(double.refused.wait(5))  # The refusal is read next, while the turn runs.
            elif len(steps) == 2:
                # Not again in the turn that refused it.
                self.assertFalse(double.steered.wait(0.4))
                rpc.messages.extend([goal_test.answer("Working.", "commentary"), goal_test.goal("blocked"),
                                     goal_test.completed()])
            else:
                self.assertTrue(double.steered.wait(5))
                rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = turns
        self.assertEqual(next(self.session(double))["output"], POINTER)
        self.assertEqual(len(self.checks(double)), 2)
        refused = [event["text"] for event in double.events if event["kind"] == "diagnostic"
                   and "time check" in event["text"]]
        self.assertEqual(refused, ["Codex did not accept the time check (no active turn to steer); it is sent "
                                   "again when the next turn starts."])

    def test_no_check_is_sent_while_the_author_pauses(self):
        pause = self.directory / "pause-request.json"
        double = Coordinator([[]], elapsed=30 * 60)

        def turn(rpc):
            pause.write_text("{}")
            self.assertTrue(double.interrupted.wait(5))
            double.elapsed = 2 * HOUR
            self.assertFalse(double.steered.wait(0.4))
            rpc.messages.extend([goal_test.goal("paused"), goal_test.completed("interrupted")])

        double.on_empty = turn
        with self.assertRaises(runtime.WorkflowPaused):
            next(self.session(double, goal_pause_file=str(pause)))
        self.assertEqual(self.checks(double), [])

    def test_a_retry_goes_on_after_the_last_check_of_the_failed_attempt(self):
        double = Coordinator([[]], elapsed=55 * 60)

        def failing(rpc):
            double.elapsed = HOUR + 5 * 60
            self.assertTrue(double.steered.wait(5))
            raise runtime.Error("Network unavailable")

        double.on_empty = failing
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.session(double))
        self.assertEqual(paused.exception.retry["checked"], 1)
        # The next attempt, at 1 h 40 min, sends no second check for the same hour; at 2 h it does.
        again = Coordinator([[]], elapsed=HOUR + 40 * 60)

        def turn(rpc):
            self.assertFalse(again.steered.wait(0.4))
            again.elapsed = 2 * HOUR
            self.assertTrue(again.steered.wait(5))
            rpc.messages.extend(goal_test.success(POINTER))

        again.on_empty = turn
        self.assertEqual(next(self.session(again, goal_retry={"checked": 1}))["output"], POINTER)
        self.assertEqual(len(self.checks(again)), 1)
        # A check that came due while the attempt failed is not lost: a retry at 2 h 10 min sends it at once.
        late = Coordinator([[]], elapsed=2 * HOUR + 10 * 60)

        def sent(rpc):
            self.assertTrue(late.steered.wait(5))
            rpc.messages.extend(goal_test.success(POINTER))

        late.on_empty = sent
        self.assertEqual(next(self.session(late, goal_retry={"checked": 1}))["output"], POINTER)
        check, = self.checks(late)
        self.assertIn("Elapsed: 2 h 10 min.", check)

    def test_every_retry_of_the_runner_carries_the_last_check(self):
        def failing(checked):
            raise runtime.WorkflowPaused("Network unavailable", reason="Network unavailable", thread_id="thread-1",
                                         retry={"thread": "thread-1", "stage": "solve", "values": {}, "output": True,
                                                "instruction": "Go on.", "delivered": True, "checked": checked})
            yield

        def done():
            yield {"outcome": "done", "output": POINTER}

        attempts, carried = iter([failing(None), done()]), []

        def launch(options, initial_instruction):
            carried.append(options["goal_retry"]["checked"])
            return next(attempts)

        with mock.patch.object(runtime, "wait_before_retry"), mock.patch.object(runtime, "emit"):
            result = runtime._goal_step({"author": failing(2)}, "author", launch, "", {}, next, "solve")
        # The second attempt failed before it knew of any check: the third still goes on after 2 h.
        self.assertEqual((result["output"], carried), (POINTER, [2, 2]))

    def test_a_failing_time_check_ends_the_checks_and_never_the_watcher(self):
        double = Coordinator([[]], elapsed=50 * 60)

        def stopped():
            return [event["text"] for event in double.events if event["kind"] == "diagnostic"]

        def turn(rpc):
            double.elapsed = HOUR + 60
            deadline = time.monotonic() + 5
            while not stopped() and time.monotonic() < deadline:
                time.sleep(0.02)
            # The watcher still delivers the user's live instructions.
            double.command = ("steer-1", "Try the invariant.")
            self.assertTrue(double.steered.wait(5))
            rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = turn
        with mock.patch.object(runtime, "checkin_text", side_effect=RuntimeError("boom")):
            self.assertEqual(next(self.session(double))["output"], POINTER)
        self.assertEqual(stopped(), ["The coordinator's time checks stopped: boom"])
        self.assertEqual(self.checks(double), [])
        self.assertTrue(any(event.get("authorSteerDelivered") == "steer-1" for event in double.events))

    def test_an_author_that_works_alone_gets_no_time_checks(self):
        double = Coordinator([[]], elapsed=5 * HOUR)

        def turn(rpc):
            double.elapsed = 9 * HOUR
            self.assertFalse(double.steered.wait(0.4))
            rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = turn
        next(self.session(double, coordinator=False))
        self.assertEqual(self.checks(double), [])

    def test_the_author_subagents_are_recorded_from_the_roots_collab_items(self):
        long = "Check every case of Lemma 4 " * 20
        items = [
            collab("spawnAgent", ["agent-a"], "Prove Lemma 2 by induction on n.\nUse memory/lemma1.md.",
                   {"agent-a": {"status": "pendingInit", "message": None}}),
            collab("spawnAgent", ["agent-b"], long + "\nDetails."),
            collab("spawn_agent", ["agent-c"], ""),
            collab("spawnAgent", [], "Refused.", status="failed"),
            # A subagent's own spawn: its thread's records never touch the root's.
            collab("spawnAgent", ["agent-z"], "Nested.", threadId="child"),
            collab("wait", ["agent-a", "agent-b"], None, {
                "agent-a": {"status": "completed", "message": "Lemma 2 holds."},
                "agent-b": {"status": "errored", "message": "Agent errored: high demand.\nThis agent's turn failed."}}),
            collab("wait", ["agent-a"], None, {"agent-a": {"status": "completed", "message": "Lemma 2 holds."}}),
            collab("closeAgent", ["agent-c"]),
            collab("wait", ["agent-q"], None, {"agent-q": {"status": "completed"}}),
        ]
        double = Coordinator([[*items, *goal_test.success(POINTER)]])
        next(self.session(double, coordinator=False))
        records = [(event["label"], event["subagentId"], event["subagentTask"], event["subagentState"],
                    event.get("subagentsRunning"), event["text"])
                   for event in double.events if event.get("authorSubagent")]
        task = long.strip()[:runtime.AUTHOR_SUBAGENT_TASK_CHARS - 1].rstrip() + "…"
        self.assertEqual(records, [
            ("Author subagent started", "agent-a", "Prove Lemma 2 by induction on n.", "started", 1,
             "Prove Lemma 2 by induction on n."),
            ("Author subagent started", "agent-b", task, "started", 2, task),
            ("Author subagent started", "agent-c", "", "started", 3, "The subagent's assignment was not reported."),
            ("Author subagent finished", "agent-a", "Prove Lemma 2 by induction on n.", "finished", 2,
             "Prove Lemma 2 by induction on n."),
            ("Author subagent failed", "agent-b", task, "failed", 1, "Agent errored: high demand."),
            ("Author subagent finished", "agent-c", "", "finished", 0, "The subagent's assignment was not reported."),
            # One an earlier attempt started: recorded, without a count.
            ("Author subagent finished", "agent-q", "", "finished", None,
             "The subagent's assignment was not reported."),
        ])
        self.assertTrue(all(event["kind"] == "status" and event["authorSubagent"] is True
                            for event in double.events if event.get("authorSubagent")))
        self.assertEqual(len(task), runtime.AUTHOR_SUBAGENT_TASK_CHARS)

    def test_subagents_still_running_when_the_attempt_ends_are_recorded_as_stopped(self):
        # Codex's subagents run in its app-server, which every attempt stops when it ends.
        pause = self.directory / "pause-request.json"
        for ending, reason in (("failure", "It stopped when Codex was restarted after a model-call failure."),
                               ("pause", "It stopped when the author paused.")):
            with self.subTest(ending=ending):
                pause.unlink(missing_ok=True)
                double = Coordinator([[collab("spawnAgent", ["agent-a"], "Prove Lemma 2."),
                                       collab("spawnAgent", ["agent-b"], "Work out examples.")]])

                def turn(rpc):
                    if ending == "failure":
                        raise runtime.Error("stream disconnected before completion")
                    pause.write_text("{}")
                    self.assertTrue(double.interrupted.wait(5))
                    rpc.messages.extend([goal_test.goal("paused"), goal_test.completed("interrupted")])

                double.on_empty = turn
                with self.assertRaises(runtime.WorkflowPaused) as paused:
                    next(self.session(double, goal_pause_file=str(pause)))
                self.assertEqual(bool(paused.exception.retry), ending == "failure")
                self.assertEqual([(event["subagentState"], event["subagentId"], event["subagentsRunning"], event["text"])
                                  for event in double.events if event.get("authorSubagent")], [
                    ("started", "agent-a", 1, "Prove Lemma 2."), ("started", "agent-b", 2, "Work out examples."),
                    ("failed", "agent-a", 1, reason), ("failed", "agent-b", 0, reason)])

    def saved_session(self, *records):
        """The root thread's saved session, in Codex's sessions folder, holding ``records`` already."""

        folder = self.directory / "codex-home" / "sessions" / time.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        saved = folder / "rollout-2026-11-12T10-00-00-thread-1.jsonl"
        saved.write_text("".join(json.dumps(record) + "\n" for record in records))
        return saved

    @staticmethod
    def subagent_rows(double):
        return [(event["subagentState"], event["subagentId"], event["subagentTask"], event["subagentsRunning"],
                 event["text"]) for event in double.events if event.get("authorSubagent")]

    def test_the_saved_session_shows_subagents_of_multi_agent_version_2(self):
        # Records of Codex 0.160's saved session (tests/test_critic_subagents.py): a subagent whose model call
        # failed, and a second one that reported. One a reopened thread's earlier attempt started stays its own.
        failed, retried = critic_test.V2_FAILED["rollout"], critic_test.V2_RETRIED["rollout"]
        earlier = json.loads(json.dumps(failed[1]))
        earlier["payload"]["item"].update(agent_thread_id="earlier-thread", agent_path="/root/earlier")
        saved = self.saved_session(earlier)
        double = SavedSession([[]], saved)
        first, second = failed[1]["payload"]["item"]["agent_thread_id"], retried[0]["payload"]["item"]["agent_thread_id"]

        def turn(rpc):
            with saved.open("a") as stream:
                stream.write(json.dumps(failed[1]) + "\n")
            # Read while the turn runs.
            deadline = time.monotonic() + 5
            while not self.subagent_rows(double) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.subagent_rows(double)), 1)
            with saved.open("a") as stream:
                stream.write("".join(json.dumps(record) + "\n" for record in [failed[2], *retried]))
            rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = turn
        with mock.patch.object(runtime, "ROLLOUT_POLL_SECONDS", 0.02):
            session = self.session(double)
            self.assertEqual(next(session)["output"], POINTER)
            session.close()
        problem = "We’re currently experiencing high demand, which may cause temporary errors."
        self.assertEqual(self.subagent_rows(double), [
            ("started", first, "/root/checker", 1, "/root/checker"), ("failed", first, "/root/checker", 0, problem),
            ("started", second, "/root/checker", 1, "/root/checker"),
            ("finished", second, "/root/checker", 0, "/root/checker")])

    def test_a_subagent_both_the_stream_and_the_saved_session_report_is_recorded_once(self):
        retried = critic_test.V2_RETRIED["rollout"]
        thread = retried[0]["payload"]["item"]["agent_thread_id"]
        saved = self.saved_session()
        double = SavedSession([[collab("spawnAgent", [thread], "Check Lemma 3 line by line."),
                                collab("wait", [thread], None, {thread: {"status": "completed"}}),
                                *goal_test.success(POINTER)]], saved)
        with saved.open("a") as stream:
            stream.write("".join(json.dumps(record) + "\n" for record in retried))
        # The saved session is read when the attempt ends, after the stream reported the same run.
        with mock.patch.object(runtime, "ROLLOUT_POLL_SECONDS", 3600):
            session = self.session(double)
            next(session)
            session.close()
        self.assertEqual(self.subagent_rows(double), [
            ("started", thread, "Check Lemma 3 line by line.", 1, "Check Lemma 3 line by line."),
            ("finished", thread, "Check Lemma 3 line by line.", 0, "Check Lemma 3 line by line.")])
        # Without a named saved session (an older Codex) only the stream is read.
        double = Coordinator([goal_test.success(POINTER)])
        with mock.patch.object(runtime, "CodexRollout", side_effect=AssertionError("read")):
            session = self.session(double)
            next(session)
            session.close()

    def test_the_coordinator_gets_its_hint_and_long_waits_and_the_critic_keeps_its_own(self):
        hint = COORDINATOR_HINT.format(count=3, s="s")
        self.assertEqual(runtime.subagent_limits(["multi_agent"], 3, coordinator="gpt-6.1-sol"), {
            "features": {"multi_agent_v2": {"max_concurrent_threads_per_session": 4, "multi_agent_mode_hint_text": hint,
                                            "default_wait_timeout_ms": 1_800_000}},
            "agents": {"max_concurrent_threads_per_session": 3, "max_depth": 1}})
        # Models of multi-agent version 1 (API models) have no default-wait setting.
        with mock.patch.object(runtime, "multi_agent_version", return_value=1):
            limits = runtime.subagent_limits(["multi_agent"], 1, coordinator="api:model")
        self.assertEqual(limits["features"]["multi_agent_v2"], {
            "max_concurrent_threads_per_session": 2,
            "multi_agent_mode_hint_text": COORDINATOR_HINT.format(count=1, s="")})
        # The critic, and an author that works alone, keep the shared hint.
        self.assertEqual(runtime.author_limits(["multi_agent"], {**SETTINGS, "coordinator": False}),
                         runtime.subagent_limits(["multi_agent"], 3))
        self.assertIn("is allowed for this task", runtime.subagent_limits(["multi_agent"], 3)["features"][
            "multi_agent_v2"]["multi_agent_mode_hint_text"])
        # The coordinator's app-server and thread have them.
        double = Coordinator([goal_test.success(POINTER)])
        next(self.session(double))
        argv = self.spawn.call_args.args[0]
        flags = [argv[index + 1] for index in range(len(argv) - 1) if argv[index] == "-c"]
        self.assertIn("features.multi_agent_v2.multi_agent_mode_hint_text=" + json.dumps(hint), flags)
        self.assertIn("features.multi_agent_v2.default_wait_timeout_ms=1800000", flags)
        started = next(params for method, params in double.rpc.calls if method == "thread/start")
        self.assertEqual(started["config"]["features"]["multi_agent_v2"]["default_wait_timeout_ms"], 1_800_000)
        double = Coordinator([goal_test.success(POINTER)])
        next(self.session(double, coordinator=False))
        argv = self.spawn.call_args.args[0]
        self.assertFalse([value for value in argv if "default_wait_timeout_ms" in value or "coordinator" in value])

    def test_a_web_cutoff_coordinator_keeps_its_hint_and_codex_s_default_wait(self):
        # Its audit compares the agent settings exactly, and whether Codex reports the default wait in its effective
        # settings is not verified: only the hint asks for long waits.
        hint = COORDINATOR_HINT.format(count=3, s="s")
        expected = {"features": {"multi_agent_v2": {"max_concurrent_threads_per_session": 4,
                                                    "multi_agent_mode_hint_text": hint}},
                    "agents": {"max_concurrent_threads_per_session": 3, "max_depth": 1}}
        self.assertEqual(runtime.author_limits(["multi_agent"], {**SETTINGS, "coordinator": True, "web_cutoff": True}),
                         expected)
        checked = []

        def preflight(*args, **kwargs):
            checked.append(kwargs.get("agent_limits"))
            return {"CODEX_HOME": str(self.directory / "codex-home")}

        def verified(rpc, policy, role, environment, allow_runtime_overrides=False, agent_limits=None):
            checked.append(agent_limits)

        locked = {"approval_policy": "never", "default_permissions": retrieval_runtime.PROFILE,
                  "web_search": "disabled", "features": {"multi_agent": True, "shell_tool": True}}
        double = Coordinator([goal_test.success(POINTER)])
        with mock.patch.object(runtime, "retrieval_policy", return_value={"isolation": {"workspace": "/run"},
                                                                          "runId": "P01"}), \
                mock.patch.object(retrieval_runtime, "codex_executable", return_value="/pinned/codex"), \
                mock.patch.object(retrieval_runtime, "preflight", side_effect=preflight), \
                mock.patch.object(retrieval_runtime, "configuration", return_value=locked), \
                mock.patch.object(retrieval_runtime, "verify_rpc", side_effect=verified):
            self.assertEqual(next(self.session(double))["output"], POINTER)
        argv = self.spawn.call_args.args[0]
        flags = [argv[index + 1] for index in range(len(argv) - 1) if argv[index] == "-c"]
        self.assertIn("features.multi_agent_v2.multi_agent_mode_hint_text=" + json.dumps(hint), flags)
        self.assertFalse([flag for flag in flags if "default_wait_timeout_ms" in flag])
        started = next(params for method, params in double.rpc.calls if method == "thread/start")
        self.assertEqual(started["config"]["features"]["multi_agent_v2"], expected["features"]["multi_agent_v2"])
        # The audit checks exactly these settings, at the start and before every turn.
        self.assertGreaterEqual(len(checked), 3)
        self.assertTrue(all(limits == expected for limits in checked))

    def test_after_a_reply_turn_an_author_with_codex_s_own_subagents_still_has_them(self):
        # A reply turn of the paused author (a reply, a progress summary, the time limit's summary) sends no agents
        # setting: Codex keeps agents.enabled = false with the thread for good (keeps_codex_subagent_tools()), so
        # one would leave a coordinator of a job from before TCS Prover's team or of a web-cutoff run, and an author
        # of --author-mode author with subagents, without Codex's subagents for the rest of the job. An author alone
        # has them off from its own start.
        locked = {"approval_policy": "never", "default_permissions": retrieval_runtime.PROFILE,
                  "web_search": "disabled", "features": {"multi_agent": True, "shell_tool": True}}

        def web_cutoff(stack):
            for target, name, value in (
                    (runtime, "retrieval_policy", mock.Mock(return_value={"isolation": {"workspace": "/run"},
                                                                          "runId": "P01"})),
                    (retrieval_runtime, "codex_executable", mock.Mock(return_value="/pinned/codex")),
                    (retrieval_runtime, "preflight", mock.Mock(side_effect=lambda *args, **kwargs: {
                        "CODEX_HOME": str(self.directory / "codex-home")})),
                    (retrieval_runtime, "configuration", mock.Mock(side_effect=lambda policy, role: json.loads(
                        json.dumps(locked)))),
                    (retrieval_runtime, "verify_rpc", mock.Mock())):
                stack.enter_context(mock.patch.object(target, name, value))

        reply_settings = {key: SETTINGS[key] for key in ("model", "effort", "speed", "summary")}
        for policy, coordinator, features, subagents in (
                (False, True, ["multi_agent"], 3), (True, True, ["multi_agent"], 3),
                (False, False, ["multi_agent"], 3), (False, False, [], 0)):
            with self.subTest(web_cutoff=policy, coordinator=coordinator, subagents=subagents), \
                    contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"TCS_PROVER_RETRIEVAL_MODE": "live"}))
                os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
                if policy:
                    web_cutoff(stack)
                doubles = []
                for step in ("start", "reply", "resume"):
                    if step == "reply":
                        double = conversation_test.Codex([conversation_test.reply()])
                        runtime.author_conversation(
                            double, "Summarize your progress.", settings=reply_settings,
                            options={"goal_cwd": str(self.directory), "goal_thread_id": "thread-1"}, features=[],
                            conversation_id=conversation_test.CONVERSATION_ID, label="Progress summary requested")
                    else:
                        double = Coordinator([goal_test.success(POINTER)])
                        session = runtime.goal_session(
                            double, "Coordinate the team on S.", prompts=PROMPTS,
                            settings={**SETTINGS, "subagents": subagents}, features=features, coordinator=coordinator,
                            options={"goal_cwd": str(self.directory), **({} if step == "start" else {
                                "goal_thread_id": "thread-1", "goal_resume": True, "goal_require_resume": True})})
                        self.addCleanup(session.close)
                        self.assertEqual(next(session)["output"], POINTER)
                    doubles.append(double)
                launches = [(method, params) for double in doubles for method, params in double.rpc.calls
                            if method in {"thread/start", "thread/resume"}]
                self.assertEqual([method for method, _ in launches], ["thread/start", "thread/resume", "thread/resume"])
                launches = [params for _, params in launches]
                if features:
                    # No launch of its thread took them, and it is resumed with them, as at its start.
                    self.assertTrue(keeps_codex_subagent_tools(launches))
                    self.assertTrue(launches[2]["config"]["features"]["multi_agent"])
                    self.assertEqual(launches[2]["config"]["agents"],
                                     {"max_concurrent_threads_per_session": 3, "max_depth": 1})
                else:
                    # Its own start took them.
                    self.assertEqual(launches[0]["config"]["agents"], {"enabled": False})
                    self.assertFalse(keeps_codex_subagent_tools(launches[:1]))
                # The reply turn leaves the thread's own setting.
                self.assertNotIn("agents", launches[1]["config"])

    def test_a_root_that_waits_silently_for_its_subagents_goes_on_while_codex_answers(self):
        # Scaled down: 0.2 s without an event starts a check, which must be answered within 0.5 s.
        double = Coordinator([[]], answers_probes=True)
        double.AUTHOR_INACTIVITY_SECONDS, double.AUTHOR_PROBE_SECONDS = 0.2, 0.5
        probes = []

        def waiting(rpc):
            # Silent for longer than the check's own limit, three checks in a row.
            if len(probes) < 3:
                self.assertTrue(double.probed.wait(5))
                double.probed.clear()
                probes.append(True)
                return
            rpc.messages.extend(goal_test.success(POINTER))

        double.on_empty = waiting
        self.assertEqual(next(self.session(double))["output"], POINTER)
        self.assertEqual(double.stopped, [])
        quiet = [event for event in double.events if event["kind"] == "diagnostic"]
        self.assertEqual(len(quiet), 3)
        self.assertTrue(all(event["text"].startswith("No author event for") for event in quiet))
        # Without an answer the same silence ends the attempt, as a failed model call.
        double = Coordinator([[]])
        double.AUTHOR_INACTIVITY_SECONDS, double.AUTHOR_PROBE_SECONDS = 0.2, 0.5
        stopped = threading.Event()
        double.stop_process = lambda process: (double.stopped.append(process), stopped.set())

        def silent(rpc):
            self.assertTrue(stopped.wait(5))
            raise goal_test.TransportError("Codex app-server stopped unexpectedly.")

        double.on_empty = silent
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.session(double))
        self.assertIn("Codex stopped responding", paused.exception.reason)


def runs(items, tracker=None):
    """The (state, id, task, count, text) of the records AuthorSubagents makes of root collab ``items``."""

    events = []
    tracker = tracker or runtime.AuthorSubagents(lambda kind, **fields: events.append(fields))
    for item in items:
        tracker.codex_item(item["params"]["item"])
    return [(event["subagentState"], event["subagentId"], event["subagentTask"], event.get("subagentsRunning"),
             event["text"]) for event in events]


class SubagentRunTests(unittest.TestCase):
    """AuthorSubagents: a subagent given new work after it ended runs again, recorded as a run of its own."""

    FIRST, SECOND = "Prove Lemma 2.", "Now check the case n = 2 of Lemma 2."

    def test_new_work_for_a_subagent_that_ended_is_a_run_of_its_own(self):
        spawned = [collab("spawnAgent", ["agent-a"], self.FIRST),
                   collab("wait", ["agent-a"], None, {"agent-a": {"status": "completed", "message": "Done."}})]
        failed = collab("wait", ["agent-a"], None, {
            "agent-a": {"status": "errored", "message": "stream disconnected before completion"}})
        expected = [("started", "agent-a", self.FIRST, 1, self.FIRST), ("finished", "agent-a", self.FIRST, 0, self.FIRST),
                    ("started", "agent-a", self.SECOND, 1, self.SECOND),
                    ("failed", "agent-a", self.SECOND, 0, "connection lost")]
        for name, given in (
                # Multi-agent version 1: send_input to the finished subagent (its state in that item may still
                # say completed: no end).
                ("send_input", [collab("sendInput", ["agent-a"], self.SECOND,
                                       {"agent-a": {"status": "completed", "message": "Done."}})]),
                # Version 1: closed, reopened (which gives it no work yet), then given work.
                ("close, resume_agent, send_input", [
                    collab("closeAgent", ["agent-a"]),
                    collab("resumeAgent", ["agent-a"], None, {"agent-a": {"status": "pendingInit"}}),
                    collab("sendInput", ["agent-a"], self.SECOND, {"agent-a": {"status": "running"}})]),
                # Version 2: followup_task.
                ("followup_task", [collab("followupTask", ["agent-a"], self.SECOND)])):
            with self.subTest(given=name):
                self.assertEqual(runs([*spawned, *given, failed]), expected)
        # Later waits that repeat a finished state are no new run.
        self.assertEqual(runs([*spawned, spawned[1]]), expected[:2])
        # A running state of a subagent that ended: it runs again (its assignment unknown).
        self.assertEqual(runs([*spawned, collab("wait", ["agent-b"], None, {"agent-a": {"status": "running"}})])[2],
                         ("started", "agent-a", "", 1, "The subagent's assignment was not reported."))
        # New work for a subagent that still runs is no new run, and its state in that item no end.
        self.assertEqual(runs([spawned[0], collab("sendInput", ["agent-a"], self.SECOND, {
            "agent-a": {"status": "interrupted"}}), failed]), [expected[0], ("failed", "agent-a", self.FIRST, 0,
                                                                            "connection lost")])

    def test_two_sources_record_each_run_once(self):
        events = []
        tracker = runtime.AuthorSubagents(lambda kind, **fields: events.append(fields))
        # The saved session (read every few seconds) reports a start and an end the stream reported already,
        # and lags behind a second run that the stream reports first.
        runs([collab("spawnAgent", ["agent-a"], self.FIRST),
              collab("wait", ["agent-a"], None, {"agent-a": {"status": "completed"}}),
              collab("sendInput", ["agent-a"], self.SECOND)], tracker)
        tracker.started("agent-a", "/root/lemma2", source="session")
        tracker.finished("agent-a", source="session")  # The end of the first run.
        tracker.finished("agent-a", source="session")  # Its other record of the same end.
        tracker.started("agent-b", "/root/examples", source="session")  # Only the saved session shows this one.
        runs([collab("wait", ["agent-a"], None, {"agent-a": {"status": "completed"}})], tracker)
        tracker.finished("agent-b", "Agent errored: high demand.", source="session")
        tracker.finished("agent-b", source="session")
        self.assertEqual([(event["subagentState"], event["subagentId"], event["subagentTask"],
                           event["subagentsRunning"]) for event in events], [
            ("started", "agent-a", self.FIRST, 1), ("finished", "agent-a", self.FIRST, 0),
            ("started", "agent-a", self.SECOND, 1), ("started", "agent-b", "/root/examples", 2),
            ("finished", "agent-a", self.SECOND, 1), ("failed", "agent-b", "/root/examples", 0)])
        # A source that saw a subagent end without seeing it start (an earlier attempt started it) and repeats
        # that end never ends a run the other source saw start since.
        events.clear()
        tracker = runtime.AuthorSubagents(lambda kind, **fields: events.append(fields))
        ended = collab("wait", ["agent-q"], None, {"agent-q": {"status": "completed"}})
        runs([ended], tracker)
        tracker.started("agent-q", "/root/q", source="session")
        runs([ended], tracker)
        self.assertEqual([(event["subagentState"], event["subagentId"]) for event in events],
                         [("finished", "agent-q"), ("started", "agent-q")])

    def test_the_subagents_still_running_when_the_process_stops_are_recorded_as_stopped(self):
        events = []
        tracker = runtime.AuthorSubagents(lambda kind, **fields: events.append(fields))
        runs([collab("spawnAgent", ["agent-a"], self.FIRST), collab("spawnAgent", ["agent-b"], self.SECOND),
              collab("wait", ["agent-a"], None, {"agent-a": {"status": "completed"}})], tracker)
        tracker.stopped("It stopped when Codex was restarted after a model-call failure.")
        tracker.stopped("Again.")  # None is left running.
        self.assertEqual([(event["subagentState"], event["subagentId"], event["subagentsRunning"], event["text"])
                          for event in events][2:], [
            ("finished", "agent-a", 1, self.FIRST),
            ("failed", "agent-b", 0, "It stopped when Codex was restarted after a model-call failure.")])


SUBAGENT_VARIABLES = ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_FORK_SUBAGENT",
                      "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH")


class ClaudeCoordinatorTests(claude_test.FakeClaudeCase):
    PROMPTS = {**claude_test.PROMPTS, "continuation": "Keep coordinating the team.",
               "checkin": COORDINATOR_PROMPTS["coordinator_checkin"]}

    def coordinator(self, model="claude-opus-5-5", features=("multi_agent",), coordinator=True, alone=None,
                    subagents=3, **options):
        session = claude_cli.goal_session(
            runtime, "Coordinate the team on S.", prompts=self.PROMPTS, features=list(features),
            settings={"model": model, "effort": "max", "speed": "standard", "summary": "concise",
                      **({"subagents": subagents} if subagents else {})},
            options={"goal_cwd": str(self.run_dir), "goal_pause_file": str(self.run_dir / "pause-request.json"),
                     "author_steer_file": str(self.run_dir / "author-steer.json"), **options},
            coordinator=coordinator, **({"alone": alone} if alone else {}))
        self.addCleanup(session.close)
        return session

    def subagent_variables(self, start):
        return {name: start["env"].get(name) for name in SUBAGENT_VARIABLES}

    def test_with_the_subagent_tool_its_subagents_run_in_the_foreground_bounded_and_never_as_forks(self):
        self.script([["init", "echo", claude_test.complete()], ["init", "echo", claude_test.complete()]])
        # Inherited values never reach the CLI: TCS Prover's own settings do.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "20",
                                          "CLAUDE_CODE_FORK_SUBAGENT": "1"}):
            self.assertEqual(next(self.coordinator(coordinator=False))["outcome"], "done")
            start = self.records("start")[-1]
            self.assertEqual(self.subagent_variables(start), dict(zip(SUBAGENT_VARIABLES, ("1", "0", "3", "1"))))
            self.assertEqual(start["settings"]["permissions"]["deny"][-1], "Agent(fork)")
            # Without the subagent tool, none of them.
            self.assertEqual(next(self.coordinator(coordinator=False, features=()))["outcome"], "done")
            start = self.records("start")[-1]
        self.assertEqual(self.subagent_variables(start), dict.fromkeys(SUBAGENT_VARIABLES))
        self.assertNotIn("Agent(fork)", start["settings"]["permissions"]["deny"])

    def test_the_critic_gets_the_same_settings_for_its_independent_critics(self):
        answer = {"statement": "S.", "notes": "Fine."}
        self.script([["init", {"structured": answer}], ["init", {"structured": answer}]])
        runtime.structured("Check S.", claude_test.SCHEMA, "critic", model="claude-opus-5-5",
                           features=("multi_agent",), subagents=2)
        start = self.records("start")[-1]
        self.assertEqual(self.subagent_variables(start), dict(zip(SUBAGENT_VARIABLES, ("1", "0", "2", "1"))))
        self.assertEqual(start["settings"]["permissions"], {"deny": ["Agent(fork)"]})
        # A call without subagents (the reviewer, the writer) has none of them.
        runtime.structured("Review S.", claude_test.SCHEMA, "review", model="claude-opus-5-5")
        start = self.records("start")[-1]
        self.assertEqual(self.subagent_variables(start), dict.fromkeys(SUBAGENT_VARIABLES))
        self.assertNotIn("permissions", start.get("settings") or {})

    def test_a_coordinators_reply_that_names_no_proof_file_is_a_report_and_it_continues(self):
        self.script([["init", "echo", claude_test.complete("Status: Lemma 1 verified; Lemma 2 open.")],
                     ["init", "echo", claude_test.complete(POINTER)]])
        self.assertEqual(next(self.coordinator()), {"outcome": "done", "output": POINTER})
        # Its start says that it coordinates (follow_author_mode()).
        started, = [event for event in self.events if event.get("label") == "Goal started"]
        self.assertEqual(started["authorModeEffective"], "coordinator")
        self.assertEqual(self.texts(), ["Coordinate the team on S.", "Keep coordinating the team."])
        report, = [event for event in self.events if event.get("label") == "Coordinator report"]
        self.assertEqual(report["text"], "Status: Lemma 1 verified; Lemma 2 open.")
        # The whole reply stays in the activity log as the coordinator's message; only the pointer was handed on.
        messages = [event["event"]["params"]["item"]["text"] for event in self.events if event["kind"] == "codex_event"
                    and event["event"].get("method") == "item/completed"
                    and event["event"]["params"]["item"]["type"] == "agentMessage"]
        self.assertEqual(messages, ["Status: Lemma 1 verified; Lemma 2 open.", POINTER])
        self.assertEqual([event["text"] for event in self.events if event["kind"] == "author_result"], [POINTER])


    def test_a_binding_with_report_answers_a_claude_coordinators_report_with_it(self):
        self.script([["init", "echo", claude_test.complete("Status: Lemma 2 open.")],
                     ["init", "echo", claude_test.complete(POINTER)]])
        report = COORDINATOR_PROMPTS["coordinator_report"]
        with mock.patch.dict(self.PROMPTS, {"report": report}):
            self.assertEqual(next(self.coordinator()), {"outcome": "done", "output": POINTER})
        self.assertEqual(self.texts(), ["Coordinate the team on S.", report])

    def test_a_time_check_is_written_into_the_running_turn(self):
        self.script([["init", "echo", {"consume": 10}, claude_test.complete(POINTER)]])
        # Scaled down: a check every 0.3 s of working time.
        with mock.patch.object(runtime, "COORDINATOR_CHECKIN_SECONDS", 0.3):
            self.assertEqual(next(self.coordinator())["output"], POINTER)
        texts = self.texts()
        self.assertEqual(texts[0], "Coordinate the team on S.")
        self.assertEqual(texts[1], "[TIME CHECK FROM TCS PROVER]\nElapsed: 0 min. No time limit.")
        check = next(event for event in self.events if event.get("label") == "Coordinator time check")
        self.assertEqual((check["kind"], check["elapsedSeconds"], check["text"]), ("status", 0, texts[1]))

    def checks(self):
        """The time checks the CLI read, and the ones recorded as sent, as (elapsed as stated, text) pairs."""

        def stated(text):
            return re.search(r"Elapsed: ([^.]*)\.", text).group(1)

        read = [text for text in self.texts() if text.startswith("[TIME CHECK FROM TCS PROVER]")]
        recorded = [event["text"] for event in self.events if event.get("label") == "Coordinator time check"]
        return [stated(text) for text in read], [stated(text) for text in recorded]

    def test_one_time_check_at_a_time_while_subagents_run_in_the_foreground(self):
        # Scaled: a second is an hour of working time. Claude starts a subagent in the foreground and takes no
        # input for 3.5 s (until 3 h 30 min), then takes in what was written, one step after the other.
        agent = {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {"content": [
            {"type": "tool_use", "id": "task-1", "name": "Agent", "input": {"prompt": "Prove Lemma 2."}}]}}}
        report = {"emit": {"type": "user", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "task-1", "content": "Lemma 2 holds."}]}}}
        self.script([["init", "echo", agent, {"sleep": 3.5}, report, {"consume": 0.5}, {"consume": 0.5},
                      claude_test.complete(POINTER)]], default_turn=[{"exit": 3}])
        began = time.monotonic()
        with mock.patch.object(runtime, "workflow_elapsed", side_effect=lambda options: (time.monotonic() - began) * HOUR), \
                mock.patch.object(claude_cli, "QUEUED_INPUT_SECONDS", 0.3):
            self.assertEqual(next(self.coordinator())["output"], POINTER)
        read, recorded = self.checks()
        # The 1 h check waited to be read; the 2 h and 3 h ones were never written: the next went once the first
        # was taken in, with the time of its writing. Each is recorded when Claude takes it in.
        self.assertEqual(len(read), 2, read)
        self.assertTrue(read[0].startswith("1 h"), read)
        self.assertTrue(read[1].startswith("3 h"), read)
        self.assertEqual(recorded, read)

    def test_a_time_check_given_up_on_is_sent_again_in_the_next_turn(self):
        # Claude takes no input before its turn ends: after QUEUED_INPUT_SECONDS (scaled down) the check is given up
        # on, and the next turn gets it again. This CLI reads the first copy but never takes it in (no turn starts
        # for it), then takes in the continuation and the second copy.
        self.script([["init", "echo", {"sleep": 0.6}, claude_test.in_progress()],
                     [{"consume": 3}, {"consume": 3}, claude_test.complete(POINTER)]],
                    default_turn=[{"exit": 3}])
        began = time.monotonic()
        # 50 min until 0.2 s of real time, then 1 h 1 min: one check comes due, in the first turn.
        clock = lambda options: 50 * 60 if time.monotonic() - began < 0.2 else HOUR + 60  # noqa: E731
        with mock.patch.object(runtime, "workflow_elapsed", side_effect=clock), \
                mock.patch.object(claude_cli, "QUEUED_INPUT_SECONDS", 0.3):
            self.assertEqual(next(self.coordinator())["output"], POINTER)
        text = runtime.checkin_text(COORDINATOR_PROMPTS["coordinator_checkin"], HOUR + 60, 0, 0)
        self.assertEqual(self.texts(), ["Coordinate the team on S.", text, "Keep coordinating the team.", text])
        self.assertIn("Queued author input was not picked up; a live instruction is offered again later and a "
                      "re-anchor leads the next turn.", [event["text"] for event in self.events
                                                         if event["kind"] == "diagnostic"])
        # Only the copy Claude took in is recorded.
        self.assertEqual(self.checks()[1], ["1 h 1 min"])

    def test_a_runner_that_starts_at_the_critic_sends_the_check_that_came_due_during_the_review(self):
        # The runner started at 5 h 50 min of working time at the critic; its author begins at 6 h 30 min.
        self.script([["init", "echo", {"consume": 10}, claude_test.complete(POINTER)]])
        with mock.patch.object(runtime, "workflow_elapsed", return_value=6 * HOUR + 30 * 60):
            self.assertEqual(next(self.coordinator(elapsed_seconds=5 * HOUR + 50 * 60))["output"], POINTER)
        self.assertTrue(self.texts()[1].startswith("[TIME CHECK FROM TCS PROVER]\nElapsed: 6 h 30 min."))

    def test_a_failing_time_check_ends_the_checks_with_a_diagnostic(self):
        self.script([["init", "echo", {"sleep": 1.0}, claude_test.complete(POINTER)]])
        with mock.patch.object(runtime, "COORDINATOR_CHECKIN_SECONDS", 0.3), \
                mock.patch.object(runtime, "checkin_text", side_effect=RuntimeError("boom")):
            self.assertEqual(next(self.coordinator())["output"], POINTER)
        self.assertEqual(self.texts(), ["Coordinate the team on S."])
        self.assertIn("The coordinator's time checks stopped: boom",
                      [event["text"] for event in self.events if event["kind"] == "diagnostic"])

    def test_the_author_subagents_are_recorded_from_its_agent_calls(self):
        partial = "API Error: 529 overloaded\n" + claude_cli.PARTIAL_SUBAGENT_NOTE + "\nPartial work."
        self.script([["init", "echo",
                      {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {"content": [
                          {"type": "tool_use", "id": "task-1", "name": "Agent",
                           "input": {"description": "Lemma 2", "prompt": "Prove Lemma 2.\nUse memory/notes.md."}},
                          {"type": "tool_use", "id": "task-2", "name": "Task", "input": {"description": "Examples"}},
                          {"type": "tool_use", "id": "task-3", "name": "Task", "input": {"prompt": "One more."}}]}}},
                      # A subagent's own subagent call is not the author's.
                      {"emit": {"type": "assistant", "parent_tool_use_id": "task-1", "uuid": "a2", "message": {
                          "content": [{"type": "tool_use", "id": "nested", "name": "Task",
                                       "input": {"prompt": "Nested."}}]}}},
                      {"emit": {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                          {"type": "tool_result", "tool_use_id": "task-1", "content": "Lemma 2 holds."},
                          {"type": "tool_result", "tool_use_id": "task-2",
                           "content": [{"type": "text", "text": partial}]},
                          {"type": "tool_result", "tool_use_id": "task-3", "is_error": True,
                           "content": "Concurrent subagent limit reached (3). Do not retry."}]}}},
                      claude_test.complete(POINTER)]])
        next(self.coordinator(coordinator=False))
        records = [(event["label"], event["subagentId"], event["subagentTask"], event.get("subagentsRunning"),
                    event["text"]) for event in self.events if event.get("authorSubagent")]
        self.assertEqual(records, [
            ("Author subagent started", "task-1", "Prove Lemma 2.", 1, "Prove Lemma 2."),
            ("Author subagent started", "task-2", "Examples", 2, "Examples"),
            ("Author subagent started", "task-3", "One more.", 3, "One more."),
            ("Author subagent finished", "task-1", "Prove Lemma 2.", 2, "Prove Lemma 2."),
            ("Author subagent failed", "task-2", "Examples", 1, "model at capacity"),
            ("Author subagent failed", "task-3", "One more.", 0, "Concurrent subagent limit reached (3). Do not retry."),
        ])

    def test_subagents_still_running_when_the_attempt_ends_are_recorded_as_stopped(self):
        self.script([["init", "echo",
                      {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": "a1", "message": {"content": [
                          {"type": "tool_use", "id": "task-1", "name": "Agent",
                           "input": {"description": "Lemma 2", "prompt": "Prove Lemma 2."}}]}}},
                      {"stderr": "fatal: crashed"}, {"exit": 3}]])
        with self.assertRaises(runtime.WorkflowPaused) as paused:
            next(self.coordinator(coordinator=False))
        self.assertTrue(paused.exception.retry)
        self.assertEqual([(event["subagentState"], event["subagentId"], event.get("subagentsRunning"), event["text"])
                          for event in self.events if event.get("authorSubagent")], [
            ("started", "task-1", 1, "Prove Lemma 2."),
            ("failed", "task-1", 0, "It stopped when Claude Code was restarted after a model-call failure.")])

    def test_without_the_subagent_tool_the_coordinator_works_alone_and_says_so_once(self):
        model = self.api_model()
        alone = {"prompt": "Prove S alone.", "prompts": dict(claude_test.PROMPTS), "recovery": ""}
        # Any further turn would mean that a reply was not handed on: the CLI then ends, and the test fails at once.
        self.script([["init", "echo", claude_test.complete("The complete inline proof.")],
                     ["init", "echo", claude_test.complete("The complete inline proof.")]],
                    drops_config_dir=True, default_turn=[{"exit": 3}])
        for _ in range(2):  # A second attempt (a retry reuses the launch) says nothing more.
            self.assertEqual(next(self.coordinator(model=model, alone=alone))["output"], "The complete inline proof.")
        start = self.records("start")[-1]
        self.assertIn("--bare", start["argv"])
        self.assertEqual(self.subagent_variables(start), dict.fromkeys(SUBAGENT_VARIABLES))
        # The author's own prompts, and no hand-in guard or time checks.
        self.assertEqual(self.texts(), ["Prove S alone.", "Prove S alone."])
        self.assertEqual(start["harness"], claude_cli.GOAL_HARNESS_PROMPT.replace("[OBJECTIVE]",
                                                                                  claude_test.PROMPTS["goal"]))
        self.assertFalse([event for event in self.events if event.get("label") == "Coordinator report"])
        note, = [event for event in self.events if event.get("label") == "No subagents with this model"]
        self.assertEqual(note["text"], f"Claude Code runs {claude_cli.providers.model_label(model)} in --bare mode "
                                       "here, which offers no subagent tool, so the author works alone instead of "
                                       "coordinating up to 3 author subagents.")
        self.assertEqual((note["authorModeEffective"], note["modelLabel"]),
                         ("author", claude_cli.providers.model_label(model)))
        self.assertTrue(all("authorModeEffective" not in event for event in self.events
                            if event.get("label") in {"Goal started", "Goal resumed"}))


class ConversationModeTests(unittest.TestCase):
    """The paused author's turn (--converse) takes the job's author mode: a coordinator's summary is its own."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        for target, name, value in ((runtime, "emit", mock.Mock()), (runtime, "require_model_credentials", mock.Mock()),
                                    (runtime.subprocess, "Popen", mock.Mock(side_effect=AssertionError("process")))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def turn(self, kind="summary", **options):
        turns = conversation_test.FakeTurns([], answer="Reply.")
        request = {"schemaVersion": 1, "id": conversation_test.CONVERSATION_ID, "kind": kind,
                   "text": "Why Lemma 2?" if kind == "message" else "", "notice": ""}
        with mock.patch.object(runtime, "author_conversation", side_effect=turns):
            runtime.run_author_conversation(WORKFLOW, {
                "author_model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise",
                "goal_cwd": str(self.directory), "goal_thread_id": "thread-1", **options}, request)
        call, = turns.calls
        self.assertEqual(call["features"], [])  # Read-only, without subagents, whatever the mode.
        return call["instruction"]

    def test_a_mode_the_job_cannot_have_fails_the_reply_at_once(self):
        events = []
        request = {"schemaVersion": 1, "id": conversation_test.CONVERSATION_ID, "kind": "summary", "text": "",
                   "notice": ""}
        with mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: events.append(fields)), \
                mock.patch.object(runtime, "author_conversation", side_effect=AssertionError("no turn starts")), \
                mock.patch.object(runtime, "wait_before_retry", side_effect=AssertionError("never retried")):
            runtime.run_author_conversation(WORKFLOW, {
                "author_model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise",
                "goal_cwd": str(self.directory), "goal_thread_id": "thread-1", "author_mode": "coordinator",
                "author_subagents": 0}, request)
        failed, = [event for event in events if event.get("label") == "Author reply failed"]
        self.assertEqual((failed["failure"], failed["final"]), ("configuration", True))
        self.assertIn("needs Author subagents 1 or more", failed["text"])

    def test_the_summary_follows_the_mode_and_the_reply_prompt_is_shared(self):
        self.assertEqual(self.turn(author_mode="coordinator"), COORDINATOR_PROMPTS["coordinator_summary"])
        self.assertEqual(self.turn(author_mode="author"), conversation_test.SUMMARY_TEXT)
        self.assertEqual(self.turn(author_mode="coordinator", author_model="gpt-5.6-luna"),
                         conversation_test.SUMMARY_TEXT)  # It worked alone: no subagent tool from Codex.
        for mode in ("coordinator", "author"):
            self.assertEqual(self.turn("message", author_mode=mode),
                             conversation_test.CONVERSATION_TEXT.format(message="Why Lemma 2?"))


if __name__ == "__main__":
    unittest.main()
