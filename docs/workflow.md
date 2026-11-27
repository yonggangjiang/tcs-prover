# The workflow

The New problem page has four stages: **Statement review**, **Author**,
**Critic** and **Writing & polishing**. All are on by default. Click a stage to
turn it off or on, keeping at least one; the selected stages run in that order.
A stage that is off makes no model requests and needs no model settings or
credentials.

- Only **Statement review**: check a problem without proving it.
- Only **Author**: a candidate proof. Without writing, a run returns its proof
  as text.
- Only **Critic**: check and repair a proof; put the complete candidate in your
  input.
- Only **Writing & polishing**: polish and compile a theorem and proof you
  supply. Author followed by writing also works with the critic off.

For an algorithmic task, include the computational model, the problem and the
asymptotic goal in the statement.

```mermaid
flowchart TD
    S["Optional statement review"] --> A["Persistent author, with its examiner"]
    A -- "continue research" --> A
    A -- "complete candidate" --> C["Critic + its independent critics"]
    C -- "reject: exact bugs and reset rounds" --> A
    C -- "edited pass: next round, accepted at the maximum" --> C
    C -- "accepted proof" --> L["LaTeX editor"]
    L --> T["Compile PDF"]
    T -- "compilation errors" --> F["Minor LaTeX fixes"]
    F --> T
    T -- "success" --> O["LaTeX source and PDF"]
    A -. "time limit or interruption" .-> P["Preserve files and continue later"]
```

## Settings and prompts

**Advanced settings** on the New problem page start with
[Saved settings](saved-settings.md) (the Default settings every new problem
opens with, and settings saved under a name to load again) and the
[related works](job-page.md#related-works) for the author. Then they hold each
role's model and reasoning (see [Models and API connections](models.md)) and:

| Setting | Default | Meaning |
| --- | --- | --- |
| Parallel agents | 1 | 1 to 11: how many agents work at a time. 1: the author works alone. 2 or more: the author may also start subagents of its own, up to one fewer than this number at a time. See [the proof author](#the-author). |
| Examiner: every N compactions | 4 | Every N compactions of the author's conversation, a fresh session of the author's model examines the whole run and reports to the author (see [The examiner](#the-examiner)). 0: no examiner. |
| Independent critics | 3 | Fresh critics the critic starts on each proof version, 1 to 10. |
| Maximum consecutive critic rounds | 2 | Edited passes in a row, 1 to 100; at the limit the latest repaired proof is accepted. |
| Time limit in hours | 0 (none) | See [the job page](job-page.md#time-limit-and-progress-summary). |
| Generation speed | Standard | **Fast** applies to Codex CLI models only. |
| Activity log detail | Concise summaries | **Status only**, or concise or detailed public summaries of the models' reasoning. Private chain-of-thought is never shown or saved. |
| Web search cutoff | Off | See [Web search cutoff](web-cutoff.md). |

Jobs run in parallel, each in its own folder under `runs/`.

**Prompts.** Every text the workflow sends its models can be edited for the
next job: the role prompts, the author's lifecycle prompts, the
[examiner](#the-examiner)'s texts and the [goal loop](#the-goal-loop)'s texts
and tool descriptions. They live in `workflows/author_critic.yaml` (the LaTeX
editor's in `workflows/clean_up.yaml`, the statement reviewer's in TCS Prover),
each with a comment saying when it is used and its placeholders. New jobs read
them when they start, so an edit of the file applies to the next job without a
restart. An edit that cannot be read leaves the last readable version in use,
with a warning on the job page, until the file is fixed.

- **Edit prompts** (also **Prompts** in the sidebar) lists every text, grouped
  by where it is used: Statement review, Author, Goal loop, Critic and Writer.
  **Search texts** finds a text by its name, its use or its wording; groups open
  and close, and the arrow keys move through the list. Each text shows when it is used, its placeholders (such as
  `{objective}`, or `[STATEMENT]` where the statement goes) and its default; an
  edited one is marked **edited**, and its group says how many are.
- **Apply to the next job** checks every edited text as TCS Prover checks it
  when a job starts, and shows a problem on its text: a text with placeholders
  may use only those, with any other brace written twice (`{{` and `}}`); a text
  with `[STATEMENT]` keeps exactly one; no text may be empty except the Codex
  compaction summary, whose empty default leaves Codex's own, and the examiner
  instructions (empty: no examiner). The **Progress summary** has no
  placeholders but is filled in like the others, so a brace is written twice
  there too, and **Reply to a message** has only `{message}`; any other text
  without placeholders (a tool description, **Related works**) is sent as
  written, braces included. The
  edits apply to the job you are about to start only: they are never remembered
  in the browser, carried into the next new problem or kept in
  [saved settings](saved-settings.md).
- **Each job keeps its texts.** A new job saves the whole set it runs with (the
  defaults and your edits) in its folders, and beside it which of them you
  edited, and reads its own copy every time it starts, resumes or continues, so
  later edits of the workflow files never change a job that has started.
  **Run settings → View this job's prompts** shows them, read-only, each marked
  when it differs from today's default. A job from before keeps working as it
  did: it saved only its role prompts (shown there with a note) and reads its
  other texts from the workflow files each time it starts or resumes.
- A job saved with the earlier default author prompt (the one with separate
  macOS or Linux and Windows instructions) gets the current default when it is
  resumed or continued; an author prompt you edited stays as it is.
- **Prompts & responses** on the job page shows the exact prompts sent and the
  text the models returned.

## Statement review

The reviewer lets you start from informal language without letting a model
silently solve a different problem. It removes ambiguities and handles corner
cases, then asks you to approve the revised statement (**Approve and start**) or
to revise it with your feedback (**Retry with feedback**). It also guesses what
you most likely want, and when the statement could be satisfied by its letter
without doing that, it closes the loophole and explains each change in its
notes. For example, a literal model may object that `m` can be smaller than `n`
in a bound such as `m log² n`; the review states the intended convention and
writes `(m+n) log² n`.

With only **Statement review** selected, the result is saved as
`checked-statement.md` in the job's run folder and can be copied from the review
page; only the reviewer's model, reasoning, prompt and speed apply.

A review that fails returns to the statement it revised, with the error and your
feedback filled in again (a first review returns to the draft). **Retry statement
review** on the batch list runs it again in the same job from
`review-input.json`. A review a restart interrupted starts again by itself.
Stopping a review is described under
[Stop and Continue](job-page.md#stop-and-continue).

## The author

One root author conversation (a Codex goal thread or a Claude Code
conversation) works on the statement for the whole run, across pauses,
restarts, context compaction and critic rejections. What it does depends on
**Parallel agents**, 1 to 11, default 1 (in the terminal, `-authorSubagents N`
with one fewer: the author's own subagents, 0 to 10):

- **1:** the author works on the proof alone, without a subagent tool. Its
  instructions (the `author` prompt in `workflows/author_critic.yaml`) ask it to
  prove the exact statement and not to give up because it looks hard or open,
  and to keep its memory in the `memory` folder of its working directory (its
  conversation is compacted from time to time, and what is not written down is
  lost). Its goal, continuation, reopen, resume and repair prompts only say to
  keep working on the same statement and, where they ask for the proof, how to
  hand it in. The goal names "your current task", since a finished job can
  continue with a new goal: then the `new_goal` prompt reopens the author's
  conversation with the new task in full (see
  [New goal](job-page.md#new-goal)).
- **2 or more:** the same author, with the same prompt, may also hand parts of
  the work to subagents of its own: its program's subagent tool (Codex's or
  Claude Code's), limited to one fewer than the number at a time, as Codex's
  Ultra mode delegates. The `subagents` text tells it how many it may start.
  The subagents run the author's model.

The number is saved when the job starts, shown in **Run settings**, and kept by
resumes, continuations and folder runs. With 2 or more parallel agents the `author` prompt ends with the
`subagents` text: "You may start up to N independent subagents at a time, for
example to explore other approaches or to check calculations; you do not need to
ask the user." A job saved without the number (from before the setting) runs one
author.

### The examiner

**Examiner: every N compactions** (4 by default) sets how often TCS Prover
starts an examiner beside the author: every N compactions of the author's own
conversation (not a subagent's, and not while the job is paused); 0 runs the
author without one. When an examination is due while the last one is still
running, the next compaction starts it. The count carries over a pause: the
examiner's timeline (`team/timeline.md`) lists every compaction, and the job's
next runner counts on from it. Each examination is a fresh conversation of the author's
model in the author's folder, with the `author_examiner` text as its whole
instruction (**Edit prompts → Author → Examiner instructions**): it works out
what is still missing between the author's work and a complete proof, checking
for itself what it relies on instead of taking the author's word for it, judges
whether that is shrinking over time and whether the author looks likely to
reach a complete proof, examines how the author organizes and uses its memory
and whether that helps it make steady progress, and tells the author; if what
is missing cannot be resolved by working the same way, it asks the author to
step back and reconsider the overall approach. Its final reply is saved as
`team/member-1/<k>-report.md` and sent into the author's running turn, framed
by `author_examiner_report` (by default `[REPORT FROM THE EXAMINER]` and the
report), the way a chat message is; the author goes on working with it, and the
activity log shows *Examiner report delivered*. Codex and Claude Code authors
both get it.

The author is told of its examiner: while the job has one, the author's
first prompt ends with the `examiner_note` text (**Edit prompts → Author →
Examiner note**), which says that a report arrives under that heading every few
compactions, that the author should take its findings seriously, and that its
goal does not change: a complete proof of the original statement. The
post-compaction re-anchor repeats the first prompt, so the note is fresh when a
report arrives. **Examiner** 0, or an empty `author_examiner` text, runs the
author without an examiner and without the note.

A job saved before this setting keeps what it ran with: with the earlier
**Examiner** on, an examination at every compaction (1); with it off, none.

### The goal loop

The author works toward one goal, a checked proof of
the statement, in turns. Every new job from the New problem page runs them in
TCS Prover's goal loop; the form has no setting for it (the
[terminal](terminal.md)'s `-goalLoop codex` still chooses Codex's own goal
mode):

- TCS Prover starts every turn of a Codex CLI author itself (the Codex CLI's
  own goal mode is off). After a turn
  that did not end the goal it sends the next one, the `goal_continuation` text:
  `{continuation}` is the author's continuation prompt (placed first),
  `{objective}` the goal, `{elapsed}` the working time and `{limit}` the time
  limit and what is left of it. Its default is the Codex CLI's own continuation
  text (version 0.160), with the working time and the time limit in place of its
  token budget lines.
- The author ends the goal with TCS Prover's `update_goal` tool, whose
  description is the `goal_tool_update_goal` text (its arguments',
  `goal_tool_update_goal_status` and `goal_tool_update_goal_note`; their
  defaults are the Codex CLI's). Status *complete* hands the turn's final
  reply on as the answer when the turn ends, as before; a turn without a final
  reply gets the continuation instead. *Blocked* or *paused* is recorded
  and TCS Prover sends the continuation: it never stops a run for them. Each
  call is a **Goal status** line in the activity log, such as **Goal status ·
  blocked**, with the author's note and what TCS Prover does next.
- Everything else works as before: the time limit, Pause, Stop, Resume, live
  instructions, chat, compaction, the hand-in rules and retries every minute
  without limit.
- Claude Code has no goal mode of its own, so a Claude Code author always runs
  in TCS Prover's loop; its harness note (`claude_goal_harness`, with
  `{objective}`) is one of the editable texts.
- `codex_compact_prompt` holds the instructions the Codex CLI follows when it
  compacts a conversation; empty (the default) leaves the Codex CLI's own.
- **Codex built-in** is the Codex CLI's own goal mode with its own texts,
  unchanged. Every run with a [web cutoff](web-cutoff.md) runs it, and so do
  jobs from before TCS Prover's goal loop and jobs started with it while the
  form still offered the choice: their **Run settings** say *Codex built-in*.
- A job the form continues (its statement review sent back to the form, revised
  in place) keeps the goal loop it recorded, never another one.
- Anything that starts a new job runs TCS Prover's goal loop, also **Rerun with
  suggested statement** on a job that ran **Codex built-in** and settings saved
  with it before.

### Handing in a proof

The author writes its complete proof to `proof.md` in its working folder and
replies `PROOF_FILE: proof.md`.

- **Windows.** TCS Prover cannot read that file safely there, so the runner adds
  a short note after the author's first prompt, the continuation prompt and every
  repair prompt asking for the complete proof in the reply instead, and refuses a
  `PROOF_FILE` reply with a reason that says so. The workflow prompts are the same
  on every computer.
- **Snapshot.** Before review, TCS Prover captures the file and gives every
  critic call `proof.md`, `statement.md` and a manifest with SHA-256 hashes in a
  fresh workspace of its own. The critic's subagents only read that snapshot.
- **Refused before review:** a missing, linked, unsafe, non-UTF-8, empty or
  larger-than-8-MiB proof file, a proof larger than 8 MiB or than the critic
  model's context window (when its settings record one), and a `PROOF_FILE` in
  `related-works/`, `chat/` or another controller file. The job pauses at the
  author with a reason that names the file and the cause, never its content (for
  example `proof.md: file not found`), resumes the author after a minute, and
  asks it to submit the complete proof again. With the critic off, a proof file
  that cannot be read is refused the same way before writing.
- **Pointers.** A `PROOF_FILE` line counts only in a short message (at most
  1,500 characters without it). Next to a complete proof in the reply it is
  ignored, unless the file holds a different proof: then the handoff is refused
  and the author asked for one of the two. Clear variants are accepted, such as
  `PROOF_FILE proof.md`, Markdown emphasis, `see proof.md` or `./proof.md`.

## The critic

Each round is one critic call that starts fresh independent critics as its own
subagents and then decides. Its instructions (the `critic` prompt, where
`{count}` is the **Independent critics** setting) ask it to start exactly that
many subagents to look hard for bugs in the statement and in `proof.md`; to wait
for all their reports and decide which objections are valid; to fix every valid
bug it can, in small edits to its own writable copy of `proof.md`; and to reject
only for a critical error that a subagent found and that it confirms but cannot
rigorously fix. It runs with the critic model and reasoning.

Its answer is `{verdict, bugs, fixes}`: `bugs` is empty exactly when the verdict
is pass, and `fixes` lists what it fixed. Whether it changed the proof is decided
by comparing its copy with the captured candidate (spacing at line ends does not
count), so no answer has to hold the whole proof. An answer that breaks a rule,
or one given after some of its subagents' calls failed, is asked again with a
plain explanation, never turned into a rejection.

**Rounds.**
- An unchanged pass (a **clean pass**) accepts the proof at once.
- A **pass with fixes** sends the new version to another round with fresh
  critics, up to **Maximum consecutive critic rounds** (default 2). A pass with
  fixes in the last round accepts that repaired version ("Reached MAX (2 rounds);
  using the latest repaired proof."); the edits of that round are not reviewed
  again.
- A rejection returns the latest safely repaired candidate and the exact
  unresolved bugs to the author and resets the count. With the author off, the
  job ends as **Proof needs revision**, keeping the candidate and the objections.
  Writing never starts from a rejected proof.
- An interrupted round (a pause, **Stop**, a crash or a restart) runs again from
  its saved input as the same round, with new subagents. A continuation in a new job starts a fresh critic.
- Model review is not formal proof verification.

**Failed subagents.** Codex reports a failed subagent of version-1 models (such
as API models) in its event stream; for GPT 6.x and GPT 5.6 Sol and Terra
(version 2) TCS Prover reads it ("Agent errored") from the critic's saved Codex
session, and a Diagnostic says when that session cannot be found. Web-cutoff
runs save no session, so there such a failure is not detected and the activity
log says so.

### What you see of the critic

**Critic review inputs and decisions**, beside **Run settings**, groups each
submission of the author into a review cycle. Each round shows the critic's one
decision (**Clean pass**, **Pass with fixes** or **Rejected**), what it fixed,
the critical bugs, and for a round that changed the proof a collapsed **Show
changes to the proof** with the diff. The critic's exact input and raw decision
stay below, collapsed. (A job started before 20 Nov 2026 may also show the
verdict of its **Convention critic**, a step since removed, under the round that
accepted the proof.) The independent critics' own reports are not shown: the critic
reads them and makes the one decision.

- A round interrupted and resumed stays one round, marked **resumed**. A paused
  round says that it starts again with fresh critics on Resume.
- A diff longer than 200,000 characters is saved whole in
  `runs/.controller/<run>/critic-diffs/` and offered as a download; nothing is
  shortened.
- While a round runs, a progress line says how many critics have started and
  finished, such as "Critic 1 of 3 finished · Critic 2 of 3 working", never
  their text. For version-2 Codex models TCS Prover reads this from the saved
  session every two seconds; in a web-cutoff run, which saves no session, the line
  says only that the critic has started its critics. Claude's Task calls show
  each start and end. Extra critics show as "Critic 4 (beyond the 3 requested)".
  While a failed critic call waits to retry, the line says so; its critics start
  again with the retry. A round counts once, however many subagents and retries
  it has.
- The per-job **Critic prompt** changes the critic's instructions (keep
  `{count}`). A job saved with the earlier default prompt of controller-run
  parallel critics gets the current default when it is resumed or continued.

### Subagents: what the program enforces

Only the critic and, with **Parallel agents** 2 or more, the author have
subagents; the statement reviewer, the LaTeX writer, the examiner and the paused
author's replies have none. These are the programs' own subagents. Checked with
Codex 0.160 against a local stand-in for the model API:

| Codex setting | What it enforces |
| --- | --- |
| `features.multi_agent_v2.max_concurrent_threads_per_session` = N + 1 | Version-2 models (GPT 6.x, GPT 5.6 Sol and Terra): at most N subagents at once, counting subagents of subagents ("agent thread limit reached"). A call without subagents gets N = 0: with `multi_agent` off Codex still lists its collaboration tools to these models, unless `agents.enabled` is false (below). |
| `agents.max_concurrent_threads_per_session` = N, `agents.max_depth` = 1 | Version-1 models (API models and others): at most N subagents at once, and subagents get no subagent tool. |
| `features.multi_agent_v2.multi_agent_mode_hint_text` | Replaces Codex's "Do not spawn sub-agents unless the user … explicitly ask …" hint with one that allows up to N subagents without asking. |
| `agents.enabled` = false | A Codex process that starts no subagent of Codex: the statement reviewer, the LaTeX writer and its repair (a `-c` flag), and the thread of an author that works alone. Its model is offered none of Codex's collaboration tools. |

So the program limits how many subagents run at once, not how many in total.
Only the prompt says that the critic starts exactly that many, and (on version
2) that subagents start none of their own. For Claude Code, TCS Prover sets
Claude Code's own limits in the environment of the critic and of an author
with subagents: at most N subagents at once, none of their own, no copies of the
calling conversation, and subagents in the foreground. These are undocumented
settings of Claude Code, found in its version 2.1.289 (see
[Developers and operators](developers.md)). GPT 5.6 Luna (older) gets no
subagent tool, so with it the critic reviews alone and the author works
without subagents; the activity log says "No subagents with this model". In a
web-cutoff run these limits are session flags of each Codex process, and the
isolation check refuses any other agent setting.

### Critics of web-cutoff runs

They read the snapshot with shell tools, with outside file access, networking,
native search and MCP turned off. There the snapshot stays read-only, and the
critic returns the complete proof in its answer.

## LaTeX writing

The LaTeX editor makes one model call with its prompt and the latest proof (or
the theorem and proof you supplied). It writes the complete document to
`latex-source.tex` in its own workspace in small steps and answers with a short
summary; in web-cutoff runs it returns the document in its answer instead.
**Compile PDF** then runs `latexmk` with `pdflatex`, with shell escape and
latexmk configuration files off.

- Compiler errors, and citations or references left undefined, go back to the
  writer for minor fixes with every error of the compile; then it compiles
  again. A repair that breaks LaTeX commands, puts a comment in place of omitted
  text, or loses more than a tenth of the text, theorem environments or labels is
  asked for again.
- A compile has 5 minutes, then up to 15 and 30 minutes before the writer is
  asked to look for an infinite loop; one that prints more than 256 MB is
  stopped at once. Of an output longer than 24,000 characters the writer reads
  the TeX errors (at most about half of that, with `[More errors omitted]` when
  some do not fit), an `[N earlier characters omitted]` marker and the end.
- When the same first error, or the same source, has failed three compiles, the
  job finishes with the source as `final.tex` and a "PDF not compiled" note
  saying why, as when the web-cutoff compiler refuses a source. If only undefined
  references remain after three compiles, the PDF is kept.
- Only a successful compile publishes `final.pdf`. The source stays in
  `latex-source.tex`, and compiler output in `latex-compile.log`, with a copy per
  attempt (`latex-compile-1.log`, …).
- When `latexmk` or `pdflatex` is missing, the run says so when it starts. TCS
  Prover also looks in the standard TeX folders (`/Library/TeX/texbin`,
  `/usr/local/texlive/*/bin/*`, TinyTeX, Homebrew). The compile step then checks
  again every minute, without model calls and without using the time budget,
  until it can run.

The time limit never stops the critic or the LaTeX steps.
