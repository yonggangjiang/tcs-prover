"""Web search that may fail, or failed, says so while the run goes on (after the first real coordinator trial,
14 Nov 2026: a connection whose test search failed still saves with its switch on, and searches that failed at run
time raised no warning).

- A job whose runner starts searching through an API connection whose saved test search did not pass gets a pinned
  "Web search may fail" line (workflow_runner.note_failed_search_checks()).
- A web search of a Codex run that never came back gets a "Web search failed" line after its turn: Codex 0.160 sends
  a search's item/started (item.started under `codex exec --json`), and its item/completed only once the search
  service answered (the real Codex against a scripted search service: a 404, a 429, a 500 after its retries, a
  dropped connection, a body it could not read all left the search without one; workflow_runner.SearchWatch). The
  author's goal loop, structured calls and team members each say so; a turn that did not complete (a pause) does not.

Offline: stand-ins only; no CLI is started.
"""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers, team
import workflow_runner as runtime

import test_goal_runtime as goal_test
import test_model_retry as retry_test
import test_team_goal_loop as team_test

KEY = "sk-search-failure-test-0001"
FAILED_CHECK = {"ok": False, "at": "2026-11-14T13:45:26Z",
                "message": "The test search could not reach the search service: the connection was refused."}


class StoreCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        store = str(self.root / "state" / "api-models.json")
        self.events = []
        for change in (mock.patch.dict(os.environ, {providers.STORE_ENV: store}),
                       mock.patch.object(runtime, "emit", side_effect=lambda kind, stage, **fields: self.events.append(
                           {"kind": kind, "stage": stage, **fields}))):
            change.start()
            self.addCleanup(change.stop)
        self.addCleanup(providers._LAST_GOOD.pop, store, None)

    def connection(self, label="Proxy", **values):
        """A saved connection (its search switch and saved test search in ``values``) and its one model's id."""

        saved = providers.save_api_connection({"format": "openai", "baseUrl": "https://proxy.example.com/v1",
                                               "apiKey": KEY, "label": label, **values},
                                              [{"id": "gpt-6.1-sol", "label": "gpt-6.1-sol"}])
        return {**saved, "modelId": saved["models"][0]["modelId"]}

    def lines(self, label):
        return [event for event in self.events if event.get("label") == label]


class StartNoticeTests(StoreCase):
    def test_a_connection_whose_test_search_failed_says_so_as_the_runner_starts(self):
        failed = self.connection(webSearch=True, webSearchCheck=FAILED_CHECK)
        passed = self.connection(label="Other", webSearch=True, webSearchCheck={"ok": True, "at": "", "message": "Fine."})
        untested = self.connection(label="Untested", webSearch=True)
        ids = {entry.get("sourceId", entry["id"]) for entry in (failed, passed, untested)}
        with mock.patch.object(runtime, "API_WEB_SEARCH", frozenset(ids)):
            runtime.note_failed_search_checks()
        notice, = self.lines("Web search may fail")
        self.assertEqual((notice["kind"], notice["stage"], notice["searchCheckFailed"], notice["connection"]),
                         ("diagnostic", "workflow", True, "Proxy"))
        self.assertEqual(notice["text"], "Web search through Proxy may fail: its test search did not pass (2026-11-14 "
                                         "13:45 UTC): The test search could not reach the search service: the "
                                         "connection was refused. The run goes on; each web search that fails says "
                                         "so in the activity log.")

    def test_a_job_that_does_not_search_through_it_says_nothing(self):
        self.connection(webSearch=True, webSearchCheck=FAILED_CHECK)
        with mock.patch.object(runtime, "API_WEB_SEARCH", frozenset()):
            runtime.note_failed_search_checks()
        self.assertEqual(self.events, [])

    def test_the_runner_says_it_as_it_starts_but_a_reply_turn_does_not(self):
        calls = []
        with mock.patch.object(runtime, "note_failed_search_checks", side_effect=lambda: calls.append("checked")), \
                mock.patch.object(runtime, "execute_workflows", side_effect=RuntimeError("stop here")), \
                mock.patch.object(runtime, "check_clis"), mock.patch.object(runtime, "open_record"), \
                mock.patch.object(runtime.sys, "stdin", SimpleNamespace(read=lambda: "S.")), \
                mock.patch.object(runtime.sys, "stderr", io.StringIO()):
            self.assertNotEqual(runtime.main([str(team_test.WORKFLOW)]), 0)
        self.assertEqual(calls, ["checked"])
        calls.clear()
        with mock.patch.object(runtime, "note_failed_search_checks", side_effect=lambda: calls.append("checked")), \
                mock.patch.object(runtime, "run_author_conversation", return_value=0), \
                mock.patch.object(runtime, "open_record"), \
                mock.patch.object(runtime, "conversation_request", return_value={}), \
                mock.patch.object(runtime.sys, "stdin", SimpleNamespace(read=lambda: "{}")):
            runtime.main([str(team_test.WORKFLOW), "--converse"])
        self.assertEqual(calls, [])


class WatchTests(unittest.TestCase):
    def test_a_search_that_started_and_never_came_back_failed(self):
        watch = runtime.SearchWatch()
        for method, identity in (("item/started", "a"), ("item/completed", "a"), ("item/started", "b"),
                                 ("item.started", "c"), ("item.completed", "c"), ("item.started", "d")):
            watch.item(method, {"type": "web_search" if method.startswith("item.") else "webSearch", "id": identity})
        watch.item("item/started", {"type": "commandExecution", "id": "e"})
        self.assertEqual(watch.failed(), 2)
        self.assertEqual(watch.failed(), 0)  # Forgotten with its turn.


def search(method, identity="ws-1"):
    return goal_test.event(method, item={"type": "webSearch", "id": identity, "query": "", "action": None,
                                         "results": None})


class AuthorTests(StoreCase):
    """The author's goal loop (the RPC double of tests/test_goal_runtime.py)."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        popen = mock.patch.object(runtime.subprocess, "Popen", return_value=SimpleNamespace())
        popen.start()
        self.addCleanup(popen.stop)

    def session(self, double, model="gpt-6.1-sol"):
        session = runtime.goal_session(
            double, "Prove S.", prompts=goal_test.PROMPTS,
            settings={"model": model, "effort": "high", "speed": "standard", "summary": "concise"},
            features=[], options={"goal_cwd": str(self.directory)})
        self.addCleanup(session.close)
        return session

    def test_searches_that_never_came_back_say_so_after_the_turn(self):
        double = goal_test.Runtime([[search("item/started", "ws-1"), search("item/completed", "ws-1"),
                                     search("item/started", "ws-2"), search("item/started", "ws-3"),
                                     *goal_test.success()]])
        self.assertEqual(next(self.session(double))["output"], "Full proof")
        line, = [event for event in double.events if event.get("label") == "Web search failed"]
        self.assertEqual((line["kind"], line["failedSearches"]), ("diagnostic", 2))
        self.assertEqual(line["text"], "The proof author: 2 web searches of this turn failed: Codex started them, and "
                                       "its search service answered with an error, nothing, or something Codex could "
                                       "not read. The run goes on; the model got the error and may search again.")

    def test_an_api_connection_s_search_service_is_named(self):
        entry = self.connection(webSearch=True)
        with mock.patch.object(runtime, "API_WEB_SEARCH", frozenset({entry.get("sourceId", entry["id"])})):
            self.assertIn("and the search service of Proxy answered", runtime.failed_search_text(1, entry["modelId"]))
            self.assertTrue(runtime.failed_search_text(1, entry["modelId"]).startswith(
                "1 web search of this turn failed: Codex started it, and "))

    def test_a_turn_that_did_not_complete_says_nothing(self):
        pause = self.directory / "pause-request.json"

        def paused(rpc):
            pause.write_text("{}")
            rpc.messages.extend([search("item/started"), goal_test.completed("interrupted")])

        double = team_test.LoopDouble([paused])
        session = runtime.goal_session(
            double, "Prove S.", prompts=goal_test.PROMPTS,
            settings={"model": "gpt-6.1-sol", "effort": "high", "speed": "standard", "summary": "concise"},
            features=[], options={"goal_cwd": str(self.directory), "goal_pause_file": str(pause)})
        self.addCleanup(session.close)
        with self.assertRaises(runtime.WorkflowPaused):
            next(session)
        self.assertFalse([event for event in double.events if event.get("label") == "Web search failed"])


@unittest.skipIf(os.name == "nt", "The stand-in CLI is a POSIX script")
class StructuredCallTests(retry_test.RetryCase):
    """A structured call whose stand-in `codex exec --json` reports a search that never came back."""

    def setUp(self):
        super().setUp()
        self.state = self.root / "codex-state"
        self.state.mkdir()
        # The retry tests' stand-in, which also prints a behaviour's "events" after its turn.started.
        script = retry_test.CODEX_FAKE.replace('event({"type": "turn.started"})\n', 'event({"type": "turn.started"})\n'
                                               'for line in behaviour.get("events", []):\n    event(line)\n', 1)
        self.assertIn('behaviour.get("events"', script)
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n{script}")
        fake.chmod(0o700)
        for change in (mock.patch.dict(os.environ, {"TCS_FAKE_CODEX_STATE": str(self.state),
                                                     "CODEX_HOME": str(self.root / "codex-home"),
                                                     "TCS_PROVER_RETRIEVAL_MODE": "live"}),
                       mock.patch.object(runtime, "codex", side_effect=lambda *args: str(fake))):
            change.start()
            self.addCleanup(change.stop)
        os.environ.pop("TCS_PROVER_RETRIEVAL_POLICY", None)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def test_the_call_says_which_of_its_searches_failed(self):
        started = {"type": "item.started", "item": {"id": "ws-1", "type": "web_search", "query": "",
                                                    "action": {"type": "other"}}}
        (self.state / "script.json").write_text(json.dumps([{"events": [started], "answer": retry_test.VALID}]))
        runtime.structured("Review S.", retry_test.SCHEMA, "critic", model="gpt-6.1-sol", effort="high",
                           activity_label="Critic coordinator")
        line, = self.statuses_and("Web search failed")
        self.assertEqual((line["activityLabel"], line["failedSearches"]), ("Critic coordinator", 1))
        self.assertTrue(line["text"].startswith("Critic coordinator: 1 web search of this turn failed: Codex started "
                                                "it, and its search service answered"))

    def statuses_and(self, label):
        return [event for event in self.events if event.get("label") == label]


class MemberTests(team_test.TeamCase):
    def test_a_member_s_search_that_never_came_back_is_its_own_line(self):
        crew = self.crew([{"model": "gpt-6.1-sol", "effort": "high"}])

        def attempt(member, prompt, conversation, started, *, directory, settings, compact_prompt, record,
                    retried=None, noted=None):
            noted(runtime.failed_search_text(1, member.model), failedSearches=1)
            return "Done: " + prompt.splitlines()[0]

        with mock.patch.object(runtime, "member_attempt", side_effect=attempt):
            crew.call("team_assign", {"member": 1, "assignment": "Lemma A"})
            self.assertIn("Done: Lemma A", crew.call("team_wait", {"timeout_minutes": 1}))
        line, = self.labelled("Web search failed")
        self.assertEqual((line["kind"], line["subagentMember"], line["subagentId"], line["failedSearches"]),
                         ("diagnostic", 1, "member-1-1", 1))
        self.assertTrue(line["text"].startswith("Member 1 (GPT 6.1 Sol): 1 web search of this turn failed"))

    def test_codex_s_member_run_finds_it(self):
        # The stand-in `codex exec --json` of the team tests, with a search that started and never came back.
        started = '{"type": "item.started", "item": {"id": "ws-1", "type": "web_search", "query": ""}}'
        fake = self.bin / "codex"
        fake.write_text(fake.read_text().replace(
            'out({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message"',
            f'sys.stdout.write({started!r} + "\\n")\nout({{"type": "item.completed", "item": {{"id": "item_1", '
            '"type": "agent_message"', 1))
        noted = []
        conversation = runtime.MemberConversation("codex", None, runtime.MemberSessions())
        runtime._codex_member_attempt("Lemma A", "gpt-6.1-sol", "high", "concise", directory=self.run_dir,
                                      conversation=conversation, record=lambda event, root: None,
                                      started=lambda process: None, speed="standard", compact_prompt="",
                                      noted=lambda text, **fields: noted.append((text, fields)))
        (text, fields), = noted
        self.assertEqual(fields, {"failedSearches": 1})
        self.assertTrue(text.startswith("1 web search of this turn failed"))


if __name__ == "__main__":
    unittest.main()
