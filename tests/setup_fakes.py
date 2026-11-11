"""Fake Codex CLI, Claude Code, LaTeX tools and installer for the Setup page tests.

Each fake is a Python script (``#!<this Python> -I``) steered by small files in
the folder named by $FAKE_STATE, and every call appends its argv and the names
of selected environment variables to calls.jsonl there. Nothing here reaches a
network, a real program or a real login.

Steering files (all optional): codex-version, codex-version-exit, codex-version-sleep, codex-no-status,
codex-login (chatgpt | apikey | none | error | sleep | access), codex-signin (ok | port-busy | unknown |
device-disabled | fail), codex-forced, codex-request, claude-version, claude-no-status, claude-no-update,
claude-login (max | pro | none | token | apikey | bad), claude-login-result (ok | fail | managed), claude-echo
(the sign-in prints back each code it reads), and for the installer: exit.txt, sleep, web-page, lines, long-line,
no-place, detached-child (the child of "sleep" starts a session of its own), partial (that many characters and
NEWEST-OUTPUT-TAIL with no line break, before "sleep" waits), leftover (a child in the installer's process group
that keeps printing after it ends), linger (a child in a session of its own that keeps the output open after it
ends) and record-sid (the installer's session in installer-sid).
"""

import os
from pathlib import Path
import stat
import sys

WATCHED = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
           "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "SSH_AUTH_SOCK",
           "CODEX_INSTALL_DAEMON_ONLY", "CODEX_HOME", "CODEX_INSTALL_DIR", "TCS_PROVER_API_TOKEN", "NO_COLOR",
           "TERM", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "RUST_LOG")

COMMON = r'''
import json, os, sys, time, threading
STATE = os.environ.get("FAKE_STATE", "")
WATCHED = %(watched)r
def read(name, default=""):
    try:
        with open(os.path.join(STATE, name), encoding="utf-8") as stream:
            return stream.read().strip()
    except OSError:
        return default
def write(name, text):
    with open(os.path.join(STATE, name), "w", encoding="utf-8") as stream:
        stream.write(text)
def record(**extra):
    with open(os.path.join(STATE, "calls.jsonl"), "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"program": PROGRAM, "argv": sys.argv[1:], "pid": os.getpid(), "cwd": os.getcwd(),
                                 "env": sorted(name for name in WATCHED if name in os.environ), **extra}) + "\n")
'''

CODEX = r'''
PROGRAM = "codex"
record()
args = sys.argv[1:]
if args == ["--version"]:
    if read("codex-version-sleep"):
        write("codex-version-pid", str(os.getpid()))
        time.sleep(float(read("codex-version-sleep")))
    print(read("codex-version", "codex-cli 0.160.0"))
    sys.exit(int(read("codex-version-exit", "0")))
if args == ["login", "--help"]:
    print("Manage login\n\nUsage: codex login [OPTIONS] [COMMAND]\n\nCommands:")
    if not read("codex-no-status"):
        print("  status  Show login status")
    print("  help    Print this message or the help of the given subcommand(s)")
    sys.exit(0)
if args == ["login", "status"]:
    mode = read("signed-in") and "chatgpt" or read("codex-login", "chatgpt")
    if mode == "sleep":
        time.sleep(30)
    line = {"chatgpt": "Logged in using ChatGPT", "apikey": "Logged in using an API key - sk-test-ABCDEFGH12345678",
            "none": "Not logged in", "error": "Error checking login status: the keyring is locked",
            "access": "Logged in using access token - eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eXoifQ.c2lnbmF0dXJl"}[mode]
    print(line, file=sys.stderr)
    sys.exit(0 if mode in {"chatgpt", "apikey", "access"} else 1)
if args == ["app-server"]:
    lock = threading.Lock()
    def send(message):
        with lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()
    def complete(login_id):
        while True:
            if read("complete"):
                ok = read("complete") == "ok"
                if ok:
                    write("signed-in", "1")
                send({"method": "account/updated", "params": {}})
                send({"method": "account/login/completed",
                      "params": {"loginId": login_id, "success": ok, "error": None if ok else read("complete")}})
                return
            time.sleep(0.05)
    print("codex app-server listening", file=sys.stderr, flush=True)
    for line in sys.stdin:
        message = json.loads(line)
        record(rpc=message.get("method") or "reply", message=message)
        method, identity = message.get("method"), message.get("id")
        if method == "initialize":
            send({"id": identity, "result": {"codexHome": os.environ.get("CODEX_HOME", ""), "platformFamily": "unix",
                                             "platformOs": "macos", "userAgent": "fake"}})
        elif method == "config/read":
            send({"id": identity, "result": {"config": {"cli_auth_credentials_store": "file",
                                                        "forced_login_method": read("codex-forced") or None}}})
        elif method == "account/login/start":
            mode, kind = read("codex-signin", "ok"), message["params"]["type"]
            if read("codex-request"):
                sys.stdout.write("this line is not JSON\n")
                sys.stdout.flush()
                send({"id": 99, "method": "item/tool/requestUserInput", "params": {}})
            if mode == "port-busy":
                send({"id": identity, "error": {"code": -32603,
                                                "message": "failed to start login server: address in use"}})
            elif mode == "unknown":
                send({"id": identity, "error": {"code": -32601, "message": "Method not found"}})
            elif mode == "device-disabled" and kind == "chatgptDeviceCode":
                send({"id": identity, "error": {"code": -32603,
                                                "message": "device code login is not enabled for this account"}})
            elif kind == "chatgpt":
                send({"id": identity, "result": {"type": "chatgpt", "loginId": "L1",
                                                 "authUrl": read("auth-url", "https://auth.openai.com/oauth/authorize?"
                                                                 "client_id=x&state=STATE123&code_challenge=CH")}})
                threading.Thread(target=complete, args=("L1",), daemon=True).start()
            else:
                send({"id": identity, "result": {"type": "chatgptDeviceCode", "loginId": "L2",
                                                 "verificationUrl": "https://auth.openai.com/codex/device",
                                                 "userCode": "ABCD-1234"}})
                threading.Thread(target=complete, args=("L2",), daemon=True).start()
        elif method == "account/login/cancel":
            send({"id": identity, "result": {"status": "canceled"}})
    sys.exit(0)
print("codex: unexpected arguments", args, file=sys.stderr)
sys.exit(2)
'''

CLAUDE = r'''
PROGRAM = "claude"
record()
args = sys.argv[1:]
if args == ["--version"]:
    print(read("claude-version", "2.1.289 (Claude Code)"))
    sys.exit(0)
if args == ["--help"]:
    print("Usage: claude [options] [command] [prompt]\n\nCommands:\n  auth            Manage authentication")
    if not read("claude-no-update"):
        print("  update|upgrade  Check for updates and install if available")
    sys.exit(0)
if args == ["auth", "--help"]:
    print("Usage: claude auth [options] [command]\n\nCommands:\n  login [options]  Sign in")
    if not read("claude-no-status"):
        print("  status [options]  Show authentication status")
    print("  logout  Log out")
    sys.exit(0)
if args == ["auth", "status", "--json"]:
    mode = read("signed-in") and "max" or read("claude-login", "max")
    rows = {"max": {"loggedIn": True, "authMethod": "claude.ai", "email": "a@b.com", "subscriptionType": "max",
                    "orgId": "org-secret-id", "orgName": "Lab"},
            "pro": {"loggedIn": True, "authMethod": "claude.ai", "email": None, "subscriptionType": "pro"},
            "none": {"loggedIn": False, "authMethod": None},
            "token": {"loggedIn": True, "authMethod": "oauth_token"},
            "apikey": {"loggedIn": True, "authMethod": "api_key", "email": None}}
    if mode == "bad":
        print("not json at all")
        sys.exit(1)
    print("warning: something on stderr", file=sys.stderr)
    print(json.dumps(rows[mode]))
    sys.exit(0 if rows[mode]["loggedIn"] else 1)
if args[:2] == ["auth", "login"]:
    url = read("claude-url", "https://claude.ai/oauth/authorize?code=true&client_id=c&state=ST4TE&code_challenge=CC")
    print("Opening browser to sign in…", flush=True)
    print(f"If the browser didn't open, visit: \x1b]8;;{url}\x07{url}\x1b]8;;\x07", flush=True)
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    result = read("claude-login-result", "ok")
    if result == "managed":
        print("\nLogin failed: your organization requires you to run interactive /login", file=sys.stderr)
        sys.exit(1)
    for line in sys.stdin:
        code = line.strip()
        with open(os.path.join(STATE, "claude-codes"), "a", encoding="utf-8") as stream:
            stream.write(code + "\n")
        if read("claude-echo"):
            print(f"received {code}", flush=True)
        if "#" not in code or code.startswith("bad"):
            print("Invalid code. Please make sure the full code was copied.", file=sys.stderr, flush=True)
            continue
        if result == "fail":
            print("Login failed: denied", file=sys.stderr, flush=True)
            sys.exit(1)
        write("signed-in", "1")
        print("Login successful.", flush=True)
        sys.exit(0)
    sys.exit(1)
if args == ["update"]:
    before = read("claude-version", "2.1.289 (Claude Code)")
    write("claude-version", "2.1.300 (Claude Code)")
    print(f"Current version: {before}\nSuccessfully updated from {before.split()[0]} to version 2.1.300")
    sys.exit(0)
print("claude: unexpected arguments", args, file=sys.stderr)
sys.exit(2)
'''

LATEXMK = r'''
PROGRAM = "latexmk"
record()
if read("latexmk-exit"):
    print("Can't locate Perl", file=sys.stderr)
    sys.exit(int(read("latexmk-exit")))
print("Latexmk, John Collins, 7 Jan. 2023. Version 4.79")
'''

PDFLATEX = r'''
PROGRAM = "pdflatex"
record()
print("pdfTeX 3.141592653-2.6-1.40.24 (TeX Live 2022)\nkpathsea version 6.3.4")
'''

# The installer stand-in: it prints what real installers print (escape codes, progress, secrets that must never
# be kept), then installs a copy of the fake program where the official installer would, and ends with exit.txt.
INSTALLER = r'''
PROGRAM = "installer"
record()
program = sys.argv[1]
if read("record-sid"):
    write("installer-sid", str(os.getsid(0)))
home = os.path.expanduser("~")
marker = read("marker", "MARKER")
if read("plain"):  # What a real installer prints, for previews and screenshots.
    if program == "codex":
        lines = ["Checking this computer: macOS, arm64", "Downloading Codex CLI 0.160.0 from releases.openai.com",
                 "Checksum verified", "Installing to ~/.codex/packages/standalone/releases/0.160.0"]
    else:
        lines = ["Checking this computer: macOS, arm64", "Downloading Claude Code 2.1.289 from downloads.claude.ai",
                 "Checksum verified", "Setting up Claude Code..."]
    for line in lines:
        print(line, flush=True)
else:
    print(f"\x1b[1mChecking this computer\x1b[0m: macOS, arm64 {marker}", flush=True)
    sys.stdout.write("Downloading  10%\rDownloading  55%\rDownloading 100%\r\n")
    print("key sk-ant-api03-ABCDEFGHIJKLMNOP and sk-proj-ZYXWVUTS98765432 seen")
    print("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLWhlcmU done")
    print("callback https://example.invalid/cb?code=SECRETCODE&state=SECRETSTATE ok")
    print("proxy http://alice:hunter2@proxy.example:8080 used")
    print("Authorization: Bearer abcdef0123456789secret")
sys.stdout.flush()
if read("lines"):
    for index in range(int(read("lines"))):
        print(f"line {index}")
if read("long-line"):
    print("x" * int(read("long-line")))
sys.stdout.flush()
if read("web-page"):
    print("bash: line 1: syntax error near unexpected token `<'", file=sys.stderr, flush=True)
    sys.exit(2)
if read("sleep"):
    import subprocess
    detached = {"start_new_session": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                             **(detached if read("detached-child") else {}))
    write("child-pid", str(child.pid))
    write("installer-pid", str(os.getpid()))
    if not read("plain"):
        print("waiting…", flush=True)
    if read("partial"):
        sys.stdout.write("y" * int(read("partial")) + "NEWEST-OUTPUT-TAIL")
        sys.stdout.flush()
    time.sleep(float(read("sleep")) if read("sleep").replace(".", "", 1).isdigit() and float(read("sleep")) > 1 else 60)
code = int(read("exit.txt", "0"))
if not read("no-place"):
    source = os.path.join(os.environ["FAKE_ROOT"], "fakes", program)
    if program == "codex":
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
        folder = os.path.join(codex_home, "packages", "standalone", "current", "bin")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, "codex")
        link = os.path.join(os.environ.get("CODEX_INSTALL_DIR") or os.path.join(home, ".local", "bin"), "codex")
    else:
        folder = os.path.join(home, ".local", "share", "claude", "versions")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, "2.1.289")
        link = os.path.join(home, ".local", "bin", "claude")
    with open(source, encoding="utf-8") as stream, open(target, "w", encoding="utf-8") as out:
        out.write(stream.read())
    os.chmod(target, 0o755)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(target, link)
    print(f"Installed {program} to {link}", flush=True)
if read("leftover") or read("linger"):
    import subprocess
    if read("leftover"):  # Same process group, keeps printing into the step's output.
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time\nfor index in range(200):\n"
                                  "    print('late line', index, flush=True)\n    time.sleep(0.1)"])
        write("leftover-pid", str(child.pid))
    if read("linger"):  # A session of its own, holding the output open for a while.
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"], start_new_session=True)
        write("linger-pid", str(child.pid))
sys.exit(code)
'''


def write_script(path, body):
    """A fake program at ``path``: this Python in isolated mode, the shared helpers, then ``body``."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable} -I\n" + COMMON % {"watched": WATCHED} + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def install_fakes(root):
    """The fakes under ``root``: their sources in fakes/, and the installer at bin-tools/fake-install."""

    root = Path(root)
    sources = {"codex": CODEX, "claude": CLAUDE, "latexmk": LATEXMK, "pdflatex": PDFLATEX}
    for name, body in sources.items():
        write_script(root / "fakes" / name, body)
    write_script(root / "tools" / "fake-install", INSTALLER)
    return root


def place(root, name, folder):
    """Copy the fake ``name`` into ``folder`` (for example the bin folder on PATH)."""

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_text((Path(root) / "fakes" / name).read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    return target


if __name__ == "__main__":  # pragma: no cover - a helper for previews: python setup_fakes.py <root>
    install_fakes(sys.argv[1])
    os.write(1, f"fakes in {sys.argv[1]}\n".encode())
