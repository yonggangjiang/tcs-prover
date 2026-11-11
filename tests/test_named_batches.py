"""The list of batches made with New batch: names, IDs, the file and its damage (no server, no model)."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers
from ui import named_batches


LOCAL = datetime(2026, 11, 10, 14, 5, tzinfo=timezone(timedelta(hours=1)))


class NamedBatchesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "runs" / ".controller" / named_batches.FILENAME

    def test_a_batch_is_saved_owner_only_and_read_back(self):
        batches = named_batches.NamedBatches(self.path)
        self.assertEqual((batches.entries(), batches.problem), ([], ""))  # No file yet: no batch, no problem.
        entry = batches.create("  Graph \t coloring ", now=LOCAL)
        self.assertEqual(entry["name"], "Graph coloring")
        self.assertRegex(entry["id"], r"^batch_[0-9a-f]{10}$")
        self.assertEqual(entry["createdAt"], "2026-11-10T13:05:00+00:00")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"version": 1, "batches": [entry]})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        again = named_batches.NamedBatches(self.path)
        self.assertEqual((again.entries(), again.ids(), again.problem), ([entry], {entry["id"]}, ""))
        # No temporary file is left beside the list.
        self.assertEqual([path.name for path in self.path.parent.iterdir()], [named_batches.FILENAME])

    def test_names_are_generated_unique_and_checked(self):
        batches = named_batches.NamedBatches(self.path)
        self.assertEqual(batches.create("", now=LOCAL)["name"], "Batch · 10 Nov 14:05")
        self.assertEqual(batches.create(None, now=LOCAL)["name"], "Batch · 10 Nov 14:05 (2)")
        self.assertEqual(batches.create("   ", now=LOCAL)["name"], "Batch · 10 Nov 14:05 (3)")
        # A name in use, in any case: the refusal quotes the batch that has it, as that batch spells it.
        for name, existing in (("batch · 10 NOV 14:05", "Batch · 10 Nov 14:05"), ("default BATCH", "Default batch"),
                               (" Default  batch ", "Default batch")):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                batches.create(name)
            self.assertEqual(str(caught.exception), f"There is already a batch named “{existing}”. Choose another name.")
        # The app's rule for names: 200 characters, counted after the spaces are tidied.
        self.assertEqual(named_batches.MAX_NAME_CHARS, providers.MAX_LABEL_CHARS)
        self.assertEqual(named_batches.MAX_NAME_CHARS, 200)
        for value, message in (("x" * 201, "A batch name can have at most 200 characters."),
                               ("a\x07b", "A batch name cannot contain control characters."),
                               (7, "Enter the batch name as text."), (["a"], "Enter the batch name as text.")):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                batches.create(value)
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(batches.create("y" * 200)["name"], "y" * 200)
        self.assertEqual(batches.create("  " + "z" * 200 + "  ")["name"], "z" * 200)
        # Line breaks and tabs become single spaces.
        self.assertEqual(batches.create("Lower\nbounds\r\n for\tsorting")["name"], "Lower bounds for sorting")

    def test_the_month_is_english_and_a_minute_can_hold_many_batches(self):
        taken = {f"batch · 3 jan 09:07{suffix}" for suffix in ("", " (2)", " (3)")}
        when = datetime(2027, 1, 3, 9, 7)
        self.assertEqual(named_batches.generated_name(when, taken), "Batch · 3 Jan 09:07 (4)")
        self.assertEqual([named_batches.generated_name(datetime(2026, month, 1), set()).split()[3]
                          for month in range(1, 13)], list(named_batches.MONTHS))

    def test_no_cap_on_the_number_of_batches(self):
        batches = named_batches.NamedBatches(self.path)
        made = [batches.create(f"Batch {index}") for index in range(150)]
        self.assertEqual(len({entry["id"] for entry in made}), 150)
        again = named_batches.NamedBatches(self.path)
        self.assertEqual(again.problem, "")
        self.assertEqual([row["name"] for row in again.entries()], [f"Batch {index}" for index in range(150)])

    def test_a_large_list_is_read_in_full(self):
        # Far more than any real list, and larger than 1 MB: nothing is cut off and nothing is refused.
        self.path.parent.mkdir(parents=True)
        rows = [{"id": f"batch_{index:010x}", "name": f"Problems of group {index} " + "x" * 150,
                 "createdAt": "2026-11-10T13:05:00+00:00"} for index in range(6000)]
        self.path.write_text(json.dumps({"version": 1, "batches": rows}), encoding="utf-8")
        self.assertGreater(self.path.stat().st_size, 1024 * 1024)
        batches = named_batches.NamedBatches(self.path)
        self.assertEqual(batches.problem, "")
        self.assertEqual(len(batches.entries()), 6000)

    def test_rename_and_delete(self):
        batches = named_batches.NamedBatches(self.path)
        first, second = batches.create("One"), batches.create("Two")
        renamed = batches.rename(first["id"], "ONE")
        self.assertEqual((renamed["id"], renamed["name"], renamed["createdAt"]), (first["id"], "ONE", first["createdAt"]))
        self.assertEqual(batches.rename(first["id"], "one")["name"], "one")  # Its own name, in another case.
        # The refusal quotes the batch that has the name ("Two"), not the name as typed ("two").
        for typed, existing in (("two", "Two"), ("Default Batch", "Default batch")):
            with self.subTest(typed=typed), self.assertRaises(ValueError) as caught:
                batches.rename(first["id"], typed)
            self.assertEqual(str(caught.exception), f"There is already a batch named “{existing}”. Choose another name.")
        with self.assertRaisesRegex(ValueError, "Enter a name for the batch."):
            batches.rename(first["id"], "   ")
        batches.delete(second["id"])
        self.assertEqual([row["name"] for row in named_batches.NamedBatches(self.path).entries()], ["one"])
        for action in (lambda: batches.rename(second["id"], "Back"), lambda: batches.delete(second["id"]),
                       lambda: batches.delete("__root__")):
            with self.assertRaisesRegex(ValueError, "That batch no longer exists."):
                action()

    def test_a_damaged_list_is_reported_and_never_written(self):
        self.path.parent.mkdir(parents=True)
        row = {"id": "batch_0123456789", "name": "A", "createdAt": "2026-11-10T13:05:00+00:00"}
        cases = (
            ("{not json", "it is not valid JSON (line 1, column 2)"),
            (b"\xff\xfe{}", "it is not UTF-8 text"),
            (json.dumps({"version": 2, "batches": []}), "it has an unknown format"),
            (json.dumps([row]), "it has an unknown format"),
            ("[" * 100000 + "]" * 100000, "it has an unknown format"),
            (json.dumps({"version": 1, "batches": [{**row, "id": "../x"}]}), "it lists a batch without a valid ID"),
            (json.dumps({"version": 1, "batches": [row, {**row, "name": "B"}]}),
             "it lists two batches with the same ID"),
            (json.dumps({"version": 1, "batches": [row, {**row, "id": "batch_9876543210", "name": "a"}]}),
             "it lists a batch without a valid, unique name"),
            (json.dumps({"version": 1, "batches": [{**row, "name": 7}]}), "it lists a batch without a valid, unique name"),
            (json.dumps({"version": 1, "batches": [{**row, "name": "x" * 201}]}),
             "it lists a batch without a valid, unique name"),
            (json.dumps({"version": 1, "batches": [{**row, "name": "default batch"}]}),
             "it lists a batch without a valid, unique name"),
            (json.dumps({"version": 1, "batches": [{**row, "createdAt": "yesterday"}]}),
             "it lists a batch without the time it was made"),
            (json.dumps({"version": 1, "batches": [{key: value for key, value in row.items() if key != "createdAt"}]}),
             "it lists a batch without the time it was made"),
        )
        for content, reason in cases:
            with self.subTest(reason=reason, content=content[:40]):
                if isinstance(content, bytes):
                    self.path.write_bytes(content)
                else:
                    self.path.write_text(content, encoding="utf-8")
                before = self.path.read_bytes()
                batches = named_batches.NamedBatches(self.path)
                self.assertEqual(batches.problem, (
                    f"The list of batches ({self.path}) cannot be read: {reason}. Until it is repaired and TCS Prover "
                    "is started again, batches cannot be made, renamed or deleted, and the problems of the batches "
                    "it lists are shown in the default batch."))
                self.assertEqual((batches.entries(), batches.ids()), ([], set()))
                for action in (lambda: batches.create("New"), lambda: batches.rename(row["id"], "New"),
                               lambda: batches.delete(row["id"])):
                    with self.assertRaises(ValueError):
                        action()
                self.assertEqual(self.path.read_bytes(), before)

    def test_a_link_or_folder_in_place_of_the_list_is_refused(self):
        self.path.parent.mkdir(parents=True)
        target = self.path.parent / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "batches": []}), encoding="utf-8")
        self.path.symlink_to(target)
        batches = named_batches.NamedBatches(self.path)
        self.assertIn("cannot be read: it is not a regular file.", batches.problem)
        with self.assertRaises(ValueError):
            batches.create("New")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"version": 1, "batches": []})
        self.path.unlink()
        self.path.mkdir()
        self.assertIn("cannot be read: it is not a regular file.", named_batches.NamedBatches(self.path).problem)


if __name__ == "__main__":
    unittest.main()
