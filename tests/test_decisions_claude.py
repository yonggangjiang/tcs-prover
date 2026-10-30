"""Claude provider decisions I-063, I-006, I-101, I-127, I-064, I-072 and I-079, with the stand-in CLI; no model calls."""

import functools
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, providers

import test_claude_cli as claude_test
import test_model_retry as retry_test

SCHEMA = claude_test.SCHEMA
PROMPTS = claude_test.PROMPTS
complete, in_progress = claude_test.complete, claude_test.in_progress
OK = {"structured": {"statement": "S", "notes": "ok"}}


def api_retry(**fields):
    return {"emit": {"type": "system", "subtype": "api_retry", "parent_tool_use_id": None, **fields}}


class AuthorCase(claude_test.FakeClaudeCase):
    """The real Claude goal session and _goal_step; retry waits take no real time."""

    def setUp(self):
        super().setUp()
        self.clock = retry_test.FakeClock()
        retry_test.patch_clock(self, self.clock)

    def launcher(self, model="claude-opus-5-5"):
        return functools.partial(claude_cli.goal_session, runtime, "Prove S exactly.", prompts=PROMPTS,
                                 settings={"model": model, "effort": "max", "speed": "standard", "summary": "concise"},
                                 features=["multi_agent"], stages=None, node_name="author")

    def run_goal(self, options=None, model="claude-opus-5-5"):
        launch = self.launcher(model)
        options = {"goal_cwd": str(self.run_dir), "goal_pause_file": str(self.run_dir / "pause-request.json"),
                   **(options or {})}
        sessions = {"author": launch(options=options, initial_instruction="")}
        self.addCleanup(lambda: sessions["author"].close())
        return runtime._goal_step(sessions, "author", launch, "", options, next, "solve")

    def first_session(self, answer="First proof."):
        """A conversation an earlier runner completed a turn in; returns its session id."""

        self.script([["init", "echo", complete(answer)]])
        earlier = self.launcher()(options={"goal_cwd": str(self.run_dir)}, initial_instruction="")
        self.assertEqual(next(earlier)["output"], answer)
        earlier.close()
        session = self.records("start")[0]["argv"][1]
        self.log.unlink()
        self.events.clear()
        return session

    def status(self, label):
        return next(event for event in self.events if event.get("label") == label)


class PrivateConfigurationTests(AuthorCase):
    """I-063: API models run without --bare in a private CLAUDE_CONFIG_DIR, checked before any request."""

    def test_the_check_is_a_bare_start_that_makes_no_request_and_runs_once_per_folder(self):
        model = self.api_model()
        self.script([["init", OK], ["init", OK]])
        for _ in range(2):
            runtime.structured("Review.", SCHEMA, "review", model=model)
        home = claude_cli.claude_home(self.run_dir, runtime)
        probes = self.records("probe")
        self.assertEqual(len(probes), 1)
        argv = probes[0]["argv"]
        self.assertIn("--bare", argv)  # Never reads the login or the keychain.
        self.assertEqual(probes[0]["env"]["CLAUDE_CONFIG_DIR"], str(home))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", probes[0]["env"])
        self.assertFalse(probes[0]["secret_in_env"] or probes[0]["secret_in_argv"])
        # Its only input is the initialize request: no prompt, so no model request.
        self.assertEqual([row["message"]["request"]["subtype"] for row in self.records("input")], ["initialize"])
        starts = self.records("start")
        self.assertEqual([("--bare" in start["argv"], start["env"]["CLAUDE_CONFIG_DIR"]) for start in starts],
                         [(False, str(home))] * 2)
        self.assertNoSecretLeaks()
        self.assertPrivateFilesRemoved()

    def test_a_launcher_that_drops_the_folder_keeps_api_models_in_bare_mode(self):
        model = self.api_model()
        self.script([["init", OK]], drops_config_dir=True)
        with mock.patch.object(claude_cli, "sandbox_supported", return_value=True):
            runtime.structured("Review.", SCHEMA, "review", model=model, features=("shell_tool", "multi_agent"))
        start = self.records("start")[0]
        argv = start["argv"]
        self.assertIn("--bare", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Bash")
        self.assertNotIn("CLAUDE_CONFIG_DIR", start["env"])  # As before: no login folder at all.
        self.assertEqual(self.status("Claude API mode tools")["text"],
                         "The claude command on PATH does not pass CLAUDE_CONFIG_DIR on, so Anthropic-format API "
                         "models run Claude Code in --bare mode, which does not offer Task, WebFetch, WebSearch.")
        self.assertNoSecretLeaks()
        self.assertPrivateFilesRemoved()

    def test_a_check_without_an_answer_keeps_api_models_in_bare_mode(self):
        model = self.api_model()
        self.script([["init", OK]], silent_probe=True)
        with mock.patch.object(claude_cli, "CONFIG_CHECK_SECONDS", 0.5):
            runtime.structured("Review.", SCHEMA, "review", model=model)
        self.assertIn("--bare", self.records("start")[0]["argv"])
        self.assertTrue(self.status("Claude API mode tools")["text"].startswith(
            "The claude command on PATH did not confirm that it uses a private CLAUDE_CONFIG_DIR, so "))

    def test_the_api_author_gets_subagents_and_web_tools_and_keeps_its_conversation_privately(self):
        model = self.api_model()
        self.script([["init", "echo", complete()]])
        self.assertEqual(self.run_goal(model=model)["outcome"], "done")
        start = self.records("start")[0]
        tools = start["argv"][start["argv"].index("--tools") + 1].split(",")
        self.assertTrue({"Task", "TodoWrite", "WebSearch", "WebFetch", "Write"} <= set(tools))
        home = claude_cli.claude_home(self.run_dir, runtime)
        self.assertEqual(start["env"]["CLAUDE_CONFIG_DIR"], str(home))
        session = start["argv"][1]
        self.assertTrue((home / "projects" / "-fake-project" / f"{session}.jsonl").is_file())
        # Neither the user's login folder nor $HOME/.claude holds anything of it.
        for folder in (self.root / "claude-config", self.root / "home" / ".claude"):
            self.assertEqual(list(folder.rglob(f"{session}*")), [])
        # The configuration and the conversation copies are TCS Prover's private state.
        for path in (home, claude_cli._run_folder(providers.CLAUDE_TRANSCRIPTS, self.run_dir, runtime)):
            self.assertTrue(any(path.resolve().is_relative_to(hidden) for hidden in providers.private_paths()))
        self.assertNoSecretLeaks()


class RetryWatchdogTests(AuthorCase):
    """I-006: every Claude child waits out 429/529 inside the same request."""

    def test_every_claude_child_runs_with_the_retry_watchdog(self):
        model = self.api_model()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_RETRY_WATCHDOG": "0", "CLAUDE_CODE_MAX_RETRIES": "1"}):
            self.script([["init", OK], ["init", OK], ["init", "echo", complete()]])
            runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
            runtime.structured("Review.", SCHEMA, "review", model=model)
            self.run_goal()
        environments = [start["env"] for start in self.records("start")]
        self.assertEqual([environment.get("CLAUDE_CODE_RETRY_WATCHDOG") for environment in environments], ["1"] * 3)
        self.assertEqual([environment.get("CLAUDE_CODE_MAX_RETRIES") for environment in environments], [None] * 3)


class SurpriseToolTests(AuthorCase):
    """I-101: Agent and Task are one tool; another surprise is denied once, then fails closed."""

    def test_agent_and_task_are_the_same_tool(self):
        self.assertEqual(claude_cli.unexpected_tools({"tools": ["Read", "Agent", "StructuredOutput"]}, ["Read", "Task"]), [])
        self.assertEqual(claude_cli.unexpected_tools({"tools": ["Read", "Task"]}, ["Read", "Agent"]), [])
        self.assertEqual(claude_cli.unexpected_tools({"tools": ["Read", "Agent"]}, ["Read"]), ["Agent"])
        self.script([[{"init": {"extra_tools": ["Agent"]}}, OK]])
        result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5", features=("multi_agent",))
        self.assertEqual(result["notes"], "ok")
        self.assertEqual(len(self.records("start")), 1)

    def test_a_surprise_tool_is_denied_on_the_next_attempt_of_a_structured_call(self):
        self.script([[{"init": {"extra_tools": ["Artifact"]}}, OK], ["init", OK]])
        result, _ = runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
        self.assertEqual(result["notes"], "ok")
        first, second = (start["argv"] for start in self.records("start"))
        self.assertNotIn("--disallowedTools", first)
        self.assertEqual(second[second.index("--disallowedTools") + 1], "Artifact")
        diagnostic = next(event["text"] for event in self.events if event["kind"] == "diagnostic")
        self.assertIn("Claude CLI enabled unexpected tools (Artifact); the request was stopped, and the next attempt "
                      "denies them with --disallowedTools.", diagnostic)
        # Later starts of this runner deny it from the outset.
        self.script([["init", OK]])
        runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
        self.assertIn("--disallowedTools", self.records("start")[-1]["argv"])

    def test_a_tool_that_disallowedtools_does_not_remove_stops_the_call_for_safety(self):
        self.script([[{"init": {"extra_tools": ["Artifact"]}}, OK]] * 3)
        with self.assertRaises(runtime.SafetyStop) as stopped:
            runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
        self.assertIn("unexpected tools (Artifact) that --disallowedTools did not remove", str(stopped.exception))
        self.assertEqual(len(self.records("start")), 2)

    def test_a_tool_name_that_cannot_be_denied_stops_at_once(self):
        self.script([[{"init": {"extra_tools": ["odd tool;name"]}}, OK]] * 2)
        with self.assertRaises(runtime.SafetyStop):
            runtime.structured("Review.", SCHEMA, "review", model="claude-opus-5-5")
        self.assertEqual(len(self.records("start")), 1)

    def test_the_author_is_relaunched_without_a_surprise_tool(self):
        self.script([[{"init": {"extra_tools": ["RemoteTrigger"]}}, "echo", complete("Unused.")],
                     ["init", "echo", complete("Done.")]])
        self.assertEqual(self.run_goal()["output"], "Done.")
        first, second = (start["argv"] for start in self.records("start"))
        self.assertNotIn("--disallowedTools", first)
        self.assertEqual(second[second.index("--disallowedTools") + 1], "RemoteTrigger")
        self.assertEqual(sum(self.clock.sleeps), 60)


class ApiRetryStatusTests(AuthorCase):
    """I-127: the CLI's own retries become status lines, and the structured heartbeat names them."""

    def test_retry_messages_read_as_status_lines(self):
        text = claude_cli.api_retry_text
        self.assertEqual(text({"type": "system", "subtype": "api_retry", "attempt": 3, "max_retries": 10,
                               "retry_delay_ms": 32000, "error_status": 529, "error": "overloaded"}),
                         "Claude API retry: status 529, retrying in 32 s (attempt 3 of 10)")
        self.assertEqual(text({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
                               "retry_delay_ms": 512, "error_status": None, "error": "connection_error"}),
                         "Claude API retry: connection error, retrying in 0.5 s (attempt 1 of 10)")
        self.assertEqual(text({"type": "system", "subtype": "api_retry", "attempt": 2, "retry_delay_ms": 300000,
                               "error_status": 429}),
                         "Claude API retry: status 429, retrying in 5 min (attempt 2)")
        self.assertEqual(text({"type": "tool_progress", "subagent_retry": {
            "agent_id": "a1", "attempt": 4, "max_retries": 10, "retry_delay_ms": 8000, "error_status": 503,
            "error_category": "server_error"}}),
            "Claude API retry (subagent): status 503, retrying in 8 s (attempt 4 of 10)")
        # A subagent that recovered, and other messages, are no retry.
        self.assertIsNone(text({"type": "tool_progress", "tool_name": "Agent", "subagent_type": "general"}))
        self.assertIsNone(text({"type": "system", "subtype": "init"}))

    def test_a_structured_request_reports_the_retry_and_its_heartbeat_names_it(self):
        self.script([["init", api_retry(attempt=3, max_retries=10, retry_delay_ms=32000, error_status=529,
                                        error="overloaded"), {"sleep": 1.2}, OK]])
        with mock.patch.object(runtime, "STRUCTURED_HEARTBEAT_SECONDS", 0.3):
            result, _ = runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5",
                                           activity_label="Critic")
        self.assertEqual(result["notes"], "ok")
        retry = self.status("Critic · Claude API retry")
        self.assertEqual((retry["text"], retry["activityLabel"]),
                         ("Claude API retry: status 529, retrying in 32 s (attempt 3 of 10)", "Critic"))
        beats = [event["text"] for event in self.events if event.get("heartbeat")]
        self.assertTrue(beats)
        self.assertTrue(any(beat.endswith("The CLI is retrying the provider request itself: Claude API retry: "
                                          "status 529, retrying in 32 s (attempt 3 of 10).") for beat in beats), beats)

    def test_the_heartbeat_without_a_retry_keeps_its_plain_note(self):
        self.script([["init", {"sleep": 1.0}, OK]])
        with mock.patch.object(runtime, "STRUCTURED_HEARTBEAT_SECONDS", 0.3):
            runtime.structured("Review.", SCHEMA, "critic", model="claude-opus-5-5")
        beats = [event["text"] for event in self.events if event.get("heartbeat")]
        self.assertTrue(beats)
        self.assertTrue(all(beat.endswith("its API does not expose which.") for beat in beats))

    def test_the_author_reports_its_own_and_its_subagents_retries(self):
        progress = {"emit": {"type": "tool_progress", "tool_use_id": "t1", "tool_name": "Agent",
                             "parent_tool_use_id": None, "elapsed_time_seconds": 0, "subagent_type": "general",
                             "subagent_retry": {"agent_id": "a1", "attempt": 1, "max_retries": 10,
                                                "retry_delay_ms": 2000, "error_status": 529,
                                                "error_category": "overloaded"}}}
        self.script([["init", "echo", api_retry(attempt=1, max_retries=10, retry_delay_ms=1000, error_status=None,
                                                error="connection_error"), progress, complete()]])
        self.assertEqual(self.run_goal()["outcome"], "done")
        retries = [event for event in self.events if event.get("label") == "Claude API retry"]
        self.assertEqual([event["text"] for event in retries],
                         ["Claude API retry: connection error, retrying in 1 s (attempt 1 of 10)",
                          "Claude API retry (subagent): status 529, retrying in 2 s (attempt 1 of 10)"])
        session = self.records("start")[0]["argv"][1]
        self.assertEqual({event["threadId"] for event in retries}, {session})


class QueuedInputTests(AuthorCase):
    """I-064: a re-anchor given up on leads the next turn, and a late pickup is never taken as an answer."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(claude_cli, "QUEUED_INPUT_SECONDS", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)

    compaction = {"emit": {"type": "system", "subtype": "compact_boundary", "uuid": "cb-1", "parent_tool_use_id": None,
                           "compact_metadata": {"trigger": "auto", "pre_tokens": 190000}}}

    def anchor(self):
        return PROMPTS["compaction"].format(directory=self.run_dir)

    def test_a_re_anchor_that_was_not_picked_up_leads_the_next_continuation(self):
        # The CLI takes the queued re-anchor only after the session gave it up and sent the continuation.
        self.script([["init", "echo", self.compaction, in_progress(), {"sleep": 1.0}],
                     ["init", "echo", complete("Answer to the late input.")],
                     ["init", "echo", complete("Done.")]])
        self.assertEqual(self.run_goal()["output"], "Done.")
        self.assertEqual(len(self.records("start")), 1)  # One CLI ran all three turns.
        self.assertEqual(self.texts(), ["Prove S exactly.", self.anchor(),
                                        f"{self.anchor()}\n\n{PROMPTS['continuation']}"])
        diagnostics = [event["text"] for event in self.events if event["kind"] == "diagnostic"]
        self.assertIn("Queued author input was not picked up; a live instruction is offered again later and a "
                      "re-anchor leads the next turn.", diagnostics)
        self.assertIn("Claude picked up queued author input after it was given up on; the turn it starts is not "
                      "taken as an answer.", diagnostics)

    def test_a_late_pickup_during_the_review_is_never_taken_as_the_repair(self):
        self.script([["init", "echo", self.compaction, complete("First proof."), {"sleep": 1.0}],
                     ["init", "echo", complete("Unsupervised answer.")],
                     ["init", "echo", complete("Repaired proof.")]])
        session = self.launcher()(options={"goal_cwd": str(self.run_dir)}, initial_instruction="")
        self.addCleanup(session.close)
        self.assertEqual(next(session)["output"], "First proof.")
        repaired = session.send({"solution": "First proof.", "bugs": "Lemma 2 fails.", "round": 1})
        self.assertEqual(repaired["output"], "Repaired proof.")
        self.assertEqual(len(self.records("start")), 1)
        self.assertNotIn("Unsupervised answer.", [event.get("text") for event in self.events
                                                  if event.get("label") == "Author solution"])
        # The repair prompt re-anchors by itself: the given-up re-anchor does not lead it.
        self.assertEqual(self.texts()[-1], "Revision 1; critic round 1: Lemma 2 fails.\nCandidate: First proof.")

    def test_an_undelivered_re_anchor_leads_the_retry_of_the_same_conversation(self):
        work = {"emit": {"type": "assistant", "parent_tool_use_id": None, "uuid": str(uuid.uuid4()),
                         "message": {"content": [{"type": "text", "text": "Lemma 1 holds."}]}}}
        self.script([["init", "echo", work, self.compaction, {"stderr": "fatal: crashed"}, {"exit": 3}],
                     ["init", "echo", complete("Done.")]])
        self.assertEqual(self.run_goal()["output"], "Done.")
        starts = [start["argv"] for start in self.records("start")]
        self.assertEqual(starts[1][:2], ["--resume", starts[0][1]])
        self.assertEqual(self.texts(), ["Prove S exactly.", f"{self.anchor()}\n\n{runtime.AUTHOR_RETRY_NOTE}"])


class SavedConversationTests(AuthorCase):
    """I-072: a private copy of each conversation; a gone one is restored, else replaced in the same run."""

    def test_each_finished_turn_keeps_a_private_copy_of_the_conversation(self):
        self.script([["init", "echo", complete("First proof.")]])
        author = self.launcher()(options={"goal_cwd": str(self.run_dir)}, initial_instruction="")
        self.addCleanup(author.close)
        self.assertEqual(next(author)["output"], "First proof.")
        # Copied as soon as the turn ended, while the session waits for the critic.
        session = self.records("start")[0]["argv"][1]
        copies = claude_cli._run_folder(providers.CLAUDE_TRANSCRIPTS, self.run_dir, runtime)
        copy = copies / "-fake-project" / f"{session}.jsonl"
        original = self.root / "claude-config" / "projects" / "-fake-project" / f"{session}.jsonl"
        self.assertEqual(copy.read_bytes(), original.read_bytes())
        self.assertEqual(oct(copy.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(copies.stat().st_mode & 0o777), "0o700")

    def test_a_conversation_claude_deleted_is_restored_from_its_copy_and_resumed(self):
        session = self.first_session()
        original = self.root / "claude-config" / "projects" / "-fake-project" / f"{session}.jsonl"
        saved = original.read_bytes()
        original.unlink()  # Claude Code's cleanupPeriodDays sweep.
        self.script([["init", "echo", complete("Resumed.")]], transcripts_required=True)
        options = {"goal_thread_id": session, "goal_resume": True, "goal_require_resume": True}
        self.assertEqual(self.run_goal(options)["output"], "Resumed.")
        self.assertEqual([start["argv"][:2] for start in self.records("start")], [["--resume", session]] * 2)
        self.assertEqual(self.status("Saved conversation restored")["threadId"], session)
        self.assertNotIn("Conversation replaced", self.labels("status"))
        self.assertTrue(original.read_bytes().startswith(saved))  # The resumed conversation went on from it.
        self.assertEqual(self.clock.sleeps, [])

    def test_without_a_usable_copy_the_conversation_is_replaced_without_waiting(self):
        session = self.first_session()
        (self.root / "claude-config" / "projects" / "-fake-project" / f"{session}.jsonl").unlink()
        for copy in claude_cli._run_folder(providers.CLAUDE_TRANSCRIPTS, self.run_dir, runtime).rglob("*.jsonl"):
            copy.unlink()
        self.script([["init", "echo", complete("Fresh.")]], transcripts_required=True)
        options = {"goal_thread_id": session, "goal_resume": True, "goal_require_resume": True}
        self.assertEqual(self.run_goal(options)["output"], "Fresh.")
        replaced = self.status("Conversation replaced")
        self.assertEqual(replaced["text"], "Claude Code cannot reopen the saved author conversation (it no longer "
                                           "has it), so the author continues in a new conversation in this run "
                                           "folder. It starts from the resume prompt; the time budget continues.")
        self.assertNotIn("threadId", replaced)
        started = self.status("Goal started")["threadId"]
        self.assertNotEqual(started, session)
        self.assertEqual(self.texts(), [PROMPTS["resume"].format(directory=self.run_dir, original_prompt="Prove S exactly.")])
        self.assertEqual(self.clock.sleeps, [])


class ResumeRefusalTests(AuthorCase):
    """I-079: identical refusals to reopen a conversation replace it; transient failures never do."""

    session = "5d1e8c2a-3b4f-4a6d-9e7f-8a9b0c1d2e3f"

    def resume(self):
        return {"goal_thread_id": self.session, "goal_resume": True, "goal_require_resume": True}

    @staticmethod
    def refusal(text):
        return [{"stderr": text}, {"exit": 1}]

    def test_three_identical_refusals_replace_the_conversation(self):
        refusal = self.refusal("Failed to resume session 5d1e8c2a-3b4f-4a6d-9e7f-8a9b0c1d2e3f: Unexpected end "
                               "of JSON input at position 81234")
        self.script([refusal, refusal, refusal, ["init", "echo", complete("Fresh.")]])
        self.assertEqual(self.run_goal(self.resume())["output"], "Fresh.")
        starts = [start["argv"] for start in self.records("start")]
        self.assertEqual([argv[0] for argv in starts], ["--resume"] * 3 + ["--session-id"])
        self.assertEqual(sum(self.clock.sleeps), 120)  # Two retries, then no further wait.
        self.assertIn("(3 identical refusals: Failed to resume session 5d1e8c2a-3b4f-4a6d-9e7f-8a9b0c1d2e3f: "
                      "Unexpected end of JSON input at position 81234)", self.status("Conversation replaced")["text"])

    def test_transient_failures_before_a_resume_are_never_counted(self):
        login = self.refusal("Not logged in · Please run /login")
        self.script([login] * 4 + [["init", "echo", complete("Resumed.")]])
        self.assertEqual(self.run_goal(self.resume())["output"], "Resumed.")
        self.assertEqual({tuple(start["argv"][:2]) for start in self.records("start")}, {("--resume", self.session)})
        self.assertNotIn("Conversation replaced", self.labels("status"))
        self.assertEqual(sum(self.clock.sleeps), 240)

    def test_different_refusals_start_the_count_again(self):
        first = self.refusal("Failed to resume session: transcript is locked")
        other = self.refusal("Claude ended this conversation. Start a new session to continue.")
        self.script([first, other, first, other, ["init", "echo", complete("Resumed.")]])
        self.assertEqual(self.run_goal(self.resume())["output"], "Resumed.")
        self.assertNotIn("Conversation replaced", self.labels("status"))

    def leftover(self, flags):
        """A process whose command line looks like an author CLI of this session."""

        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--resume", self.session,
                                    *flags], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        threading.Thread(target=process.wait, daemon=True).start()  # Reaped as soon as it ends.
        return process

    @unittest.skipIf(os.name == "nt", "Leftover CLIs are found with ps")
    def test_a_leftover_cli_that_holds_the_conversation_is_stopped_and_the_conversation_reopened(self):
        held = self.leftover(["--input-format", "stream-json", "--replay-user-messages",
                              "--append-system-prompt-file", f"/private/run-1/{claude_cli.HARNESS_FILE}"])
        unrelated = self.leftover(["--input-format", "stream-json"])  # Not an author CLI of TCS Prover.
        copy = str(uuid.uuid4())
        self.script([[{"init": {"session_id": copy}}], ["init", "echo", complete("Reopened.")]])
        with mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 1, create=True):
            self.assertEqual(self.run_goal(self.resume())["output"], "Reopened.")
        deadline = time.monotonic() + 5
        while held.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(held.poll())
        self.assertIsNone(unrelated.poll())
        self.assertEqual([start["argv"][:2] for start in self.records("start")], [["--resume", self.session]] * 2)
        self.assertIn(f"(PID {held.pid})", " ".join(event.get("text", "") for event in self.events
                                                    if event["kind"] == "diagnostic"))
        self.assertNotIn("Conversation replaced", self.labels("status"))
        self.assertEqual(self.clock.sleeps, [])

    @unittest.skipIf(os.name == "nt", "Leftover CLIs are found with ps")
    def test_a_refusal_before_start_also_stops_a_leftover_cli_first(self):
        held = self.leftover(["--input-format", "stream-json", "--replay-user-messages",
                              "--append-system-prompt-file", f"/private/run-1/{claude_cli.HARNESS_FILE}"])
        self.script([self.refusal(f"That session {self.session} is running in the background. Run `claude attach "
                                  f"{self.session}` to open it, or `claude stop {self.session}` first to resume it "
                                  "here."),
                     ["init", "echo", complete("Reopened.")]])
        with mock.patch.object(runtime, "INTERRUPT_GRACE_SECONDS", 1, create=True):
            self.assertEqual(self.run_goal(self.resume())["output"], "Reopened.")
        self.assertEqual([start["argv"][:2] for start in self.records("start")], [["--resume", self.session]] * 2)
        deadline = time.monotonic() + 5
        while held.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(held.poll())
        self.assertEqual(self.clock.sleeps, [])

    def test_a_conversation_another_process_holds_is_replaced_after_three_refusals(self):
        held = [{"init": {"session_id": str(uuid.uuid4())}}]
        self.script([held, held, held, ["init", "echo", complete("Fresh.")]])
        with mock.patch.object(claude_cli, "leftover_authors", return_value=[]):
            self.assertEqual(self.run_goal(self.resume())["output"], "Fresh.")
        self.assertEqual(sum(self.clock.sleeps), 120)
        self.assertIn("(3 identical refusals: it is open in another Claude Code process, which opened the copy ",
                      self.status("Conversation replaced")["text"])


class TerminalNoticeTests(unittest.TestCase):
    def test_a_terminal_run_shows_a_replaced_conversation(self):
        from ui import cli
        stream = io.StringIO()
        output = cli.ConciseHeadlessOutput(stream, threading.Lock(), "p.md")
        output.write(json.dumps({"kind": "status", "stage": "repair", "node": "author", "label": "Conversation replaced",
                                 "text": "Claude Code cannot reopen the saved author conversation."}) + "\n")
        self.assertEqual(stream.getvalue(),
                         "[p.md] Conversation replaced: Claude Code cannot reopen the saved author conversation.\n")


if __name__ == "__main__":
    unittest.main()
