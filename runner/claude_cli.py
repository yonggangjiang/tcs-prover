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
  user, project and local settings files; deny rules keep them off the run
  folder's protected entries (protected_settings());
- Bash runs only inside the Claude sandbox (failIfUnavailable, no unsandboxed
  escape; network to every host in a live run only, none in a web-cutoff
  run); CLAUDE_CODE_SUBPROCESS_ENV_SCRUB removes credentials
  from its environment, and the sandbox can neither read nor write TCS
  Prover's private state (the API model store and every settings folder)
  nor write the protected entries. On macOS it reads only its working
  directory, a private temp folder and the system, and writes only those two
  (see sandbox_settings());
- API models run in a private configuration folder of their run folder
  (CLAUDE_CONFIG_DIR, see claude_home()), so the subscription login is never
  read: its credentials file, its keychain entry (whose name Claude derives
  from the configuration folder) and ~/.claude.json belong to the user's own
  folder. A `claude` launcher may drop CLAUDE_CONFIG_DIR, so a --bare start,
  which reads no login and no keychain, first checks that the CLI received it
  (keeps_config_directory()); otherwise API models run with --bare, which
  offers only Bash, Read and Edit. Their gateway settings and key go in an
  owner-only --settings file in the private state folder, because the
  launcher may strip ANTHROPIC_* variables from the environment. That file
  exists only while its CLI process runs; Stop, or a later runner or server
  start, removes one left behind by a process that was killed before its
  cleanup.
"""

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from retrieval import runtime as retrieval_runtime
from runner import confinement, providers

EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
SEARCH_TOOLS = ("WebSearch", "WebFetch")
# The Bash sandbox's allowlist of a live run: every host. Claude Code 2.1.289 keeps an
# allowedDomains entry of a --settings file as it is and its sandbox runtime matches a bare "*"
# against every host (read from its bundle; it has no other setting that allows all hosts). Bash
# then reaches the internet through the sandbox's HTTP and SOCKS proxy, which its commands find in
# HTTP_PROXY, HTTPS_PROXY and ALL_PROXY: a program that ignores them gets no direct connection.
SANDBOX_ALL_HOSTS = ("*",)
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
# An absolute path that a file rule can spell as it is: none of the characters that Claude Code's
# gitignore-style patterns ("*", "?", "[", "\", ...) or its rule syntax ("(", ")") read specially.
PLAIN_RULE_PATH = re.compile(r"/[A-Za-z0-9 ._~@/-]*\Z")
# The user's own subscription login may be located through these; API models
# start from an environment without any Claude or Anthropic variable.
SUBSCRIPTION_ENVIRONMENT = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
HISTORICAL_MESSAGE = "Web cutoff timestamps require Codex CLI models for every proof role."
# Informational lines the CLI prints on every run.
HARMLESS_STDERR = ("[claude-code:unrecognized_model]", "Permission mode forced to default")
# Queued steering or re-anchor input normally starts the next turn at once.
QUEUED_INPUT_SECONDS = 30
# A CLI offered tools beyond --tools: every later start of this process denies
# them (--disallowedTools), and a CLI that still offers one is a safety stop.
DISALLOWED_TOOLS = set()
TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
# How long the check of an API model's configuration folder waits for the CLI.
CONFIG_CHECK_SECONDS = 30
_CONFIG_CHECKS = {}  # (launcher, its file, folder) -> True, False (dropped) or None (no answer)
# Calls on several threads share both: the denied tools, and one configuration check per
# launcher and folder.
_STATE_LOCK = threading.Lock()
_CONFIG_CHECK_LOCK = threading.Lock()
# Identical refusals to reopen a saved author conversation before it is replaced.
RESUME_REFUSALS = 3
_REFUSALS = {}  # session id -> [refusal, count] within this process
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
# Replaces the goal harness note for a conversation turn of the paused author
# (author_conversation()); it travels in a file named HARNESS_FILE too, so
# leftover_authors() also recognizes a conversation CLI.
CONVERSATION_HARNESS_NOTE = """TCS PROVER HARNESS NOTE (provider plumbing; the message itself arrives in the user turn)
The long-running goal is paused while the user talks with you. Reply in plain text; this turn needs no structured goal status."""


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
    critic, writer, repair) only read, except that a call with file_edit
    edits the one file of its own temporary workspace (see
    workflow_runner.EditableFile). Task (subagents) follows the node's
    multi_agent feature and Bash, always sandboxed, is offered to structured
    roles only with shell_tool. Search tools follow the run's search mode.
    """

    if role == "author":
        tools = [*(["Task"] if "multi_agent" in features else []), *(["Bash"] if bash else []),
                 "Read", "Write", "Edit", "TodoWrite"]
    else:
        tools = ["Read", *(["Task"] if "multi_agent" in features else []),
                 *(["Bash"] if bash and "shell_tool" in features else [])]
        if "file_edit" in features:
            tools += ["Write", "Edit"]
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


def sandbox_settings(hidden, read_only=(), *, workspace=None, temp=None, live=False):
    """Bash may write only to its working directory and temp space; it reaches the internet in a live run only.

    ``read_only`` paths stay readable but not writable (the run folder's
    protected entries, protected_settings()). With a private ``temp``
    folder (bash_temp(), macOS) Bash reads only its ``workspace``, that
    folder, the system and the toolchains: the home folder, other volumes,
    the shared temp folders, the checkout and the folder holding the
    workspace (the other runs) are denied, and only the workspace (its
    Edit(./**) rule) and the temp folder are writable; Claude's own
    /tmp/claude is not. Without one, Bash reads everything except ``hidden``.

    Network follows the run's search mode (``live``, search_is_live()), as
    the search tools do: a live run allows every host (SANDBOX_ALL_HOSTS),
    any other run none. The strict allowlist never asks for a host.
    """

    hidden = [str(path) for path in hidden]
    filesystem = {"denyRead": hidden, "denyWrite": [*hidden, *(str(path) for path in read_only)]}
    if temp is not None:
        temp, workspace, home = os.path.realpath(temp), os.path.realpath(workspace), os.path.realpath(Path.home())
        # The checkout and the folder holding the workspace (the other runs), also outside the home folder.
        outside = [path for path in dict.fromkeys((os.path.realpath(providers.ROOT), os.path.dirname(workspace)))
                   if os.path.commonpath((path, home)) != home and os.path.dirname(path) != path]
        filesystem = {
            "denyRead": [home, *confinement.CLAUDE_DENIED_READS, *outside, *hidden],
            "allowRead": [workspace, temp,
                          *(str(path) for path in (*confinement.claude_versions(),
                                                   *confinement.toolchain_folders(home_only=True)))],
            "allowWrite": [temp],
            "denyWrite": [*hidden, *(str(path) for path in read_only), *confinement.CLAUDE_DENIED_WRITES],
        }
    return {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "network": {"allowedDomains": list(SANDBOX_ALL_HOSTS) if live else [], "strictAllowlist": True},
        "filesystem": filesystem,
    }


def bash_temp(tools, workspace, runtime):
    """The private temp folder (confinement.claude_temp()) of a CLI that offers Bash on macOS, else None.

    The CLI's PrivateSettings remove it with the settings file.
    """

    if "Bash" not in tools or not confinement.supported():
        return None
    try:
        return confinement.claude_temp(workspace)
    except (confinement.ConfinementError, OSError) as exc:
        raise runtime.Error(f"Cannot prepare the private temp folder of Claude's Bash: {exc}") from exc


def protected_settings(directory):
    """The --settings parts and Bash paths that keep the protected entries of the run folder ``directory`` read-only.

    As for Codex, every model process may only read the controller's files
    (job-settings.json, pause.json, checked-statement.md, prompts/), the
    user's related works (a folder run hard-links one copy into every job,
    so an edit would reach every other job, see ui/related_works.py) and
    Codex's project folders .codex, .agents, .git and .aws
    (confinement.PROTECTED_ENTRIES). Deny rules keep the file tools off them
    (an Edit rule also covers Write; a folder's rule covers everything in it,
    also when it does not exist yet); the returned paths go to the Bash
    sandbox's denyWrite (sandbox_settings()). The rules name the entries by
    the run folder's real path (Edit(//<path>/<name>), an absolute pattern),
    so that, as for Bash, entries of these names deeper in the run folder
    stay writable: a rule pattern without a leading slash, such as
    Edit(./prompts), is matched like a gitignore line and denies a prompts
    folder at any depth (Claude Code 2.1.289). A real path with a character
    such patterns or the rule syntax read specially (PLAIN_RULE_PATH) keeps
    those relative rules, which deny too much rather than too little.
    """

    root = os.path.realpath(directory)
    prefix = f"/{root}/" if PLAIN_RULE_PATH.match(root) else "./"
    rules = []
    for name in confinement.PROTECTED_ENTRIES:
        rules.append(f"Edit({prefix}{name})")
        if name in confinement.PROTECTED_FOLDERS:
            rules.append(f"Edit({prefix}{name}/**)")
    return {"permissions": {"deny": rules}}, [Path(directory) / name for name in confinement.PROTECTED_ENTRIES]


def gateway_environment(model, key):
    """Settings-file environment that sends every request to the configured endpoint.

    ANTHROPIC_BASE_URL takes no /v1. The key travels in x-api-key via
    ANTHROPIC_API_KEY; the optional request source travels in User-Agent. All
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
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_CUSTOM_HEADERS": f"User-Agent: {providers.request_source(entry)}",
    }
    # The limits saved with this endpoint (Claude assumes a window for a model it does not know).
    if entry.get("contextWindow"):
        environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(int(entry["contextWindow"]))
    if entry.get("maxOutputTokens"):
        environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(entry["maxOutputTokens"]))
    return environment


def child_environment(base, effort, *, api, config=None, temp=None):
    """Start from the runner environment without any inherited Claude/Anthropic setting.

    ``config`` is the private configuration folder of an API model
    (claude_home()); ``temp`` the private temp folder of Bash (bash_temp()),
    which Claude Code makes Bash's TMPDIR, and with it Bash reads no global
    git config. Like every confined process, the CLI is not told where the
    user's SSH and GPG agents listen (confinement.AGENT_VARIABLES).
    """

    environment = {
        name: value for name, value in confinement.without_agents(base).items()
        if not name.startswith(("ANTHROPIC_", "CLAUDE")) or (not api and name in SUBSCRIPTION_ENVIRONMENT)
    }
    if config is not None:
        environment["CLAUDE_CONFIG_DIR"] = str(config)
    # The environment value takes precedence over --effort, so both agree.
    environment["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    # 429 and 529 responses are waited out inside the same request (Anthropic's own
    # runners set this too) rather than failing after a few minutes of retries.
    environment["CLAUDE_CODE_RETRY_WATCHDOG"] = "1"
    if temp is not None:
        environment["CLAUDE_CODE_TMPDIR"] = str(temp)
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
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
    close() also removes the CLI's private Bash ``temp`` folder (bash_temp()).
    """

    def __init__(self, build, temp=None):
        self.temp = temp
        try:
            self.directory = Path(tempfile.mkdtemp(prefix=f"{SETTINGS_PREFIX}{os.getpid()}-", dir=settings_parent()))
        except BaseException:
            if temp is not None:
                shutil.rmtree(temp, ignore_errors=True)
            raise
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
        if self.temp is not None:
            shutil.rmtree(self.temp, ignore_errors=True)


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
    """Remove private settings folders (and Bash temp folders) whose owner process has ended.

    Stop kills a runner that ignores its interrupt (on Windows every runner),
    and a killed process cannot delete the key-bearing file it was using.
    """

    confinement.sweep_temps(_process_alive)
    try:
        entries = list((providers.state_directory() / providers.CLAUDE_SETTINGS).iterdir())
    except OSError:
        return
    for entry in entries:
        owner = entry.name[len(SETTINGS_PREFIX):].split("-", 1)[0] if entry.name.startswith(SETTINGS_PREFIX) else ""
        if owner.isdigit() and int(owner) != os.getpid() and not _process_alive(int(owner)):
            shutil.rmtree(entry, ignore_errors=True)


def _run_folder(kind, workspace, runtime):
    """An owner-only folder of one run folder in the private state folder (``kind`` names the collection)."""

    key = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:24]
    try:
        state = providers.private_directory(providers.state_directory())
        return providers.private_directory(providers.private_directory(state / kind) / key)
    except (OSError, providers.ProviderError) as exc:
        raise runtime.Error(f"Cannot prepare TCS Prover's private Claude folder: {exc}") from exc


def claude_home(workspace, runtime):
    """The Claude configuration folder (CLAUDE_CONFIG_DIR) of the API models of one run folder.

    It holds their saved conversations and Claude Code's own state, never a
    login. Like the Codex homes it lives in the private state folder, which no
    sandbox may read or write, and persists so that the run's author can resume.
    """

    return _run_folder(providers.CLAUDE_HOMES, workspace, runtime)


def _same_folder(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def keeps_config_directory(executable_path, home, environment, model, effort, secret, workspace, runtime):
    """Whether the CLI that ``executable_path`` starts uses ``home`` (its CLAUDE_CONFIG_DIR) as configuration folder.

    A `claude` launcher script may drop the variable, and the CLI would then
    read the user's own login. One start per launcher and folder checks it:
    with --bare, which reads no login and no keychain, the CLI answers an
    initialize control request, before and without any model request, with
    the output-styles folder of the configuration folder it uses (read from
    the Claude Code 2.1.289 binary). Returns True, False when the CLI uses
    another folder, or None when it gave no answer.
    """

    try:
        launcher = os.stat(executable_path)
        key = (executable_path, launcher.st_mtime_ns, launcher.st_size, str(home))
    except OSError:
        key = (executable_path, None, None, str(home))
    with _CONFIG_CHECK_LOCK:
        if key not in _CONFIG_CHECKS:
            _CONFIG_CHECKS[key] = _check_config_directory(executable_path, home, environment, model, effort, secret,
                                                          workspace, runtime)
        return _CONFIG_CHECKS[key]


def _check_config_directory(executable_path, home, environment, model, effort, secret, workspace, runtime):
    """One start of the check that keeps_config_directory() describes."""

    request, answer, probe = f"config-{uuid.uuid4().hex[:12]}", None, None
    settings = PrivateSettings(lambda directory: {"env": gateway_environment(model, secret)})
    try:
        probe = SessionProcess(batch_safe(command_arguments(
            executable_path, model, effort, ["Read"], ["Read"], settings.path, bare=True,
            extra=["--input-format", "stream-json", "--no-session-persistence"]), runtime),
            str(workspace), environment, lambda text: redact(text, secret, runtime))
        probe.send({"type": "control_request", "request_id": request, "request": {"subtype": "initialize"}})
        deadline = time.monotonic() + CONFIG_CHECK_SECONDS
        while answer is None:
            message = probe.next(max(0.0, deadline - time.monotonic()))
            if message is None:
                break
            response = message.get("response") if message.get("type") == "control_response" else None
            if isinstance(response, dict) and response.get("request_id") == request:
                details = response.get("response") if isinstance(response.get("response"), dict) else {}
                folder = details.get("user_output_styles_dir")
                answer = isinstance(folder, str) and _same_folder(Path(folder).parent, home)
    except (OSError, ValueError, queue.Empty):
        pass  # No answer in time: treated as not confirmed.
    finally:
        if probe is not None:
            probe.close(runtime, 1)
        settings.close()
    return answer


def api_configuration(model, effort, secret, folder, environment, runtime):
    """The private configuration folder an API model's CLI runs in, or (None, why --bare is needed instead)."""

    home = claude_home(folder, runtime)
    kept = keeps_config_directory(executable(runtime), home, child_environment(environment, effort, api=True, config=home),
                                  model, effort, secret, folder, runtime)
    if kept:
        return home, ""
    return None, ("The claude command on PATH does not pass CLAUDE_CONFIG_DIR on" if kept is False
                  else "The claude command on PATH did not confirm that it uses a private CLAUDE_CONFIG_DIR")


def command_arguments(executable_path, model, effort, tools, allowed, settings_path, *, bare, extra=(), denied=()):
    """Common flags of every Claude role; ``denied`` are tools an earlier CLI offered unasked."""

    return [
        executable_path, "-p", "--output-format", "stream-json", "--verbose",
        "--model", providers.wire_model(model), "--effort", effort,
        # Never load the user's MCP servers, customizations or browser integration.
        "--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG,
        "--safe-mode", "--disable-slash-commands", "--no-chrome", "--restricted",
        # Anything not pre-approved is denied; nothing ever waits for a prompt.
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--tools", ",".join(tools), "--allowedTools", ",".join(allowed),
        *(["--disallowedTools", ",".join(denied)] if denied else []),
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
    if expected & set(SUBAGENT_TOOLS):
        expected |= set(SUBAGENT_TOOLS)  # Claude Code names the subagent tool Task or Agent.
    return sorted(str(name) for name in offered if name not in expected)


def disallowed_tools():
    """The tools every later start of this process denies (see surprise_failure()), sorted."""

    with _STATE_LOCK:
        return sorted(DISALLOWED_TOOLS)


def surprise_failure(surprise, denied, runtime, stopped):
    """The failure for tools a CLI offered beyond --tools; ``stopped`` says what was stopped.

    The first time it is an ordinary failed attempt, and every later start of
    this process denies the tools with --disallowedTools. A CLI that still
    offers a denied tool, or a name that cannot be denied, is a safety stop.
    """

    names = ", ".join(surprise)
    if any(name in denied or not TOOL_NAME.match(name) for name in surprise):
        return _failure(runtime, "SafetyStop")(
            f"Claude CLI enabled unexpected tools ({names}) that --disallowedTools did not remove; {stopped}.")
    with _STATE_LOCK:
        DISALLOWED_TOOLS.update(surprise)
    return runtime.Error(f"Claude CLI enabled unexpected tools ({names}); {stopped}, and the next attempt "
                         "denies them with --disallowedTools.")


def api_retry_text(message):
    """A status line for the CLI's own retry of a failed API request, or None.

    Claude Code reports its retries as system/api_retry (and a subagent's as
    tool_progress with subagent_retry) while it waits, so a request that waits
    out an overload is not mistaken for a hang.
    """

    if message.get("type") == "system" and message.get("subtype") == "api_retry":
        details, who = message, "Claude API retry"
    elif message.get("type") == "tool_progress" and isinstance(message.get("subagent_retry"), dict):
        details, who = message["subagent_retry"], "Claude API retry (subagent)"
    else:
        return None
    status, error = details.get("error_status"), details.get("error") or details.get("error_category")
    if _number(status):
        cause = f"status {status:g}"
    elif isinstance(error, str) and error.strip():
        cause = _clip(error.strip().replace("_", " "), 80)
    else:
        cause = "connection error"  # Claude reports no status when no response arrived.
    text = f"{who}: {cause}"
    delay = details.get("retry_delay_ms")
    if _number(delay):
        seconds = max(0.0, delay / 1000)
        text += f", retrying in {round(seconds / 60, 1):g} min" if seconds >= 60 else f", retrying in {round(seconds, 1):g} s"
    attempt, limit = details.get("attempt"), details.get("max_retries")
    if _number(attempt):
        text += f" (attempt {attempt:g} of {limit:g})" if _number(limit) else f" (attempt {attempt:g})"
    return text


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    parts.extend(line for line in list(stderr_lines) if line and not harmless(line))
    detail = (scrub or (lambda text: redact(text, secret, runtime)))("\n".join(dict.fromkeys(parts)).strip())
    # Clipped in the middle when the runner can: the newest lines often name the cause.
    clipped = getattr(runtime, "_clip_middle", None) or getattr(runtime, "_clipped", None)
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
                   workspace, environment, runtime=None, conversation=None, deadline=None):
    """Run one structured Claude request and return its structured output as JSON text.

    workflow_runner.structured() decodes, validates and retries the returned
    text exactly as it does for Codex. A missing structured output returns the
    final text so the retry prompt can show it. With a Concise or Detailed
    `summary`, a subscription model shows its summarized thinking. The shared
    RequestMonitor stops the CLI after `timeout` seconds without any output
    (subagent messages count) or after `deadline` seconds.

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
    home, reason = api_configuration(model, effort, secret, Path.cwd(), environment, runtime) if api else (None, "")
    bare = api and home is None
    tools, allowed = tool_permissions("structured", features, bare=bare, **choice)
    dropped = sorted(set(tool_permissions("structured", features, bare=False, **choice)[0]) - set(tools))
    if dropped:
        runtime.emit("status", stage, label="Claude API mode tools",
                     text=f"{reason}, so Anthropic-format API models run Claude Code in --bare mode, which does "
                          f"not offer {', '.join(dropped)}.")
    denied = disallowed_tools()
    temp = bash_temp(tools, workspace, runtime)
    settings = PrivateSettings(lambda directory: {
        **({"sandbox": sandbox_settings(hidden_paths(api), workspace=workspace, temp=temp,
                                        live=choice["live_search"])}
           if "Bash" in tools else {}),
        **({"env": gateway_environment(model, secret)} if api else {}),
    }, temp=temp)
    result, code, initialized = None, None, False
    fork = getattr(conversation, "fork_from", None) if conversation is not None else None
    resume = bool(fork)
    child = child_environment(environment, effort, api=api, config=home, temp=temp)
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
            executable(runtime), model, effort, tools, allowed, settings.path, bare=bare, denied=denied,
            extra=["--json-schema", json.dumps(schema, ensure_ascii=False),
                   *(SUMMARY_DISPLAY if summaries else [])],
        )
        # The session flag precedes every free-text option.
        command = batch_safe([arguments[0], *session_flags, *arguments[1:]], runtime)
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", cwd=str(workspace),
                env=child,
            )
        except OSError as exc:
            exc.cli_problem = "Claude"  # The CLI could not be started at all.
            raise
        monitor = runtime.RequestMonitor(process, stage, model, effort, timeout=timeout,
                                         activity_label=activity_label, cli="Claude",
                                         redact=lambda text: redact(text, secret, runtime),
                                         **({"deadline": deadline} if deadline is not None else {}))
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
                    subagent(str(block.get("id")), "started")
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
            monitor.start()
            monitor.send_input(prompt)
            for line in monitor.lines():
                message = json_line(line)
                if message is None:
                    continue
                kind = message.get("type")
                if kind == "system" and message.get("subtype") == "init" and message.get("parent_tool_use_id") is None:
                    surprise = unexpected_tools(message, tools)
                    if surprise:
                        raise surprise_failure(surprise, denied, runtime, "the request was stopped")
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
                    retrying = api_retry_text(message)
                    if retrying:
                        runtime.emit("status", stage, label=f"{activity_label} · Claude API retry" if activity_label
                                     else "Claude API retry", text=retrying, activityLabel=activity_label or "")
                    # The heartbeat names a retry the CLI waits for, until anything else arrives.
                    monitor.retrying(retrying)
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
    if conversation is not None:
        # Everything this attempt reported, for the run's failure log.
        failed = result if isinstance(result, dict) and result.get("is_error") else None
        conversation.detail = {"provider": failure_detail(failed, (), secret, runtime, limit=10 ** 9),
                               "stderr": monitor.stderr_text()}
    stopped = monitor.stopped(stage)
    if stopped is not None:
        raise stopped
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
        failure = runtime.Error(f"Claude {stage} failed: {detail}" if detail
                                else f"Claude {stage} failed without diagnostics.")
        if not initialized:
            failure.cli_problem = "Claude"  # It ended before its init message: it could not start.
        raise failure
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


def transcript_path(config_dir, session_id):
    """The saved transcript of one conversation in ``config_dir``, or None."""

    if not _session_id(session_id):
        return None
    try:
        paths = [path for path in (Path(config_dir) / "projects").glob(f"*/{session_id}.jsonl")
                 if path.is_file() and not path.is_symlink()]
    except OSError:
        return None
    return paths[0] if len(paths) == 1 else None


def _copy_file(source, target, keep_time):
    """Replace ``target`` with an owner-only copy of ``source``."""

    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".copy-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream, source.open("rb") as original:
            shutil.copyfileobj(original, stream)
        if keep_time:
            stat = source.stat()
            os.utime(temporary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save_transcript(config_dir, session_id, copies):
    """Keep a private copy of one author conversation in ``copies`` when it changed (best effort).

    Claude Code deletes saved conversations after its cleanupPeriodDays
    setting (30 days by default); restore_transcript() puts the copy back.
    """

    source = transcript_path(config_dir, session_id)
    if source is None or copies is None:
        return
    target = copies / source.parent.name / source.name
    try:
        stat = source.stat()
        if target.is_file() and (target.stat().st_size, target.stat().st_mtime_ns) == (stat.st_size, stat.st_mtime_ns):
            return
        providers.private_directory(target.parent)
        _copy_file(source, target, keep_time=True)
    except (OSError, providers.ProviderError):
        pass


def restore_transcript(config_dir, session_id, copies):
    """Put back the private copy of a conversation that Claude no longer has; return whether it did."""

    if copies is None or not _session_id(session_id) or transcript_path(config_dir, session_id) is not None:
        return False
    try:
        saved = [path for path in copies.glob(f"*/{session_id}.jsonl") if path.is_file() and not path.is_symlink()]
        if len(saved) != 1:
            return False
        target = Path(config_dir) / "projects" / saved[0].parent.name / saved[0].name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            return False
        # A fresh modification time, so Claude's cleanup does not delete it again at once.
        _copy_file(saved[0], target, keep_time=False)
    except OSError:
        return False
    return True


def leftover_authors(session_id, exclude=()):
    """PIDs of this user's Claude author CLIs that TCS Prover started for ``session_id`` and that still run.

    A runner that was killed can leave its CLI behind, which keeps the
    conversation open: Claude then resumes a copy of it instead.
    """

    if os.name == "nt" or not _session_id(session_id):
        return []
    try:
        listing = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,uid=,command="], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in listing.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        pid, command = int(fields[0]), fields[2]
        if (int(fields[1]) == os.getuid() and pid != os.getpid() and pid not in exclude
                and any(f"{flag} {session_id}" in command for flag in ("--resume", "--session-id"))
                and "--replay-user-messages" in command and HARNESS_FILE in command):
            found.append(pid)
    return found


def stop_processes(pids, grace):
    """Ask processes to end, then kill the ones still running after ``grace`` seconds."""

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_process_alive(pid) for pid in pids):
        time.sleep(0.1)
    for pid in pids:
        if _process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


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
            _close_stream(self.process.stdout)

    def _read_errors(self):
        try:
            for line in self.process.stderr:
                value = line.strip()
                # Startup diagnostics must not publish credentials.
                if value and not harmless(value):
                    self.stderr_tail.append(self._scrub(value)[:800])
        except (OSError, ValueError):
            pass
        finally:
            _close_stream(self.process.stderr)

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
        # The readers close their streams when the streams end. One that a process
        # the CLI started still holds open is left to its reader: closing it here
        # would wait for that process.
        for reader in self._readers:
            reader.join(timeout=1)
        _close_stream(self.process.stdin)


def _close_stream(stream):
    try:
        stream.close()
    except (AttributeError, OSError, ValueError):
        pass


def goal_session(runtime, prompt, *, prompts, settings, options, features=(),
                 node_name="author", stages=None, initial_instruction="", handoff_note=""):
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
      the decision that follows a turn waits for any such message, at most
      QUEUED_INPUT_SECONDS: then a live instruction is offered again later and
      a re-anchor leads the next continuation turn. Input given up on that
      Claude still picks up runs as a turn of its own, whose result is never
      taken as the answer to the instruction sent after it;
    - pause and the time limit interrupt the running turn with a control
      request; the session stays resumable. A turn abandoned without a result
      is recorded as interrupted or failed;
    - each finished turn's transcript is copied into the private state folder,
      and a saved conversation that Claude no longer has is restored from that
      copy. Otherwise, and for a missing or invalid saved id or after
      RESUME_REFUSALS identical refusals to reopen it, the author continues in
      a new conversation of the same run with a "Conversation replaced"
      notice, the resume prompt and, after a rejection, the repair prompt. A
      conversation that a leftover TCS Prover CLI still holds is reopened
      once that CLI was stopped;
    - one session is one attempt: any other failure (a failed turn, an API
      error such as overloaded, 429 or 5xx, a CLI that crashed or exited, a
      failed start or resume) ends it in WorkflowPaused whose ``retry`` the
      controller uses to --resume the same session after MODEL_RETRY_SECONDS
      (see workflow_runner._goal_step); the ``goal_retry`` option carries
      the stage, prompt values, first instruction and an undelivered
      re-anchor into that session. After a turn the model received but
      answered with nothing at all, the retry resumes the session at the last
      assistant message before it (--resume-session-at, verified with Claude
      Code 2.1.289), so the failed turn is left behind and its instruction is
      sent again; a new session whose first turn failed that way starts over
      with the same instruction. Tools a CLI offers beyond the list end the
      attempt once and are denied from then on (surprise_failure());
    - safety refusals pause without ``retry`` (safety_stop): tools that
      --disallowedTools did not remove, and a new session Claude reports
      under another id;
    - as with Codex, a resumed session gets the short "reopen" prompt (the
      full resume prompt with ``goal_reanchor``) and critic feedback it
      already received (``author_repair_delivered``) is not sent again; the
      controller's ``handoff_note`` follows any critic feedback in the first
      instruction, also in a new conversation, until the author submits
      again; a conversation that outgrew the model's context
      continues in a new session (``retry`` "replace"); and a running turn
      with no event for AUTHOR_INACTIVITY_SECONDS gets a control request,
      whose missing answer ends the attempt as a failed model call.
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

    def pending_repair():
        """The critic feedback this conversation must receive once: a repair of this session, else the recovery."""

        return render("repair") if stage == stages["resume"] else str(values.get("instruction") or "")

    def received(repair, identity):
        """Whether the resumed session already received this critic feedback (the controller recorded it)."""

        delivered = options.get("author_repair_delivered")
        digest = getattr(runtime, "_repair_digest", None)
        return (digest is not None and isinstance(delivered, dict) and delivered.get("thread") == identity
                and delivered.get("digest") == digest(repair))

    settings = dict(settings)
    model = settings["model"]
    settings["effort"] = runtime.effective_effort(model, settings["effort"])
    settings["speed"] = runtime.effective_speed(model, settings["speed"])
    summary = settings["summary"] = runtime.reasoning_summary(model, settings["summary"])
    grace = getattr(runtime, "INTERRUPT_GRACE_SECONDS", 30)
    inactivity = getattr(runtime, "AUTHOR_INACTIVITY_SECONDS", 30 * 60)
    probe_limit = getattr(runtime, "AUTHOR_PROBE_SECONDS", 60)
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
    # message with output; "fresh": this attempt started the conversation;
    # "unexpected": turns Claude runs for given-up input before the pending
    # instruction; "carried": a given-up re-anchor that leads the next turn.
    state = {"turn": None, "active": True, "running": False, "announced": False, "interrupted": None,
             "steer": options.get("author_steer_delivered"), "initialized": False, "resuming": False,
             "request": None, "instruction": "", "resent": False, "delivered": False,
             "output": False, "steered": False, "fresh": False, "completed": 0, "requested": False,
             "resume_at": None, "unexpected": 0, "carried": continued.get("pending_compaction") or None,
             "restored": False, "stopped": False,
             "anchor": rewind.get("anchor") if _session_id(rewind.get("anchor")) else (
                 continued.get("anchor") if _session_id(continued.get("anchor")) else None)}
    # message uuid -> (kind, steer id or re-anchor text) until Claude echoes it as consumed,
    # and the same for input given up on after QUEUED_INPUT_SECONDS.
    pending, given_up = {}, {}
    held, told = {}, set()  # Live instructions that could not be sent, and those already reported.
    # The newest CLI message, and the control request that checks a quiet CLI still answers.
    activity = {"last": time.monotonic(), "probe": None, "at": None}
    seen_compactions = set()
    tools = []
    denied = []
    pause_reason = ""
    retry = None
    safety_stop = False
    environment = copies = None

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

    def hold(identity, why):
        """Offer an instruction that could not be sent again at the next turn or later; say so once."""

        retry = getattr(runtime, "STEER_RETRY_SECONDS", 60)
        held[identity] = (state["turn"], time.monotonic() + retry)
        if identity not in told:
            told.add(identity)
            emit("diagnostic", text=f"{why}; it is offered again when the next turn starts or in "
                                    f"{runtime._span_text(retry)}.", threadId=thread)

    def held_back(identity):
        turn, until = held.get(identity, (None, None))
        if until is None:
            return False
        if state["turn"] != turn or time.monotonic() >= until:
            held.pop(identity, None)
            return False
        return True

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
        late = item is None
        if late:
            item = given_up.pop(identity, None)
            if item is None:
                return False
        # Input given up on that runs before the instruction sent since is a
        # turn of its own: its result is not that instruction's answer.
        unexpected = late and state["running"] and not state["delivered"]
        if unexpected:
            state["unexpected"] += 1
            emit("diagnostic", threadId=thread, text="Claude picked up queued author input after it was given up "
                                                     "on; the turn it starts is not taken as an answer.")
        if item[0] == "turn":
            state["resent"] = False
            state["delivered"] = True
            # The new branch has begun: a later relaunch resumes its newest message.
            state["resume_at"] = None
            repair = pending_repair()
            if repair and repair in state["instruction"] and hasattr(runtime, "_repair_digest"):
                # The controller sends this feedback to the same session only once.
                emit("status", label="Critic feedback delivered", threadId=session,
                     repairDelivered=runtime._repair_digest(repair),
                     text="The author's conversation has received the critic's feedback.")
        began = not state["running"]
        if began:
            begin_turn(f"turn-{uuid.uuid4().hex[:12]}")
        if item[0] in {"steer", "compaction"} and not unexpected:
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
                            emit("status", label="Author stopping", threadId=thread,
                                 text=f"Pause requested: the author may finish its current step for up to "
                                      f"{runtime._span_text(grace)} before it is stopped.")
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
                            emit("status", label="Author stopping", threadId=thread,
                                 text=f"The workflow time limit was reached: the author may finish its current "
                                      f"step for up to {runtime._span_text(grace)} before it is stopped.")
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
                            and ("steer", command[0]) not in list(pending.values()) and not held_back(command[0])):
                        try:
                            steer(command[1], command[0])
                        except (OSError, ValueError) as exc:
                            hold(command[0], f"Could not send the live instruction ({exc})")

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
        given_up.clear()
        state.update(initialized=False, resuming=resume, running=False, announced=False, turn=None,
                     interrupted=None, request=None, unexpected=0)
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
        state.update(fresh=True, completed=0, anchor=None, carried=None)  # A new conversation needs no re-anchor.
        launch(str(uuid.uuid4()), False)

    def carry(instruction):
        """Lead ``instruction`` with a re-anchor that never reached the model."""

        carried, state["carried"] = state["carried"], None
        return f"{carried}\n\n{instruction}" if carried else instruction

    def replace_conversation(reason, previous):
        """Continue in a new conversation of this run folder: Claude cannot reopen the saved one.

        It starts with the resume prompt and, after a rejection, the repair
        prompt with the critic's report, then any ``handoff_note``; the stage,
        prompt values and the workflow's time budget stay those of this run.
        """

        _REFUSALS.pop(previous, None)
        if stage == stages["resume"]:
            instruction = f"{render('resume')}\n\n{render('repair')}"
        elif initial_instruction and not continued.get("instruction"):
            instruction = f"{render('resume')}\n\n{initial_instruction}"
        else:
            instruction = render("resume")
        if handoff_note:
            instruction += "\n\n" + handoff_note
        emit("status", label="Conversation replaced", **({"replacedThreadId": previous} if previous else {}),
             text=f"Claude Code cannot reopen the saved author conversation ({reason}), so the author continues "
                  "in a new conversation in this run folder. It starts from the resume prompt"
                  + (" and the critic's last report" if stage == stages["resume"] else "")
                  + (", then the note on why its last submission was refused" if handoff_note else "")
                  + "; the time budget continues.")
        start_new_session()
        start_turn(instruction)

    def reopen(detail):
        """Claude ended before it reopened the saved conversation; return whether a new attempt is under way.

        A conversation it no longer has is put back from its private copy once,
        else replaced. Other refusals of this conversation (not transient
        failures such as a login or network problem) replace it after
        RESUME_REFUSALS identical ones in a row; until then they are retried.
        """

        if _missing_session(detail):
            if not state["restored"]:
                state["restored"] = True
                if restore_transcript(config_directory(environment), session, copies):
                    emit("status", label="Saved conversation restored", threadId=session,
                         text="Claude Code no longer had the saved author conversation; its private copy was put "
                              "back and the conversation continues.")
                    launch(session, True)
                    start_turn(state["instruction"])
                    return True
            replace_conversation("it no longer has it", session)
            return True
        lowered = detail.lower()
        if (state["resume_at"] or not lowered.strip() or getattr(runtime, "_plain_failure", lambda text: "")(detail)
                or not any(word in lowered for word in (session, "session", "conversation", "resume", "transcript"))):
            return False
        return stop_leftovers() or refused(detail)

    def refused(detail):
        """Count one refusal to reopen the saved conversation; replace it after RESUME_REFUSALS identical ones."""

        # Session ids and numbers (positions, PIDs) differ between identical refusals.
        key = re.sub(r"\d+", "#", re.sub(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", "<id>", detail.lower()))[:300]
        last = _REFUSALS.get(session)
        count = last[1] + 1 if last and last[0] == key else 1
        _REFUSALS[session] = [key, count]
        if count < RESUME_REFUSALS:
            return False
        first = next((line.strip() for line in detail.splitlines() if line.strip()), detail)
        replace_conversation(f"{count} identical refusals: {_clip(first, 300)}", session)
        return True

    def stop_leftovers():
        """Stop the CLIs an earlier TCS Prover runner left on the saved conversation and reopen it.

        Such a CLI keeps the conversation open, so Claude refuses it or opens
        a copy instead. Looked for once per attempt; returns whether it reopened.
        """

        leftovers = [] if state["stopped"] else leftover_authors(session, {transport.process.pid})
        state["stopped"] = True
        if not leftovers:
            return False
        emit("diagnostic", threadId=session,
             text=f"A Claude CLI left by an earlier TCS Prover runner (PID {', '.join(map(str, leftovers))}) still "
                  "held the saved author conversation; it was stopped and the conversation is reopened.")
        stop_processes(leftovers, grace)
        launch(session, True)
        start_turn(state["instruction"])
        return True

    def held_elsewhere(copy):
        """Claude opened a copy instead: another process holds the saved conversation.

        Returns whether a new attempt is under way (stop_leftovers(), or the
        replacement after RESUME_REFUSALS such refusals, see refused()).
        """

        return stop_leftovers() or refused(f"it is open in another Claude Code process, which opened the copy "
                                           f"{copy} instead")

    try:
        if pause_file and Path(pause_file).is_file():
            pausing.set()
            raise runtime.Error("Pause requested before starting the model.")
        if runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached.")
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
        inherited = runtime.environment(model)
        home, reason = (api_configuration(model, settings["effort"], secret, directory, inherited, runtime)
                        if api else (None, ""))
        bare = api and home is None
        tools, allowed = tool_permissions("author", features, bare=bare, **choice)
        dropped = sorted(set(tool_permissions("author", features, bare=False, **choice)[0]) - set(tools))
        if dropped:
            emit("status", label="Claude API mode tools",
                 text=f"{reason}, so Anthropic-format API models run Claude Code in --bare mode, which does not "
                      f"offer {', '.join(dropped)}.")
        denied = disallowed_tools()
        protected, protected_paths = protected_settings(directory)
        temp = bash_temp(tools, directory, runtime)
        private = PrivateSettings(lambda path: {
            **protected,
            **({"sandbox": sandbox_settings(hidden_paths(api), protected_paths, workspace=directory, temp=temp,
                                            live=choice["live_search"])}
               if "Bash" in tools else {}),
            **({"env": gateway_environment(model, secret)} if api else {}),
        }, temp=temp)
        environment = child_environment(inherited, settings["effort"], api=api, config=home, temp=temp)
        try:
            copies = _run_folder(providers.CLAUDE_TRANSCRIPTS, directory, runtime)
        except runtime.Error as exc:
            emit("diagnostic", text=f"No private copy of the author conversation can be kept: {exc}")
        harness = private.write(HARNESS_FILE, GOAL_HARNESS_PROMPT.replace("[OBJECTIVE]", render("goal")))
        base_command = command_arguments(
            executable(runtime), model, settings["effort"], tools, allowed, private.path, bare=bare, denied=denied,
            extra=["--input-format", "stream-json", "--replay-user-messages",
                   *(SUMMARY_DISPLAY if summaries else []),
                   "--json-schema", json.dumps(GOAL_TURN_SCHEMA),
                   "--append-system-prompt-file", str(harness)],
        )
        saved = options.get("goal_thread_id")
        # The first instruction of a new conversation, should the saved one turn out to be gone.
        fresh = render("resume") + ("\n\n" + initial_instruction if initial_instruction else "")
        if continued.get("instruction"):
            # A retry: the interrupted turn's instruction, or the retry note.
            instruction = continued["instruction"]
        elif saved and _session_id(saved) and "reopen" in prompts and not options.get("goal_reanchor"):
            # A session that is resumed holds the task already: the short reopen prompt, and
            # critic feedback only if it never received it. After a conversation turn that
            # compacted its context (goal_reanchor), the full resume prompt restores the task.
            instruction = render("reopen")
            if initial_instruction and not received(initial_instruction, saved.lower()):
                instruction += "\n\n" + initial_instruction
        else:
            instruction = render("resume") if options.get("goal_resume") or saved else prompt
            if initial_instruction:
                instruction += "\n\n" + initial_instruction
        if handoff_note and not continued.get("instruction"):
            # Why the last submission was refused follows any critic feedback; a retry's instruction covers it.
            instruction += "\n\n" + handoff_note
        first = instruction
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
                # A re-anchor the previous attempt could not deliver leads its first turn.
                start_turn(carry(instruction))
        else:
            watcher.start()
            with lock:
                if saved:
                    replace_conversation("the saved session ID is not a Claude session ID", saved)
                elif options.get("goal_require_resume"):
                    replace_conversation("the saved session ID is missing", None)
                else:
                    start_new_session()
                    start_turn(instruction)
        finished = None  # Structured status of a finished turn awaiting its decision.
        while True:
            if (expired.is_set() or runtime.workflow_remaining(options) <= 0) and not state["running"]:
                # A running turn is interrupted by the watcher and may end within the grace.
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
                            # Given up on: a live instruction is offered again in a later turn, and a
                            # re-anchor leads the next continuation. Claude may still pick either up.
                            for key in queued:
                                item = given_up[key] = pending.pop(key)
                                if item[0] == "compaction":
                                    state["carried"] = item[1]
                            emit("diagnostic", text="Queued author input was not picked up; a live instruction is "
                                                    "offered again later and a re-anchor leads the next turn.")
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
                            start_turn(carry(render("continuation")))
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
                        handoff_note = ""  # This submission answered it.
                        values.update(rejection)
                        values["revision_number"] += 1
                        stage = stages["resume"]
                        with lock:
                            state["carried"] = None  # The repair prompt re-anchors by itself.
                            start_turn(render("repair"))
                    continue
            try:
                message = transport.next(0.5)
            except queue.Empty:
                now = time.monotonic()
                if activity["at"] is not None and now - activity["at"] >= probe_limit:
                    raise runtime.Error(
                        f"The Claude CLI stopped responding: no event for {runtime._span_text(now - activity['last'])} "
                        f"and no answer to a check within {runtime._span_text(probe_limit)}.")
                if activity["at"] is None and state["running"] and now - activity["last"] >= inactivity:
                    # Any message, the answer included, shows that the CLI still works.
                    activity.update(at=now, probe=f"check-{uuid.uuid4().hex[:12]}")
                    try:
                        transport.send({"type": "control_request", "request_id": activity["probe"],
                                        "request": {"subtype": "mcp_status"}})
                    except (OSError, ValueError):
                        pass  # The CLI ended; its output reports why.
                    emit("diagnostic", text=f"No author event for {runtime._span_text(inactivity)}; checking that "
                                            "the Claude CLI still answers.", threadId=thread)
                continue
            activity.update(last=time.monotonic(), at=None)
            if message is None or (message.get("type") == "result" and state["resuming"] and not state["initialized"]):
                detail = failure_detail(message, transport.stderr().splitlines(), secret, runtime, 1600)
                if state["resuming"] and not state["initialized"]:
                    with lock:
                        # A conversation Claude cannot reopen is restored or replaced; a
                        # replacement starts with the full resume prompt (replace_conversation).
                        if reopen(detail):
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
            retrying = api_retry_text(message)
            if retrying:
                emit("status", label="Claude API retry", text=retrying, threadId=thread)
                continue
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
                        raise surprise_failure(surprise, denied, runtime, "the author was stopped")
                    with lock:
                        if not state["initialized"]:
                            reported = str(message.get("session_id") or "").lower()
                            if reported and reported != session:
                                if not state["resuming"]:
                                    raise _failure(runtime, "SafetyStop")(
                                        "Claude returned a different session ID; refusing to continue a replacement "
                                        "conversation.")
                                # Claude resumed a copy: another process holds the saved conversation.
                                if held_elsewhere(reported):
                                    continue
                                raise runtime.Error("Claude could not reopen the saved author conversation: it is open "
                                                    "in another Claude Code process, so Claude opened a copy instead.")
                            state["initialized"] = True
                            _REFUSALS.pop(session, None)
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
                            send_user(anchor, "compaction", anchor)
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
                if response.get("subtype") == "error" and response.get("request_id") != activity["probe"]:
                    emit("diagnostic", text=scrub(f"Claude rejected a control request: {response.get('error')}"))
                continue
            if kind != "result":
                continue
            with lock:
                if state["unexpected"] and not state["delivered"] and not pausing.is_set():
                    # The turn of input given up on ended; the instruction sent after it runs next.
                    state["unexpected"] -= 1
                    continue
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
                raise runtime.Error("Author paused; its current turn was interrupted." if interrupted
                                    else "Author paused at the current turn boundary.")
            if expired.is_set():
                raise runtime.Error("Workflow time limit reached.")
            if failed:
                # Usage limits and API errors end this attempt; the controller retries.
                raise runtime.Error(f"Claude author turn failed: {detail or 'no complete result'}")
            state["completed"] += 1
            structured = message.get("structured_output")
            finished = {"structured": structured if isinstance(structured, dict) else translator.structured,
                        "at": time.monotonic()}
            save_transcript(config_directory(environment), thread, copies)
    except Exception as exc:
        stopping = pausing.is_set() or expired.is_set() or bool(pause_file and Path(pause_file).is_file())
        abandon_turn("interrupted" if stopping else "failed")
        if pausing.is_set() or (pause_file and Path(pause_file).is_file()):
            pausing.set()
            emit("status", label="Author paused", text=str(exc), threadId=thread)
        else:
            pausing.set()
            # A model this account cannot use, or an endpoint that refuses the request: no retry helps.
            exc = getattr(runtime, "settings_failure", lambda error, model=None: None)(exc, model) or exc
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
            if limited or safety or not retryable(exc):
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
                undelivered = state["carried"] or next(
                    (item[1] for item in pending.values() if item[0] == "compaction"), None)
                retry = {"thread": thread, "stage": stage, "values": dict(values),
                         "instruction": state["instruction"] or None, "delivered": state["delivered"],
                         "output": state["output"], "anchor": state["anchor"], "rewind": rewind,
                         "rewind_failed": rewind_failed, "steer": state["steer"], "pending_compaction": undelivered}
                if thread and getattr(runtime, "_context_window_failure", lambda error: False)(exc):
                    # The same conversation would fail again: continue in a new one.
                    repair = pending_repair()
                    retry.update(replace=f"The conversation no longer fits the model's context ({_clip(scrub(str(exc)), 300)})",
                                 instruction=render("resume") + ("\n\n" + repair if repair else "")
                                 + ("\n\n" + handoff_note if handoff_note else ""),
                                 delivered=False, output=False, anchor=None, rewind=None, rewind_failed=False,
                                 pending_compaction=None)
    finally:
        stop.set()
        if transport is not None:
            # Input already written may have started a turn the CLI is running.
            transport.close(runtime, grace, interrupt=state["running"] or bool(pending))
        if thread and environment is not None:
            save_transcript(config_directory(environment), thread, copies)
        if private is not None:
            private.close()
    if pausing.is_set():
        paused = runtime.WorkflowPaused(pause_reason or "Workflow paused.", reason=pause_reason, thread_id=thread)
        paused.safety_stop = bool(safety_stop)
        paused.retry = retry
        raise paused


def author_conversation(runtime, instruction, *, settings, options, features=(), conversation_id, label,
                        attempt=1, rewind=None, classic=False):
    """One conversation turn of the paused Claude author in its saved session; return its reply.

    The counterpart of workflow_runner.author_conversation: `claude -p
    --resume <saved session>` with CONVERSATION_HARNESS_NOTE in place of the
    goal harness and no --json-schema, one user message, read until its
    result. It never starts, forks or replaces a conversation (no
    --session-id, no --fork-session) and never sends a re-anchor. A live
    run's turn offers only Read and the run's search tools
    (CONVERSATION_READ_ONLY). ``rewind`` is the message to resume at
    (--resume-session-at), which leaves a failed empty turn behind;
    ``classic`` means nothing to Claude. Web-cutoff runs never reach Claude.

    A saved conversation that Claude no longer has is put back from its
    private copy once; otherwise SavedConversationGone. A CLI left behind on
    the conversation is stopped once and the turn started again. A refusal
    to reopen the conversation raises ConversationRefused, and any other
    failed attempt ConversationAttemptFailed (see run_author_conversation).
    Every record has stage "conversation", node "author" and the
    conversationId. Return {"reply", "threadId", "compacted"}.
    """

    runtime = _runtime_module(runtime)
    directory = os.path.abspath(os.fspath(options.get("goal_cwd") or os.getcwd()))

    def emit(kind, **fields):
        runtime.emit(kind, "conversation", node="author", conversationId=conversation_id, **fields)

    if runtime.retrieval_policy(directory, features):
        raise _failure(runtime, "ConfigurationError")(HISTORICAL_MESSAGE)
    saved = options.get("goal_thread_id")
    saved = str(saved).strip() if saved is not None else ""
    if not saved:
        raise _failure(runtime, "SavedConversationGone")("The author has not started its conversation yet.")
    if not _session_id(saved):
        raise _failure(runtime, "SavedConversationGone")("The saved author session ID is not a Claude session ID.")
    session = saved.lower()
    settings = dict(settings)
    model = settings["model"]
    settings["effort"] = runtime.effective_effort(model, settings["effort"])
    settings["speed"] = runtime.effective_speed(model, settings["speed"])
    summary = settings["summary"] = runtime.reasoning_summary(model, settings["summary"])
    grace = getattr(runtime, "INTERRUPT_GRACE_SECONDS", 30)
    inactivity = getattr(runtime, "AUTHOR_INACTIVITY_SECONDS", 30 * 60)
    probe_limit = getattr(runtime, "AUTHOR_PROBE_SECONDS", 60)
    api = providers.provider_kind(model) == providers.API
    summaries = not api and summary in REASONING_SUMMARY_LEVELS
    final = tuple(error for error in (getattr(runtime, "FinalError", None), retrieval_runtime.RetrievalPolicyError,
                                      getattr(runtime, "ConversationAttemptFailed", None)) if error is not None)
    secret = ""
    transport = private = environment = copies = base_command = None
    tools, denied = [], []
    # "message" is the uuid of the turn's message, "delivered" whether Claude took
    # it into the conversation and "output" whether the model produced anything;
    # "opened" says that some CLI of this attempt reopened the conversation.
    state = {"initialized": False, "opened": False, "running": False, "message": None, "delivered": False,
             "output": False, "compacted": False, "answer": "", "restored": False, "stopped": False}
    activity = {"last": time.monotonic(), "probe": None, "at": None}

    def scrub(text):
        return redact(text, secret, runtime)

    def scrub_event(text):
        return scrub_record(text, secret, runtime)

    translator = EventTranslator("app", scrub=scrub_event, summaries=summaries)

    def record(event, root=True):
        event = runtime.public_event(event)
        if event is not None:
            emit("codex_event", event=event, root=root, provider="claude")

    def launch():
        """Start a CLI on the saved conversation and send it the turn's message.

        A CLI started before (one that ended, or that opened a copy of the
        conversation) is stopped first; what it received does not count.
        """

        nonlocal transport
        if transport is not None:
            transport.close(runtime, grace, interrupt=True)
        flags = ["--resume", session, *(["--resume-session-at", rewind] if _session_id(rewind) else [])]
        transport = SessionProcess(batch_safe([base_command[0], *flags, *base_command[1:]], runtime),
                                   directory, environment, scrub)
        state.update(initialized=False, running=True, message=str(uuid.uuid4()), delivered=False, output=False,
                     answer="", compacted=False)
        activity.update(last=time.monotonic(), probe=None, at=None)
        try:
            transport.send(_user_message(instruction, state["message"]))
        except (OSError, ValueError):
            pass  # A CLI that stops at once never reads its input; its output says why.

    def stop_leftovers():
        """Stop, once, the CLIs an earlier runner left on the saved conversation and start again; return whether it did."""

        if state["stopped"]:
            return False
        state["stopped"] = True
        leftovers = leftover_authors(session, {transport.process.pid})
        if not leftovers:
            return False
        emit("diagnostic", threadId=session,
             text=f"A Claude CLI left by an earlier TCS Prover runner (PID {', '.join(map(str, leftovers))}) still "
                  "held the saved author conversation; it was stopped and the conversation is reopened.")
        stop_processes(leftovers, grace)
        launch()
        return True

    try:
        if api:
            secret = providers.api_model_secret(model)
            if not secret:
                raise _failure(runtime, "ConfigurationError")(
                    f"Add an API key for {providers.model_label(model)} in Advanced settings → API models.")
            if hasattr(runtime, "register_secret"):
                runtime.register_secret(secret)
        live_search = search_is_live(directory, runtime)
        inherited = runtime.environment(model)
        # An API model reopens the conversation in the author's own configuration folder.
        home, _ = (api_configuration(model, settings["effort"], secret, directory, inherited, runtime)
                   if api else (None, ""))
        bare = api and home is None
        if getattr(runtime, "CONVERSATION_READ_ONLY", True):
            tools, allowed = tool_permissions("structured", (), live_search=live_search, bash=False, bare=bare)
        else:
            tools, allowed = tool_permissions("author", features, live_search=live_search, bash=sandbox_supported(),
                                              bare=bare)
        denied = disallowed_tools()
        protected, protected_paths = protected_settings(directory)
        temp = bash_temp(tools, directory, runtime)
        private = PrivateSettings(lambda path: {
            **protected,
            **({"sandbox": sandbox_settings(hidden_paths(api), protected_paths, workspace=directory, temp=temp,
                                            live=live_search)}
               if "Bash" in tools else {}),
            **({"env": gateway_environment(model, secret)} if api else {}),
        }, temp=temp)
        environment = child_environment(inherited, settings["effort"], api=api, config=home, temp=temp)
        try:
            copies = _run_folder(providers.CLAUDE_TRANSCRIPTS, directory, runtime)
        except runtime.Error as exc:
            emit("diagnostic", text=f"No private copy of the author conversation can be kept: {exc}")
        harness = private.write(HARNESS_FILE, CONVERSATION_HARNESS_NOTE)
        base_command = command_arguments(
            executable(runtime), model, settings["effort"], tools, allowed, private.path, bare=bare, denied=denied,
            extra=["--input-format", "stream-json", "--replay-user-messages",
                   *(SUMMARY_DISPLAY if summaries else []), "--append-system-prompt-file", str(harness)],
        )
        launch()
        if _session_id(rewind):
            emit("status", label="Failed turn removed", threadId=session,
                 text="The failed reply turn ended before the model produced anything; it was removed and the "
                      "message is sent again.")
        while True:
            try:
                message = transport.next(0.5)
            except queue.Empty:
                now = time.monotonic()
                if activity["at"] is not None and now - activity["at"] >= probe_limit:
                    raise runtime.Error(
                        f"Claude stopped responding: no event for {runtime._span_text(now - activity['last'])} and "
                        f"no answer to a check within {runtime._span_text(probe_limit)}.")
                if activity["at"] is None and state["running"] and now - activity["last"] >= inactivity:
                    # Any message, the answer included, shows that the CLI still works.
                    activity.update(at=now, probe=f"check-{uuid.uuid4().hex[:12]}")
                    try:
                        transport.send({"type": "control_request", "request_id": activity["probe"],
                                        "request": {"subtype": "mcp_status"}})
                    except (OSError, ValueError):
                        pass  # The CLI ended; its output reports why.
                    emit("diagnostic", text=f"No author event for {runtime._span_text(inactivity)}; checking that "
                                            "the Claude CLI still answers.", threadId=session)
                continue
            activity.update(last=time.monotonic(), at=None)
            if message is None or (message.get("type") == "result" and not state["initialized"]):
                detail = failure_detail(message, transport.stderr().splitlines(), secret, runtime, 1600)
                if not state["initialized"]:
                    if _missing_session(detail):
                        if not state["restored"]:
                            state["restored"] = True
                            if restore_transcript(config_directory(environment), session, copies):
                                emit("status", label="Saved conversation restored", threadId=session,
                                     text="Claude Code no longer had the saved author conversation; its private copy "
                                          "was put back and the conversation continues.")
                                launch()
                                continue
                        raise _failure(runtime, "SavedConversationGone")(
                            "Claude Code no longer has the saved author conversation.")
                    if stop_leftovers():
                        continue
                    lowered = detail.lower()
                    if (lowered.strip() and not getattr(runtime, "_plain_failure", lambda text: "")(detail)
                            and any(word in lowered for word in (session, "session", "conversation", "resume",
                                                                 "transcript"))):
                        # Claude refused to reopen the conversation; it is not a provider failure.
                        first = next((line.strip() for line in detail.splitlines() if line.strip()), detail)
                        raise _failure(runtime, "ConversationRefused")(_clip(first, 300))
                raise runtime.Error("Claude CLI stopped unexpectedly." + (f"\n{detail}" if detail else ""))
            kind = message.get("type")
            retrying = api_retry_text(message)
            if retrying:
                emit("status", label="Claude API retry", text=retrying, threadId=session)
                continue
            if kind == "command_lifecycle":
                if str(message.get("command_uuid") or "") == state["message"]:
                    if message.get("state") == "started":
                        state["delivered"] = True
                    elif message.get("state") == "cancelled":
                        raise runtime.Error("Claude Code dropped the message before the model received it.")
                continue
            if kind == "system":
                subtype = message.get("subtype")
                root = message.get("parent_tool_use_id") is None
                if subtype == "init" and root:
                    surprise = unexpected_tools(message, tools)
                    if surprise:
                        raise surprise_failure(surprise, denied, runtime, "the reply was stopped")
                    if not state["initialized"]:
                        reported = str(message.get("session_id") or "").lower()
                        if reported and reported != session:
                            # Claude opened a copy: another process holds the saved conversation.
                            if stop_leftovers():
                                continue
                            raise runtime.Error("Claude could not reopen the saved author conversation: it is open "
                                                "in another Claude Code process.")
                        state["initialized"] = state["opened"] = True
                        emit("status", label="Conversation reopened", threadId=session, text=f"Thread {session}")
                        emit("request", label=label, text=instruction, threadId=session, model=model,
                             reasoningEffort=settings["effort"], reasoningSummary=summary,
                             **({"attempt": attempt} if attempt > 1 else {}))
                elif subtype == "compact_boundary":
                    for event, event_root in translator.message(message):
                        record(event, event_root)
                    # The reply needs no re-anchor; the next Resume sends the full resume prompt (goal_reanchor).
                    state["compacted"] = state["compacted"] or root
                continue
            if kind == "user" and message.get("isReplay"):
                if str(message.get("uuid")) == state["message"]:
                    state["delivered"] = True
                continue
            if kind in {"assistant", "user"}:
                for event, root in translator.message(message):
                    record(event, root)
                if (kind == "assistant" and message.get("parent_tool_use_id") is None
                        and not api_error_message(message)):
                    state["output"] = True  # The model worked in this turn.
                    body = message.get("message") if isinstance(message.get("message"), dict) else {}
                    content = body.get("content") if isinstance(body.get("content"), list) else []
                    text = "\n\n".join(str(block.get("text") or "").strip() for block in content
                                       if isinstance(block, dict) and block.get("type") == "text"
                                       and str(block.get("text") or "").strip())
                    if text:
                        state["answer"] = text
                continue
            if kind == "control_response":
                response = message.get("response") if isinstance(message.get("response"), dict) else {}
                if response.get("subtype") == "error" and response.get("request_id") != activity["probe"]:
                    emit("diagnostic", text=scrub(f"Claude rejected a control request: {response.get('error')}"))
                continue
            if kind != "result":
                continue
            state["running"] = False
            if message.get("is_error"):
                raise runtime.Error("Claude reply turn failed: "
                                    + (failure_detail(message, (), secret, runtime, 1600) or "no complete result"))
            reply = message.get("result") if isinstance(message.get("result"), str) else ""
            reply = reply if reply.strip() else state["answer"]
            if not reply.strip():
                raise runtime.Error("The author's turn ended without a reply.")
            return {"reply": reply, "threadId": session, "compacted": state["compacted"]}
    except final:
        raise
    except (runtime.Error, OSError) as exc:
        # The failed turn's message, when the model received it and produced nothing: the retry resumes the
        # conversation before it.
        empty = state["delivered"] and not state["output"]
        anchor = last_answered_message(config_directory(environment), session) if empty and environment else None
        raise _failure(runtime, "ConversationAttemptFailed")(str(exc) or type(exc).__name__, retry={
            "delivered": state["delivered"], "output": state["output"], "rewind": anchor, "classic": False}) from exc
    finally:
        if transport is not None:
            transport.close(runtime, grace, interrupt=state["running"])
        if state["opened"] and environment is not None:
            save_transcript(config_directory(environment), session, copies)
        if private is not None:
            private.close()
