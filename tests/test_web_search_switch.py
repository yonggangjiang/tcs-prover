"""Web search through an OpenAI-format API connection's own search service (plan v3 section 1.8): the
connection's switch, its probe search, what a job records when it starts, and Codex's provider settings."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import providers
from ui import server

KEY = "sk-search-switch-test-0001"


class Endpoint:
    """An OpenAI-format endpoint on 127.0.0.1 with a model list and a search service whose answer is ``search``."""

    def __init__(self):
        self.requests, self.search = [], (200, {"output": "Result text the model would read.", "results": []})
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, value):
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.endswith("/models"):
                    return self.reply(200, {"data": [{"id": "gpt-6.1-sol"}], "has_more": False})
                return self.reply(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                endpoint.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                          "body": body})
                return self.reply(*endpoint.search)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class SwitchCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir()
        for change in (mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.root / "state" / "api-models.json")}),
                       mock.patch.object(server, "grant_windows_access")):
            change.start()
            self.addCleanup(change.stop)
        self.endpoint = Endpoint()
        self.addCleanup(self.endpoint.close)
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        self.addCleanup(self.http.server_close)

    def save(self, **values):
        body = {"format": "openai", "baseUrl": self.endpoint.base, "apiKey": KEY, "label": "Proxy", **values}
        return self.http.save_api_model(body)["connection"]

    def searches(self):
        return [request for request in self.endpoint.requests if request["path"].endswith("/alpha/search")]


class ConnectionSwitchTests(SwitchCase):
    def test_a_new_or_existing_connection_has_web_search_off(self):
        connection = self.save()
        self.assertFalse(connection["webSearch"])
        self.assertEqual(self.searches(), [])
        # An entry saved before the switch existed reads as off.
        store = json.loads(providers.store_path().read_text())
        for entry in store["models"]:
            entry.pop("webSearch", None)
        providers.store_path().write_text(json.dumps(store))
        self.assertFalse(providers.api_connection(connection["id"], cached=False)["webSearch"])

    def test_turning_it_on_sends_one_probe_search_and_keeps_it_on_when_the_service_answers(self):
        connection = self.save(webSearch=True)
        self.assertTrue(connection["webSearch"])
        self.assertTrue(connection["webSearchCheck"]["ok"])
        self.assertIn("answered", connection["webSearchCheck"]["message"])
        [search] = self.searches()
        self.assertEqual(search["path"], "/v1/alpha/search")
        self.assertEqual(search["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(search["body"]["commands"]["search_query"][0]["q"], server.WEB_SEARCH_PROBE_QUERY)
        self.assertEqual(search["body"]["settings"]["external_web_access"], True)
        # Saving it again unchanged does not search again; a new key does.
        self.save(id=connection["id"], webSearch=True, apiKey="")
        self.assertEqual(len(self.searches()), 1)
        self.save(id=connection["id"], webSearch=True, apiKey=KEY + "-new")
        self.assertEqual(len(self.searches()), 2)

    def test_a_service_that_does_not_answer_a_search_leaves_it_off_and_says_why(self):
        for answer, words in (((404, {"error": "no such route"}), "HTTP 404"),
                              ((200, {"results": []}), "not a search result"),
                              ((200, b"<html>login</html>"), "not a search result")):
            with self.subTest(answer=answer):
                self.endpoint.search = answer
                connection = self.save(webSearch=True)
                self.assertFalse(connection["webSearch"])
                self.assertFalse(connection["webSearchCheck"]["ok"])
                self.assertIn(words, connection["webSearchCheck"]["message"])
                self.assertFalse(providers.api_connection(connection["id"], cached=False)["webSearch"])

    def test_an_anthropic_connection_never_has_it(self):
        entry = providers._validated_connection({"format": "anthropic", "baseUrl": "https://gateway.example.com",
                                                 "apiKey": "sk-ant-0000000000000000", "webSearch": True})
        self.assertFalse(entry["webSearch"])
        with self.assertRaises(providers.ProviderError):
            providers._validated_connection({"format": "openai", "baseUrl": self.endpoint.base, "apiKey": KEY,
                                             "webSearch": "yes"})


class JobTests(SwitchCase):
    def model(self, **values):
        connection = self.save(**values)
        return connection["models"][0]["modelId"], connection["id"]

    def options(self, model, mode="live"):
        return server.App._workflow_options(author_model=model, critic_model=model, writer_model=model,
                                            review_model=model, retrieval_mode=mode,
                                            retrieval_cutoff_utc="-1" if mode == "disabled" else "")

    def test_a_new_live_job_records_the_connections_it_searches_through(self):
        on, connection = self.model(webSearch=True)
        self.assertEqual(self.options(on)["apiWebSearch"], [connection])
        off, _ = self.model(label="Other")
        self.assertEqual(self.options(off)["apiWebSearch"], [])
        self.assertEqual(self.options(on, mode="disabled")["apiWebSearch"], [])

    def test_the_recorded_choice_reaches_every_runner_and_a_job_from_before_gets_none(self):
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run("Exact statement")
        self.assertNotIn("api_web_search", " ".join(app._team_runner_options()))
        app.state["apiWebSearch"] = ["api:proxy"]
        options = app._team_runner_options(conversation=True)
        self.assertEqual(options[options.index("api_web_search=[\"api:proxy\"]") - 1], "--set")
        app._save_job_settings(app.state)
        restored = server.restore_saved_app(server.App(app.trace_file, self.runs))
        self.assertEqual(restored.state["apiWebSearch"], ["api:proxy"])


class ProviderSettingsTests(SwitchCase):
    def test_a_connection_the_job_searches_through_gets_codex_s_own_web_search(self):
        model = self.save(webSearch=True)["models"][0]["modelId"]
        flags = ["-c", "model_providers.tcs_api.supports_standalone_web_search=true",
                 "-c", "features.standalone_web_search=true", "-c", "suppress_unstable_features_warning=true"]
        with mock.patch.object(runtime, "API_WEB_SEARCH", frozenset()):
            arguments = runtime.provider_arguments(model)
            self.assertFalse(any("standalone_web_search" in argument for argument in arguments))
            self.assertFalse(runtime.codex_web_search_tool(model))  # A Lite model has no search tool here.
        connection = providers.api_model(model)["sourceId"]
        with mock.patch.object(runtime, "API_WEB_SEARCH", frozenset({connection})):
            arguments = runtime.provider_arguments(model)
            self.assertEqual(arguments[-len(flags):], flags)
            self.assertTrue(runtime.codex_web_search_tool(model))

    def test_the_runner_reads_the_job_s_list(self):
        with mock.patch.object(runtime, "API_WEB_SEARCH", frozenset()):
            runtime.set_web_search(["api:proxy"])
            self.assertEqual(runtime.API_WEB_SEARCH, frozenset({"api:proxy"}))
            runtime.set_web_search(None)
            self.assertEqual(runtime.API_WEB_SEARCH, frozenset({"api:proxy"}))
            with self.assertRaises(runtime.Error):
                runtime.set_web_search(["../etc"])


if __name__ == "__main__":
    unittest.main()
