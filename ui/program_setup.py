"""The Setup page: the programs the models run through (the Codex CLI and Claude Code), and LaTeX.

What it does. It checks each program as jobs find and start it, with no
model call (`--version`, `codex login status`, `claude auth status --json`,
`latexmk -v`, `pdflatex --version`), and runs the vendors' own installers,
updaters and sign-ins after one confirmation that shows the exact command,
with the live output and a Stop button. LaTeX gets commands to copy only.

Why the lookups jobs use. A program is found exactly as a job finds it
(runtime.codex(), claude_cli.executable(), runtime._find_tool(), plus the
folder this page remembered, providers.remembered_program()) and checked in
the environment jobs give it, so "Signed in" here means signed in for jobs:
an API key in the server's environment never counts.

Safety rules (each is tested):
- Fixed actions. The browser sends a program, an action and an option from
  fixed sets, plus the command it showed; argv comes only from the constants
  here and the server's own lookups, never from browser text. A command that
  changed since the dialog opened is refused (409) and nothing starts; so is a
  step for a program that changed since its last check in a way seen without
  starting anything (where jobs find it, what made it, a file of your own
  where OpenAI's installer writes), once a new check no longer offers it.
- No elevated rights: every action is refused while TCS Prover runs as root or
  administrator, and nothing ever uses sudo or runas.
- Subcommands are confirmed through `--help` before `login status`,
  `auth status`, `auth login` or `update` runs (Claude Code takes an unknown
  subcommand for a prompt, which with a login would be a paid model call).
- No terminal: a new session (POSIX) or a new process group with no window
  (Windows), stdin closed or a pipe, non-interactive installers. Stop ends the
  step's process group and every process group its descendants started; what
  an install or update leaves in its process group ends with it.
- Never replace a user's file: installs and updates run only when the
  command jobs use is missing or was made by the official installer.
- Running jobs first: an update, a repair and a sign-in that may replace a
  login wait while one of this server's jobs runs a model process through that
  program (a job whose calls all wait for that very step does not count).
- No process under the lock: checks run outside SetupTasks.lock, so Stop, the
  output of a step and closing never wait for one.
- Output stays on the page: every line is cleaned (escape codes, keys, tokens,
  sign-in codes, proxy passwords) before it is kept, the newest KEPT_LINES lines
  of the latest step of each program are kept in memory only, and nothing is
  written to disk, the terminal, run folders or transcripts.
- The only typed input is the code Claude Code's sign-in page may show, which
  goes to that process's stdin only.
"""

import codecs
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, confinement, providers

PROGRAMS = ("codex", "claude", "latex")
# The programs with actions; LaTeX shows commands to copy only.
ACTION_PROGRAMS = ("codex", "claude")
ACTIONS = ("install", "update", "sign-in", "use-folder")
SIGN_IN_OPTIONS = {"codex": ("browser", "code"), "claude": ("subscription", "console")}
NAMES = {"codex": providers.PROVIDER_LABELS[providers.CODEX], "claude": providers.PROVIDER_LABELS[providers.CLAUDE],
         "latex": "LaTeX"}
# A program in a sentence: "the Codex CLI", "Claude Code".
THE = {"codex": "the Codex CLI", "claude": "Claude Code", "latex": "LaTeX"}
COMMANDS = {"codex": "codex", "claude": "claude"}

# Limits, each shown on the page where it applies (no silent cut-off).
CHECK_SECONDS = runtime.CLI_CHECK_SECONDS          # One status command (20 s).
ANSWER_SECONDS = runtime.CODEX_HOME_FIRST_PROBE_SECONDS  # One Codex app-server answer during sign-in (120 s).
CANCEL_SECONDS = 5                                # The answer to Codex's sign-in cancel.
CACHE_SECONDS = 600                               # The last check, reused outside the Setup page.
STOP_GRACE_SECONDS = 10                           # Stop: a clean exit first, then the whole process tree.
CLOSE_GRACE_SECONDS = 2                           # The same when TCS Prover closes.
LEFTOVER_GRACE_SECONDS = 2                        # What an ended install or update left in its process group.
OUTPUT_GRACE_SECONDS = 5                          # Output read after a step's process has ended.
MOVED_TRIES = 3                                   # New checks of a program that keeps changing while a step starts.
KEPT_LINES = 2000                                 # The newest output lines of a step.
LINE_CHARS = 2000                                 # One output line.
CODE_CHARS = 512                                  # A pasted Claude sign-in code.
PARTIAL_SECONDS = 0.3                             # Text without a line break shown after this long.
TITLE_CHARS = 120                                 # A job title in a reason.

# The vendors' official one-line installers, run exactly as written (tests replace INSTALLERS).
# Codex: the README's line, with CODEX_NON_INTERACTIVE as Codex's own updater runs it (so its installer asks
# nothing). Claude Code: code.claude.com/docs/en/setup.
OFFICIAL_INSTALLERS = {
    ("codex", "posix"): "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
    ("codex", "windows"): "$env:CODEX_NON_INTERACTIVE=1; irm https://chatgpt.com/codex/install.ps1 | iex",
    ("claude", "posix"): "curl -fsSL https://claude.ai/install.sh | bash",
    ("claude", "windows"): "irm https://claude.ai/install.ps1 | iex",
}
INSTALLERS = OFFICIAL_INSTALLERS

# Commands to copy and run in a terminal (never run by Setup): [label, command] per group.
TERMINAL_COMMANDS = {
    ("codex", "install", "posix"): [("Official installer, in a terminal",
                                     "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
                                    ("Homebrew", "brew install --cask codex"),
                                    ("npm", "npm install -g @openai/codex")],
    ("codex", "install", "windows"): [("Official installer, in PowerShell",
                                       'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"'),
                                      ("npm", "npm install -g @openai/codex")],
    ("claude", "install", "posix"): [("Official installer, in a terminal", "curl -fsSL https://claude.ai/install.sh | bash"),
                                     ("Homebrew", "brew install --cask claude-code"),
                                     ("npm", "npm install -g @anthropic-ai/claude-code")],
    ("claude", "install", "windows"): [("Official installer, in PowerShell", "irm https://claude.ai/install.ps1 | iex"),
                                       ("Official installer, in Command Prompt",
                                        "curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd"),
                                       ("WinGet", "winget install Anthropic.ClaudeCode"),
                                       ("npm", "npm install -g @anthropic-ai/claude-code")],
    ("codex", "sign-in", "any"): [("Sign in in a terminal", "codex login"),
                                  ("From another computer", "codex login --device-auth")],
    ("claude", "sign-in", "any"): [("Sign in in a terminal", "claude auth login"),
                                   ("With an Anthropic Console account", "claude auth login --console")],
}
# How each kind of copy is updated (Codex's own update code; Claude Code's documentation).
UPDATE_COMMANDS = {
    ("codex", "installer", "posix"): "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    ("codex", "installer", "windows"): 'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"',
    ("codex", "homebrew"): "brew upgrade --cask codex",
    ("codex", "npm"): "npm install -g @openai/codex",
    ("claude", "installer"): "claude update",
    ("claude", "homebrew"): "brew upgrade claude-code",
    ("claude", "homebrew-latest"): "brew upgrade claude-code@latest",
    ("claude", "winget"): "winget upgrade Anthropic.ClaudeCode",
    ("claude", "npm"): "npm install -g @anthropic-ai/claude-code@latest",
}
ALPINE_COMMAND = ("Alpine Linux: the libraries Claude Code needs (as root)", "apk add bash curl libgcc libstdc++ ripgrep")
SANDBOX_COMMANDS = [("Debian or Ubuntu", "sudo apt install bubblewrap socat"),
                    ("Fedora", "sudo dnf install bubblewrap socat")]
# LaTeX: what to install per system (TCS Prover runs none of these). A row has a command to copy, or a link.
LATEX_COMMANDS = {
    "macos": [{"label": "MacTeX", "text": "Download it from", "link": "https://tug.org/mactex/"},
              {"label": "Homebrew", "command": "brew install --cask mactex-no-gui",
               "note": "About 5 GB; asks for an administrator password."}],
    "linux": [{"label": "Debian or Ubuntu", "command": "sudo apt install latexmk texlive-latex-extra"},
              {"label": "Fedora", "command": "sudo dnf install latexmk texlive-scheme-medium"},
              {"label": "Other Linux", "text": "TeX Live from", "link": "https://tug.org/texlive/"}],
    "windows": [{"label": "TeX Live", "text": "Download it from", "link": "https://tug.org/texlive/windows.html",
                 "note": "It includes Perl, which latexmk needs."},
                {"label": "MiKTeX", "command": "winget install MiKTeX.MiKTeX"},
                {"label": "Perl for latexmk, with MiKTeX", "command": "winget install StrawberryPerl.StrawberryPerl"}],
    "other": [{"label": "TeX Live", "text": "Download it from", "link": "https://tug.org/texlive/"}],
}
LATEX_HELP = ("TCS Prover does not run these: they need administrator rights and several gigabytes. Afterwards click "
              "Check again; the LaTeX step also looks in the standard TeX folders.")

# The hosts a sign-in link may lead to (any other address is shown as plain text, with a warning).
LINK_HOSTS = {"codex": ("auth.openai.com", "chatgpt.com"),
              "claude": ("claude.ai", "claude.com", "platform.claude.com", "console.anthropic.com")}

# Output cleaning (in this order): escape sequences, other control characters except tab, then secrets.
ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CONTROLS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")
SECRET_PATTERNS = (
    (re.compile(r"(?<![A-Za-z0-9_])sk-(?:ant-)?[A-Za-z0-9_-]{8,}"), "[removed key]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?"), "[removed token]"),
    (re.compile(r"(?i)\b(code|state|token|access_token|refresh_token|id_token|code_verifier)=[^&\s\"']+"),
     r"\1=[removed]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[removed]"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[removed]@"),
)
# A pasted Claude sign-in code: "code#state", nothing else.
CLAUDE_CODE = re.compile(r"[A-Za-z0-9._~+/=-]+#[A-Za-z0-9._~+/=-]+\Z")

# Help lines that show a subcommand exists ("  status   Show login status").
SUBCOMMAND = "(?m)^\\s{2,}%s\\b"
UPDATE_SUBCOMMAND = r"(?m)^\s{2,}update(\|upgrade)?\b"

# Variables that would change what the official Codex installer installs (a pinned release, a daemon only...).
INSTALLER_VARIABLES = ("CODEX_RELEASE", "CODEX_INSTALL_DAEMON_ONLY", "CODEX_INSTALL_DEFER_SELECTION",
                       "CODEX_INSTALL_IF_LATEST", "CODEX_INSTALL_IF_CURRENT", "CODEX_UPDATE_FROM_RELEASE",
                       "CODEX_INSTALLER_USE_RELEASES_OPENAI_COM")
QUIET_OUTPUT = {"NO_COLOR": "1", "TERM": "dumb", "FORCE_HYPERLINK": "0"}

# Results of a failed install or update: the first rule that matches (an exit code, a system with an exit code,
# or a pattern in the output), then the exit code.
FAILURES = (
    (6, "Could not find the download server. Check this computer's internet connection."),
    (7, "Could not connect to the download server. Check the internet connection or proxy settings."),
    (22, "The download server refused the request; it may not be available in your region."),
    (28, "The download timed out. Try again."),
    (35, "The secure connection to the download server failed. A proxy or security program may be in the way."),
    (60, "The secure connection to the download server failed. A proxy or security program may be in the way."),
    (re.compile(r"syntax error near unexpected token `<'"),
     "The download returned a web page instead of the installer, often because of a proxy or a regional block."),
    (re.compile(r"No space left on device"), "The disk is full."),
    (("linux", 137), "The system stopped the installer, most likely for lack of memory (Claude Code needs about "
                     "512 MB free)."),
    (re.compile(r"Cannot replace older standalone install without confirmation"),
     "The installer needs your answer before it replaces an older Codex install. Run the command yourself in "
     "PowerShell and answer its question."),
    (re.compile(r"another process is currently installing Claude|Another Claude process"),
     "Another Claude Code install or update is running. Try again in a moment."),
)

ADMIN_TEXT = {
    "root": "TCS Prover is running with administrator rights (root). Setup never installs or signs in that way: "
            "start TCS Prover as your normal user.",
    "admin": "TCS Prover is running as administrator. Setup never installs or signs in that way: start TCS Prover "
             "normally.",
}
UNKNOWN_ACTION = "Unknown Setup action."
STALE_TEXT = "The command changed since you opened this dialog. Check it and confirm again."
GONE_TEXT = "This Setup step is no longer available (TCS Prover may have restarted)."
PLATFORM_TEXT = "The official installers support macOS, Linux and Windows only."
CHANGED_TEXT = "This program changed since the last check. Click Check again."
SIGN_IN_GATE_TEXT = "TCS Prover could not check Claude Code's sign-in command. Click Check again."
NOT_WAITING_TEXT = "Claude Code is not waiting for a code now."
CODE_FORMAT_TEXT = "Paste the whole code from the sign-in page; it contains #."
CODE_REFUSED_TEXT = "Claude Code did not accept that code. Copy the whole code from the page and paste it again."
REPLACED_TEXT = "Replaced by a new sign-in."
STOPPED_SIGN_IN = "Sign-in stopped."
WAITING_TEXT = "Waiting for you to sign in…"


class SetupRefused(Exception):
    """A request the Setup page refuses: its words, its HTTP status and more of the answer (``body``).

    ``with_status`` asks the server to add the current GET /setup answer, so
    a stale dialog can show the new command.
    """

    def __init__(self, message, status=409, body=None, *, with_status=False):
        super().__init__(message)
        self.status, self.body, self.with_status = status, dict(body or {}), with_status


class CheckTimeout(Exception):
    """A status command did not answer within CHECK_SECONDS (its process tree was stopped)."""


class Stopped(Exception):
    """The user stopped a step (or TCS Prover is closing)."""


class SignInFailed(Exception):
    """A sign-in that cannot finish, with the words for the page (and whether the terminal commands help)."""

    def __init__(self, message, commands=False):
        super().__init__(message)
        self.commands = commands


class _Replace(Exception):
    """A confirmed sign-in that first ends the sign-in waiting for the user (``task``)."""

    def __init__(self, task):
        super().__init__("replace")
        self.task = task


class _Moved(Exception):
    """The program changed again since its last check while a step was being planned: check it again first."""


# --- The computer --------------------------------------------------------------------------------------------------

def platform_name():
    """"macos", "linux", "windows" or "other"."""

    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if os.name == "nt" or sys.platform in {"win32", "cygwin"}:
        return "windows"
    return "other"


def _family():
    return "windows" if platform_name() == "windows" else "posix"


def elevated():
    """"root" when TCS Prover runs as root, "admin" as a Windows administrator, else ""."""

    if os.name == "nt":
        try:
            import ctypes
            return "admin" if ctypes.windll.shell32.IsUserAnAdmin() else ""
        except (AttributeError, OSError, ImportError):
            return ""
    geteuid = getattr(os, "geteuid", None)
    return "root" if geteuid is not None and geteuid() == 0 else ""


def shown(path):
    """A path as the page shows it: the home folder written "~"."""

    if not path:
        return ""
    text, home = str(path), str(Path.home())
    if home and home != os.sep and (text == home or text.startswith(home.rstrip(os.sep) + os.sep)):
        return "~" + text[len(home.rstrip(os.sep)):]
    return text


def _inside(path, folder):
    """Whether ``path`` (resolved) lies in ``folder`` (resolved)."""

    try:
        path, folder = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    except (OSError, ValueError):
        return False
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _local_app_data():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _claude_versions():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").expanduser() / "claude" / "versions"


def installer_folder(program):
    """Where the official installer makes the program's command."""

    if platform_name() == "windows":
        return (_local_app_data() / "Programs" / "OpenAI" / "Codex" / "bin" if program == "codex"
                else Path.home() / ".local" / "bin")
    if program == "codex" and os.environ.get("CODEX_INSTALL_DIR"):
        return Path(os.environ["CODEX_INSTALL_DIR"]).expanduser()
    return Path.home() / ".local" / "bin"


def installer_target(program):
    """The command the official installer makes (Codex's replaces whatever is there)."""

    name = f"{COMMANDS[program]}.exe" if platform_name() == "windows" else COMMANDS[program]
    return installer_folder(program) / name


def known_folders(program):
    """The folders the official installers put the program in (for "Jobs cannot find it" and other copies)."""

    if platform_name() == "windows":
        return [installer_folder(program)]
    folders = [installer_folder(program)]
    if program == "codex":
        folders += [Path.home() / ".local" / "bin", codex_home() / "packages" / "standalone" / "current" / "bin"]
    return list(dict.fromkeys(folders))


def foreign_target(program):
    """Codex only: the installer's target exists and is not the installer's own link (a file it would replace)."""

    if program != "codex":
        return False
    target = installer_target(program)
    if not (target.exists() or target.is_symlink()):
        return False
    if platform_name() == "windows":
        return False  # The Windows installer keeps its own folder.
    return not _inside(target, codex_home() / "packages" / "standalone")


def launcher_kind(program, path):
    """What made the command jobs use: installer, homebrew, npm, winget, script or program."""

    real = os.path.realpath(path)
    windows = platform_name() == "windows"
    if program == "codex":
        if _inside(real, codex_home() / "packages" / "standalone") or (
                windows and _inside(path, _local_app_data() / "Programs" / "OpenAI" / "Codex" / "bin")):
            return "installer"
    elif _inside(real, _claude_versions()) or (
            windows and os.path.normcase(os.path.abspath(path))
            == os.path.normcase(str(Path.home() / ".local" / "bin" / "claude.exe"))):
        return "installer"
    if "/Caskroom/" in real or "/Cellar/" in real or real.startswith(("/opt/homebrew/", "/home/linuxbrew/")):
        return "homebrew"
    if "node_modules" in real or os.path.splitext(path)[1].lower() in {".cmd", ".ps1"}:
        return "npm"
    if windows and "\\winget\\" in real.lower():
        return "winget"
    try:
        with open(real, "rb") as stream:
            if stream.read(2) == b"#!":
                return "script"
    except OSError:
        pass
    return "program"


KIND_TEXTS = {
    ("codex", "installer"): "installed by OpenAI's installer",
    ("claude", "installer"): "installed by Anthropic's installer",
    "homebrew": "installed with Homebrew", "npm": "installed with npm", "winget": "installed with WinGet",
    "script": "your own script", "program": "installed another way",
}


def kind_text(program, kind):
    return KIND_TEXTS.get((program, kind)) or KIND_TEXTS.get(kind, "")


def _tools():
    """The programs the installers need: curl and bash (or sh) on POSIX, PowerShell on Windows."""

    if platform_name() == "windows":
        return {"powershell": powershell()}
    return {"curl": shutil.which("curl"), "bash": shutil.which("bash"), "sh": shutil.which("sh")}


def powershell():
    found = shutil.which("powershell")
    if found:
        return found
    candidate = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" \
        / "powershell.exe"
    return str(candidate) if candidate.is_file() else None


def tools_problem(program, tools=None):
    """Why the installer command cannot run here ("" when it can)."""

    tools = _tools() if tools is None else tools
    if platform_name() == "windows":
        return "" if tools.get("powershell") else "PowerShell was not found."
    if not tools.get("curl"):
        return "This computer has no curl, which the installer command needs."
    if program == "claude" and not tools.get("bash"):
        return "Claude Code's installer needs bash, which is not installed."
    if not (tools.get("bash") or tools.get("sh")):
        return "This computer has no sh, which the installer command needs."
    return ""


def command_text(argv):
    """The exact command as the dialog shows it (and the stale check compares)."""

    return subprocess.list2cmdline(argv) if platform_name() == "windows" else shlex.join(argv)


def installer_argv(program, tools=None):
    """The official installer as argv, or None when its shell is missing."""

    tools = _tools() if tools is None else tools
    line = INSTALLERS[(program, _family())]
    if platform_name() == "windows":
        shell = tools.get("powershell")
        return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", line] if shell \
            else None
    if tools.get("bash"):
        # Without pipefail a failed download feeds sh an empty script, which then ends with 0.
        return [tools["bash"], "-o", "pipefail", "-c", line]
    if program == "codex" and tools.get("sh"):
        return [tools["sh"], "-c", line]  # Its result then rests on the check that follows.
    return None


def session_options():
    """No terminal: a new session on POSIX, a new process group with no window on Windows."""

    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x8000000)}
    return {"start_new_session": True}


# --- Environments ----------------------------------------------------------------------------------------------------

def _scrubbed(environment, keep=()):
    """``environment`` without the agent sockets, any key or token the server inherited (API keys, Anthropic
    settings, Claude's sign-in tokens) and the settings that would change what an installer installs; the
    names in ``keep`` stay."""

    return {name: value for name, value in confinement.without_agents(environment).items()
            if name in keep or not (name in runtime.REDACTED_ENVIRONMENT or name in INSTALLER_VARIABLES
                                    or name.startswith(("ANTHROPIC_", "CLAUDE_CODE_OAUTH")))}


def check_environment(program):
    """The environment jobs give the program, so that "Signed in" means signed in for jobs.

    The Codex CLI: the runner's environment (no OpenAI or Codex key, quiet
    logs). Claude Code: the subscription environment of every Claude job
    (claude_cli.subscription_environment(): its config folder and its
    CLAUDE_CODE_OAUTH_TOKEN stay). Neither ever sees an API key of the server,
    so an ANTHROPIC_API_KEY or OPENAI_API_KEY can never make this page say
    "Signed in". LaTeX: the server's environment, scrubbed the same way.
    """

    if program == "codex":
        return _scrubbed(runtime.environment())
    if program == "claude":
        return claude_cli.subscription_environment(_scrubbed(runtime.environment(), keep=("CLAUDE_CODE_OAUTH_TOKEN",)))
    return _scrubbed(dict(os.environ))


def task_environment(program, action):
    """The environment of an install, an update or a sign-in.

    Installs and updates: the server's environment without any key or token,
    agent socket or installer setting that would change what is installed
    (CODEX_HOME and CODEX_INSTALL_DIR stay: they decide where). Sign-ins: the
    environment jobs use (check_environment()). Every step: no colours, no
    hyperlinks, a dumb terminal.
    """

    if action == "sign-in":
        environment = check_environment(program)
    else:
        environment = _scrubbed(dict(os.environ))
        if program == "claude":
            environment = claude_cli.subscription_environment(environment)
    return {**environment, **QUIET_OUTPUT}


# --- Cleaning --------------------------------------------------------------------------------------------------------

def clean(text, secrets_=()):
    """One line as the page may keep it: no escape codes or control characters, no keys, tokens or codes."""

    text = CONTROLS.sub("", ESCAPES.sub("", str(text)))
    ordered = sorted({secret for secret in secrets_ if secret}, key=len, reverse=True)
    for secret in ordered:
        text = text.replace(secret, "[removed]")
    text = runtime.redact(text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in ordered:
        text = text.replace(secret, "[removed]")
    return text


def _cut(text, limit):
    """``text`` at most ``limit`` characters, ending with "…" when it was shortened (never a silent cut)."""

    text = str(text)
    return text if len(text) <= limit else f"{text[:limit - 1].rstrip()}…"


def partial_line(text):
    """The line still being written (already cleaned), as the page shows it: at most LINE_CHARS characters, its
    newest ones (a progress line's news is at its end), saying when its start is not shown."""

    text = str(text)
    if len(text) <= LINE_CHARS:
        return text
    return f"(start of this line not shown, {len(text):,} characters so far) …{text[-LINE_CHARS:]}"


def _first_line(text):
    return next((line.strip() for line in str(text).splitlines() if line.strip()), "")


def _seconds_text(seconds):
    return f"{seconds:g} seconds"


def trusted_link(url, program):
    """(trusted, host) of a sign-in link: https on one of LINK_HOSTS[program]."""

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False, ""
    return parts.scheme == "https" and host in LINK_HOSTS[program] and not parts.username and not parts.password, host


# --- Checks (no model call) ------------------------------------------------------------------------------------------

def _killpg(group, signal_number):
    try:
        os.killpg(group, signal_number)
    except OSError:
        pass


def _group_alive(group):
    """Whether a process group still has a process in it (POSIX)."""

    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _outside_groups(root):
    """The process groups of ``root``'s descendants other than its own (a child that started a session of its
    own, with everything in it), listed while ``root`` runs: once it has ended, its children are no longer known
    as its own. [] when ps cannot run."""

    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
                                 check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children, groups = {}, {}
    for line in listing.splitlines():
        try:
            pid, parent, group = (int(value) for value in line.split())
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
        groups[pid] = group
    found, pending = set(), [root]
    while pending:
        for pid in children.get(pending.pop(), []):
            if pid != root and pid not in found:
                found.add(pid)
                pending.append(pid)
    own = groups.get(root, root)
    # Never the server's own group: a step runs in a session of its own, so no descendant can join it.
    return sorted({groups[pid] for pid in found if pid in groups and groups[pid] not in {own, os.getpgrp()}})


def _end_group(process, grace, *, tree=True):
    """Stop a step's or a check's process and everything it started.

    POSIX: its process group and, with ``tree``, every process group its
    descendants started (listed first, while it runs) get TERM, then
    ``grace`` seconds for a clean exit, then KILL (what still runs of its
    tree is frozen and killed first). Windows: taskkill /T while it runs.
    """

    if process is None:
        return
    if os.name == "nt":
        runtime._stop_command_tree(process)
        return
    groups = [process.pid, *(_outside_groups(process.pid) if tree and process.poll() is None else [])]
    if process.poll() is None and grace:
        for group in groups:
            _killpg(group, signal.SIGTERM)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    runtime._stop_command_tree(process)  # Returns at once when the process has ended.
    for group in groups:
        _killpg(group, signal.SIGKILL)


def _end_leftovers(group, grace=None):
    """End what an install's or update's process left in its process group once it has exited (POSIX): TERM,
    then KILL after LEFTOVER_GRACE_SECONDS. Nothing of it may keep running, or keep writing to the step's
    output, after the step has ended."""

    if os.name == "nt" or not _group_alive(group):
        return
    _killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + (LEFTOVER_GRACE_SECONDS if grace is None else grace)
    while time.monotonic() < deadline and _group_alive(group):
        time.sleep(0.05)
    _killpg(group, signal.SIGKILL)


def run_check(argv, environment, timeout=None):
    """Run one status command: (exit code, its output as text). CheckTimeout when it does not answer in time.

    No terminal, stdin closed, stdout and stderr together, a private working
    folder; on a timeout its whole process tree is stopped. The text is raw:
    callers clean or parse it before anything is kept.
    """

    timeout = CHECK_SECONDS if timeout is None else timeout
    folder = tempfile.mkdtemp(prefix="tcs-setup-check-")
    try:
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, env=environment, cwd=folder, **session_options())
        except OSError as exc:
            return None, exc.strerror or type(exc).__name__
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _end_group(process, 0)
            try:
                process.communicate(timeout=OUTPUT_GRACE_SECONDS)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            raise CheckTimeout() from None
        return process.returncode, output.decode("utf-8", "replace")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


_GATES = {}
_GATES_LOCK = threading.Lock()


def help_lists(path, args, patterns, environment):
    """Whether ``path args`` (a --help) lists every pattern; cached per binary (its real path, size and time).

    A help that did not answer is not cached (CheckTimeout passes through).
    """

    try:
        real = os.path.realpath(path)
        info = os.stat(real)
        key = (real, tuple(args), tuple(patterns), info.st_size, info.st_mtime_ns)
    except OSError:
        key = None
    with _GATES_LOCK:
        if key is not None and key in _GATES:
            return _GATES[key]
    code, text = run_check([path, *args], environment)
    answer = code is not None and all(re.search(pattern, text) for pattern in patterns)
    if key is not None:
        with _GATES_LOCK:
            _GATES[key] = answer
    return answer


def _version(path, environment, label):
    """(version, problem) of ``path --version``: its first line, or why it does not run."""

    try:
        code, text = run_check([path, "--version"], environment)
    except CheckTimeout:
        return "", f"{label} --version did not answer within {_seconds_text(CHECK_SECONDS)}."
    first = _cut(clean(_first_line(text)), 200)
    if code is None:
        return "", f"{label} --version could not start: {first}."
    if code != 0:
        return "", f"{label} --version ended with code {code}" + (f": {first}" if first else ".")
    if not first:
        return "", f"{label} --version printed nothing."
    return first, ""


def parse_codex_status(code, text):
    """`codex login status` as {state, text, method}; the line of an API key (a masked key) is never kept."""

    first = _first_line(text)
    if first == "Logged in using ChatGPT":
        return {"state": "signed-in", "text": "with ChatGPT", "method": "ChatGPT"}
    if first.startswith("Logged in using an API key"):
        return {"state": "signed-in", "text": "with an API key", "method": "an API key"}
    if first.startswith("Logged in using "):
        method = first[len("Logged in using "):].split(" - ", 1)[0].split("(", 1)[0].strip().rstrip(".")
        method = _cut(clean(method), 60) or "another method"
        return {"state": "signed-in", "text": f"with {method}", "method": method}
    if first.startswith("Not logged in"):
        return {"state": "signed-out", "text": "", "method": ""}
    if first.startswith("Error checking login status:"):
        # The page shows this after "Could not check": the reason alone, as a sentence.
        reason = _cut(clean(first[len("Error checking login status:"):].strip()), 200)
        return {"state": "unknown", "text": f"{reason[:1].upper()}{reason[1:]}" if reason else "", "method": ""}
    line = _cut(clean(first), 200)
    return {"state": "unknown", "method": "",
            "text": f"codex login status ended with code {code}" + (f": {line}" if line else ".")}


def _json_object(text):
    """The JSON object in ``text`` (a stray line around it is ignored), or None."""

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_claude_status(code, text):
    """`claude auth status --json` as {state, text, method, email, plan, token}; only five fields are read."""

    data = _json_object(text)
    if data is None or not isinstance(data.get("loggedIn"), bool):
        line = _cut(clean(_first_line(text)), 200)
        return {"state": "unknown", "method": "",
                "text": "Claude Code's sign-in status could not be read"
                        + (f" (claude auth status ended with code {code}: {line})." if line else ".")}
    if not data["loggedIn"]:
        return {"state": "signed-out", "text": "", "method": ""}
    method = data.get("authMethod")
    email = data.get("email") if isinstance(data.get("email"), str) else ""
    email = _cut(clean(email), 200)
    if method == "claude.ai":
        plan = data.get("subscriptionType") if isinstance(data.get("subscriptionType"), str) else ""
        plan = _cut(clean(plan), 40)
        plan = f"Claude {plan[:1].upper()}{plan[1:]}" if plan else "Claude subscription"
        detail = f"{email} · {plan}" if email else plan
        return {"state": "signed-in", "text": f"as {detail}", "method": detail, "email": email, "plan": plan}
    if method in {"api_key", "api_key_helper"}:
        return {"state": "signed-in", "text": "with an Anthropic Console account", "method": "Anthropic Console"}
    if method == "oauth_token":
        return {"state": "signed-in", "text": "with the token in CLAUDE_CODE_OAUTH_TOKEN",
                "method": "CLAUDE_CODE_OAUTH_TOKEN", "token": True}
    if method == "third_party":
        provider = data.get("apiProvider") if isinstance(data.get("apiProvider"), str) else ""
        provider = _cut(clean(provider), 60) or "another provider"
        return {"state": "signed-in", "text": f"through {provider}", "method": provider}
    other = _cut(clean(str(method or "another method")), 60)
    return {"state": "signed-in", "text": f"with {other}", "method": other}


def quick_facts(program):
    """What a check learns of ``program`` without starting a process: {path, kind, targetForeign}.

    ``path`` is the command jobs use, else a copy in one of its installer
    folders; ``kind`` what made it; ``targetForeign`` whether OpenAI's
    installer would replace a file that is not its own. A step compares them
    with those of the last check (moved()), so a script that appeared where
    the installer writes, a link replaced by a script, or a copy installed or
    removed since is checked again before anything runs.
    """

    path, _ = locate(program)
    path = path or offpath_copy(program) or ""
    return {"path": path, "kind": launcher_kind(program, path) if path else "",
            "targetForeign": foreign_target(program)}


def moved(program, facts):
    """Whether ``program`` changed since ``facts`` were checked, as far as quick_facts() can tell.

    Facts without that record (a check that failed) or a lookup that fails
    now count as unchanged: the plan then rests on the last check, as it did.
    """

    seen = facts.get("seen")
    if not isinstance(seen, dict):
        return False
    try:
        return quick_facts(program) != seen
    except Exception:  # A lookup that fails says nothing new.
        return False


def locate(program):
    """(the command jobs use or None, the remembered folder it came from or "")."""

    try:
        path = runtime.codex() if program == "codex" else claude_cli.executable(runtime)
    except runtime.Error:
        return None, ""
    on_path = shutil.which(COMMANDS[program])
    return path, ("" if on_path else os.path.dirname(path))


def offpath_copy(program):
    """The program in one of its installer folders, when the lookups of jobs do not find it."""

    for folder in known_folders(program):
        found = shutil.which(COMMANDS[program], path=str(folder))
        if found:
            return found
    return None


def vendor_copies(program):
    """The vendors' other places for a copy: the Codex desktop app's own Codex on macOS
    (retrieval_runtime.CODEX_APP_BINARY, which tests replace), Claude Code's earlier local install elsewhere."""

    if program == "codex" and platform_name() == "macos":
        return [str(retrieval_runtime.CODEX_APP_BINARY)]
    if program == "claude" and platform_name() != "windows":
        return [str(Path.home() / ".claude" / "local" / "claude")]
    return []


def other_copies(program, used):
    """Every other copy on PATH, in the installer folders and in the vendors' other places (by real path)."""

    name = COMMANDS[program]
    folders = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    folders += [str(folder) for folder in known_folders(program)]
    candidates = [shutil.which(name, path=folder) for folder in folders] + vendor_copies(program)
    used_real = os.path.realpath(used) if used else None
    seen, copies = set(), []
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        real = os.path.realpath(candidate)
        if real == used_real or real in seen:
            continue
        seen.add(real)
        copies.append(candidate)
    return copies


def check_program(program, previous=None):
    """What one program is like now: where jobs find it, what made it, its version and its sign-in."""

    # What the facts rest on, seen with no process: a step looks again before it runs (moved()).
    seen = quick_facts(program)
    facts = {"program": program, "name": NAMES[program], "state": "missing", "path": "", "kind": "",
             "remembered": "", "offPath": "", "otherCopies": [], "version": "", "problem": "",
             "signIn": {"state": "not-checked", "text": "", "method": ""}, "gates": {}, "tools": _tools(),
             "targetForeign": seen["targetForeign"], "seen": seen,
             # Claude jobs use a sign-in token TCS Prover was started with: signing in here would change nothing.
             "envToken": program == "claude" and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))}
    environment = check_environment(program)
    path, remembered = locate(program)
    if not path:
        found = seen["path"] or None
        if found:
            facts.update(state="offpath", offPath=os.path.dirname(found), path=found, kind=seen["kind"])
        facts["otherCopies"] = other_copies(program, found)
        return facts
    facts.update(path=path, remembered=remembered, kind=seen["kind"] if seen["path"] == path
                 else launcher_kind(program, path), otherCopies=other_copies(program, path))
    version, problem = _version(path, environment, COMMANDS[program])
    if problem:
        facts.update(state="broken", problem=problem)
        return facts
    facts.update(state="works", version=version)
    try:
        if program == "codex":
            if not help_lists(path, ["login", "--help"], [SUBCOMMAND % "status"], environment):
                facts["gates"]["status"] = False
                facts["signIn"] = {"state": "unknown", "text": "This Codex CLI cannot report its sign-in.", "method": ""}
            else:
                facts["gates"]["status"] = True
                code, text = run_check([path, "login", "status"], environment)
                facts["signIn"] = parse_codex_status(code, text)
                del text  # The status line of an API key holds a masked key: it is never kept.
        else:
            # None until `auth --help` answers: no `auth status` or `auth login` runs before it lists them (S6).
            facts["gates"]["status"] = None
            if not help_lists(path, ["auth", "--help"], [SUBCOMMAND % "status", SUBCOMMAND % "login"], environment):
                facts["gates"]["status"] = False
                facts["signIn"] = {"state": "unknown", "text": "This Claude Code cannot report its sign-in.",
                                   "method": ""}
            else:
                facts["gates"]["status"] = True
                code, text = run_check([path, "auth", "status", "--json"], environment)
                facts["signIn"] = parse_claude_status(code, text)
                del text
    except CheckTimeout:
        facts["signIn"] = {"state": "unknown", "method": "",
                           "text": f"The sign-in check did not answer within {_seconds_text(CHECK_SECONDS)}."}
    if program == "claude" and facts["kind"] == "installer":
        try:
            facts["gates"]["update"] = help_lists(path, ["--help"], [UPDATE_SUBCOMMAND], environment)
        except CheckTimeout:
            facts["gates"]["update"] = None
    return facts


def check_latex():
    """Whether latexmk and pdflatex are found (on PATH or in a standard TeX folder) and run, with their versions."""

    environment = check_environment("latex")
    tools = []
    for name, flag in (("latexmk", "-v"), ("pdflatex", "--version")):
        path = runtime._find_tool(name)
        tool = {"name": name, "path": path or "", "shownPath": shown(path), "onPath": bool(shutil.which(name)),
                "version": "", "problem": ""}
        if path:
            try:
                code, text = run_check([path, flag], environment)
            except CheckTimeout:
                tool["problem"] = f"{name} {flag} did not answer within {_seconds_text(CHECK_SECONDS)}."
            else:
                lines = [clean(line).strip() for line in text.splitlines() if line.strip()]
                if code != 0 or not lines:
                    tool["problem"] = (f"{name} {flag} ended with code {code}" if code is not None
                                       else f"{name} {flag} could not start") + (f": {_cut(lines[0], 200)}" if lines else ".")
                elif name == "latexmk":
                    line = next((line for line in lines if "Version" in line), lines[0])
                    match = re.search(r"Version\s+(\S+)", line)
                    tool["version"] = f"latexmk {match.group(1)}" if match else _cut(line, 200)
                else:
                    tool["version"] = _cut(lines[0], 200)
        tools.append(tool)
    missing = [tool["name"] for tool in tools if not tool["path"]]
    broken = [tool for tool in tools if tool["path"] and tool["problem"]]
    state = "missing" if missing else "broken" if broken else "works"
    problem = (" and ".join(missing) + (" are missing" if len(missing) > 1 else " is missing")) if missing \
        else broken[0]["problem"] if broken else ""
    return {"program": "latex", "name": NAMES["latex"], "state": state, "tools": tools, "problem": problem}


def check(programs=PROGRAMS, previous=None):
    """Check ``programs`` at the same time (one thread each): {program: facts}."""

    results = {}

    def one(program):
        try:
            results[program] = check_latex() if program == "latex" else check_program(program)
        except Exception as exc:  # A check never stops the page: it says it could not check.
            results[program] = {"program": program, "name": NAMES[program], "state": "unknown",
                                "problem": f"TCS Prover could not check {THE[program]} ({type(exc).__name__}). "
                                           "Click Check again.",
                                "signIn": {"state": "unknown", "text": "", "method": ""},
                                # LaTeX's tools are a list (one row per tool), the others' the installer's tools.
                                "tools": [] if program == "latex" else {}, "otherCopies": [], "gates": {}}

    threads = [threading.Thread(target=one, args=(program,), daemon=True, name=f"setup-check-{program}")
               for program in programs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


# --- Plans: what each action runs and says ------------------------------------------------------------------------

def busy_reason(program, titles):
    """Why a step that would replace the program waits for the jobs that use it ("" when none)."""

    if not titles:
        return ""
    count = len(titles)
    quoted = [f"“{title}”" for title in titles[:3]]
    more = count - len(quoted)
    if more:
        listed = ", ".join(quoted) + f" and {more} more"
    elif count == 1:
        listed = quoted[0]
    else:
        listed = ", ".join(quoted[:-1]) + f" and {quoted[-1]}"
    them = "it" if count == 1 else "them"
    verb = "uses" if count == 1 else "use"
    return (f"Unavailable while {count} {'job' if count == 1 else 'jobs'} {verb} {THE[program]}: {listed}. "
            f"Pause or stop {them}, or let {them} finish.")


def running_reason(program):
    return f"Another Setup step for {THE[program]} is running."


def _profile():
    """The shell profile OpenAI's installer adds the Codex folder to (install.sh pick_profile)."""

    shell = os.path.basename(os.environ.get("SHELL", ""))
    mac = platform_name() == "macos"
    if shell == "zsh":
        return "~/.zprofile" if mac else "~/.zshrc"
    if shell == "bash":
        return "~/.bash_profile" if mac else "~/.bashrc"
    return "~/.profile"


def _install_dialog(program, command, copies):
    """The Install confirmation: title, lede, the exact command, what it does and the sign-in checkbox."""

    windows = platform_name() == "windows"
    if program == "codex":
        what = ["Downloads the Codex CLI from chatgpt.com and releases.openai.com (or github.com) and checks its "
                "checksum."]
        if windows:
            what += ["Installs it in %USERPROFILE%\\.codex\\packages\\standalone and makes the command in "
                     "%LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin.", "Adds that folder to your user PATH."]
        else:
            home = "$CODEX_HOME" if os.environ.get("CODEX_HOME") else "~/.codex"
            what += [f"Installs it in {home}/packages/standalone and makes the command {shown(installer_target('codex'))}.",
                     f"Adds {shown(installer_folder('codex'))} to your PATH in {_profile()} if it is not there yet."]
        what.append("Asks no questions and needs no administrator rights.")
        what += [f"Your other Codex at {shown(path)} stays installed; Setup then shows which one jobs use."
                 for path in copies]
        what.append("Jobs find it at once; open a new terminal to use codex yourself.")
        return {"title": "Install the Codex CLI", "lede": "TCS Prover will run OpenAI's official installer:",
                "command": command, "what": what, "warning": "",
                "checkbox": {"label": "Then sign in with ChatGPT in this browser", "checked": True,
                             "option": "browser"}}
    what = ["Downloads Claude Code from claude.ai and downloads.claude.ai and checks its checksum.",
            "Installs it as %USERPROFILE%\\.local\\bin\\claude.exe." if windows
            else "Installs it in ~/.local/share/claude/versions and makes the command ~/.local/bin/claude.",
            "Does not change your shell settings: if ~/.local/bin is not on your PATH, the output says how to add it. "
            "Jobs find Claude Code either way.",
            "Claude Code then keeps itself up to date in the background.",
            "Needs no administrator rights."]
    what += [f"Your other Claude Code at {shown(path)} stays installed; Setup then shows which one jobs use."
             for path in copies]
    what.append("It shows no progress while it downloads; this can take a few minutes.")
    return {"title": "Install Claude Code", "lede": "TCS Prover will run Anthropic's official installer:",
            "command": command, "what": what, "warning": "",
            "checkbox": {"label": "Then sign in with your Claude account", "checked": True, "option": "subscription"}}


def _update_dialog(program, command, version):
    windows = platform_name() == "windows"
    if program == "codex":
        what = [f"Replaces {version or 'the installed version'} with the newest version; your login and settings stay.",
                "Downloads the Codex CLI from chatgpt.com and releases.openai.com (or github.com) and checks its "
                "checksum.",
                "Jobs started afterwards use the new version."]
        if windows:
            what.append("Close Codex windows you started yourself first: Windows cannot replace a program that is "
                        "running.")
        return {"title": "Update the Codex CLI",
                "lede": "TCS Prover will run OpenAI's official installer again; it installs the newest version:",
                "command": command, "what": what, "warning": ""}
    what = ["Installs the newest version on your Claude Code update channel and switches ~/.local/bin/claude to it.",
            "Your login and settings stay.", "Jobs started afterwards use the new version."]
    if windows:
        what.append("Close Claude Code windows you started yourself first: Windows cannot replace a program that is "
                    "running.")
    return {"title": "Update Claude Code", "lede": "TCS Prover will run Claude Code's own updater:",
            "command": command, "what": what, "warning": ""}


def sign_in_argv(program, path, option):
    if program == "codex":
        return [path, "app-server"]
    return [path, "auth", "login", *(["--console"] if option == "console" else [])]


def _sign_in_dialog(program, path, sign_in):
    state = sign_in.get("state")
    # A login that could not be checked may be there (a locked keyring, a status that did not answer).
    warning = (f"This replaces the current login ({sign_in.get('method') or 'saved'})." if state == "signed-in"
               else "This may replace the current login." if state == "unknown" else "")
    if program == "codex":
        home = os.environ.get("CODEX_HOME") or "~/.codex"
        command = command_text(sign_in_argv("codex", path, "browser"))
        return {"title": "Sign in to the Codex CLI",
                "lede": "TCS Prover will start the Codex CLI and ask it for a ChatGPT sign-in:",
                "legend": "How to sign in",
                "options": [{"id": "browser", "label": "In this browser", "command": command,
                             "help": "Opens the ChatGPT sign-in page here. Use this when TCS Prover runs on this "
                                     "computer."},
                            {"id": "code", "label": "With a one-time code", "command": command,
                             "help": "For TCS Prover on another computer, for example over SSH: you enter a code on "
                                     "any device. Turn on device code sign-in in ChatGPT's security settings first."}],
                "what": ["You sign in on ChatGPT's own page; you type nothing into TCS Prover.",
                         f"Codex saves the login in {shown(home) if os.path.isabs(home) else home}.",
                         "Jobs waiting for a Codex login continue by themselves."],
                "warning": warning, "button": "Start sign-in"}
    return {"title": "Sign in to Claude Code", "lede": "TCS Prover will run Claude Code's sign-in:",
            "legend": "Account",
            "options": [{"id": "subscription", "label": "Claude subscription (Pro, Max, Team or Enterprise)",
                         "command": command_text(sign_in_argv("claude", path, "subscription")), "help": ""},
                        {"id": "console", "label": "Anthropic Console account (billed per use)",
                         "command": command_text(sign_in_argv("claude", path, "console")), "help": ""}],
            "what": ["A sign-in tab may open by itself; this page also shows the link.",
                     "If the sign-in page shows a code, paste it into the box that appears here. TCS Prover passes "
                     "it only to Claude Code.",
                     "Claude Code saves the login (on macOS in your Keychain).",
                     "Jobs waiting for a Claude login continue by themselves."],
            "warning": warning, "button": "Start sign-in"}


def sign_in_label(program, sign_in):
    state, method = sign_in.get("state"), sign_in.get("method")
    if state != "signed-in":
        return "Sign in"
    if program == "codex" and method != "ChatGPT":
        return "Sign in with ChatGPT"
    return "Sign in again"


def plan_actions(program, facts, *, busy=(), running=None, admin="", platform=None):
    """Every action of one program as the page offers it: {install, update, signIn, useFolder}.

    Each has ``shown`` (a button appears), ``available`` (it can run now),
    ``reason`` (why not) and the dialog with the exact command, or None.
    ``running`` is the program's step that is running, if any; ``busy`` the
    titles of this server's jobs that run a model through the program now.
    """

    platform = platform or platform_name()
    state, kind = facts.get("state"), facts.get("kind")
    tools = facts.get("tools") or {}
    path = facts.get("path") or ""
    actions = {name: {"shown": False, "available": False, "reason": "", "dialog": None}
               for name in ("install", "update", "signIn")}
    actions["useFolder"] = {"shown": False, "available": False, "reason": "", "folder": ""}
    supported = platform in {"macos", "linux", "windows"}
    common = PLATFORM_TEXT if not supported else running_reason(program) if running else ""

    def offer(name, dialog, *, guarded=False, extra=""):
        reason = common or extra or (busy_reason(program, busy) if guarded else "")
        actions[name].update(shown=not admin, available=not admin and not reason, reason=reason, dialog=dialog)

    # Install: a missing program, or a broken one the official installer made (a repair).
    if state in {"missing", "broken"} and not facts.get("targetForeign") and (
            state == "missing" or kind == "installer"):
        argv = installer_argv(program, tools) if supported else None
        command = command_text(argv) if argv else ""
        offer("install", _install_dialog(program, command, facts.get("otherCopies") or []) if argv else None,
              guarded=state == "broken", extra=tools_problem(program, tools) if supported else "")
    # Update: only a copy the official installer made (Codex: and not a target it would replace).
    if state == "works" and kind == "installer" and not facts.get("targetForeign"):
        if program == "codex":
            argv = installer_argv(program, tools) if supported else None
            extra = tools_problem(program, tools) if supported else ""
        else:
            argv = [path, "update"]
            gate = (facts.get("gates") or {}).get("update")
            extra = "" if gate else ("This Claude Code has no update command." if gate is False
                                     else "TCS Prover could not check Claude Code's update command. Click Check again.")
        command = command_text(argv) if argv else ""
        offer("update", _update_dialog(program, command, facts.get("version")) if argv else None, guarded=True,
              extra=extra)
    # Sign in: a working program, except Claude Code signed in with the token from the environment. Claude Code's
    # `auth login` runs only once `auth --help` listed it (S6): when that help did not answer, the button waits for
    # Check again. Only a first sign-in (signed out) never waits for jobs; one that may replace a login (signed in,
    # or a sign-in that could not be checked) waits for the jobs that use the program.
    sign_in = facts.get("signIn") or {}
    gate = (facts.get("gates") or {}).get("status")
    if state == "works" and not sign_in.get("token") and not facts.get("envToken") and not (
            program == "claude" and gate is False):
        dialog = _sign_in_dialog(program, path, sign_in)
        extra = SIGN_IN_GATE_TEXT if program == "claude" and gate is not True else ""
        offer("signIn", dialog, guarded=sign_in.get("state") != "signed-out", extra=extra)
        actions["signIn"]["label"] = sign_in_label(program, sign_in)
    if state == "offpath":
        folder = facts.get("offPath") or ""
        actions["useFolder"].update(shown=not admin, available=not admin and bool(folder), folder=folder)
    return actions


def copy_commands(program, facts, platform=None):
    """The commands to copy for one program, each {group, label, command}."""

    platform = platform or platform_name()
    family = "windows" if platform == "windows" else "posix"
    state, kind = facts.get("state"), facts.get("kind")
    rows = []
    if state in {"missing", "broken", "offpath", "unknown"}:
        rows += [{"group": "install", "label": label, "command": command}
                 for label, command in TERMINAL_COMMANDS[(program, "install", family)]]
        if program == "claude" and platform == "linux" and os.path.exists("/etc/alpine-release"):
            rows.append({"group": "install", "label": ALPINE_COMMAND[0], "command": ALPINE_COMMAND[1]})
    if state == "works":
        if kind == "installer":
            command = UPDATE_COMMANDS.get((program, "installer", family)) or UPDATE_COMMANDS[(program, "installer")]
            rows.append({"group": "update", "label": "In a terminal", "command": command})
        elif kind == "homebrew":
            latest = program == "claude" and "claude-code@latest" in os.path.realpath(facts.get("path") or "")
            rows.append({"group": "update", "label": "Homebrew",
                         "command": UPDATE_COMMANDS[(program, "homebrew-latest" if latest else "homebrew")]})
        elif (program, kind) in UPDATE_COMMANDS:
            rows.append({"group": "update", "label": {"npm": "npm", "winget": "WinGet"}.get(kind, kind),
                         "command": UPDATE_COMMANDS[(program, kind)]})
        else:
            label, command = TERMINAL_COMMANDS[(program, "install", family)][0]
            rows.append({"group": "update", "label": label.replace("installer", "installer (installs the newest "
                                                                                 "version)", 1), "command": command})
        rows += [{"group": "sign-in", "label": label, "command": command}
                 for label, command in TERMINAL_COMMANDS[(program, "sign-in", "any")]]
    if program == "claude" and platform == "linux" and state == "works" and not claude_cli.sandbox_supported():
        rows += [{"group": "sandbox", "label": label, "command": command} for label, command in SANDBOX_COMMANDS]
    return rows


def program_notes(program, facts, platform=None):
    """The notes under a program (custom launchers, other copies, the remembered folder, ...)."""

    platform = platform or platform_name()
    notes = []
    state, kind, path = facts.get("state"), facts.get("kind"), facts.get("path") or ""
    target = installer_target(program)
    if program == "codex" and facts.get("targetForeign"):
        try:
            with open(os.path.realpath(target), "rb") as stream:
                script = stream.read(2) == b"#!"
        except OSError:
            script = False
        what, thing = ("your own script", "script") if script else ("a file OpenAI's installer did not make", "file")
        notes.append(f"{shown(target)} is {what}, not the link OpenAI's installer makes there. The installer would "
                     f"replace it, so TCS Prover does not run installs or updates here. The commands below run it "
                     f"yourself; save your {thing} first.")
    elif state in {"works", "broken"} and kind == "script":
        if program == "claude":
            notes.append(f"{shown(path)} is your own script, so it decides which Claude Code runs. Installs and "
                         "updates add versions your script does not start, so TCS Prover does not run them here.")
        else:
            notes.append(f"{shown(path)} is your own script; TCS Prover does not update it.")
    elif state == "works" and kind in {"homebrew", "npm", "winget"}:
        notes.append(f"This copy was {KIND_TEXTS[kind]}. Update it the same way:")
    elif state == "works" and kind == "program":
        notes.append("This copy was installed another way; TCS Prover does not update it.")
    copies = facts.get("otherCopies") or []
    if copies and state in {"works", "broken"}:
        notes.append(f"Jobs use the first one found: {shown(path)}. Also on this computer: "
                     f"{', '.join(shown(copy) for copy in copies)}.")
    if facts.get("remembered") and state in {"works", "broken"}:
        notes.append(f"Jobs find it in {shown(facts['remembered'])}, which TCS Prover remembers because it is not on "
                     "the path TCS Prover started with.")
    if state == "offpath":
        notes.append(f"Click Use it for jobs and TCS Prover will remember this folder. To use it in a terminal, add "
                     f"{shown(facts.get('offPath'))} to your PATH.")
    sign_in = facts.get("signIn") or {}
    if program == "codex" and sign_in.get("state") == "signed-in" and sign_in.get("method") == "ChatGPT":
        try:
            login = retrieval_runtime._login_source()
            keyring = not login.exists()
        except (OSError, RuntimeError):
            keyring = False
        if keyring:
            home = shown(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
            notes.append(f"Your Codex login is kept in the system keyring, not in {shown(login)}. Jobs with a web "
                         "search cutoff read that file, so they cannot sign in. Add cli_auth_credentials_store = "
                         f"\"file\" to {home}/config.toml, then sign in again.")
    if program == "claude" and platform == "linux" and state == "works" and not claude_cli.sandbox_supported():
        notes.append("Claude Code's shell sandbox needs bubblewrap and socat, which are not installed. Without them, "
                     "Claude authors work with file tools only. Install them with your package manager "
                     "(administrator rights needed):")
    if program == "claude" and (sign_in.get("token") or facts.get("envToken")):
        notes.append("Jobs use the sign-in token in CLAUDE_CODE_OAUTH_TOKEN, which TCS Prover was started with. "
                     "Signing in here would not change that, so it is not offered.")
    if state in {"missing", "broken"} or (state == "works" and kind == "installer" and program == "codex"):
        problem = tools_problem(program, facts.get("tools") or {}) if platform in {"macos", "linux", "windows"} else ""
        if problem:
            notes.append(problem)
    if program == "claude" and state == "works" and kind == "installer" and (facts.get("gates") or {}).get("update") \
            is False:
        notes.append("This Claude Code has no update command.")
    return notes


# --- Steps -----------------------------------------------------------------------------------------------------------

def _now_text(value=None):
    value = time.time() if value is None else value
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def failure_text(program, code, lines, platform=None):
    """Why an install or update ended with exit code ``code``, in plain words, with the code."""

    platform = platform or platform_name()
    output = "\n".join(lines)
    for rule, text in FAILURES:
        if isinstance(rule, int) and rule == code:
            return f"{text} Exit code {code}."
        if isinstance(rule, tuple) and rule == (platform, code):
            return f"{text} Exit code {code}."
        if isinstance(rule, re.Pattern) and rule.search(output):
            return f"{text} Exit code {code}."
    return f"The command ended with exit code {code}. See the output above."


class Task:
    """One Setup step: its process, its cleaned output (in memory only) and its result.

    Every field is read and written under ``lock``. ``lines`` keeps the newest
    KEPT_LINES lines; ``total`` counts every line made, so the page can ask for
    the lines after the ones it has (``after``) and say how many were dropped.
    """

    # Stop also ends the process groups the step's descendants started (_end_group's ``tree``).
    TREE = True
    # What the step's process leaves in its process group ends with it (_end_leftovers).
    LEFTOVERS = False

    def __init__(self, owner, program, action, option, argv, environment, command, title, *, after=None):
        self.owner = owner
        self.id = f"s-{secrets.token_hex(6)}"
        self.program, self.action, self.option = program, action, option
        self.argv, self.environment, self.command, self.title = argv, environment, command, title
        self.after = dict(after or {})
        self.lock = threading.RLock()
        self.state = "running"
        self.started_at, self.started = time.time(), time.monotonic()
        self.ended_at, self.ended = None, None
        self.lines, self.total = deque(maxlen=KEPT_LINES), 0
        self._line, self._line_since, self._carry = "", None, ""
        self.secrets = set()
        self.link, self.code = None, None
        self.input = {"sent": False, "message": ""}
        self.result, self.next_task, self.exit_code = None, "", None
        self.process, self.folder = None, None
        self.stopping = self.stop_requested = self.replaced = False
        # The step's process has ended; its result is being checked (a Stop then undoes nothing it did).
        self.exited = False
        self.open_commands = ""
        self.done = threading.Event()
        self.thread = None

    # Output ----------------------------------------------------------------------------------------------------------
    def log(self, text):
        """Add one finished line (already in plain words)."""

        with self.lock:
            self._keep_locked(text)

    def feed(self, text):
        """Add decoded output: "\\r\\n" ends a line, a lone "\\r" starts the current line again (progress bars)."""

        with self.lock:
            text = self._carry + text
            self._carry = ""
            if text.endswith("\r"):
                self._carry, text = "\r", text[:-1]
            segments = text.replace("\r\n", "\n").split("\n")
            for position, segment in enumerate(segments):
                if "\r" in segment:
                    self._line = segment.rsplit("\r", 1)[1]
                else:
                    self._line += segment
                if position < len(segments) - 1 or len(self._line) > 4 * LINE_CHARS:
                    self._finish_line_locked()
            if self._line and self._line_since is None:
                self._line_since = time.monotonic()

    def flush(self):
        """End the output: the line without a line break becomes a line."""

        with self.lock:
            self._carry = ""
            if self._line:
                self._finish_line_locked()

    def _finish_line_locked(self):
        raw, self._line, self._line_since = self._line, "", None
        raw = self.raw_line(raw)
        if raw is not None:
            self._keep_locked(clean(raw, self.secrets))

    def _keep_locked(self, line):
        if len(line) > LINE_CHARS:
            line = f"{line[:LINE_CHARS]} … (line cut, {len(line):,} characters)"
        self.lines.append(line)
        self.total += 1

    def raw_line(self, raw):
        """A step's look at each line before it is cleaned (the Claude sign-in takes its link here)."""

        return raw

    def read_stream(self, stream):
        """Read raw chunks of ``stream`` into the output until it ends."""

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    break
                self.feed(decoder.decode(chunk))
        except (OSError, ValueError):
            pass
        tail = decoder.decode(b"", final=True)
        if tail:
            self.feed(tail)

    # The page's view ------------------------------------------------------------------------------------------------
    def summary(self):
        with self.lock:
            return {"id": self.id, "action": self.action, "state": self.state, "title": self.title,
                    "stopping": self.stopping, "startedAt": _now_text(self.started_at)}

    def snapshot(self, after=0):
        with self.lock:
            first = self.total - len(self.lines)
            start = max(int(after or 0), first)
            lines = list(self.lines)[start - first:] if start < self.total else []
            partial = ""
            if self._line and self._line_since is not None and time.monotonic() - self._line_since >= PARTIAL_SECONDS:
                partial = partial_line(clean(self._line, self.secrets))
            elapsed = (self.ended if self.ended is not None else time.monotonic()) - self.started
            return {"id": self.id, "program": self.program, "action": self.action, "option": self.option,
                    "title": self.title, "state": self.state, "stopping": self.stopping,
                    "startedAt": _now_text(self.started_at),
                    "endedAt": _now_text(self.ended_at) if self.ended_at else "",
                    "elapsedSeconds": int(elapsed), "command": self.command,
                    "lines": lines, "first": first, "next": self.total, "dropped": first, "partial": partial,
                    "link": dict(self.link) if self.link else None, "code": dict(self.code) if self.code else None,
                    "input": dict(self.input), "result": dict(self.result) if self.result else None,
                    "nextTask": self.next_task, "openCommands": self.open_commands,
                    # The process has ended and its result is being checked (the page says so).
                    "checking": self.exited and self.state in {"running", "waiting"},
                    # An install confirmed with "Then sign in": the page says the sign-in comes next.
                    "signInAfter": bool(self.after.get("signInAfter")), "keptLines": KEPT_LINES}

    # Life -----------------------------------------------------------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"setup-{self.program}-{self.action}")
        self.thread.start()

    def _run(self):
        try:
            self.run()
        except Stopped:
            self.finish("stopped", STOPPED_SIGN_IN if self.action == "sign-in" else self.stop_text())
        except SignInFailed as exc:
            if self.stop_requested:
                self.finish("stopped", STOPPED_SIGN_IN)
            else:
                with self.lock:
                    self.open_commands = "sign-in" if exc.commands else ""
                self.finish("failed", str(exc))
        except BaseException as exc:  # Never a traceback with output in it (S13).
            if self.stop_requested:
                self.finish("stopped", self.stop_text())
            else:
                self.finish("failed", f"Setup could not run this step: {type(exc).__name__}.")
        finally:
            try:
                if self.process is not None and self.process.poll() is None:
                    _end_group(self.process, 0, tree=self.TREE)
            except Exception:
                pass
            if self.folder:
                shutil.rmtree(self.folder, ignore_errors=True)
            with self.lock:
                if self.result is None:
                    self._settle_locked("failed", "Setup could not run this step.")
            self.done.set()

    def stop_text(self):
        verb = "Install" if self.action == "install" else "Update"
        return f"Stopped. Run {verb} again to finish or repair it."

    def finish(self, state, text, exit_code=None):
        with self.lock:
            if self.result is None:
                self._settle_locked(state, text, exit_code)

    def _settle_locked(self, state, text, exit_code=None):
        tone = {"succeeded": "done", "failed": "failed", "stopped": "stopped"}[state]
        self.state, self.stopping = state, False
        self.ended_at, self.ended = time.time(), time.monotonic()
        self.result = {"tone": tone, "text": text, "exitCode": exit_code}

    def running(self):
        with self.lock:
            return self.state in {"running", "waiting"}

    def stop(self, grace=STOP_GRACE_SECONDS, *, replaced=False, wait=False):
        """Stop the step: ask its process to end, wait ``grace`` seconds, then end its whole tree."""

        with self.lock:
            if self.state not in {"running", "waiting"}:
                return
            self.replaced = self.replaced or replaced
            if self.stopping:
                starter = None
            else:
                self.stopping = self.stop_requested = True
                starter = threading.Thread(target=self._stop, args=(grace,), daemon=True,
                                           name=f"setup-stop-{self.program}")
        if starter is not None:
            starter.start()
        if wait:
            self.done.wait(grace + OUTPUT_GRACE_SECONDS + CANCEL_SECONDS + 2)

    def before_stop(self):
        """What a step does before its process is stopped (the Codex sign-in cancels its login)."""

    def _stop(self, grace):
        try:
            self.before_stop()
        except Exception:
            pass
        process = self.process
        if process is not None:
            _end_group(process, grace, tree=self.TREE)

    def popen(self, stdin):
        """Start the step's process: no terminal, a private working folder, stdout (and stderr) piped."""

        self.folder = tempfile.mkdtemp(prefix="tcs-setup-")
        self.process = subprocess.Popen(self.argv, stdin=stdin, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, env=self.environment, cwd=self.folder,
                                        **session_options())
        return self.process

    def wait_output(self, process, readers):
        """Wait for the process, end what it left in its process group (``LEFTOVERS``), then wait for its output
        (OUTPUT_GRACE_SECONDS for what a lingering child holds).

        Returns (exit code, whether a Stop came before the process ended): a
        Stop after that undoes nothing the step did.
        """

        code = process.wait()
        with self.lock:
            self.exited = True
            stopped = self.stop_requested
        if self.LEFTOVERS:
            _end_leftovers(process.pid)
        for reader in readers:
            reader.join(OUTPUT_GRACE_SECONDS)
        self.flush()
        return code, stopped


class CommandTask(Task):
    """An install or an update: the official command, its live output, then the check that tells the result.

    What the command leaves running in its process group ends with it. A Stop
    that comes once the command has ended undoes nothing: a command that
    succeeded keeps its result (the result says the Stop came too late) and
    only the sign-in it was confirmed with does not start.
    """

    LEFTOVERS = True

    def run(self):
        try:
            process = self.popen(subprocess.DEVNULL)
        except OSError as exc:
            self.finish("failed", f"The command could not start ({exc.strerror or type(exc).__name__}).")
            return
        reader = threading.Thread(target=self.read_stream, args=(process.stdout,), daemon=True,
                                  name=f"setup-output-{self.program}")
        reader.start()
        code, ended_by_stop = self.wait_output(process, [reader])
        with self.lock:
            self.exit_code = code
            lines = list(self.lines)
        facts = self.owner.recheck(self.program)
        if ended_by_stop and code != 0:
            self.finish("stopped", self.stop_text(), code)
            return
        remembered = False
        if code == 0 and facts.get("state") in {"missing", "offpath"}:
            # Installed outside the lookups of jobs: remember the folder, so jobs (also waiting ones) find it now.
            found = shutil.which(COMMANDS[self.program], path=str(installer_folder(self.program)))
            if found:
                try:
                    providers.save_program_folder(self.program, os.path.dirname(found))
                    remembered = True
                except (providers.ProviderError, OSError):
                    pass
                facts = self.owner.recheck(self.program)
        name, works = NAMES[self.program], facts.get("state") == "works"
        version, where = facts.get("version") or "", shown(facts.get("path"))
        if code == 0 and works:
            if self.action == "update":
                before = self.after.get("versionBefore") or ""
                text = (f"Updated from {before} to {version}." if before and before != version
                        else f"Already the newest version ({version}).")
            else:
                text = f"Installed: {version} at {where}."
                if remembered or facts.get("remembered"):
                    text += " Jobs find it there; open a new terminal to use it yourself."
                if facts.get("otherCopies"):
                    text += f" Jobs use {where}, the first one found."
            chain = self._chain_wanted(facts)
            if chain and not self.stop_requested:
                # The sign-in starts while this step still runs, so its result shows with the sign-in (nextTask).
                self._chain(facts)
            with self.lock:
                # Under the lock Stop takes: a Stop is seen here, or it comes once this step has ended.
                late, started = self.stop_requested, self.next_task
                if late:
                    text += (" Stop came after the update had finished." if self.action == "update"
                             else " Stop came after the installer had finished"
                             + ((", so the sign-in it started was stopped." if started
                                 else ", so the sign-in you chose was not started.") if chain else "."))
                if self.result is None:
                    self._settle_locked("succeeded", text, code)
            if late and started:
                self.owner.stop_step(started)  # It started just before the Stop was seen.
            return
        if code == 0:
            self.finish("failed", f"The installer finished, but TCS Prover cannot find {name} in "
                                  f"{shown(installer_folder(self.program))}. See the output above.", code)
            return
        text = failure_text(self.program, code, lines)
        if works:
            text += f" {name} runs now ({version})."
        self.finish("failed", text, code)

    def _chain_wanted(self, facts):
        """Whether the sign-in this Install was confirmed with follows: ticked, and the program works signed out."""

        return (self.action == "install" and bool(self.after.get("signInAfter")) and facts.get("state") == "works"
                and (facts.get("signIn") or {}).get("state") == "signed-out")

    def _chain(self, facts):
        """Install then sign in: the sign-in the user ticked in the Install dialog, under the same confirmation."""

        if not self._chain_wanted(facts) or self.stop_requested:
            return
        try:
            task = self.owner.start_chained(self.program, self.after.get("signInOption"), self)
        except SetupRefused:
            return
        if task is not None:
            with self.lock:
                self.next_task = task.id


class ClaudeSignIn(Task):
    """`claude auth login`: the link it prints, the code the sign-in page may show (pasted here), its result.

    Stop ends its process group only: on Linux the browser it may open (xdg-open) can be its own descendant.
    """

    TREE = False

    def raw_line(self, raw):
        plain = CONTROLS.sub("", ESCAPES.sub("", raw))
        match = re.search(r"visit:\s*(https?://\S+)", plain)
        if match and self.link is None:
            url = match.group(1)
            trusted, host = trusted_link(url, "claude")
            self.link = {"url": url, "trusted": trusted, "host": host}
            self.secrets.add(url)
            if self.state == "running":
                self.state = "waiting"
            # The link itself never enters the output.
            return plain[:match.start(1)] + "(the link is shown above)"
        if "Invalid code" in plain:
            self.input = {"sent": False, "message": CODE_REFUSED_TEXT}
        return raw

    def run(self):
        try:
            process = self.popen(subprocess.PIPE)
        except OSError as exc:
            self.finish("failed", f"Claude Code could not start ({exc.strerror or type(exc).__name__}).")
            return
        reader = threading.Thread(target=self.read_stream, args=(process.stdout,), daemon=True,
                                  name="setup-output-claude")
        reader.start()
        code, _ = self.wait_output(process, [reader])
        with self.lock:
            self.exit_code = code
            output = "\n".join(self.lines)
        facts = self.owner.recheck(self.program)
        if code == 0:
            # Signed in, also when a Stop came after Claude Code had finished.
            sign_in = facts.get("signIn") or {}
            email, plan = sign_in.get("email"), sign_in.get("plan")
            text = f"Signed in as {email} ({plan})." if email and plan else "Signed in."
            self.finish("succeeded", text, code)
            return
        if self.stop_requested:
            self.finish("stopped", REPLACED_TEXT if self.replaced else STOPPED_SIGN_IN, code)
            return
        if "run interactive /login" in output:
            self.finish("failed", "Your organization's settings require signing in inside Claude Code: run claude, "
                                  "then /login, in a terminal.", code)
            return
        reason = next((line.split("Login failed:", 1)[1].strip() for line in reversed(output.splitlines())
                       if "Login failed:" in line), "")
        self.finish("failed", f"Claude Code could not sign in: {reason.rstrip('.') or f'exit code {code}'}.", code)

    def send(self, text):
        """Write one pasted code to Claude Code's stdin; it is removed from anything shown afterwards."""

        with self.lock:
            if self.state != "waiting" or self.process is None or self.process.stdin is None:
                raise SetupRefused(NOT_WAITING_TEXT, 409)
            self.secrets.add(text)
            try:
                self.process.stdin.write((text + "\n").encode("utf-8"))
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise SetupRefused(NOT_WAITING_TEXT, 409) from None
            self.input = {"sent": True, "message": ""}
            if self._line:  # The prompt ("Paste code here if prompted > ") comes first.
                self._finish_line_locked()
            self._keep_locked("(code sent)")


class CodexSignIn(Task):
    """A ChatGPT sign-in through the Codex app-server's sign-in calls: a link or a one-time code, then its result."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.condition = threading.Condition(self.lock)
        self.responses, self.completions = {}, []
        self.eof = False
        self.login_id = None
        self._write_lock = threading.Lock()
        # Set once a Stop has tried to cancel the sign-in, so the process is not ended under that request.
        self.cancel_done = threading.Event()

    def send_message(self, message):
        process = self.process
        if process is None or process.stdin is None:
            raise SignInFailed("The Codex CLI ended before the sign-in finished.")
        with self._write_lock:
            try:
                process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                process.stdin.flush()
            except (OSError, ValueError):
                raise SignInFailed("The Codex CLI ended before the sign-in finished.") from None

    def pump(self, stream):
        """Read the protocol: answers by id, the sign-in's completion, requests (each refused); other lines to the log."""

        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    message = None
                if not isinstance(message, dict):
                    self.log(clean(line, self.secrets))
                    continue
                if "method" in message and "id" in message:
                    try:
                        self.send_message({"id": message["id"], "error": {
                            "code": -32601, "message": "TCS Prover Setup cannot answer this."}})
                    except SignInFailed:
                        pass
                elif "id" in message:
                    with self.condition:
                        self.responses[message["id"]] = message
                        self.condition.notify_all()
                elif message.get("method") == "account/login/completed":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    with self.condition:
                        self.completions.append(params)
                        self.condition.notify_all()
                # Every other notification (account/updated, remoteControl/status/changed, ...) is left alone.
        except (OSError, ValueError):
            pass
        finally:
            with self.condition:
                self.eof = True
                self.condition.notify_all()

    def wait_response(self, identity, timeout, *, stopping=False):
        deadline = time.monotonic() + timeout
        with self.condition:
            while identity not in self.responses:
                if self.eof:
                    raise SignInFailed("The Codex CLI ended before the sign-in finished. See the output above.")
                if self.stop_requested and not stopping:
                    raise Stopped()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SignInFailed(f"The Codex CLI did not answer within {_seconds_text(timeout)}.")
                self.condition.wait(min(remaining, 0.5))
            return self.responses.pop(identity)

    def result_of(self, reply):
        error = reply.get("error")
        if isinstance(error, dict):
            raise SignInFailed(*codex_sign_in_error(error.get("message"), error.get("code")))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def run(self):
        try:
            self.folder = tempfile.mkdtemp(prefix="tcs-setup-")
            process = self.process = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.environment, cwd=self.folder, **session_options())
        except OSError as exc:
            self.finish("failed", f"The Codex CLI could not start ({exc.strerror or type(exc).__name__}).")
            return
        pump = threading.Thread(target=self.pump, args=(process.stdout,), daemon=True, name="setup-codex-protocol")
        drain = threading.Thread(target=self.read_stream, args=(process.stderr,), daemon=True,
                                 name="setup-codex-stderr")
        pump.start()
        drain.start()
        try:
            self.protocol()
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            _end_group(process, 0 if self.stop_requested else 2)
            pump.join(OUTPUT_GRACE_SECONDS)
            drain.join(OUTPUT_GRACE_SECONDS)
            self.flush()

    def protocol(self):
        self.send_message({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "tcs_prover_setup", "title": "TCS Prover Setup", "version": "1"},
            "capabilities": {}}})
        self.result_of(self.wait_response(1, ANSWER_SECONDS))
        self.send_message({"method": "initialized", "params": {}})
        self.send_message({"id": 2, "method": "config/read", "params": {}})
        reply = self.wait_response(2, ANSWER_SECONDS)
        config = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        config = config.get("config") if isinstance(config.get("config"), dict) else config
        if config.get("forced_login_method") == "api":
            raise SignInFailed("Your Codex settings allow only API-key sign-in (forced_login_method = api).")
        browser = self.option != "code"
        self.send_message({"id": 3, "method": "account/login/start",
                           "params": {"type": "chatgpt" if browser else "chatgptDeviceCode"}})
        started = self.result_of(self.wait_response(3, ANSWER_SECONDS))
        login_id = started.get("loginId") if isinstance(started.get("loginId"), str) else None
        with self.lock:
            self.login_id = login_id
            if browser:
                url = started.get("authUrl") if isinstance(started.get("authUrl"), str) else ""
                if not url:
                    raise SignInFailed("The Codex CLI gave no sign-in link.")
                trusted, host = trusted_link(url, "codex")
                self.link = {"url": url, "trusted": trusted, "host": host}
                self.secrets.add(url)
            else:
                url = started.get("verificationUrl") if isinstance(started.get("verificationUrl"), str) else ""
                user_code = started.get("userCode") if isinstance(started.get("userCode"), str) else ""
                if not url or not user_code:
                    raise SignInFailed("The Codex CLI gave no one-time code.")
                trusted, host = trusted_link(url, "codex")
                self.code = {"url": url, "trusted": trusted, "host": host, "userCode": _cut(clean(user_code), 40)}
            if self.state == "running":
                self.state = "waiting"
            self._keep_locked(WAITING_TEXT)
        completion = self.wait_completion(login_id)
        if completion.get("success") is True:
            self.owner.recheck(self.program)
            self.finish("succeeded", "Signed in with ChatGPT.", 0)
            return
        raise SignInFailed(*codex_sign_in_error(completion.get("error") or "the sign-in did not finish"))

    def wait_completion(self, login_id):
        """Wait, without a time limit, for this sign-in's account/login/completed (or a Stop, or the end)."""

        with self.condition:
            while True:
                for params in self.completions:
                    if params.get("loginId") in (login_id, None):
                        return params
                if self.stop_requested and self.cancel_done.is_set():
                    raise Stopped()
                if self.eof:
                    raise SignInFailed("The Codex CLI ended before the sign-in finished. See the output above.")
                self.condition.wait(0.5)

    def before_stop(self):
        """Cancel the waiting sign-in (its local sign-in server ends with it), then stop the process."""

        try:
            with self.lock:
                login_id = self.login_id
            if login_id is None or self.process is None or self.process.poll() is not None:
                return
            try:
                self.send_message({"id": 4, "method": "account/login/cancel", "params": {"loginId": login_id}})
                self.wait_response(4, CANCEL_SECONDS, stopping=True)
            except (SignInFailed, Stopped):
                pass
        finally:
            with self.condition:
                self.cancel_done.set()
                self.condition.notify_all()

    def finish(self, state, text, exit_code=None):
        if state == "stopped" and self.replaced:
            text = REPLACED_TEXT
        super().finish(state, text, exit_code)


def codex_sign_in_error(message, code=None):
    """(the words for a failed Codex sign-in, whether the terminal commands help)."""

    text = str(message or "")
    lower = text.lower()
    if "failed to start login server" in lower:
        return ("Another Codex sign-in is waiting on this computer (port 1455 is in use). Finish or close it, or sign "
                "in with a code."), False
    if "device code login is not enabled" in lower:
        return ("Sign-in with a code is not turned on for your ChatGPT account. Turn on device code sign-in in "
                "ChatGPT's security settings, or sign in in this browser."), False
    if "timed out" in lower:
        return "The code expired after 15 minutes. Start the sign-in again.", False
    if code == -32601 or "unknown variant" in lower:
        return ("This Codex CLI cannot sign in from TCS Prover. Run codex login in a terminal (or codex login "
                "--device-auth on another computer)."), True
    return f"The Codex CLI could not sign in: {_cut(clean(text).rstrip('.'), 300)}.", False


# --- The page's server side --------------------------------------------------------------------------------------

TITLES = {("codex", "install"): "Installing the Codex CLI", ("codex", "update"): "Updating the Codex CLI",
          ("codex", "sign-in"): "Signing in to the Codex CLI", ("claude", "install"): "Installing Claude Code",
          ("claude", "update"): "Updating Claude Code", ("claude", "sign-in"): "Signing in to Claude Code"}


class SetupTasks:
    """The Setup page's state in this server: the last check, and the latest step of each program.

    Creating it starts no process: the first check runs at the first GET /setup
    without ``cached``. ``busy(program)`` names this server's jobs that run a
    model process through ``program`` now (the server's _setup_busy).
    ``lock`` guards the state only: no check or other process ever runs while
    it is held, so Stop, a step's output and closing never wait for one.
    """

    def __init__(self, busy=None):
        self.busy = busy or (lambda program: [])
        self.lock = threading.RLock()
        self.tasks, self.latest = {}, {}
        self.facts, self.checked_at, self.checked = {}, "", None
        self._checking = None
        self.closed = False

    # Checks ------------------------------------------------------------------------------------------------------------
    def _check(self, refresh, cached=False):
        """The facts of every program: the last check (younger than CACHE_SECONDS), or a new one.

        ``cached`` (the page's background read) never starts a process: it
        returns the last facts of any age, and none for a program not checked
        yet. One check runs at a time: a second request waits for it and
        shares its result.
        """

        with self.lock:
            if cached and not refresh:
                return dict(self.facts)
            have = bool(self.facts) and all(program in self.facts for program in PROGRAMS)
            fresh = have and self.checked is not None and time.monotonic() - self.checked < CACHE_SECONDS
            if have and not refresh and fresh:
                return dict(self.facts)
            running = self._checking
            if running is None:
                running = self._checking = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            running.wait()
            with self.lock:
                return dict(self.facts)
        try:
            with self.lock:
                # A program whose install or update runs keeps its last facts (a check could hold its files).
                skip = {program for program, identity in self.latest.items()
                        if self.tasks.get(identity) and self.tasks[identity].running()
                        and self.tasks[identity].action in {"install", "update"} and program in self.facts}
            results = check([program for program in PROGRAMS if program not in skip])
            with self.lock:
                self.facts.update(results)
                self.checked, self.checked_at = time.monotonic(), _now_text()
                return dict(self.facts)
        finally:
            with self.lock:
                self._checking = None
            running.set()

    def recheck(self, program):
        """Check one program again (after its step), and keep the result for the page. Never under ``lock``."""

        result = check([program])[program]
        with self.lock:
            self.facts[program] = result
        return result

    def _fresh_facts(self, program, chained_from=None):
        """The facts a step of ``program`` is planned from, outside the lock: the last check, or a new one when
        there is none or the program moved since (moved()). While its install or update runs, the last facts stay
        (a check could hold its files; the plan says that step runs), except for the install that starts its
        chained sign-in (``chained_from``)."""

        with self.lock:
            facts = self.facts.get(program)
            running = self._running(program)
        if running is not None and running is not chained_from and running.action in {"install", "update"}:
            return facts or {}
        if facts is None or moved(program, facts):
            facts = self.recheck(program)
        return facts

    # The page ---------------------------------------------------------------------------------------------------------
    def status(self, refresh=False, cached=False):
        """GET /setup without the parts the server adds (defaults, jobs, connections)."""

        facts = self._check(refresh, cached)
        admin, platform = elevated(), platform_name()
        programs = {}
        for program in ACTION_PROGRAMS:
            programs[program] = self._program_status(program, facts.get(program) or {}, admin, platform)
        latex = facts.get("latex") or {"state": "checking", "tools": []}
        programs["latex"] = {**latex, "name": NAMES["latex"],
                             "commands": [{"group": "install", **row}
                                          for row in LATEX_COMMANDS.get(platform, LATEX_COMMANDS["other"])],
                             "help": LATEX_HELP, "notes": []}
        with self.lock:
            checked_at = self.checked_at
        return {"checkedAt": checked_at, "platform": platform, "elevated": admin,
                "elevatedText": ADMIN_TEXT.get(admin, ""), "programs": programs,
                "limits": {"keptLines": KEPT_LINES, "lineChars": LINE_CHARS, "checkSeconds": CHECK_SECONDS,
                           "answerSeconds": ANSWER_SECONDS}}

    def _running(self, program):
        with self.lock:
            task = self.tasks.get(self.latest.get(program, ""))
            return task if task is not None and task.running() else None

    def _program_status(self, program, facts, admin, platform):
        running = self._running(program)
        busy = self._busy(program)
        actions = plan_actions(program, facts, busy=busy, running=running, admin=admin, platform=platform)
        with self.lock:
            latest = self.tasks.get(self.latest.get(program, ""))
        path = facts.get("path") or ""
        return {"name": NAMES[program], "state": facts.get("state") or "checking", "path": path,
                "shownPath": shown(path), "kind": facts.get("kind") or "", "kindText": kind_text(program, facts.get("kind")),
                "offPath": facts.get("offPath") or "", "shownOffPath": shown(facts.get("offPath")),
                "remembered": facts.get("remembered") or "", "shownRemembered": shown(facts.get("remembered")),
                "otherCopies": [shown(copy) for copy in facts.get("otherCopies") or []],
                "version": facts.get("version") or "", "problem": facts.get("problem") or "",
                "signIn": {key: value for key, value in (facts.get("signIn") or {}).items()
                           if key in {"state", "text", "method"}} or {"state": "not-checked", "text": "", "method": ""},
                "target": shown(installer_target(program)), "notes": program_notes(program, facts, platform),
                "actions": actions, "commands": copy_commands(program, facts, platform),
                "task": latest.summary() if latest is not None else None}

    def _busy(self, program):
        try:
            return [title for title in (self.busy(program) or [])]
        except Exception:
            return []

    # Steps --------------------------------------------------------------------------------------------------------
    def start(self, body):
        """POST /setup/action: check the request and start the step it confirms ({"task"} or {"use-folder": facts})."""

        program, action, option = body.get("program"), body.get("action"), body.get("option")
        if program not in ACTION_PROGRAMS or action not in ACTIONS:
            raise SetupRefused(UNKNOWN_ACTION, 400)
        if action == "sign-in":
            if option not in SIGN_IN_OPTIONS[program]:
                raise SetupRefused(UNKNOWN_ACTION, 400)
        elif option not in (None, ""):
            raise SetupRefused(UNKNOWN_ACTION, 400)
        sign_in_after = body.get("signInAfter")
        sign_in_option = body.get("signInOption")
        if sign_in_after not in (None, True, False) or (action != "install" and sign_in_after):
            raise SetupRefused(UNKNOWN_ACTION, 400)
        if sign_in_after and sign_in_option not in SIGN_IN_OPTIONS[program]:
            raise SetupRefused(UNKNOWN_ACTION, 400)
        command = body.get("command")
        if not isinstance(command, str):
            raise SetupRefused(STALE_TEXT, 409, with_status=True)
        admin = elevated()
        if admin:
            raise SetupRefused(ADMIN_TEXT[admin], 403)
        return self._begin(program, action, option, command, bool(sign_in_after), sign_in_option)

    def _begin(self, program, action, option, command, sign_in_after=False, sign_in_option=None, chained_from=None):
        """Make sure of the program's facts (outside the lock), then plan and start the step under it.

        A program that moved since its last check is checked again first, so
        the plan sees, for example, a script of your own that appeared where
        OpenAI's installer writes, and no longer offers Install or Update.
        One that keeps moving while this happens is refused (CHANGED_TEXT)
        after MOVED_TRIES checks. A new sign-in replaces the one that waits
        for the user: that one ends first (its local sign-in server with it),
        outside the lock, so the page keeps reading the status meanwhile.
        """

        replace, moves = True, 0
        while True:
            self._fresh_facts(program, chained_from)
            waiting = None
            with self.lock:
                if chained_from is not None and (self.closed or chained_from.stop_requested):
                    return None
                try:
                    result = self._start_locked(program, action, option, command, sign_in_after, sign_in_option,
                                                chained_from=chained_from, replace=replace)
                except _Moved:
                    moves += 1
                    if moves >= MOVED_TRIES:
                        raise SetupRefused(CHANGED_TEXT, 409, with_status=True) from None
                    continue
                except _Replace as replacing:
                    waiting = replacing.task
            if waiting is None:
                break
            waiting.stop(STOP_GRACE_SECONDS, replaced=True, wait=True)
            replace = False
        if "useFolder" in result:
            self.recheck(program)  # Outside the lock: the page then shows the program as jobs now find it.
        return result

    def _start_locked(self, program, action, option, command, sign_in_after=False, sign_in_option=None,
                      chained_from=None, replace=True):
        """Plan the step from the facts _begin() made sure of, compare the command, and start it. Starts no check:
        a program that moved again meanwhile raises _Moved (unless its own install or update runs, which the plan
        refuses anyway)."""

        facts = self.facts.get(program)
        running = self._running(program)
        if running is not None and running is chained_from:
            running = None  # The install that starts this sign-in ends with it.
        if running is None or running.action == "sign-in":
            if facts is None or moved(program, facts):
                raise _Moved()
        facts = facts or {}
        replacing = None
        if running is not None and action == "sign-in" and running.action == "sign-in" and replace:
            replacing = running  # A new sign-in replaces one that waits for the user.
            running = None
        actions = plan_actions(program, facts, busy=self._busy(program), running=running, admin="",
                               platform=platform_name())
        key = {"install": "install", "update": "update", "sign-in": "signIn", "use-folder": "useFolder"}[action]
        planned = actions[key]
        if action == "use-folder":
            if command != planned.get("folder"):
                raise SetupRefused(STALE_TEXT, 409, with_status=True)
        elif action == "sign-in":
            dialog = planned.get("dialog") or {}
            expected = next((row["command"] for row in dialog.get("options", []) if row["id"] == option), None)
            if expected is None or command != expected:
                raise SetupRefused(STALE_TEXT if planned.get("shown") else planned.get("reason") or STALE_TEXT, 409,
                                   with_status=True)
        else:
            dialog = planned.get("dialog") or {}
            if not dialog or command != dialog.get("command"):
                raise SetupRefused(STALE_TEXT, 409, with_status=True)
        if not planned.get("available"):
            body = {"task": running.snapshot()} if running is not None else {}
            raise SetupRefused(planned.get("reason") or "This step is not available now.", 409, body,
                               with_status=True)
        if action == "use-folder":
            providers.save_program_folder(program, planned["folder"])
            return {"useFolder": planned["folder"]}
        if replacing is not None:
            raise _Replace(replacing)
        if action == "sign-in":
            argv = sign_in_argv(program, facts["path"], option)
        elif action == "update" and program == "claude":
            argv = [facts["path"], "update"]
        else:
            argv = installer_argv(program, facts.get("tools") or _tools())
        task_class = CommandTask if action != "sign-in" else CodexSignIn if program == "codex" else ClaudeSignIn
        task = task_class(self, program, action, option or "", argv, task_environment(program, action),
                          command_text(argv), TITLES[(program, action)],
                          after={"signInAfter": sign_in_after, "signInOption": sign_in_option,
                                 "versionBefore": facts.get("version") or ""})
        self._keep(program, task, chained_from)
        task.start()
        return {"task": task.snapshot()}

    def start_chained(self, program, option, install):
        """The sign-in an Install step was confirmed with (its "Then sign in" box), once the program works.

        Not after a Stop of that install, nor while TCS Prover closes.
        """

        with self.lock:
            if self.closed or install.stop_requested:
                return None
        facts = self._fresh_facts(program, install) or {}
        actions = plan_actions(program, facts, busy=self._busy(program), running=None, admin=elevated(),
                               platform=platform_name())
        planned = actions["signIn"]
        if not planned.get("available"):
            return None
        dialog = planned.get("dialog") or {}
        command = next((row["command"] for row in dialog.get("options", []) if row["id"] == option), None)
        if command is None:
            return None
        if not self._begin(program, "sign-in", option, command, chained_from=install):
            return None
        with self.lock:
            return self.tasks.get(self.latest.get(program, ""))

    def _keep(self, program, task, chained_from=None):
        """Keep only the latest step of each program (and the install it was chained from, for the page)."""

        keep = {task.id} | ({chained_from.id} if chained_from is not None else set())
        for identity in [identity for identity, other in self.tasks.items()
                         if other.program == program and identity not in keep]:
            if not self.tasks[identity].running():
                del self.tasks[identity]
        self.tasks[task.id] = task
        self.latest[program] = task.id

    def task(self, identity):
        with self.lock:
            task = self.tasks.get(identity) if isinstance(identity, str) else None
        if task is None:
            raise SetupRefused(GONE_TEXT, 404)
        return task

    def read(self, identity, after=0):
        """GET /setup/task?id=&after=N."""

        try:
            after = max(0, int(after or 0))
        except (TypeError, ValueError):
            raise SetupRefused("Invalid output position.", 400) from None
        return {"task": self.task(identity).snapshot(after)}

    def stop(self, body):
        """POST /setup/stop {id}: a step that has ended stays as it was."""

        task = self.task(body.get("id"))
        task.stop()
        return {"task": task.snapshot()}

    def stop_step(self, identity):
        """Stop one step by its ID, if it is still kept (the sign-in a stopped install had just started)."""

        with self.lock:
            task = self.tasks.get(identity)
        if task is not None:
            task.stop()

    def send_input(self, body):
        """POST /setup/input {id, text}: the code Claude Code's sign-in page shows, to Claude Code only."""

        task = self.task(body.get("id"))
        text = body.get("text")
        if not isinstance(task, ClaudeSignIn):
            raise SetupRefused(NOT_WAITING_TEXT, 409)
        if not isinstance(text, str):
            raise SetupRefused(CODE_FORMAT_TEXT, 400)
        text = text.strip()
        if not 1 <= len(text) <= CODE_CHARS or not CLAUDE_CODE.match(text):
            raise SetupRefused(CODE_FORMAT_TEXT, 400)
        task.send(text)
        return {"task": task.snapshot()}

    def close(self):
        """Stop every running step (TCS Prover is closing): a short grace, then the whole tree."""

        with self.lock:
            self.closed = True
            running = [task for task in self.tasks.values() if task.running()]
        for task in running:
            task.stop(CLOSE_GRACE_SECONDS)
        for task in running:
            task.done.wait(CLOSE_GRACE_SECONDS + CANCEL_SECONDS + OUTPUT_GRACE_SECONDS)
