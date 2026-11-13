# Models and API connections

Every role (statement reviewer, proof author, critic, LaTeX writer) runs through
one of two programs on your computer: the **Codex CLI** (the default) or
**Claude Code**. They provide the author's conversation and every model call.
GPT 6.1 Sol at Max reasoning and Standard speed is the default for every role,
and the critic's choice also covers the convention critic. The author
coordinator's subagents run the coordinator's model, or with **Subagent models →
Choose for each** a model of their own each, from the same sources (see
[the author's team](workflow.md#the-authors-team)).

Choose per role under **Advanced settings** on the New problem page, or on
**Models & API** in the sidebar: a **Source**, then a **Model**, then its
**Reasoning** level. Sources are **Codex CLI**, **Claude Code** and each saved
API connection; each model offers only the reasoning levels it supports.
`runner/providers.py` is the single registry of model ids, programs and efforts.

| Source | Models | Needs |
| --- | --- | --- |
| Codex CLI | `gpt-6.1-sol` (GPT 6.1 Sol, the default), `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna` | the Codex CLI, signed in with ChatGPT (on the Setup page, or `codex login`) |
| Claude Code | `claude-fable-5-1` (Claude Fable 5.1), `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` | Claude Code, signed in with your Claude subscription |
| API connection | the models your endpoint lists | a saved key, and the program for its format installed but not signed in: OpenAI format through the Codex CLI, Anthropic format through Claude Code |

Install and sign in on the [Setup page](#the-setup-page); the commands to run in a
terminal instead are in the [README](../README.md#what-you-need-for-models).

## The Setup page

**Setup**, first under Configure in the sidebar, shows the programs your models
run through, and LaTeX, one card each:

| Card | Shows | Buttons |
| --- | --- | --- |
| Codex CLI | installed or not, version, location and what installed it, sign-in (ChatGPT or an API key), what needs it | Install, Update, Sign in (in this browser or with a one-time code) |
| Claude Code | the same, with the account and plan | Install, Update, Sign in (Claude subscription or Anthropic Console) |
| LaTeX | whether `latexmk` and `pdflatex` are found, and their versions | the install command for your system, to run yourself |

- **Needed by** lists the New problem roles (the form in this tab while it is
  open, else your saved default settings), your saved default settings also
  while the form is open if they use the program where the form does not, the
  jobs that can still run and use it in their current or a later step (a job in
  **Writing & polishing** no longer needs the program of its critic), and your
  API connections. The built-in settings, which new problems open with when
  none are saved, count only as the open form. API connections are only
  listed: one needs its program installed but no sign-in, and only a role or a
  job that uses it makes the program needed.
- Only a program that the form, your saved default settings or such a job
  needs is a warning (a red or amber mark, the sidebar count, the Overview
  notice) when it is missing, does not run, is not on the jobs' path or, for a
  built-in model, is not signed in. Any other program is shown calmly, in the
  same words with a plain ring and no colour, for example *Not installed* with
  *Nothing uses it now · only needed for Claude models*, *No role or job uses it
  now* under an API connection, or *Not signed in · Not needed by your settings
  or jobs* (*New problems open with the built-in settings, which use your
  ChatGPT login* while nothing is saved and the New problem form is closed):
  if you use only GPT models, Claude Code is never flagged. A calm
  card keeps its **Install**, **Use it for jobs** and **Sign in** buttons, none
  of them highlighted.
- On a computer with neither the Codex CLI nor Claude Code installed, no model
  can run, so one line says so (the sidebar count, the Overview and the top of
  Setup): *Neither the Codex CLI nor Claude Code is installed*, with which one
  each kind of model needs. It gives way to a program's own line as soon as
  the form, your saved default settings or a job needs that program.
- Setup checks the programs exactly as jobs find and start them, with no model
  call: `codex --version`, `codex login status`, `claude --version`, `claude auth
  status`, `latexmk -v` and `pdflatex --version`. "Signed in" means a login is
  saved; an expired one shows on the job that needs it, under **Needs your
  attention**. Each check waits at most 20 seconds; one that does not answer is
  reported as such, and **Check again** runs the checks again. Opening Setup,
  **Check again** and coming back to the Setup window (at most every 10
  seconds) check again. Any other page you open reuses a check younger than 10
  minutes, and checks again when it is older; a page left open keeps showing
  its last check (its background reads start no program) until you open Setup
  or reload it.
- **Install** runs the vendor's official installer after you confirm the exact
  command: `curl -fsSL https://chatgpt.com/codex/install.sh | sh` (with
  `CODEX_NON_INTERACTIVE=1`, as Codex's own updater runs it) or `curl -fsSL
  https://claude.ai/install.sh | bash`, on Windows the PowerShell installers.
  You see the live output and can stop it; installs, updates and sign-ins have
  no time limit (in a Codex sign-in, each answer of the Codex CLI must come
  within 120 seconds). **Stop** asks the installer to end, and after 10
  seconds ends it and everything it started; on macOS and Linux, what an
  install or update leaves running ends with it. A Stop that comes once the
  installer has finished undoes nothing: the result says so, and the sign-in
  you chose does not start. **Update** runs the Codex installer again, or
  `claude update`.
  **Then sign in** in the Install dialog starts the sign-in once the install
  works, under the same confirmation. It is offered when that login will be
  used: by the New problem form, your saved default settings or a job, by the
  built-in settings new problems open with while nothing is saved, or, on a
  computer with neither program yet, by the program you install first.
- A program you install here is used by new jobs at once, and by jobs already
  waiting for it: when its folder is not on TCS Prover's PATH, Setup remembers
  the folder (`.tcs-prover/program-folders.json`, which no model can read or
  write), and the lookups of jobs use it whenever PATH has no copy. Open a new
  terminal to use the program yourself. A copy that is installed but not on the
  path shows **Use it for jobs**, which remembers its folder the same way.
- Copies installed with Homebrew, npm, WinGet or a Linux package, and commands
  that are your own scripts, are never changed by Setup: it shows the commands
  to run yourself. (OpenAI's installer replaces `~/.local/bin/codex`, so Setup
  does not run it when your own script is there. A program that changed since
  the last check, for example a script you put there while the dialog was open,
  is checked again before anything runs, and the step is then refused.)
- While one of this server's jobs runs and its current or a later step runs a
  model through a program, that program's **Update**, a repair install and
  **Sign in again** (also **Sign in** when the sign-in could not be checked)
  wait: pause or stop the job, or let it finish.
  A job whose model calls all wait for that program's sign-in or for it to run
  again does not count: that step is what it waits for. Installing a missing
  program and a first sign-in never wait.
- **Sign in**: Codex gives a ChatGPT link or a one-time code (for TCS Prover on
  another computer; turn on device code sign-in in ChatGPT's security settings
  first). Claude Code may open a tab; if the sign-in page shows a code, paste it
  into the box on the Setup page, which passes it only to Claude Code. Not
  offered: Claude's `/login`, `claude setup-token` and API-key logins (keys
  belong in API connections), and there is no Sign out button. A sign-in token
  in `CLAUDE_CODE_OAUTH_TOKEN` is what Claude jobs use, so Setup then offers no
  Claude sign-in.
- Setup never uses administrator rights (it refuses every action when TCS
  Prover runs as root or administrator), never asks for API keys or tokens,
  never puts what you type into a command, and keeps its output only on the
  page until TCS Prover restarts: keys, tokens and sign-in codes are removed,
  and each step keeps its newest 2,000 lines (the panel says how many earlier
  lines it dropped).
- The sidebar's Setup item counts the programs that need setting up and that
  something needs (the New problem form while it is open, your saved default
  settings, the jobs that can still run and use them), and the Overview lists
  them; on a computer with neither the Codex CLI nor Claude Code, it counts the
  one line that says so. The chip of a role whose chosen model needs one, the
  hint under the role menus and the hint beside **Start workflow** follow the
  form's selected roles only, and a job that waits for a program or a login
  links to Setup from its line under **Needs your attention**, its row and its
  page. None of this stops a start: the job retries every minute and continues
  by itself.
- Setup is not available through the live gateway; open TCS Prover from its own
  launch link.

## What a start checks

A launch or resume checks only that each selected model is configured and, for
an API model, has its saved key; a run with a web cutoff timestamp also needs
Codex CLI models for its proof roles and the native Codex binary, and with
**Writing & polishing** on also MacTeX in `/usr/local/texlive` and macOS's
`sandbox-exec` for its isolated LaTeX compiler (see
[Web search cutoff](web-cutoff.md#what-a-timestamp-run-checks)). No other
installed program is checked there. A missing program never stops a start (a
run with a web cutoff timestamp is the exception: a start without the native
Codex binary is refused, so install it and start again; with Writing &
polishing on it also needs MacTeX, and a job whose MacTeX is missing or fails
its checks is held, checked again every minute, and starts or continues by
itself once MacTeX is installed or repaired). The model call that needs the
program says so in the activity log and is retried every minute, so the job
continues by itself once the program is installed (see
[Retries and what needs you](retries.md)).

Under each role menu a chip names the program, or for an API model its format,
program and host (for example *API · Anthropic format via Claude Code ·
gateway.example.com*). When the Setup page's last check found that program
missing, not running, not on the jobs' path or, for a built-in model, not signed
in, the chip adds it (for example *Claude Code · not installed*) and a hint under
the menus links to Setup; nothing stops a start. Only the model you chose for a
role of a selected stage gets this note: the **Source** menus offer every source
without marks. Each author subagent of **Choose for each** counts like a role:
its row's chip says the same, the hint names it (*Claude Code is not installed on
this computer; the author subagent 2 uses it.*), and the Setup page counts its
program as needed (an API connection's program installed, without a sign-in).

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

Install Claude Code and sign in with your Claude subscription on the Setup
page (or run `claude auth login` in a terminal). Its models offer `low`, `medium`, `high`, `xhigh` and `max` (a
saved `ultra` runs as `max`) and always run at standard speed.
Earlier versions of TCS Prover called this program the Claude CLI; the activity
logs of jobs run then keep that name.

- **Tools.** Every Claude process gets an explicit tool list and no MCP servers,
  user settings, skills, plugins, hooks or `CLAUDE.md`, and never waits for a
  permission prompt (see [Sandbox and network](sandbox.md#claude-code)).
  Web-search tools follow the job's search mode. Only the critic, and an author
  with **Author subagents** 1 or more (an author coordinator, or the author of
  a job from before coordinator mode), get the subagent tool (Task); TCS Prover
  limits how many subagents run at once and keeps them in the foreground
  through Claude Code's environment (see
  [what the program enforces](workflow.md#subagents-what-the-program-enforces)).
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
  some endpoints refuse. Codex (0.160) offers no search tool at all to a model
  it runs as "Responses Lite" (by the name the connection sends: gpt-6.1-sol
  and the other GPT 6 and GPT 5.6 models of Codex's own model list) through an
  API connection: its own web search comes only with its built-in OpenAI
  provider, that is, the **Codex CLI** source. A live run whose author or author subagent
  has such a model says so in the activity log as the author starts (**No web
  search**), and goes on without web search.
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
