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
    S["Optional statement review"] --> A["Persistent author: a coordinator of author subagents, or one author"]
    A -- "continue research; hourly time checks" --> A
    A -- "complete candidate" --> C["Critic + its independent critics"]
    C -- "reject: exact bugs and reset rounds" --> A
    C -- "edited pass: next round, read-only at the maximum" --> C
    C -- "accepted proof" --> V["Convention critic (warns only)"]
    V --> L["LaTeX editor"]
    L --> T["Compile PDF"]
    T -- "compilation errors" --> F["Minor LaTeX fixes"]
    F --> T
    T -- "success" --> O["LaTeX source and PDF"]
    A -. "time limit or interruption" .-> P["Preserve files and continue later"]
```

## Settings and prompts

**Advanced settings** on the New problem page start with
[Saved settings](saved-settings.md) (save these settings under a name, load
them, and choose the default new problems open with) and the
[related works](job-page.md#related-works) for the author. Then they hold each
role's model and reasoning (see [Models and API connections](models.md)) and:

| Setting | Default | Meaning |
| --- | --- | --- |
| Author subagents | 3 | 0 to 10. 1 or more: an [author coordinator](#the-author-coordinator) runs up to that many author subagents at a time. 0: one author works alone, with no subagent tool. |
| Subagent models | Same as the coordinator | With 1 or more author subagents. **Choose for each** gives each author subagent its own model and reasoning: a Codex CLI, Claude Code or API connection model, as the roles offer. See [the author's team](#the-authors-team). |
| Goal loop | TCS Prover (editable texts) | Who runs the author's turns: TCS Prover, with the texts in **Edit prompts**, or **Codex built-in**, the Codex CLI's own goal mode as before. See [the goal loop](#the-goal-loop). |
| Independent critics | 3 | Fresh critics the critic starts on each proof version, 1 to 10. |
| Maximum consecutive critic rounds | 2 | Edited passes in a row before the final check, 1 to 100. |
| Time limit in hours | 0 (none) | See [the job page](job-page.md#time-limit-and-progress-summary). |
| Generation speed | Standard | **Fast** applies to Codex CLI models only. |
| Activity log detail | Concise summaries | **Status only**, or concise or detailed public summaries of the models' reasoning. Private chain-of-thought is never shown or saved. |
| Web search cutoff | Off | See [Web search cutoff](web-cutoff.md). |

Jobs run in parallel, each in its own folder under `runs/`.

**Prompts.** Every text the workflow sends its models can be edited for the
next job: the role prompts, the author's and the coordinator's lifecycle
prompts, the [goal loop](#the-goal-loop)'s texts and the
[team](#the-authors-team)'s texts and tool descriptions. They live in
`workflows/author_critic.yaml` (the LaTeX editor's in `workflows/clean_up.yaml`,
the statement reviewer's in TCS Prover), each with a comment saying when it is
used and its placeholders. New jobs read them when they start, so an edit of the
file applies to the next job without a restart. An edit that cannot be read
leaves the last readable version in use, with a warning on the job page, until
the file is fixed.

- **Edit prompts** (also **Prompts** in the sidebar) lists every text, grouped
  by where it is used: Statement review, Author, Coordinator, Goal loop, Team,
  Team tools, Critic and Writer. **Search texts** finds a text by its name, its
  use or its wording; groups open and close, and the arrow keys move through the
  list. Each text shows when it is used, its placeholders (such as
  `{objective}`, or `[STATEMENT]` where the statement goes) and its default; an
  edited one is marked **edited**, and its group says how many are.
- **Apply to the next job** checks every edited text as TCS Prover checks it
  when a job starts, and shows a problem on its text: a text with placeholders
  may use only those, with any other brace written twice (`{{` and `}}`); a text
  with `[STATEMENT]` keeps exactly one; no text may be empty except the Codex
  compaction summary, whose empty default leaves Codex's own; a text without
  placeholders (a tool description) is sent as written, braces included. The
  edits apply to the job you are about to start only: they are never remembered
  in the browser, carried into the next new problem or kept in
  [saved settings](saved-settings.md). **Rerun with suggested statement** takes
  along the texts that were edited for the job (those that still differ from
  today's defaults); its other texts were the defaults of its day and give way
  to today's. A job that saved no record of its edits (one from before) takes
  along every text it saved that differs from today's defaults.
- **Each job keeps its texts.** A new job saves the whole set it runs with (the
  defaults and your edits) in its folders, and beside it which of them you
  edited, and reads its own copy every time it starts, resumes or continues, so
  later edits of the workflow files never change a job that has started. **Run settings → View this job's prompts**
  shows them, read-only, each marked when it differs from today's default. A job
  from before keeps working as it did: it saved only its role prompts (shown
  there with a note) and reads its other texts from the workflow files each time
  it starts or resumes.
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
**Author subagents** (`-authorSubagents N` in the terminal), 0 to 10, default 3:

- **1 or more:** it is an [author coordinator](#the-author-coordinator), which
  runs up to that many author subagents at a time and does not try to prove the
  statement itself.
- **0:** one author works on the proof alone, without a subagent tool. Its
  instructions (the `author` prompt in `workflows/author_critic.yaml`) ask it to
  prove the exact statement even when it looks like a hard or long-standing open
  problem, to think deeply and to try substantially different approaches when
  one stalls. Its goal, continuation, reopen, resume and repair prompts repeat
  that it must not give up. None of them asks for research notebooks or a file
  layout.

The number, and with it the mode (coordinator or one author), is saved when the
job starts, shown in **Run settings**, and kept by resumes, continuations and
folder runs. Jobs from before coordinator mode keep working as they did: one
saved without the number runs one author, and one saved with 1 or more runs its
author with the `author` prompt, which then ends with "You may start up to N
independent subagents at a time, for example to explore other approaches or to
check calculations; you do not need to ask the user."

### The goal loop

The author, or the author coordinator, works toward one goal, a checked proof of
the statement, in turns. **Goal loop** (in the Proof author's settings) chooses
who runs those turns:

- **TCS Prover (editable texts)**, the default. TCS Prover starts every turn of
  a Codex CLI author itself (the Codex CLI's own goal mode is off). After a turn
  that did not end the goal it sends the next one, the `goal_continuation` text:
  `{continuation}` is the role's continuation prompt (placed first),
  `{objective}` the goal, `{elapsed}` the working time and `{limit}` the time
  limit and what is left of it. Its default is the Codex CLI's own continuation
  text (version 0.160), with the working time and the time limit in place of its
  token budget lines.
- The author ends the goal with TCS Prover's `update_goal` tool, whose
  description is the `goal_tool_update_goal` text (its arguments',
  `goal_tool_update_goal_status` and `goal_tool_update_goal_note`; their
  defaults are the Codex CLI's). Status *complete* hands the turn's last
  message on as the final answer, as before. *Blocked* or *paused* is recorded
  and TCS Prover sends the continuation: it never stops a run for them. Each
  call is a **Goal status** line in the activity log, such as **Goal status ·
  blocked**, with the author's note and what TCS Prover does next.
- Everything else works as before: the time limit, Pause, Stop, Resume, live
  instructions, chat, the coordinator's time checks, compaction, the hand-in
  rules and retries every minute without limit.
- Claude Code has no goal mode of its own, so a Claude Code author always runs
  in TCS Prover's loop; its harness note (`claude_goal_harness`, with
  `{objective}`) is one of the editable texts.
- `codex_compact_prompt` holds the instructions the Codex CLI follows when it
  compacts a conversation; empty (the default) leaves the Codex CLI's own.
- **Codex built-in** is the Codex CLI's own goal mode with its own texts,
  unchanged, kept as a fallback. Jobs from before this setting run it (their
  **Run settings** say *Codex built-in*), and so does every run with a
  [web cutoff](web-cutoff.md): the form then shows it and says *Web-cutoff runs
  use Codex's goal mode.*

### The author coordinator

The coordinator does not try to prove the statement itself. Its prompt asks it
to run the team of author subagents and check the team's health, so that
progress on the problem grows steadily with the time spent, until the team has
a complete proof that has been checked. The strategy is its own: how it
organizes the team and its memory, how it judges progress (there are no fixed
criteria; it can search the run's whole record in `transcript.jsonl` in its
working folder), and how it brings the team back to steady progress when
progress stalls or slows.

- **Prompts.** Its first prompt is the per-job `coordinator` prompt, in place of
  the `author` prompt (**Coordinator prompt** in **Edit prompts → Coordinator**;
  `{author_subagents}` is the number of author subagents). Its goal, continuation, reopen,
  compaction, resume, repair and summary prompts are `coordinator_goal`,
  `coordinator_continuation`, `coordinator_reopen`, `coordinator_compaction`,
  `coordinator_resume`, `coordinator_repair` and `coordinator_summary`, and
  `coordinator_checkin` is its time check. The goal and the continuation point
  back to the coordinator prompt in one line (Codex repeats the goal in each of
  its automatic turns); reopen, compaction, resume and repair repeat the whole
  first prompt under `ORIGINAL COORDINATOR PROMPT`. `coordinator_report`
  answers a reply that is not a hand-in (below). The chat prompts, the note on a
  refused submission and the note on related works are the same as one
  author's.
- **Author subagents.** They are [TCS Prover's team](#the-authors-team): N
  members, each running the model **Subagent models** gives it, to whom the
  coordinator hands assignments with TCS Prover's team tools. The activity log
  shows each assignment as **Author subagent started** (with the member's number
  and model, such as *Author subagent 2 (Claude Opus 5.5) started*, and the
  first line of the assignment), then **Author subagent finished** or **Author
  subagent failed** (with the reason when it is known, and the file of its
  report), with how many run then. These lines stay listed while the members'
  own events (*Subagent 2 reasoning*, *Subagent 2 tool*) leave the live window.
  With a web cutoff, and in coordinator jobs from before the team, the
  subagents are the program's own instead (see
  [the author's team](#the-authors-team)).
- **Memory.** When a coordinator run starts, TCS Prover creates a `memory/`
  folder in the author's working folder, with owner-only permissions where the
  system has them; what it already holds is never removed or overwritten.
  The coordinator keeps the team's memory there as it sees fit; its prompt asks
  it to keep a log of the team's progress in `memory/progress.md`, which the job
  page shows as the [progress log](job-page.md#progress-log).
- **Time checks.** Every hour of the job's working time (the clock of the time
  limit: carried across resumes, without paused time, sleep or retry waits),
  TCS Prover sends the coordinator a time check as a live instruction with the
  elapsed time and the time limit, such as "Elapsed: 5 h 12 min. Time limit: 24
  h, about 18 h 48 min left." The first comes at 1 hour; a job resumed at 5 h 20
  min gets its next one at 6 hours. None is sent while the job pauses or stops,
  or during a progress summary or a reply. The activity log shows each as
  **Author coordinator time check**, and keeps these lines listed.
- **Handing in.** A final reply that names no proof file (no `PROOF_FILE`
  pointer, see below) is not handed to the critic: the activity log shows the
  reply as the coordinator's own line, then an **Author coordinator report** line
  that says why it was not handed on (kept listed), and the coordinator gets
  `coordinator_report`: its reply was kept as a progress report, and a hand-in
  is the line `PROOF_FILE: proof.md` alone. Where
  TCS Prover cannot read a proof file (on Windows), the coordinator hands in the
  proof in its reply, as one author does.
- **No subagent tool.** With the program's own subagents (a web-cutoff run, or
  a coordinator job from before the team) and a model that gets no subagent
  tool (GPT 5.6 Luna, or an Anthropic-format API model in bare mode, see
  [API connections](models.md#api-connections)), the author of a coordinator
  job works alone with the `author` prompt instead of coordinating (its progress
  summaries too), and the activity log, **Run settings** and the place of the
  progress log say so.
- **Claude Code**, with its own subagents (a coordinator job from before the
  team), runs the subagents in the foreground: a live instruction, a time check
  or a chat message reaches a Claude coordinator when its running subagents
  report. Time checks never pile up meanwhile: the next is written once the
  coordinator has read the last, with the time of its writing. With TCS
  Prover's team, `team_wait` returns as soon as one arrives.
- **Progress summaries** (at the time limit, or with **Summarize progress**) use
  `coordinator_summary`: from the team's memory, what has been verified, what
  remains open and why, and the most promising next steps. Like every reply of
  the paused author, the summary is read-only and starts no subagents.

### The author's team

An author coordinator works with a team of N members (N = **Author
subagents**), run by TCS Prover itself, rather than with the Codex CLI's or
Claude Code's own subagents.

- **Models.** **Subagent models** (in the Proof author's settings, shown with 1
  or more author subagents) is **Same as the coordinator** (the default: every
  member runs the coordinator's model and reasoning) or **Choose for each**: one
  row per member, *Subagent 1* to *Subagent N*, each with the roles' **Source**,
  **Model** and **Reasoning** menus (the Codex CLI, Claude Code or any API
  connection), so one team can mix models. Changing the number of subagents
  keeps the rows that remain and adds rows set to the coordinator's model. The
  program of each member's model counts on the [Setup page](models.md#the-setup-page)
  as a role's does.
- **Tools.** The coordinator works the team with TCS Prover's tools (its MCP
  server `tcs`): `team_members` (each member's model and what it is doing),
  `team_assign` (an assignment for an idle member, returning at once),
  `team_wait` (the next reports, waiting up to 30 minutes at a time; it returns
  early for a live instruction, a chat message or a time check),
  `team_followup` (continues a member's last assignment in the same
  conversation) and `team_stop`. Their descriptions, and their arguments', are
  the `team_tool_*` texts in **Edit prompts → Team tools**.
- **Assignments.** Each is a fresh run of the member's model, one turn long, in
  the author's working folder (the folder the coordinator works in, with the
  author's sandbox, network rule and credentials), with no subagents of its own.
  The member receives the assignment wrapped in `team_member` (by default the
  assignment, then "Your final reply is your report to the coordinator."), and
  its final reply is its report, saved as `team/member-<n>/<k>-report.md` in
  that folder. A failed model call is retried every minute without limit; a
  setting that cannot work (a program not installed, no sign-in, an unknown
  model) comes back to the coordinator as a failed assignment with the reason.
  Pause, Stop, the time limit and a restart stop the members with the
  coordinator; only their files stay.
- **The team note.** `team_note` is added to the coordinator's first prompt,
  after the related works; `{members}` lists the members, one line each (such as
  "- member 1: GPT 6.1 Sol (Codex CLI)").
- **Web cutoff.** A run with a web cutoff keeps the program's own subagents,
  which run the coordinator's model inside the same cutoff: **Subagent models**
  stays *Same as the coordinator*, and the form says *With a web cutoff, author
  subagents use the coordinator's model.* With Codex, the coordinator's
  delegation hint then asks it to start each subagent fresh with a
  self-contained assignment, to wait up to 30 minutes at a time and to close
  finished ones where it can. Coordinator jobs from before the team keep the
  program's own subagents too.

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
  fresh workspace of its own. The critic's subagents and the convention critic
  only read that snapshot.
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
  critics, up to **Maximum consecutive critic rounds** (default 2). At that
  maximum, one more round reviews the latest version read-only (the **final
  check**): it passes it unchanged or rejects it. Unreviewed edits are never
  accepted.
- A rejection returns the latest safely repaired candidate and the exact
  unresolved bugs to the author and resets the count. With the author off, the
  job ends as **Proof needs revision**, keeping the candidate and the objections.
  Writing never starts from a rejected proof.
- An interrupted round (a pause, **Stop**, a crash or a restart) runs again from
  its saved input as the same round, with new subagents; an interrupted final
  check stays read-only. A continuation in a new job starts a fresh critic.
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
changes to the proof** with the diff. The round that accepted the proof also
shows the **Convention critic**. The critic's exact input and raw decision stay
below, collapsed. The independent critics' own reports are not shown: the critic
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

Only the critic and, with **Author subagents** 1 or more, the author (an author
coordinator, or the author of a job from before coordinator mode) have
subagents; the statement reviewer, the convention critic, the LaTeX writer and
the paused author's replies have none.

An author coordinator with [TCS Prover's team](#the-authors-team) has exactly
N members, each working on one assignment at a time (`team_assign` refuses a
busy member), so at most N assignments run at once; members get no subagent
tool. The rest of this section is about the programs' own subagents: the
critic's, and an author's in a web-cutoff run or a job from before the team.
Checked with Codex 0.160 against a local stand-in for the model API:

| Codex setting | What it enforces |
| --- | --- |
| `features.multi_agent_v2.max_concurrent_threads_per_session` = N + 1 | Version-2 models (GPT 6.x, GPT 5.6 Sol and Terra): at most N subagents at once, counting subagents of subagents ("agent thread limit reached"). A call without subagents gets N = 0, since Codex lists its collaboration tools to these models anyway. |
| `agents.max_concurrent_threads_per_session` = N, `agents.max_depth` = 1 | Version-1 models (API models and others): at most N subagents at once, and subagents get no subagent tool. |
| `features.multi_agent_v2.multi_agent_mode_hint_text` | Replaces Codex's "Do not spawn sub-agents unless the user … explicitly ask …" hint with one that allows up to N subagents without asking; the author coordinator's hint also says that delegating is its task, that waiting for its subagents is part of its work, and that each starts fresh. |

On version-2 models the author coordinator also gets
`features.multi_agent_v2.default_wait_timeout_ms` = 1800000, so its waits for
its subagents default to 30 minutes. A web-cutoff run leaves this setting out
(its isolation check compares every agent setting, and whether Codex reports
this one has not been checked); there only the hint asks for long waits.

So the program limits how many subagents run at once, not how many in total.
Only the prompt says that the critic starts exactly that many, and (on version
2) that subagents start none of their own. For Claude Code, TCS Prover sets
Claude Code's own limits in the environment of the critic and of an author
with subagents: at most N subagents at once, none of their own, no copies of the
calling conversation, and subagents in the foreground. These are undocumented
settings of Claude Code, found in its version 2.1.289 (see
[Developers and operators](developers.md)). GPT 5.6 Luna (older) gets no
subagent tool, so with it the critic reviews alone and the author of a
coordinator job works alone; the activity log says "No subagents with this
model". In a web-cutoff run these limits are session flags of each Codex
process, and the isolation check refuses any other agent setting.

### Critics of web-cutoff runs

They read the snapshot with shell tools, with outside file access, networking,
native search and MCP turned off. There the snapshot stays read-only, and the
critic returns the complete proof in its answer.

## The convention critic

Once the critic has accepted a proof and the run is about to hand it to
**Writing & polishing** (or, without writing, just before the run ends), the
convention critic checks the accepted proof once, exactly what the writer
receives, together with the statement. Rejected rounds and passes that go on to
another round never run it.

It guesses what a conventional mathematician or theoretical computer science
researcher wants from the statement, and judges whether the proof does that or
"cheats" by exploiting a loophole in the problem description, such as a
brute-force construction that stores all answers. It is its own workflow step
(`convention` in `workflows/author_critic.yaml`): a read-only call with the
critic's model and reasoning and the critic's sandbox, network and web-cutoff
rules, without subagents. Its prompt is `convention_critic`; the prompt editor's
**Convention critic** tab changes it for one job.

Its verdict never changes the route: the run continues to writing, or to the
end, once it has answered. A failed call is retried every minute, and a run
paused or interrupted in this step runs it again on resume.

- **loophole**: a warning at the top of the job page says how the proof exploits
  the statement and suggests a statement (with **Copy**). **Rerun with suggested
  statement** opens the New problem page with that statement and this job's
  settings, with the **Author** stage on, in the job's batch while that batch
  takes problems; it starts nothing. **Ignore** dismisses the warning. Until then
  the job list tags the job **Loophole warning**.
- **faithful**: shown only in the critic review history, under the round that
  accepted the proof. A faithful verdict on a later accepted proof closes an
  earlier warning.

The latest verdict, its round and whether it was ignored survive restarts; a
saved (read-only) batch shows them too, without **Ignore**.

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
