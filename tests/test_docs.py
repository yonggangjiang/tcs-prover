"""The one-screen README and its guide pages (docs/): the README links every guide, every relative link and
anchor resolves, and the quick start names what a stock Mac needs. Reads files only; no model, no server."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"
FENCE = re.compile(r"(?ms)^```.*?^```")


def anchors(text):
    """The heading anchors GitHub makes for a Markdown text (code blocks left out)."""

    found, seen = set(), {}
    for line in FENCE.sub("", text).splitlines():
        match = re.match(r"#{1,6}\s+(.+?)\s*$", line)
        if not match:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1)).replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(f"{slug}-{count}" if count else slug)
    return found


class GuideTests(unittest.TestCase):
    def test_the_readme_is_one_screen_and_links_every_guide(self):
        text = README.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 120)
        guides = sorted(DOCS.glob("*.md"))
        self.assertGreaterEqual(len(guides), 10)
        for guide in guides:
            self.assertIn(f"](docs/{guide.name})", text, guide.name)

    def test_every_relative_link_and_anchor_resolves(self):
        for page in (README, *sorted(DOCS.glob("*.md"))):
            for target in re.findall(r"\]\(([^)\s]+)\)", FENCE.sub("", page.read_text(encoding="utf-8"))):
                if re.match(r"[a-z]+:", target):
                    continue
                name, _, anchor = target.partition("#")
                destination = (page.parent / name).resolve() if name else page
                with self.subTest(page=page.name, link=target):
                    self.assertTrue(destination.is_file())
                    if anchor:
                        self.assertIn(anchor, anchors(destination.read_text(encoding="utf-8")))

    def test_the_quick_start_works_on_a_stock_mac(self):
        text = " ".join(README.read_text(encoding="utf-8").split())
        for needed in ("Python 3.11 or newer", "is 3.9 and does not work", "-m venv .venv",
                       ".venv/bin/python -m pip install -r requirements.txt", "python3 web_ui.py", "--no-browser",
                       "private access key", "curl -fsSL https://chatgpt.com/codex/install.sh | sh", "codex login",
                       "curl -fsSL https://claude.ai/install.sh | bash", "`latexmk` and `pdflatex`",
                       "A Setup page that does this from the browser is coming"):
            self.assertIn(needed, text)
        # web_ui.py runs in the checkout's .venv whichever Python starts it, so `python3 web_ui.py` works there.
        launcher = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertIn('environment = entry.parent / ".venv"', launcher)
        self.assertIn("os.execv(str(executable), command)", launcher)


def flat(path):
    return " ".join(path.read_text(encoding="utf-8").split())


class AccuracyTests(unittest.TestCase):
    """What the README and the guides say where the code decides it."""

    def test_the_readme_says_the_work_leaves_the_computer(self):
        # Only the Codex CLI and Claude Code run here; the model calls go to the provider or the API endpoint.
        text = flat(README)
        self.assertNotIn("models run on your computer", text)
        self.assertIn("The models run through the Codex CLI or Claude Code on your computer; your statement and the "
                      "work are sent to OpenAI, Anthropic or the API endpoint you set up.", text)

    def test_a_web_cutoff_run_that_writes_latex_names_mactex_where_its_checks_are_described(self):
        # Its isolated compiler is checked before launch whenever Writing & polishing is on, with MacTeX's own
        # folders and sandbox-exec, never PATH.
        server = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(server.count("retrieval.preflight_compiler("), 3)
        compiler = (ROOT / "retrieval" / "isolated_compiler.py").read_text(encoding="utf-8")
        for path in ("/usr/local/texlive", "/Library/TeX/texbin", "/usr/bin/sandbox-exec"):
            self.assertIn(path, compiler)
        self.assertIn("with **Writing & polishing** on also MacTeX (in `/usr/local/texlive`), before they start.",
                      flat(README))
        self.assertIn("with **Writing & polishing** on also MacTeX in `/usr/local/texlive` and macOS's `sandbox-exec` "
                      "for its isolated LaTeX compiler", flat(DOCS / "models.md"))
        cutoff = flat(DOCS / "web-cutoff.md")
        checks = cutoff[cutoff.index("## What a timestamp run checks"):cutoff.index("## What else is recorded")]
        for needed in ("`/usr/local/texlive`", "`/Library/TeX/texbin`", "`sandbox-exec`",
                       '"Historical compilation requires MacTeX and sandbox-exec"', "**Re-run preflight**"):
            self.assertIn(needed, checks)
        self.assertIn("compilation requires MacTeX and sandbox-exec", compiler)

    def test_python_commands_name_the_checkouts_venv(self):
        # A stock Mac's python3 is 3.9 (no tomllib) and it has no `python`; only web_ui.py switches to the .venv
        # by itself (and the transcript viewer needs nothing newer).
        bare = re.compile(r"(?<![\w/.\\])python3? (?!web_ui\.py|transcript/)")
        for name in ("developers.md", "web-cutoff.md", "terminal.md", "troubleshooting.md"):
            with self.subTest(page=name):
                page = (DOCS / name).read_text(encoding="utf-8")
                code = " ".join([*FENCE.findall(page), *re.findall(r"`[^`]+`", FENCE.sub("", page))])
                self.assertIn("python", code)
                self.assertEqual(bare.findall(code), [])
        self.assertIn("`.venv/bin/python` (`.venv\\Scripts\\python` on Windows)", flat(DOCS / "developers.md"))

    def test_saved_settings_say_which_sandboxes_can_read_them(self):
        # On Linux and Windows the Codex roles keep Codex's legacy sandboxes, which have no deny list.
        for path in (DOCS / "saved-settings.md", ROOT / "ui" / "presets.py"):
            with self.subTest(path=path.name):
                text = flat(path)
                self.assertNotIn("hidden from every model sandbox", text)
                self.assertIn("no model sandbox can write", text)
                self.assertRegex(text, r"Linux and Windows,? (?:a Codex role|can read it)|Codex's legacy sandboxes "
                                       r"on Linux and Windows can read it")
        self.assertIn("(see [Sandbox and network](sandbox.md#linux-and-windows))", flat(DOCS / "saved-settings.md"))


if __name__ == "__main__":
    unittest.main()
