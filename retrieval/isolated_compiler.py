"""Compile historical-run TeX in a fresh macOS Seatbelt sandbox.

Only the built-in compiler command is supported. Neither PATH, user TeX
configuration, nor files planted alongside a model's source are trusted.
"""

from collections import deque
import errno
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time


ADAPTER = "codex-macos-seatbelt-v1"
# Without -halt-on-error one compile reports every error of the document.
LATEX_ARGV = [
    "latexmk", "-norc", "-pdf", "-g", "-interaction=nonstopmode",
    "-file-line-error", "-no-shell-escape",
    "-jobname=final", "latex-source.tex",
]
ARTIFACTS = ("final.pdf", "final.log", "final.aux")
MAX_SOURCE_BYTES = 16 * 1024 * 1024
# Model-written TeX can print without end: only the head and the tail of the
# output (each OUTPUT_KEEP characters) are kept, the copies of final.log and
# final.aux keep ARTIFACT_KEEP bytes, and a compile whose output or final.log
# grows past OUTPUT_LIMIT is stopped like one that timed out.
OUTPUT_KEEP = 1_000_000
OUTPUT_LIMIT = 256 * 1024 * 1024
ARTIFACT_KEEP = 8 * 1024 * 1024
# Opening a file can fail for want of resources; that passes, unlike a refusal.
_RESOURCE_ERRNOS = {errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN, errno.EINTR, errno.EIO}


class CompilerIsolationError(RuntimeError):
    """The compiler's boundary could not be established."""


class CompilerNotReady(CompilerIsolationError):
    """The protected toolchain or the sandbox failed its checks; they can pass later, as during a TeX update."""


class CompilerRefusal(CompilerIsolationError):
    """The source, or a file TeX produced, is not one the boundary accepts; nothing of it is used."""


def supported():
    """Only the macOS Seatbelt adapter exists; every historical run compiles here."""
    return sys.platform == "darwin"


def _root_owned(path):
    """Reject writable toolchains, including a writable canonical parent."""
    canonical = Path(path).resolve(strict=True)
    for item in (canonical, *canonical.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CompilerNotReady(f"Compiler toolchain is not root-owned and protected: {item}")
    return canonical


def toolchain():
    """Use the administrator-installed MacTeX distribution, never PATH."""
    try:
        root = _root_owned("/usr/local/texlive")
        binary_directory = _root_owned("/Library/TeX/texbin")
        latexmk = _root_owned(binary_directory / "latexmk")
        engine = _root_owned(binary_directory / "pdflatex")
        for path in (latexmk, engine):
            if root not in path.parents or not path.is_file():
                raise CompilerNotReady("Historical TeX tools must belong to the protected MacTeX distribution.")
        for executable in ("/usr/bin/sandbox-exec", "/usr/bin/perl", "/bin/sh"):
            _root_owned(executable)
    except OSError as exc:
        raise CompilerNotReady(f"Historical compilation requires MacTeX and sandbox-exec: {exc}") from exc
    # Retain the pdflatex basename: resolving that symlink to pdftex changes
    # TeX's implicit format selection.
    return root, binary_directory, latexmk


def sandbox_profile(stage, tex_root):
    reads = ("/System", "/usr/lib", "/usr/share", "/usr/bin", "/bin", "/Library/Perl", str(tex_root))
    return "\n".join([
        "(version 1)", "(deny default)", "(allow process*)", "(allow sysctl-read)",
        "(allow file-read-metadata)",
        "(allow file-read* (literal \"/\"))",
        "(allow file-read* " + " ".join(f"(subpath {json.dumps(path)})" for path in reads) + ")",
        "(allow file-read* file-write* (literal \"/dev/null\"))",
        "(allow file-read* (literal \"/dev/urandom\") (literal \"/dev/random\"))",
        f"(allow file-read* file-write* (subpath {json.dumps(str(stage))}))",
    ]) + "\n"


def environment(stage, binary_directory):
    """Exclude credentials, user rc files, package trees, and injected flags."""
    return {
        "PATH": f"{binary_directory}:/usr/bin:/bin", "HOME": str(stage),
        "TMPDIR": str(stage), "TMP": str(stage), "TEMP": str(stage),
        "LANG": "C", "LC_ALL": "C", "TZ": "UTC",
        "TEXMFHOME": str(stage / "texmf"), "TEXMFCONFIG": str(stage / "texmf-config"),
        "TEXMFVAR": str(stage / "texmf-var"), "TEXMFCACHE": str(stage / "texmf-var"),
        "openin_any": "p", "openout_any": "p", "shell_escape": "f",
        "MKTEXPK": "0", "MKTEXTFM": "0", "MKTEXMF": "0", "MKTEXFMT": "0",
    }


class _Output:
    """A command's output as it arrives: the head and the tail are kept, the middle only counted."""

    def __init__(self, keep=OUTPUT_KEEP):
        self.keep, self.size, self.omitted = keep, 0, 0
        self.head, self.tail, self.tail_size = [], deque(), 0
        self.lock = threading.Lock()

    def read(self, descriptor):
        try:
            with open(descriptor, encoding="utf-8", errors="replace") as stream:
                for line in iter(lambda: stream.readline(65536), ""):
                    self.add(line)
        except (OSError, ValueError):
            pass  # The pipe closed under the reader; what arrived is kept.

    def add(self, text):
        with self.lock:
            room = self.keep - self.size
            self.size += len(text)
            if room > 0:
                self.head.append(text[:room])
                text = text[room:]
            if text:
                self.tail.append(text)
                self.tail_size += len(text)
                while self.tail_size - len(self.tail[0]) >= self.keep:
                    self.tail_size -= len(self.tail[0])
                    self.omitted += len(self.tail.popleft())

    def text(self):
        with self.lock:
            tail = "".join(self.tail)
            cut = max(0, len(tail) - self.keep)
            omitted = self.omitted + cut
            return "".join(self.head) + (f"\n[{omitted} characters omitted]\n" if omitted else "") + tail[cut:]


def _size(path):
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def _stop_session(process):
    """Kill the compile's whole session, which it started with start_new_session."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Gone already: macOS reports EPERM for a session whose members exited
        # but are not reaped yet, as when a compile ends just as it times out.
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


def _run(profile, argv, stage, env, timeout, watch=()):
    """Run one sandboxed command; a timeout, Stop or any other interruption kills its whole session.

    Only the head and tail of its output are kept (_Output). A command whose
    output, or one of the `watch` files, grows past OUTPUT_LIMIT is stopped
    like one that timed out: TimeoutExpired with `runaway` saying why.
    """
    process = subprocess.Popen(
        ["/usr/bin/sandbox-exec", "-p", profile, *argv], cwd=stage, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        umask=0o077, start_new_session=True,
    )
    output, reader = _Output(), None
    try:
        # The reader owns a duplicate: closing the pipe here never waits for it.
        reader = threading.Thread(target=output.read, args=(os.dup(process.stdout.fileno()),), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        while reader.is_alive():
            reader.join(min(1.0, max(0.0, deadline - time.monotonic())))
            runaway = (f"printed more than {OUTPUT_LIMIT // 2 ** 20} MB of output" if output.size > OUTPUT_LIMIT
                       else next((f"wrote more than {OUTPUT_LIMIT // 2 ** 20} MB to {path.name}" for path in watch
                                  if _size(path) > OUTPUT_LIMIT), ""))
            if runaway or (reader.is_alive() and time.monotonic() >= deadline):
                expired = subprocess.TimeoutExpired(argv, timeout)
                expired.runaway = runaway
                raise expired
        try:
            process.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(argv, timeout) from None
        return subprocess.CompletedProcess(argv, process.returncode, output.text())
    except BaseException as exc:
        _stop_session(process)
        if reader is not None:
            reader.join(5)  # Bounded: a process outside the session could still hold the pipe.
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            pass
        if isinstance(exc, subprocess.TimeoutExpired):
            exc.stdout = output.text()
        raise
    finally:
        process.stdout.close()


def verify_sandbox(profile, stage, env):
    """Check the actual compiler sandbox before submitting model-produced TeX."""
    with tempfile.TemporaryDirectory(prefix="tcs-compiler-boundary-") as folder, socket.socket() as listener:
        outside = Path(folder).resolve()
        secret = outside / "private.txt"
        secret.write_text("compiler must not read this", encoding="utf-8")
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        script = r'''
use strict;
use warnings;
use Socket;
my ($private, $outside, $port) = @ARGV;
open(my $inside, '>', 'sandbox-canary') or die "stage write denied";
print $inside "ok"; close($inside);
open(my $read_inside, '<', 'sandbox-canary') or die "stage read denied";
close($read_inside);
if (open(my $read_outside, '<', $private)) { die "outside read allowed"; }
if (open(my $write_outside, '>', $outside)) { die "outside write allowed"; }
for my $target (['127.0.0.1', $port], ['1.1.1.1', 443]) {
    if (socket(my $sock, PF_INET, SOCK_STREAM, getprotobyname('tcp') || 6)) {
        my $connected = connect($sock, sockaddr_in($target->[1], inet_aton($target->[0])));
        die "network allowed" if $connected;
        die "network denied for an unverified reason: $!" unless $!{EPERM} || $!{EACCES};
        close($sock);
    } else {
        die "socket failed for an unverified reason: $!" unless $!{EPERM} || $!{EACCES};
    }
}
print "TCS_COMPILER_ISOLATED\n";
'''
        result = _run(profile, ["/usr/bin/perl", "-e", script, str(secret), str(outside / "write.txt"), str(port)], stage, env, 15)
        if result.returncode != 0 or result.stdout.strip() != "TCS_COMPILER_ISOLATED" or (outside / "write.txt").exists():
            raise CompilerNotReady(f"Compiler sandbox canaries failed (exit {result.returncode}): " + result.stdout[-3000:])


def _regular_file(path, limit=None, keep=None):
    """Read a regular, unlinked file; a larger one than `limit` is refused, of one larger than `keep` only its ends."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in _RESOURCE_ERRNOS:
            raise
        raise CompilerRefusal(f"Cannot safely open compiler file {path.name}: {exc}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (limit is not None and info.st_size > limit):
            raise CompilerRefusal(f"Compiler requires a regular, unlinked file within its size limit: {path.name}")
        if keep is not None and info.st_size > keep:
            head = handle.read(keep // 2)
            handle.seek(-(keep // 2), os.SEEK_END)
            tail = handle.read(keep // 2)
            return head + f"\n[{info.st_size - len(head) - len(tail)} bytes omitted]\n".encode() + tail
        data = handle.read(None if limit is None else limit + 1)
    if limit is not None and len(data) > limit:
        raise CompilerRefusal(f"Compiler input exceeds its size limit: {path.name}")
    return data


def _replace(path, data):
    """Replace directory entries without following an existing output symlink."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".compile-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def compile_command(policy, config, argv, directory):
    """Return the workflow command result, preserving outputs on boundary failure.

    CompilerNotReady (the toolchain or the sandbox fails its checks, which can
    pass later) and CompilerRefusal (a source or produced file the boundary
    does not accept) leave the run's files as they are. The result's output is
    the head and tail of the compiler output; the controller writes the log.
    """
    if not supported() or policy["isolation"]["adapter"] != ADAPTER:
        raise CompilerIsolationError("An isolated compiler/command adapter has not been verified for this platform.")
    workspace = Path(policy["isolation"]["workspace"]).resolve()
    if (directory != workspace or argv != LATEX_ARGV or config.get("requires") != ["pdflatex"]
            or config.get("env", {}) != {"openin_any": "p", "openout_any": "p"}
            or config.get("produces") != ["final.pdf"]
            or config.get("log") != "latex-compile.log"):
        raise CompilerIsolationError("The isolated compiler/command adapter supports only the built-in LaTeX command.")
    timeout = config.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise CompilerIsolationError("The isolated compiler requires a finite positive timeout.")
    tex_root, binary_directory, latexmk = toolchain()
    source = _regular_file(workspace / "latex-source.tex", MAX_SOURCE_BYTES)
    with tempfile.TemporaryDirectory(prefix="tcs-isolated-tex-") as folder:
        stage = Path(folder).resolve()
        profile, env = sandbox_profile(stage, tex_root), environment(stage, binary_directory)
        try:
            verify_sandbox(profile, stage, env)
        except subprocess.TimeoutExpired as exc:
            # Its command line holds the whole profile and script: keep the message short.
            raise CompilerNotReady(
                f"Compiler sandbox canaries did not finish within {exc.timeout:g} seconds.") from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise CompilerNotReady(f"Compiler sandbox canaries could not run: {exc}") from exc
        (stage / "latex-source.tex").write_bytes(source)
        # Root-owned latexmk is a Perl script; invoke the system interpreter
        # explicitly and pin its engine. Disable bibliography subprocesses.
        command = ["/usr/bin/perl", str(latexmk), *LATEX_ARGV[1:-1], "-bibtex-",
                   f"-pdflatex={binary_directory}/pdflatex %O %S", LATEX_ARGV[-1]]
        code, output, details = None, "", {}
        try:
            result = _run(profile, command, stage, env, timeout, watch=(stage / "final.log",))
            code, output = result.returncode, result.stdout or ""
            pdf = _regular_file(stage / "final.pdf") if (stage / "final.pdf").exists() else b""
            status = "pass" if code == 0 and pdf.startswith(b"%PDF-") else "fail"
            diagnostic = f"Isolated LaTeX command exited with code {code}."
            if code == 0 and status != "pass":
                diagnostic += " A valid PDF was not produced."
        except subprocess.TimeoutExpired as exc:
            status, details = "fail", {"timed_out": True}
            runaway = getattr(exc, "runaway", "")
            diagnostic = f"Command {runaway} and was stopped." if runaway else f"Command exceeded {timeout:g} seconds."
            if runaway:
                details["runaway"] = True
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
        except OSError as exc:
            # The tools passed their checks just now: this start failure is for
            # want of processes, files or memory, which passes (I-022).
            status, diagnostic = "unavailable", f"Isolated compiler could not run: {exc}"
            details = {"transient": True}
        # Nothing in the run directory is exposed to TeX; the controller copies
        # only these named artifacts after the bounded child exits.
        for name in ARTIFACTS:
            artifact = stage / name
            if name == "final.pdf" and status != "pass":
                (workspace / name).unlink(missing_ok=True)
            elif artifact.exists():
                _replace(workspace / name, _regular_file(artifact, keep=None if name == "final.pdf" else ARTIFACT_KEEP))
        return {**details, "status": status, "diagnostic": diagnostic, "returncode": code, "output": output}


def preflight(policy):
    """Exercise the real compiler without reading or changing any job source."""
    with tempfile.TemporaryDirectory(prefix="tcs-compiler-preflight-") as folder:
        workspace = Path(folder).resolve()
        (workspace / "latex-source.tex").write_text(
            r"\documentclass{article}\begin{document}"
            r"\ifnum\pdfshellescape=0\else\errmessage{Shell escape enabled}\fi "
            r"\section{Verification}\label{proof}See Section~\ref{proof}.\end{document}",
            encoding="utf-8",
        )
        temporary_policy = {**policy, "isolation": {**policy["isolation"], "workspace": str(workspace)}}
        config = {"requires": ["pdflatex"], "env": {"openin_any": "p", "openout_any": "p"},
                  "produces": ["final.pdf"], "log": "latex-compile.log", "timeout": 120}
        result = compile_command(temporary_policy, config, LATEX_ARGV, workspace)
        if result["status"] != "pass":
            raise CompilerIsolationError("Isolated compiler smoke test failed: " + result["output"][-3000:])
        log = (workspace / "final.log").read_text(encoding="utf-8", errors="replace")
        if "undefined references" in log or "Rerun to get cross-references right" in log:
            raise CompilerIsolationError("Isolated compiler did not stabilize document references.")
        return result
