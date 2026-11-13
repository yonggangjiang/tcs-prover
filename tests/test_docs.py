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
                       # Setup first (owner decision B3), the terminal commands kept as the alternative.
                       "Open **Setup** in the sidebar (first under Configure).",
                       "Set up the command-line programs in a terminal instead", "claude auth login"):
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

    def test_a_web_cutoff_job_held_for_its_compiler_is_described_as_checked_every_minute(self):
        # Owner decision: such a job is checked again every minute and starts or continues by itself once the
        # check passes. The guides name it as Needs your attention does (App.attention), quote what its hold
        # advises for each cause (isolated_compiler.ADVICE), and never send it to Re-run preflight. Checked
        # paragraph by paragraph: "every minute" is in these pages for other checks too.
        from retrieval import isolated_compiler

        def paragraph(name):
            parts = [" ".join(part.split()) for part in re.split(r"\n\s*\n", (DOCS / name).read_text(encoding="utf-8"))
                     if "*LaTeX compiler not ready*" in part]
            self.assertEqual(len(parts), 1, name)
            return parts[0]

        server = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"title": "LaTeX compiler not ready"', server)
        cutoff, trouble, retries = paragraph("web-cutoff.md"), paragraph("troubleshooting.md"), paragraph("retries.md")
        self.assertIn("The check runs again every minute, and the job starts or continues by itself once it passes.",
                      cutoff)
        self.assertIn("That check, like one that could not finish, runs again every minute, and the job starts or "
                      "continues by itself once it passes (a job you stopped only has its hold released and stays "
                      "stopped until you choose **Continue**).", trouble)
        self.assertIn("The check runs again every minute, also while the running-jobs limit is reached, and the job "
                      "starts or continues by itself once it passes; a job you stopped only has its hold released",
                      retries)
        for name, text in (("web-cutoff.md", cutoff), ("troubleshooting.md", trouble)):
            with self.subTest(page=name):
                # Only MacTeX's own failures are MacTeX's to fix; a Python under Rosetta needs a native one.
                self.assertIn(f"*{isolated_compiler.ADVICE['mactex'].rstrip('.')}", text)
                self.assertIn(f"*{isolated_compiler.ADVICE['sandbox'].rstrip('.')}", text)
                self.assertIn("A cause that mentions `rosetta error`", text)
                self.assertRegex(text, r"[Rr]ecreate `\.venv` with a native \(arm64\) Python")
        self.assertNotIn("Re-run preflight", cutoff)
        self.assertNotIn("Re-run preflight", retries)
        self.assertEqual(trouble.count("Re-run preflight"), 1)
        self.assertIn("For any other cause, fix it and click **Re-run preflight**.", trouble)
        # The checks take no running slot, and a damaged MacTeX met during a run is waited for too.
        text = flat(DOCS / "web-cutoff.md")
        self.assertIn("These checks start no model, so they run every minute whatever the running-jobs limit; after "
                      "one that ran by itself, the job starts or resumes as soon as a running slot is free", text)
        self.assertIn("After a compile that fails, it also compiles its small test document, so a MacTeX that is "
                      "being updated or damaged is never taken for an error in the source.", text)
        self.assertIn("The every-minute checks of held web-cutoff jobs start no model and take no slot: they run "
                      "whatever the limit and the spacing, at most four starting in the same second.",
                      flat(DOCS / "batches.md"))
        self.assertIn("HOLD_CHECKS_PER_SECOND = 4\n", server)
        checks = text[text.index("## What a timestamp run checks"):text.index("## What else is recorded")]
        self.assertNotIn("install MacTeX, then choose **Re-run preflight**", checks)
        self.assertNotIn("held until you install what is missing and choose", flat(DOCS / "models.md"))

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


class CoordinatorDocsTests(unittest.TestCase):
    """What the README and the guides say about the author coordinator (Author subagents 1 or more, the default 3),
    in the page's own words where they quote it."""

    def test_the_readme_names_the_author_coordinator_in_how_a_run_works(self):
        text = flat(README)
        steps = text[text.index("## How a run works"):text.index("## Guides")]
        self.assertIn("2. **Author.** By default an author coordinator works on the proof, as long as it takes: it does "
                      "not prove the statement itself, but runs up to 3 author subagents at a time", steps)
        self.assertIn("With **Author subagents** 0, one author works on it alone.", steps)

    def test_the_workflow_guide_gives_the_default_and_every_part_of_coordinator_mode(self):
        text = flat(DOCS / "workflow.md")
        self.assertIn("| Author subagents | 3 | 0 to 10. 1 or more: an [author coordinator](#the-author-coordinator) "
                      "runs up to that many author subagents at a time. 0: one author works alone", text)
        self.assertIn("**Author subagents** (`-authorSubagents N` in the terminal), 0 to 10, default 3", text)
        self.assertNotIn("default 0", text)
        section = text[text.index("### The author coordinator"):text.index("### Handing in a proof")]
        for needed in ("per-job `coordinator` prompt", "`{author_subagents}`", "`coordinator_checkin`",
                       *(f"`coordinator_{name}`" for name in
                         ("goal", "continuation", "reopen", "compaction", "resume", "repair", "summary")),
                       "**Author subagent started**", "**Author subagent finished**", "**Author subagent failed**",
                       "`memory/`", "owner-only permissions", "never removed or overwritten", "`memory/progress.md`",
                       "[progress log](job-page.md#progress-log)", "Every hour of the job's working time",
                       "The first comes at 1 hour; a job resumed at 5 h 20 min gets its next one at 6 hours.",
                       "**Author coordinator time check**", "names no proof file", "**Author coordinator report**",
                       "GPT 5.6 Luna", "works alone with the `author` prompt instead of coordinating",
                       "runs the subagents in the foreground", "reaches a Claude coordinator when its running "
                       "subagents report"):
            with self.subTest(needed=needed):
                self.assertIn(needed, section)
        # Claude Code's own limits are set now: no guide says that only the prompt limits its subagents.
        self.assertNotIn("Claude Code has no such setting", text)
        self.assertNotIn("Claude Code has no setting for the number", flat(DOCS / "models.md"))

    def test_the_job_page_guide_describes_the_progress_log_and_the_run_settings_wording(self):
        text = flat(DOCS / "job-page.md")
        section = text[text.index("## Progress log"):text.index("## Talk to the author")]
        for needed in ("before **Talk to the author**", "`memory/progress.md`",
                       "*Written by the author coordinator in memory/progress.md · updated 4 min ago*",
                       "plain text, never run or rendered", "stays scrolled to the end as the log grows, unless you "
                       "scroll up in it", "longer than 100,000 characters",
                       "*The coordinator has not written its progress log yet.*"):
            with self.subTest(needed=needed):
                self.assertIn(needed, section)
        for needed in ("*coordinator, up to 3 author subagents at a time*", "*works alone*",
                       "*up to 2 subagents at a time*"):
            self.assertIn(needed, text)
        self.assertIn("The built-in settings use 3", flat(DOCS / "saved-settings.md"))

    def test_the_claude_tools_say_which_author_gets_the_subagent_tool(self):
        # A Claude coordinator with TCS Prover's team runs without the CLI's subagent tool (_execute removes the
        # multi_agent feature, so claude_cli.tool_permissions() offers no Task) and with TCS Prover's server; only an
        # author whose subagents are Claude Code's own gets Task.
        text = flat(DOCS / "models.md")
        tools = text[text.index("**Tools.**"):text.index("If Claude Code offers a tool beyond the list")]
        self.assertIn("an author coordinator with [TCS Prover's team](workflow.md#the-authors-team) gets only TCS "
                      "Prover's own", tools)
        self.assertIn("whose subagents are Claude Code's own (a coordinator of a job from before TCS Prover's team, or "
                      "of a rerun that keeps them, and the author of a job from before coordinator mode), get the "
                      "subagent tool (Task)", tools)
        self.assertIn("A coordinator with TCS Prover's team, as every new one is, gets no subagent tool", tools)
        self.assertNotIn("(an author coordinator, or the author of a job from before coordinator mode)", tools)

    def test_the_words_the_guides_quote_are_the_pages(self):
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn(">The coordinator has not written its progress log yet.</p>", page)
        self.assertIn('"Written by the author coordinator in memory/progress.md"', app)
        self.assertIn("` · coordinator, up to ${state.authorSubagents} author subagent", app)
        self.assertIn('" · works alone"', app)
        # "Author subagent started", or with TCS Prover's team "Author subagent 2 (Claude Opus 5.5) started".
        self.assertIn('[member ? `Author subagent ${member}` : "Author subagent", model ? `(${model})` : ""]', app)
        self.assertIn("label: `${who} ${status}`", app)
        for label in ("Author coordinator time check", "Author coordinator report"):
            self.assertIn(label, app)


if __name__ == "__main__":
    unittest.main()
