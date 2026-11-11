"""The batches made with New batch: a name and an ID each, kept beside the runs.

They live in runs/.controller/batches.json (owner-only, outside every job's
workspace). A job of such a batch keeps its run folder directly under runs/,
like every other job, and records the batch's ID in its job settings
("batch"); the controller's copy of those settings is the one trusted.
Renaming or deleting a batch therefore never moves a run folder: web-cutoff
policies, owner locks and running processes are untouched.

The file is read once when the server starts. One that cannot be read is
never written: its batches cannot be made, renamed or deleted until it is
repaired and TCS Prover is started again, and their jobs are shown in the
default batch meanwhile. Neither the number of batches nor the file's size
is limited.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import unicodedata

import workflow_runner as runtime
from runner import providers


FILENAME = "batches.json"
BATCH_ID = re.compile(r"batch_[0-9a-f]{10}\Z")
# The name of the batch of the jobs no batch made with New batch claims (server.DEFAULT_BATCH).
DEFAULT_NAME = "Default batch"
# The app's rule for the names people give (API keys and models use it too).
MAX_NAME_CHARS = providers.MAX_LABEL_CHARS
# Generated names spell the month in English, whatever the computer's language.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def batch_name(value):
    """A typed batch name with its spaces tidied, or "" when it is blank."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Enter the batch name as text.")
    # Tabs and line breaks become single spaces; other control characters are refused.
    name = " ".join(value.split())
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise ValueError("A batch name cannot contain control characters.")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"A batch name can have at most {MAX_NAME_CHARS} characters.")
    return name


def generated_name(now, taken):
    """A name for a batch made without one ("Batch · 10 Nov 14:05"), unique among ``taken`` (casefolded)."""

    base = f"Batch · {now.day} {MONTHS[now.month - 1]} {now:%H:%M}"
    name, number = base, 2
    while name.casefold() in taken:
        name, number = f"{base} ({number})", number + 1
    return name


def _created_at(value):
    """Whether a saved creation time is an ISO time the page can show."""

    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


class NamedBatches:
    """The batches made with New batch, oldest first: [{"id", "name", "createdAt"}]."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        # Why the file cannot be read ("" when it can): nothing is written while it is set.
        self.problem = ""
        self._batches = self._load()

    def _read(self):
        """The file's bytes; a link, a folder or a device in its place is refused, never followed or opened."""

        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("it is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("it is not a regular file")
            return stream.read()

    def _load(self):
        try:
            raw = self._read()
            try:
                value = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                raise ValueError("it is not UTF-8 text") from None
            except json.JSONDecodeError as exc:
                raise ValueError(f"it is not valid JSON (line {exc.lineno}, column {exc.colno})") from None
            rows = value.get("batches") if isinstance(value, dict) and value.get("version") == 1 else None
            if not isinstance(rows, list):
                raise ValueError("it has an unknown format")
            batches, ids, names = [], set(), {DEFAULT_NAME.casefold()}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not BATCH_ID.match(row["id"]):
                    raise ValueError("it lists a batch without a valid ID")
                if row["id"] in ids:
                    raise ValueError("it lists two batches with the same ID")
                try:
                    name = batch_name(row.get("name"))
                except ValueError:
                    name = ""
                if not name or name.casefold() in names:
                    raise ValueError("it lists a batch without a valid, unique name")
                if not _created_at(row.get("createdAt")):
                    raise ValueError("it lists a batch without the time it was made")
                ids.add(row["id"])
                names.add(name.casefold())
                batches.append({"id": row["id"], "name": name, "createdAt": row["createdAt"]})
            return batches
        except FileNotFoundError:
            return []  # No batch has been made yet.
        except ValueError as exc:
            reason = str(exc)
        except RecursionError:
            reason = "it has an unknown format"
        except MemoryError:
            reason = "it is too large to read"
        except OSError as exc:
            reason = exc.strerror or str(exc) or type(exc).__name__
        self.problem = (f"The list of batches ({self.path}) cannot be read: {reason.rstrip('.')}. Until it is "
                        "repaired and TCS Prover is started again, batches cannot be made, renamed or deleted, "
                        "and the problems of the batches it lists are shown in the default batch.")
        return []

    def entries(self):
        with self.lock:
            return [dict(row) for row in self._batches]

    def ids(self):
        with self.lock:
            return {row["id"] for row in self._batches}

    def _save_locked(self, batches):
        if self.problem:
            raise ValueError(self.problem)
        runtime._private_atomic_write(self.path, json.dumps({"version": 1, "batches": batches},
                                                            ensure_ascii=False, indent=2) + "\n")
        self._batches = batches

    def _taken_locked(self, exclude=""):
        """The names in use, casefolded, each with the spelling of the batch that has it: "Default batch" and
        every batch made with New batch except ``exclude``."""

        return {DEFAULT_NAME.casefold(): DEFAULT_NAME,
                **{row["name"].casefold(): row["name"] for row in self._batches if row["id"] != exclude}}

    @staticmethod
    def _name_taken(name, taken):
        """The refusal of a name in use: it quotes the batch that has the name, as that batch spells it."""

        return ValueError(f"There is already a batch named “{taken[name.casefold()]}”. Choose another name.")

    def create(self, name="", now=None):
        """Add a batch; a blank ``name`` gets a generated one. ``now`` is an aware local time (tests)."""

        name = batch_name(name)
        now = now or datetime.now().astimezone()
        with self.lock:
            taken = self._taken_locked()
            if name and name.casefold() in taken:
                raise self._name_taken(name, taken)
            name = name or generated_name(now, taken)
            ids = {row["id"] for row in self._batches}
            batch_id = "batch_" + secrets.token_hex(5)
            while batch_id in ids:
                batch_id = "batch_" + secrets.token_hex(5)
            row = {"id": batch_id, "name": name,
                   "createdAt": now.astimezone(timezone.utc).isoformat(timespec="seconds")}
            self._save_locked([*self._batches, row])
            return dict(row)

    def rename(self, batch_id, name):
        name = batch_name(name)
        if not name:
            raise ValueError("Enter a name for the batch.")
        with self.lock:
            if not any(row["id"] == batch_id for row in self._batches):
                raise ValueError("That batch no longer exists.")
            taken = self._taken_locked(exclude=batch_id)
            if name.casefold() in taken:
                raise self._name_taken(name, taken)
            batches = [{**row, "name": name} if row["id"] == batch_id else dict(row) for row in self._batches]
            self._save_locked(batches)
            return dict(next(row for row in batches if row["id"] == batch_id))

    def delete(self, batch_id):
        with self.lock:
            if not any(row["id"] == batch_id for row in self._batches):
                raise ValueError("That batch no longer exists.")
            self._save_locked([dict(row) for row in self._batches if row["id"] != batch_id])
