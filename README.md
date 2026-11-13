# TCS Prover

TCS Prover is a local web app for proofs in theoretical computer science. You
paste a statement; an AI author works on it for as long as it takes, independent
critics check every proof it hands in, and you get a LaTeX paper and a PDF. The
models run through the Codex CLI or Claude Code on your computer; your statement
and the work are sent to OpenAI, Anthropic or the API endpoint you set up.

## Quick start

TCS Prover needs Git and Python 3.11 or newer. The `python3` that comes with
macOS is 3.9 and does not work (check with `python3 --version`): install a
current Python from [python.org](https://www.python.org/downloads/) or with
`brew install python@3.13`. On a new Mac, the first `git` command offers to
install Apple's command line tools; accept. Then:

```bash
git clone https://github.com/yonggangjiang/tcs-prover.git
cd tcs-prover
python3.13 -m venv .venv              # any Python 3.11 or newer
.venv/bin/python -m pip install -r requirements.txt
python3 web_ui.py                     # always runs in this checkout's .venv
```

On Windows: `py -3 -m venv .venv`, `.venv\Scripts\python -m pip install -r requirements.txt`, `py web_ui.py`.

`web_ui.py` prints a link such as `http://127.0.0.1:8765/#…` and opens it in
your browser. The part after `#` is a private access key, so the bare address
does not work. **To open the page again**, run `python3 web_ui.py` again: while
TCS Prover runs, this opens the same link instead of starting a second copy
(`--no-browser` only prints it). Then open **Setup** (below), click **New
problem**, paste your statement and click **Start workflow**. Ctrl-C stops TCS
Prover; working authors pause and continue by themselves about a minute after
the next start.

## What you need for models

Open **Setup** in the sidebar (first under Configure). It shows whether the
Codex CLI (used by the default GPT models), Claude Code and LaTeX are installed
and signed in, and which of your roles and jobs need each one, and it installs,
updates or signs in the one you need after you confirm the exact command (see
[the Setup page](docs/models.md#the-setup-page)).

<details>
<summary>Set up the command-line programs in a terminal instead</summary>

```bash
# Codex CLI: the built-in GPT models (the default) and OpenAI-format API keys
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex login        # sign in with ChatGPT (on another computer: codex login --device-auth)

# Claude Code: Claude models and Anthropic-format API keys
curl -fsSL https://claude.ai/install.sh | bash
claude auth login  # sign in once with your Claude account
```

On Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"`
and `irm https://claude.ai/install.ps1 | iex`.

</details>

- **Built-in models** need the program installed and signed in. **API
  connections** (your own key, saved under **Models & API**) need the program
  for their format installed (Setup installs it too), with no sign-in.
- **LaTeX** makes the PDF (`latexmk` and `pdflatex`): [MacTeX](https://tug.org/mactex/)
  or `brew install --cask mactex-no-gui` on macOS, [TeX Live](https://tug.org/texlive/)
  or MiKTeX elsewhere. Setup shows the command for your system; you run it.
- Nothing here stops a start: a job whose program or LaTeX is missing says so,
  tries again every minute, and continues by itself once you install it. Runs
  with a web cutoff timestamp are the exception: they need the native Codex CLI,
  and with **Writing & polishing** on also MacTeX (in `/usr/local/texlive`),
  before they start.

## How a run works

1. **Statement review.** A model removes ambiguities and loopholes from your
   statement; you approve the result (**Approve and start**) or send feedback.
2. **Author.** By default an author coordinator works on the proof, as long as
   it takes: it does not prove the statement itself, but runs up to 3 author
   subagents at a time, keeps the team's memory and a progress log (shown on the
   job page), checks that progress keeps growing, and hands in a
   complete proof. With **Author subagents** 0, one author works on it alone.
   Each subagent can run a model of its own (Codex CLI, Claude Code or an API
   key), and TCS Prover runs the author's turns itself; **Edit prompts** shows
   every text it sends, editable for the next job.
3. **Critic.** A critic starts independent critics (3 by default) that hunt for
   bugs, fixes what it can, and sends critical errors back to the author.
4. **Convention critic.** Checks once that the accepted proof does what you most
   likely meant, and warns if it exploits a loophole.
5. **Writing.** A LaTeX writer turns the proof into a paper and compiles the PDF.

Each stage can be turned off. On the job page, **Talk to the author** sends the
author messages and shows its answers; **Pause** and **Resume** hold and
continue it; **Summarize progress** asks for a progress summary (pausing a
working author first); **Stop** stops the job the way the time limit does, and
**Continue** picks it up again in the same job. The time limit (none by default)
stops the author and has it write a progress summary. Failed model calls are
retried every minute, without limit.

## Guides

| Guide | What it covers |
| --- | --- |
| [Models and API connections](docs/models.md) | The Setup page, Codex CLI, Claude Code, API keys, which program each model needs |
| [The workflow](docs/workflow.md) | Statement review, the author and the author coordinator, the critic, the convention critic, LaTeX |
| [The job page](docs/job-page.md) | Status, the progress log, the chat, Pause, Summary, Stop, the time limit, related works, downloads |
| [Batches and folder runs](docs/batches.md) | Batches, related works shared with every problem in a batch, running a folder of statements, the running-jobs limit |
| [Saved settings](docs/saved-settings.md) | Saving and loading New problem settings, and the default, under **Advanced settings** |
| [Web search cutoff](docs/web-cutoff.md) | Runs with no web access, or with archives from before a date only |
| [Sandbox and network](docs/sandbox.md) | What a run's models can read, write and reach |
| [Retries and what needs you](docs/retries.md) | Automatic retries, restarts, checkpoints, continuing a job |
| [Terminal runs](docs/terminal.md) | Proofs without a browser, every option, the transcript viewer |
| [Troubleshooting](docs/troubleshooting.md) | Common problems and what to do |
| [Developers and operators](docs/developers.md) | Project layout, workflow files, tests, the live gateway |
