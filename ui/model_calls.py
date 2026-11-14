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
_CACHE_LOCK = threading.Lock()
_STREAMS = {}  # stream path -> (size, lines)
_EVENTS = {}  # bundle folder -> (signature, events)
_COMPACTIONS = {}  # session copy -> (signature, [(time, summary)])
# Codex 0.160 leaves a local compaction untraced (core/src/compact.rs: "local compaction streams are left untraced"):
# that of every model it does not compact remotely, so of every API connection's model. Its summary is in the copied
# session file, as a `compacted` record with the summary as its message (a remote compaction's message is empty).
UNTRACED_COMPACTION = ("Codex 0.160 does not trace a local compaction (that of an API connection's model), so its "
                       "request is not recorded; its summary is, from the copied session file.")


class NotFound(ValueError):
    """No such record or call (404)."""


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


def _bundle(record, line):
    return records.StoredBundle(record, line.get("process"), line.get("bundle")).load()


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
        with _CACHE_LOCK:
            cached = _EVENTS.get(str(stored))
        if cached and cached[0] == signature:
            return cached[1]
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
        with _CACHE_LOCK:
            _EVENTS[str(stored)] = (signature, events)
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


def _stream_lines(record, line):
    """The stream-json lines of a Claude Code call's process (plain while it runs, compressed after)."""

    stream = line.get("stream") if isinstance(line.get("stream"), dict) else {}
    name = str(stream.get("file") or "")
    for candidate in (name, name[:-3] if name.endswith(".gz") else name + ".gz"):
        try:
            path = record.path(candidate)
            info = path.stat()
        except (OSError, ValueError):
            continue
        with _CACHE_LOCK:
            cached = _STREAMS.get(str(path))
        if cached and cached[0] == (info.st_size, info.st_mtime_ns):
            return cached[1]
        try:
            data = path.read_bytes()
            data = gzip.decompress(data) if path.suffix == ".gz" else data
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
        lines = data.decode("utf-8", "replace").split("\n")
        with _CACHE_LOCK:
            _STREAMS[str(path)] = ((info.st_size, info.st_mtime_ns), lines)
        return lines
    return []


def _claude_detail(record, line):
    lines = _stream_lines(record, line)
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
    if call_id.startswith("u"):
        row = next((row for row in _rows(record, summaries=True) if row["id"] == call_id), None)
        if row is None:
            raise NotFound("Unknown model call.")
        summary = {"type": "compacted", "message": row["_summary"]}
        return {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": None},
                "received": {"items": [_view(summary)]}, "usage": None,
                "placeholders": [{"kind": "compaction_not_traced", "facts": {"summaryChars": len(row["_summary"])},
                                  "withheldBy": "Codex", "why": UNTRACED_COMPACTION}],
                "rawAvailable": False}
    if call_id.startswith("p"):
        row = next((row for row in _placeholder_rows(record) if row["id"] == call_id), None)
        if row is None:
            raise NotFound("Unknown model call.")
        return {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": None},
                "received": {"items": []}, "usage": None,
                "placeholders": [{"kind": "web_cutoff_one_shot", "facts": {"label": row["label"]},
                                  "withheldBy": "TCS Prover",
                                  "why": "Not recorded: web-cutoff one-shot calls leave no files."}],
                "rawAvailable": False}
    line = _line(record, call_id)
    if line.get("program") == "claude":
        return _claude_detail(record, line)
    if line.get("status") == "trace format not recognized":
        return {"sent": {"instructions": None, "tools": None, "newItems": [], "previousCallId": None},
                "received": {"items": []}, "usage": None, "placeholders": _placeholders(line, [], None),
                "rawAvailable": False}
    return _codex_detail(record, line)


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


def _expanded_request(record, line, cache):
    """A Codex call's exact request, its input expanded when it continues an earlier response (WebSocket)."""

    request = _json(_payload(record, line, "request"))
    if not isinstance(request, dict):
        return None, False
    previous_id = request.get("previous_response_id")
    if not previous_id:
        cache[line["id"]] = request.get("input") or []
        return request, False
    base = next((other for other in _thread_calls(record, line) if other.get("responseId") == previous_id), None)
    if base is None:
        return request, False
    if base["id"] not in cache:
        _expanded_request(record, base, cache)
    response = _json(_payload(record, base, "response")) or {}
    full = list(cache.get(base["id"], [])) + list(response.get("output_items") or []) + list(request.get("input") or [])
    cache[line["id"]] = full
    return {**request, "input": full}, True


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
    """GET /jobs/<id>/training-record: write the ZIP to ``stream`` as it is made (see record-contract.md)."""

    record = _record(name)
    out = _Counter(stream)
    rows = {row["id"]: row for row in _rows(record)}
    lines = sorted(record.calls.read(), key=lambda line: (line.get("startedMs") or 0, line["id"]))
    transcript = records.read_transcript(record)
    rounds = _rounds(transcript)
    counts = {"requests": 0, "items": 0, "trajectories": 0, "rounds": len(rounds)}
    item_names = set()
    with _archive(out) as archive:
        with archive.open("requests.jsonl", "w", force_zip64=True) as member:
            cache = {}
            for line in lines:
                row = rows.get(line["id"], {})
                example = {key: row.get(key) for key in ("id", "time", "role", "label", "member", "model",
                                                         "program", "status")}
                if line.get("program") == "claude":
                    detail = _claude_detail(record, line)
                    example.update(request=None, sent=[view["item"] for view in detail["sent"]["newItems"]],
                                   response={"content": [view["item"] for view in detail["received"]["items"]]},
                                   usage=detail["usage"], placeholders=detail["placeholders"])
                else:
                    request, expanded = _expanded_request(record, line, cache) if line.get("request") else (None, False)
                    response = _json(_payload(record, line, "response"))
                    for item in (request or {}).get("input") or []:
                        item_names.add(records._sha(records.compact(item)))
                    for item in (response or {}).get("output_items") or []:
                        item_names.add(records._sha(records.compact(item)))
                    outputs = (response or {}).get("output_items") or []
                    example.update(request=request, inputExpanded=expanded, response=response,
                                   usage=(response or {}).get("token_usage") or line.get("usage"),
                                   placeholders=_placeholders(line, outputs, line.get("usage")))
                member.write(json.dumps(example, ensure_ascii=False).encode("utf-8") + b"\n")
                counts["requests"] += 1
            for row in rows.values():
                if row["id"].startswith(("p", "u")):
                    detail = call_detail(name, row["id"])
                    received = [view["item"] for view in detail["received"]["items"]]
                    member.write(json.dumps({**{key: row[key] for key in ("id", "time", "role", "label", "member",
                                                                           "model", "program", "status")},
                                             "request": None, "response": {"items": received} if received else None,
                                             "placeholders": detail["placeholders"]},
                                            ensure_ascii=False).encode("utf-8") + b"\n")
                    counts["requests"] += 1
        with archive.open("items.jsonl", "w", force_zip64=True) as member:
            for sha in record.objects.names():
                if sha not in item_names:
                    continue
                member.write(b'{"sha256":"' + sha.encode() + b'","item":' + record.objects.get(sha) + b"}\n")
                counts["items"] += 1
        with archive.open("trajectories.jsonl", "w", force_zip64=True) as member:
            for window in _trajectories(record, lines, rows, rounds):
                member.write(json.dumps(window, ensure_ascii=False).encode("utf-8") + b"\n")
                counts["trajectories"] += 1
        with archive.open("rounds.jsonl", "w", force_zip64=True) as member:
            for entry in rounds:
                member.write(json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n")
        snapshot = record.folder / "transcript"
        if (snapshot / "transcript.jsonl.gz").is_file():
            with archive.open("transcript/transcript.jsonl", "w", force_zip64=True) as member:
                try:
                    with gzip.open(snapshot / "transcript.jsonl.gz", "rb") as source:
                        shutil.copyfileobj(source, member, 1024 * 1024)
                except (OSError, EOFError):
                    pass  # What could be read; the record's problems are not this download's.
        for path in sorted(snapshot.rglob("*")):
            if path.is_file() and path.name not in ("transcript.jsonl.gz", "transcript.json"):
                archive.write(path, "transcript/" + path.relative_to(snapshot).as_posix())
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


def _trajectories(record, lines, rows, rounds):
    """One line per context window of each Codex thread: its items and each call's input boundary and outputs."""

    ends = sorted((entry.get("time") or "", entry.get("round"), entry.get("outcome")) for entry in rounds)
    windows, current = [], {}
    cache = {}
    for line in lines:
        if line.get("program") != "codex" or not line.get("request"):
            continue
        request, _ = _expanded_request(record, line, cache)
        response = _json(_payload(record, line, "response")) or {}
        inputs = [records._sha(records.compact(item)) for item in (request or {}).get("input") or []]
        outputs = [records._sha(records.compact(item)) for item in response.get("output_items") or []]
        key = (line.get("process"), line.get("bundle"), line.get("threadId"))
        row = rows.get(line["id"], {})
        window = current.get(key)
        if window is None or inputs[:len(window["items"])] != window["items"]:
            window = current[key] = {"window": len(windows) + 1, "threadId": line.get("threadId"),
                                     "role": row.get("role"), "label": row.get("label"), "items": [], "calls": [],
                                     "outcome": None}
            windows.append(window)
        window["items"] = inputs + outputs
        window["calls"].append({"id": line["id"], "inputEnd": len(inputs), "outputs": outputs, "role": row.get("role"),
                                "label": row.get("label"), "round": None})
        window["last"] = row.get("time") or ""
    for window in windows:
        last = window.pop("last", "")
        following = next((entry for entry in ends if entry[0] >= last), None)
        if following is not None:
            window["outcome"] = {"round": following[1], "outcome": following[2]}
            for call in window["calls"]:
                call["round"] = following[1]
    return windows
