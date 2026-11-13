# Saved settings

**Saved settings** is the first panel under **Advanced settings** on the New
problem page. Save the form's settings under a name to reuse them, and choose
which saved settings every new problem opens with.

- **Now using** names the settings that fill the form: **built-in settings**
  (TCS Prover's own), the name of saved settings, with "(default)" when new
  problems open with them, or **the job's settings** for **Rerun with suggested
  statement** and for a job sent back to the form. "· changed on this form"
  follows once a setting differs from them; the saved settings themselves stay
  as they are until you save.
- The **Advanced settings** line shows the same while the section is closed:
  **Saved settings: built-in**, or a marked **Saved settings:** *name* (or
  **Settings from the job**) with "· changed" after an edit, so a form that a
  saved default filled never goes unnoticed. A warning mark (amber, or a
  diamond in the Ink theme) says the panel holds a warning, and the words say
  which: a saved model that is not available ("· model not available"), or
  saved settings that cannot be read or used now ("Saved settings: not
  available" on a form with the built-in settings, or "· saved settings not
  available" after the name of the settings that fill the form). The same line
  counts the [related works](job-page.md#related-works) listed for the problem.

## Save these settings…

**Save these settings…** keeps the form's settings under a name: the selected
stages, each role's source, model and reasoning, author subagents, the subagent
models (*Same as the coordinator*, or each author subagent's model and
reasoning), the goal loop, independent critics, critic rounds, the time limit,
the web search cutoff, the generation speed and the activity log detail.

- As at Start, the settings of a stage that is off (its model and reasoning, the
  critics and rounds, the author's subagents and time limit) never stop a save:
  they are kept as shown, or as the built-in value when a field is not valid.
- Leave the name empty to use the one shown, such as "GPT 6.1 Sol/Max author ·
  31 Oct 17:48". A name already in use (ignoring case) asks before replacing
  those settings. Names have up to 200 characters, and there is no limit on how
  many you save.
- **Set as default**, ticked each time the dialog opens, makes these the settings
  every new problem opens with; unticked, the default stays as it was.

## Load saved settings…

**Load saved settings…** lists the default first, then the others by name, and
**Built-in settings** last: load, rename, delete, or make or stop being the
default. The list, and the model menus it loads into, are read again each time
it opens, so connections added or removed in another tab count.

- The default fills every fresh New problem form, including a `?new=1` page and
  **New problem in this batch**. **Rerun with suggested statement** and a job
  sent back to the form keep their own settings, and switching between New
  problem and **Models & API** keeps what you edited.
- While the saved settings cannot be read (TCS Prover does not answer, say), a
  fresh form opens with the built-in settings and says so; a form already
  filled keeps its settings. The next form, or the next **Load saved
  settings…**, reads them again.
- Loading changes only these settings. The statement, related works, edited
  prompts and the batch the problem goes into stay as they are, and none of them
  is ever saved. Neither is an API key or address: saved settings hold model ids,
  plus each API model's name and connection name to describe it if it is removed.
- A saved setting that names a removed API model loads that role with GPT 6.1 Sol
  (which runs through the Codex CLI), and a note in the panel names the role and
  the missing model. The saved settings are not changed, and work as saved again
  once the connection is added back under the same display name.
- A saved web search cutoff loads as saved; whether it can run (Statement review
  off, Codex CLI models, a time in the past) is checked when you start, as for
  any job.
- Saved settings keep the number of author subagents they were saved with,
  also settings saved before the author coordinator existed: 0 starts one
  author working alone, 1 or more an
  [author coordinator](workflow.md#the-author-coordinator) with that many author
  subagents. The built-in settings use 3, and so do saved settings that hold no
  number (saved before the setting existed).
- The built-in settings use *Same as the coordinator* for the
  [subagent models](workflow.md#the-authors-team) and TCS Prover's
  [goal loop](workflow.md#the-goal-loop), and so do saved settings from before
  these settings existed. A saved subagent model of a removed API connection
  loads as GPT 6.1 Sol with a note, as a role's does (*Author subagent 2 model
  not available …*).
- The **Advanced settings** line also names the goal loop (*Goal loop: TCS
  Prover*) and, with 1 or more author subagents, the team (*Coordinator, 3 author
  subagents: same model as the coordinator*); either is marked when it is not
  the default.
- Terminal runs never read saved settings.

## Where they are kept

Saved settings live in `.tcs-prover/new-problem-presets.json` on this computer
(with `new-problem-presets.lock`, which keeps two saves from losing each other):
an owner-only file that no model sandbox can write, so no model can change the
settings your next problem opens with. On macOS no model sandbox can read it
either, and Claude Code's sandbox cannot read it anywhere; on Linux and Windows
a Codex role can read it (see
[Sandbox and network](sandbox.md#linux-and-windows)). It holds no keys.

- A file that cannot be read, is damaged or comes from a newer version is
  reported and never overwritten: new problems then open with the built-in
  settings, and nothing can be saved until it is fixed or removed.
- Saved settings that cannot be used (an entry edited by hand) are listed with
  the reason and can only be deleted; every save keeps them as they are.
- An older TCS Prover server without saved settings turns **Load saved
  settings…** and **Save these settings…** off and says to restart it. After the
  restart, the next New problem form (also in a tab left open) opens with the
  default and turns both on again.
