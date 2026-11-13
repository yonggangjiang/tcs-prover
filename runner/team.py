"""TCS Prover's MCP server "tcs" (its service in the runner) and the author coordinator's team.

The author or coordinator of a run reaches TCS Prover through one MCP server, named SERVER_NAME, that its CLI starts
as a stdio process (runner/tcs_mcp.py, a thin bridge). The bridge forwards each request over a local channel to the
Service of the runner process that started the CLI: a TCP listener on 127.0.0.1 with a random port, which answers
only requests that carry the random token the bridge received in its environment (from the CLI's settings for that
server, never from argv). The service holds the tools:
- update_goal (GoalTool): how a Codex author or coordinator under TCS Prover's goal loop ends its goal
  (workflow_runner.goal_session());
- the team tools (Team): team_members, team_assign, team_wait, team_followup and team_stop of the author
  coordinator, whose subagents are TCS Prover team members, each running a model of its own (the Subagent models
  setting: job setting authorSubagentModels, runner option --author-subagent-models).
Every tool's description and argument descriptions are texts of the workflow's prompts (workflow_runner.HARNESS_TEXTS),
so the user can edit them like every other prompt.

A member's assignment is a one-shot run of its model (`codex exec --json` or `claude -p`) in the author's working
folder, with the author's sandbox, network rule and environment filtering (workflow_runner.member_attempt()); its
final reply is its report, saved to team/member-<n>/<k>-report.md there. Member processes are children of the runner:
Pause, Stop, the time limit and a restart stop them with the coordinator (Team.close()); only their files remain.
"""

from collections import deque
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import sys
import threading
import time

from runner import providers

SERVER_NAME = "tcs"
BRIDGE = Path(__file__).resolve().with_name("tcs_mcp.py")
# How Codex (0.160) and Claude Code (2.1.289) name a tool of this MCP server to the model.
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
GOAL_TOOL = "update_goal"
TEAM_TOOLS = ("team_members", "team_assign", "team_wait", "team_followup", "team_stop")
# update_goal's statuses: Codex's own three. Only "complete" ends the goal; TCS Prover records "blocked" and "paused"
# and goes on (the owner's rule: TCS Prover never stops a run for the model; the user pauses it from the job page).
GOAL_STATUSES = ("complete", "blocked", "paused")
GOAL_STATUS_LABEL = "Goal status"
# The longest team_wait, and its default (team-models-spec): 30 minutes, as Codex's coordinator hint asks.
WAIT_MINUTES = 30
# The longest report team_wait returns; a longer one is cut there, with a note naming its saved file.
REPORT_CHARS = 20_000
# The CLIs' time limit for one call of this server's tools (Codex: tool_timeout_sec; Claude Code: the server's
# timeout in ms): longer than the longest team_wait, so a wait always returns before the CLI gives up on it.
TOOL_TIMEOUT_SECONDS = 40 * 60
# How the bridge finds the service: its address and token, in the bridge's environment only.
ADDRESS_ENV = "TCS_MCP_ADDRESS"
TOKEN_ENV = "TCS_MCP_TOKEN"
# The largest request or answer the channel carries (a team_assign holds the coordinator's assignment).
MESSAGE_LIMIT = 16 * 1024 * 1024
# The member records (the author subagent records of workflow_runner.AuthorSubagents, with the member's number
# and model).
MEMBER_LABELS = {"started": "Author subagent started", "finished": "Author subagent finished",
                 "failed": "Author subagent failed"}
TASK_CHARS = 200
TEAM_FOLDER = "team"
# The start of the report of a stopped assignment; {reason} says who stopped it.
STOPPED_REPORT = "This assignment was stopped. {reason}"
# How long after an attention (Team.attention()) a team_wait that starts still ends at once: the coordinator may
# have called it just before TCS Prover sent its instruction, which then waits for the call to end.
ATTENTION_SECONDS = 10


class TeamError(ValueError):
    """A refused tool call: its text is the tool's answer."""


def assignment_id(number, count):
    """The id of member ``number``'s ``count``-th assignment (a follow-up counts as one): "member-<n>-<k>".

    Every record of that assignment carries it as subagentId: its status records, its events and its retry notes.
    Each assignment is a run of its own whose item ids start again (`codex exec --json` numbers them item_0, ...),
    so the page keeps its rows apart by this id, and a later assignment of the member never replaces them.
    """

    return f"member-{number}-{count}"


def validated_team(value, count=None, check_models=False):
    """The Subagent models setting ``value`` as a list of {"model", "effort"} (copies), or None.

    None is "Same as the coordinator": every member runs the coordinator's model. A list needs exactly ``count``
    entries (the job's Author subagents; None: any number of 1 or more), each with a model id and a reasoning
    effort of the shared menu (a retired one is kept; it runs as its replacement). With ``check_models`` each
    model must be one the catalog knows (built-in or a configured API model), else ValueError; the runner leaves
    that to the member's run, which reports a model it cannot use to the coordinator. A ValueError says what is
    wrong in the form's words; providers.StoreUnavailable means the API model store cannot be read right now.
    """

    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError("Subagent models must be Same as the coordinator or a model for each author subagent.")
    if count is not None and len(value) != count:
        raise ValueError(f"Choose a model for each of the {count} author subagent{'s' if count != 1 else ''} "
                         f"({len(value)} chosen).")
    members = []
    for number, entry in enumerate(value, 1):
        if not isinstance(entry, dict) or set(entry) != {"model", "effort"}:
            raise ValueError(f"Author subagent {number} needs a model and a reasoning effort.")
        model, effort = entry["model"], entry["effort"]
        if not isinstance(model, str) or not model or (check_models and not providers.is_known(model)):
            raise ValueError(f"Choose an available model for author subagent {number}.")
        if effort not in providers.CODEX_EFFORTS and effort not in providers.RETIRED_EFFORTS:
            raise ValueError(f"Choose a valid reasoning effort for author subagent {number}.")
        members.append({"model": model, "effort": effort})
    return members


def member_description(model):
    """How the team names one member's model: {"modelLabel", "program", "connection"}.

    ``program`` is the CLI that runs a built-in model ("Codex CLI", "Claude Code"), or for an API model
    'API connection "<its connection's label>"'; ``connection`` is that label (None for a built-in model). The team
    note, team_members and the job's authorTeam use these words. A model the catalog does not know is named by its
    id, with the program "unknown model" (its run then fails as a configuration problem).
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


def team_members(models, coordinator_model, coordinator_effort, count):
    """The team of ``count`` members: [{"member", "model", "effort", "modelLabel", "program", "connection"}].

    ``models`` is the Subagent models setting (validated_team()); None gives every member the coordinator's model
    and reasoning effort.
    """

    chosen = models if models is not None else [{"model": coordinator_model, "effort": coordinator_effort}] * count
    return [{"member": number, "model": entry["model"], "effort": entry["effort"], **member_description(entry["model"])}
            for number, entry in enumerate(chosen, 1)]


def team_note(template, members, render):
    """The team note (the team_note text with {members}, one line per member) for the coordinator's first prompt."""

    lines = "\n".join(f"- member {item['member']}: {item['modelLabel']} ({item['program']})" for item in members)
    return render(template, {"members": lines})


def _task_line(text):
    line = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    return line if len(line) <= TASK_CHARS else line[:TASK_CHARS - 1].rstrip() + "…"


def _clock(seconds):
    """A wall-clock time for team_members: "14:05 UTC"."""

    return time.strftime("%H:%M UTC", time.gmtime(seconds))


def _text_result(text, error=False):
    return {"content": [{"type": "text", "text": str(text)}], "isError": bool(error)}


class Service:
    """The runner's end of TCS Prover's MCP server: a local listener that runs the tools the bridge forwards.

    Providers (GoalTool, Team) register their tools with add(); a tool name is served by the provider that added
    it last, and remove() takes a provider's tools away again (a later attempt of the author registers anew). Each
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
        """The mcp_servers entry of a Codex thread's settings for this server (approval "never" runs its tools)."""

        names = self.names()
        return {"command": sys.executable, "args": ["-I", str(BRIDGE)], "env": self.environment(),
                "required": True, "tool_timeout_sec": TOOL_TIMEOUT_SECONDS, "enabled_tools": names,
                "tools": {name: {"approval_mode": "approve"} for name in names}}

    def claude_config(self):
        """The --mcp-config of a Claude Code CLI with this server (Claude Code 2.1.289: "timeout" in ms per call)."""

        return {"mcpServers": {SERVER_NAME: {"type": "stdio", "command": sys.executable,
                                             "args": ["-I", str(BRIDGE)], "env": self.environment(),
                                             "timeout": TOOL_TIMEOUT_SECONDS * 1000}}}

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
    """update_goal of a Codex author or coordinator under TCS Prover's goal loop (workflow_runner.goal_session()).

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
        # The member's last conversation, which team_followup continues: {"session", "harness"}, or None.
        self.conversation = None


class Team:
    """The author coordinator's team members and its tools (TEAM_TOOLS).

    ``members`` is team_members(); ``attempt(member, prompt, conversation, process)`` runs one attempt of an
    assignment and returns the member's final reply (workflow_runner.member_attempt(); ``conversation`` is a
    MemberConversation, ``process(popen)`` registers the CLI so that a stop can end it). A failed attempt is retried
    every ``retry_seconds`` without limit, continuing the member's conversation when the attempt worked in it,
    unless ``configuration(error)`` says only a change of settings can fix it: that failure is reported to the
    coordinator and the member becomes idle. ``emit(kind, **fields)`` records (each record of an assignment with its
    id, assignment_id(), which ``attempt`` finds as ``member.assignment``); ``directory`` is the shared working
    folder, where reports are saved. close() stops every member (each record says why) and ends every wait.
    """

    def __init__(self, members, *, prompts, directory, attempt, configuration, emit, render, retry_note,
                 retry_seconds=60, stop_process=None, conversation=None, discard=None, unavailable=None):
        self.members = {entry["member"]: _Member(entry) for entry in members}
        self.prompts, self.directory = prompts, Path(directory)
        self.attempt, self.configuration, self.emit, self.render = attempt, configuration, emit, render
        self.retry_note, self.retry_seconds = retry_note, retry_seconds
        self.stop_process = stop_process or (lambda process: process.kill())
        self.new_conversation = conversation or (lambda member, fork_from: {"fork_from": fork_from})
        self.discard = discard or (lambda: None)
        # Whether a failure says that the conversation the attempt continued is gone.
        self.unavailable = unavailable or (lambda error: False)
        self.cond = threading.Condition()
        self.done = deque()
        self.attention_reason = None  # (reason, time.monotonic()) of the latest attention
        self.closed = None

    # The tools, as the coordinator's CLI lists them.
    def tools(self):
        p = self.prompts

        def member(name):
            return {"type": "integer", "minimum": 1, "maximum": len(self.members), "description": p[name]}

        def schema(properties, required):
            return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}

        return [
            {"name": "team_members", "description": p["team_tool_members"], "inputSchema": schema({}, [])},
            {"name": "team_assign", "description": p["team_tool_assign"], "inputSchema": schema({
                "member": member("team_tool_assign_member"),
                "assignment": {"type": "string", "description": p["team_tool_assign_assignment"]}},
                ["member", "assignment"])},
            {"name": "team_wait", "description": p["team_tool_wait"], "inputSchema": schema({
                "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": WAIT_MINUTES,
                                    "description": p["team_tool_wait_timeout_minutes"]}}, [])},
            {"name": "team_followup", "description": p["team_tool_followup"], "inputSchema": schema({
                "member": member("team_tool_followup_member"),
                "message": {"type": "string", "description": p["team_tool_followup_message"]}},
                ["member", "message"])},
            {"name": "team_stop", "description": p["team_tool_stop"], "inputSchema": schema({
                "member": member("team_tool_stop_member")}, ["member"])},
        ]

    def call(self, name, arguments):
        if name == "team_members":
            return self.listing()
        if name == "team_assign":
            return self.assign(self._number(arguments.get("member")), arguments.get("assignment"))
        if name == "team_wait":
            return self.wait(arguments.get("timeout_minutes", WAIT_MINUTES))
        if name == "team_followup":
            return self.followup(self._number(arguments.get("member")), arguments.get("message"))
        if name == "team_stop":
            return self.stop(self._number(arguments.get("member")))
        raise TeamError(f"Unknown tool {str(name)[:80]!r}.")

    def _number(self, value):
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, bool) or not isinstance(value, int) or value not in self.members:
            raise TeamError(f"There is no member {str(value)[:20]}; the members are 1 to {len(self.members)}.")
        return value

    def _state_line(self, member):
        if member.state == "idle":
            state = "idle"
        elif member.state == "stopping":
            state = "stopping"
        else:
            state = f'working on "{member.task}" since {_clock(member.since)}'
        return f"member {member.number}: {member.label} ({member.program}): {state}"

    def listing(self):
        with self.cond:
            return "\n".join(self._state_line(member) for member in self.members.values())

    def running(self):
        return sum(1 for member in self.members.values() if member.state != "idle")

    def _record(self, member, state, text, **fields):
        self.emit("status", label=MEMBER_LABELS[state], authorSubagent=True,
                  subagentId=member.assignment, subagentTask=member.task,
                  subagentState=state, subagentsRunning=self.running(), subagentMember=member.number,
                  subagentModel=member.label, text=text, **fields)

    def assign(self, number, assignment, fork_from=None, kind="assignment"):
        if not isinstance(assignment, str) or not assignment.strip():
            raise TeamError(f"The {kind} is empty; write what member {number} should do.")
        with self.cond:
            if self.closed is not None:
                raise TeamError(f"The team has stopped: {self.closed}")
            member = self.members[number]
            if member.state != "idle":
                raise TeamError(f"Member {number} is {'stopping' if member.state == 'stopping' else 'working'}"
                                f" on \"{member.task}\"; wait for its report, or stop it with team_stop first.")
            member.state, member.task, member.since = "working", _task_line(assignment), time.time()
            member.count += 1
            # Fixed until this assignment's report: a new one starts only when the member is idle again.
            member.assignment = assignment_id(number, member.count)
            member.cancel, member.stop_reason, member.process = threading.Event(), None, None
            prompt = self.render(self.prompts["team_member"], {"assignment": assignment})
            member.thread = threading.Thread(target=self._run, args=(member, member.count, prompt, fork_from),
                                             name=f"team-member-{number}", daemon=True)
            self._record(member, "started", member.task)
            member.thread.start()
        return (f"Member {number} started the {kind} \"{member.task}\" at {_clock(member.since)}. Collect its report "
                "with team_wait.")

    def followup(self, number, message):
        with self.cond:
            member = self.members[number]
            if member.state != "idle":
                raise TeamError(f"Member {number} is working on \"{member.task}\"; wait for its report, or stop it "
                                "with team_stop first.")
            conversation = member.conversation
        if not conversation or not conversation.get("session"):
            raise TeamError(f"Member {number} has no conversation that can be continued (it has had no assignment, "
                            "or its last one did not start a conversation); give it a new assignment with "
                            "team_assign.")
        return self.assign(number, message, fork_from=conversation, kind="follow-up")

    def stop(self, number):
        with self.cond:
            member = self.members[number]
            if member.state == "idle":
                raise TeamError(f"Member {number} is idle; there is nothing to stop.")
            member.state, member.stop_reason = "stopping", "The coordinator stopped it with team_stop."
            member.cancel.set()
            process = member.process
        if process is not None:
            self.stop_process(process)
        return f"Member {number} is stopping; its report, which says it was stopped, comes with team_wait."

    def wait(self, minutes):
        if isinstance(minutes, str) and minutes.strip().isdigit():
            minutes = int(minutes.strip())
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes <= WAIT_MINUTES:
            raise TeamError(f"timeout_minutes must be 1 to {WAIT_MINUTES}.")
        deadline = time.monotonic() + minutes * 60
        with self.cond:
            if self.attention_reason is not None and time.monotonic() - self.attention_reason[1] > ATTENTION_SECONDS:
                self.attention_reason = None  # It reached the coordinator before this wait began.
            while True:
                if self.done:
                    reports = list(self.done)
                    self.done.clear()
                    return "\n\n".join(reports)
                if self.closed is not None:
                    return f"The team has stopped: {self.closed}"
                if self.attention_reason is not None:
                    reason, self.attention_reason = self.attention_reason[0], None
                    return (f"The wait ended early: TCS Prover sent you {reason}, which follows this result.\n"
                            + self.listing())
                if not self.running():
                    return ("No member is working and no report is waiting; give a member an assignment with "
                            "team_assign.\n" + self.listing())
                left = deadline - time.monotonic()
                if left <= 0:
                    return f"No assignment finished within {minutes:g} minutes.\n" + self.listing()
                self.cond.wait(min(left, 5))

    def attention(self, reason):
        """End a running team_wait early: TCS Prover sends the coordinator ``reason`` (a live instruction, ...)."""

        with self.cond:
            self.attention_reason = (reason, time.monotonic())
            self.cond.notify_all()

    def close(self, reason):
        """Stop every member (each one's record says ``reason``) and end every wait; the team takes no more work."""

        with self.cond:
            if self.closed is not None:
                return
            self.closed = reason
            working = [member for member in self.members.values() if member.state != "idle"]
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
        while True:
            conversation = self.new_conversation(member, base)
            try:
                reply = self.attempt(member, instruction, conversation, self._register(member, cancel))
                if not cancel.is_set():
                    break
            except Exception as exc:  # noqa: BLE001 - every failure is the member's, reported or retried.
                if not cancel.is_set() and self.unavailable(exc) and base is not fork_from:
                    # The conversation a retry continued is gone: the assignment runs again from its start.
                    base, instruction = fork_from, prompt
                    self.emit("diagnostic", text=f"Member {member.number} ({member.label}): its conversation could "
                                                 "not be continued, so the assignment runs again from its start.",
                              subagentMember=member.number, subagentModel=member.label,
                              subagentId=assignment_id(member.number, count))
                    continue
                if not cancel.is_set() and self.unavailable(exc):
                    failure = f"its conversation could not be continued ({str(exc).strip()[:300]})"
                    break
                if not cancel.is_set():
                    problem = self.configuration(exc, member)
                    if problem:
                        failure = problem
                        break
                    worked = bool(getattr(conversation, "session_id", None) and getattr(conversation, "output", False)
                                  and getattr(conversation, "confirmed", False))
                    self.emit("diagnostic", text=f"Member {member.number} ({member.label}): a model call failed "
                                                 f"({str(exc).strip().splitlines()[0][:300] if str(exc).strip() else type(exc).__name__}); "
                                                 f"retrying in {self.retry_seconds:g} s"
                                                 + (", continuing its conversation." if worked else
                                                    ", running the assignment again."),
                              subagentMember=member.number, subagentModel=member.label,
                              subagentId=assignment_id(member.number, count))
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
        relative = f"{TEAM_FOLDER}/member-{member.number}/{count}-report.md"
        with self.cond:
            member.state, member.process, member.thread = "idle", None, None
            closing = self.closed is not None
            shown = text if len(text) <= REPORT_CHARS else (
                text[:REPORT_CHARS] + f"\n[The report is cut here at {REPORT_CHARS:,} characters; the whole report is "
                                      f"in {relative}.]")
            head = (f"Member {member.number} ({member.label}) "
                    + ("stopped" if stopped else "failed" if state == "failed" else "finished")
                    + f" the assignment \"{member.task}\".")
            where = f"Report file: {relative}" if path else f"The report could not be saved ({problem})."
            if not closing:
                self.done.append(f"{head}\n{where}\nREPORT:\n{shown}")
            self._record(member, state, (failure if state == "failed" else member.task)[:2000] or member.task,
                         **({"subagentReport": relative} if path else {}))
            self.cond.notify_all()

    def _save(self, number, count, text):
        """Save a report to team/member-<n>/<k>-report.md (owner-only, never through a link): (path, problem)."""

        try:
            return save_report(self.directory, number, count, text), ""
        except (OSError, ValueError) as exc:
            return None, str(exc)


def save_report(directory, number, count, text):
    """Write one report file under ``directory``, owner-only, refusing any link on the way; return its path."""

    parts = (TEAM_FOLDER, f"member-{number}")
    name = f"{count}-report.md"
    data = text.encode("utf-8")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow and getattr(os, "O_DIRECTORY", 0) and os.open in os.supports_dir_fd:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                try:
                    os.mkdir(part, 0o700, dir_fd=folder)
                except FileExistsError:
                    pass
                inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=folder)
                os.close(folder)
                folder = inner
            try:
                os.unlink(name, dir_fd=folder)  # A file (or link) of that name planted earlier; unlink never follows.
            except FileNotFoundError:
                pass
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600, dir_fd=folder)
        finally:
            os.close(folder)
    else:
        path = Path(directory)
        for part in parts:
            path = path / part
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                if path.is_symlink() or not path.is_dir():
                    raise ValueError(f"{path} is not a folder")
        target = path / name
        if target.is_symlink():
            target.unlink()
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("the report file is not a plain file")
        stream.write(data)
    return Path(directory, *parts, name)
