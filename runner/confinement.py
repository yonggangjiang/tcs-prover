"""Per-run read/write confinement of model processes (macOS, and Linux with bubblewrap).

Every model process of a run (the author's goal session and its conversation
turns, and every structured call) may read only its workspace, a private temp
folder and the system and toolchain folders that commands need, and may write
only its workspace (minus the entries that stay read-only, PROTECTED_ENTRIES)
and that temp folder. Other runs, the checkout, the home folder, /tmp,
/var/tmp and the shared $TMPDIR stay out of reach. Codex enforces this with a
permission profile (workflow_runner.private_permissions()), with Seatbelt on
macOS and with bubblewrap on Linux; Claude's Bash with its sandbox settings
(claude_cli.sandbox_settings(); on macOS only, see claude_supported()), while
--restricted keeps Claude's file tools in the working directory and deny rules
keep them off the protected entries (claude_cli.protected_settings()). Other
platforms, and Linux without a bubblewrap that can make its sandbox, keep their
legacy sandboxes (see supported()). In a live run every model process
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

Verified with Codex 0.170 and `codex sandbox` (no model) on Linux (Ubuntu
24.04, kernel 6.8, the system's bubblewrap 0.9, which Codex runs from PATH and
AppArmor's bwrap profile lets make user namespaces): the same profile holds.
":minimal" is /bin, /etc, /lib, /lib64, /sbin, /usr, /dev and /proc, so /var
(with /var/tmp), /run, /opt and the home folder stay out, and /tmp is an
empty, unwritable folder. The sandbox's root is a fresh folder with only what
the profile grants: a folder on the way to a granted one, such as the runs
folder, shows only the entries on the way (the run's own folder and
.controller), never another run. Codex runs its own binary in the sandbox
(codex-linux-sandbox), so the folder Codex is installed in is granted
(codex_packages()), and /etc/resolv.conf, a link into /run on systemd
systems, needs its target granted for any name to resolve (system_files()).
Commands get their own process namespace, so `lsof -U` lists no other
process's sockets, and reach a Unix socket only in a folder they can read; with
network access they share the computer's network, localhost included.
"""

import atexit
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
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
# The same on Linux, where ":minimal" already has /usr (with /usr/local) and /etc: a TeX Live or
# Conda in /opt, the formats Debian's TeX Live generates in /var/lib/texmf, and the toolchains in
# the home folder (Lean's elan, Conda, programs and Python packages installed with --user).
LINUX_TOOLCHAIN_FOLDERS = (
    "/opt/texlive", "/opt/conda", "/var/lib/texmf",
    "~/.elan", "~/miniconda3", "~/miniforge3", "~/anaconda3", "~/.local/bin", "~/.local/lib",
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
# Codex's own programs: the codex binary (apply_patch runs it) and its bundled rg and zsh. On
# Linux also its standalone installation (~/.local/share/codex/<version>/: bin/codex and the
# bundled rg), whose binary every confined command starts with (codex-linux-sandbox).
CODEX_PACKAGES = "~/.codex/packages"
LINUX_CODEX_PACKAGES = ("~/.codex/packages", "~/.local/share/codex")
# The folders ":minimal" grants on Linux (verified with Codex 0.170), and the system files that
# may be links out of them: /etc/resolv.conf points into /run on systemd systems.
LINUX_MINIMAL = ("/bin", "/etc", "/lib", "/lib64", "/sbin", "/usr")
LINUX_SYSTEM_FILES = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/localtime",
                      "/etc/ssl/certs/ca-certificates.crt")
# Claude Code's native binaries: its shell runs rg (and grep, find) through the claude binary.
CLAUDE_VERSIONS = "~/.local/share/claude/versions"
# The private temp folders made here are named <prefix><owner's PID>-<8 characters>, so the
# folder of a process killed before it removed it is swept at the next start (sweep_temps()):
# a runner's own one without a run temp folder from the controller, one per structured call
# attempt, and one per Claude Code process.
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
UNSUPPORTED_NOTICE = ("Run confinement is enforced on macOS, and on Linux with bubblewrap (bwrap); this run's "
                      "model can read files outside its folder.")
# On Linux the Codex roles are confined, Claude Code's shell not yet (claude_supported()).
CLAUDE_UNSUPPORTED_NOTICE = ("On Linux run confinement covers Codex roles only; this run's Claude Code shell can "
                             "read files outside its folder.")
# A bubblewrap that cannot make its sandbox (no user namespaces) answers within this time.
BUBBLEWRAP_CHECK_SECONDS = 10


class ConfinementError(RuntimeError):
    """A private temp folder or the startup check failed; the message says what is wrong."""


def supported():
    """Whether Codex's model processes run confined: macOS (Seatbelt), or Linux where bubblewrap works.

    Elsewhere the legacy sandboxes apply.
    """

    if sys.platform == "darwin":
        return True
    return sys.platform.startswith("linux") and bubblewrap_works()


def claude_supported():
    """Whether Claude Code's shell runs confined: macOS only (its Linux sandbox is not verified with these rules)."""

    return sys.platform == "darwin"


_BUBBLEWRAP = {}


def bubblewrap_works():
    """Whether the bubblewrap on PATH can make the sandbox Codex makes (Linux; checked once per process).

    Codex's Linux sandbox runs `bwrap` from PATH. It needs user namespaces,
    which a kernel or an AppArmor rule may refuse (Ubuntu 24.04 grants them
    to /usr/bin/bwrap only); then this is False and runs keep the legacy
    sandboxes, with the activity log saying so (UNSUPPORTED_NOTICE).
    """

    if "works" not in _BUBBLEWRAP:
        bwrap = shutil.which("bwrap")
        try:
            works = bool(bwrap) and subprocess.run(
                [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-user", "--unshare-pid", "--unshare-net",
                 "--", "/bin/true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=BUBBLEWRAP_CHECK_SECONDS).returncode == 0
        except (OSError, subprocess.SubprocessError):
            works = False
        _BUBBLEWRAP["works"] = works
    return _BUBBLEWRAP["works"]


def _resolved(entry):
    return Path(os.path.realpath(os.path.expanduser(entry)))


def toolchain_folders(home_only=False):
    """The TOOLCHAIN_FOLDERS (on Linux LINUX_TOOLCHAIN_FOLDERS) that exist (resolved), or only those in the home folder."""

    entries = LINUX_TOOLCHAIN_FOLDERS if sys.platform.startswith("linux") else TOOLCHAIN_FOLDERS
    found = [_resolved(entry) for entry in entries
             if (not home_only or entry.startswith("~")) and os.path.isdir(os.path.expanduser(entry))]
    return list(dict.fromkeys(found))


def codex_packages(executable=None):
    """Codex's installed programs ([] when Codex is installed elsewhere).

    On Linux also the installation of ``executable``, the Codex that runs
    (its real path's <root>/bin/codex when <root> holds codex-package.json or
    package.json, else the binary itself): Codex starts its own binary in the
    sandbox, so a Codex installed elsewhere than LINUX_CODEX_PACKAGES works too.
    """

    if not sys.platform.startswith("linux"):
        path = _resolved(CODEX_PACKAGES)
        return [path] if path.is_dir() else []
    found = [_resolved(entry) for entry in LINUX_CODEX_PACKAGES if os.path.isdir(os.path.expanduser(entry))]
    if executable:
        binary = _resolved(executable)
        root = binary.parent.parent
        if binary.parent.name == "bin" and any((root / name).is_file() for name in ("codex-package.json",
                                                                                     "package.json")):
            found.append(root)
        elif binary.is_file():
            found.append(binary)
    minimal = [Path(folder) for folder in LINUX_MINIMAL]
    found = [path for path in found if not any(_inside(path, folder) for folder in minimal)]
    # Drop one inside another (a version folder of ~/.local/share/codex).
    return [path for path in dict.fromkeys(found) if not any(other != path and _inside(path, other) for other in found)]


def system_files():
    """On Linux, the targets of LINUX_SYSTEM_FILES that lie outside ":minimal" (/etc/resolv.conf's in /run); else []."""

    if not sys.platform.startswith("linux"):
        return []
    found = []
    for entry in LINUX_SYSTEM_FILES:
        target = _resolved(entry)
        if target.is_file() and not any(_inside(target, Path(folder)) for folder in LINUX_MINIMAL):
            found.append(target)
    return list(dict.fromkeys(found))


def existing_entries(profile, workspace):
    """On Linux, ``profile`` naming only the protected entries of ``workspace`` that exist, after making Codex's
    project folders there (project_folders()); elsewhere it unchanged.

    Codex 0.170's Linux sandbox bind-mounts each entry a profile names under
    ":workspace_roots", and for one that is missing it puts an empty
    placeholder file in the workspace for the command's lifetime: commands
    of two processes in the same folder then race (one removes the
    placeholder while the other's bubblewrap still needs it, "bwrap: Can't
    find source path"), and a process killed meanwhile leaves the file
    behind. Codex's own project folders (.codex, .agents, .git, .aws) stay in
    the profile whatever exists, so that a model can never create one (a
    planted .codex/config.toml would start commands outside the sandbox); their
    commands mount them without a source, but loading a conversation to
    continue it (`codex exec fork`, thread/fork) needs each one to exist, so
    project_folders() makes the missing ones first. The controller folders are
    made before the author starts (controller_folders()), so in a run folder
    this leaves out only a missing pause.json, which the controller never
    reads back from there (its own copy is the one it relies on).
    """

    if not profile or workspace is None or not sys.platform.startswith("linux"):
        return profile
    roots = profile.get("filesystem", {}).get(":workspace_roots")
    if not isinstance(roots, dict):
        return profile
    project_folders(workspace, [name for name in roots if name in retrieval_runtime.PROTECTED_DIRECTORIES])
    kept = {name: access for name, access in roots.items()
            if name == "." or name in retrieval_runtime.PROTECTED_DIRECTORIES
            or os.path.lexists(os.path.join(workspace, name))}
    return {**profile, "filesystem": {**profile["filesystem"], ":workspace_roots": kept}}


def project_folders(workspace, names=retrieval_runtime.PROTECTED_DIRECTORIES):
    """Make Codex's project folders ``names`` (.codex, .agents, .git, .aws) that are missing in ``workspace``: empty
    and owner-only, read-only to every model process (private_permissions()).

    On 20 Nov 2026 an author's follow-up failed at launch because the run folder had no .git yet: Codex 0.170 loads
    the conversation it continues through its Linux sandbox, whose bubblewrap needs every such folder the profile
    names ("bwrap: Can't find source path …/.git"). Empty folders are what a confined run folder ends up with anyway,
    and what a web-cutoff run makes for every role (retrieval_runtime.verify_protected_directories()); git treats an
    empty .git as no repository. An entry that exists (a file or a link included) is left as it is, and a folder that
    cannot be made leaves the call to fail as before.
    """

    for name in names:
        path = os.path.join(workspace, name)
        if os.path.lexists(path):
            continue
        try:
            os.mkdir(path, 0o700)
        except OSError:
            pass


def controller_folders(workspace):
    """On Linux, make the run folder's controller folders (prompts, related-works) that are missing, owner-only.

    They then exist for as long as the run's model processes do, so their
    profile names them without a placeholder (existing_entries()); an empty
    related-works folder is what TCS Prover makes for a first related work
    anyway (ui/related_works.py). Elsewhere nothing is made.
    """

    if not sys.platform.startswith("linux"):
        return
    for name in PROTECTED_FOLDERS:
        if name in retrieval_runtime.PROTECTED_DIRECTORIES:
            continue
        try:
            os.mkdir(os.path.join(workspace, name), 0o700)
        except FileExistsError:
            pass


def shared_temp_folders():
    """The shared temp folder that ":minimal" may grant besides /tmp, denied again: /private/var/tmp, or /var/tmp."""

    return ["/var/tmp"] if sys.platform.startswith("linux") else ["/private/var/tmp"]


def slash_tmp_folder():
    """/tmp as the sandbox sees it: /private/tmp on macOS."""

    return Path("/private/tmp") if sys.platform == "darwin" else Path("/tmp")


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


def inside(path, folder):
    """Whether ``path`` is ``folder`` or lies in it (both as given, not resolved)."""

    return path == folder or folder in path.parents


_inside = inside


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


def call_parent():
    """The folder that holds the folders of model calls (their snapshots, private temp folders): None for the system's.

    On Linux a folder of TCS Prover's own, owner-only, outside /tmp
    ($XDG_CACHE_HOME or ~/.cache, then tcs-prover/tmp): Codex 0.170's
    sandbox there mounts its empty /tmp (the profile denies /tmp) over the
    folders the profile lets a command read, and mounts only the ones it may
    write again on top, so a read-only snapshot in /tmp could not be read.
    None (the system's temp folder) elsewhere, and when that folder cannot
    be made; workflow_runner.private_permissions() then leaves /tmp to a
    call whose read-only folder is in it.
    """

    if not sys.platform.startswith("linux"):
        return None
    folder = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tcs-prover" / "tmp"
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _check_folder(Path(os.path.realpath(folder)), None)
    except (ConfinementError, OSError):
        return None


def fresh_folder(prefix, parent=None, workspace=None):
    """A new owner-only temp folder named after this process (resolved; its caller removes it), outside ``workspace``.

    Without a ``parent`` it is made in call_parent(), else the system's temp folder.
    """

    parent = parent if parent is not None else call_parent()
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
    """A short private temp folder for one Claude Code process (CLAUDE_CODE_TMPDIR), as /tmp/... (see CLAUDE_TEMP_PARENT).

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
    for parent in dict.fromkeys(str(folder) for folder in (call_parent() or tempfile.gettempdir(),
                                                            tempfile.gettempdir(), CLAUDE_TEMP_PARENT)):
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
    command, made of system tools only, must read the workspace (and write
    it when ``writable``) and write and read the private temp folder
    ``temp``; it must not read a controller file next to the temp folder or
    a file of this checkout, list the folder holding the workspace, or write
    /tmp. On Linux that folder can be listed, as bubblewrap shows the
    folders on the way to the granted ones: there the listing must not name
    a file the check puts next to the workspace, as another run's folder
    would be. Raises ConfinementError when the report is missing or the
    sandbox did not hold; the caller retries and never starts the model
    unconfined.
    """

    nonce = uuid.uuid4().hex
    workspace, temp = Path(os.path.realpath(workspace)), Path(os.path.realpath(temp))
    marker = f".tcs-confinement-{nonce}"
    inside, written, scratch = workspace / marker, workspace / f"{marker}-written", temp / marker
    outside, slash_tmp = temp.parent / marker, slash_tmp_folder() / f"{marker}-written"
    linux = sys.platform.startswith("linux")
    sibling = workspace.parent / f"{marker}-sibling"
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
        probes["parentList"] = (f"/bin/ls -a {q(workspace.parent)} | /bin/grep -qxF {q(sibling.name)}" if linux
                                else f"/bin/ls {q(workspace.parent)}")
    expected = {"nonce": nonce, **{key: key in ("workspaceRead", "tempWrite") for key in probes},
                "workspaceWrite": bool(writable)}
    created = []
    try:
        for path in (inside, outside, *([sibling] if linux and "parentList" in probes else [])):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(nonce)
        result = execute({"command": _report_command(nonce, probes), "cwd": str(workspace),
                          "permissionProfile": profile, "timeoutMs": CHECK_TIMEOUT_MS})
    finally:
        for path in (*created, written, scratch, slash_tmp, sibling):
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
