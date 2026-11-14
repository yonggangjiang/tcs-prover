# Developers and operators

## Project layout

`web_ui.py` starts the web page or terminal runs; `workflow_runner.py` is the
graph engine and the model transport. Workflow prompts live in YAML.

The commands on this page run the checkout's own Python, `.venv/bin/python`
(`.venv\Scripts\python` on Windows), which the README's quick start sets up:
only `web_ui.py` switches to it by itself, and the `python3` of a stock Mac is
too old.

```text
web_ui.py                   Web page and terminal-run launcher (uses the checkout's .venv)
workflow_runner.py          Graph engine, model transport and workflow CLI
workflows/
  author_critic.yaml        Author, critic and convention critic: prompts, response shapes, logic
  clean_up.yaml             LaTeX writing: prompts, response shape, logic
runner/
  providers.py              Model registry and the private API model store
  records.py                The record of every model call: store, Codex trace ingest, session copies
  claude_cli.py             Claude Code transport for structured roles and the author
  confinement.py            Per-run read/write confinement of model processes (macOS)
  proof_handoff.py          Verified capture of an author's PROOF_FILE
ui/
  server.py                 HTTP endpoints, job state and process management
  cli.py                    Startup and Markdown file and folder runs
  review.py                 Independent statement review
  model_calls.py            The record routes: model calls, raw calls, training download, Records list
  batches.py                Saved run folders shown as read-only batches
  named_batches.py          Batches made with New batch (runs/.controller/batches.json)
  presets.py                Saved New problem settings (Advanced settings → Saved settings)
  program_setup.py          The Setup page: program checks, installs, updates and sign-ins
  related_works.py          Related works given to the proof author
  retrieval.py              Web search cutoff setup for the page
  ownership.py              One owner per run, one server per runs folder
  current_run.py            The current-run selection
  live_gateway.py           New page assets beside an existing controller
  legacy_overview.py        Overview fields of a controller from before the upgrade
  index.html, app.js, styles.css
transcript/
  view_transcript.py        Transcript reader, CLI and viewer server
  transcript_ui/            Transcript viewer HTML, JavaScript and CSS
retrieval/
  runtime.py                Search modes and fail-closed historical isolation
  historical_search.py      Historical (web cutoff) MCP broker, launched by path
  historical_*.py, scholarly_discovery.py
                            Broker transport, pacing, cache and URL discovery
  isolated_compiler.py      Sandboxed LaTeX compilation for historical runs
  validate_historical_search.py, audit_historical_trajectory.py
                            Operator checks for historical batches
experiments/
  firstproof-batch3/        Pinned First Proof inputs and manifest
  prepare_firstproof.py, launch_firstproof.py
third_party/                Notes and license of the adapted Time Travel Benchmark
docs/                       These guides
tests/                      Offline regression tests
```

The server starts the workflow runner in each job's private workspace with the
bundled YAML files; statement review runs separately, before the graphs. Run
folders always live under the checkout's `runs/`, whatever the launcher's
working folder.

**Where things are kept.**
- `runs/<run>/`: the job's workspace, `transcript.jsonl` and outputs.
- `runs/.controller/<run>/`: TCS Prover's own copies of the job's
  `job-settings.json`, `pause.json`, role prompts and checked statement, which
  it relies on (the run folder's copies are for the author to read), and the
  author's private temp folder `tmp/`.
- `runs/.controller/batches.json`: the names of batches made with New batch
  (owner-only).
- `runs/.controller/.batches/<ID>/`: the related works a folder run (`b_…`) or a
  batch (its ID, `default` for the default batch) shares: one read-only copy,
  hard-linked into its jobs' `related-works/`, and its manifest, which keeps a
  removed work's entry so its name is never used again.
- `runs/.uploads`: staged related-work uploads, removed after 24 hours.
- `runs/.trash/`: deleted jobs, kept for 30 days.
- `.tcs-prover/`: the private state (see [Sandbox and network](sandbox.md)),
  including `program-folders.json`, the folders the Setup page remembered.
  Setup's own output is kept in memory only.
- `.tcs-prover/records/<run>/`: the job's record of every model call (see
  [Job records](#job-records)), kept apart from the run folder: deleting or
  trashing the job never removes it.
- Private per-call folders are pruned once they are a day old, beyond the newest
  50 critic snapshots and historical critic sessions and 200 historical
  discovery runs.
- Structured calls keep their program session (Codex without `--ephemeral`,
  Claude with `--session-id`; retries fork it with `codex exec fork <id>` or
  `claude --resume <id> --fork-session`) only while the call runs, and delete it
  when it ends (`codex delete --force`, which also removes subagent threads, or
  the Claude transcript files), newest first; a job with a record copies each
  session into it first, and a session whose copy does not read back the same
  is kept (see [Job records](#job-records)). Team members' sessions go the same
  way when the team ends, newest first and twice over (Codex refuses to delete a
  session that a later fork still references). Until then Codex keeps it under
  `$CODEX_HOME/sessions` and its state database (an OpenAI-format API model's
  private Codex home), and Claude under `projects/` in its configuration folder
  (`.tcs-prover/claude-homes/` for Anthropic-format API models, or `~/.claude`
  in bare mode). An author retry removes an empty turn with Codex
  `thread/revert` or Claude `--resume-session-at`, and a silent author is checked
  with Codex `thread/goal/get` or a Claude control request.

## Workflow files

Each YAML file has `nodes` and `prompts`. Node names are free; the first node is
the entry point.

- `run: structured` makes a model call with a response shape; `run: goal`
  starts or resumes the persistent author conversation; `run: command` runs a
  bounded command without a model. A command's `command` mapping gives `argv`,
  `timeout` and a `result` state key, with optional `cwd`, `env`, `requires`
  (executables), `produces` (files) and `log`. Transitions can test
  `result.status` (`pass`, `fail`, or `uncompiled`; a command that cannot run is
  run again every minute); a command that ran out of time also sets
  `result.timed_out` (the bundled LaTeX repair then asks the writer to look for
  an infinite loop). Repairs can use `result.output` (a short output in full; of
  a longer one its TeX errors and its end, about 24,000 characters).
- A structured node's `provider_options: {<provider>: {timeout: N}}` sets its
  inactivity limit: an attempt is stopped after N seconds without output (one
  hour by default) and retried. Its `attempts` key is accepted for older
  workflows but ignored: model calls retry until they succeed, and a run that
  uses it says so once in its activity log.
- The YAML defines inputs, result checks (`require`), state updates and
  transitions, including the critic's repeat limit. `edit: {file, field,
  input}` gives the model a writable copy of one file in its own workspace,
  starting from the named input (or the proof snapshot's file of that name); the
  text it leaves there becomes `result.<field>`. A repeat branch's `verify:
  <prompt>` runs one more visit at the limit with that extra instruction and the
  file read-only.
- `subagents` (a number, or `{option, default, minimum}`, 0 to 10) is how many
  subagents the node's model may start at once: the run option decides
  (`critic_count` for the critic, `author_subagents` for the author, 3 by
  default), the node gets the `multi_agent` feature exactly when the number is 1
  or more, Codex gets its own limits for it (see
  [The workflow](workflow.md#subagents-what-the-program-enforces)), and a
  structured node's instructions read it as `{count}`. The author's `subagents`
  prompt reads it as `{author_subagents}` and is added to the first prompt of an
  author that works alone with 1 or more (see
  [The author coordinator](#the-author-coordinator)).
- Every structured call of Codex runs without Codex's goals feature
  (`--disable goals`, `structured_tool_arguments()`): the statement reviewer,
  the critic with its independent critics, the convention critic, and the LaTeX
  editor and repair. Codex 0.160's `codex exec` otherwise offers the call and
  the subagents it starts Codex's goal tools (`create_goal`, `get_goal`,
  `update_goal`), which start Codex's goal mode inside a one-shot call (checked
  with the real Codex against a scripted model server). A web-cutoff call gets
  the same flag; its isolation check (the preflight's `config/read`) is
  unchanged, and the historical discovery call had the feature off already.
- A structured call of Codex without subagents (the statement reviewer, the
  convention critic, and the LaTeX editor and repair) also has Codex's agents
  off (`-c agents.enabled=false`, `structured_limits()`). Without it Codex 0.160
  offered GPT 6.1 Sol its collaboration tools (`spawn_agent`, `wait_agent`, ...)
  and its multi-agent instructions there too, where only the session limit of
  one thread refused a spawn; with it the model gets neither (checked with the
  real Codex against a scripted model server). The critic keeps them for its
  independent critics. A web-cutoff call's preflight starts its checked
  app-server with the same flag and compares the `agents` that `config/read`
  reports exactly (`enabled = false`, the rest unset): the real preflight of
  the structured role and of a critic snapshot passes, and `--strict-config`
  accepts the flag.
- A structured node with `proof_handoff` gets the proof snapshot as read-only
  files (the convention critic uses this without `edit`). With `edit` and
  `proof_handoff` its result also has `proofChanged` and `proofDiff` (or, for a
  long diff, `proofDiffFile` and `proofDiffNote`).
- Bundled nodes declare `workflow_stage: author`, `critic` or `writing`, plus a
  `skip` target. The `workflow_stages` option skips disabled nodes with their
  actions, prompts and credential checks. Custom graphs without these
  annotations run as defined.

**The author's prompts.** A `run: goal` node's `lifecycle` binds `goal`,
`continuation`, `compaction`, `repair` and `resume`, and optionally `pause`,
`reopen`, `summary` (the progress summary), `conversation` (a reply to the
user's message, as `{message}`), `chat` (the note added to every chat message
while the author works, with `{message}` and `{answer_file}`), `related_works`
(the block about the related-works folder), `handoff_refused` (sent, after any
critic feedback, to an author resumed after its last submission was refused; it
gets the reason as `{reason}`, which the state keeps as `handoff_refused` until
the author submits again) and `subagents`. `--set related_works=true` appends
the `related_works` prompt to the first prompt, which `resume` restates, and
`--set goal_reanchor=true` sends a reopened author the full `resume` prompt
instead of `reopen`; the server sets both when they apply. Where the author's
proof file cannot be read safely (Windows;
`proof_handoff.file_handoff_supported()`), the runner adds `INLINE_PROOF_NOTE`
from `workflow_runner.py` after the first prompt (which `{original_prompt}`
carries into `resume` and `compaction`), after the `continuation` and `repair`
prompts and after the recovery prompt; the YAML never names an operating
system.

**The coordinator binding.** A goal node with `subagents` may also have
`coordinator: {prompt, lifecycle}`: the first prompt of the author coordinator
(exactly one `[STATEMENT]`; it may name `{author_subagents}`) and the lifecycle
prompts that replace the author's of the same key (a mapping, or a list of keys
that name prompts of their own names). It may bind `goal`, `continuation`,
`compaction`, `repair`, `resume`, `reopen`, `summary`, `conversation`,
`related_works`, `handoff_refused` and `checkin`, the hourly time check, which
only this binding has (it may name `{elapsed}` and `{limit}`); every other prompt
may name exactly what its author counterpart may, and the prompts it does not
bind stay the author's (the bundled one leaves `conversation`, `chat`,
`related_works` and `handoff_refused` shared). The run's `author_mode` option
(`--author-mode`) chooses: `coordinator` runs the binding and needs it and 1 or
more subagents (else the run stops with a settings error before any model starts,
also when the workflow file was edited to drop the binding; a reply turn of the
paused author fails at once), `author` runs the author alone
(with its `subagents` note), and `auto`, the default, runs the coordinator when
the number is 1 or more. Its prompts are
checked only when it runs. The recovery prompt is the binding's prompt for the
lifecycle key the node's recovery prompt is bound to (`repair` gives
`coordinator_repair`), and `INLINE_PROOF_NOTE` follows the coordinator's
`continuation` and `repair` too.

A complete custom workflow can be as small as this `summarize.yaml`:

```yaml
nodes:
  summarize:
    run: structured
    prompt: summarize
    inputs:
      content: state.input
    response:
      summary: string
    after:
      - set: {output: result.summary}
    next: end
prompts:
  summarize: "Summarize this text in one sentence: {content}"
```

```bash
.venv/bin/python workflow_runner.py summarize.yaml < notes.md
```

Keep custom definitions outside the bundled `workflows/` folder.

## Running workflows directly

```bash
.venv/bin/python workflow_runner.py workflows/author_critic.yaml workflows/clean_up.yaml < statement.md
.venv/bin/python workflow_runner.py workflows/clean_up.yaml < theorem-and-proof.md
```

The first runs proof search, criticism and LaTeX; the second LaTeX alone. Both
skip statement review. `.venv/bin/python workflow_runner.py --help` lists the
model, reasoning, time-limit, critic-round, critic-count and prompt options.
`--author-subagents N` (the workflow's default, 3) and `--author-mode
{auto,coordinator,author}` (default `auto`: a coordinator when N is 1 or more)
choose how the author works, and `--coordinator-prompt-file` gives the
coordinator's first prompt, as `--author-prompt-file` gives the author's. The
server always passes the mode the job recorded. `--goal-loop {tcs,codex}` (default
`tcs`), `--author-team {tcs,builtin}` (default `tcs`), `--author-subagent-models
JSON` and `--prompt-set-file PATH` are described in
[TCS Prover's goal loop and team](#tcs-provers-goal-loop-and-team); options
given through the Python API without them keep Codex's goal mode and the
built-in subagents.
`--model` sets the fallback model for any workflow; `--set NAME=VALUE` sets any
named option and accepts JSON values. Node `role` settings can use options such
as `editor_model` and `editor_effort`. To run only the author:

```bash
.venv/bin/python workflow_runner.py workflows/author_critic.yaml workflows/clean_up.yaml \
  --set 'workflow_stages={"review":false,"author":true,"critic":false,"writing":false}' < statement.md
```

- Omitted stage flags default to on. A critic-only run needs the exact statement
  in `state.statement` and the complete candidate in `state.solution`, through
  `--state-file` or the Python API (the `input_solution` option can also set the
  candidate). A rejected critic-only run emits `failure_result` with
  `resultKind: needs-revision` and exits unsuccessfully; a proof without writing
  emits `final_result` with `resultKind: proof`.
- The CLI sets `state.input`, `state.statement` and `state.source` to the trimmed
  input. Chained graphs share state, and `state.failed` stops the chain. All
  graphs are checked before a chain starts.
- An optional `goal_pause_file` path pauses execution when the file exists: goal
  sessions pause their native goal and interrupt the turn without another model
  prompt, then `workflow_paused` is emitted with the current node and state,
  `state.paused` is set, and the chain stops without a completed proof. The
  server keeps this checkpoint apart from the author's workspace.
- Output is JSONL events, ending with `workflow_result` on success, whose
  `output` comes from `state.output`. The Python API is
  `execute(path, state, options=...)` or `execute_workflows(paths, state, options)`.
  `requirements.txt` installs PyYAML for the definitions, jsonschema for the
  response schemas, and pypdf for the text of related-work PDFs and of archived
  PDFs in historical search.

**A paused author's turn.** `.venv/bin/python workflow_runner.py
workflows/author_critic.yaml --converse` runs one turn of a paused author's
conversation and exits. It reads one JSON request on standard input,
`{"schemaVersion": 1, "id": "c_0123456789", "kind": "message", "text": "…",
"notice": ""}` (`kind` is `summary` with an empty `text`, or `message` with 1 to
12,000 characters; an invalid request exits with status 1), prepares only the
author, and sends the `summary` or `conversation` prompt (for an author
coordinator, `--author-mode coordinator`, the coordinator binding's `summary`,
unless a Codex model gave it no subagent tool; the server passes `--author-mode
author` for a coordinator job whose runner said that its author works alone),
after the notice when there is one, as one turn in the saved conversation named by
`--set goal_thread_id=…` and `--set goal_cwd=…`. It never starts or replaces a
conversation, never sets a Codex goal active, runs outside the time budget, and
retries every failed model call every minute without limit in that
conversation. Its records carry `stage: "conversation"`, `node: "author"` and
the request's `conversationId`: the request (**Progress summary requested** or
**Message to the paused author**), the statuses **Conversation reopened**,
**Failed turn removed** and (Claude only) **Saved conversation restored**, retry
notices, `codex_event` records, and then `conversation_reply` with the reply, or
the status **Author reply failed** (`final: true`) when no retry can help; both
end with exit status 0.

## The author coordinator

With Author subagents 1 or more (3 by default) the author node runs its
coordinator binding: one persistent conversation, as for the author, that hands
the problem-solving to its subagents. What the runner and the server add:

- **The recorded mode.** A job records `authorMode` (`coordinator` or `author`)
  in its state and `job-settings.json` when it starts, from its own number; one
  without the Author stage records `author`. A job saved without `authorMode`
  (every job from before the coordinator) is restored as `author`, one saved
  without `authorSubagents` with 0, and such a job saves no mode later
  (`authorModeRecorded` false, kept through every revision of its statement in
  place), so it stays a single author when it is resumed, revised in place (any
  number of times) or continued (`App._kept_author_mode`, which also keeps a job
  revised in place whose author works alone with subagents, as only an older job
  or a continuation of one does); an older job with 1 or more keeps the author
  prompt plus the `subagents` note. A continuation keeps an author that worked
  alone, and a continued coordinator keeps its coordinator
  prompt as a continued author keeps its author prompt. Every launch and every
  reply turn of the paused author passes `--author-mode` with the recorded mode.
- **The coordinator prompt** is a per-job prompt like the author's:
  `default_prompts()["coordinator"]` (the binding's prompt), `PROMPT_ROLES`
  (role `author`), `state.coordinatorPrompt`, `prompts/coordinator.txt` and
  `--coordinator-prompt-file`. The server checks it (exactly one `[STATEMENT]`,
  no field but `{author_subagents}`) only for a coordinator job; any other job
  keeps the default.
- **Launch.** The first prompt is the coordinator prompt with
  `{author_subagents}` filled in, then the statement, then the related-works block
  (no `subagents` note); `goal_session(..., coordinator=True)`. The runner creates
  `memory/` in the author's working folder, owner-only where the system has such
  permissions; an entry of that name is never removed or replaced, and a folder
  that cannot be made is said in a diagnostic.
- **No subagent tool.** A Codex model of multi-agent version 0 (GPT 5.6 Luna)
  runs the author alone instead: today's author prompts without the `subagents`
  note and without the `multi_agent` feature, and the status **No subagents with
  this model** says that the author works alone instead of coordinating. A Claude
  coordinator with the CLI's own subagents (a job from before TCS Prover's team,
  or a rerun of one that keeps them, `legacyTeam`) gets the same single-author
  launch as `alone`, which
  `claude_cli.goal_session` uses when its CLI offers no subagent tool (`--bare`),
  saying so once per runner; one with TCS Prover's team keeps its team in
  `--bare` mode too (see TCS Prover's MCP server below). That status carries `authorModeEffective: "author"`
  and the model's `modelLabel`, and each start of a coordinating author (its
  **Goal started** or **Goal resumed** status) carries `authorModeEffective:
  "coordinator"`: the server keeps the latest as `state.authorModeEffective`
  (`follow_author_mode()`: `{mode: "author", modelLabel, reason}`, or `null` for
  the recorded mode), in the state only, never in `job-settings.json`; a restart
  and a saved batch read it from the transcript. Each launch replaces it as its
  author starts. The page then says that the author works alone (Run settings,
  and one line in place of the progress log), and the paused author's reply turns
  get `--author-mode author`, so its summary uses the author's prompt.
- **Time checks.** Every whole hour of the job's working time
  (`workflow_elapsed()`, the clock of the time limit: `--elapsed-seconds` plus
  this runner's time without its retry waits; the server keeps machine sleep out
  of the elapsed seconds), the first at 1 h, the runner sends the `checkin`
  prompt as a live instruction (`TimeChecks`): `{elapsed}` reads like `5 h 12 min`
  or `45 min`, `{limit}` like `Time limit: 24 h, about 18 h 48 min left.` or `No
  time limit.` A new runner starts after the time it starts with (resumed at 5 h
  20 min, the next check is at 6 h); a retry after a model-call failure goes on
  after the failed attempt's last check (`checked` in its retry details). Codex
  gets it by `turn/steer` while a root turn runs; one that comes due between turns
  goes with the next turn, and one Codex refuses is sent again in the next turn.
  Claude Code gets it as a stream-json user message, as a live instruction, one
  at a time: Claude takes written input in only at its next step (not while its
  subagents run in the foreground), so no check is written while Claude has not
  taken in the last one, and the next goes once it has, with the time of its
  writing; one given up on at the end of a turn (as a live instruction would be)
  is sent again in the next turn. None is sent while the author pauses or stops,
  or in a reply turn. Each is a status record **Coordinator time check** with
  `elapsedSeconds` and the text sent (Claude Code: when Claude takes it in).
  Checks that came due while none could be sent are one check. An unexpected
  failure of the time checks ends them with a diagnostic that says so; the
  author's watcher goes on pausing, stopping and delivering live instructions.
- **Hand-in guard.** Where proof files can be read
  (`proof_handoff.file_handoff_supported()`), a final reply of the coordinator
  that names no proof file (`names_proof_file()`, which counts a pointer
  `extract_proof_reference()` cannot use, so the critic step refuses it and says
  why) is not handed to the critic: it is recorded as the status **Coordinator
  report** (its first `COORDINATOR_REPORT_CHARS`, 1,600, characters; the whole
  reply stays in the activity log as the coordinator's message), and the
  `continuation` prompt follows. Elsewhere (Windows) the reply is handed on as
  today, and `INLINE_PROOF_NOTE` asks for the proof in it.
- **The author's subagents** (`AuthorSubagents`, for every author with the
  subagent tool) become status records **Author subagent started**,
  **Author subagent finished** and **Author subagent failed** with
  `authorSubagent: true`, `subagentId`, `subagentTask` (the first line of the
  assignment, at most 200 characters), `subagentState` (`started`, `finished` or
  `failed`) and `subagentsRunning` (after the event, while the session saw each
  running subagent start); a failed one's text says why when that is known.
  Codex: the root's completed `collabAgentToolCall` items (a `spawnAgent`'s
  `receiverThreadIds`; `agentsStates` `completed` or `shutdown`, `errored`,
  `interrupted` or `notFound`; a `closeAgent`). Claude Code: the root's Agent
  (or Task) tool calls and their results (`subagent_failure()`; a call turned down
  by a limit is a failure with Claude's own words). Whether Codex 0.160's
  app-server reports a spawn of multi-agent version 2 (the built-in GPT models)
  as such an item has not been checked; `codex exec` does not.
- **Codex settings.** The coordinator's app-server (not the critic) gets
  `COORDINATOR_HINT` as `features.multi_agent_v2.multi_agent_mode_hint_text`
  instead of `SUBAGENT_HINT`, with the same limits as any author with subagents,
  and for models of multi-agent version 2
  `features.multi_agent_v2.default_wait_timeout_ms = 1800000` (30 minutes;
  Codex's own default is 30 s; version 1 has no such setting). A web-cutoff run's
  audit (`retrieval_runtime.verify_rpc`) expects these values exactly in Codex's
  effective settings (`config/read`); whether Codex reports the default wait there
  has not been verified, so a web-cutoff coordinator does not get it
  (`COORDINATOR_WAIT_MS`): it waits Codex's default unless it names a longer
  wait, as its hint asks. The author's inactivity watchdog (30 minutes without an
  event, then a `thread/goal/get` check) does not end an attempt whose root
  waits silently for its subagents while the app-server answers the check
  (tests/test_author_coordinator.py checks this with the scripted app-server).
- **Claude Code settings.** Claude Code, when it offers the subagent tool (to an
  author with Author subagents 1 or more, and to the critic), gets
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` (subagents run in the foreground, so
  their results come back in the turn TCS Prover follows; this also keeps Bash
  commands in the foreground), `CLAUDE_CODE_FORK_SUBAGENT=0` (no copies of the
  caller's conversation) and, with a number,
  `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=N` and
  `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`, set after the filter that removes
  every inherited `CLAUDE*` variable, and its private settings file denies
  `Agent(fork)` (`claude_cli.SUBAGENT_ENVIRONMENT`, `SUBAGENT_DENIED`). Anthropic
  does not document these variables: they were read from the Claude Code
  2.1.289 bundle, and need checking again whenever Claude Code changes. A known
  limit of foreground subagents: a live instruction, a time check or a chat
  message reaches a working Claude coordinator only once its running subagents
  have reported.
- **The page's live window.** The events of the subagents' own conversations
  (`codex_event` records with `root: false`, of the author's subagents and of the
  critic's) keep a window of their own, the newest `SUBAGENT_TRACE_LIMIT` (300),
  beside the newest `TRACE_LIMIT` (1,500) other records, so they never push the
  coordinator's own records out. The window keeps each record's `traceVersion`,
  so `/state?after=N` returns exactly the records added since N that it still
  holds, and a full copy's `traceFrom` is the version after which none is
  missing. The author subagent records, and the coordinator's **Coordinator time
  check** and **Coordinator report** statuses, are pinned, as the critic's
  progress is.
  `transcript.jsonl` keeps every record.
- **The progress log.** For a coordinator job (also one another process runs,
  such as a terminal run) the server's tick reads
  `memory/progress.md` in the author's working folder (`goalWorkspace`, or the run
  folder) whenever its size or modification time changed (`read_progress_log`,
  outside the job lock), as safely as a chat answer: a link, a FIFO or a file with
  a second hard link is never read, the folder is opened without following a link
  (on Windows a link is refused first), at most the file's last 400,004 bytes are
  read, decoded as UTF-8 with invalid bytes replaced, line ends normalized and
  control characters removed. `state.coordinatorProgress` is `null` while there
  is no such file, else `{updatedAt, truncated, chars, bytes, version}`: the
  file's modification time (UTC, ISO 8601), whether the file holds more than the
  text shown, how many characters that text holds (the last 100,000 at most), the
  size of the whole file in bytes, and a digest of the text that changes when it
  changes. The text itself is not in `/state` (which the page polls every 0.7 s):
  `GET /progress-log?job=…` (the same token and job checks as `/chat-answer`;
  `App.progress_log()`) returns `{text, updatedAt, truncated, chars, bytes,
  version}`, or `{text: null, version: null}`, and the page fetches it only when
  the job page opens or the version changes. A file that changes while it is read
  is read again at the next tick. Saved read-only batches show the same, read
  once per request (from the run's own folder when the saved working folder is
  not a folder of the batch catalog, such as a run moved into a batch folder).

## TCS Prover's goal loop and team

Owner's request (13 Nov 2026): choose the coordinator's and each subagent's model
(Codex, Claude, CLI or API), and simulate Codex's goal mode and subagent tools so
that every prompt and tool text is the user's to adjust. What the code does:

- **Every text is in the workflow.** `workflows/author_critic.yaml` holds, beside
  the role and lifecycle prompts, TCS Prover's own texts with "EDIT … HERE"
  comments (`workflow_runner.HARNESS_TEXTS` lists them with their placeholders):
  `goal_continuation` (`{continuation}`, `{objective}`, `{elapsed}`, `{limit}`;
  default: Codex 0.160's `goals/continuation.md`, extracted with `strings` from
  the installed binary, its token lines replaced by time lines; its `update_goal`
  is the name the model sees for TCS Prover's tool), `goal_tool_update_goal`
  with `_status` and `_note` (Codex 0.160's `update_goal` description and status
  argument; the note is TCS Prover's), `claude_goal_harness` (`{objective}`; the
  former `claude_cli.GOAL_HARNESS_PROMPT`, which stays the fallback of a workflow
  without it), `codex_compact_prompt` (Codex's `compact_prompt`; empty, the only
  prompt that may be, passes nothing), `team_note` (`{members}`), `team_member`
  (`{assignment}`) and one description per team tool and argument
  (`team_tool_*`). `prompt_rules()` says what each prompt may use, as `prepare()`
  checks it, as the code fills each text in (`_lifecycle_fields()`: a reply
  turn fills in `{message}` and nothing in a progress summary; the related works
  note and a verify-only round's instruction are used as written);
  `runner/prompt_catalog.py` names, groups and describes every text for the
  Prompts dialog (`GET /prompt-catalog`, `POST /prompt-catalog/check`), each
  entry's `template` saying whether it is filled in (a brace that is no
  placeholder written twice: every text with placeholders, and the progress
  summaries, which have none).
- **Each job keeps its texts.** A new job records `promptSet` (every catalog
  text after the user's edits) in `prompts/prompt-set.json` of its controller
  and run folders; every launch rewrites the controller's copy from the job's
  state and passes `--prompt-set-file` (catalog names; the runner maps
  `convention` to `convention_critic`), reply turns and the writer included, and
  chat messages use the job's `chat` text. A job from before has no `promptSet`
  (`goalLoopRecorded: false`) and reads the live workflow, as before. Beside the
  set, `promptEdits` (`prompts/prompt-edits.json`, `job_prompt_edits()`) keeps
  the texts edited for the job: a new job's `promptOverrides` and role prompts
  that differ from the defaults; a job made from another one (a revision in
  place, a continuation) keeps that one's and adds the request's, or saves none
  when that one saved none. A rerun (app.js `seedPromptOverrides()`) carries
  only `promptEdits` (each still in today's catalog and still differing from
  today's default); without them, every saved text that differs from today's
  default, as before.
- **The goal loop** (`goal_session`, `--goal-loop`). `codex` is Codex's goal mode,
  unchanged. `tcs` starts the app-server with `--disable goals` and sends no
  `thread/goal/*` request: TCS Prover starts every turn and, after one that did
  not end the goal, at once sends `goal_continuation` with the continuation
  prompt first (the coordinator's report answer after a reply the hand-in guard
  kept). The model ends its goal with `update_goal` (`team.GoalTool`): `complete`
  with a final reply in the same turn hands that reply on; `blocked` and
  `paused` are recorded as **Goal status** (`goalStatus`, `note`) and the work
  goes on. The liveness probe is `config/read`. Pause, Stop, the time limit,
  resume, live instructions, chat, time checks, the compaction re-anchor, the
  hand-in guard and the retries are as before. Reply turns of a `tcs` thread
  reopen it without the goals feature. Web-cutoff runs always use `codex`: their
  audited configuration (retrieval/runtime.py) has no place for TCS Prover's
  server. Claude Code authors always ran TCS Prover's loop; their harness note
  is `claude_goal_harness` and their continuation the role's, as before.
- **TCS Prover's MCP server.** `runner/tcs_mcp.py` is a stdio MCP server (standard
  library only, started by the runner's own interpreter with `-I`; it switches
  its stdin and stdout to UTF-8 as it starts, as a piped Python process on
  Windows reads and writes the ANSI code page and `-I` ignores `PYTHONUTF8`)
  that Codex
  starts from its thread settings `mcp_servers.tcs` (kept on while the user's
  servers are off, `required`, `tool_timeout_sec` 2,400, every tool
  `approval_mode: approve`, `omit_tools_from: ["deferred", "code_mode"]`). Its
  environment holds the address of the runner's `team.Service` (127.0.0.1, a
  random port) and a random token; every request carries the token, and the
  service answers nothing else. Claude Code gets no bridge: its `--mcp-config`
  file in the private settings folder names an `sdk` server (`{"type": "sdk", "name": "tcs",
  "timeout": 2400000, "alwaysLoad": true}`, so its tools are never deferred
  behind Claude Code's tool search), whose MCP messages the CLI writes as
  stream-json control requests (`mcp_message`) that `claude_cli.goal_session`
  answers from the same service (`Service.answer()`, each tool call on a thread
  of its own); an init message
  that lists not every team tool ends the attempt, which is retried, so a
  coordinator never works on without its team. Verified against Claude Code 2.1.289
  with a scripted Anthropic endpoint: under `--safe-mode`, which every Claude
  process keeps, it drops every other kind of `--mcp-config` server (a stdio
  server never starts); it loads an `sdk` one, also with `--bare`, before its
  first request, names its tools `mcp__tcs__<tool>` in its init message and to
  the model, runs them only when `--allowedTools` names them (under
  `--permission-prompts none` any other is refused at once), queues a user
  message written during a call until the call's result, cancels a running call
  on an interrupt (`notifications/cancelled`) and ends one that outlives
  `timeout`; the stand-in CLI of tests/test_claude_cli.py follows these shapes.
  The service runs the tools of the providers added to it: `GoalTool` and `Team`.
  Neither the bridge nor `Service.answer()` gives server instructions: Codex
  shows them to the model with the server's tools and Claude Code puts them in
  its system prompt, and every text the model reads from TCS Prover is an
  editable text. Without
  `omit_tools_from` Codex 0.160 defers every MCP tool of a model with tool
  search (GPT 6.1 Sol, GPT 5.5): a code-mode model then reads only that some
  tools are omitted and must search `ALL_TOOLS` for them, a direct model sees
  them only through `tool_search`, and neither reads their descriptions. Not
  deferred but in code mode, a code-mode model (GPT 6.x) calls each tool from a
  script of its `exec` tool (`tools.mcp__tcs__<name>`), whose cell yields after
  30 s, so it waits on a running `team_wait` with Codex's `wait` tool: 16 model
  calls for a wait of 200 s in a capture. Kept out of both, every
  model gets the namespace `mcp__tcs` with each tool under its own name
  (`update_goal`, `team_members`, `team_assign`, `team_wait`, `team_followup`,
  `team_stop`; GPT 6.1 Sol in its request's `additional_tools`, GPT 5.5 and an
  API model that Codex does not know in its `tools`) and calls it directly, a
  function call naming that namespace and the tool, one model call however long
  the tool runs; Codex refuses a call of the flattened name
  (`mcp__tcs__team_members`: "unsupported call"). So every default text names
  each tool by its own name. The app-server reports every call as an
  `mcpToolCall` item (`server` "tcs", `tool` the tool's name), which the job
  page shows as "team_wait · completed".
- **The team** (`--author-team tcs`, the coordinator of every new live job).
  `_execute` builds `team.team_members()` from `--author-subagent-models` (one
  `{model, effort}` per author subagent; none: the coordinator's model and
  effort), appends `team_note` to the coordinator's first prompt after the
  related works, and removes the CLI's subagent tool (no `multi_agent`, and in
  the coordinator's Codex thread `agents.enabled = false`: without it Codex
  0.160 still gives a model of multi-agent version 2 its collaboration tools
  and tells it that it is the root of a team that can spawn sub-agents; a model
  with no Codex subagent tool, such as GPT 5.6 Luna, still coordinates). Tools:
  `team_members`, `team_assign(member, assignment)`, `team_wait(timeout_minutes)`
  (1 to 30, default 30; returns at once when no member works and no report
  waits, else when an assignment finishes or fails, with every report since the
  last wait; one answer carries at most `REPORT_CHARS` (20,000) characters of
  report text, which reports that come back together share
  (`team.report_shares()`: equal shares, the room a shorter one leaves going to
  the longer ones), each cut one with a note naming its file, because Codex
  0.160 cuts a tool's answer beyond its models' 10,000-token output budget out
  of the middle; it ends
  early when TCS Prover sends a live instruction or a time check, or one came in
  the 10 s before it began), `team_followup(member, message)` (continues the
  member's last conversation; refused while it works or without one) and
  `team_stop(member)`. Each assignment is a one-shot run (`member_attempt()`):
  `codex exec --json` (persisted session, `exec fork` for a follow-up or retry,
  `--disable multi_agent`, `--disable goals` and `-c agents.enabled=false` (Codex
  0.160's `codex exec` otherwise offers its own goal tools, which start Codex's
  goal mode in the member's session, a follow-up's `exec fork` inheriting that
  goal, and to GPT 6.1 Sol its collaboration tools without a session limit),
  `--ignore-user-config`, `-a never`, the author's
  confined profile on macOS or `-s workspace-write` with the run's network rule
  elsewhere, the agent variables removed) or `claude -p` (`run_structured` with
  `role="author"`: the author's tools without Agent, its deny rules and Bash
  sandbox, no `--json-schema`) in the author's working folder. The input is
  `team_member` with the assignment; the final reply is the report, saved
  owner-only and never through a link as `team/member-<n>/<k>-report.md`
  (`team.save_report()`: relative to open folders where the system can, else,
  on Windows, each part checked with lstat, a junction included; always a new
  file, after any earlier file or link of its name is removed). A
  failed model call is retried every minute without limit, continuing the
  member's conversation when the attempt worked (`AUTHOR_RETRY_NOTE`); a
  settings problem (`member_problem()`: a missing CLI, a failed login, an unknown
  model or connection, an endpoint that refuses the request) goes to the
  coordinator as the report of a failed assignment, and the member is idle.
  Claude Code first retries a failed API request inside the member's run
  (`CLAUDE_CODE_RETRY_WATCHDOG`; Claude Code 2.1.289 retried a refused key up to
  300 times, about 35 s apart): each retry is a diagnostic of that member
  ("Member n (model): Claude API retry: ..."), and only the run's end reaches
  the rules above. Members are children of the runner: a pause, Stop, the time limit, the end of
  the work and a restart stop them (their records say why); a failed model call
  of the coordinator keeps them working, and the next attempt reads their
  reports: a `team_wait` that the failed attempt's CLI was running ends without
  taking a report or an attention (`Team.release_waits()`, from goal_session's
  cleanup on both paths). A member's retry notes carry `attempt`, counted per
  assignment, so only the first two stay pinned (`important_record()`). Records: **Author subagent started/finished/failed** with
  `subagentMember`, `subagentModel` and `subagentReport`, and the members'
  `codex_event` records (`root: false`). Every record of one assignment, its
  retry notes included, carries its id `subagentId` (`member-<n>-<k>`,
  `team.assignment_id()`): each run numbers its items from `item_0` again, so
  the page and `transcript/view_transcript.py` keep an assignment's rows apart
  by it, and a later assignment never replaces an earlier one's. `k` follows
  the member's highest report number in the working folder
  (`team.highest_report()`), and each assignment creates its empty report file
  as it starts (`team.reserve_report()`), so a later runner of the job (after a
  pause, a restart or the time limit, each with a team of its own) never takes
  an earlier one's numbers, even when that runner ended before the report.
- **Codex's own subagent tools.** A Codex author or coordinator that starts no
  subagent of Codex has `agents.enabled = false` in its thread settings
  (`_author_thread_options(builtin_agents=False)`): one without the
  `multi_agent` feature (an author that works alone, with Author subagents 0 or
  a model that gets no subagent tool, and a coordinator with TCS Prover's team),
  live or web cutoff. Without it Codex 0.160 gives a model of multi-agent
  version 2 (GPT 6.x) its collaboration tools (`spawn_agent`, `wait_agent`, ...)
  also with `multi_agent` off and with a web-cutoff run's settings, where a
  session limit of one thread refuses every spawn. An author with Codex's own
  subagents keeps them: a web-cutoff coordinator, the coordinator of a job from
  before TCS Prover's team, and an author of `--author-mode author` with
  subagents. The setting belongs to the thread, so Codex's effective settings
  (`config/read`), which a web-cutoff run's audit compares, do not show it, and
  Codex keeps it with the thread for good: once a start or resume of the thread
  has it, no later `thread/resume`, even with `agents.enabled = true`, offers
  the thread's model those tools again (both checked with the real Codex). So a
  reply turn of a paused author (a reply, a progress summary, the time limit's
  summary) sends no agents setting and leaves the thread's own: none of the
  tools for an author whose own thread has them off, and, as before, the tools
  for an author with Codex's own subagents, which a reply turn with them off
  would take for the rest of the job. A spawn in such a reply turn is refused
  (`multi_agent` off and a session limit of one thread: Codex 0.160 answered
  "collab spawn failed: agent thread limit reached"), and the author resumed
  after it starts its subagents again. Team members get
  `-c agents.enabled=false` (see above).
- **Checked with the real Codex 0.160** (the installed binary against a scripted
  model server, no model): it starts the bridge from the settings of
  `thread/start` and of `thread/resume` (a resumed thread gets the new address
  and token; Codex keeps no MCP settings with a thread, so a reply turn, which
  passes none, reopens it without TCS Prover's tools); `--disable goals` leaves
  no goal tool of Codex; while a tool call is pending it accepts `turn/steer`
  and answers `config/read` (a live instruction or time check ends a running
  `team_wait` and reaches the model as its next user message), and
  `turn/interrupt` ends the turn, the members stopping with it; `codex exec fork`
  continues a member's conversation; `compact_prompt` (thread setting or `-c`)
  is the prompt of a compaction. With the tools direct (GPT 6.1 Sol, GPT 5.5), a
  `team_wait` is one model call however long it runs: a live instruction, a time
  check, Pause and Stop end a pending one at once, Codex answers the runner's
  inactivity check (`config/read`) while it runs, and a resumed goal, a
  compaction and a reply turn work as before. (Called from code mode, the `exec`
  cell yielded after 30 s while `team_wait` ran, and the model waited on it with
  Codex's `wait` tool, 10 s per call unless it asked for longer.)
- **Old jobs and web cutoffs.** A job from before records no `goalLoop`: it is
  restored as `codex` with `authorTeam` null, so its launches pass `--goal-loop
  codex --author-team builtin` and no texts, also after a revision in place, and a
  continuation of it keeps them. Its state says `legacyTeam: true`, and the
  page's rerun of it (app.js `legacyTeamApplies()`, while the form keeps Codex
  built-in and Same as the coordinator) sends `legacyTeam: true`: the new job
  runs `codex` with the built-in subagents (`legacy_team`), saves its texts like
  any new job, and records `legacyTeam: true` in job-settings.json, so it, its
  reruns and the jobs made from it keep them too. A web-cutoff coordinator
  keeps the CLI's built-in subagents with the coordinator's model (status
  **Built-in subagents**); the server refuses Subagent models with a web
  cutoff.
- **A model without web search.** `codex_web_search_tool()` says whether
  Codex 0.160 offers a model a web search tool in a live run as TCS Prover
  starts it: its hosted `web_search` tool never goes to a "Responses Lite"
  model (`CODEX_CATALOG_LITE`, from Codex 0.160's bundled `models.json`,
  matched as Codex matches it: by the longest catalog name the model's name
  starts with, or after one `namespace/`), and its own `web.run` only to its
  built-in OpenAI provider or a provider that asks for it (named `OpenAI`,
  `supports_standalone_web_search=true` or an `x-openai-actor-authorization`
  header, read from `provider_arguments()`, which sets none of them). So a
  Lite model through an API connection has no search tool. As a live run's
  author starts, `note_no_web_search()` records one pinned diagnostic **No web
  search** (`noWebSearch: true`, `models`) naming the author's and the team
  members' models that have none; the page shows it like the other
  warnings of the activity log. The provider settings are unchanged.

## Job records

Owner's decisions (13 Nov 2026, plan v3 section 1): keep every model request and
response locally, organized for training, from the programs' own copies, with
no relay on the network. Codex's trace changes one thing there: with a trace
root set, Codex 0.160 adds the header `x-codex-inference-call-id` (a fresh id
for each attempt) to each model request it traces and sends over HTTP, that
is, an API connection's requests (not its local compactions, which it does not
trace) and any HTTP fallback (`core/src/client.rs`,
`rollout-trace/src/inference.rs`); requests over a WebSocket (the ChatGPT
login's) carry none, and no setting turns the header off. `runner/records.py`
writes a job's record, `ui/model_calls.py` reads it.

- **Where.** `<state>/records/<run folder name>/` (`providers.state_directory()`,
  `.tcs-prover/` by default), owner-only: every folder 0700, each one made above
  a new folder too (`records._private_folder()`), every file 0600. A record
  written before 14 Nov 2026 has folders of the default mode below its own; they
  are made 0700 when it is next opened (a route, the Records list, a runner or a
  snapshot of its job; `records._private_tree()`, once per process, never
  through a link), and its Codex call lines get `reasoning` then too (below).
  The server makes it
  when a runner, review worker or reply turn of the job first starts and names it
  in `TCS_PROVER_RECORD` (`records.RECORD_ENV`) for that process; `environment()`
  removes the variable from every child, so no CLI or model command learns it. A
  web-cutoff job's processes never get it. Records are never deleted
  automatically: a deleted or trashed job keeps its record, which only `DELETE
  /records/<run>` removes. No fence keeps models away from it (the owner's lowest
  priority): on macOS the confined profile reads no checkout file, so no model
  reads it; on Linux and Windows a live run's commands could.
- **Layout.** `manifest.json` (run folder, title, creation time, `webCutoff`);
  `calls.jsonl` (one line per model request, written when it ends, synced:
  program, process label, thread, times, model, status `completed`, `failed`,
  `cancelled`, `interrupted` or `trace format not recognized`, usage, where its
  request and response are); `items/` (each item's exact JSON once, by the
  sha256 of its compact JSON, in gzip packs of up to `PACK_BYTES`, 64 MB, with
  `index.jsonl`; a pack member is written and synced before its index line);
  `codex/<label>/process.json` (who the process was: role, node, round, label,
  member, model; the round as the activity log names it, so the critic's visit,
  the critic round the convention critic checks, and none for a LaTeX repair,
  `workflow_runner._record_round()`) and `codex/<label>/<bundle>/`
  (`payloads.jsonl`, where each
  trace payload went, and byte copies of the bundle's `manifest.json` and
  `trace.jsonl` as `.gz`); `claude/<label>.json` and `claude/<label>.jsonl.gz`;
  `sources/` (copied session files, gzip-compressed unless they already are);
  `transcript/` (the latest snapshot);
  `problems.jsonl` (what the record could not do); `.lock` (writers of several
  processes take it with `flock`, `msvcrt` on Windows).
- **Codex's trace.** Every Codex process of a live run gets
  `CODEX_ROLLOUT_TRACE_ROOT=<record>/codex-trace/<label>`
  (`workflow_runner._record_codex()`): the author's or coordinator's
  app-server, each reply and summary turn's, every structured `codex exec` call
  (statement reviewer, critic with its independent critics, which share its
  bundle, convention critic, LaTeX editor and repair) and every team member's.
  Codex 0.160 then writes the exact request bodies (system prompt, tools, full
  input, schema, settings), the output items (encrypted reasoning included),
  response and request ids, usage, the code cells with each nested tool call's
  input and result (the search text the model read), and its protocol events.
  The runner reads the trace every `TRACE_POLL_SECONDS` (5 s) while the
  process runs and wholly when it ends (`records.TraceIngest`): a request or
  response payload is stored as its skeleton (the JSON with its `input`,
  `output_items`, `instructions` and `tools` set to null, keeping their place)
  plus item references, a request's input as a change to the thread's previous
  one where it starts the same way (so an API connection's thread, whose
  requests are sent whole, is one chain of changes as long as the thread: over
  a thousand in a long process; `StoredBundle.resolve()` follows it in a loop,
  keeping the last `RESOLVED_INPUTS_KEPT` (16) full inputs in memory); every
  other payload as one object. Each
  stored payload is rebuilt from the record (`json.dumps(..., indent=2,
  ensure_ascii=False)`, which equals serde_json's pretty printer on every
  payload of the audit's captures) and compared byte for byte; one that differs
  (a number Python prints differently, such as `1e-7`) is kept whole. Only then
  is the payload file removed; the bundle goes when its process has ended and
  the byte copies of its manifest and event log read back. A request that never
  ended gets status `interrupted`. A request's line says what its response (a
  failed or cancelled request's partial one too) holds of reasoning
  (`reasoning`: how many reasoning items came back, whether one carried raw
  reasoning text), from which the list gives `hidden_reasoning` as the call
  detail does from the items. A line written before it existed (before 14 Nov
  2026) gets it when its record is next opened (`records._fill_reasoning()`,
  once per process, under the record's lock: `calls.jsonl` is written again
  whole beside itself and swapped in only once complete and synced, so a crash
  leaves it as it was; each line keeps its bytes, the field added at its end; a
  reader that read the file before reads it again from its start), so the list
  reads no response. A bundle whose manifest or events are not of
  schema version 1 (a later Codex: the installed one updates itself) stays as
  it is, its requests marked `trace format not recognized` as their starts are
  read, and the record's problems say so as soon as a running process's bundle
  is found to be one, since its raw files then grow with the process. A
  process's trace folder is held with a `.claim` lock
  while it runs; at the next start (`records.store_leftovers()`: every runner,
  review worker and reply turn of the job, the server for every record when it
  starts, and each snapshot) the folders nobody holds are stored, so a crash
  loses nothing that Codex wrote; a bundle written to in the last
  `LEFTOVER_QUIET_SECONDS` (120 s) waits for a later start, as a Codex process
  whose runner died may still write it. Codex 0.160 does not trace a local
  compaction (that of every model it does not compact remotely, so of every API
  connection's model: core/src/compact.rs, "local compaction streams are left
  untraced"); the model-calls list shows each one from the copied session file
  (its `compacted` record, whose message is the summary), as a row of status
  `not recorded`.
- **Session copies.** Before TCS Prover deletes a CLI session
  (`_codex_session_deleter()`, `claude_cli.session_deleter()`: structured calls,
  their retries' forks and team members), the record copies it
  (`Recorder.keep_codex_session()`: every segment of the thread, plain or
  `.zst`, and the subagent threads whose `session_meta` names it as their
  session or parent, any depth; `keep_claude_session()`:
  `projects/<project>/<session>.jsonl` with its `subagents/` and
  `tool-results/`) into `sources/codex/<thread>/` or `sources/claude/` and reads
  each copy back (copies are gzip-compressed); a session whose copy fails is not
  deleted. A Claude session the CLI never saved (it could not start, or ended
  before saving, as while Claude Code updates itself) has nothing to copy or
  delete, and is neither a problem of the record nor reported as kept. The
  author's or
  coordinator's own Codex thread (with its subagents' threads) and Claude
  session are copied when its session ends without a retry (Pause, Stop, the
  time limit, the end of the work) and after each reply turn, only the files
  that are new or grew (the CLIs only append).
- **Claude Code.** Every stream-json line of every Claude Code process
  (structured calls, members, the author, reply turns), after the redaction of
  transcript records (`claude_cli.scrub_record()`), goes to
  `claude/<label>.jsonl` while the CLI runs and `.jsonl.gz` after
  (`records.ClaudeStream`). A request is one assistant message: its line in
  `calls.jsonl` names its stream lines and the user lines sent since the same
  agent's previous request (a structured call's or team member's first request
  also its prompt, which TCS Prover writes to the CLI's input and the stream
  never repeats: `ClaudeStream.sent_input()` stores it once by sha256 and names
  it in `claude/<label>.json`, its call line's `stdin`) and says whether the
  message had a thinking block (`thinking`, the list's `hidden_reasoning`; for
  a line written before 14 Nov 2026 the list reads the stream), and is written
  once a later line of that agent or
  the turn's result shows it ended (`interrupted` when the CLI ended without a
  result after it). A request that failed is `failed`: Claude Code's own
  report of one it gave up on (an assistant message with
  `is_api_error_message`, model `<synthetic>`, usage all zero) is a call of the
  process's model with its error text and no usage, and each attempt the CLI
  sends again (`system/api_retry`; a subagent's `tool_progress` with
  `subagent_retry`) gets a row of its own with what it sent; a report repeated
  with the same attempt number (a usage-limit wait, every 30 s) counts once.
  The stream gives no per-request output tokens (Claude
  Code's lines carry the message's starting usage). With **Concise** or
  **Detailed** summaries every Claude model, an API connection's too, is asked
  for summarized thinking (`--thinking-display summarized`); only a
  subscription model's reaches the activity log.
- **Snapshots** (`records.snapshot()`, by the server's `App._record_snapshot()`):
  when a runner, review or reply turn ends (a pause, a stop, the time limit, the
  end of the job) and before the job is deleted, the record gets the
  transcript's new complete lines (one more gzip member of
  `transcript/transcript.jsonl.gz`, after a check that the copied part is
  unchanged and that the file has the size the last verified snapshot left in
  `transcript.json`, else a whole copy: an append that a crash or a full disk
  cut short is copied over, and until then readers take the members before it,
  `records.complete_members()`), `job-settings.json` and the controller's
  `prompts/`. What it copies keeps of Codex's answers to `config/read` only
  what the runner keeps now (`records.recorded_transcript()`, below): a
  transcript written before 14 Nov 2026 holds them whole, and a snapshot taken
  before then (its `transcript.json` lacks `settings`,
  `records.SNAPSHOT_SETTINGS`) is copied whole again.
- **Codex's settings in the transcript.** Codex's answer to `config/read` (at
  each start of an author, coordinator, reply or summary turn's app-server, at
  each liveness probe of TCS Prover's goal loop, and in a web-cutoff run's
  isolation check) holds the user's whole Codex configuration: in the first real
  job about 75 KB each time, with every trusted project folder, each MCP
  server's command and environment (which can hold keys), the plugins and where
  each setting came from. TCS Prover acts on all of it, but `transcript.jsonl`
  (and so the record's snapshot and the training download) records only what it
  sets or relies on (`records.recorded_settings()`, with TCS Prover's
  redaction `workflow_runner.recorded_settings()`, about 3 KB): the
  model, provider, reasoning, service tier, sandbox and approval, the permission
  profile in effect, web search, the context window and compaction limit, the
  features it sets, the subagent limits, every other setting the app-server's
  command line set (origin `sessionFlags`: an API connection's provider entry,
  the variables its commands never inherit, the commands' temp folder; of a
  table only those entries, never the user's own beside them) and the MCP
  servers' names, never their definitions, with `leftOut`, one sentence
  saying how many other settings were left out; TCS Prover's redaction runs
  over it. A web-cutoff run's answers (`RPC.web_cutoff`, set from the run's
  policy; the record's `webCutoff` for a copy) also keep the isolation
  settings TCS Prover writes into the private configuration file of each
  process (`records.ISOLATION_SETTINGS`, from
  `retrieval.runtime.configuration()`: project files off, the login shell off,
  the commands' environment policy, the bundled skills turned off, the
  features, Windows' sandbox), of each only what that file set (the answer's
  `user` layer, which the run's isolation check verifies is that file in the
  Codex home TCS Prover gives the process) or the command line: never an entry
  another layer added beside them, nor Codex's own record of the project it
  trusts. Checked against the real Codex 0.160 app-server's answer in a
  web-cutoff preflight of the author and of a structured call (no model call):
  every value TCS Prover wrote is kept, about 3.5 KB of 30 to 36 KB. No reader of the transcript needs more: the job page, the
  controller, the transcript viewer and the web-cutoff audit skip the answer,
  and `retrieval.runtime.verify_rpc()` checks the answer itself. A transcript
  written before 14 Nov 2026 holds each answer whole (about 76 KB in the first
  real job): the record's snapshot of it and the training download keep the
  same part of each (`records.recorded_transcript()`: a `codex_event` whose
  `event` answers with a `config` table and its `origins`, rewritten as the
  server writes a line, every other byte as it is), so neither carries the
  user's whole configuration, an MCP server's environment or headers; the run
  folder's own `transcript.jsonl` is left as it is.
- **Web-cutoff runs.** Their isolation is unchanged: nothing is traced or
  streamed. `records.copy_web_cutoff()` copies the author's session files from
  the run's bundle (`<codexHome>/author/sessions`) and the discovery folders
  (`discovery-state/discovery-*`) into `sources/web-cutoff/`, at each snapshot
  and every `RECORD_COPY_SECONDS` (600 s) while the job runs, well before
  discovery prunes a folder (a day old at least). A login is never copied: a
  discovery call keeps a copy of the ChatGPT login in its private Codex home
  while it runs (and a call stopped by a signal leaves it there), so files
  named `auth.json` or `.credentials.json` (`records.LOGIN_FILES`) are left
  out. Their one-shot calls appear in the model-calls list as rows of status
  `not recorded` (one per attempt's request record).
- **Placeholders** (computed by `ui/model_calls.py`, never stored): `kind`,
  `facts`, `withheldBy`, `why`: `hidden_reasoning` (reasoning tokens; whether
  the encrypted copy or signature is kept), `token_probabilities` (refused by
  OpenAI for reasoning models; not offered by Anthropic), `network_only`
  (per-word arrival times, response headers, moderation notes; Claude Code's
  request itself), `web_cutoff_one_shot`, `trace_format_not_recognized`,
  `interrupted`, `compaction_not_traced` (a local compaction, above).
- **Routes** (the session token like every route; field names in
  `~/tcs-work/fixes/record-contract.md`): `GET /jobs/<id>/model-calls?offset=&limit=`
  (time order, `limit` 1 to 1,000, 100 by default; a failed or cancelled
  call's reason, `error`, in its row and its detail as in `calls.jsonl`), `GET
  /jobs/<id>/model-calls/<callId>` (what was sent new, what came back, usage,
  placeholders; a code cell's nested tool calls from the trace; a Codex call's
  previous request is its thread's in whichever process sent it, as a paused
  author's thread goes on in a summary or reply turn's process and then in the
  resumed author's, and what is new is what this input has beyond that
  request's whole input and output, a WebSocket continuation's chain followed
  back; an item Codex sends again with an id or metadata of its own, its `id`
  or `internal_chat_message_metadata_passthrough`, is the same item,
  `CODEX_OWN_FIELDS`), `GET
  /jobs/<id>/model-calls/<callId>/raw` (the exact request and response), `GET
  /jobs/<id>/training-record` (a streamed ZIP: `requests.jsonl`, one
  self-contained example per request with a WebSocket continuation's input
  expanded; `items.jsonl`; `trajectories.jsonl`, one line per context window
  of a thread, whichever process sent its requests (as for the call detail),
  which a request goes on in when its input starts with the window's items,
  each the same item as for the call detail,
  with each call's input boundary and outputs and the critic round's outcome;
  `rounds.jsonl`; the transcript snapshot; `record/`, the record's own files,
  so nothing it has is left out: `calls.jsonl`, `problems.jsonl`, `sources/`
  (a web-cutoff run's author sessions included), `claude/`, each Codex
  process's `process.json`, each stored trace's `manifest.json`, `trace.jsonl`
  and every payload that is not a request or response (tool calls and their
  results), and raw `codex-trace/` files, `.gz` copies decompressed (locks,
  claims and copies still being written are left out);
  `manifest.json`; sent in HTTP/1.1
  chunks of about `CHUNK_BYTES`, 64 KiB: a download that fails part way gets
  no last chunk, so the browser reports it failed, and its archive no central
  directory, so no ZIP reader takes it for whole), `GET /records` and
  `DELETE /records/<run>` (409 while the job's runner, review or reply turn
  runs). They read the record folder, so a deleted job's record still answers.
  The download runs in the server, which hosts every job, so it is written in
  one pass in time order and keeps only what is still open: each bundle's
  `payloads.jsonl` is read once, a thread's latest request (its full input and
  output items, which a WebSocket continuation is expanded with) until the
  thread's last request, and a Claude Code process's stream until its last
  call; items are read parsed, not rebuilt as text first. Between requests the
  routes keep at most `READ_CACHE_BYTES` (64 MiB) of the Claude streams and
  trace event logs they read (a larger file is read again each time).
  `recordState.problems` shows the newest `PROBLEMS_SHOWN` (50) distinct
  sentences of `problems.jsonl`, which keeps them all. A subagent's session file
  is recognized by its first line (its `session_meta`), of which at most
  `SESSION_META_BYTES` (4 MiB; Codex 0.160 writes about 25 KB) are read.
- **Not recorded on this route:** what only the network carries (stream event
  timing, response headers, moderation metadata, url annotations, the search
  service's `encrypted_output`), Claude Code's request (system prompt, tools,
  parameters) and its web search's inner request, and the input of a web-cutoff
  one-shot call.

## HTTP API notes

The page reads `model_sources` (source menus) and `model_catalog` (model menus)
from the server; each model row carries its `sourceId`, `sourceLabel` and wire
model name. `/api-models` lists `connections`, each with `models` holding the
wire `id`, the display `label` and the stable selection `modelId`. Saving
without a model fetches the catalog on the server; model lists sent by a client
are ignored.

**Web search through a connection.** An OpenAI-format connection's
`webSearch` (off for every connection saved before it existed) turns on Codex
0.160's own web search (web.run), which posts each search to `<base
URL>/alpha/search` with the connection's key and request source. Saving it on
(`POST /api-models` with `webSearch: true`) first sends one probe search
(`probe_web_search()`: `{id, model, commands: {search_query: [{q}]}, settings:
{allowed_callers: ["direct"], external_web_access: true}, max_output_tokens:
2500}`, the shape Codex sends); it stays on only if the answer is HTTP 2xx with
a JSON object whose `output` is text (at most `WEB_SEARCH_PROBE_MAX_BYTES`, 4
MiB, of the answer is read; a longer one is refused, saying so), and the
connection keeps the result as
`webSearchCheck` `{ok, at, message}`, which the editor shows (the connection's
key is removed from the message, as from a Test's, since an error page may
quote the header it received). A saved connection
that stays the same (URL, key, request source, format) is not probed again. A
new live job records the connections of its models that have it on as
`apiWebSearch` in its state and `job-settings.json`; every runner of the job
gets `--set api_web_search=[...]` (the review worker `--api-web-search`), and
`provider_arguments()` then adds
`model_providers.tcs_api.supports_standalone_web_search=true`,
`features.standalone_web_search=true` and
`suppress_unstable_features_warning=true` (the job's web search is `live`). A
job from before records none and searches as before.

A test of one model (`POST /api-models/test` with its `api:<id>` selection id
and wire `model`, or with unsaved settings) sends a request shaped like the ones
runs send: streamed (server-sent events), offering one tool and asking for a
JSON answer at the format's highest reasoning (or the `effort` given), with the
connection's max output, to `<base URL>/responses` (OpenAI) or
`<base URL without /v1>/v1/messages` (Anthropic). When the endpoint refuses it
with an HTTP 4xx other than 401, 403 or 404, one minimal request
(`Reply with OK.`) follows, and the result says whether the endpoint refuses
only the shape of run requests.

**The Setup page.** `GET /setup` checks the programs (it reuses a check
younger than 10 minutes; `?refresh=1` checks again; `?cached=1`, the page's
background read, never starts a process: it returns the last check of any age,
and programs in state `checking` before the first) and lists, per
program, its state, its actions with the exact command each dialog shows, and
the commands to copy, with the default settings' roles (`defaults.source` is
`saved` or `built-in`), the jobs that can still run and use it in their current
or a later step (`Server._setup_stages_ahead`, which also decides the running
jobs that hold an Update or a sign-in), and, for information only, the API
connections of its format. The page adds the New problem form's selected roles
while the form is open (app.js `setupNeeds`), and gives a warning (the sidebar
count, the Overview notice, a red or amber mark) only to a program that the
form, the saved default settings or such a job needs; the built-in settings and
an API connection alone never make a program needed, and any other program is
shown calmly (`tone-idle`). The one exception (`setupProblems`, kind `none`) is
a computer with neither the Codex CLI nor Claude Code installed: one line says
so, counted and on the Overview and at the top of Setup, until a program has a
line of its own. Install offers its sign-in when that login will be used,
including by the built-in settings while nothing is saved and the form is
closed (`setupBuiltInSignIn`), and on a computer with neither program yet.
`POST /setup/action` takes a program, an
action and an option from fixed sets plus the command the dialog showed (a
command that changed is refused with 409 and the new status; a program that
changed since its last check in a way seen without starting anything, such as
where jobs find it, what made it or a file of your own where OpenAI's installer
writes, is checked again first); `GET
/setup/task?id=&after=N` returns a step's state and its output after line N;
`POST /setup/stop` and `POST /setup/input` (the code Claude Code's sign-in page
shows) act on a step. Every route needs the session token, the exact Host and a
matching Origin. Each step runs with no terminal (a new session, or a new
process group without a window on Windows), its output cleaned of escape codes,
keys and tokens. Stop asks its process group, and every process group its
descendants started, to end, and ends them after 10 seconds (on Windows,
taskkill /T ends its tree); on macOS and Linux, what an install or update
leaves in its process group ends with it (after 2 seconds). Checks
run outside the Setup state's lock, so Stop, a step's output and closing never
wait for one.

## Tests

Run the regression suite without model calls:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The suite keeps TCS Prover's private state in a temporary folder it removes at
the end (`tests/isolated_state.py` points `TCS_PROVER_API_MODELS` there before
any TCS Prover module is imported), so it never reads or writes the checkout's
`.tcs-prover/`. The Setup tests (`tests/test_program_setup.py`) run the real
Setup code against fake `codex`, `claude`, `latexmk` and installer programs
(`tests/setup_fakes.py`) in an empty home, with a guard that fails any test that
starts another program.

`TCS_PROVER_LIVE_CLI_TESTS=1 .venv/bin/python -m unittest tests.test_provider_cli_live.ConfinementLiveTests`
checks the run confinement on macOS with the installed Codex CLI and no model:
`codex sandbox` runs a table of probes with each role's generated profile (own
folder, other runs, home, checkout, `/tmp`, `/var/tmp`, `$TMPDIR`, protected
entries, link escapes, and python3, pdftotext, `latexmk -pdf`, git, rg,
apply_patch and zsh heredocs), and an app-server without a model runs the
author's startup check and shows that the author's commands get no agent
variables but can still find a socket with `lsof`.
`tests.test_provider_cli_live.WebCutoffTempLiveTests` does the same for the
generated configurations of the web-cutoff author and critic and their private
temp folders (heredocs, `/tmp`, `/var/tmp`), runs each role's real preflight
checks, and starts and resumes a thread of the web-cutoff author without a model
(its provider is a dead end) to show that the thread's commands get the temp
folder too.

## Running the server

- **One server per runs folder.** Running `python3 web_ui.py` again while it
  runs opens its page (`--no-browser` prints the link) instead of starting a
  second owner of the same jobs. When another program holds port 8765, the
  server takes a free port and prints its link. The browser session key is kept
  in `.tcs-prover/sessions/`, so tabs left open keep working after a restart on
  the same port. See [Troubleshooting](troubleshooting.md#starting-tcs-prover)
  for stopping and restarting.
- **Settings for operators** (trusted local settings the browser cannot
  supply): `TCS_PROVER_API_MODELS` selects another key store;
  `TCS_PROVER_UI_AUTH_HOME` names the local Codex login folder, of which only
  the authentication file is copied; `TCS_PROVER_UI_RETRIEVAL_STATE` names an
  existing controller's retrieval-state folder to share host pacing across old
  and new historical jobs.

### The current-run selection

When `.experiment-control/current-run.json` selects a current batch,
`python3 web_ui.py` opens its existing dashboard. It checks the authenticated
local server before opening the browser and never starts a second owner of
running jobs; `--no-browser` prints the dashboard link. A controller that cannot
be reached for a moment gives an error instead of restoring duplicate jobs. The
selection holds version `1` and absolute `runsRoot`, `controllerStatus` and
`dashboardStatus` paths; it is local machine state and is not committed. That
batch is marked **Current batch**. On the maintainer's computer it is the First
Proof batch `firstproof-batch3-search-v4`, whose ten workspaces stay in that
folder.

### The live gateway

To update the page while an older controller owns running jobs, without
restarting that controller:

```console
.venv/bin/python -m ui.live_gateway \
  --legacy-launch-status /absolute/path/to/launch-status.json \
  --legacy-runs /absolute/path/to/existing-runs \
  --new-runs /absolute/path/to/separate-new-runs \
  --status-file /absolute/path/to/dashboard-status.json \
  --port 8772
```

Open the `url` in the private dashboard status file. The gateway serves the new
page, reads the older jobs' records without restoring them, and forwards their
actions to their original controller; a separate controller handles new jobs.
The two runs folders must not overlap. Keep the gateway running while you use
it; the original controller stays responsible for the older batch, and a
restarted original controller is found again through its launch status file.

- On an older job, **Send** in **Talk to the author** sends a live instruction
  to its working author, as before the chat; the chat does not list it or show
  an answer, and an action the original controller does not have is refused
  with a note. In a web-cutoff run it sends none, because the original
  controller does not record live instructions in the run's audit log.
- API connections, the running-jobs limit and saved settings go to the
  controller that handles new jobs. An older server without the new search
  features cannot turn on historical search through the new page.
- The gateway cannot make batches: its pages show no **New batch** or **New
  problem in this batch**, and every job of the new controller appears in its
  card. Its cards name no shared related works, so its pages show none.
- The gateway does not forward the Setup page: `/?setup=1` loads, its `/setup`
  routes answer 404, and the page says to open TCS Prover from its own launch
  link (a step's jobs protection sees only one controller's jobs).
- Closing the gateway (Ctrl-C or SIGTERM) pauses the new controller's jobs as
  closing the server does.
