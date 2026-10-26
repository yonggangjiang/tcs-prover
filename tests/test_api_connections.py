"""API connections expose their discovered models without duplicating credentials.

All requests use loopback fixtures; runtime checks inspect arguments and environments.
"""

import json
import unittest
from unittest import mock

import workflow_runner as runtime
from runner import claude_cli, providers
from tests.test_api_model_discovery import ModelsEndpoint
from tests.test_api_models_server import ANTHROPIC_KEY, HttpCase, KEY, StoreCase
from ui import server


OPTIONS = [{"id": "model-a", "label": "First model"}, {"id": "model-z", "label": "Last model"}]


class ConnectionRegistryTests(StoreCase):
    def connection(self, **values):
        return providers.save_api_connection({"format": "openai", "baseUrl": "https://gateway.example.com/v1",
                                              "apiKey": KEY, "label": "Research gateway", **values}, OPTIONS)

    def test_one_saved_connection_offers_every_model_with_its_source(self):
        connection = self.connection()
        self.assertEqual([row["id"] for row in connection["models"]], ["model-a", "model-z"])
        choices = {row["id"]: row for row in providers.catalog()}
        self.assertNotIn(connection["id"], choices)
        self.assertEqual(choices["gpt-6.1-sol"]["sourceId"], "codex")
        self.assertEqual(choices["claude-opus-5-5"]["sourceId"], "claude")
        for option in connection["models"]:
            row = choices[option["modelId"]]
            self.assertEqual((row["sourceId"], row["sourceLabel"], row["wireModel"]),
                             (connection["id"], "Research gateway", option["id"]))
            self.assertEqual(providers.lookup(option["modelId"]), row)
            self.assertEqual(providers.wire_model(option["modelId"]), option["id"])
            self.assertEqual(providers.api_model_secret(option["modelId"]), KEY)
        saved = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["models"]), 1)
        self.assertEqual(self.store.read_text(encoding="utf-8").count(KEY), 1)
        self.assertNotIn(KEY, json.dumps(connection))
        self.assertNotIn(KEY, json.dumps(providers.catalog()))

    def test_legacy_selected_model_id_is_preserved_when_its_connection_gains_options(self):
        legacy = self.openai_model()
        providers.record_api_model_test(legacy["id"], {"ok": True, "status": 200, "message": "prior"})
        connection = providers.save_api_connection({"id": legacy["id"], "apiKey": ""},
                                                   [{"id": "gpt-gateway", "label": "Original model"}] + OPTIONS)
        original = next(row for row in connection["models"] if row["id"] == "gpt-gateway")
        self.assertEqual(original["modelId"], legacy["id"])
        self.assertEqual(providers.wire_model(legacy["id"]), "gpt-gateway")
        self.assertEqual(providers.api_model_secret(legacy["id"]), KEY)
        self.assertEqual(providers.lookup(legacy["id"])["sourceId"], connection["id"])
        self.assertEqual(providers.api_connection(connection["id"])["id"], legacy["id"])
        self.assertEqual(len(providers.load_api_connections()), 1)
        self.assertNotIn(KEY, json.dumps(providers.source_catalog()))

    def test_unlisted_model_ids_cannot_resolve_or_borrow_the_connection_key(self):
        connection = self.connection()
        unknown = providers.api_model_id(connection["id"], "not-offered")
        self.assertFalse(providers.is_known(unknown))
        self.assertIsNone(providers.api_model(unknown))
        self.assertEqual(providers.api_model_secret(unknown), "")

    def test_an_empty_or_invalid_discovered_catalog_cannot_replace_a_saved_connection(self):
        connection = self.connection()
        before = self.store.read_bytes()
        for choices in ([], [{"id": ""}], [{"id": "--cli-option"}], [{"id": "line\nbreak"}]):
            with self.subTest(choices=choices), self.assertRaises(providers.ProviderError):
                providers.save_api_connection({"id": connection["id"], "apiKey": ""}, choices)
            self.assertEqual(self.store.read_bytes(), before)

    def test_generated_selection_snapshot_retains_its_source_and_wire_model(self):
        connection = self.connection(requestSource="Proof client")
        identity = connection["models"][-1]["modelId"]
        [snapshot] = server.api_model_snapshot([identity]).values()
        self.assertEqual(snapshot["sourceId"], connection["id"])
        self.assertEqual(snapshot["sourceLabel"], "Research gateway")
        self.assertEqual(snapshot["model"], "model-z")
        self.assertEqual(snapshot["requestSource"], "Proof client")
        self.assertNotIn(KEY, json.dumps(snapshot))

    def test_model_identities_survive_connection_rename_refresh_and_key_replacement(self):
        connection = self.connection()
        identities = {row["id"]: row["modelId"] for row in connection["models"]}
        changed = providers.save_api_connection({"id": connection["id"], "label": "Renamed gateway", "apiKey": ""},
                                                list(reversed(OPTIONS)) + [{"id": "new-model", "label": "New"}])
        for option in changed["models"]:
            if option["id"] in identities:
                self.assertEqual(option["modelId"], identities[option["id"]])
        replacement = "sk-replacement-0123456789"
        providers.save_api_connection({"id": connection["id"], "apiKey": replacement}, OPTIONS)
        for identity in identities.values():
            self.assertEqual(providers.api_model_secret(identity), replacement)

    def test_equal_wire_models_from_two_connections_keep_distinct_credentials(self):
        first = self.connection()
        second = self.connection(label="Other gateway", apiKey=ANTHROPIC_KEY)
        first_id, second_id = first["models"][0]["modelId"], second["models"][0]["modelId"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(providers.wire_model(first_id), providers.wire_model(second_id))
        self.assertEqual(providers.api_model_secret(first_id), KEY)
        self.assertEqual(providers.api_model_secret(second_id), ANTHROPIC_KEY)
        self.assertTrue(providers.delete_api_model(first["id"]))
        self.assertFalse(providers.is_known(first_id))
        self.assertTrue(providers.is_known(second_id))

    def test_connection_delete_removes_all_model_choices_and_their_key(self):
        connection = self.connection()
        identities = [row["modelId"] for row in connection["models"]]
        self.assertTrue(providers.delete_api_model(connection["id"]))
        for identity in identities:
            self.assertFalse(providers.is_known(identity))
            self.assertEqual(providers.api_model_secret(identity), "")
        self.assertNotIn(KEY, self.store.read_text(encoding="utf-8"))

    def test_selected_openai_model_uses_connection_source_and_shared_key(self):
        connection = self.connection(requestSource="Zephy-TCS-Prover")
        identity = next(row["modelId"] for row in connection["models"] if row["id"] == "model-z")
        arguments = runtime.provider_arguments(identity)
        self.assertIn('model_providers.tcs_api.base_url="https://gateway.example.com/v1"', arguments)
        self.assertIn('model_providers.tcs_api.http_headers={"User-Agent"="Zephy-TCS-Prover"}', arguments)
        self.assertEqual(runtime.environment(identity)["TCS_PROVER_API_TOKEN"], KEY)
        self.assertEqual(providers.wire_model(identity), "model-z")
        self.assertNotIn(KEY, " ".join(arguments))

    def test_selected_anthropic_model_uses_connection_source_and_shared_key(self):
        connection = self.connection(format="anthropic", requestSource="Proof client", apiKey=ANTHROPIC_KEY)
        identity = next(row["modelId"] for row in connection["models"] if row["id"] == "model-z")
        environment = claude_cli.gateway_environment(identity, providers.api_model_secret(identity))
        self.assertEqual(environment["ANTHROPIC_API_KEY"], ANTHROPIC_KEY)
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], "https://gateway.example.com")
        self.assertEqual(environment["ANTHROPIC_CUSTOM_HEADERS"], "User-Agent: Proof client")
        self.assertEqual(environment["ANTHROPIC_DEFAULT_SONNET_MODEL"], "model-z")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", environment)


class ConnectionEndpointTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.endpoint = ModelsEndpoint()
        self.addCleanup(self.endpoint.close)

    def save(self, **values):
        return self.call("/api-models", {"format": "openai", "baseUrl": self.endpoint.base + "/v1",
                                          "apiKey": KEY, "label": "Local connection", **values})

    def test_save_needs_no_model_and_uses_the_server_discovered_options(self):
        status, result = self.save(models=[{"id": "client-invented"}])
        self.assertEqual(status, 200)
        connection = result["connection"]
        self.assertEqual([row["id"] for row in connection["models"]], ["gpt-a", "gpt-z"])
        self.assertEqual(len(result["connections"]), 1)
        choices = [row for row in result["model_catalog"] if row["sourceId"] == connection["id"]]
        self.assertEqual({row["wireModel"] for row in choices}, {"gpt-a", "gpt-z"})
        self.assertEqual({row["id"] for row in result["model_sources"]}, {"codex", "claude", connection["id"]})
        self.assertEqual(self.endpoint.requests[0]["headers"]["authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, json.dumps(result))

    def test_saved_discovery_refresh_persists_new_choices_and_returns_updated_catalog(self):
        status, result = self.save()
        self.assertEqual(status, 200)
        connection = result["connection"]
        identities = {row["id"]: row["modelId"] for row in connection["models"]}
        self.endpoint.reply = lambda path, headers: (200, {"data": [{"id": "gpt-z"}, {"id": "new-model"}]}, None)
        status, result = self.call("/api-models/discover", {"id": connection["id"], "apiKey": ""})
        self.assertEqual((status, result["ok"]), (200, True))
        saved = providers.api_connection(connection["id"])
        choices = {row["id"]: row["modelId"] for row in saved["models"]}
        self.assertEqual(set(choices), {"gpt-z", "new-model"})
        self.assertEqual(choices["gpt-z"], identities["gpt-z"])
        self.assertEqual({row["id"] for row in result["connections"]}, {connection["id"]})
        self.assertIn(choices["new-model"], {row["id"] for row in result["model_catalog"]})
        self.assertEqual(providers.api_model_secret(choices["new-model"]), KEY)

    def test_test_without_a_model_checks_discovery_without_a_generation_request(self):
        status, result = self.call("/api-models/test", {"format": "openai", "baseUrl": self.endpoint.base + "/v1",
                                                      "apiKey": KEY})
        self.assertEqual((status, result["ok"], result["status"]), (200, True, 200))
        self.assertEqual(len(self.endpoint.requests), 1)
        self.assertEqual(self.endpoint.requests[0]["path"], "/v1/models")
        self.assertFalse(self.store.exists())

    def test_readding_a_deleted_connection_restores_a_waiting_generated_selection(self):
        status, result = self.save()
        self.assertEqual(status, 200)
        connection = result["connection"]
        selected = connection["models"][-1]["modelId"]
        app = self.new_app()
        app.state.update(phase="paused", authorModel=selected, apiModels=server.api_model_snapshot([selected]))
        self.http.jobs[app.state["runId"]] = app
        status, result = self.call("/api-models/delete", {"id": connection["id"]})
        self.assertEqual(status, 200)
        self.assertIn("1 job", result["warning"])
        self.assertFalse(providers.is_known(selected))
        status, result = self.save(label="Recreated connection")
        self.assertEqual(status, 200)
        self.assertEqual(result["connection"]["id"], connection["id"])
        self.assertTrue(providers.is_known(selected))
        self.assertEqual(providers.api_model_secret(selected), KEY)

    def test_readding_a_legacy_source_without_a_model_restores_its_exact_saved_selection(self):
        legacy = self.openai_model(baseUrl=self.endpoint.base + "/v1", model="gpt-z")
        app = self.new_app()
        app.state.update(phase="paused", authorModel=legacy["id"], apiModels=server.api_model_snapshot([legacy["id"]]))
        self.http.jobs[app.state["runId"]] = app
        self.assertEqual(self.call("/api-models/delete", {"id": legacy["id"]})[0], 200)
        status, result = self.save(label="New local name")
        self.assertEqual(status, 200)
        self.assertEqual(result["connection"]["id"], legacy["id"])
        self.assertTrue(providers.is_known(legacy["id"]))
        self.assertEqual(providers.wire_model(legacy["id"]), "gpt-z")
        self.assertEqual(providers.api_model_secret(legacy["id"]), KEY)

    def test_failed_refresh_keeps_saved_models_and_credentials(self):
        status, result = self.save()
        self.assertEqual(status, 200)
        identity = result["connection"]["id"]
        before = self.store.read_bytes()
        self.endpoint.reply = lambda path, headers: (401, {"error": "Invalid API key"}, None)
        status, result = self.call("/api-models", {"id": identity, "apiKey": ""})
        self.assertEqual(status, 400)
        self.assertIn("Invalid API key", result["error"])
        self.assertEqual(self.store.read_bytes(), before)

    def test_save_cannot_overwrite_credentials_changed_while_models_are_loading(self):
        status, result = self.save()
        self.assertEqual(status, 200)
        connection = result["connection"]
        replacement = "sk-concurrent-replacement-0123456789"
        offered = [{"id": "gpt-a", "label": "gpt-a"}, {"id": "gpt-z", "label": "gpt-z"}]

        def concurrent_edit(entry):
            providers.save_api_connection({"id": connection["id"], "apiKey": replacement,
                                            "requestSource": "Other tab"}, offered)
            return {"ok": True, "status": 200, "models": offered}

        with mock.patch.object(server, "discover_api_models", side_effect=concurrent_edit):
            status, result = self.call("/api-models", {"id": connection["id"], "apiKey": "", "label": "Stale rename"})
        self.assertEqual(status, 400)
        self.assertIn("changed", result["error"])
        self.assertEqual(providers.api_connection(connection["id"])["requestSource"], "Other tab")
        self.assertEqual(providers.api_model_secret(connection["models"][0]["modelId"]), replacement)

    def test_save_cannot_recreate_a_connection_deleted_while_models_are_loading(self):
        status, result = self.save()
        self.assertEqual(status, 200)
        connection = result["connection"]

        def concurrent_delete(entry):
            providers.delete_api_model(connection["id"])
            return {"ok": True, "status": 200, "models": [{"id": "gpt-a", "label": "gpt-a"}]}

        with mock.patch.object(server, "discover_api_models", side_effect=concurrent_delete):
            status, result = self.call("/api-models", {"id": connection["id"], "apiKey": ""})
        self.assertEqual(status, 400)
        self.assertIsNone(providers.api_connection(connection["id"]))

    def test_running_generated_model_blocks_source_deletion_and_format_change(self):
        status, result = self.save()
        self.assertEqual(status, 200)
        connection = result["connection"]
        app = self.new_app()
        app.state.update(phase="running", authorModel=connection["models"][-1]["modelId"])
        app.worker_token = object()
        self.http.jobs[app.state["runId"]] = app
        status, result = self.call("/api-models/delete", {"id": connection["id"]})
        self.assertEqual(status, 400)
        self.assertIn("running job", result["error"])
        with mock.patch.object(server, "discover_api_models") as discover:
            status, result = self.call("/api-models", {"id": connection["id"], "format": "anthropic", "apiKey": KEY})
        self.assertEqual(status, 400)
        self.assertIn("cannot change", result["error"])
        discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
