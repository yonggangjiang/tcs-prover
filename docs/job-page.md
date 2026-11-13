# The job page

Open a job from its batch on the Overview or in the sidebar. The page shows the
exact checked statement (and your wording when it differs), the stage, the
active time and the time left, the local process status, recent public activity
and **Run settings** (the models and settings the job runs with). Job cards show
the full local start and finish time, with seconds, so repeated titles stay
apart.

## Status

The workflow rail always shows the whole workflow: Statement reviewer, Proof
author, Independent critic, Convention critic and Writing & polishing (LaTeX
editor, Compile PDF). Stages not selected for the run are muted. For an
[author coordinator](workflow.md#the-author-coordinator), the Proof author step
reads *Coordinates up to 3 author subagents and checks that progress keeps
growing.* One mark shows where the run is:

| Mark | Meaning |
| --- | --- |
| **Now** | The step that works. For the critic, how many of its independent critics have started and finished (see [The workflow](workflow.md#what-you-see-of-the-critic)). |
| **Paused here** | With the cause: a pause by you, a restart, or an error and when it retries. |
| **Stopped here** | Stopped by you, or at the time limit. |
| **Waiting here**, **Starts here** | Before the run continues. |
| **Ended here** | With the reason, such as *Accepted by the critic; writing not selected*, *Stopped by you*, or the error. |

While the job works, the status card says when the last event arrived (*Last
activity 2m 05s ago*) and, while something waits to retry, what waits and since
when: a failed model call, a command that could not run (such as compiling the
PDF without TeX), or saving a file.

**Run settings**, below the activity log, lists the models and settings the job
runs with. Its **Proof author** row ends with how the author works:

- An [author coordinator](workflow.md#the-author-coordinator) with
  [TCS Prover's team](workflow.md#the-authors-team) names each member's model,
  *coordinator, 3 author subagents: GPT 6.1 Sol · Claude Opus 5.5 · Lab model
  (API)*, or says *coordinator, 3 author subagents: same model as the
  coordinator*.
- A coordinator with the program's own subagents (a web-cutoff run, or a job
  from before the team) reads *coordinator, up to 3 author subagents at a time*.
- With **Author subagents** 0 it reads *works alone*. When the model of a
  coordinator job without the team has no subagent tool, its author works
  alone, and the row says why: *works alone: GPT 5.6 Luna (older) has no
  subagent tool (coordinator mode needs one)*. A job from before coordinator
  mode whose author could start subagents keeps its earlier wording, such as
  *up to 2 subagents at a time*.

A **Goal loop** row follows: *TCS Prover (editable texts; this job runs with the
texts saved when it started)* or *Codex built-in (the Codex CLI's own goal mode)*,
which jobs from before the setting and web-cutoff runs show (see
[the goal loop](workflow.md#the-goal-loop)). A job whose author runs through
Claude Code (a Claude model, or an API model of the Anthropic format) reads
*TCS Prover (Claude Code authors always use it)*, whatever loop the job
recorded, as Claude Code has no goal mode of its own. **View this job's
prompts**, under the rows, opens every text the job runs with, read-only and
grouped as in **Edit prompts**, each marked *changed* where it differs from
today's default; for a job from before every text was saved, the role prompts it
saved, with a note.

In the activity log, the coordinator's team shows as **Author subagent 2 (Claude
Opus 5.5) started** (the member's number and model, then the first line of its
assignment and how many members work), **… finished** with the file of its report,
or **… failed** with the reason; these lines stay listed. A member's own events
read *Subagent 2 reasoning*, *Subagent 2 tool* and *Subagent 2*. The
coordinator's team tools show as tool activity (*team_assign · completed*). Each
`update_goal` call of an author in TCS Prover's goal loop is a **Goal status**
line, such as **Goal status · blocked**, with the author's note and what TCS
Prover does next: it never stops for a blocked or paused goal, and when the turn
of a complete one ends, its final reply is handed on as the author's answer; a
turn without a final reply, and a coordinator's reply that names no proof file
(kept as a report), get the continuation instead.

### How jobs are counted

| Shown as | Meaning |
| --- | --- |
| **Running** | Reviewing, running, pausing or stopping. |
| **Needs you** | Waiting for your approval of the checked statement, or for a start (a prepared job). |
| **Queued** | Waiting for a running slot (see [Running jobs](batches.md#running-jobs)). |
| **Paused** (pause bars) | A pause by you, a restart, an interruption or a pause after a failure. |
| **Time limit reached** (clock) | Stopped at its time limit. Raise the limit, then **Continue**. |
| **Stopped** (square) | Stopped by you; the batch list says where, such as *Stopped at critic*. Jobs stopped by an earlier version count here too. |
| **Complete** (check mark) | A successful outcome. |
| **Incomplete** (cross) | Interrupted, failed or needing revision. |

Stopped and time-limit jobs are never counted as paused; both continue in the
same job with **Continue** and are shown in muted grey. The server classifies
every job once, so the sidebar, the Overview's totals (Running, Needs you,
Paused, Time limit reached, Stopped, Complete), the batch cards, the batch
page's filters and the job page always agree, also for saved batches.

A finished job says what it produced: **Answer ready** (a compiled document),
**Proof accepted** (the critic accepted it and writing was off, so no PDF),
**Proof written (not reviewed)** (the critic was off), **Review complete** (a
statement review alone), **Proof needs revision** (the critic rejected a proof
and the author was off), or **Incomplete** (the process ended without a result).
A finished result appears in the output panel. An unfinished run's own text is
not shown there, since its progress summary says where it stands; it stays in
`partial-output.md`.

## Progress log

A job with an [author coordinator](workflow.md#the-author-coordinator) shows a
**Progress log** in its status card, before **Talk to the author**: the
coordinator's `memory/progress.md`, the log of the team's progress that its
prompt asks it to keep. A line above it says
*Written by the author coordinator in memory/progress.md · updated 4 min ago*.

- TCS Prover reads the file again whenever it changes, and the card follows.
- The log is shown as plain text, never run or rendered (its Markdown stays as
  written), in a box that stays scrolled to the end as the log grows, unless you
  scroll up in it.
- Of a file longer than 100,000 characters the card shows the last 100,000 and
  says so, with the size of the whole file and where it is.
- Until the coordinator writes the file, the card says *The coordinator has not
  written its progress log yet.*
- With a model that has no subagent tool, the author of a coordinator job works
  alone and writes no log: one line says so in place of the card.

## Talk to the author

Every job with an Author stage has a **Talk to the author** card next to its
status. It lists, oldest first, your messages and the author's answers, the
author's questions and the progress summaries. **Send** sends a message of 1 to
12,000 characters.

- **While the author works**, the message reaches it as a live instruction at its
  next step. TCS Prover adds a short note: if the message asks a question or for
  information, the author writes its answer, addressed to you, to
  `chat/answer-<id>.md` in its working folder and then continues its proof work;
  if it only gives an instruction, the author follows it. The answer appears
  under your message once the file has not changed for 2 seconds, and is updated
  if the author changes it. A message the author has taken in shows **Delivered
  to the author**. Nothing waits for an answer; a message that gets none simply
  stays delivered. An author coordinator of Claude Code takes it in when its
  running subagents report, since they run in the foreground.
- **While the job is paused or stopped at the author** (by you, at the time
  limit, after a restart or a failure), the paused author answers in its own
  saved conversation (see [below](#replies-of-the-paused-author)).
- **While another step works**, or the job is pausing or stopping, **Send** is
  off and the card says when the author works again. What you typed stays, also
  after a reload.

Answers are shown as plain text, never run or rendered; one longer than 100,000
characters is shortened in the chat, which says so and offers the whole file. A
message with its note may hold at most 16,000 characters; a longer one is
refused, never cut.

A message the author has not taken in when the job pauses or stops reaches it
when it continues. A job continued as a new job gets your earlier messages
among its earlier instructions (in `USER_INSTRUCTIONS.md` in its working folder
when they do not fit in one). In a web-cutoff run every message is recorded in
the run's audit log before the author receives it; one the log does not accept
is not sent, and the page says so. A finished job, and a saved (read-only)
batch, show the conversation without the box.

### Summarize progress

**Summarize progress** is beside **Send**. While the author works it reads
**Pause and summarize**: it pauses the author exactly like **Pause**, then the
author writes a progress summary in the chat, and **Resume** continues the work.
While the author is paused or stopped, it writes the summary at once; while it
is still pausing or stopping, it writes it once it has paused or stopped. Only
the time limit writes a summary by itself. A live run saves the newest one as
`progress-summary.md`. An author coordinator writes it from the team's memory.

### Replies of the paused author

While a job is paused or stopped at the author, **Send** and **Summarize
progress** ask the paused author, which answers in its own saved Codex thread or
Claude session and so remembers the exchange; the work itself continues only on
**Resume** or **Continue**.

- A reply is one turn outside the time budget, under a prompt that asks the
  author not to continue the proof or change files; the checkpoint and the
  workflow state are untouched. In live runs the turn is read-only and has no
  subagents; a web-cutoff run uses the author's own audited launch (Codex only),
  so its cutoff is checked exactly as for the author's work.
- One reply is written at a time; the text box stays editable meanwhile, and
  **Stop reply** cancels a queued or running reply (the job stays paused). A
  reply starts without waiting for a running slot, but takes one while it is
  written. **Resume** waits until the reply is written or stopped.
- A failed model call during a reply is retried every minute without limit in
  the same conversation; the card shows the reason and the next attempt, and
  after the same failure three times the Overview lists the job under **Needs
  your attention**. A reply interrupted by a restart is asked for again.
- A reply that cannot be written fails with the reason, and you can ask again:
  the saved conversation is gone, Codex or Claude refused to reopen it three
  times in the same way, a safety stop, a configuration problem, or another job
  working in the same workspace. The card also says why when talking is not
  possible at all (no conversation started yet, a held prepared run, or another
  process running the job).
- A **Pause and summarize** that arrives just as the author hands in its proof
  says so: the job then paused at the critic, where the author cannot write it.

### Questions from the author

A Codex author can ask you a question while it works (Codex's
`request_user_input_async` tool). It does not wait for you: it keeps working,
and answering is optional. Claude authors have no question tool.

- The question appears in the chat as **The author asks**, with its options as
  buttons (the first one chosen) and a field for your own answer, which replaces
  the chosen option. Clicking the chosen option clears it, leaving that question
  unanswered. A new question, and one that is open when you open the job's page,
  is scrolled into view. The chat keeps its height, so a long question can need
  a short scroll inside the chat to reach **Send answer**.
- **Send answer** sends the answered questions as one live instruction, without
  the chat note: `Answer to your question "<title>": <answer>`, or for several
  `Answers to your questions:` with one line `"<title>": <answer>` each. It is
  refused, with the reason, when the question is no longer open, the author is
  not running, every answer is empty, or the text has a NUL character or is
  longer than 12,000 characters. **Dismiss** closes the question without
  sending anything.
- The question closes when you answer or dismiss it; when the author asks a
  newer one; when the author stops working (the proof goes to the critic, a
  pause, the time limit, a failure, **Stop**, the end of the job); and when the
  step in which it asked has ended and 10 minutes have passed since it asked. If
  it closes while you write, the page says so, and while the author still works
  you can send your answer as a chat message. A restart pauses the author
  without closing its question, which shows again when the author continues by
  itself (unless its 10 minutes are over). A closed question never comes back.
- The chat keeps every question with how it ended, and the job list tags a job
  with an open question **Question**. The activity log shows a question as
  **Author question** (**Question to the user** in the transcript reader), an
  answer as **Live instruction queued**, and **Author question closed** (with
  the reason) or **Author question dismissed**.
- A question is never the author's answer: it is not taken as the result of its
  turn or as a proof, and it is left out of the saved partial output.

## Pause and Resume

**Pause**, beside **Stop**, interrupts the author's turn and keeps the saved
conversation and the workspace. The author may finish its current step for up to
30 seconds (**Author stopping**) before its program is stopped. A pause asks for
no final answer and no summary. Wait for **Paused** before closing TCS Prover or
changing your Codex CLI or Claude Code login.

- **Resume**, on the job page or its card, reopens the same saved conversation
  in the same run folder with a short prompt; critic feedback the author already
  received is not sent again. If the program no longer has the conversation, the
  author continues in a new conversation of the same run with a **Conversation
  replaced** notice.
- Paused jobs stay resumable after a restart. Paused time, and time the computer
  sleeps, do not count against the time limit.
- Failed model calls never pause a job; they are retried every minute (see
  [Retries and what needs you](retries.md)). A job that still pauses with an
  error resumes by itself after a minute: **Resume** retries at once, **Keep
  paused** cancels the automatic resume. After a restart, an unfinished author
  shows **Paused** and continues by itself about a minute later, with the same
  choices.

## Stop and Continue

**Stop** works like reaching the time limit. While the proof author works, it
may finish its current step for up to 30 seconds (**Author stopping**); then its
program is stopped and its conversation is kept. Pressing the button again
(**Stop now**) stops it at once. During the statement review, the critic, the
convention critic or the LaTeX steps, Stop ends that step at once.

Either way the job then shows **Stopped**, never Paused or Time limit reached.
It keeps everything it saved, never continues by itself, takes no running slot
and writes no progress summary. The chat and **Summarize progress** stay usable
while it is stopped at the author.

**Continue** picks it up in the same job, run folder and batch: the author
reopens its saved conversation (the time already used still counts, so raise a
used-up time limit first), and any other step runs again from its exact saved
input, as after a restart. A stopped statement review goes back to its
statement, with a grey note, and runs again with **Continue** on the batch list
or **Start workflow** on its page; a stopped revision goes back to the earlier
review with your feedback filled in, and **Retry with feedback** runs it again.
Web-cutoff runs stop and continue the same way. A stop that a restart or a crash
of TCS Prover interrupts stays stopped.

Jobs stopped by an earlier version of TCS Prover continue as new jobs; see
[Retries and what needs you](retries.md#jobs-stopped-by-an-earlier-version).

## Time limit and progress summary

**Time limit in hours** (under **Advanced settings → Limits and logging**) is 0
by default, which means no limit. It counts active work only: paused time,
sleep, retry waits and the paused author's replies do not count. It stops only
the proof author; the critic and LaTeX steps after a submission always finish,
so an accepted proof is never cut off.

- The job page shows the limit beside **Stop**, and **Run settings** list its
  history: the starting limit, every change, every stop at the limit, the
  summary written there and the Continue after it. Each stop at the limit also
  stays noted under the job title, for example *Stopped at the time limit (0.003
  hours) at 01:09:29 · progress summary written · resumed at 03:50:06 with no
  limit*.
- **To change it**, open the control while the job is reviewing, prepared,
  running (at any step), pausing or paused. A running author applies the new
  limit at once; during the critic or LaTeX steps it applies at the author's
  next step, and while paused at **Resume**. Lowering it to the time already
  used, while the job runs, asks first and then stops the author at its next
  step.
- **When it is reached**, the author stops (**Author stopping**, then **Time
  limit reached**), never continues by itself and is not shown as paused. It
  then writes a progress summary in its saved conversation, without continuing
  the work: what it has proved, with precise statements and where they are
  written, what remains open and why, and the most promising next steps. The
  summary appears in the chat as **Progress summary at the time limit**; a live
  run also saves it as `progress-summary.md`. If the critic rejected the latest
  proof and the author has not seen that report yet, the page says so, and the
  author receives it when the run continues.
- **To continue**, raise the limit (or set 0), then **Continue**; typing a new
  limit and clicking **Continue** does both. With less than a minute left the
  limit counts as used up: **Continue** then opens the limit control instead,
  and the job list offers no Continue for that job.

For terminal runs, see [Terminal runs](terminal.md#what-the-terminal-shows).

## Related works

Papers, drafts and notes that the proof author can read while it works. Only the
author sees them: the critic, the LaTeX writer and the statement reviewer never
do, so the proof must still be complete and self-contained.

- **On the New problem page**, **Related works (optional)**, under **Advanced
  settings** after [Saved settings](saved-settings.md) (shown while the Author
  stage is on), has **Add files…** and **Add a note** (an optional title and the
  text). Each file uploads at once into a private staging area (`runs/.uploads`,
  kept for 24 hours); its row shows the name, the size, for a PDF whether its
  text could be extracted, and **Remove**. While the section is closed, its
  summary line counts them ("2 related works", with "· uploading" while one
  uploads), and marks a warning, with its reason in words, when one failed ("·
  1 failed to upload") or the Author stage is off ("· Author stage off").
  **Start workflow** and **Run a folder…** are refused while an upload is still
  running or has failed (remove it first), or while the Author stage is off,
  and the refusal opens **Advanced settings** on the list. With **Run a
  folder…** they go to every job of the folder run (see
  [Batches and folder runs](batches.md#run-a-folder)).
- **On the job page**, **Related works · N** lists them; **Add files…** and
  **Add a note** add more while the job is reviewing, prepared, running, pausing
  or paused (other jobs only list them). The author is told once, without being
  interrupted: in its first prompt, by a live instruction while it works, or
  with your next message or **Resume** while it is paused.
- Works are only ever added: the page cannot remove or download them, and a
  saved file is never overwritten. The author reads them but cannot change them
  (except Codex on Linux and Windows, whose legacy sandbox can write there).
- They are saved in `related-works/` of the author's working folder, listed in
  `related-works/INDEX.md` (oldest first, with sizes, page counts and text
  copies). A continuation that shares the workspace shares them, and a retried
  statement review copies them into its new job. Saved names keep at most 120
  portable characters (`A-Z a-z 0-9 . _ -`); a taken name gets `-2`, `-3`, ….
- A PDF's text is extracted for the author into `<name>.txt` (`[Page n]` before
  each page) by a separate process limited to 2 minutes, 2,000 pages, 5,000,000
  characters and 2 GB of memory (two at a time). An encrypted PDF, one without a
  text layer (there is no OCR), or one over a limit keeps no text copy, and its
  row says why.
- **Limits:** 64 MB per file, 200 works per job, 2,000,000 characters per note,
  and a one-line title of at most 200 characters. Each is a refusal with its
  reason, never a silent cut.
- Web-cutoff runs accept them as local inputs in the isolated author's own
  workspace (the sandbox is not widened), and record each one in the audit log
  (name, size, SHA-256, time and kind).

## Downloads

When the compile succeeds, **Download .tex** and **Download PDF** give the job's
`final.tex` and `final.pdf`. With **Writing & polishing** selected, **Download
.md** gives the proof as handed to the writer (`approved-proof.md`) once the
critic accepts it (or, with the critic off, once writing starts), and while the
LaTeX step runs or waits for TeX, **Download source** gives the current
`latex-source.tex`.
