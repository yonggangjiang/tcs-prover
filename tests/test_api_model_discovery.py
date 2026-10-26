"""Model discovery uses local HTTP fixtures and never launches a model or CLI."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from runner import providers
from tests.test_api_models_server import ANTHROPIC_KEY, HttpCase, KEY
from ui import server


class ModelsEndpoint:
    def __init__(self):
        self.requests = []
        self.reply = lambda path, headers: (200, {"data": [{"id": "gpt-z"}, {"id": "gpt-a"}]}, None)
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                headers = {name.lower(): value for name, value in self.headers.items()}
                endpoint.requests.append({"path": self.path, "headers": headers})
                status, value, location = endpoint.reply(self.path, headers)
                raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(raw)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.worker.join(timeout=2)


class ModelDiscoveryTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.endpoint = ModelsEndpoint()
        self.addCleanup(self.endpoint.close)

    def fields(self, **values):
        return {"format": "openai", "baseUrl": self.endpoint.base + "/v1", "apiKey": KEY, **values}

    def saved(self, **values):
        return providers.save_api_model({**self.fields(), "model": "saved-model", **values})

    def discover(self, fields=None, **kwargs):
        return self.call("/api-models/discover", self.fields() if fields is None else fields, **kwargs)

    def test_new_connection_lists_models_without_a_model_id_and_without_saving(self):
        status, result = self.discover()
        self.assertEqual((status, result["ok"], result["status"], result["models"]),
                         (200, True, 200, [{"id": "gpt-a", "label": "gpt-a"}, {"id": "gpt-z", "label": "gpt-z"}]))
        self.assertEqual(result["message"], "Found 2 models.")
        self.assertIsInstance(result["latencyMs"], int)
        self.assertTrue(result["testedAt"])
        self.assertFalse(self.store.exists())
        [request] = self.endpoint.requests
        self.assertEqual(request["path"], "/v1/models")
        self.assertEqual(request["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(request["headers"]["user-agent"], "TCS-Prover")

    def test_saved_key_reuse_keeps_the_model_and_last_test_unchanged(self):
        saved = self.saved()
        providers.record_api_model_test(saved["id"], {"ok": True, "message": "prior", "status": 200})
        before = self.store.read_text(encoding="utf-8")
        status, result = self.discover({"id": saved["id"], "apiKey": "", "headerName": "authorization"})
        self.assertEqual((status, result["ok"]), (200, True))
        self.assertEqual(self.endpoint.requests[0]["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(self.store.read_text(encoding="utf-8"), before)

    def test_saved_key_cannot_be_sent_to_an_edited_destination(self):
        saved = self.saved()
        for change in ({"baseUrl": self.endpoint.base + "/other"}, {"baseUrl": "https://other.example/v1"},
                       {"format": "anthropic"}):
            with self.subTest(change=change):
                status, result = self.discover({"id": saved["id"], "apiKey": "", **change})
                self.assertEqual(status, 400)
                self.assertIn("Enter the API key again", result["error"])
        self.assertEqual(self.endpoint.requests, [])
        # Source changes keep the saved key; legacy authentication overrides are ignored.
        status, result = self.discover({"id": saved["id"], "headerName": "X-Gateway-Key",
                                       "requestSource": "Zephy-TCS-Prover"})
        self.assertEqual((status, result["ok"]), (200, True))
        headers = self.endpoint.requests[0]["headers"]
        self.assertEqual(headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(headers["user-agent"], "Zephy-TCS-Prover")
        self.assertNotIn("x-gateway-key", headers)

    def test_anthropic_uses_v1_and_paginates_on_the_original_endpoint(self):
        cursor = "claude/one?key=other"

        def reply(path, headers):
            self.assertEqual(headers["x-api-key"], ANTHROPIC_KEY)
            self.assertEqual(headers["anthropic-version"], server.ANTHROPIC_VERSION)
            self.assertEqual(headers["user-agent"], "Zephy-TCS-Prover")
            self.assertNotIn("authorization", headers)
            if "?" not in path:
                return 200, {"data": [{"id": "claude-z", "display_name": "Claude Z"}], "has_more": True,
                             "last_id": cursor, "next_page": "https://attacker.example/leak"}, None
            self.assertEqual(parse_qs(urlsplit(path).query), {"after_id": [cursor]})
            return 200, {"data": [{"id": "claude-a", "display_name": "Claude A"}, {"id": "claude-z"}],
                         "has_more": False}, None

        self.endpoint.reply = reply
        for suffix in ("", "/v1", "/proxy/v1"):
            self.endpoint.requests.clear()
            status, result = self.discover(self.fields(format="anthropic", apiKey=ANTHROPIC_KEY,
                                                       baseUrl=self.endpoint.base + suffix,
                                                       requestSource="Zephy-TCS-Prover"))
            self.assertEqual((status, result["ok"], result["models"]),
                             (200, True, [{"id": "claude-a", "label": "Claude A"},
                                          {"id": "claude-z", "label": "Claude Z"}]))
            expected = "/proxy/v1/models" if suffix.startswith("/proxy") else "/v1/models"
            self.assertEqual([urlsplit(request["path"]).path for request in self.endpoint.requests],
                             [expected, expected])

    def test_authentication_errors_redact_keys_with_automatic_authentication(self):
        self.endpoint.reply = lambda path, headers: (401, {"error": {"message": f"Missing API key. {KEY}"}}, None)
        status, result = self.discover(self.fields(headerName="Zephy-TCS-Prover"))
        self.assertEqual((status, result["ok"], result["status"], result["models"]), (200, False, 401, []))
        self.assertIn("Missing API key. [redacted]", result["message"])
        [request] = self.endpoint.requests
        self.assertEqual(request["headers"]["authorization"], f"Bearer {KEY}")
        self.assertNotIn("zephy-tcs-prover", request["headers"])

    def test_request_source_is_optional_validated_and_separate_from_credentials(self):
        for source in ("non-ascii-ü", "line\r\ninjected", "tab\tvalue", "a" * 201, {"unexpected": "object"}):
            with self.subTest(source=source):
                status, result = self.discover(self.fields(requestSource=source))
                self.assertEqual(status, 400)
                self.assertIn("request source", result["error"])
        self.assertEqual(self.endpoint.requests, [])
        status, result = self.discover(self.fields(requestSource="  Source/1.0 (proof client)  "))
        self.assertEqual((status, result["ok"]), (200, True))
        self.assertEqual(self.endpoint.requests[0]["headers"]["user-agent"], "Source/1.0 (proof client)")

    def test_legacy_custom_header_store_reuses_key_and_preserves_identity(self):
        saved = self.saved()
        providers.record_api_model_test(saved["id"], {"ok": False, "status": 401, "message": "Missing API key"})
        value = json.loads(self.store.read_text(encoding="utf-8"))
        value["models"][0]["headerName"] = "Zephy-TCS-Prover"
        value["models"][0].pop("requestSource", None)
        self.store.write_text(json.dumps(value), encoding="utf-8")
        status, result = self.discover({"id": saved["id"]})
        self.assertEqual((status, result["ok"]), (200, True))
        model = providers.api_model(saved["id"])
        self.assertEqual((model["id"], model["headerName"], model["requestSource"]),
                         (saved["id"], "Authorization", ""))
        self.assertIsNone(model["lastTest"])
        self.assertEqual(self.endpoint.requests[0]["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(providers.api_model_secret(saved["id"]), KEY)

    def test_redirects_never_send_credentials_to_the_location(self):
        self.endpoint.reply = lambda path, headers: (307, {}, "https://attacker.example/models")
        status, result = self.discover()
        self.assertEqual((status, result["ok"], result["status"], result["models"]), (200, False, 307, []))
        self.assertIn("redirects are not followed", result["message"])
        self.assertEqual(len(self.endpoint.requests), 1)

    def test_empty_malformed_and_oversized_responses_have_clear_errors(self):
        cases = (({"data": []}, "No models are available"),
                 ({"models": [{"id": "wrong-shape"}]}, "valid model list"),
                 (b"<html>gateway</html>", "valid model list"),
                 (b"x" * (server.API_MODELS_MAX_RESPONSE_BYTES + 1), "too large"))
        for payload, message in cases:
            with self.subTest(message=message):
                self.endpoint.reply = lambda path, headers: (200, payload, None)
                status, result = self.discover()
                self.assertEqual((status, result["ok"], result["models"]), (200, False, []))
                self.assertIn(message, result["message"])

    def test_bad_model_ids_are_ignored_and_display_names_cannot_echo_the_key(self):
        self.endpoint.reply = lambda path, headers: (200, {"data": [None, {"id": ""}, {"id": "-option"},
            {"id": "control\ncharacter"}, {"id": KEY}, {"id": "a" * 201},
            {"id": "good", "display_name": f"Name with {KEY}"}, {"id": "good"}]}, None)
        status, result = self.discover()
        self.assertEqual((status, result["ok"], result["models"]),
                         (200, True, [{"id": "good", "label": "Name with [redacted]"}]))

    def test_failed_or_repeated_pagination_never_returns_a_partial_list(self):
        for last_id in (None, "same"):
            with self.subTest(last_id=last_id):
                self.endpoint.requests.clear()
                self.endpoint.reply = lambda path, headers: (200, {"data": [{"id": "claude-a"}],
                    "has_more": True, "last_id": last_id}, None)
                status, result = self.discover(self.fields(format="anthropic"))
                self.assertEqual((status, result["ok"], result["models"]), (200, False, []))
                self.assertIn("pagination cursor", result["message"])
                self.assertLessEqual(len(self.endpoint.requests), 2)
        self.endpoint.reply = lambda path, headers: ((401, {"error": {"message": "expired"}}, None)
            if "?" in path else (200, {"data": [{"id": "a"}], "has_more": True, "last_id": "a"}, None))
        status, result = self.discover(self.fields(format="anthropic"))
        self.assertEqual((status, result["ok"], result["status"], result["models"]), (200, False, 401, []))

    def test_endpoint_requires_a_token_a_key_and_an_existing_saved_id(self):
        self.assertEqual(self.discover(token=False)[0], 403)
        status, result = self.discover(self.fields(apiKey=""))
        self.assertEqual((status, result["error"]), (400, "Enter the API key."))
        status, result = self.discover({"id": "api:missing"})
        self.assertEqual((status, result["error"]), (400, "That API model is not configured."))
        self.assertEqual(self.endpoint.requests, [])

    def test_transport_failure_is_safe_and_retains_no_partial_models(self):
        with mock.patch.object(server, "_api_models_page", side_effect=TimeoutError(KEY)):
            status, result = self.discover()
        self.assertEqual((status, result["ok"], result["status"], result["models"]), (200, False, None, []))
        self.assertIn("No response within", result["message"])


if __name__ == "__main__":
    unittest.main()
