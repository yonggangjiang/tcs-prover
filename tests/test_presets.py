"""Saved New problem settings (ui/presets.py and the /presets routes): storage, checks and privacy.

Offline: the private state is a temporary folder per test, and no model, CLI or runner can start
(the stubs below fail any start). One test starts a second Python process that only saves settings,
to show that saves from two processes never lose each other.
"""

import http.client
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import claude_cli, providers
from ui import presets, server

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-tcs-presets-test-0123456789"
CUTOFF = "2026-01-15T00:00:00Z"
# The real Popen, for the one test that starts a second saving process (the stubs replace it).
POPEN = subprocess.Popen


class PresetCase(unittest.TestCase):
    """A private state folder of its own; nothing may start a runner, a CLI or a model session."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = self.root / "state" / "api-models.json"
        changes = [mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.store)}),
                   mock.patch.object(server, "grant_windows_access")]
        changes += [mock.patch.object(target, name, side_effect=AssertionError(f"{name} reached"))
                    for target, name in ((server.App, "_spawn_worker"), (server.subprocess, "Popen"),
                                         (runtime, "goal_session"), (runtime, "author_conversation"),
                                         (claude_cli, "goal_session"), (claude_cli, "author_conversation"))]
        for change in changes:
            change.start()
            self.addCleanup(change.stop)
        self.builtin = presets.builtin_settings()

    def settings(self, **values):
        return {**self.builtin, **values}

    def stored(self):
        return json.loads(presets.path().read_text(encoding="utf-8"))

    def names(self):
        return [row["name"] for row in presets.listing()["presets"]]

    def connection(self, label="Lab key"):
        """Save an API connection (no request: its models are given) and return it with its model ids."""

        entry = providers.save_api_connection(
            {"label": label, "format": "openai", "baseUrl": "https://api.lab.example/v1", "apiKey": KEY},
            [{"id": "model-a", "label": "Model A"}, {"id": "model-b", "label": "Model B"}])
        return entry, {row["id"]: row["modelId"] for row in entry["models"]}

    def write(self, value):
        presets.path().parent.mkdir(parents=True, exist_ok=True)
        presets.path().write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


class StorageTests(PresetCase):
    def test_nothing_saved_lists_nothing_and_the_built_in_settings(self):
        listing = presets.listing()
        self.assertEqual((listing["presets"], listing["defaultId"], listing["problem"]), ([], "", ""))
        self.assertEqual(listing["builtIn"]["name"], "Built-in settings")
        # The built-in settings are those a fresh form has without saved settings (the server's empty state).
        empty = server.empty_state()
        for key in presets.SETTINGS:
            with self.subTest(key=key):
                self.assertEqual(listing["builtIn"]["settings"][key], empty[key])
        self.assertEqual(listing["builtIn"]["labels"], {"gpt-6.1-sol": "GPT 6.1 Sol"})
        self.assertFalse(presets.path().exists(), "reading writes nothing")

    def test_save_list_and_default_round_trip_in_an_owner_only_file(self):
        identity = presets.save("Careful proof", self.settings(criticCount=5, thinkingHours=1.5, authorSubagents=2),
                                make_default=True)
        self.assertRegex(identity, r"^p-[0-9a-f]{12}$")
        listing = presets.listing()
        [row] = listing["presets"]
        self.assertEqual((listing["defaultId"], row["id"], row["name"], row["isDefault"]),
                         (identity, identity, "Careful proof", True))
        self.assertEqual(row["settings"], self.settings(criticCount=5, thinkingHours=1.5, authorSubagents=2))
        self.assertEqual(row["unavailable"], [])
        self.assertTrue(row["createdAt"] and row["createdAt"] == row["updatedAt"])
        self.assertEqual(self.stored()["version"], 1)
        self.assertEqual(self.stored()["defaultId"], identity)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(presets.path().stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(presets.path().parent.stat().st_mode), 0o700)
        # Unticked, a save never changes the default; the default moves or clears only when asked.
        other = presets.save("Quick check", self.settings(), make_default=False)
        self.assertEqual(presets.listing()["defaultId"], identity)
        presets.set_default(other)
        self.assertEqual(presets.listing()["defaultId"], other)
        presets.set_default("")
        listing = presets.listing()
        self.assertEqual((listing["defaultId"], [row["isDefault"] for row in listing["presets"]]), ("", [False, False]))

    def test_the_file_and_its_lock_are_hidden_from_every_model_sandbox(self):
        presets.save("Hidden", self.settings())
        files = [Path(os.path.realpath(path)) for path in (presets.path(),
                                                           presets.path().with_name(providers.PRESETS_LOCK))]
        self.assertTrue(all(path.exists() for path in files[:1 if presets.fcntl is None else 2]))
        for path in files:
            with self.subTest(path=path.name):
                self.assertIn(path, providers.private_paths())
                # The Claude shell sandbox hides it from every role; so does the Codex permission profile (macOS).
                self.assertIn(path, claude_cli.hidden_paths(api=False))
                profile = runtime.private_permissions(":workspace", "gpt-6.1-sol")
                if profile is not None:
                    self.assertEqual(profile["filesystem"][str(path)], "deny")

    def test_a_blank_name_a_name_in_use_rename_and_delete(self):
        first = presets.save("", self.settings())
        second = presets.save(None, self.settings())
        self.assertEqual(self.names(), ["Saved settings 1", "Saved settings 2"])
        # A name in use, ignoring case, is refused unless those settings are replaced (under their id).
        with self.assertRaisesRegex(presets.PresetError, "Saved settings called “Saved settings 1” already exist"):
            presets.save("saved SETTINGS 1", self.settings(criticCount=4))
        created = presets.listing()["presets"][0]["createdAt"]
        self.assertEqual(presets.save("Saved Settings 1", self.settings(criticCount=4), replace_id=first), first)
        row = presets.listing()["presets"][0]
        self.assertEqual((row["id"], row["name"], row["settings"]["criticCount"], row["createdAt"]),
                         (first, "Saved Settings 1", 4, created))
        with self.assertRaisesRegex(presets.PresetError, "no longer exist"):
            presets.save("Elsewhere", self.settings(), replace_id="p-000000000000")
        # Rename: another's name is refused; the same name in another case is not.
        with self.assertRaisesRegex(presets.PresetError, "already exist"):
            presets.rename(second, "saved settings 1")
        presets.rename(second, "Quick check")
        presets.rename(second, "QUICK check")
        self.assertEqual(self.names(), ["Saved Settings 1", "QUICK check"])
        for bad, message in (("", "Enter the name"), ("x" * 201, "longer than 200"), ("tab\there", "control")):
            with self.subTest(name=bad), self.assertRaisesRegex(presets.PresetError, message):
                presets.rename(second, bad)
        presets.rename(second, "y" * 200)
        # Deleting the default leaves no default; what no longer exists says so.
        presets.set_default(first)
        presets.delete(first)
        listing = presets.listing()
        self.assertEqual(([row["id"] for row in listing["presets"]], listing["defaultId"]), ([second], ""))
        for change in (lambda: presets.delete(first), lambda: presets.rename(first, "Back"),
                       lambda: presets.set_default(first)):
            with self.assertRaisesRegex(presets.PresetError, "no longer exist"):
                change()

    def test_names_in_use_ignore_case_as_the_page_compares_them(self):
        # Lower case on both sides (the page uses toLowerCase()): casefold() would also take "STRASSE" for
        # "Straße", which the page lets through without asking, so the page could never offer to replace them.
        first = presets.save("Straße", self.settings())
        second = presets.save("STRASSE", self.settings())
        self.assertEqual(sorted(self.names()), ["STRASSE", "Straße"])
        self.assertEqual([presets.name_key(name) for name in ("Straße", "STRASSE", "STRAẞE", "ﬁle")],
                         ["straße", "strasse", "straße", "ﬁle"])
        # "STRAẞE" (a capital sharp s) is the same name: refused unless those settings are replaced.
        with self.assertRaisesRegex(presets.PresetError, "Saved settings called “Straße” already exist"):
            presets.save("STRAẞE", self.settings())
        self.assertEqual(presets.save("STRAẞE", self.settings(criticCount=4), replace_id=first), first)
        presets.rename(second, "strasse")
        with self.assertRaisesRegex(presets.PresetError, "Saved settings called “STRAẞE” already exist"):
            presets.rename(second, "straße")
        self.assertEqual(sorted(self.names()), ["STRAẞE", "strasse"])

    def test_only_the_settings_of_the_form_are_kept(self):
        values = {**self.settings(criticCount=4), "statement": "Prove that P differs from NP.", "content": "Draft",
                  "relatedWorks": ["upload-token-1"], "promptOverrides": {"author": "Edited author [STATEMENT]"},
                  "authorPrompt": "Edited author [STATEMENT]", "criticPrompt": "Edited critic {count}",
                  "apiKey": KEY, "baseUrl": "https://api.lab.example/v1", "retrievalPolicyPath": "/runs/policy.json",
                  "maxRunningJobs": 3, "theme": "crimson", "somethingNew": True}
        presets.save("Everything", values)
        [stored] = self.stored()["presets"]
        self.assertEqual(set(stored), {"id", "name", "createdAt", "updatedAt", "settings", "apiModels"})
        self.assertEqual(list(stored["settings"]), list(presets.SETTINGS))
        self.assertEqual(stored["apiModels"], {})
        raw = presets.path().read_text(encoding="utf-8")
        for text in ("Prove that", "Draft", "upload-token", "Edited", "Prompt", KEY, "https://", "policy",
                     "maxRunningJobs", "crimson", "somethingNew"):
            with self.subTest(text=text):
                self.assertNotIn(text, raw)

    def test_missing_settings_take_the_built_in_ones_and_ultra_is_kept_as_max(self):
        self.write({"version": 1, "defaultId": "p-0123456789ab", "presets": [
            {"id": "p-0123456789ab", "name": "Older", "settings": {"criticCount": 7, "retiredSetting": 1}}]})
        [row] = presets.listing()["presets"]
        self.assertEqual((row["settings"], row["isDefault"]), (self.settings(criticCount=7), True))
        identity = presets.save("Ultra", self.settings(authorEffort="ultra"))
        self.assertEqual(next(row for row in presets.listing()["presets"] if row["id"] == identity)
                         ["settings"]["authorEffort"], "max")

    def test_values_the_form_could_not_send_are_refused(self):
        none = {"review": False, "author": False, "critic": False, "writing": False}
        for values in ({"criticCount": 0}, {"criticCount": 11}, {"criticCount": 2.5}, {"criticRounds": 0},
                       {"criticRounds": 101}, {"authorSubagents": -1}, {"authorSubagents": 11},
                       {"thinkingHours": -1}, {"thinkingHours": float("nan")}, {"thinkingHours": True},
                       {"thinkingHours": "soon"}, {"authorEffort": "turbo"}, {"reviewEffort": None},
                       {"speedMode": "warp"}, {"reasoningSummary": "everything"}, {"authorModel": "gpt-unknown"},
                       {"criticModel": "deepseek-v4-pro"}, {"writerModel": "api:"}, {"reviewModel": 5},
                       {"workflowStages": none}, {"workflowStages": {"review": "yes"}},
                       {"retrievalMode": "sometimes"}, {"retrievalMode": "historical", "retrievalCutoffUtc": ""},
                       {"retrievalMode": "historical", "retrievalCutoffUtc": "2026-01-15"},
                       {"retrievalMode": "historical", "retrievalCutoffUtc": "2026-02-30T00:00:00Z"},
                       {"retrievalMode": "live", "retrievalCutoffUtc": CUTOFF},
                       {"retrievalMode": "disabled", "retrievalCutoffUtc": CUTOFF}):
            with self.subTest(values=values), self.assertRaises(presets.PresetError):
                presets.save("Bad", self.settings(**values))
        for body in ([], "settings", None):
            with self.subTest(settings=body), self.assertRaises(presets.PresetError):
                presets.save("Bad", body)
        with self.assertRaises(presets.PresetError):
            presets.save("Bad", self.settings(), make_default="yes")
        self.assertFalse(presets.path().exists())

    def test_settings_of_a_stage_that_is_off_are_checked_as_at_start(self):
        # Start checks a stage's own settings only while it is on, and runs a stage that is off with the
        # built-in ones; a save follows it: a valid value is kept (turning the stage on again restores it),
        # one that is not valid becomes the built-in value instead of a refusal.
        off = {"review": True, "author": False, "critic": False, "writing": True}
        hidden = {"criticCount": 0, "criticRounds": "", "authorSubagents": -1, "thinkingHours": "",
                  "authorModel": "", "authorEffort": "turbo", "criticModel": "deepseek-v4-pro", "criticEffort": None}
        server.App._workflow_options(critic_count=0, critic_rounds="", author_subagents=-1, thinking_hours="",
                                     author_model="", author_effort="turbo", critic_model="deepseek-v4-pro",
                                     workflow_stages=off)
        identity = presets.save("Stages off", self.settings(workflowStages=off, **hidden))
        [row] = presets.listing()["presets"]
        self.assertEqual((row["id"], row["settings"]), (identity, self.settings(workflowStages=off)))
        kept = {"criticCount": 7, "criticRounds": 4, "authorSubagents": 2, "thinkingHours": 1.5,
                "authorModel": "gpt-6-luna", "authorEffort": "high", "criticModel": "claude-opus-5-5"}
        identity = presets.save("Kept", self.settings(workflowStages=off, **kept))
        saved = next(row for row in presets.listing()["presets"] if row["id"] == identity)["settings"]
        self.assertEqual({key: saved[key] for key in kept}, kept)
        # The stages that are on are checked as before; so is any other setting.
        for stage, values in (("critic", {"criticCount": 0}), ("critic", {"criticRounds": ""}),
                              ("author", {"authorSubagents": -1}), ("author", {"thinkingHours": ""}),
                              ("author", {"authorModel": ""}), ("writing", {"writerEffort": "turbo"}),
                              ("review", {"speedMode": "warp"})):
            with self.subTest(stage=stage, values=values), self.assertRaises(presets.PresetError):
                presets.save("Bad", self.settings(workflowStages={**off, stage: True}, **values))

    def test_a_web_search_cutoff_is_kept_as_saved(self):
        stages = {"review": False, "author": True, "critic": True, "writing": True}
        historical = presets.save("Historical", self.settings(workflowStages=stages, retrievalMode="historical",
                                                              retrievalCutoffUtc=f" {CUTOFF} "))
        disabled = presets.save("No web search", self.settings(retrievalMode="historical", retrievalCutoffUtc="-1"))
        # Whether a cutoff can run (in the past, on this computer, with these models) is checked at Start.
        future = presets.save("Future", self.settings(workflowStages=stages, retrievalMode="historical",
                                                      retrievalCutoffUtc="2999-01-01T00:00:00Z"))
        rows = {row["id"]: row["settings"] for row in presets.listing()["presets"]}
        self.assertEqual((rows[historical]["retrievalMode"], rows[historical]["retrievalCutoffUtc"]),
                         ("historical", CUTOFF))
        self.assertEqual((rows[disabled]["retrievalMode"], rows[disabled]["retrievalCutoffUtc"]), ("disabled", "-1"))
        self.assertEqual(rows[future]["retrievalCutoffUtc"], "2999-01-01T00:00:00Z")


class RemovedModelTests(PresetCase):
    def test_a_removed_api_model_is_listed_and_the_file_stays_as_saved(self):
        entry, models = self.connection()
        model = models["model-a"]
        presets.save("Lab author", self.settings(authorModel=model, criticModel=model, criticEffort="high"),
                     make_default=True)
        # A record of the model without key or address, kept only to name it once it is gone.
        self.assertEqual(self.stored()["presets"][0]["apiModels"], {model: {
            "label": "Model A", "model": "model-a", "format": "openai", "sourceId": entry["id"],
            "sourceLabel": "Lab key"}})
        [row] = presets.listing()["presets"]
        self.assertEqual((row["unavailable"], row["labels"][model]), ([], "Model A"))
        saved = presets.path().read_bytes()

        providers.delete_api_model(entry["id"])
        [row] = presets.listing()["presets"]
        self.assertEqual(row["unavailable"], [
            {"role": role, "model": model, "label": "Lab key · Model A",
             "text": f"{word} model not available: Lab key · Model A"}
            for role, word in (("author", "Author"), ("critic", "Critic"))])
        self.assertEqual((row["labels"][model], row["isDefault"], row["settings"]["criticEffort"]),
                         ("Model A", True, "high"))
        self.assertEqual(presets.path().read_bytes(), saved, "listing never rewrites saved settings")

        # The same display name gives the connection its ids back: the saved settings work again.
        self.connection()
        [row] = presets.listing()["presets"]
        self.assertEqual(row["unavailable"], [])
        self.assertEqual(presets.path().read_bytes(), saved)

    def test_a_connection_list_that_cannot_be_read_never_calls_a_model_missing(self):
        _, models = self.connection()
        presets.save("Lab author", self.settings(authorModel=models["model-a"]))
        self.store.write_text("{damaged", encoding="utf-8")
        with mock.patch.dict(providers._LAST_GOOD, clear=True):
            [row] = presets.listing()["presets"]
        self.assertEqual((row["unavailable"], row["labels"][models["model-a"]]), ([], "Model A"))

    def test_a_model_that_is_no_longer_supported_loads_as_its_replacement(self):
        self.write({"version": 1, "defaultId": "", "presets": [
            {"id": "p-0123456789ab", "name": "Older", "settings": {"criticModel": "deepseek-v4-pro"}}]})
        [row] = presets.listing()["presets"]
        self.assertNotIn("unusable", row)
        self.assertEqual([(item["role"], item["model"]) for item in row["unavailable"]], [("critic", "deepseek-v4-pro")])
        self.assertIn("no longer supported", row["unavailable"][0]["text"])

    def test_saving_with_a_model_that_is_not_configured_keeps_its_earlier_record(self):
        entry, models = self.connection()
        model = models["model-b"]
        identity = presets.save("Lab critic", self.settings(criticModel=model))
        providers.delete_api_model(entry["id"])
        presets.save("Lab critic", self.settings(criticModel=model, criticCount=4), replace_id=identity)
        self.assertEqual(self.stored()["presets"][0]["apiModels"][model]["label"], "Model B")
        self.assertEqual(presets.listing()["presets"][0]["unavailable"][0]["label"], "Lab key · Model B")


class DamagedFileTests(PresetCase):
    def assert_never_written(self, problem):
        before = presets.path().read_bytes() if not presets.path().is_symlink() else None
        listing = presets.listing()
        self.assertEqual((listing["presets"], listing["defaultId"]), ([], ""))
        self.assertIn(problem, listing["problem"])
        self.assertIn("new problems open with the built-in settings", listing["problem"])
        for change in (lambda: presets.save("New", self.settings()), lambda: presets.rename("p-0123456789ab", "x"),
                       lambda: presets.delete("p-0123456789ab"), lambda: presets.set_default("")):
            with self.assertRaises(presets.PresetsUnavailable):
                change()
        if before is not None:
            self.assertEqual(presets.path().read_bytes(), before)

    def test_a_damaged_file_is_reported_and_never_overwritten(self):
        for text in ("{not json", "[]", json.dumps({"version": 1}), json.dumps({"version": "1", "presets": []})):
            with self.subTest(text=text):
                self.write(text)
                self.assert_never_written("is damaged; fix or remove it")

    def test_a_file_from_a_newer_version_is_never_overwritten(self):
        self.write({"version": 2, "defaultId": "", "presets": []})
        self.assert_never_written("comes from a newer version of TCS Prover")

    @unittest.skipIf(os.name == "nt", "symbolic links need privileges on Windows")
    def test_a_symbolic_link_is_reported_and_its_target_never_written(self):
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "defaultId": "", "presets": []}), encoding="utf-8")
        presets.path().parent.mkdir(parents=True)
        presets.path().symlink_to(target)
        before = target.read_bytes()
        self.assert_never_written("is a symbolic link")
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(presets.path().is_symlink())

    def test_entries_that_cannot_be_used_are_listed_and_kept_by_every_write(self):
        good = {"id": "p-0123456789ab", "name": "Good", "settings": {"criticCount": 4}, "extra": "kept"}
        bad = {"id": "p-ba0123456789", "name": "Too many critics", "settings": {"criticCount": 99}}
        entries = [good, bad, "not an object", {"id": "p-0123456789ab", "name": "Twin", "settings": {}}]
        self.write({"version": 1, "defaultId": "p-0123456789ab", "presets": entries})
        rows = presets.listing()["presets"]
        self.assertEqual([(row["name"], row.get("unusable", False)) for row in rows],
                         [("Good", False), ("Too many critics", True), ("Unreadable saved settings", True),
                          ("Twin", True)])
        self.assertIn("Choose 1 to 10 independent critics", rows[1]["problem"])
        self.assertEqual(rows[3]["problem"], "Other saved settings have the same id.")
        self.assertTrue(all(row["id"].startswith("invalid-") for row in rows[1:]))
        # Saving, renaming and making a default keep every other entry exactly as it was.
        identity = presets.save("New", self.settings())
        presets.rename("p-0123456789ab", "Good again")
        presets.set_default(identity)
        stored = self.stored()["presets"]
        self.assertEqual(stored[1:4], entries[1:4])
        self.assertEqual((stored[0]["extra"], stored[0]["name"], stored[0]["settings"]), ("kept", "Good again",
                                                                                         {"criticCount": 4}))
        # A name in use only by an entry that cannot be used is free; that entry can still be deleted.
        presets.save("Too many critics", self.settings())
        reference = presets.listing()["presets"][1]["id"]
        presets.delete(reference)
        self.assertNotIn(bad, self.stored()["presets"])
        with self.assertRaises(presets.PresetError):
            presets.set_default(presets.listing()["presets"][1]["id"])


    def test_deleting_a_copy_of_the_default_with_the_same_id_keeps_the_default(self):
        good = {"id": "p-0123456789ab", "name": "Good", "settings": {"criticCount": 4}}
        twin = {"id": "p-0123456789ab", "name": "Twin", "settings": {}}
        self.write({"version": 1, "defaultId": "p-0123456789ab", "presets": [good, twin]})
        rows = presets.listing()["presets"]
        self.assertEqual([(row["name"], row.get("unusable", False)) for row in rows], [("Good", False), ("Twin", True)])
        # The copy (a hand copy-paste: listed as unusable, Delete only) goes; the default stays with Good.
        presets.delete(rows[1]["id"])
        listing = presets.listing()
        self.assertEqual((listing["defaultId"], [(row["name"], row["isDefault"]) for row in listing["presets"]]),
                         ("p-0123456789ab", [("Good", True)]))
        self.assertEqual(self.stored()["defaultId"], "p-0123456789ab")
        # Deleting the usable default itself leaves none, also while a copy with its id is left.
        self.write({"version": 1, "defaultId": "p-0123456789ab", "presets": [good, twin]})
        presets.delete("p-0123456789ab")
        self.assertEqual((self.stored()["defaultId"], self.stored()["presets"]), ("", [twin]))
        self.assertEqual(presets.listing()["defaultId"], "")

    def test_a_default_that_cannot_be_used_now_goes_only_with_its_own_entry(self):
        # Say a newer TCS Prover saved it with a model this one does not know: deleting other settings keeps
        # it for that version; deleting its own entry leaves no default behind.
        newer = {"id": "p-ba0123456789", "name": "Newer", "settings": {"authorModel": "gpt-9"}}
        other = {"id": "p-0123456789ab", "name": "Other", "settings": {}}
        self.write({"version": 1, "defaultId": "p-ba0123456789", "presets": [newer, other]})
        self.assertEqual(presets.listing()["defaultId"], "", "new problems open with the built-in settings")
        presets.delete("p-0123456789ab")
        self.assertEqual(self.stored()["defaultId"], "p-ba0123456789")
        presets.delete(presets.listing()["presets"][0]["id"])
        self.assertEqual(self.stored(), {"version": 1, "defaultId": "", "presets": []})


class ConcurrencyTests(PresetCase):
    def test_two_saves_at_the_same_time_both_survive(self):
        barrier, errors = threading.Barrier(4), []

        def save(name):
            try:
                barrier.wait()
                presets.save(name, self.settings())
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        threads = [threading.Thread(target=save, args=(f"Thread {index}",)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(self.names()), [f"Thread {index}" for index in range(4)])

    @unittest.skipIf(presets.fcntl is None, "file locks are POSIX only")
    def test_a_save_in_another_process_waits_for_the_lock(self):
        child_code = (f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
                      "from ui import presets\n"
                      "settings = presets.builtin_settings()\n"
                      "print('ready', flush=True)\n"
                      "presets.save('From the other process', settings)\n")
        with presets._locked():
            # This process holds the lock: the other process reads the file only after it is released.
            child = POPEN([sys.executable, "-c", child_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=dict(os.environ))
            self.addCleanup(lambda: child.poll() is None and child.kill())
            self.assertEqual(child.stdout.readline().strip(), "ready")
            threading.Event().wait(0.5)
            self.assertIsNone(child.poll(), "the other save waits for the lock")
            data = presets._load()
            item = {"id": "p-0123456789ab", "name": "From this process", "settings": self.settings(),
                    "apiModels": {}, "createdAt": "", "updatedAt": ""}
            presets._write([*data["rows"], presets.Row(item, presets._stored(item), "", item["id"])], "")
        _, errors = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 0, errors)
        self.assertEqual(sorted(self.names()), ["From the other process", "From this process"])


class RouteTests(PresetCase):
    """The routes of a real Server on 127.0.0.1, with its session token and origin checks."""

    def setUp(self):
        super().setUp()
        runs = self.root / "runs"
        runs.mkdir()
        self.http = server.Server((server.HOST, 0), runs=runs)
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)

    def call(self, path, body=None, *, headers=None):
        values = {"Origin": self.http.origin, "X-TCS-Prover-Token": self.http.token, **(headers or {})}
        values = {key: value for key, value in values.items() if value is not None}
        raw = None
        if body is not None:
            raw, values["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=20)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=values)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        self.assertNotIn(KEY.encode(), data, "no answer ever carries a key")
        return response.status, json.loads(data)

    def test_save_list_rename_default_and_delete(self):
        entry, models = self.connection()
        status, saved = self.call("/presets", {"name": "Lab author", "makeDefault": True, "settings": self.settings(
            authorModel=models["model-a"], criticCount=4)})
        self.assertEqual(status, 200)
        identity = saved["savedId"]
        self.assertEqual((saved["defaultId"], saved["presets"][0]["name"], saved["presets"][0]["settings"]["criticCount"]),
                         (identity, "Lab author", 4))
        self.assertEqual(saved["builtIn"]["settings"], self.builtin)
        status, listing = self.call("/presets")
        self.assertEqual((status, [row["id"] for row in listing["presets"]]), (200, [identity]))
        status, renamed = self.call("/presets/rename", {"id": identity, "name": "Renamed"})
        self.assertEqual((status, renamed["presets"][0]["name"]), (200, "Renamed"))
        status, cleared = self.call("/presets/default", {"id": ""})
        self.assertEqual((status, cleared["defaultId"]), (200, ""))
        status, refused = self.call("/presets/default", {})
        self.assertEqual(status, 400)
        self.assertIn("Choose saved settings", refused["error"])
        status, refused = self.call("/presets", {"name": "Renamed", "settings": self.settings()})
        self.assertEqual(status, 400)
        self.assertIn("already exist", refused["error"])
        status, refused = self.call("/presets", {"name": "Bad", "settings": self.settings(criticRounds=0)})
        self.assertEqual((status, refused["error"]), (400, "Choose 1 to 100 critic rounds."))
        status, deleted = self.call("/presets/delete", {"id": identity})
        self.assertEqual((status, deleted["presets"]), (200, []))
        status, refused = self.call("/presets/delete", {"id": identity})
        self.assertEqual((status, refused["error"]), (400, presets.GONE))

    def test_requests_without_the_token_or_from_another_page_are_refused(self):
        presets.save("Private", self.settings())
        before = presets.path().read_bytes()
        for path, body in (("/presets", None), ("/presets", {"name": "x", "settings": self.settings()}),
                           ("/presets/rename", {"id": "p-0123456789ab", "name": "x"}),
                           ("/presets/delete", {"id": "p-0123456789ab"}), ("/presets/default", {"id": ""})):
            for headers in ({"X-TCS-Prover-Token": None}, {"X-TCS-Prover-Token": "wrong-token"},
                            {"Host": "attacker.example"}, {"Origin": "http://attacker.example"}):
                with self.subTest(path=path, method="POST" if body is not None else "GET", headers=headers):
                    status, _ = self.call(path, body, headers=headers)
                    self.assertEqual(status, 403)
        self.assertEqual(presets.path().read_bytes(), before)

    def test_a_saved_batch_cannot_be_named(self):
        # A saved (read-only) folder of runs under runs/, and a batch that does not exist: both are refused before
        # the saved settings are touched, each with its own words (the batch guard in the request handler).
        older = self.http.runs / "older" / "2026-01-01_00-00-00_saved-job"
        older.mkdir(parents=True)
        (older / "job-settings.json").write_text("{}")
        for batch, refusal in (("older", "Saved batches are read-only."), ("gone", "That batch no longer exists.")):
            with self.subTest(batch=batch):
                status, value = self.call(f"/presets/default?batch={batch}", {"id": ""})
                self.assertEqual((status, value["error"]), (403, refusal))
        self.assertFalse(presets.path().exists())


if __name__ == "__main__":
    unittest.main()
