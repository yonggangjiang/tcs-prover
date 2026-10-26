"""Claude Code CLI transport for structured roles and the persistent proof author.

Provider plumbing only: prompts, defaults and the critic/author semantics stay
in the YAML workflows and workflow_runner. Claude subscription models and
Anthropic-format API models both run through `claude -p`; what Codex provides
natively (goals, live steering, interrupts) is emulated with stream-json
messages as documented on goal_session().

Isolation, verified against Claude Code 2.1.289 (see tests/test_claude_cli.py):
- an explicit --tools list, so product tools (artifacts, workflows, cron,
  skills, remote triggers, ...) are never offered, and no MCP servers;
- --restricted confines Read/Write/Edit to the working directory and ignores
  user, project and local settings files;
- Bash runs only inside the Claude sandbox (failIfUnavailable, no unsandboxed
  escape, no network); CLAUDE_CODE_SUBPROCESS_ENV_SCRUB removes credentials
  from its environment, and the sandbox can neither read nor write TCS
  Prover's private state (the API model store and every settings folder);
- API models use --bare, so the subscription login and the keychain are never
  read. Their gateway settings and key go in an owner-only --settings file in
  the private state folder, because the `claude` launcher may strip
  ANTHROPIC_* variables from the environment. That file exists only while its
  CLI process runs; Stop, or a later runner or server start, removes one left
  behind by a process that was killed before its cleanup.
"""

from collections import deque
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from retrieval import runtime as retrieval_runtime
from runner import providers

EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
SEARCH_TOOLS = ("WebSearch", "WebFetch")
# --bare offers only these built-in tools (Claude Code 2.1.289); Edit also
# creates files. Requesting others there would be silently ignored.
BARE_TOOLS = ("Bash", "Read", "Edit")
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
SUBAGENT_TOOLS = ("Task", "Agent")
# Subagent outcomes, verified against Claude Code 2.1.289: a subagent that an
# API error (rate limit, overload, server error) cut off after some work
# returns that work as an ordinary tool result headed by the error and this
# note; one that failed otherwise returns an error result. A call turned down
# by a limit, a permission rule or an invalid input runs no subagent and is
# answered with an error result containing one of SUBAGENT_REFUSALS. The
# result's subagent_stats.failed counts the subagents that started and failed.
PARTIAL_SUBAGENT_NOTE = "Everything below is PARTIAL output recovered from the agent before it was cut off."
SUBAGENT_REFUSALS = ("limit reached", "available agents:", "denied by permission rule",
                     "cannot launch new agents", "inputvalidationerror")
# Pre-approved writes stay inside the working directory; Edit rules cover Write.
WRITE_RULE = "Edit(./**)"
# --bare needs some API credential to start; with a custom header only this
# public placeholder reaches x-api-key.
API_KEY_PLACEHOLDER = "tcs-prover-placeholder"
# The user's own subscription login may be located through these; API models
# start from an environment without any Claude or Anthropic variable.
SUBSCRIPTION_ENVIRONMENT = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
HISTORICAL_MESSAGE = "Web cutoff timestamps require Codex CLI models for every proof role."
# Informational lines the CLI prints on every run.
HARMLESS_STDERR = ("[claude-code:unrecognized_model]", "Permission mode forced to default")
# Queued steering or re-anchor input normally starts the next turn at once.
QUEUED_INPUT_SECONDS = 30
# Private settings folders are named run-<owner PID>-<random>.
SETTINGS_PREFIX = "run-"
# The goal harness note travels in a file of that folder, never in argv: a
# Windows batch launcher would cut a multi-line argument at its first line.
HARNESS_FILE = "goal-harness.md"
BATCH_LAUNCHERS = (".cmd", ".bat")
# Claude has one public summary level: with Concise or Detailed reasoning
# summaries a subscription model returns Anthropic's summarized thinking
# (verified with Claude Code 2.1.289: the text arrives in the thinking block;
# by default the CLI requests "updates", which leave it empty). API gateways
# may return raw reasoning in the same block, so API models never show it.
REASONING_SUMMARY_LEVELS = ("concise", "detailed")
SUMMARY_DISPLAY = ("--thinking-display", "summarized")
# Claude deletes saved conversations after its cleanupPeriodDays setting.
UNAVAILABLE_SESSION_HINT = (
    " Claude Code no longer has this conversation (it deletes saved conversations after its "
    "cleanupPeriodDays setting, 30 days by default), so automatic retry is off. Choose Continue "
    "proof author to continue the saved work in a new conversation."
)

# Goal emulation (Codex has native goals; Claude does not): every author turn
# ends with this status, requested through --json-schema.
GOAL_TURN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["goal_status", "final_answer", "progress_note"],
    "properties": {
        "goal_status": {"type": "string", "enum": ["complete", "in_progress", "blocked"]},
        "final_answer": {"type": "string"},
        "progress_note": {"type": "string"},
    },
}
GOAL_HARNESS_PROMPT = """TCS PROVER HARNESS NOTE (provider plumbing; the task itself arrives in the user messages)
You are working on one long-running, autonomous goal in a persistent conversation. Nobody answers questions during the work; keep working with your tools.

GOAL OBJECTIVE:
[OBJECTIVE]

End every turn by calling the StructuredOutput tool:
- goal_status "complete" only when the goal objective is fully achieved. final_answer must then be exactly the final response the task instructions ask for (for example "PROOF_FILE: proof.md", or the complete inline proof) and nothing else.
- Otherwise goal_status "in_progress" (more work remains) or "blocked" (no further progress is possible right now), an empty final_answer and a short progress_note. You will then be asked to continue."""


def _runtime_module(runtime):
    if runtime is not None:
        return runtime
    import workflow_runner  # Direct callers; the runner passes itself (it may run as __main__).
    return workflow_runner


def _failure(runtime, name):
    """The runner's error class `name` (ConfigurationError, SafetyStop, ...), or its plain Error."""

    return getattr(runtime, name, runtime.Error)


def executable(runtime):
    """Find the installed Claude CLI, preferring a native binary to a Windows batch launcher."""

    path = shutil.which("claude")
    if path and os.name == "nt":
        path = native_windows_executable(path)
    if not path:
        # Missing for a moment while Claude Code updates itself: a later attempt finds it.
        raise _failure(runtime, "CLIUnavailable")("Claude CLI is not installed or is not on PATH.")
    return path


def native_windows_executable(path, search_path=None):
    """Replace an npm/yarn/pnpm claude.cmd launcher with the claude.exe it starts.

    cmd.exe parses a batch launcher's arguments (a line break ends the
    command line; %, & and ^ are special), so the npm package's own native
    binary, or a claude.exe elsewhere on PATH, runs instead when there is one.
    """

    if os.path.splitext(path)[1].lower() not in BATCH_LAUNCHERS:
        return path
    candidates = [Path(path).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"]
    for entry in (os.environ.get("PATH", "") if search_path is None else search_path).split(os.pathsep):
        if entry.strip('"'):
            candidates.append(Path(entry.strip('"')) / "claude.exe")
    return next((str(candidate) for candidate in candidates
                 if candidate.is_absolute() and candidate.is_file()), path)


def batch_safe(command, runtime):
    """Refuse a line break that a Windows batch launcher would silently cut off."""

    if (os.name == "nt" and os.path.splitext(command[0])[1].lower() in BATCH_LAUNCHERS
            and any("\r" in str(argument) or "\n" in str(argument) for argument in command)):
        raise _failure(runtime, "ConfigurationError")("Claude Code's .cmd launcher cannot pass this request's arguments; "
                            "install the native Claude Code (claude.exe) and put it on PATH.")
    return command


def sandbox_supported():
    """Whether Claude's Bash sandbox can start here (Seatbelt or bubblewrap)."""

    if sys.platform == "darwin":
        return Path("/usr/bin/sandbox-exec").exists() or shutil.which("sandbox-exec") is not None
    if sys.platform.startswith("linux"):
        return all(shutil.which(name) for name in ("bwrap", "socat"))
    return False


def search_is_live(workspace, runtime):
    """Apply the run's saved search mode exactly as the Codex harness does."""

    try:
        return retrieval_runtime.search_mode(workspace) == "live"
    except retrieval_runtime.RetrievalPolicyError as exc:
        raise _failure(runtime, "ConfigurationError")(str(exc)) from exc


def tool_permissions(role, features=(), *, live_search, bash, bare):
    """Return the offered tools and the pre-approved permission rules.

    The author edits files in its run folder; structured roles (reviewer,
    critic, writer, repair) only read. Task (subagents) follows the node's
    multi_agent feature and Bash, always sandboxed, is offered to structured
    roles only with shell_tool. Search tools follow the run's search mode.
    """

    if role == "author":
        tools = [*(["Task"] if "multi_agent" in features else []), *(["Bash"] if bash else []),
                 "Read", "Write", "Edit", "TodoWrite"]
    else:
        tools = ["Read", *(["Task"] if "multi_agent" in features else []),
                 *(["Bash"] if bash and "shell_tool" in features else [])]
    if live_search:
        tools += SEARCH_TOOLS
    if bare:
        tools = [tool for tool in tools if tool in BARE_TOOLS]
    allowed = list(dict.fromkeys(WRITE_RULE if tool in {"Write", "Edit"} else tool for tool in tools))
    return tools, allowed


def hidden_paths(api):
    """Paths the Bash sandbox may neither read nor write.

    TCS Prover's private state always (the key store and every private
    settings folder, including other runs'); for a model served by a
    third-party endpoint also the user's CLI logins.
    """

    paths = providers.private_paths()
    if api:
        paths += providers.user_login_files()
    return list(dict.fromkeys(paths))


def sandbox_settings(hidden):
    """Bash may write only to its working directory and temp space, with no network."""

    hidden = [str(path) for path in hidden]
    return {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "network": {"allowedDomains": [], "strictAllowlist": True},
        "filesystem": {"denyRead": hidden, "denyWrite": hidden},
    }


def gateway_environment(model, key):
    """Settings-file environment that sends every request to the configured endpoint.

    ANTHROPIC_BASE_URL takes no /v1. The key travels only in the configured
    header: x-api-key (ANTHROPIC_API_KEY), Authorization: Bearer
    (ANTHROPIC_AUTH_TOKEN) or any other header (ANTHROPIC_CUSTOM_HEADERS). All
    model aliases point at the configured model so no background request asks
    the gateway for a model it lacks.
    """

    entry = providers.api_model(model)
    if entry is None:
        raise providers.ProviderError(f"Unknown model {model!r}.")
    base = entry["baseUrl"].rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3]
    name = entry["model"]
    environment = {
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": name, "ANTHROPIC_DEFAULT_SONNET_MODEL": name,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": name, "CLAUDE_CODE_SUBAGENT_MODEL": name,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    header = entry["headerName"]
    if header.lower() == "x-api-key":
        environment["ANTHROPIC_API_KEY"] = key
    elif header.lower() == "authorization":
        environment["ANTHROPIC_AUTH_TOKEN"] = key
    else:
        environment["ANTHROPIC_CUSTOM_HEADERS"] = f"{header}: {key}"
        environment["ANTHROPIC_API_KEY"] = API_KEY_PLACEHOLDER
    return environment


def child_environment(base, effort, *, api):
    """Start from the runner environment without any inherited Claude/Anthropic setting."""

    environment = {
        name: value for name, value in base.items()
        if not name.startswith(("ANTHROPIC_", "CLAUDE")) or (not api and name in SUBSCRIPTION_ENVIRONMENT)
    }
    # The environment value takes precedence over --effort, so both agree.
    environment["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return environment


def settings_parent():
    """The owner-only folder, inside the private state folder, of every settings folder."""

    state = providers.private_directory(providers.state_directory())
    return providers.private_directory(state / providers.CLAUDE_SETTINGS)


class PrivateSettings:
    """An owner-only --settings file (and harness note) that lives exactly as long as its CLI process.

    It lives in the private state folder, which no Claude or Codex sandbox may
    read or write, in a folder named after its owner's PID: if this process is
    killed before close(), sweep_stale_settings() removes the folder later.
    """

    def __init__(self, build):
        self.directory = Path(tempfile.mkdtemp(prefix=f"{SETTINGS_PREFIX}{os.getpid()}-", dir=settings_parent()))
        self.path = None
        try:
            if os.name != "nt":
                os.chmod(self.directory, 0o700)
            document = build(self.directory)
            if document:
                self.path = self.write("settings.json", json.dumps(document, ensure_ascii=False))
        except BaseException:
            self.close()
            raise

    def write(self, name, text):
        """Create one more owner-only file in this folder; it is removed with it."""

        path = self.directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        return path

    def close(self):
        if self.path is not None:
            try:
                self.path.unlink()
            except OSError:
                pass
        shutil.rmtree(self.directory, ignore_errors=True)


def _process_alive(pid):
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _windows_process_alive(pid, kernel32=None, last_error=None):
    """Whether a Windows process still runs; asks without signalling (os.kill() would end it)."""

    if kernel32 is None:
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            last_error = ctypes.get_last_error
        except (ImportError, OSError, AttributeError):
            return True  # Cannot tell: keep the folder.
    synchronize, query_limited_information = 0x00100000, 0x1000
    handle = kernel32.OpenProcess(synchronize | query_limited_information, False, pid)
    if not handle:
        # Another user's running process refuses access; any other error means it is gone.
        return last_error() == 5  # ERROR_ACCESS_DENIED
    try:
        # A zero wait times out while the process runs, even when a handle keeps its entry.
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def sweep_stale_settings():
    """Remove private settings folders whose owner process has ended.

    Stop kills a runner that ignores its interrupt (on Windows every runner),
    and a killed process cannot delete the key-bearing file it was using.
    """

    try:
        entries = list((providers.state_directory() / providers.CLAUDE_SETTINGS).iterdir())
    except OSError:
        return
    for entry in entries:
        owner = entry.name[len(SETTINGS_PREFIX):].split("-", 1)[0] if entry.name.startswith(SETTINGS_PREFIX) else ""
        if owner.isdigit() and int(owner) != os.getpid() and not _process_alive(int(owner)):
            shutil.rmtree(entry, ignore_errors=True)


def command_arguments(executable_path, model, effort, tools, allowed, settings_path, *, bare, extra=()):
    """Common flags of every Claude role."""

    return [
        executable_path, "-p", "--output-format", "stream-json", "--verbose",
        "--model", providers.wire_model(model), "--effort", effort,
        # Never load the user's MCP servers, customizations or browser integration.
        "--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG,
        "--safe-mode", "--disable-slash-commands", "--no-chrome", "--restricted",
        # Anything not pre-approved is denied; nothing ever waits for a prompt.
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--tools", ",".join(tools), "--allowedTools", ",".join(allowed),
        *(["--settings", str(settings_path)] if settings_path else []),
        *(["--bare"] if bare else []), *extra,
    ]


def json_line(line):
    """Parse one stream-json line; the CLI may also print plain informational lines."""

    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def harmless(line):
    return any(marker in line for marker in HARMLESS_STDERR)


def unexpected_tools(init, tools):
    """Tools the CLI announced beyond the explicit list (fail closed on surprises)."""

    offered = init.get("tools")
    if not isinstance(offered, list):
        return []
    expected = set(tools) | {STRUCTURED_OUTPUT_TOOL}
    return sorted(str(name) for name in offered if name not in expected)


def cache_usage(usage):
    """Convert Anthropic usage (cached input counted separately) to the shared shape."""

    if not isinstance(usage, dict):
        return None
    try:
        fresh = int(usage.get("input_tokens") or 0)
        read = int(usage.get("cache_read_input_tokens") or 0)
        written = int(usage.get("cache_creation_input_tokens") or 0)
    except (TypeError, ValueError):
        return None
    return {"input_tokens": fresh + read + written, "cached_input_tokens": read,
            "cache_write_input_tokens": written}


def _clip(value, limit):
    value = "" if value is None else str(value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def redact(text, secret, runtime):
    """Remove the model's key and every other secret the runner knows of (diagnostics)."""

    text = str(text or "")
    if secret:
        text = text.replace(secret, "[redacted]")
    redactor = getattr(runtime, "redact", None)
    return redactor(text) if redactor else text


def scrub_record(text, secret, runtime):
    """Event text: the floor emit() applies to records, so a short key never rewrites words.

    Diagnostics (errors raised, stderr tails) keep redact(), which has no
    floor, as on the Codex path.
    """

    text = str(text or "")
    if secret and len(secret) >= getattr(runtime, "MIN_RECORD_SECRET_CHARS", 8):
        text = text.replace(secret, "[redacted]")
    redactor = getattr(runtime, "redacted", None)
    return redactor(text) if redactor else text


def failure_detail(result, stderr_lines, secret, runtime, limit=8000, scrub=None):
    """A short sanitized explanation of a failed Claude request (`scrub` for event text)."""

    parts = []
    if isinstance(result, dict):
        text = result.get("result")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
        errors = result.get("errors")
        if isinstance(errors, list):
            parts.extend(str(error).strip() for error in errors if str(error).strip())
        status = result.get("api_error_status")
        if status:
            parts.append(f"API status {status}.")
        if not parts and result.get("subtype"):
            parts.append(f"Claude reported {result['subtype']}.")
    parts.extend(line for line in stderr_lines if line and not harmless(line))
    detail = (scrub or (lambda text: redact(text, secret, runtime)))("\n".join(dict.fromkeys(parts)).strip())
    clipped = getattr(runtime, "_clipped", None)
    return clipped(detail, limit) if clipped else _clip(detail, limit)


def api_error_message(message):
    """Whether an assistant message is the CLI's report of a failed API request."""

    return bool(message.get("is_api_error_message") or message.get("isApiErrorMessage") or message.get("error"))


class EventTranslator:
    """Map Claude stream-json messages to the Codex-shaped events the UI renders.

    Style "app" mirrors the Codex app-server events of the goal session; style
    "exec" mirrors `codex exec --json`, whose agent messages the server does
    not copy into the live answer (as for Codex structured roles). Every text
    passes through `scrub`, which removes the model's key and other known
    secrets. Thinking blocks become reasoning items whose text is shown only
    with `summaries` (Anthropic's summarized thinking, see
    REASONING_SUMMARY_LEVELS); raw reasoning is never shown. An API error that
    the CLI reports as an assistant message becomes an `error` event, as Codex
    reports provider failures, never an agent message or part of an answer.
    """

    TYPES = {
        "agent": ("agentMessage", "agent_message"),
        "reasoning": ("reasoning", "reasoning"),
        "command": ("commandExecution", "command_execution"),
        "file": ("fileChange", "file_change"),
        "web": ("webSearch", "web_search"),
        "subagent": ("collabAgentToolCall", "collab_agent_tool_call"),
        "tool": ("dynamicToolCall", "dynamicToolCall"),
        "compaction": ("contextCompaction", "context_compaction"),
    }

    def __init__(self, style, scrub=None, summaries=False):
        self.app = style == "app"
        self.scrub = scrub or (lambda text: text)
        self.summaries = summaries
        self.tools = {}
        self.structured = None

    def kind(self, name):
        return self.TYPES[name][0 if self.app else 1]

    def text(self, value, limit=None):
        # Redact before clipping, or a clip could leave part of a key behind.
        value = self.scrub("" if value is None else str(value))
        return value if limit is None else _clip(value, limit)

    def item(self, phase, item):
        if self.app:
            return {"method": f"item/{phase}", "params": {"item": item}}
        return {"type": f"item.{phase}", "item": item}

    def error(self, message):
        if self.app:
            return {"method": "error", "params": {"error": {"message": message}, "willRetry": False}}
        return {"type": "error", "message": message}

    def turn_started(self, turn):
        return {"method": "turn/started", "params": {"turn": {"id": turn}}} if self.app else {"type": "turn.started"}

    def turn_completed(self, turn, status, usage=None, error=None):
        error = self.text(error) if error else error
        if self.app:
            return {"method": "turn/completed", "params": {"turn": {
                "id": turn, "status": status, **({"error": error} if error else {})}}}
        if status == "failed":
            return {"type": "turn.failed", "error": {"message": error or "The Claude request failed."}}
        return {"type": "turn.completed", "usage": usage or {}}

    def _tool(self, block):
        name = str(block.get("name") or "tool")
        data = block.get("input") if isinstance(block.get("input"), dict) else {}
        identity = str(block.get("id") or uuid.uuid4())
        if name == "Bash":
            return {"type": self.kind("command"), "id": identity, "command": self.text(data.get("command"), 2000)}
        if name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
            return {"type": self.kind("file"), "id": identity,
                    "name": self.text(data.get("file_path") or data.get("notebook_path") or name, 500)}
        if name in SEARCH_TOOLS:
            return {"type": self.kind("web"), "id": identity, "tool": name,
                    "query": self.text(data.get("query") or data.get("url"), 500)}
        if name in SUBAGENT_TOOLS:
            return {"type": self.kind("subagent"), "id": identity, "tool": self.text(data.get("description") or name, 200)}
        return {"type": self.kind("tool"), "id": identity, "tool": self.text(name, 200)}

    def message(self, message):
        """Translate one stream-json message into [(event, root)]."""

        kind = message.get("type")
        root = message.get("parent_tool_use_id") is None
        body = message.get("message") if isinstance(message.get("message"), dict) else {}
        content = body.get("content") if isinstance(body.get("content"), list) else []
        base = str(message.get("uuid") or uuid.uuid4())
        events = []
        if kind == "assistant" and api_error_message(message):
            # The turn's result reports the same failure (failure_detail()).
            text = " ".join(str(block.get("text") or "").strip() for block in content
                            if isinstance(block, dict) and block.get("type") == "text").strip()
            return [(self.error(self.text(text or "The Claude request failed.", 2000)), root)]
        if kind == "assistant":
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    events.append(self.item("completed", {"type": self.kind("agent"), "id": f"{base}:{index}",
                                                          "text": self.text(block["text"])}))
                elif block.get("type") in {"thinking", "redacted_thinking"}:
                    summary = block.get("thinking") if self.summaries and block.get("type") == "thinking" else ""
                    events.append(self.item("completed", {"type": "reasoning", "id": f"{base}:{index}",
                                                          "text": self.text(summary).strip()}))
                elif block.get("type") == "tool_use" and block.get("name") == STRUCTURED_OUTPUT_TOOL:
                    if root:
                        self.structured = block.get("input")
                        if not self.app:
                            # Codex shows the final structured answer as an agent message.
                            events.append(self.item("completed", {
                                "type": self.kind("agent"), "id": str(block.get("id") or f"{base}:{index}"),
                                "text": self.text(json.dumps(block.get("input"), ensure_ascii=False))}))
                elif block.get("type") == "tool_use":
                    item = self._tool(block)
                    self.tools[item["id"]] = item
                    events.append(self.item("started", {**item, "status": "inProgress" if self.app else "in_progress"}))
        elif kind == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    item = self.tools.pop(str(block.get("tool_use_id")), None)
                    if item is not None:
                        events.append(self.item("completed", {
                            **item, "status": "failed" if block.get("is_error") else "completed"}))
        elif kind == "system" and message.get("subtype") == "compact_boundary":
            events.append(self.item("completed", {"type": self.kind("compaction"), "id": base}))
        return [(event, root) for event in events]


def tool_result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return "" if content is None else str(content)


def subagent_failure(block):
    """The error that ended the subagent of one Task or Agent tool result, or None.

    None when the subagent finished, or when the call was turned down and no
    subagent ran (see SUBAGENT_REFUSALS).
    """

    text = tool_result_text(block)
    if PARTIAL_SUBAGENT_NOTE in text:
        return text.split(PARTIAL_SUBAGENT_NOTE, 1)[0].strip() or "cut off by an API error"
    if block.get("is_error") and not any(refusal in text.lower() for refusal in SUBAGENT_REFUSALS):
        return text.strip() or "the subagent failed"
    return None


def run_structured(prompt, schema, stage, model, effort, summary, *, timeout, activity_label, features,
                   workspace, environment, runtime=None, conversation=None):
    """Run one structured Claude request and return its structured output as JSON text.

    workflow_runner.structured() decodes, validates and retries the returned
    text exactly as it does for Codex. A missing structured output returns the
    final text so the retry prompt can show it. With a Concise or Detailed
    `summary`, a subscription model shows its summarized thinking.

    With a ``conversation`` (workflow_runner.StructuredConversation) the
    request persists its session (--session-id <uuid4>) instead of using
    --no-session-persistence, and a retry that continues it runs --resume
    <base> --fork-session --session-id <uuid4> with every other flag and
    settings file passed again (verified with Claude Code 2.1.289: the fork
    carries the earlier messages and leaves the base untouched). Claude keeps
    sessions per working directory, so every attempt of one call runs in the
    same workspace. A base Claude no longer has raises ConversationUnavailable
    before any request is made. Each persisted session is registered with the
    conversation, which deletes it when the call ends.
    """

    runtime = _runtime_module(runtime)
    api = providers.provider_kind(model) == providers.API
    summaries = not api and summary in REASONING_SUMMARY_LEVELS
    secret = providers.api_model_secret(model) if api else ""
    if api and not secret:
        raise _failure(runtime, "ConfigurationError")(
            f"Add an API key for {providers.model_label(model)} in Advanced settings → API models.")
    if secret and hasattr(runtime, "register_secret"):
        runtime.register_secret(secret)
    choice = {"live_search": search_is_live(None, runtime), "bash": sandbox_supported()}
    tools, allowed = tool_permissions("structured", features, bare=api, **choice)
    dropped = sorted(set(tool_permissions("structured", features, bare=False, **choice)[0]) - set(tools))
    if dropped:
        runtime.emit("status", stage, label="Claude API mode tools",
                     text=f"Anthropic-format API models run Claude Code in --bare mode, which does not offer {', '.join(dropped)}.")
    settings = PrivateSettings(lambda directory: {
        **({"sandbox": sandbox_settings(hidden_paths(api))} if "Bash" in tools else {}),
        **({"env": gateway_environment(model, secret)} if api else {}),
    })
    result, code, initialized = None, None, False
    fork = getattr(conversation, "fork_from", None) if conversation is not None else None
    resume = bool(fork)
    child = child_environment(environment, effort, api=api)
    if conversation is None:
        session_flags = ["--no-session-persistence"]
    else:
        conversation.session_id, conversation.confirmed = str(uuid.uuid4()), False
        session_flags = (["--resume", fork, "--fork-session", "--session-id", conversation.session_id] if fork
                         else ["--session-id", conversation.session_id])
        if hasattr(conversation, "created"):
            # Registered before the CLI starts: a CLI killed early may still have saved it.
            conversation.created(conversation.session_id, session_deleter(config_directory(child)))
    try:
        arguments = command_arguments(
            executable(runtime), model, effort, tools, allowed, settings.path, bare=api,
            extra=["--json-schema", json.dumps(schema, ensure_ascii=False),
                   *(SUMMARY_DISPLAY if summaries else [])],
        )
        # The session flag precedes every free-text option.
        command = batch_safe([arguments[0], *session_flags, *arguments[1:]], runtime)
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=str(workspace),
            env=child,
        )
        monitor = runtime.RequestMonitor(process, stage, model, effort, timeout=timeout,
                                         activity_label=activity_label, cli="Claude",
                                         redact=lambda text: redact(text, secret, runtime))
        def record_scrub(text):
            return scrub_record(text, secret, runtime)

        translator = EventTranslator("exec", scrub=record_scrub, summaries=summaries)
        # The model's own subagent calls, and those whose error result counts as failed.
        subagent_calls, errored = set(), {}

        def subagent(identity, status, message=""):
            if conversation is not None and hasattr(conversation, "subagent"):
                conversation.subagent(identity, status, record_scrub(message))

        def track_subagents(message):
            content = (message.get("message") or {}).get("content") if isinstance(message.get("message"), dict) else []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") in SUBAGENT_TOOLS:
                    subagent_calls.add(str(block.get("id")))
                elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in subagent_calls:
                    identity, error = str(block["tool_use_id"]), subagent_failure(block)
                    if error is not None and block.get("is_error"):
                        errored[identity] = error
                    subagent(identity, "completed" if error is None else "failed", error or "")

        def publish(event, root=True):
            event = runtime.public_event(event)
            if event is not None:
                runtime.emit("codex_event", stage, event=event, activityLabel=activity_label or "",
                             root=root, provider="claude")

        try:
            monitor.send_input(prompt)
            monitor.start()
            for line in process.stdout:
                message = json_line(line)
                if message is None:
                    continue
                kind = message.get("type")
                if kind == "system" and message.get("subtype") == "init" and message.get("parent_tool_use_id") is None:
                    surprise = unexpected_tools(message, tools)
                    if surprise:
                        raise _failure(runtime, "SafetyStop")(
                            f"Claude CLI enabled unexpected tools ({', '.join(surprise)}); no request was made.")
                    initialized = True
                    reported = message.get("session_id")
                    if conversation is not None and isinstance(reported, str) and _session_id(reported):
                        # Claude now has this conversation; a retry can continue it.
                        conversation.session_id, conversation.confirmed = reported.lower(), True
                        if hasattr(conversation, "created"):
                            conversation.created(conversation.session_id, session_deleter(config_directory(child)))
                    publish(translator.turn_started(None))
                    monitor.observed(False)
                elif kind == "result":
                    result = message
                    stats = message.get("subagent_stats")
                    count = stats.get("failed") if isinstance(stats, dict) else None
                    if isinstance(count, int) and not isinstance(count, bool):
                        # Error results beyond that count ran no subagent; failures
                        # without one (a stalled background subagent) count unnamed.
                        ranked = sorted(errored, key=lambda identity: "api error" not in errored[identity].lower())
                        for identity in ranked[max(0, count):]:
                            subagent(identity, "completed")
                        for number in range(len(errored), count):
                            subagent(f"subagent-{number + 1}", "failed")
                    usage = cache_usage(message.get("usage"))
                    failed = bool(message.get("is_error"))
                    publish(translator.turn_completed(
                        None, "failed" if failed else "completed", usage=usage,
                        error=failure_detail(message, (), secret, runtime, 1000, scrub=record_scrub)
                        if failed else None))
                    if usage:
                        runtime.emit_cache_usage(stage, usage)
                    monitor.observed(False)
                else:
                    if kind in {"assistant", "user"} and message.get("parent_tool_use_id") is None:
                        track_subagents(message)
                    events = translator.message(message)
                    for event, root in events:
                        publish(event, root)
                    monitor.observed(bool(events))
                    if (conversation is not None and kind == "assistant"
                            and message.get("parent_tool_use_id") is None and not api_error_message(message)):
                        # The model worked in this session: a retry continues from it.
                        conversation.output = True
            code = process.wait()
        finally:
            monitor.close()
    finally:
        settings.close()
    if monitor.timed_out.is_set():
        raise runtime.StructuredAttemptTimeout(f"The {stage} model did not respond within {timeout:g} seconds.",
                                               timeout)
    if result is None or result.get("is_error") or code:
        detail = failure_detail(result if result is None or result.get("is_error") else None,
                                monitor.stderr_lines, secret, runtime)
        if resume and not initialized:
            # The fork never started (Claude no longer has the saved session, or
            # this CLI cannot fork it): nothing ran.
            raise _failure(runtime, "ConversationUnavailable")(
                f"Claude could not continue the saved {stage} conversation: {detail}")
        if code:
            detail = f"{detail}\nClaude exited with code {code}.".strip()
        raise runtime.Error(f"Claude {stage} failed: {detail}" if detail else f"Claude {stage} failed without diagnostics.")
    structured = result.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return result.get("result") if isinstance(result.get("result"), str) else ""


def _user_message(text, identity):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            "uuid": identity}


def _session_id(value):
    """Accept only canonical UUIDs: the value reaches argv as --resume <id>."""

    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except ValueError:
        return False


def _missing_session(text):
    return "no conversation found" in str(text or "").lower()


def config_directory(environment):
    """Where a Claude CLI started with ``environment`` keeps its saved conversations."""

    configured = environment.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured)
    return Path(environment.get("HOME") or Path.home()) / ".claude"


def session_deleter(config_dir):
    """Remove one saved conversation (its transcript and its subagent folder) from ``config_dir``/projects.

    Claude Code keeps one folder per working directory; a session id (a UUID)
    names exactly one transcript in them.
    """

    projects = Path(config_dir) / "projects"

    def delete(session_id):
        if not _session_id(session_id):
            return False
        try:
            folders = set()
            for entry in list(projects.glob(f"*/{session_id}.jsonl")):
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                    folders.add(entry.parent)
            for entry in list(projects.glob(f"*/{session_id}")):
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                    folders.add(entry.parent)
        except OSError:
            return False
        for folder in folders:
            try:
                folder.rmdir()  # Only when this left the folder of the call's workspace empty.
            except OSError:
                pass
        return True

    return delete


TRANSCRIPT_READ_LIMIT = 512 * 1024 * 1024


def last_answered_message(config_dir, session_id):
    """The uuid of the newest assistant message on the current branch of a saved conversation.

    Read-only and best effort (it reads Claude Code's own transcript format):
    None when the transcript is missing, too large or not understood. It
    lets a retry resume a conversation at the point before a turn that
    failed without output (--resume-session-at) when this process never saw
    that conversation's messages.
    """

    if not _session_id(session_id):
        return None
    try:
        paths = list((Path(config_dir) / "projects").glob(f"*/{session_id}.jsonl"))
        if len(paths) != 1 or paths[0].is_symlink() or paths[0].stat().st_size > TRANSCRIPT_READ_LIMIT:
            return None
        records, leaf = {}, None
        with paths[0].open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if (not isinstance(record, dict) or record.get("isSidechain")
                        or not isinstance(record.get("uuid"), str)):
                    continue
                records[record["uuid"]] = record
                if record.get("type") in {"user", "assistant"}:
                    leaf = record["uuid"]
        seen = set()
        while leaf in records and leaf not in seen:
            seen.add(leaf)
            record = records[leaf]
            if (record.get("type") == "assistant" and not record.get("isApiErrorMessage")
                    and not record.get("error") and _session_id(leaf)):
                return leaf
            leaf = record.get("parentUuid")
    except OSError:
        return None
    return None


class SessionProcess:
    """One long-lived stream-json CLI process: a locked writer, a reader queue and a stderr tail."""

    def __init__(self, command, cwd, environment, scrub):
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=cwd, env=environment,
        )
        self.lock = threading.Lock()
        self.messages = queue.Queue()
        self.stderr_tail = deque(maxlen=12)
        self._scrub = scrub
        self._readers = [threading.Thread(target=self._read_output, daemon=True),
                         threading.Thread(target=self._read_errors, daemon=True)]
        for reader in self._readers:
            reader.start()

    def _read_output(self):
        try:
            for line in self.process.stdout:
                message = json_line(line)
                if message is not None:
                    self.messages.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put(None)

    def _read_errors(self):
        try:
            for line in self.process.stderr:
                value = line.strip()
                # Startup diagnostics must not publish credentials.
                if value and not harmless(value):
                    self.stderr_tail.append(self._scrub(value)[:800])
        except (OSError, ValueError):
            pass

    def send(self, message):
        with self.lock:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def exited(self, timeout):
        """Whether the CLI has ended, waiting up to `timeout` seconds for it."""

        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def next(self, timeout):
        """The next message, None at end of output; queue.Empty when quiet."""

        return self.messages.get(timeout=timeout)

    def stderr(self):
        self._readers[1].join(timeout=0.5)
        return "\n".join(self.stderr_tail)

    def close(self, runtime, grace, interrupt=False):
        """Interrupt a running turn, end input, then stop the process after a grace period."""

        if self.process.poll() is None:
            if interrupt:
                try:
                    self.send({"type": "control_request", "request_id": f"close-{uuid.uuid4().hex[:12]}",
                               "request": {"subtype": "interrupt"}})
                except (OSError, ValueError):
                    pass
            with self.lock:
                try:
                    self.process.stdin.close()
                except (OSError, ValueError):
                    pass
            try:
                self.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        runtime.stop_process(self.process)
        for reader in self._readers:
            reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (AttributeError, OSError, ValueError):
                pass


def goal_session(runtime, prompt, *, prompts, settings, options, features=(),
                 node_name="author", stages=None, initial_instruction=""):
    """Run the persistent author through Claude with the Codex goal-session protocol.

    Drop-in replacement for workflow_runner.goal_session: same generator
    protocol, events, labels, pause/time-limit behaviour and WorkflowPaused
    reasons. Provider plumbing that emulates Codex goals:
    - one `claude -p` stream-json process per session (--session-id for a new
      session, --resume to continue one). The session id becomes the public
      thread id ("Goal started"/"Goal resumed") only once Claude's first init
      confirms it, as Codex reports a thread only after creating it; that
      session's first "Exact solve input" and turn/started follow the status,
      in Codex's order, and an attempt Claude never confirmed publishes none;
    - GOAL_HARNESS_PROMPT is appended to the system prompt (from a file in the
      private settings folder, so no argument spans lines) and every turn ends
      with a GOAL_TURN_SCHEMA status. "complete" with a final answer completes
      the goal; otherwise the YAML continuation prompt is sent, as Codex
      continues by itself while a goal is active;
    - live instructions and the post-compaction re-anchor are written as user
      messages while a turn runs; Claude reads them at its next tool boundary
      and --replay-user-messages echoes each one when consumed. A message that
      arrives after its turn ended starts a new turn instead, which the CLI's
      command_lifecycle messages report (the init and the echo serve as
      fallbacks). One lock orders sending input against the end of a turn, so
      the decision that follows a turn waits for any such message;
    - pause and the time limit interrupt the running turn with a control
      request; the session stays resumable. A turn abandoned without a result
      is recorded as interrupted or failed. When Claude no longer has a
      session that must be resumed, the pause carries session_unavailable so
      the controller does not retry it;
    - one session is one attempt: any other failure (a failed turn, an API
      error such as overloaded, 429 or 5xx, a CLI that crashed or exited, a
      failed start or resume) ends it in WorkflowPaused whose ``retry`` the
      controller uses to --resume the same session after MODEL_RETRY_SECONDS
      (see workflow_runner._goal_step); the ``goal_retry`` option carries
      the stage, prompt values and first instruction into that session.
      After a turn the model received but answered with nothing at all, the
      retry resumes the session at the last assistant message before it
      (--resume-session-at, verified with Claude Code 2.1.289), so the failed
      turn is left behind and its instruction is sent again; a new session
      whose first turn failed that way starts over with the same instruction;
    - failures no retry can fix pause without ``retry``: a missing session
      id, a session Claude no longer has (both session_unavailable), and
      safety refusals (unexpected tools, a replaced session; safety_stop).
    """

    directory = os.path.abspath(os.fspath(options.get("goal_cwd") or os.getcwd()))
    if runtime.retrieval_policy(directory, features):
        raise _failure(runtime, "ConfigurationError")(HISTORICAL_MESSAGE)
    stages = stages or {"initial": "solve", "resume": "repair", "failure": "failure"}
    continued = options.get("goal_retry") if isinstance(options.get("goal_retry"), dict) else {}
    rewind = continued.get("rewind") if isinstance(continued.get("rewind"), dict) else {}
    attempt = continued.get("attempt") if isinstance(continued.get("attempt"), int) else None
    stage = continued.get("stage") or stages["initial"]
    values = dict(original_prompt=prompt, directory=directory,
                  solution="", bugs="", round=0, revision_number=0, instruction=initial_instruction)
    values.update(continued.get("values") or {})

    def render(name):
        return runtime.render_template(prompts[name], values)

    def emit(kind, **fields):
        runtime.emit(kind, stage, node=node_name, **fields)

    settings = dict(settings)
    model = settings["model"]
    settings["effort"] = runtime.effective_effort(model, settings["effort"])
    settings["speed"] = runtime.effective_speed(model, settings["speed"])
    summary = settings["summary"] = runtime.reasoning_summary(model, settings["summary"])
    grace = getattr(runtime, "INTERRUPT_GRACE_SECONDS", 5)
    api = providers.provider_kind(model) == providers.API
    summaries = not api and summary in REASONING_SUMMARY_LEVELS
    secret = ""
    session = thread = transport = private = None
    # Orders the watcher's live input against the main loop's end of a turn.
    lock = threading.RLock()
    stop = threading.Event()
    expired = threading.Event()
    pausing = threading.Event()
    pause_file = options.get("goal_pause_file")
    # "output": the model produced something in the current turn; "steered": a
    # live instruction or re-anchor joined it; "anchor": the newest assistant
    # message with output; "fresh": this attempt started the conversation.
    state = {"turn": None, "active": True, "running": False, "announced": False, "interrupted": None,
             "steer": options.get("author_steer_delivered"), "initialized": False, "resuming": False,
             "request": None, "instruction": "", "resent": False, "delivered": False,
             "output": False, "steered": False, "fresh": False, "completed": 0, "requested": False,
             "resume_at": None,
             "anchor": rewind.get("anchor") if _session_id(rewind.get("anchor")) else (
                 continued.get("anchor") if _session_id(continued.get("anchor")) else None)}
    pending = {}  # message uuid -> (kind, steer id) until Claude echoes it as consumed
    seen_compactions = set()
    tools = []
    pause_reason = ""
    unavailable = False
    retry = None
    missing_id = False
    safety_stop = False
    environment = None

    def scrub(text):
        return redact(text, secret, runtime)

    def scrub_event(text):
        return scrub_record(text, secret, runtime)

    translator = EventTranslator("app", scrub=scrub_event, summaries=summaries)

    def record(event, root=True):
        event = runtime.public_event(event)
        if event is not None:
            emit("codex_event", event=event, root=root, provider="claude")

    def begin_turn(turn, announce=True):
        state.update(turn=turn, running=True, announced=announce)
        translator.structured = None
        if announce:
            record(translator.turn_started(turn))

    def announce():
        """Publish the request and start of the turn a confirmed session is running."""

        request, state["request"] = state["request"], None
        if request is not None and state["running"] and not state["announced"]:
            emit("request", threadId=session, **request)
            state["announced"] = True
            record(translator.turn_started(state["turn"]))

    def abandon_turn(status):
        """Record the end of a published turn that will report no result."""

        with lock:
            if state["running"] and state["turn"] and state["announced"]:
                record(translator.turn_completed(state["turn"], status))
            state.update(running=False, turn=None, announced=False, request=None)

    def send_user(text, kind, identity=None):
        message_id = str(uuid.uuid4())
        pending[message_id] = (kind, identity)
        try:
            transport.send(_user_message(text, message_id))
        except BaseException:
            pending.pop(message_id, None)
            raise
        return message_id

    def interrupt():
        state["interrupted"] = state["turn"]
        transport.send({"type": "control_request", "request_id": f"interrupt-{uuid.uuid4().hex[:12]}",
                        "request": {"subtype": "interrupt"}})

    def steer(instruction, identity):
        send_user(instruction, "steer", identity)
        # The controller recognizes a restored instruction by its command id.
        emit("request", label="Live author instruction sent", text=instruction, threadId=session, steerId=identity)

    def consumed(identity):
        """Claude took one of our messages into the conversation; return whether it began a turn.

        Reported by command_lifecycle "started" or, on CLIs without lifecycle
        messages, by the --replay-user-messages echo; the later signal finds
        nothing left to do. Input that arrives after its turn ended (a live
        instruction or re-anchor) starts the next turn by itself.
        """

        item = pending.pop(identity, None)
        if item is None:
            return False
        if item[0] == "turn":
            state["resent"] = False
            state["delivered"] = True
            # The new branch has begun: a later relaunch resumes its newest message.
            state["resume_at"] = None
        began = not state["running"]
        if began:
            begin_turn(f"turn-{uuid.uuid4().hex[:12]}")
        if item[0] in {"steer", "compaction"}:
            state["steered"] = True
        if item[0] == "steer":
            state["steer"] = item[1]
            emit("status", label="Author instruction accepted", authorSteerDelivered=item[1],
                 text="The current conversation has received the live instruction.", threadId=thread)
        return began

    def watch():
        pause_started = None
        interrupted_turn = None
        while not stop.wait(0.1):
            if pause_file and Path(pause_file).is_file() and state["active"]:
                if pause_started is None:
                    pause_started = time.monotonic()
                    pausing.set()
                with lock:
                    if transport and state["running"] and state["turn"] and state["turn"] != interrupted_turn:
                        interrupted_turn = state["turn"]
                        try:
                            interrupt()
                        except (OSError, ValueError) as exc:
                            emit("diagnostic", text=f"Could not interrupt the author: {exc}")
                if time.monotonic() - pause_started >= grace:
                    emit("diagnostic", text="Claude did not acknowledge the pause; keeping its saved session and the last saved work.")
                    runtime.stop_process(transport.process)
                    return
                continue
            if runtime.workflow_remaining(options) <= 0:
                expired.set()
                if not state["active"]:
                    return
                with lock:
                    try:
                        if transport and state["running"] and state["turn"]:
                            interrupt()
                    except (OSError, ValueError):
                        pass
                if not stop.wait(grace):
                    runtime.stop_process(transport.process)
                return
            if state["active"] and state["running"] and state["turn"]:
                command = runtime.pending_author_steer(options.get("author_steer_file"), state["steer"])
                if not command:
                    continue
                with lock:
                    # Rechecked under the lock that ends turns: an instruction
                    # goes out only while a turn still runs, so the decision
                    # after that turn waits for it.
                    if (state["active"] and state["running"] and state["turn"] and command[0] != state["steer"]
                            and ("steer", command[0]) not in list(pending.values())):
                        try:
                            steer(command[1], command[0])
                        except (OSError, ValueError) as exc:
                            emit("diagnostic", text=f"Could not send live instruction: {exc}")

    def launch(identity, resume, resume_at=None):
        nonlocal transport, session
        # A relaunch before the turn reached the new branch resumes at the same message again.
        resume_at = resume_at or (state["resume_at"] if resume else None)
        state["resume_at"] = resume_at
        if transport is not None:
            transport.close(runtime, grace)
        if state["running"] and state["turn"] and state["announced"]:
            record(translator.turn_completed(state["turn"], "interrupted"))  # The old process never ran it.
        pending.clear()
        state.update(initialized=False, resuming=resume, running=False, announced=False, turn=None,
                     interrupted=None, request=None)
        session = identity
        # The session flag precedes every free-text option. --resume-session-at
        # continues after that message, leaving a failed empty turn behind.
        flags = (["--resume", identity, *(["--resume-session-at", resume_at] if resume_at else [])] if resume
                 else ["--session-id", identity])
        transport = SessionProcess(batch_safe([base_command[0], *flags, *base_command[1:]], runtime),
                                   directory, environment, scrub)

    def start_turn(instruction):
        # A retry re-sends this instruction unless Claude consumed it.
        state.update(instruction=instruction, delivered=False, output=False, steered=False)
        if pause_file and Path(pause_file).is_file():
            pausing.set()
            raise runtime.Error("Pause requested.")
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        if transport.process.poll() is not None and state["initialized"]:
            # The idle CLI ended (for example during a long review); its session is saved.
            emit("diagnostic", text="The Claude CLI exited between turns; resuming its saved session.", threadId=thread)
            launch(session, True)
        state.update(active=True, instruction=instruction, request={
            "label": "Exact solve input", "text": instruction, "model": model,
            "reasoningEffort": settings["effort"], "reasoningSummary": summary,
            **({"attempt": attempt} if attempt and not state["requested"] else {})})
        state["requested"] = True
        begin_turn(f"turn-{uuid.uuid4().hex[:12]}", announce=False)
        try:
            send_user(instruction, "turn")
        except (OSError, ValueError):
            if not transport.exited(grace):
                raise
            if state["initialized"]:
                # The idle CLI ended just now: resume its saved session and send again.
                start_turn(instruction)
                state["resent"] = True
                return
            # A CLI that stops at once (for example on a missing saved
            # session) never reads its input; the main loop then reads its
            # result or stderr and reports why.
        if state["initialized"]:
            announce()

    def start_new_session():
        nonlocal thread
        thread = None  # Announced as "Goal started" once Claude confirms it.
        state.update(fresh=True, completed=0, anchor=None)
        launch(str(uuid.uuid4()), False)

    def resume_unavailable(detail, instruction):
        nonlocal unavailable
        if options.get("goal_require_resume"):
            unavailable = True
            raise runtime.Error("Could not resume the saved Claude conversation; no replacement was started. "
                                f"{detail}{UNAVAILABLE_SESSION_HINT}")
        emit("diagnostic", text=f"Saved session unavailable; starting a new session with the supplied resume instructions: {detail}")
        start_new_session()
        # The instruction of the turn that could not start: the first prompt,
        # or a repair or continuation when a relaunch between turns failed.
        start_turn(instruction)

    try:
        if pause_file and Path(pause_file).is_file():
            pausing.set()
            raise runtime.Error("Pause requested before starting the model.")
        if runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
        if options.get("goal_require_resume") and not options.get("goal_thread_id"):
            missing_id = True
            raise runtime.Error("The saved Claude session ID is missing; cannot resume the same conversation. "
                                "Automatic retry is off. Choose Continue proof author to continue the saved work "
                                "in a new conversation.")
        if api:
            secret = providers.api_model_secret(model)
            if not secret:
                raise _failure(runtime, "ConfigurationError")(
                    f"Add an API key for {providers.model_label(model)} in Advanced settings → API models.")
            if hasattr(runtime, "register_secret"):
                runtime.register_secret(secret)
        bash = sandbox_supported()
        if not bash:
            emit("diagnostic", text="The Claude Bash sandbox is unavailable on this platform, so the author works with file tools only.")
        choice = {"live_search": search_is_live(directory, runtime), "bash": bash}
        tools, allowed = tool_permissions("author", features, bare=api, **choice)
        dropped = sorted(set(tool_permissions("author", features, bare=False, **choice)[0]) - set(tools))
        if dropped:
            emit("status", label="Claude API mode tools",
                 text=f"Anthropic-format API models run Claude Code in --bare mode, which does not offer {', '.join(dropped)}.")
        private = PrivateSettings(lambda path: {
            **({"sandbox": sandbox_settings(hidden_paths(api))} if "Bash" in tools else {}),
            **({"env": gateway_environment(model, secret)} if api else {}),
        })
        environment = child_environment(runtime.environment(model), settings["effort"], api=api)
        harness = private.write(HARNESS_FILE, GOAL_HARNESS_PROMPT.replace("[OBJECTIVE]", render("goal")))
        base_command = command_arguments(
            executable(runtime), model, settings["effort"], tools, allowed, private.path, bare=api,
            extra=["--input-format", "stream-json", "--replay-user-messages",
                   *(SUMMARY_DISPLAY if summaries else []),
                   "--json-schema", json.dumps(GOAL_TURN_SCHEMA),
                   "--append-system-prompt-file", str(harness)],
        )
        saved = options.get("goal_thread_id")
        if continued.get("instruction"):
            # A retry: the interrupted turn's instruction, or the retry note.
            instruction = continued["instruction"]
        else:
            instruction = render("resume") if options.get("goal_resume") or saved else prompt
            if initial_instruction:
                instruction += "\n\n" + initial_instruction
        watcher = threading.Thread(target=watch, daemon=True)
        if saved and _session_id(saved):
            anchor = rewind.get("anchor") if _session_id(rewind.get("anchor")) else None
            launch(saved.lower(), True, resume_at=anchor)
            if anchor:
                emit("status", label="Failed turn removed", threadId=saved.lower(),
                     text="The interrupted turn ended before the model produced anything; the conversation "
                          "continues from before it and its instruction is sent again.")
            watcher.start()
            with lock:
                start_turn(instruction)
        else:
            watcher.start()
            with lock:
                if saved:
                    resume_unavailable("the saved session ID is not a Claude session ID.", instruction)
                else:
                    start_new_session()
                    start_turn(instruction)
        finished = None  # Structured status of a finished turn awaiting its decision.
        while True:
            if expired.is_set() or runtime.workflow_remaining(options) <= 0:
                raise runtime.Error("Workflow time limit reached.")
            if not state["running"] and pause_file and Path(pause_file).is_file():
                pausing.set()
                raise runtime.Error("Author paused at the current turn boundary.")
            if finished is not None and not state["running"]:
                answer = None
                with lock:
                    queued = [key for key, item in list(pending.items()) if item[0] != "turn"]
                    waiting = bool(queued) and time.monotonic() - finished["at"] < QUEUED_INPUT_SECONDS
                    if not waiting:
                        if queued:
                            # An undelivered live instruction is offered again in a later turn.
                            for key in queued:
                                pending.pop(key, None)
                            emit("diagnostic", text="Queued author input was not picked up; continuing without it.")
                        structured, finished = finished["structured"], None
                        goal_status = structured.get("goal_status") if isinstance(structured, dict) else None
                        final = structured.get("final_answer") if isinstance(structured, dict) else None
                        note = structured.get("progress_note") if isinstance(structured, dict) else None
                        record({"method": "thread/goal/updated", "params": {"goal": {
                            "status": {"in_progress": "active"}.get(goal_status, goal_status or "active")}}})
                        if goal_status == "complete" and isinstance(final, str) and final.strip():
                            # No live instruction is sent while the answer is reviewed.
                            state["active"] = False
                            answer = final
                        else:
                            emit("status", label="Author progress", threadId=thread, goalStatus=goal_status or "missing",
                                 text=(note.strip() if isinstance(note, str) and note.strip() else
                                       "The turn ended without a structured goal status; continuing." if goal_status is None
                                       else f"Goal status: {goal_status}."))
                            start_turn(render("continuation"))
                if not waiting:
                    if answer is not None:
                        record(translator.item("completed", {"type": translator.kind("agent"),
                                                             "id": f"{thread}:final:{uuid.uuid4().hex[:8]}",
                                                             "text": translator.text(answer)}))
                        emit("status", label="Goal complete", threadId=thread, text=f"Thread {thread}")
                        emit("author_result", label="Author solution", text=answer, threadId=thread)
                        rejection = yield {"outcome": "done", "output": answer}
                        if rejection is None:
                            return
                        values.update(rejection)
                        values["revision_number"] += 1
                        stage = stages["resume"]
                        with lock:
                            start_turn(render("repair"))
                    continue
            try:
                message = transport.next(0.5)
            except queue.Empty:
                continue
            if message is None or (message.get("type") == "result" and state["resuming"] and not state["initialized"]):
                detail = failure_detail(message, transport.stderr().splitlines(), secret, runtime, 1600)
                if state["resuming"] and not state["initialized"] and _missing_session(detail):
                    with lock:
                        resume_unavailable(detail, state["instruction"])
                    continue
                if (message is None and state["initialized"] and state["running"] and not state["resent"]
                        and any(item[0] == "turn" for item in pending.values()) and transport.exited(grace)):
                    # The idle CLI ended just as the turn's message was sent and
                    # never read it: resume the saved session and send it again.
                    with lock:
                        start_turn(state["instruction"])
                        state["resent"] = True
                    continue
                if message is None:
                    raise runtime.Error("Claude CLI stopped unexpectedly." + (f"\n{detail}" if detail else ""))
            kind = message.get("type")
            if kind == "command_lifecycle":
                command, phase = str(message.get("command_uuid") or ""), message.get("state")
                with lock:
                    if phase == "started" and consumed(command):
                        finished = None
                    elif phase == "cancelled":
                        # Dropped by the CLI: a live instruction is offered again later.
                        pending.pop(command, None)
                continue
            if kind == "system":
                subtype = message.get("subtype")
                if subtype == "init" and message.get("parent_tool_use_id") is None:
                    surprise = unexpected_tools(message, tools)
                    if surprise:
                        raise _failure(runtime, "SafetyStop")(
                            f"Claude CLI enabled unexpected tools ({', '.join(surprise)}); the author was stopped.")
                    with lock:
                        if not state["initialized"]:
                            if message.get("session_id") and str(message["session_id"]).lower() != session:
                                raise _failure(runtime, "SafetyStop")(
                                    "Claude returned a different session ID; refusing to continue a replacement "
                                    "conversation.")
                            state["initialized"] = True
                            if thread != session:
                                thread = session
                                emit("status", label="Goal resumed" if state["resuming"] else "Goal started",
                                     threadId=thread, text=f"Thread {thread}")
                            announce()
                        if not state["running"]:
                            # Queued input started a turn by itself (CLIs without lifecycle messages).
                            finished = None
                            begin_turn(f"turn-{uuid.uuid4().hex[:12]}")
                elif subtype == "compact_boundary":
                    for event, root in translator.message(message):
                        record(event, root)
                    key = message.get("uuid") or uuid.uuid4().hex
                    if message.get("parent_tool_use_id") is None and not pausing.is_set() and key not in seen_compactions:
                        seen_compactions.add(key)
                        anchor = render("compaction")
                        with lock:
                            send_user(anchor, "compaction")
                        emit("request", label="Author context re-anchor after compaction", text=anchor, threadId=session)
                continue
            if kind == "user" and message.get("isReplay"):
                with lock:
                    if consumed(str(message.get("uuid"))):
                        finished = None
                continue
            if kind in {"assistant", "user"}:
                for event, root in translator.message(message):
                    record(event, root)
                if (kind == "assistant" and message.get("parent_tool_use_id") is None
                        and not api_error_message(message)):
                    state["output"] = True  # The model worked in this turn.
                    if _session_id(message.get("uuid")):
                        state["anchor"] = str(message["uuid"]).lower()
                continue
            if kind == "control_response":
                response = message.get("response") if isinstance(message.get("response"), dict) else {}
                if response.get("subtype") == "error":
                    emit("diagnostic", text=scrub(f"Claude rejected a control request: {response.get('error')}"))
                continue
            if kind != "result":
                continue
            with lock:
                turn = state["turn"] or f"turn-{uuid.uuid4().hex[:12]}"
                interrupted = state["interrupted"] is not None and state["interrupted"] == state["turn"]
                announced = state["announced"]
                state.update(running=False, turn=None, announced=False, request=None)
                for key in [key for key, item in list(pending.items()) if item[0] == "turn"]:
                    pending.pop(key, None)  # The turn its message started has ended.
            failed = bool(message.get("is_error"))
            detail = failure_detail(message, (), secret, runtime, 1600) if failed else ""
            if announced:
                record(translator.turn_completed(
                    turn, "interrupted" if interrupted else "failed" if failed else "completed",
                    error=failure_detail(message, (), secret, runtime, 1600, scrub=scrub_event) if failed else None))
            usage = cache_usage(message.get("usage"))
            if usage and hasattr(runtime, "emit_cache_usage"):
                runtime.emit_cache_usage(stage, usage, label="Author cache usage")
            if pausing.is_set() or (pause_file and Path(pause_file).is_file()):
                pausing.set()
                raise runtime.Error("Author paused at the current turn boundary.")
            if expired.is_set():
                raise runtime.Error("Workflow time limit reached.")
            if failed:
                # Usage limits and API errors end this attempt; the controller retries.
                raise runtime.Error(f"Claude author turn failed: {detail or 'no complete result'}")
            state["completed"] += 1
            structured = message.get("structured_output")
            finished = {"structured": structured if isinstance(structured, dict) else translator.structured,
                        "at": time.monotonic()}
    except Exception as exc:
        stopping = pausing.is_set() or expired.is_set() or bool(pause_file and Path(pause_file).is_file())
        abandon_turn("interrupted" if stopping else "failed")
        if pausing.is_set() or (pause_file and Path(pause_file).is_file()):
            pausing.set()
            emit("status", label="Author paused", text=str(exc), threadId=thread)
        else:
            pausing.set()
            limited = expired.is_set() or runtime.workflow_remaining(options) <= 0
            safety = isinstance(exc, (_failure(runtime, "SafetyStop"), retrieval_runtime.RetrievalPolicyError))
            safety = safety and _failure(runtime, "SafetyStop") is not runtime.Error
            pause_reason = "Workflow time limit reached." if expired.is_set() else (str(exc) or type(exc).__name__)
            if safety and not limited:
                safety_stop = True
                pause_reason = (f"Stopped for safety: {pause_reason}"
                                + getattr(runtime, "SAFETY_STOP_HINT", ""))
            emit("diagnostic", text=pause_reason, threadId=thread, **({"attempt": attempt} if attempt else {}))
            retryable = getattr(runtime, "retryable_failure", lambda error: isinstance(error, runtime.Error))
            if limited or unavailable or missing_id or safety or not retryable(exc):
                emit("status", label="Author stopped for safety" if safety and not limited else "Author paused",
                     text=pause_reason, threadId=thread)
            else:
                # A model-call failure: the controller resumes this session after a wait.
                empty = state["delivered"] and not state["output"] and not state["steered"] and thread
                anchor = state["anchor"]
                if empty and not anchor and not (state["fresh"] and not state["completed"]):
                    # A conversation this process never read: find its last answer on disk.
                    anchor = last_answered_message(config_directory(environment), thread) if environment else None
                rewind = None
                if empty:
                    rewind = ({"anchor": anchor} if anchor
                              else {"fresh": True} if state["fresh"] and not state["completed"] else None)
                # A CLI that ended before it started the conversation it was to
                # resume at a message (for example one without --resume-session-at)
                # is next reopened plainly, so the retries always progress.
                rewind_failed = bool(state["resume_at"] and not state["initialized"] and transport is not None
                                     and transport.process.poll() is not None)
                retry = {"thread": thread, "stage": stage, "values": dict(values),
                         "instruction": state["instruction"] or None, "delivered": state["delivered"],
                         "output": state["output"], "anchor": state["anchor"], "rewind": rewind,
                         "rewind_failed": rewind_failed, "steer": state["steer"]}
    finally:
        stop.set()
        if transport is not None:
            # Input already written may have started a turn the CLI is running.
            transport.close(runtime, grace, interrupt=state["running"] or bool(pending))
        if private is not None:
            private.close()
    if pausing.is_set():
        paused = runtime.WorkflowPaused(pause_reason or "Workflow paused.", reason=pause_reason, thread_id=thread)
        paused.session_unavailable = unavailable or missing_id
        paused.safety_stop = bool(safety_stop)
        paused.retry = retry
        raise paused
