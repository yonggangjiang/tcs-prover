# Retries and what needs you

## Automatic retries

Every failed model call is retried automatically every minute (plus up to five
seconds, so that many jobs do not retry in the same instant), with no attempt
limit, so a job keeps going through provider outages without you. This covers
every role: the statement reviewer, the proof author, the critic, the convention
critic, the LaTeX editor and its compile repairs, and the paused author's
replies.

A failure is anything that interrupts a call: a provider error such as "model at
capacity", overloaded, 429 or 5xx; a dropped stream; a program that crashes or
exits with an error (or reports a failed turn with exit code 0); a program that
is missing (not installed yet, or updating itself); a stalled call; a login or
usage-limit error; and an answer that cannot be used (empty, not valid JSON, the
wrong shape, or failing a workflow `require` rule, such as a critic that returns
no proof).

- **While it waits**, the job stays running, and the job page, the activity log
  and the terminal say why, for example "Model call failed (model at capacity) —
  retrying in 1 min (attempt 7, retrying for 6 min)" with the provider's message
  (keys removed). The wait does not use the time budget. **Stop** ends a wait at
  once, and **Pause** (while the author works) pauses the job instead of
  retrying. The first retries of a call stay pinned in the activity log; the
  rest stay in the recent window and in `transcript.jsonl`.
- **Slow calls.** A structured call (reviewer, critic, convention critic, LaTeX
  editor) is never stopped while it works: an attempt is stopped and retried only
  after its program has written nothing for an hour, or two hours while a critic
  waits for its subagents (Codex is silent then and allows such a wait an hour).
- **A silent author.** A turn with no event for 30 minutes gets a cheap check (no
  model call). An unanswered check (after 1 minute), or a request Codex leaves
  unanswered for 10 minutes, ends the attempt, and the retry reopens the same
  conversation. A Codex turn that ends with the goal still open, followed by no
  new turn for 1 minute, is continued with the continuation prompt. Requests
  Codex sends to TCS Prover (approvals, user input, MCP elicitations) are
  declined, and the turn continues. Codex's own reconnects appear as **Codex is
  reconnecting (2/5)…**, and in live runs its redacted error output is kept in
  `codex-app-server.log` in the run folder.
- **Time limit.** No author attempt, and no retry of the statement review,
  starts after the job's time limit is reached; the critic and LaTeX steps after
  a submission always finish.

### A retry keeps what the failed attempts knew

- **Structured calls** keep their program session. Once an attempt has produced
  model output (for example a critic read the proof), the next attempt forks that
  session with the same model and settings (`codex exec fork`,
  `claude --resume --fork-session`) and adds a short note; after an unusable
  answer the note says in plain words what was wrong, for example "the solution
  field must not be empty". A critic that rejects without listing bugs is asked
  to keep its verdict and list them. A fork that fails before producing anything
  is deleted, so an outage adds nothing to the conversation. Without earlier
  output, when the session is gone or no longer fits the model's context, when
  the critic's proof snapshot disappeared, and in web-cutoff runs (which keep no
  sessions), the retry sends the exact saved input again in a new conversation.
  Sessions are deleted when the call ends and are marked so that Codex never
  builds memories from them.
- **The author's retry** reopens the same conversation. An instruction the model
  never received is sent again; a turn it received but answered with nothing is
  first removed from the conversation and its instruction sent again; after a
  turn in which it worked, a short note asks it to continue. A retry that fails
  before it reopened the conversation keeps the pending instruction for the next
  one.
- **Conversation replaced.** The author continues in a new conversation of the
  same run, with a **Conversation replaced** notice, when its program no longer
  has the saved conversation (for Claude, after trying its private copy; see
  [Claude Code](models.md#claude-code)), refuses to reopen it three times in a row
  with the same error that is not a provider failure, or reports that it no
  longer fits the model's context. The new conversation starts from the resume
  prompt (which restates the original author prompt), any open critic feedback
  and, after a submission refused before review, why it was refused, until the
  author submits again. Critic rounds and the time budget are kept.

### Diagnostics

Each failed attempt of a structured call adds a Diagnostic card to the activity
log, and its full provider messages and program error output, keys removed, go
to `model-failures.log` in the run folder (a failure that repeats the previous
one exactly takes one line). The retry notice quotes the provider line that
names the cause (else the program's own error line, or its newest line). The
message of a CLI that could not start is labelled *CLI message*, as no provider
was reached, and the runner's own message about a CLI that is not installed is
quoted whole.

At every start of the runner (a new job, Resume, or an automatic resume), TCS
Prover runs `codex --version` or `claude --version` for the programs the job's
models need (no model call) and names their path and version in the activity
log. A program that is not installed is named with the role and model that need
it and what to install; a broken one, and one that changed since the job last
started (`cli-versions.json` in the run folder), are named too. Nothing waits for
this check. A program that fails to start in the same way three times in a row
is shown as "Codex CLI missing or incompatible" or "Claude Code missing or
incompatible" while the retries continue, until it works again.

## What needs you

Some causes need you although the job keeps retrying: an expired login, a usage
limit, an API key the endpoint refuses, or a program that is not installed (or
does not start), after the same failure three times in a row (also while a
paused author's reply waits for it). The Overview then lists the job under
**Needs your attention** with what to do, for example *Codex login needed: Sign
in again on the Setup page, or run codex login in a terminal*, *Claude login
needed: Sign in again on the Setup page, or run claude auth login in a
terminal*, or *Claude Code not installed or not working* with the call's
message; a missing program or a login ends with a link to **Setup**, where it is
installed or signed in (see [the Setup page](models.md#the-setup-page)). Its
card and page say so, and the retries go on, so the job continues by itself once
that is fixed: a program Setup installs is found by the waiting job at its next
retry. **Models & API → Running jobs** can
also show a browser notification when a job first needs you.

A web-cutoff job held because its isolated LaTeX compiler is not ready (MacTeX
missing, being updated or damaged, or the compiler's macOS sandbox check
failing) is listed as *LaTeX compiler not ready*, with what fixes its cause. The
check runs again every minute, also while the running-jobs limit is reached, and
the job starts or continues by itself once it passes; a job you stopped only
has its hold released (see
[Web search cutoff](web-cutoff.md#what-a-timestamp-run-checks)).

Failures that no retry can fix are not retried: a configuration error (for
example a missing API key, or a Claude model in a web-cutoff run), a workflow
rule that cannot be checked on a valid answer (a problem of the YAML, not of the
model), a safety refusal (a program that offers unexpected tools, ignores an API
model's private Codex home or continues a different conversation, or a changed
web-cutoff policy) and programming errors.

- A critic or LaTeX step pauses at that step with its exact input. After a
  configuration, workflow-rule or safety error it waits for **Resume**; after any
  other error (such as a runner that was killed) it resumes by itself after a
  minute.
- The proof author pauses with such an error too, and so does a model that does
  not exist or that your account cannot use (**Model unavailable**) or, for an
  API model, an endpoint that refuses the requests as invalid with HTTP 400
  (**Request rejected by the API endpoint**; Claude Code reports it as "API
  Error: 400" with the endpoint's message). Both list the job under **Needs your
  attention**. Such a job resumes by itself once its API connection's settings
  change; one whose built-in model is unavailable waits for **Resume**. A key
  store that cannot be read for a moment is retried like any failure.
- An author stopped by a safety check, or a Codex author whose saved thread ID is
  missing, waits for you (**Resume**, or **Continue proof author** for a missing
  thread ID).
- When Codex reports that its code-mode tool runtime is unavailable, a live run
  retries the same conversation with the classic shell tool; a web-cutoff run,
  which needs the audited tool set, shows **Author needs attention** and checks
  again every minute.

## Error pauses, restarts and checkpoints

If a proof author still ends up paused by an error (for example when its runner
process itself ended), TCS Prover resumes the saved conversation after 1 minute,
and retries a failed resume every minute; a terminal run does the same. A critic
or LaTeX step that fails outside its model call pauses at that step with its
exact input and is resumed there the same way. A file write that fails, for
example on a full disk, is retried in place every minute, keeping the step's
result. Retry times survive restarts and need no open browser tab. The paused job
shows its next retry time; **Keep paused** cancels automatic retries, and
**Resume** retries at once.

Pauses and stops by you, used-up time limits, safety stops and a missing Codex
thread ID do not trigger this, and neither does a model whose settings are
incomplete (a CLI that is not installed is no such setting: the resume goes
ahead, and the model call that needs it says so and is retried every minute).
Saved work and the time budget are kept.

**Restarts.** Work that a restart, crash or reboot interrupted continues about a
minute after the next start: an author resumes its saved conversation, and a
critic or LaTeX step starts again from its exact saved input (`latex-source.tex`
is compiled again). A run that another TCS Prover process still owns (a terminal
run, or a runner left by a crashed server) shows as running elsewhere until that
process ends; then this server takes it over. A run whose saved checkpoint or
settings are damaged stays listed with a warning: its checkpoint is rebuilt from
the transcript, and unusable settings fall back to the defaults.

**Saved jobs.** At startup TCS Prover scans `runs/*/transcript.jsonl`, restores
the job cards and lists every critic checkpoint it can resume. It also finds
prepared workspaces (`preparedRun: true` in `job-settings.json`): **Start
prepared run** opens a fresh author session in that folder with its saved prompts
and workspace. No open browser tab is needed for any of this.

**Checkpoints.** A saved proof candidate holds the checked statement and the
latest complete argument. A checkpoint exists only once a complete artifact has
been written; it cannot restore the middle of a model response or its private
reasoning. To continue from one, open the job's batch, find the job by its start
and finish time, and click the continuation button beside the checkpoint (the
job's page also lists it under **Continue from saved work**). After you confirm,
TCS Prover creates a new job in the same batch that copies the candidate, the
role prompts, the models, reasoning, speed, critic rounds, number of critics and
time limit, and starts a fresh critic round with new independent critics. The old
job's inputs and transcripts are kept. A role whose API model was removed reports
it before any new request; an error-paused job then checks every minute and
continues once the model is configured again. Very old runs from before
`job-settings.json` restore only the choices visible in their transcript: a role
that never ran uses the current default, and the confirmation names such roles
before a new paid request starts.

## Jobs stopped by an earlier version

Jobs stopped by an earlier version of TCS Prover ended at the stop. They show
**Stopped at statement review**, **proof author**, **critic**, **LaTeX editor**
or **failure summary**, and keep buttons that each create a new job in the
source job's batch (such a job may already have a continuation using its saved
conversation, so continuing it in place as well could run two jobs on one
conversation). A stage that ended with an error, and a job whose saved
conversation was lost, offer the same buttons, on the batch list or under
**Continue from saved work**:

| Stopped stage | Button | What it does |
| --- | --- | --- |
| Statement review | **Retry statement review** | A new review from `review-input.json`, with the current statement, feedback, prompt and role settings. Older runs without that file can recover only their original draft. |
| Proof author, repair, or interrupted failure summary | **Continue proof author** | Reuses the saved prompt, workspace and your instructions, and resumes the author conversation when available (otherwise starts from its saved input). Partial text is never taken as a complete proof. |
| Critic | **Continue critic** | A fresh critic round from the latest saved candidate, with new independent critics. |
| LaTeX editor | **Retry LaTeX editor** | Retries the editor call with the saved accepted proof (older jobs use their newest critic checkpoint). When the job saved `latex-source.tex`, **Retry LaTeX compilation** compiles it again instead, without another editor call. |

These continuations make a new run folder; the source job's inputs and
transcripts are kept, and the author's continuation reuses its workspace. Runs
from before `manual-stop.json` are recognized by their `Stop requested`
transcript entry. To continue a job from the terminal, see
[Terminal runs](terminal.md#continuing-a-saved-job).
