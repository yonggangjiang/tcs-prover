"""Workflow runners that do not depend on the server that started them.

The server is a window onto the jobs and a remote control for them, never a part of them (the design of the
meta-harness on the same machine). So a workflow runner

* runs in a session (a process group, on Windows a detached process) of its own, and the server's Ctrl-C, its
  terminal closing or the server being killed never reach it;
* reads nothing once it has started: its input is written to its standard input, which is then closed;
* writes its output to a log file of its own, never to a pipe, so it never waits for a reader: the server follows
  that file (LogFollower) and keeps the transcript from it as before.

The runner's launch is recorded beside the job's other controller files, in
``runs/.controller/<run>/runner/current.json``: its process ID and start time, its log, the file in which it writes
its exit code, and how much of its log the server has handled. A server that is closed leaves its runners working
and records how far it read; the next server finds each one by process ID and start time (adoptable()), reads its
log on from there (RunnerProcess.adopt()) and can pause or stop it as if it had started it. A runner that ended
while no server ran has its exit code in its exit file and the rest of its output in its log, which the next
server reads to the end before it settles the job. Once the server has read a runner to its end, its log, exit
file and record are removed: the transcript holds everything.

Nothing here reads the log of a runner that another live process still reads (a terminal run, an earlier server
that is still up): such a run stays "running elsewhere", as before.
"""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# The folder of a job's runner launch, in its controller folder (outside every model sandbox).
RUNNER_FOLDER = "runner"
CURRENT_FILENAME = "current.json"
SCHEMA_VERSION = 1
# The environment variable that names the file in which a runner writes its exit code (workflow_runner.py).
EXIT_ENV = "TCS_PROVER_RUNNER_EXIT"
# How often a follower looks for new output while the runner is quiet (seconds): at once after output, then
# slower, up to the longest wait.
FOLLOW_SHORTEST_SECONDS = 0.05
FOLLOW_LONGEST_SECONDS = 0.5
# How often the server records how far it has read a runner's log (seconds); a server that crashes rereads at
# most this much output, which its transcript then holds twice.
OFFSET_SAVE_SECONDS = 2.0
# How long a closing server waits for its readers to finish the line they handle (seconds).
DETACH_WAIT_SECONDS = 10.0
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
POSIX = os.name != "nt"


class RunnerDetached(BaseException):
    """Raised in a reader when its server closes: the runner works on, and the next server reads on.

    A BaseException, so that the readers' handlers of failed records (``except Exception``) let it through.
    """


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _write_atomic(path, text):
    """Replace one small owner-only file; return whether it was written."""

    path = Path(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------------------------------------------
# Which process is which: a process ID together with the time the process started, since an ID is reused.


def _windows_process_times(pid):
    """(creation time, still running) of a Windows process, or None when it cannot be asked."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query_limited_information, still_active = 0x1000, 259
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(query_limited_information, False, int(pid))
    if not handle:
        return None
    try:
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        code = wintypes.DWORD()
        running = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == still_active
        return f"{creation.dwHighDateTime}:{creation.dwLowDateTime}", running
    finally:
        kernel32.CloseHandle(handle)


def process_started(pid):
    """A text that identifies when process ``pid`` started, or None when it is gone or cannot be told.

    Linux: the start time in clock ticks since boot (/proc/<pid>/stat). macOS and the BSDs: the start time `ps`
    lists. Windows: the creation time. A process that has ended (a zombie on POSIX) has none.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            times = _windows_process_times(pid)
        except (OSError, AttributeError, ValueError):
            return None
        return times[0] if times and times[1] else None
    stat = Path(f"/proc/{pid}/stat")
    if Path("/proc/self/stat").exists():
        try:
            text = stat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fields = text[text.rfind(")") + 2:].split()
        # fields[0] is the state, fields[19] the start time (field 22 of the line).
        if len(fields) < 20 or fields[0] in {"Z", "X"}:
            return None
        return f"linux:{fields[19]}"
    try:
        listed = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10, check=False,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    line = listed.stdout.strip()
    if listed.returncode or not line or line.split()[0].startswith("Z"):
        return None
    return "ps:" + " ".join(line.split()[1:])


def same_process(pid, started):
    """Whether the process that started at ``started`` (process_started()) still runs as ``pid``."""

    return bool(started) and process_started(pid) == started


# ---------------------------------------------------------------------------------------------------------------
# The launch record.


def runner_folder(controller):
    return Path(controller) / RUNNER_FOLDER


def current_record(controller):
    """The job's recorded runner launch (current.json), or None."""

    record = _read_json(runner_folder(controller) / CURRENT_FILENAME)
    if not record or record.get("schemaVersion") != SCHEMA_VERSION:
        return None
    if not all(isinstance(record.get(key), str) and record[key] for key in ("launch", "log", "exit")):
        return None
    return record


def save_record(controller, record):
    return _write_atomic(runner_folder(controller) / CURRENT_FILENAME,
                         json.dumps(record, indent=2, sort_keys=True) + "\n")


def forget(controller, launch):
    """Remove the files of launch ``launch`` once its output has been read to its end."""

    folder = runner_folder(controller)
    record = current_record(controller)
    if record is not None and record.get("launch") == launch:
        for key in ("log", "exit"):
            try:
                Path(record[key]).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (folder / CURRENT_FILENAME).unlink(missing_ok=True)
        except OSError:
            pass


def read_exit_code(path):
    """The exit code a runner wrote when it ended, or None (it is running, or it was killed)."""

    value = _read_json(path)
    code = value.get("code") if value else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def write_exit_code(code):
    """In the runner: write its exit code where its server asked (EXIT_ENV), as its last act."""

    path = os.environ.get(EXIT_ENV)
    if path and isinstance(code, int):
        _write_atomic(path, json.dumps({"code": code, "endedAt": _now_iso()}) + "\n")


def adoptable(controller):
    """The runner launch a newly started server should read on, or None.

    That is a launch whose log the server that started it did not read to its end, and that no live process reads:
    the runner is still working, or it ended while no server read it. A launch whose starting process (a
    terminal run, an earlier server) is still alive is that process's to read.
    """

    record = current_record(controller)
    if record is None:
        return None
    if same_process(record.get("readerPid"), record.get("readerStarted")):
        return None  # Its reader (this process, a terminal run, another server) still reads it.
    return record


# ---------------------------------------------------------------------------------------------------------------
# Following a runner's log.


class LogFollower:
    """The lines of a runner's log, in order, as the runner writes them; the end comes once the runner has ended
    and everything it wrote has been read.

    ``handled`` is the byte offset up to which the caller has handled the lines: a line counts as handled when
    the caller asks for the next one. ``catching_up`` is true while the lines being read were written before
    the server started following them (it read them in one go), false once it has caught up with the runner;
    ``on_caught_up`` (if set) is called once then, between two lines. ``detach()`` makes the next request for a
    line raise RunnerDetached.
    """

    follows_file = True

    def __init__(self, path, offset, process, *, catching_up=False):
        self.path, self.process = Path(path), process
        self.handled = self._next = int(offset or 0)
        self.catching_up = catching_up
        self._buffer, self._stream, self._ended = b"", None, False
        self._detach = threading.Event()
        self.on_caught_up = None

    def detach(self):
        self._detach.set()

    @property
    def detached(self):
        return self._detach.is_set()

    def __iter__(self):
        return self

    def _open(self):
        if self._stream is None:
            self._stream = open(self.path, "rb")  # noqa: SIM115 - kept open while the runner writes.
            self._stream.seek(self._next + len(self._buffer))
        return self._stream

    def close(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None

    def __next__(self):
        self.handled = self._next
        wait = FOLLOW_SHORTEST_SECONDS
        while True:
            if self._detach.is_set():
                self.close()
                raise RunnerDetached()
            end = self._buffer.find(b"\n")
            if end >= 0:
                line, self._buffer = self._buffer[:end + 1], self._buffer[end + 1:]
                self._next += len(line)
                return line.decode("utf-8", errors="replace")
            try:
                chunk = self._open().read(1 << 16)
            except FileNotFoundError:
                chunk = b""
            if chunk:
                self._buffer += chunk
                wait = FOLLOW_SHORTEST_SECONDS
                continue
            if self._ended:
                self.close()
                if self._buffer:  # A last line without its newline (a killed runner).
                    line, self._buffer = self._buffer, b""
                    self._next += len(line)
                    return line.decode("utf-8", errors="replace")
                self.handled = self._next
                raise StopIteration
            if self.catching_up:
                self.catching_up = False
                callback, self.on_caught_up = self.on_caught_up, None
                if callback is not None:
                    callback()
            if self.process.poll() is not None:
                self._ended = True  # Read once more: the runner may have written just before it ended.
                continue
            self._detach.wait(wait)
            wait = min(FOLLOW_LONGEST_SECONDS, wait * 2)


# ---------------------------------------------------------------------------------------------------------------
# The process.


class RunnerProcess:
    """A runner as the server sees it, whether this server started it (``popen``) or an earlier one did.

    It offers what the server uses of a Popen: ``pid``, ``stdin`` (only of a runner started here, until its input
    is written), ``stdout`` (the LogFollower of its log), ``poll()``, ``wait()``, ``send_signal()``,
    ``terminate()``, ``kill()`` and ``returncode``. The exit code of a runner an earlier server started is the one
    it wrote in its exit file; one that was killed before it could has none (-1).
    """

    def __init__(self, record, popen=None, *, catching_up=False):
        self.record, self._popen = record, popen
        self.pid = popen.pid if popen is not None else record.get("pid")
        self.started = record.get("started")
        self.launch = record["launch"]
        self.adopted = popen is None
        self.stdin = popen.stdin if popen is not None else None
        self.returncode = None
        self.stdout = LogFollower(record["log"], record.get("consumed", 0), self, catching_up=catching_up)

    @classmethod
    def adopt(cls, record):
        return cls(record, None, catching_up=True)

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._popen is not None:
            code = self._popen.poll()
            if code is not None:
                self.returncode = code
            return code
        # Asking whether it still runs costs a `ps` on macOS: at most once a second, which only delays noticing its end.
        now = time.monotonic()
        if now - getattr(self, "_checked_at", 0.0) < 1.0:
            return None
        self._checked_at = now
        if same_process(self.pid, self.started):
            return None
        code = read_exit_code(self.record["exit"])
        self.returncode = -1 if code is None else code
        return self.returncode

    def wait(self, timeout=None):
        if self._popen is not None:
            code = self._popen.wait(timeout=timeout)
            self.returncode = code
            return code
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(f"runner {self.pid}", timeout)
            time.sleep(0.1)
        return self.returncode

    def send_signal(self, number):
        if self._popen is not None:
            self._popen.send_signal(number)
        elif same_process(self.pid, self.started):
            os.kill(self.pid, number)

    def terminate(self):
        if self._popen is not None:
            self._popen.terminate()
        elif same_process(self.pid, self.started):
            if POSIX:
                import signal
                os.kill(self.pid, signal.SIGTERM)
            else:
                subprocess.run(["taskkill", "/PID", str(self.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def kill(self):
        if self._popen is not None:
            self._popen.kill()
        elif same_process(self.pid, self.started):
            if POSIX:
                import signal
                os.kill(self.pid, signal.SIGKILL)
            else:
                self.terminate()


def detached_options():
    """Popen options that put a runner in a session of its own, which nothing of the server's reaches."""

    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(subprocess, "DETACHED_PROCESS", 0x8)
                 | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000))
        return {"creationflags": flags}
    return {"start_new_session": True}


def start(argv, *, controller, cwd, env, stdin_text, pass_fds=(), kind="workflow"):
    """Start one runner with its output in a log file of its own; return its RunnerProcess.

    The launch is recorded (current.json) before the runner reads its input, so a server that is closed at once
    still leaves a record the next one finds. ``stdin_text`` is written and the input closed; a failure to write
    it stops the runner (the caller's handling) and is raised.
    """

    folder = runner_folder(controller)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    launch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
    log, exit_file = folder / f"{launch}.log", folder / f"{launch}.exit"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | NOFOLLOW, 0o600)
    environment = {**(env if env is not None else os.environ), EXIT_ENV: str(exit_file.absolute())}
    options = detached_options()
    try:
        try:
            popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=descriptor, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", cwd=cwd, env=environment,
                                     **options, **({"pass_fds": tuple(pass_fds)} if pass_fds and POSIX else {}))
        except OSError:
            if os.name != "nt":
                raise
            # A Windows job object that allows no breakaway: start inside it (its end then ends the runner too).
            options["creationflags"] &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000)
            popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=descriptor, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", cwd=cwd, env=environment,
                                     **options)
    finally:
        os.close(descriptor)
    if popen.stdout is not None:
        # A stand-in for Popen (the tests') that offers its own output: it is read as it is, and nothing is recorded.
        for path in (log, exit_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return popen
    record = {"schemaVersion": SCHEMA_VERSION, "launch": launch, "kind": kind, "pid": popen.pid,
              "started": process_started(popen.pid), "log": str(log.absolute()), "exit": str(exit_file.absolute()),
              "consumed": 0, "readerPid": os.getpid(), "readerStarted": process_started(os.getpid()),
              "startedAt": _now_iso()}
    save_record(controller, record)
    return RunnerProcess(record, popen)


def note_reader(controller, process):
    """Record that this server now reads ``process``'s log (after it adopted it)."""

    record = dict(process.record)
    record.update(readerPid=os.getpid(), readerStarted=process_started(os.getpid()),
                  consumed=process.stdout.handled)
    process.record = record
    save_record(controller, record)


def save_offset(controller, process):
    """Record how far the server has handled ``process``'s log; return whether it changed."""

    handled = process.stdout.handled
    if process.record.get("consumed") == handled:
        return False
    record = current_record(controller)
    if record is None or record.get("launch") != process.launch:
        return False
    record["consumed"] = handled
    process.record = {**process.record, "consumed": handled}
    save_record(controller, record)
    return True


def release_reader(controller, process):
    """A closing server: record how far it read and that it no longer reads ``process``'s log."""

    record = current_record(controller)
    if record is None or record.get("launch") != process.launch:
        return
    record.update(consumed=process.stdout.handled, readerPid=None, readerStarted=None, detachedAt=_now_iso())
    save_record(controller, record)


if __name__ == "__main__":  # pragma: no cover - a manual check of the process identity on this computer.
    print(process_started(os.getpid()), file=sys.stderr)
