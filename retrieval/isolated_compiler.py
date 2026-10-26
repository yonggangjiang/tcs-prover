"""Compile historical-run TeX in a fresh macOS Seatbelt sandbox.

Only the built-in compiler command is supported. Neither PATH, user TeX
configuration, nor files planted alongside a model's source are trusted.
"""

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


ADAPTER = "codex-macos-seatbelt-v1"
LATEX_ARGV = [
    "latexmk", "-norc", "-pdf", "-g", "-interaction=nonstopmode",
    "-halt-on-error", "-file-line-error", "-no-shell-escape",
    "-jobname=final", "latex-source.tex",
]
ARTIFACTS = ("final.pdf", "final.log", "final.aux")
MAX_SOURCE_BYTES = 16 * 1024 * 1024


class CompilerIsolationError(RuntimeError):
    """The compiler's boundary could not be established."""


def supported():
    """Only the macOS Seatbelt adapter exists; every historical run compiles here."""
    return sys.platform == "darwin"


def _root_owned(path):
    """Reject writable toolchains, including a writable canonical parent."""
    canonical = Path(path).resolve(strict=True)
    for item in (canonical, *canonical.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CompilerIsolationError(f"Compiler toolchain is not root-owned and protected: {item}")
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
                raise CompilerIsolationError("Historical TeX tools must belong to the protected MacTeX distribution.")
        for executable in ("/usr/bin/sandbox-exec", "/usr/bin/perl", "/bin/sh"):
            _root_owned(executable)
    except OSError as exc:
        raise CompilerIsolationError(f"Historical compilation requires MacTeX and sandbox-exec: {exc}") from exc
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


def _run(profile, argv, stage, env, timeout):
    process = subprocess.Popen(
        ["/usr/bin/sandbox-exec", "-p", profile, *argv], cwd=stage, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", umask=0o077,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, output)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
        exc.stdout = output
        raise


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
            raise CompilerIsolationError(f"Compiler sandbox canaries failed (exit {result.returncode}): " + result.stdout[-3000:])


def _regular_file(path, limit=None):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise CompilerIsolationError(f"Cannot safely open compiler file {path.name}: {exc}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (limit is not None and info.st_size > limit):
            raise CompilerIsolationError(f"Compiler requires a regular, unlinked file within its size limit: {path.name}")
        data = handle.read(None if limit is None else limit + 1)
    if limit is not None and len(data) > limit:
        raise CompilerIsolationError(f"Compiler input exceeds its size limit: {path.name}")
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
    """Return the workflow command result, preserving outputs on boundary failure."""
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
            raise CompilerIsolationError(
                f"Compiler sandbox canaries did not finish within {exc.timeout:g} seconds.") from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise CompilerIsolationError(f"Compiler sandbox canaries could not run: {exc}") from exc
        (stage / "latex-source.tex").write_bytes(source)
        # Root-owned latexmk is a Perl script; invoke the system interpreter
        # explicitly and pin its engine. Disable bibliography subprocesses.
        command = ["/usr/bin/perl", str(latexmk), *LATEX_ARGV[1:-1], "-bibtex-",
                   f"-pdflatex={binary_directory}/pdflatex %O %S", LATEX_ARGV[-1]]
        code, output = None, ""
        try:
            result = _run(profile, command, stage, env, timeout)
            code, output = result.returncode, result.stdout or ""
            pdf = _regular_file(stage / "final.pdf") if (stage / "final.pdf").exists() else b""
            status = "pass" if code == 0 and pdf.startswith(b"%PDF-") else "fail"
            diagnostic = f"Isolated LaTeX command exited with code {code}."
            if code == 0 and status != "pass":
                diagnostic += " A valid PDF was not produced."
        except subprocess.TimeoutExpired as exc:
            status, diagnostic = "fail", f"Command exceeded {timeout:g} seconds."
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
        except OSError as exc:
            status, diagnostic = "unavailable", f"Isolated compiler could not run: {exc}"
        # Nothing in the run directory is exposed to TeX; the controller copies
        # only these named artifacts after the bounded child exits.
        for name in ARTIFACTS:
            artifact = stage / name
            if name == "final.pdf" and status != "pass":
                (workspace / name).unlink(missing_ok=True)
            elif artifact.exists():
                _replace(workspace / name, _regular_file(artifact))
        _replace(workspace / "latex-compile.log", (diagnostic + "\n\n" + output).encode("utf-8"))
        return {"status": status, "diagnostic": diagnostic, "returncode": code, "output": output[-24000:]}


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
