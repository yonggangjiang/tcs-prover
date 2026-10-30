"""Per-run read/write confinement of model processes (macOS).

Every model process of a run (the author's goal session and its conversation
turns, and every structured call) may read only its workspace, a private temp
folder and the system and toolchain folders that commands need, and may write
only its workspace (minus the entries that stay read-only, PROTECTED_ENTRIES)
and that temp folder. Other runs, the checkout, the home folder, /tmp,
/var/tmp and the shared $TMPDIR stay out of reach. Codex enforces this with a
permission profile (workflow_runner.private_permissions()); Claude's Bash with
its sandbox settings (claude_cli.sandbox_settings()), while --restricted keeps
Claude's file tools in the working directory and deny rules keep them off the
protected entries (claude_cli.protected_settings()). Other platforms keep
their legacy sandboxes (see supported()). In a live run every model process
reaches the internet (workflow_runner.live_network()); a Codex process with
network access can also connect to localhost and to any Unix socket whose
path it knows. No model process, on any platform, gets the variables that
name the user's SSH and GPG agents (AGENT_VARIABLES), but `lsof -U` lists
the sockets of the user's other processes, the SSH agent's among them.

Verified with Codex 0.160 and `codex sandbox` (no model) on macOS 26: the
profile has no `extends`; ":minimal" is the system runtime and also grants
/tmp and /var/tmp, so both are denied again; ":tmpdir" follows the Codex
process's TMPDIR (the private temp folder); the most specific path wins, and
"deny" wins on the same path. zsh writes heredocs to TMPPREFIX (default
/tmp/zsh) and git reads ~/.gitconfig, hence temp_environment(). macOS mktemp
ignores TMPDIR (`mktemp -p "$TMPDIR"` works), and so do the heredocs of
macOS's /bin/sh and /bin/bash (bash 3.2): they use /var/tmp or /tmp when
writable and otherwise the current folder, so they work only where the
current folder is writable. Codex runs a model's commands in zsh.
"""

import atexit
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
import uuid

from retrieval import runtime as retrieval_runtime


# Read-only software folders that commands on a model's PATH need: Homebrew,
# /usr/local (TeX Live, node), MacPorts, MacTeX, the Command Line Tools
# (/usr/bin/python3 and git run from there), python.org Python, Perl
# (latexmk), and the toolchains in the home folder on the owner's PATH (Lean's
# elan, Anaconda). Only the folders that exist are granted, read-only. To let
# a run's model use another toolchain, add its installation folder here: only
# software, never a folder that holds user data (every run can read it).
TOOLCHAIN_FOLDERS = (
    "/opt/homebrew", "/usr/local", "/opt/local", "/Library/TeX", "/Library/Developer/CommandLineTools",
    "/Library/Frameworks/Python.framework", "/System/Library/Perl", "/Library/Perl",
    "~/.elan", "~/opt/anaconda3",
)
# Files the controller writes into the run folder (it keeps its own copies outside it and relies
# on those) and the user's related works (a folder run hard-links one copy into every job).
CONTROLLER_FILES = ("job-settings.json", "pause.json", "checked-statement.md", "prompts", "related-works")
# The run folder's entries that every model process may only read: Codex's project folders (a
# .codex/config.toml planted there would start commands outside the sandbox when Codex resumes)
# and the controller's files. Of them, these are folders.
PROTECTED_ENTRIES = (*retrieval_runtime.PROTECTED_DIRECTORIES, *CONTROLLER_FILES)
PROTECTED_FOLDERS = (*retrieval_runtime.PROTECTED_DIRECTORIES, "prompts", "related-works")
# The variables that tell a process where the user's SSH and GPG agents listen. A Codex process
# with network access can connect to any Unix socket whose path it knows, so no model process gets
# them (without_agents()), confined or in a legacy sandbox elsewhere.
AGENT_VARIABLES = ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO")
# Codex's own programs: the codex binary (apply_patch runs it) and its bundled rg and zsh.
CODEX_PACKAGES = "~/.codex/packages"
# Claude Code's native binaries: its shell runs rg (and grep, find) through the claude binary.
CLAUDE_VERSIONS = "~/.local/share/claude/versions"
# The private temp folders made here are named <prefix><owner's PID>-<8 characters>, so the
# folder of a process killed before it removed it is swept at the next start (sweep_temps()):
# a runner's own one without a run temp folder from the controller, one per structured call
# attempt, and one per Claude CLI.
RUN_TEMP_PREFIX, CALL_TEMP_PREFIX, CLAUDE_TEMP_PREFIX = "tcs-run-tmp-", "tcs-call-tmp-", "tcs-prover-"
OWNED_TEMP_NAME = re.compile(r"(?:tcs-run-tmp|tcs-call-tmp|tcs-prover)-(\d+)-[A-Za-z0-9_]{8}\Z")
# Claude Code gives Bash $TMPDIR=<CLAUDE_CODE_TMPDIR>/claude-<uid> only when that path has at
# most 44 bytes, and /tmp/claude-<uid> otherwise (Claude Code 2.1.289), so a Claude role's
# private temp folder is a short one in /tmp: /tmp/tcs-prover-<PID>-<8 characters>.
CLAUDE_TEMP_PARENT = "/tmp"
# Folders Claude Code's Bash sandbox may otherwise read: the home folder is added at run time.
CLAUDE_DENIED_READS = ("/Volumes", "/private/tmp", "/private/var/tmp", "/private/var/folders")
# Shared folders Claude Code's Bash sandbox may otherwise write.
CLAUDE_DENIED_WRITES = ("/tmp/claude", "/private/tmp/claude")
# The command of the startup check may take this long.
CHECK_TIMEOUT_MS = 10000
UNSUPPORTED_NOTICE = ("Run confinement is enforced on macOS only; this run's model can read files outside its "
                      "folder.")


class ConfinementError(RuntimeError):
    """A private temp folder or the startup check failed; the message says what is wrong."""


def supported():
    """Whether model processes run confined (macOS Seatbelt); elsewhere the legacy sandboxes apply."""

    return sys.platform == "darwin"


def _resolved(entry):
    return Path(os.path.realpath(os.path.expanduser(entry)))


def toolchain_folders(home_only=False):
    """The TOOLCHAIN_FOLDERS that exist (resolved), or only those in the home folder."""

    found = [_resolved(entry) for entry in TOOLCHAIN_FOLDERS
             if (not home_only or entry.startswith("~")) and os.path.isdir(os.path.expanduser(entry))]
    return list(dict.fromkeys(found))


def codex_packages():
    """Codex's installed programs ([] when Codex is installed elsewhere)."""

    path = _resolved(CODEX_PACKAGES)
    return [path] if path.is_dir() else []


def claude_versions():
    """Claude Code's native binaries ([] for another installation)."""

    path = _resolved(CLAUDE_VERSIONS)
    return [path] if path.is_dir() else []


def temp_environment(temp):
    """The environment of a confined Codex process: TMPDIR and zsh heredocs in its temp folder, no global git config."""

    temp = str(temp)
    return {"TMPDIR": temp, "TMPPREFIX": os.path.join(temp, "zsh"), "GIT_CONFIG_GLOBAL": os.devnull}


def without_agents(environment):
    """``environment`` without the variables that name the user's SSH and GPG agents (AGENT_VARIABLES)."""

    return {name: value for name, value in environment.items() if name not in AGENT_VARIABLES}


def confined_environment(environment, temp):
    """The environment of a confined Codex process: ``environment`` without the agents, with temp_environment(temp)."""

    return {**without_agents(environment), **temp_environment(temp)}


def _inside(path, folder):
    return path == folder or folder in path.parents


def _outside_workspace(folder, workspace):
    """Refuse a temp folder that is, holds or lies inside the workspace."""

    if workspace is not None:
        workspace = Path(os.path.realpath(workspace))
        if _inside(folder, workspace) or _inside(workspace, folder):
            raise ConfinementError(f"{folder} must be outside the run's folder {workspace}.")


def _check_folder(folder, workspace):
    """Refuse a link, a non-folder, another user's folder and one that overlaps the workspace; make it 0700."""

    info = os.lstat(folder)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfinementError(f"{folder} is a link or a file, not a folder; remove it.")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfinementError(f"{folder} belongs to another user; remove it.")
    _outside_workspace(folder, workspace)
    descriptor = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return folder


def private_folder(path, workspace):
    """The run's private temp folder ``path`` (resolved): created owner-only when missing, never a link.

    It lies outside the workspace: Codex resolves the paths of a profile when
    a session starts, and a model could replace a folder inside its
    workspace by a link to anywhere.
    """

    path = Path(path)
    if not path.is_absolute():
        raise ConfinementError(f"The private temp folder {path} is not an absolute path.")
    _outside_workspace(Path(os.path.realpath(path.parent)) / path.name, workspace)
    # The controller's folder of the run normally exists; one that vanished is made again, owner-only.
    missing = [parent for parent in path.parents if not os.path.lexists(parent)]
    for parent in reversed(missing):
        try:
            os.mkdir(parent, 0o700)
        except FileExistsError:
            pass
    folder = Path(os.path.realpath(path.parent)) / path.name
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    return _check_folder(folder, workspace)


def fresh_folder(prefix, parent=None, workspace=None):
    """A new owner-only temp folder named after this process (resolved; its caller removes it), outside ``workspace``."""

    folder = Path(os.path.realpath(tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}-", dir=parent)))
    try:
        return _check_folder(folder, workspace)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


_RUNNER_FOLDERS = {}


def runner_folder(workspace):
    """Without a run temp folder from the controller (tests, direct CLI use): one per workspace and runner process."""

    key = os.path.realpath(workspace)
    folder = _RUNNER_FOLDERS.get(key)
    if folder is None or not folder.is_dir():
        folder = _RUNNER_FOLDERS[key] = fresh_folder(RUN_TEMP_PREFIX, workspace=workspace)
        atexit.register(shutil.rmtree, folder, True)
    return folder


def claude_temp(workspace=None):
    """A short private temp folder for one Claude CLI process (CLAUDE_CODE_TMPDIR), as /tmp/... (see CLAUDE_TEMP_PARENT).

    Its name carries this process's ID: sweep_temps() removes the folders
    of processes that ended without removing theirs.
    """

    path = Path(tempfile.mkdtemp(prefix=f"{CLAUDE_TEMP_PREFIX}{os.getpid()}-", dir=CLAUDE_TEMP_PARENT))
    try:
        _check_folder(Path(os.path.realpath(path)), workspace)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def sweep_temps(alive):
    """Remove this user's private temp folders whose owner process has ended (``alive(pid)`` says whether it runs)."""

    if not supported():
        return
    for parent in dict.fromkeys((tempfile.gettempdir(), CLAUDE_TEMP_PARENT)):
        try:
            entries = list(Path(parent).iterdir())
        except OSError:
            continue
        for entry in entries:
            match = OWNED_TEMP_NAME.match(entry.name)
            if not match or int(match.group(1)) == os.getpid():
                continue
            try:
                info = entry.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not alive(int(match.group(1))):
                shutil.rmtree(entry, ignore_errors=True)


def _report_command(nonce, probes):
    """One /bin/sh command that runs each probe and prints {"nonce", <probe>: true|false} as JSON."""

    lines = [f"if ( {command} ) >/dev/null 2>&1; then {key}=true; else {key}=false; fi"
             for key, command in probes.items()]
    template = json.dumps({"nonce": nonce, **{key: "%s" for key in probes}}, separators=(",", ":"))
    template = template.replace('"%s"', "%s") + "\n"
    lines.append("printf " + shlex.quote(template) + " " + " ".join('"$' + key + '"' for key in probes))
    return ["/bin/sh", "-c", "\n".join(lines)]


_PROBE_TEXT = {
    "workspaceRead": "read the run's folder", "workspaceWrite": "write the run's folder",
    "tempWrite": "write its private temp folder", "outsideRead": "read the controller's folder",
    "checkoutRead": "read the TCS Prover checkout", "parentList": "list the folder that holds the run's folder",
    "slashTmpWrite": "write /tmp",
}


def startup_check(execute, *, workspace, temp, writable, profile, checkout=None):
    """Prove without a model that ``profile`` confines a command, before the author's first turn.

    ``execute(params)`` sends one command/exec request to the app-server
    (whose commands run as the model's do) and returns its result. The
    command, made of Apple system tools only, must read the workspace (and
    write it when ``writable``) and write and read the private temp folder
    ``temp``; it must not read a controller file next to the temp folder or
    a file of this checkout, list the folder holding the workspace, or write
    /tmp. Raises ConfinementError when the report is missing or the sandbox
    did not hold; the caller retries and never starts the model unconfined.
    """

    nonce = uuid.uuid4().hex
    workspace, temp = Path(os.path.realpath(workspace)), Path(os.path.realpath(temp))
    marker = f".tcs-confinement-{nonce}"
    inside, written, scratch = workspace / marker, workspace / f"{marker}-written", temp / marker
    outside, slash_tmp = temp.parent / marker, Path("/private/tmp") / f"{marker}-written"
    checkout = Path(checkout) if checkout else Path(__file__).resolve()
    q = lambda path: shlex.quote(str(path))
    probes = {
        "workspaceRead": f'test "$(/bin/cat {q(inside)})" = {q(nonce)}',
        "workspaceWrite": f"printf %s {q(nonce)} > {q(written)}",
        "tempWrite": f'printf %s {q(nonce)} > {q(scratch)} && test "$(/bin/cat {q(scratch)})" = {q(nonce)}',
        "outsideRead": f"/bin/cat {q(outside)}",
        "checkoutRead": f"/bin/cat {q(checkout)}",
        "slashTmpWrite": f"printf %s {q(nonce)} > {q(slash_tmp)}",
    }
    if workspace.parent != Path(workspace.anchor):
        probes["parentList"] = f"/bin/ls {q(workspace.parent)}"
    expected = {"nonce": nonce, **{key: key in ("workspaceRead", "tempWrite") for key in probes},
                "workspaceWrite": bool(writable)}
    created = []
    try:
        for path in (inside, outside):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(nonce)
        result = execute({"command": _report_command(nonce, probes), "cwd": str(workspace),
                          "permissionProfile": profile, "timeoutMs": CHECK_TIMEOUT_MS})
    finally:
        for path in (*created, written, scratch, slash_tmp):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                pass
    result = result if isinstance(result, dict) else {}
    try:
        report = json.loads(result.get("stdout") or "")
    except (TypeError, ValueError):
        report = None
    if not isinstance(report, dict) or set(report) != set(expected) or report.get("nonce") != nonce:
        detail = " ".join(str(result.get("stderr") or "").split())[-300:]
        raise ConfinementError("the check command returned no valid report" + (f" ({detail})" if detail else ""))
    held = [key for key, value in expected.items() if value is False and report[key] is not False]
    if held:
        raise ConfinementError("the sandbox did not hold: the check command could "
                               + ", ".join(_PROBE_TEXT[key] for key in held))
    blocked = [key for key, value in expected.items() if value is True and report[key] is not True]
    if blocked:
        raise ConfinementError("the sandbox blocked allowed work: the check command could not "
                               + ", ".join(_PROBE_TEXT[key] for key in blocked))
    return report
