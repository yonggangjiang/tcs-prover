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
  claude_cli.py             Claude Code transport for structured roles and the author
  confinement.py            Per-run read/write confinement of model processes (macOS)
  proof_handoff.py          Verified capture of an author's PROOF_FILE
ui/
  server.py                 HTTP endpoints, job state and process management
  cli.py                    Startup and Markdown file and folder runs
  review.py                 Independent statement review
  batches.py                Saved run folders shown as read-only batches
  named_batches.py          Batches made with New batch (runs/.controller/batches.json)
  presets.py                Saved New problem settings (Save… and Load…)
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
- `runs/.uploads`: staged related-work uploads, removed after 24 hours.
- `runs/.trash/`: deleted jobs, kept for 30 days.
- `.tcs-prover/`: the private state (see [Sandbox and network](sandbox.md)),
  including `program-folders.json`, the folders the Setup page remembered.
  Setup's own output is kept in memory only.
- Private per-call folders are pruned once they are a day old, beyond the newest
  50 critic snapshots and historical critic sessions and 200 historical
  discovery runs.
- Structured calls keep their program session (Codex without `--ephemeral`,
  Claude with `--session-id`; retries fork it with `codex exec fork <id>` or
  `claude --resume <id> --fork-session`) only while the call runs, and delete it
  when it ends (`codex delete --force`, which also removes subagent threads, or
  the Claude transcript files). Until then Codex keeps it under
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
  (`critic_count` for the critic, `author_subagents` for the author), the node
  gets the `multi_agent` feature exactly when the number is 1 or more, Codex gets
  its own limits for it (see [The workflow](workflow.md#subagents-what-the-program-enforces)),
  and a structured node's instructions read it as `{count}`. The author's
  `subagents` prompt reads it as `{author_subagents}` and is added to its first
  prompt only when it is 1 or more.
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
author, and sends the `summary` or `conversation` prompt, after the notice when
there is one, as one turn in the saved conversation named by
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

## HTTP API notes

The page reads `model_sources` (source menus) and `model_catalog` (model menus)
from the server; each model row carries its `sourceId`, `sourceLabel` and wire
model name. `/api-models` lists `connections`, each with `models` holding the
wire `id`, the display `label` and the stable selection `modelId`. Saving
without a model fetches the catalog on the server; model lists sent by a client
are ignored.

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
the commands to copy, with the New problem roles, the jobs that can still run
and the API connections that need it. `POST /setup/action` takes a program, an
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
  card.
- The gateway does not forward the Setup page: `/?setup=1` loads, its `/setup`
  routes answer 404, and the page says to open TCS Prover from its own launch
  link (a step's jobs protection sees only one controller's jobs).
- Closing the gateway (Ctrl-C or SIGTERM) pauses the new controller's jobs as
  closing the server does.
