# Sandbox and network

## What a run's models can read and write (macOS)

On macOS every model process of a run is confined to its own folders: the proof
author (and its replies while paused), the critic and its subagents, the
convention critic, the LaTeX writer and its repairs, and the statement reviewer.

- **Read:** its workspace (the author's run folder, or for a continuation the
  source run's folder it works in; for the other roles their call's own folder,
  such as the critic's proof snapshot), its private temp folder, the system, and
  the read-only software folders that commands need: `/opt/homebrew`,
  `/usr/local`, `/opt/local`, `/Library/TeX`, the Command Line Tools, python.org
  Python, Perl, `~/.elan` and `~/opt/anaconda3`, each only if it exists (plus
  Codex's or Claude Code's own programs).
- **Write:** only its workspace and its private temp folder. In the run folder
  the controller's files (`job-settings.json`, `pause.json`,
  `checked-statement.md`, `prompts/`), `related-works/` and the `.codex`,
  `.agents`, `.git` and `.aws` folders stay read-only for every role, Codex or
  Claude. The statement reviewer, the critic's final check and the paused
  author's replies write nothing in their workspace. The author writes its
  answers to your chat messages in `chat/` of its workspace.
- **Neither:** other runs, the TCS Prover checkout and its private state, your
  home folder, other volumes, `/tmp`, `/var/tmp` and the shared temp folder.

TCS Prover keeps its own copies of each job's `job-settings.json`,
`pause.json`, role prompts and checked statement in `runs/.controller/<run>/`,
outside the workspace, and relies only on those; a resumed author gets its
prompts rewritten from them. The run folder keeps copies the author may read.

**Temp folders.** `$TMPDIR` names the private temp folder. The author's is
`runs/.controller/<run>/tmp/`, kept for the whole run and deleted with the job.
Each attempt of every other call gets a fresh one that is removed when the
attempt ends. A Claude shell uses a short `/tmp/tcs-prover-…` folder of its own
(Claude Code needs a short path there), removed when the program ends; one that
a killed process left is removed at the next start. In a web-cutoff run every
model process gets a fresh one (see [Web search cutoff](web-cutoff.md)).
Downloads and scratch files therefore go to `$TMPDIR` or the run folder, not
`/tmp`; macOS `mktemp` ignores `$TMPDIR`, so use `mktemp -p "$TMPDIR"`.

**More software.** To let models use another toolchain, add its installation
folder to `TOOLCHAIN_FOLDERS` in `runner/confinement.py`. Add only software,
never a folder that holds your data: every run can read it.

**Private state.** The folder `.tcs-prover/` in the checkout holds the API key
store, the running-jobs limit (`settings.json`), saved settings, browser session
keys, private Codex and Claude homes and copies of Claude conversations. No
model sandbox can read or write it: the Claude shell sandbox wherever it runs,
and on macOS the Codex sandboxes of every role. Sandboxes of API models also
cannot read your Codex or Claude login files. At startup the server checks this
folder (it may be a symbolic link to a folder you own, which is then made
owner-only) and names a folder it cannot use. Copies of a login made for one
call (web-cutoff runs) are deleted when the call ends.

## Claude Code

The proof author's file tools are confined to its run folder, and its shell runs
in Claude's sandbox (macOS, or Linux with `bwrap` and `socat`; elsewhere the
author gets file tools only). The sandbox can neither read nor write
`.tcs-prover/`; on macOS the shell reads only its run folder and private temp
folder. Neither the file tools nor the shell can change the controller's files
in the run folder, `related-works/` or the `.codex`, `.agents`, `.git` and `.aws`
folders. The statement reviewer, the critic's subagents and the convention
critic only read; the critic and the LaTeX writer also edit their own copy of
`proof.md` or `latex-source.tex`.

## Network

In a live run (Web search cutoff off) every model process can reach the
internet, Codex or Claude: the proof author and its subagents, its replies
while paused and its progress summary, the statement reviewer, the critic with
its independent critics and its final check, the convention critic, and the
LaTeX writer and its repairs.

- **With a shell** a process reaches any host: the proof author, and with Codex
  also the author's replies and summary, the critic and its subagents, its final
  check, the convention critic, and the LaTeX writer and its repairs (on Linux
  and Windows not the final check and the convention critic, see below).
- **Without a shell** a process reaches the internet only through its web search
  tools: the statement reviewer, and with Claude every role but the proof author
  itself (its replies and summary included).
- Your Codex settings do not change this: the `network_access` of
  `[sandbox_workspace_write]` no longer applies, and neither do its writable
  roots and `/tmp` settings.
- A Claude shell reaches every host through the proxy of Claude's sandbox, which
  `curl`, `git`, `pip` and most other programs use by themselves (`HTTPS_PROXY`,
  `ALL_PROXY`); a program that ignores these settings, such as `nc` or `ssh`,
  gets no direct connection, and a name lookup outside the proxy fails.

**Local programs.** With network access, the commands of every Codex role can
also reach other programs on this computer: services on `localhost`, and any
Unix socket whose path they know, even in a folder they cannot read. No model
process, on any platform, is given `SSH_AUTH_SOCK`, `SSH_AGENT_PID` or
`GPG_AGENT_INFO`, but a command can still use a socket at a fixed path, such as
gpg-agent's in `~/.gnupg`, or list the sockets your programs have open with
`lsof`, your SSH agent's among them. Claude's sandbox refuses `localhost` and
Unix sockets.

**No network.** To run without network, use a web search cutoff: with `-1` or a
timestamp no model process has network access.

## The Codex author's sandbox check

The live Codex author runs without your Codex MCP servers, plugins and apps,
which would run outside its sandbox, and without image viewing (as the other
roles already do); the rest of your Codex settings, such as the model provider
and login, still apply.

Before the author's first turn, at every start and retry, TCS Prover checks its
sandbox without a model: one command run by Codex in that sandbox must read
(and, except for a reply, write) the run folder and write the private temp
folder, and must not read the controller's folder or the checkout, list the
folder that holds the run, or write `/tmp`; it needs no network. The activity
log shows **Sandbox checked** when it passes. If the check cannot run or fails,
the author is not started; the attempt is retried every minute like a failed
model call, with the reason in the activity log.

## Linux and Windows

There the Codex sandboxes keep Codex's legacy modes, which can read your files,
and the Claude shell keeps its default reads. Every start of a run says so in
its activity log: "Run confinement is enforced on macOS only; this run's model
can read files outside its folder." In particular a Codex role's shell can read
the API key store there, so store only keys that the models you run with the
Codex CLI may see.

The Codex author, its replies and progress summary, the critic with its
subagents, and the LaTeX writer and its repairs follow the same network rule as
on macOS. Only the legacy read-only mode of Codex's one-off calls (`codex exec`)
has no network setting, so the commands of the critic's final check (with its
subagents) and of the convention critic stay offline there even in a live run;
their native web search still works. Codex on these systems can also write to
`related-works/`.

## Outside the confinement

The Codex and Claude processes themselves, the LaTeX compile of live runs, what
a model sends out over the network, and the local programs that a live run's
models reach (see above) are outside this confinement.
