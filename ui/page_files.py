"""The files of the page that the server (and the live gateway) send as they are: URL path -> (file in ui/, type).

Besides the page itself, its script and its styles, the Markdown and math renderer (markdown.js, markdown.css) and
the libraries it uses, kept unchanged in ui/vendor (ui/vendor/README.md lists their versions and checksums). Only the
files of ui/vendor with one of the types below are sent, so its licences and notes are not.
"""
from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parent
VENDOR = "vendor"
VENDOR_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}


def _vendor_files():
    """The files of ui/vendor that the page loads, by URL path (``/vendor/...``)."""

    found = {}
    folder = UI / VENDOR
    if not folder.is_dir():
        return found
    for path in sorted(folder.rglob("*")):
        kind = VENDOR_TYPES.get(path.suffix.lower())
        if kind and path.is_file() and not path.is_symlink():
            relative = path.relative_to(UI).as_posix()
            found["/" + relative] = (relative, kind)
    return found


PAGE_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    # Linked from index.html, so the browser's own icon request finds it.
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
    "/markdown.css": ("markdown.css", "text/css; charset=utf-8"),
    **_vendor_files(),
}
# What a saved batch's page loads besides its routes: every page file but the icon.
PAGE_ASSETS = frozenset(path for path in PAGE_FILES if path != "/favicon.svg")
