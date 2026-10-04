"""Summaries of folder runs: one report per batch of jobs started together.

A folder run (the web UI's "Run a folder" or ``web_ui.py FOLDER/``) starts one
job per statement file. It gets a folder under ``runs/batches/`` whose
manifest, ``batch.json``, lists its jobs. ``summary.md`` (and
``summary.json``, the same data for scripts) is rewritten when the batch
starts and whenever one of its jobs stops working, so it always describes the
latest state of every job: the models and settings, how many problems were
solved, tokens, estimated credits, quota and time for the whole batch, then
the same for each problem with the critic's verdict in every round.

Everything is read from the run folders on disk, so a summary can also be
written for jobs started before this file existed (``web_ui.py --summary``).
"""

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import statistics
import tempfile
import threading

BATCHES_DIRNAME = "batches"
MANIFEST_FILENAME = "batch.json"
SUMMARY_FILENAME = "summary.md"
SUMMARY_DATA_FILENAME = "summary.json"
SCHEMA_VERSION = 1

# Credits per million tokens at standard speed: uncached input, cached input,
# output (Codex pricing page, October 2026). Fast mode draws included usage
# 2.5 times faster. Models without a listed rate get no estimate.
CREDIT_RATES = {
    "gpt-6-astra": (250.0, 25.0, 1250.0),
    "gpt-5.6-sol": (100.0, 10.0, 500.0),
}
FAST_USAGE_MULTIPLIER = 2.5
# A job without a final record that has been quiet this long is "unfinished".
RUNNING_QUIET_SECONDS = 20 * 60
# Longer gaps inside one job (its process was gone) are not working time.
IDLE_GAP_SECONDS = 2 * 3600

ROLE_LABELS = (
    ("author", "Author"), ("subagents", "Subagents"), ("critic", "Critic"),
    ("writer", "LaTeX writer"), ("review", "Statement review"), ("other", "Other"),
)
# Structured (codex exec) calls by transcript stage.
STRUCTURED_ROLES = {"critic": "critic", "final": "writer", "review": "review"}
STRUCTURED_ROLE_NAMES = frozenset(STRUCTURED_ROLES.values())
ROLE_MODEL_KEYS = {
    "author": "authorModel", "subagents": "authorModel", "critic": "criticModel",
    "writer": "writerModel", "review": "reviewModel",
}
STAGE_ROLES = {"solve": "author", "repair": "author", "critic": "critic", "final": "writer", "review": "review"}
_USAGE_FIELDS = (
    ("total", "totalTokens"), ("input", "inputTokens"), ("cached", "cachedInputTokens"),
    ("output", "outputTokens"), ("reasoning", "reasoningOutputTokens"),
)
_EXEC_USAGE_FIELDS = (
    ("input", "input_tokens"), ("cached", "cached_input_tokens"),
    ("output", "output_tokens"), ("reasoning", "reasoning_output_tokens"),
)
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
_WRITE_LOCKS = {}
_WRITE_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------- batches

def batches_directory(runs):
    return Path(runs) / BATCHES_DIRNAME


def _slug(text, fallback="folder"):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:40].rstrip("-") or fallback


def _now():
    return datetime.now(timezone.utc)


def _write_lock(directory):
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(str(Path(directory).resolve()), threading.Lock())


def _atomic_write(path, text):
    """Replace one private file at once, so readers never see half of it."""

    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value


def create_batch(runs, folder="", folder_path="", sources=(), workflow="", origin="web",
                 settings=None, workflow_options=None, custom_prompts=(), started_at=None):
    """Create runs/batches/<stamp>_<folder>/ with its manifest; return the manifest."""

    started_at = started_at or _now()
    folder = str(folder or "").strip()
    root = batches_directory(runs)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = started_at.astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    base = f"{stamp}_{_slug(folder or Path(str(folder_path or '')).name)}"
    number = 1
    while True:
        identity = base if number == 1 else f"{base}-{number}"
        try:
            (root / identity).mkdir(mode=0o700)
        except FileExistsError:
            number += 1
            continue
        break
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "id": identity,
        "folder": folder or Path(str(folder_path or "")).name,
        "folderPath": str(folder_path or ""),
        "origin": origin,
        "createdAt": started_at.astimezone(timezone.utc).isoformat(),
        "workflow": workflow or "",
        "settings": dict(settings or {}),
        "workflowOptions": dict(workflow_options or {}),
        "customPrompts": sorted(custom_prompts),
        "sources": list(sources),
        "runs": [],
    }
    _atomic_write(root / identity / MANIFEST_FILENAME, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def load_manifest(batch_dir):
    manifest = _read_json(Path(batch_dir) / MANIFEST_FILENAME)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("runs"), list):
        return None
    return manifest


def add_run(runs, batch_id, run_id, source_file="", continues=""):
    """Record one job of a batch; a continuation names the run it continues."""

    batch_dir = batches_directory(runs) / batch_id
    with _write_lock(batch_dir):
        manifest = load_manifest(batch_dir)
        if manifest is None:
            return False
        if not any(entry.get("runId") == run_id for entry in manifest["runs"] if isinstance(entry, dict)):
            entry = {"runId": run_id, "sourceFile": source_file or ""}
            if continues:
                entry["continues"] = continues
            manifest["runs"].append(entry)
            _atomic_write(batch_dir / MANIFEST_FILENAME, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return True


def batches_for_run(runs, run_id):
    """Every batch that lists this run, newest first."""

    root = batches_directory(runs)
    if not run_id or not root.is_dir():
        return []
    found = []
    for manifest_path in root.glob(f"*/{MANIFEST_FILENAME}"):
        manifest = load_manifest(manifest_path.parent)
        if manifest and any(isinstance(entry, dict) and entry.get("runId") == run_id
                            for entry in manifest["runs"]):
            found.append(manifest_path.parent.name)
    return sorted(found, reverse=True)


def register_continuation(runs, source_run, run_id, source_file=""):
    """Put a continued job into the batches of the run it continues."""

    source_id = Path(str(source_run)).name if source_run else ""
    added = []
    for batch_id in batches_for_run(runs, source_id):
        if add_run(runs, batch_id, run_id, source_file, continues=source_id):
            added.append(batch_id)
    return added


def refresh_for_run(runs, run_id):
    """Rewrite the summary of every batch that lists this run; never raises."""

    written = []
    for batch_id in batches_for_run(runs, run_id):
        try:
            written.append(write_summary(runs, batch_id))
        except Exception:  # A summary must never break the job that triggered it.
            continue
    return written


# ---------------------------------------------------------------- one run

def _zero():
    return {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "calls": 0}


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _parse_time(text):
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _verdict(record):
    """The critic's decision in one critic_result record: (verdict, edits)."""

    report = record.get("report")
    if isinstance(report, str):
        try:
            report = ast.literal_eval(report)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            report = None
    raw = record.get("text")
    try:
        raw = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raw = None
    raw = raw if isinstance(raw, dict) else {}
    report = report if isinstance(report, dict) else {}
    verdict = report.get("verdict") or raw.get("verdict") or ""
    edits = raw.get("edits") or ""
    return str(verdict), str(edits)


def _headline(text, limit=180):
    for line in str(text or "").splitlines():
        line = re.sub(r"^[#>*\s-]+", "", line).replace("**", "").strip()
        if line:
            return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"
    return ""


def _title(run_dir):
    for name in ("checked-statement.md", "draft.md"):
        try:
            text = (run_dir / name).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for line in text.splitlines():
            match = re.match(r"#\s+(.+)", line.strip())
            if match and not re.match(r"(Statement sent directly|Checked statement|Draft problem|Statement review|Reviewer notes)",
                                      match.group(1)):
                return match.group(1).strip()
        body = re.sub(r"^#[^\n]*\n", "", text.strip()).strip()
        if body:
            return _headline(body, 90)
    return ""


def run_metrics(run_dir, now=None):
    """Read one job's folder: status, critic verdicts, tokens and time."""

    run_dir = Path(run_dir)
    now = now or _now()
    settings = _read_json(run_dir / "job-settings.json")
    settings = settings if isinstance(settings, dict) else {}
    goal_thread = settings.get("goalThreadId") or ""
    threads = {}
    roles = {name: _zero() for name, _ in ROLE_LABELS}
    role_seconds = {name: 0.0 for name, _ in ROLE_LABELS}
    structured_requests = {}
    quota = []
    counters = {"commands": 0, "webSearches": 0, "compactions": 0, "subagentsStarted": 0,
                "checkpoints": 0, "continuations": 0, "authorSubmissions": 0}
    flow = []
    first = last = previous = None
    previous_terminal = False
    active = 0.0
    lifecycle = None
    exists = (run_dir / "transcript.jsonl").is_file()
    try:
        stream = (run_dir / "transcript.jsonl").open(encoding="utf-8", errors="replace")
    except OSError:
        stream = None
    if stream is not None:
        with stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind, stage = record.get("kind"), record.get("stage")
                moment = _parse_time(record.get("time"))
                if moment is not None:
                    first = first or moment
                    if previous is not None and not previous_terminal:
                        gap = (moment - previous).total_seconds()
                        if 0 < gap <= IDLE_GAP_SECONDS:
                            active += gap
                            role_seconds[STAGE_ROLES.get(stage, "other")] += gap
                    previous, last = moment, moment
                terminal = False
                if kind == "codex_event":
                    event = record.get("event") if isinstance(record.get("event"), dict) else {}
                    method = event.get("method")
                    params = event.get("params") if isinstance(event.get("params"), dict) else {}
                    if method == "thread/tokenUsage/updated":
                        usage = (params.get("tokenUsage") or {}).get("total")
                        owner = params.get("threadId") or "unknown"
                        if isinstance(usage, dict):
                            # The runner marks the author's own thread; older logs only name it.
                            flag = record.get("root")
                            root = flag if isinstance(flag, bool) else (owner == goal_thread if goal_thread else True)
                            _thread_update(threads, owner, usage, root)
                    elif method == "account/rateLimits/updated":
                        primary = ((params.get("rateLimits") or {}).get("primary") or {})
                        used = primary.get("usedPercent")
                        if isinstance(used, (int, float)) and not isinstance(used, bool) and moment is not None:
                            quota.append((moment, float(used), primary.get("resetsAt")))
                    elif method == "item/completed":
                        item = params.get("item") if isinstance(params.get("item"), dict) else {}
                        kind_of_item = item.get("type")
                        if kind_of_item == "commandExecution":
                            counters["commands"] += 1
                        elif kind_of_item == "webSearch":
                            counters["webSearches"] += 1
                        elif kind_of_item == "contextCompaction":
                            counters["compactions"] += 1
                        elif kind_of_item == "subAgentActivity" and item.get("kind") == "started":
                            counters["subagentsStarted"] += 1
                    elif event.get("type") == "turn.completed":
                        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                        role = roles[STRUCTURED_ROLES.get(stage, "other")]
                        for key, source in _EXEC_USAGE_FIELDS:
                            role[key] += _count(usage.get(source))
                        role["calls"] += 1
                elif kind == "request":
                    label = str(record.get("label") or "")
                    if "responseSchema" in record:
                        structured_requests[stage] = structured_requests.get(stage, 0) + 1
                    if label == "Pre-compaction checkpoint requested":
                        counters["checkpoints"] += 1
                    elif label == "Continuation instruction sent":
                        counters["continuations"] += 1
                    lifecycle = ("work", None)
                elif kind == "author_result":
                    counters["authorSubmissions"] += 1
                    flow.append(("author",))
                    lifecycle = ("work", None)
                elif kind == "critic_result":
                    flow.append(("critic",) + _verdict(record))
                    lifecycle = ("work", None)
                elif kind == "status":
                    label = str(record.get("label") or "")
                    if label == "Critic approved":
                        flow.append(("approved", str(record.get("text") or "")))
                    elif label == "Usage quota" and moment is not None and isinstance(record.get("usedPercent"), (int, float)):
                        quota.append((moment, float(record["usedPercent"]), record.get("resetsAt")))
                    elif label == "Goal started" and record.get("threadId") and not goal_thread:
                        goal_thread = record["threadId"]
                    if label == "Stop requested":
                        lifecycle, terminal = ("stopped", None), True
                    elif label in {"Resume requested", "Goal started"} or label.startswith("Workflow step"):
                        lifecycle = ("work", None)
                elif kind == "workflow_result":
                    flow.append(("end",))
                    lifecycle, terminal = ("finished", None), True
                elif kind == "workflow_paused":
                    reason = str(record.get("reason") or "")
                    flow.append(("paused", reason))
                    lifecycle, terminal = ("paused", reason), True
                elif kind == "failure_result":
                    text = str(record.get("output") or record.get("summary") or record.get("text") or "")
                    rejected = stage == "critic"
                    flow.append(("rejected" if rejected else "failed", text))
                    lifecycle, terminal = ("rejected" if rejected else "failed", text), True
                previous_terminal = terminal or (previous_terminal and moment is None)
    # Usage of the author's goal session: the root thread and its subagents.
    for owner, entry in threads.items():
        role = roles["author" if entry["root"] else "subagents"]
        current = entry["current"] or {}
        for key in ("input", "cached", "output", "reasoning"):
            role[key] += entry["base"][key] + current.get(key, 0)
        role["calls"] += entry["calls"]
    subagent_threads = sum(1 for entry in threads.values() if not entry["root"])

    judge = _judge(flow)
    approvals_after_last_author = False
    for event in reversed(flow):
        if event[0] == "author":
            break
        if event[0] == "approved":
            approvals_after_last_author = True
            break
    status, detail = (lifecycle or ("work", None))
    if status == "work":
        # A job that has written nothing yet is as old as its folder.
        recent = last
        if recent is None:
            try:
                recent = datetime.fromtimestamp(run_dir.stat().st_mtime, timezone.utc)
            except OSError:
                recent = None
        quiet = (now - recent).total_seconds() if recent else None
        status = "running" if quiet is not None and quiet < RUNNING_QUIET_SECONDS else "unfinished"
        detail = "" if status == "running" else (
            f"No activity since {_local(last)}." if last else "The job never started working.")
    solved = approvals_after_last_author
    if status == "finished" and not solved:
        status = "failed"
    final_file = ""
    for name in ("final-proof.md", "final.tex"):
        if (run_dir / name).is_file():
            final_file = name
            break
    answer_text = ""
    if solved:
        for name in ("final-proof.md", "saved-candidate.md"):
            try:
                answer_text = (run_dir / name).read_text(encoding="utf-8")
                break
            except (OSError, UnicodeError):
                continue
    total = _zero()
    for name, _ in ROLE_LABELS:
        for key in total:
            total[key] += roles[name][key]
    speed = settings.get("speedMode") or "standard"
    credits = _credits(roles, settings, speed)
    quota.sort(key=lambda item: item[0])
    incomplete = {stage: max(0, count - roles[STRUCTURED_ROLES.get(stage, "other")]["calls"])
                  for stage, count in structured_requests.items()}
    return {
        "runId": run_dir.name,
        "exists": exists,
        "sourceFile": settings.get("sourceFile") or "",
        "title": _title(run_dir),
        "settings": settings,
        "status": status,
        "statusDetail": detail or "",
        "solved": solved,
        "judge": judge,
        "authorSubmissions": counters["authorSubmissions"],
        "answer": _headline(answer_text),
        "finalFile": final_file if solved else "",
        "startedAt": first.isoformat() if first else "",
        "endedAt": last.isoformat() if last else "",
        "workingSeconds": round(active, 1),
        "roleSeconds": {name: round(value, 1) for name, value in role_seconds.items() if value},
        "tokens": total,
        "roles": {name: roles[name] for name, _ in ROLE_LABELS if roles[name]["calls"] or any(roles[name].values())},
        "subagentThreads": subagent_threads,
        "credits": credits,
        "quota": {
            "first": [quota[0][0].isoformat(), quota[0][1], quota[0][2]] if quota else None,
            "last": [quota[-1][0].isoformat(), quota[-1][1], quota[-1][2]] if quota else None,
        },
        "activity": {key: counters[key] for key in ("commands", "webSearches", "compactions", "subagentsStarted",
                                                     "checkpoints", "continuations")},
        "incompleteRequests": {stage: count for stage, count in incomplete.items() if count},
    }


def _thread_update(threads, owner, usage, root):
    """Account one cumulative usage report the way the runner's meter does."""

    entry = threads.setdefault(owner, {"base": {key: 0 for key, _ in _USAGE_FIELDS}, "current": None,
                                       "calls": 0, "root": root})
    entry["root"] = entry["root"] or root
    current = {key: _count(usage.get(source)) for key, source in _USAGE_FIELDS}
    previous = entry["current"]
    if previous == current or not any(current.values()):
        # Codex repeats the same cumulative usage, and compactions report zeros.
        return
    if previous and current["total"] < previous["total"]:
        # The thread's counter restarted (a resumed session): keep what it had.
        for key, _ in _USAGE_FIELDS:
            entry["base"][key] += previous[key]
    entry["current"] = current
    entry["calls"] += 1


def _judge(flow):
    """The critic's verdict in each round: yes, repaired, or no."""

    rounds = []
    for index, event in enumerate(flow):
        if event[0] != "critic":
            continue
        verdict, edits = event[1], event[2]
        if verdict == "reject":
            rounds.append("no")
            continue
        following = next((later for later in flow[index + 1:]
                          if later[0] in {"critic", "approved", "author", "rejected"}), None)
        if following is None:
            rounds.append("repaired" if edits == "applied" else "yes")
        elif following[0] == "approved":
            rounds.append("repaired" if "Reached MAX" in following[1] else "yes")
        elif following[0] == "critic":
            rounds.append("repaired")
        else:
            rounds.append("no")
    return rounds


def _credits(roles, settings, speed):
    """Estimated credits per role at the listed rates; None where unknown."""

    multiplier = FAST_USAGE_MULTIPLIER if speed == "fast" else 1.0
    result, total, known = {}, 0.0, True
    for name, _ in ROLE_LABELS:
        usage = roles[name]
        if not any(usage[key] for key in ("input", "output")):
            continue
        rates = CREDIT_RATES.get(settings.get(ROLE_MODEL_KEYS.get(name, ""), ""))
        if rates is None:
            result[name], known = None, False
            continue
        uncached = max(0, usage["input"] - usage["cached"])
        value = (uncached * rates[0] + usage["cached"] * rates[1] + usage["output"] * rates[2]) / 1e6 * multiplier
        result[name] = round(value, 1)
        total += value
    result["total"] = round(total, 1) if known else None
    result["complete"] = known
    return result


# ---------------------------------------------------------------- the batch

def write_summary(runs, batch_id, now=None):
    """Rewrite summary.md and summary.json of one batch; return summary.md."""

    batch_dir = batches_directory(runs) / batch_id
    with _write_lock(batch_dir):
        manifest = load_manifest(batch_dir)
        if manifest is None:
            raise ValueError(f"Not a folder-run batch: {batch_dir}")
        now = now or _now()
        cached = _read_json(batch_dir / SUMMARY_DATA_FILENAME)
        cached = {entry.get("runId"): entry for entry in (cached or {}).get("runs", [])
                  if isinstance(entry, dict)} if isinstance(cached, dict) else {}
        metrics = []
        for entry in manifest["runs"]:
            if not isinstance(entry, dict) or not _SAFE_NAME.fullmatch(str(entry.get("runId") or "")):
                continue
            run_dir = Path(runs) / entry["runId"]
            signature = _signature(run_dir)
            previous = cached.get(entry["runId"])
            if (previous and previous.get("_signature") == signature
                    and previous.get("status") not in {"running", "unfinished"}):
                value = previous
            else:
                value = run_metrics(run_dir, now) if run_dir.is_dir() else _missing(entry["runId"])
                value["_signature"] = signature
            value["sourceFile"] = value.get("sourceFile") or entry.get("sourceFile") or ""
            value["continues"] = entry.get("continues") or ""
            metrics.append(value)
        data = summarize(manifest, metrics, now)
        _atomic_write(batch_dir / SUMMARY_DATA_FILENAME,
                      json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")
        _atomic_write(batch_dir / SUMMARY_FILENAME, render_markdown(data))
    return batch_dir / SUMMARY_FILENAME


def _signature(run_dir):
    parts = []
    for name in ("transcript.jsonl", "job-settings.json", "final-proof.md", "final.tex", "pause.json"):
        try:
            info = (run_dir / name).stat()
            parts.append([name, info.st_size, int(info.st_mtime_ns)])
        except OSError:
            parts.append([name, None, None])
    return parts


def _missing(run_id):
    return {"runId": run_id, "exists": False, "status": "missing", "statusDetail": "The run folder no longer exists.",
            "solved": False, "judge": [], "tokens": _zero(), "roles": {}, "credits": {"total": None, "complete": True},
            "workingSeconds": 0.0, "roleSeconds": {}, "settings": {}, "title": "", "answer": "", "finalFile": "",
            "startedAt": "", "endedAt": "", "quota": {"first": None, "last": None}, "activity": {},
            "authorSubmissions": 0, "subagentThreads": 0, "incompleteRequests": {}}


def _problems(metrics):
    """Group runs into problems: a continuation joins the run it continues."""

    chains, owner = [], {}
    for value in metrics:
        source = value.get("continues")
        if source and source in owner:
            chain = owner[source]
            chain.append(value)
        else:
            chain = [value]
            chains.append(chain)
        owner[value["runId"]] = chain
    problems = []
    for chain in chains:
        latest = chain[-1]
        tokens, roles = _zero(), {}
        credits_total, credits_known = 0.0, True
        for value in chain:
            for key in tokens:
                tokens[key] += value["tokens"].get(key, 0)
            for name, usage in value.get("roles", {}).items():
                target = roles.setdefault(name, _zero())
                for key in target:
                    target[key] += usage.get(key, 0)
            credit = value.get("credits", {})
            if credit.get("total") is None and (value["tokens"]["input"] or value["tokens"]["output"]):
                credits_known = False
            credits_total += credit.get("total") or 0.0
        problems.append({
            "sourceFile": next((value["sourceFile"] for value in chain if value.get("sourceFile")), ""),
            "title": next((value["title"] for value in chain if value.get("title")), ""),
            "runs": [value["runId"] for value in chain],
            "status": latest["status"],
            "statusDetail": latest.get("statusDetail", ""),
            "solved": latest["solved"],
            "judge": [verdict for value in chain for verdict in value.get("judge", [])],
            "authorSubmissions": sum(value.get("authorSubmissions", 0) for value in chain),
            "answer": latest.get("answer", ""),
            "finalFile": f"{latest['runId']}/{latest['finalFile']}" if latest.get("finalFile") else "",
            "startedAt": chain[0].get("startedAt", ""),
            "endedAt": latest.get("endedAt", ""),
            "workingSeconds": sum(value.get("workingSeconds", 0.0) for value in chain),
            "roleSeconds": _sum_maps(value.get("roleSeconds", {}) for value in chain),
            "tokens": tokens,
            "roles": roles,
            "subagentThreads": sum(value.get("subagentThreads", 0) for value in chain),
            "credits": round(credits_total, 1) if credits_known else None,
            "activity": _sum_maps(value.get("activity", {}) for value in chain),
            "incompleteRequests": _sum_maps(value.get("incompleteRequests", {}) for value in chain),
        })
    return problems


def _sum_maps(maps):
    total = {}
    for mapping in maps:
        for key, value in (mapping or {}).items():
            total[key] = total.get(key, 0) + value
    return total


def summarize(manifest, metrics, now=None):
    """Combine a manifest and its runs' metrics into the summary data."""

    now = now or _now()
    problems = _problems(metrics)
    tokens, roles, credits = _zero(), {}, {}
    credits_known = True
    for value in metrics:
        for key in tokens:
            tokens[key] += value["tokens"].get(key, 0)
        for name, usage in value.get("roles", {}).items():
            target = roles.setdefault(name, _zero())
            for key in target:
                target[key] += usage.get(key, 0)
        for name, amount in value.get("credits", {}).items():
            if name in {"total", "complete"}:
                continue
            if amount is None:
                credits_known = False
                credits[name] = None
            elif credits.get(name, 0.0) is not None:
                credits[name] = credits.get(name, 0.0) + amount
    credits_total = sum(amount for amount in credits.values() if amount is not None)
    starts = [_parse_time(value.get("startedAt")) for value in metrics]
    ends = [_parse_time(value.get("endedAt")) for value in metrics]
    starts = [moment for moment in starts if moment]
    ends = [moment for moment in ends if moment]
    readings = []
    for value in metrics:
        for key in ("first", "last"):
            reading = (value.get("quota") or {}).get(key)
            if reading:
                readings.append(reading)
    readings.sort(key=lambda reading: reading[0])
    statuses = {}
    for problem in problems:
        statuses[problem["status"]] = statuses.get(problem["status"], 0) + 1
    working = [problem["workingSeconds"] for problem in problems]
    solved_working = [problem["workingSeconds"] for problem in problems if problem["solved"]]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "batch": {key: manifest.get(key) for key in ("id", "folder", "folderPath", "origin", "createdAt", "workflow",
                                                   "settings", "workflowOptions", "customPrompts", "sources")},
        "writtenAt": now.isoformat(),
        "problems": problems,
        "solved": sum(1 for problem in problems if problem["solved"]),
        "total": len(problems),
        "statuses": statuses,
        "settings": _common_settings(metrics, manifest),
        "tokens": tokens,
        "roles": roles,
        "credits": {"byRole": {name: (round(amount, 1) if amount is not None else None)
                               for name, amount in credits.items()},
                    "total": round(credits_total, 1), "complete": credits_known},
        "subagentThreads": sum(value.get("subagentThreads", 0) for value in metrics),
        "time": {
            "startedAt": min(starts).isoformat() if starts else "",
            "endedAt": max(ends).isoformat() if ends else "",
            "wallSeconds": (max(ends) - min(starts)).total_seconds() if starts and ends else 0.0,
            "workingSeconds": sum(working),
            "medianWorkingSeconds": statistics.median(working) if working else 0.0,
            "solvedMedianSeconds": statistics.median(solved_working) if solved_working else None,
            "solvedMaxSeconds": max(solved_working) if solved_working else None,
            "roleSeconds": _sum_maps(problem["roleSeconds"] for problem in problems),
        },
        "quota": {"first": readings[0] if readings else None, "last": readings[-1] if readings else None},
        "activity": _sum_maps(problem["activity"] for problem in problems),
        "runs": metrics,
    }


SETTING_ROWS = (
    ("authorWorkflow", "Workflow"),
    ("author", "Author"),
    ("critic", "Critic"),
    ("latexWriter", "LaTeX writer"),
    ("skipStatementReview", "Statement review"),
    ("speedMode", "Speed"),
    ("thinkingHours", "Thinking-time limit per job"),
    ("reasoningSummary", "Reasoning summaries"),
    ("fileManagement", "Research files"),
)


def _setting_value(settings, key):
    if key == "author":
        return f"{settings.get('authorModel', '?')}, effort {settings.get('authorEffort') or settings.get('reasoningEffort', '?')}"
    if key == "critic":
        return (f"{settings.get('criticModel', '?')}, effort {settings.get('criticEffort') or settings.get('reasoningEffort', '?')}, "
                f"up to {settings.get('criticRounds', '?')} rounds")
    if key == "latexWriter":
        if settings.get("latexWriter", True) is False:
            return "off (the job ends with the approved proof in final-proof.md)"
        return f"on: {settings.get('writerModel', '?')}, effort {settings.get('writerEffort') or settings.get('reasoningEffort', '?')}"
    if key == "skipStatementReview":
        return "skipped" if settings.get("skipStatementReview") else (
            f"{settings.get('reviewModel', '?')}, effort {settings.get('reviewEffort', '?')}")
    if key == "thinkingHours":
        hours = settings.get("thinkingHours")
        return f"{hours:g} h" if isinstance(hours, (int, float)) else "?"
    if key == "fileManagement":
        return "on" if settings.get("fileManagement") else "off"
    if key == "authorWorkflow":
        workflow = settings.get("authorWorkflow") or "author_critic"
        return f"{workflow} (cost-optimized)" if workflow == "author_critic_cheap" else workflow
    value = settings.get(key)
    return "?" if value is None else str(value)


def _common_settings(metrics, manifest):
    """One value per setting, or how the jobs differ."""

    rows = []
    for key, label in SETTING_ROWS:
        values = {}
        for value in metrics:
            if value.get("settings"):
                rendered = _setting_value(value["settings"], key)
                values[rendered] = values.get(rendered, 0) + 1
        if not values and manifest.get("settings"):
            values = {_setting_value(manifest["settings"], key): 1}
        if not values:
            continue
        if len(values) == 1:
            rows.append([label, next(iter(values))])
        else:
            rows.append([label, "varies: " + "; ".join(f"{text} ({count} jobs)" for text, count in values.items())])
    if manifest.get("customPrompts"):
        rows.append(["Prompts", "custom " + ", ".join(manifest["customPrompts"]) + "; others are workflow defaults"])
    options = manifest.get("workflowOptions") or {}
    if options:
        rows.append(["Workflow options", _options_text(options)])
    return rows


def _options_text(options):
    parts = []
    names = (
        ("compaction_tokens", "compaction at {:,} tokens"),
        ("checkpoint_tokens", "checkpoint at {:,} tokens"),
        ("subagent_threads", "{} subagent at a time"),
        ("subagent_call_cap", "subagent call cap {}"),
        ("web_actions_per_hour", "{} web actions per hour"),
        ("quota_pause_percent", "pause at {}% quota"),
        ("turn_minutes_cap", "turn cap {} min"),
    )
    for key, template in names:
        if key in options and isinstance(options[key], (int, float)) and not isinstance(options[key], bool):
            if key == "turn_minutes_cap" and not options[key]:
                parts.append("no turn time cap")
                continue
            parts.append(template.format(options[key]))
    if "ultra_effort" in options:
        parts.append(f"ultra sent as {json.dumps(options['ultra_effort'])}")
    if options.get("codex_config"):
        parts.append("Codex prompt and tool trims")
    return "; ".join(parts) or json.dumps(options)


# ---------------------------------------------------------------- Markdown

def _local(text):
    moment = _parse_time(text) if isinstance(text, str) else text
    if not moment:
        return "—"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def _duration(seconds):
    if seconds is None:
        return "—"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def _short(number):
    number = number or 0
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= size:
            return f"{number / size:.{1 if abs(number) < 100 * size else 0}f}{suffix}"
    return f"{number:,}"


def _credit_text(value):
    return "—" if value is None else f"{value:,.0f}"


def _judge_text(judge):
    return " → ".join(judge) if judge else "—"


STATUS_TEXT = {
    "finished": "finished", "running": "running", "unfinished": "unfinished", "paused": "paused",
    "stopped": "stopped", "rejected": "rejected by the critic", "failed": "failed", "missing": "run folder missing",
}


def _status_text(problem):
    status = STATUS_TEXT.get(problem["status"], problem["status"])
    if problem["status"] == "paused" and "quota" in (problem.get("statusDetail") or "").lower():
        status = "paused (usage quota)"
    if problem["solved"] and problem["status"] != "finished":
        status = f"solved, then {status}"
    return status


def render_markdown(data):
    """The human-readable summary of one batch."""

    batch, time, problems = data["batch"], data["time"], data["problems"]
    folder = batch.get("folderPath") or batch.get("folder") or "?"
    running = data["statuses"].get("running", 0)
    lines = [f"# Folder run summary: {batch.get('folder') or batch.get('id')}", ""]
    if running:
        state = (f"{running} of {data['total']} jobs are still running; this file is rewritten whenever a job "
                 "stops working.")
    else:
        state = f"No job of this batch is running (written {_local(data['writtenAt'])})."
    origin = {"web": "web UI", "terminal": "terminal", "manual": "written afterwards for existing runs"}.get(
        batch.get("origin"), batch.get("origin") or "?")
    lines += [
        "| | |", "| --- | --- |",
        f"| Folder | `{folder}` ({data['total']} statement file{'s' if data['total'] != 1 else ''}) |",
        f"| Batch | `{batch.get('id')}` ({origin}) |",
        f"| Started | {_local(time['startedAt'])} |",
        f"| Last activity | {_local(time['endedAt'])} |",
        f"| Status | {state} |",
        "",
        "## Result",
        "",
        f"**{data['solved']} of {data['total']} problems solved** (the critic approved a complete proof, "
        "or a disproof when the statement is false).",
        "",
        "| Outcome | Problems |", "| --- | ---: |",
        f"| Solved | {data['solved']} |",
    ]
    unsolved = {}
    for problem in problems:
        if not problem["solved"]:
            key = _status_text(problem)
            unsolved[key] = unsolved.get(key, 0) + 1
    for text, count in sorted(unsolved.items()):
        lines.append(f"| Not solved: {text} | {count} |")
    reasons = sorted({problem["statusDetail"] for problem in problems
                      if problem["status"] == "paused" and problem.get("statusDetail")})
    if reasons:
        lines += ["", "Pause reason" + ("s" if len(reasons) > 1 else "") + ": " + " ".join(reasons)]
    lines += ["", "## Models and parameters", "", "| Setting | Value |", "| --- | --- |"]
    lines += [f"| {label} | {value} |" for label, value in data["settings"]]
    lines += ["", "## Tokens and credits", "",
              "| Role | Calls | Input | Cached input | Uncached input | Output | Reasoning output | Est. credits |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    by_role = data["credits"]["byRole"]
    for name, label in ROLE_LABELS:
        usage = data["roles"].get(name)
        if not usage:
            continue
        if name == "subagents":
            label = f"Subagents ({data['subagentThreads']} thread{'s' if data['subagentThreads'] != 1 else ''})"
        lines.append(_usage_row(label, usage, by_role.get(name)))
    lines.append(_usage_row("**Total**", data["tokens"], data["credits"]["total"], bold=True))
    total = data["tokens"]
    cached_share = total["cached"] / total["input"] * 100 if total["input"] else 0.0
    lines += ["", f"Input + output: **{total['input'] + total['output']:,} tokens**; "
              f"{cached_share:.1f}% of input tokens were cached. Calls are model calls for the author and its "
              "subagents, and requests for the critic and the LaTeX writer (one request can make several "
              "model calls when the model runs tools)."]
    compactions = data["activity"].get("compactions", 0)
    if compactions:
        lines += ["", f"Not included: the {compactions:,} context compaction{'s' if compactions != 1 else ''}. "
                  "Codex does not report their tokens, but the provider counts them, so the real use is "
                  "somewhat higher than these totals."]
    quota = data["quota"]
    if quota["first"] and quota["last"]:
        first, last = quota["first"], quota["last"]
        window = ""
        if not _same_window(first[2], last[2]):
            window = " The window reset during the batch, so the difference understates the use."
        lines += ["", f"Usage quota: the provider's usage window went from {first[1]:g}% "
                  f"({_local(first[0])}) to {last[1]:g}% ({_local(last[0])}), resetting {_epoch_local(last[2])}. "
                  "The window is shared by everything on the account, including other jobs and chats." + window]
    lines += ["", "Estimated credits use the standard-speed rates per million tokens "
              + "; ".join(f"{model}: {rates[0]:g} uncached input, {rates[1]:g} cached input, {rates[2]:g} output"
                          for model, rates in CREDIT_RATES.items())
              + f". Fast speed draws included usage {FAST_USAGE_MULTIPLIER:g} times faster. "
              "They are estimates: the provider does not publish how credits map to the usage window."]
    if not data["credits"]["complete"]:
        lines.append("Some roles use a model without a listed rate; their credits are not estimated.")
    lines += ["", "## Time", "", "| | |", "| --- | --- |",
              f"| Wall clock (first start to last activity) | {_duration(time['wallSeconds'])} |",
              f"| Working time, summed over problems | {_duration(time['workingSeconds'])} |",
              f"| Median working time per problem | {_duration(time['medianWorkingSeconds'])} |"]
    if time["solvedMedianSeconds"] is not None:
        lines.append(f"| Solved problems: median / longest working time | {_duration(time['solvedMedianSeconds'])} / "
                     f"{_duration(time['solvedMaxSeconds'])} |")
    for name, label in ROLE_LABELS:
        seconds = time["roleSeconds"].get(name) or 0
        if seconds >= 60 and name not in {"subagents", "other"}:
            lines.append(f"| {label} time, summed | {_duration(seconds)} |")
    activity = data["activity"]
    if activity:
        lines += ["", "Activity over all jobs: " + _activity_text(activity) + "."]
    lines += ["", "## Problems", "",
              "Judge: the critic's verdict in each round. **yes**: passed unchanged; **repaired**: the critic fixed "
              "bugs itself (an unchanged re-check or the round limit follows); **no**: rejected, back to the author.",
              "",
              "| # | File | Solved | Judge | Status | Working time | Tokens | Output | Est. credits |",
              "| ---: | --- | :---: | --- | --- | ---: | ---: | ---: | ---: |"]
    for index, problem in enumerate(problems, 1):
        tokens = problem["tokens"]
        lines.append(
            f"| {index} | {_cell(problem['sourceFile'] or problem['runs'][-1])} | {'yes' if problem['solved'] else 'no'} | "
            f"{_judge_text(problem['judge'])} | {_cell(_status_text(problem))} | {_duration(problem['workingSeconds'])} | "
            f"{_short(tokens['input'] + tokens['output'])} | {_short(tokens['output'])} | "
            f"{_credit_text(problem['credits'])} |")
    for index, problem in enumerate(problems, 1):
        lines += ["", f"### {index}. {problem['sourceFile'] or problem['runs'][-1]}"
                  + (f": {problem['title']}" if problem["title"] else ""), ""]
        status = _status_text(problem)
        if problem.get("statusDetail") and problem["status"] != "finished":
            status += f". {problem['statusDetail']}"
        lines.append(f"- Status: {status}")
        lines.append("- Run folder" + ("s" if len(problem["runs"]) > 1 else "") + ": "
                     + ", then ".join(f"`runs/{run}`" for run in problem["runs"]))
        if problem["solved"]:
            if problem["answer"]:
                lines.append(f"- Answer: {problem['answer']}")
            if problem["finalFile"]:
                lines.append(f"- Result: `runs/{problem['finalFile']}`")
        rounds = len(problem["judge"])
        lines.append(f"- Judge: {_judge_text(problem['judge'])} ({rounds} critic round{'s' if rounds != 1 else ''}; "
                     f"{problem['authorSubmissions']} candidate{'s' if problem['authorSubmissions'] != 1 else ''} "
                     "from the author)")
        roles_time = ", ".join(f"{label.lower()} {_duration(problem['roleSeconds'][name])}"
                               for name, label in ROLE_LABELS if problem["roleSeconds"].get(name, 0) >= 30)
        lines.append(f"- Time: {_duration(problem['workingSeconds'])} working"
                     + (f" ({roles_time})" if roles_time else "")
                     + f", {_local(problem['startedAt'])} to {_local(problem['endedAt'])}")
        tokens = problem["tokens"]
        cached = tokens["cached"] / tokens["input"] * 100 if tokens["input"] else 0.0
        lines.append(f"- Tokens: {tokens['input'] + tokens['output']:,} (input {tokens['input']:,}, {cached:.1f}% cached; "
                     f"output {tokens['output']:,}, of which {tokens['reasoning']:,} reasoning); "
                     f"{tokens['calls']:,} call{'s' if tokens['calls'] != 1 else ''}; "
                     f"est. {_credit_text(problem['credits'])} credits")
        parts = []
        for name, label in ROLE_LABELS:
            usage = problem["roles"].get(name)
            if not usage:
                continue
            if name == "subagents":
                label = f"subagents ({problem['subagentThreads']} thread{'s' if problem['subagentThreads'] != 1 else ''})"
            unit = "request" if name in STRUCTURED_ROLE_NAMES else "call"
            parts.append(f"{label.lower() if name != 'subagents' else label} {_short(usage['input'] + usage['output'])} "
                         f"in {usage['calls']} {unit}{'s' if usage['calls'] != 1 else ''}")
        if parts:
            lines.append("- By role: " + "; ".join(parts))
        if problem["activity"]:
            lines.append("- Activity: " + _activity_text(problem["activity"]))
        incomplete = problem.get("incompleteRequests") or {}
        if incomplete:
            lines.append("- Requests without a usage report (killed or failed; their tokens are not counted): "
                         + ", ".join(f"{count} {stage}" for stage, count in sorted(incomplete.items())))
    return "\n".join(lines).rstrip() + "\n"


def _usage_row(label, usage, credits, bold=False):
    uncached = max(0, usage["input"] - usage["cached"])
    cells = [f"{usage['calls']:,}", f"{usage['input']:,}", f"{usage['cached']:,}", f"{uncached:,}",
             f"{usage['output']:,}", f"{usage['reasoning']:,}", _credit_text(credits)]
    if bold:
        cells = [f"**{cell}**" for cell in cells]
    return f"| {label} | " + " | ".join(cells) + " |"


ACTIVITY_NOUNS = (
    ("commands", "shell command", "shell commands"), ("webSearches", "web search", "web searches"),
    ("compactions", "context compaction", "context compactions"),
    ("subagentsStarted", "subagent started", "subagents started"),
    ("checkpoints", "checkpoint request", "checkpoint requests"),
    ("continuations", "continuation message", "continuation messages"),
)


def _activity_text(activity):
    parts = [f"{activity[key]:,} {one if activity[key] == 1 else many}"
             for key, one, many in ACTIVITY_NOUNS if activity.get(key)]
    return ", ".join(parts) or "none recorded"


def _same_window(first, last):
    """Codex reports a window's reset time with a few seconds of jitter."""

    numbers = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (first, last))
    return first == last or (numbers and abs(first - last) < 3600)


def _epoch_local(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "at an unknown time"
    return datetime.fromtimestamp(value, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------- existing runs

def summarize_existing(runs, paths, name=""):
    """Write a summary for existing runs: a batch folder, or run folders.

    Run folders get a new batch (origin "manual") listing them in the order
    given, so later resumes of those jobs keep the summary up to date.
    """

    runs = Path(runs)
    paths = [Path(path).expanduser().resolve() for path in paths]
    batch_root = batches_directory(runs).resolve()
    if len(paths) == 1 and paths[0].parent == batch_root and (paths[0] / MANIFEST_FILENAME).is_file():
        return write_summary(runs, paths[0].name)
    run_ids = []
    for path in paths:
        if path.parent != runs.resolve() or not (path / "transcript.jsonl").is_file():
            raise ValueError(f"Not a run folder inside {runs}: {path}")
        if path.name not in run_ids:
            run_ids.append(path.name)
    if not run_ids:
        raise ValueError("Name a batch folder or at least one run folder.")
    details = [(run_id, _read_json(runs / run_id / "job-settings.json") or {}) for run_id in run_ids]
    details = [(run_id, settings if isinstance(settings, dict) else {}) for run_id, settings in details]
    # The folder's order: by statement file, as a folder run starts them.
    details.sort(key=lambda item: (str(item[1].get("sourceFile") or "\uffff").casefold(), item[0]))
    started = [_first_time(runs / run_id) for run_id in run_ids]
    started = min((moment for moment in started if moment), default=None)
    workflows = {settings.get("authorWorkflow") for _, settings in details}
    workflow = next(iter(workflows)) if len(workflows) == 1 else ""
    options = {}
    if workflow:
        try:
            import workflow_runner
            options = workflow_runner.builtin_workflow(workflow).get("options") or {}
        except Exception:
            options = {}
    manifest = create_batch(
        runs, folder=name or "folder", sources=[settings.get("sourceFile", "") for _, settings in details],
        workflow=workflow or "", origin="manual", workflow_options=options, started_at=started,
    )
    for run_id, settings in details:
        add_run(runs, manifest["id"], run_id, settings.get("sourceFile", ""))
    return write_summary(runs, manifest["id"])


def _first_time(run_dir):
    try:
        with (Path(run_dir) / "transcript.jsonl").open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    moment = _parse_time(json.loads(line).get("time"))
                except (ValueError, AttributeError):
                    continue
                if moment:
                    return moment
    except OSError:
        return None
    return None
