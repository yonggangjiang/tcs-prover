#!/usr/bin/env python3
"""Launch the TCS Prover browser interface or Markdown proof jobs."""

import os
from pathlib import Path
import sys


def main():
    """Use this checkout's environment before importing Python 3.11 modules."""
    entry = Path(__file__).resolve()
    environment = entry.parent / ".venv"
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if executable.is_file() and Path(sys.prefix).resolve() != environment.resolve():
        # Keep the virtualenv path intact: resolving its Python symlink would
        # select the base interpreter and lose the installed dependencies.
        flags = ["-B"] if sys.dont_write_bytecode else []
        try:
            os.execv(str(executable), [str(executable), *flags, str(entry), *sys.argv[1:]])
        except OSError as exc:
            print(f"Cannot start the project Python environment: {exc}", file=sys.stderr)
            return 1
    if sys.version_info < (3, 11):
        print(
            f"TCS Prover requires Python 3.11 or newer; this interpreter is "
            f"{sys.version_info.major}.{sys.version_info.minor}.\n"
            "Create the project environment with:\n"
            "  python3.11 -m venv .venv\n"
            "  .venv/bin/python -m pip install -r requirements.txt\n"
            "Then run: python web_ui.py",
            file=sys.stderr,
        )
        return 1
    from ui.cli import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
