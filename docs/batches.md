# Batches and folder runs

The Overview and the sidebar list your batches, each with its problem counts,
start date and status. Click one to see its problems, then open a problem to
see its reasoning and outputs.

## Batches

- **Default batch**: problems started with **New problem** (in the sidebar or on
  the Overview), and terminal runs.
- **Your batches**: **New batch** (on the Overview and in the sidebar) asks for a
  name, which may be left empty for one with the date and time, such as "Batch ·
  10 Nov 14:05", and opens the new batch. Used on the New problem page or on
  **Models & API**, it keeps that page open instead: the problem being written
  goes into the new batch, and what you wrote, its settings and its related
  works stay.
- **New problem in this batch**, on a batch's page, opens the New problem page,
  which says *Adding to batch "Graph coloring".* (**Add to the default batch
  instead** changes that and keeps what you wrote). The problem, or every job of
  a **Run a folder…** started there, goes into that batch.
- **Rename** renames one of your batches; **Delete batch** removes one that has
  no problems left, with the related works it shares. A name has at most 200
  characters, and there is no limit on the number of batches.
- **Previous batches**: folders under `runs/` that hold the runs of an earlier
  setup, marked **Previous batch · read-only**. Opening one never resumes its
  jobs or changes its files, and no problem can be added to it.

A job made from another job goes into the same batch: Resume at critic,
continuing from a checkpoint or a job an earlier version stopped, and **Rerun
with suggested statement**. **Continue** on a job you stopped keeps it in place,
in its batch. [Saved settings](saved-settings.md) never include the batch, so
loading or saving them never changes where the problem goes.

Jobs in every batch run, pause, resume, continue and retry the same way, share
the running-jobs limit and survive restarts. Every batch can also give all its
problems the same [related works](#shared-related-works). Every job's folder stays directly
under `runs/` whatever its batch: the batch is recorded in the job's
`job-settings.json` (`"batch"`) and the names of your batches in
`runs/.controller/batches.json`, so renaming or deleting a batch never moves a
folder. If that file cannot be read, the Overview says why, your batches'
problems are shown in the Default batch, and no batch can be made, renamed or
deleted until the file is repaired and TCS Prover is started again; jobs made
from those problems meanwhile return to their batch with them. Page links
include the batch, so equal problem names in different batches stay apart; a
link that names a batch its job is not in opens the job in its own batch.

## Shared related works

A batch can share related works, such as a common definition, notation or
background paper, with every problem in it: the default batch, a batch made
with **New batch**, and the batch a folder run goes into. On the batch page,
**Shared with every problem in this batch** lists them with **Add files…**,
**Add a note** (an optional title and the text) and **Remove** on each work.
The Overview's batch card counts them (*2 shared related works*).

- **Who gets them.** Every problem of the batch with an Author stage finds them
  in its `related-works/` folder, beside its own
  [related works](job-page.md#related-works), as hard links to one stored copy
  (no extra disk space; where a hard link is impossible, that job gets its own
  copy and its activity log says so).
  - A new problem gets what the batch shares when it is created, before
    anything runs: **New problem in this batch**, **Run a folder…** into the
    batch, a rerun into it, and a job made from another job of the batch.
  - A work you add reaches at once every problem of the batch that has not
    finished: in statement review or waiting for approval (a stopped statement
    review too), queued or prepared, running, paused, stopped or at its time
    limit. Running and paused authors are told as about any added work: a
    running author at its next step, a paused one with your next message or at
    **Resume**.
  - Finished problems keep what they had and get no new works. A job made from
    one of them gets what the batch shares then; if it reopens the saved author
    conversation, the author is told about the works its folder lacked.
- **The New problem page** says it in one line under the batch it adds to:
  *This problem also gets the 2 related works shared with every problem in
  “Graph coloring”: definitions.md and background.pdf.*, with **Open the batch
  page** (in a new window, so what you wrote stays). With the Author stage off
  it says the problem does not get them. The **Run a folder…** question says
  that every job gets them.
- **Remove** asks first, then stops sharing the work. New problems no longer
  get it, and problems whose author has not started (in statement review or
  waiting for approval, queued or prepared, or a stopped statement review) lose
  it; their activity log says **Related works removed**. Running, paused,
  stopped and finished problems keep their copy, as do web-cutoff problems and
  jobs that continue another job's work.
  The batch page says what each change did, for example *Removed notation.md.
  New problems in this batch no longer get it. It was taken out of 1 problem
  that had not started. 2 problems that have started or have a web cutoff keep
  their copy.* A work added later under the same name is saved under a new
  name (`notation-2.md`), so the problems that kept the first one get both.
- **A problem's own list** marks each shared work **from the batch** (and a
  folder run's works **from the folder run**); its own works stay its own.
- **Web-cutoff jobs** get them as local inputs like any related work: each is
  recorded in the run's audit log when the job gets it, and **Remove** never
  takes one away.
- New terminal runs do not get them (a saved job continued from the terminal
  does, as on the page). Saved (read-only) batches share none. Deleting a batch
  deletes what it shares.
- The limits of related works apply: 64 MB per file, 2,000,000 characters per
  note, 200 works per batch, and at most 200 related works in each job, its own
  and shared ones together (a start that would pass it is refused before any
  job is created; a job that cannot take the batch's works starts without them,
  and its activity log says why). They are stored once in
  `runs/.controller/.batches/<batch ID>/` (`default` for the default batch),
  outside every job's folder, and the stored files are read-only.

## Run a folder

On the New problem page, choose the stages you need, click **Run a folder…** and
choose a folder of statements. Each non-empty text file directly inside it
starts its own job, in parallel, with the selected author, critic and writing
stages and the current model and prompt settings. Statement review is skipped.

- Any file extension, or none, is accepted, including `.md`, `.txt` and `.tex`;
  files are read as UTF-8. Hidden files (names starting with `.`), `Thumbs.db`,
  `ehthumbs.db`, `desktop.ini` and subfolders are ignored. Empty files, binary
  files (with a NUL byte) and files too large for one statement (over 8 MB or
  2,000,000 characters) are skipped and listed in the confirmation.
- The browser asks before starting the jobs, and the confirmation names the
  batch they go into (the one the New problem page adds to). The job list shows
  each job's source file name, also after a restart.
- A folder run takes at most 200 statements, 2,000,000 characters per
  statement, and 8 MB for the whole request; the browser checks this before
  sending. Every request that creates a job has the same 8 MB limit, also
  through the live gateway. Folder runs with a web cutoff timestamp take at most
  10 statements and check every sandbox before starting any of them. Split
  larger folders.
- All jobs share the rate and usage limits of the same logins and API keys. A
  job that cannot be created or started (for example on a full disk) stops none
  of the others: the result names it, and TCS Prover tries it again every
  minute.

**Related works** listed under **Advanced settings** on the New problem page go
with the folder run: they are saved once, in a store of that folder run kept
outside every job's folder, and every job finds them in its own `related-works/`
folder as hard links to that one copy (no extra disk space; where a hard link is
impossible, that job gets its own copy and its activity log says so). While the
run's jobs can still take related works, the batch page lists the run under
**Shared with a folder run**: **Add files…** and **Add a note** there add works to
every job of the run, queued ones included, and each running or paused author is
told about them. A job of the run can still add works of its own on its page.
Every job of the run also gets what its batch [shares](#shared-related-works)
with every problem.

To run a folder from the terminal, see [Terminal runs](terminal.md#folders).

## Running jobs

**Models & API → Running jobs** sets how many jobs may run at once on this
computer (`0`, the default, means no limit; saved in `.tcs-prover/settings.json`).

- Beyond the limit, the jobs of a folder run wait in a queue (**Queued** on their
  cards) and start in order as running jobs finish or pause; automatic retries
  wait for a free slot too. The limit and the queue are shared by every batch.
- TCS Prover starts queued jobs and automatic retries at most one every 2
  seconds, longest-waiting first, so jobs that came due together reach the
  provider spread out.
- **Start now** starts a queued job at once, and a job you start or resume
  yourself always starts. A reply of the paused author starts without waiting
  for a slot or for the 2-second spacing, but takes a slot while it is written.
- The every-minute checks of held web-cutoff jobs start no model and take no
  slot: they run whatever the limit and the spacing, at most four starting in
  the same second. A job whose check passed by itself then starts or resumes
  like an automatic retry, within the limit and the spacing (a prepared one
  waits as **Queued**); after **Re-run preflight** it starts at once.
- The queue survives a restart, and a terminal folder run follows the same
  limit.
- **Notify me in this browser when a job needs attention** shows a browser
  notification when a job first needs you, while TCS Prover is open in a tab.
