# Models and API connections

Every role (statement reviewer, proof author, critic, LaTeX writer) runs through
one of two programs on your computer: the **Codex CLI** (the default) or
**Claude Code**. They provide the author's conversation and every model call.
GPT 6.1 Sol at Max reasoning and Standard speed is the default for every role,
and the critic's choice also covers the convention critic.

Choose per role under **Advanced settings** on the New problem page, or on
**Models & API** in the sidebar: a **Source**, then a **Model**, then its
**Reasoning** level. Sources are **Codex CLI**, **Claude Code** and each saved
API connection; each model offers only the reasoning levels it supports.
`runner/providers.py` is the single registry of model ids, programs and efforts.

| Source | Models | Needs |
| --- | --- | --- |
| Codex CLI | `gpt-6.1-sol` (GPT 6.1 Sol, the default), `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna` | the Codex CLI, signed in with ChatGPT (`codex login`) |
| Claude Code | `claude-fable-5-1` (Claude Fable 5.1), `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` | Claude Code, signed in with your Claude subscription |
| API connection | the models your endpoint lists | a saved key, and the program for its format installed but not signed in: OpenAI format through the Codex CLI, Anthropic format through Claude Code |

The install commands are in the [README](../README.md#what-you-need-for-models).

## What a start checks

A launch or resume checks only that each selected model is configured and, for
an API model, has its saved key; a run with a web cutoff timestamp also needs
Codex CLI models for its proof roles and the native Codex binary, and with
**Writing & polishing** on also MacTeX in `/usr/local/texlive` and macOS's
`sandbox-exec` for its isolated LaTeX compiler (see
[Web search cutoff](web-cutoff.md#what-a-timestamp-run-checks)). No other
installed program is checked there. A missing program never stops a start (a
run with a web cutoff timestamp is the exception: its checks before launch need
the native Codex binary, and with Writing & polishing on MacTeX; a job that
fails them is held until you install what is missing and choose **Re-run
preflight**). The model call that needs the program says so in the activity log
and is retried every minute, so the job continues by itself once the program is
installed (see [Retries and what needs you](retries.md)).

Under each role menu a chip names the program, or for an API model its format,
program and host (for example *API · Anthropic format via Claude Code ·
gateway.example.com*). It never checks whether the program is installed.

## Codex CLI

The four GPT models use your ChatGPT login and offer `low` through `max`.

- **Fast** speed applies to these models only: ChatGPT's accelerated
  generation, up to 2× on GPT 6.1 Sol and Astra and 1.5× on the other models.
  It can use up credits faster.
- **Ultra** is no longer offered. It was Max plus Codex's own task delegation,
  which had no count; subagents now follow the run's **Author subagents** and
  **Independent critics** settings. A saved job, script or flag that says
  `ultra` runs as `max`, and the activity log says so once.
- The older `gpt-5.6-sol`, `gpt-5.6-terra` and `gpt-5.6-luna` are still
  accepted for saved runs and scripts, but no longer listed.

## Claude Code

Install Claude Code, run `claude` once and sign in with your Claude
subscription. Its models offer `low`, `medium`, `high`, `xhigh` and `max` (a
saved `ultra` runs as `max`) and always run at standard speed.
Earlier versions of TCS Prover called this program the Claude CLI; the activity
logs of jobs run then keep that name.

- **Tools.** Every Claude process gets an explicit tool list and no MCP servers,
  user settings, skills, plugins, hooks or `CLAUDE.md`, and never waits for a
  permission prompt (see [Sandbox and network](sandbox.md#claude-code)).
  Web-search tools follow the job's search mode. Only the critic, and the author
  with **Author subagents** 1 or more, get the subagent tool (Task); Claude Code
  has no setting for the number, so only the prompt limits it.
- If Claude Code offers a tool beyond the list (its subagent tool counts as one
  tool under either name, Task or Agent), the attempt ends and the next one
  denies that tool with `--disallowedTools`. A tool this does not remove stops
  the job for safety.
- **Goals.** Claude has no native goals: the author ends each turn with a short
  structured status, and TCS Prover continues the conversation until the author
  reports the goal complete. Pause, Resume, chat messages, context compaction
  and critic repairs work as with Codex.
- **Reasoning summaries.** **Concise** and **Detailed** both show Anthropic's
  summarized thinking (Claude has one summary level); **Status only** shows
  none.
- **Its own retries.** Claude Code waits out 429 and 529 responses within the
  same request (`CLAUDE_CODE_RETRY_WATCHDOG`); the activity log then shows a
  **Claude API retry** line, for example "status 529, retrying in 32 s (attempt
  3 of 10)".
- **Saved conversations.** Claude Code deletes a conversation
  `cleanupPeriodDays` (30 by default) after its last activity, whenever any
  Claude Code run starts with default settings. TCS Prover keeps a private copy
  of each author conversation in `.tcs-prover/claude-transcripts/` and puts it
  back when Claude no longer has it. When it cannot be reopened (no copy, a
  missing saved ID, or the same refusal three times in a row), the author
  continues by itself in a new conversation in the same run folder, from the
  resume prompt and, after a rejection, the critic's last report; the time
  budget continues, and the activity log shows **Conversation replaced**. A
  Claude Code process left by an earlier runner that still holds the
  conversation is stopped first. To keep conversations longer, set for example
  `"cleanupPeriodDays": 365` in `~/.claude/settings.json`.

## API connections

**Models & API → API connections** (also under **Advanced settings**) saves an
endpoint and its key once, then discovers every model the key can use; you never
type a model id.

| Field | Meaning |
| --- | --- |
| Format | **OpenAI-compatible** (Responses API) or **Anthropic** (Messages API). |
| Base URL | For example `https://api.openai.com/v1` or `https://api.anthropic.com`. HTTPS is required except for `localhost`. Credentials, query strings and fragments are refused, and so that the host shown is the host contacted, so are non-ASCII hosts (use the `xn--` form), backslashes, percent-encoded hosts, IPv4 addresses other than four decimal numbers, and `.` or `..` segments. |
| API key | Stored only on this computer and never shown again; the form shows **Saved key …abcd** (nothing of a key shorter than 12 characters). Editing keeps the saved key unless you choose **Replace key**. A saved key is sent only to the server it was saved for: a base URL on another host or port asks for that server's key (**Use saved URL** puts the saved one back); another path or format keeps it. |
| Request source | Optional `User-Agent` header, so routers that log it can tell your requests apart: up to 200 printable ASCII characters, `TCS-Prover` by default. |
| Display name | Optional label inside TCS Prover; the host by default. Never sent. |
| Context window, Max output | Optional token limits for every model of the connection, for models the program does not know: a context window of at least 1,024 tokens, and a max output smaller than it. Runs pass them on: the program keeps the conversation within the window, compacting early enough to leave room for the max output, and Claude Code also asks for at most that much. Empty fields keep the program's defaults. |

- **Save connection** loads the model catalog first; a failed connection or an
  empty catalog leaves the saved settings as they were. **Load models**
  refreshes a saved connection. Renaming a connection or a reordered catalog
  keeps every selected model; two keys for the same URL stay separate sources,
  and roles can share one key with different models.
- **Authentication** is automatic: `Authorization: Bearer <key>` for OpenAI
  format, `x-api-key: <key>` for Anthropic. **401 · Missing API key** usually
  means the format, endpoint and key do not match.
- **Test connection** loads the catalog without generating an answer, from
  `<base URL>/models` (OpenAI) or `<base URL without /v1>/v1/models`
  (Anthropic, with `anthropic-version: 2023-06-01` and pagination). It has one
  30-second deadline, follows no redirect, and shows the HTTP status, the
  latency and the provider's message with the key removed. Testing unchanged
  saved settings records the result on the connection.
- **Edit** fills the form with the saved settings; the key stays saved and is
  never shown. **Remove** deletes the connection, its key and its model choices;
  saved jobs keep their model ids and show a warning until it is back. Removing a
  connection, or changing its format, is refused while a running job, or one
  waiting to start or resume, uses it.
- An entry TCS Prover cannot use (edited by hand, or saved by another version)
  is listed with the reason, **Repair** and **Remove**; the other connections
  keep working. **Repair** saves it under its id, so jobs that name its models
  find them again, and keeps its key while its saved base URL is readable and
  the new one stays on that server. A store that cannot be read is reported,
  never shown as empty: running jobs keep the settings they read last, and
  waiting jobs try again every minute.

### How API models run

- **OpenAI format** runs through the Codex CLI as an isolated custom provider
  with a private Codex home under `.tcs-prover/codex-homes/`, so your ChatGPT
  login is neither used nor sent to the endpoint. A `codex` launcher that
  replaces `CODEX_HOME` cannot run them: put the native Codex binary on `PATH`.
  Structured roles return JSON by a prompt contract that is checked and retried
  until valid. Live web search uses the endpoint's hosted search tool, which
  some endpoints refuse.
- **Anthropic format** runs through Claude Code with a private configuration
  folder per run folder under `.tcs-prover/claude-homes/` (`CLAUDE_CONFIG_DIR`),
  so your Claude login, its keychain entry and `~/.claude.json` are never read;
  these models get the same tools as Claude models. A `claude` launcher that
  drops `CLAUDE_CONFIG_DIR` cannot keep that folder: TCS Prover checks this once
  per run with a `--bare` start that reads no login and makes no request, and
  otherwise runs them in bare mode, where the author has only the sandboxed
  shell, Read and Edit, and the activity log says why. They show no reasoning
  summaries, because a gateway may return raw reasoning, which is never shown.
- The program needs no login, only the connection's key. When it is not
  installed, the job still starts, and each model call that needs it fails with
  a message naming the program, the role and the model, for example *Claude Code
  is not installed or is not on PATH. The critic's model claude-gw-1 (API
  connection “Anthropic gateway”) is Anthropic-format, and TCS Prover runs
  Anthropic-format models through Claude Code (no Claude login needed). Install
  Claude Code; the job retries every minute and continues by itself.*
- API models offer `low` through `max` and always run at standard speed.
  Terminal options take a model's stable `api:<id>` selection id; a connection id
  names the key, not a model. Older configurations keep their original
  `api:<id>` selection as an alias, so their jobs continue.

### Where keys are kept

Keys are stored in `.tcs-prover/api-models.json` in this checkout: an owner-only
file in an owner-only folder that Git ignores (`TCS_PROVER_API_MODELS` selects
another path, relative to the checkout). The browser only ever receives the hint
`…abcd`.

- Keys never enter process arguments, run folders, transcripts, logs or error
  messages. The runner gives the key only to the program: in its environment for
  Codex (whose model shell commands never inherit it), or for Claude in an
  owner-only settings file under `.tcs-prover/claude-settings/` that is deleted
  when the process exits, also after **Stop**; one left by a killed process is
  removed when the next runner or server starts.
- Transcript records are also cleaned of every key the runner knows (any value
  of at least 8 characters), so a gateway error that echoes a key shows
  `[redacted]`.
- On macOS no model sandbox can read the key store. On Linux and Windows a Codex
  role's shell can (see [Sandbox and network](sandbox.md#linux-and-windows)):
  there, store only keys that the models you run with the Codex CLI may see.

### Run settings and saved jobs

**Run settings** on the job page names each role's source: Codex CLI or Claude
Code (this computer's login), or the API connection with its format, host (and
port, unless it is the default) and program, for example *Source: API connection
“Lab gateway” · OpenAI format · gateway.example.com · runs through the Codex CLI
· id api:lab-gateway*. It never shows a key, a base URL path or a full base URL.

- Each job records the non-secret settings of its API models under `apiModels`
  in `job-settings.json`. Run settings use it to show a removed connection as
  recorded, marked *not configured now*, and one edited since the job started
  with what the job started with, for example *this job started with
  localhost:8000 (OpenAI format, model gpt-6.1-sol)*. The author can write its
  run folder, so the record is display text only and never decides where a
  model call goes.
- A model call already under way keeps what it started with; the job's next call
  uses the edited connection. While the connection list cannot be read, Run
  settings say so and never call the connection removed.
- A saved job keeps its selected model. If its connection is missing anyway, a
  waiting job checks every minute and continues once you add the endpoint and
  key again with the same format and base URL and the catalog lists its model;
  the job gets its source and selection ids back, even under another display
  name. A new connection never takes a saved job's source id with another
  format.

## Removed models

DeepSeek V4 Pro is no longer supported. A saved run that names it restores with
GPT 6.1 Sol in that role and a warning under the job title; `job-settings.json`
records the replacement once, and an error-paused run resumes automatically with
it. A role that will not run again (such as the statement reviewer of an
approved job) is not reported.
