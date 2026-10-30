"""The contracts between the server, the runner and the page for the time-limit summary, talking to a
paused author and related works, checked end to end without a model or a CLI.

- Server and runner: a reply the server starts runs the real runner entry point
  (workflow_runner.main --converse) in this process, with the command line and stdin the server gave it
  and only the author's turn itself stubbed; the server's reader then settles the item from what the
  runner printed. Resume and proof launches are parsed by the runner as the options it reads.
- Server and page: snapshots and job rows that the real server builds render in ui/app.js (Node's vm).
- Page and markup: every element ui/app.js binds is in ui/index.html.
"""

import contextlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli
from ui import related_works, server

import test_author_conversation as runner_test
import test_conversation_ui as page_test
import test_related_works_server as works_test
import test_related_works_ui as works_page_test
import test_time_limit_conversation as limit_test

REPLY = "Lemma 1 is proved in PROVED.md; Lemma 2 is open: the potential argument fails for k > 3."
# Node runs the page; the server tests replace Popen for every runner they start.
REAL_POPEN = subprocess.Popen


def runner_arguments(argv):
    """The runner's command line of one server launch (after the interpreter and the script).

    Every proof launch passes --critic-count; a runner without that setting (no MAX_CRITIC_COUNT)
    is given the other arguments, so they are still checked against it.
    """

    arguments = [str(value) for value in argv[3:]]
    if "--critic-count" in arguments and not hasattr(runtime, "MAX_CRITIC_COUNT"):
        index = arguments.index("--critic-count")
        del arguments[index:index + 2]
    return arguments


class ServerRunnerContractTests(limit_test.PausedAuthorCase):
    """The server's conversation launch, the runner's --converse entry and the server's reader, together."""

    def setUp(self):
        super().setUp()
        self.calls, self.turns, self.checked = [], [], []
        self.runner_running = False
        real_check = runtime.require_model_credentials

        def check(model):
            self.checked.append(model)
            return real_check(model)

        for change in (patch.object(runtime, "require_model_credentials", side_effect=check),
                       patch.object(runtime, "configure_standard_streams"),
                       patch.object(claude_cli, "sweep_stale_settings"),
                       patch.object(runtime, "check_clis", side_effect=AssertionError("CLI check")),
                       patch.object(runtime, "goal_session", side_effect=AssertionError("goal session")),
                       patch.object(claude_cli, "goal_session", side_effect=AssertionError("goal session")),
                       patch.object(claude_cli, "author_conversation", side_effect=AssertionError("Claude turn")),
                       patch.object(runtime, "author_conversation", side_effect=self.turn)):
            change.start()
            self.addCleanup(change.stop)

    def popen(self, argv, **kwargs):
        if self.runner_running:
            raise AssertionError(f"The runner started a process: {argv}")
        return super().popen(argv, **kwargs)

    def turn(self, module, instruction, **fields):
        """The author's conversation turn, as the runner calls it: the next of ``self.turns``."""

        self.calls.append({"module": module, "instruction": instruction, **fields})
        outcome = self.turns.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"threadId": "thread-1", "compacted": False, **outcome}

    def run_runner(self, argv, stdin_text):
        """workflow_runner.main as the server started it; (exit code, merged output lines)."""

        self.assertEqual(argv[:3], [sys.executable, "-u", str(server.ROOT / "workflow_runner.py")])
        stdout, stderr = io.StringIO(), io.StringIO()
        self.runner_running = True
        try:
            with patch.object(sys, "stdin", io.StringIO(stdin_text)), contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = runtime.main(runner_arguments(argv))
        finally:
            self.runner_running = False
        return code, stdout.getvalue() + stderr.getvalue()

    def converse(self, *turns):
        """Start the due reply, run its runner with ``turns`` and let the server's reader settle it."""

        self.turns.extend(turns)
        self.runners.append(limit_test.FakeRunner())
        self.assertTrue(self.app.start_conversation_if_due())
        argv, kwargs, runner = self.launches[-1]
        target, args = self.spawned[-1]
        self.assertIs(args[0], runner)
        self.assertEqual(Path(kwargs["cwd"]), self.run)
        request = json.loads(runner.stdin.text)
        code, output = self.run_runner(argv, runner.stdin.text)
        runner.stdout, runner.code = io.StringIO(output), code
        target(*args)
        self.assertEqual(self.turns, [], "every scripted turn ran")
        return code, request, [json.loads(line) for line in output.splitlines() if line.startswith("{")]

    def item(self):
        return self.items()[-1]

    def test_the_time_limit_summary_runs_one_author_turn_and_settles_the_item(self):
        self.time_limit_pause()
        pause = (server.controller_directory(self.run) / server.PAUSE_FILENAME).read_bytes()
        code, request, records = self.converse({"reply": REPLY})
        self.assertEqual(code, 0)
        self.assertEqual(request, {"schemaVersion": 1, "id": self.item()["id"], "kind": "summary", "text": "",
                                   "notice": ""})
        call, = self.calls
        self.assertEqual(call["instruction"], runner_test.SUMMARY_TEXT)
        self.assertEqual((call["conversation_id"], call["label"], call["attempt"], call["rewind"]),
                         (self.item()["id"], "Progress summary requested", 1, None))
        options = call["options"]
        self.assertEqual((options["goal_thread_id"], options["goal_cwd"], options["goal_require_resume"]),
                         ("thread-1", str(self.run.resolve()), True))
        self.assertEqual(options["workflow_stages"], server.CONVERSATION_STAGES)
        self.assertEqual((options["thinking_hours"], options["goal_stderr_log"]),
                         (0, str(self.run / server.AUTHOR_STDERR_FILENAME)))
        for name in ("author_limit_file", "author_steer_file", "goal_pause_file", "start_node", "state_file"):
            self.assertNotIn(name, options)
        self.assertEqual(call["settings"]["model"], self.app.state["authorModel"])
        # Only the author's model and credentials are checked: no other role is prepared.
        self.assertEqual(set(self.checked), {self.app.state["authorModel"]})
        # Every record of the runner carries the conversation, and ends with the reply.
        self.assertTrue(records)
        self.assertTrue(all((record["stage"], record["node"], record["conversationId"])
                            == ("conversation", "author", self.item()["id"]) for record in records))
        self.assertEqual(records[-1]["kind"], "conversation_reply")
        self.assertEqual((records[-1]["label"], records[-1]["conversationKind"]), ("Progress summary", "summary"))
        item = self.item()
        self.assertEqual((item["status"], item["reply"], item["error"], item["origin"]),
                         ("answered", REPLY, "", "time-limit"))
        summary = (self.run / server.PROGRESS_SUMMARY_FILENAME).read_text(encoding="utf-8")
        self.assertRegex(summary, r"\A# Progress summary \(\d{4}-\d\d-\d\d \d\d:\d\d UTC\)\n\n")
        self.assertEqual(summary.split("\n", 2)[2], REPLY + "\n")
        # The pause is untouched: a reply never changes the checkpoint.
        self.assertEqual((server.controller_directory(self.run) / server.PAUSE_FILENAME).read_bytes(), pause)
        self.assertEqual(self.app.state["phase"], "paused")
        self.assertFalse(self.app.state["pendingReanchor"])

    def test_a_message_carries_the_pending_notice_once(self):
        self.error_pause()
        self.app.state["autoResumeAt"] = 0
        self.app.state["relatedWorksPending"] = ["paper.pdf", "Plan.md"]
        self.app.ask_author("  Why does Lemma 2 need k > 3?  ")
        code, request, records = self.converse({"reply": "Because the potential drops."})
        self.assertEqual(code, 0)
        notice = related_works.notice_text(["paper.pdf", "Plan.md"])
        self.assertEqual((request["kind"], request["text"], request["notice"]),
                         ("message", "Why does Lemma 2 need k > 3?", notice))
        call, = self.calls
        message = runner_test.CONVERSATION_TEXT.format(message="Why does Lemma 2 need k > 3?")
        self.assertEqual(call["instruction"], notice + "\n\n" + message)
        self.assertEqual(call["label"], "Message to the paused author")
        reply = records[-1]
        self.assertEqual((reply["kind"], reply["label"], reply["conversationKind"]),
                         ("conversation_reply", "Author reply", "message"))
        item = self.item()
        self.assertEqual((item["status"], item["reply"], item["notice"]),
                         ("answered", "Because the potential drops.", ["paper.pdf", "Plan.md"]))
        # Delivered: neither the next message nor Resume repeats the notice.
        self.assertEqual(self.app.state["relatedWorksPending"], [])
        saved = json.loads((server.controller_directory(self.run) / server.JOB_SETTINGS_FILENAME).read_text())
        self.assertEqual(saved["relatedWorksPending"], [])
        # A message reply is not a progress summary.
        self.assertFalse((self.run / server.PROGRESS_SUMMARY_FILENAME).exists())

    def test_a_failed_attempt_is_retried_in_the_same_conversation_outside_the_workflow_clock(self):
        self.time_limit_pause()
        cut_off = runtime.ConversationAttemptFailed("Model call failed: 503 Service Unavailable",
                                                    retry={"delivered": True, "output": True})
        with patch.object(runtime, "MODEL_RETRY_SECONDS", 0), patch.object(runtime, "_retry_jitter", return_value=0):
            code, _, records = self.converse(cut_off, {"reply": REPLY})
        self.assertEqual(code, 0)
        first, second = self.calls
        self.assertEqual((first["attempt"], second["attempt"]), (1, 2))
        # A reply that was cut off is asked for again, in the same conversation.
        self.assertEqual(second["instruction"], runner_test.RETRY_NOTE_TEXT)
        self.assertEqual(second["options"]["goal_thread_id"], "thread-1")
        waits = [record for record in records if record.get("label") == runtime.RETRY_LABEL]
        self.assertEqual(len(waits), 2)
        self.assertIn("retryAt", waits[0])
        self.assertNotIn("retryAt", waits[1])
        self.assertFalse(any("budgetClock" in record or record.get("label") == "Workflow clock" for record in records))
        # The server kept the waits in the transcript, with the conversation, and the item ends answered.
        kept = [record for record in self.app.state["trace"] if record.get("label") == runtime.RETRY_LABEL]
        self.assertEqual([(record["stage"], record["conversationId"]) for record in kept],
                         [("conversation", self.item()["id"])] * 2)
        item = self.item()
        self.assertEqual((item["status"], item["reply"], item["attempts"]), ("answered", REPLY, 1))
        self.assertNotIn("retry", item)

    def test_a_conversation_that_is_gone_fails_the_item_with_the_runner_reason(self):
        self.time_limit_pause()
        gone = runtime.SavedConversationGone("Codex no longer has the saved author conversation (thread not found).")
        code, _, records = self.converse(gone)
        self.assertEqual(code, 0)
        self.assertEqual((records[-1]["label"], records[-1]["final"], records[-1]["failure"]),
                         ("Author reply failed", True, "unavailable"))
        item = self.item()
        self.assertEqual((item["status"], item["error"]),
                         ("failed", "Codex no longer has the saved author conversation (thread not found)."))
        # Nothing retries a final failure: no reply is due any more.
        self.assertFalse(self.app.start_conversation_if_due())
        self.assertFalse(self.app.snapshot()["conversation"]["busy"])

    def test_a_runner_that_ends_with_an_error_line_fails_the_item(self):
        self.time_limit_pause()
        with patch.object(runtime, "load_workflow", side_effect=ValueError("The workflow YAML cannot be read.")):
            code, _, _ = self.converse()
        self.assertEqual(code, 1)
        self.assertEqual((self.item()["status"], self.item()["error"]),
                         ("failed", "The workflow YAML cannot be read."))

    def test_a_compacted_reply_makes_the_next_resume_send_the_full_resume_prompt(self):
        self.time_limit_pause()
        self.converse({"reply": REPLY, "compacted": True})
        self.assertTrue(self.item()["compacted"])
        self.assertTrue(self.app.state["pendingReanchor"])
        self.app.set_author_time_limit(4)
        launched = []

        def execute(workflows, state, options):
            launched.append((workflows, state, options))
            return {"paused": True}

        self.app.resume()
        argv = self.launches[-1][0]
        with patch.object(runtime, "execute_workflows", side_effect=execute), patch.object(runtime, "check_clis"):
            code, _ = self.run_runner(argv, "")
        self.assertEqual(code, 0)
        (workflows, state, options), = launched
        self.assertEqual([Path(path).name for path in workflows], ["author_critic.yaml", "clean_up.yaml"])
        self.assertEqual((options["goal_reanchor"], options["goal_require_resume"], options["goal_thread_id"]),
                         (True, True, "thread-1"))
        self.assertEqual((options["start_node"], options["thinking_hours"]), ("author", 4))
        self.assertNotIn("related_works", options)
        # The critic starts its independent critics afresh: no reports of theirs are kept for a resume.
        self.assertNotIn("critic_audit_file", options)
        if hasattr(runtime, "MAX_CRITIC_COUNT"):
            self.assertEqual(options["critic_count"], self.app.state["criticCount"])


class RelatedWorksLaunchContractTests(works_test.RelatedWorksCase):
    """The server's related-works flag, as the runner parses it."""

    def parsed(self, argv):
        launched = []

        def execute(workflows, state, options):
            launched.append(options)
            return {"paused": True}

        with patch.object(runtime, "execute_workflows", side_effect=execute), patch.object(runtime, "check_clis"), \
                patch.object(runtime, "configure_standard_streams"), \
                patch.object(claude_cli, "sweep_stale_settings"), \
                patch.object(sys, "stdin", io.StringIO("Prove S.")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.main(runner_arguments(argv)), 0)
        return launched[0]

    def test_a_first_launch_with_related_works_asks_for_the_block(self):
        app = self.start(relatedWorks=[self.upload("A paper.txt")])
        argv = self.launches[-1][0]
        self.assertIs(self.parsed(argv)["related_works"], True)
        self.assertEqual(app.snapshot()["relatedWorks"]["count"], 1)

    def test_every_job_of_a_folder_run_with_related_works_asks_for_the_block(self):
        started = self.manager.start_direct_batch({
            "statements": [{"name": "a.md", "statement": "Prove a."}, {"name": "b.md", "statement": "Prove b."}],
            "workflowStages": dict(works_test.STAGES), "relatedWorks": [self.upload("A paper.txt")]})
        self.assertEqual(len(self.launches), 2)
        for app, (argv, _, _) in zip(started, self.launches):
            with self.subTest(job=app.state["sourceFile"]):
                self.assertEqual([item["batch"] for item in app.snapshot()["relatedWorks"]["items"]],
                                 [started.folder_batch])
                self.assertIs(self.parsed(argv)["related_works"], True)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class ServerPageContractTests(works_test.RelatedWorksCase):
    """Snapshots and job rows of the real server, rendered by ui/app.js."""

    def run_app(self, setup, checks):
        """Run ``checks`` against render() and the related-works code of ui/app.js, after ``setup``."""

        with patch.object(subprocess, "Popen", REAL_POPEN):
            page_test.ConversationBrowserTests.run_app(self, works_page_test.PANEL,
                                                       works_page_test.PANEL_SETUP + setup, checks)

    def folder_run(self):
        token = self.upload("A paper.txt")
        started = self.manager.start_direct_batch({
            "statements": [{"name": "a.md", "statement": "Prove a."}, {"name": "b.md", "statement": "Prove b."}],
            "workflowStages": dict(works_test.STAGES), "relatedWorks": [token]})
        return started

    def time_limit_pause(self, app):
        """Settle ``app``'s running author into a time-limit pause, then answer its summary."""

        app.state.update(goalThreadId="thread-1", thinkingHours=1, elapsedSeconds=3600)
        app.state["resumedAt"] = app.state["startedAt"] = server.datetime.now(server.timezone.utc).isoformat()
        token = object()
        process = works_test.FakeRunner([{"kind": "workflow_paused", "stage": "solve", "node": "author",
                                          "threadId": "thread-1", "state": {"statement": "Prove a.", "paused": True},
                                          "reason": "Workflow time limit reached."}])
        app.process, app.active_token, app.worker_token = process, token, token
        app._read_output(process, token)
        self.runners.append(works_test.FakeRunner([
            {"kind": "conversation_reply", "stage": "conversation", "node": "author", "label": "Progress summary",
             "conversationKind": "summary", "text": REPLY, "threadId": "thread-1", "compacted": False}]))
        self.assertTrue(app.start_conversation_if_due())
        target, args = self.spawned[-1]
        target(*args)
        return app

    @staticmethod
    def wire(value):
        """What the browser receives: the JSON the server sends."""

        return json.loads(json.dumps(value))

    def test_a_folder_run_job_at_its_time_limit_renders_its_summary_and_related_works(self):
        started = self.folder_run()
        app = self.time_limit_pause(started[0])
        snapshot = self.wire({**app.snapshot(), "batchId": "__root__"})
        self.assertEqual((snapshot["phase"], snapshot["pauseCause"], snapshot["timeLimitExhausted"]),
                         ("paused", "time-limit", True))
        self.run_app(f"\nconst snapshot = {json.dumps(snapshot)};\n", r"""
currentJob = snapshot.runId;
render(snapshot);
assert.equal(ui.runTitle.textContent, 'Time limit reached');
assert.match(ui.runDescription.textContent, /^The proof author reached the time limit of 1 hour after /);
assert.equal(ui.authorTimeLimitControl.open, true);
// The summary, as the conversation card shows it.
assert.equal(ui.authorConversation.hidden, false);
const [entry] = ui.authorConversationList.children;
assert.equal(entry.dataset.status, 'answered');
assert.match(entry.children[0].textContent, /^Progress summary at the time limit · .+$/);
assert.equal(entry.children[1].textContent, snapshot.conversation.items[0].reply);
// The box asks the author stopped at its time limit; Continue waits for a higher limit.
assert.equal(ui.authorSteerControl.hidden, false);
assert.equal(ui.authorSteerLabel.textContent, 'Ask the author');
assert.equal(ui.resume.textContent, 'Continue');
assert.equal(ui.requestSummary.hidden, false);
assert.equal(ui.resume.disabled, false);
// The batch's related work, linked into this job, and where works for the whole folder run go.
assert.equal(ui.relatedWorksPanel.hidden, false);
assert.equal(ui.relatedWorksHeading.textContent, 'Related works · 1');
assert.deepEqual(rowText(ui.relatedWorksList),
  [['TXT', 'A-paper.txt', '15 B · uploaded as “A paper.txt” · from the batch']]);
assert.equal(ui.relatedWorksNote.textContent, "Saved in the author's related-works folder. The author is told "
  + 'about new files with your next message or when the run resumes. Works for every problem of this folder run '
  + 'are added under Batch related works on the batch page.');
assert.equal(ui.relatedWorksActions.hidden, false);
""")

    def test_a_reply_being_written_disables_resume_and_offers_stop_reply(self):
        started = self.folder_run()
        app = self.time_limit_pause(started[0])
        app.set_author_time_limit(3)
        app.ask_author("Which step fails?")
        snapshot = self.wire(app.snapshot())
        self.assertEqual(snapshot["conversation"]["busy"], True)
        self.run_app(f"\nconst snapshot = {json.dumps(snapshot)};\n", r"""
currentJob = snapshot.runId;
render(snapshot);
assert.equal(ui.runTitle.textContent, 'Time limit reached');
assert.match(ui.runDescription.textContent, /^The time limit is now 3 hours \(.+ left\)\. Choose Continue to go on\.$/);
assert.equal(ui.resume.disabled, true);
assert.equal(ui.resume.title, 'The author is replying. Wait for the reply or stop it.');
assert.equal(ui.stopReply.hidden, false);
assert.equal(ui.requestSummary.hidden, true);
assert.equal(ui.sendAuthorSteer.disabled, true);
const items = ui.authorConversationList.children;
assert.equal(items.length, 2);
assert.equal(items[1].dataset.status, 'queued');
assert.deepEqual(items[1].children.map(node => node.textContent).slice(1),
  ['Which step fails?', 'Waiting to start…']);
await ui.stopReply.onclick();
assert.deepEqual(acted.at(-1), {path: '/stop-author-reply', body: {id: snapshot.conversation.items[1].id}});
""")

    def test_the_batch_page_groups_the_server_job_rows_by_their_folder_batch(self):
        started = self.folder_run()
        rows = self.wire(self.manager.job_list())
        self.assertEqual({row["folderBatch"] for row in rows}, {started.folder_batch})
        listing = self.wire(self.manager.batch_related_works(started.folder_batch))
        self.run_app(f"\nconst rows = {json.dumps(rows)};\nconst listing = {json.dumps(listing)};\n", r"""
currentJob = '';
answers['/batch-related-works'] = path => {
  assert.equal(path, '/batch-related-works?batch=' + listing.id);
  return listing;
};
renderBatchRelatedWorks(rows);
await tick(); await tick();
assert.deepEqual(sent.map(item => item.path), ['/batch-related-works?batch=' + listing.id]);
assert.equal(ui.batchRelatedWorks.hidden, false);
const [group] = ui.batchRelatedGroups.children;
assert.equal(group.dataset.batch, listing.id);
assert.equal(group.children[0].children.map(node => node.textContent).join(' | '), 'Folder run | 1 related work');
assert.equal(group.children[1].textContent, '2 problems: ' + rows.map(row => row.sourceFile).join(', '));
assert.deepEqual(rowText(group.children[2]), [['TXT', 'A-paper.txt', '15 B · uploaded as “A paper.txt”']]);
""")


class PageMarkup(HTMLParser):
    """The ids and attribute names of the shipped ui/index.html."""

    def __init__(self):
        super().__init__()
        self.ids, self.attributes = set(), set()
        self.feed((server.UI / "index.html").read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        self.ids.update(value for name, value in attrs if name == "id")
        self.attributes.update(name for name, _ in attrs)


class PageMarkupContractTests(unittest.TestCase):
    """ui/app.js and the markup it binds."""

    def test_every_element_the_page_binds_is_in_the_markup(self):
        source = (server.UI / "app.js").read_text(encoding="utf-8")
        bindings = set(re.findall(r'\$\("([^"]+)"\)', source))
        self.assertGreater(len(bindings), 100)
        self.assertEqual(bindings - PageMarkup().ids, set())

    def test_the_job_page_has_no_research_files_viewer(self):
        # Older runs may still hold APPROACHES/, PROVED.md or AUDITS/; the page neither shows nor asks for them.
        page = PageMarkup()
        self.assertFalse({"memoryPanel", "memoryDocument", "approachGraph"} & page.ids)
        self.assertNotIn("data-memory", page.attributes)
        source = (server.UI / "app.js").read_text(encoding="utf-8")
        for route in ('"/memory"', '"/approach-graph"'):
            self.assertNotIn(route, source)
        styles = (server.UI / "styles.css").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\.(?:memory|approach)-|--graph-", styles))


if __name__ == "__main__":
    unittest.main()
