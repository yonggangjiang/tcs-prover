"""Bounded local compilation of a final LaTeX document with file restrictions."""

import os
import math
from pathlib import Path
import shutil
import subprocess

from research_journal import _atomic_text


def _output_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def verify_latex(source, directory, timeout=30):
    """Save the exact source and return pass/fail/unavailable plus diagnostics.

    A compiler's absence is explicit and does not invalidate the separately
    reviewed mathematical content. Storage errors propagate to the controller.
    Compiler failures and timeouts retain full captured output in compile.log.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("LaTeX source must be a nonempty string")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Compilation timeout must be positive")
    workdir = Path(directory).resolve() / "final-validation"
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    workdir.chmod(0o700)
    _atomic_text(workdir / "main.tex", source)
    # A previous successful PDF must not make a later no-output run look valid.
    (workdir / "main.pdf").unlink(missing_ok=True)
    engine = shutil.which("pdflatex")
    if engine is None:
        diagnostic = "LaTeX compilation unavailable: pdflatex is not installed. The document was saved but has not been compiled."
        _atomic_text(workdir / "compile.log", diagnostic + "\n")
        return {"status": "unavailable", "diagnostic": diagnostic, "engine": None}
    command = [engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "main.tex"]
    environment = dict(os.environ)
    environment.update({"openout_any": "p", "openin_any": "p", "shell_escape": "f", "TEXMFOUTPUT": ""})
    try:
        result = subprocess.run(command, cwd=str(workdir), env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False,
                                **({"umask": 0o077} if os.name != "nt" else {}))
        output = _output_text(result.stdout)
        pdf = workdir / "main.pdf"
        if result.returncode == 0 and pdf.is_file() and pdf.stat().st_size > 0:
            status = "pass"
            diagnostic = "LaTeX compilation passed; final-validation/main.pdf was produced."
        else:
            status = "fail"
            diagnostic = f"LaTeX compilation failed (exit code {result.returncode}); see final-validation/compile.log."
            if result.returncode == 0:
                diagnostic = "LaTeX compiler returned success but produced no nonempty PDF; see final-validation/compile.log."
    except subprocess.TimeoutExpired as exc:
        status = "fail"
        diagnostic = f"LaTeX compilation exceeded {timeout:g} seconds; see final-validation/compile.log."
        output = _output_text(exc.stdout)
        if exc.stderr:
            output += "\n" + _output_text(exc.stderr)
    except OSError as exc:
        status = "fail"
        diagnostic = f"LaTeX compiler could not run: {exc}"
        output = ""
    _atomic_text(workdir / "compile.log", diagnostic + "\n\n" + output)
    return {"status": status, "diagnostic": diagnostic, "engine": engine}
