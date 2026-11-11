# Saved settings

The bar at the top of the New problem card names the settings the form started
from: **Built-in**, the name of saved settings (with "· default" for the
default), or **From the job** for **Rerun with suggested statement** and for a
job sent back to the form. "· changed" follows once a setting differs from them.

## Save…

**Save…** keeps the form's settings under a name: the selected stages, each
role's source, model and reasoning, author subagents, independent critics,
critic rounds, the time limit, the web search cutoff, the generation speed and
the activity log detail.

- As at Start, the settings of a stage that is off (its model and reasoning, the
  critics and rounds, the author's subagents and time limit) never stop a save:
  they are kept as shown, or as the built-in value when a field is not valid.
- Leave the name empty to use the one shown, such as "GPT 6.1 Sol/Max author ·
  31 Oct 17:48". A name already in use (ignoring case) asks before replacing
  those settings. Names have up to 200 characters, and there is no limit on how
  many you save.
- **Set as default**, ticked each time the dialog opens, makes these the settings
  every new problem opens with; unticked, the default stays as it was.

## Load…

**Load…** lists the default first, then the others by name, and **Built-in
settings** last: load, rename, delete, or make or stop being the default. The
list, and the model menus it loads into, are read again each time it opens, so
connections added or removed in another tab count.

- The default fills every fresh New problem form, including a `?new=1` page and
  **New problem in this batch**. **Rerun with suggested statement** and a job
  sent back to the form keep their own settings, and switching between New
  problem and **Models & API** keeps what you edited.
- Loading changes only these settings. The statement, related works, edited
  prompts and the batch the problem goes into stay as they are, and none of them
  is ever saved. Neither is an API key or address: saved settings hold model ids,
  plus each API model's name and connection name to describe it if it is removed.
- A saved setting that names a removed API model loads that role with GPT 6.1 Sol
  (which runs through the Codex CLI), and a note names the role and the missing
  model. The saved settings are not changed, and work as saved again once the
  connection is added back under the same display name.
- A saved web search cutoff loads as saved; whether it can run (Statement review
  off, Codex CLI models, a time in the past) is checked when you start, as for
  any job.
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
- An older TCS Prover server without saved settings turns **Load…** and
  **Save…** off and says to restart it. After the restart, the next New problem
  form (also in a tab left open) opens with the default and turns both on again.
