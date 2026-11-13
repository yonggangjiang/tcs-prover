# Web search cutoff

**Web search cutoff** under **Advanced settings** is off for new jobs.

| Cutoff | Value | What the models get |
| --- | --- | --- |
| Off (default) | not used | Native web search without a date limit; every model process has internet access. |
| On | `-1` | No web-search tools, and no model process has network access. |
| On | a UTC timestamp, such as `2026-10-02T14:13:12Z` | The author can search and read verified archives from strictly before that instant, and nothing else from the web. |

**Off.** Live web search is on for the author, the statement reviewer, the
critic, the convention critic and the LaTeX editor, including structured
retries and subagents. The runner sets Codex's
[`web_search="live"`](https://learn.chatgpt.com/docs/config-file/config-reference)
for new and resumed sessions, and Claude Code models get the WebSearch and
WebFetch tools; the models decide when to search. Through an API connection,
Codex gives some models no search tool at all, which the activity log then
says (see [How API models run](models.md#how-api-models-run)).

**`-1`** removes the web-search tools and turns off the network of every model
process. It is not the historical sandbox, and it says nothing about what the
models learned in training. Otherwise a `-1` run works like a live run; in
these guides a *web-cutoff run* means a run with a cutoff timestamp.

**A timestamp** needs macOS (elsewhere only Off and `-1` are offered),
**Statement review** off, and Codex CLI models for every proof role, because the
isolation is specific to the Codex sandbox: Claude and API models are refused
before launch, and the proof roles' menus turn off their Claude Code and API
groups and flag a chosen model that cannot run. It also needs the native Codex
binary, which a launcher script cannot replace. The cutoff must be in the past,
and it cannot be added to an existing conversation.

The search mode and cutoff are saved with the job and kept on resume; changing
the defaults never changes existing runs. Older runs without a recorded search
mode keep unrestricted search; an explicit disabled mode or historical policy
always wins.

## What a timestamp run checks

Before any model work, TCS Prover saves a separate policy for the run and checks
the native sandbox of the author and of the structured roles.

- The sandbox blocks shell networking and outside-file access, so the author
  cannot get around the archive gate with `curl`. It also denies `/tmp` and
  `/var/tmp`. Instead each model process (the author, its replies, every critic
  or writer call) gets an owner-only temp folder of its own in the run's
  controller folder (`codex-home/tmp/`), made when the process starts and removed
  when it ends (one a killed process left goes when the run next starts a
  process). `$TMPDIR` and `TMPPREFIX` name it in all the process's commands, so
  zsh heredocs (Codex runs commands in zsh) and `mktemp -p "$TMPDIR"` work. The
  checks probe it too: `$TMPDIR` must be the process's own fresh owner-only
  folder and writable, and `/tmp`, `/var/tmp` and a file next to it in
  `codex-home/tmp/` must stay out of reach. macOS's `/bin/sh` and `/bin/bash`
  ignore `$TMPDIR` for heredocs in their own scripts (`bash -c`, a script file)
  and use the current folder, so those work in the author's folder but not in the
  critic's read-only one, unless the command first changes to `$TMPDIR`.
- The sandbox grants no software folders: only the system's basic commands run
  there, not `python3`, `git`, Perl or TeX.
- With **Writing & polishing** on, the checks also cover the run's isolated
  LaTeX compiler, which never uses your `PATH`: it needs MacTeX as its installer
  puts it, in `/usr/local/texlive` with `latexmk` and `pdflatex` in
  `/Library/TeX/texbin` (owned by root and writable by no one else), and macOS's
  `sandbox-exec`, and its sandbox and a small test document must pass. When the
  compiler fails these checks, the job is held with the cause, for example
  "Historical compilation requires MacTeX and sandbox-exec", and listed under
  **Needs your attention** as *LaTeX compiler not ready*. The check runs again
  every minute, and the job starts or continues by itself once it passes. The
  hold says what fixes the cause:
  - MacTeX missing, being updated, not protected or failing the test document:
    *Install or repair MacTeX.*
  - macOS's `sandbox-exec`, `perl` or `sh` missing or not protected, or the
    compiler's sandbox failing its own checks: *The compiler's macOS sandbox
    check failed; MacTeX is not the cause.*
  - A cause that mentions `rosetta error`: TCS Prover's Python is an Intel
    (x86_64) build running under Rosetta, which the compiler's sandbox does not
    allow. Recreate `.venv` with a native (arm64) Python 3.11 or newer, then
    start TCS Prover again.

  The checks are never relaxed; no model starts before they pass.
- A failed check blocks launch: the prepared job is held and names the cause. A
  check that could not finish (a timeout, no connectivity for the controls, an
  early exit) runs again every minute, before launch and before every model
  call; a proven violation stops the run for safety. A check that passes
  releases the hold, whether it ran by itself or you chose **Re-run preflight**
  (which checks at once): a prepared job then starts and a paused one resumes,
  while a job you stopped stays stopped until you choose **Continue**. These
  checks start no model, so they run every minute whatever the running-jobs
  limit; after one that ran by itself, the job starts or resumes as soon as a
  running slot is free, a prepared one waiting in the queue (see
  [Running jobs](batches.md#running-jobs)).
- A problem no retry fixes (a moved or missing policy, a pinned binary with no
  verified replacement, an isolated configuration with settings TCS Prover does
  not write) holds the job with that message instead of retrying every minute. A
  moved checkout is adopted automatically and recorded in the search audit log.
- When Codex reports that its code-mode tool runtime is unavailable, a timestamp
  run, which needs the audited tool set, shows **Author needs attention** and
  checks again every minute.

Every model process of a timestamp run is checked this way: the author and its
paused replies (which use the author's own audited launch), the critic and its
subagents, the convention critic and the LaTeX writer. The LaTeX compile runs in
a separate isolated compiler, which repeats its MacTeX and sandbox checks before
every compile; a source it refuses ends the job with `final.tex` and a "PDF not
compiled" note. After a compile that fails, it also compiles its small test
document, so a MacTeX that is being updated or damaged is never taken for an
error in the source. When the compiler is not ready at that step (MacTeX
missing, being updated or damaged, or a sandbox check failing), the job keeps
running, waits at **Compile PDF** with *Waiting to retry a command that could
not run* and checks again every minute, without a limit and without using the
time budget. Such runs save no Codex session, so a failed subagent of a
version-2 critic is not detected there (the activity log says so), and the
critic's progress line says only that its critics have started.

## What else is recorded

- **Chat.** Every message to the author, and every answer to its questions, is
  recorded in the run's audit log (`author_message_sent`) before the author
  receives it; one the log does not accept is not sent, and the page says so.
- **Related works** are accepted as in live runs. They are local inputs in the
  isolated author's own workspace, read with its normal access, and each one is
  recorded in the audit log (name, size, SHA-256, time and kind), so the cutoff
  audit shows what you supplied.
- **Progress summaries** stay on the job page only (no `progress-summary.md`).

A timestamp run you stop continues with **Continue** in its own folder under the
same policy and keeps its time budget. Failed timestamp runs, and runs stopped
by an earlier version, continue in their own run folder under the same policy
with a new budget; from then on such a run pauses and resumes like any other,
also across a restart of TCS Prover, and stops only when you stop it again.

## The search panel

The job page shows a search panel only when a cutoff is set: whether search is
disabled or limited to archives (**Run settings** records every job's search
mode). For archive search it shows the saved cutoff, the provider, author-only
access and the broker's operation counts. *Pending* means an accepted call has
no recorded end event; it does not mean the call is still running. Verified
candidates are archive checks, not the number of sources the model received.
These counters do not replace the trajectory audit and do not prove that the
model's training knowledge is absent.

## Historical retrieval

With a timestamp, built-in web search is off for the proof roles, and an
independent retrieval service, available to the author only, enforces the saved
cutoff.

- **Providers.** The default `codex_wayback` finds candidate URLs through a
  separate Codex CLI session with general web search, then returns only strictly
  pre-cutoff Wayback snapshots whose capture time, original URL and content
  digest match the archive record. Current snippets and page titles are never
  returned; unknown dates, missing captures, unsupported content and redirects
  fail closed, and there is no live-content fallback. The optional
  `scholarly_wayback` targets research papers through the arXiv and Crossref
  APIs (no key); it discards current abstracts, titles, dates and scores, and
  its date filters only narrow discovery. The optional `brave_wayback` needs
  `BRAVE_SEARCH_API_KEY`, whose value is never written to run files. The legacy
  `duckduckgo_wayback` still works, but its public page often challenges
  automated requests; challenges are refused, never bypassed. A sealed
  historical-corpus provider exists for offline validation. `--preflight`
  checks the local configuration without a network request.
- **Discovery sessions** start fresh with only the Codex login: no inherited
  conversation, project instructions, skills, MCP servers or proof workspace,
  and no shell or local file tools. Candidate URLs come only from finished
  native search results (a model's suggestion counts only when it is in that
  list), and stay untrusted until the archive gate checks their content. Raw
  current-web traces stay in protected controller state and never reach the
  proof conversation. No separate API key is needed.
- **Pacing**, shared by every broker process and job: arXiv one active request
  and at least 3 seconds between starts; Wayback up to four active requests, 3
  seconds apart (slow responses do not hold the shared lock); Crossref one
  request, 1 second apart. Waiting requests start in arrival order, and time
  spent waiting for pacing does not use up a call's 300-second network budget (a
  call is still answered within nine minutes).
- **Errors.** Transport errors, HTTP 408, 429 and 5xx get at most two quick
  retries (5 and 10 seconds). `Retry-After` and HTTP 429 or 503 set a shared
  cooldown of at most ten minutes; a cooldown longer than a call's budget fails
  at once and says when to try again. A failed archive request or discovery is
  retried in the background every minute for fifteen minutes (discovery also
  once within the call), so the next identical call usually finds it cached.
  Challenges, redirects, invalid provenance and cutoff failures are never
  retried. Unavailable services are told apart from valid empty results, and
  partial searches are marked incomplete. Prepared batches examine up to six
  candidate URLs per search.
- **Cache.** Verified raw captures persist in a bounded controller cache outside
  the workspaces; every read rechecks the source, the times and both digests. A
  failing entry or stray file is moved to `.quarantine/` and the archive asked
  again; loosened folder modes are tightened, and a briefly unwritable audit log
  is retried every minute. URL discovery results have a fifteen-minute bounded
  cache in memory. Known archived arXiv CAPTCHA pages and publisher redirect
  stubs are refused, and older verified captures are tried instead.
- **Reading pages.** `open(url)` returns pages of at most 30,000 characters with
  `total_chars`, `has_more` and `next_offset` (runs prepared before paging return
  the whole verified document). `offset` and `length` ask for a page;
  `find(url, pattern)` searches the whole verified document for literal text and
  returns offsets (ten matches by default, with `max_matches` and continuation).
  Offsets count Unicode code points of the extracted text. Pass the returned
  raw-body `sha256` on later calls to pin the same capture; a changed capture
  fails closed. Documents over 4 MiB, 250 pages or the PDF text limits are
  reported as such, never as missing. `max_content_chars` is a search-excerpt
  setting only.
- **Concurrency.** Each broker runs up to four retrieval calls at once; further
  calls wait in arrival order with deadlines from their arrival, below Codex's
  ten-minute tool timeout. A search whose network deadline expires can return
  verified partial results. Cancelling suppresses the response. These limits do
  not limit the proof effort.

This is a historically constrained retrieval experiment, not an exact replay:
current search ranking and archive coverage differ from the original search,
and it gives no guarantee about training data. Codex's own search
[configuration](https://developers.openai.com/codex/config-reference/) has no
historical cutoff, so proof sessions turn native search off and use it only in
the protected discovery helper. The Codex-only adaptation of
[Time Travel Benchmark](https://github.com/trivikrama-madhusudhana/time-travel-benchmark)
uses its controller-owned MCP gateway, asynchronous tool handling and event-log
provenance checks, without OpenReward or a separate OpenAI API key; the
[adaptation notes](../third_party/time-travel-benchmark/UPSTREAM.md) pin the
reviewed upstream revision and explain the differences.

## Checking and auditing

Check real network availability through fresh broker processes, before starting
proof models (after preparation and authentication):

```console
.venv/bin/python -m retrieval.validate_historical_search \
  --policy-file .experiment-control/firstproof-batch3/P01/broker-policy.json \
  --cutoff 2026-10-02T14:13:12Z \
  --output .experiment-control/firstproof-batch3/search-validation \
  --query 'Python documentation sorting' --repeats 1
```

It searches and opens a verified result in a fresh broker, checks readable
results, strict cutoff compliance, matching digests and audit records, and saves
the evidence. The shared archive cache starts empty, as in a fresh batch.
`--concurrent10` starts ten harmless queries together. Neither mode sends a
benchmark statement or starts a proof model (the Codex discovery provider does
start isolated discovery models). A pass cannot promise future availability.

Audit a running or finished batch without queries or models:

```console
.venv/bin/python -m retrieval.audit_historical_trajectory \
  --control-root .experiment-control/firstproof-batch3-search-v4 \
  --output .experiment-control/firstproof-batch3-search-v4/trajectory-source-audit.json
```

The auditor matches the sources actually returned to that problem's broker log
by URL, exclusive cutoff, capture time and archived-body hash, flags unexpected
retrieval tools, and separates failed or pending calls from valid empty
results. It checks recorded provenance, not mathematical correctness or
training knowledge; the gateway itself verifies bytes before returning content.

## Pinned benchmark preparation

`.venv/bin/python -m experiments.prepare_firstproof` prepares the ten pinned
inputs in `experiments/firstproof-batch3/manifest.json` without model work.
`.venv/bin/python -m experiments.launch_firstproof` checks configuration,
isolation and compilation; `--start` releases the hold and launches the jobs.
Both take `--problems P09 P10` for a subset. Use fresh run and controller
folders for each batch (existing work is never overwritten), and regenerate
policies on each host, since their paths are machine-specific. Generated runs,
controller state, credentials and environments stay out of Git. Launches need
macOS, because the isolated LaTeX compiler has only a macOS Seatbelt adapter
(the Windows MXC adapter covers the model roles only), and the runtime canaries
must pass: unavailable isolation blocks launch.
