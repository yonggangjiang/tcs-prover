#!/usr/bin/env python3
"""Render a TCS Prover transcript.jsonl as a readable terminal narrative."""

import argparse
import json
import secrets
import sys
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
UI_ROOT = ROOT / "transcript_ui"
STAGE_LABELS = {
    "review": "STATEMENT REVIEWER",
    "solve": "PROOF AUTHOR",
    "repair": "PROOF AUTHOR REVISION",
    "critic": "INDEPENDENT CRITIC",
    "final": "LATEX EDITOR",
    "failure": "FAILURE SUMMARY",
}
RESULT_KINDS = {
    "review_result", "author_result", "final_result",
    "failure_result", "partial_result",
}
TOOL_TYPES = {
    "collabAgentToolCall", "collab_agent_tool_call",
    "subAgentActivity", "sub_agent_activity",
    "commandExecution", "command_execution",
    "fileChange", "file_change",
    "mcpToolCall", "mcp_tool_call",
    "dynamicToolCall", "dynamic_tool_call",
    "webSearch", "web_search",
}
COMPLETED_EVENTS = {"item/completed", "item.completed"}
UI_BATCH_LIMIT = 250
UI_SCAN_BYTES = 8 * 1024 * 1024


def transcript_path(value=None):
    """Resolve a transcript file, run directory, or the newest local run."""

    if value:
        path = Path(value).expanduser()
        if path.is_dir():
            path = path / "transcript.jsonl"
        if not path.is_file():
            raise ValueError(f"Transcript does not exist: {path}")
        return path.resolve()

    candidates = []
    runs = ROOT / "runs"
    if runs.is_dir():
        for folder in runs.iterdir():
            try:
                candidate = folder / "transcript.jsonl"
                if candidate.is_file():
                    candidates.append(candidate)
            except OSError:
                continue
    if not candidates:
        raise ValueError("No transcript found. Pass a transcript.jsonl or run directory.")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def plain_text(value):
    """Turn common event payload values into compact readable text."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (plain_text(item) for item in value)))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def one_line(value, limit=240):
    """Collapse whitespace for metadata and tool summaries."""

    result = " ".join(plain_text(value).split())
    if limit and len(result) > limit:
        return result[: max(0, limit - 1)].rstrip() + "…"
    return result


class TranscriptViewer:
    """Incrementally convert stored protocol records into a human narrative."""

    def __init__(
        self, output, *, show_prompts=False, show_tools=True,
        root_only=False, stages=None, max_text=4000,
    ):
        self.output = output
        self.show_prompts = show_prompts
        self.show_tools = show_tools
        self.root_only = root_only
        self.stages = set(stages or [])
        self.max_text = max_text
        self.first_time = None
        self.current_stage = None
        self.partials = {}
        self.seen_tools = set()
        self.goal_status = None
        self.records = 0
        self.shown = 0
        self.malformed = 0

    def _timestamp(self, value):
        try:
            current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return "--:--:--"
        if self.first_time is None:
            self.first_time = current
        elapsed = max(0, int((current - self.first_time).total_seconds()))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _trim(self, value):
        value = plain_text(value)
        if self.max_text and len(value) > self.max_text:
            omitted = len(value) - self.max_text
            return value[:self.max_text].rstrip() + f"\n… [{omitted:,} characters omitted]"
        return value

    def _write(self, record, label, value="", *, root=True):
        stage = str(record.get("stage") or "unknown")
        if self.stages and stage not in self.stages:
            return
        if self.root_only and not root:
            return
        if stage != self.current_stage:
            title = STAGE_LABELS.get(stage, stage.upper())
            self.output.write(f"\n{'=' * 12} {title} {'=' * 12}\n")
            self.current_stage = stage
        actor = "" if root else " [subagent]"
        self.output.write(
            f"\n[{self._timestamp(record.get('time'))}] {label}{actor}\n"
        )
        value = self._trim(value)
        if value:
            for line in value.splitlines():
                self.output.write(f"  {line}\n")
        self.shown += 1

    @staticmethod
    def _event_parts(record):
        event = record.get("event") or {}
        if not isinstance(event, dict):
            return {}, "", {}, {}
        name = event.get("method") or event.get("type") or ""
        params = event.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        item = params.get("item") or event.get("item") or {}
        if not isinstance(item, dict):
            item = {}
        return event, name, params, item

    def malformed_line(self, line_number, line):
        self.records += 1
        self.malformed += 1
        record = {"stage": "unknown", "time": None}
        self._write(
            record, "MALFORMED RECORD",
            f"Line {line_number}: {one_line(line, 300)}",
        )

    def consume(self, record):
        """Consume one decoded transcript record."""

        self.records += 1
        if not isinstance(record, dict):
            self.malformed += 1
            self._write(
                {"stage": "unknown"}, "INVALID RECORD",
                f"Expected a JSON object, got {type(record).__name__}.",
            )
            return

        # Relative times always refer to the beginning of the transcript, even
        # when the first records are hidden by a stage or event filter.
        if self.first_time is None and record.get("time"):
            try:
                self.first_time = datetime.fromisoformat(
                    str(record["time"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                pass

        kind = record.get("kind") or ""
        root = record.get("root") is not False

        if kind == "request":
            metadata = ", ".join(filter(None, [
                one_line(record.get("model")),
                f"effort={record.get('reasoningEffort')}"
                if record.get("reasoningEffort") else "",
                f"speed={record.get('serviceTier')}"
                if record.get("serviceTier") else "",
            ]))
            value = record.get("text", "") if self.show_prompts else (
                f"{metadata}\nPrompt body hidden; use --prompts to show it."
                if metadata else "Prompt body hidden; use --prompts to show it."
            )
            self._write(record, record.get("label") or "PROMPT", value)
            return

        if kind == "critic_result":
            report = record.get("report") or {}
            verdict = str(report.get("verdict") or "returned").upper()
            lines = []
            for check in report.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                lines.append(
                    f"- [{str(check.get('verdict') or '?').upper()}] "
                    f"{check.get('focus') or 'check'}: {one_line(check.get('report'))}"
                )
            if report.get("fixed"):
                lines.append("- The critic changed the candidate; another critic must recheck it.")
            if plain_text(report.get("bugs")):
                lines.extend(["", "Unresolved bugs:", plain_text(report["bugs"])])
            self._write(
                record, f"{record.get('label') or 'CRITIC'} — {verdict}",
                "\n".join(lines),
            )
            return

        if kind in RESULT_KINDS:
            value = record.get("output") if kind in {
                "final_result", "failure_result", "partial_result"
            } else record.get("text")
            self._write(record, record.get("label") or kind.replace("_", " ").upper(), value)
            return

        if kind == "status":
            self._write(record, record.get("label") or "STATUS", record.get("text"))
            return

        if kind in {"diagnostic", "error"}:
            self._write(record, "DIAGNOSTIC", record.get("text"))
            return

        if kind != "codex_event":
            return

        _event, name, params, item = self._event_parts(record)
        item_id = str(params.get("itemId") or item.get("id") or "")
        item_type = item.get("type") or ""

        if name == "item/reasoning/summaryTextDelta":
            key = ("reasoning", item_id)
            self.partials[key] = self.partials.get(key, "") + str(params.get("delta") or "")
            return

        if name == "item/agentMessage/delta":
            key = ("agent", item_id)
            self.partials[key] = self.partials.get(key, "") + str(params.get("delta") or "")
            return

        if name in COMPLETED_EVENTS and item_type == "reasoning":
            key = ("reasoning", item_id)
            value = item.get("text") or plain_text(item.get("summary")) or self.partials.get(key, "")
            self.partials.pop(key, None)
            if plain_text(value):
                self._write(record, "REASONING SUMMARY", value, root=root)
            return

        if name in COMPLETED_EVENTS and item_type in {"agentMessage", "agent_message"}:
            key = ("agent", item_id)
            value = item.get("text") or self.partials.get(key, "")
            self.partials.pop(key, None)
            if plain_text(value):
                phase = str(item.get("phase") or "").upper()
                self._write(record, phase or "MODEL MESSAGE", value, root=root)
            return

        if item_type in TOOL_TYPES and self.show_tools:
            self._tool(record, name, item, item_id, root)
            return

        if name == "thread/goal/updated":
            goal = params.get("goal") or {}
            status = goal.get("status") if isinstance(goal, dict) else None
            if status and status != self.goal_status:
                self.goal_status = status
                details = []
                if goal.get("tokensUsed") is not None:
                    details.append(f"tokens used: {goal['tokensUsed']}")
                if goal.get("timeUsedSeconds") is not None:
                    details.append(f"goal time: {goal['timeUsedSeconds']}s")
                self._write(
                    record, "GOAL STATUS",
                    f"{status}" + (f" ({', '.join(details)})" if details else ""),
                    root=root,
                )
            return

        if name == "turn/completed":
            turn = params.get("turn") or {}
            self._write(
                record, "TURN COMPLETED",
                turn.get("status") if isinstance(turn, dict) else "completed",
                root=root,
            )

    def _tool(self, record, event_name, item, item_id, root):
        """Show useful activity once per tool item, suppressing polling noise."""

        status = str(item.get("status") or "").lower()
        failed = status in {"failed", "error", "cancelled"}
        if item_id in self.seen_tools and not failed:
            return
        self.seen_tools.add(item_id)

        item_type = item.get("type") or "tool"
        action = item.get("tool") or item.get("name") or item.get("command")
        if item_type in {"subAgentActivity", "sub_agent_activity"}:
            action = item.get("kind") or "activity"
            detail = item.get("agentPath") or item.get("agentThreadId") or "subagent"
            self._write(record, "SUBAGENT ACTIVITY", f"{action}: {detail}", root=root)
            return
        if str(action).lower() == "wait" and not failed:
            return

        details = []
        if action:
            details.append(one_line(action, 600))
        prompt = item.get("prompt")
        if prompt:
            details.append(f"Assignment: {one_line(prompt, 1200)}")
        arguments = item.get("arguments") or item.get("args")
        if arguments:
            details.append(f"Arguments: {one_line(arguments, 1200)}")
        agent_path = item.get("agentPath")
        if agent_path:
            details.append(f"Agent: {agent_path}")
        if status:
            details.append(f"Status: {status}")
        if failed:
            error = item.get("error") or item.get("failureReason")
            if error:
                details.append(f"Error: {one_line(error, 1200)}")
        label = "SUBAGENT / TOOL ACTIVITY" if not root else "TOOL ACTIVITY"
        self._write(record, label, "\n".join(details) or item_type, root=root)

    def finish(self):
        """Flush incomplete streaming messages and print a short accounting."""

        for (kind, _item_id), value in list(self.partials.items()):
            if value:
                record = {"stage": self.current_stage or "unknown", "time": None}
                self._write(
                    record,
                    "INCOMPLETE REASONING SUMMARY" if kind == "reasoning"
                    else "INCOMPLETE MODEL MESSAGE",
                    value,
                )
        self.partials.clear()
        self.output.write(
            f"\n---\nRead {self.records:,} records; displayed {self.shown:,} entries"
            f"; malformed {self.malformed:,}.\n"
        )


def records(path, follow=False, poll_seconds=0.5):
    """Yield decoded records and malformed source lines, optionally following."""

    with path.open("r", encoding="utf-8", errors="replace") as source:
        line_number = 0
        while True:
            line = source.readline()
            if line:
                line_number += 1
                try:
                    yield line_number, json.loads(line), None
                except json.JSONDecodeError:
                    yield line_number, None, line.rstrip("\r\n")
                continue
            if not follow:
                return
            time.sleep(poll_seconds)


def ui_entry(record, offset):
    """Convert one stored record into a safe, compact browser timeline item."""

    if not isinstance(record, dict):
        return None
    kind = str(record.get("kind") or "")
    stage = str(record.get("stage") or "unknown")
    base = {
        "id": str(offset),
        "stage": stage,
        "stageLabel": STAGE_LABELS.get(stage, stage.upper()),
        "time": record.get("time") or "",
        "root": record.get("root") is not False,
    }

    def entry(category, label, text="", **extra):
        return {
            **base, "category": category, "label": str(label),
            "text": plain_text(text), **extra,
        }

    if kind == "request":
        return entry(
            "prompt", record.get("label") or "Prompt sent", record.get("text"),
            model=record.get("model") or "",
            effort=record.get("reasoningEffort") or "",
            speed=record.get("serviceTier") or "",
        )
    if kind == "critic_result":
        report = record.get("report") or {}
        if not isinstance(report, dict):
            report = {}
        return entry(
            "critic", record.get("label") or "Critic result",
            report.get("bugs") or (
                "The critic repaired the candidate; a fresh critic will recheck it."
                if report.get("fixed") else "No unresolved bugs."
            ),
            verdict=report.get("verdict") or "",
            fixed=bool(report.get("fixed")),
            checks=report.get("checks") if isinstance(report.get("checks"), list) else [],
        )
    if kind in RESULT_KINDS:
        value = record.get("output") if kind in {
            "final_result", "failure_result", "partial_result"
        } else record.get("text")
        return entry(
            "result", record.get("label") or kind.replace("_", " ").title(),
            value, resultKind=kind,
        )
    if kind == "status":
        return entry("status", record.get("label") or "Status", record.get("text"))
    if kind in {"diagnostic", "error"}:
        return entry("diagnostic", "Diagnostic", record.get("text"))
    if kind != "codex_event":
        return None

    _event, name, params, item = TranscriptViewer._event_parts(record)
    item_type = item.get("type") or ""
    if name in COMPLETED_EVENTS and item_type == "reasoning":
        value = item.get("text") or plain_text(item.get("summary"))
        return entry("reasoning", "Reasoning summary", value) if value else None
    if name in COMPLETED_EVENTS and item_type in {"agentMessage", "agent_message"}:
        value = item.get("text") or ""
        phase = str(item.get("phase") or "Model message").replace("_", " ").title()
        return entry("message", phase, value) if value else None
    if item_type in TOOL_TYPES:
        status = str(item.get("status") or "").lower()
        failed = status in {"failed", "error", "cancelled"}
        if name in COMPLETED_EVENTS and not failed:
            return None
        if item_type in {"subAgentActivity", "sub_agent_activity"}:
            action = item.get("kind") or "activity"
            detail = item.get("agentPath") or item.get("agentThreadId") or "subagent"
            return entry("tool", "Subagent activity", f"{action}: {detail}")
        action = item.get("tool") or item.get("name") or item.get("command") or item_type
        if str(action).lower() == "wait" and not failed:
            return None
        details = [one_line(action, 1000)]
        if item.get("prompt"):
            details.append(f"Assignment: {one_line(item['prompt'], 3000)}")
        arguments = item.get("arguments") or item.get("args")
        if arguments:
            details.append(f"Arguments: {one_line(arguments, 3000)}")
        if status:
            details.append(f"Status: {status}")
        error = item.get("error") or item.get("failureReason")
        if error:
            details.append(f"Error: {one_line(error, 3000)}")
        return entry("tool", "Tool activity", "\n".join(filter(None, details)))
    if name == "thread/goal/updated":
        goal = params.get("goal") or {}
        if isinstance(goal, dict) and goal.get("status"):
            details = [str(goal["status"])]
            if goal.get("tokensUsed") is not None:
                details.append(f"tokens used: {goal['tokensUsed']}")
            if goal.get("timeUsedSeconds") is not None:
                details.append(f"goal time: {goal['timeUsedSeconds']}s")
            return entry("status", "Goal status", " · ".join(details))
    if name == "turn/completed":
        turn = params.get("turn") or {}
        status = turn.get("status") if isinstance(turn, dict) else "completed"
        return entry("status", "Turn completed", status)
    return None


def read_ui_batch(path, offset=0, limit=UI_BATCH_LIMIT):
    """Read one bounded byte range and return only human-meaningful entries."""

    try:
        offset = max(0, int(offset))
        limit = min(UI_BATCH_LIMIT, max(1, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid transcript cursor.") from exc
    size = path.stat().st_size
    if offset > size:
        offset = 0
    entries = []
    scanned = 0
    malformed = 0
    with path.open("rb") as source:
        source.seek(offset)
        while len(entries) < limit and scanned < UI_SCAN_BYTES:
            line_offset = source.tell()
            raw = source.readline()
            if not raw:
                break
            # A live writer may not have finished the last JSON line yet.
            if not raw.endswith((b"\n", b"\r")) and source.tell() >= size:
                source.seek(line_offset)
                break
            offset = source.tell()
            scanned += len(raw)
            try:
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                malformed += 1
                entries.append({
                    "id": str(line_offset), "stage": "unknown",
                    "stageLabel": "UNKNOWN", "time": "", "root": True,
                    "category": "diagnostic", "label": "Malformed record",
                    "text": f"Could not decode JSON near byte {line_offset:,}.",
                })
                continue
            value = ui_entry(record, line_offset)
            if value:
                entries.append(value)
    latest_size = path.stat().st_size
    return {
        "entries": entries,
        "nextOffset": offset,
        "fileSize": latest_size,
        "eof": offset >= latest_size,
        "scannedBytes": scanned,
        "malformed": malformed,
    }


def transcript_metadata(path):
    """Return inexpensive identifying information for the browser header."""

    started_at = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for _ in range(50):
                line = source.readline()
                if not line:
                    break
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("time"):
                    started_at = value["time"]
                    break
    except OSError:
        pass
    return {
        "name": path.parent.name,
        "path": str(path),
        "size": path.stat().st_size,
        "startedAt": started_at,
        "stages": STAGE_LABELS,
    }


class TranscriptHTTPServer(ThreadingHTTPServer):
    """Local-only server carrying one transcript and one random API token."""

    def __init__(self, address, transcript, token):
        super().__init__(address, TranscriptRequestHandler)
        self.transcript = transcript
        self.token = token


class TranscriptRequestHandler(BaseHTTPRequestHandler):
    """Serve the viewer shell and bounded transcript API calls."""

    server_version = "TCS-Transcript-Viewer/1"

    def log_message(self, _format, *_args):
        pass

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status=200):
        self._send(
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8", status,
        )

    def _authorized(self):
        return secrets.compare_digest(
            self.headers.get("X-Transcript-Token", ""), self.server.token
        )

    def do_GET(self):
        request = urlsplit(self.path)
        if request.path.startswith("/api/"):
            if not self._authorized():
                self._json({"error": "Unauthorized local request."}, 403)
                return
            try:
                if request.path == "/api/meta":
                    self._json(transcript_metadata(self.server.transcript))
                    return
                if request.path == "/api/events":
                    query = parse_qs(request.query)
                    self._json(read_ui_batch(
                        self.server.transcript,
                        (query.get("offset") or [0])[0],
                        (query.get("limit") or [UI_BATCH_LIMIT])[0],
                    ))
                    return
                self._json({"error": "Not found."}, 404)
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, 400)
            return

        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        asset = assets.get(request.path)
        if not asset:
            self._send(b"Not found.", "text/plain; charset=utf-8", 404)
            return
        try:
            self._send((UI_ROOT / asset[0]).read_bytes(), asset[1])
        except OSError as exc:
            self._send(str(exc).encode("utf-8"), "text/plain; charset=utf-8", 500)


def serve_ui(path, port=0, open_browser=True):
    """Open the private browser UI and serve until interrupted."""

    token = secrets.token_urlsafe(24)
    server = TranscriptHTTPServer(("127.0.0.1", port), path, token)
    url = f"http://127.0.0.1:{server.server_port}/#{token}"
    print(f"Transcript UI: {url}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nTranscript UI stopped.")
    finally:
        server.server_close()
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Explore a TCS Prover transcript in a local browser UI (default) "
            "or as a terminal narrative, with stages, public reasoning "
            "summaries, model messages, tool activity, and verdicts."
        )
    )
    parser.add_argument(
        "transcript", nargs="?",
        help="transcript.jsonl or its run directory; defaults to the newest run",
    )
    parser.add_argument(
        "--text", action="store_true",
        help="use the terminal narrative instead of the default browser UI",
    )
    parser.add_argument("--no-open", action="store_true", help="serve the UI without opening a browser")
    parser.add_argument("--port", type=int, default=0, help="local UI port; 0 chooses a free port")
    parser.add_argument("-f", "--follow", action="store_true", help="keep watching a live transcript")
    parser.add_argument("-o", "--output", help="write the readable transcript to this UTF-8 file")
    parser.add_argument("--prompts", action="store_true", help="include full prompts sent to each model")
    parser.add_argument("--no-tools", action="store_true", help="hide tool and subagent activity")
    parser.add_argument("--root-only", action="store_true", help="hide events produced inside subagents")
    parser.add_argument(
        "--stage", action="append", choices=sorted(STAGE_LABELS),
        help="show only this stage; repeat to select several",
    )
    parser.add_argument(
        "--max-text", type=int, default=4000, metavar="CHARS",
        help="maximum characters per entry; 0 means unlimited (default: 4000)",
    )
    args = parser.parse_args(argv)
    if args.max_text < 0:
        parser.error("--max-text cannot be negative")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        path = transcript_path(args.transcript)
        terminal_mode = any((
            args.text, args.output, args.follow, args.prompts, args.no_tools,
            args.root_only, args.stage, args.max_text != 4000,
        ))
        if not terminal_mode:
            return serve_ui(path, args.port, not args.no_open)
        destination = Path(args.output).expanduser() if args.output else None
        if destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            output = destination.open("w", encoding="utf-8", newline="\n")
        else:
            output = sys.stdout
        try:
            output.write(f"TCS Prover transcript\nSource: {path}\n")
            viewer = TranscriptViewer(
                output,
                show_prompts=args.prompts,
                show_tools=not args.no_tools,
                root_only=args.root_only,
                stages=args.stage,
                max_text=args.max_text,
            )
            try:
                for line_number, record, malformed in records(path, args.follow):
                    if malformed is not None:
                        viewer.malformed_line(line_number, malformed)
                    else:
                        viewer.consume(record)
            except KeyboardInterrupt:
                output.write("\nStopped following transcript.\n")
            viewer.finish()
        finally:
            if destination:
                output.close()
        if destination:
            print(f"Wrote readable transcript to {destination.resolve()}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
