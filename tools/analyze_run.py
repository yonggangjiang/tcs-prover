#!/usr/bin/env python3
"""Read TCS transcripts once and report observed usage, without provider calls.

Codex app-server totals are cumulative within a thread's counter epoch. Repeated
snapshots are not new spend; a falling counter starts another epoch on resume.
These records are telemetry, not an invoice, and missing usage is never zero.
"""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path


FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens", "total_tokens")
CAMEL = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
         "outputTokens", "reasoningOutputTokens", "totalTokens")
AUDIT_COUNTERS = set(FIELDS + CAMEL) | {
    "cache_read_input_tokens", "cache_creation_input_tokens",
    "cacheReadInputTokens", "cacheCreationInputTokens",
}


def normalize_usage(value):
    """Retain known nonnegative counters; an absent field remains unknown."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for snake, camel in zip(FIELDS, CAMEL):
        count = value.get(snake, value.get(camel))
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[snake] = count
    return result


def usage_summary(totals, cache_complete=True):
    result = dict(totals)
    inputs = result.get("input_tokens")
    cached = result.get("cached_input_tokens")
    complete = bool(cache_complete and inputs is not None and cached is not None and cached <= inputs)
    result["cache_accounting_complete"] = complete
    if complete:
        result["uncached_input_tokens"] = inputs - cached
        result["cache_hit_percent"] = round(100 * cached / inputs, 3) if inputs else None
    return result


def has_audit_measurement(record):
    """Require actual counters or a reported finite cost, not a truthy object."""
    if not isinstance(record, dict):
        return False
    values = [record.get("usage")]
    models = record.get("modelUsage")
    if isinstance(models, dict):
        values.extend(models.values())
    for value in values:
        if isinstance(value, dict) and any(
            key in AUDIT_COUNTERS and type(count) is int and count >= 0
            for key, count in value.items()
        ):
            return True
    cost = record.get("totalCostUsd")
    return type(cost) in (int, float) and math.isfinite(cost) and cost >= 0


def audit_measured(call):
    records = call.get("usageRecords")
    return isinstance(records, list) and any(has_audit_measurement(record) for record in records)


def analyze_transcript(path):
    """Stream a transcript; memory grows with identities, not transcript text."""
    path = Path(path)
    kinds, methods, requests, items, audits = (Counter() for _ in range(5))
    threads, scopes, seen_items, cli_seen, commands = {}, {}, set(), set(), {}
    scope_cache_complete = {}
    event_characters, file_mentions, large_outputs = Counter(), Counter(), {}
    initial_prompt_cat_commands = 0
    first_time = last_time = None
    lines = malformed = duplicate_items = 0
    cli_active, ambiguous_cli_usage = set(), []
    cli_ambiguous = False
    cli_unattributed_completions = 0
    completed_audits = {}
    audit_calls = {}
    with path.open(encoding="utf-8") as transcript:
        for line in transcript:
            lines += 1
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            if record.get("time"):
                first_time = first_time or record["time"]
                last_time = record["time"]
            kind = record.get("kind", "unknown")
            kinds[kind] += 1
            if kind == "request":
                requests[record.get("label", "unknown")] += 1
            if kind == "research_audit":
                audits[record.get("status", "unknown")] += 1
                if record.get("status") == "completed":
                    identity = record.get("requestId") or f"line:{lines}"
                    completed_audits[identity] = has_audit_measurement(record)
            if kind == "audit_usage":
                # Audit providers differ in whether cached tokens are included in
                # input and whether costs cover all models. Preserve their evidence
                # separately rather than silently adding incompatible counters.
                identity = record.get("requestId") or f"line:{lines}"
                audit_calls[identity] = {
                    key: record[key] for key in ("requestId", "slot", "model", "provider",
                                                "coverage", "outcome", "usageRecords")
                    if key in record
                }
            event = record.get("event") or {}
            if not isinstance(event, dict):
                continue
            method = event.get("method", event.get("type", ""))
            if method:
                methods[method] += 1
            event_characters[method or kind] += len(line)
            params = event.get("params") or {}
            if not isinstance(params, dict):
                continue
            scope = "root" if record.get("root") is True else (
                "subagent" if record.get("root") is False else "unclassified")
            if method == "thread/tokenUsage/updated":
                thread_id = params.get("threadId")
                usage = params.get("tokenUsage") or {}
                current = normalize_usage(usage.get("total")) if isinstance(usage, dict) else {}
                if not thread_id or not current:
                    continue
                state = threads.setdefault(thread_id, {
                    "scope": scope, "updates": 0, "duplicate_snapshots": 0,
                    "counter_resets": 0, "counter_anomalies": 0,
                    "epochs": 1, "totals": Counter(), "previous": {}, "cache_complete": True,
                })
                state["updates"] += 1
                # A missing cache counter is unknown, including across an epoch
                # reset. Conservatively suppress ratios for this whole thread
                # even if a later snapshot restores the omitted field.
                state["cache_complete"] = state["cache_complete"] and all(
                    key in current for key in ("input_tokens", "cached_input_tokens"))
                previous = state["previous"]
                if all(previous.get(key) == value for key, value in current.items()):
                    state["duplicate_snapshots"] += 1
                    continue
                # A reset can occur in resumed child threads as well as the author.
                reset = any(current[key] < previous[key]
                            for key in ("input_tokens", "output_tokens", "total_tokens")
                            if key in current and key in previous)
                if reset:
                    state["counter_resets"] += 1
                    state["epochs"] += 1
                    previous = {}
                for key, value in current.items():
                    baseline = previous.get(key, 0)
                    if value < baseline:
                        state["counter_anomalies"] += 1
                        continue
                    state["totals"][key] += value - baseline
                    previous[key] = value
                # Omitted counters keep their last known value within this
                # epoch. Resetting that baseline to zero would count twice.
                state["previous"] = previous
            elif method == "thread.started":
                cli_active.add(event.get("thread_id") or f"unknown:{lines}")
                cli_ambiguous = cli_ambiguous or len(cli_active) > 1
            elif method == "turn.completed" and normalize_usage(event.get("usage")):
                # Native `codex exec --json` reports per-turn usage, not app-server
                # cumulative snapshots. Do not combine its spelling with turn/completed.
                owner = event.get("thread_id")
                if owner is None and not cli_ambiguous and len(cli_active) == 1:
                    owner = next(iter(cli_active))
                identity = (owner, event.get("turn_id"), record.get("time"),
                            json.dumps(event, sort_keys=True))
                if identity not in cli_seen:
                    cli_seen.add(identity)
                    if owner is None:
                        ambiguous_cli_usage.append({"line": lines, "usage": event["usage"]})
                        cli_unattributed_completions += 1
                    else:
                        totals = scopes.setdefault("structured_calls", Counter())
                        measured = normalize_usage(event["usage"])
                        totals.update(measured)
                        scope_cache_complete["structured_calls"] = (
                            scope_cache_complete.get("structured_calls", True)
                            and all(key in measured for key in ("input_tokens", "cached_input_tokens")))
                        cli_active.discard(owner)
                    if cli_unattributed_completions >= len(cli_active):
                        cli_active.clear()
                        cli_unattributed_completions = 0
                        cli_ambiguous = False
            if method not in {"item/completed", "item.completed"}:
                continue
            item = params.get("item", event.get("item")) or {}
            if not isinstance(item, dict):
                continue
            owner = params.get("threadId", event.get("thread_id"))
            if owner is None:
                owner = next(iter(cli_active)) if len(cli_active) == 1 and not cli_ambiguous else f"line:{lines}"
            identity = (owner, item.get("id"))
            if item.get("id") and identity in seen_items:
                duplicate_items += 1
                continue
            if item.get("id"):
                seen_items.add(identity)
            item_type = item.get("type", "unknown")
            items[f"{scope}:{item_type}"] += 1
            if item_type in {"commandExecution", "command_execution"}:
                command = item.get("command", "")
                if not isinstance(command, str):
                    continue
                digest = hashlib.sha256(command.encode()).hexdigest()
                entry = commands.setdefault(digest, {"sha256": digest, "calls": 0,
                                                   "output_characters": 0})
                entry["calls"] += 1
                for name in ("INITIAL_PROMPT.md", "PROVED.md", "APPROACHES/index.md",
                             "AUDITS/", "audit_history/"):
                    if name.lower() in command.lower():
                        file_mentions[name] += 1
                initial_prompt_cat_commands += command.endswith("'cat INITIAL_PROMPT.md'")
                output = item.get("aggregatedOutput", item.get("aggregated_output", ""))
                if isinstance(output, str):
                    entry["output_characters"] += len(output)
                    if len(output) >= 500:
                        output_hash = hashlib.sha256(output.encode()).hexdigest()
                        previous = large_outputs.setdefault(output_hash, {"calls": 0, "characters": len(output)})
                        previous["calls"] += 1
    for state in threads.values():
        scopes.setdefault(state["scope"], Counter()).update(state["totals"])
        scope_cache_complete[state["scope"]] = (
            scope_cache_complete.get(state["scope"], True) and state["cache_complete"])
    totals = Counter()
    for scope_total in scopes.values():
        totals.update(scope_total)
    measured_audit_calls = {key for key, value in audit_calls.items() if audit_measured(value)}
    audit_usage_records = sum(
        inline or (key in measured_audit_calls and audit_calls[key].get("outcome") == "completed")
        for key, inline in completed_audits.items()
    )
    elapsed = None
    try:
        elapsed = (datetime.fromisoformat(last_time) - datetime.fromisoformat(first_time)).total_seconds()
    except (TypeError, ValueError):
        pass
    return {
        "transcript": str(path), "bytes": path.stat().st_size, "lines": lines,
        "malformed_records": malformed, "first_time": first_time, "last_time": last_time,
        "elapsed_wall_seconds": elapsed, "event_kinds": dict(kinds),
        "event_methods": dict(methods), "requests": dict(requests),
        "event_serialized_characters": dict(event_characters),
        "completed_items": dict(items), "duplicate_item_events": duplicate_items,
        "observed_usage": usage_summary(totals, all(scope_cache_complete.values())),
        "usage_by_scope": {key: usage_summary(value, scope_cache_complete[key])
                           for key, value in scopes.items()},
        "threads": [{"id": key, **{k: v for k, v in value.items()
                                     if k not in {"previous", "totals", "cache_complete"}},
                     "observed_usage": usage_summary(value["totals"], value["cache_complete"])}
                    for key, value in threads.items()],
        "audit_events": dict(audits), "completed_audits": len(completed_audits),
        "completed_audits_with_usage": audit_usage_records,
        "completed_audits_without_usage": len(completed_audits) - audit_usage_records,
        "audit_call_measurements": list(audit_calls.values()),
        "audit_calls_with_reported_usage": len(measured_audit_calls),
        "audit_calls_with_missing_usage": len(audit_calls) - len(measured_audit_calls),
        "ambiguous_structured_usage": ambiguous_cli_usage,
        "command_calls": sum(entry["calls"] for entry in commands.values()),
        "distinct_commands": len(commands),
        "command_output_characters": sum(entry["output_characters"] for entry in commands.values()),
        "research_file_command_mentions": dict(file_mentions),
        "exact_initial_prompt_cat_commands": initial_prompt_cat_commands,
        "repeated_large_command_output_characters": sum(
            (entry["calls"] - 1) * entry["characters"] for entry in large_outputs.values()),
        "most_repeated_commands": sorted(commands.values(), key=lambda e: -e["calls"])[:10],
        "cost_usd": None,
        "limitations": [
            "Observed provider counters are not an invoice or proof that all calls were recorded.",
            "Totals sum monotone per-thread counter increments; falling counters start new epochs.",
            "First counters can include earlier unobserved usage; resets between observations may be invisible.",
            "Cache ratios and uncached input require input/cache counters in every contributing snapshot; missing coverage is unknown.",
            "Root and subagent counters are reported separately; no parent-rollup correction is assumed.",
            "Reasoning tokens are a subset of output tokens and are never added again.",
            "Audit usage coverage is separate; audit usage and displayed cache statuses are not added to Codex counters.",
            "Audit coverage requires recognized counters or a finite reported cost, joined by requestId for new calls.",
            "Unattributed interleaved structured-call usage is retained separately and excluded from totals.",
            "Wall duration includes pauses. Command output characters are not tokens or necessarily unique text.",
            "File mentions include writes; identical command output need not be avoidable rereading.",
            "No prices are assumed; missing measurements and unsupported event formats are not zero cost.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path,
                        help="Run folders or transcript.jsonl files (one streaming scan per file).")
    args = parser.parse_args(argv)
    paths = [p / "transcript.jsonl" if p.is_dir() else p for p in args.paths]
    # Passing the same transcript through a symlink must not count it twice.
    paths = list(dict.fromkeys(path.resolve() for path in paths))
    print(json.dumps({"schema_version": 1, "runs": [analyze_transcript(p) for p in paths]}, indent=2))


if __name__ == "__main__":
    main()
