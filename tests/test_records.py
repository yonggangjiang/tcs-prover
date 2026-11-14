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
import tracemalloc
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


def download(name="run-1"):
    """The training download of a record, as an open ZIP archive."""

    buffer = io.BytesIO()
    model_calls.training_record(name, buffer)
    return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))


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
        self.files, self.trace = {}, b""  # Every payload file and the event log, as written.

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
        line = json.dumps({"schema_version": self.schema, "seq": self.seq, "wall_time_unix_ms": self.time,
                           "rollout_id": self.thread, "thread_id": thread, "codex_turn_id": "turn-1",
                           "payload": payload}) + "\n"
        with open(self.folder / "trace.jsonl", "a", encoding="utf-8") as stream:
            stream.write(line)
        self.trace += line.encode()

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

    def test_an_unknown_trace_format_is_shown_while_its_process_runs(self):
        # The installed Codex updates itself: a long author process may write a format this code does not read,
        # whose raw files then grow for hours.
        bundle = Bundle(self.trace, schema=2)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        ingest = self.ingest(bundle, final=False)
        listing = model_calls.call_list("run-1")
        self.assertEqual([(row["status"], row["placeholderKinds"]) for row in listing["calls"]],
                         [("trace format not recognized", ["trace_format_not_recognized"])])
        self.assertTrue(any(self.trace.name in problem and "kept as Codex writes them" in problem
                            for problem in listing["recordState"]["problems"]), listing["recordState"]["problems"])
        bundle.call("call-b", request([PROMPT, ANSWER, PROMPT]), [ANSWER])
        ingest.step()
        ingest.step(final=True)
        self.assertEqual([(call["callId"], call["status"]) for call in self.record.calls.read()],
                         [("call-a", "trace format not recognized"), ("call-b", "trace format not recognized")])
        self.assertTrue((bundle.folder / "payloads" / "2.json").is_file())  # Kept raw.
        self.assertEqual(len((self.record.folder / "problems.jsonl").read_text().splitlines()), 1)

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

    def test_the_training_record_has_the_trace_s_event_log_and_every_payload_beside_the_calls(self):
        archive = download()
        stored = f"record/codex/{self.trace.name}/{self.bundle.folder.name}"
        self.assertEqual(archive.read(f"{stored}/trace.jsonl"), self.bundle.trace)
        self.assertEqual(json.loads(archive.read(f"{stored}/manifest.json"))["schema_version"], 1)
        self.assertEqual(json.loads(archive.read(f"record/codex/{self.trace.name}/process.json"))["role"], "critic")
        # Each payload that is not a request or response (those are in requests.jsonl), byte for byte: the session's
        # metadata, the code cell's tool call and the search text it read, the cell's own response.
        payloads = sorted(name.rsplit("/", 1)[1] for name in archive.namelist()
                          if name.startswith(f"{stored}/payloads/"))
        self.assertEqual(payloads, ["1.json", "4.json", "5.json", "6.json"])
        for name in payloads:
            self.assertEqual(archive.read(f"{stored}/payloads/{name}"), self.bundle.files[f"payloads/{name}"])
        self.assertIn(b"SEARCHOUT", archive.read(f"{stored}/payloads/5.json"))
        self.assertEqual(archive.read("record/calls.jsonl"), (self.record.folder / "calls.jsonl").read_bytes())
        self.assertEqual(json.loads(archive.read("manifest.json"))["counts"]["recordFiles"], 8)

    def test_a_training_record_that_fails_part_way_is_never_a_whole_archive(self):
        buffer = io.BytesIO()
        with mock.patch.object(records.Objects, "names", side_effect=OSError("The item index could not be read.")), \
                self.assertRaises(OSError):
            model_calls.training_record("run-1", buffer)
        self.assertIn(b"requests.jsonl", buffer.getvalue())  # What was written before the failure went out ...
        with self.assertRaises(zipfile.BadZipFile):  # ... but no central directory says it is whole.
            zipfile.ZipFile(io.BytesIO(buffer.getvalue()))

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


class LongThreadTests(RecordCase):
    """A Codex process of an API connection sends each request whole (over HTTP), which the record stores as a change
    to the thread's previous request: one chain as long as the thread, over a thousand requests in a long process."""

    LENGTH = 1500

    def setUp(self):
        super().setUp()
        bundle = records.StoredBundle(self.record, self.trace.name, f"trace-11111111-{THREAD}")
        skeleton = records.compact({"model": "gpt-6.1-sol", "input": None, "stream": True})
        self.items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"turn {n}"}]}
                      for n in range(self.LENGTH)]
        shas = [records._sha(records.compact(item)) for item in self.items]
        entries, calls = [], []
        for number in range(1, self.LENGTH + 1):
            # Each request keeps the first item of the one before and adds its own turn.
            refs = shas[:1] if number == 1 else {"base": f"payloads/{number - 1}.json", "keep": 1,
                                                 "add": [shas[number - 1]]}
            entries.append({"path": f"payloads/{number}.json", "kind": "inference_request", "sha256": "", "size": 0,
                            "store": {"lists": {"input": "input"}, "values": {}, "skeleton": records._sha(skeleton)},
                            "thread": THREAD, "input": refs})
            calls.append({"program": "codex", "kind": "request", "process": self.trace.name, "bundle": bundle.name,
                          "callId": f"call-{number}", "threadId": THREAD, "startedMs": 1794572964846 + 10 * number,
                          "endedMs": 1794572964846 + 10 * number + 5, "model": "gpt-6.1-sol", "status": "completed",
                          "request": f"payloads/{number}.json", "response": None, "usage": None})
        with self.record.locked():
            self.record.store({records._sha(records.compact(item)): records.compact(item) for item in self.items})
            self.record.store({records._sha(skeleton): skeleton})
            bundle.add(entries)
            self.record.calls.append(calls)
        self.last = f"c{self.LENGTH:06d}"

    def test_the_last_request_of_a_long_thread_reads_back(self):
        detail = model_calls.call_detail("run-1", self.last)
        self.assertEqual(detail["sent"]["previousCallId"], f"c{self.LENGTH - 1:06d}")
        self.assertEqual([view["item"] for view in detail["sent"]["newItems"]], [self.items[-1]])
        self.assertTrue(detail["rawAvailable"])
        raw = model_calls.call_raw("run-1", self.last)
        self.assertEqual(json.loads(raw["request"])["input"], [self.items[0], self.items[-1]])

    def test_the_training_record_of_a_long_thread_is_whole(self):
        buffer = io.BytesIO()
        model_calls.training_record("run-1", buffer)
        archive = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        requests = [json.loads(line) for line in archive.read("requests.jsonl").splitlines()]
        self.assertEqual(len(requests), self.LENGTH)
        self.assertEqual(requests[-1]["request"]["input"], [self.items[0], self.items[-1]])
        self.assertEqual(json.loads(archive.read("manifest.json"))["counts"]["requests"], self.LENGTH)

    def test_the_download_reads_a_bundle_s_payload_list_once(self):
        # Read again for every payload, the download's time grew with the square of the thread's requests.
        loads = []
        original = records.StoredBundle.load
        with mock.patch.object(records.StoredBundle, "load",
                               lambda bundle: loads.append(bundle.name) or original(bundle)):
            model_calls.training_record("run-1", Sink())
        self.assertEqual(len(loads), 1)


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
        self.assertEqual((rows[1]["error"], detail["error"]), (None, None))


class CallErrorTests(RecordCase):
    def test_a_failed_or_cancelled_call_says_why_in_the_list_the_detail_and_the_download(self):
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), status="failed")
        # Codex 0.160 cancels a request whose stream the provider dropped (the first real job's author, paused).
        reference = bundle.payload("inference_request", request([PROMPT]))
        bundle.event({"type": "inference_started", "inference_call_id": "call-b", "thread_id": THREAD,
                      "codex_turn_id": "turn-1", "model": "gpt-6.1-sol", "provider_name": "OpenAI",
                      "request_payload": reference}, THREAD)
        bundle.event({"type": "inference_cancelled", "inference_call_id": "call-b", "upstream_request_id": None,
                      "reason": "response stream dropped before provider terminal event",
                      "partial_response_payload": None}, THREAD)
        bundle.call("call-c", request([PROMPT]), [ANSWER])
        self.ingest(bundle)
        reasons = [("failed", "stream closed before response.completed"),
                   ("cancelled", "response stream dropped before provider terminal event"), ("completed", None)]
        rows = model_calls.call_list("run-1")["calls"]
        self.assertEqual([(row["status"], row["error"]) for row in rows], reasons)
        self.assertEqual([model_calls.call_detail("run-1", row["id"])["error"] for row in rows],
                         [error for _, error in reasons])
        requests = [json.loads(line) for line in download().read("requests.jsonl").splitlines()]
        self.assertEqual([(example["status"], example["error"]) for example in requests], reasons)


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

    def test_a_continuation_of_an_earlier_response_than_the_last_is_expanded_from_it(self):
        # A retry after a failed continuation continues the same earlier response again.
        bundle = Bundle(self.trace)
        bundle.thread_started()
        bundle.call("call-a", request([PROMPT]), [ANSWER])
        bundle.call("call-b", request([PROMPT], previous_response_id="resp_call-a"), status="failed")
        bundle.call("call-c", request([CELL_OUTPUT], previous_response_id="resp_call-a"), [ANSWER])
        self.ingest(bundle)
        buffer = io.BytesIO()
        model_calls.training_record("run-1", buffer)
        requests = [json.loads(line) for line in
                    zipfile.ZipFile(io.BytesIO(buffer.getvalue())).read("requests.jsonl").splitlines()]
        self.assertEqual([request["request"]["input"] for request in requests],
                         [[PROMPT], [PROMPT, ANSWER, PROMPT], [PROMPT, ANSWER, CELL_OUTPUT]])


class ThreadAcrossProcessesTests(RecordCase):
    """A thread a paused author leaves goes on in a summary or reply turn's Codex process, then in the resumed
    author's: a call's previous request, and the next one with its tool calls' outputs, are its thread's in whichever
    process sent them."""

    # A read-only turn's tools; the resent output of another process (Codex adds metadata of its own); the turns.
    SUMMARY_TOOLS = {"type": "additional_tools", "id": "at_2", "role": "developer",
                     "tools": [{"type": "namespace", "name": "functions", "tools": []}]}
    CELL_2 = {"type": "custom_tool_call", "id": "ctc_2", "status": "completed", "call_id": "call-2", "name": "exec",
              "input": "text(1);\n"}
    CELL_2_OUTPUT = {"type": "custom_tool_call_output", "id": "ctco_2", "call_id": "call-2",
                     "output": [{"type": "input_text", "text": "1"}]}
    ASK = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Write a progress summary."}]}
    SUMMARY = {"type": "message", "id": "msg_3", "role": "assistant",
               "content": [{"type": "output_text", "text": "Progress so far."}]}
    RESENT = {**SUMMARY, "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["text"]}}
    GO_ON = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Keep working."}]}

    def process(self, name, **labels):
        folder = self.record.folder / "codex-trace" / name
        folder.mkdir(parents=True)
        records.write_atomic(self.record.folder / "codex" / name / "process.json", json.dumps(labels).encode())
        return folder

    def test_a_call_is_compared_with_the_previous_request_of_its_thread_in_another_process(self):
        author = self.process("20261113T000000-author-aaaaaa", role="author", label="Author")
        first = Bundle(author)
        first.thread_started()
        first.call("call-a", request([TOOLS, SYSTEM, PROMPT]), [REASONING, CELL])
        # A WebSocket continuation sends only what is new since call-a's response.
        first.call("call-b", request([CELL_OUTPUT], previous_response_id="resp_call-a"), [ANSWER, self.CELL_2])
        records.TraceIngest(self.record, author.name, first.folder).step(final=True)
        summary = self.process("20261113T000100-summary-bbbbbb", role="summary", label="Progress summary")
        second = Bundle(summary)
        second.time = first.time + 60_000
        second.thread_started()
        # A new process sends the thread whole, with tools of its own, then its turn: the prompt is asked again.
        history = [SYSTEM, PROMPT, REASONING, CELL, CELL_OUTPUT, ANSWER, self.CELL_2, self.CELL_2_OUTPUT]
        second.call("call-c", request([self.SUMMARY_TOOLS, *history, self.ASK, PROMPT]), [self.SUMMARY])
        records.TraceIngest(self.record, summary.name, second.folder).step(final=True)
        resumed = self.process("20261113T000200-author-cccccc", role="author", label="Author")
        third = Bundle(resumed)
        third.time = second.time + 60_000
        third.thread_started()
        third.call("call-d", request([TOOLS, *history, self.ASK, PROMPT, self.RESENT, self.GO_ON]), [ANSWER])
        records.TraceIngest(self.record, resumed.name, third.folder).step(final=True)
        rows = model_calls.call_list("run-1")["calls"]
        self.assertEqual([(row["id"], row["role"]) for row in rows],
                         [("c000001", "author"), ("c000002", "author"), ("c000003", "summary"), ("c000004", "author")])
        news = {}
        for row in rows:
            sent = model_calls.call_detail("run-1", row["id"])["sent"]
            news[row["id"]] = (sent["previousCallId"], [view["item"] for view in sent["newItems"]])
        self.assertEqual(news, {
            "c000001": (None, [TOOLS, SYSTEM, PROMPT]),
            "c000002": ("c000001", [CELL_OUTPUT]),
            # Each item the previous request had, in its whole input or its output, is not new, once for each time it
            # had it.
            "c000003": ("c000002", [self.SUMMARY_TOOLS, self.CELL_2_OUTPUT, self.ASK, PROMPT]),
            "c000004": ("c000003", [TOOLS, self.RESENT, self.GO_ON])})
        # The author's last call before the pause: its tool call's output is in the summary turn's request.
        received = model_calls.call_detail("run-1", "c000002")["received"]["items"]
        self.assertEqual([(view["item"], view["output"]) for view in received],
                         [(ANSWER, None), (self.CELL_2, self.CELL_2_OUTPUT)])

    def test_a_continuation_kept_whole_is_followed_back_too(self):
        # A payload whose rebuild would differ (serde_json's 1e-7) is kept as its exact bytes, without a skeleton.
        author = self.process("20261113T000000-author-aaaaaa", role="author", label="Author")
        first = Bundle(author)
        first.thread_started()
        first.call("call-a", request([TOOLS, SYSTEM, PROMPT]), [REASONING, CELL])
        text = json.dumps(request([CELL_OUTPUT], previous_response_id="resp_call-a"), indent=2).replace(
            '"stream": true', '"temperature": 1e-7')
        first.call("call-b", None, [ANSWER], text=text)
        records.TraceIngest(self.record, author.name, first.folder).step(final=True)
        self.assertFalse(records.StoredBundle(self.record, author.name, first.folder.name).load()
                         .entries["payloads/4.json"].get("exact", True))
        summary = self.process("20261113T000100-summary-bbbbbb", role="summary", label="Progress summary")
        second = Bundle(summary)
        second.time = first.time + 60_000
        second.thread_started()
        second.call("call-c", request([self.SUMMARY_TOOLS, SYSTEM, PROMPT, REASONING, CELL, CELL_OUTPUT, ANSWER,
                                       self.ASK]), [self.SUMMARY])
        records.TraceIngest(self.record, summary.name, second.folder).step(final=True)
        sent = model_calls.call_detail("run-1", "c000003")["sent"]
        self.assertEqual((sent["previousCallId"], [view["item"] for view in sent["newItems"]]),
                         ("c000002", [self.SUMMARY_TOOLS, self.ASK]))


class Sink:
    """A download's destination that keeps nothing: it counts the bytes."""

    def __init__(self):
        self.size = 0

    def write(self, data):
        self.size += len(data)
        return len(data)

    def flush(self):
        pass


class DownloadCostTests(RecordCase):
    """The training download runs inside the server, which hosts every job: it holds what the threads still open
    need, never every call of the record."""

    def test_the_download_keeps_no_call_it_has_written(self):
        # 40 structured calls of a thread each, with an input of 300 KB of its own: 12 MB if they were all kept.
        for number in range(40):
            bundle = Bundle(self.trace, thread=f"01a1d4e2-0000-7000-8000-{number:012d}")
            bundle.thread_started()
            text = os.urandom(150_000).hex()
            bundle.call(f"call-{number}", request([{"type": "message", "role": "user",
                                                    "content": [{"type": "input_text", "text": text}]}]), [ANSWER])
            self.ingest(bundle)
        sink = Sink()
        with mock.patch.object(records, "MEMBER_CACHE_BYTES", 0):  # The item store's own bounded cache aside.
            tracemalloc.start()
            try:
                model_calls.training_record("run-1", sink)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        self.assertGreater(sink.size, 12_000_000)
        self.assertLess(peak, 4_000_000)


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

    def test_the_training_record_has_each_stream_and_session_copy(self):
        stream = self.recorder().claude_stream(role="critic", label="Critic coordinator")
        texts = [json.dumps(line) for line in stream_lines()]
        for text in texts:
            stream.line(text)
        stream.finish()
        project = self.root / "claude-config" / "projects" / "-work"
        project.mkdir(parents=True)
        (project / "s-1.jsonl").write_text('{"type": "user", "message": {"content": "PROMPT-MARKER"}}\n',
                                           encoding="utf-8")
        self.assertTrue(records.Recorder(self.record.folder).keep_claude_session(self.root / "claude-config", "s-1"))
        # A copy still being written when the download runs is not one of the record's files yet.
        (self.record.folder / "sources" / "claude" / "-work" / ".s-2.jsonl.gz.0123abcd.tmp").write_bytes(b"\x1f\x8b")
        archive = download()
        self.assertEqual(archive.read(f"record/claude/{stream.label}.jsonl").decode().splitlines(), texts)
        self.assertEqual(json.loads(archive.read(f"record/claude/{stream.label}.json"))["role"], "critic")
        self.assertEqual(archive.read("record/sources/claude/-work/s-1.jsonl"), (project / "s-1.jsonl").read_bytes())
        self.assertFalse([name for name in archive.namelist() if name.endswith((".claim", ".gz", ".tmp"))])

    def test_the_routes_keep_at_most_read_cache_bytes_of_what_they_read(self):
        stream = self.recorder().claude_stream(role="critic")
        for line in stream_lines():
            stream.line(json.dumps(line))
        stream.finish()
        path = str(self.record.folder / "claude" / f"{stream.label}.jsonl.gz")
        self.addCleanup(model_calls._STREAMS.entries.clear)
        model_calls.call_detail("run-1", "c000001")
        self.assertIn(path, model_calls._STREAMS.entries)  # Kept for the next request ...
        model_calls._STREAMS.entries.clear()
        with mock.patch.object(model_calls, "READ_CACHE_BYTES", 100):
            model_calls.call_detail("run-1", "c000001")
        self.assertNotIn(path, model_calls._STREAMS.entries)  # ... unless it is larger than the bound.
        cache = model_calls._ReadCache()
        with mock.patch.object(model_calls, "READ_CACHE_BYTES", 100):
            cache.put("a", 1, "A", 60)
            cache.put("b", 1, "B", 30)
            self.assertEqual(cache.get("a", 1), "A")
            cache.put("c", 1, "C", 30)  # 120 bytes: b, the least recently used, goes.
            self.assertEqual([cache.get(key, 1) for key in "abc"], ["A", None, "C"])
            self.assertIsNone(cache.get("a", 2))  # Changed since: read again.

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

    def test_a_request_the_provider_refused_is_a_failed_call_of_the_process_s_model(self):
        # Claude Code reports a request that failed for good as an assistant message of its own.
        # The job names an API connection's model by its connection; the CLI names the model it asks for.
        stream = self.recorder().claude_stream(role="critic", model="api:gateway-claude")
        for line in [{"type": "system", "subtype": "init", "session_id": "s-1", "model": "claude-opus-5-5"},
                     {"type": "assistant", "parent_tool_use_id": None, "session_id": "s-1", "error": "unknown",
                      "is_api_error_message": True, "uuid": "f2f55e24-8d4b-4ddd-b944-52133fbffd31",
                      "message": {"id": "5facec45-8185-47be-add4-1fcef5727334", "model": "<synthetic>",
                                  "content": [{"type": "text", "text": "API Error: 400 INJECTED-400"}],
                                  "usage": {"input_tokens": 0, "output_tokens": 0}}},
                     {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 400,
                      "session_id": "s-1"}]:
            stream.line(json.dumps(line))
        stream.finish()
        [call] = self.record.calls.read()
        self.assertEqual((call["status"], call["error"], call["usage"]),
                         ("failed", "API Error: 400 INJECTED-400", None))
        row = model_calls.call_list("run-1")["calls"][0]
        self.assertEqual((row["status"], row["model"], row["placeholderKinds"]),
                         ("failed", "claude-opus-5-5", ["token_probabilities", "network_only"]))

    def test_a_request_the_cli_sends_again_after_a_failure_has_its_own_failed_row(self):
        stream = self.recorder().claude_stream(role="critic", model="api:gateway-claude")
        stream.sent_input("Review.")
        retry = {"type": "system", "subtype": "api_retry", "max_retries": 300, "retry_delay_ms": 623,
                 "error_status": 529, "error": "overloaded", "session_id": "s-1"}
        lines = [{"type": "system", "subtype": "init", "session_id": "s-1", "model": "claude-opus-5-5"},
                 {**retry, "attempt": 1, "uuid": "u-1"},
                 # A wait for a usage limit is reported again every 30 s with the same attempt: one request.
                 {**retry, "attempt": 1, "uuid": "u-2"},
                 {**retry, "attempt": 2, "uuid": "u-3"},
                 stream_lines()[2],
                 # A subagent's failed request, which the CLI sends again.
                 {"type": "tool_progress", "tool_use_id": "toolu_9", "tool_name": "Agent", "parent_tool_use_id": None,
                  "elapsed_time_seconds": 0, "uuid": "u-4", "session_id": "s-1",
                  "subagent_retry": {"agent_id": "a1", "attempt": 1, "max_retries": 10, "retry_delay_ms": 2000,
                                     "error_status": 503, "error_category": "server_error"}},
                 stream_lines()[3], stream_lines()[4], stream_lines()[5]]
        for line in lines:
            stream.line(json.dumps(line))
        stream.finish()
        calls = self.record.calls.read()
        self.assertEqual([(call["callId"], call["status"], call["agentPath"], call.get("error"), call["model"])
                          for call in calls],
                         [("u-1", "failed", None, "529 overloaded", "claude-opus-5-5"),
                          ("u-3", "failed", None, "529 overloaded", "claude-opus-5-5"),
                          ("u-4", "failed", "toolu_9", "503 server_error", "claude-opus-5-5"),
                          ("msg_1", "completed", None, None, "claude-opus-5-5"),
                          ("msg_2", "completed", None, None, "claude-opus-5-5")])
        # Each failed attempt sent what the attempt after it sends again.
        for call_id in ("c000001", "c000002", "c000004"):
            detail = model_calls.call_detail("run-1", call_id)
            self.assertEqual([view["item"] for view in detail["sent"]["newItems"]],
                             [{"type": "text", "text": "Review."}], call_id)
        rows = {row["id"]: row for row in model_calls.call_list("run-1")["calls"]}
        self.assertEqual((rows["c000003"]["role"], rows["c000003"]["member"]), ("independent critic", "toolu_9"))

    def test_a_failed_call_says_why_in_the_list_and_the_detail(self):
        # An attempt the CLI sends again, then its report of the request it gave up on.
        stream = self.recorder().claude_stream(role="critic", model="api:gateway-claude")
        for line in [{"type": "system", "subtype": "init", "session_id": "s-1", "model": "claude-opus-5-5"},
                     {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 1, "retry_delay_ms": 600,
                      "error_status": 529, "error": "overloaded", "session_id": "s-1", "uuid": "u-1"},
                     {"type": "assistant", "parent_tool_use_id": None, "session_id": "s-1", "error": "unknown",
                      "is_api_error_message": True, "uuid": "f2f55e24-8d4b-4ddd-b944-52133fbffd31",
                      "message": {"id": "5facec45-8185-47be-add4-1fcef5727334", "model": "<synthetic>",
                                  "content": [{"type": "text", "text": "API Error: 400 INJECTED-400"}],
                                  "usage": {"input_tokens": 0, "output_tokens": 0}}},
                     {"type": "result", "subtype": "success", "is_error": True, "session_id": "s-1"}]:
            stream.line(json.dumps(line))
        stream.finish()
        reasons = [("failed", "529 overloaded"), ("failed", "API Error: 400 INJECTED-400")]
        rows = model_calls.call_list("run-1")["calls"]
        self.assertEqual([(row["status"], row["error"]) for row in rows], reasons)
        self.assertEqual([model_calls.call_detail("run-1", row["id"])["error"] for row in rows],
                         [error for _, error in reasons])

    def test_the_list_s_placeholder_kinds_are_its_detail_s(self):
        # msg_1 came back with a thinking block, msg_2 with text alone, msg_3 with a redacted one and never ended.
        redacted = {"type": "assistant", "parent_tool_use_id": None, "session_id": "s-1",
                    "message": {"id": "msg_3", "model": "claude-opus-5-5",
                                "content": [{"type": "redacted_thinking", "data": "ENCRYPTED"}],
                                "usage": {"input_tokens": 100, "output_tokens": 1}}}
        stream = self.recorder().claude_stream(role="author")
        for line in [*stream_lines()[:5], redacted]:
            stream.line(json.dumps(line))
        stream.finish()
        expected = [["hidden_reasoning", "token_probabilities", "network_only"],
                    ["token_probabilities", "network_only"],
                    ["hidden_reasoning", "token_probabilities", "network_only", "interrupted"]]

        def kinds():
            rows = model_calls.call_list("run-1")["calls"]
            for row in rows:
                detail = model_calls.call_detail("run-1", row["id"])
                self.assertEqual(row["placeholderKinds"],
                                 list(dict.fromkeys(item["kind"] for item in detail["placeholders"])), row["id"])
            return [row["placeholderKinds"] for row in rows]

        self.assertEqual(kinds(), expected)
        self.assertEqual([call["thinking"] for call in self.record.calls.read()], [True, False, True])
        # A call line written before 14 Nov 2026 does not say whether a thinking block came back: its stream does.
        path = self.record.folder / "calls.jsonl"
        older = [{key: value for key, value in json.loads(text).items() if key != "thinking"}
                 for text in path.read_text().splitlines()]
        path.write_text("".join(json.dumps(line) + "\n" for line in older))
        self.assertEqual(kinds(), expected)

    def test_the_prompt_written_to_the_cli_s_input_is_the_first_request_s_input(self):
        # A structured call or team member gets its prompt on standard input, which the stream never repeats.
        prompt = "Review the proof in input.txt. CRITIC-PROMPT-MARKER"
        stream = self.recorder().claude_stream(role="critic", label="Critic coordinator")
        stream.sent_input(prompt)
        for line in stream_lines():
            stream.line(json.dumps(line))
        stream.finish()
        first = model_calls.call_detail("run-1", "c000001")
        self.assertEqual([view["item"] for view in first["sent"]["newItems"]], [{"type": "text", "text": prompt}])
        self.assertIsNone(first["sent"]["previousCallId"])
        second = model_calls.call_detail("run-1", "c000002")
        self.assertEqual([view["item"]["type"] for view in second["sent"]["newItems"]], ["tool_result"])
        requests = [json.loads(line) for line in download().read("requests.jsonl").splitlines()]
        self.assertEqual(requests[0]["sent"], [{"type": "text", "text": prompt}])

    def test_a_stream_finished_at_a_later_start_keeps_its_prompt(self):
        stream = records.Recorder(self.record.folder).claude_stream(role="member", label="Member 1")
        stream.sent_input("MEMBER-ASSIGNMENT-MARKER")
        stream.stream.write("".join(json.dumps(line) + "\n" for line in stream_lines()[:5]).encode())
        stream.stream.close()
        stream.claim.release()  # The runner died: nothing finished the stream.
        records.store_leftovers(self.record)
        first = model_calls.call_detail("run-1", "c000001")
        self.assertEqual([view["item"] for view in first["sent"]["newItems"]],
                         [{"type": "text", "text": "MEMBER-ASSIGNMENT-MARKER"}])


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

    def test_a_snapshot_append_cut_short_still_reads_and_the_next_snapshot_repairs_it(self):
        self.record.update_manifest(webCutoff=True)
        transcript = self.root / "transcript.jsonl"
        lines = [json.dumps({"kind": "request", "stage": "critic", "label": f"Critic {number}",
                             "text": f"check {number} " + os.urandom(64).hex()}) for number in range(30)]
        transcript.write_text("\n".join(lines[:10]) + "\n", encoding="utf-8")
        records.snapshot(self.record, transcript)
        # The server is killed, or the disk fills, while the next snapshot appends its member: part of it stays.
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines[10:20]) + "\n")
        member = gzip.compress(("\n".join(lines[10:20]) + "\n").encode(), mtime=0)
        target = self.record.folder / "transcript" / "transcript.jsonl.gz"
        with target.open("ab") as stream:
            stream.write(member[:len(member) // 2])
        # What the last snapshot verified still reads: the list of a web-cutoff record and the download.
        self.assertEqual(len(records.read_transcript(self.record)), 10)
        self.assertEqual(model_calls.call_list("run-1")["total"], 10)
        buffer = io.BytesIO()
        model_calls.training_record("run-1", buffer)
        kept = zipfile.ZipFile(io.BytesIO(buffer.getvalue())).read("transcript/transcript.jsonl")
        self.assertEqual(kept.decode().splitlines(), lines[:10])
        # The next snapshot copies the transcript whole instead of appending after the broken bytes.
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines[20:]) + "\n")
        records.snapshot(self.record, transcript)
        self.assertEqual([entry["label"] for entry in records.read_transcript(self.record)],
                         [f"Critic {number}" for number in range(30)])
        records.snapshot(self.record, transcript)  # Nothing new: nothing changes.
        self.assertEqual(len(records.read_transcript(self.record)), 30)

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

    def test_a_web_cutoff_run_s_download_holds_its_author_sessions_and_discovery_folders(self):
        # Its author's calls exist in the record only as the copied sessions: the download must hold them.
        self.record.update_manifest(webCutoff=True)
        controller = self.root / "experiment-control" / "run-1-abc"
        home = controller / "codex-home"
        session = home / "author" / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl"
        rollout(session, {"id": THREAD, "session_id": THREAD}, [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "AUTHOR-WORK the lemma holds"}]}}])
        discovery = controller / "discovery-state" / "discovery-1"
        discovery.mkdir(parents=True)
        (discovery / "urls.json").write_text('{"urls": ["https://example.org/paper.pdf"]}', encoding="utf-8")
        policy = controller / "retrieval-policy.json"
        policy.write_text(json.dumps({"isolation": {"codexHome": str(home)}}), encoding="utf-8")
        records.copy_web_cutoff(self.record, policy)
        archive = download()
        self.assertEqual(archive.read(f"record/sources/web-cutoff/author-sessions/2026/11/13/{session.name}"),
                         session.read_bytes())
        self.assertEqual(archive.read("record/sources/web-cutoff/discovery/discovery-1/urls.json"),
                         (discovery / "urls.json").read_bytes())

    def test_a_discovery_call_s_copy_of_the_login_never_reaches_the_record(self):
        # A URL discovery call keeps a copy of the ChatGPT login in its private Codex home while it runs, and a call
        # stopped by a signal leaves it there; the folder is copied every RECORD_COPY_SECONDS meanwhile.
        controller = self.root / "experiment-control" / "run-1-abc"
        home = controller / "discovery-state" / "discovery-k2x9" / "home"
        home.mkdir(parents=True)
        (home / "auth.json").write_text('{"tokens": {"refresh_token": "FAKE-REFRESH-TOKEN"}}', encoding="utf-8")
        (home / ".credentials.json").write_text('{"token": "FAKE-MCP-TOKEN"}', encoding="utf-8")
        (home / "config.toml").write_text("", encoding="utf-8")
        (home.parent / "events.jsonl").write_text('{"type": "thread.started"}\n', encoding="utf-8")
        policy = controller / "retrieval-policy.json"
        policy.write_text(json.dumps({"isolation": {}}), encoding="utf-8")
        records.copy_web_cutoff(self.record, policy)
        copied = self.record.folder / "sources" / "web-cutoff" / "discovery" / "discovery-k2x9"
        self.assertTrue((copied / "events.jsonl.gz").is_file())
        self.assertTrue((copied / "home" / "config.toml.gz").is_file())
        self.assertEqual(sorted(path.name for path in (copied / "home").iterdir()), ["config.toml.gz"])
        for path in self.record.folder.rglob("*"):
            if path.is_file():
                data = records.read_source(path) or b""
                self.assertNotIn(b"FAKE-REFRESH-TOKEN", data, path)
                self.assertNotIn(b"FAKE-MCP-TOKEN", data, path)


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


@unittest.skipIf(os.name == "nt", "POSIX modes")
class FolderModeTests(RecordCase):
    """Every folder of a record is owner-only (0700), each folder made above a new one too, under the usual umask
    (022, with which Path.mkdir(parents=True) gives those 0755); an older record's are made so when it is next
    opened."""

    def setUp(self):
        super().setUp()
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        change = mock.patch.object(records, "_PRIVATE_TREES", set())  # As a new process: no record opened yet.
        change.start()
        self.addCleanup(change.stop)

    @staticmethod
    def modes(folder):
        """{path below ``folder``: mode} of ``folder`` and of every folder below it (links left out)."""

        return {path.relative_to(folder).as_posix(): oct(path.stat().st_mode & 0o777)
                for path in (folder, *folder.rglob("*")) if path.is_dir() and not path.is_symlink()}

    def test_every_folder_a_record_makes_is_owner_only(self):
        recorder = records.Recorder(records.record_folder("run-2"))
        self.addCleanup(recorder.close)
        record = recorder.record
        # A Codex process's trace, stored when the process ends (codex/<label>/<bundle>/).
        environment, process = recorder.codex_process({}, role="critic", label="Critic coordinator")
        Bundle(environment[records.TRACE_ROOT_ENV]).standard()
        process.finish()
        # A Claude Code stream, a copied Codex session (sources/codex/<thread>/<yyyy>/<mm>/<dd>/) and a snapshot whose
        # prompt set has a folder of its own.
        stream = recorder.claude_stream(role="critic")
        for line in stream_lines():
            stream.line(json.dumps(line))
        stream.finish()
        home = self.root / "codex-home"
        rollout(home / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T10-00-00-{THREAD}.jsonl",
                {"id": THREAD, "session_id": THREAD})
        self.assertTrue(recorder.keep_codex_session(home, THREAD))
        prompts = self.root / "controller-prompts"
        (prompts / "workflow").mkdir(parents=True)
        (prompts / "workflow" / "author.txt").write_text("Prove it.", encoding="utf-8")
        records.snapshot(record, prompts=prompts)
        modes = self.modes(record.folder)
        for made in (f"sources/codex/{THREAD}/2026/11/13", "codex", "codex-trace", "claude", "items",
                     "transcript/prompts/workflow"):
            self.assertIn(made, modes)
        self.assertEqual({path: mode for path, mode in modes.items() if mode != "0o700"}, {})

    def test_an_older_record_s_folders_are_made_owner_only_when_it_is_next_opened(self):
        outside = self.root / "outside"
        (outside / "inner").mkdir(parents=True)
        for path in (outside, outside / "inner"):
            path.chmod(0o755)
        openings = {"run-old-1": lambda: model_calls.call_list("run-old-1"),
                    "run-old-2": lambda: records.Record.create(records.record_folder("run-old-2")),
                    "run-old-3": lambda: model_calls.record_list(lambda name: False)}
        for name, opening in openings.items():
            with self.subTest(name=name):
                folder = records.record_folder(name)
                # As a record written before 14 Nov 2026 left them: the folders above the last of each path its
                # writer made have the default mode.
                for relative in (f"sources/codex/{THREAD}/2026/11/13", "codex/20261113T000000-critic-abcdef/trace-1",
                                 "transcript/prompts/workflow", "claude", "items"):
                    (folder / relative).mkdir(parents=True)
                records.write_atomic(folder / "manifest.json", b"{}\n")
                (folder / "sources" / "elsewhere").symlink_to(outside, target_is_directory=True)
                self.assertIn("0o755", self.modes(folder).values())
                opening()
                self.assertEqual(set(self.modes(folder).values()), {"0o700"})
        # A link in a record, or in place of one, is never followed.
        records.record_folder("run-link").symlink_to(outside, target_is_directory=True)
        model_calls.call_list("run-link")
        self.assertEqual([oct(path.stat().st_mode & 0o777) for path in (outside, outside / "inner")],
                         ["0o755", "0o755"])


if __name__ == "__main__":
    unittest.main()
