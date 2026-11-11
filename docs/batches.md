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
  no problems left. A name has at most 200 characters, and there is no limit on
  the number of batches.
- **Previous batches**: folders under `runs/` that hold the runs of an earlier
  setup, marked **Previous batch · read-only**. Opening one never resumes its
  jobs or changes its files, and no problem can be added to it.

A job made from another job goes into the same batch: Resume at critic,
continuing from a checkpoint or a job an earlier version stopped, and **Rerun
with suggested statement**. **Continue** on a job you stopped keeps it in place,
in its batch. [Saved settings](saved-settings.md) never include the batch, so
loading or saving them never changes where the problem goes.

Jobs in every batch run, pause, resume, continue and retry the same way, share
the running-jobs limit and survive restarts. Every job's folder stays directly
under `runs/` whatever its batch: the batch is recorded in the job's
`job-settings.json` (`"batch"`) and the names of your batches in
`runs/.controller/batches.json`, so renaming or deleting a batch never moves a
folder. If that file cannot be read, the Overview says why, your batches'
problems are shown in the Default batch, and no batch can be made, renamed or
deleted until the file is repaired and TCS Prover is started again; jobs made
from those problems meanwhile return to their batch with them. Page links
include the batch, so equal problem names in different batches stay apart; a
link that names a batch its job is not in opens the job in its own batch.

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

**Related works** listed on the New problem page go with the folder run: they
are saved once, in a store of that folder run kept outside every job's folder,
and every job finds them in its own `related-works/` folder as hard links to
that one copy (no extra disk space; where a hard link is impossible, that job
gets its own copy and its activity log says so). While the run's jobs can still
take related works, the batch page lists the run under **Batch related works**:
**Add files…** and **Add a note** there add works to every job of the run,
queued ones included, and each running or paused author is told about them. A
job of the run can still add works of its own on its page.

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
