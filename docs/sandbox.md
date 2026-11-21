# Sandbox and network

## What a run's models can read and write (macOS and Linux)

On macOS, and on Linux where bubblewrap works (see
[Linux](#linux)), every model process of a run is confined to its own folders:
the proof author (and its replies while paused), the critic and its subagents,
the LaTeX writer and its repairs, and the statement reviewer. On Linux this holds for Codex roles; a Claude Code role's shell is not
confined there yet (see [Claude Code](#claude-code)).

- **Read:** its workspace (the author's run folder, or for a continuation the
  source run's folder it works in; for the other roles their call's own folder,
  such as the critic's proof snapshot), its private temp folder, the system, and
  the read-only software folders that commands need: `/opt/homebrew`,
  `/usr/local`, `/opt/local`, `/Library/TeX`, the Command Line Tools, python.org
  Python, Perl, `~/.elan` and `~/opt/anaconda3`, each only if it exists (plus
  Codex's or Claude Code's own programs). On Linux the system is `/bin`, `/etc`,
  `/lib`, `/lib64`, `/sbin` and `/usr` (with `/usr/local`), and the software
  folders are `/opt/texlive`, `/opt/conda`, `/var/lib/texmf`, `~/.elan`,
  `~/miniconda3`, `~/miniforge3`, `~/anaconda3`, `~/.local/bin` and
  `~/.local/lib`, each only if it exists, plus the folder the Codex that runs is
  installed in (such as `~/.local/share/codex`) and the file `/etc/resolv.conf`
  points to, without which no host name resolves.
- **Write:** only its workspace and its private temp folder. In the run folder
  the controller's files (`job-settings.json`, `pause.json`,
  `checked-statement.md`, `prompts/`), `related-works/` and the `.codex`,
  `.agents`, `.git` and `.aws` folders stay read-only for every role, Codex or
  Claude. On Linux, where Codex's sandbox can protect only an entry that
  exists, TCS Prover makes `related-works/` before the author starts, and a
  `pause.json` the run folder lacks can be created there (TCS Prover relies on
  its own copy). The statement reviewer and the paused author's replies write
  nothing in their workspace. The author writes its
  answers to your chat messages in `chat/` of its workspace.
- **Neither:** other runs, the TCS Prover checkout and its private state, your
  home folder, other volumes, `/tmp`, `/var/tmp` and the shared temp folder. On
  Linux a command sees only what it may read: the folders on the way to its run
  folder, such as `runs/`, show only the entries on the way (its own folder and
  `.controller`), never another run.

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
`/tmp`; macOS `mktemp` ignores `$TMPDIR`, so use `mktemp -p "$TMPDIR"`. On
Linux the folders of the other calls are in `~/.cache/tcs-prover/tmp/`
(`$XDG_CACHE_HOME/tcs-prover/tmp/`) instead of `/tmp`, and a command that
writes `/tmp` gets "Read-only file system". (Codex's Linux sandbox would hide a
folder in `/tmp` that a call may only read; should one be there anyway, such as
a `CODEX_HOME` in `/tmp`, that call gets an empty `/tmp` of its own, gone when
each command ends.)

**More software.** To let models use another toolchain, add its installation
folder to `TOOLCHAIN_FOLDERS` in `runner/confinement.py`. Add only software,
never a folder that holds your data: every run can read it.

**Private state.** The folder `.tcs-prover/` in the checkout holds the API key
store, the running-jobs limit (`settings.json`), saved settings, browser session
keys, private Codex and Claude homes, copies of Claude conversations and the
program folders the Setup page remembered (`program-folders.json`, which name
the Codex CLI and Claude Code that jobs run). No
model sandbox can read or write it: the Claude shell sandbox wherever it runs,
and on macOS and Linux (with bubblewrap) the Codex sandboxes of every role. Sandboxes of API models also
cannot read your Codex or Claude login files. At startup the server checks this
folder (it may be a symbolic link to a folder you own, which is then made
owner-only) and names a folder it cannot use. Copies of a login made for one
call (web-cutoff runs) are deleted when the call ends.

**Job records.** `.tcs-prover/records/` holds every job's record of its model
calls (see [The job page](job-page.md#the-record-of-every-model-call)). It is
not among the entries above (keeping models away from it was left for later):
on macOS and on Linux with bubblewrap no run's Codex model reads it all the same,
as they read nothing of the checkout; on Windows, on Linux without bubblewrap,
and through a Claude Code shell on Linux a live run's model could (see below). A record
never holds a login: the copy a web-cutoff URL discovery call keeps while it
runs is left out of the discovery folders it copies.

## Claude Code

The proof author's file tools are confined to its run folder, and its shell runs
in Claude's sandbox (macOS, or Linux with `bwrap` and `socat`; elsewhere the
author gets file tools only). The sandbox can neither read nor write
`.tcs-prover/`; on macOS the shell reads only its run folder and private temp
folder. On Linux it still reads your other files (Claude Code's own sandbox has
not been checked with these rules there), and the activity log says so: "On
Linux run confinement covers Codex roles only; this run's Claude Code shell can
read files outside its folder." Neither the file tools nor the shell can change the controller's files
in the run folder, `related-works/` or the `.codex`, `.agents`, `.git` and `.aws`
folders. The statement reviewer and the critic's subagents only read; the critic and the LaTeX writer also edit their own copy of
`proof.md` or `latex-source.tex`.

## Network

In a live run (Web search cutoff off) every model process can reach the
internet, Codex or Claude: the proof author and its subagents, its replies
while paused and its progress summary, the statement reviewer, the critic with
its independent critics, and the LaTeX writer and its repairs.

- **With a shell** a process reaches any host: the proof author, and with Codex
  also the author's replies and summary, the critic and its subagents, and the
  LaTeX writer and its repairs.
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
Unix socket whose path they know, even in a folder they cannot read. On Linux a
Codex command reaches a Unix socket only in a folder it can read, or an abstract
one (Linux's sockets without a file), and has its own process list, so `lsof`
shows none of your programs' sockets; `localhost` stays reachable. No model
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
folder that holds the run (on Linux: see another entry of it, as another run's
folder would be), or write `/tmp`; it needs no network. The activity
log shows **Sandbox checked** when it passes. If the check cannot run or fails,
the author is not started; the attempt is retried every minute like a failed
model call, with the reason in the activity log.

## Linux

On Linux Codex confines its commands with bubblewrap (`bwrap`), which it runs
from `PATH`: TCS Prover gives it the same profile as on macOS (verified with
Codex 0.170 and bubblewrap 0.9 on Ubuntu 24.04). bubblewrap needs user
namespaces. TCS Prover tries it once per runner (`bwrap` making an empty
sandbox); if there is no `bwrap` or it cannot make one, the run keeps the legacy
modes below. To get the confinement, install bubblewrap (`sudo apt install
bubblewrap` on Debian and Ubuntu); on Ubuntu 24.04 AppArmor lets only
`/usr/bin/bwrap` make user namespaces, which the package's profile allows. The
author's sandbox check (above) runs before every start either way. TCS Prover
makes the empty folders `.agents`, `.aws`, `.codex` and `.git` in the run
folder, and in each model call's own folder, before a confined Codex process
starts there: the places the sandbox keeps read-only for every command, which
Codex also needs to find when it continues a conversation (a team member's
follow-up, a retried call). Git treats an empty `.git` as no repository.

## Windows, and Linux without bubblewrap

There the Codex sandboxes keep Codex's legacy modes, which can read your files,
and the Claude shell keeps its default reads. Every start of a run says so in
its activity log: "Run confinement is enforced on macOS, and on Linux with
bubblewrap (bwrap); this run's model can read files outside its folder." In
particular a Codex role's shell can read the API key store there, so store only
keys that the models you run with the Codex CLI may see. The same holds for the
job records in `.tcs-prover/records/`, which hold every job's model calls.

The Codex author, its replies and progress summary, the critic with its
subagents, and the LaTeX writer and its repairs follow the same network rule as
on macOS. Codex on these systems can also write to `related-works/`.

## Outside the confinement

The Codex and Claude processes themselves, the LaTeX compile of live runs, what
a model sends out over the network, and the local programs that a live run's
models reach (see above) are outside this confinement.
