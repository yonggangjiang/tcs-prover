"""TCS Prover's MCP server "tcs" (its service in the runner) and the author's examiner.

A Codex author under TCS Prover's goal loop reaches TCS Prover through one MCP server, named SERVER_NAME. Codex starts it
as a stdio process (runner/tcs_mcp.py, a thin bridge), which forwards each request over a local channel to the Service
of the runner process that started the CLI: a TCP listener on 127.0.0.1 with a random port, which answers only
requests that carry the random token the bridge received in its environment (from the CLI's settings for that
server, never from argv). Its tool is update_goal (GoalTool), with which such an author ends its goal
(workflow_runner.goal_session()). The tool's description and argument descriptions are texts of the workflow's prompts
(workflow_runner.HARNESS_TEXTS), so the user can edit them like every other prompt.

The author's examiner (Team, see workflow_runner.start_team()) is a seat beside the author whose only task is to
examine the whole run, in a fresh conversation each time. An examination is a one-shot run of the author's model
(`codex exec --json` or `claude -p`) in the author's working folder, with the author's sandbox, network rule and
environment filtering (workflow_runner.member_attempt()); its final reply is its report, saved to
team/member-<n>/<k>-report.md there. Its process is a child of the runner: Pause, Stop, the time limit and a restart
stop it with the author (Team.close()); only its files remain.
"""

from collections import deque
import contextlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import sys
import threading
import time

from runner import providers

SERVER_NAME = "tcs"
BRIDGE = Path(__file__).resolve().with_name("tcs_mcp.py")
GOAL_TOOL = "update_goal"
# update_goal's statuses: Codex's own three. Only "complete" ends the goal; TCS Prover records "blocked" and "paused"
# and goes on (the owner's rule: TCS Prover never stops a run for the model; the user pauses it from the job page).
GOAL_STATUSES = ("complete", "blocked", "paused")
GOAL_STATUS_LABEL = "Goal status"
# Codex's time limit for one call of this server's tools (tool_timeout_sec).
TOOL_TIMEOUT_SECONDS = 40 * 60
# How the bridge finds the service: its address and token, in the bridge's environment only.
ADDRESS_ENV = "TCS_MCP_ADDRESS"
TOKEN_ENV = "TCS_MCP_TOKEN"
# The largest request or answer the channel carries.
MESSAGE_LIMIT = 16 * 1024 * 1024
# The examiner's records (the author subagent records of workflow_runner.AuthorSubagents, with the seat's number
# and model).
MEMBER_LABELS = {"started": "Author subagent started", "finished": "Author subagent finished",
                 "failed": "Author subagent failed"}
TEAM_FOLDER = "team"
# The examiner (Team's ``examiner``): a seat beside the author whose only task is to examine the whole run, in a
# fresh conversation each time, with the author_examiner text as its whole instruction. goal_session reports each
# compaction of the author's conversation (compacted()), and every ``period``-th one starts an examination, unless the
# last one is still running (then the next compaction starts it).
EXAMINER_TASK = "Examination of the whole run"
# The list of examinations and of the author's compactions with their times, which the examiner reads:
# team/timeline.md. It also keeps the count of compactions since the last examination for the job's next runner
# (compactions_since_examination()).
TIMELINE_FILE = "timeline.md"
# Its entries for a compaction and for the start of an examination (Team.compacted(), Team._examine()).
COMPACTION_ENTRY = re.compile(r"^## [^\n]*: author: conversation compacted\b", re.M)
EXAMINATION_ENTRY = re.compile(r"^## [^\n]*: examiner: examination \S+ starts$", re.M)
# A report's file name in its seat's folder (team/member-<n>/<k>-report.md): k is the examination's number.
REPORT_FILE = re.compile(r"([0-9]+)-report\.md")
# A Windows reparse point whose tag has this bit names another file: a symlink, a junction or a similar link, which
# lstat does not follow and Path.is_symlink() does not report for a junction (as ui/related_works.py tests links).
NAME_SURROGATE_TAG = 0x20000000
# The start of the report of a stopped examination; {reason} says who stopped it.
STOPPED_REPORT = "This assignment was stopped. {reason}"


class TeamError(ValueError):
    """A refused tool call: its text is the tool's answer."""


def assignment_id(number, count):
    """The id of seat ``number``'s ``count``-th assignment (an examination): "member-<n>-<k>".

    Every record of that assignment carries it as subagentId: its status records, its events and its retry notes.
    Each assignment is a run of its own whose item ids start again (`codex exec --json` numbers them item_0, ...),
    so the page keeps its rows apart by this id, and a later assignment never replaces them.
    """

    return f"member-{number}-{count}"


def member_description(model):
    """How the team names one member's model: {"modelLabel", "program", "connection"}.

    ``program`` is the CLI that runs a built-in model ("Codex CLI", "Claude Code"), or for an API model
    'API connection "<its connection's label>"'; ``connection`` is that label (None for a built-in model). A model
    the catalog does not know is named by its id, with the program "unknown model" (its run then fails as a
    configuration problem).
    """

    try:
        row = providers.lookup(model)
    except providers.ProviderError:
        row = None
    if row is None:
        return {"modelLabel": str(model), "program": "unknown model", "connection": None}
    if row["provider"] == providers.API:
        connection = str(row.get("sourceLabel") or row["label"])
        return {"modelLabel": row["label"], "program": f'API connection "{connection}"', "connection": connection}
    return {"modelLabel": row["label"], "program": row["providerLabel"], "connection": None}


def team_members(models, model, effort, count):
    """``count`` seats: [{"member", "model", "effort", "modelLabel", "program", "connection"}], numbered from 1.

    ``models`` is a list of {"model", "effort"}, one per seat, or None: every seat runs ``model`` with ``effort``
    (the examiner's seat runs the author's).
    """

    chosen = models if models is not None else [{"model": model, "effort": effort}] * count
    return [{"member": number, "model": entry["model"], "effort": entry["effort"], **member_description(entry["model"])}
            for number, entry in enumerate(chosen, 1)]


def _excerpt(text, limit=600):
    """``text`` on one line, cut to about ``limit`` characters keeping its start and its end: a CLI's error often
    begins with harmless warnings and names its cause last."""

    text = " / ".join(line.strip() for line in str(text).strip().splitlines() if line.strip())
    if len(text) <= limit:
        return text
    head = limit // 4
    return f"{text[:head].rstrip()} … {text[-(limit - head):].lstrip()}"


def _text_result(text, error=False):
    return {"content": [{"type": "text", "text": str(text)}], "isError": bool(error)}


class Service:
    """The runner's end of TCS Prover's MCP server: a local listener that runs the tools the bridge forwards.

    Providers (GoalTool) register their tools with add(); a tool name is served by the provider that added it
    last, and remove() takes a provider's tools away again (a later attempt of the author registers anew). Each
    connection carries one request line {"token", "method": "tools/list" | "tools/call", "params"} and gets one
    answer line: {"result": ...} or {"error": text}. A request without the token is refused and closed.
    """

    def __init__(self):
        self.token = secrets.token_hex(32)
        self._tools, self._order = {}, []
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.address = "127.0.0.1:%d" % self._listener.getsockname()[1]
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="tcs-service", daemon=True)
        self._thread.start()

    def environment(self):
        """What the bridge needs, for its environment only."""

        return {ADDRESS_ENV: self.address, TOKEN_ENV: self.token}

    def add(self, provider):
        with self._lock:
            for definition in provider.tools():
                name = definition["name"]
                if name not in self._order:
                    self._order.append(name)
                self._tools[name] = (provider, definition)

    def remove(self, provider):
        with self._lock:
            for name in [name for name, (owner, _) in self._tools.items() if owner is provider]:
                del self._tools[name]
                self._order.remove(name)

    def names(self):
        with self._lock:
            return list(self._order)

    def definitions(self):
        with self._lock:
            return [self._tools[name][1] for name in self._order]

    def call(self, name, arguments):
        """Run one tool: an MCP tools/call result."""

        with self._lock:
            entry = self._tools.get(name)
        if entry is None:
            return _text_result(f"Unknown tool {str(name)[:80]!r}.", error=True)
        provider = entry[0]
        try:
            return _text_result(provider.call(name, arguments if isinstance(arguments, dict) else {}))
        except TeamError as exc:
            return _text_result(str(exc), error=True)
        except Exception as exc:  # noqa: BLE001 - a tool failure is the tool's answer, never the runner's end.
            return _text_result(f"TCS Prover could not run {name}: {type(exc).__name__}: {exc}", error=True)

    def _serve(self):
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(connection,), name="tcs-call", daemon=True).start()

    def _answer(self, connection):
        with connection:
            try:
                stream = connection.makefile("rwb")
                line = stream.readline(MESSAGE_LIMIT + 1)
                request = json.loads(line.decode("utf-8")) if line.endswith(b"\n") else None
                if (not isinstance(request, dict) or not isinstance(request.get("token"), str)
                        or not hmac.compare_digest(request["token"].encode(), self.token.encode())):
                    return  # Not from this run's bridge: no answer.
                method, params = request.get("method"), request.get("params") or {}
                if method == "tools/list":
                    answer = {"result": {"tools": self.definitions()}}
                elif method == "tools/call" and isinstance(params, dict):
                    answer = {"result": self.call(params.get("name"), params.get("arguments") or {})}
                else:
                    answer = {"error": "Unknown request."}
                stream.write(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")
                stream.flush()
            except (OSError, ValueError, UnicodeError):
                pass

    def codex_server(self):
        """The mcp_servers entry of a Codex thread's settings for this server (approval "never" runs its tools).

        Its tools are direct tools of every model: never deferred and never inside code mode (omit_tools_from
        "deferred" and "code_mode"). Codex 0.160 defers every MCP tool of a model that supports tool search (GPT 6.1
        Sol and GPT 5.5 among them): the model then sees neither the tools nor their descriptions up front, only a
        code-mode model's hint to search ALL_TOOLS, or a direct model's tool_search tool; a code-mode model (GPT
        6.x) would call them from a script of its exec tool (tools.mcp__tcs__<name>). Omitted from both, every model
        gets the namespace mcp__tcs, each tool under its own name (update_goal), and calls a tool directly (captured
        with the real Codex against a scripted model server).
        """

        names = self.names()
        return {"command": sys.executable, "args": ["-I", str(BRIDGE)], "env": self.environment(),
                "required": True, "tool_timeout_sec": TOOL_TIMEOUT_SECONDS, "enabled_tools": names,
                "omit_tools_from": ["deferred", "code_mode"],
                "tools": {name: {"approval_mode": "approve"} for name in names}}

    def close(self):
        self._closed.set()
        try:
            # Wake the accept loop, which a closed listener alone may leave waiting.
            socket.create_connection(("127.0.0.1", int(self.address.rsplit(":", 1)[1])), timeout=1).close()
        except OSError:
            pass
        try:
            self._listener.close()
        except OSError:
            pass


class GoalTool:
    """update_goal of a Codex author under TCS Prover's goal loop (workflow_runner.goal_session()).

    ``prompts`` holds its descriptions (goal_tool_update_goal and its arguments'), ``emit(kind, **fields)`` records,
    ``objective()`` and ``elapsed()`` give what its answer reports. take() returns and clears the status of the
    current turn: "complete" ends the goal when the turn ends with a final reply; "blocked" and "paused" are recorded
    as "Goal status" and the next turn follows.
    """

    def __init__(self, prompts, emit, objective, elapsed):
        self.prompts, self.emit, self.objective, self.elapsed = prompts, emit, objective, elapsed
        self.status, self.lock = None, threading.Lock()

    def tools(self):
        return [{"name": GOAL_TOOL, "description": self.prompts["goal_tool_update_goal"], "inputSchema": {
            "type": "object", "additionalProperties": False, "required": ["status"],
            "properties": {
                "status": {"type": "string", "enum": list(GOAL_STATUSES),
                           "description": self.prompts["goal_tool_update_goal_status"]},
                "note": {"type": "string", "description": self.prompts["goal_tool_update_goal_note"]},
            }}}]

    def call(self, name, arguments):
        status, note = arguments.get("status"), arguments.get("note", "")
        if status not in GOAL_STATUSES:
            raise TeamError(f"status must be one of {', '.join(GOAL_STATUSES)}.")
        if not isinstance(note, str):
            raise TeamError("note must be text.")
        with self.lock:
            self.status = status
        self.emit("status", label=GOAL_STATUS_LABEL, goalStatus=status, note=note.strip()[:4000],
                  text=f"The author set its goal {status}" + (f": {note.strip()[:4000]}" if note.strip() else "."))
        return json.dumps({"goal": {"status": status if status == "complete" else "active",
                                    "objective": self.objective(), "timeUsedSeconds": int(self.elapsed())}},
                          ensure_ascii=False)

    def take(self):
        with self.lock:
            status, self.status = self.status, None
            return status


class _Member:
    def __init__(self, entry):
        self.number, self.model, self.effort = entry["member"], entry["model"], entry["effort"]
        self.label, self.program = entry["modelLabel"], entry["program"]
        self.state, self.task, self.since, self.count = "idle", "", None, 0
        # The id of its current (or last) assignment, assignment_id(); set as an assignment starts.
        self.assignment = None
        self.thread, self.cancel, self.process = None, threading.Event(), None
        self.stop_reason = None
        # The seat's last conversation: {"session", "harness"}, or None.
        self.conversation = None


class Team:
    """The author's examiner: its seat, its examinations and their reports.

    ``members`` is empty and ``lone`` true (a lone author's examiner, start_team(lone=True)): the author has no
    other seats. ``examiner`` is the seat (team_members()); without the team_examiner text of ``prompts`` (the
    author_examiner text, which start_team() passes under that name) there is none. ``attempt(member, prompt,
    conversation, process)`` runs one attempt of an examination and returns the examiner's final reply
    (workflow_runner.member_attempt(); ``conversation`` is a MemberConversation, ``process(popen)`` registers the
    CLI so that a stop can end it). A failed attempt is retried every ``retry_seconds`` without limit, continuing its
    conversation when the attempt worked in it, unless ``configuration(error, member)`` names a failure that ends the
    examination at once (a setting the examiner cannot work with, as text): its report then says "The assignment
    failed: <the text>". ``emit(kind, **fields)`` records (each record of an examination with its id,
    assignment_id(), which ``attempt`` finds as ``member.assignment``; a failed attempt's note carries the failure in
    plain words and the provider's own, ``describe(error)``, and when it is retried); ``directory`` is the author's
    working folder, where reports are saved. compacted() counts a compaction of the author's conversation and starts
    an examination every ``period`` of them (a whole number, 1 or more), counting on from the compactions the job's
    earlier runners recorded since their last examination (team/timeline.md); examine() starts one at once;
    take_reports() collects the reports that finished; close() stops the examiner (its record says why).
    """

    def __init__(self, members, *, prompts, directory, attempt, configuration, emit, render, retry_note,
                 retry_seconds=60, stop_process=None, conversation=None, discard=None, unavailable=None,
                 describe=None, examiner=None, lone=True, period=1):
        self.members = {entry["member"]: _Member(entry) for entry in members}
        self.examiner = (_Member(examiner) if examiner and str(prompts.get("team_examiner") or "").strip()
                         and examiner["member"] not in self.members else None)
        self.lone = lone
        self.prompts, self.directory = prompts, Path(directory)
        # How many compactions start an examination, and how many happened since the last one started.
        self.period = max(1, int(period))
        self.compactions = compactions_since_examination(self.directory) if self.examiner is not None else 0
        self.attempt, self.configuration, self.emit, self.render = attempt, configuration, emit, render
        self.retry_note, self.retry_seconds = retry_note, retry_seconds
        self.stop_process = stop_process or (lambda process: process.kill())
        self.new_conversation = conversation or (lambda member, fork_from: {"fork_from": fork_from})
        self.discard = discard or (lambda: None)
        # Whether a failure says that the conversation the attempt continued is gone.
        self.unavailable = unavailable or (lambda error: False)
        # A failed attempt in plain words and the provider's own line (workflow_runner.describe_failure()): the server
        # lists an examiner whose model calls keep failing the same way under Needs your attention (ui/server.py
        # App._follow_failures()).
        self.describe = describe or (lambda error: ((str(error).strip().splitlines() or [type(error).__name__])[0], ""))
        self.cond = threading.Condition()
        self.done = deque()
        self.closed = None

    def running(self):
        return sum(1 for member in self.members.values() if member.state != "idle")

    def _record(self, member, state, text, **fields):
        self.emit("status", label=MEMBER_LABELS[state], authorSubagent=True,
                  subagentId=member.assignment, subagentTask=member.task,
                  subagentState=state, subagentsRunning=self.running(), subagentMember=member.number,
                  subagentModel=member.label, text=text,
                  **({"subagentExaminer": True} if member is self.examiner else {}), **fields)

    def _examine(self):
        """Start an examination unless one is running or the examiner has stopped; called with the lock held. True
        when one started."""

        seat = self.examiner
        if seat is None or self.closed is not None or seat.state != "idle":
            return False
        seat.state, seat.task, seat.since = "working", EXAMINER_TASK, time.time()
        seat.count = max(seat.count, highest_report(self.directory, seat.number)) + 1
        seat.assignment = assignment_id(seat.number, seat.count)
        try:
            reserve_report(self.directory, seat.number, seat.count)
        except (OSError, ValueError):
            pass
        seat.cancel, seat.stop_reason, seat.process = threading.Event(), None, None
        seat.thread = threading.Thread(target=self._run, args=(seat, seat.count, self.prompts["team_examiner"], None),
                                       name="team-examiner", daemon=True)
        self._record(seat, "started", seat.task)
        seat.thread.start()
        self._timeline(f"examiner: examination {seat.assignment} starts")
        return True

    def _timeline(self, head, body=""):
        """Add an entry to team/timeline.md, which the examiner reads (best effort)."""

        if self.examiner is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        try:
            append_timeline(self.directory, f"\n## {stamp}: {head}\n" + (f"\n{body.strip()}\n" if body.strip() else ""))
        except (OSError, ValueError):
            pass

    def examine(self):
        """Start an examination now, unless the last one is still running or the examiner has stopped. True when one
        started."""

        with self.cond:
            started = self._examine()
            if started:
                self.compactions = 0
            return started

    def compacted(self):
        """The author's conversation was compacted (goal_session, through workflow_runner.examine_after_compaction()):
        count it in the timeline, and start an examination once ``period`` compactions have passed since the last one
        started. "started", "counted" (not due yet), "busy" (due, but the last examination still runs: the next
        compaction starts it) or "closed" (the examiner has stopped)."""

        with self.cond:
            if self.examiner is None or self.closed is not None:
                return "closed"
            self.compactions += 1
            self._timeline(f"author: conversation compacted ({self.compactions} since the last examination started; "
                           f"an examination starts every {self.period})")
            if self.compactions < self.period:
                return "counted"
            if not self._examine():
                return "busy"
            self.compactions = 0
            return "started"

    def take_reports(self):
        """The reports that finished since the last call, each once: {head, where, text, file}; they reach the
        author as author_examiner_report."""

        with self.cond:
            reports = list(self.done)
            self.done.clear()
        return reports

    def close(self, reason):
        """Stop the examiner (its record says ``reason``); it takes no more work."""

        with self.cond:
            if self.closed is not None:
                return
            self.closed = reason
            working = [member for member in (*self.members.values(), *filter(None, [self.examiner]))
                       if member.state != "idle"]
            for member in working:
                member.state, member.stop_reason = "stopping", reason
                member.cancel.set()
            self.cond.notify_all()
        for member in working:
            if member.process is not None:
                self.stop_process(member.process)
        for member in working:
            if member.thread is not None:
                member.thread.join(timeout=10)
        self.discard()

    def _register(self, member, cancel):
        def process(popen):
            with self.cond:
                member.process = popen
                stopping = cancel.is_set()
            if stopping:
                self.stop_process(popen)
        return process

    def _run(self, member, count, prompt, fork_from):
        cancel = member.cancel
        base = fork_from
        instruction = prompt
        failure = reply = None
        # The attempts of this assignment so far: its retry notes carry the number of the attempt that failed, so that
        # only the first two stay pinned on the job page through a long outage (as the runner's own retries).
        attempts = 0
        while True:
            conversation = self.new_conversation(member, base)
            attempts += 1
            try:
                reply = self.attempt(member, instruction, conversation, self._register(member, cancel))
                if not cancel.is_set():
                    break
            except Exception as exc:  # noqa: BLE001 - every failure is the examiner's, reported or retried.
                if not cancel.is_set() and self.unavailable(exc) and base is not fork_from:
                    # The conversation a retry continued is gone: the assignment runs again from its start.
                    base, instruction = fork_from, prompt
                    self.emit("diagnostic", text=f"Member {member.number} ({member.label}): its conversation could "
                                                 "not be continued, so the assignment runs again from its start.",
                              subagentMember=member.number, subagentModel=member.label,
                              subagentId=assignment_id(member.number, count))
                    continue
                if not cancel.is_set() and self.unavailable(exc):
                    failure = f"its conversation could not be continued ({_excerpt(exc)})"
                    break
                if not cancel.is_set():
                    problem = self.configuration(exc, member)
                    if problem:
                        failure = problem
                        break
                    worked = bool(getattr(conversation, "session_id", None) and getattr(conversation, "output", False)
                                  and getattr(conversation, "confirmed", False))
                    self.emit("diagnostic", text=f"Member {member.number} ({member.label}): a model call failed "
                                                 f"({_excerpt(exc) if str(exc).strip() else type(exc).__name__}); "
                                                 f"retrying in {self.retry_seconds:g} s"
                                                 + (", continuing its conversation." if worked else
                                                    ", running the assignment again."),
                              subagentMember=member.number, subagentModel=member.label,
                              subagentId=assignment_id(member.number, count), attempt=attempts,
                              **self._failure_fields(exc))
                    if worked:
                        base = {"session": conversation.session_id, "harness": getattr(conversation, "harness", None)}
                        instruction = self.retry_note
                    if not cancel.wait(self.retry_seconds):
                        continue
            finally:
                with self.cond:
                    member.process = None
                    if getattr(conversation, "session_id", None) and getattr(conversation, "confirmed", False):
                        member.conversation = {"session": conversation.session_id,
                                               "harness": getattr(conversation, "harness", None)}
            break
        self._finish(member, count, reply, failure)

    def _failure_fields(self, error):
        """A failed attempt's note fields for the server: its failure in plain words, the provider's line and when it
        is retried. A description that fails leaves them out (the note stays)."""

        try:
            plain, reason = self.describe(error)
        except Exception:  # noqa: BLE001 - the note matters more than its fields.
            return {}
        return {"failure": str(plain or "")[:300], "failureReason": str(reason or "")[:300],
                "retryAt": time.time() + self.retry_seconds}

    def _finish(self, member, count, reply, failure):
        stopped = member.cancel.is_set()
        if stopped:
            reason = member.stop_reason or "It was stopped."
            text = STOPPED_REPORT.format(reason=reason) + (f"\n\n{reply}" if reply else "")
            state, failure = "failed", reason
        elif failure:
            text, state = f"The assignment failed: {failure}", "failed"
        else:
            text, state = str(reply or ""), "finished"
        path, problem = self._save(member.number, count, text)
        relative = f"{TEAM_FOLDER}/member-{member.number}/{report_name(count)}"
        with self.cond:
            member.state, member.process, member.thread = "idle", None, None
            closing = self.closed is not None
            outcome = "stopped" if stopped else "failed" if state == "failed" else "finished"
            head = (f"The examiner ({member.label}) {outcome} its examination of the whole run."
                    if state == "finished" else f"The examiner's ({member.label}) examination {outcome}.")
            self._timeline(f"examiner: examination {member.assignment} {outcome}"
                           + (f"; report: {relative}" if path else ""))
            where = f"Report file: {relative}" if path else f"The report could not be saved ({problem})."
            if not closing:
                # Collected by take_reports(), for the author.
                self.done.append({"head": head, "where": where, "text": text, "file": relative if path else None})
            self._record(member, state, (failure if state == "failed" else member.task)[:2000] or member.task,
                         **({"subagentReport": relative} if path else {}))
            self.cond.notify_all()

    def _save(self, number, count, text):
        """Save a report to team/member-<n>/<k>-report.md (owner-only, never through a link): (path, problem)."""

        try:
            return save_report(self.directory, number, count, text), ""
        except (OSError, ValueError) as exc:
            return None, str(exc)


def report_name(count):
    """The file name of a member's ``count``-th report."""

    return f"{count}-report.md"


def _linked(info):
    """Whether lstat information describes a link: a symlink, or a Windows junction or other name-surrogate link."""

    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        and getattr(info, "st_reparse_tag", 0) & NAME_SURROGATE_TAG)


class _MemberFolder:
    """A member's report folder, team/member-<n> under the working folder, reached without following any link.

    Where the system opens files relative to an open folder (``descriptor``, POSIX) every name is used there, so a
    link put in place of a folder after the walk is never followed; elsewhere (Windows) each part of ``path`` was
    checked with lstat, a junction included.
    """

    def __init__(self, descriptor=None, path=None):
        self.descriptor, self.path = descriptor, path

    def _target(self, name):
        return (name, {"dir_fd": self.descriptor}) if self.descriptor is not None else (self.path / name, {})

    def names(self):
        return os.listdir(self.descriptor if self.descriptor is not None else self.path)

    def unlink(self, name):
        target, where = self._target(name)
        os.unlink(target, **where)  # Never follows a link: a planted link is removed itself.

    def create(self, name):
        """A new owner-only file of that name, opened for writing; FileExistsError when the name is taken."""

        target, where = self._target(name)
        return os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, **where)


@contextlib.contextmanager
def _member_folder(directory, number, create=True):
    """The _MemberFolder of member ``number`` under ``directory``; ``create`` makes missing folders, owner-only.

    A part that is a link, a junction or no folder raises ValueError (or OSError where the system refuses to follow
    it), so nothing is ever read or written through a link.
    """

    parts = (TEAM_FOLDER, f"member-{number}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow and getattr(os, "O_DIRECTORY", 0) and os.open in os.supports_dir_fd:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=folder)
                    except FileExistsError:
                        pass
                inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=folder)
                os.close(folder)
                folder = inner
            yield _MemberFolder(descriptor=folder)
        finally:
            os.close(folder)
    else:
        path = Path(directory)
        for part in parts:
            path = path / part
            if create:
                try:
                    os.mkdir(path, 0o700)
                    continue
                except FileExistsError:
                    pass
            info = os.lstat(path)
            if _linked(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{path} is not a folder")
        yield _MemberFolder(path=path)


def highest_report(directory, number):
    """The highest k of member ``number``'s report files <k>-report.md under ``directory``, else 0.

    These are its assignments of the job's earlier runners; a folder that is missing, or that cannot be reached
    without following a link, has none.
    """

    try:
        with _member_folder(directory, number, create=False) as folder:
            names = folder.names()
    except (OSError, ValueError):
        return 0
    return max((int(found.group(1)) for found in map(REPORT_FILE.fullmatch, names) if found), default=0)


def compactions_since_examination(directory):
    """How many compactions of the author's conversation team/timeline.md under ``directory`` records after its last
    examination start (Team.compacted()), so that the job's next runner counts on; 0 without a readable timeline or
    when it is reached through a link."""

    folder = Path(directory, TEAM_FOLDER)
    try:
        info = os.lstat(folder)
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            return 0
        descriptor = os.open(folder / TIMELINE_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return 0
            text = stream.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return 0
    starts = list(EXAMINATION_ENTRY.finditer(text))
    return len(COMPACTION_ENTRY.findall(text, starts[-1].end() if starts else 0))


def append_timeline(directory, text):
    """Append ``text`` to team/timeline.md under ``directory``, owner-only, never through a link."""

    folder = Path(directory, TEAM_FOLDER)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(folder)
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{folder} is not a folder")
    descriptor = os.open(folder / TIMELINE_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
    with os.fdopen(descriptor, "ab") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("the timeline is not a plain file")
        stream.write(text.encode("utf-8"))


def reserve_report(directory, number, count):
    """Create the empty report file of member ``number``'s ``count``-th assignment as the assignment starts.

    Its number then stays taken when the runner ends before the report (highest_report()); save_report() replaces
    the file. A name already taken raises FileExistsError.
    """

    with _member_folder(directory, number) as folder:
        os.close(folder.create(report_name(count)))


def save_report(directory, number, count, text):
    """Write one report file under ``directory``, owner-only, refusing any link on the way; return its path."""

    name = report_name(count)
    data = text.encode("utf-8")
    with _member_folder(directory, number) as folder:
        try:
            # Its reservation (reserve_report()), or a file or link of that name planted earlier: removed first, so
            # the report is a new file and nothing is truncated or written through a link.
            folder.unlink(name)
        except FileNotFoundError:
            pass
        descriptor = folder.create(name)
    with os.fdopen(descriptor, "wb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("the report file is not a plain file")
        stream.write(data)
    return Path(directory, TEAM_FOLDER, f"member-{number}", name)
