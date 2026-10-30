"""API model endpoints, the /state model catalog, launch checks and restore migration.

Everything runs offline: requests go only to local mock endpoints, and no
model or CLI is started.
"""

import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
import workflow_runner as runtime
from runner import providers
from ui import batches, cli, retrieval, review, server

KEY = "sk-tcs-server-test-0123456789"
ANTHROPIC_KEY = "sk-ant-server-test-9876543210"
CUTOFF = "2026-10-02T14:13:12Z"
ROLES = ("author", "critic", "writer")


class MockEndpoint:
    """A local OpenAI/Anthropic-shaped endpoint that records every request."""

    def __init__(self):
        self.requests, self.release = [], threading.Event()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                headers = {name.lower(): value for name, value in self.headers.items()}
                endpoint.requests.append({"path": self.path, "headers": headers, "body": body})
                if self.path.startswith("/slow"):
                    endpoint.release.wait(10)
                    return self.reply(200, {"output_text": "late"})
                if self.path.startswith("/redirect"):
                    return self.reply(307, {}, location="/target/v1/responses")
                if self.path.startswith("/basic") and body.get("stream"):
                    # Accepts only the minimal request, as some gateways do.
                    return self.reply(400, {"error": {"message": "Unsupported parameter: 'tools'.",
                                                      "type": "invalid_request_error"}})
                if self.path.endswith("/responses"):
                    offered = headers.get("authorization", "").removeprefix("Bearer ") or headers.get("x-gateway-key", "")
                    if offered == KEY and body.get("stream"):
                        return self.events([
                            {"type": "response.created", "response": {"model": "gpt-gateway-2026"}},
                            {"type": "response.output_text.delta", "delta": '{"ok": '},
                            {"type": "response.output_text.delta", "delta": "true}"},
                            {"type": "response.completed", "response": {"model": "gpt-gateway-2026"}}])
                    if offered == KEY:
                        return self.reply(200, {"id": "resp_1", "model": "gpt-gateway-2026", "status": "completed",
                                                "output": [{"type": "message", "content": [
                                                    {"type": "output_text", "text": "OK"}]}]})
                    # Echo the credential, as careless gateways do, to prove redaction.
                    return self.reply(401, {"error": {"message": f"Incorrect API key provided: {offered}",
                                                      "type": "invalid_request_error"}})
                if self.path.endswith("/v1/messages"):
                    offered = headers.get("x-api-key") or headers.get("authorization", "").removeprefix("Bearer ")
                    if offered == ANTHROPIC_KEY and body.get("stream"):
                        return self.events([
                            {"type": "message_start", "message": {"model": "claude-gateway"}},
                            {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "tool_use", "name": "StructuredOutput", "input": {}}},
                            {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "input_json_delta", "partial_json": '{"ok": true}'}},
                            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                            {"type": "message_stop"}])
                    if offered == ANTHROPIC_KEY:
                        return self.reply(200, {"type": "message", "model": "claude-gateway",
                                                "content": [{"type": "text", "text": "OK"}]})
                    return self.reply(401, {"type": "error", "error": {
                        "type": "authentication_error", "message": f"invalid x-api-key {offered}"}})
                return self.reply(404, {"error": {"message": "no such route"}})

            def events(self, events):
                raw = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def reply(self, status, value, location=None):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class StoreCase(unittest.TestCase):
    """A private API model store and an empty runs folder per test."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.store = self.root / "state" / "api-models.json"
        for change in (mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.store)}),
                       mock.patch.object(server, "grant_windows_access")):
            change.start()
            self.addCleanup(change.stop)

    def openai_model(self, **values):
        return providers.save_api_model({"format": "openai", "baseUrl": "https://gateway.example.com/v1",
                                         "model": "gpt-gateway", "apiKey": KEY, "label": "Gateway", **values})

    def new_app(self, statement="Exact statement"):
        app = server.App(runs=self.runs, load_latest=False)
        app._new_run(statement)
        app._save("checked-statement.md", f"# Statement sent directly to the proof author\n\n{statement}\n")
        app.state["runId"] = app.run_dir.name
        return app


class HttpCase(StoreCase):
    """A real Server on 127.0.0.1 with its session token."""

    def setUp(self):
        super().setUp()
        self.http = server.Server((server.HOST, 0), runs=self.runs)
        worker = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def stop():
            self.http.shutdown()
            self.http.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)

    def call(self, path, body=None, *, token=True):
        headers = {"Origin": self.http.origin}
        if token:
            headers["X-TCS-Prover-Token"] = self.http.token
        raw = None
        if body is not None:
            raw, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=20)
        try:
            connection.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        # No response of any endpoint may ever carry a key.
        for secret in (KEY, ANTHROPIC_KEY):
            self.assertNotIn(secret.encode(), data)
        return response.status, json.loads(data)


class ApiModelEndpointTests(HttpCase):
    def test_create_list_update_and_delete_never_return_the_key(self):
        status, created = self.call("/api-models", {
            "format": "openai", "baseUrl": "https://gateway.example.com/v1/", "model": "gpt-gateway",
            "apiKey": f"Bearer {KEY}", "label": "Team gateway"})
        self.assertEqual(status, 200)
        model = created["model"]
        self.assertEqual((model["id"], model["label"], model["baseUrl"], model["host"]),
                         ("api:team-gateway", "Team gateway", "https://gateway.example.com/v1", "gateway.example.com"))
        self.assertEqual((model["headerName"], model["hasKey"], model["keyHint"]), ("Authorization", True, "…6789"))
        self.assertIn(model["id"], [row["id"] for row in created["model_catalog"]])
        self.assertEqual(created["model_efforts"][model["id"]], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(providers.api_model_secret(model["id"]), KEY)

        status, listing = self.call("/api-models")
        self.assertEqual(status, 200)
        self.assertEqual([row["id"] for row in listing["models"]], [model["id"]])
        self.assertNotIn("apiKey", listing["models"][0])
        self.assertEqual([(row["id"], row["headerName"], row["cli"]) for row in listing["formats"]],
                         [("openai", "Authorization", "Codex CLI"), ("anthropic", "x-api-key", "Claude CLI")])

        # An empty key on update keeps the saved one.
        status, updated = self.call("/api-models", {
            "id": model["id"], "format": "openai", "baseUrl": "https://gateway.example.com/v1",
            "headerName": "Authorization", "model": "gpt-gateway-2", "apiKey": "", "label": "Renamed"})
        self.assertEqual(status, 200)
        self.assertEqual((updated["model"]["label"], updated["model"]["model"]), ("Renamed", "gpt-gateway-2"))
        self.assertEqual(providers.api_model_secret(model["id"]), KEY)
        self.assertEqual(len(updated["models"]), 1)

        status, deleted = self.call("/api-models/delete", {"id": model["id"]})
        self.assertEqual((status, deleted["deleted"], deleted["warning"], deleted["models"]),
                         (200, model["id"], "", []))
        self.assertNotIn(model["id"], [row["id"] for row in deleted["model_catalog"]])
        status, missing = self.call("/api-models/delete", {"id": model["id"]})
        self.assertEqual((status, missing["error"]), (400, "That API model is not configured."))

    def test_store_is_private_and_a_failed_write_keeps_the_previous_models(self):
        self.call("/api-models", {"format": "anthropic", "baseUrl": "https://claude.example.com",
                                  "model": "claude-gw", "apiKey": ANTHROPIC_KEY})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.store.parent.stat().st_mode), 0o700)
        before = self.store.read_text(encoding="utf-8")
        with mock.patch.object(providers.os, "replace", side_effect=OSError("disk full")):
            status, value = self.call("/api-models", {"format": "openai", "baseUrl": "https://other.example.com",
                                                      "model": "m", "apiKey": KEY})
        self.assertEqual((status, value["error"]), (400, "disk full"))
        self.assertEqual(self.store.read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(path.name for path in self.store.parent.iterdir()),
                         [".api-models-staging", "api-models.json"])
        self.assertEqual(list((self.store.parent / ".api-models-staging").iterdir()), [])

    def test_invalid_settings_are_rejected_without_echoing_the_key(self):
        for body, message in (
            ({"format": "openai", "baseUrl": "http://gateway.example.com/v1", "model": "m"},
             "plain http is allowed only for localhost"),
            ({"format": "openai", "baseUrl": f"https://user:{KEY}@gateway.example.com", "model": "m"},
             "Remove credentials from the base URL"),
            ({"format": "openai", "baseUrl": "https://gateway.example.com", "model": "m", "requestSource": "bad\nsource"},
             "request source"),
            ({"format": "gemini", "baseUrl": "https://gateway.example.com", "model": "m"}, "OpenAI or Anthropic"),
            ({"format": "openai", "baseUrl": "https://gateway.example.com", "model": "m", "apiKey": ""},
             "Enter the API key."),
        ):
            with self.subTest(message=message):
                status, value = self.call("/api-models", {"apiKey": KEY, **body})
                self.assertEqual(status, 400)
                self.assertIn(message, value["error"])
        self.assertFalse(self.store.exists())

    def test_endpoints_require_the_session_token(self):
        self.openai_model()
        self.assertEqual(self.call("/api-models", token=False)[0], 403)
        for path in ("/api-models", "/api-models/delete", "/api-models/test"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, {"id": "api:gateway"}, token=False)[0], 403)
        self.assertTrue(providers.is_known("api:gateway"))

    def test_delete_is_refused_while_a_running_job_uses_the_model(self):
        model = self.openai_model()
        app = self.new_app()
        self.http.jobs[app.state["runId"]] = app
        app.state.update(phase="running", criticModel=model["id"])
        status, value = self.call("/api-models/delete", {"id": model["id"]})
        self.assertEqual(status, 400)
        self.assertEqual(value["error"], "Gateway is in use by a running job. Stop that job before removing "
                                         "this API model.")
        self.assertTrue(providers.is_known(model["id"]))
        # A job waiting to resume cannot change its model, so the model stays until no job waits for it.
        app.state["phase"] = "paused"
        status, value = self.call("/api-models/delete", {"id": model["id"]})
        self.assertEqual(status, 400)
        self.assertEqual(value["error"], "Gateway is used by 1 job waiting to start or resume, which cannot "
                                         "continue without it. Edit the connection instead, or delete that job first.")
        self.assertTrue(providers.is_known(model["id"]))
        app.state["phase"] = "done"
        status, value = self.call("/api-models/delete", {"id": model["id"]})
        self.assertEqual((status, value["warning"]), (200, ""))
        self.assertFalse(providers.is_known(model["id"]))

    def test_format_change_is_refused_while_jobs_use_the_model(self):
        model = self.openai_model()
        app = self.new_app()
        self.http.jobs[app.state["runId"]] = app
        app.state.update(phase="paused", authorModel=model["id"])
        status, value = self.call("/api-models", {"id": model["id"], "format": "anthropic"})
        self.assertEqual(status, 400)
        self.assertIn("cannot change while jobs use this connection", value["error"])
        self.assertEqual(providers.api_model(model["id"])["format"], "openai")
        # Other edits stay possible, and a finished job no longer pins the format.
        self.assertEqual(self.call("/api-models", {"id": model["id"], "label": "Edited", "model": "gpt-gateway"})[0], 200)
        app.state["phase"] = "done"
        status, value = self.call("/api-models", {"id": model["id"], "format": "anthropic"})
        # A new format is a new destination: the saved key is not reused for it.
        self.assertEqual(status, 400)
        self.assertIn("Enter the API key again", value["error"])
        status, value = self.call("/api-models", {"id": model["id"], "format": "anthropic", "apiKey": KEY,
                                                  "model": "gpt-gateway"})
        self.assertEqual((status, value["model"]["format"], value["model"]["headerName"]), (200, "anthropic", "x-api-key"))

    def removed_model_job(self, label="Lab Sol", phase="paused"):
        """A waiting job whose critic used an API model that is then removed."""
        model = self.openai_model(label=label)["id"]
        app = self.new_app()
        self.http.jobs[app.state["runId"]] = app
        app.state.update(phase=phase, criticModel=model, apiModels=server.api_model_snapshot([model]))
        # Removed outside the page (the page refuses it while a job waits for the model).
        self.assertTrue(providers.delete_api_model(model))
        return model, app

    def test_adding_a_removed_model_again_restores_its_id_whatever_its_name(self):
        endpoint = {"format": "openai", "baseUrl": "https://gateway.example.com/v1/", "model": "gpt-gateway",
                    "apiKey": KEY, "headerName": "authorization"}
        for label, expected in (("", "Lab Sol"), ("Lab Sol endpoint", "Lab Sol endpoint")):
            with self.subTest(label=label):
                model, app = self.removed_model_job()
                # The job page and Resume name the settings to add again, never another model.
                with self.assertRaises(ValueError) as failure:
                    app._check_launch_models_locked()
                self.assertEqual(str(failure.exception),
                                 f"The API model {model} chosen for the critic is not configured. Add it again under "
                                 "New problem → Advanced settings → API connections with the same settings (Lab Sol · "
                                 "OpenAI format · https://gateway.example.com/v1 · model "
                                 "gpt-gateway) to continue this job.")
                status, value = self.call("/api-models", {**endpoint, "label": label})
                self.assertEqual((status, value["model"]["id"], value["model"]["label"]), (200, model, expected))
                app._check_launch_models_locked()  # Resume and Approve can launch again.
                del self.http.jobs[app.state["runId"]]
                self.assertEqual(self.call("/api-models/delete", {"id": model})[0], 200)
        # Other settings are another model, with an id of its own.
        model, app = self.removed_model_job()
        status, value = self.call("/api-models", {**endpoint, "model": "gpt-other", "label": "Other"})
        self.assertEqual((status, value["model"]["id"]), (200, "api:other"))
        self.assertFalse(providers.is_known(model))

    def test_a_removed_id_is_never_recreated_for_another_format(self):
        model, app = self.removed_model_job(label="Lab Sol")
        anthropic = {"format": "anthropic", "baseUrl": "https://gw.example.com", "model": "claude-x", "apiKey": KEY}
        # The same name gets another id: the job's id would move its session to the Claude CLI.
        status, value = self.call("/api-models", {**anthropic, "label": "Lab Sol"})
        self.assertEqual((status, value["model"]["id"]), (200, f"{model}-2"))
        self.assertFalse(providers.is_known(model))
        status, value = self.call("/api-models", {**anthropic, "id": model, "label": "Lab Sol"})
        self.assertEqual(status, 400)
        self.assertIn(f"Jobs still use {model} as an API model of another format", value["error"])
        self.assertFalse(providers.is_known(model))
        # Once no job names it, the id is free for any format.
        app.state["criticModel"] = "gpt-6.1-sol"
        status, value = self.call("/api-models", {**anthropic, "label": "Lab Sol", "model": "claude-y"})
        self.assertEqual((status, value["model"]["id"]), (200, model))

    def test_new_jobs_may_still_choose_another_model(self):
        with self.assertRaises(ValueError) as failure:
            server.App._workflow_options(include_review=False, critic_model="api:gone")
        self.assertTrue(str(failure.exception).endswith("or choose another model."))


class StaticAssetTests(HttpCase):
    def test_the_icon_the_page_links_is_served_like_its_other_assets(self):
        from ui import live_gateway

        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">',
                      (server.UI / "index.html").read_text(encoding="utf-8"))
        # The browser asks for the icon without the session token, as for the page itself.
        connection = http.client.HTTPConnection(server.HOST, self.http.server_port, timeout=5)
        try:
            connection.request("GET", "/favicon.svg")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "image/svg+xml"))
        self.assertEqual(body, (server.UI / "favicon.svg").read_bytes())
        self.assertTrue(body.startswith(b"<svg") and b"<script" not in body)
        self.assertEqual(live_gateway.STATIC["/favicon.svg"], ("favicon.svg", "image/svg+xml"))


class CatalogTests(HttpCase):
    def test_state_lists_every_model_with_its_efforts_and_no_secret(self):
        model = self.openai_model()
        anthropic = providers.save_api_model({"format": "anthropic", "baseUrl": "https://claude.example.com",
                                              "model": "claude-gw", "apiKey": ANTHROPIC_KEY, "label": "Claude gateway"})
        status, state = self.call("/state")
        self.assertEqual(status, 200)
        settings = state["workflow"]["settings"]
        catalog = settings["model_catalog"]
        self.assertEqual([row["id"] for row in catalog], [*runtime.OFFERED_MODELS, model["id"], anthropic["id"]])
        rows = {row["id"]: row for row in catalog}
        self.assertEqual((rows["gpt-6.1-sol"]["providerLabel"], rows["gpt-6.1-sol"]["supportsFast"]), ("Codex CLI", True))
        self.assertEqual((rows["claude-opus-5-5"]["providerLabel"], rows["claude-opus-5-5"]["supportsFast"]),
                         ("Claude CLI", False))
        self.assertEqual({key: rows[model["id"]][key] for key in ("provider", "format", "host", "hasKey", "label")},
                         {"provider": "api", "format": "openai", "host": "gateway.example.com", "hasKey": True,
                          "label": "gpt-gateway"})
        self.assertEqual(rows[anthropic["id"]]["efforts"], list(providers.CLAUDE_EFFORTS))
        efforts = settings["model_efforts"]
        self.assertTrue(set(rows) | set(runtime.LEGACY_MODELS) <= set(efforts))
        # Ultra is no longer offered by any model.
        self.assertNotIn("ultra", efforts["claude-haiku-5-5"])
        self.assertNotIn("ultra", efforts["gpt-6-luna"])
        self.assertNotIn("ultra", efforts["gpt-6.1-sol"])
        # The keys the current UI reads are unchanged.
        self.assertEqual(settings["models"], list(runtime.OFFERED_MODELS))
        self.assertEqual(settings["model"], runtime.MODEL)
        self.assertEqual(settings["reasoning_efforts"], list(runtime.EFFORTS))

    def test_job_state_reads_the_catalog_per_request(self):
        app = self.new_app()
        self.http.jobs[app.state["runId"]] = app
        path = f"/state?job={app.state['runId']}"
        self.assertEqual([row["id"] for row in self.call(path)[1]["workflow"]["settings"]["model_catalog"]],
                         list(runtime.OFFERED_MODELS))
        model = self.openai_model()
        state = self.call(path)[1]
        self.assertEqual(state["workflow"]["settings"]["model_catalog"][-1]["id"], model["id"])
        self.assertIn(model["id"], state["workflow"]["settings"]["model_efforts"])
        # The shared graph constant itself never gains user data.
        self.assertNotIn("model_catalog", server.PUBLIC_GRAPH["settings"])


class ProbeTests(HttpCase):
    def call(self, path, body=None, **kwargs):
        # These cases exercise minimal generation requests. Testing a source without an
        # explicit model now exercises discovery instead (covered in test_api_connections).
        if path == "/api-models/test" and isinstance(body, dict) and "model" not in body:
            saved = providers.api_model(body.get("id"))
            if saved is not None:
                body = {"model": saved["model"], **body}
        return super().call(path, body, **kwargs)

    def setUp(self):
        super().setUp()
        self.endpoint = MockEndpoint()
        self.addCleanup(self.endpoint.close)

    def saved(self, **values):
        return providers.save_api_model({"format": "openai", "baseUrl": self.endpoint.base + "/v1",
                                         "model": "gpt-gateway", "apiKey": KEY, "label": "Local", **values})

    def test_openai_success_sends_a_request_shaped_like_a_run_and_records_the_result(self):
        model = self.saved()
        status, result = self.call("/api-models/test", {"id": model["id"]})
        self.assertEqual(status, 200)
        self.assertEqual({key: result[key] for key in ("ok", "status", "message", "model", "recorded", "effort")},
                         {"ok": True, "status": 200, "message": 'The model replied with the requested JSON: {"ok": true}',
                          "model": "gpt-gateway-2026", "recorded": True, "effort": "max"})
        self.assertIsInstance(result["latencyMs"], int)
        [request] = self.endpoint.requests
        self.assertEqual(request["path"], "/v1/responses")
        self.assertEqual(request["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(request["headers"]["accept"], "text/event-stream")
        body = request["body"]
        # Streamed, one tool, a JSON answer (the prompt contract Codex uses for these models), at the effort.
        self.assertEqual((body["model"], body["stream"], body["reasoning"]["effort"], body["tool_choice"]),
                         ("gpt-gateway", True, "max", "auto"))
        self.assertEqual([tool["name"] for tool in body["tools"]], ["report_result"])
        self.assertIn('{"ok": true}', body["input"][0]["content"])
        self.assertNotIn("max_output_tokens", body)
        last = providers.api_model(model["id"])["lastTest"]
        self.assertEqual((last["ok"], last["status"], last["latencyMs"]), (True, 200, result["latencyMs"]))
        self.assertEqual(result["apiModel"]["lastTest"], last)

    def test_anthropic_success_uses_the_messages_path_and_automatic_key_header(self):
        for base in ("", "/v1"):
            with self.subTest(base=base):
                self.endpoint.requests.clear()
                model = providers.save_api_model({"format": "anthropic", "baseUrl": self.endpoint.base + base,
                                                  "model": "claude-gw", "apiKey": ANTHROPIC_KEY})
                status, result = self.call("/api-models/test", {"id": model["id"]})
                self.assertEqual((status, result["ok"], result["model"]), (200, True, "claude-gateway"))
                self.assertEqual(result["message"], 'The model answered through the StructuredOutput tool: {"ok": true}')
                [request] = self.endpoint.requests
                self.assertEqual(request["path"], "/v1/messages")
                self.assertEqual(request["headers"]["x-api-key"], ANTHROPIC_KEY)
                self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")
                self.assertNotIn("authorization", request["headers"])
                body = request["body"]
                # The Claude CLI's structured output tool, its output budget and the effort, streamed.
                self.assertEqual((body["stream"], body["max_tokens"], body["output_config"], body["tool_choice"]),
                                 (True, 32000, {"effort": "max"}, {"type": "auto"}))
                self.assertEqual([tool["name"] for tool in body["tools"]], ["StructuredOutput"])
                providers.save_api_model({"id": model["id"], "maxOutputTokens": 8000})
                self.endpoint.requests.clear()
                self.call("/api-models/test", {"id": model["id"]})
                self.assertEqual(self.endpoint.requests[0]["body"]["max_tokens"], 8000)

    def test_an_endpoint_that_refuses_the_shape_of_run_requests_is_named_as_such(self):
        model = self.saved(baseUrl=self.endpoint.base + "/basic")
        status, result = self.call("/api-models/test", {"id": model["id"]})
        self.assertEqual((status, result["ok"], result["status"]), (200, False, 400))
        self.assertEqual(result["message"], "The endpoint answers a minimal request but refuses the requests runs "
                                            "send (streamed, with a tool and a JSON answer at max effort): "
                                            "Unsupported parameter: 'tools'.")
        # The representative request, then one minimal request.
        self.assertEqual([request["body"].get("stream", False) for request in self.endpoint.requests], [True, False])

    def test_request_source_reaches_the_router_without_changing_authentication(self):
        model = self.saved(headerName="Zephy-TCS-Prover", requestSource="Zephy-TCS-Prover", label="Local menu only")
        status, result = self.call("/api-models/test", {"id": model["id"]})
        self.assertEqual((status, result["ok"]), (200, True))
        request = self.endpoint.requests[0]
        self.assertEqual(request["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(request["headers"]["user-agent"], "Zephy-TCS-Prover")
        self.assertNotIn("zephy-tcs-prover", request["headers"])
        self.assertNotIn("Local menu only", json.dumps(request))

    def test_default_request_source_and_edited_source_keep_the_saved_key(self):
        model = self.saved()
        self.call("/api-models/test", {"id": model["id"]})
        self.assertEqual(self.endpoint.requests[-1]["headers"]["user-agent"], "TCS-Prover")
        status, result = self.call("/api-models/test", {"id": model["id"], "requestSource": "Router-visible client"})
        self.assertEqual((status, result["ok"], result["recorded"]), (200, True, False))
        self.assertEqual(self.endpoint.requests[-1]["headers"]["user-agent"], "Router-visible client")
        self.assertEqual(self.endpoint.requests[-1]["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(providers.api_model(model["id"])["requestSource"], "")

    def test_rejected_keys_report_the_provider_message_with_the_key_redacted(self):
        model = self.saved()
        status, result = self.call("/api-models/test", {
            "id": model["id"], "format": "openai", "baseUrl": self.endpoint.base + "/v1",
            "model": "gpt-gateway", "apiKey": KEY + "-old"})
        self.assertEqual((status, result["ok"], result["status"], result["recorded"]), (200, False, 401, False))
        self.assertEqual(result["message"], "Incorrect API key provided: [redacted]")
        anthropic = providers.save_api_model({"format": "anthropic", "baseUrl": self.endpoint.base,
                                              "model": "claude-gw", "apiKey": "sk-ant-wrong-key-123456"})
        status, result = self.call("/api-models/test", {"id": anthropic["id"]})
        self.assertEqual((result["ok"], result["status"], result["message"]), (False, 401, "invalid x-api-key [redacted]"))
        self.assertEqual(providers.api_model(anthropic["id"])["lastTest"]["message"], "invalid x-api-key [redacted]")

    def test_redirects_are_reported_and_never_followed(self):
        model = self.saved(baseUrl=self.endpoint.base + "/redirect")
        status, result = self.call("/api-models/test", {"id": model["id"]})
        self.assertEqual((status, result["ok"], result["status"]), (200, False, 307))
        self.assertIn("redirects are not followed", result["message"])
        self.assertEqual([request["path"] for request in self.endpoint.requests], ["/redirect/responses"])

    def test_a_silent_endpoint_times_out(self):
        model = self.saved(baseUrl=self.endpoint.base + "/slow")
        with mock.patch.object(server, "API_TEST_TIMEOUT_SECONDS", 0.5):
            status, result = self.call("/api-models/test", {"id": model["id"]})
        self.assertEqual((status, result["ok"], result["status"]), (200, False, None))
        self.assertEqual(result["message"], "No response within 0.5 seconds (TimeoutError).")
        self.assertFalse(providers.api_model(model["id"])["lastTest"]["ok"])

    def test_unreachable_endpoints_name_the_network_error(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()  # Nothing listens on this port any more.
        status, result = self.call("/api-models/test", {"format": "openai", "baseUrl": f"http://127.0.0.1:{port}/v1",
                                                        "model": "m", "apiKey": KEY})
        self.assertEqual((status, result["ok"], result["status"]), (200, False, None))
        self.assertEqual(result["message"], "The connection was refused (ConnectionRefusedError).")

    def test_an_unsaved_form_is_tested_without_saving_anything(self):
        status, result = self.call("/api-models/test", {
            "format": "openai", "baseUrl": self.endpoint.base + "/v1", "headerName": "Authorization",
            "model": "gpt-gateway", "apiKey": KEY})
        self.assertEqual((status, result["ok"], result["recorded"]), (200, True, False))
        self.assertFalse(self.store.exists())
        status, value = self.call("/api-models/test", {"format": "openai", "baseUrl": self.endpoint.base, "model": "m"})
        self.assertEqual((status, value["error"]), (400, "Enter the API key."))
        status, value = self.call("/api-models/test", {"id": "api:missing"})
        self.assertEqual((status, value["error"]), (400, "That API model is not configured."))
        self.assertEqual(len(self.endpoint.requests), 1)

    def test_a_saved_key_is_never_sent_to_a_destination_the_caller_chose(self):
        attacker = MockEndpoint()
        self.addCleanup(attacker.close)
        model = self.saved()
        for change in ({"baseUrl": attacker.base + "/v1", "headerName": "X-Leak"},
                       {"baseUrl": attacker.base + "/v1"}, {"format": "anthropic"}):
            with self.subTest(change=change):
                status, value = self.call("/api-models/test", {"id": model["id"], **change})
                self.assertEqual(status, 400)
                self.assertIn("Enter the API key again", value["error"])
                # Saving the same change cannot move the saved key either.
                status, value = self.call("/api-models", {"id": model["id"], **change})
                self.assertEqual(status, 400)
                self.assertIn("Enter the API key again", value["error"])
        self.assertEqual((attacker.requests, self.endpoint.requests), ([], []))
        self.assertEqual(providers.api_model(model["id"])["baseUrl"], self.endpoint.base + "/v1")
        self.assertEqual(providers.api_model_secret(model["id"]), KEY)
        # With the key typed again, the edited endpoint is tested (and saved) normally.
        status, result = self.call("/api-models/test", {"id": model["id"], "baseUrl": attacker.base + "/v1",
                                                        "apiKey": KEY})
        self.assertEqual((status, result["ok"], result["recorded"]), (200, True, False))
        self.assertEqual(attacker.requests[0]["headers"]["authorization"], f"Bearer {KEY}")

    def test_editing_a_saved_model_tests_with_its_saved_key(self):
        model = self.saved()
        form = {"id": model["id"], "format": "openai", "baseUrl": self.endpoint.base + "/v1",
                "headerName": "Authorization", "model": "gpt-gateway", "apiKey": ""}
        status, result = self.call("/api-models/test", form)
        self.assertEqual((status, result["ok"], result["recorded"]), (200, True, True))
        self.assertEqual(self.endpoint.requests[-1]["headers"]["authorization"], f"Bearer {KEY}")
        # An edited endpoint is tested, but the saved model's last test is not overwritten.
        status, result = self.call("/api-models/test", {**form, "model": "gpt-other"})
        self.assertEqual((status, result["ok"], result["recorded"]), (200, True, False))
        self.assertEqual(self.endpoint.requests[-1]["body"]["model"], "gpt-other")
        self.assertEqual(providers.api_model(model["id"])["model"], "gpt-gateway")


class ProbeHelperTests(StoreCase):
    def test_validation_without_saving_fills_gaps_from_the_saved_model(self):
        model = self.openai_model()
        entry, saved = providers.validated_api_model({"model": "gpt-other", "apiKey": ""}, model["id"])
        self.assertEqual((entry["model"], entry["apiKey"], entry["baseUrl"]),
                         ("gpt-other", KEY, "https://gateway.example.com/v1"))
        self.assertEqual((saved["model"], saved["apiKey"]), ("gpt-gateway", KEY))
        entry, saved = providers.validated_api_model({"format": "anthropic", "baseUrl": "https://c.example.com",
                                                      "model": "c", "apiKey": ANTHROPIC_KEY})
        self.assertIsNone(saved)
        self.assertEqual(entry["headerName"], "x-api-key")
        with self.assertRaises(providers.ProviderError):
            providers.validated_api_model({}, "gpt-6.1-sol")
        with self.assertRaises(providers.ProviderError):
            providers.validated_api_model({"format": "openai", "baseUrl": "https://g.example.com", "model": "m"})
        self.assertEqual(len(providers.load_api_models()), 1)

    def test_messages_lose_every_form_of_the_key_before_they_are_clipped(self):
        key = 'sk-quote"slash/0123456789'
        escaped = json.dumps(key)[1:-1]
        for text in (f"echo {key}", f"json {escaped}", f"url {server.quote(key, safe='')}",
                     "x" * 290 + key + "tail"):
            with self.subTest(text=text[:20]):
                message = server._api_test_text(text, key)
                self.assertLessEqual(len(message), providers.MAX_TEST_MESSAGE_CHARS)
                for form in (key, escaped, server.quote(key, safe=""), key[:8]):
                    self.assertNotIn(form, message)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-inherited-secret-55555"}):
            self.assertEqual(server._api_test_text("a sk-inherited-secret-55555 b", key), "a [redacted] b")

    def test_legacy_header_cannot_replace_content_type_or_format_authentication(self):
        path, headers, body = server._api_test_request({
            "format": "anthropic", "baseUrl": "https://gw.example.com/proxy/V1/", "headerName": "content-type",
            "model": "c", "apiKey": KEY})
        self.assertEqual(path, "/proxy/v1/messages")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["x-api-key"], KEY)
        self.assertNotIn("content-type", headers)
        self.assertEqual(json.loads(body)["max_tokens"], 16)


class LaunchValidationTests(StoreCase):
    def options(self, **values):
        return server.App._workflow_options(include_review=False, **values)

    def test_unknown_removed_and_unconfigured_models_are_named_per_role(self):
        offered = ", ".join(runtime.OFFERED_MODELS)
        for model, message in (
            ("gpt-9", f"Unknown model 'gpt-9' for the critic. Choose {offered}, or an api:<id> model configured "
                      "under Advanced settings → API connections."),
            ("deepseek-v4-pro", "DeepSeek is no longer supported; choose another model for the critic, for "
                                "example gpt-6.1-sol."),
            ("api:gone", "The API model api:gone chosen for the critic is not configured. Add it under Advanced "
                         "settings → API connections, or choose another model."),
        ):
            with self.subTest(model=model), self.assertRaises(ValueError) as failure:
                self.options(critic_model=model)
            self.assertEqual(str(failure.exception), message)

    def test_an_api_model_without_a_saved_key_cannot_launch(self):
        self.store.parent.mkdir(parents=True)
        self.store.write_text(json.dumps({"version": 1, "models": [{
            "id": "api:keyless", "label": "Keyless", "format": "openai", "baseUrl": "https://gateway.example.com/v1",
            "headerName": "Authorization", "model": "m", "apiKey": ""}]}), encoding="utf-8")
        with self.assertRaises(ValueError) as failure:
            self.options(author_model="api:keyless")
        self.assertEqual(str(failure.exception), "Add an API key for Keyless in Advanced settings → API connections.")

    def test_claude_models_need_the_claude_cli(self):
        which = shutil.which
        missing = mock.patch.object(runtime.shutil, "which",
                                    side_effect=lambda name, *a, **k: None if name == "claude" else which(name, *a, **k))
        with missing, self.assertRaises(ValueError) as failure:
            self.options(writer_model="claude-sonnet-5-5")
        self.assertEqual(str(failure.exception),
                         "Claude CLI is not installed or is not on PATH; install Claude Code to use Claude models.")
        present = mock.patch.object(runtime.shutil, "which",
                                    side_effect=lambda name, *a, **k: "/usr/local/bin/claude" if name == "claude"
                                    else which(name, *a, **k))
        with present:
            options = self.options(author_model="claude-opus-5-5", author_effort="ultra", speed_mode="fast")
        # Ultra is retired: it is kept as given and runs at Max. Fast stays saved; the runner runs Claude standard.
        self.assertEqual((options["authorModel"], options["authorEffort"], options["speedMode"]),
                         ("claude-opus-5-5", "ultra", "fast"))
        self.assertEqual(runtime.effective_effort("claude-opus-5-5", options["authorEffort"]), "max")
        self.assertNotIn("apiModels", options)

    def test_web_cutoff_timestamps_require_native_codex_models(self):
        api = self.openai_model()["id"]
        which = shutil.which
        no_claude = mock.patch.object(runtime.shutil, "which",
                                      side_effect=lambda name, *a, **k: None if name == "claude" else which(name, *a, **k))
        with mock.patch.object(retrieval.isolated_compiler, "supported", return_value=True), no_claude:
            for role, model in (("author_model", "claude-haiku-5-5"), ("critic_model", api),
                                ("writer_model", "claude-opus-5-5")):
                with self.subTest(model=model), self.assertRaises(ValueError) as failure:
                    self.options(retrieval_mode="historical", retrieval_cutoff_utc=CUTOFF, **{role: model})
                # Reported before any CLI or key check.
                self.assertEqual(str(failure.exception), "Web cutoff timestamps require Codex CLI models for every proof role.")
                self.assertEqual(str(failure.exception), runtime.HISTORICAL_CODEX_ONLY)
            with self.assertRaises(ValueError) as failure:
                server.App._workflow_options(review_model="claude-haiku-5-5", retrieval_mode="historical",
                                             retrieval_cutoff_utc=CUTOFF)
            self.assertEqual(str(failure.exception), runtime.HISTORICAL_CODEX_ONLY)

    def test_idle_jobs_recheck_their_models_before_launching(self):
        model = self.openai_model()
        for phase, action in (("paused", "resume"), ("reviewed", "approve"), ("prepared", "start_prepared_run")):
            with self.subTest(action=action):
                app = self.new_app()
                app.state.update(phase=phase, authorModel=model["id"], review={"statement": "S", "notes": ""},
                                 preparedRun=phase == "prepared")
                providers.delete_api_model(model["id"])
                with mock.patch.object(app, "_launch_workflow_locked", side_effect=AssertionError("launched")):
                    with self.assertRaises(ValueError) as failure:
                        getattr(app, action)()
                self.assertIn(f"The API model {model['id']} chosen for the proof author is not configured",
                              str(failure.exception))
                self.assertEqual(app.state["phase"], phase)
                model = self.openai_model()

    def test_api_model_jobs_record_a_snapshot_and_never_pass_the_key(self):
        model = self.openai_model(requestSource="Zephy-TCS-Prover")
        app = server.App(runs=self.runs, load_latest=False)
        launched = {}

        def popen(command, **kwargs):
            launched.update(command=command, env=kwargs.get("env"))
            raise OSError("stopped before a real launch")

        with mock.patch.object(server.subprocess, "Popen", side_effect=popen), self.assertRaises(OSError):
            app.start_direct_statement("Exact statement", author_model=model["id"], critic_model=model["id"])
        text = (app.run_dir / server.JOB_SETTINGS_FILENAME).read_text(encoding="utf-8")
        settings = json.loads(text)
        self.assertEqual(settings["authorModel"], model["id"])
        self.assertEqual(settings["apiModels"], {model["id"]: {
            "label": "Gateway", "format": "openai", "baseUrl": "https://gateway.example.com/v1",
            "headerName": "Authorization", "requestSource": "Zephy-TCS-Prover", "model": "gpt-gateway",
            "sourceId": model["id"], "sourceLabel": "Gateway"}})
        self.assertNotIn(KEY, text)
        command = launched["command"]
        self.assertEqual(command[command.index("--author-model") + 1], model["id"])
        self.assertNotIn(KEY, " ".join(command))
        self.assertNotIn(KEY, json.dumps(launched["env"]))
        for name in os.listdir(app.run_dir):
            if (app.run_dir / name).is_file():
                self.assertNotIn(KEY, (app.run_dir / name).read_text(encoding="utf-8"), name)

    def test_a_restored_snapshot_keeps_only_well_formed_display_data(self):
        app = self.new_app()
        saved = {"api:gateway": {"label": "Gateway", "format": "openai", "baseUrl": "https://g.example.com/v1",
                                 "headerName": "Authorization", "model": "m", "apiKey": KEY},
                 "api:broken": {"label": 3}, "gpt-6.1-sol": {"label": "x"}}
        app._save(server.JOB_SETTINGS_FILENAME, json.dumps({"apiModels": saved, "authorModel": "api:gateway"}))
        app.add_trace({"kind": "status", "stage": "solve", "label": "Goal started"})
        restored = server.restore_saved_jobs(self.runs)[app.run_dir.name]
        self.assertEqual(restored.state["apiModels"], {"api:gateway": {
            "label": "Gateway", "format": "openai", "baseUrl": "https://g.example.com/v1",
            "headerName": "Authorization", "requestSource": "", "model": "m",
            "sourceId": "api:gateway", "sourceLabel": "Gateway"}})
        catalog = batches.BatchCatalog(self.runs.resolve(), current_root=self.runs.resolve())
        self.assertEqual(catalog.state("__root__", app.run_dir.name)["apiModels"], restored.state["apiModels"])


class RemovedModelRestoreTests(StoreCase):
    def saved_run(self, name, settings, records=(), pause=None):
        directory = self.runs / name
        directory.mkdir()
        (directory / "draft.md").write_text("# Draft problem\n\nSaved statement\n", encoding="utf-8")
        (directory / "checked-statement.md").write_text(
            "# Statement sent directly to the proof author\n\nSaved statement\n", encoding="utf-8")
        (directory / server.JOB_SETTINGS_FILENAME).write_text(json.dumps(settings), encoding="utf-8")
        (directory / "transcript.jsonl").write_text(
            "".join(json.dumps({"time": "2026-10-20T10:00:00+00:00", **record}) + "\n" for record in records),
            encoding="utf-8")
        if pause is not None:
            (directory / server.PAUSE_FILENAME).write_text(json.dumps(pause), encoding="utf-8")
        return directory

    def test_saved_deepseek_roles_restore_as_the_default_with_a_visible_warning(self):
        self.saved_run("deepseek", {
            "skipStatementReview": True, "reviewModel": "deepseek-v4-pro", "authorModel": "deepseek-v4-pro",
            "criticModel": "gpt-6-astra", "writerModel": "deepseek/deepseek-v4-pro", "authorEffort": "max"})
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs_lock = self.runs, threading.RLock()
        manager.jobs = server.restore_saved_jobs(self.runs)
        app = manager.jobs["deepseek"]
        self.assertEqual([app.state[f"{role}Model"] for role in ("review", *ROLES)],
                         ["gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-sol"])
        # The skipped statement reviewer never runs, so it is not named.
        warning = "Proof author and LaTeX editor: DeepSeek is no longer supported; this role now uses GPT 6.1 Sol."
        self.assertEqual(app.state["settingsWarning"], warning)
        self.assertIn(providers.REMOVED_MODEL_WARNING, warning)
        self.assertEqual(manager.job_list()[0]["settingsWarning"], warning)

    def test_transcript_only_runs_are_migrated_too(self):
        records = [{"kind": "request", "stage": stage, "label": "Exact solve input", "model": model,
                    "reasoningEffort": "high"} for stage, model in (("solve", "deepseek/deepseek-v4-pro"),
                                                                    ("critic", "gpt-6-sol"),
                                                                    ("final", "deepseek-v4-pro"))]
        directory = self.saved_run("legacy", {}, records)
        (directory / server.JOB_SETTINGS_FILENAME).unlink()
        app = server.restore_saved_jobs(self.runs)["legacy"]
        self.assertEqual([app.state[f"{role}Model"] for role in ROLES], ["gpt-6.1-sol", "gpt-6-sol", "gpt-6.1-sol"])
        self.assertTrue(app.state["settingsWarning"].startswith(
            "Proof author and LaTeX editor: DeepSeek is no longer supported; this role now uses GPT 6.1 Sol."))

    def test_an_error_paused_deepseek_job_resumes_with_the_replacement_and_keeps_the_warning(self):
        pause = {"status": "paused", "node": "author", "stage": "solve", "state": {"statement": "Saved statement"},
                 "pauseCause": "error", "error": "Network error", "elapsedSeconds": 30, "autoResumeAt": 0}
        record = {"kind": "request", "stage": "solve", "label": "Exact solve input", "model": "deepseek-v4-pro",
                  "reasoningEffort": "max"}
        settings = {"skipStatementReview": True, "authorModel": "deepseek-v4-pro", "criticModel": "gpt-6.1-sol",
                    "writerModel": "gpt-6.1-sol", "thinkingHours": 24}
        self.saved_run("deepseek", settings, [record], pause)
        self.saved_run("codex", {**settings, "authorModel": "gpt-6.1-sol"}, [{**record, "model": "gpt-6.1-sol"}], pause)
        jobs = server.restore_saved_jobs(self.runs)
        replaced, unchanged = jobs["deepseek"], jobs["codex"]
        self.assertEqual((replaced.state["phase"], unchanged.state["phase"]), ("paused", "paused"))
        self.assertGreater(replaced.state["autoResumeAt"], 0)
        self.assertGreater(unchanged.state["autoResumeAt"], 0)
        warning = "Proof author: DeepSeek is no longer supported; this role now uses GPT 6.1 Sol."
        self.assertEqual(replaced.state["settingsWarning"], warning)
        self.assertEqual(replaced.state["authorModel"], "gpt-6.1-sol")
        # The retry resumes with the documented replacement.
        resumed = []
        self.assertTrue(replaced.auto_resume_if_due(lambda: resumed.append(1),
                                                    now=replaced.state["autoResumeAt"] + 1))
        self.assertEqual(resumed, [1])
        # Its settings are rewritten once with the replacement, and the warning survives restarts.
        replaced._save_job_settings(replaced.state)
        settings = json.loads((self.runs / "deepseek" / server.JOB_SETTINGS_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual((settings["authorModel"], settings["replacedRoles"]), ("gpt-6.1-sol", ["author"]))
        again = server.restore_saved_jobs(self.runs)["deepseek"]
        self.assertEqual(again.state["settingsWarning"], warning)
        self.assertGreater(again.state["autoResumeAt"], 0)

    def test_a_job_whose_api_model_was_removed_keeps_checking_until_it_is_added_again(self):
        pause = {"status": "paused", "node": "author", "stage": "solve", "state": {"statement": "Saved statement"},
                 "pauseCause": "error", "error": "Network error", "elapsedSeconds": 30, "autoResumeAt": 0}
        record = {"kind": "request", "stage": "solve", "label": "Exact solve input", "model": "api:gone",
                  "reasoningEffort": "max"}
        settings = {"skipStatementReview": True, "authorModel": "api:gone", "criticModel": "gpt-6.1-sol",
                    "writerModel": "gpt-6.1-sol", "thinkingHours": 24}
        self.saved_run("gone", settings, [record], pause)
        app = server.restore_saved_jobs(self.runs)["gone"]
        self.assertEqual(app.state["phase"], "paused")
        self.assertGreater(app.state["autoResumeAt"], 0)
        self.assertEqual(app.state["settingsWarning"],
                         "The API model api:gone chosen for the proof author is not configured. Add it again under "
                         "New problem → Advanced settings → API connections with the display name “gone” to continue "
                         "this job. The job checks again every 1 minute and continues by itself once the model is "
                         "configured.")
        attempts, due = [], app.state["autoResumeAt"]
        self.assertTrue(app.auto_resume_if_due(lambda: attempts.append(1), now=due))
        self.assertEqual(attempts, [])
        self.assertEqual(app.attention()["kind"], "settings")
        # Added again with that id: the next check resumes it.
        self.openai_model(id="api:gone", label="gone")
        self.assertTrue(app.auto_resume_if_due(lambda: attempts.append(1), now=app.state["autoResumeAt"]))
        self.assertEqual(attempts, [1])
        self.assertIsNone(app.attention())

    def test_automatic_resume_waits_while_a_model_is_removed_and_continues_when_it_is_back(self):
        model = self.openai_model()["id"]
        pause = {"status": "paused", "node": "author", "stage": "solve", "state": {"statement": "Saved statement"},
                 "pauseCause": "error", "error": "Network error", "elapsedSeconds": 30, "autoResumeAt": 0}
        record = {"kind": "request", "stage": "solve", "label": "Exact solve input", "model": model,
                  "reasoningEffort": "max"}
        settings = {"skipStatementReview": True, "authorModel": model, "criticModel": "gpt-6.1-sol",
                    "writerModel": "gpt-6.1-sol", "thinkingHours": 24}
        self.saved_run("later", settings, [record], pause)
        app = server.restore_saved_jobs(self.runs)["later"]
        self.assertGreater(app.state["autoResumeAt"], 0)
        self.assertEqual(app.state["settingsWarning"], "")
        entry = providers.api_model(model)
        providers.delete_api_model(model)
        attempts, due = [], app.state["autoResumeAt"]
        for tick in range(3):
            self.assertTrue(app.auto_resume_if_due(lambda: attempts.append(1), now=due + 1))
            due = app.state["autoResumeAt"]
        self.assertEqual(attempts, [])
        self.assertGreater(due, 0)
        notes = [record["text"] for record in app.state["trace"] if record.get("kind") == "diagnostic"]
        self.assertEqual(notes, [f"Automatic resume waits for the model settings: The API model {model} chosen for "
                                 "the proof author is not configured. Add it again under New problem → Advanced "
                                 f"settings → API connections with the display name “{model[4:]}” to continue this job. "
                                 "The job checks again every 1 minute and continues by itself once they are fixed."])
        providers.save_api_model({"id": model, "format": "openai", "baseUrl": entry["baseUrl"],
                                  "model": entry["model"], "apiKey": KEY, "label": entry["label"]})
        self.assertTrue(app.auto_resume_if_due(lambda: attempts.append(1), now=due))
        self.assertEqual(attempts, [1])

    def test_a_cli_author_continuation_reports_the_replacement(self):
        source = self.saved_run("source", {"authorModel": "deepseek-v4-pro", "criticModel": "deepseek-v4-pro"})
        self.assertEqual(server.saved_research_source(source)["replacedRoles"], ["author", "critic"])
        manager = object.__new__(server.Server)
        manager.runs, manager.jobs, manager.jobs_lock = self.runs, {}, threading.RLock()
        with mock.patch.object(server.App, "_launch_solver_locked", return_value=(object(), object())), \
                mock.patch.object(server.App, "_spawn_worker"):
            app = manager.start_saved_research_job(source, {"criticModel": "gpt-6-sol"})
        self.assertEqual((app.state["authorModel"], app.state["criticModel"]), ("gpt-6.1-sol", "gpt-6-sol"))
        warning = "Proof author: DeepSeek is no longer supported; this role now uses GPT 6.1 Sol."
        self.assertEqual(app.state["settingsWarning"], warning)
        self.assertEqual([record["text"] for record in app.state["trace"] if record.get("label") == "Saved model replaced"],
                         [warning])


class PrivateRunnerStateTests(StoreCase):
    def test_server_start_removes_settings_left_by_killed_runners(self):
        parent = self.store.parent / "claude-settings"
        parent.mkdir(parents=True)
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        (parent / f"run-{ended.pid}-abc").mkdir()
        (parent / f"run-{ended.pid}-abc" / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "left"}}')
        http = server.Server((server.HOST, 0), runs=self.runs)
        http.server_close()
        self.assertEqual(list(parent.iterdir()), [])

    def test_runner_output_that_echoes_a_key_never_reaches_the_trace_or_the_answer(self):
        secret = "sk-echoed-by-gateway-0123456789"
        saved = set(runtime._REGISTERED_SECRETS)
        self.addCleanup(lambda: (runtime._REGISTERED_SECRETS.clear(), runtime._REGISTERED_SECRETS.update(saved)))
        runtime.register_secret(secret)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            # A Codex app-server error, a command's output and an author message that all echo the key.
            runtime.emit("codex_event", "solve", node="author", root=True, event={
                "method": "error", "params": {"error": {"message": f"401 Invalid API key provided: {secret}"}}})
            runtime.emit("codex_event", "solve", node="author", root=True, event={
                "method": "item/completed", "params": {"item": {"type": "commandExecution", "id": "c1",
                                                                "aggregatedOutput": f"TOKEN={secret}"}}})
            runtime.emit("codex_event", "solve", node="author", root=True, event={
                "method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "m1",
                                                                "text": f"The gateway said {secret}."}}})
            runtime.emit("diagnostic", "solve", text=f"Claude author turn failed: {secret}")
        self.assertNotIn(secret, output.getvalue())
        app = self.new_app()
        app.state.update(phase="running", stage="solve", activeNode="author")
        process = mock.Mock(stdout=io.StringIO(output.getvalue()))
        process.wait.return_value = process.poll.return_value = 0
        token = app.active_token = app.worker_token = object()
        app._read_output(process, token)
        self.assertEqual(app.state["output"], "The gateway said [redacted].")
        self.assertNotIn(secret, json.dumps(app.state["trace"]))
        self.assertNotIn(secret, app.trace_file.read_text(encoding="utf-8"))
        partial = app.run_dir / "partial-output.md"
        self.assertTrue(not partial.exists() or secret not in partial.read_text(encoding="utf-8"))


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class StopCleanupTests(unittest.TestCase):
    """Stop interrupts a runner before killing its group, so its cleanup removes key-bearing files."""

    RUNNER = ("import signal, sys, time\n"
              "if sys.argv[2] == 'ignore':\n"
              "    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
              "open(sys.argv[1], 'w').write('settings with a key')\n"
              "try:\n"
              "    print('ready', flush=True)\n"
              "    time.sleep(60)\n"
              "finally:\n"
              "    import os\n"
              "    os.unlink(sys.argv[1])\n")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name) / "state"
        store = mock.patch.dict(os.environ, {providers.STORE_ENV: str(self.state / "api-models.json")})
        store.start()
        self.addCleanup(store.stop)

    def start(self, mode):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        marker = Path(folder.name) / "settings.json"
        process = subprocess.Popen([sys.executable, "-c", self.RUNNER, str(marker), mode], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, **server.isolated_process_options())
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")
        process.stdout.close()
        return process, marker

    def test_stop_lets_the_runner_clean_up_first(self):
        process, marker = self.start("cleanup")
        server.stop_process_tree(process)
        self.assertIsNotNone(process.returncode)
        self.assertFalse(marker.exists())

    def test_a_runner_that_ignores_the_interrupt_is_killed_after_the_grace_period(self):
        process, marker = self.start("ignore")
        with mock.patch.object(server, "STOP_GRACE_SECONDS", 0.3):
            server.stop_process_tree(process)
        self.assertEqual(process.returncode, -9)
        self.assertTrue(marker.exists())  # Removed by the next runner or server start instead.

    def test_stop_removes_the_private_settings_a_killed_runner_left(self):
        # Windows always ends the runner without its cleanup; POSIX when it ignores the interrupt.
        process, _ = self.start("ignore")
        parent = self.state / providers.CLAUDE_SETTINGS
        killed, live = parent / f"run-{process.pid}-abc123", parent / f"run-{os.getppid()}-def456"
        for folder in (killed, live):
            folder.mkdir(parents=True)
            (folder / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "left-behind"}}')
        with mock.patch.object(server, "STOP_GRACE_SECONDS", 0.3):
            server.stop_process_tree(process)
        self.assertEqual(sorted(path.name for path in parent.iterdir()), [live.name])


class CommandLineModelTests(StoreCase):
    def exit_code(self, entry, *arguments):
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as stop:
                entry([*arguments, "--help"])
        return stop.exception.code, " ".join(errors.getvalue().split())

    def web_ui(self, arguments):
        with mock.patch.object(sys, "argv", ["web_ui.py", *arguments]):
            cli.main()

    def test_claude_and_configured_api_ids_are_accepted_everywhere(self):
        api = self.openai_model()["id"]
        for model in ("claude-opus-5-5", "claude-haiku-5-5", api, "gpt-5.6-terra"):
            with self.subTest(model=model):
                self.assertEqual(self.exit_code(self.web_ui, "--author-model", model, "-criticModel", model,
                                                "--writerModel", model)[0], 0)
                self.assertEqual(self.exit_code(review.review_worker_main, "--review-model", model)[0], 0)
                self.assertEqual(self.exit_code(runtime.main, "--model", model, "--author-model", model)[0], 0)

    def test_unknown_ids_fail_with_the_offered_choices(self):
        for model in ("api:missing", "deepseek-v4-pro", "gpt-9"):
            with self.subTest(model=model):
                code, message = self.exit_code(self.web_ui, "--author-model", model)
                self.assertEqual(code, 2)
                if model == "deepseek-v4-pro":
                    self.assertIn("DeepSeek is no longer supported", message)
                else:
                    self.assertIn(f"Unknown model '{model}'. Choose {', '.join(runtime.OFFERED_MODELS)}, or an api:<id>",
                                  message)
                self.assertEqual(self.exit_code(review.review_worker_main, "--review-model", model)[0], 2)

    def test_help_names_the_api_models_and_where_fast_applies(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            self.web_ui(["--help"])
        text = " ".join(output.getvalue().split())
        self.assertIn("claude-haiku-5-5, or an api:<id> model from Advanced settings → API connections", text)
        self.assertIn("fast applies to Codex CLI models only, other models run standard", text)

    def test_a_relative_store_override_reaches_the_runner_child(self):
        model = self.openai_model()["id"]
        relative = os.path.relpath(self.store, server.ROOT)
        self.assertFalse(os.path.isabs(relative))
        elsewhere = self.root / "elsewhere"  # The runner works in its run folder, not the server's.
        elsewhere.mkdir()
        environment = {**os.environ, providers.STORE_ENV: relative}
        result = subprocess.run([sys.executable, "-B", str(server.ROOT / "workflow_runner.py"), "x.yaml",
                                 "--author-model", model, "--help"], cwd=elsewhere, env=environment,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_terminal_runs_accept_claude_and_api_models(self):
        api = self.openai_model()["id"]
        which = shutil.which
        with mock.patch.object(runtime.shutil, "which", side_effect=lambda name, *a, **k: "/usr/local/bin/claude"
                               if name == "claude" else which(name, *a, **k)):
            options = cli.direct_cli_options(author_model="claude-sonnet-5-5", critic_model=api,
                                             writer_model="claude-haiku-5-5", reasoning_effort="ultra")
        self.assertEqual((options["author_model"], options["critic_model"], options["writer_model"]),
                         ("claude-sonnet-5-5", api, "claude-haiku-5-5"))
        # A retired Ultra is kept as given; the runner runs it as max and says so once.
        self.assertEqual((options["author_effort"], options["critic_effort"]), ("ultra", "ultra"))
        self.assertEqual(runtime.effective_effort("claude-sonnet-5-5", options["author_effort"]), "max")


if __name__ == "__main__":
    unittest.main()
