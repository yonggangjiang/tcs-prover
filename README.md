# TCS Prover

A local web UI that turns a theoretical-computer-science statement into a
persistent Codex proof search, independently audits and repairs the candidate,
then produces clean LaTeX. Algorithmic tasks can be entered as statements too.

The Codex CLI is the local agent runtime for threads, goals, tools, and
subagents. Astra with Ultra reasoning and Fast generation are the defaults for every
role. DeepSeek V4 Pro is an additional
model option that uses the same harness pipeline through DeepSeek's official
API.


## Install and run

Requires Python 3.9+ and the Codex CLI. Install Codex CLI through this
[official website](https://learn.chatgpt.com/docs/codex/cli).

```bash
codex login
git clone https://github.com/yonggangjiang/tcs-prover.git
cd tcs-prover
python3 -m pip install -r requirements.txt
python3 web_ui.py
```

The Web UI opens locally with Astra, Ultra, and Fast defaults for
the reviewer, proof author, critic, and LaTeX writer. The DeepSeek setup below
is needed only when you select DeepSeek for one or more roles.

## How to use DeepSeek

### 1. Obtain an official API key

Create a key in your DeepSeek account. TCS Prover uses only
`DEEPSEEK_API_KEY`; the key must belong to the official DeepSeek API account
that will be billed for the model calls.

### 2. Make the key available to TCS Prover

In the same Terminal window where you will start the Web UI, run:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

This lasts for the lifetime of that shell. Editing or updating TCS Prover does
not erase the variable, so restarting from the same Terminal does not require
another `export`. A newly opened Terminal does require it unless you add the
same export to your shell startup configuration.

To check the variable without printing the secret:

```bash
python3 -c 'import os; print("DeepSeek key is set" if os.environ.get("DEEPSEEK_API_KEY") else "DeepSeek key is missing")'
```

### 3. Start the Web UI

From the repository directory:

```bash
python3 web_ui.py
```

Open **Advanced**, then select **DeepSeek V4 Pro — Official API** for any roles
you want DeepSeek to handle. Selecting it for all four model roles means that
the statement reviewer, author, three independent critic audits and
coordinator, and LaTeX writer all use DeepSeek. A Codex/ChatGPT login is not
required for roles that use DeepSeek.

DeepSeek exposes `high` and `max` reasoning in this harness. The shared menu is
normalized as follows: `low`, `medium`, and `high` run as DeepSeek `high`;
`xhigh`, `max`, and `ultra` run as DeepSeek `max`. DeepSeek always uses Standard
speed because the Fast service tier is specific to OpenAI models.

The harness passes the selected key through environment-backed authentication
to each relevant child process, injects an isolated
Responses-provider configuration, and loads model metadata based on DeepSeek's
[official Codex integration](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/).
It does not put the key in process arguments, edit `~/.codex/config.toml`, send
the key through the UI, or save it in run artifacts.

If the UI reports that `DEEPSEEK_API_KEY` is missing, stop the Web UI, export
the key in the Terminal that will launch it, and start it again. If DeepSeek
returns an authentication error, verify that this is an active official API
key and that no extra quotation marks were copied into its value.

You can type your open problem into the text box and click “Check Statement.” The system will first revise the statement to remove ambiguities and handle corner cases, then ask you to approve or reject the revised version.

If you approve it, persistent reasoning will begin and continue until a solution
is found or the time limit is reached. The candidate passes through an
independent critic loop. Accepted proofs are then pruned into readable LaTeX.

Options for changing the default model, time limit, and other settings are under
**Advanced**. Astra (`gpt-6-astra`) is the default model for every node.

## Terminal runs from Markdown

On a server without a browser, put the complete statement in any UTF-8 Markdown
file. Paragraphs, quotes, mathematical backslashes, and line breaks can be
copied into the file without escaping. Then pass the file directly to
`web_ui.py`:

```bash
python3 web_ui.py statement.md
```

This sends the entire file directly to the proof author, exactly like enabling
**Skip statement review** in Statement mode. It does not start an HTTP server or
open a browser. With no command-line overrides, it uses the same defaults as the
web UI: 2 critic rounds, a 168-hour total workflow limit, Astra and Ultra for all proof
roles, the built-in role prompts, and Fast generation speed. The
activity log requests concise public reasoning summaries by default.

### Optional settings

Put overrides after the file or folder. The requested single-dash spelling,
double-dash camelCase spelling, and conventional double-dash kebab-case spelling
are all accepted. For example:

```bash
python3 web_ui.py statement.md -criticRounds 6 -thinkingHours 36
python3 web_ui.py statement.md --author-model gpt-5.6-terra --speed-mode standard
```

| Option | Default | Meaning |
| --- | --- | --- |
| `-criticRounds N` | `2` | Accept after N consecutive critic rounds that fix all reported bugs, without another audit; `1` to `100`. A clean pass accepts immediately; rejection resets the count. |
| `-thinkingHours HOURS` | `168` | Total elapsed-workflow limit; greater than `0` and at most `168`. It interrupts an active author, but not an active critic or the final LaTeX editor. |
| `-authorModel MODEL` | `gpt-6-astra` | Author model: `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, or official `deepseek-v4-pro`. |
| `-criticModel MODEL` | `gpt-6-astra` | Critic model; same choices as the author. |
| `-writerModel MODEL` | `gpt-6-astra` | LaTeX writer model; same choices as the author. |
| `-reasoningEffort LEVEL` | `ultra` | Fallback effort for all three roles: `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`. |
| `-authorEffort LEVEL` | shared effort | Override only the author effort. |
| `-criticEffort LEVEL` | shared effort | Override only the critic effort. |
| `-writerEffort LEVEL` | shared effort | Override only the LaTeX writer effort. |
| `-speedMode MODE` | `fast` | `standard` for normal speed or `fast` for ChatGPT's accelerated generation. DeepSeek calls always use standard service because Fast is provider-specific. |
| `-reasoningSummary LEVEL` | `concise` | Public activity-log summaries: `none`, `concise`, or `detailed`. This never exposes private chain-of-thought. |
| `-authorPromptFile PATH` | built-in prompt | Load a UTF-8 author prompt; it must contain exactly one `[STATEMENT]`. |
| `-criticPromptFile PATH` | built-in prompt | Load a UTF-8 critic prompt. |
| `-finalPromptFile PATH` | built-in prompt | Load a UTF-8 LaTeX prompt. |

Prompt-file paths are resolved from the terminal's current working directory.
Run `python3 web_ui.py --help` to see every spelling and allowed value.

## How to use checkpoints

TCS Prover writes durable artifacts while a job progresses. When the Web UI is
started again, it scans `runs/*/transcript.jsonl`, restores the historical job
cards, and lists every resumable critic checkpoint found in those run folders.
You do not need to keep the old browser tab open.

The home-page checkpoint buttons have exact stage semantics:

| Checkpoint shown | What is already saved | What continuation runs |
| --- | --- | --- |
| **Saved proof candidate** | Checked statement and complete proof | Starts a fresh three-auditor critic review; later audit files are intentionally not reused when this earlier checkpoint is selected. |
| **Independent audits 1/3** or **2/3** | Candidate plus the displayed completed reports | Restores those reports and runs only the missing auditors. |
| **Independent audits 3/3** | Candidate plus all three reports | Skips all auditors and starts coordinator adjudication. |
| **Critic coordinator — failed/interrupted** | Candidate and all three reports | Retries only the coordinator. |

To continue from the Web UI:

1. Stop the old server if it is still running, then run `python3 web_ui.py`.
2. On the home screen, locate the historical job by its full start/finish time.
3. Click the continuation button beside the checkpoint you want.
4. Confirm the action. TCS Prover creates a new job and opens it immediately.

The source run is never overwritten. The new job copies the selected candidate,
compatible audit checkpoint, role prompts, model choices, reasoning efforts,
speed, critic-round limit, and workflow time limit. Old saved model routes that
are no longer selectable are migrated to the official DeepSeek model when the
job is restored.

Audit reuse requires a matching proof, model, effort, instructions, and audit
focus assignment; restored reports must satisfy the current response schema;
an incompatible audit file is ignored while the earlier proof-candidate
checkpoint remains available. Very old runs that predate `job-settings.json`
can restore only choices visible in their transcript. A role that never ran has
no recorded model choice and therefore uses the current default when resumed.
The continuation confirmation names these unknown legacy roles before a new
paid request starts.

A checkpoint exists only after a complete artifact has been written. It cannot
restore the middle of an in-flight model response or its private reasoning. If
a process stops between two checkpoints, continuation begins from the most
recent checkpoint listed on the job card.

### Continue a manually stopped job

Jobs stopped with the Web UI's **Stop** button remain visible as **Stopped at
review**, **solve**, **repair**, **critic**, **final**, or **failure** after the
server is restarted. On the home page, use the stage-specific continuation
button:

| Stopped stage | Button | Continuation boundary |
| --- | --- | --- |
| Statement review | **Retry statement review** | Starts a new review request from `review-input.json`, including the current statement and feedback, plus the saved prompt and role settings. Older runs without this artifact warn that only their original draft and no feedback can be recovered. |
| Proof author, repair, or interrupted failure summary | **Continue proof author** | Starts a new author thread with the exact checked statement and any compatible `author-memory.json`, and re-queues saved live user instructions. It does not treat visible partial text as a complete proof. A stopped repair originating from critic-resume safely re-enters its critic checkpoint instead, preserving the recovery assignment. |
| Critic | **Continue critic** | Restores the latest compatible proof and paid independent-audit checkpoint, so completed audits are not repeated. |
| LaTeX editor | **Retry LaTeX editor** | Starts only the final editor from `final-input.json`, using the normal workflow's original statement-plus-latest-solution prompt contract. LaTeX-only jobs reuse `latex-input.md` and their separate polish contract. Older jobs without an exact final input safely fall back to their latest critic checkpoint. |

Every action creates a new run folder and leaves the stopped source run
unchanged. An interrupted provider request, model context, subagent process, or
private reasoning cannot be resumed. Review and final requests therefore retry
from their exact saved public input, while author continuation uses the bounded
controller-maintained durable memory. Historical runs created before
`manual-stop.json` are recognized from their existing `Stop requested`
transcript entry.

### Command-line fallback

If a run contains `SOLUTION.md` or `saved-candidate.md` but no checkpoint button
is available, open a new critic job explicitly:

```bash
python3 web_ui.py --resume-critic runs/2026-08-26_16-33-26_example
```

This loads the saved complete proof, checked statement, audit checkpoint when
present, and role prompts into a new browser-visible job. A clean critic pass
continues to the LaTeX editor. A critic rejection returns the exact candidate
and bug report to the normal proof-author repair loop; the harness workflow is
not shortened or replaced.

## Parallel folder runs

Passing a folder starts an independent proof for every top-level `.md` file in
that folder:

```bash
python3 web_ui.py statements/
python3 web_ui.py statements/ -criticRounds 6 -authorEffort max
```

The lookup is case-insensitive (`.md` and `.MD` both work), deterministic, and
non-recursive. All matching files and shared settings are validated before any
job starts. The jobs then run concurrently, and every override applies to every
file. Be aware that a large folder can therefore use many simultaneous Codex
jobs and credits.

Terminal output is concise by default: it reports only the current workflow
step, diagnostics, errors, and the start/finish result for each input file.
Prompts, model events, reasoning summaries, tool activity, and proof bodies are
not printed. They remain available in each proof's complete `transcript.jsonl`
and other artifacts under its separate directory in `runs/`. Pass
`--verbose-events` to restore the full public JSONL event stream; folder events
then include an `inputFile` field. A failed job does not cancel its siblings.
The exit status is `0` when every proof succeeds, `1` for invalid input or any
failed proof, and `130` after Ctrl-C. Ctrl-C stops all active folder jobs and
their subprocess trees.

## Read a transcript as a human narrative

`transcript.jsonl` is the complete machine-readable event log and is deliberately
verbose. The local browser viewer organizes workflow stages, public reasoning
summaries, model messages, subagent/tool activity, critic checks, and final or
failure results without app-server protocol noise:

```bash
python3 transcript/view_transcript.py runs/2026-08-03_16-20-24_example/
python3 transcript/view_transcript.py runs/2026-08-03_16-20-24_example/transcript.jsonl
python3 transcript/view_transcript.py                       # newest run
```

The UI provides stage and activity filters, full-text search, root-only and
compact views, expandable long entries, critic verdict cards, automatic
full-file loading with pause/resume, and live updates. It binds only to local
host and protects its local API with a random per-launch token.

For terminal or file output, use the text mode:

```bash
python3 transcript/view_transcript.py RUN --text
python3 transcript/view_transcript.py RUN --text --follow
python3 transcript/view_transcript.py RUN --output readable.txt
```

In text mode, prompt bodies are hidden by default because they are long; add
`--prompts` to show them. Use `--stage solve`, `--stage critic`, `--root-only`,
`--no-tools`, or `--max-text 0` to adjust the view. Run
`python3 transcript/view_transcript.py --help` for all options. Both views display the
public reasoning summaries retained by TCS Prover, not private chain-of-thought.
They read incrementally, so even very large transcripts do not need to fit in
memory.

Choose **Statement** to review or edit a rough problem before approval. Include
the computational model, problem description, and asymptotic goal in the
statement for algorithmic tasks. Choose **LaTeX polish** to edit an existing
theorem and proof using only the final LaTeX node.
**Advanced** controls each node's model, reasoning effort, prompt, public activity-log
detail, workflow time limit, and critic-round limit. The log can show status
only or request concise or detailed model-generated summaries. Its **Statement review only** option runs just the
reviewer, saves the checked statement and reviewer notes, and finishes without
starting the proof author, critic, or LaTeX editor. This option and **Skip
statement review** are mutually exclusive. Jobs run in parallel. **Show
details** displays the exact application prompts and returned model text.
Private records and outputs are stored under `runs/`.

Every job card shows its full local start and finish date and time, including
seconds. Active jobs show that they have not finished yet, so repeated problem
titles remain distinguishable.

While a proof author or author repair is running, **Guide the running proof
author** sends a live instruction into that same author turn. The prefilled
instruction stops further experiments and code execution and redirects the
author to symbolic proof work. This does not restart the Goal or replace the
thread. The control is intentionally unavailable during statement review,
critic audits, failure summaries, and final LaTeX editing.

## Workflow

```mermaid
flowchart LR
    S["Statement reviewer"] --> H{"Human approves?"}
    S -- "review only" --> R["Checked statement and notes"]
    H -- "revise" --> S
    H -- "approve" --> A["Proof author"]
    A -- "blocked: continue" --> A
    A -- "deadline" --> F["Failure summary"]
    A -- "proof" --> C["Independent critic"]
    C -- "reject" --> A
    C -- "fixed, below limit: recheck" --> C
    C -- "clean pass" --> L["LaTeX editor"]
    C -- "all fixes at round limit" --> L
```

### 1. Statement reviewer

This node lets the user start with convenient informal language while preventing
a model from silently solving a different problem. To use it without requesting
a proof, open **Advanced**, enable **Statement review only**, and submit the
statement. The completed result remains available to copy from the review page
and is saved as `checked-statement.md` in that job's run directory. Only the
reviewer model, reviewer reasoning effort, reviewer prompt, and generation speed
apply in this mode.

For example, graph-algorithm papers commonly write a bound such as
`m log² n`. A literal model may object that `m` can be smaller than `n` and call
the target impossible. The review step states the intended convention and writes
it as `(m+n) log² n` instead of letting that mismatch
derail the proof search.

### 2. Proof author

The author uses the durable-state, multi-agent prompt adapted from OpenAI's
[Cycle Double Cover prompt](https://cdn.openai.com/pdf/04d1d1e4-bc75-476a-97cf-49055cd98d31/cdc_prompt.pdf).

All author models run inside the same Codex app-server lifecycle. The controller
sets the same persistent Goal through `thread/goal/set`, enables the same native
multi-agent tools, and uses the same continuation, deadline, compaction, and
memory logic. When DeepSeek V4 Pro is selected, only the model provider changes;
subagents inherit that author thread's selected provider and base model.

The author keeps the Goal active: a blocked or prematurely ended author turn is
continued until it returns a solution or reaches the user-set total elapsed-time
limit. The web limit uses the same clock displayed at the top of the page, so
statement review and approval time count too. If the limit expires during a
critic round, that critic work continues; an accepted proof proceeds to LaTeX,
while a rejection goes to the failure-summary node instead of starting another
author revision.

Each proof run also owns two private controller-written files in its `runs/...`
directory. `author-anchor.md` contains the exact original author prompt and
statement. `author-memory.json` is a bounded ledger of candidate fingerprints,
approach outcomes, blocked routes, critic feedback, and unresolved obligations.
Writes are atomic and best-effort private. The JSON file is capped at 64 KiB,
and the historical snapshot placed in a model request is independently capped
at 24 KiB; old records are folded into counters and a rolling digest.

The controller re-injects the immutable anchor and current memory after every
root-author context compaction, every explicit continuation, and every critic
rejection. A compaction re-anchor is steered into the same active turn; it does
not replace the author thread. Memory records repeated candidates but does not
skip calls, end the search, or otherwise alter the proof workflow.

### 3. Independent critic

The critic independently checks the author's proof and repairs every issue it
can. A clean pass accepts the proof immediately. If every reported bug is
fixable, the repaired proof goes to a fresh critic until the configured limit
is reached. After 2 consecutive such rounds by default, the latest repaired
proof is accepted without another check and sent to the LaTeX editor.

Unresolved bugs cause rejection: the author resumes its existing thread with
the critic's feedback, and the consecutive-round count resets. A rejection at
the round limit still returns to the author; it never counts as acceptance.

“Independent” here means fresh contexts. A human or different-family review
remains advisable. The Python controller launches three explicit auditor model
requests concurrently, each with the full statement and candidate plus a
distinct hostile-audit focus. It verifies that all three return before starting
a separate coordinating critic. The coordinator cannot spawn or wait for
subagents, eliminating model-managed spawn and payload-delivery failures.
Structured requests emit a controller heartbeat every 30 seconds. Each
heartbeat verifies that the local request process is alive and separately
reports lifecycle events and actual content events; DeepSeek does not expose
whether a silent server-side request is computing or queued. DeepSeek auditors
remain at `high` effort and receive one thirty-minute attempt; the coordinator
also receives one thirty-minute attempt at the selected critic effort. A
completed independent audit is checkpointed immediately, and a critic resume
searches sibling jobs for exact-proof matches instead of paying to repeat them,
even if the original proof job was selected by mistake. Failed model launches
retain a bounded, credential-redacted provider/CLI diagnostic. A stalled request
cannot silently trigger another equally expensive retry. Structured role
prompts include the JSON contract explicitly because V4 Pro supports JSON
output but does not natively enforce arbitrary JSON Schema.

### 4. LaTeX editor

Correct-looking generated proofs are often repetitive, poorly ordered, or hard
to read. After critic acceptance, this node preserves the mathematics while
rewriting it as a compact, structured TCS-style LaTeX proof. It also runs
independently through **LaTeX polish**.
In practice this often makes the output substantially easier to read.

## Project structure

The root has two Python entry points. `workflow_runner.py` contains the entire
workflow engine, including Codex transport, persistent sessions, deadlines, and
durable memory. `web_ui.py` launches the UI or Markdown proof jobs. The
`workflows/` directory contains exactly two YAML definitions:

```text
workflow_runner.py          Complete workflow engine and workflow CLI
web_ui.py                   UI and Markdown-job launcher
workflows/
  author_critic.yaml         Author/critic prompts, response schema, and logic
  clean_up.yaml              LaTeX prompts, response schema, and logic
transcript/
  view_transcript.py         Transcript reader, CLI, and viewer server
  transcript_ui/            Transcript viewer HTML, JavaScript, and CSS
ui/
  server.py                 HTTP endpoints, job state, and process management
  review.py                 Independent statement-review procedure
  cli.py                    UI startup and Markdown file/folder runs
  index.html, app.js, styles.css
docs/workflows.md           Workflow authoring guide and full YAML reference
tests/                      Offline regression tests
```

The UI starts the root workflow runner in each job's private workspace and
passes it the appropriate YAML files. Statement review remains independent of
the workflow graphs. UI and Markdown-job artifacts still live under the
repository's `runs/` folder, regardless of the launcher's working directory.

## Workflow files and executor

Each YAML file contains `nodes` and `prompts`. Node names are arbitrary; the
first node is the entry point. `run: structured` makes a model call with a
response shape, while `run: goal` starts or resumes a persistent task. The YAML
defines inputs, result checks, state updates, and transitions, including the
critic's repeat limit.

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
python3 workflow_runner.py summarize.yaml < notes.md
```

The [workflow authoring guide](docs/workflows.md) explains every YAML entry,
response shorthand and full schemas, branching and repeat limits, expressions,
persistent-session prompts, model overrides, and offline validation. It includes
a complete editing workflow with decisions and a configurable repeat limit.
Keep custom definitions outside the bundled `workflows/` directory.

Run the graphs directly with UTF-8 input on standard input:

```bash
python3 workflow_runner.py workflows/author_critic.yaml workflows/clean_up.yaml < statement.md
python3 workflow_runner.py workflows/clean_up.yaml < theorem-and-proof.md
```

The first command runs proof search, criticism, and LaTeX cleanup; the second
runs cleanup alone. Both skip statement review. Use
`python3 workflow_runner.py --help` for model, reasoning, time-limit, critic-round,
and prompt overrides. `--model` sets the fallback model for any workflow;
`--set NAME=VALUE` supplies arbitrary named options and accepts JSON values.
Node `role` settings can use options such as `editor_model` and `editor_effort`.

The CLI initializes `state.input`, `state.statement`, and `state.source` with the
same input after trimming surrounding whitespace. Chained graphs share state,
and `state.failed` stops the chain. Output is JSONL events, ending with
`workflow_result` on success and its
`output` field from `state.output`. The Python API is
`execute(path, state, options=...)` or `execute_workflows(paths, state, options)`.
All graphs are validated before a chain starts. `requirements.txt` installs
PyYAML for loading definitions and jsonschema for checking response schemas.

Run the regression suite without making model calls:

```bash
python3 -m unittest discover -s tests -v
```
