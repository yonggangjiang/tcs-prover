# Terminal runs

## One statement

On a computer without a browser, put the complete statement in any UTF-8
Markdown file (paragraphs, quotes, backslashes and line breaks need no
escaping) and pass the file to `web_ui.py`:

```bash
python3 web_ui.py statement.md
```

This sends the whole file to the proof author, like turning off **Statement
review** and keeping the other three stages on. It starts no web server and
opens no browser. Without options it uses the built-in defaults (a default saved
with **Save these settings…** applies only to the web form): 3 independent
critics per round, 3 author subagents (so the author is a coordinator, see
below), at most 2 consecutive edited critic passes (then one last review that
cannot edit), no time limit, GPT 6.1 Sol at Max for every proof role, the
built-in prompts, Standard speed and concise reasoning summaries.

With 1 or more author subagents the proof author is a coordinator: it does not
prove the statement itself, but runs up to that many author subagents at a time,
keeps the team's memory in the `memory` folder of its working folder and checks
that progress keeps growing. Its progress log is
`memory/progress.md` in the job's folder under `runs/`. With `-authorSubagents 0`
the author works alone, with no subagent tool.

The author subagents are TCS Prover's team: each one runs its own model in the
job's folder (by default the author's model; `-authorSubagentModels` chooses one
for each), and the coordinator reaches them through TCS Prover's tools
(`team_assign`, `team_wait`, `team_followup`, `team_members`, `team_stop`). Each
report is also saved as `team/member-<n>/<k>-report.md`. A Codex author runs
TCS Prover's goal loop by default (`-goalLoop tcs`): TCS Prover starts every
turn and the author ends its goal with TCS Prover's `update_goal` tool;
`-goalLoop codex` uses Codex's built-in goal mode instead. Web-cutoff runs keep
Codex's goal mode and the CLI's built-in subagents.

## Folders

Passing a folder starts an independent proof for every top-level `.md` file in
it:

```bash
python3 web_ui.py statements/
python3 web_ui.py statements/ -criticRounds 6 -authorSubagents 3
```

The lookup is case-insensitive (`.md` and `.MD`), in a fixed order, and does not
enter subfolders. All files and settings are checked before any job starts. The
jobs then run at once, up to the running-jobs limit (see
[Batches](batches.md#running-jobs)), and every option applies to every file, so
a large folder can use many model jobs and credits at once. A file whose job
cannot be created or started (for example on a full disk) stops none of the
others: it is tried again every minute. Terminal runs add their jobs to the
Default batch.

## Options

Put options after the file or folder. The single-dash spelling, double-dash
camelCase and double-dash kebab-case are all accepted:

```bash
python3 web_ui.py statement.md -criticRounds 6 -thinkingHours 36
python3 web_ui.py statement.md --author-model gpt-6-astra --speed-mode standard
```

| Option | Default | Meaning |
| --- | --- | --- |
| `-criticRounds N` | `2` | Maximum consecutive edited critic passes; then one last critic round that cannot edit must pass the latest solution unchanged or reject it. `1` to `100`. An unchanged pass accepts at once. A rejection returns to the author and resets the count. |
| `-criticCount N` | `3` | Independent critics the critic starts as its subagents on each proof version before it decides: `1` to `10`. Every round, including the last one that cannot edit, starts this many fresh critics. |
| `-authorSubagents N` | `3` | Author subagents, `0` to `10`. With `1` or more the proof author is a coordinator that runs up to N author subagents at a time (see above); `0` gives it no subagent tool, and it works alone. |
| `-authorSubagentModels JSON` | the author's model | The model of each author subagent: a JSON list of exactly `-authorSubagents` entries `{"model": MODEL, "effort": LEVEL}`, with any model `-authorModel` accepts, for example `'[{"model": "gpt-6.1-sol", "effort": "max"}, {"model": "claude-opus-5-5", "effort": "high"}, {"model": "gpt-6-astra", "effort": "max"}]'`. Omitted: every author subagent runs the author's model and reasoning. Not with a web cutoff. |
| `-goalLoop LOOP` | `tcs` | Who runs a Codex author's goal loop: `tcs`, TCS Prover (it sends the editable goal continuation after every turn that did not end the goal, and the author ends it with `update_goal`), or `codex`, Codex's built-in goal mode. Claude Code authors always run TCS Prover's loop. |
| `-thinkingHours HOURS` | `0` | Time limit in hours of the job's active work: `0` (the default) for none, or any positive number. Reaching it stops the proof author; the critic and LaTeX steps after a submission always finish, so an accepted proof is never cut off. Saved work is kept. |
| `-authorModel MODEL` | `gpt-6.1-sol` | Author model: Codex CLI `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, or `gpt-6-luna`; Claude Code `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, or `claude-haiku-5-5`; or a discovered model's `api:<id>` selection from **Models & API → API connections**. The older `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` are still accepted so saved runs and scripts keep working, but the web page no longer lists them. |
| `-criticModel MODEL` | `gpt-6.1-sol` | Critic model (it also runs the convention critic); same choices as the author. |
| `-writerModel MODEL` | `gpt-6.1-sol` | LaTeX writer model; same choices as the author. |
| `-reasoningEffort LEVEL` | `max` | Fallback reasoning for all three roles: `low`, `medium`, `high`, `xhigh`, or `max`. The retired `ultra` is still accepted and runs as `max`. |
| `-authorEffort LEVEL` | shared effort | Only the author's reasoning. |
| `-criticEffort LEVEL` | shared effort | Only the critic's reasoning. |
| `-writerEffort LEVEL` | shared effort | Only the LaTeX writer's reasoning. |
| `-speedMode MODE` | `standard` | `standard`, or `fast` for ChatGPT's accelerated generation (up to 2× on 6.1 Sol and Astra, 1.5× on the other models; Fast can draw credits faster). Fast applies to Codex CLI models only; Claude Code models and API models always run at standard speed. |
| `-reasoningSummary LEVEL` | `concise` | Public activity-log summaries: `none`, `concise`, or `detailed`. This never shows private chain-of-thought. Claude Code models show Anthropic's summarized thinking for both `concise` and `detailed`; Anthropic-format API models show none. |
| `-authorPromptFile PATH` | built-in prompt | A UTF-8 author prompt; it must contain exactly one `[STATEMENT]`. It is used when the author works alone (`-authorSubagents 0`). |
| `-coordinatorPromptFile PATH` | built-in prompt | A UTF-8 prompt for the author coordinator (`-authorSubagents` 1 or more): it must contain exactly one `[STATEMENT]`, and `{author_subagents}` stands for the number of author subagents (write any other brace twice, `{{` and `}}`). |
| `-criticPromptFile PATH` | built-in prompt | A UTF-8 critic prompt. |
| `-finalPromptFile PATH` | built-in prompt | A UTF-8 LaTeX prompt. |
| `--verbose-events` | off | Print every public JSONL event (see below). |

Prompt-file paths are resolved from the terminal's current folder. Run
`python3 web_ui.py --help` for every spelling and allowed value.

## What the terminal shows

Output is short by default: the current workflow step, diagnostics, errors, and
the start and finish of each input file. Prompts, model events, reasoning
summaries, tool activity and proofs are not printed; they stay in each proof's
`transcript.jsonl` and other files in its folder under `runs/`. `--verbose-events`
prints the full public JSONL event stream; folder runs then add an `inputFile`
field.

- A failed job does not cancel the others. The exit status is `0` when every
  proof succeeds, `1` for invalid input or any failed or paused proof, and `130`
  after Ctrl-C.
- The first Ctrl-C (or SIGTERM) takes queued jobs out of the queue (they stay as
  prepared jobs the web page can start), pauses each working proof author in its
  saved conversation and stops the other stages in place (they show **Stopped**;
  **Continue** on the web page runs the stopped step again in the same job). Each
  unfinished file then prints how to continue it, for example
  `python3 web_ui.py --resume-research runs/YOUR_RUN`. A second Ctrl-C stops
  every job and its processes at once; a stopped job still continues in place.
  Closing the terminal leaves the jobs running.
- A run that reaches its time limit waits for the progress summary, prints it
  after `Progress summary:` (or why it could not be written, after
  `Progress summary failed:`), and ends with
  `Paused: Workflow time limit reached.` and the command that continues the same
  conversation (exit status 1).

## Continuing a saved job

Continue a stopped author in its saved workspace with a new time budget:

```bash
python3 web_ui.py --resume-research runs/YOUR_PREVIOUS_RUN
python3 web_ui.py --resume-research runs/YOUR_PREVIOUS_RUN --thinking-hours 24
```

`--resume-author` is the same option. It opens the continued job in the web
page and keeps the source job's saved inputs and transcripts. The author keeps
working in its workspace with its full original assignment, and the way it
worked: a coordinator stays a coordinator with its coordinator prompt; a job
from before the author coordinator keeps its author as it was, with any
subagents that author may start itself (none when it saved no number); and one
with `-authorSubagents 0` keeps an author that works alone. Saved settings stay
unless you override them. Critic and LaTeX checkpoints keep their own files.

If a run has `SOLUTION.md` or `saved-candidate.md` but no checkpoint button,
open a new critic job on it:

```bash
python3 web_ui.py --resume-critic runs/2026-08-26_16-33-26_example
```

This loads the saved complete proof, checked statement and role prompts into a
new job in the web page, with fresh critics. Its author, should the critic send
the proof back, keeps the saved job's Author subagents and the way that author
worked, unless you give `-authorSubagents`. An unchanged pass goes on to the
LaTeX editor; edited passes repeat up to the maximum, then one last round that
cannot edit must pass the proof unchanged; a rejection returns the candidate and
the bug report to the author's normal repair loop.

## Reading a transcript

`transcript.jsonl` is the complete machine-readable event log (of Codex's
effective settings, which it reports at each start, it keeps only those TCS
Prover sets or relies on: see
[Job records](developers.md#job-records)). The transcript
viewer shows workflow stages, public reasoning summaries, model messages,
subagent and tool activity, critic checks and results, without protocol noise:

```bash
python3 transcript/view_transcript.py runs/2026-08-03_16-20-24_example/
python3 transcript/view_transcript.py runs/2026-08-03_16-20-24_example/transcript.jsonl
python3 transcript/view_transcript.py                       # newest run
```

The browser view has stage and activity filters, full-text search, root-only
and compact views, expandable long entries, critic verdict cards, loading of the
whole file with pause and resume, and live updates. It binds only to the local
host and protects its local API with a random token per launch.

For terminal or file output, use the text mode:

```bash
python3 transcript/view_transcript.py RUN --text
python3 transcript/view_transcript.py RUN --text --follow
python3 transcript/view_transcript.py RUN --output readable.txt
```

Text mode hides prompt bodies unless you add `--prompts`. `--stage solve`,
`--stage critic`, `--root-only`, `--no-tools` and `--max-text 0` adjust the
view; `--help` lists every option. Both views show the public reasoning
summaries TCS Prover keeps, never private chain-of-thought, and read
incrementally, so even very large transcripts fit.

To run the workflow files directly with `workflow_runner.py` (which also has
`--author-mode` to choose how the author works), see
[Developers and operators](developers.md#running-workflows-directly).
