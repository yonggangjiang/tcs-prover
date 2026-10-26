"""Regression tests for the transcript viewer's audited bugs, without model calls."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from transcript import view_transcript as vt


ROOT = Path(__file__).resolve().parents[1]
EDITED_NOTE = "Edited pass: the critic repaired the candidate"


def narrative(records, **options):
    output = io.StringIO()
    viewer = vt.TranscriptViewer(output, **options)
    for record in records:
        viewer.consume(record)
    viewer.finish()
    return output.getvalue()


def exec_event(event, stage="critic", label=""):
    return {"kind": "codex_event", "stage": stage, "activityLabel": label,
            "event": event, "time": "2026-10-21T12:00:00+00:00"}


def collab(item_id, tool, *, completed=False, prompt=None, states=None, label=""):
    return exec_event({
        "type": "item.completed" if completed else "item.started",
        "item": {
            "id": item_id, "type": "collab_tool_call", "tool": tool,
            "sender_thread_id": "t0", "receiver_thread_ids": [], "prompt": prompt,
            "agents_states": states or {},
            "status": "completed" if completed else "in_progress",
        },
    }, label=label)


def delta(stage, item_id, text, *, root=True):
    return {"kind": "codex_event", "stage": stage, "root": root,
            "time": "2026-10-21T12:00:05+00:00",
            "event": {"method": "item/agentMessage/delta",
                      "params": {"itemId": item_id, "delta": text}}}


class RecordingStream(io.StringIO):
    """Remember what had been flushed when a blocking call starts."""

    def __init__(self):
        super().__init__()
        self.flushed = ""

    def flush(self):
        self.flushed = self.getvalue()
        super().flush()


class CriticOutcomeTests(unittest.TestCase):
    def critic(self, outcome=None, **report):
        record = {"kind": "critic_result", "stage": "critic", "label": "Critic round 1",
                  "round": 1, "report": report}
        if outcome:
            record["outcome"] = outcome
        return record

    def test_edited_pass_is_marked_in_text_and_browser_views(self):
        edited = self.critic("fixed", verdict="pass", solution="Repaired proof", bugs="")
        legacy = self.critic(verdict="pass", fixed=True)
        clean = self.critic("pass", verdict="pass", solution="Same proof", bugs="")
        reject = self.critic("reject", verdict="reject", solution="Proof", bugs="Lemma 2 fails.")

        self.assertIn(EDITED_NOTE, narrative([edited]))
        self.assertIn(EDITED_NOTE, narrative([legacy]))
        self.assertNotIn(EDITED_NOTE, narrative([clean]))
        self.assertNotIn(EDITED_NOTE, narrative([reject]))

        entry = vt.ui_entry(edited, 0)
        self.assertTrue(entry["fixed"])
        self.assertTrue(entry["text"].startswith(EDITED_NOTE))
        self.assertIn("unless the round limit was reached", entry["text"])
        self.assertTrue(vt.ui_entry(legacy, 0)["fixed"])
        self.assertEqual((vt.ui_entry(clean, 0)["fixed"], vt.ui_entry(clean, 0)["text"]),
                         (False, "No unresolved bugs."))
        self.assertEqual((vt.ui_entry(reject, 0)["fixed"], vt.ui_entry(reject, 0)["text"]),
                         (False, "Lemma 2 fails."))


class ExecCollabToolCallTests(unittest.TestCase):
    def test_every_exec_process_shows_its_subagent_assignments(self):
        records = [
            exec_event({"type": "thread.started", "thread_id": "t0"}),
            collab("item_0", "spawn_agent", prompt="Audit Lemma 2"),
            collab("item_0", "spawn_agent", prompt="Audit Lemma 2", completed=True),
            # The next critic round is a new codex exec process; ids restart.
            exec_event({"type": "thread.started", "thread_id": "t9"}),
            collab("item_0", "spawn_agent", prompt="Audit the base case"),
            # Parallel audits run separate processes with their own labels.
            exec_event({"type": "thread.started", "thread_id": "a1"}, label="Independent audit 1"),
            exec_event({"type": "thread.started", "thread_id": "a2"}, label="Independent audit 2"),
            collab("item_0", "spawn_agent", prompt="Check bounds", label="Independent audit 1"),
            collab("item_0", "spawn_agent", prompt="Check limits", label="Independent audit 2"),
        ]
        text = narrative(records)
        for prompt in ("Audit Lemma 2", "Audit the base case", "Check bounds", "Check limits"):
            self.assertEqual(text.count(f"Assignment: {prompt}"), 1, text)

        entry = vt.ui_entry(records[1], 10)
        self.assertEqual(entry["category"], "tool")
        self.assertIn("spawn_agent", entry["text"])
        self.assertIn("Assignment: Audit Lemma 2", entry["text"])
        self.assertIsNone(vt.ui_entry(records[2], 20))

    def test_completed_wait_shows_the_subagent_reports_once(self):
        states = {"t1": {"status": "completed", "message": "Lemma 2 swaps quantifiers."}}
        started = collab("item_1", "wait")
        finished = collab("item_1", "wait", completed=True, states=states)
        text = narrative([exec_event({"type": "thread.started"}), started, finished, finished])
        self.assertEqual(text.count("SUBAGENT REPORTS"), 1, text)
        self.assertIn("t1 (completed): Lemma 2 swaps quantifiers.", text)

        self.assertIsNone(vt.ui_entry(started, 0))
        entry = vt.ui_entry(finished, 10)
        self.assertEqual(entry["label"], "Subagent reports")
        self.assertIn("Lemma 2 swaps quantifiers.", entry["text"])
        # A wait that returns without a report is still polling noise.
        self.assertIsNone(vt.ui_entry(collab("item_2", "wait", completed=True, states={
            "t1": {"status": "running", "message": None}}), 20))


class NewestRunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        patcher = mock.patch.object(vt, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runs = self.root / "runs"
        self.runs.mkdir()

    def job(self, relative, modified, marker="job-settings.json", transcript=True):
        folder = self.runs / relative
        folder.mkdir(parents=True, exist_ok=True)
        (folder / marker).write_text("{}\n")
        path = folder / "transcript.jsonl"
        if transcript:
            path.write_text('{"kind": "status"}\n')
            os.utime(path, (modified, modified))
        return path.resolve()

    def test_direct_runs_only(self):
        self.job("a", 100)
        newest = self.job("b", 200)
        self.assertEqual(vt.transcript_path(), newest)

    def test_batch_folders_only(self):
        self.job("batch/x", 100)
        newest = self.job("batch/y", 300, marker="draft.md")
        self.assertEqual(vt.transcript_path(), newest)

    def test_mixed_direct_runs_and_batches(self):
        direct = self.job("direct", 400, marker="checked-statement.md")
        self.job("batch/y", 300)
        self.assertEqual(vt.transcript_path(), direct)
        newest = self.job("other-batch/z", 500)
        self.assertEqual(vt.transcript_path(), newest)

    def test_deleted_jobs_and_symlinks_are_ignored(self):
        newest = self.job("batch/x", 100)
        self.job(".trash/deleted", 900)
        self.job(".trash/batch/deleted", 900)
        outside = self.root / "elsewhere" / "transcript.jsonl"
        outside.parent.mkdir()
        outside.write_text('{"kind": "status"}\n')
        os.utime(outside, (900, 900))
        linked = self.runs / "batch" / "linked"
        linked.mkdir()
        (linked / "transcript.jsonl").symlink_to(outside)
        (self.runs / "linked-batch").symlink_to(self.root / "elsewhere", target_is_directory=True)
        self.assertEqual(vt.transcript_path(), newest)

    def test_never_descends_into_a_job_workspace(self):
        newest = self.job("run", 100)
        self.job("run/workspace", 900)
        self.job("draft-only", 0, marker="draft.md", transcript=False)
        self.job("draft-only/decoy", 900)
        self.assertEqual(vt.transcript_path(), newest)

    def test_missing_transcripts_still_report_an_error(self):
        self.job("draft-only", 0, marker="draft.md", transcript=False)
        with self.assertRaisesRegex(ValueError, "No transcript found"):
            vt.transcript_path()


class IncompleteMessageTests(unittest.TestCase):
    def test_incomplete_subagent_message_respects_root_only(self):
        records = [
            {"kind": "status", "stage": "solve", "label": "Started", "text": "Working."},
            delta("solve", "sub-1", "Partial subagent summary", root=False),
        ]
        self.assertNotIn("Partial subagent summary", narrative(records, root_only=True))
        self.assertIn("INCOMPLETE MODEL MESSAGE [subagent]", narrative(records))

    def test_incomplete_message_keeps_its_stage(self):
        records = [
            delta("solve", "msg-1", "Half-written proof"),
            {"kind": "critic_result", "stage": "critic", "label": "Critic round 1",
             "outcome": "reject", "report": {"verdict": "reject", "bugs": "Gap."}},
        ]
        filtered = narrative(records, stages=["critic"])
        self.assertIn("INDEPENDENT CRITIC", filtered)
        self.assertNotIn("Half-written proof", filtered)

        text = narrative(records)
        self.assertLess(text.rindex("PROOF AUTHOR"), text.index("INCOMPLETE MODEL MESSAGE"))
        self.assertNotIn("[--:--:--] INCOMPLETE", text)

    def test_completed_message_still_uses_streamed_text(self):
        records = [
            delta("solve", "msg-1", "Hello "),
            delta("solve", "msg-1", "world"),
            {"kind": "codex_event", "stage": "solve", "event": {
                "method": "item/completed",
                "params": {"item": {"id": "msg-1", "type": "agentMessage"}}}},
        ]
        text = narrative(records)
        self.assertIn("  Hello world\n", text)
        self.assertNotIn("INCOMPLETE", text)


class TranscriptFileTests(unittest.TestCase):
    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        self.path = directory / "transcript.jsonl"

    def age(self, seconds):
        moment = time.time() - seconds
        os.utime(self.path, (moment, moment))

    @staticmethod
    def line(record):
        return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")

    def test_follow_waits_for_a_partially_written_line(self):
        first = self.line({"kind": "status", "text": "one"})
        second = self.line({"kind": "status", "text": "x ≥ y"})
        split = second.index("≥".encode("utf-8")) + 1  # inside the character
        self.path.write_bytes(first + second[:split])
        pauses = []

        def finish_line(seconds):
            pauses.append(seconds)
            if len(pauses) > 1:
                raise AssertionError("the reader kept waiting after the line was finished")
            with self.path.open("ab") as stream:
                stream.write(second[split:])

        with mock.patch.object(vt, "time", types.SimpleNamespace(sleep=finish_line)):
            lines = vt.records(self.path, follow=True)
            self.assertEqual(next(lines), (1, {"kind": "status", "text": "one"}, None))
            self.assertEqual(next(lines), (2, {"kind": "status", "text": "x ≥ y"}, None))
            lines.close()
        self.assertEqual(len(pauses), 1)

    def test_without_follow_an_unterminated_final_line_is_still_read(self):
        self.path.write_bytes(self.line({"kind": "status"}) + b'{"kind": "final_result"}')
        self.assertEqual(list(vt.records(self.path)), [
            (1, {"kind": "status"}, None), (2, {"kind": "final_result"}, None),
        ])

    def test_browser_shows_a_complete_final_record_without_newline(self):
        first = self.line({"kind": "status", "stage": "solve", "text": "Started"})
        last = json.dumps({"kind": "final_result", "stage": "final", "output": "QED"})
        self.path.write_bytes(first + last.encode("utf-8"))
        self.age(0)
        batch = vt.read_ui_batch(self.path)
        self.assertEqual([entry["category"] for entry in batch["entries"]], ["status", "result"])
        self.assertTrue(batch["eof"])
        self.assertEqual(batch["nextOffset"], self.path.stat().st_size)

        # The writer's newline may arrive after the record was accepted.
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        later = vt.read_ui_batch(self.path, batch["nextOffset"])
        self.assertEqual((later["entries"], later["malformed"], later["eof"]), ([], 0, True))

    def test_browser_waits_for_a_fresh_partial_tail_then_reports_a_stale_one(self):
        first = self.line({"kind": "status", "stage": "solve", "text": "Started"})
        self.path.write_bytes(first + b'{"kind": "final_result", "out')
        self.age(0)
        batch = vt.read_ui_batch(self.path)
        self.assertEqual(len(batch["entries"]), 1)
        self.assertEqual((batch["nextOffset"], batch["eof"]), (len(first), False))

        self.age(60)
        for _ in range(2):
            stale = vt.read_ui_batch(self.path, batch["nextOffset"])
            self.assertEqual([entry["id"] for entry in stale["entries"]], [f"{len(first)}:incomplete"])
            self.assertEqual(stale["entries"][0]["category"], "diagnostic")
            self.assertEqual((stale["nextOffset"], stale["eof"]), (len(first), True))

    def test_browser_does_not_report_a_tail_started_after_its_quiet_check(self):
        first = self.line({"kind": "status", "stage": "solve", "text": "Started"})
        last = self.line({"kind": "final_result", "stage": "final", "output": "QED"})
        split = len(last) // 2
        self.path.write_bytes(first)
        self.age(60)
        real_stat, stats = Path.stat, []

        def stat_then_resume_writing(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            stats.append(info)
            if len(stats) == 1:
                # The quiet writer starts a record right after the staleness check.
                with path.open("ab") as stream:
                    stream.write(last[:split])
            return info

        with mock.patch.object(Path, "stat", stat_then_resume_writing):
            batch = vt.read_ui_batch(self.path)
        self.assertEqual([entry["id"] for entry in batch["entries"]], ["0"])
        self.assertEqual((batch["nextOffset"], batch["eof"]), (len(first), False))

        with self.path.open("ab") as stream:
            stream.write(last[split:])
        later = vt.read_ui_batch(self.path, batch["nextOffset"])
        self.assertEqual([entry["id"] for entry in later["entries"]], [str(len(first))])
        self.assertEqual(later["entries"][0]["category"], "result")
        self.assertTrue(later["eof"])

    def test_browser_skips_blank_lines(self):
        first = self.line({"kind": "status", "stage": "solve", "text": "One"})
        second = self.line({"kind": "status", "stage": "solve", "text": "Two"})
        self.path.write_bytes(first + b"\n" + b"\r\n" + second)
        batch = vt.read_ui_batch(self.path)
        self.assertEqual([entry["text"] for entry in batch["entries"]], ["One", "Two"])
        self.assertEqual((batch["malformed"], batch["eof"]), (0, True))


class OutputStreamTests(unittest.TestCase):
    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        self.path = directory / "transcript.jsonl"
        self.path.write_text(json.dumps({
            "kind": "author_result", "stage": "solve", "label": "Proof found",
            "text": "Therefore x ≥ y.", "time": "2026-10-21T12:00:00+00:00",
        }, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_ui_url_is_flushed_before_serving(self):
        stream, observed = RecordingStream(), []

        class Server:
            server_port = 4321

            def __init__(self, _address, _transcript, _token):
                pass

            def serve_forever(self, poll_interval):
                observed.append(stream.flushed)
                raise KeyboardInterrupt

            def server_close(self):
                pass

        with mock.patch.object(vt, "TranscriptHTTPServer", Server), \
                mock.patch("sys.stdout", stream):
            self.assertEqual(vt.serve_ui(self.path, open_browser=False), 0)
        self.assertIn("Transcript UI: http://127.0.0.1:4321/#", observed[0])

    def test_follow_output_is_flushed_before_waiting(self):
        stream, observed = RecordingStream(), []

        def stop(_seconds):
            observed.append(stream.flushed)
            raise KeyboardInterrupt

        with mock.patch.object(vt, "time", types.SimpleNamespace(sleep=stop)), \
                mock.patch("sys.stdout", stream), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(vt.main([str(self.path), "--text", "--follow"]), 0)
        self.assertIn("TCS Prover transcript", observed[0])
        self.assertIn("Therefore x ≥ y.", observed[0])
        self.assertIn("Stopped following transcript.", stream.getvalue())

    def test_text_mode_survives_a_narrow_stdout_encoding(self):
        output, errors = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
        stderr = io.TextIOWrapper(errors, encoding="cp1252", errors="strict")
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = vt.main([str(self.path), "--text"])
            stdout.flush()
            stderr.flush()
        self.assertEqual(code, 0, errors.getvalue())
        text = output.getvalue().decode("utf-8")
        self.assertIn("Therefore x ≥ y.", text)
        self.assertIn("Read 1 records; displayed 1 entries", text)


BROWSER_HARNESS = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const app = fs.readFileSync(process.argv[1], 'utf8');
const prelude = String.raw`"use strict";
class Element {
  constructor() {
    this.hidden = false; this.disabled = false; this.textContent = ''; this.className = '';
    this.children = []; this.dataset = {}; this.style = {}; this.checked = false; this.value = '';
    this.classList = {add() {}, remove() {}, toggle() {}}; this.parts = {};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelector(selector) { return this.parts[selector] ||= new Element(); }
  querySelectorAll() { return []; }
  cloneNode() { return new Element(); }
  closest() { return null; }
}
const elements = {entryTemplate: new Element(), loadMore: new Element()};
elements.entryTemplate.content = {firstElementChild: new Element()};
elements.loadMore.hidden = true;
const document = {
  getElementById: (id) => elements[id] ||= new Element(),
  createElement: () => new Element(), createDocumentFragment: () => new Element(),
  createTextNode: (text) => ({text}), body: {scrollHeight: 0},
};
const window = {scrollY: 0, scrollTo() {}};
const location = {hash: '#token', pathname: '/'};
const history = {replaceState() {}};
const sessionStorage = {getItem: () => null, setItem() {}};
const timers = new Map();
let timerId = 0;
function setTimeout(callback, delay) {
  if (!delay) { Promise.resolve().then(callback); return 0; }
  timers.set(++timerId, {callback, delay});
  return timerId;
}
function clearTimeout(id) { timers.delete(id); }
let server = () => { throw new Error('No fake server response was configured.'); };
const requests = [];
async function fetch(path) {
  requests.push({path, hidden: elements.loadMore.hidden});
  await null;
  const reply = server(path);
  if (reply instanceof Error) throw reply;
  const status = reply.status || 200;
  return {ok: status < 400, status, json: async () => reply.body};
}
const settle = () => new Promise((resolve) => tick(resolve));
const pendingDelay = () => { assert.equal(timers.size, 1); return [...timers.values()][0].delay; };
async function firePoll() {
  assert.equal(timers.size, 1, 'exactly one live check is scheduled');
  const [[id, timer]] = [...timers.entries()];
  timers.delete(id);
  timer.callback();
  await settle();
}
const meta = {name: 'run', path: '/runs/run/transcript.jsonl', size: 100,
  startedAt: '2026-10-21T12:00:00Z', stages: {solve: 'PROOF AUTHOR'}};
const offsetOf = (path) => Number(/offset=(\d+)/.exec(path)[1]);
const makeEntry = (id) => ({id: String(id), stage: 'solve', stageLabel: 'PROOF AUTHOR',
  time: '2026-10-21T12:00:01Z', root: true, category: 'status', label: 'Status', text: 'Entry ' + id});
const batch = (entries, nextOffset, fileSize, eof) =>
  ({body: {entries, nextOffset, fileSize, eof, scannedBytes: 0, malformed: 0}});
`;
const result = vm.runInNewContext(
  prelude + app + '\n(async () => {' + process.argv[2] + '})()',
  {assert, tick: setImmediate, console});
Promise.resolve(result).catch((error) => { console.error(error); process.exitCode = 1; });
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class TranscriptBrowserTests(unittest.TestCase):
    def browser(self, checks):
        result = subprocess.run(
            [shutil.which("node"), "-e", BROWSER_HARNESS,
             str(ROOT / "transcript" / "transcript_ui" / "app.js"), checks],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_idle_live_checks_leave_no_pause_button(self):
        self.browser(r"""
server = (path) => path === '/api/meta' ? {body: meta}
  : offsetOf(path) === 0 ? batch([makeEntry(0)], 100, 100, true) : batch([], 100, 100, true);
await settle();
assert.equal(state.entries.length, 1);
assert.equal(ui.loadMore.hidden, true);
assert.equal(ui.loadState.textContent, 'Complete transcript loaded · watching for updates');
for (let poll = 0; poll < 3; poll += 1) {
  const before = requests.length;
  await firePoll();
  assert.ok(requests.length > before, 'live checks keep polling');
  assert.ok(requests.slice(before).every((request) => request.hidden), 'live checks do not flash the button');
  assert.equal(ui.loadMore.hidden, true);
  assert.equal(ui.loadState.textContent, 'Complete transcript loaded · watching for updates');
  assert.equal(pendingDelay(), 2000);
}
""")

    def test_a_backlog_found_by_a_live_check_can_still_be_paused(self):
        self.browser(r"""
let backlog = false;
server = (path) => {
  if (path === '/api/meta') return {body: meta};
  const offset = offsetOf(path);
  if (offset === 0) return batch([makeEntry(0)], 100, 100, true);
  if (!backlog) return batch([], 100, 100, true);
  if (offset === 100) return batch([makeEntry(100)], 200, 400, false);
  assert.equal(ui.loadMore.hidden, false);
  assert.equal(ui.loadMore.textContent, 'Pause full-file loading');
  ui.loadMore.onclick();
  assert.equal(ui.loadMore.textContent, 'Pausing…');
  assert.equal(ui.loadMore.disabled, true);
  return batch([makeEntry(200)], 300, 400, false);
};
await settle();
backlog = true;
await firePoll();
assert.equal(state.entries.length, 3);
assert.equal(state.autoLoad, false);
assert.equal(ui.loadState.textContent, 'Full-file loading paused');
assert.equal(ui.loadMore.hidden, false);
assert.equal(ui.loadMore.textContent, 'Resume full-file loading');
const before = requests.length;
await firePoll();
assert.equal(requests.length, before, 'a paused viewer sends no live checks');
""")

    def test_failed_live_checks_back_off_retry_and_recover(self):
        self.browser(r"""
let mode = 'ok', size = 100;
server = (path) => {
  if (path === '/api/meta') return {body: meta};
  if (mode === 'down') return new TypeError('Failed to fetch');
  if (mode === 'missing') return {status: 400, body: {error: 'Transcript does not exist: x'}};
  const offset = offsetOf(path);
  if (offset === 0) return batch([makeEntry(0)], 100, 100, true);
  return offset < size ? batch([makeEntry(offset)], size, size, true) : batch([], size, size, true);
};
await settle();
mode = 'down';
await firePoll();
assert.equal(ui.loadState.textContent, 'Failed to fetch · retrying…');
assert.equal(ui.liveDot.className, 'live-dot error');
assert.equal(ui.loadMore.hidden, false);
assert.equal(ui.loadMore.textContent, 'Retry now');
assert.equal(state.autoLoad, true);
assert.equal(pendingDelay(), 4000);
mode = 'missing';
await firePoll();
assert.equal(ui.loadState.textContent, 'Transcript does not exist: x · retrying…');
assert.equal(pendingDelay(), 8000);
for (let poll = 0; poll < 4; poll += 1) await firePoll();
assert.equal(pendingDelay(), 30000);
mode = 'ok'; size = 180;
const before = requests.length;
await firePoll();
assert.ok(requests.length > before, 'a failing viewer keeps checking');
assert.equal(state.entries.length, 2, 'records written during the outage still load');
assert.equal(ui.loadState.textContent, 'Complete transcript loaded · watching for updates');
assert.equal(ui.liveDot.className, 'live-dot ready');
assert.equal(ui.loadMore.hidden, true);
assert.equal(pendingDelay(), 2000);
""")

    def test_retry_now_reloads_and_restores_the_normal_cadence(self):
        self.browser(r"""
let down = false, size = 100;
server = (path) => {
  if (path === '/api/meta') return {body: meta};
  if (down) return new TypeError('Failed to fetch');
  const offset = offsetOf(path);
  if (offset === 0) return batch([makeEntry(0)], 100, 100, true);
  return offset < size ? batch([makeEntry(offset)], size, size, true) : batch([], size, size, true);
};
await settle();
down = true;
await firePoll();
await firePoll();
assert.equal(pendingDelay(), 8000);
assert.equal(ui.loadMore.textContent, 'Retry now');
down = false; size = 150;
ui.loadMore.onclick();
await settle();
assert.equal(state.entries.length, 2);
assert.equal(ui.loadState.textContent, 'Complete transcript loaded · watching for updates');
assert.equal(ui.loadMore.hidden, true);
assert.equal(pendingDelay(), 2000);
""")

    def test_a_stale_token_stops_live_checks_with_guidance(self):
        self.browser(r"""
let forbidden = false;
server = (path) => {
  if (forbidden) return {status: 403, body: {error: 'Unauthorized local request.'}};
  if (path === '/api/meta') return {body: meta};
  return offsetOf(path) === 0 ? batch([makeEntry(0)], 100, 100, true) : batch([], 100, 100, true);
};
await settle();
forbidden = true;
await firePoll();
assert.equal(ui.loadState.textContent, 'Reopen the viewer link printed in the terminal.');
assert.equal(state.autoLoad, false);
const before = requests.length;
await firePoll();
assert.equal(requests.length, before);
""")

    def test_a_stale_token_at_startup_shows_guidance(self):
        self.browser(r"""
server = () => ({status: 403, body: {error: 'Unauthorized local request.'}});
await settle();
assert.equal(ui.loadState.textContent, 'Reopen the viewer link printed in the terminal.');
assert.equal(ui.loadMore.hidden, true);
""")

    def test_an_unfinished_last_line_waits_then_completes_at_eof(self):
        self.browser(r"""
let stale = false;
server = (path) => {
  if (path === '/api/meta') return {body: {...meta, size: 150}};
  const offset = offsetOf(path);
  if (offset === 0) return batch([makeEntry(0)], 100, 150, false);
  if (!stale) return batch([], 100, 150, false);
  return batch([{...makeEntry('100:incomplete'), category: 'diagnostic'}], 100, 150, true);
};
await settle();
assert.equal(ui.loadState.textContent, 'Waiting for the last transcript line…');
assert.match(ui.scanProgress.textContent, /66\.7%$/);
assert.equal(ui.loadMore.hidden, true);
stale = true;
await firePoll();
assert.equal(ui.loadState.textContent, 'Complete transcript loaded · watching for updates');
assert.equal(ui.scanProgress.textContent, '150 B of 150 B read · 100.0%');
const before = requests.length;
await firePoll();
assert.equal(requests.length - before, 1, 'an EOF transcript needs one request per check');
assert.equal(state.entries.filter((entry) => entry.id === '100:incomplete').length, 1);
""")


if __name__ == "__main__":
    unittest.main()
