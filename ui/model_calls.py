"""The routes that read job records (runner/records.py): the model-calls list, one call, its raw request and
response, the training download and the Records list (contract: plan v3 section 1.7, record-contract.md).

Everything here only reads, except delete_record(). Placeholders say what a call's record cannot hold and why; they
are computed here, for the view and the download alike, never stored.
"""

import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import zipfile
import zlib

from runner import records

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# recordState.problems shows the newest this many distinct sentences of the record's problems.jsonl (which keeps all).
PROBLEMS_SHOWN = 50
# Stages of a web-cutoff run's one-shot calls (structured `codex exec --ephemeral`), whose request records stand in for
# calls the record cannot hold; the author's own stages are not one-shot calls.
AUTHOR_STAGES = ("solve", "repair", "failure", "conversation")
NETWORK_DETAILS = ["per-word arrival times", "response headers", "moderation notes"]
# The role a node's request records name (a web-cutoff run's placeholder rows): by stage and label.
STAGE_ROLES = {"review": "reviewer", "final": "latex editor"}
# The routes keep the Claude streams and trace event logs they read whole for the next request while their
# decompressed bytes add up to at most this much (_ReadCache); a larger file is read again each time. The training
# download keeps each Claude stream it reads until that process's last call instead.
READ_CACHE_BYTES = 64 * 1024 * 1024
_CACHE_LOCK = threading.Lock()
_COMPACTIONS = {}  # session copy -> (signature, [(time, summary)])
# Codex 0.160 leaves a local compaction untraced (core/src/compact.rs: "local compaction streams are left untraced"):
# that of every model it does not compact remotely, so of every API connection's model. Its summary is in the copied
# session file, as a `compacted` record with the summary as its message (a remote compaction's message is empty).
UNTRACED_COMPACTION = ("Codex 0.160 does not trace a local compaction (that of an API connection's model), so its "
                       "request is not recorded; its summary is, from the copied session file.")


class NotFound(ValueError):
    """No such record or call (404)."""


class _ReadCache:
    """Files read whole, kept for the next request: the least recently used goes first once their decompressed bytes
    add up to more than READ_CACHE_BYTES, and a file larger than that is never kept."""

    def __init__(self):
        self.entries, self.lock = {}, threading.Lock()  # key -> (signature, value, bytes), most recently used last

    def get(self, key, signature):
        with self.lock:
            entry = self.entries.pop(key, None)
            if entry is None or entry[0] != signature:
                return None
            self.entries[key] = entry
            return entry[1]

    def put(self, key, signature, value, size):
        with self.lock:
            self.entries.pop(key, None)
            if size > READ_CACHE_BYTES:
                return
            self.entries[key] = (signature, value, size)
            total = sum(entry[2] for entry in self.entries.values())
            while total > READ_CACHE_BYTES:
                total -= self.entries.pop(next(iter(self.entries)))[2]


_STREAMS = _ReadCache()  # Claude stream path -> its lines
_EVENTS = _ReadCache()  # stored bundle folder -> its trace events


def _record(name):
    try:
        folder = records.record_folder(name)
    except ValueError as exc:
        raise NotFound("Unknown record.") from exc
    if not folder.is_dir():
        raise NotFound("This job has no record.")
    return records.Record(folder)


def _labels(record):
    """{process label: its labels} of every Codex and Claude Code process of the record."""

    labels = {}
    for path in record.folder.glob("codex/*/process.json"):
        value = records.read_json(path, {})
        if isinstance(value, dict):
            labels[path.parent.name] = value
    for path in record.folder.glob("claude/*.json"):
        value = records.read_json(path, {})
        if isinstance(value, dict):
            labels[path.stem] = value
    return labels


def _role(line, labels):
    """(role, label, member) of one call line."""

    process = labels.get(line.get("process"), {})
    role = process.get("role") or {"review": "reviewer"}.get(process.get("stage")) or "model call"
    label = process.get("label") or process.get("activityLabel") or role
    member = process.get("member")
    agent = line.get("agentPath")
    if agent and agent != "/root":
        role, member = ("independent critic" if role == "critic" else "subagent"), agent
    if process.get("round") and role in ("critic", "independent critic", "convention critic", "latex repair"):
        label = f"{label} · round {process['round']}" if "round" not in label else label
    return role, label, member


def _tokens(line):
    usage = line.get("usage") if isinstance(line.get("usage"), dict) else {}
    if line.get("program") == "claude":
        if not usage:
            return {"input": None, "cached": None, "output": None, "reasoning": None}
        parts = [usage.get(key) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        total = sum(part for part in parts if isinstance(part, int)) if any(isinstance(part, int) for part in parts) \
            else None
        # The stream carries the usage of the message's start: its output count is not the request's.
        return {"input": total, "cached": usage.get("cache_read_input_tokens"), "output": None, "reasoning": None}
    return {"input": usage.get("input_tokens"), "cached": usage.get("cached_input_tokens"),
            "output": usage.get("output_tokens"), "reasoning": usage.get("reasoning_output_tokens")}


def _kinds(line, tokens):
    kinds = []
    if line.get("status") == "not recorded":
        return ["web_cutoff_one_shot"]
    if line.get("status") == "trace format not recognized":
        return ["trace_format_not_recognized"]
    if (tokens.get("reasoning") or 0) > 0 or line.get("program") == "claude":
        kinds.append("hidden_reasoning")
    kinds.extend(["token_probabilities", "network_only"])
    if line.get("status") == "interrupted":
        kinds.append("interrupted")
    return kinds


def _row(line, labels):
    role, label, member = _role(line, labels)
    process = labels.get(line.get("process"), {})
    tokens = _tokens(line)
    started, ended = line.get("startedMs"), line.get("endedMs")
    duration = (ended - started if line.get("program") == "codex" and isinstance(started, int)
                and isinstance(ended, int) and ended >= started else None)
    return {"id": line["id"], "time": records._iso(started), "role": role, "label": label, "member": member,
            "model": line.get("model") or process.get("model"), "program": line.get("program") or "codex",
            "durationMs": duration, "tokens": tokens, "status": line.get("status") or "completed",
            "placeholderKinds": _kinds(line, tokens), "kind": line.get("kind") or "request",
            "threadId": line.get("threadId")}


def _placeholder_rows(record):
    """Rows for calls the record cannot hold: a web-cutoff run's one-shot calls (one per attempt's request record)."""

    if not record.manifest().get("webCutoff"):
        return []
    rows, number = [], 0
    for entry in records.read_transcript(record):
        if entry.get("kind") != "request" or entry.get("stage") in AUTHOR_STAGES or entry.get("node") == "author":
            continue
        number += 1
        stage = entry.get("stage") or ""
        label = entry.get("label") or stage
        role = STAGE_ROLES.get(stage) or ("convention critic" if "onvention" in label else
                                          "critic" if stage == "critic" else stage or "model call")
        rows.append({"id": f"p{number:06d}", "time": _record_time(entry), "role": role, "label": label,
                     "member": None, "model": entry.get("model"), "program": "codex", "durationMs": None,
                     "tokens": {"input": None, "cached": None, "output": None, "reasoning": None},
                     "status": "not recorded", "placeholderKinds": ["web_cutoff_one_shot"], "kind": "request",
                     "threadId": None})
    return rows


def _compactions(path):
    """The local compactions of one copied Codex session file: [(time, summary)]."""

    try:
        info = path.stat()
    except OSError:
        return []
    signature = (info.st_size, info.st_mtime_ns)
    with _CACHE_LOCK:
        cached = _COMPACTIONS.get(str(path))
    if cached and cached[0] == signature:
        return cached[1]
    found = []
    for line in (records.read_source(path) or b"").splitlines():
        if b'"compacted"' not in line:
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        payload = value.get("payload") if isinstance(value, dict) and value.get("type") == "compacted" else None
        if isinstance(payload, dict) and isinstance(payload.get("message"), str) and payload["message"].strip():
            found.append((value.get("timestamp") if isinstance(value.get("timestamp"), str) else None,
                          payload["message"]))
    with _CACHE_LOCK:
        _COMPACTIONS[str(path)] = (signature, found)
    return found


def _compaction_rows(record, labels):
    """Rows for the local compactions of the record's Codex threads (UNTRACED_COMPACTION), from their session
    copies; each row's id is stable ("u" and a digest of its thread and time)."""

    threads = {}
    for line in record.calls.read():
        if line.get("program") == "codex" and line.get("threadId"):
            threads.setdefault(line["threadId"], line)
    rows = []
    for path in sorted(record.folder.glob("sources/codex/*/*/*/*/rollout-*")):
        match = records._ROLLOUT.match(path.name[:-3] if path.name.endswith(".gz") else path.name)
        line = threads.get(match.group(1)) if match else None
        if line is None:
            continue
        role, label, member = _role(line, labels)
        seen = {}
        for time_text, summary in _compactions(path):
            seen[time_text] = seen.get(time_text, 0) + 1
            key = f"{line['threadId']}\0{time_text}\0{seen[time_text]}"
            identity = "u" + hashlib.sha256(key.encode()).hexdigest()[:12]
            rows.append({"id": identity, "time": time_text, "role": role, "label": label, "member": member,
                         "model": line.get("model"), "program": "codex", "durationMs": None,
                         "tokens": {"input": None, "cached": None, "output": None, "reasoning": None},
                         "status": "not recorded", "placeholderKinds": ["compaction_not_traced"], "kind": "compaction",
                         "threadId": line["threadId"], "_summary": summary})
    return rows


def _record_time(entry):
    value = entry.get("time")
    return value if isinstance(value, str) else None


def _problems(record):
    problems = []
    try:
        with open(record.folder / "problems.jsonl", "rb") as stream:
            for line in stream:
                try:
                    text = json.loads(line).get("text")
                except (ValueError, AttributeError):
                    continue
                if isinstance(text, str) and text not in problems:
                    problems.append(text)
    except OSError:
        pass
    unrecognized = [line for line in record.calls.read() if line.get("status") == "trace format not recognized"]
    if unrecognized:
        processes = len({line.get("process") for line in unrecognized})
        problems.append(f"Raw trace files of {processes} Codex process{'es' if processes != 1 else ''} were kept: "
                        "their format is not recognized.")
    return problems[-PROBLEMS_SHOWN:]


def _rows(record, summaries=False):
    """Every row of the list, in time order (``summaries``: the untraced compactions keep their "_summary")."""

    labels = _labels(record)
    rows = ([_row(line, labels) for line in record.calls.read()] + _placeholder_rows(record)
            + _compaction_rows(record, labels))
    rows.sort(key=lambda row: (row["time"] or "", row["id"]))
    if not summaries:
        for row in rows:
            row.pop("_summary", None)
    return rows


def call_list(name, offset=0, limit=DEFAULT_LIMIT):
    """GET /jobs/<id>/model-calls."""

    record = _record(name)
    try:
        offset, limit = int(offset), int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("offset and limit must be whole numbers.") from exc
    if offset < 0 or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"offset must be 0 or more and limit 1 to {MAX_LIMIT}.")
    rows = _rows(record)
    return {"total": len(rows), "calls": rows[offset:offset + limit],
            "recordState": {"available": True, "problems": _problems(record)}}


# --- One call -----------------------------------------------------------------------------------------------------------

def _line(record, call_id):
    for line in record.calls.read():
        if line.get("id") == call_id:
            return line
    raise NotFound("Unknown model call.")


def _bundle(record, line, bundles=None):
    """The stored bundle of a Codex call; ``bundles``: those of a caller that reads many payloads, by process and
    bundle, each made (its payloads.jsonl read) once."""

    key = (line.get("process"), line.get("bundle"))
    bundle = bundles.get(key) if bundles is not None else None
    if bundle is None:
        bundle = records.StoredBundle(record, *key).load()
        if bundles is not None:
            bundles[key] = bundle
    return bundle


def _json(data):
    try:
        return json.loads(data)
    except (TypeError, ValueError, UnicodeError):
        return None


def _payload(record, line, field):
    """The exact bytes of a call's request or response payload, or None."""

    path = line.get(field)
    if not path or line.get("program") != "codex":
        return None
    try:
        return _bundle(record, line).payload(path)
    except (KeyError, ValueError, OSError, EOFError, gzip.BadGzipFile):
        return _raw_payload(record, line, path)


def _raw_payload(record, line, path):
    """A payload still in the raw trace (a process that is running, or a format kept raw)."""

    try:
        raw = record.path(f"codex-trace/{line['process']}/{line['bundle']}").joinpath(*path.split("/"))
        return raw.read_bytes()
    except (KeyError, OSError, ValueError, TypeError):
        return None


def _events(record, line):
    """The trace events of a call's bundle, in order (from its byte copy, or the raw trace while it runs)."""

    stored = record.folder / "codex" / str(line.get("process")) / str(line.get("bundle"))
    raw = record.folder / "codex-trace" / str(line.get("process")) / str(line.get("bundle")) / "trace.jsonl"
    for source in (stored / "trace.jsonl.gz", raw):
        try:
            info = source.stat()
        except OSError:
            continue
        signature = (str(source), info.st_size, info.st_mtime_ns)
        cached = _EVENTS.get(str(stored), signature)
        if cached is not None:
            return cached
        try:
            data = source.read_bytes()
            data = gzip.decompress(data) if source.suffix == ".gz" else data
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
        events = []
        for text in data.splitlines():
            try:
                event = json.loads(text)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict) and isinstance(event.get("payload"), dict):
                events.append(event)
        _EVENTS.put(str(stored), signature, events, len(data))
        return events
    return []


def _view(item, output=None, nested=None):
    return {"type": item.get("type") if isinstance(item, dict) else None, "item": item, "output": output,
            "nestedToolCalls": nested or []}


def _nested_calls(record, line, bundle, call_id):
    """The tools a code cell (the model's `exec` call ``call_id``) called, with their exact input and result."""

    events = _events(record, line)
    cells = {event["payload"].get("runtime_cell_id") for event in events
             if event["payload"].get("type") == "code_cell_started"
             and event["payload"].get("model_visible_call_id") == call_id}
    calls, order = {}, []
    for event in events:
        payload = event["payload"]
        kind = payload.get("type")
        requester = payload.get("requester") if isinstance(payload.get("requester"), dict) else {}
        if kind == "tool_call_started" and requester.get("type") == "code_cell" \
                and requester.get("runtime_cell_id") in cells:
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
            calls[payload.get("tool_call_id")] = {
                "tool": summary.get("label") or (payload.get("kind") or {}).get("type"),
                "input": _stored_json(bundle, payload.get("invocation_payload")), "output": None,
                "status": "started"}
            order.append(payload.get("tool_call_id"))
        elif kind == "tool_call_ended" and payload.get("tool_call_id") in calls:
            calls[payload["tool_call_id"]].update(status=payload.get("status"),
                                                  output=_stored_json(bundle, payload.get("result_payload")))
    return [calls[identity] for identity in order]


def _stored_json(bundle, reference):
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        return None
    try:
        return _json(bundle.payload(reference["path"]))
    except (KeyError, ValueError, OSError, EOFError, gzip.BadGzipFile):
        return None


def _thread_calls(record, line):
    """The Codex calls of ``line``'s thread in its bundle, in time order."""

    same = [other for other in record.calls.read()
            if other.get("program") == "codex" and other.get("process") == line.get("process")
            and other.get("bundle") == line.get("bundle") and other.get("threadId") == line.get("threadId")
            and other.get("request")]
    return sorted(same, key=lambda other: (other.get("startedMs") or 0, other["id"]))


def _codex_detail(record, line):
    bundle = _bundle(record, line)
    request = _json(_payload(record, line, "request")) or {}
    response = _json(_payload(record, line, "response")) or {}
    inputs = request.get("input") if isinstance(request.get("input"), list) else []
    outputs = response.get("output_items") if isinstance(response.get("output_items"), list) else []
    thread = _thread_calls(record, line)
    position = next((index for index, other in enumerate(thread) if other["id"] == line["id"]), None)
    previous = thread[position - 1] if position else None
    following = thread[position + 1] if position is not None and position + 1 < len(thread) else None
    new_items = inputs
    if request.get("previous_response_id"):
        previous = next((other for other in thread if other.get("responseId") == request["previous_response_id"]),
                        previous)
    elif previous is not None:
        before = _json(_payload(record, previous, "request")) or {}
        after = _json(_payload(record, previous, "response")) or {}
        known = [records.compact(item) for item in (before.get("input") or []) + (after.get("output_items") or [])]
        mine = [records.compact(item) for item in inputs]
        keep = 0
        for own, theirs in zip(mine, known):
            if own != theirs:
                break
            keep += 1
        new_items = inputs[keep:]
    later = (_json(_payload(record, following, "request")) or {}).get("input") if following else None
    outputs_by_call = {item.get("call_id"): item for item in later or ()
                       if isinstance(item, dict) and str(item.get("type", "")).endswith("_output")}
    received = []
    for item in outputs:
        call_id = item.get("call_id") if isinstance(item, dict) else None
        nested = (_nested_calls(record, line, bundle, call_id)
                  if isinstance(item, dict) and item.get("type") == "custom_tool_call" and call_id else [])
        received.append(_view(item, outputs_by_call.get(call_id) if call_id else None, nested))
    tools = request.get("tools")
    if tools is None:
        extra = next((item for item in inputs if isinstance(item, dict) and item.get("type") == "additional_tools"),
                     None)
        tools = extra.get("tools") if extra else None
    usage = response.get("token_usage") if isinstance(response.get("token_usage"), dict) else line.get("usage")
    return {"sent": {"instructions": request.get("instructions"), "tools": tools,
                     "newItems": [_view(item) for item in new_items],
                     "previousCallId": previous["id"] if previous else None},
            "received": {"items": received}, "usage": usage,
            "placeholders": _placeholders(line, outputs, usage),
            "rawAvailable": bool(line.get("request")) and _payload(record, line, "request") is not None}


def _stream_name(line):
    stream = line.get("stream") if isinstance(line.get("stream"), dict) else {}
    return str(stream.get("file") or "")


def _stream_lines(record, line, kept=None):
    """The stream-json lines of a Claude Code call's process (plain while it runs, compressed after); ``kept``: a
    caller's own {stream name: (signature, lines)} for many calls (else the routes' _STREAMS)."""

    name = _stream_name(line)
    for candidate in (name, name[:-3] if name.endswith(".gz") else name + ".gz"):
        try:
            path = record.path(candidate)
            info = path.stat()
        except (OSError, ValueError):
            continue
        signature = (info.st_size, info.st_mtime_ns)
        if kept is not None:
            cached = kept.get(name, (None, None))
            cached = cached[1] if cached[0] == (str(path), signature) else None
        else:
            cached = _STREAMS.get(str(path), signature)
        if cached is not None:
            return cached
        try:
            data = path.read_bytes()
            data = gzip.decompress(data) if path.suffix == ".gz" else data
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
        lines = data.decode("utf-8", "replace").split("\n")
        if kept is not None:
            kept[name] = ((str(path), signature), lines)
        else:
            _STREAMS.put(str(path), signature, lines, len(data))
        return lines
    return []


def _claude_detail(record, line, kept=None):
    lines = _stream_lines(record, line, kept)
    stream = line.get("stream") if isinstance(line.get("stream"), dict) else {}
    first, last = (stream.get("lines") or [0, -1])[:2]
    messages = [_json(text) for text in lines[first:last + 1]]
    blocks = []
    for message in messages:
        body = (message or {}).get("message") if isinstance(message, dict) else None
        if isinstance(body, dict) and body.get("id") == line.get("callId"):
            blocks.extend(block for block in body.get("content") or () if isinstance(block, dict))
    results = {}
    for text in lines[last + 1:]:
        message = _json(text)
        body = message.get("message") if isinstance(message, dict) else None
        for block in (body or {}).get("content") or () if isinstance(body, dict) else ():
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results.setdefault(block.get("tool_use_id"), block)
    sent = []
    for sha in stream.get("stdin") or ():
        # What TCS Prover wrote to the CLI's input (the prompt of a structured call or team member), sent first.
        try:
            sent.append(_view({"type": "text", "text": record.objects.get(sha).decode("utf-8", "replace")}))
        except (KeyError, ValueError, OSError, EOFError, zlib.error):
            pass
    for number in stream.get("sent") or ():
        message = _json(lines[number]) if 0 <= number < len(lines) else None
        body = message.get("message") if isinstance(message, dict) else None
        content = body.get("content") if isinstance(body, dict) else None
        if isinstance(content, str):
            sent.append(_view({"type": "text", "text": content}))
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict):
                sent.append(_view(block))
    previous = None
    for other in record.calls.read():
        if other["id"] == line["id"]:
            break
        if other.get("process") == line.get("process") and other.get("agentPath") == line.get("agentPath"):
            previous = other
    usage = line.get("usage") if isinstance(line.get("usage"), dict) else None
    return {"sent": {"instructions": None, "tools": None, "newItems": sent,
                     "previousCallId": previous["id"] if previous else None},
            "received": {"items": [_view(block, results.get(block.get("id")) if block.get("type") == "tool_use"
                                         else None) for block in blocks]},
            "usage": usage, "placeholders": _placeholders(line, blocks, usage),
            "rawAvailable": bool(lines)}


def _placeholders(line, outputs, usage):
    """Typed placeholders of what a call's record cannot hold (contract: Placeholder)."""

    claude = line.get("program") == "claude"
    company = "Anthropic" if claude else "OpenAI"
    found = []
    if line.get("status") == "trace format not recognized":
        return [{"kind": "trace_format_not_recognized", "facts": {}, "withheldBy": "Codex",
                 "why": "This Codex version's trace format is not one TCS Prover reads; its raw files are kept."}]
    reasoning = [item for item in outputs if isinstance(item, dict) and item.get("type") in
                 ("reasoning", "thinking", "redacted_thinking")]
    raw_text = any(item.get("content") for item in reasoning if item.get("type") == "reasoning")
    tokens = (usage or {}).get("reasoning_output_tokens") if isinstance(usage, dict) else None
    if (reasoning or tokens) and not raw_text:
        encrypted = any(item.get("encrypted_content") or item.get("signature") or item.get("data")
                        for item in reasoning)
        found.append({"kind": "hidden_reasoning", "facts": {"reasoningTokens": tokens, "encryptedCopyKept": encrypted},
                      "withheldBy": company,
                      "why": f"{company} does not send raw reasoning" + ("; its encrypted copy is kept." if encrypted
                                                                         else ".")})
    found.append({"kind": "token_probabilities", "facts": {}, "withheldBy": company,
                  "why": "Anthropic's API does not offer token probabilities." if claude
                  else "Refused by OpenAI for reasoning models."})
    found.append({"kind": "network_only", "facts": {"details": list(NETWORK_DETAILS)}, "withheldBy": "TCS Prover",
                  "why": "Not recorded on this route: TCS Prover records the programs' own copies, not the network."})
    if claude:
        found.append({"kind": "network_only", "facts": {"details": ["system prompt", "tool definitions",
                                                                     "request parameters"]},
                      "withheldBy": "Claude Code",
                      "why": "Claude Code's output stream does not carry the request it sent."})
    if line.get("status") == "interrupted":
        found.append({"kind": "interrupted", "facts": {}, "withheldBy": "TCS Prover",
                      "why": "The program stopped during this call, so its response was never complete."})
    return found


def call_detail(name, call_id):
    """GET /jobs/<id>/model-calls/<callId>."""

    record = _record(name)
    if call_id.startswith(("u", "p")):
        rows = _rows(record, summaries=True) if call_id.startswith("u") else _placeholder_rows(record)
        row = next((row for row in rows if row["id"] == call_id), None)
        if row is None:
            raise NotFound("Unknown model call.")
        return _unrecorded_detail(row)
    line = _line(record, call_id)
    if line.get("program") == "claude":
        return _claude_detail(record, line)
    if line.get("status") == "trace format not recognized":
        return {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": None},
                "received": {"items": []}, "usage": None, "placeholders": _placeholders(line, [], None),
                "rawAvailable": False}
    return _codex_detail(record, line)


def _unrecorded_detail(row):
    """The detail of a row of a call the record cannot hold: an untraced compaction ("u", its summary as its one
    received item) or a web-cutoff one-shot call ("p")."""

    if row["id"].startswith("u"):
        summary = {"type": "compacted", "message": row["_summary"]}
        return {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": None},
                "received": {"items": [_view(summary)]}, "usage": None,
                "placeholders": [{"kind": "compaction_not_traced", "facts": {"summaryChars": len(row["_summary"])},
                                  "withheldBy": "Codex", "why": UNTRACED_COMPACTION}],
                "rawAvailable": False}
    return {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": None},
            "received": {"items": []}, "usage": None,
            "placeholders": [{"kind": "web_cutoff_one_shot", "facts": {"label": row["label"]},
                              "withheldBy": "TCS Prover",
                              "why": "Not recorded: web-cutoff one-shot calls leave no files."}],
            "rawAvailable": False}


def call_raw(name, call_id):
    """GET /jobs/<id>/model-calls/<callId>/raw."""

    record = _record(name)
    if call_id.startswith(("p", "u")):
        raise NotFound("This call was not recorded.")
    line = _line(record, call_id)
    if line.get("program") == "claude":
        lines = _stream_lines(record, line)
        first, last = ((line.get("stream") or {}).get("lines") or [0, -1])[:2]
        if not lines:
            raise NotFound("This call's stream is not available.")
        return {"request": None, "response": "\n".join(lines[first:last + 1]), "format": "claude-stream"}
    request = _payload(record, line, "request")
    if request is None:
        raise NotFound("This call's request is not available.")
    response = _payload(record, line, "response")
    return {"request": request.decode("utf-8", "replace"),
            "response": response.decode("utf-8", "replace") if response is not None else None,
            "format": "codex-trace"}


# --- The Records list ------------------------------------------------------------------------------------------------

def record_list(job_exists):
    """GET /records: every record, newest first; ``job_exists(name)`` says whether its job is still listed."""

    root = records.record_root()
    rows = []
    try:
        folders = [path for path in root.iterdir() if path.is_dir() and records.valid_name(path.name)]
    except OSError:
        folders = []
    for folder in folders:
        manifest = records.Record(folder).manifest()
        rows.append({"runFolder": folder.name, "title": manifest.get("title") or "",
                     "created": manifest.get("created") or "", "sizeBytes": records.folder_size(folder),
                     "jobExists": bool(job_exists(folder.name))})
    rows.sort(key=lambda row: (row["created"], row["runFolder"]), reverse=True)
    return rows


def delete_record(name):
    """DELETE /records/<runFolder> (the caller checks that no process of the job runs)."""

    record = _record(name)
    shutil.rmtree(record.folder)
    if record.folder.exists():
        raise OSError(f"The record {name} could not be removed completely.")
    return {"deleted": name}


# --- The training download -------------------------------------------------------------------------------------------

class _Counter:
    """A write-only stream that counts what passes (zipfile writes to the response as it goes).

    cut() drops everything written after it: a download that fails part way never gets the archive's central
    directory, so no ZIP reader takes what arrived for a whole archive.
    """

    def __init__(self, stream):
        self.stream, self.written, self.cut_short = stream, 0, False

    def write(self, data):
        if self.cut_short:
            return len(data)
        self.stream.write(data)
        self.written += len(data)
        return len(data)

    def flush(self):
        if not self.cut_short:
            self.stream.flush()

    def cut(self):
        self.cut_short = True


@contextlib.contextmanager
def _archive(out):
    """A ZIP archive written to ``out`` (a _Counter) as it is made; when its block fails, ``out`` is cut before the
    archive closes, so the central directory a close writes never reaches the download."""

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        try:
            yield archive
        except BaseException:
            out.cut()
            raise


def _thread(line):
    return line.get("process"), line.get("bundle"), line.get("threadId")


def _shas(items):
    return [records._sha(records.compact(item)) for item in items]


def _list(value, field):
    items = value.get(field) if isinstance(value, dict) else None
    return items if isinstance(items, list) else []


class _Reader:
    """The Codex calls of one training download, read in time order.

    Each stored bundle is opened once (its payloads.jsonl read once; StoredBundle keeps the inputs it resolved last),
    and payloads are read parsed (StoredBundle.value()). Of each thread, only its latest request's full input and
    output items are kept, until the thread's last request: a WebSocket continuation sends only what is new since an
    earlier response, and is expanded with them. A continuation of any other request reads that one again from the
    record. Memory therefore follows the threads still open, never the whole record.
    """

    def __init__(self, record, lines):
        self.record, self.bundles, self.latest, self.responses, self.last = record, {}, {}, {}, {}
        for line in lines:
            if line.get("program") != "codex":
                continue
            if line.get("responseId"):
                self.responses.setdefault((line.get("process"), line.get("bundle"), line["responseId"]), line)
            if line.get("request"):
                self.last[_thread(line)] = line["id"]

    def value(self, line, field):
        """A Codex call's request or response payload, parsed, or None."""

        path = line.get(field)
        if not path:
            return None
        try:
            return _bundle(self.record, line, self.bundles).value(path)
        except (KeyError, ValueError, OSError, EOFError, gzip.BadGzipFile):
            return _json(_raw_payload(self.record, line, path))

    def stored(self, line, field, items):
        """The sha256 of each of ``items``, a call's own request input or its response's output items: as the record
        stored them, else computed."""

        try:
            bundle = _bundle(self.record, line, self.bundles)
            entry = bundle.entries.get(line.get(field))
            lists = (entry or {}).get("store", {}).get("lists") or {}
            shas = (bundle.input(line[field]) if field == "request" else lists.get("output_items")) if entry else None
        except (KeyError, ValueError, OSError, TypeError):
            shas = None
        return list(shas) if isinstance(shas, list) and len(shas) == len(items) else _shas(items)

    def _base(self, line, request):
        """The call whose response ``request`` continues (WebSocket), or None."""

        previous = request.get("previous_response_id")
        return self.responses.get((line.get("process"), line.get("bundle"), previous)) if previous else None

    def call(self, line):
        """(request, expanded, response, input sha256, output sha256) of a Codex call: its exact request (parsed),
        with the input expanded when it continues an earlier response, and its exact response."""

        request = self.value(line, "request") if line.get("request") else None
        request = request if isinstance(request, dict) else None
        response = self.value(line, "response")
        outputs = _list(response, "output_items")
        output_shas = self.stored(line, "response", outputs)
        if request is None:
            return None, False, response, [], output_shas
        own = _list(request, "input")
        base, expanded = self._base(line, request), False
        if base is None:
            inputs, shas = own, self.stored(line, "request", own)
        else:
            earlier = self.latest.get(_thread(line))
            if earlier is None or earlier[0] != base["id"]:
                earlier = self._again(base)
            inputs, shas = earlier[1] + earlier[3] + own, earlier[2] + earlier[4] + self.stored(line, "request", own)
            request, expanded = {**request, "input": inputs}, True
        if self.last.get(_thread(line)) == line["id"]:
            self.latest.pop(_thread(line), None)  # The thread's last request: nothing continues it.
        else:
            self.latest[_thread(line)] = (line["id"], inputs, shas, outputs, output_shas)
        return request, expanded, response, shas, output_shas

    def _again(self, line):
        """(id, input, input sha256, outputs, output sha256) of an earlier call, read again from the record: its own
        input after the input and outputs of the call it continues, and so on back, in a loop."""

        chain, seen = [], set()
        while line is not None and line["id"] not in seen:
            seen.add(line["id"])
            request = self.value(line, "request") if line.get("request") else None
            request = request if isinstance(request, dict) else {}
            chain.append((line, _list(request, "input"), _list(self.value(line, "response"), "output_items")))
            line = self._base(line, request)
        inputs, shas, outputs, output_shas = [], [], [], []
        for number, (_, own, received) in enumerate(reversed(chain)):
            inputs, shas = (inputs + outputs + own, shas + output_shas + _shas(own)) if number else (own, _shas(own))
            outputs, output_shas = received, _shas(received)
        return chain[0][0]["id"], inputs, shas, outputs, output_shas


class _Windows:
    """trajectories.jsonl, made as the requests go by: one window per context window of each Codex thread (a request
    whose input does not start with its window's items so far starts a new one). Items are held as their numbers in
    ``index`` (sha256 -> number, each item of the download once), which costs little memory."""

    def __init__(self, index):
        self.index, self.windows, self.current = index, [], {}

    def add(self, line, row, input_shas, output_shas):
        inputs = [self.index.setdefault(sha, len(self.index)) for sha in input_shas]
        outputs = [self.index.setdefault(sha, len(self.index)) for sha in output_shas]
        window = self.current.get(_thread(line))
        if window is None or inputs[:len(window["items"])] != window["items"]:
            window = self.current[_thread(line)] = {"window": len(self.windows) + 1, "threadId": line.get("threadId"),
                                                    "role": row.get("role"), "label": row.get("label"), "items": [],
                                                    "calls": [], "outcome": None}
            self.windows.append(window)
        window["items"] = inputs + outputs
        window["calls"].append({"id": line["id"], "inputEnd": len(inputs), "outputs": outputs, "role": row.get("role"),
                                "label": row.get("label"), "round": None})
        window["last"] = row.get("time") or ""

    def lines(self, rounds):
        """Each window as its line, its items named by sha256 again, with the critic round that followed it."""

        names = [None] * len(self.index)
        for sha, number in self.index.items():
            names[number] = sha
        ends = sorted((entry.get("time") or "", entry.get("round"), entry.get("outcome")) for entry in rounds)
        for window in self.windows:
            last = window.pop("last", "")
            window["items"] = [names[number] for number in window["items"]]
            for call in window["calls"]:
                call["outputs"] = [names[number] for number in call["outputs"]]
            following = next((entry for entry in ends if entry[0] >= last), None)
            if following is not None:
                window["outcome"] = {"round": following[1], "outcome": following[2]}
                for call in window["calls"]:
                    call["round"] = following[1]
            yield window


def _copy_file(archive, source, name):
    """Copy one record file into the archive as ``name`` (a .gz copy decompressed), in pieces; False when it is gone
    (a raw trace file stored and removed since it was listed)."""

    try:
        stream = gzip.open(source, "rb") if source.name.endswith(".gz") else open(source, "rb")
    except FileNotFoundError:
        return False
    with stream, archive.open(name, "w", force_zip64=True) as member:
        shutil.copyfileobj(stream, member, 1024 * 1024)
    return True


def _record_files(record, archive, bundles, lines):
    """The record's own files under record/, so the download leaves out nothing the record has: calls.jsonl and
    problems.jsonl; sources/ (the copied session files, a web-cutoff run's author sessions and discovery folders);
    claude/ (each Claude Code process's labels and stream); of codex/, each process's labels and each stored trace's
    manifest, event log and every payload that is not a call's request or response (those are in requests.jsonl);
    codex-trace/ (raw trace files not stored yet, or of a format TCS Prover does not read). A .gz copy is given back
    decompressed, under its name without .gz; a zstd one as it is. The record's bookkeeping (items/, payloads.jsonl,
    its locks and claims, copies still being written) is not a member: requests.jsonl and items.jsonl hold what it
    holds. Returns how many members."""

    folder, count = record.folder, 0

    def add(source, relative):
        nonlocal count
        name = "record/" + (relative[:-3] if relative.endswith(".gz") else relative)
        count += _copy_file(archive, source, name)

    for relative in ("calls.jsonl", "problems.jsonl"):
        if (folder / relative).is_file():
            add(folder / relative, relative)
    for top in ("sources", "claude", "codex-trace"):
        for source in sorted((folder / top).rglob("*")):
            # Claims, and a copy still being written (.<name>.<id>.tmp), are the record's bookkeeping too.
            if source.is_file() and not source.is_symlink() and not source.name.endswith(".claim") \
                    and not (source.name.startswith(".") and source.name.endswith(".tmp")):
                relative = source.relative_to(folder).as_posix()
                if top == "claude" and relative.endswith(".jsonl") and (folder / (relative + ".gz")).is_file():
                    continue  # Compressed and checked already: the .gz holds the same lines.
                add(source, relative)
    calls = {(line.get("process"), line.get("bundle"), line.get(field)) for line in lines for field in ("request",
                                                                                                        "response")}
    for process in sorted(path for path in (folder / "codex").glob("*") if path.is_dir()):
        if (process / "process.json").is_file():
            add(process / "process.json", f"codex/{process.name}/process.json")
        for stored in sorted(path for path in process.iterdir() if path.is_dir()):
            for name in ("manifest.json.gz", "trace.jsonl.gz"):
                if (stored / name).is_file():
                    add(stored / name, f"codex/{process.name}/{stored.name}/{name}")
            bundle = bundles.get((process.name, stored.name)) or records.StoredBundle(record, process.name,
                                                                                       stored.name).load()
            for path in sorted(bundle.entries, key=lambda path: (len(path), path)):
                if (process.name, stored.name, path) in calls:
                    continue
                try:
                    data = bundle.payload(path)
                except (KeyError, ValueError, OSError, EOFError, gzip.BadGzipFile):
                    continue  # Not rebuilt: its raw file stays in codex-trace/, given above.
                archive.writestr(f"record/codex/{process.name}/{stored.name}/{path}", data)
                count += 1
    return count


def _rounds(transcript):
    """One line per critic round, from the transcript snapshot: the proof the critic received in that round (the
    candidateText of the first request of its "Workflow step: critic" visit), its verdict and changes, and the
    convention critic's result of the round."""

    submitted, conventions, rounds, step = {}, {}, [], None
    for entry in transcript:
        if entry.get("kind") == "status" and str(entry.get("label", "")).startswith("Workflow step:"):
            step = (entry.get("node"), entry.get("visit"))
        if (entry.get("kind") == "request" and entry.get("candidateText") and step is not None
                and step[0] == "critic"):
            submitted.setdefault(step[1], entry["candidateText"])
        if entry.get("kind") == "convention_result":
            conventions[entry.get("round")] = {key: entry.get(key) for key in
                                               ("verdict", "intended", "assessment", "loophole", "suggestedStatement")}
    for entry in transcript:
        if entry.get("kind") != "critic_result":
            continue
        report = entry.get("report") if isinstance(entry.get("report"), dict) else {}
        number = entry.get("round")
        rounds.append({"round": number, "submitted": submitted.get(number), "verdict": report.get("verdict"),
                       "bugs": report.get("bugs"), "fixes": report.get("fixes"), "revised": report.get("solution"),
                       "proofDiff": report.get("proofDiff"), "outcome": entry.get("outcome"),
                       "convention": conventions.get(number), "time": entry.get("time")})
    return rounds


def training_record(name, stream):
    """GET /jobs/<id>/training-record: write the ZIP to ``stream`` as it is made (see record-contract.md).

    One pass over the calls in time order writes requests.jsonl and gathers each item's sha256 and the context
    windows (_Reader, _Windows); nothing of a call is kept once written, but what a later call of the same thread or
    Claude Code process needs.
    """

    record = _record(name)
    out = _Counter(stream)
    rows = {row["id"]: row for row in _rows(record, summaries=True)}
    lines = sorted(record.calls.read(), key=lambda line: (line.get("startedMs") or 0, line["id"]))
    # The transcript snapshot's bytes that read back whole (an append cut short is left out until the next snapshot).
    snapshot_data = records.transcript_data(record)
    transcript = records.transcript_lines(snapshot_data)
    rounds = _rounds(transcript)
    counts = {"requests": 0, "items": 0, "trajectories": 0, "rounds": len(rounds)}
    reader, index, streams = _Reader(record, lines), {}, {}
    windows = _Windows(index)
    # The last call of each Claude Code process: its stream's lines are kept until then.
    last_of_stream = {_stream_name(line): line["id"] for line in lines if line.get("program") == "claude"}
    with _archive(out) as archive:
        with archive.open("requests.jsonl", "w", force_zip64=True) as member:
            for line in lines:
                row = rows.get(line["id"], {})
                example = {key: row.get(key) for key in ("id", "time", "role", "label", "member", "model",
                                                         "program", "status")}
                if line.get("program") == "claude":
                    detail = _claude_detail(record, line, streams)
                    if last_of_stream.get(_stream_name(line)) == line["id"]:
                        streams.pop(_stream_name(line), None)  # The process's last call.
                    example.update(request=None, sent=[view["item"] for view in detail["sent"]["newItems"]],
                                   response={"content": [view["item"] for view in detail["received"]["items"]]},
                                   usage=detail["usage"], placeholders=detail["placeholders"])
                else:
                    request, expanded, response, input_shas, output_shas = reader.call(line)
                    for sha in (*input_shas, *output_shas):
                        index.setdefault(sha, len(index))
                    if line.get("request"):
                        windows.add(line, row, input_shas, output_shas)
                    example.update(request=request, inputExpanded=expanded, response=response,
                                   usage=(response.get("token_usage") if isinstance(response, dict) else None)
                                   or line.get("usage"),
                                   placeholders=_placeholders(line, _list(response, "output_items"), line.get("usage")))
                member.write(json.dumps(example, ensure_ascii=False).encode("utf-8") + b"\n")
                counts["requests"] += 1
            for row in rows.values():
                if row["id"].startswith(("p", "u")):
                    detail = _unrecorded_detail(row)
                    received = [view["item"] for view in detail["received"]["items"]]
                    member.write(json.dumps({**{key: row[key] for key in ("id", "time", "role", "label", "member",
                                                                           "model", "program", "status")},
                                             "request": None, "response": {"items": received} if received else None,
                                             "placeholders": detail["placeholders"]},
                                            ensure_ascii=False).encode("utf-8") + b"\n")
                    counts["requests"] += 1
        with archive.open("items.jsonl", "w", force_zip64=True) as member:
            for sha in record.objects.names():
                if sha not in index:
                    continue
                member.write(b'{"sha256":"' + sha.encode() + b'","item":' + record.objects.get(sha) + b"}\n")
                counts["items"] += 1
        with archive.open("trajectories.jsonl", "w", force_zip64=True) as member:
            for window in windows.lines(rounds):
                member.write(json.dumps(window, ensure_ascii=False).encode("utf-8") + b"\n")
                counts["trajectories"] += 1
        with archive.open("rounds.jsonl", "w", force_zip64=True) as member:
            for entry in rounds:
                member.write(json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n")
        snapshot = record.folder / "transcript"
        if (snapshot / "transcript.jsonl.gz").is_file():
            with archive.open("transcript/transcript.jsonl", "w", force_zip64=True) as member:
                member.write(snapshot_data)
        for path in sorted(snapshot.rglob("*")):
            if path.is_file() and path.name not in ("transcript.jsonl.gz", "transcript.json"):
                archive.write(path, "transcript/" + path.relative_to(snapshot).as_posix())
        counts["recordFiles"] = _record_files(record, archive, reader.bundles, lines)
        manifest = {**record.manifest(), "exportedAt": records._iso(int(time.time() * 1000)), "counts": counts,
                    "placeholderKinds": {
                        "hidden_reasoning": "the provider sent no raw reasoning; facts give its token count",
                        "token_probabilities": "not given by the provider for these models",
                        "network_only": "details only the network carries; not recorded on this route",
                        "web_cutoff_one_shot": "not recorded: web-cutoff one-shot calls leave no files",
                        "trace_format_not_recognized": "a Codex trace format TCS Prover does not read; raw files kept",
                        "interrupted": "the program stopped during the call",
                        "compaction_not_traced": UNTRACED_COMPACTION}}
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    stream.flush()
    return out.written
