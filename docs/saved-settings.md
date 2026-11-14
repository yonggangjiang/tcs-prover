# Saved settings

**Saved settings** is the first panel under **Advanced settings** on the New
problem page. Every new problem opens with **Default settings**. Save the
form's settings back to them, or under a name of their own to load again later.

- **Now using** names the settings that fill the form: **Default settings**,
  the name of saved settings you loaded, or **the job's settings** for **Rerun
  with suggested statement** and for a job sent back to the form (**TCS
  Prover's original settings** when Default settings could not be read).
  "· changed on this form" follows once a setting differs from them; the saved
  settings themselves stay as they are until you save.
- The **Advanced settings** line, in view while the section is closed, says
  nothing about saved settings unless something is wrong: a model of the
  settings that filled the form is not available (*Default settings: author
  model not available*, or the saved settings' name instead), or saved settings
  cannot be read or used now (*Default settings not available* on a fresh form,
  which then has TCS Prover's original settings, or *Saved settings not
  available* on a form that other settings fill). Such a warning has an amber
  mark (a diamond in the Ink theme). The same line counts the
  [related works](job-page.md#related-works) listed for the problem.

## Default settings

Default settings fill every fresh New problem form, including a `?new=1` page
and **New problem in this batch**. Until you save to them, they are TCS
Prover's original settings. **Rerun with suggested statement** and a job sent
back to the form keep their own settings, and switching between New problem
and **Models & API** keeps what you edited.

- The name *Default settings*, in any case, always means them: no saved
  settings can be given it. Saved settings from an earlier version that already
  have it are listed with it, and can be renamed or deleted.
- The saved settings an earlier version opened new problems with (its
  **Set as default**) are your Default settings after the update, and stay in
  the list under their own name too.
- While the saved settings cannot be read (TCS Prover does not answer, say), a
  fresh form opens with TCS Prover's original settings and says so; a form
  already filled keeps its settings. The next form, or the next **Load saved
  settings…**, reads them again.
- Default settings that cannot be used now (saved by a newer TCS Prover with a
  model this one does not know, say) are kept as they are: new problems open
  with TCS Prover's original settings meanwhile, the panel says why, and saving
  to Default settings or **Reset** replaces them.

## Save these settings…

**Save these settings…** keeps the form's settings: the selected stages, each
role's source, model and reasoning, author subagents, the subagent models
(*Same as the coordinator*, or each author subagent's model and reasoning),
independent critics, critic rounds, the time limit, the web search cutoff, the
generation speed and the activity log detail. Never a goal loop: every new job
runs [TCS Prover's](workflow.md#the-goal-loop).

- The name says where they go: back to where the form's settings came from.
  It is *Default settings* on a form they filled, the name of the saved settings
  you loaded, and empty for a job's settings. Saving there replaces those
  settings without asking.
- Type another name for new saved settings. A name that other saved settings
  have (ignoring case) asks before replacing them, and so does *Default
  settings* on a form they did not fill.
- Leave the name empty to use the one shown, such as "GPT 6.1 Sol/Max author ·
  31 Oct 17:48". Names have up to 200 characters, and there is no limit on how
  many you save.
- The panel then says *Saved to Default settings. New problems open with these
  settings.*, or *Saved as “…”.*
- As at Start, the settings of a stage that is off (its model and reasoning, the
  critics and rounds, the author's subagents and time limit) never stop a save:
  they are kept as shown, or as TCS Prover's original value when a field is not
  valid.

## Load saved settings…

**Load saved settings…** lists Default settings first, then your saved settings
by name. The list, and the model menus it loads into, are read again each time
it opens, so connections added or removed in another tab count.

- **Default settings** can be loaded, and, while they differ from TCS Prover's
  original settings, reset: **Reset** asks *Put back TCS Prover's original
  settings as Default settings?* in their row first.
- Saved settings can be loaded, renamed, made the default or deleted. **Make
  default** copies them into Default settings once you answer *Replace Default
  settings with “…”?* in their row; the saved settings stay as they are.
  Deleting saved settings never changes Default settings.
- After **Reset** or **Make default**, a form that Default settings filled and
  that you have not edited takes the new Default settings too; an edited form
  keeps your edits, marked "· changed on this form".
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
  subagents. TCS Prover's original settings use 3, and so do saved settings that
  hold no number (saved before the setting existed).
- TCS Prover's original settings use *Same as the coordinator* for the
  [subagent models](workflow.md#the-authors-team), and so do saved settings
  from before that setting existed. A saved subagent model of a removed API
  connection loads as GPT 6.1 Sol with a note, as a role's does (*Author
  subagent 2 model not available …*).
- Settings saved while the form still offered a goal loop may hold one: it is
  left out as they load, without a note, and the job runs TCS Prover's goal
  loop.
- Terminal runs never read saved settings.

## Where they are kept

Saved settings live in `.tcs-prover/new-problem-presets.json` on this computer
(with `new-problem-presets.lock`, which keeps two saves from losing each other):
an owner-only file that no model sandbox can write, so no model can change the
settings your next problem opens with. On macOS no model sandbox can read it
either, and Claude Code's sandbox cannot read it anywhere; on Linux and Windows
a Codex role can read it (see
[Sandbox and network](sandbox.md#linux-and-windows)). It holds no keys.

- Default settings are kept in the same file. A TCS Prover from before Default
  settings still reads the file but not them: it opens new problems as it did
  (with its built-in settings, or the saved settings it made its default), and
  its first save, rename, delete or change of the default drops them. Default
  settings are then TCS Prover's original settings again, or the saved
  settings that version made its default.
- A file that cannot be read, is damaged or comes from a newer version is
  reported and never overwritten: new problems then open with TCS Prover's
  original settings, which **Load saved settings…** lists alone, and nothing
  can be saved until it is fixed or removed.
- Saved settings that cannot be used (an entry edited by hand) are listed with
  the reason and can only be deleted; every save keeps them as they are.
- An older TCS Prover server, one without saved settings or from before Default
  settings, turns **Load saved settings…** and **Save these settings…** off and
  says to restart it. After the restart, the next New problem form (also in a
  tab left open) opens with Default settings and turns both on again.
