"""Model registry and the private API model store; no network or model calls."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from runner import providers

KEY = "sk-test-abcdefghijklmnop1234"


class StoreCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = self.root / "state" / "api-models.json"
        patcher = mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.store)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, **values):
        return providers.save_api_model({"format": "openai", "baseUrl": "https://api.example.com/v1",
                                         "model": "gpt-x", "apiKey": KEY, **values})


class RegistryTests(StoreCase):
    def test_catalog_lists_codex_then_claude_models_with_their_efforts(self):
        rows = providers.catalog()
        self.assertEqual([row["id"] for row in rows], [
            "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
            "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
        first = rows[0]
        self.assertEqual(first, {"id": "gpt-6.1-sol", "label": "GPT 6.1 Sol", "provider": "codex",
                                 "providerLabel": "Codex CLI", "efforts": list(providers.CODEX_EFFORTS),
                                 "defaultEffort": "max", "supportsFast": True})
        luna = next(row for row in rows if row["id"] == "gpt-6-luna")
        self.assertNotIn("ultra", luna["efforts"])
        for row in rows[4:]:
            self.assertEqual((row["provider"], row["providerLabel"], row["supportsFast"]), ("claude", "Claude CLI", False))
            self.assertEqual(row["efforts"], ["low", "medium", "high", "xhigh", "max"])
        legacy = providers.catalog(include_legacy=True)[8:]
        self.assertEqual([row["id"] for row in legacy], list(providers.LEGACY_CODEX_MODELS))
        self.assertTrue(all(row["legacy"] for row in legacy))

    def test_configured_api_models_follow_the_built_ins_without_secrets(self):
        openai = self.save(label="Gateway")
        anthropic = self.save(format="anthropic", baseUrl="https://claude.example.com", model="claude-x")
        rows = providers.catalog()
        self.assertEqual([row["id"] for row in rows[8:]], [openai["id"], anthropic["id"]])
        row = rows[8]
        self.assertEqual(row, {"id": openai["id"], "label": "Gateway", "provider": "api", "providerLabel": "API",
                               "efforts": ["low", "medium", "high", "xhigh", "max"], "defaultEffort": "max",
                               "supportsFast": False, "format": "openai", "host": "api.example.com", "hasKey": True})
        self.assertEqual(rows[9]["efforts"], list(providers.CLAUDE_EFFORTS))
        self.assertNotIn(KEY, json.dumps(rows))
        self.assertNotIn(KEY, json.dumps(providers.lookup(openai["id"])))

    def test_effective_effort_clamps_to_the_model_list(self):
        self.assertEqual(providers.effective_effort("gpt-6-luna", "ultra"), "max")
        self.assertEqual(providers.effective_effort("gpt-5.6-luna", "ultra"), "max")
        self.assertEqual(providers.effective_effort("claude-opus-5-5", "ultra"), "max")
        self.assertEqual(providers.effective_effort("gpt-6.1-sol", "ultra"), "ultra")
        api = self.save()["id"]
        self.assertEqual(providers.effective_effort(api, "ultra"), "max")
        for model in providers.offered_models():
            for effort in providers.CODEX_EFFORTS:
                self.assertIn(providers.effective_effort(model, effort), providers.efforts_for(model))
        with self.assertRaises(providers.ProviderError):
            providers.effective_effort("gpt-6.1-sol", "extreme")
        with self.assertRaises(providers.ProviderError):
            providers.effective_effort("gpt-9", "max")

    def test_harness_kind_fast_and_schema_enforcement(self):
        openai = self.save()["id"]
        anthropic = self.save(format="anthropic", model="claude-x")["id"]
        expectations = {
            "gpt-6.1-sol": ("codex", "codex", True, True), "gpt-5.6-terra": ("codex", "codex", True, True),
            "claude-haiku-5-5": ("claude", "claude", False, True),
            openai: ("api", "codex", False, False), anthropic: ("api", "claude", False, True),
        }
        for model, (kind, harness, fast, schema) in expectations.items():
            with self.subTest(model=model):
                self.assertEqual(providers.provider_kind(model), kind)
                self.assertEqual(providers.harness(model), harness)
                self.assertEqual(providers.supports_fast(model), fast)
                self.assertEqual(providers.enforces_output_schema(model), schema)
        self.assertEqual(providers.wire_model(openai), "gpt-x")
        self.assertEqual(providers.wire_model("claude-opus-5-5"), "claude-opus-5-5")

    def test_known_ids_and_removed_deepseek_routes(self):
        for model in (*providers.offered_models(), *providers.LEGACY_CODEX_MODELS):
            self.assertTrue(providers.is_known(model))
        for model in ("deepseek-v4-pro", "deepseek/deepseek-v4-pro", "gpt-9", "api:missing", "", None, 3):
            self.assertFalse(providers.is_known(model))
        self.assertEqual(providers.replacement_for_removed("deepseek-v4-pro"), "gpt-6.1-sol")
        self.assertEqual(providers.replacement_for_removed("deepseek/deepseek-v4-pro"), "gpt-6.1-sol")
        self.assertIsNone(providers.replacement_for_removed("gpt-6.1-sol"))
        self.assertEqual(providers.REMOVED_MODEL_WARNING,
                         "DeepSeek is no longer supported; this role now uses GPT 6.1 Sol.")


class StoreTests(StoreCase):
    def test_save_creates_private_files_and_never_returns_the_key(self):
        model = self.save(label="My Gateway")
        self.assertEqual(model["id"], "api:my-gateway")
        self.assertEqual((model["hasKey"], model["keyHint"]), (True, "…1234"))
        self.assertNotIn(KEY, json.dumps(model))
        self.assertNotIn(KEY, json.dumps(providers.load_api_models()))
        self.assertEqual(providers.api_model_secret(model["id"]), KEY)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.store.parent.stat().st_mode), 0o700)
        saved = json.loads(self.store.read_text())
        self.assertEqual(saved["version"], 1)
        entry = saved["models"][0]
        self.assertEqual(set(entry), {"id", "label", "format", "baseUrl", "headerName", "model", "apiKey",
                                      "createdAt", "updatedAt", "lastTest"})
        self.assertEqual((entry["headerName"], entry["baseUrl"]), ("Authorization", "https://api.example.com/v1"))

    def test_defaults_ids_and_bearer_prefix(self):
        anthropic = providers.save_api_model({"format": "anthropic", "baseUrl": "https://gw.example.com/v1/",
                                              "model": "claude-x", "apiKey": f"  Bearer {KEY}  "})
        self.assertEqual((anthropic["headerName"], anthropic["label"], anthropic["baseUrl"]),
                         ("x-api-key", "claude-x · gw.example.com", "https://gw.example.com/v1"))
        self.assertEqual(providers.api_model_secret(anthropic["id"]), KEY)
        second = providers.save_api_model({"format": "anthropic", "baseUrl": "https://gw.example.com",
                                           "model": "claude-x", "apiKey": KEY})
        self.assertNotEqual(second["id"], anthropic["id"])
        self.assertTrue(second["id"].startswith("api:claude-x"))
        explicit = self.save(id="api:team.gw_1")
        self.assertEqual(explicit["id"], "api:team.gw_1")

    def test_update_keeps_the_saved_key_and_resets_a_stale_test(self):
        model = self.save()
        providers.record_api_model_test(model["id"], {"ok": True, "status": 200, "message": "OK", "latencyMs": 12})
        self.assertTrue(providers.api_model(model["id"])["lastTest"]["ok"])
        renamed = providers.save_api_model({"id": model["id"], "label": "Renamed", "apiKey": ""})
        self.assertEqual(renamed["label"], "Renamed")
        self.assertTrue(renamed["lastTest"]["ok"])  # Nothing that affects a request changed.
        self.assertEqual(providers.api_model_secret(model["id"]), KEY)
        model_name = providers.save_api_model({"id": model["id"], "model": "gpt-y"})  # Same endpoint and header.
        self.assertIsNone(model_name["lastTest"])
        self.assertEqual(providers.api_model_secret(model["id"]), KEY)
        moved = providers.save_api_model({"id": model["id"], "baseUrl": "https://other.example.com/v1", "apiKey": KEY})
        self.assertIsNone(moved["lastTest"])
        self.assertEqual(providers.api_model_secret(model["id"]), KEY)
        providers.save_api_model({"id": model["id"], "apiKey": "replacement-key-9876"})
        self.assertEqual(providers.api_model_secret(model["id"]), "replacement-key-9876")
        self.assertEqual(len(providers.load_api_models()), 1)

    def test_validation_rejects_unsafe_or_malformed_settings(self):
        invalid = [
            {"format": "gemini"},
            {"baseUrl": "ftp://api.example.com"}, {"baseUrl": "api.example.com"},
            {"baseUrl": "http://api.example.com/v1"},  # Plain HTTP only for loopback.
            {"baseUrl": "https://user:pass@api.example.com"}, {"baseUrl": "https://api.example.com/v1#x"},
            {"baseUrl": "https://api.example.com/v1?api-version=1"}, {"baseUrl": "https://api.example.com/ v1"},
            {"baseUrl": "https://" + "a" * 2050 + ".com"}, {"baseUrl": "https://api.example.com:99999"},
            {"headerName": "X Key"}, {"headerName": "X-Key:"}, {"headerName": "x" * 65}, {"headerName": "Ünicode"},
            {"model": ""}, {"model": "gpt\nx"}, {"model": "--dangerously-skip-permissions"}, {"model": "m" * 201},
            {"apiKey": ""}, {"apiKey": "Bearer "}, {"apiKey": "two words"}, {"apiKey": "line\nbreak"},
            {"apiKey": "k" * 4097}, {"label": "x" * 201}, {"label": "bad\x00label"}, {"id": "api:Upper"},
            {"id": "gpt-6.1-sol"},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(providers.ProviderError) as failure:
                self.save(**values)
            self.assertNotIn(KEY, str(failure.exception))
        self.assertFalse(self.store.exists())
        for base in ("http://localhost:8080/v1", "http://127.0.0.1:9/v1", "http://[::1]:8000"):
            self.assertEqual(self.save(baseUrl=base)["baseUrl"], base)

    def test_atomic_write_keeps_the_previous_store_when_replacement_fails(self):
        first = self.save()
        before = self.store.read_text()
        with mock.patch.object(providers.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.save(label="Second")
        self.assertEqual(self.store.read_text(), before)
        self.assertEqual([model["id"] for model in providers.load_api_models()], [first["id"]])
        # The temporary copy is staged in the private staging folder and removed.
        self.assertEqual(sorted(path.name for path in self.store.parent.iterdir()),
                         [".api-models-staging", "api-models.json"])
        self.assertEqual(list((self.store.parent / ".api-models-staging").iterdir()), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.store.parent / ".api-models-staging").stat().st_mode), 0o700)

    def test_damaged_store_is_ignored_by_readers_and_never_overwritten(self):
        self.store.parent.mkdir(parents=True)
        self.store.write_text('{"version": 1, "models": [{"apiKey": "' + KEY + '"')
        self.assertEqual(providers.load_api_models(), [])
        self.assertEqual([row["id"] for row in providers.catalog()], list(providers.offered_models()))
        with self.assertRaises(providers.ProviderError) as failure:
            self.save()
        self.assertIn("damaged", str(failure.exception))
        self.assertNotIn(KEY, str(failure.exception))
        self.assertIn(KEY, self.store.read_text())  # Left for the user to repair.

    def test_invalid_entries_are_skipped_but_valid_ones_still_load(self):
        valid = self.save()
        stored = json.loads(self.store.read_text())
        stored["models"].append({"id": "api:broken", "format": "openai", "baseUrl": "http://evil.example.com",
                                 "model": "x", "apiKey": KEY})
        self.store.write_text(json.dumps(stored))
        self.assertEqual([model["id"] for model in providers.load_api_models()], [valid["id"]])
        self.assertFalse(providers.is_known("api:broken"))
        self.assertEqual(providers.api_model_secret("api:broken"), "")

    def test_loose_permissions_are_tightened_on_read(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        self.save()
        self.store.chmod(0o644)
        providers.load_api_models()
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)

    def test_symbolic_link_store_is_refused(self):
        if os.name == "nt":
            self.skipTest("POSIX links")
        target = self.root / "elsewhere.json"
        target.write_text('{"version": 1, "models": []}')
        self.store.parent.mkdir(parents=True)
        self.store.symlink_to(target)
        with self.assertRaises(providers.ProviderError):
            self.save()
        self.assertEqual(target.read_text(), '{"version": 1, "models": []}')

    def test_a_saved_key_is_reused_only_for_the_destination_it_was_saved_for(self):
        model = self.save(headerName="X-Gateway-Key")
        for change in ({"baseUrl": "https://attacker.example/v1"}, {"baseUrl": "https://api.example.com/v2"},
                       {"baseUrl": "https://api.example.com:8443/v1"}, {"headerName": "X-Leak"},
                       {"format": "anthropic"}):
            with self.subTest(change=change):
                for attempt in (lambda: providers.validated_api_model({**change, "apiKey": ""}, model["id"]),
                                lambda: providers.save_api_model({"id": model["id"], **change})):
                    with self.assertRaises(providers.ProviderError) as failure:
                        attempt()
                    self.assertIn("Enter the API key again", str(failure.exception))
                    self.assertNotIn(KEY, str(failure.exception))
        self.assertEqual(providers.api_model(model["id"])["baseUrl"], "https://api.example.com/v1")
        # The same destination (header names are case-insensitive) keeps the key.
        entry, _ = providers.validated_api_model({"headerName": "x-gateway-key", "model": "gpt-z"}, model["id"])
        self.assertEqual(entry["apiKey"], KEY)
        entry, _ = providers.validated_api_model({"baseUrl": "https://attacker.example/v1", "apiKey": "typed-key-0123"},
                                                 model["id"])
        self.assertEqual(entry["apiKey"], "typed-key-0123")

    def test_base_urls_that_url_parsers_read_differently_are_rejected(self):
        for base, message in (
            ("https://evil.example\\.good.example/v1", "backslashes"),
            ("https://evil%2eexample/v1", "percent-encoding"),
            ("https://[fe80::1%25en0]/v1", "percent-encoding"),
            ("https://bücher.example/v1", "plain ASCII"),
            ("https://127.1/v1", "four decimal numbers"),
            ("https://0x7f000001/v1", "four decimal numbers"),
            ("https://010.0.0.1/v1", "four decimal numbers"),
            ("https://1.2.3.4.5/v1", "four decimal numbers"),
            ("https://127.0.0.1./v1", "four decimal numbers"),
            ("https://evil.example^.good/v1", "letters, digits"),
            ("https://api.example.com/v1/../admin", "'.' and '..'"),
            ("https://api.example.com/v1/%2e%2E/admin", "'.' and '..'"),
        ):
            with self.subTest(base=base), self.assertRaises(providers.ProviderError) as failure:
                self.save(baseUrl=base)
            self.assertIn(message, str(failure.exception))
        for base, host in (("https://xn--bcher-kva.example/v1", "xn--bcher-kva.example"),
                           ("https://10.0.0.1:8443/v1", "10.0.0.1"), ("https://gw_1.internal/v1", "gw_1.internal"),
                           ("http://[::1]:8000/v1", "::1"), ("https://API.Example.com/v1", "api.example.com")):
            with self.subTest(base=base):
                self.assertEqual(self.save(baseUrl=base)["host"], host)

    def test_private_paths_cover_the_store_and_every_secret_folder(self):
        self.save()
        state = self.store.parent.resolve()
        self.assertEqual(providers.private_paths(), [self.store.resolve(), state / ".api-models-staging",
                                                     state / "codex-homes", state / "claude-settings"])
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex"), "CLAUDE_CONFIG_DIR": str(self.root / "c")}):
            self.assertEqual(providers.user_login_files(), [(self.root / "codex" / "auth.json").resolve(),
                                                            (self.root / "c" / ".credentials.json").resolve()])

    def test_a_relative_store_override_names_the_same_file_from_every_folder(self):
        relative = os.path.relpath(self.store, providers.ROOT)
        self.assertFalse(Path(relative).is_absolute())
        with mock.patch.dict(os.environ, {providers.STORE_ENV: relative}):
            first = providers.store_path()
            previous = Path.cwd()
            try:
                os.chdir(self.root)
                second = providers.store_path()
            finally:
                os.chdir(previous)
        self.assertEqual(first.resolve(), self.store.resolve())
        self.assertEqual(second.resolve(), self.store.resolve())

    def test_delete_and_test_results(self):
        model = self.save()
        result = providers.record_api_model_test(model["id"], {"ok": False, "status": 401,
                                                               "message": "x" * 500, "latencyMs": 41.5})
        self.assertEqual((result["lastTest"]["ok"], result["lastTest"]["status"]), (False, 401))
        self.assertEqual(len(result["lastTest"]["message"]), providers.MAX_TEST_MESSAGE_CHARS)
        with self.assertRaises(providers.ProviderError):
            providers.record_api_model_test("api:missing", {"ok": True})
        self.assertTrue(providers.delete_api_model(model["id"]))
        self.assertFalse(providers.delete_api_model(model["id"]))
        self.assertEqual(providers.load_api_models(), [])
        self.assertFalse(providers.is_known(model["id"]))


if __name__ == "__main__":
    unittest.main()
