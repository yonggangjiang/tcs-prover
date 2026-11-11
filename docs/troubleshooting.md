# Troubleshooting

## Starting TCS Prover

**"TCS Prover requires Python 3.11 or newer".** There is no `.venv` in this
checkout yet, or it was made with an older Python (then delete it). Create it
with Python 3.11 or newer as in the [Quick start](../README.md#quick-start).
`web_ui.py` then always uses this checkout's `.venv`, even when `python3` is an
older system or Conda interpreter. Each checkout needs its own `.venv`: another
worktree or clone does not bring one along.

**The page says "Open TCS Prover from its launch URL."** The address lacks the
access key that follows `#` in the printed link. Run `python3 web_ui.py` again:
while TCS Prover runs, this opens the right link instead of starting a second
copy (`--no-browser` prints it). The key is kept in `.tcs-prover/sessions/`, so
tabs left open keep working after a restart on the same port.

**"TCS Prover is already running at …".** One server serves a runs folder, and
starting a second one opens the first one's page instead. When
`.experiment-control/current-run.json` selects a batch, the same command opens
that batch's running dashboard (see
[Developers and operators](developers.md#the-current-run-selection)).

**Another program uses port 8765.** TCS Prover takes a free port and prints its
link.

**"Cannot start TCS Prover: …" naming a workflow file and line.** A file in
`workflows/` cannot be read and there is no earlier readable version. Fix the
file. Once TCS Prover has read a version, a later broken edit leaves that
version in use, with a warning on the job page, until the file is fixed.

**A warning about the private state folder.** TCS Prover checks `.tcs-prover/`
at startup and names a folder it cannot use; Claude models and API models cannot
start until it is fixed. It must be a folder you own (or a symbolic link to
one).

**Stopping.** Ctrl-C, closing the terminal, logging out and `kill` (SIGTERM)
close the server gracefully: working authors pause, other running steps are
interrupted, and both continue by themselves about a minute after the next
start. A second Ctrl-C stops everything at once.

## Models and programs

**A job says a program is missing** ("Codex CLI missing or incompatible",
"Claude Code is not installed or is not on PATH", or *Claude Code not installed
or not working* under **Needs your attention**). Install it (see the
[README](../README.md#what-you-need-for-models)). The job retries every minute
and continues by itself; nothing needs restarting.

**"Codex login needed" or "Claude login needed".** Run `codex login`, or
`claude` and then `/login`. The job continues by itself. Wait for **Paused**
before changing a login while an author works.

**A usage limit.** The job retries every minute and continues once the limit
resets. To use fewer at once, set a [running-jobs limit](batches.md#running-jobs).

**An API connection fails.**
- **401 · Missing API key**: the format, base URL and key do not match what the
  service expects.
- **Request rejected by the API endpoint** (HTTP 400): the endpoint refuses run
  requests as invalid. Edit the connection; an error-paused job resumes by
  itself once its connection's settings change.
- OpenAI-format models need the native Codex binary on `PATH`: a `codex`
  launcher that replaces `CODEX_HOME` cannot run them.
- Anthropic-format models run in bare mode (the activity log says why) when your
  `claude` launcher drops `CLAUDE_CONFIG_DIR`.
- Some endpoints refuse the hosted web search tool of live runs.
- See [Models and API connections](models.md#api-connections).

**Model unavailable.** The model does not exist or your account cannot use it.
The job pauses, is listed under **Needs your attention**, and waits for
**Resume** (for an API model it resumes by itself once the connection's settings
change).

**A Claude job shows "Conversation replaced".** Claude Code deleted the saved
conversation and no private copy could restore it. To keep conversations longer,
set `"cleanupPeriodDays": 365` in `~/.claude/settings.json`.

## Jobs

**No PDF.** The job waits at **Compile PDF** with *Waiting to retry a command
that could not run*: install LaTeX (`latexmk` and `pdflatex`); the job checks
every minute and continues. A job that ends with "PDF not compiled" has its
LaTeX source in `final.tex` and says why the compile failed.

**Continue is missing on a job stopped at its time limit.** Less than a minute
of its limit is left. Open the job and raise the limit (or set 0 for none).

**Resume is greyed out.** The paused author is writing a reply in the chat; wait
for it, or click **Stop reply**.

**Send is greyed out in the chat.** Another step works, or the job is pausing or
stopping; the card says when the author works again. What you typed is kept.

**A job shows as running elsewhere.** Another TCS Prover process owns it (a
terminal run, or a runner a crashed server left). This server takes it over when
that process ends.

**A job is listed with a warning about a damaged checkpoint or settings.** Its
checkpoint is rebuilt from the transcript, and unusable settings fall back to the
defaults.

**A web-cutoff job is held.** It names the cause. Fix it and click **Re-run
preflight**. See [Web search cutoff](web-cutoff.md#what-a-timestamp-run-checks).

**A folder run starts too many jobs.** Set **Models & API → Running jobs**; the
rest wait in a queue.

## Files and settings

**Batches cannot be made, renamed or deleted, and the Overview says why.**
`runs/.controller/batches.json` cannot be read. Your batches' problems show in
the Default batch meanwhile. Repair the file and start TCS Prover again.

**Saved settings cannot be saved.** `.tcs-prover/new-problem-presets.json` cannot
be read, is damaged or comes from a newer version; it is never overwritten. Fix
or remove it. **Load…** and **Save…** turned off with a note to restart means
the server is older than the page: restart TCS Prover.

**The API connections list cannot be read.** It is reported instead of shown
as empty; running jobs keep the settings they read last, and waiting jobs try
again every minute. An entry listed with a reason can be repaired or removed.

**On Linux or Windows the activity log says "Run confinement is enforced on
macOS only".** That is expected: there the models can read files outside their
run folder. See [Sandbox and network](sandbox.md#linux-and-windows).

**A job was deleted by mistake.** Its run folder stays in `runs/.trash/` for 30
days, so its files can still be copied from there.
