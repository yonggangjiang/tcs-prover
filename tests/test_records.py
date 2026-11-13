"""The record of a job's model calls (runner/records.py): the store, Codex trace ingest, Claude Code streams, session
copies and snapshots, and what ui/model_calls.py reads from them.

The trace bundles here are written the way Codex 0.160 writes them (codex-rs/rollout-trace: writer.rs writes each
payload with serde_json's pretty printer before the event that names it, events one JSON line each); the record
audit's captures of the real Codex (E3, E4) round-trip the same way.
"""

import gzip
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers, records
from ui import model_calls

THREAD = "01a1d4e2-53df-78e1-b1a3-60906592b723"
CHILD = "01a1d4e2-88a9-7c32-81e2-b3e1248ae5e7"
TOOLS = {"type": "additional_tools", "id": "at_1", "role": "developer",
         "tools": [{"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"}]}]}
SYSTEM = {"type": "message", "id": "msg_dev", "role": "developer",
          "content": [{"type": "input_text", "text": "You are Codex. " * 40}]}
PROMPT = {"type": "message", "id": "msg_user", "role": "user", "content": [{"type": "input_text", "text": "Review."}]}
REASONING = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "**Plan** step"}],
             "encrypted_content": "ENC-gAAAAABfake"}
CELL = {"type": "custom_tool_call", "id": "ctc_1", "status": "completed", "call_id": "call-1", "name": "exec",
        "input": "const r = await tools.web__run({search_query: [{q: \"q\"}]});\ntext(r);\n"}
CELL_OUTPUT = {"type": "custom_tool_call_output", "id": "ctco_1", "call_id": "call-1",
               "output": [{"type": "input_text", "text": "SEARCHOUT the text the model read"}]}
ANSWER = {"type": "message", "id": "msg_2", "role": "assistant",
          "content": [{"type": "output_text", "text": "{\"verdict\": \"pass\"}"}]}
USAGE = {"input_tokens": 1001, "cached_input_tokens": 100, "cache_write_input_tokens": 0, "output_tokens": 201,
         "reasoning_output_tokens": 151, "total_tokens": 1202}


def request(items, **extra):
    return {"model": "gpt-6.1-sol", "input": items, "tool_choice": "auto", "parallel_tool_calls": False,
            "reasoning": {"effort": "high", "summary": "concise", "context": "all_turns"}, "store": False,
            "stream": True, "include": ["reasoning.encrypted_content"], "prompt_cache_key": THREAD, **extra}


class Bundle:
    """A trace bundle written as Codex 0.160's TraceWriter writes one."""

    def __init__(self, root, thread=THREAD, schema=1):
        self.folder = Path(root) / f"trace-11111111-2222-3333-4444-555555555555-{thread}"
        (self.folder / "payloads").mkdir(parents=True)
        self.thread, self.seq, self.ordinal, self.time = thread, 0, 0, 1794572964846
        (self.folder / "manifest.json").write_text(json.dumps(
            {"schema_version": schema, "trace_id": "t", "rollout_id": thread, "root_thread_id": thread,
             "started_at_unix_ms": self.time, "raw_event_log": "trace.jsonl", "payloads_dir": "payloads"},
            indent=2), encoding="utf-8")
        self.schema = schema
        self.files = {}

    def payload(self, kind, value=None, text=None):
        self.ordinal += 1
        path = f"payloads/{self.ordinal}.json"
        data = text.encode() if text is not None else json.dumps(value, ensure_ascii=False, indent=2).encode()
        (self.folder / path).write_bytes(data)
        self.files[path] = data
        return {"raw_payload_id": f"raw_payload:{self.ordinal}", "kind": {"type": kind}, "path": path}

    def event(self, payload, thread=None):
        self.seq += 1
        self.time += 5
        with open(self.folder / "trace.jsonl", "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"schema_version": self.schema, "seq": self.seq, "wall_time_unix_ms": self.time,
                                     "rollout_id": self.thread, "thread_id": thread, "codex_turn_id": "turn-1",
                                     "payload": payload}) + "\n")

    def thread_started(self, thread=None, path="/root"):
        meta = self.payload("session_metadata", {"thread_id": thread or self.thread, "agent_path": path})
        self.event({"type": "thread_started", "thread_id": thread or self.thread, "agent_path": path,
                    "metadata_payload": meta})

    def call(self, identity, body, response=None, thread=None, status="completed", text=None):
        thread = thread or self.thread
        reference = self.payload("inference_request", body, text=text)
        self.event({"type": "inference_started", "inference_call_id": identity, "thread_id": thread,
                    "codex_turn_id": "turn-1", "model": "gpt-6.1-sol", "provider_name": "OpenAI",
                    "request_payload": reference}, thread)
        if status == "interrupted":
            return
        if status == "failed":
            self.event({"type": "inference_failed", "inference_call_id": identity, "upstream_request_id": None,
                        "error": "stream closed before response.completed", "partial_response_payload": None},
                       thread)
            return
        answer = self.payload("inference_response", {"response_id": f"resp_{identity}",
                                                     "upstream_request_id": f"req_{identity}",
                                                     "token_usage": USAGE, "output_items": response})
        self.event({"type": "inference_completed", "inference_call_id": identity, "response_id": f"resp_{identity}",
                    "upstream_request_id": f"req_{identity}", "response_payload": answer}, thread)

    def cell(self, call_id, tool_result):
        self.event({"type": "code_cell_started", "runtime_cell_id": "1", "model_visible_call_id": call_id,
                    "source_js": CELL["input"]})
        invocation = self.payload("tool_invocation", {"tool_name": "run", "tool_namespace": "web",
                                                      "payload": {"type": "function", "arguments": "{}"}})
        self.event({"type": "tool_call_started", "tool_call_id": "exec-1", "model_visible_call_id": None,
                    "code_mode_runtime_tool_id": "tool-1", "requester": {"type": "code_cell", "runtime_cell_id": "1"},
                    "kind": {"type": "other", "name": "run"},
                    "summary": {"type": "generic", "label": "web.run", "input_preview": "q"},
                    "invocation_payload": invocation})
        result = self.payload("tool_result", {"type": "code_mode_response", "value": tool_result})
        self.event({"type": "tool_call_ended", "tool_call_id": "exec-1", "status": "completed",
                    "result_payload": result})
        self.event({"type": "code_cell_ended", "runtime_cell_id": "1", "status": "completed",
                    "response_payload": self.payload("tool_result", {"response": {"Result": {"cell_id": "1"}}})})

    def standard(self):
        """A critic's call that runs one code cell (a search), then answers: two requests."""

        self.event({"type": "rollout_started", "trace_id": "t", "root_thread_id": self.thread})
        self.thread_started()
        self.call("call-a", request([TOOLS, SYSTEM, PROMPT]), [REASONING, CELL])
        self.cell("call-1", "SEARCHOUT the text the model read")
        self.call("call-b", request([TOOLS, SYSTEM, PROMPT, REASONING, CELL, CELL_OUTPUT]), [REASONING, ANSWER])
        self.event({"type": "rollout_ended", "status": "completed"})
        return self


class RecordCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        change = mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.root / "state" / "api-models.json")})
        change.start()
        self.addCleanup(change.stop)
        self.record = records.Record.create(records.record_folder("run-1"), title="A statement")
        self.trace = self.record.folder / "codex-trace" / "20261113T000000-critic-abcdef"
        self.trace.mkdir(parents=True)
        records.write_atomic(self.record.folder / "codex" / self.trace.name / "process.json",
                             json.dumps({"role": "critic", "label": "Critic coordinator", "round": 1}).encode())

    def ingest(self, bundle, final=True):
        ingest = records.TraceIngest(self.record, self.trace.name, bundle.folder)
        ingest.step(final=final)
        return ingest

    def stored(self, bundle):
        return records.StoredBundle(self.record, self.trace.name, bundle.folder.name).load()


class TraceIngestTests(RecordCase):
    def test_a_trace_is_stored_byte_for_byte_and_its_raw_files_go(self):
        bundle = Bundle(self.trace).standard()
        manifest, trace = (bundle.folder / "manifest.json").read_bytes(), (bundle.folder / "trace.jsonl").read_bytes()
        self.ingest(bundle)
        self.assertFalse(bundle.folder.exists())
        stored = self.stored(bundle)
        for path, data in bundle.files.items():
            self.assertEqual(stored.payload(path), data, path)
        self.assertEqual(stored.copy("manifest.json"), manifest)
        self.assertEqual(stored.copy("trace.jsonl"), trace)
        calls = self.record.calls.read()
        self.assertEqual([(call["callId"], call["status"], call["usage"]) for call in calls],
                         [("call-a", "completed", USAGE), ("call-b", "completed", USAGE)])
        # The second request is stored as the first one's input plus what it added; every item once.
        second = stored.entries[calls[1]["request"]]
        self.assertEqual(second["input"]["keep"], 3)
        self.assertEqual(len(second["input"]["add"]), 3)
        self.assertEqual(len([sha for sha in self.record.objects.names()
                              if json.loads(self.record.objects.get(sha)) == SYSTEM]), 1)

    def test_a_payload_whose_rebuild_would_differ_is_kept_whole(self):
        # serde_json writes 1e-7 where Python writes 1e-07: such a payload is kept as its exact bytes.
        bundle = Bundle(self.trace)
        bundle.thread_started()
        text = json.dumps(request([PROMPT]), indent=2).replace('"stream": true', '"temperature": 1e-7')
        bundle.call("call-a", None, [ANSWER], text=text)
        self.ingest(bundle)
        stored = self.stored(bundle)
        entry = stored.entries["payloads/2.json"]
        self.assertEqual(entry["store"], {"object": records._sha(text.encode())})
        self.assertFalse(entry["exact"])
        self.assertEqual(stored.payload("payloads/2.json"), text.encode())
        self.assertFalse(bundle.folder.exists())

    def test_raw_files_stay_until_their_copy_reads_back(self):
        bundle = Bundle(self.trace).standard()
        with mock.patch.object(records, "rebuild_payload", lambda *args, **kwargs: b"not the payload"):
            self.ingest(bundle)
        # Nothing the record could not give back byte for byte was removed.
        self.assertTrue(bundle.folder.is_dir())
        for path in bundle.files:
            self.assertTrue((bundle.folder / path).is_file(), path)

    def test_a_request_the_process_never_ended_is_interrupted(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        bundle.call("call-b", request([PROMPT, ANSWER]), status="interrupted")
        ingest = self.ingest(bundle, final=False)
        self.assertEqual([call["status"] for call in self.record.calls.read()], ["completed"])
        ingest.step(final=True)
        calls = self.record.calls.read()
        self.assertEqual([(call["callId"], call["status"], call["response"]) for call in calls],
                         [("call-a", "completed", "payloads/3.json"), ("call-b", "interrupted", None)])
        self.assertEqual(self.stored(bundle).payload(calls[1]["request"]), bundle.files[calls[1]["request"]])

    def test_a_failed_request_is_recorded_as_failed(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), status="failed")
        self.ingest(bundle)
        call = self.record.calls.read()[0]
        self.assertEqual((call["status"], call["error"]), ("failed", "stream closed before response.completed"))

    def test_an_unknown_trace_format_keeps_its_raw_files(self):
        bundle = Bundle(self.trace, schema=2)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        self.ingest(bundle)
        self.assertTrue((bundle.folder / "payloads" / "2.json").is_file())
        calls = self.record.calls.read()
        self.assertEqual([(call["callId"], call["status"]) for call in calls],
                         [("call-a", "trace format not recognized")])
        listing = model_calls.call_list("run-1")
        self.assertEqual(listing["calls"][0]["placeholderKinds"], ["trace_format_not_recognized"])
        self.assertIn("their format is not recognized", " ".join(listing["recordState"]["problems"]))

    def test_a_manifest_of_another_schema_version_alone_keeps_the_bundle_raw(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        manifest = json.loads((bundle.folder / "manifest.json").read_text())
        (bundle.folder / "manifest.json").write_text(json.dumps({**manifest, "schema_version": 2}, indent=2))
        self.ingest(bundle)
        self.assertTrue((bundle.folder / "payloads" / "2.json").is_file())
        self.assertEqual([call["status"] for call in self.record.calls.read()], ["trace format not recognized"])

    def test_a_running_process_is_stored_as_it_goes(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        ingest = self.ingest(bundle, final=False)
        self.assertEqual(len(self.record.calls.read()), 1)
        # A stored payload's file goes at once; the bundle stays while the process runs.
        self.assertFalse((bundle.folder / "payloads" / "2.json").exists())
        self.assertTrue((bundle.folder / "trace.jsonl").is_file())
        bundle.call("call-b", request([PROMPT, ANSWER, PROMPT]), [ANSWER])
        ingest.step()
        self.assertEqual([call["callId"] for call in self.record.calls.read()], ["call-a", "call-b"])

    def test_leftovers_nobody_holds_are_stored_and_a_held_one_is_not(self):
        held = self.record.folder / "codex-trace" / "20261113T000001-author-123456"
        Bundle(held).standard()
        claim = records.FileLock(held / ".claim")
        claim.acquire()
        Bundle(self.trace).standard()
        # A bundle written to just now may still be written by a Codex process whose runner died.
        records.store_leftovers(self.record)
        self.assertEqual(self.record.calls.read(), [])
        old = os.stat(self.trace.parent).st_mtime - 3600
        for path in (*held.rglob("trace.jsonl"), *self.trace.rglob("trace.jsonl")):
            os.utime(path, (old, old))
        records.store_leftovers(self.record)
        self.assertFalse(self.trace.exists())
        self.assertEqual(len(self.record.calls.read()), 2)
        self.assertTrue(held.is_dir())  # Its runner still works.
        claim.release()
        records.store_leftovers(self.record)
        self.assertFalse(held.exists())
        self.assertEqual(len(self.record.calls.read()), 4)
        # A second pass stores nothing twice.
        records.store_leftovers(self.record)
        self.assertEqual(len(self.record.calls.read()), 4)

    def test_a_subagent_thread_in_the_bundle_is_its_own_row(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.thread_started(CHILD, "/root/critic_one")
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        bundle.call("call-c", request([PROMPT]), [ANSWER], thread=CHILD)
        self.ingest(bundle)
        rows = model_calls.call_list("run-1")["calls"]
        self.assertEqual([(row["role"], row["member"]) for row in rows],
                         [("critic", None), ("independent critic", "/root/critic_one")])


class ModelCallViewTests(RecordCase):
    def setUp(self):
        super().setUp()
        self.bundle = Bundle(self.trace).standard()
        self.ingest(self.bundle)

    def test_the_list_names_each_call_with_its_tokens_and_placeholders(self):
        listing = model_calls.call_list("run-1")
        self.assertEqual(listing["total"], 2)
        self.assertEqual(listing["recordState"], {"available": True, "problems": []})
        row = listing["calls"][0]
        self.assertEqual({key: row[key] for key in ("id", "role", "label", "member", "model", "program", "status")},
                         {"id": "c000001", "role": "critic", "label": "Critic coordinator · round 1", "member": None,
                          "model": "gpt-6.1-sol", "program": "codex", "status": "completed"})
        self.assertEqual(row["tokens"], {"input": 1001, "cached": 100, "output": 201, "reasoning": 151})
        self.assertEqual(row["durationMs"], 5)
        self.assertEqual(row["placeholderKinds"], ["hidden_reasoning", "token_probabilities", "network_only"])
        self.assertEqual(model_calls.call_list("run-1", 1, 1)["calls"][0]["id"], "c000002")
        with self.assertRaises(model_calls.NotFound):
            model_calls.call_list("no-such-run")
        with self.assertRaises(ValueError):
            model_calls.call_list("run-1", 0, 0)

    def test_one_call_shows_what_was_sent_new_and_what_came_back(self):
        detail = model_calls.call_detail("run-1", "c000002")
        self.assertEqual(detail["sent"]["previousCallId"], "c000001")
        self.assertEqual([view["item"] for view in detail["sent"]["newItems"]], [CELL_OUTPUT])
        self.assertEqual(detail["sent"]["tools"], TOOLS["tools"])
        self.assertEqual([view["item"] for view in detail["received"]["items"]], [REASONING, ANSWER])
        self.assertEqual(detail["usage"], USAGE)
        hidden = next(item for item in detail["placeholders"] if item["kind"] == "hidden_reasoning")
        self.assertEqual(hidden["facts"], {"reasoningTokens": 151, "encryptedCopyKept": True})
        self.assertEqual(hidden["withheldBy"], "OpenAI")
        self.assertTrue(detail["rawAvailable"])
        first = model_calls.call_detail("run-1", "c000001")
        cell = first["received"]["items"][1]
        # The code cell: its output from the next request, and the search text its script read.
        self.assertEqual(cell["output"], CELL_OUTPUT)
        self.assertEqual(cell["nestedToolCalls"][0]["tool"], "web.run")
        self.assertEqual(cell["nestedToolCalls"][0]["output"]["value"], "SEARCHOUT the text the model read")

    def test_raw_gives_the_exact_request_and_response(self):
        raw = model_calls.call_raw("run-1", "c000001")
        self.assertEqual(raw["format"], "codex-trace")
        self.assertEqual(raw["request"].encode(), self.bundle.files["payloads/2.json"])
        self.assertEqual(raw["response"].encode(), self.bundle.files["payloads/3.json"])

    def test_the_training_record_holds_everything_without_replacing_anything(self):
        buffer = io.BytesIO()
        model_calls.training_record("run-1", buffer)
        archive = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        names = archive.namelist()
        for name in ("requests.jsonl", "items.jsonl", "trajectories.jsonl", "rounds.jsonl", "manifest.json"):
            self.assertIn(name, names)
        requests = [json.loads(line) for line in archive.read("requests.jsonl").splitlines()]
        self.assertEqual(requests[1]["request"], json.loads(self.bundle.files["payloads/7.json"]))
        self.assertEqual(requests[1]["response"]["output_items"], [REASONING, ANSWER])
        items = [json.loads(line) for line in archive.read("items.jsonl").splitlines()]
        self.assertIn(REASONING, [item["item"] for item in items])  # Encrypted reasoning as received.
        self.assertNotIn("transcript/transcript.jsonl", names)  # No snapshot yet: no empty member.
        windows = [json.loads(line) for line in archive.read("trajectories.jsonl").splitlines()]
        self.assertEqual(len(windows), 1)
        self.assertEqual([call["inputEnd"] for call in windows[0]["calls"]], [3, 6])
        self.assertEqual(len(windows[0]["items"]), 8)

    def test_records_are_listed_and_deleted(self):
        rows = model_calls.record_list(lambda name: name == "run-1")
        self.assertEqual([(row["runFolder"], row["title"], row["jobExists"]) for row in rows],
                         [("run-1", "A statement", True)])
        self.assertGreater(rows[0]["sizeBytes"], 0)
        self.assertEqual(model_calls.delete_record("run-1"), {"deleted": "run-1"})
        self.assertFalse(self.record.folder.exists())
        for name in ("..", "a/b", ".hidden", ""):
            with self.assertRaises(model_calls.NotFound):
                model_calls.delete_record(name)


class UntracedCompactionTests(RecordCase):
    def test_a_local_compaction_codex_leaves_untraced_is_a_row_with_its_summary(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        self.ingest(bundle)
        home = self.root / "codex-home"
        rollout(home / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl",
                {"id": THREAD, "session_id": THREAD}, [
                    {"timestamp": "2099-01-01T00:00:00.000Z", "type": "compacted",
                     "payload": {"message": "HANDOFF SUMMARY of the work so far", "replacement_history": []}},
                    # A remote compaction (OpenAI's) is traced, and its message is empty.
                    {"timestamp": "2099-01-01T00:00:01.000Z", "type": "compacted",
                     "payload": {"message": "", "replacement_history": [{"type": "compaction"}]}}])
        self.assertTrue(records.Recorder(self.record.folder).keep_codex_session(home, THREAD))
        rows = model_calls.call_list("run-1")["calls"]
        self.assertEqual([(row["kind"], row["status"], row["placeholderKinds"]) for row in rows],
                         [("request", "completed", ["hidden_reasoning", "token_probabilities", "network_only"]),
                          ("compaction", "not recorded", ["compaction_not_traced"])])
        detail = model_calls.call_detail("run-1", rows[1]["id"])
        self.assertEqual(detail["received"]["items"][0]["item"]["message"], "HANDOFF SUMMARY of the work so far")
        self.assertEqual(detail["placeholders"][0]["withheldBy"], "Codex")
        self.assertEqual(model_calls.call_list("run-1")["calls"][1]["id"], rows[1]["id"])  # Stable.


class RoundTests(unittest.TestCase):
    def test_each_critic_round_has_the_proof_it_received_and_its_convention_result(self):
        transcript = [
            {"kind": "status", "label": "Workflow step: critic", "node": "critic", "visit": 1},
            {"kind": "request", "stage": "critic", "label": "Critic coordinator", "candidateText": "PROOF 1"},
            {"kind": "critic_result", "round": 1, "outcome": "reject",
             "report": {"verdict": "reject", "bugs": "B", "fixes": "", "solution": "PROOF 1"}},
            {"kind": "status", "label": "Workflow step: critic", "node": "critic", "visit": 2},
            {"kind": "request", "stage": "critic", "label": "Critic coordinator", "candidateText": "PROOF 2"},
            {"kind": "critic_result", "round": 2, "outcome": "accept",
             "report": {"verdict": "pass", "bugs": "", "fixes": "", "solution": "PROOF 2"}},
            {"kind": "status", "label": "Workflow step: convention", "node": "convention", "visit": 1},
            {"kind": "request", "stage": "critic", "label": "Convention critic", "candidateText": "PROOF 2"},
            {"kind": "convention_result", "round": 2, "verdict": "fine"},
        ]
        rounds = model_calls._rounds(transcript)
        self.assertEqual([(entry["round"], entry["submitted"], entry["verdict"], entry["outcome"]) for entry in rounds],
                         [(1, "PROOF 1", "reject", "reject"), (2, "PROOF 2", "pass", "accept")])
        self.assertEqual(rounds[1]["convention"]["verdict"], "fine")
        self.assertIsNone(rounds[0]["convention"])


class WebSocketContinuationTests(RecordCase):
    def test_a_continuation_is_expanded_in_the_training_record(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        bundle.call("call-b", request([PROMPT], previous_response_id="resp_call-a"), [ANSWER])
        self.ingest(bundle)
        detail = model_calls.call_detail("run-1", "c000002")
        self.assertEqual(detail["sent"]["previousCallId"], "c000001")
        self.assertEqual([view["item"] for view in detail["sent"]["newItems"]], [PROMPT])
        buffer = io.BytesIO()
        model_calls.training_record("run-1", buffer)
        requests = [json.loads(line) for line in
                    zipfile.ZipFile(io.BytesIO(buffer.getvalue())).read("requests.jsonl").splitlines()]
        self.assertTrue(requests[1]["inputExpanded"])
        self.assertEqual(requests[1]["request"]["input"], [PROMPT, ANSWER, PROMPT])


def stream_lines(session="s-1"):
    def assistant(identity, block, parent=None):
        return {"type": "assistant", "parent_tool_use_id": parent, "session_id": session,
                "message": {"id": identity, "model": "claude-opus-5-5", "content": [block],
                            "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50,
                                      "cache_read_input_tokens": 25, "output_tokens": 1}}}

    def user(block, parent=None):
        return {"type": "user", "parent_tool_use_id": parent, "session_id": session,
                "message": {"role": "user", "content": [block]}}

    return [
        {"type": "system", "subtype": "init", "session_id": session},
        assistant("msg_1", {"type": "thinking", "thinking": "THINK summary", "signature": "SIG"}),
        assistant("msg_1", {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a"}}),
        user({"type": "tool_result", "tool_use_id": "toolu_1", "content": "FILE TEXT"}),
        assistant("msg_2", {"type": "text", "text": "Done."}),
        {"type": "result", "subtype": "success", "session_id": session},
    ]


class ClaudeStreamTests(RecordCase):
    def recorder(self):
        recorder = records.Recorder(self.record.folder)
        self.addCleanup(recorder.close)
        return recorder

    def test_every_line_is_kept_and_each_request_gets_its_line(self):
        stream = self.recorder().claude_stream(role="critic", label="Critic coordinator")
        texts = [json.dumps(line) for line in stream_lines()]
        for text in texts:
            stream.line(text)
        self.assertEqual([call["callId"] for call in self.record.calls.read()], ["msg_1", "msg_2"])
        stream.finish()
        kept = gzip.decompress((self.record.folder / "claude" / f"{stream.label}.jsonl.gz").read_bytes())
        self.assertEqual(kept.decode().splitlines(), texts)
        self.assertFalse(stream.path.exists())
        detail = model_calls.call_detail("run-1", "c000002")
        self.assertEqual([view["item"]["type"] for view in detail["sent"]["newItems"]], ["tool_result"])
        self.assertEqual(detail["sent"]["previousCallId"], "c000001")
        first = model_calls.call_detail("run-1", "c000001")
        self.assertEqual([view["type"] for view in first["received"]["items"]], ["thinking", "tool_use"])
        self.assertEqual(first["received"]["items"][1]["output"]["content"], "FILE TEXT")
        self.assertEqual(model_calls.call_raw("run-1", "c000001")["format"], "claude-stream")
        row = model_calls.call_list("run-1")["calls"][0]
        self.assertEqual(row["tokens"], {"input": 175, "cached": 25, "output": None, "reasoning": None})

    def test_a_request_without_a_result_after_it_is_interrupted(self):
        stream = self.recorder().claude_stream(role="author")
        for line in stream_lines()[:5]:
            stream.line(json.dumps(line))
        stream.finish()
        self.assertEqual([(call["callId"], call["status"]) for call in self.record.calls.read()],
                         [("msg_1", "completed"), ("msg_2", "interrupted")])

    def test_a_stream_left_by_a_runner_that_ended_is_finished_at_the_next_start(self):
        folder = self.record.folder / "claude"
        folder.mkdir()
        (folder / "20261113T000000-author-abcdef.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in stream_lines()[:5]), encoding="utf-8")
        records.store_leftovers(self.record)
        self.assertEqual([(call["callId"], call["status"]) for call in self.record.calls.read()],
                         [("msg_1", "completed"), ("msg_2", "interrupted")])
        self.assertTrue((folder / "20261113T000000-author-abcdef.jsonl.gz").is_file())
        self.assertFalse((folder / "20261113T000000-author-abcdef.jsonl").exists())


def rollout(path, meta, records_after=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "payload": meta}, *records_after]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


class SessionCopyTests(RecordCase):
    def test_a_codex_session_is_copied_with_its_segments_and_subagents(self):
        home = self.root / "codex-home"
        day = home / "sessions" / "2026" / "11" / "13"
        rollout(day / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl", {"id": THREAD, "session_id": THREAD})
        rollout(day / f"rollout-2026-11-13T10-05-00-{THREAD}_01a1d4e2-9999-7000-8000-000000000001.jsonl",
                {"id": THREAD, "session_id": THREAD})
        rollout(day / f"rollout-2026-11-13T10-00-01-{CHILD}.jsonl",
                {"id": CHILD, "session_id": THREAD, "parent_thread_id": THREAD})
        grandchild = "01a1d4e2-7777-7000-8000-000000000002"
        rollout(day / f"rollout-2026-11-13T10-00-02-{grandchild}.jsonl",
                {"id": grandchild, "session_id": "other", "parent_thread_id": CHILD})
        other = "01a1d4e2-6666-7000-8000-000000000003"
        rollout(day / f"rollout-2026-11-13T10-00-03-{other}.jsonl", {"id": other, "session_id": other})
        recorder = records.Recorder(self.record.folder)
        self.assertTrue(recorder.keep_codex_session(home, THREAD))
        copied = sorted((self.record.folder / "sources" / "codex" / THREAD).rglob("*.jsonl.gz"))
        self.assertEqual(len(copied), 4)
        self.assertNotIn(other, " ".join(path.name for path in copied))
        for path in copied:
            # Each copy is gzip-compressed and gives back the session file's bytes.
            self.assertEqual(gzip.decompress(path.read_bytes()), (day / path.name[:-3]).read_bytes())
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_a_session_that_cannot_be_copied_is_said_and_not_reported_kept(self):
        recorder = records.Recorder(self.record.folder)
        self.assertFalse(recorder.keep_codex_session(self.root / "missing-home", THREAD))
        self.assertIn("no session file was found", model_calls.call_list("run-1")["recordState"]["problems"][0])
        home = self.root / "codex-home"
        rollout(home / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl",
                {"id": THREAD, "session_id": THREAD})
        with mock.patch.object(records, "_copy_verified", lambda source, target: False):
            self.assertFalse(recorder.keep_codex_session(home, THREAD))

    def test_a_claude_session_is_copied_with_its_subagents_and_tool_results(self):
        config = self.root / "claude-config"
        session = "3c91969e-c89a-4995-be2e-55e1728578af"
        project = config / "projects" / "-work"
        files = {f"{session}.jsonl": "{}\n", f"{session}/subagents/agent-1.jsonl": "{}\n",
                 f"{session}/subagents/agent-1.meta.json": "{}", f"{session}/tool-results/r1.txt": "out"}
        for name, text in files.items():
            (project / name).parent.mkdir(parents=True, exist_ok=True)
            (project / name).write_text(text, encoding="utf-8")
        (project / "other.jsonl").write_text("{}\n", encoding="utf-8")
        recorder = records.Recorder(self.record.folder)
        self.assertTrue(recorder.keep_claude_session(config, session))
        copied = self.record.folder / "sources" / "claude" / "-work"
        self.assertEqual(sorted(path.relative_to(copied).as_posix() for path in copied.rglob("*") if path.is_file()),
                         sorted(name + ".gz" for name in files))
        for name, data in files.items():
            self.assertEqual(records.read_source(copied / (name + ".gz")), data.encode())

    def test_an_author_thread_is_copied_again_only_where_it_grew(self):
        home = self.root / "codex-home"
        path = home / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl"
        rollout(path, {"id": THREAD, "session_id": THREAD})
        recorder = records.Recorder(self.record.folder)
        copies = []
        original = records._copy_verified
        with mock.patch.object(records, "_copy_verified",
                               lambda source, target: copies.append(Path(source).name) or original(source, target)):
            recorder.copy_codex_thread(home, THREAD)
            recorder.copy_codex_thread(home, THREAD)
            with path.open("a") as stream:
                stream.write("{}\n")
            recorder.copy_codex_thread(home, THREAD)
        self.assertEqual(len(copies), 2)
        copy = next((self.record.folder / "sources" / "codex" / THREAD).rglob("*.jsonl.gz"))
        self.assertEqual(records.read_source(copy), path.read_bytes())


class SnapshotTests(RecordCase):
    def test_the_transcript_is_added_to_and_copied_whole_when_it_changed(self):
        transcript = self.root / "transcript.jsonl"
        transcript.write_text('{"kind": "status"}\n{"kind": "request"}\n{"partial', encoding="utf-8")
        settings, prompts = self.root / "job-settings.json", self.root / "prompts"
        settings.write_text('{"criticRounds": 2}', encoding="utf-8")
        (prompts).mkdir()
        (prompts / "author.txt").write_text("Prove it.", encoding="utf-8")
        records.snapshot(self.record, transcript, settings, prompts)
        self.assertEqual([entry["kind"] for entry in records.read_transcript(self.record)], ["status", "request"])
        with transcript.open("a") as stream:
            stream.write('": 1}\n{"kind": "critic_result"}\n')
        records.snapshot(self.record, transcript, settings, prompts)
        self.assertEqual([entry.get("kind") for entry in records.read_transcript(self.record)],
                         ["status", "request", None, "critic_result"])
        folder = self.record.folder / "transcript"
        self.assertEqual(len(gzip.decompress((folder / "transcript.jsonl.gz").read_bytes()).splitlines()), 4)
        # A transcript that is not the one copied before (it was replaced) is copied whole again.
        transcript.write_text('{"kind": "status"}\n', encoding="utf-8")
        records.snapshot(self.record, transcript)
        self.assertEqual(len(records.read_transcript(self.record)), 1)
        self.assertEqual((folder / "job-settings.json").read_text(), '{"criticRounds": 2}')
        self.assertEqual((folder / "prompts" / "author.txt").read_text(), "Prove it.")

    def test_a_web_cutoff_run_s_sessions_and_discovery_folders_are_copied(self):
        controller = self.root / "experiment-control" / "run-1-abc"
        home = controller / "codex-home"
        rollout(home / "author" / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl",
                {"id": THREAD, "session_id": THREAD})
        discovery = controller / "discovery-state" / "discovery-1"
        discovery.mkdir(parents=True)
        (discovery / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (controller / "discovery-state" / "pacing.json").write_text("{}", encoding="utf-8")
        policy = controller / "retrieval-policy.json"
        policy.write_text(json.dumps({"isolation": {"codexHome": str(home)}}), encoding="utf-8")
        records.copy_web_cutoff(self.record, policy)
        sources = self.record.folder / "sources" / "web-cutoff"
        self.assertTrue((sources / "author-sessions" / "2026" / "11" / "13"
                         / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl.gz").is_file())
        self.assertEqual(records.read_source(sources / "discovery" / "discovery-1" / "events.jsonl.gz"), b"{}\n")
        self.assertFalse((sources / "discovery" / "pacing.json.gz").exists())


class StoreTests(RecordCase):
    def test_only_plain_folder_names_name_a_record(self):
        for name in ("..", ".", "a/b", "a\\b", ".hidden", "", "x\0y", "c:z"):
            self.assertFalse(records.valid_name(name), name)
            with self.assertRaises(ValueError):
                records.record_folder(name)
        self.assertTrue(records.valid_name("2026-11-13_21-05-07_a-statement"))

    def test_the_record_is_owner_only_and_outside_the_run_folder(self):
        self.assertEqual(self.record.folder.parent, providers.state_directory() / "records")
        self.assertEqual(oct(self.record.folder.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.record.folder / "manifest.json").stat().st_mode & 0o777), "0o600")

    def test_a_line_cut_short_by_a_crash_is_skipped_and_the_next_one_starts_fresh(self):
        log = self.record.calls
        log.append([{"program": "codex"}])
        with open(log.path, "ab") as stream:
            stream.write(b'{"id": "c000002", "prog')
        reader = records.CallLog(log.path)
        self.assertEqual([line["id"] for line in reader.read()], ["c000001"])
        with self.record.locked():
            reader.append([{"program": "claude"}])
        self.assertEqual([line["id"] for line in records.CallLog(log.path).read()], ["c000001", "c000002"])

    def test_items_are_stored_once_and_read_back(self):
        data = {records._sha(value): value for value in (b'{"a":1}', b'{"b":2}')}
        with self.record.locked():
            self.record.store(data)
            self.record.store(data)
        index = (self.record.folder / "items" / "index.jsonl").read_text().splitlines()
        self.assertEqual(len(index), 1)
        fresh = records.Objects(self.record.folder / "items")
        for sha, value in data.items():
            self.assertEqual(fresh.get(sha), value)


if __name__ == "__main__":
    unittest.main()
